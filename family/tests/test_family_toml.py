"""A TOML family file: the strict read of its types, and the two file names.

The YAML path keeps its lax read. The TOML path reads each field in the
strict mode of pydantic: a value of another type is a refusal, and an integer
for a float field is the one conversion. Each test holds its own texts."""

from __future__ import annotations

from pathlib import Path

import agent_family
import pytest
from agent_family import Markup, load_registry, parse_family, registry, tomlfile
from agent_family.report import Issue, Severity

THIN_YAML = """\
name: oracle
kind: thin
description: Answers questions about the vault.
model: { router: fast, budget_usd_per_day: 2.5 }
files:
  - { path: /srv/agents/vault, mode: ro }
egress: []
"""

THIN_TOML = """\
name = "oracle"
kind = "thin"
description = "Answers questions about the vault."
model.router = "fast"
model.budget_usd_per_day = 2.5
files = [{ path = "/srv/agents/vault", mode = "ro" }]
egress = []
"""

ATTENDED_YAML = """\
name: helper
kind: attended
description: General assistant.
model: { router: agent-router, budget_usd_per_day: 15 }
files:
  # A comment of the YAML text.
  - { path: /srv/agents/vault, mode: ro }
tools:
  kagi: [kagi_search_fetch]
  ha: all
verbs:
  enqueue:
    targets: [lead]
delegates: [oracle]
shell: false
sandbox_tools: [read, grep]
sandbox: { cpus: 4, memory: 8g }
skills: [handoff]
approval: []
"""

ATTENDED_TOML = """\
name = "helper"
kind = "attended"
description = "General assistant."
model.router = "agent-router"
model.budget_usd_per_day = 15
files = [
  # A comment of the TOML text.
  { path = "/srv/agents/vault", mode = "ro" },
]
tools.kagi = ["kagi_search_fetch"]
tools.ha = "all"
verbs.enqueue.targets = ["lead"]
delegates = ["oracle"]
shell = false
sandbox_tools = ["read", "grep"]
sandbox.cpus = 4
sandbox.memory = "8g"
skills = ["handoff"]
approval = []
"""

AUTONOMOUS_YAML = """\
name: lead
kind: autonomous
description: Dispatches workers.
model: { router: agent-router, budget_usd_per_day: 10 }
tools:
  board: [survey_board, get_ticket]
verbs:
  embed: {}
  ha_call:
    allow:
      - { domain: notify, service: phone }
  job_status: {}
triggers:
  - cron: "@hourly"
  - webhook: lead-hook
max_running_turns: 1
job: { timeout: 30m }
quiet:
  board: board
  daily: { call: mail__send, hour: 16, zone: UTC, weekdays_only: true }
"""

AUTONOMOUS_TOML = """\
name = "lead"
kind = "autonomous"
description = "Dispatches workers."
model.router = "agent-router"
model.budget_usd_per_day = 10
tools.board = ["survey_board", "get_ticket"]
verbs.embed = {}
verbs.ha_call.allow = [{ domain = "notify", service = "phone" }]
verbs.job_status = {}
triggers = [{ cron = "@hourly" }, { webhook = "lead-hook" }]
max_running_turns = 1
job.timeout = "30m"

[quiet]
board = "board"
daily = { call = "mail__send", hour = 16, zone = "UTC", weekdays_only = true }
"""

#: Each kind of family, as a YAML text and as a TOML text with equal values.
PAIRS = {
    "thin": (THIN_YAML, THIN_TOML),
    "attended": (ATTENDED_YAML, ATTENDED_TOML),
    "autonomous": (AUTONOMOUS_YAML, AUTONOMOUS_TOML),
}

NOT_TOML = "the text is not TOML 1.0, or it nests deeper than 8 levels"
NOT_UTF8 = "the file is not UTF-8 text"
TOO_LARGE = "the file has more than 65536 bytes"
TWO_NAMES = "two family files: family.toml and family.yaml; keep one"


#: The lines of a small TOML family. `_with` replaces one line or adds one.
BASE_LINES = (
    'name = "oracle"',
    'kind = "thin"',
    'description = "A family."',
    'model.router = "fast"',
    "model.budget_usd_per_day = 2.5",
)


def _with(line: str) -> str:
    """The base family, with `line` in the place of the line of its key."""
    key = line.split(" = ", 1)[0]
    kept = [one for one in BASE_LINES if one.split(" = ", 1)[0] != key]

    return "\n".join([*kept, line]) + "\n"


def _issues(text: str) -> list[Issue]:
    family, issues = parse_family(text, Markup.TOML)

    assert family is None
    return issues


def _locs_and_messages(text: str) -> list[tuple[str, str]]:
    return [(issue.loc, issue.msg) for issue in _issues(text)]


# --- the two paths of parse_family ----------------------------------------------


