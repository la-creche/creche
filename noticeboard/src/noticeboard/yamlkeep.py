"""Saving a family file without losing what it says about itself.

`yamlout.py` builds a whole document from the model. That is right for a
preview and for a family that has no file yet, and wrong for a save: the
real family files carry a comment above every grant explaining why it is
there, and regenerating the document dropped all of them on the first save.

So a save patches the bytes that are already on disk. `edited_text` loads
the original round-trip, walks the two MODEL dumps side by side, and writes
only the fields whose value actually moved. Comments, key order, flow style
and quoting on every other field come back byte for byte.

    original text ─┐
                   ├─► edited_text ──► original text with N fields replaced
    before, after ─┘        │
                            └─ None, when the original cannot be round-tripped

Three rules make this safe.

1. **A field that did not move is not touched.** The two dumps come from
   the same model, so every field is present in both. A default the author
   never wrote stays unwritten, because its `before` and its `after` are
   the same value.
2. **An unchanged save returns the original bytes.** Not a re-dump of them.
   `registrywrite.save_family` compares the new text with the file and makes
   no commit when they match, which is only reachable if this is exact.
3. **`None` is a real answer.** A document that is not a mapping, that
   does not parse, or that the library cannot build, is handed back to the
   caller rather than guessed at. The caller falls back to `yamlout.to_yaml`.

This module owns the only YAML library in the noticeboard, and it owns no meaning:
it moves values between two structures the reader already produced. The
registry still has exactly one PARSER, `agent_family`. `app.py` gives the
patched text back to it and writes the emitter's document when the model
does not match (`AGENTS.md` rule 24).
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping, MutableMapping
from typing import Any, Final, Protocol, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

#: The repo's own line budget. It keeps a flow sequence wrapping where the
#: hand-written files already wrap it.
YAML_WIDTH: Final = 100


def edited_text(original: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> str | None:
    """`original`, with only the fields that moved between the two models
    replaced. `None` when `original` cannot be round-tripped."""
    if before == after:
        # Rule 2. Not a re-dump: a dump that differs by one space would make
        # every no-op save a commit.
        return original

    document = _load(original)

    if document is None:
        return None

    _merge(document, before, after)

    return _spaced_like(original, _unfolded_like(original, _dump(document)))


#: A whole line that is nothing but a flow mapping, behind an optional
#: sequence dash and an optional key, with an optional comment after it.
#: Every value in contract 01's file is a path, a slug or a number, so the
#: closing brace is the last one before the comment. A mapping that holds a
#: `#` does not match, and stays as ruamel wrote it.
_FLOW_LINE: Final = re.compile(r"^(\s*(?:- )?(?:[a-z_]+: )?)\{(\S(?:[^#]*\S)?)\}(\s*|\s+#.*)\Z")

_SPACED_FLOW: Final = re.compile(r"\{ \S")

#: A flow mapping written without the inner spaces. `{}` is excluded: an
#: EMPTY mapping has one spelling in both styles, and `embed: {}` in the
#: locked `chat` family would otherwise read as "this file is tight style".
_TIGHT_FLOW: Final = re.compile(r"\{[^\s}]")


def _spaced_like(original: str, dumped: str) -> str:
    """ruamel emits `{a: b}`. The hand-written files use `{ a: b }`.

    Round-trip mode records that a node is flow style and not how wide its
    braces were, so without this every save rewrites flow lines it did not
    otherwise touch. The registry's git log is the grant audit trail, and a
    one-field edit that churns ten lines is noise in it.

    Only when the original is unambiguously in the spaced style: a file that
    mixes both is left as ruamel wrote it rather than guessed at.
    """
    if not _SPACED_FLOW.search(original) or _TIGHT_FLOW.search(original):
        return dumped

    lines = dumped.splitlines(keepends=True)

    for index, line in enumerate(lines):
        text = line.rstrip("\n")
        found = _FLOW_LINE.match(text)

        if found is None:
            continue

        tail = "\n" if line.endswith("\n") else ""
        flow = f"{found.group(1)}{{ {found.group(2)} }}"
        lines[index] = f"{flow}{_comment_at_its_column(text, flow, found.group(3))}{tail}"

    return "".join(lines)


def _comment_at_its_column(dumped: str, flow: str, rest: str) -> str:
    """The comment after a flow mapping, back where the author put it.

    ruamel keeps the column of a comment. The two spaces that `flow` got back
    thus come out of the padding before the comment, and one space stays.
    """
    comment = rest.lstrip()

    if not comment:
        return ""

    column = len(dumped) - len(comment)

    return " " * max(1, column - len(flow)) + comment


def _unfolded_like(original: str, dumped: str) -> str:
    """ruamel folds a line at `YAML_WIDTH`. A hand-written file can hold a
    longer one, and each save then rewrote a line that it did not edit.

    A YAML reader joins the lines of a folded value with one space. So when
    that join gives back a line of the original, the value did not move and
    the line returns whole. No other line changes.
    """
    held = {one for one in original.splitlines() if len(one) > YAML_WIDTH}

    if not held:
        return dumped

    lines = dumped.split("\n")
    kept: list[str] = []
    index = 0

    while index < len(lines):
        # ruamel leaves a space at the end of a flow sequence line that it
        # folds after a comma.
        joined = lines[index].rstrip()
        after = index + 1

        while after < len(lines) and lines[after].strip() and _starts_one_of(joined, held):
            joined = f"{joined} {lines[after].strip()}"
            after += 1

        if joined in held:
            kept.append(joined)
            index = after
        else:
            kept.append(lines[index])
            index += 1

    return "\n".join(kept)


def _starts_one_of(text: str, held: set[str]) -> bool:
    """True when `text` and one space start a line that the original holds."""
    return any(one.startswith(f"{text} ") for one in held)


class _RoundTrip(Protocol):
    """ruamel ships no type information, so its two calls are named here
    once. Everything past `_load` is an ordinary `MutableMapping`, which is
    what `CommentedMap` is."""

    def load(self, stream: str) -> object: ...

    def dump(self, data: Mapping[str, Any], stream: io.StringIO) -> None: ...


def _yaml() -> _RoundTrip:
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = YAML_WIDTH
    parser.indent(mapping=2, sequence=4, offset=2)

    return cast("_RoundTrip", parser)


def _load(text: str) -> MutableMapping[str, Any] | None:
    try:
        loaded = _yaml().load(text)
    except Exception:
        # Each exception, not only the error type of the library. The library
        # builds a value with `int`, `float` and a date, and each one raises
        # its own type for a scalar that has no value. A nesting past the
        # stack raises `RecursionError`.
        return None

    if not isinstance(loaded, CommentedMap):
        return None

    return cast("MutableMapping[str, Any]", loaded)


def _dump(document: Mapping[str, Any]) -> str:
    buffer = io.StringIO()
    _yaml().dump(document, buffer)

    return buffer.getvalue()


def _merge(
    node: MutableMapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    """Replace, inside `node`, exactly the keys whose value moved."""
    for key, wanted in after.items():
        if key in before and before[key] == wanted:
            continue

        _set(node, key, before.get(key), wanted)

    # A key the edit removed. Both dumps carry every schema field, so this
    # only ever fires inside a free-form block such as `tools:`.
    for key in [one for one in node if one not in after]:
        del node[key]


def _set(node: MutableMapping[str, Any], key: str, was: object, wanted: object) -> None:
    """One key. A nested mapping recurses, so a one-field edit inside a
    block leaves that block's other keys and their comments alone."""
    inner: object = node.get(key)
    was_block = isinstance(inner, MutableMapping)

    if was_block and isinstance(wanted, Mapping) and wanted:
        _merge(
            cast("MutableMapping[str, Any]", inner),
            cast("Mapping[str, Any]", was) if isinstance(was, Mapping) else {},
            cast("Mapping[str, Any]", wanted),
        )
        return

    if was_block:
        # The block goes whole: the edit removed each key of it, or put a
        # value of another kind in its place.
        _drop_block_comment(node, key)

    # A list is replaced whole. Matching an edited item to the item it came
    # from is guesswork, and a wrong guess moves a comment onto the wrong
    # grant -- which is worse than losing it.
    node[key] = wanted


class _Comments(Protocol):
    """What ruamel keeps beside a mapping: for each key, a list of four
    comment slots. ruamel ships no type information, so the one member this
    module reads is named here."""

    items: dict[str, list[object]]


class _Commented(Protocol):
    ca: _Comments


#: The slot of a comment that stands between a key and its value: a comment
#: above the first key of a block.
_BEFORE_VALUE: Final = 3


def _drop_block_comment(node: MutableMapping[str, Any], key: str) -> None:
    """Forget the comment above the first key of a block that goes.

    ruamel keeps that comment on the parent. Left there, it comes out
    between the key and its new value, and the emitter then writes an empty
    mapping at column 0, which no reader parses. The comment explained a key
    that the edit removed.
    """
    if not isinstance(node, CommentedMap):
        return

    slots = cast("_Commented", node).ca.items.get(key)

    if slots is not None and len(slots) > _BEFORE_VALUE:
        slots[_BEFORE_VALUE] = None
