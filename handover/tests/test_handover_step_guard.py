"""A step that raises an error it does not name still ends in the ledger.

`stage7-releases.md` §2.4: every outcome is a ledger entry, and a release
that did not succeed removes every tree it staged. A step raised `Refusal`
or `StepFailed` for the ends it knew. Any other error left the run: no
entry, a staged tree left behind and the rest of the pass not drained.

Two places keep that end on purpose: the moves of the swap, and the moves
of the restore. Inside them the run cannot say which tree is live. The
error leaves the run as a crash does, the note stays, and the next run
repairs (§2.4 row 10).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG_BY_NAME
from handover.executor import drain as drain_module
from handover.executor import steps
from handover.executor.drain import Counter, drain, handle, repair_unfinished
from handover.executor.install import Installer, Paths
from handover.executor.live_state import STAMP_FILE, installed_version
from handover.executor.spool import DONE_DIR, REQUESTS_DIR, Spool, SpoolError, SwitchNote
from handover.executor.steps import NOTHING_SWAPPED, UNNAMED_ERROR, Wiring
from handover.manifest import ComponentManifest
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

COMPONENT = "chaperone"
HOOK = f"{COMPONENT}-verify"
LIVE_VERSION = "2.0.3"
NEW_VERSION = "2.1.0"
OTHER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DF"

#: A `build` entry, so the manifest stages something. Contract 06 §8's shape.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'

#: A text that only the error holds. No ledger line may carry it.
ERROR_TEXT = "the-text-of-the-error"


@dataclass
class Bench:
    """One wired executor on a host where `chaperone` is live at 2.0.3."""

    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path

    @property
    def live(self) -> Path:
        return self.components / COMPONENT

    @property
    def staged(self) -> Path:
        return self.components / f"{COMPONENT}.new"

    def entry(self, request_id: str = REQUEST_ID) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{request_id}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def has_entry(self, request_id: str = REQUEST_ID) -> bool:
        return (self.spool_root / DONE_DIR / f"{request_id}.json").exists()

    def whole_record(self, request_id: str = REQUEST_ID) -> str:
        """The entry and the log beside it, as text."""
        done = self.spool_root / DONE_DIR
        entry = (done / f"{request_id}.json").read_text(encoding="utf-8")

        return entry + (done / f"{request_id}.log").read_text(encoding="utf-8")

    def lock_is_free(self) -> bool:
        fd = os.open(self.spool_root / "lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

        return True


def _step(entry: dict[str, object], name: str) -> dict[str, object] | None:
    rows = entry["steps"]
    assert isinstance(rows, list)
    for row in cast("list[object]", rows):
        assert isinstance(row, dict)
        fields = cast("dict[str, object]", row)
        if fields.get("name") == name:
            return fields

    return None


def _status(entry: dict[str, object], name: str) -> str | None:
    row = _step(entry, name)

    return None if row is None else str(row["status"])


def _manifest_for(bench: Bench, name: str) -> str:
    text = manifest_text(name).replace("/opt/components", str(bench.components))

    return text.replace("install:", BUILD_LINE, 1)


def _write_tree(bench: Bench) -> GitFake:
    """What the fake `git clone` leaves in each fetched tree."""

    def write_tree(destination: Path, component: str) -> None:
        row = CATALOG_BY_NAME[component]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(_manifest_for(bench, component), encoding="utf-8")

    return GitFake(write_tree)


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=grant_transport(),
        readers=fake_readers(latest={COMPONENT: NEW_VERSION}),
        api=green_api("agent-control", f"{COMPONENT}-v{NEW_VERSION}", SHA_OF[COMPONENT]),
    )
    built = Bench(spool_root, wiring, run, tmp_path / "components")
    stamp_tree(built.components, COMPONENT, LIVE_VERSION)
    run.dynamic = _write_tree(built)
    run.hooks["sync"] = lambda _: stamp_tree(built.components, f"{COMPONENT}.new", NEW_VERSION)
    return built


def _file(bench: Bench, request_id: str = REQUEST_ID) -> None:
    body = request_body({COMPONENT: NEW_VERSION}, request_id=request_id)
    write_request(bench.spool_root, request_id, body)


def _handle(bench: Bench) -> str:
    _file(bench)
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


def _build_leaves_a_directory_at_the_stamp(bench: Bench) -> None:
    """The build wrote a directory where root writes the version stamp, so
    the write of the stamp raises `IsADirectoryError`."""

    def build(_: object) -> None:
        tree = stamp_tree(bench.components, f"{COMPONENT}.new", NEW_VERSION)
        (tree / STAMP_FILE).unlink()
        (tree / STAMP_FILE).mkdir()

    bench.run.hooks["sync"] = build


def _fail_the_new_hook_once(bench: Bench) -> None:
    """The hook of the new tree fails. The hook of the tree that step 10
    puts back passes."""
    calls = {"n": 0}
    bench.run.fails[HOOK] = 1

    def pass_after_the_first_call(_: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop(HOOK, None)

    bench.run.hooks[HOOK] = pass_after_the_first_call


# -- before the swap: the entry, and no tree left behind ----------------------


def test_an_os_error_in_the_stage_ends_in_the_ledger(bench: Bench) -> None:
    """Step 8 could not write its stamp. The run wrote no entry and left
    the staged tree, and the error ended the whole pass."""
    _build_leaves_a_directory_at_the_stamp(bench)

    line = _handle(bench)

    entry = bench.entry()
    assert "failed" in line
    assert entry["status"] == "failed"
    assert _status(entry, "stage") == "failed"
    assert _status(entry, "switch") is None
    assert entry["reason"] == f"stage: {UNNAMED_ERROR}: IsADirectoryError (EISDIR)"
    assert not bench.staged.exists()
    assert f"removed {bench.staged}" in cast("list[str]", entry["log_tail"])
    assert "IsADirectoryError raised at live_state.py" in bench.whole_record()
    assert installed_version(bench.live) == LIVE_VERSION
    assert bench.lock_is_free()
    assert not list((bench.spool_root / REQUESTS_DIR).glob("*.json"))


def test_a_note_that_cannot_be_written_ends_in_the_ledger(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch note is written before the move. A note that the spool
    refuses means that nothing moved, so the run records itself."""

    def refuse(self: Spool, request_id: str, note: SwitchNote) -> None:
        del self, request_id, note
        raise SpoolError(f"cannot create the note: {ERROR_TEXT}")

    monkeypatch.setattr(Spool, "note_switch", refuse)

    _handle(bench)

    entry = bench.entry()
    restore = _step(entry, "restore")
    assert entry["status"] == "failed"
    assert _status(entry, "switch") == "failed"
    assert entry["reason"] == f"switch: {UNNAMED_ERROR}: SpoolError"
    assert restore is not None
    assert restore["status"] == "skipped"
    assert restore["detail"] == NOTHING_SWAPPED
    assert ERROR_TEXT not in bench.whole_record()
    assert not bench.staged.exists()
    assert not (bench.components / f"{COMPONENT}.prev").exists()
    assert installed_version(bench.live) == LIVE_VERSION


