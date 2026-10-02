"""The PEP's unit grants `agent-pep-as` its two capabilities, and no more.

The launcher drops each MCP server to its own `mcp-<name>` user
before exec: `setgroups` and `setresgid` need CAP_SETGID, and `setresuid`
needs CAP_SETUID. The PEP runs as `pep`, not root, so the unit has to grant
both, and ambient, because only an ambient capability survives the exec of
an unprivileged binary such as the launcher.

What these pin is the unit's whole grant: the two lines, the
same two words on each, no other line that names a capability, and every
line that keeps the grant narrow still there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

UNIT: Final = Path(__file__).resolve().parents[2] / "systemd" / "agent-pep.service"
TWO: Final = "CAP_SETUID CAP_SETGID"


def _directives() -> list[str]:
    text = UNIT.read_text(encoding="utf-8")

    return [line.strip() for line in text.splitlines() if line.strip() and line[0] not in "#;"]


def test_the_unit_grants_exactly_the_two_capabilities_the_switch_needs() -> None:
    lines = _directives()

    assert [one for one in lines if one.startswith("CapabilityBoundingSet=")] == [
        f"CapabilityBoundingSet={TWO}"
    ]
    assert [one for one in lines if one.startswith("AmbientCapabilities=")] == [
        f"AmbientCapabilities={TWO}"
    ]


def test_no_other_line_names_a_capability() -> None:
    assert [one for one in _directives() if "CAP_" in one] == [
        f"CapabilityBoundingSet={TWO}",
        f"AmbientCapabilities={TWO}",
    ]


def test_the_lines_that_keep_the_grant_narrow_stay() -> None:
    """`pep` still runs the PEP. no_new_privs still refuses a setuid binary
    and a file capability to the PEP and to every server it starts. The
    syscall filter already holds `@setuid`, so it did not have to widen."""
    lines = _directives()

    for kept in (
        "User=pep",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "SystemCallFilter=@system-service",
    ):
        assert kept in lines, kept

    assert not [one for one in lines if one.startswith(("SecureBits=", "PrivateUsers="))]
