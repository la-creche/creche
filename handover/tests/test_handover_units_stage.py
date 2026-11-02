"""Step 8 of a `host` release: the staged tree is the fetched tree, copied.

`install.py` rule 10 and contract 06 §8.3. Root runs no build for a
`kind: units` component. It copies each directory and each regular file of
the fetched tree into `<install.to>.new`, and leaves out the clone's `.git`.
A link or any other entry stops the stage.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
from handover.catalog import Kind
from handover.executor.install import Installer, StepFailed, paths_of
from handover.manifest import ComponentManifest, Markup, parse_manifest
from handover_executor_fixtures import FakeRun, fake_host
from handover_fixtures import HOST_MANIFEST

HOOK = "bin/host-verify"
TIMER = "units/user/creche-follow.timer"
DROP_IN = "units/system/creche-chaperone.service.d/creche.conf"


def _manifest(tmp_path: Path) -> ComponentManifest:
    """The manifest of creche-release, with its paths under `tmp_path`."""
    text = HOST_MANIFEST.replace("/opt/components/host", str(tmp_path / "components" / "host"))

    return parse_manifest(text, "component.toml", Markup.TOML)


def _fetched(tmp_path: Path) -> Path:
    """A fetched tree as `Installer.fetch` leaves it: the clone's `.git`,
    the manifest, the verify hook and two unit files."""
    source = tmp_path / "work" / "request" / "host"
    (source / ".git" / "objects").mkdir(parents=True)
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / "component.toml").write_text(HOST_MANIFEST, encoding="utf-8")
    for name in (HOOK, TIMER, DROP_IN):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(f"# {name}\n", encoding="utf-8")

    (source / HOOK).chmod(0o755)

    return source


def _stage(tmp_path: Path, source: Path) -> tuple[Path, FakeRun]:
    manifest = _manifest(tmp_path)
    run = FakeRun()
    host = fake_host(tmp_path, run)
    paths = paths_of(manifest, host.install_roots)
    Installer(host).build(manifest, source, paths)

    return paths.new, run


def test_the_manifest_is_a_units_manifest(tmp_path: Path) -> None:
    assert _manifest(tmp_path).kind is Kind.UNITS


def test_the_staged_tree_is_the_fetched_tree(tmp_path: Path) -> None:
    new, _ = _stage(tmp_path, _fetched(tmp_path))

    staged = sorted(str(path.relative_to(new)) for path in new.rglob("*") if path.is_file())

    assert staged == sorted(["component.toml", HOOK, DROP_IN, TIMER])
    assert (new / TIMER).read_text(encoding="utf-8") == f"# {TIMER}\n"


def test_the_clone_git_directory_stays_out(tmp_path: Path) -> None:
    new, _ = _stage(tmp_path, _fetched(tmp_path))

    assert not (new / ".git").exists()


def test_the_verify_hook_keeps_its_execute_bit(tmp_path: Path) -> None:
    new, _ = _stage(tmp_path, _fetched(tmp_path))

    assert (new / HOOK).stat().st_mode & stat.S_IXUSR


def test_the_stage_runs_no_child(tmp_path: Path) -> None:
    """Root runs no build for a `units` tree: the copy is the whole stage."""
    _, run = _stage(tmp_path, _fetched(tmp_path))

    assert run.seen == []


def test_a_tree_with_no_verify_hook_fails_the_stage(tmp_path: Path) -> None:
    source = _fetched(tmp_path)
    (source / HOOK).unlink()

    with pytest.raises(StepFailed, match="verify hook is missing"):
        _stage(tmp_path, source)


def test_a_link_to_a_file_fails_the_stage(tmp_path: Path) -> None:
    source = _fetched(tmp_path)
    (tmp_path / "outside").write_text("not in the tree\n", encoding="utf-8")
    (source / "units" / "user" / "x.service").symlink_to(tmp_path / "outside")

    with pytest.raises(StepFailed, match=re.escape("units/user/x.service, which is not")):
        _stage(tmp_path, source)


def test_a_link_to_a_directory_fails_the_stage(tmp_path: Path) -> None:
    """`os.walk` lists a link to a directory among the directories. The
    copy checks each one again, so the link is not walked into."""
    source = _fetched(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (source / "units" / "linked").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(StepFailed, match="units/linked, which is not"):
        _stage(tmp_path, source)


def test_a_link_inside_the_tree_fails_the_stage_too(tmp_path: Path) -> None:
    source = _fetched(tmp_path)
    (source / "units" / "user" / "again.timer").symlink_to("creche-follow.timer")

    with pytest.raises(StepFailed, match=re.escape("units/user/again.timer, which is not")):
        _stage(tmp_path, source)


def test_a_fifo_fails_the_stage(tmp_path: Path) -> None:
    source = _fetched(tmp_path)
    os.mkfifo(source / "bin" / "pipe")

    with pytest.raises(StepFailed, match="bin/pipe, which is not"):
        _stage(tmp_path, source)


def test_a_git_name_below_the_top_is_copied(tmp_path: Path) -> None:
    """Only the clone's own `.git` stays out. Any other entry of that name
    is part of the repository's tree."""
    source = _fetched(tmp_path)
    (source / "bin" / ".git").write_text("a file of the repository\n", encoding="utf-8")

    new, _ = _stage(tmp_path, source)

    assert (new / "bin" / ".git").is_file()


def test_a_second_stage_replaces_the_first(tmp_path: Path) -> None:
    source = _fetched(tmp_path)
    new, _ = _stage(tmp_path, source)
    (new / "left-over").write_text("from the first stage\n", encoding="utf-8")

    new, _ = _stage(tmp_path, source)

    assert not (new / "left-over").exists()
