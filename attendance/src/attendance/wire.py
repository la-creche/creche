"""The channel wire format (contract 03 §2, §4, §5, §8, §13).

Every byte from the playpen is untrusted. The playpen runs inside the
sandbox, and the agent process is untrusted by design (invariant 12), so this
module validates shape and size before any value is used, and refuses rather
than raises.

Framing is LF only. A generic line reader is wrong here: Node's `readline`
also splits on U+2028 and U+2029, which are legal inside a JSON string
(contract 03 §2 rule 4).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .atomic import as_array, as_object
from .errors import TurnReason
from .models import Usage

PROTOCOL_VERSION = "1.0"
PROTOCOL_MAJOR = "1"
HOST_VERSION = "0.1.0"
STOP_GRACE_MS = 5000

MAX_LINE_BYTES = 1_048_576
MAX_EVENT_BYTES = 262_144
MAX_PROMPT_BYTES = 262_144
MAX_PERSONA_BYTES = 16_384
MAX_LOG_BYTES = 4_096

# How many objects and arrays an event may nest, itself included. The host
# puts an event inside a journal line and inside a stream line, and a JSON
# reader stops at a nesting limit of its own. This cap is far under that
# limit, so a line the host wrote is a line every reader can follow.
MAX_EVENT_DEPTH = 64

# Contract 03 §8. The playpen caps an entry's text at 64 KiB and an answer
# at 64 entries. The host re-checks both, in characters for the text, because
# a cap that only one side enforces is not a cap (invariant 12).
MAX_ENTRY_CHARS = 65_536
MAX_ENTRIES_PER_READ = 64

# A reason name from the sandbox is one word on the wire. The cap is here
# because the host stores and logs it and never matches it against a table.
MAX_REASON_BYTES = 64

PING_INTERVAL_S = 30
MISSED_PONGS_ALLOWED = 2
HOST_DEADLINE_S = 90

# Contract 03 §11.4 rule 4. The playpen rewrites `supervisor.lock` every
# `lock_beat_s` (5 by default) with a counter one higher. A counter that has
# not moved for this long means nothing is writing the file, so the host may
# remove it and dial. Four beat windows, the same ratio `host_deadline_s` has
# to the ping interval, so one missed rewrite never unlocks a live playpen.
LOCK_STALE_S = 20.0
LOCK_POLL_S = 1.0
COALESCE_MS = 50
PI_IDLE_TTL_ATTENDED_S = 900
PI_IDLE_TTL_OTHER_S = 0
CHANNEL_IDLE_TTL_ATTENDED_S = 0
CHANNEL_IDLE_TTL_OTHER_S = 120
MAX_RESIDENT_PROCESSES = 12

REFUSAL_BUDGET = 10
REFUSAL_WINDOW_S = 60.0

_LF = "\n"
_LF_BYTE = b"\n"
_CR = "\r"

# The type of a capped event whose own type is absent or cannot be kept.
_UNKNOWN_EVENT_TYPE = "unknown"

# The error handler that encodes one half of a surrogate pair as its three
# bytes. A count of bytes then has a value for each text.
_KEEP_HALF_PAIRS = "surrogatepass"


class HostType(StrEnum):
    """Host to playpen (contract 03 §4)."""

    HELLO = "hello"
    OPEN_SESSION = "open_session"
    START_TURN = "start_turn"
    PROMPT = "prompt"
    STEER = "steer"
    ABORT = "abort"
    STOP_PROCESS = "stop_process"
    GET_ENTRIES = "get_entries"
    PING = "ping"
    SHUTDOWN = "shutdown"


class PlaypenType(StrEnum):
    """Playpen to host (contract 03 §5)."""

    READY = "ready"
    SESSION_OPENED = "session_opened"
    EVENT = "event"
    TURN_SETTLED = "turn_settled"
    TURN_FAILED = "turn_failed"
    PROCESS_EXIT = "process_exit"
    PONG = "pong"
    LOG = "log"
    ENTRIES = "entries"
    FATAL = "fatal"


class FatalReason(StrEnum):
    """Why the playpen cannot serve at all (contract 03 §5.7)."""

    CONTROL_MOUNT_UNWRITABLE = "control_mount_unwritable"
    MOUNT_DIR_UNSET = "mount_dir_unset"
    # §3 version rule 2: neither side may make an unknown field fatal, so a
    # reason a newer image invents still lands as a fault the operator sees.
    UNKNOWN = "unknown"


# Contract 03 §5.6's one reason that is not a failure: the family holds no
# process between turns, so there was nothing to warm (§6 rule 4).
OPEN_REASON_NOT_HELD = "not_held"


class PlaypenReason(StrEnum):
    """Why the playpen failed a turn (contract 03 §5.3)."""

    NO_RESIDENT_PROCESS = "no_resident_process"
    FORK_REFUSED = "fork_refused"
    STALE_CREDENTIALS = "stale_credentials"
    PI_START_FAILED = "pi_start_failed"
    PI_REJECTED_PROMPT = "pi_rejected_prompt"
    PROCESS_DIED = "process_died"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SESSION_BUSY_IN_SANDBOX = "session_busy_in_sandbox"
    LINE_TOO_LARGE = "line_too_large"
    INTERNAL = "internal"


class Refusal(StrEnum):
    """Why a line from the playpen was dropped (contract 03 §13)."""

    TOO_LARGE = "too_large"
    BAD_UTF8 = "bad_utf8"
    NOT_JSON = "not_json"
    NOT_OBJECT = "not_object"
    UNKNOWN_TYPE = "unknown_type"
    MALFORMED = "malformed"
    UNKNOWN_ADDRESS = "unknown_address"
    SEQUENCE_GAP = "sequence_gap"


# Contract 03 §5.3's right column. The host never forwards a playpen reason
# to a door verbatim. Two reasons are retried by the host and never surface.
_HOST_REASON: dict[PlaypenReason, TurnReason | None] = {
    PlaypenReason.NO_RESIDENT_PROCESS: None,
    PlaypenReason.FORK_REFUSED: None,
    PlaypenReason.STALE_CREDENTIALS: TurnReason.INTERNAL,
    PlaypenReason.PI_START_FAILED: TurnReason.SANDBOX_LOST,
    PlaypenReason.PI_REJECTED_PROMPT: TurnReason.MODEL_ERROR,
    PlaypenReason.PROCESS_DIED: TurnReason.SANDBOX_LOST,
    PlaypenReason.DEADLINE_EXCEEDED: TurnReason.TURN_TIMEOUT,
    PlaypenReason.SESSION_BUSY_IN_SANDBOX: TurnReason.INTERNAL,
    PlaypenReason.LINE_TOO_LARGE: TurnReason.PROTOCOL_VIOLATION,
    PlaypenReason.INTERNAL: TurnReason.INTERNAL,
}

RETRIED_REASONS: frozenset[PlaypenReason] = frozenset(
    {PlaypenReason.NO_RESIDENT_PROCESS, PlaypenReason.FORK_REFUSED}
)


def host_reason(reason: PlaypenReason) -> TurnReason | None:
    """Map a playpen reason onto contract 02 §14's turn reasons."""
    return _HOST_REASON[reason]


