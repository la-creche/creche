"""The MCP upstream double every path-level test drives.

It was shared by the instance and family suites. Only the family suite is
left, and the double is unchanged: it is a stand-in for the pool, not for an
identity kind.

The launchers below are for the tests that start REAL stdio children. The
PEP starts every one through `chaperone-as`, which a test cannot run for
real: it needs CAP_SETUID and an `mcp-<name>` user.
"""

from __future__ import annotations

import json
import sys
from functools import partial
from pathlib import Path

from chaperone.mcp_client import Launcher, StdioUpstreamPool, UpstreamError, UpstreamTool
from chaperone.reload_pool import PoolFactory

#: `chaperone-as` with the switch faked (`fake_run_as.py`): the real
#: launcher's checks, environment and exec, as the test's own user.
FAKE_AS = Path(__file__).resolve().parent / "fake_run_as.py"
AS_TEST_USER = Launcher((sys.executable, str(FAKE_AS), "--"))

#: The same exec, checking nothing. For upstreams called `a` and `b`, which
#: are no server names, so the real launcher refuses them.
UNCHECKED = Launcher((sys.executable, str(FAKE_AS), "--unchecked", "--"))


def pools(launcher: Launcher) -> PoolFactory:
    """`ReloadablePool(make_pool=...)`: every generation starts through
    `launcher` instead of the installed one."""
    return partial(StdioUpstreamPool, launcher=launcher)


ECHO_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


#: Annotated the way ha-mcp v8.4.3 really sends them (measured 2026-09-16):
#: the write tool OMITS readOnlyHint rather than saying false; the read tool
#: says true. Pass as `FakePool(extra={"ha": HA_TOOLS})`.
HA_TOOLS: dict[str, UpstreamTool] = {
    "ha_call_service": UpstreamTool(
        name="ha_call_service",
        description="Call a Home Assistant service.",
        input_schema=ECHO_SCHEMA,
        annotations={"openWorldHint": False, "destructiveHint": True, "title": "Call Service"},
    ),
    "ha_get_state": UpstreamTool(
        name="ha_get_state",
        description="Read one entity's state.",
        input_schema=ECHO_SCHEMA,
        annotations={"readOnlyHint": True, "title": "Get State"},
    ),
}


class FakePool:
    """UpstreamPool double: one kagi tool listed, plus one unlisted tool so
    manifest filtering is observable. `extra` adds whole upstreams."""

    def __init__(self, extra: dict[str, dict[str, UpstreamTool]] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.fail_with: str | None = None
        self._tools = {
            "kagi": {
                "kagi_search_fetch": UpstreamTool(
                    name="kagi_search_fetch",
                    description="Search the web with Kagi.",
                    input_schema=ECHO_SCHEMA,
                ),
                "kagi_summarizer": UpstreamTool(
                    name="kagi_summarizer",
                    description="Summarize a URL.",
                    input_schema=ECHO_SCHEMA,
                ),
            },
            **(extra or {}),
        }

    def tools(self, server: str) -> dict[str, UpstreamTool]:
        return self._tools.get(server, {})

    async def call(self, server: str, tool: str, args: dict[str, object]) -> str:
        self.calls.append((server, tool, args))
        if self.fail_with is not None:
            raise UpstreamError(self.fail_with)
        return f"{server}:{tool}:{json.dumps(args, sort_keys=True)}"
