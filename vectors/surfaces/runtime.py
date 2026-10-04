"""The helper code that each service holds a copy of (the group `runtime`).

The surfaces, by what each one pins:

- `runtime.untrusted.<copy>.<helper>`: the JSON text of one value to what a
  lenient field reader returns for it.
- `runtime.parse_object.noticeboard`: the bytes of a file to the JSON object
  that the noticeboard reads from them, or to a refusal.
- `runtime.token.<reader>`: the bytes and the mode of one token file to
  accepted or refused.
- `runtime.bearer.<copy>`: the bytes of one `Authorization` header to
  accepted or refused, against the token that the service holds.
- `runtime.edge.<service>`: a request that no route of the service answers
  to the answer of the web framework.

Each copy of one helper gets the same inputs. A difference between two
copies is then a difference between two vector files on one vector id.

A token file is in a temporary directory, and no path of that directory
goes into a vector. An input on which an entry point raises is not in a
list (`vectors/AGENTS.md`, rule 5).
"""

from __future__ import annotations

import asyncio
import enum
import json
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast
from urllib.parse import urlsplit

from agent_door_owui import config as owui_config
from agent_door_owui import untrusted as owui_untrusted
from agent_door_owui.app import create_app as create_owui_app
from agent_door_owui.attendance import AttendanceClient as OwuiAttendance
from agent_door_trigger import untrusted as trigger_untrusted
from agent_door_trigger.attendance import AcceptedTurn, TurnRequest
from agent_door_trigger.config import AttendanceTarget, ServeConfig
from agent_door_trigger.errors import AttendanceError
from agent_door_trigger.routes import Route
from agent_door_trigger.tokens import read_webhook_token
from agent_door_trigger.webhooks import create_app as create_trigger_app
from agent_door_tui import untrusted as tui_untrusted
from attendance.api import build_app as build_attendance_app
from attendance.auth import Principal, TokenBook, TokenError
from attendance.paths import token_file
from attendance.service import SessionService
from caregiver.switch import SwitchError, read_token
from chaperone.app import PepConfig
from chaperone.app import create_app as create_chaperone_app
from chaperone.delegate import DoorTokenError, read_door_token
from fastapi import FastAPI
from noticeboard.app import build_app as build_noticeboard_app
from noticeboard.config import Config as NoticeboardConfig
from noticeboard.sessions import SessionReader, Transport
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message

from attendance import atomic as attendance_atomic
from noticeboard import jsonfiles
from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    expand,
    normalize,
    quiet_logs,
    raised,
    refused,
    repeat_input,
    text_input,
)
from vectors.surfaces.session import StandIn

GROUP: Final = "runtime"

#: No design contract holds the lenient readers. Two invariants of the
#: specification state why each service has them.
UNTRUSTED_CONTRACT: Final = "spec.md invariants 12 and 14"

# --- the lenient field readers ------------------------------------------------

#: The key that holds the value, for a helper that reads one field.
FIELD: Final = "field"

#: The two caps that a helper with a cap gets. The first is under the length
#: of each long list below, and the second is over it.
LIMITS: Final = (2, 50)

#: The count of characters that the noticeboard keeps of one text.
CUT: Final = jsonfiles.MAX_TEXT_CHARS

#: The most digits that `int` reads from a text. The JSON reader of Python
#: refuses an integer with one digit more.
INT_DIGITS_MAX: Final = 4300


@dataclass(frozen=True)
class Sample:
    """One JSON text: an id and the text, or the parts of a long text."""

    id: str
    text: str = ""
    #: A long text, written as repeated parts in place of `text`.
    parts: tuple[tuple[str, int], ...] = ()

    def inside(self, before: str, after: str) -> Sample:
        """This text between two other texts."""
        if not self.parts:
            return Sample(self.id, f"{before}{self.text}{after}")

        return Sample(self.id, parts=((before, 1), *self.parts, (after, 1)))

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else text_input(self.text)

    def source(self) -> str:
        return expand(self.parts) if self.parts else self.text


def _string(sample_id: str, char: str, count: int) -> Sample:
    """A JSON string of one character, `count` times."""
    return Sample(sample_id, parts=(('"', 1), (char, count), ('"', 1)))


#: One JSON value for each kind that a reader meets, and each edge of a kind.
SAMPLES: Final[tuple[Sample, ...]] = (
    # --- a string ---
    Sample("string", '"family"'),
    Sample("string-empty", '""'),
    Sample("string-in-spaces", '"  a  "'),
    Sample("string-of-digits", '"7"'),
    Sample("string-escapes", r'"a\"b\\c\nd\u00e9"'),
    _string("string-500", "a", CUT),
    _string("string-501", "a", CUT + 1),
    _string("string-600", "a", 600),
    _string("string-600-outside-the-bmp", "\U0001f600", 600),
    Sample("string-lone-surrogate", r'"\ud800"'),
    # --- an integer ---
    Sample("integer", "7"),
    Sample("integer-zero", "0"),
    Sample("integer-negative", "-3"),
    Sample("integer-i64-max", str(2**63 - 1)),
    Sample("integer-i64-max-plus-one", str(2**63)),
    Sample("integer-i64-min", str(-(2**63))),
    Sample("integer-i64-min-minus-one", str(-(2**63) - 1)),
    Sample("integer-u64-max", str(2**64 - 1)),
    Sample("integer-u64-max-plus-one", str(2**64)),
    Sample("integer-30-digits", "123456789012345678901234567890"),
    Sample("integer-400-digits", parts=(("9", 400),)),
    # --- a bool, a float, null ---
    Sample("bool-true", "true"),
    Sample("bool-false", "false"),
    Sample("float", "1.5"),
    Sample("float-whole", "2.0"),
    Sample("float-negative-zero", "-0.0"),
    Sample("float-exponent", "1e3"),
    Sample("float-too-large", "1e400"),
    Sample("float-nan", "NaN"),
    Sample("float-infinity", "Infinity"),
    Sample("float-negative-infinity", "-Infinity"),
    Sample("null", "null"),
    # --- an object ---
    Sample("object", '{"a":1,"b":"two"}'),
    Sample("object-empty", "{}"),
    Sample("object-nested", '{"a":{"b":[1,2]},"c":null}'),
    # --- a list ---
    Sample("list", '["a","b"]'),
    Sample("list-empty", "[]"),
    Sample("list-mixed", '["a",1,true,null,{"k":"v"},["x"],2.5,"b",{"k2":"v2"}]'),
    Sample("list-of-objects", '[{"a":1},{"b":2},{"c":3},{"d":4}]'),
    Sample("list-of-strings", '["a","b","c","d"]'),
    Sample("list-long-string", parts=(('["', 1), ("a", 600), ('","b"]', 1))),
)

#: Two objects that only a helper for one field gets: no field, and the
#: field two times.
FIELD_ONLY: Final[tuple[Sample, ...]] = (
    Sample("absent", "{}"),
    Sample("key-two-times", f'{{"{FIELD}":"first","{FIELD}":"last"}}'),
)


class Shape(enum.Enum):
    """What a helper takes."""

    #: One JSON value.
    VALUE = "value"
    #: A JSON object and the key of one field.
    FIELD = "field"
    #: A JSON object, the key of one field and a cap on the count of members.
    CAPPED = "capped"


@dataclass(frozen=True)
class Helper:
    """One public helper of one copy."""

    copy: str
    name: str
    #: The module that holds the helper.
    module: str
    shape: Shape
    #: The helper, called with the parsed input and with a cap.
    call: Callable[[object, int], object]


