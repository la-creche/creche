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
graph that does not open ends the loop: the next check cannot repair it.
After any other failed sync, the loop waits before the next one, and the
wait doubles with each failed sync in a row, up to 30 minutes. From
2026-11-04 to 2026-11-09, a sync that failed every 8 to 9 minutes filled
the journal (`memory/AGENTS.md`, the journal).

A sync in which the model's answer for some notes never fits the
extraction schema is a partial sync, not a failed one. It sets `ready`, and
those notes sync again at the next change of the vault. On 2026-11-15 one
such note kept search `not_ready` (`memory/AGENTS.md`, the partial sync)."""

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
from uuid import UUID

from .graph import Dead, GraphCheck
from .pdf import PDF_SUFFIX, PdfTexts, pdf_source

POLL_S = 30.0
QUIET_S = 30.0

#: The longest wait before the next sync after failed syncs in a row.
BACKOFF_MAX_S = 1800.0

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

#: The time between two compactions of the vector store during a sync.
#: Each upsert adds a fragment to a LanceDB table and reads all the others,
#: and Cognee compacts only after a run that completed.
COMPACT_EVERY_S = 120.0

NOTE_SUFFIX = ".md"

#: A sync of the given folders. It returns the count of notes and PDFs whose
#: graph extraction failed, and it raises for any other failure.
type Ingest = Callable[[list[str]], Awaitable[int]]
type Clock = Callable[[], float]


class Trigger(Enum):
    POLL = auto()
    WRITE = auto()


class Outcome(Enum):
    NOTHING_TO_READ = auto()
    UNCHANGED = auto()
    WAITING = auto()
    BACKING_OFF = auto()
    SYNCED = auto()
    PARTIAL = auto()
    FAILED = auto()


def backoff_s(failures: int, first_s: float) -> float:
    """The wait before the next sync after `failures` failed syncs in a row:
    `first_s` after one, then twice as long for each more, at most
    `BACKOFF_MAX_S`."""
    # The exponent stops at 32, so a long run of failures cannot overflow
    # the float. 2**32 polls is far past the cap.
    return min(first_s * 2 ** min(failures - 1, 32), BACKOFF_MAX_S)


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


def failed_extractions(runs: object) -> int:
    """The count of data items whose graph extraction failed, from the runs
    that `cognify(..., raise_on_error=False)` returns.

    An item counts only when the model answered and no answer fitted the
    extraction schema (Cognee's `is_structured_output_validation_error`).
    Cognee keeps those items' chunks, so search finds their text. Any other
    failed item raises, as `cognify` raises by default: an auth, a budget or
    a quota error fails every item that is left. A failed run with no failed
    item raises too."""
    from cognee.infrastructure.llm.exceptions import is_structured_output_validation_error
    from cognee.modules.pipelines.exceptions import AfterRunHookError, CognifyFailedError
    from cognee.modules.pipelines.models.PipelineRunInfo import (
        PipelineRunCompleted,
        PipelineRunErrored,
    )

    # The runs map each dataset id to its run. The sync has one dataset.
    found = list(cast(dict[object, object], runs).values()) if isinstance(runs, dict) else [runs]

    # The step after a completed run failed. `cognify` raises this by default.
    hooks = [one for one in found if isinstance(one, PipelineRunCompleted) and one.after_run_error]
    if hooks:
        failures: list[tuple[UUID, str, BaseException | str]] = [
            (one.dataset_id, one.dataset_name, one.after_run_error or "") for one in hooks
        ]
        raise AfterRunHookError(failures)

    run = next((one for one in found if isinstance(one, PipelineRunErrored)), None)
    if run is None:
        return 0

    # One entry per data item: `run_info`, and for a failed item `error`.
    # Its type is a bare `list`.
    entries = cast(list[object], getattr(run, "data_ingestion_info", None) or [])
    errors: list[object] = []
    for entry in entries:
        item = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        if isinstance(item.get("run_info"), PipelineRunErrored):
            errors.append(item.get("error"))

    if errors and all(
        isinstance(error, Exception) and is_structured_output_validation_error(error)
        for error in errors
    ):
        return len(errors)

    raise CognifyFailedError(run.dataset_name, run.error_class, run.error_message)


async def _add(source: object) -> None:
    import cognee

    # The dlt keywords go to the Obsidian connector's resource. Cognee's
    # `add` takes them as `**kwargs`, and its type is partly unknown.
    await cognee.add(  # pyright: ignore[reportUnknownMemberType]
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        data_per_batch=DATA_PER_BATCH,
    )


async def _compact() -> None:
    """One pass of Cognee's compaction of the vector store. It returns a
    bare `dict` of counts per table, which the sync does not read."""
    from cognee.infrastructure.databases.vector import compact_vector_store as compaction

    # Read at each pass, as a test replaces it to count the passes.
    await compaction.compact_vector_store()  # pyright: ignore[reportUnknownMemberType]


async def _compact_every(seconds: float) -> None:
    """Compact the vector store every `seconds`, until a cancel.

    A pass rewrites at most 4 groups of fragments
    (`DEFAULT_MAX_TASKS_PER_RUN`), so a large backlog drains over several
    passes. The rewrite takes the lock of each upsert, so a pass and an
    upsert never write together. A pass that starts while one runs returns
    at once."""
    while True:
        await asyncio.sleep(seconds)
        await _compact()


async def _cognify() -> int:
    """One graph extraction of each item of the dataset that needs one.

    One call for the notes and the PDFs together: Cognee rolls back the graph
    of a whole run with a failed item, and a second call would extract each
    of those items again. With `raise_on_error=False`, a run that failed
    items returns them. An error of the run itself still raises.

    Cognee compacts the vector store only after a run that completed. On
    2026-11-16 the graph write of one item grew from 15 s to 16 minutes in
    one run. So the store compacts every `COMPACT_EVERY_S` during the run,
    and once after a run that failed items. A compaction that fails fails
    the sync, as it does after a completed run."""
    import cognee

    compacting = asyncio.create_task(_compact_every(COMPACT_EVERY_S))
    try:
        runs = await cognee.cognify(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            datasets=[DATASET],
            data_per_batch=DATA_PER_BATCH,
            chunks_per_batch=CHUNKS_PER_BATCH,
            raise_on_error=False,
        )
    finally:
        # A pass that runs ends before the cancel takes effect.
        compacting.cancel()
        (stopped,) = await asyncio.gather(compacting, return_exceptions=True)

    # A pass that failed during the run. A cancelled pass is not an
    # `Exception`.
    if isinstance(stopped, Exception):
        raise stopped

    failed = failed_extractions(cast(object, runs))
    if failed:
        await _compact()

    return failed


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

    async def ingest(folders: list[str]) -> int:
        from cognee.tasks.ingestion.connectors import obsidian_source

        await _add(obsidian_source(vault_path=root, resource_name=RESOURCE, folders=folders))

        # Each PDF is read in a child process. The reads run in a daemon
        # thread first, so the store loop keeps answering searches, and a
        # stop does not wait for the PDFs that are left.
        cut = texts.cut
        await in_daemon_thread(lambda: texts.warm(root, folders))
        if texts.cut > cut:
            print(f"memory: {texts.cut - cut} PDF(s) cut at the character cap", file=sys.stderr)

        await _add(pdf_source(root, folders, texts))

        return await _cognify()

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
        #: The failed syncs in a row, and the time before which the loop
        #: does not sync again.
        self._failures = 0
        self._retry_at = 0.0
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
        # A write in the wait keeps its flag, so it syncs after the wait.
        if self._failures and self._clock() < self._retry_at:
            return Outcome.BACKING_OFF

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
            failed = await self._ingest(folders)
        except Exception as error:
            return await self._failed(error, self._clock() - started)

        # A partial sync keeps the stamp too: its failed items sync again at
        # the next change of the vault, not at each check.
        self._debounce.done(stamp)
        first = not self.ready.is_set()
        self.ready.set()
        ready = "; search is ready" if first else ""
        after = f" after {self._failures} failed syncs" if self._failures else ""
        partial = (
            f"; {failed} note(s) or PDF(s) failed graph extraction and sync again "
            "at the next change"
            if failed
            else ""
        )
        self._failures = 0
        _say(f"sync done in {self._clock() - started:.0f} s{after}{partial}{ready}")

        return Outcome.PARTIAL if failed else Outcome.SYNCED

    async def _failed(self, error: Exception, seconds: float) -> Outcome | Dead:
        """Count a failed sync, check the graph, and print one line.

        Only the type of the error is printed: a message can hold a URL. A
        dead graph stops the loop, and the command prints its own line. Any
        other failed sync does not: the loop tries again after the wait."""
        self._failures += 1
        wait = backoff_s(self._failures, self._poll_s)
        self._retry_at = self._clock() + wait
        line = (
            f"sync failed after {seconds:.0f} s: {type(error).__name__}. "
            f"Failed syncs in a row: {self._failures}."
        )
        try:
            state = await self._check()
        except BaseException:
            _say(line)
            raise

        if isinstance(state, Dead):
            _say(line)
            return state

        _say(f"{line} The next sync waits at least {wait:.0f} s.")

        return Outcome.FAILED

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
