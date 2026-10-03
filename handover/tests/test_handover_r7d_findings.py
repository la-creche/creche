"""More executor faults a hostile requester reaches, one regression each.

Each test names the fault it pins, exactly as
`test_handover_executor_review.py` does, so a later refactor that undoes a
fix fails here rather than on the host.

One fault is this file's own: install roots of `/opt/components` alone
miss the three releasable components that install under the operator's home.
Every one of them would be refused at step 8 and read as a first install
at step 2, which turns off the monotonic rule for exactly those
components.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG_BY_NAME
from handover.errors import Refusal
from handover.executor.approval import Decision, Summary
from handover.executor.drain import Counter, handle, repair_unfinished
from handover.executor.host import INSTALL_ROOT, Host, operator_install_root
from handover.executor.install import paths_of
from handover.executor.live_state import (
    MAX_MANIFEST_BYTES,
    installed_manifest,
    installed_version,
    write_manifest_stamp,
)
from handover.executor.quiesce import LAUNCH_GRACE_S, MAX_SCAN_ENTRIES, Quiesce
from handover.executor.spool import DONE_DIR, Spool, SwitchNote, VerifyHook
from handover.executor.steps import Wiring
from handover.manifest import parse_manifest
from handover.resolve import deploying_names
from handover.silent import silent_manifest
from handover.state import ReleaseState
from handover_executor_fixtures import (
    REQUEST_ID,
    SHA_OF,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    grant_transport,
    green_api,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_request,
)
from handover_fixtures import manifest_text

LIVE_VERSION = "2.0.3"
NEW_VERSION = "2.1.0"


def _repo_manifest(name: str):
    """One of THIS repository's seven manifests, parsed."""
    row = CATALOG_BY_NAME[name]
    root = Path(__file__).resolve().parents[2]
    path = root / row.path / "component.yaml"

    return parse_manifest(path.read_text(encoding="utf-8"), str(path))


def _default_roots() -> tuple[Path, ...]:
    return Host(run=lambda command: None).install_roots  # type: ignore[arg-type, return-value]


# -- the operator's install root ---------------------------------------


def test_the_default_install_roots_hold_both_trees() -> None:
    """Contract 06 §1: `chaperone` installs under `/opt/components`, `attendance`
    under the operator's home. One root covers half the catalog."""
    roots = _default_roots()

    assert Path(INSTALL_ROOT) in roots
    assert Path(operator_install_root()) in roots


@pytest.mark.parametrize("name", ["attendance", "caregiver", "noticeboard"])
def test_a_user_venv_component_is_not_refused_by_containment(name: str) -> None:
    """`paths_of` must not refuse a component whose `install.to` sits
    under `~operator`, or `attendance` could not be released at all."""
    manifest = _repo_manifest(name)

    paths = paths_of(manifest, _default_roots())

    assert str(paths.to).startswith(operator_install_root())


@pytest.mark.parametrize("name", ["chaperone", "handover", "playpen"])
def test_a_root_venv_component_is_still_contained(name: str) -> None:
    manifest = _repo_manifest(name)

    paths = paths_of(manifest, _default_roots())

    assert str(paths.to).startswith(INSTALL_ROOT)


def test_an_install_path_under_neither_root_is_still_refused() -> None:
    """The containment check is the point of the field. Widening it to two
    roots must not widen it to every root."""
    manifest = _repo_manifest("chaperone")
    bent = parse_manifest(
        _text_with_install(manifest.name, "/etc/agent", "/etc/agent.prev"), "bent.yaml"
    )

    with pytest.raises(Refusal):
        paths_of(bent, _default_roots())


# -- a repaired crash is ledgered, restarted and verified -----------------


#: What `spool.note_name` calls the note this bench writes: the run's id
#: and the component it was switching. `unfinished` splits it again, or
#: `ledgered` would be asked a question no entry can ever answer.
NOTE_NAME = f"{REQUEST_ID}-chaperone"


