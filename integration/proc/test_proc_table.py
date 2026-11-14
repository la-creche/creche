"""The service table, held against the unit files and against its own rules.

No test here starts a service. Each one checks the table that every other
test of this suite starts a service through.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml
from proc_board import verify_words
from proc_caregiver import serve_words
from proc_chaperone_pin import (
    PIN_FILE,
    BuildError,
    pinned_binary,
    pinned_commit,
    repository_url,
)
from proc_services import (
    DEFAULT_ONLY_ENV,
    SERVICES,
    SWITCHES,
    CommandError,
    Origin,
    Service,
    command_of,
    env_of,
    unknown_variables,
    venv_bin,
)
from proc_tree import Tree

#: `integration/proc/test_proc_table.py` is 3 deep in the checkout.
UNIT_DIR = Path(__file__).resolve().parents[2] / "systemd"

#: The manifest of the component whose verify hook has a row in the table.
HOOK_MANIFEST = UNIT_DIR.parent / "noticeboard" / "component.yaml"
EXEC_START = "ExecStart="
CONTINUES = "\\"
OVERRIDE_PREFIX = "CRECHE_PROC_"
FLAG = "--"

#: The three flags of `caregiver serve` that the suite adds to those of the unit.
NOT_IN_THE_UNIT = {"--litellm-base-url", "--poll-interval-s", "--chaperone-bin"}


def test_each_default_is_what_its_unit_runs() -> None:
    """A default is the program and the selector of the unit's `ExecStart`."""
    for service, entry in SERVICES.items():
        for unit in entry.units:
            words = shlex.split(_exec_start(unit))
            selector = tuple(words[1 : 1 + len(entry.selector)])

            assert Path(words[0]).name == entry.program, f"{service.value}: {unit}"
            assert selector == entry.selector, f"{service.value}: {unit}"


def test_the_arguments_of_caregiver_are_those_of_its_unit() -> None:
    """The suite starts `caregiver` with each flag of the unit, in the order of the unit.

    The row of `caregiver` has no selector, because a scenario also runs
    its other verbs. So the verb and the flags of the unit are held here.
    `proc_caregiver.py` gives the reason for each flag that the unit has not.
    """
    unit = shlex.split(_exec_start("creche-caregiver.service"))[1:]
    suite = serve_words(Tree(Path("/the-root-of-a-test")), 1, "http://chaperone.invalid")
    unit_flags = [word for word in unit if word.startswith(FLAG)]
    suite_flags = [word for word in suite if word.startswith(FLAG)]

    assert suite[0] == unit[0] == "serve"
    assert [flag for flag in suite_flags if flag in unit_flags] == unit_flags
    assert set(suite_flags) - set(unit_flags) == NOT_IN_THE_UNIT


def test_the_verify_hook_is_what_its_manifest_runs() -> None:
    """No unit runs the hook. The release executor runs `verify.command` of the manifest.

    The program of the row is the program of that command. The suite gives
    the hook the words of that command, with an env file of the test in the
    place of the env file of the host.
    """
    command = _verify_command(HOOK_MANIFEST)
    entry = SERVICES[Service.NOTICEBOARD_VERIFY]
    env_file = Path(command[-1])

    assert entry.units == ()
    assert Path(command[0]).name == entry.program
    assert entry.selector == ()
    assert verify_words(env_file) == command[1:]


def test_every_service_has_a_row_and_its_own_variable() -> None:
    overrides = [entry.override for entry in SERVICES.values()]

    assert set(SERVICES) == set(Service)
    assert len(set(overrides)) == len(overrides)
    assert all(name.startswith(OVERRIDE_PREFIX) for name in overrides)


def test_the_default_runs_from_this_venv() -> None:
    command = command_of(Service.ATTENDANCE, {})

    assert command.origin is Origin.DEFAULT
    assert command.words == (str(venv_bin() / "python"), "-m", "attendance")
    assert env_of(command) == DEFAULT_ONLY_ENV


def test_an_override_replaces_the_whole_default(tmp_path: Path) -> None:
    """The variable gives every word. No word of the default stays."""
    program = _program(tmp_path / "other-attendance")
    environ = {"CRECHE_PROC_ATTENDANCE": f"{program} --flag 'two words'"}

    command = command_of(Service.ATTENDANCE, environ)

    assert command.origin is Origin.OVERRIDE
    assert command.words == (str(program), "--flag", "two words")
    assert env_of(command) == {}


def test_an_override_reaches_one_service_only(tmp_path: Path) -> None:
    program = _program(tmp_path / "other-attendance")
    environ = {"CRECHE_PROC_ATTENDANCE": str(program)}

    assert command_of(Service.DOOR_TRIGGER, environ).origin is Origin.DEFAULT


def test_an_empty_override_is_an_error() -> None:
    """Fail closed. An empty value never means the default."""
    with pytest.raises(CommandError, match="CRECHE_PROC_CAREGIVER is set and empty"):
        command_of(Service.CAREGIVER, {"CRECHE_PROC_CAREGIVER": "  "})


