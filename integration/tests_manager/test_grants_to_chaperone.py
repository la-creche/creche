"""Seam 1: the grant file and the family token, against the real PEP.

`caregiver` writes `grants/<family>.json` and `creds.json`. The PEP reads the
first and authenticates with a digest of the token in the second. Both sides
were built from contract 04 by different agents, and neither ever saw the
other's output. These tests put the real writer and the real reader on the
same temp root.

    apply_once ──► grants/chat.json ──► FamilyStore ──► GET /manifest
               └─► creds/creds.json ──► the bearer ──┘
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .chaperone_harness import EMBED_DIMS, EMBED_MODEL, build_pep
from .conftest import CHAT_FAMILY, FAMILY, Applied

HTTP_OK = 200
HTTP_FORBIDDEN = 403

#: Not granted by the fixture family, and never executable here.
UNGRANTED_VERB = "release"
UNGRANTED_MCP_TOOL = "kagi__kagi_search_fetch"

#: The one triple the fixture family's `ha_call` fence allows.
HA_ARGS: dict[str, Any] = {"domain": "notify", "service": "mobile_app_example_phone"}


@pytest.fixture
def chaperone(applied: Applied, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """The real PEP, pointed at the state root `apply_once` just wrote."""
    app = build_pep(monkeypatch, tmp_path, applied.state_root)
    client = TestClient(app)
    client.__enter__()  # run the lifespan, as the real process does
    return client


def auth(applied: Applied) -> dict[str, str]:
    return {"Authorization": f"Bearer {applied.token}"}


def manifest(chaperone: TestClient, applied: Applied) -> dict[str, Any]:
    reply = chaperone.get("/manifest", headers=auth(applied))
    assert reply.status_code == HTTP_OK, reply.text
    body: dict[str, Any] = reply.json()
    return body


def call(chaperone: TestClient, applied: Applied, tool: str, args: dict[str, Any]) -> Any:
    return chaperone.post("/call", json={"tool": tool, "args": args}, headers=auth(applied))


# --- the token caregiver minted -------------------------------------------


def test_the_minted_token_resolves_to_the_family(chaperone: TestClient, applied: Applied) -> None:
    assert manifest(chaperone, applied)["family"] == FAMILY


def test_the_grant_file_holds_a_digest_and_never_the_token(applied: Applied) -> None:
    """Contract 04 §2.2: a stolen grant file grants nothing."""
    body = applied.grant_path.read_text(encoding="utf-8")
    assert applied.token not in body
    assert len(json.loads(body)["token_sha256"][0]) == 64


def test_another_token_resolves_to_nothing(chaperone: TestClient) -> None:
    reply = chaperone.get("/manifest", headers={"Authorization": "Bearer not-the-family-token"})
    assert reply.status_code != HTTP_OK


# --- the manifest matches the family file --------------------------------


def test_the_manifest_tools_match_the_family_file(chaperone: TestClient, applied: Applied) -> None:
    """The fixture grants two verbs and no MCP server, so the manifest is
    exactly those two, in `manifest_actions` order."""
    names = [tool["name"] for tool in manifest(chaperone, applied)["tools"]]
    assert names == ["embed", "ha_call"]
    assert set(names) == set(CHAT_FAMILY["verbs"])


def test_the_manifest_carries_the_model_alias(chaperone: TestClient, applied: Applied) -> None:
    assert manifest(chaperone, applied)["model_alias"] == CHAT_FAMILY["model"]["router"]


def test_the_manifest_rev_is_the_one_caregiver_wrote(
    chaperone: TestClient, applied: Applied
) -> None:
    written = json.loads(applied.grant_path.read_text(encoding="utf-8"))["rev"]
    assert manifest(chaperone, applied)["rev"] == written


def test_no_manifest_entry_is_gated(chaperone: TestClient, applied: Applied) -> None:
    """The fixture family's `approval` list is empty, so nothing taps."""
    assert [tool["approval"] for tool in manifest(chaperone, applied)["tools"]] == [False, False]


def test_a_granted_verb_runs(chaperone: TestClient, applied: Applied) -> None:
    reply = call(chaperone, applied, "embed", {"input": "hi"})
    assert reply.status_code == HTTP_OK, reply.text
    result = reply.json()["result"]
    assert result["model"] == EMBED_MODEL
    assert result["dims"] == EMBED_DIMS


# --- a tool the family lacks ---------------------------------------------


def test_an_ungranted_verb_is_refused(chaperone: TestClient, applied: Applied) -> None:
    reply = call(chaperone, applied, UNGRANTED_VERB, {"components": ["agent-control"]})
    assert reply.status_code == HTTP_FORBIDDEN
    assert reply.json()["reason"] == "tool_not_granted"


def test_an_ungranted_mcp_tool_is_refused(chaperone: TestClient, applied: Applied) -> None:
    reply = call(chaperone, applied, UNGRANTED_MCP_TOOL, {"q": "boiler"})
    assert reply.status_code == HTTP_FORBIDDEN
    assert reply.json()["reason"] == "tool_not_granted"


def test_an_ungranted_tool_is_absent_from_the_manifest(
    chaperone: TestClient, applied: Applied
) -> None:
    names = [tool["name"] for tool in manifest(chaperone, applied)["tools"]]
    assert UNGRANTED_VERB not in names
    assert UNGRANTED_MCP_TOOL not in names


# --- a rewritten family file, with no restart ----------------------------


def reapply_without_ha_call(applied: Applied) -> None:
    """Rewrite the family file, then run `apply_once` again. The PEP process
    is never signalled, restarted or told anything (invariant 9)."""
    result = applied.reapply({**CHAT_FAMILY, "verbs": {"embed": {}}})
    assert result.ok, f"the second apply failed: {result.status.faults}"


def test_the_next_call_after_a_narrowing_is_refused(
    chaperone: TestClient, applied: Applied
) -> None:
    """Invariant 9: a permission change needs no restart. The call that
    proves it is the very next one, on the same client and the same app."""
    allowed = call(chaperone, applied, "ha_call", HA_ARGS)
    assert allowed.status_code == HTTP_OK, allowed.text

    reapply_without_ha_call(applied)

    refused = call(chaperone, applied, "ha_call", HA_ARGS)
    assert refused.status_code == HTTP_FORBIDDEN
    assert refused.json()["reason"] == "tool_not_granted"


def test_the_narrowed_manifest_drops_the_tool(chaperone: TestClient, applied: Applied) -> None:
    assert "ha_call" in [tool["name"] for tool in manifest(chaperone, applied)["tools"]]

    reapply_without_ha_call(applied)

    assert [tool["name"] for tool in manifest(chaperone, applied)["tools"]] == ["embed"]


def test_the_token_survives_a_reapply(chaperone: TestClient, applied: Applied) -> None:
    """Contract 05 §6.2: mint once, refresh after. A narrowing is not a
    rotation, so the sandbox's mounted token keeps working."""
    before = applied.token

    reapply_without_ha_call(applied)

    assert applied.token == before
    assert manifest(chaperone, applied)["family"] == FAMILY


def test_deleting_the_grant_file_revokes_the_family(
    chaperone: TestClient, applied: Applied
) -> None:
    """Contract 04 §1.3 rule 5. The same no-restart path, at its end."""
    applied.grant_path.unlink()

    reply = call(chaperone, applied, "embed", {"input": "hi"})
    assert reply.status_code != HTTP_OK
