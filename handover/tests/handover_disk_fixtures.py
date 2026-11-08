"""What the `kind: disk-image` tests of the executor share.

The `disks` row is in the catalog (`catalog.ARRIVING`), and no manifest of
this repository says `disk-image` yet. The executor reads the kind from the
manifest, so a test serves `manifest_text("disks")` from the fake clone.
This module holds:

1. `DISK_BYTES` and `stock_disk_store`: the files of the row and their
   bundles, in the store below the operator home, as the operator side
   leaves them.
2. `verified_files`: a `VerifiedFiles`, from check P6 itself.
3. `DiskBench`: a whole release of `disks`, from the request to the
   ledger, with the stand-in verifier of `handover_bin_fixtures.py` and the
   verify program in the live `vmm` tree. `release_with_vmm` files one set
   of `vmm` and `disks`, and the vmm archive of the store holds a new
   verify program (CH78 rule 1).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from handover.artifacts import (
    asset_limits,
    disk_files,
    signer_policy,
    sum_lines_digest,
)
from handover.artifacts import asset_names as archive_names
from handover.catalog import BUNDLE_SUFFIX, CATALOG, CATALOG_BY_NAME, Kind
from handover.executor.artifact import CopiedFiles, SignedCopy
from handover.executor.drain import Counter, handle
from handover.executor.host import Host
from handover.executor.live_state import write_manifest_stamp, write_stamp
from handover.executor.provenance import VerifiedFiles, check_files
from handover.executor.source import artifact_store
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover.manifest import Markup
from handover_bin_fixtures import (
    OWNER,
    STORE_DIR_MODE,
    STORE_FILE_MODE,
    StandIn,
    archive_bytes,
    signed_bundle,
    site_text,
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
    this_uid,
    write_request,
)
from handover_fixtures import DISK_HOOK_HOLDER, manifest_text

from handover import site

DISKS = "disks"
LIVE_VERSION = "0.1.0"
NEW_VERSION = "0.2.0"
TAG = f"{DISKS}-v{NEW_VERSION}"
REPO = str(CATALOG_BY_NAME[DISKS].repo)
VMM = DISK_HOOK_HOLDER

#: The bytes of each file of the row: small stand-ins for the kernel, the
#: root disks and `digests.json`.
DISK_BYTES: dict[str, bytes] = {
    one.name: f"the bytes of {one.name}\n".encode() for one in disk_files()
}

#: The bytes of each file of the previous release, in the live tree.
LIVE_BYTES: dict[str, bytes] = {name: f"the old bytes of {name}\n".encode() for name in DISK_BYTES}

#: The program of the vmm tree that the hook of the `disks` row names.
HOOK_BYTES = b"\x7fELF a vmm\n"

#: The `vmm` release that `release_with_vmm` files beside `disks`.
VMM_LIVE_VERSION = "0.1.0"
VMM_NEW_VERSION = "0.3.0"
VMM_TAG = f"{VMM}-v{VMM_NEW_VERSION}"

#: The same program in the vmm archive of `VMM_TAG`.
NEW_HOOK_BYTES = b"\x7fELF the new vmm\n"

#: The build of the vmm manifest. The fixture manifests carry no `build`,
#: and a binary row needs one. Root runs none of it: it unpacks the archive.
VMM_BUILD_LINE = (
    'build:\n  - ["cargo", "install", "--locked", "--no-track",'
    f' "--path", "rust/crates/{VMM}"]\ninstall:'
)


def asset_names() -> list[str]:
    """Each asset of the row, each file before its bundle."""
    return [one.name for one in asset_limits(DISKS, Kind.DISK_IMAGE)]


def disk_bundle(raw: bytes) -> bytes:
    """The bundle that the stand-in verifier passes for `raw`: a signature
    of the release workflow of creche at the commit of the `disks` tag."""
    return signed_bundle(raw, commit=SHA_OF[DISKS])


def stock_disk_store(
    home: Path,
    files: Mapping[str, bytes] | None = None,
    bundles: Mapping[str, bytes] | None = None,
    tag: str = TAG,
) -> Path:
    """Each file of the row and its bundle in the store below `home`, as the
    operator side leaves them: each directory 0700, each file 0600. A
    bundle not in `bundles` is the good bundle of its file."""
    found = DISK_BYTES if files is None else files
    directory = home
    for part in (*artifact_store().relative_to(home).parts, REPO, tag):
        directory = directory / part
        directory.mkdir(exist_ok=True)
        directory.chmod(STORE_DIR_MODE)

    for name, raw in found.items():
        bundle = (bundles or {}).get(name, disk_bundle(raw))
        for path, body in ((directory / name, raw), (directory / f"{name}{BUNDLE_SUFFIX}", bundle)):
            path.write_bytes(body)
            path.chmod(STORE_FILE_MODE)

    return directory


def stock_vmm_store(home: Path) -> Path:
    """The archive of `VMM_TAG` and its bundle in the store below `home`.
    The archive holds the verify program of the vmm and `NEW_HOOK_BYTES` as
    the program that the hook of `disks` names."""
    programs = {f"{VMM}-verify": b"\x7fELF the vmm verify\n", VMM: NEW_HOOK_BYTES}
    archive = archive_bytes(VMM, programs)
    directory = home
    for part in (*artifact_store().relative_to(home).parts, REPO, VMM_TAG):
        directory = directory / part
        directory.mkdir(exist_ok=True)
        directory.chmod(STORE_DIR_MODE)

    names = archive_names(VMM)
    bundle = signed_bundle(archive, commit=SHA_OF[VMM])
    for name, raw in ((names.archive, archive), (names.bundle, bundle)):
        (directory / name).write_bytes(raw)
        (directory / name).chmod(STORE_FILE_MODE)

    return directory


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verified_files(
    where: Path, files: Mapping[str, bytes] | None = None, component: str = DISKS
) -> VerifiedFiles:
    """A `VerifiedFiles` of `files` for `component`, from check P6 itself:
    a copy in `where`, and a runner that passes each verifier child. The
    digest is the one that root's copy gives (`artifact.py` rule 5)."""
    found = DISK_BYTES if files is None else files
    where.mkdir(parents=True, exist_ok=True)
    signed: list[SignedCopy] = []
    for name, raw in found.items():
        (where / name).write_bytes(raw)
        (where / f"{name}{BUNDLE_SUFFIX}").write_bytes(b"{}")
        signed.append(SignedCopy(name, where / name, where / f"{name}{BUNDLE_SUFFIX}"))

    digest = sum_lines_digest({name: sha256_hex(raw) for name, raw in found.items()})
    copy = CopiedFiles(files=tuple(signed), digest=f"sha256:{digest}")
    policy = signer_policy(CATALOG_BY_NAME[DISKS].repo, OWNER, REPO, SHA_OF[DISKS])

    return check_files(FakeRun(), component, policy, copy)


