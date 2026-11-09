"""The third topology: `caregiver` as a process.

    a test (edits a file of the registry)
      v
    caregiver      one process, started through `Service.CAREGIVER`
      | POST /drop over vmm.sock            the vmm stand-in, at a family delete
      | systemctl --user                    the `systemctl` stand-in, found through PATH
      | HTTP, loopback port                 the LiteLLM stand-in
      | writes the status document, the credential file, the config
      | directory, `egress.json` and `repos.json` of each family
      | runs the stand-in of `chaperone check`, then writes `policy.toml`

`caregiver` runs with its four stand-ins. It starts no VM: the vmm starts
one VM per session (decisions CH1, CH9). The one call of `caregiver` to the
vmm is the drop of a deleted family (decision CH21-10, contract 05 §4.4).

No file of a family comes from this suite. A test writes the registry, and
`caregiver` publishes each family from it.

The command is the `ExecStart` of `creche-caregiver.service`. Two
arguments are not in the unit:

`--litellm-base-url`
    The unit lets `caregiver` dial LiteLLM on the LAN address of the site
    file, and no test binds that.
`--poll-interval-s`
    The unit takes the default of 2 seconds. A shorter look makes an edit
    land sooner, and it changes nothing else.
"""

from __future__ import annotations

import json
import time
import tomllib
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
    install_systemctl,
    start_litellm,
    start_vmm,
    vmm_calls,
)
from proc_tree import (
    FAMILY,
    LAN_ADDRESS,
    Tree,
    build_bare_tree,
    family_body,
    publish_family,
)

#: The value of `--pep-url` that turns the watch off (contract 05 §2.2 rule 1).
WATCH_OFF: Final = ""

#: How often `caregiver` looks at the registry in this suite, in seconds.
POLL_INTERVAL_S: Final = "0.2"

#: Every verb that changes something prints its plan. This flag makes it act.
WRITE_FLAG: Final = "--write"

# The family states of contract 05 §3.
IN_SYNC: Final = "in_sync"
RECONCILING: Final = "reconciling"
INVALID: Final = "invalid"
DEGRADED: Final = "degraded"

#: The exit codes that `caregiver/AGENTS.md` gives each verb.
EXIT_OK: Final = 0
EXIT_PROBLEM: Final = 1
EXIT_USAGE: Final = 2

#: How long a first pass may take: one start of the service, the LiteLLM
#: calls and one `chaperone check`.
CONVERGE_DEADLINE_S: Final = 60.0

_POLL_S: Final = 0.02

#: The fences of the test host, as the site file gives them.
FIXTURE_MOUNT_ROOTS: Final = ",".join(
    (
        "/srv/agents/vault",
        "/srv/agents/code",
        "/srv/agents/state/index",
        "/srv/agents/work/projects",
        "/srv/agents/work/coding-sandbox",
    )
)
FIXTURE_PLATFORM_FAMILIES: Final = "code"


def caregiver_env(tree: Tree) -> dict[str, str]:
    """The variables `creche-caregiver.service` reads from its two files,
    and the one that it sets itself.

    The unit also puts `AGENT_PEP_URL` into its command line. systemd does
    that, so here the value is an argument of `serve_words`. The two fences
    come from the site file, as on the host: the roots and the platform
    family that the fixture registries use. The unit sets
    `CAREGIVER_VMM_SOCKET` from `%t` (decision CH20). Here it names the
    socket of the vmm stand-in.
    """
    return {
        "AGENT_LAN_ADDRESS": LAN_ADDRESS,
        "LITELLM_MASTER_KEY": MASTER_KEY,
        "AGENT_MOUNT_ROOTS": FIXTURE_MOUNT_ROOTS,
        "AGENT_PLATFORM_FAMILIES": FIXTURE_PLATFORM_FAMILIES,
        "CAREGIVER_VMM_SOCKET": str(tree.vmm_socket),
    }


def watch_flags(tree: Tree, litellm_port: int, chaperone: list[str] | None = None) -> list[str]:
    """The flags that `serve` and `reconcile-once` share, as the unit gives them.

    `chaperone` replaces the flags of `chaperone_flags`.
    """
    return [
        "--state-root",
        str(tree.state_root),
        "--litellm-base-url",
        litellm_url(litellm_port),
        *(chaperone_flags(tree) if chaperone is None else chaperone),
    ]


def chaperone_flags(tree: Tree) -> list[str]:
    """Where `caregiver` writes `policy.toml`, and the stand-in that checks it.

    The unit passes no `--chaperone-bin`: on the host the default is the
    released tree. The suite names its stand-in, because the test has no
    such tree."""
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

    The module docstring names the two arguments that the unit does not
    have. `chaperone` is as `watch_flags` takes it.
    """
    return [
        "serve",
        str(tree.registry_root),
        *watch_flags(tree, litellm_port, chaperone),
        "--pep-url",
        pep_url,
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

        The registry holds one attended family. No status document and no
        credential file exists: `caregiver` writes each one.
        """
        build_bare_tree(self.tree)
        publish_family(self.tree, family_body())
        install_systemctl(self.tree)
        install_chaperone_check(self.tree)

    def start_litellm(self) -> None:
        """Start the LiteLLM stand-in on a free loopback port, and wait for it."""
        self.litellm, self.litellm_port = start_litellm(
            self.tree, self.supervisor, base_env(self.tree)
        )

    def start_vmm(self) -> None:
        """Start the vmm stand-in on `vmm.sock` of the root, and wait for it."""
        self.vmm = start_vmm(self.tree, self.supervisor, base_env(self.tree))

    def spawn_caregiver(self, *more: str) -> Child:
        """Start `caregiver serve`, with the watch off.

        `more` adds arguments after those of the unit.
        """
        words = [*serve_words(self.tree, self.litellm_port, WATCH_OFF), *more]
        self.caregiver = self.spawn(Service.CAREGIVER, caregiver_env(self.tree), *words)

        return self.caregiver

    def run_caregiver(self, *words: str, env: dict[str, str] | None = None) -> Finished:
        """Run one verb of `caregiver` to its end."""
        return self.run(Service.CAREGIVER, caregiver_env(self.tree) if env is None else env, *words)

    def await_published(self, family: str = FAMILY) -> None:
        """Wait until `caregiver` alone ends its first pass for one family:
        the family is `in_sync`."""
        wait_until(lambda: self.state(family) == IN_SYNC, f"{family} to be {IN_SYNC}")

    # -------------------------------------------------- what crossed a boundary

    def credentials(self, family: str = FAMILY) -> dict[str, Any]:
        """The credential file of one family, as the vmm reads it."""
        document: dict[str, Any] = _read_json(self.tree.creds_file(family))

        return document

    def apps(self) -> dict[str, Any]:
        """Each app table of `policy.toml`, by family. Empty with no file."""
        try:
            text = self.tree.policy_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}

        apps: dict[str, Any] = tomllib.loads(text).get("apps", {})

        return apps

    def state(self, family: str = FAMILY) -> str | None:
        """The state of one family in its status document, or None with no document."""
        document = self.tree.status(family)

        return None if document is None else str(document["state"])

    def dropped(self) -> list[str]:
        """The family of each drop that the vmm stand-in got, in call order."""
        return [
            str(call["body"]["family"]["family"])
            for call in vmm_calls(self.tree)
            if call["route"] == "drop"
        ]


def text_of(path: Path) -> str | None:
    """The text of one file of the config directory, or None while the file
    is not there.

    `caregiver` puts a new config directory in place with two renames.
    Between the two, the path of the directory does not exist. So a test
    that reads a file of it in a wait must accept that no file is there.
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
