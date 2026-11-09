"""The client of the vmm host API over a real Unix socket (decisions CH20, CH25).

A stub server stands in for the vmm. It reads one HTTP request per
connection and answers what the test scripted for its path.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from attendance.faults import FaultCode, FaultReporter
from attendance.vmm import (
    MISMATCH_TRIES,
    UNREACHABLE,
    UNREADABLE,
    VMM_PROTOCOL_MISMATCH,
    HttpVmm,
    VmmRefused,
)
from creche_wire import (
    CreateSession,
    Door,
    DoorOrigin,
    FamilyName,
    ListSessions,
    SessionCwd,
    SessionId,
    SessionOrigin,
    UnixMillis,
)
from creche_wire.routes import ACP, HOST_API_HEADER, HOST_API_PROTOCOL, MISMATCH_STATUS

from attendance import vmm as vmm_module

FAMILY = "chat"
SESSION = "78c1103d-6e82-4492-a598-bfb2e1e4eff1"
CREATED_MS = 1_792_929_600_000
CWD = "/home/operator/la-creche"


def create() -> CreateSession:
    return CreateSession(
        created_at=UnixMillis(CREATED_MS),
        cwd=SessionCwd(CWD),
        family=FamilyName(FAMILY),
        id=SessionId(SESSION),
        origin=SessionOrigin(DoorOrigin(door=Door())),
    )


def row() -> dict[str, object]:
    return {
        "created_at": CREATED_MS,
        "cwd": CWD,
        "family": FAMILY,
        "id": SESSION,
        "origin": {"door": {}},
        "title": None,
        "updated_at": CREATED_MS,
    }


@dataclass(slots=True)
class Seen:
    """One request the stub read."""

    path: str
    headers: dict[str, str]
    body: bytes


@dataclass(slots=True)
class Stub:
    """The scripted answers by path: a status and a body, used in order."""

    socket: Path
    answers: dict[str, list[tuple[int, bytes]]] = field(
        default_factory=dict[str, list[tuple[int, bytes]]]
    )
    seen: list[Seen] = field(default_factory=list[Seen])
    #: Set: after the echo the stream reads to the client's end, sets
    #: `saw_end` and waits for `release` before it ends too.
    release: asyncio.Event | None = None
    saw_end: asyncio.Event = field(default_factory=asyncio.Event)

    def answer(self, path: str, status: int, body: object) -> None:
        text = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.answers.setdefault(path, []).append((status, text))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        _, path, _ = lines[0].split(" ")
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (one.partition(":") for one in lines[1:] if one)
        }
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.seen.append(Seen(path, headers, body))
        status, text = self.answers[path].pop(0)

        if status == 101:
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\n")
            writer.write(f"Connection: Upgrade\r\nUpgrade: {ACP.token}\r\n\r\n".encode())
            await writer.drain()
            line = await reader.readline()
            writer.write(b"echo " + line)
            await writer.drain()

            if self.release is not None:
                await reader.read()
                self.saw_end.set()
                await self.release.wait()

            writer.close()
            return

        reason = "OK" if status < 300 else "Refused"
        writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
        writer.write(f"Content-Type: application/json\r\nContent-Length: {len(text)}\r\n".encode())
        writer.write(b"Connection: close\r\n\r\n" + text)
        await writer.drain()
        writer.close()


@dataclass(slots=True)
class Rig:
    stub: Stub
    vmm: HttpVmm
    faults: FaultReporter


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    monkeypatch.setattr(vmm_module, "MISMATCH_PAUSE_S", 0.0)
    # AF_UNIX cannot bind past about 104 bytes on macOS. tmp_path is close.
    socket_dir = Path(tempfile.mkdtemp(prefix="vmm-"))
    stub = Stub(socket_dir / "vmm.sock")
    server = await asyncio.start_unix_server(stub.handle, str(stub.socket))
    faults = FaultReporter(tmp_path)
    client = HttpVmm.over_socket(stub.socket, faults)

    yield Rig(stub, client, faults)

    await client.aclose()
    server.close()
    await server.wait_closed()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def test_create_sends_the_typed_body_with_the_protocol_header(rig: Rig) -> None:
    rig.stub.answer("/create", 200, row())

    made = await rig.vmm.create(create())

    (seen,) = rig.stub.seen
    assert made.id.root == SESSION
    assert seen.headers[HOST_API_HEADER] == str(HOST_API_PROTOCOL)
    assert seen.headers["content-type"] == "application/json"
    sent = {name: value for name, value in row().items() if name not in {"title", "updated_at"}}
    assert json.loads(seen.body) == sent


async def test_a_route_error_raises_its_code(rig: Rig) -> None:
    rig.stub.answer("/create", 409, {"code": "conflict"})

    with pytest.raises(VmmRefused) as refused:
        await rig.vmm.create(create())

    assert refused.value.code == "conflict"


async def test_a_body_of_another_shape_is_unreadable(rig: Rig) -> None:
    rig.stub.answer("/list", 200, {"rows": "no"})

    with pytest.raises(VmmRefused) as refused:
        await rig.vmm.list_page(ListSessions(cursor=None, cwd=None, family=FamilyName(FAMILY)))

    assert refused.value.code == UNREADABLE


async def test_a_drop_names_the_session_key(rig: Rig) -> None:
    rig.stub.answer("/drop", 200, {})

    await rig.vmm.drop(FAMILY, SESSION)

    assert json.loads(rig.stub.seen[0].body) == {"session": {"family": FAMILY, "id": SESSION}}


async def test_three_mismatches_raise_the_fault(rig: Rig) -> None:
    """Decisions CH25 rule 1, CH33 rule 10, CH34 rule 2: 3 requests in all."""
    for _ in range(MISMATCH_TRIES):
        rig.stub.answer("/list", MISMATCH_STATUS, {"code": "protocol_mismatch", "protocol": 3})

    with pytest.raises(VmmRefused) as refused:
        await rig.vmm.list_page(ListSessions(cursor=None, cwd=None, family=FamilyName(FAMILY)))

    assert refused.value.code == VMM_PROTOCOL_MISMATCH
    assert len(rig.stub.seen) == MISMATCH_TRIES
    (fault,) = rig.faults.open_faults(FAMILY)
    assert fault.code is FaultCode.VMM_PROTOCOL_MISMATCH


async def test_a_reply_with_no_mismatch_clears_the_fault(rig: Rig) -> None:
    """Contract 05 §3.3.1 rule 8, decision CH82 rule 4."""
    rig.faults.raise_fault(FAMILY, FaultCode.VMM_PROTOCOL_MISMATCH, "before")
    rig.stub.answer("/list", MISMATCH_STATUS, {"code": "protocol_mismatch", "protocol": 3})
    rig.stub.answer("/list", 200, {"next": None, "rows": []})

    page = await rig.vmm.list_page(ListSessions(cursor=None, cwd=None, family=FamilyName(FAMILY)))

    assert page.rows == []
    assert rig.faults.open_faults(FAMILY) == []


async def test_an_absent_socket_is_unreachable(tmp_path: Path) -> None:
    """Decision CH20: ENOENT is transient."""
    client = HttpVmm.over_socket(tmp_path / "none.sock", FaultReporter(tmp_path))

    with pytest.raises(VmmRefused) as refused:
        await client.drop(FAMILY, SESSION)

    assert refused.value.code == UNREACHABLE
    await client.aclose()


async def test_the_acp_route_turns_into_a_stream(rig: Rig) -> None:
    """Decision CH20: after 101 the connection carries raw ACP lines."""
    rig.stub.answer("/acp", 101, b"")

    stream = await rig.vmm.open_acp(FAMILY, SESSION, 1024)
    await stream.send('{"jsonrpc":"2.0","id":0}\n')
    echoed = await stream.receive()
    end = await stream.receive()
    await stream.close()

    (seen,) = rig.stub.seen
    assert seen.headers["upgrade"] == ACP.token
    assert seen.headers["connection"].lower() == "upgrade"
    assert json.loads(seen.body) == {"family": FAMILY, "id": SESSION}
    assert echoed is not None
    assert echoed.text == 'echo {"jsonrpc":"2.0","id":0}'
    assert end is None


async def test_an_acp_refusal_before_101_raises_its_code(rig: Rig) -> None:
    """Decision CH21-9: `not_found` comes before 101, with no boot."""
    rig.stub.answer("/acp", 404, {"code": "not_found"})

    with pytest.raises(VmmRefused) as refused:
        await rig.vmm.open_acp(FAMILY, SESSION, 1024)

    assert refused.value.code == "not_found"


async def test_a_close_ends_the_write_half_and_waits_for_the_vmm(rig: Rig) -> None:
    """Decision CH83 rule 4: the vmm holds the claim until both directions
    end, so the close returns only after the vmm ended the stream."""
    rig.stub.answer("/acp", 101, b"")
    rig.stub.release = asyncio.Event()
    stream = await rig.vmm.open_acp(FAMILY, SESSION, 1024)
    await stream.send('{"jsonrpc":"2.0","id":0}\n')
    await stream.receive()
    closing = asyncio.create_task(stream.close())

    await asyncio.wait_for(rig.stub.saw_end.wait(), 5)
    await asyncio.sleep(0.05)
    assert not closing.done()
    rig.stub.release.set()
    await asyncio.wait_for(closing, 5)


async def test_a_close_waits_for_the_vmm_at_most_its_bound(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vmm_module, "CLOSE_WAIT_S", 0.05)
    rig.stub.answer("/acp", 101, b"")
    rig.stub.release = asyncio.Event()
    stream = await rig.vmm.open_acp(FAMILY, SESSION, 1024)
    await stream.send('{"jsonrpc":"2.0","id":0}\n')
    await stream.receive()

    await asyncio.wait_for(stream.close(), 5)
    assert rig.stub.saw_end.is_set()
    rig.stub.release.set()
