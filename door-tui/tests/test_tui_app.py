"""One run of `agent-tui`, end to end against fakes.

Every external thing is behind a small protocol: `sessiond` is
`FakeSessiond`, the status document is a fake family directory, the terminal
is scripted, and `sbx` is a runner that records its argv. Nothing here
touches a live service and nothing needs the host.
"""

from __future__ import annotations

import os
import signal

import pytest
from agent_door_tui.app import EXIT_NO_STORE, EXIT_SESSION_HELD, Request, TuiDoor, Want
from agent_door_tui.errors import DoorError, Exit
from agent_door_tui.launch import LAUNCHER_ARG_NEW
from agent_door_tui.lease import WriterLease
from agent_door_tui.picker import ScriptedTerminal
from agent_door_tui.sessiond import SessiondError, SessionState, Takeover, TurnState
from agent_door_tui.status import Serving
from fake_tui_sessiond import FAMILY, FakeSessiond, row

SANDBOX = "chat-s1"
ENV_FILE = "/srv/agents/state/rework/families/chat/supervisor.env"
LAUNCHER = "/opt/agent-supervisor/agent-pi-launch.js"
INSTANCE = "tui.4242"
OWUI_SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"


class FakeFamilies:
    """The status document, as the door reads it (contract 05 §2)."""

    def __init__(self, serving: Serving | None = None) -> None:
        self.serving_value = serving or Serving(SANDBOX, ENV_FILE)
        self.raise_on_read: DoorError | None = None
        self.reads = 0

    def attended(self) -> list[str]:
        return [FAMILY]

    def serving(self, family: str) -> Serving:
        self.reads += 1

        if self.raise_on_read is not None:
            raise self.raise_on_read

        return self.serving_value


class FakeRunner:
    """Stands in for `sbx exec -it`. It records argv and returns a code."""

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.argv: list[str] = []
        self.on_run: object = None

    def run(self, argv: list[str]) -> int:
        self.argv = argv
        callback = self.on_run

        if callable(callback):
            callback()

        return self.code


class Fixture:
    """One door, with every collaborator a test can reach."""

    def __init__(self, answers: list[str] | None = None, code: int = 0) -> None:
        self.door = FakeSessiond()
        self.families = FakeFamilies()
        self.terminal = ScriptedTerminal(answers if answers is not None else [])
        self.runner = FakeRunner(code)
        self.tui = TuiDoor(
            self.door,
            self.families,
            self.terminal,
            self.runner,
            INSTANCE,
            "sbx",
            LAUNCHER,
        )

    def run(self, request: Request) -> int:
        return self.tui.run(request)

    @property
    def shown(self) -> str:
        return "\n".join(self.terminal.shown)

    def created_session(self) -> str:
        made = [one for one in self.door.calls if one.startswith("create ")]

        return made[-1].split("/")[-1] if made else ""

    def lease_held(self) -> bool:
        """Whether the door still holds the lease it took.

        The lease's own state answers that directly. Reading it back is a
        test reaching into one object it built, not a new interface, so it
        goes through `getattr` rather than widening `TuiDoor`.
        """
        lease = self._lease()

        return lease is not None and lease.held

    def lease_renewing(self) -> bool:
        lease = self._lease()

        return lease is not None and lease.renewing

    def _lease(self) -> WriterLease | None:
        found: WriterLease | None = getattr(self.tui, "_lease", None)

        return found


def named(session: str) -> Request:
    return Request(FAMILY, Want.NAMED, session=session)


def new(title: str = "") -> Request:
    return Request(FAMILY, Want.NEW, title=title)


# ----------------------------------------------------------------- the picker


def test_the_picker_lists_then_attaches() -> None:
    fixture = Fixture(answers=["1"])
    fixture.door.add(row("tui-01JB", title="Boiler"))

    assert fixture.run(Request(FAMILY, Want.PICK)) == 0
    assert "tui-01JB" in fixture.runner.argv
    assert "Boiler" in fixture.shown


