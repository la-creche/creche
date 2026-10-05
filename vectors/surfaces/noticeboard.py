"""The noticeboard: its perimeter, its readers and its two programs (the group `noticeboard`).

The surfaces, by what each one pins:

- `noticeboard.security.key`, `.csrf` and `.keyless`: the three checks of
  `noticeboard.security`.
- `noticeboard.security.cookie`: one `Cookie` header to the token of the
  edit form and to the `Set-Cookie` header of the answer.
- `noticeboard.urlform`: the bytes of a form body to its fields.
- `noticeboard.app.query`: the query of the audit page to its four filters
  and to the count of records that the page shows.
- `noticeboard.route.family` and `noticeboard.route.session`: one route
  parameter to accepted or refused.
- `noticeboard.sessions.list`, `.detail`, `.events`, `.refusal` and
  `.token`: one answer of `attendance`, or one token file, to what the
  reader of the noticeboard makes of it.
- `noticeboard.transcript.fold`: one event stream to the entries of a
  transcript.
- `noticeboard.audit.page`: one audit directory and one call to one page.
- `noticeboard.statusdocs.report`: one validation report to its issues.
- `noticeboard.verify.envfile`: one env file to the first check of the
  verify hook.
- `noticeboard.cli.check`: one environment to the lines of
  `noticeboard --check`.
- `noticeboard.static.css`: the bytes of the stylesheet.

`noticeboard_cases.py` holds the inputs. A surface that needs a request
gives the app the bytes as a server does: the raw path, the path with each
percent escape decoded, the bytes of the query and the bytes of a header. A
test client changes a path and a header before the app gets them.

A problem text of the noticeboard can hold a message of the interpreter. A
vector holds the class of each problem, and the text only when the text is
the same on each machine (`problem_of`).

Each file of an input is in a temporary directory. No path of that
directory goes into a vector.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import io
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, cast
from unittest.mock import patch
from urllib.parse import parse_qsl, unquote

import httpx
from fastapi import FastAPI
from noticeboard.app import STATIC, build_app
from noticeboard.auditfiles import AuditFilter, AuditRow
from noticeboard.config import DEFAULT_PAGE_SIZE, DEFAULT_PORT, SOCKET_BASE_URL, Config
from noticeboard.security import CSRF_COOKIE, CSRF_FIELD
from noticeboard.sessions import Reply, SessionReader, SessionRow, TurnRow
from starlette.types import ASGIApp, Message

from noticeboard import __main__ as main_module
from noticeboard import auditfiles, security, sessions, statusdocs, transcript, verify
from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    normalize,
    quiet_logs,
    raised,
    refused,
    text_input,
)
from vectors.surfaces import noticeboard_cases as cases
from vectors.surfaces.session_cases import FAMILY, SESSION, Body

GROUP: Final = "noticeboard"

PERIMETER: Final = "spec.md §8.3"
PAGES: Final = "spec.md §8.1"
SESSION_API: Final = "contract 02"

HTTP_OK: Final = 200
HTTP_NOT_FOUND: Final = 404

MODE_OWNER: Final = 0o600

#: The start of the name of the temporary directory. No vector holds a path
#: of that directory, so no vector holds this text.
SCRATCH_PREFIX: Final = "vectors-noticeboard-"


def _surface(
    tail: str,
    entry: str,
    contract: str,
    notes: tuple[str, ...],
    vectors: tuple[Vector, ...],
    context: dict[str, Json] | None = None,
) -> Surface:
    return Surface(
        name=f"{GROUP}.{tail}",
        path=f"{GROUP}/{tail}.json",
        entry=entry,
        contract=contract,
        notes=notes,
        context={} if context is None else context,
        vectors=vectors,
    )


def _bytes_json(raw: bytes | None) -> Json:
    """Bytes inside an input: a text when they are UTF-8, else a marker. None stays None."""
    if raw is None:
        return None

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return normalize(raw)


def _text_or_bytes(raw: bytes) -> str | bytes:
    """Bytes inside a value: a text when they are UTF-8. `normalize` makes the marker."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw


def _fields(value: object) -> dict[str, Json]:
    """A dataclass of the noticeboard as an object of its fields."""
    return cast("dict[str, Json]", normalize(value))


_NOTE_BYTES: Final = (
    "A text stands for the UTF-8 bytes of the text, a $base64 marker holds bytes that are "
    "not UTF-8, and null stands for a request with no such header."
)
_NOTE_LATIN: Final = "The web framework decodes the bytes of a header as Latin-1."

# --- the class of a problem -------------------------------------------------------------


class Kind(enum.Enum):
    """The class of one problem that a reader of the noticeboard gives a page."""

    NONE = "none"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too large"
    NOT_JSON = "not JSON"
    NOT_AN_OBJECT = "not an object"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"
    #: A notice: the reader stopped at a limit and kept what it read before.
    CAPPED = "capped"


#: Where the message of the JSON reader starts in a problem text.
_NOT_JSON_MARK: Final = " is not JSON: "

#: The start of each problem text that has one class, whatever follows.
_STARTS: Final[tuple[tuple[str, Kind], ...]] = (
    ("attendance refused: ", Kind.REFUSED),
    ("cannot reach attendance: ", Kind.UNREACHABLE),
    ("cannot call attendance: ", Kind.UNREACHABLE),
    ("cannot read the noticeboard-ro token: ", Kind.UNREADABLE),
    ("the noticeboard-ro token file is ", Kind.UNREADABLE),
    ("cannot list the audit directory: ", Kind.UNREADABLE),
    ("attendance answered over ", Kind.TOO_LARGE),
    ("the stream passed ", Kind.CAPPED),
    ("stopped after ", Kind.CAPPED),
    ("showing the newest ", Kind.CAPPED),
)

#: The end of each problem text that has one class, whatever comes before.
_ENDS: Final[tuple[tuple[str, Kind], ...]] = (
    (" with no error code", Kind.REFUSED),
    (", not a JSON object", Kind.NOT_AN_OBJECT),
    (" is missing", Kind.MISSING),
    (" bytes; refusing to parse it", Kind.TOO_LARGE),
    (" bytes and was not parsed", Kind.TOO_LARGE),
)

_KINDS: Final = [kind.value for kind in Kind]
_NOTE_PROBLEM: Final = (
    f"A problem is an object. class is one of {', '.join(_KINDS[:-1])} and {_KINDS[-1]}. "
    f"{Kind.CAPPED.value} is a notice: the reader stopped at a limit and kept what it read "
    "before."
)
_NOTE_PROBLEM_TEXT: Final = (
    f"text is the sentence that a page shows. A problem of the class {Kind.NOT_JSON.value} "
    "has no text: its sentence ends with a message of the JSON reader of Python. It has "
    "start, the part of the sentence before that message."
)


