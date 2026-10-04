"""Framing, parsing, the handshake, the ping deadline and the lock file."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from attendance.atomic import read_json, write_json
from attendance.channel import FakeChannel, SandboxDial
from attendance.clock import now, rfc3339_ms
from attendance.errors import TurnReason
from attendance.exec_channel import DEFAULT_COMMAND, ExecChannel, build_argv
from attendance.faults import FaultCode, FaultReporter
from attendance.paths import playpen_lock_file
from attendance.playpen_link import (
    HandshakeError,
    OrphanPlaypen,
    PlaypenFatal,
    PlaypenLink,
    Violation,
)
from attendance.states import SessionKind
from attendance.wire import (
    CHANNEL_IDLE_TTL_OTHER_S,
    MAX_EVENT_BYTES,
    MAX_EVENT_DEPTH,
    MAX_LINE_BYTES,
    REFUSAL_BUDGET,
    EventLine,
    FailedLine,
    LineSplitter,
    LogLine,
    OpenedLine,
    PlaypenReason,
    PongLine,
    Ready,
    Refusal,
    SettledLine,
    cap_event,
    encode,
    host_reason,
    parse,
    read_usage,
)
from attendance_harness import (
    FAMILY,
    PLAYPEN_ENV,
    SANDBOX,
    FakePlaypen,
    PlaypenPlan,
    nested_event,
)

SESSION = "owui-3f2a9c41"
TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
ENV_FILE = PLAYPEN_ENV

# Contract 03 §10 rule 3's 120 seconds, shrunk so a test watches it pass.
IDLE_TTL_S = 0.05
# Long enough for several idle windows to pass. A channel still open after
# it never closed for idleness.
IDLE_WATCH_S = 0.3


def dial(sandbox: str = SANDBOX) -> SandboxDial:
    """What `attendance` hands a channel factory (contract 03 §7.1)."""
    return SandboxDial(sandbox=sandbox, env_file=ENV_FILE)


# U+2028 and U+2029 are legal inside a JSON string. A generic line reader
# splits on them, which would tear one record into two (contract 03 §2 rule 4).
UNICODE_SEPARATORS = f"line{chr(0x2028)}sep{chr(0x2029)}arator"

# Three lines under the line cap that are not a `JSONDecodeError`. Each one
# raised a different exception type at a different place in `parse`: the JSON
# reader's recursion limit, the interpreter's integer digit limit, and the
# UTF-8 encode of a lone surrogate in `cap_event`.
RAISING_LINES = {
    "deep_nesting": "[" * 200_000,
    "long_integer": "1" * 5_000,
    "lone_surrogate": (
        '{"type":"event","session":"s","turn":"t","turn_seq":1,"event":{"text":"\\ud800"}}'
    ),
}

LOG_LINE = '{"type":"log","message":"pi started"}'


def test_the_splitter_uses_lf_and_nothing_else() -> None:
    splitter = LineSplitter()
    payload = json.dumps({"type": "log", "message": UNICODE_SEPARATORS})
    lines = splitter.feed(payload.encode("utf-8") + b"\n")

    assert len(lines) == 1
    assert lines[0].text == payload

    message = parse(payload)
    assert isinstance(message, LogLine)
    assert message.message == UNICODE_SEPARATORS


def test_the_splitter_strips_one_trailing_cr() -> None:
    lines = LineSplitter().feed(b'{"type":"pong","nonce":"9f13"}\r\n')

    assert lines[0].text == '{"type":"pong","nonce":"9f13"}'


def test_a_partial_record_waits_for_its_lf() -> None:
    splitter = LineSplitter()

    assert splitter.feed(b'{"type":"pon') == []
    assert splitter.pending_bytes() == 12

    lines = splitter.feed(b'g","nonce":"1"}\n')
    assert len(lines) == 1


def test_an_oversized_record_is_refused_and_the_stream_recovers() -> None:
    """Probe 0a, A8: one byte over was refused and the channel stayed usable."""
    splitter = LineSplitter()
    lines = splitter.feed(b"x" * (MAX_LINE_BYTES + 1) + b"\n")

    assert len(lines) == 1
    assert lines[0].refusal is Refusal.TOO_LARGE

    good = splitter.feed(b'{"type":"pong","nonce":"1"}\n')
    assert good[0].text is not None


def test_an_oversized_partial_record_is_refused_before_its_lf() -> None:
    """A sender with no LF must not grow this buffer without bound."""
    splitter = LineSplitter()
    lines = splitter.feed(b"x" * (MAX_LINE_BYTES + 1))

    assert lines[0].refusal is Refusal.TOO_LARGE
    assert splitter.pending_bytes() == 0

    splitter.feed(b'rest of the bad line\n{"type":"pong","nonce":"1"}\n')


def test_encode_refuses_an_oversized_outbound_line() -> None:
    with pytest.raises(ValueError, match="over the"):
        encode({"type": "log", "message": "x" * (MAX_LINE_BYTES + 1)})


def test_parse_refuses_what_contract_13_rule_1_names() -> None:
    assert parse("not json") is Refusal.NOT_JSON
    assert parse("[1,2]") is Refusal.NOT_OBJECT
    assert parse('{"type":"nope"}') is Refusal.UNKNOWN_TYPE
    assert parse("{}") is Refusal.UNKNOWN_TYPE
    assert parse('{"type":"event","session":"s"}') is Refusal.MALFORMED
    assert parse('{"type":"event","session":"s","turn":"t","turn_seq":0}') is Refusal.MALFORMED


@pytest.mark.parametrize("line", list(RAISING_LINES.values()), ids=list(RAISING_LINES))
def test_parse_refuses_a_line_that_made_it_raise(line: str) -> None:
    """Trust rule 1: `parse` returns a refusal, never raises."""
    assert len(line) < MAX_LINE_BYTES
    assert parse(line) is Refusal.MALFORMED


def test_parse_reads_each_playpen_line() -> None:
    ready = parse(json.dumps({"type": "ready", "protocol": "1.0", "sandbox": SANDBOX}))
    pong = parse('{"type":"pong","nonce":"9f13"}')
    event = parse(
        json.dumps(
            {
                "type": "event",
                "session": SESSION,
                "turn": TURN,
                "turn_seq": 1,
                "event": {"type": "message_update"},
            }
        )
    )
    settled = parse(
        json.dumps(
            {
                "type": "turn_settled",
                "session": SESSION,
                "turn": TURN,
                "turn_seq": 2,
                "resident": True,
                "leaf_id": "e5f6",
            }
        )
    )
    failed = parse(
        json.dumps(
            {
                "type": "turn_failed",
                "session": SESSION,
                "turn": TURN,
                "turn_seq": 3,
                "reason": "process_died",
            }
        )
    )

    assert isinstance(ready, Ready)
    assert ready.major == "1"
    assert isinstance(pong, PongLine)
    assert isinstance(event, EventLine)
    assert isinstance(settled, SettledLine)
    assert settled.leaf_id == "e5f6"
    assert isinstance(failed, FailedLine)
    assert failed.reason is PlaypenReason.PROCESS_DIED


def test_an_unknown_failure_reason_reads_as_internal() -> None:
    """An unknown value is a claim, not a crash (contract 03 §13)."""
    failed = parse(
        json.dumps(
            {
                "type": "turn_failed",
                "session": SESSION,
                "turn": TURN,
                "turn_seq": 1,
                "reason": "invented",
            }
        )
    )

    assert isinstance(failed, FailedLine)
    assert failed.reason is PlaypenReason.INTERNAL


def test_an_oversized_event_keeps_only_its_type() -> None:
    """Contract 03 §13 rule 6."""
    big = cap_event({"type": "message_update", "blob": "x" * (MAX_EVENT_BYTES + 10)})

    assert big["type"] == "message_update"
    assert big["truncated"] is True
    assert big["original_bytes"] > MAX_EVENT_BYTES
    assert "blob" not in big


def test_an_event_nested_past_the_cap_keeps_only_its_type() -> None:
    """The host adds levels of its own, and a reader has a nesting limit."""
    deep = cap_event(nested_event(MAX_EVENT_DEPTH + 1))

    assert deep["type"] == "message_update"
    assert deep["truncated"] is True
    assert deep["original_bytes"] < MAX_EVENT_BYTES
    assert "a" not in deep


def test_an_event_nested_to_the_cap_is_kept_whole() -> None:
    event = nested_event(MAX_EVENT_DEPTH)

    assert cap_event(event) is event


def test_usage_from_the_playpen_is_read_defensively() -> None:
    """Contract 03 §13 rule 7: LiteLLM is the authority, this is advisory."""
    usage = read_usage({"input": 10, "output": -1, "cost_usd": "free", "cache_read": True})

    assert usage.input == 10
    assert usage.output == 0
    assert usage.cost_usd == 0.0
    assert usage.cache_read == 0


def test_every_playpen_reason_maps_to_a_turn_reason() -> None:
    """Contract 03 §5.3. The host never forwards a reason verbatim."""
    mapped = {reason: host_reason(reason) for reason in PlaypenReason}

    assert mapped[PlaypenReason.NO_RESIDENT_PROCESS] is None
    assert mapped[PlaypenReason.FORK_REFUSED] is None
    assert mapped[PlaypenReason.PROCESS_DIED] is TurnReason.SANDBOX_LOST
    assert mapped[PlaypenReason.DEADLINE_EXCEEDED] is TurnReason.TURN_TIMEOUT
    assert mapped[PlaypenReason.LINE_TOO_LARGE] is TurnReason.PROTOCOL_VIOLATION


def test_the_default_command_carries_the_env_file_and_the_sandbox() -> None:
    """Contract 03 §7.1. `sbx exec` forwards no host environment, so a
    command without `--env-file` starts a playpen that finds none of its
    mounts; one without `--sandbox` makes it exit 2 before it opens
    anything (its own Dockerfile asserts that exit)."""
    argv = build_argv(DEFAULT_COMMAND, dial(SANDBOX))

    assert argv[:2] == ["sbx", "exec"]
    assert argv[argv.index("--env-file") + 1] == ENV_FILE
    assert argv[argv.index("--sandbox") + 1] == SANDBOX
    assert argv.count(SANDBOX) == 2


def test_the_sandbox_id_cannot_inject_a_second_word() -> None:
    argv = build_argv(DEFAULT_COMMAND, dial("chat-s3; rm -rf /"))

    assert argv[:2] == ["sbx", "exec"]
    assert "chat-s3; rm -rf /" in argv
    assert "rm" not in argv


def test_the_env_file_path_cannot_inject_a_second_word() -> None:
    """The path comes out of a status document another process writes, so
    it is untrusted the same way the sandbox id is (invariant 12)."""
    argv = build_argv(DEFAULT_COMMAND, SandboxDial(SANDBOX, "/tmp/e.env --privileged"))

    assert "/tmp/e.env --privileged" in argv
    assert "--privileged" not in argv


class Recorder:
    """A LinkEvents that only remembers. The service is tested elsewhere."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.settled: list[SettledLine] = []
        self.failed: list[FailedLine] = []
        self.violations: list[Violation] = []
        self.logs: list[LogLine] = []
        self.lost: list[str] = []
        self.exits: list[str] = []
        self.opened: list[OpenedLine] = []

    async def on_event(self, session: str, turn: str, event: dict[str, Any]) -> None:
        self.events.append((session, turn, event))

    async def on_settled(self, message: SettledLine) -> None:
        self.settled.append(message)

    async def on_failed(self, message: FailedLine) -> None:
        self.failed.append(message)

    async def on_violation(self, session: str, turn: str, why: Violation) -> None:
        self.violations.append(why)

    async def on_process_exit(self, message: object) -> None:
        self.exits.append(str(message))

    async def on_log(self, message: LogLine) -> None:
        self.logs.append(message)

    async def on_session_opened(self, message: OpenedLine) -> None:
        self.opened.append(message)

    async def on_channel_lost(self, sandbox: str) -> None:
        self.lost.append(sandbox)