def write_hook(tree: Path, mode: int = 0o755) -> Path:
    """The verify program of the `disks` hook, in a `vmm` tree."""
    program = tree / "bin" / VMM
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_bytes(HOOK_BYTES)
    program.chmod(mode)

    return program


def use_site(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A site file whose operator home is `home`, and that names the two
    root-owned programs of the stand-in verifier."""
    site_file = tmp_path / "site.env"
    site_file.write_text(site_text(home), encoding="utf-8")
    monkeypatch.setenv(site.SITE_FILE_ENV, str(site_file))


@dataclass
class DiskBench:
    """A whole release of `disks` from `LIVE_VERSION` to `NEW_VERSION`. The
    store holds each file of the new tag and its good bundle. The live
    `vmm` tree holds the verify program and its manifest stamp."""

    run: FakeRun
    host: Host
    spool_root: Path
    components: Path

    @property
    def live(self) -> Path:
        return self.components / DISKS

    def wiring(self) -> Wiring:
        """What the release of `disks` reads off the host and GitHub. The
        tag `VMM_TAG` is green too, for `release_with_vmm`."""
        api = green_api(REPO, TAG, SHA_OF[DISKS])
        api.bodies.update(green_api(REPO, VMM_TAG, SHA_OF[VMM]).bodies)

        return Wiring(
            host=self.host,
            transport=grant_transport(),
            readers=fake_readers(latest={DISKS: NEW_VERSION, VMM: VMM_NEW_VERSION}),
            api=api,
        )

    def release(self) -> dict[str, object]:
        """Run the release of `disks` alone. The answer is the ledger
        entry."""
        return self._release({DISKS: NEW_VERSION})

    def release_with_vmm(self) -> dict[str, object]:
        """Run one release of `vmm` and `disks`, as the follower files for
        one merge that tags both (CH78 rule 1). The answer is the ledger
        entry."""
        return self._release({VMM: VMM_NEW_VERSION, DISKS: NEW_VERSION})

    def _release(self, wanted: dict[str, str]) -> dict[str, object]:
        wiring = self.wiring()
        write_request(self.spool_root, REQUEST_ID, request_body(wanted))
        spool = Spool(str(self.spool_root), this_uid())
        try:
            handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
        finally:
            spool.close()

        done = self.spool_root / DONE_DIR / f"{REQUEST_ID}.json"
        loaded = json.loads(done.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return cast("dict[str, object]", loaded)


def disk_manifest_text(components: Path, name: str) -> str:
    """The fixture manifest of `name`, with its tree under `components`.
    The vmm manifest gets `VMM_BUILD_LINE`."""
    text = manifest_text(name).replace("/opt/components", str(components))
    if name == VMM:
        text = text.replace("install:", VMM_BUILD_LINE, 1)

    return text


def make_disk_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DiskBench:
    """The bench: a site file, a stocked store, the live `disks` and `vmm`
    trees, and a fake clone that serves each manifest of the catalog."""
    home = tmp_path / "home"
    home.mkdir()
    use_site(home, tmp_path, monkeypatch)
    stock_disk_store(home)
    stock_vmm_store(home)
    components = tmp_path / "components"
    run = FakeRun(answers=git_host_answers())
    host = replace(fake_host(tmp_path, run), run=StandIn(run))
    for name, version in ((VMM, VMM_LIVE_VERSION), (DISKS, LIVE_VERSION)):
        tree = components / name
        tree.mkdir(parents=True)
        write_stamp(tree, version)
        write_manifest_stamp(tree, disk_manifest_text(components, name), Markup.YAML)

    write_hook(components / VMM)
    for name, raw in LIVE_BYTES.items():
        (components / DISKS / name).write_bytes(raw)

    def write_tree(destination: Path, name: str) -> None:
        del name
        for row in CATALOG:
            sub = destination if row.path == "." else destination / row.path
            sub.mkdir(parents=True, exist_ok=True)
            text = disk_manifest_text(components, row.name)
            (sub / "component.yaml").write_text(text, encoding="utf-8")

    run.dynamic = GitFake(write_tree)

    return DiskBench(
        run=run, host=host, spool_root=make_spool_dirs(tmp_path), components=components
    )
