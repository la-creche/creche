"""The host journal: append first, then fan out (contract 02 §8.2, §8.3).

The journal is the record. The live stream is a convenience. A line reaches
disk before any reader sees it, so a reader can never see a line that a replay
would miss, and a client that disconnects changes nothing (invariant 4).
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from .atomic import MODE_PUBLIC_READ, as_object
from .clock import now, parse_rfc3339
from .models import FIRST_JOURNAL_SEQ, TERMINAL_LINE_KINDS, JournalLine, LineKind
from .paths import journal_file, session_dir

FSYNC_INTERVAL_S = 2.0
MAX_OPEN_JOURNALS = 64

# How much of a journal's end `tail_seq` reads. One line is capped at 1 MiB
# by the channel, so this window always holds at least one whole line.
TAIL_WINDOW_BYTES = 2_097_152

_LF = b"\n"


class _Handle:
    """One open append handle plus its fsync bookkeeping."""

    __slots__ = ("dirty", "fd", "last_fsync")

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.last_fsync = time.monotonic()
        self.dirty = False


class Journal:
    """Per-session append-only NDJSON with a bounded set of open handles.

    Handles are cached because a chat session appends hundreds of lines per
    turn. The cache is bounded because the store holds every session that ever
    existed and a process has a file-descriptor limit.
    """

    def __init__(self, sessions_root: Path, fsync_interval_s: float = FSYNC_INTERVAL_S) -> None:
        self._root = sessions_root
        self._fsync_interval_s = fsync_interval_s
        self._handles: OrderedDict[tuple[str, str], _Handle] = OrderedDict()
        self._next_seq: dict[tuple[str, str], int] = {}

    def register(self, family: str, session: str, last_seq: int) -> None:
        """Seed the sequence counter from a session record after a restart."""
        self._next_seq[(family, session)] = last_seq + 1

    def next_seq(self, family: str, session: str) -> int:
        """The sequence the next append will take."""
        return self._next_seq.get((family, session), FIRST_JOURNAL_SEQ)

    def append(
        self,
        family: str,
        session: str,
        kind: LineKind,
        turn: str | None,
        body: dict[str, Any],
        moment: datetime | None = None,
    ) -> JournalLine:
        """Append one line and return it with its sequence number filled in."""
        key = (family, session)
        seq = self._next_seq.get(key, FIRST_JOURNAL_SEQ)
        line = JournalLine(
            journal_seq=seq,
            ts=moment if moment is not None else now(),
            kind=kind,
            turn=turn,
            body=body,
        )
        payload = json.dumps(line.to_api(), separators=(",", ":")).encode("utf-8") + _LF

        handle = self._handle(family, session)
        _write_all(handle.fd, payload)
        handle.dirty = True

        if kind in TERMINAL_LINE_KINDS:
            self._sync(handle)

        self._next_seq[key] = seq + 1
        return line

    def replay(
        self,
        family: str,
        session: str,
        from_seq: int = 0,
        turn: str | None = None,
    ) -> Iterator[JournalLine]:
        """Yield stored lines after `from_seq`, oldest first.

        A line that does not parse is skipped rather than fatal. The file is
        append-only and this service is its only writer, so the one way to see
        a bad line is a torn tail after a crash.
        """
        path = journal_file(self._root, family, session)
        self.flush(family, session)

        try:
            handle = path.open("rb")
        except OSError:
            return

        with handle:
            for raw in handle:
                parsed = _parse_line(raw)

                if parsed is None:
                    continue

                if parsed.journal_seq is None or parsed.journal_seq <= from_seq:
                    continue

                if turn is not None and parsed.turn != turn:
                    continue

                yield parsed

    def tail_seq(self, family: str, session: str, window: int = TAIL_WINDOW_BYTES) -> int:
        """The newest sequence actually on disk, or 0 when the file is empty.

        The journal, not `session.json`, is what decides the next number. The
        session file is rewritten at a turn's start and end only, so after a
        crash it can name a smaller sequence than the journal holds. Seeding
        from it would reuse numbers and break the gapless rule (§8).
        """
        path = journal_file(self._root, family, session)

        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - window))
                block = handle.read()
        except OSError:
            return 0

        # A partial first line is expected: the window starts mid-file.
        for raw in reversed(block.split(_LF)):
            parsed = _parse_line(raw) if raw else None

            if parsed is not None and parsed.journal_seq is not None:
                return parsed.journal_seq

        return 0

    def flush(self, family: str, session: str) -> None:
        """Force this session's buffered appends to disk."""
        handle = self._handles.get((family, session))

        if handle is not None:
            self._sync(handle)

    def flush_due(self) -> None:
        """Sync every handle older than the interval (contract 02 §8.3)."""
        deadline = time.monotonic() - self._fsync_interval_s

        for handle in list(self._handles.values()):
            if handle.dirty and handle.last_fsync <= deadline:
                self._sync(handle)

    def close(self, family: str, session: str) -> None:
        """Release one session's handle. Called before a delete."""
        handle = self._handles.pop((family, session), None)
        self._next_seq.pop((family, session), None)

        if handle is not None:
            self._close(handle)

    def close_all(self) -> None:
        for handle in self._handles.values():
            self._close(handle)

        self._handles.clear()

    def _handle(self, family: str, session: str) -> _Handle:
        key = (family, session)
        found = self._handles.get(key)

        if found is not None:
            self._handles.move_to_end(key)
            return found

        session_dir(self._root, family, session).mkdir(parents=True, exist_ok=True)
        path = journal_file(self._root, family, session)
        # Read access is for the torn-tail check only. Every write appends.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, MODE_PUBLIC_READ)

        try:
            _end_torn_tail(fd)
        except OSError:
            os.close(fd)
            raise

        handle = _Handle(fd)
        self._handles[key] = handle
        self._evict()
        return handle

    def _evict(self) -> None:
        while len(self._handles) > MAX_OPEN_JOURNALS:
            _, oldest = self._handles.popitem(last=False)
            self._close(oldest)

    def _sync(self, handle: _Handle) -> None:
        os.fsync(handle.fd)
        handle.dirty = False
        handle.last_fsync = time.monotonic()

    def _close(self, handle: _Handle) -> None:
        if handle.dirty:
            os.fsync(handle.fd)

        os.close(handle.fd)


