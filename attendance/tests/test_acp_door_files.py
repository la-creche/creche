"""The files of a door `session/prompt` (design note attachments §6).

The door checks each `image` and `resource` block, scans each file over the
scanner socket and puts the rebuilt files in the place of the blocks. A
refusal or a `session/cancel` during the scan answers the prompt itself,
and the prompt reaches no stream. A prompt with no file block goes to its
stream as its exact bytes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from attendance.acp_door import (
    INITIALIZE_RESPONSE,
    MAX_FRAME_BYTES,
    MAX_SCANS,
    QUEUED_FRAME_BYTES,
    RPC_INVALID_PARAMS,
    SCAN_OFF,
)
from attendance.api import Listener, build_app
from attendance.auth import Principal, TokenBook
from attendance.scanner import MAX_FILE_BYTES, Reason
from attendance.service import SessionService
from attendance_harness import (
    FAMILY,
    make_config,
    make_service,
    wait_until,
    write_status,
    write_tokens,
)
from fake_scanner import PDF_TEXT, FakeScanner, ScanMode, serve_scanner
from fake_vmm import FakeVmm
from websockets.asyncio.client import ClientConnection, connect, unix_connect

DOOR_PATH = f"ws://attendance/acp/{FAMILY}"
FACADE_VECTORS = Path(__file__).parents[2] / "facade" / "vectors" / "creche-extensions.json"
CWD = "/home/operator/la-creche"
PROMPT_ID = 7

#: How long a test waits to see that nothing happens.
QUIET_S = 0.2

#: How long a test waits for one answer. A large frame takes a while.
ANSWER_S = 30

PNG_BYTES = b"\x89PNG\r\n\x1a\n pixels"
PDF_BYTES = b"%PDF-1.7 pages"
MIB = 1_048_576


class Scan(StrEnum):
    """Whether attendance has a scanner socket."""

    ON = "on"
    OFF = "off"


class _Server(uvicorn.Server):
    """A listener that leaves the signal handlers of the test run alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@dataclass(slots=True)
class Door:
    socket: Path
    tokens: dict[Principal, str]
    vmm: FakeVmm
    scanner: FakeScanner
    service: SessionService

    def connect(self) -> connect:
        headers = {"Authorization": f"Bearer {self.tokens[Principal.DOOR_ACP]}"}
        return unix_connect(str(self.socket), DOOR_PATH, additional_headers=headers, max_size=None)


@pytest.fixture
async def door(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Door]:
    scan = getattr(request, "param", Scan.ON)
    socket_dir = Path(tempfile.mkdtemp(prefix="acp-files-"))
    socket = socket_dir / "attendance.sock"

    async with serve_scanner() as fake:
        scanner_socket = fake.socket if scan is Scan.ON else None
        config = make_config(tmp_path, scanner_socket=scanner_socket)
        write_status(config.state_root)
        made = write_tokens(config.state_root)
        book = TokenBook(config.state_root)
        book.load()
        vmm = FakeVmm()
        service = make_service(config, vmm)
        app = build_app(service, book, Listener.SOCKET)
        server = _Server(uvicorn.Config(app, uds=str(socket), log_level="warning"))
        task = asyncio.create_task(server.serve())

        async with asyncio.timeout(10):
            while not socket.is_socket():
                await asyncio.sleep(0.02)

        yield Door(socket, made, vmm, fake, service)

        server.should_exit = True

        with contextlib.suppress(asyncio.CancelledError):
            await task

        await vmm.stop()

    shutil.rmtree(socket_dir, ignore_errors=True)


# --- the blocks -------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def text(words: str) -> dict[str, Any]:
    return {"type": "text", "text": words}


def image(name: str, data: bytes = PNG_BYTES, mime_type: str = "image/png") -> dict[str, Any]:
    return {"type": "image", "mimeType": mime_type, "data": b64(data), "uri": f"attachment:{name}"}


def blob(name: str, data: bytes, mime_type: str) -> dict[str, Any]:
    resource = {"uri": f"attachment:{name}", "mimeType": mime_type, "blob": b64(data)}
    return {"type": "resource", "resource": resource}


def text_resource(name: str, words: str, mime_type: str = "text/plain") -> dict[str, Any]:
    resource = {"uri": f"attachment:{name}", "mimeType": mime_type, "text": words}
    return {"type": "resource", "resource": resource}


def link(name: str) -> dict[str, Any]:
    return {"type": "resource_link", "uri": f"file:///{name}", "name": name}


