"""`agent-pep-as`: start one MCP server as its own user (contract 01b §9).

    agent-pep-as mcp-<name> /absolute/command [args...]

The PEP starts every upstream through this console script
(`mcp_client.Launcher`). It runs as the PEP's own child, holding the two
capabilities `agent-pep.service` grants, and it is the only code that uses
them:

    1  argv    a user `mcp-<name>` and an absolute command
    2  env     the object in PEP_CHILD_ENV, and nothing else
    3  user    resolved by name, once. Root's ids and the PEP's are refused
    4  switch  setgroups([]), then setresgid, then setresuid
    5  drop    every capability set emptied and no_new_privs set, then
               /proc/self/status read back: the kernel's word, not ours
    6  exec    execve(command, [command, *args], env)

A step that fails writes ONE line to stderr and exits 125, before the exec.
The PEP reads a child that exits before `initialize` as that upstream
failing its boot probe, so it fails closed for that upstream alone. There is
no fallback: a server that cannot run as its own user does not run.

**Why step 5 exists.** A setuid between two users that are both not root
keeps every capability (capabilities(7), "Effect of user ID changes on
capabilities"). Without the drop, the server would start as `mcp-kagi`
holding CAP_SETUID, and could become anyone, root included.

**Why the environment is one JSON value.** This process holds CAP_SETUID
while it starts. An ambient capability does not set AT_SECURE, so the
dynamic loader and the interpreter honour LD_PRELOAD, PYTHONPATH and the
rest before one line of this file runs. The row's variables therefore cross
this process as data, and become an environment only at the server's own
execve, after the drop. The same value keeps the PEP's own variables out:
the MCP SDK merges HOME, LOGNAME, PATH, SHELL, TERM and USER into every
child it starts, and the server receives none of them.

Standard library only, and nothing is written to stdout: stdout is the
server's MCP channel before it is anything else.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, NoReturn, cast

#: Contract 01b §9's user for a server name that passed §1's pattern. It is
#: root's `MCP_USER_RE` character for character, and a test holds the two
#: together: a server root built is a server this launcher starts.
USER_RE: Final = re.compile(r"mcp-[a-z][a-z0-9-]{1,30}")

#: The one variable this process reads: the server's whole environment.
CHILD_ENV_VAR: Final = "PEP_CHILD_ENV"

#: Every refusal, before any exec. `env(1)`'s code for "the launcher itself
#: failed", so it cannot be read as the server's own exit.
REFUSED_EXIT: Final = 125

USAGE: Final = "usage: agent-pep-as mcp-<name> /absolute/command [args...]"
NOT_AN_OBJECT: Final = f"{CHILD_ENV_VAR} is not a JSON object of strings"

#: How much of an argv word a refusal shows. It is quoted and escaped, so
#: the refusal stays one line whatever the word holds.
MAX_SHOWN: Final = 80

#: The four sets the drop must leave empty, as `/proc/self/status` names
#: them. The bounding set is not one of them: it only caps what could be
#: regained, and with these four empty and no_new_privs set, nothing can.
CAPABILITY_FIELDS: Final = ("CapInh", "CapPrm", "CapEff", "CapAmb")
STATUS_FILE: Final = "/proc/self/status"
#: Its `Uid:` and `Gid:` lines each carry four: real, effective, saved and
#: filesystem.
IDS_PER_LINE: Final = 4

#: <linux/prctl.h> and <linux/capability.h>.
PR_SET_NO_NEW_PRIVS: Final = 38
PR_CAP_AMBIENT: Final = 47
PR_CAP_AMBIENT_CLEAR_ALL: Final = 4
LINUX_CAPABILITY_VERSION_3: Final = 0x20080522
#: Version 3 takes two 32-bit words each for effective, permitted and
#: inheritable. All six zero is the empty set.
CAPABILITY_WORDS: Final = 6


class Refused(Exception):
    """One reason the launcher stops, stated in one line."""


@dataclass(frozen=True)
class Target:
    """The user a server runs as, resolved once."""

    user: str
    uid: int
    gid: int


Lookup = Callable[[str], pwd.struct_passwd]
Become = Callable[[Target], None]
Execve = Callable[[str, list[str], dict[str, str]], NoReturn]


def main() -> int:
    """The console script: the one place the real switch is wired in."""
    return launch(sys.argv[1:], os.environ, lookup=pwd.getpwnam, become=become, execve=os.execve)


def launch(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    lookup: Lookup,
    become: Become,
    execve: Execve,
) -> int:
    """Steps 1 to 6. Returns only to refuse: a success is an exec."""
    try:
        user, command, args = _parse(argv)
        env = child_env(environ)
        target = _resolve(user, lookup)
        become(target)
        _restore_signals()
        _exec(command, args, env, execve)
    except Refused as refusal:
        # One line, whatever a future message holds: the PEP's journal
        # shows it beside that upstream's boot failure.
        line = str(refusal).replace("\n", "\\n")
        print(f"agent-pep-as: refused: {line}", file=sys.stderr, flush=True)

        return REFUSED_EXIT


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Step 2. The object the PEP put in PEP_CHILD_ENV, checked for what
    `execve` can carry. Nothing else in `environ` is read, and a refusal
    names the rule, never a value: a value may be a credential."""
    raw = environ.get(CHILD_ENV_VAR)
    if raw is None:
        raise Refused(f"{CHILD_ENV_VAR} is not set: the server's environment is the PEP's to give")

    try:
        loaded: object = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise Refused(NOT_AN_OBJECT) from exc

    if not isinstance(loaded, dict):
        raise Refused(NOT_AN_OBJECT)

    env: dict[str, str] = {}
    for name, value in cast("dict[object, object]", loaded).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise Refused(NOT_AN_OBJECT)

        if not name or "=" in name or "\0" in name or "\0" in value:
            raise Refused(NOT_AN_OBJECT)

        env[name] = value

    return env


