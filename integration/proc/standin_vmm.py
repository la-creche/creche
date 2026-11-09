"""A stand-in for the vmm host API, over HTTP/1.1 on a Unix socket.

    standin_vmm.py <state directory> <socket path>

No test of this suite may start a VM. `caregiver` still sends the one call
of contract 05 §4.4: the drop of every session of a deleted family
(decisions CH20, CH21-10). This program answers it:

    POST /drop   {"family": {"family": <name>}}   Dropped

It holds no row, so the drop of one session answers `not_found`.

The request and the reply are the wire types of `creche_wire` (decision
CH25), so a body of another shape is the error `not_valid`, as the vmm
answers it. A request whose header `creche-host-api-protocol` does not name
`HOST_API_PROTOCOL` gets the mismatch reply first (decision CH33 rule 1).

Every fact is a file under the state directory:

    calls.jsonl    one line for each request: its route and its body
    tune/          what a test writes to change one answer

A test tunes one answer with a file under `tune/`:

    tune/fail-drop    the drop answers 500 `internal`

A `GET /` gets 200 and leaves no line: the harness sends one to learn that
this program listens.
"""

from __future__ import annotations

import json
import os
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Final

from creche_wire import DropTarget, FamilyDrop, ProtocolMismatch, read
from creche_wire.routes import DROP, HOST_API_BODY_CAP, HOST_API_HEADER, HOST_API_PROTOCOL
from creche_wire.routes import MISMATCH_STATUS as MISMATCH

CALLS_FILE: Final = "calls.jsonl"
TUNE_DIR: Final = "tune"

#: What the harness asks to learn that this program listens.
PROBE_PATH: Final = "/"

_OK: Final = 200
_INTERNAL: Final = 500
_NO_ROUTE: Final = 404
_BAD_REQUEST: Final = 400

_LOCK = threading.Lock()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, state: Path) -> None:
        super().__init__(socket_path, _Handler)
        self.state = state


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def do_GET(self) -> None:
        if self.path == PROBE_PATH:
            self._answer(_OK, {})
            return

        self._answer(_NO_ROUTE, {"code": "no_route"})

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(size)

        if self.headers.get(HOST_API_HEADER) != str(HOST_API_PROTOCOL):
            mismatch = ProtocolMismatch(code="protocol_mismatch", protocol=HOST_API_PROTOCOL)
            self._answer(MISMATCH, json.loads(mismatch.model_dump_json()))
            return

        if self.path != DROP.path:
            self._answer(_NO_ROUTE, {"code": "no_route"})
            return

        try:
            target = read(DropTarget, data, cap=HOST_API_BODY_CAP)
        except ValueError:
            self._answer(_BAD_REQUEST, {"code": "not_valid"})
            return

        self._record(DROP.name, json.loads(target.model_dump_json()))

        if (self.server.state / TUNE_DIR / f"fail-{DROP.name}").exists():
            self._answer(_INTERNAL, {"code": "internal"})
            return

        # A drop of a family drops each of its rows, and a family with no
        # row is done too (`FamilyDrop` of `creche_wire`). This program
        # holds no row, so a drop of one session finds none.
        if isinstance(target.root, FamilyDrop):
            self._answer(_OK, {})
            return

        self._answer(_NO_ROUTE, {"code": "not_found"})

    def _record(self, route: str, body: dict[str, Any]) -> None:
        line = json.dumps({"route": route, "body": body}, sort_keys=True)

        with _LOCK, (self.server.state / CALLS_FILE).open("a", encoding="utf-8") as calls:
            calls.write(line + "\n")

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """No log line: a Unix socket has no client address to print."""


def main(argv: list[str]) -> int:
    state, socket_path = Path(argv[1]), argv[2]
    (state / TUNE_DIR).mkdir(parents=True, exist_ok=True)

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    with _Server(socket_path, state) as server:
        server.serve_forever()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
