"""The client of the vmm host API (decisions CH20, CH21-7, CH25).

The vmm keeps the host session list and one VM per session (decision CH1).
attendance makes each row, reads the list, drops the row of a job or of a
client `session/delete`, and opens one ACP stream per session:

    attendance --- POST /create, /list, /drop ---------> vmm
               --- POST /acp + Upgrade: creche-acp ---> vmm --- guest port 1201 --> facade

The transport is HTTP/1.1 over the Unix socket `vmm.sock` (decision CH20).
Every client runs as the operator, so the mode of the socket directory is
the guard: no token. The unit sets `ATTENDANCE_VMM_SOCKET` to
`%t/vmm/vmm.sock`.

A route constant of `creche_wire.routes` pairs the request, the reply and
the error types (decision CH25), so pyright refuses a wrong model here.
After 101 the connection carries raw ACP lines (decision CH20). The vmm
parses none of them.

ENOENT, ECONNREFUSED and EOF are transient (decision CH20): a vmm restart
drops each connection. A call that meets one raises `VmmRefused` with the
code `unreachable`, and the next request tries again.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path
from typing import Final, Protocol

import httpcore
import httpx
from creche_wire import (
    AcpError,
    CreateError,
    CreateSession,
    DropError,
    Dropped,
    DropSession,
    DropTarget,
    FamilyName,
    ListError,
    ListSessions,
    OpenAcp,
    ProtocolMismatch,
    SessionDrop,
    SessionId,
    SessionPage,
    SessionRow,
    read,
)
from creche_wire.routes import (
    ACP,
    CREATE,
    DROP,
    DROP_CLIENT_TIMEOUT_S,
    HOST_API_BODY_CAP,
    HOST_API_HEADER,
    HOST_API_PROTOCOL,
    LIST,
    MISMATCH_STATUS,
)
from pydantic import BaseModel

from .channel import Channel, ChannelClosed
from .faults import FaultCode, FaultReporter
from .lines import LineSplitter, RawLine

#: The env key that names `vmm.sock` (decision CH20).
#:
#: CONTRACT-QUESTION: decision CH20 fixes the path
#: `/run/user/<uid>/vmm/vmm.sock` and names no env key. The env catalog
#: cannot spell a uid, so `default.env` holds no key for it. The reading
#: here, the reading of caregiver too: the unit sets this key from `%t`,
#: outside the catalog. To make it a catalog key, the catalog grammar needs
#: a form for the runtime dir of an account.
VMM_SOCKET_ENV: Final = "ATTENDANCE_VMM_SOCKET"

#: Decision CH33 rule 10: after a mismatch, attendance tries 3 times 2 s
#: apart, then records the fault `vmm_protocol_mismatch`. Decision CH34 rule
#: 2: the 3 requests include the first one.
MISMATCH_TRIES: Final = 3
MISMATCH_PAUSE_S: Final = 2.0

#: The host part of the URL. The transport dials the socket, so it names
#: nothing.
SOCKET_BASE_URL: Final = "http://vmm"

#: The code of a vmm that did not answer: ENOENT, ECONNREFUSED or EOF
#: (decision CH20).
UNREACHABLE: Final = "unreachable"

#: The code of an answer that does not read as the type of its route.
UNREADABLE: Final = "unreadable"

#: The code of a vmm that answered the mismatch reply on each try
#: (contract 05 §3.3).
VMM_PROTOCOL_MISMATCH: Final = FaultCode.VMM_PROTOCOL_MISMATCH.value

#: The code of the `acp` route for a session the family has no row of
#: (decisions CH21-9, CH33 rule 5).
NOT_FOUND: Final = "not_found"

#: The code of the `acp` route when the host has no room for the VM. The
#: vmm never queues (decision CH21-3).
HOST_FULL: Final = "host_full"

#: How much of the ACP stream one read takes.
READ_CHUNK_BYTES: Final = 65_536

#: How long a close of an ACP stream waits for the vmm to end the stream.
#: The vmm ends it when the facade closes its side, one guest round trip.
#:
#: CONTRACT-QUESTION: decision CH83 rule 4 gives a session one holder, and
#: no row says how a holder ends its stream so that its next Upgrade meets
#: no claim of its own. The reading here: end the write half, read to the
#: end, and wait at most this long. Past it the close releases the socket.
CLOSE_WAIT_S: Final = 5.0

#: The `acp` route answers 101 after the start of the VM, and the start has
#: no outer bound (decision CH50 rule 1). So its reply has no read timeout.
_ACP_TIMEOUT: Final = httpx.Timeout(DROP_CLIENT_TIMEOUT_S, read=None)

_SWITCHING: Final = 101
_SUCCESS: Final = range(200, 300)


class VmmRefused(Exception):
    """The vmm did not do what attendance asked.

    `code` is the code of the route error, `vmm_protocol_mismatch`,
    `unreachable` or `unreadable`. Callers match on it (decision CH25 rule
    3)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class AcpStream:
    """One vmm ACP stream after 101: raw ACP lines to the facade of one
    session (decision CH20). It frames the bytes with `LineSplitter`, LF
    only, with a cap per line (attendance trust rule 8)."""

    def __init__(
        self, response: httpx.Response, stream: httpcore.AsyncNetworkStream, max_line_bytes: int
    ) -> None:
        self._response = response
        self._stream = stream
        self._splitter = LineSplitter(max_line_bytes)
        self._pending: list[RawLine] = []
        self._eof = False
        self._closed = False

    async def send(self, line: str) -> None:
        if self._closed:
            raise ChannelClosed("the vmm stream is closed")

        try:
            await self._stream.write(line.encode("utf-8", "surrogatepass"))
        except (httpcore.NetworkError, OSError) as error:
            raise ChannelClosed(f"the vmm stream write failed: {type(error).__name__}") from error

    async def receive(self) -> RawLine | None:
        while not self._pending:
            if self._eof or self._closed:
                return None

            try:
                chunk = await self._stream.read(READ_CHUNK_BYTES)
            except (httpcore.NetworkError, OSError):
                chunk = b""

            if not chunk:
                self._eof = True
                continue

            self._pending.extend(self._splitter.feed(chunk))

        return self._pending.pop(0)

    async def close(self) -> None:
        """End the write half, wait for the vmm to end the stream, then
        release it. The vmm holds the claim of the session until both
        directions end (decision CH83 rule 4). So after this close the next
        Upgrade of the same holder meets no claim of its own. The bytes of
        the wait are dropped. `CLOSE_WAIT_S` bounds it."""
        if self._closed:
            return

        self._closed = True
        raw = self._stream.get_extra_info("socket")

        if isinstance(raw, socket.socket):
            with contextlib.suppress(httpcore.NetworkError, OSError, TimeoutError):
                async with asyncio.timeout(CLOSE_WAIT_S):
                    raw.shutdown(socket.SHUT_WR)

                    while not self._eof and await self._stream.read(READ_CHUNK_BYTES):
                        pass

        with contextlib.suppress(httpcore.NetworkError, OSError):
            await self._stream.aclose()

        await self._response.aclose()


