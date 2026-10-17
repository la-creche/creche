"""The version of the manifest contract has one source: the manifest of this tool.

The `provides` row for `component-manifest` in `handover/component.yaml`
states the version. `catalog.py` holds the same two numbers as constants,
because the reader, the resolver and the uninstalled manifest need them as
code. This module fails when the constants and the row differ.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.catalog import MANIFEST_CONTRACT_MAJOR, MANIFEST_CONTRACT_MINOR, ContractId
from handover.errors import Refusal, RefusalCode
from handover.manifest import MANIFEST_FILENAME_YAML, Markup, parse_manifest
from handover.resolve import MANIFEST_VERSION
from handover_fixtures import FIXTURE_MANIFEST_VERSION, manifest_text

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The manifest that holds the row.
OWN_MANIFEST = REPO_ROOT / "handover" / MANIFEST_FILENAME_YAML


def _provided() -> tuple[int, int]:
    """The two numbers of the one row for `component-manifest`."""
    manifest = parse_manifest(OWN_MANIFEST.read_text(encoding="utf-8"), "handover", Markup.YAML)
    rows = [one for one in manifest.provides if one.contract is ContractId.COMPONENT_MANIFEST]
    assert len(rows) == 1, rows

    return rows[0].major, rows[0].minor


def _declaring(version: str) -> str:
    """A valid manifest that declares `version`."""
    declared = f'manifest_version: "{FIXTURE_MANIFEST_VERSION}"'
    text = manifest_text("attendance")
    assert text.count(declared) == 1

    return text.replace(declared, f'manifest_version: "{version}"')


def test_the_two_constants_are_the_row_of_the_manifest() -> None:
    assert _provided() == (MANIFEST_CONTRACT_MAJOR, MANIFEST_CONTRACT_MINOR)


def test_a_resolved_manifest_states_the_version_of_the_row() -> None:
    major, minor = _provided()

    assert f"{major}.{minor}" == MANIFEST_VERSION


def test_a_manifest_at_the_version_of_the_row_reads() -> None:
    major, minor = _provided()
    version = f"{major}.{minor}"

    assert (
        parse_manifest(_declaring(version), "attendance", Markup.YAML).manifest_version == version
    )


def test_a_manifest_one_minor_above_the_row_is_refused() -> None:
    major, minor = _provided()

    with pytest.raises(Refusal) as caught:
        parse_manifest(_declaring(f"{major}.{minor + 1}"), "attendance", Markup.YAML)

    assert caught.value.code is RefusalCode.MANIFEST
    assert caught.value.detail == (
        f"manifest_version {major}.{minor + 1} is outside {major}.0 to {major}.{minor}"
    )
