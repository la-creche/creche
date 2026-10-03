"""Seam 4: the status document `apply_once` wrote, read by the real `sessiond`.

`sessiond` never calls `managerd`. One file carries everything it knows
about a family (contract 05 §2), and `sessiond`'s own tests have always
hand-written that file. These tests hand it the real one.

    apply_once ──► families/chat/status.json ──► StatusReader ──► run_turn
                                                                     │
                                       FakePlaypen ◄── channel ◄──┘

The second half of this file is the harder half. `apply_once` reports
`creating`, never `ready`, and `ready` means "the channel handshake passed"
(contract 05 §4.2). In stage 1 only `sessiond` holds a channel, so only
`sessiond` can run that handshake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_managerd import paths as managerd_paths
from agent_managerd.playpen_env import read_playpen_env
from agent_sessiond.auth import Principal
from agent_sessiond.channel import SandboxDial
from agent_sessiond.config import Config
from agent_sessiond.errors import ApiError, ErrorCode, TurnReason
from agent_sessiond.exec_channel import DEFAULT_COMMAND, build_argv
from agent_sessiond.family_status import FamilyState, SandboxState, StatusReader, check_may_serve
from agent_sessiond.requests import CreateRequest, RunTurnRequest
from agent_sessiond.service import SessionService
from agent_sessiond.states import SessionKind, TurnState
from sessiond_harness import FakeFleet, PlaypenPlan, make_config

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
    """`apply_once` leaves a new sandbox `creating`: only `sessiond`'s
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


# --- the env file managerd wrote, carried by the command sessiond builds --


def test_the_reader_finds_the_env_file_managerd_wrote(
    reader: StatusReader, applied: Applied
) -> None:
    """Contract 05 §4.1. The path travels in the status document and
    nowhere else: `sessiond` never calls `managerd`."""
    status = reader.read(FAMILY)
    assert status is not None
    box = status.sandbox_by_id(SANDBOX)
    assert box is not None
    env_path = managerd_paths.playpen_env_path(applied.state_root, FAMILY, SANDBOX)
    assert box.playpen_env == str(env_path)
    assert Path(box.playpen_env).is_file()


def test_the_env_file_names_the_directories_apply_mounted(applied: Applied) -> None:
    """The same three host paths `_sandbox_spec` handed `sbx create`. A
    mount's in-VM path IS its host path, so there is nothing to translate
    between the two sides (contract 03 §7.1)."""
    env_path = managerd_paths.playpen_env_path(applied.state_root, FAMILY, SANDBOX)
    values = read_playpen_env(env_path)
    spec = applied.driver.calls[0].args[0]
    mounted = [mount.path for mount in getattr(spec, "mounts", ())]

    assert values["AGENT_CRED_DIR"] in mounted
    assert values["AGENT_FAMILY_CONFIG_DIR"] in mounted
    assert values["AGENT_CONTROL_DIR"] in mounted
    assert values["AGENT_SANDBOX"] == SANDBOX


def test_the_default_command_carries_that_file_and_the_id(
    reader: StatusReader, applied: Applied
) -> None:
    """The end of the seam: what `sessiond` would run on the host, built
    from `managerd`'s own document with no value typed by hand.

    `sbx exec` forwards no host environment, so a command without
    `--env-file` starts a playpen that finds none of its three mounts;
    one without `--sandbox` makes it exit 2 before opening anything.
    """
    status = reader.read(FAMILY)
    assert status is not None
    box = status.sandbox_by_id(SANDBOX)
    assert box is not None

    argv = build_argv(DEFAULT_COMMAND, SandboxDial(SANDBOX, box.playpen_env))
    env_path = managerd_paths.playpen_env_path(applied.state_root, FAMILY, SANDBOX)

    assert argv[:2] == ["sbx", "exec"]
    assert argv[argv.index("--env-file") + 1] == str(env_path)
    assert argv[argv.index("--sandbox") + 1] == SANDBOX


# --- the pair, end to end ------------------------------------------------


def build_service(applied: Applied, tmp_path: Path) -> tuple[SessionService, FakeFleet]:
    """A real `sessiond` whose state root IS the one `managerd` wrote."""
    base = make_config(tmp_path / "sessiond")
    config = Config(
        sessions_root=applied.sessions_root,
        state_root=applied.state_root,
        work_root=base.work_root,
        socket_path=base.socket_path,
        bind=base.bind,
        lan_address=base.lan_address,
        lan_port=base.lan_port,
        channel_command=base.channel_command,
        log_dir=base.log_dir,
    )
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=COLD_START_WAIT_S)
    service.start()
    return service, fleet


async def test_a_turn_reaches_the_sandbox_managerd_created(
    applied: Applied, tmp_path: Path
) -> None:
    """The point of the seam: the document `managerd` wrote is enough for
    `sessiond` to dial the sandbox `managerd` created."""
    service, fleet = build_service(applied, tmp_path)
    try:
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=SESSION))
        live = await service.run_turn(OWUI, FAMILY, SESSION, RunTurnRequest(prompt=PROMPT), DOOR)
        started = await fleet.playpen(SANDBOX).next_start()

        assert live.record.sandbox == SANDBOX
        assert started["session"] == SESSION
        assert fleet.dials == [SANDBOX]
        # Contract 03 §7.1: the dial carries the file, not only the id.
        assert fleet.env_files == [
            str(managerd_paths.playpen_env_path(applied.state_root, FAMILY, SANDBOX))
        ]
    finally:
        await service.close()
        await fleet.stop()


