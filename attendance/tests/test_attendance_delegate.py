"""The delegate door: one thin job on its own session, its answer in the response.

Contract 02 §6 and §12, contract 04 §7.3. The PEP is the one client. The
job makes one row with the `delegate` origin, opens the session's vmm
stream, sends one `session/prompt`, and drops the session at its end
(decisions CH1, CH21-2, CH21-4).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from attendance.acp_link import METHOD_CANCEL, METHOD_INITIALIZE, METHOD_PROMPT
from attendance.auth import Principal
from attendance.errors import HTTP_UNAVAILABLE, ApiError, ErrorCode
from attendance.ids import is_session_id, is_ulid, new_ulid
from attendance.jobs import (
    DEFAULT_JOB_TIMEOUT_S,
    MESSAGE_MAX_BYTES,
    JobStatus,
    job_timeout_s,
)
from attendance.paths import SANDBOX_MOUNT_ENV, sandbox_sessions_mount
from attendance.requests import DelegateRequest, read_delegate
from attendance.vmm import HOST_FULL
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    make_config,
    make_service,
    wait_until,
    write_status,
)
from creche_wire import CreateSession, DelegateOrigin
from fake_facade import ANSWER, ERROR_MESSAGE, FakeFacade, PromptMode
from fake_vmm import FakeVmm

DELEGATE = Principal.CHAPERONE_DELEGATE
DISPATCH = Principal.CHAPERONE_DISPATCH

ORACLE = "vault-oracle"
CODING_SANDBOX_FAMILY = "coding-sandbox"

MESSAGE = "Which note holds the boiler service date?"
CANCEL_WAIT_S = 0.2


class DelegateHarness:
    """One service with two thin families, over one `FakeVmm`."""

    def __init__(
        self,
        tmp_path: Path,
        job_timeout_s: int | None = DEFAULT_JOB_TIMEOUT_S,
        mode: PromptMode = PromptMode.OK,
    ) -> None:
        self.config = make_config(tmp_path)
        write_status(self.config.state_root)

        for family in (ORACLE, CODING_SANDBOX_FAMILY):
            write_status(
                self.config.state_root, family=family, kind="thin", job_timeout_s=job_timeout_s
            )

        self.vmm = FakeVmm(prompt_mode=mode)
        self.service = make_service(self.config, self.vmm, cancel_wait_s=CANCEL_WAIT_S)
        self.service.start()

    def call(
        self,
        target: str = ORACLE,
        owner: str | None = CHAT_SESSION,
        delegation_id: str | None = None,
    ) -> DelegateRequest:
        return DelegateRequest(
            caller_family=FAMILY,
            target_family=target,
            delegation_id=delegation_id if delegation_id is not None else new_ulid(),
            claimed_session_id=owner,
            message=MESSAGE,
        )

    async def delegate(self, request: DelegateRequest) -> dict[str, object]:
        return await asyncio.wait_for(self.service.run_delegate(DELEGATE, request), 5.0)

    def create(self) -> CreateSession:
        (made,) = self.vmm.creates
        return made

    def facade(self) -> FakeFacade:
        (facade,) = self.vmm.facades.values()
        return facade

    async def stop(self) -> None:
        await self.service.close()
        await self.vmm.stop()


async def test_a_delegate_call_answers_ok_with_the_streamed_text(tmp_path: Path) -> None:
    """Contract 04 §7.3. `end_turn` is `ok`, and the chunks are the content."""
    harness = DelegateHarness(tmp_path)
    body = await harness.delegate(harness.call())

    assert body == {
        "status": JobStatus.OK.value,
        "session_id": body["session_id"],
        "content": ANSWER,
        "error": None,
        "usage": None,
    }
    assert str(body["session_id"]).startswith("job-")
    assert is_ulid(str(body["session_id"])[len("job-") :])
    assert harness.facade().methods == [METHOD_INITIALIZE, METHOD_PROMPT]
    await harness.stop()


async def test_the_job_makes_one_row_with_the_delegate_origin(tmp_path: Path) -> None:
    """Decisions CH21-4, CH25 rule 2: attendance mints the session id, and the
    origin holds the job id and the delegation, never `_meta`."""
    harness = DelegateHarness(tmp_path)
    delegation_id = new_ulid()
    body = await harness.delegate(harness.call(delegation_id=delegation_id))
    create = harness.create()
    origin = create.origin.root

    assert isinstance(origin, DelegateOrigin)
    assert origin.delegate.job.root == body["session_id"]
    assert origin.delegate.delegation.root == delegation_id
    assert is_session_id(create.id.root)
    assert create.family.root == ORACLE
    assert create.cwd.root == sandbox_sessions_mount(harness.config.sessions_root, ORACLE)
    assert "_meta" not in harness.facade().received[-1]["params"]
    assert harness.vmm.opened == [create.id.root]
    await harness.stop()


async def test_the_session_drops_at_the_end_of_the_job(tmp_path: Path) -> None:
    """Decision CH21-2: the job leaves no row and no stream."""
    harness = DelegateHarness(tmp_path)
    await harness.delegate(harness.call())

    assert harness.vmm.drops == [(ORACLE, harness.create().id.root)]
    assert harness.vmm.rows == {}
    assert harness.vmm.channels[harness.create().id.root].closed
    await harness.stop()


@pytest.mark.parametrize("mount", ["sessions", "/" + "a" * 4096])
async def test_a_cwd_that_the_wire_refuses_is_never_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mount: str
) -> None:
    """Decision CH21: `SessionCwd` is absolute, at most 4096 characters."""
    monkeypatch.setenv(SANDBOX_MOUNT_ENV, mount)
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call())

    assert refused.value.code is ErrorCode.INTERNAL
    assert harness.vmm.creates == []
    await harness.stop()


async def test_a_job_past_its_deadline_is_cancelled_and_answers_timeout(tmp_path: Path) -> None:
    """Contract 02 §12 rule 4: past the deadline, cancel, then `timeout`."""
    harness = DelegateHarness(tmp_path, job_timeout_s=1, mode=PromptMode.HANG)
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.TIMEOUT.value
    assert body["content"] is None
    assert str(body["error"]).startswith("turn_timeout")
    assert harness.facade().methods[-1] == METHOD_CANCEL
    assert harness.vmm.rows == {}
    await harness.stop()


async def test_a_job_whose_prompt_never_ends_still_answers_timeout(tmp_path: Path) -> None:
    """A facade that ignores the cancel delays the answer by the wait only."""
    harness = DelegateHarness(tmp_path, job_timeout_s=1, mode=PromptMode.HOLD)
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.TIMEOUT.value
    assert harness.facade().methods[-1] == METHOD_CANCEL
    await harness.stop()


async def test_a_json_rpc_error_answers_failed(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path, mode=PromptMode.ERROR)
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.FAILED.value
    assert body["content"] is None
    assert ERROR_MESSAGE in str(body["error"])
    await harness.stop()


async def test_a_stream_lost_mid_prompt_answers_failed(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path, mode=PromptMode.LOSE)
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.FAILED.value
    assert body["content"] is None
    assert str(body["error"]).startswith("channel_lost")
    assert harness.vmm.rows == {}
    await harness.stop()


async def test_a_bad_handshake_answers_failed(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path)
    harness.vmm.protocol_version = 2
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.FAILED.value
    assert str(body["error"]).startswith("channel_lost")
    assert harness.facade().methods == [METHOD_INITIALIZE]
    await harness.stop()


async def test_a_stop_reason_other_than_end_turn_answers_failed(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path, mode=PromptMode.HOLD)
    task = asyncio.create_task(harness.delegate(harness.call()))
    await wait_until(lambda: any(facade.held for facade in harness.vmm.facades.values()))
    await harness.vmm.settle(harness.vmm.only_session(), "max_tokens")
    body = await task

    assert body["status"] == JobStatus.FAILED.value
    assert str(body["error"]).startswith("max_tokens")
    await harness.stop()


@pytest.mark.parametrize("route", ["create", "acp"])
@pytest.mark.parametrize("code", ["unreachable", "sandbox_start_failed"])
async def test_a_vmm_refusal_answers_failed_with_its_code(
    tmp_path: Path, route: str, code: str
) -> None:
    """A refusal other than `host_full` ends the job at once."""
    harness = DelegateHarness(tmp_path)
    harness.vmm.refuse[route] = code
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.FAILED.value
    assert str(body["error"]).startswith(code)
    assert harness.vmm.rows == {}
    await harness.stop()


async def test_a_job_asks_again_after_host_full(tmp_path: Path) -> None:
    """Decision CH21-3: the vmm never queues, and a delegate job tries again
    inside its deadline. The row stays between tries."""
    harness = DelegateHarness(tmp_path)
    harness.vmm.refuse["acp"] = HOST_FULL
    task = asyncio.create_task(harness.delegate(harness.call()))
    await wait_until(lambda: len(harness.vmm.opened) == 1)
    del harness.vmm.refuse["acp"]
    body = await task

    assert body["status"] == JobStatus.OK.value
    assert len(harness.vmm.creates) == 1
    assert harness.vmm.opened == [harness.create().id.root] * 2
    assert harness.vmm.rows == {}
    await harness.stop()


async def test_host_full_for_the_whole_deadline_answers_503(tmp_path: Path) -> None:
    """Contract 02 §12 rule 4, decision CH83 rule 2: past the deadline the
    door answers `host_full`, 503 with no `retry_after_s`. A try starts only
    before the deadline, so a 2 s deadline holds two tries."""
    harness = DelegateHarness(tmp_path, job_timeout_s=2)
    harness.vmm.refuse["acp"] = HOST_FULL

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call())

    assert refused.value.code is ErrorCode.HOST_FULL
    assert refused.value.status == HTTP_UNAVAILABLE
    assert "retry_after_s" not in refused.value.body()
    assert harness.vmm.opened == [harness.create().id.root] * 2
    assert harness.vmm.rows == {}
    await harness.stop()


async def test_a_failed_drop_still_answers(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path)
    harness.vmm.refuse["drop"] = "unreachable"
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.OK.value
    await harness.stop()


async def test_a_non_thin_family_is_refused(tmp_path: Path) -> None:
    """Contract 02 §3.1's kind column: only a thin family takes a delegate call."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call(target=FAMILY))

    assert refused.value.code is ErrorCode.FORBIDDEN
    assert harness.vmm.creates == []
    await harness.stop()


