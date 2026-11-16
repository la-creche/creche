"""`bin/docs-mirror.py` rewrites a mirrored doc only when that doc changes.

On 2026-11-15 each doc carried the HEAD sha of its repo. So each merge to
creche rewrote all 22 creche notes of the vault, LiveSync uploaded them
again, and the memory service extracted each one again with the LLM.

Each test runs the script on a real git repo in a temp corpus."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "docs-mirror.py"

#: A git with no user or system config, and a fixed author.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    """Write `files`, commit them, and return the short sha."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")

    return _git(repo, "log", "-1", "--format=%h")


def _mirror(corpus: Path, target: Path) -> str:
    """The last line of one run of the script."""
    env = {**os.environ, **GIT_ENV, "DOCS_MIRROR_CORPUS": str(corpus)}
    env["DOCS_MIRROR_TARGET"] = str(target)
    done = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=True
    )

    return done.stdout.strip().splitlines()[-1]


def _snapshot(target: Path) -> dict[str, str]:
    return {str(p.relative_to(target)): p.read_text() for p in sorted(target.rglob("*.md"))}


def test_a_commit_that_changes_no_doc_writes_nothing(tmp_path: Path) -> None:
    corpus, target = tmp_path / "corpus", tmp_path / "docs"
    repo = corpus / "creche"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    first = _commit(
        repo,
        {"README.md": "# creche\n", "docs/a.md": "# A\n", "docs/b.md": "# B\n", "bin/x.sh": "1\n"},
    )
    _mirror(corpus, target)
    before = _snapshot(target)

    _commit(repo, {"bin/x.sh": "2\n"})

    assert _mirror(corpus, target) == f"[docs-mirror] target {target}: 0 written, 0 removed"
    assert _snapshot(target) == before
    assert f"commit: {first}" in before["creche/docs/a.md"]


def test_a_doc_change_rewrites_that_doc_and_the_indexes_only(tmp_path: Path) -> None:
    corpus, target = tmp_path / "corpus", tmp_path / "docs"
    repo = corpus / "creche"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    first = _commit(repo, {"README.md": "# creche\n", "docs/a.md": "# A\n", "docs/b.md": "# B\n"})
    _mirror(corpus, target)
    before = _snapshot(target)

    second = _commit(repo, {"docs/b.md": "# B, edited\n"})

    assert _mirror(corpus, target) == f"[docs-mirror] target {target}: 3 written, 0 removed"
    after = _snapshot(target)
    changed = sorted(rel for rel in after if after[rel] != before[rel])
    assert changed == ["Home.md", "creche/_index.md", "creche/docs/b.md"]
    assert f"commit: {second}" in after["creche/docs/b.md"]
    assert f"commit: {first}" in after["creche/docs/a.md"]
    assert f"`{second}`" in after["Home.md"]
