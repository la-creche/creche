"""A session's journal, folded into something a person reads (contract 02 §8).

The session page shows a transcript, and `attendance` has no transcript to
give: `view-ro` may replay the event stream and nothing more. So this
module turns journal lines into entries. It is pure, so a test hands it
lines and reads entries.

Four rules come straight from the contract and are not style choices.

1. **`heartbeat` never enters the sequence** (§8.1). It carries
   `journal_seq: null` and is not on disk. A reader that let it advance a
   cursor would replay from the wrong place.
2. **"Settled" is keyed on the `turn_settled` line, never on the wrapped
   `agent_settled` event** (§8.2). Two lines report a turn's end, in that
   order, so a page read in between shows the turn still in flight. That
   window is real and this module does not paper over it.
3. **`stream_overrun` is a re-attach, not a failure** (§14). It reads as
   a note that says lines were skipped, because the journal still holds
   every one of them.
4. **A prompt is user text and an answer is model text.** Neither is
   markup and neither is a command. They are rendered as text, escaped by
   the template, and nothing here interprets them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from . import jsonfiles
from .jsonfiles import Json

#: How much assembled text one turn contributes. A long answer is still
#: an answer, but a page has to render in bounded time (invariant 14).
MAX_ANSWER_CHARS: Final = 40_000

#: How many entries one page holds.
MAX_ENTRIES: Final = 2_000

#: One `text_delta`. The display cap in `jsonfiles` is 500 characters,
#: which is right for a fault message and wrong here: it would cut a
#: long delta in the middle and the page would never say so. The real
#: bound is `MAX_ANSWER_CHARS`, applied to the assembled answer.
MAX_DELTA_CHARS: Final = 8 * 1024

_TEXT_DELTA: Final = "text_delta"
_MESSAGE_UPDATE: Final = "message_update"
_STREAM_OVERRUN: Final = "stream_overrun"


class Voice(StrEnum):
    """Who is speaking in one entry."""

    PROMPT = "prompt"
    ANSWER = "answer"
    APPROVAL = "approval"
    NOTE = "note"
    FAILURE = "failure"


@dataclass(frozen=True)
class Entry:
    voice: Voice
    turn: str
    ts: str
    text: str
    detail: str = ""
    #: The answer was cut at the display cap. The page says so.
    truncated: bool = False


def fold(lines: Iterable[Json]) -> tuple[Entry, ...]:
    """Journal lines in, entries out, oldest first."""
    entries: list[Entry] = []
    answers: dict[str, list[str]] = {}

    for line in lines:
        if len(entries) >= MAX_ENTRIES:
            break

        _one(line, entries, answers)

    # A turn still running has an answer nobody flushed yet. It belongs
    # on the page: "in flight" is a state, not a reason to show nothing.
    for turn in list(answers):
        _flush(turn, entries, answers, "")

    return tuple(entries)


def _one(line: Json, entries: list[Entry], answers: dict[str, list[str]]) -> None:
    kind = jsonfiles.whole(line, "kind")

    # Rule 1: a heartbeat is not part of the session.
    if kind == "heartbeat":
        return

    turn = jsonfiles.whole(line, "turn")
    ts = jsonfiles.whole(line, "ts")
    body = jsonfiles.child(line, "body")

    if kind == "pi_event":
        _delta(turn, body, answers)
        return

    handler = _HANDLERS.get(kind)

    if handler is None:
        return

    # Anything that is not a pi event ends the answer being assembled,
    # so the entries stay in the order the journal wrote them.
    _flush(turn, entries, answers, ts)
    handler(turn, ts, body, entries)


def _delta(turn: str, body: Json, answers: dict[str, list[str]]) -> None:
    """Assistant text, one `text_delta` at a time (contract 02 §15.1)."""
    if jsonfiles.whole(body, "type") != _MESSAGE_UPDATE:
        return

    event = jsonfiles.child(body, "assistantMessageEvent")

    if jsonfiles.whole(event, "type") != _TEXT_DELTA:
        return

    answers.setdefault(turn, []).append(jsonfiles.text(event, "delta", MAX_DELTA_CHARS))


def _flush(turn: str, entries: list[Entry], answers: dict[str, list[str]], ts: str) -> None:
    parts = answers.pop(turn, None)

    if not parts:
        return

    whole = "".join(parts)

    entries.append(
        Entry(
            voice=Voice.ANSWER,
            turn=turn,
            ts=ts,
            text=whole[:MAX_ANSWER_CHARS],
            truncated=len(whole) > MAX_ANSWER_CHARS,
        )
    )


def _prompt(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    # Contract 02 §5.1: `status_stale` says caregiver's document was over
    # 90 s old when the turn started. The turn still ran.
    stale = "caregiver's status was stale when this turn started" if _stale(body) else ""
    entries.append(
        Entry(
            voice=Voice.PROMPT, turn=turn, ts=ts, text=jsonfiles.text(body, "prompt"), detail=stale
        )
    )


def _stale(body: Json) -> bool:
    return jsonfiles.flag(body, "status_stale")


def _settled(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    """Rule 2: this line, and only this line, ends a turn successfully."""
    del body
    entries.append(Entry(voice=Voice.NOTE, turn=turn, ts=ts, text="turn settled"))


def _failed(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    reason = jsonfiles.whole(body, "reason") or "unknown"
    entries.append(
        Entry(
            voice=Voice.FAILURE,
            turn=turn,
            ts=ts,
            text=f"turn failed: {reason}",
            detail=jsonfiles.text(body, "message"),
        )
    )


def _aborted(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    reason = jsonfiles.whole(body, "reason") or "unknown"
    entries.append(Entry(voice=Voice.FAILURE, turn=turn, ts=ts, text=f"turn aborted: {reason}"))


def _asked(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    tool = jsonfiles.whole(body, "tool")
    entries.append(
        Entry(
            voice=Voice.APPROVAL,
            turn=turn,
            ts=ts,
            text=f"waiting for a decision on {tool}",
            detail=jsonfiles.text(body, "summary"),
        )
    )


def _resolved(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    decision = jsonfiles.whole(body, "decision") or "unknown"
    waited = jsonfiles.integer(body, "waited_s")
    entries.append(
        Entry(voice=Voice.APPROVAL, turn=turn, ts=ts, text=f"{decision} after {waited}s")
    )


def _note(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    """Rule 3: an overrun says lines were skipped, not that one was lost."""
    named = jsonfiles.whole(body, "note")

    if named == _STREAM_OVERRUN:
        last = jsonfiles.integer(body, "last_seq")
        text = f"the reader fell behind at line {last}; nothing was lost, re-read to see the rest"
        entries.append(Entry(voice=Voice.NOTE, turn=turn, ts=ts, text=text))
        return

    entries.append(Entry(voice=Voice.NOTE, turn=turn, ts=ts, text=named or _summary(body)))


def _queued(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    depth = jsonfiles.integer(body, "queue_depth")
    entries.append(
        Entry(
            voice=Voice.PROMPT,
            turn=turn,
            ts=ts,
            text=jsonfiles.text(body, "prompt"),
            detail=f"queued behind {depth}",
        )
    )


def _titled(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    title = jsonfiles.text(body, "title")
    entries.append(Entry(voice=Voice.NOTE, turn=turn, ts=ts, text=f"titled {title}"))


def _writer(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    holder = jsonfiles.whole(body, "holder") or "nobody"
    entries.append(
        Entry(
            voice=Voice.NOTE,
            turn=turn,
            ts=ts,
            text=f"the writer lease went to {holder}",
            detail=jsonfiles.whole(body, "reason"),
        )
    )


def _created(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    del body
    entries.append(Entry(voice=Voice.NOTE, turn=turn, ts=ts, text="session created"))


def _fallback(turn: str, ts: str, body: Json, entries: list[Entry]) -> None:
    wanted = jsonfiles.whole(body, "wanted_entry")
    entries.append(
        Entry(
            voice=Voice.NOTE,
            turn=turn,
            ts=ts,
            text=f"branched from the session head instead of {wanted}",
            detail=jsonfiles.whole(body, "reason"),
        )
    )


def _summary(body: Json) -> str:
    """A body this noticeboard has no word for, named by its keys alone."""
    return ", ".join(sorted(body)[:10]) or "(empty)"


#: Contract 02 §8.1's closed set, minus `pi_event` and `heartbeat`, which
#: have their own paths above. A kind outside it is dropped rather than
#: rendered: §8.1 says a reader never meets a new one.
_HANDLERS: Final = {
    "session_created": _created,
    "session_titled": _titled,
    "writer_changed": _writer,
    "turn_queued": _queued,
    "turn_started": _prompt,
    "approval_requested": _asked,
    "approval_resolved": _resolved,
    "turn_settled": _settled,
    "turn_failed": _failed,
    "turn_aborted": _aborted,
    "branch_fallback": _fallback,
    "note": _note,
}
