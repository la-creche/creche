"""The roster root writes is the ONLY source for a declared server.

`chaperone/upstreams.yaml`, the base roster the unit names as `PEP_UPSTREAMS`,
holds no row. Base rows would serve in one state only, and only from trees
no release installed:

| The generated roster | With base rows | With none |
|---|---|---|
| absent | the old trees serve | nothing serves, and each call says so |
| unreadable, at a reload | the live pool keeps serving | the same |
| unreadable, at a start | no MCP server serves | the same |
| readable | the released rows serve | the same |

These tests hold the committed file and the PEP's behaviour with it to that
(`docs/rework/stage7-releases.md` §4.4). The server rows (kagi's command,
the name split, the `vikunja-*` env rule, github's toolsets and its fence
on `main`) are agent-registry's `mcp/<name>/server.yaml` files, checked by
that repository's CI and by root's reader.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import pytest
import yaml
from chaperone.app import PepConfig, create_app
from chaperone.family_ids import MCP_TOOL_SEPARATOR
from chaperone.mcp_client import load_upstreams
from chaperone.reload_wiring import RosterSource
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaperone import __main__ as entry

#: The base roster as this repository ships it. The deploy puts it at
#: `/opt/agent-control/chaperone/upstreams.yaml`, which is the unit's
#: `PEP_UPSTREAMS`.
COMMITTED: Final = Path(__file__).resolve().parents[1] / "upstreams.yaml"

#: The eleven servers agent-registry's `main` declares.
DECLARED: Final = (
    "board-lead",
    "github-code",
    "github-platform",
    "ha",
    "ha-read",
    "kagi",
    "mail",
    "vikunja-agent-control",
    "vikunja-finance",
    "vikunja-home-assistant",
    "vikunja-networking",
)

#: Where root puts the tree of every server a release installs.
MCP_ROOT: Final = "/opt/mcp"

#: The one grant the family fixture carries, as a family calls it.
CALL: Final = f"kagi{MCP_TOOL_SEPARATOR}kagi_search_fetch"

#: Every variable `__main__` reads beyond the four a case sets. Unset, so a
#: developer's shell cannot turn a door on under the test.
OTHER_ENV: Final = (
    "PEP_SECRETS",
    "PEP_SECRETS_DIR",
    "PEP_SESSIOND_SOCKET",
    "PEP_DELEGATE_TOKEN_FILE",
    "PEP_DISPATCH_TOKEN_FILE",
    "PEP_RELEASE_REQUESTS_DIR",
    "PEP_APPROVAL_URL",
)


def _released(path: Path, names: tuple[str, ...]) -> Path:
    """A roster in the shape root writes: one row per server, the command
    inside that server's own tree."""
    rows = {one: {"command": f"{MCP_ROOT}/{one}/bin/{one}", "args": [], "env": {}} for one in names}
    path.write_text(yaml.safe_dump(rows), encoding="utf-8")

    return path


def _absent(tmp_path: Path) -> RosterSource:
    """The committed base, and no roster root wrote."""
    return RosterSource(upstreams_file=COMMITTED, generated_file=tmp_path / "absent.yaml")


# -- the committed file ---------------------------------------------------


def test_the_committed_base_roster_is_an_empty_mapping() -> None:
    """`{}` and not a missing file: the PEP builds its reloadable roster only
    when `PEP_UPSTREAMS` names a file (`chaperone.__main__._roster`), and
    without one no release is ever served."""
    assert yaml.safe_load(COMMITTED.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("body", ("{}\n", "# a header and nothing else\n", ""))
def test_an_empty_base_file_parses_as_no_rows(tmp_path: Path, body: str) -> None:
    base = tmp_path / "upstreams.yaml"
    base.write_text(body, encoding="utf-8")

    assert load_upstreams(base) == {}


# -- the two states that decide it ----------------------------------------


def test_with_no_generated_roster_nothing_is_served(tmp_path: Path) -> None:
    """A fresh host before its first release, or a first release that was
    restored and removed the roster it wrote. No name serves from a tree no
    release installed."""
    assert _absent(tmp_path).upstreams() == {}


def test_with_a_generated_roster_it_is_all_that_serves(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every row root wrote and nothing else. No base row survives beside
    them and none is superseded, so the journal carries no `supersedes`
    line."""
    generated = _released(tmp_path / "upstreams.yaml", DECLARED)

    with caplog.at_level(logging.INFO, logger="chaperone.reload"):
        specs = RosterSource(upstreams_file=COMMITTED, generated_file=generated).upstreams()

    assert sorted(specs) == sorted(DECLARED)
    for name, spec in specs.items():
        assert spec.command.startswith(f"{MCP_ROOT}/{name}/"), name
    assert not [one for one in caplog.records if "supersedes" in one.getMessage()]


# -- what the process does with it ----------------------------------------


def test_the_pep_starts_with_the_committed_base_and_no_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`__main__.main` up to the server: the process starts, builds a
    reloadable roster, and configures no upstream at all."""
    seen: list[PepConfig] = []

    def capture(cfg: PepConfig) -> FastAPI:
        seen.append(cfg)
        return FastAPI()

    def serve(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setenv("PEP_REWORK_DIR", str(tmp_path / "rework"))
    monkeypatch.setenv("PEP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("PEP_UPSTREAMS", str(COMMITTED))
    monkeypatch.setenv("PEP_UPSTREAMS_GENERATED", str(tmp_path / "absent.yaml"))
    for name in OTHER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(entry, "create_app", capture)
    monkeypatch.setattr("chaperone.__main__.uvicorn.run", serve)
    # `main` quiets two loggers for the whole process. Put them back, so no
    # later test in this worker inherits the change.
    quieted = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        assert entry.main() == 0
    finally:
        for name, level in quieted.items():
            logging.getLogger(name).setLevel(level)

    assert seen[0].upstreams == {}
    assert seen[0].roster is not None


def test_with_no_roster_a_granted_tool_is_left_out_and_its_call_says_why(
    tmp_path: Path,
) -> None:
    """What a family sees from a PEP that serves no MCP server: the tool is
    missing from its manifest, and a call answers 501 `not_implemented`
    naming the server. That is the answer a failed probe already gives, one
    server at a time."""
    write_grants(tmp_path / "rework" / "grants", make_grants(verbs={}))
    source = _absent(tmp_path)
    app = create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            upstreams=source.upstreams(),
            roster=source,
        )
    )
    auth = {"Authorization": f"Bearer {FAMILY_TOKEN}"}

    with TestClient(app) as client:
        manifest = client.get("/manifest", headers=auth).json()
        reply = client.post("/call", json={"tool": CALL, "args": {"query": "x"}}, headers=auth)

    assert CALL not in [one["name"] for one in manifest["tools"]]
    assert reply.status_code == 501
    assert reply.json()["reason"] == "not_implemented"
    assert "server 'kagi' is not loaded" in reply.json()["detail"]
