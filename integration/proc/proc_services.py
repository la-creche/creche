"""The one table of the process-level suite: how each service starts.

A test names a `Service`. It never names a program, a module or a language.
`command_of` turns the name into the words that start the process:

1. The default is the command the service's systemd unit runs, with the
   workspace venv in the place of the component tree.
2. One environment variable per service replaces the whole default. A later
   packet points it at another binary, and no test changes.

`test_proc_table.py` holds each default against the unit file, so the table
cannot drift from what the host starts.

Every variable of this suite starts with `CRECHE_PROC_`. `unknown_variables`
finds a name with that start that the suite does not read. A misspelled
override would leave the default in place, and the run would look like a
run that judged the other binary.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final


class Service(StrEnum):
    """Every program of this repository that the suite can start."""

    ATTENDANCE = "attendance"
    DOOR_OWUI = "door-owui"
    DOOR_TRIGGER = "door-trigger"
    DOOR_TUI = "door-tui"
    CAREGIVER = "caregiver"
    CHAPERONE = "chaperone"
    NOTICEBOARD = "noticeboard"
    NOTICEBOARD_VERIFY = "noticeboard-verify"


class Origin(StrEnum):
    """Where a start command came from, for the report header."""

    DEFAULT = "default"
    OVERRIDE = "override"


@dataclass(frozen=True, slots=True)
class StartEntry:
    """One row of the table."""

    #: The program the unit runs, as a name in the component's `bin`.
    program: str
    #: The words the unit puts after the program to select the service.
    selector: tuple[str, ...]
    #: The one environment variable that replaces `program` and `selector`.
    override: str
    #: The unit files under `systemd/` whose `ExecStart` is this command.
    #: Empty for a command that no unit runs: the operator runs it by hand,
    #: or the release executor runs it as a verify hook (contract 06 §4).
    units: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StartCommand:
    """The words that start one service, and where they came from."""

    words: tuple[str, ...]
    origin: Origin


SERVICES: Final[dict[Service, StartEntry]] = {
    Service.ATTENDANCE: StartEntry(
        program="python",
        selector=("-m", "attendance"),
        override="CRECHE_PROC_ATTENDANCE",
        units=("creche-attendance.service",),
    ),
    Service.DOOR_OWUI: StartEntry(
        program="python",
        selector=("-m", "agent_door_owui"),
        override="CRECHE_PROC_DOOR_OWUI",
        units=("creche-door-owui.service",),
    ),
    Service.DOOR_TRIGGER: StartEntry(
        program="agent-trigger",
        selector=(),
        override="CRECHE_PROC_DOOR_TRIGGER",
        units=("creche-trigger-webhooks.service", "creche-trigger@.service"),
    ),
    Service.DOOR_TUI: StartEntry(
        program="agent-tui",
        selector=(),
        override="CRECHE_PROC_DOOR_TUI",
        units=(),
    ),
    Service.CAREGIVER: StartEntry(
        program="caregiver",
        selector=(),
        override="CRECHE_PROC_CAREGIVER",
        units=("creche-caregiver.service",),
    ),
    Service.CHAPERONE: StartEntry(
        program="chaperone",
        selector=(),
        override="CRECHE_PROC_CHAPERONE",
        units=("creche-chaperone.service",),
    ),
    Service.NOTICEBOARD: StartEntry(
        program="noticeboard",
        selector=(),
        override="CRECHE_PROC_NOTICEBOARD",
        units=("creche-noticeboard.service",),
    ),
    Service.NOTICEBOARD_VERIFY: StartEntry(
        program="noticeboard-verify",
        selector=(),
        override="CRECHE_PROC_NOTICEBOARD_VERIFY",
        units=(),
    ),
}

#: The start of every variable that this suite reads.
VARIABLE_PREFIX: Final = "CRECHE_PROC_"

#: Set it to keep the root of every test on disk, to read after a run.
KEEP_ROOTS_ENV: Final = "CRECHE_PROC_KEEP"

#: Set it to make a skip a failure. A run in which every test skips is green,
#: and it judged nothing. A CI job sets it.
NO_SKIP_ENV: Final = "CRECHE_PROC_NO_SKIP"

#: The variables of the suite that replace no command.
SWITCHES: Final = frozenset({KEEP_ROOTS_ENV, NO_SKIP_ENV})

#: What only the default command needs. Python holds a child's stdout in a
#: buffer when it is a file, and the failure report reads that file while the
#: process runs. An override gets none of these: nothing in a test may depend
#: on the language of a service.
DEFAULT_ONLY_ENV: Final[dict[str, str]] = {"PYTHONUNBUFFERED": "1"}


class CommandError(Exception):
    """No usable start command. The message names the variable to fix."""


def venv_bin() -> Path:
    """The `bin` of the venv this suite runs in.

    It plays the component tree: a unit runs `<component>/bin/<program>`.
    Not resolved, because the venv's `python` is a link to the base
    interpreter and the base interpreter does not hold the workspace.
    """
    return Path(sys.executable).parent


def command_of(service: Service, environ: Mapping[str, str] | None = None) -> StartCommand:
    """The words that start `service`. The override wins when it is set.

    Fail closed. An override that is set and empty, that is no command
    line, or that names no program, is an error and never a fallback to the
    default: a run that believes it judged another binary and judged the
    default is worse than a run that stops.
    """
    source = os.environ if environ is None else environ
    entry = SERVICES[service]
    raw = source.get(entry.override)

    if raw is None:
        return _default_command(service, entry)

    try:
        words = shlex.split(raw)
    except ValueError as error:
        raise CommandError(f"{entry.override} is not a command line: {error}") from error

    if not words:
        raise CommandError(f"{entry.override} is set and empty. Unset it or name a program.")

    program = shutil.which(words[0], path=source.get("PATH"))

    if program is None:
        raise CommandError(f"{entry.override} names {words[0]}, which is not a program.")

    # A service starts in the root of its test. A relative path names another file there.
    return StartCommand(words=(os.path.abspath(program), *words[1:]), origin=Origin.OVERRIDE)


def env_of(command: StartCommand) -> dict[str, str]:
    """The variables a command gets beside the ones its test gives it."""
    if command.origin is Origin.OVERRIDE:
        return {}

    return dict(DEFAULT_ONLY_ENV)


def unknown_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """Each name with the suite's prefix that the suite does not read, sorted."""
    source = os.environ if environ is None else environ
    known = SWITCHES | {entry.override for entry in SERVICES.values()}

    return sorted(name for name in source if name.startswith(VARIABLE_PREFIX) and name not in known)


def describe_table(environ: Mapping[str, str] | None = None) -> list[str]:
    """One line per service, for the header and the summary of a test run."""
    lines: list[str] = []

    for service in Service:
        try:
            command = command_of(service, environ)
        except CommandError as error:
            lines.append(f"proc {service.value}: {error}")
            continue

        lines.append(f"proc {service.value}: {shlex.join(command.words)} ({command.origin.value})")

    return lines


def _default_command(service: Service, entry: StartEntry) -> StartCommand:
    program = venv_bin() / entry.program

    if not program.is_file():
        raise CommandError(
            f"{service.value} has no default command: {program} is missing. "
            f"Run `uv sync`, or set {entry.override}."
        )

    return StartCommand(words=(str(program), *entry.selector), origin=Origin.DEFAULT)
