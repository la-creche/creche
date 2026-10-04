"""The PEP's client for `attendance`'s delegate door (contract 04 §7).

```
  sandbox --POST /call {tool: invoke_agent} --> PEP --> POST /delegate
                                                         Unix socket,
                                                         Bearer door-delegate
                                               <-- {status, session_id,
                                                    content, error}
  <-- {untrusted: true, source: "family:<target>", content: ...}
```

Four rules this module keeps, each with its reason:

1. **The door's token is read from a file, never from argv or a URL**
   (invariant 13), and it is read at CALL time, not at start
   (`CachedTokenFile`). `read_door_token` refuses a file shorter than 32
   bytes, the same floor `attendance` itself applies (contract 02 §3 rule 7).
2. **The whole call is bounded at 120 seconds** (§7.5). `httpx`'s own
   `timeout=` is per phase, so a door trickling bytes could stay under it
   forever; `asyncio.wait_for` caps the wall clock. Cancelling the request
   closes the connection, which is the only stop signal §7.3 leaves the PEP:
   it may call this one path and no other.
3. **The answer is untrusted input from another process** (invariants 12 and
   14). Its shape and size are checked before use, and `wrap_untrusted` marks
   it as data before it reaches a model.
4. **Anything that is not a clean answer is a failure.** An unknown status, a
   body that is not JSON, a refusal, a dead socket: all `FAILED`. Fail closed
   is `chaperone/AGENTS.md`'s first non-negotiable.

This module holds no policy. `family_app` decides, then calls.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Protocol, cast

import httpx

log = logging.getLogger("chaperone.delegate")

#: Contract 04 §7.3: `attendance` serves this one path for the PEP.
DELEGATE_PATH: Final = "/delegate"

#: A Unix socket carries no host, so httpx needs a base URL to build a
#: request line from. The name is never resolved. `door-owui` uses the same
#: constant against the same service.
UDS_BASE_URL: Final = "http://sessiond"

#: Contract 04 §7.5: the PEP gives up at 120 seconds.
DELEGATE_TIMEOUT_S: Final = 120.0

#: Contract 02 §3 rule 7's floor, applied on the client side too: a token file
#: that is empty or short is a misconfiguration, and the one time that shipped
#: it turned a LAN admin surface into an open one.
MIN_DOOR_TOKEN_BYTES: Final = 32

#: The answer is another process's output (invariant 12), so it is bounded
#: before it is used. 64 KiB is `invoke_agent.message`'s own cap in §4.1, so
#: an answer may be as long as the question.
MAX_CONTENT_CHARS: Final = 64 * 1024

#: Contract 04 §7.6's wrapper keys.
UNTRUSTED_SOURCE_PREFIX: Final = "family:"

HTTP_OK: Final = 200


class DoorTokenError(Exception):
    """The delegate door's token cannot be loaded, so no delegate call can
    be made. The message names the path and never the value."""


class DelegateStatus(Enum):
    """Contract 04 §7.3's `status` column."""

    OK = "ok"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(frozen=True)
class DelegateRequest:
    """Contract 04 §7.3's body. `caller_family` is trusted: the PEP read it
    from the bearer token. `claimed_session_id` is advisory and travels for
    the audit and the noticeboard only."""

    caller_family: str
    target_family: str
    delegation_id: str
    claimed_session_id: str | None
    message: str


@dataclass(frozen=True)
class DelegateReply:
    """What came back, already bounded and checked."""

    status: DelegateStatus
    session_id: str | None = None
    content: str | None = None
    error: str | None = None


class DelegateDoor(Protocol):
    """What `family_app` needs from this layer, and nothing more."""

    async def call(self, request: DelegateRequest) -> DelegateReply: ...


class DoorToken(Protocol):
    """One bearer, resolved when it is needed.

    `value` raises `DoorTokenError` rather than returning an empty string: an
    empty bearer is a credential, and one that reached a live socket once
    turned a LAN admin surface into an open one.
    """

    def value(self) -> str: ...


@dataclass(frozen=True)
class DoorConfig:
    """Where the door is and how to prove identity to it.

    `token_source` wins over `token` when both are set. The fixed `token` is
    what a test builds a door with; the source is what the live PEP uses.
    """

    token: str
    socket_path: Path | None = None
    base_url: str = UDS_BASE_URL
    timeout_s: float = DELEGATE_TIMEOUT_S
    token_source: DoorToken | None = None


def read_door_token(path: Path) -> str:
    """The `door-delegate` bearer, from a file the PEP can read.

    Never an environment value and never on argv: the unit file is in git and
    a process's command line is world-readable (invariant 13).
    """
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        # The path is not a secret. The content is, and it is not in the text.
        raise DoorTokenError(f"delegate door token: cannot read {path} ({exc.strerror})") from exc

    if len(value.encode("utf-8")) < MIN_DOOR_TOKEN_BYTES:
        raise DoorTokenError(
            f"delegate door token: {path} holds fewer than {MIN_DOOR_TOKEN_BYTES} bytes"
        )

    return value


