"""The MCP server over stdio that the two effect stand-ins share.

The Rust chaperone launches each effect plugin and is its MCP client
(chaperone `docs/design.md` §7.7). It writes one JSON-RPC message per line
and reads one message per line. This module answers the four requests that
the effect host sends: `initialize`, `ping`, `tools/list` and `tools/call`.
It takes each notification and answers none.

Each `tools/call` runs on a thread of its own. The approval stand-in holds a
call until a test answers it, and a held call must not hold another call.

The reader is strict. A line that is not one JSON object, or a line longer
than `MAX_LINE`, ends the program with exit code 2. A request with a method
that this module does not know gets the JSON-RPC error `-32601`.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any, Final, TextIO, cast

from proc_strict import strict_object

#: The longest line that the reader takes. The effect host caps a result at
#: 1 MiB, and no request of a test comes near.
MAX_LINE: Final = 1024 * 1024

EXIT_BAD_INPUT: Final = 2

_METHOD_NOT_FOUND: Final = -32601
_INVALID_PARAMS: Final = -32602

#: What one tool call gives back: the text of its one block, and the error flag.
Result = tuple[str, bool]

#: The handler of `tools/call`: the tool name, its arguments and its `_meta`.
Handler = Callable[[str, Mapping[str, Any], Mapping[str, Any]], Result]


def tool(name: str) -> dict[str, object]:
    """One entry of `tools/list`: a tool that takes any object."""
    return {
        "name": name,
        "description": f"the stand-in {name} tool",
        "inputSchema": {"type": "object"},
    }


def read_line(stream: TextIO) -> str | None:
    """One line with no newline, or None at the end of the input."""
    line = stream.readline(MAX_LINE + 1)

    if not line:
        return None

    if len(line) > MAX_LINE:
        raise ValueError(f"a line is longer than {MAX_LINE} characters")

    return line.rstrip("\n")


class Server:
    """One MCP server on stdin and stdout. One lock keeps each written line whole."""

    def __init__(self, name: str, tools: list[dict[str, object]], handler: Handler) -> None:
        self._name = name
        self._tools = tools
        self._names = {str(entry["name"]) for entry in tools}
        self._handler = handler
        self._lock = threading.Lock()

    def serve(self, stream: TextIO) -> int:
        """Answer each request until the input ends. Returns the exit code."""
        while True:
            try:
                line = read_line(stream)

                if line is None:
                    return 0

                message = strict_object(line)
            except ValueError as error:
                sys.stderr.write(f"{self._name}: {error}\n")

                return EXIT_BAD_INPUT

            self._take(message)

    def _take(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        ident = message.get("id")
        body = cast("dict[str, Any]", params) if isinstance(params, dict) else {}

        if ident is None:
            return

        if method == "initialize":
            version = body.get("protocolVersion")
            self._answer(ident, self._initialized(version))
        elif method == "ping":
            self._answer(ident, {})
        elif method == "tools/list":
            self._answer(ident, {"tools": self._tools})
        elif method == "tools/call":
            threading.Thread(target=self._call, args=(ident, body), daemon=True).start()
        else:
            self._fail(ident, _METHOD_NOT_FOUND, f"no method {method}")

    def _initialized(self, version: object) -> dict[str, object]:
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": self._name, "version": "0"},
        }

    def _call(self, ident: object, body: Mapping[str, Any]) -> None:
        name = body.get("name")
        arguments = body.get("arguments", {})
        meta: object = body.get("_meta", {})

        if name not in self._names or not isinstance(arguments, dict):
            self._fail(ident, _INVALID_PARAMS, f"no tool {name} with an object of arguments")
            return

        known = cast("dict[str, Any]", arguments)
        held = cast("dict[str, Any]", meta) if isinstance(meta, dict) else {}
        text, is_error = self._handler(str(name), known, held)
        self._answer(ident, {"content": [{"type": "text", "text": text}], "isError": is_error})

    def _answer(self, ident: object, result: object) -> None:
        self._write({"jsonrpc": "2.0", "id": ident, "result": result})

    def _fail(self, ident: object, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})

    def _write(self, message: dict[str, object]) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"

        with self._lock:
            sys.stdout.write(line)
            sys.stdout.flush()
