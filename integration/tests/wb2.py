"""What a terminal leaves behind, for packet WB2's scenario.

`stage4.py` says it plainly (note 1): a terminal's own turns never reach the
journal, because the TUI door hands its tty to pi INSIDE the sandbox
(contract 03 §7.6) and pi writes the session store directly. Stage 4's
scenarios therefore run a TUI-door turn through `sessiond`, which is the one
TUI turn a journal can hold.

Packet WB2 needs the other one. So this module writes the entries a
terminal's pi process would have written, into the store `fake-pi.mjs` keeps
under `PI_CODING_AGENT_DIR`, and then ends the lease. Everything after that
is the real path: `sessiond` sends contract 03 §4.8's `get_entries`, the real
playpen starts a real fake-pi process on that store, and the entries come
back over the channel.

```
  this module        the real path under test
  -----------        ------------------------
  write e3,e4  --->  release the tui lease
                     sessiond --get_entries--> playpen --> fake pi
                     terminal_exchange line, owui_map rows, one append
```

Nothing under test is faked here. The store file is the stand-in, and it
stands in for a pi process this harness has no interactive mode to run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stack import FAMILY
from stage4 import Stage4, in_thread

#: `playpen/test/fake-pi.mjs` keeps its entries here, beside the session
#: store pi itself would write. One file per session id.
ENTRY_STORE = "fake-pi-entries-{session}.json"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


def store_of(stage: Stage4, session: str) -> Path:
    """The fake pi's entry store for one session, on the sessions mount."""
    return stage.stack.session_dir(session) / "pi" / ENTRY_STORE.format(session=session)


def entries_of(stage: Stage4, session: str) -> list[dict[str, Any]]:
    path = store_of(stage, session)

    if not path.exists():
        return []

    parsed: object = json.loads(path.read_text(encoding="utf-8"))

    return parsed if isinstance(parsed, list) else []


def terminal_wrote(stage: Stage4, session: str, prompt: str, answer: str) -> tuple[str, str]:
    """Append one exchange to the pi store, as a terminal's pi would.

    Returns the two entry ids. `fake-pi.mjs` names them `e<n>` by position,
    so this keeps counting where the store left off.
    """
    found = entries_of(stage, session)
    user = f"e{len(found) + 1}"
    assistant = f"e{len(found) + 2}"
    found.append({"id": user, "message": {"role": ROLE_USER, "content": prompt}})
    found.append({"id": assistant, "message": {"role": ROLE_ASSISTANT, "content": answer}})

    path = store_of(stage, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(found), encoding="utf-8")

    return user, assistant


async def terminal_visit(
    stage: Stage4, session: str, instance: str, prompt: str, answer: str
) -> tuple[str, str]:
    """One whole terminal visit, in the order `agent-tui chat` runs it.

    `TuiDoor.run` steps 4, 5 and the hand-over (`door-tui/src/app.py`):

    1. Take the writer lease (contract 02 §7.3 rule 5).
    2. Release the session's pi process (§5.11). Without this the terminal
       meets contract 03 §7.6's fence, exit 8, and a resident rpc process
       would still hold the store's older state.
    3. pi runs on the tty and writes the exchange. That step is this
       module's stand-in, and it is the only one.
    4. Give the lease back (§5.10), which is where §10.5 reads.

    The door's client is synchronous and `sessiond` runs on this test's own
    loop, so every call goes to a worker thread. A direct call would block
    the service it is calling (`stage4.in_thread`).
    """
    door = stage.tui_client(instance)

    def enter() -> None:
        door.take_writer(FAMILY, session, instance)
        door.release_process(FAMILY, session, instance)

    try:
        await in_thread(enter)
        ids = terminal_wrote(stage, session, prompt, answer)
        await in_thread(lambda: door.release_writer(FAMILY, session, instance))
    finally:
        door.close()

    return ids


def copied(stage: Stage4) -> list[tuple[str, str, bool]]:
    """Every exchange that REACHED the chat: prompt, answer, and terminal.

    A refused write carried content and did not arrive, so it is not here.
    The third field is §10.5 rule 4's marker, read off the user message.
    """
    found: list[tuple[str, str, bool]] = []

    for call in stage.owui.answered():
        by_role: dict[str, dict[str, Any]] = {}

        for message in call.messages().values():
            role = message.get("role")

            if isinstance(role, str):
                by_role[role] = message

        user = by_role.get(ROLE_USER)
        assistant = by_role.get(ROLE_ASSISTANT)

        if user is None or assistant is None:
            continue

        found.append(
            (str(user.get("content", "")), str(assistant.get("content", "")), user.get("terminal"))
        )

    return found
