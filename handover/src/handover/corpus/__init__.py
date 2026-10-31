"""Bring the corpus up to the commit a release is about to name.

`/srv/agents/code/<repo>` is refreshed hourly by `code-corpus-sync.timer`,
and `executor/source.py` makes it the only place root reads a repository from.
So a release filed two minutes after a merge names a SHA the corpus does not
hold yet, and root refuses it. That refusal is correct and it is not enough:
the operator would read it out of `done/<ULID>.json` after the fact.

`handover request` runs as the operator, in the operator's own directory, with
the operator's own credentials. So it fetches first. This module is that fetch and
nothing else, and its child `artifacts.py` fetches the artifacts (rule 5).

**It is the fourth actor** of `stage7-releases.md` §2.1, and `handover/AGENTS.md`
names it beside the other three. Five rules hold its shape, and each one is
the reason it is a package of its own rather than four lines in `cli.py`.

1. **It runs ONE verb, `git fetch`, in ONE root, `/srv/agents/code`.** No
   checkout, no merge, no push, no second path. A fetch adds objects and
   moves remote-tracking refs. It cannot change a work tree, so it cannot
   change what any other process on the host is reading.
2. **It refuses to run as root.** Root holds no git credential by design
   (contract 06 §3.4), and root running git in a directory it does not own is
   the hazard `executor/source.py` fences. A `euid` of 0 is a report, never
   a run.
3. **Nothing here ever raises.** The corpus being behind is a release root
   refuses with a reason the operator can act on. A requester that refused to FILE
   because a fetch failed would turn a network blip into a command the operator
   cannot run at all.
4. **The PEP never imports it.** The PEP's `release` verb runs as `chaperone`, which
   owns no corpus and holds no credential, and this module starts children.
   `handover/tests/test_handover_r7j_freshness.py` asserts it for `requester/`.
5. **It has a second child, `curl`, and a second root, the store.**
   `artifacts.py` fetches the two assets of the Release of a binary
   component into the store of artifacts below the home of the operator
   (`executor/source.artifact_store`). Rule 1 is the rule of the git child.
   Rules 2 to 4 hold for the second child too. `run_capped` in this module
   starts it, with a time limit and a limit on the size of each file that
   it writes. Root reads the store later and checks each name and each mode
   itself.

It carries the executor's own `git_ground` and `git_env`, because this is the
same directory root reads a moment later and the operator's own gitconfig must not
decide what happens in it either. One word is overridden: the ground's
`core.sshCommand=false` suits root, which never fetches, and this fetch is an
ssh transport (`SSH`).
"""

from __future__ import annotations

import os
import resource
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..catalog import CATALOG_BY_NAME
from ..errors import Refusal
from ..executor.live_state import REGISTRY_DATA, Readers
from ..executor.source import (
    CORPUS_ROOT,
    GIT,
    REFRESH_COMMAND,
    git_env,
    git_ground,
    head_commit,
    input_digest,
    newest_tagged_version,
    require_trusted,
    tag_names,
    tag_sha,
)

#: `GIT` is `executor/source.py`'s, absolute for the same reason it is there:
#: `PATH` is the caller's here, and a `git` earlier on it would be a git the
#: caller chose.
FETCH_TIMEOUT_S: Final = 120.0

#: The ssh this fetch runs, named on the command line after the ground, where
#: the last `-c` wins. The corpus clones' remotes are `git@github.com:…`,
#: and the ground's `core.sshCommand=false` would make git run `false` as its
#: ssh: every fetch would exit 128. Absolute and on the command line for
#: the ground's own reason: no config file chooses the command.
SSH: Final = "/usr/bin/ssh"

#: What `request` prints before it files, so the operator reads what happened
#: rather than inferring it from a later refusal. It names the root the
#: caller passed, because a hard-coded path in a line a test reads is a
#: line that lies on every host but one.
FETCH_NOTE: Final = "refreshing {root} (root reads the release source from there)"

#: Said once when the corpus could not be refreshed. It names the command
#: rather than the error, because the error is git's and the command is the
#: reader's next action.
BEHIND_NOTE: Final = (
    f"the corpus may be behind; if the release refuses, run {REFRESH_COMMAND} and file again"
)

