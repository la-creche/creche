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

import asyncio
import json
from typing import Any

import httpx
from proc_chat import chat_id, session_of, until
from proc_delegate import CALLER, FIRST_REV, TARGET, DelegateStack
from proc_ids import ULID
from proc_standins import set_pi_env
from proc_tree import GRANT_LIMITS, MODEL_ALIAS, THIN, write_status

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

#: A job turn long enough to read its turn file: 200 deltas, 8 ms apart.
SLOW_JOB = {"events": 200, "delay_ms": 8}
JOB_DEADLINE_S = 60.0

#: The cap of the caller family on delegate calls in flight (contract 04 §1.2).
AT_ONCE = GRANT_LIMITS["max_inflight_delegations"]

#: A job limit the slow job cannot meet: the job runs for 1.6 seconds or more.
SHORT_JOB_TIMEOUT_S = 1

HTTP_OK = 200
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY = 429
HTTP_GATEWAY_TIMEOUT = 504


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


async def test_a_delegate_call_answers_the_caller(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """The whole path: grant file, delegate door, one job turn, one answer.

    The question is the prompt of the job, and the fake pi echoes it. The
    question in the answer proves that the message crossed every boundary.
    The answer is in the wrapper of contract 04 §7.6: data, never instruction.
    """
    response = await _ask(sandbox, session_of(chat_id()))

    assert response.status_code == HTTP_OK, response.text
    result = _result(response)
    assert result["untrusted"] is True
    assert result["source"] == f"family:{TARGET}"
    assert QUESTION in result["content"]


async def test_the_job_session_is_gone_afterwards(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Contract 04 §7.3. A job leaves the audit record and nothing else."""
    response = await _ask(sandbox, session_of(chat_id()))

    assert response.status_code == HTTP_OK, response.text
    assert delegate.tree.sessions_of(TARGET) == []
    assert len(delegate.pi_starts()) == 1


async def test_the_audit_holds_the_call_and_its_chain(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Contract 04 §6. The record names the family, the grants and the claim.

    The call carried no delegation id, so the chain has one entry: this
    family (§6.3). The session id is under `claimed`, because the sandbox
    sent it (§6.2).
    """
    session = session_of(chat_id())

    response = await _ask(sandbox, session)

    assert response.status_code == HTTP_OK, response.text
    line = _delegate_lines(delegate)[-1]
    assert line["family"] == CALLER
    assert line["grants_rev"] == FIRST_REV
    assert line["decision"] == "allow"
    assert line["args"] == {"family": TARGET, "message": QUESTION}
    assert line["chain"] == [CALLER]
    assert line["claimed"]["session_id"] == session
    assert set(line["claimed"]) == {"session_id", "turn_id", "delegation_id"}


async def test_the_minted_delegation_reaches_the_job(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Contract 04 §7.4 and contract 03 §7.4.

    The chaperone mints the id, `attendance` puts it on `start_turn`, and
    the playpen writes it into the turn file of the job. The playpen removes
    that file with the job session, so the job is slow enough to read it.
    """
    set_pi_env(delegate.tree, **SLOW_JOB)
    session = session_of(chat_id())
    control = delegate.tree.mounts(TARGET).control
    running = asyncio.create_task(_ask(sandbox, session))

    try:
        await until(
            lambda: _job_turn_files(delegate) != [],
            f"a turn file of a job under {control}",
            JOB_DEADLINE_S,
        )
        record = json.loads(_job_turn_files(delegate)[0])
    finally:
        response = await running

    assert response.status_code == HTTP_OK, response.text
    assert ULID.fullmatch(str(record["delegation"]))
    assert record["caller_session"] == session


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


async def test_a_removed_delegate_is_refused_on_the_next_call(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Invariant 9 and contract 04 §1.5. A removal applies at once.

    One process serves both calls. Between them only the grant file changes:
    no restart, no signal and no apply command.
    """
    assert delegate.chaperone is not None
    session = session_of(chat_id())
    allowed = await _ask(sandbox, session)
    assert allowed.status_code == HTTP_OK, allowed.text

    delegate.grant(delegates=(), rev=NEXT_REV)
    refused = await _ask(sandbox, session)

    assert refused.status_code == HTTP_FORBIDDEN
    assert refused.json()["reason"] == "tool_not_granted"
    assert len(delegate.pi_starts()) == 1, "the refused call reached no sandbox"
    assert delegate.chaperone.exit_code() is None


async def test_a_job_past_its_limit_ends_in_timeout(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Contract 04 §7.5. The limit of the thin family is the deadline of the turn.

    The caller reads `delegate_timeout` and never an empty answer. The job
    session is gone, as after a job that answered.
    """
    write_status(delegate.tree, TARGET, THIN, job_timeout_s=SHORT_JOB_TIMEOUT_S)
    set_pi_env(delegate.tree, **SLOW_JOB)

    response = await _ask(sandbox, session_of(chat_id()))

    assert response.status_code == HTTP_GATEWAY_TIMEOUT
    assert response.json()["reason"] == "delegate_timeout"
    assert delegate.tree.sessions_of(TARGET) == []

    # Contract 04 §5 row 11: a failure after an allow, with its own reason.
    line = _delegate_lines(delegate)[-1]
    assert line["decision"] == "allow"
    assert line["reason"] == "delegate_timeout"


async def test_two_delegate_calls_at_once_both_finish(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """One sandbox of a thin family serves two jobs side by side."""
    set_pi_env(delegate.tree, **SLOW_JOB)
    session = session_of(chat_id())

    answers = await _ask_at_once(sandbox, session, AT_ONCE)

    for index, response in enumerate(answers):
        assert response.status_code == HTTP_OK, response.text
        assert f"{QUESTION}-{index}" in _result(response)["content"]

    assert delegate.tree.sessions_of(TARGET) == []
    assert len(delegate.pi_starts()) == AT_ONCE


async def test_a_third_call_at_once_is_rate_limited(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """Contract 04 §5 row 7. The cap is the caller's own, and the default is 2.

    The chaperone refuses the third call before `attendance` sees it.
    """
    set_pi_env(delegate.tree, **SLOW_JOB)

    answers = await _ask_at_once(sandbox, session_of(chat_id()), AT_ONCE + 1)

    refused = [response for response in answers if response.status_code != HTTP_OK]
    assert len(refused) == 1, [response.status_code for response in answers]
    assert refused[0].status_code == HTTP_TOO_MANY
    assert refused[0].json()["reason"] == "rate_limited"
    assert len(delegate.pi_starts()) == AT_ONCE


async def test_three_calls_finish_when_they_are_paced(
    delegate: DelegateStack, sandbox: httpx.AsyncClient
) -> None:
    """The cap bounds what is in flight, never how many calls a chat makes."""
    session = session_of(chat_id())

    first = await _ask_at_once(sandbox, session, AT_ONCE)
    second = await _ask_at_once(sandbox, session, 1)

    assert [response.status_code for response in [*first, *second]] == [HTTP_OK] * (AT_ONCE + 1)
    assert delegate.tree.sessions_of(TARGET) == []


async def _ask(client: httpx.AsyncClient, session: str, message: str = QUESTION) -> httpx.Response:
    """One `invoke_agent` call, as the bridge sends it (contract 04 §7.1)."""
    body = {"tool": INVOKE_AGENT, "args": {"family": TARGET, "message": message}}

    return await client.post(CALL_PATH, json=body, headers={SESSION_HEADER: session})


async def _ask_at_once(client: httpx.AsyncClient, session: str, count: int) -> list[httpx.Response]:
    """`count` calls of one chat, all in flight together, each with its own text."""
    return await asyncio.gather(
        *(_ask(client, session, f"{QUESTION}-{index}") for index in range(count))
    )


def _result(response: httpx.Response) -> dict[str, Any]:
    """The result of a 200 answer, by the rule of contract 04 §5.1.

    A `result` key is the result. A body without one is the result whole.
    """
    body: dict[str, Any] = response.json()
    result: dict[str, Any] = body.get("result", body)

    return result


def _delegate_lines(delegate: DelegateStack) -> list[dict[str, Any]]:
    """Every audit record of a delegate call, in the order it was written."""
    return [line for line in delegate.tree.audit_lines() if line.get("tool") == INVOKE_AGENT]


def _job_turn_files(delegate: DelegateStack) -> list[str]:
    """The text of every turn file a job holds now (contract 03 §7.4)."""
    control = delegate.tree.mounts(TARGET).control
    found: list[str] = []

    for path in sorted(control.glob("sessions/job-*/turn.json")):
        try:
            found.append(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue

    return [text for text in found if text.strip()]
