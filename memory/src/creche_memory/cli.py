"""The command `creche-memory`: the memory service of the vault.

`creche-memory` starts the service: the store loop with its syncer, and the
socket server. `creche-memory --check` reads the config and checks the host,
opens no socket, and exits. A deploy runs it before it swaps the unit.

Exit 0 after a stop by SIGTERM or SIGINT. Exit 78 (EX_CONFIG) for a config
or a host that stops the start, so systemd does not restart the unit in a
loop (systemd/creche-caregiver.service, the restart policy)."""

import contextlib
import signal
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from types import FrameType

from .config import Config, ConfigError, check, from_env
from .search import STOP_TIMEOUT_S, Searcher, StoreLoop
from .server import MemoryServer, Service
from .store import configure
from .sync import Syncer, cognee_ingest
from .vault import Vault
from .wire import RefusedReply, SearchReply, SearchRequest
from .writes import Writer

EX_OK = 0
EX_CONFIG = 78
CHECK_FLAG = "--check"
STORE_MODE = 0o700


def _fail(reason: str) -> int:
    print(f"creche-memory: {reason}", file=sys.stderr)
    return EX_CONFIG


def _serve(config: Config) -> int:
    from .cognee_search import cognee_backend, cognee_paths

    config.store.store_root.mkdir(mode=STORE_MODE, exist_ok=True)
    configure(config.store)

    loop = StoreLoop()
    syncer = Syncer(config.vault_root, cognee_ingest(config.vault_root))
    searcher = Searcher(syncer.ready, cognee_backend, cognee_paths)

    def search(request: SearchRequest) -> SearchReply | RefusedReply:
        return loop.search(searcher, request)

    service = Service(
        vault=Vault(config.vault_root),
        writer=Writer(config.vault_root),
        search=search,
        wrote=syncer.request,
    )
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    loop.start()
    syncing = loop.spawn(syncer.run(stop))
    server = MemoryServer(config.socket_path, service, config.peer_uid)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    print(f"creche-memory: serving {config.vault_root} on {config.socket_path}")

    stop.wait()
    server.shutdown()
    server.server_close()
    syncer.wake()
    with contextlib.suppress(FutureTimeout):
        syncing.result(STOP_TIMEOUT_S)

    loop.stop()

    return EX_OK


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], [CHECK_FLAG]):
        return _fail(f"usage: creche-memory [{CHECK_FLAG}]")

    try:
        config = from_env()
    except ConfigError as error:
        return _fail(str(error))

    problems = check(config)
    if problems:
        return _fail("; ".join(problems))

    if args == [CHECK_FLAG]:
        print("creche-memory: config ok")
        return EX_OK

    return _serve(config)


if __name__ == "__main__":
    sys.exit(main())
