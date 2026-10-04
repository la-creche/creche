"""A binary component moves when the Cargo workspace files move.

Contract 06 §1 rule 10 gives every venv component the root `uv.lock`: `uv
sync --frozen` installs what it pins, so a change to it moves their tags.
A binary build reads three files of the Cargo workspace in the same way.

Two properties are held here. A binary component moves when one of those
files moves. No component of another kind moves, and that includes every
component of today's catalog.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from handover.allocate import TagPlan, plan_tags, touched
from handover.catalog import (
    ARRIVING,
    BINARY_BUILD_FILES,
    CATALOG,
    CATALOG_BY_NAME,
    RETIRING,
    Kind,
    Repo,
)
from handover.discovery import discover
from handover.executor.source import digest_paths, input_digest
from handover_bin_fixtures import BINARY_NAME, NESTED_BUNDLE, use_binary_catalog

#: Every agent-control component a run may tag, at its first version. A
#: repository in this state has no first tag left to make, so the path rule
#: alone decides.
ALL_TAGGED = tuple(
    f"{row.name}-v0.1.0"
    for row in CATALOG
    if row.repo is Repo.AGENT_CONTROL and row.name not in RETIRING | ARRIVING
)

PYTHON_LOCK = "uv.lock"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _plan(paths: tuple[str, ...], levels: tuple[str, ...] = ()) -> tuple[TagPlan, ...]:
    return plan_tags(paths, levels, ALL_TAGGED, (), Repo.AGENT_CONTROL)


def _moved(paths: tuple[str, ...]) -> set[str]:
    return {plan.component for plan in _plan(paths)}


# -- the catalog as it is: no binary component ------------------------------


def test_the_workspace_files_are_the_three_under_rust() -> None:
    assert BINARY_BUILD_FILES == (
        "rust/Cargo.lock",
        "rust/Cargo.toml",
        "rust/rust-toolchain.toml",
    )


def test_the_workspace_files_move_no_component_of_todays_catalog() -> None:
    """No row is a binary component, so a change to the Cargo workspace
    files is a merge that tags nothing."""
    assert touched(BINARY_BUILD_FILES, Repo.AGENT_CONTROL) == ()
    assert _plan(BINARY_BUILD_FILES) == ()


def test_a_change_under_rust_moves_no_component_of_todays_catalog() -> None:
    changed = (f"{NESTED_BUNDLE}/src/lib.rs", "rust/crates/other/Cargo.toml", "rust/deny.toml")

    assert touched(changed, Repo.AGENT_CONTROL) == ()


def test_each_manifest_declares_the_kind_of_its_catalog_row() -> None:
    """The allocator and the digest read a component's kind from the
    catalog. The executor reads it from the manifest. A component that
    changes kind in one place only would build as one kind and move its tag
    as another, so this repository's manifests are held to their rows."""
    found = discover([REPO_ROOT]).manifests()

    assert found
    for name, manifest in found.items():
        assert manifest.kind is CATALOG_BY_NAME[name].kind, name


# -- one binary component among the venv components -------------------------


