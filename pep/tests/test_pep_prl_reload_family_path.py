"""A reload moves what the family path reads, and no roster stops the PEP.

Two readers of the roster must not disagree about time. If the family path
served the names and applied the fences the process started with, a server
a release added would be probed and serving in the pool and answer 501 on
the family path until a restart, and a fence a release added would apply
only from the next start: a denied value would answer 200 after the reload
that carried its fence.

One reader. Every decision asks the pool that serves for the names and
fences of the rows it serves:

    roster --SIGHUP--> ReloadablePool._live --+--> names + fences --> decide_family
                                              `--> the process -----> the call

And a roster that will not parse does not stop the process at start. It
is what it is at a reload: one log line, the set serving before it keeps
serving (none, at start), `/healthz` says so, and the next signal tries
again.

The one-reader cases drive `agent_pep.__main__.main()`, the real reload and
real stdio children (`stub_mcp_server.py`). Every child starts through
`fake_run_as.py`, the real launcher with the switch faked, as
`test_pep_run_as_spawn.py` starts its own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
import pytest
import yaml
from agent_pep import __main__ as entry
from agent_pep import mcp_client
from agent_pep.app import PepConfig, create_app
from agent_pep.family_ids import MCP_TOOL_SEPARATOR
from agent_pep.mcp_client import UpstreamSpec
from agent_pep.reload_pool import ReloadablePool, ReloadReport
from agent_pep.reload_wiring import ReloadTrigger, RosterSource
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pep_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from pep_helpers import AS_TEST_USER, UNCHECKED, pools

pytestmark = pytest.mark.slow

STUB: Final = Path(__file__).resolve().parent / "stub_mcp_server.py"

#: A name the launcher's pattern admits, so the checked fake starts it.
SERVER: Final = "stub"
ECHO: Final = f"{SERVER}{MCP_TOOL_SEPARATOR}echo"

#: The value every fence below refuses, and a value none of them refuses.
DENIED: Final = "main"
ADMITTED: Final = "dev"

#: A command no host has. Its probe fails, so its row is refused.
MISSING: Final = "/nonexistent/mcp-server"

#: A ceiling on one wait, not a measurement. It catches a hang.
CEILING_S: Final = 10.0
POLL_S: Final = 0.02

AUTH: Final = {"Authorization": f"Bearer {FAMILY_TOKEN}"}

HTTP_OK: Final = 200
HTTP_BAD_REQUEST: Final = 400
HTTP_NOT_IMPLEMENTED: Final = 501

#: Every variable `__main__` reads beyond the four a case sets. Unset, so a
#: developer's shell cannot turn a door or a second roster on under a test.
OTHER_ENV: Final = (
    "PEP_UPSTREAMS_GENERATED",
    "PEP_SECRETS",
    "PEP_SECRETS_DIR",
    "PEP_SESSIOND_SOCKET",
    "PEP_DELEGATE_TOKEN_FILE",
    "PEP_DISPATCH_TOKEN_FILE",
    "PEP_RELEASE_REQUESTS_DIR",
    "PEP_APPROVAL_URL",
)

#: The line `reload_once` logs for a roster it cannot read.
UNREADABLE_LINE: Final = "the roster is unreadable"

#: `/healthz`'s `roster.state`, spelled as a reader of the route spells it.
OFF: Final = "off"
OK: Final = "ok"
UNREADABLE: Final = "unreadable"


def _row(
    version: str = "v1",
    *,
    denies: tuple[str, ...] = (),
    command: str = sys.executable,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """One roster row for the stub, in the shape root writes. `denies`
    fences `echo`'s `text` (contract 01b §7, `arg_denies`). The stub echoes
    `STUB_SECRET`, so an answer names the row that produced it."""
    row: dict[str, object] = {
        "command": command,
        "args": [str(STUB)],
        "env": {"STUB_SECRET": version, **(env or {})},
    }
    if denies:
        row["arg_denies"] = [{"tools": ["echo"], "arg": "text", "values": list(denies)}]

    return row


def _write(path: Path, rows: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(rows), encoding="utf-8")

    return path


@pytest.fixture(autouse=True)
def _fake_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pool the app builds starts its children through `fake_run_as.py`.
    The real launcher needs CAP_SETUID and an `mcp-stub` user."""
    monkeypatch.setattr(mcp_client, "installed_launcher", lambda: AS_TEST_USER)


