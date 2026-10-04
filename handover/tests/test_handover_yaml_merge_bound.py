"""The YAML readers bound what the merge keys of a document copy.

PyYAML gives a merge key no limit, and no `except` clause stops a reader
that does not end. Each YAML reader of this package counts the pairs that
the merge keys copy, and the levels of a chain of merge keys. Past a limit
the document does not parse, which is the usual refusal of that reader.

The text with merge keys that double runs in a child process. The child has
a time limit and a memory limit, so a reader with no bound fails the test
and does not take the machine. Three more kinds of small text run there too:
a merge key that the text writes with its tag, a second document that does
not compose, and a mapping that merges itself.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from handover.boundedyaml import MergeLimits, MergeTooDeep, MergeTooLarge, load
from handover.errors import Refusal, RefusalCode
from handover.intake import store
from handover.intake.store import recipients_of
from handover.manifest import MAX_MANIFEST_BYTES, parse_manifest
from handover.mcpserver import MAX_SERVER_BYTES, parse_server
from handover_fixtures import manifest_text
from handover_mcp_fixtures import KAGI_LOCK_PATH, KAGI_YAML

from handover import manifest, mcpserver

SUBJECT = "chaperone/component.yaml"
SOPS_PATH = "secrets/mcp/kagi.env"
RECIPIENT = "age1" + "q" * 58

#: The limits of the child process: seconds of wall time, seconds of CPU
#: time and bytes of memory.
WALL_LIMIT_S = 20
CPU_LIMIT_S = 10
MEMORY_LIMIT_BYTES = 1 << 30

#: The levels of the small text that each reader refuses at its copy limit.
DOUBLING_LEVELS = 40

#: What the child does before it reads: it takes the two limits. One
#: system takes no limit on the address space, so a thread also watches
#: the largest size of the process and ends it past the limit.
CHILD_LIMITS = """
import os, resource, sys, threading, time

cpu, memory = int(sys.argv[1]), int(sys.argv[2])
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
try:
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
except (ValueError, OSError):
    pass

unit = 1 if sys.platform == "darwin" else 1024

def watch():
    while True:
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit > memory:
            os._exit(70)
        time.sleep(0.02)

threading.Thread(target=watch, daemon=True).start()
text = sys.stdin.read()
"""

MANIFEST_CHILD = """
from handover.errors import Refusal
from handover.manifest import parse_manifest

try:
    parse_manifest(text, "subject")
    print("accepted")
except Refusal as refusal:
    print(refusal.detail)
"""

SERVER_CHILD = """
from handover.errors import Refusal
from handover.mcpserver import parse_server

try:
    parse_server(text.encode("utf-8"), "kagi")
    print("accepted")
except Refusal as refusal:
    print(refusal.detail)
"""

SOPS_CHILD = """
import os
from pathlib import Path
from handover.intake.store import recipients_of

