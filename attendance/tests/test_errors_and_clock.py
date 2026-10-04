"""The error model (contract 02 §14) and the one clock."""

from __future__ import annotations

from datetime import UTC, datetime

from attendance.clock import age_seconds, parse_rfc3339, rfc3339, rfc3339_ms
from attendance.errors import (
    HTTP_CONFLICT,
    HTTP_UNAVAILABLE,
    ApiError,
    ErrorCode,
    http_status,
    retry_after_s,
)

_MOMENT = datetime(2026, 9, 18, 19, 22, 5, 118000, tzinfo=UTC)
_NINETY_SECONDS = 90.0


def test_every_code_has_a_status() -> None:
    for code in ErrorCode:
        assert http_status(code) > 0


def test_retryable_codes_carry_a_wait() -> None:
    assert retry_after_s(ErrorCode.SESSION_BUSY) is not None
    assert retry_after_s(ErrorCode.FAMILY_DEGRADED) is not None
    assert retry_after_s(ErrorCode.SANDBOX_UNAVAILABLE) is not None
    assert retry_after_s(ErrorCode.BAD_REQUEST) is None
    assert retry_after_s(ErrorCode.FAMILY_INVALID) is None


def test_error_body_shape() -> None:
    error = ApiError(
        ErrorCode.SESSION_BUSY,
        "another door holds the writer lease",
        family="chat",
        session="owui-3f2a",
        detail={"holder": "owui"},
    )

    assert error.status == HTTP_CONFLICT
    body = error.body()

    assert body["error"]["code"] == "session_busy"
    assert body["error"]["family"] == "chat"
    assert body["error"]["turn"] is None
    assert body["error"]["detail"] == {"holder": "owui"}
    assert body["retry_after_s"] == retry_after_s(ErrorCode.SESSION_BUSY)


def test_no_retry_key_when_the_code_is_final() -> None:
    error = ApiError(ErrorCode.BAD_REQUEST, "prompt missing")

    assert "retry_after_s" not in error.body()


def test_degraded_is_unavailable() -> None:
    assert http_status(ErrorCode.FAMILY_DEGRADED) == HTTP_UNAVAILABLE


def test_timestamp_shapes() -> None:
    assert rfc3339(_MOMENT) == "2026-09-18T19:22:05Z"
    assert rfc3339_ms(_MOMENT) == "2026-09-18T19:22:05.118Z"


def test_parse_round_trip_and_refusal() -> None:
    assert parse_rfc3339(rfc3339_ms(_MOMENT)) == _MOMENT
    assert parse_rfc3339("not a time") is None
    assert parse_rfc3339("") is None


def test_a_time_that_leaves_the_years_of_a_datetime_is_refused() -> None:
    """The offset moves the time in UTC before year 1 or after year 9999."""
    first = datetime(1, 1, 1, tzinfo=UTC)
    last = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    assert parse_rfc3339("0001-01-01T00:00:00+00:01") is None
    assert parse_rfc3339("9999-12-31T23:59:59-00:01") is None
    assert parse_rfc3339("0001-01-01T00:00:00Z") == first
    assert parse_rfc3339("0001-01-01T00:01:00+00:01") == first
    assert parse_rfc3339("9999-12-31T23:59:59Z") == last


def test_a_year_below_1000_has_four_digits() -> None:
    """The text reads back, on each system."""
    early = datetime(999, 1, 2, 3, 4, 5, tzinfo=UTC)

    assert rfc3339(early) == "0999-01-02T03:04:05Z"
    assert rfc3339(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00Z"
    assert parse_rfc3339(rfc3339(early)) == early


def test_the_seconds_form_drops_the_fraction() -> None:
    late = datetime(2026, 9, 18, 19, 22, 5, 999999, tzinfo=UTC)

    assert rfc3339(late) == "2026-09-18T19:22:05Z"


def test_age_seconds() -> None:
    later = datetime(2026, 9, 18, 19, 23, 35, 118000, tzinfo=UTC)

    assert age_seconds(_MOMENT, later) == _NINETY_SECONDS
