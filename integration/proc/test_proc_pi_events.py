"""What the journal holds for a pi event that no strict JSON text can hold.

A text of pi can end in one half of a surrogate pair. pi writes that half as
an escape, and a line with such an escape is not strict JSON. The playpen
cuts the event before it writes the line: the type and the scalar fields
stay, and `truncated` and `original_bytes` mark the cut (contract 03 §8).
`attendance` then records the event that it got.

The scenario reads the journal, which is a file under the root. The pi
stand-in writes the event, through its tuning `raw_line`.
"""

from __future__ import annotations

import json

import httpx
from proc_chat import await_settled, chat_id, run_stream, session_of
from proc_owui import OwuiStack
from proc_standins import set_pi_env

#: The journal line kind of contract 02 §8.1 for one event of pi.
PI_EVENT = "pi_event"

#: The type of the event that the pi stand-in writes for this scenario.
EVENT_TYPE = "tool_execution_update"

#: One scalar field of that event, which holds no surrogate.
SCALAR_FIELD = "attempt"
SCALAR_VALUE = 7

#: The text field of that event. It ends in the escape of one high half.
TEXT_FIELD = "text"
LONE_HALF_ESCAPE = "\\ud83d"

#: The event as pi writes it: one line of JSON.
PI_LINE = (
    f'{{"type":"{EVENT_TYPE}","{SCALAR_FIELD}":{SCALAR_VALUE},'
    f'"{TEXT_FIELD}":"half of a pair {LONE_HALF_ESCAPE}"}}'
)


async def test_a_pi_event_with_a_lone_surrogate_keeps_its_scalar_fields(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Contract 03 §8. The cut event keeps its type and its scalar fields.

    CONTRACT-QUESTION: contract 03 §8 cuts an event by its size, and it names
    no event with a lone surrogate. Reading taken: the playpen cuts such an
    event as it cuts one over the size limit, so the scalar fields stay. A
    change costs the assertions on the body below.
    """
    assert json.loads(PI_LINE)[TEXT_FIELD].endswith("\ud83d")
    set_pi_env(owui.tree, raw_line=PI_LINE)
    chat = chat_id()
    session = session_of(chat)

    frames = await run_stream(door, chat, "hello")

    assert frames.ends_with_done
    assert frames.error_chunks == []
    await await_settled(owui.tree, session)

    bodies = [
        line["body"]
        for line in owui.tree.journal_lines(session)
        if line["kind"] == PI_EVENT and line["body"].get("type") == EVENT_TYPE
    ]
    assert len(bodies) == 1
    (body,) = bodies
    assert body["truncated"] is True
    assert body[SCALAR_FIELD] == SCALAR_VALUE
    assert TEXT_FIELD not in body
    assert body["original_bytes"] == len(PI_LINE.encode("utf-8"))
