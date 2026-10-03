"""The MCP trees a server's own unprivileged user must enter.

A staged COMPONENT tree meets the release unit's `UMask=0027` and a build
that runs as root: `root:root 0750` at the end of it, and
`creche-noticeboard.service` (run as the operator) unable to exec its program out of what
was just installed. `install.normalize_modes` answers that for components.

It is in `mcpbuild.py` too, and in three places rather than one, because an
MCP build hands root's work to an unprivileged user twice while it builds
and the PEP enters the finished tree as a third user again.

1. **`/opt/mcp` itself.** Root makes it with `Path.mkdir`, so the umask
   decides its mode, and EVERY `mcp-<name>` must traverse it to reach
   `/opt/mcp/<name>/bin/<entrypoint>`. None of them is in group root. This
   is the silent half: every tree swaps, the verify hook is root's, the
   ledger says `succeeded`, and no server can start.
2. **The staging tree**, which the `uv pip install` child reads its pinned
   `install.lock` out of.
3. **The `agent-mcp` checkout**, which the `uv sync --frozen` child runs
   in.

A fourth mode is here too: the staged tree's own modes rest on
`normalize_modes` and not on a `chmod -R a+rX` CHILD, which a fake host
answers `0` to without touching a byte, so these tests can see the one
mode that decides whether the PEP can start a server.

A fifth is the directory ABOVE two of the three: `<work root>/<request
id>/`, which `install.fetch` makes. Opening the staging tree and the
checkout is worth nothing while the directory both sit in refuses the
same user.

Every test sets the umask inside itself, to 0077 — tighter than the unit's
own 0027 on purpose, so a mode this pass leaves alone fails loudly rather
than matching 0027's gaps by coincidence.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from agent_release.catalog import Repo
from agent_release.executor.host import Command, Host, Result, RunFn
from agent_release.executor.install import DIR_MODE, NEW_SUFFIX, Installer
from agent_release.executor.mcpbuild import Fetched, McpBuilder
from release_executor_fixtures import FakeRun, GitFake, fake_host
from release_mcp_fixtures import (
    KAGI_LOCK_PATH,
    LOCK_TEXT,
    McpFake,
    kagi_server,
    vikunja_server,
)

TAG = "mcp-servers-v0.9.4"

#: Tighter than `systemd/agent-rework-release.service`'s own `UMask=0027`
#: (module docstring).
TIGHT_UMASK = 0o077

#: Read for OTHER, and read plus execute for OTHER. `install.FILE_OTHER_R`
#: and `FILE_OTHER_RX` under the names this file asks its questions in: can
#: the server's own user READ this file, and can it RUN it.
OTHER_READ = 0o004
OTHER_READ_EXECUTE = 0o005

#: Neither group nor other may write anything root staged.
WRITE_BITS = 0o022

#: The MCP root every test builds under, named here because one test plants
#: a symlink inside a staged tree before the builder exists.
MCP_ROOT_NAME = "opt-mcp"


@contextlib.contextmanager
def tight_umask() -> Iterator[None]:
    """Sets the process umask for the block, then restores it.

    A copy of `test_release_r7r_modes.py`'s, four lines long, because a test
    module is not an import target. The rule being proved is shared; this
    helper is not the rule.
    """
    previous = os.umask(TIGHT_UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _builder(tmp_path: Path, run: RunFn) -> tuple[McpBuilder, Path, Fetched]:
    """A builder whose MCP root and work tree DO NOT EXIST yet.

    That is the difference from `test_release_r7f_mcp_build.py`'s helper,
    which makes both before the build and so hides whose umask made them.
    On the host the first `mcp-servers` release makes `/opt/mcp`, and every
    release makes its own `<work root>/<request id>/mcp`.
    """
    registry = tmp_path / "agent-registry"
    committed = registry / KAGI_LOCK_PATH
    committed.parent.mkdir(parents=True)
    committed.write_text(LOCK_TEXT, encoding="utf-8")

    return (
        McpBuilder(
            Host(run=run, clock=lambda: 0.0, sleep=lambda _: None), tmp_path / MCP_ROOT_NAME
        ),
        tmp_path / "work" / "req" / "mcp",
        Fetched(registry=registry, tree=tmp_path / "agent-mcp", tag=TAG),
    )


# -- 1. the root every server's user has to traverse --------------------


def test_the_mcp_root_a_first_release_makes_is_traversable(tmp_path: Path) -> None:
    """The silent half. `/opt/mcp` is root's, made with `Path.mkdir`, so the
    release unit's umask decides it. At 0750 the PEP's `runuser -u mcp-kagi`
    cannot reach `/opt/mcp/kagi/bin/kagimcp` at all, and nothing in the
    release notices."""
    builder, work, source = _builder(tmp_path, McpFake())

    with tight_umask():
        builder.stage_one(kagi_server(), work, source)

    assert _mode(builder.mcp_root) == DIR_MODE


def test_the_mcp_root_gains_no_write_bit(tmp_path: Path) -> None:
    """Traversable is not writable. `/opt/mcp` is where root decides which
    binary the most privileged process on the host starts (`roster.py` rule
    1), so a second writer of that directory is that decision moved."""
    builder, work, source = _builder(tmp_path, McpFake())

    with tight_umask():
        builder.stage_one(kagi_server(), work, source)

    assert _mode(builder.mcp_root) & WRITE_BITS == 0


# -- 2 and 3. what the build child reads --------------------------------


def test_the_build_child_can_read_the_closure_it_installs_from(tmp_path: Path) -> None:
    """`uv pip install --require-hashes --requirement <staging>/install.lock`
    runs as `mcp-kagi` (assumption 3). Root writes that file and every
    directory on the way to it, under its own umask, and the child has to
    walk all of them."""
    builder, work, source = _builder(tmp_path, McpFake())

    with tight_umask():
        builder.stage_one(kagi_server(), work, source)

    staging = work / "kagi"
    assert _mode(work) == DIR_MODE
    assert _mode(staging) == DIR_MODE
    assert _mode(staging / "install.lock") & OTHER_READ == OTHER_READ


def test_the_agent_mcp_tree_the_sync_child_runs_in_is_readable(tmp_path: Path) -> None:
    """`uv sync --frozen` runs as `mcp-vikunja-finance` with `cwd` set to
    the `agent-mcp` checkout this release fetched. Root cloned that tree, so
    its umask left it root's alone, and the child reads `pyproject.toml`,
    `uv.lock` and every source file in it."""
    builder, work, source = _builder(tmp_path, McpFake(entrypoint="vikunja-mcp"))

    with tight_umask():
        (source.tree / "src").mkdir(parents=True)
        (source.tree / "uv.lock").write_text("version = 1\n", encoding="utf-8")

        builder.stage_one(vikunja_server(), work, source)

    assert _mode(source.tree) == DIR_MODE
    assert _mode(source.tree / "src") == DIR_MODE
    assert _mode(source.tree / "uv.lock") & OTHER_READ == OTHER_READ


# -- the staged tree, without a chmod child ------------------------------


def test_the_staged_tree_is_readable_with_no_chmod_child(tmp_path: Path) -> None:
    """§4.2 step 4 said `chmod -R a+rX`, which is a CHILD: every test here
    runs against a fake host that answers 0 and changes nothing, so the one
    mode that decides whether the PEP can start the server was the one mode
    no test could see. It is `normalize_modes` now, in root's own process."""
    builder, work, source = _builder(tmp_path, McpFake())

    with tight_umask():
        build = builder.stage_one(kagi_server(), work, source)

    new = build.paths.new
    assert _mode(new) == DIR_MODE
    assert _mode(new / "bin") == DIR_MODE
    # The console script the PEP execs: read AND execute for other.
    assert _mode(new / "bin" / "kagimcp") & OTHER_READ_EXECUTE == OTHER_READ_EXECUTE
    # `bin/python`, which the fake writes with no execute bit at all, is
    # data as far as this pass is concerned: read only, never a program.
    assert _mode(new / "bin" / "python") & OTHER_READ_EXECUTE == OTHER_READ


