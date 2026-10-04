"""The pseudo-terminal of the harness, with `sh` in the place of a program.

A scenario of the terminal door reads a lease and an exit code. It can do
that only when the terminal does what a real one does: it carries the keys,
it sends the signals, and it keeps what the program wrote. Each test here
holds one of those.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest
from proc_harness import ProcError, Supervisor
from proc_terminal import ENTER, EOF, INTERRUPT, TerminalError
from proc_tree import Tree

SH = "/bin/sh"
BASE_ENV = {"PATH": os.defpath}
EXIT_DEADLINE_S = 10.0
SHORT_DEADLINE_S = 0.2

#: A loop that a signal ends between two short sleeps.
SPIN = "while :; do sleep 0.05; done"

#: A program that opens `/dev/tty`, and that takes no terminal by itself.
#: `/bin/sh` on macOS is bash, and bash takes the terminal of its stdin.
OPENS_TTY = "import os; os.close(os.open('/dev/tty', os.O_RDWR)); print('it-controls')"


def test_a_command_on_a_terminal_has_it_on_each_stream(tree: Tree, supervisor: Supervisor) -> None:
    script = "test -t 0 && test -t 1 && test -t 2 && echo each-stream"

    child, terminal = supervisor.spawn_on_terminal("tty", [SH, "-c", script], BASE_ENV, tree.root)

    assert child.wait(EXIT_DEADLINE_S) == 0
    terminal.wait_for("each-stream")


def test_the_terminal_is_the_controlling_terminal(tree: Tree, supervisor: Supervisor) -> None:
    """`/dev/tty` opens only for a process that has a controlling terminal."""
    words = [sys.executable, "-c", OPENS_TTY]

    child, terminal = supervisor.spawn_on_terminal("tty", words, BASE_ENV, tree.root)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    terminal.wait_for("it-controls")


def test_a_command_on_a_terminal_leads_its_own_group(tree: Tree, supervisor: Supervisor) -> None:
    script = "read line"

    child, _ = supervisor.spawn_on_terminal("idle", [SH, "-c", script], BASE_ENV, tree.root)

    assert os.getpgid(child.popen.pid) == child.popen.pid
    assert os.getsid(child.popen.pid) == child.popen.pid
    assert child.pgid != os.getpgrp()


def test_the_command_reads_what_the_test_types(tree: Tree, supervisor: Supervisor) -> None:
    script = 'read line; echo "it read: $line"'
    child, terminal = supervisor.spawn_on_terminal("read", [SH, "-c", script], BASE_ENV, tree.root)

    terminal.type("typed words" + ENTER)

    assert child.wait(EXIT_DEADLINE_S) == 0
    terminal.wait_for("it read: typed words")


def test_the_eof_key_ends_the_input(tree: Tree, supervisor: Supervisor) -> None:
    script = "cat > /dev/null; echo input-ended"
    child, terminal = supervisor.spawn_on_terminal("cat", [SH, "-c", script], BASE_ENV, tree.root)

    terminal.type(EOF)

    assert child.wait(EXIT_DEADLINE_S) == 0
    terminal.wait_for("input-ended")


def test_the_interrupt_key_sends_sigint_to_the_group(tree: Tree, supervisor: Supervisor) -> None:
    """The terminal sends the signal. The test sends one byte and no signal."""
    script = f'trap "exit 3" INT; echo trap-is-set; {SPIN}'
    child, terminal = supervisor.spawn_on_terminal("spin", [SH, "-c", script], BASE_ENV, tree.root)
    terminal.wait_for("trap-is-set")

    terminal.type(INTERRUPT)

    assert child.wait(EXIT_DEADLINE_S) == 3


def test_a_hang_up_sends_sighup_to_the_leader(tree: Tree, supervisor: Supervisor) -> None:
    """A closed window. The leader of the session gets SIGHUP from the system."""
    mark = tree.root / "got-sighup"
    script = f'trap ": > {mark}; exit 4" HUP; echo trap-is-set; {SPIN}'
    child, terminal = supervisor.spawn_on_terminal("spin", [SH, "-c", script], BASE_ENV, tree.root)
    terminal.wait_for("trap-is-set")

    terminal.hang_up()

    assert child.wait(EXIT_DEADLINE_S) == 4
    assert mark.exists()


def test_the_report_of_a_child_holds_what_the_terminal_showed(
    tree: Tree, supervisor: Supervisor
) -> None:
    child, terminal = supervisor.spawn_on_terminal(
        "says", [SH, "-c", "echo the-evidence"], BASE_ENV, tree.root
    )

    assert child.wait(EXIT_DEADLINE_S) == 0
    terminal.wait_for("the-evidence")
    assert "--- says terminal ---\nthe-evidence" in child.output()


def test_a_wait_for_text_that_never_comes_says_what_came(
    tree: Tree, supervisor: Supervisor
) -> None:
    script = f"echo something-else; {SPIN}"
    _, terminal = supervisor.spawn_on_terminal("spin", [SH, "-c", script], BASE_ENV, tree.root)
    terminal.wait_for("something-else")

    with pytest.raises(TerminalError, match="did not show 'never-shown'") as caught:
        terminal.wait_for("never-shown", SHORT_DEADLINE_S)

    assert "something-else" in str(caught.value)


def test_a_wait_ends_when_the_terminal_closed(tree: Tree, supervisor: Supervisor) -> None:
    """Fail at once. A test must not wait 30 s for a program that ended."""
    child, terminal = supervisor.spawn_on_terminal(
        "ends", [SH, "-c", "echo last-words"], BASE_ENV, tree.root
    )
    assert child.wait(EXIT_DEADLINE_S) == 0

    with pytest.raises(TerminalError, match="closed and never showed 'never-shown'"):
        terminal.wait_for("never-shown")


def test_the_teardown_ends_a_command_on_a_terminal(tree: Tree) -> None:
    stays = Supervisor(tree.proc_logs)
    child, terminal = stays.spawn_on_terminal("stays", [SH, "-c", SPIN], BASE_ENV, tree.root)

    assert stays.stop_all() == []
    assert child.exit_code() == -signal.SIGTERM

    with pytest.raises(TerminalError, match="the terminal is closed"):
        terminal.type("too late")


def test_a_command_that_is_no_program_is_an_error(tree: Tree, supervisor: Supervisor) -> None:
    missing = Path(tree.root / "no-such-program")

    child, terminal = supervisor.spawn_on_terminal("missing", [str(missing)], BASE_ENV, tree.root)

    assert child.wait(EXIT_DEADLINE_S) != 0
    terminal.wait_for("cannot run")


def test_a_terminal_that_cannot_start_is_an_error(tree: Tree, supervisor: Supervisor) -> None:
    with pytest.raises(ProcError, match="cannot start nowhere"):
        supervisor.spawn_on_terminal("nowhere", [SH], BASE_ENV, tree.root / "no-such-directory")