def become(target: Target) -> None:
    """Steps 4 and 5: every id the server's, then no capability left."""
    if sys.platform != "linux":
        raise Refused(f"cannot become {target.user!r}: switching users needs Linux")

    try:
        os.setgroups([])
        os.setresgid(target.gid, target.gid, target.gid)
        os.setresuid(target.uid, target.uid, target.uid)
    except OSError as exc:
        raise Refused(f"cannot become {target.user!r}: {exc.strerror}") from exc

    drop_capabilities()
    try:
        with open(STATUS_FILE, encoding="utf-8", errors="replace") as status:
            text = status.read()
    except OSError as exc:
        raise Refused(f"cannot read {STATUS_FILE}: {exc.strerror}") from exc

    check_status(text, target)


def drop_capabilities() -> None:
    """Empty the ambient set, then the other three, then set no_new_privs.

    The ambient set goes first by name: it is the one that survives execve,
    and it is how this process got its two capabilities at all.
    """
    if sys.platform != "linux":
        raise Refused("cannot drop capabilities: they are a Linux interface")

    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        capset = libc.capset
    except (OSError, AttributeError) as exc:
        raise Refused(f"cannot drop capabilities: {exc}") from exc

    # Every argument after `option` is an unsigned long to the kernel, and
    # PR_CAP_AMBIENT_CLEAR_ALL refuses anything but zero in the rest.
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    capset.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    capset.restype = ctypes.c_int

    header = (ctypes.c_uint32 * 2)(LINUX_CAPABILITY_VERSION_3, 0)
    empty = (ctypes.c_uint32 * CAPABILITY_WORDS)()

    if prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        _cannot("clear the ambient set", ctypes.get_errno())

    if capset(header, empty) != 0:
        _cannot("clear the other three sets", ctypes.get_errno())

    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _cannot("set no_new_privs", ctypes.get_errno())


