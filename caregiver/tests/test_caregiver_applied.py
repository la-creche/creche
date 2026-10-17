"""The applied snapshot: what `classify()` compares the registry against.

`Diff` needs an OLD `FamilyFile`. The registry only holds the new one, so
the reconciler keeps a verbatim copy of the revision it last applied."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from agent_family import Markup, parse_family
from agent_family.registry import FAMILY_FILE
from caregiver.applied import forget_applied, read_applied, read_applied_text, write_applied
from caregiver.atomic import atomic_write
from caregiver_helpers import FAMILY_TOML, FAMILY_YAML, UNREADABLE_JSON

from caregiver import applied as applied_module
from caregiver import paths


def family_text(**overrides: object) -> str:
    body = dict(FAMILY_YAML)
    body.update(overrides)
    return yaml.safe_dump(body)


def write_chat(tmp_path: Path, *, rev: str = "reg-aaaaaa", image: str = "sha256:one") -> None:
    write_applied(
        tmp_path, "chat", rev=rev, image=image, family_text=family_text(), markup=Markup.YAML
    )


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
    write_applied(
        tmp_path, "chat", rev="reg-aaaaaa", image="sha256:one", family_text=text, markup=Markup.YAML
    )
    yaml_copy = paths.applied_family_path(tmp_path, "chat", Markup.YAML)
    assert yaml_copy.read_text(encoding="utf-8") == text


def test_a_second_write_replaces_the_first(tmp_path: Path) -> None:
    write_chat(tmp_path)
    write_applied(
        tmp_path,
        "chat",
        rev="reg-bbbbbb",
        image="sha256:two",
        family_text=family_text(description="Second."),
        markup=Markup.YAML,
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
    yaml_copy = paths.applied_family_path(tmp_path, "chat", Markup.YAML)
    yaml_copy.write_text("name: [broken\n", encoding="utf-8")
    assert read_applied(tmp_path, "chat") is None


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_a_meta_file_that_does_not_read_is_none(tmp_path: Path, raw: bytes) -> None:
    write_chat(tmp_path)
    paths.applied_meta_path(tmp_path, "chat").write_bytes(raw)
    assert read_applied(tmp_path, "chat") is None


def test_a_snapshot_that_is_not_utf8_reads_none(tmp_path: Path) -> None:
    write_chat(tmp_path)
    paths.applied_family_path(tmp_path, "chat", Markup.YAML).write_bytes(b"name: chat\xff\n")
    assert read_applied(tmp_path, "chat") is None
    assert read_applied_text(tmp_path, "chat") == ""


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
        markup=Markup.YAML,
        model_live_at="2026-09-19T10:00:10Z",
    )
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.model_live_at == "2026-09-19T10:00:10Z"


# --- the name of the copy follows the format of its text -------------------------


class Stopped(Exception):
    """The process stops at this point of a write."""


def write_toml_chat(tmp_path: Path, text: str = FAMILY_TOML) -> None:
    write_applied(
        tmp_path, "chat", rev="reg-cccccc", image="sha256:one", family_text=text, markup=Markup.TOML
    )


def stop_after_the_toml_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write of the TOML copy completes, and the process stops before
    the next step of `write_applied`."""

    def write_then_stop(path: Path, data: bytes, *, mode: int) -> None:
        atomic_write(path, data, mode=mode)
        if path.name == FAMILY_FILE:
            raise Stopped

    monkeypatch.setattr(applied_module, "atomic_write", write_then_stop)


def test_a_toml_copy_keeps_its_text_under_the_toml_name(tmp_path: Path) -> None:
    write_toml_chat(tmp_path)

    toml_copy = paths.applied_family_path(tmp_path, "chat", Markup.TOML)
    assert toml_copy.name == "family.toml"
    assert toml_copy.read_text(encoding="utf-8") == FAMILY_TOML
    assert read_applied_text(tmp_path, "chat") == FAMILY_TOML


