"""The applied snapshot: what `classify()` compares the registry against.

`Diff` needs an OLD `FamilyFile`. The registry only holds the new one, so
the reconciler keeps a verbatim copy of the revision it last applied."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from agent_family import Markup, parse_family
from caregiver.applied import forget_applied, read_applied, read_applied_text, write_applied
from caregiver_helpers import FAMILY_TOML, UNREADABLE_JSON, family_toml

from caregiver import paths


def write_chat(tmp_path: Path, *, rev: str = "reg-aaaaaa", image: str = "sha256:one") -> None:
    write_applied(tmp_path, "chat", rev=rev, image=image, family_text=family_toml())


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
    text = family_toml(description="A very specific description.")
    write_applied(tmp_path, "chat", rev="reg-aaaaaa", image="sha256:one", family_text=text)
    copy = paths.applied_family_path(tmp_path, "chat")
    assert copy.read_text(encoding="utf-8") == text


def test_the_copy_has_the_name_of_the_registry_file(tmp_path: Path) -> None:
    write_applied(tmp_path, "chat", rev="reg-cccccc", image="sha256:one", family_text=FAMILY_TOML)

    copy = paths.applied_family_path(tmp_path, "chat")
    assert copy.name == "family.toml"
    assert read_applied_text(tmp_path, "chat") == FAMILY_TOML


def test_the_copy_reads_with_the_toml_parser(tmp_path: Path) -> None:
    write_applied(tmp_path, "chat", rev="reg-cccccc", image="sha256:one", family_text=FAMILY_TOML)

    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.rev == "reg-cccccc"
    assert applied.family == parse_family(FAMILY_TOML, Markup.TOML)[0]


def test_a_second_write_replaces_the_first(tmp_path: Path) -> None:
    write_chat(tmp_path)
    write_applied(
        tmp_path,
        "chat",
        rev="reg-bbbbbb",
        image="sha256:two",
        family_text=family_toml(description="Second."),
    )
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.rev == "reg-bbbbbb"
    assert applied.image == "sha256:two"
    assert applied.family.description == "Second."


def test_an_unparseable_snapshot_reads_none(tmp_path: Path) -> None:
    """Invariant 19's spirit on caregiver's own state: a corrupt snapshot
    means "nothing applied", which makes the next pass a fresh apply. It is
    never a crash."""
    write_chat(tmp_path)
    copy = paths.applied_family_path(tmp_path, "chat")
    copy.write_text('name = ["broken"\n', encoding="utf-8")
    assert read_applied(tmp_path, "chat") is None


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_a_meta_file_that_does_not_read_is_none(tmp_path: Path, raw: bytes) -> None:
    write_chat(tmp_path)
    paths.applied_meta_path(tmp_path, "chat").write_bytes(raw)
    assert read_applied(tmp_path, "chat") is None


def _copy_not_utf8(path: Path) -> None:
    path.write_bytes(b'name = "chat\xff"\n')


def _copy_a_directory(path: Path) -> None:
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize(
    "spoil", [_copy_not_utf8, _copy_a_directory], ids=["not-utf8", "a-directory"]
)
def test_a_copy_that_does_not_read_reads_none(
    tmp_path: Path, spoil: Callable[[Path], None]
) -> None:
    write_chat(tmp_path)
    spoil(paths.applied_family_path(tmp_path, "chat"))

    assert read_applied(tmp_path, "chat") is None
    assert read_applied_text(tmp_path, "chat") == ""


def test_a_missing_meta_file_reads_none(tmp_path: Path) -> None:
    write_chat(tmp_path)
    paths.applied_meta_path(tmp_path, "chat").unlink()
    assert read_applied(tmp_path, "chat") is None


def test_a_yaml_copy_is_no_copy(tmp_path: Path) -> None:
    """caregiver reads no `family.yaml` copy. A snapshot with only such a
    copy reads as nothing applied."""
    write_chat(tmp_path)
    copy = paths.applied_family_path(tmp_path, "chat")
    copy.rename(copy.with_name("family.yaml"))

    assert read_applied(tmp_path, "chat") is None
    assert read_applied_text(tmp_path, "chat") == ""


def test_forgetting_removes_the_snapshot(tmp_path: Path) -> None:
    write_chat(tmp_path)
    forget_applied(tmp_path, "chat")
    assert read_applied(tmp_path, "chat") is None
    assert read_applied_text(tmp_path, "chat") == ""


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
        family_text=family_toml(),
        model_live_at="2026-09-19T10:00:10Z",
    )
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.model_live_at == "2026-09-19T10:00:10Z"