def test_zero_in_the_picker_creates_a_session() -> None:
    fixture = Fixture(answers=["0"])
    fixture.door.add(row("tui-01JB"))

    fixture.run(Request(FAMILY, Want.PICK))

    assert fixture.created_session().startswith("tui-")
    assert LAUNCHER_ARG_NEW in fixture.runner.argv


def test_the_picker_choosing_nothing_never_takes_a_lease() -> None:
    fixture = Fixture(answers=[""])
    fixture.door.add(row("tui-01JB"))

    with pytest.raises(DoorError) as caught:
        fixture.run(Request(FAMILY, Want.PICK))

    assert caught.value.code is Exit.NO_CHOICE
    assert not [one for one in fixture.door.calls if one.startswith("writer ")]


# ------------------------------------------------------------- a new session


def test_new_creates_the_session_before_anything_else() -> None:
    """A new session exists in `sessiond` before pi writes a byte."""
    fixture = Fixture()

    fixture.run(new("Debug the boiler"))

    first = [one.split(" ")[0] for one in fixture.door.calls]
    assert first[0] == "create", fixture.door.calls
    assert first.index("create") < first.index("writer")


def test_new_passes_the_launcher_flag_for_a_first_writer() -> None:
    fixture = Fixture()

    fixture.run(new())

    assert fixture.runner.argv[-1] == LAUNCHER_ARG_NEW


def test_a_title_over_the_cap_is_cut_not_refused() -> None:
    fixture = Fixture()

    assert fixture.run(new("x" * 900)) == 0


# ------------------------------------------------------------ the named path


def test_session_skips_the_picker() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))

    fixture.run(named("tui-01JB"))

    assert fixture.terminal.asked == []
    assert "tui-01JB" in fixture.runner.argv


def test_a_tui_terminal_attaches_to_an_owui_session() -> None:
    """Invariant 3: one session, every UI. §3.1's prefix check is on create."""
    fixture = Fixture()
    fixture.door.add(row(OWUI_SESSION))

    assert fixture.run(named(OWUI_SESSION)) == 0
    assert OWUI_SESSION in fixture.runner.argv


def test_a_session_id_that_is_not_one_is_refused() -> None:
    fixture = Fixture()

    with pytest.raises(DoorError) as caught:
        fixture.run(named("../../etc/passwd"))

    assert caught.value.code is Exit.BAD_USAGE


def test_a_session_sessiond_does_not_know_is_a_first_writer() -> None:
    fixture = Fixture()

    fixture.run(named("tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR"))

    assert fixture.runner.argv[-1] == LAUNCHER_ARG_NEW


# ----------------------------------------------------------------- the lease


def test_the_lease_is_taken_before_the_terminal_starts() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    order: list[str] = []
    fixture.runner.on_run = lambda: order.append("run")

    fixture.run(named("tui-01JB"))

    assert fixture.door.writer_calls(FAMILY, "tui-01JB") >= 1
    assert order == ["run"]


def test_another_terminals_lease_refuses_and_runs_nothing() -> None:
    """Contention refuses, never queues, never steals."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    fixture.door.hold(FAMILY, "tui-01JB", "tui")

    with pytest.raises(DoorError) as caught:
        fixture.run(named("tui-01JB"))

    assert caught.value.code is Exit.SESSION_BUSY
    assert "tui" in caught.value.message
    assert fixture.runner.argv == []


def test_an_idle_chat_lease_opens_the_terminal() -> None:
    """Contract 02 §7.3 rule 5. A chat continues in the terminal, no --force, no wait."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    fixture.door.hold(FAMILY, "tui-01JB", "owui")

    assert fixture.run(named("tui-01JB")) == 0
    assert fixture.door.forced == []


def test_force_takes_another_terminals_lease() -> None:
    """Contract 02 §7.3 rule 6, reached only by the operator's own `--force`."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    fixture.door.hold(FAMILY, "tui-01JB", "tui")

    code = fixture.run(Request(FAMILY, Want.NAMED, session="tui-01JB", takeover=Takeover.FORCE))

    assert code == 0
    assert fixture.door.forced == ["tui-01JB"]


def test_the_door_never_forces_on_its_own() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))

    fixture.run(named("tui-01JB"))

    assert fixture.door.forced == []


def test_the_lease_is_released_on_a_normal_exit() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))

    fixture.run(named("tui-01JB"))

    assert not fixture.lease_held()


def test_the_door_says_the_session_is_free() -> None:
    """Contract 02 §5.10: the release is immediate."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))

    fixture.run(named("tui-01JB"))

    assert "released" in fixture.shown
    assert "tui-01JB" in fixture.shown


