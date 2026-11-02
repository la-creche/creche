"""Step 9 of a `host` release links its unit files (contract 06 §8.3).

`units.units_diff` compares the two trees before a swap.
`Installer.retire_units` stops what goes, before the swap.
`Installer.link_units` links, reloads and starts, after it.

The children are recorded, never run, except `ln` and `rm`: a hook makes
and removes the real link, so a later check reads what the host would
hold.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from handover.executor.host import As, Command, Host
from handover.executor.install import INSTALL, LN, RM, Installer, StepFailed
from handover.executor.units import Change, units_diff
from handover_executor_fixtures import FakeRun, fake_host

HOST = "host"
TIMER = "units/user/creche-follow.timer"
SERVICE = "units/user/creche-follow.service"
TEMPLATE = "units/user/creche-library@.timer"
DROP_IN = "units/system/creche-chaperone.service.d/creche.conf"
OLD = "[Timer]\nOnCalendar=hourly\n"
NEW = "[Timer]\nOnCalendar=minutely\n"


def _write(tree: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(text, encoding="utf-8")

    return tree


def _real_links(run: FakeRun) -> None:
    """`ln -sfT <target> <place>`, `rm -f -- <place>` and `install -d <dir>`,
    for real."""

    def link(command: Command) -> None:
        target, place = command.argv[-2], Path(command.argv[-1])
        if place.is_symlink() or place.exists():
            place.unlink()

        place.symlink_to(target)

    def remove(command: Command) -> None:
        Path(command.argv[-1]).unlink(missing_ok=True)

    def make_dir(command: Command) -> None:
        if "-d" in command.argv:
            Path(command.argv[-1]).mkdir(parents=True, exist_ok=True)

    run.hooks[LN] = link
    run.hooks[RM] = remove
    run.hooks[INSTALL] = make_dir


def _bench(tmp_path: Path) -> tuple[Host, FakeRun, Path]:
    run = FakeRun()
    _real_links(run)

    return fake_host(tmp_path, run), run, tmp_path / "components" / HOST


def _identities(run: FakeRun, word: str) -> set[As]:
    return {one.identity for one in run.seen if any(word in part for part in one.argv)}


# ---- the diff ----------------------------------------------------------------


def test_with_no_live_tree_every_file_is_new(tmp_path: Path) -> None:
    now = _write(tmp_path / "now", {TIMER: NEW, DROP_IN: NEW})

    diff = units_diff(HOST, None, now)

    assert [change for _, change in diff.now] == [Change.NEW, Change.NEW]
    assert diff.gone == ()


def test_the_diff_reads_new_changed_same_and_gone(tmp_path: Path) -> None:
    before = _write(tmp_path / "before", {TIMER: OLD, SERVICE: OLD, TEMPLATE: OLD})
    now = _write(tmp_path / "now", {TIMER: NEW, SERVICE: OLD, DROP_IN: NEW})

    diff = units_diff(HOST, before, now)

    assert [(str(one.relative), change) for one, change in diff.now] == [
        (DROP_IN, Change.NEW),
        (SERVICE, Change.SAME),
        (TIMER, Change.CHANGED),
    ]
    assert [str(one.relative) for one in diff.gone] == [TEMPLATE]


# ---- link_units ----------------------------------------------------------------


def test_a_new_timer_is_linked_and_started_as_the_operator(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    _write(live, {TIMER: NEW})

    Installer(host).link_units(HOST, units_diff(HOST, None, live), live)

    place = host.user_unit_dir / "creche-follow.timer"
    assert os.readlink(place) == str(live / TIMER)
    assert run.argv_lines() == [
        f"{LN} -sfT {live / TIMER} {place}",
        "/usr/bin/systemctl daemon-reload",
        "/usr/bin/systemctl --user daemon-reload",
        "/usr/bin/systemctl --user enable --now creche-follow.timer",
    ]
    assert _identities(run, LN) == {As.OPERATOR}
    assert _identities(run, "enable") == {As.OPERATOR}


def test_a_file_installed_by_hand_with_the_same_bytes_becomes_the_link(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    _write(live, {TIMER: NEW})
    (host.user_unit_dir / "creche-follow.timer").write_text(NEW, encoding="utf-8")

    Installer(host).link_units(HOST, units_diff(HOST, None, live), live)

    assert (host.user_unit_dir / "creche-follow.timer").is_symlink()


def test_a_changed_timer_that_is_already_linked_is_restarted(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    before = _write(tmp_path / "before", {TIMER: OLD})
    _write(live, {TIMER: NEW})
    (host.user_unit_dir / "creche-follow.timer").symlink_to(live / TIMER)

    Installer(host).link_units(HOST, units_diff(HOST, before, live), live)

    assert not run.ran(LN)
    assert run.argv_lines()[-1] == "/usr/bin/systemctl --user restart creche-follow.timer"


def test_a_same_timer_is_only_reloaded(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    before = _write(tmp_path / "before", {TIMER: NEW})
    _write(live, {TIMER: NEW})
    (host.user_unit_dir / "creche-follow.timer").symlink_to(live / TIMER)

    Installer(host).link_units(HOST, units_diff(HOST, before, live), live)

    assert run.argv_lines() == [
        "/usr/bin/systemctl daemon-reload",
        "/usr/bin/systemctl --user daemon-reload",
    ]


@pytest.mark.parametrize("name", [SERVICE, TEMPLATE])
def test_a_new_service_or_template_is_linked_and_not_started(tmp_path: Path, name: str) -> None:
    host, run, live = _bench(tmp_path)
    _write(live, {name: NEW})

    Installer(host).link_units(HOST, units_diff(HOST, None, live), live)

    assert run.ran(LN)
    assert not run.ran("enable")
    assert not run.ran("restart")


def test_a_drop_in_is_linked_by_root_in_its_own_directory(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    _write(live, {DROP_IN: NEW})

    Installer(host).link_units(HOST, units_diff(HOST, None, live), live)

    directory = host.system_unit_dir / "creche-chaperone.service.d"
    assert run.argv_lines()[0] == f"/usr/bin/install -d -m 0755 {directory}"
    assert _identities(run, LN) == {As.ROOT}
    assert not run.ran("restart")


def test_a_place_that_changed_since_step_8_fails_the_switch(tmp_path: Path) -> None:
    """The swap is done, so it is a failure and step 10 restores."""
    host, run, live = _bench(tmp_path)
    _write(live, {TIMER: NEW})
    (host.user_unit_dir / "creche-follow.timer").write_text(OLD, encoding="utf-8")

    with pytest.raises(StepFailed, match="holds other bytes"):
        Installer(host).link_units(HOST, units_diff(HOST, None, live), live)

    assert not run.ran(LN)


# ---- a file that goes ------------------------------------------------------------


def test_a_gone_timer_is_stopped_before_the_swap_and_unlinked_after(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    _write(live, {TIMER: OLD})
    place = host.user_unit_dir / "creche-follow.timer"
    place.symlink_to(live / TIMER)
    staged = _write(tmp_path / "staged", {SERVICE: NEW})
    diff = units_diff(HOST, live, staged)
    installer = Installer(host)

    installer.retire_units(diff.gone, live)

    assert run.argv_lines() == ["/usr/bin/systemctl --user disable --now creche-follow.timer"]

    (live / TIMER).unlink()
    installer.link_units(HOST, diff, live)

    assert not place.is_symlink()
    assert f"{RM} -f -- {place}" in run.argv_lines()
    assert _identities(run, RM) == {As.OPERATOR}


def test_a_gone_file_that_root_did_not_link_stays(tmp_path: Path) -> None:
    """Contract 06 §8.3 item 4: a file root did not link stays, and so does
    what it runs."""
    host, run, live = _bench(tmp_path)
    _write(live, {TIMER: OLD})
    place = host.user_unit_dir / "creche-follow.timer"
    place.write_text(OLD, encoding="utf-8")
    diff = units_diff(HOST, live, _write(tmp_path / "staged", {SERVICE: NEW}))
    installer = Installer(host)

    installer.retire_units(diff.gone, live)
    installer.link_units(HOST, diff, live)

    assert place.read_text(encoding="utf-8") == OLD
    assert not run.ran("disable")
    assert not run.ran(RM)


def test_a_gone_template_is_unlinked_and_not_stopped(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    _write(live, {TEMPLATE: OLD})
    place = host.user_unit_dir / "creche-library@.timer"
    place.symlink_to(live / TEMPLATE)
    diff = units_diff(HOST, live, _write(tmp_path / "staged", {SERVICE: NEW}))
    installer = Installer(host)

    installer.retire_units(diff.gone, live)
    installer.link_units(HOST, diff, live)

    assert not run.ran("disable")
    assert not place.is_symlink()


def test_a_failing_link_fails_the_switch(tmp_path: Path) -> None:
    host, run, live = _bench(tmp_path)
    run.fails[LN] = 1
    _write(live, {TIMER: NEW})

    with pytest.raises(StepFailed, match=re.escape(f"cannot link {TIMER}")):
        Installer(host).link_units(HOST, units_diff(HOST, None, live), live)