class FailingRecorder(Recorder):
    """A LinkEvents whose log callback raises.

    It stands for any fault that ends the reader loop with an exception
    instead of a return.
    """

    async def on_log(self, message: LogLine) -> None:
        await super().on_log(message)
        raise RuntimeError("the log callback failed")


def make_link(
    tmp_path: Path,
    events: Recorder,
    plan: PlaypenPlan | None = None,
    ping_interval_s: float = 30.0,
    host_deadline_s: float = 90.0,
    lock_stale_s: float = 20.0,
    lock_poll_s: float = 1.0,
    kind: SessionKind = SessionKind.ATTENDED,
    idle_ttl_s: float = CHANNEL_IDLE_TTL_OTHER_S,
    exit_wait_s: float = 2.0,
    queued: Callable[[], int] = lambda: 0,
) -> tuple[PlaypenLink, dict[str, FakePlaypen]]:
    playpens: dict[str, FakePlaypen] = {}

    def factory(target: SandboxDial) -> FakeChannel:
        channel = FakeChannel(target.sandbox)
        playpen = FakePlaypen(channel, plan if plan is not None else PlaypenPlan())
        playpens[target.sandbox] = playpen
        playpen.serve()
        return channel

    link = PlaypenLink(
        family=FAMILY,
        kind=kind,
        events=events,
        factory=factory,  # pyright: ignore[reportArgumentType]
        faults=FaultReporter(tmp_path),
        state_root=tmp_path,
        ping_interval_s=ping_interval_s,
        host_deadline_s=host_deadline_s,
        lock_stale_s=lock_stale_s,
        lock_poll_s=lock_poll_s,
        idle_ttl_s=idle_ttl_s,
        exit_wait_s=exit_wait_s,
        queued=queued,
    )
    return link, playpens