def _of_value(function: Callable[[object], object]) -> Callable[[object, int], object]:
    def call(parsed: object, limit: int) -> object:
        del limit  # a helper for one value has no cap
        return function(parsed)

    return call


def _of_field(
    function: Callable[[dict[str, object], str], object],
) -> Callable[[object, int], object]:
    def call(parsed: object, limit: int) -> object:
        del limit  # the generator gives such a helper no cap
        return function(cast("dict[str, object]", parsed), FIELD)

    return call


def _of_capped(
    function: Callable[[dict[str, object], str, int], object],
) -> Callable[[object, int], object]:
    def call(parsed: object, limit: int) -> object:
        return function(cast("dict[str, object]", parsed), FIELD, limit)

    return call


def _value(copy: str, module: str, function: Callable[[object], object], name: str) -> Helper:
    return Helper(copy, name, module, Shape.VALUE, _of_value(function))


def _field(
    copy: str, module: str, function: Callable[[dict[str, object], str], object], name: str
) -> Helper:
    return Helper(copy, name, module, Shape.FIELD, _of_field(function))


def _capped(
    copy: str, module: str, function: Callable[[dict[str, object], str, int], object], name: str
) -> Helper:
    return Helper(copy, name, module, Shape.CAPPED, _of_capped(function))


_OWUI: Final = "agent_door_owui.untrusted"
_TRIGGER: Final = "agent_door_trigger.untrusted"
_TUI: Final = "agent_door_tui.untrusted"
_BOARD: Final = "noticeboard.jsonfiles"
_ATTENDANCE: Final = "attendance.atomic"

#: Each public helper of each copy, in the order of the index.
HELPERS: Final[tuple[Helper, ...]] = (
    _value("door_owui", _OWUI, owui_untrusted.is_object, "is_object"),
    _value("door_owui", _OWUI, owui_untrusted.is_list, "is_list"),
    _value("door_owui", _OWUI, owui_untrusted.as_object, "as_object"),
    _value("door_owui", _OWUI, owui_untrusted.as_text, "as_text"),
    _field("door_owui", _OWUI, owui_untrusted.field_text, "field_text"),
    _value("door_trigger", _TRIGGER, trigger_untrusted.is_object, "is_object"),
    _value("door_trigger", _TRIGGER, trigger_untrusted.is_list, "is_list"),
    _value("door_trigger", _TRIGGER, trigger_untrusted.as_object, "as_object"),
    _value("door_trigger", _TRIGGER, trigger_untrusted.as_text, "as_text"),
    _field("door_trigger", _TRIGGER, trigger_untrusted.field_text, "field_text"),
    _value("door_tui", _TUI, tui_untrusted.is_object, "is_object"),
    _value("door_tui", _TUI, tui_untrusted.is_list, "is_list"),
    _value("door_tui", _TUI, tui_untrusted.as_object, "as_object"),
    _value("door_tui", _TUI, tui_untrusted.as_list, "as_list"),
    _value("door_tui", _TUI, tui_untrusted.as_text, "as_text"),
    _field("door_tui", _TUI, tui_untrusted.field_text, "field_text"),
    _field("door_tui", _TUI, tui_untrusted.field_int, "field_int"),
    _field("noticeboard", _BOARD, jsonfiles.text, "text"),
    _field("noticeboard", _BOARD, jsonfiles.whole, "whole"),
    _field("noticeboard", _BOARD, jsonfiles.integer, "integer"),
    _field("noticeboard", _BOARD, jsonfiles.number, "number"),
    _field("noticeboard", _BOARD, jsonfiles.flag, "flag"),
    _field("noticeboard", _BOARD, jsonfiles.block, "block"),
    _field("noticeboard", _BOARD, jsonfiles.child, "child"),
    _capped("noticeboard", _BOARD, jsonfiles.children, "children"),
    _capped("noticeboard", _BOARD, jsonfiles.strings, "strings"),
    _value("attendance", _ATTENDANCE, attendance_atomic.as_object, "as_object"),
    _value("attendance", _ATTENDANCE, attendance_atomic.as_array, "as_array"),
)


def _returned(
    vector_id: str, given: dict[str, Json], value: object, params: dict[str, Json] | None
) -> Vector:
    """A vector whose value is what a helper returns. None is a value here.

    `accepted` writes no `value` for None. A helper that returns None gives
    an answer, so the vector holds an explicit null.
    """
    extra: dict[str, object] = {} if params is None else {"params": params}
    vector = accepted(vector_id, given, value, **extra)
    if value is not None:
        return vector

    return Vector(vector_id, {**vector.body, "value": None})


def _helper_vector(helper: Helper, sample: Sample, limit: int | None) -> Vector:
    vector_id = sample.id if limit is None else f"{sample.id}.limit-{limit}"
    params: dict[str, Json] | None = None if limit is None else {"limit": limit}
    given = sample.given()
    parsed: object = json.loads(sample.source())
    outcome = attempt(lambda: helper.call(parsed, 0 if limit is None else limit))
    if isinstance(outcome, Raised):
        extra: dict[str, object] = {} if params is None else {"params": params}
        return raised(vector_id, given, outcome.exc, **extra)

    return _returned(vector_id, given, outcome, params)


def _helper_vectors(helper: Helper) -> tuple[Vector, ...]:
    if helper.shape is Shape.VALUE:
        return tuple(_helper_vector(helper, sample, None) for sample in SAMPLES)

    objects = (*(one.inside(f'{{"{FIELD}":', "}") for one in SAMPLES), *FIELD_ONLY)
    if helper.shape is Shape.FIELD:
        return tuple(_helper_vector(helper, sample, None) for sample in objects)

    return tuple(_helper_vector(helper, sample, limit) for sample in objects for limit in LIMITS)


_NOTE_PARSED: Final = (
    "The generator reads the input with json.loads of Python and gives the helper the result. "
    f"That reader takes NaN, Infinity, an integer of {INT_DIGITS_MAX} digits or less and an "
    "escape of one half of a surrogate pair."
)
_NOTE_RETURNS: Final = (
    "Each vector is accepted: the helper returns a value for each input. value is what the "
    "helper returns. A value of null means that the helper returns None."
)
_SHAPE_NOTES: Final[dict[Shape, tuple[str, ...]]] = {
    Shape.VALUE: ("The input is the JSON text of one value. The helper takes that value.",),
    Shape.FIELD: (
        f"The input is the JSON text of one object. The helper takes the object and the key "
        f"{FIELD}. The vector absent has no such key. The vector key-two-times has the key two "
        "times, and json.loads keeps the last value.",
    ),
    Shape.CAPPED: (
        f"The input is the JSON text of one object. The helper takes the object, the key "
        f"{FIELD} and the cap of params.limit. Each input has one vector for each cap.",
        "The helper takes the first members up to the cap. Then it drops each member of a "
        "wrong type. A cap thus counts the members that the helper drops.",
    ),
}
_COPY_NOTES: Final[dict[str, tuple[str, ...]]] = {
    "noticeboard": (
        f"A text that the helper returns has {CUT} characters or less. A character is one "
        "code point.",
        "The generator gives the helper no argument with a default: no limit of a text and "
        "no fallback of an integer.",
    ),
}


