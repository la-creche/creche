"""The Rust chaperone with its two effect stand-ins: one smoke scenario.

The fixture writes the files of the chaperone, mints one token for each
family, runs `chaperone check` on both files, starts `chaperone serve` and
runs `chaperone verify` against it. The scenario here then reads what
crossed the front door, the audit file and the record of the upstream.
"""

from __future__ import annotations

from proc_chaperone_rs import ChaperoneRsStack
from proc_standins import upstream_calls
from proc_tree import FAMILY, RS_UPSTREAM

ECHO = f"{RS_UPSTREAM}__echo"
GATED_ECHO = f"{RS_UPSTREAM}__gated_echo"


def test_chaperone_rs_lists_the_stand_in_tools_and_audits_one_call(
    chaperone_rs: ChaperoneRsStack,
) -> None:
    """An allowed call: the audit file holds `decided`, then `ran` (§5.4)."""
    with chaperone_rs.mcp(FAMILY) as session:
        names = session.tool_names()
        result = session.call(ECHO, {"text": "hi"})

    assert names == [ECHO, GATED_ECHO]
    assert result == {"content": [{"type": "text", "text": '{"text":"hi"}'}], "isError": False}
    assert [(record["tool"], record["event"]) for record in chaperone_rs.audit()] == [
        (ECHO, "decided"),
        (ECHO, "ran"),
    ]
    assert [
        (call["tool"], call["meta"]["chaperone.app"]) for call in upstream_calls(chaperone_rs.tree)
    ] == [("echo", FAMILY)]
