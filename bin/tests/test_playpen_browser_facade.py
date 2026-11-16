"""`playpen/guest/creche_browser_facade.py`, the facade of the browser
service image (browser design note, section 10).

The tests read the program as a module. They check the line rules of the
relay links and of the local link, that the first-line read leaves the
rest of the link in the socket, the copy of the view, and the paths that
guest init and the image share. The whole program runs only in the guest,
as root, under guest init: the browser proof boots it there.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FACADE = REPO_ROOT / "playpen" / "guest" / "creche_browser_facade.py"
CHROME = REPO_ROOT / "playpen" / "guest" / "creche-chrome"
DOCKERFILE = REPO_ROOT / "playpen" / "Dockerfile"
GUEST_VECTORS = REPO_ROOT / "rust" / "crates" / "wire" / "vectors" / "guest.json"

#: The first line of the browser service target.
TARGET = "FROM ${PYTHON_IMAGE} AS service-browser"


def _facade() -> ModuleType:
    """The facade as a module, to read its constants and functions."""
    spec = importlib.util.spec_from_file_location("creche_browser_facade", FACADE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (b"mcp\n", "mcp"),
        (b"view\n", "view"),
        (b"mcp", None),
        (b"mcp\r\n", None),
        (b" mcp\n", None),
        (b"MCP\n", None),
        (b"\n", None),
        (b"viewer\n", None),
        (b"m" * 15 + b"\n", None),
        (b"m" * 16 + b"\n", None),
    ],
)
def test_the_first_line_names_the_link(line: bytes, kind: str | None) -> None:
    """Only `mcp` or `view` and one LF names a link."""
    assert _facade().link_kind(line) == kind


@pytest.mark.parametrize(
    ("line", "stop"),
    [
        (b'{"stop":{}}\n', True),
        (b'{"stop": {}}', True),
        (b'{"stop":{"now":1}}\n', False),
        (b'{"go":{}}\n', False),
        (b"stop\n", False),
        (b"\xff\n", False),
    ],
)
def test_only_the_stop_line_stops(line: bytes, stop: bool) -> None:
    assert _facade().is_stop(line) is stop


def test_the_first_line_read_leaves_the_rest() -> None:
    """The bytes after the LF stay in the socket for agent-browser."""
    facade = _facade()
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b'mcp\n{"jsonrpc":"2.0"}\n')
        assert facade.read_line(right) == b"mcp\n"
        assert right.recv(64) == b'{"jsonrpc":"2.0"}\n'


def test_the_first_line_read_stops_at_the_cap() -> None:
    """A link with no LF in its first 16 bytes reads only 16 of them."""
    facade = _facade()
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"x" * 40)
        line = facade.read_line(right)
        assert line == b"x" * facade.LINK_LINE_CAP
        assert facade.link_kind(line) is None


def test_the_view_copy_runs_both_ways() -> None:
    """`pump` copies one way to EOF and half-closes its sink."""
    facade = _facade()
    viewer, near = socket.socketpair()
    far, stream = socket.socketpair()
    with viewer, near, far, stream:
        threads = [
            threading.Thread(target=facade.pump, args=(near, far)),
            threading.Thread(target=facade.pump, args=(far, near)),
        ]
        for thread in threads:
            thread.start()

        stream.sendall(b"frame")
        assert viewer.recv(16) == b"frame"
        viewer.sendall(b"ack")
        assert stream.recv(16) == b"ack"
        viewer.shutdown(socket.SHUT_WR)
        assert stream.recv(16) == b""
        stream.shutdown(socket.SHUT_WR)
        assert viewer.recv(16) == b""
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_the_paths_are_the_ones_of_guest_init() -> None:
    """The facade path, the local link, the relay and the session env."""
    facade = _facade()
    guest = json.loads(GUEST_VECTORS.read_text(encoding="utf-8"))
    assert guest["local_link_socket"] == facade.LOCAL_LINK_SOCKET
    assert guest["relay_socket"] == facade.RELAY_SOCKET
    assert guest["session_id_env"] == facade.SESSION_ID_ENV
    copy = f"COPY playpen/guest/creche_browser_facade.py {guest['facade_program']}"
    browser = DOCKERFILE.read_text(encoding="utf-8").split(TARGET, 1)[1]
    assert copy in browser


def test_chrome_keeps_its_sandbox_and_uses_the_proxy() -> None:
    """No flag of the wrapper turns the sandbox off, and every request
    goes through the egress row of guest init."""
    facade = _facade()
    guest = json.loads(GUEST_VECTORS.read_text(encoding="utf-8"))
    wrapper = CHROME.read_text(encoding="utf-8")
    assert "--no-sandbox" not in wrapper
    assert f"--proxy-server=http://127.0.0.1:{guest['egress_proxy_port']}" in wrapper
    assert "--disable-background-networking" in wrapper
    assert facade.AGENT_ENV["AGENT_BROWSER_EXECUTABLE_PATH"] == "/usr/local/bin/creche-chrome"
    assert "AGENT_BROWSER_ARGS" not in facade.AGENT_ENV


def test_the_browser_target_ships_only_here() -> None:
    """The target copies the facade and the wrapper, removes the setuid
    helper, and its build runs the facade outside guest init."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count(TARGET) == 1
    browser = text.split(TARGET, 1)[1].split("\nFROM ", 1)[0]
    assert "creche_scanner_facade" not in browser
    assert "COPY playpen/guest/creche-chrome /usr/local/bin/creche-chrome" in browser
    assert "RUN chmod u-s /opt/google/chrome/chrome-sandbox" in browser
    assert '[ "$status" -eq 17 ]' in browser
    assert FACADE.read_text(encoding="utf-8").startswith("#!/usr/local/bin/python3.14 -I\n")
    for flavor in text.split(TARGET, 1)[0].split("\nFROM ")[1:]:
        assert "creche_browser_facade" not in flavor
