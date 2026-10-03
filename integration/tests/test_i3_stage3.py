"""Packet I3: stage 3, the first call that leaves one family and lands in
another, with every process on the path the real one.

This is the operator's stage 3 test made runnable. Three families are published by
the real `managerd` from checked-in registry files. A scripted model in the
`chat` sandbox calls `invoke_agent` through the REAL PEP bridge. The REAL
PEP checks the grant file, mints a delegation id and calls the REAL
`attendance` over its Unix socket. `attendance` runs one job session in the thin
family's own sandbox and deletes it afterwards.

    bridge (chat's model) ──► PEP ──► attendance ──► job-<ulid> ──► playpen
                               │                                     │
                        grants/chat.json                        fake-pi.mjs
                               │
                        audit/<day>.jsonl

Three fakes, none under test: `FakeDriver` (a Mac has no `sbx`),
`FakeLiteLLMKeys` (no test mints a key) and `fake-pi.mjs` (the playpen
package's own double). Everything from the family file to the wrapped
answer is real code.
"""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from agent_managerd import paths as managerd_paths
from agent_pep.delegate import DoorTokenError, read_door_token
from agent_pep.family_ids import ULID_RE
from attendance.auth import Principal, TokenBook, TokenError
from conftest import chat_id, session_of
from stack import Stack, until
from stage3 import (
    CHAT,
    CODE_SANDBOX,
    DELEGATE_TOKEN_MODE,
    LEFT_BY_PREFIX,
    VAULT_ORACLE,
    Stage3,
    bridge_bundle_missing,
    serving_pep,
)

INVOKE_AGENT = "invoke_agent"
HTTP_OK = 200
HTTP_CREATED = 201

#: What the scripted model asks a delegate. `fake-pi.mjs` echoes the prompt
#: into its deltas, so a distinctive string proves the message crossed four
#: process boundaries and came back. Short on purpose: the fake slices a
#: prompt at 24 characters, so a longer one loses whatever tells two
#: concurrent calls apart.
QUESTION = "dropped-sensor"

#: Contract 01 §3.6.1's default, which the fixture registry leaves unset. The
#: family file carries the field now (packet MD), and
#: `test_md_inflight.py` covers a family that raises it.
MAX_INFLIGHT_DELEGATIONS = 2

#: A job turn that cannot finish inside a one-second limit: 200 deltas eight
#: milliseconds apart is at least 1.6 seconds of turn.
SLOW_TURN_EVENTS = 200
SLOW_TURN_GAP_MS = 8
SHORT_JOB_TIMEOUT = "1s"

#: Long enough for three job sessions to appear and go on a loaded Mac.
SETTLE_TIMEOUT_S = 120.0

_WORLD_READABLE = 0o644
_GROUP_WRITABLE = 0o660


pytestmark = pytest.mark.skipif(
    bridge_bundle_missing(),
    reason="playpen/dist/pep-bridge.js is missing: run `pnpm install && pnpm build`",
)


@pytest.fixture
async def stage(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stage3]:
    """Three families published by the real manager, then a serving stack."""
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()
    ready = Stage3(built)

    # The stack writes `chat` a hand-made `creds.json` for stage 1. An apply
    # that finds one REFRESHES its LiteLLM key rather than minting, and this
    # fake never minted that one, so the family would come up degraded with
    # no grant file. Removing it puts all three families on one path.
    managerd_paths.creds_path(built.state_root, CHAT).unlink(missing_ok=True)
    for result in ready.apply_all():
        assert result.ok, result.status.faults

    # Stage 1's one sessions-root override names ONE root for every family,
    # so with three families every thin job would run in `chat`'s directory.
    monkeypatch.delenv("SESSIOND_SANDBOX_SESSIONS_MOUNT", raising=False)
    for name, value in ready.sandbox_environ().items():
        monkeypatch.setenv(name, value)

    await built.serve()

    try:
        yield ready
    finally:
        await built.close()


@pytest.fixture
def delegating(stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Stage3]:
    """The same stack with the real PEP in front of it."""
    with serving_pep(stage, monkeypatch, tmp_path) as ready:
        yield ready


def ask(target: str, message: str = QUESTION) -> dict[str, object]:
    """One `invoke_agent` call's arguments (contract 04 §4.1)."""
    return {"family": target, "message": message}


