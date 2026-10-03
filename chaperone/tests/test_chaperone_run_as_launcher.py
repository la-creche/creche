"""`chaperone-as`, the launcher every upstream starts through.

Contract 01b §9: each MCP server runs as its own unprivileged user,
`mcp-<name>`. The PEP spawns `chaperone-as mcp-<name> <command> <args>`,
and the launcher drops the child to that user before it execs the server.

A test cannot `setuid`, so the switch itself is replaced here and everything
around it is real: the argument checks, the environment handed to the
server, the order of the steps, and the refusals. The refusals run twice:
in process, where the test can see that no refusal ever reaches the switch
or the exec, and as the installed console script, run as the test's own
user against a user it cannot become.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import sys
import sysconfig
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final, NoReturn

import pytest
from chaperone.run_as import CHILD_ENV_VAR, REFUSED_EXIT, Refused, Target
from handover.executor.host import MCP_USER_RE
from handover.mcpserver import SERVER_NAME_RE

from chaperone import run_as

#: A user no host has. The pattern admits it, and nothing ever creates it,
#: so the refusal below reads the same on a Mac, on CI and on the host.
ABSENT: Final = "mcp-chaperone-test-absent"

COMMAND: Final = "/opt/mcp/kagi/bin/kagimcp"
ROW_ENV: Final = {"KAGI_API_KEY": "s3cret", "READ_ONLY_MODE": "true"}
FAKE_UID: Final = 64_001
FAKE_GID: Final = 64_002
PREFIX: Final = "chaperone-as: refused: "
USAGE: Final = "usage: chaperone-as mcp-<name> /absolute/command [args...]"

#: The console script, beside this interpreter, as the `chaperone` tree ships it.
SCRIPT: Final = Path(sysconfig.get_path("scripts")) / "chaperone-as"


class Exec(Exception):
    """What the fake exec raises instead of replacing the test process."""


class Seen:
    """The three steps a refusal must never reach, in the order they ran."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def lookup(self, user: str) -> pwd.struct_passwd:
        self.calls.append(("lookup", user))
        return pwd.struct_passwd((user, "x", FAKE_UID, FAKE_GID, "", "/nonexistent", "/nologin"))

    def become(self, target: Target) -> None:
        self.calls.append(("become", target))

    def execve(self, command: str, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
        self.calls.append(("exec", command, list(argv), dict(env)))
        raise Exec


def _environ(env: Mapping[str, str] = ROW_ENV) -> dict[str, str]:
    """What the launcher really inherits: the six variables the MCP SDK
    copies out of the PEP's own environment, plus the one the PEP sets."""
    return {
        "HOME": "/var/lib/creche-chaperone/home",
        "LOGNAME": "chaperone",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SHELL": "/usr/sbin/nologin",
        "USER": "chaperone",
        CHILD_ENV_VAR: json.dumps(dict(env)),
    }


def _launch(argv: list[str], environ: Mapping[str, str], seen: Seen) -> int:
    return run_as.launch(argv, environ, lookup=seen.lookup, become=seen.become, execve=seen.execve)


@pytest.fixture(autouse=True)
def signals() -> Iterator[None]:
    """`launch` restores two signals for the server it execs. Give this
    process its own dispositions back afterwards.

    Autouse, because an exec that fails returns AFTER that restore
    (`test_an_exec_that_fails_is_a_refusal`). Without it SIGPIPE stays at
    SIG_DFL in the xdist worker, and the worker's next write to a closed
    socket kills it in an unrelated test.
    """
    saved = {one: signal.getsignal(one) for one in (signal.SIGPIPE, signal.SIGXFSZ)}
    yield
    for one, handler in saved.items():
        signal.signal(one, handler)


# -- the path that execs ----------------------------------------------------


def test_it_resolves_then_switches_then_execs_the_row_exactly() -> None:
    seen = Seen()

    with pytest.raises(Exec):
        _launch(["mcp-kagi", COMMAND, "--flag"], _environ(), seen)

    assert seen.calls == [
        ("lookup", "mcp-kagi"),
        ("become", Target("mcp-kagi", FAKE_UID, FAKE_GID)),
        ("exec", COMMAND, [COMMAND, "--flag"], ROW_ENV),
    ]


def test_nothing_of_the_launchers_own_environment_reaches_the_server() -> None:
    """HOME, PATH, USER and the rest are the PEP's. The server gets the
    row and nothing else, and a variable that would steer a loader stops
    at the launcher too."""
    seen = Seen()
    environ = _environ() | {"LD_PRELOAD": "/tmp/evil.so", "PYTHONPATH": "/tmp"}

    with pytest.raises(Exec):
        _launch(["mcp-kagi", COMMAND], environ, seen)

    assert seen.calls[-1] == ("exec", COMMAND, [COMMAND], ROW_ENV)


def test_an_empty_row_env_is_an_empty_environment() -> None:
    seen = Seen()

    with pytest.raises(Exec):
        _launch(["mcp-kagi", COMMAND], _environ({}), seen)

    assert seen.calls[-1] == ("exec", COMMAND, [COMMAND], {})


def test_the_server_starts_with_the_signals_python_ignores_restored() -> None:
    """The launcher is a Python process, and Python ignores SIGPIPE and
    SIGXFSZ. An ignored signal stays ignored across exec, so without this
    the server would start differently from the way `subprocess` starts it
    today (`restore_signals=True`)."""
    dispositions: list[object] = []

    def execve(command: str, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
        dispositions.extend(signal.getsignal(one) for one in (signal.SIGPIPE, signal.SIGXFSZ))
        raise Exec

    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    seen = Seen()

    with pytest.raises(Exec):
        run_as.launch(
            ["mcp-kagi", COMMAND], _environ(), lookup=seen.lookup, become=seen.become, execve=execve
        )

    assert dispositions == [signal.SIG_DFL, signal.SIG_DFL]


# -- the argument checks ----------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "line"),
    [
        ([], USAGE),
        (["mcp-kagi"], USAGE),
        (["mcp-kagi", ""], "the command is empty"),
        (["mcp-kagi", "kagimcp"], "'kagimcp' is not an absolute path"),
        (["root", COMMAND], "'root' is not an mcp-<name> user"),
        (["chaperone", COMMAND], "'chaperone' is not an mcp-<name> user"),
        (["mcp-", COMMAND], "'mcp-' is not an mcp-<name> user"),
        (["mcp-k", COMMAND], "'mcp-k' is not an mcp-<name> user"),
        (["mcp-Kagi", COMMAND], "'mcp-Kagi' is not an mcp-<name> user"),
        (["mcp_kagi", COMMAND], "'mcp_kagi' is not an mcp-<name> user"),
        (["mcp-kagi\n", COMMAND], "'mcp-kagi\\n' is not an mcp-<name> user"),
        (["mcp-../root", COMMAND], "'mcp-../root' is not an mcp-<name> user"),
        ([f"mcp-{'k' * 32}", COMMAND], f"'mcp-{'k' * 32}' is not an mcp-<name> user"),
    ],
)
def test_a_bad_argv_is_refused_before_anything_runs(
    argv: list[str], line: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = Seen()

    assert _launch(argv, _environ(), seen) == REFUSED_EXIT

    assert seen.calls == []
    assert capsys.readouterr().err == f"{PREFIX}{line}\n"


def test_the_longest_server_name_is_still_a_user() -> None:
    seen = Seen()

    with pytest.raises(Exec):
        _launch([f"mcp-{'k' * 31}", COMMAND], _environ(), seen)


def test_the_user_pattern_is_the_one_root_builds_trees_as() -> None:
    """Root's executor refuses an `As.MCP` command whose user fails
    `MCP_USER_RE`, and root's reader validates the name with
    `SERVER_NAME_RE`. The launcher agrees with both, or a server root
    built would be one the PEP cannot start."""
    assert run_as.USER_RE.pattern == MCP_USER_RE.pattern
    assert run_as.USER_RE.pattern == f"mcp-{SERVER_NAME_RE.pattern}"


# -- the environment --------------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {"HOME": "/var/lib/creche-chaperone/home"},
        {CHILD_ENV_VAR: "not json"},
        {CHILD_ENV_VAR: "[]"},
        {CHILD_ENV_VAR: json.dumps({"KAGI_API_KEY": 7})},
        {CHILD_ENV_VAR: json.dumps({"A=B": "s3cret"})},
        {CHILD_ENV_VAR: json.dumps({"": "s3cret"})},
        {CHILD_ENV_VAR: json.dumps({"KAGI_API_KEY": "s3\x00cret"})},
        {CHILD_ENV_VAR: json.dumps({"KAGI\x00": "s3cret"})},
    ],
)
def test_an_environment_the_pep_did_not_give_is_refused(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    seen = Seen()

    assert _launch(["mcp-kagi", COMMAND], environ, seen) == REFUSED_EXIT

    assert seen.calls == []
    err = capsys.readouterr().err
    assert err.startswith(f"{PREFIX}{CHILD_ENV_VAR} ")
    assert err.count("\n") == 1
    # A refusal names the rule, never a value: this line lands in the journal.
    assert "s3" not in err


# -- the user ---------------------------------------------------------------


def test_a_user_this_host_does_not_have_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    seen = Seen()

    code = run_as.launch(
        [ABSENT, COMMAND], _environ(), lookup=pwd.getpwnam, become=seen.become, execve=seen.execve
    )

    assert code == REFUSED_EXIT
    assert seen.calls == []
    assert capsys.readouterr().err == f"{PREFIX}no user '{ABSENT}' on this host\n"


@pytest.mark.parametrize(("uid", "gid"), [(0, 0), (0, FAKE_GID), (FAKE_UID, 0)])
def test_root_is_refused_even_when_the_name_passes(
    uid: int, gid: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pattern keeps `root` out by name. A passwd line that gives an
    `mcp-` name uid 0 or gid 0 is kept out by number."""
    seen = Seen()

    def rooted(user: str) -> pwd.struct_passwd:
        return pwd.struct_passwd((user, "x", uid, gid, "", "/", "/bin/sh"))

    code = run_as.launch(
        ["mcp-kagi", COMMAND], _environ(), lookup=rooted, become=seen.become, execve=seen.execve
    )

    assert code == REFUSED_EXIT
    assert seen.calls == []
    line = f"{PREFIX}'mcp-kagi' is uid {uid} gid {gid}, and a server never runs as root\n"
    assert capsys.readouterr().err == line


@pytest.mark.parametrize(("uid", "gid"), [(os.getuid(), FAKE_GID), (FAKE_UID, os.getgid())])
def test_the_launchers_own_ids_are_refused(
    uid: int, gid: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """The launcher runs as the PEP's child: its ids are `chaperone` and the
    unit's group. A passwd line that hands an `mcp-` name either one would
    start the server as the PEP again, which is the fault this ends."""
    seen = Seen()

    def ours(user: str) -> pwd.struct_passwd:
        return pwd.struct_passwd((user, "x", uid, gid, "", "/", "/nologin"))

    code = run_as.launch(
        ["mcp-kagi", COMMAND], _environ(), lookup=ours, become=seen.become, execve=seen.execve
    )

    assert code == REFUSED_EXIT
    assert seen.calls == []
    line = f"{PREFIX}'mcp-kagi' is uid {uid} gid {gid}, and a server never runs as the PEP\n"
    assert capsys.readouterr().err == line


# -- the switch -------------------------------------------------------------


def test_a_switch_that_fails_never_reaches_the_exec(capsys: pytest.CaptureFixture[str]) -> None:
    seen = Seen()

    def refuse(target: Target) -> None:
        raise Refused(f"cannot become {target.user!r}: Operation not permitted")

    code = run_as.launch(
        ["mcp-kagi", COMMAND], _environ(), lookup=seen.lookup, become=refuse, execve=seen.execve
    )

    assert code == REFUSED_EXIT
    assert seen.calls == [("lookup", "mcp-kagi")]
    assert capsys.readouterr().err == f"{PREFIX}cannot become 'mcp-kagi': Operation not permitted\n"


def test_an_exec_that_fails_is_a_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    seen = Seen()

    def missing(command: str, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
        raise FileNotFoundError(2, "No such file or directory")

    code = run_as.launch(
        ["mcp-kagi", COMMAND], _environ(), lookup=seen.lookup, become=seen.become, execve=missing
    )

    assert code == REFUSED_EXIT
    assert capsys.readouterr().err == f"{PREFIX}cannot run '{COMMAND}': No such file or directory\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root holds the capability and would switch")
def test_the_real_switch_is_refused_without_the_capability() -> None:
    """The kernel's own refusal, not a fake: this process holds neither
    CAP_SETUID nor CAP_SETGID. On a Mac the launcher refuses before any
    call, because the drop is a Linux interface."""
    groups = os.getgroups()

    with pytest.raises(Refused, match=r"^cannot become 'mcp-kagi': "):
        run_as.become(Target("mcp-kagi", FAKE_UID, FAKE_GID))

    assert os.getgroups() == groups


# -- what the kernel says afterwards ----------------------------------------

TARGET: Final = Target("mcp-kagi", FAKE_UID, FAKE_GID)

#: `/proc/self/status` after a clean switch: every id the server's, no
#: supplementary group, every capability set empty but the bounding set
#: (which only caps what could ever be regained), and no_new_privs.
CLEAN_STATUS: Final = "\n".join(
    [
        "Name:\tpython3",
        f"Uid:\t{FAKE_UID}\t{FAKE_UID}\t{FAKE_UID}\t{FAKE_UID}",
        f"Gid:\t{FAKE_GID}\t{FAKE_GID}\t{FAKE_GID}\t{FAKE_GID}",
        "Groups:\t",
        "CapInh:\t0000000000000000",
        "CapPrm:\t0000000000000000",
        "CapEff:\t0000000000000000",
        "CapBnd:\t00000000000000c0",
        "CapAmb:\t0000000000000000",
        "NoNewPrivs:\t1",
    ]
)


def _status_with(field: str, value: str | None) -> str:
    lines = []
    for line in CLEAN_STATUS.splitlines():
        if not line.startswith(f"{field}:"):
            lines.append(line)
            continue
        if value is not None:
            lines.append(f"{field}:\t{value}")

    return "\n".join(lines)


def test_a_clean_status_passes() -> None:
    run_as.check_status(CLEAN_STATUS, TARGET)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # A setuid between two users that are not root keeps every
        # capability. These four are the reason the drop exists at all.
        ("CapEff", "00000000000000c0"),
        ("CapPrm", "0000000000000080"),
        ("CapInh", "0000000000000040"),
        ("CapAmb", "00000000000000c0"),
        ("NoNewPrivs", "0"),
        ("Uid", f"{FAKE_UID}\t{FAKE_UID}\t1001\t{FAKE_UID}"),
        ("Gid", f"1001\t{FAKE_GID}\t{FAKE_GID}\t{FAKE_GID}"),
        ("Groups", "27 1001"),
        # A field the kernel did not print is not a field that said zero.
        ("CapAmb", None),
        ("NoNewPrivs", None),
        ("Uid", None),
    ],
)
def test_a_status_that_kept_anything_is_refused(field: str, value: str | None) -> None:
    with pytest.raises(Refused, match=rf"^the switch to 'mcp-kagi' did not take: {field} "):
        run_as.check_status(_status_with(field, value), TARGET)


@pytest.mark.skipif(sys.platform != "linux", reason="capabilities are a Linux interface")
def test_the_drop_leaves_no_capability_on_this_kernel() -> None:
    """The ctypes calls themselves, against the real kernel. A subprocess,
    because no_new_privs cannot be unset and the test process keeps
    running."""
    code = (
        "from chaperone import run_as\n"
        "run_as.drop_capabilities()\n"
        "print(open('/proc/self/status').read())\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )

    assert done.returncode == 0, done.stderr
    status = dict(
        (key, value.strip())
        for key, _, value in (line.partition(":") for line in done.stdout.splitlines())
    )
    assert [int(status[one], 16) for one in ("CapInh", "CapPrm", "CapEff", "CapAmb")] == [0] * 4
    assert status["NoNewPrivs"] == "1"


# -- the installed console script -------------------------------------------


def _run_script(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *argv],
        env={CHILD_ENV_VAR: json.dumps(ROW_ENV), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_the_console_script_is_installed_beside_the_interpreter() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.slow
def test_the_console_script_refuses_a_user_it_cannot_become() -> None:
    """Run as the test's own user, against a user this host does not have:
    one line on stderr, nothing on stdout (the MCP channel), exit 125."""
    done = _run_script(ABSENT, "/bin/true")

    assert done.returncode == REFUSED_EXIT
    assert done.stdout == ""
    assert done.stderr == f"{PREFIX}no user '{ABSENT}' on this host\n"


@pytest.mark.slow
def test_the_console_script_refuses_root() -> None:
    done = _run_script("root", "/bin/true")

    assert done.returncode == REFUSED_EXIT
    assert done.stdout == ""
    assert done.stderr == f"{PREFIX}'root' is not an mcp-<name> user\n"


@pytest.mark.slow
def test_the_console_script_refuses_an_empty_command() -> None:
    done = _run_script("mcp-kagi", "")

    assert done.returncode == REFUSED_EXIT
    assert done.stderr == f"{PREFIX}the command is empty\n"
