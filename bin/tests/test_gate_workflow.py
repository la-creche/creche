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

The `rust` job runs `bin/rust-gate.sh --tests` for a change that touches
`rust/`, `vectors/`, or a file of the Rust checks themselves
(`bin/lib/rustrule.sh`). On any other code change it skips every step but the
checkout and is still a success, so `gate` reads green. The toolchain is the
one `rust/rust-toolchain.toml` names. The job takes `cargo-deny` from a
release archive, and it checks the SHA-256 of that archive before the unpack.

The `rust-coverage` job runs `bin/rust-coverage.sh` for the same change, and
skips its steps as the `rust` job does. It adds the LLVM tools to the same
toolchain, and it takes `cargo-llvm-cov` from a release archive that it
checked in the same way. `gate` needs the job, so a file of
`rust/coverage-files.txt` with code that no test runs blocks a merge.

The `proc` job runs the process-level suite (`integration/proc`), which is
in no shard: `testpaths` does not hold it. The job builds the playpen first,
and a test that skips is a failure there. A run in which every test skips
because the bundle is missing would be green and would judge nothing.

The `suites` job runs the two old suites of `integration/`, each one in a
pytest run of its own. It has the setup of the `proc` job. Its last step reads
the JUnit report of each run, and it fails for a test that skipped. Only
`gate.yml` has the job: `ONLY_IN` names that one difference between the two
files.

The `systemd-proof` job runs `bin/systemd-proof.sh` for a change that touches
a file of the proof: `systemd/` or the script.
On any other code change it skips its one step after the checkout and is
still a success. The job runs on the runner itself, with no container: only
there is systemd process 1. The script answers which change needs the proof,
and the scope runs the proof when the script gives no answer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
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

#: A step of the `rust` job that a change with no path under rust/ skips.
#: Only the answer `false` skips it. An answer that is missing, or that a
#: rename lost, runs the checks.
ONLY_RUST = "needs.scope.outputs.rust != 'false'"

#: How the scope job hands the Rust answer to the `rust` job.
RUST_OUTPUT = "${{ steps.scope.outputs.rust }}"

#: The whole of the Rust checks: the script the hooks run too.
RUST_RUN = "bin/rust-gate.sh --tests"

#: The first line of the toolchain step. It names no toolchain, so rustup
#: installs the one `rust/rust-toolchain.toml` names.
TOOLCHAIN_RUN = "rustup toolchain install --no-self-update"

#: The directory rustup reads that file from.
RUST_DIR = "rust"

#: The step that gives the job `cargo-deny`, the program behind the `cargo
#: deny` step of `bin/rust-gate.sh`.
DENY_STEP = "cargo-deny"

#: The version, and the SHA-256 of its release archive for the runner:
#: `cargo-deny-<version>-x86_64-unknown-linux-musl.tar.gz`. To take another
#: version, compute the SHA-256 of the new archive and compare it with the
#: `.sha256` file of that release. Then change the two values here and in the
#: two workflow files.
DENY_ENV = {
    "DENY_VERSION": "0.20.2",
    "DENY_SHA256": "9f12ed4c49936e09b48bf862b595cde2fe64fcbd9d74dfacac6131ca824c8d5f",
}

#: The whole text of the step. The archive comes from a release of the
#: cargo-deny repository. `set -euo pipefail` stops the step at the first
#: command that fails. The compare is before the unpack, and the unpack is
#: before the line that puts the program on PATH. A pin of some lines only
#: lets a second unpack or a second value of `found` in between them.
DENY_RUN = """\
set -euo pipefail
name="cargo-deny-$DENY_VERSION-x86_64-unknown-linux-musl"
archive="$RUNNER_TEMP/$name.tar.gz"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \\
  --output "$archive" \\
  "https://github.com/EmbarkStudios/cargo-deny/releases/download/$DENY_VERSION/$name.tar.gz"
found="$(sha256sum "$archive" | cut -d ' ' -f 1)"
if [[ "$found" != "$DENY_SHA256" ]]; then
  echo "cargo-deny: the archive has the SHA-256 $found, not $DENY_SHA256" >&2
  exit 1
fi
mkdir -p "$RUNNER_TEMP/cargo-deny"
tar -xzf "$archive" -C "$RUNNER_TEMP/cargo-deny" --strip-components 1 "$name/cargo-deny"
echo "$RUNNER_TEMP/cargo-deny" >> "$GITHUB_PATH"
PATH="$RUNNER_TEMP/cargo-deny:$PATH" cargo deny --version
"""

#: Each key of the step. One more key can change what a failure of the step
#: does, for example `continue-on-error` or `shell`.
DENY_KEYS = {"name", "if", "working-directory", "env", "run"}

#: The job that holds the coverage rule of the Rust workspace
#: (`rust/AGENTS.md`, "The coverage rule").
COVERAGE_JOB = "rust-coverage"

#: The whole of that rule: one script, with no flag. `--report` would check
#: a report of an earlier run, and `--branch` needs a nightly toolchain.
COVERAGE_RUN = "bin/rust-coverage.sh"

#: The whole text of the toolchain step of that job. The first line installs
#: the toolchain that `rust/rust-toolchain.toml` names. The second line adds
#: the LLVM tools to it, which `cargo llvm-cov` calls. The toolchain file
#: does not change.
COVERAGE_TOOLCHAIN_RUN = """\
rustup toolchain install --no-self-update
rustup component add llvm-tools-preview
cargo --version
"""

#: The step that gives the job `cargo-llvm-cov`, the program behind the
#: `cargo llvm-cov` step of `bin/rust-coverage.sh`.
COVERAGE_STEP = "cargo-llvm-cov"

