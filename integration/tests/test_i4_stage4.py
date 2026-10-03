"""Packet I4: stage 4, one session in two UIs, and the platform fence.

The operator's stage 4 test is "start a coding session on the phone, continue it in
the terminal, and see both UIs show the same session. `code` fails to write
to a platform repo. `agent-control` succeeds." Invariant 3 says the Open
WebUI chat and the TUI session within one family ARE the same session.

The pieces were built by agents who never saw each other's code: the TUI
door (packet CT), the lease rulings and `release-process` (packet CS2,
contract 02 draft 6), the Open WebUI write-back and the edit-or-regenerate
branch (the same packet), and the platform fence in `caregiver` and the PEP
(contract 01 §5.5). This file is the first time they run together.

    the phone  ─http─► door-owui ─uds─► attendance ─► playpen ─► fake pi
    the terminal ────► door-tui  ─uds─►    │
                                           └─http─► FakeOwui (the write-back)

`stage4.py` holds the harness and says what is real and what stands in.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_door_tui.attendance import (
    CODE_LEASE_TAKEN_OVER,
    CODE_SESSION_BUSY,
    AttendanceError,
    Intent,
    Takeover,
)
from agent_door_tui.ids import new_session_id
from conftest import chat_id, message_id, session_of
from stack import FAMILY, Stack
from stage4 import (
    AGENT_CONTROL,
    CODE,
    FIXTURE_FOLDER_ID,
    PLATFORM_ROOT,
    FakeOwui,
    OwuiMood,
    Stage4,
    in_thread,
    owui_config,
    serving_owui,
    serving_pep,
    settle,
)

#: Two terminals, named as `config.door_instance` names one. The pid is an
#: identifier, never a secret.
TERMINAL_A = "tui.5001"
TERMINAL_B = "tui.5002"

HTTP_OK = 200

#: Long enough that a scenario can act while the turn is in flight, and short
#: enough that a whole file of them still finishes. `fake-pi.mjs` sleeps
#: `delay_ms` between deltas, so this is about three seconds of turn.
SLOW_TURN_EVENTS = 150
SLOW_TURN_GAP_MS = 20

#: One tool from each mirrored GitHub server (contract 01b §8.2 and §8.3).
#: The PEP's call name is `<server>__<tool>`.
PLATFORM_TOOL = "github-platform__create_pull_request"
CODE_TOOL = "github-code__create_pull_request"

#: Contract 04 §5's two refusals that mean "policy said no", from
#: `chaperone.family_decisions.FamilyReason`. Every other reason is a call
#: that got PAST the decision and then failed to run, which is what a
#: harness with no `github-mcp-server` behind it expects.
POLICY_DENIALS = frozenset({"tool_not_granted", "unknown_token"})

#: A denied call answers at once. The PEP's own ceiling is far higher, and
#: nothing here should reach it.
PEP_TIMEOUT_S = 30.0

#: How long the fake Open WebUI holds a write, and how long a loaded Mac may
#: leave the event loop alone before that counts as the stall. The gap is
#: wide on purpose: a healthy run measures tens of milliseconds, and the bug
#: this scenario exists for measured the whole `DAWDLE_S`.
DAWDLE_S = 3.0
STALL_LIMIT_S = 1.0
BEAT_S = 0.02


async def call_pep(client: httpx.AsyncClient, token: str, tool: str) -> dict[str, Any]:
    """One tool call from one family's sandbox (contract 04 §7.1).

    The family token is the whole identity: every sandbox's traffic reaches
    the host from one address, so nothing rests on where the call came
    from.
    """
    answer = await client.post(
        "/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": tool, "args": {}},
    )
    body: Any = answer.json()

    return body if isinstance(body, dict) else {"ok": False, "reason": str(body)}


@pytest.fixture
async def stage(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stage4]:
    """The stage 1 stack, with a fake Open WebUI behind `attendance`.

    The fake is serving and its three settings are in the `Config` BEFORE
    `serve()`: `attendance` reads them once, when the service is built.
    """
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()
    owui = FakeOwui()
    ready = Stage4(built, owui)

    with serving_owui(owui) as url:
        owui_config(ready, url, monkeypatch)

        for name, value in ready.sandbox_environ().items():
            monkeypatch.setenv(name, value)

        await built.serve()

        try:
            yield ready
        finally:
            await built.close()


@dataclass(frozen=True)
class Pair:
    """The two message ids Open WebUI mints for one turn.

    The browser makes both before the request: the user message it just
    added, and the assistant message it is about to fill. Contract 02 §10.1
    maps each to a pi entry, and §10.2 reads the pair to place an edit.
    """

    user: str = field(default_factory=message_id)
    assistant: str = field(default_factory=message_id)


async def owui_pair(
    stage: Stage4, chat: str, pair: Pair, text: str, parent: str | None = None
) -> None:
    """One phone turn that maps BOTH of its messages."""
    answer = await stage.owui_turn(
        chat, pair.assistant, text, user_message=pair.user, parent=parent
    )

    assert answer.status_code == HTTP_OK, answer.text


def settled_count(stage: Stage4, session: str) -> int:
    return len(stage.lines_of(session, "turn_settled"))


def prompts(stage: Stage4, session: str) -> list[str]:
    """Every turn's prompt, in journal order (contract 02 §8.1)."""
    return [str(line["body"]["prompt"]) for line in stage.lines_of(session, "turn_started")]


