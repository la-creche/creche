"""A release can change a user unit (`stage7-releases.md` §2.4 rows 8 and 9).

The release unit's `UMask=0027` makes root's per-request clone
`root:root 0750`, and a user unit is installed as the operator. A step 9 that
installed the unit file straight out of that clone, from
`<work root>/<request id>/<name>/systemd/<unit>`, would fail every `noticeboard`,
`attendance` or `caregiver` release whose unit file changed, at the refresh,
with `Permission denied`. Root installing it instead would make root
the operator's deputy (`handover/AGENTS.md` rule 5a). So step 8 copies the unit
file into the staged tree before `normalize_modes`, and step 9 installs it
from the LIVE tree.

The fake host models the one fact that matters: the operator is "other" to every
file root writes. Its `install`, run as the operator, refuses a file that is not
readable by other along its whole path below `tmp_path`, and the fake
clone carries the modes `UMask=0027` gives it. A file of the operator's own gets
explicit modes that let them read it. Seven things are pinned.

1. A user unit whose file changed is installed from the live tree, as
   the operator, then `systemctl --user daemon-reload` runs as the operator.
2. The staged tree carries the unit at `systemd/<unit>`, `0644`, under the
   release unit's own umask.
3. A system unit is installed from the live tree, as root.
4. The crash repair of a user unit puts the kept copy back as the operator and
   reads nothing out of the clone.
5. A source with no unit file stages none.
6. A unit file that resolves outside the clone is never staged.
7. A unit file that cannot be staged fails step 8, and nothing moves.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME
from handover.executor.drain import Counter, handle, repair_unfinished
from handover.executor.host import As, Command
from handover.executor.host import Result as RunResult
from handover.executor.install import INSTALL, SYSTEMCTL
from handover.executor.live_state import installed_version
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

LIVE_VERSION = "0.1.3"
NEW_VERSION = "0.1.4"

#: The fixture manifests carry no `build`, and the executor stages nothing
#: without one.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen", "--no-editable"]\ninstall:'

CHAPERONE_UNIT = "creche-chaperone.service"
NOTICEBOARD_UNIT = "creche-noticeboard.service"

#: `fake_host`'s two unit directories: `/etc/systemd/system`, and
#: `~operator/.config/systemd/user`.
SYSTEM_UNITS = "system-units"
USER_UNITS = "user-units"

#: Where the staged tree carries the unit file, relative to `install.to`.
CARRIED_DIR = "systemd"

#: `systemd/creche-handover.service`'s `UMask=`.
RELEASE_UMASK = 0o027

#: What `git clone` leaves under that umask: root's, and closed to other.
CLONE_DIR_MODE = 0o750
CLONE_FILE_MODE = 0o640

#: What the operator can read and traverse as a file's owner.
OWN_DIR_MODE = 0o755
OWN_FILE_MODE = 0o644

#: What `normalize_modes` makes of a staged unit file and its directory.
NORMALIZED_FILE_MODE = 0o644
NORMALIZED_DIR_MODE = 0o755

#: One line a new unit carries, so "changed" is a fact about bytes.
NEW_SETTING = "Environment=A_NEW_SETTING=1"

#: A line only a file root alone may read carries, and that file's mode.
SECRET_LINE = "Environment=ONLY_ROOT_MAY_READ_THIS=1"
ROOT_ONLY_MODE = 0o600

#: One child the fake host answers, or None to leave it to the next fake.
Fake = Callable[[Command], RunResult | None]

#: Writes whatever a fake clone carries under `systemd/`, given the clone.
UnitWriter = Callable[[Path], None]


@dataclass
class Bench:
    """One wired executor, releasing one component whose unit is installed."""

    tmp_path: Path
    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path
    component: str
    unit: str
    unit_dir: Path

    def tree(self) -> Path:
        return self.components / self.component

    def carried(self, tree: Path | None = None) -> Path:
        return (tree or self.tree()) / CARRIED_DIR / self.unit

    def installed(self) -> Path:
        return self.unit_dir / self.unit

    def kept(self) -> Path:
        return self.unit_dir / f"{self.unit}.prev"

    def ledger(self, name: str = REQUEST_ID) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{name}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def manual(self) -> list[str]:
        return [str(one) for one in cast("list[object]", self.ledger()["manual"])]

    def refreshes(self) -> list[Command]:
        """Every `install` onto the unit that is not the kept copy's."""
        target, kept = str(self.installed()), str(self.kept())

        return [
            one
            for one in self.run.seen
            if one.argv[0] == INSTALL and one.argv[-1] == target and one.argv[-2] != kept
        ]

    def at(self, *argv: str) -> list[int]:
        """Every recorded child whose argv is exactly `argv`."""
        return [index for index, one in enumerate(self.run.seen) if one.argv == argv]


