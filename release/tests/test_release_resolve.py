"""The resolver: actions, order, the §9 document and its hash."""

from __future__ import annotations

import json

import pytest
from agent_release.catalog import CATALOG, Action, ContractId
from agent_release.errors import Refusal, RefusalCode
from agent_release.manifest import ComponentManifest, parse_manifest
from agent_release.resolve import (
    build_document,
    canonical_json,
    parse_request,
    resolve,
)
from agent_release.state import ReleaseState, SourceFacts
from release_fixtures import manifest_text, provides_entry, requires_entry

RELEASE_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DE"
RESOLVED_AT = 1758153600.0

#: `pep` provides pep-grant 2.1. `attendance` and `noticeboard` call it at 2.0, and
#: `attendance` deploys after `pep`.
EDGES: dict[str, tuple[str, str, str]] = {
    "pep": (provides_entry("pep-grant", 2, 1), "", ""),
    "attendance": (
        provides_entry("session-api", 1, 4),
        requires_entry("pep-grant", 2, 0),
        "pep",
    ),
    "noticeboard": ("", requires_entry("pep-grant", 2, 0), "attendance"),
    "caregiver": ("", "", ""),
    "playpen": ("", "", ""),
    "infra": ("", "", ""),
    "releasectl": ("", "", ""),
    "mcp-servers": ("", "", ""),
    "registry-data": ("", "", ""),
}

LIVE = {
    "pep": "2.0.3",
    "attendance": "1.4.7",
    "noticeboard": "0.7.0",
    "caregiver": "1.2.0",
    "playpen": "3.1.2",
    "infra": "0.4.1",
    "releasectl": "0.3.0",
    "mcp-servers": "0.9.3",
}


def _nine() -> dict[str, ComponentManifest]:
    manifests: dict[str, ComponentManifest] = {}
    for row in CATALOG:
        provides, requires, depends = EDGES[row.name]
        text = manifest_text(row.name, provides=provides, requires=requires, depends_on=depends)
        manifests[row.name] = parse_manifest(text, f"{row.name}/component.yaml")

    return manifests


def _state(**changes: object) -> ReleaseState:
    facts = {
        row.name: SourceFacts(
            sha=f"{index:040x}",
            input_digest="sha256:" + f"{index:064x}",
            artifact_digest=None,
        )
        for index, row in enumerate(CATALOG)
    }
    state = ReleaseState(
        live=dict(LIVE),
        provided={ContractId.PEP_GRANT: (2, 0), ContractId.SESSION_API: (1, 4)},
        latest={"pep": "2.1.0"},
        facts=facts,
    )
    for name, value in changes.items():
        object.__setattr__(state, name, value)

    return state


def _refusal(request: dict[str, str], state: ReleaseState | None = None) -> Refusal:
    with pytest.raises(Refusal) as caught:
        resolve(_nine(), state if state is not None else _state(), request)

    return caught.value


def test_parse_request_reads_name_and_version() -> None:
    assert parse_request(["pep=2.1.0", "attendance=latest"]) == {
        "pep": "2.1.0",
        "attendance": "latest",
    }


def test_parse_request_refuses_a_bare_name() -> None:
    with pytest.raises(Refusal) as caught:
        parse_request(["pep"])

    assert caught.value.code is RefusalCode.REQUEST
    assert "expected <component>=<version>" in caught.value.detail


def test_parse_request_refuses_a_repeat() -> None:
    with pytest.raises(Refusal) as caught:
        parse_request(["pep=2.1.0", "pep=2.2.0"])

    assert "names pep twice" in caught.value.detail


def test_parse_request_refuses_a_two_number_version() -> None:
    with pytest.raises(Refusal) as caught:
        parse_request(["pep=2.1"])

    assert "MAJOR.MINOR.PATCH" in caught.value.detail


def test_parse_request_caps_the_component_count() -> None:
    with pytest.raises(Refusal) as caught:
        parse_request([f"c{index}=1.0.0" for index in range(9)])

    assert "more than 8 components" in caught.value.detail


def test_a_name_outside_the_catalog_is_refused() -> None:
    refusal = _refusal({"smuggled": "1.0.0"})

    assert refusal.code is RefusalCode.REQUEST
    assert "contract 06 §1 lists" in refusal.detail


def test_the_data_component_can_never_be_released() -> None:
    assert "never releases" in _refusal({"registry-data": "1.0.0"}).detail


def test_a_component_with_no_manifest_is_refused() -> None:
    manifests = _nine()
    del manifests["pep"]

    with pytest.raises(Refusal) as caught:
        resolve(manifests, _state(), {"pep": "2.1.0"})

    assert "has no component.yaml" in caught.value.detail


def test_latest_needs_a_live_state_entry() -> None:
    assert "names no released tag" in _refusal({"attendance": "latest"}).detail


