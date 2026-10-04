"""Stand-ins, as programs on disk. Never an object inside this process.

A service in any language starts a stand-in the way it starts the real
program: by name through its `PATH`, by the path that a variable of the
sandbox image names, or by an address in an argument. So a stand-in here is
an executable file under the test's root, and the only thing a test reads
back is what that program left on disk.

Four stand-ins exist, and none is under test:

`sbx`
    Two forms. The first is `integration/tests/fake_sbx.py`, unchanged,
    behind a wrapper: it knows `exec` and nothing else. The second is
    `standin_sbx.py`, with each verb that `caregiver` runs. The wrapper of
    each form records the call. It also sets the variables that stand for
    what the sandbox image supplies, so no service holds them in its own
    environment.
`pi`
    `playpen/test/fake-pi.mjs`, the playpen's own double, behind a wrapper.
    The playpen reaches it through `AGENT_PI_BIN`.
`systemctl`
    `standin_systemctl.py`, behind a wrapper. `caregiver` finds it through
    `PATH`.
the LiteLLM key API
    `standin_litellm.py`. It listens on a loopback port, so the supervisor
    of the test starts it, and no wrapper exists for it.

Each wrapper ends in `exec`. The stand-in stays one process, so stdin, stdout
and every signal reach it unchanged, and the recorded pid is the pid of the
program that runs.

One call through a wrapper leaves two things in the recorder's directory:

    <pid>.argv   one argument per line, written whole by rename
    order        one pid per line, in call order

A stand-in with state keeps it in `<name>-state` beside that directory. The
docstring of each program lists its files.
"""

from __future__ import annotations

import json
import shlex
import stat
import sys
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, unquote

from proc_harness import (
    Child,
    ProcError,
    Supervisor,
    TcpAddress,
    kill_pid,
    pids_gone_by,
    port_is_free,
    session_id_of,
)
from proc_tree import Tree, repo_root

SBX: Final = "sbx"
PI: Final = "pi"
SYSTEMCTL: Final = "systemctl"
LITELLM: Final = "litellm"

#: Each stand-in that a wrapper starts. The teardown waits for each recorded
#: process of these.
_WRAPPED: Final = (SBX, PI, SYSTEMCTL)

#: The two kinds of policy row that the `sbx` stand-in keeps.
ALLOW: Final = "allow"
DENY: Final = "deny"

#: An obvious fixture, never a credential. The LiteLLM stand-in takes it as
#: the master key, and `caregiver` gets it in `LITELLM_MASTER_KEY`.
MASTER_KEY: Final = "FIXTURE-LITELLM-MASTER-KEY"

#: The lock numbers of contract 03 §11.1 and §11.4, made small so a killed
#: playpen costs a test under one second. `LOCK_STALE_S` is four beat
#: windows: the ratio the contract fixes.
LOCK_BEAT_MS: Final = 150
LOCK_STALE_S: Final = 0.6
LOCK_POLL_S: Final = 0.05

#: The variables that stand for what the sandbox image supplies. `fake_sbx.py`
#: forwards these by name and forwards nothing else of its own environment.
_IMAGE_SEAMS: Final = ("AGENT_PI_BIN", "AGENT_LOCK_BEAT_MS")

#: How many ports one start of a stand-in may try.
_BIND_ATTEMPTS: Final = 3

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


def standin_script(name: str) -> Path:
    """A stand-in program of this directory. Each has its own docstring."""
    return Path(__file__).resolve().with_name(f"standin_{name}.py")


def install_sbx(tree: Tree) -> None:
    """Put `sbx` in the root's `bin`, the first directory of a service's PATH.

    This form knows `exec` only. It starts a playpen for each sandbox id, so
    a topology with no `caregiver` needs no `create`.
    """
    _install_sbx(tree, [str(fake_sbx_script())])


def install_sbx_verbs(tree: Tree) -> None:
    """Put the `sbx` with each verb of `caregiver` in the root's `bin`.

    `exec` starts a playpen only for a sandbox that `create` made.
    """
    state_dir(tree, SBX).mkdir(parents=True, exist_ok=True)
    _install_sbx(tree, [str(standin_script(SBX)), str(state_dir(tree, SBX))])


