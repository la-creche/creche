"""The PR gate's jobs together still run every test, and one check says so.

`.github/workflows/gate.yml` runs the suite as N shards on N runners. Three
things could shrink what a PR is tested against without one red line, and
each gets a check here instead of a memory:

1. **A shard that names paths.** Every shard runs the whole of
   `pyproject.toml`'s `testpaths` and keeps its share with `--shard K/N`
   (the root `conftest.py`). A run line that named suites would leave out
   the suite somebody adds to `testpaths` next.
2. **A K/N written by hand.** The run line reads the job's index and the
   job total from the matrix itself, so the shards are always 1..N of N.
3. **A `gate` check that is green over a job that did not run.** `gate`
   needs every other job, and reads each one's result against the scope.

A job is billed in whole minutes, so one short check rides in a job that
runs anyway, and stays pinned: on a docs PR the tests marked `docs` in
`lint`.

It also pins what the release executor and the sessions rely on: one
workflow named `gate`, every action pinned to a commit, every local action
in the tree, full history in every checkout.

`release.yml` runs the same jobs after the merge, and the same checks hold
it: the same shard command, a last job that needs every other one, and one
shared verdict (`.github/actions/verdict`), so a PR and its release cannot
judge the same results differently. Its tag step runs only after that
verdict, one allocation at a time, and only its last job may write.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
GATE = yaml.safe_load((WORKFLOWS / "gate.yml").read_text(encoding="utf-8"))
JOBS: dict[str, dict[str, Any]] = GATE["jobs"]
RELEASE = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
RELEASE_JOBS: dict[str, dict[str, Any]] = RELEASE["jobs"]

#: The one check sessions read in `gh pr checks`, and the workflow's name.
GATE_NAME = "gate"

#: The job that tags, and the workflow's name.
RELEASE_NAME = "release"

#: Each workflow's jobs with the name of its last one, the one that judges.
WORKFLOW_JOBS = [(JOBS, GATE_NAME), (RELEASE_JOBS, RELEASE_NAME)]
BY_NAME = [GATE_NAME, RELEASE_NAME]

#: What starts the gate: a pull request, and a merge queue's group.
GATE_EVENTS = ["pull_request", "merge_group"]

#: One run per pull request head or per merge group, e.g.
#: `gate-rework/some-branch` or `gate-refs/heads/gh-readonly-queue/main/pr-12-<sha>`.
GATE_GROUP = "gate-${{ github.head_ref || github.ref }}"

SCOPE_ACTION = yaml.safe_load(
    (REPO / ".github" / "actions" / "scope" / "action.yml").read_text(encoding="utf-8")
)

#: The one copy of the verdict, and how a job hands it its needs.
VERDICT = "./.github/actions/verdict"
VERDICT_WITH = {"needs": "${{ toJSON(needs) }}"}
VERDICT_ACTION = yaml.safe_load(
    (REPO / ".github" / "actions" / "verdict" / "action.yml").read_text(encoding="utf-8")
)

#: The whole test command of a shard. No path, no `-k`, no `-m`: only its
#: share of everything `testpaths` holds.
SHARD_RUN = 'uv run pytest -n auto --dist worksteal --shard "$((INDEX + 1))/$TOTAL"'

#: Where that command's K and N come from: the matrix, never a literal.
SHARD_ENV = {"INDEX": "${{ strategy.job-index }}", "TOTAL": "${{ strategy.job-total }}"}

#: A job a docs PR leaves out, as `jobs.<job>.if` spells it.
ONLY_CODE = "needs.scope.outputs.scope == 'code'"

#: What the lint job runs for each scope: lint alone beside the shards, and
#: lint with the tests marked `docs` where there is no shard. The gate's lint
#: job asks the scope itself, and the release's reads the scope job.
LINT_RUNS = {
    GATE_NAME: {
        "steps.scope.outputs.scope == 'code'": "bin/quality-gate.sh",
        "steps.scope.outputs.scope == 'docs'": "bin/quality-gate.sh --docs",
    },
    RELEASE_NAME: {
        "needs.scope.outputs.scope == 'code'": "bin/quality-gate.sh",
        "needs.scope.outputs.scope == 'docs'": "bin/quality-gate.sh --docs",
    },
}

#: The steps of the last job that decide, by name.
VERDICT_STEP = "every job passed"
TAG_STEP = "tag and release"

#: The catalog's name for this repository (`release/src/agent_release/catalog.py`).
THIS_REPOSITORY = "agent-control"

#: The whole of the tag step: the allocator, which holds the logic and its
#: own tests (release/tests/test_release_tag_script.py).
TAG_RUN = "release/bin/allocate-tags.sh"

#: One allocation at a time, and none stopped half way.
ALLOCATE_ONE_AT_A_TIME = {"group": "release-allocate", "cancel-in-progress": False}

#: `uses: owner/action@<40 hex> # v1.2.3`, or a local `./path`.
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
TAG_COMMENT = re.compile(r"^\s*# v\d+\.\d+\.\d+$")

#: A small suite, to split for real in a subprocess.
SMALL_SUITE = "indexer/tests"


def _root_conftest() -> Any:
    """The root conftest.py as a module: `--shard` lives there."""
    spec = importlib.util.spec_from_file_location("root_conftest", REPO / "conftest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def _collected(*args: str) -> list[str]:
    """The test ids `pytest --collect-only` prints for `args`."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    return [line for line in done.stdout.splitlines() if "::" in line]