def delegate_lines(stage: Stage3) -> list[dict[str, object]]:
    """Every audit line the PEP wrote for a delegate call (contract 04 §6)."""
    return [line for line in stage.audit_lines() if line.get("tool") == INVOKE_AGENT]


def is_ulid(value: str) -> bool:
    """Contract 04 §4.1's alphabet, which the PEP's own reader applies."""
    return ULID_RE.match(value) is not None


async def run_and_read_turn_file(stage: Stage3, session: str) -> dict[str, object]:
    """One delegate call, and the turn file the job ran under.

    That file is the only place the minted id is written down outside the
    PEP's memory, and the playpen removes it with the job session, so the
    job turn is slowed down enough to read the file while it still exists.
    """
    stage.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)
    running = asyncio.create_task(stage.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session))

    try:
        await until(
            lambda: stage.job_turn_files(VAULT_ORACLE) != [],
            "the job's turn file",
            timeout=SETTLE_TIMEOUT_S,
        )
        body: dict[str, object] = json.loads(
            stage.job_turn_files(VAULT_ORACLE)[0].read_text(encoding="utf-8")
        )
    finally:
        call = await running

    assert call.ok, call.error
    stage.stack.set_pi_env()

    return body


# --- 1. chat calls vault-oracle through the PEP -----------------------------


async def test_a_delegate_call_answers_the_caller(delegating: Stage3) -> None:
    """The whole path: grant file, delegate door, one job turn, one answer.

    The question travels as the job's prompt and `fake-pi.mjs` echoes it, so
    finding it in the answer proves the message crossed every boundary.
    """
    session = session_of(chat_id())

    call = await delegating.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session)

    assert call.ok, call.error
    assert QUESTION in call.text


async def test_the_answer_reaches_the_model_as_untrusted(delegating: Stage3) -> None:
    """Contract 04 §7.6 and invariant 14. A delegated answer is data."""
    session = session_of(chat_id())

    call = await delegating.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session)

    assert call.ok, call.error
    assert call.text.startswith(f'[untrusted output from "{INVOKE_AGENT}"')
    assert VAULT_ORACLE in call.text


async def test_the_job_session_is_gone_afterwards(delegating: Stage3) -> None:
    """Contract 02 §12 rules 5 and 6, invariant 15. A job leaves the audit
    record and nothing else: no session directory, no scratch."""
    session = session_of(chat_id())

    call = await delegating.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session)

    assert call.ok, call.error
    assert delegating.sessions_of(VAULT_ORACLE) == []


async def test_the_audit_holds_the_call_and_its_chain(delegating: Stage3) -> None:
    """Contract 04 §6.3. The caller's own line is a chain of one.

    The chat turn carried no delegation id, and §6.3 says a call with no
    known id has a chain of one entry, this family. The two-entry chain
    belongs to the calls the DELEGATE makes, which the next two tests reach.
    """
    session = session_of(chat_id())

    await delegating.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session)

    line = delegate_lines(delegating)[-1]
    assert line["decision"] == "allow"
    assert line["chain"] == [CHAT]
    claimed = line["claimed"]
    assert isinstance(claimed, dict)
    assert claimed["session_id"] == session


async def test_the_minted_delegation_reaches_the_job(delegating: Stage3) -> None:
    """Contract 04 §7.4 and contract 03 §7.4 rule 4 item 5.

    The PEP mints the id, `attendance` puts it on `start_turn`, and the
    playpen writes it into the job's turn file, where that sandbox's
    bridge reads it. Packet FX fixed the case this covers: a delegation used
    to be dropped whole when the PEP sent no caller session, which lost the
    id as well.
    """
    session = session_of(chat_id())

    record = await run_and_read_turn_file(delegating, session)

    assert is_ulid(str(record["delegation"]))
    assert record["caller_session"] == session