def check_status(text: str, target: Target) -> None:
    """Step 5's read-back of `/proc/self/status`.

    A field the kernel did not print is a refusal, not a zero: a kernel too
    old to report the ambient set is one this launcher cannot vouch for.
    """
    fields = {
        key: value.strip()
        for key, found, value in (line.partition(":") for line in text.splitlines())
        if found
    }
    # Every id the server's, no supplementary group, and no_new_privs.
    ids: dict[str, list[str]] = {
        "Uid": [str(target.uid)] * IDS_PER_LINE,
        "Gid": [str(target.gid)] * IDS_PER_LINE,
        "Groups": [],
        "NoNewPrivs": ["1"],
    }

    for field, wanted in ids.items():
        value = _field(fields, field, target)
        if value.split() != wanted:
            _did_not_take(target, field, value)

    for field in CAPABILITY_FIELDS:
        value = _field(fields, field, target)
        if not _is_zero(value):
            _did_not_take(target, field, value)


def _field(fields: Mapping[str, str], field: str, target: Target) -> str:
    if field not in fields:
        raise Refused(f"the switch to {target.user!r} did not take: {field} is missing")

    return fields[field]


def _did_not_take(target: Target, field: str, value: str) -> NoReturn:
    raise Refused(f"the switch to {target.user!r} did not take: {field} is {_shown(value)}")


def _cannot(step: str, errno: int) -> NoReturn:
    raise Refused(f"cannot {step}: {os.strerror(errno)}")


def _is_zero(value: str) -> bool:
    try:
        return int(value, 16) == 0
    except ValueError:
        return False


def _parse(argv: Sequence[str]) -> tuple[str, str, list[str]]:
    """Step 1."""
    if len(argv) < 2:
        raise Refused(USAGE)

    user, command, *args = argv
    if not USER_RE.fullmatch(user):
        raise Refused(f"{_shown(user)} is not an mcp-<name> user")

    if not command:
        raise Refused("the command is empty")

    if not os.path.isabs(command):
        # No PATH search: the server's environment is the row's, and the
        # launcher's own PATH is the PEP's.
        raise Refused(f"{_shown(command)} is not an absolute path")

    return user, command, list(args)


def _resolve(user: str, lookup: Lookup) -> Target:
    """Step 3. The pattern keeps `root` out by name. This keeps it out by
    number, whatever a passwd line says."""
    try:
        entry = lookup(user)
    except KeyError as exc:
        raise Refused(f"no user {user!r} on this host") from exc

    if entry.pw_uid == 0 or entry.pw_gid == 0:
        raise Refused(
            f"{user!r} is uid {entry.pw_uid} gid {entry.pw_gid}, and a server never runs as root"
        )

    # This process is the PEP's child, so its own ids are `pep` and the
    # unit's group. A passwd line that hands an `mcp-` name either one would
    # start the server as the PEP again, or in the group that reads grants.
    if entry.pw_uid == os.getuid() or entry.pw_gid == os.getgid():
        raise Refused(
            f"{user!r} is uid {entry.pw_uid} gid {entry.pw_gid}, and a server never runs as the PEP"
        )

    return Target(user, entry.pw_uid, entry.pw_gid)


def _restore_signals() -> None:
    """What `subprocess` does before its exec (`restore_signals=True`), and
    so what every server got until this launcher sat in between. Python
    ignores these two, and an ignored signal stays ignored across exec."""
    for one in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(one, signal.SIG_DFL)


def _exec(command: str, args: list[str], env: dict[str, str], execve: Execve) -> NoReturn:
    """Step 6. argv[0] is the command, as the SDK's own spawn made it."""
    try:
        execve(command, [command, *args], env)
    except OSError as exc:
        raise Refused(f"cannot run {_shown(command)}: {exc.strerror}") from exc


def _shown(value: str) -> str:
    return repr(value[:MAX_SHOWN])
