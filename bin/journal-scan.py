#!/usr/bin/env python3
# OPERATOR, by hand. A read-only report on the journal files of attendance:
#   bin/journal-scan.py <sessions root>
# Needs Python 3.12 or later, the oldest version that the tests run it under.
# Each import is a module of the standard library, so no venv is necessary.
"""Report on the journals below one sessions root, and change nothing.

Each session has one journal. Its path is
`<sessions root>/<family>/<session>/journal.ndjson`. The session service
writes one JSON object on each line of that file.

THE OUTPUT. One row for each number, as `<name> <number>`, in this order:

  files                        The journals that the program found.
  lines                        The lines that end with a newline.
  files_not_read               The places where a journal can be and that the
                               program did not read whole: a journal, a
                               directory, or a symbolic link where one of
                               the two can be.
  lines_not_json               The lines that `json.loads` refuses, and the
                               lines whose JSON value is not an object.
  lines_unfinished             The last lines that have no newline, one for
                               each journal at most. The service may still
                               write such a line. The program does not judge
                               it, and it stops the read of that journal.
  lone_surrogate_lines         COUNT 1. The lines with a key or a value that
                               UTF-8 cannot encode, at any level of the
                               object. Such a text has a surrogate code
                               point with no partner.
  lone_surrogate_lines.<kind>  COUNT 1 for the lines of one `kind`. A row is
                               there only for a kind that COUNT 1 found. The
                               row `other` has each line whose `kind` does
                               not match KIND_WORD, a constant below.
  cut_events_small             COUNT 2. The `pi_event` lines whose `body` is
                               a cut event of a small size: its `truncated`
                               is true, and its `original_bytes` is an
                               integer that is not below 0 and not above
                               MAX_EVENT_BYTES, a constant below. The size
                               of such an event was not the cause of the
                               cut.

WHAT THE PROGRAM LEAVES ALONE. The program only reads. It creates nothing and
it locks nothing. It asks for read access only, and its memory has one line of
a journal at a time. It goes through no symbolic link below the sessions
root. It takes nothing from the environment. The output names no family, no
session and no path. From a journal line it can show one thing: a `kind` that
matches KIND_WORD.

EXIT STATUS.
  0  The program read each journal whole.
  1  `files_not_read` is not 0. Each other number can then be too low.
  2  The program refused the run and printed no number. The causes: the
     Python version is below MIN_PYTHON, the user is root, the command does
     not have exactly one argument, or the sessions root does not open as a
     directory.
Only `files_not_read` changes the exit status. Each other number is a report.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from typing import cast

#: The name of the journal in a session directory.
JOURNAL_FILE = "journal.ndjson"

#: The `kind` of a line whose `body` is one event of pi.
PI_EVENT = "pi_event"

#: The most bytes of an event that the host records whole. A cut event of
#: this size or less was not cut for its size.
MAX_EVENT_BYTES = 262_144

#: The directories between the sessions root and a journal: the family, then
#: the session.
JOURNAL_DEPTH = 2

#: A `kind` that the output can name. Each other `kind` counts as OTHER_KIND,
#: so no free text of a line reaches the output.
KIND_WORD = re.compile(r"[a-z_]{1,32}")
OTHER_KIND = "other"

#: The oldest Python version of a test run. Under an older version,
#: `json.loads` can judge a line in another way, so the program refuses it.
MIN_PYTHON = (3, 12)

ROOT_UID = 0

EXIT_READ_ALL = 0
EXIT_LOWER_LIMIT = 1
EXIT_REFUSED = 2

USAGE = "usage: bin/journal-scan.py <sessions root>"
OLD_PYTHON = "needs Python {}.{} or later".format(*MIN_PYTHON)
NOT_ROOT = "do not run this program as root"
NO_ROOT_DIR = "cannot open the sessions root as a directory"
LOWER_LIMIT = "files_not_read is not 0: each other number can be too low"

_LF = b"\n"

#: The operator names the sessions root, so the system resolves that path,
#: with each symbolic link in it. No open waits for the other end of a pipe.
_ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK

#: For each name below the sessions root: never through a symbolic link.
_DIR_FLAGS = _ROOT_FLAGS | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW

if sys.version_info < MIN_PYTHON:
    # This check is here and not in `main`, because an old Python cannot
    # load the class below.
    sys.stderr.write(f"journal-scan: {OLD_PYTHON}\n")
    sys.exit(EXIT_REFUSED)


@dataclass(slots=True)
class Counts:
    """Each number of the report."""

    files: int = 0
    lines: int = 0
    files_not_read: int = 0
    lines_not_json: int = 0
    lines_unfinished: int = 0
    lone_surrogate_lines: int = 0
    lone_surrogate_kinds: dict[str, int] = field(default_factory=dict[str, int])
    cut_events_small: int = 0

    def report(self) -> str:
        """The output: one `<name> <number>` line for each count."""
        kinds = sorted(self.lone_surrogate_kinds.items())
        rows = [
            ("files", self.files),
            ("lines", self.lines),
            ("files_not_read", self.files_not_read),
            ("lines_not_json", self.lines_not_json),
            ("lines_unfinished", self.lines_unfinished),
            ("lone_surrogate_lines", self.lone_surrogate_lines),
            *((f"lone_surrogate_lines.{kind}", number) for kind, number in kinds),
            ("cut_events_small", self.cut_events_small),
        ]

        return "".join(f"{name} {number}\n" for name, number in rows)


def main(argv: list[str], uid: int) -> int:
    """Scan the sessions root that `argv` names, as the user `uid`."""
    if uid == ROOT_UID:
        return _say(NOT_ROOT, EXIT_REFUSED)

    if len(argv) != 1:
        return _say(USAGE, EXIT_REFUSED)

    try:
        root = os.open(argv[0], _ROOT_FLAGS)
    except OSError as exc:
        return _say(f"{NO_ROOT_DIR}: {exc.strerror}", EXIT_REFUSED)

    counts = Counts()

    try:
        _scan_below(root, JOURNAL_DEPTH, counts)
    finally:
        os.close(root)

    sys.stdout.write(counts.report())

    if counts.files_not_read:
        return _say(LOWER_LIMIT, EXIT_LOWER_LIMIT)

    return EXIT_READ_ALL


def _say(reason: str, status: int) -> int:
    """Write one line to stderr, and give back the exit status that goes with it."""
    sys.stderr.write(f"journal-scan: {reason}\n")

    return status


def _scan_below(parent: int, depth: int, counts: Counts) -> None:
    """Scan the journal of each session directory `depth` levels below `parent`."""
    if depth == 0:
        _scan_journal(parent, counts)
        return

    try:
        names = os.listdir(parent)
    except OSError:
        counts.files_not_read += 1
        return

    for name in names:
        child = _open_dir(name, parent, counts)

        if child is None:
            continue

        try:
            _scan_below(child, depth - 1, counts)
        finally:
            os.close(child)


def _open_dir(name: str, parent: int, counts: Counts) -> int | None:
    """Open one directory in `parent`, or give None for a name to pass.

    A journal can be behind a symbolic link and in a directory that does not
    open, so each of the two counts as not read. A name of another type is no
    family directory and no session directory.
    """
    try:
        mode = os.lstat(name, dir_fd=parent).st_mode

        if stat.S_ISDIR(mode):
            return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError:
        counts.files_not_read += 1
        return None

    if stat.S_ISLNK(mode):
        counts.files_not_read += 1

    return None


def _scan_journal(session: int, counts: Counts) -> None:
    """Count the journal of one session directory, when the directory has one."""
    try:
        journal = os.open(JOURNAL_FILE, _FILE_FLAGS, dir_fd=session)
    except FileNotFoundError:
        return
    except OSError:
        counts.files += 1
        counts.files_not_read += 1
        return

    counts.files += 1

    try:
        whole = _read_to_end(journal, counts)
    except OSError:
        whole = False
    finally:
        os.close(journal)

    if not whole:
        counts.files_not_read += 1


def _read_to_end(journal: int, counts: Counts) -> bool:
    """Judge each line of one open journal. False for a name that is no regular file."""
    if not stat.S_ISREG(os.fstat(journal).st_mode):
        return False

    with open(journal, "rb", closefd=False) as lines:
        for raw in lines:
            if not raw.endswith(_LF):
                # The journal ends here for now. When the service ends this
                # line, a later read gives the rest of it, which is no line.
                counts.lines_unfinished += 1
                break

            _judge(raw, counts)

    return True


def _judge(raw: bytes, counts: Counts) -> None:
    """Count one line that ends with a newline."""
    counts.lines += 1
    record = _as_object(raw)

    if record is None:
        counts.lines_not_json += 1
        return

    kind = record.get("kind")

    if _holds_lone_surrogate(record):
        word = _kind_word(kind)
        counts.lone_surrogate_lines += 1
        counts.lone_surrogate_kinds[word] = counts.lone_surrogate_kinds.get(word, 0) + 1

    if kind == PI_EVENT and _is_small_cut(record.get("body")):
        counts.cut_events_small += 1


def _kind_word(kind: object) -> str:
    """The name under which the output counts a line of this `kind`."""
    if isinstance(kind, str) and KIND_WORD.fullmatch(kind):
        return kind

    return OTHER_KIND


def _as_object(raw: bytes) -> dict[str, object] | None:
    """The JSON object of one line, or None for a line that is none."""
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError: the bytes are not UTF-8, the text is not JSON, or an
        # integer has more digits than the interpreter reads. RecursionError:
        # the text nests deeper than the interpreter reads.
        return None

    if not isinstance(parsed, dict):
        return None

    return cast("dict[str, object]", parsed)


def _holds_lone_surrogate(value: object) -> bool:
    """True when UTF-8 cannot encode some key or some value, at any level of `value`.

    A list is the stack of the walk. A line can nest deeper than a function
    can call itself.
    """
    stack: list[object] = [value]

    while stack:
        item = stack.pop()

        if isinstance(item, str):
            if _has_no_utf8_form(item):
                return True
        elif isinstance(item, dict):
            members = cast("dict[str, object]", item)

            if any(_has_no_utf8_form(key) for key in members):
                return True

            stack.extend(members.values())
        elif isinstance(item, list):
            stack.extend(cast("list[object]", item))

    return False


def _has_no_utf8_form(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True

    return False


def _is_small_cut(body: object) -> bool:
    """Whether `body` is a cut event of MAX_EVENT_BYTES or less."""
    if not isinstance(body, dict):
        return False

    event = cast("dict[str, object]", body)
    size = event.get("original_bytes")

    if event.get("truncated") is not True:
        return False

    if not isinstance(size, int) or isinstance(size, bool):
        return False

    return 0 <= size <= MAX_EVENT_BYTES


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.geteuid()))
