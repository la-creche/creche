"""`bin/journal-scan.py`: each count against a known answer, and what a run leaves as it was.

The program reports on the journal files that `attendance` writes. Each test
here writes its journals by hand, as bytes, in a scratch directory. No test
starts the service, so the bytes of a line are exactly what the test says.

Most tests start the program as a child. The child gets no variable, no site
directory and so no package of the venv: that is how a host starts it. Some
tests load the program in this process. Only there can a test say that the
user is root, name another Python version, or change what the system gives
to the program.

The program cannot import `attendance`, so it has its own copy of some values
of that package. The last tests compare each copy with the value in
`attendance`. One of them takes the path of a journal and the keys of a cut
event from that package.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Self

import pytest
from attendance.models import JournalLine, LineKind
from attendance.paths import JOURNAL_FILE, journal_file
from attendance.wire import MAX_EVENT_BYTES, cap_event

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "journal-scan.py"

#: `-I` gives the child no variable of Python and no directory of the user.
#: `-S` gives it no site directory, so it can import the standard library only.
ALONE = ("-I", "-S")

#: Three texts that no output can hold. None of them is a `kind` that the
#: program can name: each one has an upper case letter, a digit and a `-`.
FAMILY = "FAM-41c9"
SESSION = "SES-77e2"
MARKER = "MRK-90ab"

ROOT_UID = 0

#: A user that is not root, for a call of the entry function in this process.
SOME_USER = 1000

EXIT_READ_ALL = 0
EXIT_LOWER_LIMIT = 1
EXIT_REFUSED = 2

#: The rows that each output has, in the order of the output. The rows of
#: COUNT 1 for one `kind` come after LONE.
FILES = "files"
LINES = "lines"
NOT_READ = "files_not_read"
NO_JSON = "lines_not_json"
UNFINISHED = "lines_unfinished"
LONE = "lone_surrogate_lines"
CUT = "cut_events_small"
ROWS_TO_LONE = (FILES, LINES, NOT_READ, NO_JSON, UNFINISHED, LONE)

#: The largest `original_bytes` that COUNT 2 takes.
EDGE = 262_144

#: Deeper than a Python function can call itself, and less deep than the
#: limit of `json.loads` in each supported Python version.
DEEP = 2_000

#: More digits than `json.loads` takes for one integer. Its limit is 4300 in
#: each supported Python version.
MANY_DIGITS = 5_000

#: The longest `kind` that the output names.
LONGEST_KIND = "a" * 32

LONE_HIGH = rb"\ud83d"
LONE_LOW = rb"\ude00"


def _line(kind: bytes, body: bytes) -> bytes:
    """One line in the form of the journal writer, with `kind` and `body` as they are."""
    head = b'{"journal_seq":1,"ts":"2026-01-01T00:00:00.000Z","kind":'

    return head + kind + b',"turn":null,"body":' + body + b"}\n"


def _event(kind: bytes, truncated: bytes, original_bytes: bytes) -> bytes:
    """One line whose body has the three keys of a cut event."""
    body = b'{"type":"message_end","truncated":' + truncated
    body += b',"original_bytes":' + original_bytes + b"}"

    return _line(kind, body)


def _lone(kind: str) -> tuple[str, ...]:
    """The rows of a line that COUNT 1 holds under `kind`."""
    return (LONE, f"{LONE}.{kind}")


#: A whole line that COUNT 1 holds under `note`.
LONE_NOTE = _line(b'"note"', rb'{"text":"\ud800"}')

#: A whole line that is in no count but `lines`.
PLAIN = _line(b'"note"', b'{"text":"plain"}')

#: Each line of test 1 that ends with a newline: its name, its bytes, and
#: each row beside `lines` to which it adds 1.
CASES: list[tuple[str, bytes, tuple[str, ...]]] = [
    # COUNT 1: a surrogate escape that has no partner.
    (
        "lone-high-value",
        _line(b'"turn_queued"', b'{"prompt":"' + MARKER.encode() + LONE_HIGH + b'"}'),
        _lone("turn_queued"),
    ),
    ("lone-low-value", _line(b'"note"', b'{"text":"' + LONE_LOW + b'"}'), _lone("note")),
    ("lone-key", _line(b'"note"', rb'{"k\ud800":1}'), _lone("note")),
    (
        "lone-three-deep-in-an-event",
        _line(b'"pi_event"', rb'{"type":"message_update","a":{"b":{"c":"\udc00"}}}'),
        _lone("pi_event"),
    ),
    (
        "lone-below-many-levels",
        _line(b'"note"', b"[" * DEEP + b'"' + LONE_HIGH + b'"' + b"]" * DEEP),
        _lone("note"),
    ),
    # The bytes of one surrogate with no escape. `json.loads` gives a text
    # that UTF-8 cannot encode.
    ("lone-as-bytes", _line(b'"note"', b'{"text":"\xed\xa0\x80"}'), _lone("note")),
    # The longest `kind` that the output names.
    (
        "lone-kind-longest",
        _line(b'"' + LONGEST_KIND.encode() + b'"', rb'{"text":"\ud800"}'),
        _lone(LONGEST_KIND),
    ),
    # A `kind` that the output cannot name counts under `other`.
    (
        "lone-kind-too-long",
        _line(b'"a' + LONGEST_KIND.encode() + b'"', rb'{"text":"\ud800"}'),
        _lone("other"),
    ),
    (
        "lone-kind-no-word",
        _line(b'"' + MARKER.encode() + b'"', rb'{"text":"\udfff"}'),
        _lone("other"),
    ),
    # One character outside `a` to `z` and `_` is sufficient: a digit, an
    # upper case letter, a `-`.
    ("lone-kind-digit", _line(b'"note1"', rb'{"text":"\ud800"}'), _lone("other")),
    ("lone-kind-upper-case", _line(b'"Note"', rb'{"text":"\ud800"}'), _lone("other")),
    ("lone-kind-dash", _line(b'"a-b"', rb'{"text":"\ud800"}'), _lone("other")),
    ("lone-kind-final-newline", _line(rb'"note\n"', rb'{"text":"\ud800"}'), _lone("other")),
    ("lone-kind-empty", _line(b'""', rb'{"text":"\ud800"}'), _lone("other")),
    ("lone-kind-no-text", _line(b"7", rb'{"text":"\ud800"}'), _lone("other")),
    ("lone-in-the-kind", _line(rb'"note\ud800"', b"{}"), _lone("other")),
    # No COUNT 1: a full pair, and a backslash before the letters `ud800`.
    ("full-pair", _line(b'"note"', b'{"text":"' + LONE_HIGH + LONE_LOW + b'"}'), ()),
    ("escaped-backslash", _line(b'"note"', rb'{"text":"\\ud800"}'), ()),
    ("plain", PLAIN, ()),
    # COUNT 2: a cut event from 0 bytes to the edge.
    ("cut-at-the-edge", _event(b'"pi_event"', b"true", b"%d" % EDGE), (CUT,)),
    ("cut-at-zero", _event(b'"pi_event"', b"true", b"0"), (CUT,)),
    # No COUNT 2.
    ("cut-above-the-edge", _event(b'"pi_event"', b"true", b"%d" % (EDGE + 1)), ()),
    ("cut-below-zero", _event(b'"pi_event"', b"true", b"-1"), ()),
    ("not-truncated", _event(b'"pi_event"', b"false", b"7"), ()),
    ("truncated-is-a-number", _event(b'"pi_event"', b"1", b"7"), ()),
    ("size-is-a-boolean", _event(b'"pi_event"', b"true", b"true"), ()),
    ("size-is-a-float", _event(b'"pi_event"', b"true", b"7.0"), ()),
    ("size-is-a-text", _event(b'"pi_event"', b"true", b'"7"'), ()),
    ("cut-of-another-kind", _event(b'"note"', b"true", b"7"), ()),
    # No JSON object.
    ("no-json", b"no JSON " + MARKER.encode() + b"\n", (NO_JSON,)),
    ("a-list", b"[1,2]\n", (NO_JSON,)),
    ("too-deep", b"[" * 100_000 + b"\n", (NO_JSON,)),
    ("integer-too-long", _line(b'"note"', b'{"n":' + b"1" * MANY_DIGITS + b"}"), (NO_JSON,)),
    ("no-utf8", _line(b'"note"', b'{"text":"\xff"}'), (NO_JSON,)),
    ("empty", b"\n", (NO_JSON,)),
]

#: How many journals `_write_journals` makes.
JOURNALS = 3

#: What the program prints for the journals of `_write_journals`: each line
#: of CASES, and one unfinished line. Each number is counted by hand from the
#: table above.
KNOWN_ANSWER = b"""\
files 3
lines 35
files_not_read 0
lines_not_json 6
lines_unfinished 1
lone_surrogate_lines 16
lone_surrogate_lines.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1
lone_surrogate_lines.note 4
lone_surrogate_lines.other 9
lone_surrogate_lines.pi_event 1
lone_surrogate_lines.turn_queued 1
cut_events_small 2
"""


def _scan(*args: str | Path) -> subprocess.CompletedProcess[bytes]:
    """Run the program as a child that has no variable and no venv."""
    if os.geteuid() == ROOT_UID:
        pytest.skip("the program refuses to run as root")

    return subprocess.run(
        [sys.executable, *ALONE, str(SCRIPT), *(str(one) for one in args)],
        env={},
        capture_output=True,
        timeout=300,
        check=False,
    )


def _journal(root: Path, family: str, session: str, body: bytes) -> Path:
    path = root / family / session / JOURNAL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)

    return path


def _write_journals(root: Path) -> None:
    """The journals of test 1, and each file that is no journal.

    A file that is no journal holds a line of COUNT 1. A scan that reads one
    of them gives a number above the known answer. The last line of the first
    journal has no newline, and it is a line of COUNT 1 too.
    """
    lines = [line for _name, line, _adds in CASES]
    half = len(lines) // 2

    _journal(root, FAMILY, SESSION, b"".join(lines[:half]) + LONE_NOTE.removesuffix(b"\n"))
    _journal(root, FAMILY, "second", b"".join(lines[half:]))
    _journal(root, "other-family", "third", b"")
    (root / "other-family" / "no-journal").mkdir()

    # The name of a journal one level too high, two levels too high and one
    # level too deep.
    (root / FAMILY / JOURNAL_FILE).write_bytes(LONE_NOTE)
    (root / JOURNAL_FILE).write_bytes(LONE_NOTE)
    (root / FAMILY / SESSION / "turns").mkdir()
    (root / FAMILY / SESSION / "turns" / JOURNAL_FILE).write_bytes(LONE_NOTE)

    # Another name in a session directory, and a file where a session
    # directory can be.
    (root / FAMILY / SESSION / "session.json").write_bytes(LONE_NOTE)
    (root / FAMILY / "a-file").write_bytes(LONE_NOTE)


def _report(numbers: dict[str, int]) -> bytes:
    """The output with `numbers`, and with 0 in each other row that each output has."""
    kinds = sorted(row for row in numbers if row.startswith(f"{LONE}."))
    rows = (*ROWS_TO_LONE, *kinds, CUT)

    return "".join(f"{row} {numbers.get(row, 0)}\n" for row in rows).encode()


# ------------------------------------------------------------ test 1: counts


def test_each_count_equals_its_known_answer(tmp_path: Path) -> None:
    _write_journals(tmp_path)

    done = _scan(tmp_path)

    assert done.returncode == EXIT_READ_ALL, done.stderr
    assert done.stdout == KNOWN_ANSWER
    assert done.stderr == b""


@pytest.mark.parametrize(
    ("line", "adds"),
    [(line, adds) for _name, line, adds in CASES],
    ids=[name for name, _line_bytes, _adds in CASES],
)
def test_one_line_alone_counts_as_the_table_says(
    tmp_path: Path, line: bytes, adds: tuple[str, ...]
) -> None:
    """Two wrong numbers can hide each other in a sum. Here each line is alone."""
    _journal(tmp_path, FAMILY, SESSION, line)

    done = _scan(tmp_path)

    assert done.returncode == EXIT_READ_ALL, done.stderr
    assert done.stdout == _report({FILES: 1, LINES: 1, **dict.fromkeys(adds, 1)})


def test_the_known_answer_is_the_sum_of_the_table() -> None:
    """KNOWN_ANSWER is written by hand. This holds it to the rows of CASES."""
    added = Counter(row for _name, _line_bytes, adds in CASES for row in adds)

    assert _report({FILES: JOURNALS, LINES: len(CASES), UNFINISHED: 1, **added}) == KNOWN_ANSWER


def test_a_root_with_no_journal_gives_each_count_as_zero(tmp_path: Path) -> None:
    done = _scan(tmp_path)

    assert done.returncode == EXIT_READ_ALL
    assert done.stdout == _report({})
    assert done.stderr == b""


def test_an_unfinished_line_is_in_no_other_count(tmp_path: Path) -> None:
    _journal(tmp_path, FAMILY, SESSION, PLAIN + LONE_NOTE.removesuffix(b"\n"))

    done = _scan(tmp_path)

    assert done.returncode == EXIT_READ_ALL
    assert done.stdout == _report({FILES: 1, LINES: 1, UNFINISHED: 1})


class _LateWriter:
    """What the program gets from `open`, for one journal that grows.

    After the first read of the program, a writer adds `rest` to the journal.
    """

    def __init__(self, lines: BinaryIO, journal: Path, rest: bytes) -> None:
        self._lines = lines
        self._journal = journal
        self._rest = rest

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lines.close()

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> bytes:
        raw = self._lines.readline()

        if not raw:
            raise StopIteration

        if self._rest:
            with self._journal.open("ab") as writer:
                writer.write(self._rest)

            self._rest = b""

        return raw


def test_the_rest_of_an_unfinished_line_is_no_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The service ends its last line when the program is at the end of the journal.

    A second read then gives the rest of that line, with a newline. The rest
    is no line of the journal, so the program must not count it.
    """
    cut = len(PLAIN) // 2
    journal = _journal(tmp_path, FAMILY, SESSION, PLAIN[:cut])

    def late_open(fd: int, mode: str, *, closefd: bool) -> _LateWriter:
        assert mode == "rb"

        return _LateWriter(open(fd, "rb", closefd=closefd), journal, PLAIN[cut:])

    program: dict[str, Any] = runpy.run_path(
        str(SCRIPT), init_globals={"open": late_open}, run_name="journal_scan"
    )

    status = program["main"]([str(tmp_path)], SOME_USER)

    assert status == EXIT_READ_ALL
    assert capsys.readouterr().out.encode() == _report({FILES: 1, UNFINISHED: 1})
    assert journal.read_bytes() == PLAIN