def test_a_refusal_never_talks_about_a_lease_it_never_held() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    fixture.door.hold(FAMILY, "tui-01JB", "tui")

    with pytest.raises(DoorError):
        fixture.run(named("tui-01JB"))

    assert "is released" not in fixture.shown


def test_the_pi_process_is_released_before_the_terminal() -> None:
    """Contract 02 §5.11. No pi_idle_ttl_s wait."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))

    fixture.run(named("tui-01JB"))

    assert fixture.door.processes_released == ["tui-01JB"]


def test_the_lease_is_released_when_the_terminal_fails() -> None:
    fixture = Fixture(code=11)
    fixture.door.add(row("tui-01JB"))

    assert fixture.run(named("tui-01JB")) == 11
    assert not fixture.lease_held()


def test_the_lease_is_renewed_while_the_terminal_runs() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    seen: list[bool] = []
    fixture.runner.on_run = lambda: seen.append(fixture.lease_renewing())

    fixture.run(named("tui-01JB"))

    assert seen == [True], "the renewal thread was not running during the terminal"


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_each_signal_releases_the_lease(signum: int) -> None:
    """SIGTERM and SIGHUP run no `finally` on their own."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    held: list[bool] = []

    def interrupt() -> None:
        os.kill(os.getpid(), signum)
        held.append(fixture.lease_held())

    fixture.runner.on_run = interrupt
    fixture.run(named("tui-01JB"))

    assert held == [False], "the lease was still held after the signal"


def test_a_signal_never_kills_the_child() -> None:
    """pi already got the same signal from the tty. Its exit code still counts."""
    fixture = Fixture(code=42)
    fixture.door.add(row("tui-01JB"))

    def interrupt() -> None:
        import os

        os.kill(os.getpid(), signal.SIGINT)

    fixture.runner.on_run = interrupt

    assert fixture.run(named("tui-01JB")) == 42


def test_the_handlers_are_put_back_after_the_run() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB"))
    before = signal.getsignal(signal.SIGINT)

    fixture.run(named("tui-01JB"))

    assert signal.getsignal(signal.SIGINT) is before


# ----------------------------------------------------- releasing the process


