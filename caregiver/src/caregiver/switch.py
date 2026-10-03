"""`POST /internal/switch-sandbox` — the one call `caregiver` makes to
`attendance` (contract 05 §1, §5).

Everything else the two services share travels through the status
document, so this whole module is that one call: its request, its answer,
and the bearer token that authorises it.

`attendance` holds every session and every channel. Only it can move new
turns from one sandbox to another, and only it knows how many turns are
still running on the outgoing one. `caregiver` asks and waits.

A refusal is an exception, never a default answer. A caller that read a
refusal as a success would destroy the sandbox that is still serving every
session in the family."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast

import httpx
from agent_family import SwitchMode

SWITCH_PATH: Final = "/internal/switch-sandbox"

#: Contract 05 §5.1: how long `attendance` may take before it answers.
DEFAULT_DEADLINE_S: Final = 300

REQUEST_TIMEOUT_MARGIN_S: Final = 30

HTTP_OK: Final = 200


class SwitchError(RuntimeError):
    """`attendance` did not move the family onto the new sandbox."""


@dataclass(frozen=True)
class SwitchRequest:
    """Contract 05 §5.1. `outgoing` is the wire's `from`, which is a Python
    keyword and so cannot be the field's name."""

    family: str
    outgoing: str | None
    to: str
    mode: SwitchMode
    reason: str
    deadline_s: int = DEFAULT_DEADLINE_S

    def as_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "from": self.outgoing,
            "to": self.to,
            "mode": str(self.mode),
            "reason": self.reason,
            "deadline_s": self.deadline_s,
        }


@dataclass(frozen=True)
class SwitchResult:
    """Contract 05 §5.2. The counts describe the OUTGOING sandbox only:
    `switched` is already true the moment `attendance` accepts the call."""

    switched: bool
    outcome: str
    turns_running_at_start: int = 0
    turns_finished: int = 0
    turns_aborted: int = 0
    sessions: int = 0


class SwitchClient(Protocol):
    def switch(self, request: SwitchRequest) -> SwitchResult: ...


def read_token(path: Path) -> str:
    """The `caregiver` token `attendance` expects (contract 02 §3 rule 5).

    A missing or empty file refuses before anything is sent. An empty key
    once turned a LAN admin surface into an open one; a client that would
    send `Bearer ` is the same mistake from the other side."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SwitchError(f"cannot read the caregiver token at {path}") from exc

    if not token:
        raise SwitchError(f"the caregiver token at {path} is empty")

    return token


#: What the base URL reads when the transport dials a Unix socket.
SOCKET_BASE_URL = "http://sessiond"


class HttpSwitchClient:
    """Speaks to `attendance` over its Unix socket or its LAN address.

    The token rides in a bearer header. It never appears in the URL, on
    argv or in a log line (invariant 13), and no method here puts a
    response body in an exception message."""

    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        if not token:
            raise SwitchError("the caregiver token is empty")

        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._client = client or httpx.Client(timeout=DEFAULT_DEADLINE_S + REQUEST_TIMEOUT_MARGIN_S)

    @classmethod
    def over_socket(cls, socket: Path, token: str) -> HttpSwitchClient:
        """`attendance` on its Unix socket. A gate runs it there and nowhere
        else, so a URL alone can never reach it. The host part of the base
        URL names nothing: the transport dials the socket."""
        client = httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)),
            timeout=DEFAULT_DEADLINE_S + REQUEST_TIMEOUT_MARGIN_S,
        )
        return cls(SOCKET_BASE_URL, token, client)

    def switch(self, request: SwitchRequest) -> SwitchResult:
        try:
            response = self._client.post(
                f"{self._base}{SWITCH_PATH}", headers=self._headers, json=request.as_json()
            )
        except httpx.HTTPError as exc:
            raise SwitchError(f"switch {request.to}: {type(exc).__name__}") from exc

        if response.status_code != HTTP_OK:
            raise SwitchError(
                f"switch {request.outgoing} to {request.to}: HTTP {response.status_code}"
            )

        return _result_of(response, request)


def _result_of(response: httpx.Response, request: SwitchRequest) -> SwitchResult:
    body = _object(response)
    if body.get("switched") is not True:
        # Contract 05 §5.3 rule 1: `switched` is what says new turns now go
        # to `to`. Without it the outgoing sandbox is still the family's,
        # and destroying it would end every session.
        raise SwitchError(f"switch to {request.to}: attendance did not switch")

    return SwitchResult(
        switched=True,
        outcome=str(body.get("outcome", "")),
        turns_running_at_start=_count(body.get("turns_running_at_start")),
        turns_finished=_count(body.get("turns_finished")),
        turns_aborted=_count(body.get("turns_aborted")),
        sessions=_count(body.get("sessions")),
    )


def _object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise SwitchError("attendance answered a body that is not JSON") from exc

    if not isinstance(body, dict):
        raise SwitchError("attendance answered a body that is not an object")

    return cast("dict[str, Any]", body)


def _count(value: Any) -> int:
    """A field the answer leaves out reads zero. It is a report, not a
    condition of the switch."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class FakeSwitchClient:
    """Records every request. `refuse` makes each call raise, which is how
    a test drives an `attendance` whose handler answers 501.

    Locked, because families converge side by side and two
    of them may ask for a switch at the same time. `HttpSwitchClient`
    needs nothing: `httpx.Client` is already safe to share."""

    def __init__(self, refuse: str | None = None, result: SwitchResult | None = None) -> None:
        self.requests: list[SwitchRequest] = []
        self._refuse = refuse
        self._result = result or SwitchResult(switched=True, outcome="drained")
        self._mutex = threading.Lock()

    def switch(self, request: SwitchRequest) -> SwitchResult:
        with self._mutex:
            self.requests.append(request)

        if self._refuse is not None:
            raise SwitchError(self._refuse)

        return self._result
