"""Step 8 for a `kind: disk-image` component, and a whole release of one.

Root copies each file that check P6 verified into `<install.to>.new`,
unchanged, root's and 0644. It runs no build and unpacks nothing (contract
06 §8.4 rule 4, CH35 (4), CH76 rule 5). The verify program sits in the tree
of the `depends_on` component (§4 rule 6, CH76 rule 6). Three groups of
tests:

1. The approval digest: the hash of the sorted sum lines of the files.
2. `Installer.stage_files` and the borrowed hook, on a fake host.
3. Where step 8 looks for the hook: the live tree of the holder, or its
   staged tree in a set that switches the holder first (CH78 rule 1).
4. A whole release of `disks`, alone and in one set with the `vmm`, with
   the stand-in verifier.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import cast

import pytest
from handover.artifacts import sum_lines_digest
from handover.catalog import BUNDLE_SUFFIX
from handover.executor import install
from handover.executor.install import BorrowedTree, Installer, StepFailed, paths_of
from handover.executor.live_state import installed_version
from handover.executor.request import parse_request
from handover.executor.spool import Spool
from handover.executor.steps import DISK_IMAGE_HOLDER_LATER, SELF_COMPONENT, Plan, Release
from handover.manifest import ComponentManifest, Markup, parse_manifest
from handover.resolve import Resolution
from handover_disk_fixtures import (
    DISK_BYTES,
    DISKS,
    HOOK_BYTES,
    LIVE_BYTES,
    LIVE_VERSION,
    NEW_HOOK_BYTES,
    NEW_VERSION,
    VMM,
    VMM_LIVE_VERSION,
    VMM_NEW_VERSION,
    DiskBench,
    disk_manifest_text,
    make_disk_bench,
    sha256_hex,
    verified_files,
    write_hook,
)
from handover_executor_fixtures import REQUEST_ID, FakeRun, fake_host, request_body, this_uid
from handover_fixtures import manifest_text

#: `shasum -a 256 <the five files of DISK_BYTES> | LC_ALL=C sort | shasum -a
#: 256`, run once by hand. A change of the rule, of the line form or of the
#: files of the row moves it.
GOLDEN_DIGEST = "900bcc9567f0c508947baaf3c649848347e1dadb6196da9001fec88af063c393"


# ---- 1. the approval digest ----------------------------------------------------


def test_the_digest_of_a_disk_row_is_the_hash_of_its_sum_lines() -> None:
    """Contract 06 §9: the SHA-256 of the sorted `sha256sum` lines of the
    files, one `<64 hex>  <file>` line each (CH76 rule 5)."""
    digests = {name: sha256_hex(raw) for name, raw in DISK_BYTES.items()}

    assert sum_lines_digest(digests) == GOLDEN_DIGEST


def test_the_digest_reads_no_order_of_its_input() -> None:
    digests = {name: sha256_hex(raw) for name, raw in DISK_BYTES.items()}

    assert sum_lines_digest(dict(reversed(digests.items()))) == GOLDEN_DIGEST


def test_one_changed_byte_of_one_file_moves_the_digest() -> None:
    changed = {**DISK_BYTES, "base.ext4.zst": b"the bytes of base.ext4.zsT\n"}

    digests = {name: sha256_hex(raw) for name, raw in changed.items()}

    assert sum_lines_digest(digests) != GOLDEN_DIGEST


# ---- 2. the stage --------------------------------------------------------------


def _manifest(components: Path, name: str = DISKS) -> ComponentManifest:
    return parse_manifest(
        disk_manifest_text(components, name), f"{name}/component.yaml", Markup.YAML
    )


def _installer(tmp_path: Path, run: FakeRun | None = None) -> Installer:
    return Installer(fake_host(tmp_path, run if run is not None else FakeRun()))


def _live_hook(components: Path) -> BorrowedTree:
    """The hook in the live `vmm` tree, which this release does not stage.
    The answer checks that tree."""
    live = components / VMM
    write_hook(live)

    return BorrowedTree(to=live, checked=live)


def test_the_staged_tree_holds_each_file_root_owned_0644(tmp_path: Path) -> None:
    """The release unit runs with the umask 0027, and the vmm reads each
    file as the operator later (CH57 rule 1)."""
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))
    verified = verified_files(tmp_path / "copy")
    before = os.umask(0o027)
    try:
        _installer(tmp_path).stage_files(manifest, paths, verified, _live_hook(components))
    finally:
        os.umask(before)

    assert sorted(one.name for one in paths.new.iterdir()) == sorted(DISK_BYTES)
    for name, raw in DISK_BYTES.items():
        staged = paths.new / name
        facts = staged.lstat()
        assert staged.read_bytes() == raw
        assert stat.S_ISREG(facts.st_mode)
        assert stat.S_IMODE(facts.st_mode) == 0o644
        assert facts.st_uid == os.geteuid()


def test_no_bundle_is_staged(tmp_path: Path) -> None:
    """Root installs the files and keeps no bundle: the vmm reads none."""
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))

    _installer(tmp_path).stage_files(
        manifest, paths, verified_files(tmp_path / "copy"), _live_hook(components)
    )

    assert not [one for one in paths.new.iterdir() if one.name.endswith(BUNDLE_SUFFIX)]


def test_a_disk_row_runs_no_build_and_no_unpack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage starts no child and unpacks nothing. `build` refuses a
    disk-image manifest before it touches a tree: its `build` is CI's."""
    unpacked: list[Path] = []
    monkeypatch.setattr(install, "extract_tar", lambda archive, *_: unpacked.append(archive))
    run = FakeRun()
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))
    installer = _installer(tmp_path, run)

    installer.stage_files(
        manifest, paths, verified_files(tmp_path / "copy"), _live_hook(components)
    )
    with pytest.raises(StepFailed, match="root runs no build of a disk-image component"):
        installer.build(manifest, tmp_path / "source", paths)

    assert run.seen == []
    assert unpacked == []
    assert sorted(one.name for one in paths.new.iterdir()) == sorted(DISK_BYTES)


