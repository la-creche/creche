"""The two limits of the YAML reader on merge keys.

The reader follows a chain of 400 merge keys at most. It makes 100,000
entries from merge keys at most. It refuses a text past a limit with a
report, so that a short text cannot use much time and memory.

A text that is far past a limit runs in a child process with a time limit
and a memory limit. A reader with no limit fails that test, and it takes no
more than the limits of the child."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import pytest
from agent_family.parse import parse_family, parse_server
from agent_family.report import Issue

#: The two limits, as numbers of their own. The tests do not read the
#: constants of the reader, so a changed number there fails a test here. The
#: Rust reader of the same files has the same two numbers.
CHAIN_MAX: Final = 400
ENTRIES_MAX: Final = 100_000

TOO_DEEP: Final = "YAML will not parse: the merge keys nest too deep"
TOO_MANY: Final = "YAML will not parse: the merge keys make too many entries"

#: The count of entries in the mapping that `_entries` merges.
BLOCK: Final = 1_000

#: A count of levels and of merge keys that is far past the limit on
#: entries.
FAR: Final = 40


@dataclass(frozen=True)
class Reader:
    """One reader, and a file that it accepts.

    The file writes one key more than one time, and the last value stays.
    Each mapping that a test adds is a value of that key, so the file stays
    valid when the YAML reader reads it."""

    parse: Callable[[str], tuple[object | None, list[Issue]]]
    head: str
    key: str
    last: str

    def text(self, values: list[str], end: str = "") -> str:
        lines = [f"{self.key}: {value}" for value in values]

        return self.head + "\n".join(lines) + f"\n{self.key}: {self.last}\n" + end


FAMILY: Final = Reader(
    parse_family,
    "name: probe\n"
    "kind: thin\n"
    "description: One written input.\n"
    "model: { router: fast, budget_usd_per_day: 1 }\n",
    "shell",
    "false",
)
SERVER: Final = Reader(
    parse_server,
    "name: probe\n"
    "identity: One written server.\n"
    "install: { source: agent-mcp }\n"
    "run: { entrypoint: example-mcp }\n",
    "tools",
    "[]",
)
READERS: Final = pytest.mark.parametrize("reader", [FAMILY, SERVER], ids=["family", "server"])


def _chain(reader: Reader, keys: int) -> str:
    """A file with a chain of `keys` merge keys."""
    values = ["&c0 {}"]
    values.extend(f"&c{link} {{ <<: *c{link - 1} }}" for link in range(1, keys))

    return reader.text(values, f"<<: *c{keys - 1}\n")


def _list_of_merges(reader: Reader, keys: int) -> str:
    """A file with `keys` mappings, where each one merges the one before it
    and no mapping merges the last one."""
    values = ["&c0 {}"]
    values.extend(f"&c{link} {{ <<: *c{link - 1} }}" for link in range(1, keys + 1))

    return reader.text(values)


def _mapping(entries: int) -> str:
    return "{ " + ", ".join(f"k{number}: 0" for number in range(entries)) + " }"


def _entries(reader: Reader, count: int) -> str:
    """A file whose merge keys make `count` entries."""
    blocks, rest = divmod(count, BLOCK)
    values = [f"&block {_mapping(BLOCK)}", "{ <<: [" + ", ".join(["*block"] * blocks) + "] }"]
    if rest:
        values.extend([f"&rest {_mapping(rest)}", "{ <<: *rest }"])

    return reader.text(values)


def _levels(reader: Reader, levels: int, key: str = "<<") -> str:
    """A file whose merge keys make more entries than the limit when `levels`
    is 16 or more."""
    values = ["&a0 { k: 0 }"]
    values.extend(
        f"&a{level} {{ {key}: [*a{level - 1}, *a{level - 1}] }}" for level in range(1, levels + 1)
    )

    return reader.text(values)


def _merge_tag(reader: Reader, levels: int) -> str:
    """The file of `_levels`, with the merge tag in place of the merge key."""
    return _levels(reader, levels, "!!merge k")


def _bad_later_document(reader: Reader, levels: int) -> str:
    """The file of `_levels`, and a second document that the reader refuses."""
    return _levels(reader, levels) + f"---\n{reader.key}: *nowhere\n"


def _self_merge(reader: Reader, keys: int) -> str:
    """A file with one mapping that merges itself. Its merge keys make more
    entries than the limit when `keys` is 11 or more."""
    merges = ", ".join(["<<: [*a, *a]"] * keys)

    return reader.text([f"&a {{ k: 0, {merges} }}"])


def _messages(reader: Reader, text: str) -> list[str] | None:
    """The messages of the report, or None when the reader reads the file."""
    value, issues = reader.parse(text)
    if value is not None:
        return None

    return [issue.msg for issue in issues]


# --- the limits ---


@READERS
def test_a_chain_at_the_limit_reads(reader: Reader) -> None:
    assert _messages(reader, _chain(reader, CHAIN_MAX)) is None


@READERS
def test_a_chain_past_the_limit_is_refused(reader: Reader) -> None:
    assert _messages(reader, _chain(reader, CHAIN_MAX + 1)) == [TOO_DEEP]


@READERS
def test_entries_at_the_limit_read(reader: Reader) -> None:
    assert _messages(reader, _entries(reader, ENTRIES_MAX)) is None


@READERS
def test_entries_past_the_limit_are_refused(reader: Reader) -> None:
    assert _messages(reader, _entries(reader, ENTRIES_MAX + 1)) == [TOO_MANY]


def test_the_entry_limit_holds_for_the_text_not_for_one_document() -> None:
    half = _entries(FAMILY, ENTRIES_MAX // 2 + BLOCK)

    assert _messages(FAMILY, f"{half}---\n{half}") == [TOO_MANY]


# --- what the limits leave as it was ---


def test_a_merge_key_still_reads() -> None:
    text = (
        FAMILY.head + "files:\n"
        "  - &vault { path: /srv/agents/vault, mode: ro }\n"
        "  - { <<: *vault, path: /srv/agents/code }\n"
    )

    family, issues = parse_family(text)

    assert family is not None, [issue.msg for issue in issues]
    assert [mount.path for mount in family.files] == ["/srv/agents/vault", "/srv/agents/code"]


@READERS
def test_a_list_of_merges_is_no_chain(reader: Reader) -> None:
    """No mapping merges the last one, so the reader follows one merge key
    at a time. The text has more merge keys than the chain limit and reads."""
    assert _messages(reader, _list_of_merges(reader, CHAIN_MAX + 1)) is None


@pytest.mark.parametrize(
    ("build", "last_read"),
    [(_levels, 15), (_merge_tag, 15), (_self_merge, 10)],
    ids=["merge-key", "merge-tag", "self-merge"],
)
def test_the_reader_counts_each_entry_that_a_merge_makes(
    build: Callable[[Reader, int], str], last_read: int
) -> None:
    """The count is the count of the Rust reader: the text with `last_read`
    levels reads, and the text with one more level is refused."""
    assert _messages(FAMILY, build(FAMILY, last_read)) is None
    assert _messages(FAMILY, build(FAMILY, last_read + 1)) == [TOO_MANY]


def test_a_bad_later_document_after_a_small_one_keeps_its_message() -> None:
    messages = _messages(FAMILY, _bad_later_document(FAMILY, 15))

    assert messages is not None
    assert [message.partition("\n")[0] for message in messages] == [
        "YAML will not parse: found undefined alias 'nowhere'"
    ]


@READERS
def test_a_character_that_the_reader_refuses_is_a_report(reader: Reader) -> None:
    """The reader of the characters runs before the first document. Its
    refusal is a report, also for a text that names a merge key."""
    messages = _messages(reader, reader.head + "# <<\x00\n")

    assert messages is not None
    assert len(messages) == 1
    assert messages[0].startswith("YAML will not parse: unacceptable character #x0000")


# --- far past a limit, in a child process ---

#: The limits of the child process. A reader that refuses the text ends in
#: less than one second and uses little memory.
CHILD_SECONDS: Final = 20.0
CHILD_BYTES: Final = 256 * 1024 * 1024

#: Reads the text on its standard input with both readers. Prints the
#: messages of each report as one line of JSON, or `null` for a text that
#: the reader reads. The argument is the memory limit of the process, in
#: bytes. The process ends with status 3 when it passes the limit.
CHILD: Final = """
import json
import os
import resource
import sys
import threading
import time

