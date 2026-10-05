"""The written inputs of the noticeboard surfaces (the group `noticeboard`).

`noticeboard.py` holds the surfaces. This module holds what each one is
given: header values, form bodies, queries, path segments, answers of
`attendance`, audit files, validation reports, env files and environments.
Every input is text written here. None names a deployment.

Two rules bound the inputs:

1. No input makes an entry point raise (`vectors/AGENTS.md`, rule 5).
2. No JSON text here is one that `json.loads` takes and a strict reader
   refuses: no `NaN`, no integer outside 64 bits, no key two times, no byte
   order mark. "Known gaps" of `vectors/AGENTS.md` lists what that leaves out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from attendance.models import LineKind

from noticeboard import auditfiles, jsonfiles, sessions, statusdocs, transcript
from vectors.surfaces.config import LAN, VIEW_KEY
from vectors.surfaces.ids import ARABIC_ONE, NAME_CASES, SESSION_CASES
from vectors.surfaces.session_cases import (
    DIGEST,
    FAMILY,
    JOB_SESSION,
    KIND_BODIES,
    OTHER_SESSION,
    SANDBOX,
    SESSION,
    TURN,
    TURN_KINDS,
    Body,
)

#: A second turn id, for a stream with two turns.
OTHER_TURN: Final = "01JBQ7X2M5C8E1G4J7N0Q3S6V9"

#: The largest integer that a strict JSON reader takes as a signed number.
I64_MAX: Final = 2**63 - 1

#: The count of characters that the noticeboard keeps of one text.
CUT: Final = jsonfiles.MAX_TEXT_CHARS

_TS: Final = "2026-10-05T19:22:31.070Z"
_EARLIER: Final = "2026-10-05T19:21:47Z"


def _json(value: object) -> bytes:
    """One compact JSON text. It holds no token that a strict reader refuses."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def _o(body_id: str, value: object) -> Body:
    """An input that is one JSON text."""
    return Body(body_id, _json(value))


def _t(body_id: str, text: str) -> Body:
    """An input that is the UTF-8 bytes of a text."""
    return Body(body_id, text.encode("utf-8"))


def _long(body_id: str, *parts: tuple[str, int]) -> Body:
    """A long input, written as repeated parts."""
    return Body(body_id, parts=parts)


# --- the access key (spec.md §8.3 rule 1) ----------------------------------------------

_K: Final = VIEW_KEY.encode("ascii")

#: A key of 32 bytes with each character outside ASCII.
KEY_OUTSIDE: Final = "\u00e9" * 16

#: A key that only a loopback bind permits.
SHORT_KEY: Final = "short"


@dataclass(frozen=True)
class KeyCase:
    """One `X-View-Key` header against the key of the service."""

    id: str
    #: The bytes of the header value. None stands for a request with no header.
    offered: bytes | None
    #: The key of the service. An empty text is a loopback service with no key.
    expected: str = VIEW_KEY


KEYS: Final[tuple[KeyCase, ...]] = (
    KeyCase("exact", _K),
    # --- no key in the request ---
    KeyCase("no-header", None),
    KeyCase("empty", b""),
    # --- another key ---
    KeyCase("wrong-last-character", _K[:-1] + b"X"),
    KeyCase("one-more-character", _K + b"X"),
    KeyCase("one-character-short", _K[:-1]),
    KeyCase("upper-case", _K.upper()),
    KeyCase("the-key-two-times", _K + _K),
    # --- the two ends ---
    KeyCase("space-at-the-end", _K + b" "),
    KeyCase("space-at-the-start", b" " + _K),
    KeyCase("tab-at-the-end", _K + b"\t"),
    KeyCase("byte-a0-at-the-end", _K + b"\xa0"),
    # --- a short key, which a loopback bind permits ---
    KeyCase("short-key", SHORT_KEY.encode("ascii"), SHORT_KEY),
    KeyCase("short-key-wrong", b"shorT", SHORT_KEY),
    KeyCase("short-key-no-header", None, SHORT_KEY),
    # --- a key with a character outside ASCII ---
    KeyCase("outside-ascii-as-latin1", KEY_OUTSIDE.encode("latin-1"), KEY_OUTSIDE),
    KeyCase("outside-ascii-as-utf8", KEY_OUTSIDE.encode("utf-8"), KEY_OUTSIDE),
    # --- a loopback service with no key ---
    KeyCase("no-key-no-header", None, ""),
    KeyCase("no-key-empty-header", b"", ""),
    KeyCase("no-key-with-a-header", _K, ""),
)

# --- the form guard (spec.md §8.3 rule 3) -----------------------------------------------

#: A token of the form that the service mints: 43 characters. It is the
#: token of no deployment.
FORM_TOKEN: Final = "vectors-form-token-0123456789abcdefghijklmn"

#: The host of the service, as a browser names it. The name is reserved for
#: documents.
HOST: Final = "view.example"
ORIGIN: Final = f"https://{HOST}"
_PORT_HOST: Final = f"{HOST}:8443"
_LOOPBACK_HOST: Final = "[::1]:8370"
_LAN_HOST: Final = f"{LAN}:8370"


@dataclass(frozen=True)
class CsrfCase:
    """One form post, as the five texts that the check takes."""

    id: str
    cookie: str | None = FORM_TOKEN
    field: str | None = FORM_TOKEN
    origin: str | None = ORIGIN
    referer: str | None = None
    host: str | None = HOST


CSRF: Final[tuple[CsrfCase, ...]] = (
    CsrfCase("same-origin"),
    CsrfCase("referer-only", origin=None, referer=f"{ORIGIN}/families/chat/edit"),
    CsrfCase("origin-and-referer", referer=f"{ORIGIN}/families/chat/edit"),
    # --- the token ---
    CsrfCase("no-cookie", cookie=None),
    CsrfCase("empty-cookie", cookie=""),
    CsrfCase("no-field", field=None),
    CsrfCase("empty-field", field=""),
    CsrfCase("no-cookie-no-field", cookie=None, field=None),
    CsrfCase("no-cookie-foreign-origin", cookie=None, origin="https://other.example"),
    CsrfCase("another-token", field=FORM_TOKEN[:-1] + "X"),
    CsrfCase("field-one-character-short", field=FORM_TOKEN[:-1]),
    CsrfCase("field-one-more-character", field=FORM_TOKEN + "n"),
    CsrfCase("field-upper-case", field=FORM_TOKEN.upper()),
    CsrfCase("field-space-at-the-end", field=FORM_TOKEN + " "),
    CsrfCase("field-outside-ascii", field="caf\u00e9"),
    CsrfCase("field-outside-the-bmp", field="\U0001f600"),
    CsrfCase("another-token-foreign-origin", field="other", origin="https://other.example"),
    CsrfCase("one-character-token", cookie="a", field="a"),
    # --- no sender ---
    CsrfCase("no-origin-no-referer", origin=None),
    CsrfCase("empty-origin-empty-referer", origin="", referer=""),
    CsrfCase("no-host", host=None),
    CsrfCase("empty-host", host=""),
    # --- which header states the sender ---
    CsrfCase("foreign-origin", origin="https://other.example"),
    CsrfCase("foreign-referer", origin=None, referer="https://other.example/page"),
    CsrfCase("foreign-origin-own-referer", origin="https://other.example", referer=ORIGIN),
    CsrfCase("own-origin-foreign-referer", referer="https://other.example/page"),
    CsrfCase("empty-origin-own-referer", origin="", referer=f"{ORIGIN}/"),
    CsrfCase("origin-null", origin="null"),
    CsrfCase("origin-null-own-referer", origin="null", referer=ORIGIN),
    # --- the host and the port ---
    CsrfCase("port-on-both", origin=f"https://{_PORT_HOST}", host=_PORT_HOST),
    CsrfCase("port-in-the-origin-only", origin=f"https://{_PORT_HOST}"),
    CsrfCase("port-in-the-host-only", host=_PORT_HOST),
    CsrfCase("default-port-in-the-origin", origin=f"https://{HOST}:443"),
    CsrfCase("another-port", origin=f"https://{HOST}:8444", host=_PORT_HOST),
    CsrfCase("host-upper-case-in-the-origin", origin="https://VIEW.example"),
    CsrfCase("host-upper-case-on-both", origin="https://VIEW.example", host="VIEW.example"),
    CsrfCase("a-name-below-the-host", origin=f"https://a.{HOST}"),
    CsrfCase("a-name-that-ends-with-the-host", origin=f"https://other-{HOST}"),
    CsrfCase("the-host-and-a-dot", origin=f"https://{HOST}."),
    CsrfCase("an-ipv6-address", origin=f"http://{_LOOPBACK_HOST}", host=_LOOPBACK_HOST),
    CsrfCase("an-ipv4-address", origin=f"http://{_LAN_HOST}", host=_LAN_HOST),
    # --- the form of the URL ---
    CsrfCase("scheme-http", origin=f"http://{HOST}"),
    CsrfCase("scheme-upper-case", origin=f"HTTPS://{HOST}"),
    CsrfCase("no-scheme", origin=HOST),
    CsrfCase("no-scheme-with-a-port", origin=_PORT_HOST, host=_PORT_HOST),
    CsrfCase("two-slashes-and-no-scheme", origin=f"//{HOST}"),
    CsrfCase("scheme-and-no-slashes", origin=f"https:{HOST}"),
    CsrfCase("scheme-and-three-slashes", origin=f"https:///{HOST}"),
    CsrfCase("scheme-alone", origin="https://"),
    CsrfCase("scheme-that-starts-with-a-digit", origin=f"1https://{HOST}"),
    CsrfCase("user-before-the-host", origin=f"https://user@{HOST}"),
    CsrfCase("host-as-the-user", origin=f"https://{HOST}@other.example"),
    CsrfCase("origin-with-a-path", origin=f"{ORIGIN}/audit"),
    CsrfCase("origin-with-a-query", origin=f"{ORIGIN}?next=1"),
    CsrfCase("origin-with-a-fragment", origin=f"{ORIGIN}#top"),
    CsrfCase("origin-with-a-final-slash", origin=f"{ORIGIN}/"),
    CsrfCase(
        "referer-with-a-path-and-a-query",
        origin=None,
        referer=f"{ORIGIN}/audit?family=chat&offset=50#top",
    ),
    CsrfCase("origin-with-a-tab-inside", origin="https://view\t.example"),
)

# --- the paths that need no key (spec.md §8.3 rule 1) -----------------------------------

