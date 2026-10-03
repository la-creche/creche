"""An autonomous family's `triggers:` as systemd USER timers (contract 01
§3.13).

    triggers: [{cron: "0 9 * * 1-5"}]
      -> ~/.config/systemd/user/creche-trigger-<family>-t1.timer
         OnCalendar=Mon..Fri *-*-* 09:00:00
         Unit=creche-trigger@<family>.service

One stored template unit runs the command; this module writes only the
timers. A changed schedule rewrites its unit. A removed trigger removes
it, and a removal runs before any write, so an interruption between the
two leaves FEWER firings than the target, never more.

`agent-trigger fire <family>` is `door-trigger`'s CLI. This module writes
the unit text that names it and never runs it.

A webhook trigger gets no timer: a door fires it."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from agent_family import FamilyFile, Kind

log = logging.getLogger("caregiver.timers")

#: One template service per fleet, instanced per family. Stored in
#: `systemd/`, installed by a bootstrap script, never generated here.
TRIGGER_SERVICE: Final = "creche-trigger@{family}.service"

#: `creche-trigger-<family>-t<N>.timer`, N counting the CRON triggers in
#: file order from 1. Position, not content: a changed schedule REWRITES
#: the unit, which a content hash could not do.
TIMER_NAME: Final = "creche-trigger-{family}-t{position}.timer"

#: Every unit this module creates.
TIMER_PREFIX: Final = "creche-trigger-"

#: The prefix this module wrote before the rename. A pass removes a timer
#: under it like any timer the file no longer wants, so the first pass
#: after the upgrade swaps each old timer for its new name, removal first.
OLD_TIMER_PREFIX: Final = "agent-trigger-"

#: Every unit this module may destroy. Anything else under
#: `~/.config/systemd/user` belongs to somebody else.
OWNED_PREFIXES: Final = (TIMER_PREFIX, OLD_TIMER_PREFIX)

SYSTEMCTL_TIMEOUT_S: Final = 30

#: Contract 01 §3.13: five fields, or one of these three.
CRON_FIELDS: Final = 5
MACRO_CALENDAR: Final = {"@hourly": "hourly", "@daily": "daily", "@weekly": "weekly"}

#: systemd's week starts on Monday, so cron's 0 (Sunday) is also 7.
CRON_DOW: Final = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
    "7": "Sun",
}
SUNDAY_LAST: Final = 7

MINUTE_MAX: Final = 59
HOUR_MAX: Final = 23
DAY_MIN: Final = 1
DAY_MAX: Final = 31
MONTH_MIN: Final = 1
MONTH_MAX: Final = 12


class CronShapeError(ValueError):
    """A cron expression contract 01 accepts and systemd cannot spell.

    cron ORs day-of-month with day-of-week. `OnCalendar` ANDs them. There
    is no `OnCalendar` that means the cron thing, so the trigger gets no
    unit rather than a unit that fires on the wrong days."""


class UnitWriter(Protocol):
    """Where a generated timer lands. `UserUnits` is the real one; a test
    uses `FakeUnits` and touches no `systemctl`."""

    def read(self, name: str) -> str | None: ...
    def write(self, name: str, text: str) -> None: ...
    def remove(self, name: str) -> None: ...
    def names(self) -> tuple[str, ...]: ...
    def is_enabled(self, name: str) -> bool: ...
    def enable(self, name: str) -> None: ...


@dataclass(frozen=True)
class TimerOutcome:
    """What one timer pass CHANGED. `written` holds only the units whose
    text moved, so a loop that runs every two seconds does not report a
    step it did not take. `refused` names every cron expression with no
    `OnCalendar` spelling, so a caller can log it. `enabled` and
    `unenabled` are the enabled-state half: what this pass turned on, and
    what it could not."""

    written: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    enabled: tuple[str, ...] = ()
    unenabled: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.written or self.removed or self.enabled)


def apply_timers(family: FamilyFile, units: UnitWriter) -> TimerOutcome:
    """Converge one family's timer set: the FILE and the ENABLED state.

    **Removals first.** A trigger the file no longer names must stop
    firing at once, and an interruption between the halves then leaves
    fewer firings than the target (the same ordering invariant 9 asks of
    every other axis)."""
    wanted, refused = _wanted(family)
    mine = _mine(family.name, units.names())
    removed = tuple(name for name in mine if name not in wanted)
    for name in removed:
        units.remove(name)

    written = tuple(name for name, text in wanted.items() if units.read(name) != text)
    for name in written:
        units.write(name, wanted[name])

    enabled, unenabled = _converge_enabled(tuple(wanted), units)
    return TimerOutcome(
        written=written,
        removed=removed,
        refused=refused,
        enabled=enabled,
        unenabled=unenabled,
    )


def _converge_enabled(
    wanted: tuple[str, ...], units: UnitWriter
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Every generated timer is probed, every pass. Answers what this pass
    enabled and what it could not.

    `write` already runs `enable --now`, and `apply_timers` rewrites only
    on a content change. A kill between the two therefore leaves the file
    on disk, unchanged, and disabled FOR EVER: the content check can never
    rewrite it, the schedule never fires, and nothing says so. One
    `is-enabled` per generated timer is
    what closes that, and the set is small — one or two per autonomous
    family, and none for any other kind.

    The second probe is the honest one: it asks systemd whether the enable
    worked, rather than reading a return code from a call that logs its own
    failure. It only runs for a timer that was disabled, which is rare."""
    enabled: list[str] = []
    unenabled: list[str] = []
    for name in wanted:
        if units.is_enabled(name):
            continue

        units.enable(name)
        (enabled if units.is_enabled(name) else unenabled).append(name)

    return tuple(enabled), tuple(unenabled)