async def tui_session(stage: Stage4, title: str = "a terminal session") -> str:
    """One session born in the terminal, made the way the door makes one.

    Step 3 of the door's order: `attendance` owns sessions, so it creates one
    before pi writes a byte. The id carries contract 02 §2's `tui-` prefix,
    which is what tells the write-back this session has no chat yet.
    """
    session = new_session_id()
    door = stage.tui_client(TERMINAL_A)

    try:
        await in_thread(lambda: door.create_session(FAMILY, session, title))
    finally:
        door.close()

    return session


def copied_turns(stage: Stage4) -> list[tuple[str, str]]:
    """Every turn that REACHED the chat, as (prompt, answer), in order.

    This is the whole point of contract 02 §10.4: what was typed in the
    terminal has to show up on the phone. So the assertion is on the
    content that crossed the boundary, not on a count of requests, and a
    refused write is not a turn that arrived.
    """
    found: list[tuple[str, str]] = []

    for call in stage.owui.answered():
        roles = {
            str(one.get("role", "")): str(one.get("content", ""))
            for one in call.messages().values()
        }

        if "user" in roles and "assistant" in roles:
            found.append((roles["user"], roles["assistant"]))

    return found


def copied_prompts(stage: Stage4) -> list[str]:
    return [prompt for prompt, _ in copied_turns(stage)]


def answers_echo_prompts(stage: Stage4) -> bool:
    """The fake pi echoes the prompt into its deltas, so the answer that
    reached the chat has to start with the prompt that caused it."""
    return all(answer.startswith(prompt) for prompt, answer in copied_turns(stage))


