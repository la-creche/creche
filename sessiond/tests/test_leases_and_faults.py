"""The writer lease (contract 02 §7) and the fault file (contract 05 §3.3.1)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from agent_sessiond.errors import ApiError, ErrorCode
from agent_sessiond.faults import FAULT_SOURCE, FaultCode, FaultReporter, blocks_turns
from agent_sessiond.leases import Intent, LeaseBook, LeaseReason, Takeover, TurnActivity
from agent_sessiond.models import Holder
from agent_sessiond.paths import fault_file, lease_file

_FAMILY = "chat"
_SESSION = "owui-3f2a"
_EXPIRED_TTL_S = 0


def test_first_writer_gets_the_lease(tmp_path: Path) -> None:
    book = LeaseBook(tmp_path)
    grant = book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    assert grant.lease.holder is Holder.OWUI
    assert grant.reason is LeaseReason.GRANTED
    assert book.get(_FAMILY, _SESSION) is not None


def test_an_active_session_refuses_a_second_writer(tmp_path: Path) -> None:
    """Contract 02 §7.3 rule 4. An active session refuses every door."""
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    with pytest.raises(ApiError) as caught:
        book.take(_FAMILY, _SESSION, Holder.TUI, "tui-1", activity=TurnActivity.ACTIVE)

    error = caught.value
    assert error.code is ErrorCode.SESSION_BUSY
    assert error.detail is not None
    assert error.detail["holder"] == "owui"
    assert "since" in error.detail
    assert "expires_at" in error.detail
    # The holder block names the door, never which process it was.
    assert "door_instance" not in error.detail


def test_an_idle_lease_passes_to_another_door(tmp_path: Path) -> None:
    """Contract 02 §7.3 rule 5.

    No turn runs, so nothing is lost, and invariant 3's "continue it in the
    terminal" stops meaning "wait 60 seconds first".
    """
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    grant = book.take(_FAMILY, _SESSION, Holder.TUI, "tui.4021")

    assert grant.lease.holder is Holder.TUI
    assert grant.reason is LeaseReason.TAKEN_OVER


def test_another_terminal_needs_force(tmp_path: Path) -> None:
    """Contract 02 §7.3 rule 6. `force` means only this one case."""
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.TUI, "tui.4021")

    with pytest.raises(ApiError) as caught:
        book.take(_FAMILY, _SESSION, Holder.TUI, "tui.4022")

    assert caught.value.code is ErrorCode.SESSION_BUSY

    grant = book.take(_FAMILY, _SESSION, Holder.TUI, "tui.4022", takeover=Takeover.FORCE)

    assert grant.reason is LeaseReason.TAKEN_OVER


def test_the_same_door_instance_renews(tmp_path: Path) -> None:
    book = LeaseBook(tmp_path)
    first = book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")
    second = book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1", intent=Intent.RENEW)

    assert second.lease.holder is first.lease.holder
    assert second.reason is LeaseReason.RENEWED


def test_a_renew_never_retakes_a_lost_lease(tmp_path: Path) -> None:
    """Contract 02 §7.4. Read as an acquire, the two doors would trade it."""
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")
    book.take(_FAMILY, _SESSION, Holder.TUI, "tui.4021")

    with pytest.raises(ApiError) as caught:
        book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1", intent=Intent.RENEW)

    error = caught.value
    assert error.code is ErrorCode.LEASE_TAKEN_OVER
    assert error.detail is not None
    assert error.detail["holder"] == "tui"


def test_a_renew_of_an_expired_lease_is_taken_over(tmp_path: Path) -> None:
    book = LeaseBook(tmp_path, ttl_s=_EXPIRED_TTL_S)
    book.take(_FAMILY, _SESSION, Holder.TUI, "tui.4021")

    with pytest.raises(ApiError) as caught:
        book.take(_FAMILY, _SESSION, Holder.TUI, "tui.4021", intent=Intent.RENEW)

    assert caught.value.code is ErrorCode.LEASE_TAKEN_OVER


def test_an_expired_lease_is_free(tmp_path: Path) -> None:
    book = LeaseBook(tmp_path, ttl_s=_EXPIRED_TTL_S)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    assert book.get(_FAMILY, _SESSION) is None
    assert book.take(_FAMILY, _SESSION, Holder.TUI, "tui-1").lease.holder is Holder.TUI


def test_force_never_interrupts_a_running_turn(tmp_path: Path) -> None:
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    with pytest.raises(ApiError):
        book.take(
            _FAMILY,
            _SESSION,
            Holder.TUI,
            "tui-1",
            takeover=Takeover.FORCE,
            activity=TurnActivity.ACTIVE,
        )

    taken = book.take(
        _FAMILY,
        _SESSION,
        Holder.TUI,
        "tui-1",
        takeover=Takeover.FORCE,
        activity=TurnActivity.IDLE,
    )
    assert taken.lease.holder is Holder.TUI


def test_the_mirror_names_the_holder(tmp_path: Path) -> None:
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    mirrored = json.loads(lease_file(tmp_path, _FAMILY, _SESSION).read_text())

    assert mirrored["holder"] == "owui"
    assert mirrored["session"] == _SESSION
    assert mirrored["released"] is False


def test_release_and_forget(tmp_path: Path) -> None:
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    assert book.release(_FAMILY, _SESSION, Holder.OWUI, "owui-1") is not None
    assert book.get(_FAMILY, _SESSION) is None
    # Releasing twice is not an error: a door on the way out never asks first.
    assert book.release(_FAMILY, _SESSION, Holder.OWUI, "owui-1") is None

    book.forget(_FAMILY, _SESSION)
    assert not lease_file(tmp_path, _FAMILY, _SESSION).exists()


def test_only_the_holder_releases(tmp_path: Path) -> None:
    """Contract 02 §5.10 rule 1."""
    book = LeaseBook(tmp_path)
    book.take(_FAMILY, _SESSION, Holder.OWUI, "owui-1")

    with pytest.raises(ApiError) as caught:
        book.release(_FAMILY, _SESSION, Holder.TUI, "tui.4021")

    assert caught.value.code is ErrorCode.SESSION_BUSY
    assert book.get(_FAMILY, _SESSION) is not None


def test_fault_file_shape_and_mode(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.ORPHAN_PROCESSES, "foreign_pi_processes=2", "chat-s3")

    path = fault_file(tmp_path, _FAMILY)
    payload = json.loads(path.read_text())

    assert payload["family"] == _FAMILY
    assert payload["source"] == FAULT_SOURCE
    assert len(payload["faults"]) == 1

    entry = payload["faults"][0]
    assert entry["code"] == "orphan_processes"
    assert entry["blocks_turns"] is False
    assert entry["source"] == FAULT_SOURCE
    assert entry["sandbox"] == "chat-s3"

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_blocks_turns_follows_the_contract_table() -> None:
    assert blocks_turns(FaultCode.PROTOCOL_MISMATCH) is True
    assert blocks_turns(FaultCode.PROTOCOL_VIOLATION) is True
    assert blocks_turns(FaultCode.ORPHAN_PROCESSES) is False


def test_an_empty_list_clears_the_faults(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.PROTOCOL_VIOLATION, "refusal budget")
    reporter.clear(_FAMILY, FaultCode.PROTOCOL_VIOLATION)

    payload = json.loads(fault_file(tmp_path, _FAMILY).read_text())

    assert payload["faults"] == []


def test_since_keeps_the_first_sighting(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.PROTOCOL_VIOLATION, "first")
    first = reporter.open_faults(_FAMILY)[0].since

    reporter.raise_fault(_FAMILY, FaultCode.PROTOCOL_VIOLATION, "second")
    again = reporter.open_faults(_FAMILY)[0]

    assert again.since == first
    assert again.message == "second"


def test_clear_all_empties_the_file(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.PROTOCOL_MISMATCH, "major 2 against 1")
    reporter.raise_fault(_FAMILY, FaultCode.ORPHAN_PROCESSES)
    reporter.clear_all(_FAMILY)

    payload = json.loads(fault_file(tmp_path, _FAMILY).read_text())

    assert payload["faults"] == []
    assert reporter.open_faults(_FAMILY) == []
