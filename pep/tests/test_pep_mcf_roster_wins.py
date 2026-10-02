"""A released upstream beats the base roster's row of the same name.

If the BASE row won a collision, a release could install a server, write
its roster row and signal the PEP, and not one call would change where it
went: a `the generated roster redeclares <name>` line, a success in the
ledger, and the old pool still serving.

Base-wins would guard against a registry writer who shadows `github` and
inherits every grant already written against that name. That names the
wrong writer. Root is what writes the generated roster, at step 9 of a
release the operator approved on their phone, from the `server.yaml` files of
exactly the tree it just installed and with every `command` built by root
from a validated name. The base roster is a bootstrap file the `pep`
component happens to ship. The newer, verified, approved fact wins.

The committed base file holds no row, so it is no FLOOR for a generated
roster that is absent or unreadable (`docs/rework/stage7-releases.md`
§4.4). An unreadable roster keeps the live pool at a reload and serves no
MCP server at a start, base rows or not. The cases below pin the merge
with a base file of their own, and `test_pep_ret_roster_only.py` holds the
committed file to the rule.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml
from agent_pep.family_ids import MCP_TOOL_SEPARATOR
from agent_pep.mcp_client import UpstreamError
from agent_pep.reload_pool import ReloadablePool
from agent_pep.reload_wiring import ReloadTrigger, RosterSource
from pep_helpers import AS_TEST_USER, pools

pytestmark = pytest.mark.slow

STUB: Final = Path(__file__).resolve().parent / "stub_mcp_server.py"

RELOAD_CEILING_S: Final = 10.0

#: A base row's command: a console script inside a venv a deploy built.
BASE_COMMAND: Final = "/opt/agent-pep-mcp/mcp/bin/kagimcp"

#: What root writes after a release: `<mcp root>/<name>/bin/<entrypoint>`.
RELEASED_COMMAND: Final = "/opt/mcp/kagi/bin/kagimcp"

SERVER: Final = "kagi"
TOOL: Final = "kagi_search_fetch"

#: The name a family grant carries and the dispatcher splits
#: (`family_ids.MCP_TOOL_SEPARATOR`).
CALL: Final = f"{SERVER}{MCP_TOOL_SEPARATOR}{TOOL}"

#: A name a base file holds and the registry does not. The merge keeps
#: it: a release replaces the names it declares and removes nothing. The
#: committed base holds no such row.
UNRELEASED: Final = "github"


def _row(command: str, secret: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {"command": command, "args": [], "env": {}}
    if secret is not None:
        body["env"] = {"KAGI_API_KEY": f"secret:{secret}"}

    return body


def _write(path: Path, body: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(body), encoding="utf-8")

    return path


def _both(tmp_path: Path, generated: dict[str, object]) -> RosterSource:
    """The two files the PEP reads, with the base as the host has it."""
    base = _write(
        tmp_path / "base.yaml",
        {SERVER: _row(BASE_COMMAND), UNRELEASED: _row("/opt/agent-pep-mcp/mcp/bin/github")},
    )

    return RosterSource(
        upstreams_file=base, generated_file=_write(tmp_path / "upstreams.yaml", generated)
    )


# -- the collision --------------------------------------------------------


def test_the_released_row_is_the_one_that_serves(tmp_path: Path) -> None:
    """Fault 4, the other way round. After the release, a call to
    `kagi__kagi_search_fetch` reaches the tree the release installed."""
    source = _both(tmp_path, {SERVER: _row(RELEASED_COMMAND)})

    specs = source.upstreams()
    server, _, tool = CALL.partition(MCP_TOOL_SEPARATOR)

    assert tool == TOOL
    assert specs[server].command == RELEASED_COMMAND
    assert specs[server].command != BASE_COMMAND


def test_a_base_row_the_release_does_not_name_keeps_serving(tmp_path: Path) -> None:
    """The release replaces the names it declares and removes nothing.
    `github` is in this case's base file and in no `mcp/<name>/server.yaml`."""
    source = _both(tmp_path, {SERVER: _row(RELEASED_COMMAND)})

    specs = source.upstreams()

    assert sorted(specs) == sorted([SERVER, UNRELEASED])
    assert specs[UNRELEASED].command.startswith("/opt/agent-pep-mcp/")


