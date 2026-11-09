"""The status document assembles contract 05 section 2's shape and writes
it atomically."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest
from agent_family import FamilyState, Issue, Markup, Report, Severity, parse_family
from caregiver.faults import FaultEntry
from caregiver.status import (
    STATUS_FILE_MODE,
    CredentialsBlock,
    LimitsBlock,
    ReconcileBlock,
    ReconcileStep,
    RotationState,
    StatusDocument,
    ValidationBlock,
    now_rfc3339,
    restamp_status,
    write_status,
    write_validation_report,
)
from caregiver.steps import limits_for
from caregiver_helpers import FAMILY_BODY, UNREADABLE_JSON, toml_text


def test_now_rfc3339_matches_the_contracts_examples() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", now_rfc3339())


VALIDATION = ValidationBlock(
    rev="reg-9f21c4",
    checked_at="2026-09-19T00:00:00Z",
    ok=True,
    never_valid=False,
    error_count=0,
    warning_count=0,
    report_path="/srv/agents/state/creche/families/chat/validation.json",
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
        "report_path": "/srv/agents/state/creche/families/chat/validation.json",
        "first_error": None,
    }


def test_the_reconcile_block_holds_no_needs_switch() -> None:
    """Contract 05 minor 20: caregiver switches no VM (decision CH21-1)."""
    block = ReconcileBlock(
        since="2026-09-19T00:00:00Z",
        from_rev="reg-8c01aa",
        to_rev="reg-9f21c4",
        step=ReconcileStep.WRITE_CONFIG,
        attempts=1,
    )
    assert block.as_json() == {
        "since": "2026-09-19T00:00:00Z",
        "from_rev": "reg-8c01aa",
        "to_rev": "reg-9f21c4",
        "step": "write_config",
        "attempts": 1,
    }


def test_the_steps_are_the_six_of_minor_20() -> None:
    """Contract 05 §3.4 drops the three VM steps (decisions CH1, CH21-1)."""
    assert [str(one) for one in ReconcileStep] == [
        "validate",
        "write_grants",
        "update_key",
        "write_config",
        "set_egress",
        "write_timers",
    ]


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
    not read from family.toml, so it is the one field not null by default."""
    assert LimitsBlock().as_json() == {
        "max_running_turns": None,
        "max_queued_turns": 100,
        "job_timeout_s": None,
    }


def test_limits_of_a_thin_family_carry_no_caller_root() -> None:
    """Contract 01 minor 18 drops `job.caller_root` (decision CH10-10), so
    the block holds the three fields of contract 05 §2.1."""
    body = FAMILY_BODY | {"kind": "thin", "job": {}}
    family, issues = parse_family(toml_text(body), Markup.TOML)
    assert family is not None, issues
    assert limits_for(family).as_json() == {
        "max_running_turns": None,
        "max_queued_turns": 100,
        "job_timeout_s": 120,
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
    # Minor 18: the vmm owns each VM, and no sandbox row is published
    # (decision CH21-1).
    assert "sandboxes" not in body
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


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_a_document_that_does_not_read_is_not_restamped(tmp_path: Path, raw: bytes) -> None:
    """The restamp runs on the loop thread. A document that does not read
    is left as it is, and the next pass publishes a whole new one."""
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
    path = tmp_path / "status.json"
    write_status(path, doc)
    path.write_bytes(raw)

    assert restamp_status(path, older_than_s=0.0) is False
    assert path.read_bytes() == raw


def test_write_validation_report_publishes_agent_familys_report(tmp_path: Path) -> None:
    report = Report(
        family="chat",
        file="families/chat/family.toml",
        issues=(Issue(Severity.ERROR, "tools.kagi[1]", "not declared"),),
        state=FamilyState.INVALID,
        applied=False,
    )
    path = tmp_path / "validation.json"
    write_validation_report(path, report)
    assert json.loads(path.read_text(encoding="utf-8")) == report.as_json()
