"""The channel abstraction: one framed record stream to one facade.

One vmm ACP stream carries one channel to the facade of one session
(decision CH20). Everything above this module works in framed records and
never touches a socket, a process or HTTP. A test drives each layer with
`tests/fake_channel.py`.
"""

from __future__ import annotations

from typing import Protocol

from .lines import RawLine


class ChannelClosed(Exception):
    """The channel is gone. Every call in flight on it is lost."""


class Channel(Protocol):
    """One long-lived, full-duplex record stream to one facade."""

    async def send(self, line: str) -> None:
        """Write one framed record. The caller has already added the LF.
        Raises `ChannelClosed`."""
        ...

    async def receive(self) -> RawLine | None:
        """The next inbound record, or None at end of stream."""
        ...

    async def close(self) -> None:
        """Release the transport. Safe to call twice."""
        ...
