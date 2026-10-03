"""Packet MD: the caller family's own cap on delegate calls in flight.

`max_inflight_delegations` used to be a constant in `caregiver` (contract 04
draft 5 §1.2), so no family file could raise it and the operator's stage 3 test of
three delegate calls at once could not pass. Contract 01 §3.6.1 makes it a
field of the family file, default 2, range 1 to 8, and this suite proves the
whole path:

    families/chat/family.yaml   max_inflight_delegations: 3
      │ caregiver apply
      ▼
    grants/chat.json            limits.max_inflight_delegations: 3
      │ the PEP re-reads per call (contract 04 §1.4)
      ▼
    three invoke_agent calls at once, and all three finish

Stage 3's harness is reused unchanged: `stage3.Stage3` publishes all three
families with the real manager, `serving_pep` puts the real PEP in front of
the real `attendance`, and `call_tool` drives the real bridge. Nothing here is a
second copy of that plumbing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import Stack, until
from stage3 import CHAT, VAULT_ORACLE, Stage3, ToolCall, bridge_bundle_missing, serving_pep

from caregiver import paths as caregiver_paths

INVOKE_AGENT = "invoke_agent"
CHAT_PATH = "/v1/chat/completions"
HTTP_OK = 200

#: Contract 04 §6.3's journal line for a finished turn. The door's stream ends
#: at pi's `agent_settled` and `attendance` writes this after, so a reader polls
#: for it rather than reading the journal the instant a stream ends.
TURN_SETTLED = "turn_settled"

#: What the scripted model asks a delegate. `fake-pi.mjs` slices a prompt at 24
#: characters, so a short distinctive string is what tells two concurrent
#: answers apart.
QUESTION = "inflight"

#: Contract 01 §3.6.1's default, and the number this packet raises it past.
DEFAULT_CAP = 2
RAISED_CAP = 3

#: A job turn that cannot finish at once: 200 deltas eight milliseconds apart
#: is at least 1.6 seconds, which is long enough for three calls to overlap.
SLOW_TURN_EVENTS = 200
SLOW_TURN_GAP_MS = 8

#: Long enough for three job sessions to appear and go on a loaded Mac. Four
#: other suites may be running beside this one.
SETTLE_TIMEOUT_S = 120.0


pytestmark = pytest.mark.skipif(
    bridge_bundle_missing(),
    reason="playpen/dist/pep-bridge.js is missing: run `pnpm install && pnpm build`",
)


@pytest.fixture
async def stage(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stage3]:
    """Three families published by the real manager, then a serving stack.

    The same set-up `test_i3_stage3.py` uses, for the same two reasons: the
    hand-made stage 1 `creds.json` would put `chat` on a second path, and
    stage 1's one sessions-root override names one root for every family.
    """
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()
    ready = Stage3(built)

    caregiver_paths.creds_path(built.state_root, CHAT).unlink(missing_ok=True)
    for result in ready.apply_all():
        assert result.ok, result.status.faults

    monkeypatch.delenv("SESSIOND_SANDBOX_SESSIONS_MOUNT", raising=False)
    for name, value in ready.sandbox_environ().items():
        monkeypatch.setenv(name, value)

    await built.serve()

    try:
        yield ready
    finally:
        await built.close()


@pytest.fixture
def delegating(stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Stage3]:
    """The same stack with the real PEP in front of it."""
    with serving_pep(stage, monkeypatch, tmp_path) as ready:
        yield ready


def ask(message: str) -> dict[str, object]:
    """One `invoke_agent` call's arguments (contract 04 §4.1)."""
    return {"family": VAULT_ORACLE, "message": message}


async def call_at_once(stage: Stage3, session: str, count: int) -> list[ToolCall]:
    """`count` delegate calls from one chat session, started together.

    Each carries its own message, so a finished answer names the call it
    belongs to and a test can tell which one was refused.
    """
    stage.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)

    return list(
        await asyncio.gather(
            *(
                stage.call_tool(INVOKE_AGENT, ask(f"{QUESTION}-{n}"), session=session)
                for n in range(count)
            )
        )
    )


def creates(stage: Stage3) -> int:
    """How many sandboxes the manager has made so far.

    A grant change replaces no sandbox (contract 01 §6), so this number is
    what a live raise must leave alone.
    """
    return stage.driver.ops().count("create")


