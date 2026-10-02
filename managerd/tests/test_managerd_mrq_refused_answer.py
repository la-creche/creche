"""A refused request is an answer, not a reason to ask again.

`request_servers` files `{"mcp-servers": "latest"}` when no declared
server is installed under `/opt/mcp`. Root can drain the spool, REFUSE it
(`mcp-servers=latest names no released tag: name a version instead`),
write `done/<id>.json` and push the refusal to the operator's phone. A pass that
treated the request as settled the moment that file existed would file
the SAME request again.

With the path unit disabled that costs one push per manual start. With
the path unit enabled it is a loop at the reconciler's cadence, to a
phone. This module is what makes that impossible.

Three rules the tests below hold to.

1. **An answer is an answer.** `done/<id>.json` settles the request AND
   holds the next identical one back.
2. **Something must have changed**: the set of servers the request is
   for, the registry revision, or the floor's 24 hours.
3. **Every marker field is hostile.** None may stop the path for ever,
   and none may make it loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import pytest
import yaml
from agent_managerd.driver import FakeDriver
from agent_managerd.egress import EgressConfig
from agent_managerd.litellm_keys import FakeLiteLLMKeys
from agent_managerd.loop import LoopConfig, LoopState, look
from agent_managerd.mcp_release import (
    ANSWER_FLOOR_S,
    MAX_REASON_CHARS,
    MAX_REQUEST_AGE_S,
    UNKNOWN_STATUS,
    McpPaths,
    RequestOutcome,
    request_servers,
)
from agent_managerd.mcp_wire import MCP_INSTALL_HELD, McpReport
from agent_managerd.paths import status_path
from agent_managerd.reconcile import Actors
from agent_managerd.switch import FakeSwitchClient
from agent_managerd.timers import FakeUnits
from managerd_helpers import write_registry

NOW: Final = 1_758_153_590.0
SERVER: Final = "weather"
OTHER: Final = "tides"
REVISION: Final = "rev-aaaa"

#: What root writes for that refusal, word for word.
ROOT_REASON: Final = "mcp-servers=latest names no released tag: name a version instead"
ROOT_CHECK: Final = "request"


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
        roster=tmp_path / "upstreams.yaml",
    )


def _filed(paths: McpPaths) -> list[str]:
    """Every request id in the spool, oldest first. The file name is the
    id (§2.2), so no file has to be opened to count them."""
    return sorted(one.stem for one in paths.requests.iterdir())


def _answer(
    paths: McpPaths,
    request_id: str,
    status: str = "refused",
    reason: str = ROOT_REASON,
) -> None:
    """Root's own ledger entry, in the shape `executor/ledger.py` writes."""
    body: dict[str, object] = {
        "id": request_id,
        "status": status,
        "kind": "release",
        "requested_by": "managerd",
        "refused_check": ROOT_CHECK if status == "refused" else None,
        "reason": reason,
        "manifest": None,
        "steps": [],
        "log_tail": [],
    }
    (paths.done / f"{request_id}.json").write_text(json.dumps(body), "utf-8")


