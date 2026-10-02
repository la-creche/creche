"""Keeping an SSE response alive while the turn is quiet.

A turn is silent for the whole of every tool call, and a delegation that
cold-starts another sandbox can take minutes. Silence lets every proxy on
the path call the stream idle, and it makes a real stall look like normal
work. A comment frame every few seconds is the cheapest cure.

The frames are pumped through a bounded queue by one task, so the timer can
fire while the source waits. The bound is what puts backpressure on the read
from `sessiond` when the browser is slower than the turn: the door stops
reading rather than growing a buffer (contract 03 §9 rule 2 has the same
shape on the channel).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator

from .sse import KEEPALIVE_FRAME

# Well under the 60 s idle default every nginx on the path uses, and far
# under the time one tool call can take.
KEEPALIVE_S = 15.0

# One thousand frames, the same bound a `sessiond` reader gets.
QUEUE_LIMIT = 1000

_END = object()


async def with_keepalive(
    source: AsyncIterator[str],
    idle_s: float = KEEPALIVE_S,
) -> AsyncGenerator[str, None]:
    """Yield the source's frames, plus a comment frame when it goes quiet."""
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=QUEUE_LIMIT)
    failures: list[BaseException] = []

    async def pump() -> None:
        # The source is iterated here and nowhere else, so its own `async
        # with` blocks open and close in this one task. Closing an httpx
        # stream from another task is what breaks anyio's cancel scopes.
        try:
            async for frame in source:
                await queue.put(frame)
        except Exception as exc:  # reported to the consumer below
            failures.append(exc)
        finally:
            await queue.put(_END)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=idle_s)
            except TimeoutError:
                yield KEEPALIVE_FRAME
                continue

            if item is _END:
                break

            yield str(item)

        if failures:
            raise failures[0]
    finally:
        # The consumer stopped, or the source ended. Cancelling the pump
        # closes the stream to `sessiond` and stops nothing on the host: the
        # turn keeps running and its events are already journalled
        # (invariant 4, contract 02 §5.4).
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
