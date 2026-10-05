#!/usr/bin/env python3
# OPERATOR on a development machine, and CI: `uv run bin/yaml-to-toml.py FILE.yaml > FILE.toml`
# The proof of one pair: `uv run bin/yaml-to-toml.py --check FILE.yaml FILE.toml`
"""Convert one YAML file of the four kinds to TOML, or prove one converted pair.

TEMPORARY. This script is a tool of the migration from YAML to TOML. Packet
`toml-converter-leave` removes it when no tracked file of the four kinds is
YAML. It needs PyYAML, which the workspace venv holds, so run it with `uv run`.

The four kinds, by the name of the file:

    family file          family.yaml, *-family.yaml
    server file          server.yaml
    component manifest   component.yaml, *_component.yaml
    roster               upstreams.yaml

The layout of the TOML text:

1. A scalar has a line of its own, and the lines keep the order of the YAML
   file. A value keeps the type that PyYAML gives it: a YAML word such as
   `yes` or `off` becomes `true` or `false`, and a quoted number stays a text.
2. A nested mapping becomes dotted keys: `model.router = "..."`. The text
   holds no table header. A key after a header belongs to that table, so a
   header would change the order of the file.
3. A list becomes an array, and a mapping in a list becomes an inline table
   on one line. A list that is on one line in the YAML file stays on one
   line. Each other list gets one line for each item.
4. A full-line comment and an end-of-line comment keep their places. A
   comment inside a value that is on one line in the TOML text goes above
   that line. A run of empty lines becomes one empty line.
5. A null value gets no key. The script names each one on stderr.
6. In a manifest, the key `release` becomes a boolean: `yes` is `true` and
   `no` is `false`.
7. A text becomes a basic string, with an escape for each control character.

The script refuses eight YAML features: an anchor, an alias, a merge key, a
tag, a second document, a key that is no text, a date and a block text. It
also refuses a value that TOML cannot hold: a null in a list, one key two
times in a mapping, an integer outside 64 bits and half of a surrogate pair.
`Reason` holds each refusal. A refusal names a line and no value of the file.

`--check` reads the TOML text with `tomllib`. It passes only when the two
parsed values are equal after rules 5 and 6 and the two files hold the same
count of comments. The script also runs that check on each text before it
prints the text.

Exit status: 0 for a good run, 1 for a pair that differs, and 2 for each
other end of a run: a file or a command line that the script refuses, an
output that the script cannot write, and a fault that the script does not
name. A Python older than 3.12 cannot read the script and ends with its own
status 1.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import errno
import math
import os
import re
import sys
import tomllib
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TextIO, cast

PROGRAM = "yaml-to-toml"

EXIT_OK = 0
EXIT_DIFFERS = 1
EXIT_REFUSED = 2

try:
    import yaml
except ModuleNotFoundError:
    # A run without `uv run` can get a Python that has no PyYAML. Python ends
    # with status 1 for a fault that no code handles, and status 1 means a
    # pair that differs.
    print(f"{PROGRAM}: PyYAML is missing: start the script with `uv run`", file=sys.stderr)
    raise SystemExit(EXIT_REFUSED) from None

#: The indent of one item of a list that has one line for each item.
INDENT = "    "

#: The start of each tag that PyYAML gives a scalar with no tag of its own.
YAML_TAG = "tag:yaml.org,2002:"

#: Each character that ends a line of a YAML text. `\r\n` is one line end.
LINE_ENDS = "\n\r\x85\u2028\u2029"

#: A TOML key that needs no quotes.
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

#: The escapes that TOML has a short form for.
SHORT_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}

#: The Unicode category of a control character, and that of half a surrogate
#: pair. TOML has an escape for the first one and no form for the second one.
CONTROL = "Cc"
SURROGATE = "Cs"

#: The range of a TOML integer: 64 bits with a sign.
INTEGER_MIN = -(2**63)
INTEGER_MAX = 2**63 - 1

#: The key of a manifest that rule 6 changes, and the two texts that it takes.
RELEASE_KEY = "release"
RELEASE_WORDS = {"yes": True, "no": False}


class FileKind(Enum):
    """The four kinds of file that the script converts. The set is closed."""

    FAMILY = "family file"
    SERVER = "server file"
    MANIFEST = "component manifest"
    ROSTER = "roster"


#: The names of each kind: a whole file name, or the end of one.
WHOLE_NAMES = {
    "family.yaml": FileKind.FAMILY,
    "server.yaml": FileKind.SERVER,
    "component.yaml": FileKind.MANIFEST,
    "upstreams.yaml": FileKind.ROSTER,
}
NAME_ENDS = {
    "-family.yaml": FileKind.FAMILY,
    "_component.yaml": FileKind.MANIFEST,
}


class Reason(Enum):
    """Why the script refuses a file. The first eight are YAML features."""

    ANCHOR = "the file holds an anchor"
    ALIAS = "the file holds an alias"
    MERGE_KEY = "the file holds a merge key"
    TAG = "the file holds a tag"
    SECOND_DOCUMENT = "the file holds a second document"
    KEY_NOT_TEXT = "a key is no text"
    DATE = "the file holds a date"
    BLOCK_TEXT = "the file holds a block text"

    FILE_NAME = "the file name is not the name of one of the four kinds"
    NOT_UTF8 = "the file is not UTF-8 text"
    BYTE_ORDER_MARK = "the file starts with a byte order mark"
    NOT_YAML = "PyYAML does not read the file"
    NOT_A_MAPPING = "the top value of the file is no mapping"
    NULL_IN_LIST = "a list holds a null value, and TOML has no null"
    KEY_TWICE = "a mapping holds one key two times"
    INTEGER = "an integer does not fit 64 bits"
    TEXT = "a text holds a code point that TOML cannot hold"
    DEPTH = "the file nests deeper than the script can read"
    COMMENT = "the script cannot tell the comments of a line from its values"
    NOT_TOML = "tomllib does not read the TOML file"
    OWN_CHECK = "the TOML text that the script wrote fails its own check"


class Refusal(Exception):
    """The script refuses a file. `line` counts from 1."""

    def __init__(self, reason: Reason, line: int | None = None, detail: str = "") -> None:
        self.reason = reason
        self.line = line
        self.detail = detail
        place = "" if line is None else f"line {line}: "
        more = f": {detail}" if detail else ""
        super().__init__(f"{place}{reason.value}{more}")


class Differs(Exception):
    """A YAML file and a TOML file do not hold the same thing."""


# --- what the script holds of a YAML file ------------------------------------

type Plain = bool | int | float | str
type Item = Scalar | Mapping | Sequence


@dataclass(frozen=True)
class Scalar:
    """One value that is no mapping and no list. `end` is past its last sign."""

    value: Plain
    start: int
    end: int


@dataclass(frozen=True)
class Key:
    """One key of a mapping. `column` is where its line holds it."""

    text: str
    start: int
    end: int
    column: int


@dataclass(frozen=True)
class Mapping:
    """One mapping, with each null value dropped."""

    pairs: tuple[tuple[Key, Item], ...]
    start: int
    end: int


@dataclass(frozen=True)
class Sequence:
    """One list. `block` says that the YAML file starts each item with `-`."""

    items: tuple[Item, ...]
    start: int
    end: int
    block: bool
    one_line: bool


@dataclass(frozen=True)
class Null:
    """One null value that the TOML text does not hold."""

    line: int
    path: str


@dataclass(frozen=True)
class Trivia:
    """One comment, or one empty line. An empty line has no text.

    `alone` says that the line holds no value before the comment.
    """

    offset: int
    line: int
    column: int
    text: str
    alone: bool


@dataclass(frozen=True)
class Source:
    """One YAML file after the read: its values, its nulls and its trivia."""

    root: Mapping
    nulls: tuple[Null, ...]
    trivia: tuple[Trivia, ...]
    rows: Rows
    covered: bytes

    @property
    def comments(self) -> int:
        return sum(1 for one in self.trivia if one.text)


@dataclass(frozen=True)
class Converted:
    """The TOML text of one YAML file, and what the proof counts."""

    toml: str
    comments: int
    nulls: tuple[Null, ...]


@dataclass(frozen=True)
class Checked:
    """A pair that passed the check, and what the proof counts."""

    comments: int
    nulls: tuple[Null, ...]


class Rows:
    """Where each line of a text starts and ends. A line number counts from 0."""

    def __init__(self, text: str) -> None:
        self._spans = tuple(_line_spans(text))
        self._starts = [start for start, _end in self._spans]

    def __iter__(self) -> Iterator[tuple[int, int]]:
        return iter(self._spans)

    def line_of(self, offset: int) -> int:
        return max(bisect.bisect_right(self._starts, offset) - 1, 0)

    def end_of_line(self, offset: int) -> int:
        return self._spans[self.line_of(offset)][1]

    def column_of(self, offset: int) -> int:
        return offset - self._spans[self.line_of(offset)][0]


def _line_spans(text: str) -> Iterator[tuple[int, int]]:
    """The start and the end of each line, without the sign that ends it."""
    start = 0
    at = 0
    while at < len(text):
        if text[at] not in LINE_ENDS:
            at += 1
            continue

        yield start, at
        at += 2 if text.startswith("\r\n", at) else 1
        start = at

    if start < len(text):
        yield start, len(text)


# --- the read of a YAML file -------------------------------------------------


def kind_of(name: str) -> FileKind:
    """The kind of a file, from its name alone."""
    whole = WHOLE_NAMES.get(name)
    if whole is not None:
        return whole

    for end, kind in NAME_ENDS.items():
        if name.endswith(end):
            return kind

    raise Refusal(Reason.FILE_NAME, detail=name)


def decode(data: bytes) -> str:
    """The text of a file: strict UTF-8 with no byte order mark."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal(Reason.NOT_UTF8) from None

    if text.startswith("\ufeff"):
        raise Refusal(Reason.BYTE_ORDER_MARK)

    return text


