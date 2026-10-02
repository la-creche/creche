"""The classifier is correct for each field (contract 01 §6, contract 05 §5.4).

`classify(old, new)` is pure, so every case builds two `FamilyFile` objects
from one fixture with `edited()` and reads off the one `FieldChange` that
field produced. A second block covers `Diff` itself: `refused`, `needs_switch`,
`switch_mode` and `steps()`."""

from __future__ import annotations

from typing import Any

import pytest
from agent_family.classify import Diff, Direction, FieldChange, Landing, Step, SwitchMode, classify
from agent_family.model import FamilyFile
from family_helpers import edited


def change_for(diff: Diff, field: str) -> FieldChange:
    matches = [one for one in diff.changes if one.field == field]
    assert len(matches) == 1, diff.changes
    return matches[0]


def built(fixture: str, old: dict[str, Any], new: dict[str, Any]) -> tuple[FamilyFile, FamilyFile]:
    return edited(fixture, **old), edited(fixture, **new)


# --- immutables and description (contract 01 §3.1) ---------------------------


def test_name_change_is_immutable() -> None:
    old, new = built("chat", {}, {"name": "chat2"})
    change = change_for(classify(old, new), "name")
    assert change.landing is Landing.IMMUTABLE
    assert change.direction is Direction.CHANGE
    assert change.step is Step.NONE


def test_kind_change_is_immutable() -> None:
    old, new = built("chat", {}, {"kind": "thin"})
    change = change_for(classify(old, new), "kind")
    assert change.landing is Landing.IMMUTABLE


def test_description_change_is_live_and_needs_no_step() -> None:
    old, new = built("chat", {}, {"description": "A new one, still one line."})
    change = change_for(classify(old, new), "description")
    assert change.landing is Landing.LIVE
    assert change.direction is Direction.CHANGE
    assert change.step is Step.NONE


# --- model (contract 01 §3.2, §6.2) ------------------------------------------


def test_model_router_change_is_both_directions() -> None:
    """A swap adds one alias and removes another (§6.2)."""
    old, new = built(
        "chat",
        {"model": {"router": "agent-router", "budget_usd_per_day": 15}},
        {"model": {"router": "code-router", "budget_usd_per_day": 15}},
    )
    change = change_for(classify(old, new), "model.router")
    assert change.landing is Landing.LIVE
    assert change.direction is Direction.BOTH
    assert change.step is Step.UPDATE_KEY


@pytest.mark.parametrize(
    ("before", "after", "expect"), [(15, 30, Direction.ADD), (15, 5, Direction.REMOVE)]
)
def test_model_budget_change_direction_follows_the_number(
    before: int, after: int, expect: Direction
) -> None:
    old, new = built(
        "chat",
        {"model": {"router": "agent-router", "budget_usd_per_day": before}},
        {"model": {"router": "agent-router", "budget_usd_per_day": after}},
    )
    change = change_for(classify(old, new), "model.budget_usd_per_day")
    assert change.direction is expect
    assert change.step is Step.UPDATE_KEY


# --- grants: tools, verbs, delegates, approval (contract 01 §3.4-3.6, §3.11) -


def test_tools_server_added() -> None:
    old, new = built(
        "chat",
        {"tools": {"kagi": ["kagi_extract"]}},
        {"tools": {"kagi": ["kagi_extract"], "mail": ["send_standup_email"]}},
    )
    change = change_for(classify(old, new), "tools")
    assert change.direction is Direction.ADD
    assert change.step is Step.WRITE_GRANTS


def test_tools_server_removed() -> None:
    old, new = built(
        "chat",
        {"tools": {"kagi": ["kagi_extract"], "mail": ["send_standup_email"]}},
        {"tools": {"kagi": ["kagi_extract"]}},
    )
    assert change_for(classify(old, new), "tools").direction is Direction.REMOVE


def test_tools_swap_is_both() -> None:
    old, new = built(
        "chat", {"tools": {"kagi": ["kagi_extract"]}}, {"tools": {"mail": ["send_standup_email"]}}
    )
    assert change_for(classify(old, new), "tools").direction is Direction.BOTH


