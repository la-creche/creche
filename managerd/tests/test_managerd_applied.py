"""The applied snapshot: what `classify()` compares the registry against.

`Diff` needs an OLD `FamilyFile`. The registry only holds the new one, so
the reconciler keeps a verbatim copy of the revision it last applied."""

from __future__ import annotations

from pathlib import Path

import yaml
from agent_managerd import paths
from agent_managerd.applied import forget_applied, read_applied, write_applied
from managerd_helpers import FAMILY_YAML


def family_text(**overrides: object) -> str:
    body = dict(FAMILY_YAML)
    body.update(overrides)
    return yaml.safe_dump(body)


def write_chat(tmp_path: Path, *, rev: str = "reg-aaaaaa", image: str = "sha256:one") -> None:
    write_applied(tmp_path, "chat", rev=rev, image=image, family_text=family_text())


def test_nothing_applied_yet_reads_none(tmp_path: Path) -> None:
    assert read_applied(tmp_path, "chat") is None


def test_it_round_trips_the_family_file(tmp_path: Path) -> None:
    write_chat(tmp_path)
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.rev == "reg-aaaaaa"
    assert applied.image == "sha256:one"
    assert applied.family.name == "chat"
    assert applied.family.model.router == "agent-router"


def test_it_keeps_the_text_verbatim(tmp_path: Path) -> None:
    text = family_text(description="A very specific description.")
    write_applied(tmp_path, "chat", rev="reg-aaaaaa", image="sha256:one", family_text=text)
    assert paths.applied_family_path(tmp_path, "chat").read_text(encoding="utf-8") == text


def test_a_second_write_replaces_the_first(tmp_path: Path) -> None:
    write_chat(tmp_path)
    write_applied(
        tmp_path,
        "chat",
        rev="reg-bbbbbb",
        image="sha256:two",
        family_text=family_text(description="Second."),
    )
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.rev == "reg-bbbbbb"
    assert applied.image == "sha256:two"
    assert applied.family.description == "Second."


def test_an_unparseable_snapshot_reads_none(tmp_path: Path) -> None:
    """Invariant 19's spirit on managerd's own state: a corrupt snapshot
    means "nothing applied", which makes the next pass a fresh apply. It is
    never a crash."""
    write_chat(tmp_path)
    paths.applied_family_path(tmp_path, "chat").write_text("name: [broken\n", encoding="utf-8")
    assert read_applied(tmp_path, "chat") is None


def test_a_missing_meta_file_reads_none(tmp_path: Path) -> None:
    write_chat(tmp_path)
    paths.applied_meta_path(tmp_path, "chat").unlink()
    assert read_applied(tmp_path, "chat") is None


def test_forgetting_removes_the_snapshot(tmp_path: Path) -> None:
    write_chat(tmp_path)
    forget_applied(tmp_path, "chat")
    assert read_applied(tmp_path, "chat") is None


def test_a_snapshot_carries_no_model_deadline_by_default(tmp_path: Path) -> None:
    write_chat(tmp_path)
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.model_live_at is None


def test_it_round_trips_the_model_deadline(tmp_path: Path) -> None:
    """Contract 05 §7 rule 6: a `/key/update` may be served stale for up to
    10 seconds, so the moment it becomes live outlives the pass that made
    it. The snapshot is where that moment survives a restart."""
    write_applied(
        tmp_path,
        "chat",
        rev="reg-aaaaaa",
        image="sha256:one",
        family_text=family_text(),
        model_live_at="2026-09-19T10:00:10Z",
    )
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.model_live_at == "2026-09-19T10:00:10Z"
