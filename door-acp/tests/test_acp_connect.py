"""`creche-acp-connect`: one `initialize` line through a fake door and back."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from agent_door_acp.connect import main
from websockets.asyncio.server import ServerConnection, serve

KEY = "k" * 32
INITIALIZE = json.dumps(
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": 1}}
)
ANSWER = json.dumps({"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": 1}})


@pytest.mark.slow
async def test_one_initialize_round_trips_through_the_door(tmp_path: Path) -> None:
    key_file = tmp_path / "door-acp.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    seen: list[tuple[str, str, str]] = []

    async def door(connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        frame = await connection.recv()
        assert isinstance(frame, str)
        seen.append((request.path, request.headers.get("Authorization", ""), frame))
        await connection.send(ANSWER)
        await connection.wait_closed()

    async with serve(door, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        bridge = await asyncio.create_subprocess_exec(
            *(sys.executable, "-m", "agent_door_acp.connect"),
            *(f"ws://127.0.0.1:{port}/acp/chat", "--key-file", str(key_file)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        assert bridge.stdin is not None
        assert bridge.stdout is not None

        bridge.stdin.write(INITIALIZE.encode("utf-8") + b"\n")
        await bridge.stdin.drain()
        line = await asyncio.wait_for(bridge.stdout.readline(), 10)
        bridge.stdin.close()
        code = await asyncio.wait_for(bridge.wait(), 10)

    assert line == ANSWER.encode("utf-8") + b"\n"
    assert seen == [("/acp/chat", f"Bearer {KEY}", INITIALIZE)]
    assert code == 0


def test_a_group_readable_key_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "door-acp.key"
    key_file.write_text(KEY, encoding="utf-8")
    os.chmod(key_file, 0o640)

    assert main(["ws://127.0.0.1:1/acp/chat", "--key-file", str(key_file)]) == 2
    assert KEY not in capsys.readouterr().err