ROOT_REFUSAL: Final = "refusing to refresh the corpus as root: it is the operator's directory"

#: The `PATH` of each child when the caller has none, and the `LANG` of each
#: child. Each child of this package takes them from here.
_PATH_FALLBACK: Final = "/usr/local/bin:/usr/bin:/bin"
_CHILD_LANG: Final = "C.UTF-8"

#: `git fetch` and nothing else. `--tags` because a provenance check resolves
#: a tag, and a tag that arrived after the last hourly sync is the same
#: staleness this module exists for.
_VERB: Final = ("fetch", "--quiet", "--tags", "origin")

#: One child, described the way `executor/host.Command` describes one: an
#: argv LIST, a working directory, a whole environment, a timeout. A test
#: substitutes it to read the argv without a network.
RunFn = Callable[[list[str], Path, dict[str, str], float], int]


@dataclass(frozen=True)
class CorpusReport:
    """What the refresh did. `ok` is false for any repository that did not
    fetch, and `lines` is what the caller prints."""

    ok: bool
    lines: list[str]


def repos_of(components: Iterable[str]) -> tuple[str, ...]:
    """Which of the three repositories a request touches, in a fixed order.

    A name the catalog does not hold is SKIPPED, not guessed: `parse_request`
    refuses it a moment later with root's own reason, and inventing a
    repository name from a caller's string is how a path leaves the three.
    """
    found = {str(CATALOG_BY_NAME[name].repo) for name in components if name in CATALOG_BY_NAME}

    return tuple(sorted(found))


def refresh(
    repos: Iterable[str],
    root: Path = Path(CORPUS_ROOT),
    run: RunFn | None = None,
    euid: int | None = None,
) -> CorpusReport:
    """One `git fetch` per repository. Never raises, whatever happens."""
    if (os.geteuid() if euid is None else euid) == 0:
        return CorpusReport(False, [ROOT_REFUSAL])

    runner = run if run is not None else _run
    lines: list[str] = []
    ok = True
    for name in repos:
        failed = _one(name, root / name, runner)
        if failed is None:
            lines.append(f"fetched {name}")
            continue

        lines.append(f"{name}: {failed}")
        ok = False

    return CorpusReport(ok, lines)


def _one(name: str, repo: Path, runner: RunFn) -> str | None:
    """Fetch one. Answers None for success, or a fixed phrase for the
    failure — never git's own stderr, which is text from a directory this
    process did not write."""
    del name
    if not (repo / ".git").is_dir():
        return "no such checkout in the corpus"

    argv = [GIT, *git_ground(repo), "-c", f"core.sshCommand={SSH}", "-C", str(repo), *_VERB]
    try:
        code = runner(argv, repo, git_env(), FETCH_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return "the fetch could not start"

    if code != 0:
        return f"the fetch exited {code}"

    return None


def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout_s: float) -> int:
    """The real child. No shell: an argv list, and there is no code path
    here that takes a string (`stage7-releases.md` §3.2 rule 6).

    The environment is the caller's `PATH` and `HOME` plus root's own git
    ground. `HOME` stays, because the operator's credential helper lives under it
    and this fetch is exactly the one that needs the operator's credentials.
    """
    whole = {
        "PATH": os.environ.get("PATH", _PATH_FALLBACK),
        "HOME": os.environ.get("HOME", ""),
        "LANG": _CHILD_LANG,
        **env,
    }
    done = subprocess.run(
        argv,
        cwd=str(cwd),
        env=whole,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )

    return done.returncode


# -- the child of a download ---------------------------------------------

#: One download child: an argv LIST, a time limit in seconds, and the
#: largest file that the child may write. It answers the exit status. A child
#: that does not start raises `OSError`, and a child past its time limit
#: raises `TimeoutError`. A test substitutes it to read the argv without a
#: network.
CappedRunFn = Callable[[list[str], float, int], int]


