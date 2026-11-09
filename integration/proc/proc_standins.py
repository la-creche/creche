"""Stand-ins, as programs on disk. Never an object inside this process.

A service in any language starts a stand-in the way it starts the real
program: by name through its `PATH`, by the path that a variable of the
sandbox image names, or by an address in an argument. So a stand-in here is
an executable file under the test's root, and the only thing a test reads
back is what that program left on disk.

Seven stand-ins exist, and none is under test:

the vmm host API
    `standin_vmm.py`. It listens on `vmm.sock` of the root, so the
    supervisor of the test starts it, and no wrapper exists for it.
`systemctl`
    `standin_systemctl.py`, behind a wrapper. `caregiver` finds it through
    `PATH`.
`chaperone check`
    `standin_chaperone.py`, behind a wrapper. `caregiver` runs it by the
    path of its `--chaperone-bin`, and not through `PATH`.
the LiteLLM key API
    `standin_litellm.py`. It listens on a loopback port, so the supervisor
    of the test starts it, and no wrapper exists for it.
the embedding service
    `standin_tei.py`. It listens on a loopback port too, and the index
    builder finds it through an address in a variable.
the upstream and the approval channel of the Rust chaperone
    `standin_upstream.py` and `standin_approval.py`, each behind a wrapper.
    The chaperone starts each one as an effect plugin, by the absolute path
    of its wrapper in the `command` of its manifest.

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

import standin_approval
import standin_tei
import standin_upstream
import standin_vmm
from proc_harness import (
    Child,
    ProcError,
    Supervisor,
    TcpAddress,
    UnixAddress,
    kill_pid,
    pids_gone_by,
    port_is_free,
    session_id_of,
)
from proc_tree import Tree

SYSTEMCTL: Final = "systemctl"
#: The program `caregiver` runs as `--chaperone-bin`.
CHAPERONE_CHECK: Final = "chaperone-check"
#: The file name of the stand-in program of `chaperone check`.
CHAPERONE: Final = "chaperone"
LITELLM: Final = "litellm"
VMM: Final = "vmm"
TEI: Final = "tei"
UPSTREAM: Final = "upstream"
APPROVAL: Final = "approval"

#: Each stand-in that a wrapper starts. The teardown waits for each recorded
#: process of these.
_WRAPPED: Final = (SYSTEMCTL, UPSTREAM, APPROVAL)

#: The tunings of the TEI stand-in. The program holds each name, and its
#: docstring says what each one changes.
TEI_MODEL: Final = standin_tei.TUNE_MODEL
TEI_NO_MODEL_ID: Final = standin_tei.TUNE_NO_MODEL_ID
TEI_DIMS: Final = standin_tei.TUNE_DIMS
TEI_FAIL_INFO: Final = standin_tei.TUNE_FAIL_INFO
TEI_FAIL_EMBED: Final = standin_tei.TUNE_FAIL_EMBED
TEI_HOLD_EMBED: Final = standin_tei.TUNE_HOLD_EMBED


#: The two answers of the approval stand-in that a scenario writes.
APPROVED: Final = '{"answer":"approved"}'
DENIED: Final = '{"answer":"denied","reason":"the test denies it"}'

#: An obvious fixture, never a credential. The LiteLLM stand-in takes it as
#: the master key, and `caregiver` gets it in `LITELLM_MASTER_KEY`.
MASTER_KEY: Final = "FIXTURE-LITELLM-MASTER-KEY"

#: How many ports one start of a stand-in may try.
_BIND_ATTEMPTS: Final = 3

#: How long a stand-in process has to end after its service ended.
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


def standin_script(name: str) -> Path:
    """A stand-in program of this directory. Each has its own docstring."""
    return Path(__file__).resolve().with_name(f"standin_{name}.py")


def install_systemctl(tree: Tree) -> None:
    """Put `systemctl` in the root's `bin`. `caregiver` finds it through PATH."""
    state_dir(tree, SYSTEMCTL).mkdir(parents=True, exist_ok=True)
    words = [sys.executable, str(standin_script(SYSTEMCTL)), str(state_dir(tree, SYSTEMCTL))]

    _write_program(tree, SYSTEMCTL, f'{_recorder(tree, SYSTEMCTL)}exec {shlex.join(words)} "$@"\n')


def install_chaperone_check(tree: Tree) -> Path:
    """Put the stand-in of `chaperone check` in the root's `bin`, and an
    empty `install.toml` in the configuration directory of chaperone.
    Answers the path of the program, for `--chaperone-bin`."""
    tree.chaperone_config_dir.mkdir(parents=True, exist_ok=True)
    (tree.chaperone_config_dir / "install.toml").write_text("version = 1\n", encoding="utf-8")
    words = [sys.executable, str(standin_script(CHAPERONE))]
    _write_program(tree, CHAPERONE_CHECK, f'#!/bin/sh\nexec {shlex.join(words)} "$@"\n')

    return chaperone_check_program(tree)


