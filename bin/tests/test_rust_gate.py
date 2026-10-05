"""When the quality gate runs cargo, and what it runs.

`bin/quality-gate.sh` runs the Rust checks only for a change that touches
`rust/` (`bin/lib/rustrule.sh`). Ten things could go wrong without one red
line, and each gets a check here:

1. **A Python change that needs cargo.** Some sessions commit from a sandbox
   with no Rust toolchain. A commit or a push that touches nothing under
   `rust/` must pass with no `cargo` on PATH and must start no cargo step.
   So must the commit that concludes a merge, when only the other side
   changed `rust/`.
2. **A Rust change that passes on the Python checks alone.** With no `cargo`
   on PATH the gate fails closed, with one line, before any check runs.
3. **A Rust path that starts the full Python suite.** `rust/` is in no Python
   package. In `--tests-for` mode a path there runs `cargo test`, and pytest
   runs only for the other paths.
4. **A crate outside the lint gate.** A crate with no `[lints] workspace =
   true` builds with none of `[workspace.lints]`, and cargo does not say so.
   `bin/rust-gate.sh` refuses it.
5. **A Rust file that reads a Markdown file.** A change of nothing but
   Markdown runs no cargo step, so it could break a doc test with no red
   line. `bin/rust-gate.sh` refuses the include.
6. **A vector that moves and breaks a Rust test.** The Rust tests read
   `vectors/data`. In CI a change under `vectors/` runs the Rust checks. A
   push runs them where `cargo` is on PATH. Where it is not, the push passes
   with one line: a Python session with no Rust toolchain regenerates the
   vectors and must still push.
7. **A product change that moves a vector, found first in CI.** A scoped run
   for a product package carries `vectors/tests` too.
8. **A locked crate that no check read.** `cargo deny` reads the advisories,
   the licenses and the sources of the locked crates (`rust/deny.toml`). It
   needs `cargo-deny`, which rustup does not install. A developer machine
   without it passes with one line. In CI the same gate fails, so a `rust`
   job that lost its install step is red.
9. **A panic boundary that no reviewer knows.** Only three places can hold
   `catch_unwind`: the runtime crate, the entry file of a crate with no
   runtime, and test code (`rust/AGENTS.md`, "The panic rule").
   `bin/rust-gate.sh` refuses the word in each other file.
10. **A struct field that other code can write.** No constructor checks the
    value of a field with `pub` (`rust/AGENTS.md`, rule 12).
    `bin/rust-gate.sh` refuses such a field in each crate that its list
    does not name.

Everything runs the real gate in a throwaway repository. `uv` and `cargo`
are fakes that write their argv to a file. PATH holds only those fakes and
links to the few tools the gate calls, so a `cargo` anywhere on the machine
cannot reach a run that must not have one. The same holds for `cargo-deny`.
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
DOCS_RULE = "bin/lib/docsrule.sh"
COPIED = (GATE, RUST_GATE, RULE, DOCS_RULE)

#: Every program the two scripts and the rule call, but `uv` and `cargo`.
TOOLS = ("bash", "git", "awk", "grep", "tr", "dirname", "find", "sort")

#: What the gate runs for a Rust change, word for word, in this order.
FMT = "fmt --all --check"
CLIPPY = "clippy --workspace --all-targets --locked -- -D warnings"
TEST = "test --workspace --locked"
LINT_STEPS = [FMT, CLIPPY]
TEST_STEPS = [FMT, CLIPPY, TEST]

#: The supply-chain check. It runs after clippy, on a machine that has
#: `cargo-deny` on PATH.
DENY = "deny --locked check"
DENY_LINT_STEPS = [FMT, CLIPPY, DENY]
DENY_TEST_STEPS = [FMT, CLIPPY, DENY, TEST]

#: The one line of a developer machine with no `cargo-deny`, on stdout.
NO_DENY = "rust-gate: cargo-deny not on PATH: no `cargo deny` check. CI runs the check"

#: The one line of CI with no `cargo-deny`, on stderr.
NO_DENY_IN_CI = "rust-gate: cargo-deny not on PATH: CI must run the `cargo deny` check"

#: What a runner sets `CI` to.
IN_CI = "true"

#: The Python checks of every mode, as the fake `uv` sees them.
PYTHON_LINT = ["run ruff check .", "run ruff format --check .", "run pyright"]
PYTEST = "run pytest -n auto"

#: The suite of the one product package of the throwaway repository, and a
#: path in that package.
SUITE = "chaperone/tests"
PYTHON_PATH = "chaperone/src/chaperone/app.py"

#: The suite that holds the vectors equal to the Python code, and a vector
#: file. A scoped run for a product package carries the suite too.
VECTORS_SUITE = "vectors/tests"
VECTORS_PATH = "vectors/data/index.json"

#: What a push of a product package runs: its own suite, then the vectors.
PRODUCT_SUITES = f"{SUITE} {VECTORS_SUITE}"

#: A crate of the throwaway workspace, and a file in it.
CRATE = "rust/crates/one"
RUST_PATH = f"{CRATE}/src/lib.rs"

#: A second file in that crate, for a change this side of a merge makes.
RUST_OWN = f"{CRATE}/src/own.rs"

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

#: The gate asks only whether this program is on PATH. cargo starts it, as
#: `cargo deny`, so a direct call fails the run.
FAKE_DENY = """#!/usr/bin/env bash
exit 1
"""

#: What each `git` command of the throwaway repository gets: the author of a
#: commit, and no maintenance. After a commit or a merge, git starts its
#: maintenance and does not wait for it. That process holds a lock file under
#: `.git` for a short time, and two tests here remove `.git`.
GIT_SETTINGS = (
    "-c",
    "user.email=t@t",
    "-c",
    "user.name=t",
    "-c",
    "maintenance.auto=false",
)

FIXTURE = {
    "pyproject.toml": (
        f'[tool.pytest.ini_options]\ntestpaths = [\n    "{SUITE}",\n    "{VECTORS_SUITE}",\n]\n'
    ),
    f"{SUITE}/test_app.py": "",
    PYTHON_PATH: "",
    f"{VECTORS_SUITE}/test_vectors_current.py": "",
    VECTORS_PATH: "{}\n",
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
    """The throwaway repository, and the three PATHs a run can have."""

    root: Path
    with_cargo: str
    without_cargo: str
    with_deny: str

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *GIT_SETTINGS, *args],
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def git(self, *args: str) -> str:
        done = self._git(*args)
        assert done.returncode == 0, done.stdout + done.stderr

        return done.stdout.strip()

    def conflict(self, branch: str) -> None:
        """Merges `branch`. The merge must stop at a conflict."""
        done = self._git("merge", "-q", branch)
        assert done.returncode == 1, done.stdout + done.stderr
        assert self.git("ls-files", "--unmerged"), "the merge stopped with no conflict"

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

    def run(
        self,
        script: str,
        *args: str,
        cargo: bool = True,
        deny: bool = False,
        fail: str = "",
        ci: str = "",
    ) -> Run:
        """Runs one of the copied scripts. `fail` names a cargo subcommand
        that exits 1. `deny` puts `cargo-deny` on PATH beside `cargo`. `ci`
        is the value of `CI`, and the empty text leaves the variable out."""
        logs = self.root.parent / "logs"
        shutil.rmtree(logs, ignore_errors=True)
        logs.mkdir()
        path = self.with_cargo if cargo else self.without_cargo
        done = subprocess.run(
            [str(self.root / script), *args],
            env={
                "PATH": self.with_deny if deny else path,
                "UV_LOG": str(logs / "uv"),
                "CARGO_LOG": str(logs / "cargo"),
                "CARGO_FAIL": fail,
                # A temporary directory inside a checkout must not lend the
                # run that checkout's git state.
                "GIT_CEILING_DIRECTORIES": str(self.root.parent),
            }
            | ({"CI": ci} if ci else {}),
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
    """A committed repository with one Python package, the vectors and one
    crate, the real gate copied in, and a PATH with and without the fake
    cargo. A third PATH also holds the fake `cargo-deny`."""
    root = tmp_path / "repo"
    tools = tmp_path / "tools"
    rusty = tmp_path / "rusty"
    denying = tmp_path / "denying"
    for one in (root, tools, rusty, denying):
        one.mkdir()

    for name in TOOLS:
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH"
        (tools / name).symlink_to(found)

    _fake(tools / "uv", FAKE_UV)
    _fake(rusty / "cargo", FAKE_CARGO)
    _fake(denying / "cargo-deny", FAKE_DENY)

    made = Tree(
        root=root,
        with_cargo=f"{rusty}:{tools}",
        without_cargo=str(tools),
        with_deny=f"{denying}:{rusty}:{tools}",
    )
    assert shutil.which("cargo", path=made.without_cargo) is None
    assert shutil.which("cargo", path=made.with_cargo) == str(rusty / "cargo")
    assert shutil.which("cargo-deny", path=made.with_cargo) is None
    assert shutil.which("cargo-deny", path=made.with_deny) == str(denying / "cargo-deny")

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
        ([PYTHON_PATH], f"{PYTEST} {PRODUCT_SUITES}"),
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


def test_a_commit_of_the_tree_starts_no_maintenance(
    tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two tests below remove `.git`. After a commit, git starts its
    maintenance and does not wait for it. That process holds a lock file
    under `.git` for a short time. A delete of `.git` in that time finds a
    file that is gone, and the test fails. So no commit of the tree starts
    the maintenance."""
    trace = tmp_path / "trace"
    monkeypatch.setenv("GIT_TRACE", str(trace))

    tree.commit(PYTHON_PATH, "x = 1\n")

    commands = trace.read_text(encoding="utf-8")
    assert "git commit" in commands
    assert "maintenance" not in commands


