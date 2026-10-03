"""`families.py`: which families' status makes them eligible for a
webhook route at all, read from caregiver's published status document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_door_trigger.families import StatusFiles


def _write_status(root: Path, family: str, **fields: Any) -> None:
    directory = root / family
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "family": family,
        "kind": "autonomous",
        "state": "in_sync",
        "written_at": "2026-09-18T19:20:11Z",
    }
    document.update(fields)
    (directory / "status.json").write_text(json.dumps(document), encoding="utf-8")


def test_an_autonomous_validated_family_is_servable(tmp_path: Path) -> None:
    _write_status(tmp_path, "scrum-lead")

    assert StatusFiles(tmp_path).servable() == frozenset({"scrum-lead"})


def test_a_restarting_family_stays_servable(tmp_path: Path) -> None:
    # Not health-gated on the live restart states, the same reasoning
    # door-owui's picker uses: reconciling/degraded/invalid-with-a-last-
    # good-definition all still have SOMETHING valid behind them.
    _write_status(tmp_path, "reconciling-family", state="reconciling")
    _write_status(tmp_path, "degraded-family", state="degraded", faults=[{"code": "spend_unknown"}])
    _write_status(
        tmp_path, "invalid-but-was-good", state="invalid", validation={"never_valid": False}
    )

    assert StatusFiles(tmp_path).servable() == {
        "reconciling-family",
        "degraded-family",
        "invalid-but-was-good",
    }


def test_a_family_that_never_validated_is_not_servable(tmp_path: Path) -> None:
    _write_status(tmp_path, "scrum-lead")
    _write_status(tmp_path, "broken", state="invalid", validation={"never_valid": True})

    assert StatusFiles(tmp_path).servable() == frozenset({"scrum-lead"})


def test_other_kinds_are_not_servable(tmp_path: Path) -> None:
    # This door fires jobs into autonomous families only (contract 02
    # §3.1: door-trigger's token creates auto-* sessions in autonomous
    # families and nothing else).
    _write_status(tmp_path, "scrum-lead")
    _write_status(tmp_path, "chat", kind="attended")
    _write_status(tmp_path, "vault-oracle", kind="thin")

    assert StatusFiles(tmp_path).servable() == frozenset({"scrum-lead"})


def test_an_unpublished_family_is_not_servable(tmp_path: Path) -> None:
    (tmp_path / "pending").mkdir()
    _write_status(tmp_path, "scrum-lead")

    assert StatusFiles(tmp_path).servable() == frozenset({"scrum-lead"})


def test_junk_in_the_directory_is_ignored(tmp_path: Path) -> None:
    _write_status(tmp_path, "scrum-lead")
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "status.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "loose-file.json").write_text("{}", encoding="utf-8")

    assert StatusFiles(tmp_path).servable() == frozenset({"scrum-lead"})


def test_a_missing_root_is_an_empty_set(tmp_path: Path) -> None:
    assert StatusFiles(tmp_path / "absent").servable() == frozenset()


def test_an_oversized_status_document_is_ignored(tmp_path: Path) -> None:
    _write_status(tmp_path, "scrum-lead")
    _write_status(tmp_path, "huge", labels={"pad": "x" * 300_000})

    assert StatusFiles(tmp_path).servable() == frozenset({"scrum-lead"})
