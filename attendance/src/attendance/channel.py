"""The channel abstraction and its in-process fake (contract 03 §1).

One `sbx exec` carries one channel to one sandbox. Everything above this
module works in framed records and never touches a pipe, a process or a CLI.
That is what lets every test run without the host and without `sbx`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from .wire import MAX_LINE_BYTES, LineSplitter, RawLine


class ChannelClosed(Exception):
    """The channel is gone. Every in-flight turn on it is lost."""


@dataclass(slots=True, frozen=True)
class SandboxDial:
    """What it takes to open one channel (contract 03 §7.1).

    Two values, never one. `sbx exec` forwards no host environment, so the
    playpen learns where its mounts are only from the env file `caregiver`
    wrote; the status document publishes that path per sandbox (contract 05
    §4.1). A dial without it starts a playpen that answers `fatal`.
    """

    sandbox: str
    env_file: str


class Channel(Protocol):
    """One long-lived, full-duplex record stream to one sandbox."""

    @property
    def sandbox(self) -> str:
        """The sandbox id this channel was dialled for."""
        ...

    @property
    def alive(self) -> bool: ...

    async def start(self) -> None:
        """Open the transport. Raises ChannelClosed when it cannot open."""
        ...

    async def send(self, line: str) -> None:
        """Write one framed record. The caller has already added the LF."""
        ...

    async def receive(self) -> RawLine | None:
        """The next inbound record, or None at end of stream."""
        ...

    async def close(self) -> None:
        """Release the transport. Safe to call twice."""
        ...


class FakeChannel:
    """A channel whose far side is a test, not a sandbox.

    Both directions are queues. A test writes what the playpen would say
    and reads what the host sent, so contract 03 can be exercised whole
    without a process anywhere.
    """

    def __init__(self, sandbox: str, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self._sandbox = sandbox
        self._max_line_bytes = max_line_bytes
        self._to_host: asyncio.Queue[RawLine | None] = asyncio.Queue()
        self._to_sandbox: asyncio.Queue[str] = asyncio.Queue()
        self._splitter = LineSplitter(max_line_bytes)
        self._alive = False
        self.start_calls = 0

    @property
    def sandbox(self) -> str:
        return self._sandbox

    @property
    def alive(self) -> bool:
        return self._alive

    async def start(self) -> None:
        self.start_calls += 1
        self._alive = True

    async def send(self, line: str) -> None:
        if not self._alive:
            raise ChannelClosed("channel is closed")

        await self._to_sandbox.put(line)

    async def receive(self) -> RawLine | None:
        item = await self._to_host.get()

        if item is None:
            self._alive = False

        return item

    async def close(self) -> None:
        if not self._alive:
            return

        self._alive = False
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
        return await asyncio.wait_for(self._to_sandbox.get(), timeout)

    def host_lines_waiting(self) -> int:
        return self._to_sandbox.qsize()
