"""A stand-in for the vmm host API, over HTTP/1.1 on a Unix socket.

    standin_vmm.py <state directory> <socket path>

No test of this suite may start a VM. `attendance` and `caregiver` still
send the calls of the host session list (decisions CH20, CH21-4 to
CH21-10). This program answers each one:

    POST /create   CreateSession                       SessionRow
    POST /list     ListSessions                        SessionPage, one page, VM none
    POST /drop     {"family": {"family": <name>}}      Dropped
                   {"session": {"family", "id"}}       Dropped, or `not_found`
    POST /acp      OpenAcp, with an Upgrade            `sandbox_start_failed`

The request and the reply are the wire types of `creche_wire` (decision
CH25), so a body of another shape is the error `not_valid`, as the vmm
answers it. A request whose header `creche-host-api-protocol` does not name
`HOST_API_PROTOCOL` gets the mismatch reply first (decision CH33 rule 1).

The `acp` route boots no VM here. The facade under the vmm reads the fixed
guest paths of a VM (decisions CH80, CH81), so no process of the suite can
serve it. A row of the family gets `sandbox_start_failed`, as a VM that
does not boot gets it. A session with no row of the family gets
`not_found` (decision CH21-9).

The list holds each row of the family, newest created first, on one page.
A `cwd` keeps the rows of that `cwd`. Each row lists its VM as `none`: no
VM ever runs here (decision CH79 rule 4).

Every fact is a file under the state directory:

    calls.jsonl    one line for each request: its route and its body
    rows.json      each row of the host session list, in create order
    tune/          what a test writes to change one answer

A test tunes one answer with a file under `tune/`:

    tune/fail-<route>    that route answers 500 `internal`

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

from creche_wire import (
    CreateSession,
    DropTarget,
    FamilyDrop,
    ListRow,
    ListSessions,
    OpenAcp,
    ProtocolMismatch,
    SessionPage,
    SessionRow,
    VmState,
    read,
)
from creche_wire.routes import ACP, CREATE, DROP, HOST_API_BODY_CAP, HOST_API_HEADER, LIST
from creche_wire.routes import HOST_API_PROTOCOL as PROTOCOL
from creche_wire.routes import MISMATCH_STATUS as MISMATCH
from pydantic import BaseModel

CALLS_FILE: Final = "calls.jsonl"
ROWS_FILE: Final = "rows.json"
TUNE_DIR: Final = "tune"

#: What the harness asks to learn that this program listens.
PROBE_PATH: Final = "/"

_OK: Final = 200
_BAD_REQUEST: Final = 400
_NOT_FOUND: Final = 404
_INTERNAL: Final = 500
_UNAVAILABLE: Final = 503

#: The request type of each route.
_REQUESTS: Final[dict[str, type[BaseModel]]] = {
    CREATE.path: CreateSession,
    LIST.path: ListSessions,
    DROP.path: DropTarget,
    ACP.path: OpenAcp,
}

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

        self._answer(_NOT_FOUND, {"code": "no_route"})

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(size)

        if self.headers.get(HOST_API_HEADER) != str(PROTOCOL):
            mismatch = ProtocolMismatch(code="protocol_mismatch", protocol=PROTOCOL)
            self._answer(MISMATCH, json.loads(mismatch.model_dump_json()))
            return

        model = _REQUESTS.get(self.path)

        if model is None:
            self._answer(_NOT_FOUND, {"code": "no_route"})
            return

        try:
            request = read(model, data, cap=HOST_API_BODY_CAP)
        except ValueError:
            self._answer(_BAD_REQUEST, {"code": "not_valid"})
            return

        route = self.path.removeprefix("/")
        self._record(route, json.loads(request.model_dump_json()))

        if (self.server.state / TUNE_DIR / f"fail-{route}").exists():
            self._answer(_INTERNAL, {"code": "internal"})
            return

        with _LOCK:
            status, body = self._route(request)

        self._answer(status, body)

    def _route(self, request: BaseModel) -> tuple[int, dict[str, Any]]:
        rows = _rows(self.server.state)

        if isinstance(request, CreateSession):
            row = SessionRow(
                created_at=request.created_at,
                cwd=request.cwd,
                family=request.family,
                id=request.id,
                origin=request.origin,
                title=None,
                updated_at=request.created_at,
            )
            _write_rows(self.server.state, [*rows, row])
            return _OK, _json(row)

        if isinstance(request, ListSessions):
            kept = [
                ListRow(**dict(row), vm=VmState.NONE)
                for row in reversed(rows)
                if row.family == request.family and request.cwd in (None, row.cwd)
            ]
            return _OK, _json(SessionPage(next=None, rows=kept))

        if isinstance(request, DropTarget):
            return self._drop(request, rows)

        assert isinstance(request, OpenAcp)

        if not any(row.family == request.family and row.id == request.id for row in rows):
            return _NOT_FOUND, {"code": "not_found"}

        return _UNAVAILABLE, {"code": "sandbox_start_failed"}

    def _drop(self, target: DropTarget, rows: list[SessionRow]) -> tuple[int, dict[str, Any]]:
        """A drop of a family drops each of its rows, and a family with no row
        is done too (`FamilyDrop` of `creche_wire`). A drop of one session
        with no row of its family is `not_found`."""
        if isinstance(target.root, FamilyDrop):
            family = target.root.family.family
            _write_rows(self.server.state, [row for row in rows if row.family != family])
            return _OK, {}

        key = target.root.session
        kept = [row for row in rows if not (row.family == key.family and row.id == key.id)]

        if len(kept) == len(rows):
            return _NOT_FOUND, {"code": "not_found"}

        _write_rows(self.server.state, kept)
        return _OK, {}

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


def _rows(state: Path) -> list[SessionRow]:
    try:
        text = (state / ROWS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    return [SessionRow.model_validate(one) for one in json.loads(text)]


def _write_rows(state: Path, rows: list[SessionRow]) -> None:
    path = state / ROWS_FILE
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps([_json(row) for row in rows]), encoding="utf-8")
    temp.replace(path)


def _json(model: BaseModel) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(model.model_dump_json())

    return body


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
