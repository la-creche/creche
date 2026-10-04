"""apply_once: the full sequence on a fresh family, idempotency on a
second run, and what stops early on an invalid file or an operational
failure."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_family import FamilyState
from caregiver.apply import ApplyResult, FamilyNotFoundError, apply_once
from caregiver.credentials import read_creds, write_creds
from caregiver.driver import DriverError, FakeDriver, SandboxSpec
from caregiver.litellm_keys import FakeLiteLLMKeys, LiteLLMError, key_alias
from caregiver.playpen_env import read_playpen_env
from caregiver.status import now_rfc3339
from caregiver.switch import FakeSwitchClient, SwitchClient
from caregiver_helpers import UNREADABLE_JSON, write_no_file_dir, write_registry

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
    driver: FakeDriver | None = None,
    litellm: FakeLiteLLMKeys | None = None,
    switch: SwitchClient | None = None,
) -> ApplyResult:
    return apply_once(
        registry_root,
        "chat",
        state_root=state_root,
        image="sha256:deadbeef",
        driver=driver if driver is not None else FakeDriver(),
        litellm=litellm if litellm is not None else FakeLiteLLMKeys(),
        switch=switch,
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


def test_it_writes_the_grant_file(registry_root: Path, state_root: Path) -> None:
    apply_chat(registry_root, state_root)
    body = json.loads(paths.grant_path(state_root, "chat").read_text(encoding="utf-8"))
    assert body["family"] == "chat"
    assert body["model_alias"] == "agent-router"


def test_it_writes_the_config_mount(registry_root: Path, state_root: Path) -> None:
    apply_chat(registry_root, state_root)
    config = paths.config_dir(state_root, "chat")
    assert (config / "instructions.md").read_text(encoding="utf-8") == "Be helpful.\n"


def test_it_creates_the_sandbox_once(registry_root: Path, state_root: Path) -> None:
    driver = FakeDriver()
    apply_chat(registry_root, state_root, driver=driver)
    assert driver.ops() == ("create", "set_egress", "assert_egress")
    assert paths.sandbox_seq_path(state_root, "chat").read_text(encoding="utf-8").strip() == "1"


def test_the_sandbox_spec_puts_sessions_first_as_the_rw_primary(
    registry_root: Path, state_root: Path
) -> None:
    driver = FakeDriver()
    apply_chat(registry_root, state_root, driver=driver)
    spec = driver.calls[0].args[0]
    assert isinstance(spec, SandboxSpec)
    assert spec.mounts[0].path == "/srv/agents/sessions/chat"
    assert spec.mounts[0].readonly is False


def test_an_apply_with_no_switch_client_leaves_the_sandbox_creating(
    registry_root: Path, state_root: Path
) -> None:
    """No handshake can run without one, and contract 05 §4.2 rule 4 forbids
    writing `ready` after `sbx create` alone. `attendance` still dials a
    `creating` sandbox, so the family serves either way."""
    result = apply_chat(registry_root, state_root)
    assert len(result.status.sandboxes) == 1
    assert str(result.status.sandboxes[0].state) == "creating"


def test_an_apply_with_a_switch_client_reaches_ready(registry_root: Path, state_root: Path) -> None:
    """Contract 05 §4.3 steps 6 and 7. Without the handshake, `apply-once`
    leaves a serving sandbox `creating` in its status document."""
    switch = FakeSwitchClient()

    result = apply_chat(registry_root, state_root, switch=switch)

    assert [(one.outgoing, one.to) for one in switch.requests] == [(None, "chat-s1")]
    assert str(result.status.sandboxes[0].state) == "ready"
    assert result.status.sandboxes[0].ready_at is not None


def test_a_refused_handshake_keeps_the_apply_ok(registry_root: Path, state_root: Path) -> None:
    """`up` runs `apply-once` before `attendance` is listening, so a refusal
    here is the normal first pass and must not fail the verb."""
    switch = FakeSwitchClient(refuse="connection refused")

    result = apply_chat(registry_root, state_root, switch=switch)

    assert result.ok is True
    assert str(result.status.sandboxes[0].state) == "creating"
    assert result.status.faults == ()


def test_a_second_apply_promotes_what_the_first_could_not(
    registry_root: Path, state_root: Path
) -> None:
    """Every step is idempotent. `up` starts the services after the first
    apply, so the pass that follows is the one that can complete the
    handshake, and it must adopt the sandbox rather than build a second."""
    # One driver and one LiteLLM across both passes, because the host has one
    # of each: a second fake would not know the key the first pass minted.
    driver, litellm = FakeDriver(), FakeLiteLLMKeys()
    refused = FakeSwitchClient(refuse="connection refused")
    apply_chat(registry_root, state_root, driver=driver, litellm=litellm, switch=refused)

    result = apply_chat(
        registry_root, state_root, driver=driver, litellm=litellm, switch=FakeSwitchClient()
    )

    assert [one.id for one in result.status.sandboxes] == ["chat-s1"]
    assert str(result.status.sandboxes[0].state) == "ready"
    assert driver.ops().count("create") == 1


def test_every_mount_is_named_by_its_host_path(registry_root: Path, state_root: Path) -> None:
    """Contract 03 §7.1: sbx mounts a host directory at that same path
    inside the VM, so a spec carries one path per mount and no target."""
    driver = FakeDriver()
    apply_chat(registry_root, state_root, driver=driver)
    spec = driver.calls[0].args[0]
    assert isinstance(spec, SandboxSpec)
    assert [mount.path for mount in spec.mounts] == [
        "/srv/agents/sessions/chat",
        str(paths.creds_dir(state_root, "chat")),
        str(paths.config_dir(state_root, "chat")),
        str(paths.control_dir(state_root, "chat", "chat-s1")),
    ]


# --- supervisor.env (contract 03 §7.1) ---------------------------------------


def test_creating_the_sandbox_writes_playpen_env(registry_root: Path, state_root: Path) -> None:
    """`sbx exec` forwards no host environment, so this file is the only
    way the playpen learns where its three mounts are."""
    apply_chat(registry_root, state_root)
    values = read_playpen_env(paths.playpen_env_path(state_root, "chat", "chat-s1"))
    assert values["AGENT_CRED_DIR"] == str(paths.creds_dir(state_root, "chat"))
    assert values["AGENT_FAMILY_CONFIG_DIR"] == str(paths.config_dir(state_root, "chat"))
    assert values["AGENT_CONTROL_DIR"] == str(paths.control_dir(state_root, "chat", "chat-s1"))
    assert values["AGENT_SANDBOX"] == "chat-s1"


def test_the_status_document_publishes_the_env_file_path(
    registry_root: Path, state_root: Path
) -> None:
    """Contract 05 §4.1. `attendance` reads the path from here and hands it
    to `sbx exec --env-file`. A document without it is a fault, not a
    guess, so the document must carry it."""
    result = apply_chat(registry_root, state_root)
    published = result.status.sandboxes[0].playpen_env
    assert published == str(paths.playpen_env_path(state_root, "chat", "chat-s1"))
    assert Path(published).is_file()


def test_the_env_file_survives_the_control_directory_being_emptied(
    registry_root: Path, state_root: Path
) -> None:
    """Contract 05 §4.3 rule 5 empties the sandbox's control directory at
    every create. The env file sits beside it, so the emptying cannot take
    it."""
    apply_chat(registry_root, state_root)
    env_path = paths.playpen_env_path(state_root, "chat", "chat-s1")
    assert env_path.is_file()
    assert not env_path.is_relative_to(paths.control_root(state_root, "chat"))


def test_a_failed_create_writes_no_env_file(registry_root: Path, state_root: Path) -> None:
    """A file naming a sandbox that does not exist would send `attendance`
    at a VM nobody made."""
    apply_chat(registry_root, state_root, driver=FailingDriver())
    assert not paths.playpen_env_path(state_root, "chat", "chat-s1").exists()


def test_a_second_apply_keeps_the_env_file_current(registry_root: Path, state_root: Path) -> None:
    """An apply that creates nothing still republishes it: a family whose
    env file went missing would fail every turn with no way back."""
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, driver=driver, litellm=litellm)
    paths.playpen_env_path(state_root, "chat", "chat-s1").unlink()

    result = apply_chat(registry_root, state_root, driver=driver, litellm=litellm)

    assert paths.playpen_env_path(state_root, "chat", "chat-s1").is_file()
    assert result.status.sandboxes[0].playpen_env == str(
        paths.playpen_env_path(state_root, "chat", "chat-s1")
    )


# --- idempotency -----------------------------------------------------------


def test_a_second_apply_does_not_remint_the_key(registry_root: Path, state_root: Path) -> None:
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, litellm=litellm)
    apply_chat(registry_root, state_root, litellm=litellm)
    assert litellm.minted == 1


def test_a_second_apply_does_not_recreate_the_sandbox(
    registry_root: Path, state_root: Path
) -> None:
    driver = FakeDriver()
    apply_chat(registry_root, state_root, driver=driver)
    apply_chat(registry_root, state_root, driver=driver)
    assert driver.ops().count("create") == 1


def test_a_second_apply_still_reaches_in_sync(registry_root: Path, state_root: Path) -> None:
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    apply_chat(registry_root, state_root, driver=driver, litellm=litellm)
    result = apply_chat(registry_root, state_root, driver=driver, litellm=litellm)
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
            image="x",
            driver=FakeDriver(),
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
            image="x",
            driver=FakeDriver(),
            litellm=FakeLiteLLMKeys(),
        )

    assert not paths.family_dir(state_root, "stray").exists()


# --- an invalid family (invariant 19) ---------------------------------------


def test_an_invalid_family_stops_before_any_credential_or_sandbox_work(
    registry_root: Path, state_root: Path
) -> None:
    write_registry(registry_root, kind="not-a-real-kind")
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    result = apply_chat(registry_root, state_root, driver=driver, litellm=litellm)
    assert result.ok is False
    assert result.status.state is FamilyState.INVALID
    assert litellm.minted == 0
    assert driver.calls == []


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


def test_an_invalid_revision_does_not_touch_the_existing_grant_file(
    registry_root: Path, state_root: Path
) -> None:
    apply_chat(registry_root, state_root)
    before = paths.grant_path(state_root, "chat").read_text(encoding="utf-8")

    write_registry(registry_root, kind="not-a-real-kind")
    apply_chat(registry_root, state_root)

    after = paths.grant_path(state_root, "chat").read_text(encoding="utf-8")
    assert before == after


def test_an_invalid_revision_does_not_touch_the_sandbox(
    registry_root: Path, state_root: Path
) -> None:
    driver = FakeDriver()
    apply_chat(registry_root, state_root, driver=driver)
    calls_after_good_apply = len(driver.calls)

    write_registry(registry_root, kind="not-a-real-kind")
    apply_chat(registry_root, state_root, driver=driver)

    assert len(driver.calls) == calls_after_good_apply


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


def test_a_litellm_failure_writes_no_grant_file(registry_root: Path, state_root: Path) -> None:
    apply_chat(registry_root, state_root, litellm=FailingLiteLLM())
    assert not paths.grant_path(state_root, "chat").exists()


def test_a_litellm_failure_creates_no_sandbox(registry_root: Path, state_root: Path) -> None:
    driver = FakeDriver()
    apply_chat(registry_root, state_root, driver=driver, litellm=FailingLiteLLM())
    assert driver.calls == []


class FailingDriver(FakeDriver):
    def create(self, spec: SandboxSpec) -> None:
        raise DriverError("simulated sbx outage")


def test_a_sandbox_failure_degrades_but_keeps_the_credentials_and_grants(
    registry_root: Path, state_root: Path
) -> None:
    litellm = FakeLiteLLMKeys()
    result = apply_chat(registry_root, state_root, driver=FailingDriver(), litellm=litellm)
    assert result.ok is False
    assert result.status.state is FamilyState.DEGRADED
    assert result.status.faults[0].code == "sandbox_start_failed"
    assert litellm.minted == 1
    assert paths.grant_path(state_root, "chat").exists()


# --- folded faults (contract 05 section 3.3.1) -------------------------------


def _rfc3339(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """A complaint about the grant file that is on disk now (contract 05
    section 3.3.1 rule 9). `since` is after the write, so it is current."""
    apply_chat(registry_root, state_root)
    written_at = paths.grant_path(state_root, "chat").stat().st_mtime
    write_pep_fault(state_root, "chat", "grants_stale", since=_rfc3339(written_at + 5))

    result = apply_chat(registry_root, state_root)

    assert result.status.state is FamilyState.DEGRADED
    assert result.status.faults[0].code == "grants_stale"
    assert result.status.faults[0].source == "pep"


def test_a_fault_about_the_replaced_grant_file_is_dropped(
    registry_root: Path, state_root: Path
) -> None:
    """A preflight probe leaves `grants_stale` behind and the pass writes the
    real grant file: publishing the fault would show `degraded` with a
    turn-blocking fault that no turn could ever clear. One second between
    the two is all rule 9 needs."""
    write_pep_fault(state_root, "chat", "grants_stale", since=_rfc3339(time.time() - 60))

    result = apply_chat(registry_root, state_root)

    assert result.ok is True
    assert result.status.faults == ()
    assert result.status.state is FamilyState.IN_SYNC


def test_a_folded_fault_still_lets_credentials_and_the_sandbox_apply(
    registry_root: Path, state_root: Path
) -> None:
    write_pep_fault(state_root, "chat", "grants_stale")
    litellm = FakeLiteLLMKeys()
    driver = FakeDriver()
    apply_chat(registry_root, state_root, driver=driver, litellm=litellm)
    assert litellm.minted == 1
    assert driver.ops() == ("create", "set_egress", "assert_egress")
