"""A stand-in approval channel of the Rust chaperone: an MCP server over stdio.

    standin_approval.py <state directory>

The install file of the chaperone puts it in the slot `approval_channel`.
It serves one tool, `approve`, with the arguments of chaperone
`docs/design.md` §5.3: `gate`, `app`, `tool` and `arguments`. The chaperone
calls it once for each gate, when the decision makes the gate, and waits for
the answer until the gate expires.

A test answers a gate with a file. The program takes the first file of
this list that exists, and holds the call until one exists:

    answers/<gate>    the gate ID: 32 lower-case hex characters
    answers/<tool>    the exposed name of the tool, for example
                      `standin__gated_echo`. It answers each gate of that tool.

The text of the file is the text of the one answer block, with no change:
`{"answer":"approved"}`, or `{"answer":"denied","reason":"<text>"}`. A test
can write any other text to make an answer that the chaperone refuses.

Every fact is a file under the state directory:

    requests.jsonl   one line for each call, in arrival order: the four
                     arguments and the `_meta` of the call

The line of a call is whole in `requests.jsonl` before the call waits. A
scenario waits for that line before it answers. A call whose arguments do
not have the shape of §5.3 gets an error result at once and no line.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from standin_mcp import Result, Server, tool

NAME: Final = "standin-approval"
APPROVE: Final = "approve"
REQUESTS_FILE: Final = "requests.jsonl"
ANSWERS_DIR: Final = "answers"

#: The keys of the arguments of `approve`.
ARGUMENT_KEYS: Final = frozenset({"gate", "app", "tool", "arguments"})

_GATE: Final = re.compile(r"[0-9a-f]{32}")
#: An exposed name: `<upstream>__<tool>`, the characters of chaperone §7.6.
_EXPOSED: Final = re.compile(r"[A-Za-z0-9_-]{1,50}")
_POLL_S: Final = 0.02


class Approval:
    """The record of each call, and the wait for the file of its answer."""

    def __init__(self, state: Path) -> None:
        self._state = state
        self._lock = threading.Lock()

    def call(self, name: str, arguments: Mapping[str, Any], meta: Mapping[str, Any]) -> Result:
        gate = arguments.get("gate")
        exposed = arguments.get("tool")

        if not _has_shape(arguments, gate, exposed):
            return "the arguments of approve do not have the shape of §5.3", True

        line = json.dumps({**arguments, "meta": meta}) + "\n"

        with self._lock, (self._state / REQUESTS_FILE).open("ab") as requests:
            requests.write(line.encode("utf-8"))

        return self._answer_of(str(gate), str(exposed)), False

    def _answer_of(self, gate: str, exposed: str) -> str:
        """The text of the first answer file for the gate. Waits until one exists."""
        answers = self._state / ANSWERS_DIR

        while True:
            for name in (gate, exposed):
                try:
                    return (answers / name).read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue

            time.sleep(_POLL_S)


def _has_shape(arguments: Mapping[str, Any], gate: object, exposed: object) -> bool:
    return (
        frozenset(arguments) == ARGUMENT_KEYS
        and isinstance(gate, str)
        and _GATE.fullmatch(gate) is not None
        and isinstance(exposed, str)
        and _EXPOSED.fullmatch(exposed) is not None
        and isinstance(arguments.get("app"), str)
        and isinstance(arguments.get("arguments"), dict)
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: standin_approval.py <state directory>\n")

        return 64

    approval = Approval(Path(argv[0]))

    return Server(NAME, [tool(APPROVE)], approval.call).serve(sys.stdin)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