def _main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base: Path, generated: Path | None = None
) -> FastAPI:
    """`agent_pep.__main__.main()` up to the server: the app a host serves."""
    for name in OTHER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PEP_REWORK_DIR", str(tmp_path / "rework"))
    monkeypatch.setenv("PEP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("PEP_UPSTREAMS", str(base))
    if generated is not None:
        monkeypatch.setenv("PEP_UPSTREAMS_GENERATED", str(generated))

    served: list[FastAPI] = []

    def serve(app: FastAPI, **_kwargs: object) -> None:
        served.append(app)

    monkeypatch.setattr(entry.uvicorn, "run", serve)
    # `main` quiets two loggers for the whole process. Put them back, so no
    # later test in this worker inherits the change.
    quieted = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        assert entry.main() == 0
    finally:
        for name, level in quieted.items():
            logging.getLogger(name).setLevel(level)

    return served[0]


@dataclass
class _Pep:
    """One running PEP, its roster file, and family `chat` granted `stub__echo`."""

    client: TestClient
    app: FastAPI
    roster: Path

    def call(self, text: str) -> httpx.Response:
        return self.client.post("/call", json={"tool": ECHO, "args": {"text": text}}, headers=AUTH)

    def offered(self) -> list[str]:
        tools = self.client.get("/manifest", headers=AUTH).json()["tools"]

        return [one["name"] for one in tools]

    def roster_state(self) -> dict[str, object]:
        return self.client.get("/healthz").json()["roster"]

    def reload(self, rows: dict[str, object] | None = None) -> ReloadReport | None:
        """Root writes the roster, then signals. `reload_once` is what the
        `SIGHUP` handler runs, on the app's own loop."""
        if rows is not None:
            _write(self.roster, rows)

        return self._portal().call(self.app.state.reload_trigger.reload_once)

    def reload_soon(self, rows: dict[str, object]) -> Future[ReloadReport | None]:
        """The same, left running. The caller waits on the returned future."""
        _write(self.roster, rows)

        return self._portal().start_task_soon(self.app.state.reload_trigger.reload_once)

    def _portal(self) -> BlockingPortal:
        portal = self.client.portal
        assert portal is not None

        return portal


@contextmanager
def _pep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, object]
) -> Iterator[_Pep]:
    """A PEP as a host starts it. The base roster is the committed `{}` and
    `rows` are the generated roster root writes, so the pool is reloadable
    and `SIGHUP` moves it. `_Pep.reload` rewrites the generated one."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = _write(tmp_path / "upstreams.yaml", {})
    generated = _write(tmp_path / "generated.yaml", rows)
    write_grants(tmp_path / "rework" / "grants", make_grants(tools={SERVER: ["echo"]}, verbs={}))
    app = _main(monkeypatch, tmp_path, base, generated)

    with TestClient(app) as client:
        yield _Pep(client, app, generated)


def _wait_for(path: Path) -> None:
    """Until a stub wrote its pid there: its process is up."""
    deadline = time.monotonic() + CEILING_S
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return
        time.sleep(POLL_S)

    raise AssertionError(f"no stub started: {path} is empty")


# -- one reader: the family path follows the reload ------------------------


def test_a_server_a_reload_adds_answers_on_the_family_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool probed it and served it, and the family path answered 501
    until the PEP restarted, because the name was not in the frozen set."""
    with _pep(tmp_path, monkeypatch, {}) as pep:
        before = pep.call(ADMITTED)
        report = pep.reload({SERVER: _row()})
        offered = pep.offered()
        answer = pep.call(ADMITTED)

    assert before.status_code == HTTP_NOT_IMPLEMENTED
    assert report is not None
    assert report.added == (SERVER,)
    assert ECHO in offered
    assert answer.status_code == HTTP_OK, answer.json()
    assert answer.json()["result"] == {"text": f"v1:{ADMITTED}"}