path = Path(sys.argv[3])
path.write_text(text, encoding="utf-8")
path.chmod(0o600)
print(recipients_of(path, "secrets/mcp/kagi.env", owner_uid=os.getuid()))
"""


def _in_child(payload: str, text: str, *args: str) -> str:
    """What the child prints. The child ends inside the limits, or the test
    fails here."""
    argv = [sys.executable, "-c", CHILD_LIMITS + payload]
    argv += [str(CPU_LIMIT_S), str(MEMORY_LIMIT_BYTES), *args]
    done = subprocess.run(
        argv, input=text, capture_output=True, text=True, timeout=WALL_LIMIT_S, check=False
    )

    assert done.returncode == 0, done.stderr[-2000:]

    return done.stdout.strip()


#: A merge key that a text writes with its tag, and with no `<<`.
TAGGED_KEY = "!!merge m"


def _doubling(levels: int, indent: str = "  ", key: str = "<<") -> str:
    """A list of mappings. Each one merges the one before two times."""
    lines = [f"{indent}- &m0 {{k: 1}}"]
    lines += [f"{indent}- &m{n} {{{key}: [*m{n - 1}, *m{n - 1}]}}" for n in range(1, levels + 1)]

    return "\n".join(lines) + "\n"


def _merges_itself(merges: int) -> str:
    """A mapping with one pair that merges itself `merges` times."""
    keys = ", ".join(["<<: *a"] * merges)

    return f"&a {{k: 1, {keys}}}"


#: A second document that does not compose: its alias has no anchor.
LATER_DOCUMENT = "---\n- *none\n"

#: Three more small texts: the lines of a list, and the text after the
#: document. Each reader refuses each one inside the limits of the child.
SMALL_TEXTS = {
    "a-merge-key-by-its-tag": (_doubling(DOUBLING_LEVELS, key=TAGGED_KEY), ""),
    "a-later-document": (_doubling(DOUBLING_LEVELS), LATER_DOCUMENT),
    "a-mapping-that-merges-itself": (f"  - {_merges_itself(DOUBLING_LEVELS)}\n", ""),
}

#: A character that PyYAML does not take. The loader refuses it before it
#: reads the first token.
NOT_A_YAML_CHARACTER = "\x07"


def _chain(levels: int) -> str:
    """A list of mappings, then a mapping that merges the last one. Each
    mapping of the list merges the one before. The reader fills `last`
    first, so the merge of `last` walks each level."""
    chain = "".join(f"  - &m{n} {{<<: *m{n - 1}}}\n" for n in range(1, levels))

    return f"chain:\n  - &m0 {{k: 1}}\n{chain}last: {{<<: *m{levels - 1}}}\n"


def _copies(keys: int, merges: int) -> str:
    """A mapping that merges one mapping of `keys` pairs `merges` times."""
    pairs = ", ".join(f"k{n}: 1" for n in range(keys))
    aliases = ", ".join(["*a"] * merges)

    return f"base: &a {{{pairs}}}\nown: {{<<: [{aliases}]}}\n"


def _manifest_with(first_unit: str) -> str:
    """A valid manifest with a first value of `unit`. The line `unit: null`
    after it replaces the value."""
    text = manifest_text("chaperone")
    assert "unit: null" in text

    return text.replace("unit: null", f"unit:\n{first_unit}unit: null")


def _manifest_refusal(text: str) -> str:
    assert len(text.encode("utf-8")) <= MAX_MANIFEST_BYTES
    with pytest.raises(Refusal) as caught:
        parse_manifest(text, SUBJECT)

    assert caught.value.code is RefusalCode.MANIFEST

    return caught.value.detail


#: The pairs of the mapping that a test of the copy limit merges.
COPIED_KEYS = 256

PAIRS_REFUSAL = f"does not parse: the merge keys copy more than {manifest.MERGE_LIMITS.pairs} pairs"

# -- the reader ----------------------------------------------------------------

LIMITS = MergeLimits(depth=4, pairs=12)


def test_a_document_with_no_merge_key_reads_as_before() -> None:
    assert load("a: [1, {b: yes}]\n", LIMITS) == {"a": [1, {"b": True}]}


def test_a_merge_key_reads_as_before() -> None:
    text = "base: &b {mode: automatic, keep: 1}\nown:\n  <<: *b\n  keep: 2\n"

    assert load(text, LIMITS)["own"] == {"mode": "automatic", "keep": 2}


def test_a_list_of_merges_gives_the_first_mapping_the_last_word() -> None:
    text = "x: &x {k: 1}\ny: &y {k: 2}\nown: {<<: [*x, *y]}\n"

    assert load(text, LIMITS)["own"] == {"k": 1}


def test_a_chain_at_the_limit_reads_and_one_level_more_does_not() -> None:
    assert load(_chain(LIMITS.depth), LIMITS)["last"] == {"k": 1}
    with pytest.raises(MergeTooDeep):
        load(_chain(LIMITS.depth + 1), LIMITS)


def test_the_copies_at_the_limit_read_and_one_pair_more_does_not() -> None:
    assert len(load(_copies(3, 4), LIMITS)["own"]) == 3
    with pytest.raises(MergeTooLarge):
        load(_copies(1, LIMITS.pairs + 1), LIMITS)


def test_the_count_is_for_the_whole_document() -> None:
    """Two mappings each copy less than the limit, and more together."""
    text = "base: &a {k0: 1, k1: 1, k2: 1}\none: {<<: [*a, *a]}\ntwo: {<<: [*a, *a, *a]}\n"

    with pytest.raises(MergeTooLarge):
        load(text, LIMITS)


def test_a_merge_key_by_its_tag_counts_as_a_merge_key() -> None:
    """The count reads the tag of a key, and not its text."""
    base = "base: &a {k0: 1, k1: 1, k2: 1}\n"

    assert len(load(f"{base}own: {{{TAGGED_KEY}: [*a, *a, *a, *a]}}\n", LIMITS)["own"]) == 3
    with pytest.raises(MergeTooLarge):
        load(f"{base}own: {{{TAGGED_KEY}: [*a, *a, *a, *a, *a]}}\n", LIMITS)


def test_a_mapping_that_merges_itself_has_the_two_limits() -> None:
    """Three merges read as PyYAML reads them. Four pass the copy limit, and
    five pass the limit of a chain."""
    assert len(load(_merges_itself(3), LIMITS)) == 1
    assert _result(lambda one: load(one, LIMITS), _merges_itself(3)) == _result(
        yaml.safe_load, _merges_itself(3)
    )
    with pytest.raises(MergeTooLarge):
        load(_merges_itself(LIMITS.depth), LIMITS)
    with pytest.raises(MergeTooDeep):
        load(_merges_itself(LIMITS.depth + 1), LIMITS)


def test_a_later_document_is_an_error_before_the_first_one_is_built() -> None:
    """The first document of this text passes the copy limit. The error is
    that of the second document, so the reader built no value of the first."""
    with pytest.raises(yaml.composer.ComposerError):
        load(_copies(1, LIMITS.pairs + 1) + LATER_DOCUMENT, LIMITS)


def test_a_character_that_pyyaml_does_not_take_is_a_yaml_error() -> None:
    """The loader raises this error when it starts. `load` makes the loader,
    so the handler of a caller for `load` takes the error too."""
    with pytest.raises(yaml.reader.ReaderError):
        load(f"a: 1 # {NOT_A_YAML_CHARACTER}\n", LIMITS)


#: Texts with a merge key that PyYAML reads at no cost. Each one is a
#: different path of the merge step.
SAME_AS_PYYAML = (
    "base: &b {x: 1, y: 2}\nown:\n  <<: *b\n  y: 3\n",
    "a: &a {k: 1}\nb: &b {j: 2}\nown: {<<: [*a, *b], k: 9}\n",
    "a: &a {k: 1}\nown: {<<: *a, <<: *a}\n",
    "- &a {k: 1}\n- {<<: *a}\n- {<<: [*a, *a]}\n",
    "own: {<<: {<<: {<<: {k: 1}}}}\n",
    "own: {<<: !!map {k: 1}}\n",
    "own: {<<: !!omap [{k: 1}]}\n",
    "own: !!set {<<: {a: null}, b: null}\n",
    "own: {=: 1, <<: {=: 2}}\n",
    "a: &a {<<: *a, k: 1}\n",
    "a: &a {<<: *a, <<: {x: 1}}\n",
    "a: &a {x: &x {<<: *a}}\n",
    "own: {<<: text}\n",
    "own: {<<: null}\n",
    "own: {<<: [[1]]}\n",
    "a: &a {k: 1}\nown: {<<: [*a, text]}\n",
    "a: &a [1, 2]\nown: {<<: *a}\n",
    "? {<<: {a: 1}}\n: v\n",
    "a: &a {k: 1}\nown: {!!merge m: *a, j: 2}\n",
    "a: &a {k: 1}\nown: {<<: *a}\n---\nb: 2\n",
)

#: Limits that no text of `SAME_AS_PYYAML` reaches.
NO_LIMIT = MergeLimits(depth=64, pairs=4096)


def _result(read: Callable[[str], object], text: str) -> str:
    """The value as text, or the type and the line of the error. A value
    can hold itself, and `repr` reads such a value."""
    try:
        return repr(read(text))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)

        return f"{type(error).__name__} at line {getattr(mark, 'line', None)}"


@pytest.mark.parametrize("text", SAME_AS_PYYAML)
def test_a_text_under_the_limits_reads_as_pyyaml_reads_it(text: str) -> None:
    assert _result(lambda one: load(one, NO_LIMIT), text) == _result(yaml.safe_load, text)


def _yaml_loads(path: Path) -> list[str]:
    """Each call of a loader of the `yaml` module in one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue

        module = node.func.value
        if isinstance(module, ast.Name) and module.id == "yaml" and "load" in node.func.attr:
            found.append(f"{path.name}:{node.lineno}")

    return found


