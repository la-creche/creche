"""The writer lease this terminal holds while pi runs (contract 02 §7).

One writer per session. The TUI takes the lease before pi writes a byte,
renews it while the terminal runs, and gives it up on the way out. Contention
refuses: it never queues and never steals.
"""

from __future__ import annotations

import pytest
from agent_door_tui.attendance import AttendanceError, Takeover
from agent_door_tui.errors import DoorError, Exit
from agent_door_tui.lease import LEASE_TTL_S, RENEW_INTERVAL_S, WriterLease
from fake_tui_attendance import FAMILY, FakeAttendance

SESSION = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR"
INSTANCE = "tui.4242"


def lease_of(door: FakeAttendance) -> WriterLease:
    return WriterLease(door, FAMILY, SESSION, INSTANCE)


def test_taking_the_lease_calls_the_writer_operation() -> None:
    door = FakeAttendance()

    lease_of(door).take()

    assert door.calls == [f"writer {FAMILY}/{SESSION}"]
    assert door.holds(FAMILY, SESSION, INSTANCE)


def test_a_held_lease_refuses_and_never_steals() -> None:
    """Contract 02 §7.2, §7.3 rule 6. Another terminal is the refusal."""
    door = FakeAttendance()
    door.hold(FAMILY, SESSION, "tui")

    with pytest.raises(DoorError) as caught:
        lease_of(door).take()

    assert caught.value.code is Exit.SESSION_BUSY
    assert "tui" in caught.value.message
    # Never a second attempt with `force`: the default never steals.
    assert door.calls == [f"writer {FAMILY}/{SESSION}"]
    assert door.forced == []


def test_another_doors_idle_lease_passes(door_instance: str = INSTANCE) -> None:
    """Contract 02 §7.3 rule 5.

    A chat that settled seconds ago is the normal case when a chat moves to
    the terminal, not a collision.
    """
    door = FakeAttendance()
    door.hold(FAMILY, SESSION, "owui")

    lease_of(door).take()

    assert door.holds(FAMILY, SESSION, door_instance)
    assert door.forced == []


def test_an_idle_holder_is_named_with_the_way_out() -> None:
    """An idle lease is not contention. The message says what would take it."""
    door = FakeAttendance()
    door.hold(FAMILY, SESSION, "tui")

    with pytest.raises(DoorError) as caught:
        lease_of(door).take()

    assert "--force" in caught.value.message


def test_a_running_turn_is_never_offered_force() -> None:
    """Contract 02 §7.3: `force` never interrupts a running turn."""
    door = FakeAttendance()
    door.hold(FAMILY, SESSION, "owui", turn="01JBTURN")

    with pytest.raises(DoorError) as caught:
        lease_of(door).take()

    assert "--force" not in caught.value.message
    assert "01JBTURN" in caught.value.message


def test_force_takes_another_terminals_idle_lease() -> None:
    """Contract 02 §7.3 rule 6. A deliberate act, never the default."""
    door = FakeAttendance()
    door.hold(FAMILY, SESSION, "tui")

    lease_of(door).take(Takeover.FORCE)

    assert door.holds(FAMILY, SESSION, INSTANCE)
    assert door.forced == [SESSION]


def test_force_is_still_refused_while_a_turn_runs() -> None:
    """`attendance` decides. The door cannot talk it into interrupting a turn."""
    door = FakeAttendance()
    door.hold(FAMILY, SESSION, "owui", turn="01JBTURN")

    with pytest.raises(DoorError) as caught:
        lease_of(door).take(Takeover.FORCE)

    assert caught.value.code is Exit.SESSION_BUSY


def test_a_renewal_never_forces() -> None:
    """A renewal that forced would steal a lease this door had already lost."""
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take(Takeover.FORCE)
    door.forced.clear()

    lease.renew_once()

    assert door.forced == []


def test_renewal_repeats_the_writer_call_under_the_ttl() -> None:
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()

    lease.renew_once()
    lease.renew_once()

    assert door.writer_calls(FAMILY, SESSION) == 3


def test_the_renewal_interval_leaves_room_for_two_misses() -> None:
    """Contract 02 §7.1's TTL is 60 s and names no interval."""
    assert RENEW_INTERVAL_S * 3 <= LEASE_TTL_S


def test_the_renewal_thread_stops_when_the_lease_is_released() -> None:
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()
    lease.start_renewal()

    lease.release()

    assert not lease.renewing
    before = door.writer_calls(FAMILY, SESSION)
    lease.renew_once()
    assert door.writer_calls(FAMILY, SESSION) == before, "a released lease is never renewed"


def test_release_hands_the_lease_back_at_once() -> None:
    """Contract 02 §5.10. No 60-second wait."""
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()

    lease.release()

    assert door.calls[-1] == f"release-writer {FAMILY}/{SESSION}"
    assert not door.holds(FAMILY, SESSION, INSTANCE)
    assert "released" in lease.release_line()


def test_release_is_safe_to_call_twice() -> None:
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()

    lease.release()
    lease.release()

    assert not lease.renewing
    assert door.calls.count(f"release-writer {FAMILY}/{SESSION}") == 1


def test_a_failed_release_never_raises() -> None:
    """It runs on the way out, often from a signal handler."""
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()
    door.fail_with = AttendanceError("unreachable", "attendance did not answer", 0)

    lease.release()

    assert not lease.held


def test_a_renew_that_lost_the_lease_stops_renewing() -> None:
    """Contract 02 §7.4. Asking again every 20 seconds would fight a human."""
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()
    door.hold(FAMILY, SESSION, "owui")

    lease.renew_once()

    assert lease.renewal_failures == 1
    assert not lease.held
    before = len(door.calls)
    lease.release()
    assert len(door.calls) == before, "a lost lease is never released"


def test_releasing_a_lease_never_taken_does_nothing() -> None:
    door = FakeAttendance()

    lease_of(door).release()

    assert door.calls == []


def test_a_renewal_that_fails_does_not_end_the_terminal() -> None:
    """A human is typing. A lost renewal is a warning, never a kill."""
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()
    door.fail_with = AttendanceError("unreachable", "attendance did not answer", 0)

    lease.renew_once()

    assert lease.renewal_failures == 1


def test_a_failed_take_leaves_nothing_to_release() -> None:
    door = FakeAttendance()
    door.fail_with = AttendanceError("unreachable", "attendance did not answer", 0)
    lease = lease_of(door)

    with pytest.raises(DoorError) as caught:
        lease.take()

    assert caught.value.code is Exit.ATTENDANCE
    lease.release()
    assert not lease.renewing


def test_the_renewal_thread_runs_and_joins() -> None:
    """The thread outlives nothing: `release` joins it before the door exits."""
    door = FakeAttendance()
    lease = lease_of(door)
    lease.take()
    lease.start_renewal()

    assert lease.renewing
    lease.release()

    assert not lease.renewing
