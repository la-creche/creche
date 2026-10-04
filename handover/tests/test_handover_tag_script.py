"""`handover/bin/allocate-tags.sh` against a real git repository.

The planner is pure and tested on its own. This module tests the script that
feeds it: what a merge changed, which labels the pull request carried, and
the tag it then creates and pushes. Git is real. `gh` is a fake on PATH, the
way every other external thing in this repo is faked.

The workflow never runs from this branch, so this is where its logic is
proved. It is also where contract 06 §2.1's third row lives: `git tag`
refusing an existing ref.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from handover.catalog import ARRIVING, CATALOG, RETIRING, Repo

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "handover" / "bin" / "allocate-tags.sh"

#: Contract 06 §1's agent-control rows, in catalog order, minus a retiring
#: or an arriving one: the planner never tags either.
COMPONENTS = tuple(
    row.name
    for row in CATALOG
    if row.repo is Repo.AGENT_CONTROL and row.name not in RETIRING | ARRIVING
)

#: Every one of them at its first version, sorted as `_tags` returns them.
FIRST_TAGS = sorted(f"{name}-v0.1.0" for name in COMPONENTS)

#: Contract 06 §1's agent-mcp rows. One today, `mcp-servers`, whose `path` is
#: `.`: the whole repository, so any change at all reaches it.
MCP_COMPONENTS = tuple(row.name for row in CATALOG if row.repo is Repo.AGENT_MCP)

MCP_FIRST_TAGS = sorted(f"{name}-v0.1.0" for name in MCP_COMPONENTS)

#: The script's own `uv run` default would build a second environment. Point
#: it at the interpreter already running the tests instead, so this passes
#: both under `uv run --project release` and under the registered workspace.
RELEASE_CLI = f"{sys.executable} -m handover.cli"

#: A `gh` that answers from the environment. The script asks it about the
#: pull request that merged the commit, asks which merged pull requests carry
#: a bump label, and tells it to publish a Release.
#:
#: `FAKE_BUMPS` is `<merge sha>=<label>` words: what `gh pr list --state
#: merged --label <label>` answers, one merge commit per line.
#:
#: `FAKE_GH_STATUS` is a real `gh` that cannot read: on any HTTP error it
#: prints the response BODY on stdout and exits non-zero. Both halves matter,
#: so the fake does both.
GH_FAKE = """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "release" ]]; then
  printf '%s\\n' "$*" >> "$GH_LOG"
  exit 0
fi
if [[ -n "${FAKE_GH_STATUS:-}" ]]; then
  printf '%s\\n' '{"message": "Bad credentials", "status": "401"}'
  exit 1
fi
listing=""
[[ "$1" == "pr" ]] && listing=yes
jq=""
label=""
while [[ $# -gt 0 ]]; do
  [[ "$1" == "--jq" ]] && jq="$2"
  [[ "$1" == "--label" ]] && label="$2"
  shift
done
if [[ -n "$listing" ]]; then
  for bump in ${FAKE_BUMPS:-}; do
    [[ "${bump#*=}" == "$label" ]] && printf '%s\\n' "${bump%%=*}"
  done
  exit 0
fi
[[ -n "${FAKE_PR:-}" ]] || exit 0
case "$jq" in
  *title*)  printf '%s\\n' "${FAKE_BODY:-a merged pull request (#1)}" ;;
  *)        printf '%s\\n' '{"number": 1}' ;;