def test_no_module_of_this_package_calls_a_loader_with_no_bound() -> None:
    source = Path(manifest.__file__).parent
    calls = [call for path in sorted(source.rglob("*.py")) for call in _yaml_loads(path)]

    assert calls == []


#: The limits of the Rust reader of a manifest and of a server file
#: (`rust/crates/creche-contracts/src/manifest/yaml.rs` and
#: `rust/crates/agent-family/src/yaml/construct.rs`).
MANIFEST_LIMITS = MergeLimits(depth=128, pairs=65_536)
SERVER_LIMITS = MergeLimits(depth=400, pairs=100_000)


def test_each_reader_has_the_limits_of_its_rust_reader() -> None:
    """The numbers themselves. Each other test takes a limit from the module
    that it tests, so a changed number there moves that test with it. The
    sops file has no Rust reader and takes the limits of a manifest."""
    assert manifest.MERGE_LIMITS == MANIFEST_LIMITS
    assert mcpserver.MERGE_LIMITS == SERVER_LIMITS
    assert store.MERGE_LIMITS == MANIFEST_LIMITS


# -- component.yaml ------------------------------------------------------------


def _manifest_chain(levels: int) -> str:
    """A manifest whose `restore` merges the last mapping of a chain. The
    mappings of the chain are in a first value of `unit`."""
    chain = "".join(f"  - &m{n} {{<<: *m{n - 1}}}\n" for n in range(1, levels))
    text = _manifest_with(f"  - &m0 {{mode: automatic}}\n{chain}")
    restore = "restore:\n  mode: automatic\n  keep: 3\n"
    assert restore in text

    return text.replace(restore, f"restore:\n  <<: *m{levels - 1}\n  keep: 1\n")


