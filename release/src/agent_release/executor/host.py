"""The one place a child process starts (`stage7-releases.md` §2.4).

Every side effect the executor has goes through `Host`, so a test fakes all
of it with temp directories and a recorded `run`. Nothing above this module
imports `subprocess`.

Three rules, each with the measurement behind it.

1. **No shell, anywhere** (§3.2 rule 6). A `Command` carries an argv LIST.
   There is no code path that accepts a string.
2. **A child inherits nothing.** Root's own environment holds every
   decrypted compose secret, so each child gets a fixed environment built
   here (§2.4, note on step 8). Invariant 13.
3. **`runuser` is `/usr/sbin/runuser`, by absolute path.** It is not on
   the operator's `PATH`, so resolving it on the child's `PATH` fails.
"""

from __future__ import annotations

import re
import resource
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from ..site import operator_home, operator_user
from . import layout
from .live_state import INSTALL_ROOT as INSTALL_ROOT
from .live_state import default_install_roots
from .live_state import operator_install_root as operator_install_root
from .source import CORPUS_ROOT

#: Absolute: `subprocess` looks `argv[0]` up on the CHILD's `PATH`, and
#: the operator's has no sbin.
RUNUSER: Final = "/usr/sbin/runuser"

#: What a child that outran its `timeout_s` answers with. Contract 06 §4
#: rule 3 makes a verify timeout a FAILURE, and no layer above this module
#: catches `subprocess.TimeoutExpired`, so letting it out killed the run
#: after the switch had already swapped the tree. 124 is `timeout(1)`'s.
TIMEOUT_EXIT_CODE: Final = 124

ROOT_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: An operator child's `PATH`, after the operator's own `~/.local/bin`.
OPERATOR_PATH_TAIL: Final = "/usr/local/bin:/usr/bin:/bin"
CHILD_LANG: Final = "C.UTF-8"

#: Where the operator's user units live, under the operator's home.
USER_UNIT_DIR: Final = ".config/systemd/user"

#: Where a release fetches and builds, before anything is swapped. Under
#: root's own root (`layout.py`).
WORK_ROOT: Final = layout.WORK_ROOT

#: The checkouts the executor fetches from. It holds no git credential
#: (contract 06 §3.4), so these are trees a process that DOES keeps current,
#: read BY SHA. A SHA is content addressed, so where it travelled cannot
#: change what it is.
#:
#: Not `/srv/agents/work/platform`: that root is the `agent-control`
#: family's READ-WRITE mount, so a model in that sandbox would write the
#: `.git/config` root's own git process reads.
#: `executor/source.py` carries the whole argument and the trust rule.
SOURCE_ROOT: Final = CORPUS_ROOT

#: `INSTALL_ROOT` and `operator_install_root` are `live_state.py`'s,
#: re-exported here because this module's readers know them by these names.
#:
#: They are the DEFAULT of `install_roots` and not merely a note, because two
#: defences read that field: `install.paths_of` refuses a manifest that names
#: a path outside it, and step 2 learns the live version from the tree under
#: it, which is what `provenance.check_monotonic` compares against. An empty
#: tuple turns both off silently. Leaving the operator's root out was that fix
#: applied to half the catalog: `paths_of` refused `sessiond`, `managerd`
#: and `noticeboard` at step 8, and a downgrade of `sessiond` read as a first
#: install.
#: Where every MCP server installs, one tree per server (contract 06 §1 row
#: 6). It is NOT an `install_roots` entry: no `component.yaml` may name a
#: path under it. Root builds each path from a server NAME it validated,
#: which is why the two roots stay apart (`mcpbuild`, assumption 2).
MCP_ROOT: Final = "/opt/mcp"

#: `executor/roster.py`'s file, spelled once in `layout.py`. It is here
#: beside the other host paths so a reader finds every path root writes in
#: one list.
ROSTER_FILE: Final = layout.ROSTER_FILE

