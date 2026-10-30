"""`mcp/<name>/server.toml`, the TOML form of a server file, as root reads it.

Root reads `server.toml` when a directory holds that name, and `server.yaml`
when it does not. A directory that holds both names is a refusal. The TOML
text goes through `handover.tomlfile` with the file kind `SERVER`, and each
check after that read is the check of the YAML form.

Three files of `handover_mce_registry` give the TOML form of each source.
`bin/yaml-to-toml.py` wrote each one from the YAML text that this module
holds, and each pair must read to one `ServerFile`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.mcpserver import (
    SERVER_FILE_NAME,
    SERVER_FILE_NAME_YAML,
    Markup,
    ServerFile,
    Source,
    parse_server,
    read_registry,
    read_server_dir,
)
from handover.tomlfile import (
    BYTE_ORDER_MARK,
    DEPTH,
    NOT_TOML,
    NOT_UTF8,
    TOO_LARGE,
    FileKind,
)

#: The TOML files of the fixture registry.
FIXTURE = Path(__file__).resolve().parent / "handover_mce_registry" / "mcp"

#: The YAML form of three files of the fixture registry: one `pypi`, one
#: `github-release` and one `mcp` server. A line longer than 100
#: characters is folded, and the value of the fold is the value of the line.
YAML_FORMS = {
    "ha": """\
name: ha
identity: "The same Home Assistant user as ha-read, non-admin, with the same entity read
  allowlist. Its write reach is bounded by that HA user's own permissions."

install:
  source: pypi
  package: ha-mcp
  version: 8.4.3
  lock: mcp/ha/install.lock
  python: "3.13"

run:
  entrypoint: ha-mcp
  env:
    HOMEASSISTANT_URL: http://192.0.2.20:8123
    HOMEASSISTANT_TOKEN: secret:ha_read_token
    READ_ONLY_MODE: "false"
    ENABLE_WEBSOCKET: "true"
    HA_MCP_DISABLE_SETTINGS_UI: "true"
    HA_MCP_DISABLE_UPDATE_CHECK: "true"
  state_dir: true
  state_dir_env: HA_MCP_CONFIG_DIR

tools:
  - { name: ha_call_service, description: Call one Home Assistant service on one entity.,
      write: true }

shared_secrets:
  - secret: ha_read_token
    server: ha-read
""",
    "github-platform": """\
name: github-platform
identity: "Fine-grained PAT scoped to creche, registry and mcp, and to
  nothing else. Only the creche family is granted this server."

install:
  source: github-release
  repo: github/github-mcp-server
  version: 1.12.0
  asset: github-mcp-server_Linux_x86_64.tar.gz
  sha256: f34de295acd8f1012c7f2c0e3b909d87361d0993b9489b57ee92ac72b85d7cca

run:
  entrypoint: github-mcp-server
  args:
    - stdio
    - --toolsets=context,repos,pull_requests,issues
    - --lockdown-mode
  env:
    GITHUB_PERSONAL_ACCESS_TOKEN: secret:github_token_platform

arg_allows:
  - tools: all
    arg: owner/repo
    values:
      - example-owner/creche
      - example-owner/registry
      - example-owner/mcp

arg_denies:
  - tools:
      - push_files
      - create_or_update_file
      - delete_file
      - create_branch
      - update_pull_request_branch
    arg: branch
    values: [main, master]

