"""The PEP as a client of `sessiond`'s dispatch door (contract 02 §13.4).

No live service: every case drives `HttpDispatchDoor` through an httpx mock
transport, the same door a sandbox's `enqueue` would reach over the socket.
"""

from __future__ import annotations

import json

import httpx
import pytest
from agent_pep.delegate import MIN_DOOR_TOKEN_BYTES
from agent_pep.dispatch import (
    DISPATCH_PATH,
    JOBS_PATH,
    DispatchConfig,
    DispatchRefused,
    DispatchRequest,
    HttpDispatchDoor,
    JobQuery,
)

TOKEN = "x" * MIN_DOOR_TOKEN_BYTES
LEAD = "scrum-lead"
WORKER = "issue-worker"
DELEGATION = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
SESSION = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
CHAIN = ("chat", "scrum-lead", "issue-worker")
MESSAGE = "Take ticket 412."


def make_request(**changed: object) -> DispatchRequest:
    fields: dict[str, object] = {
        "caller_family": LEAD,
        "target_family": WORKER,
        "delegation_id": DELEGATION,
        "chain": CHAIN,
        "claimed_session_id": "auto-01JBQ7YY1C5L9P3QWS1H7GXUAJ",
        "message": MESSAGE,
        "idempotency_key": None,
    }
    fields.update(changed)
    return DispatchRequest(**fields)  # pyright: ignore[reportArgumentType]


def door(handler: object, *, timeout_s: float = 30.0) -> HttpDispatchDoor:
    transport = httpx.MockTransport(handler)  # pyright: ignore[reportArgumentType]
    return HttpDispatchDoor(DispatchConfig(token=TOKEN, timeout_s=timeout_s), transport=transport)


def ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == DISPATCH_PATH
    return httpx.Response(200, json={"session_id": SESSION, "status": "queued", "created": True})


# ---- enqueue ---------------------------------------------------------------


async def test_the_door_gets_every_contract_field() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return ok_handler(request)

    await door(handler).enqueue(make_request())

    assert seen == [
        {
            "caller_family": LEAD,
            "target_family": WORKER,
            "delegation_id": DELEGATION,
            "chain": list(CHAIN),
            "claimed_session_id": "auto-01JBQ7YY1C5L9P3QWS1H7GXUAJ",
            "message": MESSAGE,
        }
    ]


async def test_an_idempotency_key_rides_when_it_is_set() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return ok_handler(request)

    await door(handler).enqueue(make_request(idempotency_key="morning"))

    assert seen[0]["idempotency_key"] == "morning"


async def test_the_token_rides_in_the_header() -> None:
    """Invariant 13: never in the path and never in a query string."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return ok_handler(request)

    await door(handler).enqueue(make_request())

    assert seen == [f"Bearer {TOKEN}"]
    assert TOKEN not in DISPATCH_PATH


async def test_the_answer_carries_the_session() -> None:
    reply = await door(ok_handler).enqueue(make_request())

    assert reply.session == SESSION
    assert reply.status == "queued"
    assert reply.created is True


async def test_a_declaration_refusal_becomes_tool_not_granted() -> None:
    """Contract 02 §13.4.1 rule 3 reaches the caller as a policy denial, not
    as an upstream failure: nothing ran."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": "dispatch_not_declared", "message": "no enqueue trigger"}},
        )

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "tool_not_granted"
    assert "enqueue" in raised.value.detail


async def test_a_forbidden_target_becomes_tool_not_granted() -> None:
    """A thin or attended target is refused by the door's own grant row."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"code": "forbidden", "message": "kind attended"}}
        )

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "tool_not_granted"


async def test_a_full_queue_becomes_rate_limited() -> None:
    """Contract 02 §13.4.1 rule 4. The target's queue, not the caller's."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "queue_full", "message": "full"}})

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "rate_limited"


async def test_a_bad_request_becomes_arg_validation() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "bad_request", "message": "no"}})

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "arg_validation"


async def test_an_unknown_family_becomes_tool_not_granted() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "family_unknown", "message": "no"}})

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "tool_not_granted"


async def test_a_dead_socket_is_an_upstream_failure() -> None:
    """Fail closed, and not as a denial: the PEP could not ask."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no socket")

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "upstream_failed"


async def test_a_body_that_is_not_json_is_an_upstream_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "upstream_failed"


async def test_an_answer_without_a_session_is_an_upstream_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "queued"})

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).enqueue(make_request())

    assert raised.value.reason == "upstream_failed"


async def test_a_missing_token_file_fails_that_call(tmp_path: object) -> None:
    """The path goes to the journal. The sandbox is told the door is not
    configured, and no request leaves the PEP."""
    missing = DispatchConfig(token="", token_source=_NoToken())

    with pytest.raises(DispatchRefused) as raised:
        await HttpDispatchDoor(missing).enqueue(make_request())

    assert raised.value.reason == "upstream_failed"
    assert "/" not in raised.value.detail


# ---- job_status ------------------------------------------------------------


async def test_the_job_query_carries_the_caller() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == JOBS_PATH
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"jobs": []})

    await door(handler).jobs(JobQuery(caller_family=LEAD, session=SESSION, limit=5))

    assert seen == [{"caller_family": LEAD, "session": SESSION, "limit": 5}]


async def test_an_absent_argument_is_left_out() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"jobs": []})

    await door(handler).jobs(JobQuery(caller_family=LEAD))

    assert seen == [{"caller_family": LEAD}]


async def test_the_jobs_list_is_served_through() -> None:
    """Contract 04 §4.1 rule 3: the outcome record is copied whole."""
    row = {
        "session": SESSION,
        "family": WORKER,
        "enqueued_at": "2026-09-20T06:00:01Z",
        "status": "ended",
        "outcome": {"id": "01JBQ80M4F7S2YQ1VZK6W3TDEN", "status": "ok"},
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [row]})

    reply = await door(handler).jobs(JobQuery(caller_family=LEAD))

    assert reply.jobs == [row]


async def test_a_jobs_answer_that_is_no_object_fails() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    with pytest.raises(DispatchRefused) as raised:
        await door(handler).jobs(JobQuery(caller_family=LEAD))

    assert raised.value.reason == "upstream_failed"


async def test_a_jobs_list_is_bounded() -> None:
    """Another process's answer is bounded before it reaches a model."""
    rows = [{"session": f"auto-{n}", "status": "ended"} for n in range(500)]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": rows})

    reply = await door(handler).jobs(JobQuery(caller_family=LEAD))

    assert len(reply.jobs) <= 200


class _NoToken:
    """A token source whose file is missing, the way a PEP that started
    before `sessiond` sees it."""

    def value(self) -> str:
        from agent_pep.delegate import DoorTokenError

        raise DoorTokenError("dispatch door token: cannot read /srv/x (No such file)")
