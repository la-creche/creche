"""Every fence in `validate.py` refuses with its own message (contract 01).

One parametrized test per checked function, in the file's own order. Each
case names the field it edits and the message substring that must appear, so
a failure reads as "this rule broke", not "some test broke somewhere"."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_family.grammar import PLATFORM_SERVER, PROBE_FAMILY
from agent_family.model import FamilyFile
from agent_family.parse import parse_family
from agent_family.registry import load_registry
from agent_family.report import Issues, Report
from agent_family.validate import Index, check_family
from family_helpers import (
    FIXTURES,
    FakeHost,
    check,
    check_no_host,
    edited,
    edited_server,
    errors,
    full_index,
    host,
    load,
    load_server,
    messages,
    server_text,
)

# --- 3.1 name, kind, description -------------------------------------------

IDENTITY_CASES = (
    ({"name": "Chat"}, "not a family name"),
    ({"name": "vault-oracle"}, "does not match its directory"),
    # §3.1: the gate scripts write a scratch grant file under this name and
    # delete it again. A real family of that name would have its grants
    # overwritten and then revoked by a preflight check.
    ({"name": PROBE_FAMILY}, "reserved"),
    ({"kind": "bogus"}, "is not a kind"),
    ({"description": ""}, "description is 0 characters"),
    ({"description": "x" * 201}, "description is 201 characters"),
)


@pytest.mark.parametrize(("changes", "expect"), IDENTITY_CASES)
def test_identity_fences(changes: dict[str, object], expect: str) -> None:
    report = check("chat", **changes)
    assert expect in messages(report)


# --- 3.2 model ---------------------------------------------------------------

MODEL_CASES = (
    ({"model": {"router": "agent-*", "budget_usd_per_day": 15}}, "holds a wildcard"),
    ({"model": {"router": "Agent-Router", "budget_usd_per_day": 15}}, "not a LiteLLM alias"),
    ({"model": {"router": "agent-router", "budget_usd_per_day": 0}}, "is outside 0 to 500"),
    ({"model": {"router": "agent-router", "budget_usd_per_day": 501}}, "is outside 0 to 500"),
    (
        {"model": {"router": "no-such-alias", "budget_usd_per_day": 15}},
        "not an alias LiteLLM serves",
    ),
)


@pytest.mark.parametrize(("changes", "expect"), MODEL_CASES)
def test_model_fences(changes: dict[str, object], expect: str) -> None:
    report = check("chat", **changes)
    assert expect in messages(report)


def test_model_router_downgrades_without_a_host() -> None:
    report = check_no_host("chat")
    assert "no LiteLLM context" in messages(report)
    downgraded = [issue for issue in report.issues if issue.loc == "model.router"]
    assert downgraded and downgraded[0].downgraded


# --- 3.3 files -----------------------------------------------------------

DUP_MOUNT = [
    {"path": "/srv/agents/vault", "mode": "ro"},
    {"path": "/srv/agents/vault", "mode": "rw"},
]

FILES_CASES = (
    (DUP_MOUNT, "is mounted twice"),
    ([{"path": "/srv/agents/vault", "mode": "rx"}], "is not a mode"),
    ([{"path": "/srv/agents/vault/*", "mode": "ro"}], "is a glob"),
    ([{"path": "srv/agents/vault", "mode": "ro"}], "must be an absolute path"),
    ([{"path": "/srv/agents/../etc", "mode": "ro"}], "no '.', '..' or empty segment"),
    ([{"path": "/srv/agents//vault", "mode": "ro"}], "no '.', '..' or empty segment"),
    ([{"path": "/srv/agents/vault/$HOME", "mode": "ro"}], "a character outside"),
    ([{"path": "/srv/agents/sessions/chat", "mode": "rw"}], "is the session store"),
    ([{"path": "/etc", "mode": "ro"}], "is under no allowed root"),
    ([{"path": "/srv/agents/work/platform", "mode": "ro"}], "under the platform fence"),
)


@pytest.mark.parametrize(("files", "expect"), FILES_CASES)
def test_files_fences(files: list[dict[str, str]], expect: str) -> None:
    report = check("chat", files=files)
    assert expect in messages(report)


def test_files_downgrades_without_a_host() -> None:
    report = check_no_host("chat")
    downgraded = [issue for issue in report.issues if issue.loc == "files"]
    assert downgraded and downgraded[0].downgraded
    assert "no mount's symlinks were resolved" in downgraded[0].msg


def test_a_symlink_escaping_its_root_is_an_error() -> None:
    """§5.4, §3.3 rule 5: the resolved path is checked too, and a resolve out
    of every allowed root is an error there, not a silent escape."""
    family = FamilyFile.model_validate(
        {
            "name": "chat",
            "kind": "attended",
            "description": "x",
            "model": {"router": "agent-router", "budget_usd_per_day": 15},
            "files": [{"path": "/srv/agents/vault/escape", "mode": "ro"}],
        }
    )
    sneaky = FakeHost(("agent-router",), links={"/srv/agents/vault/escape": "/etc/passwd"})
    issues = Issues()
    check_family(family, "chat", full_index(), issues, sneaky)
    assert any("under no allowed root" in issue.msg for issue in issues.frozen())


def test_nested_mounts_are_allowed() -> None:
    """§3.3 rule 3: a narrower rw mount inside a wider ro one is the intended
    shape, not a duplicate."""
    report = check(
        "chat",
        files=[
            {"path": "/srv/agents/vault", "mode": "ro"},
            {"path": "/srv/agents/vault/profile/memories", "mode": "rw"},
        ],
    )
    assert not errors(report)


# --- 3.4 tools -----------------------------------------------------------

TOOLS_CASES: tuple[tuple[dict[str, Any], str], ...] = (
    ({"Kagi": ["kagi_extract"]}, "not an MCP server name"),
    ({"no-such-server": ["x"]}, "has no mcp/no-such-server/server.yaml"),
    ({"kagi": "everything"}, "must be a list of tool names or 'all'"),
    ({"kagi": []}, "an empty list grants nothing"),
    ({"kagi": ["kagi_extract", "kagi_extract"]}, "is granted twice"),
    ({"kagi": ["kagi extract"]}, "not a tool name"),
    ({"kagi": ["kagi_summarize"]}, "not declared in mcp/kagi/server.yaml"),
)


@pytest.mark.parametrize(("tools", "expect"), TOOLS_CASES)
def test_tools_fences(tools: dict[str, object], expect: str) -> None:
    report = check("chat", tools=tools)
    assert expect in messages(report)


def test_all_is_refused_on_an_autonomous_family() -> None:
    report = check("scrum-lead", tools={"mail": "all"})
    assert "refused on an autonomous family" in messages(report)


def test_all_is_allowed_on_an_attended_family() -> None:
    report = check("chat", tools={"kagi": "all"})
    assert not errors(report)


# --- 3.5 verbs -------------------------------------------------------------

VERB_CASES: tuple[tuple[dict[str, Any], str], ...] = (
    ({"ha_call": {"allow": []}}, "needs at least one {domain, service} triple"),
    ({"ha_call": {"allow": [{"domain": "NOTIFY", "service": "x"}]}}, "domain"),
    ({"ha_call": {"allow": [{"domain": "notify", "service": "X"}]}}, "service"),
    (
        {"ha_call": {"allow": [{"domain": "notify", "service": "x", "entity_id": "bad"}]}},
        "must be domain.object_id",
    ),
    ({"enqueue": {"targets": []}}, "needs at least one target family"),
    ({"enqueue": {"targets": ["no-such-family"]}}, "not a family in this registry"),
    ({"enqueue": {"targets": ["chat"]}}, "an enqueue target must be autonomous"),
    ({"job_status": {}}, "job_status without enqueue"),
)


@pytest.mark.parametrize(("verbs", "expect"), VERB_CASES)
def test_verb_fences(verbs: dict[str, object], expect: str) -> None:
    report = check("chat", verbs=verbs)
    assert expect in messages(report)


RELEASE_CASES: tuple[tuple[dict[str, Any], str], ...] = (
    ({"release": {"components": ["chaperone"]}}, "outside the platform fence"),
    ({"release": {"components": []}}, "needs at least one component name"),
    ({"release": {"components": ["not-a-component"]}}, "not a releasable component"),
)


@pytest.mark.parametrize(("verbs", "expect"), RELEASE_CASES)
def test_release_fences_on_chat(verbs: dict[str, object], expect: str) -> None:
    """`chat` is outside the platform allowlist, so every `release` shape it
    writes fails at least the fence check first."""
    report = check("chat", verbs=verbs)
    assert expect in messages(report)


def test_release_is_allowed_for_the_platform_family() -> None:
    report = check("agent-control", verbs={"release": {"components": ["chaperone", "caregiver"]}})
    assert not errors(report)


@pytest.mark.parametrize("old_name", ["pep", "sessiond", "releasectl"])
def test_an_old_component_name_is_refused(old_name: str) -> None:
    """The names the packages had before the renames. The fence took them
    for one cycle while the registry's family file still said them, and
    refuses them now like any stranger."""
    old = check("agent-control", verbs={"release": {"components": [old_name]}})
    assert "not a releasable component" in messages(old)


def test_the_platform_family_mounts_nothing_else() -> None:
    """§5.5 rule 5: the fence runs both ways. `docs/rework/spec.md` §4.4
    locks `agent-control` to the platform root, "and nothing else", so a
    fourth repo under another allowed root is an error even though that root
    is allowed for every other family."""
    report = check(
        "agent-control",
        files=[
            {"path": "/srv/agents/work/platform", "mode": "rw"},
            {"path": "/srv/agents/work/projects/example-analytics", "mode": "rw"},
        ],
    )
    assert "reaches only the platform root" in messages(report)


def test_the_platform_family_may_mount_under_the_root() -> None:
    """One repo of the three is a mount like any other. The fence compares
    resolved paths by prefix (§5.5 rule 2), so it must not refuse these."""
    report = check(
        "agent-control",
        files=[
            {"path": "/srv/agents/work/platform/agent-control", "mode": "rw"},
            {"path": "/srv/agents/work/platform/agent-registry", "mode": "rw"},
        ],
    )
    assert not errors(report)


PLATFORM_RW = {"path": "/srv/agents/work/platform", "mode": "rw"}


def test_the_platform_family_may_read_the_hosts_copies_of_its_own_repos() -> None:
    """§5.5 rule 6. The sandbox holds no git
    credential and the three repos are private, so `git fetch origin` cannot
    work and the working copies could never follow `main`. Nothing on the host
    may run git inside `work/platform` either: a sandbox can write
    `.git/config` there. The host's OWN clones under `/srv/agents/code` are
    the fetch source, read-only. They are the same three repos, so "the three
    platform repos. Nothing else." still holds."""
    report = check(
        "agent-control",
        files=[
            PLATFORM_RW,
            {"path": "/srv/agents/code/agent-control", "mode": "ro"},
            {"path": "/srv/agents/code/agent-mcp", "mode": "ro"},
            {"path": "/srv/agents/code/agent-registry", "mode": "ro"},
        ],
    )
    assert not errors(report)


