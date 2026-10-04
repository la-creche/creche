"""When the quality gate runs cargo, and what it runs.

`bin/quality-gate.sh` runs the Rust checks only for a change that touches
`rust/` (`bin/lib/rustrule.sh`). Four things could go wrong without one red
line, and each gets a check here:

1. **A Python change that needs cargo.** Some sessions commit from a sandbox
   with no Rust toolchain. A commit or a push that touches nothing under
   `rust/` must pass with no `cargo` on PATH and must start no cargo step.
2. **A Rust change that passes on the Python checks alone.** With no `cargo`
   on PATH the gate fails closed, with one line, before any check runs.
3. **A Rust path that starts the full Python suite.** `rust/` is in no Python
   package. In `--tests-for` mode a path there runs `cargo test`, and pytest
   runs only for the other paths.
4. **A crate outside the lint gate.** A crate with no `[lints] workspace =
   true` builds with none of `[workspace.lints]`, and cargo does not say so.
   `bin/rust-gate.sh` refuses it.

Everything runs the real gate in a throwaway repository. `uv` and `cargo`
are fakes that write their argv to a file. PATH holds only those fakes and
links to the few tools the gate calls, so a `cargo` anywhere on the machine
cannot reach a run that must not have one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

#: The files under test, copied into the throwaway repository.
GATE = "bin/quality-gate.sh"
RUST_GATE = "bin/rust-gate.sh"
RULE = "bin/lib/rustrule.sh"
COPIED = (GATE, RUST_GATE, RULE)

#: Every program the two scripts and the rule call, but `uv` and `cargo`.
TOOLS = ("bash", "git", "awk", "grep", "tr", "dirname", "find", "sort")

#: What the gate runs for a Rust change, word for word, in this order.
FMT = "fmt --all --check"
CLIPPY = "clippy --workspace --all-targets --locked -- -D warnings"
TEST = "test --workspace --locked"
LINT_STEPS = [FMT, CLIPPY]
TEST_STEPS = [FMT, CLIPPY, TEST]

#: The Python checks of every mode, as the fake `uv` sees them.
PYTHON_LINT = ["run ruff check .", "run ruff format --check .", "run pyright"]
PYTEST = "run pytest -n auto"

#: The one suite of the throwaway repository, and a path in its package.
SUITE = "chaperone/tests"
PYTHON_PATH = "chaperone/src/chaperone/app.py"

#: A crate of the throwaway workspace, and a file in it.
CRATE = "rust/crates/one"
RUST_PATH = f"{CRATE}/src/lib.rs"

#: A crate file that takes the lint gate.
INHERITS = '[package]\nname = "one"\n\n[lints]\nworkspace = true\n'

FAKE_UV = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$UV_LOG"
"""

#: Writes where it ran and its argv. Fails on the subcommand CARGO_FAIL names.
FAKE_CARGO = """#!/usr/bin/env bash
printf '%s\\t%s\\n' "$PWD" "$*" >> "$CARGO_LOG"
if [[ "${1:-}" == "${CARGO_FAIL:-}" ]]; then
  exit 1
fi
"""

FIXTURE = {
    "pyproject.toml": f'[tool.pytest.ini_options]\ntestpaths = [\n    "{SUITE}",\n]\n',
    f"{SUITE}/test_app.py": "",
    PYTHON_PATH: "",
    "uv.lock": "",
    "rust/Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
    f"{CRATE}/Cargo.toml": INHERITS,
    RUST_PATH: "",
}


@dataclass(frozen=True)
class Run:
    """One run of a script: its exit code, its output and what it called."""

    code: int
    out: str
    err: str
    uv: list[str]
    cargo: list[str]
    cargo_dirs: set[str]


