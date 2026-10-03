"""The stage 1 gate, rehearsed on a Mac.

Thirteen scenarios, one per behaviour the operator will check on the host. Each drives
the real door, the real `attendance` and the real playpen in one process.
Read `integration/README.md` for what each one proves and how to read a
failure.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from typing import Any

import httpx
import sse_read
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import FAMILY, MODEL, SANDBOX, Stack, until

CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
SESSIONS_PATH = "/v1/sessions"

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409

# Contract 02 §8.1's journal line kinds, as a test reads them back.
SESSION_CREATED = "session_created"
TURN_STARTED = "turn_started"
TURN_SETTLED = "turn_settled"
TURN_FAILED = "turn_failed"

# `session.ts` fences a persona before it reaches pi. The tag is the stable
# part of that fence and it lands inside the 24 characters the fake pi echoes.
PERSONA_TAG = "<folder-prompt"

SETTLE_TIMEOUT_S = 30.0


async def test_models_lists_the_chat_family(stack: Stack) -> None:
    """Scenario 1. The picker offers `agent:chat` and nothing else."""
    response = await _client(stack).get(MODELS_PATH)

    assert response.status_code == HTTP_OK
    ids = [entry["id"] for entry in response.json()["data"]]
    assert ids == [MODEL]


async def test_one_streamed_turn_end_to_end(stack: Stack) -> None:
    """Scenario 2. SSE chunks arrive in order and end with `[DONE]`."""
    frames = await _run_stream(stack, chat_id(), "hello")

    assert frames.ends_with_done
    assert frames.text != ""
    assert frames.finish_reasons == ["stop"]
    assert frames.error_chunks == []
    # No system message, so nothing fenced anything into the prompt.
    assert PERSONA_TAG not in frames.text


async def test_five_chats_never_cross(stack: Stack) -> None:
    """Scenario 3. Five chats at once, each answered in its own stream."""
    chats = [chat_id() for _ in range(5)]
    markers = [f"marker{index}" for index in range(5)]

    results = await asyncio.gather(
        *(_run_stream(stack, chat, marker) for chat, marker in zip(chats, markers, strict=True))
    )

    for index, frames in enumerate(results):
        assert frames.ends_with_done, f"chat {index} never finished"
        assert frames.finish_reasons == ["stop"]
        assert markers[index] in frames.text

        # The fake pi echoes the prompt, so another chat's marker in this
        # stream is an event that crossed sessions on the one channel.
        others = [other for position, other in enumerate(markers) if position != index]
        for other in others:
            assert other not in frames.text, f"{other} leaked into chat {index}"

        assert len(frames.ids()) == 1

    sessions = {session_of(chat) for chat in chats}
    assert len(sessions) == len(chats)
    for session in sessions:
        await _await_settled(stack, session)
        assert _kinds(stack, session).count(TURN_SETTLED) == 1


async def test_a_dropped_client_still_settles(stack: Stack) -> None:
    """Scenario 4. The turn belongs to the host, not to the connection."""
    stack.set_pi_env(events=60, delay_ms=30)
    chat = chat_id()
    session = session_of(chat)

    await _open_and_abandon(stack, chat, "first")

    # Invariant 4: nothing about a client ending its read may end the turn.
    await _await_settled(stack, session)

    stack.set_pi_env(events=3, delay_ms=1)
    frames = await _run_stream(stack, chat, "second")

    assert frames.ends_with_done
    kinds = _kinds(stack, session)
    assert kinds.count(TURN_STARTED) == 2
    assert kinds.count(TURN_SETTLED) == 2


async def test_the_same_message_id_runs_one_turn(stack: Stack) -> None:
    """Scenario 5. `idempotency_key` is the Open WebUI message id."""
    chat = chat_id()
    message = message_id()
    session = session_of(chat)

    first = await _run_stream(stack, chat, "only once", message=message)
    second = await _run_stream(stack, chat, "only once", message=message)

    assert first.ends_with_done
    assert second.ends_with_done

    kinds = _kinds(stack, session)
    assert kinds.count(TURN_STARTED) == 1
    assert len(stack.pi_starts()) == 1


async def test_a_second_turn_gets_the_clear_409(stack: Stack) -> None:
    """Scenario 6. One writer per session, and a refusal rather than a fork."""
    stack.set_pi_env(events=60, delay_ms=30)
    chat = chat_id()
    session = session_of(chat)
    client = _client(stack)
    headers = owui_headers(chat, message_id())

    async with client.stream(
        "POST", CHAT_PATH, headers=headers, json=chat_body("first", stream=True)
    ) as response:
        assert response.status_code == HTTP_OK
        await _first_chunk(response.aiter_text())

        second = await client.post(
            CHAT_PATH,
            headers=owui_headers(chat, message_id()),
            json=chat_body("second", stream=False),
        )

    assert second.status_code == HTTP_CONFLICT
    assert second.json()["error"]["code"] == "session_busy"

    await _await_settled(stack, session)
    assert _kinds(stack, session).count(TURN_STARTED) == 1


async def test_a_second_message_reuses_the_process(stack: Stack) -> None:
    """Scenario 7. The held-open pi process is what makes chat feel instant."""
    chat = chat_id()

    first = await _run_stream(stack, chat, "one")
    second = await _run_stream(stack, chat, "two")

    assert first.ends_with_done
    assert second.ends_with_done
    assert len(stack.pi_starts()) == 1, stack.pi_starts()


async def test_the_folder_persona_reaches_pi(stack: Stack) -> None:
    """Scenario 8. A system message arrives as fenced persona text."""
    frames = await _run_stream(stack, chat_id(), "hello", system="Answer only in haiku.")

    assert frames.ends_with_done
    # The fake pi echoes the head of the prompt message it was handed, so the
    # fence proves the persona reached the process and did so wrapped.
    assert PERSONA_TAG in frames.text


async def test_a_killed_playpen_is_visible(stack: Stack) -> None:
    """Scenario 9. The stream says so, the session lives, the next turn works."""
    stack.set_pi_env(events=60, delay_ms=30)
    chat = chat_id()
    session = session_of(chat)
    client = _client(stack)

    async with client.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("die", stream=True),
    ) as response:
        assert response.status_code == HTTP_OK
        chunks = response.aiter_text()
        opened = await _first_chunk(chunks)
        _kill_playpens(stack)
        body = opened + await _read_rest(chunks)

    frames = sse_read.parse(body)
    assert frames.ends_with_done
    assert frames.error_chunks != [], "a broken turn must never look like an answer"

    kinds = _kinds(stack, session)
    assert TURN_FAILED in kinds
    assert (stack.session_dir(session) / "session.json").exists()

    # The killed playpen could not remove its own lock, and nothing here
    # removes it by hand. `attendance` proves the writer is gone by watching the
    # beat counter stand still for `lock_stale_s` (M4, contract 03 §11.4).
    assert stack.playpen_lock().exists(), "a killed playpen leaves its lock"
    stack.set_pi_env(events=3, delay_ms=1)

    again = await _run_stream(stack, chat, "after the kill")
    assert again.ends_with_done
    assert again.error_chunks == []
    assert stack.playpen_lock().exists(), "the new playpen took the lock"

    await _await_settled(stack, session)
    assert _kinds(stack, session).count(TURN_SETTLED) == 1


async def test_a_new_session_warms_its_pi_process(stack: Stack) -> None:
    """Scenario 11. `open_session` pays pi's cold start before any prompt."""
    chat = chat_id()
    session = session_of(chat)

    # Contract 02 §5.1's create-or-find, which every door calls first. The
    # door makes this call and runs a turn in the same request, so this is the
    # only place the two can be told apart.
    created = await _attendance(stack).post(
        SESSIONS_PATH, json={"family": FAMILY, "session": session}
    )
    assert created.status_code == HTTP_CREATED

    # Contract 03 §4.7 rule 8: one pi process, started before any prompt
    # exists. Probe 0a measured what that saves: 1195 ms against 4 ms.
    await until(lambda: len(stack.pi_starts()) == 1, "the pre-started pi process")
    assert session in stack.pi_starts()[0]
    assert _kinds(stack, session) == [SESSION_CREATED]

    frames = await _run_stream(stack, chat, "hello")

    assert frames.ends_with_done
    assert frames.error_chunks == []
    await _await_settled(stack, session)

    # The turn used the warm process. A second start would mean it replaced
    # what the pre-start opened, which is worse than never pre-starting.
    assert len(stack.pi_starts()) == 1, stack.pi_starts()


