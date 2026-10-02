"""A fake `sessiond` for this door, behind the door's own client protocol.

Contract 02 §15 describes a full wire-level fake. This is the smaller thing:
every external thing behind a small protocol, so a test never touches a live
service and never needs the host.

It covers exactly what the TUI door calls — list, create-or-find, get one,
take or renew the writer lease, release it, release the pi process, stop a
turn — and it keeps the state those seven need: the session rows, one lease
per session, and a call log a test reads back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_door_tui.sessiond import (
    Intent,
    SessiondError,
    SessionRow,
    SessionState,
    Takeover,
    TurnState,
)

#: Contract 02 §14's codes, for the three this fake answers with.
CODE_SESSION_BUSY = "session_busy"
CODE_LEASE_TAKEN_OVER = "lease_taken_over"
CODE_NOT_FOUND = "not_found"

HTTP_CONFLICT = 409
HTTP_NOT_FOUND = 404

FAMILY = "chat"


@dataclass
class Held:
    """One writer lease, as contract 02 §7.1 shapes it."""

    holder: str
    door_instance: str
    turn: str | None = None


@dataclass
class FakeSessiond:
    """Everything the TUI door asks of the session service, in memory."""

    rows: list[SessionRow] = field(default_factory=list["SessionRow"])
    leases: dict[tuple[str, str], Held] = field(default_factory=dict[tuple[str, str], "Held"])
    calls: list[str] = field(default_factory=list[str])
    stopped: list[str] = field(default_factory=list[str])
    #: Sessions a caller asked to take with `force`.
    forced: list[str] = field(default_factory=list[str])
    #: Sessions whose pi process a caller asked to close (contract 02 §5.11).
    processes_released: list[str] = field(default_factory=list[str])
    #: Set to raise on the next call, so a test can force a refusal.
    fail_with: SessiondError | None = None

    def sessions(self, family: str) -> list[SessionRow]:
        self._check()
        self.calls.append(f"sessions {family}")

        return [one for one in self.rows if one.family == family]

    def create_session(self, family: str, session: str, title: str) -> SessionRow:
        self._check()
        self.calls.append(f"create {family}/{session}")
        found = self._find(family, session)

        if found is not None:
            return found

        made = row(session, family=family, title=title, turns_total=0)
        self.rows.append(made)

        return made

    def get_session(self, family: str, session: str) -> SessionRow:
        self._check()
        self.calls.append(f"get {family}/{session}")
        found = self._find(family, session)

        if found is None:
            raise SessiondError(CODE_NOT_FOUND, "no such session", HTTP_NOT_FOUND)

        return found

    def take_writer(
        self,
        family: str,
        session: str,
        door_instance: str,
        takeover: Takeover = Takeover.POLITE,
        intent: Intent = Intent.ACQUIRE,
    ) -> None:
        self._check()
        self.calls.append(f"writer {family}/{session}")

        if takeover is Takeover.FORCE:
            self.forced.append(session)

        held = self.leases.get((family, session))

        if held is None or held.door_instance == door_instance:
            self.leases[(family, session)] = Held("tui", door_instance)
            return

        if intent is Intent.RENEW:
            raise SessiondError(
                CODE_LEASE_TAKEN_OVER,
                "another door took this idle lease",
                HTTP_CONFLICT,
                detail={"holder": held.holder, "turn": held.turn},
            )

        # Contract 02 §7.3's table, in the three rows this door can meet.
        # Rule 4 refuses an active session whatever `force` says, rule 5
        # hands over another door's idle lease, and rule 6 needs `force` for
        # another terminal's.
        if held.turn is not None:
            raise self._busy(held)

        if held.holder == "tui" and takeover is not Takeover.FORCE:
            raise self._busy(held)

        self.leases[(family, session)] = Held("tui", door_instance)

    def release_writer(self, family: str, session: str, door_instance: str) -> None:
        self._check()
        self.calls.append(f"release-writer {family}/{session}")
        held = self.leases.get((family, session))

        if held is None:
            return

        if held.door_instance != door_instance:
            raise self._busy(held)

        del self.leases[(family, session)]

    def release_process(self, family: str, session: str, door_instance: str) -> None:
        self._check()
        self.calls.append(f"release-process {family}/{session}")
        self.processes_released.append(session)

    def stop_turn(self, family: str, session: str, turn: str) -> None:
        self._check()
        self.calls.append(f"stop {family}/{session}/{turn}")
        self.stopped.append(turn)
        self._settle(family, session)

    def _busy(self, held: Held) -> SessiondError:
        return SessiondError(
            CODE_SESSION_BUSY,
            "another door holds the writer lease",
            HTTP_CONFLICT,
            detail={"holder": held.holder, "turn": held.turn},
        )

    # ------------------------------------------------------------ test helpers

    def add(self, one: SessionRow) -> SessionRow:
        self.rows.append(one)

        return one

    def hold(
        self, family: str, session: str, holder: str = "owui", turn: str | None = None
    ) -> None:
        """Put another door's lease on a session, so the TUI meets a refusal."""
        self.leases[(family, session)] = Held(holder, f"{holder}.1", turn)

    def writer_calls(self, family: str, session: str) -> int:
        return self.calls.count(f"writer {family}/{session}")

    def holds(self, family: str, session: str, door_instance: str) -> bool:
        held = self.leases.get((family, session))

        return held is not None and held.door_instance == door_instance

    def _settle(self, family: str, session: str) -> None:
        """A stopped turn is terminal, so a later read finds nothing in flight."""
        found = self._find(family, session)

        if found is None:
            return

        settled = tuple((turn, TurnState.ABORTED) for turn, _ in found.turns)
        self.rows[self.rows.index(found)] = replace_turns(found, settled)

    def _find(self, family: str, session: str) -> SessionRow | None:
        for one in self.rows:
            if one.family == family and one.session == session:
                return one

        return None

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with


def replace_turns(one: SessionRow, turns: tuple[tuple[str, TurnState], ...]) -> SessionRow:
    return SessionRow(
        family=one.family,
        session=one.session,
        title=one.title,
        state=one.state,
        updated_at=one.updated_at,
        turns_total=one.turns_total,
        turns_running=one.turns_running,
        turns=turns,
    )


def row(
    session: str,
    *,
    family: str = FAMILY,
    title: str = "",
    state: SessionState = SessionState.IDLE,
    updated_at: str = "2026-09-19T10:00:00Z",
    turns_total: int = 1,
    turns: tuple[tuple[str, TurnState], ...] = (),
) -> SessionRow:
    """One session row, with the fields the picker prints."""
    return SessionRow(
        family=family,
        session=session,
        title=title,
        state=state,
        updated_at=updated_at,
        turns_total=turns_total,
        turns_running=0,
        turns=turns,
    )
