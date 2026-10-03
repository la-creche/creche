"""The per-chat `code-sandbox` directory (contract 02 §12.1, contract 03 §7.2)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from agent_sessiond.errors import ApiError, ErrorCode
from agent_sessiond.workspace import (
    CODE_SANDBOX_FAMILY,
    WORKSPACE_KIND,
    make_owner_dir,
    owner_dir,
    remove_owner_dir,
    workspace_of,
)

OWNER = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"

# Every one of these would leave the work root if it became a path component.
HOSTILE_IDS = ("..", ".", "../../etc", "a/b", "", "/srv", "owui-" + chr(0), "." * 4)


def test_the_directory_sits_under_the_work_root(tmp_path: Path) -> None:
    made = make_owner_dir(tmp_path, OWNER)

    assert made == owner_dir(tmp_path, OWNER)
    assert made == tmp_path / CODE_SANDBOX_FAMILY / OWNER
    assert made.is_dir()


def test_the_directory_is_private(tmp_path: Path) -> None:
    """Contract 02 §12.1 rule 2. One chat may not read another chat's files."""
    made = make_owner_dir(tmp_path, OWNER)

    assert stat.S_IMODE(os.stat(made).st_mode) == 0o700


def test_making_it_twice_keeps_the_first_one(tmp_path: Path) -> None:
    """Rule 2 creates it at the FIRST delegate call. Later calls find it."""
    made = make_owner_dir(tmp_path, OWNER)
    (made / "chart.py").write_text("print(1)", encoding="utf-8")
    again = make_owner_dir(tmp_path, OWNER)

    assert again == made
    assert (again / "chart.py").read_text(encoding="utf-8") == "print(1)"


@pytest.mark.parametrize("owner", HOSTILE_IDS)
def test_a_hostile_owner_id_never_becomes_a_path(tmp_path: Path, owner: str) -> None:
    """The id arrives from the PEP, so it is checked before it is a component."""
    with pytest.raises(ApiError) as refused:
        make_owner_dir(tmp_path, owner)

    assert refused.value.code is ErrorCode.BAD_REQUEST
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("owner", HOSTILE_IDS)
def test_a_hostile_owner_id_is_refused_by_every_door(tmp_path: Path, owner: str) -> None:
    for call in (owner_dir, remove_owner_dir):
        with pytest.raises(ApiError):
            call(tmp_path, owner)


def test_removing_it_takes_the_whole_tree(tmp_path: Path) -> None:
    made = make_owner_dir(tmp_path, OWNER)
    (made / "out").mkdir()
    (made / "out" / "chart.png").write_bytes(b"\x89PNG")

    assert remove_owner_dir(tmp_path, OWNER) is True
    assert not made.exists()


def test_removing_one_that_was_never_made_is_quiet(tmp_path: Path) -> None:
    """A chat that never delegated still deletes cleanly (contract 02 §5.8)."""
    assert remove_owner_dir(tmp_path, OWNER) is False


def test_only_the_code_sandbox_family_gets_a_workspace() -> None:
    """Contract 03 §7.2. One kind, and one family that carries it."""
    assert workspace_of(CODE_SANDBOX_FAMILY, OWNER) == {
        "kind": WORKSPACE_KIND,
        "owner_session": OWNER,
    }
    assert workspace_of("vault-oracle", OWNER) is None
    assert workspace_of(CODE_SANDBOX_FAMILY, None) is None


def test_the_workspace_carries_no_host_path() -> None:
    """The playpen derives the target from its own mount (§7.2)."""
    body = workspace_of(CODE_SANDBOX_FAMILY, OWNER)

    assert body is not None
    assert set(body) == {"kind", "owner_session"}