class LoopBeat:
    """The longest the event loop went without running this task.

    One process serves every session of every family, so the loop is the
    shared resource. A gap here is every chat and every terminal frozen
    for that long, which no journal line and no HTTP status records.
    """

    def __init__(self) -> None:
        self.worst = 0.0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._beat())

    async def stop(self) -> None:
        task = self._task
        self._task = None

        if task is None:
            return

        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _beat(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()

        while True:
            await asyncio.sleep(BEAT_S)
            now = loop.time()
            self.worst = max(self.worst, now - last - BEAT_S)
            last = now


async def test_i4_phone_to_terminal_and_back(stage: Stage4) -> None:
    """Scenario 1. One session, two doors, and the lease moving both ways.

    Contract 02 §7.3 rule 5 hands an IDLE lease to another door with no
    `--force` and no wait. That is what makes invariant 3 usable: a chat
    started on the phone continues in the terminal in the next second, and
    the phone takes it back the same way.
    """
    chat = chat_id()
    session = session_of(chat)

    answer = await stage.owui_turn(chat, message_id(), "from the phone")
    assert answer.status_code == HTTP_OK, answer.text
    await settle(lambda: settled_count(stage, session) == 1, "the phone's turn")

    door = stage.tui_client(TERMINAL_A)

    try:
        # No `--force` and no wait, on a lease the Open WebUI door took
        # seconds ago and still holds inside its TTL.
        await in_thread(lambda: door.take_writer(FAMILY, session, TERMINAL_A))
        await stage.tui_turn(session, "from the term", TERMINAL_A)
        await settle(lambda: settled_count(stage, session) == 2, "the terminal's turn")

        # Back to the phone. The TUI's lease is idle, so rule 5 applies in
        # this direction too and the phone needs no flag either.
        again = await stage.owui_turn(chat, message_id(), "phone again")
        assert again.status_code == HTTP_OK, again.text
        await settle(lambda: settled_count(stage, session) == 3, "the phone's second turn")

        # §7.4: the terminal's renewal timer learns it lost the session. A
        # renew never takes anything back, or the two doors would trade the
        # lease every twenty seconds with nobody asking.
        with pytest.raises(AttendanceError) as renewed:
            await in_thread(
                lambda: door.take_writer(FAMILY, session, TERMINAL_A, Takeover.POLITE, Intent.RENEW)
            )
    finally:
        door.close()

    assert renewed.value.code == CODE_LEASE_TAKEN_OVER
    assert renewed.value.detail["holder"] == "owui"

    # One session, one journal, both doors' turns in the order they ran.
    assert prompts(stage, session) == ["from the phone", "from the term", "phone again"]
    assert stage.holders(session) == [
        ("owui", "granted"),
        ("tui", "taken_over"),
        ("owui", "taken_over"),
    ]


async def test_i4_a_running_turn_refuses_every_takeover(stage: Stage4) -> None:
    """Scenario 2. Contract 02 §7.3 rule 4: no door and no flag takes an
    ACTIVE session.

    Two pi writers on one session store cross-contaminate context, orphan a
    branch, and both report success with no error anywhere. Rule 4 is the
    only thing between that and a person with a phone in one hand.
    """
    chat = chat_id()
    session = session_of(chat)
    stage.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)

    running = asyncio.create_task(stage.owui_turn(chat, message_id(), "a long one"))
    door = stage.tui_client(TERMINAL_A)

    try:
        await settle(
            lambda: len(stage.lines_of(session, "turn_started")) == 1,
            "the phone's turn to start",
        )

        # Every door and every flag, against a turn that is still streaming.
        for takeover in (Takeover.POLITE, Takeover.FORCE):
            with pytest.raises(AttendanceError) as refused:
                await in_thread(
                    lambda flag=takeover: door.take_writer(FAMILY, session, TERMINAL_A, flag)
                )

            assert refused.value.code == CODE_SESSION_BUSY, takeover
            assert refused.value.detail["holder"] == "owui"
            assert refused.value.detail["turn"], "a busy refusal names the turn in flight"
    finally:
        answer = await running
        door.close()

    assert answer.status_code == HTTP_OK, answer.text
    assert stage.holders(session) == [("owui", "granted")], "no takeover reached the journal"


async def test_i4_a_terminal_session_gains_a_chat(stage: Stage4) -> None:
    """Scenario 3. Contract 02 §10.4: a session born in the terminal is
    copied into Open WebUI, created once and fed after that.

    This is the other half of invariant 3. The lease lets the terminal take
    a chat; this lets the phone see a session that never had one.
    """
    session = await tui_session(stage)

    await stage.tui_turn(session, "terminal one", TERMINAL_A)
    await settle(lambda: len(stage.owui.filings()) == 1, "the chat to be filed")

    # Step 1 and step 2 of §10.4's table, on the first settled turn.
    assert len(stage.owui.creates()) == 1
    assert stage.owui.filings()[0].body["folder_id"] == FIXTURE_FOLDER_ID

    await stage.tui_turn(session, "terminal two", TERMINAL_A)
    await settle(lambda: len(stage.owui.appends()) == 1, "the second turn to be appended")

    # Created ONCE. A second create would make the phone show two chats for
    # one session, which is invariant 3 broken in the visible direction.
    assert len(stage.owui.creates()) == 1
    assert len(stage.owui.filings()) == 1
    assert copied_prompts(stage) == ["terminal one", "terminal two"]
    assert answers_echo_prompts(stage)