def remove_timers(family_name: str, units: UnitWriter) -> tuple[str, ...]:
    """Every timer of a family that is going away. A schedule that outlives
    the family it fires would start a session for a family with no key."""
    mine = _mine(family_name, units.names())
    for name in mine:
        units.remove(name)

    return mine


def timer_text(family: str, calendar: str) -> str:
    """`Unit=` points at the one template service. Without it systemd would
    look for `creche-trigger-<family>-t<N>.service`, which does not exist."""
    service = TRIGGER_SERVICE.format(family=family)
    return f"""# Generated by caregiver for family {family}.
# Do not hand-edit: the next reconcile pass rewrites it from family.yaml.
[Unit]
Description=Trigger for autonomous family {family}

[Timer]
OnCalendar={calendar}
Unit={service}
# A host that was down still fires once when it comes back. An autonomous
# family's schedule is the whole reason it exists.
Persistent=true

[Install]
WantedBy=timers.target
"""


def cron_to_oncalendar(expression: str) -> str:
    """cron's five fields as one `OnCalendar` line. Raises
    `CronShapeError` for the one shape systemd cannot mean."""
    macro = MACRO_CALENDAR.get(expression)
    if macro is not None:
        return macro

    fields = expression.split()
    if len(fields) != CRON_FIELDS:
        raise CronShapeError(f"{expression!r}: expected five fields or a macro")

    minute, hour, day, month, weekday = fields
    clock = f"{_field(hour, 0, HOUR_MAX, 'hour')}:{_field(minute, 0, MINUTE_MAX, 'minute')}:00"
    if day == "*" and month == "*":
        return f"*-*-* {clock}" if weekday == "*" else f"{_weekdays(weekday)} *-*-* {clock}"

    if weekday == "*":
        spelled_day = _number(day, DAY_MIN, DAY_MAX, "day-of-month")
        spelled_month = (
            "*" if month == "*" else f"{_number(month, MONTH_MIN, MONTH_MAX, 'month'):02d}"
        )
        return f"*-{spelled_month}-{spelled_day:02d} {clock}"

    raise CronShapeError(
        f"{expression!r}: cron ORs day-of-month with day-of-week and OnCalendar ANDs them, "
        "so this schedule has no systemd spelling"
    )


