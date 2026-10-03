"""One MCP server, built into `/opt/mcp/<name>` (`stage7-releases.md` §4.2).

`install.py` builds ONE tree for one component from that component's `build`
argv list. `mcp-servers` is one component with many servers inside it
(contract 06 §7), each with its own pin, its own venv and its own
unprivileged user, so the per-server build is a second shape and gets its
own module.

```
  mcp/<name>/server.yaml   ->  /opt/mcp/<name>.new  ->  /opt/mcp/<name>
  (contract 01b, the pin)      built as mcp-<name>      swapped, <name>.prev
                               owned by root after
```

## Every assumption this module makes, for the reviewer

1. **The server file is already validated.** `mcpserver.ServerFile` is the
   only way in. Nothing here re-reads YAML, and nothing here takes a name, a
   path or a user from a string a caller assembled.
2. **The name is the only thing that becomes a path.** `/opt/mcp/<name>` and
   `mcp-<name>` are built from `ServerFile.name`, which matched
   `SERVER_NAME_RE` and equalled the directory root walked. Every resulting
   path is then checked for containment under the MCP root anyway, because a
   check that rests on a pattern somewhere else is a check a refactor can
   delete.
3. **Nothing third-party runs as root.** The venv is created and filled as
   `mcp-<name>`, so a package's own build script runs unprivileged
   (`stage7-releases.md` §4.2, contract 06 §1 rule 3). Root owns the tree
   afterwards, which is what §4.2 step 4's `chown -R root:root` is for: if
   root had built it, the tree would already be root's. `host.As.MCP` fails
   closed — a command that cannot name its user does not run at all.

   **Every child this module starts, and as whom.** A reader should not
   have to trace the calls to answer this:

   | Child | Identity | Why |
   |---|---|---|
   | `install -d` | root | Only root can give a directory to another user. |
   | `curl` | root | It writes into root's work tree, and it runs no code from what it fetched. |
   | `uv venv --relocatable` | `mcp-<name>` | It creates the tree the next step fills. |
   | `uv pip install` | `mcp-<name>` | **A source distribution's `setup.py`
     runs here.** Ordinary third-party code, so not root's. |
   | `uv sync --frozen --no-editable --no-dev` | `mcp-<name>` | Same, for
     the `agent-mcp` distribution. |
   | `chown` | root | Taking the tree back at the end. |
   | `systemctl kill -s HUP` | root | Signalling the PEP is root's alone. |

   The modes are not a child at all (assumption 11).

   The asymmetry is deliberate: the fetch is root's because downloading
   bytes executes nothing, and the install is not, because installing
   bytes does.

   Every `mcp-<name>` child gets `HOME` and `UV_CACHE_DIR` inside
   `<work>/<name>/home`, a directory root makes and hands to that user
   first (`HOME_DIR`). The user has no home of its own, and `uv` will not
   run without a cache directory it can create.
4. **Every pin is an INPUT, and the build resolves nothing.** A
   `github-release` asset is downloaded to a root-owned staging FILE, under
   a `Content-Length` cap AND an `RLIMIT_FSIZE` the kernel enforces as the
   bytes are written, hashed
   here with `hashlib`, and compared with the declared `sha256` before
   `tarfile` opens it. A `pypi` server installs from the closure committed
   beside its `server.yaml` (contract 01b §3.4), with `--require-hashes` and
   no resolution step, and `agent-mcp` from that tree's own `uv.lock` with
   `uv sync --frozen --no-editable --no-dev`. A mismatch fails at step 8
   with nothing swapped.

   **A server tree reads no code from outside itself** (contract 06 §8.2,
   the rule the component tree already answers to). `uv sync` installs the
   project itself EDITABLE unless told otherwise, so a server tree would
   hold a `.pth` naming a work tree the release removes at its end: a
   server that imports nothing the moment root cleans up.
   `--no-editable` builds the distribution into the tree, and
   `check_tree` runs on every venv-built server tree, so the fault is a
   refusal at step 8 and never a broken upstream.
   **Generating the closure during the release would pin nothing**, because
   `--require-hashes` would then compare root's own fresh hashes with
   themselves and an artifact could change between the review of the pull
   request and the install.
5. **The unpack refuses five shapes.** An absolute member name, any `..`
   component, a symlink or hard link, a device, FIFO or socket, and any
   setuid or setgid bit. Members are counted and their sizes summed against
   caps, because a tar bomb inside a hash-pinned artifact is still a tar
   bomb.
6. **`--relocatable` comes first.** A venv does not survive the switch's
   rename otherwise: `uv` writes an absolute interpreter path into every
   console script, and after `<name>.new` becomes `<name>` each one names a
   directory that is gone and exits 126. A component's venv is built the
   same way (`install.RELOCATABLE_ARGV`), and a server's
   `bin/<entrypoint>` is the same kind of console script.
7. **The build never sees a secret.** `run.env` is not read here at all. A
   server's credentials reach it at run time, from the PEP, in the child's
   environment (invariant 13, contract 01b §4.1 rule 3).
   **Which child runs as whom, said out loud**:
   every `uv` step runs as `mcp-<name>`, so a source distribution's
   `setup.py` — which runs at install time and is ordinary third-party code
   — runs unprivileged. The three steps that are root's are the `curl`
   fetch, `install -d`, and the `chown`/`chmod` that takes the tree back.
   Root writes no package's bytes, and the only bytes it copies are a lock
   it has already validated.
8. **The build fetches no code root did not pin.** `pypi` and
   `agent-mcp` reach the network only through `uv` under
   `--require-hashes`, and `github-release` through one `curl` to a URL
   built from validated fields. No credential is presented to any of them:
   root holds none (contract 06 §3.4).
9. **A `source: agent-mcp` server installs from the tree the release
   already fetched**, at the SHA the approval was bound to, and it names
   no ref of its own. The `mcp-servers` component tag IS the version of
   every server that repository ships: one version, one tap, one
   invariant-17 check for all six at once. Root will not fetch a second
   ref after the tap, so a ref in the file could only ever disagree with
   what the phone showed. Contract 01b §3.3.
10. **Nothing is swapped until every server is built.** `stage_all` builds
    each `<name>.new` and touches no live tree. `swap_all` is a separate
    call, made at step 9, and `swap_back` puts every server back.
11. **Every directory root makes here is opened for the server's own
    user, because the umask closes it** (`install.normalize_modes` answers
    the same umask one module over). The release unit runs `UMask=0027`, root
    builds everything, and `Path.mkdir` takes that umask — so a directory
    root made comes out `root:root 0750`, and an `mcp-<name>` is in no
    group that reaches it. Four places, and the first is the one that
    fails in silence:

    | Path | Who must enter it | When it bites |
    |---|---|---|
    | `/opt/mcp` | every `mcp-<name>`, for ever | The release succeeds and no server can start. |
    | the staging tree | the `uv pip install` child | The `pypi` build fails at step 8. |
    | the `agent-mcp` checkout | the `uv sync` child | The `agent-mcp` build fails at step 8. |
    | `<name>.new` | the PEP, at every start | The server cannot exec its entrypoint. |

    `install.normalize_modes` is the rule, `normalize_dir` its
    one-directory half, and neither is copied here: two expressions of one
    rule is how two trees end up with two answers.

    **What stays tight.** Nothing gains a write bit, so root remains the
    only writer of what the PEP executes. `/opt/mcp` is opened for
    TRAVERSAL only, and the walk stops there — every other server's live
    tree under it is somebody else's release and is not re-moded. **No
    secret is in any of these trees.** A server's credentials live in
    `/var/lib/agent-release/secrets/<name>.enc`, `root:root`, which the
    PEP decrypts and hands to the child in its environment (§4.3,
    invariant 13). `run.env` is not read by this module at all
    (assumption 7), so there is nothing in a staged tree for a wider mode
    to expose.
12. **A server that keeps state gets one directory, and it outlives every
    release** (contract 01b §4.2). A file that declares
    `run.state_dir` gets `<state root>/<name>`, owned by `mcp-<name>`,
    mode `0700`, made at step 8 by `_make_owned_dir`'s `install -d` once
    the tree is built, and before anything swaps. A swap, a restore and a
    failed release all leave it: state is the point, and removing it is a
    bigger verb than a release, which is also why a removed server's tree
    stays. The roster hands its path to the server (`roster.py` rule 5).

    ```
    /var/lib/agent-mcp          root:root 0755   the visit makes it
    /var/lib/agent-mcp/<name>   mcp-<name> 0700  step 8 makes it
    ```

    **The root is never a release's.** The PEP's children inherit its
    mount namespace, `ProtectSystem=strict` makes every path read-only
    there, and `creche-chaperone.service`'s `ReadWritePaths` entry for the root
    binds it writable only if it exists as the PEP starts. So
    `bin/rework-release-visit.sh` makes it before its own PEP restart,
    and a release that finds no root refuses before any child runs. A
    root the release made would be read-only to every server until the
    next restart, and ha-mcp falls back to `/tmp` without failing: a
    `succeeded` over state that does not persist.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from ..errors import Refusal, RefusalCode, safe_token
from ..mcpserver import ServerFile, Source
from .host import As, Command, Host, Result
from .install import (
    BUILD_TIMEOUT_S,
    INSTALL,
    NEW_SUFFIX,
    SHORT_TIMEOUT_S,
    SYSTEMCTL,
    UV,
    VENV_ENV_NAME,
    StepFailed,
    normalize_dir,
    normalize_modes,
    remove_tree,
)
from .selfcontained import check_tree

PREV_SUFFIX: Final = ".prev"

#: The server user's home for the length of its build: `<work>/<name>/home`,
#: made by root and handed to the user before any child runs. The user has
#: no home of its own on purpose (`host.NO_HOME` is `/nonexistent`), and
#: `uv` refuses to run without a cache directory it can create: without
#: this home a build dies at its first server with `the staged environment
#: could not be created (exit 2)`, which is `Failed to initialize cache at
#: /nonexistent/.cache/uv`. The cache lives under this home, so root's
#: removal of the work tree at the release's end takes both.
HOME_DIR: Final = "home"
UV_CACHE_DIR_NAME: Final = "uv-cache"

#: Where the interpreters the registry declares live: `/opt/mcp/.python`,
#: root's, `0755` all the way down, shared by every server tree that names
#: that minor. A dot name, so nothing that globs `/opt/mcp/*` for server
#: trees sees it, and under the MCP root so every `mcp-<name>` can reach
#: it. A server can require a minor the host lacks — `ha-mcp` requires
#: `>=3.13` and the host has 3.12 — and a build child may not download,
#: so without this the build dies at `No interpreter found for Python
#: 3.13`. `UV_PYTHON_INSTALL_DIR` names it and the tree is opened
#: `a+rX`, by the release, so a new server needing a new minor is still
#: one action.
PYTHON_DIR_NAME: Final = ".python"

CURL: Final = "/usr/bin/curl"
CHOWN: Final = "/usr/bin/chown"

#: The largest release asset root will hold. github-mcp-server's Linux
#: tarball is about 10 MB, and nothing an MCP server ships is near this.
MAX_ASSET_BYTES: Final = 128 * 1024 * 1024

#: `bin/creche-deploy` has fetched every github-mcp-server release
#: with these flags since it was written. `--proto '=https'` and `--tlsv1.2`
#: are the two that matter: a redirect to `http` or to `file:` is refused by
#: curl itself, before root sees a byte.
#:
#: `--max-filesize` caps what one fetch writes. A `server.yaml` names `repo`,
#: `version` and `asset`, so a hostile file picks a path on github.com that
#: serves as much as it likes into root's work tree, and a cap checked on a
#: file that has already landed is too late.
#:
#: **The flag alone is not the fix.** `curl` reads it against
#: `Content-Length`, and a chunked response sends none. So the fetch ALSO
#: runs under `RLIMIT_FSIZE` (`Command.max_file_bytes`), which the kernel
#: counts as the bytes are written and needs no length to do it. Two halves:
#: the flag refuses a declared oversize before a byte lands, the rlimit
#: stops an undeclared one at the cap.
CURL_FLAGS: Final = (
    "-fsSL",
    "--proto",
    "=https",
    "--tlsv1.2",
    "--max-time",
    "600",
    "--max-filesize",
    str(MAX_ASSET_BYTES),
)

DOWNLOAD_TIMEOUT_S: Final = 900.0

#: Assumption 5's two caps. A hash-pinned artifact can still be a tar bomb:
#: the hash proves it is the file the project published, not that the file
#: is small.
MAX_MEMBERS: Final = 20_000
MAX_UNPACKED_BYTES: Final = 512 * 1024 * 1024

#: Read in blocks so a capped file never arrives in memory whole.
HASH_BLOCK_BYTES: Final = 1024 * 1024

#: The largest committed closure root will read. A `--generate-hashes` lock
#: over a hundred dependencies is about 100 KiB.
MAX_LOCK_BYTES: Final = 4 * 1024 * 1024

#: Who owns the tree afterwards: root, every byte of it
#: (`stage7-releases.md` §4.2 step 4). The modes are assumption 11's, and
#: they are `install.normalize_modes`, not a `chmod` argument.
OWNER: Final = "root:root"

#: `install -d -m`. An explicit mode, so this one directory is the only
#: one in the module the umask never reaches.
DIR_MODE: Final = "0755"

#: Contract 01b §4.2's directory (assumption 12): the server's own, and
#: nobody else's to enter. `install -d -m` too.
STATE_DIR_MODE: Final = "0700"

#: Where root puts its copy of the committed closure, for the unprivileged
#: child to install from. The registry's own file is the input.
LOCK_FILE: Final = "install.lock"

#: What a `uv pip compile --generate-hashes` line looks like. Root reads the
#: closure to put it in the ledger, and to prove the lock pins the
#: requirement the file declared (contract 01b §3.4 rule 5).
HASH_MARKER: Final = "--hash=sha256:"

#: The unit that holds every MCP child. Step 9's last action for an
#: `mcp-servers` release is one signal to it (`stage7-releases.md` §4.4).
CHAPERONE_UNIT: Final = "creche-chaperone.service"

#: `reload_pool.TRIGGER`, as an argv list. `systemctl kill` and not a PID
#: root looked up: the unit name is the only thing root has to be right
#: about, and systemd resolves it to the running process.
HUP_ARGV: Final = (SYSTEMCTL, "kill", "-s", "HUP", CHAPERONE_UNIT)


@dataclass(frozen=True)
class Fetched:
    """Everything this release already has on disk, and the tag it deploys.

    `registry` holds the server declarations AND their committed closures
    (contract 01b §3.4). `tree` is the `agent-mcp` checkout root fetched at
    the approved SHA. `tag` is what the phone showed, and assumption 9 is
    about a `source: agent-mcp` server's `ref` agreeing with it: root
    fetches nothing after the tap.
    """

    registry: Path
    tree: Path
    tag: str


@dataclass(frozen=True)
class ServerPaths:
    """One server's three trees, all checked for containment."""

    to: Path
    prev: Path
    new: Path