#: The token of each YAML feature that the scan alone can refuse.
REFUSED_TOKENS: tuple[tuple[type[yaml.Token], Reason], ...] = (
    (yaml.AnchorToken, Reason.ANCHOR),
    (yaml.AliasToken, Reason.ALIAS),
    (yaml.TagToken, Reason.TAG),
)

#: The two styles of a block text.
BLOCK_STYLES = ("|", ">")


#: The tokens that hold no value of a document.
NO_CONTENT = (
    yaml.StreamStartToken,
    yaml.StreamEndToken,
    yaml.DirectiveToken,
    yaml.DocumentStartToken,
    yaml.DocumentEndToken,
)


def _covered(text: str) -> bytes:
    """Which characters of the text belong to a token: 1 for each one.

    PyYAML gives no token for a comment. A `#` that no token holds starts a
    comment, and a `#` inside a quoted scalar or a plain scalar starts none.
    The same pass refuses each feature that a token shows.
    """
    covered = bytearray(len(text))
    started = False
    ended = False
    tokens = cast("Iterator[yaml.Token]", yaml.scan(text, Loader=yaml.SafeLoader))
    try:
        for token in tokens:
            line = token.start_mark.line + 1
            for kind, reason in REFUSED_TOKENS:
                if isinstance(token, kind):
                    raise Refusal(reason, line)

            if isinstance(token, yaml.ScalarToken) and token.style in BLOCK_STYLES:
                raise Refusal(Reason.BLOCK_TEXT, line)

            # A second document: a `---` after a value or after another `---`,
            # or a value after `...`.
            if isinstance(token, yaml.DocumentEndToken):
                ended = True
            elif isinstance(token, yaml.DocumentStartToken):
                if started:
                    raise Refusal(Reason.SECOND_DOCUMENT, line)

                started = True
            elif not isinstance(token, NO_CONTENT):
                if ended:
                    raise Refusal(Reason.SECOND_DOCUMENT, line)

                started = True

            for at in range(token.start_mark.index, min(token.end_mark.index, len(text))):
                covered[at] = 1
    except yaml.YAMLError as fault:
        raise _not_yaml(fault) from None

    return bytes(covered)


