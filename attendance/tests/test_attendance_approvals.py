"""A tool call held at the PEP (contract 04 §8.6, contract 02 §4.1).

Nothing in contract 04 sends `attendance` a message when a gate opens. The one
artefact that exists is the PEP's audit record, `decision: pending` (§6.4),
so this drives the service by appending to that file and reads back the
session state and the journal.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from attendance.auth import Principal
from attendance.clock import now, rfc3339
from attendance.errors import TurnReason
from attendance.faults import FaultCode, blocks_turns
from attendance.models import LineKind
from attendance.paths import audit_file, fault_file
from attendance.requests import CreateRequest, RunTurnRequest
from attendance.service import SessionService
from attendance.states import SessionState, TurnState
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    SANDBOX,
    FakeFleet,
    make_config,
    settle_now,
    wait_until,
    write_status,
)

OWUI = Principal.DOOR_OWUI
DOOR = "owui-1"
PROMPT = "Turn the boiler on."
GATE = "9f2a71c4d0b83e15"
TOOL = "ha_call"
OTHER_SESSION = "owui-11111111-2222-4333-8444-555555555555"

#: One audit serves every family, so an unreadable one faults them all.
OTHER_FAMILY = "ha-review"


class Rig:
    """One service, one fake playpen, and the PEP's audit file."""

    def __init__(
        self, service: SessionService, fleet: FakeFleet, audit: Path, state_root: Path
    ) -> None:
        self.service = service
        self.fleet = fleet
        self.audit = audit
        self.state_root = state_root

    def fault_codes(self, family: str = FAMILY) -> list[str]:
        """The codes this service currently reports (contract 05 §3.3.1)."""
        path = fault_file(self.state_root, family)

        if not path.is_file():
            return []

        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return [str(entry["code"]) for entry in payload["faults"]]

    async def start(self, session: str = CHAT_SESSION) -> str:
        self.service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=session))
        live = await self.service.run_turn(
            OWUI, FAMILY, session, RunTurnRequest(prompt=PROMPT), DOOR
        )
        await self.fleet.playpen().next_start()
        return live.record.turn

    def append(self, **fields: Any) -> None:
        """One audit record, in contract 04 §6.1's shape."""
        record: dict[str, Any] = {
            "ts": rfc3339(now()),
            "family": FAMILY,
            "sandbox_id": SANDBOX,
            "tool": TOOL,
            "decision": "pending",
            "reason": "pending",
            "waited_ms": 0,
            "gate": GATE,
            "claimed": {"session_id": CHAT_SESSION, "turn_id": None, "delegation_id": None},
        }
        claimed = fields.pop("claimed", None)

        if claimed is not None:
            record["claimed"].update(claimed)

        record.update(fields)
        self.audit.parent.mkdir(parents=True, exist_ok=True)

        with self.audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def state(self, session: str = CHAT_SESSION) -> SessionState:
        body = self.service.get_session(OWUI, FAMILY, session, 0)
        return SessionState(body["state"])

    def kinds(self, session: str = CHAT_SESSION) -> list[LineKind]:
        return [line.kind for line in self.service.store.journal.replay(FAMILY, session)]

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path) -> Rig:
    config = make_config(tmp_path)
    write_status(config.state_root)
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()
    day = now().strftime("%Y-%m-%d")
    return Rig(service, fleet, audit_file(config.state_root, day), config.state_root)


async def test_a_pending_record_shows_waiting_approval(tmp_path: Path) -> None:
    """Contract 04 §8.6 and contract 02 §4.1 rule 1."""
    rig = await build(tmp_path)
    turn = await rig.start()

    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    assert rig.state() is SessionState.WAITING_APPROVAL
    assert rig.kinds()[-1] is LineKind.APPROVAL_REQUESTED
    await rig.stop()


