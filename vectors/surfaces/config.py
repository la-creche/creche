"""The configs: the site file, the roster, the mount files and three env readers.

Each surface is one public entry point of a product package that reads or
writes a config. A surface that needs a file gets one in a temporary
directory, and no path of that directory goes into a vector.

Three configs have no entry point that takes a map: `chaperone.__main__`,
`caregiver.cli` and the two doors that read a key file while they parse. No
surface covers them (`vectors/AGENTS.md`, "Known gaps").
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Final

import yaml
from handover.errors import Refusal

from attendance import config as attendance_config
from caregiver import config_mount, credentials, playpen_env
from chaperone import mcp_client, reload_wiring
from chaperone import site as chaperone_site
from handover import site as handover_site
from noticeboard import config as noticeboard_config
from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    normalize,
    quiet_logs,
    raised,
    refused,
    repeat_input,
    text_input,
)

# --- the site file ----------------------------------------------------------

#: The largest count of digits in a text that `int` reads. A zero at the start
#: is a digit of that count.
INT_DIGITS_MAX: Final = 4300

#: No design contract holds the site file. `handover.site` holds its rules.
SITE_CONTRACT: Final = "no contract: the module text of handover.site"

#: The file that a vector's input goes into, under a directory of its own.
SITE_FILE_NAME: Final = "site.env"

#: The mode of the file when a vector names none.
SITE_MODE: Final = 0o600

#: The repository that `github_repo` is asked for.
CATALOG_REPO: Final = "agent-control"

#: What a refusal of the whole file says, and the class a vector records.
_FILE_REFUSALS: Final = (
    ("cannot be read", "unreadable"),
    ("is larger than", "too_large"),
    ("is not KEY=VALUE", "not_key_value"),
    ("is owned by", "owner"),
    ("is writable by", "mode"),
)

#: What a refusal of one key says, and the class a vector records.
_KEY_REFUSALS: Final = (
    ("sets no", "unset"),
    ("is not a value this accepts", "shape"),
    ("names root", "root"),
)

_COMPLETE: Final = (
    "AGENT_GITHUB_OWNER=example-owner\n"
    "AGENT_OPERATOR_USER=operator\n"
    "AGENT_OPERATOR_HOME=/home/operator\n"
    "AGENT_LAN_ADDRESS=192.0.2.10\n"
)


@dataclass(frozen=True)
class SiteDocument:
    """One site file: an id, its bytes and its mode."""

    id: str
    raw: bytes
    mode: int = SITE_MODE
    #: The input form when the bytes are a long text. None means `raw`.
    parts: tuple[tuple[str, int], ...] | None = None

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts is not None else bytes_input(self.raw)


def _site(doc_id: str, text: str, mode: int = SITE_MODE) -> SiteDocument:
    return SiteDocument(doc_id, text.encode("utf-8"), mode)


def _with(doc_id: str, key: str, value: str) -> SiteDocument:
    """The complete file with one more line, which wins its key."""
    return _site(doc_id, f"{_COMPLETE}{key}={value}\n")


def _long(doc_id: str, count: int) -> SiteDocument:
    parts = (("#", count),)

    return SiteDocument(doc_id, ("#" * count).encode("utf-8"), parts=parts)


SITE_DOCUMENTS: Final[tuple[SiteDocument, ...]] = (
    # --- the form of the file ---
    _site("complete", _COMPLETE),
    _site("empty", ""),
    _site("only-comments", "# the site of a test\n\n   \n  # an indented comment\n"),
    _site("spaces-around-the-sign", _COMPLETE.replace("=", " = ")),
    _site("double-quotes", _COMPLETE.replace("=", '="').replace("\n", '"\n')),
    _site("single-quotes", _COMPLETE.replace("=", "='").replace("\n", "'\n")),
    _site("crlf", _COMPLETE.replace("\n", "\r\n")),
    _site("no-final-newline", _COMPLETE.rstrip("\n")),
    _site("last-line-wins", _COMPLETE + "AGENT_LAN_ADDRESS=192.0.2.11\n"),
    _site("other-keys", _COMPLETE + "AGENT_HA_URL=http://192.0.2.20:8123\nOTHER=1\n"),
    _site("export-prefix", _COMPLETE.replace("AGENT_LAN", "export AGENT_LAN")),
    _site("byte-order-mark", "\ufeff" + _COMPLETE),
    _site("line-with-no-sign", _COMPLETE + "not an assignment\n"),
    _site("word-only", "export"),
    _site("sign-only", _COMPLETE + "=\n"),
    _site("empty-key", _COMPLETE + "=value\n"),
    _site("value-with-a-sign", _COMPLETE + "OTHER=a=b\n"),
    _site("line-breaks-of-python", "\x0b".join(_COMPLETE.splitlines()) + "\x85OTHER=1\u2028"),
    _site("unit-separator-is-no-break", _COMPLETE.replace("\n", "\x1f")),
    _site("space-of-unicode", _COMPLETE.replace("=", "=\u00a0\u3000").replace("\n", "\x1f \n")),
    SiteDocument("not-utf8", b"AGENT_LAN_ADDRESS=\xff\n"),
    _long("bytes-65536", 65536),
    _long("bytes-65537", 65537),
    _site("mode-0644", _COMPLETE, 0o644),
    _site("mode-0664", _COMPLETE, 0o664),
    _site("mode-0606", _COMPLETE, 0o606),
    # --- quotes ---
    _with("quotes-not-a-pair", "AGENT_LAN_ADDRESS", "'192.0.2.10\""),
    _with("quotes-two-pairs", "AGENT_LAN_ADDRESS", '""192.0.2.10""'),
    _with("quotes-empty-value", "AGENT_LAN_ADDRESS", '""'),
    _with("quote-alone", "AGENT_LAN_ADDRESS", '"'),
    _with("quotes-keep-inner-space", "AGENT_LAN_ADDRESS", '" 192.0.2.10 "'),
    _with("comment-after-value", "AGENT_LAN_ADDRESS", "192.0.2.10 # the host"),
    # --- the owner ---
    _with("owner-one-char", "AGENT_GITHUB_OWNER", "a"),
    _with("owner-39-chars", "AGENT_GITHUB_OWNER", "a" * 39),
    _with("owner-40-chars", "AGENT_GITHUB_OWNER", "a" * 40),
    _with("owner-inner-hyphens", "AGENT_GITHUB_OWNER", "a-b-c"),
    _with("owner-leading-hyphen", "AGENT_GITHUB_OWNER", "-a"),
    _with("owner-final-hyphen", "AGENT_GITHUB_OWNER", "a-"),
    _with("owner-two-hyphens", "AGENT_GITHUB_OWNER", "a--b"),
    _with("owner-underscore", "AGENT_GITHUB_OWNER", "a_b"),
    _with("owner-upper", "AGENT_GITHUB_OWNER", "Example-Owner"),
    _with("owner-not-ascii", "AGENT_GITHUB_OWNER", "own\u00e9r"),
    _with("owner-empty", "AGENT_GITHUB_OWNER", ""),
    # --- the account ---
    _with("user-root", "AGENT_OPERATOR_USER", "root"),
    _with("user-underscore-first", "AGENT_OPERATOR_USER", "_svc"),
    _with("user-32-chars", "AGENT_OPERATOR_USER", "a" * 32),
    _with("user-33-chars", "AGENT_OPERATOR_USER", "a" * 33),
    _with("user-upper", "AGENT_OPERATOR_USER", "Operator"),
    _with("user-digit-first", "AGENT_OPERATOR_USER", "9a"),
    _with("user-hyphen-first", "AGENT_OPERATOR_USER", "-a"),
    _with("user-dot", "AGENT_OPERATOR_USER", "a.b"),
    # --- the home ---
    _with("home-one-segment", "AGENT_OPERATOR_HOME", "/operator"),
    _with("home-plain-bytes", "AGENT_OPERATOR_HOME", "/_srv/home.d/op-1"),
    _with("home-final-slash", "AGENT_OPERATOR_HOME", "/home/operator/"),
    _with("home-relative", "AGENT_OPERATOR_HOME", "home/operator"),
    _with("home-root", "AGENT_OPERATOR_HOME", "/"),
    _with("home-two-slashes", "AGENT_OPERATOR_HOME", "/home//operator"),
    _with("home-parent-segment", "AGENT_OPERATOR_HOME", "/home/../root"),
    _with("home-hidden-segment", "AGENT_OPERATOR_HOME", "/home/.operator"),
    _with("home-space", "AGENT_OPERATOR_HOME", "/home/the operator"),
    # --- the LAN address ---
    _with("lan-host-name", "AGENT_LAN_ADDRESS", "host-1.example"),
    _with("lan-localhost", "AGENT_LAN_ADDRESS", "localhost"),
    _with("lan-loopback", "AGENT_LAN_ADDRESS", "127.0.0.1"),
    _with("lan-label-63-chars", "AGENT_LAN_ADDRESS", "a" * 63),
    _with("lan-label-64-chars", "AGENT_LAN_ADDRESS", "a" * 64),
    _with("lan-final-dot", "AGENT_LAN_ADDRESS", "host.example."),
    _with("lan-two-dots", "AGENT_LAN_ADDRESS", "host..example"),
    _with("lan-hyphen-first", "AGENT_LAN_ADDRESS", "-host"),
    _with("lan-underscore", "AGENT_LAN_ADDRESS", "my_host"),
    _with("lan-with-port", "AGENT_LAN_ADDRESS", "192.0.2.10:8300"),
    _with("lan-with-scheme", "AGENT_LAN_ADDRESS", "http://192.0.2.10"),
    _with("lan-ipv6", "AGENT_LAN_ADDRESS", "::1"),
    _with("lan-digit-not-ascii", "AGENT_LAN_ADDRESS", "\uff11\uff19\uff12.0.2.10"),
    _with("lan-each-interface", "AGENT_LAN_ADDRESS", "0.0.0.0"),
    _with("lan-one-number", "AGENT_LAN_ADDRESS", "0"),
    _with("lan-three-numbers", "AGENT_LAN_ADDRESS", "192.0.2"),
    _with("lan-number-over-255", "AGENT_LAN_ADDRESS", "192.0.2.256"),
    _with("lan-leading-zero", "AGENT_LAN_ADDRESS", "192.0.2.010"),
    _with("lan-hex-number", "AGENT_LAN_ADDRESS", "0x7f.0.0.1"),
    _with("lan-name-then-number", "AGENT_LAN_ADDRESS", "host.123"),
    # --- the name of a repository ---
    _with("repo-renamed", "AGENT_GITHUB_REPO_AGENT_CONTROL", "creche"),
    _with("repo-plain-bytes", "AGENT_GITHUB_REPO_AGENT_CONTROL", "_a.b-c"),
    _with("repo-100-chars", "AGENT_GITHUB_REPO_AGENT_CONTROL", "a" * 100),
    _with("repo-101-chars", "AGENT_GITHUB_REPO_AGENT_CONTROL", "a" * 101),
    _with("repo-parent", "AGENT_GITHUB_REPO_AGENT_CONTROL", ".."),
    _with("repo-with-slash", "AGENT_GITHUB_REPO_AGENT_CONTROL", "owner/creche"),
    _with("repo-empty", "AGENT_GITHUB_REPO_AGENT_CONTROL", ""),
    _with("repo-key-lower", "agent_github_repo_agent_control", "creche"),
    _with("repo-key-with-hyphen", "AGENT_GITHUB_REPO_AGENT-CONTROL", "creche"),
)

#: The readers of one site file, in the order of a vector's value.
_SITE_READERS: Final[tuple[tuple[str, Callable[[], str]], ...]] = (
    ("github_owner", handover_site.github_owner),
    ("operator_user", handover_site.operator_user),
    ("operator_home", handover_site.operator_home),
    ("lan_address", handover_site.lan_address),
    ("github_repo", lambda: handover_site.github_repo(CATALOG_REPO)),
)


@contextmanager
def _site_file(path: Path) -> Generator[None]:
    """`AGENT_SITE_FILE` names `path`, and the variable is put back."""
    previous = os.environ.get(handover_site.SITE_FILE_ENV)
    os.environ[handover_site.SITE_FILE_ENV] = str(path)
    handover_site.forget()
    try:
        yield
    finally:
        handover_site.forget()
        if previous is None:
            os.environ.pop(handover_site.SITE_FILE_ENV, None)
        else:
            os.environ[handover_site.SITE_FILE_ENV] = previous


def _refusal_class(detail: str, table: tuple[tuple[str, str], ...]) -> str | None:
    """The class of a refusal text, or None for a text outside the table."""
    return next((name for mark, name in table if mark in detail), None)


def _site_vector(document: SiteDocument, scratch: Path) -> Vector:
    given = document.given()
    target = scratch / document.id / SITE_FILE_NAME
    target.parent.mkdir(parents=True)
    target.write_bytes(document.raw)
    target.chmod(document.mode)
    params = {"mode": f"{document.mode:04o}"}
    read: dict[str, object] = {}
    with _site_file(target):
        for name, reader in _SITE_READERS:
            outcome = attempt(reader)
            if not isinstance(outcome, Raised):
                read[name] = {"value": outcome}
                continue

            if not isinstance(outcome.exc, Refusal):
                return raised(document.id, given, outcome.exc, params=params)

            whole_file = _refusal_class(outcome.exc.detail, _FILE_REFUSALS)
            if whole_file is not None:
                return refused(document.id, given, whole_file, params=params)

            one_key = _refusal_class(outcome.exc.detail, _KEY_REFUSALS)
            if one_key is None:
                raise ValueError(f"{document.id}: a refusal text that no table holds")

            read[name] = {"refused": one_key}

    return accepted(document.id, given, read, params=params)


def _site_surface(scratch: Path) -> Surface:
    return Surface(
        name="config.site_file",
        path="config/site_file.json",
        entry="handover.site.read",
        contract=SITE_CONTRACT,
        notes=(
            "The input is the bytes of a site file. The generator writes them to a file with "
            "the mode that params.mode gives, in octal. The account of the generator owns "
            "the file.",
            "A refused vector is a file that no reader uses. refusal is the reason: "
            "unreadable, too_large, not_key_value or mode.",
            "value holds one field for each of five readers: github_owner, operator_user, "
            "operator_home, lan_address and github_repo. A field is an object with value, or "
            "an object with refused. refused is unset, shape or root.",
            f"github_repo is the call github_repo('{CATALOG_REPO}').",
        ),
        vectors=tuple(_site_vector(document, scratch) for document in SITE_DOCUMENTS),
    )


# --- the roster -------------------------------------------------------------

ROSTER_CONTRACT: Final = "stage7-releases.md §4.4"

ROSTER_FILE_NAME: Final = "upstreams.yaml"

_COMMAND: Final = "/opt/mcp/web-search/bin/web-search"


def _row(**fields_: object) -> dict[str, object]:
    return {"command": _COMMAND, **fields_}


def _deny(**fields_: object) -> dict[str, object]:
    return {"tools": ["search"], "arg": "site", "values": ["a"], **fields_}


def _denies(*entries: object) -> dict[str, object]:
    return {"a": _row(arg_denies=list(entries))}


@dataclass(frozen=True)
class Tree:
    """One roster file, as the tree that a YAML reader builds from it."""

    id: str
    document: object


TREES: Final[tuple[Tree, ...]] = (
    # --- whole files ---
    Tree("no-content", None),
    Tree("empty-mapping", {}),
    Tree("one-row", {"web-search": _row()}),
    Tree(
        "full-row",
        {
            "web-search": _row(
                args=["--stdio", "--verbose"],
                env={"SEARCH_API_KEY": "secret:search_api_key", "LOG_LEVEL": "info"},
                tools=["search", "fetch"],
                arg_denies=[
                    _deny(tools=["search", "fetch"], values=["b", "a"]),
                    _deny(arg="branch", values=["main"]),
                ],
            )
        },
    ),
    Tree("three-rows", {"c": _row(), "a": _row(command="a"), "b": _row(args=["x"])}),
    Tree("top-list", [_row()]),
    Tree("top-text", "web-search"),
    Tree("top-number", 7),
    Tree("top-true", True),
    # --- the name of a row ---
    Tree("name-number", {1: _row()}),
    Tree("name-true", {True: _row()}),
    Tree("name-null", {None: _row()}),
    Tree("name-empty", {"": _row()}),
    Tree("name-not-a-server-name", {"Not A Server/Name": _row()}),
    Tree("name-yes-text", {"yes": _row()}),
    # --- the row ---
    Tree("row-null", {"a": None}),
    Tree("row-text", {"a": _COMMAND}),
    Tree("row-list", {"a": [_COMMAND]}),
    Tree("row-empty", {"a": {}}),
    Tree("row-unknown-key", {"a": _row(user="root")}),
    Tree("row-key-number", {"a": {"command": _COMMAND, 1: "x"}}),
    # --- the command ---
    Tree("command-missing", {"a": {"args": []}}),
    Tree("command-null", {"a": _row(command=None)}),
    Tree("command-number", {"a": _row(command=7)}),
    Tree("command-true", {"a": _row(command=True)}),
    Tree("command-list", {"a": _row(command=[_COMMAND])}),
    Tree("command-empty", {"a": _row(command="")}),
    Tree("command-relative", {"a": _row(command="web-search --stdio")}),
    Tree("command-number-text", {"a": _row(command="123")}),
    # --- the arguments ---
    Tree("args-empty", {"a": _row(args=[])}),
    Tree("args-null", {"a": _row(args=None)}),
    Tree("args-text", {"a": _row(args="--stdio")}),
    Tree("args-mapping", {"a": _row(args={"--stdio": ""})}),
    Tree("args-number-item", {"a": _row(args=["--port", 8080])}),
    Tree("args-null-item", {"a": _row(args=[None])}),
    Tree("args-empty-item", {"a": _row(args=["", "a b", "--k=v"])}),
    # --- the variables ---
    Tree("env-empty", {"a": _row(env={})}),
    Tree("env-null", {"a": _row(env=None)}),
    Tree("env-list", {"a": _row(env=["A=1"])}),
    Tree("env-number-value", {"a": _row(env={"PORT": 8080})}),
    Tree("env-true-value", {"a": _row(env={"DEBUG": True})}),
    Tree("env-null-value", {"a": _row(env={"A": None})}),
    Tree("env-number-key", {"a": _row(env={1: "x"})}),
    Tree("env-number-text", {"a": _row(env={"PORT": "8080", "DEBUG": "true"})}),
    Tree("env-secret-forms", {"a": _row(env={"A": "secret:", "B": "secret:a:b", "C": "Secret:x"})}),
    Tree("env-lax-names", {"a": _row(env={"lower case": "x", "": ""})}),
    # --- the declared tools ---
    Tree("tools-empty", {"a": _row(tools=[])}),
    Tree("tools-null", {"a": _row(tools=None)}),
    Tree("tools-text", {"a": _row(tools="search")}),
    Tree("tools-number-item", {"a": _row(tools=[1])}),
    Tree("tools-lax-names", {"a": _row(tools=["", "a tool", "search", "search"])}),
    # --- the fences ---
    Tree("denies-empty", _denies()),
    Tree("denies-null", {"a": _row(arg_denies=None)}),
    Tree("denies-mapping", {"a": _row(arg_denies=_deny())}),
    Tree("denies-text-item", _denies("search")),
    Tree("denies-list-item", _denies([["search"], "site", ["a"]])),
    Tree("denies-one", _denies(_deny())),
    Tree("denies-key-missing", _denies({"tools": ["search"], "arg": "site"})),
    Tree("denies-key-more", _denies(_deny(reason="x"))),
    Tree("denies-key-number", _denies({"tools": ["search"], "arg": "site", 1: ["a"]})),
    Tree("denies-tools-empty", _denies(_deny(tools=[]))),
    Tree("denies-tools-text", _denies(_deny(tools="search"))),
    Tree("denies-tools-number-item", _denies(_deny(tools=[1]))),
    Tree("denies-values-empty", _denies(_deny(values=[]))),
    Tree("denies-values-number-item", _denies(_deny(values=[1]))),
    Tree("denies-values-null", _denies(_deny(values=None))),
    Tree("denies-arg-empty", _denies(_deny(arg=""))),
    Tree("denies-arg-number", _denies(_deny(arg=7))),
    Tree("denies-arg-null", _denies(_deny(arg=None))),
    Tree("denies-repeated-members", _denies(_deny(tools=["b", "a", "b"], values=["x", "x"]))),
    Tree("denies-second-bad", _denies(_deny(), _deny(tools=[]))),
    Tree("second-row-bad", {"a": _row(), "b": _row(args="x")}),
)


def _roster_vector(tree: Tree, scratch: Path) -> Vector:
    given: dict[str, Json] = {"args": {"document": normalize(tree.document)}}
    text = yaml.safe_dump(tree.document)
    if yaml.safe_load(text) != tree.document:
        raise ValueError(f"{tree.id}: the YAML text does not read back as the tree")

    target = scratch / tree.id / ROSTER_FILE_NAME
    target.parent.mkdir(parents=True)
    target.write_text(text, encoding="utf-8")
    outcome = attempt(lambda: mcp_client.load_upstreams(target))
    if not isinstance(outcome, Raised):
        return accepted(tree.id, given, outcome)

    if isinstance(outcome.exc, reload_wiring.UNREADABLE):
        return refused(tree.id, given)

    return raised(tree.id, given, outcome.exc)


def _roster_surface(scratch: Path) -> Surface:
    return Surface(
        name="config.roster",
        path="config/roster.json",
        entry="chaperone.mcp_client.load_upstreams",
        contract=ROSTER_CONTRACT,
        notes=(
            "The input is the tree of one roster file, as args.document. The generator writes "
            "the tree as YAML with yaml.safe_dump and gives the entry point that file. No "
            "vector holds YAML text.",
            "A mapping with a key that is not a text is an $entries marker.",
            "value maps the name of each upstream to its row. The tools and the values of a "
            "fence are sets, so each one is a sorted array.",
            "A refused vector is a file for which the entry point raises an exception that "
            "chaperone.reload_wiring.UNREADABLE holds. A reload catches each one and keeps "
            "the set that serves.",
        ),
        vectors=tuple(_roster_vector(tree, scratch) for tree in TREES),
    )


# --- runtime.json -----------------------------------------------------------

RUNTIME_CONTRACT: Final = "contract 01 §6.1"

_DEFAULT_TOOLS: Final = ("read", "grep", "find", "ls")
_ALL_TOOLS: Final = ("read", "write", "edit", "grep", "find", "ls", "codemode")
_ALIAS: Final = "agent-router"


@dataclass(frozen=True)
class Runtime:
    """One `runtime.json` to write: the fields of `RuntimeConfig`."""

    id: str
    shell: bool = False
    sandbox_tools: tuple[str, ...] = _DEFAULT_TOOLS
    model_alias: str = _ALIAS
    system_prompt: str = config_mount.DEFAULT_SYSTEM_PROMPT


RUNTIMES: Final[tuple[Runtime, ...]] = (
    Runtime("default"),
    Runtime("shell-on", shell=True),
    Runtime("no-tool", sandbox_tools=()),
    Runtime("one-tool", sandbox_tools=("read",)),
    Runtime("each-tool", shell=True, sandbox_tools=_ALL_TOOLS),
    Runtime("tools-in-file-order", sandbox_tools=("ls", "codemode", "read")),
    Runtime("replace", system_prompt="replace"),
    Runtime("alias-each-byte-class", model_alias="0a.b_c/d-e"),
    # A value below is one that the family file check refuses. The writer
    # takes each text.
    Runtime("tool-bash", sandbox_tools=("read", "bash")),
    Runtime("tool-unknown", sandbox_tools=("teleport",)),
    Runtime("tool-two-times", sandbox_tools=("read", "read")),
    Runtime("alias-upper", model_alias="Agent-Router"),
    Runtime("alias-not-ascii", model_alias="mod\u00e8le"),
    Runtime("alias-empty", model_alias=""),
    Runtime("system-prompt-unknown", system_prompt="prepend"),
)


def _runtime_vector(runtime: Runtime, scratch: Path) -> Vector:
    args = {one.name: getattr(runtime, one.name) for one in fields(runtime) if one.name != "id"}
    given: dict[str, Json] = {"args": normalize(args)}
    target = scratch / runtime.id / "config"
    config = config_mount.RuntimeConfig(**args)
    outcome = attempt(
        lambda: config_mount.write_config_mount(target, instructions="", skills={}, runtime=config)
    )
    if isinstance(outcome, Raised):
        return raised(runtime.id, given, outcome.exc)

    written = (target / config_mount.RUNTIME_FILE).read_bytes()

    return accepted(runtime.id, given, output=bytes_input(written))


def _runtime_surface(scratch: Path) -> Surface:
    return Surface(
        name="config.runtime_json.write",
        path="config/runtime_json.write.json",
        entry="caregiver.config_mount.write_config_mount",
        contract=RUNTIME_CONTRACT,
        notes=(
            "The input is the fields of caregiver.config_mount.RuntimeConfig, as args.",
            "output is the exact bytes of runtime.json in the directory that the entry point "
            "writes.",
            "The entry point checks no field. The family file check runs before it on a host.",
        ),
        vectors=tuple(_runtime_vector(runtime, scratch) for runtime in RUNTIMES),
    )


# --- creds.json -------------------------------------------------------------

CREDS_CONTRACT: Final = "contract 03 §12"

_WRITTEN: Final = "2030-01-02T03:04:05Z"
_LATER: Final = "2030-01-02T03:09:05Z"

#: The two values are text of a test. No LiteLLM and no chaperone takes them.
_KEY: Final = "sk-test-key"
_TOKEN: Final = "TESTTOKENTESTTOKENTESTTOKENTESTTOKENTESTTOKENTESTTOKEN22"
_OLD_TOKEN: Final = "OLDERTOKENOLDERTOKENOLDERTOKENOLDERTOKENOLDERTOKEN222222"


def _creds_text(**replaced: str) -> str:
    """The JSON text of a `creds.json`. A replaced field is JSON text as it is."""
    body = {
        "epoch": "7",
        "litellm_key": f'"{_KEY}"',
        "pep_token": f'"{_TOKEN}"',
        "written_at": f'"{_WRITTEN}"',
        **replaced,
    }

    return "{" + ", ".join(f'"{key}": {value}' for key, value in body.items() if value) + "}"


@dataclass(frozen=True)
class CredsDocument:
    """One `creds.json` to read: an id and its bytes."""

    id: str
    raw: bytes


def _creds(doc_id: str, **replaced: str) -> CredsDocument:
    return CredsDocument(doc_id, _creds_text(**replaced).encode("utf-8"))


CREDS_DOCUMENTS: Final[tuple[CredsDocument, ...]] = (
    # --- whole documents ---
    _creds("minimal"),
    _creds("previous-null", previous_pep_token="null", previous_expires_at="null"),
    _creds("previous-set", previous_pep_token=f'"{_OLD_TOKEN}"', previous_expires_at=f'"{_LATER}"'),
    _creds("unknown-key", family='"chat"'),
    CredsDocument("as-the-writer-writes", b""),
    CredsDocument("empty", b""),
    CredsDocument("not-json", b"epoch=7"),
    CredsDocument("top-array", b"[" + _creds_text().encode() + b"]"),
    CredsDocument("top-null", b"null"),
    CredsDocument("top-text", b'"creds"'),
    CredsDocument("top-number", b"7"),
    CredsDocument("empty-object", b"{}"),
    CredsDocument("trailing-text", _creds_text().encode() + b" x"),
    CredsDocument("space-around", b" \n\t" + _creds_text().encode() + b"\r\n"),
    CredsDocument("byte-order-mark", b"\xef\xbb\xbf" + _creds_text().encode()),
    CredsDocument(
        "duplicate-epoch", _creds_text().replace('"epoch": 7', '"epoch": 1, "epoch": 7').encode()
    ),
    _creds("nan-in-unknown-key", extra="NaN"),
    # --- the epoch ---
    _creds("epoch-zero", epoch="0"),
    _creds("epoch-negative", epoch="-3"),
    _creds("epoch-max-64-bit-signed", epoch="9223372036854775807"),
    _creds("epoch-past-64-bit-signed", epoch="9223372036854775808"),
    _creds("epoch-max-64-bit", epoch="18446744073709551615"),
    _creds("epoch-30-digits", epoch="1" + "0" * 30),
    _creds("epoch-min-64-bit-signed", epoch="-9223372036854775808"),
    _creds("epoch-below-64-bit-signed", epoch="-9223372036854775809"),
    _creds("epoch-true", epoch="true"),
    _creds("epoch-false", epoch="false"),
    _creds("epoch-fraction", epoch="7.9"),
    _creds("epoch-negative-fraction", epoch="-7.9"),
    _creds("epoch-whole-float", epoch="7.0"),
    _creds("epoch-exponent", epoch="1e2"),
    _creds("epoch-large-float", epoch="1e30"),
    _creds("epoch-float-16-digits", epoch="8848919470643385.0"),
    _creds("epoch-float-below-2-to-63", epoch="9.223372036854775e18"),
    _creds("epoch-float-2-to-63", epoch="9223372036854775807.0"),
    _creds("epoch-float-minus-2-to-63", epoch="-9223372036854775808.0"),
    _creds("epoch-nan", epoch="NaN"),
    _creds("epoch-text", epoch='"7"'),
    _creds("epoch-text-space", epoch='" 7 "'),
    _creds("epoch-text-sign", epoch='"+7"'),
    _creds("epoch-text-underscore", epoch='"1_0"'),
    _creds("epoch-text-fraction", epoch='"7.0"'),
    _creds("epoch-text-word", epoch='"seven"'),
    _creds("epoch-text-empty", epoch='""'),
    _creds("epoch-text-digit-not-ascii", epoch='"\\u0667"'),
    _creds("epoch-text-unit-separator", epoch='"\\u001f7"'),
    _creds("epoch-text-next-line", epoch='"\\u00857"'),
    _creds("epoch-text-4300-digits", epoch='"' + "0" * (INT_DIGITS_MAX - 1) + '7"'),
    _creds("epoch-text-4301-digits", epoch='"' + "0" * INT_DIGITS_MAX + '7"'),
    _creds("epoch-null", epoch="null"),
    _creds("epoch-list", epoch="[7]"),
    _creds("epoch-object", epoch='{"n": 7}'),
    _creds("epoch-missing", epoch=""),
    # --- the two secrets ---
    _creds("key-missing", litellm_key=""),
    _creds("key-null", litellm_key="null"),
    _creds("key-number", litellm_key="7"),
    _creds("key-true", litellm_key="true"),
    _creds("key-list", litellm_key='["a", 1.0, null]'),
    _creds("key-empty", litellm_key='""'),
    _creds("key-not-ascii", litellm_key='"cl\\u00e9"'),
    _creds("key-lone-surrogate", litellm_key='"\\ud800"'),
    _creds("token-missing", pep_token=""),
    _creds("token-null", pep_token="null"),
    _creds("token-empty", pep_token='""'),
    # --- the time of the write ---
    _creds("written-missing", written_at=""),
    _creds("written-null", written_at="null"),
    _creds("written-number", written_at="7"),
    _creds("written-empty", written_at='""'),
    _creds("written-not-a-time", written_at='"yesterday"'),
    # --- the previous token ---
    _creds("previous-token-only", previous_pep_token=f'"{_OLD_TOKEN}"'),
    _creds("previous-expiry-only", previous_expires_at=f'"{_LATER}"'),
    _creds("previous-empty", previous_pep_token='""', previous_expires_at='""'),
    _creds("previous-number", previous_pep_token="7", previous_expires_at="7"),
    _creds("previous-list", previous_pep_token='["old"]', previous_expires_at="[]"),
    _creds("previous-true", previous_pep_token="true", previous_expires_at="false"),
)

#: The document that the writer writes, read back by the reader.
_WRITTEN_BACK: Final = "as-the-writer-writes"


def _creds_read_vector(document: CredsDocument, scratch: Path) -> Vector:
    target = scratch / "read" / document.id / "creds.json"
    target.parent.mkdir(parents=True)
    if document.id == _WRITTEN_BACK:
        credentials.write_creds(
            target, credentials.Credentials(7, _KEY, _TOKEN, _WRITTEN, _OLD_TOKEN, _LATER)
        )
    else:
        target.write_bytes(document.raw)

    given = bytes_input(target.read_bytes())
    outcome = attempt(lambda: credentials.read_creds(target))
    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc)

    if outcome is None:
        return refused(document.id, given)

    return accepted(document.id, given, outcome)


@dataclass(frozen=True)
class CredsWrite:
    """One `creds.json` to write: the fields of `Credentials`."""

    id: str
    epoch: int = 7
    litellm_key: str = _KEY
    pep_token: str = _TOKEN
    written_at: str = _WRITTEN
    previous_pep_token: str | None = None
    previous_expires_at: str | None = None


CREDS_WRITES: Final[tuple[CredsWrite, ...]] = (
    CredsWrite("first-epoch", epoch=1),
    CredsWrite("with-previous", previous_pep_token=_OLD_TOKEN, previous_expires_at=_LATER),
    CredsWrite("previous-token-only", previous_pep_token=_OLD_TOKEN),
    CredsWrite("epoch-zero", epoch=0),
    CredsWrite("epoch-negative", epoch=-3),
    CredsWrite("epoch-max-64-bit-signed", epoch=2**63 - 1),
    CredsWrite("written-empty", written_at=""),
    CredsWrite(
        "text-that-json-escapes",
        written_at='quote " backslash \\ tab \t newline \n nul \x00 del \x7f',
    ),
    CredsWrite("text-not-ascii", written_at="\u00e9 \u2028 \U0001f600", litellm_key="cl\u00e9"),
    # A value below is one that the type of the port cannot hold.
    CredsWrite("epoch-past-64-bit-signed", epoch=2**63),
    CredsWrite("key-empty", litellm_key=""),
    CredsWrite("token-empty", pep_token=""),
)


def _creds_write_vector(write: CredsWrite, scratch: Path) -> Vector:
    args = {one.name: getattr(write, one.name) for one in fields(write) if one.name != "id"}
    given: dict[str, Json] = {"args": normalize(args)}
    target = scratch / "write" / write.id / "creds.json"
    outcome = attempt(lambda: credentials.write_creds(target, credentials.Credentials(**args)))
    if isinstance(outcome, Raised):
        return raised(write.id, given, outcome.exc)

    return accepted(write.id, given, output=bytes_input(target.read_bytes()))


def _creds_surfaces(scratch: Path) -> tuple[Surface, ...]:
    return (
        Surface(
            name="config.creds_json.read",
            path="config/creds_json.read.json",
            entry="caregiver.credentials.read_creds",
            contract=CREDS_CONTRACT,
            notes=(
                "The input is the bytes of a creds.json.",
                "value is the fields that the reader takes from the file. A refused vector "
                "is a file that the reader does not use: it returns None.",
                "Each key and each token here is text of a test.",
            ),
            vectors=tuple(_creds_read_vector(document, scratch) for document in CREDS_DOCUMENTS),
        ),
        Surface(
            name="config.creds_json.write",
            path="config/creds_json.write.json",
            entry="caregiver.credentials.write_creds",
            contract=CREDS_CONTRACT,
            notes=(
                "The input is the fields of caregiver.credentials.Credentials, as args.",
                "output is the exact bytes of the file that the entry point writes.",
                "The entry point checks no field.",
            ),
            vectors=tuple(_creds_write_vector(write, scratch) for write in CREDS_WRITES),
        ),
    )


# --- the env file of the playpen --------------------------------------------

PLAYPEN_ENV_CONTRACT: Final = "contract 03 §7.1"

_STATE_ROOT: Final = "/srv/agents/state/rework"


@dataclass(frozen=True)
class EnvWrite:
    """One env file to write: the arguments of `write_playpen_env`."""

    id: str
    state_root: str = _STATE_ROOT
    family: str = "chat"
    sandbox: str = "chat-s3"


ENV_WRITES: Final[tuple[EnvWrite, ...]] = (
    EnvWrite("one-sandbox"),
    EnvWrite("family-with-hyphens", family="dev-team", sandbox="dev-team-s12"),
    EnvWrite("root-is-the-state-root", state_root="/"),
    EnvWrite("state-root-final-slash", state_root="/tmp/state/"),
    EnvWrite("state-root-two-slashes", state_root="//net/state"),
    EnvWrite("state-root-dot-segment", state_root="/tmp/./state"),
    # A value below is one that the type of the port cannot hold.
    EnvWrite("state-root-relative", state_root="state"),
    EnvWrite("sandbox-of-another-family", sandbox="code-s1"),
    EnvWrite("sandbox-with-no-number", sandbox="chat"),
)


def _env_write_vector(write: EnvWrite, scratch: Path) -> Vector:
    args = {"state_root": write.state_root, "family": write.family, "sandbox": write.sandbox}
    given: dict[str, Json] = {"args": normalize(args)}
    target = scratch / "write" / write.id / "supervisor.env"
    outcome = attempt(
        lambda: playpen_env.write_playpen_env(
            target, state_root=Path(write.state_root), family=write.family, sandbox=write.sandbox
        )
    )
    if isinstance(outcome, Raised):
        return raised(write.id, given, outcome.exc)

    return accepted(write.id, given, output=bytes_input(target.read_bytes()))


@dataclass(frozen=True)
class EnvText:
    """One env file to read: an id and its text."""

    id: str
    text: str


_ENV_FILE: Final = (
    f"AGENT_CRED_DIR={_STATE_ROOT}/families/chat/creds\n"
    f"AGENT_FAMILY_CONFIG_DIR={_STATE_ROOT}/families/chat/config\n"
    f"AGENT_CONTROL_DIR={_STATE_ROOT}/families/chat/control/chat-s3\n"
    "AGENT_SANDBOX=chat-s3\n"
)

ENV_TEXTS: Final[tuple[EnvText, ...]] = (
    EnvText("as-the-writer-writes", _ENV_FILE),
    EnvText("empty", ""),
    EnvText("line-with-no-sign", _ENV_FILE + "not an assignment\n"),
    EnvText("comment-line", "# AGENT_SANDBOX=chat-s1\n" + _ENV_FILE),
    EnvText("last-line-wins", _ENV_FILE + "AGENT_SANDBOX=chat-s4\n"),
    EnvText("space-is-kept", " AGENT_SANDBOX = chat-s3 \n"),
    EnvText("value-with-a-sign", "A=b=c\n"),
    EnvText("empty-name-and-empty-value", "=x\nA=\n=\n"),
    EnvText("quotes-are-kept", 'A="chat-s3"\n'),
    EnvText("crlf", _ENV_FILE.replace("\n", "\r\n")),
    EnvText("no-final-newline", _ENV_FILE.rstrip("\n")),
    EnvText("line-breaks-of-python", "A=1\x0bB=2\x85C=3\u2028D=4\x1cE=5\x1fF=6"),
    EnvText("missing-variables", "AGENT_CRED_DIR=\nAGENT_SANDBOX=chat-s3\n"),
)


def _env_read_vector(env: EnvText, scratch: Path) -> Vector:
    given = text_input(env.text)
    target = scratch / "read" / env.id / "supervisor.env"
    target.parent.mkdir(parents=True)
    target.write_bytes(env.text.encode("utf-8"))
    outcome = attempt(lambda: playpen_env.read_playpen_env(target))
    if isinstance(outcome, Raised):
        return raised(env.id, given, outcome.exc)

    return accepted(env.id, given, {"variables": outcome})


def _playpen_env_surfaces(scratch: Path) -> tuple[Surface, ...]:
    return (
        Surface(
            name="config.playpen_env.write",
            path="config/playpen_env.write.json",
            entry="caregiver.playpen_env.write_playpen_env",
            contract=PLAYPEN_ENV_CONTRACT,
            notes=(
                "The input is the three named arguments of the entry point, as args. "
                "state_root is a path.",
                "output is the exact bytes of the file that the entry point writes.",
                "The entry point checks no argument.",
            ),
            vectors=tuple(_env_write_vector(write, scratch) for write in ENV_WRITES),
        ),
        Surface(
            name="config.playpen_env.read",
            path="config/playpen_env.read.json",
            entry="caregiver.playpen_env.read_playpen_env",
            contract=PLAYPEN_ENV_CONTRACT,
            notes=(
                "The input is the text of an env file.",
                "value.variables maps each name to its value. The entry point accepts each "
                "text: it is a helper for a test and for an operator. sbx parses the file on "
                "a host.",
            ),
            vectors=tuple(_env_read_vector(env, scratch) for env in ENV_TEXTS),
        ),
    )


# --- the environment of three services --------------------------------------

LAN: Final = "192.0.2.10"

#: A key of 32 bytes. It is text of a test.
VIEW_KEY: Final = "0123456789abcdef0123456789abcdef"


@dataclass(frozen=True)
class Environment:
    """One environment of a service: an id and its variables."""

    id: str
    variables: dict[str, str]

    def given(self) -> dict[str, Json]:
        return {"args": normalize(self.variables)}


def _named(error: Exception) -> dict[str, str]:
    """The variable that a `ConfigError` names: the first word of its text.

    The text also holds the value, and a vector does not.
    """
    return {"variable": str(error).split(" ", 1)[0]}


def _attendance(env_id: str, **variables: str) -> Environment:
    """An environment of `attendance` with the LAN address of the site file."""
    prefixed = {f"SESSIOND_{name}": value for name, value in variables.items()}

    return Environment(env_id, {"AGENT_LAN_ADDRESS": LAN, **prefixed})


ATTENDANCE_ENVS: Final[tuple[Environment, ...]] = (
    _attendance("site-only"),
    Environment("no-variable", {}),
    Environment("lan-address-empty", {"AGENT_LAN_ADDRESS": "  "}),
    Environment("lan-address-of-the-service", {"SESSIOND_LAN_ADDRESS": "127.0.0.1"}),
    _attendance("lan-address-of-the-service-wins", LAN_ADDRESS="192.0.2.11"),
    _attendance("lan-address-space", LAN_ADDRESS=" 192.0.2.11\n"),
    _attendance("lan-address-ipv6", LAN_ADDRESS="::1"),
    _attendance("lan-address-host-name", LAN_ADDRESS="host-1.example"),
    _attendance(
        "each-variable",
        SESSIONS_ROOT="/tmp/root/sessions",
        STATE_ROOT="/tmp/root/state",
        WORK_ROOT="/tmp/root/work",
        SOCKET="/tmp/root/sessiond.sock",
        BIND_LAN="yes",
        LAN_ADDRESS="127.0.0.1",
        LAN_PORT="18350",
        CHANNEL_COMMAND="node /tmp/root/playpen.js --sandbox {sandbox}",
        LOG_DIR="/tmp/root/log",
        OWUI_URL="http://192.0.2.10:8181",
        OWUI_KEY_FILE="/tmp/root/owui-api.key",
        OWUI_FOLDER_ID="folder-1",
        LOCK_STALE_S="0.8",
        LOCK_POLL_S="0.05",
    ),
    _attendance("empty-values", STATE_ROOT="", LAN_PORT="  ", BIND_LAN="", OWUI_URL=" "),
    _attendance("unknown-variable", NOT_A_VARIABLE="1"),
    # --- paths ---
    _attendance("path-space-around", STATE_ROOT="  /tmp/root/state\n"),
    _attendance("path-final-slash", STATE_ROOT="/tmp/root/state/"),
    _attendance("path-two-slashes", STATE_ROOT="/tmp//root/state"),
    _attendance("path-double-root", STATE_ROOT="//tmp/root"),
    _attendance("path-dot-segments", STATE_ROOT="/tmp/./root/../state"),
    _attendance("path-relative", STATE_ROOT="state"),
    _attendance("socket-107-bytes", SOCKET="/" + "a" * 106),
    _attendance("socket-108-bytes", SOCKET="/" + "a" * 107),
    # --- the bind ---
    _attendance("bind-true", BIND_LAN="true"),
    _attendance("bind-one", BIND_LAN="1"),
    _attendance("bind-on-upper", BIND_LAN=" ON "),
    _attendance("bind-false", BIND_LAN="false"),
    _attendance("bind-zero", BIND_LAN="0"),
    _attendance("bind-no", BIND_LAN="No"),
    _attendance("bind-off", BIND_LAN="off"),
    _attendance("bind-other-word", BIND_LAN="maybe"),
    _attendance("bind-two", BIND_LAN="2"),
    _attendance("lan-each-interface", BIND_LAN="1", LAN_ADDRESS="0.0.0.0"),
    _attendance("lan-each-interface-ipv6", BIND_LAN="1", LAN_ADDRESS="::"),
    _attendance("lan-not-a-host", LAN_ADDRESS="not a host"),
    Environment("site-lan-each-interface", {"AGENT_LAN_ADDRESS": "0.0.0.0"}),
    Environment("site-lan-ipv6", {"AGENT_LAN_ADDRESS": "::1"}),
    # --- the port ---
    _attendance("port-one", LAN_PORT="1"),
    _attendance("port-65535", LAN_PORT="65535"),
    _attendance("port-zero", LAN_PORT="0"),
    _attendance("port-65536", LAN_PORT="65536"),
    _attendance("port-negative", LAN_PORT="-1"),
    _attendance("port-sign", LAN_PORT="+8350"),
    _attendance("port-underscore", LAN_PORT="8_350"),
    _attendance("port-leading-zeros", LAN_PORT="008350"),
    _attendance("port-word", LAN_PORT="http"),
    _attendance("port-fraction", LAN_PORT="8350.0"),
    _attendance("port-hex", LAN_PORT="0x209e"),
    _attendance("port-digits-not-ascii", LAN_PORT="\uff18\uff13\uff15\uff10"),
    _attendance("port-5000-digits", LAN_PORT="9" * 5000),
    _attendance("port-4300-digits", LAN_PORT="0" * (INT_DIGITS_MAX - 2) + "80"),
    _attendance("port-4301-digits", LAN_PORT="0" * (INT_DIGITS_MAX - 1) + "80"),
    _attendance("port-unit-separator", LAN_PORT="\x1f8350"),
    # --- the two counts of seconds ---
    _attendance("seconds-whole", LOCK_STALE_S="30", LOCK_POLL_S="2"),
    _attendance("seconds-exponent", LOCK_STALE_S="1e3"),
    _attendance("seconds-underscore", LOCK_STALE_S="1_0.5"),
    _attendance("seconds-sign", LOCK_STALE_S="+20"),
    _attendance("seconds-no-whole-part", LOCK_POLL_S=".5"),
    _attendance("seconds-zero", LOCK_STALE_S="0"),
    _attendance("seconds-negative", LOCK_POLL_S="-1"),
    _attendance("seconds-negative-zero", LOCK_POLL_S="-0.0"),
    _attendance("seconds-underflow", LOCK_POLL_S="1e-400"),
    _attendance("seconds-word", LOCK_STALE_S="soon"),
    _attendance("seconds-unit", LOCK_STALE_S="20s"),
    _attendance("seconds-nan", LOCK_STALE_S="nan"),
    _attendance("seconds-infinity", LOCK_STALE_S="inf"),
    _attendance("seconds-overflow", LOCK_STALE_S="1e999"),
    _attendance("seconds-past-a-duration", LOCK_STALE_S="1e30"),
    _attendance("seconds-below-a-nanosecond", LOCK_POLL_S="1e-12"),
    _attendance("seconds-digits-not-ascii", LOCK_STALE_S="\u0662\u0660"),
    # --- the channel command and Open WebUI ---
    _attendance("command-quoted-words", CHANNEL_COMMAND="sh -c 'exec node \"$0\"' {sandbox}"),
    _attendance("command-space-around", CHANNEL_COMMAND="  node playpen.js  "),
    _attendance("command-quote-with-no-end", CHANNEL_COMMAND="sbx 'exec {sandbox}"),
    _attendance("command-final-backslash", CHANNEL_COMMAND="sbx exec \\"),
    _attendance("owui-url-final-slash", OWUI_URL="http://192.0.2.10:8181/"),
    _attendance("owui-url-no-scheme", OWUI_URL="192.0.2.10:8181"),
    _attendance("owui-url-with-password", OWUI_URL="http://user:password@192.0.2.10:8181"),
    _attendance("owui-key-file-relative", OWUI_KEY_FILE="owui-api.key"),
)


def _attendance_vector(env: Environment) -> Vector:
    outcome = attempt(lambda: attendance_config.from_env(dict(env.variables)))
    if not isinstance(outcome, Raised):
        value = {
            one.name: _plain(getattr(outcome, one.name)) for one in fields(attendance_config.Config)
        }
        return accepted(env.id, env.given(), value)

    if isinstance(outcome.exc, attendance_config.ConfigError):
        return refused(env.id, env.given(), _named(outcome.exc))

    return raised(env.id, env.given(), outcome.exc)


def _plain(value: object) -> object:
    """A path as its text. `normalize` has no form for a path."""
    return str(value) if isinstance(value, Path) else value


def _noticeboard(env_id: str, **variables: str) -> Environment:
    """An environment of the noticeboard on loopback, which needs no key."""
    prefixed = {f"VIEW_{name}": value for name, value in variables.items()}

    return Environment(env_id, {"VIEW_BIND": "127.0.0.1", **prefixed})


def _lan_board(env_id: str, **variables: str) -> Environment:
    """An environment of the noticeboard on the LAN address, with a key."""
    prefixed = {f"VIEW_{name}": value for name, value in variables.items()}

    return Environment(env_id, {"AGENT_LAN_ADDRESS": LAN, "VIEW_ACCESS_KEY": VIEW_KEY, **prefixed})


NOTICEBOARD_ENVS: Final[tuple[Environment, ...]] = (
    _noticeboard("loopback-no-key"),
    _lan_board("lan-with-key"),
    Environment("no-variable", {}),
    Environment("key-and-no-address", {"VIEW_ACCESS_KEY": VIEW_KEY}),
    _lan_board(
        "each-variable",
        BIND="192.0.2.11",
        PORT="18370",
        STATE_ROOT="/tmp/root/state",
        REGISTRY_DIR="/tmp/root/registry",
        SESSIOND_SOCKET="/tmp/root/sessiond.sock",
        PAGE_SIZE="25",
        COOKIE_SECURE="0",
    ),
    _noticeboard("empty-values", PORT=" ", STATE_ROOT="", PAGE_SIZE="", COOKIE_SECURE=""),
    # --- the bind and the key ---
    Environment("lan-no-key", {"AGENT_LAN_ADDRESS": LAN}),
    Environment("lan-key-31-bytes", {"AGENT_LAN_ADDRESS": LAN, "VIEW_ACCESS_KEY": VIEW_KEY[1:]}),
    Environment(
        "lan-key-32-bytes-with-space",
        {"AGENT_LAN_ADDRESS": LAN, "VIEW_ACCESS_KEY": f"  {VIEW_KEY}\n"},
    ),
    Environment(
        "lan-key-16-chars-32-bytes",
        {"AGENT_LAN_ADDRESS": LAN, "VIEW_ACCESS_KEY": "\u00e9" * 16},
    ),
    _noticeboard("loopback-short-key", ACCESS_KEY="short"),
    _noticeboard("loopback-ipv6", BIND="::1"),
    _noticeboard("loopback-name", BIND="localhost"),
    _noticeboard("loopback-other-address", BIND="127.0.0.2"),
    _lan_board("loopback-other-address-with-key", BIND="127.0.0.2"),
    _lan_board("bind-name-upper", BIND="LOCALHOST"),
    _lan_board("bind-wins-over-the-site", BIND="192.0.2.11"),
    _lan_board("bind-each-interface", BIND="0.0.0.0"),
    _lan_board("bind-each-interface-ipv6", BIND="::"),
    _lan_board("bind-star", BIND="*"),
    _lan_board("bind-each-interface-long-form", BIND="0:0:0:0:0:0:0:0"),
    _lan_board("bind-one-number", BIND="0"),
    _lan_board("bind-not-a-host", BIND="not a host"),
    Environment(
        "site-each-interface", {"AGENT_LAN_ADDRESS": "0.0.0.0", "VIEW_ACCESS_KEY": VIEW_KEY}
    ),
    # --- the port and the page size ---
    _noticeboard("port-one", PORT="1"),
    _noticeboard("port-65535", PORT="65535"),
    _noticeboard("port-zero", PORT="0"),
    _noticeboard("port-65536", PORT="65536"),
    _noticeboard("port-word", PORT="http"),
    _noticeboard("port-4301-digits", PORT="0" * (INT_DIGITS_MAX - 1) + "80"),
    _noticeboard("page-size-one", PAGE_SIZE="1"),
    _noticeboard("page-size-500", PAGE_SIZE="500"),
    _noticeboard("page-size-zero", PAGE_SIZE="0"),
    _noticeboard("page-size-501", PAGE_SIZE="501"),
    _noticeboard("page-size-underscore", PAGE_SIZE="5_0"),
    _noticeboard("page-size-word", PAGE_SIZE="many"),
    _noticeboard("page-size-fraction", PAGE_SIZE="50.0"),
    _noticeboard("page-size-4300-digits", PAGE_SIZE="0" * (INT_DIGITS_MAX - 2) + "50"),
    _noticeboard("page-size-4301-digits", PAGE_SIZE="0" * (INT_DIGITS_MAX - 1) + "50"),
    # --- the cookie switch ---
    _noticeboard("cookie-zero", COOKIE_SECURE="0"),
    _noticeboard("cookie-false-upper", COOKIE_SECURE="FALSE"),
    _noticeboard("cookie-no", COOKIE_SECURE=" no "),
    _noticeboard("cookie-off", COOKIE_SECURE="off"),
    _noticeboard("cookie-one", COOKIE_SECURE="1"),
    _noticeboard("cookie-other-word", COOKIE_SECURE="maybe"),
    # --- attendance ---
    _noticeboard("url-wins-over-the-socket", SESSIOND_URL="http://192.0.2.10:8350"),
    _noticeboard(
        "url-and-socket", SESSIOND_URL="http://192.0.2.10:8350", SESSIOND_SOCKET="/tmp/s.sock"
    ),
    _noticeboard("url-final-slash", SESSIOND_URL="http://192.0.2.10:8350/"),
    _noticeboard("url-no-scheme", SESSIOND_URL="192.0.2.10:8350"),
    _noticeboard("socket-relative", SESSIOND_SOCKET="sessiond.sock"),
    _noticeboard("state-root-relative", STATE_ROOT="state"),
    _noticeboard("registry-final-slash", REGISTRY_DIR="/tmp/root/registry/"),
)


def _noticeboard_vector(env: Environment) -> Vector:
    outcome = attempt(lambda: noticeboard_config.from_env(dict(env.variables)))
    if not isinstance(outcome, Raised):
        value = {
            one.name: _plain(getattr(outcome, one.name))
            for one in fields(noticeboard_config.Config)
        }
        return accepted(env.id, env.given(), value)

    if isinstance(outcome.exc, noticeboard_config.ConfigError):
        return refused(env.id, env.given(), _named(outcome.exc))

    return raised(env.id, env.given(), outcome.exc)


def _pep(env_id: str, **variables: str) -> Environment:
    return Environment(env_id, variables)


CHAPERONE_ENVS: Final[tuple[Environment, ...]] = (
    _pep("site-only", AGENT_LAN_ADDRESS=LAN),
    _pep("no-variable"),
    _pep("lan-address-empty", AGENT_LAN_ADDRESS="  "),
    _pep("lan-address-space", AGENT_LAN_ADDRESS=f" {LAN}\n"),
    _pep("lan-address-host-name", AGENT_LAN_ADDRESS="host-1.example"),
    _pep("lan-address-each-interface", AGENT_LAN_ADDRESS="0.0.0.0"),
    _pep("lan-address-ipv6", AGENT_LAN_ADDRESS="::1"),
    _pep("lan-address-not-a-host", AGENT_LAN_ADDRESS="not a host"),
    _pep("bind-wins", AGENT_LAN_ADDRESS=LAN, PEP_BIND="127.0.0.1:18300"),
    _pep("bind-and-no-site", PEP_BIND="127.0.0.1:18300"),
    _pep("bind-empty", AGENT_LAN_ADDRESS=LAN, PEP_BIND=" "),
    _pep("bind-space", PEP_BIND=" 127.0.0.1:18300\n"),
    _pep("bind-ipv6-brackets", PEP_BIND="[::1]:18300"),
    _pep("bind-ipv6-no-brackets", PEP_BIND="::1:18300"),
    _pep("bind-host-name", PEP_BIND="localhost:18300"),
    _pep("bind-each-interface", PEP_BIND="0.0.0.0:8300"),
    _pep("bind-no-port", PEP_BIND="127.0.0.1"),
    _pep("bind-no-host", PEP_BIND=":8300"),
    _pep("bind-port-word", PEP_BIND="127.0.0.1:http"),
    _pep("bind-port-zero", PEP_BIND="127.0.0.1:0"),
    _pep(
        "home-assistant-of-the-site", AGENT_LAN_ADDRESS=LAN, AGENT_HA_URL="http://192.0.2.20:8123"
    ),
    _pep(
        "home-assistant-of-the-service-wins",
        AGENT_HA_URL="http://192.0.2.20:8123",
        HA_URL="http://192.0.2.21:8123",
    ),
    _pep(
        "home-assistant-empty-falls-to-the-site", AGENT_HA_URL="http://192.0.2.20:8123", HA_URL=" "
    ),
    _pep("home-assistant-final-slash", HA_URL="http://192.0.2.21:8123/"),
    _pep("home-assistant-space", HA_URL=" https://ha.example\n"),
    _pep("home-assistant-no-scheme", HA_URL="192.0.2.21:8123"),
)

#: The four readers of `chaperone.site`, by the name a vector gives one.
_CHAPERONE_READERS: Final[dict[str, Callable[[Mapping[str, str]], str]]] = {
    "bind": chaperone_site.bind,
    "lan_address": chaperone_site.lan_address,
    "tei_url": chaperone_site.tei_url,
    "ha_url": chaperone_site.ha_url,
}


def _chaperone_vector(env: Environment, function: str) -> Vector:
    vector_id = f"{env.id}.{function}"
    params = {"function": function}
    reader = _CHAPERONE_READERS[function]
    outcome = attempt(lambda: reader(dict(env.variables)))
    if not isinstance(outcome, Raised):
        return accepted(vector_id, env.given(), {"text": outcome}, params=params)

    if isinstance(outcome.exc, chaperone_site.ConfigError):
        return refused(vector_id, env.given(), _named(outcome.exc), params=params)

    return raised(vector_id, env.given(), outcome.exc, params=params)


_NOTE_ENV: Final = "The input is the variables of the process, as args."
_NOTE_VARIABLE: Final = (
    "A refused vector is an environment for which the entry point raises its ConfigError. "
    "refusal.variable is the first word of the error text: the variable that the text names. "
    "The text also holds the value, and no vector holds the text."
)


def _env_surfaces() -> tuple[Surface, ...]:
    with quiet_logs():
        return (
            Surface(
                name="config.attendance.env",
                path="config/attendance.env.json",
                entry="attendance.config.from_env",
                contract="contract 02 §3",
                notes=(
                    _NOTE_ENV,
                    "value holds each field of attendance.config.Config. A path is its text.",
                    _NOTE_VARIABLE,
                ),
                vectors=tuple(_attendance_vector(env) for env in ATTENDANCE_ENVS),
            ),
            Surface(
                name="config.noticeboard.env",
                path="config/noticeboard.env.json",
                entry="noticeboard.config.from_env",
                contract="contract 02 §3",
                notes=(
                    _NOTE_ENV,
                    "value holds each field of noticeboard.config.Config. A path is its text. "
                    "attendance_socket is null when a URL wins.",
                    "No vector names VIEW_ACCESS_KEY_FILE: the entry point reads that file. "
                    "Each key here is text of a test.",
                    _NOTE_VARIABLE,
                ),
                vectors=tuple(_noticeboard_vector(env) for env in NOTICEBOARD_ENVS),
            ),
            Surface(
                name="config.chaperone.site",
                path="config/chaperone.site.json",
                entry="chaperone.site.<params.function>",
                contract="contract 02 §3 rule 2, contract 04 §4.1",
                notes=(
                    _NOTE_ENV,
                    "params.function names the reader: bind, lan_address, tei_url or ha_url. "
                    "Each environment has one vector for each reader. The id of a vector is "
                    "the id of the environment, a dot and the name of the reader.",
                    "value.text is the text that the reader returns. tei_url and ha_url "
                    "return the empty text for a site with no such service.",
                    _NOTE_VARIABLE,
                ),
                vectors=tuple(
                    _chaperone_vector(env, function)
                    for env in CHAPERONE_ENVS
                    for function in _CHAPERONE_READERS
                ),
            ),
        )


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix="vectors-config-") as scratch_name:
        scratch = Path(scratch_name)
        (scratch / "creds").mkdir()
        (scratch / "playpen-env").mkdir()

        return (
            _site_surface(scratch / "site"),
            _roster_surface(scratch / "roster"),
            _runtime_surface(scratch / "runtime"),
            *_creds_surfaces(scratch / "creds"),
            *_playpen_env_surfaces(scratch / "playpen-env"),
            *_env_surfaces(),
        )