def problem_of(text: str) -> dict[str, Json]:
    """One problem text of the noticeboard as the object that a vector keeps.

    A text that this function does not know stops the generator. A new
    sentence of the noticeboard then gets a class here before a vector
    holds it.
    """
    if not text:
        return {"class": Kind.NONE.value}

    for start, kind in _STARTS:
        if text.startswith(start):
            return {"class": kind.value, "text": text}

    if _NOT_JSON_MARK in text:
        head, mark, _ = text.partition(_NOT_JSON_MARK)
        return {"class": Kind.NOT_JSON.value, "start": head + mark}

    for end, kind in _ENDS:
        if text.endswith(end):
            return {"class": kind.value, "text": text}

    raise ValueError(f"no class for the problem text {text!r}")


# --- one request to the app -------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """One GET request, as the bytes that a server gives an app."""

    #: The path of the request line: ASCII, with each percent escape as sent.
    raw_path: bytes
    query: bytes = b""
    #: The value of the `Cookie` header. None stands for no such header.
    cookie: bytes | None = None


@dataclass(frozen=True)
class Answered:
    """The status, the headers and the body of one answer."""

    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes

    def values(self, name: bytes) -> list[str]:
        """The value of each header of this name, in the order of the answer."""
        return [value.decode("latin-1") for key, value in self.headers if key.lower() == name]


_CLIENT: Final = ("192.0.2.10", 50000)
_SERVER: Final = (cases.HOST, 80)


async def _asgi(app: ASGIApp, ask: Ask) -> Answered | Raised:
    """One request to an app, with no server and no client between the two.

    The scope is the one that the server of the noticeboard makes: `path` is
    the raw path with each percent escape decoded as UTF-8. Only the call of
    the app can give a `Raised`. A fault of a step of the generator stops
    the generator.
    """
    sent: list[Message] = []
    waiting: list[Message] = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> Message:
        return waiting.pop(0) if waiting else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    headers = [(b"host", cases.HOST.encode("ascii"))]
    if ask.cookie is not None:
        headers.append((b"cookie", ask.cookie))

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(ask.raw_path.decode("ascii")),
        "raw_path": ask.raw_path,
        "query_string": ask.query,
        "root_path": "",
        "headers": headers,
        "client": _CLIENT,
        "server": _SERVER,
    }
    try:
        await app(scope, receive, send)
    except Exception as exc:
        return Raised(exc)

    starts = [message for message in sent if message["type"] == "http.response.start"]
    if len(starts) != 1:
        raise ValueError(f"GET {ask.raw_path!r}: the app started {len(starts)} answers")

    body = b"".join(
        cast("bytes", message.get("body", b""))
        for message in sent
        if message["type"] == "http.response.body"
    )
    answered = cast("list[tuple[bytes, bytes]]", starts[0].get("headers", []))

    return Answered(cast("int", starts[0]["status"]), tuple(answered), body)


def _get(app: ASGIApp, ask: Ask) -> Answered | Raised:
    with quiet_logs():
        return asyncio.run(_asgi(app, ask))


class _Page(HTMLParser):
    """What a vector keeps of one page: each `input`, and the count of `pre` elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: dict[str, str] = {}
        self.records = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "pre":
            self.records += 1

        if tag != "input":
            return

        found = dict(attrs)
        name = found.get("name")
        if name is None:
            return

        if name in self.inputs:
            raise ValueError(f"the page has two inputs with the name {name}")

        self.inputs[name] = found.get("value") or ""


def _page(answer: Answered) -> _Page:
    """The page of an answer. Another status than 200 stops the generator."""
    if answer.status != HTTP_OK:
        raise ValueError(f"the app answers {answer.status} where a page must be")

    page = _Page()
    page.feed(answer.body.decode("utf-8"))
    page.close()

    return page


# --- attendance and the service ---------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One call that the reader made to `attendance`."""

    path: str
    params: dict[str, str]
    bearer: str

    def as_json(self) -> dict[str, Json]:
        """The call with no token: the path and the parameters of the query."""
        return {"path": self.path, "params": dict(self.params)}


@dataclass
class StandIn:
    """Stands for `attendance`: one answer for each call. It keeps each call."""

    reply: Reply
    calls: list[Call] = field(default_factory=list[Call])

    def get(self, path: str, params: Mapping[str, str], bearer: str) -> Reply:
        self.calls.append(Call(path, dict(params), bearer))

        return self.reply

    def only_call(self) -> Call:
        """The one call of a reader. Another count stops the generator."""
        if len(self.calls) != 1:
            raise ValueError(f"the reader made {len(self.calls)} calls")

        return self.calls[0]


