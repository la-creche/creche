"""The trigger, and the pool `app.py` actually holds.

`reload_pool`'s own tests prove the POOL is safe to reload. These prove the
PEP reloads:
the roster file is re-read, the credentials are re-decrypted, a broken
roster leaves the old set serving, and a call in flight on an untouched
upstream finishes across the whole wiring.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml
from chaperone.app import PepConfig, create_app
from chaperone.mcp_client import UpstreamSpec, load_upstreams
from chaperone.reload_pool import ReloadablePool
from chaperone.reload_wiring import TRIGGER_SIGNAL, ReloadTrigger, RosterSource, install
from chaperone_helpers import UNCHECKED, pools
from fastapi.testclient import TestClient

pytestmark = pytest.mark.slow

STUB: Final = Path(__file__).resolve().parent / "stub_mcp_server.py"

IN_FLIGHT_S: Final = 1.0
RELOAD_CEILING_S: Final = 10.0


def _entry(version: str, tools: list[str] | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "command": sys.executable,
        "args": [str(STUB)],
        "env": {"STUB_SECRET": version},
    }
    if tools is not None:
        body["tools"] = tools

    return body


def _write_roster(path: Path, names: dict[str, dict[str, object]]) -> None:
    path.write_text(yaml.safe_dump(names), encoding="utf-8")


def _spec(name: str, version: str) -> UpstreamSpec:
    return UpstreamSpec(
        name=name, command=sys.executable, args=(str(STUB),), env={"STUB_SECRET": version}
    )


# -- the roster file ------------------------------------------------------


def test_the_roster_carries_the_declared_tool_list(tmp_path: Path) -> None:
    """Probe README item 6, decided: `UpstreamSpec` grows a `tools` field.

    Root copies contract 01b's closed set into the roster when it writes it,
    so the PEP has ONE input file and §4.4 step 2 can refuse an upstream
    whose probe disagrees with its own definition."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1", ["echo", "sleep"])})

    specs = load_upstreams(roster)

    assert specs["a"].tools == ("echo", "sleep")


def test_a_roster_with_no_tools_list_declares_none(tmp_path: Path) -> None:
    """No `tools` list: the probe decides alone."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})

    assert load_upstreams(roster)["a"].tools == ()


def test_a_tools_key_that_is_not_a_list_of_strings_is_refused(tmp_path: Path) -> None:
    roster = tmp_path / "upstreams.yaml"
    roster.write_text("a:\n  command: /bin/true\n  tools: {echo: yes}\n", encoding="utf-8")

    with pytest.raises(Exception, match="tools must be a list of strings"):
        load_upstreams(roster)


# -- the trigger ----------------------------------------------------------


@pytest.mark.anyio
async def test_a_reload_adds_the_server_the_roster_gained(tmp_path: Path) -> None:
    """§4.1 step 6, end to end: root writes the file, the PEP reloads, and
    the new upstream answers with no restart."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        _write_roster(roster, {"a": _entry("v1"), "b": _entry("v1")})

        report = await asyncio.wait_for(trigger.reload_once(), RELOAD_CEILING_S)

        assert report is not None
        assert report.added == ("b",)
        assert report.unchanged == ("a",)
        assert await pool.call("b", "echo", {"text": "hi"}) == "v1:hi"
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_call_in_flight_on_an_untouched_upstream_finishes(tmp_path: Path) -> None:
    """The §3 gate row, driven through the wiring rather than the pool."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        in_flight = asyncio.create_task(pool.call("a", "sleep", {"seconds": IN_FLIGHT_S}))
        await asyncio.sleep(0.05)
        _write_roster(roster, {"a": _entry("v1"), "b": _entry("v1")})

        await asyncio.wait_for(trigger.reload_once(), RELOAD_CEILING_S)

        assert await asyncio.wait_for(in_flight, RELOAD_CEILING_S)
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_an_unreadable_roster_leaves_the_old_set_serving(tmp_path: Path) -> None:
    """Assumption 2. A reload that fails is a log line, never a pool with
    nothing in it."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        roster.write_text("a: [this is not a mapping]\n", encoding="utf-8")

        assert await trigger.reload_once() is None
        assert await pool.call("a", "echo", {"text": "hi"}) == "v1:hi"
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_a_roster_that_vanished_leaves_the_old_set_serving(tmp_path: Path) -> None:
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        roster.unlink()

        assert await trigger.reload_once() is None
        assert "a" in pool.live_specs
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_an_upstream_that_does_not_serve_what_it_declares_is_refused(
    tmp_path: Path,
) -> None:
    """§4.4 step 2, through the roster file. `a` keeps serving."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        _write_roster(roster, {"a": _entry("v1"), "b": _entry("v1", ["echo", "not_a_tool"])})

        report = await asyncio.wait_for(trigger.reload_once(), RELOAD_CEILING_S)

        assert report is not None
        assert [one.name for one in report.failed] == ["b"]
        assert "b" in pool.refusals
        assert await pool.call("a", "echo", {"text": "hi"}) == "v1:hi"
    finally:
        await pool.stop()


# -- the secrets ----------------------------------------------------------


def _sealed(directory: Path, name: str, value: str) -> Path:
    """One `<name>.enc`, as the intake writes it. `sops` is faked by the
    stub on PATH: the point of these tests is WHICH files are opened."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.enc"
    path.write_text(value, encoding="utf-8")

    return path