MIRROR_REFUSALS: tuple[tuple[dict[str, str], str], ...] = (
    # Read-only is the whole exception.
    ({"path": "/srv/agents/code/agent-control", "mode": "rw"}, "reaches only the platform root"),
    # A fourth repo is still a fourth repo.
    (
        {"path": "/srv/agents/code/example-analytics", "mode": "ro"},
        "reaches only the platform root",
    ),
    # The corpus root holds every other repo as well.
    ({"path": "/srv/agents/code", "mode": "ro"}, "reaches only the platform root"),
    # Exactly the repo, so nobody has to reason about what sits beside it.
    (
        {"path": "/srv/agents/code/agent-control/docs", "mode": "ro"},
        "reaches only the platform root",
    ),
    # Every other root stays shut, read-only or not.
    ({"path": "/srv/agents/vault", "mode": "ro"}, "reaches only the platform root"),
    ({"path": "/srv/agents/work/projects/agent-control", "mode": "ro"}, "reaches only"),
)


@pytest.mark.parametrize(("mount", "expect"), MIRROR_REFUSALS)
def test_the_mirror_exception_opens_nothing_else(mount: dict[str, str], expect: str) -> None:
    report = check("agent-control", files=[PLATFORM_RW, mount])
    assert expect in messages(report)