KEYLESS: Final[tuple[tuple[str, str], ...]] = (
    ("healthz", "/healthz"),
    ("healthz-final-slash", "/healthz/"),
    ("healthz-and-a-segment", "/healthz/x"),
    ("healthz-and-more-letters", "/healthzfoo"),
    ("healthz-upper-case", "/HEALTHZ"),
    ("healthz-short", "/health"),
    ("healthz-no-slash", "healthz"),
    ("healthz-two-slashes", "//healthz"),
    ("healthz-after-a-segment", "/x/healthz"),
    ("healthz-space-at-the-start", " /healthz"),
    ("static", "/static"),
    ("static-final-slash", "/static/"),
    ("static-stylesheet", "/static/noticeboard.css"),
    ("static-and-two-dots", "/static/../audit"),
    ("static-and-more-letters", "/staticfoo"),
    ("static-plural", "/statics/noticeboard.css"),
    ("static-upper-case", "/STATIC/noticeboard.css"),
    ("static-line-feed-at-the-end", "/static\n"),
    ("static-short", "/stat"),
    ("home", "/"),
    ("empty", ""),
    ("audit", "/audit"),
    ("family", "/families/chat"),
    ("edit", "/families/chat/edit"),
    ("session", "/sessions/chat/owui-x"),
)

# --- the cookie of the form guard -------------------------------------------------------

_T: Final = FORM_TOKEN.encode("ascii")
_NAME: Final = b"view_csrf="

#: Each character that the cookie writer of Python puts between no quotes,
#: beside the letters and the digits.
PLAIN_MARKS: Final = "!#$%&'*+-.^_`|~:"

#: The most bytes that the cookie of the form guard is to have.
COOKIE_BYTES: Final = 512


@dataclass(frozen=True)
class CookieCase:
    """One `Cookie` header."""

    id: str
    #: The bytes of the header value. None stands for a request with no header.
    raw: bytes | None


COOKIES: Final[tuple[CookieCase, ...]] = (
    CookieCase("token", _NAME + _T),
    # --- no token in the request ---
    CookieCase("no-header", None),
    CookieCase("empty-header", b""),
    CookieCase("empty-value", _NAME),
    CookieCase("name-alone", b"view_csrf"),
    CookieCase("another-cookie-alone", b"session=abc"),
    CookieCase("quoted-empty-value", _NAME + b'""'),
    # --- the name ---
    CookieCase("name-upper-case", b"VIEW_CSRF=" + _T),
    CookieCase("name-with-more-letters", b"view_csrf2=" + _T),
    CookieCase("name-after-more-letters", b"xview_csrf=" + _T),
    # --- more than one cookie ---
    CookieCase("after-another-cookie", b"session=abc; " + _NAME + _T),
    CookieCase("before-another-cookie", _NAME + _T + b"; session=abc"),
    CookieCase("no-space-after-the-semicolon", b"session=abc;" + _NAME + _T),
    CookieCase("semicolon-at-the-end", _NAME + _T + b";"),
    CookieCase("comma-between-two-cookies", b"session=abc, " + _NAME + _T),
    CookieCase("two-values", _NAME + b"first; " + _NAME + b"second"),
    CookieCase("two-values-last-empty", _NAME + _T + b"; " + _NAME),
    CookieCase("another-cookie-outside-ascii", b"session=caf\xc3\xa9; " + _NAME + _T),
    # --- the two ends of the name and of the value ---
    CookieCase(
        "spaces-around-the-name-and-the-value",
        b"session=abc;  view_csrf = " + _T + b" ; theme=dark",
    ),
    CookieCase(
        "tabs-around-the-name-and-the-value",
        b"session=abc;\tview_csrf\t=\t" + _T + b"\t; theme=dark",
    ),
    CookieCase("byte-a0-after-the-value", _NAME + _T + b"\xa0"),
    CookieCase("byte-a0-before-the-name", b"session=abc;\xa0" + _NAME + _T),
    # --- the characters of the value ---
    CookieCase("one-character", _NAME + b"a"),
    CookieCase("each-plain-mark", _NAME + PLAIN_MARKS.encode("ascii")),
    CookieCase("equals-inside", _NAME + b"a=b"),
    CookieCase("slash-inside", _NAME + b"a/b"),
    CookieCase("512-bytes", _NAME + b"a" * COOKIE_BYTES),
    # --- quotes around the value ---
    CookieCase("quoted", _NAME + b'"' + _T + b'"'),
    CookieCase("quoted-equals-inside", _NAME + b'"a=b"'),
    CookieCase("quoted-octal-escape", _NAME + b'"\\141bc"'),
)

# --- the form body ----------------------------------------------------------------------

FORMS: Final[tuple[Body, ...]] = (
    Body("empty", b""),
    Body("one-field", b"a=1"),
    Body("two-fields", b"a=1&b=2"),
    Body(
        "the-edit-form",
        b"csrf_token=" + _T + b"&description=the+house+assistant&shell=on&subject=&verb=save",
    ),
    # --- a piece with no name or no value ---
    Body("blank-value", b"a="),
    Body("no-equals", b"a"),
    Body("blank-name", b"=1"),
    Body("equals-alone", b"="),
    Body("ampersand-alone", b"&"),
    Body("empty-pieces", b"&&a=1&&b=2&"),
    # --- a name two times ---
    Body("name-two-times", b"a=1&a=2"),
    Body("name-two-times-last-blank", b"a=1&a="),
    Body("name-two-times-between-others", b"a=1&b=2&a=3"),
    # --- the separators ---
    Body("two-equals", b"a=b=c"),
    Body("semicolon", b"a=1;b=2"),
    Body("question-mark", b"?a=1"),
    Body("hash", b"a=1#b=2"),
    Body("spaces", b" a = 1 "),
    # --- the plus and the percent escape ---
    Body("plus", b"a=x+y&b+c=1"),
    Body("escaped-plus", b"a=x%2By"),
    Body("escaped-space", b"a=x%20y"),
    Body("escaped-ampersand-and-equals", b"a=x%26y%3Dz"),
    Body("escaped-name", b"%61=1"),
    Body("escape-lower-case", b"a=%c3%a9"),
    Body("escape-two-times", b"a=%2541"),
    Body("escape-not-hex", b"a=%zz"),
    Body("escape-one-digit", b"a=%4"),
    Body("percent-alone", b"a=%"),
    Body("percent-at-the-end", b"a=100%"),
    Body("escape-nul", b"a=x%00y"),
    Body("escape-line-feed", b"a=x%0Ay"),
    Body("escape-cr-lf", b"a=x%0D%0Ay"),
    Body("line-feed", b"a=x\ny"),
    # --- text outside ASCII ---
    Body("escape-utf8", b"a=caf%C3%A9"),
    Body("utf8", "a=caf\u00e9".encode()),
    Body("name-utf8", "caf\u00e9=1".encode()),
    Body("escape-outside-the-bmp", b"a=%F0%9F%98%80"),
    Body("line-separator", "a=x\u2028y".encode()),
    Body("escape-not-utf8", b"a=%FF"),
    Body("escape-half-a-character", b"a=%C3"),
    Body("escape-three-bytes-of-four", b"a=%F0%9F%98"),
    Body("escape-surrogate", b"a=%ED%A0%80"),
    Body("bytes-not-utf8", b"a=\xff"),
    Body("bytes-three-of-four", b"a=\xf0\x9f\x98"),
    Body("bytes-three-of-four-and-a-letter", b"a=\xf0\x9f\x98b"),
    Body("byte-and-then-escape", b"a=\xc3%A9"),
    Body("escape-and-then-byte", b"a=%C3\xa9"),
)

# --- the query of the audit page --------------------------------------------------------

#: The day file that the audit page of the query surface reads.
QUERY_DAY: Final = "2026-09-19.jsonl"

#: A family text of 200 characters. A filter value has that many at most.
LONG_FAMILY: Final = "f" * 200

_FACE: Final = "\U0001f600"


def _audit(**fields: object) -> dict[str, object]:
    """One audit record of contract 04 §6.1, with the fields of the caller."""
    record: dict[str, object] = {
        "ts": "2026-09-19T11:59:30Z",
        "family": FAMILY,
        "sandbox_id": SANDBOX,
        "sandbox_id_trusted": True,
        "grants_rev": "01K5J9QW3R7T0ZP4YB2H6N8M1D",
        "tool": "kagi__kagi_search_fetch",
        "args": {"query": "boiler service date"},
        "decision": "allow",
        "reason": "granted",
        "latency_ms": 38,
        "waited_ms": 0,
        "gate": None,
        "claimed": {"session_id": SESSION, "turn_id": TURN, "delegation_id": None},
        "chain": [FAMILY],
    }
    record.update(fields)

    return record


def _claimed(session: str) -> dict[str, object]:
    return {"session_id": session, "turn_id": TURN, "delegation_id": None}


#: The three records of the query surface, oldest first.
QUERY_RECORDS: Final[tuple[dict[str, object], ...]] = (
    _audit(ts="2026-09-19T08:00:00Z"),
    _audit(
        ts="2026-09-19T09:00:00Z",
        tool="gh__list_issues",
        decision="deny",
        reason="not_granted",
        claimed=_claimed(OTHER_SESSION),
    ),
    _audit(
        ts="2026-09-19T10:00:00Z",
        family="code",
        tool="kagi__kagi_summarize",
        claimed=_claimed("owui-9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"),
    ),
)

QUERY_FILE: Final = b"".join(_json(record) + b"\n" for record in QUERY_RECORDS)

