"""What step 8 finds at the place of each unit file (contract 06 §8.3 item 3).

`Installer.check_adopt`. Root may link over nothing, over its own link, or
over a regular file with the tree's bytes. Each other thing at the place
is refused with code `unit`, so a unit that nobody put in creche-release is
never overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.host import Host
from handover.executor.install import Adopt, Installer
from handover.executor.units import unit_files
from handover_executor_fixtures import FakeRun, fake_host

HOST = "host"
TIMER = "units/user/creche-follow.timer"
DROP_IN = "units/system/creche-chaperone.service.d/creche.conf"
TEXT = "[Timer]\nOnCalendar=minutely\n"


def _bench(tmp_path: Path) -> tuple[Host, Path, Path]:
    """A host, a staged tree with the two unit files, and the live path."""
    host = fake_host(tmp_path, FakeRun())
    live = tmp_path / "components" / HOST
    staged = tmp_path / "components" / f"{HOST}.new"
    for name in (TIMER, DROP_IN):
        (staged / name).parent.mkdir(parents=True, exist_ok=True)
        (staged / name).write_text(TEXT, encoding="utf-8")

    return host, staged, live


def _check(host: Host, staged: Path, live: Path) -> tuple[Adopt, ...]:
    files = unit_files(HOST, staged, frozenset())

    return Installer(host).check_adopt(HOST, files, staged, live)


def _refusal(host: Host, staged: Path, live: Path) -> Refusal:
    with pytest.raises(Refusal) as caught:
        _check(host, staged, live)

    assert caught.value.code is RefusalCode.UNIT

    return caught.value


def test_nothing_installed_is_absent(tmp_path: Path) -> None:
    assert _check(*_bench(tmp_path)) == (Adopt.ABSENT, Adopt.ABSENT)


def test_a_file_with_the_same_bytes_is_adopted(tmp_path: Path) -> None:
    """The first `host` release meets the units installed by hand."""
    host, staged, live = _bench(tmp_path)
    (host.user_unit_dir / "creche-follow.timer").write_text(TEXT, encoding="utf-8")
    drop_in = host.system_unit_dir / "creche-chaperone.service.d" / "creche.conf"
    drop_in.parent.mkdir()
    drop_in.write_text(TEXT, encoding="utf-8")

    assert _check(host, staged, live) == (Adopt.SAME_BYTES, Adopt.SAME_BYTES)


def test_the_link_into_the_live_tree_is_linked(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    (host.user_unit_dir / "creche-follow.timer").symlink_to(live / TIMER)

    assert _check(host, staged, live) == (Adopt.ABSENT, Adopt.LINKED)


def test_a_file_with_other_bytes_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    (host.user_unit_dir / "creche-follow.timer").write_text("[Timer]\n", encoding="utf-8")

    refusal = _refusal(host, staged, live)

    assert refusal.detail.startswith(TIMER)
    assert "holds other bytes" in refusal.detail


def test_a_drop_in_with_other_bytes_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    drop_in = host.system_unit_dir / "creche-chaperone.service.d" / "creche.conf"
    drop_in.parent.mkdir()
    drop_in.write_text("[Service]\n", encoding="utf-8")

    assert _refusal(host, staged, live).detail.startswith(DROP_IN)


def test_a_link_into_another_tree_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    (tmp_path / "other.timer").write_text(TEXT, encoding="utf-8")
    (host.user_unit_dir / "creche-follow.timer").symlink_to(tmp_path / "other.timer")

    assert "a link that root did not make" in _refusal(host, staged, live).detail


def test_a_link_into_the_staged_tree_is_refused(tmp_path: Path) -> None:
    """Each link names the live tree (§8.3 item 2), never `.new`."""
    host, staged, live = _bench(tmp_path)
    (host.user_unit_dir / "creche-follow.timer").symlink_to(staged / TIMER)

    assert "a link that root did not make" in _refusal(host, staged, live).detail


def test_a_link_to_another_file_of_the_live_tree_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    (host.user_unit_dir / "creche-follow.timer").symlink_to(live / DROP_IN)

    assert "a link that root did not make" in _refusal(host, staged, live).detail


def test_a_dangling_link_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    (host.user_unit_dir / "creche-follow.timer").symlink_to(tmp_path / "gone")

    assert "a link that root did not make" in _refusal(host, staged, live).detail


def test_a_directory_in_the_place_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    (host.user_unit_dir / "creche-follow.timer").mkdir()

    assert "not a regular file or a link" in _refusal(host, staged, live).detail


def test_a_fifo_in_the_place_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    os.mkfifo(host.user_unit_dir / "creche-follow.timer")

    assert "not a regular file or a link" in _refusal(host, staged, live).detail


def test_a_drop_in_directory_that_is_a_file_is_refused(tmp_path: Path) -> None:
    host, staged, live = _bench(tmp_path)
    (host.system_unit_dir / "creche-chaperone.service.d").write_text("", encoding="utf-8")

    assert "root cannot read its installed place" in _refusal(host, staged, live).detail
