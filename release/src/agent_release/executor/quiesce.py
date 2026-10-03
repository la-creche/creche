"""Step 7: no turn mid-start before anything is touched (§2.4 row 7).

A turn that is already running for minutes does not block a release: waiting
for it would make the window unreachable. A turn that STARTED in the last
`LAUNCH_GRACE_S` does, because that is the window in which a sandbox is
still coming up and a restart of the host side would strand it.

The evidence is the turn record `sessiond` writes at
`/srv/agents/sessions/<family>/<session>/turns/<turn>.json` (contract 02
§9), which carries `state` and `started_at`. Root reads it the way it reads
every operator-owned file: capped, and a record it cannot parse informs nothing
rather than blocking forever.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

TURNS_DIR: Final = "turns"
TURN_SUFFIX: Final = ".json"

#: Contract 02's terminal turn states. Anything else is in flight.
TERMINAL_STATES: Final = frozenset({"settled", "failed", "aborted"})

#: How long after `started_at` a turn counts as mid-start: a launch grace
#: measured on the host.
LAUNCH_GRACE_S: Final = 90.0

#: A turn record is one page.
MAX_TURN_BYTES: Final = 64 * 1024

#: How many records one pass reads before it stops looking. A host with more
#: in-flight turns than this is busy by any definition.
MAX_TURNS_SCANNED: Final = 2000

#: How many DIRECTORY ENTRIES one pass looks at, at every level, before it
#: stops walking. `MAX_TURNS_SCANNED` bounded the reads and not the LIST:
#: the sessions tree is operator-writable, so `glob` made root materialize and
#: sort as many paths as a writer cared to create. One budget spent per
#: entry, at every level, is the shape that cannot be grown from outside.
MAX_SCAN_ENTRIES: Final = 20000

RFC3339_FORMATS: Final = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")


class Quiesce:
    """Step 7's watcher, held across the polls of ONE wait.

    `started_at` is an operator-written field, so a record that keeps rolling
    it forward stays "mid-start" forever: one such record failed every
    release at step 7, AFTER the tap, and root could not tell the lie from
    a busy host.

    The honest signal is root's own. This object remembers when IT first
    saw each turn in flight, and stops counting a turn once root has been
    watching it for `LAUNCH_GRACE_S`. A rolling `started_at` therefore buys
    one grace period and no more, and a genuinely starting turn still gets
    its full window — because root saw it start too.
    """

    def __init__(self) -> None:
        self._first_seen: dict[str, float] = {}

    def starting(
        self, sessions_root: Path, now: float, budget: int = MAX_SCAN_ENTRIES
    ) -> tuple[str, ...]:
        """Every turn mid-start, as `<family>/<session>/<turn>`, in name
        order. The walk is bounded at every level by `budget`."""
        found: list[str] = []
        for record in _walk(sessions_root, budget):
            key = f"{record.parts[-4]}/{record.parts[-3]}/{record.stem}"
            if not _in_flight(record, now):
                self._first_seen.pop(key, None)
                continue

            since = self._first_seen.setdefault(key, now)
            if now - since < LAUNCH_GRACE_S:
                found.append(key)

        return tuple(sorted(found))


def starting(sessions_root: Path, now: float) -> tuple[str, ...]:
    """One pass with no memory. A caller that asks once — the noticeboard, a
    doctor check — needs no watcher, and a lie costs it one answer."""
    return Quiesce().starting(sessions_root, now)


def _walk(sessions_root: Path, budget: int) -> Iterator[Path]:
    """Every `<family>/<session>/turns/*.json`, one budget unit per entry.

    Nothing is sorted and nothing is materialized whole: `scandir` yields
    lazily, so a directory with a million entries costs the budget and not
    the memory. The order is the filesystem's, which is fine because the
    ANSWER is a set and the caller sorts it.
    """
    reads = 0
    for family in _entries(sessions_root):
        budget -= 1
        if budget <= 0:
            return

        for session in _entries(Path(family)):
            budget -= 1
            if budget <= 0:
                return

            for record in _entries(Path(session) / TURNS_DIR):
                budget -= 1
                if budget <= 0 or reads >= MAX_TURNS_SCANNED:
                    return

                if record.endswith(TURN_SUFFIX):
                    reads += 1
                    yield Path(record)


def _entries(directory: Path) -> Iterator[str]:
    """One level, lazily. A directory root cannot open contributes nothing:
    an operator-side writer must not be able to stop a release with a mode."""
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                yield entry.path
    except OSError:
        return


def _in_flight(record: Path, now: float) -> bool:
    """What the RECORD claims: a non-terminal state and a start that is not
    in the future. The window itself is `Quiesce`'s, from root's clock."""
    body = _read(record)
    if body is None:
        return False

    state = body.get("state")
    if not isinstance(state, str) or state in TERMINAL_STATES:
        return False

    started = _epoch(body.get("started_at"))
    if started is None or started > now:
        # A record that claims the future is not a turn mid-start. It is a
        # clock that disagrees, or a writer trying to hold the window open.
        return False

    return (now - started) < LAUNCH_GRACE_S


def _read(record: Path) -> dict[str, Any] | None:
    try:
        if record.stat().st_size > MAX_TURN_BYTES:
            return None

        loaded: Any = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(loaded, dict):
        return None

    return cast("dict[str, Any]", loaded)


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None

    text = value.replace("Z", "+0000")
    for shape in RFC3339_FORMATS:
        try:
            return datetime.strptime(text, shape).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue

    return None
