"""The family token and `creds.json` (contract 04 section 2, contract 03
section 12)."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import caregiver.credentials as credentials_module
import pytest
from caregiver.credentials import (
    TOKEN_BYTES,
    Credentials,
    mint_token,
    read_creds,
    token_sha256,
    write_creds,
)
from caregiver_helpers import DEEPER_THAN_STR, UNREADABLE_JSON, NoText

#: The fields that `read_creds` converts with `str`.
TEXT_FIELDS = ("litellm_key", "pep_token", "written_at")


def test_mint_token_is_base32_with_no_padding() -> None:
    token = mint_token()
    assert "=" not in token
    assert token == token.upper()  # base32's own alphabet
    # base32 encodes 5 bits per character: ceil(256 / 5) = 52 characters.
    assert len(token) == -(-TOKEN_BYTES * 8 // 5)


def test_mint_token_is_not_reused() -> None:
    assert mint_token() != mint_token()


def test_token_sha256_matches_a_plain_hash() -> None:
    token = "ABCDEF"
    assert token_sha256(token) == hashlib.sha256(token.encode("ascii")).hexdigest()


def test_credentials_as_json_shape() -> None:
    creds = Credentials(
        epoch=1, litellm_key="sk-x", pep_token="tok", written_at="2026-09-19T00:00:00Z"
    )
    assert creds.as_json() == {
        "epoch": 1,
        "litellm_key": "sk-x",
        "pep_token": "tok",
        "written_at": "2026-09-19T00:00:00Z",
        # A family that never rotated carries no overlap. The two keys are
        # present and null rather than absent, so a reader never has to ask
        # whether the writer was an older version (contract 05 §6.3).
        "previous_pep_token": None,
        "previous_expires_at": None,
    }


def test_only_the_current_token_is_accepted_without_an_overlap() -> None:
    creds = Credentials(
        epoch=1, litellm_key="sk-x", pep_token="tok", written_at="2026-09-19T00:00:00Z"
    )
    assert creds.accepted_tokens("2026-09-19T00:00:00Z") == ("tok",)


def test_both_tokens_are_accepted_until_the_overlap_expires() -> None:
    """Contract 05 §6.3 step 5: the old token keeps working until
    `rotation_grace_s` passes, so a turn that started under it finishes."""
    creds = Credentials(
        epoch=2,
        litellm_key="sk-x",
        pep_token="new",
        written_at="2026-09-19T00:00:00Z",
        previous_pep_token="old",
        previous_expires_at="2026-09-19T00:05:00Z",
    )
    assert creds.accepted_tokens("2026-09-19T00:04:59Z") == ("new", "old")
    assert creds.accepted_tokens("2026-09-19T00:05:00Z") == ("new",)


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    creds = Credentials(
        epoch=3, litellm_key="sk-x", pep_token="tok", written_at="2026-09-19T00:00:00Z"
    )
    path = tmp_path / "creds.json"
    write_creds(path, creds)
    assert read_creds(path) == creds


def test_write_creds_is_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    write_creds(path, Credentials(1, "sk-x", "tok", "2026-09-19T00:00:00Z"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_creds_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert read_creds(tmp_path / "no-such-file.json") is None


def test_read_creds_of_corrupt_json_is_none(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_creds(path) is None


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_read_creds_of_content_that_does_not_read_is_none(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "creds.json"
    path.write_bytes(raw)
    assert read_creds(path) is None


@pytest.mark.parametrize("epoch", ["Infinity", "-Infinity", "1e999"])
def test_read_creds_of_an_epoch_with_no_integer_is_none(tmp_path: Path, epoch: str) -> None:
    """The reader says that it never raises. An epoch that is a float with
    no integer value is a refusal, as an epoch that is not a number is."""
    path = tmp_path / "creds.json"
    path.write_text(
        f'{{"epoch": {epoch}, "litellm_key": "sk-x", "pep_token": "tok", "written_at": "w"}}',
        encoding="utf-8",
    )
    assert read_creds(path) is None


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_read_creds_of_a_field_that_nests_deep_is_none(tmp_path: Path, field: str) -> None:
    """One Python version reads a value that nests deeper than `str`
    converts. The reader says that it never raises."""
    deep = "[" * DEEPER_THAN_STR + "]" * DEEPER_THAN_STR
    fields = {"epoch": "1", "litellm_key": '"sk-x"', "pep_token": '"tok"', "written_at": '"w"'}
    fields[field] = deep
    body = ", ".join(f'"{name}": {value}' for name, value in fields.items())
    path = tmp_path / "creds.json"
    path.write_text(f"{{{body}}}", encoding="utf-8")

    assert read_creds(path) is None


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_read_creds_of_a_field_with_no_text_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """The same refusal under each Python version. The parser of the test
    gives the value that `str` cannot convert."""
    fields = {"epoch": 1, "litellm_key": "sk-x", "pep_token": "tok", "written_at": "w"}
    monkeypatch.setattr(credentials_module, "read_json", lambda path: {**fields, field: NoText()})

    assert read_creds(tmp_path / "creds.json") is None


def test_read_creds_of_a_json_list_is_none(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_creds(path) is None


def test_read_creds_missing_a_field_is_none(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text('{"epoch": 1, "litellm_key": "sk-x"}', encoding="utf-8")
    assert read_creds(path) is None
