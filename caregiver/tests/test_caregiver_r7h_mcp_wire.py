"""The near end of invariant 18: the reconcile loop files the request.

Every test here drives `loop.look`, the real pass, and never
`mcp_release` directly: a test that calls the module proves the module,
and a library nothing calls opens no gap and files no `mcp-servers`
request, so `stage7-releases.md` §4.1 step 3 never happens.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml
from caregiver.driver import FakeDriver
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, look
from caregiver.mcp_release import (
    MARKER_NAME,
    MAX_REQUEST_AGE_S,
    MERGE_DEPTH_MAX,
    MERGE_PAIRS_MAX,
    McpPaths,
)
from caregiver.mcp_wire import SECRET_TWICE, McpReport
from caregiver.reconcile import Actors
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import write_registry

IMAGE: Final = "sha256:deadbeef"
NOW: Final = 1_758_153_600.0

SERVER: Final = "weather"
SECRET: Final = "weather_token"

#: Two server files with the fields of a registry that was in use, comments
#: removed. Contract 01b §4.3's only shared-secret pair: a synthetic stand-in
#: could not prove the pass reads what an operator wrote.
HA_REGISTRY: Final = Path(__file__).resolve().parent / "caregiver_mws_registry"
HA_PAIR: Final = ("ha", "ha-read")
HA_SECRET: Final = "ha_read_token"


def server_yaml(name: str = SERVER, secret: str = SECRET, partner: str = "") -> str:
    """Contract 01b §8.1, shortened to what this wire reads.

    `partner` adds the §4.3 declaration. Empty means the file declares no
    sharing at all, which is the typo case and stays a clash.
    """
    body: dict[str, object] = {
        "name": name,
        "identity": f"The fleet's {name} credential. Read only.",
        "install": {
            "source": "pypi",
            "package": f"{name}-mcp",
            "version": "1.0.0",
            "lock": f"mcp/{name}/install.lock",
            "python": "3.12",
        },
        "run": {
            "entrypoint": f"{name}-mcp",
            "env": {"WEATHER_URL": "https://example.invalid", "API_TOKEN": f"secret:{secret}"},
        },
        "tools": [{"name": "forecast", "description": "Tomorrow's weather, by city."}],
    }
    if partner:
        body["shared_secrets"] = [{"secret": secret, "server": partner}]

    return yaml.safe_dump(body)


def copy_ha(registry: Path) -> None:
    """Put `mcp/ha` and `mcp/ha-read` into a registry the loop watches."""
    shutil.copytree(HA_REGISTRY / "mcp", registry / "mcp", dirs_exist_ok=True)


def write_server(registry: Path, name: str = SERVER, body: str | None = None) -> Path:
    directory = registry / "mcp" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "server.yaml"
    path.write_text(server_yaml(name) if body is None else body, encoding="utf-8")

    return path


class Bench:
    """A registry the loop watches, and the spool root's four directories."""

    def __init__(self, tmp_path: Path) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        write_registry(self.registry_root)
        self.mcp = McpPaths(
            requests=_made(tmp_path / "spool" / "requests"),
            done=_made(tmp_path / "spool" / "done"),
            secrets=_made(tmp_path / "secrets"),
            gaps=_made(tmp_path / "secret-gaps"),
            installed_root=_made(tmp_path / "opt-mcp"),
            marker=tmp_path / "state" / MARKER_NAME,
            roster=_made(tmp_path / "state") / "upstreams.yaml",
        )
        self.state = LoopState()
        self.now = NOW

    def config(self) -> LoopConfig:
        return LoopConfig(
            registry_root=self.registry_root,
            state_root=self.state_root,
            image=IMAGE,
            mcp=self.mcp,
            clock=lambda: self.now,
            # Every look passes. The real gate is 20 seconds of
            # `time.monotonic`, which a test that moves `self.now` never
            # reaches, and what is under test is what a pass DOES.
            heartbeat_s=0.0,
        )

    def actors(self) -> Actors:
        return Actors(
            FakeDriver(), FakeLiteLLMKeys(), FakeSwitchClient(), EgressConfig(), FakeUnits()
        )

    def look(self) -> None:
        """One real pass of the loop, registry read and all."""
        look(self.config(), self.actors(), self.state)

    def requests(self) -> list[dict[str, object]]:
        return [_json(one) for one in sorted(self.mcp.requests.iterdir())]

    def gaps(self) -> list[str]:
        return sorted(one.name for one in self.mcp.gaps.iterdir())

    def report(self) -> McpReport:
        """What the LAST real pass said. Not a second pass: the second one
        is idempotent by design, so calling the module again would report
        nothing and prove nothing."""
        return self.state.mcp


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


