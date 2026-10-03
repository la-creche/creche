"""Which families the picker offers, read from `managerd`'s status files.

Contract 05 §2: `managerd` writes
`/srv/agents/state/rework/families/<family>/status.json` and is its only
writer. A reader lists that directory to find every family, so no index
exists and no index can go stale.

The picker is NOT health-gated. A family that is reconciling, degraded or
running on its last good definition still appears, because a family that
vanishes from the picker while it restarts looks deleted to the person
typing. One state is excluded: a family whose very first definition never
validated has nothing to serve at all (contract 05 §3.1).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from .openai_api import is_family_name
from .untrusted import field_text, is_object

_LOG = logging.getLogger(__name__)

# The Open WebUI door writes `owui-*` sessions into attended families and
# nothing else (contract 02 §3.1). A thin or autonomous family in the picker
# would be a request `attendance` refuses.
ATTENDED = "attended"

_STATUS_FILE = "status.json"
_STATE_INVALID = "invalid"

# A status document is small. A larger file is a fault, not a document.
_MAX_STATUS_BYTES = 256 * 1024


class FamilyDirectory(Protocol):
    """Where the door learns which families exist."""

    def serving(self) -> list[str]:
        """Attended family names the picker should show, sorted."""
        ...


class StatusFiles:
    """Reads the status documents `managerd` publishes."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def serving(self) -> list[str]:
        try:
            entries = sorted(entry for entry in self._root.iterdir() if entry.is_dir())
        except OSError as exc:
            # No directory means no family is configured yet. An empty picker
            # is the honest answer, and the door still starts.
            _LOG.warning("families: cannot list %s (%s)", self._root, exc.strerror)
            return []

        return [entry.name for entry in entries if self._is_servable(entry)]

    def _is_servable(self, directory: Path) -> bool:
        if not is_family_name(directory.name):
            _LOG.warning("families: ignoring %s, not a family name", directory.name)
            return False

        status = self._read_status(directory / _STATUS_FILE)
        if status is None:
            return False
        if field_text(status, "kind") != ATTENDED:
            return False

        return not _never_valid(status)

    def _read_status(self, path: Path) -> dict[str, object] | None:
        try:
            if path.stat().st_size > _MAX_STATUS_BYTES:
                _LOG.warning("families: %s is too large to be a status document", path)
                return None
            raw = path.read_text(encoding="utf-8")
        except OSError:
            # A family directory with no readable status document is one
            # `managerd` has not published yet.
            return None

        try:
            parsed: object = json.loads(raw)
        except ValueError:
            _LOG.warning("families: %s is not JSON", path)
            return None

        return parsed if is_object(parsed) else None


def _never_valid(status: dict[str, object]) -> bool:
    """True when no revision of this family ever validated."""
    if field_text(status, "state") != _STATE_INVALID:
        return False

    validation = status.get("validation")

    return is_object(validation) and validation.get("never_valid") is True
