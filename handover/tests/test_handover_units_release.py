"""One `host` release, end to end (contract 06 §8.3).

The whole executor runs, as in `test_handover_executor_steps.py`: a real
spool, the real resolver and the real ten steps. Only the child, the GitHub
API, the phone and the clock are fakes.

What a `host` release does today: step 8 copies the fetched tree
(`install.py` rule 10) and reads its unit files (`executor/units.py`).
Step 9 swaps the tree in and runs `host-verify`. No unit file is linked
yet.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG
from handover.executor.approval import Decision, Summary, Verdict
from handover.executor.drain import Counter, handle
from handover.executor.host import Command
from handover.executor.install import INSTALL, LN, RM
from handover.executor.live_state import STAMP_FILE
from handover.executor.quiet import Busy
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
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

HOST = "host"
LIVE = "0.1.0"
NEW = "0.1.1"
TIMER = "units/user/creche-follow.timer"
DROP_IN = "units/system/creche-chaperone.service.d/creche.conf"
HOOK = f"bin/{HOST}-verify"


def _release(
    tmp_path: Path,
    files: tuple[str, ...],
    installed: dict[str, str] | None = None,
    run: FakeRun | None = None,
    live_files: dict[str, str] | None = None,
    edit: Callable[[Wiring], Wiring] | None = None,
) -> tuple[dict[str, object], Path]:
    """File one request for `host` at `NEW`, run it, and return the ledger
    entry and the components root. The fetched tree holds the manifest,
    the verify hook and `files`. `installed` maps a user unit name to the
    text of the file that the host already has. `live_files` maps a path
    of the live tree to its text. `edit` changes the wiring."""
    spool_root = make_spool_dirs(tmp_path)
    run = run if run is not None else FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    for unit, text in (installed or {}).items():
        (host.user_unit_dir / unit).write_text(text, encoding="utf-8")

    components = tmp_path / "components"
    live = stamp_tree(components, HOST, LIVE)
    for name, text in (live_files or {}).items():
        (live / name).parent.mkdir(parents=True, exist_ok=True)
        (live / name).write_text(text, encoding="utf-8")

    manifests = {
        row.name: manifest_text(row.name).replace("/opt/components", str(components))
        for row in CATALOG
    }

    def write_tree(destination: Path, component: str) -> None:
        text = manifests.get(component)
        if text is None:
            return

        destination.mkdir(parents=True, exist_ok=True)
        (destination / "component.yaml").write_text(text, encoding="utf-8")
        (destination / ".git").mkdir()
        for name in (HOOK, *files):
            (destination / name).parent.mkdir(parents=True, exist_ok=True)
            (destination / name).write_text(f"# {name}\n", encoding="utf-8")

        (destination / HOOK).chmod(0o755)

    run.dynamic = GitFake(write_tree)
    wiring = Wiring(
        host=host,
        transport=grant_transport(),
        readers=fake_readers(latest={HOST: NEW}),
        api=green_api("creche-release", f"{HOST}-v{NEW}", SHA_OF[HOST]),
    )
    wiring = edit(wiring) if edit is not None else wiring
    write_request(spool_root, REQUEST_ID, request_body({HOST: NEW}))
    spool = Spool(str(spool_root), this_uid())
    try:
        handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
    finally:
        spool.close()

    path = spool_root / DONE_DIR / f"{REQUEST_ID}.json"
    loaded = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))

    return loaded, components


def test_a_host_release_installs_the_fetched_tree(tmp_path: Path) -> None:
    entry, components = _release(tmp_path, (TIMER, DROP_IN))

    assert entry["status"] == "succeeded", entry
    live = components / HOST
    assert (live / STAMP_FILE).read_text(encoding="utf-8").strip() == NEW
    assert (live / TIMER).is_file()
    assert (live / DROP_IN).is_file()
    assert not (live / ".git").exists()


@pytest.mark.parametrize(
    ("name", "detail"),
    [
        ("units/user/notes.txt", "is not units/user/<unit>"),
        ("units/user/creche-handover.service", "a handover unit"),
    ],
)
def test_a_tree_with_a_wrong_unit_file_is_refused_and_the_host_is_unchanged(
    tmp_path: Path, name: str, detail: str
) -> None:
    entry, components = _release(tmp_path, (TIMER, name))

    assert entry["status"] == "refused", entry
    assert entry["refused_check"] == "unit"
    assert detail in str(entry["reason"])
    assert (components / HOST / STAMP_FILE).read_text(encoding="utf-8").strip() == LIVE
    assert not (components / f"{HOST}.new").exists()


def test_a_unit_installed_by_hand_with_the_same_bytes_lets_the_release_land(
    tmp_path: Path,
) -> None:
    """The first `host` release meets the units installed by hand
    (contract 06 §8.3 item 3)."""
    entry, _ = _release(tmp_path, (TIMER,), {"creche-follow.timer": f"# {TIMER}\n"})

    assert entry["status"] == "succeeded", entry


def test_a_unit_installed_by_hand_with_other_bytes_is_refused(tmp_path: Path) -> None:
    entry, components = _release(tmp_path, (TIMER,), {"creche-follow.timer": "[Timer]\n"})

    assert entry["status"] == "refused", entry
    assert entry["refused_check"] == "unit"
    assert "holds other bytes" in str(entry["reason"])
    assert (components / HOST / STAMP_FILE).read_text(encoding="utf-8").strip() == LIVE
    assert (tmp_path / "user-units" / "creche-follow.timer").read_text(encoding="utf-8") == (
        "[Timer]\n"
    )


# ---- step 9 links the units, and step 10 puts them back -------------------------

OTHER_TIMER = "units/user/creche-watchdog.timer"


def _linking_run() -> FakeRun:
    """A run whose `ln`, `rm` and `install -d` act on the real files, so each
    later check reads what the host would hold."""
    run = FakeRun(answers=git_host_answers())

    def link(command: Command) -> None:
        target, place = command.argv[-2], Path(command.argv[-1])
        place.unlink(missing_ok=True)
        place.symlink_to(target)

    def remove(command: Command) -> None:
        Path(command.argv[-1]).unlink(missing_ok=True)

    def make_dir(command: Command) -> None:
        if "-d" in command.argv:
            Path(command.argv[-1]).mkdir(parents=True, exist_ok=True)

    run.hooks[LN] = link
    run.hooks[RM] = remove
    run.hooks[INSTALL] = make_dir

    return run


def test_the_switch_links_each_unit_and_starts_each_new_timer(tmp_path: Path) -> None:
    run = _linking_run()
    entry, components = _release(tmp_path, (TIMER,), run=run)

    assert entry["status"] == "succeeded", entry
    place = tmp_path / "user-units" / "creche-follow.timer"
    assert os.readlink(place) == str(components / HOST / TIMER)
    lines = run.argv_lines()
    enable = lines.index("/usr/bin/systemctl --user enable --now creche-follow.timer")
    assert enable < next(i for i, line in enumerate(lines) if HOOK in line)


def test_a_restore_puts_the_units_of_the_previous_tree_back(tmp_path: Path) -> None:
    """Contract 06 §8.3 item 2: a restore runs items 4 to 6 again, over the
    previous tree. The new timer goes, and the changed one restarts."""
    run = _linking_run()
    run.fails[HOOK] = 1
    live_files = {TIMER: "[Timer]\nOnCalendar=hourly\n"}
    place = tmp_path / "user-units" / "creche-follow.timer"

    entry, components = _release(tmp_path, (TIMER, OTHER_TIMER), run=run, live_files=live_files)

    assert entry["status"] != "succeeded", entry
    lines = run.argv_lines()
    assert "/usr/bin/systemctl --user enable --now creche-watchdog.timer" in lines
    assert "/usr/bin/systemctl --user disable --now creche-watchdog.timer" in lines
    assert lines.count("/usr/bin/systemctl --user restart creche-follow.timer") == 2
    assert not (tmp_path / "user-units" / "creche-watchdog.timer").is_symlink()
    assert os.readlink(place) == str(components / HOST / TIMER)
    assert (components / HOST / TIMER).read_text(encoding="utf-8") == live_files[TIMER]


# ---- a drop-in change restarts its unit, named before the tap ---------------------

CHAPERONE = "creche-chaperone.service"


def _showing(shown: list[Summary]) -> Callable[[Wiring], Wiring]:
    """A phone that keeps each summary it shows, then taps yes."""

    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, wait_s
        shown.append(summary)

        return Decision(Verdict.GRANTED, gate, 1758153712.0)

    return lambda wiring: replace(wiring, transport=transport)


def _chaperone_installed(tmp_path: Path) -> None:
    (tmp_path / "system-units").mkdir(exist_ok=True)
    (tmp_path / "system-units" / CHAPERONE).write_text("[Service]\n", encoding="utf-8")


def test_a_new_drop_in_is_named_before_the_tap_and_restarts_its_unit(tmp_path: Path) -> None:
    shown: list[Summary] = []
    run = _linking_run()
    _chaperone_installed(tmp_path)

    entry, _ = _release(tmp_path, (TIMER, DROP_IN), run=run, edit=_showing(shown))

    assert entry["status"] == "succeeded", entry
    assert [summary.restarts for summary in shown] == [f"{HOST}: {CHAPERONE}"]
    assert f"/usr/bin/systemctl restart {CHAPERONE}" in run.argv_lines()


def test_an_unchanged_drop_in_restarts_nothing(tmp_path: Path) -> None:
    shown: list[Summary] = []
    run = _linking_run()
    _chaperone_installed(tmp_path)
    place = tmp_path / "system-units" / f"{CHAPERONE}.d" / "creche.conf"
    place.parent.mkdir(parents=True)
    place.symlink_to(tmp_path / "components" / HOST / DROP_IN)

    entry, _ = _release(
        tmp_path, (DROP_IN,), run=run, live_files={DROP_IN: f"# {DROP_IN}\n"}, edit=_showing(shown)
    )

    assert entry["status"] == "succeeded", entry
    assert [summary.restarts for summary in shown] == [f"{HOST}: no unit"]
    assert not run.ran(f"restart {CHAPERONE}")


def test_a_drop_in_that_moves_after_the_tap_fails_the_switch(tmp_path: Path) -> None:
    """The live tree changes between step 5 and step 9. Step 9 would then
    restart a unit the tap did not name, so it moves nothing of `host`."""
    run = _linking_run()
    _chaperone_installed(tmp_path)

    def busy_now() -> Busy:
        live = tmp_path / "components" / HOST / DROP_IN
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("[Service]\nEnvironment=MOVED=1\n", encoding="utf-8")

        return Busy.NO

    entry, components = _release(
        tmp_path,
        (DROP_IN,),
        run=run,
        live_files={DROP_IN: f"# {DROP_IN}\n"},
        edit=lambda wiring: replace(wiring, busy_now=busy_now),
    )

    assert entry["status"] != "succeeded", entry
    assert "the drop-in restarts differ" in str(entry["reason"])
    assert (components / HOST / STAMP_FILE).read_text(encoding="utf-8").strip() == LIVE
    assert not run.ran(LN)
