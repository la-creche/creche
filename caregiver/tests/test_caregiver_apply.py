"""apply_once: the full sequence on a fresh family, idempotency on a
second run, and what stops early on an invalid file or an operational
failure."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from agent_family import FamilyState
from caregiver.apply import ApplyResult, FamilyNotFoundError, apply_once
from caregiver.credentials import read_creds, write_creds
from caregiver.litellm_keys import FakeLiteLLMKeys, LiteLLMError, key_alias
from caregiver.reach_files import EGRESS_CAP
from caregiver.status import now_rfc3339
from caregiver_helpers import (
    FENCES,
    UNREADABLE_JSON,
    accepted_digests,
    current_digest,
    fake_policy,
    write_no_file_dir,
    write_registry,
)

from caregiver import paths


@pytest.fixture
def registry_root(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    write_registry(root)
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def apply_chat(
    registry_root: Path,
    state_root: Path,
    *,
    litellm: FakeLiteLLMKeys | None = None,
) -> ApplyResult:
    return apply_once(
        registry_root,
        "chat",
        state_root=state_root,
        fences=FENCES,
        policy=fake_policy(state_root),
        litellm=litellm if litellm is not None else FakeLiteLLMKeys(),
    )


# --- a fresh valid family ------------------------------------------------


def test_a_fresh_valid_family_reaches_in_sync(registry_root: Path, state_root: Path) -> None:
    result = apply_chat(registry_root, state_root)
    assert result.ok is True
    assert result.status.state is FamilyState.IN_SYNC
    assert result.status.faults == ()


def test_it_mints_a_litellm_key(registry_root: Path, state_root: Path) -> None:
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, litellm=litellm)
    assert litellm.minted == 1
    assert key_alias("chat") in litellm.keys


def test_it_writes_creds_json(registry_root: Path, state_root: Path) -> None:
    apply_chat(registry_root, state_root)
    creds = read_creds(paths.creds_path(state_root, "chat"))
    assert creds is not None
    assert creds.epoch == 1


def test_it_writes_the_table_of_policy_toml(registry_root: Path, state_root: Path) -> None:
    apply_chat(registry_root, state_root)
    assert accepted_digests(state_root) == [current_digest(state_root)]


def test_it_writes_the_config_mount(registry_root: Path, state_root: Path) -> None:
    apply_chat(registry_root, state_root)
    config = paths.config_dir(state_root, "chat")
    assert (config / "instructions.md").read_text(encoding="utf-8") == "Be helpful.\n"


def test_apply_once_writes_the_egress_file(tmp_path: Path, state_root: Path) -> None:
    """Contract 01 §6.1.1, decision CH69 rule 6."""
    registry_root = write_registry(tmp_path / "registry", egress=["GitHub.com"])
    apply_chat(registry_root, state_root)
    body = json.loads(paths.egress_path(state_root, "chat").read_bytes())
    assert body == {"hosts": [{"host": "github.com", "port": 443}]}


def test_apply_once_writes_the_repos_file(tmp_path: Path, state_root: Path) -> None:
    """Contract 01 §6.1.1, decisions CH69 rules 4 and 6."""
    registry_root = write_registry(tmp_path / "registry", repos=["mcp", "creche"])
    apply_chat(registry_root, state_root)
    body = json.loads(paths.repos_path(state_root, "chat").read_bytes())
    assert body == {"repos": ["creche", "mcp"]}


def test_an_egress_list_over_the_cap_stops_the_apply(registry_root: Path, state_root: Path) -> None:
    """Decision CH71: the refusal comes before the key step, so no key is
    minted and the last good file stays byte for byte. The one error names
    the cap."""
    write_registry(registry_root, egress=["github.com"])
    good = apply_chat(registry_root, state_root)
    egress_path = paths.egress_path(state_root, "chat")
    before = egress_path.read_bytes()

    write_registry(registry_root, egress=[f"h{index:05}.example.com" for index in range(1000)])
    litellm = FakeLiteLLMKeys()
    bad = apply_chat(registry_root, state_root, litellm=litellm)

    assert bad.ok is False
    assert bad.status.state is FamilyState.INVALID
    assert bad.status.applied_rev == good.status.applied_rev
    assert bad.status.validation.error_count == 1
    first_error = bad.status.validation.first_error
    assert first_error is not None
    assert first_error.endswith(f"the cap is {EGRESS_CAP}")
    assert egress_path.read_bytes() == before
    assert litellm.minted == 0


# --- supervisor.env (contract 03 §7.1) ---------------------------------------


# --- idempotency -----------------------------------------------------------


def test_a_second_apply_does_not_remint_the_key(registry_root: Path, state_root: Path) -> None:
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, litellm=litellm)
    apply_chat(registry_root, state_root, litellm=litellm)
    assert litellm.minted == 1


def test_a_second_apply_still_reaches_in_sync(registry_root: Path, state_root: Path) -> None:
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, litellm=litellm)
    result = apply_chat(registry_root, state_root, litellm=litellm)
    assert result.ok is True
    assert result.status.state is FamilyState.IN_SYNC


# --- a creds.json that does not read ---------------------------------------

#: Content of `creds.json` that `read_creds` refuses.
NO_CREDS: dict[str, bytes] = {
    **UNREADABLE_JSON,
    "empty": b"",
    "not-json": b"{not json",
    "no-epoch": b'{"litellm_key": "sk-x", "pep_token": "tok", "written_at": "w"}',
}

REPLACES_CREDS = (
    "chat: creds.json does not read. The pass replaces it with a new key, a new token and epoch 1"
)


@pytest.mark.parametrize("raw", NO_CREDS.values(), ids=NO_CREDS.keys())
def test_an_apply_over_creds_that_do_not_read_says_so(
    registry_root: Path, state_root: Path, caplog: pytest.LogCaptureFixture, raw: bytes
) -> None:
    """A `creds.json` that does not read is taken as an absent file. The
    pass mints again and the epoch starts again at 1, so the pass says it,
    one time."""
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, litellm=litellm)
    paths.creds_path(state_root, "chat").write_bytes(raw)

    with caplog.at_level(logging.WARNING, logger="caregiver.steps"):
        apply_chat(registry_root, state_root, litellm=litellm)
        apply_chat(registry_root, state_root, litellm=litellm)

    assert litellm.minted == 2
    assert [(one.levelno, one.getMessage()) for one in caplog.records] == [
        (logging.ERROR, REPLACES_CREDS)
    ]


def test_an_apply_over_creds_that_read_says_nothing(
    registry_root: Path, state_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The first apply finds no file. The second finds one that reads."""
    with caplog.at_level(logging.WARNING, logger="caregiver.steps"):
        apply_chat(registry_root, state_root)
        apply_chat(registry_root, state_root)

    assert caplog.records == []


