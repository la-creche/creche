"""Root reads only from roots root owns.

Until ROOTS the spool, the work root, the sealed secrets and the roster sat
under `/srv/agents/state/rework` and `/srv/agents/work`, both the operator's. A
operator-side process could rename either root aside and plant its own, and
root would fetch into, stage from and install out of a tree the operator chose.

Two halves are held here.

1. **The property.** Every path root WRITES, or reads as its own, resolves
   under a root-owned root. `Host`'s fields are walked, so a new path field
   must be classified before this passes. The paths root reads from the operator on
   purpose are a closed list, each with the reason it is safe.
2. **The one-time move** (`relocate.move_old`): what it carries, what it
   refuses, and that it never overwrites.

`bin/tests/test_rework_release_visit.py` holds the third half: the parents
the visit's own table makes are root's.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import stat
from pathlib import Path
from typing import Final, cast

import pytest
from agent_release.executor import layout
from agent_release.executor.host import Host, Result
from agent_release.executor.relocate import (
    MARKER_NAME,
    OLD_ROSTER_NAME,
    OLD_SECRETS_DIR,
    OLD_SPOOL_ROOT,
    OLD_STATE_ROOT,
    RETIRED_SUFFIX,
    RelocateError,
    move_old,
)
from agent_release.executor.spool import SPOOL_ROOT
from agent_release.intake.store import DEFAULT_SECRETS_DIR

#: Directories the operator owns on the host (`bin/bootstrap-root.sh`). A path root
#: trusts under either of these is a path the operator can rename aside.
OPERATOR_OWNED: Final = (Path("/srv/agents"), Path("/home/operator"))

#: The root-owned roots every trusted path must sit under. Each one's own
#: ancestors are system directories (`/`, `/var`, `/var/lib`, `/opt`, `/etc`).
ROOT_OWNED: Final = (
    Path(layout.RELEASE_ROOT),
    Path("/var/lib/agent-mcp"),
    Path("/opt/components"),
    Path("/opt/mcp"),
    Path("/etc/systemd/system"),
)

#: `Host` fields root WRITES, or reads as its own.
ROOT_TRUSTS: Final = frozenset(
    {"work_root", "system_unit_dir", "mcp_root", "roster_file", "mcp_state_root"}
)

#: `Host` fields root reads from the operator ON PURPOSE, and why each is safe.
OPERATOR_BY_DESIGN: Final = {
    # Read BY SHA: content addressed, so where it travelled cannot change
    # what it is (`executor/source.py`).
    "source_root": "the corpus, read by SHA",
    # Written as the operator, by a child running as the operator.
    "user_unit_dir": "the operator's own units, written as the operator",
    # A signal: planted records can only make root WAIT (`quiesce.py`).
    "sessions_root": "a quiet-window signal that can only delay",
    # §2.8's signal, read O_NOFOLLOW off the open descriptor (`quiet.py`).
    "attendance_socket": "a quiet-window signal that can only delay",
    "view_token_file": "a quiet-window signal that can only delay",
}

#: `install_roots` holds one of each: root's `/opt/components` and the operator's
#: `~/.local/components`, which only operator-run children write.
INSTALL_ROOTS_FIELD: Final = "install_roots"


def _is_under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _path_fields() -> dict[str, tuple[Path, ...]]:
    """Every `Host` field whose default is a path or a tuple of paths."""
    host = Host(run=lambda _: Result(0))
    found: dict[str, tuple[Path, ...]] = {}
    for one in dataclasses.fields(Host):
        value: object = getattr(host, one.name)
        if isinstance(value, Path):
            found[one.name] = (value,)
            continue

        if not isinstance(value, tuple):
            continue

        items = cast(tuple[object, ...], value)
        paths = tuple(item for item in items if isinstance(item, Path))
        if paths and len(paths) == len(items):
            found[one.name] = paths

    return found


# -- the property ----------------------------------------------------------


def test_every_path_root_trusts_sits_under_a_root_owned_root() -> None:
    """THE property of root's own root. A path root writes or reads as its own
    resolves under a root-owned root, and never under a directory the operator
    owns — so nothing the operator owns can be renamed above it."""
    fields = _path_fields()
    trusted = [path for name in ROOT_TRUSTS for path in fields[name]]
    trusted += [Path(SPOOL_ROOT), Path(DEFAULT_SECRETS_DIR)]
    trusted += [p for p in fields[INSTALL_ROOTS_FIELD] if not _is_under(p, OPERATOR_OWNED[1])]

    for path in trusted:
        assert any(_is_under(path, root) for root in ROOT_OWNED), path
        assert not any(_is_under(path, owned) for owned in OPERATOR_OWNED), path


def test_every_host_path_field_is_classified() -> None:
    """A new path field must say which side it is on before it ships."""
    known = ROOT_TRUSTS | OPERATOR_BY_DESIGN.keys() | {INSTALL_ROOTS_FIELD}

    assert set(_path_fields()) == known


def test_the_four_paths_that_moved_are_layouts() -> None:
    """One spelling each. The spool, the intake's store and the roster are
    what the PEP and `managerd` are pointed at, so a second copy that drifted
    would be a writer and a reader on two different paths."""
    fields = _path_fields()

    assert fields["work_root"] == (Path(layout.WORK_ROOT),)
    assert fields["roster_file"] == (Path(layout.ROSTER_FILE),)
    assert SPOOL_ROOT == layout.SPOOL_ROOT
    assert DEFAULT_SECRETS_DIR == layout.SECRETS_DIR


# -- the one-time move -----------------------------------------------------

LEDGER_ID: Final = "01K5YQ4C3E0B8M7W2T9V6N1H5D"
SECRET: Final = "kagi_api_key"


def _under(prefix: Path, absolute: str) -> Path:
    return prefix / absolute.lstrip("/")


@pytest.fixture
def old_host(tmp_path: Path) -> Path:
    """A host before ROOTS: the old spool with one ledgered release, one
    sealed secret, the roster, and the new root the visit already made."""
    old_spool = _under(tmp_path, OLD_SPOOL_ROOT)
    for part in ("requests", "running", "done", "rejected"):
        (old_spool / part).mkdir(parents=True)
    (old_spool / "lock").write_bytes(b"")
    (old_spool / "done" / f"{LEDGER_ID}.json").write_text('{"outcome": "succeeded"}\n')
    (old_spool / "done" / f"{LEDGER_ID}.log").write_text("step 1\n")

    old_secrets = _under(tmp_path, OLD_SECRETS_DIR)
    old_secrets.mkdir(parents=True)
    (old_secrets / f"{SECRET}.enc").write_bytes(b"sealed")

    _under(tmp_path, OLD_STATE_ROOT).joinpath(OLD_ROSTER_NAME).write_text("kagi: {}\n")

    for new in (f"{layout.SPOOL_ROOT}/done", layout.SECRETS_DIR, layout.WORK_ROOT):
        _under(tmp_path, new).mkdir(parents=True)

    return tmp_path


def test_the_move_carries_the_ledger_the_secrets_and_the_roster(old_host: Path) -> None:
    moved = move_old(old_host, os.getuid())

    done = _under(old_host, f"{layout.SPOOL_ROOT}/done")
    assert (done / f"{LEDGER_ID}.json").read_text() == '{"outcome": "succeeded"}\n'
    assert (done / f"{LEDGER_ID}.log").read_text() == "step 1\n"
    assert (_under(old_host, layout.SECRETS_DIR) / f"{SECRET}.enc").read_bytes() == b"sealed"
    assert _under(old_host, layout.ROSTER_FILE).read_text() == "kagi: {}\n"
    assert len(moved.copied) == 4
    assert moved.refused == []


def test_each_carried_file_gets_the_mode_its_writer_gives_it(old_host: Path) -> None:
    move_old(old_host, os.getuid())

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    assert mode(_under(old_host, f"{layout.SPOOL_ROOT}/done") / f"{LEDGER_ID}.json") == 0o640
    assert mode(_under(old_host, layout.SECRETS_DIR) / f"{SECRET}.enc") == 0o600
    assert mode(_under(old_host, layout.ROSTER_FILE)) == 0o644


def test_the_move_runs_once(old_host: Path) -> None:
    """A secret the operator removes from the new root after the move must stay
    removed: a second visit that copied again would resurrect it."""
    move_old(old_host, os.getuid())
    (_under(old_host, layout.SECRETS_DIR) / f"{SECRET}.enc").unlink()

    again = move_old(old_host, os.getuid())

    assert again.already
    assert not (_under(old_host, layout.SECRETS_DIR) / f"{SECRET}.enc").exists()
    marker: object = json.loads(
        _under(old_host, layout.RELEASE_ROOT).joinpath(MARKER_NAME).read_text()
    )
    assert isinstance(marker, dict)
    assert marker["copied"] == 4


def test_the_move_never_overwrites(old_host: Path) -> None:
    """A value already in the new root is newer intent than the old copy."""
    target = _under(old_host, layout.SECRETS_DIR) / f"{SECRET}.enc"
    target.write_bytes(b"pasted since")

    moved = move_old(old_host, os.getuid())

    assert target.read_bytes() == b"pasted since"
    assert f"secrets/{SECRET}.enc" in moved.kept


def test_nothing_another_uid_wrote_is_carried(old_host: Path) -> None:
    """The operator cannot create a root-owned file, so the owner is the proof.
    Here the fixture's files belong to this test's uid and the move is told
    root is somebody else: nothing may cross."""
    moved = move_old(old_host, os.getuid() + 1)

    assert moved.copied == []
    assert "done/" in moved.refused
    assert "secrets/" in moved.refused
    assert OLD_ROSTER_NAME in moved.refused
    assert not _under(old_host, layout.ROSTER_FILE).exists()


def test_a_second_link_is_not_a_file_root_wrote(old_host: Path) -> None:
    """A hard link is a name root did not give the file. Under one it
    would carry, say, the GitHub token as the Kagi key."""
    old_secrets = _under(old_host, OLD_SECRETS_DIR)
    os.link(old_secrets / f"{SECRET}.enc", old_secrets / "github_token.enc")

    moved = move_old(old_host, os.getuid())

    assert f"secrets/{SECRET}.enc" in moved.refused
    assert "secrets/github_token.enc" in moved.refused
    assert list(_under(old_host, layout.SECRETS_DIR).iterdir()) == []


def test_a_symlink_is_never_followed(
    old_host: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "planted.enc").write_bytes(b"planted")
    old_secrets = _under(old_host, OLD_SECRETS_DIR)
    (old_secrets / "planted_key.enc").symlink_to(elsewhere / "planted.enc")

    moved = move_old(old_host, os.getuid())

    assert "secrets/planted_key.enc" in moved.refused
    assert not (_under(old_host, layout.SECRETS_DIR) / "planted_key.enc").exists()


def test_a_symlinked_old_directory_carries_nothing(
    old_host: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The finding itself: the old secrets directory renamed aside and a
    link to one the operator made put in its place."""
    old_secrets = _under(old_host, OLD_SECRETS_DIR)
    old_secrets.rename(old_secrets.with_name("secrets.real"))
    planted = tmp_path_factory.mktemp("planted")
    (planted / "forged_key.enc").write_bytes(b"forged")
    old_secrets.symlink_to(planted)

    moved = move_old(old_host, os.getuid())

    assert "secrets/" in moved.refused
    assert list(_under(old_host, layout.SECRETS_DIR).iterdir()) == []