# --- the talk ---------------------------------------------------------------


async def new_session(connection: ClientConnection) -> str:
    message: dict[str, Any] = {"cwd": CWD, "mcpServers": []}
    await send(
        connection, {"jsonrpc": "2.0", "id": "new", "method": "session/new", "params": message}
    )
    answer = await answer_of(connection, "new")
    return str(answer["result"]["sessionId"])


async def send(connection: ClientConnection, message: dict[str, Any]) -> None:
    await connection.send(json.dumps(message))


async def send_prompt(
    connection: ClientConnection, session: str, blocks: list[dict[str, Any]]
) -> None:
    params = {"sessionId": session, "prompt": blocks}
    await send(
        connection,
        {"jsonrpc": "2.0", "id": PROMPT_ID, "method": "session/prompt", "params": params},
    )


async def answer_of(connection: ClientConnection, request_id: object) -> dict[str, Any]:
    """The answer to one request. Each update before it is skipped."""
    async with asyncio.timeout(ANSWER_S):
        while True:
            message: dict[str, Any] = json.loads(await connection.recv())

            if "method" not in message and message.get("id") == request_id:
                return message


async def answers_of(connection: ClientConnection, ids: set[object]) -> dict[object, Any]:
    """The answer to each request of `ids`, in any order."""
    found: dict[object, Any] = {}

    async with asyncio.timeout(ANSWER_S):
        while set(found) != ids:
            message: dict[str, Any] = json.loads(await connection.recv())

            if "method" not in message and message.get("id") in ids:
                found[message["id"]] = message

    return found


async def refusal(door: Door, blocks: list[dict[str, Any]]) -> str:
    """The message of the `invalidParams` that answers a prompt of `blocks`.
    The prompt reached no stream."""
    async with door.connect() as connection:
        session = await new_session(connection)
        await send_prompt(connection, session, blocks)
        answer = await answer_of(connection, PROMPT_ID)

    assert answer["error"]["code"] == RPC_INVALID_PARAMS
    assert door.vmm.opened == []
    assert door.service.door_prompts.in_flight == 0
    return str(answer["error"]["message"])


def prompt_sent(door: Door, session: str) -> dict[str, Any]:
    """The one `session/prompt` that reached the facade of `session`."""
    (one,) = [
        line for line in door.vmm.facades[session].received if line["method"] == "session/prompt"
    ]
    return one


# --- the tests --------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("door", [Scan.ON, Scan.OFF], indirect=True)
async def test_a_prompt_with_no_file_reaches_the_stream_as_its_bytes(door: Door) -> None:
    """Design note attachments §6 rules 1 and 6: no scan, no rewrite."""
    async with door.connect() as connection:
        session = await new_session(connection)
        params = {"sessionId": session, "prompt": [text("hi"), link("a.md")]}
        line = json.dumps(
            {"jsonrpc": "2.0", "id": 7.0, "method": "session/prompt", "params": params}
        )
        await connection.send(line)
        answer = await answer_of(connection, PROMPT_ID)

    assert answer["result"] == {"stopReason": "end_turn"}
    assert line + "\n" in door.vmm.facades[session].lines
    assert door.scanner.requests == []


@pytest.mark.slow
@pytest.mark.parametrize("door", [Scan.OFF], indirect=True)
async def test_a_file_is_refused_when_the_scan_is_off(door: Door) -> None:
    """Design note attachments §6 rule 6."""
    assert await refusal(door, [text("see"), image("a.png")]) == SCAN_OFF


@pytest.mark.slow
@pytest.mark.parametrize("door", [Scan.ON, Scan.OFF], indirect=True)
async def test_an_audio_block_is_refused(door: Door) -> None:
    """Design note attachments decision 1: audio is not an allowed kind.
    The prompt reaches no scan and no stream."""
    audio = {"type": "audio", "mimeType": "audio/wav", "data": b64(b"RIFF")}

    message = await refusal(door, [text("hear"), link("a.md"), audio])

    assert message == "audio: block 3 is audio, which is not an allowed kind"
    assert door.scanner.requests == []