def test_the_ledger_names_the_type_of_an_error_and_never_its_text(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.6: a reason is never built from input. The text of an error can
    hold a path or the bytes of a file, so the ledger keeps the type and
    the place in the code."""

    def raise_one(tree: Path, version: str) -> None:
        del tree, version
        raise ValueError(ERROR_TEXT)

    monkeypatch.setattr(steps, "write_stamp", raise_one)

    _handle(bench)

    entry = bench.entry()
    record = bench.whole_record()
    assert entry["status"] == "failed"
    assert entry["reason"] == f"stage: {UNNAMED_ERROR}: ValueError"
    assert ERROR_TEXT not in record
    assert "ValueError raised at steps.py:" in record
    assert "in _stage_one" in record
    assert not bench.staged.exists()


def test_an_error_without_a_number_names_its_type_alone(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_one(tree: Path, version: str) -> None:
        del tree, version
        raise OSError(ERROR_TEXT)

    monkeypatch.setattr(steps, "write_stamp", raise_one)

    _handle(bench)

    assert bench.entry()["reason"] == f"stage: {UNNAMED_ERROR}: OSError"
    assert ERROR_TEXT not in bench.whole_record()


def test_an_error_with_a_text_for_its_number_names_its_type_alone(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `OSError` that is built from two texts holds the first one as its
    number. The ledger takes a number from the table of the system only."""

    def raise_one(tree: Path, version: str) -> None:
        del tree, version
        raise OSError(ERROR_TEXT, "a second text")

    monkeypatch.setattr(steps, "write_stamp", raise_one)

    _handle(bench)

    assert bench.entry()["reason"] == f"stage: {UNNAMED_ERROR}: OSError"
    assert ERROR_TEXT not in bench.whole_record()


def test_the_pass_goes_on_after_a_step_that_raised(bench: Bench) -> None:
    """`requests/` is drained on every run (§2.2). The error of one request
    ended the pass, and the request after it stayed in `requests/`."""
    _build_leaves_a_directory_at_the_stamp(bench)
    _file(bench)
    _file(bench, OTHER_ID)
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        handled = drain(spool, bench.wiring)
    finally:
        spool.close()

    assert handled == 2
    assert bench.entry()["status"] == "failed"
    assert bench.entry(OTHER_ID)["status"] == "failed"
    assert not list((bench.spool_root / REQUESTS_DIR).glob("*.json"))


# -- after the swap: step 10 acts ---------------------------------------------


def test_an_error_after_the_swap_puts_the_previous_tree_back(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree is live and the unit refresh raises. The component is
    deployed, so step 10 puts the previous tree back, as it does for a
    verify hook that fails."""

    def raise_one(self: Installer, manifest: object, tree: Path) -> str | None:
        del self, manifest, tree
        raise OSError(errno.EIO, ERROR_TEXT)

    monkeypatch.setattr(Installer, "refresh_unit", raise_one)

    _handle(bench)

    entry = bench.entry()
    assert entry["status"] == "restored"
    assert entry["reason"] == f"switch: {UNNAMED_ERROR}: OSError (EIO)"
    assert _status(entry, "restore") == "ok"
    assert installed_version(bench.live) == LIVE_VERSION
    assert not bench.staged.exists()


def test_an_error_in_the_restore_is_a_failed_restore(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.4 row 10: a restore that fails is `failed, step: restore`, and
    the executor stops. The error comes after the moves of the restore, so
    the run can say which tree is live: the previous one."""
    _fail_the_new_hook_once(bench)
    restart = Installer.restart
    calls = {"n": 0}

    def raise_in_the_restore(self: Installer, manifest: ComponentManifest) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError(errno.EIO, ERROR_TEXT)

        restart(self, manifest)

    monkeypatch.setattr(Installer, "restart", raise_in_the_restore)

    _handle(bench)

    entry = bench.entry()
    restore = _step(entry, "restore")
    assert entry["status"] == "failed"
    assert entry["reason"] == f"restore: {UNNAMED_ERROR}: OSError (EIO)"
    assert restore is not None
    assert restore["status"] == "failed"
    assert restore["detail"] == f"{UNNAMED_ERROR}: OSError (EIO)"
    assert installed_version(bench.live) == LIVE_VERSION
    assert not bench.staged.exists()
    assert ERROR_TEXT not in bench.whole_record()


# -- inside the swap: the note, and the next run ------------------------------


def _half_a_swap(self: Installer, paths: Paths) -> None:
    """The first rename of `swap_in`, then the error of the second one."""
    del self
    os.rename(paths.to, paths.prev)

    raise OSError(errno.EIO, ERROR_TEXT)


def test_an_error_inside_the_swap_leaves_the_note_for_the_next_run(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live tree moved away and the new one did not move in. The run
    cannot say which tree is live, so it ends as a crash does: it writes no
    entry, and the note stays. The next run puts the previous tree back
    and writes the entry of the repair."""
    monkeypatch.setattr(Installer, "swap_in", _half_a_swap)

    with pytest.raises(OSError, match=ERROR_TEXT):
        _handle(bench)

    assert not bench.has_entry()
    assert not bench.live.exists()
    assert bench.lock_is_free()

    spool = Spool(str(bench.spool_root), this_uid())
    try:
        assert [one.note.component for one in spool.unfinished()] == [COMPONENT]
        lines = repair_unfinished(spool, bench.wiring)
        assert not spool.unfinished()
    finally:
        spool.close()

    assert "repaired (restored)" in lines[0]
    assert installed_version(bench.live) == LIVE_VERSION
    assert not bench.staged.exists()
    assert bench.entry(f"{REQUEST_ID}-{COMPONENT}")["status"] == "restored"


def _half_a_swap_back(self: Installer, paths: Paths) -> None:
    """The first rename of `swap_back`, then the error of the second one."""
    del self
    os.rename(paths.to, paths.new)

    raise OSError(errno.EIO, ERROR_TEXT)


def test_an_error_inside_the_restore_moves_leaves_the_note_too(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 10 moves the trees as step 9 does, in reverse. The tree that
    failed its hook moved away and the previous one did not move in. The
    run ends as a crash does and keeps each tree. The next run puts the
    previous tree in service and writes the entry of the repair."""
    _fail_the_new_hook_once(bench)
    swap_back = Installer.swap_back
    monkeypatch.setattr(Installer, "swap_back", _half_a_swap_back)

    with pytest.raises(OSError, match=ERROR_TEXT):
        _handle(bench)

    assert not bench.has_entry()
    assert not bench.live.exists()
    assert bench.staged.exists()
    assert bench.lock_is_free()

    monkeypatch.setattr(Installer, "swap_back", swap_back)
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        assert [one.note.component for one in spool.unfinished()] == [COMPONENT]
        lines = repair_unfinished(spool, bench.wiring)
        assert not spool.unfinished()
    finally:
        spool.close()

    assert "repaired (restored)" in lines[0]
    assert installed_version(bench.live) == LIVE_VERSION
    assert not bench.staged.exists()
    assert bench.entry(f"{REQUEST_ID}-{COMPONENT}")["status"] == "restored"


# -- the repair loop ----------------------------------------------------------


def test_a_repair_that_raises_does_not_end_the_pass(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repairs run before `requests/` is drained. The loop caught two
    types of error. Any other one ended the run before the drain, and the
    note stayed, so each later run ended at the same place."""
    spool = Spool(str(bench.spool_root), this_uid())
    for request_id in (REQUEST_ID, OTHER_ID):
        note = SwitchNote(COMPONENT, str(bench.live), f"{bench.live}.prev", None)
        spool.note_switch(request_id, note)

    seen: list[str] = []

    def raise_one(spool: Spool, wiring: Wiring, request_id: str, note: SwitchNote) -> None:
        del spool, wiring, note
        seen.append(request_id)
        raise ValueError(ERROR_TEXT)

    monkeypatch.setattr(drain_module, "repair", raise_one)
    try:
        lines = repair_unfinished(spool, bench.wiring)
        left = spool.unfinished()
    finally:
        spool.close()

    assert seen == [REQUEST_ID, OTHER_ID]
    assert lines == [
        f"{REQUEST_ID}: repair failed (ValueError)",
        f"{OTHER_ID}: repair failed (ValueError)",
    ]
    assert left == []
