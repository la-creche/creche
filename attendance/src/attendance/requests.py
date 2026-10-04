"""Request bodies, validated before anything uses them (contract 02 §5).

A door is another process, so every field here is untrusted input: shape and
size are checked before use (invariants 12 and 14). Each refusal carries a
code from contract 02 §14, never a Python traceback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .atomic import as_array, as_object
from .clock import parse_rfc3339
from .dispatch import JOBS_LIMIT_DEFAULT, JOBS_LIMIT_MAX
from .errors import ApiError, ErrorCode
from .ids import (
    ATTACHMENT_NAME_MAX,
    LABEL_KEYS_MAX,
    LABEL_VALUE_MAX,
    TITLE_MAX,
    is_attachment,
    is_family,
    is_session,
    is_ulid,
)
from .jobs import MESSAGE_MAX_BYTES, Delegation
from .leases import Intent
from .models import (
    CHAIN_MAX_FAMILIES,
    DEFAULT_DEADLINE_S,
    Holder,
    OwuiRefs,
    Trigger,
    TriggerKind,
)
from .states import SessionKind, SessionState
from .wire import MAX_PROMPT_BYTES

# Contract 02 §13.2 rule 2. The trigger door sends these three as labels,
# so they are read.
LABEL_TRIGGER_KIND = "trigger_kind"
LABEL_TRIGGER_NAME = "trigger_name"
LABEL_TRIGGER_FIRED_AT = "trigger_fired_at"

IDEMPOTENCY_KEY_MAX = 200
# Contract 04 §4.1's own cap on `enqueue.idempotency_key`. Narrower than the
# turn key above, because that is the number the verb's schema publishes.
DISPATCH_KEY_MAX = 128
ATTACHMENTS_MAX = 20
STEER_MESSAGE_MAX = 4_096
REASON_MAX = 200
DEADLINE_MIN_S = 1

# The message for a body that is not JSON. A text with no UTF-8 form gets
# the same message: such a body is not JSON in UTF-8.
NOT_JSON = "body is not JSON"

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 200
TURNS_DEFAULT = 10
TURNS_MAX = 100


class Wait(StrEnum):
    """How a caller wants `run turn` to answer (contract 02 §5.4)."""

    STREAM = "stream"
    SETTLED = "settled"
    ACCEPTED = "accepted"


class SwitchMode(StrEnum):
    """Contract 05 §5.1."""

    DRAIN = "drain"
    INTERRUPT = "interrupt"


@dataclass(slots=True)
class CreateRequest:
    """Contract 02 §5.1."""

    family: str
    session: str
    title: str = ""
    labels: dict[str, str] = field(default_factory=dict[str, str])
    owner_session: str | None = None


@dataclass(slots=True)
class RunTurnRequest:
    """Contract 02 §5.4."""

    prompt: str
    idempotency_key: str | None = None
    wait: Wait = Wait.STREAM
    persona_text: str = ""
    attachments: list[str] = field(default_factory=list[str])
    owui: OwuiRefs | None = None
    trigger: Trigger | None = None
    deadline_s: int = DEFAULT_DEADLINE_S
    labels: dict[str, str] = field(default_factory=dict[str, str])
    # The delegate door's own field. No §5.4 body carries one: the PEP mints
    # the id (contract 04 §7.4) and `read_delegate` is what fills this in.
    delegation: Delegation | None = None


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


@dataclass(slots=True)
class JobQuery:
    """Contract 02 §13.4.2's body. The scope is `caller_family` and nothing
    a caller adds can widen it."""

    caller_family: str
    session: str | None = None
    since: datetime | None = None
    limit: int = JOBS_LIMIT_DEFAULT


@dataclass(slots=True)
class WriterRequest:
    """Contract 02 §5.9."""

    holder: Holder | None = None
    force: bool = False
    intent: Intent = Intent.ACQUIRE


@dataclass(slots=True)
class ListQuery:
    """Contract 02 §5.2."""

    family: str | None = None
    state: SessionState | None = None
    kind: SessionKind | None = None
    limit: int = LIST_LIMIT_DEFAULT
    cursor: str | None = None


@dataclass(slots=True)
class SwitchRequest:
    """Contract 05 §5.1's request body."""

    family: str
    to: str
    mode: SwitchMode
    reason: str
    outgoing: str | None = None
    deadline_s: int = 300


