"""The reconciler's half of "adding an MCP server is one action".

`stage7-releases.md` §4.1 step 3. The reconciler sees a declared server that
is not installed and does **exactly two things, and installs nothing**:

1. If a named secret has no value, it opens a secret gap and one push goes
   out (§4.3).
2. It files a release request for `mcp-servers` at `latest`,
   `requested_by: managerd`.

Two rules shape every case below.

- **The reconciler installs nothing and knows no secret value.** It reads a
  NAME's presence, never a value. There is no read verb anywhere in this
  path (§4.3 rule 4).
- **It never floods the spool.** The loop runs every few seconds and §3.2
  rule 8 caps pending requests, so a second request waits for the first to
  be ledgered or to age out. A reconciler that filed one per tick would
  refuse itself.

The gap goes into a file root drains, and NOT to the phone directly. Root
mints the token, so the token never passes through an operator-side process —
which is the whole reason §4.3 step 2 puts the phone between them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from agent_managerd.mcp_release import (
    GAP_SUFFIX,
    MAX_REQUEST_AGE_S,
    REQUEST_COMPONENT,
    McpPaths,
    open_gaps,
    paths_under,
    request_servers,
)
from agent_managerd.paths import RELEASE_ROOT, STATE_ROOT

NOW: Final = 1_758_153_590.0
SERVER: Final = "weather"
SECRET: Final = "weather_token"


@pytest.fixture
def paths(tmp_path: Path) -> McpPaths:
    for part in ("releases/requests", "releases/done", "secrets", "secrets/gaps", "mcp"):
        (tmp_path / part).mkdir(parents=True)

    return McpPaths(
        requests=tmp_path / "releases/requests",
        done=tmp_path / "releases/done",
        secrets=tmp_path / "secrets",
        gaps=tmp_path / "secrets/gaps",
        installed_root=tmp_path / "mcp",
        marker=tmp_path / "mcp-request.json",
    )


def _requests(paths: McpPaths) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for path in sorted(paths.requests.iterdir()):
        loaded: object = json.loads(path.read_text("utf-8"))
        assert isinstance(loaded, dict)
        out.append(loaded)

    return out


# -- the request (§4.1 step 3) -------------------------------------------


def test_a_declared_server_that_is_not_installed_is_requested(paths: McpPaths) -> None:
    assert request_servers(paths, (SERVER,), NOW).servers == (SERVER,)
    filed = _requests(paths)

    assert len(filed) == 1
    assert filed[0]["components"] == {REQUEST_COMPONENT: "latest"}
    assert filed[0]["requested_by"] == "managerd"
    assert filed[0]["kind"] == "release"


def test_the_request_file_is_named_after_its_id(paths: McpPaths) -> None:
    request_servers(paths, (SERVER,), NOW)
    path = next(iter(paths.requests.iterdir()))
    filed = _requests(paths)

    assert path.name == f"{filed[0]['id']}.json"


def test_no_partial_file_is_left_for_the_path_unit(paths: McpPaths) -> None:
    """§2.2: a writer renames `.<ulid>.tmp` into `<ulid>.json`, and a name
    that does not end in `.json` neither matches the glob nor is touched."""
    request_servers(paths, (SERVER,), NOW)

    assert [path.suffix for path in paths.requests.iterdir()] == [".json"]


def test_an_installed_server_is_requested_for_nothing(paths: McpPaths) -> None:
    (paths.installed_root / SERVER).mkdir()

    assert request_servers(paths, (SERVER,), NOW).servers == ()
    assert _requests(paths) == []


def test_a_second_pass_does_not_file_a_second_request(paths: McpPaths) -> None:
    """The loop runs every few seconds. One request per gap, not per tick."""
    request_servers(paths, (SERVER,), NOW)
    request_servers(paths, (SERVER,), NOW + 1.0)

    assert len(_requests(paths)) == 1


def test_a_ledgered_request_is_an_answer_and_holds_the_next_one(paths: McpPaths) -> None:
    """A second request one second later would be a loop: root refuses
    `mcp-servers=latest`, writes `done/<id>.json`, pushes the refusal to a
    phone, and the next pass asks the same question again. A ledgered
    request is an ANSWER (`mcp_release` rule 3), and the answer holds until
    something that could change it has changed.
    `test_managerd_mrq_refused_answer` is the whole case.
    """
    request_servers(paths, (SERVER,), NOW)
    filed = _requests(paths)[0]
    (paths.done / f"{filed['id']}.json").write_text("{}", "utf-8")
    request_servers(paths, (SERVER,), NOW + 1.0)

    assert len(_requests(paths)) == 1


def test_a_request_still_in_the_spool_is_waited_on_past_the_hour(paths: McpPaths) -> None:
    """Root takes a request out of `requests/` the moment it starts it, so
    a file still there is untaken, not lost — the path unit is off, or the
    executor is down — and a second copy would only be refused behind it."""
    request_servers(paths, (SERVER,), NOW)
    outcome = request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S + 1.0)

    assert len(_requests(paths)) == 1
    assert outcome.servers == ()
    assert outcome.queued.startswith(
        f"mcp-servers request {_requests(paths)[0]['id']} has waited 1 h"
    )


def test_a_request_root_took_and_never_ledgered_ages_out(paths: McpPaths) -> None:
    """A request root took and never answered must not wedge the path for
    ever: gone from `requests/`, nothing in `done/`, an hour on."""
    request_servers(paths, (SERVER,), NOW)
    taken = _requests(paths)[0]["id"]
    (paths.requests / f"{taken}.json").unlink()
    request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S + 1.0)

    assert [one["id"] for one in _requests(paths)] != [taken]
    assert len(_requests(paths)) == 1


def test_two_missing_servers_ride_one_request(paths: McpPaths) -> None:
    """`mcp-servers` is one component (contract 06 §7), so one release
    installs every declared server the host is missing."""
    assert request_servers(paths, ("weather", "tides"), NOW).servers == ("tides", "weather")
    assert len(_requests(paths)) == 1


def test_nothing_missing_files_nothing(paths: McpPaths) -> None:
    assert request_servers(paths, (), NOW).servers == ()
    assert _requests(paths) == []


# -- the secret gap (§4.1 step 3, §4.3 step 1) ---------------------------


def test_a_secret_with_no_value_opens_a_gap(paths: McpPaths) -> None:
    assert open_gaps(paths, {SERVER: (SECRET,)}, NOW) == (SECRET,)
    body: object = json.loads((paths.gaps / f"{SECRET}{GAP_SUFFIX}").read_text("utf-8"))

    assert isinstance(body, dict)
    assert body["server"] == SERVER
    assert body["secret"] == SECRET


def test_a_secret_that_has_a_value_opens_no_gap(paths: McpPaths) -> None:
    (paths.secrets / f"{SECRET}.enc").write_bytes(b"sealed")

    assert open_gaps(paths, {SERVER: (SECRET,)}, NOW) == ()


def test_a_gap_that_is_already_open_is_not_reopened(paths: McpPaths) -> None:
    open_gaps(paths, {SERVER: (SECRET,)}, NOW)
    before = (paths.gaps / f"{SECRET}{GAP_SUFFIX}").read_bytes()
    open_gaps(paths, {SERVER: (SECRET,)}, NOW + 1.0)

    assert (paths.gaps / f"{SECRET}{GAP_SUFFIX}").read_bytes() == before


def test_the_gap_carries_no_value_anywhere(paths: McpPaths) -> None:
    """Rule 4. The reconciler never holds a value, so it cannot write one."""
    open_gaps(paths, {SERVER: (SECRET,)}, NOW)
    body = (paths.gaps / f"{SECRET}{GAP_SUFFIX}").read_text("utf-8")

    assert "value" not in body


def test_a_shared_secret_opens_one_gap_and_is_reported_once(paths: McpPaths) -> None:
    """Contract 01b §4.3 rule 5.

    `ha` and `ha-read` hold one Home Assistant token on purpose, so this
    map carries one name under two servers. There is one value to paste
    and one link to push, so there is one gap and one line in the report.
    """
    opened = open_gaps(paths, {"ha-read": ("ha_read_token",), "ha": ("ha_read_token",)}, NOW)

    assert opened == ("ha_read_token",)
    assert sorted(one.name for one in paths.gaps.iterdir()) == [f"ha_read_token{GAP_SUFFIX}"]


def test_the_shared_gap_names_the_pair_s_first_server_every_pass(paths: McpPaths) -> None:
    """The gap file carries one server name, so which one it carries must
    not depend on the order the registry walk happened to hand over."""
    open_gaps(paths, {"ha-read": ("ha_read_token",), "ha": ("ha_read_token",)}, NOW)
    body: object = json.loads((paths.gaps / f"ha_read_token{GAP_SUFFIX}").read_text("utf-8"))

    assert isinstance(body, dict)
    assert body["server"] == "ha"


def test_a_secret_name_outside_its_pattern_opens_no_gap(paths: McpPaths) -> None:
    """A gap name becomes a file name that root reads next."""
    assert open_gaps(paths, {SERVER: ("../../etc/shadow",)}, NOW) == ()
    assert list(paths.gaps.iterdir()) == []


def test_one_server_may_need_two_secrets(paths: McpPaths) -> None:
    assert open_gaps(paths, {SERVER: (SECRET, "weather_extra")}, NOW) == (
        SECRET,
        "weather_extra",
    )


def test_a_gap_is_opened_for_an_installed_server_too(paths: McpPaths) -> None:
    """A secret can be revoked and removed after the install. The server
    then runs `running: false` (contract 01b §4.1 rule 4) and the operator needs
    the same push they would have got the first time."""
    (paths.installed_root / SERVER).mkdir()

    assert open_gaps(paths, {SERVER: (SECRET,)}, NOW) == (SECRET,)


# -- the two roots ----------------------------------------


def test_paths_under_puts_roots_files_under_roots_root(tmp_path: Path) -> None:
    """The spool, the sealed secrets and the roster are ROOT's, so they
    come from the release root. The gaps and the marker are the operator's own,
    so they stay under the operator's state root. Mixing the two back up is how
    an operator-renameable directory would sit above root's spool again."""
    state = tmp_path / "state"
    release = tmp_path / "release"

    found = paths_under(state, release)

    for roots_own in (found.requests, found.done, found.secrets, found.roster):
        assert roots_own.is_relative_to(release), roots_own

    for operators_own in (found.gaps, found.marker):
        assert operators_own.is_relative_to(state), operators_own


def test_the_host_defaults_put_nothing_of_roots_under_the_state_root() -> None:
    defaults = McpPaths()

    for roots_own in (defaults.requests, defaults.done, defaults.secrets, defaults.roster):
        assert roots_own.is_relative_to(RELEASE_ROOT), roots_own
        assert not roots_own.is_relative_to(STATE_ROOT), roots_own
