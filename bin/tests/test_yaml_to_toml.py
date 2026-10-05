"""`bin/yaml-to-toml.py` writes one layout and proves each pair.

The script converts a file of the four kinds: a family file, a server file,
a component manifest and a roster. Each test here holds its own YAML text.
No test reads a YAML fixture of the repository by name, because the
migration removes those files.

Four things are held:

1. **The layout.** The header of the script holds its seven rules. Each rule
   has a written YAML text here and the exact TOML text that the script
   prints for it.
2. **The refusals.** Each of the eight YAML features stops the script, and
   so does a value that TOML cannot hold.
3. **The proof of a pair.** `--check` passes only for equal values and an
   equal count of comments.
4. **The walk.** Each tracked YAML file of the four kinds converts, and the
   pair passes the check. A tree with no such file passes.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "yaml-to-toml.py"

#: The name under which this file imports the script. The file name of the
#: script holds a `-`, so no `import` statement can name it.
MODULE_NAME = "yaml_to_toml"

#: The exit status of the script: a good run, a pair that differs, and an
#: input that the script refuses.
EXIT_OK = 0
EXIT_DIFFERS = 1
EXIT_REFUSED = 2


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its module up by name while the module still loads.
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)

    return module


y2t = _load()

FAMILY = y2t.FileKind.FAMILY
SERVER = y2t.FileKind.SERVER
MANIFEST = y2t.FileKind.MANIFEST
ROSTER = y2t.FileKind.ROSTER


def _toml(text: str, kind: Any = FAMILY) -> str:
    return y2t.convert(text, kind).toml


def _run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(one) for one in args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


# --- the kind of a file ------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("family.yaml", FAMILY),
        ("styled-family.yaml", FAMILY),
        ("server.yaml", SERVER),
        ("component.yaml", MANIFEST),
        ("some_bin_component.yaml", MANIFEST),
        ("upstreams.yaml", ROSTER),
    ],
)
def test_the_name_of_a_file_gives_its_kind(name: str, kind: Any) -> None:
    assert y2t.kind_of(name) is kind


@pytest.mark.parametrize(
    "name",
    [
        "family.yml",
        "family.toml",
        "myfamily.yaml",
        "mycomponent.yaml",
        "components.yaml",
        "secrets.enc.yaml",
        "x.yaml",
    ],
)
def test_a_name_of_no_kind_is_refused(name: str) -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.kind_of(name)

    assert raised.value.reason is y2t.Reason.FILE_NAME


# --- the layout --------------------------------------------------------------

FAMILY_YAML = """\
# The head comment.
name: chat
kind: attended

# The model.
model: { router: agent-router, budget_usd_per_day: 15 } # the chat router

files:
  # The vault.
  - { path: /srv/agents/vault, mode: ro } # to read
  - path: /srv/agents/work
    # The mode of the work root.
    mode: rw

tools:
  kagi: [kagi_search_fetch, kagi_extract]
  # Each tool of the server.
  ha: all

verbs:
  enqueue:
    targets: [scrum-lead]
  embed: {}

egress: [] # no reach

shell: false
"""

FAMILY_TOML = """\
# The head comment.
name = "chat"
kind = "attended"

# The model.
model.router = "agent-router"
model.budget_usd_per_day = 15 # the chat router

files = [
    # The vault.
    { path = "/srv/agents/vault", mode = "ro" }, # to read
    # The mode of the work root.
    { path = "/srv/agents/work", mode = "rw" },
]

tools.kagi = ["kagi_search_fetch", "kagi_extract"]
# Each tool of the server.
tools.ha = "all"

verbs.enqueue.targets = ["scrum-lead"]
verbs.embed = {}

egress = [] # no reach

shell = false
"""


def test_a_family_file_gets_the_one_layout() -> None:
    converted = y2t.convert(FAMILY_YAML, FAMILY)

    assert converted.toml == FAMILY_TOML
    assert converted.comments == 8
    assert converted.nulls == ()


def test_the_layout_holds_the_values_in_the_order_of_the_yaml_file() -> None:
    value = tomllib.loads(_toml(FAMILY_YAML))

    assert list(value) == ["name", "kind", "model", "files", "tools", "verbs", "egress", "shell"]
    assert value["model"] == {"router": "agent-router", "budget_usd_per_day": 15}
    assert value["files"][1] == {"path": "/srv/agents/work", "mode": "rw"}
    assert value["verbs"] == {"enqueue": {"targets": ["scrum-lead"]}, "embed": {}}


def test_no_line_is_a_table_header() -> None:
    """A key after a header belongs to that table, so a header moves keys."""
    for line in _toml(FAMILY_YAML).splitlines():
        assert not line.startswith("[")


SERVER_YAML = """\
name: github-code
install:
  source: github-release
  version: 1.12.0
  sha256: "1234567890123456789012345678901234567890123456789012345678901234"

run:
  entrypoint: github-mcp-server
  args:
    # The first word.
    - stdio
    - --lockdown-mode   # the last word

  env:
    GITHUB_TOKEN: secret:github_token_code

arg_denies:
  # The first fence.
  - tools: all
    arg: owner/repo   # the argument
    values:
      - example-owner/one
      - example-owner/two
  - tools: [push_files, delete_file]
    arg: branch
    values: [main, master]
  # After the last fence.

tools:
  - { name: get_commit, description: Read one commit., write: false }
"""

SERVER_TOML = """\
name = "github-code"
install.source = "github-release"
install.version = "1.12.0"
install.sha256 = "1234567890123456789012345678901234567890123456789012345678901234"

run.entrypoint = "github-mcp-server"
run.args = [
    # The first word.
    "stdio",
    "--lockdown-mode", # the last word
]

run.env.GITHUB_TOKEN = "secret:github_token_code"

arg_denies = [
    # The first fence.
    # the argument
    { tools = "all", arg = "owner/repo", values = ["example-owner/one", "example-owner/two"] },
    { tools = ["push_files", "delete_file"], arg = "branch", values = ["main", "master"] },
    # After the last fence.
]

