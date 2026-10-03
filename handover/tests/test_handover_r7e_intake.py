"""The secret intake (`stage7-releases.md` §4.3), `rework-release-gate.sh` section 3's rows.

The operator's decision: the intake is a ROOT-OWNED endpoint on the LAN address.
So every test here is about what root refuses, not about what a convenient
service would allow.

Five rules from §4.3, and the case that proves each.

1. The value travels in a POST body. Never in a URL, never in argv, never
   in a log line, never in the journal, never in the ledger.
2. A token fills a GAP. It can only write a name that has no value.
3. A token is 32 random bytes, bound to ONE name, single use, 15 minutes.
4. **No read verb exists anywhere.**
5. The plaintext reaches the encryptor on its stdin and nowhere else.

Nothing here runs as root and nothing binds a real socket. The store writes
into `tmp_path` and the encryptor is a fake that records what it was handed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

import pytest
from handover.intake.service import (
    FORM_PATH,
    MAX_BODY_BYTES,
    STORE_PATH,
    Intake,
    Reply,
)
from handover.intake.store import (
    SECRET_FILE_MODE,
    SecretStore,
    recipients_of,
)
from handover.intake.token import TOKEN_BYTES, TOKEN_TTL_S, Tokens

#: The store refuses a directory it does not own. In
#: production that number is 0. Here it is this test's own uid, which runs
#: the same check with a different number.
OWNER: Final = os.getuid()

NAME: Final = "weather_token"
SERVER: Final = "weather"
VALUE: Final = "a-real-looking-upstream-credential"
NOW: Final = 1_758_153_590.0

#: Two age recipients, the shape `.sops.yaml` holds.
RECIPIENTS: Final = (
    "age1dnkv7u39u3kfys05062g0lyl6s23ks0ualmjtk7alp3fw5r0s3cs4dcpuf",
    "age1hun3vrpmkr0zph9k3vsc4eu2gd7hjvffeee0qwxj2w0qsd7qe5usvg4cx6",
)


class _Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


class _Sealer:
    """A fake encryptor. It records every plaintext it is handed, which is
    what lets a test assert the value went to stdin and nowhere else."""

    def __init__(self, *, works: bool = True) -> None:
        self.handed: list[bytes] = []
        self.works = works

    def __call__(self, plaintext: bytes) -> bytes | None:
        self.handed.append(plaintext)
        if not self.works:
            return None

        return b"-----BEGIN AGE ENCRYPTED FILE-----\nsealed\n"


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()

    return directory


def _intake(secrets_dir: Path, clock: _Clock, sealer: _Sealer) -> Intake:
    return Intake(
        tokens=Tokens(clock.now),
        store=SecretStore(secrets_dir, sealer, owner_uid=OWNER),
    )


# -- the token (§4.3 step 1) ---------------------------------------------


def test_a_token_is_thirty_two_random_bytes() -> None:
    clock = _Clock()
    tokens = Tokens(clock.now)
    minted = tokens.mint(SERVER, NAME)

    assert len(minted.encode("ascii")) >= TOKEN_BYTES


def test_two_mints_never_answer_the_same_token() -> None:
    clock = _Clock()
    tokens = Tokens(clock.now)

    assert tokens.mint(SERVER, NAME) != tokens.mint(SERVER, NAME)


def test_a_token_is_bound_to_exactly_one_name() -> None:
    clock = _Clock()
    tokens = Tokens(clock.now)
    minted = tokens.mint(SERVER, NAME)

    assert tokens.claim(minted, "another_token") is None


def test_a_token_is_single_use() -> None:
    clock = _Clock()
    tokens = Tokens(clock.now)
    minted = tokens.mint(SERVER, NAME)

    assert tokens.claim(minted, NAME) is not None
    assert tokens.claim(minted, NAME) is None


def test_a_token_expires() -> None:
    clock = _Clock()
    tokens = Tokens(clock.now)
    minted = tokens.mint(SERVER, NAME)
    clock.advance(TOKEN_TTL_S + 1.0)

    assert tokens.claim(minted, NAME) is None


def test_an_unknown_token_claims_nothing() -> None:
    tokens = Tokens(_Clock().now)

    assert tokens.claim("x" * 43, NAME) is None


# -- the store (§4.3 steps 5 and 6, rules 2 and 4) ------------------------


def test_a_stored_secret_is_the_sealed_bytes(secrets_dir: Path) -> None:
    sealer = _Sealer()
    store = SecretStore(secrets_dir, sealer, owner_uid=OWNER)

    assert store.fill(NAME, VALUE) is True
    assert (secrets_dir / f"{NAME}.enc").read_bytes().startswith(b"-----BEGIN AGE")


def test_the_plaintext_reaches_the_sealer_and_no_file(secrets_dir: Path) -> None:
    """Rule 1 and rule 5: stdin only. No temporary plaintext on disk."""
    sealer = _Sealer()
    SecretStore(secrets_dir, sealer, owner_uid=OWNER).fill(NAME, VALUE)

    assert sealer.handed == [VALUE.encode("utf-8")]
    on_disk = b"".join(path.read_bytes() for path in secrets_dir.rglob("*") if path.is_file())
    assert VALUE.encode("utf-8") not in on_disk


def test_a_name_that_already_has_a_value_is_refused(secrets_dir: Path) -> None:
    """Rule 2: a token fills a GAP. Overwriting is `rotate`, and `rotate`
    is approval gated and is not this service."""
    store = SecretStore(secrets_dir, _Sealer(), owner_uid=OWNER)
    store.fill(NAME, VALUE)

    assert store.fill(NAME, "a second value") is False


def test_the_first_value_survives_a_refused_second(secrets_dir: Path) -> None:
    store = SecretStore(secrets_dir, _Sealer(), owner_uid=OWNER)
    store.fill(NAME, VALUE)
    before = (secrets_dir / f"{NAME}.enc").read_bytes()
    store.fill(NAME, "a second value")

    assert (secrets_dir / f"{NAME}.enc").read_bytes() == before


def test_a_failed_seal_writes_nothing(secrets_dir: Path) -> None:
    """Nothing half-applied (invariant 19), and no plaintext left behind."""
    store = SecretStore(secrets_dir, _Sealer(works=False), owner_uid=OWNER)

    assert store.fill(NAME, VALUE) is False
    assert list(secrets_dir.iterdir()) == []


def test_a_stored_secret_is_readable_by_its_owner_alone(secrets_dir: Path) -> None:
    SecretStore(secrets_dir, _Sealer(), owner_uid=OWNER).fill(NAME, VALUE)
    mode = (secrets_dir / f"{NAME}.enc").stat().st_mode & 0o777

    assert mode == SECRET_FILE_MODE


def test_the_store_has_no_read_verb() -> None:
    """Rule 4, asserted directly: no read verb exists anywhere."""
    forbidden = {"read", "get", "value", "decrypt", "reveal", "show"}
    named = {name for name in dir(SecretStore) if not name.startswith("_")}

    assert named & forbidden == set()


def test_a_name_outside_the_pattern_is_refused(secrets_dir: Path) -> None:
    """The name becomes a file name, so it is checked before it is used."""
    store = SecretStore(secrets_dir, _Sealer(), owner_uid=OWNER)

    assert store.fill("../../etc/shadow", VALUE) is False
    assert store.fill("WEATHER TOKEN", VALUE) is False


def test_recipients_come_from_the_sops_file(tmp_path: Path) -> None:
    """Encrypt-only: public keys, so the host writes what it cannot read."""
    sops = tmp_path / ".sops.yaml"
    sops.write_text(
        "creation_rules:\n"
        "  - path_regex: pep/secrets\\.enc\\.yaml$\n"
        f"    age: {','.join(RECIPIENTS)}\n",
        "utf-8",
    )

    assert recipients_of(sops, "pep/secrets.enc.yaml", owner_uid=OWNER) == RECIPIENTS


def test_a_sops_file_with_no_matching_rule_yields_no_recipient(tmp_path: Path) -> None:
    sops = tmp_path / ".sops.yaml"
    sops.write_text("creation_rules: []\n", "utf-8")

    assert recipients_of(sops, "pep/secrets.enc.yaml") == ()


# -- the service (§4.3 steps 3, 4 and 6) ---------------------------------


def test_the_form_names_the_server_and_the_secret(secrets_dir: Path) -> None:
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    reply = intake.form(minted)

    assert reply.status == 200
    assert SERVER in reply.body.decode("utf-8")
    assert NAME in reply.body.decode("utf-8")


def test_the_form_refuses_an_unknown_token(secrets_dir: Path) -> None:
    intake = _intake(secrets_dir, _Clock(), _Sealer())
    reply = intake.form("x" * 43)

    assert reply.status == 404


def test_reading_the_form_does_not_spend_the_token(secrets_dir: Path) -> None:
    """A page the operator reloads must still accept a paste."""
    intake = _intake(secrets_dir, _Clock(), _Sealer())
    minted = intake.tokens.mint(SERVER, NAME)
    intake.form(minted)

    assert intake.store_value(_body(minted, NAME, VALUE)).status == 200


def test_a_pasted_value_is_stored_once(secrets_dir: Path) -> None:
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    first = intake.store_value(_body(minted, NAME, VALUE))
    second = intake.store_value(_body(minted, NAME, VALUE))

    assert first.status == 200
    assert second.status == 403
    assert sealer.handed == [VALUE.encode("utf-8")]


def test_a_token_for_another_name_stores_nothing(secrets_dir: Path) -> None:
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    reply = intake.store_value(_body(minted, "other_token", VALUE))

    assert reply.status == 403
    assert sealer.handed == []


def test_an_expired_token_stores_nothing(secrets_dir: Path) -> None:
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    clock.advance(TOKEN_TTL_S + 1.0)

    assert intake.store_value(_body(minted, NAME, VALUE)).status == 403
    assert sealer.handed == []


def test_a_body_past_the_cap_is_refused(secrets_dir: Path) -> None:
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    reply = intake.store_value(_body(minted, NAME, "x" * (MAX_BODY_BYTES + 1)))

    assert reply.status == 413
    assert sealer.handed == []


def test_an_empty_value_is_refused(secrets_dir: Path) -> None:
    intake = _intake(secrets_dir, _Clock(), _Sealer())
    minted = intake.tokens.mint(SERVER, NAME)

    assert intake.store_value(_body(minted, NAME, "")).status == 400


def test_a_body_in_another_shape_is_refused(secrets_dir: Path) -> None:
    """One encoding, one parser, one meaning. JSON was what the page's old
    inline handler sent, and with the handler gone it is not read."""
    intake = _intake(secrets_dir, _Clock(), _Sealer())

    assert intake.store_value(b'{"token": "x", "name": "a_b", "value": "v"}').status == 400
    assert intake.store_value(b"value=hunter2&token=x").status == 400
    assert intake.store_value(b"").status == 400


def test_a_body_with_a_repeated_field_is_refused(secrets_dir: Path) -> None:
    """Two values for one field is two requests in one body. Taking the
    first or the last would be a second reading of the same bytes."""
    intake = _intake(secrets_dir, _Clock(), _Sealer())
    minted = intake.tokens.mint(SERVER, NAME)

    assert intake.store_value(_body(minted, NAME, VALUE) + b"&value=other").status == 400


# -- invariant 13: the value appears nowhere ------------------------------


def test_the_value_reaches_no_log_line(secrets_dir: Path) -> None:
    """The gate's own row: no log, no journal, no ledger."""
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    intake.store_value(_body(minted, NAME, VALUE))
    intake.store_value(_body(minted, NAME, VALUE))
    intake.form("x" * 43)
    written = "\n".join(intake.log)

    assert VALUE not in written
    assert NAME in written


def test_the_token_reaches_no_log_line(secrets_dir: Path) -> None:
    """A token is a capability. A log that holds one hands it on."""
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    intake.form(minted)
    intake.store_value(_body(minted, NAME, VALUE))

    assert minted not in "\n".join(intake.log)


def test_an_answer_never_echoes_the_value(secrets_dir: Path) -> None:
    clock, sealer = _Clock(), _Sealer()
    intake = _intake(secrets_dir, clock, sealer)
    minted = intake.tokens.mint(SERVER, NAME)
    reply = intake.store_value(_body(minted, NAME, VALUE))

    assert VALUE.encode("utf-8") not in reply.body


def test_the_value_never_rides_a_url() -> None:
    """Rule 1. The form posts, and the store path carries no id at all."""
    assert "{" not in STORE_PATH
    assert FORM_PATH.endswith("/")


def _body(token: str, name: str, value: str) -> bytes:
    """What a scriptless form posts: the page has no inline submit handler
    and builds no JSON, so this is the one encoding `store_value` reads."""
    return urlencode({"token": token, "name": name, "value": value}).encode("utf-8")


def _unused(reply: Reply) -> None:
    del reply
