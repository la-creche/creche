"""Root reads only from roots root owns.

Until ROOTS the spool, the work root, the sealed secrets and the roster sat
under `/srv/agents/state/rework` and `/srv/agents/work`, both the operator's. A
operator-side process could rename either root aside and plant its own, and
root would fetch into, stage from and install out of a tree the operator chose.

The property held here: every path root WRITES, or reads as its own,
resolves under a root-owned root. `Host`'s fields are walked, so a new path
field must be classified before this passes. The paths root reads from the
operator on purpose are a closed list, each with the reason it is safe.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Final, cast

from handover.executor import layout
from handover.executor.host import Host, Result
from handover.executor.spool import SPOOL_ROOT
from handover.intake.store import DEFAULT_SECRETS_DIR

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
    what the PEP and `caregiver` are pointed at, so a second copy that drifted
    would be a writer and a reader on two different paths."""
    fields = _path_fields()

    assert fields["work_root"] == (Path(layout.WORK_ROOT),)
    assert fields["roster_file"] == (Path(layout.ROSTER_FILE),)
    assert SPOOL_ROOT == layout.SPOOL_ROOT
    assert DEFAULT_SECRETS_DIR == layout.SECRETS_DIR