def test_a_fence_a_reload_adds_refuses_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 01b §7 from the reload on. The refusal is the one a fence
    the process started with gives: same status, reason and words."""
    with _pep(tmp_path / "at-start", monkeypatch, {SERVER: _row(denies=(DENIED,))}) as fenced:
        at_start = fenced.call(DENIED)

    with _pep(tmp_path / "reloaded", monkeypatch, {SERVER: _row()}) as pep:
        before = pep.call(DENIED)
        pep.reload({SERVER: _row(denies=(DENIED,))})
        refused = pep.call(DENIED)
        admitted = pep.call(ADMITTED)

    assert before.status_code == HTTP_OK
    assert at_start.status_code == HTTP_BAD_REQUEST
    assert refused.status_code == HTTP_BAD_REQUEST, refused.json()
    assert refused.json() == at_start.json()
    assert refused.json()["reason"] == "arg_validation"
    assert admitted.status_code == HTTP_OK


def test_a_fence_a_reload_removes_admits_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _pep(tmp_path, monkeypatch, {SERVER: _row(denies=(DENIED,))}) as pep:
        before = pep.call(DENIED)
        pep.reload({SERVER: _row()})
        admitted = pep.call(DENIED)

    assert before.status_code == HTTP_BAD_REQUEST
    assert admitted.status_code == HTTP_OK, admitted.json()
    assert admitted.json()["result"] == {"text": f"v1:{DENIED}"}


def test_a_server_a_reload_removes_is_not_loaded_and_leaves_its_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It leaves the manifest, and a call to it answers "not loaded" whatever
    it carries: the value its fence refused included. A fence that stayed
    behind with the name answered 400 instead."""
    with _pep(tmp_path, monkeypatch, {SERVER: _row(denies=(DENIED,))}) as pep:
        before = pep.offered()
        report = pep.reload({})
        offered = pep.offered()
        refused = pep.call(DENIED)

    assert ECHO in before
    assert report is not None
    assert report.removed == (SERVER,)
    assert ECHO not in offered
    assert refused.status_code == HTTP_NOT_IMPLEMENTED, refused.json()
    assert refused.json()["reason"] == "not_implemented"
    assert refused.json()["detail"] == f"server {SERVER!r} is not loaded on this PEP"


def test_a_failed_replacement_keeps_the_old_rows_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old row keeps serving, its process AND its fence, and the refused
    row's fence never applies. The old row is one a reload loaded, not the
    one the process started with, so the case tells the two apart."""
    with _pep(tmp_path, monkeypatch, {SERVER: _row()}) as pep:
        pep.reload({SERVER: _row("v2", denies=(DENIED,))})
        report = pep.reload({SERVER: _row("v3", denies=(ADMITTED,), command=MISSING)})
        refused = pep.call(DENIED)
        admitted = pep.call(ADMITTED)

    assert report is not None
    assert [one.name for one in report.failed] == [SERVER]
    assert refused.status_code == HTTP_BAD_REQUEST, refused.json()
    assert admitted.status_code == HTTP_OK, admitted.json()
    assert admitted.json()["result"] == {"text": f"v2:{ADMITTED}"}


def test_a_call_during_a_reload_sees_the_old_row_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a mix. While the new row's process is still being probed, a
    call is decided under the old row's fences and runs on the old row's
    process. The new fences arrive with the new process, in one swap.

    The new row's stub holds its probe open until the test opens `gate`, so
    the call lands mid-reload for certain."""
    gate = tmp_path / "gate"
    trace = tmp_path / "probe.pids"
    held = _row("v2", denies=(DENIED,), env={"STUB_HOLD": str(gate), "STUB_TRACE": str(trace)})

    with _pep(tmp_path, monkeypatch, {SERVER: _row()}) as pep:
        running = pep.reload_soon({SERVER: held})
        _wait_for(trace)
        mid_offered = pep.offered()
        mid = pep.call(DENIED)
        gate.touch()
        report = running.result(CEILING_S)
        after = pep.call(DENIED)
        admitted = pep.call(ADMITTED)

    assert ECHO in mid_offered
    assert mid.status_code == HTTP_OK, mid.json()
    assert mid.json()["result"] == {"text": f"v1:{DENIED}"}
    assert report is not None
    assert report.replaced == (SERVER,)
    assert after.status_code == HTTP_BAD_REQUEST, after.json()
    assert admitted.json()["result"] == {"text": f"v2:{ADMITTED}"}


