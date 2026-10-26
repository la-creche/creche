"""`policy.toml` for the Rust chaperone: the types, the renderer and
`build_app` (chaperone design sections 6 and 12)."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest
from agent_family import FamilyFile, Index, load_registry
from caregiver.grants import DEFAULT_MAX_OPEN_GATES, DEFAULT_PEP_RPM
from caregiver.policy_file import (
    App,
    Approval,
    ArgumentFence,
    AttendanceSection,
    FenceKind,
    Limits,
    PluginSections,
    PolicyFile,
    PolicyShapeError,
    ToolGrant,
    UpstreamGrant,
    build_app,
    render_policy,
)
from caregiver_helpers import chat_family

#: The fixture registry of the package `family`.
REGISTRY: Final = Path(__file__).resolve().parents[2] / "family" / "tests" / "fixtures" / "registry"
GOLDEN: Final = Path(__file__).resolve().parent / "policy_golden"
REV: Final = "3f5dcabe"
DIGEST: Final = "ab" * 32
LIMITS: Final = Limits(calls_per_minute=60, max_pending_gates=10)

_REGISTRY = load_registry(REGISTRY)
INDEX: Final = Index(
    kinds={name: family.kind for name, family in _REGISTRY.families.items()},
    servers=_REGISTRY.servers,
    skills=_REGISTRY.skills,
)


def digest_of(family: str) -> str:
    """A fixed digest per family, unique across the fixture registry."""
    return hashlib.sha256(family.encode("ascii")).hexdigest()


def rendered(family: FamilyFile) -> str:
    app = build_app(family, INDEX, (digest_of(family.name),), rev=REV)
    return render_policy(PolicyFile({family.name: app}))


def app_table(family: FamilyFile) -> dict[str, Any]:
    """The app table of `family`, as a TOML reader reads the rendering."""
    return tomllib.loads(rendered(family))["apps"][family.name]


def test_policy_app_scrum_lead() -> None:
    assert rendered(_REGISTRY.families["scrum-lead"]) == (
        "version = 1\n"
        "\n"
        "[apps.scrum-lead]\n"
        'note = "registry 3f5dcabe"\n'
        f'token_sha256 = ["{digest_of("scrum-lead")}"]\n'
        "limits = { calls_per_minute = 60, max_pending_gates = 10 }\n"
        "\n"
        "[apps.scrum-lead.grants.attendance]\n"
        "max_inflight = 2\n"
        "\n"
        "[apps.scrum-lead.grants.attendance.tools]\n"
        'enqueue = { approval = "required" }\n'
        "job_status = {}\n"
        "\n"
        "[apps.scrum-lead.grants.board-lead.tools]\n"
        "append_dev_notes = {}\n"
        "comment_on_ticket = {}\n"
        "file_child_ticket = {}\n"
        "file_ticket = {}\n"
        "get_ticket = {}\n"
        "label_epic = {}\n"
        "mark_blocked = {}\n"
        "mark_refined = {}\n"
        "set_points = {}\n"
        "survey_board = {}\n"
        "\n"
        "[apps.scrum-lead.grants.mail.tools]\n"
        "send_standup_email = {}\n"
        "\n"
        "[apps.scrum-lead.plugins.attendance]\n"
        'enqueue_targets = ["agent-control-worker", "finance-worker", '
        '"home-assistant-worker", "networking-worker"]\n'
    )


@pytest.mark.parametrize("name", sorted(_REGISTRY.families))
def test_fixture_family_matches_its_golden(name: str) -> None:
    golden = (GOLDEN / f"{name}.toml").read_text(encoding="utf-8")
    assert rendered(_REGISTRY.families[name]) == golden


def test_every_fixture_family_has_a_golden() -> None:
    assert sorted(path.stem for path in GOLDEN.glob("*.toml")) == sorted(_REGISTRY.families)


def test_named_tools_are_granted_with_no_fence() -> None:
    table = app_table(chat_family(tools={"kagi": ["kagi_search_fetch"]}))
    assert table["grants"] == {"kagi": {"tools": {"kagi_search_fetch": {}}}}


def test_all_expands_against_the_server_file() -> None:
    table = app_table(chat_family(tools={"ha-read": "all"}))
    assert sorted(table["grants"]["ha-read"]["tools"]) == sorted(
        INDEX.servers["ha-read"].tool_names()
    )


def test_a_server_with_no_tool_gets_no_grant() -> None:
    assert "grants" not in app_table(chat_family(tools={"kagi": []}))


def test_an_approval_entry_gates_one_tool() -> None:
    family = chat_family(
        tools={"kagi": ["kagi_search_fetch", "kagi_extract"]}, approval=["kagi__kagi_extract"]
    )
    assert app_table(family)["grants"] == {
        "kagi": {"tools": {"kagi_extract": {"approval": "required"}, "kagi_search_fetch": {}}}
    }


def test_a_wildcard_approval_gates_each_granted_tool() -> None:
    family = chat_family(tools={"ha-read": "all"}, approval=["ha-read__*"])
    tools = app_table(family)["grants"]["ha-read"]["tools"]
    assert tools == {
        name: {"approval": "required"} for name in INDEX.servers["ha-read"].tool_names()
    }


def test_enqueue_maps_to_its_tool_and_its_targets() -> None:
    table = app_table(chat_family(verbs={"enqueue": {"targets": ["scrum-lead"]}}))
    assert table["grants"] == {"attendance": {"max_inflight": 2, "tools": {"enqueue": {}}}}
    assert table["plugins"] == {"attendance": {"enqueue_targets": ["scrum-lead"]}}


def test_an_enqueue_approval_gates_the_enqueue_tool() -> None:
    family = chat_family(verbs={"enqueue": {"targets": ["scrum-lead"]}}, approval=["enqueue"])
    tools = app_table(family)["grants"]["attendance"]["tools"]
    assert tools == {"enqueue": {"approval": "required"}}


def test_delegates_map_to_invoke_agent_with_the_delegate_bound() -> None:
    table = app_table(chat_family(delegates=["vault-oracle"]))
    assert table["grants"] == {
        "attendance": {"timeout_s": 120, "max_inflight": 2, "tools": {"invoke_agent": {}}}
    }
    assert table["plugins"] == {"attendance": {"delegates": ["vault-oracle"]}}


def test_an_invoke_agent_approval_gates_the_delegate_tool() -> None:
    family = chat_family(delegates=["vault-oracle"], approval=["invoke_agent"])
    tools = app_table(family)["grants"]["attendance"]["tools"]
    assert tools == {"invoke_agent": {"approval": "required"}}


def test_job_status_maps_to_its_tool_with_no_section() -> None:
    table = app_table(chat_family(verbs={"job_status": {}}))
    assert table["grants"] == {"attendance": {"max_inflight": 2, "tools": {"job_status": {}}}}
    assert "plugins" not in table


def test_max_inflight_comes_from_the_family_file() -> None:
    table = app_table(chat_family(delegates=["vault-oracle"], max_inflight_delegations=5))
    assert table["grants"]["attendance"]["max_inflight"] == 5


def test_limits_keep_the_values_of_the_grant_file() -> None:
    assert app_table(chat_family())["limits"] == {
        "calls_per_minute": DEFAULT_PEP_RPM,
        "max_pending_gates": DEFAULT_MAX_OPEN_GATES,
    }


def test_release_maps_to_request_and_drops_the_components() -> None:
    table = app_table(chat_family(verbs={"release": {"components": ["caregiver"]}}))
    assert table["grants"] == {"release": {"tools": {"request": {}}}}
    assert "caregiver" not in rendered(
        chat_family(verbs={"release": {"components": ["caregiver"]}})
    )


def test_embed_and_ha_call_grant_nothing() -> None:
    family = chat_family(
        verbs={"embed": {}, "ha_call": {"allow": [{"domain": "light", "service": "turn_on"}]}},
        approval=["ha_call"],
    )
    table = app_table(family)
    assert "grants" not in table
    assert "plugins" not in table


def test_the_note_names_the_registry_revision() -> None:
    assert app_table(chat_family())["note"] == f"registry {REV}"


def test_build_app_leaves_argument_fences_empty() -> None:
    app = build_app(_REGISTRY.families["code"], INDEX, (DIGEST,), rev=REV)
    assert all(not grant.arguments for up in app.grants.values() for grant in up.tools.values())


@pytest.mark.parametrize("server", ["attendance", "release"])
def test_a_server_named_like_a_plugin_upstream_is_refused(server: str) -> None:
    with pytest.raises(PolicyShapeError, match="plugin upstream"):
        build_app(chat_family(tools={server: ["x"]}), INDEX, (DIGEST,), rev=REV)


def test_two_digests_render_in_order() -> None:
    app = App(token_sha256=("01" * 32, "02" * 32), limits=LIMITS)
    table = tomllib.loads(render_policy(PolicyFile({"chat": app})))
    assert table["apps"]["chat"]["token_sha256"] == ["01" * 32, "02" * 32]


def test_a_tool_name_with_a_dot_gets_a_quoted_key() -> None:
    grant = UpstreamGrant(tools={"tasks.list": ToolGrant()})
    text = render_policy(PolicyFile({"chat": App((DIGEST,), LIMITS, {"vikunja": grant})}))
    assert '"tasks.list" = {}\n' in text
    assert tomllib.loads(text)["apps"]["chat"]["grants"]["vikunja"]["tools"] == {"tasks.list": {}}


def test_every_field_of_a_tool_grant_renders() -> None:
    tool = ToolGrant(
        approval=Approval.REQUIRED,
        arguments={
            "owner": ArgumentFence(FenceKind.ALLOW, ("la-creche",)),
            "n": ArgumentFence(FenceKind.DENY, (7, True)),
        },
        note='say "why"\n',
    )
    grant = UpstreamGrant(tools={"t": tool}, timeout_s=60, max_inflight=3, note="up")
    app = App(
        (DIGEST,),
        LIMITS,
        {"gh": grant},
        PluginSections(AttendanceSection(("vault-oracle",))),
        "app",
    )
    table = tomllib.loads(render_policy(PolicyFile({"chat": app})))["apps"]["chat"]
    assert table["grants"]["gh"] == {
        "note": "up",
        "timeout_s": 60,
        "max_inflight": 3,
        "tools": {
            "t": {
                "note": 'say "why"\n',
                "approval": "required",
                "arguments": {"n": {"deny": [7, True]}, "owner": {"allow": ["la-creche"]}},
            }
        },
    }
    assert table["plugins"] == {"attendance": {"delegates": ["vault-oracle"]}}
    assert table["note"] == "app"


def test_a_control_character_is_escaped() -> None:
    text = render_policy(PolicyFile({"chat": App((DIGEST,), LIMITS, note="a\x00b\x7f")}))
    assert tomllib.loads(text)["apps"]["chat"]["note"] == "a\x00b\x7f"


@pytest.mark.parametrize("name", ["a b", "tool/x", "", "é", "x" * 129])
def test_a_tool_name_outside_the_rust_grammar_is_refused(name: str) -> None:
    with pytest.raises(PolicyShapeError, match="a tool name"):
        UpstreamGrant(tools={name: ToolGrant()})


@pytest.mark.parametrize("name", ["ha__call", "Kagi", "-kagi", "ka.gi", "x" * 65])
def test_a_server_name_outside_the_rust_grammar_is_refused(name: str) -> None:
    grant = UpstreamGrant(tools={"t": ToolGrant()})
    with pytest.raises(PolicyShapeError, match="a plugin name"):
        App((DIGEST,), LIMITS, {name: grant})


def test_an_argument_name_with_a_slash_is_refused() -> None:
    with pytest.raises(PolicyShapeError, match="an argument name"):
        ToolGrant(arguments={"owner/repo": ArgumentFence(FenceKind.DENY, ("x",))})


def test_an_app_id_outside_the_slug_grammar_is_refused() -> None:
    with pytest.raises(PolicyShapeError, match="an app ID"):
        PolicyFile({"Chat": App((DIGEST,), LIMITS)})


@pytest.mark.parametrize(
    "digests", [(), ("ab" * 32,) * 2, ("01" * 32, "02" * 32, "03" * 32), ("AB" * 32,), ("ab",)]
)
def test_a_bad_digest_list_is_refused(digests: tuple[str, ...]) -> None:
    with pytest.raises(PolicyShapeError):
        App(digests, LIMITS)


@pytest.mark.parametrize(
    ("timeout_s", "max_inflight"), [(0, None), (121, None), (None, 0), (None, 9)]
)
def test_a_bound_out_of_range_is_refused(timeout_s: int | None, max_inflight: int | None) -> None:
    with pytest.raises(PolicyShapeError):
        UpstreamGrant(tools={"t": ToolGrant()}, timeout_s=timeout_s, max_inflight=max_inflight)


def test_an_empty_grant_or_fence_is_refused() -> None:
    with pytest.raises(PolicyShapeError, match="at least one tool"):
        UpstreamGrant(tools={})

    with pytest.raises(PolicyShapeError, match="at least one value"):
        ArgumentFence(FenceKind.ALLOW, ())


def test_a_limit_outside_u32_is_refused() -> None:
    with pytest.raises(PolicyShapeError, match="calls_per_minute"):
        Limits(calls_per_minute=-1, max_pending_gates=0)


def test_the_attendance_section_holds_the_fence_rules() -> None:
    with pytest.raises(PolicyShapeError, match="a family of delegates"):
        AttendanceSection(delegates=("Chat",))

    with pytest.raises(PolicyShapeError, match="more than 64"):
        AttendanceSection(enqueue_targets=tuple(f"f{n:02}" for n in range(65)))
