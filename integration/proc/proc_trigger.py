"""The fourth topology: the trigger door in front of `attendance`.

    a test (plays an automation on the LAN)      a test (plays a systemd timer)
      | HTTP, loopback port, the webhook bearer    | runs the command to its end
      v                                            v
    door-trigger serve                           door-trigger fire <family>
      | reads the registry and the status documents, both
      | reads state/triggers/webhooks/ (serve), the outcome records (fire)
      | HTTP over a Unix socket, the `door-trigger` token
      v
    attendance and the stand-ins      `proc_stack.py`
      runs one `auto-<ulid>` job in the autonomous family

The door is two commands of one program, as its two units run it:
`creche-trigger-webhooks.service` runs `serve`, and `creche-trigger@.service`
runs `fire <family>` once for each tick of a generated timer. This suite is
the clock: a test decides when the timer fires.

Three families are in the root, one of each kind. The registry holds their
family files (`proc_registry.py`). No `caregiver` runs here, so the suite
writes the status documents and the webhook bearer that `caregiver` writes on
the host.

No chaperone runs here. The quiet check of contract 01 §3.15 calls the
chaperone only for a family that names a board, or that holds `enqueue`. The
family of this fixture does neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx
import proc_registry
from proc_harness import LOOPBACK, Child, Finished
from proc_services import Service
from proc_stack import CLIENT_TIMEOUT, Stack
from proc_tree import (
    AUTONOMOUS,
    FAMILY,
    LAN_ADDRESS,
    THIN,
    Tree,
    add_family,
    webhook_token_of,
)

#: The three families. The names are the ones the old stage 5 suite uses.
REVIEW: Final = "ha-review"
CHAT: Final = FAMILY
ORACLE: Final = "vault-oracle"

#: The one webhook that the autonomous family declares (contract 01 §3.13).
WEBHOOK: Final = "boiler-alert"

#: The two commands of the door, as the two units give them.
SERVE: Final = "serve"
FIRE: Final = "fire"

#: How often the listener reads its routes again. The unit leaves the
#: default of 30 seconds. A scenario that changes a route file gives a
#: shorter one, and a scenario that sends SIGHUP gives a longer one.
REFRESH_S: Final = 30.0

#: Where the quiet check finds the chaperone. No chaperone runs here, and the
#: family of this fixture gives the check no reason to call one.
NO_CHAPERONE_URL: Final = f"http://{LOOPBACK}:9"


def trigger_env(tree: Tree) -> dict[str, str]:
    """What both commands read: where `attendance` is, and the two trees.

    Both units read only the site file. Each other value is a default path
    of the host, so each one is a variable here.
    """
    return {
        "AGENT_LAN_ADDRESS": LAN_ADDRESS,
        "DOOR_TRIGGER_SESSIOND_TOKEN_FILE": str(tree.token_file("door-trigger")),
        "DOOR_TRIGGER_SESSIOND_SOCKET": str(tree.attendance_socket),
        "DOOR_TRIGGER_FAMILIES_DIR": str(tree.families_dir),
        "DOOR_TRIGGER_REGISTRY_ROOT": str(tree.registry_root),
    }


def serve_env(tree: Tree, bind: str, refresh_s: float = REFRESH_S) -> dict[str, str]:
    """The variables of `agent-trigger serve`."""
    return trigger_env(tree) | {
        "DOOR_TRIGGER_BIND": bind,
        "DOOR_TRIGGER_WEBHOOKS_DIR": str(tree.webhooks_dir),
        "DOOR_TRIGGER_REFRESH_S": str(refresh_s),
    }


def fire_env(tree: Tree) -> dict[str, str]:
    """The variables of `agent-trigger fire`."""
    return trigger_env(tree) | {
        "DOOR_TRIGGER_STATE_ROOT": str(tree.state_root),
        "DOOR_TRIGGER_PEP_URL": NO_CHAPERONE_URL,
    }


@dataclass(slots=True)
class TriggerStack(Stack):
    """The trigger door and `attendance`, with one family of each kind."""

    listener: Child | None = None
    listener_port: int = 0

    def prepare(self, quiet: str | None = None) -> None:
        """The tree of the first topology, two more families and the registry.

        `quiet` is the `quiet` mapping of the autonomous family, as YAML
        text. None leaves the field out (contract 01 §3.15).
        """
        Stack.prepare(self)
        add_family(self.tree, REVIEW, AUTONOMOUS, webhooks=(WEBHOOK,))
        add_family(self.tree, ORACLE, THIN)
        proc_registry.write_attended(self.tree, CHAT)
        proc_registry.write_thin(self.tree, ORACLE)
        proc_registry.write_autonomous(self.tree, REVIEW, webhooks=(WEBHOOK,), quiet=quiet)

    def start(self, refresh_s: float = REFRESH_S) -> None:
        """Start `attendance` and the listener, then wait for both."""
        self.spawn_attendance()
        self.start_listener(refresh_s)
        self.await_attendance()

    def start_listener(self, refresh_s: float = REFRESH_S) -> None:
        """Start the listener alone. It dials `attendance` at each firing."""
        self.listener, self.listener_port = self.start_on_port(
            Service.DOOR_TRIGGER, lambda bind: serve_env(self.tree, bind, refresh_s), SERVE
        )

    def fire(self, family: str, *args: str) -> Finished:
        """One tick of a timer: `agent-trigger fire <family>`, run to its end."""
        return self.run(Service.DOOR_TRIGGER, fire_env(self.tree), FIRE, family, *args)

    def automation(self, token: str | None = None) -> httpx.AsyncClient:
        """A client that plays an automation: the bearer of the declared webhook."""
        bearer = token if token is not None else webhook_token_of(REVIEW, WEBHOOK)

        return httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{self.listener_port}",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=CLIENT_TIMEOUT,
        )


def hook_path(family: str = REVIEW, name: str = WEBHOOK) -> str:
    """The route of one webhook (`docs/rework/spec.md` §7.3)."""
    return f"/triggers/{family}/{name}"
