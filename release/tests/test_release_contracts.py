"""Rules C1 to C5 of contract 06 §3.2, each proved by a set that is refused."""

from __future__ import annotations

import pytest
from agent_release.catalog import ContractId
from agent_release.contracts import ContractRow, LiveProvided, build_table
from agent_release.errors import Refusal, RefusalCode
from agent_release.manifest import ComponentManifest, parse_manifest
from release_fixtures import manifest_text, provides_entry, requires_entry

PEP_GRANT_LIVE: LiveProvided = {ContractId.PEP_GRANT: (2, 0)}


def _manifest(name: str, *, provides: str = "", requires: str = "") -> ComponentManifest:
    text = manifest_text(name, provides=provides, requires=requires)

    return parse_manifest(text, f"{name}/component.yaml")


def _green_set() -> dict[str, ComponentManifest]:
    """`pep` provides pep-grant 2.1. `attendance` and `noticeboard` call it at 2.0."""
    return {
        "pep": _manifest("pep", provides=provides_entry("pep-grant", 2, 1)),
        "attendance": _manifest("attendance", requires=requires_entry("pep-grant", 2, 0)),
        "noticeboard": _manifest("noticeboard", requires=requires_entry("pep-grant", 2, 0)),
        "infra": _manifest("infra"),
    }


def _refusal(
    manifests: dict[str, ComponentManifest],
    deploying: frozenset[str],
    live: LiveProvided,
) -> Refusal:
    with pytest.raises(Refusal) as caught:
        build_table(manifests, deploying, live)

    return caught.value


def test_a_green_set_returns_one_row_per_contract() -> None:
    rows = build_table(_green_set(), frozenset({"pep"}), PEP_GRANT_LIVE).rows

    assert len(rows) == 1
    assert rows[0].contract is ContractId.PEP_GRANT
    assert rows[0].provider == "pep"
    assert rows[0].version() == "2.1"
    assert [item.name for item in rows[0].consumers] == ["attendance", "noticeboard"]


def test_a_component_with_neither_list_is_in_no_row() -> None:
    rows = build_table(_green_set(), frozenset({"infra"}), PEP_GRANT_LIVE).rows

    assert all(item.name != "infra" for row in rows for item in row.consumers)


def test_rows_come_back_in_contract_id_order() -> None:
    manifests = _green_set()
    manifests["attendance"] = _manifest(
        "attendance",
        provides=provides_entry("session-api", 1, 4),
        requires=requires_entry("pep-grant", 2, 0),
    )

    rows = build_table(manifests, frozenset({"pep"}), PEP_GRANT_LIVE).rows

    assert [str(row.contract) for row in rows] == sorted(str(row.contract) for row in rows)


def test_c1_names_the_requirer_provider_and_both_versions() -> None:
    manifests = _green_set()
    manifests["attendance"] = _manifest("attendance", requires=requires_entry("pep-grant", 2, 4))

    refusal = _refusal(manifests, frozenset({"pep"}), PEP_GRANT_LIVE)

    assert refusal.code is RefusalCode.C1
    assert "attendance requires pep-grant 2.4" in refusal.detail
    assert "pep provides 2.1" in refusal.detail


def test_c1_refuses_a_different_major() -> None:
    manifests = _green_set()
    manifests["attendance"] = _manifest("attendance", requires=requires_entry("pep-grant", 3, 0))

    refusal = _refusal(manifests, frozenset({"pep"}), PEP_GRANT_LIVE)

    assert refusal.code is RefusalCode.C1
    assert "attendance requires pep-grant 3.0" in refusal.detail


def test_c1_reports_a_contract_nobody_provides_and_does_not_refuse() -> None:
    """A contract with no provider root can see is reported, not refused.

    Root cannot tell "nothing provides it" from "its provider is not a
    tree under an install root". `playpen` is live and provides
    `channel`, and it is an image and never a tree, so a refusal would
    read `caregiver requires channel 0.11, the set provides it nowhere`.

    Refusing there refuses every release on this host for ever, for a
    reason the release neither causes nor can fix. So it is reported.
    """
    manifests = _green_set()
    manifests["noticeboard"] = _manifest(
        "noticeboard", requires=requires_entry("manager-status", 1, 1)
    )

    table = build_table(manifests, frozenset({"noticeboard"}), PEP_GRANT_LIVE)

    assert [one.consumer for one in table.unprovided] == ["noticeboard"]
    assert table.unprovided[0].contract is ContractId.MANAGER_STATUS
    assert table.unprovided[0].line() == (
        "not verified: noticeboard requires manager-status 1.1, "
        "and caregiver is not a tree under an install root"
    )