def idle_link(
    tmp_path: Path,
    events: Recorder,
    plan: PlaypenPlan | None = None,
    kind: SessionKind = SessionKind.AUTONOMOUS,
    queued: Callable[[], int] = lambda: 0,
) -> tuple[PlaypenLink, dict[str, FakePlaypen]]:
    """A link whose idle window passes in a fraction of a second."""
    return make_link(
        tmp_path,
        events,
        plan,
        kind=kind,
        idle_ttl_s=IDLE_TTL_S,
        exit_wait_s=IDLE_TTL_S,
        queued=queued,
    )


async def test_the_handshake_refuses_a_wrong_sandbox_id(tmp_path: Path) -> None:
    """A stale exec left over from a switch names another sandbox (§3 rule 3)."""
    events = Recorder()
    link, _ = make_link(tmp_path, events, PlaypenPlan(sandbox="chat-s9"))

    with pytest.raises(HandshakeError):
        await link.ensure_open(dial(), 7)

    await link.close()


async def test_a_lost_channel_reports_once(tmp_path: Path) -> None:
    events = Recorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    await playpens[SANDBOX].drop()
    await asyncio.wait_for(_until(lambda: bool(events.lost)), 2.0)

    assert events.lost == [SANDBOX]
    assert link.is_open is False
    await link.close()


