"""The third topology: `caregiver` as a process.

    a test (edits a file of the registry)
      v
    caregiver      one process, started through `Service.CAREGIVER`
      | sbx create, sbx policy, sbx rm      the `sbx` stand-in, found through PATH
      | systemctl --user                    the `systemctl` stand-in, found through PATH
      | HTTP, loopback port                 the LiteLLM stand-in
      | writes the status document, the grant file, the credential file,
      | the config mount and the env file of each sandbox
      | runs the stand-in of `chaperone check`, then writes `policy.toml`
      | POST /internal/switch-sandbox over the Unix socket of `attendance`

`caregiver` runs with its three stand-ins. No `attendance` answers the
switch call, so each sandbox stays `creating` (contract 05 §4.2 rule 4).

No file of a family comes from this suite. A test writes the registry, and
`caregiver` publishes each family from it.

The command is the `ExecStart` of `creche-caregiver.service`. Three
arguments are not in the unit:

`--litellm-base-url`
    The unit lets `caregiver` dial LiteLLM on the LAN address of the site
    file, and no test binds that.
`--release-root`
    `caregiver` refuses a state root of its own with the release root of the
    host.
`--poll-interval-s`
    The unit takes the default of 2 seconds. A shorter look makes an edit
    land sooner, and it changes nothing else.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from proc_harness import LOOPBACK, Child, Finished
from proc_services import Service
from proc_stack import Stack, base_env
from proc_standins import (
    MASTER_KEY,
    chaperone_check_program,
    install_chaperone_check,
    install_sbx_verbs,
    install_systemctl,
    start_litellm,
)
from proc_tree import (
    FAMILY,
    IMAGE,
    LAN_ADDRESS,
    Tree,
    build_bare_tree,
    family_body,
    first_sandbox,
    publish_family,
)

#: The reference of the second image flavor (contract 01 §3.9). An obvious
#: fixture, as `IMAGE` is.
PYTHON_IMAGE: Final = "sha256:" + "0" * 63 + "2"

#: The value of `--pep-url` that turns the watch off (contract 05 §2.2 rule 1).
WATCH_OFF: Final = ""

#: How often `caregiver` looks at the registry in this suite, in seconds.
POLL_INTERVAL_S: Final = "0.2"

#: Every verb that changes something prints its plan. This flag makes it act.
WRITE_FLAG: Final = "--write"

#: The two endpoints that `caregiver` allows for each sandbox (contract 05
#: §4.3 step 3): LiteLLM and the chaperone, on the LAN address of the site.
PLANE_ENDPOINTS: Final = (f"{LAN_ADDRESS}:4000", f"{LAN_ADDRESS}:8300")

#: The two plane variables of the env file of a sandbox, each with its URL:
#: the endpoint of the plane behind `http://`.
PLANE_URLS: Final = {
    "AGENT_PEP_URL": f"http://{PLANE_ENDPOINTS[1]}",
    "AGENT_LITELLM_BASE_URL": f"http://{PLANE_ENDPOINTS[0]}",
}

#: The three hosts that each sandbox must not reach (contract 05 §4.3 step 2).
CANARIES: Final = ("example.com:443", "openrouter.ai", f"{LAN_ADDRESS}:8181")

# The family states of contract 05 §3, and the sandbox states of §4.2.
IN_SYNC: Final = "in_sync"
RECONCILING: Final = "reconciling"
INVALID: Final = "invalid"
DEGRADED: Final = "degraded"
CREATING: Final = "creating"
READY: Final = "ready"

#: The exit codes that `caregiver/AGENTS.md` gives each verb.
EXIT_OK: Final = 0
EXIT_PROBLEM: Final = 1
EXIT_USAGE: Final = 2

#: How long a first pass may take: one start of the service, about ten `sbx`
#: commands and one handshake.
CONVERGE_DEADLINE_S: Final = 60.0

_POLL_S: Final = 0.02


def caregiver_env() -> dict[str, str]:
    """The variables `creche-caregiver.service` reads from its two files.

    The unit also puts `AGENT_SANDBOX_IMAGE`, `AGENT_SANDBOX_PYTHON_IMAGE`
    and `AGENT_PEP_URL` into its command line. systemd does that, so here
    each value is an argument of `serve_words`.
    """
    return {"AGENT_LAN_ADDRESS": LAN_ADDRESS, "LITELLM_MASTER_KEY": MASTER_KEY}


def watch_flags(tree: Tree, litellm_port: int, chaperone: list[str] | None = None) -> list[str]:
    """The flags that `serve` and `reconcile-once` share, as the unit gives them.

    `chaperone` replaces the flags of `chaperone_flags`.
    """
    return [
        "--image",
        IMAGE,
        "--image-python",
        PYTHON_IMAGE,
        "--state-root",
        str(tree.state_root),
        "--sessiond-socket",
        str(tree.attendance_socket),
        "--litellm-base-url",
        litellm_url(litellm_port),
        *(chaperone_flags(tree) if chaperone is None else chaperone),
    ]


def chaperone_flags(tree: Tree) -> list[str]:
    """Where `caregiver` writes `policy.toml`, and the stand-in that checks it."""
    return [
        "--chaperone-config-dir",
        str(tree.chaperone_config_dir),
        "--chaperone-bin",
        str(chaperone_check_program(tree)),
    ]


def serve_words(
    tree: Tree, litellm_port: int, pep_url: str, chaperone: list[str] | None = None
) -> list[str]:
    """The words after the program in the `ExecStart` of the unit.

    The module docstring names the three arguments that the unit does not
    have. `chaperone` is as `watch_flags` takes it.
    """
    return [
        "serve",
        str(tree.registry_root),
        *watch_flags(tree, litellm_port, chaperone),
        "--pep-url",
        pep_url,
        "--release-root",
        str(tree.release_root),
        "--poll-interval-s",
        POLL_INTERVAL_S,
        WRITE_FLAG,
    ]


def litellm_url(port: int) -> str:
    """Where the LiteLLM stand-in of a test answers."""
    return f"http://{LOOPBACK}:{port}"


@dataclass(slots=True)
class CaregiverStack(Stack):
    """`caregiver` and its stand-ins."""

    caregiver: Child | None = None
    litellm: Child | None = None
    litellm_port: int = 0

    # ------------------------------------------------------------------ start

    def prepare(self) -> None:
        """Write the tree, the registry and each stand-in program. Start nothing.

        The registry holds one attended family. No status document, no grant
        file and no credential file exists: `caregiver` writes each one.
        """
        build_bare_tree(self.tree)
        self.tree.release_root.mkdir(parents=True, exist_ok=True)
        publish_family(self.tree, family_body())
        install_sbx_verbs(self.tree)
        install_systemctl(self.tree)
        install_chaperone_check(self.tree)

    def start_litellm(self) -> None:
        """Start the LiteLLM stand-in on a free loopback port, and wait for it."""
        self.litellm, self.litellm_port = start_litellm(
            self.tree, self.supervisor, base_env(self.tree)
        )

    def spawn_caregiver(self, *more: str) -> Child:
        """Start `caregiver serve`, with the watch off.

        `more` adds arguments after those of the unit.
        """
        words = [*serve_words(self.tree, self.litellm_port, WATCH_OFF), *more]
        self.caregiver = self.spawn(Service.CAREGIVER, caregiver_env(), *words)

        return self.caregiver

    def run_caregiver(self, *words: str, env: dict[str, str] | None = None) -> Finished:
        """Run one verb of `caregiver` to its end."""
        return self.run(Service.CAREGIVER, caregiver_env() if env is None else env, *words)

    def await_published(self, family: str = FAMILY) -> None:
        """Wait until `caregiver` alone ends its first pass for one family.

        The family is `in_sync` and its sandbox is `creating`: no `attendance`
        ran the handshake.
        """
        box = first_sandbox(family)
        wait_until(
            lambda: self.state(family) == IN_SYNC and self.sandboxes(family) == [(box, CREATING)],
            f"{family} to be {IN_SYNC} with {box} {CREATING}",
        )

    # -------------------------------------------------- what crossed a boundary

    def credentials(self, family: str = FAMILY) -> dict[str, Any]:
        """The credential file of one family, as its sandbox reads it."""
        document: dict[str, Any] = _read_json(self.tree.creds_file(family))

        return document

    def grants(self, family: str = FAMILY) -> dict[str, Any]:
        """The grant file of one family."""
        document: dict[str, Any] = _read_json(self.tree.grant_file(family))

        return document

    def sandboxes(self, family: str = FAMILY) -> list[tuple[str, str]]:
        """Each sandbox of the status document, as its id and its state."""
        document = self.tree.status(family)

        if document is None:
            return []

        return [(str(row["id"]), str(row["state"])) for row in document["sandboxes"]]

    def state(self, family: str = FAMILY) -> str | None:
        """The state of one family in its status document, or None with no document."""
        document = self.tree.status(family)

        return None if document is None else str(document["state"])

    def sbx_commands(self) -> list[tuple[str, ...]]:
        """The arguments of each `sbx` command that is not `exec`, in call order.

        `caregiver` runs each of these. `attendance` runs `exec`.
        """
        return [call.argv for call in self.sbx_calls() if call.argv[:1] != ("exec",)]

    def dialled(self) -> list[str]:
        """The sandbox of each `sbx exec`, in call order."""
        return [
            call.value_after("--sandbox") for call in self.sbx_calls() if call.argv[:1] == ("exec",)
        ]


def text_of(path: Path) -> str | None:
    """The text of one file of a mount, or None while the file is not there.

    `caregiver` puts a new config mount in place with two renames. Between
    the two, the path of the mount does not exist. So a test that reads a
    file of the mount in a wait must accept that no file is there.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def wait_until(
    check: Callable[[], bool], what: str, deadline_s: float = CONVERGE_DEADLINE_S
) -> None:
    """Poll until a condition holds, or say what never happened.

    For a fixture and for a test with no event loop. A test with a request
    in flight uses `proc_chat.until`, which lets the request run.
    """
    deadline = time.monotonic() + deadline_s

    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {deadline_s} s waiting for {what}")

        time.sleep(_POLL_S)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
