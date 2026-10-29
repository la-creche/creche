"""The webhook listener of the trigger door: `agent-trigger serve`.

`creche-door-trigger.service` runs it. A test plays an automation on the
LAN: it posts to `/triggers/<family>/<name>` with the bearer of that webhook
(`docs/rework/spec.md` §7.3, §11.5), then reads what `attendance` did.

The scenarios that `integration/tests/test_i5_stage5.py` also has keep the
names of that file. That suite hosts the listener in the test process and
lets `caregiver` mint the bearer. No `caregiver` runs here, so the suite
writes the bearer file as contract 05 §6.4 gives it.

CONTRACT-QUESTION: `docs/rework/spec.md` §7.3 and §11.5 give two answers of
the listener: 202 for a firing, and one 404 for an unknown family, an unknown
name and a wrong bearer. No contract gives the body of the 202, or the status
of another refusal. Reading taken: what the old stage 5 suite holds. The 202
names the session and the state of the turn. A body over the limit is 413, a
body that is not JSON is 400, and a refusal of `attendance` keeps the status
that contract 02 §14 gives it. A change costs one assertion per scenario.
"""

from __future__ import annotations

import signal

import httpx
import pytest
from proc_harness import LOOPBACK, TcpAddress, is_listening
from proc_services import Service
from proc_tree import (
    AUTONOMOUS,
    SECRET_MODE,
    Validity,
    replace_secret,
    token_of,
    webhook_token_of,
    write_status,
    write_webhook_token,
)
from proc_trigger import REVIEW, SERVE, WEBHOOK, TriggerStack, hook_path, serve_env

#: A job of the fixture family crosses four processes on a loaded machine.
JOB_DEADLINE_S = 60.0

#: Long enough to pass the 32-byte floor, so a refusal proves the comparison
#: and not the length check.
WRONG_BEARER = "not-the-minted-bearer-" + "z" * 32
ROTATED_BEARER = "FIXTURE-ROTATED-WEBHOOK-" + "r" * 32
SHORT_SECRET = "short-webhook-token"

#: Contract 02 §5.4: a prompt holds 256 KiB at most. A payload is a part of
#: the prompt, so no payload over that size can reach a job.
OVER_THE_PROMPT_CAP = 256 * 1024 + 1

#: A refresh short enough to watch, and one that never comes in a test.
SHORT_REFRESH_S = 0.2
NO_REFRESH_S = 3600.0

RELOAD_DEADLINE_S = 15.0
RELOAD_POLL_S = 0.05
EXIT_DEADLINE_S = 30.0

#: `creche-door-trigger.service`: `TimeoutStopSec`. A stop takes less.
STOP_DEADLINE_S = 30.0

EVERY_INTERFACE = "0.0.0.0"
CHECK_FLAG = "--check"
JSON_TYPE = {"Content-Type": "application/json"}


async def test_a_wrong_webhook_bearer_reaches_nothing(trigger: TriggerStack) -> None:
    """A bad token is refused at the door. `attendance` gets no call."""
    tree = trigger.tree

    async with trigger.automation(token=WRONG_BEARER) as stranger:
        wrong = await stranger.post(hook_path(), content=b"{}", headers=JSON_TYPE)
        missing = await stranger.post(hook_path(), headers={"Authorization": ""})

    assert wrong.status_code == httpx.codes.NOT_FOUND
    assert missing.status_code == httpx.codes.NOT_FOUND
    assert tree.sessions_of(REVIEW) == []
    assert tree.outcomes(REVIEW) == []
    assert webhook_token_of(REVIEW, WEBHOOK) not in wrong.text


async def test_an_unknown_hook_reads_like_a_wrong_token(trigger: TriggerStack) -> None:
    """Spec §7.3 rule 4: one 404 for each of the three. No name leaks."""
    async with trigger.automation() as automation:
        unknown_name = await automation.post(hook_path(name="no-such-hook"))
        unknown_family = await automation.post(hook_path(family="no-such-family"))

    async with trigger.automation(token=WRONG_BEARER) as stranger:
        wrong_token = await stranger.post(hook_path())

    answers = [unknown_name, unknown_family, wrong_token]

    assert [answer.status_code for answer in answers] == [httpx.codes.NOT_FOUND] * 3
    assert unknown_name.json() == unknown_family.json() == wrong_token.json()
    assert trigger.tree.sessions_of(REVIEW) == []


async def test_an_oversized_body_is_refused_at_the_door(trigger: TriggerStack) -> None:
    """Nothing over the prompt limit of `attendance` gets as far as `attendance`."""
    body = b'{"pad":"' + b"p" * OVER_THE_PROMPT_CAP + b'"}'

    async with trigger.automation() as automation:
        reply = await automation.post(hook_path(), content=body, headers=JSON_TYPE)

    assert reply.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE
    assert trigger.tree.sessions_of(REVIEW) == []


