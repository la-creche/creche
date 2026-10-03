"""A release refreshes every unit its component ships (`install.py` rule 8).

`attendance` ships four unit files: the manifest's `unit:` and three door
units that run out of the same tree. A release that refreshed only the
first left the doors on their old files. One door restarted with the new
tree under its old unit, which lacked a new `EnvironmentFile=` line, and
crash-looped while the ledger said `succeeded`.

A SIBLING of component C is an installed `*.service` in the same directory
as C's own unit, not a symlink, whose installed file and whose carried file
both start C's tree. The fake host is `test_release_urs_unit_restore.py`'s:
`install` really copies, so "byte for byte" is a claim about bytes a copy
moved. Twelve things are pinned, numbered as the cases below.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from agent_release.catalog import CATALOG, CATALOG_BY_NAME
from agent_release.executor.drain import Counter, handle, repair_unfinished
from agent_release.executor.host import As, Command
from agent_release.executor.host import Result as RunResult
from agent_release.executor.install import INSTALL, Installer, Paths
from agent_release.executor.live_state import installed_version
from agent_release.executor.spool import DONE_DIR, RUNNING_DIR, Spool
from agent_release.executor.steps import Wiring
from release_executor_fixtures import (
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
from release_fixtures import manifest_text

LIVE_VERSION = "0.1.3"
NEW_VERSION = "0.1.4"

#: The fixture manifests carry no `build`, and the executor stages nothing
#: without one.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen", "--no-editable"]\ninstall:'

PEP_UNIT = "agent-pep.service"
NOTICEBOARD_UNIT = "creche-noticeboard.service"

#: Sibling names, shaped like `attendance`'s three doors.
DOOR = "agent-door-a.service"
TRIGGER = "agent-trigger-b.service"
TEMPLATE = "creche-trigger@.service"
IDLE = "agent-door-idle.service"
OTHER = "agent-other.service"

#: `fake_host`'s two unit directories: `/etc/systemd/system`, and
#: `~operator/.config/systemd/user`.
SYSTEM_UNITS = "system-units"
USER_UNITS = "user-units"

#: What the new door unit adds, and what its old file lacks.
NEW_SETTING = "EnvironmentFile=/a/new/door.env"

#: A program in no tree any fixture installs.
ELSEWHERE = "/usr/local/bin/elsewhere"

#: `install.py`'s cap on siblings per component.
MAX_SIBLINGS = 16

#: One child the fake host answers, or None to leave it to the next fake.
Fake = Callable[[Command], RunResult | None]


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
    #: Extra unit files the fake clone carries in `systemd/`, by name.
    carried: dict[str, str] = field(default_factory=dict[str, str])

    def tree(self) -> Path:
        return self.components / self.component

    def installed(self, name: str | None = None) -> Path:
        return self.unit_dir / (name or self.unit)

    def kept(self, name: str | None = None) -> Path:
        return self.unit_dir / f"{name or self.unit}.prev"

    def ledger(self, name: str = REQUEST_ID) -> dict[str, object]:
        return _json(self.spool_root / DONE_DIR / f"{name}.json")

    def log(self, name: str = REQUEST_ID) -> list[str]:
        return [str(one) for one in cast("list[object]", self.ledger(name)["log_tail"])]

    def note_path(self) -> Path:
        return self.spool_root / RUNNING_DIR / f"{REQUEST_ID}-{self.component}.switch"

    def note(self) -> dict[str, object]:
        """The switch note as the switch left it in `running/`."""
        return _json(self.note_path())

    def at(self, *tail: str) -> list[int]:
        """Every recorded child whose argv ends in `tail`."""
        return [index for index, one in enumerate(self.run.seen) if one.argv[-len(tail) :] == tail]

    def put_back_at(self, name: str) -> list[int]:
        """Where, among the recorded children, a kept copy went back."""
        return self.at(str(self.kept(name)), str(self.installed(name)))

    def touching(self, path: Path) -> list[Command]:
        """Every `install` that named `path`."""
        return [one for one in self.run.seen if one.argv[0] == INSTALL and str(path) in one.argv]


def _json(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _starts(program: str, *extra: str) -> str:
    """A unit file that starts `program`, plus any `extra` lines."""
    lines = ["[Unit]", "Description=a fixture unit", "", "[Service]", f"ExecStart={program}"]

    return "\n".join([*lines, *extra]) + "\n"


def _own(bench: Bench, *extra: str) -> str:
    """A unit that starts the bench's own tree."""
    return _starts(f"{bench.tree()}/bin/{bench.component}", *extra)


