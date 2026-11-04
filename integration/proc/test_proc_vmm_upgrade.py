"""The Upgrade request of the `acp` route, sent by httpx 0.28.1 over `uds=`.

The `acp` request sends a JSON body with an Upgrade (decision CH20). The gap
of `wire/AGENTS.md` asks two things. The Rust test
`rust/crates/vmm/tests/upgrade_body.rs` proves that hyper reads the body
before it answers 101. This file proves the client half: httpx sends the
body, reads 101, then reads and writes raw bytes.

The server is the example `upgrade_echo` of the crate `creche-vmm`: the
accept loop and the protocol header check of the vmm, with one handler. The
handler reads the body, answers 101, writes the body back on the raw stream,
then echoes. So the first bytes after 101 are the body that the server read.

No service of the table runs here, so the test names a program (rule 1 of
`integration/proc/AGENTS.md` covers a service). The fixture builds the
example with the cargo of `rust/rust-toolchain.toml`, once per worker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpcore
import httpx
import pytest
from creche_wire.routes import ACP, HOST_API_HEADER, HOST_API_PROTOCOL
from proc_harness import Supervisor, UnixAddress
from proc_services import NO_SKIP_ENV
from proc_tree import Tree, repo_root

RUST_DIR = repo_root() / "rust"
EXAMPLE = "upgrade_echo"
#: A cold build took 6 s on a laptop with 4 jobs. The limit leaves room for a runner.
BUILD_TIMEOUT_S = 150
BODY = json.dumps({"family": "chat", "id": "0b9d6f4e-8a51-4c3e-9f57-3d2b1c0a9e88"}).encode()


@pytest.fixture(scope="module")
def upgrade_echo() -> Path:
    """The built example. A machine with no cargo skips, or fails with no skip."""
    if shutil.which("cargo") is None:
        if os.environ.get(NO_SKIP_ENV):
            pytest.fail(f"the example {EXAMPLE} needs `cargo` on PATH ({NO_SKIP_ENV} is set)")
        pytest.skip(f"the example {EXAMPLE} needs `cargo` on PATH")

    if shutil.which("rustup") is not None:
        subprocess.run(
            ["rustup", "toolchain", "install", "--no-self-update"], cwd=RUST_DIR, check=True
        )

    build = ["cargo", "build", "--locked", "-p", "creche-vmm", "--example", EXAMPLE]
    done = subprocess.run(
        [*build, "--message-format=json-render-diagnostics"],
        cwd=RUST_DIR,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_S,
        check=True,
    )
    messages = [json.loads(line) for line in done.stdout.splitlines()]
    (binary,) = [
        message["executable"]
        for message in messages
        if message.get("reason") == "compiler-artifact" and message["target"]["name"] == EXAMPLE
    ]

    return Path(binary)


def test_httpx_sends_the_body_then_reads_raw_bytes_after_101(
    tree: Tree, supervisor: Supervisor, upgrade_echo: Path
) -> None:
    """httpx sends the Content-Length body, gets 101, then reads and writes raw bytes."""
    socket = tree.root / "sock" / "vmm.sock"
    socket.parent.mkdir(exist_ok=True)
    child = supervisor.spawn(
        EXAMPLE, [str(upgrade_echo), str(socket)], {"PATH": os.environ["PATH"]}, tree.root
    )
    supervisor.wait_ready(child, UnixAddress(socket))
    headers = {
        HOST_API_HEADER: str(HOST_API_PROTOCOL),
        "Content-Type": "application/json",
        "Connection": "Upgrade",
        "Upgrade": ACP.token,
    }

    with (
        httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)), timeout=10) as client,
        client.stream("POST", f"http://vmm{ACP.path}", content=BODY, headers=headers) as response,
    ):
        stream = response.extensions["network_stream"]
        echoed = _read(stream, len(BODY))
        stream.write(b"ping\n")
        ping = _read(stream, len(b"ping\n"))

    assert response.status_code == 101
    assert echoed == BODY
    assert ping == b"ping\n"


def _read(stream: httpcore.NetworkStream, size: int) -> bytes:
    """Exactly `size` bytes from the raw stream after 101."""
    data = b""

    while len(data) < size:
        chunk = stream.read(size - len(data))
        assert chunk, f"the stream ended after {len(data)} of {size} bytes"
        data += chunk

    return data
