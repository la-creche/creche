"""Contract 04 §6: audit record v2, its path, its mode and its two halves."""

from __future__ import annotations

import json
import re
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from chaperone.audit import AuditError
from chaperone.family_audit import (
    AUDIT_ARG_STRING_MAX_CHARS,
    AUDIT_DIR_MODE,
    AUDIT_TRUNCATION_MARKER,
    HOLD_RESERVE_LEVELS,
    UNRECORDABLE_ARGS,
    AuditEntry,
    FamilyAudit,
    Outcome,
    can_hold,
    resolve_sandbox,
    truncate_strings,
)
from chaperone.headers import Claimed

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


#: More levels than the writer walks.
DEEP = 3000


def _nested(levels: int) -> object:
    value: object = "x"
    for _ in range(levels):
        value = [value]

    return value


#: Arguments that no line can hold: no UTF-8 form, or too many levels.
NOT_FOR_A_LINE = [
    pytest.param({"input": "\ud800"}, id="a-string-that-is-not-text"),
    pytest.param({"\ud800": 1}, id="a-key-that-is-not-text"),
    pytest.param({"input": _nested(DEEP)}, id="nested-too-deep"),
]


@pytest.mark.parametrize("args", NOT_FOR_A_LINE)
def test_arguments_that_no_line_can_hold_do_not_cost_the_record(
    tmp_path: Path, args: dict[str, object]
) -> None:
    """Invariant 15: every decision is recorded. The line holds a marker in
    place of such arguments, and each other field as usual."""
    audit = FamilyAudit(tmp_path)

    audit.write(allowed(args=args, outcome=Outcome.DENY, reason="tool_not_granted"))

    (record,) = lines(tmp_path)
    assert record["args"] == UNRECORDABLE_ARGS
    assert (record["tool"], record["decision"], record["reason"]) == (
        "embed",
        "deny",
        "tool_not_granted",
    )


@pytest.mark.parametrize("outcome", [Outcome.ALLOW, Outcome.PENDING])
@pytest.mark.parametrize("args", NOT_FOR_A_LINE)
def test_only_the_line_of_a_denial_can_end_with_the_marker(
    tmp_path: Path, args: dict[str, object], outcome: Outcome
) -> None:
    """The service refuses such a call before its effect and before its
    gate. If one still comes to the writer, the line stays and the write
    fails, so the route answers 500 (non-negotiable 5)."""
    audit = FamilyAudit(tmp_path)

    with pytest.raises(AuditError) as raised:
        audit.write(allowed(args=args, outcome=outcome))

    assert "\ud800" not in str(raised.value)
    (record,) = lines(tmp_path)
    assert (record["args"], record["decision"]) == (UNRECORDABLE_ARGS, outcome.value)


@pytest.mark.parametrize("args", NOT_FOR_A_LINE)
def test_such_arguments_are_not_held(args: dict[str, object]) -> None:
    assert can_hold(args) is False


def _from_deeper(frames: int, walk: Callable[[], object]) -> object:
    """Call `walk` from `frames` more frames than the caller has."""
    if frames == 0:
        return walk()

    return _from_deeper(frames - 1, walk)


def test_the_check_keeps_levels_free_for_the_write() -> None:
    """The write of a line starts deeper in the stack than the check before
    the effect. What the check passes, a caller that is deeper by half of
    the reserve still walks."""
    depth = sys.getrecursionlimit() - 200
    assert can_hold({"x": _nested(depth)}) is True
    while can_hold({"x": _nested(depth + 1)}):
        depth += 1

    args: dict[str, object] = {"x": _nested(depth)}
    walked = _from_deeper(HOLD_RESERVE_LEVELS // 2, lambda: truncate_strings(args))

    assert walked == args


def test_a_string_that_is_not_text_after_the_cut_is_not_held() -> None:
    """The line would hold what the cap leaves. The check reads the whole
    arguments, because an effect gets the whole arguments."""
    args: dict[str, object] = {"input": "a" * AUDIT_ARG_STRING_MAX_CHARS + "\ud800"}

    assert can_hold(args) is False


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({}, id="none"),
        pytest.param({"input": "caf\u00e9 \U0001f600"}, id="text-outside-ascii"),
        pytest.param({"input": "a" * (AUDIT_ARG_STRING_MAX_CHARS + 1)}, id="over-the-cap"),
        pytest.param({"a": float("nan"), "b": 10**400}, id="numbers-of-any-form"),
        pytest.param({"input": _nested(200)}, id="nested-200-levels"),
    ],
)
def test_arguments_that_a_line_holds_are_held(args: dict[str, object]) -> None:
    assert can_hold(args) is True
