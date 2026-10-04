"""Every fence in `serverrules.py` refuses with its own message (contract 01b).

One parametrized test per checked function, in the file's own order. `kagi`
(a `pypi` install) and `github-code` (a `github-release` install) are the two
base fixtures; `mail` covers the third source, `agent-mcp`."""

from __future__ import annotations

from typing import Any

import pytest
from agent_family.parse import parse_server
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


# --- run: the process (contract 01b §4) --------------------------------------


def test_entrypoint_may_carry_no_path() -> None:
    report = check_server_rules("kagi", run={"entrypoint": "bin/kagimcp", "env": {}})
    assert "must be a console script name with no path" in messages(report)


def test_entrypoint_may_not_be_empty() -> None:
    report = check_server_rules("kagi", run={"entrypoint": "", "env": {}})
    assert "must be a console script name with no path" in messages(report)


def test_env_var_name_must_match_the_shell_pattern() -> None:
    report = check_server_rules("kagi", run={"entrypoint": "kagimcp", "env": {"bad-name": "x"}})
    assert "is not an environment variable name" in messages(report)


def test_a_secret_shaped_name_must_use_the_secret_prefix() -> None:
    report = check_server_rules(
        "kagi", run={"entrypoint": "kagimcp", "env": {"KAGI_API_KEY": "sk-literal-value"}}
    )
    assert "puts a credential in git" in messages(report)


def test_a_secret_prefix_must_name_a_key() -> None:
    report = check_server_rules(
        "kagi", run={"entrypoint": "kagimcp", "env": {"KAGI_API_KEY": "secret:"}}
    )
    assert "'secret:' names no key" in messages(report)


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


def test_a_composite_fence_arg_is_allowed() -> None:
    """§7.2: `owner/repo` is the documented composite form, not a mistake."""
    report = check_server_rules(
        "github-code", arg_allows=[{"tools": "all", "arg": "owner/repo", "values": ["a/b"]}]
    )
    assert not errors(report)


# --- a text with no value (contract 01 §7, invariant 19) ---------------------

NO_VALUE_TEXTS = {
    "decimal-integer": "tools: " + "9" * 5000 + "\n",
    "base-16-integer": "tools: 0x" + "f" * 4000 + "\n",
    "date": "tools: 2001-02-30\n",
    "deep-nesting": "tools: " + "[" * 10_000 + "]" * 10_000 + "\n",
}


@pytest.mark.parametrize("case", NO_VALUE_TEXTS)
def test_a_server_text_with_no_value_is_refused(case: str) -> None:
    server, issues = parse_server(server_text("kagi") + NO_VALUE_TEXTS[case])
    assert server is None
    assert [issue.loc for issue in issues] == ["<document>"]
    assert "YAML will not parse" in issues[0].msg
