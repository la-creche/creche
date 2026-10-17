"""One `kind: binary` component, through all ten steps.

The other `kind: binary` modules drive one function each. This one files a
request and lets the executor run: resolve, provenance, approval, stage,
switch, verify and the ledger. Root runs no build of the component. Step 2
copies the two files of the store, check P6 verifies the copy at step 3,
and step 8 unpacks it.

The verifier is a stand-in that the test starts as a child (`StandIn` of
`handover_bin_fixtures.py`). Each verifier child first passes the real
check of `host.verifier_argv`: the site file names two programs that root
owns on each test machine.

The ends held here:

1. A good release lands, and the ledger says `succeeded`.
2. Check P6 refuses before the tap: a refused signature, an absent
   verifier, a verifier file that another account can write, an absent
   artifact, an archive with a link and an archive of another component.
3. A store file that changes after the tap changes nothing: the release
   lands with the first bytes.
4. A copy that the test removes after the tap is refused with `drift`, and
   an unpack fault fails the stage.
5. A tree that is not self-contained is refused with `editable`, and a unit
   that starts a program outside the tree is refused with `unit`.
6. Each refusal and each failure leaves the live tree in service and no
   staged tree.

The catalog row of the component says `binary` for each test here. The
executor refuses a manifest whose kind is not the kind of its row.
"""

from __future__ import annotations

import json
import os
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest
from handover.artifacts import ArtifactRefusal
from handover.catalog import CATALOG, CATALOG_BY_NAME
from handover.executor import install
from handover.executor.approval import Decision, Summary
from handover.executor.artifact import COPY_DIR
from handover.executor.drain import Counter, handle
from handover.executor.host import UNIT_END, VERIFIER_NO_NETWORK, As, Command, Result
from handover.executor.install import RELOCATABLE_ARGV
from handover.executor.live_state import MANIFEST_STAMP_YAML, installed_version
from handover.executor.selfcontained import NAMES_WORK_TREE, NOT_STAGED
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover.executor.unpack import UnpackFault, UnpackRefused
from handover.manifest import Markup, parse_manifest
from handover_bin_fixtures import (
    BINARY_NAME,
    CARGO,
    PROGRAM_BYTES,
    PROGRAMS,
    ROOT_OWNED_STARTER,
    ROOT_OWNED_VERIFIER,
    UNIT,
    StandIn,
    archive_bytes,
    binary_manifest_text,
    signed_bundle,
    site_text,
    stock_store,
    unit_text,
    use_binary_catalog,
    write_program,
)
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

from handover import site

LIVE_VERSION = "0.4.0"
NEW_VERSION = "0.5.0"
TAG = f"{BINARY_NAME}-v{NEW_VERSION}"

#: What the live tree's program holds, so a test can say which tree is live.
OLD_PROGRAM = b"the previous program\n"

#: A program in no tree that a release installs.
ELSEWHERE = "/opt/creche/.venv/bin/noticeboard"

#: A second commit, which no signature of a good archive names.
OTHER_SHA = "0" * 40


