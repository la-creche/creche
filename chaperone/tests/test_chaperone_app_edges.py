"""Four edges of `app.py`.

1. A client that leaves in the middle of a body.
2. The answer to a body over the cap, when the response reads the request.
3. The dispatch door of a config.
4. The delegate door at the stop of the app.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest
from chaperone.app import (
    MAX_REQUEST_BODY_BYTES,
    BodySizeLimitMiddleware,
    PepConfig,
    build_dispatch_door,
    create_app,
)
from chaperone.audit import AuditLog
from chaperone.delegate import DoorConfig, HttpDelegateDoor
from chaperone.dispatch import HttpDispatchDoor
from chaperone_helpers import FakePool
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

HTTP_TOO_LARGE = 413

HTTP_SCOPE: Scope = {"type": "http"}
CLIENT_LEFT: Message = {"type": "http.disconnect"}

TOKEN = "t" * 32


def config(tmp_path: Path, **over: object) -> PepConfig:
    fields = {"audit_dir": tmp_path / "audit", "rework_dir": tmp_path / "rework", **over}

    return PepConfig(**fields)  # pyright: ignore[reportArgumentType]


def receive_of(messages: Iterable[Message]) -> Receive:
    """A request that gives these messages, one for each read."""
    left = iter(messages)

    async def receive() -> Message:
        return next(left)

    return receive


def lines_of(directory: Path) -> list[str]:
    """Each line of each day file of a log."""
    return [
        line
        for path in sorted(directory.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


# ---- the body cap ---------------------------------------------------------------


async def test_a_client_that_leaves_mid_body_ends_the_read(tmp_path: Path) -> None:
    """The read of a body ends at a message that is no part of a body. The
    app then runs one time, and the body is no body over the cap."""
    runs: list[Scope] = []
    sent: list[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        runs.append(scope)

    async def send(message: Message) -> None:
        sent.append(message)

    part: Message = {"type": "http.request", "body": b'{"tool"', "more_body": True}
    middleware = BodySizeLimitMiddleware(
        inner, max_bytes=MAX_REQUEST_BODY_BYTES, audit=AuditLog(tmp_path / "audit")
    )

    await middleware(HTTP_SCOPE, receive_of([part, CLIENT_LEFT]), send)

    assert runs == [HTTP_SCOPE]
    assert sent == []
    assert lines_of(tmp_path / "audit") == []


async def test_the_answer_to_a_large_body_reads_no_more_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response can read the request while it answers. The answer to a body
    over the cap gets a request whose client left, so it reads no more bytes
    of that body."""
    read: list[Message] = []
    runs: list[Scope] = []
    sent: list[Message] = []

    class ReadsTheRequest(JSONResponse):
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            read.append(await receive())
            await super().__call__(scope, receive, send)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        runs.append(scope)

    async def send(message: Message) -> None:
        sent.append(message)

    monkeypatch.setattr("chaperone.app.JSONResponse", ReadsTheRequest)
    over: Message = {"type": "http.request", "body": b"12345", "more_body": True}
    middleware = BodySizeLimitMiddleware(inner, max_bytes=4, audit=AuditLog(tmp_path / "audit"))

    await middleware(HTTP_SCOPE, receive_of([over]), send)

    assert read == [CLIENT_LEFT]
    assert runs == []
    assert sent[0]["status"] == HTTP_TOO_LARGE
    assert len(lines_of(tmp_path / "audit")) == 1


# ---- the dispatch door ---------------------------------------------------------


def test_half_a_configuration_builds_no_dispatch_door(tmp_path: Path) -> None:
    assert build_dispatch_door(config(tmp_path)) is None
    assert build_dispatch_door(config(tmp_path, dispatch_token=TOKEN)) is None
    assert build_dispatch_door(config(tmp_path, attendance_socket=tmp_path / "s.sock")) is None


def test_a_token_and_a_socket_build_a_dispatch_door(tmp_path: Path) -> None:
    built = config(tmp_path, dispatch_token=TOKEN, attendance_socket=tmp_path / "s.sock")

    assert isinstance(build_dispatch_door(built), HttpDispatchDoor)


# ---- the stop of the app -------------------------------------------------------


class CountedDoor(HttpDelegateDoor):
    """A delegate door that counts each close."""

    closes = 0

    async def aclose(self) -> None:
        self.closes += 1
        await super().aclose()


def test_the_app_closes_its_delegate_door_at_the_stop(tmp_path: Path) -> None:
    door = CountedDoor(DoorConfig(token=TOKEN, socket_path=tmp_path / "s.sock"))
    app = create_app(config(tmp_path), pool=FakePool(), delegate_door=door)

    with TestClient(app):
        assert door.closes == 0

    assert door.closes == 1
