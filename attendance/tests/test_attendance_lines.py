"""Framing, the byte cap of a text, `sbx exec` and the supervisor lock.

What every link to a sandbox shares: the LF framing of contract 03 §2, the
cut of a text by bytes of contract 03 §8, the child process of `sbx exec`,
and the lock file of contract 03 §11.4.
"""

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

import pytest
from attendance.atomic import read_json, write_json
from attendance.channel import SandboxDial
from attendance.clock import now, rfc3339_ms
from attendance.config import DEFAULT_FACADE_COMMAND
from attendance.exec_channel import ExecChannel, build_argv
from attendance.lines import MAX_LINE_BYTES, LineRefusal, LineSplitter, cut_bytes
from attendance.paths import playpen_lock_file
from attendance.supervisor_lock import wait_out_lock
from attendance_harness import FAMILY, PLAYPEN_ENV, SANDBOX

ENV_FILE = PLAYPEN_ENV

# U+2028 and U+2029 are legal inside a JSON string. A generic line reader
# splits on them, which would tear one record into two (contract 03 §2 rule 4).
UNICODE_SEPARATORS = f"line{chr(0x2028)}sep{chr(0x2029)}arator"

# A cap of a few bytes, so a test passes it with a short record.
SMALL_CAP = 16

# One character for each count of bytes that UTF-8 has past one byte.
TWO_BYTES = "é"
THREE_BYTES = "€"
FOUR_BYTES = "\U0001f600"

# One half of a surrogate pair. It has no UTF-8 form.
HALF_PAIR = "\ud83d"
HALF_PAIR_BYTES = 3


def dial(sandbox: str = SANDBOX) -> SandboxDial:
    """What `attendance` hands a channel factory (contract 03 §7.1)."""
    return SandboxDial(sandbox=sandbox, env_file=ENV_FILE)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


# ------------------------------------------------------------- framing


def test_the_splitter_uses_lf_and_nothing_else() -> None:
    splitter = LineSplitter()
    payload = json.dumps({"type": "log", "message": UNICODE_SEPARATORS})
    lines = splitter.feed(payload.encode("utf-8") + b"\n")

    assert len(lines) == 1
    assert lines[0].text == payload
    assert json.loads(payload)["message"] == UNICODE_SEPARATORS


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
    assert lines[0].refusal is LineRefusal.TOO_LARGE

    good = splitter.feed(b'{"type":"pong","nonce":"1"}\n')
    assert good[0].text is not None


def test_an_oversized_partial_record_is_refused_before_its_lf() -> None:
    """A sender with no LF must not grow this buffer without bound."""
    splitter = LineSplitter()
    lines = splitter.feed(b"x" * (MAX_LINE_BYTES + 1))

    assert lines[0].refusal is LineRefusal.TOO_LARGE
    assert splitter.pending_bytes() == 0

    splitter.feed(b'rest of the bad line\n{"type":"pong","nonce":"1"}\n')


def test_the_splitter_keeps_no_byte_of_a_record_that_it_drops() -> None:
    """The bytes between the refusal and the next LF are not kept."""
    splitter = LineSplitter(SMALL_CAP)

    assert splitter.feed(b"x" * (SMALL_CAP + 1))[0].refusal is LineRefusal.TOO_LARGE

    for _ in range(5):
        assert splitter.feed(b"y" * 1000) == []
        assert splitter.pending_bytes() == 0

    lines = splitter.feed(b'the end of the record\n{"a":1}\n')

    assert [line.text for line in lines] == ['{"a":1}']
    assert splitter.pending_bytes() == 0


def test_a_record_at_the_cap_leaves_no_byte_for_the_next() -> None:
    """The record fills the buffer before its LF comes. The next record
    starts with an empty buffer."""
    splitter = LineSplitter(SMALL_CAP)

    assert splitter.feed(b"a" * SMALL_CAP) == []
    assert splitter.pending_bytes() == SMALL_CAP

    lines = splitter.feed(b"\nok\n")

    assert [line.text for line in lines] == ["a" * SMALL_CAP, "ok"]
    assert splitter.pending_bytes() == 0