# --------------------------------------------------------- test 2: read-only


def _state(top: Path) -> dict[str, tuple[int, int, int]]:
    """For `top` and for each name below it: `st_mode`, `st_size` and `st_ctime_ns`."""
    seen: dict[str, tuple[int, int, int]] = {}

    for folder, folders, files in os.walk(top):
        for name in (".", *folders, *files):
            info = os.lstat(os.path.join(folder, name))
            seen[os.path.join(folder, name)] = (info.st_mode, info.st_size, info.st_ctime_ns)

    return seen


#: How many symbolic links `_write_links` makes.
LINKS = 4


def _write_links(scratch: Path, root: Path) -> None:
    """Symbolic links below `root`: three to a journal outside it, one to no file."""
    journal = _journal(scratch / "outside", "family", "session", LONE_NOTE)

    (root / "linked-family").symlink_to(scratch / "outside" / "family", target_is_directory=True)
    (root / "linked-nowhere").symlink_to(scratch / "absent")
    (root / FAMILY).mkdir(exist_ok=True)
    (root / FAMILY / "linked-session").symlink_to(journal.parent, target_is_directory=True)
    (root / FAMILY / "has-a-link").mkdir()
    (root / FAMILY / "has-a-link" / JOURNAL_FILE).symlink_to(journal)


