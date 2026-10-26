"""The log cap of the channel is one number in two places.

Contract 03 §8 gives one `message` of a channel line 4 KiB, as bytes of
UTF-8. Two places hold that cap, and each one cuts a text with it:

1. `attendance/src/attendance/wire.py`, the host. It cuts the text of a line
   that it reads.
2. `playpen/src/constants.ts`, the playpen. It cuts a text before it writes
   the line.

With two numbers, one side cuts a text that the other side holds whole. The
check here fails when the two numbers differ. It also fails when the
playpen file holds the cap two times.
"""

from __future__ import annotations

import re
from pathlib import Path

from attendance.wire import MAX_LOG_BYTES

REPO = Path(__file__).resolve().parents[2]
PLAYPEN_CONSTANTS = REPO / "playpen" / "src" / "constants.ts"

NAME = "MAX_LOG_BYTES"

#: The one line that declares the cap in TypeScript. A digit group can hold
#: `_`.
TYPESCRIPT_LINE = re.compile(rf"export const {NAME} = ([0-9_]+);")


def _declared(path: Path, line_form: re.Pattern[str]) -> list[int]:
    """Each value that a line of this form declares in the file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    found = [line_form.fullmatch(line) for line in lines]

    return [int(match.group(1).replace("_", "")) for match in found if match is not None]


def test_the_host_and_the_playpen_hold_one_log_cap() -> None:
    assert _declared(PLAYPEN_CONSTANTS, TYPESCRIPT_LINE) == [MAX_LOG_BYTES]
