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
the vault root."""

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
    note in it. An edit, a new note and a removed note each change it."""
    digest = hashlib.sha256("\n".join(folders).encode())
    for folder in folders:
        for directory, subdirs, files in os.walk(root / folder):
            subdirs[:] = sorted(name for name in subdirs if not name.startswith("."))
            for name in sorted(files):
                if name.startswith(".") or not name.endswith(NOTE_SUFFIX):
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
            return True

        return stamp != self.synced and now - self.seen_at >= self.quiet_s

    def done(self, stamp: str) -> None:
        self.synced = stamp


def cognee_ingest(root: Path) -> Ingest:
    """The sync through Cognee's Obsidian connector. An edited note replaces
    its old document, and a removed note is forgotten."""

    async def ingest(folders: list[str]) -> None:
        import cognee
        from cognee.tasks.ingestion.connectors import obsidian_source

        source = obsidian_source(vault_path=root, resource_name=RESOURCE, folders=folders)
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

    return ingest


class Syncer:
    """Runs the checks and the syncs of one vault."""

    def __init__(
        self,
        root: Path,
        ingest: Ingest,
        *,
        poll_s: float = POLL_S,
        quiet_s: float = QUIET_S,
        clock: Clock = time.monotonic,
    ) -> None:
        self._root = root
        self._ingest = ingest
        self._poll_s = poll_s
        self._clock = clock
        self._debounce = Debounce(quiet_s)
        self._wrote = threading.Event()
        #: Set after the first sync that ends well. Search waits for it.
        self.ready = threading.Event()

    def request(self) -> None:
        """Ask for a sync at once, after a confirmed write. Any thread."""
        self._wrote.set()

    async def step(self) -> Outcome:
        folders = ingest_folders(self._root)
        if not folders:
            return Outcome.NOTHING_TO_READ

        trigger = Trigger.WRITE if self._wrote.is_set() else Trigger.POLL
        stamp = vault_stamp(self._root, folders)
        if not self._debounce.due(stamp, self._clock(), trigger):
            return Outcome.UNCHANGED if stamp == self._debounce.synced else Outcome.WAITING

        self._wrote.clear()
        try:
            await self._ingest(folders)
        except Exception as error:
            # A failed sync must not stop the loop. The next check tries
            # again. Only the type is printed: a message can hold a URL.
            print(f"memory: sync failed: {type(error).__name__}", file=sys.stderr)
            return Outcome.FAILED

        self._debounce.done(stamp)
        self.ready.set()

        return Outcome.SYNCED

    async def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            await self.step()
            await asyncio.to_thread(self._wrote.wait, self._poll_s)
