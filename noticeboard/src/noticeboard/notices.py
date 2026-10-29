"""The notice of a refused JSON text: one log line, and one row in a file.

A reader that refuses a text that is not strict JSON calls `record`. The
operator then learns of the refusal from a push. This module sends no push:
the host script `bin/creche-watchdog.sh` reads the notice file of each
program and sends one. So the module reads no variable, and it holds no
client and no token.

A program calls `configure` one time at its start, with its service word and
the path of its notice file. The path is `None` for a program that has no
notice file. `configure` reads no file and writes no file. Before the first
`configure` call, `record` writes the log line with the service word `unset`
and writes no file.

`record` never raises. It writes one log line of the level error, with the
words `json_not_strict <service> <surface> <rule>`. With a path, it then
changes the file in four steps:

1. It takes an exclusive `flock` on the file `<path>.lock`. It asks again in
   steps for `LOCK_WAIT_S` seconds at most.
2. It reads the file at the path with the strict reader and the cap
   `FILE_CAP`. A file that is absent, or that is no notice document, counts
   as no rows.
3. It adds the refusal to the rows.
4. It writes the file again: a temporary file in the same directory, then
   one rename.

The file is thus the one state, and the module keeps no row in memory. A
daemon after a restart, a program of a timer and two processes with one path
keep each row. One lock of the process holds the four steps for its threads.

The file has the mode 0640. `record` makes an absent directory with the mode
0750. It sets each mode with an explicit call, because a unit can hold
`UMask=0077`. The name of the lock file and the name of the temporary file do
not end in `.json`: the host script reads each file `*.json` of the
directory. A step that fails leaves the log line only, and the file stays as
it was.

The notice document is the text of `strictjson.dumps` with `indent=1` and
`sort_keys=True`. It has four keys:

- `kind`: the word `notices`.
- `service`: the service word of `configure`.
- `rows`: one row for each pair of a surface and a rule, in the order of
  the surface word, then of the rule word. A row holds `surface`, `rule`,
  `family`, `count`, `first_at` and `last_at`. `family` is the family of the
  last refusal of the pair, or `null`. A time is UTC with `Z` and whole
  seconds.
- `push`: one text for a reader with no JSON parser. It holds one token
  `<surface>:<rule>:<seconds>` for each pair with a proven refusal, in the
  order of the rows, with one space between two tokens. `<seconds>` is the
  time of the last proven refusal of the pair, in seconds from
  1970-01-01T00:00:00Z. With sorted keys and an indent of one space, the key
  `push` is on a line of its own.

A refusal with `proven=False` is for a text of a caller that gave no proof of
identity yet. Its row counts it, and the text `push` does not change. So a
caller with no proof cannot cause a push.

No text of the refused document goes into the log line or into the file. The
caller gives the family from a path or from a token, never from the refused
text.

This file is a copy. Each package that records a notice holds a file with
the same bytes, beside its copy of `strictjson.py`.
`bin/tests/test_strictjson_copies.py` fails when two copies differ. The file
needs the standard library only.
"""

from __future__ import annotations

import fcntl
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from . import strictjson

_LOG: Final = logging.getLogger(__name__)

#: The first word of the log line of a refusal.
LOG_WORD: Final = "json_not_strict"

#: The service word of a record before the first `configure` call.
UNSET: Final = "unset"

#: The most bytes of a notice file that `record` reads.
FILE_CAP: Final = 1 << 20

#: The longest wait for the lock of the file, in seconds, and one step of it.
LOCK_WAIT_S: Final = 1.0
_LOCK_STEP_S: Final = 0.01

FILE_MODE: Final = 0o640
DIR_MODE: Final = 0o750

#: The word of the `kind` key of a notice document.
KIND: Final = "notices"

#: The grammar of a service word and of a surface word. The host script
#: takes the service word from the name of the file, and it holds each token
#: of `push` to these patterns. A word outside them breaks the line.
_SERVICE_WORD: Final = re.compile(r"[a-z0-9-]{1,64}", re.ASCII)
_SURFACE_WORD: Final = re.compile(r"[a-z0-9_.]{1,64}", re.ASCII)

#: The form of a time of a row.
_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
_TIME_TEXT: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)

#: The most seconds of a time: 9999-12-31T23:59:59Z. A time has a year of
#: four digits, and `<seconds>` has twelve digits at most.
_SECONDS_MAX: Final = 253_402_300_799

#: The `<seconds>` of a token.
_SECONDS_TEXT: Final = re.compile(r"[0-9]{1,12}", re.ASCII)

