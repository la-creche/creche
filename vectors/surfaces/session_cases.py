"""The written inputs of the session API surfaces (contract 02).

`session.py` holds the surfaces. This module holds what each one is given:
the request bodies, the query parameters, the error bodies and the journal
lines. Every input is text written here. None names a deployment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from attendance.errors import ErrorCode, TurnReason
from attendance.models import LineKind
from attendance.states import TurnState

from vectors.core import Json, bytes_input, expand, repeat_input

FAMILY: Final = "chat"
SESSION: Final = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
OTHER_SESSION: Final = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"
JOB_SESSION: Final = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
TURN: Final = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
DELEGATION: Final = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
SANDBOX: Final = "chat-s2"
DIGEST: Final = "0123456789abcdef" * 4

#: Deeper than a JSON reader of any supported interpreter goes.
VERY_DEEP: Final = 400_000
#: One integer of this many digits is past the limit of the interpreter.
HUGE_DIGITS: Final = 5_000
#: The longest integer that the interpreter reads, in digits.
INT_DIGITS_MAX: Final = 4_300

#: The caps of `attendance.requests`, as numbers a vector can stand beside.
PROMPT_BYTES: Final = 262_144
MESSAGE_BYTES: Final = 65_536
STEER_BYTES: Final = 4_096

#: A float whose shortest text has 17 digits. A reader that is not exact to
#: the last bit reads the float below it, and then writes other digits.
LONG_FLOAT: Final = 3.7615293000000003


def _json(value: object) -> bytes:
    """One compact JSON document, the way a door writes a body."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Body:
    """One request body: its bytes, or a long text written as parts."""

    id: str
    raw: bytes = b""
    parts: tuple[tuple[str, int], ...] = ()

    def data(self) -> bytes:
        return expand(self.parts).encode("utf-8") if self.parts else self.raw

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else bytes_input(self.raw)


def _o(body_id: str, fields: dict[str, object]) -> Body:
    """A body that is one JSON object."""
    return Body(body_id, _json(fields))


#: Stands in a written field for one half of a surrogate pair. `_half_pair`
#: puts the JSON escape of that half in its place.
_HALF: Final = "\ue000"


def _half_pair(body: Body) -> Body:
    """The body with the escape of one half of a surrogate pair at each `_HALF`.

    The reader makes a text from that escape, and the text has no UTF-8 form.
    """
    return Body(body.id, body.raw.replace(_HALF.encode(), b"\\ud800"))


def _long(body_id: str, head: str, fill: str, count: int, tail: str) -> Body:
    """A body with one long run of `fill` between `head` and `tail`."""
    return Body(body_id, parts=((head, 1), (fill, count), (tail, 1)))


# --- create or find a session (§5.1) -------------------------------------------

_CREATE: Final[dict[str, object]] = {"family": FAMILY, "session": SESSION}
_CREATE_TEXT: Final = _json(_CREATE).decode("utf-8")
_CREATE_HEAD: Final = _CREATE_TEXT[:-1]


def _create(body_id: str, **fields: object) -> Body:
    return _o(body_id, {**_CREATE, **fields})


def _labels(count: int) -> dict[str, str]:
    return {f"key-{number}": "v" for number in range(count)}


#: What the reader of a body does before a parser sees a field. Every route
#: with a body shares it. The vectors are on the create route.
READER_BODIES: Final[tuple[Body, ...]] = (
    Body("body-empty", b""),
    Body("body-spaces", b" \r\n\t"),
    Body("json-text", b"not json"),
    Body("json-truncated", _CREATE_TEXT[:-1].encode()),
    Body("json-trailing-text", _CREATE_TEXT.encode() + b" x"),
    Body("json-trailing-comma", _CREATE_HEAD.encode() + b",}"),
    Body("json-two-documents", _CREATE_TEXT.encode() * 2),
    Body("json-single-quotes", _CREATE_TEXT.replace('"', "'").encode()),
    Body("json-spaces-around", b" \r\n" + _CREATE_TEXT.encode() + b"\t\n "),
    Body("json-pretty", json.dumps(_CREATE, indent=2).encode() + b"\n"),
    Body("json-control-char-in-text", _CREATE_HEAD.encode() + b',"title":"a\nb"}'),
    Body("json-escaped-text", _CREATE_HEAD.encode() + b',"title":"\\u0061\\n\\ud83d\\ude00"}'),
    Body(
        "json-duplicate-key",
        _CREATE_TEXT.replace('"family":"chat"', '"family":"Not A Family","family":"chat"').encode(),
    ),
    Body("json-duplicate-key-last-bad", _CREATE_HEAD.encode() + b',"family":"Not A Family"}'),
    Body("json-nan-unknown-field", _CREATE_HEAD.encode() + b',"x":NaN}'),
    Body("json-infinity-unknown-field", _CREATE_HEAD.encode() + b',"x":-Infinity}'),
    Body("json-float-out-of-range", _CREATE_HEAD.encode() + b',"x":1e400}'),
    Body("json-lone-surrogate-unknown-field", _CREATE_HEAD.encode() + b',"x":"\\udc00"}'),
    Body("json-lone-surrogate-unknown-key", _CREATE_HEAD.encode() + b',"\\ud800":1}'),
    Body("bytes-surrogate-unknown-field", _CREATE_HEAD.encode() + b',"x":"\xed\xa0\x80"}'),
    _long("json-integer-4300-digits", _CREATE_HEAD + ',"x":', "9", INT_DIGITS_MAX, "}"),
    _long("json-integer-4301-digits", _CREATE_HEAD + ',"x":', "9", INT_DIGITS_MAX + 1, "}"),
    _long("json-negative-integer-4300-digits", _CREATE_HEAD + ',"x":-', "9", INT_DIGITS_MAX, "}"),
    _long("json-float-5000-digits", _CREATE_HEAD + ',"x":', "9", HUGE_DIGITS, ".5}"),
    Body(
        "json-deep-128",
        parts=((_CREATE_HEAD + ',"x":', 1), ("[", 127), ("]", 127), ("}", 1)),
    ),
    Body(
        "json-deep-129",
        parts=((_CREATE_HEAD + ',"x":', 1), ("[", 128), ("]", 128), ("}", 1)),
    ),
    Body(
        "json-very-deep",
        parts=((_CREATE_HEAD + ',"x":', 1), ("[", VERY_DEEP), ("]", VERY_DEEP), ("}", 1)),
    ),
    Body("json-brackets-in-text", _CREATE_HEAD.encode() + b',"title":"' + b"[" * 200 + b'"}'),
    Body("bytes-bom", b"\xef\xbb\xbf" + _CREATE_TEXT.encode()),
    Body("bytes-utf16", _CREATE_TEXT.encode("utf-16")),
    Body("bytes-utf16-le-no-bom", _CREATE_TEXT.encode("utf-16-le")),
    Body("bytes-utf32", _CREATE_TEXT.encode("utf-32")),
    Body("bytes-not-utf8", _CREATE_HEAD.encode() + b',"title":"\xff"}'),
    Body("bytes-nul-after", _CREATE_TEXT.encode() + b"\x00"),
    Body("top-array", b"[" + _CREATE_TEXT.encode() + b"]"),
    Body("top-null", b"null"),
    Body("top-text", b'"chat"'),
    Body("top-number", b"5"),
    Body("top-true", b"true"),
    Body("top-empty-object", b"{}"),
)

CREATE_BODIES: Final[tuple[Body, ...]] = (
    _create(
        "full",
        title="Kitchen sensor debug",
        labels={"door": "owui", "room": "kitchen"},
        owner_session=OTHER_SESSION,
    ),
    _create("minimal"),
    _create("unknown-field", note="written by hand", turn=TURN),
    # --- family ---
    _o("family-missing", {"session": SESSION}),
    _create("family-null", family=None),
    _create("family-number", family=5),
    _create("family-list", family=[FAMILY]),
    _create("family-empty", family=""),
    _create("family-upper", family="Chat"),
    _create("family-underscore", family="agent_control"),
    _create("family-one-char", family="a"),
    _create("family-31-chars", family="a" * 31),
    _create("family-32-chars", family="a" * 32),
    _create("family-trailing-newline", family="chat\n"),
    _create("family-arabic-digit", family="chat\u0661"),
    # --- session ---
    _o("session-missing", {"family": FAMILY}),
    _create("session-null", session=None),
    _create("session-number", session=5),
    _create("session-empty", session=""),
    _create("session-any-prefix", session="anything.at_all-1"),
    _create("session-dot", session="."),
    _create("session-two-dots", session=".."),
    _create("session-slash", session="owui-a/b"),
    _create("session-leading-dash", session="-owui"),
    _create("session-128-chars", session="s" * 128),
    _create("session-129-chars", session="s" * 129),
    _create("session-trailing-newline", session=SESSION + "\n"),
    _create("family-and-session-bad", family="Chat", session=".."),
    # --- owner_session ---
    _create("owner-null", owner_session=None),
    _create("owner-empty", owner_session=""),
    _create("owner-number", owner_session=5),
    _create("owner-list", owner_session=[OTHER_SESSION]),
    _create("owner-two-dots", owner_session=".."),
    _create("owner-129-chars", owner_session="s" * 129),
    # --- title ---
    _create("title-empty", title=""),
    _create("title-null", title=None),
    _create("title-number", title=5),
    _create("title-200-chars", title="t" * 200),
    _create("title-201-chars", title="t" * 201),
    _create("title-200-two-byte-chars", title="\u00e9" * 200),
    _create("title-200-four-byte-chars", title="\U0001f600" * 200),
    _create("title-201-four-byte-chars", title="\U0001f600" * 201),
    _create("title-any-text", title='line\nbreak "quoted" \\ </script> \u2028'),
    _create("owner-bad-and-title-long", owner_session="..", title="t" * 201),
    # --- labels ---
    _create("labels-empty", labels={}),
    _create("labels-null", labels=None),
    _create("labels-list", labels=["door", "owui"]),
    _create("labels-text", labels="door=owui"),
    _create("labels-10-keys", labels=_labels(10)),
    _create("labels-11-keys", labels=_labels(11)),
    Body(
        "labels-11-entries-10-keys",
        _CREATE_HEAD.encode()
        + b',"labels":{'
        + ",".join(f'"key-{number % 10}":"v{number}"' for number in range(11)).encode()
        + b"}}",
    ),
    _create("labels-value-200-chars", labels={"door": "v" * 200}),
    _create("labels-value-201-chars", labels={"door": "v" * 201}),
    _create("labels-value-200-two-byte-chars", labels={"door": "\u00e9" * 200}),
    _create("labels-value-empty", labels={"door": ""}),
    _create("labels-value-number", labels={"door": 5}),
    _create("labels-value-null", labels={"door": None}),
    _create("labels-value-true", labels={"door": True}),
    _create("labels-value-object", labels={"door": {"name": "owui"}}),
    _create("labels-two-bad-values", labels={"zeta": 1, "alpha": 2}),
    _create("labels-key-empty", labels={"": "v"}),
    _create("labels-key-300-chars", labels={"k" * 300: "v"}),
    _create("labels-key-any-text", labels={"Not A Key\n\u00e9": "v"}),
    _create("title-long-and-labels-bad", title="t" * 201, labels={"door": 5}),
    # --- a text with one half of a surrogate pair ---
    _half_pair(_create("half-pair-in-title", title=f"Kitchen{_HALF}")),
    _half_pair(_create("half-pair-in-label-value", labels={"room": _HALF})),
    _half_pair(_create("half-pair-in-label-key", labels={_HALF: "kitchen"})),
    _half_pair(_create("half-pair-in-family", family=f"chat{_HALF}")),
    _half_pair(_create("half-pair-in-session", session=f"owui-{_HALF}")),
    _half_pair(_create("half-pair-in-owner-session", owner_session=f"owui-{_HALF}")),
)