tools = [
    { name = "get_commit", description = "Read one commit.", write = false },
]
"""


def test_a_server_file_gets_the_one_layout() -> None:
    converted = y2t.convert(SERVER_YAML, SERVER)

    assert converted.toml == SERVER_TOML
    assert converted.comments == 5


def test_a_quoted_run_of_digits_stays_a_text() -> None:
    value = tomllib.loads(_toml(SERVER_YAML, SERVER))

    assert value["install"]["sha256"] == "1234567890" * 6 + "1234"
    assert value["install"]["version"] == "1.12.0"


MANIFEST_YAML = """\
# One component.
manifest_version: "0.4"
name: handover
unit: null
runs_as: root
# The build.
build:
  - ["/usr/local/bin/uv", "sync", "--frozen",
     "--package", "handover"]
install:
  to: /opt/components/handover
provides:
  # The contract of the manifest.
  - { contract: component-manifest, major: 0, minor: 18 }
requires: []
verify:
  # What the hook proves.
  command: ["/opt/components/handover/bin/handover", "check",
            "--json"]
  user: root
  timeout_s: 60
release: yes
"""

MANIFEST_TOML = """\
# One component.
manifest_version = "0.4"
name = "handover"
runs_as = "root"
# The build.
build = [
    ["/usr/local/bin/uv", "sync", "--frozen", "--package", "handover"],
]
install.to = "/opt/components/handover"
provides = [
    # The contract of the manifest.
    { contract = "component-manifest", major = 0, minor = 18 },
]
requires = []
# What the hook proves.
verify.command = [
    "/opt/components/handover/bin/handover",
    "check",
    "--json",
]
verify.user = "root"
verify.timeout_s = 60
release = true
"""


def test_a_manifest_gets_the_one_layout() -> None:
    converted = y2t.convert(MANIFEST_YAML, MANIFEST)

    assert converted.toml == MANIFEST_TOML
    assert converted.comments == 4


def test_a_null_value_is_dropped_and_named() -> None:
    converted = y2t.convert(MANIFEST_YAML, MANIFEST)

    assert "unit" not in tomllib.loads(converted.toml)
    assert [(null.line, null.path) for null in converted.nulls] == [(4, "unit")]


@pytest.mark.parametrize(
    ("yaml_value", "toml_value"),
    [
        ("yes", True),
        ("no", False),
        ("Yes", True),
        ("true", True),
        ("false", False),
        ('"yes"', True),
        ('"no"', False),
        ("'yes'", True),
    ],
)
def test_the_release_of_a_manifest_is_a_boolean(yaml_value: str, toml_value: bool) -> None:
    text = _toml(f"name: handover\nrelease: {yaml_value}\n", MANIFEST)

    assert tomllib.loads(text)["release"] is toml_value


def test_another_text_in_the_release_of_a_manifest_stays_a_text() -> None:
    assert _toml('release: "maybe"\n', MANIFEST) == 'release = "maybe"\n'


def test_only_the_top_key_of_a_manifest_gets_the_release_rule() -> None:
    assert _toml('verify:\n  release: "yes"\n', MANIFEST) == 'verify.release = "yes"\n'


def test_a_quoted_word_in_another_kind_stays_a_text() -> None:
    assert _toml('release: "yes"\n', FAMILY) == 'release = "yes"\n'


@pytest.mark.parametrize(
    ("word", "boolean"),
    [("yes", "true"), ("no", "false"), ("on", "true"), ("off", "false"), ("True", "true")],
)
def test_a_yaml_boolean_word_is_a_boolean(word: str, boolean: str) -> None:
    assert _toml(f"shell: {word}\n") == f"shell = {boolean}\n"


def test_an_empty_roster_keeps_each_comment() -> None:
    converted = y2t.convert("# The base roster.\n# It holds no row.\n{}\n", ROSTER)

    assert converted.toml == "# The base roster.\n# It holds no row.\n"
    assert tomllib.loads(converted.toml) == {}
    assert converted.comments == 2


def test_a_comment_on_the_line_of_an_empty_top_mapping_stays() -> None:
    assert _toml("{} # no row\n", ROSTER) == "# no row\n"


def test_a_roster_row_gets_the_one_layout() -> None:
    text = """\
ha:
  command: /opt/mcp/ha/bin/ha-mcp
  args: []
  env: { HA_URL: "http://192.0.2.10:8123" }
  arg_denies:
    - { tools: all, arg: entity_id, values: [lock.front] }