@pytest.fixture(autouse=True)
def _binary_row(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """The catalog row and the manifest change kind together
    (`handover/AGENTS.md`, "Binary builds", rule 8)."""
    use_binary_catalog(monkeypatch)


@pytest.fixture
def home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The home of the operator, which holds the store. The site file of
    the test names it, and names the two programs of the verifier."""
    made = tmp_path_factory.mktemp("home")
    _use_site(tmp_path_factory, monkeypatch, made, ROOT_OWNED_VERIFIER)

    return made


def _use_site(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    verifier: str,
) -> None:
    path = tmp_path_factory.mktemp("site") / "site.env"
    path.write_text(site_text(home, verifier), encoding="utf-8")
    monkeypatch.setenv(site.SITE_FILE_ENV, str(path))


@dataclass
class Bench:
    """One wired executor, about to release the binary component."""

    tmp_path: Path
    spool_root: Path
    run: FakeRun
    stand_in: StandIn
    wiring: Wiring
    components: Path
    #: Whether the phone was asked. A refusal of step 3 comes before it.
    tapped: list[str] = field(default_factory=list[str])

    def tree(self) -> Path:
        return self.components / BINARY_NAME

    def staged(self) -> Path:
        return self.components / f"{BINARY_NAME}.new"

    def program(self, name: str = BINARY_NAME) -> str:
        return str(self.tree() / "bin" / name)

    def copy(self) -> Path:
        """Root's copy of the archive, in the work tree of the request."""
        return self.tmp_path / "work" / REQUEST_ID / COPY_DIR / BINARY_NAME / _archive_name()

    def ledger(self) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{REQUEST_ID}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def reason(self) -> str:
        return str(self.ledger().get("reason", ""))

    def step(self, name: str) -> dict[str, object]:
        steps = cast("list[dict[str, object]]", self.ledger()["steps"])

        return next(one for one in steps if one["name"] == name)

    def install_unit(self, text: str) -> None:
        (self.tmp_path / "system-units" / UNIT).write_text(text, encoding="utf-8")

    def release(self, at_approval: Callable[[], None] | None = None) -> str:
        """Run the release. `at_approval` runs at the tap, between step 2
        and step 8."""
        grant = self.wiring.transport

        def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
            self.tapped.append(summary.review)
            if at_approval is not None:
                at_approval()

            return grant(action_id, gate, summary, wait_s)

        wiring = replace(self.wiring, transport=transport)
        write_request(self.spool_root, REQUEST_ID, request_body({BINARY_NAME: NEW_VERSION}))
        spool = Spool(str(self.spool_root), this_uid())
        try:
            return handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
        finally:
            spool.close()


def _archive_name() -> str:
    return f"{BINARY_NAME}-x86_64-unknown-linux-musl.tar.gz"


def _serve(bench: Bench, carried_unit: str | None) -> None:
    """What the fake `git clone` leaves in the fetched tree: every
    manifest, with the binary component's saying `kind: binary`, and the
    unit file that the release carries."""

    def write_tree(destination: Path, name: str) -> None:
        # Every clone holds the whole repository, whichever component it
        # was fetched for.
        del name
        for row in CATALOG:
            text = manifest_text(row.name).replace("/opt/components", str(bench.components))
            if row.name == BINARY_NAME:
                text = binary_manifest_text(bench.components, UNIT)

            sub = destination if row.path == "." else destination / row.path
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "component.yaml").write_text(text, encoding="utf-8")

        if carried_unit is not None:
            (destination / "systemd").mkdir(parents=True, exist_ok=True)
            (destination / "systemd" / UNIT).write_text(carried_unit, encoding="utf-8")

    bench.run.dynamic = GitFake(write_tree)


def _make_bench(
    tmp_path: Path,
    home: Path,
    archive: bytes | None = None,
    bundle: bytes | None = None,
    stocked: bool = True,
) -> Bench:
    """The component live at 0.4.0 with 0.5.0 the latest tag. The store
    holds `archive` and `bundle`: by default a good archive and its
    signature. With `stocked` false the store holds no file."""
    run = FakeRun(answers=git_host_answers())
    stand_in = StandIn(run)
    wiring = Wiring(
        host=replace(fake_host(tmp_path, run), run=stand_in),
        transport=grant_transport(),
        readers=fake_readers(latest={BINARY_NAME: NEW_VERSION}),
        api=green_api(str(CATALOG_BY_NAME[BINARY_NAME].repo), TAG, SHA_OF[BINARY_NAME]),
    )
    bench = Bench(
        tmp_path=tmp_path,
        spool_root=make_spool_dirs(tmp_path),
        run=run,
        stand_in=stand_in,
        wiring=wiring,
        components=tmp_path / "components",
    )
    stamp_tree(bench.components, BINARY_NAME, LIVE_VERSION)
    write_program(bench.tree() / "bin" / BINARY_NAME, body=OLD_PROGRAM)
    if stocked:
        raw = archive if archive is not None else archive_bytes()
        stock_store(home, TAG, raw, bundle if bundle is not None else signed_bundle(raw))

    _serve(bench, None)

    return bench


@pytest.fixture
def bench(tmp_path: Path, home: Path) -> Bench:
    """A release whose unit is installed and starts the component's tree."""
    made = _make_bench(tmp_path, home)
    made.install_unit(unit_text(made.program()))

    return made


def _untouched(bench: Bench) -> None:
    """The live tree is in service, nothing restarted, and no staged tree
    is left."""
    assert installed_version(bench.tree()) == LIVE_VERSION
    assert (bench.tree() / "bin" / BINARY_NAME).read_bytes() == OLD_PROGRAM
    assert not bench.staged().exists()
    assert not bench.run.ran("restart")


