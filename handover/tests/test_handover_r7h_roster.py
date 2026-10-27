"""The far end of invariant 18: root writes the PEP's upstream roster.

Without that writer, the `SIGHUP` the executor sends makes the PEP re-read
a file nothing updates, and the reload computes `added=()`.

Under test here is `executor/roster.py`: what root writes, and that it
writes it safely.

The rule every case comes back to is WHO writes it. `caregiver` runs as
the operator, and the roster names commands the PEP executes, so a roster the operator
could write is `stage7-releases.md` §6 row 9 with the fence removed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast

import pytest
import yaml
from handover.errors import Refusal
from handover.executor.ledger import Entry
from handover.executor.mcpbuild import McpBuilder, ServerBuild, ServerPaths
from handover.executor.roster import Restored, restore, rows, write
from handover.executor.steps import MCP_COMPONENT, Release, Wiring
from handover.mcpserver import ServerFence, ServerFile, ServerPin, Source, parse_server

MCP_ROOT: Final = Path("/opt/mcp")
SERVER: Final = "weather"

SERVER_YAML: Final = f"""
name: {SERVER}
identity: "The fleet's weather key. Forecasts only, no account writes."
install:
  source: pypi
  package: weather-mcp
  version: 2.3.1
  lock: mcp/{SERVER}/install.lock
  python: "3.12"
run:
  entrypoint: weather-mcp
  args: [stdio]
  env:
    WEATHER_API: https://api.example.invalid/v3
    WEATHER_TOKEN: secret:weather_token
tools:
  - name: forecast
    description: Tomorrow's weather, by city.
  - name: observations
    description: What the station is reading right now.
arg_denies:
  - tools: all
    arg: city
    values: [area-51]
"""


def _server(**changes: object) -> ServerFile:
    """A declared server, parsed by the reader root actually uses."""
    parsed = parse_server(SERVER_YAML.encode("utf-8"), SERVER)
    if not changes:
        return parsed

    fields = {
        "name": parsed.name,
        "pin": parsed.pin,
        "entrypoint": parsed.entrypoint,
        "tools": parsed.tools,
        "secrets": parsed.secrets,
        "args": parsed.args,
        "env": parsed.env,
        "arg_denies": parsed.arg_denies,
    }
    fields.update(changes)

    return ServerFile(**fields)  # type: ignore[arg-type]


# --- what root writes --------------------------------------------------------


def test_the_command_is_built_by_root_not_copied_from_the_file() -> None:
    """§6 row 9. `command` is a console-script name in the file and an
    absolute path in the roster, and root is what joins the two."""
    row = rows((_server(),), MCP_ROOT)[SERVER]

    assert row["command"] == "/opt/mcp/weather/bin/weather-mcp"
    assert row["args"] == ["stdio"]


def test_the_row_carries_the_declared_closed_tool_set() -> None:
    """Contract 01b §5: `tools` is a control. Without it in the roster the
    PEP's probe decides alone and a package update that adds a tool widens
    reach with no file change (`stage7-releases.md` §4.2)."""
    row = rows((_server(),), MCP_ROOT)[SERVER]

    assert row["tools"] == ["forecast", "observations"]


def test_a_secret_reference_travels_as_a_name() -> None:
    """Invariant 13. The roster is a file the operator can read, so it carries
    `secret:<name>` and the PEP resolves it against root's store."""
    row = rows((_server(),), MCP_ROOT)[SERVER]

    assert row["env"] == {
        "WEATHER_API": "https://api.example.invalid/v3",
        "WEATHER_TOKEN": "secret:weather_token",
    }


def test_all_is_expanded_because_the_pep_has_no_word_for_it() -> None:
    """Contract 01b §7 allows `tools: all`. The PEP's roster reader
    takes a non-empty LIST and refuses the string, so an unexpanded `all`
    would make the whole roster unparseable and the reload would keep the
    old set — a deny that silently became no upstream at all."""
    row = rows((_server(),), MCP_ROOT)[SERVER]

    assert row["arg_denies"] == [
        {"tools": ["forecast", "observations"], "arg": "city", "values": ["area-51"]}
    ]


def test_a_server_with_no_tools_writes_no_tools_key() -> None:
    """Empty means the file declared none and the probe decides alone."""
    row = rows((_server(tools=(), arg_denies=()),), MCP_ROOT)[SERVER]

    assert "tools" not in row


# --- how it is written -------------------------------------------------------


def test_the_previous_roster_is_kept_and_restored(tmp_path: Path) -> None:
    path = tmp_path / "upstreams.yaml"
    write(path, (), MCP_ROOT)
    write(path, (_server(),), MCP_ROOT)

    assert SERVER in _read(path)
    assert restore(path) is Restored.PREVIOUS
    assert _read(path) == {}


def test_the_first_roster_a_release_ever_wrote_is_removed_by_its_restore(
    tmp_path: Path,
) -> None:
    """There is no previous roster before the first `mcp-servers`
    release, and leaving this one behind would leave the PEP a file
    naming servers the same restore had just taken off the host.

    A missing generated roster is what the PEP's roster reader reads as
    "no such release has run", which is the state being restored.
    """
    path = tmp_path / "upstreams.yaml"
    write(path, (_server(),), MCP_ROOT)

    assert restore(path) is Restored.REMOVED
    assert not path.exists()


def test_a_restore_with_no_roster_at_all_says_so(tmp_path: Path) -> None:
    assert restore(tmp_path / "upstreams.yaml") is Restored.NOTHING


