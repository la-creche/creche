"""`creche-acp-connect`: stdio NDJSON to the door's `WS /acp/<family>`.

For an ACP client that speaks stdio only. The client starts this program as
its agent. Each stdin line goes to the door as one text frame, without its
line break. Each text frame from the door goes to stdout as one line. The
bearer comes from a key file, as `Authorization: Bearer`. It is never an
argument, because argv is visible to every user of the machine.

Exit 0 when stdin ends or the door closes with 1000. Exit 2 for a bad key
file or a stdin that is not a pipe. Else exit 1, and stderr names the cause.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import BinaryIO

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import ConfigError, read_key_file
from .forward import MAX_FRAME_BYTES, Close

PROG = "creche-acp-connect"
_ARG_KEY_FILE = "--key-file"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description="stdio NDJSON to the door's WebSocket")
    parser.add_argument("url", help="the door's URL: ws://<host>:<port>/acp/<family>")
    parser.add_argument(_ARG_KEY_FILE, required=True, help="the door's bearer, mode 0600")
    args = parser.parse_args(argv)

    try:
        key = read_key_file(_ARG_KEY_FILE, args.key_file)
    except ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2

    return asyncio.run(bridge(args.url, key, sys.stdin.buffer, sys.stdout.buffer))


async def bridge(url: str, key: bytes, stdin: BinaryIO, stdout: BinaryIO) -> int:
    """Copy lines and frames both ways until one side ends."""
    loop = asyncio.get_running_loop()
    # A line break and a carriage return beyond the cap.
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES + 2)
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), stdin)
    except ValueError as exc:
        print(f"{PROG}: stdin must be a pipe: {exc}", file=sys.stderr)
        return 2

    try:
        door = await connect(
            url,
            additional_headers={"Authorization": "Bearer " + key.decode("ascii")},
            max_size=MAX_FRAME_BYTES,
        )
    except (OSError, TimeoutError, WebSocketException) as exc:
        print(f"{PROG}: cannot open {url}: {exc}", file=sys.stderr)
        return 1

    async with door:
        lines = asyncio.create_task(_lines_to_door(reader, door))
        frames = asyncio.create_task(_frames_to_stdout(door, stdout))
        await asyncio.wait({lines, frames}, return_when=asyncio.FIRST_COMPLETED)
        lines.cancel()
        frames.cancel()
        await asyncio.gather(lines, frames, return_exceptions=True)

    if door.close_code in (None, Close.NORMAL):
        return 0

    print(f"{PROG}: the door closed: {door.close_code} {door.close_reason}", file=sys.stderr)
    return 1


async def _lines_to_door(reader: asyncio.StreamReader, door: ClientConnection) -> None:
    while True:
        try:
            line = await reader.readline()
        except ValueError:
            await door.close(Close.TOO_LARGE)
            return

        if not line:
            await door.close(Close.NORMAL)
            return

        text = line.rstrip(b"\n").rstrip(b"\r")
        if not text:
            continue

        try:
            await door.send(text.decode("utf-8"))
        except UnicodeDecodeError:
            await door.close(Close.BAD_FRAME)
            return
        except ConnectionClosed:
            return


async def _frames_to_stdout(door: ClientConnection, stdout: BinaryIO) -> None:
    async for frame in door:
        if isinstance(frame, bytes):
            await door.close(Close.BAD_FRAME)
            return

        stdout.write(frame.encode("utf-8") + b"\n")
        stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
