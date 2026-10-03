"""Attached readers, replay and the heartbeat (contract 02 §5.5, §8, §9).

The journal is the record and the live stream is a convenience. A reader
subscribes first and replays second, so a line written between the two
arrives twice and is dropped by sequence, never missed.

Each reader has a bounded buffer. A reader that falls behind is dropped, not
buffered without limit: a stall on one socket must never stall the family's
one channel (contract 03 §9 rule 2). Nothing is lost, because every line is
already on disk and the reader re-attaches with the last sequence it saw.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from enum import Enum

from .clock import now
from .journal import Journal
from .models import TERMINAL_LINE_KINDS, JournalLine, LineKind

# Contract 03 §9 rule 2's default.
STREAM_BUFFER_LINES = 1000

# Contract 02 §5.5. A reader tells a live stream from a dead socket by this.
HEARTBEAT_INTERVAL_S = 15.0

# Contract 02 §5.5: a slow reader is dropped with one `note` line whose body
# carries this word and the sequence to re-attach from. It is not a §8.1
# kind and not a turn failure (§14).
OVERRUN_NOTE = "stream_overrun"


class Follow(Enum):
    """Whether a stream stays open after the replay (contract 02 §5.5)."""

    REPLAY_ONLY = "replay_only"
    KEEP_OPEN = "keep_open"


class StreamEnd(Enum):
    """Whether a terminal turn line closes the stream (contract 02 §5.4)."""

    NEVER = "never"
    AFTER_TERMINAL = "after_terminal"


class _Reader:
    """One attached client's bounded buffer."""

    __slots__ = ("overrun", "queue")

    def __init__(self, capacity: int) -> None:
        self.queue: asyncio.Queue[JournalLine] = asyncio.Queue(maxsize=capacity)
        self.overrun = False

    def offer(self, line: JournalLine) -> None:
        """Hand over one line. A full buffer marks the reader, never blocks."""
        if self.overrun:
            return

        try:
            self.queue.put_nowait(line)
        except asyncio.QueueFull:
            self.overrun = True


class StreamHub:
    """Fans journal lines out to every attached reader of one session."""

    def __init__(self, journal: Journal, capacity: int = STREAM_BUFFER_LINES) -> None:
        self._journal = journal
        self._capacity = capacity
        self._readers: dict[tuple[str, str], set[_Reader]] = {}

    def publish(self, family: str, session: str, line: JournalLine) -> None:
        """Fan one already-written line out. Never blocks on a slow reader."""
        for reader in self._readers.get((family, session), ()):
            reader.offer(line)

    def reader_count(self, family: str, session: str) -> int:
        return len(self._readers.get((family, session), ()))

    def drop_all(self, family: str, session: str) -> None:
        """Forget this session's readers. Their streams end on their own."""
        for reader in self._readers.pop((family, session), ()):
            reader.overrun = True

    async def stream(
        self,
        family: str,
        session: str,
        from_seq: int = 0,
        turn: str | None = None,
        follow: Follow = Follow.KEEP_OPEN,
        end: StreamEnd = StreamEnd.NEVER,
        heartbeat_s: float = HEARTBEAT_INTERVAL_S,
    ) -> AsyncGenerator[JournalLine, None]:
        """Replay, then follow. Lines come out in ascending, gapless order."""
        reader = self._subscribe(family, session)

        try:
            last_seq = from_seq

            for line in self._journal.replay(family, session, from_seq, turn):
                last_seq = line.journal_seq if line.journal_seq is not None else last_seq
                yield line

                if _closes(line, turn, end):
                    return

            if follow is Follow.REPLAY_ONLY:
                return

            async for line in self._follow(reader, last_seq, turn, end, heartbeat_s):
                yield line
        finally:
            self._unsubscribe(family, session, reader)

    async def _follow(
        self,
        reader: _Reader,
        last_seq: int,
        turn: str | None,
        end: StreamEnd,
        heartbeat_s: float,
    ) -> AsyncIterator[JournalLine]:
        while True:
            if reader.overrun:
                yield _overrun_line(last_seq)
                return

            line = await _next_line(reader, heartbeat_s)

            if line is None:
                yield _heartbeat_line(last_seq)
                continue

            # A line the replay already produced arrives again when it was
            # written between the subscribe and the read. Sequence decides.
            if line.journal_seq is None or line.journal_seq <= last_seq:
                continue

            last_seq = line.journal_seq

            if turn is not None and line.turn != turn:
                continue

            yield line

            if _closes(line, turn, end):
                return

    def _subscribe(self, family: str, session: str) -> _Reader:
        reader = _Reader(self._capacity)
        self._readers.setdefault((family, session), set()).add(reader)
        return reader

    def _unsubscribe(self, family: str, session: str, reader: _Reader) -> None:
        key = (family, session)
        readers = self._readers.get(key)

        if readers is None:
            return

        readers.discard(reader)

        if not readers:
            del self._readers[key]


async def _next_line(reader: _Reader, heartbeat_s: float) -> JournalLine | None:
    """The next buffered line, or None when the heartbeat is due instead."""
    try:
        return await asyncio.wait_for(reader.queue.get(), heartbeat_s)
    except TimeoutError:
        return None


def _closes(line: JournalLine, turn: str | None, end: StreamEnd) -> bool:
    """Contract 02 §5.4: a turn stream ends after that turn's terminal line."""
    if end is StreamEnd.NEVER or turn is None:
        return False

    return line.turn == turn and line.kind in TERMINAL_LINE_KINDS


def _heartbeat_line(last_seq: int) -> JournalLine:
    """Contract 02 §8.1: the one kind that is never written to disk.

    It carries `journal_seq: null` and the newest written sequence in the
    body, so it never takes a number out of the gapless run.
    """
    return JournalLine(
        journal_seq=None,
        ts=now(),
        kind=LineKind.HEARTBEAT,
        turn=None,
        body={"last_seq": last_seq},
    )


def _overrun_line(last_seq: int) -> JournalLine:
    return JournalLine(
        journal_seq=None,
        ts=now(),
        kind=LineKind.NOTE,
        turn=None,
        body={"note": OVERRUN_NOTE, "last_seq": last_seq},
    )