async def test_i4_a_chat_session_is_never_copied_back(stage: Stage4) -> None:
    """Scenario 3, second half. A session born in Open WebUI already has a
    chat, and §10.4 copies nothing into it.

    A copy here would append every turn a second time, under new message
    ids, and the person would watch their own chat duplicate itself.
    """
    chat = chat_id()
    session = session_of(chat)

    answer = await stage.owui_turn(chat, message_id(), "from the phone")
    assert answer.status_code == HTTP_OK, answer.text
    await settle(lambda: settled_count(stage, session) == 1, "the phone's turn")

    # A turn run by the OTHER door on that same session is still a turn of a
    # session Open WebUI owns, so it is not copied either.
    await stage.tui_turn(session, "from the term", TERMINAL_A)
    await settle(lambda: settled_count(stage, session) == 2, "the terminal's turn")

    assert stage.owui.paths() == [], "a chat-born session must never be written back"


async def test_i4_a_refused_copy_never_fails_a_turn(stage: Stage4) -> None:
    """Scenario 3, third half. Contract 02 §10.4 rule 3: the host journal is
    the record, so a failed write is retried, never raised.

    An Open WebUI that is down must not stop a terminal working. The turns
    that piled up while it was down go out on the next write.
    """
    session = await tui_session(stage)
    stage.owui.mood = OwuiMood.REFUSE

    await stage.tui_turn(session, "while it is down", TERMINAL_A)
    await settle(lambda: len(stage.owui.creates()) == 1, "the refused create")

    # The turn settled anyway, and nothing about it says Open WebUI refused.
    assert settled_count(stage, session) == 1

    stage.owui.mood = OwuiMood.ANSWER
    await stage.tui_turn(session, "once it is back", TERMINAL_A)
    await settle(lambda: len(stage.owui.appends()) == 1, "the waiting turn to go out")

    # Both turns reached the chat, oldest first: rule 3 says the next
    # settled turn sends what is still waiting, not only itself.
    assert copied_prompts(stage) == ["while it is down", "once it is back"]
    assert answers_echo_prompts(stage)


async def test_i4_release_process_frees_the_terminal(stage: Stage4) -> None:
    """Scenario 4. Contract 02 §5.11: the terminal waits out no timer.

    The playpen holds a session's pi process open after a turn settles,
    and contract 03 §7.6 exits 8 rather than put a second writer on one
    store. Without §5.11 a person who moved from the phone to the terminal
    would wait `pi_idle_ttl_s` — 900 seconds for an attended family.
    """
    chat = chat_id()
    session = session_of(chat)

    answer = await stage.owui_turn(chat, message_id(), "from the phone")
    assert answer.status_code == HTTP_OK, answer.text
    await settle(lambda: settled_count(stage, session) == 1, "the phone's turn")
    assert len(stage.stack.pi_starts()) == 1, "the playpen holds the process open"

    door = stage.tui_client(TERMINAL_A)

    try:
        await in_thread(lambda: door.take_writer(FAMILY, session, TERMINAL_A))
        await in_thread(lambda: door.release_process(FAMILY, session, TERMINAL_A))

        # The next turn starts a NEW pi process, and the session is the same
        # session: invariant 1 keeps sessions, so §5.11 closes a process and
        # never deletes anything.
        await stage.tui_turn(session, "from the term", TERMINAL_A)
        await settle(lambda: settled_count(stage, session) == 2, "the terminal's turn")
        row = await in_thread(lambda: door.get_session(FAMILY, session))
    finally:
        door.close()

    assert len(stage.stack.pi_starts()) == 2
    assert row.turns_total == 2
    assert prompts(stage, session) == ["from the phone", "from the term"]