@dataclass(slots=True)
class Ready:
    """The playpen's first line (contract 03 §3)."""

    protocol: str
    sandbox: str
    playpen: str = ""
    pi: str = ""
    node: str = ""
    max_resident_processes: int = MAX_RESIDENT_PROCESSES
    foreign_pi_processes: int = 0
    caps: list[str] = field(default_factory=list[str])

    @property
    def major(self) -> str:
        head, _, _ = self.protocol.partition(".")
        return head


@dataclass(slots=True)
class EventLine:
    """One wrapped pi event (contract 03 §5.1)."""

    session: str
    turn: str
    turn_seq: int
    event: dict[str, Any]


@dataclass(slots=True)
class SettledLine:
    """pi emitted `agent_settled` (contract 03 §5.2)."""

    session: str
    turn: str
    turn_seq: int
    resident: bool
    usage: Usage
    user_entry_id: str | None = None
    leaf_id: str | None = None
    entry_count: int | None = None
    settled_ms: int = 0


@dataclass(slots=True)
class FailedLine:
    """The turn ended without settling (contract 03 §5.3)."""

    session: str
    turn: str
    turn_seq: int
    reason: PlaypenReason
    message: str = ""


@dataclass(slots=True)
class ProcessExitLine:
    """A pi process ended (contract 03 §5.4)."""

    session: str
    code: int | None
    reason: str
    turn: str | None = None


