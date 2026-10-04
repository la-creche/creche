"""How `caregiver` starts, refuses, takes a signal and ends.

No suite that hosts `caregiver` in the test process can see these: an exit
code, a signal, a kill, the order of two commands of one verb. Each scenario
names the rule it holds, and each assertion reads a process boundary.

CONTRACT-QUESTION: no contract names an exit code of `caregiver`. Reading
taken: the three codes that `caregiver/AGENTS.md` gives, where the service
selects one: 0 for success, 1 for a refusal, 2 for a usage mistake. Where
the service ends with no code of its own, any code that is not 0. A unit
with `Restart=always` starts the service again whatever the code is. A
change costs one assertion in each scenario here.
"""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path

import pytest
from proc_caregiver import (
    CREATING,
    EXIT_OK,
    EXIT_PROBLEM,
    EXIT_USAGE,
    IN_SYNC,
    PLANE_ENDPOINTS,
    READY,
    RECONCILING,
    WATCH_OFF,
    WRITE_FLAG,
    CaregiverStack,
    caregiver_env,
    litellm_url,
    serve_words,
    wait_until,
    watch_flags,
)
from proc_harness import Finished
from proc_standins import (
    ALLOW,
    LITELLM,
    MASTER_KEY,
    SBX,
    litellm_calls,
    litellm_keys,
    sbx_rows,
    sbx_sandboxes,
    tune,
    untune,
)
from proc_tree import FAMILY, SANDBOX, SECRET_MODE, write_family_prose

NEXT_SANDBOX = "chat-s2"
NEW_INSTRUCTIONS = "Answer only in metric units.\n"
LAN_VARIABLE = "AGENT_LAN_ADDRESS"
KEY_VARIABLE = "LITELLM_MASTER_KEY"
RELEASE_ROOT_FLAG = "--release-root"

#: A look so rare that only a signal can cause one inside a test.
RARE_LOOK = ("--poll-interval-s", "300")

EXIT_DEADLINE_S = 30.0
LOOK_DEADLINE_S = 20.0

#: How long a scenario waits to see that nothing happens.
QUIET_S = 0.5


# ---------------------------------------------------------------- the start


def test_serve_prints_a_plan_and_starts_nothing_with_no_write_flag(
    caregiver_prepared: CaregiverStack,
) -> None:
    """Every verb that changes something needs `--write` to act."""
    words = [word for word in _serve(caregiver_prepared) if word != WRITE_FLAG]

    done = caregiver_prepared.run_caregiver(*words)

    assert done.exit_code == EXIT_OK, done.stderr
    _nothing_was_touched(caregiver_prepared, done)


def test_serve_refuses_the_release_root_of_the_host(caregiver_prepared: CaregiverStack) -> None:
    """A state root of a test, with the release root of the host, is a usage mistake.

    `caregiver` files release requests under the release root. A run with a
    state root of its own must not file one where the host reads them.
    """
    words = _serve(caregiver_prepared)
    at = words.index(RELEASE_ROOT_FLAG)

    done = caregiver_prepared.run_caregiver(*words[:at], *words[at + 2 :])

    assert done.exit_code == EXIT_USAGE
    _nothing_was_touched(caregiver_prepared, done)


def test_serve_refuses_to_start_with_no_lan_address(caregiver_prepared: CaregiverStack) -> None:
    """Fail closed. No default address exists, and the refusal names the variable."""
    env = {name: value for name, value in caregiver_env().items() if name != LAN_VARIABLE}

    done = caregiver_prepared.run_caregiver(*_serve(caregiver_prepared), env=env)

    assert done.exit_code == EXIT_USAGE
    assert LAN_VARIABLE in done.stderr
    _nothing_was_touched(caregiver_prepared, done)


def test_serve_refuses_to_start_with_no_master_key(caregiver_prepared: CaregiverStack) -> None:
    """Fail closed. A `caregiver` with no master key can mint no key."""
    env = {name: value for name, value in caregiver_env().items() if name != KEY_VARIABLE}

    done = caregiver_prepared.run_caregiver(*_serve(caregiver_prepared), env=env)

    assert done.exit_code != EXIT_OK
    assert KEY_VARIABLE in done.stderr
    _nothing_was_touched(caregiver_prepared, done)


@pytest.mark.parametrize("content", [None, "", " \n"], ids=["missing", "empty", "blank"])
def test_serve_refuses_a_bad_token_file(
    caregiver_prepared: CaregiverStack, content: str | None
) -> None:
    """Contract 02 §3 rule 7, from the side of the caller. No token, no start.

    A `caregiver` that sent an empty bearer would ask `attendance` to accept
    one.
    """
    _replace(caregiver_prepared.tree.token_file("managerd"), content)

    done = caregiver_prepared.run_caregiver(*_serve(caregiver_prepared))

    assert done.exit_code == EXIT_PROBLEM
    _nothing_was_touched(caregiver_prepared, done)


