"""The fourth topology: the noticeboard beside `attendance`.

    a test (plays the reverse proxy and a browser)
      | HTTP, loopback port, the key in `X-View-Key`
      v
    noticeboard    one process, started through `Service.NOTICEBOARD`
      | reads state/families/<family>/status.json and validation.json
      | reads state/outcomes/ and state/audit/
      | writes one git commit in <root>/registry
      | HTTP over a Unix socket, the `view-ro` token
      v
    attendance and the stand-ins      `proc_stack.py`

Two families are in the root: the attended family of every topology, and one
autonomous family. Each has a status document and a family file. The registry
is a git repository with one commit (`proc_registry.py`).

No `caregiver` runs here. A scenario that saves a family proves the commit
and nothing after it: on the host `caregiver` reads the registry and
converges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx
import proc_html
import proc_registry
from proc_harness import LOOPBACK, Child, ProcError
from proc_html import Element
from proc_services import Service
from proc_stack import CLIENT_TIMEOUT, Stack
from proc_tree import AUTONOMOUS, FAMILY, LAN_ADDRESS, VIEW_KEY, Tree, add_family, write_view_key

#: The autonomous family of the fixture. The name is the one the old stage 5
#: suite uses.
REVIEW: Final = "ha-review"

#: The header that the reverse proxy adds (`docs/rework/spec.md` §8.3).
KEY_HEADER: Final = "X-View-Key"

#: The cookie and the hidden field that carry the CSRF token (§8.3 rule 3).
CSRF_COOKIE: Final = "view_csrf"
CSRF_FIELD: Final = "csrf_token"

#: The two buttons of the edit form. A button posts its value under `verb`.
VERB_FIELD: Final = "verb"
VERB_PREVIEW: Final = "preview"
VERB_SAVE: Final = "save"

HTML_TYPE: Final = "text/html"
HTTP_OK: Final = 200


def board_env(tree: Tree, host: str, port: int) -> dict[str, str]:
    """The variables `creche-noticeboard.service` reads from its two files.

    The unit binds the LAN address of the site file on the default port.
    No test binds that, so the bind and the port are the two differences.
    The key comes from a file of mode 0600, as the unit says it can.
    """
    return {
        "AGENT_LAN_ADDRESS": LAN_ADDRESS,
        "VIEW_BIND": host,
        "VIEW_PORT": str(port),
        "VIEW_ACCESS_KEY_FILE": str(tree.view_key_file),
        "VIEW_STATE_ROOT": str(tree.state_root),
        "VIEW_REGISTRY_DIR": str(tree.registry_root),
        "VIEW_SESSIOND_SOCKET": str(tree.attendance_socket),
    }


def _env_for_bind(tree: Tree, bind: str) -> dict[str, str]:
    host, _, port = bind.rpartition(":")

    return board_env(tree, host, int(port))


@dataclass(slots=True)
class BoardStack(Stack):
    """The noticeboard and `attendance`, over two families and one registry."""

    board: Child | None = None
    board_port: int = 0

    def prepare(self) -> None:
        """The tree of the first topology, one more family, the key and the registry."""
        Stack.prepare(self)
        add_family(self.tree, REVIEW, AUTONOMOUS)
        write_view_key(self.tree)
        proc_registry.write_attended(self.tree, FAMILY)
        proc_registry.write_autonomous(self.tree, REVIEW)
        proc_registry.commit_all(self.tree)

    def start(self) -> None:
        """Start both services, then wait for both."""
        self.spawn_attendance()
        self.start_board()
        self.await_attendance()

    def start_board(self) -> None:
        """Start the noticeboard alone. It reads files, and it dials at each page."""
        self.board, self.board_port = self.start_on_port(
            Service.NOTICEBOARD, lambda bind: _env_for_bind(self.tree, bind)
        )

    @property
    def origin(self) -> str:
        """Where a browser finds the noticeboard: the scheme, the host and the port."""
        return f"http://{self.host}"

    @property
    def host(self) -> str:
        """The `Host` header a browser sends."""
        return f"{LOOPBACK}:{self.board_port}"

    def client(self, key: str | None = VIEW_KEY) -> httpx.AsyncClient:
        """A client behind the reverse proxy: it adds the key to each request.

        It follows no redirect and keeps no cookie. A scenario reads the
        `Location` header and sends the cookie itself, so each of the two is
        an assertion and not a habit of the client.
        """
        headers = {} if key is None else {KEY_HEADER: key}

        return httpx.AsyncClient(
            base_url=self.origin, headers=headers, timeout=CLIENT_TIMEOUT, follow_redirects=False
        )


async def page_of(client: httpx.AsyncClient, path: str) -> Element:
    """One page that answered 200 with HTML, as a tree."""
    response = await client.get(path)

    if response.status_code != HTTP_OK:
        raise ProcError(f"GET {path} answered {response.status_code}\n{response.text}")

    return html_of(response)


def html_of(response: httpx.Response) -> Element:
    """The tree of one HTML answer. An answer of another type is an error."""
    kind = response.headers.get("content-type", "")

    if not kind.startswith(HTML_TYPE):
        raise ProcError(f"the answer is {kind!r}, not HTML\n{response.text}")

    return proc_html.parse(response.text)


def csrf_of(response: httpx.Response) -> str:
    """The CSRF token of one answer, from its `Set-Cookie` header."""
    token = response.cookies.get(CSRF_COOKIE)

    if not token:
        raise ProcError(f"the answer sets no {CSRF_COOKIE} cookie")

    return token
