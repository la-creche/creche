"""The inputs and the committed files of the group `noticeboard`.

No test here builds the vectors. A test reads the written inputs of
`noticeboard_cases.py`, or the committed files.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from vectors import generate
from vectors.core import INT_MAX, INT_MIN, Json, depth, has_surrogate
from vectors.surfaces import noticeboard
from vectors.surfaces import noticeboard_cases as cases

PREFIX = "noticeboard/"

#: The vector file of the surface `noticeboard.security.cookie`.
COOKIE_FILE = f"{PREFIX}security.cookie.json"

#: The deepest nesting that a strict reader of a contract takes.
STRICT_DEPTH = 64

CLASSES = frozenset(kind.value for kind in noticeboard.Kind)

#: One sentence of the noticeboard for each class.
SENTENCES = (
    ("", noticeboard.Kind.NONE),
    ("validation.json is missing", noticeboard.Kind.MISSING),
    ("the noticeboard-ro token file is empty", noticeboard.Kind.UNREADABLE),
    ("cannot list the audit directory: No such file or directory", noticeboard.Kind.UNREADABLE),
    ("attendance answered over 4194304 bytes; refusing to parse it", noticeboard.Kind.TOO_LARGE),
    (
        "2026-09-19.jsonl: a record passed 524288 bytes and was not parsed",
        noticeboard.Kind.TOO_LARGE,
    ),
    ("a journal line is not JSON: Expecting value", noticeboard.Kind.NOT_JSON),
    ("validation.json is list, not a JSON object", noticeboard.Kind.NOT_AN_OBJECT),
    ("attendance refused: not_found (404) no such session", noticeboard.Kind.REFUSED),
    ("attendance answered 502 with no error code", noticeboard.Kind.REFUSED),
    (cases.UNREACHABLE, noticeboard.Kind.UNREACHABLE),
    ("stopped after 5000 journal lines", noticeboard.Kind.CAPPED),
    ("showing the newest 30 day files of 31", noticeboard.Kind.CAPPED),
)


def _committed() -> dict[str, dict[str, Json]]:
    """Each committed vector file of the group, by path."""
    return {
        path: cast("dict[str, Json]", json.loads(text))
        for path, text in generate.committed().items()
        if path.startswith(PREFIX)
    }


def _walk(value: Json) -> Iterator[Json]:
    """The value, and each value inside it."""
    stack: list[Json] = [value]
    while stack:
        item = stack.pop()
        yield item
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def test_each_sentence_has_its_class() -> None:
    assert {kind for _, kind in SENTENCES} == set(noticeboard.Kind)

    for text, kind in SENTENCES:
        assert noticeboard.problem_of(text)["class"] == kind.value, text


def test_a_message_of_the_json_reader_is_in_no_problem() -> None:
    problem = noticeboard.problem_of("a journal line is not JSON: Expecting value: line 1")

    assert problem == {
        "class": noticeboard.Kind.NOT_JSON.value,
        "start": "a journal line is not JSON: ",
    }


def test_an_unknown_sentence_stops_the_generator() -> None:
    with pytest.raises(ValueError, match="no class"):
        noticeboard.problem_of("a sentence that the noticeboard did not have before")


def test_each_committed_problem_has_a_known_class() -> None:
    found = {
        str(item["class"])
        for document in _committed().values()
        for item in _walk(document["vectors"])
        if isinstance(item, dict) and "class" in item
    }

    assert found <= CLASSES
    assert noticeboard.Kind.NOT_JSON.value in found


def test_no_committed_file_holds_a_path_of_the_machine() -> None:
    marks = (noticeboard.SCRATCH_PREFIX, str(Path.home()))
    for path, text in generate.committed().items():
        if path.startswith(PREFIX):
            assert not [mark for mark in marks if mark in text], path


def test_the_index_holds_each_file_of_the_group() -> None:
    index = json.loads(generate.committed()[generate.INDEX_FILE])
    listed = {row["path"]: row["surface"] for row in index["surfaces"]}
    files = _committed()

    assert files
    for path, document in files.items():
        assert listed[path] == document["surface"]
        assert document["surface"] == path.removesuffix(".json").replace("/", ".")


# --- the written inputs --------------------------------------------------------------


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is no token of strict JSON")


def _one_time(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("an object holds a key two times")

    return dict(pairs)


def _strict(raw: bytes) -> None:
    """Raises `ValueError` for a JSON text that a strict reader refuses."""
    parsed: Json = json.loads(
        raw.decode("utf-8"), parse_constant=_refuse_constant, object_pairs_hook=_one_time
    )
    if depth(parsed) > STRICT_DEPTH:
        raise ValueError("the text nests too deep")

    for item in _walk(parsed):
        if isinstance(item, str) and has_surrogate(item):
            raise ValueError("a text holds one half of a surrogate pair")

        if isinstance(item, float) and item in (float("inf"), float("-inf")):
            raise ValueError("a number is not finite")

        if isinstance(item, int) and not INT_MIN <= item <= INT_MAX:
            raise ValueError("an integer is outside 64 bits")

        if isinstance(item, dict) and [key for key in item if has_surrogate(key)]:
            raise ValueError("a key holds one half of a surrogate pair")


def _json_texts() -> Iterator[tuple[str, bytes]]:
    """Each written input that a reader of the noticeboard gives to its JSON reader."""
    for answer in (*cases.LISTS, *cases.DETAILS, *cases.REFUSALS):
        yield answer.id, answer.body.data()

    for report in cases.REPORTS:
        if report.body is not None:
            yield report.id, report.body.data()

    streams = [answer.body for answer in cases.STREAMS] + list(cases.TRANSCRIPTS)
    days = [day for audit in cases.AUDITS for day in audit.files or ()]
    for body in (*streams, *days):
        for number, line in enumerate(body.data().split(b"\n")):
            yield f"{body.id}:{number}", line


def test_no_written_json_text_is_one_that_only_a_lax_reader_takes() -> None:
    """Python reads `NaN`, a key two times and an integer of each size. A strict reader does not."""
    for name, raw in _json_texts():
        try:
            json.loads(raw)
        except ValueError:
            continue

        try:
            _strict(raw)
        except ValueError as error:
            pytest.fail(f"{name}: {error}")


def test_the_strict_check_refuses_what_a_lax_reader_takes() -> None:
    lax = (
        b'{"a": NaN}',
        b'{"a": 1, "a": 2}',
        b'{"a": 123456789012345678901234567890}',
        b'{"a": 1e400}',
        b'{"a": "\\ud800"}',
        b"\xef\xbb\xbf{}",
        b'{"a":' + b"[" * STRICT_DEPTH + b"]" * STRICT_DEPTH + b"}",
    )
    for raw in lax:
        assert json.loads(raw) is not None
        with pytest.raises(ValueError):
            _strict(raw)

    _strict(b'{"a": [1, 2.5, "text", null, true, {"b": 18446744073709551615}]}')


def test_each_header_of_a_cookie_is_one_that_a_server_gives_an_app() -> None:
    """A server removes the space at the two ends of a value. No header here has a control byte."""
    for case in cases.COOKIES:
        raw = case.raw or b""

        assert raw == raw.strip(b" \t"), case.id
        assert not [byte for byte in raw if byte == 0x7F or (byte < 0x20 and byte != 0x09)], case.id


def test_each_cookie_token_is_in_the_bounds_of_the_surface() -> None:
    longest = max(len(case.raw or b"") for case in cases.COOKIES)

    assert longest == len(cases.COOKIE_NAME) + cases.COOKIE_BYTES


def _kept_tokens() -> list[str]:
    """Each token that the service read from a request, in the committed cookie vectors."""
    document = _committed()[COOKIE_FILE]
    vectors = cast("list[dict[str, dict[str, Json]]]", document["vectors"])
    tokens = [vector["value"]["token"] for vector in vectors]

    return [token for token in tokens if isinstance(token, str)]


def test_each_kept_cookie_token_is_a_text_of_cookie_octets() -> None:
    """The surface holds no token that the service keeps and a strict type of a token refuses."""
    kept = _kept_tokens()

    assert kept
    for token in kept:
        assert 1 <= len(token) <= cases.COOKIE_BYTES, token
        assert set(token) <= cases.COOKIE_OCTETS, token


def test_each_query_and_each_segment_is_ascii() -> None:
    for query in cases.QUERIES:
        assert query.data().isascii(), query.id

    for vector_id, text in cases.SEGMENTS:
        assert text.isascii(), vector_id
        assert "/" not in text, vector_id


def test_each_environment_of_a_check_names_each_path() -> None:
    """A default path can exist on a machine, and the program says if a path exists."""
    for case in cases.CHECKS:
        named = case.variables.keys()

        assert {"VIEW_STATE_ROOT", "VIEW_REGISTRY_DIR"} <= named, case.id
        assert {"VIEW_SESSIOND_SOCKET", "VIEW_SESSIOND_URL"} & named, case.id
        for value in case.variables.values():
            if value.startswith("/"):
                assert value.startswith(cases.NO_ROOT), case.id