def test_tools_widened_to_all_adds() -> None:
    """`all` resolves at read time, so widening a grant to it can only add."""
    old, new = built("chat", {"tools": {"kagi": ["kagi_extract"]}}, {"tools": {"kagi": "all"}})
    assert change_for(classify(old, new), "tools").direction is Direction.ADD


def test_tools_narrowed_from_all_is_conservatively_both() -> None:
    old, new = built("chat", {"tools": {"kagi": "all"}}, {"tools": {"kagi": ["kagi_extract"]}})
    assert change_for(classify(old, new), "tools").direction is Direction.BOTH


def test_verb_added() -> None:
    old, new = built("chat", {"verbs": {}}, {"verbs": {"enqueue": {"targets": ["scrum-lead"]}}})
    change = change_for(classify(old, new), "verbs")
    assert change.direction is Direction.ADD
    assert change.step is Step.WRITE_GRANTS


def test_verb_removed() -> None:
    old, new = built("chat", {"verbs": {"enqueue": {"targets": ["scrum-lead"]}}}, {"verbs": {}})
    assert change_for(classify(old, new), "verbs").direction is Direction.REMOVE


def test_same_verb_narrower_fence_is_both() -> None:
    """Same verb names, a different fence: the PEP checks the fence per call,
    so this classifier cannot say whether it widened or narrowed."""
    old, new = built(
        "chat",
        {"verbs": {"enqueue": {"targets": ["scrum-lead"]}}},
        {"verbs": {"enqueue": {"targets": ["finance-worker"]}}},
    )
    assert change_for(classify(old, new), "verbs").direction is Direction.BOTH


def test_delegate_added() -> None:
    old, new = built(
        "chat", {"delegates": ["vault-oracle"]}, {"delegates": ["vault-oracle", "code-sandbox"]}
    )
    change = change_for(classify(old, new), "delegates")
    assert change.direction is Direction.ADD
    assert change.step is Step.WRITE_GRANTS


def test_delegate_removed() -> None:
    old, new = built(
        "chat", {"delegates": ["vault-oracle", "code-sandbox"]}, {"delegates": ["vault-oracle"]}
    )
    assert change_for(classify(old, new), "delegates").direction is Direction.REMOVE


def test_an_added_approval_entry_narrows_reach() -> None:
    """§6: 'an approval entry only narrows', so adding one reads as REMOVE."""
    old, new = built("chat", {"approval": []}, {"approval": ["invoke_agent"]})
    change = change_for(classify(old, new), "approval")
    assert change.direction is Direction.REMOVE
    assert change.step is Step.WRITE_GRANTS


def test_a_removed_approval_entry_widens_reach() -> None:
    old, new = built("chat", {"approval": ["invoke_agent"]}, {"approval": []})
    assert change_for(classify(old, new), "approval").direction is Direction.ADD


def test_a_raised_inflight_cap_is_live_and_rewrites_the_grants() -> None:
    """§3.6.1 and §6: the cap rides in the grant file, which the PEP re-reads
    per call, so raising it replaces no sandbox."""
    old, new = built("chat", {"max_inflight_delegations": 2}, {"max_inflight_delegations": 3})
    change = change_for(classify(old, new), "max_inflight_delegations")
    assert change.landing is Landing.LIVE
    assert change.direction is Direction.ADD
    assert change.step is Step.WRITE_GRANTS
    assert not classify(old, new).needs_switch


def test_a_lowered_inflight_cap_narrows() -> None:
    old, new = built("chat", {"max_inflight_delegations": 3}, {"max_inflight_delegations": 1})
    assert change_for(classify(old, new), "max_inflight_delegations").direction is Direction.REMOVE


# --- config: egress, skills, shell, sandbox_tools (contract 01 §3.7-3.8) -----


def test_egress_added() -> None:
    old, new = built("chat", {"egress": []}, {"egress": ["github.com"]})
    change = change_for(classify(old, new), "egress")
    assert change.direction is Direction.ADD
    assert change.step is Step.SET_EGRESS


def test_egress_removed() -> None:
    old, new = built("chat", {"egress": ["github.com"]}, {"egress": []})
    assert change_for(classify(old, new), "egress").direction is Direction.REMOVE