def _not_yaml(fault: yaml.YAMLError) -> Refusal:
    """The refusal for a fault of PyYAML, with its line.

    The refusal does not hold the words of PyYAML. Some of them quote a sign
    or a word of the file.
    """
    mark = getattr(fault, "problem_mark", None)
    line = mark.line + 1 if isinstance(mark, yaml.Mark) else None

    return Refusal(Reason.NOT_YAML, line)


def _trivia_of(text: str, covered: bytes, rows: Rows) -> tuple[Trivia, ...]:
    """Each comment and each empty line, in the order of the file."""
    found: list[Trivia] = []
    for number, (start, end) in enumerate(rows):
        alone = True
        comment = -1
        for at in range(start, end):
            if covered[at]:
                alone = False
                continue

            if text[at] == "#":
                comment = at
                break

            if text[at] not in " \t":
                raise Refusal(Reason.COMMENT, number + 1)

        if comment >= 0:
            if any(covered[comment:end]):
                raise Refusal(Reason.COMMENT, number + 1)

            words = text[comment:end].rstrip()
            found.append(Trivia(comment, number, comment - start, words, alone))
            continue

        if alone:
            found.append(Trivia(start, number, 0, "", True))

    return tuple(found)


class _Reader:
    """Builds the values of one YAML document and refuses what TOML cannot hold."""

    def __init__(self, text: str, kind: FileKind, rows: Rows) -> None:
        self._kind = kind
        self._rows = rows
        self._loader = yaml.SafeLoader(text)
        self.nulls: list[Null] = []

    def root(self) -> Mapping:
        try:
            node = self._loader.get_node() if self._loader.check_node() else None
            if node is not None and self._loader.check_node():
                raise Refusal(Reason.SECOND_DOCUMENT)
        except yaml.YAMLError as fault:
            raise _not_yaml(fault) from None

        if not isinstance(node, yaml.MappingNode):
            raise Refusal(Reason.NOT_A_MAPPING, None if node is None else _line(node))

        return self._mapping(node, "", top=True)

    def _item(self, node: yaml.Node, path: str) -> Item | None:
        if isinstance(node, yaml.MappingNode):
            return self._mapping(node, path, top=False)

        if isinstance(node, yaml.SequenceNode):
            return self._sequence(node, path)

        if isinstance(node, yaml.ScalarNode):
            return self._scalar(node)

        raise Refusal(Reason.NOT_YAML, _line(node))

    def _mapping(self, node: yaml.MappingNode, path: str, *, top: bool) -> Mapping:
        pairs: list[tuple[Key, Item]] = []
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = self._key(key_node)
            if key.text in seen:
                raise Refusal(Reason.KEY_TWICE, _line(key_node))

            seen.add(key.text)
            below = _dotted_path(path, key.text)
            item = self._item(value_node, below)
            if item is None:
                self.nulls.append(Null(_line(value_node), below))
                continue

            if top and self._kind is FileKind.MANIFEST and key.text == RELEASE_KEY:
                item = _release(item)

            pairs.append((key, item))

        return Mapping(tuple(pairs), node.start_mark.index, self._end(node))

    def _key(self, node: yaml.Node) -> Key:
        if not isinstance(node, yaml.ScalarNode):
            raise Refusal(Reason.KEY_NOT_TEXT, _line(node))

        if node.tag == YAML_TAG + "merge":
            raise Refusal(Reason.MERGE_KEY, _line(node))

        if node.tag != YAML_TAG + "str":
            raise Refusal(Reason.KEY_NOT_TEXT, _line(node))

        _refuse_surrogate(node.value, node)
        start = node.start_mark.index

        return Key(node.value, start, node.end_mark.index, self._rows.column_of(start))

    def _sequence(self, node: yaml.SequenceNode, path: str) -> Sequence:
        items: list[Item] = []
        for index, item_node in enumerate(node.value):
            item = self._item(item_node, f"{path}[{index}]")
            if item is None:
                # CONTRACT-QUESTION: no rule of the TOML form says what a null
                # item of a list becomes, and no tracked file holds one. The
                # reading here: refuse, because a dropped item moves each later
                # item. A change costs this branch (bin/AGENTS.md, Known gaps).
                raise Refusal(Reason.NULL_IN_LIST, _line(item_node))

            items.append(item)

        start = node.start_mark.index
        end = self._end(node)
        block = not node.flow_style
        one_line = not block and self._rows.line_of(start) == self._rows.line_of(end - 1)

        return Sequence(tuple(items), start, end, block, one_line)

    def _scalar(self, node: yaml.ScalarNode) -> Scalar | None:
        value = self._plain(node)
        if value is None:
            return None

        if isinstance(value, str):
            _refuse_surrogate(value, node)
        elif isinstance(value, int) and not INTEGER_MIN <= value <= INTEGER_MAX:
            raise Refusal(Reason.INTEGER, _line(node))

        return Scalar(value, node.start_mark.index, node.end_mark.index)

    def _plain(self, node: yaml.ScalarNode) -> Plain | None:
        """The value of a scalar, as `yaml.safe_load` gives it."""
        kind = node.tag.removeprefix(YAML_TAG)
        try:
            if kind == "str":
                return str(node.value)

            if kind == "null":
                return None

            if kind == "bool":
                return bool(self._loader.construct_yaml_bool(node))

            if kind == "int":
                return int(self._loader.construct_yaml_int(node))

            if kind == "float":
                return float(self._loader.construct_yaml_float(node))
        except (ValueError, yaml.YAMLError):
            raise Refusal(Reason.NOT_YAML, _line(node)) from None

        if kind == "timestamp":
            raise Refusal(Reason.DATE, _line(node))

        if kind == "merge":
            raise Refusal(Reason.MERGE_KEY, _line(node))

        raise Refusal(Reason.NOT_YAML, _line(node))

    def _end(self, node: yaml.Node) -> int:
        """The offset past the last sign of a node.

        PyYAML ends a block mapping and a block list at the next token, which
        can be many lines below. The last value of such a node gives its end.
        """
        if isinstance(node, yaml.MappingNode) and not node.flow_style and node.value:
            return self._end(node.value[-1][1])

        if isinstance(node, yaml.SequenceNode) and not node.flow_style and node.value:
            return self._end(node.value[-1])

        return node.end_mark.index


