"""Request bodies, validated before anything uses them (contract 02 §12, §13.4).

A door is another process, so every field here is untrusted input: shape and
size are checked before use (invariants 12 and 14). Each refusal carries a
code from contract 02 §14, never a Python traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .atomic import as_array
from .clock import parse_rfc3339
from .dispatch import JOBS_LIMIT_DEFAULT, JOBS_LIMIT_MAX
from .errors import ApiError, ErrorCode
from .ids import is_family, is_session, is_ulid
from .jobs import MESSAGE_MAX_BYTES
from .models import CHAIN_MAX_FAMILIES, TriggerKind

# Contract 04 §4.1's own cap on `enqueue.idempotency_key`.
DISPATCH_KEY_MAX = 128

# `door-trigger`'s payload cap is 200 000 bytes, and its frame of the
# payload adds a few lines.
TRIGGER_MESSAGE_MAX_BYTES = 262_144

# The kinds of a firing of `door-trigger`. A dispatch comes from the PEP.
_FIRED_KINDS = {
    TriggerKind.TIMER.value: TriggerKind.TIMER,
    TriggerKind.WEBHOOK.value: TriggerKind.WEBHOOK,
}

# The message for a body that is not JSON. A text with no UTF-8 form gets
# the same message: such a body is not JSON in UTF-8.
NOT_JSON = "body is not JSON"


class SwitchMode(StrEnum):
    """Contract 05 §5.1."""

    DRAIN = "drain"
    INTERRUPT = "interrupt"


@dataclass(slots=True)
class DelegateRequest:
    """Contract 04 §7.3's body, which the PEP alone sends."""

    caller_family: str
    target_family: str
    delegation_id: str
    message: str
    claimed_session_id: str | None = None


@dataclass(slots=True)
class DispatchRequest:
    """Contract 02 §13.4.1's body, which the PEP alone sends.

    `caller_family` is trusted for the same reason `DelegateRequest`'s is:
    the PEP read it from the family token before it built this call.
    """

    caller_family: str
    target_family: str
    delegation_id: str
    message: str
    chain: tuple[str, ...] = ()
    claimed_session_id: str | None = None
    idempotency_key: str | None = None


@dataclass(slots=True, frozen=True)
class TriggerRequest:
    """The body of `POST /trigger`, which `door-trigger` alone sends.

    A webhook firing names its trigger. A timer firing names none.
    """

    family: str
    kind: TriggerKind
    name: str | None
    message: str
    idempotency_key: str


@dataclass(slots=True)
class JobQuery:
    """Contract 02 §13.4.2's body. The scope is `caller_family` and nothing
    a caller adds can widen it."""

    caller_family: str
    session: str | None = None
    since: datetime | None = None
    limit: int = JOBS_LIMIT_DEFAULT


@dataclass(slots=True)
class SwitchRequest:
    """Contract 05 §5.1's request body."""

    family: str
    to: str
    mode: SwitchMode
    reason: str
    outgoing: str | None = None
    deadline_s: int = 300


def read_delegate(raw: dict[str, Any]) -> DelegateRequest:
    """Contract 04 §7.3. The PEP is another process, so this is untrusted.

    `claimed_session_id` is advisory for the audit, and for `coding-sandbox` it
    also becomes a path component (contract 02 §12.1). A malformed one is
    refused here rather than dropped, because dropping it would silently run
    a job in the wrong directory.
    """
    caller = _require_text(raw, "caller_family")
    target = _require_text(raw, "target_family")

    for name, value in (("caller_family", caller), ("target_family", target)):
        if not is_family(value):
            raise _bad(f"{name} is not a family name", family=value)

    delegation_id = _require_text(raw, "delegation_id")

    if not is_ulid(delegation_id):
        raise _bad("delegation_id is not a ULID", family=target)

    message = _require_text(raw, "message", target)
    _check_bytes(message, MESSAGE_MAX_BYTES, "message", target, "")
    owner = _optional_text(raw, "claimed_session_id")

    if owner is not None and not is_session(owner):
        raise _bad("claimed_session_id is not a session id", family=target)

    return DelegateRequest(
        caller_family=caller,
        target_family=target,
        delegation_id=delegation_id,
        message=message,
        claimed_session_id=owner,
    )