async def test_i4_release_process_waits_for_a_running_turn(stage: Stage4) -> None:
    """Scenario 4, second half. §5.11 rule 2 refuses while a turn is in
    flight.

    Closing pi mid-turn would abandon the answer inside the sandbox with
    nothing to report it. The door's own order stops the turn first.
    """
    chat = chat_id()
    session = session_of(chat)
    stage.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)

    running = asyncio.create_task(stage.owui_turn(chat, message_id(), "a long one"))
    door = stage.tui_client(TERMINAL_A)

    try:
        await settle(
            lambda: len(stage.lines_of(session, "turn_started")) == 1,
            "the phone's turn to start",
        )

        with pytest.raises(AttendanceError) as refused:
            await in_thread(lambda: door.release_process(FAMILY, session, TERMINAL_A))
    finally:
        answer = await running
        door.close()

    assert answer.status_code == HTTP_OK, answer.text
    assert refused.value.code == CODE_SESSION_BUSY
    assert refused.value.detail["turn"], "the refusal names the turn that blocks it"
    assert len(stage.stack.pi_starts()) == 1, "the refusal started nothing and killed nothing"


async def test_i4_a_second_terminal_needs_force(stage: Stage4) -> None:
    """Scenario 6. Contract 02 §7.3 rule 6, and only rule 6.

    §7.1's "two processes of one door share a lease" is the safe direction
    for a server door that serves many chats from one pool. It is the
    unsafe direction here: one TUI process is one human writer, and two pi
    writers on one session store cross-contaminate context.
    """
    session = await tui_session(stage)
    first = stage.tui_client(TERMINAL_A)
    second = stage.tui_client(TERMINAL_B)

    try:
        await in_thread(lambda: first.take_writer(FAMILY, session, TERMINAL_A))

        # Idle, and still refused: another TERMINAL's lease is the one case
        # rule 5 does not hand over.
        with pytest.raises(AttendanceError) as polite:
            await in_thread(lambda: second.take_writer(FAMILY, session, TERMINAL_B))

        assert polite.value.code == CODE_SESSION_BUSY
        assert not polite.value.detail.get("turn"), "an idle refusal names no turn"

        # `--force` is the sanctioned takeover, and the operator types it.
        await in_thread(lambda: second.take_writer(FAMILY, session, TERMINAL_B, Takeover.FORCE))

        # A RUNNING turn refuses even the forced one (rule 4), so the first
        # terminal cannot take the session back mid-answer.
        stage.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)
        turning = asyncio.create_task(stage.tui_turn(session, "a long one", TERMINAL_B))
        await settle(
            lambda: len(stage.lines_of(session, "turn_started")) == 1,
            "the second terminal's turn to start",
        )

        with pytest.raises(AttendanceError) as forced:
            await in_thread(lambda: first.take_writer(FAMILY, session, TERMINAL_A, Takeover.FORCE))
    finally:
        await turning
        first.close()
        second.close()

    assert forced.value.code == CODE_SESSION_BUSY
    assert forced.value.detail["turn"], "a busy refusal names the turn in flight"


