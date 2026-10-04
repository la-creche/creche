"""The gate's own record, one small file per family:

    /srv/agents/state/rework/triggers/quiet/<family>.json
    {"fired": <wake> | null, "good": <wake> | null}
    <wake> = {"session", "at", "board", "ended"}

`fired` is the last firing the gate let through, and `good` the last one
whose job ended `ok`: contract 01 §3.15's last good wake. Written by temp
file and rename. A file this module cannot read is an empty record, so the
gate wakes, and that firing writes a fresh one."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

from ..untrusted import as_object, as_text, field_text, is_list
from .decide import Wake

_LOG = logging.getLogger(__name__)

STATE_SUFFIX: Final = ".json"
#: A record holds two wakes of at most 200 job ids each. Far larger is not one.
MAX_STATE_BYTES: Final = 256 * 1024


@dataclass(frozen=True)
class GateState:
    fired: Wake | None = None
    good: Wake | None = None


class StateStore(Protocol):
    def read(self, family: str) -> GateState: ...

    def write(self, family: str, state: GateState) -> None:
        """Raises OSError."""
        ...


class StateFiles:
    """`StateStore` over one directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def read(self, family: str) -> GateState:
        path = self._root / f"{family}{STATE_SUFFIX}"
        try:
            if path.stat().st_size > MAX_STATE_BYTES:
                return GateState()

            body = as_object(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return GateState()
        except (OSError, ValueError, RecursionError) as exc:
            _LOG.warning("quiet: %s: unreadable gate record (%s)", family, type(exc).__name__)
            return GateState()

        return GateState(fired=_wake(body.get("fired")), good=_wake(body.get("good")))

    def write(self, family: str, state: GateState) -> None:
        body = {"fired": _plain(state.fired), "good": _plain(state.good)}
        self._root.mkdir(parents=True, exist_ok=True)

        # Temp file and rename: a reader sees the old record or the new one.
        fd, temp = tempfile.mkstemp(dir=self._root, prefix=f".{family}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(body, out)

            os.replace(temp, self._root / f"{family}{STATE_SUFFIX}")
        except OSError:
            Path(temp).unlink(missing_ok=True)
            raise


def _plain(wake: Wake | None) -> dict[str, object] | None:
    if wake is None:
        return None

    return {
        "session": wake.session,
        "at": wake.at.isoformat(),
        "board": wake.board,
        "ended": sorted(wake.ended) if wake.ended is not None else None,
    }


def _wake(value: object) -> Wake | None:
    """One stored wake, or None when it is absent or not one."""
    body = as_object(value)
    session = field_text(body, "session")
    try:
        at = datetime.fromisoformat(field_text(body, "at"))
    except ValueError:
        return None

    if not session or at.tzinfo is None:
        return None

    board = as_text(body.get("board")) or None
    return Wake(session=session, at=at, board=board, ended=_ended(body.get("ended")))


def _ended(value: object) -> frozenset[str] | None:
    """The stored job ids, or None: the gate then cannot compare, and wakes."""
    if not is_list(value) or not all(isinstance(one, str) for one in value):
        return None

    return frozenset(as_text(one) for one in value)
