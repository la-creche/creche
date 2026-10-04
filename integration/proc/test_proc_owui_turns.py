"""The thirteen stage 1 scenarios, with each service in its own process.

`integration/tests/test_stage1_gate.py` holds the same thirteen with both
services inside the test process. Here the door and `attendance` start from
their real entry points, and every assertion reads something that crossed a
process boundary: an HTTP status, an HTTP body, the SSE stream, a journal
file, a file in a mount, or what a stand-in program recorded.

The numbers are the numbers of the old suite, so one scenario has one name in
both.
"""

from __future__ import annotations

import asyncio

import httpx
import proc_sse
from proc_chat import (
    CHAT_PATH,
    MODELS_PATH,
    SESSION_CREATED,
    SESSIONS_PATH,
    TURN_FAILED,
    TURN_SETTLED,
    TURN_STARTED,
    await_settled,
    chat_body,
    chat_id,
    first_chunk,
    message_id,
    owui_headers,
    read_rest,
    run_stream,
    session_of,
    until,
)
from proc_owui import OwuiStack
from proc_standins import set_pi_env
from proc_tree import FAMILY, MODEL, SANDBOX

# The playpen fences a persona before it reaches pi. The tag is the stable
# part of that fence, and it is inside the 24 characters the fake pi echoes.
PERSONA_TAG = "<folder-prompt"

#: A turn long enough to act inside: 60 deltas, 30 ms apart.
LONG_TURN = {"events": 60, "delay_ms": 30}
SHORT_TURN = {"events": 3, "delay_ms": 1}


async def test_models_lists_the_chat_family(door: httpx.AsyncClient) -> None:
    """Scenario 1. The picker offers `agent:chat` and nothing else."""
    response = await door.get(MODELS_PATH)

    assert response.status_code == httpx.codes.OK
    assert [entry["id"] for entry in response.json()["data"]] == [MODEL]


async def test_one_streamed_turn_end_to_end(door: httpx.AsyncClient) -> None:
    """Scenario 2. SSE chunks arrive in order and end with `[DONE]`."""
    frames = await run_stream(door, chat_id(), "hello")

    assert frames.ends_with_done
    assert frames.text != ""
    assert frames.finish_reasons == ["stop"]
    assert frames.error_chunks == []
    # No system message, so nothing put a fence into the prompt.
    assert PERSONA_TAG not in frames.text


async def test_five_chats_never_cross(owui: OwuiStack, door: httpx.AsyncClient) -> None:
    """Scenario 3. Five chats at once, each answered in its own stream."""
    chats = [chat_id() for _ in range(5)]
    markers = [f"marker{index}" for index in range(5)]

    results = await asyncio.gather(
        *(run_stream(door, chat, marker) for chat, marker in zip(chats, markers, strict=True))
    )

    for index, frames in enumerate(results):
        assert frames.ends_with_done, f"chat {index} never finished"
        assert frames.finish_reasons == ["stop"]
        assert markers[index] in frames.text

        # The fake pi echoes the prompt, so the marker of another chat in
        # this stream is an event that crossed sessions on the one channel.
        for other in markers[:index] + markers[index + 1 :]:
            assert other not in frames.text, f"{other} leaked into chat {index}"

        assert len(frames.ids()) == 1

    for chat in chats:
        session = session_of(chat)
        await await_settled(owui.tree, session)
        assert owui.tree.journal_kinds(session).count(TURN_SETTLED) == 1


async def test_a_dropped_client_still_settles(owui: OwuiStack, door: httpx.AsyncClient) -> None:
    """Scenario 4. The turn belongs to the host, not to the connection."""
    set_pi_env(owui.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)

    async with door.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("first", stream=True),
    ) as response:
        assert response.status_code == httpx.codes.OK
        await first_chunk(response.aiter_text())

    # Invariant 4: a client that ends its read does not end the turn.
    await await_settled(owui.tree, session)

    set_pi_env(owui.tree, **SHORT_TURN)
    frames = await run_stream(door, chat, "second")

    assert frames.ends_with_done
    await await_settled(owui.tree, session, 2)
    kinds = owui.tree.journal_kinds(session)
    assert kinds.count(TURN_STARTED) == 2
    assert kinds.count(TURN_SETTLED) == 2


async def test_the_same_message_id_runs_one_turn(owui: OwuiStack, door: httpx.AsyncClient) -> None:
    """Scenario 5. `idempotency_key` is the Open WebUI message id."""
    chat = chat_id()
    message = message_id()

    first = await run_stream(door, chat, "only once", message=message)
    second = await run_stream(door, chat, "only once", message=message)

    assert first.ends_with_done
    assert second.ends_with_done
    assert owui.tree.journal_kinds(session_of(chat)).count(TURN_STARTED) == 1
    assert len(owui.pi_starts()) == 1


async def test_a_second_turn_gets_the_clear_409(owui: OwuiStack, door: httpx.AsyncClient) -> None:
    """Scenario 6. One writer per session, and a refusal rather than a fork."""
    set_pi_env(owui.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)

    async with door.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("first", stream=True),
    ) as response:
        assert response.status_code == httpx.codes.OK
        await first_chunk(response.aiter_text())

        second = await door.post(
            CHAT_PATH,
            headers=owui_headers(chat, message_id()),
            json=chat_body("second", stream=False),
        )

    assert second.status_code == httpx.codes.CONFLICT
    assert second.json()["error"]["code"] == "session_busy"

    await await_settled(owui.tree, session)
    assert owui.tree.journal_kinds(session).count(TURN_STARTED) == 1