def test_a_toml_copy_reads_with_the_toml_parser(tmp_path: Path) -> None:
    write_toml_chat(tmp_path)

    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.rev == "reg-cccccc"
    assert applied.family == parse_family(family_text())[0]


def test_a_toml_copy_removes_the_yaml_copy(tmp_path: Path) -> None:
    write_chat(tmp_path)
    write_toml_chat(tmp_path)

    assert not paths.applied_family_path(tmp_path, "chat", Markup.YAML).exists()


def test_a_yaml_copy_removes_the_toml_copy(tmp_path: Path) -> None:
    """A registry file that goes back to YAML gives a YAML copy again."""
    write_toml_chat(tmp_path)
    write_chat(tmp_path, rev="reg-dddddd")

    assert not paths.applied_family_path(tmp_path, "chat", Markup.TOML).exists()
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.rev == "reg-dddddd"


def test_a_stop_between_the_write_and_the_remove_leaves_both_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next read takes the TOML copy, which is the newer one."""
    write_applied(
        tmp_path,
        "chat",
        rev="reg-aaaaaa",
        image="sha256:one",
        family_text=family_text(description="Old."),
        markup=Markup.YAML,
    )
    stop_after_the_toml_copy(monkeypatch)
    new_text = FAMILY_TOML.replace("Test family.", "New.")

    with pytest.raises(Stopped):
        write_toml_chat(tmp_path, new_text)

    assert paths.applied_family_path(tmp_path, "chat", Markup.YAML).is_file()
    assert paths.applied_family_path(tmp_path, "chat", Markup.TOML).is_file()
    applied = read_applied(tmp_path, "chat")
    assert applied is not None
    assert applied.family.description == "New."
    assert read_applied_text(tmp_path, "chat") == new_text


def test_a_toml_copy_that_does_not_parse_reads_none(tmp_path: Path) -> None:
    """The same answer as for a YAML copy that does not parse: nothing
    applied, and the next pass is a fresh apply."""
    write_toml_chat(tmp_path)
    toml_copy = paths.applied_family_path(tmp_path, "chat", Markup.TOML)
    toml_copy.write_text('name = ["broken"\n', encoding="utf-8")

    assert read_applied(tmp_path, "chat") is None


def test_a_toml_copy_that_does_not_parse_hides_the_yaml_copy(tmp_path: Path) -> None:
    """The read takes the TOML copy first. It does not fall back to the YAML
    copy, which is an older revision."""
    write_chat(tmp_path)
    paths.applied_family_path(tmp_path, "chat", Markup.TOML).write_text(
        'name = ["broken"\n', encoding="utf-8"
    )

    assert read_applied(tmp_path, "chat") is None


def _toml_copy_not_utf8(path: Path) -> None:
    path.write_bytes(b'name = "chat\xff"\n')


def _toml_copy_a_directory(path: Path) -> None:
    path.mkdir()


@pytest.mark.parametrize(
    "spoil",
    [_toml_copy_not_utf8, _toml_copy_a_directory],
    ids=["not-utf8", "a-directory"],
)
def test_a_toml_copy_that_does_not_read_hides_the_yaml_copy(
    tmp_path: Path, spoil: Callable[[Path], None]
) -> None:
    """A TOML copy that is there and does not read is nothing applied. The
    read does not take the YAML copy, which is an older revision."""
    write_chat(tmp_path)
    spoil(paths.applied_family_path(tmp_path, "chat", Markup.TOML))

    assert read_applied(tmp_path, "chat") is None
    assert read_applied_text(tmp_path, "chat") == ""


def test_forgetting_removes_both_copies(tmp_path: Path) -> None:
    write_chat(tmp_path)
    paths.applied_family_path(tmp_path, "chat", Markup.TOML).write_text(
        FAMILY_TOML, encoding="utf-8"
    )
    forget_applied(tmp_path, "chat")

    assert read_applied(tmp_path, "chat") is None
    assert read_applied_text(tmp_path, "chat") == ""