def _wanted(family: FamilyFile) -> tuple[dict[str, str], tuple[str, ...]]:
    """The timers this file asks for, and the cron lines that have none.

    Only an autonomous family has triggers (contract 01 §3.13), so every
    other kind wants none and its old units are removed."""
    if family.kind != Kind.AUTONOMOUS or not family.triggers:
        return {}, ()

    wanted: dict[str, str] = {}
    refused: list[str] = []
    position = 0
    for trigger in family.triggers:
        if trigger.cron is None:
            continue

        position += 1
        try:
            calendar = cron_to_oncalendar(trigger.cron)
        except CronShapeError as exc:
            # CONTRACT-QUESTION: contract 05 §3.3's fault table has no code
            # for "this trigger cannot be scheduled".
            log.error("%s: trigger %d has no timer: %s", family.name, position, exc)
            refused.append(trigger.cron)
            continue

        wanted[TIMER_NAME.format(family=family.name, position=position)] = timer_text(
            family.name, calendar
        )

    return wanted, tuple(refused)


def _mine(family_name: str, installed: tuple[str, ...]) -> tuple[str, ...]:
    heads = tuple(f"{prefix}{family_name}-t" for prefix in OWNED_PREFIXES)
    return tuple(name for name in installed if name.startswith(heads))


def _number(value: str, low: int, high: int, name: str) -> int:
    if not value.isdigit() or not low <= int(value) <= high:
        raise CronShapeError(f"{name} {value!r} is not a number in {low} to {high}")

    return int(value)


def _atom(atom: str, low: int, high: int, name: str) -> str:
    """One element of a minute or hour list. systemd refuses a step on `*`
    and means the same thing by a step on the first value."""
    base, _, step = atom.partition("/")
    if step and not step.isdigit():
        raise CronShapeError(f"{name} step {step!r} is not a number")

    if base == "*":
        return f"{low:02d}/{step}" if step else "*"

    first, _, last = base.partition("-")
    bounds = [_number(first, low, high, name)]
    if last:
        bounds.append(_number(last, low, high, name))

    if bounds != sorted(bounds):
        raise CronShapeError(f"{name} range {atom!r} runs backwards")

    spelled = "..".join(f"{one:02d}" for one in bounds)
    return f"{spelled}/{step}" if step else spelled


def _field(field: str, low: int, high: int, name: str) -> str:
    return ",".join(_atom(one, low, high, name) for one in field.split(","))


def _weekdays(field: str) -> str:
    parts: list[str] = []
    for atom in field.split(","):
        first, _, last = atom.partition("-")
        if first not in CRON_DOW or (last and last not in CRON_DOW):
            raise CronShapeError(f"day-of-week {atom!r}: use 0 to 7, a range, or a list")

        if not last:
            parts.append(CRON_DOW[first])
            continue

        # systemd orders a range Monday-first. `0-6` is Sun..Sat, which runs
        # backwards there, so it is refused rather than guessed at.
        low = SUNDAY_LAST if first == "0" else int(first)
        high = SUNDAY_LAST if last == "0" else int(last)
        if low > high:
            raise CronShapeError(
                f"day-of-week range {atom!r} runs backwards in a Monday-first week"
            )

        parts.append(f"{CRON_DOW[first]}..{CRON_DOW[last]}")

    return ",".join(parts)


class Systemctl(Protocol):
    """`systemctl --user <args>`. A Protocol so no test shells out."""

    def run(self, *args: str) -> None: ...
    def query(self, *args: str) -> str: ...


#: What `systemctl is-enabled` prints for a unit that will fire. Every
#: other answer — `disabled`, `masked`, `not-found`, nothing at all — is
#: a timer that does not.
ENABLED_STATES: Final = frozenset({"enabled", "enabled-runtime"})