async def test_a_delegate_call_of_its_own_carries_the_chain(delegating: Stage3) -> None:
    """Contract 04 §6.3's own example, made real.

    The delegate's sandbox presents the id the PEP minted. The PEP looks it
    up in its own table, so `chain` names both families. A value the PEP
    never minted would name a chain of one, which is what keeps the header
    from becoming authority (§3.1 rule 2).
    """
    session = session_of(chat_id())
    minted = str((await run_and_read_turn_file(delegating, session))["delegation"])

    call = await delegating.call_tool(
        "embed",
        {"input": QUESTION},
        session=f"job-{minted}",
        family=VAULT_ORACLE,
        delegation=minted,
    )

    assert call.ok, call.error
    line = next(one for one in delegating.audit_lines() if one.get("tool") == "embed")
    assert line["chain"] == [CHAT, VAULT_ORACLE]


# --- 2. one chat, two code-sandbox jobs, one directory ----------------------


async def test_two_jobs_of_one_chat_share_a_directory(delegating: Stage3) -> None:
    """Contract 02 §12.1 and `docs/rework/spec.md` §7.2. A mount cannot join
    a running sandbox, so the family mounts the work root and each chat gets
    a directory under it, made at that chat's first delegate call."""
    session = session_of(chat_id())

    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)
    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)

    owned = delegating.owner_dir(session)
    assert delegating.job_cwds() == [str(owned.resolve()), str(owned.resolve())]


async def test_the_second_job_reads_what_the_first_left(delegating: Stage3) -> None:
    """The point of the shared directory. The first job's file is there for
    the second, read from inside the job rather than from this process."""
    session = session_of(chat_id())

    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)
    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)

    first, second = delegating.job_findings()
    assert first == ()
    assert len(second) == 1
    assert second[0].startswith(LEFT_BY_PREFIX)


async def test_the_directory_survives_both_jobs(delegating: Stage3) -> None:
    """Contract 02 §12.1 rule 4. The directory belongs to the CHAT, so it
    outlives every job that ever worked in it."""
    session = session_of(chat_id())

    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)
    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)

    owned = delegating.owner_dir(session)
    assert owned.is_dir()
    assert len(list(owned.glob(f"{LEFT_BY_PREFIX}*"))) == 2
    assert delegating.sessions_of(CODE_SANDBOX) == []


async def test_the_job_works_through_the_link_the_operator_named(delegating: Stage3) -> None:
    """The path of `docs/rework/spec.md` §7.2. The playpen derives the
    target from its own mount, so the host never sends one and a forged
    message cannot aim it."""
    session = session_of(chat_id())

    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)

    link = delegating.link_path(session)
    assert link.is_symlink() is False or link.resolve() == delegating.owner_dir(session).resolve()


# --- 3. a second chat gets its own directory --------------------------------


async def test_two_chats_get_two_directories(delegating: Stage3) -> None:
    """Contract 02 §12.1 rule 1. The directory is keyed by the calling chat,
    so a second chat works somewhere else and finds none of the first's
    files."""
    first = session_of(chat_id())
    second = session_of(chat_id())

    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=first)
    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=second)

    assert delegating.owner_dir(first) != delegating.owner_dir(second)
    assert delegating.job_cwds() == [
        str(delegating.owner_dir(first).resolve()),
        str(delegating.owner_dir(second).resolve()),
    ]
    assert delegating.job_findings() == [(), ()]


async def test_a_claimed_owner_outside_the_grammar_gets_none(delegating: Stage3) -> None:
    """Contract 02 §12.1 and contract 04 §3.1. The owner id arrives in a
    header, so it is checked twice before it becomes a path: the PEP drops a
    value outside the session grammar, and `attendance` refuses one that
    reached it anyway. Neither step builds a path from `..`.
    """
    escape = "../../etc"

    call = await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=escape)

    assert call.ok, call.error
    # No work root at all, because the PEP sent no owner: contract 03 §7.2
    # then gives the job no workspace, and it runs in its own session dir.
    assert (delegating.work_root / CODE_SANDBOX).exists() is False
    assert delegating.job_cwds() != []
    assert escape not in "".join(delegating.job_cwds())


# --- 4. deleting the chat deletes its directory -----------------------------


