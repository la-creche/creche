"""A stand-in for the embedding service (TEI), over loopback HTTP.

    standin_tei.py <state directory> <port>

No test may load an embedding model. `index-scope` still makes these two
requests, and this program answers each one:

    GET  /info    the model that the service has       {"model_id": <model>}
    POST /embed   {"inputs": [<text>, ...]}            one vector for each text

A vector depends on its text alone, so a test can compute the vector that a
store must hold. Value `i` of a vector is `((s * (i + 1)) % 1000) / 1000 - 0.5`,
where `s` is the sum of the code points of the text. `vector_of` is that rule.

Every fact is a file under the state directory:

    calls.jsonl    one line for each request, in arrival order: method, path, body
    tune/          what a test writes to change one answer

The line of a request is whole in `calls.jsonl` before its answer starts. A
`GET /` leaves no line: the harness sends one to learn that this program
listens.

Four things here take the strict reading, because no test of this suite can
ask the real service:

1. A route that this program does not know gets 404.
2. A body of `/embed` that is not an object with a list of texts in `inputs`
   gets 422. An empty list is such a body.
3. A call with more texts than `MAX_BATCH` gets 413.
4. A request with a body and no `Content-Length` header gets 411.

A test tunes one answer with a file under `tune/`:

    tune/model         the model id that `/info` gives
    tune/no-model-id   the answer of `/info` holds no `model_id`
    tune/dims          the count of values in each vector
    tune/fail-info     `/info` answers 503
    tune/fail-embed    a text: a call whose inputs hold it answers 500
    tune/hold-embed    a text: a call whose inputs hold it waits until the file is gone

A call holds a text when the text is a part of one of its inputs. An empty
text is a part of each input.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, cast

LOOPBACK: Final = "127.0.0.1"

INFO_PATH: Final = "/info"
EMBED_PATH: Final = "/embed"

#: What the harness asks to learn that this program listens.
PROBE_PATH: Final = "/"

CALLS_FILE: Final = "calls.jsonl"
TUNE_DIR: Final = "tune"

TUNE_MODEL: Final = "model"
TUNE_NO_MODEL_ID: Final = "no-model-id"
TUNE_DIMS: Final = "dims"
TUNE_FAIL_INFO: Final = "fail-info"
TUNE_FAIL_EMBED: Final = "fail-embed"
TUNE_HOLD_EMBED: Final = "hold-embed"

#: The model id of `/info` when no tuning gives another. An obvious fixture.
DEFAULT_MODEL: Final = "standin/embed-768"

#: The count of values in a vector when no tuning gives another.
DEFAULT_DIMS: Final = 768

#: The most texts that one call may hold. The real service refuses a larger
#: call, and its default limit is this number.
MAX_BATCH: Final = 32

#: The terms of the vector rule: the modulus, and half of the range.
_MODULUS: Final = 1000
_CENTRE: Final = 0.5

HTTP_OK: Final = 200
HTTP_NOT_FOUND: Final = 404
HTTP_LENGTH_REQUIRED: Final = 411
HTTP_TOO_LARGE: Final = 413
HTTP_UNPROCESSABLE: Final = 422
HTTP_FAILED: Final = 500
HTTP_UNAVAILABLE: Final = 503

_MAX_BODY: Final = 16 * 1024 * 1024
_HOLD_POLL_S: Final = 0.02

Answer = tuple[int, Any]


def vector_of(text: str, dims: int = DEFAULT_DIMS) -> list[float]:
    """The vector of one text. It depends on the text and on nothing else."""
    total = sum(map(ord, text))

    return [((total * (index + 1)) % _MODULUS) / _MODULUS - _CENTRE for index in range(dims)]


class Embedder:
    """The answers and the call log. Each line of the log is one write under one lock."""

    def __init__(self, state: Path) -> None:
        self._state = state
        self._lock = threading.Lock()

    def answer(self, method: str, path: str, body: object) -> Answer:
        """Record one request and answer it."""
        if (method, path) == ("GET", PROBE_PATH):
            return HTTP_NOT_FOUND, {"error": "no such route"}

        self._record(method, path, body)

        if (method, path) == ("GET", INFO_PATH):
            return self._info()

        if (method, path) == ("POST", EMBED_PATH):
            return self._embed(body)

        return HTTP_NOT_FOUND, {"error": "no such route"}

    def refuse(self, method: str, path: str, status: int, reason: str) -> Answer:
        """Record a request whose body this program did not read, and refuse it."""
        self._record(method, path, None)

        return status, {"error": reason}

    def _info(self) -> Answer:
        if self._tuned(TUNE_FAIL_INFO) is not None:
            return HTTP_UNAVAILABLE, {"error": "info failed"}

        if self._tuned(TUNE_NO_MODEL_ID) is not None:
            return HTTP_OK, {}

        return HTTP_OK, {"model_id": self._tuned(TUNE_MODEL) or DEFAULT_MODEL}

    def _embed(self, body: object) -> Answer:
        texts = _texts_of(body)

        if texts is None:
            return HTTP_UNPROCESSABLE, {"error": "inputs is not a list of one text or more"}

        if len(texts) > MAX_BATCH:
            return HTTP_TOO_LARGE, {"error": f"{len(texts)} texts, and the limit is {MAX_BATCH}"}

        if self._holds(TUNE_FAIL_EMBED, texts):
            return HTTP_FAILED, {"error": "embed failed"}

        while self._holds(TUNE_HOLD_EMBED, texts):
            time.sleep(_HOLD_POLL_S)

        dims = int(self._tuned(TUNE_DIMS) or DEFAULT_DIMS)

        return HTTP_OK, [vector_of(text, dims) for text in texts]

    def _holds(self, what: str, texts: list[str]) -> bool:
        """Whether a tuning is there and one of the texts holds its text."""
        part = self._tuned(what)

        return part is not None and any(part in text for text in texts)

    def _tuned(self, what: str) -> str | None:
        """The text of one tuning, or None when no test wrote it."""
        try:
            return (self._state / TUNE_DIR / what).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _record(self, method: str, path: str, body: object) -> None:
        line = json.dumps({"method": method, "path": path, "body": body}) + "\n"

        with self._lock, (self._state / CALLS_FILE).open("ab") as calls:
            calls.write(line.encode("utf-8"))


def _texts_of(body: object) -> list[str] | None:
    """The texts of one `/embed` body, or None for a body of another shape."""
    if not isinstance(body, dict):
        return None

    inputs = cast("dict[str, object]", body).get("inputs")

    if not isinstance(inputs, list) or not inputs:
        return None

    texts = cast("list[object]", inputs)

    if not all(isinstance(text, str) for text in texts):
        return None

    return cast("list[str]", texts)


def _handler_for(embedder: Embedder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        #: The real service keeps a connection open between two requests.
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._send(embedder.answer("GET", self.path, None))

        def do_POST(self) -> None:
            length = self.headers.get("Content-Length")

            if length is None or not length.isdecimal():
                self._refuse(HTTP_LENGTH_REQUIRED, "this stand-in needs a Content-Length header")
                return

            if int(length) > _MAX_BODY:
                self._refuse(HTTP_TOO_LARGE, f"the body is larger than {_MAX_BODY} bytes")
                return

            self._send(embedder.answer("POST", self.path, _json_of(self.rfile.read(int(length)))))

        def log_message(self, format: str, *args: object) -> None:
            """Write no access line. `calls.jsonl` is the record."""

        def _refuse(self, status: int, reason: str) -> None:
            """Answer a request whose body stays in the socket, and close the connection."""
            self.close_connection = True
            self._send(embedder.refuse("POST", self.path, status, reason))

        def _send(self, answer: Answer) -> None:
            status, value = answer
            raw = json.dumps(value).encode("utf-8")

            # A caller that a test killed has no socket for the answer of a
            # held call. That is no fault of this program.
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                self.close_connection = True

    return Handler


def _json_of(raw: bytes) -> object:
    """The JSON value of one body, or None for a body that is not JSON."""
    try:
        value: object = json.loads(raw)
    except ValueError:
        return None

    return value


class Server(ThreadingHTTPServer):
    """The listener. One thread serves one connection, so a held call holds no other."""

    request_queue_size = 128

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Write no traceback for a caller that closed its connection."""
        if isinstance(sys.exception(), OSError):
            return

        super().handle_error(request, client_address)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: standin_tei.py <state directory> <port>\n")

        return 64

    server = Server((LOOPBACK, int(argv[1])), _handler_for(Embedder(Path(argv[0]))))
    server.daemon_threads = True
    server.serve_forever()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