def _install_for_real(command: Command) -> RunResult | None:
    """`install -m MODE SRC DST`, done: the one shape `install.py` writes."""
    argv = command.argv
    if len(argv) != 5 or argv[0] != INSTALL or argv[1] != "-m":
        return None

    source, target = Path(argv[3]), Path(argv[4])
    if not source.is_file():
        return RunResult(1, "", f"install: cannot stat '{source}': No such file or directory\n")

    data = source.read_bytes()
    target.unlink(missing_ok=True)
    target.write_bytes(data)
    target.chmod(int(argv[2], 8))

    return RunResult(0, "", "")


def _chain(*fakes: Fake) -> Fake:
    def run(command: Command) -> RunResult | None:
        for fake in fakes:
            answered = fake(command)
            if answered is not None:
                return answered

        return None

    return run


def _manifest_for(bench: Bench, name: str) -> str:
    text = manifest_text(name).replace("/opt/components", str(bench.components))
    text = text.replace("install:", BUILD_LINE, 1)
    if name == bench.component:
        text = text.replace("unit: null", f"unit: {bench.unit}")

    return text


def _serve(bench: Bench, main: str, *first: Fake) -> None:
    """What the fake clone leaves: every manifest, the main unit file and
    every file in `bench.carried`. `first` answers ahead of the rest."""
    texts = {row.name: _manifest_for(bench, row.name) for row in CATALOG}

    def write_tree(destination: Path, name: str) -> None:
        row = CATALOG_BY_NAME[name]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(texts[name], encoding="utf-8")
        units = destination / "systemd"
        units.mkdir(parents=True, exist_ok=True)
        (units / bench.unit).write_text(main, encoding="utf-8")
        for unit, text in bench.carried.items():
            (units / unit).write_text(text, encoding="utf-8")

    bench.run.dynamic = _chain(*first, _install_for_real, GitFake(write_tree))


def _make_bench(tmp_path: Path, component: str, unit: str, units: str) -> Bench:
    """`component` live at 0.1.3, 0.1.4 the latest tag, and `unit` installed
    in `units`."""
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
    bench.installed().write_text(_own(bench), encoding="utf-8")

    return bench


@pytest.fixture
def system(tmp_path: Path) -> Bench:
    """`pep`, with its unit under `/etc/systemd/system`."""
    return _make_bench(tmp_path, "pep", PEP_UNIT, SYSTEM_UNITS)


@pytest.fixture
def user(tmp_path: Path) -> Bench:
    """`noticeboard`, with its unit in the operator's own unit directory."""
    return _make_bench(tmp_path, "noticeboard", NOTICEBOARD_UNIT, USER_UNITS)


def _sibling(bench: Bench, name: str, *, carried: str | None = None) -> bytes:
    """`name` installed as the old door, and carried as the new one. The
    installed bytes, for a test that wants them back."""
    bench.installed(name).write_text(_own(bench), encoding="utf-8")
    bench.carried[name] = carried if carried is not None else _own(bench, NEW_SETTING)

    return bench.installed(name).read_bytes()


def _fail_verify_once(bench: Bench) -> None:
    """The new version's hook fails and the restored one passes."""
    word = f"{bench.component}-verify"
    calls = {"n": 0}
    bench.run.fails[word] = 1

    def count(_: Command) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop(word, None)

    bench.run.hooks[word] = count


def _is_active(answers: Callable[[str], str | None]) -> Fake:
    """`systemctl is-active <unit>`, answered per unit name. None leaves
    the unit to the host's own `active`."""

    def run(command: Command) -> RunResult | None:
        if "is-active" not in command.argv:
            return None

        said = answers(command.argv[-1])
        if said is None:
            return None

        return RunResult(0 if said == "active" else 3, f"{said}\n", "")

    return run


def _release(bench: Bench) -> str:
    write_request(bench.spool_root, REQUEST_ID, request_body({bench.component: NEW_VERSION}))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


class _Crash(Exception):
    """The executor dying between the switch and the record."""


