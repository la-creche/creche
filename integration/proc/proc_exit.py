"""The exit test: `caregiver` and the Rust chaperone, as processes.

    a test (plays the agent module of one family, and edits the registry)
      | MCP over streamable HTTP, loopback port, the token of the family
      v
    chaperone-rs             started through `Service.CHAPERONE_RS`
      | MCP over stdio --> the upstream stand-in: echo, gated_echo
      | MCP over stdio --> the approval stand-in: approve
      v
    chaperone-rs/audit.jsonl

    caregiver serve          started through `Service.CAREGIVER`
      | reads the registry: both family files and the server file `standin`
      | HTTP, loopback port --> the LiteLLM stand-in: the key of each family
      | writes creds.json and one fragment of each family, runs `chaperone
      | check` of chaperone-rs on the joined text, then writes
      | chaperone-rs/config/policy.toml

Family `chat` is attended. Family `vault-oracle` is thin. The two app
instances of the chaperone are the two families (chaperone `docs/design.md`
§12), and `chat` may ask `vault-oracle`.

`caregiver` writes every file of a family: the credential file with a token
of 64 hex characters and the table of the family in `policy.toml`. The
chaperone serves that policy file, and the test writes none.

The six scenarios of phase 2 (plan row 30) left this topology at the Cloud
Hypervisor cut (plan PR CR1). Each one ran a turn of the built facade
through `attendance` and `sbx exec`. After the cut the vmm starts the
facade in a VM, one per session (decisions CH1, CH9), and the facade reads
the fixed guest paths of the VM (decisions CH80, CH81). No process of this
suite can start that VM. The kvm job runs a turn of the real facade in a VM
(`rust/crates/vmm/tests/kvm_facade.rs`).
"""

from __future__ import annotations

import hashlib
import json
import signal
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from proc_caregiver import (
    WATCH_OFF,
    WRITE_FLAG,
    caregiver_env,
    litellm_url,
    serve_words,
)
from proc_chaperone_rs import ChaperoneRsStack
from proc_harness import Child, Finished, ProcError
from proc_services import Service, command_of
from proc_stack import base_env
from proc_standins import install_systemctl, start_litellm, start_vmm
from proc_tree import (
    ATTENDED,
    FAMILY,
    RS_UPSTREAM,
    THIN,
    family_body,
    publish_family,
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

#: Where a log of each service is, for the text of a failed assertion.
#: `proc_report.py` puts each of these in the report of a failed test.
LOGS: Final = {
    "chaperone": (
        "the stderr of `chaperone-rs` (proc-logs/NN-chaperone-rs.stderr), which "
        "holds the stderr of each effect plugin"
    ),
    "caregiver": "the stderr of `caregiver` (proc-logs/NN-caregiver.stderr)",
}

_POLL_S: Final = 0.05
_WAIT_S: Final = 30.0

#: How often `ExitStack.await_pass` sends `caregiver serve` a new `SIGHUP`.
#: One pass of the two families takes less than this on an idle machine.
LOOK_EVERY_S: Final = 1.0


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

    litellm: Child | None = None
    litellm_port: int = 0
    caregiver: Child | None = None

    # ------------------------------------------------------------------ start

    def prepare(self) -> None:
        """Every file that no service writes.

        The registry holds both families and the server file of the upstream
        stand-in. The plugin `attendance` of the chaperone names the socket
        of `attendance`. No scenario calls it, so no `attendance` runs.
        """
        self.prepare_install(self.tree.attendance_socket)
        write_server_file(self.tree, RS_UPSTREAM, (ECHO_TOOL, GATED_TOOL))
        publish_family(self.tree, caller_body())
        publish_family(self.tree, target_body())
        install_systemctl(self.tree)

    def start(self) -> None:
        """Start each service in the order that the files need.

        1. The LiteLLM and vmm stand-ins start.
        2. `caregiver serve` starts. Its first pass writes each file of each
           family, and `policy.toml`.
        3. The chaperone starts on the `policy.toml` of `caregiver`. It
           refuses to start with no policy file.
        """
        env = base_env(self.tree)
        self.litellm, self.litellm_port = start_litellm(self.tree, self.supervisor, env)
        self.vmm = start_vmm(self.tree, self.supervisor, env)
        words = serve_words(self.tree, self.litellm_port, WATCH_OFF, self.chaperone_flags())
        self.caregiver = self.spawn(Service.CAREGIVER, caregiver_env(self.tree), *words)
        wait_until(lambda: self.policy_apps() >= set(FAMILIES) or None, "policy.toml", "caregiver")
        ChaperoneRsStack.start(self)

        for family in FAMILIES:
            self.tokens[family] = str(self.credentials(family)["pep_token"])

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
            caregiver_env(self.tree),
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

    # -------------------------------------------------- what crossed a boundary

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
