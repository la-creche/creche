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
from proc_library import Profile, index_words
from proc_services import (
    DEFAULT_ONLY_ENV,
    SERVICES,
    SWITCHES,
    CommandError,
    Origin,
    Service,
    command_of,
    env_of,
    reference_of,
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
NOT_IN_THE_UNIT = {"--litellm-base-url", "--release-root", "--poll-interval-s"}

#: The two units that run the index builder in a sandbox, and the profile
#: word that each one gives. None is a unit that gives no such word.
INDEX_UNITS = {"index@.service": None, "index-code@.service": Profile.CODE}

#: The words of an `ExecStart` that give a shell its command text.
SHELL = ("sh", "-c")
END_OF_COMMAND = ";"


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


def test_the_index_units_run_the_program_of_the_library_row() -> None:
    """The row of `library` names no unit, so its program is held here.

    Each index unit runs a shell in a sandbox. The first command of the
    shell text is the index builder with a corpus directory, an index
    directory and the profile word of the unit. The suite gives the program
    the same words: `index_words` of `proc_library.py`.
    """
    for unit, profile in INDEX_UNITS.items():
        words = shlex.split(_exec_start(unit))
        text_at = _index_after(words, SHELL)
        command = shlex.split(words[text_at].partition(END_OF_COMMAND)[0])
        suite = index_words(Path(command[1]), Path(command[2]), profile)

        assert command[0] == SERVICES[Service.LIBRARY].program, unit
        assert command[1:] == suite, unit


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

    assert command_of(Service.DOOR_OWUI, environ).origin is Origin.DEFAULT


def test_the_reference_is_the_default_with_the_variable_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reference of a row reads no variable: it stays the default command.

    The variable of the row names another program here. The judged command
    is that program, and the reference is the default command of the row,
    with what only the default command gets.
    """
    entry = SERVICES[Service.LIBRARY]
    monkeypatch.setenv(entry.override, str(_program(tmp_path / "other-library")))

    judged = command_of(Service.LIBRARY)
    reference = reference_of(Service.LIBRARY)

    assert judged.origin is Origin.OVERRIDE
    assert reference.origin is Origin.DEFAULT
    assert reference.words == (str(venv_bin() / entry.program), *entry.selector)
    assert reference == command_of(Service.LIBRARY, {})
    assert env_of(reference) == DEFAULT_ONLY_ENV


def test_an_empty_override_is_an_error() -> None:
    """Fail closed. An empty value never means the default."""
    with pytest.raises(CommandError, match="CRECHE_PROC_CHAPERONE is set and empty"):
        command_of(Service.CHAPERONE, {"CRECHE_PROC_CHAPERONE": "  "})


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
        "CRECHE_PROC_DOOR_OWUI_": "/path/to/the/binary",
        "CRECHE_PROC_CHAPERONE": "/path/to/the/binary",
        "CRECHE_PROC_KEEP": "1",
        "PATH": "/usr/bin",
    }

    assert unknown_variables(environ) == ["CRECHE_PROC_ATTENDENCE", "CRECHE_PROC_DOOR_OWUI_"]


def test_every_variable_of_the_table_is_known() -> None:
    environ = dict.fromkeys((entry.override for entry in SERVICES.values()), "/bin/sh")

    assert unknown_variables(environ) == []


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


def _index_after(words: list[str], run: tuple[str, ...]) -> int:
    """The index of the word that follows the first place of `run` in `words`."""
    starts = range(len(words) - len(run))

    return next(at for at in starts if tuple(words[at : at + len(run)]) == run) + len(run)


def _program(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)

    return path