async def test_a_second_message_reuses_the_process(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Scenario 7. The held-open pi process is what makes chat feel instant."""
    chat = chat_id()

    first = await run_stream(door, chat, "one")
    second = await run_stream(door, chat, "two")

    assert first.ends_with_done
    assert second.ends_with_done
    assert len(owui.pi_starts()) == 1, owui.pi_starts()


async def test_the_folder_persona_reaches_pi(door: httpx.AsyncClient) -> None:
    """Scenario 8. A system message arrives as fenced persona text."""
    frames = await run_stream(door, chat_id(), "hello", system="Answer only in haiku.")

    assert frames.ends_with_done
    # The fake pi echoes the head of the prompt it was handed, so the fence
    # proves that the persona reached the process, and reached it wrapped.
    assert PERSONA_TAG in frames.text


async def test_a_killed_playpen_is_visible(owui: OwuiStack, door: httpx.AsyncClient) -> None:
    """Scenario 9. The stream says so, the session lives, the next turn works."""
    set_pi_env(owui.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)

    async with door.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("die", stream=True),
    ) as response:
        assert response.status_code == httpx.codes.OK
        chunks = response.aiter_text()
        opened = await first_chunk(chunks)
        assert owui.kill_playpens(), "no playpen process was open to kill"
        body = opened + await read_rest(chunks)

    frames = proc_sse.parse(body)
    assert frames.ends_with_done
    assert frames.error_chunks != [], "a broken turn must never look like an answer"

    assert TURN_FAILED in owui.tree.journal_kinds(session)
    assert (owui.tree.session_dir(session) / "session.json").exists()

    # The killed playpen could not remove its own lock, and nothing here
    # removes it by hand. `attendance` proves that the writer is gone: it
    # watches the beat counter stand still (contract 03 §11.4 rule 4).
    assert owui.tree.playpen_lock().exists(), "a killed playpen leaves its lock"
    set_pi_env(owui.tree, **SHORT_TURN)

    again = await run_stream(door, chat, "after the kill")
    assert again.ends_with_done
    assert again.error_chunks == []
    assert owui.tree.playpen_lock().exists(), "the new playpen took the lock"

    await await_settled(owui.tree, session)
    assert owui.tree.journal_kinds(session).count(TURN_SETTLED) == 1
    assert len(owui.sbx_calls()) == 2, "the second turn ran on a second playpen"


async def test_an_empty_chat_id_is_refused(owui: OwuiStack, door: httpx.AsyncClient) -> None:
    """Scenario 10. An absent header arrives present but empty, and must fail."""
    response = await door.post(
        CHAT_PATH,
        headers=owui_headers("", message_id()),
        json=chat_body("hello", stream=True),
    )

    assert response.status_code == httpx.codes.BAD_REQUEST
    assert response.json()["error"]["code"] == "missing_chat_id"

    # Nothing may fall back to a shared session: every chat would be in it.
    assert list(owui.tree.mounts().sessions.iterdir()) == []
    assert owui.pi_starts() == []


async def test_a_new_session_warms_its_pi_process(
    owui: OwuiStack, door: httpx.AsyncClient, attendance_api: httpx.AsyncClient
) -> None:
    """Scenario 11. `open_session` pays the cold start of pi before any prompt."""
    chat = chat_id()
    session = session_of(chat)

    # The create-or-find of contract 02 §5.1, which every door calls first.
    # The door makes this call and runs a turn in one request, so a direct
    # call is the only way to see what happens between the two.
    created = await attendance_api.post(SESSIONS_PATH, json={"family": FAMILY, "session": session})
    assert created.status_code == httpx.codes.CREATED

    # Contract 03 §4.7 rule 8: one pi process, started before any prompt.
    await until(lambda: len(owui.pi_starts()) == 1, "the pi process of the new session")
    assert session in " ".join(owui.pi_starts()[0].argv)
    assert owui.tree.journal_kinds(session) == [SESSION_CREATED]

    frames = await run_stream(door, chat, "hello")

    assert frames.ends_with_done
    assert frames.error_chunks == []
    await await_settled(owui.tree, session)

    # The turn used the warm process. A second start would mean that the
    # turn replaced the process the create opened.
    assert len(owui.pi_starts()) == 1, owui.pi_starts()


async def test_the_channel_command_carries_the_env_file_and_the_id(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Scenario 12. The playpen is reached the way the host reaches it.

    Contract 03 §7.1. `sbx exec` forwards no host environment, so the three
    mount paths arrive only through `--env-file`, and the playpen exits 2
    without `--sandbox`. The `sbx` stand-in recorded the command line that
    `attendance` ran, and that record is what this scenario reads.
    """
    frames = await run_stream(door, chat_id(), "hello")

    assert frames.ends_with_done
    assert frames.error_chunks == []

    calls = owui.sbx_calls()
    assert len(calls) == 1, calls

    call = calls[0]
    assert call.argv[0] == "exec"
    assert call.value_after("--env-file") == str(owui.tree.playpen_env())
    assert call.argv[call.argv.index("--") - 1] == SANDBOX
    assert call.value_after("--sandbox") == SANDBOX


async def test_the_playpen_reads_the_mounts_it_was_given(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Scenario 13. The paths in the env file are the ones the playpen used.

    The lock file is in the control directory the env file names, and the
    turn file is under it (contract 03 §11.1, §7.4).
    """
    chat = chat_id()
    session = session_of(chat)
    await run_stream(door, chat, "hello")
    await await_settled(owui.tree, session)

    control = owui.tree.mounts().control

    assert owui.tree.playpen_lock().is_file()
    assert (control / "sessions" / session / "turn.json").is_file(), sorted(control.rglob("*"))
