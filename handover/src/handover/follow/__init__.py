"""Follow the tags: file the release request a new tag calls for.

`stage7-releases.md` §2.7 gave one step after a merge to a human: type
`handover request <name> --wait` on the host. Only then did the phone ask.
`handover follow` is that step on a timer, as the operator. It files a
request and it approves nothing. The tap at step 5 stays the only gate, and
it binds to root's own resolution, as it does for every other requester.

It is a requester like the other three (§3.1), and root believes it no more
than it believes them: the request carries intent only, and `requested_by`
is a claim.

Six rules hold its shape, each with the reason it is a rule.

1. **It moves a tree forward and never makes the first one.** A component
   is followed only while its tree carries a release stamp. A first release
   is a deliberate act (`stage7-releases.md` §2.5), and it stays one.
2. **A set is seen once before it is filed.** CI makes a tag and then its
   Release, and one merge can tag several components. A request filed at
   the first sight of a tag can name half a set, or a tag root finds no
   Release for. The second run that reads the same set files it.
3. **One request per set, not one per run.** The marker names the set that
   was asked and the request that asked it. The timer fires for ever, and a
   run that finds its own set in the marker files nothing.
4. **An answer is an answer.** A denial, a refusal and a failed release
   each hold the set until a newer tag changes it. Root has pushed the
   outcome already, and asking again would push it again.
5. **A tap nobody gave is not an answer.** The gate closes after fifteen
   minutes. A phone out of reach must not send the operator to a terminal,
   which is the step this module removes. So that one outcome is asked
   again after `AGAIN_S`, and `MAX_ASKS` times in all.
6. **The marker goes down BEFORE the request.** A request with no marker
   behind it is filed again on the next run, and each one is a push. In
   this order a marker that cannot be written costs no request at all.

This package reads one ledger entry and writes one marker. It starts no
child and opens no socket: the fetch is `corpus/`'s and the request is
`requester/`'s, and `cli.py` calls all three. `chaperone/` never imports it.

Stdlib only, like the requester: a release re-syncs this venv under a run.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from ..errors import RefusalCode
from ..executor.approval import Verdict, not_granted
from ..executor.spool import DONE_DIR
from ..state import ReleaseState

#: §2.3's `requested_by`, and a row of the phone summary (§2.5): the
#: operator reads who asked before the tap. Recorded, never trusted.
REQUESTED_BY: Final = "follow"

#: How long a set whose tap nobody gave waits before it is asked again.
#: The gate itself is open for fifteen minutes, so an hour is four of them.
AGAIN_S: Final = 3600.0

#: How many requests one set gets, the first one included. Past it the set
#: is quiet until a newer tag changes it, or the operator files by hand.
MAX_ASKS: Final = 4

#: The marker, under the operator's own home. Its directory is `0700` and
#: the file `0600`: nothing but this module reads it.
MARKER_DIR: Final = ".local/state/creche"
MARKER_NAME: Final = "follow.json"
MARKER_FILE_MODE: Final = 0o600
MARKER_DIR_MODE: Final = 0o700

#: A marker holds at most eight names and one id. A larger file is not one.
MAX_MARKER_BYTES: Final = 4096

#: A ledger entry carries a 200-line log tail. One mebibyte is far above it.
MAX_ENTRY_BYTES: Final = 1024 * 1024

ENTRY_SUFFIX: Final = ".json"

_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: Component name to the version a release would move it to.
Moving = dict[str, str]

#: The two reasons root ledgers when the gate closed with no tap.
_NOBODY_TAPPED: Final = frozenset({not_granted(Verdict.TIMEOUT), not_granted(Verdict.EXPIRED)})


class Step(StrEnum):
    """What one run does."""

    #: No followed component has a newer tag.
    IDLE = "idle"
    #: A set this module reads for the first time. Recorded, not filed.
    SEEN = "seen"
    #: File one request for the set.
    FILE = "file"
    #: The set is asked already. Nothing is filed.
    HELD = "held"


class Answer(StrEnum):
    """What root's ledger says about the request the marker names."""

    #: No entry: the request waits in `requests/`, or the gate is open.
    NONE = "none"
    #: The gate closed with no tap.
    UNANSWERED = "unanswered"
    #: Every other entry: a success, a denial, a refusal, a failure.
    FINAL = "final"


@dataclass(frozen=True)
class Marker:
    """What this module remembers from one run to the next."""

    #: The set the last run read. Rule 2.
    seen: Moving = field(default_factory=Moving)
    #: The set the last request named. Rule 3.
    asked: Moving = field(default_factory=Moving)
    #: The request that named it.
    request_id: str | None = None
    #: How many requests `asked` has had. Rule 5.
    asks: int = 0
    #: When the last one was filed, on the wall clock.
    asked_at: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "seen": dict(self.seen),
            "asked": dict(self.asked),
            "id": self.request_id,
            "asks": self.asks,
            "at": self.asked_at,
        }


def default_marker() -> Path:
    return Path.home() / MARKER_DIR / MARKER_NAME


def done_dir_of(requests_dir: str) -> Path:
    """The ledger directory beside `requests/` (`executor/spool.py`)."""
    return Path(requests_dir).parent / DONE_DIR


