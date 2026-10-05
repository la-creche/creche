"""Packet DJS: the pi shim starts the fake pi with `--mode=rpc` as one word.

A sandbox on the host is a microVM with a process table of its own. A test
has no microVM, so each playpen of the machine is in one process table.

    playpen of sandbox 2 ── `ready` ──► attendance ──► fault file ──► caregiver
          │
          └─ reads the process table: each process with `--mode` `rpc`

On Linux a playpen counts each process of that table whose arguments hold
`--mode` and `rpc` as two words. `ready` reports the count as the pi
processes that an earlier channel left (contract 03 §3 rule 4). `attendance`
then raises the fault `orphan_processes`, and `caregiver` publishes the
family as `degraded`.

With two words, the fake pi of one sandbox is such a process for the playpen
of a second sandbox. Each replace-class scenario of stage 2 then ends
`degraded` on Linux. On macOS a playpen can count nothing, and the same
scenario ends `in_sync`. With one word, no playpen counts a fake pi. The
fake pi reads only `--session-id`, so it runs the same.

This scenario reads the process table with `ps`, so it has one result on
Linux and on macOS.
"""

from __future__ import annotations

import subprocess

from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import Stack, fake_pi_script, until

CHAT_PATH = "/v1/chat/completions"
HTTP_OK = 200

#: The two words that the playpen sends (`playpen/src/pi-args.ts`), and the
#: one word that the fake pi runs with.
MODE_FLAG = "--mode"
MODE = "rpc"
ONE_WORD = "--mode=rpc"


def fake_pi_words(session: str) -> list[list[str]]:
    """The arguments of each fake pi process of one session, as `ps` prints
    them. The shim is not in the answer before it becomes the fake pi."""
    done = subprocess.run(
        ["ps", "-A", "-ww", "-o", "args="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    program = str(fake_pi_script())
    rows = [line.split() for line in done.stdout.splitlines()]

    return [row for row in rows if program in row and session in row]


async def test_djs_the_fake_pi_runs_with_one_mode_word(stack: Stack) -> None:
    """The playpen holds the pi process of a session open after a turn. Its
    arguments in the process table hold the mode as one word. The record of
    the shim keeps the two words that the playpen sent."""
    client = stack.client
    assert client is not None, "the stack fixture always serves before it yields"
    chat = chat_id()
    session = session_of(chat)

    response = await client.post(
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("hello", stream=False),
    )

    assert response.status_code == HTTP_OK, response.text

    await until(lambda: fake_pi_words(session) != [], "the fake pi of the session")
    [running] = fake_pi_words(session)

    assert ONE_WORD in running
    assert MODE_FLAG not in running
    assert MODE not in running

    [sent] = [line.split() for line in stack.pi_starts()]

    assert sent[sent.index(MODE_FLAG) + 1] == MODE
