"""The delegate door: one thin job over ACP, its answer in the response.

Contract 02 §12 and §12.1, contract 04 §7.3. The PEP is the one client. The
job is one `session/new` and one `session/prompt` on the thin family's
facade (phase 2 plan, row 18). The conversation stays in the facade's store
(phase 2 plan, Q5).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from attendance.acp_link import METHOD_CANCEL, METHOD_NEW_SESSION, METHOD_PROMPT
from attendance.auth import Principal
from attendance.errors import ApiError, ErrorCode
from attendance.ids import is_ulid, new_ulid
from attendance.jobs import (
    DEFAULT_JOB_TIMEOUT_S,
    MESSAGE_MAX_BYTES,
    JobStatus,
    job_timeout_s,
)
from attendance.paths import sandbox_sessions_mount
from attendance.requests import DelegateRequest, read_delegate
from attendance.workspace import CODE_SANDBOX_FAMILY, owner_dir
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    make_config,
    wait_until,
    write_status,
)
from creche_vectors import meta_key, value_fields
from fake_facade import ANSWER, ERROR_MESSAGE, FacadeFleet, FakeFacade, PromptMode

from attendance.service import SessionService  # isort: skip

DELEGATE = Principal.DOOR_DELEGATE
DISPATCH = Principal.DOOR_DISPATCH

ORACLE = "vault-oracle"
ORACLE_SANDBOX = "vault-oracle-s1"
CODE_SANDBOX = "code-sandbox-s1"

MESSAGE = "Which note holds the boiler service date?"
CANCEL_WAIT_S = 0.2


class DelegateHarness:
    """One service with two thin families, each served by a `FakeFacade`."""

    def __init__(
        self,
        tmp_path: Path,
        job_timeout_s: int | None = DEFAULT_JOB_TIMEOUT_S,
        mode: PromptMode = PromptMode.OK,
    ) -> None:
        self.config = make_config(tmp_path)
        write_status(self.config.state_root)

        for family, sandbox in ((ORACLE, ORACLE_SANDBOX), (CODE_SANDBOX_FAMILY, CODE_SANDBOX)):
            write_status(
                self.config.state_root,
                family=family,
                kind="thin",
                sandboxes=((sandbox, "ready"),),
                job_timeout_s=job_timeout_s,
            )

        self.facades = FacadeFleet(prompt_mode=mode)
        self.service = SessionService(
            self.config,
            acp_factory=self.facades.factory,
            cold_start_wait_s=1.0,
            cancel_wait_s=CANCEL_WAIT_S,
        )
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

    def facade(self, sandbox: str = ORACLE_SANDBOX) -> FakeFacade:
        return self.facades.facades[sandbox]

    def prompt_meta(self, sandbox: str = ORACLE_SANDBOX) -> object:
        prompt = next(
            one for one in self.facade(sandbox).received if one["method"] == METHOD_PROMPT
        )
        return prompt["params"].get("_meta")

    def new_session_params(self, sandbox: str = ORACLE_SANDBOX) -> dict[str, object]:
        call = next(
            one for one in self.facade(sandbox).received if one["method"] == METHOD_NEW_SESSION
        )
        return call["params"]

    async def stop(self) -> None:
        await self.service.close()
        await self.facades.stop()


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
    assert harness.facade().methods == ["initialize", METHOD_NEW_SESSION, METHOD_PROMPT]
    await harness.stop()


async def test_the_job_id_rides_on_session_new(tmp_path: Path) -> None:
    """Q5: the conversation stays in the facade's store under the `job-` id."""
    harness = DelegateHarness(tmp_path)
    body = await harness.delegate(harness.call())
    params = harness.new_session_params()

    assert params["_meta"] == {meta_key("job"): body["session_id"]}
    assert params["cwd"] == sandbox_sessions_mount(harness.config.sessions_root, ORACLE)
    assert params["mcpServers"] == []
    await harness.stop()


async def test_the_conversation_is_kept(tmp_path: Path) -> None:
    """Q5: nothing deletes the conversation in the facade's store."""
    harness = DelegateHarness(tmp_path)
    await harness.delegate(harness.call())

    assert harness.facade().sessions == ["sess-1"]
    assert "session/close" not in harness.facade().methods
    await harness.stop()


async def test_a_job_past_its_deadline_is_cancelled_and_answers_timeout(tmp_path: Path) -> None:
    """Contract 02 §12 rule 4: past the deadline, cancel, then `timeout`."""
    harness = DelegateHarness(tmp_path, job_timeout_s=1, mode=PromptMode.HANG)
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.TIMEOUT.value
    assert body["content"] is None
    assert str(body["error"]).startswith("turn_timeout")
    assert harness.facade().methods[-1] == METHOD_CANCEL
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


async def test_a_link_lost_mid_prompt_answers_failed(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path, mode=PromptMode.LOSE)
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.FAILED.value
    assert body["content"] is None
    assert str(body["error"]).startswith("channel_lost")
    await harness.stop()


async def test_the_next_job_dials_a_lost_link_again(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path, mode=PromptMode.LOSE)
    await harness.delegate(harness.call())
    harness.facades.prompt_mode = PromptMode.OK
    await wait_until(lambda: harness.service.acp_link(ORACLE_SANDBOX) is None)

    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.OK.value
    assert harness.facades.dials == [ORACLE_SANDBOX, ORACLE_SANDBOX]
    await harness.stop()


