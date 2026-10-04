"""Which sandbox serves a terminal, from `caregiver`'s status document.

Contract 05 §2 and §4. The door needs two values before it can exec: the
sandbox id and the host path of that sandbox's `supervisor.env`. It refuses
when a switch is in progress, or when no sandbox serves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agent_door_tui.errors import DoorError, Exit
from agent_door_tui.status import StatusFiles

FAMILY = "chat"
ENV_PATH = "/srv/agents/state/rework/families/chat/supervisor.env"

# More levels than the JSON parser of each supported Python reads.
TOO_DEEP = 100_000


def write_status(root: Path, **overrides: Any) -> Path:
    """One status document, as contract 05 §2 shapes it."""
    document: dict[str, Any] = {
        "family": FAMILY,
        "kind": "attended",
        "state": "in_sync",
        "written_at": "2026-09-19T10:00:00Z",
        "validation": {"never_valid": False},
        "faults": [],
        "reconcile": None,
        "sandboxes": [{"id": "chat-s1", "state": "ready", "supervisor_env": ENV_PATH}],
    }
    document.update(overrides)
    path = root / FAMILY / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    return path


def read(root: Path, family: str = FAMILY) -> Any:
    return StatusFiles(root).serving(family)


def test_a_ready_sandbox_serves(tmp_path: Path) -> None:
    write_status(tmp_path)

    serving = read(tmp_path)

    assert serving.sandbox == "chat-s1"
    assert serving.playpen_env == ENV_PATH


def test_the_newest_ready_sandbox_wins(tmp_path: Path) -> None:
    """`N` is monotonic and never reused, so the highest one is the newest."""
    write_status(
        tmp_path,
        sandboxes=[
            {"id": "chat-s9", "state": "ready", "supervisor_env": ENV_PATH},
            {"id": "chat-s10", "state": "ready", "supervisor_env": ENV_PATH},
            {"id": "chat-s2", "state": "ready", "supervisor_env": ENV_PATH},
        ],
    )

    assert read(tmp_path).sandbox == "chat-s10"


def test_a_creating_sandbox_does_not_serve_a_terminal(tmp_path: Path) -> None:
    """`ready` means the channel handshake passed (contract 05 §4.1)."""
    write_status(
        tmp_path, sandboxes=[{"id": "chat-s2", "state": "creating", "supervisor_env": ENV_PATH}]
    )

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX
    assert "creating" in caught.value.message


def test_a_draining_sandbox_means_a_switch_is_running(tmp_path: Path) -> None:
    write_status(
        tmp_path,
        sandboxes=[
            {"id": "chat-s1", "state": "draining", "supervisor_env": ENV_PATH},
            {"id": "chat-s2", "state": "ready", "supervisor_env": ENV_PATH},
        ],
    )

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX
    assert "switch" in caught.value.message


def test_a_reconcile_that_switches_refuses(tmp_path: Path) -> None:
    write_status(
        tmp_path,
        state="reconciling",
        reconcile={"step": "switch_sandbox", "needs_switch": True, "to_rev": "reg-9f21c4"},
    )

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX
    assert "switch_sandbox" in caught.value.message


def test_a_reconcile_that_does_not_switch_still_serves(tmp_path: Path) -> None:
    """Instructions and grants land with no sandbox change (contract 05 §3.5)."""
    write_status(
        tmp_path,
        state="reconciling",
        reconcile={"step": "write_config", "needs_switch": False},
    )

    assert read(tmp_path).sandbox == "chat-s1"


def test_a_sandbox_row_with_no_env_path_is_a_fault(tmp_path: Path) -> None:
    """Contract 05 §4.1.1 rule 4. Dialling anyway hides the cause."""
    write_status(tmp_path, sandboxes=[{"id": "chat-s1", "state": "ready"}])

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX
    assert "supervisor.env" in caught.value.message


def test_a_family_that_never_validated_refuses(tmp_path: Path) -> None:
    write_status(tmp_path, state="invalid", validation={"never_valid": True})

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX
    assert "ever validated" in caught.value.message


def test_invalid_with_a_last_good_definition_still_serves(tmp_path: Path) -> None:
    """A bad file yields a report, not an outage (contract 05 §3.1)."""
    write_status(tmp_path, state="invalid", validation={"never_valid": False})

    assert read(tmp_path).sandbox == "chat-s1"


def test_a_blocking_fault_warns_and_still_serves(tmp_path: Path) -> None:
    """A terminal is not a turn. A fault is exactly when the operator wants one."""
    write_status(
        tmp_path,
        state="degraded",
        faults=[{"code": "key_mint_failed", "blocks_turns": True}],
    )

    serving = read(tmp_path)

    assert serving.sandbox == "chat-s1"
    assert "key_mint_failed" in serving.warning


def test_no_status_document_refuses(tmp_path: Path) -> None:
    with pytest.raises(DoorError) as caught:
        read(tmp_path, "nosuch")

    assert caught.value.code is Exit.NO_SANDBOX
    assert "nosuch" in caught.value.message


def test_a_document_that_is_not_json_refuses(tmp_path: Path) -> None:
    path = tmp_path / FAMILY / "status.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX


def test_a_document_that_is_not_utf8_refuses(tmp_path: Path) -> None:
    path = write_status(tmp_path)
    path.write_bytes(b'{"kind":"attended\xff"}')

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX
    assert "no readable status document" in caught.value.message
    assert StatusFiles(tmp_path).attended() == []


def test_a_document_that_nests_too_deep_refuses(tmp_path: Path) -> None:
    """The file is under the size cap, so the reader parses it."""
    path = write_status(tmp_path)
    text = '{"kind":"attended","x":' + "[" * TOO_DEEP + "]" * TOO_DEEP + "}"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.NO_SANDBOX
    assert "no readable status document" in caught.value.message
    assert StatusFiles(tmp_path).attended() == []


def test_a_huge_document_is_refused_unread(tmp_path: Path) -> None:
    """Untrusted input is bounded before it is parsed (invariant 14)."""
    path = write_status(tmp_path)
    path.write_text(json.dumps({"pad": "x" * (400 * 1024)}), encoding="utf-8")

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert "too large" in caught.value.message


def test_a_stale_document_warns_and_still_serves(tmp_path: Path) -> None:
    """Contract 05 §2 rule 5. A stale document means `caregiver` is not running."""
    write_status(tmp_path, written_at="2020-01-01T00:00:00Z")

    assert "stale" in read(tmp_path).warning


def test_the_family_name_is_checked_before_a_path_is_built(tmp_path: Path) -> None:
    with pytest.raises(DoorError) as caught:
        read(tmp_path, "../../etc")

    assert caught.value.code is Exit.BAD_USAGE


def test_attended_is_the_only_kind_a_terminal_reaches(tmp_path: Path) -> None:
    """Contract 02 §3.1. The `door-tui` token touches attended families only."""
    write_status(tmp_path, kind="autonomous")

    with pytest.raises(DoorError) as caught:
        read(tmp_path)

    assert caught.value.code is Exit.BAD_USAGE
    assert "autonomous" in caught.value.message


def test_families_lists_every_attended_one(tmp_path: Path) -> None:
    write_status(tmp_path)
    (tmp_path / "scrum-lead").mkdir()
    (tmp_path / "scrum-lead" / "status.json").write_text(
        json.dumps({"family": "scrum-lead", "kind": "autonomous"}), encoding="utf-8"
    )

    assert StatusFiles(tmp_path).attended() == ["chat"]