async def test_an_approval_returns_the_turn_to_running(tmp_path: Path) -> None:
    """Contract 02 §4.3. `waiting-approval` returns to `running` on approve."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    rig.append(claimed={"turn_id": turn}, decision="allow", reason="approved", waited_ms=4200)
    rig.service.read_gates()
    lines = list(rig.service.store.journal.replay(FAMILY, CHAT_SESSION))

    assert rig.state() is SessionState.RUNNING
    assert lines[-1].kind is LineKind.APPROVAL_RESOLVED
    assert lines[-1].body == {"decision": "approved", "waited_s": 4}
    await rig.stop()


async def test_a_denial_returns_the_turn_to_running(tmp_path: Path) -> None:
    """Contract 02 §4.3. The model reports the tool error itself."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    rig.append(claimed={"turn_id": turn}, decision="deny", reason="approval_denied")
    rig.service.read_gates()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.state is TurnState.RUNNING
    assert live.record.reason is None
    assert live.record.gates.denied == 1
    await rig.stop()


async def test_a_timeout_returns_the_turn_to_running(tmp_path: Path) -> None:
    """Contract 04 §8.5's "a tool error it can report": the turn runs on to report it."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    rig.append(claimed={"turn_id": turn}, decision="deny", reason="approval_timeout")
    rig.service.read_gates()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.state is TurnState.RUNNING
    assert live.record.gates.timed_out == 1
    await rig.stop()


async def test_an_abandoned_gate_still_ends_the_turn(tmp_path: Path) -> None:
    """Contract 02 §4.3's table. The caller is gone, so nobody is left to
    report to: this is the one resolution that ends a turn."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    rig.append(claimed={"turn_id": turn}, decision="deny", reason="approval_abandoned")
    rig.service.read_gates()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.state is TurnState.FAILED
    assert live.record.reason is TurnReason.APPROVAL_DENIED
    await rig.stop()


@pytest.mark.parametrize("reason", ["approval_undeliverable", "approval_revoked"])
async def test_a_refusal_with_a_live_caller_lets_the_turn_go_on(
    tmp_path: Path, reason: str
) -> None:
    """Contract 02 §4.3. No phone saw the gate, or the grant went
    away while the call waited. Either way the PEP hands a LIVE sandbox a tool
    error, exactly as for a deny, so the model reports it and the turn goes
    on. A call made one second after the grant went away gets a plain denial
    and its turn goes on: a call caught inside the gate must fare no worse."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    rig.append(claimed={"turn_id": turn}, decision="deny", reason=reason)
    rig.service.read_gates()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.state is TurnState.RUNNING
    await rig.stop()


async def test_the_turn_counts_its_gates(tmp_path: Path) -> None:
    """Contract 02 §4.4's `approvals`, which §13.1's record sums."""
    rig = await build(tmp_path)
    turn = await rig.start()

    for index in range(2):
        rig.append(claimed={"turn_id": turn}, gate=f"{index:016x}")
        rig.service.read_gates()
        rig.append(
            claimed={"turn_id": turn}, gate=f"{index:016x}", decision="allow", reason="approved"
        )
        rig.service.read_gates()

    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.approvals == 2
    await rig.stop()


async def test_one_turn_counts_an_approval_and_a_denial(tmp_path: Path) -> None:
    """Contract 02 §13.1.1. No single failure reason could record this.

    The turn survives the refusal, so it meets a second gate. `approved`
    and `denied` are counted apart, from the PEP's own resolving records.
    """
    rig = await build(tmp_path)
    turn = await rig.start()

    for index, reason in enumerate(["approved", "approval_denied"]):
        rig.append(claimed={"turn_id": turn}, gate=f"{index:016x}")
        rig.service.read_gates()
        rig.append(claimed={"turn_id": turn}, gate=f"{index:016x}", reason=reason, decision="deny")
        rig.service.read_gates()

    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.state is TurnState.RUNNING
    assert live.record.approvals == 2
    assert live.record.gates.approved == 1
    assert live.record.gates.denied == 1
    assert live.record.gates.timed_out == 0
    await rig.stop()


async def test_an_undeliverable_push_counts_as_denied(tmp_path: Path) -> None:
    """Contract 02 §13.1.1: §8.4's undeliverable IS a denial of the call."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    rig.append(claimed={"turn_id": turn}, decision="deny", reason="approval_undeliverable")
    rig.service.read_gates()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.gates.denied == 1
    await rig.stop()


