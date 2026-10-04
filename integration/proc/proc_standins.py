"""Stand-ins, as programs on disk. Never an object inside this process.

A service in any language starts a stand-in the way it starts the real
program: by name through its `PATH`, or by the path that a variable of the
sandbox image names. So a stand-in here is an executable file under the
test's root, and the only thing a test reads back is what that program
recorded on disk.

Two stand-ins exist, and neither is under test:

`sbx`
    `integration/tests/fake_sbx.py`, unchanged, behind a wrapper. The wrapper
    records the call. It also sets the variables that stand for what the
    sandbox image supplies, so no service holds them in its own environment.
    It drops `-it` after the record: the real `sbx exec -it` gives the
    command the terminal of the caller, and a program that becomes the
    command keeps that terminal with no flag.
`pi`
    `playpen/test/fake-pi.mjs`, the playpen's own double, behind a wrapper.
    The playpen reaches it through `AGENT_PI_BIN`.

Each wrapper ends in `exec`. The stand-in stays one process, so stdin, stdout
and every signal reach it unchanged, and the recorded pid is the pid of the
program that runs.

One call leaves two things in the recorder's directory:

    <pid>.argv   one argument per line, written whole by rename
    order        one pid per line, in call order
"""

from __future__ import annotations

import shlex
import stat
import sys
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from proc_harness import kill_pid, pids_gone_by, session_id_of
from proc_tree import Tree, repo_root

SBX: Final = "sbx"
PI: Final = "pi"

#: The lock numbers of contract 03 §11.1 and §11.4, made small so a killed
#: playpen costs a test under one second. `LOCK_STALE_S` is four beat
#: windows: the ratio the contract fixes.
LOCK_BEAT_MS: Final = 150
LOCK_STALE_S: Final = 0.6
LOCK_POLL_S: Final = 0.05

#: Contract 03 §7.1 rule 6: where the sessions of the family are in the
#: sandbox. On the host the path in the sandbox is the path on the host, and
#: nothing sets the variable. The launcher of the terminal door reads it
#: (contract 03 §7.6 rule 4). The playpen does not: `attendance` tells it.
SESSIONS_MOUNT_ENV: Final = "SESSIOND_SANDBOX_SESSIONS_MOUNT"

#: The variables that stand for what the sandbox image supplies. `fake_sbx.py`
#: forwards these by name and forwards nothing else of its own environment.
_IMAGE_SEAMS: Final = ("AGENT_PI_BIN", "AGENT_LOCK_BEAT_MS", SESSIONS_MOUNT_ENV)

#: What `sbx exec` takes to give the command the terminal of the caller.
TERMINAL_FLAG: Final = "-it"

#: The word that ends the words of `sbx` and starts the command.
_COMMAND_SEPARATOR: Final = "--"

#: How long a stand-in process has to end after its service ended. The
#: playpen exits when its stdin closes, and pi exits when the playpen does.
_EXIT_DEADLINE_S: Final = 10.0

_ORDER_FILE: Final = "order"
_ARGV_SUFFIX: Final = ".argv"
_EXECUTABLE: Final = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


@dataclass(frozen=True, slots=True)
class Call:
    """One start of a stand-in: the pid it ran as, and its arguments."""

    pid: int
    argv: tuple[str, ...]

    def value_after(self, flag: str) -> str:
        """The argument that follows `flag`. Raises when the flag is absent."""
        return self.argv[self.argv.index(flag) + 1]


def fake_sbx_script() -> Path:
    """The stand-in for `sbx exec --env-file`. It has its own docstring."""
    return repo_root() / "integration" / "tests" / "fake_sbx.py"


def fake_pi_script() -> Path:
    """The double for `pi --mode rpc` that belongs to `playpen/`."""
    return repo_root() / "playpen" / "test" / "fake-pi.mjs"


def install_sbx(tree: Tree) -> None:
    """Put `sbx` in the root's `bin`, the first directory of a service's PATH."""
    seams = {
        "AGENT_PI_BIN": str(_program(tree, PI)),
        "AGENT_LOCK_BEAT_MS": str(LOCK_BEAT_MS),
        "FAKE_SBX_IMAGE_ENV": ",".join(_IMAGE_SEAMS),
    }
    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in seams.items())
    target = shlex.join([sys.executable, str(fake_sbx_script())])
    head = f"{_recorder(tree, SBX)}{exports}{_sandbox_facts(tree)}"

    _write_program(tree, SBX, f'{head}exec {target} "$@"\n')


def install_pi(tree: Tree) -> None:
    """Write the program `AGENT_PI_BIN` names.

    It reads `pi-env.sh` at every start, because the playpen passes a fixed
    set of names to pi and no `FAKE_PI_*` name is in it. `set_pi_env`
    rewrites that file.
    """
    set_pi_env(tree)
    env_file = shlex.quote(str(_pi_env_file(tree)))
    target = shlex.join(["node", str(fake_pi_script())])

    _write_program(tree, PI, f'{_recorder(tree, PI)}. {env_file}\nexec {target} "$@"\n')


