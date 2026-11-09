"""The command `creche-memory`: the memory service of the vault.

`creche-memory` starts the service: the store loop with its syncer, and the
socket server. `creche-memory --check` reads the config and checks the host,
opens no socket, and exits. A deploy runs it before it swaps the unit.

Exit 0 after a stop by SIGTERM or SIGINT. Exit 78 (EX_CONFIG) for a config
or a host that stops the start, so systemd does not restart the unit in a
loop (systemd/creche-caregiver.service, the restart policy). Exit 78 too for
a graph database that does not open, at the start or after a failed sync
(`graph.py`): a restart cannot repair it.

Exit 75 (EX_TEMPFAIL) for an error that ends the start check or the sync
loop and is not a verdict on the database, such as a worker that did not
start. systemd restarts the unit, and the check at the next start decides."""

import signal
import sys
import threading
from concurrent.futures import Future, wait
from types import FrameType

from .config import Config, ConfigError, check, from_env
from .graph import Dead, cognee_graph_state
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
    # Only the type: a message can hold a URL.
    print(
        f"creche-memory: {what} failed: {type(error).__name__}. "
        "systemd restarts the unit. The next start checks the graph database again.",
        file=sys.stderr,
        flush=True,
    )
    return EX_TEMPFAIL


def ended(syncing: Future[Dead | None], timeout_s: float) -> int:
    """The exit status from the end of the sync loop, after the stop. A loop
    that has not ended within `timeout_s` is still in a sync after a stop by
    signal: exit 0."""
    done, _ = wait([syncing], timeout_s)
    if not done:
        return EX_OK

    error = syncing.exception()
    if error is not None:
        return _broke("the sync loop", error)

    dead = syncing.result()

    return EX_OK if dead is None else _dead(dead)


def _serve(config: Config) -> int:
    from .cognee_search import cognee_backend, cognee_paths

    places = config.places
    places.store_root.mkdir(mode=STORE_MODE, exist_ok=True)
    configure(config.store)

    loop = StoreLoop()
    loop.start()
    # The first open of the graph database. A dead one stops the start
    # before the socket opens.
    try:
        state = loop.spawn(cognee_graph_state()).result()
    except Exception as error:
        loop.stop()
        return _broke("the graph check", error)

    if isinstance(state, Dead):
        loop.stop()
        return _dead(state)

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
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # The sync loop sets `stop` when it ends, so a dead graph or an error
    # ends the wait below and stops the service.
    syncing = loop.spawn(syncer.run(stop))
    server = MemoryServer(places.socket_path, service, places.peer_uid)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    print(f"creche-memory: serving {places.vault_root} on {places.socket_path}")

    stop.wait()
    server.shutdown()
    server.server_close()
    syncer.wake()
    status = ended(syncing, STOP_TIMEOUT_S)
    loop.stop()

    return status


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