async def test_a_revoked_gate_counts_in_requested_alone(tmp_path: Path) -> None:
    """Contract 02 §13.1.1. The grant went away, so nobody decided the call."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()

    rig.append(claimed={"turn_id": turn}, decision="deny", reason="approval_revoked")
    rig.service.read_gates()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    assert live.record.approvals == 1
    assert live.record.gates.approved == 0
    assert live.record.gates.denied == 0
    assert live.record.gates.timed_out == 0
    await rig.stop()


async def test_the_tally_survives_a_restart(tmp_path: Path) -> None:
    """A job's record is built after the turn ends, so the counts are stored."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()
    rig.append(claimed={"turn_id": turn}, decision="deny", reason="approval_denied")
    rig.service.read_gates()

    stored = [one for one in rig.service.store.load_turns(FAMILY, CHAT_SESSION) if one.turn == turn]

    assert stored
    assert stored[0].gates.denied == 1
    await rig.stop()


async def test_a_claim_naming_no_turn_is_dropped(tmp_path: Path) -> None:
    """The claimed ids are the sandbox's (contract 04 §3.1), so they are
    used only where the host already owns that exact turn."""
    rig = await build(tmp_path)
    await rig.start()

    rig.append(claimed={"session_id": OTHER_SESSION, "turn_id": "01JBQ7WZ0X4T9V6K2H8M3N5PQR"})
    rig.service.read_gates()

    assert rig.state() is SessionState.RUNNING
    assert LineKind.APPROVAL_REQUESTED not in rig.kinds()
    await rig.stop()


async def test_a_record_is_read_once(tmp_path: Path) -> None:
    """The file only grows, so each poll reads from where the last stopped."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})

    rig.service.read_gates()
    rig.service.read_gates()

    assert rig.kinds().count(LineKind.APPROVAL_REQUESTED) == 1
    await rig.stop()


async def test_a_turn_that_waits_for_approval_can_settle(tmp_path: Path) -> None:
    """Contract 03 §13 rule 8 accepts `turn_settled` for such a turn.

    pi settles after the gated call returned, so the PEP decided the gate.
    The audit tail lags, so this service can still show `waiting-approval`.
    Contract 02 §4.3 has no `waiting-approval -> settled`, so the turn goes
    through `running`. The turn must not wait for its deadline.
    """
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    await rig.fleet.playpen().settle(CHAT_SESSION, turn)
    await settle_now(live.done)

    assert live.record.state is TurnState.SETTLED
    assert rig.kinds()[-2:] == [LineKind.APPROVAL_REQUESTED, LineKind.TURN_SETTLED]
    assert LineKind.NOTE not in rig.kinds()
    assert rig.state() is SessionState.IDLE
    await rig.stop()


async def test_a_settle_reads_the_decision_of_its_gate_first(tmp_path: Path) -> None:
    """The decision is in the audit file and the poll did not read it yet.
    The settle reads it, so the journal and the tally hold the decision."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()
    rig.append(claimed={"turn_id": turn}, decision="allow", reason="approved", waited_ms=4200)
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    await rig.fleet.playpen().settle(CHAT_SESSION, turn)
    await settle_now(live.done)

    assert live.record.state is TurnState.SETTLED
    assert rig.kinds()[-2:] == [LineKind.APPROVAL_RESOLVED, LineKind.TURN_SETTLED]
    assert live.record.gates.approved == 1
    await rig.stop()