async def test_an_empty_chat_id_is_refused(stack: Stack) -> None:
    """Scenario 10. An absent header arrives present but empty, and must fail."""
    response = await _client(stack).post(
        CHAT_PATH,
        headers=owui_headers("", message_id()),
        json=chat_body("hello", stream=True),
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["error"]["code"] == "missing_chat_id"

    # Nothing may fall back to a shared session: every chat would land in one.
    assert list(stack.mounts.sessions.iterdir()) == []
    assert stack.pi_starts() == []


async def test_the_channel_command_carries_the_env_file_and_the_id(stack: Stack) -> None:
    """Scenario 12. The playpen is reached the way the host reaches it.

    Contract 03 §7.1. `sbx exec` forwards no host environment, so the three
    mount paths arrive only through `--env-file`, and the playpen exits 2
    without `--sandbox`. Nothing in this harness puts those paths in the
    child's environment any other way, so a turn that settles is itself the
    proof that the file travelled. The two assertions on argv are what fails
    loudly when a flag is dropped, instead of leaving a `channel_lost` to
    explain.
    """
    frames = await _run_stream(stack, chat_id(), "hello")

    assert frames.ends_with_done
    assert frames.error_chunks == []

    argv = stack.channel_argv()
    assert len(argv) == 1, argv

    command = argv[0]
    assert command[command.index("--env-file") + 1] == str(stack.playpen_env)
    assert command[command.index("--sandbox") + 1] == SANDBOX


async def test_the_playpen_reads_the_mounts_it_was_given(stack: Stack) -> None:
    """Scenario 13. The paths in the env file are the ones it used.

    The lock file lands in the control directory `supervisor.env` names, and
    the turn file under it (contract 03 §11.1, §7.4). Both are host paths,
    because a mount's in-VM path IS its host path.
    """
    chat = chat_id()
    session = session_of(chat)
    await _run_stream(stack, chat, "hello")
    await _await_settled(stack, session)

    turn_file = stack.mounts.control / "sessions" / session / "turn.json"

    assert turn_file.is_file(), sorted(stack.mounts.control.rglob("*"))


# --------------------------------------------------------------------- helpers


def _client(stack: Stack) -> httpx.AsyncClient:
    client = stack.client
    assert client is not None, "the stack fixture always serves before it yields"

    return client


def _attendance(stack: Stack) -> httpx.AsyncClient:
    """A door's own client to `attendance`, for the call the door makes first."""
    client = stack.door_to_attendance
    assert client is not None, "the stack fixture always serves before it yields"

    return client


async def _run_stream(
    stack: Stack,
    chat: str,
    text: str,
    *,
    message: str | None = None,
    system: str | None = None,
) -> sse_read.Frames:
    """One streamed turn, read to its end."""
    headers = owui_headers(chat, message if message is not None else message_id())

    async with _client(stack).stream(
        "POST", CHAT_PATH, headers=headers, json=chat_body(text, stream=True, system=system)
    ) as response:
        assert response.status_code == HTTP_OK, await response.aread()
        body = await _read_rest(response.aiter_text())

    return sse_read.parse(body)


async def _open_and_abandon(stack: Stack, chat: str, text: str) -> None:
    """Open a streamed turn, read one chunk, then drop the connection."""
    async with _client(stack).stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body(text, stream=True),
    ) as response:
        assert response.status_code == HTTP_OK
        await _first_chunk(response.aiter_text())


