"""The exit test of phase 2 (plan row 30): six scenarios on one topology.

`proc_exit.py` draws the topology. Each scenario plays Panda: a WebSocket
client of ACP v1 at door-acp, with the door bearer. The model is the model
stand-in, which answers from the script of each scenario.

Each assertion reads what crossed a process boundary: an ACP frame, the
audit file of the chaperone, a record of a stand-in. The text of each
failed assertion names the log that explains it, and the report of a failed
test holds each of those logs (`integration/proc/AGENTS.md`, "Reading a
failure").

`conftest.py` marks every test of this directory `slow`. The fixture
`scenario_budget` fails a scenario that takes 60 seconds or more, the start of the
topology and its end included.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Iterator
from typing import Final

import pytest
from acp_client import (
    AcpClient,
    MessageChunk,
    OtherUpdate,
    ToolCall,
    ToolCallUpdate,
    Update,
)
from proc_exit import CALLER, TARGET, ExitStack, explain, json_of, wait_until
from proc_standins import (
    MODEL,
    MODEL_DELAY_MS,
    SBX,
    answer_gate,
    approval_requests,
    calls_of,
    script_model,
    text_turn,
    tool_turn,
    tune,
    upstream_calls,
)
from proc_tree import RS_ATTENDANCE, RS_UPSTREAM, first_sandbox

#: The budget of one scenario, in seconds.
BUDGET_S: Final = 60.0

ECHO: Final = f"{RS_UPSTREAM}__echo"
GATED_ECHO: Final = f"{RS_UPSTREAM}__gated_echo"
INVOKE_AGENT: Final = f"{RS_ATTENDANCE}__invoke_agent"

#: The connection `ch` of the agent module puts `mcp__ch__` before each
#: exposed name of the chaperone (`agent/src/chaperone.ts`).
CH: Final = "mcp__ch__"

ANSWER: Final = "Hello from the scripted model."
DONE: Final = "Done."

#: The fixed text of a gated call (chaperone `docs/design.md` §5.3).
AWAITING: Final = re.compile(r"Awaiting permission for tool call \S+ \(gate ([0-9a-f]{32})\)")

#: A long turn, and the milliseconds between two of its words. The turn
#: takes about 8 seconds, so a cancel lands while it streams.
SLOW_WORDS: Final = 40
SLOW_DELAY_MS: Final = "200"

#: What family B answers, and what family A asks it.
ORACLE_ANSWER: Final = "The oracle says forty-two."
ORACLE_QUESTION: Final = "What does the oracle say?"


@pytest.fixture(autouse=True)
def scenario_budget() -> Iterator[None]:
    """Each scenario, its topology included, ends within `BUDGET_S`."""
    started = time.monotonic()

    yield

    spent = time.monotonic() - started
    assert spent < BUDGET_S, f"the scenario took {spent:.1f} s, over its budget of {BUDGET_S} s"


def test_a_prompt_ends_its_turn_with_the_text_of_the_model(exit_stack: ExitStack) -> None:
    """Scenario 1: `session/new`, then one prompt that ends `end_turn`.

    The text of the turn reaches the client as `agent_message_chunk`.
    """
    script_model(exit_stack.tree, [text_turn(ANSWER)])

    with exit_stack.client() as client:
        session = client.new_session(exit_stack.cwd())
        reason = client.prompt(session, "Say hello.")

    assert reason == "end_turn", explain("facade", "model")
    assert client.agent_text(session) == ANSWER, explain("facade", "model")


def test_an_allowed_call_runs_and_the_audit_holds_decided_then_ran(
    exit_stack: ExitStack,
) -> None:
    """Scenario 2: the model calls `echo`, which the policy allows.

    The client sees `tool_call`, then `tool_call_update` `completed` with
    the result of the upstream. The audit file holds `decided`, then `ran`
    (chaperone `docs/design.md` §5.4).
    """
    script_model(exit_stack.tree, [tool_turn(CH + ECHO, {"text": "hi"}), text_turn(DONE)])

    with exit_stack.client() as client:
        session = client.new_session(exit_stack.cwd())
        reason = client.prompt(session, "Echo hi.")

    calls = _tool_updates(client.updates_of(session))

    assert reason == "end_turn", explain("facade", "model")
    assert calls == [
        ("tool_call", CH + ECHO, "in_progress", ""),
        ("tool_call_update", None, "completed", '{"text":"hi"}'),
    ], explain("facade", "chaperone")
    assert _events(exit_stack, ECHO) == [("decided", "allow"), ("ran", "is_error:false")], explain(
        "chaperone"
    )
    assert [
        (call["tool"], call["meta"]["chaperone.app"]) for call in upstream_calls(exit_stack.tree)
    ] == [("echo", CALLER)], explain("chaperone")
    assert client.agent_text(session) == DONE, explain("facade", "model")


def test_a_gated_call_waits_for_the_approval_then_gives_the_kept_result(
    exit_stack: ExitStack,
) -> None:
    """Scenario 3: the model calls `gated_echo`, which needs an approval.

    The first call ends its turn with the text "Awaiting permission". The
    test approves the gate through the file of the approval stand-in. An
    identical call in the next turn collects the kept result. The audit file
    holds `decided` (gate), `waiting`, `ran`, `decided` (allow), `delivered`
    (§5.3, §5.4).
    """
    call = tool_turn(CH + GATED_ECHO, {"text": "x"})
    script_model(exit_stack.tree, [call, text_turn("Waiting."), call, text_turn(DONE)])

    with exit_stack.client() as client:
        session = client.new_session(exit_stack.cwd())
        first = client.prompt(session, "Echo x.")
        held = _tool_updates(client.updates_of(session))
        found = AWAITING.search(str(held[-1][3]))

        assert first == "end_turn", explain("facade", "model")
        assert found is not None, f"no gate in {held}. " + explain("facade", "chaperone")

        gate = found.group(1)
        wait_until(lambda: _request_of(exit_stack, gate), f"approve call of {gate}", "chaperone")
        answer_gate(exit_stack.tree, gate)
        wait_until(lambda: "ran" in exit_stack.audit_events(GATED_ECHO) or None, "ran", "chaperone")
        before = len(client.updates_of(session))
        second = client.prompt(session, "Approved. Call it again.")
        kept = _tool_updates(client.updates_of(session)[before:])

    assert [entry[:3] for entry in held] == [
        ("tool_call", CH + GATED_ECHO, "in_progress"),
        ("tool_call_update", None, "failed"),
    ], explain("facade", "chaperone")
    assert second == "end_turn", explain("facade", "model")
    assert kept == [
        ("tool_call", CH + GATED_ECHO, "in_progress", ""),
        ("tool_call_update", None, "completed", '{"text":"x"}'),
    ], explain("facade", "chaperone")
    assert _events(exit_stack, GATED_ECHO) == [
        ("decided", f"gate:{gate}"),
        ("waiting", f"gate:{gate}"),
        ("ran", "is_error:false"),
        ("decided", "allow"),
        ("delivered", f"gate:{gate}"),
    ], explain("chaperone")
    assert len(upstream_calls(exit_stack.tree)) == 1, explain("chaperone")


def test_a_cancel_mid_turn_ends_the_prompt_cancelled(exit_stack: ExitStack) -> None:
    """Scenario 4: `session/cancel` while the model streams a slow turn.

    The prompt answers `cancelled`. The model streams one word every
    `SLOW_DELAY_MS`, so the turn runs when the cancel comes.
    """
    words = " ".join(f"word{at}" for at in range(SLOW_WORDS))
    tune(exit_stack.tree, MODEL, MODEL_DELAY_MS, SLOW_DELAY_MS)
    script_model(exit_stack.tree, [text_turn(words)])

    with exit_stack.client() as client:
        session = client.new_session(exit_stack.cwd())
        asked = client.start_prompt(session, "Talk slowly.")
        client.wait_for(lambda update: isinstance(update, MessageChunk))
        client.cancel(session)
        reason = client.stop_reason(asked)

    assert reason == "cancelled", explain("facade", "model")
    assert len(client.agent_text(session)) < len(words), explain("facade", "model")


def test_a_reconnect_loads_the_session_as_the_first_connection_saw_it(
    exit_stack: ExitStack,
) -> None:
    """Scenario 5: drop the WebSocket, reconnect, `session/load`.

    The replay equals what the first connection saw. The comparison reads
    the conversation: each text joined across its chunks, each tool call and
    each tool result. Two differences are the protocol's, so the comparison
    removes them. The replay starts each turn with the prompt as a
    `user_message_chunk`, which a live client sent itself. A live client
    gets `session_info_update`, which is no part of the conversation.
    `session/list` names the session.
    """
    prompt = "Echo hi, then say done."
    script_model(exit_stack.tree, [tool_turn(CH + ECHO, {"text": "hi"}), text_turn(DONE)])

    with exit_stack.client() as first:
        session = first.new_session(exit_stack.cwd())
        reason = first.prompt(session, prompt)
        seen = first.updates_of(session)
        _drop(first)

    with exit_stack.client() as second:
        replay = second.load_session(session, exit_stack.cwd())
        listed = second.list_sessions()

    assert reason == "end_turn", explain("facade", "model")
    assert _conversation(replay) == [("user_message_chunk", prompt), *_conversation(seen)], explain(
        "facade"
    )
    assert session in [entry.get("sessionId") for entry in listed["sessions"]], (
        f"session/list gave {listed}. " + explain("facade")
    )


def test_family_a_asks_family_b_and_gets_its_answer_as_the_tool_result(
    exit_stack: ExitStack,
) -> None:
    """Scenario 6: `chat` calls `invoke_agent` with `vault-oracle`.

    The chaperone's plugin `attendance` posts the call to the delegate door.
    `attendance` starts the facade of `vault-oracle`, which answers from the
    script. The text comes back to `chat` as the result of the tool, in the
    frame of untrusted data (§12). The model stand-in takes the turns in
    arrival order: the call of `chat`, the answer of `vault-oracle`, the
    last word of `chat`.
    """
    asked = {"family": TARGET, "message": ORACLE_QUESTION}
    turns = [tool_turn(CH + INVOKE_AGENT, asked), text_turn(ORACLE_ANSWER), text_turn(DONE)]
    script_model(exit_stack.tree, turns)

    with exit_stack.client() as client:
        session = client.new_session(exit_stack.cwd())
        reason = client.prompt(session, "Ask the oracle.")

    calls = _tool_updates(client.updates_of(session))
    facades = [call.argv for call in calls_of(exit_stack.tree, SBX) if "creche-facade" in call.argv]

    assert reason == "end_turn", explain("facade", "model")
    assert [entry[:3] for entry in calls] == [
        ("tool_call", CH + INVOKE_AGENT, "in_progress"),
        ("tool_call_update", None, "completed"),
    ], explain("facade", "chaperone", "attendance")
    assert json_of(str(calls[-1][3])) == {
        "untrusted": True,
        "source": f"family:{TARGET}",
        "content": ORACLE_ANSWER,
    }, explain("chaperone", "attendance", "facade")
    assert [argv[argv.index("--") - 1] for argv in facades] == [
        first_sandbox(CALLER),
        first_sandbox(TARGET),
    ], explain("attendance")
    assert _events(exit_stack, INVOKE_AGENT) == [("decided", "allow"), ("ran", "is_error:false")], (
        explain("chaperone")
    )
    assert client.agent_text(session) == DONE, explain("facade", "model")


# ---------------------------------------------------------------- readers


def _tool_updates(updates: list[Update]) -> list[tuple[str, str | None, str | None, str]]:
    """Each tool update: its kind, the title of a call, its status, the text of a result."""
    found: list[tuple[str, str | None, str | None, str]] = []

    for update in updates:
        if isinstance(update, ToolCall):
            found.append(("tool_call", update.title, update.status, ""))
        elif isinstance(update, ToolCallUpdate):
            found.append(("tool_call_update", None, update.status, update.text))

    return found


def _conversation(updates: list[Update]) -> list[tuple[str, ...]]:
    """The conversation of some updates: texts joined across chunks, tool calls, results."""
    found: list[tuple[str, ...]] = []

    for update in updates:
        if isinstance(update, MessageChunk):
            if found and found[-1][0] == update.kind:
                found[-1] = (update.kind, found[-1][1] + update.text)
            else:
                found.append((update.kind, update.text))
        elif isinstance(update, ToolCall):
            found.append(("tool_call", update.tool_call_id, update.title, str(update.status)))
        elif isinstance(update, ToolCallUpdate):
            found.append(("tool_call_update", update.tool_call_id, str(update.status), update.text))
        elif isinstance(update, OtherUpdate):
            found.append((update.kind,))
        # A `SessionInfo` is metadata of the session, no part of the conversation.

    return found


def _events(stack: ExitStack, tool: str) -> list[tuple[str, str]]:
    """The event and the detail of each audit record of one exposed tool."""
    return [
        (str(record["event"]), str(record["detail"]))
        for record in stack.audit()
        if record.get("tool") == tool
    ]


def _request_of(stack: ExitStack, gate: str) -> dict[str, object] | None:
    """The `approve` call of one gate, once the approval stand-in holds it."""
    for request in approval_requests(stack.tree):
        if request["gate"] == gate and request["app"] == CALLER:
            return request

    return None


def _drop(client: AcpClient) -> None:
    """End the TCP connection under a WebSocket with no close frame, as a lost network does."""
    client.socket.socket.shutdown(socket.SHUT_RDWR)