#: Contract 01b §4.2's root: `<root>/<name>` is the state directory of each
#: server whose file declares `run.state_dir`. Under `/var/lib`
#: and not `/opt/mcp`, because state outlives every tree a release swaps.
#: The root itself is NOT a release's: `bin/rework-release-visit.sh` makes it
#: `root:root 0755` and `agent-pep.service` names it in `ReadWritePaths`,
#: which binds it writable only if it exists when the PEP starts.
MCP_STATE_ROOT: Final = "/var/lib/agent-mcp"

#: §2.8's signal, and the two paths root READS and never writes. They are
#: the operator's, and `bin/rework-cutover.sh up` makes both: the socket 0660 in a
#: 2750 directory, the token 0600. `executor/quiet.py` carries why root may
#: open an operator-owned token at all and how it avoids being the operator's deputy.
SESSIOND_SOCKET: Final = "/srv/agents/state/rework/sock/sessiond.sock"
VIEW_TOKEN_FILE: Final = "/srv/agents/state/rework/tokens/view-ro.token"


class As(StrEnum):
    """Who a child runs as. Contract 06 §4's `user`, and §8's `runs_as`."""

    ROOT = "root"
    #: The site's operator account (`site.operator_user`).
    OPERATOR = "operator"
    #: One MCP server's own unprivileged user (contract 06 §8's `runs_as:
    #: mcp`). It is the one identity that names no single user:
    #: `mcp-servers` is a FAMILY of processes, so the user is in
    #: `Command.user` and `MCP_USER_RE` is what makes it a user name.
    MCP = "mcp"


#: `mcp-<name>` for a server name root already validated. A `Command` that
#: claims `As.MCP` and carries anything else does not run — see `make_runner`.
#: Root builds this string from the directory name it walked, never from a
#: field of the file it is installing.
MCP_USER_RE: Final = re.compile(r"mcp-[a-z][a-z0-9-]{1,30}")

#: What an `As.MCP` command with no usable user answers. It is a failure and
#: never a fallback to root: a build that cannot drop privilege must not run
#: with them (`stage7-releases.md` §4.2, "a third-party package never runs as
#: root").
NO_MCP_USER_EXIT_CODE: Final = 125


@dataclass(frozen=True)
class Command:
    """One child. Everything it may differ in, and nothing else."""

    argv: tuple[str, ...]
    identity: As
    timeout_s: float
    cwd: Path | None = None
    #: Extra names on top of the fixed set. The executor passes the staged
    #: venv path here and nothing else.
    env: tuple[tuple[str, str], ...] = ()
    #: Required by `As.MCP`, meaningless for the other two. Kept out of
    #: `identity` because a StrEnum cannot hold one member per server.
    user: str | None = None
    #: The largest file this child may WRITE, as `RLIMIT_FSIZE`. None means
    #: no limit. The kernel enforces it during the write, which is the only
    #: place a download can be capped when the server sent no length. A
    #: child that exceeds it takes `SIGXFSZ`.
    max_file_bytes: int | None = None

    def line(self) -> str:
        """What the log records. Argv words, never a shell line, so nobody
        can paste it back as one."""
        who = f"{self.identity}:{self.user}" if self.user else str(self.identity)

        return f"({who}) {' '.join(self.argv)}"


@dataclass(frozen=True)
class Result:
    """One child's exit status and output, both bounded by the caller."""

    code: int
    stdout: str = ""
    stderr: str = ""


RunFn = Callable[[Command], Result]

#: Where `sops` lives. Absolute for the same reason `runuser` is: a child's
#: `PATH` is built here and is not promised to hold it.
SOPS: Final = "/usr/local/bin/sops"

#: One seal. Encryption needs PUBLIC keys only, so this child holds no
#: private key and root can write a secret it cannot read
#: (`stage7-releases.md` §4.3). Reading `/dev/stdin` is what keeps the
#: plaintext out of argv and off the disk. `sops`'s stdin handling is
#: confirmed on the host before the first paste.
SEAL_TIMEOUT_S: Final = 30.0

#: A sealed secret root will read back into memory. The largest is an
#: armoured PEM block around an 8 KiB value.
MAX_SEALED_BYTES: Final = 128 * 1024