"""

    assert _toml(text, ROSTER) == (
        'ha.command = "/opt/mcp/ha/bin/ha-mcp"\n'
        "ha.args = []\n"
        'ha.env.HA_URL = "http://192.0.2.10:8123"\n'
        "ha.arg_denies = [\n"
        '    { tools = "all", arg = "entity_id", values = ["lock.front"] },\n'
        "]\n"
    )


# --- single values -----------------------------------------------------------


@pytest.mark.parametrize(
    ("yaml_text", "toml_text"),
    [
        ("a: text\n", 'a = "text"\n'),
        ("a: 100\n", "a = 100\n"),
        ("a: -7\n", "a = -7\n"),
        ("a: 1.5\n", "a = 1.5\n"),
        ("a: 1.0\n", "a = 1.0\n"),
        ("a: 1.0e+20\n", "a = 1e+20\n"),
        ("a: .inf\n", "a = inf\n"),
        ("a: -.inf\n", "a = -inf\n"),
        ("a: .nan\n", "a = nan\n"),
        ("a: true\n", "a = true\n"),
        ("a: 0x1F\n", "a = 31\n"),
        ("a: 010\n", "a = 8\n"),
        ("a: 1_000\n", "a = 1000\n"),
        ("a: '100'\n", 'a = "100"\n'),
        ('a: "true"\n', 'a = "true"\n'),
        ("a: 1.12.0\n", 'a = "1.12.0"\n'),
        ("a: 16g\n", 'a = "16g"\n'),
        ('a: ""\n', 'a = ""\n'),
        ("a: 9223372036854775807\n", "a = 9223372036854775807\n"),
        ("a: -9223372036854775808\n", "a = -9223372036854775808\n"),
    ],
)
def test_one_scalar_is_one_line(yaml_text: str, toml_text: str) -> None:
    assert _toml(yaml_text) == toml_text


@pytest.mark.parametrize(
    ("yaml_text", "toml_text"),
    [
        ('a: "say \\"x\\""\n', 'a = "say \\"x\\""\n'),
        ("a: 'back\\slash'\n", 'a = "back\\\\slash"\n'),
        ('a: "line\\nend"\n', 'a = "line\\nend"\n'),
        ('a: "tab\\there"\n', 'a = "tab\\there"\n'),
        ('a: "cr\\rhere"\n', 'a = "cr\\rhere"\n'),
        ('a: "one\\x01"\n', 'a = "one\\u0001"\n'),
        ('a: "nul\\0"\n', 'a = "nul\\u0000"\n'),
        ('a: "del\\x7f"\n', 'a = "del\\u007F"\n'),
        ('a: "c1\\x85"\n', 'a = "c1\\u0085"\n'),
        ("a: §4.4 — naïve\n", 'a = "§4.4 — naïve"\n'),
        ("a: it's\n", 'a = "it\'s"\n'),
    ],
)
def test_each_text_is_a_basic_string(yaml_text: str, toml_text: str) -> None:
    text = _toml(yaml_text)

    assert text == toml_text
    assert text.split(" = ", 1)[1].startswith('"')


def test_a_text_reads_back_equal_with_each_control_character() -> None:
    escapes = "".join(f"\\x{code:02x}" for code in [*range(0x20), 0x7F, *range(0x80, 0xA0)])
    text = _toml(f'a: "{escapes}"\n')
    line = text.rstrip("\n")

    assert all(0x20 <= ord(one) < 0x7F for one in line)
    assert tomllib.loads(text)["a"] == "".join(
        chr(code) for code in [*range(0x20), 0x7F, *range(0x80, 0xA0)]
    )


@pytest.mark.parametrize(
    ("yaml_text", "toml_text"),
    [
        ("github-platform: all\n", 'github-platform = "all"\n'),
        ("under_score9: 1\n", "under_score9 = 1\n"),
        ('"with space": 1\n', '"with space" = 1\n'),
        ('"a.b": 1\n', '"a.b" = 1\n'),
        ('"": 1\n', '"" = 1\n'),
        ("é: 1\n", '"é" = 1\n'),
        ('outer:\n  "in.ner": { leaf: 1 }\n', 'outer."in.ner".leaf = 1\n'),
        ('"quoted": 1\n', "quoted = 1\n"),
    ],
)
def test_a_key_is_bare_only_when_toml_permits_it(yaml_text: str, toml_text: str) -> None:
    text = _toml(yaml_text)

    assert text == toml_text
    tomllib.loads(text)


# --- mappings and lists ------------------------------------------------------


def test_a_nested_mapping_gets_dotted_keys_in_each_style() -> None:
    block = "sandbox:\n  cpus: 8\n  memory: 16g\n"
    flow = "sandbox: { cpus: 8, memory: 16g }\n"
    expected = 'sandbox.cpus = 8\nsandbox.memory = "16g"\n'

    assert _toml(block) == expected
    assert _toml(flow) == expected


def test_an_empty_mapping_is_an_empty_inline_table() -> None:
    assert _toml("verbs: {}\ntools:\n  kagi: {}\n") == "verbs = {}\ntools.kagi = {}\n"


def test_a_mapping_that_holds_only_nulls_is_an_empty_inline_table() -> None:
    converted = y2t.convert("verbs:\n  embed: null\n  other: ~\nshell: true\n", FAMILY)

    assert converted.toml == "verbs = {}\nshell = true\n"
    assert [null.path for null in converted.nulls] == ["verbs.embed", "verbs.other"]


def test_a_list_on_one_line_stays_on_one_line() -> None:
    assert _toml("skills: [a, b, c]\n") == 'skills = ["a", "b", "c"]\n'


def test_a_list_on_more_than_one_line_gets_one_line_for_each_item() -> None:
    expected = 'egress = [\n    "one.example",\n    "two.example",\n]\n'

    assert _toml("egress:\n  - one.example\n  - two.example\n") == expected
    assert _toml("egress: [one.example,\n  two.example]\n") == expected


def test_a_list_of_lists_holds_each_inner_list_on_one_line() -> None:
    assert _toml("build:\n  - - a\n    - b\n  - [c]\n") == (
        'build = [\n    ["a", "b"],\n    ["c"],\n]\n'
    )


def test_a_mapping_in_a_list_is_one_inline_table_with_dotted_keys() -> None:
    text = _toml(
        "rows:\n  - name: one\n    limit:\n      cpus: 2\n      memory: 4g\n    tags: {}\n"
    )

    assert text == (
        'rows = [\n    { name = "one", limit.cpus = 2, limit.memory = "4g", tags = {} },\n]\n'
    )
    assert tomllib.loads(text) == {
        "rows": [{"name": "one", "limit": {"cpus": 2, "memory": "4g"}, "tags": {}}]
    }


def test_a_null_in_a_mapping_of_a_list_is_dropped_and_named() -> None:
    converted = y2t.convert("rows:\n  - name: one\n    unit: null\n", FAMILY)

    assert converted.toml == 'rows = [\n    { name = "one" },\n]\n'
    assert [(null.line, null.path) for null in converted.nulls] == [(3, "rows[0].unit")]


@pytest.mark.parametrize(
    ("yaml_text", "toml_text"),
    [
        ("---\n# first\nname: chat\n", '# first\nname = "chat"\n'),
        ("%YAML 1.1\n---\nname: chat\n", 'name = "chat"\n'),
        ("name: chat\n...\n# after\n", 'name = "chat"\n# after\n'),
        ("? name\n: chat   # one\n", 'name = "chat" # one\n'),
        ("name:   # one\n  chat\nkind: x\n", '# one\nname = "chat"\nkind = "x"\n'),
        ("text: one\n  two   # c\nkind: x\n", 'text = "one two" # c\nkind = "x"\n'),
        ('text: "one\n  two # no\n\n  three"  # c\n', 'text = "one two # no\\nthree" # c\n'),
        ("rows: [a: 1, b: 2]\n", "rows = [{ a = 1 }, { b = 2 }]\n"),
        ("list: [a, b,]\nmap: { a: 1, }\n", 'list = ["a", "b"]\nmap.a = 1\n'),
        ("a: [   # one\n]\nb: {   # two\n}\n", "# one\na = []\n# two\nb = {}\n"),
        ("list:\n  -   # one\n    k: v\n", 'list = [\n    # one\n    { k = "v" },\n]\n'),
        ("list:\n- a\n# one\nn: 1\n", 'list = [\n    "a",\n]\n# one\nn = 1\n'),
        ("name: chat # one", 'name = "chat" # one\n'),
        ("name: chat\n# end", 'name = "chat"\n# end\n'),
        ("name: chat # one\rkind: x\r", 'name = "chat" # one\nkind = "x"\n'),
        ("name: chat\x85kind: x # one\x85", 'name = "chat"\nkind = "x" # one\n'),
        ("name: chat\u2028kind: x # one\u2028", 'name = "chat"\nkind = "x" # one\n'),
        ("name: chat\u2029kind: x # one\u2029", 'name = "chat"\nkind = "x" # one\n'),
    ],
)
def test_each_form_of_a_yaml_text_converts(yaml_text: str, toml_text: str) -> None:
    converted = y2t.convert(yaml_text, FAMILY)

    assert converted.toml == toml_text
    assert converted.comments == toml_text.count("#") - toml_text.count("# no")


def test_a_key_with_no_value_in_a_flow_mapping_is_a_dropped_null() -> None:
    converted = y2t.convert("map: { a, b: 1 }\n", FAMILY)

    assert converted.toml == "map.b = 1\n"
    assert [null.path for null in converted.nulls] == ["map.a"]


# --- comments ----------------------------------------------------------------


def test_a_hash_sign_in_a_scalar_is_no_comment() -> None:
    text = """\