def test_each_superseded_base_row_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A row that stops serving says so. Silence here is how eleven
    upstreams could have moved with nothing in the journal to read."""
    source = _both(tmp_path, {SERVER: _row(RELEASED_COMMAND)})
    with caplog.at_level(logging.INFO, logger="agent_pep.reload"):
        source.upstreams()

    said = [one.getMessage() for one in caplog.records if SERVER in one.getMessage()]

    assert len(said) == 1
    assert "supersedes" in said[0]
    assert UNRELEASED not in said[0]


def test_an_unreadable_generated_roster_raises(tmp_path: Path) -> None:
    """At a reload `reload_once` catches this and the live pool never
    moves. A start takes the same path and serves no MCP server, whatever
    the base holds, which is why the base rows were never a floor for
    this state."""
    base = _write(tmp_path / "base.yaml", {SERVER: _row(BASE_COMMAND)})
    generated = _write(tmp_path / "upstreams.yaml", {SERVER: "not a mapping"})

    with pytest.raises(UpstreamError):
        RosterSource(upstreams_file=base, generated_file=generated).upstreams()


def test_no_generated_roster_at_all_is_the_state_before_stage_7(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.yaml", {SERVER: _row(BASE_COMMAND)})

    specs = RosterSource(upstreams_file=base, generated_file=tmp_path / "gone.yaml").upstreams()

    assert specs[SERVER].command == BASE_COMMAND


# -- the open secret gap --------------------------------------------------


def _stub_row(version: str) -> dict[str, object]:
    return {"command": sys.executable, "args": [str(STUB)], "env": {"STUB_SECRET": version}}


def _gapped_row(secret: str) -> dict[str, object]:
    """A row whose credential has no value yet, because the operator has pasted
    nothing."""
    return {
        "command": sys.executable,
        "args": [str(STUB)],
        "env": {"STUB_SECRET": f"secret:{secret}"},
    }


@pytest.mark.anyio
async def test_a_server_with_an_open_gap_fails_closed_for_itself_alone(
    tmp_path: Path,
) -> None:
    """Contract 01b §4.1 rule 4, over a real reload.

    `resolve_env` refuses a `secret:` name it cannot find, so the gapped
    upstream never starts. It lands in `pool.refusals`, which `/healthz`
    counts as `upstreams.refused`, and every other upstream in the same
    reload keeps answering."""
    roster = _write(
        tmp_path / "upstreams.yaml", {"served": _stub_row("v1"), "gapped": _gapped_row("no_value")}
    )
    pool = ReloadablePool({}, {}, make_pool=pools(AS_TEST_USER))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        report = await asyncio.wait_for(trigger.reload_once(), RELOAD_CEILING_S)

        assert report is not None
        assert report.added == ("served",)
        assert [one.name for one in report.failed] == ["gapped"]
        assert sorted(pool.refusals) == ["gapped"]
        assert await pool.call("served", "echo", {"text": "hi"}) == "v1:hi"
    finally:
        await pool.stop()


@pytest.mark.anyio
async def test_the_pasted_value_starts_that_upstream_on_the_next_reload(
    tmp_path: Path,
) -> None:
    """The second tap, days after the first. Nothing about the release
    changes: the roster row is the one root already wrote, and the only
    new fact is a value in the secrets map."""
    roster = _write(tmp_path / "upstreams.yaml", {"gapped": _gapped_row("kagi_api_key")})
    pool = ReloadablePool({}, {}, make_pool=pools(AS_TEST_USER))
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster))
    try:
        await asyncio.wait_for(trigger.reload_once(), RELOAD_CEILING_S)

        assert sorted(pool.refusals) == ["gapped"]

        pool._secrets["kagi_api_key"] = "v2"  # pyright: ignore[reportPrivateUsage]
        report = await asyncio.wait_for(trigger.reload_once(), RELOAD_CEILING_S)

        assert report is not None
        assert report.added == ("gapped",)
        assert await pool.call("gapped", "echo", {"text": "hi"}) == "v2:hi"
    finally:
        await pool.stop()
