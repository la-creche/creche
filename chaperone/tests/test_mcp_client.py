from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from chaperone.fences import ArgDeny
from chaperone.mcp_client import (
    IDLE_TTL_S,
    StdioUpstreamPool,
    UpstreamError,
    UpstreamSpec,
    load_upstreams,
    resolve_env,
)
from chaperone_helpers import AS_TEST_USER

STUB = Path(__file__).parent / "stub_mcp_server.py"

#: How long an idle close may take before the test calls it a failure, and how
#: often to look. Generous: the point is to outlast a loaded machine, not to
#: measure the TTL — the TTL each test sets is what bounds the real wait.
IDLE_CLOSE_DEADLINE_S = 30.0
IDLE_CLOSE_POLL_S = 0.01


def test_load_upstreams_parses_the_committed_file() -> None:
    """The committed base roster holds no row. Every server is
    an `mcp/<name>/server.yaml` in agent-registry, and the roster root
    writes from those files is the only source (`stage7-releases.md` §4.4).
    The server rows (kagi's command, the name split, the `vikunja-*` env
    rule, github's token and toolsets) are that registry's files, checked
    by its CI and by root's reader.
    `test_chaperone_ret_roster_only.py` holds the rule."""
    committed = Path(__file__).parent.parent / "upstreams.yaml"

    assert load_upstreams(committed) == {}


def test_load_upstreams_parses_an_arg_deny(tmp_path: Path) -> None:
    # The fence `github-code` and `github-platform` carry on `main`, in the
    # shape root writes it into the roster: the write half of their verbs
    # may not name main or master. from_branch is not fenced — branching
    # FROM main is the point.
    roster = tmp_path / "upstreams.yaml"
    roster.write_text(
        "github-code:\n"
        "  command: /opt/mcp/github-code/bin/github-mcp-server\n"
        "  arg_denies:\n"
        "    - tools: [push_files, create_or_update_file, delete_file, create_branch,\n"
        "              update_pull_request_branch]\n"
        "      arg: branch\n"
        "      values: [main, master]\n"
        "kagi:\n"
        "  command: /opt/mcp/kagi/bin/kagimcp\n",
        encoding="utf-8",
    )
    specs = load_upstreams(roster)
    assert specs["github-code"].arg_denies == (
        ArgDeny(
            tools=frozenset(
                {
                    "push_files",
                    "create_or_update_file",
                    "delete_file",
                    "create_branch",
                    "update_pull_request_branch",
                }
            ),
            arg="branch",
            values=frozenset({"main", "master"}),
        ),
    )
    assert specs["kagi"].arg_denies == ()  # the key is optional; absent = no fence


def test_load_upstreams_refuses_bad_shapes(tmp_path: Path) -> None:
    for bad in (
        "- a list\n",
        "kagi: not-a-mapping\n",
        "kagi: {args: []}\n",  # no command
        "kagi: {command: x, args: [1]}\n",
        "kagi: {command: x, env: [a]}\n",
        "kagi: {command: x, surprise: y}\n",
        "kagi: {command: x, arg_denies: nope}\n",
        "kagi: {command: x, arg_denies: [nope]}\n",
        "kagi: {command: x, arg_denies: [{tools: [a], arg: b}]}\n",  # no values
        "kagi: {command: x, arg_denies: [{tools: [], arg: b, values: [c]}]}\n",
        "kagi: {command: x, arg_denies: [{tools: [a], arg: b, values: []}]}\n",
        "kagi: {command: x, arg_denies: [{tools: [1], arg: b, values: [c]}]}\n",
        "kagi: {command: x, arg_denies: [{tools: [a], arg: 1, values: [c]}]}\n",
        "kagi: {command: x, arg_denies: [{tools: [a], arg: '', values: [c]}]}\n",
        "kagi: {command: x, arg_denies: [{tools: [a], arg: b, values: [c], extra: d}]}\n",
    ):
        path = tmp_path / "u.yaml"
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(UpstreamError):
            load_upstreams(path)


def test_resolve_env_secret_references() -> None:
    spec = UpstreamSpec(name="s", command="x", args=(), env={"A": "secret:alpha", "B": "literal"})
    assert resolve_env(spec, {"alpha": "value"}) == {"A": "value", "B": "literal"}
    with pytest.raises(UpstreamError, match="missing secret"):
        resolve_env(spec, {})


@pytest.mark.slow
async def test_stdio_roundtrip_with_env_delivery() -> None:
    specs = {
        "stub": UpstreamSpec(
            name="stub",
            command=sys.executable,
            args=(str(STUB),),
            env={"STUB_SECRET": "secret:stub_secret"},
        )
    }
    pool = StdioUpstreamPool(specs, {"stub_secret": "s3cret"}, launcher=AS_TEST_USER)
    await pool.start()
    try:
        tools = pool.tools("stub")
        assert "echo" in tools
        assert tools["echo"].input_schema["type"] == "object"
        # The probe keeps the upstream's hints, wire-shaped; none stays None.
        assert tools["echo"].annotations == {"readOnlyHint": True}
        assert tools["echo"].write is False
        assert tools["sleep"].annotations is None
        assert tools["sleep"].write is None
        result = await pool.call("stub", "echo", {"text": "hi"})
        assert result == "s3cret:hi"
    finally:
        await pool.stop()


