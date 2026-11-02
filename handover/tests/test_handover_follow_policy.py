"""`follow/policy.py`: the five rules of `host.toml`, its read, and the
ceiling that `moving_of` applies (`follow/` rule 9)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from handover.follow import moving_of
from handover.follow.policy import (
    POLICY_NAME,
    Policy,
    PolicyError,
    Registry,
    parse_policy,
    read_policy,
)
from handover.state import ReleaseState
from handover.tomlfile import FileKind

#: `creche-release`'s `host.toml` on 2026-11-02, without its comments.
CRECHE_RELEASE_POLICY = """\
[follow]
chaperone = "newest"
attendance = "newest"
caregiver = "newest"
noticeboard = "newest"
playpen = "newest"
handover = "newest"
mcp-servers = "newest"
host = "newest"

[registry]
mcp = "follow"
"""

BOARD = "noticeboard"


def _fault(text: str) -> str:
    with pytest.raises(PolicyError) as caught:
        parse_policy(text.encode())

    return str(caught.value)


# ---- the five rules -------------------------------------------------------------


def test_the_policy_of_creche_release_today_is_read() -> None:
    policy = parse_policy(CRECHE_RELEASE_POLICY.encode())

    assert policy.registry is Registry.FOLLOW
    assert len(policy.follow) == 8
    assert set(policy.follow.values()) == {None}


def test_a_version_is_a_ceiling() -> None:
    text = f'[follow]\n{BOARD} = "0.4.2"\n\n[registry]\nmcp = "hold"\n'

    assert parse_policy(text.encode()) == Policy({BOARD: "0.4.2"}, Registry.HOLD)


def test_an_empty_follow_table_follows_nothing() -> None:
    assert parse_policy(b'[follow]\n[registry]\nmcp = "hold"\n') == Policy({}, Registry.HOLD)


@pytest.mark.parametrize(
    ("text", "fault"),
    [
        ('[registry]\nmcp = "hold"\n', "host.toml has no key follow"),
        ("[follow]\n", "host.toml has no key registry"),
        ('[follow]\n[registry]\nmcp = "hold"\n[other]\n', "has the key other, which no rule names"),
        ('follow = "x"\n[registry]\nmcp = "hold"\n', "follow is not a table"),
        ("registry = 1\n[follow]\n", "registry is not a table"),
    ],
)
def test_rule_1_the_two_tables(text: str, fault: str) -> None:
    assert fault in _fault(text)


@pytest.mark.parametrize("name", ["registry", "nobody", "creche-handover"])
def test_rule_2_a_name_that_cannot_release_is_a_fault(name: str) -> None:
    text = f'[follow]\n{name} = "newest"\n[registry]\nmcp = "hold"\n'

    assert "which cannot release" in _fault(text)


@pytest.mark.parametrize("value", ['"latest"', '"0.4"', '"v0.4.2"', '"0.4.2-rc1"', "1", "true"])
def test_rule_3_a_value_that_is_not_newest_or_a_version_is_a_fault(value: str) -> None:
    text = f'[follow]\n{BOARD} = {value}\n[registry]\nmcp = "hold"\n'

    assert "not newest or MAJOR.MINOR.PATCH" in _fault(text)


@pytest.mark.parametrize(
    ("registry", "fault"),
    [
        ("", "[registry] has no key mcp"),
        ('mcp = "hold"\nfamilies = "hold"\n', "has the key families, which no rule names"),
        ('mcp = "newest"\n', "mcp is newest, not follow or hold"),
        ("mcp = 1\n", "not follow or hold"),
    ],
)
def test_rule_4_the_registry_table(registry: str, fault: str) -> None:
    assert fault in _fault(f"[follow]\n[registry]\n{registry}")


def test_rule_5_the_registry_watch_needs_mcp_servers_followed() -> None:
    text = f'[follow]\n{BOARD} = "newest"\n[registry]\nmcp = "follow"\n'

    assert "needs mcp-servers in [follow]" in _fault(text)


def test_a_text_that_is_not_toml_is_a_fault() -> None:
    assert "the text is not TOML 1.0" in _fault("[follow\n")


def test_a_text_past_the_byte_limit_is_a_fault() -> None:
    text = CRECHE_RELEASE_POLICY + "#" * FileKind.POLICY.byte_limit

    assert "more than 8192 bytes" in _fault(text)


# ---- the read -------------------------------------------------------------------


def _host_tree(root: Path) -> Path:
    tree = root / "host"
    tree.mkdir(parents=True)

    return tree


def test_the_policy_of_the_first_root_with_a_host_tree_is_read(tmp_path: Path) -> None:
    first, second = tmp_path / "root", tmp_path / "operator"
    (_host_tree(second) / POLICY_NAME).write_text("[follow\n", encoding="utf-8")
    (_host_tree(first) / POLICY_NAME).write_text(CRECHE_RELEASE_POLICY, encoding="utf-8")

    assert read_policy((first, second)).registry is Registry.FOLLOW


def test_no_host_tree_is_a_fault(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="no install root holds a host tree"):
        read_policy((tmp_path,))


def test_a_linked_host_tree_is_no_tree(tmp_path: Path) -> None:
    elsewhere = _host_tree(tmp_path / "elsewhere")
    (elsewhere / POLICY_NAME).write_text(CRECHE_RELEASE_POLICY, encoding="utf-8")
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "host").symlink_to(elsewhere)

    with pytest.raises(PolicyError, match="no install root holds a host tree"):
        read_policy((tmp_path / "root",))


def test_a_host_tree_with_no_policy_is_a_fault(tmp_path: Path) -> None:
    _host_tree(tmp_path)

    with pytest.raises(PolicyError, match="cannot open"):
        read_policy((tmp_path,))


def test_a_linked_policy_is_a_fault(tmp_path: Path) -> None:
    (tmp_path / "real.toml").write_text(CRECHE_RELEASE_POLICY, encoding="utf-8")
    (_host_tree(tmp_path) / POLICY_NAME).symlink_to(tmp_path / "real.toml")

    with pytest.raises(PolicyError, match="cannot open"):
        read_policy((tmp_path,))


def test_a_policy_that_is_not_a_regular_file_is_a_fault(tmp_path: Path) -> None:
    os.mkfifo(_host_tree(tmp_path) / POLICY_NAME)

    with pytest.raises(PolicyError, match="is not a regular file"):
        read_policy((tmp_path,))


# ---- the ceiling in moving_of ------------------------------------------------------


def _state(live: str, newest: str) -> ReleaseState:
    return ReleaseState(live={BOARD: live}, provided={}, latest={BOARD: newest})


@pytest.mark.parametrize(
    ("live", "newest", "ceiling", "moving", "held"),
    [
        ("0.1.0", "0.1.2", None, {BOARD: "0.1.2"}, False),
        ("0.1.0", "0.1.2", "0.1.1", {BOARD: "0.1.1"}, True),
        ("0.1.0", "0.1.2", "0.1.2", {BOARD: "0.1.2"}, False),
        ("0.1.0", "0.1.1", "0.2.0", {BOARD: "0.1.1"}, False),
        ("0.1.1", "0.1.2", "0.1.1", {}, True),
        ("0.1.2", "0.1.3", "0.1.1", {}, True),
    ],
)
def test_a_ceiling_moves_up_to_its_version_and_never_above_it(
    live: str, newest: str, ceiling: str | None, moving: dict[str, str], held: bool
) -> None:
    found, notes = moving_of(_state(live, newest), {BOARD: ceiling})

    assert found == moving
    assert notes == ((f"{BOARD} is held at {ceiling}: {newest} is not followed",) if held else ())
