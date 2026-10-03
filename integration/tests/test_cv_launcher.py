"""Packet CV: `agent-pi-launch` against the real playpen, off the host.

The stage 1 gate proves one channel. This proves the OTHER way into a
sandbox, contract 03 §7.6: a terminal on a session the playpen is already
serving.

    fake_sbx.py exec --env-file <supervisor.env> chat-s1 -- \\
      node playpen/dist/agent-pi-launch.js --sandbox chat-s1 --session ...

Nothing here is faked that is not faked in the gate. The door, `sessiond`, the
playpen bundle and the launcher bundle are all real, both bundles read the
same `supervisor.env` through the same stand-in for `sbx exec`, and the pi
shim is the gate's own.

Two scenarios, one per promise §7.6 makes:

1. **It refuses while a live rpc process holds the session** (§7.5). Exit 8,
   with the session named. The gate's `sessiond` lease cannot see inside a
   sandbox, so this record is what stops two writers on one pi session store.
2. **The terminal's pi command is the playpen's, minus `--mode rpc`.** The
   playpen's own harness asserts that against its pool. This asserts it
   against the BUILT bundles, where a build that shipped two different
   builders would still look right in unit tests.

`SESSIOND_SANDBOX_SESSIONS_MOUNT` is set for the launcher here, and it is
§7.1 rule 6's one seam: on the host the sessions mount is at its host path and
nothing sets it, and a harness whose store is a temp directory has to say so.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import SANDBOX, Stack, fake_sbx_script, repo_root, until

CHAT_PATH = "/v1/chat/completions"
HTTP_OK = 200

#: Contract 03 §7.6's exit codes, for the two this file provokes.
EXIT_SESSION_HELD = 8

#: Contract 03 §1 rule 3. The two argv entries an interactive pi drops.
RPC_MODE_ARGS = ["--mode", "rpc"]

LAUNCH_TIMEOUT_S = 30.0

_BUILD_HINT = "run `pnpm install && pnpm build` in playpen/ first"


def launch_bundle() -> Path:
    """The built launcher. `pnpm build` in `playpen/` writes it."""
    return repo_root() / "playpen" / "dist" / "agent-pi-launch.js"


@pytest.fixture(autouse=True)
def _require_launch_bundle() -> None:
    """Skip rather than fail, the way the gate's own bundle check does."""
    bundle = launch_bundle()
    if not bundle.exists():
        pytest.skip(f"{bundle} is missing: {_BUILD_HINT}")


def process_record(stack: Stack, session: str) -> Path:
    """Contract 03 §7.5's record, in the control mount both programs share."""
    return stack.mounts.control / "processes" / f"{session}.json"


def read_record(stack: Stack, session: str) -> dict[str, object]:
    return json.loads(process_record(stack, session).read_text(encoding="utf-8"))


def run_launcher(stack: Stack, session: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """The TUI door's `sbx exec -it`, through the gate's stand-in for sbx.

    The environment is built here rather than inherited, because that is the
    whole point of the stand-in: `sbx exec` forwards none of the host's, and
    `--env-file` is what carries §7.1's three directories in. The two names in
    `FAKE_SBX_IMAGE_ENV` stand in for what the sandbox image supplies.

    stdin is /dev/null so the pi the launcher runs sees EOF and exits. A real
    terminal is what `-it` attaches, and there is none here.
    """
    image_env = "AGENT_PI_BIN,SESSIOND_SANDBOX_SESSIONS_MOUNT"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
        "AGENT_PI_BIN": os.environ["AGENT_PI_BIN"],
        "SESSIOND_SANDBOX_SESSIONS_MOUNT": str(stack.mounts.sessions),
        "FAKE_SBX_IMAGE_ENV": image_env,
    }

    return subprocess.run(
        [
            sys.executable,
            str(fake_sbx_script()),
            "exec",
            "--env-file",
            str(stack.playpen_env),
            SANDBOX,
            "--",
            "node",
            str(launch_bundle()),
            "--sandbox",
            SANDBOX,
            "--session",
            session,
            *extra,
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=LAUNCH_TIMEOUT_S,
        check=False,
    )


async def run_one_turn(stack: Stack, chat: str) -> None:
    """One non-streamed turn, so the playpen holds a pi process after it."""
    client = stack.client
    assert client is not None, "the stack fixture always serves before it yields"

    response = await client.post(
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("hello", stream=False),
    )

    assert response.status_code == HTTP_OK, response.text


async def test_cv_launcher_refuses_a_held_session(stack: Stack) -> None:
    """§7.6 rule 3. A terminal never joins a session the playpen holds."""
    chat = chat_id()
    session = session_of(chat)
    await run_one_turn(stack, chat)

    await until(lambda: process_record(stack, session).exists(), "the §7.5 record")
    assert read_record(stack, session)["released"] is False

    done = run_launcher(stack, session)

    assert done.returncode == EXIT_SESSION_HELD, done.stderr
    assert session in done.stderr
    # The refusal costs no pi process: it happens before the plan is built.
    assert done.stdout == ""


async def test_cv_launcher_runs_the_pool_line_without_rpc(stack: Stack) -> None:
    """§7.6 rule 1, on the built bundles rather than on one import of one."""
    chat = chat_id()
    session = session_of(chat)
    await run_one_turn(stack, chat)
    await until(lambda: process_record(stack, session).exists(), "the §7.5 record")

    # Free the session the way §7.5 rule 4 describes: the pi process goes and
    # the playpen writes the release. Either alone makes the record stale.
    pid = read_record(stack, session)["pid"]
    assert isinstance(pid, int)
    os.kill(pid, signal.SIGKILL)
    await until(lambda: read_record(stack, session)["released"] is True, "the release")

    before = stack.pi_starts()
    done = run_launcher(stack, session)

    assert done.returncode == 0, done.stderr
    starts = stack.pi_starts()
    assert len(starts) == len(before) + 1, "the launcher started no pi process"

    pool = before[-1].split()
    terminal = starts[-1].split()

    assert pool[:2] == RPC_MODE_ARGS
    assert terminal == pool[2:], "the terminal's line is not the pool's minus --mode rpc"
