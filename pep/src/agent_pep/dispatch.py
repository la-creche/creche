"""The PEP's client for `sessiond`'s dispatch door (contract 02 §13.4).

```
  sandbox --POST /call {tool: enqueue} --> PEP --> POST /dispatch
                                                    Unix socket,
                                                    Bearer door-dispatch
                                          <-- {session_id, status, created}
  sandbox --POST /call {tool: job_status} -> PEP --> POST /dispatch/jobs
                                          <-- {jobs: [...]}
```

Four rules, each with its reason:

1. **This door's bearer is its own**, not `door-delegate`'s. Contract 02 §3.1
   keeps one prefix and one family kind per principal, so a stolen bearer
   reaches one kind. The file is read at CALL time for the same reason the
   delegate token is: `sessiond` writes it when IT starts, and the PEP is a
   system unit that can start first.
2. **A refusal keeps its meaning.** `sessiond` answers with contract 02 §14's
   codes, and this module maps each one to a contract 04 §5 reason. A target
   that declares no `enqueue` trigger is `tool_not_granted` and not
   `upstream_failed`, because nothing ran and the answer will not change on a
   retry.
3. **Neither call waits for a job** (contract 02 §13.4). `enqueue` answers as
   soon as the session exists, so the timeout here is a socket timeout and
   not a job's.
4. **The answer is another process's output** (invariants 12 and 14). Its
   shape and size are checked before it reaches a model.

This module holds no policy. `family_app` decides, then calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

import httpx

from .delegate import UDS_BASE_URL, DoorToken, DoorTokenError
from .family_decisions import FamilyReason

log = logging.getLogger("agent_pep.dispatch")

#: Contract 02 §13.4's two paths.
DISPATCH_PATH: Final = "/dispatch"
JOBS_PATH: Final = "/dispatch/jobs"

#: Neither call waits for a job, so this bounds a socket and nothing else.
#: `sessiond` creates a session and starts one turn inside it.
DISPATCH_TIMEOUT_S: Final = 30.0

#: Contract 02 §13.4.2's own `limit` ceiling, applied again on the way back:
#: an answer from another process is bounded before a model sees it.
MAX_JOBS: Final = 200

HTTP_OK: Final = 200

#: Contract 02 §14's codes, mapped to contract 04 §5's reasons. Every code
#: this table does not name is `upstream_failed`: the PEP asked and did not
#: get an answer it understands, which is not a policy denial.
_BY_ERROR_CODE: Final[dict[str, FamilyReason]] = {
    "dispatch_not_declared": "tool_not_granted",
    "forbidden": "tool_not_granted",
    "family_unknown": "tool_not_granted",
    "family_invalid": "tool_not_granted",
    "bad_request": "arg_validation",
    "idempotency_mismatch": "arg_validation",
    "payload_too_large": "arg_validation",
    "queue_full": "rate_limited",
}

#: What a caller is told when `sessiond` refuses without a code this table
#: knows. The detail never names a path: the caller is a sandbox.
UNREACHABLE_DETAIL: Final = "the dispatch door did not answer"


class DispatchRefused(Exception):
    """One dispatch call did not succeed, with the reason a caller is told.

    It carries a contract 04 §5 reason rather than an HTTP status, so
    `family_app` writes one audit record and answers one body without
    re-deciding anything.
    """

    def __init__(self, reason: FamilyReason, detail: str) -> None:
        super().__init__(detail)
        # Annotated, not inferred: an attribute assignment widens a Literal
        # union to `str`, and the audit's reason column is not a free string.
        self.reason: FamilyReason = reason
        self.detail = detail


@dataclass(frozen=True)
class DispatchRequest:
    """Contract 02 §13.4.1's body. `caller_family` and `chain` are trusted:
    the PEP read one from the token and built the other from its own mints
    (contract 04 §6.3)."""

    caller_family: str
    target_family: str
    delegation_id: str
    chain: tuple[str, ...]
    claimed_session_id: str | None
    message: str
    idempotency_key: str | None = None


@dataclass(frozen=True)
class DispatchReply:
    """What `POST /dispatch` answered, already checked."""

    session: str
    status: str
    created: bool


@dataclass(frozen=True)
class JobQuery:
    """Contract 02 §13.4.2's body. The scope is the caller family, and the
    verb gives a model no way to name another one."""

    caller_family: str
    session: str | None = None
    since: str | None = None
    limit: int | None = None


@dataclass(frozen=True)
class JobsReply:
    """The `jobs` list, bounded. Each row is served through as it came."""

    jobs: list[dict[str, object]]


class DispatchDoor(Protocol):
    """What `family_app` needs from this layer, and nothing more."""

    async def enqueue(self, request: DispatchRequest) -> DispatchReply: ...

    async def jobs(self, query: JobQuery) -> JobsReply: ...


@dataclass(frozen=True)
class DispatchConfig:
    """Where the door is and how to prove identity to it.

    `token_source` wins over `token` when both are set. The fixed `token` is
    what a test builds a door with; the source is what the live PEP uses.
    """

    token: str
    socket_path: Path | None = None
    base_url: str = UDS_BASE_URL
    timeout_s: float = DISPATCH_TIMEOUT_S
    token_source: DoorToken | None = None


class HttpDispatchDoor:
    """One door, one client, built on first use.

    The client is lazy because `app.py` builds this object before the event
    loop exists, and an `httpx.AsyncClient` binds its loop on first use.
    """

    def __init__(
        self, config: DispatchConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def enqueue(self, request: DispatchRequest) -> DispatchReply:
        """Contract 02 §13.4.1. Start one job and answer with its id."""
        body = self._enqueue_body(request)
        answer = await self._post(DISPATCH_PATH, body)
        session = answer.get("session_id")

        if not isinstance(session, str) or not session:
            raise DispatchRefused("upstream_failed", "the dispatch door named no session")

        status = answer.get("status")

        return DispatchReply(
            session=session,
            status=status if isinstance(status, str) else "",
            created=answer.get("created") is not False,
        )

    async def jobs(self, query: JobQuery) -> JobsReply:
        """Contract 02 §13.4.2. The jobs this family dispatched."""
        answer = await self._post(JOBS_PATH, self._jobs_body(query))
        listed = answer.get("jobs")

        if not isinstance(listed, list):
            raise DispatchRefused("upstream_failed", "the dispatch door sent no job list")

        rows = cast("list[object]", listed)[:MAX_JOBS]

        return JobsReply(jobs=[cast("dict[str, object]", one) for one in rows if _is_row(one)])

    async def aclose(self) -> None:
        if self._client is None:
            return

        await self._client.aclose()
        self._client = None

    @staticmethod
    def _enqueue_body(request: DispatchRequest) -> dict[str, object]:
        body: dict[str, object] = {
            "caller_family": request.caller_family,
            "target_family": request.target_family,
            "delegation_id": request.delegation_id,
            "chain": list(request.chain),
            "claimed_session_id": request.claimed_session_id,
            "message": request.message,
        }

        # An absent key and a null key are different requests to a validator,
        # so an unset option is simply not sent.
        if request.idempotency_key is not None:
            body["idempotency_key"] = request.idempotency_key

        return body

    @staticmethod
    def _jobs_body(query: JobQuery) -> dict[str, object]:
        body: dict[str, object] = {"caller_family": query.caller_family}

        if query.session is not None:
            body["session"] = query.session

        if query.since is not None:
            body["since"] = query.since

        if query.limit is not None:
            body["limit"] = query.limit

        return body

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

    async def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        # Before the connection, not after: a PEP that cannot prove its
        # identity must not open a socket to `sessiond` at all.
        try:
            bearer = self._bearer()
        except DoorTokenError as exc:
            # The path goes to the journal and never to the sandbox.
            log.error("%s; the dispatch call was refused", exc)
            raise DispatchRefused(
                "upstream_failed", "the dispatch door is not configured yet"
            ) from exc

        try:
            reply = await self._open().post(
                path,
                json=body,
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=self._config.timeout_s,
            )
        except httpx.HTTPError as exc:
            raise DispatchRefused("upstream_failed", UNREACHABLE_DETAIL) from exc

        if reply.status_code != HTTP_OK:
            raise _refusal(reply)

        try:
            parsed: object = reply.json()
        except ValueError as exc:
            raise DispatchRefused("upstream_failed", "the dispatch door sent no JSON") from exc

        if not isinstance(parsed, dict):
            raise DispatchRefused("upstream_failed", "the dispatch door sent no object")

        return cast("dict[str, object]", parsed)


def _refusal(reply: httpx.Response) -> DispatchRefused:
    """Contract 02 §14's body, read as a contract 04 §5 reason.

    An unknown code is `upstream_failed` rather than a guess: a denial a
    caller can act on must be one this table names.
    """
    code, message = _error_of(reply)
    reason = _BY_ERROR_CODE.get(code)

    if reason is None:
        return DispatchRefused(
            "upstream_failed", f"the dispatch door answered HTTP {reply.status_code}"
        )

    return DispatchRefused(reason, message or code)


def _error_of(reply: httpx.Response) -> tuple[str, str]:
    """The `code` and `message` of §14's one body shape, or empty strings."""
    try:
        parsed: object = reply.json()
    except ValueError:
        return "", ""

    if not isinstance(parsed, dict):
        return "", ""

    error = cast("dict[str, object]", parsed).get("error")

    if not isinstance(error, dict):
        return "", ""

    fields = cast("dict[str, object]", error)
    code = fields.get("code")
    message = fields.get("message")

    return (code if isinstance(code, str) else ""), (message if isinstance(message, str) else "")


def _is_row(value: object) -> bool:
    return isinstance(value, dict)
