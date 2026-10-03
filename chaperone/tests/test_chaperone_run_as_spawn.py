"""The PEP starts every upstream through `chaperone-as`.

    <launcher> mcp-<name> <command> <args...>      PEP_CHILD_ENV=<the row's env>

Contract 01b §9: a server runs as its own user, `mcp-<name>`, and never as
`chaperone`. The roster row's `command` stays what root wrote. The PEP puts the
launcher and the user in front of it at spawn time, from the row's name,
and hands the row's variables over as one value the launcher decodes after
the switch.

These drive real stdio children through `stub_mcp_server.py`. The switch
itself is `fake_run_as.py`: the real launcher, with the user database and
the setuid replaced, because a test cannot setuid. The refusal is the
real, installed launcher, run against a user this host does not have.
"""

from __future__ import annotations

import json
import os
import sys
import sysconfig
from functools import partial
from pathlib import Path
from typing import Final

import pytest
import yaml
from chaperone.mcp_client import (
    LAUNCHER_SCRIPT,
    Launcher,
    StdioUpstreamPool,
    UpstreamError,
    UpstreamSpec,
    installed_launcher,
    resolve_env,
    server_user,
)
from chaperone.reload_pool import ReloadablePool
from chaperone.reload_wiring import RosterSource
from chaperone.run_as import CHILD_ENV_VAR
from handover.executor.roster import rows
from handover.mcpserver import parse_server

HERE: Final = Path(__file__).resolve().parent
STUB: Final = HERE / "stub_mcp_server.py"
FAKE_AS: Final = HERE / "fake_run_as.py"

#: `fake_run_as.py`'s pretend ids. Nobody's, and never actually taken.
FAKE_UID: Final = 64_001
FAKE_GID: Final = 64_002

#: A server name the pattern admits and no host has a user for.
ABSENT: Final = "chaperone-test-absent"

#: What a Python server's own startup puts in its environment, given none.
SERVER_RUNTIME_ADDS: Final = frozenset({"LC_CTYPE", "__CF_USER_TEXT_ENCODING"})

#: The PEP's real launcher, as the `chaperone` tree installs it.
LAUNCHER: Final = "/opt/components/chaperone/bin/chaperone-as"

#: `mcp/kagi/server.yaml` on agent-registry's main, as root reads it.
KAGI_SERVER: Final = b"""\
name: kagi
identity: "The fleet's Kagi API key. Search and page extraction, no account writes."

install:
  source: pypi
  package: kagimcp
  version: 1.0.2
  lock: mcp/kagi/install.lock
  python: "3.12"

run:
  entrypoint: kagimcp
  env:
    KAGI_API_KEY: secret:kagi_api_key

tools:
  - { name: kagi_search_fetch, description: Search the web., write: false }
  - { name: kagi_extract, description: Fetch one URL., write: false }
"""

#: A row in the base file's shape (`chaperone/upstreams.yaml` itself holds none):
#: a name the registry does not declare, and so a name no release made a user
#: for.
NODERED_BASE: Final = {
    "command": "/opt/agent-pep-mcp/mcp/bin/nodered-mcp",
    "args": [],
    "env": {"NODERED_MODE": "read", "NODERED_PASSWORD": "secret:nodered_mcp_password"},
}


def _faked(trace: Path) -> Launcher:
    return Launcher((sys.executable, str(FAKE_AS), "--trace", str(trace), "--"))


def _launched(trace: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]


# -- the argv ----------------------------------------------------------------


def test_the_spawn_argv_for_one_row() -> None:
    spec = UpstreamSpec(
        name="kagi",
        command="/opt/mcp/kagi/bin/kagimcp",
        args=("--flag",),
        env={"KAGI_API_KEY": "secret:kagi_api_key"},
    )

    params = Launcher((LAUNCHER,)).params(spec, resolve_env(spec, {"kagi_api_key": "s3cret"}))

    assert params.command == LAUNCHER
    assert params.args == ["mcp-kagi", "/opt/mcp/kagi/bin/kagimcp", "--flag"]
    # The row's variables are ONE value. The launcher is not handed them as
    # its own environment, and nothing else goes with them.
    assert params.env == {CHILD_ENV_VAR: json.dumps({"KAGI_API_KEY": "s3cret"})}


def test_a_roster_row_and_a_base_row_each_run_as_their_own_user(tmp_path: Path) -> None:
    """Root's generated roster and the base file, through the PEP's own
    merge. The user comes from the row's name either way, so a base row for
    `nodered` runs as `mcp-nodered` or does not run."""
    base = tmp_path / "upstreams.yaml"
    base.write_text(yaml.safe_dump({"nodered": NODERED_BASE}), encoding="utf-8")
    generated = tmp_path / "generated.yaml"
    released = rows((parse_server(KAGI_SERVER, "kagi"),), Path("/opt/mcp"))
    generated.write_text(yaml.safe_dump(released), encoding="utf-8")

    specs = RosterSource(upstreams_file=base, generated_file=generated).upstreams()
    launcher = Launcher((LAUNCHER,))
    spawned = {name: launcher.params(spec, {}).args for name, spec in specs.items()}

    assert spawned == {
        "kagi": ["mcp-kagi", "/opt/mcp/kagi/bin/kagimcp"],
        "nodered": ["mcp-nodered", "/opt/agent-pep-mcp/mcp/bin/nodered-mcp"],
    }


