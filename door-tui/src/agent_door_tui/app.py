"""The TUI front door: find or make a session, then hand over the terminal.

Invariant 3: "One session, every UI. An Open WebUI chat and a TUI session
within the same family are the same session." The TUI is pi's own interactive
UI, run INSIDE the family sandbox on the same session store. This door is the
host-side program the operator types.

Each step below exists because of the one before it.

1. Read the family's status document. A family that is not attended, or that
   is mid-switch, is refused before the operator is asked to choose anything.
2. Pick the session: `--session`, `--new`, or the numbered list.
3. A NEW session is created through `sessiond` FIRST, so the session exists
   in the one place that owns sessions before pi writes a byte.
4. Take the writer lease as `door-tui`. A lease another door holds refuses:
   contention never queues and never steals.
5. Ask `sessiond` to release the session's held-open pi process (§5.11), so
   the terminal never waits out `pi_idle_ttl_s` behind pi's own fence. A
   turn still in flight is stopped first: §5.11 refuses while one runs.
6. Re-read the status document, because the sandbox may have changed while
   the operator was choosing, and exec `sbx exec -it`.
7. The terminal belongs to pi. When pi exits, release the lease and exit with
   pi's code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import DoorError, Exit
from .ids import TITLE_MAX, is_session, new_session_id
from .launch import EXIT_SBX_FAILED, Store, TerminalRunner, launch_argv
from .lease import WriterLease
from .picker import NEW_SESSION, Terminal, pick_session
from .sessiond import SessiondClient, SessiondError, SessionRow, Takeover, raise_refusal
from .signals import Interrupted, SignalGuard
from .status import FamilyDirectory, Serving

#: Contract 03 §7.6's exit code for a session a live rpc process holds.
EXIT_SESSION_HELD = 8
#: Contract 03 §7.6's exit code for a session with no store and no `--new`.
EXIT_NO_STORE = 9


class Want(Enum):
    """Which session this run is after."""

    PICK = "pick"
    NEW = "new"
    NAMED = "named"


@dataclass(frozen=True)
class Request:
    """One command line, already parsed."""

    family: str
    want: Want
    session: str = ""
    title: str = ""
    #: `--force`, and only from the operator. The door never picks it.
    takeover: Takeover = Takeover.POLITE


@dataclass(frozen=True)
class Chosen:
    """The session this run will attach to, and whether it is brand new."""

    session: str
    store: Store


class TuiDoor:
    """One run of `agent-tui`. Everything external arrives as a collaborator."""

    def __init__(
        self,
        door: SessiondClient,
        families: FamilyDirectory,
        terminal: Terminal,
        runner: TerminalRunner,
        instance: str,
        sbx: str,
        launcher: str,
    ) -> None:
        self._door = door
        self._families = families
        self._terminal = terminal
        self._runner = runner
        self._instance = instance
        self._sbx = sbx
        self._launcher = launcher
        self._lease: WriterLease | None = None
        self._child_running = False

    def run(self, request: Request) -> int:
        """Do the seven steps. Returns the code the shell will see."""
        # Step 1. A family that cannot serve a terminal is refused before
        # the operator is asked to choose a session inside it.
        self._families.serving(request.family)

        chosen = self._choose(request)
        lease = WriterLease(self._door, request.family, chosen.session, self._instance)
        self._lease = lease

        try:
            return self._attach(request, chosen, lease)
        except Interrupted:
            # A signal arrived while no child owned the terminal.
            return int(Exit.NO_CHOICE)
        finally:
            self._give_up(lease)

    def _give_up(self, lease: WriterLease) -> None:
        """Release the lease and say the session is writable again.

        Contract 02 §5.10 makes that immediate. Before it existed the person
        who closed a terminal had to be told about a 60-second wait.
        """
        was_held = lease.held
        lease.release()

        if was_held:
            self._terminal.show(lease.release_line())

    # ------------------------------------------------------------ the session

    def _choose(self, request: Request) -> Chosen:
        if request.want is Want.NAMED:
            return self._named(request)

        if request.want is Want.NEW:
            return self._create(request)

        return self._picked(request)

    def _named(self, request: Request) -> Chosen:
        """`--session <id>`: attach to a session that already exists."""
        if not is_session(request.session):
            raise DoorError(Exit.BAD_USAGE, f"{request.session!r} is not a session id.")

        return Chosen(request.session, _store_of(self._read(request.family, request.session)))

    def _picked(self, request: Request) -> Chosen:
        """The numbered list. `0` starts a new session."""
        rows = self._list(request.family)
        choice = pick_session(rows, self._terminal)

        if choice == NEW_SESSION:
            return self._create(request)

        found = next((row for row in rows if row.session == choice.session), None)

        return Chosen(choice.session, _store_of(found))

    def _create(self, request: Request) -> Chosen:
        """Step 3. `sessiond` owns sessions, so it makes one before pi does."""
        session = new_session_id()
        title = request.title[:TITLE_MAX]

        try:
            self._door.create_session(request.family, session, title)
        except SessiondError as error:
            raise raise_refusal(error) from error

        self._terminal.show(f"New session {session} in family {request.family}.")

        return Chosen(session, Store.NONE_YET)

    # ------------------------------------------------------------ the terminal

    def _attach(self, request: Request, chosen: Chosen, lease: WriterLease) -> int:
        family = request.family

        # Step 4. Contention refuses. `take` raises with the holder named.
        lease.take(request.takeover)

        # Step 5. Free the session's pi process before the terminal starts.
        self._release_process(family, chosen.session)

        # Step 6. The status document is read again: a switch can start while
        # the operator is choosing, and the sandbox id is what the exec names.
        serving = self._families.serving(family)
        self._warn(serving)

        lease.start_renewal()
        code = self._hand_over(serving, chosen)
        self._explain(code, family, chosen.session)

        return code

    def _release_process(self, family: str, session: str) -> None:
        """Ask `sessiond` to let go of the session's pi process (§5.11).

        The operation maps to contract 03 §4.5's `stop_process`, which only
        `sessiond` may send. A turn still in flight is stopped first, because
        §5.11 rule 2 refuses while one is. Without this the terminal meets
        contract 03 §7.6's fence, exit 8, for `pi_idle_ttl_s`.
        """
        self._stop_unfinished(family, session)

        try:
            self._door.release_process(family, session, self._instance)
        except SessiondError as error:
            raise raise_refusal(error) from error

    def _stop_unfinished(self, family: str, session: str) -> None:
        """Stop the session's turn in flight, if it has one."""
        row = self._read(family, session)

        if row is None:
            return

        turn = row.unfinished_turn

        if turn is None:
            return

        self._terminal.show(f"Stopping turn {turn}, which is still in flight.")

        try:
            self._door.stop_turn(family, session, turn)
        except SessiondError as error:
            raise raise_refusal(error) from error

    def _hand_over(self, serving: Serving, chosen: Chosen) -> int:
        """Step 7. From here the terminal belongs to pi."""
        argv = launch_argv(
            sbx=self._sbx,
            supervisor_env=serving.supervisor_env,
            sandbox=serving.sandbox,
            session=chosen.session,
            launcher=self._launcher,
            store=chosen.store,
        )
        self._terminal.show(f"Attaching to {chosen.session} in sandbox {serving.sandbox}.")

        with SignalGuard(self._on_signal):
            self._child_running = True
            try:
                return self._runner.run(argv)
            finally:
                self._child_running = False

    def _on_signal(self, _signum: int) -> None:
        """Give the lease up at once, and never kill the child.

        pi already received the same signal from the terminal driver and will
        exit on its own. Killing it here would throw away whatever the person
        was in the middle of, and the door still has to report pi's code.
        """
        lease = self._lease

        if lease is not None:
            lease.release()

        if not self._child_running:
            raise Interrupted(_signum)

    # -------------------------------------------------------------- reporting

    def _explain(self, code: int, family: str, session: str) -> None:
        """Turn the launcher's exit code into a sentence with a next step."""
        if code == EXIT_SESSION_HELD:
            self._terminal.show(
                f"The supervisor still holds a pi process for {session}. "
                "It is freed when the family's idle timeout passes "
                "(pi_idle_ttl_s, 900s for an attended family) or when the next "
                "turn reaps it. Contract 03 §7.5."
            )
            return

        if code == EXIT_NO_STORE:
            self._terminal.show(
                f"Session {session} has no pi store in the sandbox yet. "
                f"Run `agent-tui {family} --new` to start one."
            )
            return

        if code == EXIT_SBX_FAILED:
            self._terminal.show("sbx did not run. Check that it is on PATH.")

    def _warn(self, serving: Serving) -> None:
        if serving.warning:
            self._terminal.show(f"Note: {serving.warning}.")

    # -------------------------------------------------------- sessiond, safely

    def _list(self, family: str) -> list[SessionRow]:
        try:
            return self._door.sessions(family)
        except SessiondError as error:
            raise raise_refusal(error) from error

    def _read(self, family: str, session: str) -> SessionRow | None:
        """One session, or None when `sessiond` does not know it yet."""
        try:
            return self._door.get_session(family, session)
        except SessiondError as error:
            if error.status == _NOT_FOUND:
                return None

            raise raise_refusal(error) from error


_NOT_FOUND = 404


def _store_of(row: SessionRow | None) -> Store:
    """Whether the sandbox already holds a pi store for this session.

    A session that has never run a turn has no store, and contract 03 §7.6
    exits 9 without `--new`. `turns_total` is the host's own count, so it
    answers without reaching into the sandbox.
    """
    if row is None or row.turns_total == 0:
        return Store.NONE_YET

    return Store.EXISTING
