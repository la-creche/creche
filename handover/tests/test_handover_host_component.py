"""The `host` component of creche-release (contract 06 §1 row 10, §8.3).

It is the one `kind: units` component. Its manifest has no `build` and
names no `unit`. A host may not have a clone of creche-release yet, so
discovery tolerates its absence (`catalog.ARRIVING_REPOS`). The allocator
still tags it in its own repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.allocate import TagOutcome, plan_tags, untagged
from handover.catalog import ARRIVING_REPOS, CATALOG, CATALOG_BY_NAME, Kind, Repo
from handover.discovery import discover, read_one
from handover.errors import Refusal, RefusalCode
from handover.manifest import UNITS_HAVE_NO_BUILD, UNITS_NAME_NO_UNIT, Markup, parse_manifest
from handover_fixtures import HOST_MANIFEST, manifest_text, write_manifest

HOST = "host"

SUBJECT = "component.toml"


def _refusal(text: str) -> Refusal:
    with pytest.raises(Refusal) as caught:
        parse_manifest(text, SUBJECT, Markup.TOML)

    return caught.value


def test_the_row_is_the_whole_creche_release_repository() -> None:
    row = CATALOG_BY_NAME[HOST]

    assert (row.repo, row.path, row.kind) == (Repo.CRECHE_RELEASE, ".", Kind.UNITS)


def test_the_manifest_of_creche_release_reads(tmp_path: Path) -> None:
    (tmp_path / SUBJECT).write_text(HOST_MANIFEST, encoding="utf-8")

    found = read_one(tmp_path, HOST)

    assert found.manifest.kind is Kind.UNITS
    assert found.manifest.unit is None
    assert found.manifest.build == ()


def test_a_units_manifest_that_names_a_unit_is_refused() -> None:
    text = HOST_MANIFEST.replace('runs_as = "none"\n', 'runs_as = "none"\nunit = "x.service"\n')

    refusal = _refusal(text)

    assert (refusal.code, refusal.detail) == (RefusalCode.MANIFEST, UNITS_NAME_NO_UNIT)


def test_a_units_manifest_with_a_build_is_refused() -> None:
    text = HOST_MANIFEST.replace('runs_as = "none"\n', 'runs_as = "none"\nbuild = [["/bin/a"]]\n')

    refusal = _refusal(text)

    assert (refusal.code, refusal.detail) == (RefusalCode.MANIFEST, UNITS_HAVE_NO_BUILD)


# ---- a host with no clone of creche-release -----------------------------------


def _clones(tmp_path: Path, omit: str = "") -> list[Path]:
    """One root per repository, as on a host. No root for a repository in
    `ARRIVING_REPOS`."""
    roots: dict[Repo, Path] = {}
    for row in CATALOG:
        if row.repo in ARRIVING_REPOS or row.name == omit:
            continue

        root = roots.setdefault(row.repo, tmp_path / str(row.repo))
        write_manifest(root, row.name, manifest_text(row.name))

    return list(roots.values())


def test_a_host_with_no_clone_of_creche_release_reads_complete(tmp_path: Path) -> None:
    assert discover(_clones(tmp_path)).missing == ()


def test_another_absent_component_is_still_missing(tmp_path: Path) -> None:
    assert discover(_clones(tmp_path, omit="handover")).missing == ("handover",)


# ---- the tags of creche-release ------------------------------------------------


def test_the_first_merge_tags_host() -> None:
    assert untagged((), Repo.CRECHE_RELEASE) == (HOST,)

    plans = plan_tags(("README.md",), (), (), (), Repo.CRECHE_RELEASE)

    assert [(plan.component, plan.outcome, plan.tag) for plan in plans] == [
        (HOST, TagOutcome.CREATE, "host-v0.1.0")
    ]


def test_a_changed_unit_moves_host() -> None:
    plans = plan_tags(
        ("units/user/creche-follow.timer",), (), ("host-v0.1.0",), (), Repo.CRECHE_RELEASE
    )

    assert [(plan.component, plan.tag) for plan in plans] == [(HOST, "host-v0.1.1")]
