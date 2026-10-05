"""Write the vector files, or check that the committed ones are current.

    uv run python -m vectors.generate                 write vectors/data/
    uv run python -m vectors.generate --check         exit 1 when a file differs
    uv run python -m vectors.generate --counts        print the vectors per surface
    uv run python -m vectors.generate --freeze PATH   freeze a file that no group writes

Run it from the repository root. The Python implementation is the
authority: this program imports the product packages from the workspace and
records what they do (`vectors/README.md`).

A frozen file is a file under `vectors/data/` whose Python origin left this
repository. The index holds the SHA-256 of each one. This program writes no
frozen file and removes none (`vectors/README.md`, "A frozen file").
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast

from vectors.core import ACCEPTED, FORMAT, RAISED, REFUSED, Json, Surface, compact, render
from vectors.surfaces import (
    audit,
    channel,
    classify,
    config,
    family_cli,
    family_file,
    grants,
    ids,
    manifest,
    runtime,
    server_file,
    session,
    status,
    status_files,
)

DATA_DIR: Final = Path(__file__).resolve().parent / "data"

#: The list of every surface, written beside the vector files.
INDEX_FILE: Final = "index.json"
#: The suffix of every file that the generator writes.
JSON_SUFFIX: Final = ".json"
DISAGREEMENTS_FILE: Final = "ids/disagreements.json"

#: The key of the index that holds the frozen files: each path under
#: `vectors/data/`, with the SHA-256 of the bytes of that file.
FROZEN_KEY: Final = "frozen"
#: A digest in the index: 64 hexadecimal digits in lower case.
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
#: A tree with no frozen file.
_NOTHING_FROZEN: Final[Mapping[str, str]] = MappingProxyType({})
#: What to do with an index that does not give the frozen files.
RESTORE_INDEX: Final = f"take {INDEX_FILE} of the base branch, then run the generator again"

EXIT_OK: Final = 0
EXIT_STALE: Final = 1


def _files_of(surfaces: tuple[Surface, ...]) -> dict[str, str]:
    files: dict[str, str] = {}
    for surface in surfaces:
        if surface.path in files:
            raise ValueError(f"two surfaces write {surface.path}")

        files[surface.path] = render(surface)

    return files


#: Every group, in the order of the index.
GROUPS: Final[dict[str, Callable[[], tuple[Surface, ...]]]] = {
    "ids": ids.surfaces,
    "family_file": family_file.surfaces,
    "server_file": server_file.surfaces,
    "classify": classify.surfaces,
    "family_cli": family_cli.surfaces,
    "channel": channel.surfaces,
    "chaperone": grants.surfaces,
    "audit": audit.surfaces,
    "status": status.surfaces,
    "status_files": status_files.surfaces,
    "config": config.surfaces,
    "manifest": manifest.surfaces,
    "session": session.surfaces,
    "runtime": runtime.surfaces,
}


def _extra_files(name: str, surfaces: tuple[Surface, ...]) -> dict[str, str]:
    """Files of a group that are not vector files."""
    if name == "ids":
        return {DISAGREEMENTS_FILE: ids.render_disagreements(surfaces)}

    if name == "family_file":
        return {family_file.REGISTRIES_FILE: family_file.render_registries()}

    return {}


def build_group(name: str) -> tuple[tuple[Surface, ...], dict[str, str]]:
    """The surfaces of one group, and every file the group writes."""
    surfaces = GROUPS[name]()
    files = _files_of(surfaces)
    files.update(_extra_files(name, surfaces))

    return surfaces, files


def _digest(text: str) -> str:
    """The SHA-256 of a file that `committed` read, as the index holds it.

    A frozen file is ASCII, so the UTF-8 bytes of its text are its bytes. A
    file that is not UTF-8 has another text, and its digest differs.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_data_path(path: str) -> bool:
    """Whether `committed` can give `path`: a JSON file below the root."""
    pure = PurePosixPath(path)

    return (
        pure.as_posix() == path
        and not pure.is_absolute()
        and ".." not in pure.parts
        and pure.suffix == JSON_SUFFIX
    )