def read_create(raw: dict[str, Any]) -> CreateRequest:
    family = _require_text(raw, "family")
    session = _require_text(raw, "session")

    if not is_family(family):
        raise _bad("family is not a family name", family=family)

    if not is_session(session):
        raise _bad("session is not a session id", family=family, session=session)

    owner = _optional_text(raw, "owner_session")

    if owner is not None and not is_session(owner):
        raise _bad("owner_session is not a session id", family=family, session=session)

    return CreateRequest(
        family=family,
        session=session,
        title=_title(raw, family, session),
        labels=_labels(raw, family, session),
        owner_session=owner,
    )


def read_run_turn(raw: dict[str, Any], family: str, session: str) -> RunTurnRequest:
    prompt = _require_text(raw, "prompt", family, session)
    _check_bytes(prompt, MAX_PROMPT_BYTES, "prompt", family, session)
    labels = _labels(raw, family, session)

    return RunTurnRequest(
        prompt=prompt,
        idempotency_key=_idempotency_key(raw, family, session),
        wait=_wait(raw, family, session),
        persona_text=_persona(raw, family, session),
        attachments=_attachments(raw, family, session),
        owui=_owui(raw, family, session),
        trigger=_trigger(raw, labels, family, session),
        deadline_s=_deadline(raw, family, session),
        labels=labels,
    )


def read_delegate(raw: dict[str, Any]) -> DelegateRequest:
    """Contract 04 §7.3. The PEP is another process, so this is untrusted.

    `claimed_session_id` is advisory for the audit, and for `code-sandbox` it
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


def read_jobs(raw: dict[str, Any]) -> JobQuery:
    """Contract 02 §13.4.2. The caller family decides the scope, and it is
    the one field this service checks against a token."""
    caller = _require_text(raw, "caller_family")

    if not is_family(caller):
        raise _bad("caller_family is not a family name", family=caller)

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
    key = _optional_text(raw, "idempotency_key")

    if key is None:
        return None

    if len(key) > DISPATCH_KEY_MAX:
        raise _bad(f"idempotency_key is over {DISPATCH_KEY_MAX} characters", family=family)

    return key


def read_writer(raw: dict[str, Any], family: str, session: str) -> WriterRequest:
    holder_text = _optional_text(raw, "holder")
    holder: Holder | None = None

    if holder_text is not None:
        try:
            holder = Holder(holder_text)
        except ValueError as error:
            raise _bad("holder is not a door", family=family, session=session) from error

    return WriterRequest(
        holder=holder,
        force=raw.get("force") is True,
        intent=_intent(raw, family, session),
    )


def _intent(raw: dict[str, Any], family: str, session: str) -> Intent:
    """Contract 02 §7.4. Absent means `acquire`, which is the wider power.

    A door that asks for nothing gets the behaviour every draft before 6
    had, so an older door keeps working against a newer service.
    """
    text = _optional_text(raw, "intent")

    if text is None:
        return Intent.ACQUIRE

    try:
        return Intent(text)
    except ValueError as error:
        raise _bad("intent is not acquire or renew", family=family, session=session) from error


def read_steer(raw: dict[str, Any], family: str, session: str) -> str:
    message = _require_text(raw, "message", family, session)
    _check_bytes(message, STEER_MESSAGE_MAX, "message", family, session)
    return message


def read_stop_reason(raw: dict[str, Any], family: str, session: str) -> str:
    reason = _optional_text(raw, "reason") or "user_stopped"

    if len(reason) > REASON_MAX:
        raise _bad("reason is over its limit", family=family, session=session)

    return reason


def read_list_query(
    family: str | None,
    state: str | None,
    kind: str | None,
    limit: int | None,
    cursor: str | None,
) -> ListQuery:
    if family is not None and not is_family(family):
        raise _bad("family is not a family name", family=family)

    return ListQuery(
        family=family,
        state=_enum_or_none(state, SessionState, "state"),
        kind=_enum_or_none(kind, SessionKind, "kind"),
        limit=_bounded(limit, LIST_LIMIT_DEFAULT, 1, LIST_LIMIT_MAX, "limit"),
        cursor=cursor,
    )


def read_turns_wanted(turns: int | None) -> int:
    return _bounded(turns, TURNS_DEFAULT, 0, TURNS_MAX, "turns")


def read_from_seq(from_seq: int | None) -> int:
    if from_seq is None:
        return 0

    if from_seq < 0:
        raise _bad("from_seq is negative")

    return from_seq


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
        reason=_optional_text(raw, "reason") or "",
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


def _utf8_text(text: str) -> str:
    """The text, or `bad_request` for a text that has no UTF-8 form.

    The JSON reader makes a text with one half of a surrogate pair from an
    escape such as `\\ud800`. No answer, no line of the channel and no byte
    count can hold that text, so each of them raises on it. The refusal
    names no family and no session: either one can be the text.

    CONTRACT-QUESTION: contract 02 §3 rule 3 says that a body is JSON and
    does not say what a reader does with such an escape. This reading
    refuses the body for each text that a parser reads, and takes such an
    escape in a member that no parser reads. The other reading replaces the
    character, which changes a text that a door sent.
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


