"""The two logs of the chaperone (contract 04 §6), and its reasons (§5).

Three surfaces:

- `chaperone.audit_line`: the fields of one audit record v2 to the exact
  line that `FamilyAudit.write` appends.
- `chaperone.unidentified_line`: one request that names no family to the
  exact line that the chaperone appends to its other log.
- `chaperone.reason`: one text to the HTTP status of an answer with that
  reason, or to a refusal when the text is not a reason.

Each record holds the time of the write. The generator gives the two
modules that read the clock a clock of its own, so a vector holds no time
of the machine. It changes no file of the product.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Final
from unittest.mock import patch

import chaperone.audit as audit_module
import chaperone.family_audit as family_audit_module
from chaperone.app import MAX_REQUEST_BODY_BYTES, CallBody, PepConfig, create_app
from chaperone.family_audit import AuditEntry, FamilyAudit, Outcome, Sandbox
from chaperone.family_decisions import FAMILY_DENY_STATUS
from chaperone.headers import Claimed
from starlette.testclient import TestClient

from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    normalize,
    raised,
    refused,
    text_input,
)
from vectors.surfaces.grants import BodyReader, call_reader

CONTRACT: Final = "contract 04"

FAMILY: Final = "chat"
REV: Final = "reg-9f21c4"
GATE: Final = "0123456789abcdef"
SESSION: Final = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"
TURN: Final = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
DELEGATION: Final = "01JBQ7WZ1A2B3C4D5E6F7G8H9J"

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

#: 2026-09-18T19:41:07.412999Z, in microseconds after the epoch. The last
#: three digits show that a record cuts a time to milliseconds.
AT_US: Final = 1_789_760_467_412_999

#: The cap of one string of `args` in an audit record (contract 04 §6.1).
CAP: Final = 8 * 1024

#: The audit name of a manifest fetch. A fetch is not a call and has no body.
MANIFEST: Final = "$manifest"

HTTP_FORBIDDEN: Final = 403
HTTP_TOO_MANY: Final = 429
HTTP_TOO_LARGE: Final = 413

JSON_HEADERS: Final = {"content-type": "application/json"}


@dataclass(frozen=True)
class Clock:
    """What stands for `datetime` in a module that reads the clock."""

    at: datetime

    def now(self, tz: tzinfo | None = None) -> datetime:
        del tz  # each caller asks for UTC, and `at` is UTC
        return self.at


def _at(micros: int) -> datetime:
    return EPOCH + timedelta(microseconds=micros)


@contextmanager
def _clock(micros: int) -> Generator[None]:
    """The two modules that write a record read this time and no other."""
    clock = Clock(_at(micros))
    with (
        patch.object(audit_module, "datetime", clock),
        patch.object(family_audit_module, "datetime", clock),
    ):
        yield


def _one_line(directory: Path) -> tuple[str, bytes]:
    """The name and the bytes of the one file of a log, and the file removed."""
    (path,) = sorted(directory.iterdir())
    raw = path.read_bytes()
    path.unlink()

    return path.name, raw


def _body(tool: str, args: str = "{}") -> str:
    """The body of a `POST /call`. `args` is the JSON text of the arguments."""
    return f'{{"tool":"{tool}","args":{args}}}'


# --- the audit record v2 -------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """One audit record: the body of the call and what the chaperone decided."""

    id: str
    #: The body of the `POST /call`. `None` stands for a manifest fetch.
    call: str | None = _body("embed", '{"input":"where is the note"}')
    decision: Outcome = Outcome.ALLOW
    reason: str = "granted"
    at_us: int = AT_US
    family: str = FAMILY
    grants_rev: str | None = REV
    sandbox_id: str | None = None
    sandbox_id_trusted: bool = False
    latency_ms: int | None = 38
    waited_ms: int = 0
    gate: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    delegation_id: str | None = None
    chain: tuple[str, ...] = ()

    def args(self) -> dict[str, object]:
        return {
            "at_us": self.at_us,
            "family": self.family,
            "call": self.call,
            "decision": self.decision,
            "reason": self.reason,
            "grants_rev": self.grants_rev,
            "sandbox_id": self.sandbox_id,
            "sandbox_id_trusted": self.sandbox_id_trusted,
            "latency_ms": self.latency_ms,
            "waited_ms": self.waited_ms,
            "gate": self.gate,
            "claimed": {
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "delegation_id": self.delegation_id,
            },
            "chain": self.chain,
        }


def _denied(reason: str) -> Record:
    return Record(f"deny-{reason.replace('_', '-')}", decision=Outcome.DENY, reason=reason)


#: Each reason of contract 04 §5 and §6.1 that a denial carries.
_DENIALS: Final = (
    "unknown_token",
    "rate_limited",
    "arg_validation",
    "tool_not_granted",
    "not_implemented",
    "upstream_failed",
    "internal_error",
    "delegate_timeout",
    "approval_denied",
    "approval_timeout",
    "approval_revoked",
    "approval_undeliverable",
    "approval_abandoned",
)

_LONG: Final = "a" * CAP

RECORDS: Final[tuple[Record, ...]] = (
    Record("allow-granted"),
    Record("allow-manifest", call=None, latency_ms=None),
    Record("allow-approved", reason="approved", gate=GATE, waited_ms=41_250),
    Record("allow-upstream-failed", reason="upstream_failed", latency_ms=30_000),
    Record("allow-delegate-timeout", reason="delegate_timeout", latency_ms=120_000),
    Record(
        "pending",
        decision=Outcome.PENDING,
        reason="approval_required",
        gate=GATE,
        latency_ms=None,
    ),
    *(_denied(reason) for reason in _DENIALS),
    Record(
        "deny-after-a-wait",
        decision=Outcome.DENY,
        reason="approval_timeout",
        gate=GATE,
        waited_ms=900_000,
        latency_ms=None,
    ),
    # --- the trusted fields ---
    Record("no-grants-rev", grants_rev=None),
    Record("sandbox-trusted", sandbox_id="chat-s3", sandbox_id_trusted=True),
    Record("latency-zero", latency_ms=0),
    # --- the claimed fields and the chain ---
    Record("claimed-every-field", session_id=SESSION, turn_id=TURN, delegation_id=DELEGATION),
    Record("claimed-session-only", session_id=SESSION),
    Record(
        "chain-of-two",
        family="vault-oracle",
        delegation_id=DELEGATION,
        chain=("chat", "vault-oracle"),
    ),
    Record("chain-of-one-given", chain=(FAMILY,)),
    # --- the time ---
    Record("time-epoch", at_us=0),
    Record("time-last-microsecond-of-a-day", at_us=1_789_775_999_999_999),
    Record("time-first-of-a-day", at_us=1_789_776_000_000_000),
    Record("time-leap-day", at_us=1_709_210_096_789_000),
    Record("time-end-of-year", at_us=1_798_761_599_999_999),
    Record("time-year-9999", at_us=253_402_300_799_999_999),
    Record("time-one-millisecond", at_us=1_000),
    # --- the tool ---
    Record("tool-with-server", call=_body("kagi__kagi_search_fetch", '{"query":"weather"}')),
    Record("tool-any-text", call='{"tool":"not a tool\\n\\"../x\\" caf\\u00e9"}'),
    Record("tool-no-args", call='{"tool":"embed"}'),
    # --- the arguments ---
    Record("args-key-order", call=_body("embed", '{"b":1,"a":2,"c":{"z":1,"y":2}}')),
    Record("args-duplicate-key", call=_body("embed", '{"a":1,"b":2,"a":3}')),
    Record(
        "args-nested",
        call=_body("ha_call", '{"a":{"b":[1,2.5,"c",null,true,false,[],{}]},"d":{}}'),
    ),
    Record(
        "args-text-outside-ascii",
        call=_body("embed", '{"caf\\u00e9":"na\\u00efve \\u2028 \\ud83d\\ude00 \\u007f"}'),
    ),
    Record(
        "args-text-escapes",
        call=_body("embed", '{"a":"\\" \\\\ \\/ \\b \\f \\n \\r \\t \\u0000 \\u001f"}'),
    ),
    Record(
        "args-integers",
        call=_body(
            "embed", '{"a":0,"b":-0,"c":-7,"d":18446744073709551616,"e":-9223372036854775809}'
        ),
    ),
    Record(
        "args-floats",
        call=_body(
            "embed",
            '{"a":1.0,"b":1e2,"c":-0.0,"d":0.1,"e":1e16,"f":1e15,"g":1e-5,"h":0.0001,'
            '"i":123456789.125,"j":5e-324,"k":1.7976931348623157e308,"l":1e22,'
            '"m":9007199254740993.0,"n":-1.5e-7,"o":12345678901234567890.0,"p":0.1e1}',
        ),
    ),
    Record(
        "args-floats-between-two-decimals",
        call=_body(
            "embed",
            '{"a":1160972656570364.25,"b":1160972656570364.75,"c":2000000000000000.25,"d":2.5}',
        ),
    ),
    Record(
        "args-floats-not-finite",
        call=_body("embed", '{"a":NaN,"b":Infinity,"c":-Infinity,"d":1e400,"e":-1e400}'),
    ),
    Record("args-text-at-the-cap", call=_body("embed", f'{{"input":"{_LONG}"}}')),
    Record("args-text-over-the-cap", call=_body("embed", f'{{"input":"{_LONG}b"}}')),
    Record(
        "args-text-at-the-cap-astral",
        call=_body("embed", f'{{"input":"{_LONG[:-1]}\\ud83d\\ude00"}}'),
    ),
    Record(
        "args-text-over-the-cap-astral",
        call=_body("embed", f'{{"input":"{_LONG[:-1]}\\ud83d\\ude00\\ud83d\\ude00"}}'),
    ),
    Record(
        "args-text-over-the-cap-nested",
        call=_body("embed", f'{{"a":[{{"b":"{_LONG}c"}},"{_LONG}d"],"e":"short"}}'),
    ),
    Record("args-key-over-the-cap", call=_body("embed", f'{{"{_LONG}k":"v"}}')),
)


def _read(reader: BodyReader, call: str | None) -> tuple[str, dict[str, object]]:
    """The tool and the arguments, as the chaperone reads them from a body."""
    if call is None:
        return MANIFEST, {}

    _, body, detail = reader.read(call.encode("utf-8"))
    if not isinstance(body, CallBody):
        raise ValueError(f"the chaperone refuses the body of a vector: {detail}")

    return body.tool, body.args


def _record_vector(record: Record, reader: BodyReader, directory: Path) -> Vector:
    given: dict[str, Json] = {"args": normalize(record.args())}
    tool, args = _read(reader, record.call)
    entry = AuditEntry(
        family=record.family,
        tool=tool,
        args=args,
        outcome=record.decision,
        reason=record.reason,
        grants_rev=record.grants_rev,
        sandbox=Sandbox(record.sandbox_id, record.sandbox_id_trusted),
        latency_ms=record.latency_ms,
        waited_ms=record.waited_ms,
        gate=record.gate,
        claimed=Claimed(record.session_id, record.turn_id, record.delegation_id),
        chain=record.chain,
    )
    log = FamilyAudit(directory)
    with _clock(record.at_us):
        outcome = attempt(lambda: log.write(entry))

    if isinstance(outcome, Raised):
        return raised(record.id, given, outcome.exc)

    name, raw = _one_line(directory)

    return accepted(record.id, given, file=name, output=text_input(raw.decode("utf-8")))


# --- the record of a request that names no family ------------------------------------


@dataclass(frozen=True)
class Request:
    """One request that the chaperone answers before it knows a family."""

    id: str
    #: The body of a `POST /call`. `None` stands for a `GET /manifest`.
    call: str | None = _body("embed", '{"input":"where is the note"}')
    #: Whether the bucket for such requests is empty when the request comes.
    limited: bool = False
    #: The count of bytes of a body over the cap. 0 for each other request.
    oversized_bytes: int = 0
    at_us: int = AT_US

    def args(self) -> dict[str, object]:
        return {
            "at_us": self.at_us,
            "call": self.call,
            "limited": self.limited,
            "oversized_bytes": self.oversized_bytes,
        }


REQUESTS: Final[tuple[Request, ...]] = (
    Request("unknown-token"),
    Request("unknown-token-manifest", call=None),
    Request("unknown-token-no-args", call='{"tool":"embed"}'),
    Request("rate-limited", limited=True),
    Request("rate-limited-manifest", call=None, limited=True),
    Request("oversized-by-one", call=None, oversized_bytes=MAX_REQUEST_BODY_BYTES + 1),
    Request("oversized", call=None, oversized_bytes=2 * MAX_REQUEST_BODY_BYTES),
    Request("time-epoch", at_us=0),
    Request("time-last-microsecond-of-a-day", at_us=1_789_775_999_999_999),
    Request("tool-any-text", call='{"tool":"not a tool\\n\\"../x\\" caf\\u00e9"}'),
    Request("args-key-order", call=_body("embed", '{"b":1,"a":2,"c":{"z":1,"y":[{"k":1,"j":2}]}}')),
    Request("args-duplicate-key", call=_body("embed", '{"b":1,"a":2,"b":3}')),
    Request(
        "args-text-outside-ascii",
        call=_body(
            "embed", '{"caf\\u00e9":"na\\u00efve \\u2028 \\ud83d\\ude00 \\u007f","Z":1,"a":2}'
        ),
    ),
    Request(
        "args-text-escapes",
        call=_body("embed", '{"a":"\\" \\\\ \\/ \\b \\f \\n \\r \\t \\u0000 \\u001f"}'),
    ),
    Request(
        "args-numbers",
        call=_body(
            "embed",
            '{"a":0,"b":-0,"c":18446744073709551616,"d":1.0,"e":1e16,"f":1e-5,"g":-0.0,'
            '"h":NaN,"i":Infinity,"j":-Infinity,"k":true,"l":null}',
        ),
    ),
    Request("args-text-over-the-audit-cap", call=_body("embed", f'{{"input":"{_LONG}b"}}')),
)


def _send(client: TestClient, request: Request) -> int:
    """Sends one request with no bearer. Answers the status."""
    if request.oversized_bytes:
        body = b" " * request.oversized_bytes
        return client.post("/call", content=body, headers=JSON_HEADERS).status_code

    if request.call is None:
        return client.get("/manifest").status_code

    return client.post(
        "/call", content=request.call.encode("utf-8"), headers=JSON_HEADERS
    ).status_code


def _expected_status(request: Request) -> int:
    if request.oversized_bytes:
        return HTTP_TOO_LARGE

    return HTTP_TOO_MANY if request.limited else HTTP_FORBIDDEN


#: How many times the generator sends a request that must meet an empty
#: bucket. The bucket fills again with time: one token in two seconds. On a
#: slow machine a token can come between the last take and the request.
LIMITED_ATTEMPTS: Final = 5


def _answer(request: Request, root: Path) -> tuple[int, Path] | Raised:
    """One request to a chaperone of its own. Answers the status and the log."""
    directory = root / "unidentified"
    app = create_app(PepConfig(audit_dir=directory, rework_dir=root / "rework"))
    if request.limited:
        while app.state.unauth_bucket.take():
            pass

    client = TestClient(app)
    with _clock(request.at_us):
        outcome = attempt(lambda: _send(client, request))

    if isinstance(outcome, Raised):
        return outcome

    return outcome, directory


def _request_vector(request: Request, scratch: Path) -> Vector:
    given: dict[str, Json] = {"args": normalize(request.args())}
    wanted = _expected_status(request)
    attempts = LIMITED_ATTEMPTS if request.limited else 1
    status = 0
    for number in range(attempts):
        answer = _answer(request, scratch / f"{request.id}-{number}")
        if isinstance(answer, Raised):
            return raised(request.id, given, answer.exc)

        status, directory = answer
        if status == wanted:
            name, raw = _one_line(directory)

            return accepted(request.id, given, file=name, output=text_input(raw.decode("utf-8")))

    raise ValueError(f"{request.id}: the chaperone answers {status}")


# --- the reasons ---------------------------------------------------------------------

#: Texts that are not a reason of an answer, each with the id of its vector.
#: The first three are the reason of an audit record that is not a denial.
NOT_REASONS: Final = (
    ("granted", "granted"),
    ("approved", "approved"),
    ("approval-required", "approval_required"),
    ("body-too-large", "body_too_large"),
    ("unknown-gate", "unknown_gate"),
    ("empty", ""),
    ("upper-case", "Unknown_Token"),
    ("hyphen", "unknown-token"),
    ("space-first", " unknown_token"),
    ("newline-last", "unknown_token\n"),
    ("decision-allow", "allow"),
    ("decision-deny", "deny"),
)


def _reason_vectors() -> tuple[Vector, ...]:
    statuses = {str(reason): status for reason, status in FAMILY_DENY_STATUS.items()}
    reasons = tuple(
        accepted(reason.replace("_", "-"), text_input(reason), http_status=status)
        for reason, status in statuses.items()
    )
    others = tuple(
        accepted(f"not-a-reason-{name}", text_input(text), http_status=statuses[text])
        if text in statuses
        else refused(f"not-a-reason-{name}", text_input(text))
        for name, text in NOT_REASONS
    )

    return (*reasons, *others)


_NOTE_TIME: Final = (
    "args.at_us is the time of the write: microseconds after 1970-01-01T00:00:00Z. The "
    "generator gives the entry point a clock that answers this time."
)
_NOTE_CALL: Final = (
    "args.call is the body of the POST /call, as text. The tool and the arguments of the "
    "record are what the chaperone reads from that body (the surface chaperone.call_body). "
    "A record keeps the key order of the arguments. args.call null stands for a GET "
    "/manifest: the tool is $manifest and the arguments are an empty object."
)
_NOTE_OUTPUT: Final = (
    "output is the exact line that the entry point appends, with its LF. file is the name "
    "of the file that takes the line: the UTC day of the write."
)


def surfaces() -> tuple[Surface, ...]:
    reader = call_reader()
    with tempfile.TemporaryDirectory(prefix="vectors-audit-") as scratch_name, reader.client:
        scratch = Path(scratch_name)
        records = tuple(_record_vector(record, reader, scratch / "audit") for record in RECORDS)
        requests = tuple(_request_vector(request, scratch) for request in REQUESTS)

    return (
        Surface(
            name="chaperone.audit_line",
            path="chaperone/audit_line.json",
            entry="chaperone.family_audit.FamilyAudit.write",
            contract=f"{CONTRACT} §6",
            notes=(
                "The input is the fields of one audit record v2.",
                _NOTE_TIME,
                _NOTE_CALL,
                "args.decision and args.reason are the two fields of contract 04 §6.1. "
                "args.chain is the chain that the chaperone built from its own delegation "
                "ids. An empty chain stands for a chain of one, this family.",
                _NOTE_OUTPUT,
                f"A string of the arguments has {CAP} characters or less in the record. A "
                "character is one code point. The entry point cuts a longer string and "
                "adds a marker. It does not cut a key.",
            ),
            vectors=records,
        ),
        Surface(
            name="chaperone.unidentified_line",
            path="chaperone/unidentified_line.json",
            entry="chaperone.app.create_app, a request with no bearer",
            contract=f"{CONTRACT} §5",
            notes=(
                "The input is one request that names no family. The chaperone refuses it "
                "and appends one line to the log for such requests.",
                _NOTE_TIME,
                _NOTE_CALL,
                "args.limited true means that the bucket for such requests is empty. The "
                "reason is then rate_limited, and unknown_token in each other case.",
                "args.oversized_bytes is the count of bytes of a body over the cap of "
                f"{MAX_REQUEST_BODY_BYTES} bytes, or 0. The chaperone refuses such a body "
                "before it reads a bearer or a tool.",
                _NOTE_OUTPUT,
                "The record holds no argument. args_bytes is the count of bytes of the "
                "arguments as JSON with sorted keys, a space after each comma and each "
                "colon, and text outside ASCII as it is. args_sha256 is the SHA-256 of "
                "those bytes.",
            ),
            vectors=requests,
        ),
        Surface(
            name="chaperone.reason",
            path="chaperone/reason.json",
            entry="chaperone.family_decisions.FAMILY_DENY_STATUS",
            contract=f"{CONTRACT} §5, §6.1",
            notes=(
                "The input is one text. An accepted vector is a reason of an answer that "
                "is not HTTP 200, and http_status is the status of that answer.",
                "A refused vector is a text that is not such a reason.",
                "The surface holds each reason that the entry point holds.",
            ),
            vectors=_reason_vectors(),
        ),
    )
