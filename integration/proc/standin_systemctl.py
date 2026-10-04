"""A stand-in for `systemctl --user`, with the verbs that `caregiver` runs.

    standin_systemctl.py <state directory> --user <verb> [--now] [<unit>]

A test machine has no user manager to give units to, and macOS has no
systemd. `caregiver` still runs these commands for each timer that it
generates:

    --user daemon-reload
    --user enable --now <unit>
    --user disable --now <unit>
    --user is-enabled <unit>

Every fact is a file under the state directory:

    enabled/<unit>     the unit is enabled
    tune/              what a test writes to change one call

Three things here copy what the real program does:

1. It finds a unit file in `$XDG_CONFIG_HOME/systemd/user`, the directory of
   a user manager. `enable` fails for a unit with no file there.
2. `is-enabled` prints one word. It exits 0 only for `enabled`.
3. A call with no `--user` fails. A user unit runs none of these as root.

One thing follows what `caregiver/src/caregiver/timers.py` records, and no
run of the real program checked it for this suite:

4. `disable` fails for a unit with no file, and the unit stays enabled. On
   the host, a unit file that goes first leaves the link that enabled it.

A test tunes one call with a file under `tune/`:

    tune/refuse-enable-<unit>     `enable` fails for that unit
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_NOT_FOUND: Final = 4
EXIT_USAGE: Final = 64

USER_FLAG: Final = "--user"
ENABLED_DIR: Final = "enabled"
TUNE_DIR: Final = "tune"

ENABLED: Final = "enabled"
DISABLED: Final = "disabled"
NOT_FOUND: Final = "not-found"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != USER_FLAG:
        return _fail(EXIT_USAGE, f"usage: standin_systemctl.py <state directory> {USER_FLAG} ...")

    state = Path(argv[0])
    verb = argv[2]
    units = [word for word in argv[3:] if not word.startswith("-")]

    if verb == "daemon-reload":
        return EXIT_OK

    if len(units) != 1 or "/" in units[0]:
        return _fail(EXIT_USAGE, f"{verb} needs one unit name")

    unit = units[0]

    if verb == "enable":
        return _enable(state, unit)

    if verb == "disable":
        return _disable(state, unit)

    if verb == "is-enabled":
        return _is_enabled(state, unit)

    return _fail(EXIT_USAGE, f"no verb named {verb}")


def _enable(state: Path, unit: str) -> int:
    if not _unit_file(unit).is_file():
        return _fail(EXIT_FAILED, f"Unit {unit} does not exist")

    if (state / TUNE_DIR / f"refuse-enable-{unit}").exists():
        return _fail(EXIT_FAILED, f"Failed to enable unit {unit}")

    marker = state / ENABLED_DIR / unit
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    return EXIT_OK


def _disable(state: Path, unit: str) -> int:
    """A unit with no file stays enabled: the removal of the file came too soon."""
    if not _unit_file(unit).is_file():
        return _fail(EXIT_FAILED, f"Unit file {unit} does not exist")

    (state / ENABLED_DIR / unit).unlink(missing_ok=True)

    return EXIT_OK


def _is_enabled(state: Path, unit: str) -> int:
    if (state / ENABLED_DIR / unit).exists():
        sys.stdout.write(f"{ENABLED}\n")

        return EXIT_OK

    if _unit_file(unit).is_file():
        sys.stdout.write(f"{DISABLED}\n")

        return EXIT_FAILED

    sys.stdout.write(f"{NOT_FOUND}\n")

    return EXIT_NOT_FOUND


def _unit_file(unit: str) -> Path:
    """Where a user manager reads the unit from."""
    config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config) if config else Path.home() / ".config"

    return base / "systemd" / "user" / unit


def _fail(code: int, message: str) -> int:
    sys.stderr.write(f"standin_systemctl: {message}\n")

    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
