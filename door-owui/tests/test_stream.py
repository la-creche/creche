"""The keepalive relay: frames pass through, silence gets a comment frame,
and closing early does not hang or lose the source's own cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from agent_door_owui.sse import KEEPALIVE_FRAME
from agent_door_owui.stream import with_keepalive


async def _frames(*items: str) -> AsyncIterator[str]:
    for item in items:
        yield item


async def test_frames_pass_through_unchanged() -> None:
    out = [frame async for frame in with_keepalive(_frames("a", "b", "c"))]

    assert out == ["a", "b", "c"]


async def test_silence_gets_a_keepalive_comment() -> None:
    async def slow() -> AsyncIterator[str]:
        yield "first"
        await asyncio.sleep(0.2)
        yield "second"

    out = [frame async for frame in with_keepalive(slow(), idle_s=0.03)]

    assert out[0] == "first"
    assert KEEPALIVE_FRAME in out
    assert out[-1] == "second"


async def test_a_source_failure_surfaces_after_its_own_frames() -> None:
    async def breaks() -> AsyncIterator[str]:
        yield "before"
        raise ValueError("upstream broke")

    seen: list[str] = []
    with pytest.raises(ValueError, match="upstream broke"):
        async for frame in with_keepalive(breaks()):
            seen.append(frame)

    assert seen == ["before"]


async def test_closing_early_runs_the_sources_own_cleanup() -> None:
    # This is the client-disconnect case: the reader stops iterating before
    # the source is exhausted. The turn itself is not this module's concern
    # (contract 02 §5.4: a disconnect changes nothing) — what this module
    # owns is closing ITS OWN resource (the source) cleanly, in the same
    # task that opened it, rather than leaking the pump task.
    closed = False

    async def never_ends() -> AsyncIterator[str]:
        nonlocal closed
        try:
            yield "one"
            while True:
                await asyncio.sleep(10)
                yield "unreachable"
        finally:
            closed = True

    gen = with_keepalive(never_ends())
    first = await anext(gen)
    assert first == "one"

    await gen.aclose()

    # aclose() only guarantees the generator will not run again, not that
    # the spawned pump task has already unwound. Give the loop one tick.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert closed


async def test_the_end_marker_is_not_yielded_as_a_frame() -> None:
    out = [frame async for frame in with_keepalive(_frames("only"))]

    assert out == ["only"]
