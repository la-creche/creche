"""A stuck upstream must not hold `/call` open indefinitely. The MCP
call has an SDK-level read timeout and surfaces as `upstream_failed`, not as
a hang or a misfiled `internal_error`.

The delegate call's own wall-clock cap is contract 04 §7.5's, and
`test_pep_delegate_door.py` holds it (`a door slower than the limit times
out`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from agent_pep import mcp_client as mcp_client_module
from agent_pep.mcp_client import StdioUpstreamPool, UpstreamError, UpstreamSpec
from pep_helpers import AS_TEST_USER

STUB = Path(__file__).parent / "stub_mcp_server.py"


@pytest.mark.slow
async def test_mcp_call_tool_timeout_raises_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_client_module, "UPSTREAM_CALL_TIMEOUT_S", 0.2)
    specs = {"stub": UpstreamSpec(name="stub", command=sys.executable, args=(str(STUB),), env={})}
    pool = StdioUpstreamPool(specs, {}, launcher=AS_TEST_USER)
    await pool.start()
    try:
        with pytest.raises(UpstreamError):
            await pool.call("stub", "sleep", {"seconds": 5})
    finally:
        await pool.stop()
