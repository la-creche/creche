"""The PEP service: identity, manifest, per-call enforcement, audit.

One process, one identity kind. A bearer resolves to a family (contract 04
§1) or to nothing at all.

This module owns HTTP and the process: the routes, the request-body cap, the
rate window, the lifespan (the upstream pool, its reload, the retention and
fault sweeps) and the log for a request nobody could identify. Every
decision, every execution and the audit record v2 live one layer below, in
`family_app.py`.

Fail closed: an unhandled error inside a call becomes a denial, and an
allowed call that cannot be recorded is reported as a failure even though
the effect happened (`AuditError` → 500). Unrecorded effects are worse.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets as secretslib
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .audit import AuditError, AuditLog
from .delegate import (
    UDS_BASE_URL,
    CachedTokenFile,
    DelegateDoor,
    DoorConfig,
    HttpDelegateDoor,
)
from .dispatch import DispatchConfig, DispatchDoor, HttpDispatchDoor
from .family_app import HTTP_NOT_MODIFIED, MANIFEST_ACTION, FamilyDeps, FamilyGate, Reply
from .family_audit import FamilyAudit
from .family_decisions import FAMILY_DENY_STATUS, FamilyReason
from .family_grants import FamilyStore
from .family_ids import rfc3339_s
from .fault_sweep import FAULT_SWEEP_INTERVAL_S, fault_sweep_loop
from .faults import FaultWriter
from .gatekeeper import Gatekeeper, HttpApprovalNotifier, Verdict
from .gates import GATE_ID_RE
from .mcp_client import IDLE_TTL_S, StdioUpstreamPool, UpstreamPool, UpstreamSpec
from .release_door import ReleaseDoor, SpoolReleaseDoor
from .reload_pool import ReloadablePool, Serving, serving_of
from .reload_wiring import ReloadTrigger, RosterHealth, RosterSource, RosterState
from .reload_wiring import install as install_reload

log = logging.getLogger("chaperone.app")


def _no_remover() -> None:
    """What the lifespan calls when it installed no reload trigger."""


@dataclass(frozen=True)
class PepConfig:
    # The log for a request that resolved to no family: an unknown bearer and
    # an oversized body. It is NOT contract 04 §6's audit, which hangs off
    # `rework_dir` and whose every record names a family. Two directories,
    # because a record with no family fits neither §6.1's table nor a reader
    # that expects one. `PEP_AUDIT_DIR` on the unit.
    audit_dir: Path
    # The rework state root (contract 04): grants/, audit/ and faults/pep/
    # hang off it. Required: a PEP without it would resolve no bearer at all
    # and serve nothing.
    rework_dir: Path
    # The rows the pool starts with. `__main__` sets none: with `roster`
    # set, the lifespan reads the rows itself, on the path a reload takes,
    # and with none there is no MCP server to start.
    upstreams: dict[str, UpstreamSpec] = field(default_factory=dict[str, UpstreamSpec])
    secrets: dict[str, str] = field(default_factory=dict[str, str])
    # TEI is stateless LAN compute; embeddings route through the
    # PEP so the sandbox needs no third egress hole. Both URLs are the
    # site's (`site.py`) and `__main__` builds them. Empty = not on this
    # site: the verb fails closed rather than dial a relative URL.
    tei_url: str = ""
    ha_url: str = ""
    # How long an idle upstream keeps its process. Nothing about authorization
    # changes with it — only whether the next call finds one already warm.
    mcp_idle_ttl_s: float = IDLE_TTL_S
    # Contract 04 §1.6 rule 7: how often a faulted family's grant file is
    # re-read with no call to drive it. Configurable so a host can trade the
    # `stat` cost against how long a family stays blocked.
    fault_sweep_interval_s: float = FAULT_SWEEP_INTERVAL_S
    # `stage7-releases.md` §4.4: where a reload re-reads the upstream roster
    # and the credentials. Set = the pool is reloadable and `SIGHUP` moves
    # it, which is what makes invariant 18 ("adding an MCP server is one
    # action") true. The lifespan also reads it at start, on the path a
    # signal takes, so a file that will not parse starts the PEP with no MCP
    # server rather than stopping it. Unset = the boot-time
    # pool, and a new server needs a restart, which drops every approval
    # blocked in place.
    roster: RosterSource | None = None
    # Contract 04 §7: attendance's delegate door. `delegate_token_file` is what
    # the unit sets and what the live PEP uses: the file is read on each call
    # (CachedTokenFile), because attendance writes it at ITS start and this
    # process can start first. `delegate_token` is the fixed-value form a test
    # builds a door with. Neither is ever an env value or an argv value
    # (invariant 13). Without one of the two AND a socket (or a URL),
    # invoke_agent stays a seam and the manifest does not offer it.
    delegate_token: str = ""
    delegate_token_file: Path | None = None
    # Contract 02 §13.4: attendance's dispatch door, which serves `enqueue` and
    # `job_status`. Its own bearer, not the delegate door's, so one stolen
    # token reaches one family kind. Read on each call for the same reason.
    # Without one of the two AND a socket, both verbs stay seams.
    dispatch_token: str = ""
    dispatch_token_file: Path | None = None
    attendance_socket: Path | None = None
    attendance_url: str = UDS_BASE_URL
    # Contract 04 §8.4: the protected Node-RED hook a gate is pushed to, the
    # bearer the PEP presents to it, and the bearer Node-RED presents back on
    # POST /approval/<gate>. All three are VALUES here, read from the sops
    # secrets by __main__ — never env vars and never on argv (invariant 13).
    # Without all three, a gated action stays a seam: a gate that can open and
    # never resolve is a 15 minute stall on every gated call.
    approval_hook_url: str = ""
    approval_hook_token: str = ""
    approval_callback_token: str = ""
    # `stage7-releases.md` §2.3: root's release spool, `requests/` only. Set =
    # the `release` verb files one file per call; unset leaves the verb a
    # named seam answering 501. The PEP never creates this directory: root
    # owns the spool, and a directory this
    # process made would have this process's owner and mode.
    release_requests_dir: Path | None = None


#: A body over this size is rejected before parsing, counted as it streams in
#: — Content-Length is spoofable, an unauthenticated flood is not.
MAX_REQUEST_BODY_BYTES = 256 * 1024
#: Contract 04 §5 has no row for this: the body is refused before any bearer
#: is read, so there is no family to decide for. RFC 9110 §15.5.14.
BODY_TOO_LARGE_STATUS = 413
BODY_TOO_LARGE_REASON = "body_too_large"

#: Unidentified calls skip the per-family rate window entirely (there is no
#: family), so a small global bucket bounds how many of them the PEP answers
#: at all — independent of, and in addition to, the body cap.
UNAUTH_BUCKET_CAPACITY = 30.0
UNAUTH_BUCKET_REFILL_PER_S = 0.5

#: How often the lifespan re-runs the audit retention sweep; the first
#: sweep runs at startup, this is the repeat.
AUDIT_SWEEP_INTERVAL_S = 24 * 60 * 60

#: The audit name for a body refused before it was parsed. A `$` cannot start
#: a tool name, so it can never collide with one.
OVERSIZED_ACTION = "$oversized"


class CallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=200)
    args: dict[str, object] = Field(default_factory=dict)


class ApprovalBody(BaseModel):
    """Contract 04 §8.4 rule 4's body. Two words, and nothing else is one."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]


