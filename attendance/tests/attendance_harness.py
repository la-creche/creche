"""The fixtures of the attendance tests: tokens, a status document, a config.

Every external thing has a fake behind a small protocol, so no test needs
the host, the vmm, LiteLLM or the PEP. `fake_vmm.py` is the vmm, and
`fake_facade.py` is the far side of each stream.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from attendance.acp_jobs import CANCEL_WAIT_S
from attendance.auth import MIN_TOKEN_BYTES, Principal
from attendance.clock import now, rfc3339
from attendance.config import Bind, Config
from attendance.faults import FaultReporter
from attendance.paths import status_file, token_file
from attendance.queueing import MAX_QUEUED_TURNS
from attendance.service import SessionService
from attendance.vmm import Vmm

FAMILY = "chat"
CHAT_SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
CONFIG_REV = "reg-9f21c4"
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
    never_valid: bool = False,
    faults: tuple[dict[str, Any], ...] = (),
    job_timeout_s: int | None = JOB_TIMEOUT_S,
    max_running_turns: int | None = None,
    config_rev: str = CONFIG_REV,
    accepts_dispatch: bool = False,
    caller_root: Path | None = None,
) -> None:
    """One status document, as `caregiver` writes it (contract 05 §2)."""
    path = status_file(state_root, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    limits: dict[str, Any] = {} if job_timeout_s is None else {"job_timeout_s": job_timeout_s}
    if caller_root is not None:
        limits["caller_root"] = str(caller_root)

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
        # `caregiver` still publishes the epoch. `attendance` ignores it.
        "credentials": {"epoch": 7},
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
        socket_path=tmp_path / "attendance.sock",
        bind=Bind.SOCKET_ONLY,
        lan_address="192.0.2.10",
        lan_port=8350,
        vmm_socket=tmp_path / "vmm" / "vmm.sock",
    )


async def wait_until(test: Callable[[], bool], timeout: float = SETTLE_TIMEOUT_S) -> None:
    """Spin until a condition holds. Used where nothing else signals."""
    deadline = asyncio.get_running_loop().time() + timeout

    while not test():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")

        await asyncio.sleep(0.005)


def make_service(
    config: Config,
    vmm: Vmm,
    cancel_wait_s: float = CANCEL_WAIT_S,
    max_queued_turns: int = MAX_QUEUED_TURNS,
) -> SessionService:
    """One service over `vmm`, with the fault file of `config`."""
    faults = FaultReporter(config.state_root)
    return SessionService(config, vmm, faults, cancel_wait_s, max_queued_turns)
