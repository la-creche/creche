"""The one page, and what happens after the paste.

Two rules this file pins down.

1. **The page carries no script**, so the policy can say `script-src
   'none'` and an injected `<script>` stops at the policy rather than at the
   escaping. The form posts by itself, in the encoding a form sends.
2. **A stored value closes its gap** (§4.3 step 6). The gap file lives in
   an operator-written directory and `caregiver` re-opens a gap whose name has
   no value, so the close has to come after the value lands.

Nothing here runs as root: `owner_uid` is this test's own uid.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

import pytest
from handover.intake.service import (
    CONTENT_SECURITY_POLICY,
    FORM_CONTENT_TYPE,
    MAX_BODY_BYTES,
    STORE_PATH,
    Intake,
)
from handover.intake.store import MAX_SECRET_CHARS, SealFn, SecretStore
from handover.intake.token import Tokens

SERVER: Final = "weather"
NAME: Final = "weather_token"
VALUE: Final = "a-real-looking-upstream-credential"
SEALED: Final = b"-----BEGIN AGE ENCRYPTED FILE-----\nsealed\n"


def _seal(plaintext: bytes) -> bytes | None:
    del plaintext

    return SEALED


def _refuse_to_seal(plaintext: bytes) -> bytes | None:
    del plaintext

    return None


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)

    return directory


class _Closed:
    """Records the names the intake asked to close."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(self, name: str) -> bool:
        self.names.append(name)

        return True


def _body(token: str, name: str, value: str) -> bytes:
    return urlencode({"token": token, "name": name, "value": value}).encode("utf-8")


# -- the page ------------------------------------------------------------


def test_the_page_carries_no_script_at_all() -> None:
    from handover.intake.service import _page

    page = _page(SERVER, NAME, "a-token").decode("utf-8")

    assert "<script" not in page.lower()
    assert "onsubmit" not in page.lower()
    assert "fetch(" not in page


def test_the_policy_allows_no_script() -> None:
    """A policy with `'unsafe-inline'` does not stop an injected script
    from reading the token out of the page."""
    assert "script-src 'none'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in CONTENT_SECURITY_POLICY


def test_the_form_posts_in_the_one_encoding_that_is_read() -> None:
    from handover.intake.service import _page

    page = _page(SERVER, NAME, "a-token").decode("utf-8")

    assert f'method="post" action="{STORE_PATH}"' in page
    assert f'enctype="{FORM_CONTENT_TYPE}"' in page


def test_the_page_names_the_server_and_the_secret() -> None:
    from handover.intake.service import _page

    page = _page(SERVER, NAME, "a-token").decode("utf-8")

    assert SERVER in page
    assert NAME in page


def test_the_body_cap_leaves_room_for_percent_encoding(secrets_dir: Path) -> None:
    """A value of exactly `MAX_SECRET_CHARS` may triple in a form body, so
    a cap of `MAX_SECRET_CHARS + 1024` refused the longest legal paste
    before anything read it."""
    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)
    intake = Intake(tokens=tokens, store=_store(secrets_dir, _seal))
    longest = _body(minted, NAME, "%" * MAX_SECRET_CHARS)

    assert len(longest) <= MAX_BODY_BYTES
    assert intake.store_value(longest).status == 200


# -- after the paste -----------------------------------------------------


def _store(directory: Path, seal: SealFn) -> SecretStore:
    return SecretStore(directory, seal, owner_uid=os.getuid())


def test_a_stored_value_closes_its_gap(secrets_dir: Path) -> None:
    closed = _Closed()
    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)
    intake = Intake(tokens=tokens, store=_store(secrets_dir, _seal), close_gap=closed)

    assert intake.store_value(_body(minted, NAME, VALUE)).status == 200
    assert closed.names == [NAME]


def test_a_refused_paste_leaves_its_gap_open(secrets_dir: Path) -> None:
    """A gap closed on a failure is a gap nobody re-opens, and the secret
    is then missing for ever with nothing said."""
    closed = _Closed()
    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)
    intake = Intake(tokens=tokens, store=_store(secrets_dir, _refuse_to_seal), close_gap=closed)

    assert intake.store_value(_body(minted, NAME, VALUE)).status == 500
    assert closed.names == []


def test_a_close_that_fails_does_not_un_store_the_value(secrets_dir: Path) -> None:
    """The value is the thing that matters. A gap root could not remove
    costs one more push, never the paste the operator already made."""

    def raise_oserror(name: str) -> bool:
        raise OSError(name)

    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)
    intake = Intake(tokens=tokens, store=_store(secrets_dir, _seal), close_gap=raise_oserror)

    assert intake.store_value(_body(minted, NAME, VALUE)).status == 200
    assert (secrets_dir / f"{NAME}.enc").read_bytes() == SEALED


def test_the_close_never_carries_a_value(secrets_dir: Path) -> None:
    """§4.3 rule 4's shape: the name travels, the value does not."""
    closed = _Closed()
    tokens = Tokens(lambda: 0.0)
    minted = tokens.mint(SERVER, NAME)
    intake = Intake(tokens=tokens, store=_store(secrets_dir, _seal), close_gap=closed)
    intake.store_value(_body(minted, NAME, VALUE))

    assert VALUE not in "".join(closed.names)
