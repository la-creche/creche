"""Turning an edited family into the text of a TOML family file.

The noticeboard reads a family through `agent_family`, which owns the
reader. It writes a whole TOML text through this module. `tomlkeep.py` uses
the text of one value from here too, so one module holds the layout.

The layout is the layout of a converted registry file:

1. Each scalar has a line of its own, in the order of the mapping.
2. A nested mapping becomes dotted keys: `model.router = "..."`. The text
   has no `[table]` line. Such a line takes each later key into its table.
3. A list of scalars is on one line. A list that holds a table has one line
   for each item, and each item is an inline table.
4. An empty mapping is `{}`, and an empty list is `[]`.
5. A key whose value is `None` gets no line. TOML has no null, and an
   absent key reads as `None` in the model.
6. A text is a basic string, with an escape for each control character.
7. A date, a time and a date-time have their ISO text and no quotes.

The writer proves its own output in the same way as `yamlout.py`:
`app.py` reads the text again with `agent_family.parse_family` and writes it
only when the model that comes back equals the model that went in. The save
then gives the text to the validator of the whole registry.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, time
from typing import Final, cast

#: The indent of one item of a list that has one line for each item.
INDENT: Final = "    "

#: A key that needs no quotes.
_BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]+")

#: The escapes that TOML has a short form for.
_SHORT_ESCAPES: Final = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}

#: The Unicode category of a control character and that of one half of a
#: surrogate pair. Each one gets a `\\u` escape. TOML refuses the escape of a
#: half pair, so the reader refuses such a text and the save says so.
_ESCAPED_CATEGORIES: Final = frozenset(("Cc", "Cs"))

_EMPTY_TABLE: Final = "{}"

#: The types of a TOML date, time and date-time in a value of the reader.
_MOMENTS: Final = (date, time)


def to_toml(document: Mapping[str, object]) -> str:
    """One TOML text, in the layout above, ending in a newline.

    Raises `TypeError` for a value that TOML has no form for."""
    return "".join(f"{one}\n" for one in _lines(document, ()))


def _lines(body: Mapping[str, object], prefix: tuple[str, ...]) -> Iterator[str]:
    for key, value in body.items():
        if value is None:
            continue

        path = (*prefix, str(key))

        if _is_table(value) and _holds_a_value(cast("Mapping[str, object]", value)):
            yield from _lines(cast("Mapping[str, object]", value), path)
            continue

        if _holds_a_table(value):
            yield f"{_dotted(path)} = ["
            yield from (f"{INDENT}{_inline(one)}," for one in cast("Sequence[object]", value))
            yield "]"
            continue

        yield f"{_dotted(path)} = {_inline(value)}"


def _is_table(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_list(value: object) -> bool:
    return isinstance(value, list | tuple)


def _holds_a_value(body: Mapping[str, object]) -> bool:
    """True when one key of `body` has a value that gets a line."""
    return any(one is not None for one in body.values())


def _holds_a_table(value: object) -> bool:
    """True for a list with a table in it."""
    return _is_list(value) and any(_is_table(one) for one in cast("Sequence[object]", value))


def _inline(value: object) -> str:
    """One value on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        return _number(value)

    if isinstance(value, str):
        return _basic(value)

    if _is_table(value):
        pairs = list(_inline_pairs(cast("Mapping[str, object]", value), ()))

        return "{ " + ", ".join(pairs) + " }" if pairs else _EMPTY_TABLE

    if _is_list(value):
        return "[" + ", ".join(_inline(one) for one in cast("Sequence[object]", value)) + "]"

    if isinstance(value, _MOMENTS):
        # The reader gives a TOML date, time or date-time as a value of
        # `datetime`. Its ISO text is the same value in TOML, so the model
        # reads a moment and not a text.
        return value.isoformat()

    raise TypeError(f"TOML has no form for a value of type {type(value).__name__}")


def _inline_pairs(body: Mapping[str, object], prefix: tuple[str, ...]) -> Iterator[str]:
    """Each `key = value` of an inline table, with dotted keys below it."""
    for key, value in body.items():
        if value is None:
            continue

        path = (*prefix, str(key))

        if _is_table(value) and _holds_a_value(cast("Mapping[str, object]", value)):
            yield from _inline_pairs(cast("Mapping[str, object]", value), path)
            continue

        yield f"{_dotted(path)} = {_inline(value)}"


def _number(value: float) -> str:
    if math.isnan(value):
        return "nan"

    if math.isinf(value):
        return "inf" if value > 0 else "-inf"

    return repr(value)


def _basic(text: str) -> str:
    """A text as a TOML basic string."""
    parts = ['"']

    for sign in text:
        short = _SHORT_ESCAPES.get(sign)

        if short is not None:
            parts.append(short)
        elif unicodedata.category(sign) in _ESCAPED_CATEGORIES:
            parts.append(f"\\u{ord(sign):04X}")
        else:
            parts.append(sign)

    parts.append('"')

    return "".join(parts)


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(one if _BARE_KEY.fullmatch(one) else _basic(one) for one in path)