@dataclass(slots=True)
class PongLine:
    """Answer to `ping` (contract 03 §5.5)."""

    nonce: str


@dataclass(slots=True)
class LogLine:
    """Free text, including a pi process's stderr (contract 03 §5.5)."""

    level: str
    message: str
    session: str | None = None


@dataclass(slots=True)
class OpenedLine:
    """The answer to `open_session` (contract 03 §5.6).

    The host reads it and acts on nothing in it. It is parsed so that ten
    pre-starts cannot spend the refusal budget on an unknown type (§13 rule 2),
    and so an operator learns why a pre-start did not happen.
    """

    session: str
    resident: bool
    reason: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class PiEntry:
    """One pi entry, as contract 03 §5.8 hands it to the host."""

    id: str
    role: str
    text: str


@dataclass(slots=True)
class EntriesLine:
    """The answer to `get_entries` (contract 03 §5.8).

    The only message that carries conversation text back to the host. It
    names no turn, because a terminal's exchanges are not turns.
    """

    request: str
    session: str
    ok: bool
    entries: list[PiEntry] = field(default_factory=list[PiEntry])
    since_matched: bool = False
    truncated: bool = False
    leaf_id: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class FatalLine:
    """The playpen cannot serve and is about to exit (contract 03 §5.7)."""

    reason: FatalReason
    message: str = ""


PlaypenMessage = (
    Ready
    | OpenedLine
    | EventLine
    | SettledLine
    | FailedLine
    | ProcessExitLine
    | PongLine
    | LogLine
    | EntriesLine
    | FatalLine
)


@dataclass(slots=True)
class RawLine:
    """One framed record, or the reason it was refused."""

    text: str | None
    size: int
    refusal: Refusal | None = None


class LineSplitter:
    """Splits an incoming byte stream on LF only (contract 03 §2).

    A record over `MAX_LINE_BYTES` is refused, and the bytes up to the next LF
    are discarded so the channel stays usable. Probe 0a measured that exact
    behaviour: one byte over was refused and the channel kept working (§14, A8).

    The buffer holds the start of one record and never more than the cap.
    No byte of a chunk enters it before the size check, in each state.
    """

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self._max = max_line_bytes
        self._buffer = bytearray()
        self._dropping = False

    def feed(self, chunk: bytes) -> list[RawLine]:
        lines: list[RawLine] = []
        view = memoryview(chunk)
        start = 0

        while True:
            index = chunk.find(_LF_BYTE, start)

            if index < 0:
                break

            tail = view[start:index]
            start = index + 1

            if self._dropping:
                self._dropping = False
                continue

            lines.append(self._record(tail))
            self._buffer.clear()

        # The record that is being dropped has no LF yet. Its bytes go.
        if self._dropping:
            return lines

        rest = view[start:]
        size = len(self._buffer) + len(rest)

        # A partial record already over the cap can be refused now. Holding it
        # would let one bad sender grow this buffer without bound.
        if size > self._max:
            lines.append(RawLine(text=None, size=size, refusal=Refusal.TOO_LARGE))
            self._buffer.clear()
            self._dropping = True
            return lines

        self._buffer.extend(rest)

        return lines

    def pending_bytes(self) -> int:
        return len(self._buffer)

    def _record(self, tail: memoryview) -> RawLine:
        """The record that `tail` completes. The size check comes first."""
        size = len(self._buffer) + len(tail)

        if size > self._max:
            return RawLine(text=None, size=size, refusal=Refusal.TOO_LARGE)

        return _decode_record(bytes(self._buffer) + bytes(tail))


def _decode_record(record: bytes) -> RawLine:
    size = len(record)

    try:
        text = record.decode("utf-8")
    except UnicodeDecodeError:
        return RawLine(text=None, size=size, refusal=Refusal.BAD_UTF8)

    # One optional trailing CR is stripped. Nothing else is (§2 rule 3).
    if text.endswith(_CR):
        text = text[:-1]

    return RawLine(text=text, size=size)