def test_the_user_is_roots_own_spelling() -> None:
    """Root builds `/opt/mcp/<name>` as `ServerFile.user`. The PEP drops the
    child to what that same property says, not to a second spelling."""
    server = parse_server(KAGI_SERVER, "kagi")

    assert server_user(server.name) == server.user == "mcp-kagi"
    assert server_user("github-code") == "mcp-github-code"


def test_the_pool_starts_children_through_the_installed_launcher() -> None:
    """The default is the real launcher. A pool that spawned a server as the
    PEP's own user unless told otherwise would be the fault this ends."""
    script = Path(sysconfig.get_path("scripts")) / LAUNCHER_SCRIPT

    assert installed_launcher() == Launcher((str(script),))
    assert StdioUpstreamPool({}, {})._launcher == installed_launcher()
    assert script.is_file()
    assert os.access(script, os.X_OK)


# -- a real spawn, with the switch faked -------------------------------------


@pytest.mark.slow
async def test_the_server_gets_the_rows_env_and_nothing_of_the_peps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the launcher hands `execve`, and what the server itself sees,
    are both exactly the row with its secret resolved. The PEP's own
    variables, the six the SDK copies included, and the other secrets in
    the PEP's memory reach neither."""
    monkeypatch.setenv("HOME", "/var/lib/creche-chaperone/home")
    monkeypatch.setenv("USER", "chaperone")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", "/etc/agent-pep/age.key")
    trace = tmp_path / "launched.jsonl"
    seen = tmp_path / "server-env.json"
    spec = UpstreamSpec(
        name="stub",
        command=sys.executable,
        args=(str(STUB),),
        env={"STUB_SECRET": "secret:stub_secret", "STUB_ENV_TRACE": str(seen)},
    )
    secrets = {"stub_secret": "s3cret", "kagi_api_key": "another server's"}
    pool = StdioUpstreamPool({"stub": spec}, secrets, launcher=_faked(trace))

    await pool.start()
    try:
        assert "echo" in pool.tools("stub")
        assert await pool.call("stub", "echo", {"text": "hi"}) == "s3cret:hi"
    finally:
        await pool.stop()

    row = {"STUB_SECRET": "s3cret", "STUB_ENV_TRACE": str(seen)}
    launched = _launched(trace)
    # The boot probe, then the call: two processes, each switched first.
    assert [(one["user"], one["uid"], one["gid"]) for one in launched] == [
        ("mcp-stub", FAKE_UID, FAKE_GID)
    ] * 2
    assert [one["argv"] for one in launched] == [[sys.executable, str(STUB)]] * 2
    assert [one["env"] for one in launched] == [row] * 2

    # The server's own view. Its interpreter adds what no PEP gave it:
    # PEP 538 sets LC_CTYPE when an environment names no locale, and macOS
    # gives every process __CF_USER_TEXT_ENCODING.
    server = json.loads(seen.read_text(encoding="utf-8"))
    assert {name: server[name] for name in row} == row
    assert set(server) - set(row) <= SERVER_RUNTIME_ADDS
    assert not {"HOME", "USER", "PATH", "SOPS_AGE_KEY_FILE"} & set(server)


@pytest.mark.slow
async def test_a_name_the_launcher_refuses_leaves_the_rest_serving(tmp_path: Path) -> None:
    """Fail closed for that upstream alone. `x` is no server name, so the
    real launcher logic refuses `mcp-x`, and `stub` beside it still serves."""
    trace = tmp_path / "launched.jsonl"
    specs = {
        name: UpstreamSpec(name=name, command=sys.executable, args=(str(STUB),), env={})
        for name in ("stub", "x")
    }
    pool = ReloadablePool(specs, {}, make_pool=partial(StdioUpstreamPool, launcher=_faked(trace)))

    report = await pool.start()
    try:
        assert [one.name for one in report.failed] == ["x"]
        assert report.added == ("stub",)
        assert "echo" in pool.tools("stub")
        with pytest.raises(UpstreamError, match="not running"):
            await pool.call("x", "echo", {"text": "hi"})
    finally:
        await pool.stop()


# -- the real launcher, as the test's own user --------------------------------


@pytest.mark.slow
async def test_a_launcher_that_cannot_switch_leaves_the_upstream_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No fake anywhere: the pool's default launcher, the installed console
    script, against a user this host does not have. It exits before
    `initialize`, and the PEP records that upstream as refused.

    The launcher's own line goes to the PEP's stderr, which is the journal
    on a host. `test_chaperone_run_as_launcher.py` pins its words."""
    spec = UpstreamSpec(name=ABSENT, command=sys.executable, args=(str(STUB),), env={})
    pool = ReloadablePool({ABSENT: spec}, {})

    report = await pool.start()
    try:
        assert [one.name for one in report.failed] == [ABSENT]
        assert ABSENT in pool.refusals
        assert pool.tools(ABSENT) == {}
        with pytest.raises(UpstreamError, match="not running"):
            await pool.call(ABSENT, "echo", {"text": "hi"})
    finally:
        await pool.stop()

    assert f"upstream '{ABSENT}' failed to start" in caplog.text