def _marker(paths: McpPaths) -> dict[str, object]:
    loaded: object = json.loads(paths.marker.read_text("utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in loaded.items()}


def _write_marker(paths: McpPaths, body: dict[str, object]) -> None:
    paths.marker.write_text(json.dumps(body), "utf-8")


def _taken(paths: McpPaths) -> None:
    """Root's `spool.start`: the request leaves `requests/` the moment root
    takes it, before any ledger entry exists. The fake answer above writes
    the entry only, so a test about a request root TOOK says so here."""
    for one in paths.requests.iterdir():
        one.unlink()


def _hostile_answer(paths: McpPaths, request_id: str, at: object) -> None:
    """A marker whose recorded answer carries an `at` nobody should
    believe. Everything else in it is well formed, so the test measures
    that one field."""
    _write_marker(
        paths,
        {
            "id": request_id,
            "servers": [SERVER],
            "revision": REVISION,
            "at": NOW,
            "answered": {"status": "refused", "at": at, "reason": "", "check": ""},
        },
    )


def _refused_once(paths: McpPaths, now: float = NOW) -> str:
    """File one request and have root refuse it. Answers the request id."""
    outcome = request_servers(paths, (SERVER,), now, REVISION)
    assert outcome.servers == (SERVER,)
    request_id = _filed(paths)[0]
    _answer(paths, request_id)

    return request_id


def _read_the_answer(paths: McpPaths, at: float) -> float:
    """One pass that reads root's answer. Answers the time the floor
    runs from, which is when THIS process read the entry, never a time
    root stamped."""
    assert request_servers(paths, (SERVER,), at, REVISION).held is not None

    return at


# -- rule 1: the answer holds the next request ---------------------------


def test_a_refused_request_is_not_filed_again(paths: McpPaths) -> None:
    """THE LOOP. Two seconds after the refusal the old code filed the
    same request again, and root pushed the same refusal to a phone."""
    _refused_once(paths)

    outcome = request_servers(paths, (SERVER,), NOW + 2.0, REVISION)

    assert outcome.servers == ()
    assert len(_filed(paths)) == 1


def test_the_held_request_names_root_s_reason_and_the_ledger_id(paths: McpPaths) -> None:
    """The reason as root wrote it, and the ledger id."""
    request_id = _refused_once(paths)

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert held.request_id == request_id
    assert held.status == "refused"
    assert held.reason == ROOT_REASON
    assert held.check == ROOT_CHECK
    assert held.servers == (SERVER,)


def test_a_failed_release_holds_the_next_request_too(paths: McpPaths) -> None:
    """A failure is root's answer as much as a refusal is."""
    outcome = request_servers(paths, (SERVER,), NOW, REVISION)
    assert outcome.servers == (SERVER,)
    _answer(paths, _filed(paths)[0], status="failed", reason="stage: venv build exited 1")

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert held.status == "failed"
    assert len(_filed(paths)) == 1


def test_a_succeeded_release_that_left_the_server_missing_holds_too(paths: McpPaths) -> None:
    """The other answer. The release RAN and the host
    still does not match the registry, so the released `mcp-servers`
    does not carry this server. Asking again at `latest` runs the same
    release and installs the same tree, so it is an answer as well — a
    different one, because the fix is a tag in agent-mcp, not a retry."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    _answer(paths, _filed(paths)[0], status="succeeded", reason="")

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert held.status == "succeeded"
    assert held.servers == (SERVER,)
    assert len(_filed(paths)) == 1


# -- rule 2: what "changed" means ----------------------------------------


def test_a_newly_declared_server_asks_again(paths: McpPaths) -> None:
    """The set the request is FOR moved, so the answer was about a
    different question."""
    _refused_once(paths)

    outcome = request_servers(paths, (SERVER, OTHER), NOW + 2.0, REVISION)

    assert outcome.servers == (OTHER, SERVER)
    assert len(_filed(paths)) == 2


def test_a_server_that_arrived_asks_again_for_the_rest(paths: McpPaths) -> None:
    """Half the request landed. The remainder is a different question."""
    request_servers(paths, (SERVER, OTHER), NOW, REVISION)
    _answer(paths, _filed(paths)[0], status="succeeded", reason="")
    (paths.installed_root / OTHER).mkdir()

    outcome = request_servers(paths, (SERVER, OTHER), NOW + 2.0, REVISION)

    assert outcome.servers == (SERVER,)
    assert len(_filed(paths)) == 2


def test_a_moved_registry_revision_asks_again(paths: McpPaths) -> None:
    """The operator edited `mcp/weather/server.yaml`: a corrected pin, a
    rebuilt lock. The names did not move and the answer may have."""
    _refused_once(paths)

    outcome = request_servers(paths, (SERVER,), NOW + 2.0, "rev-bbbb")

    assert outcome.servers == (SERVER,)
    assert len(_filed(paths)) == 2


def test_the_floor_asks_again_with_no_person(paths: McpPaths) -> None:
    """A tag appearing in agent-mcp moves neither the names nor the
    registry revision, and this host watches that repository not at all.
    The floor is what heals it without a human."""
    _refused_once(paths)
    read_at = _read_the_answer(paths, NOW + 2.0)

    assert request_servers(paths, (SERVER,), read_at + ANSWER_FLOOR_S - 1.0, REVISION).servers == ()
    assert request_servers(paths, (SERVER,), read_at + ANSWER_FLOOR_S + 1.0, REVISION).servers == (
        SERVER,
    )


def test_nothing_missing_holds_nothing(paths: McpPaths) -> None:
    """A host that matches the registry has no held request to report,
    whatever the marker still says."""
    _refused_once(paths)
    (paths.installed_root / SERVER).mkdir()

    outcome = request_servers(paths, (SERVER,), NOW + 2.0, REVISION)

    assert outcome.servers == ()
    assert outcome.held is None


# -- rule 3 boundary: no answer yet, against answered ---------------------


def test_an_unanswered_request_still_ages_out(paths: McpPaths) -> None:
    """Root took the request and never answered — a crash
    between `running/` and the ledger — and a lost request must not stop
    every future install."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    _taken(paths)

    assert request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S - 1.0, REVISION).servers == ()
    assert request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S + 1.0, REVISION).servers == (
        SERVER,
    )