esac
"""


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )

    return done.stdout.strip()


def _commit(repo: Path, *paths: str) -> str:
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path}\n", encoding="utf-8")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"touch {paths[0]}")

    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with an origin, the script in place, and one base commit."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "ci@example.invalid")
    _git(work, "config", "user.name", "ci")
    _git(work, "remote", "add", "origin", str(origin))

    script = work / "handover" / "bin" / "allocate-tags.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    _commit(work, "README.md")
    _git(work, "push", "-q", "origin", "HEAD")

    return work


@pytest.fixture
def other_repo(tmp_path: Path) -> Path:
    """A second checkout, with no copy of the script and its own origin.

    agent-mcp's workflow is exactly this shape: agent-control is checked out
    beside agent-mcp's own tree and its script tags THAT tree. The origin is
    named `agent-mcp.git` so the run can also find the repository name with
    no `GITHUB_REPOSITORY` set, the way a developer's checkout does.
    """
    origin = tmp_path / "agent-mcp.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    work = tmp_path / "mcp-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "ci@example.invalid")
    _git(work, "config", "user.name", "ci")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "README.md")
    _git(work, "push", "-q", "origin", "HEAD")

    return work


@pytest.fixture
def shims(tmp_path: Path) -> Path:
    """A PATH directory holding the fake `gh`."""
    binaries = tmp_path / "shims"
    binaries.mkdir()
    fake = binaries / "gh"
    fake.write_text(GH_FAKE, encoding="utf-8")
    fake.chmod(0o755)

    return binaries


#: `_run`'s default repository. `None` unsets `GITHUB_REPOSITORY` instead,
#: which is a developer's own checkout: the script then reads the tagged
#: tree's `origin` remote.
DEFAULT_REPOSITORY = "example-owner/agent-control"


def _run(
    repo: Path,
    shims: Path,
    tmp_path: Path,
    *args: str,
    labels: str = "",
    bumps: dict[str, str] | None = None,
    pr: str = "",
    repository: str | None = DEFAULT_REPOSITORY,
    tree: Path | None = None,
    gh_reads: bool = True,
    tag_repo: str | None = None,
    cli: str = RELEASE_CLI,
) -> subprocess.CompletedProcess[str]:
    """The script in `repo`, tagging `tree` (itself, unless one is given).

    `labels` are the bump labels of the pull request that merged the tagged
    tree's HEAD. `bumps` names any other merge commit's label.
    """
    asked = dict(bumps or {})
    if labels:
        asked[_git(tree or repo, "rev-parse", "HEAD")] = labels

    env = dict(os.environ)
    env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
    env["RELEASE_CLI"] = cli
    env["GH_LOG"] = str(tmp_path / "gh.log")
    env["FAKE_PR"] = pr
    env["FAKE_BUMPS"] = " ".join(f"{sha}={label}" for sha, label in asked.items())
    env["FAKE_GH_STATUS"] = "" if gh_reads else "401"
    env.pop("GITHUB_SHA", None)
    env.pop("GITHUB_REPOSITORY", None)
    env.pop("TAG_REPO_ROOT", None)
    env.pop("TAG_REPO", None)
    if repository is not None:
        env["GITHUB_REPOSITORY"] = repository

    if tag_repo is not None:
        env["TAG_REPO"] = tag_repo

    if tree is not None:
        env["TAG_REPO_ROOT"] = str(tree)

    return subprocess.run(
        ["bash", str(repo / "handover" / "bin" / "allocate-tags.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def _tags(repo: Path) -> list[str]:
    return sorted(line for line in _git(repo, "tag", "--list").splitlines() if line)


def _seed_first_tags(repo: Path) -> None:
    """Put every component's first tag on the base commit.

    The planner tags an untagged component whatever the commit changed
    (`plan_tags`), so a test about the PATH rule seeds the
    bootstrap away first and then reads one component's next number.
    """
    base = _git(repo, "rev-parse", "HEAD")
    for name in COMPONENTS:
        _git(repo, "tag", f"{name}-v0.1.0", base)


# ---- the first tags --------------------------------------------------------


@pytest.mark.slow
def test_a_first_run_tags_every_component_that_has_none(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """Item 1: a repository with no component tag gets them all at once, so
    the operator has a `noticeboard-v…` to name without waiting for a merge under `noticeboard/`."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "released chaperone-v0.1.0" in done.stdout
    assert "released noticeboard-v0.1.0" in done.stdout
    assert _tags(repo) == FIRST_TAGS