def _crash_at_the_restart(bench: Bench) -> None:
    """The run dies at the switch's own restart: the tree is swapped, every
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


def _main_restarts(bench: Bench) -> list[int]:
    return bench.at("restart", bench.unit)


def _try_restarts(bench: Bench, name: str) -> list[int]:
    return bench.at("try-restart", name)


def _restarts(bench: Bench, name: str) -> list[int]:
    return bench.at("restart", name)


# -- 1. a sibling is refreshed with the main unit ----------------------------


@pytest.mark.parametrize(
    ("component", "unit", "units", "who"),
    [
        ("pep", PEP_UNIT, SYSTEM_UNITS, As.ROOT),
        ("noticeboard", NOTICEBOARD_UNIT, USER_UNITS, As.OPERATOR),
    ],
    ids=["system unit, as root", "user unit, as the operator"],
)
def test_a_sibling_is_refreshed_with_the_main_unit(
    tmp_path: Path, component: str, unit: str, units: str, who: As
) -> None:
    """The live case: the door's new file carries a line its old one
    lacks, and it lands from the LIVE tree, as the directory's owner."""
    bench = _make_bench(tmp_path, component, unit, units)
    _sibling(bench, DOOR)
    _serve(bench, _own(bench, NEW_SETTING))

    assert "succeeded" in _release(bench), bench.ledger()["reason"]

    assert bench.installed(DOOR).read_text(encoding="utf-8") == bench.carried[DOOR]
    refresh = (
        INSTALL,
        "-m",
        "0644",
        str(bench.tree() / "systemd" / DOOR),
        str(bench.installed(DOOR)),
    )
    refreshed = [one for one in bench.run.seen if one.argv == refresh]
    assert [one.identity for one in refreshed] == [who]
    assert [one.identity for one in bench.touching(bench.kept(DOOR))] == [who]
    assert f"sibling kept: {DOOR} at {bench.kept(DOOR)}" in bench.log()


# -- 2 to 5. what is never touched -------------------------------------------


def test_a_unit_that_starts_another_tree_is_not_touched(user: Bench) -> None:
    """Another component's unit in the same directory. The release even
    carries a file of that name starting this tree: the INSTALLED file
    decides, and it starts another one."""
    user.installed(OTHER).write_text(_starts(ELSEWHERE), encoding="utf-8")
    old = user.installed(OTHER).read_bytes()
    user.carried[OTHER] = _own(user)
    _serve(user, _own(user, NEW_SETTING))

    assert "succeeded" in _release(user)

    assert user.installed(OTHER).read_bytes() == old
    assert not user.touching(user.installed(OTHER))
    assert not user.kept(OTHER).exists()


def test_a_unit_nothing_installed_is_not_installed(user: Bench) -> None:
    """Installing a unit stays the installer's job (rule 4)."""
    user.carried[DOOR] = _own(user, NEW_SETTING)
    _serve(user, _own(user, NEW_SETTING))

    assert "succeeded" in _release(user)

    assert not user.installed(DOOR).exists()
    assert not user.touching(user.installed(DOOR))


def test_a_symlinked_sibling_is_not_touched(user: Bench) -> None:
    """Root never writes through a link in the operator's directory."""
    real = user.tmp_path / "linked-door.service"
    real.write_text(_own(user), encoding="utf-8")
    old = real.read_bytes()
    user.installed(DOOR).symlink_to(real)
    user.carried[DOOR] = _own(user, NEW_SETTING)
    _serve(user, _own(user, NEW_SETTING))

    assert "succeeded" in _release(user)

    assert user.installed(DOOR).is_symlink()
    assert real.read_bytes() == old
    assert not user.touching(user.installed(DOOR))


def test_a_carried_sibling_that_starts_outside_the_tree_is_not_installed(user: Bench) -> None:
    """A release cannot use a sibling to point a running door anywhere
    but its own tree."""
    old = _sibling(user, DOOR, carried=_starts(ELSEWHERE))
    _serve(user, _own(user, NEW_SETTING))

    assert "succeeded" in _release(user)

    assert user.installed(DOOR).read_bytes() == old
    assert not user.touching(user.installed(DOOR))
    assert not (user.tree() / "systemd" / DOOR).exists()


# -- 6. the note names every kept sibling first ------------------------------


