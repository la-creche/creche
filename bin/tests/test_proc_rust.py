"""What `bin/proc-rust.sh` builds, what it runs, and what its files select.

The process-level suite is the judge of a port (`integration/proc/AGENTS.md`).
`bin/proc-rust.sh` gives the suite one Rust program for each file
`rust/proc/*.run`, through the variable of one service. Five things could go
wrong without one red line, and each gets a check here:

1. **A run that judged the Python service.** The suite starts the default
   command of a service whose variable is not set. A file that names a
   switch of the suite, or a name that the service table does not hold,
   would start no Rust program. Each file must name the variable of a row
   of the table.
2. **A run that judged an old program.** The script removes the program of
   an earlier build before the build. A build that wrote no program stops
   the run.
3. **A run that judged nothing.** A directory with no file fails. A test
   that skips is a failure: the script sets the switch of the suite.
4. **A selection that rots.** A file selects each test by its name. A test
   that the suite no longer has fails here, before the job runs.
5. **A scenario that a file leaves out in silence.** `probe.run` selects
   each scenario of `test_proc_board_start.py` but the one that `NOT_YET`
   names, with its reason.

The tests of the script run the real script in a throwaway tree. `cargo` and
`uv` are fakes that write their argv, their directory and their variables to
a file. PATH holds only those fakes and links to the few tools that the
script calls.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

#: The file under test, as git names it.
SCRIPT = "bin/proc-rust.sh"

#: The directory of the files, and the end of the name of a file.
RUN_DIR = "rust/proc"
RUN_SUFFIX = ".run"

#: The flag that prints the lines of each file and starts nothing.
DRY_RUN = "--dry-run"

#: Each program that the script calls, but `cargo` and `uv`. Then each
#: program that only the two fakes call.
TOOLS = ("bash", "dirname", "rm", "env", "mkdir", "chmod", "grep", "sort", "tr")

#: The cargo step of one file, word for word, before the name of the package.
BUILD = "build --release --locked -p"

#: What the script gives `uv` after the selected tests, and before them.
PYTEST = "run pytest"
ONLY_SLOW = "-m slow"

#: The switch of the suite that makes a skip a failure, and its value.
NO_SKIP = "CRECHE_PROC_NO_SKIP"
NO_SKIP_ON = "1"

#: The start of each variable of the suite.
PREFIX = "CRECHE_PROC_"

#: Where a release build of the workspace writes a program.
BUILD_DIR = "rust/target/release"

#: The module of the suite that holds the service table.
SERVICES_MODULE = REPO / "integration" / "proc" / "proc_services.py"

#: The directory of the crates of the workspace.
CRATES = REPO / "rust" / "crates"

#: The file of the probe, and the test file whose scenarios it selects.
PROBE = "probe"
BOARD_START = "integration/proc/test_proc_board_start.py"

#: The scenarios of `BOARD_START` that `probe.run` does not select yet, each
#: with its reason. The Python noticeboard ends a refused start with status
#: 2, and the scenario holds that status. The probe ends a refused start with
#: status 78, as each Rust daemon does (`rust/AGENTS.md`, "The rules for a
#: service", rule 17). "Known gaps" of `integration/proc/AGENTS.md` has the
#: entry. The pull request that makes the scenario hold status 78 empties
#: this table and adds the scenario to `probe.run`.
NOT_YET = {
    "test_a_lan_bind_with_no_full_key_refuses_to_start": "the scenario holds exit status 2",
}

#: A file that the script takes, and the name of its program.
GOOD = (
    "# one program\n"
    "package one-crate\n"
    "program one-program\n"
    "variable CRECHE_PROC_NOTICEBOARD\n"
    "select integration/proc/test_one.py::test_first\n"
    "select integration/proc/test_one.py::test_second[case-1]\n"
    "select integration/proc/test_two.py\n"
)
GOOD_PROGRAM = "one-program"
GOOD_SELECTION = (
    "integration/proc/test_one.py::test_first "
    "integration/proc/test_one.py::test_second[case-1] "
    "integration/proc/test_two.py"
)

#: A second file, for another service.
OTHER = (
    "package two-crate\n"
    "program two-program\n"
    "variable CRECHE_PROC_LIBRARY\n"
    "select integration/proc/test_three.py\n"
)

#: Writes where it ran and its argv. Its last word is the package. It fails
#: for the package that CARGO_FAIL names. For each other package it writes
#: the program `<CARGO_WRITES>-of-<package>` with the mode change CARGO_MODE,
#: unless CARGO_WRITES is empty.
FAKE_CARGO = """#!/usr/bin/env bash
printf '%s\\t%s\\n' "$PWD" "$*" >> "$CARGO_LOG"
if [[ "${!#}" == "${CARGO_FAIL:-}" ]]; then
  exit 1
