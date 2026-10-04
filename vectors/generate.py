"""Write the vector files, or check that the committed ones are current.

    uv run python -m vectors.generate            write vectors/data/
    uv run python -m vectors.generate --check    exit 1 when a file differs
    uv run python -m vectors.generate --counts   print the vectors per surface

Run it from the repository root. The Python implementation is the
authority: this program imports the product packages from the workspace and
records what they do (`vectors/README.md`).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from vectors.core import FORMAT, Json, Surface, compact, render
from vectors.surfaces import ids

DATA_DIR: Final = Path(__file__).resolve().parent / "data"

#: The list of every surface, written beside the vector files.
INDEX_FILE: Final = "index.json"
DISAGREEMENTS_FILE: Final = "ids/disagreements.json"

EXIT_OK: Final = 0
EXIT_STALE: Final = 1

#: One group of surfaces: a name, and the function that builds its files.
type Group = Callable[[], dict[str, str]]


def _files_of(surfaces: tuple[Surface, ...]) -> dict[str, str]:
    files: dict[str, str] = {}
    for surface in surfaces:
        if surface.path in files:
            raise ValueError(f"two surfaces write {surface.path}")

        files[surface.path] = render(surface)

    return files


def _ids_group() -> tuple[Surface, ...]:
    return ids.surfaces()


#: Every group, in the order of the index. A test asks for one group by name.
GROUPS: Final[dict[str, Callable[[], tuple[Surface, ...]]]] = {
    "ids": _ids_group,
}


def _extra_files(name: str, surfaces: tuple[Surface, ...]) -> dict[str, str]:
    """Files of a group that are not vector files."""
    if name == "ids":
        return {DISAGREEMENTS_FILE: ids.render_disagreements(surfaces)}

    return {}


def build_group(name: str) -> tuple[tuple[Surface, ...], dict[str, str]]:
    """The surfaces of one group, and every file the group writes."""
    surfaces = GROUPS[name]()
    files = _files_of(surfaces)
    files.update(_extra_files(name, surfaces))

    return surfaces, files


def render_index(surfaces: Sequence[Surface]) -> str:
    """The bytes of `index.json`: one row per surface, in build order."""
    rows: list[str] = []
    for surface in surfaces:
        row: dict[str, Json] = {
            "surface": surface.name,
            "path": surface.path,
            "entry": surface.entry,
            "vectors": len(surface.vectors),
            **surface.tally(),
        }
        rows.append(f"  {compact(row)}")

    body = "[\n" + ",\n".join(rows) + "\n ]" if rows else "[]"

    return f'{{\n "format": {FORMAT},\n "kind": "index",\n "surfaces": {body}\n}}\n'


def build() -> tuple[tuple[Surface, ...], dict[str, str]]:
    """Every surface, and every file under `vectors/data/` by relative path."""
    surfaces: list[Surface] = []
    files: dict[str, str] = {}
    for name in GROUPS:
        built, group_files = build_group(name)
        clash = files.keys() & group_files.keys()
        if clash:
            raise ValueError(f"two groups write {sorted(clash)}")

        surfaces.extend(built)
        files.update(group_files)

    names = [surface.name for surface in surfaces]
    if len(names) != len(set(names)):
        raise ValueError("two surfaces share one name")

    files[INDEX_FILE] = render_index(surfaces)

    return tuple(surfaces), files


def committed(root: Path = DATA_DIR) -> dict[str, str]:
    """Every file under `vectors/data/` as it is on disk, by relative path."""
    if not root.is_dir():
        return {}

    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = path.read_bytes().decode("utf-8")

    return found


def stale(files: dict[str, str], on_disk: dict[str, str]) -> list[str]:
    """One line per file that is missing, different or left over."""
    problems = [f"missing: {path}" for path in sorted(files.keys() - on_disk.keys())]
    problems += [f"left over: {path}" for path in sorted(on_disk.keys() - files.keys())]
    problems += [
        f"differs: {path}"
        for path in sorted(files.keys() & on_disk.keys())
        if files[path] != on_disk[path]
    ]

    return problems


def write(files: dict[str, str], root: Path = DATA_DIR) -> None:
    """Make `root` hold exactly `files`."""
    for path in sorted(committed(root).keys() - files.keys()):
        (root / path).unlink()

    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


def _print_counts(surfaces: Sequence[Surface]) -> None:
    for surface in surfaces:
        tally = surface.tally()
        parts = ", ".join(f"{count} {result}" for result, count in tally.items() if count)
        print(f"{surface.name}: {len(surface.vectors)} ({parts})")

    print(f"total: {sum(len(surface.vectors) for surface in surfaces)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vectors.generate", description=__doc__)
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when stale")
    parser.add_argument("--counts", action="store_true", help="print the vectors per surface")
    args = parser.parse_args(argv)

    surfaces, files = build()
    if args.counts:
        _print_counts(surfaces)
        return EXIT_OK

    if args.check:
        problems = stale(files, committed())
        for problem in problems:
            print(problem, file=sys.stderr)

        return EXIT_STALE if problems else EXIT_OK

    write(files)
    print(f"wrote {len(files)} files, {sum(len(one.vectors) for one in surfaces)} vectors")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