def _line(node: yaml.Node) -> int:
    return node.start_mark.line + 1


def _refuse_surrogate(text: str, node: yaml.Node) -> None:
    """Refuse half of a surrogate pair. A YAML escape can make one."""
    if any(unicodedata.category(sign) == SURROGATE for sign in text):
        raise Refusal(Reason.TEXT, _line(node))


def _dotted_path(path: str, key: str) -> str:
    """The name of a value for a message, for example `verify.command[2]`."""
    return f"{path}.{key}" if path else key


def _release(item: Item) -> Item:
    """Rule 6: the text `yes` or `no` of the key `release` is a boolean."""
    if not isinstance(item, Scalar) or not isinstance(item.value, str):
        return item

    word = RELEASE_WORDS.get(item.value)
    if word is None:
        return item

    return Scalar(word, item.start, item.end)


def read_yaml(text: str, kind: FileKind) -> Source:
    """Read one YAML text of a kind. Refuse what the script does not convert."""
    covered = _covered(text)
    rows = Rows(text)
    reader = _Reader(text, kind, rows)
    root = reader.root()
    trivia = _trivia_of(text, covered, rows)

    return Source(root, tuple(reader.nulls), trivia, rows, covered)


def plain(item: Item) -> object:
    """The value of an item as `tomllib` gives the same value."""
    if isinstance(item, Scalar):
        return item.value

    if isinstance(item, Sequence):
        return [plain(one) for one in item.items]

    return {key.text: plain(value) for key, value in item.pairs}