def test_the_mirror_exception_is_for_the_platform_family_only() -> None:
    """Any other family already may read the corpus. This must not change
    what `chat` is told about the platform root."""
    report = check("chat", files=[{"path": "/srv/agents/work/platform", "mode": "ro"}])
    assert "under the platform fence" in messages(report)


# --- 5.5 rule 7: the operator lands every platform change ---------------------

MERGE = "merge_pull_request"
MERGE_ENTRY = {"name": MERGE, "description": "Merge a pull request.", "write": True}


def _merge_declared() -> Index:
    """The fixture registry with the platform server declaring its merge tool
    again: what an edit to that server file restoring it looks like."""
    body: dict[str, Any] = yaml.safe_load(server_text(PLATFORM_SERVER))
    server = edited_server(PLATFORM_SERVER, tools=[*body["tools"], MERGE_ENTRY])
    index = full_index()
    return Index(index.kinds, {**index.servers, PLATFORM_SERVER: server}, index.skills)


def _check_against(index: Index, fixture: str, **changes: Any) -> Report:
    family = edited(fixture, **changes)
    issues = Issues()
    check_family(family, fixture, index, issues, host())
    return Report(family.name, f"families/{fixture}/family.yaml", issues.frozen())


def test_the_platform_merge_is_never_granted_by_name() -> None:
    """`main` may carry no branch protection, so a family that could merge on
    agent-registry would land its own grants, and `caregiver` would apply them
    with nobody looking."""
    report = _check_against(_merge_declared(), "agent-control", tools={PLATFORM_SERVER: [MERGE]})
    assert "never granted" in messages(report)
    assert f"tools.{PLATFORM_SERVER}[0]" in {issue.loc for issue in errors(report)}