async def test_deleting_the_chat_deletes_its_directory(delegating: Stage3) -> None:
    """Contract 02 §5.8's last row and §12.1 rule 4. The work directory is
    keyed by the session that OWNS it, so the chat's delete takes it and a
    job's delete never did."""
    session = session_of(chat_id())
    owui = delegating.stack.attendance_as(Principal.DOOR_OWUI)
    created = await owui.post("/v1/sessions", json={"family": CHAT, "session": session})
    assert created.status_code == HTTP_CREATED, created.text

    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)
    assert delegating.owner_dir(session).is_dir()

    deleted = await owui.delete(f"/v1/sessions/{CHAT}/{session}")

    assert deleted.status_code == HTTP_OK, deleted.text
    assert delegating.owner_dir(session).is_dir() is False


# --- 5. a family without the delegate in its grant file ---------------------


async def test_an_empty_delegates_list_hides_the_tool(
    stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Contract 04 §4 rule 1. `invoke_agent` is synthesized from `delegates`,
    so a family with none never sees the tool at all.

    Nothing may reach `attendance`: no job session, no pi process.
    """
    rewritten = stage.rewrite_family(CHAT, tmp_path / "narrowed", delegates=[])
    stage.apply_from(rewritten, CHAT)
    before = len(stage.job_cwds())

    with serving_pep(stage, monkeypatch, tmp_path) as ready:
        call = await ready.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session_of(chat_id()))

    assert INVOKE_AGENT not in call.registered
    assert call.ok is False
    assert stage.sessions_of(VAULT_ORACLE) == []
    assert len(stage.job_cwds()) == before


async def test_a_target_outside_delegates_is_refused(
    stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Contract 04 §7.2 row 4. The tool is granted, the target is not.

    The grant file is the whole of the reach and the PEP re-reads it per
    call, so this edit lands with no restart and nothing reaches `attendance`.
    """
    rewritten = stage.rewrite_family(CHAT, tmp_path / "one-delegate", delegates=[CODE_SANDBOX])
    stage.apply_from(rewritten, CHAT)
    before = len(stage.job_cwds())

    with serving_pep(stage, monkeypatch, tmp_path) as ready:
        call = await ready.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session_of(chat_id()))

    assert INVOKE_AGENT in call.registered
    assert call.ok is False
    assert "tool_not_granted" in call.error
    assert stage.sessions_of(VAULT_ORACLE) == []
    assert len(stage.job_cwds()) == before


# --- 6. a job that runs past its limit --------------------------------------


