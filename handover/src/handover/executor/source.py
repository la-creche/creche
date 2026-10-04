"""Where root reads a repository from, and on what terms.

Root never runs git inside `/srv/agents/work/platform/<repo>`. That directory
is the `agent-control` family's READ-WRITE mount — the registry's
`families/agent-control/family.yaml` says so and `docs/rework/spec.md` §4.4
fences it — so a model in that sandbox writes `.git/config`, `.git/hooks` and
`.git/objects/info/alternates` of all three checkouts. The SHA pins WHAT root
builds: it comes from GitHub through the P1 to P5 predicate, not from the
checkout. It does not pin what root's `git` PROCESS does while it reads a
repository whose configuration an attacker wrote.

`/srv/agents/code/<repo>` is the answer: the operator's OWN clone of each of the
three platform repositories, refreshed by `code-corpus-sync.timer` as the operator,
and read-only in every sandbox
that sees it at all (contract 01 §5.5 rule 6, the platform mirror; the family
file's own comment says "the sandbox holds no git credential ... and nothing
on the host may run git inside `work/platform`"). Root already trusts a
checkout the operator owns — `bin/creche-deploy` fetches `/opt/creche`
from the operator's `~/code/agent-control` — so an operator-owned clone no sandbox can
write is inside a trust root already has. A sandbox-writable one is not.

Two controls, and neither replaces the other.

1. `require_trusted` REFUSES a source before any git process starts in it.
   Ownership and mode, on the directory and on its `.git`.
2. `git_ground` and `git_env` carry root's own configuration on every git
   command line, because `safe.directory` — which root needs to read a
   directory it does not own — puts the repository's configuration back in
   play. Measured, git 2.54.0 (Apple Git-157), 2026-09-20: with the
   repository called dubious, `git config --get core.fsmonitor` answers the
   built-in `true`; with `-c safe.directory=<path>` it answers the
   attacker's command.

What each word closes is written beside it below.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from ..catalog import BINARY_BUILD_FILES, CATALOG_BY_NAME, Kind
from ..errors import Refusal, RefusalCode
from ..resolve import TAG_FORMAT

#: The operator's OWN clones, refreshed hourly by `code-corpus-sync.timer` as the operator.
#: Every sandbox that sees this root sees it READ-ONLY (contract 01 §5.5
#: rule 6). It is not `/srv/agents/work/platform`, which is a family's rw
#: mount, and which root must never run git inside.
CORPUS_ROOT: Final = "/srv/agents/code"

#: The one command the operator runs when the corpus does not hold a SHA yet. It is
#: `bin/sync-code-corpus.sh`'s deployed path: ff-only, as the operator, over the
#: repositories `/srv/agents/state/code-repos.txt` names.
REFRESH_COMMAND: Final = "/opt/creche/bin/sync-code-corpus.sh"

#: Where git reads a source's borrowed object stores. Measured on git 2.54.0:
#: `clone --local` copies this file VERBATIM into the clone, so a source that
#: carries one hands root an object store at a path the writer of that file
#: chose. `.git/info/alternates` is NOT the path git reads — measured too, and
#: a clone from a source carrying that one has no alternates at all.
ALTERNATES: Final = ".git/objects/info/alternates"

GIT_DIR: Final = ".git"

#: Write bits this check refuses outright, whoever the owner is.
_OTHER_WRITE: Final = stat.S_IWOTH
_GROUP_WRITE: Final = stat.S_IWGRP

#: Root's own git configuration, as ENVIRONMENT rather than as `-c`, because
#: these three have no `-c` spelling.
#:
#: * `GIT_CONFIG_NOSYSTEM` drops `/etc/gitconfig`, so no third package's
#:   post-install line decides what root's git does.
#: * `GIT_CONFIG_GLOBAL=/dev/null` drops `/root/.gitconfig`. Root's own file
#:   is not hostile, but it is one more place a value comes from, and the
#:   executor must behave the same on a host where nobody ever wrote one.
#: * `GIT_TERMINAL_PROMPT=0` is the other half of `core.askPass`: a prompt in
#:   a unit with no terminal is a release that hangs until `TimeoutStartSec`.
#:
#: Measured 2026-09-20: a clone and a checkout with all three set behave
#: exactly as without them.
_GIT_ENV: Final = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def git_env() -> dict[str, str]:
    """The environment every git child of the executor gets on top of
    `host.child_env`. A copy, so no caller can edit the constant."""
    return dict(_GIT_ENV)


def git_ground(repo: Path) -> tuple[str, ...]:
    """`-c` words for one source path. Command line, so nobody's gitconfig
    changes and nothing on disk decides (`bin/rework-cutover.sh` `tree_git`
    is the precedent, and contract 06 §3.4 is the rule it serves).

    Command-line `-c` beats the repository's own config: measured, git
    2.54.0, `git -c core.fsmonitor=false -C <hostile> config --get
    core.fsmonitor` answers `false`. That is what makes enumerating the
    dangerous keys a defence rather than a wish — an `[include]` in the
    source's config is loaded (measured), and it can set any of them.

    What each word closes:

    * two `safe.directory` entries, because they are not interchangeable.
      Measured: `-c safe.directory=<worktree>` lets `cat-file -e` read a
      repository git calls dubious, and `clone --local` STILL exits 128; the
      clone checks the GITDIR, so `<worktree>/.git` is needed as well. Both
      name one exact path, so no other repository is widened.
    * `core.hooksPath=/dev/null`: every hook in either repository, including
      `post-checkout` and `reference-transaction`, which a checkout runs.
    * `core.fsmonitor=false`: the one setting that runs a command on an index
      refresh. The three commands below never refresh the source's index, so
      this closes the NEXT command somebody adds rather than one today.
    * `core.pager=cat`: a pager runs only with a terminal, and a unit has
      none. It costs one word to stop that from being the reason.
    * `core.sshCommand=false`, `core.askPass=`, `core.editor=false`: three
      more command-shaped values. None is reachable through `--local`, and
      all three are one transport or one interactive verb away.
    * `protocol.ext.allow=never`: `ext::<command>` is a URL that IS a
      command. The source path is a literal here and never a URL, so this
      keeps it that way.

    Two settings are deliberately absent, each measured:

    * `protocol.file.allow`. `--local` works with the SOURCE's own config
      saying `never` (measured), so the attacker cannot wedge the clone and
      root needs no answer to it. Setting `always` would only re-enable
      file-URL submodule cloning, which `--no-checkout` and the absence of
      `--recurse-submodules` already rule out.
    * `uploadpack.packObjectsHook`. `--local` runs no `upload-pack` at all,
      and `--no-local` did not fire it either (measured).

    An alias cannot shadow a built-in git command (measured: `git rev-parse`
    runs the built-in while the config's `rev-parse` alias sits unused), and
    every command below is a built-in named in full.
    """
    return (
        "-c",
        f"safe.directory={repo}",
        "-c",
        f"safe.directory={repo}/{GIT_DIR}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "core.sshCommand=false",
        "-c",
        "core.editor=false",
        "-c",
        "core.askPass=",
        "-c",
        "protocol.ext.allow=never",
    )


def require_trusted(component: str, repo: Path, owner_uid: int, owner_gid: int) -> None:
    """Refuse a source root should not read, BEFORE any git process runs.

    The rule, decided from what the host really has. `/srv/agents/code/<repo>`
    is the operator's, group and owner, mode 0775 today, and no account but the
    operator's is in that group — the fleet's sandbox users are `chaperone`, `mcp-<name>` and the
    per-family sandbox uids, none of which the registry or `bootstrap-root.sh`
    puts in it. So:

    1. The path is a real directory. A symlink is refused: it names a
       directory this check did not read.
    2. Its owner is the configured owner (the operator) or root. Root already trusts
       both — `bin/creche-deploy` fetches `/opt/creche` out of
       the operator's own checkout.
    3. Nothing is writable by OTHER, ever.
    4. Group write is allowed only for the owner's OWN group, which is the
       0775 the corpus really has. Any other group means a second set of
       accounts can write it, and root cannot know which.
    5. The same four rules for `.git`, because that is where the
       configuration and the hooks live. A `.git` FILE is refused: it names a
       gitdir somewhere else, which this check did not read.
    6. No `.git/objects/info/alternates`. Measured: `clone --local` copies it
       verbatim, so root's object store would borrow from a path the writer
       of that file chose. Rules 2 to 4 already stop an attacker writing one;
       this names the file so a reader does not have to work that out.

    `component` is a catalog name, which is the only thing that reaches the
    detail. A path never does: §3.2 rule 4 says a refusal's reason is fixed
    words, and the paths here are a directory an attacker may have named.
    """
    _require_directory(component, repo, owner_uid, owner_gid, "the source")
    _require_directory(component, repo / GIT_DIR, owner_uid, owner_gid, "the source's .git")
    if (repo / ALTERNATES).exists():
        _refuse(component, "the source carries a git alternates file")


def _require_directory(
    component: str, path: Path, owner_uid: int, owner_gid: int, what: str
) -> None:
    try:
        # lstat, not stat: a symlink must be judged as the link it is.
        facts = os.lstat(path)
    except OSError:
        _refuse(component, f"{what} is missing or unreadable")

        return

    if not stat.S_ISDIR(facts.st_mode):
        _refuse(component, f"{what} is not a plain directory")

    if facts.st_uid not in (owner_uid, 0):
        _refuse(component, f"{what} has the wrong owner")

    if facts.st_mode & _OTHER_WRITE:
        _refuse(component, f"{what} is writable by other")

    if facts.st_mode & _GROUP_WRITE and facts.st_gid not in (owner_gid, 0):
        _refuse(component, f"{what} is writable by a group that is not the owner's")


def _refuse(component: str, detail: str) -> None:
    raise Refusal(RefusalCode.SOURCE, component, detail)


# -- reading facts out of a trusted clone ----------------------------------

#: Absolute, for the reason `host.RUNUSER` is: a child's `PATH` is built by
#: `host.child_env` and is not promised to hold a `git`. It is spelled here
#: and imported by `install.py` and `corpus/`, so the three readers of a
#: repository name one binary (`handover/AGENTS.md`, pain 1).
GIT: Final = "/usr/bin/git"

#: One git read, as the two callers can both provide it: an argv LIST and the
#: directory it runs in, answering the child's stdout or None for ANY failure.
#: Root's runner is `Host.run`; the requester's is `corpus`'s own child. No
#: implementation of this lives here, because `host.py` is the one module in
#: `executor/` that starts a child.
GitRunFn = Callable[[Sequence[str], Path], str | None]

#: `git tag --list` prints one name per line, and a clone's tag namespace
#: grows with the repository. The cap is here so a corpus with a planted tag
#: flood cannot be read into memory.
MAX_TAG_LINES: Final = 4096

#: What a component's `input_digest` is computed over (contract 06 §9): its
#: own source subtree, and the lock file that decides what its build
#: installs. A component whose subtree IS the whole repository already
#: carries the lock file inside it.
LOCK_FILE: Final = "uv.lock"
WHOLE_REPO: Final = "."

#: A path that does not exist at that commit. It is written down rather than
#: skipped, so the digest of a component that GAINS a lock file differs from
#: the digest of the same subtree before it.
ABSENT_OBJECT: Final = "-"

DIGEST_PREFIX: Final = "sha256:"

#: What `git rev-parse` prints for a tree or a blob.
OBJECT_ID_RE: Final = re.compile(r"[0-9a-f]{40}")

#: One page of `git tag --list` output is the whole answer, so the only
#: pattern needed here is the tag grammar itself, which `allocate.py` owns.
_TAG_RE: Final = re.compile(r"([a-z][a-z0-9-]{1,30})-v([0-9]+)\.([0-9]+)\.([0-9]+)")


def newest_tagged_version(names: Sequence[str], component: str) -> str | None:
    """The highest `MAJOR.MINOR.PATCH` among this component's tags, or None.

    A name that is not `<component>-vX.Y.Z` is ignored rather than refused.
    The repository's own history carries `v*` and `schema-v*`, which contract
    06 §2 does not reuse, and neither must read as a version of anything.
    """
    found: list[tuple[int, int, int]] = []
    for name in names:
        matched = _TAG_RE.fullmatch(name)
        if matched is None or matched.group(1) != component:
            continue

        found.append((int(matched.group(2)), int(matched.group(3)), int(matched.group(4))))

    if not found:
        return None

    highest = max(found)

    return f"{highest[0]}.{highest[1]}.{highest[2]}"


def tag_names(run: GitRunFn, repo: Path, component: str) -> tuple[str, ...]:
    """Every `<component>-v*` tag a clone holds. Empty for any failure.

    The caller decides what an empty answer means. For the requester it is a
    printed note, because root re-derives the same list from GitHub.
    """
    prefix = f"{component}-v"
    printed = run([GIT, *git_ground(repo), "-C", str(repo), "tag", "--list", f"{prefix}*"], repo)
    if printed is None:
        return ()

    lines = printed.splitlines()[:MAX_TAG_LINES]

    return tuple(line.strip() for line in lines if line.strip())


def tag_sha(run: GitRunFn, repo: Path, component: str, version: str) -> str | None:
    """The commit a clone's own `<component>-v<version>` tag names.

    This is the REQUESTER's answer and never root's. Root reads the same
    fact from GitHub (`provenance.tag_commit`), because a tag in a clone is
    a name operator-side code can write and a Release is not.
    """
    tag = TAG_FORMAT.format(name=component, version=version)
    argv = [GIT, *git_ground(repo), "-C", str(repo), "rev-parse", "--verify", f"{tag}^{{commit}}"]
    printed = run(argv, repo)
    if printed is None:
        return None

    found = printed.strip()
    if not OBJECT_ID_RE.fullmatch(found):
        return None

    return found


def digest_paths(component: str) -> tuple[str, ...]:
    """Contract 06 §9's digest inputs for one component, in a fixed order."""
    row = CATALOG_BY_NAME[component]
    path = row.path
    if path == WHOLE_REPO:
        return (WHOLE_REPO,)

    # A binary build reads the Cargo workspace files and no `uv.lock`, so
    # those are its lock files (`catalog.BINARY_BUILD_FILES`).
    if row.kind is Kind.BINARY:
        return tuple(sorted((path, *BINARY_BUILD_FILES)))

    return tuple(sorted((path, LOCK_FILE)))


def _object_spec(sha: str, path: str) -> str:
    """What `git rev-parse` is asked for. The whole repository is its root
    tree, and every other path is the object at that path in that commit."""
    if path == WHOLE_REPO:
        return f"{sha}^{{tree}}"

    return f"{sha}:{path}"


def has_commit(run: GitRunFn, repo: Path, sha: str) -> bool:
    """Whether the clone holds this commit. The corpus is refreshed hourly,
    so a release filed minutes after a merge names one it has not got."""
    argv = [GIT, *git_ground(repo), "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"]

    return run(argv, repo) is not None


def object_id(run: GitRunFn, repo: Path, sha: str, path: str) -> str | None:
    """The git object id of one path at one commit.

    The caller has already proved the commit is there, so a failure here is
    a path that does not exist AT that commit: `ABSENT_OBJECT`, a fact about
    the commit rather than a failure. None is kept for the root tree, which
    a commit that exists always has.
    """
    spec = _object_spec(sha, path)
    argv = [GIT, *git_ground(repo), "-C", str(repo), "rev-parse", "--verify", spec]
    printed = run(argv, repo)
    if printed is None:
        return None if path == WHOLE_REPO else ABSENT_OBJECT

    found = printed.strip()
    if not OBJECT_ID_RE.fullmatch(found):
        return None if path == WHOLE_REPO else ABSENT_OBJECT

    return found


def digest_of(rows: Sequence[tuple[str, str]]) -> str:
    """Contract 06 §9's `input_digest`, from `(path, object id)` pairs.

    The recipe is written down so a second machine reproduces it with git
    alone: sort by path, write one `<path> <object id>\\n` line each, and
    take the SHA-256 of those bytes. A git object id is content addressed,
    so the same commit gives the same digest on every machine that holds it.
    """
    text = "".join(f"{path} {found}\n" for path, found in sorted(rows))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return f"{DIGEST_PREFIX}{digest}"


def input_digest(run: GitRunFn, repo: Path, component: str, sha: str) -> str | None:
    """One component's `input_digest` out of a clone that holds `sha`.

    None means the clone does not hold `sha`, which on the host means the
    corpus has not fetched that commit yet. The fetch a moment later refuses
    the release with the command that fixes it
    (`install.Installer.fetch`), so a null digest never reaches a release
    that deploys.
    """
    if not has_commit(run, repo, sha):
        return None

    rows: list[tuple[str, str]] = []
    for path in digest_paths(component):
        found = object_id(run, repo, sha, path)
        if found is None:
            return None

        rows.append((path, found))

    return digest_of(rows)