def _refused_p6(bench: Bench, reason: ArtifactRefusal) -> None:
    """Refused by check P6 with `reason`, before the tap."""
    assert "refused" in bench.release()

    ledger = bench.ledger()
    assert ledger["refused_check"] == "P6"
    assert ledger["reason"] == str(reason)
    assert bench.step("provenance")["status"] == "refused"
    assert bench.tapped == []
    _untouched(bench)


# ---- a release that lands ----------------------------------------------------


def test_a_binary_component_releases(bench: Bench) -> None:
    assert "succeeded" in bench.release(), bench.reason()

    assert bench.ledger()["status"] == "succeeded"
    assert installed_version(bench.tree()) == NEW_VERSION
    assert (bench.tree() / "bin" / BINARY_NAME).read_bytes() == PROGRAM_BYTES
    assert not bench.staged().exists()


def test_the_released_tree_holds_the_programs_of_the_archive(bench: Bench) -> None:
    bench.release()

    assert sorted(one.name for one in (bench.tree() / "bin").iterdir()) == sorted(PROGRAMS)


def test_step_3_names_the_six_checks(bench: Bench) -> None:
    bench.release()

    assert str(bench.step("provenance")["detail"]).startswith("P1-P6 pass")


def test_the_previous_tree_is_kept(bench: Bench) -> None:
    """Contract 06 §5: one previous artifact stays, for a restore."""
    bench.release()
    previous = bench.components / f"{BINARY_NAME}.prev"

    assert installed_version(previous) == LIVE_VERSION
    assert (previous / "bin" / BINARY_NAME).read_bytes() == OLD_PROGRAM


def test_no_command_of_the_binary_manifest_reaches_the_runner(bench: Bench) -> None:
    """CI runs the `build` argv. Root starts no child with it."""
    bench.release()
    manifest = parse_manifest(
        binary_manifest_text(bench.components, UNIT), "noticeboard/component.yaml", Markup.YAML
    )

    assert manifest.build
    assert all(one.argv not in manifest.build for one in bench.run.seen)
    assert not bench.run.ran(CARGO)


def test_each_verifier_child_runs_in_its_unit(bench: Bench) -> None:
    """Two children, each through the real check of the runner: the first
    loads the trust root, and the second checks the signature with no
    network."""
    bench.release()
    refresh, check = bench.stand_in.words

    assert refresh[0] == ROOT_OWNED_STARTER
    assert refresh[refresh.index(UNIT_END) + 1 :] == (ROOT_OWNED_VERIFIER, "initialize")
    assert check[check.index(UNIT_END) + 1 : check.index(UNIT_END) + 3] == (
        ROOT_OWNED_VERIFIER,
        "verify-blob",
    )
    assert check[-1] == str(bench.copy())
    assert VERIFIER_NO_NETWORK[1] not in refresh
    assert VERIFIER_NO_NETWORK[1] in check


def test_the_copy_is_open_to_the_verifier_under_the_umask_0027(bench: Bench) -> None:
    """The verifier reads the copy as a dynamic user. The release unit runs
    with the umask 0027, and the release makes the work root itself. Each
    directory from the work root down to the copy is open when a verifier
    child starts."""
    work_root = bench.tmp_path / "work"
    work_root.rmdir()
    seen: dict[str, int] = {}

    def look(command: Command) -> Result:
        if command.identity is As.VERIFIER:
            copy = bench.copy()
            seen["file"] = os.stat(copy).st_mode & 0o777
            for directory in copy.parents:
                seen[str(directory.relative_to(work_root))] = os.stat(directory).st_mode & 0o777
                if directory == work_root:
                    break

        return bench.stand_in(command)

    bench.wiring = replace(bench.wiring, host=replace(bench.wiring.host, run=look))
    before = os.umask(0o027)
    try:
        assert "succeeded" in bench.release(), bench.reason()
    finally:
        os.umask(before)

    assert seen == {
        "file": 0o644,
        f"{REQUEST_ID}/{COPY_DIR}/{BINARY_NAME}": 0o755,
        f"{REQUEST_ID}/{COPY_DIR}": 0o755,
        REQUEST_ID: 0o755,
        ".": 0o755,
    }


def test_the_release_unpacks_then_restarts_then_runs_the_hook(bench: Bench) -> None:
    """The order of step 3, step 8 and step 9 for a tree of compiled
    programs. The check of the signature comes before the unit restarts."""
    bench.release()
    lines = bench.run.argv_lines()
    check = next(index for index, line in enumerate(lines) if "verify-blob" in line)
    restart = next(index for index, line in enumerate(lines) if f"restart {UNIT}" in line)
    hook = next(index for index, line in enumerate(lines) if f"{BINARY_NAME}-verify " in line)

    assert check < restart < hook