@pytest.mark.slow
async def test_the_rebuilt_files_take_the_place_of_their_blocks(door: Door) -> None:
    """Design note attachments §6 rule 4: an image is an image block, each
    other file is a resource blob. A PDF gives two resource blocks."""
    blocks = [
        text("look"),
        image("a.gif", b"GIF89a", "image/gif"),
        link("b.md"),
        blob("doc.pdf", PDF_BYTES, "application/pdf"),
        text_resource("notes.txt", "hé\n"),
    ]

    async with door.connect() as connection:
        session = await new_session(connection)
        await send_prompt(connection, session, blocks)
        answer = await answer_of(connection, PROMPT_ID)

    assert answer["result"] == {"stopReason": "end_turn"}
    assert prompt_sent(door, session)["params"]["prompt"] == [
        text("look"),
        image("a.gif", b"GIF89a", "image/png"),
        link("b.md"),
        blob("doc.pdf", PDF_BYTES, "application/pdf"),
        blob("doc.pdf.txt", PDF_TEXT, "text/plain"),
        blob("notes.txt", "hé\n".encode(), "text/plain"),
    ]
    assert [one["name"] for one in door.scanner.requests] == ["a.gif", "doc.pdf", "notes.txt"]
    assert door.scanner.requests[2]["data"] == b64("hé\n".encode())
    assert door.service.door_prompts.in_flight == 0


def _over(count: int, size: int) -> list[dict[str, Any]]:
    return [blob(f"f{one}.txt", b"a" * size, "text/plain") for one in range(count)]


def _resource(**fields: Any) -> dict[str, Any]:
    return {
        "type": "resource",
        "resource": {"uri": "attachment:a.txt", "mimeType": "text/plain", **fields},
    }


@pytest.mark.slow
@pytest.mark.parametrize(
    ("blocks", "rule"),
    [
        ([image(f"{one}.png") for one in range(5)], "max_files"),
        ([image("a.png", b"a" * (MAX_FILE_BYTES + 1))], "max_file_bytes"),
        ([text_resource("a.txt", "a" * (MAX_FILE_BYTES + 1))], "max_file_bytes"),
        (_over(3, 3_670_016), "max_message_bytes"),
        ([{**image("a.png"), "uri": None}], "uri"),
        ([{k: v for k, v in image("a.png").items() if k != "uri"}], "uri"),
        ([{**image("a.png"), "uri": "file:///a.png"}], "uri"),
        ([image("")], "uri"),
        ([image("a/b.png")], "uri"),
        ([image("a\x00b.png")], "uri"),
        ([image("a\nb.png")], "uri"),
        ([image(".")], "uri"),
        ([image("..")], "uri"),
        ([image("x" * 256)], "uri"),
        ([blob("..", b"a", "text/plain")], "uri"),
        ([{"type": "resource", "resource": "a"}], "resource"),
        ([{"type": "resource"}], "resource"),
        ([_resource()], "resource"),
        ([_resource(blob=b64(b"a"), text="a")], "resource"),
        ([_resource(text=1)], "resource"),
        ([_resource(blob="a b")], "blob"),
        ([{**image("a.png"), "data": "AAA"}], "data"),
        ([{**image("a.png"), "data": None}], "data"),
        ([{**image("a.png"), "mimeType": ""}], "mimeType"),
        ([{k: v for k, v in image("a.png").items() if k != "mimeType"}], "mimeType"),
    ],
    ids=[
        "five-files",
        "file-over",
        "text-over",
        "message-over",
        "uri-null",
        "uri-missing",
        "uri-other-scheme",
        "name-empty",
        "name-slash",
        "name-nul",
        "name-line-feed",
        "name-dot",
        "name-dot-dot",
        "name-256-bytes",
        "resource-name-dot-dot",
        "resource-not-object",
        "resource-missing",
        "resource-no-content",
        "resource-blob-and-text",
        "resource-text-number",
        "blob-not-base64",
        "data-bad-padding",
        "data-null",
        "mime-empty",
        "mime-missing",
    ],
)
async def test_a_failed_check_is_invalid_params(
    door: Door, blocks: list[dict[str, Any]], rule: str
) -> None:
    """Design note attachments §6 rule 2: the message names the rule. The
    prompt reaches no scan and no stream."""
    message = await refusal(door, [text("see"), *blocks])

    assert message.startswith(f"{rule}: ")
    assert door.scanner.requests == []


@pytest.mark.slow
async def test_a_name_of_255_bytes_passes(door: Door) -> None:
    name = "x" * 251 + ".png"

    async with door.connect() as connection:
        session = await new_session(connection)
        await send_prompt(connection, session, [image(name)])
        answer = await answer_of(connection, PROMPT_ID)

    assert answer["result"] == {"stopReason": "end_turn"}
    assert door.scanner.requests[0]["name"] == name