a: "one # two"   # the first
b: 'three # four'
c: five#six
d: [ "seven # eight" ]
"""
    converted = y2t.convert(text, FAMILY)

    assert converted.toml == (
        'a = "one # two" # the first\nb = "three # four"\nc = "five#six"\nd = ["seven # eight"]\n'
    )
    assert converted.comments == 1


def test_a_comment_on_the_line_of_a_mapping_key_goes_above_its_first_line() -> None:
    text = "verify:   # the hook\n  user: root   # the account\n  timeout_s: 60\n"

    assert _toml(text) == (
        '# the hook\nverify.user = "root" # the account\nverify.timeout_s = 60\n'
    )


def test_a_comment_on_the_line_of_a_list_key_stays_on_that_line() -> None:
    assert _toml("egress:   # each host\n  - one.example\n") == (
        'egress = [ # each host\n    "one.example",\n]\n'
    )


def test_a_comment_after_a_dropped_null_goes_above_the_next_line() -> None:
    converted = y2t.convert("kind: venv\nunit: null   # no unit\nruns_as: root\n", MANIFEST)

    assert converted.toml == 'kind = "venv"\n# no unit\nruns_as = "root"\n'
    assert converted.comments == 1


def test_a_comment_after_the_last_value_stays_at_the_end() -> None:
    assert _toml("name: chat\n\n# The end.\n# Two lines.\n") == (
        'name = "chat"\n\n# The end.\n# Two lines.\n'
    )


def test_a_comment_in_a_list_keeps_its_place_by_its_indent() -> None:
    text = """\
run:
  args:
    - stdio
    # still about args
  # about env
  env: {}
"""

    assert _toml(text) == (
        'run.args = [\n    "stdio",\n    # still about args\n]\n# about env\nrun.env = {}\n'
    )


def test_a_comment_after_a_value_that_follows_a_list_is_not_in_the_list() -> None:
    text = """\
run:
  args:
    - stdio
  unit: null
      # after the unit
  user: root
"""

    assert _toml(text, MANIFEST) == (
        'run.args = [\n    "stdio",\n]\n# after the unit\nrun.user = "root"\n'
    )


def test_an_empty_line_in_a_list_stays_and_one_in_an_inline_table_goes() -> None:
    text = """\
rows:
  - one

  - two
  - name: three

    mode: rw

  # the end of the rows
next: 1
"""

    assert _toml(text) == (
        "rows = [\n"
        '    "one",\n'
        "\n"
        '    "two",\n'
        '    { name = "three", mode = "rw" },\n'
        "\n"
        "    # the end of the rows\n"
        "]\n"
        "next = 1\n"
    )


def test_an_empty_line_below_the_key_of_a_mapping_goes() -> None:
    assert _toml("a: 1\nverify:\n\n  user: root\n") == 'a = 1\nverify.user = "root"\n'


def test_a_comment_after_a_key_in_a_flow_mapping_goes_above_its_first_line() -> None:
    text = "outer: { a: 1, verify:   # the hook\n  { user: root } }\n"

    assert _toml(text) == 'outer.a = 1\n# the hook\nouter.verify.user = "root"\n'


def test_a_comment_in_a_list_on_more_than_one_line_keeps_its_place() -> None:
    text = """\
command: [ # the words
  "one",   # first
  # between
  "two"
]   # after
"""

    assert _toml(text) == (
        'command = [ # the words\n    "one", # first\n    # between\n    "two",\n] # after\n'
    )


def test_each_comment_in_a_flow_mapping_keeps_its_place() -> None:
    text = """\