@dataclass(frozen=True)
class ServerBuild:
    """What one staged server is, in the words the ledger uses.

    `artifact` names WHAT was installed and `closure` is the resolved set of
    hashes root generated for it. Neither holds a secret: a pin is a public
    fact (contract 01b §3).
    """

    name: str
    paths: ServerPaths
    artifact: str
    closure: tuple[str, ...] = ()
    #: Contract 01b §4.2's directory, or None when the file keeps no state.
    #: Nothing swaps it and nothing puts it back (assumption 12).
    state_dir: Path | None = None

    def as_dict(self) -> dict[str, object]:
        return {"server": self.name, "artifact": self.artifact, "closure": list(self.closure)}


def _refuse(name: str, detail: str) -> Refusal:
    return Refusal(RefusalCode.SERVER, f"mcp/{safe_token(name)}", detail)


class McpBuilder:
    """Stage, swap and restore the per-server trees of one release.

    It holds the host and the MCP root and nothing else, so a test drives
    every step inside `tmp_path` with a recorded `run`.
    """

    def __init__(self, host: Host, mcp_root: Path) -> None:
        self.host = host
        self.mcp_root = mcp_root
        #: `PYTHON_DIR_NAME`'s reason. Under the MCP root, so a test's
        #: `tmp_path` holds it too.
        self.python_root = mcp_root / PYTHON_DIR_NAME
        #: Assumption 12's root, `host.MCP_STATE_ROOT` on a host.
        self.state_root = host.mcp_state_root

    # -- stage ----------------------------------------------------------

    def stage_all(
        self,
        servers: tuple[ServerFile, ...],
        work: Path,
        source: Fetched,
        *,
        traversable_from: Path | None = None,
    ) -> tuple[ServerBuild, ...]:
        """Build every server's `<name>.new`. Assumption 10: it swaps none.

        `source` is the `agent-mcp` checkout this release already fetched at
        the approved SHA, with the tag it was fetched at. A
        `source: agent-mcp` server installs from that tree and from nowhere
        else, and names no tag of its own (assumption 9).

        `traversable_from` is the work ROOT. Every directory from it down to
        `work` is opened for traversal first (assumption 11):
        `stage_one` opens `work` and the staging directory, but the
        release unit's `UMask=0027` makes the root's own
        `/srv/agents/work/release` and the request's directory above them
        `0750`, so no `mcp-<name>` could reach its home, its lock or its
        cache.
        """
        if traversable_from is not None:
            _open_down_to(traversable_from, work)

        # Assumption 12, before any child runs: a host without the state
        # root is refused before it pays for a single build.
        keeping = next((one for one in servers if one.state_dir_env is not None), None)
        if keeping is not None:
            self._require_state_root(keeping.name)

        self._ensure_interpreters(servers)

        return tuple(self.stage_one(one, work, source) for one in servers)

    def _ensure_interpreters(self, servers: tuple[ServerFile, ...]) -> None:
        """Every minor a venv server declares is on the host before any
        build child runs (`PYTHON_DIR_NAME`).

        Root asks `uv python find` first, with downloads off and the
        shared directory in view: the host's own interpreter answers for
        3.12, an earlier release's for a minor already installed. Only a
        minor nobody provides is installed, by ROOT — the one child of
        this module that fetches from the network, and it fetches an
        interpreter build whose sha256 the host's `uv` release carries in
        its own manifest, so the input is pinned by that release rather
        than by a build child's choice. `--no-bin`: no symlink into
        anybody's `~/.local/bin`. Then the whole tree is opened (`a+rX`):
        root's umask made every directory `0750`.
        """
        wanted = sorted(
            {one.pin.python for one in servers if one.pin.source is not Source.GITHUB_RELEASE}
        )
        shared = {"UV_PYTHON_INSTALL_DIR": str(self.python_root)}
        for minor in wanted:
            found = self.host.run(
                Command(
                    (UV, "python", "find", minor),
                    As.ROOT,
                    SHORT_TIMEOUT_S,
                    env=tuple(sorted({**shared, "UV_PYTHON_DOWNLOADS": "never"}.items())),
                )
            )
            if found.code == 0:
                continue

            self._as_root(
                [UV, "python", "install", "--no-bin", minor],
                f"python {minor}: the interpreter could not be installed",
                timeout=BUILD_TIMEOUT_S,
                env=shared,
            )
            normalize_modes(self.python_root)

    def stage_one(self, server: ServerFile, work: Path, source: Fetched) -> ServerBuild:
        paths = self.paths_of(server.name)
        remove_tree(paths.new)
        staging = work / server.name
        remove_tree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        # Assumption 11 at BUILD time. `uv pip install` reads its closure
        # out of `staging` as `mcp-<name>`, and `Path.mkdir` gave both of
        # these the release unit's umask.
        normalize_dir(work)
        normalize_dir(staging)
        home = staging / HOME_DIR
        self._make_owned_dir(home, server.user)
        self._make_owned_dir(paths.new, server.user)
        build = self._fill(server, paths, staging, source, home)
        self._check_entrypoint(server, paths.new)
        self._check_self_contained(server, paths.new)
        self._give_to_root(paths.new)

        return replace(build, state_dir=self._make_state_dir(server))

    def _check_self_contained(self, server: ServerFile, new: Path) -> None:
        """Contract 06 §8.2 for a server tree: a `.pth` or an editable
        `direct_url.json` that names a path outside `<name>.new` is a
        refusal, with nothing swapped.

        Only the two venv sources. A `github-release` tree is an unpacked
        binary with no site-packages, which is the one shape the walk
        refuses for a reason that cannot apply to it.
        """
        if server.pin.source is Source.GITHUB_RELEASE:
            return

        check_tree(server.name, new)

    def paths_of(self, name: str) -> ServerPaths:
        """`/opt/mcp/<name>`, its previous tree and its staged one.

        Assumption 2: the name already matched its pattern, and the result
        is checked for containment anyway.
        """
        to = self.mcp_root / name
        paths = ServerPaths(
            to=to,
            prev=self.mcp_root / f"{name}{PREV_SUFFIX}",
            new=self.mcp_root / f"{name}{NEW_SUFFIX}",
        )
        for path in (paths.to, paths.prev, paths.new):
            if path.parent != self.mcp_root:
                raise _refuse(name, "resolves outside the MCP root")

        return paths

    def _fill(
        self,
        server: ServerFile,
        paths: ServerPaths,
        staging: Path,
        source: Fetched,
        home: Path,
    ) -> ServerBuild:
        if server.pin.source is Source.GITHUB_RELEASE:
            return self._from_release(server, paths, staging)

        self._venv(server, paths.new, home)
        if server.pin.source is Source.AGENT_MCP:
            return self._from_agent_mcp(server, paths, source.tree, source.tag, home)

        return self._from_pypi(server, paths, staging, source.registry, home)

    # -- the three sources ----------------------------------------------

    def _from_pypi(
        self,
        server: ServerFile,
        paths: ServerPaths,
        staging: Path,
        registry: Path,
        home: Path,
    ) -> ServerBuild:
        """Contract 01b §3.1. Install from the COMMITTED closure, and
        resolve nothing.

        The lock is copied into the staging directory before the install,
        so the unprivileged child reads a file root has already validated
        rather than reaching into the registry checkout for itself.
        """
        requirement = server.pin.requirement()
        text = _read_lock(server, registry)
        closure = _closure_of(server.name, text, requirement)
        lock = staging / LOCK_FILE
        lock.write_text(text, encoding="utf-8")
        lock.chmod(0o644)
        self._as_server(
            server,
            [
                UV,
                "pip",
                "install",
                "--python",
                str(_python_of(paths.new)),
                "--require-hashes",
                "--requirement",
                str(lock),
            ],
            f"{server.name}: the pinned closure would not install",
            home=home,
        )

        return ServerBuild(server.name, paths, requirement, closure)

    def _from_release(self, server: ServerFile, paths: ServerPaths, staging: Path) -> ServerBuild:
        """Contract 01b §3.2. Download, HASH, then unpack. In that order."""
        url = server.pin.asset_url()
        archive = staging / str(server.pin.asset)
        self._must(
            Command(
                argv=(CURL, *CURL_FLAGS, "--output", str(archive), url),
                identity=As.ROOT,
                timeout_s=DOWNLOAD_TIMEOUT_S,
                max_file_bytes=MAX_ASSET_BYTES,
            ),
            f"{server.name}: the release asset could not be fetched",
        )
        declared = str(server.pin.sha256)
        measured = _sha256_of(server.name, archive)
        if measured != declared:
            # Assumption 4. Nothing has been unpacked and nothing swapped:
            # the only thing on the host is one staging file inside the
            # request's own work tree, which the next stage removes.
            raise _refuse(
                server.name,
                f"{safe_token(str(server.pin.asset))} hashes to {measured}, "
                f"the file pins {declared}",
            )

        unpacked = staging / "unpacked"
        _extract_tar(server.name, archive, unpacked)
        self._place_binary(server, unpacked, paths.new)

        return ServerBuild(server.name, paths, url, (f"sha256:{declared}",))

    def _from_agent_mcp(
        self, server: ServerFile, paths: ServerPaths, tree: Path, tag: str, home: Path
    ) -> ServerBuild:
        """Contract 01b §3.3. Our own distribution, from the tree this
        release already fetched by SHA (assumption 9).

        `uv sync --frozen --no-editable`, so the closure is `agent-mcp`'s
        own committed `uv.lock` — a hash-pinned file inside the tree whose
        SHA the approval was bound to. `--frozen` refuses to update it,
        which is what stops a release from resolving. `--no-editable`
        builds the distribution INTO `<name>.new`:
        without it `uv sync` links the project as editable, and the tree
        would import `agent_mcp` from a work tree this release removes.
        `--no-dev`: the tree is that distribution alone. Its
        `dependencies` is empty on purpose, and the dev group is 42
        packages (pytest, ruff, pyright, the `mcp` client) no server
        imports — none of which belongs in a tree that runs as
        `mcp-<name>` with a credential in its environment.

        `tag` is the artifact this server was built from, and it comes
        from the RELEASE rather than from the file. The file names no
        version at all, so there is nothing here that could disagree with
        what the phone showed.
        """
        # Assumption 11. This is the one child whose WORKING DIRECTORY is
        # root's own fetched tree: `uv sync` reads `pyproject.toml`,
        # `uv.lock` and every source file in it as `mcp-<name>`, and root
        # cloned all of it under the release unit's umask. Public source
        # in root's work tree, so nothing widened here is a secret (§4.3
        # keeps every secret in a `<name>.enc` file elsewhere entirely).
        normalize_modes(tree)
        self._as_server(
            server,
            [UV, "sync", "--frozen", "--no-editable", "--no-dev"],
            f"{server.name}: the agent-mcp distribution would not install",
            home=home,
            cwd=tree,
            env={VENV_ENV_NAME: str(paths.new)},
        )

        return ServerBuild(server.name, paths, f"agent-mcp@{tag}")

    # -- the tree --------------------------------------------------------

    def _venv(self, server: ServerFile, new: Path, home: Path) -> None:
        """Assumption 6, then assumption 3: relocatable, and not as root."""
        self._as_server(
            server,
            [UV, "venv", "--relocatable", "--python", server.pin.python, str(new)],
            f"{server.name}: the staged environment could not be created",
            home=home,
        )

    def _make_owned_dir(self, path: Path, user: str, mode: str = DIR_MODE) -> None:
        """Root makes the directory and hands it to the server's user, so
        `uv` can fill it without root ever writing a package's bytes, or
        so the server has a state directory no other user may enter."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Assumption 11. That parent is the MCP root, root makes it with
        # `Path.mkdir`, and the umask therefore decides whether any server
        # can traverse it. `install -d` fixes the directory BELOW it only.
        # The state root is opened the same way (assumption 12).
        normalize_dir(path.parent)
        self._as_root(
            [INSTALL, "-d", "-m", mode, "-o", user, "-g", user, str(path)],
            f"cannot make {path.name} for {user}",
        )

    def _require_state_root(self, name: str) -> None:
        """Assumption 12: the root is the visit's, and a release never makes
        it. `creche-chaperone.service` binds it writable only if it exists as the
        PEP starts, so a root made here would be read-only to every server
        until the next restart, behind a ledger saying `succeeded`."""
        if self.state_root.is_symlink() or not self.state_root.is_dir():
            raise _refuse(
                name,
                f"state root {self.state_root} is not a directory "
                "(bin/rework-release-visit.sh makes it)",
            )

    def _make_state_dir(self, server: ServerFile) -> Path | None:
        """Assumption 12. `<state root>/<name>`, the server's own, `0700`.

        Made when missing and re-owned when present, by the same child
        `_make_owned_dir` runs for every other directory it hands over.
        Never removed, and never entered: whatever is inside, a link
        included, is the server's, and `install -d` touches only the
        directory itself.
        """
        if server.state_dir_env is None:
            return None

        self._require_state_root(server.name)
        path = self.state_root / server.name
        if path.parent != self.state_root:
            raise _refuse(server.name, "resolves outside the state root")

        if path.is_symlink():
            raise _refuse(server.name, "its state directory is a symlink")

        self._make_owned_dir(path, server.user, STATE_DIR_MODE)

        return path

    def _own(self, path: Path, user: str) -> None:
        self._as_root(
            [CHOWN, "-R", f"{user}:{user}", str(path)], f"cannot hand {path.name} to {user}"
        )

    def _give_to_root(self, new: Path) -> None:
        """§4.2 step 4. After this the tree is root's and world readable,
        and the unprivileged user that built it can no longer change it.

        The owner still needs a child: only `chown` can move a tree
        between users. The modes do not, and assumption 11 says why they
        stopped using one.
        """
        self._as_root([CHOWN, "-R", OWNER, str(new)], f"cannot take {new.name} back")
        normalize_modes(new)

    def _place_binary(self, server: ServerFile, unpacked: Path, new: Path) -> None:
        """A release asset ships a binary, not a venv. Put it where the PEP
        resolves it: `/opt/mcp/<name>/bin/<entrypoint>`."""
        found = _find_entrypoint(unpacked, server.entrypoint)
        if found is None:
            raise _refuse(server.name, f"the asset holds no {safe_token(server.entrypoint)}")

        target = new / "bin" / server.entrypoint
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(found, target)
        target.chmod(0o755)

    def _check_entrypoint(self, server: ServerFile, new: Path) -> None:
        """The staged tree must hold the console script the PEP will run,
        and that script must not name the staging tree (assumption 6)."""
        binary = new / "bin" / server.entrypoint
        if binary.is_symlink() or not binary.is_file():
            raise _refuse(server.name, "the staged tree holds no entrypoint")

        if not os.access(binary, os.X_OK):
            raise _refuse(server.name, "the staged entrypoint is not executable")

        if names_the_staging_tree(binary, new):
            raise _refuse(server.name, "the staged entrypoint still names the staging tree")

    # -- switch ----------------------------------------------------------

    def swap_all(self, builds: tuple[ServerBuild, ...]) -> None:
        for build in builds:
            self.swap_in(build)

    def swap_in(self, build: ServerBuild) -> None:
        """`<to>` to `<prev>`, then `<new>` to `<to>`. Two renames inside
        one filesystem, so each is atomic (`install.swap_in`'s rule)."""
        remove_tree(build.paths.prev)
        if build.paths.to.exists():
            os.rename(build.paths.to, build.paths.prev)

        os.rename(build.paths.new, build.paths.to)

    def swap_back(self, build: ServerBuild) -> None:
        """Put `<prev>` back. The failed tree goes to `<new>`, and the run
        that staged it removes it before it ends (`steps.remove_staged`).

        **A FIRST install has no `<prev>`, and taking the tree away IS the
        restore.** Every new server hits this the day it ships. Returning
        early would leave the new tree live at `/opt/mcp/<name>` after a
        release that failed its own verify, with the roster already put
        back — so root's own two records of what is installed would
        disagree.

        The tree goes where every other failed tree goes, `<name>.new`,
        and `run` removes it: a tree this run built minutes ago from a
        pin the ledger records is not something root might still want.
        """
        remove_tree(build.paths.new)
        if build.paths.to.exists():
            os.rename(build.paths.to, build.paths.new)

        if build.paths.prev.is_dir():
            os.rename(build.paths.prev, build.paths.to)

    def reload_chaperone(self) -> Result:
        """§4.4 step 1, and step 9's last action for an `mcp-servers`
        release. One signal, by unit name, as an argv list."""
        return self.host.run(Command(argv=HUP_ARGV, identity=As.ROOT, timeout_s=SHORT_TIMEOUT_S))

    # -- plumbing --------------------------------------------------------

    def _as_root(
        self,
        argv: list[str],
        what: str,
        timeout: float = SHORT_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ) -> str:
        command = Command(tuple(argv), As.ROOT, timeout, env=tuple(sorted((env or {}).items())))

        return self._must(command, what)

    def _as_server(
        self,
        server: ServerFile,
        argv: list[str],
        what: str,
        *,
        home: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Assumption 3. The identity is `As.MCP` and the user is derived
        from the validated name, so `host.make_runner` puts `runuser` in
        front and refuses to run at all when it cannot.

        `home` is required, not defaulted: `host.child_env` gives an
        `As.MCP` child `HOME=/nonexistent` and nothing else, and every
        `uv` child needs a cache directory it can create (`HOME_DIR`). The
        interpreter is the host's, or one root put under `PYTHON_DIR_NAME`
        before this child ran (`_ensure_interpreters`), or nothing: a
        download by this child would cross the egress allowlist and fail
        late with a worse message.
        """
        child_env = {
            "HOME": str(home),
            "UV_CACHE_DIR": str(home / UV_CACHE_DIR_NAME),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PYTHON_INSTALL_DIR": str(self.python_root),
            **(env or {}),
        }
        command = Command(
            argv=tuple(argv),
            identity=As.MCP,
            timeout_s=BUILD_TIMEOUT_S,
            cwd=cwd,
            env=tuple(sorted(child_env.items())),
            user=server.user,
        )

        return self._must(command, what)

    def _must(self, command: Command, what: str) -> str:
        """Run one child, or fail the step with what the child said.

        The child's last stderr lines go into the message, bounded and
        made printable: two `mcp-servers` releases failed with `the staged
        environment could not be created (exit 2)` and nothing else, and
        each cause (`Failed to initialize cache at /nonexistent/.cache/uv`,
        then a directory the user could not traverse) had to be found by
        running the child again by hand.
        """
        try:
            result = self.host.run(command)
        except OSError as exc:
            raise StepFailed(f"{command.argv[0]}: {type(exc).__name__}") from None

        if result.code != 0:
            said = _last_lines(result.stderr)
            raise StepFailed(f"{what} (exit {result.code}){said}")

        return result.stdout


#: How much of a failed child's stderr the ledger carries: the last lines,
#: each cut, and made printable the way every other reported string is.
STDERR_LINES: Final = 3
STDERR_LINE_CHARS: Final = 240


def _last_lines(stderr: str) -> str:
    """`: line | line | line`, or nothing when the child said nothing."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""

    kept = [
        "".join(ch if ch.isprintable() else "?" for ch in line)[:STDERR_LINE_CHARS]
        for line in lines[-STDERR_LINES:]
    ]

    return ": " + " | ".join(kept)


def _open_down_to(root: Path, leaf: Path) -> None:
    """`normalize_dir` on `root` and on every directory below it down to
    `leaf`, inclusive. Nothing under `leaf` is walked, and a `leaf` outside
    `root` is a caller's mistake that stops the release rather than a
    reason to re-mode something else."""
    steps = leaf.resolve().relative_to(root.resolve()).parts
    current = root
    current.mkdir(parents=True, exist_ok=True)
    normalize_dir(current)
    for part in steps:
        current = current / part
        # `work` itself may not exist yet: the request's directory holds
        # the fetched components, and `stage_one` makes the rest. Made here
        # with the unit's umask, then opened, like every other step.
        current.mkdir(exist_ok=True)
        normalize_dir(current)


def _python_of(new: Path) -> Path:
    return new / "bin" / "python"


#: How much of a console script the check below reads. A `#!` line is under
#: 128 bytes, and the `#!/bin/sh` wrapper's own interpreter path sits on the
#: SECOND line, so a one-line read would miss exactly the case that matters.
SCRIPT_HEAD_BYTES: Final = 2048


def names_the_staging_tree(binary: Path, new: Path) -> bool:
    """Assumption 6's check, against the whole script header.

    Two shapes reach here, and both were measured with a real `uv`
    (`test_handover_r7f_server_venv.py`):

    ```
    #!/opt/mcp/kagi.new/bin/python              a short path: line 1
    #!/bin/sh                                   a long one: line 2
    '''exec' '/opt/mcp/kagi.new/bin/python' "$0" "$@"
    ```

    `uv` switches to the second when the path is too long for a `#!` line,
    which is why this looks for the staged tree's own path rather than for
    `.new`: the path is exact, so there is no false positive, and it is
    found wherever in the header `uv` put it.
    """
    with binary.open("rb") as handle:
        head = handle.read(SCRIPT_HEAD_BYTES).decode("utf-8", "replace")

    return any(str(one) in head for one in (new, new.resolve()))


def _sha256_of(name: str, path: Path) -> str:
    """The asset's real hash, read in blocks and capped (assumption 4)."""
    if path.is_symlink() or not path.is_file():
        raise _refuse(name, "the download left no regular file")

    size = path.stat().st_size
    if size > MAX_ASSET_BYTES:
        raise _refuse(name, f"the asset is larger than {MAX_ASSET_BYTES} bytes")

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK_BYTES), b""):
            digest.update(block)

    return digest.hexdigest()