def test_a_state_git_cannot_read_counts_as_a_rust_change(tree: Tree) -> None:
    """The line names what happened: no path under `rust/` changed, and git
    could not say so."""
    shutil.rmtree(tree.root / ".git")

    done = tree.run(GATE, cargo=False)

    assert done.code == 1
    assert done.uv == []
    assert done.err.splitlines() == [
        "quality-gate: cargo not on PATH: the Rust checks must run for "
        "a state of rust/ that git cannot read"
    ]
    assert tree.run(GATE).cargo == LINT_STEPS


def test_the_rule_tells_a_change_from_a_state_git_cannot_read(tree: Tree) -> None:
    assert tree.rule("rust_dirty") == 1

    _stage(tree)

    assert tree.rule("rust_dirty") == 0

    shutil.rmtree(tree.root / ".git")

    assert tree.rule("rust_dirty") == 2


# --- a merge that brings the other side's Rust change ------------------------


def _merge(tree: Tree, *ours: str) -> None:
    """Merges a branch that changed a Rust file, and resolves the one
    conflict, which is in a Python file. Nothing concludes the merge: this is
    the state the pre-commit hook sees at `git commit`. This side changed
    `ours` before the merge."""
    tree.git("checkout", "-q", "-b", "theirs")
    tree.commit(RUST_PATH, "// theirs\n")
    tree.commit(PYTHON_PATH, "theirs = 1\n")
    tree.git("checkout", "-q", "main")
    for name in (PYTHON_PATH, *ours):
        tree.commit(name, "ours = 1\n")

    tree.conflict("theirs")
    tree.write(PYTHON_PATH, "merged = 1\n")
    tree.git("add", PYTHON_PATH)


def test_a_merge_of_the_other_sides_rust_needs_no_cargo(tree: Tree) -> None:
    """A session with no toolchain merges main, and main holds a Rust change.
    This side changed nothing under `rust/`. The other side's files passed
    the checks where they were made."""
    _merge(tree)

    done = tree.run(GATE, cargo=False)

    assert _passed(done), done.out + done.err
    assert done.uv == PYTHON_LINT


def test_a_merge_of_the_other_sides_rust_starts_no_cargo_step(tree: Tree) -> None:
    _merge(tree)

    done = tree.run(GATE)

    assert _passed(done), done.out + done.err
    assert done.cargo == []


def _drop(tree: Tree) -> None:
    """Deletes the file the merge brought. `-f`: the index holds the other
    side's copy, not the one of `HEAD`."""
    tree.git("rm", "-q", "-f", RUST_PATH)


@pytest.mark.parametrize("change", [_stage, _edit, _drop], ids=["staged", "edited", "deleted"])
def test_a_merge_with_an_own_rust_change_needs_cargo(
    tree: Tree, change: Callable[[Tree], None]
) -> None:
    _merge(tree)
    change(tree)

    without = tree.run(GATE, cargo=False)

    assert without.code == 1
    assert without.uv == []
    assert "cargo not on PATH" in without.err
    assert tree.run(GATE).cargo == LINT_STEPS


def test_a_merge_of_rust_from_both_sides_needs_cargo(tree: Tree) -> None:
    """Each side changed a file under `rust/`. The merged workspace is the
    tree of neither side, so no run checked it."""
    _merge(tree, RUST_OWN)

    without = tree.run(GATE, cargo=False)

    assert without.code == 1
    assert "cargo not on PATH" in without.err
    assert tree.run(GATE).cargo == LINT_STEPS


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
    assert _pytest_calls(done) == [f"{PYTEST} {PRODUCT_SUITES}"]


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


# --- a change under vectors/, which the Rust tests read -----------------------


def _cargo_lines(done: Run) -> list[str]:
    """Every line of a run that names cargo."""
    return [line for line in (done.out + done.err).splitlines() if "cargo" in line]