@contextmanager
def _umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _unit_text(bench: Bench, *extra: str) -> str:
    """A unit that starts its own tree, so rule 8 passes it
    (`test_handover_r7k_unit_binding.py`)."""
    lines = [
        "[Unit]",
        "Description=a fixture unit",
        "",
        "[Service]",
        f"ExecStart={bench.tree()}/bin/{bench.component}",
        *extra,
    ]

    return "\n".join(lines) + "\n"


def _readable_by_operator(path: Path, root: Path) -> bool:
    """The operator is other to a file root wrote: every directory below `root`
    must let other through, and the file must let other read."""
    below = [parent for parent in path.parents if root in parent.parents]
    if any(not parent.stat().st_mode & stat.S_IXOTH for parent in below):
        return False

    return bool(path.stat().st_mode & stat.S_IROTH)


def _install_for_real(bench: Bench) -> Fake:
    """`install -m MODE SRC DST`, done: the one shape `install.py` writes.

    The real one unlinks `DST` and creates it again. A missing `SRC` is its
    `cannot stat`, and a `SRC` the operator cannot read, run as the operator, is its
    `Permission denied`. Any other child is the next fake's.
    """

    def run(command: Command) -> RunResult | None:
        argv = command.argv
        if len(argv) != 5 or argv[0] != INSTALL or argv[1] != "-m":
            return None

        source, target = Path(argv[3]), Path(argv[4])
        if not source.is_file():
            return RunResult(1, "", f"install: cannot stat '{source}': No such file or directory\n")

        if command.identity is As.OPERATOR and not _readable_by_operator(source, bench.tmp_path):
            denied = f"install: cannot open '{source}' for reading: Permission denied\n"

            return RunResult(1, "", denied)

        data = source.read_bytes()
        target.unlink(missing_ok=True)
        target.write_bytes(data)
        target.chmod(int(argv[2], 8))

        return RunResult(0, "", "")

    return run


def _chain(*fakes: Fake) -> Fake:
    def run(command: Command) -> RunResult | None:
        for fake in fakes:
            answered = fake(command)
            if answered is not None:
                return answered

        return None

    return run


def _close_the_clone(clone: Path) -> None:
    """The modes `git clone` gives root's clone under `UMask=0027`. A
    symlink is left alone, because `chmod` follows one."""
    for dirpath, _dirnames, filenames in os.walk(clone):
        here = Path(dirpath)
        here.chmod(CLONE_DIR_MODE)
        for name in filenames:
            one = here / name
            if not one.is_symlink():
                one.chmod(CLONE_FILE_MODE)


def _manifest_for(bench: Bench, name: str) -> str:
    text = manifest_text(name).replace("/opt/components", str(bench.components))
    text = text.replace("install:", BUILD_LINE, 1)
    if name == bench.component:
        text = text.replace("unit: null", f"unit: {bench.unit}")

    return text


def _carries(bench: Bench, text: str) -> UnitWriter:
    """A clone whose `systemd/<unit>` is a plain file holding `text`."""

    def write(clone: Path) -> None:
        (clone / CARRIED_DIR).mkdir(parents=True, exist_ok=True)
        (clone / CARRIED_DIR / bench.unit).write_text(text, encoding="utf-8")

    return write


def _serve(bench: Bench, units: UnitWriter | None, *first: Fake) -> None:
    """What the fake clone leaves: every manifest, and what `units` writes,
    all closed to the operator. `first` answers ahead of the host's own fakes."""
    texts = {row.name: _manifest_for(bench, row.name) for row in CATALOG}

    def write_tree(destination: Path, name: str) -> None:
        row = CATALOG_BY_NAME[name]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(texts[name], encoding="utf-8")
        if units is not None and name == bench.component:
            units(destination)

        _close_the_clone(destination)

    bench.run.dynamic = _chain(*first, _install_for_real(bench), GitFake(write_tree))


def _make_bench(tmp_path: Path, component: str, unit: str, units: str) -> Bench:
    """`component` live at 0.1.3, 0.1.4 the latest tag, and `unit` installed
    in `units` — a system unit, or one of the operator's."""
    run = FakeRun(answers=git_host_answers())
    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=grant_transport(),
        readers=fake_readers(latest={component: NEW_VERSION}),
        api=green_api(
            str(CATALOG_BY_NAME[component].repo),
            f"{component}-v{NEW_VERSION}",
            SHA_OF[component],
        ),
    )
    bench = Bench(
        tmp_path=tmp_path,
        spool_root=make_spool_dirs(tmp_path),
        wiring=wiring,
        run=run,
        components=tmp_path / "components",
        component=component,
        unit=unit,
        unit_dir=tmp_path / units,
    )
    stamp_tree(bench.components, component, LIVE_VERSION)
    run.hooks["sync"] = lambda _: stamp_tree(bench.components, f"{component}.new", NEW_VERSION)
    bench.installed().write_text(_unit_text(bench), encoding="utf-8")

    # `~operator/.local/components` and `~operator/.config/systemd/user` are
    # the operator's own, and so is the unit file in the second.
    bench.components.chmod(OWN_DIR_MODE)
    bench.unit_dir.chmod(OWN_DIR_MODE)
    bench.installed().chmod(OWN_FILE_MODE)

    return bench