async def test_a_job_past_its_limit_ends_in_timeout(
    stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Contract 02 §12 rule 3 and contract 04 §7.5. The family file's
    `job.timeout` is the turn's deadline, the answer is `timeout` rather
    than `failed`, and the caller reads a tool error.

    The job session goes whatever happened (§12 rules 5 and 6), including
    when it never answered.
    """
    rewritten = stage.rewrite_family(
        VAULT_ORACLE, tmp_path / "impatient", job={"timeout": SHORT_JOB_TIMEOUT}
    )
    stage.apply_from(rewritten, VAULT_ORACLE)
    stage.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)

    with serving_pep(stage, monkeypatch, tmp_path) as ready:
        call = await ready.call_tool(INVOKE_AGENT, ask(VAULT_ORACLE), session=session_of(chat_id()))

    assert call.ok is False
    assert call.error
    assert stage.sessions_of(VAULT_ORACLE) == []
    line = delegate_lines(stage)[-1]
    assert line["decision"] == "allow"
    # Contract 04 §5 row 11: a failure after an allow, with its own reason so
    # a job that ran out of time never reads like a broken upstream.
    assert line["reason"] == "delegate_timeout"


# --- 7. several delegate calls at once --------------------------------------


async def test_two_delegate_calls_at_once_both_finish(delegating: Stage3) -> None:
    """A thin family has no turn limit and one sandbox serves every job, so
    two jobs run side by side in one VM (`docs/rework/spec.md` §7.1,
    contract 02 §12)."""
    session = session_of(chat_id())
    delegating.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)

    calls = await asyncio.gather(
        *(
            delegating.call_tool(
                INVOKE_AGENT, ask(VAULT_ORACLE, f"{QUESTION}-{n}"), session=session
            )
            for n in range(MAX_INFLIGHT_DELEGATIONS)
        )
    )

    assert [one.ok for one in calls] == [True] * MAX_INFLIGHT_DELEGATIONS
    for n, one in enumerate(calls):
        assert f"{QUESTION}-{n}" in one.text

    assert delegating.sessions_of(VAULT_ORACLE) == []
    assert len(delegating.job_cwds()) == MAX_INFLIGHT_DELEGATIONS


async def test_a_third_call_at_once_is_rate_limited(delegating: Stage3) -> None:
    """Contract 04 §1.2 caps ONE caller family at its own
    `max_inflight_delegations`, and this fixture family leaves it at the
    default of 2.

    The third is refused by the PEP before `attendance` sees it. A family that
    sets contract 01 §3.6.1's field higher runs three at once: see
    `test_md_inflight.py`.
    """
    session = session_of(chat_id())
    delegating.stack.set_pi_env(events=SLOW_TURN_EVENTS, delay_ms=SLOW_TURN_GAP_MS)
    at_once = MAX_INFLIGHT_DELEGATIONS + 1

    calls = await asyncio.gather(
        *(
            delegating.call_tool(
                INVOKE_AGENT, ask(VAULT_ORACLE, f"{QUESTION}-{n}"), session=session
            )
            for n in range(at_once)
        )
    )

    refused = [one for one in calls if not one.ok]
    assert len(refused) == 1
    assert "rate_limited" in refused[0].error
    assert len(delegating.job_cwds()) == MAX_INFLIGHT_DELEGATIONS


async def test_three_calls_finish_when_they_are_paced(delegating: Stage3) -> None:
    """The same three calls, two at a time, all finish. The cap bounds what
    is in flight, never how many a chat may make."""
    session = session_of(chat_id())
    answers: list[str] = []

    for start in range(0, 3, MAX_INFLIGHT_DELEGATIONS):
        batch = await asyncio.gather(
            *(
                delegating.call_tool(
                    INVOKE_AGENT, ask(VAULT_ORACLE, f"{QUESTION}-{n}"), session=session
                )
                for n in range(start, min(start + MAX_INFLIGHT_DELEGATIONS, 3))
            )
        )
        for one in batch:
            assert one.ok, one.error
            answers.append(one.text)

    assert len(answers) == 3
    assert delegating.sessions_of(VAULT_ORACLE) == []


# --- 8. the delegate token's mode ------------------------------------------


def test_the_delegate_token_is_deployed_group_readable(stage: Stage3) -> None:
    """Contract 02 §3 rule 5's one exception. The PEP runs as user `pep` and
    `attendance` owns the file, so group read is what opens the door at all."""
    path = stage.open_delegate_door()

    assert stat.S_IMODE(path.stat().st_mode) == DELEGATE_TOKEN_MODE
    assert read_door_token(path)


def test_attendance_accepts_only_the_delegate_at_0640(stage: Stage3) -> None:
    """The exception is one file wide. Every other principal stays 0600.

    The harness cannot change users, so this asserts on the code path that
    decides, which is the one a wrong mode on the host would meet.
    """
    tokens = stage.stack.state_root / "tokens"
    stage.open_delegate_door()
    TokenBook(stage.stack.state_root).load()

    (tokens / f"{Principal.DOOR_OWUI.value}.token").chmod(DELEGATE_TOKEN_MODE)
    with pytest.raises(TokenError) as refused:
        TokenBook(stage.stack.state_root).load()

    assert Principal.DOOR_OWUI.value in str(refused.value)
    assert "0600" in str(refused.value)


@pytest.mark.parametrize("mode", [_WORLD_READABLE, _GROUP_WRITABLE])
def test_a_wider_delegate_token_is_refused(stage: Stage3, mode: int) -> None:
    """Group read and nothing past it (contract 02 §3 rule 5)."""
    path = stage.open_delegate_door()
    path.chmod(mode)

    with pytest.raises(TokenError) as refused:
        TokenBook(stage.stack.state_root).load()

    assert Principal.DOOR_DELEGATE.value in str(refused.value)
    assert "0600 or 0640" in str(refused.value)


def test_a_token_the_pep_cannot_read_names_the_path_only(tmp_path: Path) -> None:
    """The PEP's own reader, which is what a 0600 file meets when the PEP
    runs as another user. The message names the path and never the value
    (invariant 13)."""
    missing = tmp_path / "door-delegate.token"

    with pytest.raises(DoorTokenError) as refused:
        read_door_token(missing)

    assert str(missing) in str(refused.value)