@pytest.mark.parametrize("kind", list(PAIRS))
def test_a_toml_family_of_each_kind_reads_to_the_model_of_its_yaml_form(kind: str) -> None:
    yaml_text, toml_text = PAIRS[kind]

    from_yaml, yaml_issues = parse_family(yaml_text)
    from_toml, toml_issues = parse_family(toml_text, Markup.TOML)

    assert yaml_issues == toml_issues == []
    assert from_yaml is not None
    assert from_toml == from_yaml
    assert from_toml.kind == kind


def test_the_default_markup_is_yaml() -> None:
    family, issues = parse_family(THIN_YAML, Markup.YAML)

    assert issues == []
    assert parse_family(THIN_YAML) == (family, issues)


def test_the_markup_is_a_text_enum_of_the_package() -> None:
    assert agent_family.Markup is Markup
    assert [str(markup) for markup in Markup] == ["toml", "yaml"]


def test_an_integer_for_a_float_field_is_accepted() -> None:
    family, issues = parse_family(ATTENDED_TOML, Markup.TOML)

    assert issues == []
    assert family is not None
    assert family.model.budget_usd_per_day == 15.0
    assert isinstance(family.model.budget_usd_per_day, float)


@pytest.mark.parametrize(
    ("line", "loc", "message"),
    [
        ('max_inflight_delegations = "2"', "max_inflight_delegations", "valid integer"),
        ("max_inflight_delegations = 2.0", "max_inflight_delegations", "valid integer"),
        ("max_inflight_delegations = true", "max_inflight_delegations", "valid integer"),
        ('shell = "yes"', "shell", "valid boolean"),
        ("shell = 1", "shell", "valid boolean"),
        ("description = 2024-01-01", "description", "valid string"),
        ("description = 07:32:00", "description", "valid string"),
        ("description = 7", "description", "valid string"),
        ('delegates = "oracle"', "delegates", "valid list"),
        ('model.budget_usd_per_day = "2.5"', "model.budget_usd_per_day", "valid number"),
        ("model.budget_usd_per_day = true", "model.budget_usd_per_day", "valid number"),
    ],
    ids=[
        "text-for-integer",
        "float-for-integer",
        "boolean-for-integer",
        "text-for-boolean",
        "integer-for-boolean",
        "date-for-text",
        "time-for-text",
        "integer-for-text",
        "text-for-list",
        "text-for-float",
        "boolean-for-float",
    ],
)
def test_a_value_of_another_type_is_refused(line: str, loc: str, message: str) -> None:
    issues = _issues(_with(line))

    assert [issue.loc for issue in issues] == [loc]
    assert message in issues[0].msg
    assert issues[0].severity is Severity.ERROR


def test_the_yaml_path_keeps_its_lax_read() -> None:
    text = THIN_YAML + 'max_inflight_delegations: "2"\nshell: "yes"\n'

    family, issues = parse_family(text)

    assert issues == []
    assert family is not None
    assert family.max_inflight_delegations == 2
    assert family.shell is True


def test_two_unknown_keys_give_two_issues_in_sorted_order() -> None:
    issues = _issues(THIN_TOML + "zeta = 1\nalpha = 2\n")

    assert [issue.loc for issue in issues] == ["alpha", "zeta"]
    assert all(issue.msg.startswith("unknown field") for issue in issues)


def test_two_wrong_tool_grants_give_their_issues_in_sorted_order() -> None:
    locs = [issue.loc for issue in _issues(THIN_TOML + "tools.yy = 1\ntools.bb = 2\n")]

    assert len(locs) == 4
    assert all(loc.startswith("tools.bb") for loc in locs[:2])
    assert all(loc.startswith("tools.yy") for loc in locs[2:])


def test_an_unknown_nested_key_names_the_closest_field() -> None:
    assert _locs_and_messages(THIN_TOML + "model.routr = 'fast'\n") == [
        ("model.routr", "unknown field 'routr'; did you mean 'router'?")
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (THIN_TOML + "name = 'twice'\n", NOT_TOML),
        (THIN_TOML + "a = {b = 1,}\n", NOT_TOML),
        (THIN_TOML + "max_running_turns = 9223372036854775808\n", NOT_TOML),
        (THIN_TOML + 'surrogate = "\\ud800"\n', NOT_TOML),
        ("\ufeff" + THIN_TOML, "the file starts with a byte order mark"),
        (THIN_TOML + "padding = '" + "x" * 65536 + "'\n", TOO_LARGE),
    ],
    ids=["duplicate-key", "toml11-form", "integer-past-64-bits", "lone-surrogate", "bom", "size"],
)
def test_a_refusal_of_the_reader_is_one_issue_at_the_document(text: str, message: str) -> None:
    assert _locs_and_messages(text) == [("<document>", message)]


@pytest.mark.parametrize(
    "text",
    ['name = "a\ud800"\n', 'name = "\udcc3\udca9"\n'],
    ids=["high", "two-low-that-escape-utf8-bytes"],
)
def test_a_text_with_a_lone_surrogate_is_no_utf8_text(text: str) -> None:
    assert _locs_and_messages(text) == [("<document>", NOT_UTF8)]


