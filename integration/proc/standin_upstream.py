"""A stand-in upstream of the Rust chaperone: an MCP server over stdio.

    standin_upstream.py <state directory>

The install file of the chaperone enables it as an effect plugin. Its
manifest declares one secret parameter, `SECRET_PARAM`, so the chaperone
writes the secret line first (chaperone `docs/design.md` §7.5). This
program refuses to serve without that line: exit code 2.

It serves two tools. Each one gives back its arguments as one text block:
the canonical JSON of the arguments, with sorted keys.

    echo          the policy grants it with no approval
    gated_echo    the policy grants it with `approval = "required"`

Every fact is a file under the state directory:

    calls.jsonl   one line for each tool call, in arrival order:
                  the tool, the arguments and the `_meta` of the call

The line of a call is whole in `calls.jsonl` before its answer starts. The
secret value is in no file and in no answer.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, cast

from proc_strict import strict_object
from standin_mcp import EXIT_BAD_INPUT, Result, Server, read_line, tool

NAME: Final = "standin"
ECHO: Final = "echo"
GATED_ECHO: Final = "gated_echo"
SECRET_PARAM: Final = "STANDIN_KEY"
CALLS_FILE: Final = "calls.jsonl"


def canonical(arguments: Mapping[str, Any]) -> str:
    """The text of an answer: the arguments with sorted keys and no white space."""
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Upstream:
    """The record of each call. Each line of the record is one write under one lock."""

    def __init__(self, state: Path) -> None:
        self._state = state
        self._lock = threading.Lock()

    def call(self, name: str, arguments: Mapping[str, Any], meta: Mapping[str, Any]) -> Result:
        line = json.dumps({"tool": name, "arguments": arguments, "meta": meta}) + "\n"

        with self._lock, (self._state / CALLS_FILE).open("ab") as calls:
            calls.write(line.encode("utf-8"))

        return canonical(arguments), False


def secret_line_is_valid(line: str | None) -> bool:
    """Whether the first line holds the one secret of the manifest, and nothing else."""
    if line is None:
        return False

    try:
        message = strict_object(line)
    except ValueError:
        return False

    secrets = message.get("secrets")

    if set(message) != {"secrets"} or not isinstance(secrets, dict):
        return False

    held = cast("dict[str, object]", secrets)
    value = held.get(SECRET_PARAM)

    return list(held) == [SECRET_PARAM] and isinstance(value, str) and bool(value)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: standin_upstream.py <state directory>\n")

        return 64

    try:
        first = read_line(sys.stdin)
    except ValueError:
        first = None

    if not secret_line_is_valid(first):
        sys.stderr.write(f"standin_upstream: the first line holds no secret {SECRET_PARAM}\n")

        return EXIT_BAD_INPUT

    upstream = Upstream(Path(argv[0]))

    return Server(NAME, [tool(ECHO), tool(GATED_ECHO)], upstream.call).serve(sys.stdin)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