model: {
  router: a,   # first
  # between
  budget: 15   # second
}   # after
shell: true
"""
    converted = y2t.convert(text, FAMILY)

    assert converted.toml == (
        'model.router = "a" # first\n# between\nmodel.budget = 15 # second\n# after\nshell = true\n'
    )
    assert converted.comments == 4


def test_a_run_of_empty_lines_is_one_empty_line() -> None:
    assert _toml("\n\n# head\n\n\n\nname: chat\n\n\nkind: attended\n\n\n") == (
        '# head\n\nname = "chat"\n\nkind = "attended"\n'
    )


def test_a_comment_keeps_its_text_and_loses_the_space_at_its_end() -> None:
    assert _toml("#no space   \n#\n   # indented\nname: chat\n") == (
        '#no space\n#\n# indented\nname = "chat"\n'
    )


def test_a_file_with_windows_line_ends_converts() -> None:
    converted = y2t.convert("# head\r\nname: chat # eol\r\nlist:\r\n  - a\r\n", FAMILY)

    assert converted.toml == '# head\nname = "chat" # eol\nlist = [\n    "a",\n]\n'
    assert converted.comments == 2


# --- the refusals ------------------------------------------------------------

#: One text for each of the eight YAML features that the script refuses, with
#: the line that the refusal names.
REFUSED_FEATURES = [
    ("ANCHOR", "base: &base\n  a: 1\n", 1),
    ("ALIAS", "a: 1\nb: *base\n", 2),
    ("MERGE_KEY", "a: 1\nchild:\n  <<: { a: 2 }\n", 3),
    ("TAG", "a: !!str 1\n", 1),
    ("SECOND_DOCUMENT", "a: 1\n---\nb: 2\n", 2),
    ("KEY_NOT_TEXT", "a: 1\n2: b\n", 2),
    ("DATE", "a: 1\nday: 2026-10-05\n", 2),
    ("BLOCK_TEXT", "a: 1\ntext: |\n  one\n  two\n", 2),
]


@pytest.mark.parametrize(
    ("reason", "text", "line"), REFUSED_FEATURES, ids=[one[0] for one in REFUSED_FEATURES]
)
def test_each_of_the_eight_yaml_features_is_refused(reason: str, text: str, line: int) -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.convert(text, FAMILY)

    assert raised.value.reason is y2t.Reason[reason]
    assert raised.value.line == line


def test_the_table_of_refused_features_holds_eight() -> None:
    assert len({reason for reason, _text, _line in REFUSED_FEATURES}) == 8


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("TAG", "a: !local 1\n"),
        ("TAG", "a: !!binary aGk=\n"),
        ("ALIAS", "a: 1\nb: [*x]\n"),
        ("ANCHOR", "a: [&x 1]\n"),
        ("MERGE_KEY", "<<: { a: 2 }\n"),
        ("SECOND_DOCUMENT", "a: 1\n...\nb: 2\n"),
        ("SECOND_DOCUMENT", "---\na: 1\n---\n"),
        ("KEY_NOT_TEXT", "yes: 1\n"),
        ("KEY_NOT_TEXT", "on: 1\n"),
        ("KEY_NOT_TEXT", "~: 1\n"),
        ("KEY_NOT_TEXT", "1.5: 1\n"),
        ("KEY_NOT_TEXT", "2026-10-05: 1\n"),
        ("KEY_NOT_TEXT", "? [a, b]\n: 1\n"),
        ("KEY_NOT_TEXT", "rows:\n  - { 1: a }\n"),
        ("DATE", "at: 2026-10-05T07:32:00Z\n"),
        ("DATE", "days: [2026-10-05]\n"),
        ("BLOCK_TEXT", "text: >\n  one\n  two\n"),
        ("BLOCK_TEXT", "rows:\n  - |\n    one\n"),
    ],
)
def test_each_form_of_a_refused_feature_is_refused(reason: str, text: str) -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.convert(text, FAMILY)

    assert raised.value.reason is y2t.Reason[reason]


#: More levels than Python reads with its default limit for a recursion.
DEEP = 1000


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("NOT_A_MAPPING", ""),
        ("NOT_A_MAPPING", "# only a comment\n"),
        ("NOT_A_MAPPING", "- a\n- b\n"),
        ("NOT_A_MAPPING", "text\n"),
        ("NOT_A_MAPPING", "---\n"),
        ("NULL_IN_LIST", "args: [a, null, b]\n"),
        ("NULL_IN_LIST", "args:\n  - a\n  -\n"),
        ("KEY_TWICE", "a: 1\na: 2\n"),
        ("KEY_TWICE", "m: { a: 1, a: 2 }\n"),
        ("INTEGER", "a: 9223372036854775808\n"),
        ("INTEGER", "a: -9223372036854775809\n"),
        ("INTEGER", "sha256: 1234567890123456789012345678901234567890123456789012345678901234\n"),
        ("TEXT", 'a: "\\ud800"\n'),
        ("TEXT", '"\\udfff": 1\n'),
        ("NOT_YAML", "a: [1, 2\n"),
        ("NOT_YAML", "a: 1\n b: 2\n"),
        ("NOT_YAML", "a: =\n"),
        ("NOT_YAML", "a: \x01\n"),
        ("NOT_YAML", "a:\tb\n"),
        ("NOT_YAML", "a: 0x_\n"),
        pytest.param("NOT_YAML", "a: " + "9" * 5000 + "\n", id="NOT_YAML-5000-digits"),
        pytest.param("DEPTH", "a: " + "[" * DEEP + "1" + "]" * DEEP + "\n", id="DEPTH-lists"),
        pytest.param("DEPTH", "a: " + "{ k: " * DEEP + "1" + " }" * DEEP + "\n", id="DEPTH-maps"),
    ],
)
def test_a_text_that_toml_cannot_hold_is_refused(reason: str, text: str) -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.convert(text, FAMILY)

    assert raised.value.reason is y2t.Reason[reason]


def test_bytes_that_are_no_utf8_are_refused() -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.decode(b"name: caf\xe9\n")

    assert raised.value.reason is y2t.Reason.NOT_UTF8


def test_a_byte_order_mark_is_refused() -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.decode(b"\xef\xbb\xbfname: chat\n")

    assert raised.value.reason is y2t.Reason.BYTE_ORDER_MARK


@pytest.mark.parametrize("text", ['a: 1\nb: "\\ud800"\n', 'a: 1\n"\\udfff": 1\n'])
def test_half_of_a_surrogate_pair_is_refused_with_its_line(text: str) -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.convert(text, FAMILY)

    assert raised.value.reason is y2t.Reason.TEXT
    assert raised.value.line == 2


@pytest.mark.parametrize(
    ("text", "mask"),
    [
        ("name: chat\n", bytes(11)),
        ("a # b\n", bytes([1, 0, 0, 0, 1, 0])),
    ],
)
def test_a_line_that_the_tokens_do_not_explain_is_refused(text: str, mask: bytes) -> None:
    """The mask of the tokens is the proof that a `#` starts a comment.

    The first text has a sign that is in no token and in no comment. The
    second text has a token after the `#`.
    """
    with pytest.raises(y2t.Refusal) as raised:
        y2t._trivia_of(text, mask, y2t.Rows(text))

    assert raised.value.reason is y2t.Reason.COMMENT
    assert raised.value.line == 1


def test_a_refusal_names_its_line_and_no_value() -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.convert("a: 1\nsecret_word: &anchor hunter2\n", FAMILY)

    assert "line 2" in str(raised.value)
    assert "hunter2" not in str(raised.value)


def test_the_script_does_not_print_a_text_that_fails_its_own_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(y2t, "_basic", lambda text: '"changed"')

    with pytest.raises(y2t.Refusal) as raised:
        y2t.convert("name: chat\n", FAMILY)

    assert raised.value.reason is y2t.Reason.OWN_CHECK


# --- the count of comments in a TOML text ------------------------------------


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("", 0),
        ("a = 1\n", 0),
        ("# one\na = 1 # two\n", 2),
        ("# one", 1),
        ('a = "# no" # yes\n', 1),
        ("a = '# no' # yes\n", 1),
        ('a = "\\" # no" # yes\n', 1),
        ('a = "\\"" # yes\n', 1),
        ('a = "x\\" # no"\n', 0),
        ('a = """x\\""" # no"""\n', 0),
        ("a = 'c:\\' # yes\n", 1),
        ('a = """\n# no\n""" # yes\n', 1),
        ("a = '''\n# no\n''' # yes\n", 1),
        ('a = """two"" # no""" # yes\n', 1),
        ('a = """end"""" # yes\n', 1),
        ('a = """end""""" # yes\n', 1),
        ("a = '''end''''' # yes\n", 1),
        ('a = """\\""" # no""" # yes\n', 1),
        ('"# no" = 1 # yes\n', 1),
        ("'# no' = 1 # yes\n", 1),
        ("a = [\n  1, # one\n  # two\n]\n# three\n", 3),
        ("# one # still one\n", 1),
        ("# one\r\n# two\r\n", 2),
    ],
)
def test_the_count_of_toml_comments(text: str, count: int) -> None:
    tomllib.loads(text)

    assert y2t.toml_comments(text) == count


