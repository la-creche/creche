"""Packet I2: stage 2, with the reconciler, `sessiond` and the supervisor
running together for the first time.

This is the operator's stage 2 test, invariant 9 made runnable: "A permission
change needs one action and no manual apply. It never ends a session. A
removal applies at once." One action here means one edit to one registry
file. Nothing else is touched, and every assertion reads something that
crossed a process boundary.

    family.yaml ──► managerd ──► grants, config mount, status.json
                          └────► POST /internal/switch-sandbox ──► sessiond
                                                                      │
    Open WebUI ──► door-owui ──────────────────────────────────────►──┘
                                                                      │
                                              fake_sbx ──► agent-supervisor
                                                                      │
                                                              fake-pi.mjs

Two of the eight are replace-class: a sandbox is built beside the old one
and the family moves onto it. Both sandboxes are real, each with its own
control directory, its own `supervisor.env` and its own supervisor process
(contract 03 §7.1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import httpx
import sse_read
from agent_family import FamilyState
from agent_pep.family_decisions import decide_family, manifest_actions
from agent_pep.family_grants import FamilyStore
from agent_pep.faults import FaultWriter
from agent_sessiond.auth import Principal
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import FAMILY, FIXTURE_PEP_TOKEN, SANDBOX, Stack, until
from stage2 import BreakTheIncomingEnv, Manager, http_switch_client, write_registry

CHAT_PATH = "/v1/chat/completions"
SWITCH_PATH = "/internal/switch-sandbox"
HTTP_OK = 200

NEXT_SANDBOX = "chat-s2"

#: Raised from 2, the family file's default, so the change is replace-class
#: and an ADDITION: contract 05 §5.4's first row, which drains.
MORE_CPUS = 4

#: A read-only mount whose removal is a REMOVAL: §5.4's second row, which
#: interrupts. One of contract 01 §3.3's allowed roots, and not the
#: platform root, which only `agent-control` may mount. The driver is a
#: fake, so nothing here opens the directory.
A_MOUNT = "/srv/agents/work/projects/notes"

EMBED = "embed"
HA_CALL = "ha_call"
HA_ALLOW: list[dict[str, str]] = [{"domain": "notify", "service": "mobile_app_example_phone"}]

TURN_STARTED = "turn_started"
TURN_SETTLED = "turn_settled"
TURN_FAILED = "turn_failed"
TURN_ABORTED = "turn_aborted"
TERMINAL_KINDS = (TURN_SETTLED, TURN_FAILED, TURN_ABORTED)
NOTE = "note"

SETTLE_TIMEOUT_S = 60.0
LONG_TURN_EVENTS = 200
LONG_TURN_GAP_MS = 25
SHORT_TURN_EVENTS = 3


# --- 1. a tool added --------------------------------------------------------


async def test_an_added_tool_reaches_the_next_manifest(stack: Stack, tmp_path: Path) -> None:
    """Contract 05 §3.5 row 1: a tool change lands live, per call.

    The PEP re-reads the grant file on every lookup, so the edit reaches the
    next manifest request with no sandbox touched and no session lost.
    """
    registry = write_registry(tmp_path / "registry", verbs={EMBED: {}})
    manager = Manager(stack, registry)
    await manager.pass_once()
    chat = chat_id()
    assert await _one_turn(stack, chat) == []
    assert _actions(manager) == [EMBED]

    manager.driver.calls.clear()
    write_registry(registry, verbs={EMBED: {}, HA_CALL: {"allow": HA_ALLOW}})
    await manager.pass_once()

    assert _actions(manager) == [EMBED, HA_CALL]
    assert manager.driver.ops() == ()
    assert manager.live_ids() == (SANDBOX,)

    # The session is the same one, and it still answers.
    assert await _one_turn(stack, chat) == []
    assert _kinds(stack, session_of(chat)).count(TURN_SETTLED) == 2


# --- 2. a tool removed ------------------------------------------------------


async def test_a_removed_tool_is_refused_on_the_next_call(stack: Stack, tmp_path: Path) -> None:
    """Invariant 9's last sentence: a removal applies at once.

    No sandbox is replaced and no process is restarted. The next `/call`
    the PEP decides reads the file `managerd` has already rewritten.
    """
    both = {EMBED: {}, HA_CALL: {"allow": HA_ALLOW}}
    registry = write_registry(tmp_path / "registry", verbs=both)
    manager = Manager(stack, registry)
    await manager.pass_once()

    assert _decide(manager, HA_CALL, HA_ALLOW[0]).allow is True

    write_registry(registry, verbs={EMBED: {}})
    await manager.pass_once()

    refused = _decide(manager, HA_CALL, HA_ALLOW[0])

    assert refused.allow is False
    assert refused.reason == "tool_not_granted"
    assert _actions(manager) == [EMBED]


# --- 3. the instructions changed --------------------------------------------


async def test_new_instructions_recycle_at_the_turn_boundary(stack: Stack, tmp_path: Path) -> None:
    """Contract 01 §6.1 and contract 03 §6: `config_rev` moves, and the
    held-open process is replaced at the NEXT turn, never mid-turn.

    The persona path is what the new process is started with: contract 03
    §7.1 sends `instructions.md` to pi as a path on argv, so a reader of the
    spawn log sees the new one and never the text.
    """
    registry = write_registry(tmp_path / "registry", instructions="Be helpful.\n")
    manager = Manager(stack, registry)
    await manager.pass_once()
    chat = chat_id()

    assert await _one_turn(stack, chat) == []

    before = len(stack.pi_starts())
    first_rev = manager.status()["config_rev"]

    write_registry(registry, instructions="Answer only in metric units.\n")
    await manager.pass_once()

    # The edit alone touches no process: nothing runs between the two turns.
    assert len(stack.pi_starts()) == before
    assert manager.status()["config_rev"] != first_rev

    assert await _one_turn(stack, chat) == []

    started = stack.pi_starts()
    instructions = stack.mounts.config / "instructions.md"

    assert len(started) == before + 1
    assert str(instructions) in started[-1]
    assert instructions.read_text(encoding="utf-8") == "Answer only in metric units.\n"


# --- 4. a replace-class addition --------------------------------------------


async def test_more_cpus_drains_onto_a_second_sandbox(stack: Stack, tmp_path: Path) -> None:
    """Contract 05 §5.3 rule 2, end to end on two real supervisors.

    A running turn finishes on the outgoing sandbox. The next turn runs on
    the incoming one. The outgoing sandbox is destroyed, and the session and
    its history are untouched.
    """
    registry = write_registry(tmp_path / "registry")
    manager = Manager(stack, registry)
    await manager.pass_once()
    chat = chat_id()
    session = session_of(chat)

    assert await _one_turn(stack, chat) == []

    stack.set_pi_env(events=LONG_TURN_EVENTS, delay_ms=LONG_TURN_GAP_MS)
    running = asyncio.create_task(_one_turn(stack, chat))
    await _started(stack, session, 2)

    write_registry(registry, sandbox={"cpus": MORE_CPUS})
    result = await manager.pass_once()

    # The drain waited for the turn, and the turn finished rather than died.
    assert await running == []
    assert result.status.state is FamilyState.IN_SYNC
    assert manager.live_ids() == (NEXT_SANDBOX,)
    assert "destroy" in manager.driver.ops()

    stack.set_pi_env(events=SHORT_TURN_EVENTS, delay_ms=1)

    assert await _one_turn(stack, chat) == []
    assert _sandboxes_of(stack, session) == [SANDBOX, SANDBOX, NEXT_SANDBOX]
    assert _kinds(stack, session).count(TURN_SETTLED) == 3
    assert _notes(stack, session) == ["sandbox_switched"]


# --- 5. a replace-class removal ---------------------------------------------


async def test_a_removed_mount_interrupts_the_running_turn(stack: Stack, tmp_path: Path) -> None:
    """Contract 05 §5.3 rule 3. A turn that still holds the removed reach
    must not be allowed to finish, so the abort is immediate and the client
    is told why."""
    registry = write_registry(tmp_path / "registry", files=[{"path": A_MOUNT, "mode": "ro"}])
    manager = Manager(stack, registry)
    await manager.pass_once()
    chat = chat_id()
    session = session_of(chat)

    stack.set_pi_env(events=LONG_TURN_EVENTS, delay_ms=LONG_TURN_GAP_MS)
    running = asyncio.create_task(_one_turn(stack, chat))
    await _started(stack, session, 1)

    write_registry(registry)
    await manager.pass_once()

    errors = await running

    assert [one["error"]["code"] for one in errors] == ["permission_removed"]
    assert _kinds(stack, session)[-1] == TURN_ABORTED

    stack.set_pi_env(events=SHORT_TURN_EVENTS, delay_ms=1)

    assert await _one_turn(stack, chat) == []
    assert _sandboxes_of(stack, session) == [SANDBOX, NEXT_SANDBOX]


# --- 6. a switch that cannot complete ---------------------------------------


async def test_a_switch_that_cannot_complete_keeps_serving(stack: Stack, tmp_path: Path) -> None:
    """Contract 05 §5.3 rule 8, the case packet CM could only guess at.

    The incoming sandbox's `supervisor.env` names none of its mounts, so the
    real supervisor answers `fatal` with `mount_dir_unset` instead of
    `ready` (contract 03 §5.7) and the handshake the switch asks for cannot
    pass. Nothing moves: the family keeps answering on the sandbox it was
    already serving on, both sandboxes are accounted for, and the document
    says which sandbox could not start.

    Until packet FX2, `managerd` dropped `sessiond`'s `sandbox_start_failed`,
    so this scenario could only assert the `reconcile` block: "an operator
    sees that a switch is outstanding, but not why". The fault now says why,
    and the family is `degraded` rather than `reconciling` because §3's
    `degraded` row wins over the `reconciling` one when both hold.
    """
    registry = write_registry(tmp_path / "registry")
    breaking = BreakTheIncomingEnv(stack, http_switch_client(stack))
    manager = Manager(stack, registry, switch=breaking)
    await manager.pass_once()
    chat = chat_id()

    assert await _one_turn(stack, chat) == []

    write_registry(registry, sandbox={"cpus": MORE_CPUS})
    refused = await manager.pass_once()

    assert refused.status.state is FamilyState.DEGRADED
    assert any(one.startswith("switch_sandbox:refused") for one in refused.ran)
    assert "destroy_sandbox" not in refused.ran
    assert "destroy" not in manager.driver.ops()

    # Both sandboxes are accounted for, in the ledger and in the document.
    assert set(manager.live_ids()) == {SANDBOX, NEXT_SANDBOX}
    assert manager.published_ids() == [SANDBOX, NEXT_SANDBOX]

    # The document names the sandbox that could not start, and says the
    # family still serves: the fault is the INCOMING sandbox's, and the
    # outgoing one is answering every turn (contract 05 §5.3).
    fault = _one_fault(manager.status(), "sandbox_start_failed")
    assert fault["sandbox"] == NEXT_SANDBOX
    assert fault["blocks_turns"] is False

    # The family keeps serving, on the sandbox it was already serving on.
    assert await _one_turn(stack, chat) == []
    assert _sandboxes_of(stack, session_of(chat)) == [SANDBOX, SANDBOX]


async def test_a_later_pass_completes_the_refused_switch(stack: Stack, tmp_path: Path) -> None:
    """The converge half of scenario 6. `managerd` writes the env file at
    every create, so the pass after the one that could not finish hands the
    supervisor its mounts and the same call succeeds."""
    registry = write_registry(tmp_path / "registry")
    breaking = BreakTheIncomingEnv(stack, http_switch_client(stack))
    manager = Manager(stack, registry, switch=breaking)
    await manager.pass_once()
    chat = chat_id()

    assert await _one_turn(stack, chat) == []

    write_registry(registry, sandbox={"cpus": MORE_CPUS})
    await manager.pass_once()

    # The next pass rewrites the env file and makes the same call.
    healed = await manager.pass_once()

    assert healed.status.state is FamilyState.IN_SYNC
    assert manager.live_ids() == (NEXT_SANDBOX,)
    assert await _one_turn(stack, chat) == []
    assert _sandboxes_of(stack, session_of(chat))[-1] == NEXT_SANDBOX


async def test_the_managerd_token_clears_the_internal_check(stack: Stack, tmp_path: Path) -> None:
    """Kept from packet CM's own test. A refusal for any reason raises the
    same `SwitchError`, so the scenarios above would pass even with a token
    `sessiond` rejects. This asks `sessiond` directly."""
    registry = write_registry(tmp_path / "registry")
    manager = Manager(stack, registry)
    await manager.pass_once()

    response = await _switch_as_managerd(stack, to=SANDBOX)

    # Not 401 and not 404: the token authenticates, the principal may make
    # an internal call, and the body parsed. Only the pair is refused,
    # because `from` and `to` name one sandbox (contract 05 §5.3 rule 8).
    assert response.status_code != httpx.codes.UNAUTHORIZED
    assert response.json()["error"]["code"] == "bad_request"


# --- 7. an invalid family file ----------------------------------------------


async def test_an_invalid_file_reports_and_keeps_serving(stack: Stack, tmp_path: Path) -> None:
    """Invariant 19 and contract 05 §3.1. A bad definition yields a report,
    never an outage, and the last good state keeps serving."""
    registry = write_registry(tmp_path / "registry")
    manager = Manager(stack, registry)
    applied = await manager.pass_once()
    chat = chat_id()

    assert await _one_turn(stack, chat) == []

    (registry / "families" / FAMILY / "family.yaml").write_text(
        "name: chat\nkind: nonsense\n", encoding="utf-8"
    )
    broken = await manager.pass_once()
    document = manager.status()

    assert broken.status.state is FamilyState.INVALID
    assert document["validation"]["ok"] is False
    assert document["validation"]["never_valid"] is False
    assert document["validation"]["error_count"] > 0
    assert document["applied_rev"] == applied.status.registry_rev

    # Nothing was touched, and the family answers on the same sandbox.
    assert manager.live_ids() == (SANDBOX,)
    assert await _one_turn(stack, chat) == []
    assert _sandboxes_of(stack, session_of(chat)) == [SANDBOX, SANDBOX]


# --- 8. managerd killed mid-replacement -------------------------------------


async def test_a_kill_between_publish_and_destroy_converges(stack: Stack, tmp_path: Path) -> None:
    """Contract 05 §4.3 then §5 then §4.4, interrupted in the middle.

    The first `managerd` publishes the replacement and dies before the
    destroy. The second one adopts what it finds rather than building a
    third sandbox, and at no moment did either grant a sandbox more reach
    than the family file allows (invariant 9, narrowing before widening).
    """
    registry = write_registry(tmp_path / "registry")
    killed = Manager(stack, registry, switch=_DieAfterPublish(stack))
    await killed.pass_once()
    chat = chat_id()

    assert await _one_turn(stack, chat) == []

    write_registry(registry, sandbox={"cpus": MORE_CPUS})
    await killed.pass_once()

    assert set(killed.live_ids()) == {SANDBOX, NEXT_SANDBOX}

    # A fresh process, with the state root as the only thing it inherits.
    revived = Manager(stack, registry)
    result = await revived.pass_once()

    assert result.status.state is FamilyState.IN_SYNC
    assert revived.live_ids() == (NEXT_SANDBOX,)
    assert revived.published_ids() == [NEXT_SANDBOX]

    allowed = _family_reach()

    for manager in (killed, revived):
        for sandbox in (SANDBOX, NEXT_SANDBOX):
            assert set(manager.reach_of(sandbox)) <= allowed

    assert await _one_turn(stack, chat) == []
    assert _sandboxes_of(stack, session_of(chat)) == [SANDBOX, NEXT_SANDBOX]


class _DieAfterPublish:
    """A `managerd` that never reaches its own §5 call.

    Contract 05 §4.3 step 5b publishes the replacement first, so this is
    the window between "new sandbox published" and "old sandbox destroyed"
    that the scenario names. A refusal leaves the reconciler in exactly the
    state a kill there would.
    """

    def __init__(self, stack: Stack) -> None:
        self._stack = stack

    def switch(self, request: object) -> object:
        from agent_managerd.switch import SwitchError

        raise SwitchError("managerd was killed before the call was answered")


# --- reading what crossed a boundary ----------------------------------------


def _family_reach() -> set[str]:
    """Every host an `egress: []` attended family may reach: the two plane
    endpoints `managerd` adds itself (contract 05 §4.3 step 3)."""
    from agent_managerd.egress import litellm_endpoint, pep_endpoint

    return {litellm_endpoint(), pep_endpoint()}


def _grants(manager: Manager) -> Any:
    """The PEP's own reader over the file `managerd` just wrote.

    Every lookup re-scans, which is what makes a tool change land per call
    (contract 04 §1.4). The HTTP path around it is proved in
    `integration/tests_manager/test_grants_to_pep.py`.
    """
    store = FamilyStore(manager.state_root / "grants", FaultWriter(manager.state_root / "pep-out"))
    found = store.lookup(FIXTURE_PEP_TOKEN)

    assert found is not None, "the PEP does not recognise the token managerd published"

    return found


def _actions(manager: Manager) -> list[str]:
    """The manifest the next bridge start would be given (contract 04 §4)."""
    return manifest_actions(_grants(manager), frozenset())


def _decide(manager: Manager, tool: str, args: dict[str, Any]) -> Any:
    """One `/call` decision, as the PEP makes it (contract 04 §5)."""
    return decide_family(_grants(manager), tool, dict(args), rate_exceeded=False)


async def _one_turn(stack: Stack, chat: str) -> list[dict[str, Any]]:
    """One streamed turn through the real door. Returns its error chunks."""
    client = stack.client

    assert client is not None

    async with client.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("hello", stream=True),
    ) as response:
        assert response.status_code == HTTP_OK, await response.aread()
        body = "".join([part async for part in response.aiter_text()])

    frames = sse_read.parse(body)

    assert frames.ends_with_done, body

    await _await_every_turn_ended(stack, session_of(chat))

    return frames.error_chunks


async def _await_every_turn_ended(stack: Stack, session: str) -> None:
    """Wait for the host journal, which lands AFTER the client's `[DONE]`.

    The door ends its stream at pi's own `agent_settled` event (contract 02
    §8.2). `sessiond` writes the terminal line when the supervisor's message
    arrives, which is later: 5 ms in the run that caught this, under load. A
    test that counts `turn_settled` the instant a stream ends reads too early.
    """
    await until(
        lambda: _open_turns(stack, session) == 0,
        f"every started turn of {session} to reach a terminal line",
        SETTLE_TIMEOUT_S,
    )


def _open_turns(stack: Stack, session: str) -> int:
    kinds = _kinds(stack, session)

    return kinds.count(TURN_STARTED) - sum(kinds.count(kind) for kind in TERMINAL_KINDS)


async def _switch_as_managerd(stack: Stack, *, to: str) -> httpx.Response:
    client = stack.sessiond_as(Principal.MANAGERD)

    return await client.post(
        SWITCH_PATH,
        json={
            "family": FAMILY,
            "from": SANDBOX,
            "to": to,
            "mode": "drain",
            "reason": "sandbox.cpus changed: 2 to 4",
            "deadline_s": 300,
        },
    )


async def _started(stack: Stack, session: str, count: int) -> None:
    """Wait until the journal shows a turn has reached the sandbox."""
    await until(
        lambda: _kinds(stack, session).count(TURN_STARTED) >= count,
        f"{session} to start {count} turns",
        SETTLE_TIMEOUT_S,
    )


def _kinds(stack: Stack, session: str) -> list[str]:
    return [str(line.get("kind", "")) for line in stack.journal_lines(session)]


def _one_fault(status: dict[str, Any], code: str) -> dict[str, Any]:
    """The one fault of that code in the status document (contract 05 §3.3)."""
    found = [one for one in status["faults"] if one.get("code") == code]
    assert len(found) == 1, status["faults"]

    return cast("dict[str, Any]", found[0])


def _sandboxes_of(stack: Stack, session: str) -> list[str]:
    """The sandbox each turn started on, in order (contract 02 §8)."""
    return [
        str(_body(line).get("sandbox", ""))
        for line in stack.journal_lines(session)
        if line.get("kind") == TURN_STARTED
    ]


def _notes(stack: Stack, session: str) -> list[str]:
    return [
        str(_body(line).get("note", ""))
        for line in stack.journal_lines(session)
        if line.get("kind") == NOTE
    ]


def _body(line: dict[str, Any]) -> dict[str, Any]:
    body = line.get("body")

    return body if isinstance(body, dict) else {}