def test_a_vectors_push_runs_the_cargo_tests_where_cargo_is(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", VECTORS_PATH, "vectors/generate.py")

    assert _passed(done), done.out + done.err
    assert done.cargo == TEST_STEPS
    assert _pytest_calls(done) == [f"{PYTEST} {VECTORS_SUITE}"]
    assert f"{RUST_GATE}, for {VECTORS_PATH} and 1 more" in done.out


def test_a_vectors_push_with_no_cargo_passes_with_one_line(tree: Tree) -> None:
    """A Python session with no Rust toolchain regenerates the vectors. It
    must still push. CI runs the Rust tests for that change."""
    done = tree.run(GATE, "--tests-for", VECTORS_PATH, cargo=False)

    assert _passed(done), done.out + done.err
    assert done.uv == [*PYTHON_LINT, f"{PYTEST} {VECTORS_SUITE}"]
    assert _cargo_lines(done) == [
        f"quality-gate: cargo not on PATH: no Rust test for {VECTORS_PATH}. CI runs the Rust tests"
    ]
    assert done.err == ""


def test_a_product_push_with_a_moved_vector_runs_both_suites(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", PYTHON_PATH, VECTORS_PATH)

    assert _passed(done), done.out + done.err
    assert done.cargo == TEST_STEPS
    assert _pytest_calls(done) == [f"{PYTEST} {PRODUCT_SUITES}"]


def test_a_product_push_alone_starts_no_cargo_step(tree: Tree) -> None:
    """The push changes no vector file, so the Rust tests read what they
    read before. `vectors/tests` fails when the change moves a vector."""
    done = tree.run(GATE, "--tests-for", PYTHON_PATH)

    assert _passed(done), done.out + done.err
    assert done.cargo == []
    assert f"{VECTORS_SUITE}, for a change in a product package" in done.out


def test_a_tree_with_no_vectors_suite_runs_the_package_alone(tree: Tree) -> None:
    """pytest fails on a path that the tree does not hold. When `testpaths`
    names no `vectors/tests`, a product push runs the suite of its package
    and nothing else."""
    tree.write("pyproject.toml", f'[tool.pytest.ini_options]\ntestpaths = [\n    "{SUITE}",\n]\n')

    done = tree.run(GATE, "--tests-for", PYTHON_PATH)

    assert _passed(done), done.out + done.err
    assert _pytest_calls(done) == [f"{PYTEST} {SUITE}"]


def test_a_vectors_commit_runs_no_cargo_step(tree: Tree) -> None:
    """A commit runs no test, in either language. `cargo fmt` and `cargo
    clippy` do not read a vector."""
    tree.write(VECTORS_PATH, '{"moved": 1}\n')
    tree.git("add", "-A")

    with_cargo = tree.run(GATE)
    without = tree.run(GATE, cargo=False)

    assert _passed(with_cargo), with_cargo.out + with_cargo.err
    assert with_cargo.cargo == []
    assert _passed(without), without.out + without.err
    assert _cargo_lines(without) == []


def test_a_push_of_vectors_and_rust_runs_the_rust_gate_one_time(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", VECTORS_PATH, RUST_PATH)

    assert _passed(done), done.out + done.err
    assert done.cargo == TEST_STEPS


def test_a_push_of_vectors_and_rust_with_no_cargo_still_fails(tree: Tree) -> None:
    """The path under `rust/` fails closed. The vector beside it lifts
    nothing."""
    done = tree.run(GATE, "--tests-for", VECTORS_PATH, RUST_PATH, cargo=False)

    assert done.code == 1
    assert done.uv == []
    assert done.err.splitlines() == [
        f"quality-gate: cargo not on PATH: the Rust checks must run for {RUST_PATH}"
    ]


def test_a_failed_cargo_test_fails_a_vectors_push(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", VECTORS_PATH, fail="test")

    assert done.code != 0
    assert "quality-gate: PASS" not in done.out
    assert done.cargo == TEST_STEPS
    assert _pytest_calls(done) == []


def test_a_change_under_vectors_runs_the_rust_checks_in_ci(tree: Tree) -> None:
    """A runner always has cargo, so CI takes no notice line: a vector that
    moves runs the `rust` job."""
    base = tree.git("rev-parse", "HEAD")
    data = tree.commit(VECTORS_PATH, '{"moved": 1}\n')
    generator = tree.commit("vectors/generate.py", "later = 1\n")

    assert tree.rule(f"rust_touched {base} {data}") == 0
    assert tree.rule(f"rust_touched {data} {generator}") == 0


@pytest.mark.parametrize(
    ("path", "under"),
    [
        ("vectors/data/index.json", True),
        ("vectors/generate.py", True),
        ("vectors/README.md", True),
        ('"vectors/data/a\\tb.json"', True),
        ("vectors", False),
        ("vectorsx/data/index.json", False),
        ("chaperone/vectors/index.json", False),
        ("docs/vectors.md", False),
        ("rust/vectors/index.json", False),
    ],
)
def test_only_a_path_under_vectors_is_a_vectors_path(tree: Tree, path: str, under: bool) -> None:
    assert (tree.rule(f"vectors_path '{path}'") == 0) == under


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


# --- the supply-chain check: cargo deny, where cargo-deny is on PATH ---------


def _deny_lines(done: Run) -> list[str]:
    """Every line of a run that names cargo-deny."""
    return [line for line in (done.out + done.err).splitlines() if "cargo-deny" in line]


def test_the_rust_gate_runs_cargo_deny_after_clippy_and_before_the_tests(tree: Tree) -> None:
    without_tests = tree.run(RUST_GATE, deny=True)
    with_tests = tree.run(RUST_GATE, "--tests", deny=True)

    assert without_tests.code == 0, without_tests.out + without_tests.err
    assert without_tests.cargo == DENY_LINT_STEPS
    assert with_tests.code == 0, with_tests.out + with_tests.err
    assert with_tests.cargo == DENY_TEST_STEPS
    assert with_tests.cargo_dirs == {str(tree.root / "rust")}
    assert _deny_lines(with_tests) == []


def test_a_commit_and_a_push_run_cargo_deny_where_it_is(tree: Tree) -> None:
    """The hooks run the same script, so a machine with `cargo-deny` checks
    the locked crates before CI does."""
    _stage(tree)

    commit = tree.run(GATE, deny=True)
    push = tree.run(GATE, "--tests-for", RUST_PATH, deny=True)

    assert _passed(commit), commit.out + commit.err
    assert commit.cargo == DENY_LINT_STEPS
    assert _passed(push), push.out + push.err
    assert push.cargo == DENY_TEST_STEPS


def test_a_machine_with_no_cargo_deny_passes_with_one_line(tree: Tree) -> None:
    """rustup does not install `cargo-deny`. A developer machine without it
    runs each other step, and CI runs the check for the same change."""
    done = tree.run(RUST_GATE, "--tests")

    assert done.code == 0, done.out + done.err
    assert done.cargo == TEST_STEPS
    assert _deny_lines(done) == [NO_DENY]
    assert done.err == ""
    assert "rust-gate: PASS" in done.out


def test_ci_with_no_cargo_deny_fails_and_runs_no_later_step(tree: Tree) -> None:
    """A `rust` job that lost its install step must be red. A green job
    there would be a job that checked no locked crate."""
    done = tree.run(RUST_GATE, "--tests", ci=IN_CI)

    assert done.code == 1
    assert done.cargo == LINT_STEPS
    assert done.err.splitlines() == [NO_DENY_IN_CI]
    assert "rust-gate: PASS" not in done.out


def test_ci_with_cargo_deny_runs_the_check(tree: Tree) -> None:
    done = tree.run(RUST_GATE, "--tests", deny=True, ci=IN_CI)

    assert done.code == 0, done.out + done.err
    assert done.cargo == DENY_TEST_STEPS
    assert _deny_lines(done) == []


def test_a_failed_cargo_deny_fails_the_gate(tree: Tree) -> None:
    done = tree.run(GATE, "--tests-for", PYTHON_PATH, RUST_PATH, deny=True, fail="deny")

    assert done.code != 0
    assert "quality-gate: PASS" not in done.out
    assert done.cargo == DENY_LINT_STEPS
    assert _pytest_calls(done) == []


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


def test_a_crate_in_a_directory_named_target_is_still_a_crate(tree: Tree) -> None:
    """Only `rust/target` is cargo's build directory. `crates/*` makes
    `crates/target` a workspace member like any other."""
    tree.write("rust/crates/target/Cargo.toml", OUTSIDE["no table"])

    done = tree.run(RUST_GATE)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [
        "rust-gate: rust/crates/target/Cargo.toml has no `[lints] workspace = true`"
    ]


def test_a_check_that_read_no_crate_fails(tree: Tree) -> None:
    """A search that finds nothing has checked nothing: a moved workspace,
    or a `find` that failed."""
    (tree.root / CRATE / "Cargo.toml").unlink()

    done = tree.run(RUST_GATE)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [
        "rust-gate: the `[lints]` check read no crate under rust/",
    ]


#: Lines of Rust that include a Markdown file.
MD_INCLUDES = {
    "a crate doc": '#![doc = include_str!("../README.md")]\n',
    "an item doc": '#[doc = include_str!("../../../AGENTS.md")]\npub struct One;\n',
    "bytes": 'const TEXT: &[u8] = include_bytes!("notes.md");\n',
    "code": 'include!("generated.md");\n',
    "a built path": 'const TEXT: &str = include_str!(concat!(env!("OUT_DIR"), "/a.md"));\n',
}

#: Lines of Rust that include none.
OTHER_INCLUDES = {
    "a text file": 'const TEXT: &str = include_str!("vectors.txt");\n',
    "a name in a string": 'const NAME: &str = "README.md";\n',
    "no include": "pub struct One;\n",
}


@pytest.mark.parametrize("body", MD_INCLUDES.values(), ids=MD_INCLUDES.keys())
def test_a_rust_file_that_includes_markdown_is_refused(tree: Tree, body: str) -> None:
    """A push of nothing but Markdown runs no cargo step. A doc test that
    reads a `.md` would then break on main with no cargo run."""
    tree.write(RUST_PATH, body)

    done = tree.run(RUST_GATE)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [f"rust-gate: {RUST_PATH} includes a Markdown file"]


@pytest.mark.parametrize("body", OTHER_INCLUDES.values(), ids=OTHER_INCLUDES.keys())
def test_a_rust_file_that_includes_no_markdown_passes(tree: Tree, body: str) -> None:
    tree.write(RUST_PATH, body)

    done = tree.run(RUST_GATE)

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


def test_every_rust_file_that_includes_markdown_gets_its_own_line(tree: Tree) -> None:
    tree.write(RUST_PATH, MD_INCLUDES["a crate doc"])
    tree.write(f"{CRATE}/tests/wire.rs", MD_INCLUDES["bytes"])
    tree.write("rust/target/debug/build/out.rs", MD_INCLUDES["code"])
    tree.write("rust/AGENTS.md", 'Do not write `include_str!("README.md")`.\n')

    done = tree.run(RUST_GATE)

    assert done.code == 1
    assert done.err.splitlines() == [
        f"rust-gate: {RUST_PATH} includes a Markdown file",
        f"rust-gate: {CRATE}/tests/wire.rs includes a Markdown file",
    ]


# --- the panic check: where a file can hold `catch_unwind` --------------------


def _crate_file(name: str, *more: str) -> str:
    """A crate file that takes the lint gate, with `more` lines below it."""
    return "\n".join([f'[package]\nname = "{name}"\n\n[lints]\nworkspace = true', *more, ""])


#: The crate whose code can catch a panic, and the entry file of each other
#: crate.
RUNTIME_CRATE = "rust/crates/creche-runtime"
ENTRY_PATH = f"{CRATE}/src/entry.rs"

#: A Rust file that catches a panic outside test code.
CATCHES = "pub fn run() {\n    let _ = std::panic::catch_unwind(|| ());\n}\n"

#: A test module that catches a panic.
CATCHING_TESTS = """
#[cfg(test)]
mod tests {
    #[test]
    fn a_panic_is_caught() {
        assert!(std::panic::catch_unwind(|| panic!("stop")).is_err());
    }
}
"""


#: A test module with no last line: the file ends inside it.
OPEN_TESTS = "#[cfg(test)]\nmod tests {\n    use std::panic::catch_unwind;\n"

#: The end of a test module that holds a comment too, as `cargo fmt` keeps it.
COMMENT_ENDS = {
    "a line comment": "} // mod tests",
    "a block comment": "} /* mod tests */",
}


def _crlf(text: str) -> str:
    """`text` with CR LF at the end of each line. `cargo fmt` keeps such a
    file as it is."""
    return text.replace("\n", "\r\n")


def _stray_catch(path: str) -> str:
    """The line of the panic check for one file."""
    return f"rust-gate: {path} holds `catch_unwind` outside the three places of the panic rule"


def _run_on(tree: Tree, files: dict[str, str]) -> Run:
    """Writes `files` and runs the Rust gate."""
    for path, body in files.items():
        tree.write(path, body)

    return tree.run(RUST_GATE)


def test_the_runtime_crate_can_catch_a_panic(tree: Tree) -> None:
    """The runtime holds each boundary of a service: the edge layer, the
    loop and the tracked task."""
    done = _run_on(
        tree,
        {
            f"{RUNTIME_CRATE}/Cargo.toml": _crate_file("creche-runtime"),
            f"{RUNTIME_CRATE}/src/tasks.rs": CATCHES,
        },
    )

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


#: Crate files that name no dependency on the runtime crate.
NO_RUNTIME = {
    "no dependency": _crate_file("one"),
    "the contracts only": _crate_file(
        "one", "", "[dependencies]", 'creche-contracts = { path = "../creche-contracts" }'
    ),
    "a longer name": _crate_file(
        "one", "", "[dev-dependencies]", 'creche-runtime-fakes = { path = "../fakes" }'
    ),
}


@pytest.mark.parametrize("crate_file", NO_RUNTIME.values(), ids=NO_RUNTIME.keys())
def test_the_entry_file_of_a_crate_with_no_runtime_can_catch_a_panic(
    tree: Tree, crate_file: str
) -> None:
    """A program with no runtime has no boundary but its own. Its one place
    for the call is `src/entry.rs`."""
    done = _run_on(tree, {f"{CRATE}/Cargo.toml": crate_file, ENTRY_PATH: CATCHES})

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


#: Test code that catches a panic: a path, and the text of the file.
CATCHING_TEST_CODE = {
    "a tests directory": (f"{CRATE}/tests/boundary.rs", CATCHES),
    "a directory below the tests directory": (f"{CRATE}/tests/common/mod.rs", CATCHES),
    "a test module": (RUST_PATH, "pub fn run() {}\n" + CATCHING_TESTS),
    "a test module with CR LF line ends": (
        RUST_PATH,
        _crlf("pub fn run() {}\n" + CATCHING_TESTS + "\npub fn later() {}\n"),
    ),
    "the second function of a test module": (
        RUST_PATH,
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    fn first() {\n"
        "        let _ = 1;\n"
        "    }\n\n"
        "    fn second() {\n"
        "        let _ = std::panic::catch_unwind(|| ());\n"
        "    }\n"
        "}\n",
    ),
    "a test module below a longer name": (
        RUST_PATH,
        "pub fn my_catch_unwind_helper() {}\n" + CATCHING_TESTS,
    ),
    "a test module that the crate can use": (
        RUST_PATH,
        "pub fn run() {}\n" + CATCHING_TESTS.replace("mod tests", "pub(crate) mod testing"),
    ),
    "a test module inside a module": (
        RUST_PATH,
        "pub mod wire {\n"
        "    #[cfg(test)]\n"
        "    mod tests {\n"
        "        use std::panic::catch_unwind;\n"
        "    }\n"
        "}\n\n"
        "pub fn later() {}\n",
    ),
}


@pytest.mark.parametrize(
    ("path", "body"), CATCHING_TEST_CODE.values(), ids=CATCHING_TEST_CODE.keys()
)
def test_test_code_can_catch_a_panic(tree: Tree, path: str, body: str) -> None:
    done = _run_on(tree, {path: body})

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


#: Names that hold the word and are not the word.
LONGER_NAMES = {
    "the word at the end": "pub fn my_catch_unwind() {}\n",
    "the word at the start": "pub fn catch_unwinding() {}\n",
}


@pytest.mark.parametrize("body", LONGER_NAMES.values(), ids=LONGER_NAMES.keys())
def test_a_longer_name_is_not_a_panic_catch(tree: Tree, body: str) -> None:
    done = _run_on(tree, {RUST_PATH: body})

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


#: Files that hold the word in another place. Each case is the files of the
#: tree, then each file that the check refuses.
STRAY_CATCHES: dict[str, tuple[dict[str, str], list[str]]] = {
    "a library file": ({RUST_PATH: CATCHES}, [RUST_PATH]),
    "the entry file of a crate on the runtime": (
        {
            f"{CRATE}/Cargo.toml": _crate_file(
                "one", "", "[dependencies]", 'creche-runtime = { path = "../creche-runtime" }'
            ),
            ENTRY_PATH: CATCHES,
        },
        [ENTRY_PATH],
    ),
    "the entry file of a crate with the runtime in its tests": (
        {
            f"{CRATE}/Cargo.toml": _crate_file(
                "one", "", "[dev-dependencies.creche-runtime]", 'path = "../creche-runtime"'
            ),
            ENTRY_PATH: CATCHES,
        },
        [ENTRY_PATH],
    ),
    "the entry file of a crate that gives the runtime another name": (
        {
            f"{CRATE}/Cargo.toml": _crate_file(
                "one", "", "[dependencies]", 'runtime = { package = "creche-runtime" }'
            ),
            ENTRY_PATH: CATCHES,
        },
        [ENTRY_PATH],
    ),
    "an entry file in another directory": (
        {f"{CRATE}/src/bin/entry.rs": CATCHES},
        [f"{CRATE}/src/bin/entry.rs"],
    ),
    "above the test module": ({RUST_PATH: CATCHES + CATCHING_TESTS}, [RUST_PATH]),
    "below the test module": (
        {RUST_PATH: "#[cfg(test)]\nmod tests {\n    use super::run;\n}\n\n" + CATCHES},
        [RUST_PATH],
    ),
    "a comment": (
        {RUST_PATH: "/// The runtime calls `catch_unwind` for this crate.\npub fn run() {}\n"},
        [RUST_PATH],
    ),
    "below a test mark on one item": (
        {RUST_PATH: "#[cfg(test)]\nmod python;\n\n" + CATCHES},
        [RUST_PATH],
    ),
    "in a module below a test mark on one item": (
        {
            RUST_PATH: (
                "#[cfg(test)]\nmod python;\n\n"
                "pub mod wire {\n"
                "    pub fn run() {\n"
                "        let _ = std::panic::catch_unwind(|| ());\n"
                "    }\n"
                "}\n"
            )
        },
        [RUST_PATH],
    ),
    "below a test module that ends with a line comment": (
        {
            RUST_PATH: (
                "#[cfg(test)]\nmod tests {\n    use super::run;\n"
                + COMMENT_ENDS["a line comment"]
                + "\n\n"
                + CATCHES
            )
        },
        [RUST_PATH],
    ),
    "below a test module that ends with a block comment": (
        {
            RUST_PATH: (
                "#[cfg(test)]\nmod tests {\n    use super::run;\n"
                + COMMENT_ENDS["a block comment"]
                + "\n\n"
                + CATCHES
            )
        },
        [RUST_PATH],
    ),
    "below a test module, with CR LF line ends": (
        {RUST_PATH: _crlf("#[cfg(test)]\nmod tests {\n    use super::run;\n}\n\n" + CATCHES)},
        [RUST_PATH],
    ),
    "a tests directory below src": (
        {f"{CRATE}/src/tests/boundary.rs": CATCHES},
        [f"{CRATE}/src/tests/boundary.rs"],
    ),
    "a crate with the name tests": (
        {
            "rust/crates/tests/Cargo.toml": _crate_file("tests"),
            "rust/crates/tests/src/lib.rs": CATCHES,
        },
        ["rust/crates/tests/src/lib.rs"],
    ),
    "a crate with a longer name than the runtime": (
        {
            f"{RUNTIME_CRATE}-fakes/Cargo.toml": _crate_file("creche-runtime-fakes"),
            f"{RUNTIME_CRATE}-fakes/src/lib.rs": CATCHES,
        },
        [f"{RUNTIME_CRATE}-fakes/src/lib.rs"],
    ),
    "the entry file of a crate with no crate file": (
        {"rust/crates/two/src/entry.rs": CATCHES},
        ["rust/crates/two/src/entry.rs"],
    ),
    "two files, with a line for each one": (
        {
            RUST_PATH: CATCHES,
            f"{CRATE}/src/boundary.rs": CATCHES,
            f"{CRATE}/tests/boundary.rs": CATCHES,
            "rust/target/debug/build/out.rs": CATCHES,
        },
        [f"{CRATE}/src/boundary.rs", RUST_PATH],
    ),
}


@pytest.mark.parametrize(("files", "refused"), STRAY_CATCHES.values(), ids=STRAY_CATCHES.keys())
def test_a_panic_catch_in_another_place_is_refused(
    tree: Tree, files: dict[str, str], refused: list[str]
) -> None:
    """A reviewer reads three places for a boundary. A call in a fourth
    place is a boundary that no reviewer reads. The check reads the text, so
    the word in a comment counts too."""
    done = _run_on(tree, files)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [_stray_catch(path) for path in refused]


def test_a_panic_catch_in_a_test_module_with_no_end_is_refused(tree: Tree) -> None:
    """The scan reads no code below the first line of a test module that it
    finds no end of. A check that passed then would pass the word in code
    that no scan read."""
    done = _run_on(tree, {RUST_PATH: OPEN_TESTS})

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [
        f"rust-gate: {RUST_PATH} holds a test module, and the panic check found no end of it"
    ]


# --- the public-field check: no struct field with `pub` -----------------------

#: The crates that the public-field check does not read yet, entry for entry.
#: A crate leaves the list when no struct of it has a public field.
FIELD_CHECK_SKIPS = ("agent-family", "creche-contracts", "creche-runtime", "creche-testkit")

#: A second source file of the crate.
OTHER_PATH = f"{CRATE}/src/wire.rs"


def _public_field(path: str, struct: str, field: str) -> str:
    """The line of the public-field check for one field."""
    return f"rust-gate: {path} gives the struct `{struct}` the public field `{field}`"


#: Structs with named fields. Each case is the files of the tree, then each
#: field that the check refuses: its file, its struct and its name.
PUBLIC_FIELDS: dict[str, tuple[dict[str, str], list[tuple[str, str, str]]]] = {
    "one field": (
        {RUST_PATH: "pub struct One {\n    pub name: String,\n}\n"},
        [(RUST_PATH, "One", "name")],
    ),
    "a private struct": (
        {RUST_PATH: "struct One {\n    pub name: String,\n}\n"},
        [(RUST_PATH, "One", "name")],
    ),
    "beside a private field and a field for the crate": (
        {
            RUST_PATH: (
                "pub(crate) struct One {\n"
                "    id: u32,\n"
                "    pub(crate) kind: Kind,\n"
                "    pub name: String,\n"
                "}\n"
            )
        },
        [(RUST_PATH, "One", "name")],
    ),
    "below a comment and an attribute": (
        {
            RUST_PATH: (
                "/// A record.\n"
                "#[derive(Debug, Clone)]\n"
                "pub struct One {\n"
                "    /// A `pub` word, a comma, and a brace } that closes nothing.\n"
                '    #[serde(rename = "pub alias: {", default)]\n'
                "    pub name: String, // pub tail: u8,\n"
                "    /* pub block: u8, */\n"
                "    #[arg(short = '}')]\n"
                '    #[doc = r#"a quote " and a brace }"#]\n'
                "    pub last: char,\n"
                "}\n"
            )
        },
        [(RUST_PATH, "One", "last"), (RUST_PATH, "One", "name")],
    ),
    "a generic struct with a where clause": (
        {
            RUST_PATH: (
                "pub struct One<'a, F: Fn(u32) -> u32>\n"
                "where\n"
                "    F: Send,\n"
                "{\n"
                "    pub call: F,\n"
                "    pub text: &'a str,\n"
                "}\n"
            )
        },
        [(RUST_PATH, "One", "call"), (RUST_PATH, "One", "text")],
    ),
    "a type over more than one line": (
        {
            RUST_PATH: (
                "pub struct One {\n"
                "    pub pairs: std::collections::HashMap<\n"
                "        String,\n"
                "        Vec<(u32, [u8; 1 << 4])>,\n"
                "    >,\n"
                "    pub call: fn(u8) -> u8,\n"
                "    count: u8,\n"
                "    pub name: String,\n"
                "}\n"
            )
        },
        [(RUST_PATH, "One", "call"), (RUST_PATH, "One", "name"), (RUST_PATH, "One", "pairs")],
    ),
    "a visibility with a path": (
        {RUST_PATH: "pub struct One {\n    pub(in crate::wire) name: String,\n}\n"},
        [(RUST_PATH, "One", "name")],
    ),
    "a visibility for the module of the struct": (
        {RUST_PATH: "pub struct One {\n    pub(self) name: String,\n}\n"},
        [(RUST_PATH, "One", "name")],
    ),
    "a struct inside a function": (
        {RUST_PATH: "pub fn run() {\n    struct Local {\n        pub seen: bool,\n    }\n}\n"},
        [(RUST_PATH, "Local", "seen")],
    ),
    "below a test mark on one item": (
        {RUST_PATH: "#[cfg(test)]\nmod python;\n\npub struct One {\n    pub name: String,\n}\n"},
        [(RUST_PATH, "One", "name")],
    ),
    "in a module below a test mark on one item": (
        {
            RUST_PATH: (
                "#[cfg(test)]\nmod python;\n\n"
                "pub mod wire {\n    pub struct One {\n        pub name: String,\n    }\n}\n"
            )
        },
        [(RUST_PATH, "One", "name")],
    ),
    "below the test module": (
        {
            RUST_PATH: (
                "#[cfg(test)]\nmod tests {\n    pub struct Probe {\n        pub seen: u32,\n"
                "    }\n}\n\nmod later;\n\npub struct One {\n    pub name: String,\n}\n"
            )
        },
        [(RUST_PATH, "One", "name")],
    ),
    "below a test module that ends with a line comment": (
        {
            RUST_PATH: (
                "#[cfg(test)]\nmod tests {\n    use super::One;\n"
                + COMMENT_ENDS["a line comment"]
                + "\n\npub struct One {\n    pub name: String,\n}\n"
            )
        },
        [(RUST_PATH, "One", "name")],
    ),
    "below a test module that ends with a block comment": (
        {
            RUST_PATH: (
                "#[cfg(test)]\nmod tests {\n    use super::One;\n"
                + COMMENT_ENDS["a block comment"]
                + "\n\npub struct One {\n    pub name: String,\n}\n"
            )
        },
        [(RUST_PATH, "One", "name")],
    ),
    "CR LF line ends": (
        {RUST_PATH: _crlf("pub struct One {\n    id: u32,\n    pub name: String,\n}\n")},
        [(RUST_PATH, "One", "name")],
    ),
    "a CR with no LF": (
        {RUST_PATH: "pub struct One {\r    id: u32,\r    pub name: String,\r}\n"},
        [(RUST_PATH, "One", "name")],
    ),
    "below a test module, with CR LF line ends": (
        {
            RUST_PATH: _crlf(
                "#[cfg(test)]\nmod tests {\n    use super::One;\n}\n\n"
                "pub struct One {\n    pub name: String,\n}\n"
            )
        },
        [(RUST_PATH, "One", "name")],
    ),
    "a name with a letter that is not ASCII": (
        {RUST_PATH: "pub struct École {\n    pub name: String,\n}\n"},
        [(RUST_PATH, "École", "name")],
    ),
    "a where clause with a brace inside a bracket": (
        {
            RUST_PATH: (
                "pub struct One\n"
                "where\n"
                "    [u8; { 1 + 1 }]: Sized,\n"
                "    (u8, [u8; { 2 }]): Sized,\n"
                "{\n"
                "    pub name: String,\n"
                "}\n"
            )
        },
        [(RUST_PATH, "One", "name")],
    ),
    "a tests directory below src": (
        {f"{CRATE}/src/tests/common.rs": "pub struct Probe {\n    pub seen: u32,\n}\n"},
        [(f"{CRATE}/src/tests/common.rs", "Probe", "seen")],
    ),
    "a crate with the name tests": (
        {
            "rust/crates/tests/Cargo.toml": _crate_file("tests"),
            "rust/crates/tests/src/lib.rs": "pub struct One {\n    pub name: String,\n}\n",
        },
        [("rust/crates/tests/src/lib.rs", "One", "name")],
    ),
    "a crate with a part of a name of the list": (
        {
            "rust/crates/creche/Cargo.toml": _crate_file("creche"),
            "rust/crates/creche/src/lib.rs": "pub struct One {\n    pub name: String,\n}\n",
            "rust/crates/testkit/Cargo.toml": _crate_file("testkit"),
            "rust/crates/testkit/src/lib.rs": "pub struct Two {\n    pub name: String,\n}\n",
        },
        [
            ("rust/crates/creche/src/lib.rs", "One", "name"),
            ("rust/crates/testkit/src/lib.rs", "Two", "name"),
        ],
    ),
    "two files, with a line for each field": (
        {
            RUST_PATH: (
                "pub struct One {\n    pub name: String,\n}\n\n"
                "pub struct Two {\n    pub left: u8,\n    pub right: u8,\n}\n"
            ),
            OTHER_PATH: "pub struct Wire {\n    pub body: Vec<u8>,\n}\n",
            "rust/target/debug/build/out.rs": "pub struct Built {\n    pub out: u8,\n}\n",
        },
        [
            (RUST_PATH, "One", "name"),
            (RUST_PATH, "Two", "left"),
            (RUST_PATH, "Two", "right"),
            (OTHER_PATH, "Wire", "body"),
        ],
    ),
}


@pytest.mark.parametrize(("files", "fields"), PUBLIC_FIELDS.values(), ids=PUBLIC_FIELDS.keys())
def test_a_struct_with_a_public_field_is_refused(
    tree: Tree, files: dict[str, str], fields: list[tuple[str, str, str]]
) -> None:
    """Other code can write a public field, so no constructor checks its
    value. The line names the file, the struct and the field."""
    done = _run_on(tree, files)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [_public_field(*field) for field in fields]


#: Tuple structs. Each case is the text of the file, then the place of each
#: part that the check refuses. The first part has the place 0.
PUBLIC_PARTS = {
    "one part": ("pub struct Pair(pub f64);\n", ["0"]),
    "the second part": ("pub struct Pair(u32, pub String);\n", ["1"]),
    "each part": ("pub struct Pair(pub u32, pub String);\n", ["0", "1"]),
    "more than one line": (
        "pub struct Pair(\n"
        "    /// The left part.\n"
        "    pub(crate) u32,\n"
        "    #[doc(hidden)] pub Vec<(u8, u8)>,\n"
        ");\n",
        ["1"],
    ),
    "a generic part with a where clause": (
        "pub struct Pair<F>(pub F)\nwhere\n    F: Fn(u8) -> u8;\n",
        ["0"],
    ),
    "a part that is a tuple": ("pub struct Pair(pub (u8, u8));\n", ["0"]),
    "more than one line, with CR LF line ends": (
        _crlf("pub struct Pair(\n    pub u32,\n    String,\n    pub Vec<u8>,\n);\n"),
        ["0", "2"],
    ),
}


@pytest.mark.parametrize(("body", "places"), PUBLIC_PARTS.values(), ids=PUBLIC_PARTS.keys())
def test_a_tuple_struct_with_a_public_part_is_refused(
    tree: Tree, body: str, places: list[str]
) -> None:
    done = _run_on(tree, {RUST_PATH: body})

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [_public_field(RUST_PATH, "Pair", place) for place in places]


#: Structs whose fields are not public: rule 12 permits `pub(crate)` and
#: `pub(super)`.
INSIDE_FIELDS = {
    "a field for the crate": "pub struct One {\n    pub(crate) name: String,\n}\n",
    "a field for the parent module": "pub struct One {\n    pub(super) name: String,\n}\n",
    "a private field": "pub struct One {\n    name: String,\n}\n",
    "a tuple struct": "pub struct Pair(pub(crate) u32, pub(super) (u8, u8), String);\n",
    "a field with pub at the start of its name": (
        "pub struct One {\n    public: bool,\n    pub_key: String,\n}\n"
    ),
}


@pytest.mark.parametrize("body", INSIDE_FIELDS.values(), ids=INSIDE_FIELDS.keys())
def test_a_field_for_the_crate_or_the_parent_passes(tree: Tree, body: str) -> None:
    done = _run_on(tree, {RUST_PATH: body})

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


#: Test code with a public field: a path, and the text of the file.
FIELDS_IN_TEST_CODE = {
    "a test module": (
        RUST_PATH,
        "pub struct One {\n    name: String,\n}\n\n"
        "#[cfg(test)]\nmod tests {\n    pub struct Probe {\n        pub seen: u32,\n    }\n}\n",
    ),
    "a test module that the crate can use": (
        RUST_PATH,
        "#[cfg(test)]\npub(crate) mod testing {\n    pub struct Probe {\n        pub seen: u32,\n"
        "    }\n}\n",
    ),
    "a test module inside a module": (
        RUST_PATH,
        "pub mod wire {\n    #[cfg(test)]\n    mod tests {\n        pub struct Probe {\n"
        "            pub seen: u32,\n        }\n    }\n}\n\n"
        "pub struct One {\n    name: String,\n}\n",
    ),
    "a test module with CR LF line ends": (
        RUST_PATH,
        _crlf(
            "#[cfg(test)]\nmod tests {\n    pub struct Probe {\n        pub seen: u32,\n    }\n}\n"
            "\npub struct One {\n    name: String,\n}\n"
        ),
    ),
    "a test module that ends with a line comment": (
        RUST_PATH,
        "#[cfg(test)]\nmod tests {\n    pub struct Probe(pub u32);\n"
        + COMMENT_ENDS["a line comment"]
        + "\n",
    ),
    "a test module that ends with a block comment": (
        RUST_PATH,
        "#[cfg(test)]\nmod tests {\n    pub struct Probe(pub u32);\n"
        + COMMENT_ENDS["a block comment"]
        + "\n",
    ),
    "below the first function of a test module": (
        RUST_PATH,
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    fn first() {\n"
        "        let _ = 1;\n"
        "    }\n\n"
        "    pub struct Probe {\n"
        "        pub seen: u32,\n"
        "    }\n"
        "}\n",
    ),
    "below a line of a text that starts with a brace": (
        RUST_PATH,
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    const SAMPLE: &str = r#"{\n'
        '  "name": "one"\n'
        '}"#;\n\n'
        "    pub struct Probe {\n"
        "        pub seen: u32,\n"
        "    }\n"
        "}\n",
    ),
    "a tests directory": (
        f"{CRATE}/tests/common.rs",
        "pub struct Probe {\n    pub seen: u32,\n}\n",
    ),
    "a directory below the tests directory": (
        f"{CRATE}/tests/common/mod.rs",
        "pub struct Probe(pub u32);\n",
    ),
}


@pytest.mark.parametrize(
    ("path", "body"), FIELDS_IN_TEST_CODE.values(), ids=FIELDS_IN_TEST_CODE.keys()
)
def test_a_public_field_in_test_code_passes(tree: Tree, path: str, body: str) -> None:
    """A probe or a fake of a test holds no value of the platform."""
    done = _run_on(tree, {path: body})

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


#: Text with `pub` that is no field of a struct.
NO_FIELDS = {
    "an impl block": (
        "pub struct One {\n    name: String,\n}\n\n"
        "impl One {\n"
        "    pub const LIMIT: usize = 8;\n\n"
        "    pub fn new(name: String) -> Self {\n        Self { name }\n    }\n\n"
        "    pub fn name(&self) -> &str {\n        &self.name\n    }\n"
        "}\n"
    ),
    "items of a module": (
        "pub mod wire;\n\npub use wire::Line;\n\npub const LIMIT: usize = 8;\n\n"
        "pub trait Named {\n    fn name(&self) -> &str;\n}\n\npub fn run() {}\n"
    ),
    "structs with no field": "pub struct Marker;\n\npub struct Empty {}\n\npub struct Unit();\n",
    "a struct in a comment": (
        "/// Do not write this:\n"
        "///\n"
        "/// pub struct Open {\n"
        "///     pub name: String,\n"
        "/// }\n"
        "// struct Old(pub u8);\n"
        "pub struct One {\n    name: String,\n}\n"
    ),
    "a struct in a text": (
        'pub const SAMPLE: &str = "pub struct Open { pub name: String }";\npub fn structure() {}\n'
    ),
}


@pytest.mark.parametrize("body", NO_FIELDS.values(), ids=NO_FIELDS.keys())
def test_a_public_function_of_an_impl_passes(tree: Tree, body: str) -> None:
    """The check reads the field list of a struct and no other item."""
    done = _run_on(tree, {RUST_PATH: body})

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


def test_an_enum_variant_with_fields_passes(tree: Tree) -> None:
    """A field of a variant is as public as its enum, and Rust permits no
    `pub` there. Rule 12 does not apply to it."""
    body = "pub enum Shape {\n    Circle { radius: u32 },\n    Pair(u32, u32),\n    Point,\n}\n"

    done = _run_on(tree, {RUST_PATH: body})

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


def _struct_with_no_end(tree: Tree) -> None:
    tree.write(RUST_PATH, "pub struct One {\n    name: String,\n")


def _brace_in_a_generic_argument(tree: Tree) -> None:
    tree.write(
        RUST_PATH,
        "pub struct One<const N: usize>\nwhere\n    Holder<{ N }>: Marker,\n"
        "{\n    pub bytes: [u8; N],\n}\n",
    )


def _brace_in_a_later_generic_argument(tree: Tree) -> None:
    tree.write(
        RUST_PATH,
        "pub struct One<const N: usize>\nwhere\n    [u8; (2 > 1) as usize]: Sized,\n"
        "    Holder<{ N }>: Marker,\n{\n    pub bytes: [u8; N],\n}\n",
    )


def _test_module_with_no_end(tree: Tree) -> None:
    tree.write(RUST_PATH, "#[cfg(test)]\nmod tests {\n    pub struct Probe(pub u32);\n")


def _test_module_with_another_last_line(tree: Tree) -> None:
    tree.write(
        RUST_PATH,
        "#[cfg(test)]\nmod tests {\n    pub struct Probe(pub u32);\n} pub struct One(pub u8);\n",
    )


def _source_file_that_is_gone(tree: Tree) -> None:
    (tree.root / CRATE / "src" / "gone.rs").symlink_to("no-such-file.rs")


#: The line for a struct whose end the scan does not find.
NO_END = (
    f"rust-gate: {RUST_PATH} holds the struct `One`, and the public-field check found no end of it"
)

#: The line for a test module whose end the scan does not find.
NO_MODULE_END = (
    f"rust-gate: {RUST_PATH} holds a test module, and the public-field check found no end of it"
)

#: Trees that the scan cannot read to the end. Each case is a function that
#: writes the tree, then the last line of the gate.
NOT_READ = {
    "a struct with no end": (_struct_with_no_end, NO_END),
    "a brace in a generic argument": (_brace_in_a_generic_argument, NO_END),
    "a brace in a later generic argument": (_brace_in_a_later_generic_argument, NO_END),
    "a test module with no end": (_test_module_with_no_end, NO_MODULE_END),
    "a test module with code on its last line": (
        _test_module_with_another_last_line,
        NO_MODULE_END,
    ),
    "a source file that is gone": (
        _source_file_that_is_gone,
        "rust-gate: the public-field check did not read rust/crates",
    ),
}


@pytest.mark.parametrize(("write", "line"), NOT_READ.values(), ids=NOT_READ.keys())
def test_a_scan_that_cannot_read_a_struct_fails_the_check(
    tree: Tree, write: Callable[[Tree], None], line: str
) -> None:
    """A scan that stops early read no field after that place. A check that
    passed then would pass a field that no scan read."""
    write(tree)

    done = tree.run(RUST_GATE)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines()[-1] == line


def test_a_public_field_in_a_crate_of_the_list_passes(tree: Tree) -> None:
    """The four crates still hold public fields, and other changes make
    them private. The list of the script names these crates and no other: a
    name that arrives there takes one more crate out of the check."""
    script = (tree.root / RUST_GATE).read_text(encoding="utf-8")
    files: dict[str, str] = {}
    for name in FIELD_CHECK_SKIPS:
        files[f"rust/crates/{name}/Cargo.toml"] = _crate_file(name)
        files[f"rust/crates/{name}/src/lib.rs"] = "pub struct One {\n    pub name: String,\n}\n"

    done = _run_on(tree, files)

    assert f'\nFIELD_CHECK_SKIPS="{" ".join(FIELD_CHECK_SKIPS)}"\n' in script
    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


def test_a_file_that_the_check_skips_hides_no_other_file(tree: Tree) -> None:
    """One scan reads each file of the tree, in the order of `find`. What
    the scan keeps for a file that it skips must not reach the next file:
    not the skip, and not a test module with no end."""
    skipped = "pub struct One {\n    pub name: String,\n}\n\n#[cfg(test)]\nmod tests {\n"
    read = "pub struct Two {\n    pub name: String,\n}\n"
    others = ("aaa", "one", "zzz")
    files: dict[str, str] = {f"{CRATE}/tests/common.rs": skipped}
    for name in FIELD_CHECK_SKIPS:
        files[f"rust/crates/{name}/Cargo.toml"] = _crate_file(name)
        files[f"rust/crates/{name}/src/lib.rs"] = skipped
    for name in others:
        files[f"rust/crates/{name}/Cargo.toml"] = _crate_file(name)
        files[f"rust/crates/{name}/src/lib.rs"] = read

    done = _run_on(tree, files)

    assert done.code == 1
    assert done.cargo == []
    assert done.err.splitlines() == [
        _public_field(f"rust/crates/{name}/src/lib.rs", "Two", "name") for name in others
    ]


def test_the_check_reads_no_test_module_of_a_file_that_it_skips(tree: Tree) -> None:
    """A test module with no end fails the check only in a file that the
    check reads. It reads no crate of the list and no file below the
    directory `tests` of a crate."""
    body = "#[cfg(test)]\nmod tests {\n    pub struct Probe(pub u32);\n"
    name = FIELD_CHECK_SKIPS[0]

    done = _run_on(
        tree,
        {
            f"rust/crates/{name}/Cargo.toml": _crate_file(name),
            f"rust/crates/{name}/src/lib.rs": body,
            f"{CRATE}/tests/common.rs": body,
        },
    )

    assert done.code == 0, done.out + done.err
    assert done.cargo == LINT_STEPS


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


#: A file of the Rust checks that the throwaway repository holds.
GATE_FILE = RUST_GATE


def test_a_change_to_the_rust_checks_runs_them_in_ci(tree: Tree) -> None:
    """These tests use a fake cargo. A wrong cargo flag in the script would
    merge with no proof, and the next change under `rust/` would be red for
    it. So CI counts the script as a Rust change."""
    base = tree.git("rev-parse", "HEAD")
    changed = tree.commit(GATE_FILE, "#!/usr/bin/env bash\n")

    assert tree.rule(f"rust_touched {base} {changed}") == 0


def test_a_change_to_the_rust_checks_needs_no_cargo_here(tree: Tree) -> None:
    """Only CI takes the wider answer. A commit or a push of the script
    passes with no `cargo` on PATH, as every change outside `rust/` does."""
    with (tree.root / GATE_FILE).open("a", encoding="utf-8") as file:
        file.write("# staged\n")
    tree.git("add", "-A")

    commit = tree.run(GATE, cargo=False)
    push = tree.run(GATE, "--tests-for", GATE_FILE, RULE, cargo=False)

    assert _passed(commit), commit.out + commit.err
    assert _passed(push), push.out + push.err
    assert _pytest_calls(push) == [PYTEST]


@pytest.mark.parametrize(
    ("path", "of_checks"),
    [
        ("bin/rust-gate.sh", True),
        ("bin/rust-coverage.sh", True),
        ("bin/lib/rustrule.sh", True),
        (".github/workflows/gate.yml", True),
        (".github/workflows/release.yml", True),
        (".github/actions/scope/action.yml", True),
        ("bin/quality-gate.sh", False),
        ("bin/lib/docsrule.sh", False),
        (".github/workflows/retest.yml", False),
        (".github/actions/verdict/action.yml", False),
        ("rust/Cargo.toml", False),
        ("docs/bin/rust-gate.sh", False),
    ],
)
def test_only_these_files_are_the_rust_checks(tree: Tree, path: str, of_checks: bool) -> None:
    assert (tree.rule(f"rust_gate_path '{path}'") == 0) == of_checks


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