def _title(raw: dict[str, Any], family: str, session: str) -> str:
    title = _optional_text(raw, "title") or ""

    if len(title) > TITLE_MAX:
        raise _bad("title is over its limit", family=family, session=session)

    return title


def _labels(raw: dict[str, Any], family: str, session: str) -> dict[str, str]:
    typed = as_object(raw.get("labels"))

    if typed is None:
        return {}

    if len(typed) > LABEL_KEYS_MAX:
        raise _bad(f"labels carries over {LABEL_KEYS_MAX} keys", family=family, session=session)

    found: dict[str, str] = {}

    for key, value in typed.items():
        _utf8_text(key)

        if not isinstance(value, str) or len(value) > LABEL_VALUE_MAX:
            raise _bad(f"label {key} is not a short string", family=family, session=session)

        found[key] = _utf8_text(value)

    return found


def _idempotency_key(raw: dict[str, Any], family: str, session: str) -> str | None:
    key = _optional_text(raw, "idempotency_key")

    if key is None:
        return None

    if len(key) > IDEMPOTENCY_KEY_MAX:
        raise _bad("idempotency_key is over its limit", family=family, session=session)

    return key


def _wait(raw: dict[str, Any], family: str, session: str) -> Wait:
    text = _optional_text(raw, "wait")

    if text is None:
        return Wait.STREAM

    try:
        return Wait(text)
    except ValueError as error:
        raise _bad("wait is not a known mode", family=family, session=session) from error


def _persona(raw: dict[str, Any], family: str, session: str) -> str:
    """Over the cap the service truncates rather than refuses (§11 rule 6).

    A refusal here would make a folder prompt able to break a chat, and a
    folder may only shape behaviour (invariant 7).
    """
    text = raw.get("persona_text")

    if text is None:
        return ""

    if not isinstance(text, str):
        raise _bad("persona_text is not a string", family=family, session=session)

    return _utf8_text(text)


def _attachments(raw: dict[str, Any], family: str, session: str) -> list[str]:
    entries = as_array(raw.get("attachments"))

    if entries is None:
        return []

    if len(entries) > ATTACHMENTS_MAX:
        raise ApiError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"attachments carries over {ATTACHMENTS_MAX} items",
            family=family,
            session=session,
        )

    names: list[str] = []

    for entry in entries:
        # A name becomes a path inside the sandbox, so `.`, `..` and every
        # separator are refused here (contract 02 §5.4.1).
        if not isinstance(entry, str) or not is_attachment(entry):
            raise _bad(
                f"an attachment name is not {ATTACHMENT_NAME_MAX} safe characters",
                family=family,
                session=session,
            )

        names.append(entry)

    return names