def _members(value: object, name: str) -> dict[str, object]:
    """The members of the JSON object `value`. `name` says where it is."""
    if not isinstance(value, dict):
        raise ValueError(f"{name} is no JSON object")

    return cast("dict[str, object]", value)


def _document(text: str, path: str) -> dict[str, object]:
    """The members of the JSON object that is the text of the file `path`."""
    try:
        value: object = json.loads(text)
    except ValueError as error:
        raise ValueError(f"{path} is no JSON") from error

    return _members(value, path)


def frozen_of(on_disk: Mapping[str, str]) -> dict[str, str]:
    """The frozen files that the index of `on_disk` names: path to digest.

    The index is the one home of that map. The generator reads the map
    there and writes it again as it is. A tree with no JSON file has no
    frozen file. The generator stops on each other tree that does not give
    the map: with a wrong map, `write` removes a frozen file as a left over
    file, and no group can write it again.
    """
    if not on_disk:
        return {}

    text = on_disk.get(INDEX_FILE)
    if text is None:
        raise ValueError(f"{INDEX_FILE} is absent; {RESTORE_INDEX}")

    try:
        return _frozen_map(_document(text, INDEX_FILE))
    except ValueError as error:
        raise ValueError(f"{error}; {RESTORE_INDEX}") from error


def _frozen_map(index: Mapping[str, object]) -> dict[str, str]:
    """The map of the frozen files in the members of an index, checked."""
    if FROZEN_KEY not in index:
        raise ValueError(f"{INDEX_FILE} has no `{FROZEN_KEY}` map")

    frozen: dict[str, str] = {}
    for path, digest in _members(index[FROZEN_KEY], f"`{FROZEN_KEY}` of {INDEX_FILE}").items():
        if path == INDEX_FILE or not _is_data_path(path):
            raise ValueError(f"{INDEX_FILE}: {path!r} is no path of a frozen file")

        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError(f"{INDEX_FILE}: the digest of {path} is not 64 hexadecimal digits")

        frozen[path] = digest

    return frozen


def _freeze(
    paths: Sequence[str],
    frozen: Mapping[str, str],
    files: Mapping[str, str],
    on_disk: Mapping[str, str],
) -> dict[str, str]:
    """`frozen` and each of `paths`, with the digest of the file on disk.

    A path is relative to `vectors/data/`. `files` holds what the groups
    write. A file freezes one time, when no group writes it.
    """
    added = dict(frozen)
    for path in paths:
        if path in added:
            raise ValueError(f"{path} is frozen already")

        if path in files or path == INDEX_FILE:
            raise ValueError(f"the generator writes {path}; remove its surface first")

        text = on_disk.get(path)
        if text is None:
            raise ValueError(f"no JSON file {path}; give the path from vectors/data/")

        if not text.isascii():
            raise ValueError(f"{path} is not ASCII, so the generator did not write it")

        # The call makes no row here. It stops on a file that can have none.
        _frozen_row(path, text)
        added[path] = _digest(text)

    return added


def index_row(surface: Surface) -> dict[str, Json]:
    """The index row of a surface that a group built."""
    return {
        "surface": surface.name,
        "path": surface.path,
        "entry": surface.entry,
        "vectors": len(surface.vectors),
        **surface.tally(),
    }


def _frozen_row(path: str, text: str) -> dict[str, Json] | None:
    """The index row of a frozen file, from the file itself.

    A vector file has no `kind`. Each other file of the generator has one,
    for example the registry file, and the index gives it no row: `None`.
    """
    document = _document(text, path)
    if "kind" in document:
        return None

    name, entry, held = document.get("surface"), document.get("entry"), document.get("vectors")
    if (
        document.get("format") != FORMAT
        or not isinstance(name, str)
        or not isinstance(entry, str)
        or not isinstance(held, list)
    ):
        raise ValueError(f"{path} is no vector file of format {FORMAT}")

    vectors = cast("list[object]", held)
    counts = {ACCEPTED: 0, REFUSED: 0, RAISED: 0}
    for vector in vectors:
        result = _members(vector, f"a vector of {path}").get("result")
        if not isinstance(result, str) or result not in counts:
            raise ValueError(f"{path}: {result!r} is no result of a vector")

        counts[result] += 1

    return {"surface": name, "path": path, "entry": entry, "vectors": len(vectors), **counts}