def _user_bench(tmp_path: Path) -> Bench:
    """`noticeboard`, whose `creche-noticeboard.service` is a user unit of the operator's."""
    return _make_bench(tmp_path, "noticeboard", NOTICEBOARD_UNIT, USER_UNITS)


def _system_bench(tmp_path: Path) -> Bench:
    """`chaperone`, whose `creche-chaperone.service` is root's."""
    return _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT, SYSTEM_UNITS)


def _release(bench: Bench) -> str:
    write_request(bench.spool_root, REQUEST_ID, request_body({bench.component: NEW_VERSION}))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


class _Crash(BaseException):
    """The executor dying between the switch and the record. A death is no
    `Exception`: a step records an `Exception` in the ledger."""


def _crash_at_the_restart(bench: Bench) -> None:
    """The run dies at the switch's own restart: the tree is swapped, the
    unit refreshed, the note in `running/`, and nothing ledgered."""

    def crash(_: Command) -> None:
        raise _Crash

    bench.run.hooks["restart"] = crash
    with pytest.raises(_Crash):
        _release(bench)

    del bench.run.hooks["restart"]


def _repair(bench: Bench) -> list[str]:
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return repair_unfinished(spool, bench.wiring)
    finally:
        spool.close()


# -- 1. a user unit changes, as the operator, from the live tree --------------


def test_a_changed_user_unit_is_installed_from_the_live_tree_as_the_operator(
    tmp_path: Path,
) -> None:
    """The clone is root's `0750`, so the operator can never read a unit file
    inside it. The live tree is readable by everyone, so the refresh runs
    as the unit's owner and lands."""
    bench = _user_bench(tmp_path)
    new = _unit_text(bench, NEW_SETTING)
    _serve(bench, _carries(bench, new))

    result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    assert bench.installed().read_text(encoding="utf-8") == new
    refreshes = bench.refreshes()
    assert [(one.argv[-2], one.identity) for one in refreshes] == [
        (str(bench.carried()), As.OPERATOR)
    ]
    refreshed = bench.run.seen.index(refreshes[0])
    reloads = [i for i in bench.at(SYSTEMCTL, "--user", "daemon-reload") if i > refreshed]
    assert reloads, "no daemon-reload after the refresh"
    assert bench.run.seen[reloads[0]].identity is As.OPERATOR


# -- 2. the staged tree carries the unit, readable by everyone ----------------


def test_the_staged_tree_carries_the_unit_with_the_normalized_mode(tmp_path: Path) -> None:
    """Read at the keep, BEFORE the swap: `<install.to>.new/systemd/<unit>`
    holds the release's unit byte for byte, and under the release unit's
    own umask it is still `0644` in a `0755` directory, because the copy
    runs before `normalize_modes` and not after it."""
    bench = _user_bench(tmp_path)
    new = _unit_text(bench, NEW_SETTING)
    staged = bench.carried(bench.components / f"{bench.component}.new")
    seen: list[tuple[str, int, int]] = []

    def at_the_keep(command: Command) -> RunResult | None:
        if command.argv[0] != INSTALL or command.argv[-1] != str(bench.kept()):
            return None

        if not staged.is_file():
            seen.append(("no staged unit", 0, 0))

            return None

        modes = (stat.S_IMODE(staged.stat().st_mode), stat.S_IMODE(staged.parent.stat().st_mode))
        seen.append((staged.read_text(encoding="utf-8"), *modes))

        return None

    _serve(bench, _carries(bench, new), at_the_keep)

    with _umask(RELEASE_UMASK):
        result = _release(bench)

    assert seen == [(new, NORMALIZED_FILE_MODE, NORMALIZED_DIR_MODE)], seen
    assert "succeeded" in result, bench.ledger()["reason"]


# -- 3. a system unit still works ---------------------------------------------


def test_a_system_unit_is_installed_from_the_live_tree_as_root(tmp_path: Path) -> None:
    """`chaperone`'s unit is root's, and root can read the clone. The refresh
    still reads the same place a user unit does, and runs as root."""
    bench = _system_bench(tmp_path)
    new = _unit_text(bench, NEW_SETTING)
    _serve(bench, _carries(bench, new))

    result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    assert bench.installed().read_text(encoding="utf-8") == new
    refreshes = bench.refreshes()
    assert [(one.argv[-2], one.identity) for one in refreshes] == [(str(bench.carried()), As.ROOT)]
    refreshed = bench.run.seen.index(refreshes[0])
    reloads = [i for i in bench.at(SYSTEMCTL, "daemon-reload") if i > refreshed]
    assert reloads, "no daemon-reload after the refresh"
    assert bench.run.seen[reloads[0]].identity is As.ROOT