class RepairBench:
    """A host with `chaperone` swapped in and a switch note nobody cleared."""

    def __init__(self, tmp_path: Path, run: FakeRun) -> None:
        self.tmp_path = tmp_path
        self.run = run
        self.spool_root = make_spool_dirs(tmp_path)
        self.components = tmp_path / "components"
        self.wiring = Wiring(
            host=fake_host(tmp_path, run),
            transport=grant_transport(),
            readers=fake_readers(),
            api=None,
        )

    def note(self, *, hook: VerifyHook | None) -> SwitchNote:
        return SwitchNote(
            "chaperone",
            str(self.components / "chaperone"),
            str(self.components / "chaperone.prev"),
            "creche-chaperone.service",
            hook,
        )

    def repair(self, note: SwitchNote) -> list[str]:
        spool = Spool(str(self.spool_root), this_uid())
        try:
            spool.note_switch(REQUEST_ID, note)
            lines = repair_unfinished(spool, self.wiring)
            assert not spool.unfinished()
        finally:
            spool.close()

        return lines

    def ledger(self) -> dict[str, object]:
        """A repair's entry, filed under the NOTE's own name.

        Not the request's: a crash can leave one note per component of one
        request, and each repair is its own entry (`drain._repair_one`).
        The entry's `id` field is the request id, and a test here holds it
        to that.
        """
        path = self.spool_root / DONE_DIR / f"{NOTE_NAME}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


@pytest.fixture
def repair_bench(tmp_path: Path) -> RepairBench:
    bench = RepairBench(tmp_path, FakeRun(answers=git_host_answers()))
    stamp_tree(bench.components, "chaperone.prev", LIVE_VERSION)
    stamp_tree(bench.components, "chaperone", NEW_VERSION)
    # A unit is refreshed and restarted only where one is already
    # installed (contract 06 §8, `install.py` rule 4).
    (tmp_path / "system-units").mkdir(exist_ok=True)
    (tmp_path / "system-units" / "creche-chaperone.service").write_text(
        "[Unit]\n", encoding="utf-8"
    )

    return bench


def _hook(bench: RepairBench) -> VerifyHook:
    return VerifyHook(
        command=(str(bench.components / "chaperone" / "bin" / "chaperone-verify"), "--json"),
        user="root",
        timeout_s=60,
    )


def test_a_repaired_crash_writes_its_own_ledger_entry(repair_bench: RepairBench) -> None:
    """A repair printed to the journal alone leaves `done/` saying nothing
    happened while the host runs restored code."""
    repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    entry = repair_bench.ledger()

    assert entry["status"] == "restored"
    assert entry["reason"] == "a crash between the switch and the verify"
    names = [str(row.get("name")) for row in cast("list[dict[str, object]]", entry["steps"])]
    assert names == ["restore"]
    assert installed_version(repair_bench.components / "chaperone") == LIVE_VERSION


def test_a_repairs_entry_says_which_request_it_belongs_to(repair_bench: RepairBench) -> None:
    """The entry is FILED under the note's name, so two
    components of one crashed run are two entries — and its `id` is the
    request id, which is what §2.6 promises a reader and what the operator greps
    for."""
    repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    assert repair_bench.ledger()["id"] == REQUEST_ID
    assert (repair_bench.spool_root / DONE_DIR / f"{NOTE_NAME}.json").is_file()


def test_a_repair_removes_the_crashed_trees_new(repair_bench: RepairBench) -> None:
    """A tree root leaves at `.new` is what the next release stages into
    and what the next cutover dies on, so the repair removes it the way a
    failed release does."""
    repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    staged = repair_bench.components / "chaperone.new"
    assert not staged.exists()
    assert f"removed {staged}" in cast("list[object]", repair_bench.ledger()["log_tail"])