def test_the_release_makes_no_python_environment(bench: Bench) -> None:
    bench.release()

    assert all(one.argv[:3] != RELOCATABLE_ARGV for one in bench.run.seen)


def test_the_live_tree_carries_both_stamps(bench: Bench) -> None:
    """The version and the manifest go into a binary tree as they go into
    a venv, so the next release reads `kind: binary` out of a tree root
    owns."""
    bench.release()

    assert "kind: binary" in (bench.tree() / MANIFEST_STAMP_YAML).read_text(encoding="utf-8")


def test_everyone_can_run_a_released_program(bench: Bench) -> None:
    """The mode pass runs on a binary tree. The unit runs as its own user,
    which is not root."""
    bench.release()
    mode = (bench.tree() / "bin" / BINARY_NAME).stat().st_mode

    assert mode & 0o005 == 0o005
    assert not mode & 0o022


def test_the_phone_is_told_the_kind(bench: Bench) -> None:
    """§2.5's `review` field names the kinds in the set."""
    bench.release()

    assert bench.tapped == ["safe: 1 component(s), binary"]


def test_a_store_file_that_changes_after_the_tap_changes_nothing(bench: Bench, home: Path) -> None:
    """Root reads the store one time, at step 2. The release lands with the
    bytes of root's copy."""
    other = archive_bytes(programs=dict.fromkeys(PROGRAMS, b"other bytes\n"))

    def change() -> None:
        stock_store(home, TAG, other, signed_bundle(other))

    assert "succeeded" in bench.release(change), bench.reason()

    assert (bench.tree() / "bin" / BINARY_NAME).read_bytes() == PROGRAM_BYTES


# ---- check P6 refuses before the tap -----------------------------------------


def test_a_refused_signature_is_refused_before_the_tap(tmp_path: Path, home: Path) -> None:
    """The bundle signs another commit."""
    raw = archive_bytes()
    bench = _make_bench(tmp_path, home, raw, signed_bundle(raw, commit=OTHER_SHA))
    bench.install_unit(unit_text(bench.program()))

    _refused_p6(bench, ArtifactRefusal.SIGNATURE_REFUSED)


def test_a_signature_of_other_bytes_is_refused(tmp_path: Path, home: Path) -> None:
    bench = _make_bench(tmp_path, home, archive_bytes(), signed_bundle(b"other bytes"))
    bench.install_unit(unit_text(bench.program()))

    _refused_p6(bench, ArtifactRefusal.SIGNATURE_REFUSED)