def test_the_platform_merge_is_never_granted_through_all() -> None:
    """`all` expands against the server file at read time (§3.4 rule 4), so a
    server file that declares the merge again must not re-grant it quietly."""
    report = _check_against(_merge_declared(), "agent-control", tools={PLATFORM_SERVER: "all"})
    assert "never granted" in messages(report)


def test_no_family_holds_the_platform_merge() -> None:
    """The rule fences the server, not the allowlist."""
    report = _check_against(_merge_declared(), "chat", tools={PLATFORM_SERVER: [MERGE]})
    assert "never granted" in messages(report)


def test_the_platform_family_keeps_every_other_github_tool() -> None:
    """The real shape: the platform server declares no merge tool, and `all`
    grants every tool it does declare."""
    report = check("agent-control")
    assert not errors(report), messages(report)
    assert MERGE not in load_server(PLATFORM_SERVER).tool_names()


def test_github_code_keeps_its_merge() -> None:
    """Rule 7 fences the platform's server only. `github-code` cannot reach
    the three platform repos at all (contract 01b §8.2)."""
    report = check("code", tools={"github-code": [MERGE]})
    assert not errors(report), messages(report)


# --- 3.6 delegates -----------------------------------------------------------

DELEGATE_CASES = (
    ({"delegates": ["chat"]}, "may not delegate to itself"),
    ({"delegates": ["no-such-family"]}, "not a family in this registry"),
    ({"delegates": ["scrum-lead"]}, "a delegate must be thin"),
)


def test_thin_family_delegates_must_be_empty() -> None:
    report = check("vault-oracle", delegates=["code-sandbox"])
    assert "delegates must be empty" in messages(report)


@pytest.mark.parametrize(("changes", "expect"), DELEGATE_CASES)
def test_delegate_fences(changes: dict[str, object], expect: str) -> None:
    report = check("chat", **changes)
    assert expect in messages(report)


def test_duplicate_delegate_is_an_error() -> None:
    report = check("chat", delegates=["vault-oracle", "vault-oracle"])
    assert "is named twice" in messages(report)


# --- 3.6.1 max_inflight_delegations ------------------------------------------


def test_max_inflight_delegations_defaults_to_two() -> None:
    assert load("chat").max_inflight_delegations == 2


@pytest.mark.parametrize("cap", [0, 9])
def test_max_inflight_delegations_range(cap: int) -> None:
    report = check("chat", max_inflight_delegations=cap)
    assert "is outside 1 to 8" in messages(report)


def test_max_inflight_delegations_is_clean_inside_its_range() -> None:
    report = check("chat", max_inflight_delegations=3)
    assert not report.issues


def test_a_cap_without_delegates_is_a_warning_not_an_error() -> None:
    """§3.6.1 rule 3. The number only bites on a delegate call, so with an
    empty `delegates` it cannot. A warning never blocks an apply (§7 rule 4),
    which keeps `delegates: []` and a leftover cap from making a family
    invalid."""
    report = check("chat", delegates=[], max_inflight_delegations=3)
    assert not errors(report)
    assert "no delegate call to bound" in messages(report)


def test_the_default_cap_without_delegates_says_nothing() -> None:
    """A family that never set the field has nothing to be warned about."""
    report = check("chat", delegates=[])
    assert "max_inflight_delegations" not in messages(report)


# --- 3.7 egress --------------------------------------------------------------

EGRESS_CASES = (
    (["192.0.2.10"], "is an IP literal"),
    (["*.github.com"], "holds a wildcard, which is ungrantable"),
    (["github.com:99999"], "has a port outside 1 to 65535"),
    (["_bad_host_"], "is not a hostname or hostname:port"),
    # §3.7 rule 3: a port is ASCII digits. U+00B2 and U+0661 are digits to
    # `str.isdigit`, and `int` reads only the second one.
    (["github.com:\u00b2"], "has a port outside 1 to 65535"),
    (["github.com:\u0661"], "has a port outside 1 to 65535"),
    # `hostname:port` with no port is neither of the two forms.
    (["github.com:"], "has a port outside 1 to 65535"),
    # More digits than the interpreter converts.
    (["github.com:" + "9" * 5000], "has a port outside 1 to 65535"),
)


@pytest.mark.parametrize(("egress", "expect"), EGRESS_CASES)
def test_egress_fences(egress: list[str], expect: str) -> None:
    report = check("code", egress=egress)
    assert expect in messages(report)


def test_empty_egress_is_the_normal_case() -> None:
    assert not errors(check("chat"))