def test_a_repair_restarts_the_restored_unit(repair_bench: RepairBench) -> None:
    """Without a restart the host keeps running the NEW code from a
    process whose tree has just been renamed away."""
    repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    assert repair_bench.run.ran("restart")
    assert repair_bench.run.ran("creche-chaperone.service")


def test_a_repair_runs_the_previous_versions_verify_hook(repair_bench: RepairBench) -> None:
    """The hook the note names is the one the LAST approved
    release installed, so root runs it against the tree it just put back."""
    repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    entry = repair_bench.ledger()
    rows = cast("list[dict[str, object]]", entry["verify"])

    # `detail`: the hook's own bounded stdout, pass
    # or fail. Empty here because the fake hook is never actually asked to
    # print anything.
    expected = {
        "component": "chaperone",
        "status": "ok",
        "seconds": rows[0]["seconds"],
        "detail": "",
    }
    assert rows == [expected]
    assert repair_bench.run.ran("chaperone-verify")


def test_a_repair_whose_verify_fails_is_ledgered_failed(repair_bench: RepairBench) -> None:
    """Nothing further is automatic (§2.4 row 10). The entry says so instead
    of the journal saying nothing."""
    repair_bench.run.fails["chaperone-verify"] = 1

    repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    entry = repair_bench.ledger()
    assert entry["status"] == "failed"
    assert "verify" in str(entry["reason"])


def test_a_repair_with_no_recorded_hook_says_so_under_manual(
    repair_bench: RepairBench,
) -> None:
    """A note an older executor wrote carries no hook. The restore still
    happens, and the ledger names what root could not do."""
    repair_bench.repair(repair_bench.note(hook=None))

    entry = repair_bench.ledger()
    assert entry["status"] == "restored"
    assert any("verify" in str(one) for one in cast("list[object]", entry["manual"]))
    assert not repair_bench.run.ran("chaperone-verify")


def test_a_repair_with_no_previous_tree_restarts_nothing(repair_bench: RepairBench) -> None:
    """The FIRST install of a component: there is no `.prev`, so there is
    nothing to put back and the unit keeps running what the crashed switch
    left live. That is `noticeboard` on the host, and it is deliberate — a restart
    would take the component down to reach the same tree.

    What it must not be is silent. The note is cleared, one `manual` line
    says nothing went back, and the entry carries the request id the note
    belonged to."""
    (repair_bench.components / "chaperone.prev").rename(repair_bench.components / "chaperone.kept")

    lines = repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    entry = repair_bench.ledger()
    assert entry["id"] == REQUEST_ID
    assert any("nothing to put back" in str(one) for one in cast("list[object]", entry["manual"]))
    assert not repair_bench.run.ran("restart")
    assert installed_version(repair_bench.components / "chaperone") == NEW_VERSION
    assert any(REQUEST_ID in one for one in lines)


def test_a_run_that_recorded_itself_is_not_repaired(repair_bench: RepairBench) -> None:
    """A note whose run is ledgered is spent.

    Asked about `<request id>-chaperone` rather than the request id, `ledgered`
    would match no entry and every note would be repaired — the unit
    restarted and the hook re-run over a release the run had already
    decided and written down. A run that recorded itself is not a crash,
    whatever its outcome says.
    """
    done = repair_bench.spool_root / DONE_DIR / f"{REQUEST_ID}.json"
    done.write_text(json.dumps({"id": REQUEST_ID, "status": "failed"}), encoding="utf-8")

    lines = repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    assert any("already ledgered" in one for one in lines)
    # Nothing was put back and nothing was started.
    assert installed_version(repair_bench.components / "chaperone") == NEW_VERSION
    assert not repair_bench.run.ran("chaperone-verify")
    assert not (repair_bench.spool_root / DONE_DIR / f"{NOTE_NAME}.json").exists()


