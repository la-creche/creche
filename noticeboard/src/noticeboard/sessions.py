"""`attendance` as the noticeboard reads it: principal `view-ro`.

The noticeboard holds the weakest token in the system. `view-ro` reads
every family and writes nothing, so this module offers two calls and no
third: list the sessions of one family, and read one page of the
transcript of one session. Nothing here can create, steer, stop or delete,
because there is no code for it.

`attendance` serves the two calls as read-only view routes:

    GET /families/{family}/sessions          `session/list` of the facade
    GET /families/{family}/sessions/{id}     `session/load` of the facade

For each call, `attendance` opens one `acp-relay` to the family's facade,
sends `initialize` and one read method, and answers JSON. The facade owns
each conversation.

Two shapes this module defends against.

1. **An `attendance` that does not answer.** A page must still render. Every
   call returns its rows plus a `problem` string, never an exception, and
   the page shows the problem as a report (invariant 19).
2. **An answer that is not what the route says.** The body crosses a
   process boundary, so it is untrusted input: every field is read through
   `jsonfiles`, every list is capped, and an entry of a kind that the
   reader does not know is a problem (invariants 12 and 14).

The token is read from a file, never from the environment and never from
argv, and it appears in one place: an `Authorization` header (invariant
13). It is never logged and never rendered.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from . import jsonfiles
from .jsonfiles import Json, Surface

#: The cap of one answer. A transcript page of `attendance` holds 1 MiB of
#: entries at most, and its JSON can be larger than that.
MAX_ANSWER_BYTES: Final = 4 << 20

#: The most sessions one list answer gives a page.
MAX_SESSIONS: Final = 200

#: The most entries of one transcript page of `attendance`.
MAX_ENTRIES: Final = 500

#: The cap of one text of a transcript: the 64 KiB cap of `attendance`.
#: A character has one byte or more, so no text that `attendance` sends
#: passes it.
MAX_ENTRY_CHARS: Final = 65_536

_OK: Final = 200

#: A session id becomes one segment of a path, so a route checks it.
SESSION_ID_MAX: Final = 128
_SESSION_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def is_session(value: str) -> bool:
    """A session id of contract 02 §2.

    A session id becomes one segment of a path on `attendance`, so a route
    checks it before the reader builds that path.
    """
    return len(value) <= SESSION_ID_MAX and _SESSION_RE.match(value) is not None


@dataclass(frozen=True)
class Reply:
    """One HTTP answer, or the reason there was none."""

    status: int = 0
    body: bytes = b""
    #: A transport failure: no socket, refused connection, timeout. Empty
    #: when the call reached `attendance`, whatever it then answered.
    problem: str = ""


class Transport(Protocol):
    """The one thing this module cannot fake for itself.

    A test supplies a transport that answers from fixtures. Nothing else
    in the noticeboard knows whether `attendance` is on a socket or on TCP.

    `bearer` is its own parameter, never a member of `params`. A token in
    a bag of query parameters is one edit away from a URL (invariant 13).
    """

    def get(self, path: str, params: Mapping[str, str], bearer: str) -> Reply: ...


class EntryKind(enum.StrEnum):
    """The kind of one entry of a transcript page of `attendance`."""

    USER = "user"
    AGENT = "agent"
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"


@dataclass(frozen=True)
class SessionRow:
    """One row of `GET /families/{family}/sessions`."""

    #: The family of the route. A row of the answer names no family.
    family: str
    session: str
    title: str
    created: str
    updated: str
    job_id: str
    delegation: str
    trigger: str

    @property
    def origin(self) -> str:
        """What started the session, as a page shows it. Empty for a
        session that a person started through a door."""
        if self.trigger:
            return f"trigger {self.trigger}"

        if self.delegation:
            return "delegation"

        if self.job_id:
            return "job"

        return ""


@dataclass(frozen=True)
class SessionList:
    rows: tuple[SessionRow, ...] = ()
    next_cursor: str = ""
    problem: str = ""


@dataclass(frozen=True)
class Entry:
    """One entry of a transcript page. A tool call has a title, a status
    and its result as `text`."""

    position: int
    kind: EntryKind
    text: str
    title: str = ""
    status: str = ""


@dataclass(frozen=True)
class TranscriptPage:
    """One page of the replay of one session, oldest entry first."""

    entries: tuple[Entry, ...] = ()
    #: The `start` of the next page, or None when this page is the last.
    next_start: int | None = None
    #: `attendance` stopped the replay at its cap of updates.
    truncated: bool = False
    #: An entry of a kind that this reader does not know, one sentence for
    #: each. The page shows the sentence in the place of the entry.
    unread: tuple[str, ...] = ()
    #: The answer gave no page.
    problem: str = ""


@dataclass
class SessionReader:
    """The two calls `view-ro` may make."""

    transport: Transport
    token_file: Path
    _token: str = field(default="", init=False, repr=False)

    def sessions(self, family: str, cursor: str = "") -> SessionList:
        """One page of the family's sessions, newest first, as the facade
        orders them."""
        params = {"cursor": cursor} if cursor else {}
        body, problem = self._object(f"/families/{family}/sessions", params, family)

        if body is None:
            return SessionList(problem=problem)

        rows = jsonfiles.children(body, "sessions", MAX_SESSIONS)

        return SessionList(
            rows=tuple(_session(family, one) for one in rows),
            next_cursor=jsonfiles.text(body, "next_cursor"),
        )

    def transcript(self, family: str, session: str, start: int = 0) -> TranscriptPage:
        """One page of the transcript from the entry `start`."""
        params = {"start": str(start)} if start > 0 else {}
        body, problem = self._object(f"/families/{family}/sessions/{session}", params, family)

        if body is None:
            return TranscriptPage(problem=problem)

        entries: list[Entry] = []
        unread: list[str] = []

        for one in jsonfiles.children(body, "entries", MAX_ENTRIES):
            entry = _entry(one)

            if isinstance(entry, str):
                unread.append(entry)
                continue

            entries.append(entry)

        return TranscriptPage(
            entries=tuple(entries),
            next_start=_next_start(body),
            truncated=jsonfiles.flag(body, "truncated"),
            unread=tuple(unread),
        )

    def _object(self, path: str, params: Mapping[str, str], family: str) -> tuple[Json | None, str]:
        """One answer of `attendance`. `family` is the family of the path, for
        the notice of an answer that is not strict JSON."""
        reply = self._call(path, params)

        if reply.problem:
            return None, reply.problem

        if reply.status != _OK:
            return None, _refusal(reply, family)

        if len(reply.body) > MAX_ANSWER_BYTES:
            return None, f"attendance answered over {MAX_ANSWER_BYTES} bytes; refusing to parse it"

        body, problem = jsonfiles.parse_object(
            reply.body,
            "attendance's answer",
            MAX_ANSWER_BYTES,
            surface=Surface.SESSION_ANSWER,
            family=family,
        )

        return body, problem or ""

    def _call(self, path: str, params: Mapping[str, str]) -> Reply:
        token, problem = self._bearer()

        if problem:
            return Reply(problem=problem)

        # The token travels beside the query, never inside it. The
        # transport turns it into one `Authorization` header.
        return self.transport.get(path, params, token)

    def _bearer(self) -> tuple[str, str]:
        """The `view-ro` token, read once (contract 02 §3 rules 4 and 5)."""
        if self._token:
            return self._token, ""

        try:
            found = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            # The file is mode 0600 and owned by attendance's user, so "not
            # readable" is the ordinary first-run failure here.
            return "", f"cannot read the noticeboard-ro token: {error.strerror or error}"
        except UnicodeDecodeError:
            # Not OSError. The text of this error quotes a byte of the file,
            # and no byte of a token file belongs on a page.
            return "", "the noticeboard-ro token file is not UTF-8 text"

        if not found:
            return "", "the noticeboard-ro token file is empty"

        self._token = found

        return found, ""


def _refusal(reply: Reply, family: str) -> str:
    """Contract 02 §14's error body, read as a sentence for a page.

    A refusal has the cap of an answer.
    """
    body, _ = jsonfiles.parse_object(
        reply.body,
        "attendance's refusal",
        MAX_ANSWER_BYTES,
        surface=Surface.SESSION_ANSWER,
        family=family,
    )
    error = jsonfiles.child(body or {}, "error")
    code = jsonfiles.whole(error, "code")
    message = jsonfiles.text(error, "message")

    if not code:
        return f"attendance answered {reply.status} with no error code"

    return f"attendance refused: {code} ({reply.status}) {message}".rstrip()


def _session(family: str, body: Json) -> SessionRow:
    return SessionRow(
        family=family,
        session=jsonfiles.whole(body, "id"),
        title=jsonfiles.text(body, "title"),
        created=jsonfiles.whole(body, "created"),
        updated=jsonfiles.whole(body, "updated"),
        job_id=jsonfiles.whole(body, "job_id"),
        delegation=jsonfiles.whole(body, "delegation"),
        trigger=_trigger(body),
    )


def _trigger(body: Json) -> str:
    """The kind and the name of the trigger of a session, or empty."""
    found = jsonfiles.block(body, "trigger")

    if found is None:
        return ""

    parts = (jsonfiles.whole(found, "kind"), jsonfiles.whole(found, "name"))

    return " ".join(one for one in parts if one)


def _entry(body: Json) -> Entry | str:
    """One entry, or the problem of an entry of a kind this reader does
    not know."""
    kind = jsonfiles.whole(body, "kind")

    if kind not in set(EntryKind):
        return f"an entry of the transcript has the kind {kind[: jsonfiles.MAX_TEXT_CHARS]!r}"

    if kind == EntryKind.TOOL_CALL:
        return Entry(
            position=jsonfiles.integer(body, "position"),
            kind=EntryKind.TOOL_CALL,
            text=jsonfiles.text(body, "result", MAX_ENTRY_CHARS),
            title=jsonfiles.text(body, "title"),
            status=jsonfiles.whole(body, "status"),
        )

    return Entry(
        position=jsonfiles.integer(body, "position"),
        kind=EntryKind(kind),
        text=jsonfiles.text(body, "text", MAX_ENTRY_CHARS),
    )


def _next_start(body: Json) -> int | None:
    """The `next` of a page: an integer, or null for the last page."""
    value = body.get("next")

    if type(value) is int and value >= 0:
        return value

    return None
