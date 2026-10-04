"""The terminal door on a pseudo-terminal: attach, the lease, and how it ends.

`agent-tui <family>` finds or makes a session, takes the writer lease, and
gives its terminal to pi (`docs/rework/spec.md` §3.7). A test plays the
operator: it types the command at a terminal, and it ends pi with the EOF
key, a signal or a closed terminal.

Each scenario reads four boundaries and no screen text:

1. the record of the `sbx` stand-in: the command of contract 03 §7.6
2. the record of the pi stand-in: a pi process that the launcher started
3. `attendance`: the lease of contract 02 §7, and the journal
4. the exit code of the door

The three scenarios that `integration/tests/test_ct_tui_door.py` has keep
their names, and so does the one of `test_cv_launcher.py`. Those suites run
the door as an object in the test process, with an `sbx` that exits at once.

Each scenario but two starts a terminal on a session that ran a turn and
that no terminal held before. The playpen then holds one pi process that
runs and waits, and the door has it released before the launcher looks.

In the two other scenarios the playpen starts a pi process at about the time
the launcher looks: for a new session (contract 03 §4.7 rule 8), and after a
`tui` lease ended (contract 02 §10.5). What the launcher finds then changes
from run to run. The scenario of a new session and the scenario of
`--force` assert only what holds in each order (`AGENTS.md`, Known gaps).
"""

from __future__ import annotations

import json
import signal
import time

import pytest
from proc_chat import until
from proc_harness import Child, pid_is_alive, pids_gone_by
from proc_ids import ULID
from proc_standins import PI, calls_of
from proc_terminal import ENTER, EOF, INTERRUPT, Terminal
from proc_tree import FAMILY, SANDBOX
from proc_tui import (
    ARG_NEW,
    ARG_SANDBOX,
    ARG_SESSION,
    GRANTED,
    HOLDER,
    RELEASED,
    TAKEN_OVER,
    TUI_PREFIX,
    TuiStack,
    instance_of,
    launch_bundle,
)

#: Contract 03 §7.6: the launcher could read no credential file.
EXIT_NO_CREDENTIALS = 10

#: Contract 03 §1 rule 3: the two words that only the playpen gives pi.
RPC_MODE = ("--mode", "rpc")

PI_DEADLINE_S = 30.0
EXIT_DEADLINE_S = 30.0
GONE_DEADLINE_S = 10.0

TITLE = "Boiler"
FORCE_FLAG = "--force"

#: What the operator types at pi. The pi stand-in reads one command per line.
TERMINAL_PROMPT = "what did the sensor read"
PROMPT_COMMAND = json.dumps({"type": "prompt", "id": "t1", "message": TERMINAL_PROMPT})

#: The journal line of contract 02 §10.5.
TERMINAL_EXCHANGE = "terminal_exchange"

#: The last event of a turn of pi (the rpc documentation of pi).
SETTLED_EVENT = "agent_settled"


async def attach(tui: TuiStack, session: str, *args: str) -> tuple[Child, Terminal]:
    """Type `agent-tui chat --session <id>`, and wait until pi has the terminal."""
    before = len(tui.terminal_pi_starts())
    child, terminal = tui.open_terminal(FAMILY, ARG_SESSION, session, *args)

    await until(
        lambda: len(tui.terminal_pi_starts()) > before or child.exit_code() is not None,
        f"pi on the terminal of {session}",
        PI_DEADLINE_S,
    )
    assert child.exit_code() is None, child.output()

    return child, terminal


async def test_ct_tui_attaches_to_an_owui_session(tui: TuiStack) -> None:
    """Invariant 3. One session, two doors, and the second is a terminal.

    Contract 02 §7.3 rule 5: the lease of the chat is idle, so the terminal
    takes it with no `--force` and no wait. Contract 02 §5.11: the door has
    the pi process of the playpen released, or the launcher exits 8.
    """
    session = await tui.chat_session()

    child, terminal = await attach(tui, session)
    writer = await tui.writer(session)
    (call,) = tui.terminal_calls()
    terminal.type(EOF)
    code = child.wait(EXIT_DEADLINE_S)

    # Contract 03 §7.6: the command, word for word. Contract 05 §4.1.1 rule 3:
    # the env file is the one that the status document names.
    assert call.argv == (
        "exec",
        "-it",
        "--env-file",
        str(tui.tree.playpen_env()),
        SANDBOX,
        "--",
        "node",
        str(launch_bundle()),
        ARG_SANDBOX,
        SANDBOX,
        ARG_SESSION,
        session,
    )
    assert writer is not None
    assert writer["holder"] == HOLDER
    # Contract 02 §7.1: one instance for each terminal, `tui.<pid>`.
    assert writer["door_instance"] == instance_of(child)
    assert writer["turn"] is None
    assert code == 0, child.output()
    assert tui.lease_changes(session) == [
        ("owui", GRANTED),
        (HOLDER, TAKEN_OVER),
        (HOLDER, RELEASED),
    ]
    assert await tui.writer(session) is None