#: Each query is ASCII: a client sends each other byte as a percent escape.
QUERIES: Final[tuple[Body, ...]] = (
    Body("no-query", b""),
    # --- each filter ---
    Body("family", b"family=chat"),
    Body("family-other", b"family=code"),
    Body("family-unknown", b"family=vault"),
    Body("family-upper-case", b"family=CHAT"),
    Body("family-part-of-a-name", b"family=cha"),
    Body("decision", b"decision=deny"),
    Body("decision-part-of-a-word", b"decision=den"),
    Body("tool-part-of-a-name", b"tool=kagi"),
    Body("tool-whole-name", b"tool=gh__list_issues"),
    Body("tool-unknown", b"tool=ha_call"),
    Body("session-part-of-an-id", b"session=owui-"),
    Body("session-whole-id", b"session=" + OTHER_SESSION.encode("ascii")),
    Body("two-filters", b"family=chat&decision=allow"),
    Body("four-filters", b"family=chat&session=owui-&tool=kagi&decision=allow"),
    Body("two-filters-no-record", b"family=code&decision=deny"),
    Body("unknown-name", b"sandbox=chat-s2"),
    Body("name-upper-case", b"FAMILY=code"),
    # --- the form of one value ---
    Body("blank-value", b"family="),
    Body("no-equals", b"family"),
    Body("value-two-times", b"family=chat&family=code"),
    Body("value-two-times-last-blank", b"family=code&family="),
    Body("plus-around-the-value", b"family=+chat+"),
    Body("escaped-spaces-around-the-value", b"family=%20chat%20"),
    Body("escaped-tab-and-line-feed-around-the-value", b"family=%09chat%0A"),
    Body("escaped-u001f-and-u00a0-around-the-value", b"family=%1Fchat%C2%A0"),
    Body("space-inside-the-value", b"tool=kagi+search"),
    Body("escaped-letter", b"family=ch%61t"),
    Body("escaped-name", b"f%61mily=code"),
    Body("escape-not-hex", b"family=%zz"),
    Body("escape-not-utf8", b"family=%FF"),
    Body("semicolon", b"family=chat;decision=deny"),
    Body("markup", b"tool=%3Cb%3E%22%27%26"),
    # --- the cut of one value ---
    Body("200-characters", b"family=" + LONG_FAMILY.encode("ascii")),
    Body("201-characters", b"family=" + LONG_FAMILY.encode("ascii") + b"x"),
    Body("space-and-201-characters", b"family=+" + LONG_FAMILY.encode("ascii") + b"x"),
    Body("201-characters-outside-the-bmp", b"session=" + b"%F0%9F%98%80" * 201),
    # --- the offset ---
    Body("offset-zero", b"offset=0"),
    Body("offset-one", b"offset=1"),
    Body("offset-two", b"offset=2"),
    Body("offset-three", b"offset=3"),
    Body("offset-past-the-end", b"offset=50"),
    Body("offset-negative", b"offset=-1"),
    Body("offset-plus-sign", b"offset=%2B1"),
    Body("offset-in-spaces", b"offset=+1+"),
    Body("offset-zeros-at-the-start", b"offset=001"),
    Body("offset-underscore", b"offset=1_0"),
    Body("offset-underscore-at-the-end", b"offset=1_"),
    Body("offset-blank", b"offset="),
    Body("offset-word", b"offset=two"),
    Body("offset-fraction", b"offset=1.0"),
    Body("offset-exponent", b"offset=1e0"),
    Body("offset-hex", b"offset=0x1"),
    Body("offset-30-digits", b"offset=123456789012345678901234567890"),
    Body("offset-negative-30-digits", b"offset=-123456789012345678901234567890"),
    _long("offset-4300-digits", ("offset=", 1), ("0", 4299), ("1", 1)),
    _long("offset-4301-digits", ("offset=", 1), ("0", 4300), ("1", 1)),
    Body("offset-two-times", b"offset=1&offset=2"),
    Body("offset-with-a-filter", b"family=chat&offset=1"),
)

# --- the route parameters ---------------------------------------------------------------

#: Each character that a path segment holds with no escape (RFC 3986, the
#: unreserved set).
_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def segment(text: str) -> str:
    """The text as one path segment: a percent escape for each byte that needs one."""
    return "".join(
        chr(byte) if chr(byte) in _UNRESERVED else f"%{byte:02X}" for byte in text.encode("utf-8")
    )


#: Texts that are no input of the id surfaces, as the route gets them.
_ROUTE_TEXTS: Final[tuple[tuple[str, str], ...]] = (
    ("not-a-name", "Not A Name"),
    ("final-line-feed", "chat\n"),
    ("final-arabic-digit", "cha" + ARABIC_ONE),
    ("nul-inside", "ch\x00at"),
    ("question-mark", "chat?"),
    ("hash", "chat#"),
)

#: Each family route parameter: an id and the segment as a client sends it.
SEGMENTS: Final[tuple[tuple[str, str], ...]] = (
    *((case_id, segment(text)) for case_id, text in NAME_CASES),
    *((case_id, segment(text)) for case_id, text in _ROUTE_TEXTS),
    # --- a segment that only its written form tells apart ---
    ("empty", ""),
    ("one-dot", "."),
    ("escaped-dots", "%2E%2E"),
    ("escaped-letter", "ch%61t"),
    ("escape-lower-case", "%63hat"),
    ("escape-not-utf8", "chat%FF"),
    ("escape-not-hex", "chat%zz"),
)

#: Each session id that the id surfaces of the session grammar get, and the
#: texts that the route must refuse.
SESSION_IDS: Final[tuple[tuple[str, str], ...]] = (
    *SESSION_CASES,
    ("empty", ""),
    ("not-an-id", "not an id"),
    ("final-line-feed", "owui-x\n"),
    ("final-cr", "owui-x\r"),
    ("space-at-the-start", " owui-x"),
    ("space-at-the-end", "owui-x "),
    ("nul-at-the-end", "owui-x\x00"),
    ("final-arabic-digit", "owui-" + ARABIC_ONE),
    ("each-door-prefix", "job-auto-tui-owui-1"),
)

# --- the answers of attendance (contract 02 §4, §5) -------------------------------------

_WRITER: Final[dict[str, object]] = {
    "holder": "owui",
    "door_instance": "owui-1",
    "since": _EARLIER,
    "expires_at": "2026-10-05T19:23:31Z",
    "turn": TURN,
}


def _session(**fields: object) -> dict[str, object]:
    """One session object of contract 02 §4.2, with the fields of the caller."""
    row: dict[str, object] = {
        "family": FAMILY,
        "session": SESSION,
        "kind": "attended",
        "title": "Kitchen sensor debug",
        "state": "idle",
        "created_at": _EARLIER,
        "updated_at": _TS,
        "journal_seq": 42,
        "writer": None,
        "turns_total": 3,
        "turns_running": 0,
        "sandbox": SANDBOX,
        "persona_hash": DIGEST,
        "labels": {"door": "owui"},
    }
    row.update(fields)

    return row


def _usage(**fields: object) -> dict[str, object]:
    usage: dict[str, object] = {
        "input": 4120,
        "output": 188,
        "cache_read": 12,
        "cache_write": 3,
        "cost_usd": 0.014,
    }
    usage.update(fields)

    return usage


def _turn(**fields: object) -> dict[str, object]:
    """One turn object of contract 02 §4.4, with the fields of the caller."""
    row: dict[str, object] = {
        "turn": TURN,
        "state": "settled",
        "reason": None,
        "started_at": _EARLIER,
        "ended_at": _TS,
        "deadline_s": 3600,
        "idempotency_key": None,
        "sandbox": SANDBOX,
        "usage": _usage(),
        "approvals": 0,
        "owui": None,
    }
    row.update(fields)

    return row


@dataclass(frozen=True)
class Answer:
    """One answer of `attendance`, and the arguments of the call that gets it."""

    body: Body
    status: int = 200
    #: The arguments of the reader that are not the default.
    call: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def id(self) -> str:
        return self.body.id


def _listed(body_id: str, *rows: object, **fields: object) -> Answer:
    """A list answer of contract 02 §5.2 that holds `rows`."""
    return Answer(_o(body_id, {"sessions": list(rows), "next_cursor": None, **fields}))


def _labels(count: int) -> dict[str, str]:
    return {f"key-{number:02d}": f"value-{number}" for number in range(count)}


_LONG_KEY: Final = "k" * CUT

#: A transport failure, as the HTTP module of the noticeboard words one.
UNREACHABLE: Final = "cannot reach attendance: ConnectError: the stand-in has no socket"

LISTS: Final[tuple[Answer, ...]] = (
    _listed("one-session", _session()),
    _listed("no-session"),
    _listed("two-sessions", _session(), _session(session=OTHER_SESSION, title="")),
    Answer(_o("empty-object", {})),
    # --- the arguments of the call ---
    Answer(_o("call-family", {"sessions": []}), call={"family": FAMILY}),
    Answer(_o("call-cursor", {"sessions": []}), call={"cursor": "b2Zmc2V0OjUw"}),
    Answer(_o("call-limit-1", {"sessions": []}), call={"limit": 1}),
    Answer(_o("call-limit-200", {"sessions": []}), call={"limit": sessions.MAX_SESSION_LIMIT}),
    Answer(_o("call-limit-500", {"sessions": []}), call={"limit": 500}),
    Answer(
        _o("call-each-argument", {"sessions": []}),
        call={"family": FAMILY, "limit": 25, "cursor": "b2Zmc2V0OjUw"},
    ),
    # --- the list and the cursor ---
    _listed("next-cursor", _session(), next_cursor="b2Zmc2V0OjUw"),
    _listed("next-cursor-number", next_cursor=5),
    _listed("next-cursor-600-characters", next_cursor="c" * 600),
    Answer(_o("sessions-null", {"sessions": None})),
    Answer(_o("sessions-object", {"sessions": {"0": _session()}})),
    Answer(_o("sessions-text", {"sessions": "none"})),
    _listed("rows-that-are-no-object", 5, "text", None, [_session()], _session(), True),
    Answer(
        _o(
            "201-rows",
            {
                "sessions": [
                    *([0] * (sessions.MAX_SESSION_LIMIT - 1)),
                    _session(session="owui-row-200"),
                    _session(session="owui-row-201"),
                ]
            },
        )
    ),
    # --- the door of a session ---
    _listed("door-owui", _session(session="owui-1")),
    _listed("door-tui", _session(session="tui-1")),
    _listed("door-delegate", _session(session=f"job-{TURN}")),
    _listed("door-trigger", _session(session=JOB_SESSION)),
    _listed("door-from-the-writer", _session(session=TURN, writer=_WRITER)),
    _listed("door-prefix-and-writer", _session(session="tui-1", writer=_WRITER)),
    _listed("door-none", _session(session=TURN)),
    _listed("door-prefix-upper-case", _session(session="OWUI-1")),
    _listed("door-prefix-with-no-hyphen", _session(session="owui1")),
    # --- each field of a session ---
    _listed("row-empty-object", {}),
    _listed(
        "wrong-types",
        _session(
            family=5,
            session=None,
            kind=["attended"],
            title={"text": "x"},
            state=True,
            created_at=1,
            updated_at=2.5,
            journal_seq="42",
            turns_total=True,
            turns_running=3.0,
            sandbox=0,
            persona_hash=False,
            writer="owui",
            labels=["door"],
        ),
    ),
    _listed("nulls", _session(sandbox=None, persona_hash=None, title=None, labels=None)),
    _listed("title-500-characters", _session(title="t" * CUT)),
    _listed("title-501-characters", _session(title="t" * (CUT + 1))),
    _listed("title-501-characters-outside-the-bmp", _session(title=_FACE * (CUT + 1))),
    _listed("title-text-forms", _session(title='caf\u00e9 "quoted" <b> & \u2028 \x7f\n')),
    _listed("session-501-characters", _session(session="s" * (CUT + 1))),
    _listed("counts-zero", _session(journal_seq=0, turns_total=0, turns_running=0)),
    _listed(
        "counts-at-the-64-bit-limit",
        _session(journal_seq=I64_MAX, turns_total=I64_MAX, turns_running=I64_MAX),
    ),
    _listed("unknown-field", _session(terminal_total=2, door="owui")),
    # --- the writer ---
    _listed("writer", _session(writer=_WRITER)),
    _listed("writer-empty-object", _session(writer={})),
    _listed("writer-list", _session(writer=[_WRITER])),
    _listed(
        "writer-wrong-types",
        _session(
            writer={"holder": 5, "door_instance": None, "since": True, "expires_at": [], "turn": {}}
        ),
    ),
    _listed("writer-turn-null", _session(writer={**_WRITER, "turn": None})),
    # --- the labels ---
    _listed("labels-empty", _session(labels={})),
    _listed("labels-20", _session(labels=_labels(sessions.MAX_LABELS))),
    _listed("labels-21", _session(labels=_labels(sessions.MAX_LABELS + 1))),
    _listed("labels-sorted", _session(labels={"b": "2", "a": "1", "B": "3", "_": "4", "1": "5"})),
    _listed("label-600-characters", _session(labels={"note": "v" * 600})),
    _listed("label-key-600-characters", _session(labels={"k" * 600: "value"})),
    _listed(
        "label-keys-equal-after-the-cut",
        _session(labels={_LONG_KEY + "a": "first", _LONG_KEY + "b": "second"}),
    ),
    _listed(
        "label-values-that-are-no-text",
        _session(labels={"a": 5, "b": None, "c": "kept", "d": ["x"], "e": {"k": "v"}, "f": True}),
    ),
    _listed(
        "21-labels-first-is-no-text",
        _session(labels={**_labels(sessions.MAX_LABELS + 1), "key-00": 5}),
    ),
    # --- a body that the reader refuses ---
    Answer(_t("not-json", "not json")),
    Answer(_t("cut-short", '{"sessions": [')),
    Answer(_t("empty", "")),
    Answer(Body("not-utf8", b'{"sessions": "\xff"}')),
    Answer(_t("list", "[]")),
    Answer(_t("text", '"sessions"')),
    Answer(_t("number", "5")),
    Answer(_t("null", "null")),
    Answer(
        _long(
            "at-the-size-cap", ('{"sessions":[]', 1), (" ", sessions.MAX_LIST_BYTES - 15), ("}", 1)
        )
    ),
    Answer(
        _long(
            "over-the-size-cap",
            ('{"sessions":[]', 1),
            (" ", sessions.MAX_LIST_BYTES - 14),
            ("}", 1),
        )
    ),
)