def _owui(raw: dict[str, Any], family: str, session: str) -> OwuiRefs | None:
    typed = as_object(raw.get("owui"))

    if typed is None:
        return None

    chat_id = _optional_text(typed, "chat_id")
    message_id = _optional_text(typed, "message_id")

    # An absent chat id arrives present but empty, not omitted (§10).
    if chat_id is None or message_id is None:
        raise _bad("owui needs a chat_id and a message_id", family=family, session=session)

    return OwuiRefs(
        chat_id=chat_id,
        message_id=message_id,
        user_message_id=_optional_text(typed, "user_message_id"),
        parent_id=_optional_text(typed, "parent_id"),
    )


def _trigger(
    raw: dict[str, Any], labels: dict[str, str], family: str, session: str
) -> Trigger | None:
    """Contract 02 §13.2. The object wins, the three labels still work.

    The trigger door sends `trigger_kind`, `trigger_name` and
    `trigger_fired_at` as labels, and §13.2 rule 2 keeps reading them.
    """
    typed = as_object(raw.get("trigger"))

    if typed is None:
        return _trigger_from_labels(labels, family, session)

    kind = _trigger_kind(_optional_text(typed, "kind"), family, session)

    return Trigger(
        kind=kind,
        name=_optional_text(typed, "name"),
        fired_at=_fired_at(_optional_text(typed, "fired_at"), family, session),
        chain=_chain(typed.get("chain"), kind, family, session),
    )


def _trigger_from_labels(labels: dict[str, str], family: str, session: str) -> Trigger | None:
    kind = labels.get(LABEL_TRIGGER_KIND)

    if kind is None:
        return None

    return Trigger(
        kind=_trigger_kind(kind, family, session),
        name=labels.get(LABEL_TRIGGER_NAME),
        fired_at=_fired_at(labels.get(LABEL_TRIGGER_FIRED_AT), family, session),
    )


def _trigger_kind(text: str | None, family: str, session: str) -> TriggerKind:
    """§13.2 rule 4. It reaches a durable record, so it is validated."""
    if text is None:
        raise _bad("trigger needs a kind", family=family, session=session)

    try:
        return TriggerKind(text)
    except ValueError as error:
        raise _bad(
            "trigger kind is not timer, webhook or dispatch", family=family, session=session
        ) from error


def _fired_at(text: str | None, family: str, session: str) -> datetime | None:
    """None means the turn's own start time (§13.2's field table)."""
    if text is None:
        return None

    moment = parse_rfc3339(text)

    if moment is None:
        raise _bad("trigger fired_at is not RFC 3339", family=family, session=session)

    return moment


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


def _deadline(raw: dict[str, Any], family: str, session: str) -> int:
    value = _optional_int(raw, "deadline_s")

    if value is None:
        return DEFAULT_DEADLINE_S

    if not DEADLINE_MIN_S <= value <= DEFAULT_DEADLINE_S:
        raise _bad(
            f"deadline_s is outside {DEADLINE_MIN_S} to {DEFAULT_DEADLINE_S}",
            family=family,
            session=session,
        )

    return value


def _bounded(value: int | None, fallback: int, low: int, high: int, name: str) -> int:
    if value is None:
        return fallback

    if not low <= value <= high:
        raise _bad(f"{name} is outside {low} to {high}")

    return value


def _enum_or_none[EnumT: StrEnum](value: str | None, kind: type[EnumT], name: str) -> EnumT | None:
    if value is None:
        return None

    try:
        return kind(value)
    except ValueError as error:
        raise _bad(f"{name} is not a known value") from error
