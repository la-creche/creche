"""The vector file format: one normalizer, one renderer, one way to record a run.

A vector is an input and what the Python code did with it. Everything a
vector file holds goes through `normalize`, so two runs on two machines write
the same bytes: no timestamp, no absolute path, sorted keys, ASCII only.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import json
import logging
import math
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, cast

from pydantic import BaseModel

#: The version of the file format. A reader refuses a file with another one.
FORMAT: Final = 1

#: What the Python code did with an input.
ACCEPTED: Final = "accepted"
REFUSED: Final = "refused"
RAISED: Final = "raised"

#: A JSON number a reader can hold exactly in 64 bits, signed or unsigned.
#: An integer outside it is written as a marker object instead.
INT_MIN: Final = -(2**63)
INT_MAX: Final = 2**64 - 1

#: The marker objects. Each one is an object with exactly this one key.
INT_MARKER: Final = "$int"
FLOAT_MARKER: Final = "$float"
UTF16_MARKER: Final = "$utf16"
BASE64_MARKER: Final = "$base64"
ENTRIES_MARKER: Final = "$entries"
JSON_MARKER: Final = "$json"
MARKERS: Final = frozenset(
    {INT_MARKER, FLOAT_MARKER, UTF16_MARKER, BASE64_MARKER, ENTRIES_MARKER, JSON_MARKER}
)

#: The deepest nesting one field of a vector keeps as plain JSON. A reader
#: in another language has a limit of its own: serde_json refuses level 129.
#: A field sits three levels inside its file, so a file nests 99 levels at
#: most. A field that nests deeper is written as its JSON text in a marker.
DEPTH_MAX: Final = 96

_SURROGATE_FIRST: Final = 0xD800
_SURROGATE_LAST: Final = 0xDFFF

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


@contextmanager
def quiet_logs() -> Generator[None]:
    """No log line from an entry point that logs a refusal, and the level put back."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def has_surrogate(text: str) -> bool:
    """Whether `text` holds a lone surrogate, which no UTF-8 file can hold."""
    return any(_SURROGATE_FIRST <= ord(char) <= _SURROGATE_LAST for char in text)


def _utf16_units(text: str) -> list[Json]:
    raw = text.encode("utf-16-be", "surrogatepass")

    return [int.from_bytes(raw[index : index + 2], "big") for index in range(0, len(raw), 2)]


def _float(value: float) -> Json:
    if math.isfinite(value):
        return value

    if math.isnan(value):
        return {FLOAT_MARKER: "NaN"}

    return {FLOAT_MARKER: "Infinity" if value > 0 else "-Infinity"}


def _mapping(value: Mapping[object, object]) -> Json:
    keys = list(value)
    plain = all(isinstance(key, str) and not has_surrogate(key) for key in keys)
    if not plain:
        return {ENTRIES_MARKER: [[normalize(key), normalize(value[key])] for key in keys]}

    out: dict[str, Json] = {str(key): normalize(value[key]) for key in keys}
    if len(out) == 1 and next(iter(out)) in MARKERS:
        raise ValueError(f"a value collides with the marker {next(iter(out))}")

    return out


def normalize(value: object) -> Json:
    """One Python value as JSON a strict reader accepts, the same on every run.

    An enum is its value. A dataclass and a pydantic model are objects of
    their fields. A tuple is an array. A set is a sorted array. A time is its
    ISO 8601 text. A float that
    is not finite, an integer past 64 bits, a string with a lone surrogate
    and bytes are marker objects (`vectors/README.md`). The nesting is as
    deep as the value's: `bounded` is what holds a field under a limit.
    """
    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, enum.Enum):
        return normalize(value.value)

    if isinstance(value, int):
        return value if INT_MIN <= value <= INT_MAX else {INT_MARKER: str(value)}

    if isinstance(value, float):
        return _float(value)

    if isinstance(value, str):
        return {UTF16_MARKER: _utf16_units(value)} if has_surrogate(value) else value

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, (bytes, bytearray)):
        return {BASE64_MARKER: base64.b64encode(bytes(value)).decode("ascii")}

    if isinstance(value, BaseModel):
        return normalize(value.model_dump(mode="python"))

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            one.name: normalize(cast("object", getattr(value, one.name)))
            for one in dataclasses.fields(value)
        }

    if isinstance(value, Mapping):
        return _mapping(cast("Mapping[object, object]", value))

    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in cast("list[object]", value)]

    if isinstance(value, (set, frozenset)):
        items = [normalize(item) for item in cast("set[object]", value)]
        return sorted(items, key=compact)

    raise TypeError(f"no JSON form for {type(value).__name__}")


def depth(value: Json) -> int:
    """How many arrays and objects nest at the deepest point of `value`."""
    deepest = 0
    stack: list[tuple[Json, int]] = [(value, 1)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, dict):
            children = list(item.values())
        elif isinstance(item, list):
            children = item
        else:
            continue

        deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in children)

    return deepest