def _read_lock(server: ServerFile, registry: Path) -> str:
    """The committed closure, read out of the registry checkout root has.

    `mcpserver` already refused a `lock` that is absolute, holds `..` or
    ends in `/`. This resolves it anyway and checks containment, because a
    check that rests on a pattern in another module is a check a refactor
    can delete. The file itself must be a regular file: a symlink here is
    how a path that passed every pattern still reads `/etc/shadow`.
    """
    declared = server.pin.lock
    if declared is None:
        raise _refuse(server.name, "declares no closure to install from")

    named = registry / declared
    if named.is_symlink():
        # Before `resolve()`, because the RESOLVED path has already followed
        # the link and this check could never be True there. Containment is
        # still what stops the attack; this is the check the docstring names.
        raise _refuse(server.name, "the closure is a symlink")

    path = named.resolve()
    root = registry.resolve()
    if root not in path.parents:
        raise _refuse(server.name, "the closure resolves outside the registry")

    if not path.is_file():
        raise _refuse(server.name, "the closure is missing or is not a regular file")

    if path.stat().st_size > MAX_LOCK_BYTES:
        raise _refuse(server.name, f"the closure is larger than {MAX_LOCK_BYTES} bytes")

    return path.read_text(encoding="utf-8", errors="replace")


def _closure_of(name: str, text: str, requirement: str) -> tuple[str, ...]:
    """The lock's hash lines, and the two rules it has to pass first.

    Contract 01b §3.4 rule 5: a lock that does not PIN the declared
    requirement is refused. The two came from one pull request, so
    disagreeing means one of them was edited alone. A lock with no hashes
    at all would install whatever the index serves.

    "Pin" is a requirement LINE and not a substring. A
    `--generate-hashes` lock is mostly comments — the compiler writes
    `# via kagimcp` into one itself — so a substring check would pass a
    lock whose real requirement lines install something else while
    `ServerBuild.artifact` and the ledger still name the declared
    package: the phone shows X and the executor deploys Y, which is §6
    row 3 inside the one place row 3 does not cover.
    """
    if not any(_pins(one, requirement) for one in _requirement_lines(text)):
        raise _refuse(name, f"the closure does not pin {safe_token(requirement)}")

    lines = tuple(one.strip() for one in text.splitlines() if HASH_MARKER in one)
    if not lines:
        raise _refuse(name, "the closure carries no hashes")

    return lines


