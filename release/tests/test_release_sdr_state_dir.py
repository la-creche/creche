"""A server that keeps state gets its own directory.

Contract 01b §4.2: `state_dir: true` gives a server one directory, owned by
its `mcp-<name>` user, and passes the path in the variable its
`run.state_dir_env` names. A server gets no `HOME`, so without it `ha-mcp`
(`ha` and `ha-read` both) falls back to `/tmp/ha-mcp` under the PEP's
`PrivateTmp`: its state is lost at every PEP restart, and the two blocks
of one binary reach for one directory, which §4.2 forbids.

Root makes it at step 8, beside the per-server tree, and it outlives every
release:

    /var/lib/agent-mcp          root:root 0755      the visit makes it
    /var/lib/agent-mcp/<name>   mcp-<name> 0700     step 8 makes it

These drive `McpBuilder` and the roster writer against `McpFake`. The
eleven real declarations are `test_release_mce_first_mcp_release.py`'s.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Final

import pytest
from agent_release.errors import Refusal, RefusalCode
from agent_release.executor.host import MCP_STATE_ROOT, As, Host
from agent_release.executor.install import INSTALL
from agent_release.executor.mcpbuild import STATE_DIR_MODE, Fetched, McpBuilder
from agent_release.executor.roster import rows
from agent_release.mcpserver import ServerFile, parse_server
from release_mcp_fixtures import KAGI_LOCK_PATH, LOCK_TEXT, McpFake, kagi_server

TAG: Final = "mcp-servers-v0.9.4"

#: One server that keeps state. It borrows kagi's pin and committed closure,
#: so the bench's one lock serves it; the name and the state are its own.
STATEFUL: Final = "notes"
STATE_ENV: Final = "NOTES_DIR"
STATEFUL_YAML: Final = f"""
name: {STATEFUL}
identity: "A server that keeps its notes on disk."
install:
  source: pypi
  package: kagimcp
  version: 1.0.2
  lock: {KAGI_LOCK_PATH}
  python: "3.12"
run:
  entrypoint: kagimcp
  env:
    NOTES_URL: http://127.0.0.1:9
{{literal}}  state_dir: true
  state_dir_env: {STATE_ENV}
tools:
  - name: read_note
    description: Read one note.
"""

#: `STATE_DIR_MODE` as a number, which is what `stat` answers in.
STATE_MODE: Final = 0o700
#: The root, as `bin/rework-release-visit.sh` makes it.
ROOT_MODE: Final = 0o755

#: What `ha-mcp` writes into its directory on every start
#: (`docs/ha-read-mcp.md` §3). Here it is any file the server wrote.
KEPT_NAME: Final = "tool_config.json"
KEPT_BODY: Final = '{"kept": true}\n'


def _stateful(literal: str | None = None) -> ServerFile:
    """`literal` also sets the variable in `run.env`, which the file may
    do and which root's own path overrides."""
    line = f"    {STATE_ENV}: {literal}\n" if literal else ""

    return parse_server(STATEFUL_YAML.format(literal=line).encode("utf-8"), STATEFUL)


def _bench(tmp_path: Path, fake: McpFake) -> tuple[McpBuilder, Path, Fetched]:
    """A builder on a host after the visit: the state root exists, root's
    mode, and nothing is under it yet."""
    state_root = tmp_path / "var-lib-agent-mcp"
    state_root.mkdir()
    state_root.chmod(ROOT_MODE)
    mcp_root = tmp_path / "opt-mcp"
    mcp_root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    registry = tmp_path / "agent-registry"
    committed = registry / KAGI_LOCK_PATH
    committed.parent.mkdir(parents=True)
    committed.write_text(LOCK_TEXT, encoding="utf-8")
    host = Host(run=fake, clock=lambda: 0.0, sleep=lambda _: None, mcp_state_root=state_root)

    return (
        McpBuilder(host, mcp_root),
        work,
        Fetched(registry=registry, tree=tmp_path / "agent-mcp", tag=TAG),
    )


