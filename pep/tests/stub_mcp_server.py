"""Minimal stdio MCP server for tests: one echo tool that also reveals an env
var, so the per-upstream env delivery is observable from the client side, and
one sleep tool that never answers in time, so the client's call timeout
can be exercised without a real stuck upstream. `echo` carries a
readOnlyHint and `sleep` no annotations at all, so both halves of what the
PEP keeps from `list_tools` cross a real stdio pipe.

If STUB_TRACE names a file, this process appends its pid to it at startup. That
is how the lazy-spawn tests count how many processes an upstream actually cost:
a probe that did not close, or a burst that spawned twice, shows up as an extra
line rather than as a timing guess.

STUB_LINGER_S keeps the process alive that long after stdin closes. It widens
the client's close window so a test can land a call inside it.

If STUB_ENV_TRACE names a file, this process writes its whole environment to it
at startup, as one JSON object. That is how a test sees what the server really
received, rather than what the PEP meant to send.

If STUB_HOLD names a file, this process waits for that file to exist before it
serves, for at most HOLD_CAP_S. A probe of it stays open until the test lets it
go, so a reload is mid-probe for certain rather than by a timing guess."""

from __future__ import annotations

import asyncio
import json
import os
import time

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

#: A held stub nobody releases gives up waiting, so a failed test cannot hang.
HOLD_CAP_S = 30.0
HOLD_POLL_S = 0.02

server = MCPServer("stub")

_trace = os.environ.get("STUB_TRACE")
if _trace:
    with open(_trace, "a", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\n")

_env_trace = os.environ.get("STUB_ENV_TRACE")
if _env_trace:
    with open(_env_trace, "w", encoding="utf-8") as fh:
        json.dump(dict(os.environ), fh)


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """Echo text back, prefixed with STUB_SECRET from the environment."""
    return f"{os.environ.get('STUB_SECRET', 'unset')}:{text}"


@server.tool()
async def sleep(seconds: float) -> str:
    """Never answer within `seconds` — stands in for a stuck upstream."""
    await asyncio.sleep(seconds)
    return "done"


def _hold() -> None:
    """Wait for STUB_HOLD's file, before `initialize` is answered."""
    gate = os.environ.get("STUB_HOLD")
    if not gate:
        return

    deadline = time.monotonic() + HOLD_CAP_S
    while not os.path.exists(gate) and time.monotonic() < deadline:
        time.sleep(HOLD_POLL_S)


if __name__ == "__main__":
    _hold()
    server.run("stdio")
    time.sleep(float(os.environ.get("STUB_LINGER_S", "0")))