async def test_two_missed_pongs_drop_the_channel(tmp_path: Path) -> None:
    """Contract 03 §10 rule 4."""
    events = Recorder()
    link, _ = make_link(tmp_path, events, PlaypenPlan(answer_ping=False), ping_interval_s=0.02)
    await link.ensure_open(dial(), 7)
    await asyncio.wait_for(_until(lambda: bool(events.lost)), 2.0)

    assert events.lost == [SANDBOX]
    await link.close()


async def test_a_pong_keeps_the_channel_open(tmp_path: Path) -> None:
    events = Recorder()
    link, playpens = make_link(tmp_path, events, ping_interval_s=0.02)
    await link.ensure_open(dial(), 7)
    await asyncio.wait_for(_until(lambda: playpens[SANDBOX].pings >= 3), 2.0)

    assert events.lost == []
    assert link.is_open is True
    await link.close()


async def test_an_absent_lock_file_dials_at_once(tmp_path: Path) -> None:
    """§11.4 rule 4, step 1: the last playpen removed it as it exited."""
    events = Recorder()
    link, _ = make_link(tmp_path, events, lock_stale_s=10.0)

    started = asyncio.get_running_loop().time()
    await link.ensure_open(dial(), 7)

    assert asyncio.get_running_loop().time() - started < 1.0
    assert link.is_open is True
    await link.close()


