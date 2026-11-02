"""The generated models read the wire as the Rust crate does.

Each case here mirrors a test of `rust/crates/wire/src/lib.rs`. The shared
wire vectors (decision CH9-1) replace these cases with one data file that
both languages read."""

import pytest
from creche_wire import SessionId
from pydantic import ValidationError

GOOD = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b"


def test_a_lowercase_uuid_is_a_session_id() -> None:
    session = SessionId.model_validate_json(f'"{GOOD}"')

    assert session.root == GOOD
    assert session.model_dump_json() == f'"{GOOD}"'


@pytest.mark.parametrize(
    "text",
    [
        "",
        "3f2b8c1e",
        f"{GOOD}0",
        "3F2B8C1E-9D4A-4E6B-8A7C-0B1D2E3F4A5B",
        "3f2b8c1e99d4a-4e6b-8a7c-0b1d2e3f4a5b",
        "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5g",
        "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a/b",
        "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4aé",
    ],
)
def test_a_text_of_the_wrong_form_is_refused(text: str) -> None:
    with pytest.raises(ValidationError):
        SessionId.model_validate_json(f'"{text}"')


def test_a_trailing_newline_is_refused() -> None:
    """A `$` of Python's `re` also matches before a final newline. pydantic
    checks a pattern with its Rust engine, where `$` is the end of the text,
    as in the Rust crate."""
    with pytest.raises(ValidationError):
        SessionId.model_validate_json(f'"{GOOD}\\n"')


@pytest.mark.parametrize("json", ["42", "null", "[]"])
def test_a_json_value_that_is_no_string_is_refused(json: str) -> None:
    with pytest.raises(ValidationError):
        SessionId.model_validate_json(json)
