"""The quiet check of contract 01 §3.15, through the timer command.

A cron firing of a family whose file holds `quiet` runs a check first. When
the check finds no change since the last good wake, the firing starts no
session. Each scenario runs `agent-trigger fire` as a timer does, then reads
what `attendance` got.

The check keeps its own record at `state/triggers/quiet/<family>.json`
(`docs/rework/spec.md` §10.2), and it reads the outcome records that
`attendance` writes. So one scenario is two firings or more.

The family of this fixture names no board and holds no `enqueue`, so the
check calls no chaperone, and no chaperone runs here. The two wake reasons
that need one have no scenario (`AGENTS.md`, Known gaps).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from proc_chat import until
from proc_harness import Finished, Supervisor
from proc_standins import set_pi_env
from proc_tree import Tree, append_audit, audit_record
from proc_trigger import HELD_TURN, REVIEW, TriggerStack, hook_path

#: Contract 01 §3.15: the default floor. No scenario here runs that long, so
#: the floor never is the reason for a wake.
QUIET = "{ floor_hours: 24 }"

#: One call owed each day from hour 0 in UTC: owed all day, until the audit
#: holds the call. `embed` is the one verb the fixture family holds.
DAILY_CALL = "embed"
QUIET_WITH_DAILY = f"{{ daily: {{ call: {DAILY_CALL}, hour: 0, zone: UTC }} }}"

#: `docs/rework/spec.md` §11.6: what a skipped firing answers.
QUIET_ANSWER = f"quiet: {REVIEW}: nothing changed since"

JOB_DEADLINE_S = 60.0

#: The pi stand-in ends in its first delta, so the turn fails.
FAILED_TURN = {"die_at": 1}

FORCE_FLAG = "--force"


def _stack(tree: Tree, supervisor: Supervisor, quiet: str) -> TriggerStack:
    """`attendance`, and the autonomous family with a `quiet` mapping in its file."""
    stack = TriggerStack(tree, supervisor)
    stack.prepare(quiet=quiet)
    stack.spawn_attendance()
    stack.await_attendance()

    return stack


@pytest.fixture
def quiet(tree: Tree, supervisor: Supervisor, bundle: Path) -> TriggerStack:
    return _stack(tree, supervisor, QUIET)


async def good_wake(stack: TriggerStack) -> None:
    """One firing that wakes the family, and its job ended `ok`."""
    before = len(stack.tree.outcomes(REVIEW))
    fired = stack.fire(REVIEW)
    assert fired.exit_code == 0, fired.stderr
    await _jobs(stack.tree, before + 1)
    assert stack.tree.outcomes(REVIEW)[-1]["status"] == "ok"


async def test_the_first_firing_wakes_the_family(quiet: TriggerStack) -> None:
    """Wake reason 1: there is no last good wake."""
    fired = quiet.fire(REVIEW)

    assert fired.exit_code == 0, fired.stderr
    (record,) = await _jobs(quiet.tree, 1)
    assert record["status"] == "ok"
    assert record["trigger"]["kind"] == "timer"


async def test_a_firing_with_nothing_changed_starts_nothing(quiet: TriggerStack) -> None:
    """The check finds no change, so the firing starts no session and no turn."""
    tree = quiet.tree
    await good_wake(quiet)
    await until(lambda: tree.sessions_of(REVIEW) == [], "the session of the first job to go")

    skipped = quiet.fire(REVIEW)

    assert skipped.exit_code == 0, skipped.stderr
    assert QUIET_ANSWER in skipped.stdout
    assert tree.sessions_of(REVIEW) == []
    assert len(tree.outcomes(REVIEW)) == 1


async def test_force_fires_without_the_check(quiet: TriggerStack) -> None:
    """Rule 5: `--force` skips the check."""
    await good_wake(quiet)

    forced = quiet.fire(REVIEW, FORCE_FLAG)

    assert forced.exit_code == 0, forced.stderr
    assert len(await _jobs(quiet.tree, 2)) == 2


async def test_a_webhook_firing_is_never_checked(quiet: TriggerStack) -> None:
    """A webhook firing has a payload, so it starts a job each time."""
    await good_wake(quiet)
    quiet.start_listener()

    async with quiet.automation() as automation:
        reply = await automation.post(hook_path())

    assert reply.status_code == httpx.codes.ACCEPTED, reply.text
    records = await _jobs(quiet.tree, 2)
    assert records[-1]["trigger"]["kind"] == "webhook"


async def test_a_firing_while_a_wake_is_live_is_skipped(quiet: TriggerStack) -> None:
    """Rule 3: while a wake of the family runs, a second firing starts nothing."""
    tree = quiet.tree
    set_pi_env(tree, **HELD_TURN)
    first = quiet.fire(REVIEW)
    assert first.exit_code == 0, first.stderr
    assert len(tree.sessions_of(REVIEW)) == 1

    second = quiet.fire(REVIEW)

    assert second.exit_code == 0, second.stderr
    assert len(tree.sessions_of(REVIEW)) == 1
    assert tree.outcomes(REVIEW) == []


async def test_a_wake_that_failed_is_no_good_wake(quiet: TriggerStack) -> None:
    """A wake that fails leaves no good wake, so the next firing wakes too."""
    tree = quiet.tree
    set_pi_env(tree, **FAILED_TURN)
    first = quiet.fire(REVIEW)
    assert first.exit_code == 0, first.stderr
    (failed,) = await _jobs(tree, 1)
    assert failed["status"] != "ok"
    await until(lambda: tree.sessions_of(REVIEW) == [], "the session of the failed job to go")

    set_pi_env(tree)
    second = quiet.fire(REVIEW)

    assert second.exit_code == 0, second.stderr
    records = await _jobs(tree, 2)
    assert records[-1]["status"] == "ok"


async def test_a_record_that_does_not_parse_wakes_the_family(quiet: TriggerStack) -> None:
    """Rule 1: a record that the check cannot read wakes the family."""
    tree = quiet.tree
    await good_wake(quiet)
    record = tree.state_root / "triggers" / "quiet" / f"{REVIEW}.json"
    assert record.is_file(), "the check keeps its record where the spec says"
    record.write_text("{ this is not JSON", encoding="utf-8")

    fired = quiet.fire(REVIEW)

    assert fired.exit_code == 0, fired.stderr
    assert len(await _jobs(tree, 2)) == 2


async def test_the_daily_call_wakes_the_family_until_the_audit_holds_it(
    tree: Tree, supervisor: Supervisor, bundle: Path
) -> None:
    """Wake reason 3. The call is owed until the audit holds one that ran.

    The audit is the chaperone's (contract 04 §6). No chaperone runs here, so
    the test writes the record that the chaperone writes for the call.

    The check counts a record from the last midnight in the zone. A new UTC
    day between the record and the firing makes the record one of yesterday,
    so the scenario then writes the record again.
    """
    stack = _stack(tree, supervisor, QUIET_WITH_DAILY)
    await good_wake(stack)

    owed = stack.fire(REVIEW)
    assert owed.exit_code == 0, owed.stderr
    assert len(await _jobs(tree, 2)) == 2, "the call was owed, so the firing must wake"
    await until(lambda: tree.sessions_of(REVIEW) == [], "the session of the second job to go")

    clear = _fire_after_the_call(stack)

    assert clear.exit_code == 0, clear.stderr
    assert QUIET_ANSWER in clear.stdout
    assert len(tree.outcomes(REVIEW)) == 2


def _fire_after_the_call(stack: TriggerStack) -> Finished:
    """Write the audit record of the daily call, then fire, both in one UTC day."""
    while True:
        day = _utc_day()
        append_audit(stack.tree, [audit_record(REVIEW, DAILY_CALL)])
        fired = stack.fire(REVIEW)

        if _utc_day() == day:
            return fired


async def _jobs(tree: Tree, count: int) -> list[dict[str, Any]]:
    """Wait for `count` outcome records of the family. Returns them in end order."""
    await until(
        lambda: len(tree.outcomes(REVIEW)) >= count,
        f"{count} outcome records of {REVIEW}",
        JOB_DEADLINE_S,
    )

    return sorted(tree.outcomes(REVIEW), key=lambda record: str(record["id"]))


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")