# --- run a turn (§5.4) -------------------------------------------------------------


def _turn(body_id: str, **fields: object) -> Body:
    return _o(body_id, {"prompt": "Which sensor dropped out last night?", **fields})


_OWUI: Final[dict[str, object]] = {
    "chat_id": "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33",
    "message_id": "b7c1e2d0-1f44-4c61-8a2b-9e0d3c5f7a11",
}

_PROMPT_HEAD: Final = '{"prompt":"'

#: One `fired_at` or `since` text per form. The two parsers of
#: `attendance.requests` give each one to `attendance.clock.parse_rfc3339`.
#: Each text here has the same result under every supported interpreter.
TIMES: Final[tuple[tuple[str, str], ...]] = (
    ("zulu", "2026-10-06T06:00:00Z"),
    ("offset-zero", "2026-10-06T06:00:00+00:00"),
    ("offset-minus-zero", "2026-10-06T06:00:00-00:00"),
    ("offset-plus", "2026-10-06T06:00:00+05:30"),
    ("offset-minus", "2026-10-06T06:00:00-05:30"),
    ("offset-no-colon", "2026-10-06T06:00:00+0530"),
    ("offset-hours-only", "2026-10-06T06:00:00+05"),
    ("offset-with-seconds", "2026-10-06T06:00:00+05:30:15"),
    ("offset-with-fraction", "2026-10-06T06:00:00+05:30:15.5"),
    ("offset-23-59", "2026-10-06T06:00:00+23:59"),
    ("offset-minus-23-59", "2026-10-06T06:00:00-23:59"),
    ("offset-24-00", "2026-10-06T06:00:00+24:00"),
    ("offset-60-minutes", "2026-10-06T06:00:00+00:60"),
    ("offset-one-digit", "2026-10-06T06:00:00+5"),
    ("offset-empty", "2026-10-06T06:00:00+"),
    ("offset-then-zulu", "2026-10-06T06:00:00+00:00Z"),
    ("offset-over-a-year-end", "2026-12-31T23:30:00-01:00"),
    ("offset-over-a-leap-day", "2024-02-29T23:30:00-01:00"),
    ("no-zone", "2026-10-06T06:00:00"),
    ("date-only", "2026-10-06"),
    ("date-and-zulu", "2026-10-06Z"),
    ("date-and-offset", "2026-10-06+06:00"),
    ("hours-only", "2026-10-06T06"),
    ("no-seconds", "2026-10-06T06:00"),
    ("no-seconds-zulu", "2026-10-06T06:00Z"),
    ("space-separator", "2026-10-06 06:00:00Z"),
    ("lower-t-separator", "2026-10-06t06:00:00Z"),
    ("two-byte-separator", "2026-10-06\u00e906:00:00Z"),
    ("four-byte-separator", "2026-10-06\U0001f60006:00:00Z"),
    ("two-separators", "2026-10-06TT06:00:00Z"),
    ("no-separator", "2026-10-0606:00:00Z"),
    ("basic-date", "20261006"),
    ("basic-date-and-time", "20261006T060000Z"),
    ("basic-time", "2026-10-06T060000Z"),
    ("basic-date-extended-time", "20261006T06:00:00Z"),
    ("basic-hours-minutes", "2026-10-06T0600+05"),
    ("mixed-time-separators", "2026-10-06T06:0000Z"),
    ("week-date", "2026-W38-6"),
    ("week-date-and-time", "2026-W38-6T06:00:00Z"),
    ("basic-week-date", "2026W386"),
    ("fraction-1-digit", "2026-10-06T06:00:00.1Z"),
    ("fraction-3-digits", "2026-10-06T06:00:00.123Z"),
    ("fraction-6-digits", "2026-10-06T06:00:00.123456Z"),
    ("fraction-7-digits", "2026-10-06T06:00:00.1234567Z"),
    ("fraction-20-digits", "2026-10-06T06:00:00.12345678901234567890Z"),
    ("fraction-comma", "2026-10-06T06:00:00,5Z"),
    ("fraction-and-offset", "2026-10-06T06:00:00.1234+05:30"),
    ("fraction-twice", "2026-10-06T06:00:00.5.5Z"),
    ("fraction-arabic-digit", "2026-10-06T06:00:00.\u0661Z"),
    ("lower-zulu", "2026-10-06T06:00:00z"),
    ("space-before-zulu", "2026-10-06T06:00:00 Z"),
    ("digit-before-zulu", "2026-10-06T06:00:009Z"),
    ("colon-before-offset", "2026-10-06T06:00:+05:30"),
    ("text-after-a-fraction", "2026-10-06T06:00:00.123456abc+05:30"),
    ("basic-time-and-more-digits", "2026-10-06T06000530"),
    ("two-zulus", "2026-10-06T06:00:00ZZ"),
    ("zone-name", "2026-10-06T06:00:00UTC"),
    ("leading-space", " 2026-10-06T06:00:00Z"),
    ("trailing-space", "2026-10-06T06:00:00Z "),
    ("trailing-newline", "2026-10-06T06:00:00Z\n"),
    ("one-digit-month", "2026-9-19T06:00:00Z"),
    ("one-digit-hour", "2026-10-06T6:00:00Z"),
    ("two-digit-year", "26-09-19T06:00:00Z"),
    ("arabic-digits-year", "\u0662\u0660\u0662\u0666-09-19T06:00:00Z"),
    ("leap-day", "2024-02-29T00:00:00Z"),
    ("no-leap-day", "2026-02-29T00:00:00Z"),
    ("month-13", "2026-13-01T00:00:00Z"),
    ("day-31-of-september", "2026-09-31T00:00:00Z"),
    ("day-zero", "2026-09-00T00:00:00Z"),
    ("hour-25", "2026-10-06T25:00:00Z"),
    ("minute-60", "2026-10-06T23:60:00Z"),
    ("second-60", "2026-10-06T23:59:60Z"),
    ("year-zero", "0000-01-01T00:00:00Z"),
    ("year-one", "0001-01-01T00:00:00Z"),
    ("year-9999", "9999-12-31T23:59:59.999999Z"),
    ("year-10000", "10000-01-01T00:00:00Z"),
    ("epoch", "1970-01-01T00:00:00Z"),
    ("before-the-epoch", "1969-12-31T23:59:59.999999Z"),
    ("only-zulu", "Z"),
    ("words", "last Tuesday"),
    ("unix-seconds", "1789797600"),
    ("space-before-offset", "2026-10-06 06:00:00 +0200"),
    ("two-spaces-before-zulu", "2026-10-06T06:00:00  Z"),
    ("space-and-no-offset", "2026-10-06T06:00:00 "),
    ("text-after-a-short-fraction", "2026-10-06T06:00:00.12 Z"),
    ("text-after-zulu-and-nul", "2026-10-06T06:00:00Z\x00abc"),
    ("basic-time-and-one-more-digit", "2026-10-06T0600003"),
    ("basic-time-digits-and-text", "2026-10-06T060005301234.5+00:00"),
    ("week-date-no-day", "2026-W38"),
    ("basic-week-date-no-day", "2026W38"),
    ("week-date-dash-separator", "2026-W38-06:00"),
    ("basic-week-date-digit-separator", "2026W386060000"),
    ("week-53", "2026-W53-1"),
    ("week-53-of-a-short-year", "2025-W53-1"),
    ("week-date-day-8", "2026-W38-8"),
    ("week-date-past-year-9999", "9999-W52-6"),
    ("offset-moves-before-year-one", "0001-01-01T00:00:00+00:01"),
    ("offset-moves-past-year-9999", "9999-12-31T23:59:59-00:01"),
    ("offset-moves-to-year-one", "0001-01-01T00:01:00+00:01"),
)

