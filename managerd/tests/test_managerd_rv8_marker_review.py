"""Attacks on the reconciler's release-request writer.

The marker that holds the next request back is
`/srv/agents/state/rework/mcp-request.json`, written by the operator at mode
0640. The attacker writes every operator-writable file root or the
reconciler reads, so it writes this one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from agent_managerd.mcp_release import MAX_REQUEST_AGE_S, McpPaths, request_servers

NOW: Final = 1_758_153_590.0
SERVER: Final = "weather"


@pytest.fixture
def paths(tmp_path: Path) -> McpPaths:
    for part in ("releases/requests", "releases/done", "secrets", "secret-gaps", "mcp"):
        (tmp_path / part).mkdir(parents=True)

    return McpPaths(
        requests=tmp_path / "releases/requests",
        done=tmp_path / "releases/done",
        secrets=tmp_path / "secrets",
        gaps=tmp_path / "secret-gaps",
        installed_root=tmp_path / "mcp",
        marker=tmp_path / "mcp-request.json",
    )


def _write_marker(paths: McpPaths, body: dict[str, object]) -> None:
    paths.marker.write_text(json.dumps(body), "utf-8")


def test_a_marker_dated_in_the_future_does_not_suppress_every_request(
    paths: McpPaths,
) -> None:
    """`now - float(at) < MAX_REQUEST_AGE_S` is TRUE for every future
    `at`, so one file wedged the whole path: no `mcp-servers` request is
    ever filed again, and nothing says why."""
    _write_marker(paths, {"id": "AAAAAAAAAAAAAAAAAAAAAAAAAA", "at": NOW + 1e12})

    assert request_servers(paths, (SERVER,), NOW).servers == (SERVER,)


def test_a_marker_naming_no_ulid_does_not_suppress_every_request(
    paths: McpPaths,
) -> None:
    """The id is what `_hold` looks for in `done/`. An id no executor can
    ever write is an id `done/` never holds."""
    _write_marker(paths, {"id": "not-a-ulid", "at": NOW})

    assert request_servers(paths, (SERVER,), NOW).servers == (SERVER,)


def test_a_real_outstanding_request_still_holds_the_next_one_back(
    paths: McpPaths,
) -> None:
    """The fix must not turn the reconciler into the flood §3.2 rule 8
    exists to stop."""
    assert request_servers(paths, (SERVER,), NOW).servers == (SERVER,)
    assert request_servers(paths, (SERVER,), NOW + 1.0).servers == ()


def test_an_aged_out_request_is_asked_for_again(paths: McpPaths) -> None:
    """Aged out means root TOOK it (the file left `requests/`) and never
    answered. One still in `requests/` is waited on, whatever its age."""
    assert request_servers(paths, (SERVER,), NOW).servers == (SERVER,)
    for one in paths.requests.iterdir():
        one.unlink()

    assert request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S + 1.0).servers == (SERVER,)


def test_an_untaken_request_is_not_asked_for_again(paths: McpPaths) -> None:
    assert request_servers(paths, (SERVER,), NOW).servers == (SERVER,)

    late = request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S + 1.0)

    assert late.servers == ()
    assert "has waited 1 h in the spool untaken" in late.queued