def read_dispatch(raw: dict[str, Any]) -> DispatchRequest:
    """Contract 02 §13.4.1. The PEP is another process, so this is untrusted.

    Every field is checked here rather than where it is used, so one refusal
    table serves the door and nothing half-validated reaches a session.
    """
    caller = _require_text(raw, "caller_family")
    target = _require_text(raw, "target_family")

    for name, value in (("caller_family", caller), ("target_family", target)):
        if not is_family(value):
            raise _bad(f"{name} is not a family name", family=value)

    delegation_id = _require_text(raw, "delegation_id")

    if not is_ulid(delegation_id):
        raise _bad("delegation_id is not a ULID", family=target)

    message = _require_text(raw, "message", target)
    _check_bytes(message, MESSAGE_MAX_BYTES, "message", target, "")
    owner = _optional_text(raw, "claimed_session_id")

    if owner is not None and not is_session(owner):
        raise _bad("claimed_session_id is not a session id", family=target)

    return DispatchRequest(
        caller_family=caller,
        target_family=target,
        delegation_id=delegation_id,
        message=message,
        chain=_chain(raw.get("chain"), TriggerKind.DISPATCH, target, ""),
        claimed_session_id=owner,
        idempotency_key=_dispatch_key(raw, target),
    )


def read_trigger(raw: dict[str, Any]) -> TriggerRequest:
    """`POST /trigger`. `door-trigger` is another process, so this is untrusted."""
    family = _require_text(raw, "family")

    if not is_family(family):
        raise _bad("family is not a family name", family=family)

    kind = _FIRED_KINDS.get(_require_text(raw, "kind", family))

    if kind is None:
        raise _bad("kind is not timer or webhook", family=family)

    name = _trigger_name(raw.get("name"), kind, family)
    message = _require_text(raw, "message", family)
    _check_bytes(message, TRIGGER_MESSAGE_MAX_BYTES, "message", family, "")
    key = _require_text(raw, "idempotency_key", family)

    if len(key) > DISPATCH_KEY_MAX:
        raise _bad(f"idempotency_key is over {DISPATCH_KEY_MAX} characters", family=family)

    return TriggerRequest(
        family=family,
        kind=kind,
        name=name,
        message=message,
        idempotency_key=key,
    )


def _trigger_name(value: object, kind: TriggerKind, family: str) -> str | None:
    """A webhook firing names its trigger, a family-name slug. A timer names none."""
    if kind is TriggerKind.TIMER and value is None:
        return None

    if kind is TriggerKind.WEBHOOK and isinstance(value, str) and is_family(value):
        return value

    raise _bad("a webhook firing names its trigger, and a timer firing names none", family=family)


def read_jobs(raw: dict[str, Any]) -> JobQuery:
    """Contract 02 §13.4.2. The caller family decides the scope, and it is
    the one field this service checks against a token."""
    return _job_query(raw, "caller_family")


def read_trigger_jobs(raw: dict[str, Any]) -> JobQuery:
    """`POST /trigger/jobs`. `family` decides the scope: its trigger ledger."""
    return _job_query(raw, "family")


def _job_query(raw: dict[str, Any], scope: str) -> JobQuery:
    caller = _require_text(raw, scope)

    if not is_family(caller):
        raise _bad(f"{scope} is not a family name", family=caller)

    session = _optional_text(raw, "session")

    if session is not None and not is_session(session):
        raise _bad("session is not a session id", family=caller, session=session)

    return JobQuery(
        caller_family=caller,
        session=session,
        since=_since(raw, caller),
        limit=_bounded(_optional_int(raw, "limit"), JOBS_LIMIT_DEFAULT, 1, JOBS_LIMIT_MAX, "limit"),
    )


def _since(raw: dict[str, Any], family: str) -> datetime | None:
    """§13.4.2's `since`. Absent means every entry the limit reaches."""
    text = _optional_text(raw, "since")

    if text is None:
        return None

    moment = parse_rfc3339(text)

    if moment is None:
        raise _bad("since is not RFC 3339", family=family)

    return moment


def _dispatch_key(raw: dict[str, Any], family: str) -> str | None:
    """§13.4.4's key, at contract 04 §4.1's own 128 character cap."""
    key = _stored_text(raw, "idempotency_key")

    if key is None:
        return None

    if len(key) > DISPATCH_KEY_MAX:
        raise _bad(f"idempotency_key is over {DISPATCH_KEY_MAX} characters", family=family)

    return key