@pytest.mark.parametrize("entry", ["github.com", "github.com:1", "github.com:65535", "a.b:00443"])
def test_a_hostname_with_a_port_in_range_is_allowed(entry: str) -> None:
    assert not errors(check("code", egress=[entry]))


# --- 3.8 shell and sandbox_tools ---------------------------------------------

RUNTIME_CASES = (
    (["bash"], "'bash' is never a sandbox tool"),
    (["fly"], "not a sandbox tool"),
    (["read", "read"], "is listed twice"),
)


@pytest.mark.parametrize(("tools", "expect"), RUNTIME_CASES)
def test_sandbox_tools_fences(tools: list[str], expect: str) -> None:
    report = check("chat", sandbox_tools=tools)
    assert expect in messages(report)


def test_empty_sandbox_tools_is_valid() -> None:
    assert not errors(check("vault-oracle", sandbox_tools=[]))


def test_codemode_is_a_sandbox_tool() -> None:
    # pi's built-in extension that runs scripts over the other granted tools.
    assert not errors(check("code", sandbox_tools=["read", "grep", "codemode"]))


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_system_prompt_modes_are_valid(mode: str) -> None:
    assert not errors(check("chat", system_prompt=mode))


def test_system_prompt_rejects_an_unknown_mode() -> None:
    report = check("chat", system_prompt="prepend")
    assert "'prepend' is not a system prompt mode; use one of append, replace" in messages(report)


# --- 3.9 sandbox ---------------------------------------------------------

SANDBOX_CASES = (
    ({"cpus": 0, "memory": "2g"}, "is outside 1 to 8"),
    ({"cpus": 9, "memory": "2g"}, "is outside 1 to 8"),
    ({"cpus": 2, "memory": "2x"}, "must match [1-9][0-9]*[mg]"),
    ({"cpus": 2, "memory": "64m"}, "is outside 256m to 16g"),
    ({"cpus": 2, "memory": "32g"}, "is outside 256m to 16g"),
    # A count of more digits than the interpreter converts is past the range.
    ({"cpus": 2, "memory": "9" * 5000 + "g"}, "is outside 256m to 16g"),
    ({"cpus": 2, "memory": "9" * 5000 + "m"}, "is outside 256m to 16g"),
    ({"cpus": 2, "memory": "2g", "max_resident_processes": 0}, "is outside 1 to 32"),
    ({"cpus": 2, "memory": "2g", "max_resident_processes": 33}, "is outside 1 to 32"),
)


@pytest.mark.parametrize(("sandbox", "expect"), SANDBOX_CASES)
def test_sandbox_fences(sandbox: dict[str, object], expect: str) -> None:
    report = check("chat", sandbox=sandbox)
    assert expect in messages(report)


def test_too_many_resident_processes_for_memory_is_a_warning_not_an_error() -> None:
    report = check("chat", sandbox={"cpus": 2, "memory": "256m", "max_resident_processes": 30})
    assert not errors(report)
    assert "held-open processes need about" in messages(report)


def test_the_default_image_flavor_is_base() -> None:
    assert load("chat").sandbox.image == "base"


@pytest.mark.parametrize("flavor", ["base", "python"])
def test_every_flavor_the_platform_builds_is_accepted(flavor: str) -> None:
    assert not errors(check("chat", sandbox={"cpus": 2, "memory": "2g", "image": flavor}))


def test_an_unknown_image_flavor_is_an_error_naming_the_set() -> None:
    report = check("chat", sandbox={"cpus": 2, "memory": "2g", "image": "rust"})
    assert errors(report)
    assert "sandbox.image" in messages(report)
    assert "base, python" in messages(report)


def test_an_image_reference_is_not_a_flavor() -> None:
    """Invariant 10: only the platform changes the platform. A registry file
    that could name an image would choose what code runs."""
    report = check("chat", sandbox={"cpus": 2, "memory": "2g", "image": "127.0.0.1:5000/mine:1"})
    assert errors(report)


# --- 3.10 skills -----------------------------------------------------------

SKILLS_CASES = (
    (["grill-me", "grill-me"], "is granted twice"),
    (["Grill-Me"], "not a skill name"),
    (["no-such-skill"], "has no skills/no-such-skill/SKILL.md"),
)


@pytest.mark.parametrize(("skills", "expect"), SKILLS_CASES)
def test_skills_fences(skills: list[str], expect: str) -> None:
    report = check("chat", skills=skills)
    assert expect in messages(report)


# --- 3.11 approval -----------------------------------------------------------


def test_approval_duplicate_entry_is_an_error() -> None:
    report = check("agent-control", approval=["release", "release"])
    assert "is listed twice" in messages(report)


