"""The channel protocol (contract 03): what `attendance.wire` reads and writes.

Three surfaces:

- `channel.parse`: one line of text to `wire.parse`, the typed message or
  the refusal. One line per message type, per field and per JSON trap.
- `channel.frame`: chunks of bytes to `wire.LineSplitter`, the records it
  frames and the ones it refuses.
- `channel.build`: the arguments of one host-side builder to the exact
  bytes `wire.encode` writes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from attendance.branching import Branch, Decision
from attendance.jobs import Delegation
from attendance.workspace import CODE_SANDBOX_FAMILY, workspace_of

from attendance import wire
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
    raised,
    refused,
    repeat_input,
    text_input,
)

CONTRACT: Final = "contract 03"

SESSION: Final = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"
TURN: Final = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
SANDBOX: Final = "chat-s1"

#: Deeper than any supported interpreter parses before it stops: a JSON
#: reader here gives up near 10,000 levels on Python 3.12 and 3.13 and near
#: 120,000 on 3.14. The line still fits under `MAX_LINE_BYTES`.
VERY_DEEP: Final = 400_000

#: Under the nesting limit of each supported interpreter, with a margin for
#: the depth of the caller: 9,997 levels on Python 3.12, 9,998 on 3.13 and
#: about 116,000 on 3.14. The Rust reader stops at 9,000 levels, so it
#: refuses a line of this depth. The line object is one of the levels.
DEEP_AND_READ: Final = 9_100

#: Past the interpreter's limit of 4,300 digits for one integer.
HUGE_DIGITS: Final = 5_000


def _line(**fields: object) -> str:
    """One compact JSON object, the way the playpen writes a line."""
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False)


def _turn(kind: str, **fields: object) -> str:
    """A line of a turn: `session`, `turn` and `turn_seq` are in place."""
    return _line(type=kind, session=SESSION, turn=TURN, turn_seq=1, **fields)


def _nested(levels: int, inner: str = "1") -> str:
    """`inner` inside `levels` arrays."""
    return "[" * levels + inner + "]" * levels


@dataclass(frozen=True)
class Line:
    """One input line: plain text, or a long text written as parts."""

    id: str
    text: str = ""
    parts: tuple[tuple[str, int], ...] = ()

    def value(self) -> str:
        return expand(self.parts) if self.parts else self.text

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else text_input(self.text)


def _l(line_id: str, text: str) -> Line:
    return Line(line_id, text)


_USAGE: Final = {"input": 4120, "output": 12, "cache_read": 7, "cache_write": 3, "cost_usd": 0.002}
_EVENT: Final = {"type": "message_update", "delta": "Sensor "}
_EVENT_HEAD: Final = f'{{"type":"event","session":"{SESSION}","turn":"{TURN}","turn_seq":1,"event":'

#: The longest event `cap_event` keeps, in bytes of compact JSON.
_EVENT_FILL_HEAD: Final = '{"type":"message_update","text":"'
_EVENT_FILL_TAIL: Final = '"}'
_FILL_OVER: Final = wire.MAX_EVENT_BYTES - len(_EVENT_FILL_HEAD) - len(_EVENT_FILL_TAIL) + 1

LINES: Final[tuple[Line, ...]] = (
    # --- ready (§3) ---------------------------------------------------------
    _l(
        "ready-full",
        _line(
            type="ready",
            protocol="1.0",
            sandbox=SANDBOX,
            supervisor="0.3.1",
            pi="0.5.0",
            node="v24.1.0",
            max_resident_processes=8,
            foreign_pi_processes=2,
            caps=["get_entries", "open_session"],
        ),
    ),
    _l("ready-minimal", _line(type="ready", protocol="1.0", sandbox=SANDBOX)),
    _l("ready-no-protocol", _line(type="ready", sandbox=SANDBOX)),
    _l("ready-no-sandbox", _line(type="ready", protocol="1.0")),
    _l("ready-protocol-number", _line(type="ready", protocol=1.0, sandbox=SANDBOX)),
    _l("ready-sandbox-null", _line(type="ready", protocol="1.0", sandbox=None)),
    _l("ready-other-major", _line(type="ready", protocol="2.7", sandbox=SANDBOX)),
    _l("ready-empty-strings", _line(type="ready", protocol="", sandbox="")),
    _l(
        "ready-playpen-key",
        _line(type="ready", protocol="1.0", sandbox=SANDBOX, playpen="0.3.1"),
    ),
    _l(
        "ready-wrong-types",
        _line(
            type="ready",
            protocol="1.0",
            sandbox=SANDBOX,
            supervisor=3,
            pi=None,
            node=["v24"],
            max_resident_processes="8",
            foreign_pi_processes=-1,
            caps="get_entries",
        ),
    ),
    _l(
        "ready-counts-bool-float",
        _line(
            type="ready",
            protocol="1.0",
            sandbox=SANDBOX,
            max_resident_processes=True,
            foreign_pi_processes=2.0,
        ),
    ),
    _l(
        "ready-count-zero",
        _line(type="ready", protocol="1.0", sandbox=SANDBOX, max_resident_processes=0),
    ),
    _l(
        "ready-caps-mixed",
        _line(type="ready", protocol="1.0", sandbox=SANDBOX, caps=["a", 1, None, "b", ["c"], ""]),
    ),
    _l(
        "ready-unknown-field",
        _line(type="ready", protocol="1.0", sandbox=SANDBOX, future={"nested": [1, 2]}),
    ),
    _l("ready-lone-surrogate", '{"type":"ready","protocol":"1.\\ud800","sandbox":"\\udc00"}'),
    # --- session_opened (§5.6) ----------------------------------------------
    _l(
        "opened-full",
        _line(type="session_opened", session=SESSION, resident=True, reason=None, message=""),
    ),
    _l("opened-minimal", _line(type="session_opened", session=SESSION)),
    _l(
        "opened-not-held",
        _line(type="session_opened", session=SESSION, resident=False, reason="not_held"),
    ),
    _l("opened-no-session", _line(type="session_opened", resident=True)),
    _l("opened-resident-one", _line(type="session_opened", session=SESSION, resident=1)),
    _l("opened-resident-text", _line(type="session_opened", session=SESSION, resident="true")),
    _l(
        "opened-reason-long",
        _line(type="session_opened", session=SESSION, reason="r" * 65, message="m" * 4097),
    ),
    _l("opened-reason-number", _line(type="session_opened", session=SESSION, reason=5, message=5)),
    # --- event (§5.1) --------------------------------------------------------
    _l("event-full", _turn("event", event=_EVENT)),
    _l("event-empty-object", _turn("event", event={})),
    _l("event-no-event", _turn("event")),
    _l("event-event-array", _turn("event", event=[_EVENT])),
    _l("event-event-null", _turn("event", event=None)),
    _l("event-event-text", _turn("event", event="message_update")),
    _l("event-no-session", _line(type="event", turn=TURN, turn_seq=1, event=_EVENT)),
    _l("event-no-turn", _line(type="event", session=SESSION, turn_seq=1, event=_EVENT)),
    _l("event-no-seq", _line(type="event", session=SESSION, turn=TURN, event=_EVENT)),
    _l("event-seq-zero", _line(type="event", session=SESSION, turn=TURN, turn_seq=0, event=_EVENT)),
    _l(
        "event-seq-negative",
        _line(type="event", session=SESSION, turn=TURN, turn_seq=-1, event=_EVENT),
    ),
    _l(
        "event-seq-true",
        _line(type="event", session=SESSION, turn=TURN, turn_seq=True, event=_EVENT),
    ),
    _l(
        "event-seq-float",
        _line(type="event", session=SESSION, turn=TURN, turn_seq=1.0, event=_EVENT),
    ),
    _l(
        "event-seq-text",
        _line(type="event", session=SESSION, turn=TURN, turn_seq="1", event=_EVENT),
    ),
    _l("event-seq-exponent", _EVENT_HEAD.replace('"turn_seq":1', '"turn_seq":1e2') + "{}}"),
    _l(
        "event-seq-past-64-bits",
        _EVENT_HEAD.replace('"turn_seq":1', '"turn_seq":36893488147419103232') + "{}}",
    ),
    _l("event-seq-minus-zero", _EVENT_HEAD.replace('"turn_seq":1', '"turn_seq":-0') + "{}}"),
    _l(
        "event-session-not-an-id",
        _line(type="event", session="../x y", turn="not a ulid", turn_seq=1, event=_EVENT),
    ),
    _l("event-session-empty", _line(type="event", session="", turn="", turn_seq=1, event=_EVENT)),
    _l(
        "event-pi-names",
        _turn(
            "event",
            event={
                "type": "message_update",
                "usage": {"input": 4120, "cacheRead": 0, "cost": {"total": 0.002}},
                "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "S"},
            },
        ),
    ),
    _l("event-depth-64", _EVENT_HEAD + '{"type":"deep","a":' + _nested(63) + "}}"),
    _l("event-depth-65", _EVENT_HEAD + '{"type":"deep","a":' + _nested(64) + "}}"),
    _l("event-depth-65-objects", _EVENT_HEAD + '{"a":' * 65 + "1" + "}" * 65 + "}"),
    _l("event-depth-200", _EVENT_HEAD + '{"type":"deep","a":' + _nested(200) + "}}"),
    _l(
        "event-depth-65-no-type",
        _EVENT_HEAD + '{"a":' + _nested(64) + "}}",
    ),
    _l(
        "event-depth-65-type-number",
        _EVENT_HEAD + '{"type":7,"a":' + _nested(64) + "}}",
    ),
    _l(
        "event-depth-65-bytes",
        _EVENT_HEAD
        + '{"type":"deep","text":"caf\u00e9 \u2028 \U0001f600","f":[1.0,1e100,0.1,-0.0,1E2],"a":'
        + _nested(64)
        + "}}",
    ),
    _l(
        "event-depth-65-spaces",
        _EVENT_HEAD + '{ "type" : "deep" , "a" : ' + _nested(64, " 1 ") + " } }",
    ),
    _l(
        "event-depth-65-escapes",
        _EVENT_HEAD
        + '{"type":"deep","text":"\\u00e9\\/\\u0041\\ud83d\\ude00","a":'
        + _nested(64)
        + "}}",
    ),
    Line(
        "event-over-size",
        parts=(
            (_EVENT_HEAD + _EVENT_FILL_HEAD, 1),
            ("a", _FILL_OVER),
            (_EVENT_FILL_TAIL + "}", 1),
        ),
    ),
    _l("event-nan-inside", _EVENT_HEAD + '{"type":"x","v":[NaN,Infinity,-Infinity]}}'),
    _l("event-duplicate-key", _EVENT_HEAD + '{"type":"first","type":"second"}}'),
    _l("event-lone-surrogate", _EVENT_HEAD + '{"type":"x","text":"\\ud800"}}'),
    _l("event-surrogate-pair", _EVENT_HEAD + '{"type":"x","text":"\\ud83d\\ude00"}}'),
    _l("event-nul-escape", _EVENT_HEAD + '{"type":"x","text":"a\\u0000b"}}'),
    _l("event-huge-integer", _EVENT_HEAD + '{"type":"x","n":' + "9" * HUGE_DIGITS + "}}"),
    _l("event-4300-digits", _EVENT_HEAD + '{"type":"x","a":' + _nested(64, "9" * 4300) + "}}"),
    _l(
        "event-seq-4300-digits",
        _EVENT_HEAD.replace('"turn_seq":1', '"turn_seq":' + "9" * 4300) + "{}}",
    ),
    _l("event-key-lone-surrogate", _EVENT_HEAD + '{"type":"x","\\udc00":1}}'),
    _l("event-type-lone-surrogate", _EVENT_HEAD + '{"type":"x\\ud800","text":"a"}}'),
    _l("event-lone-surrogate-nested", _EVENT_HEAD + '{"type":"x","a":[{"b":["\\udc00"]}]}}'),
    _l("event-lone-surrogates-reversed", _EVENT_HEAD + '{"type":"x","text":"\\ude00\\ud83d"}}'),
    _l(
        "event-lone-surrogate-and-text-forms",
        _EVENT_HEAD + '{"type":"x","text":"caf\u00e9 \\n \\u0001 \U0001f600 \\ud83d"}}',
    ),
    _l("event-lone-surrogate-no-type", _EVENT_HEAD + '{"text":"\\ud800"}}'),
    _l("event-type-empty-depth-65", _EVENT_HEAD + '{"type":"","a":' + _nested(64) + "}}"),
    _l(
        "event-duplicate-key-last-shallow",
        _EVENT_HEAD + '{"type":"x","a":' + _nested(64) + ',"a":1}}',
    ),
    _l(
        "event-duplicate-key-last-deep",
        _EVENT_HEAD + '{"type":"x","a":1,"b":"bb","a":' + _nested(64) + "}}",
    ),
    _l(
        "event-float-forms", _EVENT_HEAD + '{"f":[1e16,1e15,1e-5,0.0001,1.5E+300,5e-324,2.5,-0.0]}}'
    ),
    _l(
        "event-float-ties-depth-65",
        _EVENT_HEAD
        + '{"type":"f","f":[669758432410385.25,20278963822605.3125,669758432410385.75],"a":'
        + _nested(64)
        + "}}",
    ),
    Line(
        "event-very-deep",
        parts=(
            (_EVENT_HEAD + '{"type":"x","a":', 1),
            ("[", VERY_DEEP),
            ("]", VERY_DEEP),
            ("}}", 1),
        ),
    ),
    # --- turn_settled (§5.2) ---
    _l(
        "settled-full",
        _turn(
            "turn_settled",
            resident=True,
            usage=_USAGE,
            user_entry_id="e-1",
            leaf_id="e-9",
            entry_count=9,
            settled_ms=1234,
        ),
    ),
    _l("settled-minimal", _turn("turn_settled")),
    _l("settled-no-session", _line(type="turn_settled", turn=TURN, turn_seq=1)),
    _l("settled-seq-zero", _line(type="turn_settled", session=SESSION, turn=TURN, turn_seq=0)),
    _l("settled-usage-array", _turn("turn_settled", usage=[1, 2])),
    _l(
        "settled-usage-wrong-types",
        _turn(
            "turn_settled",
            usage={
                "input": "4120",
                "output": -1,
                "cache_read": True,
                "cache_write": 1.5,
                "cost_usd": "0.002",
            },
        ),
    ),
    _l("settled-cost-int", _turn("turn_settled", usage={"cost_usd": 2})),
    _l("settled-cost-negative", _turn("turn_settled", usage={"cost_usd": -0.5})),
    _l("settled-cost-true", _turn("turn_settled", usage={"cost_usd": True})),
    _l("settled-cost-negative-int", _turn("turn_settled", usage={"cost_usd": -3})),
    _l("settled-cost-minus-zero", _turn("turn_settled", usage={"cost_usd": -0.0})),
    _l("settled-cost-int-rounds", _turn("turn_settled", usage={"cost_usd": 2**53 + 1})),
    _l("settled-cost-int-largest", _turn("turn_settled", usage={"cost_usd": 10**308})),
    _l("settled-cost-int-past-float", _turn("turn_settled", usage={"cost_usd": 10**309})),
    _l(
        "settled-cost-negative-int-past-float",
        _turn("turn_settled", usage={"cost_usd": -(10**309)}),
    ),
    _l(
        "settled-cost-negative-infinity",
        '{"type":"turn_settled","session":"s","turn":"t","turn_seq":1,'
        '"usage":{"cost_usd":-Infinity}}',
    ),
    _l(
        "settled-cost-nan",
        '{"type":"turn_settled","session":"s","turn":"t","turn_seq":1,"usage":{"cost_usd":NaN}}',
    ),
    _l(
        "settled-cost-infinity",
        '{"type":"turn_settled","session":"s","turn":"t","turn_seq":1,'
        '"usage":{"cost_usd":Infinity}}',
    ),
    _l(
        "settled-cost-overflow",
        '{"type":"turn_settled","session":"s","turn":"t","turn_seq":1,"usage":{"cost_usd":1e999}}',
    ),
    _l(
        "settled-count-past-64-bits",
        '{"type":"turn_settled","session":"s","turn":"t","turn_seq":1,'
        '"usage":{"input":18446744073709551616},"settled_ms":36893488147419103232}',
    ),
    _l(
        "settled-pi-usage-names",
        _turn("turn_settled", usage={"cacheRead": 5, "cacheWrite": 6, "cost": {"total": 1}}),
    ),
    _l(
        "settled-optional-wrong-types",
        _turn(
            "turn_settled",
            resident="yes",
            user_entry_id=5,
            leaf_id=None,
            entry_count=-1,
            settled_ms=12.5,
        ),
    ),
    _l("settled-entry-count-true", _turn("turn_settled", entry_count=True, settled_ms=False)),
    _l("settled-entry-count-zero", _turn("turn_settled", entry_count=0)),
    # --- turn_failed (§5.3) ---
    *(
        _l(f"failed-{reason.value.replace('_', '-')}", _turn("turn_failed", reason=reason.value))
        for reason in wire.PlaypenReason
    ),
    _l("failed-with-message", _turn("turn_failed", reason="process_died", message="exit 137")),
    _l("failed-unknown-reason", _turn("turn_failed", reason="out_of_cheese")),
    _l("failed-reason-upper", _turn("turn_failed", reason="INTERNAL")),
    _l("failed-reason-empty", _turn("turn_failed", reason="")),
    _l("failed-no-reason", _turn("turn_failed", message="no reason given")),
    _l("failed-reason-null", _turn("turn_failed", reason=None)),
    _l("failed-reason-number", _turn("turn_failed", reason=5)),
    _l("failed-message-number", _turn("turn_failed", reason="internal", message=5)),
    _l("failed-message-4097", _turn("turn_failed", reason="internal", message="m" * 4097)),
    _l(
        "failed-message-lone-surrogate",
        _EVENT_HEAD.replace('"type":"event"', '"type":"turn_failed"').replace(
            '"event":', '"reason":"internal","message":"cut \\ud83d"}'
        ),
    ),
    _l(
        "failed-no-turn",
        _line(type="turn_failed", session=None, turn=None, turn_seq=0, reason="line_too_large"),
    ),
    # --- process_exit (§5.4) ---
    _l(
        "exit-full",
        _line(type="process_exit", session=SESSION, code=0, reason="idle_ttl", turn=TURN),
    ),
    _l("exit-minimal", _line(type="process_exit", session=SESSION)),
    _l("exit-no-session", _line(type="process_exit", code=1, reason="crashed")),
    _l("exit-code-null", _line(type="process_exit", session=SESSION, code=None, reason="killed")),
    _l("exit-code-negative", _line(type="process_exit", session=SESSION, code=-9)),
    _l("exit-code-true", _line(type="process_exit", session=SESSION, code=True)),
    _l("exit-code-float", _line(type="process_exit", session=SESSION, code=1.0)),
    _l("exit-code-text", _line(type="process_exit", session=SESSION, code="1")),
    _l(
        "exit-code-past-64-bits",
        '{"type":"process_exit","session":"s","code":36893488147419103232}',
    ),
    _l("exit-reason-number", _line(type="process_exit", session=SESSION, reason=7, turn=7)),
    _l("exit-reason-long", _line(type="process_exit", session=SESSION, reason="r" * 300)),
    _l("exit-reason-empty", _line(type="process_exit", session=SESSION, reason="")),
    *(
        _l(f"exit-reason-{reason}", _line(type="process_exit", session=SESSION, reason=reason))
        for reason in ("reaped", "stopped", "shutdown")
    ),
    # --- pong and log (§5.5) ---
    _l("pong-full", _line(type="pong", nonce="5f3a9c1e")),
    _l("pong-no-nonce", _line(type="pong")),
    _l("pong-nonce-number", _line(type="pong", nonce=5)),
    _l("pong-nonce-null", _line(type="pong", nonce=None)),
    _l("pong-nonce-empty", _line(type="pong", nonce="")),
    _l("log-full", _line(type="log", level="warn", message="pi wrote to stderr", session=SESSION)),
    _l("log-minimal", _line(type="log")),
    _l("log-wrong-types", _line(type="log", level=3, message=["a"], session=7)),
    _l("log-unknown-level", _line(type="log", level="LOUD", message="x")),
    _l("log-level-empty", _line(type="log", level="", message="x", session="")),
    *(_l(f"log-level-{level}", _line(type="log", level=level)) for level in ("debug", "error")),
    _l("log-message-4096", _line(type="log", message="m" * 4096)),
    _l("log-message-4097", _line(type="log", message="m" * 4097)),
    _l("log-message-4097-two-byte", _line(type="log", message="\u00e9" * 4097)),
    _l("log-message-cut-at-astral", _line(type="log", message="m" * 4095 + "\U0001f600\U0001f600")),
    _l("log-message-cut-in-pair", '{"type":"log","message":"' + "m" * 4095 + '\\ud83d\\ude00x"}'),
    _l("log-lone-surrogate", '{"type":"log","message":"a\\ud800b"}'),
    _l("log-line-separator", _line(type="log", message="a\u2028b\u2029c")),
    _l("log-deep-unknown-field", '{"type":"log","message":"x","extra":' + _nested(200) + "}"),
    # --- entries (§5.8) ---
    _l(
        "entries-full",
        _line(
            type="entries",
            request=TURN,
            session=SESSION,
            ok=True,
            entries=[
                {"id": "e-1", "role": "user", "text": "Is the door locked?"},
                {"id": "e-2", "role": "assistant", "text": "Yes."},
            ],
            since_matched=True,
            truncated=False,
            leaf_id="e-2",
            reason=None,
        ),
    ),
    _l("entries-minimal", _line(type="entries", request=TURN, session=SESSION)),
    _l("entries-no-request", _line(type="entries", session=SESSION, ok=True)),
    _l("entries-no-session", _line(type="entries", request=TURN, ok=True)),
    _l(
        "entries-refused",
        _line(type="entries", request=TURN, session=SESSION, ok=False, reason="pi_start_failed"),
    ),
    _l(
        "entries-flags-not-true",
        _line(
            type="entries",
            request=TURN,
            session=SESSION,
            ok=1,
            since_matched="true",
            truncated=None,
        ),
    ),
    _l(
        "entries-not-a-list", _line(type="entries", request=TURN, session=SESSION, entries={"a": 1})
    ),
    _l(
        "entries-bad-items",
        _line(
            type="entries",
            request=TURN,
            session=SESSION,
            ok=True,
            entries=[
                "text",
                None,
                {"role": "user", "text": "no id"},
                {"id": 5, "role": "user", "text": "id is a number"},
                {"id": "e-1"},
                {"id": "e-2", "role": 7, "text": 7},
                {"id": "", "role": "user", "text": "empty id"},
                {"id": "e-3", "role": "user", "text": "kept", "extra": [1]},
            ],
        ),
    ),
    _l(
        "entries-65-items",
        _line(
            type="entries",
            request=TURN,
            session=SESSION,
            ok=True,
            entries=[{"id": f"e-{number}", "role": "user", "text": ""} for number in range(65)],
        ),
    ),
    _l(
        "entries-64-after-bad-items",
        _line(
            type="entries",
            request=TURN,
            session=SESSION,
            ok=True,
            entries=[None, None]
            + [{"id": f"e-{number}", "role": "user", "text": ""} for number in range(64)],
        ),
    ),
    Line(
        "entries-text-over-cap",
        parts=(
            (
                f'{{"type":"entries","request":"{TURN}","session":"{SESSION}","ok":true,'
                '"entries":[{"id":"e-1","role":"user","text":"',
                1,
            ),
            ("a", wire.MAX_ENTRY_CHARS - 1),
            ("\u00e9\u00e9", 1),
            ('"}]}', 1),
        ),
    ),
    _l(
        "entries-reason-long",
        _line(type="entries", request=TURN, session=SESSION, reason="r" * 65, leaf_id=9),
    ),
    _l(
        "entries-lone-surrogate",
        f'{{"type":"entries","request":"{TURN}","session":"{SESSION}","ok":true,'
        '"entries":[{"id":"e-\\ud800","role":"\\ud800","text":"a\\ud83d"}]}',
    ),
    # --- fatal (§5.7) ---
    _l(
        "fatal-full",
        _line(
            type="fatal", reason="control_mount_unwritable", message="EROFS on the control mount"
        ),
    ),
    _l("fatal-minimal", _line(type="fatal")),
    _l("fatal-unknown-reason", _line(type="fatal", reason="out_of_cheese")),
    _l("fatal-reason-unknown-word", _line(type="fatal", reason="unknown")),
    _l("fatal-reason-number", _line(type="fatal", reason=5, message=5)),
    _l("fatal-message-4097", _line(type="fatal", message="m" * 4097)),
    _l("fatal-mount-dir-unset", _line(type="fatal", reason="mount_dir_unset")),
    # --- the type field (§13 rule 2) ---
    _l("type-missing", "{}"),
    _l("type-missing-with-fields", _line(session=SESSION, nonce="x")),
    _l("type-null", _line(type=None)),
    _l("type-number", _line(type=5)),
    _l("type-array", _line(type=["pong"], nonce="x")),
    _l("type-unknown", _line(type="teleport")),
    _l("type-host-hello", _line(type="hello", protocol="1.0")),
    _l("type-host-ping", _line(type="ping", nonce="x")),
    _l("type-upper", _line(type="PONG", nonce="x")),
    _l("type-trailing-space", _line(type="pong ", nonce="x")),
    _l("type-empty", _line(type="", nonce="x")),
    _l("type-duplicate-last-wins", '{"type":"pong","type":"log","nonce":"x"}'),
    _l("type-duplicate-last-unknown", '{"type":"pong","nonce":"x","type":"teleport"}'),
    _l("type-lone-surrogate", '{"type":"pong\\ud800","nonce":"x"}'),
    # --- not an object (§13 rule 1) ---
    _l("top-array", '[{"type":"pong","nonce":"x"}]'),
    _l("top-string", '"pong"'),
    _l("top-number", "5"),
    _l("top-null", "null"),
    _l("top-true", "true"),
    _l("top-nan", "NaN"),
    _l("top-infinity", "-Infinity"),
    _l("top-empty-array", "[]"),
    # --- not JSON ---
    _l("json-empty", ""),
    _l("json-spaces-only", "   "),
    _l("json-text", "hello"),
    _l("json-truncated", '{"type":"pong","nonce":"x"'),
    _l("json-trailing-comma", '{"type":"pong","nonce":"x",}'),
    _l("json-trailing-text", '{"type":"pong","nonce":"x"} extra'),
    _l("json-two-objects", '{"type":"pong","nonce":"x"}{"type":"pong","nonce":"y"}'),
    _l("json-single-quotes", "{'type':'pong','nonce':'x'}"),
    _l("json-unquoted-key", '{type:"pong",nonce:"x"}'),
    _l("json-comment", '{"type":"pong","nonce":"x"} // done'),
    _l("json-leading-zero", '{"type":"process_exit","session":"s","code":01}'),
    _l("json-plus-number", '{"type":"process_exit","session":"s","code":+1}'),
    _l("json-hex-number", '{"type":"process_exit","session":"s","code":0x1}'),
    _l("json-bare-dot", '{"type":"process_exit","session":"s","code":.5}'),
    _l("json-raw-tab-in-string", '{"type":"pong","nonce":"a\tb"}'),
    _l("json-raw-nul-in-string", '{"type":"pong","nonce":"a\x00b"}'),
    _l("json-bad-escape", '{"type":"pong","nonce":"a\\xb"}'),
    _l("json-short-unicode-escape", '{"type":"pong","nonce":"\\u12"}'),
    _l("json-bom", "\ufeff" + '{"type":"pong","nonce":"x"}'),
    _l("json-nbsp-before", "\u00a0" + '{"type":"pong","nonce":"x"}'),
    _l("json-upper-true", '{"type":"session_opened","session":"s","resident":True}'),
    _l("json-undefined", '{"type":"pong","nonce":undefined}'),
    *(
        _l(f"json-number-{name}", '{"type":"pong","nonce":"x","n":' + number + "}")
        for name, number in (
            ("dot-no-digit", "1."),
            ("exponent-no-digit", "1e"),
            ("exponent-sign-no-digit", "1.5e+"),
            ("minus-only", "-"),
            ("minus-leading-zero", "-01"),
            ("minus-nan", "-NaN"),
            ("lower-infinity", "infinity"),
        )
    ),
    _l("json-literal-then-text", '{"type":"pong","nonce":"x","n":nullx}'),
    _l("json-surrogate-then-bad-escape", '{"type":"pong","nonce":"\\ud83d\\uzzzz"}'),
    _l("json-vertical-tab-before", "\x0b" + '{"type":"pong","nonce":"x"}'),
    _l("json-bad-then-huge-integer", '{"type":"pong",,"n":' + "9" * HUGE_DIGITS + "}"),
    # --- JSON that a strict reader may refuse and this one takes ---
    _l("lax-leading-spaces", '  \t{"type":"pong","nonce":"x"}'),
    _l("lax-trailing-spaces", '{"type":"pong","nonce":"x"}  \t'),
    _l("lax-inner-newline", '{"type":"pong",\n"nonce":"x"}'),
    _l("lax-trailing-cr", '{"type":"pong","nonce":"x"}\r'),
    _l("lax-escaped-slash", '{"type":"pong","nonce":"a\\/b"}'),
    _l("lax-delete-in-string", '{"type":"pong","nonce":"a\x7fb"}'),
    _l("lax-escaped-type", '{"\\u0074ype":"\\u0070ong","nonce":"x"}'),
    _l("lax-deep-unknown-200", '{"type":"pong","nonce":"x","extra":' + _nested(200) + "}"),
    _l("lax-big-exponent", '{"type":"pong","nonce":"x","n":1e400}'),
    _l("lax-many-digits-float", '{"type":"pong","nonce":"x","n":0.' + "3" * 400 + "}"),
    _l("lax-4300-digits", '{"type":"pong","nonce":"x","n":' + "9" * 4300 + "}"),
    _l("lax-negative-4300-digits", '{"type":"pong","nonce":"x","n":-' + "9" * 4300 + "}"),
    *(
        _l(f"lax-number-{name}", '{"type":"pong","nonce":"x","n":' + number + "}")
        for name, number in (
            ("minus-zero", "-0"),
            ("minus-infinity", "-Infinity"),
            ("upper-exponent", "1E+2"),
            ("zero-exponent", "0e0"),
            ("tiny-exponent", "1e-999"),
        )
    ),
    _l(
        "lax-surrogate-forms",
        '{"type":"pong","nonce":"\\ud83d\\ud83d\\ude00\\ude00\\ud83d\\u0041"}',
    ),
    _l("lax-surrogate-then-escaped-backslash", '{"type":"pong","nonce":"\\ud83d\\\\ude00"}'),
    _l("lax-upper-hex-escape", '{"type":"pong","nonce":"\\u00E9\\uD83D\\uDE00"}'),
    _l("4301-digits", '{"type":"pong","nonce":"x","n":' + "9" * 4301 + "}"),
    _l("huge-integer-then-bad-json", '{"type":"pong","nonce":"x","n":' + "9" * HUGE_DIGITS),
    _l(
        "huge-integer-exponent-no-digit",
        '{"type":"pong","nonce":"x","n":' + "9" * HUGE_DIGITS + "e}",
    ),
    _l("huge-integer-unknown-field", '{"type":"pong","nonce":"x","n":' + "9" * HUGE_DIGITS + "}"),
    _l("huge-negative-integer", '{"type":"pong","nonce":"x","n":-' + "9" * HUGE_DIGITS + "}"),
    _l("huge-float-digits", '{"type":"pong","nonce":"x","n":' + "9" * HUGE_DIGITS + ".5}"),
    Line(
        "very-deep-unknown-field",
        parts=(
            ('{"type":"pong","nonce":"x","extra":', 1),
            ("[", VERY_DEEP),
            ("]", VERY_DEEP),
            ("}", 1),
        ),
    ),
    Line("very-deep-top", parts=(("[", VERY_DEEP), ("]", VERY_DEEP))),
    Line("very-deep-then-bad-json", parts=(("[", VERY_DEEP),)),
    Line(
        "deep-unknown-field-9100-levels",
        parts=(
            ('{"type":"pong","nonce":"x","extra":', 1),
            ("[", DEEP_AND_READ - 1),
            ("]", DEEP_AND_READ - 1),
            ("}", 1),
        ),
    ),
)


def _parse_vector(line: Line) -> Vector:
    given = line.given()
    text = line.value()
    outcome = attempt(lambda: wire.parse(text))
    if isinstance(outcome, Raised):
        return raised(line.id, given, outcome.exc)

    if isinstance(outcome, wire.Refusal):
        return refused(line.id, given, outcome)

    return accepted(line.id, given, {"kind": type(outcome).__name__, "message": outcome})


# --- framing -----------------------------------------------------------------


@dataclass(frozen=True)
class Stream:
    """Chunks of bytes, in the order the host reads them, and the cap."""

    id: str
    chunks: tuple[bytes, ...]
    max_line_bytes: int = wire.MAX_LINE_BYTES


_SMALL_CAP: Final = 16

STREAMS: Final[tuple[Stream, ...]] = (
    Stream("one-line", (b'{"type":"pong","nonce":"x"}\n',)),
    Stream("two-lines-one-chunk", (b'{"a":1}\n{"b":2}\n',)),
    Stream("line-over-two-chunks", (b'{"a"', b":1}\n")),
    Stream("line-byte-by-byte", tuple(bytes([one]) for one in b"ab\n")),
    Stream("no-final-lf", (b'{"a":1}',)),
    Stream("lf-only", (b"\n",)),
    Stream("three-empty-lines", (b"\n\n\n",)),
    Stream("crlf", (b'{"a":1}\r\n',)),
    Stream("cr-cr-lf", (b'{"a":1}\r\r\n',)),
    Stream("cr-only", (b'{"a":1}\r{"b":2}\n',)),
    Stream("cr-inside", (b"a\rb\n",)),
    Stream("crlf-split", (b'{"a":1}\r', b"\n")),
    Stream("line-separator-inside", ("a\u2028b\u2029c\n".encode(),)),
    Stream("next-line-inside", ("a\u0085b\n".encode(),)),
    Stream("nul-inside", (b"a\x00b\n",)),
    Stream("form-feed-and-tab", (b"a\x0c\tb\n",)),
    Stream("bom", (b'\xef\xbb\xbf{"a":1}\n',)),
    Stream("bad-utf8", (b"a\xffb\n",)),
    Stream("bad-utf8-then-good", (b"\xff\nok\n",)),
    Stream("utf8-surrogate", (b"\xed\xa0\x80\n",)),
    Stream("utf8-overlong", (b"\xc0\xaf\n",)),
    Stream("utf8-cut-at-lf", (b"\xc3\n\xa9\n",)),
    Stream("utf8-split-over-chunks", (b"caf\xc3", b"\xa9\n")),
    Stream("utf16-bytes", ("ab\n".encode("utf-16-le"),)),
    Stream("at-the-cap", (b"a" * _SMALL_CAP + b"\n",), _SMALL_CAP),
    Stream("at-the-cap-with-cr", (b"a" * (_SMALL_CAP - 1) + b"\r\n",), _SMALL_CAP),
    Stream("one-over-the-cap", (b"a" * (_SMALL_CAP + 1) + b"\nok\n",), _SMALL_CAP),
    Stream("cap-counts-the-cr", (b"a" * _SMALL_CAP + b"\r\nok\n",), _SMALL_CAP),
    Stream("cap-counts-bytes", ("\u00e9".encode() * 9 + b"\nok\n",), _SMALL_CAP),
    Stream(
        "over-the-cap-no-lf-yet",
        (b"a" * (_SMALL_CAP + 1), b"still the same record", b"\nok\n"),
        _SMALL_CAP,
    ),
    Stream(
        "over-the-cap-then-lf-in-one-chunk",
        (b"a" * 40 + b"\nok\n",),
        _SMALL_CAP,
    ),
    Stream(
        "partial-at-the-cap",
        (b"a" * _SMALL_CAP, b"\nok\n"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-then-two-lines",
        (b"a" * (_SMALL_CAP + 1), b"x\nfirst\nsecond\n"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-with-no-lf",
        (b"a" * (_SMALL_CAP + 1), b"more of the same record", b"and more"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-then-partial",
        (b"a" * (_SMALL_CAP + 1), b"x\nfir"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-then-over-the-cap",
        (b"a" * (_SMALL_CAP + 1), b"x\n" + b"b" * (_SMALL_CAP + 1), b"more", b"\nok\n"),
        _SMALL_CAP,
    ),
    Stream("empty-chunk", (b"", b"ok\n", b"")),
)


def _raw_line(raw: wire.RawLine) -> dict[str, object]:
    return {"text": raw.text, "size": raw.size, "refusal": raw.refusal}


def _frame_vector(stream: Stream) -> Vector:
    chunks: list[Json] = [bytes_input(chunk) for chunk in stream.chunks]
    given: dict[str, Json] = {"chunks": chunks}
    params = {"max_line_bytes": stream.max_line_bytes}

    def split() -> tuple[list[list[wire.RawLine]], int]:
        splitter = wire.LineSplitter(stream.max_line_bytes)
        records = [splitter.feed(chunk) for chunk in stream.chunks]

        return records, splitter.pending_bytes()

    outcome = attempt(split)
    if isinstance(outcome, Raised):
        return raised(stream.id, given, outcome.exc, params=params)

    records, pending = outcome
    feeds = [[_raw_line(raw) for raw in one] for one in records]

    return accepted(stream.id, given, {"feeds": feeds, "pending_bytes": pending}, params=params)


# --- the host-side builders ----------------------------------------------------

#: Every object inside the arguments has its keys in sorted order. A vector
#: file sorts keys, and a builder keeps the order it is given, so any other
#: order would be lost on the way to a reader.
_WORKSPACE: Final = {"ref": "main", "repo": "example/project"}


@dataclass(frozen=True)
class Build:
    """One call of one builder: its name, its arguments and the line cap."""

    id: str
    builder: str
    args: dict[str, Any]
    max_line_bytes: int = wire.MAX_LINE_BYTES


_START: Final[dict[str, Any]] = {
    "turn": TURN,
    "session": SESSION,
    "cwd": "/workspace",
    "session_dir": f"/sessions/{SESSION}",
    "prompt": "Is the door locked?",
    "deadline_s": 600,
    "epoch": 3,
    "config_rev": "reg-9f21c4",
}

_OPEN: Final[dict[str, Any]] = {
    "session": SESSION,
    "cwd": "/workspace",
    "session_dir": f"/sessions/{SESSION}",
    "epoch": 3,
    "config_rev": "reg-9f21c4",
}

#: The three objects that the host makes for a turn, from the producer of
#: each one. A typed reader can hold these. `to_channel` writes `id` before
#: `caller_session`, so that object is the one whose key order is not the
#: sorted order.
_OWNER: Final = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
_DELEGATION_ID: Final = "01JBQ7WZ0X4T9V6K2H8M3N5PQS"
_HOST_WORKSPACE: Final = workspace_of(CODE_SANDBOX_FAMILY, _OWNER)
_HOST_BRANCH: Final = Decision(Branch.FORK, fork_from="e-4").wire_branch
_HOST_DELEGATION: Final = Delegation(_DELEGATION_ID, "chat", _OWNER).to_channel()
_HOST_DELEGATION_NO_CALLER: Final = Delegation(_DELEGATION_ID, "chat").to_channel()

BUILDS: Final[tuple[Build, ...]] = (
    Build(
        "hello-attended",
        "hello",
        {"family": "chat", "sandbox": SANDBOX, "epoch": 3, "pi_idle_ttl_s": 900},
    ),
    Build(
        "hello-every-argument",
        "hello",
        {
            "family": "vault-oracle",
            "sandbox": "vault-oracle-s12",
            "epoch": 0,
            "pi_idle_ttl_s": 0,
            "max_resident_processes": 4,
            "channel_idle_ttl_s": 120,
        },
    ),
    Build("open-session-minimal", "open_session", dict(_OPEN)),
    Build(
        "open-session-every-argument",
        "open_session",
        {**_OPEN, "model": "code-router", "workspace": _WORKSPACE},
    ),
    Build("open-session-empty-model", "open_session", {**_OPEN, "model": "", "workspace": {}}),
    Build(
        "open-session-host-shapes",
        "open_session",
        {**_OPEN, "model": "code-router", "workspace": _HOST_WORKSPACE},
    ),
    Build("open-session-empty-model-only", "open_session", {**_OPEN, "model": ""}),
    Build("get-entries-minimal", "get_entries", {"request": TURN, **_OPEN}),
    Build(
        "get-entries-every-argument",
        "get_entries",
        {"request": TURN, **_OPEN, "since": "e-7", "workspace": _WORKSPACE},
    ),
    Build("get-entries-empty-since", "get_entries", {"request": TURN, **_OPEN, "since": ""}),
    Build(
        "get-entries-host-shapes",
        "get_entries",
        {"request": TURN, **_OPEN, "since": "e-7", "workspace": _HOST_WORKSPACE},
    ),
    Build("start-turn-minimal", "start_turn", dict(_START)),
    Build(
        "start-turn-every-argument",
        "start_turn",
        {
            **_START,
            "persona": "Answer in one sentence.",
            "attachments": ["notes.txt", "plan-2.pdf"],
            "workspace": _WORKSPACE,
            "branch": {"from_entry": "e-4"},
            "delegation": {"caller": "chat", "id": "01JBQ7WZ0X4T9V6K2H8M3N5PQS"},
        },
    ),
    Build(
        "start-turn-empty-optionals",
        "start_turn",
        {**_START, "persona": "", "attachments": [], "workspace": {}, "branch": {}},
    ),
    Build(
        "start-turn-text-forms",
        "start_turn",
        {
            **_START,
            "prompt": 'caf\u00e9 "quoted" \\ / \u2028 \u2029 \U0001f600 \x7f\x00\x1f\n\t</script>',
        },
    ),
    Build(
        "start-turn-numbers",
        "start_turn",
        {**_START, "deadline_s": 0, "epoch": 2**64, "workspace": {"f": 1.0, "g": 1e100, "h": 0.1}},
    ),
    Build("start-turn-lone-surrogate", "start_turn", {**_START, "prompt": "a\ud800b"}),
    Build(
        "start-turn-host-shapes",
        "start_turn",
        {
            **_START,
            "persona": "Answer in one sentence.",
            "attachments": ["notes.txt", "plan-2.pdf"],
            "workspace": _HOST_WORKSPACE,
            "branch": _HOST_BRANCH,
            "delegation": _HOST_DELEGATION,
        },
    ),
    Build(
        "start-turn-host-delegation-no-caller",
        "start_turn",
        {**_START, "delegation": _HOST_DELEGATION_NO_CALLER},
    ),
    Build("start-turn-empty-texts", "start_turn", {**_START, "persona": "", "attachments": []}),
    Build(
        "start-turn-number-limits",
        "start_turn",
        {**_START, "deadline_s": 0, "epoch": 2**64 - 1},
    ),
    Build("start-turn-over-the-cap", "start_turn", dict(_START), 64),
    Build("steer", "steer", {"session": SESSION, "turn": TURN, "message": "Check the garage too."}),
    Build("abort", "abort", {"session": SESSION, "turn": TURN}),
    Build("stop-process-default", "stop_process", {"session": SESSION}),
    Build("stop-process-grace", "stop_process", {"session": SESSION, "grace_ms": 0}),
    Build("ping", "ping", {"nonce": "5f3a9c1e"}),
    Build("ping-empty-nonce", "ping", {"nonce": ""}),
    Build("shutdown-default", "shutdown", {}),
    Build("shutdown-grace", "shutdown", {"grace_ms": 250}),
)

_BUILDERS: Final[dict[str, Callable[..., dict[str, Any]]]] = {
    "hello": wire.hello,
    "open_session": wire.open_session,
    "get_entries": wire.get_entries,
    "start_turn": wire.start_turn,
    "steer": wire.steer,
    "abort": wire.abort,
    "stop_process": wire.stop_process,
    "ping": wire.ping,
    "shutdown": wire.shutdown,
}


def _build_vector(build: Build) -> Vector:
    given: dict[str, Json] = {"args": normalize(build.args)}
    params = {"builder": build.builder, "max_line_bytes": build.max_line_bytes}
    message = attempt(lambda: _BUILDERS[build.builder](**build.args))
    if isinstance(message, Raised):
        return raised(build.id, given, message.exc, params=params)

    line = attempt(lambda: wire.encode(message, build.max_line_bytes))
    if not isinstance(line, Raised):
        return accepted(build.id, given, params=params, output=text_input(line))

    if isinstance(line.exc, UnicodeEncodeError):
        # A ValueError too, so every caller of `encode` takes it as the
        # refusal. Only the type is kept: the message names an offset.
        refusal = {"exception": type(line.exc).__name__}
        return refused(build.id, given, refusal, params=params)

    if isinstance(line.exc, ValueError):
        return refused(build.id, given, {"message": str(line.exc)}, params=params)

    return raised(build.id, given, line.exc, params=params)


def _vectors[T](items: Sequence[T], make: Callable[[T], Vector]) -> tuple[Vector, ...]:
    return tuple(make(item) for item in items)


def surfaces() -> tuple[Surface, ...]:
    return (
        Surface(
            name="channel.parse",
            path="channel/parse.json",
            entry="attendance.wire.parse",
            contract=f"{CONTRACT} §5, §13",
            notes=(
                "The input is one record, after the line splitter took the LF and one CR away.",
                "value.kind is the name of the message class. value.message holds its fields.",
                "refusal is the reason the host drops the line with.",
                "The reasons too_large and bad_utf8 belong to the surface channel.frame. The "
                "reasons unknown_address and sequence_gap need the state of a channel. No "
                "vector here gives them.",
            ),
            vectors=_vectors(LINES, _parse_vector),
        ),
        Surface(
            name="channel.frame",
            path="channel/frame.json",
            entry="attendance.wire.LineSplitter.feed",
            contract=f"{CONTRACT} §2",
            notes=(
                "The input is the chunks of one byte stream, in the order the host reads them.",
                "params.max_line_bytes is the cap the splitter starts with. The host uses "
                f"{wire.MAX_LINE_BYTES}. A vector about the cap uses a small one.",
                "value.feeds holds one array per chunk: the records that chunk completed, or "
                "refused. size is the bytes of the record with no LF. value.pending_bytes is "
                "what the splitter still holds after the last chunk.",
            ),
            vectors=_vectors(STREAMS, _frame_vector),
        ),
        Surface(
            name="channel.build",
            path="channel/build.json",
            entry="attendance.wire.encode(attendance.wire.<params.builder>(**args))",
            contract=f"{CONTRACT} §3, §4",
            notes=(
                "The input is the named arguments of the builder that params.builder names.",
                "A builder keeps the key order of an object it is given. Every object "
                "inside args has its keys in sorted order, with one exception.",
                "The exception is the delegation of a vector whose id starts with "
                "start-turn-host-. The host makes that object with "
                "attendance.jobs.Delegation.to_channel, which writes the key id and then "
                "the key caller_session.",
                "In a vector whose id holds host-shapes, workspace, branch and delegation "
                "are the objects that the host makes: attendance.workspace.workspace_of, "
                "attendance.branching.Decision.wire_branch and to_channel.",
                "output is the exact line the host writes, with its LF.",
                "A refused vector is an input for which the entry point raises ValueError. "
                "Each caller of the entry point catches that type.",
                "refusal.message is the text of the ValueError for a line over "
                "params.max_line_bytes.",
                "refusal.exception is the name of a subtype of ValueError. A text with a lone "
                "surrogate has no UTF-8 form, and the entry point raises UnicodeEncodeError.",
            ),
            vectors=_vectors(BUILDS, _build_vector),
        ),
    )
