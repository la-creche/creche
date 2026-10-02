"""The family decision core: contract 04 §5, row by row.

Held at 100% branch coverage. A new branch needs a test here AND a new row in
`docs/pep-decisions.md` (`pep/AGENTS.md`).
"""

from __future__ import annotations

from agent_pep.delegations import MAX_DELEGATION_HOPS
from agent_pep.family_decisions import (
    FAMILY_DENY_STATUS,
    Executor,
    decide_family,
    is_granted,
    manifest_actions,
)
from agent_pep.fences import FenceRule, ServerFence
from pep_family_helpers import make_grants

KNOWN = frozenset({"kagi", "github-code"})


def _decide(tool: str, args: dict[str, object] | None = None, **over: object):
    grants = make_grants(**over)
    return decide_family(grants, tool, args or {}, rate_exceeded=False, known_servers=KNOWN)


def _gateable(tool: str, args: dict[str, object] | None = None, **over: object):
    """The same call on a PEP with a delegate door and an approval transport."""
    grants = make_grants(**over)
    return decide_family(
        grants,
        tool,
        args or {},
        rate_exceeded=False,
        known_servers=KNOWN,
        delegate_ready=True,
        approvals_ready=True,
    )


# Row 1 -------------------------------------------------------------------


def test_row_1_no_grants_is_unknown_token() -> None:
    decision = decide_family(None, "embed", {}, rate_exceeded=False)
    assert not decision.allow
    assert decision.reason == "unknown_token"
    assert FAMILY_DENY_STATUS[decision.reason] == 403


# Row 2 -------------------------------------------------------------------


def test_row_2_over_the_rate_limit() -> None:
    decision = decide_family(make_grants(), "embed", {"input": "x"}, rate_exceeded=True)
    assert decision.reason == "rate_limited"
    assert FAMILY_DENY_STATUS[decision.reason] == 429


# Row 3 -------------------------------------------------------------------


def test_row_3_an_upstream_fence_runs_before_any_grant() -> None:
    fences = {
        "github-code": ServerFence(
            denies=(FenceRule(tools=None, arg="branch", values=frozenset({"main"})),)
        )
    }
    # The family holds no github-code grant at all, and the fence still bites.
    decision = decide_family(
        make_grants(),
        "github-code__push_files",
        {"branch": "main"},
        rate_exceeded=False,
        fences=fences,
        known_servers=KNOWN,
    )
    assert decision.reason == "arg_validation"
    assert FAMILY_DENY_STATUS[decision.reason] == 400


# Row 4 -------------------------------------------------------------------


def test_row_4_a_malformed_mcp_name() -> None:
    assert _decide("__orphan").reason == "tool_not_granted"
    assert _decide("kagi__").reason == "tool_not_granted"


def test_row_4_an_ungranted_server_and_tool() -> None:
    assert _decide("github-code__get_file_contents").reason == "tool_not_granted"
    assert _decide("kagi__kagi_extract").reason == "tool_not_granted"


def test_row_4_an_ungranted_verb() -> None:
    decision = _decide("release", {"components": {"pep": "0.1.0"}})
    assert decision.reason == "tool_not_granted"


def test_row_4_a_verb_outside_the_catalog() -> None:
    """A grant file naming a verb this PEP does not hold reaches nothing."""
    decision = _decide("teleport", {}, verbs={"teleport": {}})
    assert decision.reason == "tool_not_granted"
    assert decision.detail is not None
    assert "catalog" in decision.detail


# Row 5 -------------------------------------------------------------------


def test_row_5_arguments_failing_the_schema() -> None:
    decision = _decide("embed", {"input": ""})
    assert decision.reason == "arg_validation"


def test_row_5_arguments_failing_a_verb_fence() -> None:
    decision = _decide("ha_call", {"domain": "lock", "service": "unlock"})
    assert decision.reason == "arg_validation"


# Row 6 and 7 -------------------------------------------------------------


def test_row_6_a_delegate_target_outside_the_list() -> None:
    decision = _decide(
        "invoke_agent", {"family": "finance", "message": "hi"}, delegates=["vault-oracle"]
    )
    assert decision.reason == "tool_not_granted"


def test_row_4_invoke_agent_without_delegates() -> None:
    decision = _decide("invoke_agent", {"family": "vault-oracle", "message": "hi"})
    assert decision.reason == "tool_not_granted"


def test_row_5_invoke_agent_with_bad_arguments() -> None:
    decision = _decide("invoke_agent", {"family": "Vault"}, delegates=["vault-oracle"])
    assert decision.reason == "arg_validation"