async def test_i4_an_edit_becomes_a_pi_fork(stage: Stage4) -> None:
    """Scenario 5. Contract 02 §10.2 rule 3, through to pi's own `fork`.

    Editing a message in Open WebUI reattaches the new one to an OLDER
    parent. `owui_map` is the only thing that says where that lands in the
    pi transcript, and a parent that is not the current leaf has to become
    `branch: {fork_from}` on `start_turn` (contract 03 §4.1). Without it,
    an edited message would be appended to the end and the person would
    get an answer to a question they had just replaced.
    """
    chat = chat_id()
    session = session_of(chat)
    first = Pair()
    second = Pair()

    await owui_pair(stage, chat, first, "turn one")
    await settle(lambda: settled_count(stage, session) == 1, "the first turn")
    await owui_pair(stage, chat, second, "turn two", parent=first.assistant)
    await settle(lambda: settled_count(stage, session) == 2, "the second turn")

    # No fork yet: each turn attached to the current leaf, which is rule 2.
    assert stage.forks() == []

    # The edit. Its parent is the FIRST turn's assistant message, which is
    # no longer the leaf, so rule 3 forks at the user entry after it.
    await owui_pair(stage, chat, Pair(), "turn two again", parent=first.assistant)
    await settle(lambda: settled_count(stage, session) == 3, "the edited turn")

    forks = stage.forks()

    assert len(forks) == 1, "one edit is one fork"
    assert forks[0]["ok"], "pi found the entry on its active branch"
    assert forks[0]["entryId"], "the fork named an entry, not a null"

    # Nothing fell back: a `branch_fallback` line is how §10.2 rule 4 says
    # "this turn did not branch after all", and this one did.
    assert stage.lines_of(session, "branch_fallback") == []
    assert prompts(stage, session) == ["turn one", "turn two", "turn two again"]


async def test_i4_an_unmapped_parent_falls_back(stage: Stage4) -> None:
    """Scenario 5, second half. §10.2 rule 4: a parent `owui_map` never saw
    names no entry, so the turn runs as a plain prompt and says so.

    Refusing instead would make an Open WebUI quirk a chat outage, which
    invariant 1 forbids. Branching silently would put the answer on the
    wrong branch with nothing in the record.
    """
    chat = chat_id()
    session = session_of(chat)

    await owui_pair(stage, chat, Pair(), "turn one")
    await settle(lambda: settled_count(stage, session) == 1, "the first turn")

    # A parent id from another chat: well-formed, and in no map.
    await owui_pair(stage, chat, Pair(), "turn two", parent=message_id())
    await settle(lambda: settled_count(stage, session) == 2, "the second turn")

    fallbacks = stage.lines_of(session, "branch_fallback")

    assert stage.forks() == [], "an unmapped parent asks pi for nothing"
    assert len(fallbacks) == 1
    assert fallbacks[0]["body"]["reason"] == "unmapped_parent"
    assert prompts(stage, session) == ["turn one", "turn two"]


# ----------------------------------------------- scenario 7, the platform fence


@pytest.fixture
def fleet(stage: Stage4) -> Stage4:
    """`code` and `agent-control` published by the real reconciler."""
    for result in stage.apply_platform():
        assert result.ok, result.status.faults

    return stage


def test_i4_only_agent_control_mounts_the_platform(fleet: Stage4) -> None:
    """Scenario 7, the file half of invariant 10 (`docs/rework/spec.md` §4.4).

    This is the sandbox spec `caregiver` hands the driver, which is as close
    to `sbx create` as a Mac reaches. What only the host can prove is that
    the VM then holds exactly these mounts and nothing else.
    """
    code_mounts = fleet.mounts_of(CODE)
    control_mounts = fleet.mounts_of(AGENT_CONTROL)

    assert code_mounts, "the reconciler created a sandbox for code"
    assert control_mounts, "the reconciler created a sandbox for agent-control"

    reached = [path for path in code_mounts if path.startswith(PLATFORM_ROOT)]

    assert reached == [], f"code reached the platform root: {reached}"

    # The one family inside the fence mounts the root itself, and no path
    # under it: the three platform repos are what live there.
    inside = [path for path in control_mounts if path.startswith(PLATFORM_ROOT)]

    assert inside == [PLATFORM_ROOT]