async def test_another_sandboxs_live_lock_never_delays(tmp_path: Path) -> None:
    """Contract 03 §7.1: the control directory is per sandbox.

    A drain keeps the outgoing playpen alive and beating ITS lock while
    the switch asks for the incoming sandbox's handshake. A family-wide
    lock made that handshake wait the outgoing playpen out, so a drain
    switch could never complete.
    """
    events = Recorder()
    beating = asyncio.create_task(_beat_lock(tmp_path, every_s=0.02))
    link, _ = make_link(tmp_path, events, lock_stale_s=10.0, lock_poll_s=0.02)

    started = asyncio.get_running_loop().time()
    await link.ensure_open(dial("chat-s2"), 7)
    waited = asyncio.get_running_loop().time() - started

    beating.cancel()

    assert waited < 1.0
    assert link.is_open is True
    await link.close()


async def test_a_lock_whose_beat_stopped_is_removed(tmp_path: Path) -> None:
    """§11.4 rule 4, step 2: a counter that stopped means nobody is writing."""
    events = Recorder()
    path = playpen_lock_file(tmp_path, FAMILY, SANDBOX)
    _write_lock(tmp_path, beat=41)
    link, _ = make_link(tmp_path, events, lock_stale_s=0.2, lock_poll_s=0.02)

    started = asyncio.get_running_loop().time()
    await link.ensure_open(dial(), 7)
    waited = asyncio.get_running_loop().time() - started

    assert waited >= 0.2
    assert link.is_open is True
    assert path.exists() is False
    await link.close()


async def test_a_beating_lock_keeps_the_host_waiting(tmp_path: Path) -> None:
    """§11.4 rule 4, step 4: a counter that moves is a live playpen."""
    events = Recorder()
    link, _ = make_link(tmp_path, events, host_deadline_s=0.3, lock_stale_s=0.2, lock_poll_s=0.01)

    # The first beat lands before the dial, so the reader never sees the file
    # absent and dial at once.
    _write_lock(tmp_path, beat=1)
    beating = asyncio.create_task(_beat_lock(tmp_path, every_s=0.02, first=2))

    try:
        with pytest.raises(OrphanPlaypen):
            await link.ensure_open(dial(), 7)
    finally:
        beating.cancel()

    faults = json.loads((tmp_path / "faults" / "sessiond" / f"{FAMILY}.json").read_text())
    assert faults["faults"][0]["code"] == FaultCode.ORPHAN_PROCESSES.value
    await link.close()


async def test_one_unreadable_read_never_unlocks_the_family(tmp_path: Path) -> None:
    """§11.4 rule 4, step 3: a half-written file decides nothing on its own."""
    events = Recorder()
    path = playpen_lock_file(tmp_path, FAMILY, SANDBOX)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{half writ", encoding="utf-8")
    link, _ = make_link(tmp_path, events, lock_stale_s=0.2, lock_poll_s=0.02)

    started = asyncio.get_running_loop().time()
    await link.ensure_open(dial(), 7)
    waited = asyncio.get_running_loop().time() - started

    # A file that never reads cleanly for a whole window is not one a live
    # playpen is writing, so the wait is `lock_stale_s` and not 90 seconds.
    assert waited >= 0.2
    assert link.is_open is True
    await link.close()


@pytest.mark.parametrize(
    "content",
    ["[" * 200_000, '{"beat":' + "1" * 5_000 + "}"],
    ids=["deep_nesting", "long_integer"],
)
def test_read_json_reads_content_that_made_it_raise_as_nothing(
    tmp_path: Path, content: str
) -> None:
    """The lock file comes from inside the sandbox, and this is its reader."""
    path = tmp_path / "supervisor.lock"
    path.write_text(content, encoding="utf-8")

    assert read_json(path) is None


async def test_a_fatal_in_place_of_ready_raises_a_fault(tmp_path: Path) -> None:
    """Contract 03 §5.7 rule 3: the mount is read before the first line."""
    events = Recorder()
    link, _ = make_link(tmp_path, events, plan=PlaypenPlan(fatal="control_mount_unwritable"))

    with pytest.raises(PlaypenFatal):
        await link.ensure_open(dial(), 7)

    faults = json.loads((tmp_path / "faults" / "sessiond" / f"{FAMILY}.json").read_text())

    assert faults["faults"][0]["code"] == FaultCode.SANDBOX_START_FAILED.value
    assert faults["faults"][0]["blocks_turns"] is True
    assert "control_mount_unwritable" in faults["faults"][0]["message"]
    await link.close()


async def test_a_fatal_on_a_live_channel_drops_it(tmp_path: Path) -> None:
    """§5.7 rule 2: a last line, never a state."""
    events = Recorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    await playpens[SANDBOX].send_fatal("control_mount_unwritable")
    await asyncio.wait_for(_until(lambda: bool(events.lost)), 2.0)

    assert link.is_open is False
    faults = json.loads((tmp_path / "faults" / "sessiond" / f"{FAMILY}.json").read_text())
    assert faults["faults"][0]["code"] == FaultCode.SANDBOX_START_FAILED.value
    await link.close()


async def test_session_opened_never_spends_the_refusal_budget(tmp_path: Path) -> None:
    """Contract 03 §5.6. Ten pre-starts must not close a family's channel."""
    events = Recorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)

    for index in range(REFUSAL_BUDGET + 2):
        await playpens[SANDBOX].open_session(f"{SESSION}-{index}")

    await asyncio.wait_for(_until(lambda: len(events.opened) == REFUSAL_BUDGET + 2), 2.0)

    assert link.is_open is True
    assert events.lost == []
    assert events.opened[0].resident is True
    await link.close()