def test_skills_added() -> None:
    old, new = built("chat", {"skills": ["grill-me"]}, {"skills": ["grill-me", "handoff"]})
    change = change_for(classify(old, new), "skills")
    assert change.direction is Direction.ADD
    assert change.step is Step.WRITE_CONFIG


def test_skills_removed() -> None:
    old, new = built("chat", {"skills": ["grill-me", "handoff"]}, {"skills": ["grill-me"]})
    assert change_for(classify(old, new), "skills").direction is Direction.REMOVE


def test_shell_turned_on_adds() -> None:
    old, new = built("chat", {"shell": False}, {"shell": True})
    change = change_for(classify(old, new), "shell")
    assert change.direction is Direction.ADD
    assert change.step is Step.WRITE_CONFIG


def test_shell_turned_off_removes() -> None:
    old, new = built("chat", {"shell": True}, {"shell": False})
    assert change_for(classify(old, new), "shell").direction is Direction.REMOVE


def test_sandbox_tools_added() -> None:
    old, new = built("chat", {"sandbox_tools": ["read"]}, {"sandbox_tools": ["read", "write"]})
    change = change_for(classify(old, new), "sandbox_tools")
    assert change.direction is Direction.ADD
    assert change.step is Step.WRITE_CONFIG


def test_sandbox_tools_removed() -> None:
    old, new = built("chat", {"sandbox_tools": ["read", "write"]}, {"sandbox_tools": ["read"]})
    assert change_for(classify(old, new), "sandbox_tools").direction is Direction.REMOVE


def test_system_prompt_change_is_live_and_rewrites_config() -> None:
    old, new = built("chat", {"system_prompt": "append"}, {"system_prompt": "replace"})
    change = change_for(classify(old, new), "system_prompt")
    assert change.landing is Landing.LIVE
    assert change.direction is Direction.CHANGE
    assert change.step is Step.WRITE_CONFIG


# --- kind-fenced fields (contract 01 §3.12-3.14) -----------------------------


def test_job_timeout_increase_adds() -> None:
    old, new = built("vault-oracle", {"job": {"timeout": "60s"}}, {"job": {"timeout": "120s"}})
    change = change_for(classify(old, new), "job.timeout")
    assert change.direction is Direction.ADD
    assert change.step is Step.NONE


def test_job_timeout_decrease_removes() -> None:
    old, new = built("vault-oracle", {"job": {"timeout": "120s"}}, {"job": {"timeout": "60s"}})
    assert change_for(classify(old, new), "job.timeout").direction is Direction.REMOVE


def test_trigger_added() -> None:
    old, new = built(
        "scrum-lead",
        {"triggers": [{"cron": "@hourly"}]},
        {"triggers": [{"cron": "@hourly"}, {"cron": "@daily"}]},
    )
    change = change_for(classify(old, new), "triggers")
    assert change.direction is Direction.ADD
    assert change.step is Step.NONE


def test_trigger_replaced_is_both() -> None:
    old, new = built(
        "scrum-lead", {"triggers": [{"cron": "@hourly"}]}, {"triggers": [{"cron": "@daily"}]}
    )
    assert change_for(classify(old, new), "triggers").direction is Direction.BOTH


def test_triggers_reordered_is_a_change_with_the_same_set() -> None:
    old, new = built(
        "scrum-lead",
        {"triggers": [{"cron": "@hourly"}, {"cron": "@daily"}]},
        {"triggers": [{"cron": "@daily"}, {"cron": "@hourly"}]},
    )
    assert change_for(classify(old, new), "triggers").direction is Direction.CHANGE


@pytest.mark.parametrize(
    ("before", "after", "expect"), [(1, 3, Direction.ADD), (3, 1, Direction.REMOVE)]
)
def test_max_running_turns_direction_follows_the_number(
    before: int, after: int, expect: Direction
) -> None:
    old, new = built("scrum-lead", {"max_running_turns": before}, {"max_running_turns": after})
    change = change_for(classify(old, new), "max_running_turns")
    assert change.direction is expect
    assert change.step is Step.NONE


