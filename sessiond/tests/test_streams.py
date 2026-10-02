"""Replay, follow, the heartbeat and the slow-reader drop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from agent_sessiond.journal import Journal
from agent_sessiond.models import JournalLine, LineKind
from agent_sessiond.streams import (
    OVERRUN_NOTE,
    Follow,
    StreamEnd,
    StreamHub,
)

FAMILY = "chat"
SESSION = "owui-3f2a9c41"
TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
OTHER_TURN = "01JBQ7X1M2K4T9V6K2H8M3N5PS"
FAST_HEARTBEAT_S = 0.05


def make_hub(tmp_path: Path, capacity: int = 1000) -> tuple[Journal, StreamHub]:
    journal = Journal(tmp_path)
    journal.register(FAMILY, SESSION, 0)
    return journal, StreamHub(journal, capacity=capacity)


def append(journal: Journal, hub: StreamHub, kind: LineKind, turn: str | None) -> JournalLine:
    """Append first, then fan out. That is the service's own order."""
    line = journal.append(FAMILY, SESSION, kind, turn, {})
    hub.publish(FAMILY, SESSION, line)
    return line


async def take(stream: AsyncGenerator[JournalLine, None], count: int) -> list[JournalLine]:
    collected: list[JournalLine] = []

    for _ in range(count):
        collected.append(await asyncio.wait_for(anext(stream), 2.0))

    return collected


async def test_replay_only_reads_the_journal(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path)

    for _ in range(3):
        append(journal, hub, LineKind.PI_EVENT, TURN)

    stream = hub.stream(FAMILY, SESSION, follow=Follow.REPLAY_ONLY)
    lines = [line async for line in stream]

    assert [line.journal_seq for line in lines] == [1, 2, 3]


async def test_replay_starts_after_from_seq(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path)

    for _ in range(5):
        append(journal, hub, LineKind.PI_EVENT, TURN)

    stream = hub.stream(FAMILY, SESSION, from_seq=3, follow=Follow.REPLAY_ONLY)
    lines = [line async for line in stream]

    assert [line.journal_seq for line in lines] == [4, 5]


async def test_replay_filters_to_one_turn(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path)
    append(journal, hub, LineKind.PI_EVENT, TURN)
    append(journal, hub, LineKind.PI_EVENT, OTHER_TURN)
    append(journal, hub, LineKind.PI_EVENT, TURN)

    stream = hub.stream(FAMILY, SESSION, turn=TURN, follow=Follow.REPLAY_ONLY)
    lines = [line async for line in stream]

    assert [line.journal_seq for line in lines] == [1, 3]


async def test_a_follower_sees_later_lines(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path)
    append(journal, hub, LineKind.TURN_STARTED, TURN)

    stream = hub.stream(FAMILY, SESSION, heartbeat_s=FAST_HEARTBEAT_S)
    first = await take(stream, 1)
    assert first[0].kind is LineKind.TURN_STARTED

    append(journal, hub, LineKind.PI_EVENT, TURN)
    live = await take(stream, 1)

    assert live[0].journal_seq == 2
    await stream.aclose()


async def test_a_line_written_during_the_replay_is_not_repeated(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path)
    append(journal, hub, LineKind.TURN_STARTED, TURN)

    stream = hub.stream(FAMILY, SESSION, heartbeat_s=FAST_HEARTBEAT_S)
    await take(stream, 1)

    # The same line is offered again, as it would be by a publish that raced
    # the subscribe. The follower drops it by sequence.
    hub.publish(FAMILY, SESSION, journal.replay(FAMILY, SESSION).__next__())
    append(journal, hub, LineKind.PI_EVENT, TURN)
    lines = await take(stream, 1)

    assert [line.journal_seq for line in lines] == [2]
    await stream.aclose()


async def test_an_idle_stream_heartbeats(tmp_path: Path) -> None:
    _, hub = make_hub(tmp_path)
    stream = hub.stream(FAMILY, SESSION, heartbeat_s=FAST_HEARTBEAT_S)
    lines = await take(stream, 2)

    for line in lines:
        assert line.kind is LineKind.HEARTBEAT
        assert line.journal_seq is None
        assert line.body["last_seq"] == 0

    await stream.aclose()


async def test_a_turn_stream_ends_after_its_terminal_line(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path)
    append(journal, hub, LineKind.TURN_STARTED, TURN)
    append(journal, hub, LineKind.TURN_SETTLED, TURN)
    append(journal, hub, LineKind.PI_EVENT, OTHER_TURN)

    stream = hub.stream(FAMILY, SESSION, turn=TURN, end=StreamEnd.AFTER_TERMINAL)
    lines = [line async for line in stream]

    assert [line.kind for line in lines] == [LineKind.TURN_STARTED, LineKind.TURN_SETTLED]


async def test_a_terminal_line_arriving_live_ends_the_stream(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path)
    append(journal, hub, LineKind.TURN_STARTED, TURN)

    stream = hub.stream(
        FAMILY,
        SESSION,
        turn=TURN,
        end=StreamEnd.AFTER_TERMINAL,
        heartbeat_s=FAST_HEARTBEAT_S,
    )
    await take(stream, 1)
    append(journal, hub, LineKind.TURN_FAILED, TURN)
    tail = [line async for line in stream]

    assert [line.kind for line in tail] == [LineKind.TURN_FAILED]


async def test_a_slow_reader_is_dropped_with_a_note(tmp_path: Path) -> None:
    journal, hub = make_hub(tmp_path, capacity=2)
    append(journal, hub, LineKind.TURN_STARTED, TURN)

    stream = hub.stream(FAMILY, SESSION, heartbeat_s=FAST_HEARTBEAT_S)
    await take(stream, 1)

    for _ in range(5):
        append(journal, hub, LineKind.PI_EVENT, TURN)

    lines = [line async for line in stream]
    last = lines[-1]

    assert last.kind is LineKind.NOTE
    assert last.body["note"] == OVERRUN_NOTE
    assert last.journal_seq is None


async def test_one_line_reaches_every_attached_reader(tmp_path: Path) -> None:
    """Many readers attach at once. Attaching is a read (invariant 3)."""
    journal, hub = make_hub(tmp_path)
    streams = [hub.stream(FAMILY, SESSION, heartbeat_s=FAST_HEARTBEAT_S) for _ in range(3)]
    waiting = [asyncio.create_task(take(stream, 1)) for stream in streams]

    while hub.reader_count(FAMILY, SESSION) < len(streams):
        await asyncio.sleep(0)

    append(journal, hub, LineKind.PI_EVENT, TURN)
    seen = await asyncio.gather(*waiting)

    assert [lines[0].journal_seq for lines in seen] == [1, 1, 1]

    for stream in streams:
        await stream.aclose()

    assert hub.reader_count(FAMILY, SESSION) == 0
