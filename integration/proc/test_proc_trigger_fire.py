"""The timer command of the trigger door: `agent-trigger fire <family>`.

`creche-door-trigger@.service` runs it once for each tick of a generated timer.
A test is the clock here: it runs the command to its end, then reads what
`attendance` did with the firing.

The scenarios that `integration/tests/test_i5_stage5.py` also has keep the
names of that file. That suite calls a function of the door inside the test
process. This one starts the program.

CONTRACT-QUESTION: no contract names the exit codes of the command. Reading
taken: 0 for a firing that `attendance` accepted, and for a firing that the
quiet check skipped (contract 01 §3.15). Any other code is a firing that
started nothing. A change to fixed codes costs one assertion per scenario.
"""

from __future__ import annotations

import re
import signal
from typing import Any

import pytest
from proc_chat import until
from proc_services import Service
from proc_tree import Tree, replace_secret, token_of
from proc_trigger import CHAT, FIRE, ORACLE, REVIEW, TriggerStack, fire_env

#: Contract 02 §13.1: the times of an outcome record are RFC 3339 in UTC.
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")

#: A job of the fixture family is one short turn of the pi stand-in, and it
#: crosses four processes on a loaded machine.
JOB_DEADLINE_S = 60.0

#: `ha-review` runs one turn at a time (contract 01 §3.14), so three firings
#: are one running turn and two that wait (contract 02 §13 rules 2 and 3).
FIRES_AT_ONCE = 3

#: Under the 32-byte floor of contract 02 §3 rule 7. Its text is one a test
#: can find in an output.
SHORT_SECRET = "short-trigger-token"

CHECK_FLAG = "--check"
NO_FAMILY = "no-such-family"

#: What a person asks the attended family while the queue is full.
ATTENDED_PROMPT = "is the heating on"

#: `creche-attendance.service`: a stop takes less than its `TimeoutStopSec`.
STOP_DEADLINE_S = 30.0


async def ended_jobs(tree: Tree, family: str, count: int = 1) -> list[dict[str, Any]]:
    """Wait until one family has `count` outcome records, then return them.

    The id of a record is a ULID, so the order of the ids is the order in
    which the jobs ended.
    """
    await until(
        lambda: len(tree.outcomes(family)) >= count,
        f"{count} outcome records of {family}",
        JOB_DEADLINE_S,
    )

    return sorted(tree.outcomes(family), key=lambda record: str(record["id"]))


async def test_a_trigger_for_an_attended_family_is_refused(timer: TriggerStack) -> None:
    """Contract 02 §3.1. The `door-trigger` token reaches an autonomous family only."""
    refused_fire(timer, CHAT)


async def test_a_trigger_for_a_thin_family_is_refused(timer: TriggerStack) -> None:
    """The same rule from the other side: a thin family runs a job for a delegation only."""
    refused_fire(timer, ORACLE)


async def test_a_trigger_for_an_unknown_family_is_refused(timer: TriggerStack) -> None:
    """A family with no status document. The old suite has no such scenario."""
    refused_fire(timer, NO_FAMILY)


async def test_a_fire_with_attendance_down_starts_nothing(trigger_prepared: TriggerStack) -> None:
    """The socket does not answer. The command fails, and the next tick is the retry."""
    fired = trigger_prepared.fire(REVIEW)

    assert fired.exit_code != 0
    assert trigger_prepared.tree.outcomes(REVIEW) == []


def test_the_fire_check_validates_and_calls_nothing(trigger_prepared: TriggerStack) -> None:
    """`--check` reads the config and exits 0. No `attendance` runs here."""
    tree = trigger_prepared.tree

    checked = trigger_prepared.fire(REVIEW, CHECK_FLAG)

    assert checked.exit_code == 0, checked.stderr
    assert token_of("door-trigger") not in checked.stdout + checked.stderr
    assert tree.sessions_of(REVIEW) == []


@pytest.mark.parametrize("content", [None, "", SHORT_SECRET], ids=["missing", "empty", "short"])
async def test_a_fire_refuses_a_bad_token_file(timer: TriggerStack, content: str | None) -> None:
    """Contract 02 §3 rule 7, as the door applies it to its own token. Fail closed."""
    tree = timer.tree
    replace_secret(tree.token_file("door-trigger"), content)

    fired = timer.fire(REVIEW)

    assert fired.exit_code != 0
    assert SHORT_SECRET not in fired.stdout + fired.stderr
    assert tree.sessions_of(REVIEW) == []
    assert tree.outcomes(REVIEW) == []


def test_a_fire_with_no_family_is_a_usage_error(trigger_prepared: TriggerStack) -> None:
    """The unit gives the family as `%i`. A command with none starts nothing."""
    finished = trigger_prepared.run(Service.DOOR_TRIGGER, fire_env(trigger_prepared.tree), FIRE)

    assert finished.exit_code != 0


def refused_fire(timer: TriggerStack, family: str) -> None:
    """Fire for a family that the door must refuse. Nothing starts, and no secret shows."""
    tree = timer.tree

    fired = timer.fire(family)

    assert fired.exit_code != 0
    assert tree.outcomes(family) == []
    assert not (tree.sessions_root / family).is_dir() or tree.sessions_of(family) == []
    assert token_of("door-trigger") not in fired.stdout + fired.stderr


def restart_attendance(stack: TriggerStack) -> None:
    """Stop `attendance` with SIGTERM, as a stop of its unit does, and start it again."""
    assert stack.attendance is not None
    stack.attendance.send(signal.SIGTERM)
    stack.attendance.wait(STOP_DEADLINE_S)
    stack.spawn_attendance()
    stack.await_attendance()