def test_approval_of_an_ungranted_verb_is_an_error() -> None:
    report = check("chat", approval=["release"])
    assert "is not a verb this file grants" in messages(report)


def test_invoke_agent_approval_needs_delegates() -> None:
    report = check("vault-oracle", approval=["invoke_agent"])
    assert "invoke_agent is granted by a non-empty 'delegates'" in messages(report)


def test_invoke_agent_approval_is_allowed_with_delegates() -> None:
    report = check("chat", approval=["invoke_agent"])
    assert not errors(report)


def test_approval_of_an_ungranted_server_is_an_error() -> None:
    report = check("chat", approval=["mail__send_standup_email"])
    assert "is not a server this file grants" in messages(report)


def test_approval_of_an_ungranted_tool_is_an_error() -> None:
    report = check("chat", approval=["kagi__kagi_summarize"])
    assert "is not a tool this file grants from" in messages(report)


def test_approval_wildcard_and_a_named_tool_overlap() -> None:
    report = check("chat", approval=["kagi__*", "kagi__kagi_extract"])
    assert "overlaps" in messages(report)


# --- 3.12 job (thin only) ---------------------------------------------------

JOB_CASES = (
    ({"timeout": "10x"}, "must match [1-9][0-9]*[smh]"),
    ({"timeout": "0s"}, "must match [1-9][0-9]*[smh]"),  # [1-9] leads, so "0s" is a bad shape
    ({"timeout": "2h"}, "outside 1s to 1h"),
    ({"timeout": "9" * 5000 + "s"}, "outside 1s to 1h"),
)


@pytest.mark.parametrize(("job", "expect"), JOB_CASES)
def test_job_fences(job: dict[str, object], expect: str) -> None:
    report = check("vault-oracle", job=job)
    assert expect in messages(report)


def test_job_on_a_non_thin_family_is_an_error() -> None:
    report = check("chat", job={"timeout": "60s"})
    assert "'job' is thin only" in messages(report)


def test_job_timeout_past_the_delegate_limit_is_a_warning() -> None:
    report = check("vault-oracle", job={"timeout": "180s"})
    assert not errors(report)
    assert "past the PEP's 120s delegate limit" in messages(report)


# --- 3.13 triggers (autonomous only) -----------------------------------------


def test_triggers_on_a_non_autonomous_family_is_an_error() -> None:
    report = check("chat", triggers=[{"cron": "@hourly"}])
    assert "'triggers' is autonomous only" in messages(report)


def test_autonomous_family_needs_at_least_one_trigger() -> None:
    report = check("scrum-lead", triggers=[])
    assert "needs at least one trigger" in messages(report)


TRIGGER_CASES: tuple[tuple[list[dict[str, Any]], str], ...] = (
    ([{"cron": "@hourly", "webhook": "x"}], "exactly one of 'cron', 'webhook' or 'enqueue'"),
    ([{}], "exactly one of 'cron', 'webhook' or 'enqueue'"),
    ([{"cron": "@hourly", "enqueue": True}], "exactly one of 'cron', 'webhook' or 'enqueue'"),
    ([{"cron": "not a cron"}], "not a five-field cron expression"),
    ([{"webhook": "Bad_Name"}], "must match [a-z][a-z0-9-]"),
    # §3.13 rule 5: absence is denial, so a false trigger is not a trigger.
    ([{"enqueue": False}], "'enqueue: false' is not a trigger"),
    ([{"enqueue": True}, {"enqueue": True}], "says the same thing twice"),
)


@pytest.mark.parametrize(("triggers", "expect"), TRIGGER_CASES)
def test_trigger_fences(triggers: list[dict[str, object]], expect: str) -> None:
    report = check("scrum-lead", triggers=triggers)
    assert expect in messages(report)


def test_a_five_field_cron_is_valid() -> None:
    report = check("scrum-lead", triggers=[{"cron": "0 * * * *"}])
    assert not errors(report)


def test_an_enqueue_trigger_is_a_valid_form() -> None:
    """§3.13's third form. A dispatch-only family has no timer and no webhook,
    and inventing one for it would open a route nothing needs."""
    report = check("finance-worker", triggers=[{"enqueue": True}])
    assert not errors(report)


def test_an_enqueue_trigger_nobody_targets_is_a_warning(tmp_path: Path) -> None:
    """§3.13 rule 6, checked at the registry: the caller and the target can
    land in either order, so a dead trigger never blocks an apply."""
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    target = registry_copy / "families" / "finance-worker" / "family.yaml"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "- webhook: finance-worker-dispatch", "- enqueue: true"
        ),
        encoding="utf-8",
    )

    targeted = load_registry(registry_copy, host())
    assert targeted.reports["finance-worker"].ok
    assert "no family enqueues" not in messages(targeted.reports["finance-worker"])

    lead = registry_copy / "families" / "scrum-lead" / "family.yaml"
    lead.write_text(
        lead.read_text(encoding="utf-8").replace("      - finance-worker\n", ""), encoding="utf-8"
    )

    orphaned = load_registry(registry_copy, host())
    assert orphaned.reports["finance-worker"].ok  # a warning never blocks an apply
    assert "no family enqueues" in messages(orphaned.reports["finance-worker"])


