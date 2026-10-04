"""The field helpers every reader goes through (`jsonfiles.py`)."""

from __future__ import annotations

import tracemalloc
from collections.abc import Callable
from pathlib import Path

from noticeboard import jsonfiles

#: An integer that JSON can write and that no float can hold.
PAST_EVERY_FLOAT = 10**400

#: A file far past the cap of the reader, and the most memory its read may take.
FAR_PAST_THE_CAP = 64 * jsonfiles.MAX_DOC_BYTES
READ_MEMORY_MAX = 4 * jsonfiles.MAX_DOC_BYTES


def peak_memory_of[T](call: Callable[[], T]) -> tuple[T, int]:
    """What `call` returns, and the most bytes it held at one time."""
    tracemalloc.start()
    try:
        result = call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return result, peak


def test_a_number_reads_as_a_float() -> None:
    assert jsonfiles.number({"spend_usd": 3}, "spend_usd") == 3.0
    assert jsonfiles.number({"spend_usd": 3.42}, "spend_usd") == 3.42


def test_a_field_that_is_not_a_number_reads_none() -> None:
    assert jsonfiles.number({"spend_usd": "3.42"}, "spend_usd") is None
    assert jsonfiles.number({"spend_usd": True}, "spend_usd") is None
    assert jsonfiles.number({}, "spend_usd") is None


def test_an_integer_past_every_float_reads_none() -> None:
    """`float` raises OverflowError on this integer."""
    assert jsonfiles.number({"spend_usd": PAST_EVERY_FLOAT}, "spend_usd") is None
    assert jsonfiles.number({"spend_usd": -PAST_EVERY_FLOAT}, "spend_usd") is None


def test_a_name_that_is_no_path_reports_and_does_not_raise(tmp_path: Path) -> None:
    """`open` raises ValueError for a name with a NUL, not OSError."""
    body, problem = jsonfiles.read_object(tmp_path / "validation\x00.json")

    assert body is None
    assert problem == "cannot read 'validation\\x00.json': the system takes no such path"


def test_a_file_past_the_cap_is_refused_and_not_read_whole(tmp_path: Path) -> None:
    """The size of a file must not set the memory of a page."""
    path = tmp_path / "status.json"
    with path.open("wb") as handle:
        handle.truncate(FAR_PAST_THE_CAP)

    (body, problem), peak = peak_memory_of(lambda: jsonfiles.read_object(path))

    assert body is None
    assert problem == f"status.json is over {jsonfiles.MAX_DOC_BYTES} bytes; refusing to parse it"
    assert peak < READ_MEMORY_MAX


def test_a_file_of_exactly_the_cap_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    text = b'{"kind": "attended"}'
    path.write_bytes(text + b" " * (jsonfiles.MAX_DOC_BYTES - len(text)))

    body, problem = jsonfiles.read_object(path)

    assert problem is None
    assert body == {"kind": "attended"}
