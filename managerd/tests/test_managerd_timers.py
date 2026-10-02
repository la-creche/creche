"""An autonomous family's triggers as systemd user timers.

Two things must hold. The `OnCalendar` line must mean what the cron line
meant, or no unit is written at all — a timer that fires on the wrong days
is worse than one that never fires, because nobody looks for it. And a
removed trigger must stop firing before any other timer of that family is
rewritten."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_managerd.timers import (
    CronShapeError,
    FakeSystemctl,
    FakeUnits,
    UserUnits,
    apply_timers,
    cron_to_oncalendar,
    remove_timers,
    timer_text,
)
from managerd_helpers import chat_family

HOURLY: str = "0 * * * *"
WEEKDAYS: str = "0 9 * * 1-5"


def autonomous(**overrides: object) -> object:
    body: dict[str, object] = {
        "kind": "autonomous",
        "triggers": [{"cron": HOURLY}],
    }
    body.update(overrides)
    return chat_family(**body)


# --- cron to OnCalendar -----------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "calendar"),
    [
        ("@hourly", "hourly"),
        ("@daily", "daily"),
        ("@weekly", "weekly"),
        ("0 * * * *", "*-*-* *:00:00"),
        ("*/15 * * * *", "*-*-* *:00/15:00"),
        ("0 */6 * * *", "*-*-* 00/6:00:00"),
        ("0 9 * * 1-5", "Mon..Fri *-*-* 09:00:00"),
        ("30 4 1 * *", "*-*-01 04:30:00"),
        ("0 3 1 6 *", "*-06-01 03:00:00"),
        ("15 2 * * 0", "Sun *-*-* 02:15:00"),
    ],
)
def test_it_spells_a_cron_line_as_a_calendar_line(expression: str, calendar: str) -> None:
    assert cron_to_oncalendar(expression) == calendar


@pytest.mark.parametrize(
    "expression",
    [
        "0 9 1 * 1",  # day-of-month and day-of-week at once
        "0 9 * *",  # four fields
        "99 9 * * *",  # minute out of range
        "0 9 * * 8",  # no such weekday
        "0 9 * * 6-1",  # a range that runs backwards Monday-first
    ],
)
def test_a_shape_systemd_cannot_mean_is_refused(expression: str) -> None:
    with pytest.raises(CronShapeError):
        cron_to_oncalendar(expression)


# --- the unit text ------------------------------------------------------------------


def test_the_timer_points_at_the_one_template_service() -> None:
    units = FakeUnits()
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    text = units.files["agent-trigger-chat-t1.timer"]
    assert "Unit=agent-trigger@chat.service" in text
    assert "OnCalendar=*-*-* *:00:00" in text


def test_the_timer_says_not_to_hand_edit_it() -> None:
    units = FakeUnits()
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    assert "Do not hand-edit" in units.files["agent-trigger-chat-t1.timer"]


def test_a_missed_firing_is_caught_up_after_a_reboot() -> None:
    units = FakeUnits()
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    assert "Persistent=true" in units.files["agent-trigger-chat-t1.timer"]


# --- converging the set ---------------------------------------------------------------


def test_one_timer_per_cron_trigger() -> None:
    units = FakeUnits()
    apply_timers(autonomous(triggers=[{"cron": HOURLY}, {"cron": WEEKDAYS}]), units)  # type: ignore[arg-type]
    assert units.names() == ("agent-trigger-chat-t1.timer", "agent-trigger-chat-t2.timer")


def test_a_webhook_trigger_gets_no_timer() -> None:
    """A door fires a webhook. Nothing schedules it."""
    units = FakeUnits()
    apply_timers(autonomous(triggers=[{"webhook": "deploy-done"}]), units)  # type: ignore[arg-type]
    assert units.names() == ()


def test_a_changed_schedule_rewrites_the_same_unit() -> None:
    units = FakeUnits()
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    apply_timers(autonomous(triggers=[{"cron": WEEKDAYS}]), units)  # type: ignore[arg-type]
    assert units.names() == ("agent-trigger-chat-t1.timer",)
    assert "Mon..Fri" in units.files["agent-trigger-chat-t1.timer"]


def test_a_removed_trigger_removes_its_unit() -> None:
    units = FakeUnits()
    apply_timers(autonomous(triggers=[{"cron": HOURLY}, {"cron": WEEKDAYS}]), units)  # type: ignore[arg-type]
    apply_timers(autonomous(triggers=[{"cron": HOURLY}]), units)  # type: ignore[arg-type]
    assert units.names() == ("agent-trigger-chat-t1.timer",)


def test_a_removal_runs_before_any_write() -> None:
    """The same ordering every other axis keeps: an interruption between
    the halves leaves fewer firings than the target, never more."""
    units = FakeUnits()
    apply_timers(autonomous(triggers=[{"cron": HOURLY}, {"cron": WEEKDAYS}]), units)  # type: ignore[arg-type]
    units.calls.clear()
    apply_timers(autonomous(triggers=[{"cron": WEEKDAYS}]), units)  # type: ignore[arg-type]
    assert units.calls == [
        "remove agent-trigger-chat-t2.timer",
        "write agent-trigger-chat-t1.timer",
    ]


def test_an_unchanged_set_reports_no_change() -> None:
    units = FakeUnits()
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    assert apply_timers(autonomous(), units).changed is False  # type: ignore[arg-type]


def test_an_attended_family_has_no_timers() -> None:
    units = FakeUnits()
    units.write("agent-trigger-chat-t1.timer", "stale")
    apply_timers(chat_family(), units)  # type: ignore[arg-type]
    assert units.names() == ()


def test_another_familys_timer_is_never_touched() -> None:
    units = FakeUnits()
    units.write("agent-trigger-ops-t1.timer", "not mine")
    apply_timers(chat_family(), units)  # type: ignore[arg-type]
    assert units.names() == ("agent-trigger-ops-t1.timer",)


def test_a_deleted_family_loses_every_timer() -> None:
    units = FakeUnits()
    apply_timers(autonomous(triggers=[{"cron": HOURLY}, {"cron": WEEKDAYS}]), units)  # type: ignore[arg-type]
    units.write("agent-trigger-ops-t1.timer", "not mine")
    assert remove_timers("chat", units) == (
        "agent-trigger-chat-t1.timer",
        "agent-trigger-chat-t2.timer",
    )
    assert units.names() == ("agent-trigger-ops-t1.timer",)


def test_an_unspellable_trigger_is_reported_and_written_nowhere() -> None:
    units = FakeUnits()
    outcome = apply_timers(autonomous(triggers=[{"cron": "0 9 1 * 1"}]), units)  # type: ignore[arg-type]
    assert outcome.refused == ("0 9 1 * 1",)
    assert units.names() == ()


def test_one_unspellable_trigger_does_not_lose_the_others() -> None:
    units = FakeUnits()
    triggers = [{"cron": HOURLY}, {"cron": "0 9 1 * 1"}, {"cron": WEEKDAYS}]
    outcome = apply_timers(autonomous(triggers=triggers), units)  # type: ignore[arg-type]
    assert len(outcome.refused) == 1
    assert units.names() == ("agent-trigger-chat-t1.timer", "agent-trigger-chat-t3.timer")


# --- the ENABLED state, not only the file ------------------------------------------------
#
# `UserUnits.write` writes the
# unit file, runs `daemon-reload`, then `enable --now`, and `apply_timers`
# rewrites only on a content change. A kill between the file and the
# enable leaves the file there, unchanged, and disabled for ever: the
# schedule silently never fires and no fault says so.


def test_a_timer_the_file_matches_but_nothing_enabled_is_enabled(tmp_path: Path) -> None:
    """The kill this whole section exists for. The content check can never
    rewrite it, so only a probe of the enabled state can recover it."""
    units = FakeUnits()
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    units.enabled.clear()  # the kill: file written, `enable --now` never ran
    units.calls.clear()

    outcome = apply_timers(autonomous(), units)  # type: ignore[arg-type]

    assert outcome.enabled == ("agent-trigger-chat-t1.timer",)
    assert outcome.changed is True
    assert units.calls == ["enable agent-trigger-chat-t1.timer"]


def test_an_enabled_timer_is_not_enabled_again(tmp_path: Path) -> None:
    """The loop looks every two seconds. Re-enabling an enabled unit on
    every tick would be one systemctl call per timer per tick."""
    units = FakeUnits()
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    units.calls.clear()

    outcome = apply_timers(autonomous(), units)  # type: ignore[arg-type]

    assert outcome.enabled == ()
    assert outcome.changed is False
    assert units.calls == []


def test_a_timer_that_will_not_enable_is_reported(tmp_path: Path) -> None:
    units = FakeUnits()
    units.refuse.add("agent-trigger-chat-t1.timer")

    outcome = apply_timers(autonomous(), units)  # type: ignore[arg-type]

    assert outcome.unenabled == ("agent-trigger-chat-t1.timer",)
    assert outcome.enabled == ()


def test_a_family_with_no_cron_line_enables_nothing(tmp_path: Path) -> None:
    """A commented-out `cron` in the registry generates no timer at all,
    and this pass must not invent one to enable."""
    units = FakeUnits()

    outcome = apply_timers(autonomous(triggers=[{"webhook": "deploy-done"}]), units)  # type: ignore[arg-type]

    assert outcome.enabled == ()
    assert outcome.unenabled == ()
    assert units.calls == []


def test_an_unspellable_cron_line_enables_nothing(tmp_path: Path) -> None:
    units = FakeUnits()

    outcome = apply_timers(autonomous(triggers=[{"cron": "0 9 1 * 1"}]), units)  # type: ignore[arg-type]

    assert outcome.refused == ("0 9 1 * 1",)
    assert outcome.enabled == ()
    assert outcome.unenabled == ()


def test_the_probe_and_the_enable_are_the_real_systemctl_argv(tmp_path: Path) -> None:
    """No real systemd in the room: the writer's own argv is what a reader
    has to be able to check."""
    systemctl = FakeSystemctl()
    units = UserUnits(tmp_path, systemctl)
    # The kill: the exact file this family wants, on disk, never enabled.
    (tmp_path / "agent-trigger-chat-t1.timer").write_text(
        timer_text("chat", cron_to_oncalendar(HOURLY)), encoding="utf-8"
    )
    systemctl.calls.clear()

    apply_timers(autonomous(), units)  # type: ignore[arg-type]

    assert systemctl.calls == [
        "is-enabled agent-trigger-chat-t1.timer",
        "enable --now agent-trigger-chat-t1.timer",
        "is-enabled agent-trigger-chat-t1.timer",
    ]


def test_the_writer_reads_systemds_own_answer(tmp_path: Path) -> None:
    """`systemctl is-enabled` exits non-zero to say `disabled`, so the
    answer is stdout, never the exit code."""
    systemctl = FakeSystemctl()
    units = UserUnits(tmp_path, systemctl)
    assert units.is_enabled("agent-trigger-chat-t1.timer") is False

    units.enable("agent-trigger-chat-t1.timer")
    assert units.is_enabled("agent-trigger-chat-t1.timer") is True


# --- the real writer, without systemctl ------------------------------------------------


def test_the_user_writer_puts_a_unit_where_systemd_reads_it(tmp_path: Path) -> None:
    units = UserUnits(tmp_path / "systemd" / "user", FakeSystemctl())
    units.write("agent-trigger-chat-t1.timer", "text\n")
    assert (tmp_path / "systemd" / "user" / "agent-trigger-chat-t1.timer").exists()


def test_a_written_unit_is_enabled(tmp_path: Path) -> None:
    """A timer file nothing enabled never fires, and nothing complains."""
    systemctl = FakeSystemctl()
    UserUnits(tmp_path, systemctl).write("agent-trigger-chat-t1.timer", "text\n")
    assert systemctl.calls == ["daemon-reload", "enable --now agent-trigger-chat-t1.timer"]


def test_an_unchanged_pass_writes_nothing(tmp_path: Path) -> None:
    """The loop looks every two seconds. Reloading and re-enabling an
    unchanged unit on every tick would be two systemctl calls per family
    per tick, for nothing. One `is-enabled` probe per generated timer is
    what is left, and it is what recovers a kill between the two."""
    systemctl = FakeSystemctl()
    units = UserUnits(tmp_path, systemctl)
    apply_timers(autonomous(), units)  # type: ignore[arg-type]
    systemctl.calls.clear()

    outcome = apply_timers(autonomous(), units)  # type: ignore[arg-type]
    assert outcome.changed is False
    assert systemctl.calls == ["is-enabled agent-trigger-chat-t1.timer"]


def test_a_removed_unit_is_disabled_before_it_is_unlinked(tmp_path: Path) -> None:
    """Unlinking an enabled unit leaves a dangling symlink in
    `timers.target.wants`."""
    systemctl = FakeSystemctl()
    units = UserUnits(tmp_path, systemctl)
    units.write("agent-trigger-chat-t1.timer", "text\n")
    systemctl.calls.clear()
    units.remove("agent-trigger-chat-t1.timer")
    assert systemctl.calls[0] == "disable --now agent-trigger-chat-t1.timer"
    assert units.names() == ()


def test_the_user_writer_lists_only_its_own_units(tmp_path: Path) -> None:
    root = tmp_path / "systemd" / "user"
    root.mkdir(parents=True)
    (root / "agent-trigger-chat-t1.timer").write_text("mine\n", encoding="utf-8")
    (root / "shim.service").write_text("someone else's\n", encoding="utf-8")
    assert UserUnits(root, FakeSystemctl()).names() == ("agent-trigger-chat-t1.timer",)