def test_a_run_changes_no_file_and_no_directory(tmp_path: Path) -> None:
    """The state holds the scratch directory too, so the root gets no new file beside it."""
    root = tmp_path / "sessions"
    root.mkdir()
    _write_journals(root)
    _write_links(tmp_path, root)
    before = _state(tmp_path)

    done = _scan(root)

    assert done.returncode == EXIT_LOWER_LIMIT, done.stderr
    assert _state(tmp_path) == before


# ----------------------------------------------------------- test 3: refusals


def _refused(done: subprocess.CompletedProcess[bytes]) -> bool:
    """Status 2, no count, and one line on stderr."""
    one_line = done.stderr.startswith(b"journal-scan: ") and done.stderr.count(b"\n") == 1

    return done.returncode == EXIT_REFUSED and done.stdout == b"" and one_line


def test_no_argument_is_refused() -> None:
    assert _refused(_scan())


def test_two_arguments_are_refused(tmp_path: Path) -> None:
    assert _refused(_scan(tmp_path, tmp_path))


def test_a_root_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    assert _refused(_scan(tmp_path / "missing"))


def test_a_root_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"")

    assert _refused(_scan(tmp_path / "file"))


def test_the_user_root_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _journal(tmp_path, FAMILY, SESSION, PLAIN)
    program: dict[str, Any] = runpy.run_path(str(SCRIPT), run_name="journal_scan")

    status = program["main"]([str(tmp_path)], ROOT_UID)

    printed = capsys.readouterr()
    assert status == EXIT_REFUSED
    assert printed.out == ""
    assert printed.err.startswith("journal-scan: ") and printed.err.count("\n") == 1