def encode(message: dict[str, Any], max_line_bytes: int = MAX_LINE_BYTES) -> str:
    """Serialize one outbound record.

    Raises `ValueError` for a record that the channel cannot carry.
    `PlaypenLink.send` passes the error on. Each caller of `send` catches
    that type and takes it as a refusal of the one message. The `hello` of
    the handshake holds validated ids and numbers only.

    1. A line over the cap raises `ValueError`.
    2. A text with a lone surrogate has no UTF-8 form. It raises
       `UnicodeEncodeError`, which is a subtype of `ValueError`.
    """
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    size = len(body.encode("utf-8"))

    if size > max_line_bytes:
        raise ValueError(f"outbound line is {size} bytes, over the {max_line_bytes} cap")

    return body + _LF


def parse(text: str) -> PlaypenMessage | Refusal:
    """Validate one inbound record (contract 03 §13 rule 1).

    Returns the typed message, or the reason it was refused. Nothing here
    raises, because a bad line from a sandbox is expected rather than
    exceptional.
    """
    try:
        return _parse_record(text)
    except json.JSONDecodeError:
        return Refusal.NOT_JSON
    except Exception:
        # Every exception, not a list of types. Three lines under the size cap
        # raised three types in three places: RecursionError on deep nesting,
        # a plain ValueError on an integer past the interpreter's digit limit,
        # and UnicodeEncodeError in `cap_event` on a lone surrogate.
        return Refusal.MALFORMED


def _parse_record(text: str) -> PlaypenMessage | Refusal:
    decoded: object = json.loads(text)
    record = as_object(decoded)

    if record is None:
        return Refusal.NOT_OBJECT

    type_text = _text(record.get("type"))

    if type_text is None:
        return Refusal.UNKNOWN_TYPE

    try:
        kind = PlaypenType(type_text)
    except ValueError:
        return Refusal.UNKNOWN_TYPE

    return _parse_typed(kind, record)


def _parse_typed(kind: PlaypenType, record: dict[str, Any]) -> PlaypenMessage | Refusal:
    if kind is PlaypenType.READY:
        return _parse_ready(record)

    if kind is PlaypenType.PONG:
        # CONTRACT-QUESTION: contract 03 §4.6 and §5.5 show a `nonce` that
        # is a text and do not say that a `ping` can have none. The playpen
        # answers a `ping` with no nonce with `nonce: null`. This host sends
        # a nonce in each `ping`, so it refuses a `pong` with no text there:
        # the stricter reading. A host that sends no nonce must read null.
        nonce = _text(record.get("nonce"))
        return PongLine(nonce=nonce) if nonce is not None else Refusal.MALFORMED

    if kind is PlaypenType.LOG:
        return _parse_log(record)

    if kind is PlaypenType.PROCESS_EXIT:
        return _parse_process_exit(record)

    if kind is PlaypenType.SESSION_OPENED:
        return _parse_opened(record)

    if kind is PlaypenType.ENTRIES:
        return _parse_entries(record)

    if kind is PlaypenType.FATAL:
        return _parse_fatal(record)

    return _parse_turn_line(kind, record)


def _parse_opened(record: dict[str, Any]) -> OpenedLine | Refusal:
    session = _text(record.get("session"))

    if session is None:
        return Refusal.MALFORMED

    return OpenedLine(
        session=session,
        resident=record.get("resident") is True,
        reason=_short_text(record.get("reason")),
        message=(_text(record.get("message")) or "")[:MAX_LOG_BYTES],
    )


def _parse_entries(record: dict[str, Any]) -> EntriesLine | Refusal:
    """Contract 03 §5.8, validated as untrusted input (§13, invariant 12)."""
    request = _text(record.get("request"))
    session = _text(record.get("session"))

    if request is None or session is None:
        return Refusal.MALFORMED

    entries = _entry_list(as_array(record.get("entries")) or [])

    return EntriesLine(
        request=request,
        session=session,
        ok=record.get("ok") is True,
        entries=entries,
        since_matched=record.get("since_matched") is True,
        truncated=record.get("truncated") is True,
        leaf_id=_text(record.get("leaf_id")),
        reason=_short_text(record.get("reason")),
    )


