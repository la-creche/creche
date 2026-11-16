"""A stand-in for the scanner socket that the vmm serves.

`FakeScanner` listens on a Unix socket in a temporary directory. Each link
carries one request line, as the scan link does (design note attachments
§4). `mode` says what the fake answers. A test sets `gate` to hold each
reply until the test sets the event.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from attendance.scanner import LINE_CAP, PDF_TYPES

#: The text of the `.txt` file that the fake makes of each PDF.
PDF_TEXT = b"the text of the pages\n"

#: The type that the fake gives a rebuilt image that is not JPEG.
PNG = "image/png"
JPEG = "image/jpeg"


class ScanMode(StrEnum):
    """What the fake answers to each request."""

    #: The file as it is. A PDF also gets its `<name>.txt`. An image that
    #: is not JPEG becomes PNG.
    REBUILD = "rebuild"
    #: `{"refused": {"reason": reason}}`.
    REFUSE = "refuse"
    #: The line `raw`, as it is.
    RAW = "raw"
    #: Close the link at once, before a byte is read: the link cap.
    CLOSE = "close"
    #: Write `{"refused": {"reason": "busy"}}` before a byte is read, then
    #: close: the vmm at its link cap.
    CAP_LINE = "cap_line"
    #: Read the request, then close with no reply.
    SILENT = "silent"
    #: Read the request and never answer.
    HANG = "hang"


@dataclass(slots=True)
class FakeScanner:
    socket: Path
    mode: ScanMode = ScanMode.REBUILD
    reason: str = "mismatch"
    raw: bytes = b"not json\n"
    #: Each reply waits for this event, when it is set.
    gate: asyncio.Event | None = None
    #: Each request, as the fake read it.
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: The links that wait at the gate now.
    waiting: int = 0
    #: Set at the end of the test. A hung link then ends.
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.mode is ScanMode.CLOSE:
            writer.transport.abort()
            return

        if self.mode is ScanMode.CAP_LINE:
            writer.write(_line({"refused": {"reason": "busy"}}))
            writer.close()

            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

            return

        try:
            await self._answer(reader, writer)
        finally:
            # A close sends each byte of the reply first. An abort would
            # drop the bytes that wait in the buffer.
            writer.close()

            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        request: dict[str, Any] = json.loads(line)
        self.requests.append(request)

        if self.gate is not None:
            self.waiting += 1
            await self.gate.wait()
            self.waiting -= 1

        if self.mode is ScanMode.SILENT:
            return

        if self.mode is ScanMode.HANG:
            await self.stopped.wait()
            return

        # The host may have closed the link already, after a cancel.
        with contextlib.suppress(ConnectionError):
            writer.write(self._reply(request))
            await writer.drain()

    def _reply(self, request: dict[str, Any]) -> bytes:
        if self.mode is ScanMode.RAW:
            return self.raw

        if self.mode is ScanMode.REFUSE:
            return _line({"refused": {"reason": self.reason}})

        return _line({"ok": {"files": rebuilt_files(request)}})


def rebuilt_files(request: dict[str, Any]) -> list[dict[str, Any]]:
    """What `ScanMode.REBUILD` answers for one request."""
    name, mime_type, data = request["name"], request["mime_type"], request["data"]

    if mime_type in PDF_TYPES:
        text = base64.b64encode(PDF_TEXT).decode("ascii")
        pdf = {"name": name, "mime_type": mime_type, "data": data}
        return [pdf, {"name": f"{name}.txt", "mime_type": "text/plain", "data": text}]

    if mime_type.startswith("image/") and mime_type != JPEG:
        mime_type = PNG

    return [{"name": name, "mime_type": mime_type, "data": data}]


def _line(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


@contextlib.asynccontextmanager
async def serve_scanner() -> AsyncGenerator[FakeScanner]:
    """One fake on a fresh socket. AF_UNIX cannot bind past about 104 bytes
    on macOS, so the socket is not under `tmp_path`."""
    folder = Path(tempfile.mkdtemp(prefix="scan-"))
    fake = FakeScanner(folder / "scanner.sock")
    server = await asyncio.start_unix_server(fake.handle, str(fake.socket), limit=LINE_CAP + 1)

    try:
        yield fake
    finally:
        fake.stopped.set()

        if fake.gate is not None:
            fake.gate.set()

        server.close()
        server.close_clients()
        await server.wait_closed()
        shutil.rmtree(folder, ignore_errors=True)
