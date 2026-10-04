"""The second topology: the chaperone in front of `attendance`.

    a test (plays the bridge inside the `chat` sandbox)
      | HTTP, loopback port, the family token
      v
    chaperone      one process, started through `Service.CHAPERONE`
      | reads state/grants/<family>.json at each call   contract 04 §1.4
      | POST /delegate over the Unix socket             contract 04 §7.3
      v
    attendance and the stand-ins      `proc_stack.py`
      runs one `job-<ulid>` turn in the thin family

The grant file comes from this suite, by the rules of contract 04 §1. On the
host `caregiver` writes it. No `caregiver` process runs here, so no scenario
of this topology proves that side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx
from proc_harness import LOOPBACK, Child
from proc_services import Service
from proc_stack import CLIENT_TIMEOUT, Stack
from proc_tree import FAMILY, LAN_ADDRESS, THIN, Tree, add_family, pep_token_of, write_grants

#: The caller and its one delegate. The names are the ones the old stage 3
#: suite uses, so one scenario reads the same in both.
CALLER: Final = FAMILY
TARGET: Final = "vault-oracle"

#: The first revision of the caller's grant file. A scenario that rewrites
#: the file gives it another.
FIRST_REV: Final = "01K5J9QW3R7T0ZP4YB2H6N8M1D"


def chaperone_env(tree: Tree, bind: str) -> dict[str, str]:
    """The variables `creche-chaperone.service` sets, for the delegate path.

    `PEP_BIND` is the one difference from the unit: the unit binds the LAN
    address of the site file, and no test binds that. No roster and no
    secret file is named, so the chaperone serves no MCP server.
    """
    return {
        "AGENT_LAN_ADDRESS": LAN_ADDRESS,
        "PEP_BIND": bind,
        "PEP_REWORK_DIR": str(tree.state_root),
        "PEP_AUDIT_DIR": str(tree.root / "pep-audit"),
        "PEP_SESSIOND_SOCKET": str(tree.attendance_socket),
        "PEP_DELEGATE_TOKEN_FILE": str(tree.token_file("door-delegate")),
        "PEP_DISPATCH_TOKEN_FILE": str(tree.token_file("door-dispatch")),
    }


@dataclass(slots=True)
class DelegateStack(Stack):
    """The chaperone and `attendance`, with one caller and one thin family."""

    chaperone: Child | None = None
    chaperone_port: int = 0

    def prepare(self) -> None:
        """The tree of the first topology, a thin family, and one grant file."""
        Stack.prepare(self)
        add_family(self.tree, TARGET, THIN)
        self.grant(delegates=(TARGET,))

    def grant(self, *, delegates: tuple[str, ...], rev: str = FIRST_REV) -> None:
        """Write the caller's grant file again, as one edit of its family file."""
        write_grants(self.tree, CALLER, rev=rev, delegates=delegates)

    def start(self) -> None:
        """Start both services, then wait for both."""
        self.spawn_attendance()
        self.chaperone, self.chaperone_port = self.start_on_port(
            Service.CHAPERONE, lambda bind: chaperone_env(self.tree, bind)
        )
        self.await_attendance()

    def sandbox_client(self, family: str = CALLER) -> httpx.AsyncClient:
        """A client that plays one family's sandbox: the family token."""
        return httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{self.chaperone_port}",
            headers={"Authorization": f"Bearer {pep_token_of(family)}"},
            timeout=CLIENT_TIMEOUT,
        )