def test_the_file_is_never_seen_half_written(tmp_path: Path) -> None:
    """Rule 2: create then rename, inside one directory. A reader gets the
    old bytes or the new ones, and the PEP reads this file on a signal it
    does not choose the moment of."""
    path = tmp_path / "upstreams.yaml"
    write(path, (_server(),), MCP_ROOT)

    assert not (tmp_path / "upstreams.yaml.new").exists()
    assert path.stat().st_mode & 0o777 == 0o644


def test_the_roster_never_stops_existing(tmp_path: Path) -> None:
    """The previous roster is COPIED aside, not renamed. A rename leaves a
    window in which the path does not exist, and a reload landing in it
    reads the base file alone and removes every released upstream."""
    path = tmp_path / "upstreams.yaml"
    write(path, (_server(),), MCP_ROOT)
    inode = path.stat().st_ino

    write(path, (), MCP_ROOT)

    assert path.is_file()
    assert path.with_name("upstreams.yaml.prev").stat().st_ino != inode
    assert SERVER in _read(path.with_name("upstreams.yaml.prev"))


def test_a_name_that_leaves_the_mcp_root_is_refused() -> None:
    """Belt and braces over `paths_of`. The name matched a pattern with no
    slash in it, so reaching this means a pattern changed."""
    with pytest.raises(Refusal):
        rows((_server(name="../etc"),), MCP_ROOT)


# --- the reader's own refusals ----------------------------------------------


def test_all_on_a_file_with_no_tools_is_refused() -> None:
    """The expansion would be empty, the PEP's reader would refuse the
    roster, and the whole reload would keep the old set."""
    head = SERVER_YAML.split("tools:")[0]
    body = head + "arg_denies:\n  - tools: all\n    arg: city\n    values: [x]\n"

    with pytest.raises(Refusal, match="declares no tool"):
        parse_server(body.encode("utf-8"), SERVER)


def test_a_fence_with_no_values_is_refused() -> None:
    body = SERVER_YAML.replace("values: [area-51]", "values: []")

    with pytest.raises(Refusal, match="fences nothing"):
        parse_server(body.encode("utf-8"), SERVER)


def test_the_pin_is_unchanged_by_the_new_fields() -> None:
    """The fields a roster needs leave the pin the reader produces as
    every other test reads it."""
    parsed = _server()

    assert parsed.pin.source is Source.PYPI
    assert parsed.pin == ServerPin(
        source=Source.PYPI,
        python="3.12",
        package="weather-mcp",
        version="2.3.1",
        lock=f"mcp/{SERVER}/install.lock",
    )
    assert parsed.secrets == ("weather_token",)
    assert parsed.arg_denies == (ServerFence(tools=None, arg="city", values=("area-51",)),)


def _read(path: Path) -> dict[str, object]:
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))

    return loaded if isinstance(loaded, dict) else {}


def test_both_packages_name_the_same_roster_file() -> None:
    """`caregiver` reads what root writes, and the two packages share no
    module, so the path is spelled twice. A drift would make every pass
    ask for a release it does not need — harmless, and invisible until a
    reader wonders why `done/` is full."""
    from caregiver.mcp_release import ROSTER_FILE as read_by_caregiver
    from handover.executor.host import ROSTER_FILE as written_by_root

    assert str(read_by_caregiver) == written_by_root


# --- the restore the deploy loop could not reach -----------------------------


class _Entry:
    """`ledger.Entry` with the two members this path touches."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.manual: list[str] = []

    def say(self, line: str) -> None:
        self.lines.append(line)


class _Result:
    code = 0


class _NoBuilder:
    """`McpBuilder` with the two calls this path makes and no host."""

    def __init__(self) -> None:
        self.swapped_back: list[str] = []
        self.signalled = 0

    def swap_back(self, build: object) -> None:
        self.swapped_back.append(str(getattr(build, "name", "?")))

    def reload_chaperone(self) -> _Result:
        self.signalled += 1

        return _Result()


class _NoWiring:
    def __init__(self, roster: Path) -> None:
        self.host = SimpleNamespace(roster_file=roster, mcp_root=MCP_ROOT)


def test_the_servers_go_back_when_the_component_never_deployed(tmp_path: Path) -> None:
    """Step 9 swaps the per-server trees and writes the roster BEFORE it
    swaps `mcp-servers`'s own tree, so a failure in between leaves the new
    servers live and the component out of `deployed`. A step 10 that walks
    `deployed` alone puts nothing back: the roster still names the new
    server, no signal goes out, and the PEP keeps serving a set no release
    ever finished.
    """
    roster = tmp_path / "state" / "upstreams.yaml"
    roster.parent.mkdir(parents=True)
    write(roster, (_server(),), MCP_ROOT)
    write(roster, (), MCP_ROOT)

    release = Release.__new__(Release)
    release.deployed = []
    release.swapped = [
        ServerBuild(
            name=SERVER,
            paths=ServerPaths(
                to=tmp_path / SERVER,
                prev=tmp_path / f"{SERVER}.prev",
                new=tmp_path / f"{SERVER}.new",
            ),
            artifact="weather-mcp==2.3.1",
        )
    ]
    release.roster_written = True
    release.entry = cast("Entry", _Entry())
    builder = _NoBuilder()
    release.mcp = cast("McpBuilder", builder)
    release.wiring = cast("Wiring", _NoWiring(roster))

    # The private name is the unit under test: this restore has no public
    # caller that can be reached without a whole release.
    put_back = release._restore_orphaned_servers()

    assert put_back == [f"{MCP_COMPONENT} servers"]
    assert SERVER in _read(roster), "the roster was not put back"
    assert builder.swapped_back == [SERVER]
    assert builder.signalled == 1