# -- no roster takes the PEP down ------------------------------------------


def _bad_yaml(path: Path) -> None:
    path.write_text(f"{SERVER}: [unclosed\n", encoding="utf-8")


def _bad_row(path: Path) -> None:
    path.write_text(f"{SERVER}: [this is not a mapping]\n", encoding="utf-8")


def _unreadable(path: Path) -> None:
    """A directory where the file should be. `open` refuses it for every
    user, root included, so the case holds on any runner."""
    path.mkdir()


START_SHAPES: Final = (
    pytest.param(_bad_yaml, id="bad-yaml"),
    pytest.param(_bad_row, id="bad-row"),
    pytest.param(_unreadable, id="unreadable-file"),
)


def _since(state: dict[str, object]) -> datetime:
    """`/healthz`'s `roster.since`, which is RFC 3339 at second resolution."""
    raw = state["since"]
    assert isinstance(raw, str), state

    return datetime.fromisoformat(raw)


@pytest.mark.parametrize("shape", START_SHAPES)
def test_a_roster_that_will_not_parse_at_start_leaves_the_pep_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    shape: Callable[[Path], None],
) -> None:
    """The process starts with no MCP server serving, the reason is logged
    once, and `/healthz` answers with the roster's state. A raise out of
    `__main__.main` would stop the one enforcement point, verbs and all.

    The bad file is the generated roster, which is the one root writes: the
    base is the committed `{}`. The family's verbs still reach its manifest,
    because they never depended on the roster."""
    base = _write(tmp_path / "upstreams.yaml", {})
    generated = tmp_path / "generated.yaml"
    shape(generated)
    write_grants(tmp_path / "rework" / "grants", make_grants(tools={SERVER: ["echo"]}))
    started = datetime.now(UTC).replace(microsecond=0)

    with caplog.at_level(logging.ERROR, logger="agent_pep.reload"):
        app = _main(monkeypatch, tmp_path, base, generated)
        with TestClient(app) as client:
            health = client.get("/healthz").json()
            tools = client.get("/manifest", headers=AUTH).json()["tools"]
            mcp = client.post("/call", json={"tool": ECHO, "args": {"text": "x"}}, headers=AUTH)

    assert health["ok"] is True
    assert health["roster"]["state"] == UNREADABLE
    assert health["upstreams"] == {"serving": 0, "refused": 0}
    assert started <= _since(health["roster"]) <= datetime.now(UTC)
    offered = [one["name"] for one in tools]
    assert ECHO not in offered
    assert {"embed", "ha_call"} <= set(offered)
    assert mcp.status_code == HTTP_NOT_IMPLEMENTED
    logged = [one for one in caplog.records if UNREADABLE_LINE in one.getMessage()]
    assert len(logged) == 1, [one.getMessage() for one in logged]


def test_the_start_refuses_a_row_that_does_not_serve_what_it_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start is a reload, so §4.4 step 2's check runs at start as well.
    The boot probe it replaced passed no declared list and skipped the
    check, so a restart served a row the next reload refused."""
    declares_more = {**_row(), "tools": ["echo", "not_served"]}

    with _pep(tmp_path, monkeypatch, {SERVER: declares_more}) as pep:
        offered = pep.offered()
        answer = pep.call(ADMITTED)
        refusals = pep.app.state.reload_trigger.pool.refusals

    assert ECHO not in offered
    assert answer.status_code == HTTP_NOT_IMPLEMENTED
    assert "declares ['not_served']" in refusals[SERVER].detail


def test_healthz_counts_what_the_start_serves_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose server will not start is refused and the PEP still
    answers 200 `ok: true`. A launcher that cannot switch users refuses its
    upstream the same way. The count is
    what `pep-verify` fails on, so nobody reads the journal by hand."""
    rows = {SERVER: _row(), "gone": _row(command=MISSING)}

    with _pep(tmp_path, monkeypatch, rows) as pep:
        answer = pep.client.get("/healthz")

    assert answer.status_code == HTTP_OK
    assert answer.json()["ok"] is True
    assert answer.json()["upstreams"] == {"serving": 1, "refused": 1}