def test_an_empty_text_names_each_required_field() -> None:
    assert _locs_and_messages("") == [
        ("name", "Field required"),
        ("kind", "Field required"),
        ("description", "Field required"),
        ("model", "Field required"),
    ]


# --- the two file names in a registry ---------------------------------------------


def _registry(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """A registry with one family directory, `oracle`, and the named files."""
    directory = tmp_path / registry.FAMILIES_DIR / "oracle"
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)

    return tmp_path


def test_the_file_names_are_two_constants() -> None:
    assert registry.FAMILY_FILE == "family.toml"
    assert registry.FAMILY_FILE_YAML == "family.yaml"


def test_the_registry_reads_a_toml_family_file(tmp_path: Path) -> None:
    root = _registry(tmp_path, {registry.FAMILY_FILE: THIN_TOML.encode()})

    loaded = load_registry(root)

    assert loaded.families["oracle"] == parse_family(THIN_YAML)[0]
    assert loaded.reports["oracle"].file == "families/oracle/family.toml"


def test_the_registry_reads_a_yaml_family_file_as_today(tmp_path: Path) -> None:
    root = _registry(tmp_path, {registry.FAMILY_FILE_YAML: THIN_YAML.encode()})

    loaded = load_registry(root)

    assert loaded.families["oracle"] == parse_family(THIN_YAML)[0]
    assert loaded.reports["oracle"].file == "families/oracle/family.yaml"


def test_both_names_in_one_directory_are_one_error_and_no_parse(tmp_path: Path) -> None:
    root = _registry(
        tmp_path,
        {registry.FAMILY_FILE: THIN_TOML.encode(), registry.FAMILY_FILE_YAML: THIN_YAML.encode()},
    )

    loaded = load_registry(root)

    assert "oracle" not in loaded.families
    assert loaded.reports["oracle"].issues == (Issue(Severity.ERROR, "<document>", TWO_NAMES),)
    assert loaded.reports["oracle"].file == "families/oracle/family.toml"


def test_a_directory_with_no_file_keeps_the_warning_of_today(tmp_path: Path) -> None:
    (tmp_path / registry.FAMILIES_DIR / "oracle").mkdir(parents=True)

    loaded = load_registry(tmp_path)

    assert loaded.reports["oracle"].issues == (
        Issue(Severity.WARNING, "<document>", "no family.yaml; this directory is ignored"),
    )
    assert loaded.reports["oracle"].file == "families/oracle/family.yaml"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (THIN_TOML.encode() + b"# \xff\n", NOT_UTF8),
        (b"\xef\xbb\xbf" + THIN_TOML.encode(), "the file starts with a byte order mark"),
        (THIN_TOML.encode() + b"# " + b"x" * 1_000_000 + b"\n", TOO_LARGE),
        (THIN_TOML.encode() + b"name = 'twice'\n", NOT_TOML),
    ],
    ids=["no-utf8", "bom", "size", "duplicate-key"],
)
def test_the_registry_gives_each_refusal_of_the_reader(
    tmp_path: Path, data: bytes, message: str
) -> None:
    root = _registry(tmp_path, {registry.FAMILY_FILE: data})

    loaded = load_registry(root)

    assert "oracle" not in loaded.families
    assert loaded.reports["oracle"].issues == (Issue(Severity.ERROR, "<document>", message),)


def test_a_file_at_the_byte_limit_reaches_the_reader(tmp_path: Path) -> None:
    padding = 65536 - len(THIN_TOML.encode()) - len(b"# \n")
    data = THIN_TOML.encode() + b"# " + b"x" * padding + b"\n"
    root = _registry(tmp_path, {registry.FAMILY_FILE: data})

    loaded = load_registry(root)

    assert len(data) == 65536
    assert "oracle" in loaded.families


def test_a_file_at_the_byte_limit_with_bytes_that_are_no_utf8_is_no_utf8_text(
    tmp_path: Path,
) -> None:
    padding = 65536 - len(THIN_TOML.encode()) - len(b"# \n")
    data = THIN_TOML.encode() + b"# " + b"\xff" * padding + b"\n"
    root = _registry(tmp_path, {registry.FAMILY_FILE: data})

    loaded = load_registry(root)

    assert len(data) == 65536
    assert loaded.reports["oracle"].issues == (Issue(Severity.ERROR, "<document>", NOT_UTF8),)


def test_the_registry_reads_at_most_one_byte_past_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _registry(tmp_path, {registry.FAMILY_FILE: b"x" * 200_000})
    sizes: list[int] = []
    real = tomlfile.loads

    def loads(data: bytes, kind: tomlfile.FileKind) -> object:
        sizes.append(len(data))
        return real(data, kind)

    monkeypatch.setattr(tomlfile, "loads", loads)

    load_registry(root)

    assert sizes == [65537]
