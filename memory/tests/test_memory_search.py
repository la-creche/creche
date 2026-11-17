"""A search waits for the first sync, maps each hit to its note, and never hangs."""

import asyncio
import threading
from collections.abc import Iterator

import pytest
from creche_memory import search as search_module
from creche_memory.search import MAX_FACTS_CHARS, Hit, Searcher, StoreLoop
from creche_memory.wire import (
    MAX_SNIPPET_CHARS,
    Family,
    Refusal,
    RefusedReply,
    SearchReply,
    SearchRequest,
)

PATHS = {
    "doc-printer": "house/memories/printer.md",
    "doc-dentist": "profile/memories/dentist.md",
    "doc-hidden": ".obsidian/app.md",
    "doc-bylaws": "house/01 - Governance/Bylaws.pdf",
    "doc-scan": "house/scan.png",
}


def _request(count: int = 3) -> SearchRequest:
    return SearchRequest(v=1, op="search", caller=Family("chat"), query="printer ink", k=count)


def _searcher(hits: list[Hit], facts: str = "Epson -- uses -- 522 ink") -> Searcher:
    ready = threading.Event()
    ready.set()

    async def backend(query: str, count: int) -> tuple[str, list[Hit]]:
        return facts, hits

    async def paths() -> dict[str, str]:
        return PATHS

    return Searcher(ready, backend, paths)


def _run(searcher: Searcher, request: SearchRequest) -> SearchReply | RefusedReply:
    return asyncio.run(searcher.search(request))


@pytest.fixture
def loop() -> Iterator[StoreLoop]:
    store = StoreLoop()
    store.start()
    yield store
    store.stop()


def test_a_search_before_the_first_sync_is_not_ready() -> None:
    async def never() -> dict[str, str]:
        raise AssertionError("the store was asked")

    async def backend(query: str, count: int) -> tuple[str, list[Hit]]:
        raise AssertionError("the store was asked")

    reply = _run(Searcher(threading.Event(), backend, never), _request())

    assert isinstance(reply, RefusedReply)
    assert reply.refused is Refusal.NOT_READY


def test_each_hit_carries_the_path_of_its_note() -> None:
    hits = [
        Hit("doc-printer", "Epson 522 ink bottles"),
        Hit("doc-gone", "a note removed since the search"),
        Hit("doc-hidden", "a path the wire types refuse"),
        Hit("doc-dentist", "x" * (MAX_SNIPPET_CHARS + 50)),
    ]
    reply = _run(_searcher(hits), _request())

    assert isinstance(reply, SearchReply)
    assert reply.facts == "Epson -- uses -- 522 ink"
    assert [passage.path.root for passage in reply.passages] == [
        "house/memories/printer.md",
        "profile/memories/dentist.md",
    ]
    assert len(reply.passages[1].snippet) == MAX_SNIPPET_CHARS


def test_a_hit_from_a_pdf_carries_the_path_of_its_pdf() -> None:
    hits = [Hit("doc-bylaws", "Article 4: pets"), Hit("doc-scan", "neither a note nor a PDF")]
    reply = _run(_searcher(hits), _request())

    assert isinstance(reply, SearchReply)
    assert [passage.path.root for passage in reply.passages] == ["house/01 - Governance/Bylaws.pdf"]


def test_a_search_keeps_k_passages_and_caps_the_facts() -> None:
    hits = [Hit("doc-printer", f"passage {index}") for index in range(5)]
    reply = _run(_searcher(hits, facts="f" * (MAX_FACTS_CHARS + 1)), _request(count=2))

    assert isinstance(reply, SearchReply)
    assert len(reply.passages) == 2
    assert len(reply.facts) == MAX_FACTS_CHARS


def test_the_loop_runs_a_search_from_another_thread(loop: StoreLoop) -> None:
    reply = loop.search(_searcher([Hit("doc-printer", "Epson")]), _request())

    assert isinstance(reply, SearchReply)
    assert reply.passages[0].path.root == "house/memories/printer.md"


def test_a_slow_search_is_refused(loop: StoreLoop, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_module, "SEARCH_TIMEOUT_S", 0.1)
    ready = threading.Event()
    ready.set()

    async def slow(query: str, count: int) -> tuple[str, list[Hit]]:
        await asyncio.sleep(5)
        return "", []

    async def paths() -> dict[str, str]:
        return PATHS

    reply = loop.search(Searcher(ready, slow, paths), _request())

    assert isinstance(reply, RefusedReply)
    assert reply.refused is Refusal.INTERNAL


def test_a_failed_search_names_only_the_error_type(loop: StoreLoop) -> None:
    ready = threading.Event()
    ready.set()

    async def broken(query: str, count: int) -> tuple[str, list[Hit]]:
        raise ConnectionError("http://secret.test/ refused")

    async def paths() -> dict[str, str]:
        return PATHS

    reply = loop.search(Searcher(ready, broken, paths), _request())

    assert isinstance(reply, RefusedReply)
    assert reply.refused is Refusal.INTERNAL
    assert "ConnectionError" in reply.reason
    assert "secret.test" not in reply.reason