def test_the_loop_files_one_request_for_a_declared_server(bench: Bench) -> None:
    write_server(bench.registry_root)

    bench.look()

    filed = bench.requests()
    assert len(filed) == 1
    assert filed[0]["components"] == {"mcp-servers": "latest"}
    assert filed[0]["requested_by"] == "managerd"
    # §4.3 step 1: the gap goes with it, because the secret has no value.
    assert bench.gaps() == [f"{SECRET}.json"]
    # Step 3's other half: the reconciler installs nothing.
    assert not any(bench.mcp.installed_root.iterdir())


def test_a_second_pass_files_nothing(bench: Bench) -> None:
    """Idempotence. The loop looks every two seconds, and §3.2 rule 8 caps
    pending requests per requester, so a pass that filed one per tick
    would refuse itself and fill `done/` with `rate` entries."""
    write_server(bench.registry_root)
    bench.look()
    bench.now += 1.0

    bench.look()

    assert len(bench.requests()) == 1
    assert bench.gaps() == [f"{SECRET}.json"]


def test_an_untaken_request_is_waited_on_and_said(bench: Bench) -> None:
    """With the path unit off, an hour rule alone would file a copy of the
    same request every hour, each one after the first a refusal (`the
    request moves no component`) and a phone push once root drains them
    in id order. A request still in `requests/` is untaken,
    not lost, so the loop waits — and says so, as a problem line, from
    the hour on."""
    write_server(bench.registry_root)
    bench.look()
    request_id = str(bench.requests()[0]["id"])
    bench.now += 3 * MAX_REQUEST_AGE_S + 1.0

    bench.look()

    assert [one["id"] for one in bench.requests()] == [request_id]
    assert bench.report().held is None
    assert bench.report().problems == (
        f"mcp-servers request {request_id} has waited 3 h in the spool untaken: "
        "the release path is not draining",
    )


def test_a_request_root_took_and_lost_ages_out(bench: Bench) -> None:
    """The other shape past the hour. Root removes a request from
    `requests/` the moment it starts it (`spool.start`); one that is gone
    with nothing in `done/` was lost between `running/` and the ledger,
    and that one is asked for again."""
    write_server(bench.registry_root)
    bench.look()
    taken = str(bench.requests()[0]["id"])
    (bench.mcp.requests / f"{taken}.json").unlink()
    bench.now += MAX_REQUEST_AGE_S + 1.0

    bench.look()

    filed = [str(one["id"]) for one in bench.requests()]
    assert len(filed) == 1
    assert filed != [taken]
    assert bench.report().problems == ()


def test_a_state_that_lasts_is_logged_once(bench: Bench, caplog: pytest.LogCaptureFixture) -> None:
    """Ten open gaps would log the same `secret gaps open` line every twenty
    seconds, 4300 times a day, while the gaps wait on ten taps of a phone.
    The line is logged when it changes."""
    write_server(bench.registry_root)
    with caplog.at_level(logging.INFO, logger="caregiver.loop"):
        bench.look()
        for _ in range(3):
            bench.now += 20.0
            bench.look()

    mcp_lines = [
        record.getMessage() for record in caplog.records if record.getMessage().startswith("mcp: ")
    ]
    assert len(mcp_lines) == 2
    assert mcp_lines[0].startswith(
        f"mcp: mcp-servers requested for {SERVER}; secret gaps open: {SECRET}"
    )
    assert mcp_lines[1] == f"mcp: secret gaps open: {SECRET}"


def test_an_installed_server_asks_for_nothing(bench: Bench) -> None:
    write_server(bench.registry_root)
    (bench.mcp.installed_root / SERVER).mkdir()
    (bench.mcp.secrets / f"{SECRET}.enc").write_text("sealed", encoding="utf-8")

    bench.look()

    assert bench.requests() == []
    assert bench.gaps() == []


def test_a_broken_server_file_reports_and_the_loop_survives(bench: Bench) -> None:
    """Invariant 19. A file that does not parse is a report, not a crash,
    and the OTHER server still gets its request."""
    write_server(bench.registry_root, name="broken", body="name: [this is not a server file")
    write_server(bench.registry_root)

    bench.look()

    report = bench.report()
    assert "broken" in report.problems[0]
    assert report.requested == (SERVER,)
    assert len(bench.requests()) == 1


