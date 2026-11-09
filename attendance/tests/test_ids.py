"""Identifier forms and ULID minting (contract 02 §2)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from attendance.ids import (
    SESSION_CWD_MAX,
    SESSION_ID_MAX,
    UlidFactory,
    is_attachment,
    is_family,
    is_session,
    is_session_cwd,
    is_session_id,
    is_ulid,
    new_session_id,
    new_ulid,
)

ULID_LENGTH = 26
_MINT_COUNT = 5000


def test_family_form() -> None:
    assert is_family("chat")
    assert is_family("creche")
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


def test_session_cwd_form() -> None:
    """`SessionCwd` of the wire (decision CH21). The cap counts code points."""
    astral = "/" + "\U0001d49c" * (SESSION_CWD_MAX - 1)

    assert is_session_cwd("/")
    assert is_session_cwd(astral)
    assert not is_session_cwd("work")
    assert not is_session_cwd("/a\0b")
    assert not is_session_cwd(astral + "a")


def test_a_minted_session_id_is_a_wire_session_id() -> None:
    """Contract 02 §2: a UUID in its lowercase hyphenated form (decision CH25
    rule 2)."""
    minted = {new_session_id() for _ in range(100)}

    assert len(minted) == 100
    assert all(is_session_id(one) for one in minted)


@pytest.mark.parametrize(
    "value",
    [
        "sess-1",
        "3F2A9C41-77B0-4A1E-9A4C-1D0E5F8B2C33",
        "3f2a9c4177b04a1e9a4c1d0e5f8b2c33",
        "{3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33}",
        "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33\n",
        "",
    ],
    ids=["sbx_era", "upper", "no_hyphens", "braces", "newline", "empty"],
)
def test_an_id_of_another_form_is_no_session_id(value: str) -> None:
    """Decision CH10-9: an sbx-era id is not found, with no vmm call."""
    assert not is_session_id(value)


def test_attachment_forms() -> None:
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
