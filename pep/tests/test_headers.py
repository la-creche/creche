"""Contract 04 §3: the advisory headers are read, bounded, and never trusted."""

from __future__ import annotations

import logging
from re import Pattern

import pytest
from agent_pep.family_ids import FAMILY_NAME_RE, SESSION_ID_RE, ULID_RE
from agent_pep.headers import (
    DELEGATION_ID_HEADER,
    MAX_SESSION_ID_CHARS,
    SESSION_ID_HEADER,
    TURN_ID_HEADER,
    Claimed,
    read_claimed,
)

#: Crockford base32 excludes I, L, O and U. These are contract 04 §6.3's
#: example ids, which the shape accepts.
TURN = "01K5J9QWB9R1V3T5Y7H9J2K4P6"
DELEGATION = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
SESSION = "owui-8f1c2e"


def test_all_three_are_read() -> None:
    claimed = read_claimed(
        {
            SESSION_ID_HEADER: SESSION,
            TURN_ID_HEADER: TURN,
            DELEGATION_ID_HEADER: DELEGATION,
        }
    )
    assert claimed == Claimed(session_id=SESSION, turn_id=TURN, delegation_id=DELEGATION)


def test_header_names_are_case_insensitive() -> None:
    claimed = read_claimed({"X-Session-Id": SESSION, "X-Turn-Id": TURN})
    assert claimed.session_id == SESSION
    assert claimed.turn_id == TURN


def test_absent_headers_read_as_none() -> None:
    assert read_claimed({}) == Claimed()


def test_the_record_always_carries_three_keys() -> None:
    assert read_claimed({}).as_record() == {
        "session_id": None,
        "turn_id": None,
        "delegation_id": None,
    }


@pytest.mark.parametrize(
    "name,value",
    [
        (SESSION_ID_HEADER, "-starts-with-a-dash"),
        (SESSION_ID_HEADER, "has a space"),
        (SESSION_ID_HEADER, "slash/into/a/path"),
        (SESSION_ID_HEADER, ""),
        (TURN_ID_HEADER, "not-a-ulid"),
        (TURN_ID_HEADER, "01K5J9QWB9R1V3T5Y7H9J2K4P"),
        (DELEGATION_ID_HEADER, "01K5J9QWB2M4N6Q8S0V2W4Y6A8X"),
        (DELEGATION_ID_HEADER, "01K5J9QWB2M4N6Q8S0V2W4Y6AI"),
    ],
)
def test_a_malformed_value_reads_as_absent(name: str, value: str) -> None:
    field = name.removeprefix("x-").replace("-", "_")
    assert read_claimed({name: value}).as_record()[field] is None


def test_an_oversized_session_id_is_dropped() -> None:
    claimed = read_claimed({SESSION_ID_HEADER: "a" * (MAX_SESSION_ID_CHARS + 1)})
    assert claimed.session_id is None


def test_a_session_id_at_the_cap_is_kept() -> None:
    value = "a" * MAX_SESSION_ID_CHARS
    assert read_claimed({SESSION_ID_HEADER: value}).session_id == value


def test_a_dropped_value_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """The value is caller-supplied, so only its length is reported."""
    secret_looking = "x" * 200
    with caplog.at_level(logging.WARNING):
        read_claimed({SESSION_ID_HEADER: secret_looking})
    assert secret_looking not in caplog.text
    assert "200 characters" in caplog.text


def test_one_bad_header_does_not_drop_the_others() -> None:
    claimed = read_claimed({SESSION_ID_HEADER: "bad id", TURN_ID_HEADER: TURN})
    assert claimed.session_id is None
    assert claimed.turn_id == TURN


def test_a_lowercase_ulid_is_dropped() -> None:
    """Contract 02 §2 and contract 04 §4.1 fix one upper-case alphabet."""
    claimed = read_claimed({TURN_ID_HEADER: TURN.lower(), DELEGATION_ID_HEADER: DELEGATION.lower()})
    assert claimed.turn_id is None
    assert claimed.delegation_id is None


@pytest.mark.parametrize(
    "shape,valid", [(FAMILY_NAME_RE, "chat"), (SESSION_ID_RE, SESSION), (ULID_RE, TURN)]
)
def test_a_trailing_newline_never_passes_an_anchor(shape: Pattern[str], valid: str) -> None:
    """Python's `$` matches before a final newline. These patterns use `\\Z`."""
    assert shape.match(valid) is not None
    assert shape.match(valid + "\n") is None