def test_the_next_reload_serves_a_roster_unreadable_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same shape a reload gives: the next signal tries again."""
    base = _write(tmp_path / "upstreams.yaml", {})
    generated = tmp_path / "generated.yaml"
    _bad_yaml(generated)
    write_grants(tmp_path / "rework" / "grants", make_grants(tools={SERVER: ["echo"]}, verbs={}))
    app = _main(monkeypatch, tmp_path, base, generated)

    with TestClient(app) as client:
        pep = _Pep(client, app, generated)
        first = pep.roster_state()
        report = pep.reload({SERVER: _row()})
        second = pep.roster_state()
        answer = pep.call(ADMITTED)

    assert first["state"] == UNREADABLE
    assert report is not None
    assert report.added == (SERVER,)
    assert second["state"] == OK
    assert _since(second) >= _since(first)
    assert answer.status_code == HTTP_OK, answer.json()
    assert answer.json()["result"] == {"text": f"v1:{ADMITTED}"}


# -- `reload_once` never raises --------------------------------------------


def _pool_spec(version: str) -> UpstreamSpec:
    return UpstreamSpec(
        name="a", command=sys.executable, args=(str(STUB),), env={"STUB_SECRET": version}
    )


def _stub_entry(version: str = "v1") -> dict[str, object]:
    return {"command": sys.executable, "args": [str(STUB)], "env": {"STUB_SECRET": version}}


def _gone(path: Path) -> None:
    path.unlink()


def _raw(body: bytes) -> Callable[[Path], None]:
    def write(path: Path) -> None:
        path.write_bytes(body)

    return write


#: Everything a roster read can raise, one case each. Any one that
#: escaped `reload_once` would raise out of the signal task.
READ_FAILURES: Final = (
    pytest.param(_raw(b"a: [unclosed\n"), id="yaml-parser"),
    pytest.param(_raw(b"a:\n\tcommand: x\n"), id="yaml-scanner-tab"),
    pytest.param(_raw(b"a: \xff\xfe\n"), id="not-utf-8"),
    pytest.param(_raw(b"a: 2001-02-30\n"), id="a-date-yaml-cannot-build"),
    pytest.param(_raw(b"[" * 20_000 + b"]" * 20_000), id="nested-past-the-recursion-limit"),
    pytest.param(_raw(b"a: [this is not a mapping]\n"), id="bad-row"),
    pytest.param(_gone, id="file-gone"),
)


@pytest.mark.anyio
@pytest.mark.parametrize("damage", READ_FAILURES)
async def test_reload_once_never_raises_and_the_old_set_keeps_serving(
    tmp_path: Path, damage: Callable[[Path], None]
) -> None:
    """Assumption 2, for every failure a read can raise."""
    roster = _write(tmp_path / "upstreams.yaml", {"a": _stub_entry()})
    pool = ReloadablePool({"a": _pool_spec("v1")}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        damage(roster)

        assert await trigger.reload_once() is None
        assert await pool.call("a", "echo", {"text": "hi"}) == "v1:hi"
    finally:
        await pool.stop()


@pytest.fixture
def fake_sops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `sops` that prints the file it is asked to decrypt, as
    `test_pep_r7f_reload_wiring.py` fakes it. The real one needs a key."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "sops"
    stub.write_text('#!/bin/sh\nexec cat "$2"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")

    return binaries


#: What a decrypted secret holds in the two cases below. Neither may reach
#: a log line (invariant 13): a YAML error quotes the line it failed on.
LEAKED_LINE: Final = "unclosed-credential"
LEAKED_BYTES: Final = b"\xff\xfe"


def _monolith_not_yaml(tmp_path: Path) -> RosterSource:
    """A monolith whose decrypted text will not parse."""
    monolith = tmp_path / "secrets.enc.yaml"
    monolith.write_text(f"kagi_api_key: [{LEAKED_LINE}\n", encoding="utf-8")

    return RosterSource(upstreams_file=tmp_path / "upstreams.yaml", secrets_file=monolith)


def _sealed_not_utf8(tmp_path: Path) -> RosterSource:
    """One pasted value that `sops` hands back as bytes that are not UTF-8."""
    store = tmp_path / "secrets"
    store.mkdir()
    (store / "kagi_api_key.enc").write_bytes(LEAKED_BYTES)

    return RosterSource(upstreams_file=tmp_path / "upstreams.yaml", secrets_dir=store)


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_sops")
@pytest.mark.parametrize(
    ("source", "secret"),
    (
        pytest.param(_monolith_not_yaml, LEAKED_LINE, id="monolith-not-yaml"),
        pytest.param(_sealed_not_utf8, "0xff", id="pasted-value-not-utf-8"),
    ),
)
async def test_secrets_that_will_not_read_keep_the_ones_in_memory(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    source: Callable[[Path], RosterSource],
    secret: str,
) -> None:
    """Assumption 4, for the failures `load_sops_secrets` and
    `load_secret_dir` raise past `SecretsError`. The credentials in memory
    stay, the roster, which read fine, applies, and the log names the
    failure's type and never the decrypted text."""
    _write(tmp_path / "upstreams.yaml", {"a": _stub_entry()})
    pool = ReloadablePool(
        {"a": _pool_spec("v1")}, {"kagi_api_key": "kept"}, make_pool=pools(UNCHECKED)
    )
    await pool.start()
    trigger = ReloadTrigger(pool, source(tmp_path))
    try:
        _write(tmp_path / "upstreams.yaml", {"a": _stub_entry(), "b": _stub_entry()})

        with caplog.at_level(logging.DEBUG):
            report = await trigger.reload_once()

        assert report is not None
        assert report.added == ("b",)
        assert pool.secrets == {"kagi_api_key": "kept"}
        assert "secrets unreadable" in caplog.text
        assert secret not in caplog.text
    finally:
        await pool.stop()