def _write(path: Path, raw: bytes, mode: int = MODE_OWNER) -> Path:
    """A file with these bytes and this mode. The umask does not set the mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)

    return path


def _token_file(scratch: Path) -> Path:
    """The token file of each reader that a surface does not give another one."""
    return _write(scratch / "view-ro.token", cases.VIEW_TOKEN.encode("ascii") + b"\n")


def _answered(answer: cases.Answer) -> StandIn:
    return StandIn(Reply(status=answer.status, body=answer.body.data()))


#: What `attendance` of an app answers each call with: no session.
_NO_SESSION: Final = Reply(status=HTTP_OK, body=b'{"sessions":[]}')


def _app(scratch: Path, *, cookie_secure: bool = True) -> FastAPI:
    """The service on loopback with no key, over a state root of its own."""
    config = Config(
        bind=cases.LOOPBACK,
        port=DEFAULT_PORT,
        state_root=scratch / "state",
        registry_dir=scratch / "registry",
        attendance_socket=None,
        attendance_url=SOCKET_BASE_URL,
        page_size=DEFAULT_PAGE_SIZE,
        cookie_secure=cookie_secure,
    )
    reader = SessionReader(transport=StandIn(_NO_SESSION), token_file=_token_file(scratch))

    return build_app(config, reader)


_NOTE_APP: Final = (
    "The service of the generator binds loopback and has no access key. Its state root and "
    "its registry are empty directories, and its attendance answers each call with no session."
)
_NOTE_SERVER: Final = (
    "The generator gives the app the request with no server and no client between the two. "
    "The app gets the raw path, the path with each percent escape decoded as UTF-8, the "
    "bytes of the query and the bytes of each header, as it gets them from its server."
)

# --- the access key ---------------------------------------------------------------------


def _key_vector(case: cases.KeyCase) -> Vector:
    given: dict[str, Json] = {
        "args": {"offered": _bytes_json(case.offered), "expected": case.expected}
    }
    offered = None if case.offered is None else case.offered.decode("latin-1")
    outcome = attempt(lambda: security.check_key(offered, case.expected))
    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc)

    if outcome is None:
        return accepted(case.id, given)

    return refused(case.id, given, outcome)


def _key_surface() -> Surface:
    return _surface(
        "security.key",
        "noticeboard.security.check_key",
        f"{PERIMETER} rules 1 and 4",
        (
            f"The input is one request and the key of the service. args.offered is the value "
            f"of the {security.ACCESS_HEADER} header. {_NOTE_BYTES}",
            f"{_NOTE_LATIN} The generator makes that text and gives it to the entry point.",
            "args.expected is the key of the service. An empty text is a service on loopback "
            "with no key: the entry point lets each request through.",
            "An accepted vector is a request that can pass. refusal is the word of the answer: "
            "no_key or bad_key.",
            "The entry point removes no space. A server removes each space and each tab at "
            "the two ends of a header value before the app gets the value.",
        ),
        tuple(_key_vector(case) for case in cases.KEYS),
    )


# --- the form guard ---------------------------------------------------------------------


def _csrf_vector(case: cases.CsrfCase) -> Vector:
    given: dict[str, Json] = {
        "args": {
            "cookie": case.cookie,
            "field": case.field,
            "origin": case.origin,
            "referer": case.referer,
            "host": case.host,
        }
    }
    sender = security.Origin(origin=case.origin, referer=case.referer, host=case.host)
    outcome = attempt(lambda: security.check_csrf(case.cookie, case.field, sender))
    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc)

    if outcome is None:
        return accepted(case.id, given)

    return refused(case.id, given, outcome)


def _csrf_surface() -> Surface:
    return _surface(
        "security.csrf",
        "noticeboard.security.check_csrf",
        f"{PERIMETER} rules 3 and 4",
        (
            "The input is one form post, as the five texts that the entry point takes. "
            f"args.cookie is the value of the cookie {CSRF_COOKIE}. args.field is the value "
            f"of the form field {CSRF_FIELD}. args.origin, args.referer and args.host are "
            "the values of the Origin, Referer and Host headers. null stands for a value "
            "that the request does not hold.",
            "An accepted vector is a post that can change state. refusal is the word of the "
            "answer: no_token, bad_token or foreign_origin.",
            "The entry point reads the Origin header, and the Referer header only when the "
            "Origin header is absent or empty. It takes the part of that URL between the two "
            "slashes and the next slash, question mark or hash. It takes the whole text when "
            "that part is empty. It compares the result with args.host, character for "
            "character.",
            "The URL reader of Python removes each tab from a URL before it reads the URL.",
        ),
        tuple(_csrf_vector(case) for case in cases.CSRF),
    )


def _keyless_vector(vector_id: str, path: str) -> Vector:
    given = text_input(path)
    outcome = attempt(lambda: security.is_keyless(path))
    if isinstance(outcome, Raised):
        return raised(vector_id, given, outcome.exc)

    return accepted(vector_id, given) if outcome else refused(vector_id, given)


def _keyless_surface() -> Surface:
    return _surface(
        "security.keyless",
        "noticeboard.security.is_keyless",
        f"{PERIMETER} rule 1",
        (
            "The input is the path of one request, with each percent escape decoded.",
            "An accepted vector is a path that needs no access key. A refused vector is a "
            "path that needs the key.",
            "The entry point reads the text and no route. A path that needs no key can have "
            "no route.",
        ),
        tuple(_keyless_vector(vector_id, path) for vector_id, path in cases.KEYLESS),
    )


# --- the cookie of the form guard -------------------------------------------------------

#: Stands in a `Set-Cookie` header for a token that the service minted.
MINTED: Final = "$TOKEN"

#: The page whose form holds the token.
EDIT_PATH: Final = f"/families/{FAMILY}/edit"


def _form_token(app: ASGIApp, case: cases.CookieCase) -> tuple[str, list[str]] | Raised:
    """The token of the edit form and each `Set-Cookie` header, for one request."""
    answer = _get(app, Ask(EDIT_PATH.encode("ascii"), cookie=case.raw))
    if isinstance(answer, Raised):
        return answer

    return _page(answer).inputs[CSRF_FIELD], answer.values(b"set-cookie")


def _cookie_vector(app: ASGIApp, case: cases.CookieCase, secure: bool) -> Vector:
    vector_id = f"{case.id}.secure-{'on' if secure else 'off'}"
    given: dict[str, Json] = {"args": {"cookie": _bytes_json(case.raw)}}
    params: dict[str, Json] = {"cookie_secure": secure}
    first = _form_token(app, case)
    second = _form_token(app, case)
    if isinstance(first, Raised):
        return raised(vector_id, given, first.exc, params=params)

    if isinstance(second, Raised):
        return raised(vector_id, given, second.exc, params=params)

    token, headers = first
    if token == second[0]:
        # The service gives one request the same token two times only when it
        # reads the token from the request.
        return accepted(vector_id, given, {"token": token, "set_cookie": headers}, params=params)

    if not all(token in header for header in headers):
        raise ValueError(f"{vector_id}: the form holds a token that the cookie does not hold")

    marked = [header.replace(token, MINTED) for header in headers]

    return accepted(vector_id, given, {"token": None, "set_cookie": marked}, params=params)


def _cookie_surface(scratch: Path) -> Surface:
    apps = {
        secure: _app(scratch / f"cookie-{number}", cookie_secure=secure)
        for number, secure in enumerate((True, False))
    }

    return _surface(
        "security.cookie",
        f"noticeboard.app.build_app, the route GET {EDIT_PATH}",
        f"{PERIMETER} rule 3",
        (
            f"The input is one request for the edit page. args.cookie is the value of its "
            f"Cookie header. {_NOTE_BYTES}",
            "params.cookie_secure is the switch of the config for Secure on the cookie. Each "
            "input has one vector for each state of the switch.",
            f"value.token is the value of the form field {CSRF_FIELD} on the page. It is the "
            "token that the service read from the request. It is null when the service "
            "minted a token: such a token is random.",
            f"value.set_cookie is the value of each Set-Cookie header of the answer. The "
            f"text {MINTED} stands for a token that the service minted.",
            f"{_NOTE_LATIN} The service reads the last cookie of the name. It removes the "
            "whitespace of Python str.strip from the two ends of a name and of a value. It "
            "removes one pair of double quotes around a value, and it reads an escape of a "
            "backslash and three octal digits between them.",
            "The cookie writer of Python puts a value between double quotes when the value "
            "holds a character that is no letter, no digit and none of "
            f"{cases.PLAIN_MARKS}",
            "The generator sends each request two times. A token that differs between the "
            "two answers is a minted token.",
            _NOTE_APP,
            _NOTE_SERVER,
        ),
        tuple(
            _cookie_vector(apps[secure], case, secure)
            for case in cases.COOKIES
            for secure in (True, False)
        ),
        {"path": EDIT_PATH},
    )


# --- the form body ----------------------------------------------------------------------


def _form_pairs(raw: bytes) -> list[tuple[str, str]]:
    """The fields of a form body, as the edit route of the noticeboard reads them."""
    return parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)


def _form_vector(body: Body) -> Vector:
    given = body.given()
    raw = body.data()
    outcome = attempt(lambda: _form_pairs(raw))
    if isinstance(outcome, Raised):
        return raised(body.id, given, outcome.exc)

    pairs: list[Json] = [[name, value] for name, value in outcome]

    return accepted(body.id, given, {"pairs": pairs, "fields": dict(outcome)})


def _form_surface() -> Surface:
    return _surface(
        "urlform",
        "urllib.parse.parse_qsl, as the edit route of noticeboard.app calls it",
        "spec.md §8.2",
        (
            "The input is the bytes of one form body.",
            "The generator decodes the bytes as UTF-8 and puts U+FFFD in the place of each "
            "byte that is not UTF-8. It gives the text to parse_qsl with blank values kept. "
            "The edit route does the same two steps.",
            "value.pairs is each field of the body in its order: a name and a value. "
            "value.fields is the mapping that the route makes from the pairs: the last value "
            "of a name stays.",
            "The decode step comes before the percent step. A percent escape that is not "
            "UTF-8 also gives U+FFFD.",
        ),
        tuple(_form_vector(body) for body in cases.FORMS),
    )


# --- the query of the audit page --------------------------------------------------------

AUDIT_PATH: Final = "/audit"

#: The four filters of the audit page, in the order of its form.
FILTERS: Final = ("family", "session", "tool", "decision")


def _query_vector(app: ASGIApp, body: Body) -> Vector:
    given = body.given()
    answer = _get(app, Ask(AUDIT_PATH.encode("ascii"), query=body.data()))
    if isinstance(answer, Raised):
        return raised(body.id, given, answer.exc)

    page = _page(answer)
    filters: dict[str, Json] = {name: page.inputs[name] for name in FILTERS}

    return accepted(body.id, given, {"filters": filters, "rows": page.records})


def _query_surface(scratch: Path) -> Surface:
    home = scratch / "query"
    _write(home / "state" / "audit" / cases.QUERY_DAY, cases.QUERY_FILE)
    app = _app(home)

    return _surface(
        "app.query",
        f"noticeboard.app.build_app, the route GET {AUDIT_PATH}",
        PAGES,
        (
            "The input is the query of one request for the audit page: the bytes after the "
            "question mark. Each input is ASCII.",
            "The audit directory holds one day file, context.audit_file. It has three "
            "records, oldest first. The page size is context.page_size.",
            "value.filters is the value of each of the four inputs of the filter form on the "
            "page. value.rows is the count of records that the page shows.",
            "The service reads the last value of a name. It removes the whitespace of Python "
            "str.strip from the two ends of a filter value. Then it keeps the first 200 "
            "characters. A character is one code point.",
            "The service reads the offset with int of Python and reads a negative number as "
            "0. It reads a text that int refuses as 0. int refuses a text of more than 4300 "
            "digits.",
            "The filters family and decision take a record whose field equals the value. The "
            "filters tool and session take a record whose field holds the value.",
            _NOTE_APP,
            _NOTE_SERVER,
        ),
        tuple(_query_vector(app, body) for body in cases.QUERIES),
        {
            "path": AUDIT_PATH,
            "page_size": DEFAULT_PAGE_SIZE,
            "audit_file": {"name": cases.QUERY_DAY, "text": cases.QUERY_FILE.decode("utf-8")},
        },
    )


# --- the route parameters ---------------------------------------------------------------

#: The body of the answer for a path that has no route.
NOT_FOUND_BODY: Final = b'{"detail":"Not Found"}'

#: The three routes that take a family name. `{}` is the place of the segment.
FAMILY_ROUTES: Final[tuple[tuple[str, str], ...]] = (
    ("family", "/families/{}"),
    ("edit", "/families/{}/edit"),
    ("session", "/sessions/{}/owui-x"),
)


def _route_status(app: ASGIApp, path: str) -> int | Raised:
    answer = _get(app, Ask(path.encode("ascii")))
    if isinstance(answer, Raised):
        return answer

    if answer.status == HTTP_NOT_FOUND and answer.body != NOT_FOUND_BODY:
        raise ValueError(f"GET {path}: the 404 answer has another body")

    return answer.status


def _segment_vector(app: ASGIApp, vector_id: str, text: str) -> Vector:
    given = text_input(text)
    statuses: dict[str, Json] = {}
    for name, route in FAMILY_ROUTES:
        status = _route_status(app, route.format(text))
        if isinstance(status, Raised):
            return raised(vector_id, given, status.exc)

        statuses[name] = status

    found = set(statuses.values())
    if found == {HTTP_OK}:
        return accepted(vector_id, given, statuses)

    if found == {HTTP_NOT_FOUND}:
        return refused(vector_id, given, statuses)

    raise ValueError(f"{vector_id}: the three routes answer {statuses}")


def _segment_surface(scratch: Path) -> Surface:
    app = _app(scratch / "route")

    return _surface(
        "route.family",
        "noticeboard.app.build_app, the three routes of context.routes",
        PAGES,
        (
            "The input is one path segment, as a client sends it: ASCII, with a percent "
            "escape for each byte that needs one.",
            f"Each input of the surface {cases.NAME_SURFACE} is here as a segment, with the "
            "id of its vector there.",
            "The generator puts the segment into each path of context.routes, in the place "
            "of {}. It sends one GET request for each path.",
            "An accepted vector is a segment that each of the three routes takes as a family "
            "name: each answer has status 200. value is the status of each route. No family "
            "of that name exists, so each page holds a report.",
            "A refused vector is a segment that no route takes: each answer has status 404 "
            "and the body of context.not_found_body. refusal is the status of each route. "
            "That body is the answer of the web framework for a path that has no route.",
            "The web framework finds the route with the decoded path. A segment with an "
            "escaped slash is thus two segments.",
            _NOTE_APP,
            _NOTE_SERVER,
        ),
        tuple(_segment_vector(app, vector_id, text) for vector_id, text in cases.SEGMENTS),
        {
            "routes": {name: route for name, route in FAMILY_ROUTES},
            "not_found_body": NOT_FOUND_BODY.decode("ascii"),
        },
    )


def _session_id_vector(vector_id: str, text: str) -> Vector:
    given = text_input(text)
    outcome = attempt(lambda: sessions.is_session(text))
    if isinstance(outcome, Raised):
        return raised(vector_id, given, outcome.exc)

    return accepted(vector_id, given) if outcome else refused(vector_id, given)


def _session_id_surface() -> Surface:
    return _surface(
        "route.session",
        "noticeboard.sessions.is_session",
        f"{SESSION_API} §2",
        (
            "The input is one route parameter, with each percent escape decoded.",
            f"Each input of the surface {cases.SESSION_SURFACE} is here, with the id of its "
            "vector there.",
            "An accepted vector is a text that the entry point takes as a session id.",
            "The session route of noticeboard.app calls the entry point. It answers status 404 "
            "for a text that the entry point refuses. No vector holds that answer.",
        ),
        tuple(_session_id_vector(vector_id, text) for vector_id, text in cases.SESSION_IDS),
    )


# --- the three calls of the reader ------------------------------------------------------

_NOTE_ANSWER: Final = (
    "The input is the bytes of the body of one answer of attendance. params.status is the "
    "HTTP status of that answer."
)
_NOTE_REQUEST: Final = (
    "value.request is the call that the reader makes: the path, and each parameter of the "
    "query as text. The reader sends its token in a header and never in the query."
)
_NOTE_REFUSED: Final = (
    "A refused vector is an answer that the reader gets nothing from. refusal is the problem "
    "that a page shows."
)
_NOTE_LENIENT: Final = (
    f"A field of a wrong type reads as an empty text, as 0, as false or as absent. A text "
    f"has {cases.CUT} characters or less. A character is one code point."
)


#: A field of a session row that the reader never sets and no page shows.
_UNUSED_FIELD: Final = "problem"


def _session_json(row: SessionRow) -> dict[str, Json]:
    found = {key: value for key, value in _fields(row).items() if key != _UNUSED_FIELD}

    return {**found, "door": row.door}


def _turn_json(row: TurnRow) -> dict[str, Json]:
    usage: dict[str, Json] = {**_fields(row.usage), "tokens_text": row.usage.tokens_text}

    return {**_fields(row), "usage": usage, "running": row.running}


def _list_params(answer: cases.Answer) -> dict[str, Json]:
    return {"status": answer.status, "family": "", "limit": 50, "cursor": "", **answer.call}


def _list_vector(answer: cases.Answer, token_file: Path) -> Vector:
    given = answer.body.given()
    params = _list_params(answer)
    stand_in = _answered(answer)
    reader = SessionReader(transport=stand_in, token_file=token_file)
    outcome = attempt(lambda: reader.sessions(**answer.call))
    if isinstance(outcome, Raised):
        return raised(answer.id, given, outcome.exc, params=params)

    if outcome.problem:
        return refused(answer.id, given, problem_of(outcome.problem), params=params)

    rows: list[Json] = [_session_json(row) for row in outcome.rows]
    value: dict[str, Json] = {
        "request": stand_in.only_call().as_json(),
        "rows": rows,
        "next_cursor": outcome.next_cursor,
    }

    return accepted(answer.id, given, value, params=params)


def _unreachable_vector(token_file: Path) -> Vector:
    """The reader over a transport that reached no `attendance`."""
    given: dict[str, Json] = {"args": {"problem": cases.UNREACHABLE}}
    reader = SessionReader(
        transport=StandIn(Reply(problem=cases.UNREACHABLE)), token_file=token_file
    )
    outcome = attempt(reader.sessions)
    if isinstance(outcome, Raised):
        return raised("transport-problem", given, outcome.exc)

    return refused("transport-problem", given, problem_of(outcome.problem))


def _list_surface(token_file: Path) -> Surface:
    return _surface(
        "sessions.list",
        "noticeboard.sessions.SessionReader.sessions",
        f"{SESSION_API} §4.2, §5.2",
        (
            _NOTE_ANSWER,
            "params.family, params.limit and params.cursor are the arguments of the entry "
            "point. An empty family and an empty cursor are the defaults.",
            _NOTE_REQUEST,
            "value.rows is each session of the answer. door is the door that the reader "
            "derives: from the start of the session id first, and from the holder of the "
            "writer lease second.",
            f"The reader takes the first {sessions.MAX_SESSION_LIMIT} items of the list and "
            f"then drops each item that is no object. It takes the first "
            f"{sessions.MAX_LABELS} labels in the sorted order of their keys and then drops "
            "each label whose value is no text.",
            _NOTE_LENIENT,
            _NOTE_REFUSED,
            "The vector transport-problem has no answer: args.problem is the text that the "
            "transport gives the reader in the place of one. The reader gives a page the "
            "same text.",
            _NOTE_PROBLEM,
            _NOTE_PROBLEM_TEXT,
        ),
        (
            *(_list_vector(answer, token_file) for answer in cases.LISTS),
            _unreachable_vector(token_file),
        ),
    )


def _detail_vector(answer: cases.Answer, token_file: Path) -> Vector:
    given = answer.body.given()
    params: dict[str, Json] = {
        "status": answer.status,
        "family": FAMILY,
        "session": SESSION,
        "turns": sessions.DEFAULT_TURNS,
        **answer.call,
    }
    stand_in = _answered(answer)
    reader = SessionReader(transport=stand_in, token_file=token_file)
    outcome = attempt(lambda: reader.detail(FAMILY, SESSION, **answer.call))
    if isinstance(outcome, Raised):
        return raised(answer.id, given, outcome.exc, params=params)

    if outcome.session is None:
        return refused(answer.id, given, problem_of(outcome.problem), params=params)

    turns: list[Json] = [_turn_json(turn) for turn in outcome.turns]
    value: dict[str, Json] = {
        "request": stand_in.only_call().as_json(),
        "session": _session_json(outcome.session),
        "turns": turns,
    }

    return accepted(answer.id, given, value, params=params)


def _detail_surface(token_file: Path) -> Surface:
    return _surface(
        "sessions.detail",
        "noticeboard.sessions.SessionReader.detail",
        f"{SESSION_API} §4.2, §4.4, §5.3",
        (
            _NOTE_ANSWER,
            "params.family, params.session and params.turns are the arguments of the entry point.",
            _NOTE_REQUEST,
            "value.session is the session of the answer, as noticeboard.sessions.list gives "
            "one. value.turns is each turn of the answer.",
            "running is true for a turn in one of the three states that are not final. "
            "usage.tokens_text is the sum of the four counts of the usage, as a page shows "
            "it. The sum of four counts of 64 bits can need more than 64 bits.",
            f"The reader takes the first {sessions.MAX_TURNS} items of the list of turns and "
            "then drops each item that is no object.",
            _NOTE_LENIENT,
            _NOTE_REFUSED,
            _NOTE_PROBLEM,
            _NOTE_PROBLEM_TEXT,
        ),
        tuple(_detail_vector(answer, token_file) for answer in cases.DETAILS),
    )


def _events_vector(answer: cases.Answer, token_file: Path) -> Vector:
    given = answer.body.given()
    params: dict[str, Json] = {
        "status": answer.status,
        "family": FAMILY,
        "session": SESSION,
        "from_seq": 0,
        **answer.call,
    }
    stand_in = _answered(answer)
    reader = SessionReader(transport=stand_in, token_file=token_file)
    outcome = attempt(lambda: reader.events(FAMILY, SESSION, **answer.call))
    if isinstance(outcome, Raised):
        return raised(answer.id, given, outcome.exc, params=params)

    problems: list[Json] = [problem_of(problem) for problem in outcome.problems]
    value: dict[str, Json] = {
        "request": stand_in.only_call().as_json(),
        "lines": cast("list[Json]", normalize(outcome.lines)),
        "problems": problems,
        "truncated": outcome.truncated,
    }

    return accepted(answer.id, given, value, params=params)


def _events_surface(token_file: Path) -> Surface:
    return _surface(
        "sessions.events",
        "noticeboard.sessions.SessionReader.events",
        f"{SESSION_API} §5.5, §8",
        (
            _NOTE_ANSWER,
            "params.family, params.session and params.from_seq are the arguments of the "
            "entry point.",
            _NOTE_REQUEST,
            "value.lines is each line of the stream that is one JSON object, in the order of "
            "the stream. The reader checks no field of a line.",
            "The reader ends a line at LF only. It skips a line that holds only the six "
            "bytes of ASCII whitespace: space, tab, LF, CR, VT and FF.",
            "value.problems has one problem for each line that is no JSON object and one for "
            "each limit that the reader stopped at. value.truncated is true when the reader "
            "stopped at a limit.",
            f"The reader keeps the first {sessions.MAX_STREAM_BYTES} bytes of a stream and "
            f"the first {sessions.MAX_STREAM_LINES} lines that are a JSON object.",
            _NOTE_PROBLEM,
            _NOTE_PROBLEM_TEXT,
        ),
        tuple(_events_vector(answer, token_file) for answer in cases.STREAMS),
    )


def _refusal_text(answer: cases.Answer, token_file: Path) -> str | Raised:
    """The problem of one answer, which each of the three calls must give."""

    def ask() -> set[str]:
        reader = SessionReader(transport=_answered(answer), token_file=token_file)

        return {
            reader.sessions().problem,
            reader.detail(FAMILY, SESSION).problem,
            *reader.events(FAMILY, SESSION).problems,
        }

    outcome = attempt(ask)
    if isinstance(outcome, Raised):
        return outcome

    if len(outcome) != 1:
        raise ValueError(f"{answer.id}: the three calls give {sorted(outcome)}")

    return outcome.pop()


def _refusal_vector(answer: cases.Answer, token_file: Path) -> Vector:
    given = answer.body.given()
    params: dict[str, Json] = {"status": answer.status}
    text = _refusal_text(answer, token_file)
    if isinstance(text, Raised):
        return raised(answer.id, given, text.exc, params=params)

    return refused(answer.id, given, problem_of(text), params=params)


def _refusal_surface(token_file: Path) -> Surface:
    return _surface(
        "sessions.refusal",
        "noticeboard.sessions.SessionReader.sessions, .detail and .events",
        f"{SESSION_API} §14",
        (
            _NOTE_ANSWER,
            "Each vector is refused: the status is not 200. refusal is the problem that a "
            "page shows. Its text holds the code and the message of the error body, and the "
            "status.",
            "The reader checks the status before the size and before the form of the body. "
            "It reads a body that is no error body as a body with no code.",
            f"The reader keeps the first {cases.CUT} characters of the code and of the "
            "message. Then it removes the whitespace of Python str.rstrip from the end of "
            "the sentence.",
            "The generator makes the three calls of the reader for each answer. It stops "
            "when two calls give two sentences.",
            _NOTE_PROBLEM,
        ),
        tuple(_refusal_vector(answer, token_file) for answer in cases.REFUSALS),
    )


# --- the token file of the reader -------------------------------------------------------

#: The input of a vector whose path holds no file.
NO_FILE: Final[dict[str, Json]] = {"args": {"file": None}}


def _file_input(raw: bytes | None) -> dict[str, Json]:
    return NO_FILE if raw is None else bytes_input(raw)


def _token_vector(case: cases.TokenCase, scratch: Path) -> Vector:
    given = _file_input(case.raw)
    path = scratch / case.id / "view-ro.token"
    path.parent.mkdir(parents=True)
    if case.raw is not None:
        _write(path, case.raw)

    stand_in = StandIn(_NO_SESSION)
    reader = SessionReader(transport=stand_in, token_file=path)
    outcome = attempt(reader.sessions)
    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc)

    if not outcome.problem:
        return accepted(case.id, given, {"bearer": stand_in.only_call().bearer})

    if stand_in.calls:
        raise ValueError(f"{case.id}: the reader made a call with no token")

    return refused(case.id, given, problem_of(outcome.problem))


def _token_surface(scratch: Path) -> Surface:
    return _surface(
        "sessions.token",
        "noticeboard.sessions.SessionReader.sessions, the token file of the reader",
        f"{SESSION_API} §3 rules 4 and 5",
        (
            "The input is the bytes of the token file of the reader. The file has mode "
            "context.mode. args.file of null stands for a path with no file.",
            "The generator makes one call of the entry point over that file. An accepted "
            "vector is a file that the reader takes a token from. value.bearer is the token "
            "that the reader gives its transport.",
            "A refused vector is a file that the reader takes no token from. The reader then "
            "makes no call. refusal is the problem that a page shows.",
            "The reader reads the file as UTF-8 text, in the text mode of Python. That mode "
            "gives LF for each CR and for each CR LF. Then the reader removes the whitespace "
            "of Python str.strip from the two ends.",
            "The sentence for a path with no file ends with the text that the system gives "
            "for the error.",
            _NOTE_PROBLEM,
        ),
        tuple(_token_vector(case, scratch / "token") for case in cases.TOKENS),
        {"mode": f"{MODE_OWNER:04o}"},
    )


# --- the transcript ---------------------------------------------------------------------


def _folded(body: Body, token_file: Path) -> list[Json] | Raised:
    """The entries of one stream: the reader first, and `fold` on its lines."""
    reader = SessionReader(
        transport=StandIn(Reply(status=HTTP_OK, body=body.data())), token_file=token_file
    )
    stream = attempt(lambda: reader.events(FAMILY, SESSION))
    if isinstance(stream, Raised):
        return stream

    if stream.problems:
        raise ValueError(f"{body.id}: the reader has a problem with a line of the transcript")

    entries = attempt(lambda: transcript.fold(stream.lines))
    if isinstance(entries, Raised):
        return entries

    return [_fields(entry) for entry in entries]


def _transcript_vector(body: Body, token_file: Path) -> Vector:
    given = body.given()
    outcome = _folded(body, token_file)
    if isinstance(outcome, Raised):
        return raised(body.id, given, outcome.exc)

    return accepted(body.id, given, {"entries": outcome})


def _transcript_surface(token_file: Path) -> Surface:
    return _surface(
        "transcript.fold",
        "noticeboard.transcript.fold, on the lines of noticeboard.sessions.SessionReader.events",
        f"{SESSION_API} §8, §15.1",
        (
            "The input is the bytes of one event stream: each line is one JSON object and "
            "ends with LF. The surface noticeboard.sessions.events holds what the reader "
            "does with another line.",
            "value.entries is each entry of the transcript, oldest first. voice is one of "
            "prompt, answer, approval, note and failure.",
            "The text deltas of one turn make one answer. A line of that turn that is no pi "
            "event ends the answer, and the answer gets the time of that line. A heartbeat, "
            "a line of the kind terminal_exchange, a line of an unknown kind and a line of "
            "another turn do not end it. An answer with no such line comes after each other "
            "entry and has an empty time.",
            f"One delta has {transcript.MAX_DELTA_CHARS} characters or less. One answer has "
            f"{transcript.MAX_ANSWER_CHARS} characters or less, and truncated is true for an "
            "answer that had more.",
            f"The entry point reads no line after it has {transcript.MAX_ENTRIES} entries. "
            "Each answer with no end still makes an entry after that.",
            _NOTE_LENIENT,
        ),
        tuple(_transcript_vector(body, token_file) for body in cases.TRANSCRIPTS),
    )


# --- the audit files --------------------------------------------------------------------


def _audit_given(case: cases.AuditCase) -> dict[str, Json]:
    files: Json = None
    if case.files is not None:
        files = [{"name": one.id, **one.given()} for one in case.files]

    return {
        "args": {
            "files": files,
            "filter": {
                "family": case.family,
                "session": case.session,
                "tool": case.tool,
                "decision": case.decision,
            },
            "offset": case.offset,
            "limit": case.limit,
        }
    }


def _audit_row(row: AuditRow) -> dict[str, Json]:
    return {**_fields(row), "problem": problem_of(row.problem), "gated": row.gated}


def _audit_vector(case: cases.AuditCase, scratch: Path) -> Vector:
    given = _audit_given(case)
    directory = scratch / case.id
    if case.files is not None:
        directory.mkdir(parents=True)
        for one in case.files:
            (directory / one.id).write_bytes(one.data())

    wanted = AuditFilter(case.family, case.session, case.tool, case.decision)
    outcome = attempt(lambda: auditfiles.read_page(directory, wanted, case.offset, case.limit))
    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc)

    rows: list[Json] = [_audit_row(row) for row in outcome.rows]
    problems: list[Json] = [problem_of(problem) for problem in outcome.problems]
    value: dict[str, Json] = {
        **_fields(outcome),
        "rows": rows,
        "problems": problems,
        "previous_offset": outcome.previous_offset,
        "next_offset": outcome.next_offset,
    }

    return accepted(case.id, given, value)


def _audit_surface(scratch: Path) -> Surface:
    return _surface(
        "audit.page",
        "noticeboard.auditfiles.read_page",
        "contract 04 §6",
        (
            "The input is one audit directory and one call. args.files is each file of the "
            "directory: its name, and its bytes in one of the forms text, base64 and repeat. "
            "args.files of null stands for a path with no directory.",
            "args.filter, args.offset and args.limit are the arguments of the entry point. "
            "An empty filter value takes each record.",
            "value is the page. value.rows is its records, newest first: the reader reads "
            "the day files in the reversed order of their names, and the lines of one file "
            "from the last one to the first one.",
            "The reader ends a line at LF only. It skips a line that holds only the six "
            "bytes of ASCII whitespace. A line that is no JSON object makes a row with a "
            "problem, and each filter takes that row.",
            "args of a row is the arguments of the record as text: JSON with sorted keys, an "
            "indent of 2 spaces and each character outside ASCII as it is. A record with no "
            "arguments and a record whose arguments are null have an empty text. "
            f"args_truncated is true when the text had more than {auditfiles.MAX_ARGS_CHARS} "
            "characters.",
            f"chain is the first {auditfiles.MAX_CHAIN} items of the chain, less each item "
            "that is no text. gated is true for a row whose gate is not empty.",
            "value.scanned is the count of lines that the reader looked at. The reader "
            "counts a line before it reads it. value.has_more is true when the reader found "
            "one record more than the limit. value.days_read is each day file that the "
            f"reader found, {auditfiles.MAX_DAYS} at most.",
            _NOTE_LENIENT,
            "The sentence for a path with no directory ends with the text that the system "
            "gives for the error.",
            _NOTE_PROBLEM,
            _NOTE_PROBLEM_TEXT,
        ),
        tuple(_audit_vector(case, scratch / "audit") for case in cases.AUDITS),
    )


# --- the validation report --------------------------------------------------------------


def _report_vector(case: cases.ReportCase, scratch: Path) -> Vector:
    given = NO_FILE if case.body is None else case.body.given()
    path = scratch / case.id / cases.REPORT_NAME
    path.parent.mkdir(parents=True)
    if case.body is not None:
        path.write_bytes(case.body.data())

    outcome = attempt(lambda: statusdocs.read_report(path))
    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc)

    issues, problem = outcome
    if problem:
        return refused(case.id, given, problem_of(problem))

    return accepted(case.id, given, {"issues": [_fields(issue) for issue in issues]})


def _report_surface(scratch: Path) -> Surface:
    return _surface(
        "statusdocs.report",
        "noticeboard.statusdocs.read_report",
        "contract 01 §7, contract 05 §3.2",
        (
            "The input is the bytes of one validation report. The generator writes them to a "
            "file with the name context.name and gives the entry point the path of that "
            "file. args.file of null stands for a path with no file.",
            "value.issues is each issue of the report: the two texts that a page shows.",
            f"A field loc or msg that is no text reads as {statusdocs.NOT_TEXT}. A field "
            "that is absent reads as an empty text.",
            f"The reader takes the first {statusdocs.MAX_ISSUES} items of the list and then "
            "drops each item that is no object.",
            _NOTE_LENIENT,
            "A refused vector is a file that the reader gets no list of issues from. refusal is "
            "the problem that a page shows.",
            _NOTE_PROBLEM,
            _NOTE_PROBLEM_TEXT,
        ),
        tuple(_report_vector(case, scratch / "report") for case in cases.REPORTS),
        {"name": cases.REPORT_NAME},
    )


# --- the env file of the verify hook ----------------------------------------------------

#: The checks whose result the first check of the hook can be.
CHECK_CONFIG: Final = "config"
CHECK_ENV_FILE: Final = "env-file"


def _no_probe(url: str, *, timeout: float) -> httpx.Response:
    """Stands for the one HTTP call of the hook: the service does not run."""
    del timeout
    raise httpx.ConnectError(f"the generator opens no socket for {url}")


def _first_check(path: Path, environ: dict[str, str]) -> dict[str, Json] | Raised:
    """The first check that the hook reports for one env file and one environment."""
    out = io.StringIO()
    with (
        patch.dict(os.environ, environ, clear=True),
        patch.object(httpx, "get", _no_probe),
        contextlib.redirect_stdout(out),
    ):
        outcome = attempt(lambda: verify.main(["--json", "--env-file", str(path)]))

    if isinstance(outcome, Raised):
        return outcome

    report = cast("dict[str, list[dict[str, Json]]]", json.loads(out.getvalue()))

    return report["checks"][0]


def _env_file_vector(case: cases.EnvFileCase, scratch: Path) -> Vector:
    given = _file_input(case.raw)
    environ = {**cases.NO_PATHS, **case.inherited}
    params: dict[str, Json] = {"environ": dict(environ)}
    path = scratch / case.id / "view.env"
    path.parent.mkdir(parents=True)
    if case.raw is not None:
        path.write_bytes(case.raw)

    check = _first_check(path, environ)
    if isinstance(check, Raised):
        return raised(case.id, given, check.exc, params=params)

    name, detail = str(check["name"]), str(check["detail"])
    if name == CHECK_CONFIG and check["ok"] is True:
        return accepted(case.id, given, {"check": name, "detail": detail}, params=params)

    if name == CHECK_CONFIG:
        refusal: dict[str, Json] = {"check": name, "variable": detail.split()[0]}
        return refused(case.id, given, refusal, params=params)

    if name == CHECK_ENV_FILE and case.raw is None:
        return refused(case.id, given, {"check": name}, params=params)

    raise ValueError(f"{case.id}: the first check of the hook is {name}")


def _env_file_surface(scratch: Path) -> Surface:
    return _surface(
        "verify.envfile",
        "noticeboard.verify.main, with --json and --env-file",
        "contract 06 §4 rule 7",
        (
            "The input is the bytes of one env file. args.file of null stands for a path "
            "with no file. params.environ is each variable that the hook gets from its "
            "caller. A variable of the file wins over a variable of the caller.",
            "The value is from the first check that the hook reports. An accepted vector is "
            "a file and an environment that give a config. value.detail is the detail of the "
            f"check {CHECK_CONFIG}: the bind and the port.",
            "A refused vector has no config. refusal.check is the check that failed. For the "
            f"check {CHECK_CONFIG}, refusal.variable is the first word of the detail: the "
            "variable that the text names. The text also holds the value, and no vector "
            f"holds the text. The check {CHECK_ENV_FILE} fails for a file that the hook "
            "cannot open.",
            "The hook reads the file as UTF-8 text and splits it with Python str.splitlines. "
            "It removes the whitespace of Python str.strip from the two ends of a line. It "
            "skips an empty line and a line that starts with #. It takes a line with an "
            "equals sign whose name is an ASCII letter or underscore and then ASCII letters, "
            "digits and underscores. The value is the text after the first equals sign.",
            "Each path of params.environ is under a directory that no machine has. The "
            "generator puts a stand-in in the place of the one HTTP call of the hook, so the "
            "hook opens no socket. No vector holds the exit status or another check.",
        ),
        tuple(_env_file_vector(case, scratch / "verify") for case in cases.ENV_FILES),
    )


# --- the check of the service -----------------------------------------------------------

#: The start of the line that the program writes for a config that it refuses.
_REFUSED_START: Final = "noticeboard: "


@dataclass(frozen=True)
class Ran:
    """One run of a program: its exit status and the two texts that it wrote."""

    status: int
    out: str
    err: str


def _run_check(variables: dict[str, str]) -> Ran | Raised:
    """One run of `noticeboard --check` in an environment.

    The entry point sets up the root logger. The generator puts a stand-in
    in the place of that call, so the run leaves the log setup as it is.
    """
    out, err = io.StringIO(), io.StringIO()
    with (
        patch.dict(os.environ, variables, clear=True),
        patch.object(logging, "basicConfig"),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        outcome = attempt(lambda: main_module.main(["--check"]))

    if isinstance(outcome, Raised):
        return outcome

    return Ran(outcome, out.getvalue(), err.getvalue())


def _check_params(case: cases.CheckCase) -> dict[str, object]:
    """The params of the vector: what the generator makes under the directory."""
    if not any(cases.ROOT in value for value in case.variables.values()):
        return {}

    files = {name: _text_or_bytes(raw) for name, raw in case.files.items()}

    return {"params": {"directories": list(case.directories), "files": files}}


def _check_vector(case: cases.CheckCase, scratch: Path) -> Vector:
    given: dict[str, Json] = {"args": dict(case.variables)}
    extra = _check_params(case)
    root = scratch / case.id
    root.mkdir(parents=True)
    for directory in case.directories:
        (root / directory).mkdir(parents=True, exist_ok=True)

    for name, raw in case.files.items():
        _write(root / name, raw)

    here = str(root)
    variables = {name: value.replace(cases.ROOT, here) for name, value in case.variables.items()}
    ran = _run_check(variables)
    if isinstance(ran, Raised):
        return raised(case.id, given, ran.exc, **extra)

    lines: list[Json] = [text.replace(here, cases.ROOT) for text in ran.out.splitlines()]
    if ran.status == main_module.EXIT_OK:
        return accepted(case.id, given, {"exit_status": ran.status, "stdout": lines}, **extra)

    if not ran.err.startswith(_REFUSED_START):
        raise ValueError(f"{case.id}: the program ends with {ran.status} and names no variable")

    refusal: dict[str, Json] = {
        "exit_status": ran.status,
        "stdout": lines,
        "variable": ran.err.removeprefix(_REFUSED_START).split()[0],
    }

    return refused(case.id, given, refusal, **extra)


def _check_surface(scratch: Path) -> Surface:
    return _surface(
        "cli.check",
        "noticeboard.__main__.main, with --check",
        f"{PERIMETER} rule 2",
        (
            "The input is the variables of the process, as args.",
            f"The text {cases.ROOT} in a value stands for a directory that the generator "
            "makes for the vector. A reader puts a directory of its own in that place. "
            "params.directories is each directory that the generator makes under it. "
            "params.files is each file that it makes there, with its bytes: a text stands "
            "for the UTF-8 bytes of the text, and a $base64 marker holds bytes that are not "
            f"UTF-8. Each file has mode {MODE_OWNER:04o}. A vector with no params names no "
            "path under such a directory.",
            f"Each other path is under {cases.NO_ROOT}, a directory that no machine has. "
            "Each vector names the state root, the registry, and the socket or the URL of "
            "attendance: a default path can exist on a machine.",
            "An accepted vector is a config that the program takes. value.exit_status is the "
            "exit status, and value.stdout is each line that the program writes to standard "
            f"output, with {cases.ROOT} in the place of the directory.",
            "A refused vector is a config that the program refuses. refusal.exit_status is "
            "the exit status of the Python program today. refusal.stdout is empty. "
            "refusal.variable is the first word that the program writes to standard error "
            "after its name: the variable that the text names. No vector holds that text.",
            "The program prints no key and no token. It says set for a key, and it says "
            "present or MISSING for a path.",
        ),
        tuple(_check_vector(case, scratch / "check") for case in cases.CHECKS),
    )


# --- the stylesheet ---------------------------------------------------------------------

STYLE_NAME: Final = "noticeboard.css"
STYLE_PATH: Final = f"/static/{STYLE_NAME}"


def _style_vector(app: ASGIApp) -> Vector:
    given: dict[str, Json] = {"args": {"method": "GET", "path": STYLE_PATH}}
    answer = _get(app, Ask(STYLE_PATH.encode("ascii")))
    if isinstance(answer, Raised):
        return raised("stylesheet", given, answer.exc)

    if answer.body != (STATIC / STYLE_NAME).read_bytes():
        raise ValueError("the app answers other bytes than the file of the package")

    (content_type,) = answer.values(b"content-type")
    value: dict[str, Json] = {"status": answer.status, "content_type": content_type}

    return accepted("stylesheet", given, value, output=bytes_input(answer.body))


def _style_surface(scratch: Path) -> Surface:
    return _surface(
        "static.css",
        f"noticeboard.app.build_app, the route GET {STYLE_PATH}",
        f"{PERIMETER} rule 1",
        (
            "The input is one request with no access key: args.method and args.path.",
            "value.status is the HTTP status. value.content_type is the Content-Type header. "
            "output is the bytes of the body: the stylesheet of the package.",
            "The web framework also sends the headers ETag and Last-Modified, which hold "
            "values of the machine. No vector holds them.",
            _NOTE_SERVER,
        ),
        (_style_vector(_app(scratch / "style")),),
    )


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch_name:
        scratch = Path(scratch_name)
        token_file = _token_file(scratch / "reader")

        return (
            _key_surface(),
            _csrf_surface(),
            _keyless_surface(),
            _cookie_surface(scratch),
            _form_surface(),
            _query_surface(scratch),
            _segment_surface(scratch),
            _session_id_surface(),
            _list_surface(token_file),
            _detail_surface(token_file),
            _events_surface(token_file),
            _refusal_surface(token_file),
            _token_surface(scratch),
            _transcript_surface(token_file),
            _audit_surface(scratch),
            _report_surface(scratch),
            _env_file_surface(scratch),
            _check_surface(scratch),
            _style_surface(scratch),
        )
