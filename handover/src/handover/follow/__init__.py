"""Follow the tags: file the release request a new tag calls for.

`stage7-releases.md` §2.7 gave one step after a merge to a human: type
`handover request <name> --wait` on the host. Only then did the phone ask.
`handover follow` is that step on a timer, as the operator. It files a
request and it approves nothing. The tap at step 5 stays the only gate, and
it binds to root's own resolution, as it does for every other requester.

It is a requester like the other three (§3.1), and root believes it no more
than it believes them: the request carries intent only, and `requested_by`
is a claim.

Seven rules hold its shape, each with the reason it is a rule.

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
   again after `AGAIN_S`, and `MAX_ASKS` times in all. Check P6 with the
   reason `ArtifactRefusal.VERIFIER_UNAVAILABLE` is asked again in the
   same way: the verifier gave root no answer about the signature.
6. **The marker goes down BEFORE the request.** A request with no marker
   behind it is filed again on the next run, and each one is a push. In
   this order a marker that cannot be written costs no request at all.
7. **A binary member waits for its artifact, for a time.** Root installs a
   binary component from the archive in the store of the operator. A member
   that ships an archive and has no two files there is not ready, and while
   it waits the run files nothing. After `ARTIFACT_WAIT_S` it leaves the set
   of the run, so one network fault costs no refusal and stops no other
   member. `cli.py` asks the store, and `gate_artifacts` takes the answer as
   data. When the files arrive later, the member is part of a new set.

This package reads one ledger entry and writes one marker. When the ledger
entry or the marker is not strict JSON (`strictjson.py`), the follower also
writes one row in its notice file (`notices.py`). It starts no child and
opens no socket: the fetch is `corpus/`'s and the request is `requester/`'s,
and `cli.py` calls all three. The PEP never imports it.

Stdlib only, like the requester: a release re-syncs this venv under a run.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from .. import notices, strictjson
from ..artifacts import ArtifactRefusal
from ..errors import RefusalCode
from ..executor.approval import Verdict, not_granted
from ..executor.layout import OPERATOR_STATE_DIR, STATE_ROOT
from ..executor.spool import DONE_DIR
from ..executor.spool import SURFACE as ENTRY_SURFACE
from ..state import ReleaseState

#: §2.3's `requested_by`, and a row of the phone summary (§2.5): the
#: operator reads who asked before the tap. Recorded, never trusted.
REQUESTED_BY: Final = "follow"

#: How long a set that nobody answered (rule 5) waits before it is asked again.
#: The gate itself is open for fifteen minutes, so an hour is four of them.
AGAIN_S: Final = 3600.0

#: How many requests one set gets, the first one included. Past it the set
#: is quiet until a newer tag changes it, or the operator files by hand.
MAX_ASKS: Final = 4

#: The marker, under the operator's own home. Its directory is `0700` and
#: the file `0600`: nothing but this module reads it.
MARKER_DIR: Final = OPERATOR_STATE_DIR
MARKER_NAME: Final = "follow.json"
MARKER_FILE_MODE: Final = 0o600
MARKER_DIR_MODE: Final = 0o700

#: The notice file of the follower, under the state root of the operator.
#: A timer starts the follower, and the operator does not watch its log.
#: The host script that sends the notice push reads each file of this
#: directory.
NOTICE_SERVICE: Final = "follow"
NOTICES_FILE: Final = f"{STATE_ROOT}/notices/{NOTICE_SERVICE}.json"

#: The surface word of a notice for the marker. A notice for an entry of
#: the ledger takes the word of the spool (`ENTRY_SURFACE`).
MARKER_SURFACE: Final = "release.marker"

#: A marker holds three sets of at most eight names each, and one id. A
#: larger file is not one.
MAX_MARKER_BYTES: Final = 4096

#: A ledger entry carries a 200-line log tail. One mebibyte is far above it.
MAX_ENTRY_BYTES: Final = 1024 * 1024

ENTRY_SUFFIX: Final = ".json"

_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: How long a member that ships an archive waits for its two files in the
#: store, from the first run that read its tag with no files. CI publishes
#: the assets after it pushes the tag, so the first runs can find none.
ARTIFACT_WAIT_S: Final = 600

#: The time of one run, from the start of the process. The fetch of the
#: artifacts gets what is left of it. The unit gives a run more time than
#: this (`systemd/creche-follow.service`, `TimeoutStartSec=`).
RUN_DEADLINE_S: Final = 270

#: The two lines of the wait rule (rule 7).
WAITING: Final = "waiting: the artifact of {name} {version} is not in the store"
OMITTED: Final = "omitted: {name} {version} has no artifact after {seconds} s"

#: The key of the waits in the marker, and the two keys of one wait.
WAITS_KEY: Final = "waits"
WAIT_KEYS: Final = frozenset({"version", "since"})

#: Component name to the version a release would move it to.
Moving = dict[str, str]

#: The check and the reason of each ledger entry that nobody answered
#: (rule 5): the gate closed with no tap, or check P6 got no answer from
#: the verifier.
_NOBODY_ANSWERED: Final = frozenset(
    {
        (str(RefusalCode.APPROVAL), not_granted(Verdict.TIMEOUT)),
        (str(RefusalCode.APPROVAL), not_granted(Verdict.EXPIRED)),
        (str(RefusalCode.P6), str(ArtifactRefusal.VERIFIER_UNAVAILABLE)),
    }
)


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
    #: The gate closed with no tap, or check P6 got no answer from the
    #: verifier.
    UNANSWERED = "unanswered"
    #: Every other entry: a success, a denial, a refusal, a failure.
    FINAL = "final"


@dataclass(frozen=True)
class Wait:
    """One member that waits for its artifact: the version of its tag, and
    the wall time of the first run that read that tag with no artifact."""

    version: str
    since: float


#: Component name to its wait.
Waits = dict[str, Wait]


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
    #: Each member that waits for its artifact. Rule 7.
    waits: Waits = field(default_factory=Waits)

    def as_dict(self) -> dict[str, object]:
        """The marker as JSON values. A marker with no wait has no key
        `waits`, so its text is the text of a follower with no wait rule."""
        body: dict[str, object] = {
            "seen": dict(self.seen),
            "asked": dict(self.asked),
            "id": self.request_id,
            "asks": self.asks,
            "at": self.asked_at,
        }
        if self.waits:
            body[WAITS_KEY] = {
                name: {"version": wait.version, "since": wait.since}
                for name, wait in self.waits.items()
            }

        return body


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
        seen=dict(moving),
        asked=dict(moving),
        request_id=request_id,
        asks=asks,
        asked_at=now,
        waits=marker.waits,
    )


def seen_marker(moving: Mapping[str, str], marker: Marker) -> Marker:
    return Marker(
        seen=dict(moving),
        asked=marker.asked,
        request_id=marker.request_id,
        asks=marker.asks,
        asked_at=marker.asked_at,
        waits=marker.waits,
    )


@dataclass(frozen=True)
class ArtifactGate:
    """What the wait rule gives one run (rule 7)."""

    #: The set that goes to `decide`: the moving set less each omitted member.
    moving: Moving
    #: The waits that the marker holds after this run.
    waits: Waits
    #: Each member that waits and whose time is not over. The run files nothing.
    waiting: Moving
    #: Each member whose time is over. It leaves the set of this run.
    omitted: Moving

    def lines(self) -> tuple[str, ...]:
        waiting = (
            WAITING.format(name=name, version=version) for name, version in self.waiting.items()
        )
        omitted = (
            OMITTED.format(name=name, version=version, seconds=ARTIFACT_WAIT_S)
            for name, version in self.omitted.items()
        )

        return (*waiting, *omitted)


def gate_artifacts(
    moving: Mapping[str, str], ready: AbstractSet[str], waits: Mapping[str, Wait], now: float
) -> ArtifactGate:
    """The wait rule. Pure: every input is an argument.

    `ready` holds each member that ships no archive, and each member whose
    two files are in the store. A member that is not ready waits from the
    first run that read its version. A wait of another version starts again,
    and so does a wait whose time is after `now`, because the wall clock
    went back.
    """
    kept: Waits = {}
    waiting: Moving = {}
    omitted: Moving = {}
    for name in sorted(moving):
        version = moving[name]
        if name in ready:
            continue

        wait = waits.get(name)
        if wait is None or wait.version != version or wait.since > now:
            wait = Wait(version, now)

        kept[name] = wait
        target = waiting if now - wait.since < ARTIFACT_WAIT_S else omitted
        target[name] = version

    rest = {name: version for name, version in moving.items() if name not in omitted}

    return ArtifactGate(moving=rest, waits=kept, waiting=waiting, omitted=omitted)


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


def _object_of(raw: bytes | None, cap: int, surface: str) -> dict[str, object] | None:
    """The object of a strict text of at most `cap` bytes, or None. A text
    that is not strict JSON also records a notice with `surface`."""
    if raw is None:
        return None

    try:
        body = strictjson.loads(raw, cap=cap)
    except strictjson.NotStrictJson as refusal:
        notices.record(surface, refusal.rule)

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


def _wait_of(value: object) -> Wait | None:
    """One wait: an object with a version text and a time, and no other key."""
    if not isinstance(value, dict):
        return None

    found = cast("dict[str, object]", value)
    if found.keys() != WAIT_KEYS:
        return None

    version, since = found["version"], found["since"]
    if not isinstance(version, str) or isinstance(since, bool):
        return None

    if not isinstance(since, int | float):
        return None

    try:
        return Wait(version, float(since))
    except OverflowError:
        return None


def _waits_of(body: Mapping[str, object]) -> Waits | None:
    """The waits of a marker. No key is no wait, and each other shape of the
    value is None."""
    if WAITS_KEY not in body:
        return {}

    value = body[WAITS_KEY]
    if not isinstance(value, dict):
        return None

    found: Waits = {}
    for name, item in cast("dict[object, object]", value).items():
        wait = _wait_of(item)
        if not isinstance(name, str) or wait is None:
            return None

        found[name] = wait

    return found


def read_marker(path: Path) -> Marker:
    """The marker, or an empty one. A file that does not read as a marker
    costs one repeated request, and refusing would stop every future one.
    A marker with no key `waits` has no wait (rule 7)."""
    body = _object_of(_read_capped(path, MAX_MARKER_BYTES), MAX_MARKER_BYTES, MARKER_SURFACE)
    if body is None:
        return Marker()

    seen, asked = _set_of(body.get("seen")), _set_of(body.get("asked"))
    request_id, asks, at = body.get("id"), body.get("asks"), body.get("at")
    waits = _waits_of(body)
    if seen is None or asked is None or waits is None:
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

    return Marker(
        seen=seen, asked=asked, request_id=request_id, asks=asks, asked_at=asked_at, waits=waits
    )


def write_marker(path: Path, marker: Marker) -> None:
    """Whole file or none. Raises `OSError` when this host cannot write it,
    and the caller then files nothing (rule 6). Raises `NotStrictJson`, and
    writes nothing, for a marker with no strict text."""
    data = strictjson.dumps(marker.as_dict(), sort_keys=True).encode("utf-8")
    path.parent.mkdir(mode=MARKER_DIR_MODE, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp, flags, MARKER_FILE_MODE)
    try:
        os.fchmod(fd, MARKER_FILE_MODE)
        os.write(fd, data)
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

    body = _object_of(_read_capped(entry, MAX_ENTRY_BYTES), MAX_ENTRY_BYTES, ENTRY_SURFACE)
    if body is None:
        return Answer.FINAL

    check, reason = body.get("refused_check"), body.get("reason")
    if not isinstance(check, str) or not isinstance(reason, str):
        return Answer.FINAL

    return Answer.UNANSWERED if (check, reason) in _NOBODY_ANSWERED else Answer.FINAL


__all__ = [
    "AGAIN_S",
    "ARTIFACT_WAIT_S",
    "MAX_ASKS",
    "NOTICES_FILE",
    "NOTICE_SERVICE",
    "OMITTED",
    "REQUESTED_BY",
    "RUN_DEADLINE_S",
    "WAITING",
    "Answer",
    "ArtifactGate",
    "Marker",
    "Moving",
    "Step",
    "Wait",
    "Waits",
    "asked_marker",
    "decide",
    "default_marker",
    "done_dir_of",
    "gate_artifacts",
    "moving_of",
    "read_answer",
    "read_marker",
    "seen_marker",
    "write_marker",
]
