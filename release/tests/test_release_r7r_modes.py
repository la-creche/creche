"""A staged tree the user whose unit runs it cannot even list.

`install.Installer.normalize_modes` is what step 8 runs once everything is
written into `<install.to>.new` (the build's own output, the version stamp,
the manifest stamp), so this proves the pass itself under the tighter of the
two umasks in play: the release unit's own is 0027, and every test here uses
0077 so a mode this pass leaves alone fails loudly rather than by
coincidence matching 0027's own gaps.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from agent_release.executor.host import Host, Result
from agent_release.executor.install import DIR_MODE, Installer

#: Tighter than `systemd/agent-rework-release.service`'s own `UMask=0027`,
#: on purpose (module docstring).
TIGHT_UMASK = 0o077


@contextlib.contextmanager
def tight_umask() -> Iterator[None]:
    """Sets the process umask for the block, then restores it. A test must
    never depend on, or leak, the umask of the machine that runs it."""
    previous = os.umask(TIGHT_UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def _installer() -> Installer:
    """`normalize_modes` never calls `Host.run`, so a placeholder is enough:
    this is a filesystem pass, not a child process."""
    return Installer(Host(run=lambda command: Result(0)))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_tree_built_under_a_tight_umask_becomes_readable(tmp_path: Path) -> None:
    """The fault seen on the host, exactly: `uv sync` under the unit's umask left
    `noticeboard.new` `root:root 0750`, and `creche-noticeboard.service` (run as the operator)
    could not exec its program out of it."""
    tree = tmp_path / "noticeboard.new"
    with tight_umask():
        (tree / "bin").mkdir(parents=True)
        (tree / "lib" / "site-packages").mkdir(parents=True)
        script = tree / "bin" / "noticeboard"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o700)
        data = tree / "lib" / "site-packages" / "pkg.py"
        data.write_text("x = 1\n", encoding="utf-8")

    _installer().normalize_modes(tree)

    for directory in (tree, tree / "bin", tree / "lib", tree / "lib" / "site-packages"):
        assert _mode(directory) == DIR_MODE, directory

    # Owner-executable: gains OTHER read and execute, so `ExecStart=` and a
    # verify hook's `argv[0]` can run as the unit's own user.
    assert _mode(script) == 0o705

    # Not executable: OTHER read only. Never a program a shell could run.
    assert _mode(data) == 0o604


def test_nothing_gains_a_write_bit(tmp_path: Path) -> None:
    """Readable and, where relevant, runnable by anyone — writable by root
    alone. Root built this tree and stays the only writer."""
    tree = tmp_path / "managerd.new" / "bin"
    with tight_umask():
        tree.mkdir(parents=True)
        script = tree / "agent-managerd"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o700)

    _installer().normalize_modes(tree.parent)

    assert _mode(tree.parent) & 0o022 == 0
    assert _mode(script) & 0o022 == 0


def test_a_symlink_inside_the_tree_is_never_followed(tmp_path: Path) -> None:
    """`chmod` follows a symlink to its target, and a relocatable venv's
    tree can hold one that points outside the tree being staged — a system
    interpreter, a shared library. Leaving a mode this pass did not mean to
    touch alone is safer than changing one outside `root`."""
    outside = tmp_path / "outside-interpreter"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o700)

    tree = tmp_path / "pep.new" / "bin"
    tree.mkdir(parents=True)
    (tree / "python3").symlink_to(outside)

    _installer().normalize_modes(tmp_path / "pep.new")

    assert _mode(outside) == 0o700


def test_a_symlinked_directory_is_never_entered(tmp_path: Path) -> None:
    """The same rule, one level up: a symlinked subdirectory is not
    `dirpath` on any turn of the walk, so its contents are never reached
    and its own mode is never touched."""
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "keep"
    outside_file.write_text("x", encoding="utf-8")
    outside_file.chmod(0o600)
    outside_dir.chmod(0o700)

    tree = tmp_path / "infra.new"
    tree.mkdir()
    (tree / "linked").symlink_to(outside_dir)

    _installer().normalize_modes(tree)

    assert _mode(outside_dir) == 0o700
    assert _mode(outside_file) == 0o600
