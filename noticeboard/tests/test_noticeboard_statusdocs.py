"""Reading `managerd`'s status documents (contract 05 §2, §3, §4, §7, §8)."""

from __future__ import annotations

from pathlib import Path

from noticeboard.statusdocs import (
    Health,
    read_families,
    read_family,
    read_outcomes,
    read_report,
)
from noticeboard_helpers import (
    NOW,
    fault_doc,
    make_state_root,
    outcome_doc,
    sandbox_doc,
    status_doc,
    write_json,
)


def test_a_fresh_document_reads_in_sync(tmp_path: Path) -> None:
    root = make_state_root(tmp_path)

    row = read_family(root / "families", "chat", NOW)

    assert row.health is Health.IN_SYNC
    assert row.health.label == "in sync"
    assert row.reason == ""
    assert row.kind == "attended"
    assert row.epoch == 7


def test_every_family_is_found_by_listing_the_directory(tmp_path: Path) -> None:
    root = make_state_root(tmp_path)

    rows = read_families(root / "families", NOW)

    assert [one.name for one in rows] == ["chat", "scrum-lead"]


def test_a_stale_document_reads_unknown_not_its_own_state(tmp_path: Path) -> None:
    """Contract 05 §2 rule 5: past 90 s, `managerd` is not running."""
    root = tmp_path / "state"
    write_json(root / "families" / "chat" / "status.json", status_doc(age_s=200))

    row = read_family(root / "families", "chat", NOW)

    assert row.health is Health.UNKNOWN
    assert row.stale
    assert "managerd last wrote 200s ago" in row.reason


def test_a_document_with_no_written_at_reads_unknown(tmp_path: Path) -> None:
    root = tmp_path / "state"
    doc = status_doc()
    doc.pop("written_at")
    write_json(root / "families" / "chat" / "status.json", doc)

    row = read_family(root / "families", "chat", NOW)

    assert row.health is Health.UNKNOWN
    assert "may not be running" in row.reason


def test_a_missing_document_reports_and_does_not_raise(tmp_path: Path) -> None:
    (tmp_path / "families" / "ghost").mkdir(parents=True)

    row = read_family(tmp_path / "families", "ghost", NOW)

    assert row.health is Health.UNREADABLE
    assert row.problem == "status.json is missing"
    assert row.sandboxes == ()
    assert row.spend is None


def test_a_malformed_document_reports_and_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "families" / "chat" / "status.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all", encoding="utf-8")

    row = read_family(tmp_path / "families", "chat", NOW)

    assert row.health is Health.UNREADABLE
    assert "is not JSON" in row.problem


def test_a_document_that_is_not_an_object_reports(tmp_path: Path) -> None:
    path = tmp_path / "families" / "chat" / "status.json"
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    row = read_family(tmp_path / "families", "chat", NOW)

    assert "not a JSON object" in row.problem


