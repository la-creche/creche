"""Manifest text and fixture trees the release tests share.

A test that needs a hostile manifest writes the text itself. This module
builds only VALID manifests, so a test can change one field and prove that
one refusal.
"""

from __future__ import annotations

from pathlib import Path

from handover.catalog import CATALOG_BY_NAME, Kind, Releases

#: The contract version the fixtures declare. Kept equal to the catalog's
#: ceiling so a fixture never fails on `manifest_version` by accident.
FIXTURE_MANIFEST_VERSION = "0.4"

#: Where a fixture component installs. No test touches these paths: they only
#: have to satisfy contract 06 §8's absolute-path rule.
_INSTALL_ROOT = "/opt/components"


def manifest_text(
    name: str,
    *,
    provides: str = "",
    requires: str = "",
    depends_on: str = "",
    path: str | None = None,
    release: str | None = None,
) -> str:
    """One valid `component.yaml`, with three lists a caller may fill in."""
    row = CATALOG_BY_NAME[name]
    kind = Kind.DATA if row.releases is Releases.NO else Kind.VENV
    declared = release if release is not None else str(row.releases)

    return f"""
manifest_version: "{FIXTURE_MANIFEST_VERSION}"
name: {name}
repo: {row.repo}
path: "{path if path is not None else row.path}"
kind: {kind}
unit: null
runs_as: operator
install:
  to: {_INSTALL_ROOT}/{name}
  prev: {_INSTALL_ROOT}/{name}.prev
provides: [{provides}]
requires: [{requires}]
depends_on: [{depends_on}]
verify:
  command: ["{_INSTALL_ROOT}/{name}/bin/{name}-verify", "--json"]
  user: operator
  timeout_s: 60
restore:
  mode: automatic
  keep: 3
secrets: []
release: "{declared}"
"""


def provides_entry(contract: str, major: int, minor: int) -> str:
    return f"{{ contract: {contract}, major: {major}, minor: {minor} }}"


def requires_entry(contract: str, major: int, min_minor: int) -> str:
    return f"{{ contract: {contract}, major: {major}, min_minor: {min_minor} }}"


def write_manifest(root: Path, name: str, text: str) -> Path:
    """Write one manifest at the directory the catalog gives that component."""
    row = CATALOG_BY_NAME[name]
    directory = root if row.path == "." else root / row.path
    directory.mkdir(parents=True, exist_ok=True)
    written = directory / "component.yaml"
    written.write_text(text, encoding="utf-8")

    return written