def test_latest_resolves_to_the_newest_version() -> None:
    resolution = resolve(_nine(), _state(), {"pep": "latest"})
    pep = next(item for item in resolution.components if item.name == "pep")

    assert pep.action is Action.DEPLOY
    assert pep.to_version == "2.1.0"
    assert pep.tag() == "pep-v2.1.0"


def test_requesting_the_live_version_changes_nothing() -> None:
    resolution = resolve(_nine(), _state(), {"pep": "2.0.3"})
    pep = next(item for item in resolution.components if item.name == "pep")

    assert pep.action is Action.UNCHANGED
    assert resolution.order == ()


def test_every_component_appears_in_name_order() -> None:
    resolution = resolve(_nine(), _state(), {"pep": "2.1.0"})
    names = [item.name for item in resolution.components]

    assert names == sorted(names)
    assert len(names) == len(CATALOG)


def test_the_data_component_carries_no_version_or_tag() -> None:
    resolution = resolve(_nine(), _state(), {"pep": "2.1.0"})
    data = next(item for item in resolution.components if item.name == "registry-data")

    assert data.action is Action.UNCHANGED
    assert data.from_version is None
    assert data.to_version is None
    assert data.tag() is None


def test_a_set_deploys_dependencies_first() -> None:
    resolution = resolve(_nine(), _state(), {"pep": "2.1.0", "attendance": "1.5.0"})

    assert resolution.order == ("pep", "attendance")
    assert resolution.deploying == frozenset({"pep", "attendance"})


def test_the_contract_table_names_every_consumer() -> None:
    resolution = resolve(_nine(), _state(), {"pep": "2.1.0"})
    grant = next(row for row in resolution.contracts if row.contract is ContractId.PEP_GRANT)

    assert grant.provider == "pep"
    assert [item.name for item in grant.consumers] == ["attendance", "noticeboard"]


def _document(request: dict[str, str]) -> dict[str, object]:
    state = _state()

    return build_document(resolve(_nine(), state, request), state, RELEASE_ID, "human", RESOLVED_AT)


def test_the_document_has_contract_06_paragraph_9_fields() -> None:
    document = _document({"pep": "2.1.0"})

    assert set(document) == {
        "manifest_version",
        "id",
        "resolved_at",
        "requested_by",
        "components",
        "order",
        "contracts",
        "manifest_sha256",
    }
    assert str(document["manifest_sha256"]).startswith("sha256:")


def test_the_same_set_hashes_the_same_whatever_the_argument_order() -> None:
    forwards = _document(parse_request(["pep=2.1.0", "attendance=1.5.0"]))
    backwards = _document(parse_request(["attendance=1.5.0", "pep=2.1.0"]))

    assert forwards["manifest_sha256"] == backwards["manifest_sha256"]


def test_a_different_version_hashes_differently() -> None:
    one = _document({"pep": "2.1.0"})
    two = _document({"pep": "2.1.1"})

    assert one["manifest_sha256"] != two["manifest_sha256"]


def test_the_hash_covers_every_other_field() -> None:
    document = _document({"pep": "2.1.0"})
    body = dict(document)
    del body["manifest_sha256"]
    text = canonical_json(body)

    assert " " not in text
    assert json.loads(text) == body


def test_a_component_with_no_facts_reads_null_and_not_a_refusal() -> None:
    """Contract 06 §9.

    A document that refused unless EVERY one of the nine carried a `sha`
    and an `input_digest` could not be built on a host where a component
    has no tag: a first release can run with none in agent-mcp or
    agent-registry, so `mcp-servers` and `registry-data` resolve nothing.

    So the columns are nullable, and the rule is where they are non-null:
    the components root resolved a tag for. Root still refuses to FETCH a
    component with no sha (`steps.NO_SOURCE_FACTS`), which is the check
    that matters — a build needs a commit, a record does not.
    """
    state = _state(facts={})

    document = build_document(
        resolve(_nine(), state, {"pep": "2.1.0"}), state, RELEASE_ID, "human", RESOLVED_AT
    )

    rows = document["components"]
    assert isinstance(rows, list)
    blank = [{"sha": None, "input_digest": None, "artifact_digest": None}] * len(rows)

    assert [{key: row[key] for key in blank[0]} for row in rows] == blank


def test_a_lower_case_ulid_is_refused() -> None:
    state = _state()

    with pytest.raises(Refusal) as caught:
        build_document(resolve(_nine(), state, {}), state, RELEASE_ID.lower(), "human", RESOLVED_AT)

    assert "26 upper-case Crockford characters" in caught.value.detail


def test_a_requested_by_with_a_space_is_refused() -> None:
    state = _state()

    with pytest.raises(Refusal) as caught:
        build_document(resolve(_nine(), state, {}), state, RELEASE_ID, "a family", RESOLVED_AT)

    assert "requested_by must be" in caught.value.detail
