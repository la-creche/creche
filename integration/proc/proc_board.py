"""The fifth topology: the noticeboard beside `attendance`.

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

The verify hook of the noticeboard is a second program, started through
`Service.NOTICEBOARD_VERIFY`. The release executor runs it to its end
(contract 06 §4). It reads the env file of the unit, and it asks the
noticeboard for `/healthz`.
"""

from __future__ import annotations

import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import proc_html
import proc_registry
from proc_harness import LOOPBACK, STOP_GRACE_S, Child, Finished, ProcError, TcpAddress
from proc_html import Element, form_values
from proc_services import Service, command_of
from proc_stack import CLIENT_TIMEOUT, Stack
from proc_tree import (
    AUTONOMOUS,
    FAMILY,
    LAN_ADDRESS,
    VIEW_KEY,
    Tree,
    add_family,
    write_env_file,
    write_view_key,
)

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

#: A browser sends each line end of a form value as CR LF (the HTML
#: standard, "Converting an entry list to a list of name-value pairs"). A
#: page gives the text of a text area with another line end.
_LINE_END: Final = re.compile(r"\r\n|\r|\n")
_POSTED_LINE_END: Final = "\r\n"

#: The variable of the site file. The unit reads that file beside its own
#: env file, and the verify hook does not.
SITE_ADDRESS_ENV: Final = "AGENT_LAN_ADDRESS"

#: The two flags of `verify.command` in `noticeboard/component.yaml`.
VERIFY_JSON: Final = "--json"
VERIFY_ENV_FILE: Final = "--env-file"


def board_env(tree: Tree, host: str, port: int) -> dict[str, str]:
    """The variables `creche-noticeboard.service` reads from its two files.

    The unit binds the LAN address of the site file on the default port.
    No test binds that, so the bind and the port are the two differences.
    The key comes from a file of mode 0600, as the unit says it can.
    """
    return {
        SITE_ADDRESS_ENV: LAN_ADDRESS,
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


def unit_file_env(tree: Tree, port: int) -> dict[str, str]:
    """The variables of the env file that the unit names, for a loopback bind.

    The unit reads two files. The verify hook reads only this one
    (contract 06 §4 rule 7), so the variable of the site file is not here.
    """
    env = board_env(tree, LOOPBACK, port)
    del env[SITE_ADDRESS_ENV]

    return env


def verify_words(env_file: Path) -> tuple[str, ...]:
    """The words after the program in `verify.command` of the manifest.

    `test_proc_table.py` holds them against `noticeboard/component.yaml`.
    """
    return (VERIFY_JSON, VERIFY_ENV_FILE, str(env_file))


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

    def restart_board(self) -> None:
        """Stop the noticeboard as its unit does, then start it on the same root and port.

        The stop has the time that the teardown gives a group, which is the
        `TimeoutStopSec` of the unit. The first process ended before the
        second one starts. So one process at most holds the registry, and the
        teardown finds one group.
        """
        if self.board is None:
            raise ProcError("the noticeboard was not started")

        self.board.send(signal.SIGTERM)
        self.board.wait(STOP_GRACE_S)
        env = board_env(self.tree, LOOPBACK, self.board_port)
        self.board = self.spawn(Service.NOTICEBOARD, env)
        self.supervisor.wait_ready(self.board, TcpAddress(self.board_port))

    def write_unit_file(self, values: Mapping[str, str] | None = None) -> Path:
        """Write the env file of the unit, and return its path.

        None gives the variables of `unit_file_env` for the port of the
        noticeboard of this test.
        """
        chosen = unit_file_env(self.tree, self.board_port) if values is None else values
        write_env_file(self.tree.view_env_file, chosen)

        return self.tree.view_env_file

    def verify(self, env_file: Path, env: Mapping[str, str] | None = None) -> Finished:
        """Run the verify hook to its end, with the words of the manifest.

        `env` is the whole environment of the hook. None gives the base
        environment of a service: the release executor gives a hook a small
        environment with no variable of its unit (contract 06 §4 rule 7).
        """
        words = verify_words(env_file)

        if env is None:
            return self.run(Service.NOTICEBOARD_VERIFY, {}, *words)

        command = command_of(Service.NOTICEBOARD_VERIFY)
        name = Service.NOTICEBOARD_VERIFY.value

        return self.supervisor.run(name, [*command.words, *words], env, self.tree.root)

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


class Browser:
    """One open edit form: its fields, its cookie, and how it posts."""

    def __init__(self, stack: BoardStack, client: httpx.AsyncClient, family: str) -> None:
        self.stack = stack
        self.client = client
        self.path = f"/families/{family}/edit"
        self.values: dict[str, str] = {}
        self.cookie = ""

    async def open(self) -> None:
        response = await self.client.get(self.path)
        assert response.status_code == httpx.codes.OK, response.text
        self.values = form_values(html_of(response).one("form"))
        self.cookie = csrf_of(response)

    def sender(self) -> dict[str, str]:
        """What a browser on the page of the form sends: its cookie and its origin."""
        return {"Cookie": f"{CSRF_COOKIE}={self.cookie}", "Origin": self.stack.origin}

    async def post(self, verb: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """Click one button. `headers` takes the place of what a browser sends.

        Each line end of a value goes out as CR LF, as from a browser.
        """
        sent = self.sender() if headers is None else headers
        fields = self.values | {VERB_FIELD: verb}
        data = {name: _LINE_END.sub(_POSTED_LINE_END, value) for name, value in fields.items()}

        return await self.client.post(self.path, data=data, headers=sent)


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
