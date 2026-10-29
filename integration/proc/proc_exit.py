"""The exit test: every service of the agent side, as processes, and `caregiver`.

    a test (plays Panda, a browser client of ACP, and edits the registry)
      | WebSocket, loopback port, the door bearer
      v
    door-acp                 started through `Service.DOOR_ACP`
      | WebSocket over the Unix socket, the token `door-acp`
      v
    attendance               started through `Service.ATTENDANCE`
      | sbx exec creche-facade   one for each sandbox, over `standin_sbx.py`
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
      |                    socket of attendance, the token `chaperone-delegate`
      v
    chaperone-rs/audit.jsonl

    caregiver serve          started through `Service.CAREGIVER`
      | reads the registry: both family files and the server file `standin`
      | HTTP, loopback port --> the LiteLLM stand-in: the key of each family
      | sbx create, sbx policy --> the `sbx` stand-in
      | writes creds.json, the env file of each sandbox and one fragment
      | of each family, runs `chaperone check` of chaperone-rs on the
      | joined text, then writes chaperone-rs/config/policy.toml
      | POST /internal/switch-sandbox over the Unix socket of attendance

Family `chat` is attended: the door reaches it. Family `vault-oracle` is
thin: the delegate door reaches it. The two app instances of the chaperone
are the two families (chaperone `docs/design.md` §12), and `chat` may ask
`vault-oracle`.

`caregiver` writes every file of a family: the credential file with a token
of 64 hex characters, the env file of each sandbox and the table of the
family in `policy.toml`. The chaperone serves that policy file, and the
test writes none. The switch of `chat` onto its sandbox starts its facade
(contract 05 §5.3 rule 8). The first delegate call to `vault-oracle` starts
its facade.

The env file names the two planes on the LAN address of the site, and the
session store under `/srv/agents/sessions`. The `sbx` stand-in gives the
facade the env file as its VM sees the host (`standin_sbx.py`, rule 5):
the loopback port of each plane, and the session store of the root.

One model stand-in is the LiteLLM plane of both families. Its script is one
list of turns, taken in arrival order, so a scenario that runs both
families writes the turns of each in the order the calls come.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import signal
import time
import tomllib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from acp_client import AcpClient, AcpError
from proc_caregiver import (
    PLANE_URLS,
    READY,
    WATCH_OFF,
    WRITE_FLAG,
    caregiver_env,
    litellm_url,
    serve_words,
)
from proc_chaperone_rs import ChaperoneRsStack
from proc_harness import LOOPBACK, Child, Finished, ProcError
from proc_services import Service, command_of
from proc_stack import base_env
from proc_standins import (
    SBX,
    SandboxTmp,
    install_sbx_verbs,
    install_systemctl,
    set_model_keys,
    set_sbx_view,
    start_litellm,
    start_model,
    tune,
    untune,
)
from proc_tree import (
    ATTENDED,
    FAMILY,
    LAN_ADDRESS,
    RS_UPSTREAM,
    SECRET_MODE,
    THIN,
    Tree,
    family_body,
    first_sandbox,
    publish_family,
    repo_root,
    write_server_file,
)

#: Family A: attended, behind the door. Family B: thin, behind the delegate door.
CALLER: Final = FAMILY
TARGET: Final = "vault-oracle"
FAMILIES: Final = (CALLER, TARGET)

#: The tools of the upstream stand-in, as its server file declares them.
ECHO_TOOL: Final = "echo"
GATED_TOOL: Final = "gated_echo"

#: What `vault-oracle` may call at the start. A scenario adds `gated_echo`.
TARGET_TOOLS: Final = (ECHO_TOOL,)

#: The bearer that a client sends to door-acp. An obvious fixture, never a
#: credential. It is not the token `door-acp` of `attendance`.
DOOR_ACP_KEY: Final = "fixture-door-acp-key-" + "k" * 32

#: The programs of the facade package, as the sandbox image puts them on PATH.
FACADE_PROGRAMS: Final = ("creche-facade", "acp-relay")

#: The session store of the host. Each sandbox mounts its family directory
#: at this same path (contract 03 §7.1).
HOST_SESSIONS: Final = "/srv/agents/sessions/"

#: The tuning of the `sbx` stand-in that holds each `exec` until `start`
#: takes it away.
HOLD_EXEC: Final = "hold-exec"

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
    "caregiver": "the stderr of `caregiver` (proc-logs/NN-caregiver.stderr)",
}

_POLL_S: Final = 0.05
_WAIT_S: Final = 30.0

#: How often `ExitStack.await_pass` sends `caregiver serve` a new `SIGHUP`.
#: One pass of the two families takes less than this on an idle machine.
LOOK_EVERY_S: Final = 1.0


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
            raise ProcError(f"timed out after {_WAIT_S} s waiting for {what}. " + explain(*logs))

        time.sleep(_POLL_S)


def caller_body() -> dict[str, Any]:
    """`chat`: `echo`, `gated_echo` with an approval, and one delegate (contract 01 §2)."""
    return family_body(
        CALLER,
        ATTENDED,
        tools={RS_UPSTREAM: [ECHO_TOOL, GATED_TOOL]},
        approval=[f"{RS_UPSTREAM}__{GATED_TOOL}"],
        delegates=[TARGET],
    )


def target_body(tools: tuple[str, ...] = TARGET_TOOLS) -> dict[str, Any]:
    """`vault-oracle`: the tools of the upstream stand-in that it may call."""
    return family_body(TARGET, THIN, tools={RS_UPSTREAM: list(tools)})


def token_digest(token: str) -> str:
    """The digest of a token in `policy.toml`: SHA-256 of its text (chaperone §9)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ExitStack(ChaperoneRsStack):
    """The topology of the exit test, and what a test may touch of it."""

    model: Child | None = None
    model_port: int = 0
    litellm: Child | None = None
    litellm_port: int = 0
    door: Child | None = None
    door_port: int = 0
    caregiver: Child | None = None

    # ------------------------------------------------------------------ start

    def prepare(self) -> None:
        """Every file that no service writes, and the programs of the sandbox image.

        The registry holds both families and the server file of the upstream
        stand-in. The `sbx` stand-in holds each `exec` until `start` lets it go.
        """
        self.prepare_install(self.tree.attendance_socket)
        write_server_file(self.tree, RS_UPSTREAM, (ECHO_TOOL, GATED_TOOL))
        publish_family(self.tree, caller_body())
        publish_family(self.tree, target_body())
        install_sbx_verbs(self.tree, SandboxTmp.OWN)
        install_systemctl(self.tree)
        tune(self.tree, SBX, HOLD_EXEC)
        _install_facade(self.tree)
        _write_door_key(self.tree)

    def start(self) -> None:
        """Start each service in the order that the files need.

        1. The model and LiteLLM stand-ins, `attendance` and door-acp start.
        2. `caregiver serve` starts. Its first pass writes each file of each
           family, and `policy.toml`. The switch of `chat` waits at `exec`.
        3. The chaperone starts on the `policy.toml` of `caregiver`. It
           refuses to start with no policy file.
        4. The model takes the key of each `creds.json`. `exec` goes on, and
           the facade of `chat` starts.
        """
        env = base_env(self.tree)
        self.model, self.model_port = start_model(self.tree, self.supervisor, env)
        self.litellm, self.litellm_port = start_litellm(self.tree, self.supervisor, env)
        self.spawn_attendance()
        self.await_attendance()
        self.door, self.door_port = self.start_on_port(
            Service.DOOR_ACP, lambda bind: door_env(self.tree, bind)
        )
        words = serve_words(self.tree, self.litellm_port, WATCH_OFF, self.chaperone_flags())
        self.caregiver = self.spawn(Service.CAREGIVER, caregiver_env(), *words)
        wait_until(lambda: self.policy_apps() >= set(FAMILIES) or None, "policy.toml", "caregiver")
        ChaperoneRsStack.start(self)

        for family in FAMILIES:
            self.tokens[family] = str(self.credentials(family)["pep_token"])

        set_model_keys(self.tree, *(str(self.credentials(one)["litellm_key"]) for one in FAMILIES))
        set_sbx_view(
            self.tree,
            {
                PLANE_URLS["AGENT_PEP_URL"]: f"http://{LOOPBACK}:{self.port}",
                PLANE_URLS["AGENT_LITELLM_BASE_URL"]: f"http://{LOOPBACK}:{self.model_port}",
                HOST_SESSIONS: f"{self.tree.sessions_root}/",
            },
        )
        untune(self.tree, SBX, HOLD_EXEC)
        wait_until(
            lambda: self.serves(CALLER) or None, f"{CALLER} {READY}", "caregiver", "attendance"
        )

    def chaperone_flags(self) -> list[str]:
        """Where `caregiver` writes `policy.toml`, and the pinned chaperone that checks it."""
        words = command_of(Service.CHAPERONE_RS).words

        if len(words) != 1:
            raise ProcError("caregiver runs the chaperone by one path: give the row one word")

        return ["--chaperone-config-dir", str(self.files.config_dir), "--chaperone-bin", words[0]]

    def rotate(self, family: str) -> Finished:
        """`caregiver rotate`, graceful by default, run to its end beside `caregiver serve`."""
        return self.run(
            Service.CAREGIVER,
            caregiver_env(),
            "rotate",
            str(self.tree.registry_root),
            family,
            "--state-root",
            str(self.tree.state_root),
            "--litellm-base-url",
            litellm_url(self.litellm_port),
            *self.chaperone_flags(),
            WRITE_FLAG,
        )

    def await_pass[T](self, found: Callable[[], T | None], what: str) -> T:
        """Send `caregiver serve` a `SIGHUP` until `found` gives a value.

        The `ExecReload` of `creche-caregiver.service` sends the same signal.
        `caregiver serve` then starts a pass of each family at its next look,
        and does not wait for the heartbeat of 20 seconds. A pass that runs
        when the signal comes can cancel the forced pass. So the wait sends a
        new signal each `LOOK_EVERY_S`.
        """
        caregiver = self.caregiver

        if caregiver is None:
            raise ProcError("no caregiver serve to signal: call start first")

        sent = time.monotonic() - LOOK_EVERY_S

        def look() -> T | None:
            nonlocal sent
            value = found()

            if value is None and time.monotonic() - sent >= LOOK_EVERY_S:
                caregiver.send(signal.SIGHUP)
                sent = time.monotonic()

            return value

        return wait_until(look, what, "caregiver")

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

    def credentials(self, family: str) -> dict[str, Any]:
        """The credential file that `caregiver` wrote for one family (contract 03 §12)."""
        document: dict[str, Any] = json.loads(
            self.tree.creds_file(family).read_text(encoding="utf-8")
        )

        return document

    def policy(self) -> dict[str, Any]:
        """`policy.toml` as `caregiver` wrote it. An empty table while it has none."""
        try:
            return tomllib.loads(self.files.config_file("policy.toml").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def policy_apps(self) -> set[str]:
        """The app IDs of `policy.toml`."""
        return set(self.policy().get("apps", {}))

    def serves(self, family: str) -> bool:
        """True when the status document puts the first sandbox of the family `ready`."""
        document = self.tree.status(family)

        if document is None:
            return False

        rows = [(str(row["id"]), str(row["state"])) for row in document["sandboxes"]]

        return (first_sandbox(family), READY) in rows


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
        "DOOR_ACP_ACCESS_KEY_FILE": str(_door_key_file(tree)),
        "DOOR_ACP_BEARER_FILE": str(tree.token_file("door-acp")),
        "DOOR_ACP_SOCKET_FILE": str(tree.attendance_socket),
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