def _version_key(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None

    return tuple(int(part) for part in parts)


def _is_newer(target: str, live: str) -> bool:
    """Whether `target` is above `live`. A version that does not read as
    numbers is not above anything: a component never goes backwards
    (`stage7-releases.md` §6 row 7), and root refuses it anyway."""
    new, old = _version_key(target), _version_key(live)
    if new is None or old is None:
        return False

    return new > old


def moving_of(state: ReleaseState, names: Iterable[str]) -> tuple[Moving, tuple[str, ...]]:
    """The followed components a release would move, and one note for each
    name rule 1 leaves out."""
    moving: Moving = {}
    notes: list[str] = []
    for name in sorted(names):
        live = state.live.get(name)
        if live is None:
            notes.append(f"{name} has had no release: its first one is filed by hand")
            continue

        newest = state.latest.get(name)
        if newest is None or not _is_newer(newest, live):
            continue

        moving[name] = newest

    return moving, tuple(notes)


def decide(moving: Mapping[str, str], marker: Marker, answer: Answer, now: float) -> Step:
    """What this run does. Pure: every input is an argument."""
    if not moving:
        return Step.IDLE

    if moving != marker.asked:
        return Step.FILE if moving == marker.seen else Step.SEEN

    if answer is not Answer.UNANSWERED:
        return Step.HELD

    if marker.asks >= MAX_ASKS or now - marker.asked_at < AGAIN_S:
        return Step.HELD

    return Step.FILE


def asked_marker(moving: Mapping[str, str], marker: Marker, request_id: str, now: float) -> Marker:
    """The marker a filed request leaves. A repeat of the same set counts
    up, and a new set starts at one."""
    asks = marker.asks + 1 if moving == marker.asked else 1

    return Marker(
        seen=dict(moving), asked=dict(moving), request_id=request_id, asks=asks, asked_at=now
    )


def seen_marker(moving: Mapping[str, str], marker: Marker) -> Marker:
    return Marker(
        seen=dict(moving),
        asked=marker.asked,
        request_id=marker.request_id,
        asks=marker.asks,
        asked_at=marker.asked_at,
    )


def _read_capped(path: Path, cap: int) -> bytes | None:
    """A regular file's bytes, or None. No symlink is followed."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        return None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None

        return os.read(fd, cap)
    except OSError:
        return None
    finally:
        os.close(fd)


def _object_of(raw: bytes | None) -> dict[str, object] | None:
    if raw is None:
        return None

    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None

    if not isinstance(body, dict):
        return None

    return cast("dict[str, object]", body)


def _set_of(value: object) -> Moving | None:
    if not isinstance(value, dict):
        return None

    found: Moving = {}
    for name, version in cast("dict[object, object]", value).items():
        if not isinstance(name, str) or not isinstance(version, str):
            return None

        found[name] = version

    return found


def read_marker(path: Path) -> Marker:
    """The marker, or an empty one. A file that does not read as a marker
    costs one repeated request, and refusing would stop every future one."""
    body = _object_of(_read_capped(path, MAX_MARKER_BYTES))
    if body is None:
        return Marker()

    seen, asked = _set_of(body.get("seen")), _set_of(body.get("asked"))
    request_id, asks, at = body.get("id"), body.get("asks"), body.get("at")
    if seen is None or asked is None:
        return Marker()

    if request_id is not None and not isinstance(request_id, str):
        return Marker()

    if isinstance(asks, bool) or not isinstance(asks, int):
        return Marker()

    if isinstance(at, bool) or not isinstance(at, int | float):
        return Marker()

    # A whole number past the largest float has no float.
    try:
        asked_at = float(at)
    except OverflowError:
        return Marker()

    return Marker(seen=seen, asked=asked, request_id=request_id, asks=asks, asked_at=asked_at)


def write_marker(path: Path, marker: Marker) -> None:
    """Whole file or none. Raises `OSError` when this host cannot write it,
    and the caller then files nothing (rule 6)."""
    path.parent.mkdir(mode=MARKER_DIR_MODE, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp, flags, MARKER_FILE_MODE)
    try:
        os.fchmod(fd, MARKER_FILE_MODE)
        os.write(fd, json.dumps(marker.as_dict(), sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(tmp, path)


def read_answer(done_dir: Path, request_id: str | None) -> Answer:
    """What the ledger says about one request, read by its name.

    `done/` informs and never decides (`stage7-releases.md` §3.2). An entry
    this side cannot read holds the set, which is the quiet answer.
    """
    if request_id is None:
        return Answer.NONE

    entry = done_dir / f"{request_id}{ENTRY_SUFFIX}"
    if not entry.exists():
        return Answer.NONE

    body = _object_of(_read_capped(entry, MAX_ENTRY_BYTES))
    if body is None:
        return Answer.FINAL

    if body.get("refused_check") != str(RefusalCode.APPROVAL):
        return Answer.FINAL

    return Answer.UNANSWERED if body.get("reason") in _NOBODY_TAPPED else Answer.FINAL


__all__ = [
    "AGAIN_S",
    "MAX_ASKS",
    "REQUESTED_BY",
    "Answer",
    "Marker",
    "Moving",
    "Step",
    "asked_marker",
    "decide",
    "default_marker",
    "done_dir_of",
    "moving_of",
    "read_answer",
    "read_marker",
    "seen_marker",
    "write_marker",
]
