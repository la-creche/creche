"""`!retest` on a PR restarts the right run, and only for the right people.

`.github/workflows/retest.yml` runs on every PR comment, holds
`actions: write`, and exists to clear a red or cancelled run from a PR head
so a release can pass P5. Its step runs here against a `gh` binstub: what it
restarts is read from the stub's log, and nothing reaches GitHub.

`issue_comment` runs the file on the default branch, so no PR can try a
change to it before the merge. These cases are the only run it gets first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
RETEST = yaml.safe_load((REPO / ".github" / "workflows" / "retest.yml").read_text(encoding="utf-8"))
JOB: dict[str, Any] = RETEST["jobs"]["retest"]

#: PyYAML reads the key `on` as the boolean.
TRIGGER = RETEST[True]

#: The PR head the stub answers with, and its short form in a reply.
SHA = "5f8e4724c0ffee00000000000000000000000000"
SHORT = SHA[:8]

#: A `gh` that logs its arguments and answers from the environment: the head
#: for `pr view`, the runs for `api`, nothing for `run rerun` and `pr comment`.
GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "pr view") printf '%s\\n' "$STUB_SHA" ;;
  "api "*) printf '%s' "$STUB_RUNS" ;;
esac
"""


def _run(tmp_path: Path, comment: str, runs: str) -> tuple[list[str], str]:
    """The step's script against the stub: the re-runs it asked for, and its
    reply on the PR."""
    stub = tmp_path / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "gh.log"
    log.touch()

    (step,) = JOB["steps"]
    done = subprocess.run(
        ["bash", "-c", step["run"]],
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "GH_LOG": str(log),
            "GH_REPO": "example/agent-control",
            "PR": "163",
            "COMMENT": comment,
            "STUB_SHA": SHA,
            "STUB_RUNS": runs,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    calls = log.read_text(encoding="utf-8").splitlines()
    reruns = [call for call in calls if call.startswith("run rerun")]
    replies = [call for call in calls if call.startswith("pr comment")]

    return reruns, "\n".join(replies)


def test_a_failed_run_restarts_its_failed_jobs(tmp_path: Path) -> None:
    reruns, reply = _run(tmp_path, "!retest", "11 completed failure")

    assert reruns == ["run rerun 11 --failed"]
    assert f"restarted run 11 on {SHORT} (its failed jobs)" in reply


def test_a_cancelled_run_restarts_whole_and_a_green_one_is_left(tmp_path: Path) -> None:
    """The reopened PR: the run its reopening cancelled sits beside a green
    one, and P5 refuses the head until the cancelled one is green."""
    reruns, reply = _run(tmp_path, "!retest\n", "12 completed success\n11 completed cancelled")

    assert reruns == ["run rerun 11"]
    assert "(every job)" in reply


def test_when_every_run_passed_the_newest_runs_again(tmp_path: Path) -> None:
    reruns, _reply = _run(tmp_path, "!retest", "12 completed success\n11 completed success")

    assert reruns == ["run rerun 12"]


def test_one_run_restarts_per_comment(tmp_path: Path) -> None:
    """Two re-runs at once share the gate's concurrency group, and the second
    would cancel the first."""
    reruns, reply = _run(tmp_path, "!retest", "12 completed failure\n11 completed cancelled")

    assert reruns == ["run rerun 12 --failed"]
    assert "1 more red run(s)" in reply


def test_a_run_still_going_holds_every_restart(tmp_path: Path) -> None:
    reruns, reply = _run(tmp_path, "!retest", "12 in_progress none\n11 completed cancelled")

    assert reruns == []
    assert "still going" in reply


def test_a_head_with_no_run_restarts_nothing(tmp_path: Path) -> None:
    reruns, reply = _run(tmp_path, "!retest", "")

    assert reruns == []
    assert f"no run on {SHORT}" in reply


@pytest.mark.parametrize("comment", ["!retest later", "!retests", "please !retest", "!retest all"])
def test_the_command_is_the_whole_comment(tmp_path: Path, comment: str) -> None:
    reruns, reply = _run(tmp_path, comment, "11 completed failure")

    assert reruns == []
    assert reply == ""


def test_only_a_pr_comment_from_someone_with_write_access_starts_a_runner() -> None:
    rule = JOB["if"]

    assert TRIGGER == {"issue_comment": {"types": ["created"]}}
    assert "github.event.issue.pull_request" in rule
    assert "startsWith(github.event.comment.body, '!retest')" in rule
    assert '["OWNER", "MEMBER", "COLLABORATOR"]' in rule
    assert "github.event.comment.author_association" in rule


def test_it_runs_no_code_of_the_pr_and_reads_the_comment_from_the_environment() -> None:
    (step,) = JOB["steps"]

    # No checkout and no action: the job holds `actions: write`.
    assert "uses" not in step
    assert RETEST["permissions"] == {"actions": "write", "pull-requests": "write"}

    # A comment pasted into the script would run as shell.
    assert "${{" not in step["run"]
    assert step["env"]["COMMENT"] == "${{ github.event.comment.body }}"
