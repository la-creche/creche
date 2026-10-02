"""`tokens.py`: reading a webhook's own token file, and comparing it in
constant time."""

from __future__ import annotations

from pathlib import Path

from agent_door_trigger.tokens import (
    MIN_WEBHOOK_TOKEN_BYTES,
    read_webhook_token,
    tokens_match,
    webhook_token_file,
)

GOOD_TOKEN = "w" * MIN_WEBHOOK_TOKEN_BYTES


def _write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def test_webhook_token_file_is_nested_by_family_and_name(tmp_path: Path) -> None:
    path = webhook_token_file(tmp_path, "scrum-lead", "deploy-notify")

    assert path == tmp_path / "scrum-lead" / "deploy-notify.token"


def test_a_good_token_file_reads_back_stripped(tmp_path: Path) -> None:
    path = tmp_path / "t.token"
    _write(path, f"  {GOOD_TOKEN}  \n")

    assert read_webhook_token(path) == GOOD_TOKEN


def test_a_missing_token_file_is_none(tmp_path: Path) -> None:
    assert read_webhook_token(tmp_path / "absent.token") is None


def test_an_empty_token_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "empty.token"
    _write(path, "")

    assert read_webhook_token(path) is None


def test_a_short_token_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "short.token"
    _write(path, "abc")

    assert read_webhook_token(path) is None


def test_a_non_utf8_token_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "binary.token"
    path.write_bytes(b"\xff\xfe" + b"x" * MIN_WEBHOOK_TOKEN_BYTES)
    path.chmod(0o600)

    assert read_webhook_token(path) is None


def test_a_group_readable_token_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "loose.token"
    _write(path, GOOD_TOKEN, mode=0o640)

    assert read_webhook_token(path) is None


def test_a_world_readable_token_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "loose.token"
    _write(path, GOOD_TOKEN, mode=0o644)

    assert read_webhook_token(path) is None


def test_an_owner_writable_token_file_is_still_fine(tmp_path: Path) -> None:
    # 0600 is contract 05 §6.4's mode; a tighter, owner-only mode with the
    # write bit too must not be refused for reasons this check does not
    # care about.
    path = tmp_path / "t.token"
    _write(path, GOOD_TOKEN, mode=0o600)

    assert read_webhook_token(path) == GOOD_TOKEN


def test_tokens_match_true_for_the_same_value() -> None:
    assert tokens_match(GOOD_TOKEN, GOOD_TOKEN)


def test_tokens_match_false_for_a_different_value() -> None:
    assert not tokens_match(GOOD_TOKEN, "w" * (MIN_WEBHOOK_TOKEN_BYTES - 1) + "x")


def test_tokens_match_false_for_different_lengths() -> None:
    assert not tokens_match(GOOD_TOKEN, GOOD_TOKEN + "extra")