@pytest.mark.slow
@pytest.mark.parametrize("reason", list(Reason))
async def test_a_refusal_names_the_file_and_the_reason(door: Door, reason: Reason) -> None:
    """Design note attachments §6 rule 3."""
    door.scanner.mode, door.scanner.reason = ScanMode.REFUSE, reason.value

    assert await refusal(door, [image("a.png")]) == f"a.png: {reason}"


@pytest.mark.slow
async def test_the_first_refusal_ends_the_scan(door: Door) -> None:
    """Each file in sequence: the scan of the first refused file is the last."""
    door.scanner.mode, door.scanner.reason = ScanMode.REFUSE, Reason.MALWARE.value

    assert await refusal(door, [image("a.png"), image("b.png")]) == "a.png: malware"
    assert [one["name"] for one in door.scanner.requests] == ["a.png"]


@pytest.mark.slow
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        (ScanMode.CLOSE, Reason.BUSY),
        (ScanMode.SILENT, Reason.FAILED),
        (ScanMode.RAW, Reason.FAILED),
    ],
    ids=["link-cap", "no-reply", "bad-reply"],
)
async def test_a_link_that_gives_no_result_is_refused(
    door: Door, mode: ScanMode, reason: Reason
) -> None:
    """A link closed at once is the link cap of the vmm. Each other link
    with no good reply failed."""
    door.scanner.mode = mode
    large = blob("big.txt", b"a" * (4 * MIB), "text/plain")

    assert await refusal(door, [large]) == f"big.txt: {reason}"


def _reply_of(size: int, name: str = "big.png") -> bytes:
    """An `ok` reply of one rebuilt image of `size` bytes."""
    made = {"name": name, "mime_type": "image/png", "data": b64(b"a" * size)}
    return json.dumps({"ok": {"files": [made]}}).encode() + b"\n"


@pytest.mark.slow
async def test_a_rebuilt_file_over_max_file_bytes_is_too_large(door: Door) -> None:
    """The caps hold for the rebuilt files too. The text names the file
    that the client sent."""
    door.scanner.mode, door.scanner.raw = ScanMode.RAW, _reply_of(MAX_FILE_BYTES + 1)

    assert await refusal(door, [image("a.png")]) == "a.png: too_large"


@pytest.mark.slow
async def test_rebuilt_files_over_max_message_bytes_are_too_large(door: Door) -> None:
    """Each rebuilt file fits, and the second one takes the message over."""
    door.scanner.mode, door.scanner.raw = ScanMode.RAW, _reply_of(6 * MIB)

    assert await refusal(door, [image("a.png"), image("b.png")]) == "b.png: too_large"
    assert [one["name"] for one in door.scanner.requests] == ["a.png", "b.png"]


@pytest.mark.slow
async def test_a_pdf_and_its_text_count_as_one_file(door: Door) -> None:
    """Four PDFs give eight rebuilt files, and they pass `max_files`."""
    blocks = [blob(f"{one}.pdf", PDF_BYTES, "application/pdf") for one in range(4)]

    async with door.connect() as connection:
        session = await new_session(connection)
        await send_prompt(connection, session, blocks)
        answer = await answer_of(connection, PROMPT_ID)

    assert answer["result"] == {"stopReason": "end_turn"}
    assert len(prompt_sent(door, session)["params"]["prompt"]) == 8


@pytest.mark.slow
async def test_rebuilt_files_over_the_line_cap_are_refused(door: Door) -> None:
    """The rebuilt prompt must fit the ACP line cap of the facade."""
    door.scanner.mode, door.scanner.raw = ScanMode.RAW, _reply_of(15 * MIB // 2)

    message = await refusal(door, [text("a" * (6 * MIB)), image("a.png")])

    assert message.startswith("acp_line_cap: ")


def test_the_door_answers_the_prompt_capabilities_of_the_facade() -> None:
    """`initialize` of the door copies the facade's `promptCapabilities`,
    and the line cap is the facade's."""
    vectors = json.loads(FACADE_VECTORS.read_text(encoding="utf-8"))
    facade = vectors["initialize"]["response"]["result"]["agentCapabilities"]

    door_capabilities = INITIALIZE_RESPONSE["agentCapabilities"]
    assert door_capabilities["promptCapabilities"] == facade["promptCapabilities"]
    assert vectors["max_line_bytes"] == MAX_FRAME_BYTES


@pytest.mark.slow
async def test_a_cancel_in_the_scan_answers_cancelled(door: Door) -> None:
    """Design note attachments §6 rule 5: the door answers the prompt, and
    the prompt reaches no stream."""
    door.scanner.gate = asyncio.Event()

    async with door.connect() as connection:
        session = await new_session(connection)
        await send_prompt(connection, session, [image("a.png")])
        await wait_until(lambda: door.scanner.waiting == 1)
        assert door.service.door_prompts.in_flight == 1

        cancel = {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}}
        await send(connection, cancel)
        answer = await answer_of(connection, PROMPT_ID)
        door.scanner.gate.set()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(connection.recv(), QUIET_S)

    assert answer == {"jsonrpc": "2.0", "id": PROMPT_ID, "result": {"stopReason": "cancelled"}}
    assert door.vmm.opened == []
    assert door.service.door_prompts.in_flight == 0