@pytest.mark.slow
async def test_broken_upstream_is_tolerated() -> None:
    specs = {
        "broken": UpstreamSpec(
            name="broken", command="/nonexistent/definitely-not-a-binary", args=(), env={}
        )
    }
    pool = StdioUpstreamPool(specs, {}, launcher=AS_TEST_USER)
    await pool.start()
    try:
        assert pool.tools("broken") == {}
        with pytest.raises(UpstreamError, match="not running"):
            await pool.call("broken", "anything", {})
    finally:
        await pool.stop()


@pytest.mark.slow
async def test_missing_secret_skips_upstream_not_crash() -> None:
    specs = {
        "needy": UpstreamSpec(
            name="needy",
            command=sys.executable,
            args=(str(STUB),),
            env={"STUB_SECRET": "secret:absent"},
        )
    }
    pool = StdioUpstreamPool(specs, {}, launcher=AS_TEST_USER)  # no secrets at all
    await pool.start()  # must not raise: fail closed per upstream
    try:
        assert pool.tools("needy") == {}
        with pytest.raises(UpstreamError, match="not running"):
            await pool.call("needy", "echo", {})
    finally:
        await pool.stop()


# ---- lazy spawn ----------------------------------------------------------
#
# The property under test is that a probe at boot decides what /manifest
# publishes, while a *process* exists only while it is being used. The stub's
# STUB_TRACE file counts processes, so these assert what actually got spawned
# rather than inferring it from timing.


#: How long the idle test lets an unused upstream live, and how often its
#: owner task looks. Never a TTL a CALL runs under.
SHORT_TTL_S = 0.1

#: How long a lingering stub outlives its stdin. Below the SDK's 2 s grace, so
#: the stub exits on its own and is never terminated.
LINGER_S = 1.0


def _traced(
    tmp_path: Path, ttl: float = IDLE_TTL_S, linger_s: float = 0.0
) -> tuple[StdioUpstreamPool, Path]:
    trace = tmp_path / "pids"
    specs = {
        "stub": UpstreamSpec(
            name="stub",
            command=sys.executable,
            args=(str(STUB),),
            env={
                "STUB_SECRET": "secret:stub_secret",
                "STUB_TRACE": str(trace),
                "STUB_LINGER_S": str(linger_s),
            },
        )
    }
    return StdioUpstreamPool(specs, {"stub_secret": "s3cret"}, ttl, AS_TEST_USER), trace


def _pids(trace: Path) -> list[str]:
    return trace.read_text(encoding="utf-8").split() if trace.exists() else []


async def _await_idle_close(pool: StdioUpstreamPool, name: str) -> None:
    """Wait until the owner task has actually closed the idle upstream.

    The close is the owner task's work, so a fixed sleep only bets that it got
    scheduled in time. Under `pytest -n auto` that bet loses on a loaded
    machine. Poll the pool's own record of what is live instead, with a
    deadline far past any TTL a test sets.
    """
    deadline = time.monotonic() + IDLE_CLOSE_DEADLINE_S
    # `_live` is the close's only witness: the trace file only ever grows.
    while name in pool._live:
        assert time.monotonic() < deadline, f"upstream {name!r} was never closed"
        await asyncio.sleep(IDLE_CLOSE_POLL_S)


@pytest.mark.slow
async def test_the_boot_probe_caches_tools_then_lets_the_process_go(tmp_path: Path) -> None:
    pool, trace = _traced(tmp_path)
    await pool.start()
    try:
        # The tools /manifest needs survive the probe...
        assert "echo" in pool.tools("stub")
        # ...and exactly one process paid for them, which is no longer running.
        assert len(_pids(trace)) == 1
    finally:
        await pool.stop()


@pytest.mark.slow
async def test_a_call_after_the_probe_spawns_again_and_works(tmp_path: Path) -> None:
    pool, trace = _traced(tmp_path)
    await pool.start()
    try:
        assert await pool.call("stub", "echo", {"text": "hi"}) == "s3cret:hi"
        assert len(_pids(trace)) == 2, "the probe's process should not have been reused"
    finally:
        await pool.stop()


@pytest.mark.slow
async def test_a_burst_of_concurrent_calls_spawns_one_process(tmp_path: Path) -> None:
    # Two calls arriving at a cold upstream together must not race into two
    # processes, each holding the same credential.
    pool, trace = _traced(tmp_path)
    await pool.start()
    try:
        results = await asyncio.gather(
            *(pool.call("stub", "echo", {"text": str(n)}) for n in range(4))
        )
        assert sorted(results) == ["s3cret:0", "s3cret:1", "s3cret:2", "s3cret:3"]
        assert len(_pids(trace)) == 2, "probe plus exactly one spawn"
    finally:
        await pool.stop()


