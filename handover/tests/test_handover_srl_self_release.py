"""Contract 06 §1.1: `handover` releases itself.

The three rules, end to end on a fake host:

1. `handover` is last in `order` (`order.py`, already built).
2. Its switch happens after the ledger entry is written and the lock is
   released. Step 9 writes a note and swaps nothing.
3. Its verify hook runs as a fresh process, from the new tree, after that
   swap. A failed hook puts the previous tree back.

A crash between the swap and the second entry leaves the note, and the next
run settles it.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG_BY_NAME
from handover.executor.approval import Decision, Summary, Verdict
from handover.executor.drain import Counter, drain, handle, run_pass
from handover.executor.live_state import installed_version, write_manifest_stamp
from handover.executor.notice import Notice
from handover.executor.spool import (
    DONE_DIR,
    REQUESTS_DIR,
    RUNNING_DIR,
    SelfNote,
    Spool,
    VerifyHook,
)
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
    green_api,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_request,
)
from handover_fixtures import manifest_text

SELF = "handover"
LIVE = "0.1.6"
NEW = "0.1.7"
PEP_LIVE = "2.0.3"
PEP_NEW = "2.1.0"

#: A second request, filed after the first. ULIDs sort by time.
LATER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DF"

#: The entry the switch writes, beside the release's own.
SELF_ENTRY = f"{REQUEST_ID}-{SELF}"
SELF_NOTE = f"{SELF_ENTRY}.self"

#: A `build` entry, so the manifest stages something (contract 06 §8).
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'

HOOK = f"{SELF}-verify"


class Crash(Exception):
    """The executor's process dying at the point a test names."""


@dataclass
class Bench:
    tmp_path: Path
    spool_root: Path
    components: Path
    run: FakeRun
    wiring: Wiring

    def manifest(self, name: str) -> str:
        text = manifest_text(name).replace("/opt/components", str(self.components))

        return text.replace("install:", BUILD_LINE, 1)

    def ledger(self, name: str = REQUEST_ID) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{name}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def self_entry(self) -> dict[str, object]:
        return self.ledger(SELF_ENTRY)

    def has_entry(self, name: str) -> bool:
        return (self.spool_root / DONE_DIR / f"{name}.json").is_file()

    def running(self) -> list[str]:
        return sorted(os.listdir(self.spool_root / RUNNING_DIR))

    def live(self, name: str = SELF) -> str | None:
        return installed_version(self.components / name)


def _make_bench(tmp_path: Path, moves: dict[str, tuple[str, str]]) -> Bench:
    """Every component in `moves` is live at its first version and has a
    green Release at its second."""
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    holder: list[Bench] = []

    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, summary, wait_s

        return Decision(Verdict.GRANTED, gate, holder[0].wiring.host.clock())

    bodies: dict[str, object] = {}
    for name, (_, new) in moves.items():
        repo = str(CATALOG_BY_NAME[name].repo)
        bodies |= green_api(repo, f"{name}-v{new}", SHA_OF[name]).bodies

    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=transport,
        readers=fake_readers(latest={name: new for name, (_, new) in moves.items()}),
        api=FakeApi(bodies),
    )
    bench = Bench(tmp_path, spool_root, tmp_path / "components", run, wiring)
    holder.append(bench)

    for name, (live, _) in moves.items():
        tree = stamp_tree(bench.components, name, live)
        write_manifest_stamp(tree, bench.manifest(name))

    _serve(bench, {name: new for name, (_, new) in moves.items()})

    return bench


def _serve(bench: Bench, staged: dict[str, str]) -> None:
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


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return _make_bench(tmp_path, {SELF: (LIVE, NEW)})


@pytest.fixture
def pair(tmp_path: Path) -> Bench:
    return _make_bench(tmp_path, {"chaperone": (PEP_LIVE, PEP_NEW), SELF: (LIVE, NEW)})


def _run(bench: Bench, components: dict[str, str], counter: Counter | None = None) -> str:
    write_request(bench.spool_root, REQUEST_ID, request_body(components))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, counter or Counter())
    finally:
        spool.close()


