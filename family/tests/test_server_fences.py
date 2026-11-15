"""Every fence in `serverrules.py` refuses with its own message (contract 01b).

One parametrized test per checked function, in the file's own order. `kagi`
(a `pypi` install) and `github-code` (a `github-release` install) are the two
base fixtures; `mail` covers the third source, `agent-mcp`, and
`forgejo-code` the fourth, `forge-release`."""

from __future__ import annotations

from typing import Any

import pytest
from agent_family.parse import Markup, parse_server
from family_helpers import check_server_rules, errors, messages, server_text

# --- name and identity (contract 01b §1, §6) ---------------------------------

NAME_CASES: tuple[tuple[dict[str, Any], str], ...] = (
    ({"name": "Kagi"}, "not a server name"),
    ({"name": "other-name"}, "does not match its directory 'mcp/kagi'"),
    ({"identity": ""}, "identity is 0 characters"),
    ({"identity": "x" * 301}, "identity is 301 characters"),
)


@pytest.mark.parametrize(("changes", "expect"), NAME_CASES)
def test_name_and_identity_fences(changes: dict[str, Any], expect: str) -> None:
    report = check_server_rules("kagi", **changes)
    assert expect in messages(report)


# --- install: the pin (contract 01b §3) --------------------------------------


def test_unknown_source_is_an_error() -> None:
    report = check_server_rules("kagi", install={"source": "ftp", "package": "x", "version": "1"})
    assert "is not a source" in messages(report)


def test_missing_required_pin_field_is_an_error() -> None:
    report = check_server_rules("kagi", install={"source": "pypi", "package": "kagimcp"})
    assert "requires 'install.version'" in messages(report)


def test_pypi_requires_a_committed_lock() -> None:
    """Contract 01b §3.4. The field was in the contract, in the design's
    §4.2 table and in `handover`'s reader, and in no schema here, so
    this validator refused every real `pypi` file."""
    report = check_server_rules(
        "kagi", install={"source": "pypi", "package": "kagimcp", "version": "1.0.2"}
    )
    assert "requires 'install.lock'" in messages(report)


LOCK_CASES: tuple[tuple[str, str], ...] = (
    ("/etc/shadow", "is not a repository path"),
    ("mcp/kagi/../../etc/passwd", "not a plain repository path"),
    ("mcp/kagi/", "not a plain repository path"),
    ("mcp//kagi/install.lock", "not a plain repository path"),
)


@pytest.mark.parametrize(("lock", "expect"), LOCK_CASES)
def test_a_lock_path_may_not_leave_the_registry(lock: str, expect: str) -> None:
    """§4.1 step 2: CI refuses a traversing path before the byte reaches
    the host. The executor refuses it again — this is the first refusal."""
    report = check_server_rules(
        "kagi",
        install={"source": "pypi", "package": "kagimcp", "version": "1.0.2", "lock": lock},
    )
    assert expect in messages(report)


def test_agent_mcp_takes_no_pin_field_of_its_own() -> None:
    """Contract 01b §3.3. The `mcp-servers`
    component tag is the version, so a `ref` here is a second version
    root could only refuse."""
    assert check_server_rules("mail", install={"source": "agent-mcp"}).ok

    report = check_server_rules("mail", install={"source": "agent-mcp", "ref": "v0.4.6"})

    assert "does not take 'install.ref'" in messages(report)


def test_a_shared_secret_must_be_one_this_file_names() -> None:
    """Contract 01b §4.3, the half one file can be read for. The
    agreement between the two files is registry-wide and root checks it
    (`handover.mcpserver._one_upstream_per_secret`)."""
    report = check_server_rules(
        "ha-read", shared_secrets=[{"secret": "ha_token_write", "server": "ha"}]
    )
    assert "is not a secret this file's run.env names" in messages(report)


def test_a_server_may_not_share_a_secret_with_itself() -> None:
    report = check_server_rules(
        "ha-read", shared_secrets=[{"secret": "ha_token_read", "server": "ha-read"}]
    )
    assert "cannot agree with itself" in messages(report)


def test_one_secret_may_have_one_partner() -> None:
    """§4.3 rule 4: two entries for one name are two partners, and the
    file does not say which."""
    report = check_server_rules(
        "ha-read",
        shared_secrets=[
            {"secret": "ha_token_read", "server": "ha"},
            {"secret": "ha_token_read", "server": "kagi"},
        ],
    )
    assert "one secret has one partner" in messages(report)


def test_a_declared_pair_is_clean() -> None:
    report = check_server_rules(
        "ha-read", shared_secrets=[{"secret": "ha_token_read", "server": "ha"}]
    )
    assert report.ok, messages(report)