async def test_the_channel_carries_the_epoch_and_config_rev(
    applied: Applied, tmp_path: Path
) -> None:
    """Both numbers come from the status document and nowhere else."""
    service, fleet = build_service(applied, tmp_path)
    try:
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=SESSION))
        await service.run_turn(OWUI, FAMILY, SESSION, RunTurnRequest(prompt=PROMPT), DOOR)
        started = await fleet.playpen(SANDBOX).next_start()
        hello = fleet.playpen(SANDBOX).hello

        assert started["env_epoch"] == 1
        assert started["config_rev"] == applied.result.status.config_rev
        assert hello is not None
        assert hello["family"] == FAMILY
    finally:
        await service.close()
        await fleet.stop()


async def test_a_failed_handshake_still_fails_the_turn(applied: Applied, tmp_path: Path) -> None:
    """Contract 05 §4.2: `ready` means the handshake passed. Dialling a
    sandbox is not the same as it serving, and nothing here weakens that."""
    service, fleet = build_service(applied, tmp_path)
    # Contract 03 §3: a `ready` naming another sandbox fails the handshake.
    fleet.plan(SANDBOX, PlaypenPlan(sandbox="chat-s99"))
    try:
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=SESSION))
        live = await service.run_turn(OWUI, FAMILY, SESSION, RunTurnRequest(prompt=PROMPT), DOOR)

        assert live.record.state is TurnState.FAILED
        assert live.record.reason is TurnReason.CHANNEL_LOST
    finally:
        await service.close()
        await fleet.stop()


async def test_a_family_with_no_sandbox_is_refused(applied: Applied, tmp_path: Path) -> None:
    """The check is not weakened: an empty `sandboxes` list still refuses,
    and nothing is dialled."""
    strip_sandboxes(applied.status_path)
    service, fleet = build_service(applied, tmp_path)
    try:
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=SESSION))
        with pytest.raises(ApiError) as raised:
            await service.run_turn(OWUI, FAMILY, SESSION, RunTurnRequest(prompt=PROMPT), DOOR)

        assert raised.value.code is ErrorCode.SANDBOX_UNAVAILABLE
        assert fleet.dials == []
    finally:
        await service.close()
        await fleet.stop()


async def test_a_planned_sandbox_is_waited_for_not_dialled(
    applied: Applied, tmp_path: Path
) -> None:
    """`planned` means nothing exists yet (contract 05 §4.2), so there is
    nothing to dial. The cold-start wait still covers that case."""
    set_sandbox_state(applied.status_path, SandboxState.PLANNED.value)
    service, fleet = build_service(applied, tmp_path)
    try:
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=SESSION))
        with pytest.raises(ApiError) as raised:
            await service.run_turn(OWUI, FAMILY, SESSION, RunTurnRequest(prompt=PROMPT), DOOR)

        assert raised.value.code is ErrorCode.SANDBOX_UNAVAILABLE
        assert fleet.dials == []
    finally:
        await service.close()
        await fleet.stop()


async def test_a_ready_sandbox_still_wins_over_a_newer_creating_one(
    applied: Applied, tmp_path: Path
) -> None:
    """Stage 2's switch: while a replacement is being built, the sandbox
    that already handshook keeps serving."""
    add_sandbox(applied.status_path, "chat-s2", SandboxState.CREATING.value)
    set_sandbox_state(applied.status_path, SandboxState.READY.value, box_id=SANDBOX)
    service, fleet = build_service(applied, tmp_path)
    try:
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=SESSION))
        live = await service.run_turn(OWUI, FAMILY, SESSION, RunTurnRequest(prompt=PROMPT), DOOR)
        await fleet.playpen(SANDBOX).next_start()

        assert live.record.sandbox == SANDBOX
    finally:
        await service.close()
        await fleet.stop()


# --- editing the document, the way a later stage would -------------------


def read_status(path: Path) -> dict[str, object]:
    body: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return body


def write_status(path: Path, body: dict[str, object]) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def strip_sandboxes(path: Path) -> None:
    body = read_status(path)
    body["sandboxes"] = []
    write_status(path, body)


def set_sandbox_state(path: Path, state: str, box_id: str = SANDBOX) -> None:
    body = read_status(path)
    for box in body["sandboxes"]:  # pyright: ignore[reportUnknownVariableType]
        if box["id"] == box_id:  # pyright: ignore[reportUnknownMemberType]
            box["state"] = state  # pyright: ignore[reportUnknownMemberType]
    write_status(path, body)


def add_sandbox(path: Path, box_id: str, state: str) -> None:
    body = read_status(path)
    first = body["sandboxes"][0]  # pyright: ignore[reportUnknownVariableType]
    body["sandboxes"].append({**first, "id": box_id, "state": state})  # pyright: ignore[reportUnknownMemberType]
    write_status(path, body)