@pytest.mark.parametrize("line", list(RAISING_LINES.values()), ids=list(RAISING_LINES))
async def test_a_line_that_made_parse_raise_is_one_refusal(tmp_path: Path, line: str) -> None:
    """Contract 03 §13 rule 2. The line spends one refusal and the reader reads on."""
    events = Recorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    await playpens[SANDBOX].send_raw(line)

    for _ in range(REFUSAL_BUDGET - 2):
        await playpens[SANDBOX].send_malformed()

    await playpens[SANDBOX].open_session(SESSION)
    await asyncio.wait_for(_until(lambda: bool(events.opened)), 2.0)

    assert link.is_open is True
    assert events.lost == []

    await playpens[SANDBOX].send_malformed()
    await asyncio.wait_for(_until(lambda: bool(events.lost)), 2.0)

    assert link.is_open is False
    await link.close()


async def test_close_drops_the_channel_after_a_loop_died(tmp_path: Path) -> None:
    """A dead loop keeps its exception. `close` must close the channel anyway."""
    events = FailingRecorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    await playpens[SANDBOX].send_raw(LOG_LINE)
    await asyncio.wait_for(_until(lambda: bool(events.logs)), 2.0)

    await link.close()

    assert link.is_open is False


async def test_a_loop_that_dies_says_so_at_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log names the fault when the loop ends, not at the next `close`."""
    events = FailingRecorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        await playpens[SANDBOX].send_raw(LOG_LINE)
        await asyncio.wait_for(_until(lambda: "the log callback failed" in caplog.text), 2.0)

    assert link.is_open is True
    assert f"task reader {FAMILY} ended with an error" in caplog.text
    await link.close()


async def test_a_dead_reader_still_ends_in_channel_lost(tmp_path: Path) -> None:
    """Contract 03 §10 rule 4. With no reader no pong arrives, so the ping
    loop drops the channel. The dead reader must not stop that."""
    events = FailingRecorder()
    link, playpens = make_link(tmp_path, events, ping_interval_s=0.02)
    await link.ensure_open(dial(), 7)
    await playpens[SANDBOX].send_raw(LOG_LINE)
    await asyncio.wait_for(_until(lambda: bool(events.lost)), 2.0)

    assert events.lost == [SANDBOX]
    assert link.is_open is False
    await link.close()


async def test_a_line_for_an_unknown_turn_is_dropped(tmp_path: Path) -> None:
    """Contract 03 §13 rule 3: addressing must name a turn in flight."""
    events = Recorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    await playpens[SANDBOX].emit_text(SESSION, TURN, "nobody asked")
    await asyncio.sleep(0.05)

    assert events.events == []
    await link.close()


async def test_a_registered_turn_receives_its_events(tmp_path: Path) -> None:
    events = Recorder()
    link, playpens = make_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    link.register(SESSION, TURN)
    await playpens[SANDBOX].emit_text(SESSION, TURN, "Sensor ")
    await asyncio.wait_for(_until(lambda: bool(events.events)), 2.0)

    assert events.events[0][0] == SESSION
    await link.close()


@pytest.mark.parametrize("kind", [SessionKind.THIN, SessionKind.AUTONOMOUS])
async def test_an_idle_channel_closes(tmp_path: Path, kind: SessionKind) -> None:
    """Contract 03 §10 rule 3. The VM stops once nothing holds it up."""
    events = Recorder()
    link, playpens = idle_link(tmp_path, events, kind=kind)
    await link.ensure_open(dial(), 7)
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)

    # `shutdown` first: a killed exec does not reach the in-VM playpen,
    # which would keep beating its lock and hold the VM up (probe 0a, A5).
    assert playpens[SANDBOX].shutdowns == 1
    # Closing for idleness is not a loss. No turn was there to fail.
    assert events.lost == []
    await link.close()


async def test_an_attended_channel_never_closes_for_idleness(tmp_path: Path) -> None:
    """Contract 03 §10 rule 2. `channel_idle_ttl_s` 0 means never close."""
    events = Recorder()
    link, playpens = idle_link(tmp_path, events, kind=SessionKind.ATTENDED)
    await link.ensure_open(dial(), 7)
    await asyncio.sleep(IDLE_WATCH_S)

    assert link.is_open is True
    assert playpens[SANDBOX].shutdowns == 0
    await link.close()


async def test_a_running_turn_holds_the_channel(tmp_path: Path) -> None:
    events = Recorder()
    link, _ = idle_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    link.register(SESSION, TURN)
    await asyncio.sleep(IDLE_WATCH_S)

    assert link.is_open is True

    link.unregister(SESSION, TURN)
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)
    await link.close()


async def test_a_queued_turn_holds_the_channel(tmp_path: Path) -> None:
    """A queued turn is about to start, so closing now costs it a cold dial."""
    events = Recorder()
    depth = [1]
    link, _ = idle_link(tmp_path, events, queued=lambda: depth[0])
    await link.ensure_open(dial(), 7)
    await asyncio.sleep(IDLE_WATCH_S)

    assert link.is_open is True

    depth[0] = 0
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)
    await link.close()


async def test_a_resident_process_holds_the_channel(tmp_path: Path) -> None:
    """Contract 03 §10 rule 3: no resident process, and §5.4 ends one."""
    events = Recorder()
    link, playpens = idle_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    link.note_opened(SESSION)
    await playpens[SANDBOX].open_session(SESSION)
    await asyncio.wait_for(_until(lambda: bool(events.opened)), 2.0)
    await asyncio.sleep(IDLE_WATCH_S)

    assert link.is_open is True

    await playpens[SANDBOX].reap(SESSION)
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)
    await link.close()


async def test_a_settled_turn_that_holds_nothing_frees_the_channel(tmp_path: Path) -> None:
    """§5.2's `resident` false: `pi_idle_ttl_s` 0 keeps no process (§6 rule 4)."""
    events = Recorder()
    link, playpens = idle_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    link.note_opened(SESSION)
    await playpens[SANDBOX].open_session(SESSION)
    link.register(SESSION, TURN)
    await playpens[SANDBOX].settle(SESSION, TURN)
    await asyncio.wait_for(_until(lambda: bool(events.settled)), 2.0)
    link.unregister(SESSION, TURN)

    assert events.settled[0].resident is False
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)
    await link.close()


