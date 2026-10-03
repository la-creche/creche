"""One bearer per declared webhook (contract 05 §6.4).

Invariant 16: a family file that declares a webhook trigger must work with
no hand-made file. Without a minted bearer, `door-trigger` answers 404 on
every call until an operator writes the token by hand, and it fails
silently apart from one log line.
"""

from __future__ import annotations

import base64
import stat
from pathlib import Path

from agent_family import FamilyFile
from caregiver.webhook_tokens import (
    WEBHOOK_TOKEN_BYTES,
    WEBHOOK_TOKEN_MODE,
    ensure_webhooks,
    forget_webhooks,
    mint_webhook_token,
    rotate_webhooks,
)
from caregiver_helpers import chat_family

from caregiver import paths

FAMILY = "ha-review"
HOOK = "boiler-alert"
SECOND_HOOK = "meter-read"


def declaring(*names: str) -> FamilyFile:
    """One autonomous family file with these webhook triggers."""
    triggers = [{"webhook": name} for name in names]
    return chat_family(name=FAMILY, kind="autonomous", triggers=triggers)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_minted_token_is_32_random_bytes_in_base64() -> None:
    """Item C: "32 random bytes, base64"."""
    token = mint_webhook_token()

    assert len(base64.urlsafe_b64decode(token + "==")) == WEBHOOK_TOKEN_BYTES
    assert token != mint_webhook_token()


def test_a_declared_webhook_gets_a_token_file(tmp_path: Path) -> None:
    published = ensure_webhooks(declaring(HOOK), tmp_path)

    assert [one.name for one in published] == [HOOK]
    assert published[0].path == paths.webhook_token_path(tmp_path, FAMILY, HOOK)
    assert published[0].path.is_file()


def test_the_token_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    """`door-trigger`'s `tokens.py` refuses any file with a group or other
    bit, and treats its route as unconfigured."""
    published = ensure_webhooks(declaring(HOOK), tmp_path)

    assert mode_of(published[0].path) == WEBHOOK_TOKEN_MODE
    assert mode_of(published[0].path.parent) == 0o700


def test_a_second_apply_keeps_the_token(tmp_path: Path) -> None:
    """Rotating on every reconcile pass would break Home Assistant hourly."""
    first = ensure_webhooks(declaring(HOOK), tmp_path)
    value = first[0].path.read_text(encoding="utf-8")

    ensure_webhooks(declaring(HOOK), tmp_path)

    assert first[0].path.read_text(encoding="utf-8") == value


def test_a_withdrawn_webhook_loses_its_token(tmp_path: Path) -> None:
    """The credential goes when the trigger leaves the family file."""
    first = ensure_webhooks(declaring(HOOK, SECOND_HOOK), tmp_path)
    gone = next(one for one in first if one.name == SECOND_HOOK)

    kept = ensure_webhooks(declaring(HOOK), tmp_path)

    assert [one.name for one in kept] == [HOOK]
    assert not gone.path.exists()
    assert kept[0].path.is_file()


def test_a_family_with_no_webhook_publishes_nothing(tmp_path: Path) -> None:
    assert ensure_webhooks(declaring(), tmp_path) == ()


def test_a_cron_trigger_mints_no_token(tmp_path: Path) -> None:
    """A timer fires from inside the host. It needs no bearer."""
    timed = chat_family(name=FAMILY, kind="autonomous", triggers=[{"cron": "0 6 * * *"}])

    assert ensure_webhooks(timed, tmp_path) == ()


def test_rotate_replaces_every_declared_token(tmp_path: Path) -> None:
    """Contract 05 §6.3: `scope: token` covers a family's webhook bearers."""
    before = ensure_webhooks(declaring(HOOK, SECOND_HOOK), tmp_path)
    values = [one.path.read_text(encoding="utf-8") for one in before]

    after = rotate_webhooks(declaring(HOOK, SECOND_HOOK), tmp_path)

    assert [one.name for one in after] == [HOOK, SECOND_HOOK]
    assert [one.path.read_text(encoding="utf-8") for one in after] != values
    assert all(mode_of(one.path) == WEBHOOK_TOKEN_MODE for one in after)


def test_the_published_row_carries_a_path_and_no_value(tmp_path: Path) -> None:
    """Contract 05 §6.4: The operator needs the path to paste into Home Assistant.
    The status document is 0644, so a value there would be world-readable."""
    published = ensure_webhooks(declaring(HOOK), tmp_path)
    row = published[0].as_json()

    assert row == {"name": HOOK, "token_path": str(published[0].path)}
    assert published[0].path.read_text(encoding="utf-8").strip() not in str(row)


def test_forget_removes_the_whole_family_directory(tmp_path: Path) -> None:
    """A deleted family keeps no credential (invariant 13)."""
    published = ensure_webhooks(declaring(HOOK), tmp_path)

    forget_webhooks(tmp_path, FAMILY)

    assert not published[0].path.exists()
    assert not published[0].path.parent.exists()


def test_forget_is_safe_when_nothing_was_ever_minted(tmp_path: Path) -> None:
    forget_webhooks(tmp_path, FAMILY)

    assert not paths.webhooks_family_dir(tmp_path, FAMILY).exists()