def test_row_7_too_many_delegations_in_flight() -> None:
    grants = make_grants(delegates=["vault-oracle"])
    decision = decide_family(
        grants,
        "invoke_agent",
        {"family": "vault-oracle", "message": "hi"},
        rate_exceeded=False,
        known_servers=KNOWN,
        inflight=2,
    )
    assert decision.reason == "rate_limited"


def test_row_7_reads_the_cap_the_grant_file_carries() -> None:
    """The cap is the caller family's own number (contract 01 §3.6.1), read
    per call from the loaded grants. The same two calls in flight that row 7
    refuses above are allowed when the file says 3."""
    grants = make_grants(
        delegates=["vault-oracle"],
        limits={"pep_rpm": 60, "max_inflight_delegations": 3, "max_open_gates": 10},
    )
    decision = decide_family(
        grants,
        "invoke_agent",
        {"family": "vault-oracle", "message": "hi"},
        rate_exceeded=False,
        known_servers=KNOWN,
        inflight=2,
        delegate_ready=True,
    )
    assert decision.allow


# Row 8 and 9 -------------------------------------------------------------


def test_row_8_too_many_open_gates() -> None:
    grants = make_grants(approval=["embed"])
    decision = decide_family(
        grants, "embed", {"input": "x"}, rate_exceeded=False, known_servers=KNOWN, open_gates=10
    )
    assert decision.reason == "rate_limited"


def test_row_9_a_gated_verb_without_a_transport() -> None:
    """No Node-RED configured, so the gate cannot open and the honest answer
    is that this PEP cannot execute the call."""
    decision = _decide("embed", {"input": "x"}, approval=["embed"])
    assert decision.reason == "not_implemented"
    assert FAMILY_DENY_STATUS[decision.reason] == 501


def test_row_9_a_gated_mcp_tool_without_a_transport() -> None:
    decision = _decide("kagi__kagi_search_fetch", {"q": "x"}, approval=["kagi__kagi_search_fetch"])
    assert decision.reason == "not_implemented"


def test_row_9_a_gated_delegate_without_a_transport() -> None:
    decision = _decide(
        "invoke_agent",
        {"family": "vault-oracle", "message": "hi"},
        delegates=["vault-oracle"],
        approval=["invoke_agent"],
    )
    assert decision.reason == "not_implemented"


def test_row_9_a_gated_verb_asks_for_approval() -> None:
    decision = _gateable("embed", {"input": "x"}, approval=["embed"])
    assert decision.allow
    assert decision.approval
    assert decision.executor is Executor.VERB


def test_row_9_a_gated_mcp_tool_asks_for_approval() -> None:
    decision = _gateable(
        "kagi__kagi_search_fetch", {"q": "x"}, approval=["kagi__kagi_search_fetch"]
    )
    assert decision.allow
    assert decision.approval


def test_row_9_a_gated_delegate_asks_for_approval() -> None:
    decision = _gateable(
        "invoke_agent",
        {"family": "vault-oracle", "message": "hi"},
        delegates=["vault-oracle"],
        approval=["invoke_agent"],
    )
    assert decision.allow
    assert decision.approval
    assert decision.executor is Executor.DELEGATE


def test_an_ungated_action_never_asks_for_approval() -> None:
    assert not _gateable("embed", {"input": "x"}).approval


# Row 10 and the seams ----------------------------------------------------


def test_row_10_a_granted_mcp_tool_is_allowed() -> None:
    decision = _decide("kagi__kagi_search_fetch", {"q": "boiler"})
    assert decision.allow
    assert decision.executor is Executor.MCP
    assert decision.server == "kagi"
    assert decision.tool == "kagi_search_fetch"
    assert decision.args == {"q": "boiler"}


def test_row_10_a_granted_verb_is_allowed() -> None:
    decision = _decide("embed", {"input": "boiler"})
    assert decision.allow
    assert decision.executor is Executor.VERB
    assert decision.tool == "embed"


def test_a_granted_server_this_pep_has_not_loaded_is_a_stage_7_seam() -> None:
    decision = decide_family(
        make_grants(tools={"mail": ["send_standup_email"]}),
        "mail__send_standup_email",
        {},
        rate_exceeded=False,
        known_servers=KNOWN,
    )
    assert decision.reason == "not_implemented"


