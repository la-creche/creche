"""Finding manifests on disk, and matching what is found to the catalog.

This is the only module that touches the filesystem for manifests. It reads
and never writes. A repository can be a branch an agent wrote, so the walk
itself is defensive: no symlink is followed, a cap bounds the number of files,
and a manifest is read only after its size passes.

A directory holds `component.toml` or `component.yaml`. The walk and
`read_one` read `component.toml` where the directory holds that name. Else
they read `component.yaml`. The tool refuses a directory that holds both
names, with the code `manifest`, and reads neither file. For that rule, each
entry of a name counts: a regular file, a link, a link to no file and a
directory.

Contract 06 §10's last two rows live here: a catalogued component with no
file, and a file for a name the catalog does not hold.
"""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from .catalog import ARRIVING, CATALOG, CATALOG_BY_NAME, RETIRING, Releases
from .errors import Refusal, RefusalCode, safe_token
from .manifest import (
    EITHER_MANIFEST_NAME,
    MANIFEST_FILENAME,
    MANIFEST_FILENAME_YAML,
    MAX_MANIFEST_BYTES,
    REPO_ROOT_PATH,
    ComponentManifest,
    Markup,
    parse_manifest,
    toml_text_of,
)
from .tomlfile import FileKind

#: Nine components plus room for a mistake. A tree with more than this many
#: manifests is not the repository this tool knows. The cap counts both names.
MAX_MANIFEST_FILES = 32

#: The two names, for the text of a refusal.
_BOTH_NAMES = f"{MANIFEST_FILENAME} and {MANIFEST_FILENAME_YAML}"

#: Deep enough for `playpen/bridge/...`, shallow enough that a crafted tree
#: cannot make the walk expensive.
MAX_WALK_DEPTH = 6

#: Build output, caches and other checkouts. A `component.yaml` under any of
#: these is not a declaration, whatever it says.
SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".claude",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "site-packages",
    }
)


@dataclass(frozen=True)
class FoundManifest:
    """One manifest, and where it was read from."""

    manifest: ComponentManifest
    #: Repo-relative directory that held the file, `.` at a repository root.
    directory: str
    #: The label a refusal names, such as `chaperone/component.toml`.
    subject: str
    #: The markup of the text. The switch stamps the text under the name of
    #: this markup (`live_state.write_manifest_stamp`).
    markup: Markup
    #: The file's own bytes, as read. The switch stamps them into the
    #: artifact so the NEXT release reads this component's interface out
    #: of a tree root owns (`live_state.MANIFEST_STAMP`).
    text: str = ""


@dataclass(frozen=True)
class ManifestSet:
    """Every manifest found under the given roots, keyed by component name."""

    found: dict[str, FoundManifest]
    #: Catalogued components no root held a file for.
    missing: tuple[str, ...]

    def manifests(self) -> dict[str, ComponentManifest]:
        return {name: item.manifest for name, item in self.found.items()}


def _read_manifest_file(path: Path, subject: str, markup: Markup) -> str:
    """Read one manifest, refusing a symlink and anything oversized.

    A TOML file is read up to one byte past the byte limit of
    `FileKind.MANIFEST`, and the TOML reader refuses a larger text with its
    own text. A YAML file keeps `MAX_MANIFEST_BYTES`."""
    if path.is_symlink():
        raise Refusal(RefusalCode.MANIFEST, subject, "is a symlink")

    stat_result = path.stat()
    if not path.is_file():
        raise Refusal(RefusalCode.MANIFEST, subject, "is not a regular file")

    if markup is Markup.TOML:
        with path.open("rb") as handle:
            return toml_text_of(handle.read(FileKind.MANIFEST.byte_limit + 1), subject)

    if stat_result.st_size > MAX_MANIFEST_BYTES:
        raise Refusal(RefusalCode.MANIFEST, subject, f"larger than {MAX_MANIFEST_BYTES} bytes")

    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise Refusal(RefusalCode.MANIFEST, subject, "is not UTF-8") from None


def _entries_of(directory: Path) -> frozenset[str]:
    """The names of a manifest that `directory` has an entry for."""
    names = (MANIFEST_FILENAME, MANIFEST_FILENAME_YAML)

    return frozenset(name for name in names if _holds(directory / name))


def _holds(path: Path) -> bool:
    """Whether a directory has an entry at `path`. A link counts, also a
    link to no file, and the link is not followed."""
    return path.is_symlink() or path.exists()


def _markup_of(entries: Collection[str], subject: str) -> Markup:
    """The markup of the manifest of a directory with these entries: TOML
    when it holds an entry named `component.toml`, else YAML. A directory
    that holds both names is a refusal.

    CONTRACT-QUESTION: contract 06 §10.2 makes a directory that holds both
    names a refusal. It does not say which kind of entry holds a name. Each
    entry counts, also a link, a link to no file and a directory: a stray
    entry of the other name thus refuses the directory. The other reading
    counts only a regular file. A change costs the entries that the walk and
    `_entries_of` give, and one test of `tests/test_handover_manifest_toml.py`."""
    has_toml = MANIFEST_FILENAME in entries
    if has_toml and MANIFEST_FILENAME_YAML in entries:
        raise Refusal(RefusalCode.MANIFEST, subject, f"holds {_BOTH_NAMES}; keep one")

    return Markup.TOML if has_toml else Markup.YAML