#: Answers the approval callback. A gate this process does not hold, whatever
#: the reason, and a PEP with no phone rail at all.
NOT_FOUND_STATUS = 404
NOT_IMPLEMENTED_STATUS = 501

#: A body the reader refused. FastAPI's own status for it (RFC 9110 §15.5.21).
BODY_REFUSED_STATUS = 422


#: A window this many minutes idle is dropped instead of kept forever — a
#: long-running PEP otherwise grows one entry per family generation for as
#: long as it runs.
RATE_WINDOW_IDLE_MINUTES = 2


class _RateWindow:
    """Fixed-window per-family call counter; the count includes denied calls
    so a flood of rejects is bounded too. Idle entries are evicted."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[int, int]] = {}

    def check_and_count(self, key: str, limit: int, now_minute: int) -> bool:
        stale = [
            entry
            for entry, (minute, _) in self._windows.items()
            if now_minute - minute > RATE_WINDOW_IDLE_MINUTES
        ]
        for entry in stale:
            del self._windows[entry]

        minute, count = self._windows.get(key, (now_minute, 0))
        if minute != now_minute:
            minute, count = now_minute, 0
        count += 1
        self._windows[key] = (minute, count)
        return count > limit


class _TokenBucket:
    """A small global bucket, refilled continuously. Used to bound calls that
    resolve to no family, which have no key to run a window on."""

    def __init__(
        self, capacity: float, refill_per_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._capacity = capacity
        self._refill_per_s = refill_per_s
        self._clock = clock
        self._tokens = capacity
        self._last = clock()

    def take(self) -> bool:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._refill_per_s)
        self._last = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


def _args_digest(args: dict[str, object]) -> tuple[int, str]:
    """Size and digest of the args an unidentified request supplied — never
    the content itself.

    `surrogatepass`: the body reader takes a string that is not Unicode
    text, and such a string has no strict UTF-8 form. It counts as the
    bytes it has "as it is", so the request still gets its line.

    CONTRACT-QUESTION: contract 04 §6 describes the log of a family only,
    so it has no rule for the bytes of such a string in this log. The
    reading here: the `surrogatepass` bytes. A change costs a reader that
    compares the digests of two requests."""
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True).encode("utf-8", "surrogatepass")
    return len(raw), hashlib.sha256(raw).hexdigest()


def _unidentified_record(tool: str, reason: str, detail: str | None) -> dict[str, object]:
    """The trusted half of a record for a request that named no family.

    No `instance` and no `agent` key: a line that carries one was written
    by the retired instance path.
    """
    return {"tool": tool, "decision": "deny", "reason": reason, "detail": detail}


class BodySizeLimitMiddleware:
    """Reject request bodies over `max_bytes`, counting bytes as they stream
    in. `Content-Length` is caller-supplied and can lie or be absent; only
    counting what actually arrives closes that gap (an unauthenticated,
    unbounded POST can otherwise fill `/srv/agents/state`).

    The body is buffered here (bounded by `max_bytes`) and replayed to the
    app as one ASGI message rather than raising through `receive()`: FastAPI
    catches any exception it hits while reading the body and turns it into
    its own generic 400, which would swallow this check silently.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, audit: AuditLog) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        body = b""
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += cast("bytes", message.get("body", b""))
            more_body = bool(message.get("more_body", False))
            if len(body) > self._max_bytes:
                await self._reject(scope, send, len(body))
                return

        replayed: Message = {"type": "http.request", "body": body, "more_body": False}

        async def replay_receive() -> Message:
            nonlocal replayed
            message, replayed = replayed, {"type": "http.disconnect"}
            return message

        await self._app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, send: Send, seen: int) -> None:
        detail = f"body exceeds {self._max_bytes} bytes"
        # The bearer was never read, so this record names no family. The size
        # is what a reader needs and the content is what an attacker chose.
        record = _unidentified_record(OVERSIZED_ACTION, BODY_TOO_LARGE_REASON, detail)
        record["args_bytes"] = seen
        self._audit.write(record)
        response = JSONResponse(
            status_code=BODY_TOO_LARGE_STATUS,
            content={"ok": False, "reason": BODY_TOO_LARGE_REASON, "detail": detail},
        )

        async def no_receive() -> Message:
            return {"type": "http.disconnect"}

        await response(scope, no_receive, send)