@pytest.mark.parametrize("changed", BINARY_BUILD_FILES)
def test_each_workspace_file_moves_the_binary_component_alone(
    monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """The lock file pins the crates, the workspace manifest holds the build
    profile, and the toolchain file names the compiler. Each one changes
    the programs that a build makes."""
    use_binary_catalog(monkeypatch)

    assert _moved((changed,)) == {BINARY_NAME}


def test_the_workspace_files_move_no_venv_component(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the venv components need: a Rust dependency upgrade
    must not release one of them."""
    use_binary_catalog(monkeypatch)
    venvs = {row.name for row in CATALOG if row.kind is Kind.VENV} - {BINARY_NAME}

    assert venvs
    assert touched(BINARY_BUILD_FILES, Repo.AGENT_CONTROL) == (BINARY_NAME,)
    assert not venvs & set(touched(BINARY_BUILD_FILES, Repo.AGENT_CONTROL))


def test_the_python_lock_file_moves_no_binary_component(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary build reads no `uv.lock`, so a Python dependency upgrade
    must not release a binary component. Every venv component still moves."""
    use_binary_catalog(monkeypatch)
    ours = [row for row in CATALOG if row.repo is Repo.AGENT_CONTROL and row.name not in RETIRING]
    venvs = {row.name for row in ours if row.kind is Kind.VENV} - {BINARY_NAME}

    assert _moved((PYTHON_LOCK,)) == venvs


@pytest.mark.parametrize(
    "changed",
    [
        "Cargo.lock",
        "Cargo.toml",
        "rust-toolchain.toml",
        "rust/crates/other/Cargo.toml",
        "docs/rust/Cargo.lock",
        "rust/Cargo.lock.orig",
    ],
)
def test_a_file_of_that_name_in_another_place_moves_nothing(
    monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """Only the three files at the workspace root are read by every build."""
    use_binary_catalog(monkeypatch)

    assert _moved((changed,)) == set()


def test_a_label_counts_through_a_workspace_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace file is one of the component's own paths, so a labelled
    pull request that changed only that file raises the level."""
    use_binary_catalog(monkeypatch)
    lines = (f"{BINARY_NAME}\trust/Cargo.lock",)
    levels = (f"{BINARY_NAME}\tbump:minor\trust/Cargo.lock",)

    (plan,) = _plan(lines, levels)

    assert plan.tag == f"{BINARY_NAME}-v0.2.0"


def test_a_label_on_a_workspace_file_raises_no_venv_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_binary_catalog(monkeypatch)
    lines = ("chaperone\tchaperone",)
    levels = ("chaperone\tbump:minor\trust/Cargo.lock",)

    (plan,) = _plan(lines, levels)

    assert plan.tag == "chaperone-v0.1.1"


# -- the input digest --------------------------------------------------------


def test_a_binary_digest_covers_the_workspace_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract 06 §9: the source subtree plus the lock file. For a binary
    component the lock files are the Cargo workspace's, and `uv.lock` is no
    input to what its build makes."""
    row = use_binary_catalog(monkeypatch)

    assert digest_paths(BINARY_NAME) == tuple(sorted((row.path, *BINARY_BUILD_FILES)))
    assert PYTHON_LOCK not in digest_paths(BINARY_NAME)


def test_every_other_kind_keeps_its_digest_paths() -> None:
    """The digest of every component of today's catalog is over the same
    paths as before: its subtree and `uv.lock`, or the whole repository."""
    for row in CATALOG:
        expected = (".",) if row.path == "." else tuple(sorted((row.path, PYTHON_LOCK)))

        assert digest_paths(row.name) == expected, row.name


def _git(repo: Path, *args: str) -> str:
    """One git command that reads no user's configuration."""
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
    if done.returncode != 0:
        return None

    return done.stdout


def _clone(tmp_path: Path) -> Path:
    """A repository with a component directory, a Cargo workspace and a
    Python lock file."""
    repo = tmp_path / "agent-control"
    for name in (BINARY_NAME, "chaperone", "rust"):
        (repo / name).mkdir(parents=True)

    repo.chmod(0o755)
    (repo / BINARY_NAME / "component.yaml").write_text("one\n", encoding="utf-8")
    (repo / "chaperone" / "component.yaml").write_text("one\n", encoding="utf-8")
    for path in (*BINARY_BUILD_FILES, PYTHON_LOCK):
        (repo / path).write_text("one\n", encoding="utf-8")

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")

    return repo


def _digest_after(repo: Path, component: str, changed: str) -> str | None:
    (repo / changed).write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")

    return input_digest(_run_git, repo, component, _git(repo, "rev-parse", "HEAD").strip())


@pytest.mark.slow
@pytest.mark.parametrize("changed", BINARY_BUILD_FILES)
def test_a_workspace_file_changes_a_binary_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    use_binary_catalog(monkeypatch)
    repo = _clone(tmp_path)
    before = input_digest(_run_git, repo, BINARY_NAME, _git(repo, "rev-parse", "HEAD").strip())

    assert before is not None
    assert _digest_after(repo, BINARY_NAME, changed) != before


@pytest.mark.slow
def test_the_python_lock_file_does_not_change_a_binary_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_binary_catalog(monkeypatch)
    repo = _clone(tmp_path)
    before = input_digest(_run_git, repo, BINARY_NAME, _git(repo, "rev-parse", "HEAD").strip())

    assert before is not None
    assert _digest_after(repo, BINARY_NAME, PYTHON_LOCK) == before


@pytest.mark.slow
@pytest.mark.parametrize("changed", BINARY_BUILD_FILES)
def test_a_workspace_file_does_not_change_a_venv_digest(tmp_path: Path, changed: str) -> None:
    """`chaperone` is a venv component of today's catalog."""
    assert CATALOG_BY_NAME["chaperone"].kind is Kind.VENV
    repo = _clone(tmp_path)
    before = input_digest(_run_git, repo, "chaperone", _git(repo, "rev-parse", "HEAD").strip())

    assert before is not None
    assert _digest_after(repo, "chaperone", changed) == before
