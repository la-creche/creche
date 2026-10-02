"""The `sbx exec` that hands the terminal to pi (contract 03 §7.6).

    sbx exec -it --env-file <supervisor.env> <sandbox> -- \\
      node /opt/agent-supervisor/agent-pi-launch.js \\
        --sandbox <family>-s<N> --session <session id> [--new]

There is no ingress into a sandbox and no second protocol here: the terminal
IS the client. From the moment this runs, the tty belongs to pi.

Three facts shape the command.

1. `sbx exec` forwards NO host environment. `--env-file` is the only thing
   that carries the three mount paths in, and `managerd` publishes that
   file's path per sandbox (contract 05 §4.1.1).
2. `-it` is what gives pi a terminal. Without it pi reads EOF and exits.
3. Nothing on this argv is a secret (invariant 13). Every word is a path or
   an identifier. Credentials reach pi through the credential mount, inside
   the VM, and never through this command.

The door runs `sbx` as a child rather than replacing itself with it, because
it still has work to do after pi exits: stop renewing the lease, then exit
with pi's own code.
"""

from __future__ import annotations

import subprocess
from enum import Enum
from typing import Protocol

#: Contract 03 §7.6's argument list.
LAUNCHER_ARG_SANDBOX = "--sandbox"
LAUNCHER_ARG_SESSION = "--session"
LAUNCHER_ARG_NEW = "--new"

#: The host has no `node` on its PATH, but the sandbox image does. This word
#: runs inside the VM, never on the host.
NODE = "node"

#: The exit code the door reports when `sbx` itself will not run. It is
#: outside contract 03 §7.6's 0 to 11 and outside this door's own codes.
EXIT_SBX_FAILED = 126


class Store(Enum):
    """Whether this session already has a pi store inside the sandbox.

    Contract 03 §7.6: without `--new` the launcher exits 9 when the store is
    missing. A session the door just created through `sessiond` has none, so
    this terminal is its first writer.
    """

    EXISTING = "existing"
    NONE_YET = "none_yet"


class TerminalRunner(Protocol):
    """Runs one foreground command and returns its exit code."""

    def run(self, argv: list[str]) -> int:
        """Run it, with this process's own stdin, stdout and stderr."""
        ...


class Terminal:
    """The real one: a foreground child that inherits the tty."""

    def run(self, argv: list[str]) -> int:
        try:
            return subprocess.run(argv, check=False).returncode
        except OSError as exc:
            # `sbx` missing or not executable. The door says which program
            # and why, because the operator's next step is to fix PATH.
            print(f"agent-tui: cannot run {argv[0]}: {exc.strerror}")

            return EXIT_SBX_FAILED


def launch_argv(
    *,
    sbx: str,
    supervisor_env: str,
    sandbox: str,
    session: str,
    launcher: str,
    store: Store,
) -> list[str]:
    """Contract 03 §7.6's command, word for word."""
    argv = [
        sbx,
        "exec",
        "-it",
        "--env-file",
        supervisor_env,
        sandbox,
        "--",
        NODE,
        launcher,
        LAUNCHER_ARG_SANDBOX,
        sandbox,
        LAUNCHER_ARG_SESSION,
        session,
    ]

    if store is Store.NONE_YET:
        argv.append(LAUNCHER_ARG_NEW)

    return argv
