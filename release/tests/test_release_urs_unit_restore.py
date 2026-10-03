"""A failed verify puts the unit back too (`stage7-releases.md` §2.4 rows 9 and 10).

Step 9 swaps the tree in, then `refresh_unit` copies the unit file the
release carries over the installed one. A step 10 that put back the tree
alone would restart it under the unit then in force: the NEW one. `chaperone`
0.1.4's unit grants `CAP_SETUID` and `CAP_SETGID`, bounding and ambient,
for the launcher its tree carries. If its verify failed, 0.1.3 — which
spawns every MCP server itself — would restart under that unit, and every
server would inherit both.

The fake host is the one the switch and restore tests use, with one
difference: `install` really copies, so "byte for byte" is a claim about
bytes a copy moved. Six things are pinned.

1. The switch keeps the unit it replaces, beside itself as `<unit>.prev`,
   and its note names the copy before the unit moves. A keep that fails
   moves nothing.
2. A failed verify puts the copy back byte for byte, then reloads, then
   restarts.
3. A release that replaced no unit puts none back.
4. The crash repair puts it back, from the note.
5. A copy that cannot go back is a `manual` line with the commands, and
   the previous tree does not restart under this release's unit.
6. A unit of the operator's is kept and put back as the operator.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from agent_release.catalog import CATALOG, CATALOG_BY_NAME
from agent_release.executor.drain import Counter, handle, repair_unfinished
from agent_release.executor.host import As, Command
from agent_release.executor.host import Result as RunResult
from agent_release.executor.install import INSTALL, Installer
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

CHAPERONE_UNIT = "creche-chaperone.service"
NOTICEBOARD_UNIT = "creche-noticeboard.service"

#: `fake_host`'s two unit directories: `/etc/systemd/system`, and
#: `~operator/.config/systemd/user`.
SYSTEM_UNITS = "system-units"
USER_UNITS = "user-units"

#: What `chaperone` 0.1.4's unit adds for its launcher, and what 0.1.3 must never
#: run under.
CAPABILITIES = (
    "CapabilityBoundingSet=CAP_SETUID CAP_SETGID",
    "AmbientCapabilities=CAP_SETUID CAP_SETGID",
)

#: The crash repair's `reason` when the unit could not go back.
UNIT_NOT_BACK = "restore: the previous unit file could not be put back"

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
    unit: str | None
    unit_dir: Path

    def tree(self) -> Path:
        return self.components / self.component

    def installed(self) -> Path:
        return self.unit_dir / str(self.unit)

    def kept(self) -> Path:
        return self.unit_dir / f"{self.unit}.prev"

    def ledger(self, name: str = REQUEST_ID) -> dict[str, object]:
        return _json(self.spool_root / DONE_DIR / f"{name}.json")

    def manual(self, name: str = REQUEST_ID) -> list[str]:
        return [str(one) for one in cast("list[object]", self.ledger(name)["manual"])]

    def note(self) -> dict[str, object]:
        """The switch note as the switch left it in `running/`."""
        return _json(self.spool_root / RUNNING_DIR / f"{REQUEST_ID}-{self.component}.switch")

    def put_back_at(self) -> list[int]:
        """Where, among the recorded children, the kept copy went back."""
        wanted = (INSTALL, "-m", "0644", str(self.kept()), str(self.installed()))

        return [index for index, one in enumerate(self.run.seen) if one.argv == wanted]

    def at(self, *tail: str) -> list[int]:
        """Every recorded child whose argv ends in `tail`."""
        return [index for index, one in enumerate(self.run.seen) if one.argv[-len(tail) :] == tail]


def _json(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _unit_text(bench: Bench, *extra: str) -> str:
    """A unit that starts its own tree, so rule 8 passes it
    (`test_release_r7k_unit_binding.py`)."""
    lines = [
        "[Unit]",
        "Description=a fixture unit",
        "",
        "[Service]",
        f"ExecStart={bench.tree()}/bin/{bench.component}",
        *extra,
    ]

    return "\n".join(lines) + "\n"


def _install_for_real(command: Command) -> RunResult | None:
    """`install -m MODE SRC DST`, done: the one shape `install.py` writes.

    The real one unlinks `DST` and creates it again, and a missing `SRC` is
    its `cannot stat`, exit 1. Any other child is the next fake's.
    """
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
    if name == bench.component and bench.unit is not None:
        text = text.replace("unit: null", f"unit: {bench.unit}")

    return text


def _serve(bench: Bench, staged: str, *first: Fake) -> None:
    """What the fake clone leaves: every manifest, and the unit file step 9
    refreshes from. `first` answers ahead of the host's own fakes."""
    texts = {row.name: _manifest_for(bench, row.name) for row in CATALOG}

    def write_tree(destination: Path, name: str) -> None:
        row = CATALOG_BY_NAME[name]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(texts[name], encoding="utf-8")
        if bench.unit is None:
            return

        units = destination / "systemd"
        units.mkdir(parents=True, exist_ok=True)
        (units / bench.unit).write_text(staged, encoding="utf-8")

    bench.run.dynamic = _chain(*first, _install_for_real, GitFake(write_tree))


