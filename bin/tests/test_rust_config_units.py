"""The config types of the Rust port and the unit files say the same thing.

`rust/crates/creche-contracts/src/config/` holds one config type for each
daemon. Each type states what the daemon does when the parse of its config
fails, and its text says what the unit file does today. Three things could
change without one red line, and each gets a check here:

1. **A unit that a config type names, and that is not in `systemd/`.** The
   seven daemon units are the seven units that start again after an exit.
2. **A unit that stops the restart loop, with a doc that says it does not.**
   Today each daemon unit holds `Restart=always` and
   `StartLimitIntervalSec=0`, and none holds `RestartPreventExitStatus`.
   `rust/AGENTS.md` says so. The pull request that adds the line changes
   this pin and that text.
3. **A variable of a unit that no config type knows.** Each `Environment=`
   name with the prefix of its service, and each flag of the caregiver
   command, is in the Rust module of that daemon.
4. **A unit that checks its config before the main process starts, with a
   doc that does not say so.** systemd reads `RestartPreventExitStatus` only
   for the main process. A unit with an `ExecStartPre=... --check` line
   starts again after a check that fails, with that line too. Three units
   hold such a check today, and the Rust module of each one says what the
   port must do with it.

A Rust test reads no file outside `rust/` and `vectors/data`, so these
checks are here. A push that changes only `rust/` runs no pytest suite
(`bin/lib/rustrule.sh`), so for such a change they run in CI.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UNITS = REPO / "systemd"
CONFIG = REPO / "rust" / "crates" / "creche-contracts" / "src" / "config"

#: The Rust module of each daemon, and the prefix of the variables that the
#: daemon reads. The caregiver and the intake read no variable with a prefix
#: of their own.
MODULES = {
    "creche-attendance.service": ("attendance", "SESSIOND_"),
    "creche-caregiver.service": ("caregiver", None),
    "creche-chaperone.service": ("chaperone", "PEP_"),
    "creche-door-owui.service": ("door_owui", "DOOR_OWUI_"),
    "creche-trigger-webhooks.service": ("door_trigger", "DOOR_TRIGGER_"),
    "creche-noticeboard.service": ("noticeboard", "VIEW_"),
    "creche-handover-intake.service": ("intake", None),
}

#: `const UNIT: &'static str = "<unit>";` in a Rust module.
UNIT_CONST = re.compile(r'const UNIT: &\'static str = "([^"]+)";')

#: `Environment=NAME=value` in a unit file.
ENVIRONMENT = re.compile(r"^Environment=([A-Z][A-Z0-9_]*)=", re.MULTILINE)

#: A flag on the command line of a unit, for example `--state-root`.
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")

RESTART_ALWAYS = "Restart=always"
NO_START_LIMIT = "StartLimitIntervalSec=0"
PREVENT = "RestartPreventExitStatus"

#: A line that runs a process before the main process of a unit.
PRE_START = "ExecStartPre="

#: The flag of a service that parses its config and exits.
CHECK_FLAG = "--check"

#: The units that check their config before the main process starts.
#: `RestartPreventExitStatus` does not read the exit status of that check.
PRE_CHECKED = {
    "creche-attendance.service",
    "creche-door-owui.service",
    "creche-noticeboard.service",
}

#: What the doc comment of a config type says when its unit holds the check.
REMOVE_THE_CHECK = "removes the `ExecStartPre` line"


def _unit(name: str) -> str:
    return (UNITS / name).read_text(encoding="utf-8")


def _module(name: str) -> str:
    return (CONFIG / f"{name}.rs").read_text(encoding="utf-8")


def _lines(text: str) -> list[str]:
    """Each line of a unit file that is not a comment."""
    return [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]


def _restarting_units() -> set[str]:
    return {
        path.name for path in UNITS.glob("*.service") if RESTART_ALWAYS in _lines(_unit(path.name))
    }


def test_each_config_type_names_one_daemon_unit() -> None:
    named: dict[str, str] = {}
    for path in sorted(CONFIG.glob("*.rs")):
        for unit in UNIT_CONST.findall(path.read_text(encoding="utf-8")):
            assert unit not in named, f"{unit}: {named[unit]} and {path.stem}"
            named[unit] = path.stem

    assert named == {unit: module for unit, (module, _) in MODULES.items()}
    assert set(named) == _restarting_units()


def test_each_daemon_unit_starts_again_after_each_exit_status() -> None:
    for unit in MODULES:
        lines = _lines(_unit(unit))

        assert RESTART_ALWAYS in lines, unit
        assert NO_START_LIMIT in lines, unit
        assert not [line for line in lines if line.startswith(PREVENT)], unit


def test_a_unit_with_a_check_before_the_start_says_so_in_its_config_type() -> None:
    checked = {
        unit
        for unit in MODULES
        if any(
            line.startswith(PRE_START) and line.endswith(CHECK_FLAG) for line in _lines(_unit(unit))
        )
    }

    assert checked == PRE_CHECKED, (
        "the units with an ExecStartPre check changed: change PRE_CHECKED here, the FAILURE "
        "ACTION comment of each config type and the crash loop rule in rust/AGENTS.md"
    )
    for unit, (module, _) in MODULES.items():
        says_so = REMOVE_THE_CHECK in _module(module)

        assert says_so == (unit in PRE_CHECKED), (
            f"{unit}: the FAILURE ACTION comment in rust/.../config/{module}.rs must say "
            f"'{REMOVE_THE_CHECK}' exactly when the unit holds an ExecStartPre check"
        )


def test_the_exit_status_of_a_bad_config_is_ex_config() -> None:
    root = (CONFIG.parent / "config.rs").read_text(encoding="utf-8")

    assert f"pub const EX_CONFIG: u8 = {os.EX_CONFIG};" in root
    assert f'pub const NO_RESTART_LINE: &str = "{PREVENT}={os.EX_CONFIG}";' in root


def test_each_variable_of_a_unit_has_a_constant() -> None:
    for unit, (module, prefix) in MODULES.items():
        if prefix is None:
            continue

        source = _module(module)
        names = [name for name in ENVIRONMENT.findall(_unit(unit)) if name.startswith(prefix)]
        missing = [name for name in names if f'"{name}"' not in source]

        assert not missing, f"{unit}: rust/.../config/{module}.rs has no constant for {missing}"


def test_the_chaperone_unit_gives_its_variables_in_the_unit_file() -> None:
    """The check above reads no name for a unit with no `Environment=` line."""
    names = ENVIRONMENT.findall(_unit("creche-chaperone.service"))

    assert "PEP_REWORK_DIR" in names
    assert "PEP_AUDIT_DIR" in names


def test_each_flag_of_the_caregiver_command_has_a_field() -> None:
    lines = _lines(_unit("creche-caregiver.service"))
    start = next(index for index, line in enumerate(lines) if line.startswith("ExecStart="))
    command: list[str] = []
    for line in lines[start:]:
        command.append(line)
        if not line.endswith("\\"):
            break

    flags = FLAG.findall(" ".join(command))
    source = _module("caregiver")

    assert "--image" in flags and "--write" in flags
    assert not [flag for flag in flags if f"`{flag}`" not in source and f'"{flag}"' not in source]