def test_a_failed_mint_over_creds_that_do_not_read_keeps_the_file(
    registry_root: Path, state_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No write, so nothing to say: the file is as it was."""
    apply_chat(registry_root, state_root)
    creds_path = paths.creds_path(state_root, "chat")
    creds_path.write_bytes(NO_CREDS["not-json"])

    with caplog.at_level(logging.WARNING, logger="caregiver.steps"):
        apply_chat(registry_root, state_root, litellm=FailingLiteLLM())

    assert creds_path.read_bytes() == NO_CREDS["not-json"]
    assert caplog.records == []


# --- family not in the registry ----------------------------------------


def test_family_not_in_the_registry_raises(registry_root: Path, state_root: Path) -> None:
    with pytest.raises(FamilyNotFoundError):
        apply_once(
            registry_root,
            "no-such-family",
            state_root=state_root,
            fences=FENCES,
            policy=fake_policy(state_root),
            litellm=FakeLiteLLMKeys(),
        )


def test_a_directory_with_no_family_file_raises(registry_root: Path, state_root: Path) -> None:
    """Contract 01 §5.6 rule 3: the directory holds no family. The call
    writes no state for that name."""
    write_no_file_dir(registry_root)

    with pytest.raises(FamilyNotFoundError):
        apply_once(
            registry_root,
            "stray",
            state_root=state_root,
            fences=FENCES,
            policy=fake_policy(state_root),
            litellm=FakeLiteLLMKeys(),
        )

    assert not paths.family_dir(state_root, "stray").exists()


# --- an invalid family (invariant 19) ---------------------------------------


def test_an_invalid_family_stops_before_any_credential_work(
    registry_root: Path, state_root: Path
) -> None:
    write_registry(registry_root, kind="not-a-real-kind")
    litellm = FakeLiteLLMKeys()
    result = apply_chat(registry_root, state_root, litellm=litellm)
    assert result.ok is False
    assert result.status.state is FamilyState.INVALID
    assert litellm.minted == 0


def test_an_invalid_family_still_writes_the_validation_report(
    registry_root: Path, state_root: Path
) -> None:
    write_registry(registry_root, kind="not-a-real-kind")
    apply_chat(registry_root, state_root)
    assert paths.validation_path(state_root, "chat").is_file()


def test_a_family_thats_never_validated_reports_never_valid(
    registry_root: Path, state_root: Path
) -> None:
    write_registry(registry_root, kind="not-a-real-kind")
    result = apply_chat(registry_root, state_root)
    assert result.status.validation.never_valid is True


def test_an_invalid_revision_keeps_the_last_good_applied_rev(
    registry_root: Path, state_root: Path
) -> None:
    good = apply_chat(registry_root, state_root)
    assert good.ok is True

    write_registry(registry_root, kind="not-a-real-kind")
    bad = apply_chat(registry_root, state_root)

    assert bad.status.state is FamilyState.INVALID
    assert bad.status.applied_rev == good.status.applied_rev
    assert bad.status.validation.never_valid is False


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_an_invalid_revision_over_a_document_that_does_not_read(
    registry_root: Path, state_root: Path, raw: bytes
) -> None:
    """The apply reads the last document for the revision it keeps. A
    document that does not read gives no revision, and the apply still
    publishes its report."""
    apply_chat(registry_root, state_root)
    paths.status_path(state_root, "chat").write_bytes(raw)

    write_registry(registry_root, kind="not-a-real-kind")
    bad = apply_chat(registry_root, state_root)

    assert bad.status.state is FamilyState.INVALID
    assert bad.status.applied_rev == ""


def test_an_invalid_revision_still_publishes_the_epoch(
    registry_root: Path, state_root: Path
) -> None:
    """A rotation raises the epoch in `creds.json` whatever the family file
    says, and the status document is where `attendance` reads it."""
    apply_chat(registry_root, state_root)
    creds_path = paths.creds_path(state_root, "chat")
    creds = read_creds(creds_path)
    assert creds is not None
    write_creds(creds_path, replace(creds, epoch=creds.epoch + 1))

    write_registry(registry_root, kind="not-a-real-kind")
    bad = apply_chat(registry_root, state_root)

    assert bad.status.state is FamilyState.INVALID
    assert bad.status.credentials is not None
    assert bad.status.credentials.epoch == creds.epoch + 1


def test_a_family_thats_never_validated_publishes_no_credentials(
    registry_root: Path, state_root: Path
) -> None:
    write_registry(registry_root, kind="not-a-real-kind")
    result = apply_chat(registry_root, state_root)
    assert result.status.credentials is None


def test_an_invalid_revision_does_not_touch_the_existing_fragment(
    registry_root: Path, state_root: Path
) -> None:
    apply_chat(registry_root, state_root)
    before = paths.policy_fragment_path(state_root, "chat").read_text(encoding="utf-8")

    write_registry(registry_root, kind="not-a-real-kind")
    apply_chat(registry_root, state_root)

    after = paths.policy_fragment_path(state_root, "chat").read_text(encoding="utf-8")
    assert before == after


# --- operational failures (caregiver-detected faults) -------------------------


class FailingLiteLLM(FakeLiteLLMKeys):
    def ensure_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        raise LiteLLMError("simulated LiteLLM outage")


def test_a_litellm_failure_degrades_and_raises_no_exception(
    registry_root: Path, state_root: Path
) -> None:
    result = apply_chat(registry_root, state_root, litellm=FailingLiteLLM())
    assert result.ok is False
    assert result.status.state is FamilyState.DEGRADED
    assert result.status.faults[0].code == "key_mint_failed"
    assert result.status.faults[0].blocks_turns is True


def test_a_litellm_failure_writes_no_fragment(registry_root: Path, state_root: Path) -> None:
    apply_chat(registry_root, state_root, litellm=FailingLiteLLM())
    assert not paths.policy_fragment_path(state_root, "chat").exists()


# --- folded faults (contract 05 section 3.3.1) -------------------------------


def write_pep_fault(state_root: Path, family: str, code: str, since: str | None = None) -> None:
    fault_path = paths.fault_path(state_root, "pep", family)
    fault_path.parent.mkdir(parents=True, exist_ok=True)
    fault_path.write_text(
        json.dumps(
            {
                "family": family,
                "written_at": now_rfc3339(),
                "faults": [{"code": code, "since": since or now_rfc3339(), "source": "pep"}],
            }
        ),
        encoding="utf-8",
    )


def test_a_folded_pep_fault_marks_an_otherwise_healthy_family_degraded(
    registry_root: Path, state_root: Path
) -> None:
    """A fault file of the PEP folds into the document (contract 05
    section 3.3.1)."""
    apply_chat(registry_root, state_root)
    write_pep_fault(state_root, "chat", "grants_stale")

    result = apply_chat(registry_root, state_root)

    assert result.status.state is FamilyState.DEGRADED
    assert result.status.faults[0].code == "grants_stale"
    assert result.status.faults[0].source == "pep"


def test_a_folded_fault_still_lets_the_credentials_and_the_files_apply(
    registry_root: Path, state_root: Path
) -> None:
    write_pep_fault(state_root, "chat", "grants_stale")
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, litellm=litellm)
    assert litellm.minted == 1
    assert paths.egress_path(state_root, "chat").exists()
    assert paths.repos_path(state_root, "chat").exists()
