"""The one reader of a time text: the `date-time` of RFC 3339, section 5.6.

`parse` gives the instant of a text as an aware `datetime` in UTC. For each
other text it gives `None`. It raises for no text.

A time has these parts, in this sequence, and no other character:

1. The date, `YYYY-MM-DD`. The month is 01 to 12, and the month has the day.
2. `T` or `t`.
3. The time of the day, `HH:MM:SS`. The hour is 00 to 23. The minute and the
   second are 00 to 59.
4. An optional fraction of the second: `.` and 1 to 9 digits. The result
   keeps the first six digits and drops the others. It does not round.
5. The UTC offset: `Z`, `z`, `+HH:MM` or `-HH:MM`. The hours of an offset are
   00 to 23, and its minutes are 00 to 59.

A digit is one of the ten ASCII digits. The instant in UTC is in the years
0001 to 9999, and the year of the text is not 0000.

Thus these texts are not a time: a text with no UTC offset, a date alone, a
text with a space in place of the `T`, hour 24 and second 60. What a caller
does with a text that is not a time is the rule of that caller.

The source of the grammar is the Rust type `creche_contracts::time::Timestamp`,
in `rust/crates/creche-contracts/src/time.rs`. The vector surface
`runtime.time` records `parse` of the noticeboard copy. A test of the Rust
module gives each input of that surface to the Rust reader. It demands the
same result and the same instant.

One pattern holds the form of a text and the bounds of each field.
`datetime.fromisoformat` then reads the text, and it knows which days a month
has. It gets only a text that the pattern takes, because it also reads texts
that are not in the grammar. It reads some of them under one supported Python
version only, for example hour 24.

This file is a copy. Each package that reads a time holds a file with the
same bytes, and `bin/tests/test_rfc3339_copies.py` fails when two copies
differ. Change each copy in one commit. The file needs the standard library
only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

# CONTRACT-QUESTION: the contracts name RFC 3339 for a time and give no more
# of its grammar, for example contract 05 §2.1. Section 5.6 of RFC 3339
# permits three texts that this reader refuses: a fraction of more than 9
# digits, second 60 and the year 0000. The reader takes the grammar of the
# Rust type `creche_contracts::time::Timestamp`, which refuses the three. To
# take one of them costs a change in each copy of this file, the same change
# of the Rust type and the vectors of that text.

#: The form of a time and the bounds of each field. The pattern does not know
#: which days a month has. It takes day 01 to 31 for each month.
_TIME: Final = re.compile(
    r"(?P<date>\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))"
    r"[Tt]"
    r"(?P<time>(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?)"
    r"(?:[Zz]|(?P<offset>[+-](?:[01]\d|2[0-3]):[0-5]\d))",
    re.ASCII,
)

#: The offset that `Z` and `z` stand for.
_UTC_OFFSET: Final = "+00:00"


def parse(text: str) -> datetime | None:
    """The instant of a time text in UTC, or `None` for a text that is no time."""
    found = _TIME.fullmatch(text)
    if found is None:
        return None

    offset = found["offset"] or _UTC_OFFSET

    try:
        return datetime.fromisoformat(f"{found['date']}T{found['time']}{offset}").astimezone(UTC)
    except (ValueError, OverflowError):
        # `ValueError`: the month does not have the day, or the year is 0000.
        # `OverflowError`: the offset moves the instant out of the years 0001
        # to 9999.
        return None
