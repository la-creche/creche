"""The ids a test mints when it plays a door, held against contract 02 §2."""

from __future__ import annotations

import re

from proc_ids import AUTO_PREFIX, auto_session, new_ulid

#: Contract 02 §2: 26 characters of upper-case Crockford base32.
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

#: A ULID holds 48 bits of time in its first 10 characters, so the first
#: character is 0 to 7.
FIRST_CHARS = "01234567"

MANY = 2000


def test_a_ulid_has_the_form_of_the_contract() -> None:
    minted = [new_ulid() for _ in range(MANY)]

    assert all(ULID.fullmatch(one) for one in minted)
    assert all(one[0] in FIRST_CHARS for one in minted)
    assert len(set(minted)) == MANY


def test_a_later_ulid_does_not_sort_before_an_earlier_time() -> None:
    """The time leads, so two ids of different milliseconds sort by time."""
    first = new_ulid()
    later = new_ulid()

    assert later[:10] >= first[:10]


def test_an_autonomous_session_has_the_prefix_of_the_trigger_door() -> None:
    session = auto_session()

    assert session.startswith(AUTO_PREFIX)
    assert ULID.fullmatch(session.removeprefix(AUTO_PREFIX))