async def test_a_gate_that_ends_the_turn_wins_over_a_late_settle(tmp_path: Path) -> None:
    """The decision in the audit file ends the turn. The settle changes nothing."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.service.read_gates()
    rig.append(claimed={"turn_id": turn}, decision="deny", reason="approval_abandoned")
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)

    assert live is not None
    await rig.fleet.playpen().settle(CHAT_SESSION, turn)
    await settle_now(live.done)

    assert live.record.state is TurnState.FAILED
    assert live.record.reason is TurnReason.APPROVAL_DENIED
    assert rig.kinds()[-1] is LineKind.TURN_FAILED
    assert live.record.usage.input == 0
    await rig.stop()


async def test_a_move_the_contract_refuses_is_visible(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move that contract 02 §4.3 does not allow is a defect somewhere.

    The turn does not move. A log line and a `note` line say so, because a
    move that is refused in silence hides the defect. No known path makes
    such a move, so this test gives the service a state table that refuses
    each move.
    """
    rig = await build(tmp_path)
    turn = await rig.start()
    monkeypatch.setattr("attendance.service.can_move", lambda current, wanted: False)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        await rig.fleet.playpen().settle(CHAT_SESSION, turn)
        await wait_until(lambda: "cannot become settled" in caplog.text)

    live = rig.service.live_turn(FAMILY, CHAT_SESSION, turn)
    lines = list(rig.service.store.journal.replay(FAMILY, CHAT_SESSION))

    assert live is not None
    assert live.record.state is TurnState.RUNNING
    assert lines[-1].kind is LineKind.NOTE
    assert lines[-1].turn == turn
    assert lines[-1].body == {
        "note": "illegal_transition",
        "from": "running",
        "to": "settled",
    }
    await rig.stop()


async def test_a_missing_audit_file_is_silence(tmp_path: Path) -> None:
    """No audit yet is not a fault. The PEP may simply not have written one."""
    rig = await build(tmp_path)
    await rig.start()

    rig.service.read_gates()

    assert rig.state() is SessionState.RUNNING
    assert rig.fault_codes() == []
    await rig.stop()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory anyway")
async def test_an_unreadable_audit_raises_a_fault(tmp_path: Path) -> None:
    """Contract 05 §3.3's `audit_unreadable`.

    The PEP owns the audit at 0640 (contract 04 §6). A host that leaves
    `attendance`'s user out of that group gets `running` where it would have
    got `waiting-approval`, and this fault says why.
    """
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.audit.parent.chmod(0o000)

    try:
        rig.service.read_gates()

        assert rig.state() is SessionState.RUNNING
        assert rig.fault_codes() == [FaultCode.AUDIT_UNREADABLE.value]
        assert blocks_turns(FaultCode.AUDIT_UNREADABLE) is False
    finally:
        rig.audit.parent.chmod(0o700)

    await rig.stop()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory anyway")
async def test_the_audit_fault_clears_by_itself(tmp_path: Path) -> None:
    """Item B: it clears when the file becomes readable, with no restart."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    rig.audit.parent.chmod(0o000)
    rig.service.read_gates()
    rig.audit.parent.chmod(0o700)

    rig.service.read_gates()

    assert rig.fault_codes() == []
    assert rig.state() is SessionState.WAITING_APPROVAL
    await rig.stop()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory anyway")
async def test_the_audit_fault_names_every_family(tmp_path: Path) -> None:
    """One audit serves every family, and the fault file is per family."""
    rig = await build(tmp_path)
    await rig.start()
    write_status(rig.state_root, family=OTHER_FAMILY)
    rig.audit.parent.mkdir(parents=True, exist_ok=True)
    rig.audit.parent.chmod(0o000)

    try:
        rig.service.read_gates()

        assert rig.fault_codes(OTHER_FAMILY) == [FaultCode.AUDIT_UNREADABLE.value]
    finally:
        rig.audit.parent.chmod(0o700)

    await rig.stop()


async def test_a_line_nested_too_deep_is_skipped(tmp_path: Path) -> None:
    """The JSON reader raises RecursionError on this line, not ValueError."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.audit.parent.mkdir(parents=True, exist_ok=True)
    rig.audit.write_text("[" * 200_000 + "\n", encoding="utf-8")
    rig.append(claimed={"turn_id": turn})

    rig.service.read_gates()

    assert rig.state() is SessionState.WAITING_APPROVAL
    await rig.stop()


async def test_a_torn_line_waits_for_its_rest(tmp_path: Path) -> None:
    """An append-only file can be read mid-write."""
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.append(claimed={"turn_id": turn})
    whole = rig.audit.read_text(encoding="utf-8")
    rig.audit.write_text(whole[:-20], encoding="utf-8")

    rig.service.read_gates()
    assert rig.state() is SessionState.RUNNING

    rig.audit.write_text(whole, encoding="utf-8")
    rig.service.read_gates()

    assert rig.state() is SessionState.WAITING_APPROVAL
    await rig.stop()