async def test_a_family_of_no_known_kind_takes_no_delegate_call(tmp_path: Path) -> None:
    """Contract 02 §3.1. A status document that states no known kind proves no
    thin family. The refusal comes before a session is made."""
    harness = DelegateHarness(tmp_path)
    write_status(harness.config.state_root, family=CODING_SANDBOX_FAMILY, kind="robot")

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call(target=CODING_SANDBOX_FAMILY))

    assert refused.value.code is ErrorCode.FORBIDDEN
    assert refused.value.detail == {"kind": None}
    assert harness.vmm.creates == []
    await harness.stop()


async def test_only_the_delegate_door_may_call_it(tmp_path: Path) -> None:
    """Contract 02 §3.1. The PEP is the one client of this path."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.run_delegate(DISPATCH, harness.call())

    assert refused.value.code is ErrorCode.FORBIDDEN
    await harness.stop()


@pytest.mark.parametrize("owner", [CHAT_SESSION, None])
async def test_a_job_works_in_the_family_mount(tmp_path: Path, owner: str | None) -> None:
    """Contract 01 minor 18 drops `job.caller_root` (decision CH10-10). A job
    runs in a VM of its own, dropped at job end (decisions CH1, CH21-2), so
    the calling chat makes no directory and names no cwd."""
    harness = DelegateHarness(tmp_path)
    await harness.delegate(harness.call(target=CODING_SANDBOX_FAMILY, owner=owner))
    mount = sandbox_sessions_mount(harness.config.sessions_root, CODING_SANDBOX_FAMILY)

    assert harness.create().cwd.root == mount
    await harness.stop()


async def test_two_jobs_get_two_sessions(tmp_path: Path) -> None:
    """Contract 02 §12 rule 2: thin jobs of one family share no VM."""
    harness = DelegateHarness(tmp_path, mode=PromptMode.HOLD)
    first = asyncio.create_task(harness.delegate(harness.call()))
    second = asyncio.create_task(harness.delegate(harness.call()))
    await wait_until(lambda: sum(len(one.held) for one in harness.vmm.facades.values()) == 2)
    assert harness.service.busy(Principal.NOTICEBOARD)["prompts_in_flight"] == 2

    for session in list(harness.vmm.facades):
        await harness.vmm.settle(session)

    bodies = await asyncio.gather(first, second)

    assert [body["status"] for body in bodies] == [JobStatus.OK.value] * 2
    assert bodies[0]["session_id"] != bodies[1]["session_id"]
    assert len(set(harness.vmm.opened)) == 2
    assert harness.service.busy(Principal.NOTICEBOARD)["prompts_in_flight"] == 0
    await harness.stop()


def _body(**over: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "caller_family": FAMILY,
        "target_family": ORACLE,
        "delegation_id": new_ulid(),
        "claimed_session_id": CHAT_SESSION,
        "message": MESSAGE,
    }
    raw.update(over)
    return raw


def test_a_good_delegate_body_parses() -> None:
    parsed = read_delegate(_body())

    assert parsed.target_family == ORACLE
    assert parsed.claimed_session_id == CHAT_SESSION
    assert parsed.message == MESSAGE


def test_an_absent_calling_session_id_is_allowed() -> None:
    """Contract 04 §3.1 rule 4: the PEP drops a malformed header and sends none."""
    raw = _body()
    del raw["claimed_session_id"]

    assert read_delegate(raw).claimed_session_id is None


@pytest.mark.parametrize(
    "over",
    [
        {"delegation_id": "not-a-ulid"},
        {"delegation_id": "01jbq7wz0x4t9v6k2h8m3n5pqr"},
        {"caller_family": "Chat"},
        {"target_family": "vault_oracle"},
        {"claimed_session_id": "../../etc"},
        {"message": ""},
    ],
)
def test_a_bad_delegate_body_is_refused(over: dict[str, object]) -> None:
    with pytest.raises(ApiError) as refused:
        read_delegate(_body(**over))

    assert refused.value.code is ErrorCode.BAD_REQUEST


def test_a_message_over_the_cap_is_refused() -> None:
    """Contract 04 §7.1: 1 to 64 KiB."""
    with pytest.raises(ApiError) as refused:
        read_delegate(_body(message="x" * (MESSAGE_MAX_BYTES + 1)))

    assert refused.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_the_job_timeout_falls_back_to_the_contract_default() -> None:
    """Contract 02 §12 rule 3. 120 seconds when the document names none."""
    assert job_timeout_s(None) == DEFAULT_JOB_TIMEOUT_S
    assert job_timeout_s(0) == DEFAULT_JOB_TIMEOUT_S
    assert job_timeout_s(7) == 7
