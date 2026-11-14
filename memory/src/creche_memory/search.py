"""Search of the memory store: raw context with no LLM call (decision MM9).

A search asks Cognee twice. Graph completion with `only_context` gives the
facts of the knowledge graph near the query. Chunk search gives the matching
passages, and each passage maps back to its note or PDF through its document. The
calling agent reads both and reasons over them itself.

Cognee runs in one asyncio loop in one thread, `StoreLoop`. The syncer runs
there, and each search goes there from the thread of its connection."""

import asyncio
import threading
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .wire import (
    MAX_SNIPPET_CHARS,
    Passage,
    Refusal,
    RefusedReply,
    SearchReply,
    SearchRequest,
    SourcePath,
)

#: The longest text of graph facts that a reply carries.
MAX_FACTS_CHARS = 32 * 1024

#: The seconds a search may take, under the upstream's 20 s socket timeout.
SEARCH_TIMEOUT_S = 15.0

#: The seconds that a stop waits for the tasks of the loop to end.
STOP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Hit:
    """One passage that the chunk search found."""

    document_id: str
    text: str


#: The facts and the hits for a query and a count.
type Backend = Callable[[str, int], Awaitable[tuple[str, list[Hit]]]]

#: The vault path of each document, by its id.
type Paths = Callable[[], Awaitable[dict[str, str]]]


def _refused(code: Refusal, reason: str) -> RefusedReply:
    return RefusedReply(ok=False, refused=code, reason=reason)


def _passages(hits: list[Hit], paths: dict[str, str], count: int) -> list[Passage]:
    passages: list[Passage] = []
    for hit in hits:
        path = paths.get(hit.document_id)
        if path is None:
            continue

        try:
            passages.append(Passage(path=SourcePath(path), snippet=hit.text[:MAX_SNIPPET_CHARS]))
        except ValidationError:
            continue

        if len(passages) == count:
            break

    return passages


class Searcher:
    """Answers a search once the first sync has ended well."""

    def __init__(self, ready: threading.Event, backend: Backend, paths: Paths) -> None:
        self._ready = ready
        self._backend = backend
        self._paths = paths

    async def search(self, request: SearchRequest) -> SearchReply | RefusedReply:
        if not self._ready.is_set():
            return _refused(Refusal.NOT_READY, "the first sync of the vault has not ended")

        facts, hits = await self._backend(request.query, request.k)
        passages = _passages(hits, await self._paths(), request.k)

        return SearchReply(ok=True, op="search", facts=facts[:MAX_FACTS_CHARS], passages=passages)


class StoreLoop:
    """One asyncio loop in one thread. Every call into Cognee runs here."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def spawn[T](self, work: Coroutine[Any, Any, T]) -> Future[T]:
        return asyncio.run_coroutine_threadsafe(work, self._loop)

    def search(self, searcher: Searcher, request: SearchRequest) -> SearchReply | RefusedReply:
        """Run one search on the loop, from the thread of a connection."""
        future = asyncio.run_coroutine_threadsafe(searcher.search(request), self._loop)
        try:
            return future.result(SEARCH_TIMEOUT_S)
        except FutureTimeout:
            future.cancel()
            return _refused(Refusal.INTERNAL, f"the search took over {SEARCH_TIMEOUT_S:.0f} s")
        except Exception as error:
            # Only the type: a message can hold a URL.
            return _refused(Refusal.INTERNAL, f"the search failed: {type(error).__name__}")

    def cancel(self) -> bool:
        """Cancel each task and wait for it to end. `False`: a task was still
        running after `STOP_TIMEOUT_S`. The loop keeps running."""
        cancelling = asyncio.run_coroutine_threadsafe(_cancel_tasks(), self._loop)
        try:
            cancelling.result(STOP_TIMEOUT_S)
        except FutureTimeout:
            return False

        return True

    def stop(self) -> None:
        """Cancel each task, wait for it to end, then stop and close the loop."""
        self.cancel()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


async def _cancel_tasks() -> None:
    tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