def test_two_servers_naming_one_secret_open_no_gap(bench: Bench) -> None:
    """§6 row 8 at the near end. `_write_gap` keys the file on the SECRET
    name, so the second declaration would overwrite the first one's gap
    and the operator would be pushed one link naming one of the two servers.

    The far end is `read_registry`, which is a refused RELEASE. Here it is
    a refused GAP: root must not mint a token for a name whose owner is
    ambiguous."""
    write_server(bench.registry_root)
    write_server(bench.registry_root, name="evil", body=server_yaml("evil", SECRET))

    bench.look()

    report = bench.report()
    assert bench.gaps() == []
    # The journal line itself, not a substring. It is what the operator reads, and
    # the declared pair below must not have changed one character of it.
    assert report.problems == (f"{SECRET_TWICE}: {SECRET} is named by evil, {SERVER}",)
    # The request is still filed: the executor reads the registry again and
    # refuses the release itself, which is the fail-closed end and is where
    # The operator sees the reason.
    assert len(bench.requests()) == 1


def test_the_real_ha_pair_opens_one_gap(bench: Bench) -> None:
    """Contract 01b §4.3 lets two servers share one secret when BOTH
    declare it, and `mcp/ha` and `mcp/ha-read` do. Called a clash, the
    shared HA token would open no gap, the intake would never push the operator
    its link, and both HA upstreams would fail closed.

    The gap's shape is §4.3 rule 5's: ONE file, keyed on the secret, whose
    `server` is the pair's first name in sorted order. Nothing is added
    for the second name. `handover.intake.gaps.GAP_KEYS` is a closed
    set of `server`, `secret` and `at`, and `_parse_gap` refuses a body
    holding any other key and a `server` that is not one name — so an
    extended shape would not reach the operator at all.
    """
    copy_ha(bench.registry_root)

    bench.look()

    report = bench.report()
    assert report.problems == ()
    assert report.gaps == (HA_SECRET,)
    assert bench.gaps() == [f"{HA_SECRET}.json"]
    assert _json(bench.mcp.gaps / f"{HA_SECRET}.json") == {
        "server": HA_PAIR[0],
        "secret": HA_SECRET,
        "at": NOW,
    }
    assert report.requested == HA_PAIR


def test_a_one_sided_declaration_is_still_a_clash(bench: Bench) -> None:
    """§4.3 rule 1. `weather` names `evil` and `evil` names nobody, so
    the agreement is half written. Accepting it would let one merged edit
    bind a name whose owner never agreed, which is §6 row 8's attack."""
    write_server(bench.registry_root, body=server_yaml(SERVER, partner="evil"))
    write_server(bench.registry_root, name="evil", body=server_yaml("evil", SECRET))

    bench.look()

    assert bench.gaps() == []
    assert bench.report().problems == (f"{SECRET_TWICE}: {SECRET} is named by evil, {SERVER}",)


def test_a_third_server_breaks_the_pair(bench: Bench) -> None:
    """§4.3 rule 4: one secret has at most one partner. A third holder
    cannot be agreed by anybody, and no reader could say which agreement
    is missing, so the whole name is a clash again and the pair loses the
    gap it would have had alone."""
    copy_ha(bench.registry_root)
    write_server(bench.registry_root, name="evil", body=server_yaml("evil", HA_SECRET))

    bench.look()

    assert bench.gaps() == []
    assert bench.report().problems == (
        f"{SECRET_TWICE}: {HA_SECRET} is named by evil, {HA_PAIR[0]}, {HA_PAIR[1]}",
    )


def test_a_missing_spool_is_quiet(tmp_path: Path) -> None:
    """A host with no release spool has no `requests/`. That is not an
    error to log every two seconds."""
    bench = Bench(tmp_path)
    bench.mcp.requests.rmdir()
    write_server(bench.registry_root)

    bench.look()

    assert bench.report().problems == ()
    assert not bench.mcp.requests.exists()


def test_a_manager_with_no_mcp_paths_does_nothing(bench: Bench) -> None:
    """The default. A `LoopConfig` with no `mcp` files no MCP request, and
    it must not start writing into `/srv/agents/state/rework/`."""
    write_server(bench.registry_root)
    config = LoopConfig(registry_root=bench.registry_root, state_root=bench.state_root, image=IMAGE)

    look(config, bench.actors(), bench.state)

    assert bench.requests() == []


