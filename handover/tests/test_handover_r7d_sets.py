"""Sets, order, the `infra` and `playpen` kinds and the quiet window.

`bin/rework-release-gate.sh` section 2's list, all of it:

1. a set of two deploys in `order` and restores in reverse;
2. an `infra` diff that leaves LiteLLM alone needs no window, and one that
   changes LiteLLM waits — a synthetic spend row blocks it, then goes
   quiet;
3. a breaking contract change is refused when a consumer is missing from
   the set and accepted when it is present;
4. a release run with every credential revoked still succeeds;
5. `caregiver` publishes `live-manifest.json` afterwards.

The whole executor runs here, as in `test_handover_executor_steps.py`: a
real `Spool` on real directories, the real resolver, the real ten steps.
Only the child, the GitHub API, the phone, the clock and §2.8's spend
signal are fakes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from caregiver.live_manifest import publish
from handover.catalog import CATALOG_BY_NAME
from handover.executor.approval import Decision, Summary, Verdict
from handover.executor.drain import Counter, handle
from handover.executor.live_state import installed_version, write_manifest_stamp
from handover.executor.quiet import QUIET_WINDOW_S, WATCHED_SERVICES, needs_window
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover_executor_fixtures import (
    REQUEST_ID,
    SHA_OF,
    FakeApi,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    live_state_body,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_live_state,
    write_request,
)
from handover_fixtures import manifest_text, provides_entry, requires_entry

PEP_LIVE = "2.0.3"
PEP_NEW = "2.1.0"
ATTENDANCE_LIVE = "1.4.6"
ATTENDANCE_NEW = "1.5.0"
INFRA_LIVE = "1.0.0"
INFRA_NEW = "1.1.0"

#: A `build` entry, so the manifest stages something. Contract 06 §8's shape.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'

NOW = 1758153600.0

#: `handover_fixtures.manifest_text` builds every component as `kind: venv`
#: with `unit: null` and no `depends_on`. A set needs the real kind, the
#: real unit and a real edge, because those are exactly what `order`, the
#: quiet window and the restart
#: read. The values are contract 06 §1's own.
SPEC: dict[str, tuple[str, str, str]] = {
    "chaperone": ("venv", "creche-chaperone.service", ""),
    "attendance": ("venv", "attendance.service", "chaperone"),
    "caregiver": ("venv", "creche-caregiver.service", "chaperone"),
    "noticeboard": ("venv", "creche-noticeboard.service", ""),
    "infra": ("compose", "ai-stack.service", ""),
    "playpen": ("oci-image", "null", ""),
    "handover": ("venv", "creche-handover.service", ""),
    "mcp-servers": ("venv", "null", ""),
    "registry-data": ("data", "null", ""),
}


@dataclass
class SetBench:
    """One executor, the components it can release, and the phone's view."""

    tmp_path: Path
    spool_root: Path
    components: Path
    run: FakeRun
    wiring: Wiring
    summaries: list[Summary]
    #: What §2.8's reader answers: when root last saw a turn in flight.
    #: None is never quiet.
    busy_seen: list[float | None] = field(default_factory=lambda: [NOW - 10_000.0])
    edits: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    #: Contract 06 §3.1's two lists, per component, as YAML entry text.
    contracts: dict[str, tuple[str, str]] = field(default_factory=dict[str, "tuple[str, str]"])

    def manifest(self, name: str) -> str:
        kind, unit, depends = SPEC[name]
        provides, requires = self.contracts.get(name, ("", ""))
        text = manifest_text(name, depends_on=depends, provides=provides, requires=requires)
        text = text.replace("/opt/components", str(self.components))
        text = text.replace("kind: venv", f"kind: {kind}", 1)
        text = text.replace("unit: null", f"unit: {unit}", 1)
        text = text.replace("install:", BUILD_LINE, 1)
        for old, new in self.edits.get(name, {}).items():
            text = text.replace(old, new)

        return text

    def install_units(self) -> None:
        """A unit is refreshed and restarted only where one is already
        installed (contract 06 §8, `install.py` rule 4).

        Each one carries an `ExecStart` inside its own component's tree:
        contract 06 §1 rule 8 refuses a release whose unit starts a program
        outside `install.to`, and a unit with no `ExecStart` at all is the
        fail-closed end of the same rule.
        A fixture unit that names no program stands where no real unit
        stands.
        """
        directory = self.tmp_path / "system-units"
        directory.mkdir(exist_ok=True)
        for name, (_, unit, _) in SPEC.items():
            if unit == "null":
                continue

            body = f"[Unit]\n\n[Service]\nExecStart={self.components}/{name}/bin/{name}\n"
            (directory / unit).write_text(body, encoding="utf-8")

    def ledger(self, request_id: str = REQUEST_ID) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{request_id}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def step(self, entry: dict[str, object], name: str) -> dict[str, object] | None:
        for row in cast("list[dict[str, object]]", entry["steps"]):
            if row.get("name") == name:
                return row

        return None