def _install_sbx(tree: Tree, program: list[str]) -> None:
    seams = {
        "AGENT_PI_BIN": str(_program(tree, PI)),
        "AGENT_LOCK_BEAT_MS": str(LOCK_BEAT_MS),
        "FAKE_SBX_IMAGE_ENV": ",".join(_IMAGE_SEAMS),
    }
    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in seams.items())
    target = shlex.join([sys.executable, *program])

    _write_program(tree, SBX, f'{_recorder(tree, SBX)}{exports}exec {target} "$@"\n')


def install_systemctl(tree: Tree) -> None:
    """Put `systemctl` in the root's `bin`. `caregiver` finds it through PATH."""
    state_dir(tree, SYSTEMCTL).mkdir(parents=True, exist_ok=True)
    words = [sys.executable, str(standin_script(SYSTEMCTL)), str(state_dir(tree, SYSTEMCTL))]

    _write_program(tree, SYSTEMCTL, f'{_recorder(tree, SYSTEMCTL)}exec {shlex.join(words)} "$@"\n')


def start_litellm(tree: Tree, supervisor: Supervisor, env: Mapping[str, str]) -> tuple[Child, int]:
    """Start the LiteLLM stand-in on a free loopback port, and wait for it.

    A start that fails because another program took the port first is tried
    again on another port. Any other failed start is an error.
    """
    state = state_dir(tree, LITELLM)
    state.mkdir(parents=True, exist_ok=True)
    (state / "master.key").write_text(MASTER_KEY + "\n", encoding="utf-8")
    program = [sys.executable, str(standin_script(LITELLM)), str(state)]

    for _ in range(_BIND_ATTEMPTS):
        port = supervisor.free_port()
        child = supervisor.spawn(LITELLM, [*program, str(port)], env, tree.root)

        try:
            supervisor.wait_ready(child, TcpAddress(port))
        except ProcError:
            if child.exit_code() is None or port_is_free(port):
                raise

            continue

        return child, port

    raise ProcError(f"the LiteLLM stand-in found no free port in {_BIND_ATTEMPTS} attempts")


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
    """The pid of every stand-in process that a wrapper started in this test."""
    return [call.pid for name in _WRAPPED for call in calls_of(tree, name)]


def state_dir(tree: Tree, name: str) -> Path:
    """Where one stand-in keeps its state."""
    return tree.standins / f"{name}-state"


def tune(tree: Tree, name: str, what: str, text: str = "") -> None:
    """Change what one stand-in does at its next call.

    `what` is a path under the `tune` directory of the stand-in. The
    docstring of each program lists the paths that it reads.
    """
    path = state_dir(tree, name) / "tune" / what
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def untune(tree: Tree, name: str, what: str) -> None:
    """Take one tuning away. The stand-in does the default thing again."""
    (state_dir(tree, name) / "tune" / what).unlink(missing_ok=True)


def sbx_sandboxes(tree: Tree) -> dict[str, dict[str, Any]]:
    """Each sandbox that `sbx create` made and no `sbx rm` removed, by id."""
    found: dict[str, dict[str, Any]] = {}

    for path in sorted((state_dir(tree, SBX) / "vms").glob("*.json")):
        try:
            found[unquote(path.stem)] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue

    return found


def sbx_rows(tree: Tree, sandbox: str, kind: str) -> list[str]:
    """The hosts with a policy row of one kind for one sandbox, sorted."""
    directory = state_dir(tree, SBX) / "policy" / quote(sandbox, safe="") / kind

    if not directory.is_dir():
        return []

    return sorted(unquote(path.name) for path in directory.iterdir())


def litellm_calls(tree: Tree) -> list[dict[str, Any]]:
    """Each request that the LiteLLM stand-in got, in arrival order."""
    try:
        raw = (state_dir(tree, LITELLM) / "calls.jsonl").read_bytes()
    except FileNotFoundError:
        return []

    return [json.loads(line) for line in raw.split(b"\n")[:-1] if line.strip()]


def litellm_keys(tree: Tree) -> dict[str, dict[str, Any]]:
    """Each alias that holds a key at the LiteLLM stand-in now."""
    try:
        keys: dict[str, dict[str, Any]] = json.loads(
            (state_dir(tree, LITELLM) / "keys.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}

    return keys


def enabled_units(tree: Tree) -> list[str]:
    """Each unit that the `systemctl` stand-in holds as enabled, sorted."""
    directory = state_dir(tree, SYSTEMCTL) / "enabled"

    if not directory.is_dir():
        return []

    return sorted(path.name for path in directory.iterdir())


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