# --- the proof of a pair -----------------------------------------------------


def test_a_converted_pair_passes_the_check() -> None:
    checked = y2t.check(MANIFEST_YAML, MANIFEST_TOML, MANIFEST)

    assert checked.comments == 4
    assert [null.path for null in checked.nulls] == ["unit"]


def test_a_pair_with_another_layout_and_equal_values_passes() -> None:
    other = '[model]\nrouter = "a" # one\nbudget = 15\n'

    y2t.check("model: { router: a, budget: 15 } # one\n", other, FAMILY)


@pytest.mark.parametrize(
    ("yaml_text", "toml_text", "named"),
    [
        ("a: 1\n", "a = 2\n", "a"),
        ("a: 1\n", "a = 1.0\n", "a"),
        ("a: 1\n", "a = true\n", "a"),
        ("a: true\n", "a = 1\n", "a"),
        ("a: '1'\n", "a = 1\n", "a"),
        ("a: 1.5\n", "a = 1.25\n", "a"),
        ("a: 0.0\n", "a = -0.0\n", "a"),
        ("a: text\n", 'a = "Text"\n', "a"),
        ("a: 1\n", "a = 1\nb = 2\n", "b"),
        ("a: 1\nb: 2\n", "a = 1\n", "b"),
        ("m: { x: 1 }\n", "m.x = 2\n", "m.x"),
        ("m: { x: 1 }\n", "m = 1\n", "m"),
        ("l: [1, 2]\n", "l = [1, 3]\n", "l[1]"),
        ("l: [1, 2]\n", "l = [1]\n", "l"),
        ("l: [1, 2]\n", "l = [2, 1]\n", "l[0]"),
        ("l: [{ x: 1 }]\n", "l = [{ x = 1, y = 2 }]\n", "l[0].y"),
        ("a: 1\n", "a = 1979-05-27\n", "a"),
        ("unit: null\n", 'unit = ""\n', "unit"),
    ],
)
def test_a_pair_with_another_value_fails_the_check(
    yaml_text: str, toml_text: str, named: str
) -> None:
    with pytest.raises(y2t.Differs) as raised:
        y2t.check(yaml_text, toml_text, FAMILY)

    assert f"`{named}`" in str(raised.value)


def test_the_check_names_no_value() -> None:
    with pytest.raises(y2t.Differs) as raised:
        y2t.check("word: hunter2\n", 'word = "hunter3"\n', FAMILY)

    assert "hunter" not in str(raised.value)


def test_the_check_takes_equal_values_that_are_no_number() -> None:
    y2t.check("a: .nan\nb: .inf\n", "a = nan\nb = inf\n", FAMILY)


@pytest.mark.parametrize(
    ("yaml_text", "toml_text"),
    [
        ("# one\na: 1\n", "a = 1\n"),
        ("a: 1\n", "# one\na = 1\n"),
        ("# one\na: 1 # two\n", "# one\na = 1\n"),
    ],
)
def test_a_pair_with_another_count_of_comments_fails_the_check(
    yaml_text: str, toml_text: str
) -> None:
    with pytest.raises(y2t.Differs) as raised:
        y2t.check(yaml_text, toml_text, FAMILY)

    assert "comments" in str(raised.value)


def test_the_check_applies_the_release_rule_of_a_manifest() -> None:
    y2t.check('release: "yes"\n', "release = true\n", MANIFEST)

    with pytest.raises(y2t.Differs):
        y2t.check('release: "yes"\n', 'release = "yes"\n', MANIFEST)

    with pytest.raises(y2t.Differs):
        y2t.check('release: "yes"\n', "release = true\n", FAMILY)


def test_the_check_refuses_a_text_that_is_no_toml() -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.check("a: 1\n", "a = \n", FAMILY)

    assert raised.value.reason is y2t.Reason.NOT_TOML


def test_the_check_refuses_a_yaml_feature_too() -> None:
    with pytest.raises(y2t.Refusal) as raised:
        y2t.check("a: &x 1\n", "a = 1\n", FAMILY)

    assert raised.value.reason is y2t.Reason.ANCHOR


#: A value of `DEEP` levels, in a form that YAML and TOML both read.
DEEP_VALUE = "[" * DEEP + "1" + "]" * DEEP