fi
if [[ -n "${CARGO_WRITES:-}" ]]; then
  mkdir -p target/release
  printf '#!/usr/bin/env bash\\n' > "target/release/$CARGO_WRITES-of-${!#}"
  chmod "$CARGO_MODE" "target/release/$CARGO_WRITES-of-${!#}"
fi
"""

#: Writes where it ran, its argv and each variable of the suite that it got.
#: Fails when its argv holds the word that UV_FAIL names.
FAKE_UV = """#!/usr/bin/env bash
printf '%s\\t%s\\n' "$PWD" "$*" >> "$UV_LOG"
env | grep '^CRECHE_PROC_' | LC_ALL=C sort | tr '\\n' ' ' >> "$UV_ENV_LOG"
printf '\\n' >> "$UV_ENV_LOG"
if [[ -n "${UV_FAIL:-}" && " $* " == *" $UV_FAIL "* ]]; then
  exit 1
fi
"""


@dataclass(frozen=True)
class Run:
    """One run of the script: its exit code, its output and what it called."""

    code: int
    out: str
    err: str
    cargo: list[str]
    cargo_dirs: set[Path]
    uv: list[str]
    uv_dirs: set[Path]
    uv_env: list[dict[str, str]]


@dataclass(frozen=True)
class Tree:
    """The throwaway tree, and the PATH of a run."""

    root: Path
    path: str
    without_cargo: str
    without_uv: str

    def write(self, name: str, body: str) -> None:
        path = self.root / RUN_DIR / f"{name}{RUN_SUFFIX}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode("utf-8"))

    def built(self, program: str) -> Path:
        return self.root / BUILD_DIR / program

    def run(
        self,
        *args: str,
        path: str | None = None,
        writes: str = "built",
        mode: str = "+x",
        cargo_fail: str = "",
        uv_fail: str = "",
        env: dict[str, str] | None = None,
    ) -> Run:
        """Runs the copied script. The fake `cargo` writes the program
        `<writes>-of-<package>` and gives it the mode change `mode`.
        `cargo_fail` names a package whose build fails, and `uv_fail` names a
        word of a pytest run that fails."""
        logs = self.root.parent / "logs"
        shutil.rmtree(logs, ignore_errors=True)
        logs.mkdir()
        done = subprocess.run(
            [str(self.root / SCRIPT), *args],
            env={
                "PATH": self.path if path is None else path,
                "CARGO_LOG": str(logs / "cargo"),
                "CARGO_WRITES": writes,
                "CARGO_MODE": mode,
                "CARGO_FAIL": cargo_fail,
                "UV_LOG": str(logs / "uv"),
                "UV_ENV_LOG": str(logs / "uv-env"),
                "UV_FAIL": uv_fail,
            }
            | (env or {}),
            cwd=self.root.parent,
            capture_output=True,
            text=True,
            timeout=60,
        )
        cargo = [line.split("\t") for line in _lines(logs / "cargo")]
        uv = [line.split("\t") for line in _lines(logs / "uv")]

        return Run(
            code=done.returncode,
            out=done.stdout,
            err=done.stderr,
            cargo=[argv for _where, argv in cargo],
            cargo_dirs={Path(where).resolve() for where, _argv in cargo},
            uv=[argv for _where, argv in uv],
            uv_dirs={Path(where).resolve() for where, _argv in uv},
            uv_env=[
                dict(pair.split("=", 1) for pair in line.split())
                for line in _lines(logs / "uv-env")
            ],
        )


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    return path.read_text(encoding="utf-8").splitlines()


def _fake(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """A tree with the real script and no file under `rust/proc`. The fake
    `cargo` of this tree names a program after its package:
    `built-of-<package>`. So the files of a test name such a program."""
    root = tmp_path / "repo"
    tools = tmp_path / "tools"
    rusty = tmp_path / "rusty"
    synced = tmp_path / "synced"
    for one in (root / "bin", root / "rust", tools, rusty, synced):
        one.mkdir(parents=True)

    for name in TOOLS:
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH"
        (tools / name).symlink_to(found)

    _fake(rusty / "cargo", FAKE_CARGO)
    _fake(synced / "uv", FAKE_UV)
    shutil.copy2(REPO / SCRIPT, root / SCRIPT)

    return Tree(
        root=root,
        path=f"{rusty}:{synced}:{tools}",
        without_cargo=f"{synced}:{tools}",
        without_uv=f"{rusty}:{tools}",
    )


def _file(package: str, variable: str = "CRECHE_PROC_NOTICEBOARD", test: str = "test_one") -> str:
    """A file for the program that the fake `cargo` writes for `package`."""
    return (
        f"package {package}\n"
        f"program built-of-{package}\n"
        f"variable {variable}\n"
        f"select integration/proc/{test}.py\n"
    )


# --- what a run builds and what it runs --------------------------------------


def test_a_run_builds_the_package_inside_rust_and_then_runs_the_selection(tree: Tree) -> None:
    tree.write("one", _file("one-crate"))

    done = tree.run()

    assert done.code == 0, done.out + done.err
    assert done.cargo == [f"{BUILD} one-crate"]
    assert done.cargo_dirs == {(tree.root / "rust").resolve()}
    assert done.uv == [f"{PYTEST} integration/proc/test_one.py {ONLY_SLOW}"]
    assert done.uv_dirs == {tree.root.resolve()}
    assert "proc-rust: PASS (1 of 1 files)" in done.out


def test_the_suite_gets_the_built_program_in_the_variable_of_the_file(tree: Tree) -> None:
    """The path is absolute: the suite starts a service in the directory of
    its test."""
    tree.write("one", _file("one-crate"))

    done = tree.run()

    (env,) = done.uv_env
    program = Path(env["CRECHE_PROC_NOTICEBOARD"])
    assert program.is_absolute()
    assert program.resolve() == tree.built("built-of-one-crate").resolve()
    assert os.access(program, os.X_OK)


def test_a_skip_is_a_failure_and_no_other_variable_of_the_suite_is_set(tree: Tree) -> None:
    """The suite stops on a variable with its prefix that it does not read.
    The script sets the variable of the file and the one switch."""
    tree.write("one", _file("one-crate"))

    done = tree.run()

    (env,) = done.uv_env
    assert env[NO_SKIP] == NO_SKIP_ON
    assert set(env) == {"CRECHE_PROC_NOTICEBOARD", NO_SKIP}
    assert f'"{NO_SKIP}"' in SERVICES_MODULE.read_text(encoding="utf-8")


def test_a_run_gives_each_selected_test_to_one_pytest_run(tree: Tree) -> None:
    body = GOOD.replace("one-crate", "one").replace(GOOD_PROGRAM, "built-of-one")
    tree.write("one", body)

    done = tree.run()

    assert done.code == 0, done.out + done.err
    assert done.uv == [f"{PYTEST} {GOOD_SELECTION} {ONLY_SLOW}"]


def test_each_file_has_a_build_and_a_run_of_its_own_in_the_order_of_the_names(tree: Tree) -> None:
    tree.write("b-second", _file("two", "CRECHE_PROC_LIBRARY", "test_two"))
    tree.write("a-first", _file("one"))

    done = tree.run()

    assert done.code == 0, done.out + done.err
    assert done.cargo == [f"{BUILD} one", f"{BUILD} two"]
    assert done.uv == [
        f"{PYTEST} integration/proc/test_one.py {ONLY_SLOW}",
        f"{PYTEST} integration/proc/test_two.py {ONLY_SLOW}",
    ]
    assert [set(env) for env in done.uv_env] == [
        {"CRECHE_PROC_NOTICEBOARD", NO_SKIP},
        {"CRECHE_PROC_LIBRARY", NO_SKIP},
    ]
    assert "proc-rust: PASS (2 of 2 files)" in done.out


def test_a_variable_of_the_caller_reaches_the_suite(tree: Tree) -> None:
    """A person keeps the root of each test with `CRECHE_PROC_KEEP`. The
    variable of the file wins over the same name of the caller."""
    tree.write("one", _file("one-crate"))

    done = tree.run(env={"CRECHE_PROC_KEEP": "1", "CRECHE_PROC_NOTICEBOARD": "/another/program"})

    (env,) = done.uv_env
    assert env["CRECHE_PROC_KEEP"] == "1"
    assert Path(env["CRECHE_PROC_NOTICEBOARD"]).resolve() == (
        tree.built("built-of-one-crate").resolve()
    )


# --- a run that must not pass ------------------------------------------------


def test_a_tree_with_no_file_fails(tree: Tree) -> None:
    """A run with no file judged nothing."""
    (tree.root / RUN_DIR).mkdir(parents=True)

    for args in ([], [DRY_RUN]):
        done = tree.run(*args)

        assert done.code == 1
        assert "holds no file" in done.err
        assert done.cargo == []
        assert done.uv == []


def test_a_tree_with_no_directory_of_files_fails(tree: Tree) -> None:
    done = tree.run()

    assert done.code == 1
    assert "holds no file" in done.err


def test_a_file_with_another_suffix_is_no_file_of_the_script(tree: Tree) -> None:
    tree.write("one", _file("one-crate"))
    (tree.root / RUN_DIR / "notes.txt").write_text("package no\n", encoding="utf-8")

    done = tree.run()

    assert done.code == 0, done.out + done.err
    assert done.cargo == [f"{BUILD} one-crate"]


def test_a_failed_build_runs_no_test(tree: Tree) -> None:
    tree.write("one", _file("one-crate"))

    done = tree.run(cargo_fail="one-crate")

    assert done.code != 0
    assert done.uv == []
    assert "PASS" not in done.out


def test_a_build_that_wrote_no_program_runs_no_test(tree: Tree) -> None:
    tree.write("one", _file("one-crate"))

    done = tree.run(writes="")

    assert done.code == 1
    assert "wrote no program" in done.err
    assert done.uv == []


def test_the_program_of_an_earlier_build_is_never_judged(tree: Tree) -> None:
    """The file is there before the run, and the build of this run writes no
    program. The script must not give the old file to the suite."""
    tree.write("one", _file("one-crate"))
    old = tree.built("built-of-one-crate")
    old.parent.mkdir(parents=True)
    _fake(old, "#!/usr/bin/env bash\n")

    done = tree.run(writes="")

    assert done.code == 1
    assert "wrote no program" in done.err
    assert done.uv == []
    assert not old.exists()


def test_a_file_that_the_system_cannot_start_is_no_program(tree: Tree) -> None:
    tree.write("one", _file("one-crate"))

    done = tree.run(mode="-x")

    assert done.code == 1
    assert "wrote no program" in done.err
    assert done.uv == []


def test_a_failed_test_run_fails_the_script_and_stops_it(tree: Tree) -> None:
    tree.write("a-first", _file("one"))
    tree.write("b-second", _file("two", "CRECHE_PROC_LIBRARY", "test_two"))

    done = tree.run(uv_fail="integration/proc/test_one.py")

    assert done.code != 0
    assert done.cargo == [f"{BUILD} one"]
    assert len(done.uv) == 1
    assert "PASS" not in done.out


def test_a_run_without_cargo_fails_with_one_line(tree: Tree) -> None:
    tree.write("one", _file("one-crate"))

    done = tree.run(path=tree.without_cargo)

    assert done.code == 1
    assert done.err.splitlines() == [
        "proc-rust: cargo not on PATH: the build of a program needs the Rust toolchain"
    ]
    assert done.uv == []


def test_a_run_without_uv_fails_with_one_line_and_builds_nothing(tree: Tree) -> None:
    tree.write("one", _file("one-crate"))

    done = tree.run(path=tree.without_uv)

    assert done.code == 1
    assert done.err.splitlines() == [
        "proc-rust: uv not on PATH: the suite runs in the venv of the workspace"
    ]
    assert done.cargo == []


@pytest.mark.parametrize("args", [["--tests"], ["rust/proc/one.run"], [DRY_RUN, DRY_RUN]])
def test_the_script_refuses_each_other_argument(tree: Tree, args: list[str]) -> None:
    tree.write("one", _file("one-crate"))

    done = tree.run(*args)

    assert done.code == 2
    assert done.err.splitlines() == [f"usage: {SCRIPT} [{DRY_RUN}]"]
    assert done.cargo == []
    assert done.uv == []


# --- a file that breaks a rule -----------------------------------------------

#: (the name of the case, the text of the file, a part of the one line)
REFUSED_FILES = [
    ("no package", GOOD.replace("package one-crate\n", ""), "no line `package`"),
    ("no program", GOOD.replace("program one-program\n", ""), "no line `program`"),
    (
        "no variable",
        GOOD.replace("variable CRECHE_PROC_NOTICEBOARD\n", ""),
        "no line `variable`",
    ),
    ("no test", "package a\nprogram b\nvariable CRECHE_PROC_LIBRARY\n", "no line `select`"),
    ("only a comment", "# nothing\n", "no line `package`"),
    ("no byte", "", "no line `package`"),
    ("a package two times", GOOD + "package other\n", "the key `package` is there two times"),
    ("a program two times", GOOD + "program other\n", "the key `program` is there two times"),
    (
        "a variable two times",
        GOOD + "variable CRECHE_PROC_LIBRARY\n",
        "the key `variable` is there two times",
    ),
    (
        "a test two times",
        GOOD + "select integration/proc/test_two.py\n",
        "line 8: the test is there two times",
    ),
    ("another key", GOOD + "needs playpen\n", "line 8: the file has no key `needs`"),
    (
        "an empty line",
        GOOD.replace("program one-program", "\nprogram one-program"),
        "line 3: the line is not a comment",
    ),
    ("a key with no value", GOOD + "select\n", "line 8: the line is not a comment"),
    ("a key and a space", GOOD + "select \n", "line 8: the line is not a comment"),
    ("a space before the key", " " + GOOD, "line 1: the line is not a comment"),
    ("a comment after a space", GOOD + " # later\n", "line 8: the line is not a comment"),
    (
        "two spaces",
        GOOD.replace("package one-crate", "package  one-crate"),
        "line 2: the value of `package` has not the form",
    ),
    (
        "a space at the end",
        GOOD.replace("program one-program", "program one-program "),
        "line 3: the value of `program` has not the form",
    ),
    (
        "a tab",
        GOOD.replace("package one-crate", "package\tone-crate"),
        "line 2: the line is not a comment",
    ),
    (
        "a line that ends with CR LF",
        GOOD.replace("\n", "\r\n"),
        "line 2: the value of `package` has not the form",
    ),
    (
        "a package with a path",
        GOOD.replace("package one-crate", "package ../one-crate"),
        "line 2: the value of `package` has not the form",
    ),
    (
        "a package that is a flag",
        GOOD.replace("package one-crate", "package --workspace"),
        "line 2: the value of `package` has not the form",
    ),
    (
        "a program with a path",
        GOOD.replace("program one-program", "program ../../bin/sh"),
        "line 3: the value of `program` has not the form",
    ),
    (
        "a variable with another start",
        GOOD.replace("CRECHE_PROC_NOTICEBOARD", "PATH"),
        "line 4: the value of `variable` has not the form",
    ),
    (
        "a variable in lower case",
        GOOD.replace("CRECHE_PROC_NOTICEBOARD", "CRECHE_PROC_noticeboard"),
        "line 4: the value of `variable` has not the form",
    ),
    (
        "a variable with a value",
        GOOD.replace("CRECHE_PROC_NOTICEBOARD", "CRECHE_PROC_NOTICEBOARD=x"),
        "line 4: the value of `variable` has not the form",
    ),
    (
        "the switch of the suite",
        GOOD.replace("CRECHE_PROC_NOTICEBOARD", NO_SKIP),
        "is a switch of the suite and starts no service",
    ),
    (
        "a test of another suite",
        GOOD + "select integration/tests/test_one.py\n",
        "line 8: the value of `select` is not a test below integration/proc",
    ),
    (
        "a test above the suite",
        GOOD + "select integration/proc/../tests/test_one.py\n",
        "line 8: the value of `select` is not a test below integration/proc",
    ),
    (
        "a directory",
        GOOD + "select integration/proc/\n",
        "line 8: the value of `select` is not a test below integration/proc",
    ),
    (
        "the whole suite",
        GOOD + "select integration/proc\n",
        "line 8: the value of `select` is not a test below integration/proc",
    ),
    (
        "a pytest flag",
        GOOD + "select -k\n",
        "line 8: the value of `select` is not a test below integration/proc",
    ),
    (
        "a test with a flag after it",
        GOOD + "select integration/proc/test_one.py --deselect\n",
        "line 8: the value of `select` is not a test below integration/proc",
    ),
]


@pytest.mark.parametrize(
    ("body", "line"), [case[1:] for case in REFUSED_FILES], ids=[case[0] for case in REFUSED_FILES]
)
@pytest.mark.parametrize("args", [[], [DRY_RUN]], ids=["run", "dry-run"])
def test_a_file_that_breaks_a_rule_stops_the_run_before_the_first_build(
    tree: Tree, body: str, line: str, args: list[str]
) -> None:
    """The file with the fault has the last name. The file before it is
    sound, and the script builds nothing for it either."""
    tree.write("a-sound", _file("one"))
    tree.write("b-broken", body)

    done = tree.run(*args)

    assert done.code == 1, done.out + done.err
    (said,) = done.err.splitlines()
    assert said.startswith(f"proc-rust: {RUN_DIR}/b-broken{RUN_SUFFIX}: "), said
    assert line in said
    assert done.out == ""
    assert done.cargo == []
    assert done.uv == []


@pytest.mark.parametrize("name", ["Upper", "two words", "-flag", "a.b", "_a"])
def test_a_file_with_a_name_of_another_form_is_refused(tree: Tree, name: str) -> None:
    tree.write(name, _file("one-crate"))

    done = tree.run()

    assert done.code == 1
    assert "the name of the file has not the form of a program name" in done.err
    assert done.cargo == []


def test_a_last_line_with_no_line_end_is_a_line(tree: Tree) -> None:
    tree.write("one", _file("one-crate").rstrip("\n"))

    done = tree.run()

    assert done.code == 0, done.out + done.err
    assert done.uv == [f"{PYTEST} integration/proc/test_one.py {ONLY_SLOW}"]


# --- the dry run -------------------------------------------------------------


def _facts(text: str) -> dict[str, dict[str, list[str]]]:
    """The lines of a dry run: for each file, each value of each key."""
    found: dict[str, dict[str, list[str]]] = {}
    for line in text.splitlines():
        name, key, value = line.split("\t")
        found.setdefault(name, {}).setdefault(key, []).append(value)

    return found


def test_the_dry_run_prints_each_line_of_each_file_and_starts_nothing(tree: Tree) -> None:
    tree.write("one", GOOD)
    tree.write("two", OTHER)

    done = tree.run(DRY_RUN, path=tree.without_cargo)

    assert done.code == 0, done.out + done.err
    assert _facts(done.out) == {
        "one": {
            "package": ["one-crate"],
            "program": [GOOD_PROGRAM],
            "variable": ["CRECHE_PROC_NOTICEBOARD"],
            "select": GOOD_SELECTION.split(),
        },
        "two": {
            "package": ["two-crate"],
            "program": ["two-program"],
            "variable": ["CRECHE_PROC_LIBRARY"],
            "select": ["integration/proc/test_three.py"],
        },
    }
    assert done.cargo == []
    assert done.uv == []


# --- the files of this repository --------------------------------------------


def _services() -> Any:
    """The module of the suite that holds the service table."""
    spec = importlib.util.spec_from_file_location("proc_services_of_the_suite", SERVICES_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass of the module looks its own module up by this name.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]

    return module


@pytest.fixture(scope="module")
def real() -> dict[str, dict[str, list[str]]]:
    """What the script reads in the files of this repository. The script is
    the one reader of the format, and a dry run needs no `cargo`."""
    done = subprocess.run(
        [str(REPO / SCRIPT), DRY_RUN],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    return _facts(done.stdout)


def _tests_of(path: Path) -> set[str]:
    """The name of each test function at the top of one test file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def test_the_dry_run_reads_each_file_of_the_repository(
    real: dict[str, dict[str, list[str]]],
) -> None:
    on_disk = (REPO / RUN_DIR).glob(f"*{RUN_SUFFIX}")

    assert PROBE in real
    assert sorted(real) == sorted(path.name.removesuffix(RUN_SUFFIX) for path in on_disk)