def _make_bench(
    tmp_path: Path, component: str, unit: str | None, units: str = SYSTEM_UNITS
) -> Bench:
    """`component` live at 0.1.3, 0.1.4 the latest tag, and `unit` installed
    in `units` as the switch finds it — a system unit, or one of the operator's."""
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
    if unit is not None:
        bench.installed().write_text(_unit_text(bench), encoding="utf-8")

    return bench


def _fail_verify_once(bench: Bench, then: Callable[[], object] | None = None) -> None:
    """The new version's hook fails and the restored one passes. `then`
    runs inside the failing call, after the switch and before the restore."""
    word = f"{bench.component}-verify"
    calls = {"n": 0}
    bench.run.fails[word] = 1

    def count(_: Command) -> None:
        calls["n"] += 1
        if calls["n"] == 1 and then is not None:
            then()

        if calls["n"] > 1:
            bench.run.fails.pop(word, None)

    bench.run.hooks[word] = count


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


def _by_hand(kept: Path, who: str, systemctl: str) -> str:
    """The `manual` line, spelled out here so the test pins the words."""
    unit = kept.name.removesuffix(".prev")

    return (
        f"restore: {unit} is still this release's unit file; as {who}: "
        f"install -m 0644 {kept} {kept.with_name(unit)} && {systemctl} daemon-reload"
        f" && {systemctl} restart {unit}"
    )


# -- 1. the switch keeps what it replaces, and says so first ------------------


def test_the_switch_note_names_the_kept_unit_before_it_moves(tmp_path: Path) -> None:
    """A crash repair reads the note, so the note says where the old unit
    went BEFORE the refresh overwrites it — and the copy is already there,
    byte for byte."""
    bench = _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT)
    old = bench.installed().read_bytes()
    seen: list[tuple[dict[str, object], bytes | None]] = []

    def at_the_refresh(command: Command) -> RunResult | None:
        argv = command.argv
        refreshing = argv[-1] == str(bench.installed()) and argv[-2] != str(bench.kept())
        if argv[0] == INSTALL and refreshing:
            kept = bench.kept()
            seen.append((bench.note(), kept.read_bytes() if kept.is_file() else None))

        return None

    _serve(bench, _unit_text(bench, *CAPABILITIES), at_the_refresh)

    assert "succeeded" in _release(bench)
    assert len(seen) == 1
    note, copy = seen[0]
    assert note.get("unit_kept") == str(bench.kept())
    assert copy == old


def test_a_unit_that_cannot_be_kept_moves_nothing(tmp_path: Path) -> None:
    """The keep runs before the note and before the swap, so a keep that
    fails is a switch that moved nothing: the old tree and the old unit
    stay in service, and there is nothing for step 10 to put back."""
    bench = _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT)
    old = bench.installed().read_bytes()
    keep = (INSTALL, "-m", "0644", str(bench.installed()), str(bench.kept()))

    def refuse_the_keep(command: Command) -> RunResult | None:
        if command.argv != keep:
            return None

        return RunResult(1, "", "install: cannot create regular file: Read-only file system\n")

    _serve(bench, _unit_text(bench, *CAPABILITIES), refuse_the_keep)

    _release(bench)

    entry = bench.ledger()
    assert entry["status"] == "failed"
    assert str(entry["reason"]).startswith(f"switch: cannot keep {CHAPERONE_UNIT}")
    assert installed_version(bench.tree()) == LIVE_VERSION
    assert bench.installed().read_bytes() == old
    assert not bench.run.ran("daemon-reload")