_TWO_TURNS: Final = (
    _turn(),
    _turn(turn=OTHER_TURN, state="running", ended_at=None, usage=None),
)


def _detail(body_id: str, *turns: object, **fields: object) -> Answer:
    """A detail answer of contract 02 §5.3: one session and `turns`."""
    return Answer(_o(body_id, {**_session(**fields), "turns": list(turns)}))


def _of_turn(body_id: str, **fields: object) -> Answer:
    """A detail answer with one turn that has the fields of the caller."""
    return _detail(body_id, _turn(**fields))


def _of_usage(body_id: str, **fields: object) -> Answer:
    return _of_turn(body_id, usage=_usage(**fields))


_NO_USAGE: Final = {key: value for key, value in _turn().items() if key != "usage"}

DETAILS: Final[tuple[Answer, ...]] = (
    _detail("session-and-two-turns", *_TWO_TURNS),
    _detail("no-turn"),
    Answer(_o("no-turns-key", _session())),
    Answer(_o("empty-object", {})),
    # --- the arguments of the call ---
    Answer(_o("call-turns-1", _session()), call={"turns": 1}),
    Answer(_o("call-turns-100", _session()), call={"turns": sessions.MAX_TURNS}),
    Answer(_o("call-turns-500", _session()), call={"turns": 500}),
    # --- the list of turns ---
    Answer(_o("turns-null", {**_session(), "turns": None})),
    Answer(_o("turns-object", {**_session(), "turns": {"0": _turn()}})),
    _detail("turns-that-are-no-object", 5, "text", None, [_turn()], _turn(), False),
    _detail(
        "101-turns",
        *([0] * (sessions.MAX_TURNS - 1)),
        _turn(turn="turn-100"),
        _turn(turn="turn-101"),
    ),
    # --- the session of the answer ---
    _detail("session-writer", _turn(), writer=_WRITER),
    _detail("session-wrong-types", _turn(), family=5, session=None, journal_seq="42", labels=5),
    # --- each state of a turn ---
    *(
        _of_turn(f"state-{state}", state=state)
        for state in (
            "queued",
            "running",
            "waiting-approval",
            "settled",
            "failed",
            "aborted",
            "exploded",
        )
    ),
    _of_turn("state-empty", state=""),
    _of_turn("state-upper-case", state="Running"),
    _of_turn("state-number", state=1),
    # --- each field of a turn ---
    _detail("turn-empty-object", {}),
    _of_turn(
        "turn-wrong-types",
        turn=5,
        state=None,
        reason=["none"],
        started_at=1,
        ended_at=False,
        deadline_s="3600",
        sandbox={},
        approvals=1.0,
        usage="none",
        persona_truncated="true",
    ),
    _of_turn("reason-text", state="failed", reason="model_error"),
    _of_turn("deadline-true", deadline_s=True),
    _of_turn("approvals-two", approvals=2),
    _of_turn("persona-truncated", persona_truncated=True),
    _of_turn("persona-truncated-false", persona_truncated=False),
    _of_turn("persona-truncated-one", persona_truncated=1),
    _of_turn("persona-truncated-text", persona_truncated="true"),
    # --- the usage ---
    _detail("no-usage", _NO_USAGE),
    _of_turn("usage-null", usage=None),
    _of_turn("usage-empty-object", usage={}),
    _of_turn("usage-list", usage=[4120, 188]),
    _of_usage("usage-wrong-types", input="5", output=True, cache_read=1.5, cache_write=None),
    _of_usage("usage-zeros", input=0, output=0, cache_read=0, cache_write=0, cost_usd=0.0),
    _of_usage(
        "usage-sum-past-64-bits", input=2**62, output=2**62, cache_read=2**62, cache_write=2**62
    ),
    _of_usage(
        "usage-each-count-at-the-64-bit-limit",
        input=I64_MAX,
        output=I64_MAX,
        cache_read=I64_MAX,
        cache_write=I64_MAX,
    ),
    _of_usage("cost-null", cost_usd=None),
    _of_usage("cost-integer", cost_usd=2),
    _of_usage("cost-zero", cost_usd=0),
    _of_usage("cost-true", cost_usd=True),
    _of_usage("cost-text", cost_usd="0.014"),
    _of_usage("cost-small", cost_usd=0.00004),
    _of_usage("cost-large", cost_usd=1e22),
    # --- a body that the reader refuses ---
    Answer(_t("not-json", "not json")),
    Answer(_t("empty", "")),
    Answer(_t("list", "[]")),
    Answer(_t("null", "null")),
)


def line(seq: object, kind: object, body: object = None, turn: object = None) -> bytes:
    """One line of the event stream of contract 02 §8, with its LF."""
    record: dict[str, object] = {"journal_seq": seq, "ts": _TS, "kind": kind, "turn": turn}
    if body is not None:
        record["body"] = body

    return _json(record) + b"\n"


def _kind_line(seq: int, kind: LineKind) -> bytes:
    """A line of one kind, with the body that `attendance` writes for that kind."""
    turn = TURN if kind in TURN_KINDS else None

    return line(seq, kind.value, KIND_BODIES[kind], turn)


def _delta(seq: int, text: object, turn: str = TURN) -> bytes:
    """One text delta of the answer of a turn (contract 02 §15.1)."""
    body = {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": text},
    }

    return line(seq, LineKind.PI_EVENT.value, body, turn)


_STARTED: Final = line(1, "turn_started", {"prompt": "Which sensor dropped out?"}, TURN)
_SETTLED: Final = line(4, "turn_settled", {}, TURN)
_NOTE: Final = line(7, "note", {"note": "persona_truncated", "cap_bytes": 16384})
_HEARTBEAT: Final = line(None, "heartbeat", {"last_seq": 7})

#: One exchange: a prompt, an answer in two parts, and the end of the turn.
EXCHANGE: Final = _STARTED + _delta(2, "Sensor ") + _delta(3, "3 dropped out.") + _SETTLED

_LINES_CAP: Final = sessions.MAX_STREAM_LINES
_BYTES_CAP: Final = sessions.MAX_STREAM_BYTES


def _stream(body_id: str, raw: bytes, **call: object) -> Answer:
    return Answer(Body(body_id, raw), call=dict(call))


