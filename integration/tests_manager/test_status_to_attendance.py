"""Seam 4: the status document `apply_once` wrote, read by the real `attendance`.

`attendance` never calls `caregiver`. One file carries everything it knows
about a family (contract 05 §2), and `attendance`'s own tests have always
hand-written that file. These tests hand it the real one.

    apply_once ──► families/chat/status.json ──► StatusReader

The scenarios that ran a turn over the channel left with the session API
(phase 2 plan, row 21).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from attendance.auth import Principal
from attendance.channel import SandboxDial
from attendance.config import DEFAULT_FACADE_COMMAND
from attendance.exec_channel import build_argv
from attendance.family_status import FamilyState, SandboxState, StatusReader, check_may_serve
from attendance.states import SessionKind
from caregiver.playpen_env import read_playpen_env

from caregiver import paths as caregiver_paths

from .conftest import FAMILY, Applied

SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
SANDBOX = "chat-s1"
PROMPT = "Which sensor dropped out last night?"
DOOR = "owui-1"
OWUI = Principal.DOOR_OWUI

#: Long enough to prove a wait happens, short enough not to cost a minute.
COLD_START_WAIT_S = 1.0


@pytest.fixture
def reader(applied: Applied) -> StatusReader:
    return StatusReader(applied.state_root)


# --- the document itself -------------------------------------------------


def test_the_reader_accepts_the_document(reader: StatusReader, applied: Applied) -> None:
    status = reader.read(FAMILY)
    assert status is not None
    assert status.family == FAMILY
    assert status.kind is SessionKind.ATTENDED
    assert status.state is FamilyState.IN_SYNC
    assert status.config_rev == applied.result.status.config_rev


def test_the_reader_finds_the_sandbox_id(reader: StatusReader) -> None:
    status = reader.read(FAMILY)
    assert status is not None
    assert [box.id for box in status.sandboxes] == [SANDBOX]
    assert status.sandboxes[0].index == 1


def test_the_reader_takes_the_credential_epoch(reader: StatusReader) -> None:
    """Contract 03 §12.5: the epoch rides on the channel, so a wrong one
    would make the playpen refuse every turn."""
    status = reader.read(FAMILY)
    assert status is not None
    assert status.epoch == 1


def test_the_document_is_not_stale_when_written(reader: StatusReader) -> None:
    status = reader.read(FAMILY)
    assert status is not None
    assert status.is_stale() is False


def test_the_family_may_serve(reader: StatusReader) -> None:
    status = reader.read(FAMILY)
    assert status is not None
    check_may_serve(status)  # contract 02 §5.1: `in_sync` is accepted


def test_the_family_appears_in_the_listing(reader: StatusReader) -> None:
    assert reader.families() == [FAMILY]


def test_apply_reports_creating_and_never_ready(reader: StatusReader) -> None:
    """`apply_once` leaves a new sandbox `creating`: only `attendance`'s
    handshake promotes it. Restated as a test so a change to it is visible."""
    status = reader.read(FAMILY)
    assert status is not None
    assert status.sandboxes[0].state is SandboxState.CREATING
    assert status.ready_sandbox() is None
    assert status.has_cold_start() is True


def test_a_reapply_keeps_the_document_readable(reader: StatusReader, applied: Applied) -> None:
    applied.reapply()
    status = reader.read(FAMILY)
    assert status is not None
    assert [box.id for box in status.sandboxes] == [SANDBOX]


# --- the env file caregiver wrote, carried by the command attendance builds --


def test_the_reader_finds_the_env_file_caregiver_wrote(
    reader: StatusReader, applied: Applied
) -> None:
    """Contract 05 §4.1. The path travels in the status document and
    nowhere else: `attendance` never calls `caregiver`."""
    status = reader.read(FAMILY)
    assert status is not None
    box = status.sandbox_by_id(SANDBOX)
    assert box is not None
    env_path = caregiver_paths.playpen_env_path(applied.state_root, FAMILY, SANDBOX)
    assert box.playpen_env == str(env_path)
    assert Path(box.playpen_env).is_file()


def test_the_env_file_names_the_directories_apply_mounted(applied: Applied) -> None:
    """The same three host paths `_sandbox_spec` handed `sbx create`. A
    mount's in-VM path IS its host path, so there is nothing to translate
    between the two sides (contract 03 §7.1)."""
    env_path = caregiver_paths.playpen_env_path(applied.state_root, FAMILY, SANDBOX)
    values = read_playpen_env(env_path)
    spec = applied.driver.calls[0].args[0]
    mounted = [mount.path for mount in getattr(spec, "mounts", ())]

    assert values["AGENT_CRED_DIR"] in mounted
    assert values["AGENT_FAMILY_CONFIG_DIR"] in mounted
    assert values["AGENT_CONTROL_DIR"] in mounted
    assert values["AGENT_SANDBOX"] == SANDBOX


def test_the_facade_command_carries_that_file_and_the_id(
    reader: StatusReader, applied: Applied
) -> None:
    """The end of the seam: what `attendance` would run on the host, built
    from `caregiver`'s own document with no value typed by hand.

    `sbx exec` forwards no host environment, so a command without
    `--env-file` starts a facade that finds none of its three mounts.
    """
    status = reader.read(FAMILY)
    assert status is not None
    box = status.sandbox_by_id(SANDBOX)
    assert box is not None

    argv = build_argv(DEFAULT_FACADE_COMMAND, SandboxDial(SANDBOX, box.playpen_env))
    env_path = caregiver_paths.playpen_env_path(applied.state_root, FAMILY, SANDBOX)

    assert argv[:2] == ["sbx", "exec"]
    assert argv[argv.index("--env-file") + 1] == str(env_path)
    assert SANDBOX in argv