# -------------------------------------------------------------- the signals


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_a_stop_signal_ends_the_service_with_success(
    caregiver_alone: CaregiverStack, signum: signal.Signals
) -> None:
    """`KillSignal=SIGTERM` of the unit. A stop is no failure, and it destroys nothing."""
    assert caregiver_alone.caregiver is not None

    caregiver_alone.caregiver.send(signum)

    assert caregiver_alone.caregiver.wait(EXIT_DEADLINE_S) == EXIT_OK
    assert caregiver_alone.state() == IN_SYNC
    assert list(sbx_sandboxes(caregiver_alone.tree)) == [SANDBOX]
    assert list(litellm_keys(caregiver_alone.tree)) == [f"family-{FAMILY}"]


def test_sighup_makes_the_service_look_now(caregiver_prepared: CaregiverStack) -> None:
    """`ExecReload` of the unit sends SIGHUP: look at the registry now.

    The service looks one time in 300 seconds here. So an edit lands only
    after the signal, and the signal does not end the service.
    """
    stack = caregiver_prepared
    child = stack.spawn_caregiver(*RARE_LOOK)
    stack.await_published()
    instructions = stack.tree.mounts().config / "instructions.md"

    write_family_prose(stack.tree, text=NEW_INSTRUCTIONS)
    time.sleep(QUIET_S)
    assert instructions.read_text(encoding="utf-8") != NEW_INSTRUCTIONS

    child.send(signal.SIGHUP)

    wait_until(
        lambda: instructions.read_text(encoding="utf-8") == NEW_INSTRUCTIONS,
        "the new instructions in the config mount",
        LOOK_DEADLINE_S,
    )
    assert child.exit_code() is None, "a reload ended the service"


def test_a_stop_during_a_create_ends_the_step_and_starts_no_other(
    house_prepared: CaregiverStack,
) -> None:
    """`TimeoutStopSec` of the unit, and contract 05 §4.2.

    A stop is checked between two steps and never inside one. The create in
    flight ends whole. No switch call follows, so `attendance` dials nothing.
    The next start asks for the handshake of the same sandbox.
    """
    house = house_prepared
    tune(house.tree, SBX, f"hold-create-{SANDBOX}")
    house.start_house()
    assert house.caregiver is not None
    wait_until(lambda: house.sbx_commands() != [], "the create to start")

    house.caregiver.send(signal.SIGTERM)
    time.sleep(QUIET_S)
    assert house.caregiver.exit_code() is None, "the stop cut the create in flight"

    untune(house.tree, SBX, f"hold-create-{SANDBOX}")

    assert house.caregiver.wait(EXIT_DEADLINE_S) == EXIT_OK
    assert house.dialled() == [], "a stopped caregiver asked for a handshake"
    assert house.sandboxes() == [(SANDBOX, CREATING)]
    assert house.state() == RECONCILING
    assert _plane_rows(house, SANDBOX) == sorted(PLANE_ENDPOINTS)

    house.spawn_caregiver()
    house.await_serving()

    assert house.sandboxes() == [(SANDBOX, READY)]
    assert len(_creates(house)) == 1, "the next start made a second sandbox"


