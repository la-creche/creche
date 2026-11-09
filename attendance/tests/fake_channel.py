"""A channel whose far side is a test, not a facade.

Both directions are queues. A test writes what the far side would say and
reads what the host sent, so a link can be exercised whole without a vmm.
"""

from __future__ import annotations

import asyncio

from attendance.channel import ChannelClosed
from attendance.lines import MAX_LINE_BYTES, LineSplitter, RawLine


class FakeChannel:
    """The far side of one vmm ACP stream."""

    def __init__(
        self, max_line_bytes: int = MAX_LINE_BYTES, close_gate: asyncio.Event | None = None
    ) -> None:
        self._to_host: asyncio.Queue[RawLine | None] = asyncio.Queue()
        self._to_far: asyncio.Queue[str] = asyncio.Queue()
        self._splitter = LineSplitter(max_line_bytes)
        #: A close waits for this event, as a close waits for the vmm.
        self._close_gate = close_gate
        self.closing = False
        self.closed = False

    async def send(self, line: str) -> None:
        if self.closed:
            raise ChannelClosed("channel is closed")

        await self._to_far.put(line)

    async def receive(self) -> RawLine | None:
        item = await self._to_host.get()

        if item is None:
            # The end stays the end for each later read.
            await self._to_host.put(None)

        return item

    async def close(self) -> None:
        if self.closing:
            return

        self.closing = True

        if self._close_gate is not None:
            await self._close_gate.wait()

        self.closed = True
        await self._to_host.put(None)

    async def feed_bytes(self, chunk: bytes) -> None:
        """Push raw bytes from the far side, framing included."""
        for line in self._splitter.feed(chunk):
            await self._to_host.put(line)

    async def feed(self, text: str) -> None:
        """Push one record from the far side. The LF is added here."""
        await self.feed_bytes(text.encode("utf-8") + b"\n")

    async def drop(self) -> None:
        """The far side vanished. Reads see end of stream."""
        await self._to_host.put(None)

    async def next_host_line(self, timeout: float = 2.0) -> str:
        """The next record the host wrote. Fails the test on silence."""
        return await asyncio.wait_for(self._to_far.get(), timeout)

    def host_lines_waiting(self) -> int:
        return self._to_far.qsize()