async def _first_chunk(chunks: AsyncIterator[str]) -> str:
    """Pull one frame, which proves the turn is running on the host.

    One response is read through ONE iterator. httpx refuses a second one on
    a consumed stream, so a caller that has to act mid-turn keeps this
    iterator and hands the same one to `_read_rest`.
    """
    async for chunk in chunks:
        if chunk.strip():
            return chunk

    raise AssertionError("the stream closed before its first frame")


async def _read_rest(chunks: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in chunks])


async def _await_settled(stack: Stack, session: str) -> None:
    """Wait for the host journal, which lands AFTER the client's `[DONE]`.

    The door closes its stream on pi's own `agent_settled` event.
    `attendance` writes the `turn_settled` LINE when the playpen's
    `turn_settled` message arrives, which is later. A test that reads the
    journal the instant a stream ends is reading too early, and so is any
    other reader.
    """
    await until(
        lambda: TURN_SETTLED in _kinds(stack, session),
        f"{session} to reach {TURN_SETTLED}",
        SETTLE_TIMEOUT_S,
    )


def _kinds(stack: Stack, session: str) -> list[str]:
    return [_text(line, "kind") for line in stack.journal_lines(session)]


def _text(line: dict[str, Any], field: str) -> str:
    value = line.get(field)

    return value if isinstance(value, str) else ""


def _kill_playpens(stack: Stack) -> None:
    pids = stack.channel_pids()
    assert pids, "no playpen process was open to kill"

    for pid in pids:
        os.kill(pid, signal.SIGKILL)
