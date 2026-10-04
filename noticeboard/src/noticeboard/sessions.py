"""`attendance` as the noticeboard reads it: principal `view-ro` (contract 02).

The noticeboard holds the weakest token in the system. Contract 02 §3.1 gives
`view-ro` every family, no session id of its own, and no write at all, so
this module offers three calls and no fourth: list sessions, read one
session with its turns, and replay a session's event stream. Nothing here
can create, steer, stop or delete, because there is no code for it.

Two shapes this module defends against.

1. **An `attendance` that does not answer.** A page must still render. Every
   call returns its rows plus a `problem` string, never an exception, and
   the page shows the problem as a report (invariant 19).
2. **An answer that is not what the contract says.** The body crosses a
   process boundary, so it is untrusted input: every field is read through
   `jsonfiles`, every list is capped, and a row that will not parse becomes
   a row that says so (invariants 12 and 14).

The token is read from a file, never from the environment and never from
argv, and it appears in one place: an `Authorization` header (invariant
13). It is never logged and never rendered.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from . import jsonfiles
from .jsonfiles import Json

#: Contract 02 §5.2: 1 to 200, default 50.
MAX_SESSION_LIMIT: Final = 200

#: Contract 02 §5.3: the newest N turns, at most 100.
MAX_TURNS: Final = 100
DEFAULT_TURNS: Final = 20

#: A list body is small, but it crosses a process boundary all the same.
MAX_LIST_BYTES: Final = 4 << 20

#: One replayed stream. A session's whole journal can be large, so the
#: reader asks for a window (see `from_seq` in `events`) and still caps
#: what it will hold.
MAX_STREAM_BYTES: Final = 8 << 20
MAX_STREAM_LINES: Final = 5_000

_OK: Final = 200

#: Contract 02 §2: a session id is 1 to 128 characters.
SESSION_ID_MAX: Final = 128
_SESSION_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")

#: Contract 02 §2's session id prefixes, mapped to the door that owns
#: them. §3.1 names the matching tokens: `door-owui`, `door-tui`,
#: `door-delegate`, `door-trigger`.
#:
#: CONTRACT-QUESTION: contract 02 §4.2 has no `door` field, and the session
#: list shows one. The prefix is the only
#: statement of origin that survives after a writer lease expires, so it
#: is read first and `writer.holder` second.
DOOR_BY_PREFIX: Final = (
    ("owui-", "owui"),
    ("tui-", "tui"),
    ("job-", "delegate"),
    ("auto-", "trigger"),
)


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


@dataclass(frozen=True)
class WriterRow:
    """Contract 02 §7.1. Null when nobody holds the lease."""

    holder: str
    door_instance: str
    since: str
    expires_at: str
    turn: str


@dataclass(frozen=True)
class UsageRow:
    """Contract 02 §4.4's five members."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float | None = None

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


@dataclass(frozen=True)
class TurnRow:
    """Contract 02 §4.4."""

    turn: str
    state: str
    reason: str
    started_at: str
    ended_at: str
    deadline_s: int
    sandbox: str
    approvals: int
    usage: UsageRow
    #: §11 rule 6 sets this when the persona was cut at 16 KiB. §4.4 does
    #: not list it, so absent reads false.
    persona_truncated: bool

    @property
    def running(self) -> bool:
        """Contract 02 §4.3: three states are not terminal."""
        return self.state in ("queued", "running", "waiting-approval")


@dataclass(frozen=True)
class SessionRow:
    """Contract 02 §4.2, plus the door this noticeboard derives."""

    family: str
    session: str
    kind: str
    title: str
    state: str
    created_at: str
    updated_at: str
    journal_seq: int
    turns_total: int
    turns_running: int
    sandbox: str
    persona_hash: str
    writer: WriterRow | None
    labels: Mapping[str, str]
    problem: str = ""

    @property
    def door(self) -> str:
        """Which door made this session."""
        for prefix, name in DOOR_BY_PREFIX:
            if self.session.startswith(prefix):
                return name

        return self.writer.holder if self.writer is not None else ""

    @property
    def last_activity(self) -> str:
        """§4.2: `updated_at` is the last journal append."""
        return self.updated_at