@dataclass(frozen=True)
class Tree:
    """The throwaway repository, and the two PATHs a run can have."""

    root: Path
    with_cargo: str
    without_cargo: str

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr

        return done.stdout.strip()

    def write(self, name: str, body: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def commit(self, name: str, body: str) -> str:
        """Writes one file and commits it. Returns the commit."""
        self.write(name, body)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"change {name}")

        return self.git("rev-parse", "HEAD")

    def run(self, script: str, *args: str, cargo: bool = True, fail: str = "") -> Run:
        """Runs one of the copied scripts. `fail` names a cargo subcommand
        that exits 1."""
        logs = self.root.parent / "logs"
        shutil.rmtree(logs, ignore_errors=True)
        logs.mkdir()
        done = subprocess.run(
            [str(self.root / script), *args],
            env={
                "PATH": self.with_cargo if cargo else self.without_cargo,
                "UV_LOG": str(logs / "uv"),
                "CARGO_LOG": str(logs / "cargo"),
                "CARGO_FAIL": fail,
                # A temporary directory inside a checkout must not lend the
                # run that checkout's git state.
                "GIT_CEILING_DIRECTORIES": str(self.root.parent),
            },
            cwd=self.root.parent,
            capture_output=True,
            text=True,
            timeout=60,
        )
        calls = [line.split("\t") for line in _lines(logs / "cargo")]

        return Run(
            code=done.returncode,
            out=done.stdout,
            err=done.stderr,
            uv=_lines(logs / "uv"),
            cargo=[argv for _where, argv in calls],
            cargo_dirs={where for where, _argv in calls},
        )

    def rule(self, script: str) -> int:
        """Sources the rule in the repository, runs `script`, returns its
        exit code."""
        done = subprocess.run(
            ["bash", "-c", f". {RULE}\n{script}"],
            env={
                "PATH": self.without_cargo,
                "GIT_CEILING_DIRECTORIES": str(self.root.parent),
            },
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )

        return done.returncode


def _git_env() -> dict[str, str]:
    """This process's environment, with no git state of the caller's and no
    personal git config (githooks/pre-commit says why)."""
    leaked = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    )
    env = {name: value for name, value in os.environ.items() if name not in leaked}

    return env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    return path.read_text(encoding="utf-8").splitlines()


def _fake(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """A committed repository with one Python package and one crate, the
    real gate copied in, and a PATH with and without the fake cargo."""
    root = tmp_path / "repo"
    tools = tmp_path / "tools"
    rusty = tmp_path / "rusty"
    for one in (root, tools, rusty):
        one.mkdir()

    for name in TOOLS:
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH"
        (tools / name).symlink_to(found)

    _fake(tools / "uv", FAKE_UV)
    _fake(rusty / "cargo", FAKE_CARGO)

    made = Tree(root=root, with_cargo=f"{rusty}:{tools}", without_cargo=str(tools))
    assert shutil.which("cargo", path=made.without_cargo) is None
    assert shutil.which("cargo", path=made.with_cargo) == str(rusty / "cargo")

    for name in COPIED:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BIN.parent / name, root / name)

    for name, body in FIXTURE.items():
        made.write(name, body)

    made.git("init", "-q", "-b", "main")
    made.git("add", "-A")
    made.git("commit", "-q", "-m", "the tree")

    return made


def _pytest_calls(done: Run) -> list[str]:
    return [line for line in done.uv if line.startswith(PYTEST)]


def _passed(done: Run) -> bool:
    return done.code == 0 and "quality-gate: PASS" in done.out


# --- a change that touches nothing under rust/ -------------------------------


def test_a_python_commit_needs_no_cargo(tree: Tree) -> None:
    tree.write(PYTHON_PATH, "staged = 1\n")
    tree.git("add", "-A")

    done = tree.run(GATE, cargo=False)

    assert _passed(done), done.out + done.err
    assert done.uv == PYTHON_LINT
    assert "rust" not in done.out + done.err


def test_a_python_commit_starts_no_cargo_step(tree: Tree) -> None:
    tree.write(PYTHON_PATH, "unstaged = 1\n")

    done = tree.run(GATE)

    assert _passed(done), done.out + done.err
    assert done.cargo == []