def test_an_untaken_request_is_waited_on_and_said(paths: McpPaths) -> None:
    """With the path unit off, an hour rule alone would file a copy of the
    same request every hour. Root drains in id order, so every copy after
    the first would be refused (`the request moves no component`) with one
    phone push each. A request still in `requests/` is untaken, not lost:
    the wait goes on, and is said from the hour on, in whole hours."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    request_id = _filed(paths)[0]

    early = request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S - 1.0, REVISION)
    late = request_servers(paths, (SERVER,), NOW + 3 * MAX_REQUEST_AGE_S + 1.0, REVISION)

    assert early == RequestOutcome()
    assert late.servers == ()
    assert late.held is None
    assert late.queued == (
        f"mcp-servers request {request_id} has waited 3 h in the spool untaken: "
        "the release path is not draining"
    )
    assert _filed(paths) == [request_id]


def test_an_answered_request_is_not_retried_at_the_unanswered_bound(paths: McpPaths) -> None:
    """Two bounds. An hour is how long "no answer
    yet" waits. An ANSWER waits the floor, which is a day."""
    _refused_once(paths)

    assert request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S + 1.0, REVISION).servers == ()


def test_an_unanswered_request_reports_nothing_held(paths: McpPaths) -> None:
    """Waiting is not being held back: root has said nothing yet, so
    there is nothing for a status document to name."""
    request_servers(paths, (SERVER,), NOW, REVISION)

    assert request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held is None


# -- rule 3: every marker field is hostile -------------------------------


def test_a_marker_dated_in_the_future_does_not_suppress_every_request(paths: McpPaths) -> None:
    """`now - at` is negative for every future `at`, so one such file must
    not wedge the path for ever."""
    _write_marker(paths, {"id": "AAAAAAAAAAAAAAAAAAAAAAAAAA", "at": NOW + 1e12})

    assert request_servers(paths, (SERVER,), NOW, REVISION).servers == (SERVER,)


def test_a_marker_naming_no_ulid_does_not_suppress_every_request(paths: McpPaths) -> None:
    _write_marker(paths, {"id": "not-a-ulid", "at": NOW})

    assert request_servers(paths, (SERVER,), NOW, REVISION).servers == (SERVER,)


def test_an_answer_dated_in_the_future_is_dropped_for_root_s_own_entry(
    paths: McpPaths,
) -> None:
    """The floor is `now - answered.at`. A future stamp makes that
    negative and a negative age is under every cap, so the field is not
    believed: the pass re-reads root's ledger entry and stamps the floor
    with its own clock."""
    request_id = _refused_once(paths)
    _hostile_answer(paths, request_id, at=NOW + 1e12)

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert held.at == NOW + 2.0
    assert held.reason == ROOT_REASON


def test_an_answer_stamped_with_a_string_is_dropped_the_same_way(paths: McpPaths) -> None:
    request_id = _refused_once(paths)
    _hostile_answer(paths, request_id, at="soon")

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert held.at == NOW + 2.0


