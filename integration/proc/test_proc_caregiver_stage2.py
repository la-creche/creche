"""The stage 2 scenarios, with `caregiver` as a process.

`integration/tests/test_i2_stage2.py` runs one reconcile pass of `caregiver`
inside the test process, with a fake driver and a fake LiteLLM. Here every
service of the house is a process, and one edit to one registry file is the
only action of a scenario (invariant 9). Nothing tells `caregiver` to look.

Each scenario keeps the name of the old one. Each assertion reads the status
document, the grant file, the record of a stand-in, the journal of a session
or an HTTP answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

import httpx
from proc_caregiver import (
    CREATING,
    DEGRADED,
    INVALID,
    PLANE_ENDPOINTS,
    READY,
    CaregiverStack,
)
from proc_chat import (
    TURN_SETTLED,
    TURN_STARTED,
    await_settled,
    chat_id,
    run_stream,
    session_of,
    until,
)
from proc_standins import ALLOW, SBX, sbx_rows, sbx_sandboxes, set_pi_env, tune, untune
from proc_tree import (
    FAMILY,
    FAULTS_OF_ATTENDANCE,
    SANDBOX,
    Tree,
    family_body,
    write_family_file,
    write_family_prose,
    write_registry_file,
)

MANIFEST_PATH = "/manifest"
CALL_PATH = "/call"

NEXT_SANDBOX = "chat-s2"

#: Raised from 2, the default of contract 01 §3.9. A raise is an addition:
#: the first row of contract 05 §5.4, which drains.
MORE_CPUS = 4

#: A read-only mount under an allowed root of contract 01 §5.4. Its removal
#: is the second row of contract 05 §5.4, which interrupts. The `sbx`
#: stand-in opens no mount, so the directory does not exist.
A_MOUNT = "/srv/agents/work/projects/notes"

EMBED = "embed"
HA_CALL = "ha_call"
HA_ALLOW: list[dict[str, str]] = [{"domain": "notify", "service": "mobile_app_example_phone"}]
HA_ARGS: dict[str, str] = dict(HA_ALLOW[0])

NEW_INSTRUCTIONS = "Answer only in metric units.\n"

TURN_ABORTED = "turn_aborted"
NOTE = "note"
SWITCH_NOTE = "sandbox_switched"
DRAIN = "drain"

#: A turn long enough to replace a sandbox inside: 200 deltas, 25 ms apart.
#: `caregiver` needs about one second from the edit to the switch call on a
#: laptop. A scenario fails when the call comes after the end of the turn.
LONG_TURN = {"events": 200, "delay_ms": 25}
SHORT_TURN = {"events": 3, "delay_ms": 1}

STARTED_DEADLINE_S = 30.0
PASS_DEADLINE_S = 30.0

#: `caregiver` tries a refused switch again when the status document is due:
#: 20 seconds after the last pass. No argument makes that time shorter.
RETRY_DEADLINE_S = 60.0
EXIT_DEADLINE_S = 30.0

HTTP_OK = 200
HTTP_FORBIDDEN = 403


# --- 1. a tool added --------------------------------------------------------


async def test_an_added_tool_reaches_the_next_manifest(house_prepared: CaregiverStack) -> None:
    """Contract 05 §3.5 row 1: a tool change lands live, for each call.

    The chaperone reads the grant file at each call (contract 04 §1.4). So
    the edit reaches the next manifest with no sandbox touched and no
    session lost.
    """
    house = house_prepared
    write_family_file(house.tree, family_body(verbs={EMBED: {}}))
    house.start_house()
    house.await_serving()
    chat = chat_id()
    first_rev = house.grants()["rev"]

    async with house.door_client() as door, house.sandbox_client() as sandbox:
        assert (await run_stream(door, chat, "one")).error_chunks == []
        assert _tools(await sandbox.get(MANIFEST_PATH)) == [EMBED]
        before = house.sbx_commands()

        write_family_file(house.tree, family_body(verbs={EMBED: {}, HA_CALL: {"allow": HA_ALLOW}}))
        await until(lambda: house.grants()["rev"] != first_rev, "a new grant file", PASS_DEADLINE_S)
        manifest = await sandbox.get(MANIFEST_PATH)

        assert _tools(manifest) == [EMBED, HA_CALL]
        assert manifest.json()["rev"] == house.grants()["rev"]
        assert house.sbx_commands() == before, "a tool change ran an sbx command"
        assert house.sandboxes() == [(SANDBOX, READY)]

        # The session is the same one, and it still answers.
        assert (await run_stream(door, chat, "two")).error_chunks == []

    await await_settled(house.tree, session_of(chat), 2)


# --- 2. a tool removed ------------------------------------------------------


async def test_a_removed_tool_is_refused_on_the_next_call(house_prepared: CaregiverStack) -> None:
    """Invariant 9: a removal applies at once.

    One chaperone process decides both calls. Between them only the grant
    file changes: `caregiver` writes it again from the edited family file.
    The first call passes the decision and fails later, because this
    chaperone holds no Home Assistant token. The audit record holds the
    decision (contract 04 §5 row 11).
    """
    house = house_prepared
    both = {EMBED: {}, HA_CALL: {"allow": HA_ALLOW}}
    write_family_file(house.tree, family_body(verbs=both))
    house.start_house()
    house.await_serving()
    assert house.chaperone is not None
    first_rev = house.grants()["rev"]

    async with house.sandbox_client() as sandbox:
        allowed = await _call(sandbox, HA_CALL, HA_ARGS)
        assert _decisions(house.tree, HA_CALL) == ["allow"], allowed.text

        write_family_file(house.tree, family_body(verbs={EMBED: {}}))
        await until(lambda: house.grants()["rev"] != first_rev, "a new grant file", PASS_DEADLINE_S)
        refused = await _call(sandbox, HA_CALL, HA_ARGS)
        manifest = await sandbox.get(MANIFEST_PATH)

    assert refused.status_code == HTTP_FORBIDDEN
    assert refused.json()["reason"] == "tool_not_granted"
    assert _decisions(house.tree, HA_CALL) == ["allow", "deny"]
    assert _tools(manifest) == [EMBED]
    assert house.chaperone.exit_code() is None, "the chaperone restarted or ended"


# --- 3. the instructions changed --------------------------------------------


async def test_new_instructions_recycle_at_the_turn_boundary(
    house: CaregiverStack, house_door: httpx.AsyncClient
) -> None:
    """Contract 01 §6.1 and contract 03 §6. `config_rev` moves, and the next turn gets a new pi.

    The edit alone starts no process. The instructions reach pi as a path
    on its command line (contract 03 §7.1), so the record of the pi stand-in
    shows the path and never the text.
    """
    instructions = house.tree.mounts().config / "instructions.md"
    chat = chat_id()
    session = session_of(chat)
    assert (await run_stream(house_door, chat, "one")).error_chunks == []
    await await_settled(house.tree, session)
    before = len(house.pi_starts())
    first_rev = _status(house)["config_rev"]

    write_family_prose(house.tree, text=NEW_INSTRUCTIONS)
    await until(
        lambda: _status(house)["config_rev"] != first_rev and house.serves(),
        "a new config_rev",
        PASS_DEADLINE_S,
    )

    assert len(house.pi_starts()) == before, "the edit alone started a pi process"
    assert instructions.read_text(encoding="utf-8") == NEW_INSTRUCTIONS

    assert (await run_stream(house_door, chat, "two")).error_chunks == []
    await await_settled(house.tree, session, 2)

    starts = house.pi_starts()
    assert len(starts) == before + 1
    assert str(instructions) in starts[-1].argv
    assert NEW_INSTRUCTIONS.strip() not in " ".join(starts[-1].argv)


# --- 4. a replace-class addition --------------------------------------------


async def test_more_cpus_drains_onto_a_second_sandbox(
    house: CaregiverStack, house_door: httpx.AsyncClient
) -> None:
    """Contract 05 §4.3, §5.3 rule 2 and §4.4, on two real playpens.

    A running turn finishes on the outgoing sandbox. The next turn runs on
    the incoming one. `caregiver` destroys the outgoing sandbox with its
    policy rows, and the session and its journal stay.
    """
    set_pi_env(house.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)
    running = asyncio.create_task(run_stream(house_door, chat, "one"))
    await _started(house.tree, session, 1)

    write_family_file(house.tree, family_body(sandbox={"cpus": MORE_CPUS}))
    drained = await running

    # The drain waited for the turn, and the turn finished.
    assert drained.ends_with_done
    assert drained.error_chunks == []
    await until(lambda: house.serves(sandbox=NEXT_SANDBOX), f"{NEXT_SANDBOX} to serve")

    assert list(sbx_sandboxes(house.tree)) == [NEXT_SANDBOX]
    assert sbx_sandboxes(house.tree)[NEXT_SANDBOX]["cpus"] == MORE_CPUS
    assert ("rm", "-f", SANDBOX) in house.sbx_commands()
    # Contract 05 §4.4 step 3: `sbx rm` leaves the rows, so `caregiver` removes each one.
    assert _plane_rows(house.tree, SANDBOX) == []
    assert _plane_rows(house.tree, NEXT_SANDBOX) == sorted(PLANE_ENDPOINTS)

    set_pi_env(house.tree, **SHORT_TURN)
    assert (await run_stream(house_door, chat, "two")).error_chunks == []
    await await_settled(house.tree, session, 2)

    assert _sandboxes_of(house.tree, session) == [SANDBOX, NEXT_SANDBOX]
    kinds = house.tree.journal_kinds(session)
    assert kinds.count(TURN_SETTLED) == 2
    # Contract 05 §5.3 rule 6: one note in the journal, with why the turn moved.
    notes = _switch_notes(house.tree, session)
    assert [(note["from"], note["to"], note["mode"]) for note in notes] == [
        (SANDBOX, NEXT_SANDBOX, DRAIN)
    ]
    assert notes[0]["reason"]
    # The call came during the turn: the note is before the end of the turn.
    assert kinds.index(NOTE) < kinds.index(TURN_SETTLED)


# --- 5. a replace-class removal ---------------------------------------------


async def test_a_removed_mount_interrupts_the_running_turn(house_prepared: CaregiverStack) -> None:
    """Contract 05 §5.3 rule 3. A turn that holds the removed reach does not finish.

    The abort is immediate, and the client is told why. The incoming sandbox
    has no mount of the removed path.
    """
    house = house_prepared
    write_family_file(house.tree, family_body(files=[{"path": A_MOUNT, "mode": "ro"}]))
    house.start_house()
    house.await_serving()
    assert {"path": A_MOUNT, "readonly": True} in sbx_sandboxes(house.tree)[SANDBOX]["mounts"]
    set_pi_env(house.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)

    async with house.door_client() as door:
        running = asyncio.create_task(run_stream(door, chat, "one"))
        await _started(house.tree, session, 1)

        write_family_file(house.tree, family_body())
        interrupted = await running

        assert interrupted.error_codes == ["permission_removed"]
        await until(
            lambda: TURN_ABORTED in house.tree.journal_kinds(session), f"{session} to be aborted"
        )
        assert TURN_SETTLED not in house.tree.journal_kinds(session)
        await until(lambda: house.serves(sandbox=NEXT_SANDBOX), f"{NEXT_SANDBOX} to serve")
        assert A_MOUNT not in [
            mount["path"] for mount in sbx_sandboxes(house.tree)[NEXT_SANDBOX]["mounts"]
        ]

        set_pi_env(house.tree, **SHORT_TURN)
        assert (await run_stream(door, chat, "two")).error_chunks == []

    await await_settled(house.tree, session)
    assert _sandboxes_of(house.tree, session) == [SANDBOX, NEXT_SANDBOX]


# --- 6. a switch that cannot complete ---------------------------------------


async def test_a_switch_that_cannot_complete_keeps_serving(
    house: CaregiverStack, house_door: httpx.AsyncClient
) -> None:
    """Contract 05 §5.3 rule 8: the handshake runs before anything moves.

    The incoming sandbox gets none of its mounts, so the real playpen answers
    `fatal` and not `ready` (contract 03 §5.7). Nothing moves. The family
    answers on the sandbox it served on, `caregiver` destroys nothing, and
    the status document names the sandbox that cannot start (contract 05
    §3.3.1).
    """
    chat = chat_id()
    session = session_of(chat)
    assert (await run_stream(house_door, chat, "one")).error_chunks == []
    await await_settled(house.tree, session)

    tune(house.tree, SBX, f"no-mounts/{NEXT_SANDBOX}")
    write_family_file(house.tree, family_body(sandbox={"cpus": MORE_CPUS}))
    await until(lambda: house.state() == DEGRADED, f"{FAMILY} to be {DEGRADED}", PASS_DEADLINE_S)

    # Both sandboxes stay, in the document and at the `sbx` stand-in.
    assert house.sandboxes() == [(SANDBOX, READY), (NEXT_SANDBOX, CREATING)]
    assert list(sbx_sandboxes(house.tree)) == [SANDBOX, NEXT_SANDBOX]
    assert not [command for command in house.sbx_commands() if command[0] == "rm"]

    # `attendance` reports the sandbox in its fault file (contract 05 §3.3.1 rule 1).
    assert [(fault["code"], fault["sandbox"]) for fault in _attendance_faults(house.tree)] == [
        ("sandbox_start_failed", NEXT_SANDBOX)
    ]
    # `caregiver` folds the report into the document (rule 4). The fault is of
    # the incoming sandbox, and the outgoing one answers each turn.
    fault = _one_fault(_status(house), "sandbox_start_failed")
    assert fault["sandbox"] == NEXT_SANDBOX
    assert fault["blocks_turns"] is False

    assert (await run_stream(house_door, chat, "two")).error_chunks == []
    await await_settled(house.tree, session, 2)
    assert _sandboxes_of(house.tree, session) == [SANDBOX, SANDBOX]


async def test_a_later_pass_completes_the_refused_switch(
    house: CaregiverStack, house_door: httpx.AsyncClient
) -> None:
    """The other half of scenario 6. Nobody acts, and `caregiver` tries again.

    The incoming sandbox gets its mounts back. A later pass makes the same
    call, and it makes no third sandbox (contract 05 §5.3 rule 8).
    """
    chat = chat_id()
    session = session_of(chat)
    assert (await run_stream(house_door, chat, "one")).error_chunks == []
    await await_settled(house.tree, session)

    tune(house.tree, SBX, f"no-mounts/{NEXT_SANDBOX}")
    write_family_file(house.tree, family_body(sandbox={"cpus": MORE_CPUS}))
    await until(lambda: house.state() == DEGRADED, f"{FAMILY} to be {DEGRADED}", PASS_DEADLINE_S)

    untune(house.tree, SBX, f"no-mounts/{NEXT_SANDBOX}")
    await until(
        lambda: house.serves(sandbox=NEXT_SANDBOX), f"{NEXT_SANDBOX} to serve", RETRY_DEADLINE_S
    )

    # Contract 05 §3.3.1 rule 8: the handshake that passed is what clears the fault.
    assert _attendance_faults(house.tree) == []
    assert _status(house)["faults"] == []
    assert list(sbx_sandboxes(house.tree)) == [NEXT_SANDBOX]
    assert len(_creates(house)) == 2, "a later pass made a third sandbox"

    assert (await run_stream(house_door, chat, "two")).error_chunks == []
    await await_settled(house.tree, session, 2)
    assert _sandboxes_of(house.tree, session) == [SANDBOX, NEXT_SANDBOX]


# --- 7. an invalid family file ----------------------------------------------


async def test_an_invalid_file_reports_and_keeps_serving(
    house: CaregiverStack, house_door: httpx.AsyncClient
) -> None:
    """Invariant 19 and contract 05 §3.1. A bad file is a report, never an outage.

    The last good definition serves. `caregiver` keeps running, and it
    touches no sandbox, no key and no grant.
    """
    assert house.caregiver is not None
    chat = chat_id()
    session = session_of(chat)
    assert (await run_stream(house_door, chat, "one")).error_chunks == []
    await await_settled(house.tree, session)
    applied = _status(house)
    before = house.sbx_commands()
    grants = house.grants()

    write_registry_file(house.tree, house.tree.family_file(), "name: chat\nkind: nonsense\n")
    await until(lambda: house.state() == INVALID, f"{FAMILY} to be {INVALID}", PASS_DEADLINE_S)
    document = _status(house)

    assert document["validation"]["ok"] is False
    assert document["validation"]["never_valid"] is False
    assert document["validation"]["error_count"] > 0
    assert document["validation"]["first_error"]
    assert document["validation"]["report_path"] == str(house.tree.validation_file())
    assert house.tree.validation_file().is_file()
    assert document["applied_rev"] == applied["applied_rev"]
    assert document["registry_rev"] != applied["registry_rev"]
    assert document["kind"] == applied["kind"]

    # Nothing was touched, and the family answers on the same sandbox.
    assert house.sandboxes() == [(SANDBOX, READY)]
    assert house.sbx_commands() == before
    assert house.grants() == grants
    assert (await run_stream(house_door, chat, "two")).error_chunks == []
    await await_settled(house.tree, session, 2)
    assert _sandboxes_of(house.tree, session) == [SANDBOX, SANDBOX]
    assert house.caregiver.exit_code() is None, "a bad family file ended caregiver"


# --- 8. caregiver killed during a replacement --------------------------------


async def test_a_kill_between_publish_and_destroy_converges(
    house: CaregiverStack, house_door: httpx.AsyncClient
) -> None:
    """Contract 05 §4.3, then §5, then §4.4, with a kill in the middle.

    The first `caregiver` publishes the replacement, makes the switch call
    and starts the destroy. SIGKILL ends it and its `sbx rm` there, as the
    stop of a unit ends a whole control group. A new process gets the state
    root and nothing else. It adopts the sandbox it finds and makes no third
    one. At no moment does a sandbox get more reach than the family file
    allows (invariant 9).
    """
    assert house.caregiver is not None
    chat = chat_id()
    session = session_of(chat)
    assert (await run_stream(house_door, chat, "one")).error_chunks == []
    await await_settled(house.tree, session)

    tune(house.tree, SBX, "hold-rm")
    write_family_file(house.tree, family_body(sandbox={"cpus": MORE_CPUS}))
    await until(lambda: ("rm", "-f", SANDBOX) in house.sbx_commands(), f"the rm of {SANDBOX}")
    killed = house.caregiver
    os.killpg(killed.pgid, signal.SIGKILL)
    await until(lambda: killed.exit_code() is not None, "the killed caregiver to end")
    untune(house.tree, SBX, "hold-rm")

    # The kill came after the publish and before the end of the destroy.
    assert house.sandboxes() == [(SANDBOX, READY), (NEXT_SANDBOX, CREATING)]
    assert list(sbx_sandboxes(house.tree)) == [SANDBOX, NEXT_SANDBOX]

    house.spawn_caregiver()
    await until(lambda: house.serves(sandbox=NEXT_SANDBOX), f"{NEXT_SANDBOX} to serve")

    assert list(sbx_sandboxes(house.tree)) == [NEXT_SANDBOX]
    assert len(_creates(house)) == 2, "the new process made a third sandbox"

    for sandbox in (SANDBOX, NEXT_SANDBOX):
        assert set(_allowed_ever(house, sandbox)) <= set(PLANE_ENDPOINTS)

    assert (await run_stream(house_door, chat, "two")).error_chunks == []
    await await_settled(house.tree, session, 2)
    assert _sandboxes_of(house.tree, session) == [SANDBOX, NEXT_SANDBOX]


# --- reading what crossed a boundary ----------------------------------------


def _status(house: CaregiverStack) -> dict[str, Any]:
    document = house.tree.status()
    assert document is not None, "caregiver published no status document"

    return document


def _tools(manifest: httpx.Response) -> list[str]:
    """The name of each tool that one manifest offers (contract 04 §4)."""
    assert manifest.status_code == HTTP_OK, manifest.text

    return [str(tool["name"]) for tool in manifest.json()["tools"]]


async def _call(sandbox: httpx.AsyncClient, tool: str, args: dict[str, str]) -> httpx.Response:
    """One `/call`, as the bridge sends it (contract 04 §5)."""
    return await sandbox.post(CALL_PATH, json={"tool": tool, "args": args})


def _decisions(tree: Tree, tool: str) -> list[str]:
    """The decision of each audit record of one tool, in file order (contract 04 §6)."""
    return [str(line["decision"]) for line in tree.audit_lines() if line.get("tool") == tool]


def _plane_rows(tree: Tree, sandbox: str) -> list[str]:
    """The plane endpoints that one sandbox may reach now, by its allow rows."""
    return [host for host in sbx_rows(tree, sandbox, ALLOW) if host in PLANE_ENDPOINTS]


def _creates(house: CaregiverStack) -> list[tuple[str, ...]]:
    return [command for command in house.sbx_commands() if command[0] == "create"]


def _allowed_ever(house: CaregiverStack, sandbox: str) -> list[str]:
    """Each host that an `sbx policy allow` named for one sandbox, in call order.

    A later `policy rm` takes no host out of this list. The question is
    whether one moment of a pass gave more reach than the family file.
    """
    head = ("policy", "allow", "network", "--sandbox", sandbox)

    return [command[-1] for command in house.sbx_commands() if command[: len(head)] == head]


def _attendance_faults(tree: Tree) -> list[dict[str, Any]]:
    """Each open fault that `attendance` reports for the family (contract 05 §3.3.1)."""
    document = json.loads(tree.fault_file(FAULTS_OF_ATTENDANCE).read_text(encoding="utf-8"))
    faults: list[dict[str, Any]] = document["faults"]

    return faults


def _one_fault(status: dict[str, Any], code: str) -> dict[str, Any]:
    """The one fault of that code in the status document (contract 05 §3.3)."""
    found = [fault for fault in status["faults"] if fault.get("code") == code]
    assert len(found) == 1, status["faults"]
    fault: dict[str, Any] = found[0]

    return fault


async def _started(tree: Tree, session: str, count: int) -> None:
    """Wait until the journal shows that a turn reached the sandbox."""
    await until(
        lambda: tree.journal_kinds(session).count(TURN_STARTED) >= count,
        f"{session} to start {count} turns",
        STARTED_DEADLINE_S,
    )


def _sandboxes_of(tree: Tree, session: str) -> list[str]:
    """The sandbox each turn started on, in order (contract 02 §8.1)."""
    return [
        str(_body(line).get("sandbox", ""))
        for line in tree.journal_lines(session)
        if line.get("kind") == TURN_STARTED
    ]


def _switch_notes(tree: Tree, session: str) -> list[dict[str, Any]]:
    """The body of every switch note in the journal of one session."""
    bodies = [_body(line) for line in tree.journal_lines(session) if line.get("kind") == NOTE]

    return [body for body in bodies if body.get("note") == SWITCH_NOTE]


def _body(line: dict[str, Any]) -> dict[str, Any]:
    body = line.get("body")

    return body if isinstance(body, dict) else {}  # pyright: ignore[reportUnknownVariableType]
