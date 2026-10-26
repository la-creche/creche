"""The written inputs of the HTTP surfaces of `attendance` (contract 02).

`session.py` holds the surfaces. This module holds what each one is given:
the request bodies and the error bodies. Every input is text written here.
None names a deployment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from attendance.errors import ErrorCode

from vectors.core import Json, bytes_input, expand, repeat_input

FAMILY: Final = "chat"
SESSION: Final = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
OTHER_SESSION: Final = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"
JOB_SESSION: Final = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
TURN: Final = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
DELEGATION: Final = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
SANDBOX: Final = "chat-s2"
DIGEST: Final = "0123456789abcdef" * 4

#: The cap of `attendance.requests`, as a number a vector can stand beside.
MESSAGE_BYTES: Final = 65_536

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


# --- the times of a field (RFC 3339) -----------------------------------------------

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
    #: the order that `attendance.leases` wrote them. Each other detail has
    #: its keys in the order that `attendance` writes them.
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