def index_rows(
    surfaces: Sequence[Surface],
    files: Mapping[str, str],
    frozen: Mapping[str, str],
    on_disk: Mapping[str, str],
) -> list[dict[str, Json]]:
    """One row per surface: each built one in build order, then each frozen one by path.

    `files` holds what the groups write. A frozen file that is missing or
    that differs from its digest gets no row here. `stale` reports it.
    """
    clash = files.keys() & frozen.keys()
    if clash:
        raise ValueError(f"a group writes the frozen files {sorted(clash)}")

    rows = [index_row(surface) for surface in surfaces]
    for path in sorted(frozen):
        text = on_disk.get(path)
        row = (
            _frozen_row(path, text) if text is not None and _digest(text) == frozen[path] else None
        )
        if row is not None:
            rows.append(row)

    names = [row["surface"] for row in rows]
    if len(names) != len(set(names)):
        raise ValueError("a frozen surface and another surface share one name")

    return rows


def render_index(rows: Sequence[dict[str, Json]], frozen: Mapping[str, str]) -> str:
    """The bytes of `index.json`: one row per surface, and one line per frozen file."""
    lines = [f"  {compact(row)}" for row in rows]
    body = "[\n" + ",\n".join(lines) + "\n ]" if lines else "[]"
    pins = [f"  {compact(path)}: {compact(frozen[path])}" for path in sorted(frozen)]
    held = "{\n" + ",\n".join(pins) + "\n }" if pins else "{}"

    return (
        f'{{\n "format": {FORMAT},\n "{FROZEN_KEY}": {held},\n'
        f' "kind": "index",\n "surfaces": {body}\n}}\n'
    )


def _build_groups() -> tuple[tuple[Surface, ...], dict[str, str]]:
    """Every surface that a group builds, and every file of a group by relative path."""
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

    return tuple(surfaces), files


def build(root: Path = DATA_DIR) -> tuple[tuple[Surface, ...], dict[str, str]]:
    """Every built surface, and every file that the generator writes under `root`.

    The files are by relative path. The index is one of them. It holds the
    frozen files that the index under `root` names, and a row for each
    frozen surface. `frozen_of` reads the map of a built index too.
    """
    on_disk = committed(root)
    frozen = frozen_of(on_disk)
    surfaces, files = _build_groups()
    files[INDEX_FILE] = render_index(index_rows(surfaces, files, frozen, on_disk), frozen)

    return surfaces, files


def committed(root: Path = DATA_DIR) -> dict[str, str]:
    """Every JSON file under `vectors/data/` as it is on disk, by relative path.

    Bytes that are not UTF-8 become U+FFFD. No file of the generator holds
    that character, so such a file differs and nothing raises.
    """
    if not root.is_dir():
        return {}

    found: dict[str, str] = {}
    for path in sorted(root.rglob(f"*{JSON_SUFFIX}")):
        if path.is_file() and not path.is_symlink():
            text = path.read_bytes().decode("utf-8", errors="replace")
            found[path.relative_to(root).as_posix()] = text

    return found


def strays(root: Path = DATA_DIR) -> list[str]:
    """Every path under `vectors/data/` that the generator did not write.

    That is a symbolic link, or a file that is not `*.json`. Neither is
    read, so a file of a file browser cannot stop the check.
    """
    if not root.is_dir():
        return []

    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_symlink() or (path.is_file() and path.suffix != JSON_SUFFIX)
    ]


def stale(
    files: Mapping[str, str],
    on_disk: Mapping[str, str],
    stray: Sequence[str] = (),
    frozen: Mapping[str, str] = _NOTHING_FROZEN,
) -> list[str]:
    """One line per file that is missing, different or left over.

    A frozen file is no left over file. It differs when its bytes do not
    have the digest that `frozen` holds.
    """
    wanted = files.keys() | frozen.keys()
    left_over = {*(on_disk.keys() - wanted), *stray}
    problems = [f"missing: {path}" for path in sorted(wanted - on_disk.keys())]
    problems += [f"left over: {path}" for path in sorted(left_over)]
    problems += [
        f"differs: {path}"
        for path in sorted(wanted & on_disk.keys())
        if (
            _digest(on_disk[path]) != frozen[path]
            if path in frozen
            else files[path] != on_disk[path]
        )
    ]

    return problems


