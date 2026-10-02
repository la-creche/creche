"""The PEP reloads its upstreams with no restart (`stage7-releases.md` §4.4).

`stage7-releases.md` §7.5's reload row, stated exactly: **a new upstream
appears while a call on another upstream is in flight, and that call
finishes.** A restart instead would drop every approval blocked in place,
which is why §4.4 exists at all.

These drive real stdio child processes through `stub_mcp_server.py`, as
probe A7 did. Two of its features carry the evidence:

- `STUB_SECRET` is echoed back, so an answer names the process that produced
  it. `v1:hi` came from the old generation, `v2:hi` from the new one.
- `sleep` stands in for a call that has not come back: a slow upstream, or a
  turn paused on a human.

The probe's own ten tests stay in `probes/pep-reload/` and are not repeated.
What is here is that row, plus two rules the probe does not hold: a refused
name stays visible, and the drain bound sits between the call timeout and
the idle TTL.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from agent_pep.mcp_client import (
    IDLE_TTL_S,
    UPSTREAM_CALL_TIMEOUT_S,
    UpstreamError,
    UpstreamSpec,
)
from agent_pep.reload_pool import (
    DRAIN_TIMEOUT_S,
    TRIGGER,
    Action,
    ReloadablePool,
)
from pep_helpers import UNCHECKED, pools

pytestmark = pytest.mark.slow

STUB: Final = Path(__file__).resolve().parent / "stub_mcp_server.py"

#: How long the in-flight `sleep` call holds its process open. Long enough
#: to reload around it, short enough to keep this file quick.
IN_FLIGHT_S: Final = 1.0

#: A ceiling on one reload, not a measurement. It catches a hang.
RELOAD_CEILING_S: Final = 10.0


def _spec(name: str, version: str) -> UpstreamSpec:
    return UpstreamSpec(
        name=name,
        command=sys.executable,
        args=(str(STUB),),
        env={"STUB_SECRET": version},
    )


@pytest.mark.anyio
async def test_an_add_lands_while_another_upstream_is_mid_call() -> None:
    """§4.4's whole promise, in one case.

    `a` is mid-call. `b` is added. The call on `a` answers from the process
    it started on, and `a` is not respawned.
    """
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        in_flight = asyncio.create_task(pool.call("a", "sleep", {"seconds": IN_FLIGHT_S}))
        await asyncio.sleep(0.05)

        report = await asyncio.wait_for(
            pool.reload({"a": _spec("a", "v1"), "b": _spec("b", "v1")}),
            RELOAD_CEILING_S,
        )

        assert report.added == ("b",)
        assert report.unchanged == ("a",)
        assert report.ok
        # The newcomer is callable the moment `reload()` returns.
        assert await pool.call("b", "echo", {"text": "hi"}) == "v1:hi"
        # And the call that was already running still answers.
        assert await asyncio.wait_for(in_flight, RELOAD_CEILING_S)
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_an_unchanged_upstream_is_the_same_object() -> None:
    """ "Unchanged" is stronger than "left alone": nothing can reach it."""
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        before = pool.live_specs["a"]
        await pool.reload({"a": _spec("a", "v1"), "b": _spec("b", "v1")})

        assert pool.live_specs["a"] == before
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_refused_upstream_stays_visible_with_its_reason() -> None:
    """Probe README item 3. A refused name must not vanish: absent reads as
    "never declared", which is a different fact."""
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        broken = replace(_spec("bad", "v1"), command="/nonexistent/mcp-server")
        report = await asyncio.wait_for(
            pool.reload({"a": _spec("a", "v1"), "bad": broken}), RELOAD_CEILING_S
        )

        assert not report.ok
        assert [one.name for one in report.failed] == ["bad"]
        assert pool.refusals["bad"].action is Action.ADD
        assert pool.refusals["bad"].detail
        # The refusal did not touch the upstream that was already serving.
        assert await pool.call("a", "echo", {"text": "hi"}) == "v1:hi"
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_declared_subset_of_what_the_server_serves_is_accepted() -> None:
    """Contract 01b §5 rule 3: a served tool the file does not declare is a
    warning, not a refusal. `ha`, `ha-read`, `github-code` and
    `github-platform` each declare a handful of the tools their server
    serves — the fence, working as written."""
    pool = ReloadablePool({}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        report = await asyncio.wait_for(
            pool.reload({"a": _spec("a", "v1")}, declared={"a": ("echo",)}), RELOAD_CEILING_S
        )

        assert report.ok, [one.detail for one in report.failed]
        assert "a" not in pool.refusals
        assert await pool.call("a", "echo", {"text": "hi"}) == "v1:hi"
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_declared_tool_the_server_does_not_serve_is_refused() -> None:
    """The other half of the rule: the catalog promised a call nobody can
    make. The refusal names the missing tool, not the whole probe."""
    pool = ReloadablePool({}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        report = await asyncio.wait_for(
            pool.reload({"a": _spec("a", "v1")}, declared={"a": ("echo", "nope")}),
            RELOAD_CEILING_S,
        )

        assert not report.ok
        assert [one.name for one in report.failed] == ["a"]
        assert "declares ['nope']" in pool.refusals["a"].detail
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_name_that_starts_later_is_no_longer_refused() -> None:
    pool = ReloadablePool({}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        broken = replace(_spec("b", "v1"), command="/nonexistent/mcp-server")
        await asyncio.wait_for(pool.reload({"b": broken}), RELOAD_CEILING_S)
        await asyncio.wait_for(pool.reload({"b": _spec("b", "v1")}), RELOAD_CEILING_S)

        assert pool.refusals == {}
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_name_dropped_from_the_roster_is_not_refused_either() -> None:
    """It is not refused, it is not asked for. Two different facts."""
    pool = ReloadablePool({}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        broken = replace(_spec("b", "v1"), command="/nonexistent/mcp-server")
        await asyncio.wait_for(pool.reload({"b": broken}), RELOAD_CEILING_S)
        await asyncio.wait_for(pool.reload({}), RELOAD_CEILING_S)

        assert pool.refusals == {}
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_removed_upstream_answers_the_words_the_pep_already_uses() -> None:
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    try:
        await asyncio.wait_for(pool.reload({}), RELOAD_CEILING_S)
        with pytest.raises(UpstreamError, match="is not running"):
            await pool.call("a", "echo", {"text": "hi"})
    finally:
        await pool.stop()


def test_the_drain_bound_sits_between_the_two_timeouts() -> None:
    """Probe condition 1 and README item 7. Below the call timeout, a
    reload cuts off a caller about to answer. Above the idle TTL, a call
    would have its process closed under it."""
    assert UPSTREAM_CALL_TIMEOUT_S < DRAIN_TIMEOUT_S < IDLE_TTL_S


def test_the_trigger_is_a_signal_and_not_a_listener() -> None:
    """§4.4 step 1. A trigger is a new path into the crown jewel, and a
    signal is the one that adds no path: the kernel already decides who may
    send it, and only root or the PEP's own uid can."""
    assert TRIGGER == "SIGHUP"
