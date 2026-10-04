"""A reload reads off the loop thread, and no read stops the trigger.

`reload_wiring` assumption 9 and assumption 2. The tests here start no MCP
server: the roster is empty, so a reload has nothing to spawn.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Final

import pytest
from chaperone.mcp_client import UpstreamSpec
from chaperone.reload_pool import ReloadablePool
from chaperone.reload_wiring import Credentials, ReloadTrigger, RosterSource, RosterState

from chaperone import reload_wiring

#: How long the stand-in for `sops` waits for the test to let it go on.
BLOCKED_READ_S: Final = 2.0
CEILING_S: Final = 10.0

IN_MEMORY: Final = {"old_key": "v1"}


def _trigger(tmp_path: Path) -> ReloadTrigger:
    roster = tmp_path / "upstreams.yaml"
    roster.write_text("{}\n", encoding="utf-8")
    sealed = tmp_path / "secrets.enc.yaml"
    sealed.write_text("sealed\n", encoding="utf-8")
    pool = ReloadablePool({}, dict(IN_MEMORY))

    return ReloadTrigger(pool, RosterSource(upstreams_file=roster, secrets_file=sealed))


async def test_a_reload_decrypts_off_the_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sops -d` can take seconds. The loop holds every call, so the read
    runs on another thread and the loop goes on while the read waits."""
    reading = threading.Event()
    go_on = threading.Event()
    readers: list[int] = []

    def slow_sops(_path: Path) -> dict[str, str]:
        readers.append(threading.get_ident())
        reading.set()
        go_on.wait(BLOCKED_READ_S)

        return {"new_key": "v2"}

    monkeypatch.setattr(reload_wiring, "load_sops_secrets", slow_sops)
    trigger = _trigger(tmp_path)

    reload = asyncio.create_task(trigger.reload_once())
    try:
        async with asyncio.timeout(CEILING_S):
            while not reading.is_set():
                await asyncio.sleep(0.01)

        the_loop_ran_during_the_read = not reload.done()
    finally:
        go_on.set()

    report = await asyncio.wait_for(reload, CEILING_S)

    assert the_loop_ran_during_the_read
    assert len(readers) == 1
    assert readers[0] != threading.get_ident()
    assert report is not None
    assert trigger.pool.secrets == {"new_key": "v2"}


async def test_the_first_read_is_off_the_loop_thread_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start reads the roster files and decrypts nothing. A file read
    blocks too, so it takes the same path."""
    readers: list[int] = []
    load = reload_wiring.load_upstreams

    def seen(path: Path) -> dict[str, UpstreamSpec]:
        readers.append(threading.get_ident())

        return load(path)

    monkeypatch.setattr(reload_wiring, "load_upstreams", seen)
    trigger = _trigger(tmp_path)

    await asyncio.wait_for(trigger.start(), CEILING_S)

    assert len(readers) == 1
    assert readers[0] != threading.get_ident()
    assert trigger.health.state is RosterState.OK
    assert trigger.pool.secrets == IN_MEMORY


@pytest.mark.parametrize("credentials", [Credentials.DECRYPT, Credentials.IN_MEMORY])
async def test_a_read_that_raises_anything_keeps_the_set_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    credentials: Credentials,
) -> None:
    """Assumption 2, for a failure that `UNREADABLE` does not name. The
    reload is one log line with the type of the failure and never its text:
    the text can quote a decrypted line."""

    def explode(_path: Path) -> dict[str, UpstreamSpec]:
        raise RuntimeError("line 3: not-for-a-log")

    monkeypatch.setattr(reload_wiring, "load_upstreams", explode)
    trigger = _trigger(tmp_path)

    with caplog.at_level(logging.ERROR, logger="chaperone.reload"):
        report = await asyncio.wait_for(trigger.reload_once(credentials), CEILING_S)

    assert report is None
    assert trigger.health.state is RosterState.UNREADABLE
    assert trigger.pool.secrets == IN_MEMORY
    assert "RuntimeError" in caplog.text
    assert "not-for-a-log" not in caplog.text


async def test_a_first_read_that_raises_anything_does_not_stop_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assumption 8. The start is a reload, so it never raises into the
    lifespan: the chaperone starts with no MCP server and serves its verbs."""

    def explode(_path: Path) -> dict[str, UpstreamSpec]:
        raise RuntimeError("a failure nobody predicted")

    monkeypatch.setattr(reload_wiring, "load_upstreams", explode)
    trigger = _trigger(tmp_path)

    await asyncio.wait_for(trigger.start(), CEILING_S)

    assert trigger.health.state is RosterState.UNREADABLE
    assert trigger.pool.live_specs == {}