def test_two_families_cannot_claim_the_same_webhook(tmp_path: Path) -> None:
    """§5.6 rule 1, checked at the registry, not the single-file level: both
    reports name the other owner. Edits a throwaway copy of the fixture
    registry, never the shared fixtures other tests read."""
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    clean = load_registry(registry_copy, host())
    assert clean.reports["finance-worker"].ok  # today's fixture: no collision yet

    target = registry_copy / "families" / "networking-worker" / "family.yaml"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "webhook: networking-dispatch", "webhook: finance-worker-dispatch"
        ),
        encoding="utf-8",
    )

    collided = load_registry(registry_copy, host())
    assert "also claimed by" in messages(collided.reports["finance-worker"])
    assert "also claimed by" in messages(collided.reports["networking-worker"])


# --- 3.14 max_running_turns (autonomous only) --------------------------------


def test_max_running_turns_on_a_non_autonomous_family_is_an_error() -> None:
    report = check("chat", max_running_turns=2)
    assert "'max_running_turns' is autonomous only" in messages(report)


@pytest.mark.parametrize("turns", [0, 9])
def test_max_running_turns_range(turns: int) -> None:
    report = check("scrum-lead", max_running_turns=turns)
    assert "is outside 1 to 8" in messages(report)


# --- 3.15 quiet (autonomous only) --------------------------------------------

DAILY: dict[str, Any] = {
    "call": "mail__send_standup_email",
    "hour": 16,
    "zone": "America/Phoenix",
    "weekdays_only": True,
}


def test_quiet_on_a_non_autonomous_family_is_an_error() -> None:
    report = check("chat", quiet={})
    assert "'quiet' is autonomous only" in messages(report)


def test_a_full_quiet_block_is_clean() -> None:
    report = check("scrum-lead", quiet={"board": "board-lead", "daily": DAILY, "floor_hours": 24})
    assert not report.issues, messages(report)


def test_a_verb_is_a_daily_call_too() -> None:
    report = check("scrum-lead", quiet={"daily": {**DAILY, "call": "ha_call"}})
    assert not errors(report), messages(report)


QUIET_CASES: tuple[tuple[dict[str, Any], str], ...] = (
    ({"board": "kagi"}, "'kagi' is not a server this file grants"),
    ({"board": "mail"}, "'mail' does not grant 'survey_board'"),
    ({"daily": {**DAILY, "call": "mail__send_invoice"}}, "'mail__send_invoice' is not a verb"),
    ({"daily": {**DAILY, "call": "release"}}, "'release' is not a verb"),
    ({"daily": {**DAILY, "hour": 24}}, "24 is outside 0 to 23"),
    ({"daily": {**DAILY, "zone": "Mars/Olympus"}}, "is not an IANA time zone"),
    ({"floor_hours": 0}, "0 is outside 1 to 168"),
    ({"floor_hours": 169}, "169 is outside 1 to 168"),
)


@pytest.mark.parametrize(("quiet", "expect"), QUIET_CASES)
def test_quiet_fences(quiet: dict[str, Any], expect: str) -> None:
    report = check("scrum-lead", quiet=quiet)
    assert expect in messages(report)


def test_quiet_needs_job_status_beside_enqueue() -> None:
    """§3.15 item 2: a family that starts jobs is never quiet while one
    runs, and `job_status` is how the check sees them."""
    verbs = {"embed": {}, "enqueue": {"targets": ["finance-worker"]}}
    report = check("scrum-lead", quiet={}, verbs=verbs)
    assert "reads this family's jobs with 'job_status'" in messages(report)


def test_quiet_with_no_cron_trigger_is_a_warning() -> None:
    """It gates nothing, and says so without refusing the file."""
    report = check("scrum-lead", quiet={}, triggers=[{"enqueue": True}])
    assert not errors(report)
    assert "has no cron trigger" in messages(report)


# --- unknown fields (contract 01 §7 rule 1) ----------------------------------


def test_unknown_top_level_field_is_refused() -> None:
    family, issues = parse_family(
        "name: chat\nkind: attended\ndescription: x\n"
        "model: {router: agent-router, budget_usd_per_day: 1}\n"
        "bogus_field: 1\n"
    )
    assert family is None
    assert any("unknown field 'bogus_field'" in issue.msg for issue in issues)
    assert any("known fields here are" in issue.msg for issue in issues)