@pytest.mark.slow
async def test_other_frames_flow_while_a_scan_waits(door: Door) -> None:
    """Design note attachments §6: the scan runs as a task. A list, a new
    session and a prompt of another session pass it."""
    door.scanner.gate = asyncio.Event()

    async with door.connect() as connection:
        first = await new_session(connection)
        second = await new_session(connection)
        await send_prompt(connection, first, [image("a.png")])
        await wait_until(lambda: door.scanner.waiting == 1)

        listing: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": "list",
            "method": "session/list",
            "params": {},
        }
        await send(connection, listing)
        assert "result" in await answer_of(connection, "list")

        params = {"sessionId": second, "prompt": [text("hi")]}
        other = {"jsonrpc": "2.0", "id": "other", "method": "session/prompt", "params": params}
        await send(connection, other)
        assert (await answer_of(connection, "other"))["result"] == {"stopReason": "end_turn"}
        assert door.vmm.opened == [second]

        door.scanner.gate.set()
        answer = await answer_of(connection, PROMPT_ID)

    assert answer["result"] == {"stopReason": "end_turn"}
    assert prompt_sent(door, first)["params"]["prompt"] == [image("a.png")]


@pytest.mark.slow
async def test_a_prompt_past_the_scans_of_a_connection_is_busy(door: Door) -> None:
    """Each scan holds a frame and its files. A connection holds
    `MAX_SCANS` of them at once."""
    door.scanner.gate = asyncio.Event()

    async with door.connect() as connection:
        sessions = [await new_session(connection) for _ in range(MAX_SCANS + 1)]

        for spot, session in enumerate(sessions[:MAX_SCANS]):
            params = {"sessionId": session, "prompt": [image(f"{spot}.png")]}
            message = {"jsonrpc": "2.0", "id": spot, "method": "session/prompt", "params": params}
            await send(connection, message)

        await wait_until(lambda: door.scanner.waiting == MAX_SCANS)
        await send_prompt(connection, sessions[-1], [image("last.png")])
        answer = await answer_of(connection, PROMPT_ID)
        door.scanner.gate.set()
        answers = await answers_of(connection, set(range(MAX_SCANS)))

    assert answer["error"] == {"code": RPC_INVALID_PARAMS, "message": "last.png: busy"}
    assert [one["result"] for one in answers.values()] == [{"stopReason": "end_turn"}] * MAX_SCANS


@pytest.mark.slow
async def test_the_lanes_of_a_connection_hold_a_byte_cap(door: Door) -> None:
    """A frame can be 16 MiB, so the lanes of one connection hold
    `QUEUED_FRAME_BYTES`, the frames in their routes included. Past it the
    read of the next frame waits, also for a frame of another lane."""
    async with door.connect() as connection:
        session = await new_session(connection)
        gate = door.vmm.open_gates[session] = asyncio.Event()
        third = QUEUED_FRAME_BYTES // 3 + 1

        for spot in range(3):
            params = {"sessionId": session, "prompt": [text("a" * third)]}
            message = {"jsonrpc": "2.0", "id": spot, "method": "session/prompt", "params": params}
            await send(connection, message)

        await wait_until(lambda: door.vmm.opened == [session])
        listing: dict[str, Any] = {"jsonrpc": "2.0", "id": "list", "method": "session/list"}
        await send(connection, listing)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(connection.recv(), QUIET_S)

        gate.set()
        ends = await answers_of(connection, {"list", 0, 1, 2})

    assert "result" in ends.pop("list")
    assert [one["result"] for one in ends.values()] == [{"stopReason": "end_turn"}] * 3