def test_a_future_stamp_with_no_entry_left_expires_at_the_unanswered_bound(
    paths: McpPaths,
) -> None:
    """The worst a hostile stamp can do. Root pruned `done/`, so nothing
    replaces the field, and the pass falls back to "no answer yet" —
    which, for a request root took, ages out in an hour and asks again."""
    request_id = _refused_once(paths)
    _taken(paths)
    _hostile_answer(paths, request_id, at=NOW + 1e12)
    (paths.done / f"{request_id}.json").unlink()

    assert request_servers(paths, (SERVER,), NOW + 2.0, REVISION).servers == ()
    assert request_servers(paths, (SERVER,), NOW + MAX_REQUEST_AGE_S + 1.0, REVISION).servers == (
        SERVER,
    )


def test_a_long_reason_is_cut_to_the_cap(paths: McpPaths) -> None:
    """Root's reason is root-written and is still DATA. It reaches a log
    line, a status document and the one view."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    _answer(paths, _filed(paths)[0], reason="x" * 4000)

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert len(held.reason) == MAX_REASON_CHARS
    assert _marker(paths)["answered"] == {
        "at": NOW + 2.0,
        "check": ROOT_CHECK,
        "reason": "x" * MAX_REASON_CHARS,
        "status": "refused",
    }


def test_a_reason_holding_control_characters_is_made_printable(paths: McpPaths) -> None:
    """A newline in a report line is a second line a reader may believe."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    _answer(paths, _filed(paths)[0], reason="refused\nmanagerd: everything is fine")

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert "\n" not in held.reason
    assert held.reason.startswith("refused")


def test_a_status_of_the_wrong_type_still_holds_the_request(paths: McpPaths) -> None:
    """An unknown status is still an answer: root wrote the entry."""
    request_id = _refused_once(paths)
    _write_marker(
        paths,
        {
            "id": request_id,
            "servers": [SERVER],
            "revision": REVISION,
            "at": NOW,
            "answered": {"status": {"nested": 1}, "at": NOW, "reason": "", "check": ""},
        },
    )

    outcome = request_servers(paths, (SERVER,), NOW + 2.0, REVISION)

    assert outcome.servers == ()
    assert outcome.held is not None
    assert outcome.held.status == UNKNOWN_STATUS


def test_a_marker_whose_servers_are_not_a_list_asks_again(paths: McpPaths) -> None:
    """An unreadable set is a set that cannot match today's, so the path
    asks. It never goes quiet."""
    request_id = _refused_once(paths)
    _write_marker(
        paths,
        {
            "id": request_id,
            "servers": "weather",
            "revision": REVISION,
            "at": NOW,
            "answered": {"status": "refused", "at": NOW, "reason": "", "check": ""},
        },
    )

    assert request_servers(paths, (SERVER,), NOW + 2.0, REVISION).servers == (SERVER,)


def test_a_marker_holding_ten_thousand_server_names_asks_again(paths: McpPaths) -> None:
    """The list is read with a cap, and a capped list cannot equal
    today's set, so the pass asks rather than believing it."""
    request_id = _refused_once(paths)
    _write_marker(
        paths,
        {
            "id": request_id,
            "servers": [f"s{index}" for index in range(10_000)],
            "revision": REVISION,
            "at": NOW,
            "answered": {"status": "refused", "at": NOW, "reason": "", "check": ""},
        },
    )

    assert request_servers(paths, (SERVER,), NOW + 2.0, REVISION).servers == (SERVER,)


def test_a_marker_with_no_revision_asks_again(paths: McpPaths) -> None:
    """A field that is absent or of the wrong type reads as "not the
    revision this pass is on"."""
    request_id = _refused_once(paths)
    _write_marker(
        paths,
        {
            "id": request_id,
            "servers": [SERVER],
            "at": NOW,
            "answered": {"status": "refused", "at": NOW, "reason": "", "check": ""},
        },
    )

    assert request_servers(paths, (SERVER,), NOW + 2.0, REVISION).servers == (SERVER,)


def test_one_hostile_marker_costs_one_request_and_no_more(paths: McpPaths) -> None:
    """The reason no hostile value can make the path LOOP: filing a
    request rewrites the marker, so the reconciler's own file takes over
    on the next pass."""
    _write_marker(paths, {"id": "not-a-ulid", "at": NOW})

    assert request_servers(paths, (SERVER,), NOW, REVISION).servers == (SERVER,)
    assert request_servers(paths, (SERVER,), NOW + 2.0, REVISION).servers == ()
    assert len(_filed(paths)) == 1


