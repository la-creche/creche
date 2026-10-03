"""The fault file the PEP writes for `caregiver` (contract 04 §1.6).

The PEP is the only service that knows whether it is serving a family's current
grants, so it reports that in a file instead of behind an endpoint — one
writer, one reader, and no new surface on the process that holds every upstream
credential. Contract 05 §3.3.1 fixes the path, the mode, the group and the
atomic write.

`grants_stale` is the only code the PEP raises. `caregiver` drops anything else
(contract 05 §3.3.1 rule 6).

A write failure here never fails a call. The fault file reports a denial that
has already been decided; losing the report must not turn a clean deny into a
500.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from .family_ids import is_family_name, rfc3339_s

log = logging.getLogger("agent_pep.faults")

#: Contract 05 §3.3.1: the writer owns the file, the one reader gets it by
#: group. Set explicitly so the process umask cannot widen either.
FAULT_DIR_MODE: Final = 0o750
FAULT_FILE_MODE: Final = 0o640

#: Contract 05 §3.3.1 rule 7 calls a file whose `written_at` is older than 90
#: seconds stale, and `caregiver` then marks every entry `stale: true`. The PEP
#: rewrites an open fault on this interval so a live PEP never reads as a dead
#: one.
FAULT_REFRESH_S: Final = 30.0

#: The one code the PEP detects (contract 05 §3.3, contract 04 §1.6 rule 5).
GRANTS_STALE: Final = "grants_stale"

#: Contract 05 §3.3's table fixes `blocks_turns` per code.
GRANTS_STALE_BLOCKS_TURNS: Final = True

SOURCE_PEP: Final = "pep"

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Open:
    """One family's open fault, as last written."""

    message: str
    rev: str | None
    since: datetime
    written_at: datetime


def _write_atomic(path: Path, payload: str) -> None:
    """Contract 05 §3.3.1 rule 3: temporary file in the same directory,
    `fsync`, then `rename`. A reader sees the old file or the new one."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FAULT_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())

    # O_CREAT honours the umask and leaves an existing tmp file's mode alone,
    # so pin the mode rather than hope for it.
    os.chmod(tmp, FAULT_FILE_MODE)
    tmp.replace(path)

    # Contract 04 §1.3 step 4: fsync the directory so the rename survives a
    # power loss.
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class FaultWriter:
    """Per-family fault files under `<faults_dir>/pep/`.

    `directory` is the PEP's own fault directory. `None` disables reporting
    entirely, which only a test asks for.
    """

    def __init__(self, directory: Path | None, *, clock: Clock = _utc_now) -> None:
        self._dir = directory
        self._clock = clock
        self._open: dict[str, _Open] = {}
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, FAULT_DIR_MODE)

    def _path(self, family: str) -> Path | None:
        if self._dir is None:
            return None
        # The family name reaches here from a grant filename stem. Refuse
        # anything that is not a family name rather than build a path from it.
        if not is_family_name(family):
            log.error("refusing to write a fault file for %r: not a family name", family)
            return None
        return self._dir / f"{family}.json"

    def _publish(self, family: str, faults: list[dict[str, object]], now: datetime) -> bool:
        path = self._path(family)
        if path is None:
            return False
        document = {
            "family": family,
            "source": SOURCE_PEP,
            "written_at": rfc3339_s(now),
            "faults": faults,
        }
        try:
            _write_atomic(path, json.dumps(document, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.error("fault file %s could not be written: %s", path, exc)
            return False
        return True

    def raise_stale(self, family: str, message: str, rev: str | None) -> None:
        """Report `grants_stale` for one family (contract 04 §1.6 rules 1, 2).

        `rev` is the last revision the PEP parsed successfully, or None when it
        never parsed one. `since` holds across a refresh, so the reader sees
        when the fault started and not when it was last written.
        """
        now = self._clock()
        current = self._open.get(family)
        unchanged = current is not None and current.message == message and current.rev == rev
        if unchanged:
            assert current is not None  # unchanged implies a current entry
            fresh_enough = (now - current.written_at).total_seconds() < FAULT_REFRESH_S
            if fresh_enough:
                return

        since = current.since if unchanged and current is not None else now
        entry: dict[str, object] = {
            "code": GRANTS_STALE,
            "blocks_turns": GRANTS_STALE_BLOCKS_TURNS,
            "since": rfc3339_s(since),
            "source": SOURCE_PEP,
            "message": message,
            "rev": rev,
        }
        if not self._publish(family, [entry], now):
            return
        self._open[family] = _Open(message=message, rev=rev, since=since, written_at=now)

    def clear(self, family: str) -> None:
        """Contract 04 §1.6 rule 3: an empty list clears the fault.

        A family this PEP never faulted writes no file at all — contract 05
        §3.3.1 rule 5 reads a missing file as "this writer raised nothing".
        """
        if family not in self._open:
            return
        if not self._publish(family, [], self._clock()):
            return
        del self._open[family]

    def open_families(self) -> frozenset[str]:
        """Which families currently hold a fault. For tests and logging."""
        return frozenset(self._open)
