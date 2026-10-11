"""The grant file builder expands `all` and `<server>__*` exactly once, and
leaves everything else untouched (contract 04 section 1.2).

The writer writes a file of `GRANT_FILE_MAX_BYTES` bytes at most. A pass
reads the family whose file is larger as `invalid`, and the last grant file
stays."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_family import FamilyFile, FamilyState, Index, load_registry
from agent_family.grammar import DEFAULT_MAX_INFLIGHT_DELEGATIONS
from agent_family.server import McpServerFile
from caregiver.driver import FakeDriver
from caregiver.egress import EgressConfig
from caregiver.grants import (
    DEFAULT_MAX_OPEN_GATES,
    DEFAULT_PEP_RPM,
    GRANT_FILE_MAX_BYTES,
    GRANT_FILE_MODE,
    GRANT_FILE_VERSION,
    GrantFile,
    GrantFileTooLargeError,
    build_grant_file,
    delete_grant_file,
    holds_grants,
    rewrite_digests,
    write_grant_file,
)
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.reconcile import Actors, ReconcileResult, reconcile_family
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import UNREADABLE_JSON, published, write_registry

from caregiver import paths

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


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_a_grant_file_that_does_not_read_is_no_grant_file(tmp_path: Path, raw: bytes) -> None:
    """The pass reads the grant file to see whether it must write it
    again. A file that does not read is one to write again, and the read
    must not raise into the pass."""
    path = tmp_path / "chat.json"
    path.write_bytes(raw)

    assert holds_grants(path, "chat") is False
    assert rewrite_digests(path, "chat", rev="new", token_sha256=("ccc",)) is False
    assert path.read_bytes() == raw


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


# --- the cap of the file ------------------------------------------------------


def sized_grant(tmp_path: Path, size: int) -> GrantFile:
    """A grant whose file has `size` bytes. One character of the alias is
    one byte of the file."""

    def with_alias(alias: str) -> GrantFile:
        return GrantFile(
            family="chat",
            rev="reg-abc",
            token_sha256=("deadbeef",),
            model_alias=alias,
            tools={},
            verbs={},
            delegates=(),
            max_inflight_delegations=DEFAULT_MAX_INFLIGHT_DELEGATIONS,
            approval=(),
        )

    smallest = tmp_path / "smallest.json"
    write_grant_file(smallest, with_alias(""))
    return with_alias("a" * (size - smallest.stat().st_size))


def test_a_grant_file_of_the_cap_is_written(tmp_path: Path) -> None:
    grant = sized_grant(tmp_path, GRANT_FILE_MAX_BYTES)
    path = tmp_path / "grants" / "chat.json"

    write_grant_file(path, grant)

    assert path.stat().st_size == GRANT_FILE_MAX_BYTES
    assert json.loads(path.read_text(encoding="utf-8")) == grant.as_json()


def test_a_grant_file_of_one_byte_more_is_refused(tmp_path: Path) -> None:
    grant = sized_grant(tmp_path, GRANT_FILE_MAX_BYTES + 1)
    directory = tmp_path / "grants"

    with pytest.raises(GrantFileTooLargeError) as refusal:
        write_grant_file(directory / "chat.json", grant)

    assert refusal.value.size == GRANT_FILE_MAX_BYTES + 1
    assert str(refusal.value) == (
        f"the grant file would have {GRANT_FILE_MAX_BYTES + 1} bytes, "
        f"and the cap is {GRANT_FILE_MAX_BYTES} bytes"
    )
    assert not directory.exists()


def test_a_refused_grant_file_leaves_the_file_of_the_last_write(tmp_path: Path) -> None:
    path = written(tmp_path / "grants")
    before = path.read_bytes()

    with pytest.raises(GrantFileTooLargeError):
        write_grant_file(path, sized_grant(tmp_path, GRANT_FILE_MAX_BYTES + 1))

    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


# --- a pass and the cap -------------------------------------------------------

SERVER = "kagi"

#: One tool name with as many bytes as the cap. A grant file that holds it
#: is larger than the cap.
LONG_TOOL = "t" * GRANT_FILE_MAX_BYTES

TOO_LARGE_NOTE = "refused: grant file over the cap"


@dataclass
class Host:
    """One registry with the family `chat`, one state root and the fakes of
    a pass. The family grants each tool of the server."""

    registry_root: Path
    state_root: Path
    driver: FakeDriver = field(default_factory=FakeDriver)
    litellm: FakeLiteLLMKeys = field(default_factory=FakeLiteLLMKeys)

    def serve(self, *tools: str) -> None:
        """Write the server file with these tools."""
        body = {
            "name": SERVER,
            "identity": "Search.",
            "install": {"source": "agent-mcp"},
            "run": {"entrypoint": "kagi-mcp", "env": {"KAGI_API_KEY": "secret:kagi_api_key"}},
            "tools": [{"name": tool, "description": "One tool."} for tool in tools],
        }
        directory = self.registry_root / "mcp" / SERVER
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "server.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")

    def one_pass(self) -> ReconcileResult:
        actors = Actors(self.driver, self.litellm, FakeSwitchClient(), EgressConfig(), FakeUnits())
        return reconcile_family(
            load_registry(self.registry_root),
            "chat",
            state_root=self.state_root,
            image="sha256:deadbeef",
            actors=actors,
        )

    @property
    def grant_path(self) -> Path:
        return paths.grant_path(self.state_root, "chat")

    def report(self) -> dict[str, Any]:
        """The validation report that the status document names."""
        path = paths.validation_path(self.state_root, "chat")
        return json.loads(path.read_text(encoding="utf-8"))

    def refused_size(self, scratch: Path) -> int:
        """The size that the writer names for the grant file of the registry.
        A pass writes a `rev` of 32 characters and one digest."""
        registry = load_registry(self.registry_root)
        servers = Index(kinds={}, servers=registry.servers, skills=registry.skills)
        grant = build_grant_file(
            registry.families["chat"], servers, rev="0" * 32, token_sha256=("0" * 64,)
        )
        with pytest.raises(GrantFileTooLargeError) as refusal:
            write_grant_file(scratch / "scratch.json", grant)

        return refusal.value.size


@pytest.fixture
def host(tmp_path: Path) -> Host:
    registry_root = write_registry(tmp_path / "registry", tools={SERVER: "all"})
    return Host(registry_root, tmp_path / "state")


def test_a_pass_keeps_the_last_grant_file_when_the_next_one_is_over_the_cap(host: Host) -> None:
    """The family reads `invalid`, as for each file that a pass cannot
    apply. The last grant file stays, so the family keeps what it had."""
    host.serve("search")
    first = host.one_pass()
    assert first.status.state is FamilyState.IN_SYNC
    before = host.grant_path.read_bytes()
    host.driver.calls.clear()

    host.serve("search", LONG_TOOL)
    result = host.one_pass()

    assert result.status.state is FamilyState.INVALID
    assert result.note == TOO_LARGE_NOTE
    assert host.grant_path.read_bytes() == before
    assert list(host.grant_path.parent.iterdir()) == [host.grant_path]
    assert result.status.applied_rev == first.status.registry_rev
    assert result.status.validation.never_valid is False
    assert [one.id for one in result.status.sandboxes] == ["chat-s1"]
    assert host.driver.ops() == ()


def test_a_grant_file_over_the_cap_is_an_issue_and_no_fault(host: Host, tmp_path: Path) -> None:
    """The report names the size and the cap. No fault code says that a
    grant file is too large, so the document holds no fault for it."""
    host.serve("search")
    host.one_pass()

    host.serve("search", LONG_TOOL)
    result = host.one_pass()

    size = host.refused_size(tmp_path)
    said = f"the grant file would have {size} bytes, and the cap is {GRANT_FILE_MAX_BYTES} bytes"
    report = host.report()
    assert report["status"] == "invalid"
    assert report["errors"] == 1
    issue = {"severity": "error", "loc": "<document>", "msg": said, "downgraded": False}
    assert issue in report["issues"]
    validation = published(host.state_root)["validation"]
    assert validation["ok"] is False
    assert validation["error_count"] == 1
    assert validation["first_error"] == f"<document>: {said}"
    assert result.status.faults == ()


def test_a_first_pass_writes_no_grant_file_over_the_cap(host: Host) -> None:
    host.serve("search", LONG_TOOL)

    result = host.one_pass()

    assert result.status.state is FamilyState.INVALID
    assert result.status.validation.never_valid is True
    assert not host.grant_path.parent.exists()
    assert host.driver.ops() == ()


def test_a_pass_writes_the_grant_file_when_it_fits_again(host: Host) -> None:
    host.serve("search")
    host.one_pass()
    host.serve("search", LONG_TOOL)
    host.one_pass()

    host.serve("search", "extract")
    result = host.one_pass()

    assert result.status.state is FamilyState.IN_SYNC
    granted = json.loads(host.grant_path.read_text(encoding="utf-8"))
    assert granted["tools"] == {SERVER: ["search", "extract"]}
    assert host.report()["errors"] == 0
    assert published(host.state_root)["validation"]["ok"] is True
