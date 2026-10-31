"""The registry commit in the resolved manifest (contract 06 §9, §9.2).

A set that deploys `mcp-servers` carries the registry commit root reads
`mcp/` at, in the `registry-data` row, so the tap binds the server files.
Step 2 reads the commit at the clone's `HEAD`. Step 8 reads at the commit
the approved manifest names.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

from handover.executor.live_state import (
    REGISTRY_STAMP_FILE,
    Readers,
    build_state,
    installed_registry_rev,
    write_registry_stamp,
)
from handover.executor.source import head_commit, input_digest
from handover.executor.steps import Plan
from handover.resolve import Resolution
from handover_executor_fixtures import DIGEST, fake_readers

REGISTRY_SHA = "a" * 40
LATER_SHA = "b" * 40


def _git(repo: Path, *args: str) -> str:
    """One git command that reads no user's config."""
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )

    return done.stdout


def _run_git(argv: list[str], cwd: Path) -> str | None:
    done = subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True, check=False)

    return done.stdout if done.returncode == 0 else None


def _commit(repo: Path, path: str, text: str) -> str:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", path)

    return _git(repo, "rev-parse", "HEAD").strip()


def _registry(tmp_path: Path) -> tuple[Path, str]:
    """A repository shaped like the registry: `mcp/` and `families/`."""
    repo = tmp_path / "registry"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "families/chat/family.toml", "one\n")
    first = _commit(repo, "mcp/kagi/server.toml", "one\n")

    return repo, first


# -- the clone ------------------------------------------------------------


def test_head_commit_is_the_clones_head(tmp_path: Path) -> None:
    repo, first = _registry(tmp_path)

    assert head_commit(_run_git, repo) == first


def test_a_directory_that_is_no_clone_has_no_head(tmp_path: Path) -> None:
    assert head_commit(_run_git, tmp_path) is None


def test_the_registry_digest_covers_mcp_and_nothing_else(tmp_path: Path) -> None:
    """A family edit moves no server file, so it must not move the digest
    the tap binds. A server edit must."""
    repo, first = _registry(tmp_path)
    family = _commit(repo, "families/chat/family.toml", "two\n")
    server = _commit(repo, "mcp/kagi/server.toml", "two\n")

    before = input_digest(_run_git, repo, "registry-data", first)
    assert before is not None
    assert input_digest(_run_git, repo, "registry-data", family) == before
    assert input_digest(_run_git, repo, "registry-data", server) != before


# -- the builder ----------------------------------------------------------


def _readers(head: str | None, asked: list[str]) -> Readers:
    base = fake_readers(latest={"mcp-servers": "0.1.13", "noticeboard": "0.2.0"})

    def registry_head() -> str | None:
        asked.append("head")

        return head

    return Readers(
        newest_version=base.newest_version,
        tag_sha=base.tag_sha,
        input_digest=base.input_digest,
        registry_head=registry_head,
    )


def test_a_set_that_deploys_mcp_servers_carries_the_registry_commit(tmp_path: Path) -> None:
    asked: list[str] = []
    built = build_state((tmp_path,), {"mcp-servers": "latest"}, _readers(REGISTRY_SHA, asked))

    row = built.state.facts["registry-data"]
    assert (row.sha, row.input_digest, row.artifact_digest) == (REGISTRY_SHA, DIGEST, None)
    assert asked == ["head"]


def test_any_other_set_reads_no_registry(tmp_path: Path) -> None:
    asked: list[str] = []
    built = build_state((tmp_path,), {"noticeboard": "latest"}, _readers(REGISTRY_SHA, asked))

    assert "registry-data" not in built.state.facts
    assert asked == []


def test_step_8_reads_at_the_approved_commit_and_not_at_head(tmp_path: Path) -> None:
    """A registry merge after the tap is not drift (§9.2 rule 6)."""
    asked: list[str] = []
    readers = _readers(LATER_SHA, asked)
    built = build_state((tmp_path,), {"mcp-servers": "latest"}, readers, registry_sha=REGISTRY_SHA)

    assert built.state.facts["registry-data"].sha == REGISTRY_SHA
    assert asked == []


def test_no_registry_commit_is_a_note_and_no_row(tmp_path: Path) -> None:
    asked: list[str] = []
    built = build_state((tmp_path,), {"mcp-servers": "latest"}, _readers(None, asked))

    assert "registry-data" not in built.state.facts
    assert "no registry commit: the mcp/ files are not bound" in built.notes


# -- the plan -------------------------------------------------------------


def _plan(rows: list[object]) -> Plan:
    return Plan(
        resolution=cast(Resolution, None),
        manifests={},
        document={"components": rows},
        order=(),
    )


def test_the_plan_names_the_approved_registry_commit() -> None:
    rows: list[object] = [
        {"name": "mcp-servers", "sha": "c" * 40},
        {"name": "registry-data", "sha": REGISTRY_SHA},
    ]

    assert _plan(rows).registry_sha() == REGISTRY_SHA


def test_a_plan_with_a_null_registry_row_names_none() -> None:
    assert _plan([{"name": "registry-data", "sha": None}]).registry_sha() is None
    assert _plan([]).registry_sha() is None


# -- the installed stamp --------------------------------------------------


def test_the_registry_stamp_reads_back_as_its_commit(tmp_path: Path) -> None:
    write_registry_stamp(tmp_path, REGISTRY_SHA)

    assert installed_registry_rev(tmp_path) == REGISTRY_SHA


def test_no_stamp_reads_as_none(tmp_path: Path) -> None:
    assert installed_registry_rev(tmp_path) is None


def test_a_stamp_that_is_not_one_commit_reads_as_none(tmp_path: Path) -> None:
    """A bad stamp reads as "a different tree" (contract 06 §9.2 rule 3), so
    it can only send a redeploy to the tap, never refuse one."""
    (tmp_path / REGISTRY_STAMP_FILE).write_text("HEAD\n", encoding="utf-8")

    assert installed_registry_rev(tmp_path) is None


def test_a_stamp_that_is_a_symlink_reads_as_none(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text(f"{REGISTRY_SHA}\n", encoding="utf-8")
    (tmp_path / REGISTRY_STAMP_FILE).symlink_to(target)

    assert installed_registry_rev(tmp_path) is None