def _entry_list(raw: list[object]) -> list[PiEntry]:
    """Entries with a usable id. One without one is no cursor, so it is gone."""
    found: list[PiEntry] = []

    for item in raw[:MAX_ENTRIES_PER_READ]:
        record = as_object(item)

        if record is None:
            continue

        entry_id = _text(record.get("id"))

        if entry_id is None:
            continue

        text = _text(record.get("text")) or ""
        found.append(
            PiEntry(id=entry_id, role=_text(record.get("role")) or "", text=text[:MAX_ENTRY_CHARS])
        )

    return found


def _parse_fatal(record: dict[str, Any]) -> FatalLine:
    """An unknown reason is still a fatal. Only the name is unrecognised."""
    reason_text = _text(record.get("reason"))

    try:
        reason = FatalReason(reason_text) if reason_text is not None else FatalReason.UNKNOWN
    except ValueError:
        reason = FatalReason.UNKNOWN

    return FatalLine(
        reason=reason,
        message=(_text(record.get("message")) or "")[:MAX_LOG_BYTES],
    )


def _parse_ready(record: dict[str, Any]) -> Ready | Refusal:
    protocol = _text(record.get("protocol"))
    sandbox = _text(record.get("sandbox"))

    if protocol is None or sandbox is None:
        return Refusal.MALFORMED

    return Ready(
        protocol=protocol,
        sandbox=sandbox,
        playpen=_text(record.get("supervisor")) or "",
        pi=_text(record.get("pi")) or "",
        node=_text(record.get("node")) or "",
        max_resident_processes=_count(record.get("max_resident_processes"), MAX_RESIDENT_PROCESSES),
        foreign_pi_processes=_count(record.get("foreign_pi_processes"), 0),
        caps=_str_list(record.get("caps")),
    )


def _parse_log(record: dict[str, Any]) -> LogLine:
    message = _text(record.get("message")) or ""

    return LogLine(
        level=_text(record.get("level")) or "info",
        message=message[:MAX_LOG_BYTES],
        session=_text(record.get("session")),
    )


def _parse_process_exit(record: dict[str, Any]) -> ProcessExitLine | Refusal:
    session = _text(record.get("session"))

    if session is None:
        return Refusal.MALFORMED

    code = record.get("code")

    return ProcessExitLine(
        session=session,
        code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        reason=_text(record.get("reason")) or "crashed",
        turn=_text(record.get("turn")),
    )


def _parse_turn_line(
    kind: PlaypenType, record: dict[str, Any]
) -> EventLine | SettledLine | FailedLine | Refusal:
    session = _text(record.get("session"))
    turn = _text(record.get("turn"))
    turn_seq = record.get("turn_seq")

    if session is None or turn is None:
        return Refusal.MALFORMED

    if isinstance(turn_seq, bool) or not isinstance(turn_seq, int) or turn_seq < 1:
        return Refusal.MALFORMED

    if kind is PlaypenType.EVENT:
        event = as_object(record.get("event"))
        if event is None:
            return Refusal.MALFORMED

        return EventLine(session=session, turn=turn, turn_seq=turn_seq, event=cap_event(event))

    if kind is PlaypenType.TURN_SETTLED:
        return SettledLine(
            session=session,
            turn=turn,
            turn_seq=turn_seq,
            resident=record.get("resident") is True,
            usage=read_usage(record.get("usage")),
            user_entry_id=_text(record.get("user_entry_id")),
            leaf_id=_text(record.get("leaf_id")),
            entry_count=_optional_count(record.get("entry_count")),
            settled_ms=_count(record.get("settled_ms"), 0),
        )

    reason_text = _text(record.get("reason"))

    try:
        reason = PlaypenReason(reason_text) if reason_text is not None else None
    except ValueError:
        reason = PlaypenReason.INTERNAL

    if reason is None:
        return Refusal.MALFORMED

    return FailedLine(
        session=session,
        turn=turn,
        turn_seq=turn_seq,
        reason=reason,
        message=(_text(record.get("message")) or "")[:MAX_LOG_BYTES],
    )