def test_a_repair_never_overwrites_an_existing_ledger_entry(
    repair_bench: RepairBench,
) -> None:
    """A half-written `finish` the other way round: an entry that already
    exists is the authority, and the repair may only clear the note. Here
    it is a previous repair's OWN entry, under the note's name."""
    done = repair_bench.spool_root / DONE_DIR / f"{NOTE_NAME}.json"
    done.write_text(json.dumps({"id": REQUEST_ID, "status": "restored"}), encoding="utf-8")

    lines = repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    assert repair_bench.ledger()["status"] == "restored"
    assert any("already ledgered" in one for one in lines)
    assert not repair_bench.run.ran("chaperone-verify")


def test_a_stale_log_from_a_half_written_finish_does_not_wedge_the_run(
    repair_bench: RepairBench,
) -> None:
    """`finish` writes `<id>.log` then `<id>.json`. A crash between them
    leaves a log with no entry, which must not make the next run raise
    `SpoolError` at `_write_new` and exit 1 without draining."""
    (repair_bench.spool_root / DONE_DIR / f"{NOTE_NAME}.log").write_text("half\n")

    repair_bench.repair(repair_bench.note(hook=_hook(repair_bench)))

    assert repair_bench.ledger()["status"] == "restored"


# -- a quiesce signal root can trust, and a bounded walk ------------------


