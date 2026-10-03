"""The real channel: one long-lived child process over stdio (contract 03 §1).

On the host the command is

    sbx exec --env-file <supervisor.env> <sandbox id> -- \\
      node /opt/agent-supervisor/agent-supervisor.js --sandbox <sandbox id>

It is configurable so a test can point it at anything, and so the command can
change without a code change.

Four facts shape this module.

1. `sbx exec` inherits and consumes stdin. That is the mechanism the channel
   relies on, and it is why every other `sbx exec` the platform runs needs
   `</dev/null` (contract 03 §1).
2. stdout carries the protocol and nothing else. stderr is free text and is
   written to a log file, never parsed as protocol (contract 03 §1 rule 4).
3. `sbx exec` forwards NO host environment. `--env-file` is the only way a
   value reaches the VM, and `caregiver` writes the file it names
   (contract 03 §7.1).
4. The playpen exits 2 without a sandbox id, so `--sandbox` is part of the
   command and not an optional extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
from pathlib import Path

from .channel import ChannelClosed, SandboxDial
from .wire import MAX_LINE_BYTES, LineSplitter, RawLine

READ_CHUNK_BYTES = 65_536
SANDBOX_TEMPLATE_FIELD = "{sandbox}"
ENV_FILE_TEMPLATE_FIELD = "{env_file}"
DEFAULT_COMMAND = (
    "sbx exec --env-file {env_file} {sandbox} -- "
    "node /opt/agent-supervisor/agent-supervisor.js --sandbox {sandbox}"
)
_STDERR_LIMIT_BYTES = 4_096

#: How long a terminated child has to exit before it is killed, and a killed
#: one before `close` gives up on it. A healthy `sbx exec` exits at once.
_EXIT_WAIT_S = 5.0


def build_argv(template: str, dial: SandboxDial) -> list[str]:
    """Fill the dial into the command template, then split it safely.

    Both values are substituted after splitting, so neither a sandbox id nor
    an env file path can inject a second word into the command line. Both are
    validated before they reach here as well (invariant 14), which makes this
    the second of two checks.
    """
    parts = shlex.split(template)

    return [
        part.replace(SANDBOX_TEMPLATE_FIELD, dial.sandbox).replace(
            ENV_FILE_TEMPLATE_FIELD, dial.env_file
        )
        for part in parts
    ]


class ExecChannel:
    """Speaks contract 03 over a child process's stdin and stdout."""

    def __init__(
        self,
        dial: SandboxDial,
        command: str = DEFAULT_COMMAND,
        log_path: Path | None = None,
        max_line_bytes: int = MAX_LINE_BYTES,
    ) -> None:
        self._sandbox = dial.sandbox
        self._argv = build_argv(command, dial)
        self._log_path = log_path
        self._max_line_bytes = max_line_bytes
        self._splitter = LineSplitter(max_line_bytes)
        self._process: asyncio.subprocess.Process | None = None
        self._pending: list[RawLine] = []
        self._stderr_task: asyncio.Task[None] | None = None
        self._eof = False

    @property
    def sandbox(self) -> str:
        return self._sandbox

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def argv(self) -> list[str]:
        return list(self._argv)

    async def start(self) -> None:
        if not self._argv:
            raise ChannelClosed("channel command is empty")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise ChannelClosed(f"cannot start channel: {error}") from error

        self._eof = False
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def send(self, line: str) -> None:
        process = self._process

        if process is None or process.stdin is None or not self.alive:
            raise ChannelClosed("channel is closed")

        try:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as error:
            raise ChannelClosed(f"channel write failed: {error}") from error

    async def receive(self) -> RawLine | None:
        """One framed record, or None at end of stream.

        Bytes are split here rather than by a stream line reader. A generic
        reader also splits on U+2028 and U+2029, which are legal inside a JSON
        string (contract 03 §2 rule 4).
        """
        while True:
            if self._pending:
                return self._pending.pop(0)

            if self._eof:
                return None

            chunk = await self._read_chunk()

            if not chunk:
                self._eof = True
                continue

            self._pending.extend(self._splitter.feed(chunk))

    async def close(self) -> None:
        process = self._process

        if process is None:
            return

        stderr_task = self._stderr_task

        if stderr_task is not None:
            stderr_task.cancel()
            self._stderr_task = None

        # Closing stdin is what a healthy playpen sees as its exit signal.
        # A client-side kill does not reach the in-VM process (probe 0a, A5),
        # so the playpen's own ping deadline is the real safety net.
        if process.stdin is not None:
            with contextlib.suppress(BrokenPipeError, RuntimeError):
                process.stdin.close()

        if process.returncode is None:
            process.terminate()

        # Dropped before the wait, so a second `close` returns at once.
        self._process = None

        await _reap(process, stderr_task)

    async def _read_chunk(self) -> bytes:
        process = self._process

        if process is None or process.stdout is None:
            return b""

        try:
            return await process.stdout.read(READ_CHUNK_BYTES)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            return b""

    async def _drain_stderr(self) -> None:
        """stderr is free text. It reaches a log file and never the protocol."""
        process = self._process

        if process is None or process.stderr is None:
            return

        while True:
            chunk = await process.stderr.readline()

            if not chunk:
                return

            self._write_log(chunk[:_STDERR_LIMIT_BYTES])

    def _write_log(self, chunk: bytes) -> None:
        if self._log_path is None:
            return

        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

            with self._log_path.open("ab") as handle:
                handle.write(f"[{self._sandbox}] ".encode() + chunk.rstrip(b"\n") + b"\n")
        except OSError:
            return


async def _drained(reader: asyncio.StreamReader | None) -> None:
    """Read `reader` to its end and drop what it says."""
    if reader is None:
        return

    # RuntimeError: another coroutine is still reading, and sees the end itself.
    with contextlib.suppress(RuntimeError, ConnectionResetError):
        while await reader.read(READ_CHUNK_BYTES):
            continue


async def _ended(
    process: asyncio.subprocess.Process, stderr_task: asyncio.Task[None] | None
) -> None:
    """Return once the child has exited and each of its pipes has ended.

    asyncio closes a child's transport at that point, and not before. A
    transport still open when its loop closes fails later, in the collector:
    "Exception ignored in BaseSubprocessTransport.__del__ ... Event loop is
    closed".
    """
    # The cancelled reader lets go of stderr first. `gather`, and not a
    # suppressed CancelledError: a cancel of THIS call still gets out.
    if stderr_task is not None:
        await asyncio.gather(stderr_task, return_exceptions=True)

    if process.stdin is not None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()

    await _drained(process.stdout)
    await _drained(process.stderr)
    await process.wait()


async def _reap(
    process: asyncio.subprocess.Process, stderr_task: asyncio.Task[None] | None
) -> None:
    """Wait for a terminated child to end, and kill one that does not.

    Bounded twice over, because `close` runs under the dial lock: a child
    that ignores SIGTERM is killed, and one whose pipes a descendant still
    holds is left to the loop.
    """
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_EXIT_WAIT_S):
            await _ended(process, stderr_task)
            return

    with contextlib.suppress(ProcessLookupError):
        process.kill()

    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_EXIT_WAIT_S):
            await _ended(process, stderr_task)
