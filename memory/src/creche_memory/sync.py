"""The sync of the vault into the memory store (decisions MM6 and MM8).

The loop checks the vault every 30 seconds. It syncs once the vault has
stayed the same for 30 seconds, so a burst of edits makes one sync. A write
that the service confirmed asks for a sync at once. One process owns the
store, and one loop syncs it, so two syncs never run together.

The store ingests the whole vault except `house/ha` and `house/scrum`, which
agents generate (decision MM8). Cognee's Obsidian connector takes only the
folders to read, so the loop lists them at each check: each top-level folder,
and each folder beside a skipped one. A note directly in a folder that holds
a skipped folder, such as `house/<note>.md`, is not read. Nor is a note at
the vault root.

The PDFs of the same folders go in as a second source beside the notes
(`pdf.py`).

After a failed sync, the loop checks the graph database (`graph.py`). A
graph that does not open ends the loop: the next check cannot repair it."""

import asyncio
import hashlib
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import cast

from .graph import Dead, GraphCheck
from .pdf import PDF_SUFFIX, PdfTexts, pdf_source

POLL_S = 30.0
QUIET_S = 30.0

#: The dataset and the sync state of the vault. A fixed name, so a move of
#: the vault folder does not ingest every note again.
DATASET = "vault"
RESOURCE = "vault"

#: The folders that agents generate, which the store does not read (MM8).
SKIPPED: tuple[tuple[str, ...], ...] = (("house", "ha"), ("house", "scrum"))

#: Small batches: the model provider limits every family on one account,
#: and Cognee's default batches got 429 errors in the spike.
DATA_PER_BATCH = 1
CHUNKS_PER_BATCH = 4

NOTE_SUFFIX = ".md"

type Ingest = Callable[[list[str]], Awaitable[None]]
type Clock = Callable[[], float]


class Trigger(Enum):
    POLL = auto()
    WRITE = auto()


class Outcome(Enum):
    NOTHING_TO_READ = auto()
    UNCHANGED = auto()
    WAITING = auto()
    SYNCED = auto()
    FAILED = auto()


def _folders_in(path: Path) -> list[str]:
    try:
        entries = list(os.scandir(path))
    except FileNotFoundError:
        return []

    names = [one.name for one in entries if one.is_dir(follow_symlinks=False)]

    return sorted(name for name in names if not name.startswith("."))


def _expand(root: Path, prefix: tuple[str, ...]) -> list[str]:
    chosen: list[str] = []
    for name in _folders_in(root.joinpath(*prefix)):
        path = (*prefix, name)
        if path in SKIPPED:
            continue

        if any(skip[: len(path)] == path for skip in SKIPPED):
            chosen.extend(_expand(root, path))
        else:
            chosen.append("/".join(path))

    return chosen


def ingest_folders(root: Path) -> list[str]:
    """The vault-relative folders that the store reads, with their subfolders."""
    return _expand(root, ())


def vault_stamp(root: Path, folders: list[str]) -> str:
    """A digest of the folder list and of the path, size and time of each
    note and each PDF in it. An edit, a new file and a removed file each
    change it."""
    digest = hashlib.sha256("\n".join(folders).encode())
    for folder in folders:
        for directory, subdirs, files in os.walk(root / folder):
            subdirs[:] = sorted(name for name in subdirs if not name.startswith("."))
            for name in sorted(files):
                if name.startswith(".") or not (
                    name.endswith(NOTE_SUFFIX) or name.lower().endswith(PDF_SUFFIX)
                ):
                    continue

                path = Path(directory, name)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue

                line = f"{path.relative_to(root)}\0{info.st_size}\0{info.st_mtime_ns}\n"
                digest.update(line.encode())

    return digest.hexdigest()


@dataclass
class Debounce:
    """When a sync is due: after a quiet time, or at once after a write."""

    quiet_s: float
    synced: str | None = None
    seen: str | None = None
    seen_at: float = 0.0

    def due(self, stamp: str, now: float, trigger: Trigger) -> bool:
        if stamp != self.seen:
            self.seen, self.seen_at = stamp, now

        if trigger is Trigger.WRITE:
            return stamp != self.synced

        return stamp != self.synced and now - self.seen_at >= self.quiet_s

    def done(self, stamp: str) -> None:
        self.synced = stamp


async def _remember(source: object) -> None:
    import cognee

    # The type of `remember` leaves out the dlt resource that the Obsidian
    # connector returns, and that the connector's own docs pass to it.
    await cognee.remember(  # pyright: ignore[reportUnknownMemberType]
        source,  # pyright: ignore[reportArgumentType]
        dataset_name=DATASET,
        self_improvement=False,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        data_per_batch=DATA_PER_BATCH,
        chunks_per_batch=CHUNKS_PER_BATCH,
    )