def test_the_note_names_every_kept_sibling_before_the_swap(
    user: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash repair reads the note, so it names each copy BEFORE the
    tree moves, and each copy is already there, byte for byte."""
    old = {name: _sibling(user, name) for name in (DOOR, TRIGGER)}
    _serve(user, _own(user, NEW_SETTING))
    seen: list[tuple[dict[str, object], dict[str, bytes]]] = []
    swap_in = Installer.swap_in

    def spy(installer: Installer, paths: Paths) -> None:
        copies = {name: user.kept(name).read_bytes() for name in old}
        seen.append((user.note(), copies))
        swap_in(installer, paths)

    monkeypatch.setattr(Installer, "swap_in", spy)

    assert "succeeded" in _release(user)

    assert len(seen) == 1
    note, copies = seen[0]
    assert note["siblings_kept"] == [str(user.kept(DOOR)), str(user.kept(TRIGGER))]
    assert copies == old


# -- 7. a failed verify puts every sibling back ------------------------------


def test_a_failed_verify_puts_every_sibling_back_before_the_restart(user: Bench) -> None:
    old = {name: _sibling(user, name) for name in (DOOR, TRIGGER)}
    _serve(user, _own(user, NEW_SETTING))
    _fail_verify_once(user)

    _release(user)

    entry = user.ledger()
    assert entry["status"] == "restored", entry["reason"]
    assert installed_version(user.tree()) == LIVE_VERSION
    for name, data in old.items():
        assert user.installed(name).read_bytes() == data
        put_back = user.put_back_at(name)
        assert len(put_back) == 1, f"{name} never went back"
        assert [index for index in _main_restarts(user) if index > put_back[0]]
        assert f"sibling back: {name} from {user.kept(name)}" in user.log()


# -- 8. the crash repair -----------------------------------------------------


def test_the_crash_repair_puts_every_sibling_back_from_the_note(user: Bench) -> None:
    old = _sibling(user, DOOR)
    _serve(user, _own(user, NEW_SETTING))
    _crash_at_the_restart(user)
    assert user.installed(DOOR).read_bytes() != old
    crashed = len(user.run.seen)

    lines = _repair(user)

    assert "repaired (restored)" in lines[0]
    assert user.installed(DOOR).read_bytes() == old
    put_back = [index for index in user.put_back_at(DOOR) if index >= crashed]
    assert len(put_back) == 1, "the repair never put the sibling back"
    assert [index for index in _main_restarts(user) if index > put_back[0]]


def test_a_note_without_the_key_repairs_as_before(user: Bench) -> None:
    """An older executor's note names no sibling. The tree and the main
    unit go back, and no sibling is touched."""
    old_main = user.installed().read_bytes()
    _sibling(user, DOOR)
    _serve(user, _own(user, NEW_SETTING))
    _crash_at_the_restart(user)
    note = user.note()
    del note["siblings_kept"]
    user.note_path().unlink()
    user.note_path().write_text(json.dumps(note), encoding="utf-8")
    refreshed = user.installed(DOOR).read_bytes()
    crashed = len(user.run.seen)

    lines = _repair(user)

    assert "repaired (restored)" in lines[0]
    assert installed_version(user.tree()) == LIVE_VERSION
    assert user.installed().read_bytes() == old_main
    assert user.installed(DOOR).read_bytes() == refreshed
    assert not [index for index in user.put_back_at(DOOR) if index >= crashed]


@pytest.mark.parametrize(
    ("written", "read"),
    [("a string", ()), ([1, "/a/b.service.prev", None], ("/a/b.service.prev",))],
    ids=["not a list", "only strings kept"],
)
def test_a_note_reads_siblings_tolerantly(
    tmp_path: Path, written: object, read: tuple[str, ...]
) -> None:
    root = make_spool_dirs(tmp_path)
    body = {"component": "noticeboard", "to": "/a", "prev": "/a.prev", "siblings_kept": written}
    (root / RUNNING_DIR / f"{REQUEST_ID}-noticeboard.switch").write_text(
        json.dumps(body), encoding="utf-8"
    )
    spool = Spool(str(root), this_uid())
    try:
        found = spool.unfinished()
    finally:
        spool.close()

    assert [one.note.siblings_kept for one in found] == [read]


# -- 9. which siblings restart -----------------------------------------------


def test_only_an_active_non_template_sibling_is_try_restarted(user: Bench) -> None:
    """After the main restart. A template has no instance of its own, and
    a door that was stopped stays stopped."""
    for name in (DOOR, TEMPLATE, IDLE):
        _sibling(user, name)

    idle = _is_active(lambda unit: "inactive" if unit == IDLE else None)
    _serve(user, _own(user, NEW_SETTING), idle)

    assert "succeeded" in _release(user), user.ledger()["reason"]

    restarted = _try_restarts(user, DOOR)
    assert len(restarted) == 1
    assert restarted[0] > max(_main_restarts(user))
    assert user.run.seen[restarted[0]].identity is As.OPERATOR
    assert not _try_restarts(user, TEMPLATE)
    assert not _try_restarts(user, IDLE)
    assert user.installed(TEMPLATE).read_text(encoding="utf-8") == user.carried[TEMPLATE]


# -- 10. a sibling that does not come back -----------------------------------


def test_a_sibling_that_does_not_come_back_fails_the_switch(user: Bench) -> None:
    """The door was up, its try-restart left it down: the switch fails and
    the tree, the main unit and every sibling go back."""
    old_main = user.installed().read_bytes()
    old = _sibling(user, DOOR)

    def after_its_restart(unit: str) -> str | None:
        if unit != DOOR or not _try_restarts(user, DOOR):
            return None

        return "failed"

    _serve(user, _own(user, NEW_SETTING), _is_active(after_its_restart))

    _release(user)

    entry = user.ledger()
    assert entry["status"] == "restored", entry["reason"]
    assert DOOR in str(entry["reason"])
    assert installed_version(user.tree()) == LIVE_VERSION
    assert user.installed().read_bytes() == old_main
    assert user.installed(DOOR).read_bytes() == old
    # `try-restart` does nothing for a unit that failed. The door ran before
    # this release, so the restore starts it again, after its file is back.
    revived = _restarts(user, DOOR)
    assert revived, "the door stayed down after the restore"
    assert revived[-1] > user.put_back_at(DOOR)[0]


# -- 11. a bound on the work -------------------------------------------------


def test_more_than_sixteen_siblings_is_refused_at_stage(user: Bench) -> None:
    count = MAX_SIBLINGS + 1
    for index in range(count):
        _sibling(user, f"agent-door-{index:02d}.service")

    _serve(user, _own(user, NEW_SETTING))

    _release(user)

    entry = user.ledger()
    assert entry["status"] == "failed"
    assert str(entry["reason"]).startswith("stage: ")
    assert str(count) in str(entry["reason"])
    assert installed_version(user.tree()) == LIVE_VERSION
    assert not [one for one in user.run.seen if one.argv[0] == INSTALL]


# -- 12. identical siblings --------------------------------------------------


def test_identical_siblings_keep_nothing_and_still_restart(user: Bench) -> None:
    """The unit files are the release's already, so nothing is kept or
    written. The tree under them changed, so each running one restarts: a
    door that is not `PartOf=` the main unit would go on running the old
    code."""
    same = _own(user)
    for name in (DOOR, TRIGGER):
        _sibling(user, name, carried=same)

    notes: list[dict[str, object]] = []
    _serve(user, same)
    user.run.hooks["daemon-reload"] = lambda _: notes.append(user.note())

    assert "succeeded" in _release(user)

    assert notes[0]["siblings_kept"] == []
    assert not [one for one in user.run.seen if one.argv[0] == INSTALL]
    assert not [path for path in user.unit_dir.iterdir() if path.name.endswith(".prev")]
    for name in (DOOR, TRIGGER):
        restarted = _try_restarts(user, name)
        assert len(restarted) == 1, name
        assert restarted[0] > max(_main_restarts(user))


# -- 13. the operator's directory is never listed ----------------------------


def test_the_operators_unit_directory_is_never_listed(
    user: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root asks for the names the release carries, one by one. Listing the
    operator's directory would make every release walk whatever the operator
    put there."""
    _sibling(user, DOOR)
    for index in range(40):
        (user.unit_dir / f"junk-{index:02d}.service").write_text("[Unit]\n", encoding="utf-8")

    real_scandir = os.scandir

    def scandir(path: object) -> object:
        assert Path(str(path)) != user.unit_dir, "the operator's unit directory was listed"

        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", scandir)
    _serve(user, _own(user, NEW_SETTING))

    assert "succeeded" in _release(user), user.ledger()["reason"]
    assert user.installed(DOOR).read_text(encoding="utf-8") == user.carried[DOOR]


# -- 14. a crash repair revives what it put back -----------------------------


def test_the_crash_repair_revives_a_sibling_that_is_not_stopped(user: Bench) -> None:
    """The repair has no memory of what ran. A put-back sibling that is not
    `inactive` is restarted under its old file, and one a person stopped
    stays stopped.

    The name of this test must not hold the word `restart`: pytest names the
    temporary directory after it, and `_crash_at_the_restart` crashes at the
    first child whose argv holds that word.
    """
    for name in (DOOR, IDLE):
        _sibling(user, name)

    idle = _is_active(lambda unit: "inactive" if unit == IDLE else None)
    _serve(user, _own(user, NEW_SETTING), idle)
    _crash_at_the_restart(user)
    crashed = len(user.run.seen)

    _repair(user)

    assert [index for index in _restarts(user, DOOR) if index >= crashed]
    assert not [index for index in _restarts(user, IDLE) if index >= crashed]