class CachedTokenFile:
    """`read_door_token` moved off the start path and onto the call path.

    WHY. `attendance` writes `tokens/door-delegate.token` when IT starts. The
    PEP is a system unit and can start first, so a value read once at boot is
    empty for the life of the process, and every rotation needs
    `sudo systemctl restart creche-chaperone`. Reading at call time removes that
    root step.

    WHAT IT COSTS. One `stat` per delegate call. The file is re-read only
    when `(st_mtime_ns, st_size, st_ino)` moved, which covers a rewrite in
    place and the `os.replace` every rotation in this repo uses. A delegate
    call already spends up to 120 seconds (§7.5), so one stat is free.

    The checks are `read_door_token`'s own, unchanged: the 32 byte floor and
    a read that fails closed. Mode is not checked here: §7.3 names no mode
    check.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._facts: tuple[int, int, int] | None = None
        self._value = ""

    def value(self) -> str:
        """The bearer the file holds now. Raises `DoorTokenError` when it
        cannot be read, which is the fail-closed answer."""
        facts = self._facts_now()

        # A stat that failed is never served from the cache: the file may
        # have been removed on purpose, and a revoked token must stop working.
        if facts is not None and facts == self._facts:
            return self._value

        value = read_door_token(self._path)
        self._facts = facts
        self._value = value
        return value

    def _facts_now(self) -> tuple[int, int, int] | None:
        try:
            found = self._path.stat()
        except OSError:
            return None

        return (found.st_mtime_ns, found.st_size, found.st_ino)


def wrap_untrusted(family: str, content: str) -> dict[str, object]:
    """Contract 04 §7.6. A delegated answer is data, never instruction.

    The PEP does not read it, summarize it, or let it change any decision
    (invariant 14). The wrapper is the only shape the caller ever sees.
    """
    return {
        "untrusted": True,
        "source": f"{UNTRUSTED_SOURCE_PREFIX}{family}",
        "content": content,
    }


def _text(body: dict[str, object], key: str, cap: int) -> str | None:
    """One string field of an untrusted answer, bounded. A value of another
    type reads as absent rather than as its `str()`."""
    value = body.get(key)
    if not isinstance(value, str):
        return None
    return value[:cap]


def _read_reply(body: object) -> DelegateReply:
    """Contract 04 §7.3's answer, checked before use (invariant 12)."""
    if not isinstance(body, dict):
        return DelegateReply(DelegateStatus.FAILED, error="the delegate door sent no object")

    fields = cast("dict[str, object]", body)
    raw = fields.get("status")
    try:
        status = DelegateStatus(raw)
    except ValueError:
        return DelegateReply(DelegateStatus.FAILED, error="the delegate door sent no known status")

    if status is not DelegateStatus.OK:
        return DelegateReply(
            status,
            session_id=_text(fields, "session_id", MAX_CONTENT_CHARS),
            error=_text(fields, "error", MAX_CONTENT_CHARS),
        )

    return DelegateReply(
        DelegateStatus.OK,
        session_id=_text(fields, "session_id", MAX_CONTENT_CHARS),
        content=_text(fields, "content", MAX_CONTENT_CHARS),
    )


class HttpDelegateDoor:
    """One door, one client, built on first use.

    The client is lazy because `app.py` builds this object before the event
    loop exists, and an `httpx.AsyncClient` binds its loop when it is first
    used rather than when it is constructed.
    """

    def __init__(
        self, config: DoorConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def call(self, request: DelegateRequest) -> DelegateReply:
        """Contract 04 §7.3, bounded by §7.5's 120 seconds."""
        try:
            return await asyncio.wait_for(self._post(request), timeout=self._config.timeout_s)
        except TimeoutError:
            # Cancelling closes the connection, and that is what tells
            # `attendance` to stop the turn: §7.3 gives the PEP this one path.
            log.warning(
                "delegate to %s passed %.0fs; the request was cancelled",
                request.target_family,
                self._config.timeout_s,
            )
            return DelegateReply(
                DelegateStatus.TIMEOUT,
                error=f"{request.target_family} did not answer within "
                f"{self._config.timeout_s:.0f}s",
            )

    async def aclose(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client

        transport = self._transport
        if transport is None and self._config.socket_path is not None:
            transport = httpx.AsyncHTTPTransport(uds=str(self._config.socket_path))

        self._client = httpx.AsyncClient(base_url=self._config.base_url, transport=transport)
        return self._client

    def _bearer(self) -> str:
        """The token to present on THIS call. Raises `DoorTokenError`."""
        source = self._config.token_source
        if source is None:
            return self._config.token

        return source.value()

    async def _post(self, request: DelegateRequest) -> DelegateReply:
        # Before the connection, not after: a PEP that cannot prove its
        # identity must not open a socket to attendance at all.
        try:
            bearer = self._bearer()
        except DoorTokenError as exc:
            # The path goes to the journal and never to the sandbox.
            log.error("%s; the delegate call was refused", exc)
            return DelegateReply(
                DelegateStatus.FAILED, error="the delegate door is not configured yet"
            )

        body = {
            "caller_family": request.caller_family,
            "target_family": request.target_family,
            "delegation_id": request.delegation_id,
            "claimed_session_id": request.claimed_session_id,
            "message": request.message,
        }
        try:
            reply = await self._open().post(
                DELEGATE_PATH,
                json=body,
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=self._config.timeout_s,
            )
        except httpx.HTTPError as exc:
            return DelegateReply(DelegateStatus.FAILED, error=f"delegate door unreachable: {exc}")

        if reply.status_code != HTTP_OK:
            return DelegateReply(
                DelegateStatus.FAILED,
                error=f"the delegate door answered HTTP {reply.status_code}",
            )

        try:
            return _read_reply(reply.json())
        except (ValueError, RecursionError):
            return DelegateReply(DelegateStatus.FAILED, error="the delegate door sent no JSON")