async def test_a_residency_claim_for_an_unopened_session_holds_nothing(tmp_path: Path) -> None:
    """Every byte from the playpen is a claim (§13). Naming sessions this
    host never opened must not keep the VM up."""
    events = Recorder()
    link, playpens = idle_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    await playpens[SANDBOX].open_session(SESSION)
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)
    await link.close()


async def test_a_playpen_that_never_exits_is_closed_anyway(tmp_path: Path) -> None:
    """The wait for the exit is bounded, so the next dial is not held."""
    events = Recorder()
    link, playpens = idle_link(tmp_path, events, PlaypenPlan(exits_on_shutdown=False))
    await link.ensure_open(dial(), 7)
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)

    assert playpens[SANDBOX].shutdowns == 1
    await link.close()


async def test_the_next_dial_after_an_idle_close_opens_a_new_channel(tmp_path: Path) -> None:
    """Contract 03 §10 rule 5. The next turn pays the cold start."""
    events = Recorder()
    link, playpens = idle_link(tmp_path, events)
    await link.ensure_open(dial(), 7)
    first = playpens[SANDBOX]
    await asyncio.wait_for(_until(lambda: not link.is_open), 2.0)
    await link.ensure_open(dial(), 7)

    assert link.is_open is True
    assert playpens[SANDBOX] is not first
    await link.close()