tools:
  - { name: get_file_contents, description: Read one file at a ref., write: false }
  - { name: list_branches, description: List a repository's branches., write: false }
  - { name: list_commits, description: List commits on a ref., write: false }
  - { name: get_commit, description: Read one commit., write: false }
  - { name: list_pull_requests, description: List pull requests., write: false }
  - { name: pull_request_read, description: Read a pull request and its diff., write: false }
  - { name: list_issues, description: List a repository's issues., write: false }
  - { name: issue_read, description: Read one issue., write: false }
  - { name: create_branch, description: Create a branch from another branch., write: true }
  - { name: create_or_update_file, description: Write one file., write: true }
  - { name: push_files, description: Push several files in one commit., write: true }
  - { name: delete_file, description: Delete one file., write: true }
  - { name: create_pull_request, description: Open a pull request., write: true }
  - { name: update_pull_request, description: Change a pull request's fields., write: true }
  - { name: update_pull_request_branch, description: Update a pull request's branch., write: true }
  - { name: merge_pull_request, description: Merge a pull request., write: true }
  - { name: pull_request_review_write, description: Submit a pull request review., write: true }
  - { name: add_reply_to_pull_request_comment, description: Reply to a review comment.,
      write: true }
  - { name: add_issue_comment, description: Comment on an issue., write: true }
  - { name: issue_write, description: Open or change one issue., write: true }
""",
    "mail": """\
name: mail
identity: "One SMTP relay account at smtp.example.invalid. It sends as bot@example.invalid
  to operator@example.invalid and to nobody else, because both addresses are env here and
  neither is a tool argument."

install:
  source: agent-mcp

run:
  entrypoint: mail-mcp
  env:
    MAIL_SMTP_HOST: smtp.example.invalid
    MAIL_SMTP_PORT: "587"
    MAIL_STARTTLS: "yes"
    MAIL_FROM: bot@example.invalid
    MAIL_TO: operator@example.invalid
    MAIL_SMTP_USER: smtp-user
    MAIL_SMTP_PASSWORD: secret:smtp_password

tools:
  - { name: send_standup_email, description: "Send the operator one plain-text email. The
      subject and body are yours, the envelope is not.", write: true }
""",
}

#: A small TOML server file that root installs from.
KAGI_TOML = """\
name = "kagi"
identity = "The fleet's Kagi API key."

install.source = "pypi"
install.package = "kagimcp"
install.version = "1.0.2"
install.lock = "mcp/kagi/install.lock"
install.python = "3.12"

run.entrypoint = "kagimcp"
run.env.KAGI_API_KEY = "secret:kagi_api_key"

tools = [
    { name = "kagi_search_fetch", description = "Search the web.", write = false },
]
"""

#: The same server in YAML.
KAGI_YAML = """\
name: kagi
identity: "The fleet's Kagi API key."
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
    description: Search the web.
    write: false
"""

#: A second server in TOML, for a registry with two servers.
VIKUNJA_TOML = """\
name = "vikunja-finance"
identity = "The Vikunja bot bot-finance."

install.source = "agent-mcp"

run.entrypoint = "vikunja-mcp"
run.env.VIKUNJA_TOKEN = "secret:vikunja_token_finance"

tools = [
    { name = "get_ticket", description = "Read one ticket.", write = false },
]
"""

#: The refusal of a directory that holds both names.
BOTH_NAMES = f"holds {SERVER_FILE_NAME} and {SERVER_FILE_NAME_YAML}; keep one"

#: A text that a refusal must never hold.
MARKER = "do-not-echo-this-value"


def _toml_refusal(text: bytes | str, directory: str = "kagi") -> Refusal:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    with pytest.raises(Refusal) as caught:
        parse_server(raw, directory, Markup.TOML)

    assert caught.value.code is RefusalCode.SERVER
    assert caught.value.subject == f"mcp/{directory}"

    return caught.value


def _dir_refusal(directory: Path) -> Refusal:
    with pytest.raises(Refusal) as caught:
        read_server_dir(directory)

    assert caught.value.code is RefusalCode.SERVER
    assert caught.value.subject == f"mcp/{directory.name}"

    return caught.value


def _server_dir(root: Path, name: str = "kagi") -> Path:
    directory = root / name
    directory.mkdir(parents=True)

    return directory


def _padded(text: str, size: int) -> bytes:
    """`text` with one comment line at its end, `size` bytes in all."""
    room = size - len(text.encode("utf-8")) - len("#\n")
    assert room >= 0

    return f"{text}#{'a' * room}\n".encode()


def _args_of_levels(levels: int) -> str:
    """`KAGI_TOML` whose `run.args` has a table or an array at `levels`.

    The top table is level 1, `run` is level 2 and `run.args` is level 3.
    Each array inside `run.args` adds one level."""
    inner = levels - 3
    return f'{KAGI_TOML}run.args = {"[" * (inner + 1)}"stdio"{"]" * (inner + 1)}\n'


# -- the TOML form reads as the YAML form ---------------------------------------


@pytest.mark.parametrize("name", sorted(YAML_FORMS))
def test_a_toml_file_of_each_source_reads_as_its_yaml_form(name: str) -> None:
    toml_form = (FIXTURE / name / SERVER_FILE_NAME).read_bytes()

    from_toml = parse_server(toml_form, name, Markup.TOML)
    from_yaml = parse_server(YAML_FORMS[name].encode("utf-8"), name, Markup.YAML)

    assert from_toml == from_yaml


def test_the_three_files_hold_each_source() -> None:
    sources = {
        parse_server((FIXTURE / name / SERVER_FILE_NAME).read_bytes(), name, Markup.TOML).pin.source
        for name in YAML_FORMS
    }

    assert sources == set(Source)


def test_the_small_toml_file_reads_as_its_yaml_form() -> None:
    from_toml = parse_server(KAGI_TOML.encode("utf-8"), "kagi", Markup.TOML)

    assert from_toml == parse_server(KAGI_YAML.encode("utf-8"), "kagi", Markup.YAML)


def test_the_parse_reads_yaml_when_no_markup_is_given() -> None:
    assert parse_server(KAGI_YAML.encode("utf-8"), "kagi") == parse_server(
        KAGI_YAML.encode("utf-8"), "kagi", Markup.YAML
    )


def test_the_fixture_registry_holds_eleven_toml_files() -> None:
    entries = sorted(FIXTURE.glob("*/*"))

    assert len(entries) == 11
    assert {path.name for path in entries} == {SERVER_FILE_NAME}


# -- the two names --------------------------------------------------------------


def test_the_two_names_are_two_constants() -> None:
    assert (SERVER_FILE_NAME, SERVER_FILE_NAME_YAML) == ("server.toml", "server.yaml")
    assert Markup.TOML.file_name == SERVER_FILE_NAME
    assert Markup.YAML.file_name == SERVER_FILE_NAME_YAML


def test_a_directory_with_a_toml_file_reads_it(tmp_path: Path) -> None:
    directory = _server_dir(tmp_path)
    (directory / SERVER_FILE_NAME).write_text(KAGI_TOML, encoding="utf-8")

    assert read_server_dir(directory) == parse_server(KAGI_YAML.encode("utf-8"), "kagi")


def test_a_directory_with_a_yaml_file_reads_it(tmp_path: Path) -> None:
    directory = _server_dir(tmp_path)
    (directory / SERVER_FILE_NAME_YAML).write_text(KAGI_YAML, encoding="utf-8")

    assert read_server_dir(directory) == parse_server(
        KAGI_TOML.encode("utf-8"), "kagi", Markup.TOML
    )


def test_a_directory_with_both_names_is_refused(tmp_path: Path) -> None:
    """Two good files of one server are still a refusal: root reads no
    file of such a directory."""
    directory = _server_dir(tmp_path)
    (directory / SERVER_FILE_NAME).write_text(KAGI_TOML, encoding="utf-8")
    (directory / SERVER_FILE_NAME_YAML).write_text(KAGI_YAML, encoding="utf-8")

    assert _dir_refusal(directory).detail == BOTH_NAMES


def _good_file(directory: Path, name: str) -> None:
    text = KAGI_TOML if name == SERVER_FILE_NAME else KAGI_YAML
    (directory / name).write_text(text, encoding="utf-8")


def _link(directory: Path, name: str) -> None:
    elsewhere = directory.parent / f"elsewhere-{name}"
    _good_file(elsewhere.parent, elsewhere.name)
    (directory / name).symlink_to(elsewhere)


def _dangling_link(directory: Path, name: str) -> None:
    (directory / name).symlink_to(directory.parent / "absent")


def _directory(directory: Path, name: str) -> None:
    (directory / name).mkdir()


ODD_ENTRIES = {
    "link": _link,
    "dangling-link": _dangling_link,
    "directory": _directory,
}


@pytest.mark.parametrize("odd", sorted(ODD_ENTRIES))
@pytest.mark.parametrize("name", [SERVER_FILE_NAME, SERVER_FILE_NAME_YAML])
def test_both_names_are_refused_when_one_is_no_regular_file(
    tmp_path: Path, name: str, odd: str
) -> None:
    """Each entry of a name counts: a link, a link to no file and a
    directory too."""
    directory = _server_dir(tmp_path / "mcp")
    other = SERVER_FILE_NAME_YAML if name == SERVER_FILE_NAME else SERVER_FILE_NAME
    _good_file(directory, other)
    ODD_ENTRIES[odd](directory, name)

    assert _dir_refusal(directory).detail == BOTH_NAMES


@pytest.mark.parametrize("odd", sorted(ODD_ENTRIES))
@pytest.mark.parametrize("name", [SERVER_FILE_NAME, SERVER_FILE_NAME_YAML])
def test_a_name_that_is_no_regular_file_is_refused(tmp_path: Path, name: str, odd: str) -> None:
    """A link is refused for each name. The link is never followed."""
    directory = _server_dir(tmp_path / "mcp")
    ODD_ENTRIES[odd](directory, name)

    assert _dir_refusal(directory).detail == f"{name} is missing or is not a regular file"


def test_a_directory_with_no_file_is_refused_as_before(tmp_path: Path) -> None:
    directory = _server_dir(tmp_path)

    assert _dir_refusal(directory).detail == "server.yaml is missing or is not a regular file"


def test_one_registry_reads_both_markups(tmp_path: Path) -> None:
    _server_dir(tmp_path, "kagi").joinpath(SERVER_FILE_NAME_YAML).write_text(KAGI_YAML, "utf-8")
    vikunja = _server_dir(tmp_path, "vikunja-finance")
    vikunja.joinpath(SERVER_FILE_NAME).write_text(VIKUNJA_TOML, "utf-8")

    found = read_registry(tmp_path, limit=10)

    assert [one.name for one in found] == ["kagi", "vikunja-finance"]
    assert found[1].pin.source is Source.AGENT_MCP


def test_one_pair_of_shared_secrets_spans_both_markups(tmp_path: Path) -> None:
    """`ha` declares the pair in YAML and `ha-read` in TOML. The pair
    holds across the two forms."""
    _server_dir(tmp_path, "ha").joinpath(SERVER_FILE_NAME_YAML).write_text(
        YAML_FORMS["ha"], "utf-8"
    )
    ha_read = (FIXTURE / "ha-read" / SERVER_FILE_NAME).read_bytes()
    _server_dir(tmp_path, "ha-read").joinpath(SERVER_FILE_NAME).write_bytes(ha_read)

    found = read_registry(tmp_path, limit=10)

    assert [one.secrets for one in found] == [("ha_read_token",), ("ha_read_token",)]


# -- the limits of the reader ---------------------------------------------------


def test_a_toml_file_past_the_byte_limit_is_refused() -> None:
    limit = FileKind.SERVER.byte_limit
    refusal = _toml_refusal(_padded(KAGI_TOML, limit + 1))

    assert refusal.detail == f"server.toml: {TOO_LARGE.format(limit=limit)}"


def test_a_toml_file_at_the_byte_limit_is_read() -> None:
    text = _padded(KAGI_TOML, FileKind.SERVER.byte_limit)

    assert parse_server(text, "kagi", Markup.TOML).name == "kagi"


#: Each text that the reader refuses, with the text of its refusal.
READER_REFUSALS = {
    "no-utf8": (KAGI_TOML.encode("utf-8") + b"# \xff\n", NOT_UTF8),
    "byte-order-mark": (("\ufeff" + KAGI_TOML).encode("utf-8"), BYTE_ORDER_MARK),
    "syntax": ((KAGI_TOML + f"extra = {MARKER}\n").encode("utf-8"), NOT_TOML),
    "key-two-times": ((KAGI_TOML + 'name = "kagi"\n').encode("utf-8"), NOT_TOML),
    "integer-past-64-bits": (
        (KAGI_TOML + "extra = 9223372036854775808\n").encode("utf-8"),
        NOT_TOML,
    ),
    "float-past-its-range": ((KAGI_TOML + "extra = 1e999\n").encode("utf-8"), NOT_TOML),
    "lone-surrogate": ((KAGI_TOML + 'extra = "\\ud800"\n').encode("utf-8"), NOT_TOML),
    "comma-before-brace": (
        KAGI_TOML.replace("write = false }", "write = false, }").encode("utf-8"),
        NOT_TOML,
    ),
    "levels-past-the-limit": (_args_of_levels(DEPTH + 1).encode("utf-8"), NOT_TOML),
}


@pytest.mark.parametrize("case", sorted(READER_REFUSALS))
def test_each_refusal_of_the_reader_is_one_refusal(case: str) -> None:
    raw, message = READER_REFUSALS[case]

    assert _toml_refusal(raw).detail == f"server.toml: {message}"


def test_a_refusal_of_the_reader_holds_no_text_of_the_file() -> None:
    refusal = _toml_refusal(KAGI_TOML + f"extra = {MARKER}\n")

    assert MARKER not in refusal.detail
    assert MARKER not in str(refusal)


def test_a_text_at_the_level_limit_reaches_the_checks_of_the_file() -> None:
    """The reader takes 8 levels. The check of `run.args` then refuses a
    word that is no text."""
    refusal = _toml_refusal(_args_of_levels(DEPTH))

    assert refusal.detail == "run.args holds a word that is not plain text"


# -- the checks after the read --------------------------------------------------


def test_a_missing_key_names_the_toml_file() -> None:
    refusal = _toml_refusal(KAGI_TOML.replace('name = "kagi"\n', ""))

    assert refusal.detail == "server.toml has no name"


def test_an_unknown_key_names_the_toml_file() -> None:
    refusal = _toml_refusal(KAGI_TOML + 'post_install = "x"\n')

    assert refusal.detail == "server.toml has unknown key post_install"


#: A value of a TOML type that the field does not take, with the refusal.
WRONG_TYPES = {
    "date-time-identity": (
        ('identity = "The fleet\'s Kagi API key."', "identity = 1979-05-27T07:32:00Z"),
        "field 'identity' is malformed",
    ),
    "integer-version": (
        ('install.version = "1.0.2"', "install.version = 1"),
        "field 'install.version' is malformed",
    ),
    "float-python": (
        ('install.python = "3.12"', "install.python = 3.12"),
        "install.python is not one of the two interpreters in use",
    ),
    "text-state-dir": (
        ('run.entrypoint = "kagimcp"', 'run.entrypoint = "kagimcp"\nrun.state_dir = "true"'),
        "run.state_dir is not a boolean",
    ),
    "table-tools": (
        (KAGI_TOML[KAGI_TOML.index("tools = [") :], 'tools.one = "x"\n'),
        "field 'tools' is not a list",
    ),
}


@pytest.mark.parametrize("case", sorted(WRONG_TYPES))
def test_a_value_of_a_wrong_toml_type_is_refused(case: str) -> None:
    (old, new), message = WRONG_TYPES[case]
    assert old in KAGI_TOML

    assert _toml_refusal(KAGI_TOML.replace(old, new, 1)).detail == message


def test_a_credential_value_is_refused_and_not_echoed() -> None:
    text = KAGI_TOML.replace('"secret:kagi_api_key"', f'"{MARKER}"')

    refusal = _toml_refusal(text)

    assert refusal.detail == "run.env[KAGI_API_KEY] must name a secret, not a value"
    assert MARKER not in str(refusal)


def test_a_toml_file_whose_name_is_not_its_directory_is_refused() -> None:
    assert _toml_refusal(KAGI_TOML, "kagi-two").detail == (
        "field 'name' differs from the directory name"
    )


def test_the_parse_gives_a_server_file() -> None:
    server = parse_server(KAGI_TOML.encode("utf-8"), "kagi", Markup.TOML)

    assert isinstance(server, ServerFile)
    assert server.tools == ("kagi_search_fetch",)
    assert server.secrets == ("kagi_api_key",)