# --- the TOML text of one value ----------------------------------------------


def _basic(text: str) -> str:
    """A text as a TOML basic string."""
    parts = ['"']
    for sign in text:
        short = SHORT_ESCAPES.get(sign)
        category = unicodedata.category(sign)
        if short is not None:
            parts.append(short)
        elif category == CONTROL:
            parts.append(f"\\u{ord(sign):04X}")
        elif category == SURROGATE:
            raise Refusal(Reason.TEXT)
        else:
            parts.append(sign)

    parts.append('"')

    return "".join(parts)


def _key_text(key: str) -> str:
    return key if BARE_KEY.fullmatch(key) else _basic(key)


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(_key_text(one) for one in path)


def _number(value: float) -> str:
    if math.isnan(value):
        return "nan"

    if math.isinf(value):
        return "inf" if value > 0 else "-inf"

    return repr(value)


def _inline(item: Item) -> str:
    """One value on one line."""
    if isinstance(item, Sequence):
        return "[" + ", ".join(_inline(one) for one in item.items) + "]"

    if isinstance(item, Mapping):
        pairs = list(_inline_pairs(item, ()))

        return "{ " + ", ".join(pairs) + " }" if pairs else "{}"

    value = item.value
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        return _number(value)

    return _basic(value)


def _inline_pairs(mapping: Mapping, prefix: tuple[str, ...]) -> Iterator[str]:
    """Each `key = value` of an inline table, with dotted keys below it."""
    for key, item in mapping.pairs:
        path = (*prefix, key.text)
        if isinstance(item, Mapping) and item.pairs:
            yield from _inline_pairs(item, path)
            continue

        yield f"{_dotted(path)} = {_inline(item)}"