def test_a_turn_in_flight_is_stopped_before_the_terminal() -> None:
    """A turn in flight stops first: contract 02 §5.11 rule 2 refuses while one runs."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB", turns=(("01JBTURN", TurnState.RUNNING),)))
    stopped_first: list[bool] = []
    fixture.runner.on_run = lambda: stopped_first.append(bool(fixture.door.stopped))

    fixture.run(named("tui-01JB"))

    assert fixture.door.stopped == ["01JBTURN"]
    assert stopped_first == [True], "the terminal started while a turn still ran"
    assert "01JBTURN" in fixture.shown


def test_a_settled_turn_is_left_alone() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB", turns=(("01JBTURN", TurnState.SETTLED),)))

    fixture.run(named("tui-01JB"))

    assert fixture.door.stopped == []


def test_a_turn_state_this_door_does_not_know_is_stopped() -> None:
    """The safe direction: a terminal never opens beside a live pi process."""
    fixture = Fixture()
    fixture.door.add(row("tui-01JB", turns=(("01JBTURN", TurnState.UNKNOWN),)))

    fixture.run(named("tui-01JB"))

    assert fixture.door.stopped == ["01JBTURN"]


# --------------------------------------------------------------- the sandbox


def test_a_switch_in_progress_refuses() -> None:
    """A sandbox switch in progress refuses the terminal."""
    fixture = Fixture()
    fixture.families.raise_on_read = DoorError(Exit.NO_SANDBOX, "chat is switching sandboxes")

    with pytest.raises(DoorError) as caught:
        fixture.run(new())

    assert caught.value.code is Exit.NO_SANDBOX
    assert fixture.runner.argv == []


def test_a_switch_that_starts_while_operator_chooses_still_refuses() -> None:
    """The status document is read again after the pick, for that reason."""
    fixture = Fixture(answers=["1"])
    fixture.door.add(row("tui-01JB"))
    calls: list[int] = []

    def second_read(family: str) -> Serving:
        calls.append(1)

        if len(calls) > 1:
            raise DoorError(Exit.NO_SANDBOX, "chat-s1 is draining")

        return Serving(SANDBOX, ENV_FILE)

    fixture.families.serving = second_read  # pyright: ignore[reportAttributeAccessIssue]

    with pytest.raises(DoorError) as caught:
        fixture.run(Request(FAMILY, Want.PICK))

    assert caught.value.code is Exit.NO_SANDBOX
    assert fixture.runner.argv == []


def test_the_family_is_checked_before_the_picker_asks_anything() -> None:
    fixture = Fixture(answers=["1"])
    fixture.families.raise_on_read = DoorError(Exit.BAD_USAGE, "chat is autonomous")

    with pytest.raises(DoorError):
        fixture.run(Request(FAMILY, Want.PICK))

    assert fixture.terminal.asked == []
    assert fixture.door.calls == []


def test_a_warning_is_printed_and_the_terminal_still_opens() -> None:
    fixture = Fixture()
    fixture.families.serving_value = Serving(
        SANDBOX, ENV_FILE, warning="turns are blocked by key_mint_failed"
    )
    fixture.door.add(row("tui-01JB"))

    assert fixture.run(named("tui-01JB")) == 0
    assert "key_mint_failed" in fixture.shown


# --------------------------------------------------------------- the argv out


def test_the_exec_names_the_serving_sandbox_and_its_env_file() -> None:
    fixture = Fixture()
    fixture.families.serving_value = Serving("chat-s7", "/srv/env/chat-s7.env")
    fixture.door.add(row("tui-01JB"))

    fixture.run(named("tui-01JB"))

    argv = fixture.runner.argv
    assert argv[argv.index("--env-file") + 1] == "/srv/env/chat-s7.env"
    assert argv[argv.index("--sandbox") + 1] == "chat-s7"
    assert "chat-s7" in argv


def test_pis_exit_code_is_the_doors_exit_code() -> None:
    assert Fixture(code=0).run(new()) == 0
    assert Fixture(code=130).run(new()) == 130


def test_a_held_session_is_explained_in_plain_words() -> None:
    fixture = Fixture(code=EXIT_SESSION_HELD)
    fixture.door.add(row("tui-01JB"))

    assert fixture.run(named("tui-01JB")) == EXIT_SESSION_HELD
    assert "pi_idle_ttl_s" in fixture.shown


def test_a_missing_store_is_explained_in_plain_words() -> None:
    fixture = Fixture(code=EXIT_NO_STORE)
    fixture.door.add(row("tui-01JB"))

    fixture.run(named("tui-01JB"))

    assert "--new" in fixture.shown


def test_a_sessiond_that_does_not_answer_refuses() -> None:
    fixture = Fixture()
    fixture.door.fail_with = SessiondError("unreachable", "sessiond did not answer", 0)

    with pytest.raises(DoorError) as caught:
        fixture.run(new())

    assert caught.value.code is Exit.SESSIOND
    assert fixture.runner.argv == []


def test_a_running_session_in_the_list_still_shows() -> None:
    """The picker is not health-gated. A running session is visible."""
    fixture = Fixture(answers=["1"])
    fixture.door.add(row("tui-01JB", state=SessionState.RUNNING))

    fixture.run(Request(FAMILY, Want.PICK))

    assert "running" in fixture.shown


def test_a_store_that_exists_never_asks_for_new() -> None:
    fixture = Fixture()
    fixture.door.add(row("tui-01JB", turns_total=4))

    fixture.run(named("tui-01JB"))

    assert LAUNCHER_ARG_NEW not in fixture.runner.argv
