"""`supervisor.env`, the file `sbx exec --env-file` hands the playpen
(contract 03 §7.1).

sbx mounts a host directory at the SAME path inside the VM. There is no
target path, so no fixed in-VM path exists for the playpen to fall back
to, and `sbx exec` forwards no host environment either. One file closes
both gaps:

    /srv/agents/state/rework/families/<family>/supervisor-<family>-s<N>.env
        AGENT_CRED_DIR=.../families/<family>/creds
        AGENT_FAMILY_CONFIG_DIR=.../families/<family>/config
        AGENT_CONTROL_DIR=.../families/<family>/control/<family>-s<N>
        AGENT_SANDBOX=<family>-s<N>

One file per SANDBOX, not per family: it names the sandbox id and that
sandbox's own control directory, and contract 05 §5 keeps the outgoing
sandbox serving while the incoming one starts.

It sits BESIDE the control directory, not inside it: a create empties the
sandbox's control directory (contract 05 §4.3 rule 5), and that directory
is mounted read-write into the untrusted sandbox.

It holds no secret. Every value is a path or an identifier, which is why
mode 0640 is enough and `creds.json` still needs 0600 (invariant 13)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from . import paths
from .atomic import atomic_write

#: Contract 05 §2 rule 3 keeps secrets out of world-readable files. This one
#: names directories only, so the group that runs the platform may read it.
PLAYPEN_ENV_MODE: Final = 0o640

#: The names `playpen/src/mounts.ts` reads, and `AGENT_SANDBOX`, which
#: `playpen/src/index.ts` takes when `--sandbox` is absent.
CRED_DIR_VAR: Final = "AGENT_CRED_DIR"
CONFIG_DIR_VAR: Final = "AGENT_FAMILY_CONFIG_DIR"
CONTROL_DIR_VAR: Final = "AGENT_CONTROL_DIR"
# Contract 05 §4.1.1's spelling.
SANDBOX_VAR: Final = "AGENT_SANDBOX"

_ASSIGN: Final = "="


def write_playpen_env(path: Path, *, state_root: Path, family: str, sandbox: str) -> None:
    """Write the file for one sandbox. Called once per create.

    Each directory is written as its HOST path, because that is also its
    path inside the VM. Nothing here rewrites a path into a `/run/...`
    target: no such directory exists under sbx."""
    lines = [
        f"{CRED_DIR_VAR}{_ASSIGN}{paths.creds_dir(state_root, family)}",
        f"{CONFIG_DIR_VAR}{_ASSIGN}{paths.config_dir(state_root, family)}",
        f"{CONTROL_DIR_VAR}{_ASSIGN}{paths.control_dir(state_root, family, sandbox)}",
        f"{SANDBOX_VAR}{_ASSIGN}{sandbox}",
    ]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    atomic_write(path, body, mode=PLAYPEN_ENV_MODE)


def read_playpen_env(path: Path) -> dict[str, str]:
    """The file as a mapping. A missing or unreadable file yields nothing.

    This exists for tests and for an operator reading the file back. The
    playpen never calls it: `sbx exec --env-file` is what parses the
    file on the way into the VM."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    found: dict[str, str] = {}

    for line in text.splitlines():
        name, sep, value = line.partition(_ASSIGN)

        if sep:
            found[name] = value

    return found