def test_a_field_from_another_source_is_an_error() -> None:
    report = check_server_rules(
        "kagi", install={"source": "pypi", "package": "kagimcp", "version": "1.0.2", "repo": "a/b"}
    )
    assert "does not take 'install.repo'" in messages(report)


def test_version_must_be_exact() -> None:
    report = check_server_rules(
        "kagi", install={"source": "pypi", "package": "kagimcp", "version": "^1.0"}
    )
    assert "is not an exact version" in messages(report)


VERSION_REFUSED: tuple[str, ...] = ("1.0.0-rc1", "1.0.0+local", "1" * 65)


@pytest.mark.parametrize("version", VERSION_REFUSED)
def test_a_version_takes_no_sign_and_64_characters(version: str) -> None:
    """The validator and root read one grammar for a version."""
    report = check_server_rules(
        "kagi", install={"source": "pypi", "package": "kagimcp", "version": version}
    )
    expect = f"'{version}' is not an exact version; use [0-9][0-9A-Za-z.]{{0,63}}"
    assert expect in messages(report)


def test_a_version_of_64_characters_is_exact() -> None:
    report = check_server_rules(
        "kagi", install={"source": "pypi", "package": "kagimcp", "version": "1" * 64}
    )
    assert "is not an exact version" not in messages(report)


def test_sha256_must_be_64_hex_characters() -> None:
    report = check_server_rules(
        "github-code",
        install={
            "source": "github-release",
            "repo": "octocat/hello",
            "version": "1.0.0",
            "asset": "x.tar.gz",
            "sha256": "deadbeef",
        },
    )
    assert "sha256 must be 64 lowercase hexadecimal characters" in messages(report)


def test_repo_must_be_owner_slash_name() -> None:
    report = check_server_rules(
        "github-code",
        install={
            "source": "github-release",
            "repo": "not-a-repo-path",
            "version": "1.0.0",
            "asset": "x.tar.gz",
            "sha256": "f" * 64,
        },
    )
    assert "is not <owner>/<name> on GitHub" in messages(report)


# --- install: a release asset of a Forgejo or Gitea host ----------------------

#: The pin of the fixture `forgejo-code`: forgejo-mcp 3.4.1, from the host
#: that publishes it.
FORGE: dict[str, Any] = {
    "source": "forge-release",
    "host": "git.b4mad.industries",
    "repo": "agentic-forges/forgejo-mcp",
    "version": "3.4.1",
    "asset": "forgejo-mcp_3.4.1_linux_amd64.tar.gz",
    "sha256": "940d3ed08a5535bf1baaf426cb93610cfef5ad02adbda76b7c70a618f08fa53c",
}

HOST_RULE = (
    "is not a host; use a DNS name in lower case, with no scheme, no port, no path, "
    "no user and no IP address"
)

#: One value for each rule of `install.host`, by the rule that it breaks.
HOST_REFUSED: dict[str, str] = {
    "a-scheme": "https://git.example.org",
    "a-port": "git.example.org:3000",
    "a-path": "git.example.org/owner",
    "a-user": "bot@git.example.org",
    "a-user-and-a-password": "bot:word@git.example.org",
    "an-ipv4-address": "192.0.2.10",
    "an-ipv4-address-in-hexadecimal": "0xc0.0.2.10",
    "an-ipv4-address-as-one-number": "3221225994",
    "an-ipv6-address": "[2001:db8::1]",
    "upper-case": "Git.Example.org",
    "one-label": "localhost",
    "an-end-dot": "git.example.org.",
    "an-empty-label": "git..example.org",
    "a-label-that-starts-with-a-hyphen": "-git.example.org",
    "a-label-that-ends-with-a-hyphen": "git-.example.org",
    "a-label-of-64-characters": f"{'a' * 64}.example.org",
    "an-underscore": "git_forge.example.org",
    "a-space": "git.example.org evil.example.org",
    "a-line-end": "git.example.org\n",
    "a-query": "git.example.org?x=1",
    "an-empty-text": "",
}

HOST_ACCEPTED: tuple[str, ...] = (
    "git.b4mad.industries",
    "codeberg.org",
    "forge.example.org",
    "a.b.c.d.example.org",
    "xn--bcher-kva.example",
    "1forge.example.org",
    f"{'a' * 63}.example.org",
)


def test_the_forge_release_fixture_is_clean() -> None:
    report = check_server_rules("forgejo-code")

    assert report.ok, messages(report)


@pytest.mark.parametrize("host", HOST_ACCEPTED)
def test_a_lower_case_dns_name_is_a_host(host: str) -> None:
    report = check_server_rules("forgejo-code", install={**FORGE, "host": host})

    assert report.ok, messages(report)