#: The version, and the SHA-256 of its release archive for the runner:
#: `cargo-llvm-cov-x86_64-unknown-linux-musl.tar.gz` of the release
#: `v<version>`. To take another version, compute the SHA-256 of the new
#: archive and compare it with the digest that the release page shows. Then
#: change the two values here and in the two workflow files.
COVERAGE_ENV = {
    "LLVM_COV_VERSION": "0.9.1",
    "LLVM_COV_SHA256": "3fca950394a3c49457657c158b1619cec8dfd2647ae5b48746734c0ab969a522",
}

#: The whole text of the step, as for `cargo-deny`: the compare is before the
#: unpack, and the unpack is before the line that puts the program on PATH.
#: The archive holds the program and no directory, so the unpack names the
#: one member that it takes.
COVERAGE_INSTALL_RUN = """\
set -euo pipefail
name="cargo-llvm-cov-x86_64-unknown-linux-musl"
archive="$RUNNER_TEMP/$name.tar.gz"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \\
  --output "$archive" \\
  "https://github.com/taiki-e/cargo-llvm-cov/releases/download/v$LLVM_COV_VERSION/$name.tar.gz"
found="$(sha256sum "$archive" | cut -d ' ' -f 1)"
if [[ "$found" != "$LLVM_COV_SHA256" ]]; then
  echo "cargo-llvm-cov: the archive has the SHA-256 $found, not $LLVM_COV_SHA256" >&2
  exit 1
fi
mkdir -p "$RUNNER_TEMP/cargo-llvm-cov"
tar -xzf "$archive" -C "$RUNNER_TEMP/cargo-llvm-cov" cargo-llvm-cov
echo "$RUNNER_TEMP/cargo-llvm-cov" >> "$GITHUB_PATH"
PATH="$RUNNER_TEMP/cargo-llvm-cov:$PATH" cargo llvm-cov --version
"""

#: Each key of the job, and each key of its three steps after the checkout.
#: One more key can make a red step green, for example `continue-on-error`.
COVERAGE_JOB_KEYS = {"needs", "if", "runs-on", "timeout-minutes", "env", "steps"}

#: The runner of the job: the system of the release archive above.
COVERAGE_RUNNER = "ubuntu-latest"

#: Each variable of the job. One more can change what the run compiles, for
#: example `RUSTFLAGS`, and then what the report holds.
COVERAGE_JOB_ENV = {"CARGO_INCREMENTAL": 0}
COVERAGE_STEP_KEYS = [
    {"name", "if", "working-directory", "run"},
    {"name", "if", "working-directory", "env", "run"},
    {"if", "run"},
]

#: The two files the cargo cache is good for.
CACHE_FILES = ("rust/rust-toolchain.toml", "rust/Cargo.lock")

#: The whole key of the cargo cache: the system of the runner, then one hash
#: of the two files.
CACHE_KEY = "rust-${{ runner.os }}-${{ hashFiles('rust/rust-toolchain.toml', 'rust/Cargo.lock') }}"

#: The copy of the crates.io index that cargo keeps on the runner. For a
#: version that its author removed, `cargo deny` reads only this copy.
INDEX_COPY = "~/.cargo/registry/index"

#: The job that proves the restart rule of the daemon units, and the whole
#: of the proof: one script.
SYSTEMD_JOB = "systemd-proof"
SYSTEMD_RUN = "bin/systemd-proof.sh"

#: The step of that job that a change with no file of the proof skips. As
#: for `rust`, only the answer `false` skips it.
ONLY_SYSTEMD = "needs.scope.outputs.systemd != 'false'"

#: How the scope job hands that answer to the job.
SYSTEMD_OUTPUT = "${{ steps.scope.outputs.systemd }}"

#: The machine of the job: the hosted runner, with its own systemd.
SYSTEMD_RUNNER = "ubuntu-latest"

#: Each key of the job. One more key can move the proof off the systemd of
#: the runner, for example `container`. One more key can also let a failed
#: proof pass, for example `continue-on-error`.
SYSTEMD_KEYS = {"needs", "if", "runs-on", "timeout-minutes", "steps"}

#: Each key of the step that runs the proof.
SYSTEMD_STEP_KEYS = {"run", "if"}

#: How each scope asks the script. The answer starts as `true`, and only a
#: call that succeeds makes it `false`. A script that is absent, or that
#: fails, thus runs the proof.
SYSTEMD_ASKED = {
    GATE_NAME: 'if bin/systemd-proof.sh --unchanged "$base" HEAD; then\n  systemd=false\nfi\n',
    RELEASE_NAME: (
        'if [[ "$passed" == 1 ]] && bin/systemd-proof.sh --unchanged "$BEFORE" "$GITHUB_SHA";'
        " then\n  systemd=false\nfi\n"
    ),
}

#: The whole test command of the `proc` job, as `integration/proc/AGENTS.md`
#: gives it.
PROC_RUN = "uv run pytest integration/proc -m slow"

#: The switch that makes a skip a failure, and the file that reads it.
PROC_ENV = {"CRECHE_PROC_NO_SKIP": "1"}
PROC_SWITCHES = REPO / "integration" / "proc" / "proc_services.py"

#: The build that the suite needs, and where it runs. No vitest and no
#: typecheck: the `playpen` job runs those.
PLAYPEN_BUILD = "pnpm install --frozen-lockfile && pnpm run build"
PLAYPEN_DIR = "playpen"

