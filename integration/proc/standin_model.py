"""A scripted model: an OpenAI chat completions server over loopback HTTP.

    standin_model.py <state directory> <port>

No test may call a real model. The agent module calls this program as it
calls LiteLLM, with `openai-completions` and the bearer of the family. The
program answers two routes, each with and without the `/v1` prefix, as
LiteLLM does:

    GET  /v1/models             one model: the text of `model`, or the default
    POST /v1/chat/completions   the next turn of the script, as an SSE stream

The agent module gives its client a base URL with no path, because a plane
URL holds none, and the client appends `/chat/completions`.

The test writes the script and the key. Every fact is a file under the
state directory:

    key           the bearers this program takes, one on each line
    turns.json    the script: a list of turns, read again at each request
    calls.jsonl   one line for each request, in arrival order: path and body
    tune/         what a test writes to change one answer

A turn is one of two objects:

    {"text": "<text>"}                                   ends with `stop`
    {"tool_call": {"name": "<tool>", "arguments": {}}}   ends with `tool_calls`

Streamed completion N takes turn N, from 0. A test can add turns to the
file while the program runs. A completion past the last turn gets 500, so
a turn that the script did not plan fails the test.

The stream sends the text one word in each chunk, then the chunk with the
finish reason, then `[DONE]`. A tool call is one chunk.

    tune/delay_ms   the milliseconds between two chunks. A slow turn lets a
                    scenario act while the turn runs.

Four things here take the strict reading:

1. A route that this program does not know gets 404.
2. A request with no bearer, or a bearer that `key` does not hold, gets 401.
3. A body that is not a JSON object with `"stream": true` gets 400. The
   agent module always streams.
4. A request with a body and no `Content-Length` header gets 411.

The line of a request is whole in `calls.jsonl` before its answer starts. A
request that 401 refuses leaves no line. A `GET /` leaves no line: the
harness sends one to learn that this program listens.
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
MODELS_PATH: Final = "/v1/models"
COMPLETIONS_PATH: Final = "/v1/chat/completions"

#: The prefix that LiteLLM serves each route with, and without.
_VERSION_PREFIX: Final = "/v1"

KEY_FILE: Final = "key"
MODEL_FILE: Final = "model"
TURNS_FILE: Final = "turns.json"
CALLS_FILE: Final = "calls.jsonl"
TUNE_DIR: Final = "tune"
TUNE_DELAY_MS: Final = "delay_ms"

DEFAULT_MODEL: Final = "standin-model"

HTTP_OK: Final = 200
HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_NOT_FOUND: Final = 404
HTTP_LENGTH_REQUIRED: Final = 411
HTTP_TOO_LARGE: Final = 413
HTTP_FAILED: Final = 500

_MAX_BODY: Final = 16 * 1024 * 1024
_BEARER: Final = "Bearer "


class Script:
    """The turns, the call log and the count of turns taken, under one lock."""

    def __init__(self, state: Path) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._taken = 0

    def bearers(self) -> frozenset[str]:
        lines = (self._state / KEY_FILE).read_text(encoding="utf-8").split("\n")

        return frozenset(line.strip() for line in lines if line.strip())

    def model(self) -> str:
        try:
            return (self._state / MODEL_FILE).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return DEFAULT_MODEL

    def delay_s(self) -> float:
        try:
            return int((self._state / TUNE_DIR / TUNE_DELAY_MS).read_text(encoding="utf-8")) / 1000
        except FileNotFoundError:
            return 0.0

    def record(self, path: str, body: object) -> None:
        line = json.dumps({"path": path, "body": body}) + "\n"

        with self._lock, (self._state / CALLS_FILE).open("ab") as calls:
            calls.write(line.encode("utf-8"))

    def take(self) -> tuple[int, dict[str, Any] | None]:
        """The number of the next turn, and the turn. None past the last turn."""
        with self._lock:
            turns = json.loads((self._state / TURNS_FILE).read_text(encoding="utf-8"))
            number = self._taken
            self._taken += 1

        return number, cast("dict[str, Any]", turns[number]) if number < len(turns) else None


def chunks_of(turn: dict[str, Any], number: int, model: str) -> list[dict[str, Any]]:
    """The chunks of one turn, the last one with the finish reason."""

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        choice = {"index": 0, "delta": delta, "finish_reason": finish}

        return {
            "id": f"chatcmpl-standin-{number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [choice],
        }

    first = chunk({"role": "assistant", "content": ""})

    if "tool_call" in turn:
        call = turn["tool_call"]
        function = {"name": call["name"], "arguments": json.dumps(call["arguments"])}
        entry = {
            "index": 0,
            "id": f"call_standin_{number}",
            "type": "function",
            "function": function,
        }

        return [first, chunk({"tool_calls": [entry]}), chunk({}, "tool_calls")]

    words = str(turn["text"]).split(" ")
    parts = [word if at == 0 else f" {word}" for at, word in enumerate(words)]

    return [first, *(chunk({"content": part}) for part in parts), chunk({}, "stop")]


def _handler_for(script: Script) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if _route(self.path) != MODELS_PATH:
                self._json(HTTP_NOT_FOUND, {"error": {"message": "no such route"}})
                return

            if self._refused():
                return

            script.record(self.path, None)
            model = {"id": script.model(), "object": "model", "owned_by": "standin"}
            self._json(HTTP_OK, {"object": "list", "data": [model]})

        def do_POST(self) -> None:
            if _route(self.path) != COMPLETIONS_PATH:
                self._json(HTTP_NOT_FOUND, {"error": {"message": "no such route"}})
                return

            length = self.headers.get("Content-Length")

            if length is None or not length.isdecimal():
                self._json(HTTP_LENGTH_REQUIRED, {"error": {"message": "no Content-Length"}})
                return

            if int(length) > _MAX_BODY:
                self._json(HTTP_TOO_LARGE, {"error": {"message": "the body is too large"}})
                return

            body = _json_of(self.rfile.read(int(length)))

            if self._refused():
                return

            script.record(self.path, body)

            if not isinstance(body, dict) or cast("dict[str, Any]", body).get("stream") is not True:
                self._json(HTTP_BAD_REQUEST, {"error": {"message": "only a stream is served"}})
                return

            number, turn = script.take()

            if turn is None:
                self._json(HTTP_FAILED, {"error": {"message": f"the script has no turn {number}"}})
                return

            self._stream(chunks_of(turn, number, script.model()))

        def log_message(self, format: str, *args: object) -> None:
            """Write no access line. `calls.jsonl` is the record."""

        def _refused(self) -> bool:
            sent = self.headers.get("Authorization", "")

            if sent.startswith(_BEARER) and sent.removeprefix(_BEARER) in script.bearers():
                return False

            self._json(HTTP_UNAUTHORIZED, {"error": {"message": "wrong bearer"}})

            return True

        def _stream(self, chunks: list[dict[str, Any]]) -> None:
            self.send_response(HTTP_OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            for at, chunk in enumerate(chunks):
                if at:
                    time.sleep(script.delay_s())

                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()

            self.wfile.write(b"data: [DONE]\n\n")

        def _json(self, status: int, value: object) -> None:
            raw = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def _route(path: str) -> str:
    """The route of one path: the path itself, or the path with `/v1` before it."""
    return path if path.startswith(_VERSION_PREFIX + "/") else _VERSION_PREFIX + path


def _json_of(raw: bytes) -> object:
    """The JSON value of one body, or None for a body that is not JSON."""
    try:
        value: object = json.loads(raw)
    except ValueError:
        return None

    return value


class Server(ThreadingHTTPServer):
    """The listener. One thread serves one connection, so a slow turn holds no other."""

    request_queue_size = 128

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Write no traceback for a caller that is gone, for example after a cancel."""
        if isinstance(sys.exception(), OSError):
            return

        super().handle_error(request, client_address)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: standin_model.py <state directory> <port>\n")

        return 64

    server = Server((LOOPBACK, int(argv[1])), _handler_for(Script(Path(argv[0]))))
    server.daemon_threads = True
    server.serve_forever()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