# -- 2. a failed verify puts it back, before the restart ----------------------


def test_a_failed_verify_puts_the_previous_unit_back_byte_for_byte(tmp_path: Path) -> None:
    """The `chaperone` case: 0.1.4's unit grants two capabilities, its verify
    fails, and 0.1.3 comes back under 0.1.3's own unit file."""
    bench = _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT)
    old = bench.installed().read_bytes()
    _serve(bench, _unit_text(bench, *CAPABILITIES))
    _fail_verify_once(bench)

    _release(bench)

    entry = bench.ledger()
    assert entry["status"] == "restored", entry["reason"]
    assert installed_version(bench.tree()) == LIVE_VERSION
    assert bench.installed().read_bytes() == old
    log = cast("list[object]", entry["log_tail"])
    assert f"unit back: {CHAPERONE_UNIT} from {bench.kept()}" in log


def test_the_unit_goes_back_and_reloads_before_the_restart(tmp_path: Path) -> None:
    """The kept copy goes back, then `daemon-reload`, then the restart:
    the previous tree never starts under this release's unit."""
    bench = _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT)
    _serve(bench, _unit_text(bench, *CAPABILITIES))
    _fail_verify_once(bench)

    _release(bench)

    put_back = bench.put_back_at()
    assert len(put_back) == 1, "the kept unit never went back"
    reload = min(index for index in bench.at("daemon-reload") if index > put_back[0])
    restart = min(index for index in bench.at("restart", CHAPERONE_UNIT) if index > put_back[0])
    assert reload < restart
    # The failing verify came first: this is the restore, not the switch.
    verified = [
        index for index, one in enumerate(bench.run.seen) if "chaperone-verify" in one.argv[0]
    ]
    assert verified[0] < put_back[0]


# -- 3. nothing replaced, nothing put back ------------------------------------


@pytest.mark.parametrize("unit", [CHAPERONE_UNIT, None], ids=["identical", "none named"])
def test_a_release_that_replaced_no_unit_puts_none_back(tmp_path: Path, unit: str | None) -> None:
    """The manifest names no unit, or the release carries the file already
    installed. Either way the switch replaces nothing, its note says so,
    and the restore touches no unit file. A `<unit>.prev` an EARLIER
    release left is not this one's: nothing reads a copy the note does not
    name."""
    bench = _make_bench(tmp_path, "chaperone", unit)
    if unit is not None:
        bench.kept().write_bytes(b"[Unit]\nDescription=an earlier release's copy\n")

    before = {path.name: path.read_bytes() for path in bench.unit_dir.iterdir()}
    notes: list[dict[str, object]] = []
    _serve(bench, _unit_text(bench))
    _fail_verify_once(bench, then=lambda: notes.append(bench.note()))

    _release(bench)

    assert bench.ledger()["status"] == "restored"
    assert "unit_kept" in notes[0]
    assert notes[0]["unit_kept"] is None
    assert {path.name: path.read_bytes() for path in bench.unit_dir.iterdir()} == before
    assert not [one for one in bench.run.seen if one.argv[0] == INSTALL]


# -- 4. the crash repair puts it back from the note ---------------------------


