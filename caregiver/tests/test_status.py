"""The status document assembles contract 05 section 2's shape and writes
it atomically."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

from agent_family import FamilyState, Issue, Report, Severity
from caregiver.faults import FaultEntry
from caregiver.status import (
    STATUS_FILE_MODE,
    ChannelState,
    CredentialsBlock,
    LimitsBlock,
    RotationState,
    SandboxLifecycle,
    SandboxPower,
    SandboxStatus,
    StatusDocument,
    ValidationBlock,
    now_rfc3339,
    write_status,
    write_validation_report,
)


def test_now_rfc3339_matches_the_contracts_examples() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", now_rfc3339())


VALIDATION = ValidationBlock(
    rev="reg-9f21c4",
    checked_at="2026-09-19T00:00:00Z",
    ok=True,
    never_valid=False,
    error_count=0,
    warning_count=0,
    report_path="/srv/agents/state/rework/families/chat/validation.json",
    first_error=None,
)


def test_validation_block_as_json() -> None:
    assert VALIDATION.as_json() == {
        "rev": "reg-9f21c4",
        "checked_at": "2026-09-19T00:00:00Z",
        "ok": True,
        "never_valid": False,
        "error_count": 0,
        "warning_count": 0,
        "report_path": "/srv/agents/state/rework/families/chat/validation.json",
        "first_error": None,
    }


def test_sandbox_status_stringifies_its_enums() -> None:
    sandbox = SandboxStatus(
        id="chat-s1",
        state=SandboxLifecycle.READY,
        power=SandboxPower.RUNNING,
        image="sha256:abc",
        spec_hash="deadbeef",
        cpus=4,
        memory="8g",
        created_at="2026-09-19T00:00:00Z",
        ready_at="2026-09-19T00:00:10Z",
        channel=ChannelState.OPEN,
    )
    body = sandbox.as_json()
    assert body["state"] == "ready"
    assert body["power"] == "running"
    assert body["channel"] == "open"
    # No `turns_running`: `caregiver` may not call `GET /v1/sessions`
    # (contract 02 §3.1), and a session row's `sandbox` names the sandbox
    # that served the LAST turn. A field that cannot be kept true is left
    # out, never written.
    assert "turns_running" not in body


def test_credentials_block_as_json() -> None:
    creds = CredentialsBlock(
        epoch=1,
        key_id="family-chat",
        token_id="family-chat",
        rotated_at="2026-09-19T00:00:00Z",
        next_rotation_at=None,
        rotation_state=RotationState.SETTLED,
    )
    assert creds.as_json()["rotation_state"] == "settled"
    assert creds.as_json()["epoch"] == 1


def test_limits_block_defaults_max_queued_turns_to_100() -> None:
    """Contract 05 §2.1: attendance's own constant (contract 02 §13 rule 4),
    not read from family.yaml, so it is the one field not null by default."""
    assert LimitsBlock().as_json() == {
        "max_running_turns": None,
        "max_queued_turns": 100,
        "job_timeout_s": None,
    }


def test_status_document_assembles_the_full_shape() -> None:
    doc = StatusDocument(
        family="chat",
        kind="attended",
        state=FamilyState.IN_SYNC,
        written_at="2026-09-19T00:00:00Z",
        registry_rev="reg-9f21c4",
        applied_rev="reg-9f21c4",
        config_rev="reg-9f21c4",
        validation=VALIDATION,
        faults=(FaultEntry(code="grants_stale", blocks_turns=True, since="x", source="pep"),),
    )
    body = doc.as_json()
    assert body["family"] == "chat"
    assert body["state"] == "in_sync"
    assert body["sandboxes"] == []
    assert body["credentials"] is None
    assert body["reconcile"] is None
    assert body["spend"] is None
    assert body["faults"] == [
        {
            "code": "grants_stale",
            "blocks_turns": True,
            "since": "x",
            "source": "pep",
            "stale": False,
        }
    ]
    assert body["limits"] == {
        "max_running_turns": None,
        "max_queued_turns": 100,
        "job_timeout_s": None,
    }


def test_status_document_in_json_uses_the_display_underscore_form() -> None:
    """Contract 05 section 2.1: `in_sync` in JSON, "in sync" only for
    display."""
    doc = StatusDocument(
        family="chat",
        kind="attended",
        state=FamilyState.IN_SYNC,
        written_at="x",
        registry_rev="r",
        applied_rev="r",
        config_rev="r",
        validation=VALIDATION,
    )
    assert doc.as_json()["state"] == "in_sync"


def test_write_status_is_atomic_and_mode_0644(tmp_path: Path) -> None:
    doc = StatusDocument(
        family="chat",
        kind="attended",
        state=FamilyState.RECONCILING,
        written_at="x",
        registry_rev="r1",
        applied_rev="r0",
        config_rev="r0",
        validation=VALIDATION,
    )
    path = tmp_path / "status.json"
    write_status(path, doc)
    assert json.loads(path.read_text(encoding="utf-8")) == doc.as_json()
    assert stat.S_IMODE(path.stat().st_mode) == STATUS_FILE_MODE == 0o644


def test_write_validation_report_publishes_agent_familys_report(tmp_path: Path) -> None:
    report = Report(
        family="chat",
        file="families/chat/family.yaml",
        issues=(Issue(Severity.ERROR, "tools.kagi[1]", "not declared"),),
        state=FamilyState.INVALID,
        applied=False,
    )
    path = tmp_path / "validation.json"
    write_validation_report(path, report)
    assert json.loads(path.read_text(encoding="utf-8")) == report.as_json()
