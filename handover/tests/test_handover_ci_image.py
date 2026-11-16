"""`handover check-ci-image`, the verify hook of the `ci-runner-image` row.

The row is a disk-image tree of four files (docs/design/ci-runner.md,
section 8, PR 3). The hook reads `digests.sha256`, decompresses the root
disk and checks each file against the digest of the build
(`ciimage.py`). Each test builds a small tree in `tmp_path`.

The ends held here:

1. A good tree passes each check, and the command exits 0.
2. Other bytes, a stream that is not whole zstd, and a stream past the cap
   fail the check of the root disk.
3. A digest file of another form fails, and no other file is checked.
4. A link, an absent file and a file past its limit fail.
5. The real verify command of the manifest runs, from the `handover` tree
   that its `depends_on` names.
"""

from __future__ import annotations

import hashlib
import json
import os
from compression import zstd
from pathlib import Path

import pytest
from handover.artifacts import disk_files
from handover.catalog import (
    CI_DIGESTS_FILE,
    CI_DIGESTS_MAX_BYTES,
    CI_IMAGE,
    CI_INITRD_FILE,
    CI_KERNEL_FILE,
    CI_ROOT_DISK,
    CI_ROOT_FILE,
)
from handover.ciimage import (
    MATCHES,
    NOT_A_FILE,
    NOT_DIGESTS,
    NOT_ZSTD,
    OTHER_BYTES,
    TOO_LARGE,
    TOO_LARGE_ROOT,
    UNCHECKED,
    FileCheck,
    check_image,
)
from handover.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from handover.discovery import discover
from handover.manifest import hook_holder

from handover import ciimage

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The bytes of each file of the image before the compression.
ROOT_BYTES = b"a root disk of qcow2\n" * 4096
KERNEL_BYTES = b"a kernel\n"
INITRD_BYTES = b"an initrd\n"


def _line(name: str, body: bytes) -> str:
    return f"{hashlib.sha256(body).hexdigest()}  {name}\n"


#: `digests.sha256` of the build: `sha256sum root.qcow2 vmlinuz initrd.img`.
DIGESTS = (
    _line(CI_ROOT_DISK, ROOT_BYTES)
    + _line(CI_KERNEL_FILE, KERNEL_BYTES)
    + _line(CI_INITRD_FILE, INITRD_BYTES)
)


def _tree(tmp_path: Path, **files: bytes) -> Path:
    """A good tree, with each file of `files` in place of its good bytes.
    A key names a file with `_` for `.`: `root_qcow2_zst`."""
    tree = tmp_path / "ci-runner-image"
    tree.mkdir()
    good = {
        CI_DIGESTS_FILE: DIGESTS.encode("ascii"),
        CI_KERNEL_FILE: KERNEL_BYTES,
        CI_INITRD_FILE: INITRD_BYTES,
        CI_ROOT_FILE: zstd.compress(ROOT_BYTES),
    }
    changed = {name.replace("_", "."): body for name, body in files.items()}
    for name, body in {**good, **changed}.items():
        (tree / name).write_bytes(body)

    return tree


def _by_file(checks: tuple[FileCheck, ...]) -> dict[str, tuple[bool, str]]:
    return {one.file: (one.ok, one.detail) for one in checks}


# ---- 1. a good tree ------------------------------------------------------------


def test_a_good_tree_passes_each_check(tmp_path: Path) -> None:
    """One check for each file of the row, in the order of `disk_files`."""
    checks = check_image(_tree(tmp_path))

    assert [one.file for one in checks] == [one.name for one in disk_files(CI_IMAGE)]
    assert all(one.ok for one in checks)
    assert [one.detail for one in checks[1:]] == [MATCHES] * 3


def test_the_command_prints_one_json_object_and_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _tree(tmp_path)

    code = main(["check-ci-image", "--tree", str(tree)])

    report = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert report["ok"] is True
    assert [one["file"] for one in report["checks"]] == [one.name for one in disk_files(CI_IMAGE)]


def test_a_root_disk_of_two_frames_passes(tmp_path: Path) -> None:
    """A zstd file may hold more than one frame. The bytes of all frames
    are the root disk."""
    half = len(ROOT_BYTES) // 2
    stream = zstd.compress(ROOT_BYTES[:half]) + zstd.compress(ROOT_BYTES[half:])

    checks = _by_file(check_image(_tree(tmp_path, root_qcow2_zst=stream)))

    assert checks[CI_ROOT_FILE] == (True, MATCHES)


# ---- 2. the root disk ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stream", "fault"),
    [
        (zstd.compress(ROOT_BYTES + b"!"), OTHER_BYTES),
        (ROOT_BYTES, NOT_ZSTD),
        (zstd.compress(ROOT_BYTES)[:-8], NOT_ZSTD),
    ],
    ids=["other-bytes", "no-zstd", "a-cut-stream"],
)
def test_a_bad_root_disk_fails_its_check(tmp_path: Path, stream: bytes, fault: str) -> None:
    checks = _by_file(check_image(_tree(tmp_path, root_qcow2_zst=stream)))

    assert checks[CI_ROOT_FILE] == (False, fault)
    assert checks[CI_KERNEL_FILE] == (True, MATCHES)


