"""What `attendance`, the door and the playpen do with what `caregiver` publishes.

`integration/tests/test_i2_stage2.py` edits a family file and lets
`caregiver`'s own code publish the result, inside the test process. No
`caregiver` process runs in this topology, so a test plays `caregiver`: it
writes the status document and the config mount as contracts 01 and 05 give
them.

So these scenarios judge the readers. They do not prove that `caregiver`
writes these files. `test_proc_caregiver_stage2.py` does, with a `caregiver`
process.
"""

from __future__ import annotations

import httpx
from proc_chat import (
    MODELS_PATH,
    SESSIONS_PATH,
    await_settled,
    chat_id,
    run_stream,
    session_of,
)
from proc_owui import OwuiStack
from proc_tree import FAMILY, MODEL, SANDBOX, Validity, write_instructions, write_status

NEXT_CONFIG_REV = "reg-c2-proc"
NEW_INSTRUCTIONS = "Answer only in metric units.\n"

HTTP_OK = 200
HTTP_CONFLICT = 409


async def test_a_new_config_revision_replaces_pi_at_the_next_turn(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Contract 01 §6.1. `config_rev` moves, and the next turn gets a new pi.

    The held-open process is replaced at the NEXT turn, never between two
    turns. The instructions reach pi as a path on its command line
    (contract 03 §7.1), so the record of the pi stand-in shows the path.

    CONTRACT-QUESTION: contract 03 §6 rule 5 lists three reasons to reap a
    held process, and a new `config_rev` is not one of them. Contract 01 §6.1
    rule 4 promises only that the change reaches the next turn. Reading
    taken: what the playpen does and the old stage 2 suite holds, a new
    process at the next turn. A change costs the count of pi starts below.
    """
    instructions = owui.tree.mounts().config / "instructions.md"
    chat = chat_id()
    session = session_of(chat)
    first = await run_stream(door, chat, "one")
    assert first.error_chunks == []
    await await_settled(owui.tree, session)
    before = len(owui.pi_starts())

    write_instructions(owui.tree, text=NEW_INSTRUCTIONS)
    write_status(owui.tree, config_rev=NEXT_CONFIG_REV)

    second = await run_stream(door, chat, "two")
    assert second.error_chunks == []
    await await_settled(owui.tree, session, 2)

    starts = owui.pi_starts()
    assert len(starts) == before + 1
    assert str(instructions) in starts[-1].argv
    assert instructions.read_text(encoding="utf-8") == NEW_INSTRUCTIONS


async def test_an_unchanged_config_revision_keeps_the_process(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Contract 05 §2 rule 8. A new `written_at` alone replaces no process."""
    chat = chat_id()
    session = session_of(chat)
    await run_stream(door, chat, "one")
    await await_settled(owui.tree, session)

    write_status(owui.tree)

    second = await run_stream(door, chat, "two")
    assert second.error_chunks == []
    await await_settled(owui.tree, session, 2)
    assert len(owui.pi_starts()) == 1


async def test_an_invalid_family_file_keeps_the_family_serving(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Invariant 19 and contract 05 §3.1. A bad file is a report, never an outage."""
    chat = chat_id()
    session = session_of(chat)
    first = await run_stream(door, chat, "one")
    assert first.error_chunks == []
    await await_settled(owui.tree, session)

    write_status(owui.tree, validity=Validity.INVALID)

    models = await door.get(MODELS_PATH)
    second = await run_stream(door, chat, "two")

    assert [entry["id"] for entry in models.json()["data"]] == [MODEL]
    assert second.ends_with_done
    assert second.error_chunks == []
    await await_settled(owui.tree, session, 2)
    assert len(owui.sbx_calls()) == 1, "the family moved off the sandbox it served on"
    assert owui.sbx_calls()[0].value_after("--sandbox") == SANDBOX


async def test_a_family_that_was_never_valid_serves_nothing(
    owui: OwuiStack, door: httpx.AsyncClient, attendance_api: httpx.AsyncClient
) -> None:
    """Contract 05 §3.1 and contract 02 §5.1. Nothing was applied, so nothing serves."""
    write_status(owui.tree, validity=Validity.NEVER_VALID)
    session = session_of(chat_id())

    models = await door.get(MODELS_PATH)
    created = await attendance_api.post(SESSIONS_PATH, json={"family": FAMILY, "session": session})

    assert models.status_code == HTTP_OK
    # CONTRACT-QUESTION: contract 05 §3.1 and contract 02 §5.1 give only the
    # refusal `family_invalid` by `attendance`. No contract says what the
    # door lists. Reading taken: the door hides a family that was never
    # valid, because no turn of that family can run. A change costs this one
    # assertion.
    assert models.json()["data"] == []
    assert created.status_code == HTTP_CONFLICT
    assert created.json()["error"]["code"] == "family_invalid"
    assert not owui.tree.session_dir(session).exists()
    assert owui.pi_starts() == []
