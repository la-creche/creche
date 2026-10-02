"""Upstream argument fences (contract 01b §7)."""

from __future__ import annotations

from agent_pep.fences import ArgDeny, FenceRule, ServerFence, from_upstreams, refuse

_OWNER_REPO = "owner/repo"
_PLATFORM = frozenset({"example-owner/agent-control", "example-owner/agent-registry"})


def _denying_all() -> dict[str, ServerFence]:
    return {
        "github-code": ServerFence(
            denies=(FenceRule(tools=None, arg=_OWNER_REPO, values=_PLATFORM),)
        )
    }


def _allowing_all() -> dict[str, ServerFence]:
    return {
        "github-platform": ServerFence(
            allows=(FenceRule(tools=None, arg=_OWNER_REPO, values=_PLATFORM),)
        )
    }


def test_no_fence_refuses_nothing() -> None:
    assert refuse({}, "kagi", "kagi_search_fetch", {"q": "x"}) is None


def test_a_deny_refuses_a_listed_value() -> None:
    fences = _denying_all()
    args = {"owner": "example-owner", "repo": "agent-control"}
    assert refuse(fences, "github-code", "get_file_contents", args) is not None

    elsewhere = {"owner": "someone", "repo": "notes"}
    assert refuse(fences, "github-code", "get_file_contents", elsewhere) is None


def test_a_composite_never_ignores_the_owner() -> None:
    """§7.2: matching `repo` alone would let another owner's fork pass."""
    fences = _denying_all()
    forked = {"owner": "someone-else", "repo": "agent-control"}
    assert refuse(fences, "github-code", "get_file_contents", forked) is None


def test_a_deny_covering_named_tools_only() -> None:
    fences = {
        "github-code": ServerFence(
            denies=(
                FenceRule(
                    tools=frozenset({"push_files"}), arg="branch", values=frozenset({"main"})
                ),
            )
        )
    }
    assert refuse(fences, "github-code", "push_files", {"branch": "main"}) is not None
    assert refuse(fences, "github-code", "list_commits", {"branch": "main"}) is None


def test_a_missing_argument_passes_a_deny() -> None:
    fences = _denying_all()
    assert refuse(fences, "github-code", "search_code", {"q": "x"}) is None


def test_an_allow_refuses_anything_outside_it() -> None:
    fences = _allowing_all()
    inside = {"owner": "example-owner", "repo": "agent-control"}
    assert refuse(fences, "github-platform", "get_file_contents", inside) is None

    outside = {"owner": "someone", "repo": "notes"}
    assert refuse(fences, "github-platform", "get_file_contents", outside) is not None


def test_an_allow_refuses_a_call_carrying_no_such_argument() -> None:
    """§7.1 rule 3: deny by default. This is why search is unreachable."""
    fences = _allowing_all()
    assert refuse(fences, "github-platform", "search_code", {"q": "x"}) is not None


def test_an_allow_skips_a_tool_it_does_not_cover() -> None:
    fences = {
        "github-platform": ServerFence(
            allows=(
                FenceRule(
                    tools=frozenset({"get_file_contents"}), arg="repo", values=frozenset({"x"})
                ),
            )
        )
    }
    assert refuse(fences, "github-platform", "list_tags", {}) is None


def test_denies_run_before_allows() -> None:
    fences = {
        "s": ServerFence(
            denies=(FenceRule(tools=None, arg="branch", values=frozenset({"main"})),),
            allows=(FenceRule(tools=None, arg="branch", values=frozenset({"main", "dev"})),),
        )
    }
    assert refuse(fences, "s", "push", {"branch": "main"}) is not None
    assert refuse(fences, "s", "push", {"branch": "dev"}) is None


def test_the_stage_one_source_is_the_upstreams_file() -> None:
    built = from_upstreams(
        {
            "github": (
                ArgDeny(tools=frozenset({"push_files"}), arg="branch", values=frozenset({"main"})),
            ),
            "kagi": (),
        }
    )
    assert set(built) == {"github"}
    assert refuse(built, "github", "push_files", {"branch": "main"}) is not None
    # The roster carries `arg_denies` alone, so no allow rule is built.
    assert built["github"].allows == ()