def _naming(fake: McpFake, path: Path) -> list[tuple[str, ...]]:
    """Every child whose argv names `path` or anything under it."""
    return [one.argv for one in fake.seen if any(str(path) in word for word in one.argv)]


# -- the file ---------------------------------------------------------------


def test_the_file_s_variable_is_carried_and_a_file_without_one_carries_none() -> None:
    """`mcpserver` carries both fields: a validator that dropped them would
    leave nothing downstream to act on the declaration."""
    assert _stateful().state_dir_env == STATE_ENV
    assert kagi_server().state_dir_env is None


# -- step 8 makes it ----------------------------------------------------------


def test_a_server_that_keeps_state_gets_its_directory_before_the_swap(tmp_path: Path) -> None:
    """Made at step 8 by the one child that can give a directory to another
    user, `install -d`, as root. Nothing is swapped yet: the live tree does
    not exist and the directory already does.

    It is the ONLY child that names the path. No `chown -R`, because the
    directory is the server's from birth and root never takes it back, and
    no `chmod`: `-m` sets the mode, so the umask never reaches it."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    server = _stateful()

    (build,) = builder.stage_all((server,), work, source)

    wanted = builder.state_root / STATEFUL
    assert _naming(fake, wanted) == [
        (INSTALL, "-d", "-m", STATE_DIR_MODE, "-o", server.user, "-g", server.user, str(wanted))
    ]
    made = next(one for one in fake.seen if one.argv[-1] == str(wanted))
    assert made.identity is As.ROOT
    assert wanted.is_dir()
    assert build.state_dir == wanted
    assert not build.paths.to.exists(), "the swap is step 9's"


def test_a_server_that_keeps_no_state_gets_neither_directory_nor_variable(
    tmp_path: Path,
) -> None:
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)

    (build,) = builder.stage_all((kagi_server(),), work, source)

    assert build.state_dir is None
    assert list(builder.state_root.iterdir()) == []
    assert _naming(fake, builder.state_root) == []
    row = rows((kagi_server(),), builder.mcp_root, builder.state_root)["kagi"]
    assert row["env"] == {"KAGI_API_KEY": "secret:kagi_api_key"}


def test_a_second_release_leaves_the_directory_and_its_contents_alone(tmp_path: Path) -> None:
    """State is the point. A release that emptied the directory would reset
    what the server remembers on every pin bump. `install -d` on a
    directory that exists re-asserts its owner and mode and touches nothing
    inside it, and no pass of root's own re-modes it: `normalize_dir` would
    leave it 0755."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    server = _stateful()
    (first,) = builder.stage_all((server,), work, source)
    builder.swap_all((first,))
    kept = builder.state_root / STATEFUL
    kept.chmod(STATE_MODE)
    (kept / KEPT_NAME).write_text(KEPT_BODY, encoding="utf-8")
    made = kept.stat().st_ino

    (second,) = builder.stage_all((server,), work, source)
    builder.swap_all((second,))

    assert kept.stat().st_ino == made, "the directory was made again"
    assert (kept / KEPT_NAME).read_text(encoding="utf-8") == KEPT_BODY
    assert stat.S_IMODE(kept.stat().st_mode) == STATE_MODE


def test_a_restore_takes_the_tree_and_leaves_the_directory(tmp_path: Path) -> None:
    """A first install's restore removes the tree. The state
    directory stays: removing state is a bigger verb than a release, the
    same reason a removed server's tree stays."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    (build,) = builder.stage_all((_stateful(),), work, source)
    builder.swap_in(build)

    builder.swap_back(build)

    assert not build.paths.to.exists()
    assert build.state_dir is not None
    assert build.state_dir.is_dir()


def test_the_state_root_stays_traversable(tmp_path: Path) -> None:
    """Every `mcp-<name>` that keeps state walks through the root to its own
    directory. `_make_owned_dir` opens the parent it works in, as it opens
    `/opt/mcp`, so a root somebody tightened is 0755 again."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    builder.state_root.chmod(STATE_MODE)

    builder.stage_all((_stateful(),), work, source)

    assert stat.S_IMODE(builder.state_root.stat().st_mode) == ROOT_MODE


