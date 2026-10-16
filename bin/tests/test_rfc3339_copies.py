"""Each copy of `rfc3339.py` has the same bytes.

`rfc3339.py` is the one reader of a time text. It needs the standard library
only, and no package imports it from another package. Each package that reads
a time holds its own copy, at `<package>/src/<module>/rfc3339.py`.

Two copies that differ are two grammars for one time text. A scoped run for
one package does not show the difference. So the rule has a test here, and CI
runs it for each change.

`test_strictjson_copies.py` holds the search for the copies of a name and the
comparison of their bytes. This file gives it the name of the time reader.
"""

from pathlib import Path

from test_strictjson_copies import copies, differing

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The name of the copied module.
NAME = "rfc3339.py"

#: The first copy. Each other copy has its bytes.
FIRST_COPY = f"noticeboard/src/noticeboard/{NAME}"


def _write(root: Path, package: str, data: bytes) -> None:
    path = root / package / "src" / package.replace("-", "_") / NAME
    path.parent.mkdir(parents=True)
    path.write_bytes(data)


def test_each_copy_in_the_repository_has_the_bytes_of_the_first_copy() -> None:
    assert differing(REPO_ROOT, NAME) == [], f"make each copy of {NAME} the same file"


def test_the_search_finds_the_first_copy() -> None:
    found = [str(path.relative_to(REPO_ROOT)) for path in copies(REPO_ROOT, NAME)]

    assert FIRST_COPY in found


def test_two_copies_that_differ_in_one_byte_fail(tmp_path: Path) -> None:
    _write(tmp_path, "attendance", b'OFFSET = "+00:00"\n')
    _write(tmp_path, "door-trigger", b'OFFSET = "+00:01"\n')
    _write(tmp_path, "noticeboard", b'OFFSET = "+00:00"\n')

    assert differing(tmp_path, NAME) == [f"door-trigger/src/door_trigger/{NAME}"]


def test_one_copy_passes(tmp_path: Path) -> None:
    _write(tmp_path, "noticeboard", b'OFFSET = "+00:00"\n')

    assert len(copies(tmp_path, NAME)) == 1
    assert differing(tmp_path, NAME) == []


def test_copies_with_the_same_bytes_pass(tmp_path: Path) -> None:
    for package in ("caregiver", "door-tui", "noticeboard"):
        _write(tmp_path, package, b'OFFSET = "+00:00"\n')

    assert len(copies(tmp_path, NAME)) == 3
    assert differing(tmp_path, NAME) == []
