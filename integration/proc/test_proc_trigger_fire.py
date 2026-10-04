"""The timer command of the trigger door: `agent-trigger fire <family>`.

`creche-trigger@.service` runs it once for each tick of a generated timer.
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
from pathlib import Path
from typing import Any

import httpx
import pytest
from proc_chat import SESSIONS_PATH, TURN_STARTED, until
from proc_ids import AUTO_PREFIX
from proc_services import Service
from proc_standins import set_pi_env
from proc_tree import SECRET_MODE, Tree, first_sandbox, token_of
from proc_trigger import CHAT, FIRE, ORACLE, REVIEW, TriggerStack, fire_env

#: Contract 02 §2: a ULID in upper-case Crockford base32.
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

#: Contract 02 §13.1: the times of an outcome record are RFC 3339 in UTC.
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")

#: A job of the fixture family is one short turn of the pi stand-in, and it
#: crosses four processes on a loaded machine.
JOB_DEADLINE_S = 60.0

#: A turn long enough that a second firing finds it running: 250 deltas,
#: 40 ms apart.
HELD_TURN = {"events": 250, "delay_ms": 40}

#: `ha-review` runs one turn at a time (contract 01 §3.14), so three firings
#: are one running turn and two that wait (contract 02 §13 rules 2 and 3).
FIRES_AT_ONCE = 3

#: Under the 32-byte floor of contract 02 §3 rule 7. Its text is one a test
#: can find in an output.
SHORT_SECRET = "short-trigger-token"

CHECK_FLAG = "--check"
NO_FAMILY = "no-such-family"

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


async def test_a_timer_fire_runs_one_job(timer: TriggerStack) -> None:
    """Contract 02 §13 rules 1 and 7. A timer fire is a whole job."""
    fired = timer.fire(REVIEW)

    assert fired.exit_code == 0, fired.stderr

    (record,) = await ended_jobs(timer.tree, REVIEW)
    session = str(record["session"])

    assert session.startswith(AUTO_PREFIX)
    assert ULID.fullmatch(session.removeprefix(AUTO_PREFIX))
    assert record["family"] == REVIEW
    assert record["status"] == "ok"
    assert record["turns"] == 1


async def test_the_job_leaves_no_session_and_no_scratch(timer: TriggerStack) -> None:
    """Invariant 15. A job leaves the record and nothing else."""
    tree = timer.tree
    assert timer.fire(REVIEW).exit_code == 0

    (record,) = await ended_jobs(tree, REVIEW)
    session = str(record["session"])
    # The record is on disk before the session goes (contract 02 §13.1).
    await until(lambda: tree.sessions_of(REVIEW) == [], "the session directory of the job to go")

    async with timer.attendance_client("view-ro") as view:
        gone = await view.get(f"{SESSIONS_PATH}/{REVIEW}/{session}")

    assert not tree.session_dir(session, REVIEW).exists()
    assert gone.status_code == httpx.codes.NOT_FOUND


async def test_the_record_names_the_trigger_that_fired(timer: TriggerStack) -> None:
    """Contract 02 §13.1: `trigger`, `started_at`, `ended_at`, `spend_usd`, `sandbox`."""
    assert timer.fire(REVIEW).exit_code == 0

    (record,) = await ended_jobs(timer.tree, REVIEW)
    assert record["trigger"]["kind"] == "timer"
    # §13.2: a cron firing has no name.
    assert record["trigger"].get("name") is None
    assert RFC3339.fullmatch(str(record["started_at"]))
    assert RFC3339.fullmatch(str(record["ended_at"]))
    assert record["spend_usd"] is None or isinstance(record["spend_usd"], (int, float))
    assert record["sandbox"] == first_sandbox(REVIEW)


@pytest.mark.parametrize("family", [CHAT, ORACLE, NO_FAMILY], ids=["attended", "thin", "unknown"])
async def test_a_trigger_for_another_kind_of_family_is_refused(
    timer: TriggerStack, family: str
) -> None:
    """Contract 02 §3.1. The `door-trigger` token reaches an autonomous family only.

    The old suite has one scenario for the attended family and one for the
    thin family. A family with no status document is the third refusal.
    """
    tree = timer.tree

    fired = timer.fire(family)

    assert fired.exit_code != 0
    assert tree.outcomes(family) == []
    assert not (tree.sessions_root / family).is_dir() or tree.sessions_of(family) == []
    assert token_of("door-trigger") not in fired.stdout + fired.stderr


async def test_three_fires_give_one_running_and_two_queued(timer: TriggerStack) -> None:
    """Contract 02 §13 rules 2 and 3. `max_running_turns: 1` is one at a time."""
    set_pi_env(timer.tree, **HELD_TURN)
    codes = [timer.fire(REVIEW).exit_code for _ in range(FIRES_AT_ONCE)]

    async with timer.attendance_client("view-ro") as view:
        listed = await view.get(SESSIONS_PATH, params={"family": REVIEW})

    states = sorted(str(row["state"]) for row in listed.json()["sessions"])

    assert codes == [0] * FIRES_AT_ONCE
    assert states == ["queued"] * (FIRES_AT_ONCE - 1) + ["running"]
    assert sorted(timer.tree.sessions_of(REVIEW)) == sorted(
        str(row["session"]) for row in listed.json()["sessions"]
    )


async def test_the_queue_runs_in_fire_order(timer: TriggerStack) -> None:
    """Contract 02 §13 rule 3: the queue is first in, first out.

    The door mints the session id at the firing, and a ULID starts with the
    time. So the order of the session ids is the order of the firings.
    """
    codes = [timer.fire(REVIEW).exit_code for _ in range(FIRES_AT_ONCE)]

    records = await ended_jobs(timer.tree, REVIEW, FIRES_AT_ONCE)
    ended = [str(record["session"]) for record in records]

    assert codes == [0] * FIRES_AT_ONCE
    assert [record["status"] for record in records] == ["ok"] * FIRES_AT_ONCE
    assert ended == sorted(ended)


async def test_a_restart_ends_every_queued_turn(timer: TriggerStack) -> None:
    """Contract 02 §13.3. The queue is in memory, so a restart ends it.

    Each queued turn becomes `aborted` with `queue_lost` (§4.3), and its job
    leaves a record that says `cancelled` (§13.1). The old suite makes a
    second service object over the same state. Here the process stops and
    starts, as the unit does it.
    """
    tree = timer.tree
    set_pi_env(tree, **HELD_TURN)
    codes = [timer.fire(REVIEW).exit_code for _ in range(FIRES_AT_ONCE)]
    # The door mints each session id at its firing, so the first id ran.
    queued = sorted(tree.sessions_of(REVIEW))[1:]

    restart_attendance(timer)

    records = await ended_jobs(tree, REVIEW, FIRES_AT_ONCE)
    by_session = {str(record["session"]): record for record in records}

    assert codes == [0] * FIRES_AT_ONCE
    assert len(queued) == FIRES_AT_ONCE - 1
    assert [by_session[session]["status"] for session in queued] == ["cancelled"] * len(queued)
    assert all(not tree.session_dir(session, REVIEW).exists() for session in queued)


async def test_a_restart_sweeps_the_job_it_interrupted(timer: TriggerStack) -> None:
    """The turn that ran at the stop is `failed` (contract 03 §11.4 rule 1), and its job ends."""
    tree = timer.tree
    set_pi_env(tree, **HELD_TURN)
    assert timer.fire(REVIEW).exit_code == 0
    await until(
        lambda: TURN_STARTED in _kinds_of_the_one_session(tree),
        "the turn of the job to start",
        JOB_DEADLINE_S,
    )

    restart_attendance(timer)

    (record,) = await ended_jobs(tree, REVIEW)
    await until(lambda: tree.sessions_of(REVIEW) == [], "the session of the job to go")

    assert record["status"] == "failed"
    assert record["turns"] == 1


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
    _replace(tree.token_file("door-trigger"), content)

    fired = timer.fire(REVIEW)

    assert fired.exit_code != 0
    assert SHORT_SECRET not in fired.stdout + fired.stderr
    assert tree.sessions_of(REVIEW) == []
    assert tree.outcomes(REVIEW) == []


def test_a_fire_with_no_family_is_a_usage_error(trigger_prepared: TriggerStack) -> None:
    """The unit gives the family as `%i`. A command with none starts nothing."""
    finished = trigger_prepared.run(Service.DOOR_TRIGGER, fire_env(trigger_prepared.tree), FIRE)

    assert finished.exit_code != 0


def restart_attendance(stack: TriggerStack) -> None:
    """Stop `attendance` with SIGTERM, as a stop of its unit does, and start it again."""
    assert stack.attendance is not None
    stack.attendance.send(signal.SIGTERM)
    stack.attendance.wait(STOP_DEADLINE_S)
    stack.spawn_attendance()
    stack.await_attendance()


def _kinds_of_the_one_session(tree: Tree) -> list[str]:
    """The journal line kinds of the one session the family has, or none."""
    sessions = tree.sessions_of(REVIEW)

    return tree.journal_kinds(sessions[0], REVIEW) if sessions else []


def _replace(path: Path, content: str | None) -> None:
    """Remove a secret file, or put another in its place by rename."""
    if content is None:
        path.unlink()
        return

    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.chmod(SECRET_MODE)
    temp.replace(path)