def _made(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    return path


#: More levels than the YAML reader takes, and more digits than the
#: interpreter converts to an integer.
DEEPER_THAN_THE_YAML_READER: Final = 5_000
MORE_DIGITS_THAN_AN_INTEGER: Final = 5_000


def _json(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in loaded.items()}


def test_an_oversized_roster_is_read_as_nothing_served(bench: Bench) -> None:
    """The cap is read, not applied after the read. A roster past it says
    "nothing is served", which asks for a release rather than silently
    keeping a server running."""
    from caregiver.mcp_release import MAX_ROSTER_BYTES, served_servers

    bench.mcp.roster.write_text("a: {}\n" + "#" * MAX_ROSTER_BYTES, encoding="utf-8")

    assert served_servers(bench.mcp) == ()


@pytest.mark.parametrize(
    "text",
    [
        "weather: " + "[" * DEEPER_THAN_THE_YAML_READER + "]" * DEEPER_THAN_THE_YAML_READER + "\n",
        "weather: {port: " + "9" * MORE_DIGITS_THAN_AN_INTEGER + "}\n",
        "weather: {command: \xff}\n",
        "weather: {command: x}\n---\nother: {command: x}\n",
    ],
    ids=["very-deep", "huge-integer", "not-utf8", "two-documents"],
)
def test_a_roster_that_does_not_read_is_nothing_served(bench: Bench, text: str) -> None:
    """A roster that this process cannot read asks for a release, and the
    release writes the roster again. A raise here would stop that repair.

    A roster is one document. The reader does not take the first document
    of a file that holds two."""
    from caregiver.mcp_release import served_servers

    bench.mcp.roster.write_bytes(text.encode("latin-1"))

    assert served_servers(bench.mcp) == ()


def test_a_roster_names_what_is_served(bench: Bench) -> None:
    from caregiver.mcp_release import served_servers

    bench.mcp.roster.write_text("weather: {command: /opt/mcp/weather/bin/x}\n", encoding="utf-8")

    assert served_servers(bench.mcp) == (SERVER,)


def test_a_roster_name_with_no_text_is_nothing_served(bench: Bench) -> None:
    """The name of a row can be an integer that the interpreter does not
    convert to text. The reader refuses that roster, as each roster that
    does not read."""
    from caregiver.mcp_release import served_servers

    number = "0x" + "f" * MORE_DIGITS_THAN_AN_INTEGER
    bench.mcp.roster.write_text(f"weather: &name {number}\n*name : {{}}\n", encoding="utf-8")

    assert served_servers(bench.mcp) == ()


#: The limits of the child process that reads one roster. A reader that
#: refuses the file ends in milliseconds and uses little memory.
CHILD_SECONDS: Final = 30.0
CHILD_BYTES: Final = 256 * 1024 * 1024

#: Reads the roster that the first argument names, and prints the answer.
#: The second argument is the memory limit of the process, in bytes. The
#: process ends with status 3 when it passes the limit.
ROSTER_CHILD: Final = """
import os
import resource
import sys
import threading
import time
from pathlib import Path

LIMIT = int(sys.argv[2])


def peak_bytes():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def watch():
    while peak_bytes() <= LIMIT:
        time.sleep(0.01)

    os._exit(3)


try:
    resource.setrlimit(resource.RLIMIT_AS, (4 * LIMIT, 4 * LIMIT))
except (OSError, ValueError):
    pass

threading.Thread(target=watch, daemon=True).start()

from caregiver.mcp_release import McpPaths, served_servers

print(served_servers(McpPaths(roster=Path(sys.argv[1]))))
"""

#: The count of keys in the mapping that `_merged` copies.
MERGED_KEYS: Final = 256

#: A chain of this many levels copies more pairs than the bound permits.
#: The text of the roster is smaller than 1 KiB.
CHAIN_LEVELS: Final = 26

#: A row that merges itself this many times copies more pairs than the
#: limit permits.
OWN_MERGES: Final = 30

#: The two limits of the roster reader as numbers, not as the names of
#: `mcp_release`. They are the numbers of the Rust reader of a component
#: manifest. A number that changes in `mcp_release` fails a test that uses
#: these.
CHAIN_AT_THE_LIMIT: Final = 128
PAIRS_AT_THE_LIMIT: Final = 65_536


def _merged(times: int) -> str:
    """A roster with one row whose merge key copies `MERGED_KEYS` pairs
    `times` times."""
    keys = ", ".join(f"k{number}: 0" for number in range(MERGED_KEYS))
    aliases = ", ".join(["*base"] * times)

    return f"base: &base {{{keys}}}\nweather: {{<<: [{aliases}]}}\n"


def _nested(levels: int) -> str:
    """A roster with one row whose merge key holds a merge key, `levels`
    levels deep."""
    return "weather: " + "{<<: " * levels + "{command: x}" + "}" * levels + "\n"


def _empty_values(values: int, times: int) -> str:
    """A roster with one row that merges a list of `values` empty mappings
    `times` times."""
    listed = ", ".join(["{}"] * values)
    merges = ", ".join(["<<: *list"] * times)

    return f"list: &list [{listed}]\nweather: {{{merges}}}\n"


def _chain(levels: int, key: str = "<<") -> str:
    """A roster where each row merges the row before it two times. `key`
    is the text of each merge key."""
    rows = ["row0: &row0 {command: x}"]
    rows.extend(
        f"row{level}: &row{level} {{{key}: [*row{level - 1}, *row{level - 1}]}}"
        for level in range(1, levels + 1)
    )

    return "\n".join(rows) + "\n"


def _own_merges(times: int) -> str:
    """A roster with one row that merges itself `times` times."""
    merges = ", ".join(["<<: [*row, *row]"] * times)

    return f"weather: &row {{{merges}, command: x}}\n"


def _linked(links: int) -> str:
    """A roster with `links` merge keys, where each row merges the row
    before it one time. The reader makes each row before the row that
    merges it, so it follows one merge key at a time."""
    lines = ["row0: &row0 {command: x}"]
    lines.extend(f"row{row}: &row{row} {{<<: *row{row - 1}}}" for row in range(1, links))
    lines.append(f"weather: {{<<: *row{links - 1}}}")

    return "\n".join(lines) + "\n"


def _alias_chain(levels: int) -> str:
    """A roster whose last row merges the end of a chain of `levels` merge
    keys. The value of each merge key is an alias, so the chain adds no
    nesting to the text. The rows of the chain are in a list, so the
    reader makes the last row first and follows the full chain.

    The Rust reader of a component manifest has a test of this form at
    the same limit: `a_merge_chain_past_the_depth_limit_is_refused`."""
    chain = "".join(f"  - &m{level} {{<<: *m{level - 1}}}\n" for level in range(1, levels))

    return f"chain:\n  - &m0 {{command: x}}\n{chain}weather: {{<<: *m{levels - 1}}}\n"


def _copied(pairs: int) -> str:
    """A roster with one row whose merge key copies `pairs` pairs."""
    times, rest = divmod(pairs, MERGED_KEYS)
    keys = ", ".join(f"k{number}: 0" for number in range(MERGED_KEYS))
    aliases = ", ".join(["*base"] * times + ["*one"] * rest)

    return f"base: &base {{{keys}}}\none: &one {{k0: 0}}\nweather: {{<<: [{aliases}]}}\n"


def test_a_roster_with_a_merge_key_names_what_is_served(bench: Bench) -> None:
    """A merge key inside the bounds reads as before."""
    from caregiver.mcp_release import served_servers

    bench.mcp.roster.write_text(
        "base: &base {command: /opt/mcp/weather/bin/x}\nweather: {<<: *base}\n", encoding="utf-8"
    )

    assert served_servers(bench.mcp) == ("base", SERVER)


@pytest.mark.parametrize(
    "text",
    [_merged(MERGE_PAIRS_MAX // MERGED_KEYS), _nested(MERGE_DEPTH_MAX)],
    ids=["pairs", "depth"],
)
def test_a_roster_at_a_merge_bound_names_what_is_served(bench: Bench, text: str) -> None:
    from caregiver.mcp_release import served_servers

    bench.mcp.roster.write_text(text, encoding="utf-8")

    assert SERVER in served_servers(bench.mcp)


@pytest.mark.parametrize(
    "text",
    [_merged(MERGE_PAIRS_MAX // MERGED_KEYS + 1), _nested(MERGE_DEPTH_MAX + 1)],
    ids=["pairs", "depth"],
)
def test_a_roster_past_a_merge_bound_is_nothing_served(bench: Bench, text: str) -> None:
    """The refusal is the answer for each roster that does not read."""
    from caregiver.mcp_release import served_servers

    bench.mcp.roster.write_text(text, encoding="utf-8")

    assert served_servers(bench.mcp) == ()


def test_a_merge_of_an_empty_value_counts_against_the_bound(bench: Bench) -> None:
    """An empty value copies no pair, and the reader still does work for
    it. Each one counts as one pair, so the time of a read has a bound."""
    from caregiver.mcp_release import served_servers

    side = 300
    assert side * side > MERGE_PAIRS_MAX
    bench.mcp.roster.write_text(_empty_values(side, side), encoding="utf-8")

    assert served_servers(bench.mcp) == ()


@pytest.mark.parametrize(
    "text",
    [
        _nested(CHAIN_AT_THE_LIMIT),
        _alias_chain(CHAIN_AT_THE_LIMIT),
        _linked(CHAIN_AT_THE_LIMIT + 1),
        _copied(PAIRS_AT_THE_LIMIT),
        _empty_values(MERGED_KEYS, PAIRS_AT_THE_LIMIT // MERGED_KEYS),
    ],
    ids=["chain", "alias-chain", "rows", "pairs", "empty-values"],
)
def test_the_last_roster_inside_a_limit_names_what_is_served(bench: Bench, text: str) -> None:
    """The reader takes a chain of 128 merge keys and 65,536 copied pairs.
    A chain is a merge key that holds a merge key, in the text or through
    an alias. Rows that each merge the row before it make no chain, so 129
    such merge keys read. A merged value with no pair counts as one pair,
    so 65,536 such values read."""
    from caregiver.mcp_release import served_servers

    bench.mcp.roster.write_text(text, encoding="utf-8")

    assert SERVER in served_servers(bench.mcp)


@pytest.mark.parametrize(
    "text",
    [
        _nested(CHAIN_AT_THE_LIMIT + 1),
        _alias_chain(CHAIN_AT_THE_LIMIT + 1),
        _copied(PAIRS_AT_THE_LIMIT + 1),
    ],
    ids=["chain", "alias-chain", "pairs"],
)
def test_the_first_roster_past_a_limit_is_nothing_served(bench: Bench, text: str) -> None:
    """One more merge key in the chain, or one more copied pair, and the
    roster does not read."""
    from caregiver.mcp_release import served_servers

    bench.mcp.roster.write_text(text, encoding="utf-8")

    assert served_servers(bench.mcp) == ()


@pytest.mark.parametrize(
    "text",
    [
        _chain(CHAIN_LEVELS),
        _chain(CHAIN_LEVELS, "!!merge m"),
        _chain(CHAIN_LEVELS) + "---\n*none\n",
        _own_merges(OWN_MERGES),
        _chain(CHAIN_LEVELS) + "# \x00\n",
    ],
    ids=["merge-key", "merge-tag", "second-document", "own-merge", "refused-character"],
)
def test_a_small_roster_cannot_take_the_memory_of_the_reader(bench: Bench, text: str) -> None:
    """The reader refuses the file before its merge keys copy more pairs
    than the bound. A child process reads the file inside a time limit and
    a memory limit, so a reader with no bound fails here and takes no more
    than the limit.

    The cases: a merge key, the merge tag on a key that is not `<<`, a
    second document that does not read, a row that merges itself, and a
    character that the YAML library does not accept. The answer is that
    of each roster that does not read. A reader that raises fails here
    too."""
    bench.mcp.roster.write_text(text, encoding="utf-8")

    done = subprocess.run(
        [sys.executable, "-c", ROSTER_CHILD, str(bench.mcp.roster), str(CHILD_BYTES)],
        capture_output=True,
        text=True,
        timeout=CHILD_SECONDS,
        check=False,
    )

    assert (done.returncode, done.stdout.strip()) == (0, "()"), done.stderr


def test_a_served_server_the_registry_dropped_asks_for_a_release(bench: Bench) -> None:
    """The REMOVE half. A deleted file that asked for nothing would leave
    the tree installed, the roster naming it, and the upstream serving for
    ever."""
    bench.mcp.roster.write_text("weather: {command: /opt/mcp/weather/bin/x}\n", encoding="utf-8")
    (bench.mcp.installed_root / SERVER).mkdir()

    bench.look()

    assert len(bench.requests()) == 1
    assert bench.report().requested == (SERVER,)