STREAMS: Final[tuple[Answer, ...]] = (
    _stream("one-exchange", EXCHANGE),
    _stream("no-line", b""),
    _stream("each-kind", b"".join(_kind_line(seq, kind) for seq, kind in enumerate(LineKind, 1))),
    _stream("heartbeat", _STARTED + _HEARTBEAT),
    # --- the arguments of the call ---
    _stream("call-from-seq-0", _NOTE, from_seq=0),
    _stream("call-from-seq-42", _NOTE, from_seq=42),
    _stream("call-from-seq-at-the-64-bit-limit", _NOTE, from_seq=I64_MAX),
    # --- where a line ends ---
    _stream("no-final-line-feed", _STARTED + _SETTLED[:-1]),
    _stream("blank-lines", b"\n\n" + _STARTED + b"   \n\t\n\r\n" + _SETTLED + b"\n"),
    _stream("cr-lf", _STARTED[:-1] + b"\r\n" + _SETTLED[:-1] + b"\r\n"),
    _stream("line-in-spaces", b"  " + _NOTE[:-1] + b"  \n"),
    _stream("line-of-ascii-whitespace", _STARTED + b" \t\r\x0b\x0c\n" + _SETTLED),
    _stream("line-of-u00a0", _STARTED + "\u00a0\n".encode() + _SETTLED),
    _stream("line-separator-inside-a-text", line(1, "note", {"note": "a\u2028b\u2029c\u0085d"})),
    _stream("two-objects-on-one-line", _NOTE[:-1] + _NOTE),
    # --- a line that the reader does not keep ---
    _stream("line-not-json", _STARTED + b"not json\n" + _SETTLED),
    _stream("line-cut-short", _STARTED + _SETTLED[:-3]),
    _stream("line-list", _STARTED + b"[1]\n" + _SETTLED),
    _stream("line-text", b'"note"\n'),
    _stream("line-number", b"5\n"),
    _stream("line-null", b"null\n"),
    _stream("line-not-utf8", _STARTED + b'{"kind": "\xff"}\n' + _SETTLED),
    _stream("each-line-not-json", b"a\nb\nc\n"),
    # --- a line of free form ---
    _stream("line-empty-object", b"{}\n"),
    _stream("line-other-keys", b'{"a":1,"b":[true,null,1.5],"c":{"d":"e"}}\n'),
    _stream(
        "line-wrong-types",
        line("42", 5, ["body"], {"turn": TURN}),
    ),
    # --- the two caps ---
    Answer(_long("5000-lines-and-a-blank-line", ("{}\n", _LINES_CAP), (" \n", 1))),
    Answer(_long("5001-lines", ("{}\n", _LINES_CAP + 1))),
    Answer(_long("5001-lines-first-not-json", ("x\n", 1), ("{}\n", _LINES_CAP), ("x\n", 1))),
    Answer(_long("at-the-size-cap", ('{"a":1}\n', 1), (" ", _BYTES_CAP - 16), ('{"b":2}\n', 1))),
    Answer(_long("over-the-size-cap", ('{"a":1}\n', 1), (" ", _BYTES_CAP - 15), ('{"b":2}\n', 1))),
    Answer(
        _long(
            "over-the-size-cap-inside-a-text",
            ('{"a":1}\n{"b":"', 1),
            ("x", _BYTES_CAP),
            ('"}\n', 1),
        )
    ),
)

_NOT_FOUND: Final = {"error": {"code": "not_found", "message": "no such session"}}


def _refusal(body_id: str, status: int, body: object) -> Answer:
    return Answer(_o(body_id, body), status)


def _error(body_id: str, status: int = 404, **fields: object) -> Answer:
    return _refusal(body_id, status, {"error": {"code": "not_found", **fields}})


REFUSALS: Final[tuple[Answer, ...]] = (
    _refusal("not-found", 404, _NOT_FOUND),
    _refusal("unauthorized", 401, {"error": {"code": "unauthorized", "message": "unknown token"}}),
    _refusal("forbidden", 403, {"error": {"code": "forbidden", "message": "view-ro reads only"}}),
    _refusal("unavailable", 503, {"error": {"code": "unavailable", "message": "starting"}}),
    _refusal("detail", 409, {"error": {"code": "conflict", "message": "busy", "detail": {"a": 1}}}),
    # --- a status that is not 200 ---
    _refusal("status-201", 201, {"sessions": []}),
    _refusal("status-204", 204, {}),
    _refusal("status-302", 302, _NOT_FOUND),
    _refusal("status-500", 500, _NOT_FOUND),
    _refusal("status-599", 599, _NOT_FOUND),
    # --- no error code ---
    _refusal("no-code", 404, {"error": {"message": "no such session"}}),
    _refusal("code-empty", 404, {"error": {"code": "", "message": "no such session"}}),
    _refusal("code-null", 404, {"error": {"code": None, "message": "no such session"}}),
    _refusal("code-number", 404, {"error": {"code": 404, "message": "no such session"}}),
    _refusal("no-error", 404, {}),
    _refusal("error-null", 404, {"error": None}),
    _refusal("error-text", 404, {"error": "not_found"}),
    _refusal("error-list", 404, {"error": [_NOT_FOUND]}),
    _refusal("code-outside-the-error", 404, {"code": "not_found", "message": "no such session"}),
    Answer(_t("body-empty", ""), 502),
    Answer(_t("body-not-json", "<html>Bad Gateway</html>"), 502),
    Answer(_t("body-list", "[]"), 500),
    Answer(Body("body-not-utf8", b'{"error": {"code": "\xff"}}'), 500),
    # --- the code and the message ---
    _error("no-message"),
    _error("message-empty", message=""),
    _error("message-null", message=None),
    _error("message-number", message=5),
    _error("message-spaces", message="   "),
    _error("message-spaces-at-the-two-ends", message="  no such session  "),
    _error("message-line-feed-at-the-end", message="no such session\n"),
    _error("message-u00a0-at-the-end", message="no such session\u00a0"),
    _error("message-u200b-at-the-end", message="no such session\u200b"),
    _error("message-500-characters", message="m" * CUT),
    _error("message-501-characters", message="m" * (CUT + 1)),
    _error("message-500-characters-and-a-space", message="m" * (CUT - 1) + " x"),
    _error("message-text-forms", message='caf\u00e9 "quoted" <b> & \u2028'),
    _refusal("code-501-characters", 404, {"error": {"code": "c" * (CUT + 1)}}),
    _refusal("code-with-a-space", 404, {"error": {"code": "not found"}}),
    _refusal("code-spaces", 404, {"error": {"code": "  "}}),
    _refusal("code-spaces-and-no-message", 404, {"error": {"code": " ", "message": ""}}),
)

# --- the token file of the reader (contract 02 §3 rule 5) -------------------------------

#: A token of 43 characters. It is the token of no deployment.
VIEW_TOKEN: Final = "vectors-view-token-0123456789abcdefghijklmn"


@dataclass(frozen=True)
class TokenCase:
    """One token file."""

    id: str
    #: The bytes of the file. None stands for a path with no file.
    raw: bytes | None


_V: Final = VIEW_TOKEN.encode("ascii")

TOKENS: Final[tuple[TokenCase, ...]] = (
    TokenCase("token", _V),
    TokenCase("final-line-feed", _V + b"\n"),
    TokenCase("final-cr-lf", _V + b"\r\n"),
    TokenCase("in-spaces", b"  " + _V + b"  \n"),
    TokenCase("empty", b""),
    TokenCase("spaces-only", b"   "),
    TokenCase("line-feed-only", b"\n"),
    TokenCase("not-utf8", b"\xff" + _V),
    TokenCase("absent", None),
)

# --- the transcript (contract 02 §8) ----------------------------------------------------

_ANSWER_CAP: Final = transcript.MAX_ANSWER_CHARS
_DELTA_CAP: Final = transcript.MAX_DELTA_CHARS
_ENTRY_CAP: Final = transcript.MAX_ENTRIES


def _pi(seq: int, body: object, turn: str = TURN) -> bytes:
    return line(seq, LineKind.PI_EVENT.value, body, turn)


def _event(event: object) -> dict[str, object]:
    return {"type": "message_update", "assistantMessageEvent": event}


def _of_kind(body_id: str, kind: str, body: object = None, turn: object = TURN) -> Body:
    """A stream of one line."""
    return Body(body_id, line(5, kind, body, turn))


def _noted(body_id: str, body: object) -> Body:
    return _of_kind(body_id, "note", body, None)


_TWO_PARTS: Final = _delta(2, "Sensor ") + _delta(3, "3 dropped out.")

