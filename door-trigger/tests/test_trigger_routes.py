"""`routes.py`: the route table built from the registry's declared webhook
triggers, gated by which families `families.py` says are servable."""

from __future__ import annotations

from pathlib import Path

from agent_door_trigger.routes import RouteTable
from agent_door_trigger.tokens import MIN_WEBHOOK_TOKEN_BYTES

GOOD_TOKEN = "w" * MIN_WEBHOOK_TOKEN_BYTES


class FakeFamilies:
    """An `AutonomousFamilies` test double: a fixed, settable set."""

    def __init__(self, names: frozenset[str] = frozenset()) -> None:
        self.names = names

    def servable(self) -> frozenset[str]:
        return self.names


def _write_family(
    registry_root: Path,
    name: str,
    *,
    triggers: str = "",
) -> None:
    directory = registry_root / "families" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "family.yaml").write_text(
        f"""\
name: {name}
kind: autonomous
description: Test fixture family.

model: {{ router: agent-router, budget_usd_per_day: 10 }}
{triggers}
""",
        encoding="utf-8",
    )


def _write_token(webhooks_dir: Path, family: str, name: str, token: str = GOOD_TOKEN) -> None:
    directory = webhooks_dir / family
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.token"
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)


def test_a_servable_familys_declared_webhook_becomes_a_route(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: deploy-notify\n")
    _write_token(webhooks_dir, "scrum-lead", "deploy-notify")

    table = RouteTable(
        registry_root=registry_root,
        webhooks_dir=webhooks_dir,
        families=FakeFamilies(frozenset({"scrum-lead"})),
    )
    assert table.refresh() == 1

    route = table.get("scrum-lead", "deploy-notify")
    assert route is not None
    assert route.token == GOOD_TOKEN


def test_a_family_families_py_excludes_gets_no_route_even_if_declared(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: deploy-notify\n")
    _write_token(webhooks_dir, "scrum-lead", "deploy-notify")

    # families.py says nothing is servable: e.g. this family never validated.
    table = RouteTable(
        registry_root=registry_root, webhooks_dir=webhooks_dir, families=FakeFamilies(frozenset())
    )
    table.refresh()

    assert table.get("scrum-lead", "deploy-notify") is None


def test_a_declared_webhook_with_no_token_file_gets_no_route(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: deploy-notify\n")
    # No _write_token call: the route has no token file at all.

    table = RouteTable(
        registry_root=registry_root,
        webhooks_dir=webhooks_dir,
        families=FakeFamilies(frozenset({"scrum-lead"})),
    )
    table.refresh()

    assert table.get("scrum-lead", "deploy-notify") is None


def test_a_token_caregiver_mints_later_is_served_on_the_next_refresh(tmp_path: Path) -> None:
    """`caregiver` mints the file, and contract 05 §6.4 rule 6 makes a new
    one live within `refresh_s`. A door that only read it at startup would
    answer 404 until the next restart."""
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: deploy-notify\n")
    table = RouteTable(
        registry_root=registry_root,
        webhooks_dir=webhooks_dir,
        families=FakeFamilies(frozenset({"scrum-lead"})),
    )
    assert table.refresh() == 0

    _write_token(webhooks_dir, "scrum-lead", "deploy-notify")

    assert table.refresh() == 1
    route = table.get("scrum-lead", "deploy-notify")
    assert route is not None
    assert route.token == GOOD_TOKEN


def test_a_rotated_token_replaces_the_old_one_on_a_refresh(tmp_path: Path) -> None:
    """Contract 05 §6.3 rotates a webhook bearer. No restart, and the old
    value stops working: one file holds one value, with no overlap."""
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: deploy-notify\n")
    _write_token(webhooks_dir, "scrum-lead", "deploy-notify")
    table = RouteTable(
        registry_root=registry_root,
        webhooks_dir=webhooks_dir,
        families=FakeFamilies(frozenset({"scrum-lead"})),
    )
    table.refresh()
    rotated = "r" * MIN_WEBHOOK_TOKEN_BYTES
    _write_token(webhooks_dir, "scrum-lead", "deploy-notify", token=rotated)

    table.refresh()

    route = table.get("scrum-lead", "deploy-notify")
    assert route is not None
    assert route.token == rotated


def test_a_cron_only_family_contributes_no_webhook_route(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers='\ntriggers:\n  - cron: "@hourly"\n')

    table = RouteTable(
        registry_root=registry_root,
        webhooks_dir=webhooks_dir,
        families=FakeFamilies(frozenset({"scrum-lead"})),
    )
    assert table.refresh() == 0


def test_an_unknown_family_and_name_are_both_simply_absent(tmp_path: Path) -> None:
    table = RouteTable(
        registry_root=tmp_path / "registry",
        webhooks_dir=tmp_path / "webhooks",
        families=FakeFamilies(frozenset()),
    )
    table.refresh()

    assert table.get("no-such-family", "no-such-name") is None


def test_refresh_replaces_the_table_rather_than_merging(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: old-hook\n")
    _write_token(webhooks_dir, "scrum-lead", "old-hook")

    table = RouteTable(
        registry_root=registry_root,
        webhooks_dir=webhooks_dir,
        families=FakeFamilies(frozenset({"scrum-lead"})),
    )
    table.refresh()
    assert table.get("scrum-lead", "old-hook") is not None

    # The family file is edited to rename the webhook. A stale entry for
    # the old name must not survive a refresh.
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: new-hook\n")
    _write_token(webhooks_dir, "scrum-lead", "new-hook")
    table.refresh()

    assert table.get("scrum-lead", "old-hook") is None
    assert table.get("scrum-lead", "new-hook") is not None


def test_two_families_each_get_their_own_route(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    webhooks_dir = tmp_path / "webhooks"
    _write_family(registry_root, "scrum-lead", triggers="\ntriggers:\n  - webhook: lead-hook\n")
    _write_family(registry_root, "ha-review", triggers="\ntriggers:\n  - webhook: review-hook\n")
    _write_token(webhooks_dir, "scrum-lead", "lead-hook")
    _write_token(webhooks_dir, "ha-review", "review-hook")

    table = RouteTable(
        registry_root=registry_root,
        webhooks_dir=webhooks_dir,
        families=FakeFamilies(frozenset({"scrum-lead", "ha-review"})),
    )
    assert table.refresh() == 2
    assert table.get("scrum-lead", "lead-hook") is not None
    assert table.get("ha-review", "review-hook") is not None
    # A token provisioned for one family never answers for another.
    assert table.get("scrum-lead", "review-hook") is None
