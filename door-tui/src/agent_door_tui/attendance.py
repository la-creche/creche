"""The client of `attendance`, the session service (contract 02).

The TUI door holds no session state. `attendance` owns sessions, and this door
asks it for seven things and nothing else:

1. list a family's sessions, for the picker (§5.2),
2. create or find one (§5.1),
3. read one, with its newest turns (§5.3),
4. take or renew the writer lease (§5.9),
5. release the writer lease (§5.10),
6. release the session's held-open pi process (§5.11),
7. stop a turn (§5.7).

The client is synchronous. This door makes a call, then blocks on a terminal
in the foreground, so an event loop would buy nothing and would have to
survive a child that owns the tty.

Two rules here are not style choices.

1. The token never reaches argv, a URL or a message (invariant 13). It is
   read from a file into memory once, in `config.py`, and sent in one header.
2. Every answer is untrusted input. Shape is checked before a field is read
   (invariants 12 and 14), so a malformed body is a refusal and never a
   crash in the middle of a terminal session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Protocol

import httpx

from .config import TuiConfig
from .errors import DoorError, Exit
from .untrusted import as_list, as_object, field_int, field_text, is_object

DOOR_NAME = "tui"
DOOR_INSTANCE_HEADER = "X-Door-Instance"

#: Contract 02 §5.3's cap. The door needs only the newest few.
TURNS_WANTED = 10
#: Contract 02 §5.2's cap.
SESSIONS_WANTED = 200

#: Contract 02 §14's code for a lease another door holds.
CODE_SESSION_BUSY = "session_busy"
#: Contract 02 §14, §7.4. A renew found the lease in another door's hands.
CODE_LEASE_TAKEN_OVER = "lease_taken_over"
#: This client's own code. `attendance` never sends it: the socket did not answer.
CODE_UNREACHABLE = "unreachable"

_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 30.0

_OK = 200
_CREATED = 201

#: Contract 02 §5.7's reason field. It says who stopped the turn and why.
_STOP_REASON = "tui_takeover"


class SessionState(StrEnum):
    """Contract 02 §4.1's five states, plus one for a state this door
    does not know. A closed enum over untrusted input would raise."""

    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting-approval"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TurnState(StrEnum):
    """Contract 02 §4.3. Only the three terminal ones end a turn."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting-approval"
    SETTLED = "settled"
    FAILED = "failed"
    ABORTED = "aborted"
    UNKNOWN = "unknown"


_TERMINAL = frozenset({TurnState.SETTLED, TurnState.FAILED, TurnState.ABORTED})


class Takeover(Enum):
    """Whether this call may take a lease another TERMINAL still holds.

    `POLITE` is the default and the only one the door reaches on its own.
    `FORCE` is contract 02 §7.3 rule 6, and it has one job: take an idle
    lease from another instance of this same door. Another door's idle
    lease passes without it (rule 5), and no flag takes an active session
    (rule 4). It takes a deliberate `--force` from the operator.
    """

    POLITE = "polite"
    FORCE = "force"


class Intent(Enum):
    """Take a lease, or keep the one this terminal already holds (§7.4).

    A renew never takes anything. Sent as an acquire, a renewal timer would
    take back a session the human had just moved to Open WebUI, and the two
    doors would trade the lease every 20 seconds with nobody asking.
    """

    ACQUIRE = "acquire"
    RENEW = "renew"


@dataclass(frozen=True)
class SessionRow:
    """One session, with the fields the picker prints (contract 02 §4.2)."""

    family: str
    session: str
    title: str
    state: SessionState
    updated_at: str
    turns_total: int
    turns_running: int
    #: Newest first, from §5.3. Empty on a listing, which carries no turns.
    turns: tuple[tuple[str, TurnState], ...] = field(default=())

    @property
    def unfinished_turn(self) -> str | None:
        """The newest turn that has not reached a terminal state, if any."""
        for turn, state in self.turns:
            if state not in _TERMINAL:
                return turn

        return None