def seal_argv(recipients: tuple[str, ...]) -> tuple[str, ...]:
    """`sops`, encrypt only, to a public recipient set.

    `sops` and not `age`, because the PEP decrypts these files with
    `sops -d` already (`pep/src/agent_pep/secrets.py`). One format and one
    tool on both ends beats a second one that root alone understands.

    `--config /dev/null` is a control rather than tidiness
    (`test_release_r7g_sealer_sops.py`).
    `sops` walks UP from the child's working directory looking for a
    `.sops.yaml`, and a config whose `creation_rules` match no path exits
    1 with "no matching creation rules found" — even though `--age` names
    the recipients right here on the command line. The sealer's child
    inherits root's working directory, and every platform checkout under
    `/srv/agents/work/platform/` carries a `.sops.yaml` the attacker
    writes. One file therefore wedged every future paste at 500, and the
    only thing said anywhere would have been "the value could not be
    sealed". Named explicitly, no file on disk decides whether root can
    seal. Measured with `sops` 3.13.3: a discovered config can never ADD a
    recipient past this `--age`, so the wedge was the whole exposure.
    """
    return (
        SOPS,
        "--config",
        "/dev/null",
        "--encrypt",
        "--age",
        ",".join(recipients),
        "--input-type",
        "binary",
        "--output-type",
        "binary",
        "/dev/stdin",
    )


