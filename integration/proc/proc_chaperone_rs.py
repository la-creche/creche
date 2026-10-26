"""The Rust chaperone, with its two effect stand-ins and no `attendance`.

    a test (plays the agent module of one family)
      | MCP over streamable HTTP, loopback port, the bearer of the family
      v
    chaperone serve      one process, started through `Service.CHAPERONE_RS`
      | MCP over stdio                  | MCP over stdio
      v                                 v
    standin_upstream.py                 standin_approval.py
      `echo`, `gated_echo`              answers each gate from a file

The suite writes every file that the chaperone reads, by chaperone
`docs/design.md` §6 and §7: `install.toml`, `policy.toml`, the plugin tree
and the secrets directory. `chaperone token new` mints the token of each
family and adds its digest to the policy file. `chaperone check` validates
both files before the start. `chaperone verify` checks the running front
door after it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, cast

import httpx
from proc_harness import LOOPBACK, Child, Finished, ProcError
from proc_services import Service
from proc_stack import CLIENT_TIMEOUT, Stack
from proc_standins import APPROVAL, UPSTREAM, effect_program, install_effects
from proc_strict import strict_object
from proc_tree import RS_FAMILIES, RsFiles, build_bare_tree, write_rs_tree

#: The path of the front door (§7.6) and of its health answer.
MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/healthz"

#: The protocol version that the test sends in `initialize`.
MCP_PROTOCOL: Final = "2025-06-18"

HTTP_OK: Final = 200
HTTP_ACCEPTED: Final = 202

_TOKEN: Final = re.compile(r"[0-9a-f]{64}")
_SESSION_HEADER: Final = "Mcp-Session-Id"
_DATA_FIELD: Final = "data:"
_HEALTH_POLLS: Final = 100
_HEALTH_POLL_S: Final = 0.1


def serve_words(files: RsFiles, bind: str) -> list[str]:
    """The words after the program for `chaperone serve` (§7.6, §10)."""
    return [
        "serve",
        "--config-dir",
        str(files.config_dir),
        "--plugins-dir",
        str(files.plugins_dir),
        "--audit-file",
        str(files.audit_file),
        "--secrets-dir",
        str(files.secrets_dir),
        "--admin-socket",
        str(files.admin_socket),
        "--listen",
        bind,
    ]


@dataclass(slots=True)
class ChaperoneRsStack(Stack):
    """The Rust chaperone of one test, the token of each family, and its port."""

    chaperone: Child | None = None
    port: int = 0
    tokens: dict[str, str] = field(default_factory=dict[str, str])
    #: The families that each app instance may ask through the plugin
    #: `attendance`. None leaves the plugin out (`proc_exit.py` sets it).
    delegates: dict[str, tuple[str, ...]] | None = None

    @property
    def files(self) -> RsFiles:
        return RsFiles(self.tree.root)

    def prepare(self) -> None:
        """Write every file, mint one token for each family, then check both files."""
        build_bare_tree(self.tree)
        install_effects(self.tree)
        attendance = (
            (self.tree.attendance_socket, self.delegates) if self.delegates is not None else None
        )
        write_rs_tree(
            self.files,
            effect_program(self.tree, UPSTREAM),
            effect_program(self.tree, APPROVAL),
            attendance,
        )

        for family in RS_FAMILIES:
            self.tokens[family] = self._mint(family)

        for name in ("install.toml", "policy.toml"):
            self._run_ok("check", str(self.files.config_file(name)), *self._plugins())

    def start(self) -> None:
        """Start `chaperone serve`. Wait for `/healthz`, then run `chaperone verify`."""
        self.chaperone, self.port = self.start_on_port(
            Service.CHAPERONE_RS, lambda _: {}, args_for=lambda bind: serve_words(self.files, bind)
        )
        self._await_health()
        report = json.loads(self.verify().stdout)

        if report.get("ok") is not True:
            raise ProcError(f"chaperone verify does not pass: {report}")

    def verify(self) -> Finished:
        """`chaperone verify --json` against the running front door. Exit 0 or an error."""
        listen = f"{LOOPBACK}:{self.port}"
        config = ("--config-dir", str(self.files.config_dir))

        return self._run_ok("verify", "--json", *config, *self._plugins(), "--listen", listen)

    @contextmanager
    def mcp(self, family: str) -> Generator[McpSession]:
        """An MCP session at the front door, with the bearer of one family."""
        headers = {"Authorization": f"Bearer {self.tokens[family]}"}
        url = f"http://{LOOPBACK}:{self.port}{MCP_PATH}"

        with httpx.Client(headers=headers, timeout=CLIENT_TIMEOUT) as client:
            session = McpSession(client, url)
            session.open()

            yield session

    def audit(self) -> list[dict[str, Any]]:
        """Each record of the audit file, in write order (§5.4)."""
        try:
            raw = self.files.audit_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []

        return [strict_object(line) for line in raw.split("\n")[:-1]]

    def _mint(self, family: str) -> str:
        """`chaperone token new`: the token on stdout, its digest in the policy file (§9)."""
        done = self._run_ok("token", "new", family, "--config-dir", str(self.files.config_dir))
        token = done.stdout.strip()

        if _TOKEN.fullmatch(token) is None:
            raise ProcError(f"chaperone token new {family} printed no token of 64 hex characters")

        return token

    def _plugins(self) -> tuple[str, str]:
        return ("--plugins-dir", str(self.files.plugins_dir))

    def _run_ok(self, *args: str) -> Finished:
        done = self.run(Service.CHAPERONE_RS, {}, *args)

        if done.exit_code != 0:
            raise ProcError(f"chaperone {args[0]} exited {done.exit_code}\n{done.stderr}")

        return done

    def _await_health(self) -> None:
        """`/healthz` answers 200 while a valid policy is loaded (§7.6)."""
        url = f"http://{LOOPBACK}:{self.port}{HEALTH_PATH}"

        for _ in range(_HEALTH_POLLS):
            if httpx.get(url, timeout=CLIENT_TIMEOUT).status_code == HTTP_OK:
                return

            time.sleep(_HEALTH_POLL_S)

        raise ProcError(f"{HEALTH_PATH} did not answer {HTTP_OK}")


@dataclass(slots=True)
class McpSession:
    """One MCP session over streamable HTTP: one request in each POST."""

    client: httpx.Client
    url: str
    session_id: str | None = None
    _next_id: int = 0

    def open(self) -> None:
        params = {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "proc-suite", "version": "0"},
        }
        self.request("initialize", params)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool_names(self) -> list[str]:
        tools = cast("list[dict[str, Any]]", self.request("tools/list", {})["tools"])

        return [str(entry["name"]) for entry in tools]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        ident = self._next_id
        answer = self._post({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})

        if self.session_id is None:
            self.session_id = answer.headers.get(_SESSION_HEADER)

        message = _message_of(answer, ident)

        if "error" in message:
            raise ProcError(f"{method} answered an error: {message['error']}")

        return cast("dict[str, Any]", message["result"])

    def _post(self, message: dict[str, Any]) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream"}

        if self.session_id is not None:
            headers[_SESSION_HEADER] = self.session_id

        answer = self.client.post(self.url, json=message, headers=headers)

        if answer.status_code not in (HTTP_OK, HTTP_ACCEPTED):
            raise ProcError(f"POST {MCP_PATH} answered {answer.status_code}\n{answer.text}")

        return answer


def _message_of(answer: httpx.Response, ident: int) -> dict[str, Any]:
    """The JSON-RPC answer with one id, from a JSON body or from an SSE stream.

    The front door starts each stream with an event whose data is empty. A
    client takes no value from it.
    """
    if answer.headers.get("content-type", "").startswith("application/json"):
        return strict_object(answer.text)

    for line in answer.text.split("\n"):
        data = line.removeprefix(_DATA_FIELD).strip() if line.startswith(_DATA_FIELD) else ""

        if data:
            message = strict_object(data)

            if message.get("id") == ident:
                return message

    raise ProcError(f"no answer with the id {ident} in the stream\n{answer.text}")
