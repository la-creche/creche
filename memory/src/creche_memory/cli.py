"""The command `creche-memory`: the memory service of the vault.

`creche-memory` starts the service: the store loop with its syncer, and the
socket server. `creche-memory --check` reads the config and checks the host,
opens no socket, and exits. A deploy runs it before it swaps the unit.

Exit 0 after a stop by SIGTERM or SIGINT. The stop closes the socket, ends
every task of the store loop, then exits at once (`stop_store`). Exit 78
(EX_CONFIG) for a config or a host that stops the start, so systemd does
not restart the unit in a loop (systemd/creche-caregiver.service, the
restart policy). Exit 78 too for a graph database that does not open, at
the start or after a failed sync, and for one that another engine built
(`graph.py`): a restart cannot repair either one.

Exit 75 (EX_TEMPFAIL) for an error that ends the start check or the sync
loop and is not a verdict on the database, such as a worker that did not
start, a lock that another opener holds, or a full disk. systemd restarts
the unit, and the check at the next start decides."""

import os
import signal
import sys
import threading
from concurrent.futures import Future, wait
from types import FrameType
from typing import NoReturn

from .config import Config, ConfigError, check, from_env
from .graph import (
    Dead,
    NotAVerdict,
    check_engine,
    cognee_graph_file,
    cognee_graph_state,
    service_engine,
)
from .logs import plain_console
from .search import STOP_TIMEOUT_S, Searcher, StoreLoop
from .server import MemoryServer, Service
from .store import configure
from .sync import Syncer, cognee_ingest
from .vault import Vault
from .wire import RefusedReply, SearchReply, SearchRequest
from .writes import Writer

EX_OK = 0
EX_TEMPFAIL = 75
EX_CONFIG = 78
CHECK_FLAG = "--check"
STORE_MODE = 0o700


def _fail(reason: str) -> int:
    print(f"creche-memory: {reason}", file=sys.stderr)
    return EX_CONFIG


def _dead(state: Dead) -> int:
    print(f"creche-memory: {state.report()}", file=sys.stderr, flush=True)
    return EX_CONFIG


def _broke(what: str, error: BaseException) -> int:
    # Only the type: a message can hold the URL of a service. The text of a
    # `NotAVerdict` is ladybug's, and it names a file, so the line carries it.
    cause = type(error).__name__
    if isinstance(error, NotAVerdict):
        cause = f"{cause}: {str(error).rstrip('.')}"
    print(
        f"creche-memory: {what} failed: {cause}. "
        "systemd restarts the unit. The next start checks the graph database again.",
        file=sys.stderr,
        flush=True,
    )
    return EX_TEMPFAIL


def ended(syncing: Future[Dead | None], timeout_s: float) -> int:
    """The exit status from the end of the sync loop, after the stop. A loop
    that the stop cancelled, or that has not ended within `timeout_s`, was in
    a sync at the stop by signal: exit 0."""
    done, _ = wait([syncing], timeout_s)
    if not done or syncing.cancelled():
        return EX_OK

    error = syncing.exception()
    if error is not None:
        return _broke("the sync loop", error)

    dead = syncing.result()

    return EX_OK if dead is None else _dead(dead)


def stop_signals(stop: threading.Event) -> None:
    """Set `stop` at SIGTERM or SIGINT. Install it before the first call into
    Cognee, so a signal never ends the process inside a write."""

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)


def stop_store(loop: StoreLoop, syncing: Future[Dead | None] | None) -> NoReturn:
    """End the store after a stop, and exit with its status.

    1. Read the end of the sync loop. A loop that ended before the stop
       stopped the service, and its end gives the status. A loop that still
       runs is in a stop by signal: exit 0.
    2. Cancel every task of the loop, and wait at most `STOP_TIMEOUT_S` for
       them.
    3. End the process at once (`os._exit`). The exit ends Cognee's workers,
       by `PR_SET_PDEATHSIG` on Linux and by Cognee's parent watchdog on
       macOS. The next start replays the WAL of the graph database.

    The stop writes nothing to the graph database. Ladybug 0.22.3 opens a
    file whose worker died in a checkpoint, with every committed row
    (`memory/AGENTS.md`, the stop). A normal exit would wait: Python joins
    Cognee's query threads at the exit. And on Linux the end of the loop
    thread sends SIGTERM to the graph worker, and Cognee answers a dead
    worker with a new one."""
    status = EX_OK
    if syncing is not None and syncing.done():
        status = ended(syncing, 0.0)

    if not loop.cancel():
        _say(
            f"a task of the store loop still ran {STOP_TIMEOUT_S:.0f} s after the stop. "
            "The service exits without it."
        )

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)


def _say(line: str) -> None:
    print(f"creche-memory: {line}", file=sys.stderr, flush=True)


def _serve(config: Config) -> int:
    from .cognee_search import cognee_backend, cognee_paths

    places = config.places
    places.store_root.mkdir(mode=STORE_MODE, exist_ok=True)
    configure(config.store)
    # The sync loop sets `stop` when it ends, so a dead graph or an error
    # ends the wait below and stops the service.
    stop = threading.Event()
    stop_signals(stop)
    # The first import of Cognee. Its own console handler draws a traceback
    # box of many lines for each failed document, so swap it before the
    # first sync (`logs.py`).
    try:
        plain_console()
    except RuntimeError as error:
        return _fail(str(error))

    # Before the first open of the graph database, which builds a new file.
    try:
        other = check_engine(cognee_graph_file(), service_engine())
    except OSError as error:
        return _broke("the engine check", error)

    if other is not None:
        return _fail(other.report())

    loop = StoreLoop()
    loop.start()
    # The first open of the graph database. A dead one stops the start
    # before the socket opens. A signal in the check waits for its end.
    try:
        state = loop.spawn(cognee_graph_state()).result()
    except Exception as error:
        loop.stop()
        return _broke("the graph check", error)

    if isinstance(state, Dead):
        loop.stop()
        return _dead(state)

    if stop.is_set():
        return stop_store(loop, None)

    syncer = Syncer(places.vault_root, cognee_ingest(places.vault_root), cognee_graph_state)
    searcher = Searcher(syncer.ready, cognee_backend, cognee_paths)

    def search(request: SearchRequest) -> SearchReply | RefusedReply:
        return loop.search(searcher, request)

    service = Service(
        vault=Vault(places.vault_root),
        writer=Writer(places.vault_root),
        search=search,
        wrote=syncer.request,
    )

    syncing = loop.spawn(syncer.run(stop))
    server = MemoryServer(places.socket_path, service, places.peer_uid)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    print(f"creche-memory: serving {places.vault_root} on {places.socket_path}")

    stop.wait()
    server.shutdown()
    server.server_close()
    syncer.wake()

    return stop_store(loop, syncing)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], [CHECK_FLAG]):
        return _fail(f"usage: creche-memory [{CHECK_FLAG}]")

    try:
        config = from_env()
    except ConfigError as error:
        return _fail(str(error))

    problems = check(config.places)
    if problems:
        return _fail("; ".join(problems))

    if args == [CHECK_FLAG]:
        print("creche-memory: config ok")
        return EX_OK

    return _serve(config)


if __name__ == "__main__":
    sys.exit(main())