def cap_raised_to(stage: Stage3, registry_root: Path, cap: int) -> None:
    """Publish `chat` with a different cap, through the real manager."""
    rewritten = stage.rewrite_family(CHAT, registry_root, max_inflight_delegations=cap)
    result = stage.apply_from(rewritten, CHAT)

    assert result.ok, result.status.faults


def granted_cap(stage: Stage3) -> int:
    """The number the PEP will read on its next call (contract 04 §1.2)."""
    path = stage.stack.state_root / "grants" / f"{CHAT}.json"
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    return int(body["limits"]["max_inflight_delegations"])


# --- the field decides how many run at once ----------------------------------


async def test_a_cap_of_three_runs_three_delegate_calls_at_once(
    stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator's stage 3 test, with contract 01 §3.6.1's field at 3.

    The thin family has no turn limit and one sandbox serves every job, so the
    only thing that ever stopped the third call was the caller's cap.
    """
    cap_raised_to(stage, tmp_path / "cap-three", RAISED_CAP)
    assert granted_cap(stage) == RAISED_CAP

    with serving_pep(stage, monkeypatch, tmp_path) as ready:
        calls = await call_at_once(ready, session_of(chat_id()), RAISED_CAP)

    assert [one.ok for one in calls] == [True] * RAISED_CAP
    for n, one in enumerate(calls):
        assert f"{QUESTION}-{n}" in one.text

    assert len(stage.job_cwds()) == RAISED_CAP
    assert stage.sessions_of(VAULT_ORACLE) == []


async def test_the_default_cap_still_refuses_the_third(delegating: Stage3) -> None:
    """No field, so the default of 2 applies and the third is refused
    `rate_limited` at the PEP, before `attendance` sees it (contract 04 §5
    row 7).
    """
    assert granted_cap(delegating) == DEFAULT_CAP

    calls = await call_at_once(delegating, session_of(chat_id()), DEFAULT_CAP + 1)

    refused = [one for one in calls if not one.ok]
    assert len(refused) == 1
    assert "rate_limited" in refused[0].error
    assert len(delegating.job_cwds()) == DEFAULT_CAP


# --- the raise lands live ----------------------------------------------------


async def test_raising_the_cap_live_keeps_the_sandbox_and_the_session(
    delegating: Stage3, tmp_path: Path
) -> None:
    """Contract 01 §3.6.1 rule 5 and §6: the cap rides in the grant file, so a
    raise applies on the next call with no sandbox replacement.

    The chat answers a turn first, so the session is real and on disk. After
    the raise it answers again on the same session: the journal keeps both
    turns, which is what "it never ends a session" means (invariant 9).
    """
    chat = chat_id()
    session = session_of(chat)
    await answer(delegating.stack, chat, "before the raise")
    await settled(delegating.stack, session, 1)
    sandboxes_before = creates(delegating)

    refused = [one for one in await call_at_once(delegating, session, RAISED_CAP) if not one.ok]
    assert len(refused) == 1

    # The raise, with the PEP serving and `attendance` holding the session.
    cap_raised_to(delegating, tmp_path / "raised-live", RAISED_CAP)
    assert granted_cap(delegating) == RAISED_CAP

    calls = await call_at_once(delegating, session, RAISED_CAP)

    assert [one.ok for one in calls] == [True] * RAISED_CAP
    assert creates(delegating) == sandboxes_before

    await answer(delegating.stack, chat, "after the raise")
    await settled(delegating.stack, session, 2)
    assert kinds(delegating.stack, session).count(TURN_SETTLED) == 2


# --- the door, for the session half of the claim ------------------------------


async def answer(stack: Stack, chat: str, text: str) -> None:
    """One whole turn through the Open WebUI door, read to the end."""
    stack.set_pi_env(events=2, delay_ms=1)
    client = stack.client

    assert client is not None

    async with client.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body(text, stream=True),
    ) as response:
        assert response.status_code == HTTP_OK, await response.aread()
        async for _ in response.aiter_text():
            continue


async def settled(stack: Stack, session: str, count: int) -> None:
    """The journal line lands after the client's `[DONE]`, so poll for it."""
    await until(
        lambda: kinds(stack, session).count(TURN_SETTLED) == count,
        f"{session} to reach {count} settled turns",
        SETTLE_TIMEOUT_S,
    )


def kinds(stack: Stack, session: str) -> list[str]:
    return [str(line.get("kind", "")) for line in stack.journal_lines(session)]
