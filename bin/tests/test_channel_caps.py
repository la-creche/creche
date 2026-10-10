"""The log cap of the channel is one number in three places.

Contract 03 §8 gives one `message` of a channel line 4 KiB, as bytes of
UTF-8. Three places hold that cap, and each one cuts a text with it:

1. `attendance/src/attendance/wire.py`, the host. It cuts the text of a line
   that it reads.
2. `playpen/src/constants.ts`, the playpen. It cuts a text before it writes
   the line.
3. `rust/crates/creche-contracts/src/channel.rs`, the Rust contract crate.
   Its host side and its playpen side read the one constant.

With two numbers, one side cuts a text that the other side holds whole. The
checks here fail when the three numbers differ. They also fail when a file
holds the cap two times, or when another file of the Rust channel module
holds a cap of its own.

A Rust test reads no file outside `rust/` and `vectors/data`, so these
checks are here. A push that changes only `rust/` runs no pytest suite
(`bin/lib/rustrule.sh`), so for such a change they run in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

from attendance.wire import MAX_LOG_BYTES

REPO = Path(__file__).resolve().parents[2]
PLAYPEN_CONSTANTS = REPO / "playpen" / "src" / "constants.ts"
RUST_CHANNEL = REPO / "rust" / "crates" / "creche-contracts" / "src" / "channel.rs"
RUST_CHANNEL_PARTS = RUST_CHANNEL.with_suffix("")

NAME = "MAX_LOG_BYTES"

#: The one line that declares the cap in each language. A digit group can
#: hold `_`.
TYPESCRIPT_LINE = re.compile(rf"export const {NAME} = ([0-9_]+);")
RUST_LINE = re.compile(rf"pub const {NAME}: usize = ([0-9_]+);")

#: A Rust line that declares a constant whose name starts with the name of a
#: log cap, at each visibility.
RUST_LOG_CAP = re.compile(r"\s*(?:pub(?:\([a-z]+\))? )?const MAX_LOG_[A-Z_]*:.*")


def _declared(path: Path, line_form: re.Pattern[str]) -> list[int]:
    """Each value that a line of this form declares in the file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    found = [line_form.fullmatch(line) for line in lines]

    return [int(match.group(1).replace("_", "")) for match in found if match is not None]


def test_the_host_the_playpen_and_the_rust_crate_hold_one_log_cap() -> None:
    assert _declared(PLAYPEN_CONSTANTS, TYPESCRIPT_LINE) == [MAX_LOG_BYTES]
    assert _declared(RUST_CHANNEL, RUST_LINE) == [MAX_LOG_BYTES]


def test_no_part_of_the_rust_channel_module_holds_a_log_cap_of_its_own() -> None:
    parts = sorted(RUST_CHANNEL_PARTS.glob("*.rs"))
    holders = [
        part.name
        for part in parts
        if any(RUST_LOG_CAP.fullmatch(line) for line in part.read_text("utf-8").splitlines())
    ]

    assert parts, "the Rust channel module has no part"
    assert holders == []


def test_the_pattern_finds_a_log_cap_at_each_visibility() -> None:
    held = [
        "pub const MAX_LOG_BYTES: usize = 4096;",
        "pub const MAX_LOG_CHARS: usize = 4096;",
        "const MAX_LOG_BYTES: usize = 4_096;",
        "    pub(super) const MAX_LOG_BYTES: usize = 4096;",
        "pub(crate) const MAX_LOG_UNITS: u64 = 1;",
    ]
    not_held = [
        "use super::MAX_LOG_BYTES;",
        "/// At most [`MAX_LOG_BYTES`] bytes.",
        "pub const MAX_LINE_BYTES: usize = 1_048_576;",
        "    text_or_empty(record, key).cut_bytes(MAX_LOG_BYTES)",
    ]

    assert [line for line in held if RUST_LOG_CAP.fullmatch(line) is None] == []
    assert [line for line in not_held if RUST_LOG_CAP.fullmatch(line) is not None] == []