# -- the ledger entry is hostile input as well ---------------------------


def test_a_half_written_ledger_entry_neither_raises_nor_floods(paths: McpPaths) -> None:
    """`live_manifest.py`'s rule, in a new place. The entry EXISTS, so
    root settled the request, and this pass could not read what it said."""
    request_id = request_servers(paths, (SERVER,), NOW, REVISION).servers and _filed(paths)[0]
    assert isinstance(request_id, str)
    (paths.done / f"{request_id}.json").write_text('{"id": "AAA', "utf-8")

    outcome = request_servers(paths, (SERVER,), NOW + 2.0, REVISION)

    assert outcome.servers == ()
    assert outcome.held is not None
    assert outcome.held.status == UNKNOWN_STATUS


def test_a_ledger_entry_that_becomes_readable_reports_root_s_words(paths: McpPaths) -> None:
    """A half write resolves on the next pass, two seconds later, and
    nothing was written down in the meantime."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    request_id = _filed(paths)[0]
    (paths.done / f"{request_id}.json").write_text('{"id": "AAA', "utf-8")
    request_servers(paths, (SERVER,), NOW + 2.0, REVISION)
    _answer(paths, request_id)

    held = request_servers(paths, (SERVER,), NOW + 4.0, REVISION).held

    assert held is not None
    assert held.reason == ROOT_REASON


def test_a_ledger_entry_bigger_than_the_cap_is_not_read(paths: McpPaths) -> None:
    """An unbounded read on a loop that runs every two seconds is a way
    to spend the manager."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    request_id = _filed(paths)[0]
    body = {"id": request_id, "status": "refused", "reason": "x" * (2 << 20)}
    (paths.done / f"{request_id}.json").write_text(json.dumps(body), "utf-8")

    held = request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held

    assert held is not None
    assert held.status == UNKNOWN_STATUS


# -- the marker is written before the request ----------------------------


def test_no_request_is_filed_when_the_marker_cannot_be_written(paths: McpPaths) -> None:
    """A request that lands with no marker behind it is the loop again:
    the next pass finds no marker and files the same request. So the
    marker goes down FIRST, and a marker this process cannot write costs
    one report line and no spool file at all."""
    paths.marker.mkdir()

    with pytest.raises(OSError):
        request_servers(paths, (SERVER,), NOW, REVISION)

    assert _filed(paths) == []


# -- the number, over a simulated day ------------------------------------


class RefusingRoot:
    """A root that drains the spool and refuses everything, at once.

    It answers every request with the
    same sentence, writes `done/<id>.json`, and pushes the refusal to a
    phone. `pushes` is what a person would have received.
    """

    def __init__(self, paths: McpPaths) -> None:
        self._paths = paths
        self.pushes = 0

    def drain(self) -> None:
        """`stage7-releases.md` §2.2: a run ends with every entry GONE
        from `requests/`, ledgered or rejected. So the spool cannot tell
        the reconciler what it already asked — only the marker can."""
        for path in sorted(self._paths.requests.iterdir()):
            _answer(self._paths, path.stem)
            path.unlink()
            self.pushes += 1


def _simulate(paths: McpPaths, days: int, step_s: float) -> RefusingRoot:
    """Run the pass at `step_s` for `days`, with nothing changing."""
    root = RefusingRoot(paths)
    passes = int((days * 86_400.0) / step_s)
    for tick in range(passes):
        request_servers(paths, (SERVER,), NOW + (tick * step_s), REVISION)
        root.drain()

    return root


def test_a_standing_refusal_costs_one_request_a_day(paths: McpPaths) -> None:
    """4320 passes is one day at the 20-second heartbeat, which is how
    often `loop._finish` reaches the MCP pass on an idle fleet. Without the
    hold every one of them would file a request and earn a phone push. A
    day of an unchanged, standing refusal costs ONE.
    """
    root = _simulate(paths, days=1, step_s=20.0)

    assert root.pushes == 1
    assert _filed(paths) == []


def test_three_days_of_the_same_refusal_cost_three_requests(paths: McpPaths) -> None:
    """The floor retries without a person, and never in a loop."""
    root = _simulate(paths, days=3, step_s=20.0)

    assert root.pushes == 3