async def test_cv_launcher_runs_the_pool_line_without_rpc(tui: TuiStack) -> None:
    """Contract 03 §7.6 rule 1: the command of the playpen, minus `--mode rpc`."""
    session = await tui.chat_session()
    (pool,) = calls_of(tui.tree, PI)

    child, terminal = await attach(tui, session)
    (on_terminal,) = tui.terminal_pi_starts()
    terminal.type(EOF)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert pool.argv[:2] == RPC_MODE
    assert on_terminal.argv == pool.argv[2:]


async def test_ct_a_second_terminal_is_refused(tui: TuiStack) -> None:
    """Contract 02 §7.2. Contention refuses: no queue, and no lease is taken away."""
    session = await tui.chat_session()
    first, first_terminal = await attach(tui, session)

    second, _ = tui.open_terminal(FAMILY, ARG_SESSION, session)
    refused = second.wait(EXIT_DEADLINE_S)
    writer = await tui.writer(session)
    first_terminal.type(EOF)

    assert refused != 0
    assert len(tui.terminal_calls()) == 1, "the second terminal ran sbx"
    assert writer is not None
    assert writer["door_instance"] == instance_of(first)
    assert first.wait(EXIT_DEADLINE_S) == 0, first.output()


async def test_force_takes_an_idle_lease_from_another_terminal(tui: TuiStack) -> None:
    """Contract 02 §7.3 rule 6. `--force` is a word that the operator types.

    It takes an idle lease from another instance of the same door, and it
    is the one thing that does: the scenario before this one is the same
    command with no `--force`. The takeover ends no pi process.

    The scenario stops at the lease. A `tui` lease that ends makes
    `attendance` read the session through a new pi process of the playpen
    (§10.5), and the second launcher can find that process (`AGENTS.md`,
    Known gaps). So the scenario does not need the second pi to start.
    """
    session = await tui.chat_session()
    first, first_terminal = await attach(tui, session)
    (first_pi,) = tui.terminal_pi_starts()

    second, second_terminal = tui.open_terminal(FAMILY, ARG_SESSION, session, FORCE_FLAG)
    await until(
        lambda: tui.lease_changes(session).count((HOLDER, TAKEN_OVER)) == 2,
        "the second terminal to take the lease",
    )
    first_pi_runs = pid_is_alive(first_pi.pid)
    _type_if_open(second_terminal, EOF)
    second.wait(EXIT_DEADLINE_S)
    first_terminal.type(EOF)

    assert first_pi_runs, "the takeover ended the pi process of the first terminal"
    assert first.wait(EXIT_DEADLINE_S) == 0, first.output()
    assert tui.lease_changes(session)[:3] == [
        ("owui", GRANTED),
        (HOLDER, TAKEN_OVER),
        (HOLDER, TAKEN_OVER),
    ]
    assert await tui.writer(session) is None


async def test_ct_a_new_session_exists_before_pi_runs(tui: TuiStack) -> None:
    """`attendance` makes the session before the door runs `sbx`.

    The launcher gets `--new`: this terminal is the first writer of the
    session (contract 03 §7.6). The scenario does not read the exit code,
    and it does not need pi to start (the head of this file says why).
    """
    tree = tui.tree
    child, terminal = tui.open_terminal(FAMILY, ARG_NEW, "--title", TITLE)

    await until(lambda: tui.terminal_calls() != [], "the door to run sbx", PI_DEADLINE_S)
    (call,) = tui.terminal_calls()
    session = call.value_after(ARG_SESSION)
    held = tree.session_dir(session).is_dir()
    _type_if_open(terminal, EOF)
    child.wait(EXIT_DEADLINE_S)

    found = await tui.session(session)

    assert session.startswith(TUI_PREFIX)
    assert ULID.fullmatch(session.removeprefix(TUI_PREFIX))
    assert held, "the session was not on disk when the door ran sbx"
    assert call.argv[-1] == ARG_NEW
    assert tree.journal_kinds(session)[0] == "session_created"
    assert found is not None
    assert found["title"] == TITLE
    assert tui.lease_changes(session)[0] == (HOLDER, GRANTED)
    assert tui.lease_changes(session)[-1] == (HOLDER, RELEASED)


async def test_the_picker_attaches_to_the_chosen_session(tui: TuiStack) -> None:
    """`agent-tui chat` lists the sessions of the family. Number 1 is the first row."""
    session = await tui.chat_session()

    child, terminal = tui.open_terminal(FAMILY)
    terminal.type("1" + ENTER)
    await until(
        lambda: tui.terminal_pi_starts() != [] or child.exit_code() is not None,
        "pi on the terminal",
        PI_DEADLINE_S,
    )
    (call,) = tui.terminal_calls()
    terminal.type(EOF)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert call.value_after(ARG_SESSION) == session
    assert ARG_NEW not in call.argv