# -- 4. the crash repair ------------------------------------------------------


def test_the_crash_repair_of_a_user_unit_reads_nothing_from_the_clone(tmp_path: Path) -> None:
    """A run that dies after the refresh leaves the new unit over a tree
    nobody recorded. The next run puts back the kept copy as the operator, then
    reloads and restarts, and no child of the repair names the clone."""
    bench = _user_bench(tmp_path)
    old = bench.installed().read_bytes()
    new = _unit_text(bench, NEW_SETTING)
    _serve(bench, _carries(bench, new))
    _crash_at_the_restart(bench)
    assert bench.installed().read_text(encoding="utf-8") == new
    crashed = len(bench.run.seen)

    lines = _repair(bench)

    assert "repaired (restored)" in lines[0], lines
    assert bench.installed().read_bytes() == old
    repaired = bench.run.seen[crashed:]
    put_back = (INSTALL, "-m", "0644", str(bench.kept()), str(bench.installed()))
    assert [one.identity for one in repaired if one.argv == put_back] == [As.OPERATOR]
    work = str(bench.wiring.host.work_root)
    assert not [one.argv for one in repaired if any(work in word for word in one.argv)]


# -- 5. no unit in the source, none staged ------------------------------------


def test_a_source_with_no_unit_file_stages_none(tmp_path: Path) -> None:
    """The manifest names a unit and the source carries no file for it.
    The staged tree carries none, the installed unit stays, and `manual`
    says why, as it did before."""
    bench = _user_bench(tmp_path)
    old = bench.installed().read_bytes()
    _serve(bench, None)

    result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    assert not (bench.tree() / CARRIED_DIR).exists()
    assert bench.installed().read_bytes() == old
    assert f"no unit file in the source tree: {bench.unit}" in bench.manual()
    assert not [one for one in bench.run.seen if one.argv[0] == INSTALL]


# -- 6. nothing outside the clone becomes a unit -------------------------------


def _links_the_file(bench: Bench, outside: Path) -> UnitWriter:
    def write(clone: Path) -> None:
        (clone / CARRIED_DIR).mkdir(parents=True, exist_ok=True)
        (clone / CARRIED_DIR / bench.unit).symlink_to(outside)

    return write


def _links_the_directory(bench: Bench, outside: Path) -> UnitWriter:
    del bench

    def write(clone: Path) -> None:
        (clone / CARRIED_DIR).symlink_to(outside.parent, target_is_directory=True)

    return write


@pytest.mark.parametrize(
    "link", [_links_the_file, _links_the_directory], ids=["file link", "directory link"]
)
def test_a_unit_file_outside_the_clone_is_never_staged(
    tmp_path: Path, link: Callable[[Bench, Path], UnitWriter]
) -> None:
    """Root copies the unit into a tree everyone can read, so a link out of
    the clone would publish whatever it names. It is not a unit file the
    release carries: nothing is staged and nothing is installed."""
    bench = _system_bench(tmp_path)
    old = bench.installed().read_bytes()
    outside = tmp_path / "root-only" / bench.unit
    outside.parent.mkdir()
    outside.write_text(_unit_text(bench, SECRET_LINE), encoding="utf-8")
    outside.chmod(ROOT_ONLY_MODE)
    _serve(bench, link(bench, outside))

    result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    assert bench.installed().read_bytes() == old
    assert not bench.carried().exists()
    assert f"no unit file in the source tree: {bench.unit}" in bench.manual()


# -- 7. a copy that cannot be made --------------------------------------------


def test_a_unit_that_cannot_be_staged_fails_the_stage(tmp_path: Path) -> None:
    """The build left a FILE where the unit's directory goes. The copy is
    part of step 8, so it fails there, as a step and not as a crash: the
    old tree and the old unit stay in service."""
    bench = _user_bench(tmp_path)
    old = bench.installed().read_bytes()

    def build(_: Command) -> None:
        staged = stamp_tree(bench.components, f"{bench.component}.new", NEW_VERSION)
        (staged / CARRIED_DIR).write_text("not a directory\n", encoding="utf-8")

    bench.run.hooks["sync"] = build
    _serve(bench, _carries(bench, _unit_text(bench, NEW_SETTING)))

    _release(bench)

    entry = bench.ledger()
    assert entry["status"] == "failed"
    assert entry["reason"] == f"stage: {bench.component}: cannot stage {bench.unit}"
    assert installed_version(bench.tree()) == LIVE_VERSION
    assert bench.installed().read_bytes() == old