def _untrusted_surface(helper: Helper) -> Surface:
    tail = f"untrusted.{helper.copy}.{helper.name}"

    return Surface(
        name=f"{GROUP}.{tail}",
        path=f"{GROUP}/{tail}.json",
        entry=f"{helper.module}.{helper.name}",
        contract=UNTRUSTED_CONTRACT,
        notes=(
            *_SHAPE_NOTES[helper.shape],
            _NOTE_PARSED,
            _NOTE_RETURNS,
            *_COPY_NOTES.get(helper.copy, ()),
        ),
        vectors=_helper_vectors(helper),
    )


# --- the object of a file -----------------------------------------------------

#: The name that the noticeboard puts into a problem text. No vector holds
#: that text.
FILE_NAME: Final = "document.json"

#: More levels than the JSON reader of each supported Python version reads.
VERY_DEEP: Final = 400_000

#: More levels than `serde_json` reads, and fewer than each supported Python
#: version reads.
DEEP: Final = 200

#: One more digit than `int` reads from a text.
TOO_MANY_DIGITS: Final = INT_DIGITS_MAX + 1

_SMALL: Final = '{"a": 1}'


@dataclass(frozen=True)
class Document:
    """The bytes of one file: an id and the bytes, or the parts of a long text."""

    id: str
    raw: bytes = b""
    #: A long text, written as repeated parts in place of `raw`.
    parts: tuple[tuple[str, int], ...] = ()

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else bytes_input(self.raw)

    def data(self) -> bytes:
        return expand(self.parts).encode("utf-8") if self.parts else self.raw


def _doc(doc_id: str, text: str) -> Document:
    return Document(doc_id, text.encode("utf-8"))


def _nested(doc_id: str, levels: int) -> Document:
    return Document(doc_id, parts=(('{"a":', levels), ("1", 1), ("}", levels)))


DOCUMENTS: Final[tuple[Document, ...]] = (
    # --- an object ---
    _doc("object", _SMALL),
    _doc("object-empty", "{}"),
    _doc("object-in-spaces", f" \n{_SMALL}\t\r\n"),
    _doc("object-nested", '{"a": {"b": [1, {"c": null}]}}'),
    _doc("key-two-times", '{"a": 1, "a": 2}'),
    _doc("key-empty", '{"": 1}'),
    # --- a value that is no object ---
    _doc("list", "[1]"),
    _doc("number", "7"),
    _doc("string", '"a"'),
    _doc("true", "true"),
    _doc("null", "null"),
    # --- a text that is not JSON ---
    _doc("cut-short", '{"a": '),
    _doc("word", "nope"),
    _doc("two-documents", '{"a": 1}{"b": 2}'),
    _doc("comma-last", '{"a": 1,}'),
    _doc("single-quotes", "{'a': 1}"),
    _doc("empty", ""),
    _doc("spaces-only", "   "),
    # --- the encoding ---
    Document("not-utf8", b'{"a": "\xff"}'),
    _doc("byte-order-mark", f"\ufeff{_SMALL}"),
    Document("utf16-with-mark", b"\xff\xfe" + _SMALL.encode("utf-16-le")),
    Document("utf16-little-endian", _SMALL.encode("utf-16-le")),
    Document("utf16-big-endian", _SMALL.encode("utf-16-be")),
    Document("utf32-with-mark", b"\xff\xfe\x00\x00" + _SMALL.encode("utf-32-le")),
    Document("utf32-little-endian", _SMALL.encode("utf-32-le")),
    Document("utf32-big-endian", _SMALL.encode("utf-32-be")),
    # --- what a strict reader refuses ---
    _doc("nan", '{"a": NaN}'),
    _doc("infinity", '{"a": [Infinity, -Infinity]}'),
    _doc("lone-surrogate", r'{"a": "\ud800"}'),
    _doc("integer-30-digits", '{"a": 123456789012345678901234567890}'),
    Document("integer-4301-digits", parts=(('{"a": ', 1), ("9", TOO_MANY_DIGITS), ("}", 1))),
    _nested("nested-200", DEEP),
    _nested("nested-400000", VERY_DEEP),
)


def _parsed(raw: bytes) -> tuple[bool, object]:
    """Whether the noticeboard reads an object from `raw`, and the object."""
    body, problem = jsonfiles.parse_object(raw, FILE_NAME)

    return problem is None and body is not None, body


def _document_vector(document: Document) -> Vector:
    given = document.given()
    raw = document.data()
    outcome = attempt(lambda: _parsed(raw))
    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc)

    took, body = outcome
    if took:
        return accepted(document.id, given, body)

    return refused(document.id, given)


def _parse_surface() -> Surface:
    tail = "parse_object.noticeboard"

    return Surface(
        name=f"{GROUP}.{tail}",
        path=f"{GROUP}/{tail}.json",
        entry="noticeboard.jsonfiles.parse_object",
        contract=f"contract 05 §2, {UNTRUSTED_CONTRACT}",
        notes=(
            "The input is the bytes of one file. The entry point also takes the name of the "
            f"file. The generator gives it {FILE_NAME}.",
            "An accepted vector is a file that holds one JSON object. value is that object.",
            "A refused vector is a file that the entry point reads no object from. The entry "
            "point returns a problem text for it. No vector holds that text.",
            "The entry point gives the bytes to json.loads of Python. That reader takes "
            "UTF-8 with a byte order mark, UTF-16 and UTF-32. It takes NaN, Infinity and an "
            "escape of one half of a surrogate pair. It keeps the last value of a key that "
            "an object holds two times.",
        ),
        vectors=tuple(_document_vector(document) for document in DOCUMENTS),
    )


# --- the token files ----------------------------------------------------------

#: The mode of a token file when a vector names none.
MODE_OWNER: Final = 0o600

#: A token of 32 bytes, the least count that most readers take. It is the
#: token of no deployment.
CORE: Final = b"0123456789abcdefghijklmnopqrstuv"

#: One byte less than the least count.
SHORT: Final = CORE[:-1]

#: The token of each file that a reader needs and that is not the input.
FILLER: Final = b"vectors-filler-token-0123456789abcdefghijk"

#: Each character that some reader removes from the two ends of a token, and
#: three that no reader removes: U+0000, U+200B and U+FEFF.
EDGE_CHARS: Final = (
    " ",
    "\t",
    "\n",
    "\r",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x1f",
    "\x00",
    "\x85",
    "\xa0",
    "\u2003",
    "\u2028",
    "\u200b",
    "\ufeff",
)

#: Each mode that a file of 32 bytes gets, after the mode of the owner alone.
MODES: Final = (0o640, 0o644, 0o660, 0o400, 0o440, 0o700, 0o604, 0o620, 0o610)

#: A mode that each reader with a mode rule refuses.
MODE_WIDE: Final = 0o644


@dataclass(frozen=True)
class TokenFile:
    """One token file: an id, its bytes and its mode."""

    id: str
    raw: bytes
    mode: int = MODE_OWNER


def _edged(char: str) -> TokenFile:
    """31 bytes with one character at each end.

    A reader that needs 32 bytes takes the file only when it keeps the
    character.
    """
    edge = char.encode("utf-8")

    return TokenFile(f"31-bytes-in-u{ord(char):04x}", edge + SHORT + edge)


