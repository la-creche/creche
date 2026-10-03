"""Finding `component.yaml` on disk, and matching what is found to the catalog.

This is the only module that touches the filesystem for manifests. It reads
and never writes. A repository can be a branch an agent wrote, so the walk
itself is defensive: no symlink is followed, a cap bounds the number of files,
and a manifest is read only after its size passes.

Contract 06 §10's last two rows live here: a catalogued component with no
file, and a file for a name the catalog does not hold.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .catalog import CATALOG, CATALOG_BY_NAME, RETIRING, Releases
from .errors import Refusal, RefusalCode, safe_token
from .manifest import (
    MANIFEST_FILENAME,
    MAX_MANIFEST_BYTES,
    REPO_ROOT_PATH,
    ComponentManifest,
    parse_manifest,
)

#: Nine components plus room for a mistake. A tree with more than this many
#: `component.yaml` files is not the repository this tool knows.
MAX_MANIFEST_FILES = 32

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
    #: The label a refusal names, such as `chaperone/component.yaml`.
    subject: str
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


def _read_manifest_file(path: Path, subject: str) -> str:
    """Read one manifest, refusing a symlink and anything oversized."""
    if path.is_symlink():
        raise Refusal(RefusalCode.MANIFEST, subject, "is a symlink")

    stat_result = path.stat()
    if not path.is_file():
        raise Refusal(RefusalCode.MANIFEST, subject, "is not a regular file")

    if stat_result.st_size > MAX_MANIFEST_BYTES:
        raise Refusal(RefusalCode.MANIFEST, subject, f"larger than {MAX_MANIFEST_BYTES} bytes")

    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise Refusal(RefusalCode.MANIFEST, subject, "is not UTF-8") from None


def _walk_for_manifests(root: Path) -> list[Path]:
    """Every `component.yaml` under `root`, symlinked directories excluded."""
    found: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        if depth >= MAX_WALK_DEPTH:
            directories[:] = []

        directories[:] = sorted(
            name
            for name in directories
            if name not in SKIPPED_DIRS and not (here / name).is_symlink()
        )

        if MANIFEST_FILENAME in files:
            found.append(here / MANIFEST_FILENAME)

        if len(found) > MAX_MANIFEST_FILES:
            raise Refusal(
                RefusalCode.CATALOG,
                str(root),
                f"more than {MAX_MANIFEST_FILES} {MANIFEST_FILENAME} files",
            )

    return found


def _directory_of(path: Path, root: Path) -> str:
    relative = path.parent.relative_to(root).as_posix()

    return REPO_ROOT_PATH if relative == "" else relative


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


def read_one(root: Path, name: str) -> FoundManifest:
    """One component's `component.yaml`, at the path the catalog fixes.

    `discover` walks a whole repository, which is what the CLI wants from a
    checkout. The executor wants the opposite: it fetches one clone PER
    COMPONENT (`stage7-releases.md` §2.4 row 2, each at its own tag), so
    seven clones of `agent-control` each hold all seven manifests and a walk
    of the second one refuses the set as a duplicate declaration.

    Contract 06 §10's "a catalogued component with no file" lands here, per
    component, instead of in `require_complete`.
    """
    row = CATALOG_BY_NAME[name]
    directory = root if row.path == REPO_ROOT_PATH else root / row.path
    subject = f"{row.path}/{MANIFEST_FILENAME}"
    # The tree came out of a checkout at a SHA the live state named, so the
    # directory is input too: `_read_manifest_file` refuses a symlinked
    # FILE, and this refuses a symlinked directory above it.
    if directory.is_symlink():
        raise Refusal(RefusalCode.CATALOG, subject, f"{row.path} is a symlink")

    path = directory / MANIFEST_FILENAME
    if not path.exists():
        detail = f"no {MANIFEST_FILENAME} under any root: {name}"
        raise Refusal(RefusalCode.CATALOG, "contract 06 §1", detail)

    text = _read_manifest_file(path, subject)
    manifest = parse_manifest(text, subject)
    found = FoundManifest(manifest=manifest, directory=row.path, subject=subject, text=text)
    _check_against_catalog(found)
    if manifest.name != name:
        detail = f"declares {safe_token(manifest.name)} at {name}'s path"
        raise Refusal(RefusalCode.CATALOG, subject, detail)

    return found


def discover(roots: list[Path]) -> ManifestSet:
    """Read every `component.yaml` under `roots` and match it to the catalog."""
    found: dict[str, FoundManifest] = {}
    for root in roots:
        resolved = root.resolve()
        for path in _walk_for_manifests(resolved):
            directory = _directory_of(path, resolved)
            subject = f"{directory}/{MANIFEST_FILENAME}"
            text = _read_manifest_file(path, subject)
            manifest = parse_manifest(text, subject)
            entry = FoundManifest(
                manifest=manifest, directory=directory, subject=subject, text=text
            )
            _check_against_catalog(entry)

            earlier = found.get(manifest.name)
            if earlier is not None:
                detail = f"{manifest.name} is declared twice, also at {earlier.subject}"
                raise Refusal(RefusalCode.CATALOG, subject, detail)

            found[manifest.name] = entry

    absent = (row.name for row in CATALOG if row.name not in found)
    missing = tuple(name for name in absent if name not in RETIRING)

    return ManifestSet(found=found, missing=missing)


def require_complete(manifests: ManifestSet) -> None:
    """Contract 06 §10: a component with no file is a bug, not a release."""
    if not manifests.missing:
        return

    detail = f"no {MANIFEST_FILENAME} under any root: {', '.join(manifests.missing)}"
    raise Refusal(RefusalCode.CATALOG, "contract 06 §1", detail)


def releasable(manifests: ManifestSet) -> dict[str, ComponentManifest]:
    """Everything a request may name: the catalog minus the data components."""
    return {
        name: item.manifest
        for name, item in manifests.found.items()
        if item.manifest.release is Releases.YES
    }