class UserSystemctl:
    """A failure is logged, never raised. A timer that would not enable
    costs a schedule. Stopping the reconcile pass over it would cost the
    family's permissions, which is the larger loss."""

    def run(self, *args: str) -> None:
        done = self._call(args)
        if done is None:
            return

        if done.returncode != 0:
            log.error("systemctl --user %s: %s", " ".join(args), done.stderr.strip()[:200])

    def query(self, *args: str) -> str:
        """The first word systemd printed, or empty when it could not be
        asked at all.

        The exit code is NOT read. `systemctl is-enabled` exits non-zero
        to say `disabled`, which is the answer this method exists for, so
        a caller that read the code would call every disabled timer an
        error and every error a disabled timer."""
        done = self._call(args)
        if done is None:
            return ""

        return done.stdout.strip().partition("\n")[0].strip()

    def _call(self, args: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["systemctl", "--user", *args],
                capture_output=True,
                text=True,
                timeout=SYSTEMCTL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("systemctl --user %s: %s", " ".join(args), exc)
            return None


class UserUnits:
    """`~/.config/systemd/user`, plus the `systemctl --user` calls that make
    a written file real.

    A write is followed by `enable --now`: a timer file nothing enabled
    never fires, which is the silent half of this whole feature going
    wrong. A remove disables first, then unlinks, because unlinking an
    enabled unit leaves a dangling symlink in `timers.target.wants`.

    One lock, because families converge side by side now and
    this is the one actor they share that is not already safe: the file
    write and the two `systemctl` calls that make it real are one action,
    and `daemon-reload` is the whole manager's, not one unit's."""

    def __init__(self, root: Path | None = None, systemctl: Systemctl | None = None) -> None:
        self._root = root if root is not None else _default_unit_dir()
        self._systemctl = systemctl if systemctl is not None else UserSystemctl()
        self._mutex = threading.Lock()

    def read(self, name: str) -> str | None:
        try:
            return (self._root / name).read_text(encoding="utf-8")
        except OSError:
            return None

    def write(self, name: str, text: str) -> None:
        with self._mutex:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / name).write_text(text, encoding="utf-8")
            self._systemctl.run("daemon-reload")
            self._systemctl.run("enable", "--now", name)

    def remove(self, name: str) -> None:
        with self._mutex:
            self._systemctl.run("disable", "--now", name)
            (self._root / name).unlink(missing_ok=True)
            self._systemctl.run("daemon-reload")

    def is_enabled(self, name: str) -> bool:
        """systemd's own answer, not this process's memory of one. The
        memory is exactly what a kill takes away."""
        return self._systemctl.query("is-enabled", name) in ENABLED_STATES

    def enable(self, name: str) -> None:
        with self._mutex:
            self._systemctl.run("enable", "--now", name)

    def names(self) -> tuple[str, ...]:
        if not self._root.is_dir():
            return ()

        return tuple(
            sorted(
                one.name
                for one in self._root.iterdir()
                if one.is_file() and one.name.startswith(OWNED_PREFIXES)
            )
        )


def _default_unit_dir() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config) if config else Path.home() / ".config"
    return base / "systemd" / "user"


class FakeSystemctl:
    """Records every `systemctl --user` call as one string, and remembers
    what `enable` and `disable` did, so `is-enabled` answers the way the
    real one would. Add a name to `refuse` for a unit systemd will not
    enable."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.refuse: set[str] = set()
        self._enabled: set[str] = set()

    def run(self, *args: str) -> None:
        self.calls.append(" ".join(args))
        verb, name = args[0], args[-1]
        if verb == "enable" and name not in self.refuse:
            self._enabled.add(name)
        elif verb == "disable":
            self._enabled.discard(name)

    def query(self, *args: str) -> str:
        self.calls.append(" ".join(args))
        return "enabled" if args[-1] in self._enabled else "disabled"


class FakeUnits:
    """In memory. Tests read `.files` for content, `.calls` for order and
    `.enabled` for what would fire. Add a name to `refuse` for a unit that
    will not enable.

    `write` marks a unit enabled without recording a call, because
    `UserUnits.write` runs `enable --now` as part of the same one action.

    Locked like `UserUnits`, because a test that drives the loop drives
    several families' passes at once."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.calls: list[str] = []
        self.enabled: set[str] = set()
        self.refuse: set[str] = set()
        self._mutex = threading.Lock()

    def read(self, name: str) -> str | None:
        with self._mutex:
            return self.files.get(name)

    def write(self, name: str, text: str) -> None:
        with self._mutex:
            self.calls.append(f"write {name}")
            self.files[name] = text
            if name not in self.refuse:
                self.enabled.add(name)

    def remove(self, name: str) -> None:
        with self._mutex:
            self.calls.append(f"remove {name}")
            self.files.pop(name, None)
            self.enabled.discard(name)

    def is_enabled(self, name: str) -> bool:
        with self._mutex:
            return name in self.enabled

    def enable(self, name: str) -> None:
        with self._mutex:
            self.calls.append(f"enable {name}")
            if name not in self.refuse:
                self.enabled.add(name)

    def names(self) -> tuple[str, ...]:
        with self._mutex:
            return tuple(sorted(self.files))