# -- the refusals -----------------------------------------------------------


def test_a_host_without_the_state_root_is_refused_before_any_child(tmp_path: Path) -> None:
    """The root is the visit's, never the release's. The PEP's unit names it
    in `ReadWritePaths`, and systemd binds it writable only if it exists as
    the PEP starts (`ProtectSystem=strict`). A root this release made would
    be read-only to every server until the next restart, and ha-mcp falls
    back to `/tmp` without failing: a success over state that does not
    persist. So root refuses, names the visit, and runs nothing."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    builder.state_root.rmdir()

    with pytest.raises(Refusal) as refused:
        builder.stage_all((kagi_server(), _stateful()), work, source)

    assert refused.value.code is RefusalCode.SERVER
    assert refused.value.subject == f"mcp/{STATEFUL}"
    assert "bin/rework-release-visit.sh" in refused.value.detail
    assert fake.seen == [], "a child ran before the refusal"
    assert not builder.state_root.exists(), "the release made the root itself"


def test_a_registry_that_keeps_no_state_needs_no_root(tmp_path: Path) -> None:
    """The root is required by a server that keeps state, not by every
    release: a host without it still releases the other nine."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    builder.state_root.rmdir()

    (build,) = builder.stage_all((kagi_server(),), work, source)

    assert build.state_dir is None
    assert not builder.state_root.exists()


def test_a_link_where_the_directory_goes_is_refused(tmp_path: Path) -> None:
    """The root is root's and nobody else may write it, so this is belt and
    braces: `install -d` on a link re-owns the TARGET, which would hand
    the server a directory root never chose."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (builder.state_root / STATEFUL).symlink_to(elsewhere)

    with pytest.raises(Refusal, match="symlink"):
        builder.stage_all((_stateful(),), work, source)

    assert _naming(fake, builder.state_root) == []


# -- the roster names it ------------------------------------------------------


def test_the_roster_row_carries_the_variable_with_that_path(tmp_path: Path) -> None:
    """The PEP hands a row's `env` to the child as it is, so
    the variable in the row is the whole of how the server learns it."""
    fake = McpFake()
    builder, work, source = _bench(tmp_path, fake)
    (build,) = builder.stage_all((_stateful(),), work, source)

    row = rows((_stateful(),), builder.mcp_root, builder.state_root)[STATEFUL]

    assert row["env"] == {"NOTES_URL": "http://127.0.0.1:9", STATE_ENV: str(build.state_dir)}


def test_root_s_path_wins_over_a_literal_of_the_same_name() -> None:
    """A file that also sets the variable in `run.env` has said two
    things, and only one of them is a directory root made for this server
    alone. Another server's directory, or `/tmp`, is what §4.2 forbids."""
    root = Path(MCP_STATE_ROOT)

    row = rows((_stateful(literal="/tmp/elsewhere"),), Path("/opt/mcp"), root)[STATEFUL]

    assert row["env"] == {"NOTES_URL": "http://127.0.0.1:9", STATE_ENV: f"{root}/{STATEFUL}"}


def test_the_roster_s_default_root_is_the_host_s() -> None:
    """A caller that names no root gets the host's, the one `Host` and the
    visit both use."""
    assert Host(run=McpFake()).mcp_state_root == Path(MCP_STATE_ROOT)

    row = rows((_stateful(),), Path("/opt/mcp"))[STATEFUL]

    assert row["env"] == {"NOTES_URL": "http://127.0.0.1:9", STATE_ENV: f"{MCP_STATE_ROOT}/notes"}