def _green_for(*components: tuple[str, str]) -> FakeApi:
    """One `FakeApi` answering P1 to P5 for every component in the set."""
    from handover_executor_fixtures import green_api

    bodies: dict[str, object] = {}
    for name, version in components:
        repo = str(CATALOG_BY_NAME[name].repo)
        bodies |= green_api(repo, f"{name}-v{version}", SHA_OF[name]).bodies

    return FakeApi(bodies)


def _make_bench(
    tmp_path: Path,
    live: dict[str, str | None],
    latest: dict[str, str],
    api: FakeApi,
) -> SetBench:
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    seen: list[Summary] = []
    bench_holder: list[SetBench] = []

    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, wait_s
        seen.append(summary)

        return Decision(Verdict.GRANTED, gate, bench_holder[0].wiring.host.clock())

    def busy_seen() -> float | None:
        return bench_holder[0].busy_seen[0]

    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=transport,
        readers=fake_readers(latest=latest),
        api=api,
        last_busy_seen=busy_seen,
    )
    bench = SetBench(tmp_path, spool_root, tmp_path / "components", run, wiring, seen)
    bench_holder.append(bench)
    bench.install_units()

    return bench


def _serve(bench: SetBench, staged: dict[str, str]) -> None:
    """What the fake clone leaves behind, and what each `build` writes."""

    def write_tree(destination: Path, component: str) -> None:
        row = CATALOG_BY_NAME[component]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(bench.manifest(component), encoding="utf-8")

    def build_all(_: object) -> None:
        for name, version in staged.items():
            stamp_tree(bench.components, f"{name}.new", version)

    bench.run.dynamic = GitFake(write_tree)
    bench.run.hooks["sync"] = build_all


def _live_tree(bench: SetBench, name: str, version: str) -> None:
    """An install tree as a verified release left it: the version stamp AND
    the manifest, so step 2 reads this component out of a tree root owns
    rather than at the document's SHA."""
    tree = stamp_tree(bench.components, name, version)
    write_manifest_stamp(tree, bench.manifest(name))


