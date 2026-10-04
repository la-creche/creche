"""The grant file builder expands `all` and `<server>__*` exactly once, and
leaves everything else untouched (contract 04 section 1.2)."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path

from agent_family import FamilyFile, Index
from agent_family.grammar import DEFAULT_MAX_INFLIGHT_DELEGATIONS
from agent_family.server import McpServerFile
from caregiver.grants import (
    DEFAULT_MAX_OPEN_GATES,
    DEFAULT_PEP_RPM,
    GRANT_FILE_MODE,
    GRANT_FILE_VERSION,
    GrantFile,
    build_grant_file,
    delete_grant_file,
    rewrite_digests,
    write_grant_file,
)

KAGI = McpServerFile.model_validate(
    {
        "name": "kagi",
        "identity": "The fleet's Kagi API key.",
        "install": {"source": "pypi", "package": "kagimcp", "version": "1.0.2"},
        "run": {"entrypoint": "kagimcp", "env": {"KAGI_API_KEY": "secret:kagi_api_key"}},
        "tools": [
            {"name": "kagi_search_fetch", "description": "Search."},
            {"name": "kagi_extract", "description": "Extract."},
        ],
    }
)


def family(**overrides: object) -> FamilyFile:
    body: dict[str, object] = {
        "name": "chat",
        "kind": "attended",
        "description": "Test family.",
        "model": {"router": "agent-router", "budget_usd_per_day": 15},
    }
    body.update(overrides)
    return FamilyFile.model_validate(body)


def index(servers: Mapping[str, McpServerFile] | None = None) -> Index:
    resolved = servers if servers is not None else {"kagi": KAGI}
    return Index(kinds={}, servers=resolved, skills=frozenset())


def test_basic_fields() -> None:
    grant = build_grant_file(family(), index(), rev="reg-abc123", token_sha256=("deadbeef",))
    assert grant.family == "chat"
    assert grant.rev == "reg-abc123"
    assert grant.token_sha256 == ("deadbeef",)
    assert grant.model_alias == "agent-router"
    assert grant.delegates == ()
    assert grant.approval == ()


def test_as_json_carries_the_version_and_the_manager_owned_limits() -> None:
    grant = build_grant_file(family(), index(), rev="reg-abc123", token_sha256=("deadbeef",))
    body = grant.as_json()
    assert body["version"] == GRANT_FILE_VERSION
    assert body["limits"] == {
        "pep_rpm": DEFAULT_PEP_RPM,
        "max_inflight_delegations": DEFAULT_MAX_INFLIGHT_DELEGATIONS,
        "max_open_gates": DEFAULT_MAX_OPEN_GATES,
    }


def test_the_inflight_cap_comes_from_the_family_file() -> None:
    """Contract 01 §3.6.1 and contract 04 §1.2: the caller family's own file
    carries this number, and `caregiver` copies it."""
    f = family(delegates=[], max_inflight_delegations=5)
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.max_inflight_delegations == 5
    assert grant.as_json()["limits"]["max_inflight_delegations"] == 5


def test_a_family_file_with_no_cap_writes_the_default() -> None:
    """The family model defaults the field to 2, so an unset field and an
    explicit 2 reach the PEP as the same grant file."""
    grant = build_grant_file(family(), index(), rev="r", token_sha256=())
    assert grant.max_inflight_delegations == DEFAULT_MAX_INFLIGHT_DELEGATIONS


def test_named_tools_pass_through_unchanged() -> None:
    f = family(tools={"kagi": ["kagi_extract"]})
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.tools == {"kagi": ["kagi_extract"]}


def test_all_expands_to_every_declared_tool() -> None:
    f = family(tools={"kagi": "all"})
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.tools == {"kagi": ["kagi_search_fetch", "kagi_extract"]}


# --- verbs -----------------------------------------------------------------


def test_only_granted_verbs_appear() -> None:
    f = family(verbs={"enqueue": {"targets": ["scrum-lead"]}})
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.verbs == {"enqueue": {"targets": ["scrum-lead"]}}


def test_embed_and_job_status_dump_as_empty_objects() -> None:
    f = family(verbs={"embed": {}, "enqueue": {"targets": ["scrum-lead"]}, "job_status": {}})
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.verbs["embed"] == {}
    assert grant.verbs["job_status"] == {}


def test_ha_call_keeps_a_null_entity_id() -> None:
    """Contract 04 section 1.2's own example keeps `entity_id: null` inside
    a granted triple -- this must not silently drop it."""
    f = family(verbs={"ha_call": {"allow": [{"domain": "notify", "service": "mobile_app_x"}]}})
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.verbs["ha_call"] == {
        "allow": [{"domain": "notify", "service": "mobile_app_x", "entity_id": None}]
    }


def test_release_dumps_its_components() -> None:
    f = family(name="agent-control", verbs={"release": {"components": ["chaperone"]}})
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.verbs["release"] == {"components": ["chaperone"]}


# --- delegates and approval --------------------------------------------------


def test_delegates_pass_through() -> None:
    f = family(delegates=["vault-oracle", "code-sandbox"])
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.delegates == ("vault-oracle", "code-sandbox")


def test_approval_verb_and_invoke_agent_pass_through() -> None:
    f = family(
        verbs={"enqueue": {"targets": ["scrum-lead"]}},
        delegates=["vault-oracle"],
        approval=["enqueue", "invoke_agent"],
    )
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.approval == ("enqueue", "invoke_agent")


def test_approval_named_tool_passes_through() -> None:
    f = family(tools={"kagi": ["kagi_extract"]}, approval=["kagi__kagi_extract"])
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.approval == ("kagi__kagi_extract",)


def test_approval_wildcard_expands_to_every_granted_tool() -> None:
    f = family(tools={"kagi": ["kagi_extract", "kagi_search_fetch"]}, approval=["kagi__*"])
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.approval == ("kagi__kagi_extract", "kagi__kagi_search_fetch")


def test_approval_wildcard_on_an_all_grant_expands_through_the_server_file() -> None:
    f = family(tools={"kagi": "all"}, approval=["kagi__*"])
    grant = build_grant_file(f, index(), rev="r", token_sha256=())
    assert grant.approval == ("kagi__kagi_search_fetch", "kagi__kagi_extract")


# --- write/delete ----------------------------------------------------------


def test_write_grant_file_round_trips_and_is_mode_0640(tmp_path: Path) -> None:
    grant = GrantFile(
        family="chat",
        rev="reg-abc",
        token_sha256=("deadbeef",),
        model_alias="agent-router",
        tools={},
        verbs={},
        delegates=(),
        max_inflight_delegations=DEFAULT_MAX_INFLIGHT_DELEGATIONS,
        approval=(),
    )
    path = tmp_path / "chat.json"
    write_grant_file(path, grant)
    assert json.loads(path.read_text(encoding="utf-8")) == grant.as_json()
    assert stat.S_IMODE(path.stat().st_mode) == GRANT_FILE_MODE


def test_delete_grant_file_removes_it(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    path.write_text("{}", encoding="utf-8")
    delete_grant_file(path)
    assert not path.exists()


def test_delete_grant_file_of_an_absent_file_does_not_raise(tmp_path: Path) -> None:
    delete_grant_file(tmp_path / "never-existed.json")


# --- the digests alone -----------------------------------------------------


def written(tmp_path: Path) -> Path:
    """A grant file on disk, with one tool and two accepted digests."""
    path = tmp_path / "chat.json"
    grant = build_grant_file(
        family(tools={"kagi": ["kagi_extract"]}), index(), rev="old", token_sha256=("aaa", "bbb")
    )
    write_grant_file(path, grant)
    return path


def test_rewrite_digests_moves_the_digests_and_the_rev_alone(tmp_path: Path) -> None:
    path = written(tmp_path)
    before = json.loads(path.read_text(encoding="utf-8"))

    assert rewrite_digests(path, "chat", rev="new", token_sha256=("ccc",)) is True

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == {**before, "rev": "new", "token_sha256": ["ccc"]}
    assert stat.S_IMODE(path.stat().st_mode) == GRANT_FILE_MODE


def test_rewrite_digests_of_an_absent_file_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "never-existed.json"
    assert rewrite_digests(path, "chat", rev="new", token_sha256=("ccc",)) is False
    assert not path.exists()


def test_rewrite_digests_refuses_a_file_that_is_not_a_grant_file(tmp_path: Path) -> None:
    for text in ("not json", "[]", "{}"):
        path = tmp_path / "chat.json"
        path.write_text(text, encoding="utf-8")
        assert rewrite_digests(path, "chat", rev="new", token_sha256=("ccc",)) is False
        assert path.read_text(encoding="utf-8") == text


def test_rewrite_digests_refuses_another_version(tmp_path: Path) -> None:
    """A file this version did not write holds content this version cannot
    answer for."""
    path = written(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["version"] = GRANT_FILE_VERSION - 1
    path.write_text(json.dumps(body), encoding="utf-8")

    assert rewrite_digests(path, "chat", rev="new", token_sha256=("ccc",)) is False
    assert json.loads(path.read_text(encoding="utf-8")) == body


def test_rewrite_digests_refuses_another_familys_file(tmp_path: Path) -> None:
    path = written(tmp_path)
    before = path.read_bytes()

    assert rewrite_digests(path, "ops", rev="new", token_sha256=("ccc",)) is False
    assert path.read_bytes() == before