def test_a_quiet_change_is_live_and_moves_no_reach() -> None:
    """§3.15: the trigger door reads the block at the next cron firing, so
    no reconcile step writes it and no sandbox is replaced."""
    old, new = built("scrum-lead", {"quiet": {"floor_hours": 24}}, {"quiet": {"floor_hours": 12}})
    diff = classify(old, new)
    change = change_for(diff, "quiet")
    assert change.landing is Landing.LIVE
    assert change.direction is Direction.CHANGE
    assert change.step is Step.NONE
    assert not diff.needs_switch


# --- sandbox: files, cpus, memory, max_resident_processes (needs a switch) --


def test_a_mount_added_replaces_the_sandbox() -> None:
    old, new = built(
        "chat",
        {"files": [{"path": "/srv/agents/vault", "mode": "ro"}]},
        {
            "files": [
                {"path": "/srv/agents/vault", "mode": "ro"},
                {"path": "/srv/agents/code", "mode": "ro"},
            ]
        },
    )
    change = change_for(classify(old, new), "files")
    assert change.landing is Landing.REPLACE
    assert change.direction is Direction.ADD
    assert change.step is Step.CREATE_SANDBOX


def test_a_mount_removed_replaces_the_sandbox() -> None:
    old, new = built(
        "chat",
        {
            "files": [
                {"path": "/srv/agents/vault", "mode": "ro"},
                {"path": "/srv/agents/code", "mode": "ro"},
            ]
        },
        {"files": [{"path": "/srv/agents/vault", "mode": "ro"}]},
    )
    assert change_for(classify(old, new), "files").direction is Direction.REMOVE


def test_a_mount_widened_to_rw_adds_reach() -> None:
    old, new = built(
        "chat",
        {"files": [{"path": "/srv/agents/vault", "mode": "ro"}]},
        {"files": [{"path": "/srv/agents/vault", "mode": "rw"}]},
    )
    assert change_for(classify(old, new), "files").direction is Direction.ADD


def test_a_mount_narrowed_to_ro_removes_reach() -> None:
    old, new = built(
        "chat",
        {"files": [{"path": "/srv/agents/vault", "mode": "rw"}]},
        {"files": [{"path": "/srv/agents/vault", "mode": "ro"}]},
    )
    assert change_for(classify(old, new), "files").direction is Direction.REMOVE


@pytest.mark.parametrize(
    ("before", "after", "expect"), [(4, 8, Direction.ADD), (8, 4, Direction.REMOVE)]
)
def test_sandbox_cpus_replaces(before: int, after: int, expect: Direction) -> None:
    old, new = built(
        "chat",
        {"sandbox": {"cpus": before, "memory": "8g"}},
        {"sandbox": {"cpus": after, "memory": "8g"}},
    )
    change = change_for(classify(old, new), "sandbox.cpus")
    assert change.landing is Landing.REPLACE
    assert change.direction is expect
    assert change.step is Step.CREATE_SANDBOX


def test_sandbox_memory_replaces() -> None:
    old, new = built(
        "chat", {"sandbox": {"cpus": 4, "memory": "8g"}}, {"sandbox": {"cpus": 4, "memory": "16g"}}
    )
    change = change_for(classify(old, new), "sandbox.memory")
    assert change.landing is Landing.REPLACE
    assert change.direction is Direction.ADD
    assert change.step is Step.CREATE_SANDBOX


def test_sandbox_image_replaces() -> None:
    """The flavor picks the filesystem a turn runs on, and that only
    arrives with a new microVM, so it sits beside `files` in §6."""
    old, new = built(
        "chat",
        {"sandbox": {"cpus": 4, "memory": "8g", "image": "base"}},
        {"sandbox": {"cpus": 4, "memory": "8g", "image": "python"}},
    )
    change = change_for(classify(old, new), "sandbox.image")
    assert change.landing is Landing.REPLACE
    assert change.direction is Direction.CHANGE
    assert change.step is Step.CREATE_SANDBOX
    assert "base" in change.detail
    assert "python" in change.detail


