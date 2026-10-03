"""`rotate` (contract 05 §6.3).

The sequence is the whole test. A key rotation that deleted the new key,
or minted before it braked a leaked one, would look identical from the
outside and be worth nothing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_family import FamilyFile, Index
from caregiver.credentials import Credentials, read_creds, token_sha256, write_creds
from caregiver.litellm_keys import FakeLiteLLMKeys, LiteLLMError, Spend, key_alias
from caregiver.rotate import (
    Mode,
    Reason,
    RotateError,
    RotateRequest,
    Scope,
    rotate,
    settle,
)
from caregiver.status import RotationState
from caregiver_helpers import chat_family

from caregiver import paths

FAMILY: str = "chat"
OLD_TOKEN: str = "OLDTOKEN"


class Watching(FakeLiteLLMKeys):
    """Records the order of every call, and what the budget was set to."""

    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []
        self.budget_pushed: list[float] = []

    def read_spend(self, key: str) -> Spend:
        self.order.append("read_spend")
        return Spend(spend_usd=4.25, budget_usd=15.0)

    def update_key(self, key: str, models: list[str], budget_usd_per_day: float) -> None:
        self.order.append("update_key")
        self.budget_pushed.append(budget_usd_per_day)
        super().update_key(key, models, budget_usd_per_day)

    def delete_key(self, family: str) -> None:
        self.order.append("delete_key")
        super().delete_key(family)

    def rotate_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        self.order.append("rotate_key")
        return super().rotate_key(family, models, budget_usd_per_day)


class RefusingMint(FakeLiteLLMKeys):
    def rotate_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        raise LiteLLMError("key mint failed: HTTP 503")


@pytest.fixture
def family() -> FamilyFile:
    return chat_family()


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def litellm() -> Watching:
    return Watching()


def start(state_root: Path, litellm: FakeLiteLLMKeys) -> Credentials:
    """One family already up: a minted key, a token, epoch 1."""
    key = litellm.ensure_key(FAMILY, ["agent-router"], 15.0)
    creds = Credentials(
        epoch=1, litellm_key=key, pep_token=OLD_TOKEN, written_at="2026-09-19T10:00:00Z"
    )
    write_creds(paths.creds_path(state_root, FAMILY), creds)
    return creds


def grant_digests(state_root: Path) -> list[str]:
    body = json.loads(paths.grant_path(state_root, FAMILY).read_text(encoding="utf-8"))
    return list(body["token_sha256"])


def turn(state_root: Path, family: FamilyFile, litellm: FakeLiteLLMKeys, **fields: object):  # type: ignore[no-untyped-def]
    request = RotateRequest(**fields)  # type: ignore[arg-type]
    return rotate(request, family, Index(), state_root=state_root, litellm=litellm)


# --- the epoch ----------------------------------------------------------------------


def test_a_rotation_raises_the_epoch(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    start(state_root, litellm)
    assert turn(state_root, family, litellm).epoch == 2


def test_a_family_with_no_credentials_has_nothing_to_rotate(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    with pytest.raises(RotateError):
        turn(state_root, family, litellm)


# --- the key half -------------------------------------------------------------------


def test_the_key_moves(state_root: Path, family: FamilyFile, litellm: Watching) -> None:
    before = start(state_root, litellm)
    turn(state_root, family, litellm)
    after = read_creds(paths.creds_path(state_root, FAMILY))
    assert after is not None
    assert after.litellm_key != before.litellm_key


def test_the_old_key_is_deleted_before_the_new_one_is_minted(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    """One alias, one key. Minting first would put a second key under the
    alias, and `/key/delete` takes an ALIAS — so the delete that followed
    could take the NEW key."""
    start(state_root, litellm)
    turn(state_root, family, litellm)
    assert litellm.order == ["delete_key", "rotate_key"]


def test_a_suspicion_brakes_the_old_key_before_deleting_it(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    """A budget drop refuses the next request at once; a deleted key is
    still served by some worker for up to 10 s (contract 05 §6.3)."""
    start(state_root, litellm)
    turn(state_root, family, litellm, reason=Reason.SUSPICION)
    assert litellm.order == ["read_spend", "update_key", "delete_key", "rotate_key"]
    assert litellm.budget_pushed == [4.25]


def test_a_brake_that_fails_does_not_stop_the_rotation(
    state_root: Path, family: FamilyFile
) -> None:
    """A suspicion that cannot brake must still rotate. The brake is an
    accelerator for the delete, never a condition of it."""

    class BlindSpend(Watching):
        def read_spend(self, key: str) -> Spend:
            raise LiteLLMError("key info failed: HTTP 503")

    litellm = BlindSpend()
    start(state_root, litellm)
    outcome = turn(state_root, family, litellm, reason=Reason.SUSPICION)
    assert outcome.key_rotated is True
    assert litellm.order == ["delete_key", "rotate_key"]


def test_a_refused_mint_leaves_the_old_epoch_on_disk(state_root: Path, family: FamilyFile) -> None:
    """A caller that wrote `creds.json` here would publish a key that does
    not exist."""
    litellm = RefusingMint()
    before = start(state_root, litellm)
    with pytest.raises(RotateError):
        turn(state_root, family, litellm)

    after = read_creds(paths.creds_path(state_root, FAMILY))
    assert after is not None
    assert after.epoch == before.epoch
    assert after.litellm_key == before.litellm_key


def test_the_alias_never_holds_two_keys(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    start(state_root, litellm)
    turn(state_root, family, litellm)
    assert list(litellm.keys) == [key_alias(FAMILY)]


# --- the token half ------------------------------------------------------------------


def test_a_graceful_token_rotation_leaves_both_digests_accepted(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    """Contract 04 §2.2: the grant file lists every accepted digest, so a
    turn that started under the old epoch finishes."""
    start(state_root, litellm)
    outcome = turn(state_root, family, litellm)
    digests = grant_digests(state_root)
    assert token_sha256(OLD_TOKEN) in digests
    assert len(digests) == 2
    assert outcome.state is RotationState.ROTATING


def test_an_immediate_rotation_accepts_only_the_new_digest(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    start(state_root, litellm)
    outcome = turn(state_root, family, litellm, mode=Mode.IMMEDIATE)
    assert grant_digests(state_root) == [token_sha256(_token_of(state_root))]
    assert outcome.state is RotationState.SETTLED


def test_the_overlap_ends_when_its_grace_runs_out(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    start(state_root, litellm)
    rotate(
        RotateRequest(),
        family,
        Index(),
        state_root=state_root,
        litellm=litellm,
        grace_s=-1,
    )
    assert settle(family, Index(), state_root=state_root) is True
    assert grant_digests(state_root) == [token_sha256(_token_of(state_root))]


def test_an_overlap_that_is_still_open_is_left_alone(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    start(state_root, litellm)
    turn(state_root, family, litellm)
    assert settle(family, Index(), state_root=state_root) is False
    assert len(grant_digests(state_root)) == 2


def test_settling_a_family_that_never_rotated_does_nothing(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    start(state_root, litellm)
    assert settle(family, Index(), state_root=state_root) is False


# --- scope ----------------------------------------------------------------------------


def test_a_key_only_rotation_keeps_the_token(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    before = start(state_root, litellm)
    outcome = turn(state_root, family, litellm, scope=Scope.KEY)
    after = read_creds(paths.creds_path(state_root, FAMILY))
    assert after is not None
    assert after.pep_token == before.pep_token
    assert outcome.token_rotated is False


def test_a_token_only_rotation_keeps_the_key(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    before = start(state_root, litellm)
    outcome = turn(state_root, family, litellm, scope=Scope.TOKEN)
    after = read_creds(paths.creds_path(state_root, FAMILY))
    assert after is not None
    assert after.litellm_key == before.litellm_key
    assert outcome.key_rotated is False
    assert litellm.order == []


def test_a_graceful_key_rotation_says_it_was_not_graceful(
    state_root: Path, family: FamilyFile, litellm: Watching
) -> None:
    """Honesty in the outcome, not only in a docstring."""
    start(state_root, litellm)
    assert "no overlap" in turn(state_root, family, litellm).note


def _token_of(state_root: Path) -> str:
    creds = read_creds(paths.creds_path(state_root, FAMILY))
    assert creds is not None
    return creds.pep_token