def test_an_untracked_file_under_rust_is_in_no_commit(tree: Tree) -> None:
    tree.write("rust/crates/two/Cargo.toml", "[package]\n")

    done = tree.run(GATE, cargo=False)

    assert _passed(done), done.out + done.err


@pytest.mark.parametrize(
    ("paths", "pytest_line"),
    [
        ([PYTHON_PATH], f"{PYTEST} {SUITE}"),
        ([PYTHON_PATH, "uv.lock"], PYTEST),
        (["docs/rust.md", "rustic/Cargo.toml"], PYTEST),
    ],
    ids=["a package", "a path in no package", "a name that only starts with rust"],
)
def test_a_python_push_needs_no_cargo(tree: Tree, paths: list[str], pytest_line: str) -> None:
    done = tree.run(GATE, "--tests-for", *paths, cargo=False)

    assert _passed(done), done.out + done.err
    assert _pytest_calls(done) == [pytest_line]


def test_a_docs_push_needs_no_cargo(tree: Tree) -> None:
    """The pre-push hook sends a push of nothing but `.md` files here, and a
    `.md` under `rust/` is one of them."""
    tree.write("rust/AGENTS.md", "# rust\n")
    tree.git("add", "-A")

    done = tree.run(GATE, "--docs", cargo=False)

    assert _passed(done), done.out + done.err
    assert _pytest_calls(done) == [f"{PYTEST} -m docs"]


# --- a commit that touches rust/ ---------------------------------------------


def _stage(tree: Tree) -> None:
    tree.write(RUST_PATH, "// staged\n")
    tree.git("add", "-A")


def _edit(tree: Tree) -> None:
    """A change `git commit -a` would carry: in the work tree, not staged."""
    tree.write(RUST_PATH, "// not staged\n")


def _delete(tree: Tree) -> None:
    tree.git("rm", "-q", RUST_PATH)


@pytest.mark.parametrize("change", [_stage, _edit, _delete], ids=["staged", "edited", "deleted"])
def test_a_rust_commit_runs_fmt_and_clippy_and_no_test(
    tree: Tree, change: Callable[[Tree], None]
) -> None:
    change(tree)

    done = tree.run(GATE)

    assert _passed(done), done.out + done.err
    assert done.cargo == LINT_STEPS
    assert _pytest_calls(done) == []
    assert "rust-gate: PASS" in done.out


def test_every_cargo_step_runs_inside_rust(tree: Tree) -> None:
    """rustup reads `rust/rust-toolchain.toml` from the directory cargo
    starts in. A step that ran from the root would take the default
    toolchain instead."""
    _stage(tree)

    done = tree.run(GATE, "--tests")

    assert done.cargo_dirs == {str(tree.root / "rust")}


def test_a_rust_commit_without_cargo_fails_before_any_check(tree: Tree) -> None:
    _stage(tree)

    done = tree.run(GATE, cargo=False)

    assert done.code == 1
    assert "quality-gate: PASS" not in done.out
    assert done.uv == []
    assert done.err.splitlines() == [
        "quality-gate: cargo not on PATH: the Rust checks must run for "
        "a change under rust/ in the index or the work tree"
    ]


def test_a_state_git_cannot_read_counts_as_a_rust_change(tree: Tree) -> None:
    shutil.rmtree(tree.root / ".git")

    done = tree.run(GATE, cargo=False)

    assert done.code == 1
    assert "cargo not on PATH" in done.err


# --- a push that touches rust/ -----------------------------------------------