def set_pi_env(tree: Tree, **values: int) -> None:
    """Tune the fake pi for the next process that starts.

    `events` is the delta count and `delay_ms` is the gap between two
    deltas. A scenario that acts during a turn makes the turn long first.
    """
    lines = [f"export FAKE_PI_{name.upper()}={number}" for name, number in values.items()]
    temp = _pi_env_file(tree).with_suffix(".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(_pi_env_file(tree))


def calls_of(tree: Tree, name: str) -> list[Call]:
    """Every start of one stand-in, in call order."""
    directory = _record_dir(tree, name)

    try:
        order = (directory / _ORDER_FILE).read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return []

    return [
        Call(pid=int(pid), argv=_read_argv(directory / f"{pid}{_ARGV_SUFFIX}")) for pid in order
    ]


def recorded_pids(tree: Tree) -> list[int]:
    """The pid of every stand-in process this test started."""
    return [call.pid for name in (SBX, PI) for call in calls_of(tree, name)]


def end_standins(
    tree: Tree, sessions: Collection[int], deadline_s: float = _EXIT_DEADLINE_S
) -> list[str]:
    """Wait for every stand-in process to end. Returns one line per problem.

    Called after the services of the test ended. `sessions` holds the session
    of each service. A stand-in that outlives its service is killed, and that
    is a problem to report: on the host it would be a process no unit owns.

    A pid is not a name. A stand-in that ended earlier in the test gave its
    pid back, and another program can hold it now. So only a pid in one of
    `sessions` is killed. A pid that runs in another session is reported and
    gets no signal.
    """
    left = pids_gone_by(recorded_pids(tree), time.monotonic() + deadline_s)
    problems: list[str] = []

    for pid in left:
        if session_id_of(pid) not in sessions:
            problems.append(
                f"the recorded pid {pid} names a process outside this test: no signal sent"
            )
            continue

        kill_pid(pid)
        problems.append(f"stand-in process {pid} outlived its service and was killed")

    return problems


def _recorder(tree: Tree, name: str) -> str:
    """The head of a wrapper: record this call, then go on.

    The argument file is complete before the pid reaches `order`, so a reader
    that finds a pid there finds its arguments too. One pid and a newline is
    one short `write`, which an append keeps whole.
    """
    directory = _record_dir(tree, name)
    directory.mkdir(parents=True, exist_ok=True)
    quoted = shlex.quote(str(directory))

    return (
        "#!/bin/sh\n"
        f"d={quoted}\n"
        'printf "%s\\n" "$@" > "$d/.$$.tmp"\n'
        f'mv "$d/.$$.tmp" "$d/$${_ARGV_SUFFIX}"\n'
        f'printf "%s\\n" "$$" >> "$d/{_ORDER_FILE}"\n'
    )


def _sandbox_facts(tree: Tree) -> str:
    """The part of the `sbx` wrapper that reads the words of one call.

    1. The sandbox id is the word before the command separator. Its family
       names the sessions mount (contract 03 §7.6 rule 4), so one wrapper
       serves each family of a root.
    2. `-it` before the separator is dropped. The stand-in becomes the
       command, so the command has the terminal of the caller already.
    """
    sessions = shlex.quote(str(tree.sessions_root))

    return (
        "box=\n"
        "count=$#\n"
        "past=\n"
        'while [ "$count" -gt 0 ]; do\n'
        "  word=$1\n"
        "  shift\n"
        "  count=$((count - 1))\n"
        f'  if [ -z "$past" ] && [ "$word" = "{TERMINAL_FLAG}" ]; then continue; fi\n'
        f'  if [ -z "$past" ] && [ "$word" = "{_COMMAND_SEPARATOR}" ]; then past=1; fi\n'
        '  if [ -z "$past" ]; then box=$word; fi\n'
        '  set -- "$@" "$word"\n'
        "done\n"
        f'export {SESSIONS_MOUNT_ENV}={sessions}/"${{box%-s*}}"\n'
    )


def _read_argv(path: Path) -> tuple[str, ...]:
    lines = path.read_text(encoding="utf-8").split("\n")[:-1]

    return () if lines == [""] else tuple(lines)


def _record_dir(tree: Tree, name: str) -> Path:
    return tree.standins / name


def _program(tree: Tree, name: str) -> Path:
    return tree.bin_dir / name


def _pi_env_file(tree: Tree) -> Path:
    return tree.standins / "pi-env.sh"


def _write_program(tree: Tree, name: str, text: str) -> None:
    path = _program(tree, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | _EXECUTABLE)
