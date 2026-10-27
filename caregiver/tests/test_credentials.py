"""The family token and `creds.json` (contract 04 section 2, contract 03
section 12)."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import caregiver.credentials as credentials_module
import pytest
from agent_family import Index
from caregiver.credentials import (
    TOKEN_BYTES,
    Credentials,
    mint_token,
    read_creds,
    token_sha256,
    write_creds,
)
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.steps import check_policy, ensure_credentials, land_policy
from caregiver_helpers import (
    DEEPER_THAN_STR,
    OLD_TOKEN,
    UNREADABLE_JSON,
    NoText,
    accepted_digests,
    chat_family,
    fake_policy,
)

from caregiver import paths

#: The fields that `read_creds` converts with `str`.
TEXT_FIELDS = ("litellm_key", "pep_token", "written_at")


#: A token of the old form: 32 bytes as base32 with no padding.
BASE32_TOKEN = "AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA5DYPQ"

#: The SHA-256 of `OLD_TOKEN`, as `chaperone token new` writes it into
#: `policy.toml`: 64 lower-case hex characters of the digest of the text.
OLD_TOKEN_DIGEST = "a8ae6e6ee929abea3afcfc5258c8ccd6f85273e0d4626d26c7279f3250f77c8e"


def test_minted_token_is_64_lower_hex() -> None:
    token = mint_token()
    assert len(token) == 2 * TOKEN_BYTES
    assert set(token) <= set("0123456789abcdef")


def test_the_digest_has_the_form_of_chaperone_token_new() -> None:
    assert token_sha256(OLD_TOKEN) == OLD_TOKEN_DIGEST


@pytest.mark.parametrize(
    "token", [BASE32_TOKEN, OLD_TOKEN.upper(), OLD_TOKEN[:-1], OLD_TOKEN + "0", "tok"]
)
def test_a_token_of_another_form_reads_as_absent(tmp_path: Path, token: str) -> None:
    path = tmp_path / "creds.json"
    write_creds(path, Credentials(4, "sk-x", token, "2026-09-19T00:00:00Z"))
    assert read_creds(path) is None


def test_a_pass_mints_a_new_token_over_one_of_the_old_form(tmp_path: Path) -> None:
    family = chat_family()
    path = paths.creds_path(tmp_path, family.name)
    write_creds(path, Credentials(4, "sk-old", BASE32_TOKEN, "2026-09-19T00:00:00Z"))

    creds, _, faults = ensure_credentials(family, tmp_path, FakeLiteLLMKeys())

    assert faults == ()
    assert creds is not None
    assert creds.pep_token != BASE32_TOKEN
    assert len(creds.pep_token) == 2 * TOKEN_BYTES
    assert read_creds(path) == creds


#: A token of the form of `mint_token` that is not `OLD_TOKEN`.
NEW_TOKEN = "fedcba9876543210" * 4

#: After any clock this suite runs under.
FAR_FUTURE = "2999-01-01T00:00:00Z"


def _with_previous(path: Path, previous: str) -> None:
    """A `creds.json` with a current token of the right form and an overlap
    that has not expired."""
    write_creds(
        path,
        Credentials(4, "sk-x", NEW_TOKEN, "2026-09-19T00:00:00Z", previous, FAR_FUTURE),
    )


@pytest.mark.parametrize("previous", ["\u00e9", BASE32_TOKEN, OLD_TOKEN.upper()])
def test_a_previous_token_of_another_form_reads_as_no_overlap(
    tmp_path: Path, previous: str
) -> None:
    path = tmp_path / "creds.json"
    _with_previous(path, previous)

    creds = read_creds(path)

    assert creds == Credentials(4, "sk-x", NEW_TOKEN, "2026-09-19T00:00:00Z")


@pytest.mark.parametrize("previous", ["\u00e9", BASE32_TOKEN])
def test_the_policy_lists_no_digest_for_a_previous_token_of_another_form(
    tmp_path: Path, previous: str
) -> None:
    family = chat_family()
    no_servers = Index(kinds={}, servers={}, skills=frozenset())
    _with_previous(paths.creds_path(tmp_path, family.name), previous)
    creds = read_creds(paths.creds_path(tmp_path, family.name))
    assert creds is not None

    check_policy(family, no_servers)
    land_policy(family, no_servers, tmp_path, creds, fake_policy(tmp_path), rev="r1")

    assert accepted_digests(tmp_path) == [token_sha256(NEW_TOKEN)]


def test_a_previous_token_of_the_right_form_stays(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    _with_previous(path, OLD_TOKEN)

    creds = read_creds(path)

    assert creds is not None
    assert creds.previous_pep_token == OLD_TOKEN
    assert creds.previous_expires_at == FAR_FUTURE


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
        epoch=3, litellm_key="sk-x", pep_token=OLD_TOKEN, written_at="2026-09-19T00:00:00Z"
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