def test_i4_a_platform_mount_on_code_is_refused(fleet: Stage4, tmp_path: Path) -> None:
    """Scenario 7, the refusal. Contract 01 §5.5: the allowlist is a
    constant in code, so the registry cannot widen it.

    Invariant 19: an invalid file is refused with a report, and the LAST
    GOOD definition keeps serving. A fence that took the family down
    instead would be an outage anyone with registry write could cause.
    """
    before = fleet.mounts_of(CODE)
    rewritten = fleet.rewrite_family(
        CODE,
        tmp_path / "widened-registry",
        files=[{"path": PLATFORM_ROOT, "mode": "rw"}],
    )

    result = fleet.apply_one(rewritten, CODE)

    assert not result.ok, "a platform mount on code must never apply"

    # The status document says `invalid` and points at the report, which is
    # what a reader of contract 05 §2.1 acts on.
    document = result.status.as_json()

    assert str(document["state"]) == "invalid"

    # The report names the path and the family that may hold it, because
    # the next step is a person editing that line of that file.
    report = Path(str(document["validation"]["report_path"])).read_text(encoding="utf-8")

    assert PLATFORM_ROOT in report
    assert AGENT_CONTROL in report

    # Nothing moved. The sandbox that was serving is still the one serving.
    assert fleet.mounts_of(CODE) == before


async def test_i4_the_pep_fences_the_platform_credential(
    fleet: Stage4, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario 7, the credential half of the platform fence
    (`docs/rework/spec.md` §4.4).

    Two mirrored MCP servers, one credential each. `code` is granted
    `github-code` and `agent-control` `github-platform`, and the PEP is
    what holds the two apart at call time. This fence and the mount fence
    have to agree, or `code` reaches the platform through the other door.
    """
    with serving_pep(fleet, monkeypatch, tmp_path) as pep_url:
        async with httpx.AsyncClient(base_url=pep_url, timeout=PEP_TIMEOUT_S) as client:
            denied = await call_pep(client, fleet.family_token(CODE), PLATFORM_TOOL)
            allowed = await call_pep(client, fleet.family_token(AGENT_CONTROL), PLATFORM_TOOL)
            own = await call_pep(client, fleet.family_token(CODE), CODE_TOOL)

    # `code` is refused on policy, by name, before anything runs.
    assert denied["ok"] is False
    assert denied["reason"] in POLICY_DENIALS, denied

    # `agent-control` gets past the decision. It then fails to reach a real
    # `github-mcp-server`, which no test may run, so what is asserted is
    # that its refusal is NOT a policy one.
    assert allowed.get("reason") not in POLICY_DENIALS, allowed

    # The fence is about the platform credential, not about GitHub: `code`
    # keeps its own server.
    assert own.get("reason") not in POLICY_DENIALS, own


# ------------------------------------------- packet I4 item 3, the write-back's cost


async def test_i4_a_hung_open_webui_stalls_nothing(stage: Stage4) -> None:
    """Contract 02 §10.4 rule 5: the copy's cost sits OFF the turn's path.

    One `attendance` process serves every session of every family on one
    event loop. A synchronous write from the turn-settle path puts an Open
    WebUI outage on that loop, and its own client waits 10 seconds, so one
    terminal session settling would freeze every chat on the host for as
    long as Open WebUI took to answer.

    Two measurements, because one alone is arguable: the loop's own worst
    gap, and a second session on the OTHER door finishing while the write
    is still out.
    """
    writing = await tui_session(stage, "the one that is copied")
    chat = chat_id()
    watched = session_of(chat)
    stage.owui.mood = OwuiMood.DAWDLE
    stage.owui.dawdle_s = DAWDLE_S

    beat = LoopBeat()
    beat.start()

    try:
        # This settles into a write the fake will not answer for DAWDLE_S.
        await stage.tui_turn(writing, "the copied one", TERMINAL_A)

        # A phone session, which §10.4 never copies. It must run and settle
        # while that write is still in the fake.
        started = asyncio.get_running_loop().time()
        answer = await stage.owui_turn(chat, message_id(), "the other one")
        took = asyncio.get_running_loop().time() - started
        await settle(lambda: settled_count(stage, watched) == 1, "the phone's turn")
    finally:
        await beat.stop()

    assert answer.status_code == HTTP_OK, answer.text
    assert took < STALL_LIMIT_S, f"the other session waited {took:.2f}s on a hung Open WebUI"
    assert beat.worst < STALL_LIMIT_S, f"the event loop stood still for {beat.worst:.2f}s"

    # The write is still out, which is the whole premise: this scenario
    # measured a session settling AROUND it, not after it.
    assert stage.owui.in_flight >= 1