def make_sealer(recipients: tuple[str, ...], operator_uid: int) -> Callable[[bytes], bytes | None]:
    """The one child that is HANDED a secret.

    The plaintext goes to the child's STDIN. It never reaches `Command`,
    which is logged, and never reaches argv or a file (invariant 13). The
    answer is the sealed bytes, or None for every failure — a caller that
    learns why would have to say so somewhere.
    """
    argv = list(seal_argv(recipients))

    def seal(plaintext: bytes) -> bytes | None:
        if not recipients:
            return None

        try:
            proc = subprocess.run(
                argv,
                input=plaintext,
                env=child_env(As.ROOT, operator_uid),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=SEAL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        if proc.returncode != 0 or not proc.stdout:
            return None

        if len(proc.stdout) > MAX_SEALED_BYTES:
            return None

        return proc.stdout

    return seal


def _user_unit_dir() -> Path:
    """The operator's user-unit directory, e.g.
    `/home/operator/.config/systemd/user`."""
    return Path(operator_home()) / USER_UNIT_DIR


@dataclass(frozen=True)
class Host:
    """Every side effect the executor has, injected so tests fake all of it."""

    run: RunFn
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    work_root: Path = Path(WORK_ROOT)
    source_root: Path = Path(SOURCE_ROOT)
    #: Who the corpus belongs to. `executor/source.py` refuses a source
    #: owned by anybody but this account or root, so root never runs git in
    #: a directory it cannot say who wrote.
    #:
    #: The default is ROOT, and that is the fail-closed end rather than a
    #: guess: a `Host` nobody configured trusts only what root itself owns,
    #: and every operator-owned corpus clone is refused with a code in the
    #: ledger. `drain.build_wiring` passes the operator's real numbers out of
    #: `pwd`, and a test passes its own, so no test needs the operator's account
    #: to exist. A hard-coded uid here would be a default that looks
    #: configured and is wrong on one host.
    source_owner_uid: int = 0
    source_owner_gid: int = 0
    system_unit_dir: Path = Path("/etc/systemd/system")
    user_unit_dir: Path = field(default_factory=_user_unit_dir)
    sessions_root: Path = Path("/srv/agents/sessions")
    install_roots: tuple[Path, ...] = field(default_factory=default_install_roots)
    mcp_root: Path = Path(MCP_ROOT)
    #: `stage7-releases.md` §4.4 step 1's file: the upstream roster root
    #: writes from the servers it just installed. Root's own state, not an
    #: install root — a `pep` release swaps the whole `pep` tree, so a
    #: roster inside it would vanish on the PEP's next release.
    roster_file: Path = Path(ROSTER_FILE)
    #: `MCP_STATE_ROOT`, a field so a test points it inside `tmp_path`.
    mcp_state_root: Path = Path(MCP_STATE_ROOT)
    #: §2.8's signal. Fields rather than constants at the call site, so a
    #: test points the quiet window at a socket of its own.
    sessiond_socket: Path = Path(SESSIOND_SOCKET)
    view_token_file: Path = Path(VIEW_TOKEN_FILE)


#: An `As.MCP` child's `PATH`. No sbin and no `~operator/.local/bin`: this user
#: administers nothing and owns no home directory of its own. The build
#: passes `HOME` and the `uv` cache directory in `extra`, pointing inside the
#: per-request work tree root made writable for it.
MCP_PATH: Final = "/usr/local/bin:/usr/bin:/bin"

#: An `As.MCP` child gets no home unless the caller names one. A package
#: build script that writes to `$HOME` fails rather than landing somewhere
#: root did not choose.
NO_HOME: Final = "/nonexistent"


def child_env(
    identity: As, operator_uid: int, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The WHOLE environment a child gets; nothing of ours is inherited.

    The operator: what a user unit needs to reach its own systemd (`HOME`,
    `XDG_CONFIG_HOME`) plus `XDG_RUNTIME_DIR`.

    mcp: the smallest environment `uv` can build a venv in. No `SUDO_UID`,
    because this child reads no git checkout, and no root `PATH`.

    root: `SUDO_UID` names the owner of the checkouts root reads. git lets
    root use a repository another user owns only then, and a system unit has
    none — the fetch would die of dubious ownership.
    """
    if identity is As.OPERATOR:
        home = operator_home()
        base = {
            "PATH": f"{home}/.local/bin:{OPERATOR_PATH_TAIL}",
            "HOME": home,
            "LANG": CHILD_LANG,
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_RUNTIME_DIR": f"/run/user/{operator_uid}",
        }
    elif identity is As.MCP:
        base = {"PATH": MCP_PATH, "HOME": NO_HOME, "LANG": CHILD_LANG}
    else:
        base = {
            "PATH": ROOT_PATH,
            "HOME": "/root",
            "LANG": CHILD_LANG,
            "SUDO_UID": str(operator_uid),
        }

    return base | (extra or {})


def _limit_writes(max_bytes: int) -> Callable[[], None]:
    """`RLIMIT_FSIZE` for one child, applied between fork and exec.

    This is the half of the download cap that `curl --max-filesize` cannot
    give: that flag reads `Content-Length`, and a server that sends none
    slips past it. The kernel does not need a length — it counts the bytes
    as they are written and raises `SIGXFSZ` at the limit.

    `preexec_fn` is documented as unsafe in a threaded process. The release
    executor is one short-lived root process with no threads of its own,
    which is the condition that makes it safe, and it is written down here
    because a future reader will ask.
    """

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))

    return apply


def make_runner(operator_uid: int) -> RunFn:
    """The real runner. `runuser` is how a root unit reaches the operator's units."""

    def run(command: Command) -> Result:
        argv = list(command.argv)
        if command.identity is As.OPERATOR:
            argv = [RUNUSER, "-u", operator_user(), "--", *argv]

        if command.identity is As.MCP:
            # Fail closed. A command that asked to drop privilege and cannot
            # say to whom must not run with the privilege it was dropping.
            if command.user is None or not MCP_USER_RE.fullmatch(command.user):
                return Result(NO_MCP_USER_EXIT_CODE, "", "no mcp user for this command\n")

            argv = [RUNUSER, "-u", command.user, "--", *argv]

        cap = command.max_file_bytes
        try:
            proc = subprocess.run(
                argv,
                env=child_env(command.identity, operator_uid, dict(command.env)),
                cwd=str(command.cwd) if command.cwd else None,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=command.timeout_s,
                check=False,
                preexec_fn=_limit_writes(cap) if cap is not None else None,
            )
        except subprocess.TimeoutExpired:
            # An ordinary failure, not an exception: every caller above
            # reads a `Result`, and none of them catches this.
            return Result(TIMEOUT_EXIT_CODE, "", f"no answer in {command.timeout_s:.0f}s\n")

        return Result(proc.returncode, proc.stdout, proc.stderr)

    return run