def compact(value: Json) -> str:
    """One JSON value on one line: sorted keys, ASCII only, no NaN."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def bounded(value: Json) -> Json:
    """`value`, or its JSON text in a marker when it nests past `DEPTH_MAX`."""
    if depth(value) <= DEPTH_MAX:
        return value

    return {JSON_MARKER: compact(value)}


def _field(value: object) -> Json:
    """One field of a vector: normalized, and readable under a nesting limit."""
    return bounded(normalize(value))


def text_input(text: str) -> dict[str, Json]:
    """An input the Python code takes as text."""
    if has_surrogate(text):
        raise ValueError("an input text holds a lone surrogate; no reader can decode it")

    return {"text": text}


def repeat_input(parts: Sequence[tuple[str, int]]) -> dict[str, Json]:
    """A long text input, written short: each part is a text and a count.

    The input is every part's text, repeated its count of times, joined in
    order. It keeps a 400,000-deep nesting out of the file as bytes, and a
    reader of the file sees what the input is.
    """
    rows: list[Json] = [[text_input(text)["text"], count] for text, count in parts]

    return {"repeat": rows}


def expand(parts: Sequence[tuple[str, int]]) -> str:
    """The text a `repeat` input stands for."""
    return "".join(text * count for text, count in parts)


def bytes_input(raw: bytes) -> dict[str, Json]:
    """An input the Python code takes as bytes: text when it is UTF-8."""
    try:
        return {"text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(raw).decode("ascii")}


@dataclass(frozen=True)
class Vector:
    """One input and its outcome. `body` is every key but `id`."""

    id: str
    body: dict[str, Json]

    def as_json(self) -> dict[str, Json]:
        return {"id": self.id, **self.body}


def accepted(
    vector_id: str, given: dict[str, Json], value: object = None, **extra: object
) -> Vector:
    """The Python code took the input. `value` is what it parsed it into."""
    body: dict[str, Json] = {"input": given, "result": ACCEPTED}
    if value is not None:
        body["value"] = _field(value)

    body.update({key: _field(item) for key, item in extra.items()})

    return Vector(vector_id, body)


def refused(
    vector_id: str, given: dict[str, Json], refusal: object = None, **extra: object
) -> Vector:
    """The Python code refused the input, the way its contract says it does."""
    body: dict[str, Json] = {"input": given, "result": REFUSED}
    if refusal is not None:
        body["refusal"] = _field(refusal)

    body.update({key: _field(item) for key, item in extra.items()})

    return Vector(vector_id, body)


def raised(vector_id: str, given: dict[str, Json], exc: BaseException, **extra: object) -> Vector:
    """The Python code raised where its contract names no exception.

    Only the type is kept. A message of the interpreter differs between two
    Python versions, and a vector file must not.
    """
    body: dict[str, Json] = {"input": given, "result": RAISED, "exception": type(exc).__name__}
    body.update({key: _field(item) for key, item in extra.items()})

    return Vector(vector_id, body)


def run(
    vector_id: str,
    given: dict[str, Json],
    call: Callable[[], Vector],
    **extra: object,
) -> Vector:
    """`call()`, or a `raised` vector when it raises anything at all."""
    try:
        return call()
    except Exception as exc:
        return raised(vector_id, given, exc, **extra)


@dataclass(frozen=True)
class Surface:
    """One vector file: one entry point of the Python code and its vectors."""

    #: The name a Rust test asks for, e.g. `id.family_name.attendance`.
    name: str
    #: The file, relative to `vectors/data/`, e.g. `ids/family_name.attendance.json`.
    path: str
    #: The Python entry point the generator calls, e.g. `attendance.ids.is_family`.
    entry: str
    #: The design contract the surface belongs to, e.g. `contract 02 §2`.
    contract: str
    vectors: tuple[Vector, ...]
    #: What a reader must know to replay a vector. Sentences, in order.
    notes: tuple[str, ...] = ()
    #: Fixed facts every vector of the file shares, e.g. a fixture path.
    context: dict[str, Json] = field(default_factory=dict[str, Json])

    def tally(self) -> dict[str, int]:
        """How many vectors ended each way."""
        counts = {ACCEPTED: 0, REFUSED: 0, RAISED: 0}
        for vector in self.vectors:
            counts[str(vector.body["result"])] += 1

        return counts


def render(surface: Surface) -> str:
    """The bytes of one vector file. One vector per line, one final newline."""
    seen: set[str] = set()
    for vector in surface.vectors:
        if vector.id in seen:
            raise ValueError(f"{surface.name}: the id {vector.id} is used twice")

        if depth(vector.body["input"]) > DEPTH_MAX:
            raise ValueError(f"{surface.name}: the input of {vector.id} nests too deep")

        seen.add(vector.id)

    head: dict[str, Json] = {
        "format": FORMAT,
        "surface": surface.name,
        "entry": surface.entry,
        "contract": surface.contract,
        "notes": list(surface.notes),
        "context": surface.context,
    }
    lines = [f" {compact(key)}: {compact(head[key])}," for key in sorted(head)]
    rows = ",\n".join(f"  {compact(vector.as_json())}" for vector in surface.vectors)
    body = f"[\n{rows}\n ]" if rows else "[]"

    return "{\n" + "\n".join(lines) + f'\n "vectors": {body}\n}}\n'