#: The build refuses to run without a LAN address (playpen/build.mjs). The
#: example site's TEST-NET-1 address is the one the suite writes too.
BUILD_ENV = {"AGENT_LAN_ADDRESS": "192.0.2.10"}

#: The job that runs the two old suites of `integration/`.
SUITES_JOB = "suites"

#: The jobs that one workflow has and the other one lacks. This is the one
#: named exception to "the release runs the jobs of the gate". The `suites`
#: job is new to CI, and one red run of it in `release.yml` stops the tags of
#: that merge. So the release gets the job after it passed 20 runs of the
#: merge queue in a row. The pull request that adds the job to `release.yml`
#: empties this table.
ONLY_IN: dict[str, set[str]] = {GATE_NAME: {SUITES_JOB}, RELEASE_NAME: set()}

#: The whole test command of each suite, as `integration/AGENTS.md` gives it,
#: by the name of its report. The job runs each command as it is, in a step of
#: its own.
SUITE_RUNS = {
    "tests": "uv run pytest integration/tests -m slow",
    "tests_manager": "uv run pytest integration/tests_manager -m slow",
}

#: Where the runs of the job write their JUnit reports, and the option that
#: names one report. The option travels in a variable, so the command stays
#: as it is.
REPORT_DIR = "${{ runner.temp }}/suites"
REPORT_OPTION = "--junitxml="
REPORT_VARIABLE = "PYTEST_ADDOPTS"

#: The last step of the job, and the variable that gives it the reports. These
#: suites read no switch that makes a skip a failure, so this step reads the
#: report of each run.
NO_SKIP_STEP = "no test skipped"
NO_SKIP_KEYS = {"name", "shell", "env", "run"}
NO_SKIP_SHELL = "python3 {0}"
REPORTS_VARIABLE = "REPORTS"

#: Each key of the job. One more key can change what a failure of the job
#: does, for example `continue-on-error`.
SUITES_KEYS = {"needs", "if", "runs-on", "timeout-minutes", "steps"}

#: The time limit of the job in minutes: the limit of the `proc` job.
SUITES_MINUTES = 20

#: One small test file for each kind of pytest run that the last step judges.
SMALL_RUNS = {
    "ran": "def test_one():\n    pass\n",
    "one_skipped": (
        "import pytest\n\n\n"
        "def test_one():\n    pass\n\n\n"
        "def test_two():\n    pytest.skip('the bundle is absent')\n"
    ),
    "each_skipped": (
        "import pytest\n\n"
        "pytestmark = pytest.mark.skipif(True, reason='the bundle is absent')\n\n\n"
        "def test_one():\n    pass\n\n\n"
        "def test_two():\n    pass\n"
    ),
    "no_test": "ONE = 1\n",
}

#: A report that no run wrote, and a report that is no XML text.
ABSENT_REPORT = "absent"
BROKEN_REPORT = "broken"

#: The steps that give a job node and pnpm, by the start of `uses`.
NODE_ACTIONS = ("pnpm/action-setup@", "actions/setup-node@")

#: The local action that gives a job uv and the venv.
UV_SYNC = "./.github/actions/uv-sync"

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

#: The catalog's name for this repository (`handover/src/handover/catalog.py`).
THIS_REPOSITORY = "agent-control"

#: The whole of the tag step: the allocator, which holds the logic and its
#: own tests (handover/tests/test_handover_tag_script.py).
TAG_RUN = "handover/bin/allocate-tags.sh"

#: One allocation at a time, and none stopped half way.
ALLOCATE_ONE_AT_A_TIME = {"group": "release-allocate", "cancel-in-progress": False}

#: `uses: owner/action@<40 hex> # v1.2.3`, or a local `./path`.
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
TAG_COMMENT = re.compile(r"^\s*# v\d+\.\d+\.\d+$")

