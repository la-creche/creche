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
3. **`None` is a real answer.** A document that is not a mapping, or that
   does not parse, is handed back to the caller rather than guessed at. The
   caller falls back to `yamlout.to_yaml`.

This module owns the only YAML library in the noticeboard, and it owns no meaning:
it moves values between two structures the reader already produced. The
registry still has exactly one PARSER, `agent_family`, and
`registrywrite.py` still re-reads what it wrote and refuses a save whose
model does not match (`AGENTS.md` rule 24).
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping, MutableMapping
from typing import Any, Final, Protocol, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

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

    return _spaced_like(original, _dump(document))


#: A whole line that is nothing but a flow mapping, behind an optional
#: sequence dash and an optional key. Every value in contract 01's file is a
#: path, a slug or a number, so the closing brace is unambiguous.
_FLOW_LINE: Final = re.compile(r"^(\s*(?:- )?(?:[a-z_]+: )?)\{(\S(?:.*\S)?)\}\s*$")

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
        found = _FLOW_LINE.match(line.rstrip("\n"))

        if found is None:
            continue

        tail = "\n" if line.endswith("\n") else ""
        lines[index] = f"{found.group(1)}{{ {found.group(2)} }}{tail}"

    return "".join(lines)


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
    except YAMLError:
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

    if isinstance(inner, MutableMapping) and isinstance(wanted, Mapping):
        _merge(
            cast("MutableMapping[str, Any]", inner),
            cast("Mapping[str, Any]", was) if isinstance(was, Mapping) else {},
            cast("Mapping[str, Any]", wanted),
        )
        return

    # A list is replaced whole. Matching an edited item to the item it came
    # from is guesswork, and a wrong guess moves a comment onto the wrong
    # grant -- which is worse than losing it.
    node[key] = wanted