def test_a_stream_past_the_cap_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap bounds the work of the hook: a small stream can give many
    bytes."""
    monkeypatch.setattr(ciimage, "CI_ROOT_MAX_BYTES", len(ROOT_BYTES) - 1)

    checks = _by_file(check_image(_tree(tmp_path)))

    assert checks[CI_ROOT_FILE] == (False, TOO_LARGE_ROOT)


def test_the_kernel_with_other_bytes_fails(tmp_path: Path) -> None:
    checks = _by_file(check_image(_tree(tmp_path, vmlinuz=b"another kernel\n")))

    assert checks[CI_KERNEL_FILE] == (False, OTHER_BYTES)
    assert checks[CI_ROOT_FILE] == (True, MATCHES)


def test_a_failed_check_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree = _tree(tmp_path, initrd_img=b"another initrd\n")

    code = main(["check-ci-image", "--tree", str(tree)])

    report = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert report["ok"] is False


# ---- 3. the digest file ----------------------------------------------------------

#: Texts of `digests.sha256` that the hook refuses.
BAD_DIGESTS = {
    "two-lines": DIGESTS.split("\n", 1)[1],
    "four-lines": DIGESTS + _line("other", b""),
    "another-order": "".join(sorted(DIGESTS.splitlines(keepends=True))),
    "no-last-line-feed": DIGESTS.removesuffix("\n"),
    "a-carriage-return": DIGESTS.replace("\n", "\r\n"),
    "upper-case-hex": DIGESTS.upper(),
    "binary-mode": DIGESTS.replace("  ", " *"),
    "a-path": DIGESTS.replace(f"  {CI_KERNEL_FILE}", f"  ./{CI_KERNEL_FILE}"),
    "empty": "",
}


@pytest.mark.parametrize("text", BAD_DIGESTS.values(), ids=BAD_DIGESTS)
def test_a_digest_file_of_another_form_fails(tmp_path: Path, text: str) -> None:
    """With no digest, no other file is checked."""
    checks = check_image(_tree(tmp_path, digests_sha256=text.encode("ascii")))

    assert checks[0] == FileCheck(CI_DIGESTS_FILE, False, NOT_DIGESTS)
    assert [(one.ok, one.detail) for one in checks[1:]] == [(False, UNCHECKED)] * 3


def test_a_digest_file_that_is_no_ascii_fails(tmp_path: Path) -> None:
    checks = check_image(_tree(tmp_path, digests_sha256="é\n".encode()))

    assert checks[0] == FileCheck(CI_DIGESTS_FILE, False, NOT_DIGESTS)


# ---- 4. the entries ----------------------------------------------------------------


def test_a_file_past_its_limit_fails(tmp_path: Path) -> None:
    padded = DIGESTS.encode("ascii") + b" " * CI_DIGESTS_MAX_BYTES

    checks = check_image(_tree(tmp_path, digests_sha256=padded))

    assert checks[0] == FileCheck(CI_DIGESTS_FILE, False, TOO_LARGE)


def test_a_link_fails(tmp_path: Path) -> None:
    """Root installs regular files. A link is no file of the tree."""
    tree = _tree(tmp_path)
    (tree / CI_KERNEL_FILE).unlink()
    (tree / CI_KERNEL_FILE).symlink_to(tree / CI_INITRD_FILE)

    checks = _by_file(check_image(tree))

    assert checks[CI_KERNEL_FILE] == (False, NOT_A_FILE)


def test_an_absent_file_fails(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / CI_ROOT_FILE).unlink()

    checks = _by_file(check_image(tree))

    assert checks[CI_ROOT_FILE] == (False, NOT_A_FILE)


def test_a_directory_in_place_of_a_file_fails(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / CI_INITRD_FILE).unlink()
    (tree / CI_INITRD_FILE).mkdir()

    checks = _by_file(check_image(tree))

    assert checks[CI_INITRD_FILE] == (False, NOT_A_FILE)


def test_a_fifo_in_place_of_a_file_fails_at_once(tmp_path: Path) -> None:
    """An open of a FIFO waits for a writer. The hook opens with no wait."""
    tree = _tree(tmp_path)
    (tree / CI_KERNEL_FILE).unlink()
    os.mkfifo(tree / CI_KERNEL_FILE)

    checks = _by_file(check_image(tree))

    assert checks[CI_KERNEL_FILE] == (False, NOT_A_FILE)


def test_a_tree_that_is_a_link_fails_each_check(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(tree)

    checks = check_image(link)

    assert checks[0] == FileCheck(CI_DIGESTS_FILE, False, NOT_A_FILE)
    assert not any(one.ok for one in checks)


def test_a_relative_tree_is_a_usage_mistake(capsys: pytest.CaptureFixture[str]) -> None:
    """The hook inherits the working directory of the executor."""
    with pytest.raises(SystemExit) as caught:
        main(["check-ci-image", "--tree", "ci-runner-image"])

    assert caught.value.code == EXIT_USAGE
    assert "expected an absolute path" in capsys.readouterr().err


# ---- 5. the hook of the manifest -----------------------------------------------


def test_the_hook_of_the_manifest_is_this_command_of_the_handover_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verify program sits in the tree of `handover`, which the
    `depends_on` of the row names (contract 06 §4 rule 6). The command of
    the manifest passes on a good tree in place of its install path."""
    manifests = discover([REPO_ROOT]).manifests()
    manifest = manifests[CI_IMAGE]
    holder = hook_holder(manifest, manifests)
    tree = _tree(tmp_path)
    words = [str(tree) if one == manifest.install.to else one for one in manifest.verify.command]

    assert manifest.depends_on == ("handover",)
    assert holder is not None and holder.name == "handover"
    assert manifest.verify.command[0] == f"{holder.install.to}/bin/handover"
    assert str(tree) in words
    assert main(words[1:]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["ok"] is True