#: The token files that are UTF-8 text.
TEXT_FILES: Final[tuple[TokenFile, ...]] = (
    # --- the count of bytes ---
    TokenFile("32-bytes", CORE),
    TokenFile("31-bytes", SHORT),
    TokenFile("33-bytes", CORE + b"w"),
    TokenFile("1-byte", b"a"),
    TokenFile("32-bytes-in-16-characters", ("\u00e9" * 16).encode("utf-8")),
    TokenFile("31-bytes-in-16-characters", ("\u00e9" * 15 + "a").encode("utf-8")),
    # --- a file with no token ---
    TokenFile("empty", b""),
    TokenFile("spaces-only", b"   "),
    TokenFile("newline-only", b"\n"),
    TokenFile("ascii-whitespace-only", b" \t\n\r\x0b\x0c"),
    TokenFile("u00a0-only", "\u00a0".encode()),
    # --- the two ends ---
    TokenFile("32-bytes-final-newline", CORE + b"\n"),
    TokenFile("32-bytes-final-crlf", CORE + b"\r\n"),
    TokenFile("31-bytes-final-newline", SHORT + b"\n"),
    TokenFile("32-bytes-in-spaces", b"  " + CORE + b"  "),
    TokenFile("31-bytes-in-ascii-whitespace", b" \t\n\r\x0b\x0c" + SHORT + b"\x0c\x0b\r\n\t "),
    *(_edged(char) for char in EDGE_CHARS),
    # --- inside the token ---
    TokenFile("32-bytes-inner-space", CORE[:16] + b" " + CORE[17:]),
    TokenFile("32-bytes-inner-cr", CORE[:16] + b"\r" + CORE[17:]),
    TokenFile("32-bytes-inner-crlf", CORE[:15] + b"\r\n" + CORE[17:]),
    # --- the mode ---
    *(TokenFile(f"mode-{mode:04o}", CORE, mode) for mode in MODES),
    TokenFile("31-bytes-mode-0644", SHORT, MODE_WIDE),
    TokenFile("empty-mode-0644", b"", MODE_WIDE),
)

#: The token files that are not UTF-8 text.
OTHER_FILES: Final[tuple[TokenFile, ...]] = (
    TokenFile("not-utf8", b"\xff" + CORE),
    TokenFile("not-utf8-short", b"\xffshort"),
    TokenFile("31-bytes-in-byte-a0", b"\xa0" + SHORT + b"\xa0"),
)


class Returns(enum.Enum):
    """What the entry point of a token reader returns."""

    #: Nothing. A call that returns is a file that the reader took.
    NOTHING = "nothing"
    #: The token as text.
    TOKEN = "token"
    #: The token as text, or None for a file that the reader does not trust.
    TOKEN_OR_NONE = "token_or_none"


@dataclass(frozen=True)
class TokenReader:
    """One reader of a token file."""

    name: str
    entry: str
    contract: str
    #: Writes the file under a directory. Returns the call of the entry point.
    prepare: Callable[[Path, TokenFile], Callable[[], object]]
    returns: Returns
    #: The files that the reader gets.
    files: tuple[TokenFile, ...]
    #: The exception of a refusal, when the reader has one.
    refusal: type[Exception] | None = None
    #: A part of each refusal text, and the kind that a vector records.
    kinds: tuple[tuple[str, str], ...] = ()
    #: A text that each refusal holds. It names the file of the input.
    about: str = ""
    notes: tuple[str, ...] = ()