#: The largest count of a row: the largest integer of 63 bits. A count stops
#: there.
_COUNT_MAX: Final = 2**63 - 1

_ROW_KEYS: Final = frozenset({"surface", "rule", "family", "count", "first_at", "last_at"})
_DOCUMENT_KEYS: Final = frozenset({"kind", "service", "rows", "push"})

_RULE_WORDS: Final = frozenset(rule.value for rule in strictjson.Rule)

#: One lock for the four steps of `record` in this process.
_STEPS: Final = threading.Lock()

#: The text of the temporary file before its random part, and after it.
_TEMP_PREFIX: Final = "."
_TEMP_SUFFIX: Final = ".tmp"
_LOCK_SUFFIX: Final = ".lock"


@dataclass(frozen=True, slots=True)
class _Settings:
    service: str
    path: Path | None
    clock: Callable[[], float]


#: The values of the last `configure` call. `None` before the first one.
_settings: _Settings | None = None


@dataclass(slots=True)
class _Row:
    family: str | None
    count: int
    first_at: str
    last_at: str


#: The pair of a row: a surface word and a rule word.
type _Pair = tuple[str, str]


def configure(service: str, path: Path | None, *, clock: Callable[[], float] | None = None) -> None:
    """Sets the service word and the notice file of this program.

    `clock` gives the time of a record, in seconds from 1970. A program gives
    none, and the module then reads the clock of the system. The call reads
    no file and writes no file.

    Raises `ValueError` for a service word that the host script does not
    read: 1 to 64 characters of `a` to `z`, `0` to `9` and `-`.
    """
    global _settings

    if _SERVICE_WORD.fullmatch(service) is None:
        raise ValueError("the service word has another form than the host script reads")

    _settings = _Settings(service, path, clock or time.time)


def record(
    surface: str,
    rule: strictjson.Rule,
    family: str | None = None,
    *,
    proven: bool = True,
) -> None:
    """Records one refusal of a text that is not strict JSON. Never raises.

    `surface` names what the text was, for example `status.document`. A
    word outside the grammar of the host script gets the log line only.
    `family` comes from a path or a token, never from the refused text.
    """
    settings = _settings
    service = UNSET if settings is None else settings.service
    _LOG.error("%s %s %s %s", LOG_WORD, service, surface, rule.value)

    if settings is None or settings.path is None:
        return

    if _SURFACE_WORD.fullmatch(surface) is None:
        return

    # A step that fails leaves the log line only. The broad clause is the
    # rule of this function: a notice must not stop the reader that called.
    try:
        _add(settings, (surface, rule.value), family, proven)
    except Exception:
        return


def _add(settings: _Settings, pair: _Pair, family: str | None, proven: bool) -> None:
    """The four steps of `record`. Raises for a step that fails."""
    path = settings.path
    if path is None:
        return

    seconds = _whole_seconds(settings.clock())
    if not _STEPS.acquire(timeout=LOCK_WAIT_S):
        raise TimeoutError("the lock of the process")

    try:
        _make_directory(path.parent)
        lock = os.open(
            f"{path}{_LOCK_SUFFIX}", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            _set_mode(lock, FILE_MODE)
            _wait_for(lock)
            rows, pushed = _read(path, settings.service)
            _count(rows, pushed, pair, family, proven, seconds)
            _write(path, _document(settings.service, rows, pushed))
        finally:
            os.close(lock)
    finally:
        _STEPS.release()


def _whole_seconds(moment: float) -> int:
    if not math.isfinite(moment) or not 0 <= moment <= _SECONDS_MAX:
        raise ValueError("the clock gave no time of a record")

    return math.floor(moment)


def _time_text(seconds: int) -> str:
    return time.strftime(_TIME_FORMAT, time.gmtime(seconds))


def _make_directory(directory: Path) -> None:
    """Makes `directory` with `DIR_MODE` when it is absent. Its parent must exist."""
    try:
        os.mkdir(directory, DIR_MODE)
    except FileExistsError:
        return

    os.chmod(directory, DIR_MODE)


def _set_mode(handle: int, mode: int) -> None:
    if os.fstat(handle).st_mode & 0o7777 != mode:
        os.fchmod(handle, mode)


def _wait_for(lock: int) -> None:
    """Takes the exclusive lock, and asks again for `LOCK_WAIT_S` at most."""
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise

            time.sleep(_LOCK_STEP_S)
            continue

        return


def _read(path: Path, service: str) -> tuple[dict[_Pair, _Row], dict[_Pair, int]]:
    """The rows and the tokens of the file. Both are empty for a file that is
    absent or that is no notice document of `service`."""
    try:
        handle = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}, {}

    with os.fdopen(handle, "rb") as file:
        data = file.read(FILE_CAP + 1)

    try:
        value = strictjson.loads(data, cap=FILE_CAP)
    except strictjson.NotStrictJson:
        return {}, {}

    return _parsed(value, service) or ({}, {})