def test_each_file_names_the_variable_of_a_row_of_the_service_table(
    real: dict[str, dict[str, list[str]]],
) -> None:
    """A name with the prefix that is no variable of a service starts no
    program: the suite then judges the default command of each service."""
    services = _services()
    overrides = {entry.override for entry in services.SERVICES.values()}

    for name, facts in real.items():
        (variable,) = facts["variable"]

        assert variable in overrides, name
        assert variable not in services.SWITCHES, name


def _programs_of(crate: Path) -> tuple[str, set[str]]:
    """The name of the package of one crate, and each program that cargo
    builds for it: each `[[bin]]` table, each file and each directory of
    `src/bin`, and `src/main.rs` under the name of the package."""
    manifest = tomllib.loads((crate / "Cargo.toml").read_text(encoding="utf-8"))
    package = manifest["package"]["name"]
    programs = {one["name"] for one in manifest.get("bin", [])}
    programs |= {path.stem for path in (crate / "src" / "bin").glob("*.rs")}
    programs |= {path.parent.name for path in (crate / "src" / "bin").glob("*/main.rs")}
    if (crate / "src" / "main.rs").is_file():
        programs.add(package)

    return package, programs


def test_each_file_names_a_program_of_a_crate_of_the_workspace(
    real: dict[str, dict[str, list[str]]],
) -> None:
    programs = dict(_programs_of(path.parent) for path in CRATES.glob("*/Cargo.toml"))

    for name, facts in real.items():
        (package,) = facts["package"]
        (program,) = facts["program"]

        assert package in programs, f"{name} names the package {package}"
        assert program in programs[package], f"{name} names the program {program}"