TRANSCRIPTS: Final[tuple[Body, ...]] = (
    Body("one-exchange", EXCHANGE),
    Body("no-line", b""),
    *(Body(f"kind-{kind.value}", _kind_line(5, kind)) for kind in LineKind),
    Body("each-kind", b"".join(_kind_line(seq, kind) for seq, kind in enumerate(LineKind, 1))),
    # --- what ends an answer ---
    Body("answer-with-no-end", _STARTED + _TWO_PARTS),
    Body("answer-and-then-a-note", _TWO_PARTS + _NOTE),
    Body("answer-and-then-a-heartbeat", _delta(2, "Sensor ") + _HEARTBEAT + _delta(3, "3.")),
    Body(
        "answer-and-then-an-unknown-kind",
        _delta(2, "Sensor ") + line(3, "stream_overrun", {}, TURN) + _delta(4, "3."),
    ),
    Body(
        "answer-and-then-a-terminal-exchange",
        _delta(2, "Sensor ") + _kind_line(3, LineKind.TERMINAL_EXCHANGE) + _delta(4, "3."),
    ),
    Body(
        "answer-and-then-a-line-of-another-turn",
        _delta(2, "Sensor ") + line(3, "turn_settled", {}, OTHER_TURN) + _delta(4, "3."),
    ),
    Body(
        "answer-and-then-a-line-with-no-turn",
        _delta(2, "Sensor ") + line(3, "session_titled", {"title": "Sensors"}) + _delta(4, "3."),
    ),
    Body(
        "two-turns-that-interleave",
        line(1, "turn_started", {"prompt": "first"}, TURN)
        + line(2, "turn_queued", {"prompt": "second", "queue_depth": 1}, OTHER_TURN)
        + _delta(3, "one ", TURN)
        + _delta(4, "two ", OTHER_TURN)
        + _delta(5, "three", TURN)
        + _delta(6, "four", OTHER_TURN)
        + line(7, "turn_settled", {}, OTHER_TURN)
        + line(8, "turn_settled", {}, TURN),
    ),
    Body(
        "two-answers-with-no-end",
        _delta(1, "one", OTHER_TURN) + _delta(2, "two", TURN) + _delta(3, "three", OTHER_TURN),
    ),
    Body(
        "answer-of-a-line-with-no-turn",
        line(1, "pi_event", _event({"type": "text_delta", "delta": "x"})),
    ),
    # --- a pi event ---
    Body("delta-missing", _pi(2, _event({"type": "text_delta"}))),
    Body("delta-number", _delta(2, 5)),
    Body("delta-null-and-then-text", _delta(2, None) + _delta(3, "text")),
    Body("delta-empty", _delta(2, "")),
    Body("event-of-another-type", _pi(2, _event({"type": "thinking_delta", "delta": "hm"}))),
    Body("event-with-no-type", _pi(2, _event({"delta": "text"}))),
    Body("event-that-is-no-object", _pi(2, _event("text_delta"))),
    Body(
        "message-of-another-type",
        _pi(
            2,
            {"type": "message_end", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}},
        ),
    ),
    Body("message-with-no-event", _pi(2, {"type": "message_update"})),
    Body("pi-event-with-no-body", line(2, "pi_event", None, TURN)),
    Body("delta-text-forms", _delta(2, 'caf\u00e9 "quoted" <b> & \u2028 \x7f\n') + _SETTLED),
    _long(
        "delta-8193-characters",
        ('{"kind":"pi_event","turn":"t","body":{"type":"message_update",', 1),
        ('"assistantMessageEvent":{"type":"text_delta","delta":"', 1),
        ("d", _DELTA_CAP + 1),
        ('"}}}\n', 1),
    ),
    _long(
        "answer-40000-characters",
        (_delta(2, "d" * 8000).decode("utf-8"), _ANSWER_CAP // 8000),
    ),
    _long(
        "answer-40001-characters",
        (_delta(2, "d" * 8000).decode("utf-8"), _ANSWER_CAP // 8000),
        (_delta(3, "e").decode("utf-8"), 1),
    ),
    # --- each field of a line ---
    Body("line-with-no-body", line(1, "turn_started", None, TURN)),
    Body("line-with-no-kind", b'{"journal_seq":1,"turn":"t","body":{"prompt":"x"}}\n'),
    Body("line-empty-object", b"{}\n"),
    Body(
        "line-wrong-types",
        b'{"journal_seq":"1","ts":7,"kind":"turn_started","turn":5,"body":["prompt"]}\n',
    ),
    Body("kind-number", line(1, 5, {"prompt": "x"}, TURN)),
    Body("kind-upper-case", line(1, "Turn_Started", {"prompt": "x"}, TURN)),
    Body("turn-501-characters", line(1, "turn_settled", {}, "t" * (CUT + 1))),
    # --- the body of each kind ---
    _of_kind("started-status-stale", "turn_started", {"prompt": "x", "status_stale": True}),
    _of_kind("started-status-stale-text", "turn_started", {"prompt": "x", "status_stale": "true"}),
    _of_kind("started-prompt-501-characters", "turn_started", {"prompt": "p" * (CUT + 1)}),
    _of_kind("started-prompt-number", "turn_started", {"prompt": 5}),
    _of_kind("queued-no-depth", "turn_queued", {"prompt": "x"}),
    _of_kind("queued-depth-text", "turn_queued", {"prompt": "x", "queue_depth": "3"}),
    _of_kind("queued-depth-true", "turn_queued", {"prompt": "x", "queue_depth": True}),
    _of_kind("failed-no-reason", "turn_failed", {"message": "the model gave no answer"}),
    _of_kind("failed-no-message", "turn_failed", {"reason": "model_error"}),
    _of_kind("failed-empty-body", "turn_failed", {}),
    _of_kind("aborted-no-reason", "turn_aborted", {}),
    _of_kind(
        "approval-requested-with-a-summary",
        "approval_requested",
        {"tool": "ha_call", "summary": "turn the boiler off"},
    ),
    _of_kind("approval-requested-no-tool", "approval_requested", {}),
    _of_kind("approval-resolved-no-decision", "approval_resolved", {"waited_s": 12}),
    _of_kind("approval-resolved-no-wait", "approval_resolved", {"decision": "approved"}),
    _of_kind(
        "approval-resolved-wait-fraction",
        "approval_resolved",
        {"decision": "approved", "waited_s": 1.5},
    ),
    _of_kind("writer-no-holder", "writer_changed", {"reason": "lease_expired"}, None),
    _of_kind("writer-holder-null", "writer_changed", {"holder": None, "reason": "released"}, None),
    _of_kind("titled-no-title", "session_titled", {}, None),
    _of_kind(
        "fallback-with-an-entry",
        "branch_fallback",
        {"wanted_entry": "e5", "reason": "unmapped_parent"},
    ),
    # --- a note ---
    _noted("note-stream-overrun", {"note": "stream_overrun", "last_seq": 118}),
    _noted("note-stream-overrun-no-seq", {"note": "stream_overrun"}),
    _noted("note-named", {"note": "persona_truncated", "cap_bytes": 16384}),
    _noted("note-no-name", {"cap_bytes": 16384, "b": 1, "a": 2}),
    _noted("note-name-number", {"note": 5}),
    _noted("note-empty-body", {}),
    _noted("note-11-keys", {f"key-{number:02d}": number for number in range(11, 0, -1)}),
    # --- the cap of entries ---
    _long(
        "an-answer-with-no-end-and-2001-lines",
        (_delta(1, "open").decode("utf-8"), 1),
        ('{"kind":"turn_settled"}\n', _ENTRY_CAP + 1),
    ),
)

# --- the audit files (contract 04 §6) ---------------------------------------------------

DAY: Final = "2026-09-19.jsonl"
DAY_BEFORE: Final = "2026-09-18.jsonl"

_ARGS_CAP: Final = auditfiles.MAX_ARGS_CHARS
_LINE_CAP: Final = auditfiles.MAX_LINE_BYTES


@dataclass(frozen=True)
class AuditCase:
    """One audit directory and one call of the reader."""

    id: str
    #: Each file of the directory. None stands for a path with no directory.
    files: tuple[Body, ...] | None
    family: str = ""
    session: str = ""
    tool: str = ""
    decision: str = ""
    offset: int = 0
    limit: int = 50


def _day(name: str, *records: object) -> Body:
    """One day file: each record on one line, oldest first."""
    return Body(name, b"".join(_json(record) + b"\n" for record in records))


def _numbered(count: int, **fields: object) -> tuple[dict[str, object], ...]:
    """`count` records. The tool of each one holds its number."""
    return tuple(_audit(tool=f"tool-{number}", **fields) for number in range(1, count + 1))


def _one(case_id: str, **fields: object) -> AuditCase:
    """A directory with one day file that holds one record."""
    return AuditCase(case_id, (_day(DAY, _audit(**fields)),))


_FIVE: Final = (_day(DAY, *_numbered(5)),)
_TWO_DAYS: Final = (
    _day(DAY_BEFORE, *_numbered(3, family="code")),
    _day(DAY, *_numbered(3)),
)
_MIXED: Final = (_day(QUERY_DAY, *QUERY_RECORDS),)
_RECORD: Final = _json(_audit())
_OTHER_RECORD: Final = _json(_audit(family="code"))

AUDITS: Final[tuple[AuditCase, ...]] = (
    AuditCase("one-record", (_day(DAY, _audit()),)),
    AuditCase("no-directory", None),
    AuditCase("no-file", ()),
    AuditCase("empty-file", (Body(DAY, b""),)),
    # --- the order and the page ---
    AuditCase("five-records", _FIVE),
    AuditCase("limit-2", _FIVE, limit=2),
    AuditCase("limit-2-offset-2", _FIVE, limit=2, offset=2),
    AuditCase("limit-2-offset-4", _FIVE, limit=2, offset=4),
    AuditCase("limit-5", _FIVE, limit=5),
    AuditCase("limit-4", _FIVE, limit=4),
    AuditCase("offset-5", _FIVE, offset=5),
    AuditCase("offset-past-the-end", _FIVE, offset=50),
    AuditCase("limit-1", _FIVE, limit=1),
    AuditCase("two-days", _TWO_DAYS),
    AuditCase("two-days-limit-4", _TWO_DAYS, limit=4),
    AuditCase("two-days-limit-3", _TWO_DAYS, limit=3),
    AuditCase("two-days-limit-2-offset-2", _TWO_DAYS, limit=2, offset=2),
    AuditCase("two-days-family-of-the-older-day", _TWO_DAYS, family="code", limit=2),
    # --- the four filters ---
    AuditCase("filter-family", _MIXED, family="chat"),
    AuditCase("filter-family-part-of-a-name", _MIXED, family="cha"),
    AuditCase("filter-decision", _MIXED, decision="deny"),
    AuditCase("filter-decision-part-of-a-word", _MIXED, decision="den"),
    AuditCase("filter-tool-part-of-a-name", _MIXED, tool="kagi"),
    AuditCase("filter-tool-upper-case", _MIXED, tool="KAGI"),
    AuditCase("filter-session-part-of-an-id", _MIXED, session="owui-"),
    AuditCase(
        "filter-each", _MIXED, family="chat", session="owui-", tool="fetch", decision="allow"
    ),
    AuditCase("filter-no-record", _MIXED, family="code", decision="deny"),
    AuditCase("filter-and-offset", _MIXED, family="chat", offset=1),
    AuditCase(
        "filter-keeps-a-line-that-is-not-json",
        (Body(DAY, _RECORD + b"\nnot json\n" + _OTHER_RECORD + b"\n"),),
        family="vault",
    ),
    # --- where a line ends ---
    AuditCase("no-final-line-feed", (Body(DAY, _RECORD + b"\n" + _OTHER_RECORD),)),
    AuditCase(
        "blank-lines", (Body(DAY, b"\n\n" + _RECORD + b"\n  \n\t\n" + _OTHER_RECORD + b"\n\n"),)
    ),
    AuditCase("cr-lf", (Body(DAY, _RECORD + b"\r\n" + _OTHER_RECORD + b"\r\n"),)),
    _one("line-separator-inside-a-text", reason="a\u2028b\u2029c"),
    # --- a line that the reader does not parse ---
    AuditCase("line-not-json", (Body(DAY, _RECORD + b"\nnot json\n" + _OTHER_RECORD + b"\n"),)),
    AuditCase("line-cut-short", (Body(DAY, _RECORD + b"\n" + _OTHER_RECORD[:-5]),)),
    AuditCase("line-list", (Body(DAY, b"[1]\n"),)),
    AuditCase("line-null", (Body(DAY, b"null\n"),)),
    AuditCase("line-not-utf8", (Body(DAY, b'{"family": "\xff"}\n'),)),
    AuditCase(
        "line-at-the-size-cap",
        (_long(DAY, ('{"reason":"', 1), ("r", _LINE_CAP - 13), ('"}\n', 1)),),
    ),
    AuditCase(
        "line-over-the-size-cap",
        (_long(DAY, ('{"reason":"', 1), ("r", _LINE_CAP - 12), ('"}\n', 1)),),
    ),
    # --- each field of a record ---
    AuditCase("record-empty-object", (Body(DAY, b"{}\n"),)),
    _one(
        "wrong-types",
        ts=5,
        family=None,
        sandbox_id=["chat-s2"],
        sandbox_id_trusted="true",
        grants_rev={},
        tool=True,
        decision=1.5,
        reason=0,
        latency_ms="38",
        waited_ms=True,
        gate=7,
        claimed="owui-1",
        chain="chat",
    ),
    _one("untrusted-sandbox", sandbox_id_trusted=False),
    _one("trusted-is-one", sandbox_id_trusted=1),
    _one("gate", decision="allow", reason="approved", gate="0123456789abcdef", waited_ms=4200),
    _one("gate-empty", gate=""),
    _one("texts-501-characters", tool="t" * (CUT + 1), reason="r" * (CUT + 1)),
    _one("counts-at-the-64-bit-limit", latency_ms=I64_MAX, waited_ms=I64_MAX),
    _one("counts-that-are-fractions", latency_ms=38.0, waited_ms=0.5),
    _one("unknown-field", verb="call", server="kagi"),
    # --- the claimed block ---
    _one("claimed-null", claimed=None),
    _one("claimed-missing-keys", claimed={}),
    _one(
        "claimed-each-key",
        claimed={"session_id": SESSION, "turn_id": TURN, "delegation_id": OTHER_TURN},
    ),
    _one("claimed-wrong-types", claimed={"session_id": 5, "turn_id": None, "delegation_id": ["x"]}),
    _one("claimed-list", claimed=[SESSION]),
    # --- the chain ---
    _one("chain-empty", chain=[]),
    _one("chain-null", chain=None),
    _one("chain-10", chain=[f"family-{number}" for number in range(1, 11)]),
    _one("chain-11", chain=[f"family-{number}" for number in range(1, 12)]),
    _one("chain-items-that-are-no-text", chain=["chat", 5, None, "code", ["x"], "vault"]),
    _one("chain-11-first-is-no-text", chain=[5, *(f"family-{number}" for number in range(1, 11))]),
    _one("chain-item-501-characters", chain=["c" * (CUT + 1)]),
    # --- the arguments ---
    AuditCase("args-missing", (Body(DAY, b'{"tool":"ha_call"}\n'),)),
    _one("args-null", args=None),
    _one("args-empty-object", args={}),
    _one("args-empty-list", args=[]),
    _one("args-text", args="boiler"),
    _one("args-number", args=5),
    _one("args-false", args=False),
    _one("args-list", args=["a", 1, None, True]),
    _one("args-keys-not-sorted", args={"b": 1, "a": 2, "B": 3, "_": 4, "10": 5, "9": 6}),
    _one("args-nested", args={"filter": {"state": ["open", "closed"], "labels": []}, "limit": 20}),
    _one(
        "args-text-forms",
        args={"query": 'caf\u00e9 "quoted" <b> & \\ / \u2028 \U0001f600 \x7f\n\t'},
    ),
    AuditCase(
        "args-number-forms",
        (
            Body(
                DAY,
                b'{"args":{"a":1.5,"b":0.1,"c":-0.0,"d":1e22,"e":1E5,"f":-7,"g":100,"h":2.50}}\n',
            ),
        ),
    ),
    AuditCase(
        "args-20000-characters",
        (_long(DAY, ('{"args":"', 1), ("a", _ARGS_CAP - 2), ('"}\n', 1)),),
    ),
    AuditCase(
        "args-20001-characters",
        (_long(DAY, ('{"args":"', 1), ("a", _ARGS_CAP - 1), ('"}\n', 1)),),
    ),
    AuditCase(
        "args-long-list",
        (_long(DAY, ('{"args":[', 1), ("1,", 4999), ("1]}\n", 1)),),
    ),
    # --- the names of the files ---
    AuditCase(
        "names-that-are-no-day",
        (
            _day(DAY, _audit()),
            _day("2026-9-21.jsonl", _audit(tool="month-of-one-digit")),
            _day("2026-09-22.json", _audit(tool="another-suffix")),
            _day("2026-09-23.jsonl.bak", _audit(tool="a-second-suffix")),
            _day("2026-09-20.JSONL", _audit(tool="suffix-upper-case")),
            _day("x2026-09-24.jsonl", _audit(tool="a-letter-first")),
            _day("20260925.jsonl", _audit(tool="no-hyphen")),
            _day(f"2026-09-2{ARABIC_ONE}.jsonl", _audit(tool="arabic-digit")),
            _day("audit.jsonl", _audit(tool="a-word")),
        ),
    ),
    AuditCase(
        "names-that-are-no-date",
        (
            _day("2026-13-45.jsonl", _audit(tool="month-13")),
            _day("0000-00-00.jsonl", _audit(tool="zeros")),
            _day("9999-99-99.jsonl", _audit(tool="nines")),
        ),
    ),
    AuditCase(
        "30-days",
        tuple(_day(f"2026-08-{day:02d}.jsonl", _audit(tool=f"day-{day}")) for day in range(1, 31)),
        limit=2,
    ),
    AuditCase(
        "31-days",
        tuple(_day(f"2026-08-{day:02d}.jsonl", _audit(tool=f"day-{day}")) for day in range(1, 32)),
        limit=2,
    ),
    AuditCase(
        "31-days-family-of-the-oldest-day",
        (
            *(_day(f"2026-08-{day:02d}.jsonl", _audit()) for day in range(2, 32)),
            _day("2026-08-01.jsonl", _audit(family="code")),
        ),
        family="code",
    ),
    # --- the cap of lines ---
    AuditCase(
        "40000-lines-and-no-match",
        (_long(DAY, ('{"family":"code"}\n', auditfiles.MAX_LINES_SCANNED)),),
        family="chat",
    ),
    AuditCase(
        "40001-lines-and-no-match",
        (_long(DAY, ('{"family":"code"}\n', auditfiles.MAX_LINES_SCANNED + 1)),),
        family="chat",
    ),
    AuditCase(
        "40001-lines-and-the-oldest-matches",
        (
            _long(
                DAY,
                ('{"family":"chat"}\n', 1),
                ('{"family":"code"}\n', auditfiles.MAX_LINES_SCANNED),
            ),
        ),
        family="chat",
    ),
)

# --- the validation report (contract 01 §7) ---------------------------------------------

#: The name of the report file. A problem text holds it.
REPORT_NAME: Final = statusdocs.VALIDATION_FILE

_DOC_CAP: Final = jsonfiles.MAX_DOC_BYTES
_ISSUE_CAP: Final = statusdocs.MAX_ISSUES

_ISSUE: Final[dict[str, object]] = {
    "severity": "error",
    "code": "unknown_server",
    "loc": "tools.kagi",
    "msg": "no MCP server named 'kagi' in the registry",
}


@dataclass(frozen=True)
class ReportCase:
    """One report file."""

    id: str
    #: The file. None stands for a path with no file.
    body: Body | None


def _report(case_id: str, *issues: object, **fields: object) -> ReportCase:
    return ReportCase(case_id, _o(case_id, {"ok": False, "issues": list(issues), **fields}))


def _issue(case_id: str, **fields: object) -> ReportCase:
    return _report(case_id, {**_ISSUE, **fields})


REPORTS: Final[tuple[ReportCase, ...]] = (
    _report(
        "two-issues",
        _ISSUE,
        {"severity": "warning", "loc": "files[0].path", "msg": "no such directory"},
    ),
    _report("no-issue"),
    ReportCase("absent", None),
    # --- the list of issues ---
    ReportCase("empty-object", _o("empty-object", {})),
    ReportCase("issues-null", _o("issues-null", {"issues": None})),
    ReportCase("issues-object", _o("issues-object", {"issues": {"0": _ISSUE}})),
    ReportCase("issues-text", _o("issues-text", {"issues": "none"})),
    _report("issues-that-are-no-object", 5, "text", None, [_ISSUE], _ISSUE, True),
    ReportCase(
        "200-issues",
        _long(
            "200-issues",
            ('{"issues":[', 1),
            ('{"loc":"l","msg":"m"},', _ISSUE_CAP - 1),
            ('{"loc":"last","msg":"m"}]}', 1),
        ),
    ),
    ReportCase(
        "201-issues",
        _long(
            "201-issues",
            ('{"issues":[', 1),
            ('{"loc":"l","msg":"m"},', _ISSUE_CAP),
            ('{"loc":"last","msg":"m"}]}', 1),
        ),
    ),
    # --- each field of an issue ---
    _report("issue-empty-object", {}),
    ReportCase("no-loc", _o("no-loc", {"issues": [{"msg": "the file has no name"}]})),
    ReportCase("no-msg", _o("no-msg", {"issues": [{"loc": "name"}]})),
    _issue("loc-number", loc=5),
    _issue("loc-null", loc=None),
    _issue("loc-list", loc=["tools", "kagi"]),
    _issue("loc-true", loc=True),
    _issue("loc-empty", loc=""),
    _issue("msg-number", msg=5),
    _issue("msg-null", msg=None),
    _issue("msg-object", msg={"text": "x"}),
    _issue("msg-500-characters", msg="m" * CUT),
    _issue("msg-501-characters", msg="m" * (CUT + 1)),
    _issue("loc-501-characters", loc="l" * (CUT + 1)),
    _issue("text-forms", loc="tools['caf\u00e9']", msg='<b> & "quoted" \u2028 \U0001f600\n'),
    # --- a file that the reader refuses ---
    ReportCase("list", _o("list", [_ISSUE])),
    ReportCase("text", _t("text", '"issues"')),
    ReportCase("null", _t("null", "null")),
    ReportCase("not-json", _t("not-json", "not json")),
    ReportCase("cut-short", _t("cut-short", '{"issues": [')),
    ReportCase("empty", _t("empty", "")),
    ReportCase("not-utf8", Body("not-utf8", b'{"issues": "\xff"}')),
    ReportCase(
        "at-the-size-cap",
        _long("at-the-size-cap", ('{"issues":[]', 1), (" ", _DOC_CAP - 13), ("}", 1)),
    ),
    ReportCase(
        "over-the-size-cap",
        _long("over-the-size-cap", ('{"issues":[]', 1), (" ", _DOC_CAP - 12), ("}", 1)),
    ),
)

# --- the env file of the verify hook (contract 06 §4) -----------------------------------

#: A directory that no machine has. A vector can hold a path under it.
NO_ROOT: Final = "/no-such-root-of-the-vectors"

#: Stands for the directory that the generator makes for one vector.
ROOT: Final = "$ROOT"

LOOPBACK: Final = "127.0.0.1"

#: The paths that each environment of a hook or of a check names. None exists.
NO_PATHS: Final[dict[str, str]] = {
    "VIEW_STATE_ROOT": f"{NO_ROOT}/state",
    "VIEW_REGISTRY_DIR": f"{NO_ROOT}/registry",
    "VIEW_SESSIOND_SOCKET": f"{NO_ROOT}/sessiond.sock",
}

_BIND: Final = f"VIEW_BIND={LOOPBACK}\n"


@dataclass(frozen=True)
class EnvFileCase:
    """One env file, and the variables that the hook gets from its caller."""

    id: str
    #: The bytes of the file. None stands for a path with no file.
    raw: bytes | None
    #: The variables of the caller, beside the paths of `NO_PATHS`.
    inherited: dict[str, str] = field(default_factory=dict[str, str])


def _env(case_id: str, text: str, **inherited: str) -> EnvFileCase:
    return EnvFileCase(case_id, text.encode("utf-8"), dict(inherited))


ENV_FILES: Final[tuple[EnvFileCase, ...]] = (
    _env("bind-and-port", _BIND + "VIEW_PORT=8371\n"),
    _env("bind-only", _BIND),
    _env("no-final-line-end", _BIND + "VIEW_PORT=8371"),
    _env("cr-lf", f"VIEW_BIND={LOOPBACK}\r\nVIEW_PORT=8371\r\n"),
    _env("empty-file", ""),
    EnvFileCase("absent", None),
    # --- the caller and the file ---
    _env("caller-alone", "", VIEW_BIND=LOOPBACK, VIEW_PORT="8372"),
    _env("file-wins-over-the-caller", "VIEW_PORT=8371\n", VIEW_BIND=LOOPBACK, VIEW_PORT="8372"),
    _env("file-and-caller", "VIEW_PORT=8371\n", VIEW_BIND=LOOPBACK),
    _env("empty-value-wins-over-the-caller", "VIEW_PORT=\n", VIEW_BIND=LOOPBACK, VIEW_PORT="8372"),
    # --- a line that holds no variable ---
    _env(
        "comment-and-blank-lines", "# the bind\n\n   \n" + _BIND + "  # indented\nVIEW_PORT=8371\n"
    ),
    _env("comment-holds-a-variable", _BIND + "#VIEW_PORT=8371\n"),
    _env("no-equals", _BIND + "VIEW_PORT\n"),
    _env("no-name", _BIND + "=8371\n"),
    _env("export", _BIND + "export VIEW_PORT=8371\n"),
    _env("space-before-the-equals", _BIND + "VIEW_PORT =8371\n"),
    _env("name-starts-with-a-digit", _BIND + "1VIEW_PORT=8371\nVIEW_PORT1=8371\n"),
    _env("name-with-a-hyphen", _BIND + "VIEW-PORT=8371\n"),
    _env("name-outside-ascii", _BIND + "VIEW_P\u00d6RT=8371\n"),
    _env("name-lower-case", _BIND + "view_port=8371\n"),
    _env("byte-order-mark", "\ufeffVIEW_PORT=8371\n" + _BIND),
    # --- the value ---
    _env("empty-value", _BIND + "VIEW_PORT=\n"),
    _env("space-after-the-equals", _BIND + "VIEW_PORT= 8371\n"),
    _env("spaces-around-the-line", "  " + _BIND + "\tVIEW_PORT=8371  \n"),
    _env("u00a0-around-the-line", _BIND + "\u00a0VIEW_PORT=8371\u00a0\n"),
    _env("double-quotes", _BIND + 'VIEW_PORT="8371"\n'),
    _env("single-quotes", _BIND + "VIEW_PORT='8371'\n"),
    _env("comment-after-the-value", _BIND + "VIEW_PORT=8371 # the port\n"),
    _env(
        "equals-inside-the-value",
        f"AGENT_LAN_ADDRESS={LAN}\nVIEW_ACCESS_KEY={VIEW_KEY[:16]}={VIEW_KEY[16:]}\n",
    ),
    _env("variable-two-times", _BIND + "VIEW_PORT=8371\nVIEW_PORT=8373\n"),
    _env("variable-two-times-last-empty", _BIND + "VIEW_PORT=8371\nVIEW_PORT=\n"),
    _env("site-address", f"AGENT_LAN_ADDRESS={LAN}\nVIEW_ACCESS_KEY={VIEW_KEY}\n"),
    # --- each other character that ends a line for `str.splitlines` ---
    _env("vertical-tab-between-two-variables", f"VIEW_BIND={LOOPBACK}\x0bVIEW_PORT=8371\n"),
    _env("form-feed-between-two-variables", f"VIEW_BIND={LOOPBACK}\x0cVIEW_PORT=8371\n"),
    _env("u001c-between-two-variables", f"VIEW_BIND={LOOPBACK}\x1cVIEW_PORT=8371\n"),
    _env("u0085-between-two-variables", f"VIEW_BIND={LOOPBACK}\x85VIEW_PORT=8371\n"),
    _env("u2028-between-two-variables", f"VIEW_BIND={LOOPBACK}\u2028VIEW_PORT=8371\n"),
    _env("cr-between-two-variables", f"VIEW_BIND={LOOPBACK}\rVIEW_PORT=8371\n"),
    # --- a config that the hook refuses ---
    _env("no-bind", "VIEW_PORT=8371\n"),
    _env("port-word", _BIND + "VIEW_PORT=http\n"),
    _env("port-zero", _BIND + "VIEW_PORT=0\n"),
    _env("wildcard-bind", "VIEW_BIND=0.0.0.0\n"),
    _env("site-address-and-no-key", f"AGENT_LAN_ADDRESS={LAN}\n"),
)

# --- the check of the service (spec.md §8.3 rule 2) -------------------------------------


@dataclass(frozen=True)
class CheckCase:
    """One environment of `noticeboard --check`."""

    id: str
    #: The variables. `ROOT` in a value stands for the directory of the vector.
    variables: dict[str, str]
    #: Each directory that the generator makes under that directory.
    directories: tuple[str, ...] = ()
    #: Each file that the generator makes under that directory, with its bytes.
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])


