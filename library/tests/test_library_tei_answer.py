"""The answers of TEI: `index-scope` reads each one as strict JSON.

`REFUSED` holds one answer for each rule of `strictjson.py`. Each answer
breaks the rule of its row and no other rule. The row of `SYNTAX` is an
answer that is not JSON, so each other row must end as that row ends.

A fake TEI answers over a mock transport. No test opens a socket.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest
from library.__main__ import ANSWER_MAX_BYTES, TEI_URL_ENV, TeiEmbedder, main
from library.library import DIMS
from library.strictjson import DEPTH_MAX, INTEGER_MAX, NotStrictJson, Rule

#: An address that no test dials: the mock transport answers each request.
URL = "http://tei.test:1"

INFO_PATH = "/info"

#: The model of the fake TEI. An obvious fixture.
MODEL = "standin/embed-768"

#: The first text that `index-scope` embeds, before it reads the corpus.
CANARY = "canary"

#: The status of a preflight that fails, and of a run that publishes a store.
EXIT_PREFLIGHT = 2
EXIT_OK = 0

#: A text that is in one note of the corpus only, and the name of that note.
MARKED_WORD = "Tuesday"
MARKED_NOTE = "meeting.md"
OTHER_NOTE = "bikes.md"

#: A text inside a refused answer. No line that the program writes holds it.
PRIVATE_TEXT = "text-of-the-answer"

#: One answer for each rule. The first one has one byte more than the cap.
REFUSED: tuple[tuple[Rule, bytes], ...] = (
    (Rule.TOO_LARGE, b"[" + b" " * (ANSWER_MAX_BYTES - 1) + b"]"),
    (Rule.NOT_UTF8, b'["\xff"]'),
    (Rule.BYTE_ORDER_MARK, b"\xef\xbb\xbf[]"),
    (Rule.SYNTAX, b"<html>no JSON</html>"),
    (Rule.CONSTANT, b"[NaN]"),
    (Rule.TRAILING_DATA, b"[] []"),
    (Rule.TOO_DEEP, b"[" * (DEPTH_MAX + 1) + b"]" * (DEPTH_MAX + 1)),
    (Rule.DUPLICATE_KEY, b'{"model_id": "a", "model_id": "b"}'),
    (Rule.LONE_SURROGATE, b'["\\ud800"]'),
    (Rule.INTEGER_RANGE, f"[{INTEGER_MAX + 1}]".encode()),
    (Rule.FLOAT_RANGE, b"[1e999]"),
)

each_rule = pytest.mark.parametrize(
    ("rule", "body"), REFUSED, ids=[rule.value for rule, _ in REFUSED]
)


def vector_of(text: str) -> list[float]:
    """The vector of one text: the count of its characters, then zeros."""
    return [float(len(text))] + [0.0] * (DIMS - 1)


class FakeTei:
    """The TEI of a test. A test sets one attribute to change one answer."""

    def __init__(self) -> None:
        #: The body of the answer of `GET /info`.
        self.info = json.dumps({"model_id": MODEL}).encode()

        #: The body of each answer of `POST /embed`. `None` gives the vector
        #: of each text.
        self.embed: bytes | None = None

        #: The body for a call of `/embed` with `MARKED_WORD` in one of its
        #: texts. `None` gives that call the answer of each other call.
        self.marked: bytes | None = None

        #: The texts of each call of `/embed`, in the order of the calls.
        self.embedded: list[list[str]] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == INFO_PATH:
            return httpx.Response(200, content=self.info)

        texts: list[str] = json.loads(request.content)["inputs"]
        self.embedded.append(texts)
        if self.marked is not None and any(MARKED_WORD in text for text in texts):
            return httpx.Response(200, content=self.marked)

        if self.embed is not None:
            return httpx.Response(200, content=self.embed)

        return httpx.Response(200, content=json.dumps([vector_of(text) for text in texts]).encode())


@pytest.fixture
def tei(monkeypatch: pytest.MonkeyPatch) -> FakeTei:
    """Give each `httpx.Client` of the program the fake TEI as its transport."""
    fake = FakeTei()
    real_client = httpx.Client

    def client(**options: float) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(fake.answer), **options)

    monkeypatch.setattr("library.__main__.httpx.Client", client)

    return fake


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A corpus of two notes, and the command line and the address of a run."""
    scope = tmp_path / "notes"
    scope.mkdir()
    (scope / MARKED_NOTE).write_text(f"The meeting is on {MARKED_WORD} in the lobby.")
    (scope / OTHER_NOTE).write_text("The rules for bicycle storage changed in March.")
    monkeypatch.setenv(TEI_URL_ENV, URL)
    monkeypatch.setattr("sys.argv", ["index-scope", str(scope), str(index_dir(scope))])

    return scope


def index_dir(scope: Path) -> Path:
    return scope.parent / "index"


def indexed_notes(scope: Path) -> list[str]:
    """The name of each note that the published store holds."""
    store = sqlite3.connect(index_dir(scope) / "store.db")
    try:
        return sorted(Path(row[0]).name for row in store.execute("SELECT path FROM files"))
    finally:
        store.close()