RUN_TURN_BODIES: Final[tuple[Body, ...]] = (
    _turn(
        "full",
        idempotency_key="b7c1e2d0-1f44-4c61-8a2b-9e0d3c5f7a11",
        wait="settled",
        persona_text="You answer as the house assistant.",
        attachments=["notes.txt", "plan-2.pdf"],
        owui={
            **_OWUI,
            "user_message_id": "a1b2c3d4-5e6f-4071-8293-a4b5c6d7e8f9",
            "parent_id": "9f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f",
        },
        trigger={"kind": "webhook", "name": "boiler-alert", "fired_at": "2026-10-06T06:00:00Z"},
        deadline_s=600,
        labels={"door": "owui"},
    ),
    _o("minimal", {"prompt": "x"}),
    _turn("unknown-field", delegation={"id": DELEGATION}, turn=TURN),
    # --- prompt ---
    _o("prompt-missing", {"wait": "stream"}),
    _o("prompt-null", {"prompt": None}),
    _o("prompt-empty", {"prompt": ""}),
    _o("prompt-number", {"prompt": 5}),
    _o("prompt-list", {"prompt": ["x"]}),
    _o("prompt-any-text", {"prompt": 'caf\u00e9 "quoted" \\ \u2028 \U0001f600 \x7f\n\t'}),
    _long("prompt-at-the-cap", _PROMPT_HEAD, "a", PROMPT_BYTES, '"}'),
    _long("prompt-over-the-cap", _PROMPT_HEAD, "a", PROMPT_BYTES + 1, '"}'),
    _long("prompt-two-byte-chars-over-the-cap", _PROMPT_HEAD, "\u00e9", PROMPT_BYTES // 2, 'a"}'),
    # --- idempotency_key ---
    _turn("key-null", idempotency_key=None),
    _turn("key-empty", idempotency_key=""),
    _turn("key-number", idempotency_key=5),
    _turn("key-200-chars", idempotency_key="k" * 200),
    _turn("key-201-chars", idempotency_key="k" * 201),
    _turn("key-200-two-byte-chars", idempotency_key="\u00e9" * 200),
    _turn("key-any-text", idempotency_key="Not A Key\n"),
    # --- wait ---
    _turn("wait-stream", wait="stream"),
    _turn("wait-settled", wait="settled"),
    _turn("wait-accepted", wait="accepted"),
    _turn("wait-upper", wait="Stream"),
    _turn("wait-other-word", wait="queued"),
    _turn("wait-trailing-space", wait="stream "),
    _turn("wait-empty", wait=""),
    _turn("wait-null", wait=None),
    _turn("wait-number", wait=1),
    _turn("wait-true", wait=True),
    # --- persona_text ---
    _turn("persona-empty", persona_text=""),
    _turn("persona-null", persona_text=None),
    _turn("persona-number", persona_text=5),
    _turn("persona-list", persona_text=["You answer."]),
    _turn("persona-over-16-kib", persona_text="p" * 16_385),
    # --- attachments ---
    _turn("attachments-empty", attachments=[]),
    _turn("attachments-null", attachments=None),
    _turn("attachments-text", attachments="notes.txt"),
    _turn("attachments-object", attachments={"0": "notes.txt"}),
    _turn("attachments-20", attachments=[f"file-{number}.txt" for number in range(20)]),
    _turn("attachments-21", attachments=[f"file-{number}.txt" for number in range(21)]),
    _turn("attachments-21-with-a-bad-name", attachments=["..", *(["a"] * 20)]),
    _turn("attachments-same-name-twice", attachments=["notes.txt", "notes.txt"]),
    _turn("attachments-hidden-and-dash", attachments=[".hidden", "-rf"]),
    _turn("attachments-dot", attachments=["."]),
    _turn("attachments-two-dots", attachments=[".."]),
    _turn("attachments-slash", attachments=["a/b"]),
    _turn("attachments-empty-name", attachments=[""]),
    _turn("attachments-space", attachments=["my notes.txt"]),
    _turn("attachments-120-chars", attachments=["n" * 120]),
    _turn("attachments-121-chars", attachments=["n" * 121]),
    _turn("attachments-number", attachments=[5]),
    _turn("attachments-null-entry", attachments=["notes.txt", None]),
    _turn("attachments-trailing-newline", attachments=["notes.txt\n"]),
    # --- owui (§10) ---
    _turn("owui-two-ids", owui=_OWUI),
    _turn("owui-null", owui=None),
    _turn("owui-text", owui="3f2a9c41"),
    _turn("owui-list", owui=[_OWUI]),
    _turn("owui-empty-object", owui={}),
    _turn("owui-no-message-id", owui={"chat_id": "c"}),
    _turn("owui-no-chat-id", owui={"message_id": "m"}),
    _turn("owui-empty-chat-id", owui={**_OWUI, "chat_id": ""}),
    _turn("owui-chat-id-number", owui={**_OWUI, "chat_id": 5}),
    _turn("owui-message-id-null", owui={**_OWUI, "message_id": None}),
    _turn("owui-parent-null", owui={**_OWUI, "parent_id": None}),
    _turn("owui-parent-empty", owui={**_OWUI, "parent_id": ""}),
    _turn("owui-parent-number", owui={**_OWUI, "parent_id": 5}),
    _turn("owui-user-message-number", owui={**_OWUI, "user_message_id": 5}),
    _turn("owui-any-text", owui={"chat_id": "Not An Id\n", "message_id": "m" * 300}),
    _turn("owui-unknown-field", owui={**_OWUI, "task": "title"}),
    # --- trigger (§13.2) ---
    _turn("trigger-timer", trigger={"kind": "timer"}),
    _turn("trigger-webhook", trigger={"kind": "webhook", "name": "boiler-alert"}),
    _turn(
        "trigger-dispatch",
        trigger={"kind": "dispatch", "name": "scrum-lead", "chain": ["chat", "scrum-lead"]},
    ),
    _turn("trigger-null", trigger=None),
    _turn("trigger-text", trigger="timer"),
    _turn("trigger-list", trigger=[{"kind": "timer"}]),
    _turn("trigger-empty-object", trigger={}),
    _turn("trigger-kind-null", trigger={"kind": None}),
    _turn("trigger-kind-number", trigger={"kind": 5}),
    _turn("trigger-kind-empty", trigger={"kind": ""}),
    _turn("trigger-kind-unknown", trigger={"kind": "cron"}),
    _turn("trigger-kind-upper", trigger={"kind": "Timer"}),
    _turn("trigger-name-number", trigger={"kind": "timer", "name": 5}),
    _turn("trigger-name-empty", trigger={"kind": "timer", "name": ""}),
    _turn("trigger-name-any-text", trigger={"kind": "webhook", "name": "Not A Name\n" * 40}),
    _turn("trigger-fired-at-number", trigger={"kind": "timer", "fired_at": 1789797600}),
    _turn("trigger-fired-at-empty", trigger={"kind": "timer", "fired_at": ""}),
    _turn("trigger-fired-at-null", trigger={"kind": "timer", "fired_at": None}),
    _turn("trigger-unknown-field", trigger={"kind": "timer", "family": "chat"}),
    _turn("trigger-chain-on-a-timer", trigger={"kind": "timer", "chain": ["chat"]}),
    _turn("trigger-empty-chain-on-a-timer", trigger={"kind": "timer", "chain": []}),
    _turn("trigger-null-chain-on-a-timer", trigger={"kind": "timer", "chain": None}),
    _turn("trigger-chain-empty", trigger={"kind": "dispatch", "chain": []}),
    _turn("trigger-chain-null", trigger={"kind": "dispatch", "chain": None}),
    _turn("trigger-chain-text", trigger={"kind": "dispatch", "chain": "chat"}),
    _turn("trigger-chain-object", trigger={"kind": "dispatch", "chain": {"0": "chat"}}),
    _turn("trigger-chain-8", trigger={"kind": "dispatch", "chain": ["chat"] * 8}),
    _turn("trigger-chain-9", trigger={"kind": "dispatch", "chain": ["chat"] * 9}),
    _turn("trigger-chain-bad-name", trigger={"kind": "dispatch", "chain": ["chat", "Scrum"]}),
    _turn("trigger-chain-number", trigger={"kind": "dispatch", "chain": ["chat", 5]}),
    _turn("trigger-chain-9-with-a-bad-name", trigger={"kind": "dispatch", "chain": ["Scrum"] * 9}),
    _turn(
        "trigger-kind-unknown-and-chain",
        trigger={"kind": "cron", "fired_at": "never", "chain": "chat"},
    ),
    _turn(
        "trigger-fired-at-bad-and-chain-bad",
        trigger={"kind": "timer", "fired_at": "never", "chain": ["chat"]},
    ),
    *(
        _turn(f"trigger-fired-at-{name}", trigger={"kind": "timer", "fired_at": text})
        for name, text in TIMES
    ),
    # --- the three trigger labels (§13.2 rule 2) ---
    _turn("labels-trigger-kind", labels={"trigger_kind": "timer"}),
    _turn(
        "labels-trigger-all-three",
        labels={
            "trigger_kind": "webhook",
            "trigger_name": "boiler-alert",
            "trigger_fired_at": "2026-10-06T06:00:00Z",
        },
    ),
    _turn("labels-trigger-kind-dispatch", labels={"trigger_kind": "dispatch"}),
    _turn("labels-trigger-kind-unknown", labels={"trigger_kind": "cron"}),
    _turn("labels-trigger-kind-empty", labels={"trigger_kind": ""}),
    _turn("labels-trigger-name-empty", labels={"trigger_kind": "timer", "trigger_name": ""}),
    _turn(
        "labels-trigger-fired-at-empty", labels={"trigger_kind": "timer", "trigger_fired_at": ""}
    ),
    _turn(
        "labels-trigger-fired-at-bad",
        labels={"trigger_kind": "timer", "trigger_fired_at": "this morning"},
    ),
    _turn("labels-trigger-name-and-no-kind", labels={"trigger_name": "boiler-alert"}),
    _turn(
        "labels-trigger-and-the-object",
        trigger={"kind": "webhook", "name": "from-the-object"},
        labels={"trigger_kind": "timer", "trigger_name": "from-the-labels"},
    ),
    _turn(
        "labels-trigger-and-a-trigger-that-is-text",
        trigger="webhook",
        labels={"trigger_kind": "timer"},
    ),
    # --- deadline_s ---
    _turn("deadline-1", deadline_s=1),
    _turn("deadline-3600", deadline_s=3600),
    _turn("deadline-0", deadline_s=0),
    _turn("deadline-3601", deadline_s=3601),
    _turn("deadline-negative", deadline_s=-60),
    _turn("deadline-null", deadline_s=None),
    _turn("deadline-text", deadline_s="60"),
    _turn("deadline-true", deadline_s=True),
    _turn("deadline-float-whole", deadline_s=60.0),
    _turn("deadline-float-fraction", deadline_s=60.5),
    _turn("deadline-list", deadline_s=[60]),
    Body("deadline-exponent", b'{"prompt":"x","deadline_s":6e1}'),
    Body("deadline-minus-zero", b'{"prompt":"x","deadline_s":-0}'),
    Body("deadline-float-out-of-range", b'{"prompt":"x","deadline_s":1e400}'),
    Body("deadline-nan", b'{"prompt":"x","deadline_s":NaN}'),
    _turn("deadline-past-64-bits", deadline_s=2**70),
    _turn("deadline-negative-past-64-bits", deadline_s=-(2**70)),
    _long("deadline-4300-digits", '{"prompt":"x","deadline_s":', "9", INT_DIGITS_MAX, "}"),
    Body("deadline-twice", b'{"prompt":"x","deadline_s":0,"deadline_s":60}'),
    # --- labels ---
    _turn("labels-10-keys", labels=_labels(10)),
    _turn("labels-11-keys", labels=_labels(11)),
    _turn("labels-value-number", labels={"door": 5}),
    _turn("labels-list", labels=["door"]),
    # --- the order of the checks ---
    Body(
        "order-prompt-over-the-cap-and-labels-bad",
        parts=((_PROMPT_HEAD, 1), ("a", PROMPT_BYTES + 1), ('","labels":{"door":5}}', 1)),
    ),
    _turn("order-labels-and-key", labels={"door": 5}, idempotency_key="k" * 201),
    _turn("order-key-and-wait", idempotency_key="k" * 201, wait="queued"),
    _turn("order-wait-and-persona", wait="queued", persona_text=5),
    _turn("order-persona-and-attachments", persona_text=5, attachments=[".."]),
    _turn("order-attachments-and-owui", attachments=[".."], owui={}),
    _turn("order-owui-and-trigger", owui={}, trigger={}),
    _turn("order-trigger-and-deadline", trigger={}, deadline_s=0),
    # --- a text with one half of a surrogate pair ---
    _half_pair(_o("half-pair-in-prompt", {"prompt": f"Which sensor{_HALF}"})),
    _half_pair(_turn("half-pair-in-key", idempotency_key=f"key-{_HALF}")),
    _half_pair(_turn("half-pair-in-wait", wait=f"settled{_HALF}")),
    _half_pair(_turn("half-pair-in-persona", persona_text=f"You answer{_HALF}")),
    _half_pair(_turn("half-pair-in-label-value", labels={"room": _HALF})),
    _half_pair(_turn("half-pair-in-label-key", labels={_HALF: "kitchen"})),
    _half_pair(_turn("half-pair-in-owui-chat-id", owui={**_OWUI, "chat_id": _HALF})),
    _half_pair(_turn("half-pair-in-owui-message-id", owui={**_OWUI, "message_id": _HALF})),
    _half_pair(_turn("half-pair-in-owui-parent-id", owui={**_OWUI, "parent_id": _HALF})),
    _half_pair(_turn("half-pair-in-trigger-name", trigger={"kind": "timer", "name": _HALF})),
    _half_pair(_turn("half-pair-in-trigger-kind", trigger={"kind": f"timer{_HALF}"})),
    _half_pair(
        _turn("half-pair-in-trigger-fired-at", trigger={"kind": "timer", "fired_at": _HALF})
    ),
)

# --- the writer lease, steer and stop (§5.6, §5.7, §5.9) ---------------------------

WRITER_BODIES: Final[tuple[Body, ...]] = (
    _o("full", {"holder": "tui", "force": False, "intent": "acquire"}),
    _o("empty-object", {}),
    Body("no-body", b""),
    Body("top-null", b"null"),
    _o("unknown-field", {"door_instance": "tui.4242"}),
    *(_o(f"holder-{holder}", {"holder": holder}) for holder in ("owui", "tui", "delegate")),
    *(_o(f"holder-{holder}", {"holder": holder}) for holder in ("dispatch", "trigger")),
    _o("holder-unknown", {"holder": "view"}),
    _o("holder-principal-name", {"holder": "door-tui"}),
    _o("holder-upper", {"holder": "TUI"}),
    _o("holder-null", {"holder": None}),
    _o("holder-empty", {"holder": ""}),
    _o("holder-number", {"holder": 5}),
    _o("holder-list", {"holder": ["tui"]}),
    _o("force-true", {"force": True}),
    _o("force-false", {"force": False}),
    _o("force-null", {"force": None}),
    _o("force-text-true", {"force": "true"}),
    _o("force-one", {"force": 1}),
    _o("force-list", {"force": [True]}),
    _o("intent-acquire", {"intent": "acquire"}),
    _o("intent-renew", {"intent": "renew"}),
    _o("intent-unknown", {"intent": "release"}),
    _o("intent-upper", {"intent": "Renew"}),
    _o("intent-null", {"intent": None}),
    _o("intent-empty", {"intent": ""}),
    _o("intent-number", {"intent": 5}),
    _o("holder-and-intent-unknown", {"holder": "view", "intent": "release"}),
    _half_pair(_o("half-pair-in-holder", {"holder": f"tui{_HALF}"})),
    _half_pair(_o("half-pair-in-intent", {"intent": f"renew{_HALF}"})),
)

STEER_BODIES: Final[tuple[Body, ...]] = (
    _o("message", {"message": "Check the garage too."}),
    _o("unknown-field", {"message": "Stop.", "turn": TURN}),
    _o("message-missing", {}),
    _o("message-null", {"message": None}),
    _o("message-empty", {"message": ""}),
    _o("message-number", {"message": 5}),
    _o("message-list", {"message": ["Stop."]}),
    _o("message-any-text", {"message": 'caf\u00e9 "quoted" \\ \u2028 \U0001f600\n'}),
    _long("message-at-the-cap", '{"message":"', "a", STEER_BYTES, '"}'),
    _long("message-over-the-cap", '{"message":"', "a", STEER_BYTES + 1, '"}'),
    _o("message-two-byte-chars-at-the-cap", {"message": "\u00e9" * (STEER_BYTES // 2)}),
    _o("message-two-byte-chars-over-the-cap", {"message": "\u00e9" * (STEER_BYTES // 2) + "a"}),
    Body("no-body", b""),
    Body("top-array", b'["Stop."]'),
    _half_pair(_o("half-pair-in-message", {"message": f"Check the garage{_HALF}"})),
)

STOP_BODIES: Final[tuple[Body, ...]] = (
    _o("reason", {"reason": "user_stopped"}),
    _o("reason-any-text", {"reason": "tui_takeover: Not A Reason\n"}),
    _o("empty-object", {}),
    _o("unknown-field", {"turn": TURN}),
    _o("reason-null", {"reason": None}),
    _o("reason-empty", {"reason": ""}),
    _o("reason-number", {"reason": 5}),
    _o("reason-list", {"reason": ["user_stopped"]}),
    _o("reason-200-chars", {"reason": "r" * 200}),
    _o("reason-201-chars", {"reason": "r" * 201}),
    _o("reason-200-two-byte-chars", {"reason": "\u00e9" * 200}),
    Body("no-body", b""),
    Body("top-text", b'"user_stopped"'),
    _half_pair(_o("half-pair-in-reason", {"reason": f"tui_takeover{_HALF}"})),
)

# --- the dispatch door (§13.4) and the delegate door (contract 04 §7.3) -------------

_DISPATCH: Final[dict[str, object]] = {
    "caller_family": "chat",
    "target_family": "scrum-lead",
    "delegation_id": DELEGATION,
    "message": "Triage the open issues.",
}


def _dispatch(body_id: str, **fields: object) -> Body:
    return _o(body_id, {**_DISPATCH, **fields})


def _without(body_id: str, base: dict[str, object], name: str) -> Body:
    return _o(body_id, {key: value for key, value in base.items() if key != name})


def _message_body(
    body_id: str, base: dict[str, object], count: int, fill: str = "m", last: str = ""
) -> Body:
    """`base` with a message of `count` times `fill`, then `last`."""
    head = _json({key: value for key, value in base.items() if key != "message"}).decode()

    return _long(body_id, head[:-1] + ',"message":"', fill, count, last + '"}')


_DOOR_BODIES: Final[tuple[tuple[str, dict[str, object]], ...]] = (
    ("caller-number", {"caller_family": 5}),
    ("caller-empty", {"caller_family": ""}),
    ("caller-upper", {"caller_family": "Chat"}),
    ("caller-trailing-newline", {"caller_family": "chat\n"}),
    ("target-null", {"target_family": None}),
    ("target-upper", {"target_family": "Scrum-Lead"}),
    ("target-one-char", {"target_family": "s"}),
    ("caller-and-target-bad", {"caller_family": "Chat", "target_family": "Scrum"}),
    ("same-caller-and-target", {"caller_family": "chat", "target_family": "chat"}),
    ("delegation-number", {"delegation_id": 5}),
    ("delegation-empty", {"delegation_id": ""}),
    ("delegation-lower", {"delegation_id": DELEGATION.lower()}),
    ("delegation-25-chars", {"delegation_id": DELEGATION[:-1]}),
    ("delegation-letter-u", {"delegation_id": "U" + DELEGATION[1:]}),
    ("delegation-trailing-newline", {"delegation_id": DELEGATION + "\n"}),
    ("message-null", {"message": None}),
    ("message-empty", {"message": ""}),
    ("message-number", {"message": 5}),
    ("message-one-byte", {"message": "m"}),
    ("message-any-text", {"message": 'caf\u00e9 "quoted" \\ \u2028 \U0001f600\n'}),
    ("claimed-session", {"claimed_session_id": "owui-8f1c2e"}),
    ("claimed-null", {"claimed_session_id": None}),
    ("claimed-empty", {"claimed_session_id": ""}),
    ("claimed-number", {"claimed_session_id": 5}),
    ("claimed-two-dots", {"claimed_session_id": ".."}),
    ("claimed-slash", {"claimed_session_id": "owui-a/b"}),
    ("claimed-129-chars", {"claimed_session_id": "s" * 129}),
    ("target-bad-and-delegation-bad", {"target_family": "Scrum", "delegation_id": "x"}),
    ("delegation-bad-and-message-empty", {"delegation_id": "x", "message": ""}),
    ("message-empty-and-claimed-bad", {"message": "", "claimed_session_id": ".."}),
    ("unknown-field", {"trigger": {"kind": "timer"}, "session": SESSION}),
)


def _door_bodies(base: dict[str, object]) -> tuple[Body, ...]:
    """The fields that `/dispatch` and `/delegate` share."""
    return (
        _o("minimal", base),
        _without("caller-missing", base, "caller_family"),
        _without("target-missing", base, "target_family"),
        _without("delegation-missing", base, "delegation_id"),
        _without("message-missing", base, "message"),
        *(_o(body_id, {**base, **fields}) for body_id, fields in _DOOR_BODIES),
        _message_body("message-at-the-cap", base, MESSAGE_BYTES),
        _message_body("message-over-the-cap", base, MESSAGE_BYTES + 1),
        _message_body(
            "message-two-byte-chars-over-the-cap", base, MESSAGE_BYTES // 2, "\u00e9", "a"
        ),
        Body("no-body", b""),
        Body("top-array", b"[]"),
        _half_pair(_o("half-pair-in-caller", {**base, "caller_family": f"chat{_HALF}"})),
        _half_pair(_o("half-pair-in-target", {**base, "target_family": f"chat{_HALF}"})),
        _half_pair(_o("half-pair-in-delegation", {**base, "delegation_id": _HALF})),
        _half_pair(_o("half-pair-in-message", {**base, "message": f"Triage{_HALF}"})),
        _half_pair(_o("half-pair-in-claimed", {**base, "claimed_session_id": f"owui-{_HALF}"})),
    )


DISPATCH_BODIES: Final[tuple[Body, ...]] = (
    _dispatch(
        "full",
        chain=["chat", "scrum-lead"],
        claimed_session_id="owui-8f1c2e",
        idempotency_key="morning-triage-2026-10-07",
    ),
    *_door_bodies(_DISPATCH),
    _dispatch("chain-empty", chain=[]),
    _dispatch("chain-null", chain=None),
    _dispatch("chain-one", chain=["scrum-lead"]),
    _dispatch("chain-8", chain=["chat"] * 8),
    _dispatch("chain-9", chain=["chat"] * 9),
    _dispatch("chain-text", chain="chat"),
    _dispatch("chain-object", chain={"0": "chat"}),
    _dispatch("chain-number", chain=5),
    _dispatch("chain-bad-name", chain=["chat", "Scrum"]),
    _dispatch("chain-number-entry", chain=["chat", 5]),
    _dispatch("chain-31-char-name", chain=["a" * 31]),
    _dispatch("chain-32-char-name", chain=["a" * 32]),
    _dispatch("chain-other-families", chain=["issue-worker", "vault-oracle"]),
    _dispatch("key-null", idempotency_key=None),
    _dispatch("key-empty", idempotency_key=""),
    _dispatch("key-number", idempotency_key=5),
    _dispatch("key-128-chars", idempotency_key="k" * 128),
    _dispatch("key-129-chars", idempotency_key="k" * 129),
    _dispatch("key-128-two-byte-chars", idempotency_key="\u00e9" * 128),
    _dispatch("claimed-bad-and-chain-bad", claimed_session_id="..", chain=["Scrum"]),
    _dispatch("chain-bad-and-key-long", chain=["Scrum"], idempotency_key="k" * 129),
    _half_pair(_dispatch("half-pair-in-key", idempotency_key=f"morning-{_HALF}")),
)

_DELEGATE: Final[dict[str, object]] = {
    "caller_family": "chat",
    "target_family": "vault-oracle",
    "delegation_id": DELEGATION,
    "message": "What did the sensor read?",
}

DELEGATE_BODIES: Final[tuple[Body, ...]] = (
    _o("full", {**_DELEGATE, "claimed_session_id": SESSION}),
    *_door_bodies(_DELEGATE),
    _o("chain-ignored", {**_DELEGATE, "chain": "not a chain", "idempotency_key": "k" * 500}),
)

_JOBS: Final[dict[str, object]] = {"caller_family": "scrum-lead"}


def _jobs(body_id: str, **fields: object) -> Body:
    return _o(body_id, {**_JOBS, **fields})


JOBS_BODIES: Final[tuple[Body, ...]] = (
    _jobs("full", session=JOB_SESSION, since="2026-10-07T05:00:00Z", limit=20),
    _jobs("minimal"),
    _jobs("unknown-field", family="issue-worker", cursor="x"),
    _o("caller-missing", {"session": JOB_SESSION}),
    _jobs("caller-null", caller_family=None),
    _jobs("caller-number", caller_family=5),
    _jobs("caller-empty", caller_family=""),
    _jobs("caller-upper", caller_family="Scrum-Lead"),
    _jobs("session-null", session=None),
    _jobs("session-empty", session=""),
    _jobs("session-number", session=5),
    _jobs("session-two-dots", session=".."),
    _jobs("session-129-chars", session="s" * 129),
    _jobs("session-any-prefix", session="owui-8f1c2e"),
    _jobs("since-null", since=None),
    _jobs("since-empty", since=""),
    _jobs("since-number", since=1789797600),
    *(_jobs(f"since-{name}", since=text) for name, text in TIMES),
    _jobs("limit-1", limit=1),
    _jobs("limit-200", limit=200),
    _jobs("limit-0", limit=0),
    _jobs("limit-201", limit=201),
    _jobs("limit-negative", limit=-1),
    _jobs("limit-null", limit=None),
    _jobs("limit-text", limit="20"),
    _jobs("limit-true", limit=True),
    _jobs("limit-float-whole", limit=20.0),
    _jobs("limit-past-64-bits", limit=2**70),
    _jobs("caller-bad-and-session-bad", caller_family="Scrum", session=".."),
    _jobs("session-bad-and-since-bad", session="..", since="never"),
    _jobs("since-bad-and-limit-bad", since="never", limit=0),
    Body("no-body", b""),
    Body("top-array", b"[]"),
    _half_pair(_jobs("half-pair-in-caller", caller_family=f"chat{_HALF}")),
    _half_pair(_jobs("half-pair-in-session", session=f"auto-{_HALF}")),
    _half_pair(_jobs("half-pair-in-since", since=f"2026-10-07T05:00:00Z{_HALF}")),
)

# --- the sandbox switch (contract 05 §5.1) -----------------------------------------

_SWITCH: Final[dict[str, object]] = {"family": FAMILY, "to": "chat-s3", "mode": "drain"}


def _switch(body_id: str, **fields: object) -> Body:
    return _o(body_id, {**_SWITCH, **fields})


SWITCH_BODIES: Final[tuple[Body, ...]] = (
    _switch("full", reason="definition_changed", deadline_s=120, **{"from": SANDBOX}),
    _switch("minimal"),
    _switch("unknown-field", outgoing=SANDBOX, epoch=3),
    _without("family-missing", _SWITCH, "family"),
    _switch("family-empty", family=""),
    _switch("family-number", family=5),
    _switch("family-any-text", family="Not A Family\n"),
    _without("to-missing", _SWITCH, "to"),
    _switch("to-empty", to=""),
    _switch("to-number", to=3),
    _switch("to-any-text", to="Not A Sandbox\n"),
    _switch("to-other-family", to="scrum-lead-s1"),
    _without("mode-missing", _SWITCH, "mode"),
    _switch("mode-interrupt", mode="interrupt"),
    _switch("mode-unknown", mode="replace"),
    _switch("mode-upper", mode="Drain"),
    _switch("mode-empty", mode=""),
    _switch("mode-number", mode=1),
    _switch("reason-null", reason=None),
    _switch("reason-empty", reason=""),
    _switch("reason-number", reason=5),
    _switch("reason-any-text", reason="Not A Reason\n" * 40),
    _switch("from-null", **{"from": None}),
    _switch("from-empty", **{"from": ""}),
    _switch("from-number", **{"from": 2}),
    _switch("from-any-text", **{"from": "Not A Sandbox\n"}),
    _switch("deadline-1", deadline_s=1),
    _switch("deadline-3600", deadline_s=3600),
    _switch("deadline-0", deadline_s=0),
    _switch("deadline-3601", deadline_s=3601),
    _switch("deadline-null", deadline_s=None),
    _switch("deadline-text", deadline_s="120"),
    _switch("deadline-true", deadline_s=True),
    _switch("deadline-float-whole", deadline_s=120.0),
    _switch("deadline-past-64-bits", deadline_s=2**70),
    _switch("family-missing-and-mode-unknown", family=None, mode="replace"),
    _switch("mode-unknown-and-deadline-bad", mode="replace", deadline_s=0),
    Body("no-body", b""),
    Body("top-array", b"[]"),
    _half_pair(_switch("half-pair-in-family", family=f"chat{_HALF}")),
    _half_pair(_switch("half-pair-in-to", to=f"chat-s{_HALF}")),
    _half_pair(_switch("half-pair-in-mode", mode=f"drain{_HALF}")),
    _half_pair(_switch("half-pair-in-reason", reason=f"definition_changed{_HALF}")),
    _half_pair(_switch("half-pair-in-from", **{"from": f"chat-s{_HALF}"})),
)

# --- the three queries (§5.2, §5.3, §5.5) ------------------------------------------


@dataclass(frozen=True)
class Query:
    """The parameters of one query. A parameter that is None is not sent."""

    id: str
    args: dict[str, str | None] = field(default_factory=dict[str, str | None])


def _q(query_id: str, **args: str | None) -> Query:
    return Query(query_id, args)


#: One text per form of a number in a query. `attendance.api` gives each
#: one to `int`.
_NUMBER_FORMS: Final[tuple[tuple[str, str], ...]] = (
    ("empty", ""),
    ("word", "ten"),
    ("spaces-around", " 5 "),
    ("tab-and-newline-around", "\t5\n"),
    ("vertical-tab-and-form-feed-around", "\x0b5\x0c"),
    ("no-break-space-around", "\u00a05\u00a0"),
    ("separator-char-around", "\x1c5\x1f"),
    ("space-inside", "1 0"),
    ("plus-sign", "+5"),
    ("plus-and-space", "+ 5"),
    ("two-signs", "+-5"),
    ("underscore", "1_0"),
    ("underscore-at-the-end", "10_"),
    ("two-underscores", "1__0"),
    ("zero-first", "007"),
    ("float", "5.0"),
    ("exponent", "1e1"),
    ("hex", "0x10"),
    ("arabic-digits", "\u0661\u0660"),
    ("fullwidth-digit", "\uff15"),
    ("superscript-digit", "\u00b2"),
)

LIST_QUERIES: Final[tuple[Query, ...]] = (
    _q("none"),
    _q("full", family="chat", state="running", kind="attended", limit="50", cursor="b3BhcXVl"),
    _q("unknown-parameter", order="asc"),
    _q("family-empty", family=""),
    _q("family-upper", family="Chat"),
    _q("family-one-char", family="c"),
    _q("family-trailing-newline", family="chat\n"),
    *(
        _q(f"state-{state}", state=state)
        for state in ("idle", "queued", "running", "waiting-approval", "failed")
    ),
    _q("state-of-a-turn", state="settled"),
    _q("state-underscore", state="waiting_approval"),
    _q("state-upper", state="Running"),
    _q("state-empty", state=""),
    *(_q(f"kind-{kind}", kind=kind) for kind in ("attended", "thin", "autonomous")),
    _q("kind-unknown", kind="robot"),
    _q("kind-empty", kind=""),
    _q("limit-1", limit="1"),
    _q("limit-200", limit="200"),
    _q("limit-0", limit="0"),
    _q("limit-201", limit="201"),
    _q("limit-negative", limit="-1"),
    _q("limit-minus-zero", limit="-0"),
    *(_q(f"limit-{name}", limit=text) for name, text in _NUMBER_FORMS),
    _q("limit-30-digits", limit="9" * 30),
    _q("limit-huge", limit="9" * HUGE_DIGITS),
    _q("cursor-empty", cursor=""),
    _q("cursor-any-text", cursor="Not A Cursor\n&limit=0#\u00e9"),
    _q("family-bad-and-state-bad", family="Chat", state="Running"),
    _q("state-bad-and-kind-bad", state="Running", kind="robot"),
    _q("kind-bad-and-limit-bad", kind="robot", limit="0"),
    _q("limit-word-and-family-bad", family="Chat", limit="ten"),
)

GET_QUERIES: Final[tuple[Query, ...]] = (
    _q("none"),
    _q("turns-10", turns="10"),
    _q("turns-0", turns="0"),
    _q("turns-100", turns="100"),
    _q("turns-101", turns="101"),
    _q("turns-negative", turns="-1"),
    *(_q(f"turns-{name}", turns=text) for name, text in _NUMBER_FORMS),
    _q("turns-huge", turns="9" * HUGE_DIGITS),
)

EVENTS_QUERIES: Final[tuple[Query, ...]] = (
    _q("none"),
    _q("full", from_seq="41", turn=TURN, follow="true"),
    _q("from-seq-0", from_seq="0"),
    _q("from-seq-negative", from_seq="-1"),
    _q("from-seq-minus-zero", from_seq="-0"),
    _q("from-seq-64-bits", from_seq=str(2**64 - 1)),
    _q("from-seq-past-64-bits", from_seq=str(2**64)),
    _q("from-seq-negative-past-64-bits", from_seq=str(-(2**70))),
    _q("from-seq-4300-digits", from_seq="9" * INT_DIGITS_MAX),
    _q("from-seq-huge", from_seq="9" * HUGE_DIGITS),
    *(_q(f"from-seq-{name}", from_seq=text) for name, text in _NUMBER_FORMS),
    _q("turn-empty", turn=""),
    _q("turn-lower", turn=TURN.lower()),
    _q("turn-any-text", turn="Not A Turn\n"),
    *(
        _q(f"follow-{name}", follow=text)
        for name, text in (
            ("false", "false"),
            ("zero", "0"),
            ("no", "no"),
            ("upper-false", "FALSE"),
            ("spaces-around-no", " No\t"),
            ("no-break-space-around-zero", "\u00a00\u00a0"),
            ("true", "true"),
            ("one", "1"),
            ("yes", "yes"),
            ("empty", ""),
            ("off", "off"),
            ("letter-f", "f"),
            ("any-text", "Not A Flag"),
            ("fullwidth-no", "\uff2e\uff2f"),
            ("zero-zero", "00"),
        )
    ),
    _q("from-seq-bad-and-follow", from_seq="ten", follow="false"),
)

# --- the error body (§14) --------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """The arguments of one `ApiError`."""

    id: str
    code: ErrorCode
    message: str
    family: str | None = None
    session: str | None = None
    turn: str | None = None
    detail: dict[str, Any] | None = None
    #: `detail` is the holder block of contract 02 §7.2, with its keys in
    #: the order of `attendance.leases`. Each other detail has its keys in
    #: the order that `attendance` writes them.
    holder_block: bool = False


_HOLDER_BLOCK: Final[dict[str, Any]] = {
    "holder": "owui",
    "since": "2026-10-05T19:21:47Z",
    "expires_at": "2026-10-05T19:22:47Z",
    "turn": TURN,
}

REFUSALS: Final[tuple[Refusal, ...]] = (
    *(
        Refusal(f"code-{code.value}", code, f"a refusal with the code {code.value}")
        for code in ErrorCode
    ),
    Refusal(
        "bad-request-of-a-parser",
        ErrorCode.BAD_REQUEST,
        "session is not a session id",
        family=FAMILY,
        session="..",
    ),
    Refusal(
        "session-busy-holder-block",
        ErrorCode.SESSION_BUSY,
        "another door holds the writer lease",
        family=FAMILY,
        session=SESSION,
        detail=_HOLDER_BLOCK,
        holder_block=True,
    ),
    Refusal(
        "session-busy-second-turn",
        ErrorCode.SESSION_BUSY,
        "this session already runs a turn",
        family=FAMILY,
        session=SESSION,
        detail={"turn": TURN},
    ),
    Refusal(
        "lease-taken-over",
        ErrorCode.LEASE_TAKEN_OVER,
        "another door took this idle lease",
        family=FAMILY,
        session=SESSION,
        detail={**_HOLDER_BLOCK, "holder": "tui", "turn": None},
        holder_block=True,
    ),
    Refusal(
        "lease-taken-over-no-holder",
        ErrorCode.LEASE_TAKEN_OVER,
        "another door took this idle lease",
        family=FAMILY,
        session=SESSION,
        detail={},
    ),
    Refusal(
        "sandbox-unavailable",
        ErrorCode.SANDBOX_UNAVAILABLE,
        "no sandbox of the family is ready",
        family=FAMILY,
        session=SESSION,
        detail={"family_state": "reconciling"},
    ),
    Refusal(
        "family-degraded",
        ErrorCode.FAMILY_DEGRADED,
        "family chat has a fault that blocks turns",
        family=FAMILY,
        detail={"family_state": "degraded", "fault": "sandbox_unreachable"},
    ),
    Refusal(
        "queue-full",
        ErrorCode.QUEUE_FULL,
        "the queue of family scrum-lead is full",
        family="scrum-lead",
        detail={"max_queued_turns": 100},
    ),
    Refusal(
        "turn-not-found",
        ErrorCode.TURN_NOT_FOUND,
        "no such turn in this session",
        family=FAMILY,
        session=SESSION,
        turn=TURN,
    ),
    Refusal(
        "forbidden", ErrorCode.FORBIDDEN, "no", detail={"principal": "door-owui", "kind": "thin"}
    ),
    Refusal(
        "forbidden-prefix",
        ErrorCode.FORBIDDEN,
        "no",
        family=FAMILY,
        session=OTHER_SESSION,
        detail={"principal": "door-owui", "expected_prefix": "owui-"},
    ),
    Refusal(
        "idempotency-mismatch-other-family",
        ErrorCode.IDEMPOTENCY_MISMATCH,
        "the key is in use",
        family="scrum-lead",
        detail={"session": JOB_SESSION, "family": "chat"},
    ),
    Refusal(
        "sandbox-unavailable-handshake",
        ErrorCode.SANDBOX_UNAVAILABLE,
        "the sandbox gave no answer",
        family=FAMILY,
        detail={"sandbox": SANDBOX, "message": "the channel closed"},
    ),
    Refusal(
        "detail-keys-not-sorted",
        ErrorCode.INTERNAL,
        "x",
        detail={"z": 1, "a": {"b": 1, "c": [2]}, "m": None},
    ),
    Refusal(
        "text-forms",
        ErrorCode.BAD_REQUEST,
        'caf\u00e9 "quoted" \\ / \u2028 \u2029 \U0001f600 \x7f\x00\x1f\n\t</script>',
        family="Not A Family\n",
        session="\u00e9",
        turn="not a turn",
    ),
    Refusal("empty-texts", ErrorCode.INTERNAL, "", family="", session="", turn=""),
    Refusal(
        "detail-forms",
        ErrorCode.INTERNAL,
        "x",
        detail={
            "empty": {},
            "float": 0.5,
            "list": [1, "two", None, True, [], {"a": 1}],
            "negative": -1,
            "nested": {"a": {"b": {"c": "caf\u00e9"}}},
            "null": None,
            "u64": 2**64 - 1,
        },
    ),
    Refusal("detail-long-float", ErrorCode.INTERNAL, "x", detail={"cost_usd": LONG_FLOAT}),
)

# --- one journal line to the record a replay gives (§8) ---------------------------------

_TS: Final = "2026-10-05T19:22:05.118Z"


def _line(**fields: object) -> bytes:
    return _json({"journal_seq": 42, "ts": _TS, "kind": "note", "turn": None, "body": {}, **fields})


def _l(line_id: str, **fields: object) -> Body:
    return Body(line_id, _line(**fields))


_LINE_TEXT: Final = _line().decode("utf-8")
_LINE_HEAD: Final = _LINE_TEXT[:-1]

_USAGE: Final[dict[str, Any]] = {
    "input": 4120,
    "output": 188,
    "cache_read": 0,
    "cache_write": 0,
    "cost_usd": 0.014,
}

_SESSION_OBJECT: Final[dict[str, Any]] = {
    "family": FAMILY,
    "session": SESSION,
    "kind": "attended",
    "title": "Kitchen sensor debug",
    "state": "idle",
    "created_at": "2026-10-05T19:21:47Z",
    "updated_at": "2026-10-05T19:21:47Z",
    "journal_seq": 0,
    "writer": None,
    "turns_total": 0,
    "terminal_total": 0,
    "turns_running": 0,
    "sandbox": None,
    "persona_hash": None,
    "labels": {"door": "owui"},
}

#: The body of each kind, with the keys in the order that
#: `attendance.service` writes them. The body of a `pi_event` has its keys in
#: sorted order: `session.py` says why.
KIND_BODIES: Final[dict[LineKind, dict[str, Any]]] = {
    LineKind.SESSION_CREATED: _SESSION_OBJECT,
    LineKind.SESSION_TITLED: {"title": "Kitchen sensor debug"},
    LineKind.WRITER_CHANGED: {"holder": "tui", "reason": "taken_over"},
    LineKind.TURN_QUEUED: {
        "prompt": "Triage the open issues.",
        "idempotency_key": "morning-triage-2026-10-07",
        "queue_depth": 3,
    },
    LineKind.TURN_STARTED: {
        "prompt": "Which sensor dropped out last night?",
        "sandbox": SANDBOX,
        "deadline_s": 3600,
        "persona_hash": DIGEST,
        "status_stale": False,
    },
    LineKind.PI_EVENT: {
        "assistantMessageEvent": {"contentIndex": 0, "delta": "Sensor ", "type": "text_delta"},
        "type": "message_update",
    },
    LineKind.APPROVAL_REQUESTED: {
        "tool": "ha_call",
        "summary": "",
        "gate_id": "0123456789abcdef",
    },
    LineKind.APPROVAL_RESOLVED: {"decision": "approval_denied", "waited_s": 12},
    LineKind.TURN_SETTLED: {"usage": _USAGE, "leaf_id": "e6", "user_entry_id": "e5"},
    LineKind.TURN_FAILED: {"reason": "model_error", "message": "the model gave no answer"},
    LineKind.TURN_ABORTED: {"reason": "user_stopped", "message": "tui_takeover"},
    LineKind.BRANCH_FALLBACK: {"wanted_entry": None, "reason": "unmapped_parent"},
    LineKind.TERMINAL_EXCHANGE: {
        "entry_id": "e6",
        "parent_entry_id": "e5",
        "prompt": "what did the sensor read",
        "answer": "21.4 degrees at 09:12",
    },
    LineKind.NOTE: {"note": "persona_truncated", "cap_bytes": 16384},
    LineKind.HEARTBEAT: {"last_seq": 41},
}

#: The kinds that carry a turn id.
TURN_KINDS: Final = frozenset(
    {
        LineKind.TURN_QUEUED,
        LineKind.TURN_STARTED,
        LineKind.PI_EVENT,
        LineKind.APPROVAL_REQUESTED,
        LineKind.APPROVAL_RESOLVED,
        LineKind.TURN_SETTLED,
        LineKind.TURN_FAILED,
        LineKind.TURN_ABORTED,
        LineKind.BRANCH_FALLBACK,
    }
)


def _kind_line(kind: LineKind) -> Body:
    turn = TURN if kind in TURN_KINDS else None

    return _l(f"kind-{kind.value}", kind=kind.value, turn=turn, body=KIND_BODIES[kind])


JOURNAL_LINES: Final[tuple[Body, ...]] = (
    *(_kind_line(kind) for kind in LineKind),
    _l("unknown-field", session=SESSION, family=FAMILY),
    # --- journal_seq ---
    _l("seq-1", journal_seq=1),
    _l("seq-0", journal_seq=0),
    _l("seq-negative", journal_seq=-1),
    _l("seq-null", journal_seq=None),
    _l("seq-text", journal_seq="42"),
    _l("seq-true", journal_seq=True),
    _l("seq-false", journal_seq=False),
    _l("seq-float-whole", journal_seq=42.0),
    _l("seq-list", journal_seq=[42]),
    _l("seq-64-bits", journal_seq=2**64 - 1),
    _l("seq-past-64-bits", journal_seq=2**64),
    Body("seq-missing", _json({"ts": _TS, "kind": "note", "turn": None, "body": {}})),
    Body("seq-exponent", _LINE_TEXT.replace('"journal_seq":42', '"journal_seq":4e1').encode()),
    Body("seq-minus-zero", _LINE_TEXT.replace('"journal_seq":42', '"journal_seq":-0').encode()),
    Body(
        "seq-twice",
        _LINE_TEXT.replace('"journal_seq":42', '"journal_seq":0,"journal_seq":42').encode(),
    ),
    # --- kind ---
    _l("kind-unknown", kind="stream_overrun"),
    _l("kind-upper", kind="Note"),
    _l("kind-trailing-space", kind="note "),
    _l("kind-empty", kind=""),
    _l("kind-null", kind=None),
    _l("kind-number", kind=5),
    _l("kind-list", kind=["note"]),
    Body("kind-missing", _json({"journal_seq": 42, "ts": _TS, "turn": None, "body": {}})),
    # --- turn ---
    _l("turn-ulid", turn=TURN),
    _l("turn-lower", turn=TURN.lower()),
    _l("turn-any-text", turn="Not A Turn\n"),
    _l("turn-empty", turn=""),
    _l("turn-number", turn=5),
    _l("turn-list", turn=[TURN]),
    Body("turn-missing", _json({"journal_seq": 42, "ts": _TS, "kind": "note", "body": {}})),
    # --- ts ---
    _l("ts-seconds", ts="2026-10-05T19:22:05Z"),
    _l("ts-microseconds", ts="2026-10-05T19:22:05.118999Z"),
    _l("ts-offset", ts="2026-10-05T21:22:05.118+02:00"),
    _l("ts-no-zone", ts="2026-10-05T19:22:05.118"),
    _l("ts-date-only", ts="2026-10-05"),
    _l("ts-space-before-offset", ts="2026-10-05 21:22:05 +0200"),
    _l("ts-week-date", ts="2026-W41-1T19:22:05Z"),
    _l("ts-words", ts="just now"),
    _l("ts-offset-moves-before-year-one", ts="0001-01-01T00:00:00+00:01"),
    _l("ts-empty", ts=""),
    _l("ts-null", ts=None),
    _l("ts-number", ts=1789759325),
    _l("ts-list", ts=[_TS]),
    Body("ts-missing", _json({"journal_seq": 42, "kind": "note", "turn": None, "body": {}})),
    # --- body ---
    _l("body-null", body=None),
    _l("body-list", body=[1, 2]),
    _l("body-text", body="note"),
    _l("body-number", body=5),
    _l("body-nested", body={"a": {"b": [1, 2.5, "c", None, True]}, "d": {}}),
    _l("body-text-forms", body={"text": 'caf\u00e9 "quoted" \\ / \u2028 \U0001f600 \x7f\n\t'}),
    _l("body-number-forms", body={"a": 1.0, "b": 0.1, "c": -1, "d": 2**64 - 1, "e": 1e100}),
    _l("body-past-64-bits", body={"a": 2**64}),
    Body("body-missing", _json({"journal_seq": 42, "ts": _TS, "kind": "note", "turn": None})),
    Body(
        "body-duplicate-key",
        _LINE_HEAD.replace('"body":{}', '"body":{"a":1,"a":2}').encode() + b"}",
    ),
    Body("body-nan", _LINE_TEXT.replace('"body":{}', '"body":{"a":NaN}').encode()),
    Body("body-lone-surrogate", _LINE_TEXT.replace('"body":{}', '"body":{"a":"\\ud800"}').encode()),
    Body(
        "body-deep-64",
        _LINE_TEXT.replace('"body":{}', '"body":{"a":' + "[" * 63 + "]" * 63 + "}").encode(),
    ),
    Body(
        "body-deep-128",
        _LINE_TEXT.replace('"body":{}', '"body":{"a":' + "[" * 126 + "]" * 126 + "}").encode(),
    ),
    Body(
        "body-deep-129",
        _LINE_TEXT.replace('"body":{}', '"body":{"a":' + "[" * 127 + "]" * 127 + "}").encode(),
    ),
    Body(
        "body-very-deep",
        parts=(
            (_LINE_TEXT.split('"body":{}', maxsplit=1)[0] + '"body":{"a":', 1),
            ("[", VERY_DEEP),
            ("]", VERY_DEEP),
            ("}}", 1),
        ),
    ),
    _long("body-integer-4300-digits", _LINE_HEAD + ',"x":', "9", INT_DIGITS_MAX, "}"),
    _long("body-integer-4301-digits", _LINE_HEAD + ',"x":', "9", INT_DIGITS_MAX + 1, "}"),
    # --- the line as bytes ---
    Body("line-empty", b""),
    Body("line-spaces", b"  "),
    Body("line-text", b"not json"),
    Body("line-truncated", _LINE_TEXT[:-1].encode()),
    Body("line-trailing-text", _LINE_TEXT.encode() + b" x"),
    Body("line-spaces-around", b" " + _LINE_TEXT.encode() + b" \r"),
    Body("line-top-array", b"[" + _LINE_TEXT.encode() + b"]"),
    Body("line-top-null", b"null"),
    Body("line-top-text", b'"note"'),
    Body("line-empty-object", b"{}"),
    Body("line-bom", b"\xef\xbb\xbf" + _LINE_TEXT.encode()),
    Body("line-utf16", _LINE_TEXT.encode("utf-16-le")),
    Body("line-not-utf8", _LINE_TEXT.replace('"body":{}', '"body":{"a":"\xff"}').encode("latin-1")),
)


@dataclass(frozen=True)
class Append:
    """The arguments of one `Journal.append`, and the count of lines before it."""

    id: str
    kind: LineKind
    body: dict[str, Any]
    turn: str | None = None
    moment: str = "2026-10-05T19:22:05.118000+00:00"
    last_seq: int = 41
    #: The contract leaves the body free. `session.py` makes sure that such a
    #: body has its keys in sorted order at each level.
    free: bool = False


def _kind_append(kind: LineKind) -> Append:
    turn = TURN if kind in TURN_KINDS else None

    return Append(
        f"kind-{kind.value}", kind, KIND_BODIES[kind], turn, free=kind is LineKind.PI_EVENT
    )


def _free_note(append_id: str, **body: object) -> Append:
    """A note of a form that the service does not write."""
    return Append(append_id, LineKind.NOTE, dict(body), free=True)


def _note(append_id: str, word: str, turn: str | None = None, **fields: object) -> Append:
    """A note of the service: `note` first, then each field in the order given."""
    return Append(append_id, LineKind.NOTE, {"note": word, **fields}, turn)


def _event(append_id: str, **fields: object) -> Append:
    body = {**fields, "type": "message_update"}

    return Append(append_id, LineKind.PI_EVENT, dict(sorted(body.items())), TURN, free=True)


def _settled(append_id: str, **usage: object) -> Append:
    body = {"usage": {**_USAGE, **usage}, "leaf_id": "e6", "user_entry_id": "e5"}

    return Append(append_id, LineKind.TURN_SETTLED, body, TURN)


_NESTED: Final[dict[str, Any]] = {"b": {"c": [[], {}, [{"d": None}]]}}

#: One text with each kind of character that the writer of a line escapes.
_TEXT_FORMS: Final = (
    'caf\u00e9 "quoted" \\ / \u2028 \u2029 \U0001f600 \x7f\x00\x1f\n\r\t\x08\x0c</script>'
)

APPENDS: Final[tuple[Append, ...]] = (
    *(_kind_append(kind) for kind in LineKind if kind is not LineKind.HEARTBEAT),
    # --- the sequence number and the time ---
    Append("seq-first", LineKind.NOTE, {"note": "x"}, last_seq=0),
    Append("seq-64-bits", LineKind.NOTE, {"note": "x"}, last_seq=2**64 - 2),
    Append("time-whole-second", LineKind.NOTE, {"note": "x"}, moment="2026-10-05T19:22:05+00:00"),
    Append(
        "time-microseconds-cut",
        LineKind.NOTE,
        {"note": "x"},
        moment="2026-10-05T19:22:05.118999+00:00",
    ),
    Append(
        "time-offset",
        LineKind.NOTE,
        {"note": "x"},
        moment="2026-10-05T21:22:05.118000+02:00",
    ),
    Append(
        "time-offset-over-a-year-end",
        LineKind.NOTE,
        {"note": "x"},
        moment="2026-12-31T23:30:00.001000-01:00",
    ),
    Append("time-year-1000", LineKind.NOTE, {"note": "x"}, moment="1000-01-01T00:00:00+00:00"),
    Append(
        "time-year-9999", LineKind.NOTE, {"note": "x"}, moment="9999-12-31T23:59:59.999999+00:00"
    ),
    Append(
        "time-before-the-epoch",
        LineKind.NOTE,
        {"note": "x"},
        moment="1969-12-31T23:59:59.999000+00:00",
    ),
    # --- the lines that the service writes, by kind ---
    Append(
        "session-created-with-a-writer",
        LineKind.SESSION_CREATED,
        {
            **_SESSION_OBJECT,
            "kind": "autonomous",
            "title": "",
            "state": "running",
            "journal_seq": 7,
            "writer": {
                "holder": "trigger",
                "door_instance": "door-trigger",
                "since": "2026-10-05T19:21:47Z",
                "expires_at": "2026-10-05T19:22:47Z",
                "turn": TURN,
            },
            "turns_total": 3,
            "terminal_total": 1,
            "turns_running": 1,
            "sandbox": SANDBOX,
            "persona_hash": DIGEST,
            "labels": {},
        },
    ),
    *(
        Append(
            f"writer-changed-{holder}-{reason}",
            LineKind.WRITER_CHANGED,
            {"holder": holder, "reason": reason},
        )
        for holder, reason in (
            ("owui", "granted"),
            ("tui", "renewed"),
            ("delegate", "released"),
            ("dispatch", "granted"),
            ("trigger", "taken_over"),
        )
    ),
    Append(
        "turn-queued-no-key",
        LineKind.TURN_QUEUED,
        {"prompt": "x", "idempotency_key": None, "queue_depth": 1},
        TURN,
    ),
    Append(
        "turn-started-no-persona-stale",
        LineKind.TURN_STARTED,
        {
            "prompt": "x",
            "sandbox": "scrum-lead-s12",
            "deadline_s": 1,
            "persona_hash": None,
            "status_stale": True,
        },
        TURN,
    ),
    Append(
        "turn-started-text-forms",
        LineKind.TURN_STARTED,
        {
            "prompt": _TEXT_FORMS,
            "sandbox": SANDBOX,
            "deadline_s": 3600,
            "persona_hash": DIGEST,
            "status_stale": False,
        },
        TURN,
    ),
    *(
        Append(
            f"approval-resolved-{decision}",
            LineKind.APPROVAL_RESOLVED,
            {"decision": decision, "waited_s": waited},
            TURN,
        )
        for decision, waited in (
            ("approved", 0),
            ("approval_timeout", 900),
            ("approval_undeliverable", 1),
            ("approval_revoked", 30),
            ("approval_abandoned", 2),
        )
    ),
    Append(
        "approval-requested-call-name",
        LineKind.APPROVAL_REQUESTED,
        {"tool": "kagi__kagi_extract", "summary": "", "gate_id": "fedcba9876543210"},
        TURN,
    ),
    Append(
        "turn-settled-no-entries",
        LineKind.TURN_SETTLED,
        {"usage": _USAGE, "leaf_id": None, "user_entry_id": None},
        TURN,
    ),
    *(
        _settled(f"turn-settled-cost-{name}", cost_usd=cost)
        for name, cost in (
            ("zero", 0.0),
            ("one", 1.0),
            ("tenth", 0.1),
            ("sum-of-tenths", 0.1 + 0.2),
            ("small", 0.0001),
            ("smaller", 0.00001),
            ("tiny", 1.5e-7),
            ("sixteen-digits", 1234567890123456.0),
            ("seventeen-digits", 12345678901234567.0),
            ("large", 1e16),
            ("huge", 1.7976931348623157e308),
            ("least", 5e-324),
            ("long", 123456.789012345),
            ("third", 1 / 3),
            ("whole", 100.0),
            ("e22", 1e22),
            ("e21", 1e21),
            ("near-e16", 9999999999999998.0),
            # The exact value ends in .25. The two shortest texts end in 2
            # and in 3, and each is as near. Python writes the even digit.
            ("between-two-digits", 1315490761899226.25),
            ("between-two-other-digits", 146218358399002.625),
            ("long-fraction", LONG_FLOAT),
        )
    ),
    _settled("turn-settled-large-counts", input=2**53, output=2**63, cache_read=2**64 - 1),
    *(
        Append(
            f"turn-failed-{reason.value}",
            LineKind.TURN_FAILED,
            {"reason": reason.value, "message": f"the turn failed with {reason.value}"},
            TURN,
        )
        for reason in TurnReason
    ),
    Append("turn-failed-no-message", LineKind.TURN_FAILED, {"reason": "channel_lost"}, TURN),
    Append("turn-aborted-no-message", LineKind.TURN_ABORTED, {"reason": "queue_lost"}, TURN),
    Append(
        "turn-aborted-permission-removed",
        LineKind.TURN_ABORTED,
        {"reason": "permission_removed", "message": "a removal took the grant"},
        TURN,
    ),
    Append(
        "branch-fallback-fork-refused",
        LineKind.BRANCH_FALLBACK,
        {"wanted_entry": "e4", "reason": "pi did not find the entry"},
        TURN,
    ),
    # --- the notes that the service writes ---
    _note(
        "note-sandbox-switched",
        "sandbox_switched",
        **{"from": SANDBOX},
        to="chat-s3",
        mode="drain",
        reason="definition_changed",
    ),
    _note(
        "note-sandbox-switched-no-outgoing",
        "sandbox_switched",
        **{"from": None},
        to="chat-s3",
        mode="interrupt",
        reason="",
    ),
    _note("note-persona-truncated", "persona_truncated", TURN, cap_bytes=16384),
    _note("note-unexpected-playpen-reason", "unexpected_playpen_reason", TURN, reason="retried"),
    _note("note-process-exit", "process_exit", reason="killed", code=137),
    _note("note-process-exit-no-code", "process_exit", reason="idle", code=None),
    _note("note-playpen-log", "playpen_log", level="warn", message="pi wrote to stderr"),
    # --- a note of another form: the contract leaves the body free ---
    _free_note("note-any-body", a={"b": [1, 2.5, "c", None, True]}, z=""),
    _free_note("note-other-word", count=3, note="written_by_hand"),
    _free_note("note-word-and-other-fields", extra=1, note="process_exit"),
    _free_note("note-word-is-a-number", note=5),
    _free_note("note-empty-body"),
    # --- a pi event, which the host does not change ---
    _event("pi-event-text-forms", delta='caf\u00e9 "quoted" \\ / \u2028 \U0001f600 \x7f\x00\n'),
    _event("pi-event-key-forms", **{"": 1, "caf\u00e9": 2, "A": 3, "a": 4, "_": 5, "1": 6}),
    _event("pi-event-number-forms", a=1.0, b=0.1, c=-1, d=2**64 - 1, e=1e100, f=-0.0, g=2**63),
    _event("pi-event-past-64-bits", a=2**64, b=-(2**63) - 1),
    _event("pi-event-nested", a=_NESTED),
    _event("pi-event-capped", original_bytes=300000, truncated=True),
    _event("pi-event-long-float", a=LONG_FLOAT),
)


@dataclass(frozen=True)
class Record:
    """One line of the event stream, as `attendance.api` is given it."""

    id: str
    journal_seq: int | None
    kind: LineKind
    body: dict[str, Any]
    turn: str | None = None
    ts: str = "2026-10-05T19:22:31.070000+00:00"


RECORDS: Final[tuple[Record, ...]] = (
    Record("heartbeat", None, LineKind.HEARTBEAT, {"last_seq": 118}),
    Record("heartbeat-empty-journal", None, LineKind.HEARTBEAT, {"last_seq": 0}),
    Record("stream-overrun", None, LineKind.NOTE, {"note": "stream_overrun", "last_seq": 118}),
    Record("line", 42, LineKind.PI_EVENT, KIND_BODIES[LineKind.PI_EVENT], TURN),
    Record(
        "line-text-forms",
        43,
        LineKind.NOTE,
        {"note": 'caf\u00e9 "quoted" \\ \u2028 \U0001f600 \x7f\n'},
    ),
    Record(
        "line-turn-settled", 63, LineKind.TURN_SETTLED, KIND_BODIES[LineKind.TURN_SETTLED], TURN
    ),
    Record("line-long-float", 64, LineKind.PI_EVENT, {"a": LONG_FLOAT, "type": "x"}, TURN),
)

# --- what the stream itself makes (§5.5, §8.1) ------------------------------------------


@dataclass(frozen=True)
class Live:
    """One stream: the lines on disk, where the reader starts, and what occurs."""

    id: str
    #: `heartbeat`: no line arrives. `overrun`: more lines arrive than the
    #: reader has room for.
    event: str
    lines_on_disk: int = 0
    from_seq: int = 0


LIVES: Final[tuple[Live, ...]] = (
    Live("heartbeat-empty-journal", "heartbeat"),
    Live("heartbeat-after-a-replay", "heartbeat", lines_on_disk=3),
    Live("heartbeat-from-the-end", "heartbeat", lines_on_disk=3, from_seq=3),
    Live("heartbeat-from-past-the-end", "heartbeat", lines_on_disk=3, from_seq=7),
    Live("overrun-empty-journal", "overrun"),
    Live("overrun-after-a-replay", "overrun", lines_on_disk=3),
)

# --- the turn states (§4.3) and the session state (§4.1) ---------------------------------

MOVES: Final[tuple[tuple[TurnState, TurnState], ...]] = tuple(
    (current, wanted) for current in TurnState for wanted in TurnState
)


@dataclass(frozen=True)
class Derive:
    """The states of the turns of one session, oldest first, and the sticky flag."""

    id: str
    turns: tuple[TurnState, ...]
    last_settled_failed: bool = False


DERIVES: Final[tuple[Derive, ...]] = (
    Derive("no-turn", ()),
    Derive("no-turn-last-failed", (), last_settled_failed=True),
    *(Derive(f"one-{state.value}", (state,)) for state in TurnState),
    *(
        Derive(f"one-{state.value}-last-failed", (state,), last_settled_failed=True)
        for state in TurnState
    ),
    Derive("running-and-waiting", (TurnState.RUNNING, TurnState.WAITING_APPROVAL)),
    Derive("waiting-and-running", (TurnState.WAITING_APPROVAL, TurnState.RUNNING)),
    Derive("queued-and-running", (TurnState.QUEUED, TurnState.RUNNING)),
    Derive("queued-and-waiting", (TurnState.QUEUED, TurnState.WAITING_APPROVAL)),
    Derive("failed-and-queued", (TurnState.FAILED, TurnState.QUEUED)),
    Derive("failed-and-settled", (TurnState.FAILED, TurnState.SETTLED)),
    Derive("settled-and-failed", (TurnState.SETTLED, TurnState.FAILED), last_settled_failed=True),
    Derive(
        "aborted-and-queued-last-failed",
        (TurnState.ABORTED, TurnState.QUEUED),
        last_settled_failed=True,
    ),
)