def test_an_old_python_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes at the load of the program, before a line that an old Python cannot run."""
    major, minor = runpy.run_path(str(SCRIPT), run_name="journal_scan")["MIN_PYTHON"]

    with monkeypatch.context() as patch, pytest.raises(SystemExit) as refused:
        patch.setattr(sys, "version_info", (major, minor - 1, 99, "final", 0))
        runpy.run_path(str(SCRIPT), run_name="journal_scan")

    printed = capsys.readouterr()
    assert refused.value.code == EXIT_REFUSED
    assert printed.out == ""
    assert printed.err.startswith("journal-scan: ") and printed.err.count("\n") == 1


def test_a_journal_that_does_not_open_counts_as_not_read(tmp_path: Path) -> None:
    journal = _journal(tmp_path, FAMILY, SESSION, PLAIN)
    journal.chmod(0o000)

    try:
        done = _scan(tmp_path)
    finally:
        journal.chmod(0o600)

    assert done.returncode == EXIT_LOWER_LIMIT
    assert done.stdout == _report({FILES: 1, NOT_READ: 1})
    assert done.stderr.startswith(b"journal-scan: ") and done.stderr.count(b"\n") == 1


def test_a_journal_that_no_user_can_write_is_read(tmp_path: Path) -> None:
    """The program asks for read access only. A request to write fails on this journal."""
    journal = _journal(tmp_path, FAMILY, SESSION, PLAIN)
    journal.chmod(0o444)

    done = _scan(tmp_path)

    assert done.returncode == EXIT_READ_ALL, done.stderr
    assert done.stdout == _report({FILES: 1, LINES: 1})


@pytest.mark.parametrize("closed", [FAMILY, f"{FAMILY}/{SESSION}"], ids=["family", "session"])
def test_a_directory_that_does_not_open_counts_as_not_read(tmp_path: Path, closed: str) -> None:
    _journal(tmp_path, FAMILY, SESSION, PLAIN)
    (tmp_path / closed).chmod(0o000)

    try:
        done = _scan(tmp_path)
    finally:
        (tmp_path / closed).chmod(0o700)

    assert done.returncode == EXIT_LOWER_LIMIT
    assert done.stdout == _report({NOT_READ: 1})


def test_a_symbolic_link_is_not_followed_and_counts_as_not_read(tmp_path: Path) -> None:
    """Three links go to a journal with a line of COUNT 1. COUNT 1 stays 0.

    Only the link with the name of a journal is a journal that the program
    found.
    """
    root = tmp_path / "sessions"
    root.mkdir()
    _write_links(tmp_path, root)

    done = _scan(root)

    assert done.returncode == EXIT_LOWER_LIMIT
    assert done.stdout == _report({FILES: 1, NOT_READ: LINKS})


def test_a_directory_that_becomes_a_link_is_not_followed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name can change to a symbolic link after the program looked at it.

    Here the look of the program goes through each link, so each link is a
    directory for the program. The open that comes next must still refuse the
    link. Both links go to a journal with a line of COUNT 1.
    """
    journal = _journal(tmp_path / "outside", "family", "session", LONE_NOTE)
    root = tmp_path / "sessions"
    (root / FAMILY).mkdir(parents=True)
    (root / "linked-family").symlink_to(journal.parent.parent, target_is_directory=True)
    (root / FAMILY / "linked-session").symlink_to(journal.parent, target_is_directory=True)
    program: dict[str, Any] = runpy.run_path(str(SCRIPT), run_name="journal_scan")

    def through_links(name: str, *, dir_fd: int) -> os.stat_result:
        return os.stat(name, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", through_links)
        status = program["main"]([str(root)], SOME_USER)

    assert status == EXIT_LOWER_LIMIT
    assert capsys.readouterr().out.encode() == _report({NOT_READ: 2})


def test_a_directory_with_the_name_of_a_journal_counts_as_not_read(tmp_path: Path) -> None:
    (tmp_path / FAMILY / SESSION / JOURNAL_FILE).mkdir(parents=True)

    done = _scan(tmp_path)

    assert done.returncode == EXIT_LOWER_LIMIT
    assert done.stdout == _report({FILES: 1, NOT_READ: 1})


def test_a_pipe_with_the_name_of_a_journal_counts_as_not_read(tmp_path: Path) -> None:
    """No process writes to the pipe. A program that waits for one does not end."""
    (tmp_path / FAMILY / SESSION).mkdir(parents=True)
    os.mkfifo(tmp_path / FAMILY / SESSION / JOURNAL_FILE)

    done = _scan(tmp_path)

    assert done.returncode == EXIT_LOWER_LIMIT
    assert done.stdout == _report({FILES: 1, NOT_READ: 1})


def test_a_root_that_is_a_symbolic_link_is_read(tmp_path: Path) -> None:
    """The operator names the root, so the link of the root itself is followed."""
    _journal(tmp_path / "real", FAMILY, SESSION, PLAIN)
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    done = _scan(tmp_path / "link")

    assert done.returncode == EXIT_READ_ALL
    assert done.stdout == _report({FILES: 1, LINES: 1})


# ------------------------------------------------------------ test 4: no leak


def test_no_output_holds_a_name_or_a_text_of_a_line(tmp_path: Path) -> None:
    """One run reads each journal. A second run also finds what it cannot read."""
    root = tmp_path / "sessions"
    root.mkdir()
    _write_journals(root)
    whole = _scan(root)

    _write_links(tmp_path, root)
    (root / FAMILY / f"{SESSION}-link").symlink_to(tmp_path / "absent")
    closed_journal = _journal(root, FAMILY, f"{SESSION}-a", PLAIN)
    closed_session = _journal(root, FAMILY, f"{SESSION}-b", PLAIN).parent
    closed_journal.chmod(0o000)
    closed_session.chmod(0o000)

    try:
        partial = _scan(root)
    finally:
        closed_journal.chmod(0o600)
        closed_session.chmod(0o700)

    assert whole.returncode == EXIT_READ_ALL
    assert partial.returncode == EXIT_LOWER_LIMIT
    assert b"%s %d\n" % (NOT_READ.encode(), LINKS + 3) in partial.stdout
    for done in (whole, partial):
        for secret in (FAMILY, SESSION, MARKER):
            assert secret.encode() not in done.stdout
            assert secret.encode() not in done.stderr


# --------------------------------------------------------- test 5: one source


def test_the_header_names_the_operator_and_the_command() -> None:
    """bin/AGENTS.md: who runs a script, and the exact command, in the first three lines."""
    program: dict[str, Any] = runpy.run_path(str(SCRIPT), run_name="journal_scan")
    shebang, who, command = SCRIPT.read_text(encoding="utf-8").splitlines()[:3]

    assert shebang == "#!/usr/bin/env python3"
    assert who.startswith("# OPERATOR")
    assert program["USAGE"] == "usage: " + command.removeprefix("#").strip()


def test_the_oldest_python_has_one_source() -> None:
    """The root `pyproject.toml` names the oldest Python version of a test run.

    The program has a copy of that version, and its header and its refusal
    name the same version.
    """
    program: dict[str, Any] = runpy.run_path(str(SCRIPT), run_name="journal_scan")
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    needs = SCRIPT.read_text(encoding="utf-8").splitlines()[3]
    major, minor = program["MIN_PYTHON"]

    assert project["project"]["requires-python"] == f">={major}.{minor}"
    assert needs.startswith(f"# Needs Python {major}.{minor} or later")
    assert program["OLD_PYTHON"] == f"needs Python {major}.{minor} or later"


def test_each_constant_equals_its_source_in_attendance() -> None:
    program: dict[str, Any] = runpy.run_path(str(SCRIPT), run_name="journal_scan")

    assert program["JOURNAL_FILE"] == JOURNAL_FILE
    assert program["PI_EVENT"] == LineKind.PI_EVENT.value
    assert program["MAX_EVENT_BYTES"] == MAX_EVENT_BYTES
    assert MAX_EVENT_BYTES == EDGE


def test_a_cut_event_of_attendance_counts_at_its_own_path(tmp_path: Path) -> None:
    """Here `attendance` gives the path of the journal and each key of the line.

    Each other test writes its paths and its keys by hand. This test fails
    when `attendance` moves the journal, or gives a key of a line or of a cut
    event another name. The event has a lone surrogate, so `cap_event` cuts
    it, and the cut form has none.
    """
    event = cap_event({"type": "message_update", "text": "a\ud83d"})
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    line = JournalLine(journal_seq=1, ts=moment, kind=LineKind.PI_EVENT, turn=None, body=event)
    journal = journal_file(tmp_path, FAMILY, SESSION)
    journal.parent.mkdir(parents=True)
    journal.write_bytes(json.dumps(line.to_api(), separators=(",", ":")).encode() + b"\n")

    done = _scan(tmp_path)

    assert done.returncode == EXIT_READ_ALL, done.stderr
    assert done.stdout == _report({FILES: 1, LINES: 1, CUT: 1})