class Vmm(Protocol):
    """The calls of attendance to the vmm. Each raises `VmmRefused`."""

    async def create(self, session: CreateSession) -> SessionRow:
        """Make one row of the host session list (decision CH25 rule 2)."""
        ...

    async def list_page(self, query: ListSessions) -> SessionPage:
        """One page of the rows of one family (decision CH21-5)."""
        ...

    async def drop(self, family: str, session: str) -> None:
        """Drop one session: its VM, snapshot, disk and row (decisions CH21-2,
        CH27)."""
        ...

    async def open_acp(self, family: str, session: str, max_line_bytes: int) -> Channel:
        """The ACP stream of one session (decisions CH20, CH21-7). It boots
        or restores the VM first (decision CH21-9)."""
        ...


class HttpVmm:
    """The vmm on `vmm.sock`. One `httpx.AsyncClient` serves every call.

    A reply with no mismatch clears the faults of attendance for the family
    of the call (contract 05 §3.3.1 rule 8, decision CH82 rule 4). The last
    mismatch raises `vmm_protocol_mismatch`, which blocks no turn (decision
    CH82 rule 3)."""

    def __init__(self, client: httpx.AsyncClient, faults: FaultReporter) -> None:
        self._client = client
        self._faults = faults

    @classmethod
    def over_socket(cls, socket: Path, faults: FaultReporter) -> HttpVmm:
        """The vmm on its Unix socket. A drop stops a VM in 35 s at most, so
        each JSON call waits `DROP_CLIENT_TIMEOUT_S` for its reply
        (decisions CH33 rule 11, CH34 rule 5)."""
        transport = httpx.AsyncHTTPTransport(uds=str(socket))
        return cls(httpx.AsyncClient(transport=transport, timeout=DROP_CLIENT_TIMEOUT_S), faults)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create(self, session: CreateSession) -> SessionRow:
        family = session.family.root
        response = await self._call(family, CREATE.path, CREATE.encode(session))

        if response.status_code in _SUCCESS:
            return _read(SessionRow, response)

        raise _refused(_read(CreateError, response).root.code, CREATE.path, response)

    async def list_page(self, query: ListSessions) -> SessionPage:
        response = await self._call(query.family.root, LIST.path, LIST.encode(query))

        if response.status_code in _SUCCESS:
            return _read(SessionPage, response)

        raise _refused(_read(ListError, response).root.code, LIST.path, response)

    async def drop(self, family: str, session: str) -> None:
        key = DropSession(family=FamilyName(family), id=SessionId(session))
        target = DropTarget(SessionDrop(session=key))
        response = await self._call(family, DROP.path, DROP.encode(target))

        if response.status_code in _SUCCESS:
            _read(Dropped, response)
            return

        raise _refused(_read(DropError, response).root.code, DROP.path, response)

    async def open_acp(self, family: str, session: str, max_line_bytes: int) -> AcpStream:
        body = ACP.encode(OpenAcp(family=FamilyName(family), id=SessionId(session)))
        upgrade = {"Connection": "Upgrade", "Upgrade": ACP.token}
        response = await self._call(family, ACP.path, body, upgrade, _ACP_TIMEOUT)

        if response.status_code != _SWITCHING:
            raise _refused(_read(AcpError, response).root.code, ACP.path, response)

        stream = response.extensions.get("network_stream")

        if not isinstance(stream, httpcore.AsyncNetworkStream):
            await response.aclose()
            raise VmmRefused(UNREADABLE, "the 101 of the acp route carries no stream")

        return AcpStream(response, stream, max_line_bytes)

    async def _call(
        self,
        family: str,
        path: str,
        body: bytes,
        more: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        """One request, sent again after each mismatch reply, up to
        `MISMATCH_TRIES` requests in all, 2 s apart (decisions CH25 rule 1,
        CH33 rule 10). Each reply body is read, except the stream of a
        101."""
        for _ in range(MISMATCH_TRIES - 1):
            answer = await self._answered(family, path, body, more, timeout)

            if isinstance(answer, httpx.Response):
                return answer

            await asyncio.sleep(MISMATCH_PAUSE_S)

        answer = await self._answered(family, path, body, more, timeout)

        if isinstance(answer, httpx.Response):
            return answer

        message = (
            f"the vmm speaks host API protocol {answer.protocol}, "
            f"and attendance speaks {HOST_API_PROTOCOL}"
        )
        self._faults.raise_fault(family, FaultCode.VMM_PROTOCOL_MISMATCH, message)
        raise VmmRefused(VMM_PROTOCOL_MISMATCH, message)

    async def _answered(
        self,
        family: str,
        path: str,
        body: bytes,
        more: dict[str, str] | None,
        timeout: httpx.Timeout | None,
    ) -> httpx.Response | ProtocolMismatch:
        """One request: its response, or the mismatch reply it got. A
        response with no mismatch clears the faults of the family."""
        response = await self._send(path, body, more, timeout)

        if response.status_code == MISMATCH_STATUS:
            return _read(ProtocolMismatch, response)

        if self._faults.open_faults(family):
            self._faults.clear_all(family)

        return response

    async def _send(
        self,
        path: str,
        body: bytes,
        more: dict[str, str] | None,
        timeout: httpx.Timeout | None,
    ) -> httpx.Response:
        """One request. A vmm that does not answer is `unreachable`."""
        headers = {HOST_API_HEADER: str(HOST_API_PROTOCOL), "Content-Type": "application/json"}
        headers.update(more or {})
        url = f"{SOCKET_BASE_URL}{path}"
        request = self._client.build_request(
            "POST", url, content=body, headers=headers, timeout=timeout or self._client.timeout
        )

        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise VmmRefused(UNREACHABLE, f"POST {path}: {type(error).__name__}") from error

        if response.status_code == _SWITCHING:
            return response

        try:
            await response.aread()
        except httpx.HTTPError as error:
            raise VmmRefused(UNREACHABLE, f"POST {path}: {type(error).__name__}") from error
        finally:
            await response.aclose()

        return response


def _read[M: BaseModel](model: type[M], response: httpx.Response) -> M:
    """The body of `response` as `model`, under the body cap of the host
    API. A body that does not read is `VmmRefused`, never a default."""
    try:
        return read(model, response.content, cap=HOST_API_BODY_CAP)
    except ValueError as error:
        # `read` raises `NotStrictJson` and `pydantic.ValidationError`, and
        # each is a `ValueError`.
        raise VmmRefused(
            UNREADABLE,
            f"HTTP {response.status_code}: the body is no {model.__name__}: {type(error).__name__}",
        ) from error


def _refused(code: str, path: str, response: httpx.Response) -> VmmRefused:
    return VmmRefused(code, f"POST {path} answered HTTP {response.status_code}")
