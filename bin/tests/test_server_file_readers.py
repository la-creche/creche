"""The validator and root read one server file alike.

Two programs read `mcp/<name>/server.toml`: the validator, `agent_family`,
and root, `handover.mcpserver.parse_server`. Under contract 01b §1, root
accepts each file that the validator accepts. This file imports `handover`,
and the tests of `family` do not.

1. The cross test. Root accepts each server file of the fixture registry of
   the package `family`, and the validator accepts it too.
2. The rules of root. For each rule that root holds and contract 01b does
   not give, root refuses a text, and the validator refuses it too.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from agent_family import Markup, parse_server
from agent_family.registry import SERVER_FILE
from agent_family.report import Issue, Issues
from agent_family.serverrules import check_server
from handover.errors import Refusal

from handover import mcpserver

REPO = Path(__file__).resolve().parents[2]
SERVERS = REPO / "family" / "tests" / "fixtures" / "registry" / "mcp"

#: Each server file of the fixture registry, by its directory name.
FIXTURE_SERVERS = sorted(path.parent.name for path in SERVERS.glob(f"*/{SERVER_FILE}"))


def _server_issues(text: str, directory: str) -> tuple[Issue, ...]:
    """Each issue of the validator for one server text: the parse, then each
    rule. A text that does not parse gives the issues of the parse."""
    server, issues = parse_server(text, Markup.TOML)
    if server is None:
        return tuple(issues)

    found = Issues(issues)
    check_server(server, directory, found)
    return found.frozen()


@pytest.mark.parametrize("name", FIXTURE_SERVERS)
def test_root_and_the_validator_accept_each_fixture_server_file(name: str) -> None:
    path = SERVERS / name / SERVER_FILE

    read = mcpserver.parse_server(path.read_bytes(), name, mcpserver.Markup.TOML)

    assert read.name == name
    assert _server_issues(path.read_text(encoding="utf-8"), name) == ()


def test_the_walk_finds_the_eleven_fixture_server_files() -> None:
    assert len(FIXTURE_SERVERS) == 11


# --- the rules of root ----------------------------------------------------------------


def _key(key: str) -> str:
    return key if key.replace("_", "").replace("-", "").isalnum() else json.dumps(key)


def _value(value: Any) -> str:
    """One value as TOML text. A text of JSON is also a basic string of TOML
    when it holds no character past U+FFFF."""
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int | str):
        return json.dumps(value)

    if isinstance(value, list):
        return "[" + ", ".join(_value(one) for one in value) + "]"

    pairs = ", ".join(f"{_key(key)} = {_value(one)}" for key, one in value.items())
    return "{ " + pairs + " }"


def _toml(body: dict[str, Any]) -> str:
    """A TOML text with one line for each key of the top table."""
    return "".join(f"{_key(key)} = {_value(value)}\n" for key, value in body.items())


PYPI: dict[str, Any] = {
    "source": "pypi",
    "package": "kagimcp",
    "version": "0.1.2",
    "lock": "mcp/kagi/install.lock",
}

RELEASE: dict[str, Any] = {
    "source": "github-release",
    "repo": "github/github-mcp-server",
    "version": "1.12.0",
    "asset": "github-mcp-server_Linux_x86_64.tar.gz",
    "sha256": "f34de295acd8f1012c7f2c0e3b909d87361d0993b9489b57ee92ac72b85d7cca",
}

SECRET_ENV: dict[str, Any] = {"KAGI_API_KEY": "secret:kagi_api_key"}

RUN: dict[str, Any] = {"entrypoint": "kagimcp", "args": ["stdio"], "env": SECRET_ENV}

BASE: dict[str, Any] = {
    "name": "kagi",
    "identity": "The Kagi API key of the operator.",
    "install": PYPI,
    "run": RUN,
    "tools": [{"name": "kagi_search_fetch", "description": "Search the web.", "write": False}],
    "arg_denies": [{"tools": "all", "arg": "url", "values": ["file:///"]}],
}


def _fences(count: int, values: list[str]) -> list[dict[str, Any]]:
    return [{"tools": "all", "arg": "url", "values": list(values)} for _ in range(count)]


def _tools(count: int) -> list[dict[str, Any]]:
    return [{"name": f"tool_{n}", "description": "One tool."} for n in range(count)]


def _env(**changes: str) -> dict[str, Any]:
    return {**RUN, "env": {**SECRET_ENV, **changes}}


def _shared(count: int) -> dict[str, Any]:
    env = {f"S{n}_TOKEN": f"secret:shared_{n}" for n in range(count)}
    return {
        "run": {**RUN, "env": {**SECRET_ENV, **env}},
        "shared_secrets": [{"secret": f"shared_{n}", "server": "ha"} for n in range(count)],
    }


#: One file for each rule that root holds and contract 01b does not give.
RULES_OF_ROOT: dict[str, dict[str, Any]] = {
    "identity-past-1000-characters": {"identity": "a" + " " * 1000 + "b"},
    "description-past-200-characters": {
        "tools": [{"name": "kagi_search_fetch", "description": "a" + " " * 200 + "b"}],
    },
    "package": {"install": {**PYPI, "package": "_kagi"}},
    "repo": {"install": {**RELEASE, "repo": ".github/x"}},
    "asset": {"install": {**RELEASE, "asset": "dir/a.tar.gz"}},
    "python": {"install": {**PYPI, "python": "3.11"}},
    "entrypoint": {"run": {**RUN, "entrypoint": "kagi mcp"}},
    "state-dir-env": {"run": {**RUN, "state_dir": True, "state_dir_env": "state_dir"}},
    "value-length": {"run": _env(KAGI_URL="x" * 257)},
    "value-nul": {"run": _env(KAGI_URL="a\x00b")},
    "secret-key": {"run": _env(KAGI_API_KEY="secret:Kagi")},
    "fence-with-no-tool": {"arg_denies": [{"tools": [], "arg": "url", "values": ["x"]}]},
    "tools-count": {"tools": _tools(201)},
    "args-count": {"run": {**RUN, "args": ["x"] * 33}},
    "word-length": {"run": {**RUN, "args": ["x" * 257]}},
    "word-nul": {"run": {**RUN, "args": ["a\x00"]}},
    "env-count": {"run": _env(**{f"VAR_{n}": "x" for n in range(32)})},
    "fences-count": {"arg_denies": _fences(65, ["v"])},
    "fence-values-count": {"arg_denies": _fences(1, ["v"] * 257)},
    "fence-value-length": {"arg_denies": _fences(1, ["v" * 257])},
    "fence-value-nul": {"arg_denies": _fences(1, ["a\x00"])},
    "shared-count": _shared(9),
}


@pytest.mark.parametrize("case", list(RULES_OF_ROOT))
def test_root_refuses_each_rule_of_root_and_so_does_the_validator(case: str) -> None:
    body = copy.deepcopy(BASE)
    body.update(copy.deepcopy(RULES_OF_ROOT[case]))
    text = _toml(body)

    with pytest.raises(Refusal):
        mcpserver.parse_server(text.encode(), "kagi", mcpserver.Markup.TOML)

    assert _server_issues(text, "kagi") != ()


def test_root_and_the_validator_accept_the_base_of_the_rules() -> None:
    text = _toml(BASE)

    assert mcpserver.parse_server(text.encode(), "kagi", mcpserver.Markup.TOML).name == "kagi"
    assert _server_issues(text, "kagi") == ()
