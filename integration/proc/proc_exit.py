"""The exit test of phase 2: every service of the agent side, as processes.

    a test (plays Panda, a browser client of ACP)
      | WebSocket, loopback port, the door bearer
      v
    door-acp                 started through `Service.DOOR_ACP`
      | WebSocket over the Unix socket, the token `door-acp`
      v
    attendance               started through `Service.ATTENDANCE`
      | sbx exec creche-facade   one for each sandbox, over `fake_sbx.py`
      | sbx exec acp-relay       one for each door connection
      v
    creche-facade of `chat` and of `vault-oracle`     the built `facade/dist`
      | openai-completions, the family key ----------> the model stand-in
      | MCP over streamable HTTP, the family token --> chaperone-rs
      v
    chaperone-rs             started through `Service.CHAPERONE_RS`
      | MCP over stdio --> the upstream stand-in: echo, gated_echo
      | MCP over stdio --> the approval stand-in: approve
      | MCP over stdio --> chaperone-attendance --> POST /delegate on the
      |                    socket of attendance, the token `door-delegate`
      v
    chaperone-rs/audit.jsonl

Family `chat` is attended: the door reaches it. Family `vault-oracle` is
thin: the delegate door reaches it. The two app instances of the chaperone
are the two families (chaperone `docs/design.md` §12), and `chat` may ask
`vault-oracle`.

The test plays `caregiver` once: it asks `attendance` to switch `chat` onto
its sandbox, and the switch is what starts a facade (contract 05 §5.3 rule
8). `vault-oracle` gets no switch: its first delegate call starts its
facade.

One model stand-in is the LiteLLM plane of both families. Its script is one
list of turns, taken in arrival order, so a scenario that runs both
families writes the turns of each in the order the calls come.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
from acp_client import AcpClient, AcpError
from proc_chaperone_rs import ChaperoneRsStack
from proc_harness import LOOPBACK, Child, ProcError
from proc_services import Service
from proc_stack import base_env
from proc_standins import SandboxTmp, install_sbx, start_model
from proc_tree import (
    ATTENDED,
    FAMILY,
    FIXTURE_LITELLM_KEY,
    LAN_ADDRESS,
    SECRET_MODE,
    THIN,
    Tree,
    add_family,
    first_sandbox,
    repo_root,
    token_of,
    write_creds,
    write_facade_env,
)

#: Family A: attended, behind the door. Family B: thin, behind the delegate door.
CALLER: Final = FAMILY
TARGET: Final = "vault-oracle"

#: The principal of `attendance` that plays `caregiver` (contract 05 §5).
CAREGIVER: Final = "managerd"

#: The bearer that a client sends to door-acp. An obvious fixture, never a
#: credential. It is not the token `door-acp` of `attendance`.
DOOR_ACP_KEY: Final = "fixture-door-acp-key-" + "k" * 32

#: The path of the switch call (contract 05 §5.1).
SWITCH_PATH: Final = "/internal/switch-sandbox"

#: The programs of the facade package, as the sandbox image puts them on PATH.
FACADE_PROGRAMS: Final = ("creche-facade", "acp-relay")

#: Where a log of each service is, for the text of a failed assertion.
#: `proc_report.py` puts each of these in the report of a failed test.
LOGS: Final = {
    "attendance": "the stderr of `attendance` (proc-logs/NN-attendance.stderr)",
    "facade": "the stderr of each facade and relay (log/playpen-<family>.log)",
    "chaperone": (
        "the stderr of `chaperone-rs` (proc-logs/NN-chaperone-rs.stderr), which "
        "holds the stderr of each effect plugin"
    ),
    "door": "the stderr of door-acp (proc-logs/NN-door-acp.stderr)",
    "model": "the calls of the model stand-in (standins/model-state/calls.jsonl)",
}

HTTP_OK: Final = 200

_POLL_S: Final = 0.05
_WAIT_S: Final = 30.0


def facade_bin(program: str) -> Path:
    """One program of the built facade. `pnpm run build` in `facade/` writes it."""
    return repo_root() / "facade" / "dist" / "src" / "bin" / f"{program}.js"


def explain(*names: str) -> str:
    """The text that a failed assertion carries: the log of each named service."""
    return "read " + "; ".join(LOGS[name] for name in names)


def wait_until[T](found: Callable[[], T | None], what: str, *logs: str) -> T:
    """Poll until `found` gives a value. After the deadline, raise with `what`
    and the log of each service in `logs`."""
    deadline = time.monotonic() + _WAIT_S

    while True:
        value = found()

        if value is not None:
            return value

        if time.monotonic() > deadline:
            raise ProcError(f"no {what} in {_WAIT_S} s. " + explain(*logs))

        time.sleep(_POLL_S)


@dataclass(slots=True)
class ExitStack(ChaperoneRsStack):
    """The topology of the exit test, and what a test may touch of it."""

    model: Child | None = None
    model_port: int = 0
    door: Child | None = None
    door_port: int = 0

    # ------------------------------------------------------------------ start

    def prepare(self) -> None:
        """Every file of the root, and the programs that the sandbox image holds."""
        self.delegates = {CALLER: (TARGET,)}
        ChaperoneRsStack.prepare(self)
        add_family(self.tree, CALLER, ATTENDED)
        add_family(self.tree, TARGET, THIN)
        install_sbx(self.tree, SandboxTmp.OWN)
        _install_facade(self.tree)
        _write_door_key(self.tree)

    def start(self) -> None:
        """Start each service, then switch `chat` onto its sandbox."""
        self.model, self.model_port = start_model(
            self.tree, self.supervisor, base_env(self.tree), FIXTURE_LITELLM_KEY
        )
        ChaperoneRsStack.start(self)

        for family in (CALLER, TARGET):
            write_facade_env(
                self.tree,
                family,
                pep_url=f"http://{LOOPBACK}:{self.port}",
                litellm_url=f"http://{LOOPBACK}:{self.model_port}",
            )
            write_creds(self.tree, family, pep_token=self.tokens[family])

        self.spawn_attendance()
        self.await_attendance()
        self.door, self.door_port = self.start_on_port(
            Service.DOOR_ACP, lambda bind: door_env(self.tree, bind)
        )
        self.switch_on(CALLER)

    def switch_on(self, family: str) -> None:
        """Play `caregiver`: switch a family with no sandbox onto its first one."""
        body = {
            "family": family,
            "to": first_sandbox(family),
            "mode": "drain",
            "reason": "the exit test publishes the family",
        }

        with httpx.Client(
            base_url="http://attendance",
            transport=httpx.HTTPTransport(uds=str(self.tree.attendance_socket)),
            headers={"Authorization": f"Bearer {token_of(CAREGIVER)}"},
            timeout=httpx.Timeout(5.0, read=90.0),
        ) as client:
            answer = client.post(SWITCH_PATH, json=body)

        if answer.status_code != HTTP_OK:
            raise ProcError(
                f"the switch of {family} answered {answer.status_code}: {answer.text}. "
                + explain("attendance", "facade")
            )

    # ---------------------------------------------------------------- clients

    @contextmanager
    def client(self, family: str = CALLER) -> Generator[AcpClient]:
        """A WebSocket to the door, as Panda opens it, after `initialize`."""
        url = f"ws://{LOOPBACK}:{self.door_port}/acp/{family}"

        try:
            with AcpClient.connect(url, DOOR_ACP_KEY) as client:
                client.initialize()

                yield client
        except AcpError as error:
            raise AcpError(f"{error}. " + explain("door", "attendance", "facade")) from error

    def cwd(self, family: str = CALLER) -> str:
        """The working directory a client names: the sessions mount of the family."""
        return str(self.tree.mounts(family).sessions)

    # -------------------------------------------------- what crossed a boundary

    def audit_events(self, tool: str) -> list[str]:
        """The event of each audit record of one exposed tool, in write order."""
        return [str(record["event"]) for record in self.audit() if record.get("tool") == tool]


def door_env(tree: Tree, bind: str) -> dict[str, str]:
    """The variables `creche-door-acp.service` sets, with the bind of the test.

    `DOOR_ACP_BIND` is the one difference from the unit: the unit binds the
    LAN address of the site file, and no test binds that. A terminal client
    reaches the door here, so `DOOR_ACP_REQUIRE_AUTHENTIK` is false, as the
    unit allows.
    """
    return {
        "AGENT_LAN_ADDRESS": LAN_ADDRESS,
        "DOOR_ACP_BIND": bind,
        "DOOR_ACP_KEY_FILE": str(_door_key_file(tree)),
        "DOOR_ACP_ATTENDANCE_TOKEN_FILE": str(tree.token_file("door-acp")),
        "DOOR_ACP_ATTENDANCE_SOCKET": str(tree.attendance_socket),
        "DOOR_ACP_REQUIRE_AUTHENTIK": "false",
    }


def _install_facade(tree: Tree) -> None:
    """Put `creche-facade` and `acp-relay` in the root's `bin`, as the image holds them.

    `fake_sbx.py` runs a command with the PATH of `attendance`, so each one
    is found there. Each wrapper ends in `exec`, so the pid that the `sbx`
    record holds is the pid of the Node program.
    """
    for program in FACADE_PROGRAMS:
        path = tree.bin_dir / program
        target = shlex.join(["node", str(facade_bin(program))])
        path.write_text(f'#!/bin/sh\nexec {target} "$@"\n', encoding="utf-8")
        path.chmod(0o755)


def _door_key_file(tree: Tree) -> Path:
    return tree.state_root / "door-acp.key"


def _write_door_key(tree: Tree) -> None:
    path = _door_key_file(tree)
    path.write_text(DOOR_ACP_KEY + "\n", encoding="utf-8")
    path.chmod(SECRET_MODE)


def json_of(text: str) -> dict[str, Any]:
    """The JSON object of one tool result text."""
    value: object = json.loads(text)

    if not isinstance(value, dict):
        raise ProcError(f"a tool result that is no JSON object: {text}")

    return value  # pyright: ignore[reportUnknownVariableType]