def _write_all(fd: int, payload: bytes) -> None:
    """Write the whole line. A short write is retried, never left half-done."""
    written = 0

    while written < len(payload):
        written += os.write(fd, payload[written:])


def _end_torn_tail(fd: int) -> None:
    """End a last line that has no LF, so the next append is a line of its own.

    A crash in the middle of a write leaves such a line. Without the LF the
    next append joins it, and no reader can parse the joined line. The torn
    bytes stay on disk as a line that every reader skips. Truncation would
    delete the record of the crash, and it would delete a whole line that
    lacks only its LF, which a replay before the append did return.
    """
    size = os.fstat(fd).st_size

    if size == 0:
        return

    if os.pread(fd, 1, size - 1) != _LF:
        _write_all(fd, _LF)


def _parse_line(raw: bytes) -> JournalLine | None:
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError covers bad UTF-8, bad JSON and an integer past the
        # interpreter's digit limit. Deep nesting raises RecursionError, and
        # a journal older than `wire.MAX_EVENT_DEPTH` can hold such a line.
        return None

    record = as_object(parsed)

    if record is None:
        return None

    seq = record.get("journal_seq")
    kind_text = record.get("kind")
    body = record.get("body")

    if not isinstance(seq, int) or not isinstance(kind_text, str):
        return None

    try:
        kind = LineKind(kind_text)
    except ValueError:
        return None

    turn_value = record.get("turn")
    turn = turn_value if isinstance(turn_value, str) else None

    # A replayed line keeps the timestamp it was written with. Re-stamping it
    # would make a replay look like a fresh event to every reader.
    ts_value = record.get("ts")
    stamped = parse_rfc3339(ts_value) if isinstance(ts_value, str) else None

    return JournalLine(
        journal_seq=seq,
        ts=stamped if stamped is not None else now(),
        kind=kind,
        turn=turn,
        body=as_object(body) or {},
    )
