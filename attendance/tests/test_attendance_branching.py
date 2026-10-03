"""An Open WebUI edit or regenerate becomes a pi branch (contract 02 §10.2).

The four rules, then the two things that reach the channel: `branch` on
`start_turn` (contract 03 §4.1) and the retry when pi refuses the fork.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from attendance.auth import Principal
from attendance.branching import UNMAPPED_PARENT, Branch, decide
from attendance.models import LineKind, OwuiRefs
from attendance.requests import CreateRequest, RunTurnRequest
from attendance.service import SessionService
from attendance.states import TurnState
from attendance.wire import PlaypenReason
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    FakeFleet,
    make_config,
    settle_now,
    write_status,
)

OWUI = Principal.DOOR_OWUI
DOOR = "owui-1"
PROMPT = "Which sensor dropped out last night?"
ANSWER = "Sensor kitchen_temp stopped reporting at 02:14."
CHAT_ID = "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"

# Two settled turns, as §10.1 writes them: user then assistant, in order.
MAP = {"u1": "e-u1", "a1": "e-a1", "u2": "e-u2", "a2": "e-a2"}


# ------------------------------------------------------------ the four rules


def test_a_first_turn_just_prompts() -> None:
    """Rule 1. Nothing to attach to."""
    assert decide({}, None, 0).branch is Branch.PROMPT


def test_the_current_leaf_is_a_continuation() -> None:
    """Rule 2. The newest mapped entry is where pi already is."""
    assert decide(MAP, "a2", 2).branch is Branch.PROMPT


def test_an_older_parent_forks_at_the_next_user_entry() -> None:
    """Rule 3. pi forks from a previous USER message on the active branch."""
    found = decide(MAP, "a1", 2)

    assert found.branch is Branch.FORK
    assert found.fork_from == "e-u2"
    assert found.wire_branch == {"fork_from": "e-u2"}


def test_an_unmapped_parent_falls_back() -> None:
    """Rule 4. The turn runs and the divergence is written down."""
    found = decide(MAP, "a9", 2)

    assert found.branch is Branch.FALLBACK
    assert found.reason == UNMAPPED_PARENT
    assert found.wire_branch is None


def test_a_null_parent_on_a_used_session_falls_back() -> None:
    """§10.2 gives an edit of the first message no rule, so rule 4 takes it."""
    assert decide(MAP, None, 2).branch is Branch.FALLBACK


# ----------------------------------------------------------- on the channel


class Rig:
    def __init__(self, service: SessionService, fleet: FakeFleet) -> None:
        self.service = service
        self.fleet = fleet

    async def turn(self, parent: str | None, message: str, user: str) -> str:
        refs = OwuiRefs(chat_id=CHAT_ID, message_id=message, user_message_id=user, parent_id=parent)
        live = await self.service.run_turn(
            OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT, owui=refs), DOOR
        )
        await self.fleet.playpen().next_start()
        return live.record.turn

    async def settle(self, turn: str, user_entry: str, leaf: str) -> None:
        playpen = self.fleet.playpen()
        await playpen.settle(CHAT_SESSION, turn, user_entry_id=user_entry, leaf_id=leaf)
        live = self.service.live_turn(FAMILY, CHAT_SESSION, turn)
        assert live is not None
        await settle_now(live.done)

    def starts(self) -> list[dict[str, Any]]:
        return self.fleet.playpen().started

    def kinds(self) -> list[LineKind]:
        return [line.kind for line in self.service.store.journal.replay(FAMILY, CHAT_SESSION)]

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path) -> Rig:
    config = make_config(tmp_path)
    write_status(config.state_root)
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()
    service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=CHAT_SESSION))
    return Rig(service, fleet)


async def test_a_regenerate_puts_a_branch_on_start_turn(tmp_path: Path) -> None:
    """Contract 03 §4.1's `branch` field, from §10.2 rule 3."""
    rig = await build(tmp_path)
    first = await rig.turn(None, "a1", "u1")
    await rig.settle(first, "e-u1", "e-a1")
    second = await rig.turn("a1", "a2", "u2")
    await rig.settle(second, "e-u2", "e-a2")

    await rig.turn("a1", "a3", "u3")

    assert rig.starts()[-1]["branch"] == {"fork_from": "e-u2"}
    await rig.stop()


async def test_a_continuation_carries_no_branch(tmp_path: Path) -> None:
    rig = await build(tmp_path)
    first = await rig.turn(None, "a1", "u1")
    await rig.settle(first, "e-u1", "e-a1")

    await rig.turn("a1", "a2", "u2")

    assert "branch" not in rig.starts()[-1]
    await rig.stop()


async def test_an_unmapped_parent_writes_the_fallback_line(tmp_path: Path) -> None:
    """Contract 02 §8.1's `branch_fallback`, with §10.2 rule 4's reason."""
    rig = await build(tmp_path)
    first = await rig.turn(None, "a1", "u1")
    await rig.settle(first, "e-u1", "e-a1")

    await rig.turn("a9", "a2", "u2")
    lines = list(rig.service.store.journal.replay(FAMILY, CHAT_SESSION))
    fallbacks = [one for one in lines if one.kind is LineKind.BRANCH_FALLBACK]

    assert fallbacks[-1].body["reason"] == UNMAPPED_PARENT
    assert "branch" not in rig.starts()[-1]
    await rig.stop()


async def test_a_refused_fork_runs_the_prompt_anyway(tmp_path: Path) -> None:
    """Contract 03 §4.1 and §10.2's last paragraph. The chat stays usable."""
    rig = await build(tmp_path)
    first = await rig.turn(None, "a1", "u1")
    await rig.settle(first, "e-u1", "e-a1")
    second = await rig.turn("a1", "a2", "u2")
    await rig.settle(second, "e-u2", "e-a2")
    third = await rig.turn("a1", "a3", "u3")

    await rig.fleet.playpen().fail(
        CHAT_SESSION, third, PlaypenReason.FORK_REFUSED.value, "entry is not on the branch"
    )
    await rig.fleet.playpen().next_start()
    live = rig.service.live_turn(FAMILY, CHAT_SESSION, third)
    lines = list(rig.service.store.journal.replay(FAMILY, CHAT_SESSION))
    fallbacks = [one for one in lines if one.kind is LineKind.BRANCH_FALLBACK]

    assert live is not None
    assert live.record.state is TurnState.RUNNING
    assert fallbacks[-1].body["wanted_entry"] == "a1"
    assert fallbacks[-1].body["reason"] == "entry is not on the branch"
    # The retry runs the same turn with no branch at all.
    assert "branch" not in rig.starts()[-1]
    assert rig.starts()[-1]["turn"] == third
    await rig.stop()