LIMIT = int(sys.argv[1])


def peak_bytes():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def watch():
    while peak_bytes() <= LIMIT:
        time.sleep(0.01)

    os._exit(3)


try:
    resource.setrlimit(resource.RLIMIT_AS, (4 * LIMIT, 4 * LIMIT))
except (OSError, ValueError):
    pass

threading.Thread(target=watch, daemon=True).start()

from agent_family.parse import parse_family, parse_server

text = sys.stdin.buffer.read().decode("utf-8")
for parse in (parse_family, parse_server):
    value, issues = parse(text)
    print(json.dumps(None if value is not None else [issue.msg for issue in issues]))
"""


def _in_child(text: str) -> list[list[str] | None]:
    """The answer of `parse_family` and of `parse_server` for `text`, from a
    child process inside the two limits."""
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(CHILD_BYTES)],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=CHILD_SECONDS,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")

    return [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]


@pytest.mark.parametrize(
    "build",
    [_levels, _merge_tag, _bad_later_document, _self_merge],
    ids=["merge-key", "merge-tag", "bad-later-document", "self-merge"],
)
def test_a_text_far_past_the_entry_limit_is_refused_in_bounds(
    build: Callable[[Reader, int], str],
) -> None:
    assert _in_child(build(FAMILY, FAR)) == [[TOO_MANY], [TOO_MANY]]


def test_a_refused_character_is_a_report_in_a_child_process() -> None:
    family, server = _in_child(FAMILY.head + "# <<\x00\n")

    assert family is not None
    assert family == server
    assert len(family) == 1
    assert family[0].startswith("YAML will not parse: unacceptable character #x0000")