def _fail_hook_once(bench: SetBench, word: str) -> None:
    """The NEW version's hook fails; the restored one passes.

    That is the fact contract 06 §5.1 demands — the previous version
    verifies green, not merely that the files went back. The fake matches
    on an argv word, and both trees carry the same hook name, so the
    failure has to be spent after one call.
    """
    calls = {"n": 0}
    bench.run.fails[word] = 1

    def pass_after_the_first_call(_: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop(word, None)

    bench.run.hooks[word] = pass_after_the_first_call


def _run(bench: SetBench, components: dict[str, str]) -> str:
    write_request(bench.spool_root, REQUEST_ID, request_body(components))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


# -- 1. a set of two: order forward, restore in reverse -------------------


@pytest.fixture
def two(tmp_path: Path) -> SetBench:
    """`chaperone` and `attendance`, which `depends_on: [chaperone, playpen]` puts
    in that order."""
    bench = _make_bench(
        tmp_path,
        {"chaperone": PEP_LIVE, "attendance": ATTENDANCE_LIVE},
        {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW},
        _green_for(("chaperone", PEP_NEW), ("attendance", ATTENDANCE_NEW)),
    )
    for name, version in (("chaperone", PEP_LIVE), ("attendance", ATTENDANCE_LIVE)):
        _live_tree(bench, name, version)
        stamp_tree(bench.components, f"{name}.prev", version)

    _serve(bench, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    return bench


def test_a_set_of_two_deploys_in_order(two: SetBench) -> None:
    """§2.4 row 9 walks `order`, which comes from `depends_on`: `attendance`
    waits for `chaperone`, so `chaperone` swaps first."""
    result = _run(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    assert "succeeded" in result
    entry = two.ledger()
    assert entry["status"] == "succeeded"
    assert two.step(entry, "switch") is not None
    assert str(two.step(entry, "switch"))
    assert installed_version(two.components / "chaperone") == PEP_NEW
    assert installed_version(two.components / "attendance") == ATTENDANCE_NEW
    assert cast("dict[str, object]", entry["manifest"])["order"] == ["chaperone", "attendance"]


def test_a_set_of_two_restores_in_reverse(two: SetBench) -> None:
    """Contract 06 §5.1: the unit of restore is the whole release, in
    REVERSE deploy order. Taking `chaperone` back before `attendance` would leave
    `attendance` talking to a version that is no longer there."""
    _fail_hook_once(two, "attendance-verify")

    _run(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    entry = two.ledger()
    assert entry["status"] == "restored"
    assert installed_version(two.components / "chaperone") == PEP_LIVE
    assert installed_version(two.components / "attendance") == ATTENDANCE_LIVE
    verified = [str(row["component"]) for row in cast("list[dict[str, object]]", entry["verify"])]
    # chaperone verifies, attendance fails, then both go back: attendance first.
    assert verified == ["chaperone", "attendance", "attendance", "chaperone"]


def test_one_manual_component_stops_the_whole_restore(two: SetBench) -> None:
    """Contract 06 §5.2: ANY deployed component with `mode: manual` stops
    the restore, not only the failing one. Restoring a subset lands the
    host in a combination nobody resolved, checked or approved."""
    two.edits["chaperone"] = {"mode: automatic": "mode: manual"}
    _serve(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})
    _live_tree(two, "chaperone", PEP_LIVE)
    _fail_hook_once(two, "attendance-verify")

    _run(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    entry = two.ledger()
    assert entry["status"] == "failed"
    assert any("manual" in str(one) for one in cast("list[object]", entry["manual"]))
    # Nothing went back: the new trees are still live, and the ledger says so.
    assert installed_version(two.components / "chaperone") == PEP_NEW


def test_the_phone_summary_names_every_component_of_the_set(two: SetBench) -> None:
    """§2.5: the summary and the deployed set come from ONE computation, so
    a set of two shows two moves and two units."""
    _run(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    summary = two.summaries[0]
    assert "chaperone 2.0.3 → 2.1.0" in summary.components
    assert "attendance 1.4.6 → 1.5.0" in summary.components
    assert "creche-chaperone.service" in summary.restarts
    assert "attendance.service" in summary.restarts


# -- 2. the quiet window --------------------------------------------------


def test_a_diff_that_leaves_litellm_alone_needs_no_window() -> None:
    """§2.8: the check is on the SERVICE, because a service that goes away
    drops every in-flight model call whatever the verb was."""
    assert not needs_window(["ai-openwebui"])
    assert not needs_window([])


@pytest.mark.parametrize("service", WATCHED_SERVICES)
def test_a_diff_that_changes_a_watched_service_waits(service: str) -> None:
    assert needs_window([service, "ai-openwebui"])


@pytest.fixture
def compose(tmp_path: Path) -> SetBench:
    """`infra` alone, whose dry run the fake child answers."""
    bench = _make_bench(
        tmp_path, {"infra": INFRA_LIVE}, {"infra": INFRA_NEW}, _green_for(("infra", INFRA_NEW))
    )
    _live_tree(bench, "infra", INFRA_LIVE)
    stamp_tree(bench.components, "infra.prev", INFRA_LIVE)
    _serve(bench, {"infra": INFRA_NEW})

    return bench


def test_an_infra_release_that_recreates_litellm_waits_then_lands(compose: SetBench) -> None:
    """The gate's own case: a turn in flight blocks the switch, then the
    host goes quiet and the release lands."""
    compose.run.answers["sync"] = "Recreate ai-litellm\nRecreate ai-redis\n"
    # A turn seen just now: not quiet.
    compose.busy_seen[0] = compose.wiring.host.clock()

    def go_quiet(_: object) -> None:
        compose.busy_seen[0] = compose.wiring.host.clock() - QUIET_WINDOW_S - 1.0

    compose.run.hooks["is-active"] = go_quiet

    result = _run(compose, {"infra": INFRA_NEW})

    assert "succeeded" in result
    assert any("quiet window" in line for line in _log(compose))


def test_an_infra_release_with_no_quiet_window_fails_the_switch(compose: SetBench) -> None:
    """§2.8: an hour with no window is `failed, step: switch`. The default
    reader answers None, which is never quiet — a host whose session service
    root cannot reach is not an idle host."""
    compose.run.answers["sync"] = "Recreate ai-litellm\n"
    compose.busy_seen[0] = None

    _run(compose, {"infra": INFRA_NEW})

    entry = compose.ledger()
    assert entry["status"] in {"failed", "restored"}
    assert "no quiet window" in str(entry["reason"])


def test_an_infra_diff_that_leaves_litellm_alone_never_waits(compose: SetBench) -> None:
    compose.run.answers["sync"] = "Recreate ai-openwebui\n"
    compose.busy_seen[0] = compose.wiring.host.clock()

    result = _run(compose, {"infra": INFRA_NEW})

    assert "succeeded" in result
    assert not any("quiet window" in line for line in _log(compose))


def _log(bench: SetBench) -> list[str]:
    path = bench.spool_root / DONE_DIR / f"{REQUEST_ID}.log"

    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# -- 3. playpen: one release replaces every family's sandbox --------


def test_a_sandbox_image_release_lands(tmp_path: Path) -> None:
    """Contract 06 §1 rule 5 and §2.2: `playpen` is `kind: oci-image`
    and has ONE live version, so the manifest carries no range, and it
    releases like any other kind."""
    version, live = "3.1.0", "3.0.0"
    bench = _make_bench(
        tmp_path,
        {"playpen": live},
        {"playpen": version},
        _green_for(("playpen", version)),
    )
    _live_tree(bench, "playpen", live)
    _serve(bench, {"playpen": version})

    result = _run(bench, {"playpen": version})

    assert "succeeded" in result
    assert installed_version(bench.components / "playpen") == version
    # `unit: null`, so nothing is restarted and no session is lost
    # (invariant 1: sessions live on the host).
    assert not bench.run.ran("restart")


# -- 4. a release with every credential revoked --------------------------


def test_a_release_with_no_git_credential_still_succeeds(two: SetBench) -> None:
    """Contract 06 §3.4: the executor holds no git credential. It reads a
    checkout another process keeps current, BY SHA, so revoking every
    credential root does not hold cannot stop a release."""
    _run(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    fetched = [line for line in two.run.argv_lines() if "clone" in line]

    assert two.ledger()["status"] == "succeeded"
    assert all("--local" in line for line in fetched)
    assert not any("https://" in line or "git@" in line for line in fetched)


# -- 5. caregiver publishes live-manifest.json afterwards -----------------


def test_caregiver_publishes_what_the_ledger_says_is_live(two: SetBench) -> None:
    """Contract 06 §3.4: root writes the ledger and never a repository, so
    `caregiver` carries the resolved lock off the host. The whole loop is
    proved here — a real release writes `done/`, and the publisher reads
    that same directory."""
    _run(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    checkout = two.tmp_path / "registry"
    found = publish(checkout, two.spool_root / DONE_DIR)

    # This set left `caregiver` alone, so there is nothing to name.
    assert found is None
    assert not (checkout / "live-manifest.json").exists()


def test_caregiver_publishes_after_a_release_that_moves_it(tmp_path: Path) -> None:
    caregiver_live, caregiver_new = "1.1.0", "1.2.0"
    bench = _make_bench(
        tmp_path,
        {"caregiver": caregiver_live, "chaperone": PEP_LIVE},
        {"caregiver": caregiver_new},
        _green_for(("caregiver", caregiver_new)),
    )
    _live_tree(bench, "caregiver", caregiver_live)
    _live_tree(bench, "chaperone", PEP_LIVE)
    _serve(bench, {"caregiver": caregiver_new})

    _run(bench, {"caregiver": caregiver_new})

    found = publish(tmp_path / "registry", bench.spool_root / DONE_DIR)

    assert found is not None
    assert found.caregiver == caregiver_new
    assert found.release_id == REQUEST_ID


# -- 3. C4: a breaking change ships as a set ------------------------------


def _breaking(bench: SetBench) -> None:
    """`chaperone` bumps `pep-grant` from major 2 to major 3, and `attendance` is
    its consumer. Contract 06 §3.2 rule C4 is the whole point: a breaking
    interface change ships as one set with one approval."""
    bench.contracts["chaperone"] = (provides_entry("pep-grant", 3, 0), "")
    bench.contracts["attendance"] = ("", requires_entry("pep-grant", 3, 0))
    _serve(bench, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})
    # What the LIVE provider provides, which is what C4 compares against.
    write_live_state(
        bench.tmp_path,
        live_state_body({"chaperone": PEP_LIVE, "attendance": ATTENDANCE_LIVE}, {})
        | {"provided": {"pep-grant": "2.0"}},
    )
    # `attendance` is live at the OLD floor, so a `chaperone` release alone breaks
    # it. That manifest is the one root reads out of the install tree.
    bench.contracts["attendance"] = ("", requires_entry("pep-grant", 2, 0))
    _live_tree(bench, "attendance", ATTENDANCE_LIVE)
    bench.contracts["attendance"] = ("", requires_entry("pep-grant", 3, 0))


def test_a_breaking_change_is_refused_when_a_consumer_is_missing(two: SetBench) -> None:
    _breaking(two)

    _run(two, {"chaperone": PEP_NEW})

    entry = two.ledger()
    assert entry["status"] == "refused"
    assert entry["refused_check"] in {"C1", "C4"}
    assert "attendance" in str(entry["reason"])


def test_a_breaking_change_is_accepted_when_the_consumer_is_present(two: SetBench) -> None:
    _breaking(two)

    result = _run(two, {"chaperone": PEP_NEW, "attendance": ATTENDANCE_NEW})

    assert "succeeded" in result