class AttendanceError(Exception):
    """One error from contract 02 §14, as `attendance` reported it."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail if detail is not None else {}


class AttendanceClient(Protocol):
    """What the TUI door needs from the session service, and nothing more."""

    def sessions(self, family: str) -> list[SessionRow]:
        """The family's sessions, newest first. Contract 02 §5.2."""
        ...

    def create_session(self, family: str, session: str, title: str) -> SessionRow:
        """Create or find the session. Contract 02 §5.1."""
        ...

    def get_session(self, family: str, session: str) -> SessionRow:
        """One session and its newest turns. Contract 02 §5.3."""
        ...

    def take_writer(
        self,
        family: str,
        session: str,
        door_instance: str,
        takeover: Takeover = Takeover.POLITE,
        intent: Intent = Intent.ACQUIRE,
    ) -> None:
        """Take or renew the writer lease. Contract 02 §5.9."""
        ...

    def release_writer(self, family: str, session: str, door_instance: str) -> None:
        """Give the writer lease back now. Contract 02 §5.10."""
        ...

    def release_process(self, family: str, session: str, door_instance: str) -> None:
        """Close the session's held-open pi process. Contract 02 §5.11."""
        ...

    def stop_turn(self, family: str, session: str, turn: str) -> None:
        """Stop a turn that is still in flight. Contract 02 §5.7."""
        ...


class HttpAttendance:
    """`AttendanceClient` over HTTP, on a Unix socket or the LAN address."""

    def __init__(self, config: TuiConfig, client: httpx.Client | None = None) -> None:
        self._client = client if client is not None else _build_client(config)
        self._auth = {"Authorization": f"Bearer {config.attendance_token}"}

    def close(self) -> None:
        self._client.close()

    def sessions(self, family: str) -> list[SessionRow]:
        body = self._call(
            "GET", "/v1/sessions", params={"family": family, "limit": SESSIONS_WANTED}
        )

        return [_row_of(as_object(one)) for one in as_list(body.get("sessions")) if is_object(one)]

    def create_session(self, family: str, session: str, title: str) -> SessionRow:
        payload: dict[str, object] = {
            "family": family,
            "session": session,
            "labels": {"door": DOOR_NAME},
        }

        if title:
            payload["title"] = title

        return _row_of(self._call("POST", "/v1/sessions", json=payload))

    def get_session(self, family: str, session: str) -> SessionRow:
        path = f"/v1/sessions/{family}/{session}"

        return _row_of(self._call("GET", path, params={"turns": TURNS_WANTED}))

    def take_writer(
        self,
        family: str,
        session: str,
        door_instance: str,
        takeover: Takeover = Takeover.POLITE,
        intent: Intent = Intent.ACQUIRE,
    ) -> None:
        # `force` is false unless the operator typed `--force`, and it means one
        # thing: take an idle lease from another terminal (§7.3 rule 6). Even
        # forced, `attendance` refuses while a turn is in flight (rule 4).
        self._call(
            "POST",
            f"/v1/sessions/{family}/{session}/writer",
            json={
                "holder": DOOR_NAME,
                "force": takeover is Takeover.FORCE,
                "intent": intent.value,
            },
            headers={DOOR_INSTANCE_HEADER: door_instance},
        )

    def release_writer(self, family: str, session: str, door_instance: str) -> None:
        self._call(
            "DELETE",
            f"/v1/sessions/{family}/{session}/writer",
            headers={DOOR_INSTANCE_HEADER: door_instance},
        )

    def release_process(self, family: str, session: str, door_instance: str) -> None:
        self._call(
            "POST",
            f"/v1/sessions/{family}/{session}/release-process",
            headers={DOOR_INSTANCE_HEADER: door_instance},
        )

    def stop_turn(self, family: str, session: str, turn: str) -> None:
        self._call(
            "POST",
            f"/v1/sessions/{family}/{session}/turns/{turn}/stop",
            json={"reason": _STOP_REASON},
        )

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """One request, one refusal table. Every failure is `AttendanceError`."""
        sent = dict(self._auth)

        if headers is not None:
            sent.update(headers)

        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers=sent,
                timeout=httpx.Timeout(_CONNECT_TIMEOUT_S, read=_READ_TIMEOUT_S),
            )
        except httpx.HTTPError as exc:
            # The socket is this door's only way in. Naming the failure as its
            # own code keeps a dead service from reading as a refused call.
            raise AttendanceError(CODE_UNREACHABLE, f"attendance did not answer: {exc}", 0) from exc

        if response.status_code not in (_OK, _CREATED):
            raise _error_of(response.status_code, response.text)

        return _json_object(response.text)


