"""The PEP as a client of `attendance`'s delegate door (contract 04 §7).

No live service: every case drives `HttpDelegateDoor` through an httpx mock
transport, which is the same door an `sbx` sandbox would reach over the Unix
socket.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from chaperone.delegate import (
    DELEGATE_PATH,
    MAX_CONTENT_CHARS,
    MIN_DOOR_TOKEN_BYTES,
    UDS_BASE_URL,
    DelegateRequest,
    DelegateStatus,
    DoorConfig,
    DoorTokenError,
    HttpDelegateDoor,
    read_door_token,
    wrap_untrusted,
)

TOKEN = "x" * MIN_DOOR_TOKEN_BYTES
CHAT = "chat"
ORACLE = "vault-oracle"
DELEGATION = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
SESSION = "job-01K5J9QWB4XN2A7C6E0F3G5H8J"
ANSWER = "The boiler was serviced on 2026-03-11."


def make_request(message: str = "where is the boiler note") -> DelegateRequest:
    return DelegateRequest(
        caller_family=CHAT,
        target_family=ORACLE,
        delegation_id=DELEGATION,
        claimed_session_id="owui-8f1c2e",
        message=message,
    )


def door(handler: object, *, timeout_s: float = 120.0) -> HttpDelegateDoor:
    transport = httpx.MockTransport(handler)  # pyright: ignore[reportArgumentType]
    return HttpDelegateDoor(DoorConfig(token=TOKEN, timeout_s=timeout_s), transport=transport)


def ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == DELEGATE_PATH
    return httpx.Response(200, json={"status": "ok", "session_id": SESSION, "content": ANSWER})


# ---- the request the door receives -----------------------------------------


async def test_the_door_gets_the_five_contract_fields() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return ok_handler(request)

    await door(handler).call(make_request())
    assert seen == [
        {
            "caller_family": CHAT,
            "target_family": ORACLE,
            "delegation_id": DELEGATION,
            "claimed_session_id": "owui-8f1c2e",
            "message": "where is the boiler note",
        }
    ]


async def test_the_door_token_rides_in_the_header() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return ok_handler(request)

    await door(handler).call(make_request())
    assert seen == [f"Bearer {TOKEN}"]
    # Invariant 13: never in the path or the query.
    assert TOKEN not in DELEGATE_PATH


# ---- the answer ------------------------------------------------------------


async def test_an_answer_comes_back_whole() -> None:
    reply = await door(ok_handler).call(make_request())
    assert reply.status is DelegateStatus.OK
    assert reply.content == ANSWER
    assert reply.session_id == SESSION


async def test_a_failed_status_carries_its_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failed", "error": "no sandbox"})

    reply = await door(handler).call(make_request())
    assert reply.status is DelegateStatus.FAILED
    assert reply.error == "no sandbox"


async def test_the_door_answering_timeout_is_a_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "timeout", "error": "deadline"})

    assert (await door(handler).call(make_request())).status is DelegateStatus.TIMEOUT


async def test_a_status_the_contract_does_not_name_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "fine", "content": "trust me"})

    reply = await door(handler).call(make_request())
    assert reply.status is DelegateStatus.FAILED
    assert reply.content is None


async def test_a_body_that_is_not_an_object_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["ok", ANSWER])

    assert (await door(handler).call(make_request())).status is DelegateStatus.FAILED


async def test_a_non_json_body_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    assert (await door(handler).call(make_request())).status is DelegateStatus.FAILED


async def test_a_refusal_from_the_door_is_a_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="who are you")

    reply = await door(handler).call(make_request())
    assert reply.status is DelegateStatus.FAILED
    assert "403" in (reply.error or "")


async def test_attendance_being_down_is_a_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no such socket")

    assert (await door(handler).call(make_request())).status is DelegateStatus.FAILED


async def test_a_door_slower_than_the_limit_times_out() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json={"status": "ok", "content": ANSWER})

    reply = await door(handler, timeout_s=0.05).call(make_request())
    assert reply.status is DelegateStatus.TIMEOUT
    assert reply.content is None


async def test_an_oversized_answer_is_cut_not_dropped() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "content": "a" * (MAX_CONTENT_CHARS + 50)})

    reply = await door(handler).call(make_request())
    assert reply.status is DelegateStatus.OK
    assert reply.content is not None
    assert len(reply.content) == MAX_CONTENT_CHARS


async def test_a_content_that_is_not_a_string_reads_as_absent() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "content": {"nested": "object"}})

    reply = await door(handler).call(make_request())
    assert reply.status is DelegateStatus.OK
    assert reply.content is None


async def test_one_client_serves_every_call_and_closes_once() -> None:
    """The client is built on first use, because `app.py` builds this object
    before the event loop exists."""
    one = door(ok_handler)
    await one.call(make_request())
    await one.call(make_request())
    await one.aclose()
    await one.aclose()


async def test_a_socket_path_builds_a_unix_transport(tmp_path: Path) -> None:
    """`attendance` listens on a Unix socket, so the door speaks HTTP over one
    and never over a port. Nothing is connected here: building the client
    opens no socket."""
    built = HttpDelegateDoor(DoorConfig(token=TOKEN, socket_path=tmp_path / "sessiond.sock"))
    client = built._open()  # pyright: ignore[reportPrivateUsage]
    assert str(client.base_url) == UDS_BASE_URL
    await built.aclose()


# ---- the untrusted wrapper (§7.6) ------------------------------------------


def test_the_wrapper_names_the_family_and_marks_it_untrusted() -> None:
    assert wrap_untrusted(ORACLE, ANSWER) == {
        "untrusted": True,
        "source": f"family:{ORACLE}",
        "content": ANSWER,
    }


def test_the_wrapper_never_loses_the_untrusted_flag() -> None:
    """Invariant 14: a delegated answer is data. The wrapper is the only
    shape the caller ever sees, so the flag cannot be argued away."""
    assert wrap_untrusted(ORACLE, "")["untrusted"] is True


# ---- the door's own token (invariant 13) -----------------------------------


def test_the_token_is_read_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "door-delegate.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    assert read_door_token(path) == TOKEN


def test_a_short_token_refuses_to_load(tmp_path: Path) -> None:
    path = tmp_path / "door-delegate.token"
    path.write_text("tiny", encoding="utf-8")
    with pytest.raises(DoorTokenError) as caught:
        read_door_token(path)
    assert "tiny" not in str(caught.value)


def test_a_missing_token_file_refuses_to_load(tmp_path: Path) -> None:
    with pytest.raises(DoorTokenError):
        read_door_token(tmp_path / "absent.token")