def test_an_unknown_state_word_reads_unreadable(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_json(root / "families" / "chat" / "status.json", status_doc(state="fine"))

    row = read_family(root / "families", "chat", NOW)

    assert row.health is Health.UNREADABLE


def test_invalid_says_the_first_error(tmp_path: Path) -> None:
    root = tmp_path / "state"
    doc = status_doc(state="invalid")
    doc["validation"]["ok"] = False
    doc["validation"]["error_count"] = 2
    doc["validation"]["first_error"] = "tools.kagi: unknown MCP server"
    write_json(root / "families" / "chat" / "status.json", doc)

    row = read_family(root / "families", "chat", NOW)

    assert row.health is Health.INVALID
    assert row.reason == "tools.kagi: unknown MCP server"


def test_never_valid_says_so_first(tmp_path: Path) -> None:
    root = tmp_path / "state"
    doc = status_doc(state="invalid")
    doc["validation"]["ok"] = False
    doc["validation"]["never_valid"] = True
    doc["validation"]["first_error"] = "model: field required"
    write_json(root / "families" / "chat" / "status.json", doc)

    row = read_family(root / "families", "chat", NOW)

    assert row.reason.startswith("no revision of this family ever validated")


def test_degraded_names_the_blocking_fault_first(tmp_path: Path) -> None:
    root = tmp_path / "state"
    doc = status_doc(
        state="degraded",
        faults=[
            fault_doc("spend_unknown", blocks_turns=False),
            fault_doc("grants_stale", blocks_turns=True, message="the PEP has not re-read"),
        ],
    )
    write_json(root / "families" / "chat" / "status.json", doc)

    row = read_family(root / "families", "chat", NOW)

    assert row.health is Health.DEGRADED
    assert row.reason.startswith("grants_stale")
    assert len(row.blocking_faults) == 1


def test_reconciling_names_the_step(tmp_path: Path) -> None:
    root = tmp_path / "state"
    doc = status_doc(
        state="reconciling",
        reconcile={
            "since": "2026-09-19T11:59:40Z",
            "from_rev": "reg-8c01aa",
            "to_rev": "reg-9f21c4",
            "step": "switch_sandbox",
            "attempts": 1,
            "needs_switch": True,
        },
    )
    write_json(root / "families" / "chat" / "status.json", doc)

    row = read_family(root / "families", "chat", NOW)

    assert row.health is Health.RECONCILING
    assert row.reason == "step switch_sandbox, attempt 1"
    assert row.reconcile is not None
    assert row.reconcile.needs_switch


def test_the_serving_sandbox_is_ready_before_creating(tmp_path: Path) -> None:
    """Contract 05 §4.2 rule 1: a `ready` sandbox always wins."""
    root = tmp_path / "state"
    doc = status_doc(
        sandboxes=[
            sandbox_doc("chat-s3", state="draining"),
            sandbox_doc("chat-s4", state="ready"),
            sandbox_doc("chat-s5", state="creating"),
        ]
    )
    write_json(root / "families" / "chat" / "status.json", doc)

    row = read_family(root / "families", "chat", NOW)

    assert row.serving is not None
    assert row.serving.id == "chat-s4"


def test_no_sandbox_row_carries_a_running_turn_count(tmp_path: Path) -> None:
    """The status document carries no `turns_running`, and this page must
    not put it back by defaulting it.

    `managerd` may not call `GET /v1/sessions` (contract 02 §3.1), so it
    cannot count a running turn, and a summed default of 0 would put
    "0 running" on the one screen invariant 20 calls the truth. The live
    count has one authority, `sessiond`, and the family page's session
    table is where it shows."""
    root = tmp_path / "state"
    write_json(
        root / "families" / "chat" / "status.json",
        status_doc(sandboxes=[sandbox_doc("chat-s3", turns_running=7)]),
    )

    row = read_family(root / "families", "chat", NOW)

    assert not hasattr(row, "turns_running")
    assert not hasattr(row.sandboxes[0], "turns_running")


def test_the_newest_creating_sandbox_serves_when_none_is_ready(tmp_path: Path) -> None:
    root = tmp_path / "state"
    doc = status_doc(
        sandboxes=[
            sandbox_doc("chat-s3", state="creating"),
            sandbox_doc("chat-s4", state="creating"),
        ]
    )
    write_json(root / "families" / "chat" / "status.json", doc)

    row = read_family(root / "families", "chat", NOW)

    assert row.serving is not None
    assert row.serving.id == "chat-s4"


def test_no_sandbox_carries_spend(tmp_path: Path) -> None:
    """The budget belongs to the family, never a sandbox."""
    root = make_state_root(tmp_path)

    row = read_family(root / "families", "chat", NOW)

    assert row.spend is not None
    assert row.spend.spend_usd == 3.42
    assert row.spend.budget_usd == 15.0
    for sandbox in row.sandboxes:
        assert not hasattr(sandbox, "spend_usd")
        assert not hasattr(sandbox, "budget_usd")


def test_spend_older_than_the_document_reads_stale(tmp_path: Path) -> None:
    """Contract 05 §7 rule 4: a stale number is never presented as current."""
    root = tmp_path / "state"
    write_json(root / "families" / "chat" / "status.json", status_doc(spend_age_s=600))

    row = read_family(root / "families", "chat", NOW)

    assert row.spend is not None
    assert row.spend.stale


def test_null_spend_is_not_a_number(tmp_path: Path) -> None:
    """A one-shot apply publishes `spend: null` (contract 05 §7 rule 3a)."""
    root = tmp_path / "state"
    write_json(root / "families" / "chat" / "status.json", status_doc(spend=None))

    row = read_family(root / "families", "chat", NOW)

    assert row.spend is None


def test_spend_share_needs_both_numbers(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_json(
        root / "families" / "chat" / "status.json",
        status_doc(spend={"window": "day", "spend_usd": 5.0, "budget_usd": 0, "as_of": None}),
    )

    row = read_family(root / "families", "chat", NOW)

    assert row.spend is not None
    assert row.spend.share is None
    assert row.spend.known


def test_a_sandbox_without_playpen_env_is_visible(tmp_path: Path) -> None:
    """Contract 05 §4.1.1 rule 4 makes it a fault; the page must show it."""
    root = tmp_path / "state"
    write_json(
        root / "families" / "chat" / "status.json",
        status_doc(sandboxes=[sandbox_doc("chat-s3", supervisor_env="")]),
    )

    row = read_family(root / "families", "chat", NOW)

    assert not row.sandboxes[0].has_playpen_env


def test_a_count_field_that_is_not_a_number_reads_zero(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_json(
        root / "families" / "chat" / "status.json",
        status_doc(sandboxes=[sandbox_doc("chat-s3", cpus=True)]),
    )

    row = read_family(root / "families", "chat", NOW)

    assert row.sandboxes[0].cpus == 0


def test_limits_keep_null_apart_from_zero(tmp_path: Path) -> None:
    root = make_state_root(tmp_path)

    row = read_family(root / "families", "chat", NOW)

    assert row.limits.max_running_turns is None
    assert row.limits.max_queued_turns == 100


def test_the_validation_report_is_read_from_its_own_file(tmp_path: Path) -> None:
    root = make_state_root(tmp_path)
    write_json(
        root / "families" / "chat" / "validation.json",
        {
            "family": "chat",
            "issues": [
                {"severity": "error", "loc": "tools.kagi", "msg": "unknown MCP server"},
                {"severity": "warning", "loc": "egress", "msg": "empty", "downgraded": False},
            ],
        },
    )

    issues, problem = read_report(root / "families" / "chat" / "validation.json")

    assert problem == ""
    assert len(issues) == 2


def test_a_missing_report_is_reported(tmp_path: Path) -> None:
    issues, problem = read_report(tmp_path / "validation.json")

    assert issues == ()
    assert problem == "validation.json is missing"


def test_outcomes_come_back_newest_first(tmp_path: Path) -> None:
    root = tmp_path / "state" / "outcomes" / "scrum-lead"
    for outcome_id in ("01JBQ80M4F7S2YQ1VZK6W3TDEA", "01JBQ80M4F7S2YQ1VZK6W3TDEZ"):
        write_json(root / f"{outcome_id}.json", outcome_doc(outcome_id))

    rows = read_outcomes(tmp_path / "state" / "outcomes", "scrum-lead", limit=10)

    assert [one.id for one in rows] == [
        "01JBQ80M4F7S2YQ1VZK6W3TDEZ",
        "01JBQ80M4F7S2YQ1VZK6W3TDEA",
    ]
    assert rows[0].trigger == "timer morning-triage"
    assert rows[0].spend_usd == 0.21


def test_a_malformed_outcome_reports_on_its_own_row(tmp_path: Path) -> None:
    root = tmp_path / "state" / "outcomes" / "scrum-lead"
    root.mkdir(parents=True)
    (root / "01JBQ80M4F7S2YQ1VZK6W3TDEN.json").write_text("nope", encoding="utf-8")

    rows = read_outcomes(tmp_path / "state" / "outcomes", "scrum-lead", limit=10)

    assert len(rows) == 1
    assert "is not JSON" in rows[0].problem


def test_a_missing_outcome_directory_is_not_an_error(tmp_path: Path) -> None:
    assert read_outcomes(tmp_path / "outcomes", "chat", limit=10) == ()