def _parsed(value: object, service: str) -> tuple[dict[_Pair, _Row], dict[_Pair, int]] | None:
    """The rows and the tokens of a notice document. `None` for each other value."""
    document = _object(value, _DOCUMENT_KEYS)
    if document is None or document["kind"] != KIND or document["service"] != service:
        return None

    entries = document["rows"]
    if not isinstance(entries, list):
        return None

    rows: dict[_Pair, _Row] = {}
    for entry in cast("list[object]", entries):
        found = _row(entry)
        if found is None or found[0] in rows:
            return None

        rows[found[0]] = found[1]

    pushed = _tokens(document["push"], rows)
    if pushed is None:
        return None

    return rows, pushed


def _object(value: object, keys: frozenset[str]) -> dict[str, object] | None:
    """`value` as an object with exactly `keys`. `None` for each other value.

    The strict reader gives a text for each key of an object.
    """
    if not isinstance(value, dict):
        return None

    members = cast("dict[str, object]", value)

    return members if members.keys() == keys else None


def _row(entry: object) -> tuple[_Pair, _Row] | None:
    members = _object(entry, _ROW_KEYS)
    if members is None:
        return None

    surface, rule, family = members["surface"], members["rule"], members["family"]
    count, first_at, last_at = members["count"], members["first_at"], members["last_at"]
    if not isinstance(surface, str) or _SURFACE_WORD.fullmatch(surface) is None:
        return None
    if not isinstance(rule, str) or rule not in _RULE_WORDS:
        return None
    if family is not None and not isinstance(family, str):
        return None
    if type(count) is not int or not 1 <= count <= _COUNT_MAX:
        return None
    if not isinstance(first_at, str) or _TIME_TEXT.fullmatch(first_at) is None:
        return None
    if not isinstance(last_at, str) or _TIME_TEXT.fullmatch(last_at) is None:
        return None

    return (surface, rule), _Row(family, count, first_at, last_at)


def _tokens(text: object, rows: dict[_Pair, _Row]) -> dict[_Pair, int] | None:
    """The `<seconds>` of each pair of the text `push`. Each pair has a row."""
    if not isinstance(text, str):
        return None

    pushed: dict[_Pair, int] = {}
    for token in text.split(" ") if text else ():
        parts = token.split(":")
        if len(parts) != 3:
            return None

        surface, rule, seconds = parts
        pair = (surface, rule)
        if pair not in rows or pair in pushed or _SECONDS_TEXT.fullmatch(seconds) is None:
            return None

        pushed[pair] = int(seconds)

    return pushed


def _count(
    rows: dict[_Pair, _Row],
    pushed: dict[_Pair, int],
    pair: _Pair,
    family: str | None,
    proven: bool,
    seconds: int,
) -> None:
    at = _time_text(seconds)
    row = rows.get(pair)
    if row is None:
        rows[pair] = _Row(family, 1, at, at)
    else:
        row.family = family
        row.count = min(row.count + 1, _COUNT_MAX)
        row.last_at = at

    if proven:
        pushed[pair] = seconds


def _document(service: str, rows: dict[_Pair, _Row], pushed: dict[_Pair, int]) -> dict[str, object]:
    pairs = sorted(rows)

    return {
        "kind": KIND,
        "service": service,
        "rows": [
            {
                "surface": surface,
                "rule": rule,
                "family": rows[(surface, rule)].family,
                "count": rows[(surface, rule)].count,
                "first_at": rows[(surface, rule)].first_at,
                "last_at": rows[(surface, rule)].last_at,
            }
            for surface, rule in pairs
        ],
        "push": " ".join(f"{one[0]}:{one[1]}:{pushed[one]}" for one in pairs if one in pushed),
    }


def _write(path: Path, document: dict[str, object]) -> None:
    """Writes `document` to a temporary file beside `path`, then renames it."""
    data = strictjson.dumps(document, indent=1, sort_keys=True).encode("utf-8")
    handle, temporary = tempfile.mkstemp(
        prefix=f"{_TEMP_PREFIX}{path.name}.", suffix=_TEMP_SUFFIX, dir=path.parent
    )
    try:
        with os.fdopen(handle, "wb") as file:
            os.fchmod(file.fileno(), FILE_MODE)
            file.write(data)

        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
