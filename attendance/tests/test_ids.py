"""Identifier forms and ULID minting (contract 02 §2)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from attendance.ids import (
    SESSION_ID_MAX,
    SessionPrefix,
    UlidFactory,
    is_attachment,
    is_family,
    is_sandbox,
    is_session,
    is_ulid,
    new_ulid,
    session_prefix,
)

ULID_LENGTH = 26
_MINT_COUNT = 5000


def test_family_form() -> None:
    assert is_family("chat")
    assert is_family("agent-control")
    assert not is_family("Chat")
    assert not is_family("9chat")
    assert not is_family("c")
    assert not is_family("a" * 32)


def test_session_form() -> None:
    assert is_session("owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33")
    assert is_session("tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR")
    assert not is_session("")
    assert not is_session(".")
    assert not is_session("..")
    assert not is_session("-leading-dash")
    assert not is_session("has/slash")
    assert not is_session("a" * (SESSION_ID_MAX + 1))


def test_session_prefix() -> None:
    assert session_prefix("owui-abc") is SessionPrefix.OWUI
    assert session_prefix("job-01JB") is SessionPrefix.JOB
    assert session_prefix("nothing") is None


def test_sandbox_and_attachment_forms() -> None:
    assert is_sandbox("chat-s3")
    assert not is_sandbox("chat-3")
    assert is_attachment("data.csv")
    assert not is_attachment("..")
    assert not is_attachment("a/b")
    assert not is_attachment("x" * 121)


@pytest.mark.parametrize(
    ("check", "value"),
    [
        (is_family, "chat"),
        (is_session, "tui-abc"),
        (is_ulid, "01JBQ7WZ0X4T9V6K2H8M3N5PQR"),
        (is_sandbox, "chat-s1"),
        (is_attachment, "data.csv"),
    ],
)
def test_trailing_newline_is_refused(check: Callable[[str], bool], value: str) -> None:
    """A `$` anchor also matches before a final newline. `\\Z` does not."""
    assert check(value)
    assert not check(value + "\n")


def test_ulid_is_sortable_and_unique() -> None:
    minted = [new_ulid() for _ in range(_MINT_COUNT)]

    assert all(len(value) == ULID_LENGTH for value in minted)
    assert all(is_ulid(value) for value in minted)
    assert len(set(minted)) == _MINT_COUNT
    assert minted == sorted(minted)


def test_ulid_rejects_excluded_letters() -> None:
    assert not is_ulid("I" * ULID_LENGTH)
    assert not is_ulid("01JBQ7WZ0X4T9V6K2H8M3N5PQ")


def test_factory_is_monotonic_within_one_ms() -> None:
    factory = UlidFactory()
    batch = [factory.mint() for _ in range(100)]

    assert batch == sorted(batch)
    assert len(set(batch)) == len(batch)
