"""The client of `attendance`'s session API (contract 02), as `door-trigger`
uses it: create-or-find a session (§5.1), then run one turn with
`wait: "accepted"` (§5.4) — the one response shape contract 02 names for
the trigger door. This door never streams and never waits for settled: a
firing hands the job to `attendance` and returns, and a refusal is never
retried in a loop by this door.

Synchronous on purpose. `fire` is a one-shot CLI process with no event
loop to share, and `serve`'s route handler offloads this blocking client
to a thread (`webhooks.py`), so one client implementation serves both.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .config import AttendanceTarget
from .errors import AttendanceError
from .untrusted import as_object, field_text, is_list

_LOG = logging.getLogger(__name__)

_OK = 200
_CREATED = 201
_ACCEPTED = 202

CONNECT_TIMEOUT_S = 5.0
# Creating a session and queueing a turn are both fast, host-local calls:
# neither waits on a sandbox or a model reply (contract 02 §5.4's
# `accepted` row answers "at once").
READ_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class TurnRequest:
    """One `accepted`-mode turn, addressed to a fresh session."""

    family: str
    session: str
    prompt: str
    idempotency_key: str
    labels: dict[str, str] = field(default_factory=dict[str, str])

    def body(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "idempotency_key": self.idempotency_key,
            "wait": "accepted",
            "labels": self.labels,
        }


@dataclass(frozen=True)
class AcceptedTurn:
    """Contract 02 §5.4's `accepted` response: `{turn, state, journal_seq}`."""

    turn: str
    state: str
    journal_seq: int


class AttendanceClient(Protocol):
    """What a firing needs from `attendance` (`fire.py`), and nothing more."""

    def ensure_session(self, family: str, session: str) -> None:
        """Create or find the session. Contract 02 §5.1."""
        ...

    def accepted_turn(self, request: TurnRequest) -> AcceptedTurn:
        """Run a turn and return at once. Contract 02 §5.4."""
        ...


class HttpAttendance:
    """`AttendanceClient` over HTTP, on a Unix socket or the LAN address."""

    def __init__(self, target: AttendanceTarget, client: httpx.Client | None = None) -> None:
        self._client = client if client is not None else _build_client(target)
        self._auth = {"Authorization": f"Bearer {target.token}"}

    def close(self) -> None:
        self._client.close()

    def ensure_session(self, family: str, session: str) -> None:
        response = self._client.post(
            "/v1/sessions",
            json={"family": family, "session": session, "labels": {"door": "trigger"}},
            headers=self._auth,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
        )
        if response.status_code in (_OK, _CREATED):
            return

        raise _error_of(response.status_code, response.text)

    def accepted_turn(self, request: TurnRequest) -> AcceptedTurn:
        response = self._client.post(
            f"/v1/sessions/{request.family}/{request.session}/turns",
            json=request.body(),
            headers=self._auth,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
        )
        if response.status_code != _ACCEPTED:
            raise _error_of(response.status_code, response.text)

        return _accepted_of(response.text)

    def any_live(self, family: str) -> bool | None:
        """Whether any session of `family` is listed (contract 02 §5.2). An
        autonomous session is deleted when its job ends, so a listed one is
        a job still queued or running. None when `attendance` would not say."""
        response = self._client.get(
            "/v1/sessions",
            params={"family": family, "limit": 1},
            headers=self._auth,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
        )
        if response.status_code != _OK:
            _LOG.warning("attendance answered %d to a session list", response.status_code)
            return None

        sessions = _json_object(response.text).get("sessions")
        return bool(sessions) if is_list(sessions) else None


def _build_client(target: AttendanceTarget) -> httpx.Client:
    transport = httpx.HTTPTransport(uds=str(target.socket)) if target.socket is not None else None

    return httpx.Client(base_url=target.url, transport=transport)


def _accepted_of(raw: str) -> AcceptedTurn:
    body = _json_object(raw)
    turn = field_text(body, "turn")
    state = field_text(body, "state")
    journal_seq = body.get("journal_seq")
    if not turn or not state or not isinstance(journal_seq, int):
        raise AttendanceError("internal", "attendance's accepted response is malformed", _ACCEPTED)

    return AcceptedTurn(turn=turn, state=state, journal_seq=journal_seq)


def _error_of(status: int, raw: str) -> AttendanceError:
    """Read contract 02 §14's error body. A missing or malformed one still
    fails, as `internal`, rather than swallowing the refusal."""
    error = as_object(_json_object(raw).get("error"))
    code = field_text(error, "code") or "internal"
    message = field_text(error, "message") or f"attendance answered {status}"

    return AttendanceError(code, message, status)


def _json_object(raw: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError: an answer that nests too deep is not a ValueError.
        return {}

    return as_object(parsed)
