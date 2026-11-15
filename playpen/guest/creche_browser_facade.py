#!/usr/local/bin/python3.12 -I
"""creche-browser-facade: the facade of the browser service image.

The browser VM runs headless Chrome for the `browser` MCP upstream, with
Chrome's own sandbox on (browser design note, section 10). Guest init
treats this program as it treats the facade of a family VM
(`rust/crates/guest-init/AGENTS.md`): it starts it as root at
`creche_wire::guest::FACADE_PROGRAM`, restarts it after an exit, and stops
it with the stop line of the local link. It splices each link of guest
port 1201 to `RELAY_SOCKET`, byte for byte.

    host vmm --vsock 1201--> guest init --RELAY_SOCKET--> this program
                                                            |
                       first line "mcp"  -> agent-browser mcp (user browser)
                       first line "view" -> stream server, 127.0.0.1:9223

1. The program opens the local link at once, so guest init marks it
   `running`. It sends no line on it. The stop line `{"stop":{}}`, or the
   end of the link, ends the program and every child it started.
2. Each relay link starts with one line that names the link. `mcp` starts
   `agent-browser mcp --tools core` as the user `browser`, with the link
   as its stdin and stdout. `view` joins the link to the stream server of
   agent-browser. Any other line closes the link.
3. Each MCP link shares one Chrome: agent-browser keeps one daemon per
   session name, and every link uses the default one. The live view thus
   shows the browser that the agent drives.
4. HOME is a new directory on the guest tmpfs, so each VM start has a
   fresh profile and nothing persists (decision V4).

The vmm is the guard of each link: it checks who may open it and filters
the view. Nothing here is a guard.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Final

#: `creche_wire::guest::LOCAL_LINK_SOCKET`. A playpen test holds them equal.
LOCAL_LINK_SOCKET: Final = "/run/creche/guest-init.sock"

#: `creche_wire::guest::RELAY_SOCKET`. A playpen test holds them equal.
RELAY_SOCKET: Final = "/tmp/creche-facade/acp.sock"

#: `creche_wire::guest::SESSION_ID_ENV`: guest init sets it.
SESSION_ID_ENV: Final = "CRECHE_SESSION_ID"

#: The exit status with no session id, as `creche-facade` has.
EXIT_NO_SESSION: Final = 17

#: The stop line of the local link, as guest init writes it.
STOP_LINE: Final[dict[str, dict[str, object]]] = {"stop": {}}

#: The one line that starts each relay link, LF included.
LINK_LINE_CAP: Final = 16

#: How long a relay link may take to send its first line.
LINK_LINE_WAIT: Final = 10.0

#: Open links of each kind. A link past the cap closes at once.
LINK_CAP: Final = 8

#: The user that runs agent-browser and Chrome.
BROWSER_USER: Final = "browser"

#: The fresh home of each VM start, on the guest tmpfs.
BROWSER_HOME: Final = Path("/tmp/browser-home")

#: agent-browser, at its pinned sha256 (`playpen/Dockerfile`).
AGENT_BROWSER: Final = "/usr/local/bin/agent-browser"

#: The Chrome wrapper of the image: Google's Chrome with the proxy and no
#: background traffic.
CHROME: Final = "/usr/local/bin/creche-chrome"

#: Where agent-browser's stream server listens in the guest.
STREAM_HOST: Final = "127.0.0.1"
STREAM_PORT: Final = 9223

#: The env of agent-browser. No other name reaches it.
AGENT_ENV: Final = {
    "AGENT_BROWSER_CONTENT_BOUNDARIES": "1",
    "AGENT_BROWSER_EXECUTABLE_PATH": CHROME,
    "AGENT_BROWSER_MAX_OUTPUT": "200000",
    "AGENT_BROWSER_NO_VERSION_CHECK": "1",
    "AGENT_BROWSER_STREAM_PORT": str(STREAM_PORT),
    "HOME": str(BROWSER_HOME),
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "TMPDIR": "/tmp",
}

#: The argv of one MCP link.
AGENT_ARGV: Final = (AGENT_BROWSER, "mcp", "--tools", "core")

LINK_MCP: Final = "mcp"
LINK_VIEW: Final = "view"
LINK_KINDS: Final = (LINK_MCP, LINK_VIEW)


def say(text: str) -> None:
    """Write one line to the console, which is the stderr of this program."""
    sys.stderr.write(f"browser-facade: {text}\n")
    sys.stderr.flush()


def link_kind(line: bytes) -> str | None:
    """The kind that the first line of a relay link names, or None.

    The line is exactly `mcp` or `view`, ended by one LF. Anything else,
    a CR included, names no kind.
    """
    if len(line) > LINK_LINE_CAP or not line.endswith(b"\n"):
        return None

    text = line[:-1]
    for kind in LINK_KINDS:
        if text == kind.encode():
            return kind

    return None


def is_stop(line: bytes) -> bool:
    """True for the stop line of the local link, `{"stop":{}}`."""
    try:
        return json.loads(line) == STOP_LINE
    except ValueError:
        return False


def read_line(conn: socket.socket) -> bytes:
    """The first line of a link, at most `LINK_LINE_CAP` bytes.

    It reads one byte at a time, so no byte after the LF leaves the
    socket: the rest of the link belongs to agent-browser or the stream.
    """
    conn.settimeout(LINK_LINE_WAIT)
    line = b""
    while len(line) < LINK_LINE_CAP and not line.endswith(b"\n"):
        byte = conn.recv(1)
        if not byte:
            break
        line += byte

    conn.settimeout(None)
    return line


class Facade:
    """The links and the children of one start of the program."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.open: dict[str, int] = dict.fromkeys(LINK_KINDS, 0)
        self.children: set[subprocess.Popen[bytes]] = set()

    def take(self, kind: str) -> bool:
        """Counts one more open link of `kind`, or False past the cap."""
        with self.lock:
            if self.open[kind] >= LINK_CAP:
                return False
            self.open[kind] += 1
            return True

    def give(self, kind: str) -> None:
        with self.lock:
            self.open[kind] -= 1

    def serve(self, conn: socket.socket, number: int) -> None:
        """Serves one relay link until it ends."""
        try:
            kind = link_kind(read_line(conn))
        except OSError as error:
            say(f"link {number} read failed: {error}")
            conn.close()
            return

        if kind is None:
            say(f"link {number} refused: no kind line")
            conn.close()
            return

        if not self.take(kind):
            say(f"link {number} refused: {kind} links at the cap")
            conn.close()
            return

        say(f"link {number} {kind} open")
        try:
            if kind == LINK_MCP:
                self.run_mcp(conn)
            else:
                view(conn)
        except OSError as error:
            say(f"link {number} failed: {error}")
        finally:
            self.give(kind)
            conn.close()
            say(f"link {number} closed")

    def run_mcp(self, conn: socket.socket) -> None:
        """Runs agent-browser with the link as its stdin and stdout."""
        child = subprocess.Popen(
            AGENT_ARGV,
            stdin=conn.fileno(),
            stdout=conn.fileno(),
            env=AGENT_ENV,
            cwd=BROWSER_HOME,
            user=BROWSER_USER,
            group=BROWSER_USER,
            extra_groups=[],
            start_new_session=True,
        )
        with self.lock:
            self.children.add(child)

        child.wait()
        with self.lock:
            self.children.discard(child)

    def end(self) -> None:
        """Sends SIGKILL to the group of each child, then to the browser user.

        agent-browser leaves its daemon and Chrome running after an MCP
        link ends, so the last step reaches them too.
        """
        with self.lock:
            children = list(self.children)

        for child in children:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)

        subprocess.run(["pkill", "-KILL", "-u", BROWSER_USER], check=False)