@pytest.mark.slow
async def test_the_exec_channel_round_trips_through_a_real_process() -> None:
    """`cat` echoes stdin to stdout, so the framing crosses a real pipe."""
    channel = ExecChannel(dial=dial(), command="cat")
    await channel.start()
    await channel.send(encode({"type": "pong", "nonce": "9f13"}))
    line = await asyncio.wait_for(channel.receive(), 5.0)

    assert line is not None
    assert line.text is not None

    message = parse(line.text)
    assert isinstance(message, PongLine)
    await channel.close()


#: A child that says its pid and then waits on stdin, as the playpen does.
PID_THEN_WAIT = "sh -c 'echo $$; exec cat'"


@pytest.mark.slow
def test_a_closed_exec_channel_leaves_no_child_and_no_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`close` returns once the child is reaped and its transport is closed.

    A transport that outlives its loop fails later, in the collector, on
    whichever test is running at that moment: "Exception ignored in
    BaseSubprocessTransport.__del__ ... Event loop is closed", or
    "ResourceWarning: unclosed transport". Where a thread waits for the
    child, the collector cannot reach the transport yet, so the child's pid
    is checked too.
    """
    ignored: list[object] = []
    monkeypatch.setattr(sys, "unraisablehook", ignored.append)

    async def open_and_close() -> None:
        channel = ExecChannel(dial=dial(), command=PID_THEN_WAIT)
        await channel.start()
        line = await asyncio.wait_for(channel.receive(), 5.0)
        assert line is not None
        assert line.text is not None

        await channel.close()

        # A reaped child has no pid. A zombie still has one.
        with pytest.raises(ProcessLookupError):
            os.kill(int(line.text), 0)

    # What earlier tests left behind is collected first, outside the watch.
    gc.collect()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)

        # Its own loop, closed the moment `close` returns: the collector then
        # runs with the loop gone. `asyncio.run` would turn the loop a few
        # more times first and hide the leak.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(open_and_close())
        finally:
            loop.close()

        gc.collect()

    leaks = [str(one.message) for one in caught if issubclass(one.category, ResourceWarning)]

    assert not leaks
    assert not ignored


def _write_lock(state_root: Path, beat: int, host_deadline_s: float = 90.0) -> None:
    """The lock file as the playpen writes it (contract 03 §11.1 rule 3).

    `started_at` is here because the file carries it. Nothing on this side
    reads it: the two clocks are not the same clock (§11.4 rule 4).
    """
    write_json(
        playpen_lock_file(state_root, FAMILY, SANDBOX),
        {
            "pid": 412,
            "sandbox": SANDBOX,
            "started_at": rfc3339_ms(now()),
            "host_deadline_s": host_deadline_s,
            "lock_beat_s": 5,
            "beat": beat,
        },
    )


async def _beat_lock(state_root: Path, every_s: float, first: int = 1) -> None:
    """A live playpen, rewriting its lock with a counter one higher."""
    beat = first

    while True:
        _write_lock(state_root, beat=beat)
        beat += 1
        await asyncio.sleep(every_s)


async def _until(test: Callable[[], bool]) -> None:
    while not test():
        await asyncio.sleep(0.005)