def damaged(frozen: Mapping[str, str], on_disk: Mapping[str, str]) -> list[str]:
    """One line per frozen file that is missing or that differs from its digest.

    No group writes such a file again. Restore it from git.
    """
    held = {path: on_disk[path] for path in frozen.keys() & on_disk.keys()}

    return stale({}, held, (), frozen)


def _refuse_link(root: Path, target: Path) -> None:
    """A write through a symbolic link lands outside `vectors/data/`."""
    for path in (target, *target.parents):
        if path == root:
            return

        if path.is_symlink():
            raise ValueError(f"{path} is a symbolic link; remove it")


def write(
    files: Mapping[str, str], root: Path = DATA_DIR, frozen: Mapping[str, str] = _NOTHING_FROZEN
) -> list[str]:
    """Make `root` hold `files`, the frozen files and no other JSON file.

    `strays` stay. A frozen file stays as it is: `write` does not write it
    and does not remove it. The result is each path that `write` removed.
    """
    for path in files:
        if path in frozen:
            raise ValueError(f"{path} is frozen; the generator does not write it")

        _refuse_link(root, root / path)

    removed = sorted(committed(root).keys() - files.keys() - frozen.keys())
    for path in removed:
        (root / path).unlink()

    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    return removed


def _print_counts(rows: Sequence[dict[str, Json]]) -> None:
    total = 0
    for row in rows:
        parts = ", ".join(
            f"{row[result]} {result}" for result in (ACCEPTED, REFUSED, RAISED) if row[result]
        )
        print(f"{row['surface']}: {row['vectors']} ({parts})")
        total += cast("int", row["vectors"])

    print(f"total: {total}")


def _report(problems: Sequence[str]) -> int:
    for problem in problems:
        print(problem, file=sys.stderr)

    return EXIT_STALE if problems else EXIT_OK


def main(argv: Sequence[str] | None = None, root: Path = DATA_DIR) -> int:
    parser = argparse.ArgumentParser(prog="vectors.generate", description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="write nothing; exit 1 when stale")
    mode.add_argument("--counts", action="store_true", help="print the vectors per surface")
    mode.add_argument(
        "--freeze",
        nargs="+",
        default=(),
        metavar="PATH",
        help="freeze each file, by its path from vectors/data/; no group writes it",
    )
    args = parser.parse_args(argv)
    paths = cast("Sequence[str]", args.freeze)

    on_disk = committed(root)
    known = frozen_of(on_disk)
    surfaces, files = _build_groups()
    frozen = _freeze(paths, known, files, on_disk)
    rows = index_rows(surfaces, files, frozen, on_disk)
    files[INDEX_FILE] = render_index(rows, frozen)
    if args.counts:
        _print_counts(rows)
        return EXIT_OK

    if args.check:
        # No group builds a frozen file, so the check has no text to compare
        # it with. It compares the digest and names the file on one line.
        for path in sorted(frozen):
            print(f"frozen: {path}")

        return _report(stale(files, on_disk, strays(root), frozen))

    broken = damaged(frozen, on_disk)
    if broken:
        return _report(broken)

    # A write removes each JSON file with no group and no line in the map. A
    # freeze that names only some of those files must not remove the others.
    unnamed = sorted(on_disk.keys() - files.keys() - frozen.keys()) if paths else []
    if unnamed:
        return _report([f"left over: {path}" for path in unnamed])

    # A removed file had no group and no line in the map of the frozen files.
    for path in write(files, root, frozen):
        print(f"removed: {path}")

    print(f"wrote {len(files)} files, {sum(len(one.vectors) for one in surfaces)} vectors")

    return _report(stale(files, committed(root), strays(root), frozen))


if __name__ == "__main__":
    raise SystemExit(main())