def _yaml_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *(REPO / ".github" / "actions").glob("*/action.yml")])


def _step(jobs: dict[str, dict[str, Any]], last: str, name: str) -> dict[str, Any]:
    (step,) = [one for one in jobs[last]["steps"] if one.get("name") == name]

    return step


def _gate_step(name: str) -> dict[str, Any]:
    return _step(JOBS, GATE_NAME, name)


def test_one_workflow_and_one_check_named_gate() -> None:
    assert GATE["name"] == GATE_NAME
    assert GATE_NAME in JOBS


def test_the_gate_runs_on_a_pull_request_and_on_a_merge_group() -> None:
    """A merge queue merges on the `gate` of the group it built. With no
    `merge_group` trigger that check never reports and the queue waits for
    ever. PyYAML reads the key `on` as the boolean."""
    assert GATE[True] == GATE_EVENTS


def test_two_merge_groups_never_share_a_concurrency_group() -> None:
    """`head_ref` is empty on a merge group. A group named by it alone is
    `gate-` for every entry of the queue, and each one cancels the last."""
    assert GATE["concurrency"] == {"group": GATE_GROUP, "cancel-in-progress": True}


def test_the_scope_of_a_merge_group_is_read_from_the_groups_base() -> None:
    (step,) = SCOPE_ACTION["runs"]["steps"]

    assert step["env"] == {"GROUP_BASE": "${{ github.event.merge_group.base_sha }}"}
    assert 'base="$GROUP_BASE"' in step["run"]
    assert 'docs_only "$base" HEAD' in step["run"]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_last_job_needs_every_other_job_and_always_runs(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    judge = jobs[last]

    assert sorted(judge["needs"]) == sorted(set(jobs) - {last})
    assert judge["if"] == "always()"


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_shard_names_no_path_and_no_number(jobs: dict[str, dict[str, Any]], last: str) -> None:
    tests = jobs["tests"]
    runs = [step for step in tests["steps"] if "run" in step]

    assert tests["if"] == ONLY_CODE
    assert [step["run"] for step in runs] == [SHARD_RUN]
    assert runs[0]["env"] == SHARD_ENV


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_docs_scope_runs_no_shard_and_no_playpen(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    by_scope = {name: job.get("if") for name, job in jobs.items() if name != last}

    assert {name for name, rule in by_scope.items() if rule == ONLY_CODE} == {"tests", "playpen"}
    assert {name for name, rule in by_scope.items() if rule is None} == {"scope", "lint"}


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_lint_runs_the_docs_tests_on_a_docs_change_and_no_test_beside_the_shards(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    runs = {step["if"]: step["run"] for step in jobs["lint"]["steps"] if "run" in step}

    assert runs == LINT_RUNS[last]


def test_the_release_runs_the_gates_test_jobs() -> None:
    """A change to one file's shards or playpen steps that misses the
    other would let a merge pass a release its PR could not, or the reverse."""
    for name in ("tests", "playpen"):
        assert RELEASE_JOBS[name] == JOBS[name], f"release.yml's {name} job is not gate.yml's"


def test_the_tag_step_runs_only_after_the_verdict() -> None:
    steps = RELEASE_JOBS[RELEASE_NAME]["steps"]
    names = [step.get("name") for step in steps]
    verdict = _step(RELEASE_JOBS, RELEASE_NAME, VERDICT_STEP)
    tag = _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)

    assert verdict["uses"] == VERDICT
    assert verdict["with"] == VERDICT_WITH
    assert names.index(VERDICT_STEP) < names.index(TAG_STEP)

    # No `if` on any step: a failed verdict skips every step after it, and
    # the job is `always()`, so a rule on a step is the one way to tag a red
    # tree.
    for step in steps:
        assert "if" not in step, f"{step.get('name') or step.get('uses')} sets its own rule"

    assert tag["run"] == TAG_RUN


def test_the_tag_step_says_which_catalog_repository_it_tags() -> None:
    """The allocator would otherwise take the name from where the
    repository is hosted, and refuse a name the catalog does not hold
    (`release/bin/allocate-tags.sh`, TAG_REPO)."""
    tag = _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)

    assert tag["env"]["TAG_REPO"] == THIS_REPOSITORY


def test_two_merges_never_allocate_at_once() -> None:
    """Two runs that read the same newest tag would allocate one number
    twice, and a run stopped half way would leave a tag with no Release."""
    assert RELEASE_JOBS[RELEASE_NAME]["concurrency"] == ALLOCATE_ONE_AT_A_TIME

    # A group on the whole workflow would queue the suite too, and a queued
    # run that gives way would never test its commit.
    assert "concurrency" not in RELEASE


def test_only_the_release_job_may_write() -> None:
    """Every other job runs the merged code, so none of them holds the
    token that tags."""
    assert RELEASE["permissions"] == {"contents": "read", "actions": "read"}
    assert RELEASE_JOBS[RELEASE_NAME]["permissions"] == {
        "contents": "write",
        "pull-requests": "read",
    }
    for name, job in RELEASE_JOBS.items():
        if name != RELEASE_NAME:
            assert "permissions" not in job, f"{name} sets its own permissions"


def test_gate_judges_with_the_shared_verdict_and_nothing_else() -> None:
    """A pull request carries no number, so no step of `gate` reads one. The
    job is `always()` and no `if` holds its one check back."""
    gate = JOBS[GATE_NAME]
    runs = [step for step in gate["steps"] if "run" in step]

    assert runs == []
    assert "if" not in _gate_step(VERDICT_STEP)
    assert _gate_step(VERDICT_STEP)["uses"] == VERDICT
    assert _gate_step(VERDICT_STEP)["with"] == VERDICT_WITH


def test_no_file_carries_a_version_to_bump() -> None:
    """Contract 06 §2.1: CI allocates each component's number at the merge.
    A root version that moved again would bring back the collision of two
    pull requests that chose the same one."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    assert root["project"]["version"] == "0.0.0"


@pytest.mark.parametrize("total", [1, 2, 6, 13])
def test_every_test_is_in_exactly_one_shard(total: int) -> None:
    in_shard = _root_conftest().in_shard
    ids = [f"pep/tests/test_{number}.py::test_it[{number}]" for number in range(500)]

    for one in ids:
        holders = [shard for shard in range(1, total + 1) if in_shard(one, shard, total)]
        assert len(holders) == 1, f"{one} is in shards {holders} of {total}"


def test_the_shards_of_a_real_suite_add_up_to_the_suite() -> None:
    total = 3
    whole = _collected(SMALL_SUITE)
    shards = [_collected(SMALL_SUITE, "--shard", f"{k}/{total}") for k in range(1, total + 1)]

    assert whole, f"{SMALL_SUITE} collects nothing"
    assert sorted(one for shard in shards for one in shard) == sorted(whole)


@pytest.mark.parametrize("text", ["0/4", "5/4", "4", "a/b", "1/0", "/"])
def test_a_shard_outside_one_to_n_is_refused(text: str) -> None:
    with pytest.raises(pytest.UsageError):
        _root_conftest().parse_shard(text)


def _needs(scope: str | None, results: dict[str, str]) -> str:
    """`toJSON(needs)` as the last job sees it: every job a success but for
    the ones `results` names. Both workflows need the same four jobs."""
    names = sorted(set(JOBS) - {GATE_NAME})
    assert names == sorted(set(RELEASE_JOBS) - {RELEASE_NAME})
    needs = {name: {"result": results.get(name, "success"), "outputs": {}} for name in names}
    if scope is not None:
        needs["scope"]["outputs"] = {"scope": scope}

    return json.dumps(needs)


#: What a docs PR skips. A code PR skips nothing.
DOCS = {"tests": "skipped", "playpen": "skipped"}

#: (what the jobs did, whether `gate` is green)
VERDICTS = [
    (_needs("code", {}), True),
    (_needs("docs", DOCS), True),
    # One red shard makes the matrix job a failure.
    (_needs("code", {"tests": "failure"}), False),
    (_needs("code", {"playpen": "cancelled"}), False),
    (_needs("code", {"lint": "failure"}), False),
    (_needs("docs", DOCS | {"lint": "failure"}), False),
    # A suite that did not run on a code PR is red, not skipped.
    (_needs("code", {"tests": "skipped"}), False),
    (_needs("code", {"lint": "skipped"}), False),
    # No scope: the scope job failed and everything behind it was skipped.
    (_needs(None, DOCS | {"scope": "failure"}), False),
]


@pytest.mark.parametrize(("needs", "green"), VERDICTS)
def test_the_verdict_is_green_only_when_every_job_its_scope_asks_for_passed(
    needs: str, green: bool
) -> None:
    (step,) = VERDICT_ACTION["runs"]["steps"]
    assert step["env"] == {"NEEDS": "${{ inputs.needs }}"}

    done = subprocess.run(
        [sys.executable, "-c", step["run"]],
        env={"NEEDS": needs},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert (done.returncode == 0) == green, done.stdout + done.stderr


def test_every_checkout_takes_full_history() -> None:
    """The allocator needs every tag, the scope a merge-base, and some
    tests read git state. A shallow clone has to be proved per job first."""
    for name, job in [*JOBS.items(), *RELEASE_JOBS.items()]:
        for step in job["steps"]:
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue

            assert step["with"]["fetch-depth"] == 0, f"{name} checks out a shallow clone"


@pytest.mark.parametrize("path", _yaml_files(), ids=lambda one: one.parent.name + "/" + one.name)
def test_every_action_is_pinned_or_in_the_tree(path: Path) -> None:
    for found in USES.finditer(path.read_text(encoding="utf-8")):
        name, rest = found.group(1), found.group(2)

        # A local action is read from the checkout: it must be there.
        if name.startswith("./"):
            assert (REPO / name / "action.yml").is_file(), f"{path.name} uses {name}"
            continue

        assert PINNED.match(name), f"{path.name} uses {name}, not a commit"
        assert TAG_COMMENT.match(rest), f"{path.name} uses {name} with no `# vX.Y.Z` beside it"
