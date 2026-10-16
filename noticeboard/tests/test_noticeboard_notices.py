"""The notice file of a refused JSON text (`notices.py`).

Each test gives the module a fixed clock or a clock that moves one second
for each call. A test that ends with a notice file also checks that the
directory holds one file whose name ends in `.json`: the host script reads
each such file.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from noticeboard.strictjson import Rule

from noticeboard import notices, strictjson

#: The time of a refusal: a count of seconds from 1970, and its text.
AT = 1_760_000_000
AT_TEXT = "2025-10-09T08:53:20Z"

SERVICE = "noticeboard"
SURFACE = "status.document"

#: The modes that the module gives the file and a directory that it makes.
FILE_MODE = 0o640
DIR_MODE = 0o750

#: The pattern of the line of the key `push`, as `bin/rework-watchdog.sh`
#: holds it: `PUSH_LINE`, with each token `<surface>:<rule>:<seconds>`.
NAME = r"[a-z0-9_.]{1,64}"
TOKEN = rf"{NAME}:{NAME}:[0-9]{{1,12}}"
PUSH_LINE = rf' "push": "({TOKEN}( {TOKEN})*)?",?'


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts before the first `configure` call, and the state of
    the module goes back after it."""
    monkeypatch.setattr(notices, "_settings", None)


def ticking(start: int = AT) -> Callable[[], float]:
    """A clock that gives `start`, then one second more for each call."""
    moments = iter(range(start, start + 10_000))

    return lambda: float(next(moments))


def configure(path: Path | None, clock: Callable[[], float] | None = None) -> None:
    notices.configure(SERVICE, path, clock=clock or (lambda: float(AT)))


def document(path: Path) -> dict[str, object]:
    found = strictjson.loads(path.read_bytes(), cap=1 << 20)
    assert isinstance(found, dict)

    return found


def rows(path: Path) -> list[dict[str, object]]:
    found = document(path)["rows"]
    assert isinstance(found, list)

    return found


def push(path: Path) -> str:
    found = document(path)["push"]
    assert isinstance(found, str)

    return found


def json_names(directory: Path) -> list[str]:
    return sorted(one.name for one in directory.iterdir() if one.name.endswith(".json"))


@pytest.fixture
def notice_file(tmp_path: Path) -> Iterator[Path]:
    """The path of the notice file. After the test, the directory holds one
    file whose name ends in `.json`, or no such file."""
    path = tmp_path / "notices" / f"{SERVICE}.json"
    yield path

    if path.parent.exists():
        assert json_names(path.parent) in ([], [path.name])


# --- the log line --------------------------------------------------------------------


