"""The live-state document, parsed as input."""

from __future__ import annotations

import json
from typing import Any

import pytest
from agent_release.catalog import ContractId
from agent_release.errors import Refusal, RefusalCode
from agent_release.state import MAX_STATE_BYTES, parse_state

SHA = "2b59c3bd81f4a6079ce5d2a3418b6f0cc7d9e215"
DIGEST = "sha256:" + ("5e41" * 16)

FULL: dict[str, Any] = {
    "live": {"chaperone": "2.0.3", "attendance": None},
    "provided": {"pep-grant": "2.0"},
    "latest": {"chaperone": "2.1.0"},
    "facts": {"chaperone": {"sha": SHA, "input_digest": DIGEST, "artifact_digest": None}},
}


def _parse(body: Any) -> Any:
    return parse_state(json.dumps(body), "live-state.json")


def _refusal(body: Any) -> Refusal:
    with pytest.raises(Refusal) as caught:
        _parse(body)

    assert caught.value.code is RefusalCode.STATE

    return caught.value


def test_a_full_document_round_trips() -> None:
    state = _parse(FULL)

    assert state.live == {"chaperone": "2.0.3", "attendance": None}
    assert state.provided == {ContractId.PEP_GRANT: (2, 0)}
    assert state.latest == {"chaperone": "2.1.0"}
    assert state.facts["chaperone"].sha == SHA
    assert state.facts["chaperone"].artifact_digest is None


def test_every_key_is_optional() -> None:
    state = _parse({})

    assert state.live == {}
    assert state.provided == {}
    assert state.latest == {}
    assert state.facts == {}


def test_an_unknown_top_level_field_is_refused() -> None:
    assert "unknown field: smuggled" in _refusal({"smuggled": 1}).detail


def test_a_scalar_document_is_refused() -> None:
    assert "must be an object" in _refusal("live").detail


def test_unparseable_json_is_refused() -> None:
    with pytest.raises(Refusal) as caught:
        parse_state("{not json", "live-state.json")

    assert "does not parse as JSON" in caught.value.detail


def test_an_oversized_document_is_refused() -> None:
    padded = dict(FULL)
    padded["latest"] = {"chaperone": "1.0.0"}
    text = json.dumps(padded) + (" " * MAX_STATE_BYTES)

    with pytest.raises(Refusal) as caught:
        parse_state(text, "live-state.json")

    assert "larger than" in caught.value.detail


def test_live_refuses_a_name_outside_the_catalog() -> None:
    assert "contract 06 §1 omits" in _refusal({"live": {"smuggled": "1.0.0"}}).detail


def test_live_refuses_a_version_that_is_not_three_numbers() -> None:
    assert "MAJOR.MINOR.PATCH" in _refusal({"live": {"chaperone": "2.1"}}).detail


def test_latest_refuses_a_null() -> None:
    assert "MAJOR.MINOR.PATCH" in _refusal({"latest": {"chaperone": None}}).detail


def test_provided_refuses_an_unknown_contract_id() -> None:
    assert "is no contract id" in _refusal({"provided": {"made-up": "1.0"}}).detail


def test_provided_refuses_a_three_number_version() -> None:
    assert "must be MAJOR.MINOR" in _refusal({"provided": {"pep-grant": "2.0.1"}}).detail


def test_facts_refuse_a_short_sha() -> None:
    body = {"facts": {"chaperone": {"sha": "abc", "input_digest": DIGEST, "artifact_digest": None}}}

    assert "40 lower-case hex" in _refusal(body).detail


def test_facts_refuse_an_unprefixed_digest() -> None:
    body = {"facts": {"chaperone": {"sha": SHA, "input_digest": "5e41", "artifact_digest": None}}}

    assert "sha256:<64 hex>" in _refusal(body).detail


def test_facts_accept_an_artifact_digest() -> None:
    body = {"facts": {"chaperone": {"sha": SHA, "input_digest": DIGEST, "artifact_digest": DIGEST}}}

    assert _parse(body).facts["chaperone"].artifact_digest == DIGEST


def test_facts_refuse_an_unknown_field() -> None:
    body = {"facts": {"chaperone": {"sha": SHA, "input_digest": DIGEST, "command": "/bin/sh"}}}

    assert "unknown field: command" in _refusal(body).detail


def test_a_map_field_refuses_a_list() -> None:
    assert "'live' must be an object" in _refusal({"live": ["chaperone"]}).detail


def test_facts_refuse_a_scalar_entry() -> None:
    assert "'facts.chaperone' must be an object" in _refusal({"facts": {"chaperone": SHA}}).detail