@dataclass(frozen=True)
class SessionList:
    rows: tuple[SessionRow, ...] = ()
    next_cursor: str = ""
    problem: str = ""


@dataclass(frozen=True)
class SessionDetail:
    session: SessionRow | None = None
    turns: tuple[TurnRow, ...] = ()
    problem: str = ""


@dataclass(frozen=True)
class Stream:
    """A replayed journal, as lines. `transcript.py` gives them meaning."""

    lines: tuple[Json, ...] = ()
    #: Every bound that bit, and every line that would not parse.
    problems: tuple[str, ...] = ()
    truncated: bool = False


@dataclass
class SessionReader:
    """The three calls `view-ro` may make."""

    transport: Transport
    token_file: Path
    _token: str = field(default="", init=False, repr=False)

    def sessions(self, family: str = "", limit: int = 50, cursor: str = "") -> SessionList:
        """Contract 02 §5.2. Newest activity first, as `attendance` orders it."""
        params: dict[str, str] = {"limit": str(min(limit, MAX_SESSION_LIMIT))}

        if family:
            params["family"] = family

        if cursor:
            params["cursor"] = cursor

        body, problem = self._object("/v1/sessions", params, MAX_LIST_BYTES)

        if body is None:
            return SessionList(problem=problem)

        rows = jsonfiles.children(body, "sessions", MAX_SESSION_LIMIT)

        return SessionList(
            rows=tuple(_session(one) for one in rows),
            next_cursor=jsonfiles.whole(body, "next_cursor"),
        )

    def detail(self, family: str, session: str, turns: int = DEFAULT_TURNS) -> SessionDetail:
        """Contract 02 §5.3: the session object plus its newest turns."""
        path = f"/v1/sessions/{family}/{session}"
        params = {"turns": str(min(turns, MAX_TURNS))}
        body, problem = self._object(path, params, MAX_LIST_BYTES)

        if body is None:
            return SessionDetail(problem=problem)

        return SessionDetail(
            session=_session(body),
            turns=tuple(_turn(one) for one in jsonfiles.children(body, "turns", MAX_TURNS)),
        )

    def events(self, family: str, session: str, from_seq: int = 0) -> Stream:
        """Contract 02 §5.5, with `follow=false`.

        A page is a snapshot, so the reader takes the replay and closes.
        Following would hold a connection open for the life of a request
        and give the reader nothing a refresh does not.
        """
        path = f"/v1/sessions/{family}/{session}/events"
        params = {"from_seq": str(max(0, from_seq)), "follow": "false"}
        reply = self._call(path, params)

        if reply.problem:
            return Stream(problems=(reply.problem,))

        if reply.status != _OK:
            return Stream(problems=(_refusal(reply),))

        return _stream(reply.body)

    def _object(self, path: str, params: Mapping[str, str], limit: int) -> tuple[Json | None, str]:
        reply = self._call(path, params)

        if reply.problem:
            return None, reply.problem

        if reply.status != _OK:
            return None, _refusal(reply)

        if len(reply.body) > limit:
            return None, f"attendance answered over {limit} bytes; refusing to parse it"

        body, problem = jsonfiles.parse_object(reply.body, "attendance's answer")

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


def _refusal(reply: Reply) -> str:
    """Contract 02 §14's error body, read as a sentence for a page."""
    body, _ = jsonfiles.parse_object(reply.body, "attendance's refusal")
    error = jsonfiles.child(body or {}, "error")
    code = jsonfiles.whole(error, "code")
    message = jsonfiles.text(error, "message")

    if not code:
        return f"attendance answered {reply.status} with no error code"

    return f"attendance refused: {code} ({reply.status}) {message}".rstrip()