def _turn(root: Path, session: str, turn: str, started: float, state: str = "running") -> Path:
    """One turn record, exactly the shape contract 02 §9 writes."""
    directory = root / "chat" / session / "turns"
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / f"{turn}.json"
    stamp = datetime.fromtimestamp(started, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f%z")
    record.write_text(json.dumps({"state": state, "started_at": stamp}), encoding="utf-8")

    return record


NOW = 1758153600.0


def test_a_rolling_started_at_stops_blocking_after_one_grace(tmp_path: Path) -> None:
    """`started_at` is operator-written, so a record that keeps rolling it
    forward would block step 7 for its whole 180 seconds and fail the
    release AFTER the tap. Root's own first sighting is the signal it can
    trust."""
    sessions = tmp_path / "sessions"
    watcher = Quiesce()
    record = _turn(sessions, "owui-1", "T1", NOW)

    # Five polls, each one grace period apart, and the record rolls its own
    # `started_at` forward every time.
    seen: list[int] = []
    for step in range(5):
        now = NOW + step * LAUNCH_GRACE_S
        _turn(sessions, "owui-1", "T1", now)
        seen.append(len(watcher.starting(sessions, now)))

    assert record.exists()
    assert seen[0] == 1
    assert seen[-1] == 0


def test_a_genuinely_starting_turn_still_gets_its_whole_window(tmp_path: Path) -> None:
    """Root's own sighting must not make step 7 blind: a turn that really
    did just start blocks until the launch grace runs out."""
    sessions = tmp_path / "sessions"
    watcher = Quiesce()
    _turn(sessions, "owui-1", "T1", NOW)

    assert watcher.starting(sessions, NOW) == ("chat/owui-1/T1",)
    assert watcher.starting(sessions, NOW + LAUNCH_GRACE_S / 2) == ("chat/owui-1/T1",)


def test_a_turn_that_claims_the_future_does_not_block(tmp_path: Path) -> None:
    """A `started_at` ahead of root's clock is a disagreeing clock or a
    writer holding the window open. Neither is a turn mid-start."""
    sessions = tmp_path / "sessions"
    _turn(sessions, "owui-1", "T1", NOW + 10_000.0)

    assert Quiesce().starting(sessions, NOW) == ()


def test_a_settled_turn_is_forgotten_so_a_later_one_gets_its_window(
    tmp_path: Path,
) -> None:
    """The watcher's memory is per turn, and it drops a turn that settles.
    A session that runs turn after turn must not exhaust one budget."""
    sessions = tmp_path / "sessions"
    watcher = Quiesce()
    _turn(sessions, "owui-1", "T1", NOW)
    watcher.starting(sessions, NOW)
    _turn(sessions, "owui-1", "T1", NOW, state="settled")
    watcher.starting(sessions, NOW + 1.0)

    _turn(sessions, "owui-1", "T1", NOW + 2 * LAUNCH_GRACE_S)

    assert watcher.starting(sessions, NOW + 2 * LAUNCH_GRACE_S) == ("chat/owui-1/T1",)


def test_the_walk_spends_one_budget_unit_per_directory_entry(tmp_path: Path) -> None:
    """`MAX_TURNS_SCANNED` bounded the READS
    and not the LIST: `glob` materialized and sorted every path a
    operator-side writer cared to create.

    The budget binds at EVERY level, which is what a padded middle level
    proves: with room for only a few entries the walk never reaches the
    real turn, and with the whole budget it does.
    """
    sessions = tmp_path / "sessions"
    for index in range(40):
        (sessions / "chat" / f"pad{index:03d}").mkdir(parents=True)

    _turn(sessions, "owui-1", "T1", NOW)

    assert Quiesce().starting(sessions, NOW, budget=4) == ()
    assert Quiesce().starting(sessions, NOW) == ("chat/owui-1/T1",)


def test_the_default_budget_bounds_the_whole_walk() -> None:
    """The number itself is the defence, so a later editor who lowers it to
    a handful, or removes it, fails here."""
    assert MAX_SCAN_ENTRIES >= 10_000


def test_a_sessions_root_that_does_not_exist_is_quiet(tmp_path: Path) -> None:
    """A host with no sessions tree yet is not busy, and a directory root
    cannot open must not be able to stop a release."""
    assert Quiesce().starting(tmp_path / "nothing", NOW) == ()


# -- what root may believe about a manifest it did not verify -------------


def test_a_component_with_no_install_tree_declares_nothing() -> None:
    """Contract 06 §10.2's third row. Nothing is installed, so nothing is
    running, so no interface it might declare can be broken by this
    release."""
    found = silent_manifest("attendance")

    assert found.provides == ()
    assert found.requires == ()
    assert found.depends_on == ()
    assert found.name == "attendance"


def test_a_silent_manifest_passes_the_real_parser() -> None:
    """It is built as YAML and parsed, so it is not the one manifest in the
    system with no validation behind it."""
    for name in ("chaperone", "infra", "playpen", "registry-data"):
        assert silent_manifest(name).name == name


def test_the_switch_stamps_the_manifest_into_the_artifact(tmp_path: Path) -> None:
    """The stamp is what turns the next release's read into a fact: the
    tree is root-owned and the file got there through a verified release."""
    tree = tmp_path / "chaperone"
    tree.mkdir()
    write_manifest_stamp(tree, "name: chaperone\n")

    assert installed_manifest(tree) == "name: chaperone\n"
    assert installed_manifest(tmp_path / "nothing") is None


def test_an_oversized_stamped_manifest_is_not_read(tmp_path: Path) -> None:
    """An install tree is root-owned, and the cap is still here: a planted
    file must not be read into memory."""
    tree = tmp_path / "chaperone"
    tree.mkdir()
    write_manifest_stamp(tree, "x" * (MAX_MANIFEST_BYTES + 1))

    assert installed_manifest(tree) is None


def test_only_the_deploying_component_is_fetched(tmp_path: Path) -> None:
    """The enforcement, end to end. Step 2 cloned all NINE components at
    SHAs the operator-written document supplied, and step 3 tied exactly one
    of them to a verified tag. Now it clones what it deploys and reads the
    rest out of trees root owns."""
    bench = _release_bench(tmp_path)

    _drive_one(bench)

    cloned = [line for line in bench.run.argv_lines() if "clone" in line]
    assert len(cloned) == 1
    assert "/chaperone" in cloned[0]


def test_an_unstamped_live_tree_puts_suspect_in_front_of_the_operator(tmp_path: Path) -> None:
    """Contract 06 §10.2's fourth row, the upgrade path. A tree installed
    before manifests were stamped has none, so root reads the document's
    SHA and §2.5's first field says so BEFORE the tap."""
    bench = _release_bench(tmp_path)
    stamp_tree(bench.components, "infra", "1.0.0")

    _drive_one(bench)

    assert bench.summaries
    assert bench.summaries[0].review.startswith("suspect:")
    assert "infra" in bench.summaries[0].review


def test_a_stamped_live_tree_reads_as_safe(tmp_path: Path) -> None:
    """The same tree WITH the manifest root stamped into it is a fact, so
    the verdict goes back to `safe`."""
    bench = _release_bench(tmp_path)
    tree = stamp_tree(bench.components, "infra", "1.0.0")
    write_manifest_stamp(tree, _bench_manifest(bench, "infra"))

    _drive_one(bench)

    assert bench.summaries[0].review.startswith("safe:")


def test_deploying_names_needs_no_manifest() -> None:
    """The executor has to know what deploys BEFORE it decides which
    manifests it may believe. The action depends only on the request and
    the live state, which is what makes that ordering possible."""
    state = ReleaseState(live={"chaperone": "2.0.3"}, provided={}, latest={}, facts={})

    assert deploying_names(state, {"chaperone": "2.1.0"}) == frozenset({"chaperone"})
    # Already live: `unchanged`, so it is not deployed and its manifest is
    # not fetched either.
    assert deploying_names(state, {"chaperone": "2.0.3"}) == frozenset()
    assert deploying_names(state, {"not-a-component": "1.0.0"}) == frozenset()


#: A `build` entry, so the manifest stages something. Contract 06 §8's shape.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'


@dataclass
class ReleaseBench:
    """One wired executor that releases `chaperone`, and the summaries the phone
    was shown."""

    tmp_path: Path
    spool_root: Path
    components: Path
    run: FakeRun
    wiring: Wiring
    summaries: list[Summary]


def _bench_manifest(bench: ReleaseBench, name: str) -> str:
    text = manifest_text(name).replace("/opt/components", str(bench.components))

    return text.replace("install:", BUILD_LINE, 1)


def _release_bench(tmp_path: Path) -> ReleaseBench:
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    seen: list[Summary] = []

    def watching(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        seen.append(summary)

        return grant_transport()(action_id, gate, summary, wait_s)

    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=watching,
        readers=fake_readers(latest={"chaperone": NEW_VERSION}),
        api=green_api("agent-control", f"chaperone-v{NEW_VERSION}", SHA_OF["chaperone"]),
    )
    bench = ReleaseBench(tmp_path, spool_root, tmp_path / "components", run, wiring, seen)
    tree = stamp_tree(bench.components, "chaperone", LIVE_VERSION)
    write_manifest_stamp(tree, _bench_manifest(bench, "chaperone"))

    return bench


def _drive_one(bench: ReleaseBench) -> str:
    """One release of `chaperone`, through the real spool and the real steps."""

    def write_tree(destination: Path, component: str) -> None:
        row = CATALOG_BY_NAME[component]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(_bench_manifest(bench, component), encoding="utf-8")

    bench.run.dynamic = GitFake(write_tree)
    bench.run.hooks["sync"] = lambda _: stamp_tree(bench.components, "chaperone.new", NEW_VERSION)
    write_request(bench.spool_root, REQUEST_ID, request_body({"chaperone": NEW_VERSION}))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


def _text_with_install(name: str, to: str, prev: str) -> str:
    return f"""
manifest_version: "0.4"
name: {name}
repo: agent-control
path: chaperone
kind: venv
unit: creche-chaperone.service
runs_as: root
install:
  to: {to}
  prev: {prev}
verify:
  command: ["{to}/bin/chaperone-verify", "--json"]
  user: root
  timeout_s: 60
restore:
  mode: automatic
  keep: 3
release: yes
"""
