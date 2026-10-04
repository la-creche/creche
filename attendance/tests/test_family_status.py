"""Reading `caregiver`'s status document (contract 05 §2, §3, §4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from attendance.atomic import write_json
from attendance.clock import now, rfc3339
from attendance.errors import ApiError, ErrorCode
from attendance.family_status import (
    FamilyState,
    SandboxState,
    StatusReader,
    check_may_serve,
    served_kind,
)
from attendance.paths import status_file
from attendance.states import SessionKind

_FAMILY = "chat"
_OLD_TIMESTAMP = "2020-01-01T00:00:00Z"


def _document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "family": _FAMILY,
        "kind": "attended",
        "state": "in_sync",
        "written_at": rfc3339(now()),
        "registry_rev": "reg-9f21c4",
        "applied_rev": "reg-9f21c4",
        "config_rev": "reg-9f21c4",
        "validation": {"ok": True, "never_valid": False},
        "faults": [],
        "reconcile": None,
        "sandboxes": [
            {"id": "chat-s3", "state": "draining"},
            {"id": "chat-s4", "state": "ready"},
        ],
        "credentials": {"epoch": 7, "key_id": "sk-live-chat-7"},
        "spend": {"spend_usd": 3.42},
        "limits": {"max_running_turns": None, "job_timeout_s": None},
    }
    base.update(overrides)
    return base


def _publish(root: Path, **overrides: Any) -> StatusReader:
    write_json(status_file(root, _FAMILY), _document(**overrides))
    return StatusReader(root)


def test_reads_the_fields_this_service_needs(tmp_path: Path) -> None:
    status = _publish(tmp_path).read(_FAMILY)

    assert status is not None
    assert status.kind is SessionKind.ATTENDED
    assert status.state is FamilyState.IN_SYNC
    assert status.epoch == 7
    assert status.config_rev == "reg-9f21c4"
    assert status.is_stale() is False


def test_the_newest_ready_sandbox_serves(tmp_path: Path) -> None:
    status = _publish(tmp_path).read(_FAMILY)

    assert status is not None
    ready = status.ready_sandbox()
    assert ready is not None
    assert ready.id == "chat-s4"
    assert ready.state is SandboxState.READY


def test_a_cold_start_is_visible(tmp_path: Path) -> None:
    creating = [{"id": "chat-s5", "state": "creating"}]
    status = _publish(tmp_path, sandboxes=creating).read(_FAMILY)

    assert status is not None
    assert status.ready_sandbox() is None
    assert status.has_cold_start() is True


def test_a_creating_sandbox_is_startable(tmp_path: Path) -> None:
    """Contract 05 §4.2: `ready` means the handshake passed, and §4.3 steps
    6 and 7 run that handshake on the channel. This service holds the only
    channel, so a `creating` sandbox is the one it dials to get there."""
    creating = [{"id": "chat-s5", "state": "creating"}]
    status = _publish(tmp_path, sandboxes=creating).read(_FAMILY)

    assert status is not None
    startable = status.startable_sandbox()
    assert startable is not None
    assert startable.id == "chat-s5"


def test_a_ready_sandbox_beats_a_newer_creating_one(tmp_path: Path) -> None:
    """A switch is in flight. The one that already handshook keeps serving."""
    during_switch = [
        {"id": "chat-s4", "state": "ready"},
        {"id": "chat-s5", "state": "creating"},
    ]
    status = _publish(tmp_path, sandboxes=during_switch).read(_FAMILY)

    assert status is not None
    startable = status.startable_sandbox()
    assert startable is not None
    assert startable.id == "chat-s4"


def test_a_planned_sandbox_is_not_startable(tmp_path: Path) -> None:
    """`planned` means nothing exists yet, so there is nothing to dial."""
    planned = [{"id": "chat-s5", "state": "planned"}]
    status = _publish(tmp_path, sandboxes=planned).read(_FAMILY)

    assert status is not None
    assert status.startable_sandbox() is None
    assert status.has_cold_start() is True


def test_a_failed_sandbox_is_not_startable(tmp_path: Path) -> None:
    failed = [{"id": "chat-s5", "state": "failed"}]
    status = _publish(tmp_path, sandboxes=failed).read(_FAMILY)

    assert status is not None
    assert status.startable_sandbox() is None
    assert status.has_cold_start() is False


def test_the_env_file_path_is_read_per_sandbox(tmp_path: Path) -> None:
    """Contract 05 §4.1: `attendance` hands this path to
    `sbx exec --env-file`, which is the only way the playpen learns
    where its mounts are (contract 03 §7.1)."""
    rows = [{"id": "chat-s5", "state": "ready", "supervisor_env": "/srv/a/supervisor.env"}]
    status = _publish(tmp_path, sandboxes=rows).read(_FAMILY)

    assert status is not None
    box = status.sandbox_by_id("chat-s5")
    assert box is not None
    assert box.playpen_env == "/srv/a/supervisor.env"


def test_a_sandbox_row_with_no_env_file_reads_empty(tmp_path: Path) -> None:
    """The reader never invents a path. `service.py` turns the empty value
    into a fault, because no in-VM mount path exists to fall back to."""
    rows = [{"id": "chat-s5", "state": "ready"}]
    status = _publish(tmp_path, sandboxes=rows).read(_FAMILY)

    assert status is not None
    box = status.sandbox_by_id("chat-s5")
    assert box is not None
    assert box.playpen_env == ""


def test_an_unknown_sandbox_id_is_none(tmp_path: Path) -> None:
    status = _publish(tmp_path).read(_FAMILY)

    assert status is not None
    assert status.sandbox_by_id("chat-s99") is None


def test_a_missing_document_is_family_unknown(tmp_path: Path) -> None:
    reader = StatusReader(tmp_path)

    assert reader.read("nosuch") is None

    with pytest.raises(ApiError) as caught:
        reader.require("nosuch")

    assert caught.value.code is ErrorCode.FAMILY_UNKNOWN


def test_never_valid_refuses(tmp_path: Path) -> None:
    validation = {"ok": False, "never_valid": True}
    status = _publish(tmp_path, state="invalid", validation=validation).read(_FAMILY)
    assert status is not None

    with pytest.raises(ApiError) as caught:
        check_may_serve(status)

    assert caught.value.code is ErrorCode.FAMILY_INVALID


def test_invalid_alone_keeps_serving(tmp_path: Path) -> None:
    validation = {"ok": False, "never_valid": False}
    status = _publish(tmp_path, state="invalid", validation=validation).read(_FAMILY)

    assert status is not None
    check_may_serve(status)


def test_a_blocking_fault_degrades_the_family(tmp_path: Path) -> None:
    faults = [
        {"code": "orphan_processes", "blocks_turns": False},
        {"code": "sandbox_start_failed", "blocks_turns": True},
    ]
    status = _publish(tmp_path, state="degraded", faults=faults).read(_FAMILY)
    assert status is not None
    assert status.blocking_fault == "sandbox_start_failed"

    with pytest.raises(ApiError) as caught:
        check_may_serve(status)

    assert caught.value.code is ErrorCode.FAMILY_DEGRADED


def test_a_non_blocking_fault_still_serves(tmp_path: Path) -> None:
    faults = [{"code": "image_behind", "blocks_turns": False}]
    status = _publish(tmp_path, state="degraded", faults=faults).read(_FAMILY)

    assert status is not None
    assert status.blocking_fault is None
    check_may_serve(status)


def test_an_old_document_reads_as_stale(tmp_path: Path) -> None:
    status = _publish(tmp_path, written_at=_OLD_TIMESTAMP).read(_FAMILY)

    assert status is not None
    assert status.is_stale() is True


def test_junk_fields_take_their_defaults(tmp_path: Path) -> None:
    status = _publish(
        tmp_path,
        kind=42,
        state="not-a-state",
        credentials={"epoch": "seven"},
        sandboxes=["chat-s4", {"id": "../escape", "state": "ready"}, {"state": "ready"}],
        faults="not a list",
    ).read(_FAMILY)

    assert status is not None
    # No default for these two. A default kind opens the family to the
    # attended doors, and a default state reports a health nobody published.
    assert status.kind is None
    assert status.state is None
    assert status.epoch == 1
    assert status.sandboxes == []
    assert status.blocking_fault is None


def test_a_stated_kind_is_the_served_kind(tmp_path: Path) -> None:
    status = _publish(tmp_path, kind="thin").read(_FAMILY)

    assert status is not None
    assert served_kind(status) is SessionKind.THIN


@pytest.mark.parametrize("kind", ["", "robot", "Attended", 5, None], ids=repr)
def test_a_kind_this_service_cannot_read_reaches_no_door(tmp_path: Path, kind: object) -> None:
    """Contract 02 §3.1. A door reaches one kind, and this document proves none."""
    status = _publish(tmp_path, kind=kind).read(_FAMILY)
    assert status is not None
    assert status.kind is None

    with pytest.raises(ApiError) as caught:
        served_kind(status)

    assert caught.value.code is ErrorCode.FORBIDDEN
    assert caught.value.family == _FAMILY


def test_a_document_with_no_kind_reaches_no_door(tmp_path: Path) -> None:
    document = _document()
    del document["kind"]
    write_json(status_file(tmp_path, _FAMILY), document)
    status = StatusReader(tmp_path).read(_FAMILY)
    assert status is not None

    with pytest.raises(ApiError) as caught:
        served_kind(status)

    assert caught.value.code is ErrorCode.FORBIDDEN


def test_a_family_that_never_validated_states_no_kind(tmp_path: Path) -> None:
    """Contract 05 §3.1. `caregiver` knows no kind for such a family and writes
    an empty one. The answer is `family_invalid` for each door (contract 02
    §14)."""
    validation = {"ok": False, "never_valid": True}
    status = _publish(tmp_path, kind="", state="invalid", validation=validation).read(_FAMILY)
    assert status is not None

    with pytest.raises(ApiError) as caught:
        served_kind(status)

    assert caught.value.code is ErrorCode.FAMILY_INVALID


def test_a_state_this_service_cannot_read_is_not_in_sync(tmp_path: Path) -> None:
    """The state feeds one thing: the detail of a refusal. It then holds null."""
    faults = [{"code": "sandbox_start_failed", "blocks_turns": True}]
    status = _publish(tmp_path, state="in sync", faults=faults).read(_FAMILY)
    assert status is not None
    assert status.state is None

    with pytest.raises(ApiError) as caught:
        check_may_serve(status)

    assert caught.value.detail == {"family_state": None, "fault": "sandbox_start_failed"}


def test_families_lists_the_directory(tmp_path: Path) -> None:
    reader = _publish(tmp_path)
    write_json(status_file(tmp_path, "code"), _document(family="code"))

    assert reader.families() == ["chat", "code"]