def _walk_for_manifests(root: Path) -> list[tuple[Path, Markup]]:
    """Every manifest under `root` with its markup, symlinked directories
    excluded. A YAML name counts where the walk lists it among the files
    of its directory."""
    found: list[tuple[Path, Markup]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        here = Path(current)
        # Each entry of the directory, before the filter below: a link to a
        # directory is in `directories`, and a link to no file in `files`.
        entries = frozenset(files) | frozenset(directories)
        depth = len(here.relative_to(root).parts)
        if depth >= MAX_WALK_DEPTH:
            directories[:] = []

        directories[:] = sorted(
            name
            for name in directories
            if name not in SKIPPED_DIRS and not (here / name).is_symlink()
        )

        markup = _markup_of(entries, _relative(here, root))
        if markup is Markup.TOML or MANIFEST_FILENAME_YAML in files:
            found.append((here / markup.file_name, markup))

        if len(found) > MAX_MANIFEST_FILES:
            raise Refusal(
                RefusalCode.CATALOG,
                str(root),
                f"more than {MAX_MANIFEST_FILES} {_BOTH_NAMES} files",
            )

    return found


def _relative(directory: Path, root: Path) -> str:
    """A directory under `root` as a repo-relative path, `.` for the root."""
    relative = directory.relative_to(root).as_posix()

    return REPO_ROOT_PATH if relative in {"", "."} else relative


def _directory_of(path: Path, root: Path) -> str:
    return _relative(path.parent, root)


def _check_against_catalog(found: FoundManifest) -> None:
    """Contract 06 §10: the list is fixed by the contract, not by a merge."""
    manifest = found.manifest
    row = CATALOG_BY_NAME.get(manifest.name)
    if row is None:
        detail = f"names {safe_token(manifest.name)}, which contract 06 §1 does not list"
        raise Refusal(RefusalCode.CATALOG, found.subject, detail)

    if manifest.repo is not row.repo:
        detail = f"{manifest.name} lives in {row.repo}, not {manifest.repo}"
        raise Refusal(RefusalCode.CATALOG, found.subject, detail)

    if manifest.path != found.directory:
        detail = f"declares path {safe_token(manifest.path)} but sits in {found.directory}"
        raise Refusal(RefusalCode.CATALOG, found.subject, detail)

    if manifest.release is not row.releases:
        detail = f"{manifest.name} releases: {row.releases}, not {manifest.release}"
        raise Refusal(RefusalCode.CATALOG, found.subject, detail)

    if manifest.kind is not row.kind:
        detail = f"{manifest.name} is kind {row.kind}, not {manifest.kind}"
        raise Refusal(RefusalCode.CATALOG, found.subject, detail)


def read_one(root: Path, name: str) -> FoundManifest:
    """One component's manifest, at the path the catalog fixes.

    `discover` walks a whole repository, which is what the CLI wants from a
    checkout. The executor wants the opposite: it fetches one clone PER
    COMPONENT (`stage7-releases.md` §2.4 row 2, each at its own tag), so
    seven clones of `creche` each hold all seven manifests and a walk
    of the second one refuses the set as a duplicate declaration.

    Contract 06 §10's "a catalogued component with no file" lands here, per
    component, instead of in `require_complete`.
    """
    row = CATALOG_BY_NAME[name]
    directory = root if row.path == REPO_ROOT_PATH else root / row.path
    # The tree came out of a checkout at a SHA the live state named, so the
    # directory is input too: `_read_manifest_file` refuses a symlinked
    # FILE, and this refuses a symlinked directory above it, before the
    # entries of the directory count.
    if directory.is_symlink():
        raise Refusal(RefusalCode.CATALOG, row.path, f"{row.path} is a symlink")

    markup = _markup_of(_entries_of(directory), row.path)
    subject = f"{row.path}/{markup.file_name}"
    path = directory / markup.file_name
    if not path.exists():
        detail = f"no {EITHER_MANIFEST_NAME} under any root: {name}"
        raise Refusal(RefusalCode.CATALOG, "contract 06 §1", detail)

    text = _read_manifest_file(path, subject, markup)
    manifest = parse_manifest(text, subject, markup)
    found = FoundManifest(
        manifest=manifest, directory=row.path, subject=subject, markup=markup, text=text
    )
    _check_against_catalog(found)
    if manifest.name != name:
        detail = f"declares {safe_token(manifest.name)} at {name}'s path"
        raise Refusal(RefusalCode.CATALOG, subject, detail)

    return found


def discover(roots: list[Path]) -> ManifestSet:
    """Read every manifest under `roots` and match it to the catalog."""
    found: dict[str, FoundManifest] = {}
    for root in roots:
        resolved = root.resolve()
        for path, markup in _walk_for_manifests(resolved):
            directory = _directory_of(path, resolved)
            subject = f"{directory}/{markup.file_name}"
            text = _read_manifest_file(path, subject, markup)
            manifest = parse_manifest(text, subject, markup)
            entry = FoundManifest(
                manifest=manifest, directory=directory, subject=subject, markup=markup, text=text
            )
            _check_against_catalog(entry)

            earlier = found.get(manifest.name)
            if earlier is not None:
                detail = f"{manifest.name} is declared twice, also at {earlier.subject}"
                raise Refusal(RefusalCode.CATALOG, subject, detail)

            found[manifest.name] = entry

    absent = (row.name for row in CATALOG if row.name not in found)
    missing = tuple(name for name in absent if name not in RETIRING | ARRIVING)

    return ManifestSet(found=found, missing=missing)


def require_complete(manifests: ManifestSet) -> None:
    """Contract 06 §10: a component with no file is a bug, not a release."""
    if not manifests.missing:
        return

    detail = f"no {EITHER_MANIFEST_NAME} under any root: {', '.join(manifests.missing)}"
    raise Refusal(RefusalCode.CATALOG, "contract 06 §1", detail)


def releasable(manifests: ManifestSet) -> dict[str, ComponentManifest]:
    """Everything a request may name: the catalog minus the data components."""
    return {
        name: item.manifest
        for name, item in manifests.found.items()
        if item.manifest.release is Releases.YES
    }