# -- `/healthz` says the roster's state ------------------------------------


def test_healthz_says_off_with_no_roster(tmp_path: Path) -> None:
    """A PEP with no roster source. A field, not a silence: a reader that
    finds no `roster` key is reading a PEP older than the field."""
    app = create_app(PepConfig(audit_dir=tmp_path / "audit", rework_dir=tmp_path / "rework"))

    with TestClient(app) as client:
        body = client.get("/healthz").json()

    assert body == {"ok": True, "roster": {"state": OFF, "since": None}, "upstreams": None}


@pytest.mark.anyio
async def test_an_unreadable_streak_keeps_the_time_it_began(tmp_path: Path) -> None:
    """An `unreadable` state keeps the time of the first failed read of its
    streak, and the next good read starts an `ok` of its own."""
    roster = _write(tmp_path / "upstreams.yaml", {"a": _stub_entry()})
    pool = ReloadablePool({}, {}, make_pool=pools(UNCHECKED))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        assert trigger.health.state == OFF

        await trigger.reload_once()
        good = trigger.health
        _bad_yaml(roster)
        await trigger.reload_once()
        first = trigger.health
        await asyncio.sleep(0.01)
        await trigger.reload_once()
        second = trigger.health
        _write(roster, {"a": _stub_entry()})
        await trigger.reload_once()
        back = trigger.health
    finally:
        await pool.stop()

    assert good.state == OK
    assert first.state == UNREADABLE
    assert second == first
    assert back.state == OK
    assert good.since is not None and first.since is not None and back.since is not None
    assert good.since <= first.since <= back.since
