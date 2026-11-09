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

import pytest
from proc_caregiver import (
    EXIT_OK,
    EXIT_USAGE,
    IN_SYNC,
    WATCH_OFF,
    WRITE_FLAG,
    CaregiverStack,
    caregiver_env,
    chaperone_flags,
    litellm_url,
    serve_words,
    text_of,
    wait_until,
    watch_flags,
)
from proc_harness import Finished
from proc_standins import (
    LITELLM,
    MASTER_KEY,
    VMM,
    litellm_calls,
    litellm_keys,
    tune,
    untune,
    vmm_calls,
)
from proc_tree import FAMILY, write_family_prose

NEW_INSTRUCTIONS = "Answer only in metric units.\n"
LAN_VARIABLE = "AGENT_LAN_ADDRESS"
KEY_VARIABLE = "LITELLM_MASTER_KEY"
VMM_VARIABLE = "CAREGIVER_VMM_SOCKET"
LITELLM_FLAG = "--litellm-base-url"

#: The exit status of a fault of the env files (`caregiver/src/caregiver/cli.py`).
EXIT_CONFIG = 78

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


def test_serve_refuses_to_start_with_no_lan_address(caregiver_prepared: CaregiverStack) -> None:
    """Fail closed. No default address exists, and the refusal names the variable.

    The unit gives no `--litellm-base-url`, so LiteLLM is on the LAN address
    of the site file. Here the flag is gone too.
    """
    env = {
        name: value
        for name, value in caregiver_env(caregiver_prepared.tree).items()
        if name != LAN_VARIABLE
    }
    words = _serve(caregiver_prepared)
    at = words.index(LITELLM_FLAG)
    del words[at : at + 2]

    done = caregiver_prepared.run_caregiver(*words, env=env)

    assert done.exit_code == EXIT_USAGE
    assert LAN_VARIABLE in done.stderr
    _nothing_was_touched(caregiver_prepared, done)


def test_serve_refuses_to_start_with_no_vmm_socket(caregiver_prepared: CaregiverStack) -> None:
    """Fail closed (decision CH20). The unit sets the variable from `%t`, and
    a `serve` with no socket cannot drop a deleted family. The refusal names
    the variable, with the exit status of a fault of the env files."""
    env = {
        name: value
        for name, value in caregiver_env(caregiver_prepared.tree).items()
        if name != VMM_VARIABLE
    }

    done = caregiver_prepared.run_caregiver(*_serve(caregiver_prepared), env=env)

    assert done.exit_code == EXIT_CONFIG
    assert VMM_VARIABLE in done.stderr
    _nothing_was_touched(caregiver_prepared, done)


def test_serve_refuses_to_start_with_no_master_key(caregiver_prepared: CaregiverStack) -> None:
    """Fail closed. A `caregiver` with no master key can mint no key."""
    env = {
        name: value
        for name, value in caregiver_env(caregiver_prepared.tree).items()
        if name != KEY_VARIABLE
    }

    done = caregiver_prepared.run_caregiver(*_serve(caregiver_prepared), env=env)

    assert done.exit_code != EXIT_OK
    assert KEY_VARIABLE in done.stderr
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
        lambda: text_of(instructions) == NEW_INSTRUCTIONS,
        "the new instructions in the config directory",
        LOOK_DEADLINE_S,
    )
    assert child.exit_code() is None, "a reload ended the service"


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


def test_delete_stops_when_the_key_stays(caregiver_alone: CaregiverStack) -> None:
    """Invariant 13: credentials die before processes. The key is first.

    LiteLLM refuses the delete. So the verb fails, the app table and the
    credential file stay, and the vmm drops nothing.
    """
    stack = caregiver_alone
    _stop(stack)
    tune(stack.tree, LITELLM, "fail-delete")

    done = _delete(stack)

    assert done.exit_code != EXIT_OK
    assert list(litellm_keys(stack.tree)) == [f"family-{FAMILY}"]
    assert stack.tree.policy_fragment().is_file()
    assert list(stack.apps()) == [FAMILY]
    assert stack.tree.creds_file().is_file()
    assert stack.dropped() == []


def test_delete_ends_each_credential_before_the_vmm_drop(caregiver_alone: CaregiverStack) -> None:
    """Invariant 13, contract 05 §4.4 and decision CH21-10. The drop is last.

    The vmm fails the drop. At that moment the key, the app table, the
    credential file and the config directory are gone already, and the
    state of the family stays for the next try. A second run ends the work.
    """
    stack = caregiver_alone
    _stop(stack)
    tune(stack.tree, VMM, "fail-drop")

    failed = _delete(stack)

    assert failed.exit_code != EXIT_OK
    assert stack.dropped() == [FAMILY]
    assert litellm_keys(stack.tree) == {}
    assert not stack.tree.policy_fragment().exists()
    assert stack.apps() == {}
    assert not stack.tree.creds_file().exists()
    assert not stack.tree.mounts().config.exists()
    assert stack.tree.family_dir().exists()

    untune(stack.tree, VMM, "fail-drop")
    done = _delete(stack)

    assert done.exit_code == EXIT_OK, done.stderr
    assert stack.dropped() == [FAMILY, FAMILY]
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
        *chaperone_flags(stack.tree),
        WRITE_FLAG,
    )


def _stop(stack: CaregiverStack) -> None:
    """End `caregiver serve`, as a stop of its unit does."""
    assert stack.caregiver is not None
    stack.caregiver.send(signal.SIGTERM)
    assert stack.caregiver.wait(EXIT_DEADLINE_S) == EXIT_OK


def _nothing_was_touched(stack: CaregiverStack, done: Finished) -> None:
    """A start that did not happen leaves no family and no key, and calls
    no vmm.

    No output holds the master key (invariant 13).
    """
    assert stack.tree.status() is None
    assert not stack.tree.policy_fragment().exists()
    assert vmm_calls(stack.tree) == []
    assert litellm_calls(stack.tree) == []
    assert MASTER_KEY not in done.stdout + done.stderr
