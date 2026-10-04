"""The test side of a pseudo-terminal, for a program that a person types.

A test plays the person at the keyboard. It holds the master side of the
terminal, and the program holds the slave side as its controlling terminal:

1. `type` writes what the person types. The terminal gives it to the program
   that reads next, so a test can type before the program asks.
2. `EOF` and `INTERRUPT` are keys. The terminal turns the first into the end
   of the input, and the second into SIGINT for the foreground group.
3. `hang_up` closes the terminal, as a closed window does. The system sends
   SIGHUP to the leader of the session and to the foreground group.
4. One thread reads everything the program writes, from the start to the
   end, into a file under the root. A program that writes to a terminal
   which nobody reads blocks, and it can block in its own exit.

Nothing here knows what a service is. `Supervisor.spawn_on_terminal` starts
the program and closes the terminal with the test.
"""

from __future__ import annotations

import contextlib
import os
import pty
import select
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: The program that makes the terminal the controlling terminal of a command.
LOGIN_PROGRAM: Final = Path(__file__).resolve().parent / "proc_login.py"

#: The key that ends the input, at the start of a line (termios `VEOF`).
EOF: Final = "\x04"

#: The key that interrupts the foreground group (termios `VINTR`).
INTERRUPT: Final = "\x03"

#: The key that ends a line.
ENTER: Final = "\n"

#: How long a test waits for a line on the terminal.
SHOW_DEADLINE_S: Final = 30.0

_POLL_S: Final = 0.02
_CHUNK: Final = 4096
_JOIN_S: Final = 5.0

#: A terminal changes each LF that a program writes into CR and LF.
_TERMINAL_NEWLINE: Final = "\r\n"


class TerminalError(Exception):
    """The terminal did not show what a test waited for. The message holds its text."""


@dataclass(slots=True)
class Terminal:
    """The master side of one pseudo-terminal, and everything it showed."""

    master: int
    log_path: Path
    _seen: bytearray = field(default_factory=bytearray)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)
    _ended: threading.Event = field(default_factory=threading.Event)
    _reader: threading.Thread | None = None
    _open: bool = True

    def start(self) -> None:
        """Start the thread that reads what the program writes."""
        self._reader = threading.Thread(target=self._read, name="proc-terminal", daemon=True)
        self._reader.start()

    def type(self, text: str) -> None:
        """Write what the person types. The terminal keeps it for the next reader."""
        if not self._open:
            raise TerminalError("the terminal is closed")

        os.write(self.master, text.encode("utf-8"))

    def text(self) -> str:
        """Everything the terminal showed until now, with LF as the line end."""
        with self._lock:
            raw = bytes(self._seen)

        return raw.decode("utf-8", errors="replace").replace(_TERMINAL_NEWLINE, "\n")

    def wait_for(self, wanted: str, deadline_s: float = SHOW_DEADLINE_S) -> None:
        """Return when the terminal showed `wanted`. Raise when it cannot.

        The wait ends early when every program closed the terminal: nothing
        more can come then.
        """
        deadline = time.monotonic() + deadline_s

        while wanted not in self.text():
            if self._ended.is_set() and wanted not in self.text():
                raise TerminalError(
                    f"the terminal closed and never showed {wanted!r}\n{self.text()}"
                )

            if time.monotonic() > deadline:
                raise TerminalError(
                    f"the terminal did not show {wanted!r} in {deadline_s} s\n{self.text()}"
                )

            time.sleep(_POLL_S)

    def hang_up(self) -> None:
        """Close the terminal while the program runs, as a closed window does."""
        self.close()

    def close(self) -> None:
        """Stop the reader and close the master side. A second call does nothing."""
        if not self._open:
            return

        self._open = False
        self._stop.set()

        if self._reader is not None:
            self._reader.join(_JOIN_S)

        with contextlib.suppress(OSError):
            os.close(self.master)

    def _read(self) -> None:
        """Copy what the program writes into the file, until the terminal ends.

        The thread never blocks in `read`. A `close` in another thread does
        not end a `read` that waits, and the terminal would then stay open.
        """
        try:
            with self.log_path.open("ab", buffering=0) as log:
                while not self._stop.is_set():
                    chunk = self._next_chunk()

                    if chunk is None:
                        return

                    if chunk:
                        log.write(chunk)

                        with self._lock:
                            self._seen.extend(chunk)
        finally:
            self._ended.set()

    def _next_chunk(self) -> bytes | None:
        """What the program wrote, empty when it wrote nothing, None at the end.

        Linux answers EIO when no program holds the slave side. macOS
        answers an empty read.
        """
        try:
            ready, _, _ = select.select([self.master], [], [], _POLL_S)
        except (OSError, ValueError):
            return None

        if not ready:
            return b""

        try:
            chunk = os.read(self.master, _CHUNK)
        except OSError:
            return None

        return chunk if chunk else None


def open_terminal(log_path: Path) -> tuple[Terminal, int]:
    """A new pseudo-terminal. Returns the test side and the slave descriptor.

    The caller gives the slave to the program and then closes its own copy.
    While the caller holds it, the terminal has a holder and loses nothing.
    """
    master, slave = pty.openpty()

    return Terminal(master=master, log_path=log_path), slave