def test_the_two_second_tick_costs_the_same_one_request_a_day(paths: McpPaths) -> None:
    """The cadence does not change the answer: the floor is a clock, not
    a counter of passes."""
    root = _simulate(paths, days=1, step_s=120.0)

    assert root.pushes == 1


def test_one_day_of_passes_costs_one_marker_write_per_answer(paths: McpPaths) -> None:
    """The floor's other job. The marker records the answer, so the
    ledger entry — a whole resolved manifest and a 200-line log tail —
    is read once per answer and not 4320 times a day."""
    _simulate(paths, days=1, step_s=20.0)

    assert isinstance(_marker(paths).get("answered"), dict)


def test_the_marker_carries_the_answer_so_the_ledger_is_read_once(paths: McpPaths) -> None:
    """The entry holds a whole resolved manifest and a 200-line log tail.
    Re-reading it every two seconds for a day is 43200 reads of up to a
    megabyte for one sentence that cannot change."""
    _refused_once(paths)
    request_servers(paths, (SERVER,), NOW + 2.0, REVISION)

    answered = _marker(paths).get("answered")

    assert isinstance(answered, dict)
    assert answered["reason"] == ROOT_REASON


# -- where the operator sees it: contract 05 §3.3 ------------------------


def _held_report(paths: McpPaths) -> McpReport:
    """One held-back request, as `mcp_wire` would report it."""
    _refused_once(paths)

    return McpReport(held=request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held)


def test_a_held_request_is_a_non_blocking_fault(paths: McpPaths) -> None:
    """A missing upstream is one tool fewer, not eleven families off the
    air. §3.3's `blocks_turns` column is fixed per code."""
    fault = _held_report(paths).fault()

    assert fault is not None
    assert fault.code == MCP_INSTALL_HELD
    assert fault.blocks_turns is False
    assert fault.source == "managerd"


def test_the_fault_names_root_s_reason_and_the_ledger_id(paths: McpPaths) -> None:
    """The reason and the ledger id, in the place a reader looks."""
    report = _held_report(paths)
    held = report.held
    fault = report.fault()

    assert held is not None
    assert fault is not None
    assert fault.detail["release_id"] == held.request_id
    assert ROOT_REASON in str(fault.detail["message"])
    assert held.request_id in str(fault.detail["message"])
    assert SERVER in str(fault.detail["message"])


def test_the_fault_says_when_the_request_goes_out_again(paths: McpPaths) -> None:
    """A held request with no retry time reads as "never", and a reader
    who believes that files a ticket instead of waiting a day."""
    fault = _held_report(paths).fault()

    assert fault is not None
    assert fault.since == "2025-09-17T23:59:52Z"
    assert fault.detail["retry_after"] == "2025-09-18T23:59:52Z"


def test_a_succeeded_release_gets_its_own_sentence(paths: McpPaths) -> None:
    """The repair differs, so the sentence differs: a tag in agent-mcp,
    not a retry of the same release."""
    request_servers(paths, (SERVER,), NOW, REVISION)
    _answer(paths, _filed(paths)[0], status="succeeded", reason="")
    report = McpReport(held=request_servers(paths, (SERVER,), NOW + 2.0, REVISION).held)

    assert "succeeded and weather is still not installed" in report.held_line()
    assert "does not carry it" in report.held_line()


def test_two_held_servers_read_as_a_plural(paths: McpPaths) -> None:
    """A sentence a reader trips over is a sentence a reader rereads."""
    request_servers(paths, (SERVER, OTHER), NOW, REVISION)
    _answer(paths, _filed(paths)[0])
    report = McpReport(held=request_servers(paths, (SERVER, OTHER), NOW + 2.0, REVISION).held)

    assert "tides, weather are still not installed" in report.held_line()


def test_nothing_held_raises_no_fault(paths: McpPaths) -> None:
    assert McpReport().fault() is None
    assert McpReport().held_line() == ""


def test_the_held_line_reaches_the_loop_s_log_line(paths: McpPaths) -> None:
    """`log_line` is what `loop._mcp_servers` writes. A held request that
    only ever reached a document would be invisible in the journal, and
    the journal is where an operator reads a sequence."""
    assert ROOT_REASON in _held_report(paths).log_line()


