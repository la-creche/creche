"""The one call of `caregiver` into `attendance`, at the HTTP boundary.

The scenarios that ran a switch to its end left in phase 2 row 20: the
handshake is ACP `initialize` on the incoming facade, and this topology has
no facade. What stays is the token check and the refusal of a sandbox that
cannot start, which come before the handshake succeeds.
"""

from __future__ import annotations

import httpx
from proc_stack import Stack
from proc_tree import FAMILY, PRINCIPALS, SANDBOX, Tree, write_playpen_env, write_status

SWITCH_PATH = "/internal/switch-sandbox"
CAREGIVER = "managerd"
NEXT_SANDBOX = "chat-s2"

DRAIN = "drain"
ADDITION = "sandbox.cpus changed: 2 to 4"

HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_UNAVAILABLE = 503


async def test_the_caregiver_token_clears_the_internal_check(attended: Stack) -> None:
    """Contract 02 §3.1 and contract 05 §5.3 rule 8.

    The token authenticates and may make an internal call. Only the pair is
    refused, because `from` and `to` name one sandbox.
    """
    response = await _switch(attended, DRAIN, to=SANDBOX)

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["error"]["code"] == "bad_request"


async def test_no_door_token_reaches_the_internal_call(attended: Stack) -> None:
    """Contract 02 §3.1: no door token reaches `/internal/*`."""
    _publish_incoming(attended.tree)

    response = await _switch(attended, DRAIN, principal="door-trigger")

    assert response.status_code == HTTP_FORBIDDEN
    assert response.json()["error"]["code"] == "forbidden"
    assert len(attended.sbx_calls()) == 0, "a refused call dialled a sandbox"


async def test_each_token_of_the_tree_authenticates(attended: Stack) -> None:
    """Contract 02 §3.1: each token file of the tree names a principal of `attendance`.

    `attendance` knows the bearer, so the call gets past the token check. A
    door token is then refused at `/internal/*`, and the caregiver token is
    refused for the pair, because `from` and `to` name one sandbox.
    """
    answers = {
        principal: await _switch(attended, DRAIN, to=SANDBOX, principal=principal)
        for principal in PRINCIPALS
    }

    statuses = {principal: answer.status_code for principal, answer in answers.items()}
    assert HTTP_UNAUTHORIZED not in statuses.values(), statuses


async def test_a_switch_onto_a_sandbox_that_cannot_start_is_refused(attended: Stack) -> None:
    """Contract 05 §5.3 rule 8: the handshake runs before anything moves.

    The incoming sandbox runs no facade, so ACP `initialize` gets no answer.
    The call is refused, and nothing moves.
    """
    _publish_incoming(attended.tree)
    response = await _switch(attended, DRAIN)

    assert response.status_code == HTTP_UNAVAILABLE
    assert response.json()["error"]["code"] == "sandbox_unavailable"


def _publish_incoming(tree: Tree) -> None:
    """Contract 05 §4.3: the env file, then the incoming sandbox in the document."""
    write_playpen_env(tree, FAMILY, NEXT_SANDBOX)
    write_status(tree, sandboxes=((SANDBOX, "ready"), (NEXT_SANDBOX, "creating")))


async def _switch(
    attended: Stack,
    mode: str,
    *,
    to: str = NEXT_SANDBOX,
    reason: str = ADDITION,
    principal: str = CAREGIVER,
) -> httpx.Response:
    """One switch call (contract 05 §5.1), with the token of one principal."""
    body = {"family": FAMILY, "from": SANDBOX, "to": to, "mode": mode, "reason": reason}

    async with attended.attendance_client(principal) as client:
        return await client.post(SWITCH_PATH, json=body)