def test_an_absent_verifier_is_no_verifier(
    tmp_path: Path,
    home: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_site(tmp_path_factory, monkeypatch, home, str(tmp_path / "absent" / "cosign"))
    bench = _make_bench(tmp_path, home)
    bench.install_unit(unit_text(bench.program()))

    _refused_p6(bench, ArtifactRefusal.VERIFIER_UNAVAILABLE)
    assert bench.stand_in.words == []


def test_a_verifier_file_that_another_account_can_write_is_not_run(
    tmp_path: Path,
    home: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file is the test account's, and each account can write it."""
    verifier = write_program(tmp_path / "verifier" / "cosign", mode=0o777)
    _use_site(tmp_path_factory, monkeypatch, home, str(verifier))
    bench = _make_bench(tmp_path, home)
    bench.install_unit(unit_text(bench.program()))

    _refused_p6(bench, ArtifactRefusal.VERIFIER_UNAVAILABLE)
    assert bench.stand_in.words == []


def test_an_absent_artifact_is_refused_before_the_tap(tmp_path: Path, home: Path) -> None:
    bench = _make_bench(tmp_path, home, stocked=False)
    bench.install_unit(unit_text(bench.program()))

    _refused_p6(bench, ArtifactRefusal.NO_ARTIFACT)
    assert bench.stand_in.words == []


def test_an_archive_with_no_bundle_is_refused_before_the_tap(tmp_path: Path, home: Path) -> None:
    bench = _make_bench(tmp_path, home, stocked=False)
    bench.install_unit(unit_text(bench.program()))
    stock_store(home, TAG, archive_bytes(), None)

    _refused_p6(bench, ArtifactRefusal.NO_ARTIFACT)


def _link(tar: tarfile.TarFile) -> None:
    info = tarfile.TarInfo(f"{BINARY_NAME}/bin/linked")
    info.type = tarfile.SYMTYPE
    info.linkname = "/usr/bin/env"
    tar.addfile(info)


def test_an_archive_with_a_link_is_refused_before_the_tap(tmp_path: Path, home: Path) -> None:
    """The signature passes. The member list holds a link."""
    raw = archive_bytes(extra=_link)
    bench = _make_bench(tmp_path, home, raw, signed_bundle(raw))
    bench.install_unit(unit_text(bench.program()))

    _refused_p6(bench, ArtifactRefusal.SHAPE_REFUSED)


def test_an_archive_of_another_component_is_refused_before_the_tap(
    tmp_path: Path, home: Path
) -> None:
    """One run signs each archive of one commit, so the signature passes.
    The members name another component."""
    raw = archive_bytes(component="caregiver")
    bench = _make_bench(tmp_path, home, raw, signed_bundle(raw))
    bench.install_unit(unit_text(bench.program()))

    _refused_p6(bench, ArtifactRefusal.SHAPE_REFUSED)


# ---- after the tap -----------------------------------------------------------


def test_a_copy_removed_after_the_tap_is_refused_with_drift(bench: Bench) -> None:
    """Step 8 hashes the copy again before it resolves. With no copy the
    hash of the resolved manifest differs, and no component is staged."""

    def remove() -> None:
        bench.copy().unlink()

    assert "refused" in bench.release(remove)

    assert bench.ledger()["refused_check"] == "drift"
    assert bench.step("stage")["status"] == "refused"
    _untouched(bench)


def test_an_unpack_fault_fails_the_stage(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fault comes after the second hash of the copy."""

    def refuse(archive: Path, into: Path, component: str | None = None) -> None:
        del archive, into, component
        raise UnpackRefused(UnpackFault.NOT_READABLE)

    monkeypatch.setattr(install, "extract_tar", refuse)

    assert "failed" in bench.release()

    assert bench.ledger()["status"] == "failed"
    assert bench.ledger()["refused_check"] is None
    assert bench.step("stage")["status"] == "failed"
    _untouched(bench)


# ---- a tree that is not self-contained ----------------------------------------


def test_a_program_that_names_the_work_tree_is_refused(tmp_path: Path, home: Path) -> None:
    """The archive passes P6, and a program holds the path of the work tree
    that root fetched for this release. The walk after the unpack refuses
    the tree."""
    source = tmp_path / "work" / REQUEST_ID / BINARY_NAME
    raw = archive_bytes(
        programs={BINARY_NAME: f"reads {source}/rust\n".encode(), f"{BINARY_NAME}-verify": b"ok"}
    )
    bench = _make_bench(tmp_path, home, raw, signed_bundle(raw))
    bench.install_unit(unit_text(bench.program()))

    assert "refused" in bench.release()

    assert bench.ledger()["refused_check"] == "editable"
    assert f"bin/{BINARY_NAME} {NAMES_WORK_TREE}" in bench.reason()
    _untouched(bench)


def test_a_unit_whose_program_the_archive_does_not_hold_is_refused(
    tmp_path: Path, home: Path
) -> None:
    """The release carries a unit that starts `noticeboard-next`. The
    archive holds no such program, so the restart would start nothing."""
    bench = _make_bench(tmp_path, home)
    bench.install_unit(unit_text(bench.program()))
    _serve(bench, unit_text(bench.program("noticeboard-next")))

    assert "refused" in bench.release()

    assert bench.ledger()["refused_check"] == "editable"
    assert f"bin/noticeboard-next {NOT_STAGED}" in bench.reason()
    _untouched(bench)


# ---- a unit that starts another tree -------------------------------------------


def test_a_unit_outside_the_tree_is_refused_before_the_unpack(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 06 §1 rule 8, for a binary component. The refusal comes
    before the unpack, so the host is exactly as it was."""
    unpacked: list[Path] = []

    def record(archive: Path, into: Path, component: str | None = None) -> None:
        del into, component
        unpacked.append(archive)

    monkeypatch.setattr(install, "extract_tar", record)
    bench = _make_bench(tmp_path, home)
    bench.install_unit(unit_text(ELSEWHERE))

    assert "refused" in bench.release()

    assert bench.ledger()["refused_check"] == "unit"
    assert unpacked == []
    _untouched(bench)
