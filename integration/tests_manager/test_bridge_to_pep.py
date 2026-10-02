"""Seam 2: the real PEP bridge bundle against the real PEP family path.

Packet B6 coded the manifest's fields and `/call`'s response body from
contract 04, never from the PEP, and said so in its own caveat. This file
closes it. `supervisor/` is built with its documented commands, and the
bundle that would ship in the sandbox image is driven the way
`supervisor/test/bridge.test.ts` drives it -- with one thing swapped:

    bridge.test.ts:  bridge ──► test/fake-pep.ts   (no grants, no audit)
    this file:       bridge ──► the real PEP       (managerd's grant file)

The PEP listens on 127.0.0.1, never on the host's LAN address, and nothing
here needs the host.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .conftest import Applied
from .pep_harness import EMBED_MODEL, build_pep, free_port, serving

DRIVER = Path(__file__).parent / "node" / "bridge_driver.mjs"

SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
DELEGATION = "01JBQ7X1M2T9V6K2H8M3N5PQRS"

DRIVER_TIMEOUT_S = 120


@pytest.fixture
def pep_url(applied: Applied, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """The real PEP on a loopback port, for as long as a test lasts."""
    app = build_pep(monkeypatch, tmp_path, applied.state_root)
    with serving(app, free_port()) as url:
        yield url


def write_turn_file(control_dir: Path, delegation: str | None = None) -> Path:
    """The per-session file the supervisor rewrites at each turn (contract
    03 §7.4). `apply_once` created the control directory this sits in."""
    directory = control_dir / "sessions" / SESSION
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "turn.json"
    body = {"session": SESSION, "turn": TURN, "delegation": delegation}
    path.write_text(f"{json.dumps(body)}\n", encoding="utf-8")
    return path


def drive(
    node_bin: str,
    supervisor_build: Path,
    applied: Applied,
    pep_url: str,
    scratch: Path,
    *,
    tool: str = "",
    args: dict[str, Any] | None = None,
    delegation: str | None = None,
) -> dict[str, Any]:
    """Run the bundle once and read its report."""
    report = scratch / "bridge-report.json"
    argv = [node_bin, str(DRIVER), str(supervisor_build / "dist" / "pep-bridge.js"), str(report)]
    if tool:
        argv += [tool, json.dumps(args or {})]

    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=DRIVER_TIMEOUT_S,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            # Contract 03 §7: what the supervisor sets for each pi child.
            "PEP_URL": pep_url,
            "PEP_TOKEN": applied.token,
            "AGENT_SESSION": SESSION,
            "AGENT_TURN": TURN,
            "AGENT_TURN_FILE": str(write_turn_file(applied.control_dir, delegation)),
        },
    )
    if done.returncode != 0:
        pytest.fail(f"bridge_driver exited {done.returncode}:\n{done.stdout}\n{done.stderr}")

    body: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
    return body


def audit_lines(applied: Applied) -> list[dict[str, Any]]:
    """Every v2 audit line the PEP wrote, in file order (contract 04 §6)."""
    lines: list[dict[str, Any]] = []
    for path in sorted((applied.state_root / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            lines.append(json.loads(line))
    return lines


# --- the manifest becomes pi tools ---------------------------------------


def test_the_bridge_registers_the_granted_tools(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    report = drive(node_bin, supervisor_build, applied, pep_url, scratch)
    assert [tool["name"] for tool in report["tools"]] == ["embed", "ha_call"]


def test_the_schema_reaches_pi_unchanged(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """The manifest's JSON Schema goes to pi as pi's `parameters`. B6 coded
    the field name from the contract; this proves the PEP fills it."""
    report = drive(node_bin, supervisor_build, applied, pep_url, scratch)
    embed = next(tool for tool in report["tools"] if tool["name"] == "embed")
    assert embed["parameters"]["required"] == ["input"]
    assert embed["description"]


def test_a_narrowed_grant_reaches_the_next_process(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """The manifest is a snapshot per process (contract 04 §4 rule 2), so a
    narrowing shows up when the next pi process starts."""
    from .conftest import CHAT_FAMILY

    applied.reapply({**CHAT_FAMILY, "verbs": {"embed": {}}})

    report = drive(node_bin, supervisor_build, applied, pep_url, scratch)
    assert [tool["name"] for tool in report["tools"]] == ["embed"]


# --- one call, all the way through ---------------------------------------


def test_one_call_completes(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """`embed` needs no sandbox egress: the PEP reaches the local embedding
    service, which `pep/tests` already fakes."""
    report = drive(
        node_bin, supervisor_build, applied, pep_url, scratch, tool="embed", args={"input": "hi"}
    )
    call = report["call"]
    assert call["ok"] is True, call["error"]
    assert EMBED_MODEL in call["text"]


def test_the_result_reaches_the_model_inside_an_untrusted_block(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """Invariant 14. The PEP's body shape must survive the wrapper."""
    report = drive(
        node_bin, supervisor_build, applied, pep_url, scratch, tool="embed", args={"input": "hi"}
    )
    text = report["call"]["text"]
    assert text.startswith('[untrusted output from "embed"')
    assert text.rstrip().endswith('[end of untrusted output from "embed"]')
    assert EMBED_MODEL in text