def test_unknown_field_suggests_the_closest_name() -> None:
    family, issues = parse_family(
        "name: chat\nkind: attended\ndescriptoin: x\ndescription: x\n"
        "model: {router: agent-router, budget_usd_per_day: 1}\n"
    )
    assert family is None
    assert any("did you mean 'description'?" in issue.msg for issue in issues)


def test_unknown_nested_field_names_its_container() -> None:
    family, issues = parse_family(
        "name: chat\nkind: attended\ndescription: x\n"
        "model: {router: agent-router, budget_usd_per_day: 1, extra: 1}\n"
    )
    assert family is None
    assert any("model.extra" in f"{issue.loc}" for issue in issues)


def test_one_parse_reports_every_violation_it_can_see() -> None:
    """§7 rule 2: not just the first."""
    report = check("chat", name="Chat!", kind="bogus")
    assert len(errors(report)) >= 2


# --- document shape (contract 01 §1) -----------------------------------------


def test_a_non_mapping_document_is_refused() -> None:
    family, issues = parse_family("- just\n- a\n- list\n")
    assert family is None
    assert any("top level must be a mapping" in issue.msg for issue in issues)


def test_two_documents_in_one_file_is_refused() -> None:
    family, issues = parse_family("name: chat\n---\nname: code\n")
    assert family is None
    assert any("one YAML document per file" in issue.msg for issue in issues)


def test_unparsable_yaml_is_refused() -> None:
    family, issues = parse_family("name: [unclosed\n")
    assert family is None
    assert any("YAML will not parse" in issue.msg for issue in issues)


# --- a text with no value (contract 01 §7, invariant 19) ---------------------

_HEAD = (
    "name: chat\nkind: attended\ndescription: x\n"
    "model: {router: agent-router, budget_usd_per_day: 1}\n"
)

#: YAML that the reader has no value for. Each one is past the syntax check.
NO_VALUE_TEXTS = {
    # A decimal integer of more digits than the interpreter converts.
    "decimal-integer": "max_inflight_delegations: " + "9" * 5000 + "\n",
    # The same size in base 16, which the reader converts and nothing prints.
    "base-16-integer": "max_inflight_delegations: 0x" + "f" * 4000 + "\n",
    "base-16-nested": "sandbox: { cpus: 0x" + "f" * 4000 + " }\n",
    "base-16-key": "? 0x" + "f" * 4000 + "\n: 1\n",
    # A date that the calendar does not hold.
    "date": "shell: 2001-02-30\n",
    # A tag on a text that is no value of the tag.
    "int-tag-word": "sandbox: { cpus: !!int two }\n",
    "int-tag-empty": 'sandbox: { cpus: !!int "" }\n',
    "float-tag-word": "sandbox: { cpus: !!float two }\n",
    "bool-tag-word": "shell: !!bool maybe\n",
    "timestamp-tag-word": "shell: !!timestamp soon\n",
    # A base 60 float past the largest float.
    "base-60-float": "sandbox: { cpus: 1" + ":0" * 200 + ".5 }\n",
}


@pytest.mark.parametrize("case", NO_VALUE_TEXTS)
def test_a_text_with_no_value_is_refused(case: str) -> None:
    family, issues = parse_family(_HEAD + NO_VALUE_TEXTS[case])
    assert family is None
    assert [issue.loc for issue in issues] == ["<document>"]
    assert "YAML will not parse" in issues[0].msg


def test_a_text_that_nests_too_deep_is_refused() -> None:
    family, issues = parse_family(_HEAD + "skills: " + "[" * 10_000 + "]" * 10_000 + "\n")
    assert family is None
    assert [issue.msg for issue in issues] == ["YAML will not parse: the text nests too deep"]


def test_an_anchor_that_holds_itself_is_read_to_its_end() -> None:
    family, issues = parse_family(_HEAD + "skills: &again [*again]\n")
    assert family is None
    assert [issue.loc for issue in issues] == ["skills[0]"]


def test_one_file_with_no_value_does_not_stop_the_registry_read(tmp_path: Path) -> None:
    """§7 rule 5: the other families keep their reports."""
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    broken = registry_copy / "families" / "no-value"
    broken.mkdir()
    (broken / "family.yaml").write_text(_HEAD + NO_VALUE_TEXTS["decimal-integer"], encoding="utf-8")

    loaded = load_registry(registry_copy, host())
    assert "YAML will not parse" in messages(loaded.reports["no-value"])
    assert not loaded.reports["no-value"].ok
    assert "no-value" not in loaded.families
    assert loaded.reports["chat"].ok
    assert "chat" in loaded.families