@pytest.mark.parametrize(
    ("yaml_text", "toml_text", "reason"),
    [
        pytest.param(f"a: {DEEP_VALUE}\n", "a = 1\n", "DEPTH", id="deep-yaml"),
        pytest.param("a: 1\n", f"a = {DEEP_VALUE}\n", "NOT_TOML", id="deep-toml"),
    ],
)
def test_the_check_refuses_a_deep_text_of_each_side(
    yaml_text: str, toml_text: str, reason: str
) -> None:
    """A pair that the script cannot read is refused. It does not read as a difference."""
    with pytest.raises(y2t.Refusal) as raised:
        y2t.check(yaml_text, toml_text, FAMILY)

    assert raised.value.reason is y2t.Reason[reason]


# --- the command line --------------------------------------------------------


def test_the_script_prints_the_toml_text_and_names_each_null(tmp_path: Path) -> None:
    source = tmp_path / "component.yaml"
    source.write_text(MANIFEST_YAML, encoding="utf-8")

    done = _run(source)

    assert done.returncode == EXIT_OK
    assert done.stdout == MANIFEST_TOML
    assert done.stderr.count("\n") == 1
    assert "line 4" in done.stderr
    assert "`unit`" in done.stderr


def test_the_script_writes_utf8_when_stdout_is_ascii(tmp_path: Path) -> None:
    source = tmp_path / "family.yaml"
    source.write_text("# §4.4 — naïve\nname: café\n", encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(source)],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
    )

    assert done.returncode == EXIT_OK
    assert done.stdout.decode("utf-8") == '# §4.4 — naïve\nname = "café"\n'


def test_the_check_mode_exits_0_for_an_equal_pair(tmp_path: Path) -> None:
    source = tmp_path / "component.yaml"
    target = tmp_path / "component.toml"
    source.write_text(MANIFEST_YAML, encoding="utf-8")
    target.write_text(_run(source).stdout, encoding="utf-8")

    done = _run("--check", source, target)

    assert done.returncode == EXIT_OK
    assert "4 comments" in done.stdout
    assert "1 null" in done.stdout
    assert done.stderr == ""


def test_the_check_mode_exits_1_for_a_pair_that_differs(tmp_path: Path) -> None:
    source = tmp_path / "family.yaml"
    target = tmp_path / "family.toml"
    source.write_text("name: chat # one\n", encoding="utf-8")
    target.write_text('name = "chat"\n', encoding="utf-8")

    done = _run("--check", source, target)

    assert done.returncode == EXIT_DIFFERS
    assert done.stdout == ""
    assert "comments" in done.stderr


@pytest.mark.parametrize("mode", [(), ("--check",)])
def test_a_refused_file_exits_2_and_prints_no_toml(tmp_path: Path, mode: tuple[str, ...]) -> None:
    source = tmp_path / "server.yaml"
    target = tmp_path / "server.toml"
    source.write_text("name: a\ntext: |\n  block\n", encoding="utf-8")
    target.write_text('name = "a"\n', encoding="utf-8")

    done = _run(*mode, source, *([target] if mode else []))

    assert done.returncode == EXIT_REFUSED
    assert done.stdout == ""
    assert "line 2" in done.stderr
    assert "block text" in done.stderr


def test_a_refusal_of_the_toml_file_names_that_file(tmp_path: Path) -> None:
    source = tmp_path / "family.yaml"
    target = tmp_path / "family.toml"
    source.write_text("name: chat\n", encoding="utf-8")
    target.write_bytes(b'name = "caf\xe9"\n')

    done = _run("--check", source, target)

    assert done.returncode == EXIT_REFUSED
    assert "UTF-8" in done.stderr
    assert str(target) in done.stderr


def test_a_file_name_of_no_kind_exits_2(tmp_path: Path) -> None:
    source = tmp_path / "notes.yaml"
    source.write_text("name: a\n", encoding="utf-8")

    done = _run(source)

    assert done.returncode == EXIT_REFUSED
    assert done.stdout == ""


def test_a_file_that_is_not_there_exits_2(tmp_path: Path) -> None:
    done = _run(tmp_path / "family.yaml")

    assert done.returncode == EXIT_REFUSED
    assert done.stdout == ""
    assert "Traceback" not in done.stderr


#: Starts a program with stdout closed: `sh` closes it, and `exec` gives the
#: program the process.
CLOSED_STDOUT = ("/bin/sh", "-c", 'exec "$0" "$@" >&-')