def refusal_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """The level and the text of each log line that names a refused answer."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if "json_not_strict" in record.getMessage()
    ]


def line_of(rule: Rule) -> tuple[int, str]:
    return logging.ERROR, f"json_not_strict embedding.answer {rule.value}"


# --- the table and the cap --------------------------------------------------------------


def test_the_table_holds_one_answer_for_each_rule() -> None:
    assert [rule for rule, _ in REFUSED] == list(Rule)


def test_the_cap_of_an_answer_is_16_mebibytes() -> None:
    assert ANSWER_MAX_BYTES == 16_777_216


def test_an_answer_of_exactly_the_cap_is_read(tei: FakeTei) -> None:
    tei.embed = b"[" + b" " * (ANSWER_MAX_BYTES - 2) + b"]"

    assert len(tei.embed) == ANSWER_MAX_BYTES
    assert TeiEmbedder(URL).embed([CANARY]) == []


# --- a strict answer ---------------------------------------------------------------------


def test_a_strict_answer_is_read_and_writes_no_line(
    tei: FakeTei, caplog: pytest.LogCaptureFixture
) -> None:
    embedder = TeiEmbedder(URL)

    assert embedder.model == MODEL
    assert embedder.embed(["a", "bcd"]) == [vector_of("a"), vector_of("bcd")]
    assert refusal_lines(caplog) == []


def test_an_info_answer_with_no_model_id_gives_the_model_unknown(tei: FakeTei) -> None:
    tei.info = b'{"max_client_batch_size": 32}'

    assert TeiEmbedder(URL).model == "unknown"


# --- the two reads -----------------------------------------------------------------------


@each_rule
def test_an_info_answer_that_is_not_strict_is_refused(
    rule: Rule, body: bytes, tei: FakeTei, caplog: pytest.LogCaptureFixture
) -> None:
    tei.info = body

    with pytest.raises(NotStrictJson) as refused:
        TeiEmbedder(URL)

    assert refused.value.rule is rule
    assert refusal_lines(caplog) == [line_of(rule)]


@each_rule
def test_an_embed_answer_that_is_not_strict_is_refused(
    rule: Rule, body: bytes, tei: FakeTei, caplog: pytest.LogCaptureFixture
) -> None:
    tei.embed = body
    embedder = TeiEmbedder(URL)

    with pytest.raises(NotStrictJson) as refused:
        embedder.embed([CANARY])

    assert refused.value.rule is rule
    assert refusal_lines(caplog) == [line_of(rule)]


# --- the end of a run --------------------------------------------------------------------


@each_rule
def test_a_refused_info_answer_ends_the_preflight(
    rule: Rule,
    body: bytes,
    tei: FakeTei,
    corpus: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tei.info = body

    assert main() == EXIT_PREFLIGHT
    assert "TEI preflight failed" in capsys.readouterr().err
    assert refusal_lines(caplog) == [line_of(rule)]
    assert tei.embedded == []
    assert not index_dir(corpus).exists()


@each_rule
def test_a_refused_canary_answer_ends_the_preflight(
    rule: Rule,
    body: bytes,
    tei: FakeTei,
    corpus: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tei.embed = body

    assert main() == EXIT_PREFLIGHT
    assert "TEI preflight failed" in capsys.readouterr().err
    assert refusal_lines(caplog) == [line_of(rule)]
    assert tei.embedded == [[CANARY]]
    assert not index_dir(corpus).exists()


def test_a_canary_of_another_length_ends_the_preflight(
    tei: FakeTei, corpus: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tei.embed = json.dumps([[0.0, 1.0]]).encode()

    assert main() == EXIT_PREFLIGHT
    assert f"expected {DIMS}" in capsys.readouterr().err
    assert not index_dir(corpus).exists()


@each_rule
def test_a_refused_answer_for_one_file_is_an_error_of_that_file(
    rule: Rule,
    body: bytes,
    tei: FakeTei,
    corpus: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule 4 of `library/AGENTS.md`: the run publishes the store with each other file."""
    tei.marked = body

    assert main() == EXIT_OK

    report = capsys.readouterr().out
    assert "1 indexed" in report
    assert f"ERROR {corpus.resolve() / MARKED_NOTE}: {rule.value}" in report
    assert refusal_lines(caplog) == [line_of(rule)]
    assert indexed_notes(corpus) == [OTHER_NOTE]


def test_no_line_holds_a_part_of_a_refused_answer(
    tei: FakeTei,
    corpus: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tei.marked = f'["{PRIVATE_TEXT}", NaN]'.encode()
    main()
    tei.info = f'{{"model_id": "{PRIVATE_TEXT}", "model_id": "{PRIVATE_TEXT}"}}'.encode()
    main()

    written = capsys.readouterr()
    assert [line for _, line in refusal_lines(caplog)] == [
        "json_not_strict embedding.answer constant",
        "json_not_strict embedding.answer duplicate_key",
    ]
    assert PRIVATE_TEXT not in written.out + written.err + caplog.text
