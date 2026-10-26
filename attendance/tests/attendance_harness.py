"""The fixtures of the attendance tests: tokens, a status document, a config.

Every external thing has a fake behind a small protocol, so no test needs
the host, `sbx`, LiteLLM or the PEP. `fake_facade.py` is the far side of a
link.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from attendance.auth import MIN_TOKEN_BYTES, Principal
from attendance.clock import now, rfc3339
from attendance.config import Bind, Config
from attendance.paths import status_file, token_file

FAMILY = "chat"
SANDBOX = "chat-s1"

# Contract 05 §4.1: the status document publishes one env file per sandbox,
# and `attendance` refuses to dial without it. An obvious fixture path: nothing
# in these tests opens the file, because `sbx exec --env-file` is what reads
# it on the real host.
PLAYPEN_ENV = "/srv/agents/state/rework/families/chat/supervisor.env"
CHAT_SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
CONFIG_REV = "reg-9f21c4"
EPOCH = 7
SETTLE_TIMEOUT_S = 2.0

# Contract 01 §3.12's default, mirrored into the status document's limits.
JOB_TIMEOUT_S = 120


def write_tokens(state_root: Path, skip: Principal | None = None) -> dict[Principal, str]:
    """One distinct token per principal, at the mode contract 02 §3 names."""
    made: dict[Principal, str] = {}

    for principal in Principal:
        if principal is skip:
            continue

        value = f"{principal.value}-{'x' * MIN_TOKEN_BYTES}"
        path = token_file(state_root, principal.value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        made[principal] = value

    return made


def write_status(
    state_root: Path,
    family: str = FAMILY,
    kind: str = "attended",
    state: str = "in_sync",
    sandboxes: tuple[tuple[str, str], ...] = ((SANDBOX, "ready"),),
    playpen_env: str = PLAYPEN_ENV,
    never_valid: bool = False,
    faults: tuple[dict[str, Any], ...] = (),
    epoch: int = EPOCH,
    job_timeout_s: int | None = JOB_TIMEOUT_S,
    max_running_turns: int | None = None,
    config_rev: str = CONFIG_REV,
    accepts_dispatch: bool = False,
) -> None:
    """One status document, as `caregiver` writes it (contract 05 §2)."""
    path = status_file(state_root, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    limits: dict[str, Any] = {} if job_timeout_s is None else {"job_timeout_s": job_timeout_s}

    if max_running_turns is not None:
        limits["max_running_turns"] = max_running_turns
    payload: dict[str, Any] = {
        "family": family,
        "kind": kind,
        "state": state,
        "written_at": rfc3339(now()),
        "config_rev": config_rev,
        "validation": {"never_valid": never_valid},
        "faults": list(faults),
        "sandboxes": [
            {"id": box, "state": box_state, "supervisor_env": playpen_env}
            for box, box_state in sandboxes
        ],
        "credentials": {"epoch": epoch},
        "limits": limits,
        # Contract 05 §2.1's triggers block. `enqueue` is what contract 02
        # §13.4.1 rule 3 reads, and a family without it is not dispatchable.
        "triggers": {"webhooks": [], "enqueue": accepts_dispatch},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_config(tmp_path: Path) -> Config:
    """A config rooted entirely inside one temporary directory."""
    return Config(
        sessions_root=tmp_path / "sessions",
        state_root=tmp_path / "state",
        work_root=tmp_path / "work",
        socket_path=tmp_path / "sessiond.sock",
        bind=Bind.SOCKET_ONLY,
        lan_address="192.0.2.10",
        lan_port=8350,
        log_dir=tmp_path / "log",
    )


async def wait_until(test: Callable[[], bool], timeout: float = SETTLE_TIMEOUT_S) -> None:
    """Spin until a condition holds. Used where nothing else signals."""
    deadline = asyncio.get_running_loop().time() + timeout

    while not test():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")

        await asyncio.sleep(0.005)
