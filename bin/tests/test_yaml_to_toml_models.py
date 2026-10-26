"""A converted family file or server file reads to the model of its YAML text.

`bin/yaml-to-toml.py` converts each YAML family file and each YAML server
file of the fixture registry of the package `family`, in a child process.
The TOML path of `agent_family` reads the TOML text in the strict mode, and
the YAML path reads the YAML text in the lax mode. Both give one model.

Two more tests hold the TOML path of a server file to the root reader,
`handover.mcpserver.parse_server`. This file imports `handover`, and the
tests of `family` do not.

1. The cross test. Root accepts the TOML text of each fixture server, and
   the validator accepts it too.
2. The differences. For each rule that root holds and the YAML path does
   not, root refuses a YAML text and its converted text. The YAML path
   accepts the YAML text, and the TOML path refuses the converted text.

The test converts at test time and writes no file of the repository. The
migration removes the YAML fixtures. After that, each walk has no file to
convert, and the test still passes."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_family import Markup, parse_family, parse_server
from agent_family.registry import FAMILY_FILE_YAML, SERVER_FILE, SERVER_FILE_YAML
from agent_family.report import Issue, Issues
from agent_family.serverrules import check_server
from handover.errors import Refusal

from handover import mcpserver

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "yaml-to-toml.py"
FIXTURES = REPO / "family" / "tests" / "fixtures" / "registry"
FAMILIES = FIXTURES / "families"
SERVERS = FIXTURES / "mcp"

#: Each YAML family file of the fixture registry, by its directory name.
YAML_FAMILIES = sorted(path.parent.name for path in FAMILIES.glob(f"*/{FAMILY_FILE_YAML}"))

#: Each YAML server file of the fixture registry, by its directory name.
YAML_SERVERS = sorted(path.parent.name for path in SERVERS.glob(f"*/{SERVER_FILE_YAML}"))

#: Each TOML server file of the fixture registry, by its directory name.
TOML_SERVERS = sorted(path.parent.name for path in SERVERS.glob(f"*/{SERVER_FILE}"))

#: What a walk gets when its list is empty. pytest reports a parametrized
#: test with no value as skipped, and an empty walk must pass.
NO_FILE = "(no YAML file is left)"


def _converted(path: Path) -> str:
    """The TOML text that the converter prints for one YAML file."""
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert done.returncode == 0, done.stderr
    return done.stdout


def _server_issues(text: str, directory: str, markup: Markup) -> tuple[Issue, ...]:
    """Each issue of the validator for one server text: the parse, then each
    rule. A text that does not parse gives the issues of the parse."""
    server, issues = parse_server(text, markup)
    if server is None:
        return tuple(issues)

    found = Issues(issues)
    check_server(server, directory, found, markup)
    return found.frozen()


@pytest.mark.parametrize("name", YAML_FAMILIES or [NO_FILE])
def test_the_toml_text_reads_to_the_model_of_the_yaml_text(name: str) -> None:
    if name == NO_FILE:
        assert YAML_FAMILIES == []
        return

    path = FAMILIES / name / FAMILY_FILE_YAML

    from_yaml, yaml_issues = parse_family(path.read_text(encoding="utf-8"))
    from_toml, toml_issues = parse_family(_converted(path), Markup.TOML)

    assert yaml_issues == []
    assert toml_issues == []
    assert from_yaml is not None
    assert from_toml == from_yaml


def test_the_walk_finds_the_ten_family_files_of_today() -> None:
    """The fixture registry holds 10 YAML family files until the migration
    removes them. Then the list is empty, and that passes too."""
    assert len(YAML_FAMILIES) in (0, 10)


@pytest.mark.parametrize("name", YAML_SERVERS or [NO_FILE])
def test_the_toml_server_text_reads_to_the_model_of_the_yaml_text(name: str) -> None:
    if name == NO_FILE:
        assert YAML_SERVERS == []
        return

    path = SERVERS / name / SERVER_FILE_YAML

    from_yaml, yaml_issues = parse_server(path.read_text(encoding="utf-8"))
    from_toml, toml_issues = parse_server(_converted(path), Markup.TOML)

    assert yaml_issues == []
    assert toml_issues == []
    assert from_yaml is not None
    assert from_toml == from_yaml


def test_the_walk_finds_the_eleven_server_files_of_today() -> None:
    """The fixture registry holds 11 YAML server files until the migration
    removes them. Then the list is empty, and that passes too."""
    assert len(YAML_SERVERS) in (0, 11)


@pytest.mark.parametrize("name", TOML_SERVERS)
def test_root_and_the_validator_accept_each_toml_server_file(name: str) -> None:
    path = SERVERS / name / SERVER_FILE

    read = mcpserver.parse_server(path.read_bytes(), name, mcpserver.Markup.TOML)

    assert read.name == name
    assert _server_issues(path.read_text(encoding="utf-8"), name, Markup.TOML) == ()


def test_the_walk_finds_the_eleven_toml_server_files() -> None:
    assert len(TOML_SERVERS) == 11


# --- the differences between the YAML path and root --------------------------------

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


#: One YAML file for each rule that root holds and the YAML path does not.
DIFFERENCES: dict[str, dict[str, Any]] = {
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


@pytest.mark.parametrize("case", list(DIFFERENCES))
def test_root_refuses_each_difference_and_so_does_the_toml_path(case: str, tmp_path: Path) -> None:
    body = copy.deepcopy(BASE)
    body.update(copy.deepcopy(DIFFERENCES[case]))
    yaml_text = yaml.safe_dump(body)
    path = tmp_path / SERVER_FILE_YAML
    path.write_text(yaml_text, encoding="utf-8")

    toml_text = _converted(path)

    with pytest.raises(Refusal):
        mcpserver.parse_server(yaml_text.encode(), "kagi")

    with pytest.raises(Refusal):
        mcpserver.parse_server(toml_text.encode(), "kagi", mcpserver.Markup.TOML)

    assert _server_issues(yaml_text, "kagi", Markup.YAML) == ()
    assert _server_issues(toml_text, "kagi", Markup.TOML) != ()


def test_root_and_each_path_accept_the_base_of_the_differences(tmp_path: Path) -> None:
    yaml_text = yaml.safe_dump(BASE)
    path = tmp_path / SERVER_FILE_YAML
    path.write_text(yaml_text, encoding="utf-8")

    toml_text = _converted(path)

    assert mcpserver.parse_server(yaml_text.encode(), "kagi").name == "kagi"
    assert mcpserver.parse_server(toml_text.encode(), "kagi", mcpserver.Markup.TOML).name == "kagi"
    assert _server_issues(yaml_text, "kagi", Markup.YAML) == ()
    assert _server_issues(toml_text, "kagi", Markup.TOML) == ()
