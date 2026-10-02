"""The family token and `creds.json` (contract 04 section 2, contract 03
section 12)."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from agent_managerd.credentials import (
    TOKEN_BYTES,
    Credentials,
    mint_token,
    read_creds,
    token_sha256,
    write_creds,
)


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


def test_read_creds_of_a_json_list_is_none(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_creds(path) is None


def test_read_creds_missing_a_field_is_none(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text('{"epoch": 1, "litellm_key": "sk-x"}', encoding="utf-8")
    assert read_creds(path) is None