# -- the whole path, through the real loop -------------------------------


class Bench:
    """A registry the loop watches, a spool root refuses from, and the
    status documents the operator reads. Every test below drives `loop.look`."""

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
            marker=tmp_path / "state" / "mcp-request.json",
            roster=_made(tmp_path / "state") / "upstreams.yaml",
        )
        self.state = LoopState()
        self.root = RefusingRoot(self.mcp)
        self.now = NOW

    def looks(self, count: int, step_s: float = 20.0) -> None:
        """`count` looks, one step of the clock apart.

        Three is the smallest number that publishes a refusal, and the
        shape is the reason: the MCP pass runs AFTER the families
        (`loop._finish`), so look 1 files the request, look 2 reads
        root's answer, and look 3 hands it to each family. On the host
        look 3 follows look 2 by one poll interval, because a moved
        verdict calls `LoopState.force_pass`.
        """
        for tick in range(count):
            self.now = NOW + (tick * step_s)
            self.look()

    def look(self) -> None:
        """One real pass of the loop, registry read and all, then root
        drains whatever landed in the spool."""
        look(
            LoopConfig(
                registry_root=self.registry_root,
                state_root=self.state_root,
                image="sha256:deadbeef",
                mcp=self.mcp,
                clock=lambda: self.now,
                # Every look passes every family. The real gate is
                # 20 seconds of `time.monotonic`, which a test that
                # moves a fake wall clock cannot reach — and what is
                # under test is what each pass PUBLISHES, not when the
                # heartbeat lets it.
                heartbeat_s=0.0,
            ),
            Actors(
                FakeDriver(), FakeLiteLLMKeys(), FakeSwitchClient(), EgressConfig(), FakeUnits()
            ),
            self.state,
        )
        self.root.drain()

    def declare(self, name: str) -> None:
        directory = self.registry_root / "mcp" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "server.yaml").write_text(_server_yaml(name), encoding="utf-8")

    def document(self, family: str = "chat") -> dict[str, object]:
        loaded: object = json.loads(status_path(self.state_root, family).read_text("utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in loaded.items()}

    def faults(self, family: str = "chat") -> list[dict[str, object]]:
        found = self.document(family).get("faults")
        assert isinstance(found, list)

        return [one for one in cast("list[object]", found) if isinstance(one, dict)]


def _made(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    return path


def _server_yaml(name: str) -> str:
    """Contract 01b §8.1, shortened to what this wire reads."""
    return yaml.safe_dump(
        {
            "name": name,
            "identity": f"The fleet's {name} credential. Read only.",
            "install": {
                "source": "pypi",
                "package": f"{name}-mcp",
                "version": "1.0.0",
                "lock": f"mcp/{name}/install.lock",
                "python": "3.12",
            },
            "run": {"entrypoint": f"{name}-mcp", "env": {"URL": "https://example.invalid"}},
            "tools": [{"name": "forecast", "description": "Tomorrow's weather, by city."}],
        }
    )


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


def test_the_loop_files_one_request_and_root_refuses_it_once(bench: Bench) -> None:
    """The refusal loop, in a harness. Five looks, five chances
    to ask the same question again, and one push."""
    bench.declare(SERVER)
    bench.looks(5)

    assert bench.root.pushes == 1


def test_operator_reads_the_held_request_in_the_status_document(bench: Bench) -> None:
    """Visible without a journal."""
    bench.declare(SERVER)
    bench.looks(3)

    held = [one for one in bench.faults() if one.get("code") == MCP_INSTALL_HELD]

    assert len(held) == 1
    assert held[0]["blocks_turns"] is False
    assert ROOT_REASON in str(held[0]["message"])


def test_the_document_carries_no_such_fault_when_nothing_is_held(bench: Bench) -> None:
    bench.look()

    assert [one for one in bench.faults() if one.get("code") == MCP_INSTALL_HELD] == []


def test_a_held_request_does_not_stop_the_family(bench: Bench) -> None:
    """`blocks_turns: false` is the whole row. Eleven families off the
    air over one missing upstream would be an outage this platform
    caused."""
    bench.declare(SERVER)
    bench.looks(3)

    assert bench.document()["state"] == "degraded"
    assert all(one["blocks_turns"] is False for one in bench.faults())