@pytest.mark.parametrize("case", list(HOST_REFUSED))
def test_each_rule_of_a_host_refuses_a_value(case: str) -> None:
    report = check_server_rules("forgejo-code", install={**FORGE, "host": HOST_REFUSED[case]})

    assert [(issue.loc, HOST_RULE in issue.msg) for issue in errors(report)] == [
        ("install.host", True)
    ]


def test_a_host_has_253_characters_at_most() -> None:
    label = "a" * 49
    at_the_cap = ".".join([label] * 5) + ".org"
    past_the_cap = f"a{at_the_cap}"

    assert len(at_the_cap) == 253
    assert check_server_rules("forgejo-code", install={**FORGE, "host": at_the_cap}).ok
    report = check_server_rules("forgejo-code", install={**FORGE, "host": past_the_cap})
    assert "the host is 254 characters; it may have 253 at most" in messages(report)


def test_the_host_of_github_release_is_not_a_forge_host() -> None:
    report = check_server_rules("forgejo-code", install={**FORGE, "host": "github.com"})

    assert [(issue.loc, issue.msg) for issue in errors(report)] == [
        ("install.host", "'github.com' takes the source 'github-release', not 'forge-release'")
    ]


def test_a_forge_release_requires_a_host() -> None:
    install = {key: value for key, value in FORGE.items() if key != "host"}
    report = check_server_rules("forgejo-code", install=install)

    assert "source 'forge-release' requires 'install.host'" in messages(report)


def test_a_github_release_takes_no_host() -> None:
    install = {**FORGE, "source": "github-release"}
    report = check_server_rules("forgejo-code", install=install)

    assert "source 'github-release' does not take 'install.host'" in messages(report)


def test_a_forge_release_takes_a_format() -> None:
    report = check_server_rules("forgejo-code", install={**FORGE, "format": "binary"})

    assert report.ok, messages(report)


def test_a_forge_release_repo_must_be_owner_slash_name() -> None:
    report = check_server_rules("forgejo-code", install={**FORGE, "repo": "not-a-repo-path"})

    assert "is not <owner>/<name> on the host" in messages(report)


# --- run: the process (contract 01b §4) --------------------------------------


def test_entrypoint_may_carry_no_path() -> None:
    report = check_server_rules("kagi", run={"entrypoint": "bin/kagimcp", "env": {}})
    assert "'bin/kagimcp' is not a console script name" in messages(report)


def test_entrypoint_may_not_be_empty() -> None:
    report = check_server_rules("kagi", run={"entrypoint": "", "env": {}})
    assert "'' is not a console script name" in messages(report)


def test_env_var_name_must_match_the_shell_pattern() -> None:
    report = check_server_rules("kagi", run={"entrypoint": "kagimcp", "env": {"bad-name": "x"}})
    assert "is not an environment variable name" in messages(report)


def test_an_env_var_name_has_64_characters_at_most() -> None:
    """The validator and root read one grammar for the name of a variable."""
    short, long = "A" * 64, "A" * 65
    report = check_server_rules("kagi", run={"entrypoint": "kagimcp", "env": {short: "x"}})
    assert "is not an environment variable name" not in messages(report)

    report = check_server_rules("kagi", run={"entrypoint": "kagimcp", "env": {long: "x"}})
    expect = f"'{long}' is not an environment variable name; use [A-Z][A-Z0-9_]{{0,63}}"
    assert expect in messages(report)


def test_a_secret_shaped_name_must_use_the_secret_prefix() -> None:
    report = check_server_rules(
        "kagi", run={"entrypoint": "kagimcp", "env": {"KAGI_API_KEY": "sk-literal-value"}}
    )
    assert "puts a credential in git" in messages(report)


def test_a_secret_prefix_must_name_a_key() -> None:
    report = check_server_rules(
        "kagi", run={"entrypoint": "kagimcp", "env": {"KAGI_API_KEY": "secret:"}}
    )
    assert "the key after 'secret:' is not a secret key" in messages(report)


def test_state_dir_true_requires_state_dir_env() -> None:
    report = check_server_rules(
        "kagi",
        run={"entrypoint": "kagimcp", "env": {"KAGI_API_KEY": "secret:x"}, "state_dir": True},
    )
    assert "state_dir: true requires 'run.state_dir_env'" in messages(report)


def test_state_dir_env_requires_state_dir_true() -> None:
    report = check_server_rules(
        "kagi",
        run={
            "entrypoint": "kagimcp",
            "env": {"KAGI_API_KEY": "secret:x"},
            "state_dir_env": "STATE_DIR",
        },
    )
    assert "state_dir_env needs 'run.state_dir: true'" in messages(report)


# --- tools: the catalog (contract 01b §5) ------------------------------------

