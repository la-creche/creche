"""Each copy of a copied module has the same bytes.

A copied module needs the standard library only, and no package imports it
from another package. Each package that needs the module holds its own copy,
at `<package>/src/<module>/<name>`. `COPIED` lists the name of each such
module.

Two copies that differ are two rules, and nothing in one package shows the
difference. A scoped run for one package does not show it either. So the
rule has a test here, and CI runs it for each change.

The test passes for a name with one copy and for a name with no copy.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The name of each module that a package holds as a copy.
COPIED = ("strictjson.py", "notices.py")


def copies(root: Path, name: str) -> list[Path]:
    """Each file `<package>/src/<module>/<name>` under `root`, in sorted order.

    A directory whose name starts with a dot is no package, for example the
    directory of a virtual environment.
    """
    found = root.glob(f"*/src/*/{name}")

    return sorted(path for path in found if not path.relative_to(root).parts[0].startswith("."))


def differing(root: Path, name: str) -> list[str]:
    """Each copy of `name` whose bytes are not the bytes of the first copy."""
    found = copies(root, name)
    if not found:
        return []

    first = found[0].read_bytes()

    return [str(path.relative_to(root)) for path in found[1:] if path.read_bytes() != first]


@pytest.mark.parametrize("name", COPIED)
def test_each_copy_in_the_repository_has_the_bytes_of_the_first_copy(name: str) -> None:
    assert differing(REPO_ROOT, name) == [], f"make each copy of {name} the same file"


def _write(root: Path, package: str, name: str, data: bytes) -> None:
    path = root / package / "src" / package.replace("-", "_") / name
    path.parent.mkdir(parents=True)
    path.write_bytes(data)


@pytest.mark.parametrize("name", COPIED)
def test_two_copies_that_differ_in_one_byte_fail(tmp_path: Path, name: str) -> None:
    _write(tmp_path, "attendance", name, b"RULE = 64\n")
    _write(tmp_path, "caregiver", name, b"RULE = 65\n")
    _write(tmp_path, "noticeboard", name, b"RULE = 64\n")

    assert differing(tmp_path, name) == [f"caregiver/src/caregiver/{name}"]


@pytest.mark.parametrize("name", COPIED)
def test_a_copy_with_one_byte_more_fails(tmp_path: Path, name: str) -> None:
    _write(tmp_path, "door-owui", name, b"RULE = 64\n")
    _write(tmp_path, "noticeboard", name, b"RULE = 64\n\n")

    assert differing(tmp_path, name) == [f"noticeboard/src/noticeboard/{name}"]


@pytest.mark.parametrize("name", COPIED)
def test_copies_with_the_same_bytes_pass(tmp_path: Path, name: str) -> None:
    for package in ("attendance", "door-owui", "noticeboard"):
        _write(tmp_path, package, name, b"RULE = 64\n")

    assert len(copies(tmp_path, name)) == 3
    assert differing(tmp_path, name) == []


@pytest.mark.parametrize("name", COPIED)
def test_one_copy_passes_and_no_copy_passes(tmp_path: Path, name: str) -> None:
    assert differing(tmp_path, name) == []

    _write(tmp_path, "noticeboard", name, b"RULE = 64\n")

    assert len(copies(tmp_path, name)) == 1
    assert differing(tmp_path, name) == []


def test_a_copy_of_one_name_is_no_copy_of_the_other_name(tmp_path: Path) -> None:
    _write(tmp_path, "attendance", "strictjson.py", b"RULE = 64\n")
    _write(tmp_path, "noticeboard", "notices.py", b"RULE = 65\n")

    assert [differing(tmp_path, name) for name in COPIED] == [[], []]


def test_a_file_in_another_place_is_no_copy(tmp_path: Path) -> None:
    name = COPIED[0]
    _write(tmp_path, "noticeboard", name, b"RULE = 64\n")
    for other in (
        tmp_path / "noticeboard" / "tests" / name,
        tmp_path / "noticeboard" / "src" / name,
        tmp_path / "noticeboard" / "src" / "noticeboard" / "deeper" / name,
        tmp_path / ".venv" / "src" / "noticeboard" / name,
        tmp_path / name,
    ):
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"RULE = 65\n")

    assert [str(path.relative_to(tmp_path)) for path in copies(tmp_path, name)] == [
        f"noticeboard/src/noticeboard/{name}"
    ]
    assert differing(tmp_path, name) == []