def test_a_manifest_reads_a_chain_at_its_limit() -> None:
    parsed = parse_manifest(_manifest_chain(manifest.MERGE_LIMITS.depth), SUBJECT)

    assert parsed.restore.keep == 1


def test_a_manifest_with_a_longer_chain_does_not_parse() -> None:
    detail = _manifest_refusal(_manifest_chain(manifest.MERGE_LIMITS.depth + 1))

    assert detail == f"does not parse: nests deeper than {manifest.MERGE_LIMITS.depth} levels"


def _manifest_copies(merges: int) -> str:
    """A manifest whose first value of `unit` merges one mapping of 256
    pairs `merges` times."""
    pairs = ", ".join(f"k{n}: 1" for n in range(COPIED_KEYS))
    aliases = ", ".join(["*a"] * merges)

    return _manifest_with(f"  - &a {{{pairs}}}\n  - {{<<: [{aliases}]}}\n")


def test_a_manifest_reads_the_copies_at_its_limit() -> None:
    merges = manifest.MERGE_LIMITS.pairs // COPIED_KEYS

    assert parse_manifest(_manifest_copies(merges), SUBJECT).unit is None


def test_a_manifest_whose_merge_keys_copy_more_does_not_parse() -> None:
    merges = manifest.MERGE_LIMITS.pairs // COPIED_KEYS + 1

    assert _manifest_refusal(_manifest_copies(merges)) == PAIRS_REFUSAL


def test_a_merge_of_a_value_that_is_no_mapping_still_names_its_line() -> None:
    text = manifest_text("chaperone").replace("  mode: automatic\n", "  <<: automatic\n")

    assert _manifest_refusal(text).startswith("does not parse: line ")


def test_a_small_manifest_with_merge_keys_that_double_does_not_parse() -> None:
    text = _manifest_with(_doubling(DOUBLING_LEVELS))
    assert len(text.encode("utf-8")) <= 4096

    assert _in_child(MANIFEST_CHILD, text) == PAIRS_REFUSAL


#: The start of the refusal of a manifest for each text of `SMALL_TEXTS`.
#: The second document has a line, and the two other texts have none.
SMALL_TEXT_DETAILS = {
    "a-merge-key-by-its-tag": PAIRS_REFUSAL,
    "a-later-document": "does not parse: line ",
    "a-mapping-that-merges-itself": PAIRS_REFUSAL,
}


@pytest.mark.parametrize("name", SMALL_TEXTS)
def test_a_small_manifest_of_each_other_kind_does_not_parse(name: str) -> None:
    body, tail = SMALL_TEXTS[name]
    text = _manifest_with(body) + tail
    assert len(text.encode("utf-8")) <= 4096

    assert _in_child(MANIFEST_CHILD, text).startswith(SMALL_TEXT_DETAILS[name])


def test_a_manifest_with_a_character_that_is_no_yaml_does_not_parse() -> None:
    """The usual refusal, and no error of the loader."""
    text = _manifest_with(f"  - a # {NOT_A_YAML_CHARACTER}\n")

    assert _manifest_refusal(text) == "does not parse: unreadable YAML"


# -- server.yaml ---------------------------------------------------------------


def _server_text(extra: str) -> str:
    return f"{KAGI_YAML.format(lock=KAGI_LOCK_PATH)}{extra}"