def cap_event(event: dict[str, Any]) -> dict[str, Any]:
    """Contract 03 §13 rule 6: an oversized event keeps its type only.

    CONTRACT-QUESTION: §13 rule 6 caps the bytes of an event and names no
    nesting limit. An event nested past `MAX_EVENT_DEPTH` is read as
    oversized here and keeps its type only: the stricter reading. To refuse
    the line would cost the turn, because a refused line leaves a gap in
    `turn_seq` and rule 4 fails a turn on a gap.

    CONTRACT-QUESTION: §13 rule 5 says that the host records an event, and
    no rule names an event that the host cannot record. A text of pi can end
    in one half of a surrogate pair, and such an event has no UTF-8 form. It
    is read as oversized here and keeps its type only, for the reason above.
    `original_bytes` counts one half as three bytes. The other reading puts
    U+FFFD in the place of the half, which changes an event that rule 5
    keeps unchanged.
    """
    text = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    size, whole = _utf8_size(text)

    if whole and size <= MAX_EVENT_BYTES and not _nests_past(event, MAX_EVENT_DEPTH):
        return event

    kind = _text(event.get("type"))

    return {
        "type": kind if kind and _utf8_size(kind)[1] else _UNKNOWN_EVENT_TYPE,
        "truncated": True,
        "original_bytes": size,
    }


def _utf8_size(text: str) -> tuple[int, bool]:
    """The count of UTF-8 bytes of a text, and whether it has a UTF-8 form.

    A text with one half of a surrogate pair has none. Its count takes each
    half as three bytes.
    """
    try:
        return len(text.encode("utf-8")), True
    except UnicodeEncodeError:
        return len(text.encode("utf-8", _KEEP_HALF_PAIRS)), False


def _nests_past(event: dict[str, Any], limit: int) -> bool:
    """True when the event nests more than `limit` objects and arrays.

    One level at a time and never by recursion, so the depth of the input
    cannot exhaust the stack of this function either.
    """
    level: list[object] = [event]

    for _ in range(limit):
        level = [child for holder in level for child in _containers(holder)]

        if not level:
            return False

    return True


def _containers(holder: object) -> list[object]:
    """The objects and arrays directly inside one object or array."""
    record = as_object(holder)
    values: list[Any] = list(record.values()) if record is not None else as_array(holder) or []

    return [value for value in values if isinstance(value, (dict, list))]


def read_usage(value: object) -> Usage:
    """Usage from the playpen is advisory (contract 03 §13 rule 7)."""
    record = as_object(value)

    if record is None:
        return Usage()

    return Usage(
        input=_count(record.get("input"), 0),
        output=_count(record.get("output"), 0),
        cache_read=_count(record.get("cache_read"), 0),
        cache_write=_count(record.get("cache_write"), 0),
        cost_usd=_money(record.get("cost_usd")),
    )


def hello(
    family: str,
    sandbox: str,
    epoch: int,
    pi_idle_ttl_s: int,
    max_resident_processes: int = MAX_RESIDENT_PROCESSES,
    channel_idle_ttl_s: int = CHANNEL_IDLE_TTL_ATTENDED_S,
) -> dict[str, Any]:
    """Accept the channel (contract 03 §3). Carries an epoch, never a value."""
    return {
        "type": HostType.HELLO.value,
        "protocol": PROTOCOL_VERSION,
        "host": f"sessiond/{HOST_VERSION}",
        "family": family,
        "sandbox": sandbox,
        "max_line_bytes": MAX_LINE_BYTES,
        "coalesce_ms": COALESCE_MS,
        "pi_idle_ttl_s": pi_idle_ttl_s,
        "max_resident_processes": max_resident_processes,
        "host_deadline_s": HOST_DEADLINE_S,
        "channel_idle_ttl_s": channel_idle_ttl_s,
        "env_epoch": epoch,
    }


