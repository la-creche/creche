"""The Forgejo gate is the GitHub gate, but for a stated list of differences.

`.forgejo/workflows/gate.yml` is the port of `.github/workflows/gate.yml`
for Forgejo Actions. `bin/tests/test_gate_workflow.py` pins the GitHub
file. This file pins the Forgejo file to it, so the two gates cannot drift
apart without one red line: each job of the GitHub gate is in the Forgejo
gate with the same name, the same steps and the same test commands.

The tests port each GitHub job by the differences below, and compare the
result with the Forgejo job. A difference that is not in this list fails.

1. **Triggers.** `pull_request` only. Forgejo has no merge queue and sends
   no `merge_group` event (docs/design/forgejo-release-path.md, FR13). So
   the `frozen host api` step of `rust` and the scope action read no base
   of a merge group.
2. **Action URLs.** Forgejo resolves an action of another repository from
   its default host, so each one is a full URL, pinned by commit. A local
   action lives under `.forgejo/actions`.
3. **Runner labels.** The Docker runner gives a job no Docker socket, no
   systemd as process 1 and no /dev/kvm. A job that needs Docker or
   systemd runs on `vm-plain`. So does `memory`: its stop tests need a
   process 1 that reaps orphans. The jobs that need /dev/kvm run on
   `vm-kvm`: `kvm`, and `rust`, two of whose tests open /dev/kvm.
4. **Artifact actions.** Upstream upload-artifact and download-artifact v4
   and later refuse a server that is not GitHub. The gate uses neither, and
   a test holds that. A port of one must name the Forgejo action and add
   its own line to this list.
5. **Shard numbers.** Forgejo sets no `strategy.job-index` and no
   `strategy.job-total`. Its shard command reads K from `matrix.shard` and
   N from the step's `TOTAL`. A test holds the matrix to 1..N and `TOTAL`
   to N, so a shard cannot fall out.
6. **rustup.** The image of the Docker runner has no rustup and no cargo.
   Each job that runs cargo first runs `.forgejo/actions/rustup`, at a
   stated place. That action does nothing on a runner with rustup.
7. **File keys.** No `permissions` key. Forgejo ignores it, and a job token
   can write its own repository, so the key would claim a limit that does
   not hold. One `env` key: the load limits of difference 9.
8. **User.** The Docker runner runs a step as root with the umask 0000. The
   jobs of `RUNNER_USER_STEPS` run `.forgejo/actions/runner-user`, at a
   stated place, and name its `as-runner` as the shell of their run steps.
   Those steps then run as a user that is not root, with the umask 0022,
   the open file limit 65536 and a RUNNER_TEMP that is not /tmp, as on the
   hosted GitHub runner. The cache step of `proc` runs as root, where `~`
   is /root. So its path names the HOME of that user, where the suite
   builds the chaperone.
9. **Load.** The workflow `env` holds cargo, the Rust test threads, xdist
   and vitest to 6 workers each: 24 CPUs of the Docker runner for its 4
   jobs at once.
10. **Time limits.** A job with no `timeout-minutes` in the GitHub file gets
    the limit of `TIME_LIMITS`. A job with one keeps it.

The protection of `main` on Forgejo requires one status:
`gate / gate (pull_request)`. The workflow name, the name of its last job
and its one event make that context.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
GITHUB_DIR = REPO / ".github"
FORGEJO_DIR = REPO / ".forgejo"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


GITHUB_GATE = _load(GITHUB_DIR / "workflows" / "gate.yml")
FORGEJO_GATE_FILE = FORGEJO_DIR / "workflows" / "gate.yml"
FORGEJO_GATE = _load(FORGEJO_GATE_FILE)
GITHUB_JOBS: dict[str, dict[str, Any]] = GITHUB_GATE["jobs"]
FORGEJO_JOBS: dict[str, dict[str, Any]] = FORGEJO_GATE["jobs"]

#: The workflow name and the name of its last job.
GATE_NAME = "gate"

#: What starts the Forgejo gate (difference 1).
FORGEJO_EVENTS = ["pull_request"]

#: The status that the protection of `main` requires on Forgejo. Its form is
#: `<workflow name> / <job name> (<event>)`.
REQUIRED_CONTEXT = "gate / gate (pull_request)"

#: Each key of the Forgejo file. PyYAML reads the key `on` as the boolean.
#: No `permissions` (difference 7).
FORGEJO_FILE_KEYS = {"name", True, "concurrency", "env", "jobs"}

#: The workflow env: 6 workers for each tool (difference 9).
LOAD_ENV = {
    "CARGO_BUILD_JOBS": "6",
    "RUST_TEST_THREADS": "6",
    "PYTEST_XDIST_AUTO_NUM_WORKERS": "6",
    "VITEST_MAX_FORKS": "6",
    "VITEST_MAX_THREADS": "6",
}

#: The local actions that the gate uses, by their name.
LOCAL_ACTIONS = ["scope", "uv-sync", "verdict", "rustup", "runner-user"]
GITHUB_LOCAL = "./.github/actions/"
FORGEJO_LOCAL = "./.forgejo/actions/"

#: The host of each action of another repository (difference 2).
ACTION_HOST = "https://github.com/"

#: The label of the Docker runner, and the labels of the runner VMs
#: (difference 3). A job with no row in VM_LABELS runs on the Docker runner.
DOCKER_LABEL = "ubuntu-24.04"
VM_PLAIN = "vm-plain"
VM_KVM = "vm-kvm"
VM_LABELS = {
    "memory": VM_PLAIN,
    "playpen-image": VM_PLAIN,
    "systemd-proof": VM_PLAIN,
    "rust": VM_KVM,
    "kvm": VM_KVM,
}

#: The GitHub actions that refuse a server that is not GitHub (difference 4).
ARTIFACT_ACTIONS = ("actions/upload-artifact@", "actions/download-artifact@")

#: The step of `rust` that reads the base of the change, and the variable
#: that held the base of a merge group (difference 1).
FROZEN_JOB = "rust"
FROZEN_STEP = "frozen host api"
GROUP_BASE = "GROUP_BASE"

#: The text of that step that reads the base of a merge group, and its port.
FROZEN_BASE = (
    'base="$GROUP_BASE"\nif [[ -z "$base" && -n "$BASE_REF" ]]; then\n',
    'base=""\nif [[ -n "$BASE_REF" ]]; then\n',
)

#: The text of the scope action that reads the base of a merge group, and
#: its port.
SCOPE_BASE = (
    'base="$GROUP_BASE"\nif [[ -z "$base" ]]; then\n'
    '  base="$(git merge-base "origin/$GITHUB_BASE_REF" HEAD)" || base=""\nfi\n',
    'base="$(git merge-base "origin/$GITHUB_BASE_REF" HEAD)" || base=""\n',
)

#: The step that installs rustup (difference 6), and where each job that
#: runs cargo has it: the index in the steps of the job, and its `if`.
RUSTUP = "./.forgejo/actions/rustup"
ONLY_VMM = "needs.scope.outputs.vmm != 'false'"
RUSTUP_STEPS: dict[str, tuple[int, dict[str, str]]] = {
    "tests": (2, {"uses": RUSTUP}),
    "proc": (2, {"uses": RUSTUP}),
    "rust": (1, {"uses": RUSTUP}),
    "vmm-archive": (2, {"uses": RUSTUP, "if": ONLY_VMM}),
}

#: The step that changes the user (difference 8), where each job has it,
#: and the shell of the run steps of that job. The index counts the rustup
#: step of difference 6.
RUNNER_USER = "./.forgejo/actions/runner-user"
RUNNER_USER_STEPS: dict[str, tuple[int, dict[str, str]]] = {
    "tests": (3, {"uses": RUNNER_USER}),
    "proc": (4, {"uses": RUNNER_USER}),
    "suites": (2, {"uses": RUNNER_USER}),
    "vmm-archive": (3, {"uses": RUNNER_USER, "if": ONLY_VMM}),
}
AS_RUNNER = {"run": {"shell": "as-runner {0}"}}

#: The job of the chaperone cache, and its path in each gate (difference 8).
CACHE_JOB = "proc"
CACHE_DIR = ".cache/creche-proc/chaperone-rs"
CACHE_PATH = (f"~/{CACHE_DIR}", f"/home/ubuntu/{CACHE_DIR}")

#: The time limit of each job that has none in the GitHub file
#: (difference 10).
TIME_LIMITS = {
    "scope": 5,
    "lint": 10,
    "tests": 20,
    "agent": 10,
    "facade": 10,
    "memory": 15,
    "gate": 5,
}

#: The job of the shards, and the shard command of each gate with its env
#: (difference 5).
SHARD_JOB = "tests"
SHARD_RUN = (
    'uv run pytest -n auto --dist worksteal --shard "$((INDEX + 1))/$TOTAL"',
    'uv run pytest -n auto --dist worksteal --shard "$SHARD/$TOTAL"',
)
SHARD_ENV = (
    {"INDEX": "${{ strategy.job-index }}", "TOTAL": "${{ strategy.job-total }}"},
    {"SHARD": "${{ matrix.shard }}", "TOTAL": "<N>"},
)

#: An action of another repository in a Forgejo file: a full URL, a commit,
#: and the tag as a comment.
FULL_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")
USES = re.compile(r"^\s*-?\s*uses:\s*(.+?)\s*$", re.MULTILINE)


def _port_uses(uses: str) -> str:
    """The Forgejo spelling of one `uses` of the GitHub gate."""
    assert not uses.startswith(ARTIFACT_ACTIONS), f"{uses}: state its Forgejo action"
    if uses.startswith(GITHUB_LOCAL):
        return FORGEJO_LOCAL + uses.removeprefix(GITHUB_LOCAL)

    return ACTION_HOST + uses


def _ported(value: Any) -> Any:
    """`value` with each `uses` in its Forgejo spelling."""
    if isinstance(value, dict):
        return {
            key: _port_uses(one) if key == "uses" else _ported(one) for key, one in value.items()
        }
    if isinstance(value, list):
        return [_ported(one) for one in value]

    return value


def _replaced(text: str, pair: tuple[str, str]) -> str:
    old, new = pair
    assert text.count(old) == 1, f"the GitHub text changed: {old!r}"

    return text.replace(old, new)


def _port_job(name: str, job: dict[str, Any]) -> dict[str, Any]:
    """The GitHub job `name` after each stated difference."""
    port = _ported(copy.deepcopy(job))
    if name in VM_LABELS:
        port["runs-on"] = VM_LABELS[name]
    if name == FROZEN_JOB:
        (step,) = [one for one in port["steps"] if one.get("name") == FROZEN_STEP]
        del step["env"][GROUP_BASE]
        step["run"] = _replaced(step["run"], FROZEN_BASE)
    if name in RUSTUP_STEPS:
        index, step = RUSTUP_STEPS[name]
        port["steps"].insert(index, step)
    if name in RUNNER_USER_STEPS:
        index, step = RUNNER_USER_STEPS[name]
        port["steps"].insert(index, step)
        port["defaults"] = AS_RUNNER
    if name == CACHE_JOB:
        (step,) = [one for one in port["steps"] if "cache@" in one.get("uses", "")]
        assert step["with"]["path"] == CACHE_PATH[0]
        step["with"]["path"] = CACHE_PATH[1]
    if name in TIME_LIMITS:
        assert "timeout-minutes" not in port, f"{name}: the GitHub job has a limit"
        port["timeout-minutes"] = TIME_LIMITS[name]
    if name == SHARD_JOB:
        (step,) = [one for one in port["steps"] if "run" in one]
        assert (step["run"], step["env"]) == (SHARD_RUN[0], SHARD_ENV[0])
        total = str(len(port["strategy"]["matrix"]["shard"]))
        step["run"] = SHARD_RUN[1]
        step["env"] = {**SHARD_ENV[1], "TOTAL": total}

    return port


def _action(root: Path, name: str) -> dict[str, Any]:
    return _load(root / "actions" / name / "action.yml")


def _forgejo_files() -> list[Path]:
    actions = [FORGEJO_DIR / "actions" / one / "action.yml" for one in LOCAL_ACTIONS]

    return [FORGEJO_GATE_FILE, *actions]


def test_the_forgejo_gate_has_the_name_and_the_jobs_of_the_github_gate() -> None:
    assert FORGEJO_GATE["name"] == GITHUB_GATE["name"] == GATE_NAME
    assert list(FORGEJO_JOBS) == list(GITHUB_JOBS)


@pytest.mark.parametrize("name", list(GITHUB_JOBS))
def test_each_job_equals_its_github_job_but_for_the_stated_differences(name: str) -> None:
    """The same `needs`, `if`, matrix, env, time limit and steps, and so the
    same test commands. Only the differences of the module docstring may
    change a job."""
    assert FORGEJO_JOBS[name] == _port_job(name, GITHUB_JOBS[name])


def test_the_proc_cache_is_below_the_home_of_the_run_steps() -> None:
    """The suite builds below the HOME that `as-runner` sets. A cache of
    another path saves nothing, and each run builds the chaperone again."""
    (step,) = _action(FORGEJO_DIR, "runner-user")["runs"]["steps"]
    home = f"/home/{step['env']['RUN_USER']}"

    assert "HOME=/home/$RUN_USER" in step["run"]
    assert CACHE_PATH[1] == f"{home}/{CACHE_DIR}"
    assert '"/home/$RUN_USER"' in step["run"]


def test_the_shards_are_one_to_n_of_n() -> None:
    """K comes from the matrix and N from TOTAL. A list that skips a number,
    or a TOTAL that is not its length, would leave a share of the suite in
    no shard."""
    shards = FORGEJO_JOBS[SHARD_JOB]["strategy"]["matrix"]["shard"]
    (step,) = [one for one in FORGEJO_JOBS[SHARD_JOB]["steps"] if "run" in one]

    assert shards == list(range(1, len(shards) + 1))
    assert step["env"]["TOTAL"] == str(len(shards))


def test_the_forgejo_gate_runs_on_a_pull_request_only() -> None:
    assert FORGEJO_GATE[True] == FORGEJO_EVENTS
    assert "merge_group" not in json.dumps(FORGEJO_GATE)


def test_the_forgejo_file_has_no_permissions_key() -> None:
    """A `permissions` key would claim a read-only token. Forgejo ignores it."""
    assert set(FORGEJO_GATE) == FORGEJO_FILE_KEYS
    assert FORGEJO_GATE["concurrency"] == GITHUB_GATE["concurrency"]


def test_the_load_env_holds_each_tool_to_six_workers() -> None:
    assert FORGEJO_GATE["env"] == LOAD_ENV


def test_each_job_has_a_time_limit() -> None:
    """A job with no limit runs for 6 hours on a hung test."""
    assert all("timeout-minutes" in job for job in FORGEJO_JOBS.values())


def test_the_required_context_is_the_last_job_on_a_pull_request() -> None:
    (event,) = FORGEJO_EVENTS

    assert f"{FORGEJO_GATE['name']} / {GATE_NAME} ({event})" == REQUIRED_CONTEXT
    assert sorted(FORGEJO_JOBS[GATE_NAME]["needs"]) == sorted(set(FORGEJO_JOBS) - {GATE_NAME})
    assert FORGEJO_JOBS[GATE_NAME]["if"] == "always()"


def test_only_the_jobs_of_the_vm_table_wait_for_a_runner_vm() -> None:
    """A job with a label that no runner has waits for ever. The table names
    each job that the Docker runner cannot serve."""
    labels = {name: job["runs-on"] for name, job in FORGEJO_JOBS.items()}

    assert {name: one for name, one in labels.items() if one != DOCKER_LABEL} == VM_LABELS
    assert set(VM_LABELS) <= set(GITHUB_JOBS)


@pytest.mark.parametrize("path", _forgejo_files(), ids=lambda one: str(one.relative_to(REPO)))
def test_each_action_is_a_full_url_pinned_by_commit_or_local(path: Path) -> None:
    for uses in USES.findall(path.read_text(encoding="utf-8")):
        if uses.startswith(FORGEJO_LOCAL):
            assert (REPO / uses / "action.yml").is_file(), uses
            continue

        assert FULL_URL.fullmatch(uses), uses


def test_the_gate_uses_no_artifact_action() -> None:
    """Difference 4 has no line yet: neither gate moves a file between jobs."""
    for path in [GITHUB_DIR / "workflows" / "gate.yml", *_forgejo_files()]:
        text = path.read_text(encoding="utf-8")
        assert not any(one in text for one in ARTIFACT_ACTIONS), path


@pytest.mark.parametrize("name", ["uv-sync", "verdict"])
def test_a_local_action_equals_its_github_action_but_for_the_urls(name: str) -> None:
    assert _action(FORGEJO_DIR, name) == _ported(_action(GITHUB_DIR, name))


def test_the_scope_action_reads_no_merge_group() -> None:
    port = _action(GITHUB_DIR, "scope")
    (step,) = port["runs"]["steps"]
    del step["env"]
    step["run"] = _replaced(step["run"], SCOPE_BASE)

    assert _action(FORGEJO_DIR, "scope") == port


def test_the_rustup_action_installs_nothing_on_a_runner_with_rustup(tmp_path: Path) -> None:
    """The hosted runner and the runner VMs have rustup. There the action must
    not download a second one: a `curl` that fails proves no download ran."""
    (step,) = _action(FORGEJO_DIR, "rustup")["runs"]["steps"]
    for tool in ("rustup", "curl"):
        stub = tmp_path / tool
        stub.write_text(f"#!/bin/sh\nexit {0 if tool == 'rustup' else 1}\n", encoding="utf-8")
        stub.chmod(0o755)

    done = subprocess.run(
        ["bash", "-c", step["run"]],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "RUSTUP_DIR": str(tmp_path / "dir")},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert done.returncode == 0, done.stderr
    assert "nothing to install" in done.stdout
    assert not (tmp_path / "dir").exists()


def test_the_rustup_action_pins_the_program_by_its_digest() -> None:
    (step,) = _action(FORGEJO_DIR, "rustup")["runs"]["steps"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", step["env"]["RUSTUP_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", step["env"]["RUSTUP_SHA256"])
    assert 'if [[ "$found" != "$RUSTUP_SHA256" ]]; then' in step["run"]
    assert step["run"].index("$RUSTUP_SHA256") < step["run"].index('"$init" -y')