def test_the_staged_tree_gains_no_write_bit(tmp_path: Path) -> None:
    """The server's own user built this tree and must stop being able to
    change it before the PEP runs it (`mcpbuild` assumption 3). Readable by
    everyone, writable by root alone."""
    builder, work, source = _builder(tmp_path, McpFake())

    with tight_umask():
        build = builder.stage_one(kagi_server(), work, source)

    new = build.paths.new
    for path in (new, new / "bin", new / "bin" / "kagimcp", new / "bin" / "python"):
        assert _mode(path) & WRITE_BITS == 0, path


def test_a_symlink_out_of_the_staged_tree_is_not_followed(tmp_path: Path) -> None:
    """`chmod -R` skipped symlinks and so does `normalize_modes`, and the
    staged tree is exactly where one lives: a relocatable venv's `bin/` can
    point at a system interpreter or a shared library outside itself."""
    outside = tmp_path / "outside-interpreter"
    outside.write_text("#!/x\n", encoding="utf-8")
    outside.chmod(0o600)

    fake = McpFake()
    staged = tmp_path / MCP_ROOT_NAME / f"kagi{NEW_SUFFIX}"
    builder, work, source = _builder(tmp_path, _planting(fake, staged / "bin" / "python3", outside))

    with tight_umask():
        builder.stage_one(kagi_server(), work, source)

    assert _mode(outside) == 0o600