async def test_no_choice_at_the_picker_starts_nothing(tui: TuiStack) -> None:
    """The end of the input at the list is an answer: nothing was chosen."""
    session = await tui.chat_session()

    child, terminal = tui.open_terminal(FAMILY)
    terminal.type(EOF)

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert tui.terminal_calls() == []
    assert tui.lease_changes(session) == [("owui", GRANTED)]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
async def test_a_signal_gives_the_lease_back_and_leaves_pi(
    tui: TuiStack, signum: signal.Signals
) -> None:
    """The door gives the lease back at the signal. It does not end pi.

    The signal goes to the door alone, as `kill` from another shell sends
    it. The session is writable at once (contract 02 §5.10), and the door
    still reports the exit code of pi when pi ends.
    """
    session = await tui.chat_session()
    child, terminal = await attach(tui, session)
    (pi,) = tui.terminal_pi_starts()

    child.send(signum)
    await until(lambda: (HOLDER, RELEASED) in tui.lease_changes(session), "the lease to go back")
    alive = pid_is_alive(pi.pid)
    door_runs = child.exit_code() is None
    terminal.type(EOF)

    assert alive, "the signal to the door ended pi"
    assert door_runs, "the door ended before pi did"
    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert await tui.writer(session) is None


async def test_a_closed_terminal_gives_the_lease_back(tui: TuiStack) -> None:
    """A closed window. The system sends SIGHUP to the door, the leader of the session.

    What pi gets differs by system. Linux sends SIGHUP to the foreground
    group too. macOS sends it to the leader alone. So the scenario reads
    the lease, and the teardown ends what still runs.
    """
    session = await tui.chat_session()
    _, terminal = await attach(tui, session)

    terminal.hang_up()
    await until(lambda: (HOLDER, RELEASED) in tui.lease_changes(session), "the lease to go back")

    assert await tui.writer(session) is None


async def test_the_interrupt_key_gives_the_lease_back(tui: TuiStack) -> None:
    """Ctrl-C. The terminal sends SIGINT to the door, to the launcher and to pi.

    The pi stand-in ends at the signal. The real pi reads the key itself.
    In each case the door gives the lease back and ends after pi.
    """
    session = await tui.chat_session()
    child, terminal = await attach(tui, session)
    (pi,) = tui.terminal_pi_starts()

    terminal.type(INTERRUPT)
    child.wait(EXIT_DEADLINE_S)

    assert (HOLDER, RELEASED) in tui.lease_changes(session)
    assert await tui.writer(session) is None
    assert pids_gone_by([pi.pid], time.monotonic() + GONE_DEADLINE_S) == []


async def test_the_door_passes_the_exit_code_of_the_launcher(tui: TuiStack) -> None:
    """Contract 03 §7.6 rule 7: each refusal of the launcher has its own code.

    The credential file is gone, so the launcher exits 10. The door exits
    with that code, and it gives the lease back.
    """
    session = await tui.chat_session()
    (tui.tree.mounts().creds / "creds.json").unlink()

    child, _ = tui.open_terminal(FAMILY, ARG_SESSION, session)
    code = child.wait(EXIT_DEADLINE_S)

    assert code == EXIT_NO_CREDENTIALS, child.output()
    assert tui.terminal_pi_starts() == []
    assert tui.lease_changes(session)[-1] == (HOLDER, RELEASED)


async def test_a_terminal_exchange_reaches_the_journal(tui: TuiStack) -> None:
    """Contract 02 §10.5. `attendance` reads what the terminal wrote when the lease ends.

    The operator types one prompt at pi and waits for the answer. The host
    sees no turn. When the door gives the lease back, `attendance` asks the
    playpen for the new entries and writes one `terminal_exchange` line.
    """
    tree = tui.tree
    session = await tui.chat_session()
    child, terminal = await attach(tui, session)

    terminal.type(PROMPT_COMMAND + ENTER)
    terminal.wait_for(SETTLED_EVENT)
    terminal.type(EOF)
    code = child.wait(EXIT_DEADLINE_S)
    await until(
        lambda: TERMINAL_EXCHANGE in tree.journal_kinds(session),
        f"the {TERMINAL_EXCHANGE} line of {session}",
    )
    (line,) = [one for one in tree.journal_lines(session) if one["kind"] == TERMINAL_EXCHANGE]
    found = await tui.session(session)

    assert code == 0, child.output()
    assert line["turn"] is None
    assert line["body"]["prompt"] == TERMINAL_PROMPT
    assert line["body"]["answer"].startswith(TERMINAL_PROMPT)
    assert found is not None
    # Contract 02 §4.2: the turn of the chat, and the exchange of the terminal.
    assert found["turns_total"] == 2
    assert found["terminal_total"] == 1


def _type_if_open(terminal: Terminal, text: str) -> None:
    """Type at a terminal that the door can have left already."""
    try:
        terminal.type(text)
    except OSError:
        return