def _stream(raw: bytes) -> Stream:
    """NDJSON to objects. LF is the only delimiter (contract 02 §8).

    U+2028 and U+2029 are legal inside a JSON string, so a generic line
    reader would cut one journal line into two.
    """
    problems: list[str] = []
    truncated = False

    if len(raw) > MAX_STREAM_BYTES:
        raw = raw[:MAX_STREAM_BYTES]
        problems.append(f"the stream passed {MAX_STREAM_BYTES} bytes and was cut")
        truncated = True

    lines: list[Json] = []

    for one in raw.split(b"\n"):
        if not one.strip():
            continue

        if len(lines) >= MAX_STREAM_LINES:
            problems.append(f"stopped after {MAX_STREAM_LINES} journal lines")
            truncated = True
            break

        body, problem = jsonfiles.parse_object(one, "a journal line")

        if body is None:
            problems.append(problem or "a journal line would not parse")
            continue

        lines.append(body)

    return Stream(lines=tuple(lines), problems=tuple(problems), truncated=truncated)


def _session(body: Json) -> SessionRow:
    return SessionRow(
        family=jsonfiles.whole(body, "family"),
        session=jsonfiles.whole(body, "session"),
        kind=jsonfiles.whole(body, "kind"),
        title=jsonfiles.text(body, "title"),
        state=jsonfiles.whole(body, "state"),
        created_at=jsonfiles.whole(body, "created_at"),
        updated_at=jsonfiles.whole(body, "updated_at"),
        journal_seq=jsonfiles.integer(body, "journal_seq"),
        turns_total=jsonfiles.integer(body, "turns_total"),
        turns_running=jsonfiles.integer(body, "turns_running"),
        sandbox=jsonfiles.whole(body, "sandbox"),
        persona_hash=jsonfiles.whole(body, "persona_hash"),
        writer=_writer(body),
        labels=_labels(body),
    )


def _writer(body: Json) -> WriterRow | None:
    found = jsonfiles.block(body, "writer")

    if found is None:
        return None

    return WriterRow(
        holder=jsonfiles.whole(found, "holder"),
        door_instance=jsonfiles.whole(found, "door_instance"),
        since=jsonfiles.whole(found, "since"),
        expires_at=jsonfiles.whole(found, "expires_at"),
        turn=jsonfiles.whole(found, "turn"),
    )


#: §4.2 calls `labels` "small free strings for the noticeboard". Free means a
#: family can write anything into them, so the count and each value are
#: capped before a page renders them.
MAX_LABELS: Final = 20


def _labels(body: Json) -> Mapping[str, str]:
    found = jsonfiles.child(body, "labels")
    kept: dict[str, str] = {}

    for key in sorted(found)[:MAX_LABELS]:
        value = found[key]

        if isinstance(value, str):
            kept[key[: jsonfiles.MAX_TEXT_CHARS]] = value[: jsonfiles.MAX_TEXT_CHARS]

    return kept


def _turn(body: Json) -> TurnRow:
    return TurnRow(
        turn=jsonfiles.whole(body, "turn"),
        state=jsonfiles.whole(body, "state"),
        reason=jsonfiles.whole(body, "reason"),
        started_at=jsonfiles.whole(body, "started_at"),
        ended_at=jsonfiles.whole(body, "ended_at"),
        deadline_s=jsonfiles.integer(body, "deadline_s"),
        sandbox=jsonfiles.whole(body, "sandbox"),
        approvals=jsonfiles.integer(body, "approvals"),
        usage=_usage(body),
        persona_truncated=jsonfiles.flag(body, "persona_truncated"),
    )


def _usage(body: Json) -> UsageRow:
    found = jsonfiles.child(body, "usage")

    return UsageRow(
        input=jsonfiles.integer(found, "input"),
        output=jsonfiles.integer(found, "output"),
        cache_read=jsonfiles.integer(found, "cache_read"),
        cache_write=jsonfiles.integer(found, "cache_write"),
        cost_usd=jsonfiles.number(found, "cost_usd"),
    )