def test_each_file_selects_tests_that_the_suite_has(
    real: dict[str, dict[str, list[str]]],
) -> None:
    """pytest stops for a test that it does not find. This test says so
    before the job builds a program."""
    for name, facts in real.items():
        for selected in facts["select"]:
            path, _, rest = selected.partition("::")

            assert (REPO / path).is_file(), f"{name} selects {path}"
            if rest:
                assert rest.split("[", 1)[0] in _tests_of(REPO / path), f"{name}: {selected}"


def test_the_probe_stands_in_for_the_noticeboard(
    real: dict[str, dict[str, list[str]]],
) -> None:
    services = _services()

    assert real[PROBE]["package"] == ["creche-testkit"]
    assert real[PROBE]["program"] == ["creche-probe"]
    assert real[PROBE]["variable"] == [services.SERVICES[services.Service.NOTICEBOARD].override]


def test_the_probe_selects_each_scenario_of_the_board_start_but_the_named_ones(
    real: dict[str, dict[str, list[str]]],
) -> None:
    """A new scenario of the test file must go to `probe.run`, or to
    `NOT_YET` with its reason. No scenario leaves the selection in silence."""
    selected = real[PROBE]["select"]
    prefix = f"{BOARD_START}::"
    scenarios = _tests_of(REPO / BOARD_START)

    assert all(one.startswith(prefix) for one in selected)
    assert set(NOT_YET) <= scenarios, "NOT_YET names a scenario that the file no longer has"
    assert {one.removeprefix(prefix) for one in selected} == scenarios - set(NOT_YET)


def test_the_two_names_of_the_suite_in_the_script_are_names_of_the_suite() -> None:
    """The script holds a copy of the prefix and of the name of one switch.
    The service module of the suite is the source of both."""
    services = _services()

    assert services.VARIABLE_PREFIX == PREFIX
    assert services.NO_SKIP_ENV == NO_SKIP
    assert NO_SKIP in services.SWITCHES


def test_the_text_of_the_script_holds_the_prefix_of_the_suite_one_time() -> None:
    """The suite stops the run on a variable with its prefix that it does
    not read (`integration/proc/AGENTS.md`). The code of the script holds
    the prefix in one constant, and builds one name from it: the switch
    that makes a skip a failure."""
    text = (REPO / SCRIPT).read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    assert [line for line in code if PREFIX in line] == [f'PROC_PREFIX="{PREFIX}"']
    assert [line for line in code if "PROC_PREFIX}" in line and "=" in line.split("$")[0]] == [
        'PROC_NO_SKIP="${PROC_PREFIX}NO_SKIP"',
        'VARIABLE_FORM="^${PROC_PREFIX}[A-Z][A-Z0-9_]*\\$"',
    ]