@pytest.fixture
def fake_sops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `sops` that prints the file it is asked to decrypt.

    The real one needs an age key this machine does not have. What these
    tests check is the LAYOUT the PEP reads, not sops itself.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "sops"
    stub.write_text('#!/bin/sh\nexec cat "$2"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")

    return binaries


@pytest.mark.usefixtures("fake_sops")
def test_the_reload_reads_the_per_secret_store(tmp_path: Path) -> None:
    """The value the operator pastes lands in
    `/var/lib/agent-release/secrets/<name>.enc`, which is the only
    layout the host can author, so the reload reads it as well as the
    monolith."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    store = tmp_path / "secrets"
    _sealed(store, "kagi_api_key", "pasted-by-operator")

    found = RosterSource(upstreams_file=roster, secrets_dir=store).read({})

    assert found.secrets == {"kagi_api_key": "pasted-by-operator"}


@pytest.mark.usefixtures("fake_sops")
def test_a_pasted_value_wins_over_the_monolith(tmp_path: Path) -> None:
    """A name in both was pasted after the monolith was authored, and the
    paste is the newer intent."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    monolith = tmp_path / "secrets.enc.yaml"
    monolith.write_text("kagi_api_key: the-old-one\nother: kept\n", encoding="utf-8")
    store = tmp_path / "secrets"
    _sealed(store, "kagi_api_key", "pasted-by-operator")

    found = RosterSource(upstreams_file=roster, secrets_file=monolith, secrets_dir=store).read({})

    assert found.secrets == {"kagi_api_key": "pasted-by-operator", "other": "kept"}


@pytest.mark.usefixtures("fake_sops")
def test_one_unreadable_secret_file_costs_one_credential(tmp_path: Path) -> None:
    """Contract 01b §4.1 rule 4, applied to the loader: a server whose
    secret will not decrypt fails closed alone."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    store = tmp_path / "secrets"
    _sealed(store, "good_key", "kept")
    broken = store / "bad_key.enc"
    broken.write_text("x", encoding="utf-8")
    broken.chmod(0o000)

    found = RosterSource(upstreams_file=roster, secrets_dir=store).read({})

    broken.chmod(0o600)
    assert found.secrets == {"good_key": "kept"}


@pytest.mark.usefixtures("fake_sops")
def test_a_store_that_does_not_exist_yet_is_not_an_error(tmp_path: Path) -> None:
    """The state before the first paste. Every upstream with a credential
    in the monolith keeps working."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})

    found = RosterSource(upstreams_file=roster, secrets_dir=tmp_path / "absent").read({})

    assert found.secrets == {}


@pytest.mark.usefixtures("fake_sops")
def test_a_symlinked_secret_file_is_not_decrypted(tmp_path: Path) -> None:
    """A symlink would point `sops` at a file outside the store."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("not a secret of ours", encoding="utf-8")
    store = tmp_path / "secrets"
    store.mkdir()
    (store / "sneaky.enc").symlink_to(elsewhere)

    found = RosterSource(upstreams_file=roster, secrets_dir=store).read({})

    assert found.secrets == {}


@pytest.mark.usefixtures("fake_sops")
def test_a_file_name_outside_the_secret_pattern_is_skipped(tmp_path: Path) -> None:
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    store = tmp_path / "secrets"
    store.mkdir()
    (store / "Bad-Name.enc").write_text("x", encoding="utf-8")
    (store / "notes.txt").write_text("x", encoding="utf-8")

    found = RosterSource(upstreams_file=roster, secrets_dir=store).read({})

    assert found.secrets == {}


@pytest.mark.anyio
async def test_a_failed_decryption_keeps_the_credentials_in_memory(tmp_path: Path) -> None:
    """Assumption 4's second half. `load_sops_secrets` shells out to `sops`,
    which is absent here, so this exercises the real failure path."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    sealed = tmp_path / "secrets.enc.yaml"
    sealed.write_text("not really sops\n", encoding="utf-8")
    pool = ReloadablePool(
        {"a": _spec("a", "v1")}, {"kagi_api_key": "kept"}, make_pool=pools(UNCHECKED)
    )
    await pool.start()
    source = RosterSource(upstreams_file=roster, secrets_file=sealed)
    try:
        assert source.read(pool.secrets).secrets == {"kagi_api_key": "kept"}
    finally:
        await pool.stop()


# -- the signal -----------------------------------------------------------


@pytest.mark.anyio
async def test_the_signal_runs_one_reload_at_a_time(tmp_path: Path) -> None:
    """Assumption 3. Three signals while one reload runs cost one more
    pass, not three tasks swapping against three readings."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        for _ in range(4):
            trigger.signal_arrived()

        await asyncio.sleep(0)
        while any(not task.done() for task in asyncio.all_tasks() - {asyncio.current_task()}):
            await asyncio.sleep(0.05)

        assert len(trigger.reports) <= 2
        assert "a" in pool.live_specs
    finally:
        await pool.stop()


def test_the_app_holds_a_reloadable_pool_when_a_roster_is_configured(tmp_path: Path) -> None:
    """`TestClient` runs the loop in a worker thread, which cannot take a
    signal handler. That is assumption 7's case, and the PEP still boots:
    the trigger is on `app.state` and callable.
    """
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    app = create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            upstreams=load_upstreams(roster),
            roster=RosterSource(upstreams_file=roster),
        )
    )
    with TestClient(app):
        assert isinstance(app.state.reload_trigger, ReloadTrigger)
        assert isinstance(app.state.reload_trigger.pool, ReloadablePool)


def test_the_app_keeps_the_boot_pool_when_no_roster_is_configured(tmp_path: Path) -> None:
    """No roster: the PEP keeps the boot-time pool and installs no trigger."""
    app = create_app(PepConfig(audit_dir=tmp_path / "audit", rework_dir=tmp_path / "rework"))

    with TestClient(app):
        assert not hasattr(app.state, "reload_trigger")


@pytest.mark.anyio
async def test_the_handler_is_installed_on_the_running_loop(tmp_path: Path) -> None:
    """The seam, closed. A real `SIGHUP` to this process reloads the pool."""
    roster = tmp_path / "upstreams.yaml"
    _write_roster(roster, {"a": _entry("v1")})
    pool = ReloadablePool({"a": _spec("a", "v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger, remove = install(pool, RosterSource(upstreams_file=roster))
    try:
        assert TRIGGER_SIGNAL is signal.SIGHUP
        _write_roster(roster, {"a": _entry("v1"), "b": _entry("v1")})
        signal.raise_signal(signal.SIGHUP)

        for _ in range(200):
            await asyncio.sleep(0.05)
            if trigger.reports:
                break

        assert trigger.reports and trigger.reports[-1].added == ("b",)
    finally:
        remove()
        await pool.stop()