#: Contract 04's three directories, all under `PepConfig.rework_dir`.
REWORK_GRANTS_SUBDIR = "grants"
REWORK_AUDIT_SUBDIR = "audit"
REWORK_FAULTS_SUBDIR = "faults/pep"


async def _retention_sweep_loop(sweeps: list[Callable[[], None]], interval_s: float) -> None:
    """Repeat each `sweep_retention` every `interval_s`. The lifespan
    runs one sweep at startup before this task is even created. Both logs
    sweep here: contract 04 §6's audit, and the unidentified log."""
    while True:
        await asyncio.sleep(interval_s)
        for sweep in sweeps:
            try:
                sweep()
            except Exception:
                # Every failure, as in `fault_sweep_loop`: one that left
                # this task would end the retention of both logs for good.
                log.exception("audit retention sweep failed")


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _deny_response(reason: FamilyReason, detail: str | None = None) -> JSONResponse:
    """The shape `family_app` answers a denial with (contract 04 §5.1), for
    the two denials this layer makes itself."""
    return JSONResponse(
        status_code=FAMILY_DENY_STATUS[reason],
        content={"ok": False, "reason": reason, "detail": detail},
    )


def build_delegate_door(cfg: PepConfig) -> DelegateDoor | None:
    """Contract 04 §7's client, or `None` when this PEP has no door.

    Both halves are required: a credential proves identity to `attendance` and a
    socket (or a URL) says where it listens. Half a configuration leaves
    `invoke_agent` a seam, which is the fail-closed answer — the decision core
    then denies it with `not_implemented` and the manifest never offers it.

    A token FILE counts as configured even when it is not readable yet. That
    is the point: the PEP can start before `attendance` writes it, and the door
    reads it on the call instead. An empty file at call time fails that one
    call and nothing else.
    """
    source = CachedTokenFile(cfg.delegate_token_file) if cfg.delegate_token_file else None
    if source is None and not cfg.delegate_token:
        return None
    if cfg.attendance_socket is None and cfg.attendance_url == UDS_BASE_URL:
        return None

    return HttpDelegateDoor(
        DoorConfig(
            token=cfg.delegate_token,
            socket_path=cfg.attendance_socket,
            base_url=cfg.attendance_url,
            token_source=source,
        )
    )