@pytest.mark.slow
def test_the_first_run_is_idempotent(repo: Path, shims: Path, tmp_path: Path) -> None:
    """A second dispatch on the same commit is the safe thing to do."""
    _commit(repo, "chaperone/src/one.py")
    assert _run(repo, shims, tmp_path).returncode == 0

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "allocate-tags: 0 created" in done.stdout
    assert _tags(repo) == FIRST_TAGS


@pytest.mark.slow
def test_after_the_bootstrap_only_the_changed_component_moves(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """The rule extinguishes itself: one tag per component, once."""
    _commit(repo, "chaperone/src/one.py")
    assert _run(repo, shims, tmp_path).returncode == 0

    _commit(repo, "chaperone/src/two.py")
    assert _run(repo, shims, tmp_path).returncode == 0

    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.1.1"])


@pytest.mark.slow
def test_a_dry_run_shows_every_first_tag_and_writes_none(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """What the operator reads before starting the real dispatch."""
    _commit(repo, "docs/rework/design.md")

    done = _run(repo, shims, tmp_path, "--dry-run")

    assert done.returncode == 0, done.stderr
    for tag in FIRST_TAGS:
        assert f"would create {tag}" in done.stdout

    assert _tags(repo) == []


@pytest.mark.slow
def test_an_unknown_argument_stops_the_run(repo: Path, shims: Path, tmp_path: Path) -> None:
    """A typo for `--dry-run` must not be a real run. The script creates
    tags and Releases, and a silently ignored argument is how a reader who
    meant to look ends up having allocated."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, "--dryrun")

    assert done.returncode != 0
    assert "usage" in done.stderr
    assert _tags(repo) == []


# ---- the path rule, on a repository that is already bootstrapped -----------


@pytest.mark.slow
def test_two_merges_in_a_row_both_get_a_tag(repo: Path, shims: Path, tmp_path: Path) -> None:
    """Pain 1, end to end: neither merge leaves the branch tip untagged."""
    _seed_first_tags(repo)
    first = _commit(repo, "chaperone/src/one.py")
    assert _run(repo, shims, tmp_path).returncode == 0

    second = _commit(repo, "chaperone/src/two.py")
    assert _run(repo, shims, tmp_path).returncode == 0

    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.1.1", "chaperone-v0.1.2"])
    assert _git(repo, "rev-list", "-n1", "chaperone-v0.1.1") == first
    assert _git(repo, "rev-list", "-n1", "chaperone-v0.1.2") == second


@pytest.mark.slow
def test_a_rerun_on_one_commit_creates_nothing(repo: Path, shims: Path, tmp_path: Path) -> None:
    _seed_first_tags(repo)
    _commit(repo, "chaperone/src/one.py")
    assert _run(repo, shims, tmp_path).returncode == 0

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0
    assert "already on this commit" in done.stdout
    assert "allocate-tags: 0 created" in done.stdout
    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.1.1"])


@pytest.mark.slow
def test_a_docs_only_merge_produces_no_tag(repo: Path, shims: Path, tmp_path: Path) -> None:
    _seed_first_tags(repo)
    _commit(repo, "docs/rework/design.md")

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0
    assert "no component touched" in done.stdout
    assert _tags(repo) == FIRST_TAGS


@pytest.mark.slow
def test_a_bump_label_raises_the_level(repo: Path, shims: Path, tmp_path: Path) -> None:
    _seed_first_tags(repo)
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, labels="bump:minor", pr="yes")

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.2.0"])


@pytest.mark.slow
def test_a_label_raises_only_the_component_its_merge_changed(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """A merge queue lands two pull requests in one push, so one run reads
    both. The labelled one changed `attendance/`, the other `chaperone/`: the label
    is `attendance`'s alone, though both ranges hold that merge."""
    _seed_first_tags(repo)
    labelled = _commit(repo, "attendance/src/one.py")
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, bumps={labelled: "bump:minor"}, pr="yes")

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.1.1", "attendance-v0.2.0"])


@pytest.mark.slow
def test_a_label_from_before_the_range_raises_nothing(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """A labelled merge the component's newest tag already covers asked for
    that tag's level, not for the next one's."""
    covered = _commit(repo, "chaperone/src/one.py")
    _seed_first_tags(repo)
    _commit(repo, "chaperone/src/two.py")

    done = _run(repo, shims, tmp_path, bumps={covered: "bump:major"}, pr="yes")

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.1.1"])


@pytest.mark.slow
def test_a_bump_label_never_raises_a_first_tag(repo: Path, shims: Path, tmp_path: Path) -> None:
    """A component that has never released has no number to bump."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, labels="bump:major", pr="yes")

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == FIRST_TAGS


@pytest.mark.slow
def test_a_set_of_components_each_get_their_own_tag(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    _seed_first_tags(repo)
    _commit(repo, "chaperone/src/one.py", "attendance/src/two.py")

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.1.1", "attendance-v0.1.1"])


@pytest.mark.slow
def test_a_tag_the_remote_already_holds_stops_the_run(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """Contract 06 §2.1's third row, where it can actually happen.

    The planner's target is always above every tag it was shown, so only a
    tag this checkout has not fetched can collide. Here the remote carries
    `chaperone-v0.1.0` on another commit and this checkout does not.
    """
    origin = tmp_path / "origin.git"
    base = _git(repo, "rev-parse", "HEAD")
    _git(origin, "tag", "chaperone-v0.1.0", base)
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path)

    assert done.returncode != 0
    assert "conflict: chaperone-v0.1.0 exists on the remote" in done.stderr


@pytest.mark.slow
def test_dry_run_writes_no_tag(repo: Path, shims: Path, tmp_path: Path) -> None:
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, "--dry-run")

    assert done.returncode == 0
    assert "would create chaperone-v0.1.0" in done.stdout
    assert _tags(repo) == []


# ---- one range per component -----------------------------------------------


@pytest.mark.slow
def test_a_missed_dispatch_still_tags_the_earlier_merge(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """A missed run, end to end.

    A red `main` tags nothing, so a missed run is a normal case. Merge A
    changes `noticeboard/` and its run is missed. Merge B changes only `docs/`, and
    its run passes. The head commit's own diff names no
    component, so a planner reading it alone would never tag `noticeboard`, and
    nothing would say so.
    """
    _seed_first_tags(repo)
    _commit(repo, "noticeboard/src/one.py")
    _commit(repo, "docs/rework/design.md")

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "noticeboard-v0.1.1"])


@pytest.mark.slow
def test_each_component_reads_its_own_window(repo: Path, shims: Path, tmp_path: Path) -> None:
    """Two components, two different newest tags, two different ranges."""
    _seed_first_tags(repo)
    _commit(repo, "noticeboard/src/one.py")
    _commit(repo, "chaperone/src/one.py")
    assert _run(repo, shims, tmp_path).returncode == 0
    assert _tags(repo) == sorted([*FIRST_TAGS, "chaperone-v0.1.1", "noticeboard-v0.1.1"])

    # `noticeboard`'s window now starts at noticeboard-v0.1.1, so the earlier noticeboard/ change is
    # behind it and only the new one counts. `chaperone` saw nothing at all.
    _commit(repo, "noticeboard/src/two.py")
    assert _run(repo, shims, tmp_path).returncode == 0

    assert _tags(repo) == sorted(
        [*FIRST_TAGS, "chaperone-v0.1.1", "noticeboard-v0.1.1", "noticeboard-v0.1.2"]
    )


@pytest.mark.slow
def test_a_tag_that_is_not_an_ancestor_refuses_that_component(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """Item 4. A tag made by hand on a side branch, or left behind by a
    force-push, is not in this commit's history. Diffing against it would
    describe a range that never happened, so that one component refuses and
    the others are tagged."""
    _seed_first_tags(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "chaperone/src/side.py")
    _git(repo, "tag", "chaperone-v0.2.0", side)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "chaperone/src/one.py", "noticeboard/src/one.py")

    done = _run(repo, shims, tmp_path)

    assert done.returncode != 0
    assert "chaperone-v0.2.0 is not an ancestor" in done.stderr
    assert "noticeboard-v0.1.1" in _tags(repo)
    assert "chaperone-v0.2.1" not in _tags(repo)


@pytest.mark.slow
def test_the_dry_run_prints_the_range_per_component(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """Item 5. What the operator reads before starting the real dispatch: every
    component's window, not only the ones that moved."""
    _seed_first_tags(repo)
    _commit(repo, "noticeboard/src/one.py")
    _commit(repo, "docs/rework/design.md")

    done = _run(repo, shims, tmp_path, "--dry-run")

    assert done.returncode == 0, done.stderr
    assert "noticeboard: 2 commit(s) since noticeboard-v0.1.0" in done.stdout
    assert "handover: 2 commit(s) since handover-v0.1.0" in done.stdout
    assert "would create noticeboard-v0.1.1" in done.stdout
    assert "would create handover" not in done.stdout
    # `chaperone` has no `chaperone/` here, but its build installs `release/`, which the
    # fixture holds (contract 06 §1 rule 9), so its range is read.
    assert "chaperone: 2 commit(s) since chaperone-v0.1.0" in done.stdout
    # Neither `caregiver/` nor `family/` exists, so no range can be read for
    # `caregiver`. A skipped component is said out loud, never left out.
    assert "caregiver: no path in this tree" in done.stdout
    assert _tags(repo) == FIRST_TAGS
    assert not (tmp_path / "gh.log").exists()


@pytest.mark.slow
def test_the_release_body_names_the_range(repo: Path, shims: Path, tmp_path: Path) -> None:
    """Item 3. A tag made by a late dispatch covers more than its own
    commit, so the Release says how much more."""
    _seed_first_tags(repo)
    _commit(repo, "noticeboard/src/one.py")
    _commit(repo, "noticeboard/src/two.py")

    done = _run(repo, shims, tmp_path, pr="yes")

    assert done.returncode == 0, done.stderr
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "noticeboard-v0.1.1" in log
    assert "2 commit(s) since noticeboard-v0.1.0" in log


@pytest.mark.slow
def test_the_body_names_the_pull_requests_in_the_range(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """Item 2. A tag covers a range, so the body names every pull request
    the range holds."""
    _seed_first_tags(repo)
    _commit(repo, "noticeboard/src/one.py")
    _git(repo, "commit", "-q", "--amend", "-m", "Add the noticeboard (#12)")
    _commit(repo, "noticeboard/src/two.py")
    _git(repo, "commit", "-q", "--amend", "-m", "Fix the noticeboard (#14)")

    done = _run(repo, shims, tmp_path, pr="yes")

    assert done.returncode == 0, done.stderr
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "#12" in log
    assert "#14" in log


@pytest.mark.slow
def test_a_first_tags_body_says_it_has_no_range(repo: Path, shims: Path, tmp_path: Path) -> None:
    """A component with no tag has no window to measure, and the body must
    not imply one."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, pr="yes")

    assert done.returncode == 0, done.stderr
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "no previous tag" in log
    assert "commit(s) since" not in log


# ---- the repository the run tags -------------------------------------------
#
# `allocate.plan_tags` scopes every decision to one `Repo`, and the script
# passes `--repo`, so the CLI default `agent-control` does not stand wherever
# it runs. These are the two halves of that: agent-control never reaches
# `mcp-servers`, and a run in agent-mcp reaches nothing else.


@pytest.mark.slow
def test_a_run_in_agent_control_never_tags_mcp_servers(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """The catalog's rule, seen from the script. `mcp-servers` lives in
    another repository, which this run cannot write and must not name."""
    _commit(repo, "src/agent_mcp/one.py")

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "mcp-servers" not in done.stdout
    assert _tags(repo) == FIRST_TAGS


@pytest.mark.slow
def test_a_run_in_agent_mcp_tags_mcp_servers_and_nothing_else(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """The mirror case: without a tag, nothing could name `mcp-servers` in a
    release request."""
    _commit(repo, "src/agent_mcp/one.py")

    done = _run(repo, shims, tmp_path, repository="example-owner/agent-mcp")

    assert done.returncode == 0, done.stderr
    assert "released mcp-servers-v0.1.0" in done.stdout
    assert _tags(repo) == MCP_FIRST_TAGS


@pytest.mark.slow
def test_agent_mcp_bumps_its_one_component_on_the_next_run(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """`mcp-servers`'s path is `.`, so every merge moves it once it has a
    first tag. A re-run on one commit still creates nothing."""
    _commit(repo, "src/agent_mcp/one.py")
    assert _run(repo, shims, tmp_path, repository="example-owner/agent-mcp").returncode == 0

    _commit(repo, "src/agent_mcp/two.py")
    done = _run(repo, shims, tmp_path, repository="example-owner/agent-mcp")

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*MCP_FIRST_TAGS, "mcp-servers-v0.1.1"])

    again = _run(repo, shims, tmp_path, repository="example-owner/agent-mcp")

    assert again.returncode == 0, again.stderr
    assert "allocate-tags: 0 created" in again.stdout
    assert _tags(repo) == sorted([*MCP_FIRST_TAGS, "mcp-servers-v0.1.1"])


@pytest.mark.slow
def test_a_repository_the_catalog_does_not_know_stops_the_run(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """Falling back to agent-control's components in somebody else's
    checkout is how a tag lands in the wrong repository. Refuse instead."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, repository="example-owner/some-other-repo")

    assert done.returncode != 0
    assert "refused repository 'some-other-repo'" in done.stderr
    assert _tags(repo) == []


@pytest.mark.slow
def test_tag_repo_names_the_catalogs_repository_when_its_host_does_not(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """A repository may be hosted under a name the catalog does not hold.
    Its workflow then says which catalog repository it is, and GitHub is
    still asked by the hosted name."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(
        repo,
        shims,
        tmp_path,
        repository="example-owner/some-product",
        tag_repo="agent-control",
        pr="yes",
    )

    assert done.returncode == 0, done.stderr
    assert "repo:    agent-control" in done.stdout
    assert "chaperone-v0.1.0" in _tags(repo)
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "--repo example-owner/some-product" in log
    assert "example-owner/agent-control" not in log


@pytest.mark.slow
def test_a_tag_repo_the_catalog_does_not_know_stops_the_run(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, tag_repo="some-other-repo")

    assert done.returncode != 0
    assert "refused repository 'some-other-repo'" in done.stderr
    assert _tags(repo) == []


@pytest.mark.slow
def test_the_origin_remote_names_the_repository_when_ci_does_not(
    repo: Path, shims: Path, other_repo: Path, tmp_path: Path
) -> None:
    """A developer's checkout sets no `GITHUB_REPOSITORY`. The tagged tree's
    own origin answers, so a dry run from an agent-mcp checkout plans
    agent-mcp and never agent-control."""
    _commit(other_repo, "src/agent_mcp/one.py")

    done = _run(repo, shims, tmp_path, "--dry-run", repository=None, tree=other_repo)

    assert done.returncode == 0, done.stderr
    assert "repo:    agent-mcp" in done.stdout
    assert "would create mcp-servers-v0.1.0" in done.stdout
    assert "chaperone" not in done.stdout


# ---- the tree the run tags -------------------------------------------------


@pytest.mark.slow
def test_the_script_tags_the_tree_it_is_pointed_at(
    repo: Path, shims: Path, other_repo: Path, tmp_path: Path
) -> None:
    """agent-mcp's workflow, proved here because it never runs from this
    branch: the script reads and tags the borrowing checkout, and leaves its
    own alone."""
    _commit(other_repo, "src/agent_mcp/one.py")
    _commit(repo, "chaperone/src/one.py")

    done = _run(
        repo, shims, tmp_path, repository="example-owner/agent-mcp", tree=other_repo, pr="yes"
    )

    assert done.returncode == 0, done.stderr
    assert _tags(other_repo) == MCP_FIRST_TAGS
    assert _tags(repo) == []


@pytest.mark.slow
def test_a_borrowed_run_pushes_to_the_borrowers_origin(
    repo: Path, shims: Path, other_repo: Path, tmp_path: Path
) -> None:
    """The tag must reach the borrower's remote, not the script's own."""
    _commit(other_repo, "src/agent_mcp/one.py")

    done = _run(repo, shims, tmp_path, repository="example-owner/agent-mcp", tree=other_repo)

    assert done.returncode == 0, done.stderr
    assert _tags(tmp_path / "agent-mcp.git") == MCP_FIRST_TAGS
    assert _tags(tmp_path / "origin.git") == []


@pytest.mark.slow
def test_the_release_names_its_repository(
    repo: Path, shims: Path, other_repo: Path, tmp_path: Path
) -> None:
    """`gh release create` otherwise reads the repository from the current
    directory, which for a borrowed run is the wrong one."""
    _commit(other_repo, "src/agent_mcp/one.py")

    done = _run(
        repo, shims, tmp_path, repository="example-owner/agent-mcp", tree=other_repo, pr="yes"
    )

    assert done.returncode == 0, done.stderr
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "--repo example-owner/agent-mcp" in log


@pytest.mark.slow
def test_a_tag_repo_root_that_is_not_a_directory_stops_the_run(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """A mistyped path must not quietly tag the script's own checkout."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, tree=tmp_path / "nowhere")

    assert done.returncode != 0
    assert "TAG_REPO_ROOT is not a directory" in done.stderr
    assert _tags(repo) == []


# ---- a `gh` read that fails is no pull request -----------------------------


@pytest.mark.slow
def test_a_failed_pull_request_read_is_no_pull_request(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """`gh api` prints the error BODY on stdout and exits non-zero, and the
    script only swallowed the exit code. A token that cannot read pull
    requests therefore turned a 401 body into the labels and into the
    Release notes. Two tokens are now in play (agent-mcp's workflow adds a
    PAT), so a read that failed must read as no pull request at all."""
    _commit(repo, "chaperone/src/one.py")

    done = _run(repo, shims, tmp_path, labels="bump:minor", pr="yes", gh_reads=False)

    assert done.returncode == 0, done.stderr
    assert "labels:  none" in done.stdout
    assert "Bad credentials" not in done.stdout
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "Bad credentials" not in log
    assert "touch chaperone/src/one.py" in log


# ---- a directory deeper than the top level, and the Cargo workspace --------

#: A crate directory that a binary component's build installs.
NESTED_BUNDLE = "rust/crates/creche-contracts"

#: The planner, with `noticeboard` as a binary component that bundles one
#: crate directory. The catalog holds no binary component and no nested
#: directory, so the test gives the script a planner that holds both. The
#: script asks the planner for every decision, so this is the whole change.
BINARY_PLANNER = f"""
import sys
from dataclasses import replace

from handover import allocate
from handover.catalog import CATALOG, Kind
from handover.cli import main

allocate.CATALOG = tuple(
    replace(row, kind=Kind.BINARY, bundles=("{NESTED_BUNDLE}",))
    if row.name == "noticeboard"
    else row
    for row in CATALOG
)
sys.exit(main())
"""


@pytest.fixture
def binary_cli(tmp_path: Path) -> str:
    """`RELEASE_CLI` for a run whose catalog holds one binary component."""
    planner = tmp_path / "binary_planner.py"
    planner.write_text(BINARY_PLANNER, encoding="utf-8")

    return f"{sys.executable} {planner}"


@pytest.mark.slow
def test_a_change_under_a_nested_bundle_tags_its_component(
    repo: Path, shims: Path, tmp_path: Path, binary_cli: str
) -> None:
    """The script cut every changed path to its first segment, so a change
    under `rust/crates/<crate>/` reached the planner as `rust` and tagged
    nothing. The planner makes the cut now, and it keeps a nested bundle."""
    _seed_first_tags(repo)
    _commit(repo, f"{NESTED_BUNDLE}/src/lib.rs")

    done = _run(repo, shims, tmp_path, cli=binary_cli)

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "noticeboard-v0.1.1"])


@pytest.mark.slow
def test_a_change_beside_a_nested_bundle_tags_nothing(
    repo: Path, shims: Path, tmp_path: Path, binary_cli: str
) -> None:
    """A crate that no build installs is no component's path."""
    _seed_first_tags(repo)
    _commit(repo, f"{NESTED_BUNDLE}/src/lib.rs")
    assert _run(repo, shims, tmp_path, cli=binary_cli).returncode == 0
    _commit(repo, "rust/crates/other/src/lib.rs", "rust/README.md")

    done = _run(repo, shims, tmp_path, cli=binary_cli)

    assert done.returncode == 0, done.stderr
    assert "allocate-tags: 0 created" in done.stdout
    assert _tags(repo) == sorted([*FIRST_TAGS, "noticeboard-v0.1.1"])


@pytest.mark.slow
def test_the_cargo_lock_file_tags_the_binary_component_alone(
    repo: Path, shims: Path, tmp_path: Path, binary_cli: str
) -> None:
    """`rust/Cargo.lock` is a file two segments deep. Cut to `rust`, it
    moved nothing. It moves the binary component, and no venv component."""
    _commit(repo, f"{NESTED_BUNDLE}/src/lib.rs", "chaperone/src/one.py")
    _seed_first_tags(repo)
    _commit(repo, "rust/Cargo.lock")

    done = _run(repo, shims, tmp_path, cli=binary_cli)

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "noticeboard-v0.1.1"])


@pytest.mark.slow
def test_a_label_counts_through_a_nested_bundle(
    repo: Path, shims: Path, tmp_path: Path, binary_cli: str
) -> None:
    """The level lines carry the same cut, so a labelled merge that changed
    only a nested bundle raises its component."""
    _seed_first_tags(repo)
    _commit(repo, f"{NESTED_BUNDLE}/src/lib.rs")

    done = _run(repo, shims, tmp_path, labels="bump:minor", cli=binary_cli)

    assert done.returncode == 0, done.stderr
    assert _tags(repo) == sorted([*FIRST_TAGS, "noticeboard-v0.2.0"])


@pytest.mark.slow
def test_the_cargo_workspace_tags_no_component_of_todays_catalog(
    repo: Path, shims: Path, tmp_path: Path
) -> None:
    """The real planner, whose catalog holds no binary component: a merge
    that changes the Cargo workspace and a crate moves no tag at all."""
    _commit(repo, "chaperone/src/one.py", "noticeboard/src/one.py")
    _seed_first_tags(repo)
    _commit(repo, "rust/Cargo.lock", "rust/Cargo.toml", "rust/rust-toolchain.toml")
    _commit(repo, f"{NESTED_BUNDLE}/src/lib.rs")

    done = _run(repo, shims, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "allocate-tags: 0 created" in done.stdout
    assert _tags(repo) == FIRST_TAGS
