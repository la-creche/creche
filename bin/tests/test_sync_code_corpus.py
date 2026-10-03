"""`bin/sync-code-corpus.sh`: one clone per line of the list, under the name
the line gives.

A repository's directory in the corpus is the name the catalog knows it by
(`/srv/agents/code/<repo>`, `handover/src/handover/executor/source.py`).
Where it is hosted may call it something else, so a line may name the
directory: `<url> <name>`. Without a name the directory is the URL's own
last part, as it always was.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "sync-code-corpus.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )

    return done.stdout.strip()


@pytest.fixture
def hosted(tmp_path: Path) -> Path:
    """A repository as its host names it: `some-product.git`, one commit."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "file").write_text("one\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "one")
    bare = tmp_path / "some-product.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))

    return bare


def _sync(tmp_path: Path, lines: list[str]) -> subprocess.CompletedProcess[str]:
    listing = tmp_path / "code-repos.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = {
        **os.environ,
        **GIT_ENV,
        "CODE_CORPUS_ROOT": str(tmp_path / "corpus"),
        "CODE_REPO_LIST": str(listing),
    }

    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_a_line_with_no_name_clones_under_the_urls_own_name(tmp_path: Path, hosted: Path) -> None:
    done = _sync(tmp_path, ["# the product", f"file://{hosted}"])

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "corpus" / "some-product" / "file").is_file()


def test_a_line_may_name_the_directory(tmp_path: Path, hosted: Path) -> None:
    done = _sync(tmp_path, [f"file://{hosted} agent-control  # hosted under another name"])

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "corpus" / "agent-control" / "file").is_file()
    assert not (tmp_path / "corpus" / "some-product").exists()


def test_a_second_run_pulls_the_named_directory(tmp_path: Path, hosted: Path) -> None:
    lines = [f"file://{hosted} agent-control"]
    assert _sync(tmp_path, lines).returncode == 0
    work = tmp_path / "work"
    (work / "file").write_text("two\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "two")
    _git(work, "push", "-q", str(hosted), "main")

    done = _sync(tmp_path, lines)

    assert done.returncode == 0, done.stderr
    assert "updated agent-control" in done.stdout
    assert (tmp_path / "corpus" / "agent-control" / "file").read_text(encoding="utf-8") == "two\n"


@pytest.mark.parametrize("name", ["..", ".", "a/b", "../escape", "-flag", ".git"])
def test_a_name_that_is_no_plain_directory_name_is_refused(
    tmp_path: Path, hosted: Path, name: str
) -> None:
    """The name is joined onto the corpus root. It must stay inside it."""
    done = _sync(tmp_path, [f"file://{hosted} {name}"])

    assert done.returncode != 0
    assert "name" in done.stderr
    assert not (tmp_path / "escape").exists()


def test_a_line_with_a_third_word_is_refused(tmp_path: Path, hosted: Path) -> None:
    done = _sync(tmp_path, [f"file://{hosted} agent-control extra"])

    assert done.returncode != 0
    assert not (tmp_path / "corpus" / "agent-control").exists()


def test_a_last_line_with_no_newline_is_still_read(tmp_path: Path, hosted: Path) -> None:
    """`read` answers non-zero on a final line that has no newline, and a
    loop that stops there skips the last repository without a word."""
    listing = tmp_path / "code-repos.txt"
    listing.write_text(f"file://{hosted} agent-control", encoding="utf-8")
    env = {
        **os.environ,
        **GIT_ENV,
        "CODE_CORPUS_ROOT": str(tmp_path / "corpus"),
        "CODE_REPO_LIST": str(listing),
    }

    done = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "corpus" / "agent-control" / "file").is_file()