def build_dispatch_door(cfg: PepConfig) -> DispatchDoor | None:
    """Contract 02 §13.4's client, or `None` when this PEP has no door.

    Both halves are required, exactly as for the delegate door: a credential
    proves identity to `attendance` and a socket (or a URL) says where it
    listens. Half a configuration leaves `enqueue` and `job_status` seams,
    which is the fail-closed answer.
    """
    source = CachedTokenFile(cfg.dispatch_token_file) if cfg.dispatch_token_file else None
    if source is None and not cfg.dispatch_token:
        return None
    if cfg.attendance_socket is None and cfg.attendance_url == UDS_BASE_URL:
        return None

    return HttpDispatchDoor(
        DispatchConfig(
            token=cfg.dispatch_token,
            socket_path=cfg.attendance_socket,
            base_url=cfg.attendance_url,
            token_source=source,
        )
    )


def build_release_door(cfg: PepConfig) -> ReleaseDoor | None:
    """`stage7-releases.md` §2.3's writer, or `None` when this PEP has no
    spool.

    One directory, and it must already exist: root creates it with its own
    owner and mode, and a PEP that made it
    would make it with the PEP's. Unset leaves `release` a named seam.
    """
    if cfg.release_requests_dir is None:
        return None

    return SpoolReleaseDoor(str(cfg.release_requests_dir))


def build_gatekeeper(cfg: PepConfig) -> Gatekeeper | None:
    """Contract 04 §8's phone rail, or `None` when this PEP has none.

    Three parts, all required. The hook URL and its bearer send the push
    (§8.4 rule 1); the callback bearer is what lets the tap come back (rule
    4). Two out of three is worse than none: the gate would open, the human
    would tap, and the call would still stall for 15 minutes. Without all
    three the decision core denies a gated action with `not_implemented` and
    the manifest never offers it.
    """
    if not cfg.approval_hook_url or not cfg.approval_hook_token:
        return None
    if not cfg.approval_callback_token:
        return None

    return Gatekeeper(HttpApprovalNotifier(cfg.approval_hook_url, cfg.approval_hook_token))