@pytest.mark.parametrize("chunk_bytes", [1, 3, SMALL_CAP, SMALL_CAP + 1, 64, 1000])
def test_the_splitter_holds_the_cap_at_most_in_each_state(chunk_bytes: int) -> None:
    """The result does not change with the size of a chunk."""
    stream = (
        b"a" * 40
        + b"\nok\n"
        + b"b" * 100
        + b"\n"
        + b"c" * SMALL_CAP
        + b"\n"
        + b"d" * (SMALL_CAP + 1)
        + b"e" * 100
    )
    splitter = LineSplitter(SMALL_CAP)
    texts: list[str] = []
    refusals: list[LineRefusal] = []

    for start in range(0, len(stream), chunk_bytes):
        for line in splitter.feed(stream[start : start + chunk_bytes]):
            if line.text is not None:
                texts.append(line.text)

            if line.refusal is not None:
                refusals.append(line.refusal)

        assert splitter.pending_bytes() <= SMALL_CAP

    assert texts == ["ok", "c" * SMALL_CAP]
    assert refusals == [LineRefusal.TOO_LARGE] * 3
    assert splitter.pending_bytes() == 0


# --------------------------------------------------- the cap of a text


@pytest.mark.parametrize("wide", [TWO_BYTES, THREE_BYTES, FOUR_BYTES])
def test_a_cut_ends_on_a_whole_character(wide: str) -> None:
    cap = 64
    width = _size(wide)

    for room in range(width):
        # `room` bytes are free before the cap, and the character needs more.
        text = "m" * (cap - room) + wide + "tail"

        assert cut_bytes(text, cap) == "m" * (cap - room)

    exact = "m" * (cap - width) + wide

    assert cut_bytes(exact + "tail", cap) == exact


def test_a_text_that_fits_stays_as_it_is() -> None:
    for text in ("", "exit 137", TWO_BYTES * 32, FOUR_BYTES * 16):
        assert cut_bytes(text, 64) == text


def test_a_cut_at_no_byte_gives_the_empty_text() -> None:
    assert cut_bytes("exit 137", 0) == ""
    assert cut_bytes("exit 137", -1) == ""
    assert cut_bytes(TWO_BYTES, 1) == ""


def test_half_a_pair_counts_as_three_bytes_and_stays() -> None:
    text = "a" + HALF_PAIR + "b"

    assert cut_bytes(text, 1 + HALF_PAIR_BYTES + 1) == text
    assert cut_bytes(text, 1 + HALF_PAIR_BYTES) == "a" + HALF_PAIR
    assert cut_bytes(text, HALF_PAIR_BYTES) == "a"
    assert cut_bytes(HALF_PAIR * 4, 2 * HALF_PAIR_BYTES + 1) == HALF_PAIR * 2


# ------------------------------------------------------------- sbx exec


def test_the_facade_command_carries_the_env_file_and_the_sandbox() -> None:
    """Contract 03 §7.1. `sbx exec` forwards no host environment, so a
    command without `--env-file` starts a facade that finds none of its
    mounts."""
    argv = build_argv(DEFAULT_FACADE_COMMAND, dial(SANDBOX))

    assert argv[:2] == ["sbx", "exec"]
    assert argv[argv.index("--env-file") + 1] == ENV_FILE
    assert SANDBOX in argv


def test_the_sandbox_id_cannot_inject_a_second_word() -> None:
    argv = build_argv(DEFAULT_FACADE_COMMAND, dial("chat-s3; rm -rf /"))

    assert argv[:2] == ["sbx", "exec"]
    assert "chat-s3; rm -rf /" in argv
    assert "rm" not in argv


def test_the_env_file_path_cannot_inject_a_second_word() -> None:
    """The path comes out of a status document another process writes, so
    it is untrusted the same way the sandbox id is (invariant 12)."""
    argv = build_argv(DEFAULT_FACADE_COMMAND, SandboxDial(SANDBOX, "/tmp/e.env --privileged"))

    assert "/tmp/e.env --privileged" in argv
    assert "--privileged" not in argv


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


@pytest.mark.slow
async def test_the_exec_channel_round_trips_through_a_real_process() -> None:
    """`cat` echoes stdin to stdout, so the framing crosses a real pipe."""
    channel = ExecChannel(dial=dial(), command="cat")
    await channel.start()
    await channel.send('{"type":"pong","nonce":"9f13"}\n')
    line = await asyncio.wait_for(channel.receive(), 5.0)

    assert line is not None
    assert line.text == '{"type":"pong","nonce":"9f13"}'
    await channel.close()


#: A child that writes one stderr line of 200,000 bytes, then one short line,
#: then waits on stdin. The stream reader of the host holds 65,536 bytes.
LONG_STDERR_LINE = (
    f"{sys.executable} -c \"import sys; sys.stderr.write('x' * 200000 + '\\nafter\\n'); "
    'sys.stderr.flush(); sys.stdin.read()"'
)