async def in_daemon_thread[T](work: Callable[[], T]) -> T:
    """The result of `work`, run in a daemon thread of its own.

    `asyncio.to_thread` runs in a pool thread, and the exit of the process
    waits for each pool thread. A cancel does not end a thread. So a stop in
    a long read would wait for its end. The exit does not wait for a daemon
    thread."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[T] = loop.create_future()

    def settle(result: T | None, error: BaseException | None) -> None:
        if done.cancelled():
            return

        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(cast(T, result))

    def run() -> None:
        result: T | None = None
        error: BaseException | None = None
        try:
            result = work()
        except BaseException as caught:
            error = caught

        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:
            # The stop closed the loop first. Nothing waits for the result.
            return

    threading.Thread(target=run, daemon=True).start()

    return await done


def cognee_ingest(root: Path) -> Ingest:
    """The sync through Cognee's Obsidian connector, then the PDFs. An edited
    note or PDF replaces its old document, and a removed one is forgotten."""
    texts = PdfTexts()

    async def ingest(folders: list[str]) -> None:
        from cognee.tasks.ingestion.connectors import obsidian_source

        await _remember(obsidian_source(vault_path=root, resource_name=RESOURCE, folders=folders))

        # Each PDF is read in a child process. The reads run in a daemon
        # thread first, so the store loop keeps answering searches, and a
        # stop does not wait for the PDFs that are left.
        cut = texts.cut
        await in_daemon_thread(lambda: texts.warm(root, folders))
        if texts.cut > cut:
            print(f"memory: {texts.cut - cut} PDF(s) cut at the character cap", file=sys.stderr)

        await _remember(pdf_source(root, folders, texts))

    return ingest


def _say(line: str) -> None:
    print(f"memory: {line}", file=sys.stderr, flush=True)


class Syncer:
    """Runs the checks and the syncs of one vault."""

    def __init__(
        self,
        root: Path,
        ingest: Ingest,
        check: GraphCheck,
        *,
        poll_s: float = POLL_S,
        quiet_s: float = QUIET_S,
        clock: Clock = time.monotonic,
    ) -> None:
        self._root = root
        self._ingest = ingest
        self._check = check
        self._poll_s = poll_s
        self._clock = clock
        self._debounce = Debounce(quiet_s)
        self._wrote = threading.Event()
        self._wake = threading.Event()
        #: Set after the first sync that ends well. Search waits for it.
        self.ready = threading.Event()

    def request(self) -> None:
        """Ask for a sync at once, after a confirmed write. Any thread."""
        self._wrote.set()
        self._wake.set()

    def wake(self) -> None:
        """End the wait of `run` at once, so a stop is seen. Any thread."""
        self._wake.set()

    async def step(self) -> Outcome | Dead:
        folders = ingest_folders(self._root)
        if not folders:
            return Outcome.NOTHING_TO_READ

        # Clear the flag each time it is read. A write that does not move the
        # stamp would otherwise keep it set, and the loop would never sleep.
        trigger = Trigger.WRITE if self._wrote.is_set() else Trigger.POLL
        self._wrote.clear()
        stamp = vault_stamp(self._root, folders)
        if not self._debounce.due(stamp, self._clock(), trigger):
            return Outcome.UNCHANGED if stamp == self._debounce.synced else Outcome.WAITING

        # One line when a sync starts and one when it ends, so the journal
        # says when search is ready. The first sync takes many minutes.
        started = self._clock()
        _say(f"sync started: {len(folders)} folders")
        try:
            await self._ingest(folders)
        except Exception as error:
            # Only the type is printed: a message can hold a URL.
            _say(f"sync failed after {self._clock() - started:.0f} s: {type(error).__name__}")
            # A dead graph stops the loop. Any other failed sync does not:
            # the next check tries again.
            state = await self._check()
            return state if isinstance(state, Dead) else Outcome.FAILED

        self._debounce.done(stamp)
        first = not self.ready.is_set()
        self.ready.set()
        ready = "; search is ready" if first else ""
        _say(f"sync done in {self._clock() - started:.0f} s{ready}")

        return Outcome.SYNCED

    async def run(self, stop: threading.Event) -> Dead | None:
        """Check and sync until `stop` is set, or until a failed sync finds a
        dead graph. Return the dead graph, or `None` after a stop.

        `run` sets `stop` when it ends, for any reason, so the service stops
        with its sync loop. An error that ends it goes to the caller."""
        try:
            while not stop.is_set():
                # Clear before the check: a request during the check then
                # ends the next wait at once.
                self._wake.clear()
                outcome = await self.step()
                if isinstance(outcome, Dead):
                    return outcome

                await asyncio.to_thread(self._wake.wait, self._poll_s)
        finally:
            stop.set()

        return None
