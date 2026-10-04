"""The picker: which families `/v1/models` offers, and which it does not."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agent_door_owui import families
from agent_door_owui.families import StatusFiles
from owui_json_limit import ParserAtItsLimit


def _write_status(root: Path, family: str, **fields: Any) -> None:
    directory = root / family
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "family": family,
        "kind": "attended",
        "state": "in_sync",
        "written_at": "2026-09-18T19:20:11Z",
    }
    document.update(fields)
    (directory / "status.json").write_text(json.dumps(document), encoding="utf-8")


def test_every_attended_family_is_offered(tmp_path: Path) -> None:
    _write_status(tmp_path, "chat")
    _write_status(tmp_path, "code")

    assert StatusFiles(tmp_path).serving() == ["chat", "code"]


def test_a_restarting_family_stays_in_the_picker(tmp_path: Path) -> None:
    # Never health-gated: a family that vanishes while it reconciles looks
    # deleted to the person typing.
    _write_status(tmp_path, "chat", state="reconciling")
    _write_status(tmp_path, "code", state="degraded", faults=[{"code": "orphan_processes"}])
    _write_status(tmp_path, "notes", state="invalid", validation={"never_valid": False})
    _write_status(tmp_path, "stale", written_at="2020-01-01T00:00:00Z")

    assert StatusFiles(tmp_path).serving() == ["chat", "code", "notes", "stale"]


def test_a_family_that_never_validated_is_not_offered(tmp_path: Path) -> None:
    _write_status(tmp_path, "chat")
    _write_status(tmp_path, "broken", state="invalid", validation={"never_valid": True})

    assert StatusFiles(tmp_path).serving() == ["chat"]


def test_other_kinds_are_not_offered(tmp_path: Path) -> None:
    # This door writes `owui-*` sessions into attended families only.
    _write_status(tmp_path, "chat")
    _write_status(tmp_path, "vault-oracle", kind="thin")
    _write_status(tmp_path, "scrum-lead", kind="autonomous")

    assert StatusFiles(tmp_path).serving() == ["chat"]


def test_an_unpublished_family_is_not_offered(tmp_path: Path) -> None:
    (tmp_path / "pending").mkdir()
    _write_status(tmp_path, "chat")

    assert StatusFiles(tmp_path).serving() == ["chat"]


def test_junk_in_the_directory_is_ignored(tmp_path: Path) -> None:
    _write_status(tmp_path, "chat")
    (tmp_path / "Not A Family").mkdir()
    (tmp_path / "Not A Family" / "status.json").write_text("{}", encoding="utf-8")
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "status.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "loose-file.json").write_text("{}", encoding="utf-8")

    assert StatusFiles(tmp_path).serving() == ["chat"]


def test_a_missing_root_is_an_empty_picker(tmp_path: Path) -> None:
    assert StatusFiles(tmp_path / "absent").serving() == []


def test_an_oversized_status_document_is_ignored(tmp_path: Path) -> None:
    _write_status(tmp_path, "chat")
    _write_status(tmp_path, "huge", labels={"pad": "x" * 300_000})

    assert StatusFiles(tmp_path).serving() == ["chat"]


def test_a_document_that_is_not_utf8_is_ignored(tmp_path: Path) -> None:
    _write_status(tmp_path, "chat")
    other = tmp_path / "other"
    other.mkdir()
    (other / "status.json").write_bytes(b'{"kind":"attended\xff"}')

    assert StatusFiles(tmp_path).serving() == ["chat"]


def test_a_document_that_nests_too_deep_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_status(tmp_path, "chat")
    monkeypatch.setattr(families, "json", ParserAtItsLimit)

    assert StatusFiles(tmp_path).serving() == []