def view(conn: socket.socket) -> None:
    """Joins the link to the stream server, both ways, until either ends."""
    stream = socket.create_connection((STREAM_HOST, STREAM_PORT), timeout=LINK_LINE_WAIT)
    stream.settimeout(None)
    other = threading.Thread(target=pump, args=(stream, conn), daemon=True)
    other.start()
    pump(conn, stream)
    other.join()
    stream.close()


def pump(source: socket.socket, sink: socket.socket) -> None:
    """Copies `source` to `sink` until EOF, then half-closes `sink`."""
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)


def make_home() -> None:
    """Makes the fresh home of the browser user, owned by it, mode 0700."""
    user = pwd.getpwnam(BROWSER_USER)
    if BROWSER_HOME.exists():
        shutil.rmtree(BROWSER_HOME)

    BROWSER_HOME.mkdir(mode=0o700)
    os.chown(BROWSER_HOME, user.pw_uid, user.pw_gid)


def listen_relay() -> socket.socket:
    """Listens on `RELAY_SOCKET`, mode 0600 in a 0700 directory."""
    path = Path(RELAY_SOCKET)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(RELAY_SOCKET)
    path.chmod(0o600)
    server.listen(LINK_CAP * len(LINK_KINDS))
    return server


def accept_links(server: socket.socket, facade: Facade) -> None:
    """Serves each relay link on a thread of its own."""
    number = 0
    while True:
        conn, _ = server.accept()
        number += 1
        threading.Thread(target=facade.serve, args=(conn, number), daemon=True).start()


def main() -> int:
    if not os.environ.get(SESSION_ID_ENV):
        say(f"no {SESSION_ID_ENV}: this program runs under guest init only")
        return EXIT_NO_SESSION

    facade = Facade()
    make_home()
    server = listen_relay()
    local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    local.connect(LOCAL_LINK_SOCKET)
    threading.Thread(target=accept_links, args=(server, facade), daemon=True).start()
    say("up")

    # The local link carries only the stop line. Its end, or the stop,
    # ends the program.
    reader = local.makefile("rb")
    for line in reader:
        if is_stop(line):
            say("stop")
            break

    facade.end()
    return 0


if __name__ == "__main__":
    sys.exit(main())