def test_a_kill_inside_a_create_retires_the_sandbox(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §4.2 rule 5. A `planned` sandbox at a start is never adopted.

    SIGKILL ends `caregiver` inside `sbx create`. The create still ends, so a
    sandbox exists whose reach nobody proved. The next start destroys it and
    makes a new one, and the id of the first is never used again (§4.1).
    """
    stack = caregiver_prepared
    tune(stack.tree, SBX, f"hold-create-{SANDBOX}")
    killed = stack.spawn_caregiver()
    wait_until(lambda: stack.sbx_commands() != [], "the create to start")

    killed.send(signal.SIGKILL)
    assert killed.wait(EXIT_DEADLINE_S) == -signal.SIGKILL
    untune(stack.tree, SBX, f"hold-create-{SANDBOX}")
    wait_until(lambda: SANDBOX in sbx_sandboxes(stack.tree), f"the create of {SANDBOX} to end")

    stack.spawn_caregiver()
    wait_until(
        lambda: stack.state() == IN_SYNC and stack.sandboxes() == [(NEXT_SANDBOX, CREATING)],
        f"{NEXT_SANDBOX} in the place of {SANDBOX}",
    )

    assert list(sbx_sandboxes(stack.tree)) == [NEXT_SANDBOX]
    assert ("rm", "-f", SANDBOX) in stack.sbx_commands()
    assert _plane_rows(stack, SANDBOX) == []
    assert [command[command.index("--name") + 1] for command in _creates(stack)] == [
        SANDBOX,
        NEXT_SANDBOX,
    ]


# ------------------------------------------------------ the verbs that end


def test_status_prints_the_published_document(caregiver_alone: CaregiverStack) -> None:
    """The `status` verb reads the status document and changes nothing."""
    stack = caregiver_alone
    _stop(stack)
    published = stack.tree.status()

    done = stack.run_caregiver(
        "status", FAMILY, "--state-root", str(stack.tree.state_root), "--json"
    )

    assert done.exit_code == EXIT_OK, done.stderr
    assert json.loads(done.stdout) == published
    assert stack.tree.status() == published


def test_status_of_a_family_with_no_document_fails(caregiver_prepared: CaregiverStack) -> None:
    stack = caregiver_prepared

    done = stack.run_caregiver("status", FAMILY, "--state-root", str(stack.tree.state_root))

    assert done.exit_code == EXIT_USAGE
    assert done.stdout == ""


def test_reconcile_once_runs_one_pass_and_ends(caregiver_prepared: CaregiverStack) -> None:
    """One pass in one process. It watches nothing, and it says so.

    Contract 05 §2.2 rule 1: a verb that runs one pass publishes `off` for
    the watch, because one pass has no interval to probe over.
    """
    stack = caregiver_prepared
    words = [
        "reconcile-once",
        str(stack.tree.registry_root),
        FAMILY,
        *watch_flags(stack.tree, stack.litellm_port),
        WRITE_FLAG,
    ]

    done = stack.run_caregiver(*words)

    assert done.exit_code == EXIT_OK, done.stderr
    document = stack.tree.status()
    assert document is not None
    assert document["state"] == IN_SYNC
    assert document["pep"]["watch"] == "off"
    assert stack.sandboxes() == [(SANDBOX, CREATING)]


def test_delete_stops_when_the_key_stays(caregiver_alone: CaregiverStack) -> None:
    """Invariant 13: credentials die before processes. The key is first.

    LiteLLM refuses the delete. So the verb fails, and the grant file, the
    credential file and the sandbox all stay.
    """
    stack = caregiver_alone
    _stop(stack)
    tune(stack.tree, LITELLM, "fail-delete")

    done = _delete(stack)

    assert done.exit_code != EXIT_OK
    assert list(litellm_keys(stack.tree)) == [f"family-{FAMILY}"]
    assert stack.tree.grant_file().is_file()
    assert stack.tree.creds_file().is_file()
    assert list(sbx_sandboxes(stack.tree)) == [SANDBOX]


def test_delete_ends_each_credential_before_the_sandbox(caregiver_alone: CaregiverStack) -> None:
    """Invariant 13 and contract 05 §4.4. The sandbox is last.

    `sbx rm` fails. At that moment the key, the grant file and the
    credential file are gone already. A second run ends the work.
    """
    stack = caregiver_alone
    _stop(stack)
    tune(stack.tree, SBX, "fail-rm", "the sbx daemon does not answer")

    failed = _delete(stack)

    assert failed.exit_code != EXIT_OK
    assert list(sbx_sandboxes(stack.tree)) == [SANDBOX]
    assert litellm_keys(stack.tree) == {}
    assert not stack.tree.grant_file().exists()
    assert not stack.tree.creds_file().exists()

    untune(stack.tree, SBX, "fail-rm")
    done = _delete(stack)

    assert done.exit_code == EXIT_OK, done.stderr
    assert sbx_sandboxes(stack.tree) == {}
    assert _plane_rows(stack, SANDBOX) == []
    assert not stack.tree.family_dir().exists()


# -------------------------------------------------------------------- helpers


def _serve(stack: CaregiverStack) -> list[str]:
    """The words of the unit's start command, with the watch off."""
    return serve_words(stack.tree, stack.litellm_port, WATCH_OFF)


def _delete(stack: CaregiverStack) -> Finished:
    return stack.run_caregiver(
        "delete",
        FAMILY,
        "--state-root",
        str(stack.tree.state_root),
        "--litellm-base-url",
        litellm_url(stack.litellm_port),
        WRITE_FLAG,
    )


def _stop(stack: CaregiverStack) -> None:
    """End `caregiver serve`, as a stop of its unit does."""
    assert stack.caregiver is not None
    stack.caregiver.send(signal.SIGTERM)
    assert stack.caregiver.wait(EXIT_DEADLINE_S) == EXIT_OK


def _nothing_was_touched(stack: CaregiverStack, done: Finished) -> None:
    """A start that did not happen leaves no family, no key and no sandbox.

    No output holds the master key (invariant 13).
    """
    assert stack.tree.status() is None
    assert not stack.tree.grant_file().exists()
    assert stack.sbx_calls() == []
    assert litellm_calls(stack.tree) == []
    assert MASTER_KEY not in done.stdout + done.stderr


def _creates(stack: CaregiverStack) -> list[tuple[str, ...]]:
    return [command for command in stack.sbx_commands() if command[0] == "create"]


def _plane_rows(stack: CaregiverStack, sandbox: str) -> list[str]:
    """The plane endpoints that one sandbox may reach now, by its allow rows."""
    return [host for host in sbx_rows(stack.tree, sandbox, ALLOW) if host in PLANE_ENDPOINTS]


def _replace(path: Path, content: str | None) -> None:
    """Remove a secret file, or put another in its place by rename."""
    if content is None:
        path.unlink()
        return

    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.chmod(SECRET_MODE)
    temp.replace(path)