#: What may follow a requirement on its own line: a continuation, an
#: environment marker, or nothing. A version digit may not, or
#: `kagimcp==1.0.21` would answer for `kagimcp==1.0.2`.
PIN_ENDINGS: Final = (" ", "\\", ";", "[")


def _requirement_lines(text: str) -> tuple[str, ...]:
    """The lock's own requirement lines, with comments dropped.

    `uv pip compile --generate-hashes` writes each requirement at column
    zero and puts its hashes and its `# via` note on indented lines under
    it, so anything indented is not a requirement. A line starting with
    `-` is an option, such as `--index-url`.
    """
    kept: list[str] = []
    for line in text.splitlines():
        if not line or line[0].isspace() or line.startswith(("#", "-")):
            continue

        bare = line.split("#", 1)[0].strip()
        if bare:
            kept.append(bare)

    return tuple(kept)


def _pins(line: str, requirement: str) -> bool:
    lowered, wanted = line.lower(), requirement.lower()
    if not lowered.startswith(wanted):
        return False

    rest = lowered[len(wanted) :]

    return not rest or rest.startswith(PIN_ENDINGS)


def _find_entrypoint(unpacked: Path, entrypoint: str) -> Path | None:
    """The binary at the top of the asset, or under its `bin/`. Two places
    and no walk: a search would find a file the archive chose the name of."""
    for candidate in (unpacked / entrypoint, unpacked / "bin" / entrypoint):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate

    return None