TOOLS_CASES: tuple[tuple[list[dict[str, Any]], str], ...] = (
    ([{"name": "bad tool", "description": "x"}], "not a tool name"),
    (
        [{"name": "x", "description": "a"}, {"name": "x", "description": "b"}],
        "is declared twice in this file",
    ),
    ([{"name": "x", "description": ""}], "description is 0 characters"),
    ([{"name": "x", "description": "y" * 201}], "description is 201 characters"),
)


@pytest.mark.parametrize(("tools", "expect"), TOOLS_CASES)
def test_tool_catalog_fences(tools: list[dict[str, Any]], expect: str) -> None:
    report = check_server_rules("kagi", tools=tools)
    assert expect in messages(report)


def test_a_declared_tool_name_has_64_characters_at_most() -> None:
    """The validator and root read one grammar for a tool name."""
    short, long = "a" * 64, "a" * 65
    report = check_server_rules("kagi", tools=[{"name": short, "description": "x"}])
    assert "is not a tool name" not in messages(report)

    report = check_server_rules("kagi", tools=[{"name": long, "description": "x"}])
    assert f"'{long}' is not a tool name; use [A-Za-z][A-Za-z0-9_-]{{0,63}}" in messages(report)


# --- fences: arg_allows and arg_denies (contract 01b §7) ---------------------


def test_fence_tools_must_be_a_list_or_all() -> None:
    report = check_server_rules("kagi", arg_allows=[{"tools": "some", "arg": "x", "values": ["a"]}])
    assert "must be a list of tool names or 'all'" in messages(report)


def test_fence_tools_list_must_be_declared() -> None:
    report = check_server_rules(
        "github-code",
        arg_denies=[{"tools": ["no_such_tool"], "arg": "branch", "values": ["main"]}],
    )
    assert "are not declared in this file's 'tools'" in messages(report)


FENCE_ARG_CASES = (
    {"tools": "all", "arg": "a/b/c", "values": ["x"]},
    {"tools": "all", "arg": "1bad", "values": ["x"]},
    # §7.2: an argument name has 64 characters at most.
    {"tools": "all", "arg": "a" * 65, "values": ["x"]},
    {"tools": "all", "arg": "owner/" + "b" * 65, "values": ["x"]},
)


@pytest.mark.parametrize("entry", FENCE_ARG_CASES)
def test_fence_arg_must_be_one_name_or_two_joined_by_slash(entry: dict[str, Any]) -> None:
    report = check_server_rules("kagi", arg_allows=[entry])
    assert "must be one argument name, or two joined by '/'" in messages(report)


def test_fence_needs_at_least_one_value() -> None:
    report = check_server_rules(
        "github-code", arg_allows=[{"tools": "all", "arg": "owner/repo", "values": []}]
    )
    assert "a fence entry needs at least one value" in messages(report)


def test_a_fence_arg_name_of_64_characters_is_allowed() -> None:
    entry = {"tools": "all", "arg": "a" * 64 + "/" + "b" * 64, "values": ["x"]}
    assert not errors(check_server_rules("kagi", arg_allows=[entry]))


@pytest.mark.parametrize("field", ["arg_allows", "arg_denies"])
def test_a_fence_over_all_needs_a_declared_tool(field: str) -> None:
    """§7.1: `all` on a file with no tool covers nothing."""
    entry = {"tools": "all", "arg": "branch", "values": ["main"]}
    report = check_server_rules("kagi", tools=[], **{field: [entry]})
    assert f"{field}[0].tools: 'all' needs at least one tool" in messages(report)


def test_a_fence_over_all_is_allowed_with_a_declared_tool() -> None:
    entry = {"tools": "all", "arg": "branch", "values": ["main"]}
    assert not errors(check_server_rules("kagi", arg_denies=[entry]))


def test_a_composite_fence_arg_is_allowed() -> None:
    """§7.2: `owner/repo` is the documented composite form, not a mistake."""
    report = check_server_rules(
        "github-code", arg_allows=[{"tools": "all", "arg": "owner/repo", "values": ["a/b"]}]
    )
    assert not errors(report)


# --- a text with no value (contract 01 §7, invariant 19) ---------------------

NO_VALUE_TEXTS = {
    "decimal-integer": "padding = " + "9" * 5000 + "\n",
    "base-16-integer": "padding = 0x" + "f" * 4000 + "\n",
    "date": "padding = 2001-02-30\n",
    "deep-nesting": "padding = " + "[" * 10_000 + "]" * 10_000 + "\n",
}


@pytest.mark.parametrize("case", NO_VALUE_TEXTS)
def test_a_server_text_with_no_value_is_refused(case: str) -> None:
    server, issues = parse_server(server_text("kagi") + NO_VALUE_TEXTS[case], Markup.TOML)
    assert server is None
    assert [issue.loc for issue in issues] == ["<document>"]
    assert "not TOML 1.0" in issues[0].msg