async def test_a_body_that_is_not_json_is_refused_at_the_door(trigger: TriggerStack) -> None:
    """The shape is checked before a session exists."""
    async with trigger.automation() as automation:
        reply = await automation.post(hook_path(), content=b"not json at all")

    assert reply.status_code == httpx.codes.BAD_REQUEST
    assert trigger.tree.sessions_of(REVIEW) == []


async def test_a_family_that_never_validated_has_no_route(trigger_prepared: TriggerStack) -> None:
    """Invariant 19, at this door: a file that was never valid opens no route."""
    tree = trigger_prepared.tree
    write_status(tree, REVIEW, AUTONOMOUS, validity=Validity.NEVER_VALID, webhooks=(WEBHOOK,))
    trigger_prepared.start()

    async with trigger_prepared.automation() as automation:
        reply = await automation.post(hook_path())

    assert reply.status_code == httpx.codes.NOT_FOUND
    assert tree.sessions_of(REVIEW) == []


@pytest.mark.parametrize("case", ["missing", "short", "group-read", "not-text"])
async def test_a_bearer_file_that_cannot_be_trusted_has_no_route(
    trigger_prepared: TriggerStack, case: str
) -> None:
    """Contract 05 §6.4 rule 6. Such a route is absent, and absent answers 404."""
    tree = trigger_prepared.tree
    path = tree.webhook_token_file(REVIEW, WEBHOOK)
    offered = webhook_token_of(REVIEW, WEBHOOK)

    if case == "missing":
        path.unlink()
    elif case == "short":
        offered = SHORT_SECRET
        write_webhook_token(tree, REVIEW, WEBHOOK, token=SHORT_SECRET)
    elif case == "group-read":
        path.chmod(0o640)
    else:
        path.write_bytes(b"\xff\xfe" + b"x" * 40)
        path.chmod(SECRET_MODE)

    trigger_prepared.start()

    async with trigger_prepared.automation(token=offered) as automation:
        reply = await automation.post(hook_path())

    assert reply.status_code == httpx.codes.NOT_FOUND
    assert tree.sessions_of(REVIEW) == []


async def test_sigterm_ends_the_listener(trigger_prepared: TriggerStack) -> None:
    """`KillSignal=SIGTERM` of the unit. The process ends, and the port closes."""
    trigger_prepared.start_listener()
    assert trigger_prepared.listener is not None

    trigger_prepared.listener.send(signal.SIGTERM)
    trigger_prepared.listener.wait(STOP_DEADLINE_S)

    assert not is_listening(TcpAddress(trigger_prepared.listener_port))


def test_the_serve_check_validates_and_binds_nothing(trigger_prepared: TriggerStack) -> None:
    """`--check` reads the config and exits 0. It opens no socket."""
    tree = trigger_prepared.tree
    port = trigger_prepared.supervisor.free_port()
    env = serve_env(tree, f"{LOOPBACK}:{port}")

    checked = trigger_prepared.run(Service.DOOR_TRIGGER, env, SERVE, CHECK_FLAG)

    assert checked.exit_code == 0, checked.stderr
    assert not is_listening(TcpAddress(port))
    assert token_of("door-trigger") not in checked.stdout + checked.stderr


@pytest.mark.parametrize("content", [None, "", SHORT_SECRET], ids=["missing", "empty", "short"])
def test_the_listener_refuses_a_bad_token_file(
    trigger_prepared: TriggerStack, content: str | None
) -> None:
    """Contract 02 §3 rule 7, as the door applies it to its own token. Fail closed."""
    tree = trigger_prepared.tree
    port = trigger_prepared.supervisor.free_port()
    replace_secret(tree.token_file("door-trigger"), content)

    child = trigger_prepared.spawn(
        Service.DOOR_TRIGGER, serve_env(tree, f"{LOOPBACK}:{port}"), SERVE
    )

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not is_listening(TcpAddress(port))
    assert SHORT_SECRET not in child.output()


def test_the_listener_refuses_to_bind_every_interface(trigger_prepared: TriggerStack) -> None:
    """Contract 02 §3 rule 9: no door binds every interface, even when its bind names it."""
    tree = trigger_prepared.tree
    port = trigger_prepared.supervisor.free_port()

    child = trigger_prepared.spawn(
        Service.DOOR_TRIGGER, serve_env(tree, f"{EVERY_INTERFACE}:{port}"), SERVE
    )

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not is_listening(TcpAddress(port))
