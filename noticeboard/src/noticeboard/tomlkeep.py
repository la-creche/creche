"""Saving a TOML family file without losing what it says about itself.

This module is the TOML twin of `yamlkeep.py`. `tomlout.py` builds a whole
text from the model, and that text has no comment. So a save patches the
bytes that are on disk. `edited_text` reads the original with `tomlkit`,
walks the two MODEL dumps side by side, and writes only the fields whose
value moved. Comments, key order and the layout of each other field come
back byte for byte.

The three rules of `yamlkeep.py` apply here too:

1. **A field that did not move is not touched.** Its `before` and its
   `after` are the same value.
2. **An unchanged save returns the original bytes.** Not a new dump of them.
3. **`None` is a real answer.** A text that `tomlkit` cannot read, or an
   edit that this module cannot make in the layout of the file, goes back
   to the caller. The caller then writes the text of `tomlout.to_toml`.

How a value that moved reaches the text:

1. A key whose new value is `None` goes. TOML has no null, and an absent
   key reads as `None` in the model.
2. A table that holds keys in the file and in the edit takes the edit key
   by key. A key that the edit removed goes.
3. A table in a place where the file holds no table becomes dotted keys:
   `quiet.daily.hour = 7`. The keys go at the end of the table that holds
   them, or at the end of the text. In an inline table this module adds no
   table, and the answer is `None`.
4. Each other value takes the text that `tomlout.py` writes for it, in the
   place of the old value: a scalar, a list or an empty table. A list goes
   whole.

A save keeps each comment line outside a list that it replaces. A comment
line above a key that the edit removed stays where it was.

This module owns the one TOML library of the noticeboard that keeps a
layout, and it reads no meaning: it moves values between two structures that
the reader already made. `agent_family` stays the one reader of the
registry. `app.py` gives the patched text back to it, and writes the text of
`tomlout.py` when the model does not match (`AGENTS.md` rule 24).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any, Final, cast

import tomlkit
from tomlkit.items import InlineTable, Item, Table

from .tomlout import to_toml

#: The one key of the text that `_item_of` reads a value from.
_SLOT: Final = "value"


class _CannotKeep(Exception):
    """An edit that this module cannot make in the layout of the file."""


def edited_text(original: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> str | None:
    """`original`, with only the fields that moved between the two models
    replaced. `None` when `original` cannot be kept."""
    if before == after:
        # Rule 2. Not a new dump: a dump that differs by one space would make
        # each save with no edit a commit.
        return original

    try:
        document = tomlkit.parse(original)
        _merge(document, document, (), before, after)

        return document.as_string()
    except Exception:
        # Each exception, not only the error types of the library. The library
        # raises its own types for a text that it cannot read and for an edit
        # that it cannot make, and a nesting past the stack raises
        # `RecursionError`. Each one is rule 3.
        return None


def _merge(
    document: tomlkit.TOMLDocument,
    node: MutableMapping[str, Any],
    path: tuple[str, ...],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    """Replace, inside `node`, exactly the keys whose value moved."""
    for key, wanted in after.items():
        if key in before and before[key] == wanted:
            continue

        _set(document, node, (*path, key), before.get(key), wanted)

    # A key that the edit removed. Both dumps hold each field of the schema,
    # so this applies only inside a free-form table such as `tools`.
    for key in [one for one in node if one not in after]:
        del node[key]


def _set(
    document: tomlkit.TOMLDocument,
    node: MutableMapping[str, Any],
    path: tuple[str, ...],
    was: object,
    wanted: object,
) -> None:
    """One key. A table in the file and in the edit takes the edit key by
    key, so a one-field edit leaves the other keys and their comments."""
    key = path[-1]
    held: object = node.get(key)

    if wanted is None:
        if key in node:
            del node[key]
        return

    if _holds_a_value(wanted) and _is_kept_table(held):
        _merge(
            document,
            cast("MutableMapping[str, Any]", held),
            path,
            cast("Mapping[str, Any]", was) if isinstance(was, Mapping) else {},
            cast("Mapping[str, Any]", wanted),
        )
        return

    if _holds_a_value(wanted):
        _add_table(document, node, path, cast("Mapping[str, object]", wanted))
        return

    node[key] = _item_of(wanted)


def _holds_a_value(value: object) -> bool:
    """True for a mapping with a key whose value gets a line."""
    if not isinstance(value, Mapping):
        return False

    return any(one is not None for one in cast("Mapping[str, object]", value).values())


def _is_kept_table(held: object) -> bool:
    """True for a table of the file with a key in it."""
    return isinstance(held, MutableMapping) and len(cast("MutableMapping[str, Any]", held)) > 0


def _add_table(
    document: tomlkit.TOMLDocument,
    node: MutableMapping[str, Any],
    path: tuple[str, ...],
    wanted: Mapping[str, object],
) -> None:
    """Rule 3: a new table, as one dotted key for each value of it."""
    if isinstance(node, InlineTable):
        raise _CannotKeep(".".join(path))

    key = path[-1]

    if key in node:
        del node[key]

    # A table of the file takes the keys at its own end. The view that
    # `tomlkit` gives of a table whose keys are apart in the file can add
    # none, so the text takes the keys at its end, under the whole path.
    if isinstance(node, tomlkit.TOMLDocument | Table):
        for leaf, value in _leaves((key,), wanted):
            node.add(tomlkit.key(list(leaf)), _item_of(value))
        return

    for leaf, value in _leaves(path, wanted):
        document.add(tomlkit.key(list(leaf)), _item_of(value))


def _leaves(
    path: tuple[str, ...], body: Mapping[str, object]
) -> Iterator[tuple[tuple[str, ...], object]]:
    """Each dotted key below `path` and its value, in the order of `body`."""
    for key, value in body.items():
        if value is None:
            continue

        if _holds_a_value(value):
            yield from _leaves((*path, key), cast("Mapping[str, object]", value))
            continue

        yield (*path, key), value


def _item_of(value: object) -> Item:
    """One value, in the text that `tomlout.py` writes for it."""
    return cast("Item", tomlkit.parse(to_toml({_SLOT: value})).item(_SLOT))
