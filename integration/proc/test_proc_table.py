"""The service table, held against the unit files and against its own rules.

No test here starts a service. Each one checks the table that every other
test of this suite starts a service through.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from proc_services import (
    DEFAULT_ONLY_ENV,
    SERVICES,
    CommandError,
    Origin,
    Service,
    command_of,
    env_of,
    unknown_variables,
    venv_bin,
)

#: `integration/proc/test_proc_table.py` is 3 deep in the checkout.
UNIT_DIR = Path(__file__).resolve().parents[2] / "systemd"
EXEC_START = "ExecStart="
CONTINUES = "\\"
OVERRIDE_PREFIX = "CRECHE_PROC_"


def test_each_default_is_what_its_unit_runs() -> None:
    """A default is the program and the selector of the unit's `ExecStart`."""
    for service, entry in SERVICES.items():
        for unit in entry.units:
            words = shlex.split(_exec_start(unit))
            selector = tuple(words[1 : 1 + len(entry.selector)])

            assert Path(words[0]).name == entry.program, f"{service.value}: {unit}"
            assert selector == entry.selector, f"{service.value}: {unit}"


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


def _program(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)

    return path
