"""The PEP's upstream roster, written by root from what root installed.

`stage7-releases.md` §4.4 step 1 says "root writes the new upstream set and
signals the PEP". This module is that writer. Without it the `SIGHUP` the
executor sends makes the PEP re-read a file nothing updates, and the reload
computes `added=()`.

## Who writes it, and why that one

The roster names COMMANDS the PEP executes. So the writer has to be the
process that knows which commands exist, and the file has to be one that
the operator cannot write. Root, at step 9 of a verified `mcp-servers` release,
from the `server.yaml` files of exactly the tree it just installed, is the
only candidate that is both.

`caregiver` runs as the operator. If `caregiver` wrote this file, an operator-side
process could make the PEP run any command on the host — which is
`stage7-releases.md` §6 row 9 with the fence removed, and worse than row 9
because it needs no package and no release at all. The threat table's
answer to row 9 is "`command` is a console-script name, resolved inside
`/opt/mcp/<name>/bin/`, which only the hash-pinned install populates", and
that sentence is only true while the thing that BUILDS the path is root.

Five rules follow from it, and each is a test.

1. **Root builds `command` itself.** `<mcp root>/<name>/bin/<entrypoint>`,
   from the validated directory name and the validated entrypoint. No path
   out of the file is ever copied into it (§6 row 9).
2. **The file is written atomically, and the old one is kept.** The new
   roster is staged beside the name and renamed onto it, and the old one
   is COPIED to `<name>.prev` first — so a reader never sees a partial
   file and never sees no file at all, and a restore is one rename back.
3. **A roster the PEP cannot parse leaves the old pool serving.** That
   half belongs to `reload_wiring.reload_once`, which catches
   `UpstreamError` and says so. This module's half is to write YAML that
   `mcp_client.load_upstreams` accepts: `tools: all` is expanded against
   the declared set, because the PEP's reader has no word for `all`.
4. **This roster is the ONLY source for a declared server.** The PEP also
   reads the base roster the deployed checkout carries
   (`reload_wiring.RosterSource`), and that file holds no row
   (`pep/upstreams.yaml`). A name in both would take the generated row,
   because this file is root's, written at step 9 of a release the operator
   approved, from the tree root just installed. So a PEP that starts with
   no readable generated roster serves no MCP server, and a reload onto an
   unreadable one keeps the live pool.
5. **Root names a server's state directory, too** (contract 01b §4.2).
   A file that declares `run.state_dir` gets one more
   variable in its row, the one `run.state_dir_env` names, set to
   `<state root>/<name>`: the directory `mcpbuild` made at step 8. The
   PEP hands a row's `env` to the child as it is, so this
   variable is the whole of how the server learns its path.

## What is NOT here

`arg_allows`. Contract 01b §7 declares both fences and `UpstreamSpec`
carries only `arg_denies` (`pep/src/agent_pep/fences.py` says why: no
family holds an allow rule yet). Writing one into the roster would make
the file unparseable, so `steps._say_unapplied_allows` puts one `manual`
line in the ledger naming every server whose allows are not applied.

**Where the file lives** is `host.ROSTER_FILE`, beside the other paths
root writes: `/var/lib/agent-release/upstreams.yaml`, under root's own root
(`layout.py`). NOT `/opt/agent-control/pep/upstreams.yaml`, which is inside
the `pep` component's artifact — a `pep` release swaps that whole tree, so every
generated row would vanish on the PEP's next release with nothing
anywhere to say why.
"""

from __future__ import annotations

import os
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Final

import yaml

from ..errors import Refusal, RefusalCode
from ..mcpserver import ServerFile
from .host import MCP_STATE_ROOT

PREV_SUFFIX: Final = ".prev"
NEW_SUFFIX: Final = ".new"

#: `bin/` inside the installed tree, which is where `_check_entrypoint`
#: already proved the console script is.
BIN_DIR: Final = "bin"

#: Everybody may read it, only root may write it. The PEP runs as its own
#: user and has to open it on every reload.
FILE_MODE: Final = 0o644

#: Rule 5's root when a caller names none: the host's, as `Host` has it.
DEFAULT_STATE_ROOT: Final = Path(MCP_STATE_ROOT)


def rows(
    servers: tuple[ServerFile, ...], mcp_root: Path, state_root: Path = DEFAULT_STATE_ROOT
) -> dict[str, dict[str, object]]:
    """One roster row per server, in the PEP's own vocabulary.

    Rule 1: `command` is built here and never read out of the file. So is
    a state directory's path (rule 5).
    """
    return {
        one.name: _row(one, mcp_root, state_root)
        for one in sorted(servers, key=lambda one: one.name)
    }


