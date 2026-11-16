"""The assets of a disk-image component: its files, their limits, their bundles.

A `kind: disk-image` component has N assets and no archive: each file of
contract 06 §8.4 and the bundle of its signature (§3.4, CH35 (4), CH76 rule
5). `artifacts.asset_limits` holds the list and the limits, so the tag
script, the operator fetch and root's copy read one source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from handover.artifacts import asset_limits, disk_files
from handover.catalog import (
    BUNDLE_MAX_BYTES,
    CATALOG_BY_NAME,
    CI_DIGESTS_MAX_BYTES,
    CI_IMAGE,
    DIGESTS_MAX_BYTES,
    DISK_FILE_MAX_BYTES,
    Kind,
)
from handover.errors import Refusal, RefusalCode

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The reader of `digests.json` in the vmm (CH57 rule 1).
IMAGE_ROOT_RS = REPO_ROOT / "rust" / "crates" / "vmm" / "src" / "image_root.rs"

DIGESTS_CAP_RE = re.compile(r"pub const DIGESTS_CAP: u64 = (\d+) \* (\d+);")

GIB = 1024 * 1024 * 1024

#: The disk-image rows of the catalog, in catalog order.
DISK_ROWS = ("disks", CI_IMAGE)

#: The file that each build job of release.yml hands to the `sign` job
#: beside the files of its row. No file of a row may have its name.
SUMS = "SHA256SUMS"


def _lines(component: str = "disks") -> list[tuple[str, int]]:
    return [(one.name, one.max_bytes) for one in asset_limits(component, Kind.DISK_IMAGE)]


def test_the_disk_image_rows_are_disks_and_the_ci_runner_image() -> None:
    assert tuple(row.name for row in CATALOG_BY_NAME.values() if row.kind is Kind.DISK_IMAGE) == (
        DISK_ROWS
    )


def test_each_disk_image_row_has_its_files() -> None:
    """`disk_files` answers for each disk-image row of the catalog."""
    assert all(disk_files(name) for name in DISK_ROWS)


def test_a_name_with_no_disk_image_row_has_no_files() -> None:
    """The `catalog` code: a binary row ships an archive, not files."""
    with pytest.raises(Refusal) as caught:
        disk_files("vmm")

    assert caught.value.code is RefusalCode.CATALOG


def test_the_disks_row_lists_each_file_and_bundle() -> None:
    """`digests.json`, the kernel, then one disk for each flavor, then one
    for each service image. Each file comes right before its own bundle
    (contract 06 §8.4 rule 2)."""
    assert _lines() == [
        ("digests.json", 64 * 1024),
        ("digests.json.sigstore.json", BUNDLE_MAX_BYTES),
        ("vmlinux-x86_64.zst", 2 * GIB - 1),
        ("vmlinux-x86_64.zst.sigstore.json", BUNDLE_MAX_BYTES),
        ("base.ext4.zst", 2 * GIB - 1),
        ("base.ext4.zst.sigstore.json", BUNDLE_MAX_BYTES),
        ("python.ext4.zst", 2 * GIB - 1),
        ("python.ext4.zst.sigstore.json", BUNDLE_MAX_BYTES),
        ("coding.ext4.zst", 2 * GIB - 1),
        ("coding.ext4.zst.sigstore.json", BUNDLE_MAX_BYTES),
        ("browser.ext4.zst", 2 * GIB - 1),
        ("browser.ext4.zst.sigstore.json", BUNDLE_MAX_BYTES),
    ]


def test_the_ci_runner_image_row_lists_each_file_and_bundle() -> None:
    """`digests.sha256`, the kernel, the initrd, then the root disk. Each
    file comes right before its own bundle (docs/design/ci-runner.md,
    section 8, PR 3)."""
    assert _lines(CI_IMAGE) == [
        ("digests.sha256", 4 * 1024),
        ("digests.sha256.sigstore.json", BUNDLE_MAX_BYTES),
        ("vmlinuz", 2 * GIB - 1),
        ("vmlinuz.sigstore.json", BUNDLE_MAX_BYTES),
        ("initrd.img", 2 * GIB - 1),
        ("initrd.img.sigstore.json", BUNDLE_MAX_BYTES),
        ("root.qcow2.zst", 2 * GIB - 1),
        ("root.qcow2.zst.sigstore.json", BUNDLE_MAX_BYTES),
    ]
    assert CI_DIGESTS_MAX_BYTES == 4 * 1024


def test_each_disk_file_is_under_the_cap_of_a_release_asset() -> None:
    """GitHub takes an asset under 2 GiB."""
    assert DISK_FILE_MAX_BYTES == 2 * GIB - 1
    assert all(one.max_bytes < 2 * GIB for name in DISK_ROWS for one in disk_files(name))


def test_no_two_disk_rows_ship_a_file_of_one_name() -> None:
    """The `sign` job and the tag step of release.yml read the files of
    each row from one directory. Two files of one name would overwrite each
    other there. The `SHA256SUMS` of each build job is beside them."""
    names = [one.name for name in DISK_ROWS for one in disk_files(name)]

    assert len(set(names)) == len(names)
    assert SUMS not in names


def test_the_digests_limit_is_the_cap_of_the_vmm_reader() -> None:
    """The vmm refuses a `digests.json` past `DIGESTS_CAP`, so CI and root
    hold the file to the same number."""
    matched = DIGESTS_CAP_RE.search(IMAGE_ROOT_RS.read_text(encoding="utf-8"))
    assert matched is not None

    assert int(matched.group(1)) * int(matched.group(2)) == DIGESTS_MAX_BYTES


@pytest.mark.parametrize("component", DISK_ROWS)
def test_the_names_have_one_reading(component: str) -> None:
    """Each name is one word of a line form, and no name repeats."""
    names = [name for name, _ in _lines(component)]

    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) for name in names)