def _bearer_matches(expected: str, supplied: str) -> bool:
    """A constant-time bearer check for an endpoint with one fixed token.

    Bytes, not str: `compare_digest` raises a TypeError on a non-ASCII str,
    and a header decodes as latin-1, so a stray high byte must land on this
    comparison rather than crash past it.
    """
    if not expected or not supplied:
        return False

    return secretslib.compare_digest(
        supplied.encode("utf-8", "surrogateescape"), expected.encode("utf-8")
    )


def _build_family_gate(
    cfg: PepConfig,
    *,
    rate: Callable[[str, int], bool],
    pool: Callable[[], UpstreamPool | None],
    client: Callable[[], httpx.AsyncClient | None],
    serving: Callable[[], Serving],
    delegate_door: DelegateDoor | None,
    gatekeeper: Gatekeeper | None,
    dispatch_door: DispatchDoor | None,
    release_door: ReleaseDoor | None,
) -> FamilyGate:
    """The one path (contract 04). Every bearer this process answers resolves
    here or nowhere."""
    faults = FaultWriter(cfg.rework_dir / REWORK_FAULTS_SUBDIR)
    return FamilyGate(
        FamilyDeps(
            store=FamilyStore(cfg.rework_dir / REWORK_GRANTS_SUBDIR, faults),
            audit=FamilyAudit(cfg.rework_dir / REWORK_AUDIT_SUBDIR),
            rate=rate,
            pool=pool,
            client=client,
            serving=serving,
            tei_url=cfg.tei_url,
            ha_url=cfg.ha_url,
            secrets=cfg.secrets,
            delegate_door=delegate_door,
            gatekeeper=gatekeeper,
            dispatch_door=dispatch_door,
            release_door=release_door,
        )
    )


def _family_response(reply: Reply) -> JSONResponse:
    return JSONResponse(status_code=reply.status, content=reply.payload)


def _roster_block(trigger: ReloadTrigger | None) -> dict[str, str | None]:
    """`/healthz`'s `roster` (contract 04 §10). `off` when this PEP has no
    roster source, and otherwise what the last read found, and since when:

        {"state": "unreadable", "since": "2026-09-22T10:00:00Z"}
    """
    health = trigger.health if trigger is not None else RosterHealth(RosterState.OFF)
    since = rfc3339_s(health.since) if health.since is not None else None

    return {"state": health.state.value, "since": since}


def _upstreams_block(pool: UpstreamPool | None) -> dict[str, int] | None:
    """`/healthz`'s `upstreams` (contract 04 §10): how many
    upstreams serve, and how many the roster declares that the pool
    refused. Null when the pool does not reload, which keeps no refusals:

        {"serving": 9, "refused": 2}
    """
    if not isinstance(pool, ReloadablePool):
        return None

    return {"serving": len(pool.live_specs), "refused": len(pool.refusals)}