async def test_a_stop_reason_other_than_end_turn_answers_failed(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path, mode=PromptMode.HOLD)
    task = asyncio.create_task(harness.delegate(harness.call()))
    await wait_until(lambda: ORACLE_SANDBOX in harness.facades.facades)
    await wait_until(lambda: "sess-1" in harness.facade().held)
    await harness.facades.settle(ORACLE_SANDBOX, "sess-1", "max_tokens")
    body = await task

    assert body["status"] == JobStatus.FAILED.value
    assert str(body["error"]).startswith("max_tokens")
    await harness.stop()


async def test_a_family_with_no_sandbox_is_unavailable(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path)
    write_status(harness.config.state_root, family=ORACLE, kind="thin", sandboxes=())

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call())

    assert refused.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    await harness.stop()


async def test_a_non_thin_family_is_refused(tmp_path: Path) -> None:
    """Contract 02 §3.1's kind column: only a thin family takes a delegate call."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call(target=FAMILY))

    assert refused.value.code is ErrorCode.FORBIDDEN
    assert harness.facades.dials == []
    await harness.stop()


async def test_a_family_of_no_known_kind_takes_no_delegate_call(tmp_path: Path) -> None:
    """Contract 02 §3.1. A status document that states no known kind proves no
    thin family. The refusal comes before the work directory exists."""
    harness = DelegateHarness(tmp_path)
    write_status(
        harness.config.state_root,
        family=CODE_SANDBOX_FAMILY,
        kind="robot",
        sandboxes=((CODE_SANDBOX, "ready"),),
    )

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY))

    assert refused.value.code is ErrorCode.FORBIDDEN
    assert refused.value.detail == {"kind": None}
    assert harness.facades.dials == []
    assert not owner_dir(harness.config.work_root, CHAT_SESSION).exists()
    await harness.stop()


async def test_only_the_delegate_door_may_call_it(tmp_path: Path) -> None:
    """Contract 02 §3.1. The PEP is the one client of this path."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.run_delegate(DISPATCH, harness.call())

    assert refused.value.code is ErrorCode.FORBIDDEN
    await harness.stop()


@pytest.mark.parametrize("owner", ["../../etc", "a/b", ".."])
async def test_a_hostile_calling_session_id_is_refused(tmp_path: Path, owner: str) -> None:
    """It becomes a path component, so it is checked before anything runs."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY, owner=owner))

    assert refused.value.code is ErrorCode.BAD_REQUEST
    assert harness.facades.dials == []
    assert not (harness.config.work_root / CODE_SANDBOX_FAMILY).exists()
    await harness.stop()


async def test_code_sandbox_works_in_the_calling_chats_directory(tmp_path: Path) -> None:
    """Contract 02 §12.1 rules 2 and 3. The chat's directory is the cwd."""
    harness = DelegateHarness(tmp_path)
    await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY))
    made = owner_dir(harness.config.work_root, CHAT_SESSION)

    assert made.is_dir()
    assert harness.new_session_params(CODE_SANDBOX)["cwd"] == str(made)
    await harness.stop()


async def test_code_sandbox_with_no_calling_chat_works_in_the_family_mount(
    tmp_path: Path,
) -> None:
    harness = DelegateHarness(tmp_path)
    await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY, owner=None))
    mount = sandbox_sessions_mount(harness.config.sessions_root, CODE_SANDBOX_FAMILY)

    assert harness.new_session_params(CODE_SANDBOX)["cwd"] == mount
    await harness.stop()


async def test_the_chat_directory_outlives_the_job(tmp_path: Path) -> None:
    """§12.1 rule 4. The directory belongs to the chat, not to the job."""
    harness = DelegateHarness(tmp_path)
    await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY))

    made = owner_dir(harness.config.work_root, CHAT_SESSION)
    (made / "clean.py").write_text("import pandas", encoding="utf-8")

    await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY))

    assert (made / "clean.py").read_text(encoding="utf-8") == "import pandas"
    await harness.stop()


async def test_the_delegation_rides_on_the_prompt(tmp_path: Path) -> None:
    """Contract 04 §6.3's chain. The prompt's `_meta` carries the id."""
    harness = DelegateHarness(tmp_path)
    delegation_id = new_ulid()
    await harness.delegate(harness.call(delegation_id=delegation_id))

    sent = {"id": delegation_id, "caller_session": CHAT_SESSION}

    assert harness.prompt_meta() == {meta_key("delegation"): sent}
    assert set(sent) == value_fields("delegation")
    await harness.stop()


async def test_a_call_with_no_caller_session_still_sends_the_delegation_id(
    tmp_path: Path,
) -> None:
    """Contract 04 §7.3 makes `claimed_session_id` advisory. The id still goes."""
    harness = DelegateHarness(tmp_path)
    delegation_id = new_ulid()
    await harness.delegate(harness.call(owner=None, delegation_id=delegation_id))

    assert harness.prompt_meta() == {
        meta_key("delegation"): {"id": delegation_id, "caller_session": None}
    }
    await harness.stop()


async def test_two_jobs_share_the_thin_familys_link(tmp_path: Path) -> None:
    """`docs/rework/spec.md` §7.1 and §7.4. No limit and no queue for a thin family."""
    harness = DelegateHarness(tmp_path, mode=PromptMode.HOLD)
    first = asyncio.create_task(harness.delegate(harness.call()))
    second = asyncio.create_task(harness.delegate(harness.call()))
    await wait_until(lambda: ORACLE_SANDBOX in harness.facades.facades)
    await wait_until(lambda: len(harness.facade().held) == 2)

    for session in list(harness.facade().held):
        await harness.facades.settle(ORACLE_SANDBOX, session)

    bodies = await asyncio.gather(first, second)

    assert [body["status"] for body in bodies] == [JobStatus.OK.value] * 2
    assert bodies[0]["session_id"] != bodies[1]["session_id"]
    assert harness.facades.dials == [ORACLE_SANDBOX]
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
