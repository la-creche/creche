"""The chaperone as a process, on the delegate path of the old stage 3 suite.

`integration/tests/test_i3_stage3.py` hosts the chaperone and `attendance`
inside the test process and publishes the families through `caregiver`'s
own code. Here both services are processes, the grant file follows contract
04 §1, and a test plays the sandbox: it sends the HTTP requests the bridge
sends, with the family token.

Each assertion reads an HTTP status, an HTTP body, the audit file, a file in
a mount, or the session store.
"""

from __future__ import annotations

from typing import Any

import httpx
from proc_chat import chat_id, session_of
from proc_delegate import CALLER, FIRST_REV, TARGET, DelegateStack
from proc_tree import MODEL_ALIAS

CALL_PATH = "/call"
MANIFEST_PATH = "/manifest"
HEALTH_PATH = "/healthz"
INVOKE_AGENT = "invoke_agent"
SESSION_HEADER = "X-Session-Id"

#: What the caller asks its delegate. The fake pi echoes the first 24
#: characters of a prompt, so a short text comes back whole.
QUESTION = "dropped-sensor"

#: A delegate the caller may name, with no family behind it.
OTHER_TARGET = "code-sandbox"
NEXT_REV = "01K5J9QW3R7T0ZP4YB2H6N8M2E"

HTTP_OK = 200
HTTP_FORBIDDEN = 403


async def test_healthz_answers_with_no_bearer(delegate: DelegateStack) -> None:
    """Contract 04 §10. The one route with no token, and `ok` is what a reader reads."""
    async with delegate.sandbox_client() as client:
        client.headers.pop("Authorization")
        response = await client.get(HEALTH_PATH)

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["ok"] is True
    # Rule 2: this chaperone has no roster source, so no roster read runs.
    assert body["roster"]["state"] == "off"


async def test_the_manifest_offers_the_delegate_tool(sandbox: httpx.AsyncClient) -> None:
    """Contract 04 §4 rule 1. `invoke_agent` comes from `delegates`."""
    response = await sandbox.get(MANIFEST_PATH)

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["family"] == CALLER
    assert body["rev"] == FIRST_REV
    assert body["model_alias"] == MODEL_ALIAS

    tools = {tool["name"]: tool for tool in body["tools"]}
    assert list(tools) == [INVOKE_AGENT]
    assert tools[INVOKE_AGENT]["schema"]["properties"]["family"]["enum"] == [TARGET]


async def test_an_empty_delegates_list_hides_the_tool(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Contract 04 §4 rule 1, the other side: no delegate, no tool."""
    delegate.grant(delegates=(), rev=NEXT_REV)

    manifest = await sandbox.get(MANIFEST_PATH)
    refused = await _ask(sandbox, session_of(chat_id()))

    assert manifest.json()["rev"] == NEXT_REV
    assert manifest.json()["tools"] == []
    assert refused.status_code == HTTP_FORBIDDEN
    assert refused.json()["reason"] == "tool_not_granted"
    assert delegate.tree.sessions_of(TARGET) == []
    assert delegate.pi_starts() == []


async def test_an_unknown_token_is_refused(delegate: DelegateStack) -> None:
    """Contract 04 §5 row 1. A token no grant file knows names no family."""
    async with delegate.sandbox_client("no-such-family") as stranger:
        response = await _ask(stranger, session_of(chat_id()))

    assert response.status_code == HTTP_FORBIDDEN
    assert response.json()["reason"] == "unknown_token"
    assert delegate.tree.sessions_of(TARGET) == []


async def test_a_target_outside_delegates_is_refused(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Contract 04 §5 row 6. The tool is granted, the target is not."""
    delegate.grant(delegates=(OTHER_TARGET,), rev=NEXT_REV)

    response = await _ask(sandbox, session_of(chat_id()))

    assert response.status_code == HTTP_FORBIDDEN
    assert response.json()["reason"] == "tool_not_granted"
    assert delegate.tree.sessions_of(TARGET) == []
    assert delegate.pi_starts() == []

    line = _delegate_lines(delegate)[-1]
    assert line["decision"] == "deny"
    assert line["reason"] == "tool_not_granted"
    assert line["grants_rev"] == NEXT_REV


async def _ask(client: httpx.AsyncClient, session: str, message: str = QUESTION) -> httpx.Response:
    """One `invoke_agent` call, as the bridge sends it (contract 04 §7.1)."""
    body = {"tool": INVOKE_AGENT, "args": {"family": TARGET, "message": message}}

    return await client.post(CALL_PATH, json=body, headers={SESSION_HEADER: session})


def _delegate_lines(delegate: DelegateStack) -> list[dict[str, Any]]:
    """Every audit record of a delegate call, in the order it was written."""
    return [line for line in delegate.tree.audit_lines() if line.get("tool") == INVOKE_AGENT]
