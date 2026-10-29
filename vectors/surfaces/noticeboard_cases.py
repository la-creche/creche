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
import string
from dataclasses import dataclass, field
from typing import Any, Final

from noticeboard import auditfiles, jsonfiles, security, sessions, statusdocs
from vectors.surfaces import ids
from vectors.surfaces.config import INT_DIGITS_MAX, LAN, NOTICEBOARD_KEY
from vectors.surfaces.session_cases import (
    FAMILY,
    JOB_SESSION,
    OTHER_SESSION,
    SESSION,
    Body,
)

#: The largest integer that a strict JSON reader takes as a signed number.
I64_MAX: Final = 2**63 - 1

#: The count of characters that the noticeboard keeps of one text.
CUT: Final = jsonfiles.MAX_TEXT_CHARS


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

_K: Final = NOTICEBOARD_KEY.encode("ascii")

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
    expected: str = NOTICEBOARD_KEY


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
    CsrfCase("scheme-with-a-mark", origin=f"ht!tp://{HOST}"),
    CsrfCase("scheme-empty", origin=f"://{HOST}"),
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
    # --- what the URL reader removes ---
    CsrfCase("origin-with-a-tab-inside", origin="https://view\t.example"),
    CsrfCase("origin-with-a-cr-inside", origin="https://view\r.example"),
    CsrfCase("origin-with-a-line-feed-inside", origin="https://view\n.example"),
    CsrfCase("origin-u0001-at-the-start", origin="\x01" + ORIGIN),
    CsrfCase("origin-space-at-the-start", origin=" " + ORIGIN),
    CsrfCase("origin-u0001-at-the-end", origin=ORIGIN + "\x01"),
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
_COOKIE: Final = security.CSRF_COOKIE.encode("ascii")

#: The start of a `Cookie` header that gives the cookie of the form guard a value.
COOKIE_NAME: Final = _COOKIE + b"="

#: Each character that the cookie writer of Python puts between no quotes,
#: beside the letters and the digits.
PLAIN_MARKS: Final = "!#$%&'*+-.^_`|~:"

#: The most bytes that the cookie of the form guard is to have.
COOKIE_BYTES: Final = 512

#: Each character that a token of a cookie input is made of: a `cookie-octet`
#: of RFC 6265. It is a printable ASCII character, and it is no space, no
#: double quote, no comma, no semicolon and no backslash.
COOKIE_OCTETS: Final = frozenset(chr(code) for code in range(0x21, 0x7F)) - frozenset('",;\\')

#: Each other mark of a `cookie-octet`. The cookie writer of Python puts a
#: value that holds one of them between double quotes.
QUOTED_MARKS: Final = "".join(
    sorted(COOKIE_OCTETS - frozenset(string.ascii_letters + string.digits + PLAIN_MARKS))
)


@dataclass(frozen=True)
class CookieCase:
    """One `Cookie` header."""

    id: str
    #: The bytes of the header value. None stands for a request with no header.
    raw: bytes | None


COOKIES: Final[tuple[CookieCase, ...]] = (
    CookieCase("token", COOKIE_NAME + _T),
    # --- no token in the request ---
    CookieCase("no-header", None),
    CookieCase("empty-header", b""),
    CookieCase("empty-value", COOKIE_NAME),
    CookieCase("name-alone", _COOKIE),
    CookieCase("another-cookie-alone", b"session=abc"),
    CookieCase("quoted-empty-value", COOKIE_NAME + b'""'),
    # --- the name ---
    CookieCase("name-upper-case", COOKIE_NAME.upper() + _T),
    CookieCase("name-with-more-letters", _COOKIE + b"2=" + _T),
    CookieCase("name-after-more-letters", b"x" + COOKIE_NAME + _T),
    # --- more than one cookie ---
    CookieCase("after-another-cookie", b"session=abc; " + COOKIE_NAME + _T),
    CookieCase("before-another-cookie", COOKIE_NAME + _T + b"; session=abc"),
    CookieCase("no-space-after-the-semicolon", b"session=abc;" + COOKIE_NAME + _T),
    CookieCase("semicolon-at-the-end", COOKIE_NAME + _T + b";"),
    CookieCase("comma-between-two-cookies", b"session=abc, " + COOKIE_NAME + _T),
    CookieCase("two-values", COOKIE_NAME + b"first; " + COOKIE_NAME + b"second"),
    CookieCase("two-values-last-empty", COOKIE_NAME + _T + b"; " + COOKIE_NAME),
    CookieCase("another-cookie-outside-ascii", b"session=caf\xc3\xa9; " + COOKIE_NAME + _T),
    # --- the two ends of the name and of the value ---
    CookieCase(
        "spaces-around-the-name-and-the-value",
        b"session=abc;  " + _COOKIE + b" = " + _T + b" ; theme=dark",
    ),
    CookieCase(
        "tabs-around-the-name-and-the-value",
        b"session=abc;\t" + _COOKIE + b"\t=\t" + _T + b"\t; theme=dark",
    ),
    CookieCase("byte-a0-after-the-value", COOKIE_NAME + _T + b"\xa0"),
    CookieCase("byte-a0-before-the-name", b"session=abc;\xa0" + COOKIE_NAME + _T),
    # --- the characters of the value ---
    CookieCase("one-character", COOKIE_NAME + b"a"),
    CookieCase("each-plain-mark", COOKIE_NAME + PLAIN_MARKS.encode("ascii")),
    CookieCase("equals-inside", COOKIE_NAME + b"a=b"),
    CookieCase("slash-inside", COOKIE_NAME + b"a/b"),
    CookieCase("each-quoted-mark", COOKIE_NAME + QUOTED_MARKS.encode("ascii")),
    CookieCase("512-bytes", COOKIE_NAME + b"a" * COOKIE_BYTES),
    # --- quotes around the value ---
    CookieCase("quoted", COOKIE_NAME + b'"' + _T + b'"'),
    CookieCase("quoted-equals-inside", COOKIE_NAME + b'"a=b"'),
    CookieCase("quoted-octal-escape", COOKIE_NAME + b'"\\141bc"'),
    CookieCase("quoted-backslash-letter", COOKIE_NAME + b'"\\abc"'),
)