@pytest.mark.slow
async def test_a_long_stderr_line_does_not_end_the_log(tmp_path: Path) -> None:
    """stderr is free text from the far side. One long line must not stop the
    reader, or the text after it reaches no log and the pipe fills."""
    log = tmp_path / "playpen.log"
    channel = ExecChannel(dial=dial(), command=LONG_STDERR_LINE, log_path=log)
    await channel.start()

    try:
        await asyncio.wait_for(_until(lambda: log.is_file() and b"after" in log.read_bytes()), 5.0)
    finally:
        await channel.close()


@pytest.mark.slow
async def test_close_ends_after_a_stderr_read_failed() -> None:
    """A pipe that failed keeps its error. `close` must still reap the child,
    or the link keeps a channel that is gone and the next dial fails."""
    channel = ExecChannel(dial=dial(), command="cat")
    await channel.start()
    process = channel._process
    assert process is not None
    assert process.stderr is not None
    process.stderr.set_exception(OSError("the read failed"))

    await channel.close()

    assert process.returncode is not None


@pytest.mark.slow
async def test_a_stderr_reader_that_fails_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """The reader goes on after a long line and after nothing else. An error
    of another type ends the task, and the log must name the task."""
    channel = ExecChannel(dial=dial(), command="cat")
    await channel.start()
    process = channel._process
    assert process is not None
    assert process.stderr is not None

    with caplog.at_level(logging.ERROR, logger="attendance"):
        process.stderr.set_exception(OSError("the read failed"))
        await asyncio.wait_for(_until(lambda: "the read failed" in caplog.text), 2.0)

    assert f"task stderr {SANDBOX} ended with an error" in caplog.text
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


# ------------------------------------------------------- the supervisor lock


async def test_an_absent_lock_file_frees_the_dial_at_once(tmp_path: Path) -> None:
    """§11.4 rule 4, step 1: the last supervisor removed it as it exited."""
    path = playpen_lock_file(tmp_path, FAMILY, SANDBOX)
    started = asyncio.get_running_loop().time()

    assert await wait_out_lock(path, 10.0, 0.02, 1.0) is True
    assert asyncio.get_running_loop().time() - started < 1.0


async def test_a_lock_whose_beat_stopped_is_removed(tmp_path: Path) -> None:
    """§11.4 rule 4, step 2: a counter that stopped means nobody is writing."""
    path = playpen_lock_file(tmp_path, FAMILY, SANDBOX)
    _write_lock(tmp_path, beat=41)
    started = asyncio.get_running_loop().time()

    assert await wait_out_lock(path, 0.2, 0.02, 5.0) is True
    assert asyncio.get_running_loop().time() - started >= 0.2
    assert path.exists() is False


async def test_a_beating_lock_keeps_the_host_waiting(tmp_path: Path) -> None:
    """§11.4 rule 4, step 4: a counter that moves is a live supervisor."""
    path = playpen_lock_file(tmp_path, FAMILY, SANDBOX)
    # The first beat lands before the wait, so the reader never sees the file
    # absent.
    _write_lock(tmp_path, beat=1)
    beating = asyncio.create_task(_beat_lock(tmp_path, every_s=0.02, first=2))

    try:
        assert await wait_out_lock(path, 0.2, 0.01, 0.3) is False
    finally:
        beating.cancel()


async def test_one_unreadable_read_never_frees_the_dial(tmp_path: Path) -> None:
    """§11.4 rule 4, step 3: a half-written file decides nothing on its own."""
    path = playpen_lock_file(tmp_path, FAMILY, SANDBOX)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{half writ", encoding="utf-8")
    started = asyncio.get_running_loop().time()

    # A file that never reads cleanly for a whole window is not one a live
    # supervisor is writing, so the wait is `stale_s` and not the deadline.
    assert await wait_out_lock(path, 0.2, 0.02, 5.0) is True
    assert asyncio.get_running_loop().time() - started >= 0.2


def _write_lock(state_root: Path, beat: int, host_deadline_s: float = 90.0) -> None:
    """The lock file as a supervisor writes it (contract 03 §11.1 rule 3).

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
    """A live supervisor, rewriting its lock with a counter one higher."""
    beat = first

    while True:
        _write_lock(state_root, beat=beat)
        beat += 1
        await asyncio.sleep(every_s)


async def _until(test: Callable[[], bool]) -> None:
    while not test():
        await asyncio.sleep(0.005)
