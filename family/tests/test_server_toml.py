"""A TOML server file: the strict read, the grammar of root and the file name.

A server file gets each rule of contract 01b and each rule of the root
reader of the same file. Each test holds its own texts. The tests of the
rules of root build each text from one value with `_toml`."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from agent_family import Markup, load_registry, parse_server, registry, tomlfile
from agent_family.report import Issue, Issues, Severity
from agent_family.serverrules import check_server

PYPI_TOML = """\
name = "kagi"
identity = "The Kagi API key of the operator. Search and page extraction."

install.source = "pypi"
install.package = "kagimcp"
install.version = "0.1.2"
install.lock = "mcp/kagi/install.lock"

run.entrypoint = "kagimcp"
run.env.KAGI_API_KEY = "secret:kagi_api_key"

tools = [
    { name = "kagi_search_fetch", description = "Search the web.", write = false },
]
"""

RELEASE_TOML = """\
name = "github-code"
identity = "A token for each repository of the operator."

install.source = "github-release"
install.repo = "github/github-mcp-server"
install.version = "1.12.0"
install.asset = "github-mcp-server_Linux_x86_64.tar.gz"
install.sha256 = "1234567890123456789012345678901234567890123456789012345678901234"

run.entrypoint = "github-mcp-server"
run.args = ["stdio", "--lockdown-mode"]
run.env.GITHUB_PERSONAL_ACCESS_TOKEN = "secret:github_token_code"

arg_denies = [
    { tools = "all", arg = "owner/repo", values = ["example-owner/agent-control"] },
    { tools = ["push_files"], arg = "branch", values = ["main", "master"] },
]

tools = [
    { name = "push_files", description = "Push several files in one commit.", write = true },
]
"""

AGENT_MCP_TOML = """\
name = "ha-read"
identity = "A read token of the house."

install.source = "agent-mcp"
install.python = "3.13"

run.entrypoint = "ha-mcp"
run.env.HA_URL = "http://192.0.2.10:8123"
run.env.HA_TOKEN = "secret:ha_token"
run.state_dir = true
run.state_dir_env = "HA_MCP_CONFIG_DIR"