def test_the_crash_repair_puts_the_kept_unit_back(tmp_path: Path) -> None:
    """A run that dies after the refresh leaves this release's unit over a
    tree nobody recorded. The next run puts back the tree, then the unit,
    then reloads and restarts."""
    bench = _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT)
    old = bench.installed().read_bytes()
    _serve(bench, _unit_text(bench, *CAPABILITIES))
    _crash_at_the_restart(bench)
    assert bench.installed().read_bytes() != old
    crashed = len(bench.run.seen)

    lines = _repair(bench)

    assert "repaired (restored)" in lines[0]
    assert installed_version(bench.tree()) == LIVE_VERSION
    assert bench.installed().read_bytes() == old
    put_back = [index for index in bench.put_back_at() if index >= crashed]
    assert len(put_back) == 1, "the repair never put the kept unit back"
    reload = min(index for index in bench.at("daemon-reload") if index > put_back[0])
    restart = min(index for index in bench.at("restart", CHAPERONE_UNIT) if index > put_back[0])
    assert reload < restart


# -- 5. a copy that cannot go back --------------------------------------------


def test_a_copy_that_cannot_go_back_is_a_manual_line(tmp_path: Path) -> None:
    """The copy is gone when the restore needs it. The ledger names the
    three commands a person types, and the restore stops BEFORE the
    restart, so the previous tree does not come up under this release's
    unit."""
    bench = _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT)
    new = _unit_text(bench, *CAPABILITIES)
    _serve(bench, new)
    _fail_verify_once(bench, then=lambda: bench.kept().unlink(missing_ok=True))

    _release(bench)

    entry = bench.ledger()
    assert entry["status"] == "failed"
    assert str(entry["reason"]).startswith("restore: ")
    assert _by_hand(bench.kept(), "root", "systemctl") in bench.manual()
    assert bench.installed().read_text(encoding="utf-8") == new
    attempt = bench.put_back_at()
    assert len(attempt) == 1
    assert not [index for index in bench.at("restart", CHAPERONE_UNIT) if index > attempt[0]]


def test_a_repair_whose_copy_is_gone_says_so_under_manual(tmp_path: Path) -> None:
    """The same end, reached by the crash repair: the entry says the unit
    did not go back, names the commands, and restarts nothing."""
    bench = _make_bench(tmp_path, "chaperone", CHAPERONE_UNIT)
    new = _unit_text(bench, *CAPABILITIES)
    _serve(bench, new)
    _crash_at_the_restart(bench)
    bench.kept().unlink(missing_ok=True)
    crashed = len(bench.run.seen)

    _repair(bench)

    note_name = f"{REQUEST_ID}-{bench.component}"
    entry = bench.ledger(note_name)
    assert entry["status"] == "failed"
    assert entry["reason"] == UNIT_NOT_BACK
    assert _by_hand(bench.kept(), "root", "systemctl") in bench.manual(note_name)
    assert bench.installed().read_text(encoding="utf-8") == new
    assert not [index for index in bench.at("restart", CHAPERONE_UNIT) if index >= crashed]


# -- a unit of the operator's -------------------------------------------------


def test_a_user_unit_is_kept_and_put_back_as_the_operator(tmp_path: Path) -> None:
    """`noticeboard`'s unit lives under `~operator/.config/systemd/user`. The copy is
    kept beside it and put back by the identity that owns that directory,
    the one the refresh runs as: root never copies a file of the operator's by
    path into one the operator can read."""
    bench = _make_bench(tmp_path, "noticeboard", NOTICEBOARD_UNIT, USER_UNITS)
    old = bench.installed().read_bytes()
    _serve(bench, _unit_text(bench, "Environment=A_NEW_SETTING=1"))
    _fail_verify_once(bench)

    _release(bench)

    assert bench.ledger()["status"] == "restored"
    assert bench.installed().read_bytes() == old
    kept = str(bench.kept())
    moved = [one for one in bench.run.seen if one.argv[0] == INSTALL and kept in one.argv]
    # The keep, then the put-back.
    assert [one.identity for one in moved] == [As.OPERATOR, As.OPERATOR]


def test_a_user_units_manual_line_is_the_operators(tmp_path: Path) -> None:
    """What a person types for one of the operator's units: as the operator, with
    `systemctl --user`."""
    installer = Installer(fake_host(tmp_path, FakeRun()))
    kept = tmp_path / USER_UNITS / f"{NOTICEBOARD_UNIT}.prev"

    assert installer.unit_by_hand(kept) == _by_hand(kept, "operator", "systemctl --user")
