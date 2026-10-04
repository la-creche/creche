"""The wire to `attendance` (`attendancehttp.py`).

`httpx.MockTransport` stands for the socket, so no test opens one.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from noticeboard.attendancehttp import HttpTransport

TOKEN = "v" * 40
LIST_PATH = "/v1/sessions"


def transport(handler: Callable[[httpx.Request], httpx.Response]) -> HttpTransport:
    client = httpx.Client(base_url="http://sessiond", transport=httpx.MockTransport(handler))

    return HttpTransport(client)


def answers_empty(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"{}")


def test_an_answer_comes_back_with_its_status_and_its_bytes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, content=b'{"error":{"code":"not_found"}}')

    reply = transport(handler).get(LIST_PATH, {"limit": "5"}, TOKEN)

    assert reply.problem == ""
    assert reply.status == 404
    assert reply.body == b'{"error":{"code":"not_found"}}'
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(seen[0].url)


def test_a_dead_attendance_becomes_a_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    reply = transport(handler).get(LIST_PATH, {}, TOKEN)

    assert reply.problem == "cannot reach attendance: ConnectError: connection refused"


def test_an_error_that_quotes_the_request_names_no_part_of_it() -> None:
    """The client refuses a header it cannot send, and the text of that
    error holds the header. The header holds the token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {TOKEN}'")

    reply = transport(handler).get(LIST_PATH, {}, TOKEN)

    assert reply.problem
    assert TOKEN not in reply.problem
    assert "Bearer" not in reply.problem


def test_a_token_that_no_header_carries_becomes_a_report() -> None:
    """The client raises UnicodeEncodeError for it, not an HTTP error."""
    token = TOKEN + "é"

    reply = transport(answers_empty).get(LIST_PATH, {}, token)

    assert reply.problem
    assert token not in reply.problem
    assert "é" not in reply.problem


def test_a_path_that_is_no_url_becomes_a_report() -> None:
    """The client raises InvalidURL for it, which is not an HTTP error."""
    reply = transport(answers_empty).get(f"{LIST_PATH}/chat/a\x00b", {}, TOKEN)

    assert reply.problem
    assert reply.status == 0