@dataclass(frozen=True)
class Refusal:
    """What the door prints and the code it exits with."""

    code: Exit
    message: str


def refusal_of(error: AttendanceError) -> Refusal:
    """Turn one `attendance` error into the line the operator reads and an exit code."""
    if error.code == CODE_SESSION_BUSY:
        return Refusal(Exit.SESSION_BUSY, _busy_line(error))

    return Refusal(Exit.ATTENDANCE, f"attendance refused: {error.code}: {error.message}")


def raise_refusal(error: AttendanceError) -> DoorError:
    """The same mapping, as the exception the command line handles."""
    refusal = refusal_of(error)

    return DoorError(refusal.code, refusal.message)


def _busy_line(error: AttendanceError) -> str:
    """Contract 02 §7.2's holder block, said plainly.

    Contention refuses, so the line has to say who holds the session and
    what would free it. The two cases lead to different next steps.

    1. A turn is in flight. Nothing takes the lease from it: contract 02
       §7.3 rule 4 refuses `force` too. Waiting is the only move, so
       `--force` is not offered.
    2. The holder is idle, which means another TERMINAL: an
       idle lease held by any other door passes without a refusal (rule 5).
       `--force` is the sanctioned takeover (rule 6).
    """
    holder = field_text(error.detail, "holder") or "another door"
    turn = field_text(error.detail, "turn")

    if turn:
        return (
            f"{holder} holds the writer lease and is running turn {turn}. "
            "Stop it in that UI, or wait for it to settle, then run this again."
        )

    return (
        f"Another {holder} terminal holds the writer lease on this session "
        "and is idle. Close it, or pass --force to take the session now."
    )


def _build_client(config: TuiConfig) -> httpx.Client:
    transport = (
        httpx.HTTPTransport(uds=str(config.attendance_socket))
        if config.attendance_socket is not None
        else None
    )

    return httpx.Client(base_url=config.attendance_url, transport=transport)


def _row_of(body: dict[str, object]) -> SessionRow:
    return SessionRow(
        family=field_text(body, "family"),
        session=field_text(body, "session"),
        title=field_text(body, "title"),
        state=_state_of(field_text(body, "state")),
        updated_at=field_text(body, "updated_at"),
        turns_total=field_int(body, "turns_total"),
        turns_running=field_int(body, "turns_running"),
        turns=_turns_of(body.get("turns")),
    )


def _turns_of(raw: object) -> tuple[tuple[str, TurnState], ...]:
    found: list[tuple[str, TurnState]] = []

    for one in as_list(raw):
        if not is_object(one):
            continue

        turn = field_text(one, "turn")

        if turn:
            found.append((turn, _turn_state_of(field_text(one, "state"))))

    return tuple(found)


def _state_of(raw: str) -> SessionState:
    try:
        return SessionState(raw)
    except ValueError:
        return SessionState.UNKNOWN


def _turn_state_of(raw: str) -> TurnState:
    try:
        return TurnState(raw)
    except ValueError:
        # An unknown state is treated as unfinished, which is the safe
        # direction: the door stops it rather than opening a terminal beside it.
        return TurnState.UNKNOWN


def _error_of(status: int, raw: str) -> AttendanceError:
    """Read contract 02 §14's error body. A missing one still fails."""
    body = _json_object(raw)
    error = as_object(body.get("error"))
    code = field_text(error, "code") or "internal"
    message = field_text(error, "message") or f"attendance answered {status}"

    return AttendanceError(code, message, status, as_object(error.get("detail")))


def _json_object(raw: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return {}

    return parsed if is_object(parsed) else {}