def test_an_override_that_names_no_program_is_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "not-built-yet"

    with pytest.raises(CommandError, match="CRECHE_PROC_NOTICEBOARD names"):
        command_of(Service.NOTICEBOARD, {"CRECHE_PROC_NOTICEBOARD": str(missing)})


def test_an_override_with_an_open_quote_is_an_error() -> None:
    """The error names the variable, as each other error of an override does."""
    with pytest.raises(CommandError, match="CRECHE_PROC_ATTENDANCE is not a command line"):
        command_of(Service.ATTENDANCE, {"CRECHE_PROC_ATTENDANCE": "/bin/sh 'open"})


def test_a_relative_override_becomes_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service starts in the root of its test, where a relative path names nothing."""
    program = _program(tmp_path / "other-attendance")
    monkeypatch.chdir(tmp_path)

    command = command_of(Service.ATTENDANCE, {"CRECHE_PROC_ATTENDANCE": "./other-attendance -x"})

    assert Path(command.words[0]).is_absolute()
    assert Path(command.words[0]).samefile(program)
    assert command.words[1:] == ("-x",)


def test_a_misspelled_variable_is_found() -> None:
    """Fail closed. A name that no row has would leave the default in place."""
    environ = {
        "CRECHE_PROC_ATTENDENCE": "/path/to/the/binary",
        "CRECHE_PROC_DOOR_ACP_": "/path/to/the/binary",
        "CRECHE_PROC_CHAPERONE_RS": "/path/to/the/binary",
        "CRECHE_PROC_KEEP": "1",
        "PATH": "/usr/bin",
    }

    assert unknown_variables(environ) == ["CRECHE_PROC_ATTENDENCE", "CRECHE_PROC_DOOR_ACP_"]


def test_every_variable_of_the_table_is_known() -> None:
    environ = dict.fromkeys((entry.override for entry in SERVICES.values()), "/bin/sh")

    assert unknown_variables(environ) == []


def test_the_rust_chaperone_defaults_to_the_pinned_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default of `chaperone-rs` is the binary of the pinned commit in the cache.

    It is never a program of the venv: the venv holds the Python
    `chaperone`, and a run of that would judge another program.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    binary = pinned_binary()

    with pytest.raises(CommandError, match="CRECHE_PROC_CHAPERONE_RS"):
        command_of(Service.CHAPERONE_RS, {})

    binary.parent.mkdir(parents=True)
    _program(binary)
    command = command_of(Service.CHAPERONE_RS, {})

    assert command.origin is Origin.DEFAULT
    assert command.words == (str(binary),)
    assert binary.is_relative_to(tmp_path / ".cache" / "creche-proc" / "chaperone-rs")
    assert binary.parent.parent.name == pinned_commit()


def test_the_pin_file_holds_one_commit_and_a_comment() -> None:
    lines = PIN_FILE.read_text(encoding="utf-8").splitlines()

    assert [line for line in lines if line.startswith("#")] == lines[:1]
    assert lines[1:] == [pinned_commit()]


@pytest.mark.parametrize(
    "text", ["", "# only a comment\n", "abc\n", "A" * 40 + "\n", "a" * 40 + "\n" + "b" * 40 + "\n"]
)
def test_a_pin_with_no_one_commit_is_an_error(text: str) -> None:
    with pytest.raises(BuildError):
        pinned_commit(text)


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/example-owner/creche",
        "https://github.com/example-owner/creche.git",
        "git@github.com:example-owner/creche.git",
        "ssh://git@github.com/example-owner/creche.git",
    ],
)
def test_the_chaperone_repository_is_beside_the_origin(origin: str) -> None:
    """No file of this checkout names the owner. The origin remote gives it."""
    assert repository_url(origin) == "https://github.com/example-owner/chaperone"


def test_an_origin_off_github_is_an_error() -> None:
    with pytest.raises(BuildError, match="CRECHE_PROC_CHAPERONE_RS"):
        repository_url("https://git.example.invalid/example-owner/creche.git")


def test_every_switch_of_the_suite_is_known() -> None:
    assert frozenset({"CRECHE_PROC_KEEP", "CRECHE_PROC_NO_SKIP"}) == SWITCHES
    assert unknown_variables(dict.fromkeys(SWITCHES, "1")) == []


def _exec_start(unit: str) -> str:
    """The `ExecStart` value of one unit, with its continuation lines joined."""
    lines = (UNIT_DIR / unit).read_text(encoding="utf-8").split("\n")
    start = next(index for index, line in enumerate(lines) if line.startswith(EXEC_START))
    parts: list[str] = []

    for line in lines[start:]:
        parts.append(line.removeprefix(EXEC_START).removesuffix(CONTINUES).strip())

        if not line.endswith(CONTINUES):
            break

    return " ".join(parts)


def _verify_command(manifest: Path) -> tuple[str, ...]:
    """`verify.command` of one component manifest (contract 06 §4)."""
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))

    return tuple(str(word) for word in document["verify"]["command"])


def _program(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)

    return path
