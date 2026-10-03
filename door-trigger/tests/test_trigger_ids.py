"""`ulid.py`, `payload.py` and the error-code table in `errors.py`: the
small, pure-function modules every other part of this door builds on."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from agent_door_trigger.errors import WEBHOOK_STATUS, webhook_status
from agent_door_trigger.payload import (
    MAX_PAYLOAD_BYTES,
    PayloadInvalid,
    PayloadTooLarge,
    read_payload,
)
from agent_door_trigger.ulid import ULID_LENGTH, ULID_PATTERN, new_ulid


def test_a_ulid_matches_contract_02s_own_pattern() -> None:
    value = new_ulid()

    assert len(value) == ULID_LENGTH
    assert ULID_PATTERN.match(value)
    assert value == value.upper()


def test_no_ulid_carries_i_l_o_or_u() -> None:
    # Generate a handful: a single draw could get lucky and never land on
    # the excluded letters' positions by chance.
    for _ in range(200):
        value = new_ulid()
        assert not any(letter in value for letter in "ILOU")


def _clock(seconds: float) -> Callable[[], float]:
    def fixed() -> float:
        return seconds

    return fixed


def test_two_ulids_in_the_same_millisecond_still_differ() -> None:
    fixed = _clock(1_700_000_000.0)

    first = new_ulid(now=fixed)
    second = new_ulid(now=fixed)

    assert first != second
    # Same millisecond -> same first 10 characters (the timestamp half).
    assert first[:10] == second[:10]


def test_a_later_timestamp_sorts_after_an_earlier_one() -> None:
    early = new_ulid(now=_clock(1_700_000_000.0))
    late = new_ulid(now=_clock(1_700_000_100.0))

    assert early < late


# --- payload.py ---


def test_a_small_valid_json_payload_passes_through_untouched() -> None:
    raw = b'{"b": 1, "a": 2}'  # deliberately unsorted keys

    assert read_payload(raw) == raw.decode("utf-8")


def test_an_oversized_payload_is_refused_as_too_large() -> None:
    raw = b'{"pad": "' + b"x" * MAX_PAYLOAD_BYTES + b'"}'

    # The specific subclass matters: a caller maps PayloadTooLarge to 413
    # and PayloadInvalid to 400 (webhooks.py).
    with pytest.raises(PayloadTooLarge, match="over the"):
        read_payload(raw)


def test_a_payload_just_under_the_cap_is_accepted() -> None:
    filler = "x" * (MAX_PAYLOAD_BYTES - 1_000)
    raw = f'{{"x":"{filler}"}}'.encode()
    assert len(raw) < MAX_PAYLOAD_BYTES

    assert read_payload(raw) == raw.decode("utf-8")


def test_invalid_json_is_refused_as_invalid() -> None:
    with pytest.raises(PayloadInvalid, match="not valid JSON"):
        read_payload(b"{not json")


def test_non_utf8_bytes_are_refused_as_invalid() -> None:
    with pytest.raises(PayloadInvalid, match="not UTF-8"):
        read_payload(b"\xff\xfe\x00\x01")


def test_a_json_array_or_scalar_payload_is_valid_too() -> None:
    # JSON's grammar allows any value as a document root, and the rule is
    # "valid JSON" - not "a JSON object".
    assert read_payload(b"[1, 2, 3]") == "[1, 2, 3]"
    assert read_payload(b'"just a string"') == '"just a string"'


# --- errors.py ---


def test_every_webhook_status_is_a_real_http_status() -> None:
    for code, status in WEBHOOK_STATUS.items():
        assert 400 <= status <= 599, f"{code} maps to {status}, not a client/server error status"


def test_an_unmapped_attendance_code_falls_back_to_502() -> None:
    assert webhook_status("some_future_code_this_table_does_not_know") == 502


def test_queue_full_is_429_so_a_caller_can_tell_it_apart_from_a_hard_refusal() -> None:
    assert webhook_status("queue_full") == 429


def test_forbidden_is_403_the_not_autonomous_or_wrong_kind_case() -> None:
    assert webhook_status("forbidden") == 403