@pytest.mark.parametrize("mode", [(), ("--check",)])
def test_a_closed_stdout_exits_2(tmp_path: Path, mode: tuple[str, ...]) -> None:
    """Status 1 is a pair that differs. A fault of another kind never gives it."""
    source = tmp_path / "family.yaml"
    target = tmp_path / "family.toml"
    source.write_text("name: chat\n", encoding="utf-8")
    target.write_text('name = "chat"\n', encoding="utf-8")
    files = [str(source), *([str(target)] if mode else [])]

    done = subprocess.run(
        [*CLOSED_STDOUT, sys.executable, str(SCRIPT), *mode, *files],
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert done.returncode == EXIT_REFUSED
    assert done.stderr == "yaml-to-toml: stdout is closed\n"


def test_a_reader_of_stdout_that_left_exits_2(tmp_path: Path) -> None:
    source = tmp_path / "family.yaml"
    source.write_text("name: chat\n", encoding="utf-8")
    read_end, write_end = os.pipe()
    os.close(read_end)

    try:
        done = subprocess.run(
            [sys.executable, str(SCRIPT), str(source)],
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
    finally:
        os.close(write_end)

    assert done.returncode == EXIT_REFUSED
    assert done.stderr == f"yaml-to-toml: {os.strerror(errno.EPIPE)}\n"


def test_a_python_with_no_pyyaml_exits_2(tmp_path: Path) -> None:
    """`-S` starts Python with no package of the venv, as a run without `uv run` can."""
    source = tmp_path / "family.yaml"
    source.write_text("name: chat\n", encoding="utf-8")

    done = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert done.returncode == EXIT_REFUSED
    assert done.stdout == ""
    assert done.stderr == "yaml-to-toml: PyYAML is missing: start the script with `uv run`\n"


def test_a_fault_that_the_script_does_not_name_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def stop(*_args: object) -> None:
        raise RuntimeError("hunter2")

    monkeypatch.setattr(y2t, "_run", stop)

    assert y2t.main([str(tmp_path / "family.yaml")]) == EXIT_REFUSED

    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.count("\n") == 1
    assert "RuntimeError" in printed.err
    assert "hunter2" not in printed.err


@pytest.mark.parametrize(
    "args",
    [(), ("--check", "family.yaml"), ("family.yaml", "family.toml"), ("--unknown", "family.yaml")],
)
def test_a_wrong_command_line_exits_2(tmp_path: Path, args: tuple[str, ...]) -> None:
    (tmp_path / "family.yaml").write_text("name: chat\n", encoding="utf-8")
    (tmp_path / "family.toml").write_text('name = "chat"\n', encoding="utf-8")

    done = _run(*(arg if arg.startswith("--") else tmp_path / arg for arg in args))

    assert done.returncode == EXIT_REFUSED
    assert done.stdout == ""
    assert "usage:" in done.stderr


# --- the values against a second reader --------------------------------------

#: The two texts that the key `release` of a manifest holds as a boolean.
RELEASE_TEXTS = {"yes": True, "no": False}


def _without_nulls(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_nulls(one) for key, one in value.items() if one is not None}

    if isinstance(value, list):
        return [_without_nulls(one) for one in value]

    return value


def _safe_load_values(text: str, kind: Any) -> object:
    """The values that a product reader gets from a YAML text.

    Each product reader of the four kinds uses `yaml.safe_load`. The script
    builds its values with a reader of its own, so this function is the one
    anchor outside that reader. It applies the two rules of the script that
    change a value: a null of a mapping gets no key, and the key `release` of
    a manifest is a boolean.
    """
    value = _without_nulls(yaml.safe_load(text))
    if kind is MANIFEST and isinstance(value, dict):
        release = value.get("release")
        if isinstance(release, str) and release in RELEASE_TEXTS:
            value["release"] = RELEASE_TEXTS[release]

    return value


def _differs_from_safe_load(text: str, kind: Any) -> str | None:
    """Where the TOML text of the script and `yaml.safe_load` differ, or None."""
    written = tomllib.loads(y2t.convert(text, kind).toml)

    return y2t._difference(_safe_load_values(text, kind), written, "")


#: Texts that the script and `yaml.safe_load` must read to the same values.
#: YAML 1.1 has forms of a number that TOML does not have: base 60, base 2,
#: base 8 with one `0`, a sign `+` and a `_` between digits. Some texts here
#: look like a number and are a text in YAML 1.1.
SAME_VALUES = [
    pytest.param(FAMILY_YAML, FAMILY, id="family"),
    pytest.param(SERVER_YAML, SERVER, id="server"),
    pytest.param(MANIFEST_YAML, MANIFEST, id="manifest"),
    pytest.param('release: "no"\nunit: ~\n', MANIFEST, id="manifest-release-text"),
    *(
        pytest.param(text, FAMILY, id=text.strip())
        for text in (
            "a: 1:30\n",
            "a: -1:30\n",
            "a: 190:20:30\n",
            "a: 1:30.5\n",
            "a: 0b101\n",
            "a: 0b1_0\n",
            "a: 0x_1f\n",
            "a: 00\n",
            "a: 017\n",
            "a: 08\n",
            "a: 0o17\n",
            "a: +1\n",
            "a: -0\n",
            "a: .5\n",
            "a: -0.0\n",
            "a: 1_000.5\n",
            "a: 6.02e+23\n",
            "a: 1.e+3\n",
            "a: 1e3\n",
            "a: 1e+3\n",
            "a: +.INF\n",
            "a: .NaN\n",
            "a: Off\n",
            "a: TRUE\n",
            "a: y\n",
            "a: Null\nb: 1\n",
            "a:\nb: 1\n",
            'a: "1:30"\n',
            "a: [1:30, {b: 0b11}]\n",
            "a: { b: [0x10, 1:00.0], c: ~ }\n",
        )
    ),
]


@pytest.mark.parametrize(("text", "kind"), SAME_VALUES)
def test_the_script_and_safe_load_read_the_same_values(text: str, kind: Any) -> None:
    assert _differs_from_safe_load(text, kind) is None


# --- the walk over the files of the repository -------------------------------

#: How git names a file of the four kinds. `kind_of` holds the same names.
KIND_PATHSPECS = (
    ":(glob)**/family.yaml",
    ":(glob)**/*-family.yaml",
    ":(glob)**/server.yaml",
    ":(glob)**/component.yaml",
    ":(glob)**/*_component.yaml",
    ":(glob)**/upstreams.yaml",
)


def _git_files(*pathspecs: str) -> list[str]:
    """Each tracked file of the pathspecs that the work tree still holds."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    return sorted(one for one in listing.split("\0") if one and (REPO / one).is_file())


#: Each tracked YAML file of the four kinds. The migration removes them one
#: packet at a time, so the list gets shorter and then empty.
TRACKED = _git_files(*KIND_PATHSPECS)

#: What the walk gets when the list is empty. pytest reports a parametrized
#: test with no value as skipped, and an empty tree must pass.
NO_FILE = "(no YAML file of the four kinds is left)"


@pytest.mark.parametrize("name", TRACKED or [NO_FILE])
def test_each_tracked_file_of_the_four_kinds_converts(name: str) -> None:
    if name == NO_FILE:
        assert TRACKED == []
        return

    path = REPO / name
    kind = y2t.kind_of(path.name)
    yaml_text = y2t.decode(path.read_bytes())

    converted = y2t.convert(yaml_text, kind)
    checked = y2t.check(yaml_text, converted.toml, kind)

    assert _differs_from_safe_load(yaml_text, kind) is None
    assert checked.comments == converted.comments
    assert checked.nulls == converted.nulls
    assert not [line for line in converted.toml.splitlines() if line.startswith("[")]
    assert converted.toml == "" or converted.toml.endswith("\n")


def _has_a_kind(name: str) -> bool:
    try:
        y2t.kind_of(name)
    except y2t.Refusal:
        return False

    return True


def test_the_walk_and_the_script_take_the_same_names() -> None:
    """A name that only one of the two takes is a file that no test converts."""
    taken = [name for name in _git_files(":(glob)**") if _has_a_kind(Path(name).name)]

    assert taken == TRACKED