def test_enqueue_and_job_status_and_release_are_stage_seams() -> None:
    enqueued = _decide(
        "enqueue",
        {"family": "finance-worker", "message": "go"},
        verbs={"enqueue": {"targets": ["finance-worker"]}},
    )
    assert enqueued.reason == "not_implemented"

    status = _decide("job_status", {}, verbs={"job_status": {}})
    assert status.reason == "not_implemented"

    released = _decide(
        "release",
        {"components": {"pep": "0.2.0"}},
        verbs={"release": {"components": ["pep"]}},
    )
    assert released.reason == "not_implemented"


def test_a_delegate_without_a_door_is_still_a_seam() -> None:
    """A PEP with no delegate door configured cannot run the call, so it says
    so rather than failing inside execution."""
    decision = _decide(
        "invoke_agent", {"family": "vault-oracle", "message": "hi"}, delegates=["vault-oracle"]
    )
    assert decision.reason == "not_implemented"
    assert decision.detail is not None
    assert "delegate door" in decision.detail


def test_a_delegate_that_passes_every_row_is_allowed() -> None:
    decision = _gateable(
        "invoke_agent", {"family": "vault-oracle", "message": "hi"}, delegates=["vault-oracle"]
    )
    assert decision.allow
    assert decision.executor is Executor.DELEGATE
    assert decision.tool == "invoke_agent"
    assert decision.args == {"family": "vault-oracle", "message": "hi"}


def test_a_second_hop_is_not_granted() -> None:
    """Contract 01 §3.6 rule 4 bounds a chain to one hop. The PEP enforces
    that itself instead of trusting every family file to be written right."""
    decision = decide_family(
        make_grants(delegates=["vault-oracle"]),
        "invoke_agent",
        {"family": "vault-oracle", "message": "hi"},
        rate_exceeded=False,
        known_servers=KNOWN,
        delegate_ready=True,
        hops=MAX_DELEGATION_HOPS,
    )
    assert decision.reason == "tool_not_granted"
    assert FAMILY_DENY_STATUS[decision.reason] == 403


# The manifest ------------------------------------------------------------


def test_the_manifest_offers_only_what_call_can_execute() -> None:
    grants = make_grants(
        tools={"kagi": ["kagi_search_fetch"], "mail": ["send_standup_email"]},
        verbs={"embed": {}, "release": {"components": ["pep"]}},
        delegates=["vault-oracle"],
    )
    assert manifest_actions(grants, KNOWN) == ["kagi__kagi_search_fetch", "embed"]


def test_the_manifest_offers_invoke_agent_once_the_door_exists() -> None:
    grants = make_grants(tools={}, verbs={"embed": {}}, delegates=["vault-oracle"])
    offered = manifest_actions(grants, KNOWN, delegate_ready=True)
    assert offered == ["embed", "invoke_agent"]
    assert offered.count("invoke_agent") == 1


def test_the_manifest_hides_invoke_agent_without_delegates() -> None:
    grants = make_grants(tools={}, verbs={"embed": {}})
    assert manifest_actions(grants, KNOWN, delegate_ready=True) == ["embed"]


def test_the_manifest_hides_a_gated_action_with_no_transport() -> None:
    grants = make_grants(tools={}, verbs={"embed": {}}, approval=["embed"])
    assert manifest_actions(grants, KNOWN, approvals_ready=False) == []
    assert manifest_actions(grants, KNOWN) == ["embed"]


# Is the action still granted? (contract 04 §1.5.3) ------------------------


def test_a_granted_mcp_tool_still_reaches() -> None:
    assert is_granted(make_grants(), "kagi__kagi_search_fetch") is True


def test_a_tool_removed_from_its_server_stops_reaching() -> None:
    assert is_granted(make_grants(tools={"kagi": ["other"]}), "kagi__kagi_search_fetch") is False


def test_a_removed_server_stops_reaching() -> None:
    assert is_granted(make_grants(tools={}), "kagi__kagi_search_fetch") is False


def test_a_malformed_mcp_name_reaches_nothing() -> None:
    assert is_granted(make_grants(), "__kagi_search_fetch") is False


def test_a_granted_verb_still_reaches() -> None:
    assert is_granted(make_grants(), "embed") is True


def test_a_removed_verb_stops_reaching() -> None:
    assert is_granted(make_grants(verbs={"ha_call": {}}), "embed") is False


def test_invoke_agent_reaches_while_delegates_hold_a_name() -> None:
    assert is_granted(make_grants(delegates=["vault-oracle"]), "invoke_agent") is True


def test_invoke_agent_stops_reaching_on_empty_delegates() -> None:
    assert is_granted(make_grants(delegates=[]), "invoke_agent") is False