def test_no_verified_files_is_a_failed_stage(tmp_path: Path) -> None:
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))

    with pytest.raises(StepFailed, match="no files that check P6 verified"):
        _installer(tmp_path).stage_files(manifest, paths, None, _live_hook(components))

    assert not paths.new.exists()


def test_files_that_p6_verified_for_another_component_fail_the_stage(tmp_path: Path) -> None:
    """Step 8 of `disks` stages only the files that check P6 verified for
    `disks`."""
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))
    other = verified_files(tmp_path / "copy", component=VMM)

    with pytest.raises(StepFailed, match="no files that check P6 verified"):
        _installer(tmp_path).stage_files(manifest, paths, other, _live_hook(components))

    assert other.component == VMM
    assert not paths.new.exists()


def test_a_copy_that_changed_after_the_check_fails_the_stage(tmp_path: Path) -> None:
    """The stage hashes what it writes. Other bytes give other sum lines,
    and the tap bound the first ones."""
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))
    verified = verified_files(tmp_path / "copy")
    (tmp_path / "copy" / "python.ext4.zst").write_bytes(b"other bytes\n")

    with pytest.raises(StepFailed, match="not the files P6 verified"):
        _installer(tmp_path).stage_files(manifest, paths, verified, _live_hook(components))


def test_the_verify_program_comes_from_the_live_vmm_tree(tmp_path: Path) -> None:
    """The `disks` tree holds no program. The hook names the vmm tree, and
    the stage checks the program there (CH76 rule 6)."""
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))
    hook = _live_hook(components)

    _installer(tmp_path).stage_files(manifest, paths, verified_files(tmp_path / "copy"), hook)

    assert manifest.verify.command[0] == str(components / VMM / "bin" / VMM)
    assert (components / VMM / "bin" / VMM).read_bytes() == HOOK_BYTES
    assert not (paths.new / "bin").exists()