def run_capped(argv: list[str], timeout_s: float, max_file_bytes: int) -> int:
    """The real download child, with no shell and no text of the child kept.

    The child writes no file larger than `max_file_bytes`. The kernel
    counts the bytes as the child writes them, so the limit holds also when
    a server sends no length (`executor/host.py`, `_limit_writes`). The
    environment holds `PATH` and `LANG` only: the child needs no credential.
    """
    whole = {"PATH": os.environ.get("PATH", _PATH_FALLBACK), "LANG": _CHILD_LANG}
    try:
        done = subprocess.run(
            argv,
            env=whole,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
            preexec_fn=_limit_writes(max_file_bytes),
        )
    except subprocess.TimeoutExpired as late:
        raise TimeoutError("the child passed its time limit") from late
    except subprocess.SubprocessError as fault:
        raise OSError("the child could not start") from fault

    return done.returncode


def _limit_writes(max_bytes: int) -> Callable[[], None]:
    """`RLIMIT_FSIZE` for one child, applied between fork and exec. A hard
    limit below `max_bytes` stays: an account cannot raise its hard limit.

    `preexec_fn` is not safe in a process with threads. Each command of
    this package runs in one process with no thread of its own.
    """
    _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    cap = max_bytes if hard == resource.RLIM_INFINITY else min(max_bytes, hard)

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))

    return apply


# -- what the requester can say about a tag ------------------------------

#: One `git tag --list` or `git rev-parse` in a clone. Both print a line and
#: exit, so the cap is a hung child rather than a slow one.
READ_TIMEOUT_S: Final = 30.0

#: Said once when the corpus cannot be read at all. Root re-derives every
#: one of these answers, so the requester prints the line and files anyway.
UNREADABLE_NOTE: Final = "the corpus could not be read here; root resolves it again at step 2"


def read_git(argv: Sequence[str], cwd: Path) -> str | None:
    """`source.GitRunFn` as the operator: one git read, stdout or None.

    It is the second verb this package runs and it is still not a write:
    `tag --list` and `rev-parse` read refs and objects. `_run` cannot serve
    here because a fetch's caller wants an exit code and these callers want
    the output.
    """
    try:
        done = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=READ_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if done.returncode != 0:
        return None

    return done.stdout


def _child_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", _PATH_FALLBACK),
        "HOME": os.environ.get("HOME", ""),
        "LANG": _CHILD_LANG,
        **git_env(),
    }


def local_readers(root: Path = Path(CORPUS_ROOT)) -> Readers:
    """Contract 06 §11's off-disk answers, as the operator can get them.

    Every one of them comes out of the corpus clone, and none of them is a
    decision: root re-derives `latest`, the commit and the digest at step 2
    from GitHub and from the same corpus. So this side never refuses. A
    clone it cannot vouch for, a tag that is not there, a commit the clone
    has not fetched — each answers None, the builder records a note, and
    `cli.py` prints it above the request it files anyway.

    `require_trusted` runs for the same reason root runs it: `git_ground`
    disarms the settings that name a command, and the ownership rule is
    what keeps a clone somebody else writes from being read at all.
    """

    def _repo_of(component: str) -> Path | None:
        repo = str(CATALOG_BY_NAME[component].repo)
        path = root / repo
        try:
            require_trusted(repo, path, os.getuid(), os.getgid())
        except Refusal:
            return None

        return path

    def newest(component: str) -> str | None:
        path = _repo_of(component)
        if path is None:
            return None

        return newest_tagged_version(tag_names(read_git, path, component), component)

    def sha_of(component: str, version: str) -> str | None:
        path = _repo_of(component)
        if path is None:
            return None

        return tag_sha(read_git, path, component, version)

    def digest(component: str, sha: str) -> str | None:
        path = _repo_of(component)
        if path is None:
            return None

        return input_digest(read_git, path, component, sha)

    def registry_head() -> str | None:
        """The commit at `HEAD` of the registry clone (contract 06 §9.2
        rule 2), the commit root reads at step 2."""
        path = _repo_of(REGISTRY_DATA)
        if path is None:
            return None

        return head_commit(read_git, path)

    return Readers(
        newest_version=newest,
        tag_sha=sha_of,
        input_digest=digest,
        registry_head=registry_head,
    )


__all__ = [
    "BEHIND_NOTE",
    "CORPUS_ROOT",
    "FETCH_NOTE",
    "FETCH_TIMEOUT_S",
    "UNREADABLE_NOTE",
    "CappedRunFn",
    "CorpusReport",
    "RunFn",
    "local_readers",
    "read_git",
    "refresh",
    "repos_of",
    "run_capped",
]
