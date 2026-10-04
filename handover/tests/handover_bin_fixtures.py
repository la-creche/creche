"""What the `kind: binary` tests share.

The catalog holds no binary component, and this change adds none. So a test
that needs one swaps one row of the catalog for a binary copy of itself, for
that test alone. A test of the executor needs no catalog change: the
executor reads the kind from the manifest, so the manifest says `binary`.

Nothing here runs `cargo`. `staged_binary_tree` writes what `cargo install`
leaves, and `FakeRun` calls it when the build argv runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME, CatalogRow, Kind
from handover.executor.host import Command
from handover.executor.install import Installer, Paths, paths_of
from handover.manifest import ComponentManifest, parse_manifest
from handover_executor_fixtures import FakeRun, fake_host, git_host_answers
from handover_fixtures import manifest_text

from handover import allocate

#: The component the tests turn into a binary one. It is a venv in the catalog.
BINARY_NAME = "noticeboard"

#: A crate directory under the Cargo workspace, as a build bundles one.
NESTED_BUNDLE = "rust/crates/creche-contracts"

#: The fixture manifest of a binary component, beside this module.
BINARY_MANIFEST = Path(__file__).resolve().parent / "handover_bin_component.yaml"

#: The word `FakeRun` knows the build by. Every build argv below holds it.
CARGO = "cargo"

#: The fixture manifests carry no `build`, and the executor stages nothing
#: without one. This is the build of the fixture manifest file.
BUILD_LINE = (
    'build:\n  - ["/usr/local/bin/cargo", "install", "--locked", "--no-track",'
    f' "--path", "rust/crates/{BINARY_NAME}"]\ninstall:'
)

#: The unit of the binary component, and a second unit out of the same tree.
UNIT = "creche-noticeboard.service"
SIBLING_UNIT = "creche-noticeboard-door.service"

#: What a compiled program starts with on the host. The tests never run one.
PROGRAM_BYTES = b"\x7fELF\x02\x01\x01\x00 a compiled program\n"

PROGRAM_MODE = 0o755


def binary_row(bundles: tuple[str, ...] = (NESTED_BUNDLE,)) -> CatalogRow:
    """`BINARY_NAME`'s own row, as a binary component with these bundles."""
    return replace(CATALOG_BY_NAME[BINARY_NAME], kind=Kind.BINARY, bundles=bundles)


def catalog_with(row: CatalogRow) -> tuple[CatalogRow, ...]:
    """The catalog, in its own order, with `row` in place of its namesake."""
    return tuple(row if one.name == row.name else one for one in CATALOG)


def use_binary_catalog(
    monkeypatch: pytest.MonkeyPatch, bundles: tuple[str, ...] = (NESTED_BUNDLE,)
) -> CatalogRow:
    """Make `BINARY_NAME` a binary component for one test. The allocator
    reads the row list and the digest reads the map, so both change."""
    row = binary_row(bundles)
    monkeypatch.setattr(allocate, "CATALOG", catalog_with(row))
    monkeypatch.setitem(CATALOG_BY_NAME, row.name, row)

    return row


# -- a binary component on the fake host -------------------------------------


def binary_manifest_text(
    components: Path, unit: str | None = None, kind: Kind = Kind.BINARY
) -> str:
    """`BINARY_NAME`'s manifest, with its tree under `components`, the
    cargo build and the given unit. `kind` lets a test read the same
    manifest as a venv, to show what the kind alone changes."""
    text = manifest_text(BINARY_NAME).replace("/opt/components", str(components))
    text = text.replace(f"kind: {Kind.VENV}", f"kind: {kind}")
    text = text.replace("install:", BUILD_LINE, 1)
    if unit is not None:
        text = text.replace("unit: null", f"unit: {unit}")

    return text


def unit_text(*programs: str) -> str:
    """A unit file with one `ExecStart=` line per program."""
    lines = ["[Unit]", "Description=a fixture unit", "", "[Service]"]
    lines.extend(f"ExecStart={program}" for program in programs)

    return "\n".join(lines) + "\n"


def write_program(path: Path, mode: int = PROGRAM_MODE, body: bytes = PROGRAM_BYTES) -> Path:
    """One compiled program, as `cargo install` writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(mode)

    return path


def staged_binary_tree(new: Path, *programs: str) -> Path:
    """The tree `cargo install --no-track` leaves at `new`: a `bin/` that
    holds the component's program and its verify hook, plus `programs`."""
    for name in (BINARY_NAME, f"{BINARY_NAME}-verify", *programs):
        write_program(new / "bin" / name)

    return new


@dataclass
class Staging:
    """One binary component about to be built on the fake host."""

    tmp_path: Path
    run: FakeRun
    installer: Installer
    manifest: ComponentManifest
    paths: Paths
    #: The fetched work tree, where the build argv runs.
    source: Path

    def build(self) -> None:
        self.installer.build(self.manifest, self.source, self.paths)

    def program(self, name: str = BINARY_NAME) -> str:
        """The path a unit names for one program of the LIVE tree."""
        return str(self.paths.to / "bin" / name)

    def install_unit(self, text: str, name: str = UNIT) -> Path:
        """Put a unit file where systemd reads system units."""
        path = self.tmp_path / "system-units" / name
        path.write_text(text, encoding="utf-8")

        return path

    def carry_unit(self, text: str, name: str = UNIT) -> Path:
        """Put a unit file in the fetched tree's `systemd/`."""
        path = self.source / "systemd" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

        return path

    def builds(self) -> list[Command]:
        """Every build child the executor started."""
        return [one for one in self.run.seen if any(CARGO in word for word in one.argv)]


def make_staging(
    tmp_path: Path,
    made: Callable[[Path], object] | None = staged_binary_tree,
    unit: str | None = None,
    kind: Kind = Kind.BINARY,
) -> Staging:
    """A fake host on which the build argv of a binary component leaves
    what `made` writes at `<install.to>.new`. None leaves nothing."""
    components = tmp_path / "components"
    manifest = parse_manifest(
        binary_manifest_text(components, unit, kind), f"{BINARY_NAME}/component.yaml"
    )
    paths = paths_of(manifest, (components,))
    run = FakeRun(answers=git_host_answers())
    if made is not None:
        run.hooks[CARGO] = lambda _: made(paths.new)

    host = fake_host(tmp_path, run)
    source = host.work_root / "01K5J8M2Q7V3X9R4T6N0B8C2DE" / BINARY_NAME
    source.mkdir(parents=True)

    return Staging(tmp_path, run, Installer(host), manifest, paths, source)
