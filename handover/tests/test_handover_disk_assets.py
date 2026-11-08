"""The assets of a disk-image component: its files, their limits, their bundles.

A `kind: disk-image` component has N assets and no archive: each file of
contract 06 §8.4 and the bundle of its signature (§3.4, CH35 (4), CH76 rule
5). `artifacts.asset_limits` holds the list and the limits, so the tag
script, the operator fetch and root's copy read one source.
"""

from __future__ import annotations

import re
from pathlib import Path

from handover.artifacts import asset_limits, disk_files
from handover.catalog import (
    BUNDLE_MAX_BYTES,
    CATALOG_BY_NAME,
    DIGESTS_MAX_BYTES,
    DISK_FILE_MAX_BYTES,
    Kind,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The reader of `digests.json` in the vmm (CH57 rule 1).
IMAGE_ROOT_RS = REPO_ROOT / "rust" / "crates" / "vmm" / "src" / "image_root.rs"

DIGESTS_CAP_RE = re.compile(r"pub const DIGESTS_CAP: u64 = (\d+) \* (\d+);")

GIB = 1024 * 1024 * 1024


def _lines() -> list[tuple[str, int]]:
    return [(one.name, one.max_bytes) for one in asset_limits("disks", Kind.DISK_IMAGE)]


def test_the_disks_row_is_the_one_disk_image_row() -> None:
    assert [row.name for row in CATALOG_BY_NAME.values() if row.kind is Kind.DISK_IMAGE] == [
        "disks"
    ]


def test_the_disks_row_lists_each_file_and_bundle() -> None:
    """`digests.json`, the kernel, then one disk for each flavor. Each file
    comes right before its own bundle (contract 06 §8.4 rule 2)."""
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
    ]


def test_each_disk_file_is_under_the_cap_of_a_release_asset() -> None:
    """GitHub takes an asset under 2 GiB."""
    assert DISK_FILE_MAX_BYTES == 2 * GIB - 1
    assert all(one.max_bytes < 2 * GIB for one in disk_files())


def test_the_digests_limit_is_the_cap_of_the_vmm_reader() -> None:
    """The vmm refuses a `digests.json` past `DIGESTS_CAP`, so CI and root
    hold the file to the same number."""
    matched = DIGESTS_CAP_RE.search(IMAGE_ROOT_RS.read_text(encoding="utf-8"))
    assert matched is not None

    assert int(matched.group(1)) * int(matched.group(2)) == DIGESTS_MAX_BYTES


def test_the_names_have_one_reading() -> None:
    """Each name is one word of a line form, and no name repeats."""
    names = [name for name, _ in _lines()]

    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) for name in names)