@pytest.mark.slow
async def test_an_idle_upstream_is_closed_and_respawned_on_the_next_call(
    tmp_path: Path,
) -> None:
    # A process must outlive the call that uses it. With the pool built at
    # ttl=0.1, a loaded machine needs longer than that to answer one echo, so
    # the owner task closed the process MID-CALL and the call read "Connection
    # closed", on the first call or on the respawn. Production cannot do this:
    # IDLE_TTL_S (300 s) is far above UPSTREAM_CALL_TIMEOUT_S (60 s). So both
    # calls run at the production TTL, and only the wait between them is short.
    pool, trace = _traced(tmp_path)
    pool._poll_s = SHORT_TTL_S
    await pool.start()
    try:
        await pool.call("stub", "echo", {"text": "first"})
        spawned = _pids(trace)

        pool._idle_ttl_s = SHORT_TTL_S
        await _await_idle_close(pool, "stub")
        pool._idle_ttl_s = IDLE_TTL_S

        assert await pool.call("stub", "echo", {"text": "second"}) == "s3cret:second"
        assert len(_pids(trace)) == len(spawned) + 1, "the idle process should have been closed"
    finally:
        await pool.stop()


def _on_close(
    pool: StdioUpstreamPool, monkeypatch: pytest.MonkeyPatch
) -> asyncio.Future[asyncio.Task[Any] | None]:
    """Resolve to the first owner task that decides to close its upstream."""
    until_idle = pool._until_idle
    closing: asyncio.Future[asyncio.Task[Any] | None] = asyncio.get_running_loop().create_future()

    async def spy(name: str) -> None:
        await until_idle(name)
        if not closing.done():
            closing.set_result(asyncio.current_task())

    monkeypatch.setattr(pool, "_until_idle", spy)
    return closing


async def _into_close(
    pool: StdioUpstreamPool, closing: asyncio.Future[asyncio.Task[Any] | None]
) -> asyncio.Task[Any]:
    """Let the live upstream go idle. Return its owner, still closing."""
    pool._idle_ttl_s = SHORT_TTL_S
    owner = await asyncio.wait_for(closing, IDLE_CLOSE_DEADLINE_S)
    pool._idle_ttl_s = IDLE_TTL_S

    assert owner is not None and not owner.done(), "the close must still be running"
    return owner


@pytest.mark.slow
async def test_a_call_during_an_idle_close_spawns_a_fresh_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An idle close takes up to ~2 s: stdin EOF, then the wait for the process
    # to exit. A call landing in it must not take the closing session and
    # fail on "Connection closed". The lingering stub holds the window open.
    pool, trace = _traced(tmp_path, linger_s=LINGER_S)
    pool._poll_s = SHORT_TTL_S
    closing = _on_close(pool, monkeypatch)
    await pool.start()
    try:
        await pool.call("stub", "echo", {"text": "first"})
        spawned = _pids(trace)

        owner = await _into_close(pool, closing)
        assert await pool.call("stub", "echo", {"text": "second"}) == "s3cret:second"
        assert len(_pids(trace)) == len(spawned) + 1, "the closing process must not be reused"

        # The closed owner's cleanup must leave its successor's session alone.
        await owner
        assert await pool.call("stub", "echo", {"text": "third"}) == "s3cret:third"
        assert len(_pids(trace)) == len(spawned) + 1, "the successor must outlive its predecessor"
    finally:
        await pool.stop()


@pytest.mark.slow
async def test_stop_awaits_an_owner_that_is_still_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A successor that fails to start must not drop its closing predecessor
    # from the owners stop() awaits, or stop() returns with a process alive.
    pool, _ = _traced(tmp_path, linger_s=LINGER_S)
    pool._poll_s = SHORT_TTL_S
    closing = _on_close(pool, monkeypatch)
    await pool.start()
    try:
        await pool.call("stub", "echo", {"text": "first"})
        owner = await _into_close(pool, closing)

        pool._specs["stub"] = replace(pool._specs["stub"], command="/nonexistent/binary")
        with pytest.raises(UpstreamError, match="failed to start"):
            await pool.call("stub", "echo", {"text": "second"})
    finally:
        await pool.stop()

    assert owner.done(), "stop() returned with an owner still closing"


@pytest.mark.slow
async def test_a_probe_failure_still_reads_as_not_running(tmp_path: Path) -> None:
    # Fail-closed did not move: an upstream that could not start at boot is never
    # retried per call, so a broken pin cannot turn into a spawn storm.
    specs = {
        "broken": UpstreamSpec(
            name="broken", command="/nonexistent/definitely-not-a-binary", args=(), env={}
        )
    }
    pool = StdioUpstreamPool(specs, {}, launcher=AS_TEST_USER)
    await pool.start()
    try:
        assert pool.tools("broken") == {}
        with pytest.raises(UpstreamError, match="not running"):
            await pool.call("broken", "echo", {})
    finally:
        await pool.stop()