def test_a_rust_path_runs_the_cargo_tests_and_no_pytest(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", RUST_PATH, "rust/Cargo.lock")

    assert _passed(done), done.out + done.err
    assert done.cargo == TEST_STEPS
    assert done.uv == PYTHON_LINT
    assert f"{RUST_GATE}, for {RUST_PATH} and 1 more" in done.out
    assert "no pytest: every path is under rust/" in done.out


def test_a_push_of_rust_and_python_runs_both(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", PYTHON_PATH, RUST_PATH)

    assert _passed(done), done.out + done.err
    assert done.cargo == TEST_STEPS
    assert _pytest_calls(done) == [f"{PYTEST} {SUITE}"]


def test_a_rust_path_is_not_a_path_in_no_package(tree: Tree) -> None:
    """A path in no package runs the full Python suite. `uv.lock` does, and
    the Rust path beside it adds nothing to that."""
    done = tree.run(GATE, "--tests-for", RUST_PATH, "uv.lock")

    assert _passed(done), done.out + done.err
    assert done.cargo == TEST_STEPS
    assert _pytest_calls(done) == [PYTEST]
    assert "full suite, for uv.lock in no package" in done.out


def test_a_path_git_wrote_in_quotes_still_counts(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", '"rust/crates/one/src/a\\tb.rs"', cargo=False)

    assert done.code == 1
    assert "cargo not on PATH" in done.err


def test_a_rust_push_without_cargo_fails_before_any_check(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", PYTHON_PATH, RUST_PATH, cargo=False)

    assert done.code == 1
    assert done.uv == []
    assert done.err.splitlines() == [
        f"quality-gate: cargo not on PATH: the Rust checks must run for {RUST_PATH}"
    ]


def test_the_full_run_leaves_rust_out_of_nothing(tree: Tree) -> None:
    """The pre-push hook asks for `--tests` when it cannot read what a push
    changes. What it cannot read counts as code, in both languages."""
    done = tree.run(GATE, "--tests")

    assert _passed(done), done.out + done.err
    assert done.cargo == TEST_STEPS
    assert _pytest_calls(done) == [PYTEST]

    without = tree.run(GATE, "--tests", cargo=False)

    assert without.code == 1
    assert without.uv == []
    assert "cargo not on PATH" in without.err


@pytest.mark.parametrize(
    ("fail", "ran"),
    [("fmt", [FMT]), ("clippy", [FMT, CLIPPY]), ("test", TEST_STEPS)],
)
def test_a_failed_cargo_step_fails_the_gate(tree: Tree, fail: str, ran: list[str]) -> None:
    done = tree.run(GATE, "--tests-for", PYTHON_PATH, RUST_PATH, fail=fail)

    assert done.code != 0
    assert "quality-gate: PASS" not in done.out
    assert done.cargo == ran
    assert _pytest_calls(done) == []


# --- bin/rust-gate.sh by itself, as CI runs it -------------------------------


def test_the_rust_gate_runs_three_steps_with_tests_and_two_without(tree: Tree) -> None:
    assert tree.run(RUST_GATE).cargo == LINT_STEPS
    assert tree.run(RUST_GATE, "--tests").cargo == TEST_STEPS


def test_the_rust_gate_refuses_an_unknown_flag(tree: Tree) -> None:
    done = tree.run(RUST_GATE, "--test")

    assert done.code == 2
    assert done.cargo == []


def test_the_rust_gate_without_cargo_fails_with_one_line(tree: Tree) -> None:
    done = tree.run(RUST_GATE, "--tests", cargo=False)

    assert done.code == 1
    assert done.err.splitlines() == [
        "rust-gate: cargo not on PATH: a change under rust/ needs the Rust toolchain"
    ]


#: Crate files that do not take the lint gate.
OUTSIDE = {
    "no table": '[package]\nname = "one"\n',
    "false": '[package]\nname = "one"\n\n[lints]\nworkspace = false\n',
    "its own lints": '[package]\nname = "one"\n\n[lints.clippy]\nunwrap_used = "allow"\n',
    "another table": '[package]\nname = "one"\n\n[dependencies.serde]\nworkspace = true\n',
    "a later table": "[lints]\n\n[dependencies.serde]\nworkspace = true\n",
    "a dotted key": '[package]\nname = "one"\nlints.workspace = true\n',
    "a comment": '[package]\nname = "one"\n\n# [lints]\n# workspace = true\n',
}

#: Crate files that do.
INSIDE = {
    "plain": INHERITS,
    "comments": '[lints] # the gate\nworkspace = true # always\n\n[package]\nname = "one"\n',
    "spaces": '[package]\nname = "one"\n\n  [lints]\n  workspace=true\n',
}


@pytest.mark.parametrize("body", OUTSIDE.values(), ids=OUTSIDE.keys())
def test_a_crate_outside_the_lint_gate_is_refused(tree: Tree, body: str) -> None:
    tree.write(f"{CRATE}/Cargo.toml", body)

    done = tree.run(RUST_GATE)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [
        f"rust-gate: {CRATE}/Cargo.toml has no `[lints] workspace = true`"
    ]


@pytest.mark.parametrize("body", INSIDE.values(), ids=INSIDE.keys())
def test_a_crate_inside_the_lint_gate_passes(tree: Tree, body: str) -> None:
    tree.write(f"{CRATE}/Cargo.toml", body)

    done = tree.run(RUST_GATE)

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


def test_every_crate_outside_the_lint_gate_gets_its_own_line(tree: Tree) -> None:
    tree.write("rust/crates/two/Cargo.toml", OUTSIDE["no table"])
    tree.write("rust/tools/three/Cargo.toml", OUTSIDE["false"])

    done = tree.run(RUST_GATE)

    assert done.code == 1
    assert done.err.splitlines() == [
        "rust-gate: rust/crates/two/Cargo.toml has no `[lints] workspace = true`",
        "rust-gate: rust/tools/three/Cargo.toml has no `[lints] workspace = true`",
    ]


def test_a_crate_file_in_cargos_build_directory_is_not_a_crate(tree: Tree) -> None:
    tree.write("rust/target/package/one-0.0.0/Cargo.toml", OUTSIDE["no table"])

    done = tree.run(RUST_GATE)

    assert done.code == 0, done.out + done.err


# --- the rule CI asks: does this change touch rust/? -------------------------


def test_a_change_under_rust_is_a_rust_change(tree: Tree) -> None:
    base = tree.git("rev-parse", "HEAD")
    python = tree.commit(PYTHON_PATH, "later = 1\n")
    rust = tree.commit(RUST_PATH, "// later\n")
    tree.git("rm", "-q", RUST_PATH)
    tree.git("commit", "-q", "-m", "delete a Rust file")
    deleted = tree.git("rev-parse", "HEAD")

    assert tree.rule(f"rust_touched {base} {python}") == 1
    assert tree.rule(f"rust_touched {base} {rust}") == 0
    assert tree.rule(f"rust_touched {python} {rust}") == 0
    assert tree.rule(f"rust_touched {rust} {deleted}") == 0


def test_a_change_git_cannot_read_is_a_rust_change(tree: Tree) -> None:
    """A revision this clone lacks, or no base at all: the cargo checks are
    the safe answer."""
    unknown = "1" * 40

    assert tree.rule(f"rust_touched {unknown} HEAD") == 0
    assert tree.rule("rust_touched '' HEAD") == 0


@pytest.mark.parametrize(
    ("path", "under"),
    [
        ("rust/Cargo.toml", True),
        ("rust/crates/one/src/lib.rs", True),
        ("rust/AGENTS.md", True),
        ('"rust/a\\tb.rs"', True),
        ("rust", False),
        ("rustic/Cargo.toml", False),
        ("chaperone/rust/lib.rs", False),
        ("docs/rust.md", False),
    ],
)
def test_only_a_path_under_rust_is_a_rust_path(tree: Tree, path: str, under: bool) -> None:
    assert (tree.rule(f"rust_path '{path}'") == 0) == under