def _staged_hook(components: Path) -> BorrowedTree:
    """A set that switches the `vmm` first: the hook names the live tree,
    and the stage checks the staged `vmm.new` (CH78 rule 1)."""
    return BorrowedTree(to=components / VMM, checked=components / f"{VMM}.new")


def test_a_staged_holder_has_the_program_checked_in_its_staged_tree(tmp_path: Path) -> None:
    """The live vmm tree holds no program. The staged one does, and that
    one is live when the hook runs."""
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))
    (components / VMM).mkdir(parents=True)
    write_hook(components / f"{VMM}.new")

    hook = _staged_hook(components)
    _installer(tmp_path).stage_files(manifest, paths, verified_files(tmp_path / "copy"), hook)

    assert (paths.new / "base.ext4.zst").is_file()


def test_a_staged_holder_with_no_program_fails_the_stage(tmp_path: Path) -> None:
    """The live vmm tree holds a good program. The staged one, which is
    live when the hook runs, holds none."""
    components = tmp_path / "components"
    write_hook(components / VMM)
    (components / f"{VMM}.new").mkdir(parents=True)

    _refused(tmp_path, _staged_hook(components), "missing or escapes")


def test_a_staged_program_that_links_to_the_live_tree_fails_the_stage(tmp_path: Path) -> None:
    """The swap moves the live tree to `.prev`, so a link out of the staged
    tree names no program of the release."""
    components = tmp_path / "components"
    live = write_hook(components / VMM)
    (components / f"{VMM}.new" / "bin").mkdir(parents=True)
    (components / f"{VMM}.new" / "bin" / VMM).symlink_to(live)

    _refused(tmp_path, _staged_hook(components), "missing or escapes")


def _refused(tmp_path: Path, hook: Path | BorrowedTree, match: str) -> None:
    """`hook` is a tree that the stage checks in place, or a whole
    `BorrowedTree`."""
    components = tmp_path / "components"
    manifest = _manifest(components)
    paths = paths_of(manifest, (components,))
    holder = hook if isinstance(hook, BorrowedTree) else BorrowedTree(to=hook, checked=hook)

    with pytest.raises(StepFailed, match=match):
        _installer(tmp_path).stage_files(manifest, paths, verified_files(tmp_path / "copy"), holder)


def test_an_absent_verify_program_fails_the_stage(tmp_path: Path) -> None:
    live = tmp_path / "components" / VMM
    live.mkdir(parents=True)

    _refused(tmp_path, live, "missing or escapes")


@pytest.mark.parametrize("mode", [0o775, 0o757], ids=["group", "other"])
def test_a_verify_program_that_others_can_write_fails_the_stage(tmp_path: Path, mode: int) -> None:
    live = tmp_path / "components" / VMM
    write_hook(live, mode)

    _refused(tmp_path, live, "not root's alone")