def chaperone_check_program(tree: Tree) -> Path:
    """The path of the stand-in of `chaperone check`."""
    return _program(tree, CHAPERONE_CHECK)


def start_vmm(tree: Tree, supervisor: Supervisor, env: Mapping[str, str]) -> Child:
    """Start the vmm stand-in on `vmm.sock` of the root, and wait for it."""
    state = state_dir(tree, VMM)
    state.mkdir(parents=True, exist_ok=True)
    tree.vmm_socket.parent.mkdir(parents=True, exist_ok=True)
    program = [sys.executable, str(standin_script(VMM)), str(state), str(tree.vmm_socket)]
    child = supervisor.spawn(VMM, program, env, tree.root)
    supervisor.wait_ready(child, UnixAddress(tree.vmm_socket))

    return child


def vmm_calls(tree: Tree) -> list[dict[str, Any]]:
    """Each request that the vmm stand-in got, in arrival order: its route and its body."""
    try:
        raw = (state_dir(tree, VMM) / standin_vmm.CALLS_FILE).read_bytes()
    except FileNotFoundError:
        return []

    return [json.loads(line) for line in raw.split(b"\n")[:-1] if line.strip()]


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


def start_tei(tree: Tree, supervisor: Supervisor, env: Mapping[str, str]) -> tuple[Child, int]:
    """Start the TEI stand-in on a free loopback port, and wait for it.

    A start that fails because another program took the port first is tried
    again on another port. Any other failed start is an error.
    """
    state = state_dir(tree, TEI)
    state.mkdir(parents=True, exist_ok=True)
    program = [sys.executable, str(standin_script(TEI)), str(state)]

    for _ in range(_BIND_ATTEMPTS):
        port = supervisor.free_port()
        child = supervisor.spawn(TEI, [*program, str(port)], env, tree.root)

        try:
            supervisor.wait_ready(child, TcpAddress(port))
        except ProcError:
            if child.exit_code() is None or port_is_free(port):
                raise

            continue

        return child, port

    raise ProcError(f"the TEI stand-in found no free port in {_BIND_ATTEMPTS} attempts")


def install_effects(tree: Tree) -> None:
    """Write the wrappers of the upstream and of the approval channel into the root's `bin`.

    The manifest of each plugin names the absolute path that
    `effect_program` gives.
    """
    (state_dir(tree, APPROVAL) / standin_approval.ANSWERS_DIR).mkdir(parents=True, exist_ok=True)

    for name in (UPSTREAM, APPROVAL):
        state_dir(tree, name).mkdir(parents=True, exist_ok=True)
        words = [sys.executable, str(standin_script(name)), str(state_dir(tree, name))]
        _write_program(tree, name, f'{_recorder(tree, name)}exec {shlex.join(words)} "$@"\n')


def effect_program(tree: Tree, name: str) -> Path:
    """The wrapper of one effect stand-in: `UPSTREAM` or `APPROVAL`."""
    return _program(tree, name)


def answer_gate(tree: Tree, name: str, answer: str = APPROVED) -> None:
    """Answer a gate at the approval stand-in, by rename.

    `name` is the gate ID, or the exposed name of a tool. A file of a tool
    answers each gate of that tool, until `unanswer_gate` removes it.
    """
    path = state_dir(tree, APPROVAL) / standin_approval.ANSWERS_DIR / name
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(answer, encoding="utf-8")
    temp.replace(path)


def unanswer_gate(tree: Tree, name: str) -> None:
    (state_dir(tree, APPROVAL) / standin_approval.ANSWERS_DIR / name).unlink(missing_ok=True)


def upstream_calls(tree: Tree) -> list[dict[str, Any]]:
    """Each tool call that the upstream stand-in got: tool, arguments and `_meta`."""
    return _lines_of(state_dir(tree, UPSTREAM) / standin_upstream.CALLS_FILE)


def approval_requests(tree: Tree) -> list[dict[str, Any]]:
    """Each `approve` call that the approval stand-in holds or held, in arrival order."""
    return _lines_of(state_dir(tree, APPROVAL) / standin_approval.REQUESTS_FILE)


def _lines_of(path: Path) -> list[dict[str, Any]]:
    """The JSON lines of one record file. A line with no newline yet is not whole."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []

    return [json.loads(line) for line in raw.split(b"\n")[:-1] if line.strip()]


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


def tei_calls(tree: Tree) -> list[dict[str, Any]]:
    """Each request that the TEI stand-in got, in arrival order."""
    try:
        raw = (state_dir(tree, TEI) / standin_tei.CALLS_FILE).read_bytes()
    except FileNotFoundError:
        return []

    return [json.loads(line) for line in raw.split(b"\n")[:-1] if line.strip()]


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


def _write_program(tree: Tree, name: str, text: str) -> None:
    path = _program(tree, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | _EXECUTABLE)