def _extract_tar(name: str, archive: Path, into: Path) -> None:
    """Assumption 5. Every member checked before a byte of it is written."""
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    written = 0
    count = 0
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in _members(name, tar):
                count += 1
                if count > MAX_MEMBERS:
                    raise _refuse(name, f"the asset holds more than {MAX_MEMBERS} members")

                written += member.size
                if written > MAX_UNPACKED_BYTES:
                    raise _refuse(name, f"the asset unpacks to more than {MAX_UNPACKED_BYTES}")

                _write_member(name, tar, member, root)
    except tarfile.TarError:
        raise _refuse(name, "the asset is not a readable archive") from None


def _members(name: str, tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    for member in tar:
        _check_member(name, member)
        yield member


def _check_member(name: str, member: tarfile.TarInfo) -> None:
    """The five refused shapes, each with what it would otherwise do."""
    path = Path(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise _refuse(name, "the asset holds a member that leaves its tree")

    if member.issym() or member.islnk():
        # A link is the shape that leaves the tree after extraction rather
        # than during it, so it is refused by kind and not by target.
        raise _refuse(name, "the asset holds a link")

    if not member.isfile() and not member.isdir():
        raise _refuse(name, "the asset holds a member that is not a file or a directory")

    if member.mode & (0o4000 | 0o2000):
        raise _refuse(name, "the asset holds a setuid or setgid member")


def _write_member(name: str, tar: tarfile.TarFile, member: tarfile.TarInfo, root: Path) -> None:
    target = (root / member.name).resolve()
    if target != root and root not in target.parents:
        raise _refuse(name, "the asset holds a member that resolves outside its tree")

    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)

        return

    target.parent.mkdir(parents=True, exist_ok=True)
    source = tar.extractfile(member)
    if source is None:
        raise _refuse(name, "the asset holds a member with no content")

    with target.open("wb") as handle:
        for block in iter(lambda: source.read(HASH_BLOCK_BYTES), b""):
            handle.write(block)

    target.chmod(member.mode & 0o755)
