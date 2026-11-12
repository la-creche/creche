"""Session ids of a chat, and how a scenario waits with a request in flight.

No helper here holds an object of a service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Final

_DEFAULT_DEADLINE_S: Final = 30.0
_POLL_S: Final = 0.01


async def until(
    check: Callable[[], bool], what: str, deadline_s: float = _DEFAULT_DEADLINE_S
) -> None:
    """Poll until a condition holds, or say what never happened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s

    while not check():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {deadline_s} s waiting for {what}")

        await asyncio.sleep(_POLL_S)