def _fail_hook_once(bench: Bench, word: str) -> None:
    """The new tree's hook fails and the restored one passes. Both trees
    carry the same hook name, so the failure is spent after one call."""
    calls = {"n": 0}
    bench.run.fails[word] = 1

    def pass_after_the_first_call(_: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop(word, None)

    bench.run.hooks[word] = pass_after_the_first_call


def _lock_is_free(spool_root: Path) -> bool:
    """Whether a second opener could take the spool lock right now."""
    fd = os.open(spool_root / "lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)

    return True


def _verified(entry: dict[str, object]) -> list[str]:
    return [str(row["component"]) for row in cast("list[dict[str, object]]", entry["verify"])]


def _step(entry: dict[str, object], name: str) -> dict[str, object]:
    for row in cast("list[dict[str, object]]", entry["steps"]):
        if row.get("name") == name:
            return row

    raise AssertionError(f"no {name} step")


def _note(bench: Bench) -> SelfNote:
    """The note step 9 writes, for a test that plants the state a crash
    leaves behind."""
    return SelfNote(
        component=SELF,
        to=str(bench.components / SELF),
        prev=str(bench.components / f"{SELF}.prev"),
        to_version=NEW,
        from_version=LIVE,
        requested_by="agent-control",
        verify=VerifyHook((str(bench.components / SELF / "bin" / HOOK), "--json"), "operator", 60),
    )


def _plant_note(bench: Bench) -> None:
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        spool.note_self(REQUEST_ID, _note(bench))
    finally:
        spool.close()


def _run_pass(bench: Bench) -> int:
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return run_pass(spool, bench.wiring)
    finally:
        spool.close()


# -- rules 1 to 3 -----------------------------------------------------------


def test_handover_releases_itself(bench: Bench) -> None:
    """The request the host refused at step 2 now lands, with a tap."""
    result = _run(bench, {SELF: NEW})

    assert "succeeded" in result
    assert bench.ledger()["status"] == "succeeded"
    assert bench.self_entry()["status"] == "succeeded"
    assert bench.live() == NEW
    assert bench.live(f"{SELF}.prev") == LIVE
    assert not (bench.components / f"{SELF}.new").exists()
    assert bench.running() == []


def test_the_switch_waits_for_the_ledger_and_the_lock(bench: Bench) -> None:
    """Rule 2. When the new tree's hook runs, the release's own entry is
    already on disk, the lock is free, the new tree is live and the note
    that a crash would need is still there."""
    seen: dict[str, object] = {}

    def at_the_hook(_: object) -> None:
        seen["ledger"] = bench.has_entry(REQUEST_ID)
        seen["lock_free"] = _lock_is_free(bench.spool_root)
        seen["live"] = bench.live()
        seen["note"] = SELF_NOTE in bench.running()

    bench.run.hooks[HOOK] = at_the_hook

    _run(bench, {SELF: NEW})

    assert seen == {"ledger": True, "lock_free": True, "live": NEW, "note": True}


def test_step_nine_swaps_nothing_and_says_when_it_will(bench: Bench) -> None:
    """The release's own entry is written before the swap, so it must say
    the swap is still to come and where its outcome is."""
    _run(bench, {SELF: NEW})

    detail = str(_step(bench.ledger(), "switch")["detail"])
    assert "after the ledger" in detail
    assert bench.ledger()["verify"] == []
    assert _verified(bench.self_entry()) == [SELF]


def test_a_failed_verify_puts_the_previous_tree_back(bench: Bench) -> None:
    """Rule 3. The previous tree goes back and its own hook runs again.
    The release's entry stands: it was written before the swap."""
    _fail_hook_once(bench, HOOK)

    _run(bench, {SELF: NEW})

    entry = bench.self_entry()
    assert entry["status"] == "restored"
    assert bench.live() == LIVE
    assert not (bench.components / f"{SELF}.new").exists()
    statuses = [row["status"] for row in cast("list[dict[str, object]]", entry["verify"])]
    assert statuses == ["failed", "ok"]
    assert bench.ledger()["status"] == "succeeded"
    assert bench.running() == []


def test_the_intake_restart_is_named_under_manual(bench: Bench) -> None:
    """Rule 4. `handover` carries no unit, so the listener keeps the old
    code until root restarts it, and the entry says so."""
    _run(bench, {SELF: NEW})

    manual = [str(one) for one in cast("list[object]", bench.self_entry()["manual"])]
    assert any("systemctl restart creche-handover-intake" in one for one in manual)


def test_a_set_switches_handover_after_everything_else(pair: Bench) -> None:
    """Rule 1 and rule 2 together: `chaperone` swaps and verifies inside the
    release, `handover` after its entry."""
    _run(pair, {"chaperone": PEP_NEW, SELF: NEW})

    release = pair.ledger()
    assert cast("dict[str, object]", release["manifest"])["order"] == ["chaperone", SELF]
    assert _verified(release) == ["chaperone"]
    assert _verified(pair.self_entry()) == [SELF]
    assert pair.live("chaperone") == PEP_NEW
    assert pair.live() == NEW


def test_a_failed_set_never_switches_handover(pair: Bench) -> None:
    """`chaperone` fails its hook, the set restores, and `handover` never
    moves: no note, no second entry, no staged tree left behind."""
    pair.run.fails["chaperone-verify"] = 1

    _run(pair, {"chaperone": PEP_NEW, SELF: NEW})

    assert pair.ledger()["status"] != "succeeded"
    assert not pair.has_entry(SELF_ENTRY)
    assert pair.live() == LIVE
    assert not (pair.components / f"{SELF}.new").exists()
    assert pair.running() == []


def test_the_pass_ends_once_the_executor_replaced_itself(bench: Bench) -> None:
    """The process that swapped runs the previous code. What is left in
    `requests/` re-fires the path unit, and that run is on the new tree."""
    write_request(bench.spool_root, REQUEST_ID, request_body({SELF: NEW}))
    later = request_body({SELF: NEW}, request_id=LATER_ID)
    write_request(bench.spool_root, LATER_ID, later)
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        handled = drain(spool, bench.wiring)
    finally:
        spool.close()

    assert handled == 1
    assert (bench.spool_root / REQUESTS_DIR / f"{LATER_ID}.json").is_file()


def test_a_vanished_staged_tree_never_moves_the_live_one(bench: Bench) -> None:
    """`swap_in` moves the live tree aside before it moves the new one in.
    With no `.new` to move, that would leave no executor at all."""

    def lose_the_staged_tree(notice: Notice) -> bool:
        del notice
        shutil.rmtree(bench.components / f"{SELF}.new", ignore_errors=True)

        return False

    bench.wiring = replace(bench.wiring, notify=lose_the_staged_tree)

    _run(bench, {SELF: NEW})

    assert bench.live() == LIVE
    assert bench.self_entry()["status"] == "failed"
    assert bench.running() == []


# -- a crash in the gap -----------------------------------------------------


def test_a_crash_after_the_swap_is_verified_by_the_next_run(bench: Bench) -> None:
    """The process dies with the new tree live and no second entry. The
    next run is the new code, and rule 3's hook runs there."""

    def die(_: object) -> None:
        raise Crash

    bench.run.hooks[HOOK] = die
    with pytest.raises(Crash):
        _run(bench, {SELF: NEW})

    assert bench.live() == NEW
    assert SELF_NOTE in bench.running()
    del bench.run.hooks[HOOK]

    handled = _run_pass(bench)

    assert handled == 0
    assert bench.self_entry()["status"] == "succeeded"
    assert bench.live() == NEW
    assert bench.running() == []


def test_a_crash_before_the_swap_removes_the_staged_tree(bench: Bench) -> None:
    """The note is there and the old tree is still live: the switch never
    ran. Root does not retry it. The staged tree goes, and the entry says
    so."""
    stamp_tree(bench.components, f"{SELF}.new", NEW)
    _plant_note(bench)

    _run_pass(bench)

    entry = bench.self_entry()
    assert entry["status"] == "failed"
    assert "never" in str(entry["reason"])
    assert bench.live() == LIVE
    assert not (bench.components / f"{SELF}.new").exists()
    assert not bench.run.ran(HOOK)
    assert bench.running() == []


def test_a_note_whose_entry_exists_is_only_cleared(bench: Bench) -> None:
    """The entry is the record. A second settle would overwrite it."""
    _plant_note(bench)
    (bench.spool_root / DONE_DIR / f"{SELF_ENTRY}.json").write_text("{}\n", encoding="utf-8")

    _run_pass(bench)

    assert bench.running() == []
    assert not bench.run.ran(HOOK)
    assert bench.self_entry() == {}
