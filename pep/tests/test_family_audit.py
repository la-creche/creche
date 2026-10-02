"""Contract 04 §6: audit record v2, its path, its mode and its two halves."""

from __future__ import annotations

import json
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_pep.audit import AuditError
from agent_pep.family_audit import (
    AUDIT_ARG_STRING_MAX_CHARS,
    AUDIT_DIR_MODE,
    AUDIT_TRUNCATION_MARKER,
    AuditEntry,
    FamilyAudit,
    Outcome,
    resolve_sandbox,
    truncate_strings,
)
from agent_pep.headers import Claimed

TURN = "01K5J9QWB9R1V3T5Y7H9J2K4P6"
REV = "01K5J9QW3R7T0ZP4YB2H6N8M1D"

#: §6.1: RFC 3339, UTC, milliseconds. Exactly three fraction digits and a `Z`.
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")


def lines(directory: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            out.append(json.loads(line))
    return out


def allowed(**over: object) -> AuditEntry:
    base: dict[str, object] = {
        "family": "chat",
        "tool": "embed",
        "args": {"input": "where did I note the boiler service date"},
        "outcome": Outcome.ALLOW,
        "reason": "granted",
        "grants_rev": REV,
        "latency_ms": 38,
    }
    base.update(over)
    return AuditEntry(**base)  # pyright: ignore[reportArgumentType]


def test_the_day_names_the_file(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed())
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (tmp_path / f"{today}.jsonl").exists()


def test_every_contract_field_is_written(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed(claimed=Claimed(session_id="owui-8f1c2e", turn_id=TURN)))
    (record,) = lines(tmp_path)
    assert set(record) == {
        "ts",
        "family",
        "sandbox_id",
        "sandbox_id_trusted",
        "grants_rev",
        "tool",
        "args",
        "decision",
        "reason",
        "latency_ms",
        "waited_ms",
        "gate",
        "claimed",
        "chain",
    }
    assert record["decision"] == "allow"
    assert record["reason"] == "granted"
    assert record["grants_rev"] == REV
    assert record["waited_ms"] == 0
    assert record["gate"] is None


def test_the_timestamp_is_rfc3339_with_milliseconds(tmp_path: Path) -> None:
    """`AuditLog.write` prepends its own `ts`; the record's own key must win."""
    audit = FamilyAudit(tmp_path)
    audit.write(allowed())
    (record,) = lines(tmp_path)
    assert TS_RE.match(str(record["ts"])) is not None


def test_claimed_values_stay_in_their_own_object(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed(claimed=Claimed(session_id="owui-8f1c2e", turn_id=TURN)))
    (record,) = lines(tmp_path)
    assert record["claimed"] == {
        "session_id": "owui-8f1c2e",
        "turn_id": TURN,
        "delegation_id": None,
    }
    # The trusted half never carries a claimed value under its own name.
    assert "session_id" not in record
    assert "turn_id" not in record


def test_a_call_with_no_delegation_has_a_chain_of_one(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed())
    (record,) = lines(tmp_path)
    assert record["chain"] == ["chat"]


def test_a_minted_chain_is_written_as_given(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed(chain=("chat", "vault-oracle")))
    (record,) = lines(tmp_path)
    assert record["chain"] == ["chat", "vault-oracle"]


def test_the_sandbox_is_not_evidence_yet(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed(sandbox=resolve_sandbox("192.0.2.10:51234")))
    (record,) = lines(tmp_path)
    assert record["sandbox_id"] is None
    assert record["sandbox_id_trusted"] is False


def test_a_denial_is_recorded_too(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(
        allowed(outcome=Outcome.DENY, reason="tool_not_granted", latency_ms=None, grants_rev=None)
    )
    (record,) = lines(tmp_path)
    assert record["decision"] == "deny"
    assert record["latency_ms"] is None
    assert record["grants_rev"] is None


def test_a_gated_call_can_be_recorded_as_pending(tmp_path: Path) -> None:
    """§6.4's first of two records, written when a gate opens."""
    audit = FamilyAudit(tmp_path)
    gated = allowed(outcome=Outcome.PENDING, reason="approval_required", gate="a1b2c3d4e5f60718")
    audit.write(gated)
    (record,) = lines(tmp_path)
    assert record["decision"] == "pending"
    assert record["gate"] == "a1b2c3d4e5f60718"


def test_records_append(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed())
    audit.write(allowed(outcome=Outcome.DENY, reason="rate_limited"))
    assert [r["decision"] for r in lines(tmp_path)] == ["allow", "deny"]


def test_modes_are_pinned(tmp_path: Path) -> None:
    directory = tmp_path / "audit"
    audit = FamilyAudit(directory)
    audit.write(allowed())
    assert stat.S_IMODE(directory.stat().st_mode) == AUDIT_DIR_MODE
    for path in directory.glob("*.jsonl"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_an_unwritable_directory_raises(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    tmp_path.chmod(0o500)
    try:
        assert audit.probe_writable() is False
        with pytest.raises(AuditError):
            audit.write(allowed())
    finally:
        tmp_path.chmod(0o750)


def test_probe_writable_answers_yes_on_a_good_directory(tmp_path: Path) -> None:
    assert FamilyAudit(tmp_path).probe_writable() is True


def test_retention_deletes_an_old_day(tmp_path: Path) -> None:
    stale = tmp_path / "2000-01-01.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    FamilyAudit(tmp_path).sweep_retention()
    assert not stale.exists()


def test_a_long_string_is_truncated_not_dropped(tmp_path: Path) -> None:
    audit = FamilyAudit(tmp_path)
    audit.write(allowed(args={"input": "x" * (AUDIT_ARG_STRING_MAX_CHARS + 50), "model": "bge"}))
    (record,) = lines(tmp_path)
    args = record["args"]
    assert isinstance(args, dict)
    assert str(args["input"]).endswith(AUDIT_TRUNCATION_MARKER)
    assert args["model"] == "bge"


def test_truncation_keeps_structure_and_keys() -> None:
    long = "y" * (AUDIT_ARG_STRING_MAX_CHARS + 1)
    out = truncate_strings({"a": [long, 3, True], "b": {"c": long}, "d": None})
    assert out == {
        "a": ["y" * AUDIT_ARG_STRING_MAX_CHARS + AUDIT_TRUNCATION_MARKER, 3, True],
        "b": {"c": "y" * AUDIT_ARG_STRING_MAX_CHARS + AUDIT_TRUNCATION_MARKER},
        "d": None,
    }