def open_session(
    session: str,
    cwd: str,
    session_dir: str,
    epoch: int,
    config_rev: str,
    model: str | None = None,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a session's pi process before its first prompt (§4.7).

    `start_turn` without the prompt. It is an optimisation, never a
    precondition: §4.7 rule 10 makes every turn correct without it.
    """
    message: dict[str, Any] = {
        "type": HostType.OPEN_SESSION.value,
        "session": session,
        "cwd": cwd,
        "session_dir": session_dir,
        "env_epoch": epoch,
        "config_rev": config_rev,
    }

    if model:
        message["model"] = model

    if workspace is not None:
        message["workspace"] = workspace

    return message


def get_entries(
    request: str,
    session: str,
    cwd: str,
    session_dir: str,
    epoch: int,
    config_rev: str,
    since: str | None = None,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read a session's pi entries back, with their text (§4.8).

    It takes §4.7's binding fields because the process is usually not
    resident: a terminal ran pi itself and left.
    """
    message: dict[str, Any] = {
        "type": HostType.GET_ENTRIES.value,
        "request": request,
        "session": session,
        "cwd": cwd,
        "session_dir": session_dir,
        "env_epoch": epoch,
        "config_rev": config_rev,
    }

    if since:
        message["since"] = since

    if workspace is not None:
        message["workspace"] = workspace

    return message


def start_turn(
    turn: str,
    session: str,
    cwd: str,
    session_dir: str,
    prompt: str,
    deadline_s: int,
    epoch: int,
    config_rev: str,
    persona: str | None = None,
    attachments: list[str] | None = None,
    workspace: dict[str, Any] | None = None,
    branch: dict[str, Any] | None = None,
    delegation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a turn, starting a pi process when none is resident (§4.1)."""
    message: dict[str, Any] = {
        "type": HostType.START_TURN.value,
        "turn": turn,
        "session": session,
        "cwd": cwd,
        "session_dir": session_dir,
        "prompt": prompt,
        "deadline_s": deadline_s,
        "env_epoch": epoch,
        "config_rev": config_rev,
    }

    if persona:
        message["persona"] = persona

    if attachments:
        message["attachments"] = attachments

    if workspace is not None:
        message["workspace"] = workspace

    if branch is not None:
        message["branch"] = branch

    # §7.4 rule 4: `start_turn` carries an optional `delegation`, and the
    # playpen copies its two fields into the turn file. Absent means a
    # turn outside any chain, which is what every non-job turn is.
    if delegation is not None:
        message["delegation"] = delegation

    return message


def steer(session: str, turn: str, message: str) -> dict[str, Any]:
    """Queue a steering message into a running turn (§4.3)."""
    return {
        "type": HostType.STEER.value,
        "session": session,
        "turn": turn,
        "message": message,
    }


def abort(session: str, turn: str) -> dict[str, Any]:
    """Abort the running turn of one session (§4.4)."""
    return {"type": HostType.ABORT.value, "session": session, "turn": turn}


def stop_process(session: str, grace_ms: int = STOP_GRACE_MS) -> dict[str, Any]:
    """End the pi process of one session (§4.5)."""
    return {
        "type": HostType.STOP_PROCESS.value,
        "session": session,
        "grace_ms": grace_ms,
    }


def ping(nonce: str) -> dict[str, Any]:
    """Liveness probe (§4.6). Also the playpen's own deadline signal."""
    return {"type": HostType.PING.value, "nonce": nonce}


def shutdown(grace_ms: int = STOP_GRACE_MS) -> dict[str, Any]:
    """End every process and exit (§4.6)."""
    return {"type": HostType.SHUTDOWN.value, "grace_ms": grace_ms}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _short_text(value: object, cap: int = MAX_REASON_BYTES) -> str | None:
    """A name from the sandbox. Capped, because it is untrusted (§13 rule 1)."""
    return value[:cap] if isinstance(value, str) else None


def _count(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback

    return value


def _optional_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None

    return value


def _money(value: object) -> float:
    """A cost from the sandbox, or 0.0 for a value that is no cost.

    NaN is not below zero, so the range check alone lets it pass. A cost
    that is not finite has no JSON text, and an answer cannot hold it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0.0

    cost = float(value)

    return cost if math.isfinite(cost) else 0.0


def _str_list(value: object) -> list[str]:
    entries = as_array(value)

    if entries is None:
        return []

    return [item for item in entries if isinstance(item, str)]
