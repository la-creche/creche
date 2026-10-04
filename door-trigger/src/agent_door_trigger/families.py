"""Which families the webhook listener may ever open a route for.

Contract 05 §2: `caregiver` writes
`/srv/agents/state/rework/families/<family>/status.json` and is its only
writer. This module reads that document to answer one question per family:
has `caregiver` ever applied a valid definition for it, and is it
`autonomous`? Both must hold before this door will even look at what the
registry says that family's webhook triggers are named (`routes.py`).

CONTRACT-QUESTION: contract 05 §2.1 publishes each declared webhook in
`triggers.webhooks`, but `routes.py` takes the names from the registry
file. So validity is the one gate this door takes from the status
document: a family that has never validated can never open a route — the
severe half of "an invalid family file never opens a route". `README.md`'s
Known gaps names the narrower race this does not close.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from .untrusted import as_object, field_text, is_object

_LOG = logging.getLogger(__name__)

AUTONOMOUS = "autonomous"
_STATUS_FILE = "status.json"
_STATE_INVALID = "invalid"

#: A status document is small (contract 05 §2). A larger file is a fault,
#: not a document — the same reasoning door-owui's picker uses for its own
#: `_MAX_STATUS_BYTES`.
_MAX_STATUS_BYTES = 256 * 1024


class AutonomousFamilies(Protocol):
    """Where this door learns which families may receive a trigger."""

    def servable(self) -> frozenset[str]:
        """Autonomous family names with at least one applied, valid
        definition. Unordered: a caller that needs order sorts it."""
        ...


class StatusFiles:
    """Reads the status documents `caregiver` publishes (contract 05 §2)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def servable(self) -> frozenset[str]:
        try:
            entries = [entry for entry in self._root.iterdir() if entry.is_dir()]
        except OSError as exc:
            _LOG.warning("families: cannot list %s (%s)", self._root, exc.strerror)
            return frozenset()

        return frozenset(entry.name for entry in entries if self._is_servable(entry))

    def _is_servable(self, directory: Path) -> bool:
        status = self._read_status(directory / _STATUS_FILE)
        if status is None:
            return False
        if field_text(status, "kind") != AUTONOMOUS:
            return False

        return not _never_valid(status)

    def _read_status(self, path: Path) -> dict[str, object] | None:
        try:
            if path.stat().st_size > _MAX_STATUS_BYTES:
                _LOG.warning("families: %s is too large to be a status document", path)
                return None
            raw = path.read_bytes()
        except OSError:
            # A family directory with no readable status document is one
            # `caregiver` has not published yet.
            return None

        try:
            parsed: object = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            # ValueError covers bytes that are not UTF-8, text that is not
            # JSON and a number past the digit limit of the interpreter. A
            # document that nests too deep raises RecursionError.
            _LOG.warning("families: %s is not JSON", path)
            return None

        return parsed if is_object(parsed) else None


def _never_valid(status: dict[str, object]) -> bool:
    """True when no revision of this family ever validated."""
    if field_text(status, "state") != _STATE_INVALID:
        return False

    validation = as_object(status.get("validation"))
    return validation.get("never_valid") is True
