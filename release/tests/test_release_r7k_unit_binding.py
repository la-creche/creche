"""A release that cannot change what runs is refused.

A unit that execs `/opt/agent-control/.venv/bin/agent-pep` while
`pep/component.yaml` installs to `/opt/components/pep` and names that unit
lets a `pep` release build a tree, swap it, restart the unit, pass its
verify and write `done` — while the PEP still runs the code of `/opt`. What
the tap binds to must be what runs (invariant 17, `stage7-releases.md`
§2.5). The repository's own files are proved at the end of this module, and
the refusal is proved with fixture units here.

Three things this pins.

1. **The check runs at step 8, before anything is swapped.** A refusal
   leaves the host exactly as it was, and the ledger carries the code
   `unit`.
2. **It reads the unit FILE the host really has**, under
   `/etc/systemd/system` for a system unit and `~operator/.config/systemd/user`
   for a user unit of the operator's. Not `systemctl show`: a user unit's
   properties come off the operator's own bus, which root reaches only through
   `runuser` and an `XDG_RUNTIME_DIR` that may not exist.
3. **The unit the release will INSTALL is the one that is read.** Step 9
   refreshes the unit file in place from the fetched tree, so a release
   whose own tree carries the corrected unit does change what runs, and
   refusing it would refuse the fix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from agent_release.catalog import CATALOG, CATALOG_BY_NAME, Kind
from agent_release.executor.drain import Counter, handle
from agent_release.executor.install import Installer, exec_start_programs
from agent_release.executor.live_state import installed_version
from agent_release.executor.spool import DONE_DIR, Spool
from agent_release.executor.steps import Wiring
from agent_release.manifest import parse_manifest
from agent_release.site import SITE_FILE_ENV, operator_home
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

REPO_ROOT = Path(__file__).resolve().parents[2]

LIVE_VERSION = "2.0.3"
NEW_VERSION = "2.1.0"

#: The fixture manifests carry no `build`, and the executor stages nothing
#: without one.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen", "--no-editable"]\ninstall:'

PEP_UNIT = "agent-pep.service"
NOTICEBOARD_UNIT = "creche-noticeboard.service"

#: The refusal code, as the ledger records it.
UNIT_CODE = "unit"

#: systemd's specifier for the home of the user a user unit runs as.
HOME_SPECIFIER = "%h"


def _unit_text(exec_start: str) -> str:
    return f"[Unit]\nDescription=a fixture unit\n\n[Service]\nExecStart={exec_start}\n"


@dataclass
class Bench:
    """One wired executor, releasing one component that names one unit."""

    tmp_path: Path
    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path
    component: str
    unit: str | None

    def tree(self) -> Path:
        return self.components / self.component

    def ledger(self) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{REQUEST_ID}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def reason(self) -> str:
        return str(self.ledger().get("reason", ""))


def _manifest_for(bench: Bench, name: str) -> str:
    """A fixture manifest whose paths sit under the bench, carrying the
    unit under test on the component this bench releases."""
    text = manifest_text(name).replace("/opt/components", str(bench.components))
    text = text.replace("install:", BUILD_LINE, 1)
    if name == bench.component and bench.unit is not None:
        text = text.replace("unit: null", f"unit: {bench.unit}")

    return text


def _serve(bench: Bench, staged_unit: str | None = None) -> None:
    """What the fake `git clone` leaves in the fetched tree: the manifest,
    and the unit file step 9 would refresh from."""
    texts = {row.name: _manifest_for(bench, row.name) for row in CATALOG}

    def write_tree(destination: Path, name: str) -> None:
        text = texts.get(name)
        if text is None:
            return

        row = CATALOG_BY_NAME[name]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(text, encoding="utf-8")
        if staged_unit is not None and bench.unit is not None:
            units = destination / "systemd"
            units.mkdir(parents=True, exist_ok=True)
            (units / bench.unit).write_text(staged_unit, encoding="utf-8")

    bench.run.dynamic = GitFake(write_tree)


def _install_unit(bench: Bench, text: str, *, user: bool = False) -> Path:
    directory = bench.tmp_path / ("user-units" if user else "system-units")
    assert bench.unit is not None
    path = directory / bench.unit
    path.write_text(text, encoding="utf-8")

    return path


def _make_bench(tmp_path: Path, component: str, unit: str | None) -> Bench:
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    wiring = Wiring(
        host=host,
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
        spool_root=spool_root,
        wiring=wiring,
        run=run,
        components=tmp_path / "components",
        component=component,
        unit=unit,
    )
    stamp_tree(bench.components, component, LIVE_VERSION)
    run.hooks["sync"] = lambda _: stamp_tree(bench.components, f"{component}.new", NEW_VERSION)

    return bench


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    """`pep` live at 2.0.3, 2.1.0 the latest tag, one system unit."""
    return _make_bench(tmp_path, "pep", PEP_UNIT)


def _release(bench: Bench) -> str:
    write_request(bench.spool_root, REQUEST_ID, request_body({bench.component: NEW_VERSION}))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


# -- the live case ------------------------------------------------------------


def test_a_unit_that_starts_a_program_outside_the_tree_is_refused(bench: Bench) -> None:
    """The unit execs `/opt/agent-control/.venv`, and the release installs
    `/opt/components/pep`."""
    _install_unit(bench, _unit_text("/opt/agent-control/.venv/bin/agent-pep"))
    _serve(bench)

    result = _release(bench)

    assert "refused" in result
    entry = bench.ledger()
    assert entry["refused_check"] == UNIT_CODE
    assert PEP_UNIT in bench.reason()
    assert "/opt/agent-control/.venv/bin/agent-pep" in bench.reason()


def test_the_refusal_swaps_nothing_and_restarts_nothing(bench: Bench) -> None:
    """Step 8 refuses, so the old tree is still in service and the unit was
    never touched."""
    _install_unit(bench, _unit_text("/opt/agent-control/.venv/bin/agent-pep"))
    _serve(bench)

    _release(bench)

    assert installed_version(bench.tree()) == LIVE_VERSION
    assert not (bench.components / "pep.new").exists()
    assert not (bench.components / "pep.prev").exists()
    assert not bench.run.ran("restart")


def test_a_unit_that_starts_the_tree_releases(bench: Bench) -> None:
    """The other direction, and the one that must not become collateral."""
    _install_unit(bench, _unit_text(f"{bench.tree()}/bin/agent-pep"))
    _serve(bench)

    result = _release(bench)

    assert "succeeded" in result, bench.reason()
    assert installed_version(bench.tree()) == NEW_VERSION


def test_the_tree_itself_counts_as_inside_itself(bench: Bench) -> None:
    """`install.to` is not outside `install.to`. A containment test written
    with `parents` alone reads the root itself as an escape."""
    _install_unit(bench, _unit_text(str(bench.tree())))
    _serve(bench)

    assert "succeeded" in _release(bench), bench.reason()


# -- where the unit file is read from ----------------------------------------


def test_an_operator_user_unit_is_read_from_its_own_directory(tmp_path: Path) -> None:
    """`attendance`, `managerd` and the noticeboard are USER units, installed under
    `~operator/.config/systemd/user` by `bin/rework-cutover.sh`. Root reads the
    FILE: `systemctl --user show` needs the operator's own bus."""
    bench = _make_bench(tmp_path, "noticeboard", NOTICEBOARD_UNIT)
    _install_unit(bench, _unit_text("/opt/agent-control/.venv/bin/noticeboard"), user=True)
    _serve(bench)

    result = _release(bench)

    assert "refused" in result
    assert bench.ledger()["refused_check"] == UNIT_CODE
    assert NOTICEBOARD_UNIT in bench.reason()