def test_a_symlinked_old_state_root_carries_no_roster(
    old_host: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The roster is the one file whose directory is the operator's. With that
    directory swapped for a link, nothing in it is root's to vouch for."""
    state = _under(old_host, OLD_STATE_ROOT)
    state.rename(state.with_name("rework.real"))
    planted = tmp_path_factory.mktemp("planted-state")
    (planted / OLD_ROSTER_NAME).write_text("evil: {command: /bin/sh}\n")
    state.symlink_to(planted)

    moved = move_old(old_host, os.getuid())

    assert OLD_ROSTER_NAME in moved.refused
    assert not _under(old_host, layout.ROSTER_FILE).exists()


def test_a_name_its_own_module_would_not_write_is_left(old_host: Path) -> None:
    done = _under(old_host, f"{OLD_SPOOL_ROOT}/done")
    (done / ".01K5YQ4C3E0B8M7W2T9V6N1H5D.json.tmp").write_text("half")
    (done / "notes.txt").write_text("hello")

    move_old(old_host, os.getuid())

    carried = sorted(p.name for p in _under(old_host, f"{layout.SPOOL_ROOT}/done").iterdir())
    assert carried == [f"{LEDGER_ID}.json", f"{LEDGER_ID}.log"]


def test_a_request_left_in_the_old_spool_stops_the_move(old_host: Path) -> None:
    """The new executor reads the new spool, so an old request would never
    be drained. The move refuses before it copies anything."""
    (_under(old_host, f"{OLD_SPOOL_ROOT}/requests") / f"{LEDGER_ID}.json").write_text("{}")

    with pytest.raises(RelocateError, match="Drain the old spool first"):
        move_old(old_host, os.getuid())

    assert not _under(old_host, layout.ROSTER_FILE).exists()
    assert not _under(old_host, layout.RELEASE_ROOT).joinpath(MARKER_NAME).exists()


def test_a_switch_note_left_in_the_old_spool_stops_the_move(old_host: Path) -> None:
    (_under(old_host, f"{OLD_SPOOL_ROOT}/running") / f"{LEDGER_ID}-pep.switch").write_text("{}")

    with pytest.raises(RelocateError, match="unfinished switch"):
        move_old(old_host, os.getuid())


def test_a_release_holding_the_old_lock_stops_the_move(old_host: Path) -> None:
    fd = os.open(_under(old_host, f"{OLD_SPOOL_ROOT}/lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RelocateError, match="holds the old spool lock"):
            move_old(old_host, os.getuid())
    finally:
        os.close(fd)


def test_the_old_spool_is_renamed_aside(old_host: Path) -> None:
    """An old `managerd` then finds no `requests/` and stays quiet, instead
    of filing into a spool nothing drains any more."""
    moved = move_old(old_host, os.getuid())

    assert not _under(old_host, OLD_SPOOL_ROOT).exists()
    assert _under(old_host, f"{OLD_SPOOL_ROOT}{RETIRED_SUFFIX}").is_dir()
    assert "renamed" in moved.retired


def test_the_old_secrets_and_roster_stay_where_they_were(old_host: Path) -> None:
    """Copied, never moved: `retire-old.md` removes them once the one
    process that still reads them, an old `managerd`, is released."""
    move_old(old_host, os.getuid())

    assert (_under(old_host, OLD_SECRETS_DIR) / f"{SECRET}.enc").is_file()
    assert _under(old_host, OLD_STATE_ROOT).joinpath(OLD_ROSTER_NAME).is_file()


def test_a_host_with_nothing_old_moves_nothing(tmp_path: Path) -> None:
    _under(tmp_path, layout.RELEASE_ROOT).mkdir(parents=True)

    moved = move_old(tmp_path, os.getuid())

    assert moved.copied == []
    assert moved.refused == []
    assert _under(tmp_path, layout.RELEASE_ROOT).joinpath(MARKER_NAME).is_file()


def test_the_move_needs_the_new_root(tmp_path: Path) -> None:
    with pytest.raises(RelocateError, match="The visit makes it first"):
        move_old(tmp_path, os.getuid())