def read_switch(raw: dict[str, Any]) -> SwitchRequest:
    family = _require_text(raw, "family")
    to_sandbox = _require_text(raw, "to")
    mode_text = _require_text(raw, "mode")

    try:
        mode = SwitchMode(mode_text)
    except ValueError as error:
        raise _bad("mode is not drain or interrupt", family=family) from error

    return SwitchRequest(
        family=family,
        to=to_sandbox,
        mode=mode,
        reason=_stored_text(raw, "reason") or "",
        outgoing=_optional_text(raw, "from"),
        deadline_s=_bounded(_optional_int(raw, "deadline_s"), 300, 1, 3600, "deadline_s"),
    )


def _bad(message: str, family: str | None = None, session: str | None = None) -> ApiError:
    return ApiError(ErrorCode.BAD_REQUEST, message, family=family, session=session)


def _require_text(
    raw: dict[str, Any],
    name: str,
    family: str | None = None,
    session: str | None = None,
) -> str:
    value = raw.get(name)

    if not isinstance(value, str) or not value:
        raise _bad(f"{name} is missing or not a string", family=family, session=session)

    return _utf8_text(value)


def _optional_text(raw: dict[str, Any], name: str) -> str | None:
    value = raw.get(name)
    return _utf8_text(value) if isinstance(value, str) and value else None


def _stored_text(raw: dict[str, Any], name: str) -> str | None:
    """A text that reaches a file and the event stream only.

    Two texts are read here: the reason of a switch and the key of a
    dispatch. Each file holds the JSON escape of one half of a surrogate
    pair, so nothing raises on such a text. The service took it before `_utf8_text`
    came, and it still does. A refusal here is a decision of the owner.
    """
    value = raw.get(name)
    return value if isinstance(value, str) and value else None


def _utf8_text(text: str) -> str:
    """The text, or `bad_request` for a text that has no UTF-8 form.

    The JSON reader makes a text with one half of a surrogate pair from an
    escape such as `\\ud800`. No answer, no line of the link and no byte
    count can hold that text, so each of them raises on it. The refusal
    names no family and no session: either one can be the text.

    CONTRACT-QUESTION: contract 02 §3 rule 3 says that a body is JSON and
    does not say what a reader does with such an escape. This reading
    refuses the body for each text that an answer, a line of the link or
    a byte count can hold. It takes such an escape in the two texts of
    `_stored_text`, and in a member that no parser reads. The other reading
    replaces the character, which changes a text that a door sent. A
    refusal of the two texts too costs two call sites.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ApiError(ErrorCode.BAD_REQUEST, NOT_JSON) from error

    return text


def _optional_int(raw: dict[str, Any], name: str) -> int | None:
    value = raw.get(name)

    if isinstance(value, bool) or not isinstance(value, int):
        return None

    return value


def _check_bytes(text: str, cap: int, name: str, family: str, session: str) -> None:
    """Caps are byte counts, not character counts (contract 03 §8)."""
    if len(text.encode("utf-8")) <= cap:
        return

    raise ApiError(
        ErrorCode.PAYLOAD_TOO_LARGE,
        f"{name} is over its {cap} byte limit",
        family=family,
        session=session,
    )


def _chain(value: object, kind: TriggerKind, family: str, session: str) -> tuple[str, ...]:
    """§13.2 rule 6. Only a dispatch carries one, and it is bounded.

    It reaches a durable record and it says who reached this job, so a
    malformed entry is refused rather than dropped.
    """
    if value is None:
        return ()

    entries = as_array(value)

    if entries is None or kind is not TriggerKind.DISPATCH:
        raise _bad("only a dispatch trigger carries a chain", family=family, session=session)

    if len(entries) > CHAIN_MAX_FAMILIES:
        raise _bad(
            f"trigger chain holds more than {CHAIN_MAX_FAMILIES} families",
            family=family,
            session=session,
        )

    for one in entries:
        if not isinstance(one, str) or not is_family(one):
            raise _bad("trigger chain holds a name that is not a family", family=family)

    return tuple(str(one) for one in entries)


def _bounded(value: int | None, fallback: int, low: int, high: int, name: str) -> int:
    if value is None:
        return fallback

    if not low <= value <= high:
        raise _bad(f"{name} is outside {low} to {high}")

    return value