shared_secrets = [{ secret = "ha_token", server = "ha" }]
"""

#: A TOML text of each source of a pin.
SOURCES = {"pypi": PYPI_TOML, "github-release": RELEASE_TOML, "agent-mcp": AGENT_MCP_TOML}

NOT_TOML = "the text is not TOML 1.0, or it nests deeper than 8 levels"
NOT_UTF8 = "the file is not UTF-8 text"
TOO_LARGE = "the file has more than 65536 bytes"
NO_FILE = "no server.toml; this directory is ignored"


def _issues(text: str) -> list[Issue]:
    server, issues = parse_server(text, Markup.TOML)

    assert server is None
    return issues


def _locs_and_messages(text: str) -> list[tuple[str, str]]:
    return [(issue.loc, issue.msg) for issue in _issues(text)]


# --- parse_server ---------------------------------------------------------------


@pytest.mark.parametrize("source", list(SOURCES))
def test_a_toml_server_of_each_source_reads(source: str) -> None:
    server, issues = parse_server(SOURCES[source], Markup.TOML)

    assert issues == []
    assert server is not None
    assert server.install.source == source


@pytest.mark.parametrize("source", list(SOURCES))
def test_a_toml_server_of_each_source_passes_each_rule(source: str) -> None:
    server, _ = parse_server(SOURCES[source], Markup.TOML)
    issues = Issues()

    assert server is not None
    check_server(server, server.name, issues)

    assert issues.frozen() == ()


def test_the_default_markup_of_a_server_is_toml() -> None:
    server, issues = parse_server(PYPI_TOML, Markup.TOML)

    assert issues == []
    assert parse_server(PYPI_TOML) == (server, issues)


def test_a_sha256_of_64_digits_is_a_text_in_toml() -> None:
    server, issues = parse_server(RELEASE_TOML, Markup.TOML)

    assert issues == []
    assert server is not None
    assert (
        server.install.sha256 == "1234567890123456789012345678901234567890123456789012345678901234"
    )


@pytest.mark.parametrize(
    ("old", "new", "loc", "message"),
    [
        ('install.python = "3.13"', "install.python = 3.13", "install.python", "valid string"),
        ("run.state_dir = true", 'run.state_dir = "yes"', "run.state_dir", "valid boolean"),
        ("run.state_dir = true", "run.state_dir = 1", "run.state_dir", "valid boolean"),
        (
            'run.env.HA_URL = "http://192.0.2.10:8123"',
            "run.env.HA_URL = 8123",
            "run.env.HA_URL",
            "valid string",
        ),
        (
            'identity = "A read token of the house."',
            "identity = 2024-01-01",
            "identity",
            "valid string",
        ),
        (
            'shared_secrets = [{ secret = "ha_token", server = "ha" }]',
            'shared_secrets = [{ secret = 7, server = "ha" }]',
            "shared_secrets[0].secret",
            "valid string",
        ),
    ],
    ids=[
        "float-for-text",
        "text-for-boolean",
        "integer-for-boolean",
        "integer-for-text",
        "date-for-text",
        "integer-for-secret",
    ],
)
def test_a_value_of_another_type_is_refused(old: str, new: str, loc: str, message: str) -> None:
    text = AGENT_MCP_TOML.replace(old, new, 1)
    issues = _issues(text)

    assert text != AGENT_MCP_TOML
    assert [issue.loc for issue in issues] == [loc]
    assert message in issues[0].msg
    assert issues[0].severity is Severity.ERROR


def test_two_unknown_keys_of_a_server_give_two_issues_in_sorted_order() -> None:
    issues = _issues(PYPI_TOML + "zeta = 1\nalpha = 2\n")

    assert [issue.loc for issue in issues] == ["alpha", "zeta"]
    assert all(issue.msg.startswith("unknown field") for issue in issues)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (PYPI_TOML + "name = 'twice'\n", NOT_TOML),
        (PYPI_TOML + "a = {b = 1,}\n", NOT_TOML),
        (PYPI_TOML + "c = 9223372036854775808\n", NOT_TOML),
        ("\ufeff" + PYPI_TOML, "the file starts with a byte order mark"),
        (PYPI_TOML + "padding = '" + "x" * 65536 + "'\n", TOO_LARGE),
        ('name = "a\ud800"\n', NOT_UTF8),
    ],
    ids=["duplicate-key", "toml11-form", "integer-past-64-bits", "bom", "size", "lone-surrogate"],
)
def test_a_refusal_of_the_reader_is_one_issue_at_the_document_of_a_server(
    text: str, message: str
) -> None:
    assert _locs_and_messages(text) == [("<document>", message)]


def test_an_empty_text_names_each_required_field_of_a_server() -> None:
    assert _locs_and_messages("") == [
        ("name", "Field required"),
        ("identity", "Field required"),
        ("install", "Field required"),
        ("run", "Field required"),
    ]


# --- the grammar of root ------------------------------------------------------------


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

BASE: dict[str, Any] = {
    "name": "kagi",
    "identity": "The Kagi API key of the operator.",
    "install": PYPI,
    "run": {"entrypoint": "kagimcp", "args": ["stdio"], "env": SECRET_ENV},
    "tools": [{"name": "kagi_search_fetch", "description": "Search the web.", "write": False}],
    "arg_denies": [{"tools": "all", "arg": "url", "values": ["file:///"]}],
}


def _tools(count: int) -> list[dict[str, Any]]:
    return [{"name": f"tool_{n}", "description": "One tool.", "write": False} for n in range(count)]


def _fences(count: int) -> list[dict[str, Any]]:
    return [{"tools": "all", "arg": "url", "values": [f"v{n}"]} for n in range(count)]


def _run(**changes: Any) -> dict[str, Any]:
    return {**BASE["run"], **changes}


def _env(**changes: str) -> dict[str, Any]:
    return _run(env={**SECRET_ENV, **changes})


def _variables(count: int) -> dict[str, Any]:
    """`count` variables, the variable of the key included."""
    return _run(env={**SECRET_ENV, **{f"VAR_{n}": "x" for n in range(count - 1)}})


def _shared(count: int) -> dict[str, Any]:
    """`count` shared secrets, each one named in `run.env`."""
    env = {f"S{n}_TOKEN": f"secret:shared_{n}" for n in range(count)}
    return {
        "run": _run(env={**SECRET_ENV, **env}),
        "shared_secrets": [{"secret": f"shared_{n}", "server": "ha"} for n in range(count)],
    }


def _fence_values(values: list[str]) -> list[dict[str, Any]]:
    return [{"tools": "all", "arg": "url", "values": values}]


def _changed(changes: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(BASE)
    body.update(copy.deepcopy(changes))
    return body


def _report(body: dict[str, Any]) -> tuple[Issue, ...]:
    """The issues of one server file, from its TOML text."""
    server, issues = parse_server(_toml(body), Markup.TOML)

    assert issues == [], issues
    assert server is not None
    found = Issues()
    check_server(server, body["name"], found)
    return found.frozen()


#: Each rule of root that contract 01b does not give: a file that root
#: refuses. The validator refuses it at `loc`.
DIFFERENCES: dict[str, tuple[dict[str, Any], str]] = {
    "identity-past-1000-characters": ({"identity": "a" + " " * 1000 + "b"}, "identity"),
    "description-past-200-characters": (
        {"tools": [{"name": "kagi_search_fetch", "description": "a" + " " * 200 + "b"}]},
        "tools[0]",
    ),
    "package-with-a-leading-underscore": (
        {"install": {**PYPI, "package": "_kagi"}},
        "install.package",
    ),
    "package-of-65-characters": ({"install": {**PYPI, "package": "k" * 65}}, "install.package"),
    "repo-with-a-leading-dot": ({"install": {**RELEASE, "repo": ".github/x"}}, "install.repo"),
    "repo-owner-of-40-characters": (
        {"install": {**RELEASE, "repo": "o" * 40 + "/x"}},
        "install.repo",
    ),
    "asset-with-a-slash": ({"install": {**RELEASE, "asset": "dir/a.tar.gz"}}, "install.asset"),
    "python-not-in-use": ({"install": {**PYPI, "python": "3.11"}}, "install.python"),
    "entrypoint-with-a-space": ({"run": _run(entrypoint="kagi mcp")}, "run.entrypoint"),
    "entrypoint-with-a-leading-dash": ({"run": _run(entrypoint="-kagi")}, "run.entrypoint"),
    "state-dir-env-in-lower-case": (
        {"run": _run(state_dir=True, state_dir_env="state_dir")},
        "run.state_dir_env",
    ),
    "value-of-257-characters": ({"run": _env(KAGI_URL="x" * 257)}, "run.env.KAGI_URL"),
    "value-with-a-nul": ({"run": _env(KAGI_URL="a\x00b")}, "run.env.KAGI_URL"),
    "secret-key-in-upper-case": ({"run": _env(KAGI_API_KEY="secret:Kagi")}, "run.env.KAGI_API_KEY"),
    "secret-key-of-one-character": ({"run": _env(KAGI_API_KEY="secret:k")}, "run.env.KAGI_API_KEY"),
    "fence-with-no-tool": (
        {"arg_denies": [{"tools": [], "arg": "url", "values": ["x"]}]},
        "arg_denies[0].tools",
    ),
    "201-tools": ({"tools": _tools(201)}, "tools"),
    "33-words-of-args": ({"run": _run(args=["x"] * 33)}, "run.args"),
    "word-of-257-characters": ({"run": _run(args=["x" * 257])}, "run.args[0]"),
    "word-with-a-nul": ({"run": _run(args=["a\x00"])}, "run.args[0]"),
    "33-variables": ({"run": _variables(33)}, "run.env"),
    "65-denies": ({"arg_denies": _fences(65)}, "arg_denies"),
    "65-allows": ({"arg_allows": _fences(65)}, "arg_allows"),
    "257-values-of-a-fence": ({"arg_denies": _fence_values(["v"] * 257)}, "arg_denies[0].values"),
    "value-of-a-fence-of-257-characters": (
        {"arg_denies": _fence_values(["v" * 257])},
        "arg_denies[0].values[0]",
    ),
    "value-of-a-fence-with-a-nul": (
        {"arg_denies": _fence_values(["a\x00"])},
        "arg_denies[0].values[0]",
    ),
    "9-shared-secrets": (_shared(9), "shared_secrets"),
}

#: Each limit of root, at the limit. The validator accepts each one.
AT_THE_LIMIT: dict[str, dict[str, Any]] = {
    "identity-of-300-characters": {"identity": "i" * 300},
    "description-of-200-characters": {
        "tools": [{"name": "kagi_search_fetch", "description": "d" * 200}],
    },
    "package-of-64-characters": {"install": {**PYPI, "package": "k" * 64}},
    "repo-of-39-and-100-characters": {"install": {**RELEASE, "repo": "o" * 39 + "/" + "n" * 100}},
    "asset-of-128-characters": {"install": {**RELEASE, "asset": "a" * 128}},
    "python-3-13": {"install": {**PYPI, "python": "3.13"}},
    "entrypoint-of-64-characters": {"run": _run(entrypoint="e" * 64)},
    "value-of-256-characters": {"run": _env(KAGI_URL="x" * 256)},
    "secret-key-of-63-characters": {"run": _env(KAGI_API_KEY="secret:k" + "_" * 62)},
    "200-tools": {"tools": _tools(200)},
    "32-words-of-args": {"run": _run(args=["x" * 256] * 32)},
    "32-variables": {"run": _variables(32)},
    "64-denies": {"arg_denies": _fences(64)},
    "256-values-of-a-fence": {"arg_denies": _fence_values(["v"] * 256)},
    "value-of-a-fence-of-256-characters": {"arg_denies": _fence_values(["v" * 256])},
    "8-shared-secrets": _shared(8),
}


@pytest.mark.parametrize("case", list(DIFFERENCES))
def test_the_validator_refuses_each_rule_of_root(case: str) -> None:
    changes, loc = DIFFERENCES[case]

    issues = _report(_changed(changes))

    assert [issue.loc for issue in issues] == [loc]
    assert issues[0].severity is Severity.ERROR


@pytest.mark.parametrize("case", list(AT_THE_LIMIT))
def test_the_validator_accepts_each_limit_of_root(case: str) -> None:
    assert _report(_changed(AT_THE_LIMIT[case])) == ()


def test_the_base_server_of_the_rules_of_root_passes() -> None:
    assert _report(copy.deepcopy(BASE)) == ()


@pytest.mark.parametrize(
    ("changes", "loc", "message"),
    [
        (
            {"identity": "a" + " " * 1000 + "b"},
            "identity",
            "identity is 1002 characters; it must be 1 to 300 and say whose credential "
            "this server holds",
        ),
        (
            {"identity": "   "},
            "identity",
            "identity holds only white space; it must say whose credential this server holds",
        ),
        (
            {"tools": [{"name": "kagi_search_fetch", "description": " \t "}]},
            "tools[0]",
            "description holds only white space",
        ),
        (
            {"install": {**PYPI, "package": "_kagi"}},
            "install.package",
            "'_kagi' is not a package name; use [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
        ),
        (
            {"install": {**RELEASE, "repo": ".github/x"}},
            "install.repo",
            "'.github/x' is not <owner>/<name> on GitHub",
        ),
        (
            {"install": {**RELEASE, "asset": "dir/a.tar.gz"}},
            "install.asset",
            "'dir/a.tar.gz' is not a release asset name; use [A-Za-z0-9][A-Za-z0-9._+-]{0,127}",
        ),
        (
            {"install": {**PYPI, "python": "3.11"}},
            "install.python",
            "'3.11' is not a Python version in use; use one of 3.12, 3.13",
        ),
        (
            {"run": _run(entrypoint="bin/kagimcp")},
            "run.entrypoint",
            "'bin/kagimcp' is not a console script name; use [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
        ),
        (
            {"run": _run(state_dir=True, state_dir_env="state_dir")},
            "run.state_dir_env",
            "'state_dir' is not an environment variable name; use [A-Z][A-Z0-9_]{0,63}",
        ),
        (
            {"run": _env(KAGI_URL="x" * 257)},
            "run.env.KAGI_URL",
            "the value is 257 characters; it may have 256 at most",
        ),
        (
            {"run": _env(KAGI_URL="a\x00b")},
            "run.env.KAGI_URL",
            "the value holds a NUL character",
        ),
        (
            {"run": _env(KAGI_API_KEY="secret:")},
            "run.env.KAGI_API_KEY",
            "the key after 'secret:' is not a secret key; use [a-z][a-z0-9_]{1,62}",
        ),
        (
            {"run": _run(args=["x" * 257])},
            "run.args[0]",
            "the word is 257 characters; it may have 256 at most",
        ),
        (
            {"arg_denies": [{"tools": [], "arg": "url", "values": ["x"]}]},
            "arg_denies[0].tools",
            "a fence entry needs at least one tool; use 'all' or name a tool",
        ),
        (
            {"arg_denies": _fence_values(["a\x00"])},
            "arg_denies[0].values[0]",
            "the value holds a NUL character",
        ),
        ({"tools": _tools(201)}, "tools", "tools holds 201 entries; it may hold 200 at most"),
        ({"run": _variables(33)}, "run.env", "run.env holds 33 entries; it may hold 32 at most"),
    ],
    ids=[
        "identity-count",
        "identity-blank",
        "description-blank",
        "package",
        "repo",
        "asset",
        "python",
        "entrypoint",
        "state-dir-env",
        "value-length",
        "value-nul",
        "secret-key",
        "word-length",
        "fence-with-no-tool",
        "fence-value-nul",
        "tools-count",
        "env-count",
    ],
)
def test_each_rule_of_root_has_one_message(changes: dict[str, Any], loc: str, message: str) -> None:
    assert _report(_changed(changes)) == (Issue(Severity.ERROR, loc, message),)


def test_an_identity_counts_each_character() -> None:
    body = _changed({"identity": "a" + " " * 400 + "b"})

    assert [issue.loc for issue in _report(body)] == ["identity"]


def test_a_rule_of_contract_01b_gives_one_issue() -> None:
    body = _changed({"install": {**PYPI, "version": "1.0.0-rc1"}, "identity": ""})

    assert [issue.loc for issue in _report(body)] == ["identity", "install.version"]


# --- the file name in a registry ---------------------------------------------------


def _registry(tmp_path: Path, files: dict[str, bytes], name: str = "kagi") -> Path:
    """A registry with one server directory and the named files."""
    directory = tmp_path / registry.MCP_DIR / name
    directory.mkdir(parents=True)
    for file, data in files.items():
        (directory / file).write_bytes(data)

    return tmp_path


def test_the_server_file_name_is_a_constant() -> None:
    assert registry.SERVER_FILE == "server.toml"


def test_the_registry_reads_a_toml_server_file(tmp_path: Path) -> None:
    root = _registry(tmp_path, {registry.SERVER_FILE: PYPI_TOML.encode()})

    loaded = load_registry(root)

    assert loaded.servers["kagi"] == parse_server(PYPI_TOML)[0]
    assert loaded.server_reports["kagi"].file == "mcp/kagi/server.toml"
    assert loaded.server_reports["kagi"].issues == ()


def test_a_server_yaml_is_no_server_file(tmp_path: Path) -> None:
    """The registry reads no `server.yaml`. A directory that holds one and no
    `server.toml` gets the warning of a directory with no file."""
    root = _registry(tmp_path, {"server.yaml": b"name: kagi\n"})

    loaded = load_registry(root)

    assert "kagi" not in loaded.servers
    assert loaded.server_reports["kagi"].issues == (Issue(Severity.WARNING, "<document>", NO_FILE),)


def test_a_server_directory_with_no_file_gets_a_warning(tmp_path: Path) -> None:
    (tmp_path / registry.MCP_DIR / "kagi").mkdir(parents=True)

    loaded = load_registry(tmp_path)

    assert loaded.server_reports["kagi"].issues == (Issue(Severity.WARNING, "<document>", NO_FILE),)
    assert loaded.server_reports["kagi"].file == "mcp/kagi/server.toml"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (PYPI_TOML.encode() + b"# \xff\n", NOT_UTF8),
        (b"\xef\xbb\xbf" + PYPI_TOML.encode(), "the file starts with a byte order mark"),
        (PYPI_TOML.encode() + b"# " + b"x" * 1_000_000 + b"\n", TOO_LARGE),
        (PYPI_TOML.encode() + b"name = 'twice'\n", NOT_TOML),
    ],
    ids=["no-utf8", "bom", "size", "duplicate-key"],
)
def test_the_registry_gives_each_refusal_of_the_reader_for_a_server(
    tmp_path: Path, data: bytes, message: str
) -> None:
    root = _registry(tmp_path, {registry.SERVER_FILE: data})

    loaded = load_registry(root)

    assert "kagi" not in loaded.servers
    assert loaded.server_reports["kagi"].issues == (Issue(Severity.ERROR, "<document>", message),)


def test_a_server_file_at_the_byte_limit_reaches_the_reader(tmp_path: Path) -> None:
    padding = 65536 - len(PYPI_TOML.encode()) - len(b"# \n")
    data = PYPI_TOML.encode() + b"# " + b"x" * padding + b"\n"
    root = _registry(tmp_path, {registry.SERVER_FILE: data})

    loaded = load_registry(root)

    assert len(data) == 65536
    assert "kagi" in loaded.servers


def test_the_registry_reads_at_most_one_byte_past_the_limit_of_a_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _registry(tmp_path, {registry.SERVER_FILE: b"x" * 200_000})
    calls: list[tuple[int, tomlfile.FileKind]] = []
    real = tomlfile.loads

    def loads(data: bytes, kind: tomlfile.FileKind) -> object:
        calls.append((len(data), kind))
        return real(data, kind)

    monkeypatch.setattr(tomlfile, "loads", loads)

    load_registry(root)

    assert calls == [(65537, tomlfile.FileKind.SERVER)]


# --- a family and its servers ------------------------------------------------------

FAMILY_TOML = """\
name = "helper"
kind = "attended"
description = "General assistant."
model.router = "fast"
model.budget_usd_per_day = 2.5
tools.kagi = ["kagi_search_fetch"]
tools.github-code = ["push_files"]
"""


def test_a_toml_family_finds_its_toml_servers(tmp_path: Path) -> None:
    root = _registry(tmp_path, {registry.SERVER_FILE: PYPI_TOML.encode()})
    _registry(tmp_path, {registry.SERVER_FILE: RELEASE_TOML.encode()}, name="github-code")
    family = tmp_path / registry.FAMILIES_DIR / "helper"
    family.mkdir(parents=True)
    (family / registry.FAMILY_FILE).write_text(FAMILY_TOML, encoding="utf-8")

    loaded = load_registry(root)

    assert sorted(loaded.servers) == ["github-code", "kagi"]
    assert [issue for issue in loaded.reports["helper"].issues if not issue.downgraded] == []
    assert loaded.ok