def test_a_denial_reaches_the_model_as_a_tool_error(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """Contract 04 §5. `input` is required, so the PEP refuses the shape."""
    report = drive(node_bin, supervisor_build, applied, pep_url, scratch, tool="embed", args={})
    call = report["call"]
    assert call["ok"] is False
    assert "arg_validation" in call["error"]


# --- the audit line ------------------------------------------------------


def test_the_audit_line_carries_the_session_and_turn(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    drive(node_bin, supervisor_build, applied, pep_url, scratch, tool="embed", args={"input": "hi"})
    call = next(line for line in audit_lines(applied) if line["tool"] == "embed")
    assert call["claimed"]["session_id"] == SESSION
    assert call["claimed"]["turn_id"] == TURN
    assert call["decision"] == "allow"


def test_the_audit_line_carries_the_delegation_id(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """Contract 04 §3's third header. Stage 1's supervisor always writes
    null, so the bridge's own path is what this proves."""
    drive(
        node_bin,
        supervisor_build,
        applied,
        pep_url,
        scratch,
        tool="embed",
        args={"input": "hi"},
        delegation=DELEGATION,
    )
    call = next(line for line in audit_lines(applied) if line["tool"] == "embed")
    assert call["claimed"]["delegation_id"] == DELEGATION


def test_the_manifest_fetch_is_audited_too(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    drive(node_bin, supervisor_build, applied, pep_url, scratch)
    manifest = next(line for line in audit_lines(applied) if line["tool"] == "$manifest")
    assert manifest["family"] == applied.family
    assert manifest["claimed"]["session_id"] == SESSION


def test_the_audit_line_names_the_grant_revision(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """Contract 04 §6.1. The revision ties the decision to the exact grant
    file `managerd` wrote."""
    written = json.loads(applied.grant_path.read_text(encoding="utf-8"))["rev"]
    drive(node_bin, supervisor_build, applied, pep_url, scratch)
    manifest = next(line for line in audit_lines(applied) if line["tool"] == "$manifest")
    assert manifest["grants_rev"] == written


def test_a_delegation_id_the_pep_will_not_take_is_dropped_not_denied(
    node_bin: str, supervisor_build: Path, applied: Applied, pep_url: str, scratch: Path
) -> None:
    """The two sides read the header differently: the bridge sends any
    session-shaped value up to 128 characters, the PEP takes a ULID only.
    Contract 04 §3.1 makes that safe -- a header decides nothing -- so the
    call still completes and the audit simply records no delegation.
    """
    report = drive(
        node_bin,
        supervisor_build,
        applied,
        pep_url,
        scratch,
        tool="embed",
        args={"input": "hi"},
        delegation="not-a-ulid",
    )
    assert report["call"]["ok"] is True

    call = next(line for line in audit_lines(applied) if line["tool"] == "embed")
    assert call["claimed"]["delegation_id"] is None