def write(
    path: Path,
    servers: tuple[ServerFile, ...],
    mcp_root: Path,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> int:
    """Rule 2. Answers how many upstreams the new roster names.

    The write is a create-then-rename inside one directory, so a reader
    sees the old file or the new one and never a partial one, and never
    NO file: the previous roster is COPIED to `<name>.prev` rather than
    renamed to it. Renaming left a window, however short, in which the
    path did not exist — and a reload landing in that window reads the
    base roster alone and removes every released upstream.
    """
    written = rows(servers, mcp_root, state_root)
    if not path.parent.is_dir():
        # Root's visit makes the state root, and a release never does.
        # Creating it here would turn a mistyped path into a roster nobody
        # reads, and the release would report success.
        raise Refusal(RefusalCode.SERVER, "roster", f"{path.parent} is not a directory")

    body = yaml.safe_dump(written, sort_keys=True, default_flow_style=False)
    staged = path.with_name(path.name + NEW_SUFFIX)
    previous = path.with_name(path.name + PREV_SUFFIX)
    staged.write_text(body, encoding="utf-8")
    staged.chmod(FILE_MODE)
    if path.is_file():
        shutil.copy2(path, previous)

    os.replace(staged, path)

    return len(written)


class Restored(StrEnum):
    """What a restore did, for the ledger line that reports it.

    Three values and not a boolean, because "put the old one back" and
    "removed the one this release wrote" leave two different hosts, and a
    reader of `done/<ULID>.json` has to be able to tell them apart.
    """

    PREVIOUS = "put back"
    REMOVED = "removed, because this release wrote the first one"
    NOTHING = "nothing to put back"


def restore(path: Path) -> Restored:
    """Put the host back where the roster is concerned.

    Step 10 calls it before the signal, for the same reason
    `_restore_servers` puts the trees back before it: the PEP reads the
    roster as it is at the moment the signal lands.

    **A first release has no previous roster, and removing the file is
    the restore.** `write` copies the current roster to
    `<name>.prev` whenever there IS one, so no `.prev` beside a file this
    run wrote means exactly one thing: this run created the file. Before
    it, no generated roster existed, and `reload_wiring.RosterSource`
    reads a missing generated file as "no `mcp-servers` release has run",
    which is the state this restore is putting back.

    Leaving it would undo the trees' restore: the file would keep naming
    upstreams that are no longer installed, so every reader — and the PEP
    on its next reload — would see a release that failed its own verify.
    """
    previous = path.with_name(path.name + PREV_SUFFIX)
    if previous.is_file():
        os.replace(previous, path)

        return Restored.PREVIOUS

    if not path.is_file():
        return Restored.NOTHING

    path.unlink()

    return Restored.REMOVED


def _row(server: ServerFile, mcp_root: Path, state_root: Path) -> dict[str, object]:
    command = mcp_root / server.name / BIN_DIR / server.entrypoint
    if command.parent.parent.parent != mcp_root:
        # Belt and braces over `paths_of`'s own containment check. The name
        # and the entrypoint each matched a pattern with no slash in it, so
        # reaching here means one of those patterns changed.
        raise Refusal(RefusalCode.SERVER, f"mcp/{server.name}", "resolves outside the MCP root")

    row: dict[str, object] = {
        "command": str(command),
        "args": list(server.args),
        "env": _env(server, state_root),
    }
    if server.tools:
        # Contract 01b §5's closed set. Empty means the file declared none
        # and the probe decides alone, so the key is left out rather than
        # written empty.
        row["tools"] = list(server.tools)

    denies = [
        {"tools": list(one.covers(server.tools)), "arg": one.arg, "values": list(one.values)}
        for one in server.arg_denies
    ]
    if denies:
        row["arg_denies"] = denies

    return row


def _env(server: ServerFile, state_root: Path) -> dict[str, str]:
    """Rule 5. `run.env`, plus contract 01b §4.2's variable for a server
    that keeps state: `<state root>/<name>`, the directory `mcpbuild` made
    at step 8.

    Written LAST, so a literal of the same name in `run.env` loses. That
    file said two things, and only one of them is a directory root made for
    this server alone (e.g. `HA_MCP_CONFIG_DIR: /tmp/ha-mcp`, which two
    servers would share).
    """
    env = dict(server.env)
    if server.state_dir_env is None:
        return env

    state_dir = state_root / server.name
    if state_dir.parent != state_root:
        raise Refusal(RefusalCode.SERVER, f"mcp/{server.name}", "resolves outside the state root")

    env[server.state_dir_env] = str(state_dir)

    return env