def test_a_record_writes_one_log_line_of_level_error(
    notice_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    configure(notice_file)

    with caplog.at_level(logging.INFO):
        notices.record(SURFACE, Rule.SYNTAX)

    assert [(one.levelno, one.getMessage()) for one in caplog.records] == [
        (logging.ERROR, "json_not_strict noticeboard status.document syntax")
    ]


def test_a_record_before_configure_writes_the_word_unset_and_no_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        notices.record(SURFACE, Rule.CONSTANT, "chat")

    assert caplog.messages == ["json_not_strict unset status.document constant"]
    assert list(tmp_path.iterdir()) == []


def test_a_configure_with_no_path_writes_the_log_line_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    configure(None)

    with caplog.at_level(logging.INFO):
        notices.record(SURFACE, Rule.CONSTANT)

    assert caplog.messages == ["json_not_strict noticeboard status.document constant"]
    assert list(tmp_path.iterdir()) == []


def test_configure_reads_no_file_and_writes_no_file(notice_file: Path) -> None:
    configure(notice_file)

    assert not notice_file.parent.exists()


def test_configure_refuses_a_service_word_that_the_host_script_does_not_read(
    notice_file: Path,
) -> None:
    for word in ("", "Noticeboard", "notice board", "notice_board", "n" * 65, "a/b"):
        with pytest.raises(ValueError, match="service"):
            notices.configure(word, notice_file)


# --- the rows ------------------------------------------------------------------------


def test_a_refusal_gives_one_row_with_the_count_1(notice_file: Path) -> None:
    configure(notice_file)

    notices.record(SURFACE, Rule.SYNTAX, "chat")

    assert document(notice_file) == {
        "kind": "notices",
        "service": SERVICE,
        "rows": [
            {
                "surface": SURFACE,
                "rule": "syntax",
                "family": "chat",
                "count": 1,
                "first_at": AT_TEXT,
                "last_at": AT_TEXT,
            }
        ],
        "push": f"{SURFACE}:syntax:{AT}",
    }


def test_a_second_refusal_of_the_pair_gives_the_count_2(notice_file: Path) -> None:
    configure(notice_file, ticking())

    notices.record(SURFACE, Rule.SYNTAX, "chat")
    notices.record(SURFACE, Rule.SYNTAX, "code")

    [row] = rows(notice_file)
    assert row["count"] == 2
    assert row["first_at"] == AT_TEXT
    assert row["last_at"] == "2025-10-09T08:53:21Z"
    # The family of a row is the family of its last refusal.
    assert row["family"] == "code"


def test_each_pair_has_a_row_of_its_own_in_the_order_of_the_words(notice_file: Path) -> None:
    configure(notice_file)

    notices.record("status.outcome", Rule.CONSTANT)
    notices.record(SURFACE, Rule.SYNTAX)
    notices.record(SURFACE, Rule.CONSTANT)

    assert [(one["surface"], one["rule"], one["family"]) for one in rows(notice_file)] == [
        (SURFACE, "constant", None),
        (SURFACE, "syntax", None),
        ("status.outcome", "constant", None),
    ]
    assert push(notice_file) == (
        f"{SURFACE}:constant:{AT} {SURFACE}:syntax:{AT} status.outcome:constant:{AT}"
    )


def test_a_second_configure_on_the_path_keeps_the_rows(notice_file: Path) -> None:
    configure(notice_file)
    notices.record(SURFACE, Rule.SYNTAX)

    configure(notice_file)
    notices.record(SURFACE, Rule.SYNTAX)

    assert [one["count"] for one in rows(notice_file)] == [2]


def test_a_file_that_is_no_notice_document_counts_as_no_rows(notice_file: Path) -> None:
    configure(notice_file)
    good = {
        "kind": "notices",
        "service": SERVICE,
        "rows": [
            {
                "surface": SURFACE,
                "rule": "syntax",
                "family": None,
                "count": 4,
                "first_at": AT_TEXT,
                "last_at": AT_TEXT,
            }
        ],
        "push": "",
    }
    broken: list[object] = [
        b"",
        b"{",
        b"[]",
        {**good, "kind": "faults"},
        {**good, "service": "caregiver"},
        {**good, "more": 1},
        {**good, "push": f"{SURFACE}:syntax"},
        {**good, "push": f"{SURFACE}:constant:{AT}"},
        {**good, "push": f" {SURFACE}:syntax:{AT}"},
        {**good, "rows": [{**good["rows"][0], "count": 0}]},  # type: ignore[index]
        {**good, "rows": [{**good["rows"][0], "count": True}]},  # type: ignore[index]
        {**good, "rows": [{**good["rows"][0], "rule": "Syntax"}]},  # type: ignore[index]
        {**good, "rows": [{**good["rows"][0], "first_at": "2025-10-09T08:53:20+00:00"}]},  # type: ignore[index]
        {**good, "rows": [good["rows"][0], good["rows"][0]]},  # type: ignore[index]
    ]
    for content in broken:
        data = content if isinstance(content, bytes) else json.dumps(content).encode()
        notice_file.parent.mkdir(exist_ok=True)
        notice_file.write_bytes(data)

        notices.record(SURFACE, Rule.SYNTAX)

        assert [one["count"] for one in rows(notice_file)] == [1], data

    # The document of the module keeps its rows.
    notice_file.write_text(json.dumps(good))
    notices.record(SURFACE, Rule.SYNTAX)

    assert [one["count"] for one in rows(notice_file)] == [5]


def test_a_count_at_the_largest_integer_of_63_bits_stays_there(notice_file: Path) -> None:
    configure(notice_file)
    full = 2**63 - 1
    row = {
        "surface": SURFACE,
        "rule": "syntax",
        "family": None,
        "count": full,
        "first_at": AT_TEXT,
        "last_at": AT_TEXT,
    }
    notice_file.parent.mkdir()
    notice_file.write_text(
        json.dumps({"kind": "notices", "service": SERVICE, "rows": [row], "push": ""})
    )

    notices.record(SURFACE, Rule.SYNTAX)

    assert [one["count"] for one in rows(notice_file)] == [full]


# --- the text `push` -----------------------------------------------------------------


def test_a_proven_refusal_adds_one_token_to_push(notice_file: Path) -> None:
    configure(notice_file, ticking())

    notices.record(SURFACE, Rule.SYNTAX)

    assert push(notice_file) == f"{SURFACE}:syntax:{AT}"


def test_a_second_proven_refusal_gives_the_token_a_newer_time(notice_file: Path) -> None:
    configure(notice_file, ticking())

    notices.record(SURFACE, Rule.SYNTAX)
    notices.record(SURFACE, Rule.SYNTAX)

    assert push(notice_file) == f"{SURFACE}:syntax:{AT + 1}"


def test_a_refusal_with_no_proof_changes_the_row_and_not_push(notice_file: Path) -> None:
    configure(notice_file, ticking())

    notices.record(SURFACE, Rule.SYNTAX, proven=False)

    assert push(notice_file) == ""
    assert [one["count"] for one in rows(notice_file)] == [1]

    notices.record(SURFACE, Rule.SYNTAX)
    notices.record(SURFACE, Rule.SYNTAX, proven=False)

    assert push(notice_file) == f"{SURFACE}:syntax:{AT + 1}"
    [row] = rows(notice_file)
    assert row["count"] == 3
    assert row["last_at"] == "2025-10-09T08:53:22Z"


def test_the_line_of_push_holds_the_pattern_of_the_host_script(notice_file: Path) -> None:
    import re

    configure(notice_file)
    for surface in ("status.document", "status.outcome", "audit.line", "session.answer"):
        for rule in Rule:
            notices.record(surface, rule)

    lines = notice_file.read_text(encoding="utf-8").splitlines()
    held = [one for one in lines if re.search(r'"push"[ \t]*:', one)]

    assert len(held) == 1
    assert re.fullmatch(PUSH_LINE, held[0])
    assert held[0].count(":") == 1 + 2 * 4 * len(Rule)


def test_two_runs_with_one_clock_write_the_same_bytes(tmp_path: Path) -> None:
    written: list[bytes] = []
    for run in ("one", "two"):
        path = tmp_path / run / f"{SERVICE}.json"
        configure(path)
        notices.record(SURFACE, Rule.SYNTAX, "chat")
        notices.record("audit.line", Rule.CONSTANT)
        notices.record(SURFACE, Rule.SYNTAX, proven=False)
        written.append(path.read_bytes())

    assert written[0] == written[1]
    assert written[0] == strictjson.dumps(json.loads(written[0]), indent=1, sort_keys=True).encode(
        "utf-8"
    )


# --- the modes and the faults ----------------------------------------------------------


def test_under_a_umask_of_077_the_modes_are_0640_and_0750(notice_file: Path) -> None:
    configure(notice_file)

    was = os.umask(0o077)
    try:
        notices.record(SURFACE, Rule.SYNTAX)
    finally:
        os.umask(was)

    assert stat.S_IMODE(notice_file.stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(notice_file.parent.stat().st_mode) == DIR_MODE


def test_a_directory_that_exists_keeps_its_mode(notice_file: Path) -> None:
    notice_file.parent.mkdir(mode=0o700)
    configure(notice_file)

    notices.record(SURFACE, Rule.SYNTAX)

    assert stat.S_IMODE(notice_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(notice_file.stat().st_mode) == FILE_MODE


def test_the_lock_file_and_the_temporary_file_do_not_end_in_json(notice_file: Path) -> None:
    configure(notice_file)

    notices.record(SURFACE, Rule.SYNTAX)

    names = sorted(one.name for one in notice_file.parent.iterdir())
    assert names == [f"{SERVICE}.json", f"{SERVICE}.json.lock"]


def test_the_temporary_file_does_not_end_in_json_before_the_rename(
    notice_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(notice_file)
    seen: list[list[str]] = []
    rename = os.replace

    def watched(source: str, target: Path) -> None:
        seen.append(json_names(notice_file.parent))
        rename(source, target)

    monkeypatch.setattr(notices.os, "replace", watched)

    notices.record(SURFACE, Rule.SYNTAX)
    notices.record(SURFACE, Rule.SYNTAX)

    assert seen == [[], [f"{SERVICE}.json"]]


def test_a_path_that_cannot_be_written_leaves_the_log_line_and_raises_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The parent of the path is a file, so no directory can be made there.
    blocker = tmp_path / "notices"
    blocker.write_bytes(b"")
    configure(blocker / f"{SERVICE}.json")

    with caplog.at_level(logging.INFO):
        notices.record(SURFACE, Rule.SYNTAX)

    assert caplog.messages == ["json_not_strict noticeboard status.document syntax"]
    assert blocker.read_bytes() == b""


def test_a_directory_that_cannot_be_made_leaves_the_log_line_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    configure(tmp_path / "absent" / "notices" / f"{SERVICE}.json")

    with caplog.at_level(logging.INFO):
        notices.record(SURFACE, Rule.SYNTAX)

    assert caplog.messages == ["json_not_strict noticeboard status.document syntax"]
    assert list(tmp_path.iterdir()) == []


def test_a_lock_that_another_process_holds_for_1_second_leaves_the_log_line_only(
    notice_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    configure(notice_file)
    notices.record(SURFACE, Rule.SYNTAX)
    before = notice_file.read_bytes()
    caplog.clear()

    lock = os.open(f"{notice_file}.lock", os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with caplog.at_level(logging.INFO):
            notices.record(SURFACE, Rule.SYNTAX)
    finally:
        os.close(lock)

    assert caplog.messages == ["json_not_strict noticeboard status.document syntax"]
    assert notice_file.read_bytes() == before


def test_a_surface_word_that_the_host_script_does_not_read_writes_no_row(
    notice_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    configure(notice_file)

    with caplog.at_level(logging.INFO):
        for word in ("", "Status.document", "status document", "status:document", "s" * 65):
            notices.record(word, Rule.SYNTAX)

    assert len(caplog.messages) == 5
    assert not notice_file.exists()


def test_a_clock_that_gives_no_time_of_a_record_leaves_the_log_line_only(
    notice_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> float:
        raise OSError("no clock")

    for clock in (lambda: -1.0, lambda: 1e300, lambda: float("nan"), broken):
        configure(notice_file, clock)
        with caplog.at_level(logging.INFO):
            notices.record(SURFACE, Rule.SYNTAX)

    assert caplog.messages == ["json_not_strict noticeboard status.document syntax"] * 4
    assert not notice_file.exists()


def test_a_family_that_is_no_strict_text_leaves_the_log_line_only(
    notice_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    configure(notice_file)

    with caplog.at_level(logging.INFO):
        notices.record(SURFACE, Rule.SYNTAX, "chat\udcff")

    assert caplog.messages == ["json_not_strict noticeboard status.document syntax"]
    assert not notice_file.exists()


# --- more than one writer ---------------------------------------------------------------


def test_threads_of_one_process_keep_each_refusal(notice_file: Path) -> None:
    configure(notice_file)

    def fifty() -> None:
        for _ in range(50):
            notices.record(SURFACE, Rule.SYNTAX)

    threads = [threading.Thread(target=fifty) for _ in range(4)]
    for one in threads:
        one.start()
    for one in threads:
        one.join()

    assert [one["count"] for one in rows(notice_file)] == [200]


#: One process that records 50 refusals of one pair on the path of argv[1].
RECORD_FIFTY = """
import sys
from pathlib import Path
from noticeboard import notices
from noticeboard.strictjson import Rule

notices.configure("noticeboard", Path(sys.argv[1]))
for _ in range(50):
    notices.record("status.document", Rule.SYNTAX)
"""


def test_two_processes_that_each_record_50_refusals_leave_the_count_100(
    notice_file: Path,
) -> None:
    runs = [
        subprocess.Popen([sys.executable, "-c", RECORD_FIFTY, str(notice_file)]) for _ in range(2)
    ]
    for one in runs:
        assert one.wait(timeout=120) == 0

    assert [one["count"] for one in rows(notice_file)] == [100]


# --- the source of the module ------------------------------------------------------------


def test_the_module_holds_no_client_no_bearer_and_no_variable() -> None:
    source = Path(notices.__file__).read_text(encoding="utf-8")

    for word in ("http", "urllib", "socket", "Bearer", "environ", "getenv", "Thread("):
        assert word not in source, word