# --- the form body ----------------------------------------------------------------------

FORMS: Final[tuple[Body, ...]] = (
    Body("empty", b""),
    Body("one-field", b"a=1"),
    Body("two-fields", b"a=1&b=2"),
    Body(
        "the-edit-form",
        security.CSRF_FIELD.encode("ascii")
        + b"="
        + _T
        + b"&description=the+house+assistant&shell=on&subject=&verb=save",
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

#: The audit file. The audit page of the query surface reads it, and each
#: case of the audit surface that has a file writes it.
AUDIT_FILE: Final = "audit.jsonl"

#: The count of characters that the audit page keeps of one filter value.
FILTER_CHARS: Final = 200

#: A family text of that many characters.
LONG_FAMILY: Final = "f" * FILTER_CHARS

_FACE: Final = "\U0001f600"

#: A run ID and a gate ID of the chaperone: 16 bytes as lower-case hex.
RUN: Final = "07" * 16
OTHER_RUN: Final = "08" * 16
GATE: Final = "0a" * 16

#: The SHA-256 of `{}`, the canonical form of no arguments.
NO_ARGUMENTS: Final = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


def _audit(
    call: int = 1, event: str = "decided", detail: str = "allow", **fields: object
) -> dict[str, object]:
    """One line of the chaperone's audit file, in the key order of the Rust
    sink, with the fields of the caller."""
    record: dict[str, object] = {
        "run": RUN,
        "call": call,
        "started": "2026-09-19T11:59:30Z",
        "time": "2026-09-19T11:59:30.250Z",
        "app": FAMILY,
        "tool": "kagi__kagi_search_fetch",
        "arguments_sha256": NO_ARGUMENTS,
        "event": event,
        "detail": detail,
    }
    record.update(fields)

    return record


#: The three records of the query surface, oldest first: three calls.
QUERY_RECORDS: Final[tuple[dict[str, object], ...]] = (
    _audit(1, started="2026-09-19T08:00:00Z", time="2026-09-19T08:00:00Z"),
    _audit(
        2,
        detail="deny:tool_not_granted",
        started="2026-09-19T09:00:00Z",
        time="2026-09-19T09:00:00Z",
        tool="gh__list_issues",
    ),
    _audit(
        3,
        started="2026-09-19T10:00:00Z",
        time="2026-09-19T10:00:00Z",
        app="code",
        tool="kagi__kagi_summarize",
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
    Body("201-characters-outside-the-bmp", b"session=" + b"%F0%9F%98%80" * (FILTER_CHARS + 1)),
    # --- the offset ---
    Body("offset-zero", b"offset=0"),
    Body("offset-one", b"offset=1"),
    Body("offset-two", b"offset=2"),
    Body("offset-three", b"offset=3"),
    Body("offset-past-the-end", b"offset=50"),
    Body("offset-negative", b"offset=-1"),
    Body("offset-plus-sign", b"offset=%2B1"),
    Body("offset-in-spaces", b"offset=+1+"),
    Body("offset-u00a0-before", b"offset=%C2%A01"),
    Body("offset-form-feed-after", b"offset=1%0C"),
    Body("offset-u001f-before", b"offset=%1F1"),
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
    _long("offset-4300-digits", ("offset=", 1), ("0", INT_DIGITS_MAX - 1), ("1", 1)),
    _long("offset-4301-digits", ("offset=", 1), ("0", INT_DIGITS_MAX), ("1", 1)),
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


#: The id surface of each grammar that a route parameter follows.
NAME_SURFACE: Final = "id.family_name.noticeboard"
SESSION_SURFACE: Final = "id.session_id.attendance"


def _id_inputs(surface: str) -> tuple[tuple[str, str], ...]:
    """Each input of one id surface: the id of its vector and its text.

    A copy of a grammar gets the inputs of each other copy, with the same
    vector ids (`vectors/README.md`, rule 2 for a new surface). A difference
    between two copies is then a difference on one vector id.
    """
    (found,) = (one for one in ids.surfaces() if one.name == surface)
    inputs: list[tuple[str, str]] = []
    for vector in found.vectors:
        given = vector.body["input"]
        text = given.get("text") if isinstance(given, dict) else None
        if not isinstance(text, str):
            raise ValueError(f"{surface}: the input of {vector.id} is no text")

        inputs.append((vector.id, text))

    return tuple(inputs)


#: Each family route parameter: an id and the segment as a client sends it.
SEGMENTS: Final[tuple[tuple[str, str], ...]] = (
    *((case_id, segment(text)) for case_id, text in _id_inputs(NAME_SURFACE)),
    # --- texts that the id surface does not hold ---
    ("not-a-name", segment("Not A Name")),
    ("question-mark", segment("chat?")),
    ("hash", segment("chat#")),
    # --- a segment that only its written form tells apart ---
    ("one-dot", "."),
    ("escaped-dots", "%2E%2E"),
    ("escaped-letter", "ch%61t"),
    ("escape-lower-case", "%63hat"),
    ("escape-not-utf8", "chat%FF"),
    ("escape-not-hex", "chat%zz"),
)

#: Each session id of the id surface, and two texts that it does not hold.
SESSION_IDS: Final[tuple[tuple[str, str], ...]] = (
    *_id_inputs(SESSION_SURFACE),
    ("not-an-id", "not an id"),
    ("each-door-prefix", "job-auto-tui-owui-1"),
)

# --- the answers of attendance: the two view routes ------------------------------------

_TS: Final = "2026-10-05T19:22:31.070Z"

#: The trigger of a session that a firing started.
_TRIGGER: Final[dict[str, object]] = {
    "kind": "dispatch",
    "name": "scrum-lead",
    "fired_at": "2026-10-25T12:00:00Z",
}


def _row(**fields: object) -> dict[str, object]:
    """One row of `GET /families/{f}/sessions`, with the fields of the caller."""
    row: dict[str, object] = {
        "id": SESSION,
        "title": "Kitchen sensor debug",
        "created": None,
        "updated": _TS,
        "job_id": None,
        "delegation": None,
        "trigger": None,
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
    """A list answer that holds `rows`."""
    return Answer(_o(body_id, {"sessions": list(rows), "next_cursor": None, **fields}))


#: A transport failure, as the HTTP module of the noticeboard words one.
UNREACHABLE: Final = "cannot reach attendance: ConnectError: the stand-in has no socket"

_ANSWER_CAP: Final = sessions.MAX_ANSWER_BYTES

LISTS: Final[tuple[Answer, ...]] = (
    _listed("one-session", _row()),
    _listed("no-session"),
    _listed("two-sessions", _row(), _row(id=OTHER_SESSION, title=None)),
    Answer(_o("empty-object", {})),
    # --- the arguments of the call ---
    Answer(_o("call-cursor", {"sessions": []}), call={"cursor": "b2Zmc2V0OjUw"}),
    # --- the list and the cursor ---
    _listed("next-cursor", _row(), next_cursor="b2Zmc2V0OjUw"),
    _listed("next-cursor-number", next_cursor=5),
    _listed("next-cursor-600-characters", next_cursor="c" * 600),
    Answer(_o("sessions-null", {"sessions": None})),
    Answer(_o("sessions-object", {"sessions": {"0": _row()}})),
    Answer(_o("sessions-text", {"sessions": "none"})),
    _listed("rows-that-are-no-object", 5, "text", None, [_row()], _row(), True),
    Answer(
        _o(
            "201-rows",
            {
                "sessions": [
                    *([0] * (sessions.MAX_SESSIONS - 1)),
                    _row(id="row-200"),
                    _row(id="row-201"),
                ]
            },
        )
    ),
    # --- what started a session ---
    _listed("origin-trigger", _row(trigger=_TRIGGER)),
    _listed("origin-trigger-with-no-name", _row(trigger={"kind": "webhook"})),
    _listed("origin-trigger-empty-object", _row(trigger={})),
    _listed("origin-trigger-list", _row(trigger=[_TRIGGER])),
    _listed("origin-delegation", _row(delegation="vault-oracle")),
    _listed("origin-job", _row(job_id=JOB_SESSION)),
    _listed("origin-each", _row(trigger=_TRIGGER, delegation="vault-oracle", job_id=JOB_SESSION)),
    # --- each field of a row ---
    _listed("row-empty-object", {}),
    _listed(
        "wrong-types",
        _row(
            id=5,
            title={"text": "x"},
            created=1,
            updated=2.5,
            job_id=True,
            delegation=["x"],
            trigger="dispatch",
        ),
    ),
    _listed("created", _row(created="2026-10-05T19:21:47Z")),
    _listed("title-500-characters", _row(title="t" * CUT)),
    _listed("title-501-characters", _row(title="t" * (CUT + 1))),
    _listed("title-501-characters-outside-the-bmp", _row(title=_FACE * (CUT + 1))),
    _listed("title-text-forms", _row(title='caf\u00e9 "quoted" <b> & \u2028 \x7f\n')),
    _listed("id-501-characters", _row(id="s" * (CUT + 1))),
    _listed("unknown-field", _row(cwd="/srv/agents/sessions/chat")),
    # --- a body that the reader refuses ---
    Answer(_t("not-json", "not json")),
    Answer(_t("cut-short", '{"sessions": [')),
    Answer(_t("empty", "")),
    Answer(Body("not-utf8", b'{"sessions": "\xff"}')),
    Answer(_t("list", "[]")),
    Answer(_t("text", '"sessions"')),
    Answer(_t("number", "5")),
    Answer(_t("null", "null")),
    Answer(_long("at-the-size-cap", ('{"sessions":[]', 1), (" ", _ANSWER_CAP - 15), ("}", 1))),
    Answer(_long("over-the-size-cap", ('{"sessions":[]', 1), (" ", _ANSWER_CAP - 14), ("}", 1))),
)


def _said(position: object, kind: object, text: object) -> dict[str, object]:
    """One text entry of a transcript page."""
    return {"position": position, "kind": kind, "text": text}


def _tool(position: object, **fields: object) -> dict[str, object]:
    """One tool call entry of a transcript page."""
    entry: dict[str, object] = {
        "position": position,
        "kind": "tool_call",
        "tool_call_id": "call-1",
        "title": "kagi__kagi_search_fetch",
        "status": "completed",
        "result": "Sensor 3 dropped out at 02:10.",
    }
    entry.update(fields)

    return entry


def _paged(body_id: str, *entries: object, **fields: object) -> Answer:
    """A transcript page that holds `entries`."""
    page: dict[str, object] = {
        "session_id": SESSION,
        "entries": list(entries),
        "next": None,
        "truncated": False,
    }
    page.update(fields)

    return Answer(_o(body_id, page))


_EXCHANGE: Final = (
    _said(0, "user", "Which sensor dropped out?"),
    _said(1, "agent", "Sensor 3 dropped out."),
)

_ENTRY_CAP: Final = sessions.MAX_ENTRY_CHARS

TRANSCRIPTS: Final[tuple[Answer, ...]] = (
    _paged("one-exchange", *_EXCHANGE),
    _paged("no-entry"),
    Answer(_o("empty-object", {})),
    _paged(
        "each-kind",
        _said(0, "user", "Which sensor dropped out?"),
        _said(1, "thought", "The log names the sensor."),
        _tool(2),
        _said(3, "agent", "Sensor 3 dropped out."),
    ),
    # --- the arguments of the call ---
    Answer(_paged("call-start-0", *_EXCHANGE).body, call={"start": 0}),
    Answer(_paged("call-start-4", _said(4, "agent", "Yes.")).body, call={"start": 4}),
    # --- the next page and the cap of the replay ---
    _paged("next-4", *_EXCHANGE, next=4),
    _paged("next-text", *_EXCHANGE, next="4"),
    _paged("next-negative", *_EXCHANGE, next=-1),
    _paged("next-true", *_EXCHANGE, next=True),
    _paged("next-fraction", *_EXCHANGE, next=4.0),
    _paged("truncated", *_EXCHANGE, truncated=True),
    _paged("truncated-one", *_EXCHANGE, truncated=1),
    # --- the list of entries ---
    Answer(_o("entries-null", {"session_id": SESSION, "entries": None})),
    Answer(_o("entries-object", {"session_id": SESSION, "entries": {"0": _EXCHANGE[0]}})),
    _paged("entries-that-are-no-object", 5, "text", None, [_EXCHANGE[0]], _EXCHANGE[1], True),
    _paged(
        "501-entries",
        *([0] * (sessions.MAX_ENTRIES - 1)),
        _said(499, "user", "entry 500"),
        _said(500, "user", "entry 501"),
    ),
    # --- the kind of an entry ---
    _paged("kind-unknown", _EXCHANGE[0], _said(1, "plan", "x"), _EXCHANGE[1]),
    _paged("kind-upper-case", _said(0, "User", "x")),
    _paged("kind-number", _said(0, 5, "x")),
    _paged("kind-missing", {"position": 0, "text": "x"}),
    _paged("kind-501-characters", _said(0, "k" * (CUT + 1), "x")),
    # --- each field of an entry ---
    _paged("entry-empty-object", {}),
    _paged("said-wrong-types", _said("0", "user", 5)),
    _paged("tool-wrong-types", _tool(True, title=5, status=None, result=["x"])),
    _paged("tool-with-no-result", _tool(0, result=None, status="in_progress")),
    _paged("text-at-the-cap", _said(0, "agent", "a" * _ENTRY_CAP)),
    _paged("text-over-the-cap", _said(0, "agent", "a" * (_ENTRY_CAP + 1))),
    _paged("text-forms", _said(0, "agent", 'caf\u00e9 "quoted" <b> & \u2028 \x7f\n')),
    _paged("position-at-the-64-bit-limit", _said(I64_MAX, "user", "x")),
    # --- a body that the reader refuses ---
    Answer(_t("not-json", "not json")),
    Answer(_t("empty", "")),
    Answer(_t("list", "[]")),
    Answer(_t("null", "null")),
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
    # --- the status comes before the size cap ---
    Answer(
        _long(
            "body-over-the-size-cap",
            ('{"error":{"code":"not_found"}', 1),
            (" ", sessions.MAX_ANSWER_BYTES),
            ("}", 1),
        ),
        404,
    ),
)

# --- the token file of the reader (contract 02 §3 rule 5) -------------------------------

#: A token of 43 characters. It is the token of no deployment.
NOTICEBOARD_TOKEN: Final = "vectors-view-token-0123456789abcdefghijklmn"


@dataclass(frozen=True)
class TokenCase:
    """One token file."""

    id: str
    #: The bytes of the file. None stands for a path with no file.
    raw: bytes | None


_V: Final = NOTICEBOARD_TOKEN.encode("ascii")

TOKENS: Final[tuple[TokenCase, ...]] = (
    TokenCase("token", _V),
    TokenCase("final-line-feed", _V + b"\n"),
    TokenCase("empty", b""),
    TokenCase("spaces-only", b"   "),
    TokenCase("line-feed-only", b"\n"),
    TokenCase("not-utf8", b"\xff" + _V),
    TokenCase("absent", None),
)

# --- the audit file ---------------------------------------------------------------------

_LINE_CAP: Final = auditfiles.MAX_LINE_BYTES
_TAIL_CAP: Final = auditfiles.MAX_TAIL_BYTES


@dataclass(frozen=True)
class AuditCase:
    """One audit file and one call of the reader."""

    id: str
    #: The bytes of the file. None stands for a path with no file.
    file: Body | None
    family: str = ""
    tool: str = ""
    decision: str = ""
    offset: int = 0
    limit: int = 50


def _file(*records: object) -> Body:
    """The audit file: each record on one line, oldest first."""
    return Body(AUDIT_FILE, b"".join(_json(record) + b"\n" for record in records))


def _one(case_id: str, **fields: Any) -> AuditCase:
    """A file that holds one record."""
    return AuditCase(case_id, _file(_audit(**fields)))


def _numbered(count: int) -> tuple[dict[str, object], ...]:
    """`count` calls of one record each. The call number counts from 1."""
    return tuple(_audit(number) for number in range(1, count + 1))


def _tail(extra: int) -> Body:
    """A file of `_TAIL_CAP + extra` bytes: one line of spaces, then two
    records. The reader skips a line of spaces."""
    line = _json(_audit(app="code")) + b"\n"
    spaces = _TAIL_CAP + extra - 2 * len(line) - 1

    return _long(AUDIT_FILE, (" ", spaces), ("\n", 1), (line.decode("utf-8"), 2))


_FIVE: Final = _file(*_numbered(5))
_MIXED: Final = _file(*QUERY_RECORDS)
_RECORD: Final = _json(_audit())
_OTHER_RECORD: Final = _json(_audit(2, app="code"))

#: The three lines of `writes_safe_records_with_mode_0600` in
#: `plugins/default/audit-jsonl/src/lib.rs` of the chaperone repository.
_RUST: Final = (
    _audit(
        started="2025-10-24T00:00:00.123Z",
        time="2025-10-24T00:00:00.123Z",
        app=None,
        tool=auditfiles.UNKNOWN_TOOL,
    ),
    _audit(
        event="failed",
        detail="not_connected",
        started="2025-10-24T00:00:00.123Z",
        time="2025-10-24T00:01:00.123Z",
        app=None,
        tool=auditfiles.UNKNOWN_TOOL,
    ),
    _audit(
        2,
        detail="deny:tool_not_granted",
        started="2025-10-24T00:00:00.123Z",
        time="2025-10-24T00:00:00.123Z",
        app=None,
        tool=auditfiles.UNKNOWN_TOOL,
    ),
)

_GATED: Final = (
    _audit(1, detail=f"gate:{GATE}"),
    _audit(1, "waiting", f"gate:{GATE}"),
    _audit(2),
    _audit(2, "ran", "is_error:false"),
    _audit(1, "ran", "is_error:true"),
)

AUDITS: Final[tuple[AuditCase, ...]] = (
    AuditCase("one-record", _file(_audit())),
    AuditCase("no-file", None),
    AuditCase("empty-file", Body(AUDIT_FILE, b"")),
    AuditCase("rust-fixture", _file(*_RUST)),
    # --- the records of one call ---
    AuditCase("gated-calls", _file(*_GATED)),
    AuditCase("delivered", _file(_audit(3), _audit(3, "delivered", f"gate:{GATE}"))),
    AuditCase("decision-not-read", _file(_audit(4, "ran", "is_error:false"))),
    AuditCase("two-runs-one-call-number", _file(_audit(), _audit(run=OTHER_RUN))),
    AuditCase(
        "records-that-disagree",
        _file(_audit(), _audit(event="ran", detail="is_error:false", tool="mail__send")),
    ),
    AuditCase("failed-with-no-decision", _file(_audit(event="failed", detail="halted"))),
    # --- the order and the page ---
    AuditCase("five-calls", _FIVE),
    AuditCase("limit-2", _FIVE, limit=2),
    AuditCase("limit-2-offset-2", _FIVE, limit=2, offset=2),
    AuditCase("limit-2-offset-4", _FIVE, limit=2, offset=4),
    AuditCase("limit-5", _FIVE, limit=5),
    AuditCase("offset-5", _FIVE, offset=5),
    AuditCase("offset-past-the-end", _FIVE, offset=50),
    # --- the three filters ---
    AuditCase("filter-family", _MIXED, family="chat"),
    AuditCase("filter-family-part-of-a-name", _MIXED, family="cha"),
    AuditCase("filter-family-no-app", _file(_audit(app=None)), family="chat"),
    AuditCase("filter-decision", _MIXED, decision="deny"),
    AuditCase("filter-decision-part-of-a-word", _MIXED, decision="den"),
    AuditCase("filter-decision-gate", _file(*_GATED), decision="gate"),
    AuditCase(
        "filter-decision-not-read",
        _file(_audit(event="ran", detail="is_error:false")),
        decision="allow",
    ),
    AuditCase("filter-tool-part-of-a-name", _MIXED, tool="kagi"),
    AuditCase("filter-tool-upper-case", _MIXED, tool="KAGI"),
    AuditCase("filter-each", _MIXED, family="chat", tool="fetch", decision="allow"),
    AuditCase("filter-no-record", _MIXED, family="code", decision="deny"),
    AuditCase("filter-and-offset", _MIXED, family="chat", offset=1),
    AuditCase(
        "filter-keeps-a-line-that-is-not-json",
        Body(AUDIT_FILE, _RECORD + b"\nnot json\n" + _OTHER_RECORD + b"\n"),
        family="vault",
    ),
    # --- where a line ends ---
    AuditCase("no-final-line-feed", Body(AUDIT_FILE, _RECORD + b"\n" + _OTHER_RECORD)),
    AuditCase(
        "blank-lines",
        Body(AUDIT_FILE, b"\n\n" + _RECORD + b"\n  \n\t\n" + _OTHER_RECORD + b"\n\n"),
    ),
    AuditCase("cr-lf", Body(AUDIT_FILE, _RECORD + b"\r\n" + _OTHER_RECORD + b"\r\n")),
    AuditCase("form-feed-before-the-record", Body(AUDIT_FILE, b"\x0c" + _RECORD + b"\n")),
    _one("line-separator-inside-a-text", detail="deny:a\u2028b"),
    # --- a line that the reader does not parse ---
    AuditCase("line-not-json", Body(AUDIT_FILE, _RECORD + b"\nnot json\n" + _OTHER_RECORD + b"\n")),
    AuditCase("line-list", Body(AUDIT_FILE, b"[1]\n")),
    AuditCase("line-null", Body(AUDIT_FILE, b"null\n")),
    AuditCase("line-not-utf8", Body(AUDIT_FILE, b'{"app": "\xff"}\n')),
    AuditCase(
        "line-at-the-size-cap",
        _long(AUDIT_FILE, ('{"tool":"', 1), ("t", _LINE_CAP - 11), ('"}\n', 1)),
    ),
    AuditCase(
        "line-over-the-size-cap",
        _long(AUDIT_FILE, ('{"tool":"', 1), ("t", _LINE_CAP - 10), ('"}\n', 1)),
    ),
    # --- the keys of a record ---
    AuditCase("record-empty-object", Body(AUDIT_FILE, b"{}\n")),
    _one("unknown-key", verb="call"),
    AuditCase(
        "key-missing",
        _file({key: value for key, value in _audit().items() if key != "detail"}),
    ),
    # --- each field of a record ---
    _one("run-15-bytes", run="07" * 15),
    _one("run-upper-case", run="AB" * 16),
    _one("call-zero", call=0),
    _one("call-negative", call=-1),
    _one("call-true", call=True),
    _one("call-fraction", call=1.0),
    _one("call-at-the-64-bit-limit", call=I64_MAX),
    _one("started-with-no-offset", started="2026-09-19T11:59:30"),
    _one("started-with-an-offset", started="2026-09-19T13:59:30+02:00"),
    _one("time-number", time=7),
    _one("app-null", app=None),
    _one("app-empty", app=""),
    _one("app-upper-case", app="Chat"),
    _one("app-64-characters", app="a" * 64),
    _one("app-65-characters", app="a" * 65),
    _one("tool-unknown", tool=auditfiles.UNKNOWN_TOOL),
    _one("tool-no-separator", tool="kagi"),
    _one("tool-upper-case-upstream", tool="Kagi__search"),
    _one("tool-upper-case-tool", tool="kagi__Search.v2"),
    _one("digest-short", arguments_sha256="00"),
    _one("digest-upper-case", arguments_sha256=NO_ARGUMENTS.upper()),
    _one("event-unknown", event="allowed"),
    _one("event-upper-case", event="DECIDED"),
    # --- the detail of each event ---
    _one("decided-deny", detail="deny:argument_fenced"),
    _one("decided-gate", detail=f"gate:{GATE}"),
    _one("decided-deny-no-code", detail="deny:"),
    _one("decided-gate-short", detail="gate:0a"),
    _one("decided-permit", detail="permit"),
    _one("ran-is-error-yes", event="ran", detail="is_error:yes"),
    _one("ran-allow", event="ran", detail="allow"),
    _one("waiting-no-gate", event="waiting", detail="allow"),
    _one("delivered-gate-upper-case", event="delivered", detail=f"gate:{GATE.upper()}"),
    _one("failed-code", event="failed", detail="upstream_timeout"),
    _one("failed-words", event="failed", detail="Not Connected"),
    _one("detail-number", detail=5),
    # --- the tail of the file ---
    AuditCase("file-at-the-tail-cap", _tail(0)),
    AuditCase("file-over-the-tail-cap", _tail(1)),
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
    "NOTICEBOARD_STATE_ROOT": f"{NO_ROOT}/state",
    "NOTICEBOARD_REGISTRY_DIR": f"{NO_ROOT}/registry",
    "NOTICEBOARD_ATTENDANCE_SOCKET": f"{NO_ROOT}/attendance.sock",
}

_BIND: Final = f"NOTICEBOARD_BIND={LOOPBACK}\n"


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
    _env("bind-and-port", _BIND + "NOTICEBOARD_PORT=8371\n"),
    _env("bind-only", _BIND),
    _env("no-final-line-end", _BIND + "NOTICEBOARD_PORT=8371"),
    _env("cr-lf", f"NOTICEBOARD_BIND={LOOPBACK}\r\nNOTICEBOARD_PORT=8371\r\n"),
    _env("empty-file", ""),
    EnvFileCase("absent", None),
    # --- the caller and the file ---
    _env("caller-alone", "", NOTICEBOARD_BIND=LOOPBACK, NOTICEBOARD_PORT="8372"),
    _env(
        "file-wins-over-the-caller",
        "NOTICEBOARD_PORT=8371\n",
        NOTICEBOARD_BIND=LOOPBACK,
        NOTICEBOARD_PORT="8372",
    ),
    _env("file-and-caller", "NOTICEBOARD_PORT=8371\n", NOTICEBOARD_BIND=LOOPBACK),
    _env(
        "empty-value-wins-over-the-caller",
        "NOTICEBOARD_PORT=\n",
        NOTICEBOARD_BIND=LOOPBACK,
        NOTICEBOARD_PORT="8372",
    ),
    # --- a line that holds no variable ---
    _env(
        "comment-and-blank-lines",
        "# the bind\n\n   \n" + _BIND + "  # indented\nNOTICEBOARD_PORT=8371\n",
    ),
    _env("comment-holds-a-variable", _BIND + "#NOTICEBOARD_PORT=8371\n"),
    _env("no-equals", _BIND + "NOTICEBOARD_PORT\n"),
    _env("no-name", _BIND + "=8371\n"),
    _env("export", _BIND + "export NOTICEBOARD_PORT=8371\n"),
    _env("space-before-the-equals", _BIND + "NOTICEBOARD_PORT =8371\n"),
    _env("name-starts-with-a-digit", _BIND + "1NOTICEBOARD_PORT=8371\nNOTICEBOARD_PORT1=8371\n"),
    _env("name-with-a-hyphen", _BIND + "VIEW-PORT=8371\n"),
    _env("name-outside-ascii", _BIND + "NOTICEBOARD_P\u00d6RT=8371\n"),
    _env("name-lower-case", _BIND + "view_port=8371\n"),
    _env("byte-order-mark", "\ufeffNOTICEBOARD_PORT=8371\n" + _BIND),
    # --- the value ---
    _env("empty-value", _BIND + "NOTICEBOARD_PORT=\n"),
    _env("space-after-the-equals", _BIND + "NOTICEBOARD_PORT= 8371\n"),
    _env("spaces-around-the-line", "  " + _BIND + "\tNOTICEBOARD_PORT=8371  \n"),
    _env("u00a0-around-the-line", _BIND + "\u00a0NOTICEBOARD_PORT=8371\u00a0\n"),
    _env("double-quotes", _BIND + 'NOTICEBOARD_PORT="8371"\n'),
    _env("single-quotes", _BIND + "NOTICEBOARD_PORT='8371'\n"),
    _env("comment-after-the-value", _BIND + "NOTICEBOARD_PORT=8371 # the port\n"),
    _env(
        "equals-inside-the-value",
        f"AGENT_LAN_ADDRESS={LAN}\nNOTICEBOARD_ACCESS_KEY={NOTICEBOARD_KEY[:16]}={NOTICEBOARD_KEY[16:]}\n",
    ),
    _env("variable-two-times", _BIND + "NOTICEBOARD_PORT=8371\nNOTICEBOARD_PORT=8373\n"),
    _env("variable-two-times-last-empty", _BIND + "NOTICEBOARD_PORT=8371\nNOTICEBOARD_PORT=\n"),
    _env("site-address", f"AGENT_LAN_ADDRESS={LAN}\nNOTICEBOARD_ACCESS_KEY={NOTICEBOARD_KEY}\n"),
    # --- each other character that ends a line for `str.splitlines` ---
    _env(
        "vertical-tab-between-two-variables",
        f"NOTICEBOARD_BIND={LOOPBACK}\x0bNOTICEBOARD_PORT=8371\n",
    ),
    _env(
        "form-feed-between-two-variables", f"NOTICEBOARD_BIND={LOOPBACK}\x0cNOTICEBOARD_PORT=8371\n"
    ),
    _env("u001c-between-two-variables", f"NOTICEBOARD_BIND={LOOPBACK}\x1cNOTICEBOARD_PORT=8371\n"),
    _env("u0085-between-two-variables", f"NOTICEBOARD_BIND={LOOPBACK}\x85NOTICEBOARD_PORT=8371\n"),
    _env(
        "u2028-between-two-variables", f"NOTICEBOARD_BIND={LOOPBACK}\u2028NOTICEBOARD_PORT=8371\n"
    ),
    _env("cr-between-two-variables", f"NOTICEBOARD_BIND={LOOPBACK}\rNOTICEBOARD_PORT=8371\n"),
    # --- a config that the hook refuses ---
    _env("no-bind", "NOTICEBOARD_PORT=8371\n"),
    _env("port-word", _BIND + "NOTICEBOARD_PORT=http\n"),
    _env("port-zero", _BIND + "NOTICEBOARD_PORT=0\n"),
    _env("wildcard-bind", "NOTICEBOARD_BIND=0.0.0.0\n"),
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
    return CheckCase(case_id, {"NOTICEBOARD_BIND": LOOPBACK, **NO_PATHS, **variables})


_ROOT_PATHS: Final[dict[str, str]] = {
    "NOTICEBOARD_BIND": LOOPBACK,
    "NOTICEBOARD_STATE_ROOT": f"{ROOT}/state",
    "NOTICEBOARD_REGISTRY_DIR": f"{ROOT}/registry",
    "NOTICEBOARD_ATTENDANCE_SOCKET": f"{ROOT}/attendance.sock",
}
_KEY_FILE: Final = "view.key"
_KEY_FILE_VARIABLE: Final = {"NOTICEBOARD_ACCESS_KEY_FILE": f"{ROOT}/{_KEY_FILE}"}


def _key_file(case_id: str, raw: bytes | None, **variables: str) -> CheckCase:
    """An environment that names a key file. None stands for a path with no file."""
    files = {} if raw is None else {_KEY_FILE: raw}

    return CheckCase(
        case_id, {**_check(case_id, **variables).variables, **_KEY_FILE_VARIABLE}, files=files
    )


_LAN_BIND: Final = {"AGENT_LAN_ADDRESS": LAN}

CHECKS: Final[tuple[CheckCase, ...]] = (
    _check("loopback-no-key"),
    _check("loopback-short-key", NOTICEBOARD_ACCESS_KEY=SHORT_KEY),
    _check("loopback-ipv6", NOTICEBOARD_BIND="::1"),
    _check("loopback-name", NOTICEBOARD_BIND="localhost"),
    CheckCase("lan-with-key", {**NO_PATHS, **_LAN_BIND, "NOTICEBOARD_ACCESS_KEY": NOTICEBOARD_KEY}),
    CheckCase(
        "bind-wins-over-the-site-address",
        {
            **NO_PATHS,
            **_LAN_BIND,
            "NOTICEBOARD_BIND": "192.0.2.11",
            "NOTICEBOARD_ACCESS_KEY": NOTICEBOARD_KEY,
        },
    ),
    _check("port", NOTICEBOARD_PORT="18370"),
    _check("page-size", NOTICEBOARD_PAGE_SIZE="25"),
    _check("cookie-off", NOTICEBOARD_COOKIE_SECURE="0"),
    _check("cookie-on", NOTICEBOARD_COOKIE_SECURE="1"),
    _check("url-wins-over-the-socket", NOTICEBOARD_ATTENDANCE_URL="http://192.0.2.10:8350"),
    _check("another-variable", NOTICEBOARD_UNKNOWN="1", OTHER_PORT="9"),
    # --- paths that exist ---
    CheckCase(
        "each-path-present",
        _ROOT_PATHS,
        ("state/families", "state/audit", "state/outcomes", "state/tokens", "registry"),
        {
            "state/tokens/view-ro.token": NOTICEBOARD_TOKEN.encode("ascii") + b"\n",
            "attendance.sock": b"",
        },
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
    _key_file("key-file", NOTICEBOARD_KEY.encode("ascii")),
    _key_file("key-file-final-line-feed", NOTICEBOARD_KEY.encode("ascii") + b"\n"),
    _key_file(
        "key-file-on-the-lan",
        NOTICEBOARD_KEY.encode("ascii") + b"\n",
        NOTICEBOARD_BIND="192.0.2.11",
    ),
    _key_file(
        "key-file-wins-over-the-variable",
        NOTICEBOARD_KEY.encode("ascii"),
        NOTICEBOARD_ACCESS_KEY=SHORT_KEY,
    ),
    _key_file("key-file-empty-on-loopback", b""),
    _key_file("key-file-spaces-on-loopback", b"  \n"),
    _key_file("key-file-short-on-loopback", SHORT_KEY.encode("ascii")),
    _key_file("key-file-empty-on-the-lan", b"", NOTICEBOARD_BIND="192.0.2.11"),
    _key_file(
        "key-file-empty-wins-over-the-variable",
        b"",
        NOTICEBOARD_BIND="192.0.2.11",
        NOTICEBOARD_ACCESS_KEY=NOTICEBOARD_KEY,
    ),
    _key_file(
        "key-file-short-on-the-lan", SHORT_KEY.encode("ascii"), NOTICEBOARD_BIND="192.0.2.11"
    ),
    _key_file("key-file-absent", None),
    _key_file("key-file-not-utf8", b"\xff" + NOTICEBOARD_KEY.encode("ascii")),
    # --- a config that the service refuses ---
    CheckCase("no-bind", dict(NO_PATHS)),
    CheckCase("lan-no-key", {**NO_PATHS, **_LAN_BIND}),
    CheckCase("lan-short-key", {**NO_PATHS, **_LAN_BIND, "NOTICEBOARD_ACCESS_KEY": SHORT_KEY}),
    _check("wildcard-bind", NOTICEBOARD_BIND="0.0.0.0"),
    _check("port-word", NOTICEBOARD_PORT="http"),
    _check("port-zero", NOTICEBOARD_PORT="0"),
    _check("page-size-501", NOTICEBOARD_PAGE_SIZE="501"),
)