def _write(path: Path, raw: bytes, mode: int) -> None:
    """A file with these bytes and this mode. The umask does not set the mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _book(principal: Principal) -> Callable[[Path, TokenFile], Callable[[], object]]:
    """The token file of one principal, among the files of each other one."""

    def prepare(directory: Path, file: TokenFile) -> Callable[[], object]:
        for other in Principal:
            _write(token_file(directory, other.value), FILLER, MODE_OWNER)

        _write(token_file(directory, principal.value), file.raw, file.mode)

        return TokenBook(directory).load

    return prepare


def _door_key(directory: Path, file: TokenFile) -> Callable[[], object]:
    key = directory / "door.key"
    token = directory / "attendance.token"
    _write(key, file.raw, file.mode)
    _write(token, FILLER, MODE_OWNER)
    variables = {owui_config.ENV_KEY_FILE: str(key), owui_config.ENV_TOKEN_FILE: str(token)}

    return lambda: owui_config.from_env(variables).door_key


def _one_file(
    read: Callable[[Path], object],
) -> Callable[[Path, TokenFile], Callable[[], object]]:
    """A reader that takes the path of the file and nothing else."""

    def prepare(directory: Path, file: TokenFile) -> Callable[[], object]:
        path = directory / "token"
        _write(path, file.raw, file.mode)

        return lambda: read(path)

    return prepare


_BOOK_KINDS: Final = (
    ("is unreadable", "unreadable"),
    ("is empty", "empty"),
    ("is under", "short"),
    ("is not mode", "mode"),
)
_NOTE_BOOK: Final = (
    "The entry point reads the token file of each principal. The generator gives each "
    f"other principal a file of {len(FILLER)} bytes with mode 0600. A refusal thus names "
    "the input."
)
_NOTE_NO_VALUE: Final = (
    "The entry point returns nothing, so an accepted vector has no value. The reader keeps "
    "the bytes of the file with no decode, less the six bytes of ASCII whitespace at the two "
    "ends: space, tab, LF, CR, VT and FF."
)
_NOTE_TEXT: Final = (
    "The reader reads the file as UTF-8 text, in the text mode of Python. That mode gives "
    "LF for each CR and for each CR LF. Then the reader removes the whitespace of Python "
    "str.strip from the two ends. value.token is the text that the reader returns."
)

TOKEN_READERS: Final[tuple[TokenReader, ...]] = (
    TokenReader(
        name="attendance",
        entry="attendance.auth.TokenBook.load, the token file of door-owui",
        contract="contract 02 §3 rules 5 and 7",
        prepare=_book(Principal.DOOR_OWUI),
        returns=Returns.NOTHING,
        files=(*TEXT_FILES, *OTHER_FILES),
        refusal=TokenError,
        kinds=_BOOK_KINDS,
        about=f"token file for {Principal.DOOR_OWUI.value} ",
        notes=(
            _NOTE_BOOK,
            _NOTE_NO_VALUE,
            "refusal is the kind of the refusal: unreadable, empty, short or mode. The "
            "reader checks the count of bytes before the mode. mode means that the group or "
            "another account has a permission bit.",
        ),
    ),
    TokenReader(
        name="attendance_pep_read",
        entry="attendance.auth.TokenBook.load, the token file of door-delegate",
        contract="contract 02 §3 rules 5 and 7",
        prepare=_book(Principal.DOOR_DELEGATE),
        returns=Returns.NOTHING,
        files=(*TEXT_FILES, *OTHER_FILES),
        refusal=TokenError,
        kinds=_BOOK_KINDS,
        about=f"token file for {Principal.DOOR_DELEGATE.value} ",
        notes=(
            _NOTE_BOOK,
            _NOTE_NO_VALUE,
            "refusal is the kind of the refusal: unreadable, empty, short or mode. The "
            "reader checks the count of bytes before the mode. The chaperone reads this "
            "file, so the group can have the read bit. mode means that the group or another "
            "account has another permission bit.",
        ),
    ),
    TokenReader(
        name="door",
        entry=f"agent_door_owui.config.from_env, the file of {owui_config.ENV_KEY_FILE}",
        contract="contract 02 §3 rule 7",
        prepare=_door_key,
        returns=Returns.TOKEN,
        files=(*TEXT_FILES, *OTHER_FILES),
        refusal=owui_config.ConfigError,
        kinds=(
            ("cannot read", "unreadable"),
            ("is not UTF-8 text", "not_utf8"),
            ("is shorter than", "short"),
        ),
        about=f"{owui_config.ENV_KEY_FILE}:",
        notes=(
            f"The generator gives the entry point two variables: {owui_config.ENV_KEY_FILE} "
            f"names the input, and {owui_config.ENV_TOKEN_FILE} names a file of "
            f"{len(FILLER)} bytes.",
            _NOTE_TEXT,
            "refusal is the kind of the refusal: unreadable, not_utf8 or short. The reader "
            "counts the UTF-8 bytes of the text. An empty file is short. The reader checks "
            "no mode.",
        ),
    ),
    TokenReader(
        name="delegate",
        entry="chaperone.delegate.read_door_token",
        contract="contract 04 §7.3, contract 02 §3 rule 7",
        prepare=_one_file(read_door_token),
        returns=Returns.TOKEN,
        files=TEXT_FILES,
        refusal=DoorTokenError,
        kinds=(("cannot read", "unreadable"), ("holds fewer than", "short")),
        notes=(
            _NOTE_TEXT,
            "refusal is the kind of the refusal: unreadable or short. The reader counts "
            "the UTF-8 bytes of the text. An empty file is short. The reader checks no mode.",
        ),
    ),
    TokenReader(
        name="webhook",
        entry="agent_door_trigger.tokens.read_webhook_token",
        contract="contract 05 §6.4",
        prepare=_one_file(read_webhook_token),
        returns=Returns.TOKEN_OR_NONE,
        files=(*TEXT_FILES, *OTHER_FILES),
        notes=(
            "The reader checks the mode first: the group and each other account have no "
            "permission bit. Then it removes the six bytes of ASCII whitespace from the two "
            "ends, counts the bytes and decodes them as UTF-8. value.token is the text that "
            "the reader returns.",
            "A refused vector is a file for which the reader returns None. The reader gives "
            "no reason, so a refused vector has no refusal.",
        ),
    ),
    TokenReader(
        name="caregiver",
        entry="caregiver.switch.read_token",
        contract="contract 02 §3 rule 5",
        prepare=_one_file(read_token),
        returns=Returns.TOKEN,
        files=(*TEXT_FILES, *OTHER_FILES),
        refusal=SwitchError,
        kinds=(("cannot read", "unreadable"), ("is empty", "empty")),
        notes=(
            _NOTE_TEXT,
            "refusal is the kind of the refusal: unreadable or empty. unreadable is also "
            "the kind of a file that is not UTF-8. The reader has no least count of bytes "
            "and checks no mode.",
        ),
    ),
)


def _kind(reader: TokenReader, error: Exception, home: Path) -> str:
    """The kind of one refusal text. No vector holds the text: it names a path.

    The path is under `home`, a temporary directory. The search reads the
    text without that directory, so its name cannot hold a mark.
    """
    text = str(error).replace(str(home), "")
    if reader.about not in text:
        raise ValueError(f"{reader.name}: a refusal that does not name the input")

    kind = next((name for mark, name in reader.kinds if mark in text), None)
    if kind is None:
        raise ValueError(f"{reader.name}: a refusal text that no table holds")

    return kind


def _token_vector(reader: TokenReader, file: TokenFile, scratch: Path) -> Vector:
    given = bytes_input(file.raw)
    params = {"mode": f"{file.mode:04o}"}
    home = scratch / reader.name / file.id
    call = reader.prepare(home, file)
    with quiet_logs():
        outcome = attempt(call)

    if isinstance(outcome, Raised):
        if reader.refusal is not None and isinstance(outcome.exc, reader.refusal):
            return refused(file.id, given, _kind(reader, outcome.exc, home), params=params)

        return raised(file.id, given, outcome.exc, params=params)

    if reader.returns is Returns.NOTHING:
        return accepted(file.id, given, params=params)

    if isinstance(outcome, str):
        return accepted(file.id, given, {"token": outcome}, params=params)

    if outcome is None and reader.returns is Returns.TOKEN_OR_NONE:
        return refused(file.id, given, params=params)

    raise ValueError(f"{reader.name}: the reader returned {type(outcome).__name__}")


_NOTE_FILE: Final = (
    "The input is the bytes of one token file. The generator writes them to a file with the "
    "mode that params.mode gives, in octal. The account of the generator owns the file and "
    "can read each mode of a vector."
)
_NOTE_ENDS: Final = (
    "A vector 31-bytes-in-u<code point> is 31 ASCII bytes with that character at each of "
    "the two ends, as UTF-8. A reader that needs 32 bytes takes the file only when it keeps "
    "the character."
)


def _token_surface(reader: TokenReader, scratch: Path) -> Surface:
    tail = f"token.{reader.name}"

    return Surface(
        name=f"{GROUP}.{tail}",
        path=f"{GROUP}/{tail}.json",
        entry=reader.entry,
        contract=reader.contract,
        notes=(_NOTE_FILE, _NOTE_ENDS, *reader.notes),
        vectors=tuple(_token_vector(reader, file, scratch) for file in reader.files),
    )


# --- the bearer of a request --------------------------------------------------

#: The token that a service holds. It is the token of no deployment.
TOKEN: Final = "vectors-bearer-token-0123456789abcdefghij"

#: A token with one character outside ASCII. Latin-1 has that character.
OUTSIDE: Final = "vectors-bearer-token-\u00e9-0123456789abcdefgh"

_SCHEME: Final = b"Bearer "
_T: Final = TOKEN.encode("utf-8")
_B: Final = _SCHEME + _T

#: The address of the client in a request that the generator makes.
CLIENT: Final = ("192.0.2.10", 50000)
SERVER: Final = ("testserver", 80)

JSON_TYPE: Final = (b"content-type", b"application/json")

HTTP_OK: Final = 200
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_NOT_FOUND: Final = 404
HTTP_NOT_IMPLEMENTED: Final = 501


@dataclass(frozen=True)
class Header:
    """One `Authorization` header, and the token that the service holds."""

    id: str
    #: The bytes of the value. None stands for a request with no such header.
    raw: bytes | None
    token: str = TOKEN

    def given(self) -> dict[str, Json]:
        return {"args": {"authorization": _header_json(self.raw)}}

    def extra(self) -> dict[str, object]:
        """The params of the vector: the token, when it is not the one of the context."""
        return {} if self.token == TOKEN else {"params": {"token": self.token}}


def _header_json(raw: bytes | None) -> Json:
    """The bytes of a header value: a text when they are UTF-8, else a marker."""
    if raw is None:
        return None

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return normalize(raw)


HEADERS: Final[tuple[Header, ...]] = (
    Header("exact", _B),
    # --- no token ---
    Header("no-header", None),
    Header("empty", b""),
    Header("scheme-only", b"Bearer"),
    Header("scheme-and-space", _SCHEME),
    Header("scheme-and-spaces", _SCHEME + b"   "),
    # --- the scheme ---
    Header("scheme-lower-case", b"bearer " + _T),
    Header("scheme-upper-case", b"BEARER " + _T),
    Header("scheme-basic", b"Basic " + _T),
    Header("no-scheme", _T),
    Header("two-spaces-after-the-scheme", _SCHEME + b" " + _T),
    Header("tab-after-the-scheme", b"Bearer\t" + _T),
    Header("space-before-the-scheme", b" " + _B),
    # --- the two ends of the token ---
    Header("space-at-the-end", _B + b" "),
    Header("tab-at-the-end", _B + b"\t"),
    Header("byte-a0-at-the-end", _B + b"\xa0"),
    Header("byte-a0-before-the-token", _SCHEME + b"\xa0" + _T),
    Header("u00a0-at-the-end", _B + "\u00a0".encode()),
    Header("byte-85-at-the-end", _B + b"\x85"),
    Header("byte-1c-at-the-end", _B + b"\x1c"),
    # --- another token ---
    Header("wrong-token", _SCHEME + _T[:-1] + b"X"),
    Header("one-more-character", _B + b"X"),
    Header("one-character-short", _B[:-1]),
    # --- a token with a character outside ASCII ---
    Header("outside-ascii-as-utf8", _SCHEME + OUTSIDE.encode("utf-8"), OUTSIDE),
    Header("outside-ascii-as-latin1", _SCHEME + OUTSIDE.encode("latin-1"), OUTSIDE),
)


@dataclass(frozen=True)
class Ask:
    """One request, as the bytes that a server gives an app."""

    method: str
    path: str
    headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class Reply:
    """The status and the body of one answer."""

    status: int
    body: bytes


async def _asgi(app: ASGIApp, ask: Ask) -> Reply | Raised:
    """One request to an app, with no server and no client between the two.

    A test client decodes a header value and encodes it again as UTF-8. A
    header value that is not UTF-8 then reaches the app as other bytes. This
    call gives the app the bytes as they are, as a server does.

    Only the call of the app can give a `Raised`. A fault of a step of the
    generator stops the generator.
    """
    sent: list[Message] = []
    waiting: list[Message] = [{"type": "http.request", "body": ask.body, "more_body": False}]

    async def receive() -> Message:
        return waiting.pop(0) if waiting else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": ask.method,
        "scheme": "http",
        "path": ask.path,
        "raw_path": ask.path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", SERVER[0].encode("ascii")), *ask.headers],
        "client": CLIENT,
        "server": SERVER,
    }
    try:
        await app(scope, receive, send)
    except Exception as exc:
        return Raised(exc)

    starts = [message for message in sent if message["type"] == "http.response.start"]
    if len(starts) != 1:
        raise ValueError(f"{ask.method} {ask.path}: the app started {len(starts)} answers")

    body = b"".join(
        cast("bytes", message.get("body", b""))
        for message in sent
        if message["type"] == "http.response.body"
    )

    return Reply(cast("int", starts[0]["status"]), body)


def _send(app: ASGIApp, ask: Ask, header: bytes | None) -> Reply | Raised:
    """One request with this `Authorization` header, or with none."""
    if header is not None:
        ask = Ask(ask.method, ask.path, (*ask.headers, (b"authorization", header)), ask.body)

    return asyncio.run(_asgi(app, ask))


def _reason(reply: Reply) -> object:
    """The `reason` of an answer of the chaperone, or None."""
    body: object = json.loads(reply.body)

    return cast("dict[str, object]", body).get("reason") if isinstance(body, dict) else None


@dataclass(frozen=True)
class Verdict:
    """Whether a service took the bearer of a request, and what it read from it."""

    took: bool
    value: object = None


class Gate(Protocol):
    """One service that holds one token."""

    def ask(self, header: bytes | None) -> Reply | Raised:
        """Sends one request. Returns the answer, or the exception that the app raised."""
        ...

    def verdict(self, reply: Reply) -> Verdict:
        """Reads the answer. A fault of the generator stops it here."""
        ...


class _AttendanceGate:
    """`attendance` with a stand-in service. The token is the one of door-owui."""

    ASK: Final = Ask("GET", "/v1/sessions")

    def __init__(self, scratch: Path, token: str) -> None:
        for other in Principal:
            _write(token_file(scratch, other.value), FILLER, MODE_OWNER)

        _write(token_file(scratch, Principal.DOOR_OWUI.value), token.encode("utf-8"), MODE_OWNER)
        book = TokenBook(scratch)
        book.load()
        self._service = StandIn()
        self._app = build_attendance_app(cast("SessionService", self._service), book)

    def ask(self, header: bytes | None) -> Reply | Raised:
        self._service.reset()

        return _send(self._app, self.ASK, header)

    def verdict(self, reply: Reply) -> Verdict:
        calls = self._service.calls
        if reply.status == HTTP_UNAUTHORIZED and not calls:
            return Verdict(took=False)

        if reply.status == HTTP_NOT_IMPLEMENTED and len(calls) == 1:
            return Verdict(took=True, value={"principal": calls[0].args[0]})

        raise ValueError(f"attendance answers {reply.status}")


class _NoFamilies:
    """Stands for the family directory of the Open WebUI door: no family."""

    def serving(self) -> list[str]:
        return []


class _OwuiGate:
    """The Open WebUI door. The token is the key of the door."""

    ASK: Final = Ask("GET", "/v1/models")

    def __init__(self, scratch: Path, token: str) -> None:
        config = owui_config.DoorConfig(
            bind_host="127.0.0.1",
            bind_port=8340,
            door_key=token,
            attendance_token=FILLER.decode("ascii"),
            attendance_url=owui_config.UDS_BASE_URL,
            attendance_socket=None,
            families_dir=scratch / "families",
        )
        # The route of the request makes no call to `attendance`.
        self._app = create_owui_app(config, cast("OwuiAttendance", object()), _NoFamilies())

    def ask(self, header: bytes | None) -> Reply | Raised:
        return _send(self._app, self.ASK, header)

    def verdict(self, reply: Reply) -> Verdict:
        if reply.status == HTTP_UNAUTHORIZED:
            return Verdict(took=False)

        if reply.status == HTTP_OK:
            return Verdict(took=True)

        raise ValueError(f"the Open WebUI door answers {reply.status}")


#: The one route of the trigger listener that the generator declares.
HOOK_FAMILY: Final = "chat"
HOOK_NAME: Final = "hook"
HOOK_PATH: Final = f"/triggers/{HOOK_FAMILY}/{HOOK_NAME}"

#: The message of the refusal that a stand-in answers each call with.
TAKEN: Final = "the stand-in took the call"


class _OneRoute:
    """Stands for the route table of the trigger listener: one webhook."""

    def __init__(self, token: str) -> None:
        self._route = Route(HOOK_FAMILY, HOOK_NAME, token)

    def get(self, family: str, name: str) -> Route | None:
        return self._route if (family, name) == (HOOK_FAMILY, HOOK_NAME) else None

    def refresh(self) -> int:
        return 1


class _CountingAttendance:
    """Stands for `attendance` behind the trigger listener.

    It counts each call and then refuses it. The listener calls it only
    after it took the bearer of a request.
    """

    def __init__(self) -> None:
        self.calls = 0

    def ensure_session(self, family: str, session: str) -> None:
        del family, session
        self.calls += 1
        raise AttendanceError("not_implemented", TAKEN, HTTP_NOT_IMPLEMENTED)

    def accepted_turn(self, request: TurnRequest) -> AcceptedTurn:
        del request
        self.calls += 1
        raise AttendanceError("not_implemented", TAKEN, HTTP_NOT_IMPLEMENTED)


class _TriggerGate:
    """The trigger listener. The token is the one of the webhook."""

    ASK: Final = Ask("POST", HOOK_PATH)

    def __init__(self, scratch: Path, token: str) -> None:
        config = ServeConfig(
            attendance=AttendanceTarget(owui_config.UDS_BASE_URL, None, FILLER.decode("ascii")),
            bind_host="127.0.0.1",
            bind_port=8360,
            families_dir=scratch / "families",
            registry_root=scratch / "registry",
            webhooks_dir=scratch / "webhooks",
            refresh_s=30.0,
        )
        self._attendance = _CountingAttendance()
        self._app = create_trigger_app(config, self._attendance, _OneRoute(token))

    def ask(self, header: bytes | None) -> Reply | Raised:
        self._attendance.calls = 0

        return _send(self._app, self.ASK, header)

    def verdict(self, reply: Reply) -> Verdict:
        calls = self._attendance.calls
        if reply.status == HTTP_NOT_FOUND and not calls:
            return Verdict(took=False)

        if reply.status == HTTP_NOT_IMPLEMENTED and calls == 1:
            return Verdict(took=True)

        raise ValueError(f"the trigger listener answers {reply.status}")


#: A gate id of the form that the chaperone takes. No gate has this id.
GATE: Final = "0123456789abcdef"


class _ChaperoneGate:
    """The chaperone with no phone rail. The token is the one of the approval callback."""

    ASK: Final = Ask("POST", f"/approval/{GATE}", (JSON_TYPE,), b'{"decision":"approve"}')

    def __init__(self, scratch: Path, token: str) -> None:
        config = PepConfig(
            audit_dir=scratch / "unidentified",
            rework_dir=scratch / "rework",
            approval_callback_token=token,
        )
        self._app = create_chaperone_app(config)

    def ask(self, header: bytes | None) -> Reply | Raised:
        return _send(self._app, self.ASK, header)

    def verdict(self, reply: Reply) -> Verdict:
        reason = _reason(reply)
        if reply.status == HTTP_FORBIDDEN and reason == "unknown_token":
            return Verdict(took=False)

        if reply.status == HTTP_NOT_IMPLEMENTED and reason == "not_implemented":
            return Verdict(took=True)

        raise ValueError(f"the chaperone answers {reply.status}")


@dataclass(frozen=True)
class Copy:
    """One copy of the bearer reader, in its service."""

    name: str
    entry: str
    contract: str
    ask: Ask
    build: Callable[[Path, str], Gate]
    notes: tuple[str, ...]


_NOTE_LATIN: Final = "The web framework decodes the bytes of a header as Latin-1."
_NOTE_SCHEME: Final = (
    "The copy takes a text that starts with Bearer and one space, in that case of letters."
)
_NOTE_STRIP: Final = (
    f"{_NOTE_LATIN} {_NOTE_SCHEME} It removes the whitespace of Python str.strip from the "
    "two ends of the rest. Then it encodes the rest as UTF-8 and compares the bytes with "
    "the token."
)

COPIES: Final[tuple[Copy, ...]] = (
    Copy(
        name="attendance",
        entry=(
            "attendance.auth.TokenBook.identify, through the route GET /v1/sessions of "
            "attendance.api.build_app"
        ),
        contract="contract 02 §3 rules 4 and 6, §3.1",
        ask=_AttendanceGate.ASK,
        build=_AttendanceGate,
        notes=(
            "The service holds one token for each principal. context.token is the token of "
            f"{Principal.DOOR_OWUI.value}. Each other principal has another token.",
            "An accepted vector is a request that the entry point finds a principal for. "
            "value.principal is that principal. A refused vector gets HTTP 401.",
            _NOTE_STRIP,
        ),
    ),
    Copy(
        name="door_owui",
        entry="agent_door_owui.app.create_app, the route GET /v1/models",
        contract="no contract: the module text of agent_door_owui.app",
        ask=_OwuiGate.ASK,
        build=_OwuiGate,
        notes=(
            "context.token is the key of the door. An accepted vector gets HTTP 200. A "
            "refused vector gets HTTP 401.",
            f"{_NOTE_LATIN} {_NOTE_SCHEME} It removes nothing from the rest. It encodes "
            "the rest as UTF-8 and compares the bytes with the key.",
        ),
    ),
    Copy(
        name="door_trigger",
        entry=f"agent_door_trigger.webhooks.create_app, the route POST {HOOK_PATH}",
        contract="contract 05 §6.4",
        ask=_TriggerGate.ASK,
        build=_TriggerGate,
        notes=(
            "context.token is the token of the one webhook that the route table holds. An "
            "accepted vector is a request that the listener starts a job for. A refused "
            "vector gets HTTP 404, the answer for a webhook that the listener does not know.",
            _NOTE_STRIP,
        ),
    ),
    Copy(
        name="chaperone",
        entry=f"chaperone.app.create_app, the route POST /approval/{GATE}",
        contract="contract 04 §8.4",
        ask=_ChaperoneGate.ASK,
        build=_ChaperoneGate,
        notes=(
            "context.token is the token of the approval callback. The request has the body "
            '{"decision":"approve"}. A refused vector gets HTTP 403 with the reason '
            "unknown_token. An accepted vector gets another answer: the chaperone of the "
            "generator has no phone rail, so it answers HTTP 501.",
            f"{_NOTE_LATIN} The copy splits the text at the first space. It takes the "
            "scheme bearer in each case of letters. Then it removes the whitespace of Python "
            "str.strip from the two ends of the rest, encodes the rest as UTF-8 and compares "
            "the bytes with the token.",
        ),
    ),
)


def _bearer_vector(header: Header, gate: Gate) -> Vector:
    given = header.given()
    with quiet_logs():
        outcome = gate.ask(header.raw)

    if isinstance(outcome, Raised):
        return raised(header.id, given, outcome.exc, **header.extra())

    verdict = gate.verdict(outcome)
    if verdict.took:
        return accepted(header.id, given, verdict.value, **header.extra())

    return refused(header.id, given, **header.extra())


_NOTE_HEADER: Final = (
    "The input is one request. args.authorization is the value of its Authorization header: "
    "a text stands for the UTF-8 bytes of the text, a $base64 marker holds bytes that are "
    "not UTF-8, and null stands for a request with no such header."
)
_NOTE_TOKEN: Final = (
    "context.token is the token that the service holds. A vector with params.token is for a "
    "service that holds that token in its place."
)
_NOTE_NO_SERVER: Final = (
    "The generator gives the app the bytes of the header with no server between the two. A "
    "server removes each space and each tab at the two ends of a header value before the "
    "app gets the value."
)


def _bearer_surface(copy: Copy, scratch: Path) -> Surface:
    tail = f"bearer.{copy.name}"
    gates: dict[str, Gate] = {}
    for number, token in enumerate((TOKEN, OUTSIDE)):
        home = scratch / copy.name / str(number)
        home.mkdir(parents=True)
        gates[token] = copy.build(home, token)

    return Surface(
        name=f"{GROUP}.{tail}",
        path=f"{GROUP}/{tail}.json",
        entry=copy.entry,
        contract=copy.contract,
        notes=(_NOTE_HEADER, _NOTE_TOKEN, _NOTE_NO_SERVER, *copy.notes),
        context={"token": TOKEN, "method": copy.ask.method, "path": copy.ask.path},
        vectors=tuple(_bearer_vector(header, gates[header.token]) for header in HEADERS),
    )


# --- the answers of the web framework ----------------------------------------

EDGE_CONTRACT: Final = "no contract: the answers of the web framework"

#: A path that no service has a route for.
UNKNOWN_PATH: Final = "/no/such/path"

#: The path of the route that the generator adds to each app. Its handler
#: raises.
RAISE_PATH: Final = "/vectors/raises"

GET: Final = "GET"
POST: Final = "POST"
HEAD: Final = "HEAD"


async def _raises() -> None:
    """The handler of the route that the generator adds."""
    raise RuntimeError("the generator made this handler raise")


@dataclass(frozen=True)
class Service:
    """One service, and one path that it has a route for."""

    name: str
    entry: str
    build: Callable[[Path], FastAPI]
    path: str
    #: The method of the route of `path`.
    method: str
    #: A method that no route of `path` takes.
    other: str
    notes: tuple[str, ...] = ()

    def requests(self) -> tuple[tuple[str, str, str], ...]:
        """Each request of the surface: the id of its vector, the method and the path."""
        route = "get" if self.method == GET else "post"

        return (
            ("unknown-path", GET, UNKNOWN_PATH),
            ("wrong-method", self.other, self.path),
            (f"head-on-a-{route}-route", HEAD, self.path),
            ("final-slash", self.method, f"{self.path}/"),
            ("handler-raises", GET, RAISE_PATH),
        )


def _attendance_app(scratch: Path) -> FastAPI:
    # No request of the surface gets to the service or to the tokens.
    return build_attendance_app(cast("SessionService", StandIn()), TokenBook(scratch))


def _owui_app(scratch: Path) -> FastAPI:
    config = owui_config.DoorConfig(
        bind_host="127.0.0.1",
        bind_port=8340,
        door_key=TOKEN,
        attendance_token=FILLER.decode("ascii"),
        attendance_url=owui_config.UDS_BASE_URL,
        attendance_socket=None,
        families_dir=scratch / "families",
    )

    return create_owui_app(config, cast("OwuiAttendance", object()), _NoFamilies())


def _trigger_app(scratch: Path) -> FastAPI:
    config = ServeConfig(
        attendance=AttendanceTarget(owui_config.UDS_BASE_URL, None, FILLER.decode("ascii")),
        bind_host="127.0.0.1",
        bind_port=8360,
        families_dir=scratch / "families",
        registry_root=scratch / "registry",
        webhooks_dir=scratch / "webhooks",
        refresh_s=30.0,
    )

    return create_trigger_app(config, _CountingAttendance(), _OneRoute(TOKEN))


def _chaperone_app(scratch: Path) -> FastAPI:
    return create_chaperone_app(
        PepConfig(audit_dir=scratch / "unidentified", rework_dir=scratch / "rework")
    )


def _noticeboard_app(scratch: Path) -> FastAPI:
    config = NoticeboardConfig(
        bind="127.0.0.1",
        port=8370,
        state_root=scratch / "state",
        registry_dir=scratch / "registry",
        attendance_socket=None,
        attendance_url=owui_config.UDS_BASE_URL,
        page_size=50,
    )
    # No request of the surface gets to a page that reads a session.
    reader = SessionReader(
        transport=cast("Transport", object()), token_file=scratch / "view-ro.token"
    )

    return build_noticeboard_app(config, reader)


_NOTE_OWN_HANDLER: Final = (
    "The service has a handler for an exception that no route expects. The answer of the "
    "vector handler-raises is the answer of that handler."
)

SERVICES: Final[tuple[Service, ...]] = (
    Service(
        name="attendance",
        entry="attendance.api.build_app",
        build=_attendance_app,
        path="/v1/sessions",
        method=GET,
        other="PUT",
        notes=(
            "The path /v1/sessions has a route for POST and a route for GET, in that order. "
            "The web framework names only the methods of the first route in the Allow header.",
            _NOTE_OWN_HANDLER,
        ),
    ),
    Service(
        name="door_owui",
        entry="agent_door_owui.app.create_app",
        build=_owui_app,
        path="/v1/models",
        method=GET,
        other=POST,
        notes=(_NOTE_OWN_HANDLER,),
    ),
    Service(
        name="door_trigger",
        entry="agent_door_trigger.webhooks.create_app",
        build=_trigger_app,
        path=HOOK_PATH,
        method=POST,
        other=GET,
        notes=(
            "The listener has no route for GET. The surface thus holds HEAD on a route for POST.",
            _NOTE_OWN_HANDLER,
        ),
    ),
    Service(
        name="chaperone",
        entry="chaperone.app.create_app",
        build=_chaperone_app,
        path="/healthz",
        method=GET,
        other=POST,
        notes=(_NOTE_OWN_HANDLER,),
    ),
    Service(
        name="noticeboard",
        entry="noticeboard.app.build_app",
        build=_noticeboard_app,
        path="/healthz",
        method=GET,
        other=POST,
        notes=(
            "The noticeboard of the generator binds loopback and has no access key, so its "
            "key check lets each request through.",
            _NOTE_OWN_HANDLER,
        ),
    ),
)


def _answer(status: int, headers: Mapping[str, str], content: bytes) -> dict[str, object]:
    """What a vector keeps of one answer."""
    answer: dict[str, object] = {
        "status": status,
        "content_type": headers.get("content-type"),
        "body": content.decode("utf-8"),
    }
    allow = headers.get("allow")
    if allow is not None:
        answer["allow"] = sorted(method.strip() for method in allow.split(","))

    location = headers.get("location")
    if location is not None:
        answer["location"] = urlsplit(location).path

    return answer


def _edge_vector(client: TestClient, vector_id: str, method: str, path: str) -> Vector:
    given: dict[str, Json] = {"args": {"method": method, "path": path}}
    with quiet_logs():
        outcome = attempt(lambda: client.request(method, path))

    if isinstance(outcome, Raised):
        return raised(vector_id, given, outcome.exc)

    answer = _answer(outcome.status_code, outcome.headers, outcome.content)

    return accepted(vector_id, given, answer)


def _edge_surface(service: Service, scratch: Path) -> Surface:
    tail = f"edge.{service.name}"
    home = scratch / service.name
    home.mkdir(parents=True)
    app = service.build(home)
    app.get(RAISE_PATH)(_raises)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    try:
        vectors = tuple(
            _edge_vector(client, vector_id, method, path)
            for vector_id, method, path in service.requests()
        )
    finally:
        client.close()

    return Surface(
        name=f"{GROUP}.{tail}",
        path=f"{GROUP}/{tail}.json",
        entry=service.entry,
        contract=EDGE_CONTRACT,
        notes=(
            "The input is one request with no body and no Authorization header: args.method "
            "and args.path. The generator sends it through the test client of the web "
            "framework and follows no redirect.",
            f"The path {service.path} has a route for {service.method}. The generator adds "
            f"one route to the app, {GET} {RAISE_PATH}. The handler of that route raises.",
            "value.status is the HTTP status. value.content_type is the Content-Type header, "
            "or null when the answer has none. value.body is the body as text: the answer "
            "holds the UTF-8 bytes of that text. The answer to HEAD has no body.",
            "value.allow is each method of the Allow header, in sorted order. value.location "
            "is the path of the Location header. The header also holds the scheme and the "
            "host name of the test client. An answer with no such header has no such key.",
            *service.notes,
        ),
        context={"raise_path": RAISE_PATH},
        vectors=vectors,
    )


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix="vectors-runtime-") as scratch_name:
        scratch = Path(scratch_name)

        return (
            *(_untrusted_surface(helper) for helper in HELPERS),
            _parse_surface(),
            *(_token_surface(reader, scratch / "token") for reader in TOKEN_READERS),
            *(_bearer_surface(copy, scratch / "bearer") for copy in COPIES),
            *(_edge_surface(service, scratch / "edge") for service in SERVICES),
        )
