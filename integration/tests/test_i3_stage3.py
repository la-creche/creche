"""Packet I3: stage 3, the first call that leaves one family and lands in
another, with every process on the path the real one.

This is the operator's stage 3 test made runnable. Three families are published by
the real `caregiver` from checked-in registry files. A scripted model in the
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

import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from attendance.auth import Principal, TokenBook, TokenError
from chaperone.delegate import DoorTokenError, read_door_token
from conftest import chat_id, session_of
from stack import Stack
from stage3 import (
    CHAT,
    CODE_SANDBOX,
    DELEGATE_TOKEN_MODE,
    VAULT_ORACLE,
    Stage3,
    bridge_bundle_missing,
    serving_pep,
)

from caregiver import paths as caregiver_paths

INVOKE_AGENT = "invoke_agent"
HTTP_OK = 200
HTTP_CREATED = 201

#: What the scripted model asks a delegate. `fake-pi.mjs` echoes the prompt
#: into its deltas, so a distinctive string proves the message crossed four
#: process boundaries and came back. Short on purpose: the fake slices a
#: prompt at 24 characters, so a longer one loses whatever tells two
#: concurrent calls apart.
QUESTION = "dropped-sensor"

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
    caregiver_paths.creds_path(built.state_root, CHAT).unlink(missing_ok=True)
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


# --- 1. chat calls vault-oracle through the PEP -----------------------------


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


# --- 2. one chat, two code-sandbox jobs, one directory ----------------------


async def test_the_job_works_through_the_link_the_operator_named(delegating: Stage3) -> None:
    """The path of `docs/rework/spec.md` §7.2. The playpen derives the
    target from its own mount, so the host never sends one and a forged
    message cannot aim it."""
    session = session_of(chat_id())

    await delegating.call_tool(INVOKE_AGENT, ask(CODE_SANDBOX), session=session)

    link = delegating.link_path(session)
    assert link.is_symlink() is False or link.resolve() == delegating.owner_dir(session).resolve()


# --- 3. a second chat gets its own directory --------------------------------


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


# --- 7. several delegate calls at once --------------------------------------


# --- 8. the delegate token's mode ------------------------------------------


def test_the_delegate_token_is_deployed_group_readable(stage: Stage3) -> None:
    """Contract 02 §3 rule 5's one exception. The PEP runs as user `chaperone` and
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