def create_app(
    cfg: PepConfig,
    *,
    pool: UpstreamPool | None = None,
    http_client: httpx.AsyncClient | None = None,
    delegate_door: DelegateDoor | None = None,
    gatekeeper: Gatekeeper | None = None,
    dispatch_door: DispatchDoor | None = None,
    release_door: ReleaseDoor | None = None,
) -> FastAPI:
    audit = AuditLog(cfg.audit_dir)
    rate = _RateWindow()
    unauth_bucket = _TokenBucket(UNAUTH_BUCKET_CAPACITY, UNAUTH_BUCKET_REFILL_PER_S)
    active_pool: UpstreamPool | None = pool
    active_client: httpx.AsyncClient | None = http_client
    active_trigger: ReloadTrigger | None = None
    # What serves, as the family path reads it. The upstream
    # fences (docs/pep-decisions.md row 3a) ride with the names, and the
    # decision core takes both as an input and stays pure. A reloadable pool
    # answers for itself, so a reload moves the decision with the calls. Any
    # other pool serves the rows this process started with, frozen here: a
    # PEP with no roster source.
    boot = serving_of(cfg.upstreams)

    def serving() -> Serving:
        if isinstance(active_pool, ReloadablePool):
            return active_pool.serving

        return boot

    def family_over_rate(key: str, limit: int) -> bool:
        """One process, one bound (contract 04 §5)."""
        return rate.check_and_count(key, limit, int(datetime.now(UTC).timestamp() // 60))

    door = delegate_door if delegate_door is not None else build_delegate_door(cfg)
    keeper = gatekeeper if gatekeeper is not None else build_gatekeeper(cfg)
    dispatcher = dispatch_door if dispatch_door is not None else build_dispatch_door(cfg)
    releaser = release_door if release_door is not None else build_release_door(cfg)
    family = _build_family_gate(
        cfg,
        rate=family_over_rate,
        pool=lambda: active_pool,
        client=lambda: active_client,
        serving=serving,
        delegate_door=door,
        gatekeeper=keeper,
        dispatch_door=dispatcher,
        release_door=releaser,
    )
    sweeps: list[Callable[[], None]] = [audit.sweep_retention, family.sweep_retention]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        nonlocal active_pool, active_client, active_trigger
        owned_pool = active_pool is None
        drop_trigger: Callable[[], None] = _no_remover
        if active_pool is None:
            # §4.4: with a roster source the pool can be reloaded, so a new
            # MCP server appears with no restart and no blocked approval is
            # lost. Without one the PEP keeps the boot-time pool, and a new
            # server needs a restart.
            if cfg.roster is None:
                stdio = StdioUpstreamPool(cfg.upstreams, cfg.secrets, cfg.mcp_idle_ttl_s)
                await stdio.start()
                active_pool = stdio
            else:
                reloadable = ReloadablePool(cfg.upstreams, cfg.secrets, cfg.mcp_idle_ttl_s)
                await reloadable.start()
                active_pool = reloadable
                trigger, drop_trigger = install_reload(reloadable, cfg.roster)
                active_trigger = trigger
                _app.state.reload_trigger = trigger  # tests drive it directly
                # The roster is read here, on the path a `SIGHUP` takes, and
                # not by `__main__`. A file that will not parse is
                # then what it is at a reload: one log line, no MCP server
                # serving, and the next signal tries again. It never stops
                # the one enforcement point, verbs and all.
                await trigger.start()
        owns_client = active_client is None
        if active_client is None:
            active_client = httpx.AsyncClient()

        # 180 d retention, enforced here — nothing else sweeps an audit dir.
        # Once at startup, then repeated on the interval.
        for sweep in sweeps:
            sweep()
        sweep_task = asyncio.create_task(_retention_sweep_loop(sweeps, AUDIT_SWEEP_INTERVAL_S))
        # Contract 04 §1.6 rule 7. Its own task: the retention sweep runs
        # daily and a fault must clear in seconds, so one loop cannot serve
        # both.
        tasks = [
            sweep_task,
            asyncio.create_task(fault_sweep_loop(family.sweep_faults, cfg.fault_sweep_interval_s)),
        ]
        try:
            yield
        finally:
            drop_trigger()
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if owns_client:
                await active_client.aclose()
                active_client = None
            if isinstance(door, HttpDelegateDoor):
                await door.aclose()
            if keeper is not None:
                await keeper.aclose()
            if owned_pool and isinstance(active_pool, StdioUpstreamPool | ReloadablePool):
                await active_pool.stop()
                active_pool = None

    # No interactive docs: this is a LAN-exposed enforcement point,
    # not an API to browse — Swagger/ReDoc/the schema JSON add nothing here.
    app = FastAPI(
        title="chaperone", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.unauth_bucket = unauth_bucket  # tests drain it directly
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES, audit=audit)

    def _refuse_unidentified(tool: str, args: dict[str, object]) -> JSONResponse:
        """A bearer that resolves to no family (contract 04 §5 row 1), or one
        request too many from such a caller.

        Both are recorded in the unidentified log, with the size and digest of
        the arguments rather than their content: an unauthenticated caller's
        bytes never land verbatim in a file this host keeps for 180 days.
        """
        reason: FamilyReason = "unknown_token" if unauth_bucket.take() else "rate_limited"
        record = _unidentified_record(tool, reason, None)
        args_bytes, args_sha256 = _args_digest(args)
        record["args_bytes"] = args_bytes
        record["args_sha256"] = args_sha256
        audit.write(record)

        return _deny_response(reason)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Contract 04 §10. `ok` is what every reader decides on. `roster` is
        additive, and it is here because `chaperone-verify` reads this route and
        nothing else: a roster that will not parse does not stop the
        process, so without it nothing outside the journal would say so.
        `upstreams` is additive for the same reader: a refused upstream
        leaves this route answering 200."""
        return {
            "ok": True,
            "roster": _roster_block(active_trigger),
            "upstreams": _upstreams_block(active_pool),
        }

    @app.post("/approval/{gate}")
    async def approval(  # pyright: ignore[reportUnusedFunction]
        gate: str, request: Request, body: ApprovalBody
    ) -> JSONResponse:
        """Contract 04 §8.4 rule 4: the tap, as Node-RED delivers it.

        Its own bearer, which is not a family token and grants nothing else
        (§9 open point 2). The bearer is checked first, so an unauthenticated
        caller learns nothing about which gates are open.

        The gate id names the call and nothing else, so this endpoint reads
        no family and no arguments: `resolve` finds the waiting call, which
        then writes the resolving audit record itself (§6.4).
        """
        if not _bearer_matches(cfg.approval_callback_token, _bearer(request)):
            log.warning("approval callback refused: unknown bearer")
            return _deny_response("unknown_token")

        if keeper is None:
            return JSONResponse(
                status_code=NOT_IMPLEMENTED_STATUS,
                content={"ok": False, "reason": "not_implemented"},
            )

        # A gate id is 16 hex characters, so a path that is not one names no
        # gate this process could hold. Shape first, then the lookup.
        if GATE_ID_RE.match(gate) is None or not keeper.resolve(gate, Verdict(body.decision)):
            return JSONResponse(
                status_code=NOT_FOUND_STATUS, content={"ok": False, "reason": "unknown_gate"}
            )

        return JSONResponse({"ok": True, "gate": gate, "decision": body.decision})

    @app.get("/manifest")
    async def manifest(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        grants = family.lookup(_bearer(request))
        if grants is None:
            return _refuse_unidentified(MANIFEST_ACTION, {})

        reply = family.manifest(grants, request.headers)
        if reply.status == HTTP_NOT_MODIFIED:
            # RFC 9110 §15.4.5: a 304 carries no body at all, and a JSON
            # `{}` with a content length is a body.
            return Response(status_code=HTTP_NOT_MODIFIED)

        return _family_response(reply)

    @app.post("/call")
    async def call(request: Request, body: CallBody) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        grants = family.lookup(_bearer(request))
        if grants is None:
            return _refuse_unidentified(body.tool, body.args)

        return _family_response(await family.call(grants, body.tool, body.args, request.headers))

    @app.exception_handler(AuditError)
    async def audit_error(_request: Request, exc: AuditError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        log.error("%s", exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "reason": "internal_error", "detail": "audit unavailable"},
        )

    @app.exception_handler(RequestValidationError)
    async def refused_body(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """A body the reader refused. FastAPI's own answer echoes what it
        refused, and an echo that is no JSON text raises while the answer is
        built. The answer then keeps the status and each error, without the
        echo: the caller's key is in `loc` and its value in `input`.

        CONTRACT-QUESTION: contract 04 §5 has no row for a body that the
        reader refuses. The reading here: the status that each other
        refused body gets, and no text of the caller. A change costs a
        caller that reads `loc`."""
        try:
            return await request_validation_exception_handler(request, exc)
        except (ValueError, RecursionError):
            errors = [
                {"type": error["type"], "msg": error["msg"]}
                for error in cast("list[dict[str, object]]", exc.errors())
            ]
            return JSONResponse(status_code=BODY_REFUSED_STATUS, content={"detail": errors})

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 04 §5 row 12 gives `internal_error` for a failure that
        no layer below handled. Without this handler, such a failure
        answers 500 with no `reason`. The log line holds the type and not
        the text: the text can quote the request. Starlette raises the
        failure again after this answer, so the server logs its
        traceback."""
        log.error("a route raised %s; answering internal_error", type(exc).__name__)
        return _deny_response("internal_error")

    return app