def test_sandbox_image_change_interrupts_rather_than_drains() -> None:
    """The two flavors are not ordered: `python` is not more reach than
    `base`, and `base` is not a subset of `python`. An unordered move is a
    `change`, and contract 05 §5.4 interrupts on one."""
    old, new = built(
        "chat",
        {"sandbox": {"cpus": 4, "memory": "8g", "image": "python"}},
        {"sandbox": {"cpus": 4, "memory": "8g", "image": "base"}},
    )
    diff = classify(old, new)
    assert diff.needs_switch
    assert diff.switch_mode is SwitchMode.INTERRUPT


def test_an_unchanged_image_is_no_change() -> None:
    old, new = built("chat", {}, {})
    assert not [one for one in classify(old, new).changes if one.field == "sandbox.image"]


def test_sandbox_max_resident_processes_replaces() -> None:
    old, new = built(
        "chat",
        {"sandbox": {"cpus": 4, "memory": "8g", "max_resident_processes": 12}},
        {"sandbox": {"cpus": 4, "memory": "8g", "max_resident_processes": 20}},
    )
    change = change_for(classify(old, new), "sandbox.max_resident_processes")
    assert change.landing is Landing.REPLACE
    assert change.step is Step.CREATE_SANDBOX


# --- the Diff object itself ---------------------------------------------------


def test_no_change_is_no_change() -> None:
    old, new = built("chat", {}, {})
    diff = classify(old, new)
    assert diff.changed is False
    assert diff.changes == ()
    assert diff.refused == ()
    assert diff.needs_switch is False
    assert diff.switch_mode is None
    assert diff.steps() == ()
    assert diff.reason() == "no change"


def test_refused_lists_only_the_immutable_changes() -> None:
    old, new = built("chat", {}, {"name": "chat2", "description": "still one line, changed"})
    diff = classify(old, new)
    assert [one.field for one in diff.refused] == ["name"]


def test_an_addition_only_switch_drains() -> None:
    old, new = built(
        "chat",
        {"files": [{"path": "/srv/agents/vault", "mode": "ro"}]},
        {
            "files": [
                {"path": "/srv/agents/vault", "mode": "ro"},
                {"path": "/srv/agents/code", "mode": "ro"},
            ]
        },
    )
    diff = classify(old, new)
    assert diff.needs_switch is True
    assert diff.switch_mode is SwitchMode.DRAIN


def test_a_removal_switch_interrupts() -> None:
    old, new = built(
        "chat",
        {
            "files": [
                {"path": "/srv/agents/vault", "mode": "ro"},
                {"path": "/srv/agents/code", "mode": "ro"},
            ]
        },
        {"files": [{"path": "/srv/agents/vault", "mode": "ro"}]},
    )
    assert classify(old, new).switch_mode is SwitchMode.INTERRUPT


def test_a_revision_that_both_adds_and_removes_interrupts() -> None:
    """Contract 05 §5.4: 'a revision that both adds and removes is a
    removal'."""
    old, new = built(
        "chat",
        {"files": [{"path": "/srv/agents/vault", "mode": "ro"}]},
        {"files": [{"path": "/srv/agents/code", "mode": "ro"}]},
    )
    assert classify(old, new).switch_mode is SwitchMode.INTERRUPT


def test_steps_are_ordered_and_deduplicated() -> None:
    """`tools` and `verbs` both land through WRITE_GRANTS: one step, not two,
    and steps appear in the field order `classify` itself checks them."""
    old, new = built(
        "chat",
        {"tools": {"kagi": ["kagi_extract"]}, "verbs": {}, "egress": []},
        {
            "tools": {"kagi": ["kagi_extract", "kagi_search_fetch"]},
            "verbs": {"enqueue": {"targets": ["scrum-lead"]}},
            "egress": ["github.com"],
        },
    )
    assert classify(old, new).steps() == (Step.WRITE_GRANTS, Step.SET_EGRESS)


def test_reason_joins_every_detail() -> None:
    old, new = built(
        "chat", {"shell": False, "egress": []}, {"shell": True, "egress": ["github.com"]}
    )
    reason = classify(old, new).reason()
    assert "shell changed" in reason
    assert "egress changed" in reason
    assert "; " in reason