# --- the lines of the TOML text ----------------------------------------------


@dataclass
class Line:
    """One line of the TOML text, and the part of the YAML file that made it.

    `start` and `end` are offsets in the YAML text. `eol_line` is the YAML
    line whose end-of-line comment this line takes. `inside` is set for the
    `]` of a block list: a full-line comment after the last item is in the
    list when its column is past that number.
    """

    text: str
    start: int
    end: int
    eol_line: int | None
    indent: str = ""
    before_indent: str = ""
    inside: int | None = None
    before: list[str] = field(default_factory=list[str])
    eol: str = ""


class _Layout:
    """Turns the values of a YAML file into the lines of the TOML text."""

    def __init__(self, source: Source) -> None:
        self._rows = source.rows
        self._trivia = source.trivia
        self._covered = source.covered
        self.lines: list[Line] = []
        self.trailing: list[str] = []
        self._mapping(source.root, (), None)
        self._close_block_lists()
        self._place_trivia()

    def _mapping(self, mapping: Mapping, prefix: tuple[str, ...], start: int | None) -> None:
        """Add the lines of a mapping.

        `start` is the offset of the outer key when this mapping is the first
        value below it. The first line then starts there, so a comment on the
        line of the outer key goes above that first line.
        """
        for key, item in mapping.pairs:
            first = key.start if start is None else start
            start = None
            path = (*prefix, key.text)
            if isinstance(item, Mapping) and item.pairs:
                self._mapping(item, path, first)
                continue

            if isinstance(item, Sequence) and item.items and not item.one_line:
                self._list(item, key, _dotted(path), first)
                continue

            last = self._rows.line_of(item.end - 1)
            self.lines.append(Line(f"{_dotted(path)} = {_inline(item)}", first, item.end, last))

    def _list(self, items: Sequence, key: Key, name: str, first: int) -> None:
        """Add the lines of a list that has one line for each item."""
        self.lines.append(Line(f"{name} = [", first, key.end, self._rows.line_of(key.end - 1)))
        for item in items.items:
            last = self._rows.line_of(item.end - 1)
            self.lines.append(Line(f"{_inline(item)},", item.start, item.end, last, INDENT, INDENT))

        if items.block:
            at = self._rows.end_of_line(items.end - 1)
            self.lines.append(Line("]", at, at, None, before_indent=INDENT, inside=key.column))
            return

        close = items.end - 1
        self.lines.append(
            Line("]", close, items.end, self._rows.line_of(close), before_indent=INDENT)
        )

    def _close_block_lists(self) -> None:
        """Move the end of each block list past the comments that belong to it.

        A block list has no sign for its end. A full-line comment after the
        last item belongs to the list when its indent is deeper than the key
        of the list and no value is between the two.
        """
        for line in self.lines:
            if line.inside is None:
                continue

            last_item = line.start
            for one in self._trivia:
                if one.offset < last_item or not one.text:
                    continue

                if one.column <= line.inside or any(self._covered[last_item : one.offset]):
                    break

                line.end = one.offset + 1

    def _place_trivia(self) -> None:
        """Give each comment and each empty line to one line of the TOML text."""
        starts = [line.start for line in self.lines]
        ends = [line.end for line in self.lines]
        for one in self._trivia:
            if one.text and not one.alone and self._takes_eol(starts, one):
                continue

            after = bisect.bisect_right(ends, one.offset)
            if after == len(self.lines):
                self.trailing.append(one.text)
                continue

            line = self.lines[after]
            if not one.text and line.inside is None and line.start <= one.offset:
                # An empty line inside a value that is on one line now.
                continue

            line.before.append(one.text)

    def _takes_eol(self, starts: list[int], comment: Trivia) -> bool:
        """Put an end-of-line comment on the line whose value it follows."""
        before = bisect.bisect_right(starts, comment.offset) - 1
        if before < 0:
            return False

        line = self.lines[before]
        if line.eol or line.eol_line != comment.line:
            return False

        line.eol = comment.text

        return True

    def text(self) -> str:
        rows: list[str] = []
        for line in self.lines:
            rows.extend(f"{line.before_indent}{one}" if one else "" for one in line.before)
            rows.append(f"{line.indent}{line.text} {line.eol}".rstrip())

        rows.extend(self.trailing)

        return "".join(f"{row}\n" for row in _one_empty_line(rows))