def _planting(fake: McpFake, link: Path, target: Path) -> RunFn:
    """`uv venv`, plus the symlink a real relocatable venv leaves behind.

    The plant has to happen DURING the build: `stage_one` removes `<name>.
    new` before it starts, so a symlink made after one call is gone by the
    next one.
    """

    def run(command: Command) -> Result:
        result = fake(command)
        if "venv" in command.argv and not link.is_symlink():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)

        return result

    return run


# -- 5. the request directory the same children walk through ------------

SHA = "e" * 40


def test_the_request_directory_a_fetch_makes_is_traversable(tmp_path: Path) -> None:
    """The one above `/work/<request id>/mcp` and above the `agent-mcp`
    checkout: `install.fetch` makes it with
    `Path.mkdir`, so the release unit's `UMask=0027` leaves it `root:root
    0750`.

    Every child of an `mcp-servers` release runs as `mcp-<name>` (`As.MCP`,
    `mcpbuild` assumption 3) — `uv venv`, `uv pip install` reading
    `<work>/<request id>/mcp/<name>/install.lock`, and `uv sync --frozen`
    with its `cwd` in `<work>/<request id>/mcp-servers`. None of those users
    is in group root, so all three fail at step 8 on the DIRECTORY, whatever
    modes `mcpbuild` opens below it. Nothing is swapped and the ledger names a
    build that cannot read its own inputs.
    """
    run = FakeRun(dynamic=GitFake(lambda directory, _name: (directory / "src").mkdir()))
    host = fake_host(tmp_path, run)
    into = host.work_root / "01K5J8M2Q7V3X9R4T6N0B8C2DG" / "mcp-servers"

    with tight_umask():
        Installer(host).fetch(str(Repo.AGENT_MCP), SHA, into)

    assert _mode(into.parent) == DIR_MODE


def test_the_request_directory_gains_no_write_bit(tmp_path: Path) -> None:
    """Traversable is not writable. The build children are unprivileged and
    root is the only writer of the tree they read (`mcpbuild` assumption 3,
    and `install.normalize_modes`' own rule)."""
    run = FakeRun(dynamic=GitFake(lambda directory, _name: (directory / "src").mkdir()))
    host = fake_host(tmp_path, run)
    into = host.work_root / "01K5J8M2Q7V3X9R4T6N0B8C2DH" / "mcp-servers"

    with tight_umask():
        Installer(host).fetch(str(Repo.AGENT_MCP), SHA, into)

    assert _mode(into.parent) & WRITE_BITS == 0