def _check(case_id: str, **variables: str) -> CheckCase:
    """An environment with a loopback bind and no path that exists."""
    return CheckCase(case_id, {"VIEW_BIND": LOOPBACK, **NO_PATHS, **variables})


_ROOT_PATHS: Final[dict[str, str]] = {
    "VIEW_BIND": LOOPBACK,
    "VIEW_STATE_ROOT": f"{ROOT}/state",
    "VIEW_REGISTRY_DIR": f"{ROOT}/registry",
    "VIEW_SESSIOND_SOCKET": f"{ROOT}/sessiond.sock",
}
_KEY_FILE: Final = "view.key"
_KEY_FILE_VARIABLE: Final = {"VIEW_ACCESS_KEY_FILE": f"{ROOT}/{_KEY_FILE}"}


def _key_file(case_id: str, raw: bytes | None, **variables: str) -> CheckCase:
    """An environment that names a key file. None stands for a path with no file."""
    files = {} if raw is None else {_KEY_FILE: raw}

    return CheckCase(
        case_id, {**_check(case_id, **variables).variables, **_KEY_FILE_VARIABLE}, files=files
    )


_LAN_BIND: Final = {"AGENT_LAN_ADDRESS": LAN}

CHECKS: Final[tuple[CheckCase, ...]] = (
    _check("loopback-no-key"),
    _check("loopback-short-key", VIEW_ACCESS_KEY=SHORT_KEY),
    _check("loopback-ipv6", VIEW_BIND="::1"),
    _check("loopback-name", VIEW_BIND="localhost"),
    CheckCase("lan-with-key", {**NO_PATHS, **_LAN_BIND, "VIEW_ACCESS_KEY": VIEW_KEY}),
    CheckCase(
        "bind-wins-over-the-site-address",
        {**NO_PATHS, **_LAN_BIND, "VIEW_BIND": "192.0.2.11", "VIEW_ACCESS_KEY": VIEW_KEY},
    ),
    _check("port", VIEW_PORT="18370"),
    _check("page-size", VIEW_PAGE_SIZE="25"),
    _check("cookie-off", VIEW_COOKIE_SECURE="0"),
    _check("cookie-on", VIEW_COOKIE_SECURE="1"),
    _check("url-wins-over-the-socket", VIEW_SESSIOND_URL="http://192.0.2.10:8350"),
    _check("another-variable", VIEW_UNKNOWN="1", OTHER_PORT="9"),
    # --- paths that exist ---
    CheckCase(
        "each-path-present",
        _ROOT_PATHS,
        ("state/families", "state/audit", "state/outcomes", "state/tokens", "registry"),
        {"state/tokens/view-ro.token": VIEW_TOKEN.encode("ascii") + b"\n", "sessiond.sock": b""},
    ),
    CheckCase("state-root-present", _ROOT_PATHS, ("state",)),
    CheckCase("families-and-registry-present", _ROOT_PATHS, ("state/families", "registry")),
    CheckCase(
        "token-file-empty", _ROOT_PATHS, ("state/tokens",), {"state/tokens/view-ro.token": b""}
    ),
    CheckCase(
        "a-file-in-place-of-a-directory",
        _ROOT_PATHS,
        ("state",),
        {"state/families": b"", "registry": b""},
    ),
    # --- the key file ---
    _key_file("key-file", VIEW_KEY.encode("ascii")),
    _key_file("key-file-final-line-feed", VIEW_KEY.encode("ascii") + b"\n"),
    _key_file("key-file-on-the-lan", VIEW_KEY.encode("ascii") + b"\n", VIEW_BIND="192.0.2.11"),
    _key_file(
        "key-file-wins-over-the-variable", VIEW_KEY.encode("ascii"), VIEW_ACCESS_KEY=SHORT_KEY
    ),
    _key_file("key-file-empty-on-loopback", b""),
    _key_file("key-file-spaces-on-loopback", b"  \n"),
    _key_file("key-file-short-on-loopback", SHORT_KEY.encode("ascii")),
    _key_file("key-file-empty-on-the-lan", b"", VIEW_BIND="192.0.2.11"),
    _key_file(
        "key-file-empty-wins-over-the-variable",
        b"",
        VIEW_BIND="192.0.2.11",
        VIEW_ACCESS_KEY=VIEW_KEY,
    ),
    _key_file("key-file-short-on-the-lan", SHORT_KEY.encode("ascii"), VIEW_BIND="192.0.2.11"),
    _key_file("key-file-absent", None),
    _key_file("key-file-not-utf8", b"\xff" + VIEW_KEY.encode("ascii")),
    # --- a config that the service refuses ---
    CheckCase("no-bind", dict(NO_PATHS)),
    CheckCase("lan-no-key", {**NO_PATHS, **_LAN_BIND}),
    CheckCase("lan-short-key", {**NO_PATHS, **_LAN_BIND, "VIEW_ACCESS_KEY": SHORT_KEY}),
    _check("wildcard-bind", VIEW_BIND="0.0.0.0"),
    _check("port-word", VIEW_PORT="http"),
    _check("port-zero", VIEW_PORT="0"),
    _check("page-size-501", VIEW_PAGE_SIZE="501"),
)