def _one_empty_line(rows: list[str]) -> Iterator[str]:
    """The rows with no empty row at the start or at the end, and no two in a row."""
    last = len(rows)
    while last and not rows[last - 1]:
        last -= 1

    previous = ""
    for row in rows[:last]:
        if row or previous:
            yield row

        previous = row


# --- the proof ---------------------------------------------------------------


def toml_comments(text: str) -> int:
    """The count of comments of a TOML text that `tomllib` reads.

    A `#` starts a comment when it is outside each of the four string forms.
    """
    count = 0
    at = 0
    while at < len(text):
        sign = text[at]
        if sign == "#":
            count += 1
            end = text.find("\n", at)
            at = len(text) if end < 0 else end
        elif sign in "\"'":
            at = _string_end(text, at)
        else:
            at += 1

    return count


def _string_end(text: str, at: int) -> int:
    """The offset past the TOML string that starts at `at`."""
    quote = text[at]
    escapes = quote == '"'
    fence = quote * 3
    if not text.startswith(fence, at):
        at += 1
        while at < len(text) and text[at] != quote:
            at += 2 if escapes and text[at] == "\\" else 1

        return at + 1

    at += len(fence)
    while at < len(text) and not text.startswith(fence, at):
        at += 2 if escapes and text[at] == "\\" else 1

    at += len(fence)
    # A multi-line string can end with one or two quotes before its fence.
    while at < len(text) and text[at] == quote:
        at += 1

    return at


def _difference(left: object, right: object, path: str) -> str | None:
    """The name of the first value that differs, or None for equal values.

    `left` is from the YAML file and `right` is from the TOML file. A boolean,
    an integer and a float are three types here: `1`, `1.0` and `true` differ.
    The order of the keys of a mapping does not count. The answer names the
    place and never the value.
    """
    here = f"`{path}`" if path else "the top value"
    if type(left) is not type(right):
        return f"{here} has another type"

    if isinstance(left, dict):
        return _mapping_difference(
            cast("dict[str, object]", left), cast("dict[str, object]", right), path
        )

    if isinstance(left, list):
        return _list_difference(cast("list[object]", left), cast("list[object]", right), path)

    if isinstance(left, float) and isinstance(right, float):
        same = (math.isnan(left) and math.isnan(right)) or (
            left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
        )

        return None if same else f"{here} has another value"

    return None if left == right else f"{here} has another value"


def _list_difference(left: list[object], right: list[object], path: str) -> str | None:
    if len(left) != len(right):
        return f"`{path}` has another count of items"

    for index, (one, other) in enumerate(zip(left, right, strict=True)):
        found = _difference(one, other, f"{path}[{index}]")
        if found is not None:
            return found

    return None


def _mapping_difference(left: dict[str, object], right: dict[str, object], path: str) -> str | None:
    for key, value in left.items():
        below = _dotted_path(path, key)
        if key not in right:
            return f"only the YAML file holds `{below}`"

        found = _difference(value, right[key], below)
        if found is not None:
            return found

    for key in right:
        if key not in left:
            return f"only the TOML file holds `{_dotted_path(path, key)}`"

    return None


def _check(source: Source, toml_text: str) -> Checked:
    try:
        value = tomllib.loads(toml_text)
    except (tomllib.TOMLDecodeError, RecursionError):
        raise Refusal(Reason.NOT_TOML) from None

    found = _difference(plain(source.root), value, "")
    if found is not None:
        raise Differs(f"the values differ: {found}")

    count = toml_comments(toml_text)
    if count != source.comments:
        raise Differs(
            f"the YAML file holds {source.comments} comments and the TOML file holds {count}"
        )

    return Checked(count, source.nulls)


def check(yaml_text: str, toml_text: str, kind: FileKind) -> Checked:
    """Prove one pair: equal values after rules 5 and 6, and equal counts of comments."""
    try:
        return _check(read_yaml(yaml_text, kind), toml_text)
    except RecursionError:
        raise Refusal(Reason.DEPTH) from None


def convert(yaml_text: str, kind: FileKind) -> Converted:
    """The TOML text of one YAML text. The text passed the check of `--check`."""
    try:
        source = read_yaml(yaml_text, kind)
        toml_text = _Layout(source).text()
        checked = _own_check(source, toml_text)
    except RecursionError:
        raise Refusal(Reason.DEPTH) from None

    return Converted(toml_text, checked.comments, checked.nulls)


