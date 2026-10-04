"""What the `kind: binary` tests share.

The catalog holds no binary component, and this change adds none. So a test
that needs one swaps one row of the catalog for a binary copy of itself, for
that test alone.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME, CatalogRow, Kind

from handover import allocate

#: The component the tests turn into a binary one. It is a venv in the catalog.
BINARY_NAME = "noticeboard"

#: A crate directory under the Cargo workspace, as a build bundles one.
NESTED_BUNDLE = "rust/crates/creche-contracts"

#: The fixture manifest of a binary component, beside this module.
BINARY_MANIFEST = Path(__file__).resolve().parent / "handover_bin_component.yaml"


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
