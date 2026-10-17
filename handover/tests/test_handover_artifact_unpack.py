"""The unpack of the archive of a binary component, and its member list.

An archive of a binary component holds regular files named
`<component>/bin/<program>`, and nothing else. `extract_tar` with a
component writes each one to `<into>/bin/<program>` with the mode 0755.
`list_component_archive` reads the same member headers with the same checks
and writes no file. Each test here gives both functions one archive. The
unpack of an MCP asset keeps its own rules, and
`test_handover_r7f_mcp_build.py` holds them.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from handover.catalog import ARCHIVE_MAX_MEMBERS, ARCHIVE_MAX_UNPACKED_BYTES
from handover.executor.unpack import (
    ASSET_MAX_MEMBERS,
    ASSET_MAX_UNPACKED_BYTES,
    UnpackFault,
    UnpackRefused,
    extract_tar,
    list_component_archive,
)

COMPONENT = "noticeboard"

#: What a compiled program starts with. No test runs one.
PROGRAM = b"\x7fELF\x02\x01\x01\x00 a compiled program\n"

Make = Callable[[tarfile.TarFile], None]


def _member(tar: tarfile.TarFile, name: str, mode: int = 0o755, body: bytes = PROGRAM) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    info.mode = mode
    tar.addfile(info, io.BytesIO(body))


def _special(tar: tarfile.TarFile, name: str, kind: bytes, linkname: str = "") -> None:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    tar.addfile(info)


def _archive(tmp_path: Path, make: Make, mode: str = "w:gz") -> Path:
    path = tmp_path / "archive" / f"{COMPONENT}.tar.gz"
    path.parent.mkdir()
    with tarfile.open(path, mode) as tar:
        make(tar)

    return path


def _program(name: str) -> Make:
    return lambda tar: _member(tar, f"{COMPONENT}/bin/{name}")


def _refusal(tmp_path: Path, make: Make) -> tuple[UnpackFault, UnpackFault, list[Path]]:
    """What the unpack and the list each refuse `make` with, and each file
    that the list left in the directory of the archive."""
    archive = _archive(tmp_path, make)
    with pytest.raises(UnpackRefused) as unpacked:
        extract_tar(archive, tmp_path / "into", COMPONENT)

    with pytest.raises(UnpackRefused) as listed:
        list_component_archive(archive, COMPONENT)

    left = sorted(archive.parent.iterdir())

    return unpacked.value.fault, listed.value.fault, left


def _both_refuse(tmp_path: Path, make: Make, fault: UnpackFault) -> None:
    unpacked, listed, left = _refusal(tmp_path, make)

    assert unpacked is fault
    assert listed is fault
    assert [one.name for one in left] == [f"{COMPONENT}.tar.gz"]


# -- a good archive ----------------------------------------------------------


def _good(tar: tarfile.TarFile) -> None:
    _member(tar, f"{COMPONENT}/bin/{COMPONENT}")
    _member(tar, f"{COMPONENT}/bin/{COMPONENT}-verify", mode=0o700)


def test_a_good_archive_unpacks_into_bin(tmp_path: Path) -> None:
    into = tmp_path / "into"

    extract_tar(_archive(tmp_path, _good), into, COMPONENT)

    assert sorted(one.name for one in into.iterdir()) == ["bin"]
    assert sorted(one.name for one in (into / "bin").iterdir()) == [
        COMPONENT,
        f"{COMPONENT}-verify",
    ]
    assert (into / "bin" / COMPONENT).read_bytes() == PROGRAM


def test_each_program_gets_the_mode_0755(tmp_path: Path) -> None:
    """The member of the verify hook says 0700. The unpack sets 0755."""
    into = tmp_path / "into"

    extract_tar(_archive(tmp_path, _good), into, COMPONENT)

    for program in (into / "bin").iterdir():
        assert program.stat().st_mode & 0o7777 == 0o755


def test_the_list_gives_the_programs_in_the_order_of_the_archive(tmp_path: Path) -> None:
    def make(tar: tarfile.TarFile) -> None:
        for name in ("zeta", "alpha", "mid"):
            _member(tar, f"{COMPONENT}/bin/{name}")

    archive = _archive(tmp_path, make)

    assert list_component_archive(archive, COMPONENT) == ("zeta", "alpha", "mid")


def test_the_list_writes_no_file(tmp_path: Path) -> None:
    archive = _archive(tmp_path, _good)

    list_component_archive(archive, COMPONENT)

    assert [one.name for one in archive.parent.iterdir()] == [archive.name]
    assert sorted(one.name for one in tmp_path.iterdir()) == ["archive"]


def test_an_archive_of_64_members_passes(tmp_path: Path) -> None:
    """The limit is inclusive."""

    def make(tar: tarfile.TarFile) -> None:
        for index in range(ARCHIVE_MAX_MEMBERS):
            _member(tar, f"{COMPONENT}/bin/p{index:02d}", body=b"x")

    archive = _archive(tmp_path, make)

    assert len(list_component_archive(archive, COMPONENT)) == ARCHIVE_MAX_MEMBERS
    extract_tar(archive, tmp_path / "into", COMPONENT)
    assert len(list((tmp_path / "into" / "bin").iterdir())) == ARCHIVE_MAX_MEMBERS


# -- the names of a component archive --------------------------------------------


def test_a_member_of_another_component_is_refused(tmp_path: Path) -> None:
    """The archive of another component of the same commit has a valid
    signature too. Its names tell it apart."""
    _both_refuse(
        tmp_path,
        lambda tar: _member(tar, "caregiver/bin/caregiver"),
        UnpackFault.NOT_A_PROGRAM,
    )


@pytest.mark.parametrize(
    "name",
    [
        f"{COMPONENT}/lib/{COMPONENT}",
        f"{COMPONENT}/{COMPONENT}",
        f"{COMPONENT}/bin/sub/{COMPONENT}",
        f"bin/{COMPONENT}",
        COMPONENT,
        f"./{COMPONENT}/bin/{COMPONENT}",
        f"{COMPONENT}//bin/{COMPONENT}",
        f"{COMPONENT}/bin/.",
        f"{COMPONENT}/bin/a program",
    ],
)
def test_a_member_outside_bin_is_refused(tmp_path: Path, name: str) -> None:
    _both_refuse(tmp_path, lambda tar: _member(tar, name), UnpackFault.NOT_A_PROGRAM)


def test_a_directory_member_is_refused(tmp_path: Path) -> None:
    def make(tar: tarfile.TarFile) -> None:
        _special(tar, f"{COMPONENT}/bin", tarfile.DIRTYPE)
        _program(COMPONENT)(tar)

    _both_refuse(tmp_path, make, UnpackFault.DIRECTORY)


def test_two_members_with_one_name_are_refused(tmp_path: Path) -> None:
    def make(tar: tarfile.TarFile) -> None:
        _program(COMPONENT)(tar)
        _member(tar, f"{COMPONENT}/bin/{COMPONENT}", body=b"a second program\n")

    _both_refuse(tmp_path, make, UnpackFault.SAME_NAME)


def test_the_second_member_of_one_name_writes_nothing_over_the_first(tmp_path: Path) -> None:
    def make(tar: tarfile.TarFile) -> None:
        _program(COMPONENT)(tar)
        _member(tar, f"{COMPONENT}/bin/{COMPONENT}", body=b"a second program\n")

    into = tmp_path / "into"
    with pytest.raises(UnpackRefused):
        extract_tar(_archive(tmp_path, make), into, COMPONENT)

    assert (into / "bin" / COMPONENT).read_bytes() == PROGRAM


# -- the five shapes -------------------------------------------------------------


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_a_link_is_refused(tmp_path: Path, kind: bytes) -> None:
    _both_refuse(
        tmp_path,
        lambda tar: _special(tar, f"{COMPONENT}/bin/{COMPONENT}", kind, "/etc/shadow"),
        UnpackFault.LINK,
    )


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_a_device_is_refused(tmp_path: Path, kind: bytes) -> None:
    _both_refuse(
        tmp_path,
        lambda tar: _special(tar, f"{COMPONENT}/bin/{COMPONENT}", kind),
        UnpackFault.NOT_FILE_OR_DIRECTORY,
    )


@pytest.mark.parametrize("mode", [0o4755, 0o2755])
def test_a_set_id_bit_is_refused(tmp_path: Path, mode: int) -> None:
    _both_refuse(
        tmp_path,
        lambda tar: _member(tar, f"{COMPONENT}/bin/{COMPONENT}", mode=mode),
        UnpackFault.SET_ID,
    )


def test_an_absolute_name_is_refused(tmp_path: Path) -> None:
    _both_refuse(
        tmp_path,
        lambda tar: _member(tar, f"/{COMPONENT}/bin/{COMPONENT}"),
        UnpackFault.LEAVES_TREE,
    )


@pytest.mark.parametrize(
    "name", [f"{COMPONENT}/bin/../{COMPONENT}", f"../{COMPONENT}/bin/{COMPONENT}"]
)
def test_a_dot_dot_segment_is_refused(tmp_path: Path, name: str) -> None:
    _both_refuse(tmp_path, lambda tar: _member(tar, name), UnpackFault.LEAVES_TREE)


# -- the two limits --------------------------------------------------------------


def test_65_members_are_refused(tmp_path: Path) -> None:
    def make(tar: tarfile.TarFile) -> None:
        for index in range(ARCHIVE_MAX_MEMBERS + 1):
            _member(tar, f"{COMPONENT}/bin/p{index:02d}", body=b"x")

    _both_refuse(tmp_path, make, UnpackFault.ARCHIVE_MEMBERS)


def _header(name: str, size: int) -> bytes:
    """One member header that claims `size` bytes. The limit is checked on
    the header, before a byte of the member is read."""
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o755

    return info.tobuf(format=tarfile.PAX_FORMAT)


def _claims(tmp_path: Path, *raw: bytes) -> Path:
    archive = tmp_path / "archive" / f"{COMPONENT}.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(gzip.compress(b"".join(raw)))

    return archive


def test_more_bytes_than_the_limit_are_refused(tmp_path: Path) -> None:
    archive = _claims(
        tmp_path, _header(f"{COMPONENT}/bin/{COMPONENT}", ARCHIVE_MAX_UNPACKED_BYTES + 1)
    )
    into = tmp_path / "into"

    with pytest.raises(UnpackRefused) as unpacked:
        extract_tar(archive, into, COMPONENT)

    with pytest.raises(UnpackRefused) as listed:
        list_component_archive(archive, COMPONENT)

    assert unpacked.value.fault is UnpackFault.ARCHIVE_BYTES
    assert listed.value.fault is UnpackFault.ARCHIVE_BYTES
    assert not (into / "bin").exists()


def test_the_bytes_of_each_member_count(tmp_path: Path) -> None:
    """One byte, then a member of exactly the limit: the sum passes it. The
    first member fits, so the unpack writes it."""
    first = _header(f"{COMPONENT}/bin/small", 1) + b"x".ljust(tarfile.BLOCKSIZE, b"\0")
    archive = _claims(tmp_path, first, _header(f"{COMPONENT}/bin/big", ARCHIVE_MAX_UNPACKED_BYTES))
    into = tmp_path / "into"

    with pytest.raises(UnpackRefused) as unpacked:
        extract_tar(archive, into, COMPONENT)

    with pytest.raises(UnpackRefused) as listed:
        list_component_archive(archive, COMPONENT)

    assert unpacked.value.fault is UnpackFault.ARCHIVE_BYTES
    assert listed.value.fault is UnpackFault.ARCHIVE_BYTES
    assert (into / "bin" / "small").read_bytes() == b"x"
    assert not (into / "bin" / "big").exists()


# -- the archive itself --------------------------------------------------------


def test_a_tar_that_is_no_gzip_is_refused(tmp_path: Path) -> None:
    archive = _archive(tmp_path, _good, mode="w")

    with pytest.raises(UnpackRefused) as unpacked:
        extract_tar(archive, tmp_path / "into", COMPONENT)

    with pytest.raises(UnpackRefused) as listed:
        list_component_archive(archive, COMPONENT)

    assert unpacked.value.fault is UnpackFault.NOT_READABLE
    assert listed.value.fault is UnpackFault.NOT_READABLE


def test_bytes_that_are_no_archive_are_refused(tmp_path: Path) -> None:
    archive = tmp_path / f"{COMPONENT}.tar.gz"
    archive.write_bytes(b"this is no archive\n")

    with pytest.raises(UnpackRefused) as listed:
        list_component_archive(archive, COMPONENT)

    assert listed.value.fault is UnpackFault.NOT_READABLE


def test_each_detail_is_a_fixed_text(tmp_path: Path) -> None:
    """The detail names the rule and no byte of the archive."""
    archive = _archive(tmp_path, lambda tar: _member(tar, "secret-name/bin/secret-program"))

    with pytest.raises(UnpackRefused) as listed:
        list_component_archive(archive, COMPONENT)

    assert listed.value.detail == str(UnpackFault.NOT_A_PROGRAM)
    assert "secret" not in listed.value.detail


def test_the_limits_of_an_mcp_asset_stay() -> None:
    assert ASSET_MAX_MEMBERS == 20_000
    assert ASSET_MAX_UNPACKED_BYTES == 512 * 1024 * 1024
    assert str(UnpackFault.ASSET_MEMBERS) == "the asset holds more than 20000 members"
    assert str(UnpackFault.ASSET_BYTES) == "the asset unpacks to more than 536870912"