def test_a_user_unit_that_starts_its_own_tree_releases(tmp_path: Path) -> None:
    """`noticeboard` is the safe first release: its user unit starts its own tree."""
    bench = _make_bench(tmp_path, "noticeboard", NOTICEBOARD_UNIT)
    _install_unit(bench, _unit_text(f"{bench.tree()}/bin/noticeboard"), user=True)
    _serve(bench)

    assert "succeeded" in _release(bench), bench.reason()


@pytest.fixture
def home_is_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A site whose operator's home is this test's directory, so `%h` in a
    user unit is the directory the bench's trees sit under."""
    path = tmp_path / "home-site.env"
    lines = ["AGENT_GITHUB_OWNER=example-owner", "AGENT_OPERATOR_USER=operator"]
    lines.append(f"AGENT_OPERATOR_HOME={tmp_path}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv(SITE_FILE_ENV, str(path))


@pytest.mark.usefixtures("home_is_the_bench")
def test_a_user_unit_reaches_its_tree_through_the_home_specifier(tmp_path: Path) -> None:
    """A unit file names no account. `%h/x` is `<the operator's home>/x`,
    which is what systemd's user manager makes of it."""
    bench = _make_bench(tmp_path, "noticeboard", NOTICEBOARD_UNIT)
    _install_unit(bench, _unit_text("%h/components/noticeboard/bin/noticeboard"), user=True)
    _serve(bench)

    assert "succeeded" in _release(bench), bench.reason()


@pytest.mark.usefixtures("home_is_the_bench")
def test_the_home_specifier_in_a_system_unit_is_not_the_operators_home(bench: Bench) -> None:
    """In a system unit `%h` is root's home. It is left as written, which
    is a path outside every tree, so the unit is refused."""
    _install_unit(bench, _unit_text("%h/components/pep/bin/agent-pep"))
    _serve(bench)

    assert "refused" in _release(bench)
    assert bench.ledger()["refused_check"] == UNIT_CODE


def test_a_component_with_no_unit_is_not_checked(tmp_path: Path) -> None:
    """`releasectl` and `playpen` carry `unit: null`. Nothing is
    restarted, so there is no unit to disagree with the tree."""
    bench = _make_bench(tmp_path, "pep", None)
    _serve(bench)

    assert "succeeded" in _release(bench), bench.reason()


def test_a_unit_nothing_installed_is_left_to_the_manual_list(bench: Bench) -> None:
    """Step 9 already lists an uninstalled unit under `manual` and never
    restarts it. Refusing here would refuse the first release of a component
    whose unit the installer has not put in yet."""
    _serve(bench)

    result = _release(bench)

    assert "succeeded" in result, bench.reason()
    manual = bench.ledger()["manual"]
    assert isinstance(manual, list)
    assert any(PEP_UNIT in str(one) for one in manual), manual  # pyright: ignore[reportUnknownVariableType]


def test_the_unit_the_release_installs_is_the_one_that_is_read(bench: Bench) -> None:
    """Step 9 refreshes the unit file in place from the fetched tree, so the
    release that CORRECTS the unit is exactly the release that must pass."""
    _install_unit(bench, _unit_text("/opt/agent-control/.venv/bin/agent-pep"))
    _serve(bench, staged_unit=_unit_text(f"{bench.tree()}/bin/agent-pep"))

    assert "succeeded" in _release(bench), bench.reason()


def test_a_staged_unit_that_still_points_away_is_refused(bench: Bench) -> None:
    """The same door, shut. A tree that carries a unit file is not thereby
    trusted: the file is read and held to the same rule."""
    _install_unit(bench, _unit_text(f"{bench.tree()}/bin/agent-pep"))
    _serve(bench, staged_unit=_unit_text("/opt/agent-control/.venv/bin/agent-pep"))

    result = _release(bench)

    assert "refused" in result
    assert bench.ledger()["refused_check"] == UNIT_CODE


def test_a_symlinked_installed_unit_keeps_its_own_text(bench: Bench) -> None:
    """`refresh_unit` never writes through a symlink, so a release does not
    replace a linked unit and the linked file is what stays in force."""
    installed = _install_unit(bench, "")
    installed.unlink()
    real = bench.tmp_path / "linked-agent-pep.service"
    real.write_text(_unit_text("/opt/agent-control/.venv/bin/agent-pep"), encoding="utf-8")
    installed.symlink_to(real)
    _serve(bench, staged_unit=_unit_text(f"{bench.tree()}/bin/agent-pep"))

    result = _release(bench)

    assert "refused" in result
    assert bench.ledger()["refused_check"] == UNIT_CODE


def test_a_unit_with_no_exec_start_is_refused(bench: Bench) -> None:
    """Invariant 19's fail-closed end: a unit root cannot read a program out
    of is a unit root cannot say starts the tree."""
    _install_unit(bench, "[Unit]\nDescription=no service section\n")
    _serve(bench)

    result = _release(bench)

    assert "refused" in result
    assert bench.ledger()["refused_check"] == UNIT_CODE


# -- reading `ExecStart=` ------------------------------------------------------


def test_a_continued_exec_start_line_is_one_line() -> None:
    """`agent-managerd.service` writes its arguments over four lines."""
    text = "[Service]\nExecStart=/a/bin/managerd serve \\\n  --registry /srv \\\n  --json\n"

    assert exec_start_programs(text) == ("/a/bin/managerd",)


@pytest.mark.parametrize("prefix", ("", "-", "@", "+", "!", "!!", "-@"))
def test_systemds_prefix_characters_are_not_part_of_the_path(prefix: str) -> None:
    """`ExecStart=-/a/bin/x` starts `/a/bin/x`. A reader that kept the dash
    would find every prefixed unit outside every tree."""
    assert exec_start_programs(f"[Service]\nExecStart={prefix}/a/bin/x\n") == ("/a/bin/x",)


def test_an_empty_assignment_resets_the_list() -> None:
    """systemd's own rule for a list-valued setting. A drop-in that clears
    `ExecStart=` and sets another one must not read as two programs."""
    text = "[Service]\nExecStart=/a/bin/first\nExecStart=\nExecStart=/a/bin/second\n"

    assert exec_start_programs(text) == ("/a/bin/second",)


def test_every_exec_start_is_reported() -> None:
    """A oneshot may list several. The check refuses when ANY of them starts
    from outside the tree."""
    text = "[Service]\nExecStart=/a/bin/one\nExecStart=/a/bin/two\n"

    assert exec_start_programs(text) == ("/a/bin/one", "/a/bin/two")


def test_a_quoted_program_is_unquoted() -> None:
    text = '[Service]\nExecStart="/a/bin/with a space" --json\n'

    assert exec_start_programs(text) == ("/a/bin/with a space",)


def test_exec_start_pre_is_not_exec_start() -> None:
    """`creche-noticeboard.service` carries both. `ExecStartPre` is a check that
    runs and exits, and it is not what the unit runs."""
    text = "[Service]\nExecStartPre=/a/bin/x --check\nExecStart=/a/bin/x\n"

    assert exec_start_programs(text) == ("/a/bin/x",)


# -- this repository's own units ----------------------------------------------

#: Every component whose SHIPPED unit does not start a program inside its
#: own `install.to`: none, since `agent-pep.service` execs
#: `/opt/components/pep/bin/agent-pep` and `bin/rework-release-visit.sh`
#: bootstraps that first tree the way it bootstraps `releasectl`'s.
#:
#: Keep it EMPTY. A new row here is a component whose release would write
#: `succeeded` over a host it never changed, and rule 8 is what turns that
#: into a refusal instead.
UNITS_THAT_DO_NOT_START_THEIR_TREE: frozenset[str] = frozenset()


def _shipped_unit(unit: str) -> Path | None:
    for candidate in (REPO_ROOT / "systemd" / unit, *REPO_ROOT.glob(f"*/systemd/{unit}")):
        if candidate.is_file():
            return candidate

    return None


def test_the_repositorys_own_units_start_the_trees_their_manifests_install() -> None:
    """The live case, held in the repository rather than found on the host.

    Only `kind: venv`: a compose project's unit starts docker, and what
    binds it to its tree is `WorkingDirectory` rather than `ExecStart`.
    """
    found: set[str] = set()
    for path in sorted(REPO_ROOT.glob("*/component.yaml")):
        manifest = parse_manifest(path.read_text(encoding="utf-8"), str(path))
        if manifest.unit is None or manifest.kind is not Kind.VENV:
            continue

        unit = _shipped_unit(manifest.unit)
        assert unit is not None, f"{manifest.name}: no {manifest.unit} in this repository"
        programs = exec_start_programs(unit.read_text(encoding="utf-8"))
        assert programs, f"{manifest.name}: {manifest.unit} has no ExecStart"
        # A user unit reaches its tree through `%h`, the operator's home.
        programs = tuple(one.replace(HOME_SPECIFIER, operator_home(), 1) for one in programs)
        if any(not one.startswith(manifest.install.to + "/") for one in programs):
            found.add(manifest.name)

    assert found == UNITS_THAT_DO_NOT_START_THEIR_TREE


def test_the_real_pep_unit_and_the_real_manifest_pass_rule_eight(tmp_path: Path) -> None:
    """`pep`, through the executor's OWN check rather than a re-reading.

    The test above compares two strings. This one runs `check_unit_binds`
    with this repository's `systemd/agent-pep.service` and
    `pep/component.yaml` exactly as they ship, in both readings step 9 can
    take: the file already installed under `/etc/systemd/system`, and the
    file the release would refresh in its place. `pep` is the component
    this rule is written for, and the one worth driving end to end.
    """
    manifest = parse_manifest(
        (REPO_ROOT / "pep" / "component.yaml").read_text(encoding="utf-8"), "pep/component.yaml"
    )
    assert manifest.install.to == "/opt/components/pep"

    installer = Installer(fake_host(tmp_path, FakeRun(answers=git_host_answers())))
    shipped = (REPO_ROOT / "systemd" / PEP_UNIT).read_text(encoding="utf-8")
    (tmp_path / "system-units" / PEP_UNIT).write_text(shipped, encoding="utf-8")

    # No `systemd/` under the source, so the INSTALLED file is what is read.
    installer.check_unit_binds(manifest, tmp_path / "no-systemd-directory")
    # And with it, so the file step 9 would copy over is what is read.
    installer.check_unit_binds(manifest, REPO_ROOT)


#: `attendance`'s three door units, which run out of its tree
#: (`install.py` rule 8).
ATTENDANCE_DOORS = frozenset(
    {"creche-door-owui.service", "creche-trigger-webhooks.service", "creche-trigger@.service"}
)


def _starts_its_tree(text: str, to: str, user_unit: bool) -> bool:
    programs = exec_start_programs(text)
    if user_unit:
        programs = tuple(one.replace(HOME_SPECIFIER, operator_home(), 1) for one in programs)

    return bool(programs) and all(one.startswith(to + "/") for one in programs)


def test_every_unit_that_starts_a_tree_travels_with_its_component(tmp_path: Path) -> None:
    """Rule 8, proven from the repository through the executor's own
    `stage_unit`. Every shipped unit is installed on a fake host, and a
    stage out of this repository must carry exactly the units that start
    the component's tree: its `unit:` and its siblings. So `attendance`'s
    three doors are siblings of `attendance`."""
    shipped = sorted((REPO_ROOT / "systemd").glob("*.service"))
    staged_for: dict[str, set[str]] = {}
    for path in sorted(REPO_ROOT.glob("*/component.yaml")):
        manifest = parse_manifest(path.read_text(encoding="utf-8"), str(path))
        if manifest.unit is None or manifest.kind is not Kind.VENV:
            continue

        # A user unit's tree is under the operator's home, a system unit's
        # is not.
        user_unit = manifest.install.to.startswith(operator_home() + "/")
        (tmp_path / manifest.name).mkdir()
        host = fake_host(tmp_path / manifest.name, FakeRun(answers=git_host_answers()))
        units = host.user_unit_dir if user_unit else host.system_unit_dir
        for one in shipped:
            (units / one.name).write_bytes(one.read_bytes())

        new = tmp_path / manifest.name / "new"
        new.mkdir()
        Installer(host).stage_unit(manifest, REPO_ROOT, new)

        staged = {one.name for one in (new / "systemd").iterdir()}
        texts = {one.name: one.read_text(encoding="utf-8") for one in shipped}
        starting = {
            name
            for name, text in texts.items()
            if _starts_its_tree(text, manifest.install.to, user_unit)
        }
        assert staged == starting, manifest.name
        staged_for[manifest.name] = staged

    assert staged_for["attendance"] == {"creche-attendance.service", *ATTENDANCE_DOORS}