#: A small suite, to split for real in a subprocess.
SMALL_SUITE = "library/tests"


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
def test_the_docs_scope_runs_no_shard_no_playpen_and_no_rust(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    by_scope = {name: job.get("if") for name, job in jobs.items() if name != last}
    only_code = {name for name, rule in by_scope.items() if rule == ONLY_CODE}

    code_jobs = {"tests", "playpen", "proc", "rust", COVERAGE_JOB, SYSTEMD_JOB}
    elsewhere = set().union(*(names for name, names in ONLY_IN.items() if name != last))

    assert only_code == code_jobs | ONLY_IN[last]
    assert only_code == set(DOCS) - elsewhere, "the verdict table of this file names other jobs"
    assert {name for name, rule in by_scope.items() if rule is None} == {"scope", "lint"}


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_lint_runs_the_docs_tests_on_a_docs_change_and_no_test_beside_the_shards(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    runs = {step["if"]: step["run"] for step in jobs["lint"]["steps"] if "run" in step}

    assert runs == LINT_RUNS[last]


def test_the_release_runs_the_gates_test_jobs() -> None:
    """A change to one file's shards, playpen steps, process suite, Rust
    steps, coverage steps or systemd proof that misses the other would let a
    merge pass a release its PR could not, or the reverse."""
    for name in ("tests", "playpen", "proc", "rust", COVERAGE_JOB, SYSTEMD_JOB):
        assert RELEASE_JOBS[name] == JOBS[name], f"release.yml's {name} job is not gate.yml's"


def test_the_two_workflows_differ_only_by_the_jobs_of_the_exception() -> None:
    """`ONLY_IN` is the whole difference. A job that enters one file alone
    fails here, and so does a job of the table that both files have."""
    gate = set(JOBS) - {GATE_NAME}
    release = set(RELEASE_JOBS) - {RELEASE_NAME}

    assert gate - release == ONLY_IN[GATE_NAME]
    assert release - gate == ONLY_IN[RELEASE_NAME]


def _node_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if step.get("uses", "").startswith(NODE_ACTIONS)]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_proc_job_builds_the_playpen_then_runs_the_process_suite(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The suite starts `node playpen/dist/playpen.js`, so the build comes
    first, and the venv comes before the suite. The suite is one command,
    with no path of one test and no `-k`."""
    proc = jobs["proc"]
    runs = [step for step in proc["steps"] if "run" in step]
    build, suite = runs
    order = [step.get("uses") or step["run"] for step in proc["steps"]]

    assert proc["needs"] == "scope"
    assert proc["if"] == ONLY_CODE
    assert build["run"] == PLAYPEN_BUILD
    assert build["working-directory"] == PLAYPEN_DIR
    assert build["env"] == BUILD_ENV
    assert suite["run"] == PROC_RUN
    assert order.index(PLAYPEN_BUILD) < order.index(UV_SYNC) < order.index(PROC_RUN)


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_proc_job_has_node_and_pnpm_as_the_playpen_job_has_them(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """One node version and one pnpm for the bundle, whichever job builds
    it. The suite must judge the bundle that the `playpen` job tests."""
    steps = _node_steps(jobs["proc"])

    assert [step["uses"].split("@")[0] for step in steps] == [
        name.rstrip("@") for name in NODE_ACTIONS
    ]
    assert steps == _node_steps(jobs["playpen"])


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_skip_in_the_proc_job_is_a_failure(jobs: dict[str, dict[str, Any]], last: str) -> None:
    """Every topology test skips itself when the bundle is missing. The
    switch must be one that the suite reads: the suite stops on a variable
    with its prefix that it does not know, and a name with another prefix
    would change nothing."""
    (suite,) = [step for step in jobs["proc"]["steps"] if step.get("run") == PROC_RUN]

    assert suite["env"] == PROC_ENV
    for name in PROC_ENV:
        assert f'"{name}"' in PROC_SWITCHES.read_text(encoding="utf-8")


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_proc_job_has_a_time_limit(jobs: dict[str, dict[str, Any]], last: str) -> None:
    """A teardown waits for each process group. A fault in the harness can
    cost every test that wait, and a job with no limit has six hours."""
    assert 0 < jobs["proc"]["timeout-minutes"] <= 30


def _report_of(name: str) -> str:
    """The path of the JUnit report of one suite, as the workflow spells it."""
    return f"{REPORT_DIR}/{name}.xml"


def _setup_of(job: dict[str, Any], run: str) -> list[dict[str, Any]]:
    """The steps of a job before the step whose command is `run`."""
    runs = [step.get("run") for step in job["steps"]]

    return job["steps"][: runs.index(run)]


def _suite_steps() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The steps of the `suites` job after its setup: one for each suite, and
    the last one."""
    suites = JOBS[SUITES_JOB]
    first = next(iter(SUITE_RUNS.values()))
    *runs, last = suites["steps"][len(_setup_of(suites, first)) :]

    return runs, last


def test_the_suites_job_has_the_setup_of_the_proc_job() -> None:
    """Each step before the first suite is a step of the `proc` job, in the
    same order: the checkout, node and pnpm, the build of the playpen, then
    the venv. The two jobs then judge the bundle of one build command."""
    suites = JOBS[SUITES_JOB]
    setup = _setup_of(suites, next(iter(SUITE_RUNS.values())))

    assert set(suites) == SUITES_KEYS
    assert suites["needs"] == "scope"
    assert suites["if"] == ONLY_CODE
    assert suites["runs-on"] == JOBS["proc"]["runs-on"]
    assert setup == _setup_of(JOBS["proc"], PROC_RUN)
    assert [step.get("uses") or step["run"] for step in setup][-2:] == [PLAYPEN_BUILD, UV_SYNC]


def test_the_suites_job_runs_each_suite_in_a_pytest_run_of_its_own() -> None:
    """After the setup the job has one step for each suite, then the last
    step. A suite step holds the whole command of `integration/AGENTS.md`,
    with no path of one test and no `-k`. Its one other key is the variable
    that names its report."""
    runs, last = _suite_steps()

    assert len(runs) == len(SUITE_RUNS)
    for step, (name, run) in zip(runs, SUITE_RUNS.items(), strict=True):
        assert step == {"run": run, "env": {REPORT_VARIABLE: REPORT_OPTION + _report_of(name)}}
    assert last["name"] == NO_SKIP_STEP


def test_the_last_step_of_the_suites_job_reads_the_report_of_each_suite() -> None:
    """The step gets one path for each suite, on a line of its own. A path
    that differs from the path of a run would judge a report that no run
    wrote. No `if` and no `continue-on-error` can hold the step back."""
    _, last = _suite_steps()

    assert set(last) == NO_SKIP_KEYS
    assert last["shell"] == NO_SKIP_SHELL
    assert last["env"] == {REPORTS_VARIABLE: "\n".join(_report_of(name) for name in SUITE_RUNS)}


def test_the_suites_job_has_the_time_limit_of_the_proc_job() -> None:
    """The stack of a test waits for each listener and for each child at its
    teardown. A fault there can cost each test that wait."""
    assert JOBS[SUITES_JOB]["timeout-minutes"] == SUITES_MINUTES
    assert JOBS["proc"]["timeout-minutes"] == SUITES_MINUTES


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A JUnit report for each kind of run in `SMALL_RUNS`, from the pytest of
    this workspace, plus the two reports that are not one. The last step must
    read the form that this pytest writes."""
    root = tmp_path_factory.mktemp("reports")
    env = {name: value for name, value in os.environ.items() if name != REPORT_VARIABLE}
    found = {ABSENT_REPORT: root / "absent.xml", BROKEN_REPORT: root / "broken.xml"}
    found[BROKEN_REPORT].write_text("<testsuites>", encoding="utf-8")

    for name, body in SMALL_RUNS.items():
        home = root / name
        home.mkdir()
        (home / "test_small.py").write_text(body, encoding="utf-8")
        found[name] = root / f"{name}.xml"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                f"--rootdir={home}",
                f"--confcutdir={home}",
                f"{REPORT_OPTION}{found[name]}",
                "test_small.py",
            ],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert found[name].is_file(), f"the run `{name}` wrote no report"

    return found


def _no_skip(paths: list[Path]) -> subprocess.CompletedProcess[str]:
    """Runs the last step of the `suites` job over `paths`."""
    _, last = _suite_steps()

    return subprocess.run(
        [sys.executable, "-c", last["run"]],
        env={REPORTS_VARIABLE: "\n".join(str(path) for path in paths)},
        capture_output=True,
        text=True,
        timeout=30,
    )


#: (the report of each suite, whether the last step is green)
NO_SKIP_VERDICTS: list[tuple[list[str], bool]] = [
    (["ran", "ran"], True),
    # One test that skips is red, in each of the two places.
    (["ran", "one_skipped"], False),
    (["one_skipped", "ran"], False),
    # A suite in which each test skips is the run that judged nothing.
    (["ran", "each_skipped"], False),
    (["ran", "no_test"], False),
    # A suite with no report did not run, or it wrote to another path.
    (["ran", ABSENT_REPORT], False),
    (["ran", BROKEN_REPORT], False),
    ([], False),
]


@pytest.mark.parametrize(("kinds", "green"), NO_SKIP_VERDICTS, ids=str)
def test_the_suites_job_is_green_only_when_no_test_skipped(
    reports: dict[str, Path], kinds: list[str], green: bool
) -> None:
    done = _no_skip([reports[kind] for kind in kinds])

    assert (done.returncode == 0) == green, done.stdout + done.stderr


def test_the_last_step_prints_the_counts_of_each_suite(reports: dict[str, Path]) -> None:
    """The log of a run shows how many tests ran and how many skipped, for
    each suite. A reader counts the green runs of the job from those lines."""
    done = _no_skip([reports["ran"], reports["one_skipped"], reports["each_skipped"]])

    assert "ran: 1 of 1 tests ran, 0 skipped" in done.stdout
    assert "one_skipped: 1 of 2 tests ran, 1 skipped" in done.stdout
    assert "each_skipped: 0 of 2 tests ran, 2 skipped" in done.stdout
    assert "::error::one_skipped: 1 of 2 tests skipped" in done.stdout
    assert "::error::each_skipped: the suite ran no test" in done.stdout


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_change_outside_rust_skips_every_rust_step_and_not_the_job(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """A job skipped as a whole on a code change is red in the verdict. So
    the job runs, and each step after the checkout carries the rule."""
    checkout, *steps = jobs["rust"]["steps"]

    assert jobs["scope"]["outputs"]["rust"] == RUST_OUTPUT
    assert jobs["rust"]["needs"] == "scope"
    assert checkout["uses"].startswith("actions/checkout@")
    assert "if" not in checkout
    assert steps, "the rust job has no step but the checkout"
    for step in steps:
        assert step.get("if") == ONLY_RUST, f"{step.get('name') or step.get('uses')} always runs"


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_rust_job_runs_the_gate_the_hooks_run(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """One copy of the cargo commands: `bin/rust-gate.sh`. A cargo line
    written here would drift from the one a commit and a push run."""
    runs = [step for step in jobs["rust"]["steps"] if "run" in step]
    toolchain, deny, checks = runs

    assert deny["name"] == DENY_STEP
    assert checks["run"] == RUST_RUN
    assert toolchain["working-directory"] == RUST_DIR
    assert toolchain["run"].splitlines()[0] == TOOLCHAIN_RUN
    for step in jobs["rust"]["steps"]:
        assert "toolchain" not in step.get("with", {}), "a step names its own toolchain"


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_rust_job_takes_cargo_deny_from_an_archive_that_it_checked(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """No action installs `cargo-deny`, so no commit pin holds the program.
    The SHA-256 of the archive is the pin. The step must compare it before
    the unpack, and it must put nothing on PATH after a sum that differs.
    The test holds the whole text of the step and each key of the step."""
    (deny,) = [step for step in jobs["rust"]["steps"] if step.get("name") == DENY_STEP]
    names = [step.get("name") or step.get("run") for step in jobs["rust"]["steps"]]

    assert set(deny) == DENY_KEYS
    assert deny["env"] == DENY_ENV
    assert deny["working-directory"] == RUST_DIR
    assert deny["run"] == DENY_RUN
    assert names.index(DENY_STEP) < names.index(RUST_RUN)


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_cargo_cache_is_keyed_by_the_toolchain_and_the_lock(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    (cache,) = [
        step for step in jobs["rust"]["steps"] if step.get("uses", "").startswith("actions/cache@")
    ]

    for name in CACHE_FILES:
        assert f"'{name}'" in cache["with"]["key"]
        assert (REPO / name).is_file(), f"the cache key names {name}"
    assert "restore-keys" not in cache["with"]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_cargo_cache_keeps_the_index_copy_until_a_keyed_file_changes(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The cache holds the copy of the crates.io index, and its key changes
    only with the two files. A run can thus read the copy that an earlier
    run saved, and `cargo deny` reads only that copy for a removed version.
    "Known gaps" of `rust/AGENTS.md` lists that limit and names both facts.
    Change that entry in the commit that changes the path or the key."""
    (cache,) = [
        step for step in jobs["rust"]["steps"] if step.get("uses", "").startswith("actions/cache@")
    ]

    assert INDEX_COPY in cache["with"]["path"].split()
    assert cache["with"]["key"] == CACHE_KEY


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_change_outside_rust_skips_every_coverage_step_and_not_the_job(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """As for the `rust` job: the job runs on each code change, and each step
    after the checkout carries the rule. The job has no key that hides a red
    step."""
    job = jobs[COVERAGE_JOB]
    checkout, *steps = job["steps"]

    assert set(job) == COVERAGE_JOB_KEYS
    assert job["needs"] == "scope"
    assert job["if"] == ONLY_CODE
    assert job["runs-on"] == COVERAGE_RUNNER
    assert job["env"] == COVERAGE_JOB_ENV
    assert checkout["uses"].startswith("actions/checkout@")
    assert "if" not in checkout
    assert [set(step) for step in steps] == COVERAGE_STEP_KEYS
    for step in steps:
        assert step["if"] == ONLY_RUST, f"{step.get('name') or step['run']} always runs"


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_coverage_job_runs_the_script_on_the_pinned_toolchain(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """One copy of the cargo line and of the rule: `bin/rust-coverage.sh`.
    The step gives the script no flag. rustup takes the toolchain from
    `rust/rust-toolchain.toml`, so the step runs inside `rust/` and names no
    toolchain."""
    _checkout, toolchain, _install, script = jobs[COVERAGE_JOB]["steps"]

    assert toolchain["working-directory"] == RUST_DIR
    assert toolchain["run"] == COVERAGE_TOOLCHAIN_RUN
    assert toolchain["run"].splitlines()[0] == TOOLCHAIN_RUN
    assert script["run"] == COVERAGE_RUN
    assert (REPO / COVERAGE_RUN).is_file()


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_coverage_job_takes_cargo_llvm_cov_from_an_archive_that_it_checked(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """No action installs `cargo-llvm-cov`, so no commit pin holds the
    program. The SHA-256 of the archive is the pin, as for `cargo-deny`. The
    test holds the whole text of the step and the two constants."""
    _checkout, _toolchain, install, _script = jobs[COVERAGE_JOB]["steps"]

    assert install["name"] == COVERAGE_STEP
    assert install["env"] == COVERAGE_ENV
    assert install["working-directory"] == RUST_DIR
    assert install["run"] == COVERAGE_INSTALL_RUN


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_coverage_job_has_a_time_limit(jobs: dict[str, dict[str, Any]], last: str) -> None:
    """A test that does not end under the measurement holds the job, and a
    job with no limit has six hours."""
    assert 0 < jobs[COVERAGE_JOB]["timeout-minutes"] <= 30


def test_the_release_skips_rust_only_over_a_commit_whose_run_passed() -> None:
    """As for `docs`: a push never skips the Rust checks over a tree whose
    Rust failed, or is still in, its own run."""
    (step,) = [one for one in RELEASE_JOBS["scope"]["steps"] if one.get("id") == "scope"]

    assert "rust=true\n" in step["run"]
    assert 'if [[ "$passed" == 1 ]] && ! rust_touched "$BEFORE" "$GITHUB_SHA"; then' in step["run"]
    assert 'echo "rust=$rust" >> "$GITHUB_OUTPUT"' in step["run"]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_change_outside_systemd_skips_the_proof_step_and_not_the_job(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """As for `rust`: a job skipped as a whole on a code change is red in
    the verdict. So the job runs, and each step after the checkout carries
    the rule."""
    checkout, *steps = jobs[SYSTEMD_JOB]["steps"]

    assert jobs["scope"]["outputs"]["systemd"] == SYSTEMD_OUTPUT
    assert jobs[SYSTEMD_JOB]["needs"] == "scope"
    assert jobs[SYSTEMD_JOB]["if"] == ONLY_CODE
    assert checkout["uses"].startswith("actions/checkout@")
    assert "if" not in checkout
    assert steps, "the systemd-proof job has no step but the checkout"
    for step in steps:
        assert step.get("if") == ONLY_SYSTEMD, f"{step.get('name') or step.get('run')} always runs"


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_systemd_job_runs_the_proof_on_the_runner_itself(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """One copy of the proof: `bin/systemd-proof.sh`. The job has no
    container, because a container has no systemd as process 1. The test
    holds each key of the job and each key of the step."""
    job = jobs[SYSTEMD_JOB]
    _checkout, proof = job["steps"]

    assert set(job) == SYSTEMD_KEYS
    assert job["runs-on"] == SYSTEMD_RUNNER
    assert 0 < job["timeout-minutes"] <= 15
    assert set(proof) == SYSTEMD_STEP_KEYS
    assert proof["run"] == SYSTEMD_RUN
    assert (REPO / SYSTEMD_RUN).is_file()


def _scope_run(name: str) -> str:
    """The script of the step that answers the scope of a workflow."""
    if name == GATE_NAME:
        (step,) = SCOPE_ACTION["runs"]["steps"]
    else:
        (step,) = [one for one in RELEASE_JOBS["scope"]["steps"] if one.get("id") == "scope"]

    return step["run"]


@pytest.mark.parametrize("name", BY_NAME)
def test_a_scope_skips_the_proof_only_when_the_script_says_unchanged(name: str) -> None:
    """The release also asks for a commit whose own run passed, as it does
    for `rust`: a push never skips the proof over a tree whose proof failed,
    or is still in, its run."""
    run = _scope_run(name)

    assert run.count("systemd=true\n") == 1
    assert run.count("systemd=false\n") == 1
    assert SYSTEMD_ASKED[name] in run
    assert run.index("systemd=true\n") < run.index(SYSTEMD_ASKED[name])
    assert 'echo "systemd=$systemd" >> "$GITHUB_OUTPUT"' in run


def test_the_scope_action_gives_the_systemd_answer_as_an_output() -> None:
    assert SCOPE_ACTION["outputs"]["systemd"]["value"] == SYSTEMD_OUTPUT


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
    (`handover/bin/allocate-tags.sh`, TAG_REPO)."""
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
    ids = [f"chaperone/tests/test_{number}.py::test_it[{number}]" for number in range(500)]

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


def _needs(
    scope: str | None,
    results: dict[str, str],
    jobs: dict[str, dict[str, Any]] = JOBS,
    last: str = GATE_NAME,
) -> str:
    """`toJSON(needs)` as the last job of a workflow sees it: every job a
    success but for the ones `results` names. The gate needs each job of the
    release and the jobs of `ONLY_IN`. A name in `results` that the workflow
    lacks is left out."""
    names = sorted(set(jobs) - {last})
    needs = {name: {"result": results.get(name, "success"), "outputs": {}} for name in names}
    if scope is not None:
        needs["scope"]["outputs"] = {"scope": scope}

    return json.dumps(needs)


#: What a docs PR skips. A code PR skips nothing.
DOCS = {
    "tests": "skipped",
    "playpen": "skipped",
    "proc": "skipped",
    SUITES_JOB: "skipped",
    "rust": "skipped",
    COVERAGE_JOB: "skipped",
    SYSTEMD_JOB: "skipped",
}

#: (what the jobs did, whether `gate` is green)
VERDICTS = [
    (_needs("code", {}), True),
    (_needs("docs", DOCS), True),
    # One red shard makes the matrix job a failure.
    (_needs("code", {"tests": "failure"}), False),
    (_needs("code", {"playpen": "cancelled"}), False),
    (_needs("code", {"rust": "failure"}), False),
    (_needs("code", {COVERAGE_JOB: "failure"}), False),
    (_needs("code", {"proc": "failure"}), False),
    (_needs("code", {SYSTEMD_JOB: "failure"}), False),
    (_needs("code", {SYSTEMD_JOB: "cancelled"}), False),
    (_needs("code", {"lint": "failure"}), False),
    (_needs("docs", DOCS | {"lint": "failure"}), False),
    # A suite that did not run on a code PR is red, not skipped.
    (_needs("code", {"tests": "skipped"}), False),
    (_needs("code", {"lint": "skipped"}), False),
    # The Rust checks run on every code PR. The job skips its own steps when
    # the PR touches nothing under rust/, and is a success.
    (_needs("code", {"rust": "skipped"}), False),
    # The coverage job does the same. A run of it that did not end is red too.
    (_needs("code", {COVERAGE_JOB: "skipped"}), False),
    (_needs("code", {COVERAGE_JOB: "cancelled"}), False),
    # The process suite is in no shard. A code PR on which it did not run
    # is red.
    (_needs("code", {"proc": "skipped"}), False),
    # The two old suites of integration/ are in no shard either.
    (_needs("code", {SUITES_JOB: "failure"}), False),
    (_needs("code", {SUITES_JOB: "skipped"}), False),
    # The systemd proof runs on every code PR, as the Rust checks do. The
    # job skips its own step when the PR touches no file of the proof.
    (_needs("code", {SYSTEMD_JOB: "skipped"}), False),
    # A job that ran on a docs PR is not what the scope asks for.
    (_needs("docs", DOCS | {"rust": "success"}), False),
    (_needs("docs", DOCS | {COVERAGE_JOB: "success"}), False),
    (_needs("docs", DOCS | {"proc": "success"}), False),
    (_needs("docs", DOCS | {SUITES_JOB: "success"}), False),
    (_needs("docs", DOCS | {SYSTEMD_JOB: "success"}), False),
    # The release has no `suites` job yet (`ONLY_IN`), and its verdict asks
    # for none.
    (_needs("code", {}, RELEASE_JOBS, RELEASE_NAME), True),
    (_needs("docs", DOCS, RELEASE_JOBS, RELEASE_NAME), True),
    (_needs("code", {"proc": "failure"}, RELEASE_JOBS, RELEASE_NAME), False),
    (_needs("docs", DOCS | {"proc": "success"}, RELEASE_JOBS, RELEASE_NAME), False),
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


#: What the scope action answers for a change, as (docs or code, rust,
#: systemd).
SCOPES = [
    (["chaperone/src/chaperone/app.py"], ("code", "false", "false")),
    (["rust/crates/one/src/lib.rs"], ("code", "true", "false")),
    (["chaperone/src/chaperone/app.py", "rust/Cargo.lock"], ("code", "true", "false")),
    (["docs/later.md"], ("docs", "false", "false")),
    # The `rust` job is left out of a docs PR, whatever this answer is.
    (["rust/AGENTS.md"], ("docs", "true", "false")),
    # A file of the Rust checks themselves. The tests of the gate use a fake
    # cargo, so only this run proves the change with the real one.
    (["bin/rust-gate.sh"], ("code", "true", "false")),
    (["bin/rust-coverage.sh"], ("code", "true", "false")),
    (["bin/lib/rustrule.sh"], ("code", "true", "false")),
    # The three CI files are files of the Rust checks. They are no files of
    # the systemd proof: the tests of this file hold the `systemd-proof` job.
    ([".github/workflows/gate.yml"], ("code", "true", "false")),
    ([".github/workflows/release.yml"], ("code", "true", "false")),
    ([".github/actions/scope/action.yml"], ("code", "true", "false")),
    # The Rust tests read vectors/data, so a vector that moves runs them.
    (["vectors/data/index.json"], ("code", "true", "false")),
    (["vectors/generate.py"], ("code", "true", "false")),
    (["chaperone/src/chaperone/app.py", "vectors/data/ids/family.json"], ("code", "true", "false")),
    (["vectors/README.md"], ("docs", "true", "false")),
    # The Python half of the gate starts no cargo step of its own in CI.
    (["bin/quality-gate.sh"], ("code", "false", "false")),
    ([".github/actions/verdict/action.yml"], ("code", "false", "false")),
    # A unit file, and the proof script itself. The tests of the script use
    # a fake systemd, so only this run proves the change with the real one.
    (["systemd/creche-attendance.service"], ("code", "false", "true")),
    (["chaperone/src/chaperone/app.py", "systemd/creche-follow.timer"], ("code", "false", "true")),
    (["bin/systemd-proof.sh"], ("code", "false", "true")),
    (["systemd/creche-new@.timer"], ("code", "false", "true")),
    # git writes this path inside double quotes.
    (['systemd/creche-"one".service'], ("code", "false", "true")),
    # Every path under systemd/ counts, a Markdown file too. The
    # `systemd-proof` job is left out of a docs PR, whatever this answer is.
    (["systemd/AGENTS.md"], ("docs", "false", "true")),
    # Only the directory at the root holds the unit files.
    (["docs/systemd/notes.md"], ("docs", "false", "false")),
    (["systemd-notes.txt"], ("code", "false", "false")),
]


def _git(repo: Path, *args: str) -> str:
    leaked = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    )
    env = {name: value for name, value in os.environ.items() if name not in leaked}
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        env=env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    return done.stdout.strip()


def _scope_of(repo: Path, base: str) -> tuple[str, str, str]:
    """Runs the scope action's own script in `repo`, as a merge group on
    `base` would. Returns its three outputs."""
    (step,) = SCOPE_ACTION["runs"]["steps"]
    output = repo.parent / "output"
    output.write_text("", encoding="utf-8")
    done = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=repo,
        env={
            "PATH": os.environ["PATH"],
            "GROUP_BASE": base,
            "GITHUB_BASE_REF": "main",
            "GITHUB_OUTPUT": str(output),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())

    return outputs["scope"], outputs["rust"], outputs["systemd"]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository with one commit, the two rules the scope action sources
    and the script that it asks."""
    repo = tmp_path / "repo"
    (repo / "bin" / "lib").mkdir(parents=True)
    for name in ("lib/docsrule.sh", "lib/rustrule.sh", "systemd-proof.sh"):
        shutil.copy2(REPO / "bin" / name, repo / "bin" / name)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the base")

    return repo


@pytest.mark.parametrize(("paths", "scope"), SCOPES, ids=lambda one: " ".join(one))
def test_the_scope_says_whether_a_change_touches_rust(
    checkout: Path, paths: list[str], scope: tuple[str, str, str]
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    for name in paths:
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        # One more line, not a new body: the scope sources the two rules and
        # runs the script.
        with (checkout / name).open("a", encoding="utf-8") as file:
            file.write("# changed\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "the change")

    assert _scope_of(checkout, base) == scope


def test_a_unit_file_that_moves_out_of_systemd_runs_the_proof(checkout: Path) -> None:
    """git can report a move as one change with the new path only. The scope
    must see the old path too: the proof then reads one unit file less."""
    (checkout / "systemd").mkdir()
    (checkout / "systemd" / "creche-one.service").write_text(
        "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
    )
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "a unit")
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "units").mkdir()
    _git(checkout, "mv", "systemd/creche-one.service", "units/creche-one.service")
    _git(checkout, "commit", "-q", "-m", "the move")

    assert _scope_of(checkout, base) == ("code", "false", "true")


def test_a_change_the_scope_cannot_read_is_code_and_rust(checkout: Path) -> None:
    """No merge group, and no `origin/main` to take a merge-base with: the
    full suite, the cargo checks and the systemd proof are the safe answer."""
    assert _scope_of(checkout, "") == ("code", "true", "true")


def test_a_base_that_the_clone_lacks_is_code_rust_and_systemd(checkout: Path) -> None:
    """A merge group names its base. A clone that lacks that commit reads no
    change, and each check is then the safe answer."""
    assert _scope_of(checkout, "0" * 40) == ("code", "true", "true")


@pytest.mark.parametrize("fault", ["absent", "not executable", "fails"])
def test_a_scope_that_gets_no_answer_from_the_script_runs_the_proof(
    checkout: Path, fault: str
) -> None:
    """The change touches no file of the proof, so the script of the commit
    says `unchanged`. A script that gives no answer must not read as that
    answer. The fault is in the work tree only: the change stays the same."""
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "later.py").write_text("", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "the change")

    assert _scope_of(checkout, base) == ("code", "false", "false")

    script = checkout / SYSTEMD_RUN
    if fault == "absent":
        script.unlink()
    elif fault == "not executable":
        script.chmod(0o644)
    else:
        script.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")

    assert _scope_of(checkout, base) == ("code", "false", "true")


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