def test_a_verify_program_that_root_does_not_own_fails_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is 0755, so only the owner half of the rule refuses: root
    is now another uid than the owner of the file."""
    live = tmp_path / "components" / VMM
    owner = write_hook(live).stat().st_uid
    monkeypatch.setattr(install.os, "geteuid", lambda: owner + 1)

    _refused(tmp_path, live, "not root's alone")


def test_a_verify_program_that_links_out_of_its_tree_fails_the_stage(tmp_path: Path) -> None:
    live = tmp_path / "components" / VMM
    elsewhere = write_hook(tmp_path / "elsewhere")
    (live / "bin").mkdir(parents=True)
    (live / "bin" / VMM).symlink_to(elsewhere)

    _refused(tmp_path, live, "missing or escapes")


def test_a_hook_outside_the_tree_it_borrows_fails_the_stage(tmp_path: Path) -> None:
    other = tmp_path / "components" / "caregiver"
    write_hook(other)

    _refused(tmp_path, other, "in no tree of depends_on")


# ---- 3. where step 8 looks for the hook ------------------------------------------


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DiskBench:
    return make_disk_bench(tmp_path, monkeypatch)


def _holder_tree(
    bench: DiskBench, order: tuple[str, ...], disks_text: str | None = None
) -> BorrowedTree:
    """`Release._hook_holder_tree` for the `disks` manifest, in a plan that
    deploys `order`. `disks_text` replaces the manifest of `disks`."""
    texts = {name: disk_manifest_text(bench.components, name) for name in (VMM, DISKS)}
    texts[SELF_COMPONENT] = manifest_text(SELF_COMPONENT).replace(
        "/opt/components", str(bench.components)
    )
    if disks_text is not None:
        texts[DISKS] = disks_text

    manifests = {
        name: parse_manifest(text, f"{name}/component.yaml", Markup.YAML)
        for name, text in texts.items()
    }
    plan = Plan(Resolution((), order, (), frozenset(order)), manifests, {}, order)
    raw = request_body(dict.fromkeys(order, NEW_VERSION))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        release = Release(
            parse_request(json.dumps(raw).encode("utf-8"), REQUEST_ID), bench.wiring(), spool
        )

        return release._hook_holder_tree(plan, manifests[DISKS])  # pyright: ignore[reportPrivateUsage]
    finally:
        spool.close()


def test_a_set_of_disks_alone_reads_the_hook_in_the_live_vmm_tree(bench: DiskBench) -> None:
    live = bench.components / VMM

    assert _holder_tree(bench, (DISKS,)) == BorrowedTree(to=live, checked=live)


def test_a_set_that_switches_the_vmm_first_reads_its_staged_tree(bench: DiskBench) -> None:
    """CH78 rule 1: step 9 switches the `vmm` before `disks`, so the hook
    runs from the vmm tree that the set installs."""
    live = bench.components / VMM

    assert _holder_tree(bench, (VMM, DISKS)) == BorrowedTree(
        to=live, checked=live.with_name(f"{VMM}.new")
    )


def test_a_set_that_switches_the_vmm_after_the_disks_fails_the_stage(bench: DiskBench) -> None:
    """The order rule of CH78 rule 1: the hook would run while the old vmm
    tree is live, so step 8 refuses before any swap."""
    with pytest.raises(StepFailed, match=DISK_IMAGE_HOLDER_LATER):
        _holder_tree(bench, (DISKS, VMM))


def test_a_holder_that_switches_after_the_ledger_fails_the_stage(bench: DiskBench) -> None:
    """`handover` switches after the ledger, outside step 9 (contract 06
    §1.1 rule 2). A hook in its tree would run before that switch, even
    where `plan.order` puts `handover` first."""
    disks = disk_manifest_text(bench.components, DISKS)
    disks = disks.replace(f"depends_on: [{VMM}]", f"depends_on: [{SELF_COMPONENT}]")
    disks = disks.replace(f"/{VMM}/bin/{VMM}", f"/{SELF_COMPONENT}/bin/{SELF_COMPONENT}")

    with pytest.raises(StepFailed, match=DISK_IMAGE_HOLDER_LATER):
        _holder_tree(bench, (SELF_COMPONENT, DISKS), disks)


# ---- 4. a whole release --------------------------------------------------------


def _row(ledger: dict[str, object]) -> dict[str, object]:
    manifest = cast("dict[str, object]", ledger["manifest"])
    rows = cast("list[dict[str, object]]", manifest["components"])

    return next(one for one in rows if one["name"] == DISKS)


def test_a_disk_release_installs_each_file_unchanged(bench: DiskBench) -> None:
    ledger = bench.release()

    assert ledger["status"] == "succeeded", ledger.get("reason")
    assert installed_version(bench.live) == NEW_VERSION
    for name, raw in DISK_BYTES.items():
        assert (bench.live / name).read_bytes() == raw
        assert stat.S_IMODE((bench.live / name).stat().st_mode) == 0o644

    assert (bench.live.with_name(f"{DISKS}.prev") / "base.ext4.zst").read_bytes() == LIVE_BYTES[
        "base.ext4.zst"
    ]


def test_the_resolved_manifest_binds_the_sum_lines_of_the_files(bench: DiskBench) -> None:
    ledger = bench.release()

    assert _row(ledger)["artifact_digest"] == f"sha256:{GOLDEN_DIGEST}"


def test_a_disk_release_checks_each_file_and_restarts_nothing(bench: DiskBench) -> None:
    """Child 1 of the verifier, then child 2 for each file. A disk-image
    row names no unit, so step 9 runs no `systemctl` (§8.4 rule 5)."""
    ledger = bench.release()

    assert ledger["status"] == "succeeded", ledger.get("reason")
    checks = [one for one in bench.run.argv_lines() if "verify-blob" in one]
    assert len(checks) == len(DISK_BYTES)
    for name in DISK_BYTES:
        assert any(
            line.endswith(f"/{name}") and f"{name}{BUNDLE_SUFFIX}" in line for line in checks
        )

    assert not bench.run.ran("systemctl")


def test_the_hook_runs_from_the_live_vmm_tree(bench: DiskBench) -> None:
    ran = _programs_at_the_hook(bench)

    bench.release()

    hooks = [one for one in bench.run.argv_lines() if "check-images" in one]
    assert hooks == [f"{bench.components / VMM / 'bin' / VMM} check-images --json"]
    assert ran == [HOOK_BYTES]


def _programs_at_the_hook(bench: DiskBench) -> list[bytes]:
    """The bytes of the program at each run of the disks hook, read when
    the hook runs."""
    ran: list[bytes] = []
    bench.run.hooks["check-images"] = lambda command: ran.append(Path(command.argv[0]).read_bytes())

    return ran


def test_one_set_of_vmm_and_disks_runs_the_hook_from_the_new_vmm_tree(
    bench: DiskBench,
) -> None:
    """CH78 rule 1: the `vmm` switches first, so the disks hook runs the
    program of the vmm archive that the same set installs."""
    ran = _programs_at_the_hook(bench)

    ledger = bench.release_with_vmm()

    assert ledger["status"] == "succeeded", ledger.get("reason")
    assert ran == [NEW_HOOK_BYTES]
    assert installed_version(bench.components / VMM) == VMM_NEW_VERSION
    assert installed_version(bench.live) == NEW_VERSION
    for name, raw in DISK_BYTES.items():
        assert (bench.live / name).read_bytes() == raw


def test_a_failed_vmm_verify_stops_the_set_before_the_disks(bench: DiskBench) -> None:
    """CH78 rule 1: a failed vmm step fails the set before disks switches.

    The first vmm verify fails. The restore runs it again on the old tree,
    and that run passes. The disks hook never runs and no disks tree is
    staged.
    """
    ran: list[str] = []
    bench.run.hooks["check-images"] = lambda command: ran.append(command.argv[0])
    calls = {"n": 0}
    bench.run.fails["vmm-verify"] = 1

    def pass_after_the_first_call(_: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop("vmm-verify", None)

    bench.run.hooks["vmm-verify"] = pass_after_the_first_call
    ledger = bench.release_with_vmm()
    rows = cast("list[dict[str, object]]", ledger["verify"])

    assert ledger["status"] == "restored", ledger.get("reason")
    assert [(r["component"], r["status"]) for r in rows] == [(VMM, "failed"), (VMM, "ok")]
    assert ran == []
    assert installed_version(bench.components / VMM) == VMM_LIVE_VERSION
    assert installed_version(bench.live) == LIVE_VERSION
    assert not (bench.components / f"{DISKS}.new").exists()


def test_the_release_removes_the_copies(bench: DiskBench, tmp_path: Path) -> None:
    bench.release()

    assert not (tmp_path / "work" / REQUEST_ID).exists()


def test_a_missing_file_in_the_store_refuses_at_p6(bench: DiskBench, tmp_path: Path) -> None:
    """Step 2 makes no copy, so check P6 finds none (§8.4 rule 3)."""
    store = next((tmp_path / "home").rglob(f"{DISKS}-v{NEW_VERSION}"))
    (store / "python.ext4.zst").unlink()

    ledger = bench.release()

    assert ledger["status"] == "refused"
    assert ledger["refused_check"] == "P6"
    assert installed_version(bench.live) != NEW_VERSION