def _server_refusal(text: str) -> str:
    raw = text.encode("utf-8")
    assert len(raw) <= MAX_SERVER_BYTES
    with pytest.raises(Refusal) as caught:
        parse_server(raw, "kagi")

    assert caught.value.code is RefusalCode.SERVER

    return caught.value.detail


def test_a_server_file_reads_merge_keys_at_its_limits() -> None:
    """The reader takes the text. The refusal is then for the key `chain`,
    which no server file has."""
    limits = mcpserver.MERGE_LIMITS
    at_depth = _server_refusal(_server_text(_chain(limits.depth)))
    at_pairs = _server_refusal(_server_text(_copies(250, limits.pairs // 250)))

    assert "not readable YAML" not in at_depth
    assert "not readable YAML" not in at_pairs


def test_a_server_file_past_a_merge_limit_does_not_parse() -> None:
    limits = mcpserver.MERGE_LIMITS
    past_depth = _server_refusal(_server_text(_chain(limits.depth + 1)))
    past_pairs = _server_refusal(_server_text(_copies(250, limits.pairs // 250 + 1)))

    assert past_depth == "server.yaml is not readable YAML"
    assert past_pairs == "server.yaml is not readable YAML"


def test_a_small_server_file_with_merge_keys_that_double_does_not_parse() -> None:
    text = _server_text("extra:\n" + _doubling(DOUBLING_LEVELS))
    assert len(text.encode("utf-8")) <= 4096

    assert _in_child(SERVER_CHILD, text) == "server.yaml is not readable YAML"


@pytest.mark.parametrize("name", SMALL_TEXTS)
def test_a_small_server_file_of_each_other_kind_does_not_parse(name: str) -> None:
    body, tail = SMALL_TEXTS[name]
    text = _server_text("extra:\n" + body) + tail
    assert len(text.encode("utf-8")) <= 4096

    assert _in_child(SERVER_CHILD, text) == "server.yaml is not readable YAML"


def test_a_server_file_with_a_character_that_is_no_yaml_does_not_parse() -> None:
    text = _server_text(f"# {NOT_A_YAML_CHARACTER}\n")

    assert _server_refusal(text) == "server.yaml is not readable YAML"


# -- the sops file -------------------------------------------------------------


def _sops_text(extra: str) -> str:
    return f"creation_rules:\n  - path_regex: secrets/mcp/.*\n    age: {RECIPIENT}\n{extra}"


def _recipients(tmp_path: Path, text: str) -> tuple[str, ...]:
    path = tmp_path / ".sops.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)

    return recipients_of(path, SOPS_PATH, owner_uid=os.getuid())


def test_a_sops_file_reads_merge_keys_at_its_limits(tmp_path: Path) -> None:
    limits = store.MERGE_LIMITS

    assert _recipients(tmp_path, _sops_text(_chain(limits.depth))) == (RECIPIENT,)
    assert _recipients(tmp_path, _sops_text(_copies(256, limits.pairs // 256))) == (RECIPIENT,)


def test_a_sops_file_past_a_merge_limit_names_no_recipient(tmp_path: Path) -> None:
    limits = store.MERGE_LIMITS

    assert _recipients(tmp_path, _sops_text(_chain(limits.depth + 1))) == ()
    assert _recipients(tmp_path, _sops_text(_copies(256, limits.pairs // 256 + 1))) == ()


def test_a_small_sops_file_with_merge_keys_that_double_names_no_recipient(
    tmp_path: Path,
) -> None:
    text = _sops_text("extra:\n" + _doubling(DOUBLING_LEVELS))
    assert len(text.encode("utf-8")) <= 4096

    assert _in_child(SOPS_CHILD, text, str(tmp_path / ".sops.yaml")) == "()"


@pytest.mark.parametrize("name", SMALL_TEXTS)
def test_a_small_sops_file_of_each_other_kind_names_no_recipient(tmp_path: Path, name: str) -> None:
    body, tail = SMALL_TEXTS[name]
    text = _sops_text("extra:\n" + body) + tail
    assert len(text.encode("utf-8")) <= 4096

    assert _in_child(SOPS_CHILD, text, str(tmp_path / ".sops.yaml")) == "()"


def test_a_sops_file_with_a_character_that_is_no_yaml_names_no_recipient(
    tmp_path: Path,
) -> None:
    assert _recipients(tmp_path, _sops_text(f"# {NOT_A_YAML_CHARACTER}\n")) == ()