def test_a_provider_that_IS_in_the_set_is_still_refused() -> None:
    """The teeth stay where root can bite. Reporting an absent provider
    does not soften the check on one root can see, which is every
    component a release manages."""
    manifests = _green_set()
    manifests["attendance"] = _manifest("attendance", requires=requires_entry("pep-grant", 2, 9))

    refusal = _refusal(manifests, frozenset({"pep"}), PEP_GRANT_LIVE)

    assert refusal.code is RefusalCode.C1
    assert "attendance requires pep-grant 2.9, pep provides 2.1" in refusal.detail


def test_c2_refuses_two_providers() -> None:
    manifests = _green_set()
    manifests["caregiver"] = _manifest("caregiver", provides=provides_entry("pep-grant", 2, 1))

    refusal = _refusal(manifests, frozenset({"pep"}), PEP_GRANT_LIVE)

    assert refusal.code is RefusalCode.C2
    assert "ambiguous provider for pep-grant" in refusal.detail
    assert "caregiver, pep" in refusal.detail


def test_c3_refuses_a_provider_that_owns_nothing() -> None:
    manifests = {
        "noticeboard": _manifest("noticeboard", provides=provides_entry("pep-grant", 2, 1))
    }

    refusal = _refusal(manifests, frozenset({"noticeboard"}), PEP_GRANT_LIVE)

    assert refusal.code is RefusalCode.C3
    assert "wrong provider for pep-grant: noticeboard provides it, pep owns it" in refusal.detail


def test_c4_refuses_a_major_bump_that_leaves_a_consumer_behind() -> None:
    manifests = _green_set()
    manifests["pep"] = _manifest("pep", provides=provides_entry("pep-grant", 3, 0))
    manifests["attendance"] = _manifest("attendance", requires=requires_entry("pep-grant", 3, 0))

    refusal = _refusal(manifests, frozenset({"pep", "attendance"}), PEP_GRANT_LIVE)

    assert refusal.code is RefusalCode.C4
    assert "breaking change needs a set" in refusal.detail
    assert "2.0 -> 3.0" in refusal.detail
    assert "missing consumers: noticeboard" in refusal.detail


def test_c4_accepts_the_bump_when_every_consumer_ships() -> None:
    manifests = _green_set()
    manifests["pep"] = _manifest("pep", provides=provides_entry("pep-grant", 3, 0))
    manifests["attendance"] = _manifest("attendance", requires=requires_entry("pep-grant", 3, 0))
    manifests["noticeboard"] = _manifest("noticeboard", requires=requires_entry("pep-grant", 3, 0))

    rows = build_table(
        manifests, frozenset({"pep", "attendance", "noticeboard"}), PEP_GRANT_LIVE
    ).rows

    assert rows[0].version() == "3.0"


def test_c4_does_not_apply_to_a_first_install() -> None:
    manifests = _green_set()
    manifests["pep"] = _manifest("pep", provides=provides_entry("pep-grant", 2, 1))
    manifests["attendance"] = _manifest("attendance", requires=requires_entry("pep-grant", 2, 0))
    manifests["noticeboard"] = _manifest("noticeboard", requires=requires_entry("pep-grant", 2, 0))

    assert build_table(manifests, frozenset({"pep"}), {}).rows


def test_c4_ignores_a_provider_this_release_leaves_alone() -> None:
    """A stale live state cannot make an untouched provider look like a bump."""
    manifests = _green_set()
    manifests["pep"] = _manifest("pep", provides=provides_entry("pep-grant", 3, 0))
    manifests["attendance"] = _manifest("attendance", requires=requires_entry("pep-grant", 3, 0))
    manifests["noticeboard"] = _manifest("noticeboard", requires=requires_entry("pep-grant", 3, 0))

    table = build_table(manifests, frozenset({"infra"}), PEP_GRANT_LIVE)
    rows: tuple[ContractRow, ...] = table.rows

    assert rows[0].version() == "3.0"


def test_a_minor_bump_needs_no_set() -> None:
    rows = build_table(_green_set(), frozenset({"pep"}), {ContractId.PEP_GRANT: (2, 0)}).rows

    assert rows[0].version() == "2.1"