def _own_check(source: Source, toml_text: str) -> Checked:
    try:
        return _check(source, toml_text)
    except (Differs, Refusal) as fault:
        raise Refusal(Reason.OWN_CHECK, detail=str(fault)) from None


# --- the command line --------------------------------------------------------


def _arguments(argv: list[str]) -> tuple[Path, Path | None]:
    """The YAML file, and the TOML file of `--check`. None asks for the TOML text."""
    parser = argparse.ArgumentParser(
        prog=f"{PROGRAM}.py",
        description="Convert one YAML file of the four kinds to TOML, or prove one pair.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="prove that FILE.toml holds the values and the count of comments of FILE.yaml",
    )
    parser.add_argument("yaml", type=Path, metavar="FILE.yaml")
    parser.add_argument("toml", type=Path, metavar="FILE.toml", nargs="?")
    parsed = parser.parse_args(argv)
    yaml_path = cast("Path", parsed.yaml)
    toml_path = cast("Path | None", parsed.toml)
    if parsed.check and toml_path is None:
        parser.error("--check needs FILE.yaml and FILE.toml")

    if not parsed.check and toml_path is not None:
        parser.error("only --check takes FILE.toml")

    return yaml_path, toml_path


def _read(path: Path) -> str:
    try:
        return decode(path.read_bytes())
    except Refusal as refusal:
        raise Refusal(refusal.reason, detail=str(path)) from None


def _write(text: str) -> None:
    """Write UTF-8 to stdout, under each locale."""
    # Python gives `sys.stdout` the value None when it starts with stdout closed.
    stream = cast("TextIO | None", sys.stdout)
    if stream is None:
        raise OSError(errno.EBADF, "stdout is closed")

    try:
        stream.buffer.write(text.encode("utf-8"))
        stream.buffer.flush()
    except OSError:
        _drop_stdout(stream)
        raise


def _drop_stdout(stream: TextIO) -> None:
    """Point stdout at the null device after a write that failed.

    Python flushes stdout one more time at its end. The bytes that the failed
    write left in the buffer fail that flush too, and Python then replaces the
    exit status of the script with its own.
    """
    with contextlib.suppress(OSError):
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, stream.fileno())
        finally:
            os.close(null)


def _nulls_text(nulls: tuple[Null, ...]) -> str:
    return "1 null dropped" if len(nulls) == 1 else f"{len(nulls)} nulls dropped"


def _run(yaml_path: Path, toml_path: Path | None) -> None:
    kind = kind_of(yaml_path.name)
    yaml_text = _read(yaml_path)
    if toml_path is not None:
        checked = check(yaml_text, _read(toml_path), kind)
        _write(
            f"{PROGRAM}: equal: {yaml_path} and {toml_path}:"
            f" {checked.comments} comments, {_nulls_text(checked.nulls)}\n"
        )
        return

    converted = convert(yaml_text, kind)
    for null in converted.nulls:
        print(
            f"{PROGRAM}: {yaml_path}: line {null.line}: dropped the null value of `{null.path}`",
            file=sys.stderr,
        )

    _write(converted.toml)


def _os_fault_text(fault: OSError) -> str:
    """What the system says of a fault, after the file that the fault names."""
    what = fault.strerror or type(fault).__name__
    if fault.filename is None:
        return what

    return f"{fault.filename}: {what}"


def main(argv: list[str]) -> int:
    yaml_path, toml_path = _arguments(argv)
    try:
        _run(yaml_path, toml_path)
    except Differs as fault:
        print(f"{PROGRAM}: {yaml_path}: {fault}", file=sys.stderr)
        return EXIT_DIFFERS
    except Refusal as fault:
        print(f"{PROGRAM}: {yaml_path}: {fault}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as fault:
        print(f"{PROGRAM}: {_os_fault_text(fault)}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as fault:
        # Python ends with status 1 for a fault that no code handles, and
        # status 1 means a pair that differs. The line names the type alone,
        # because the text of a fault can hold a value of the file.
        print(
            f"{PROGRAM}: {yaml_path}: the script stopped: {type(fault).__name__}", file=sys.stderr
        )
        return EXIT_REFUSED

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
