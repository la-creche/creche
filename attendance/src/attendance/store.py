"""The session store on the host (contract 02 §9).

A session always exists once created. It survives a restart of this service, a
sandbox replacement and a deploy (invariant 1). That is why every durable fact
lives in a file here and nothing important lives only in memory.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from .atomic import MODE_PUBLIC_READ, as_array, as_object, read_json, write_json
from .clock import now, parse_rfc3339
from .errors import TurnReason
from .ids import is_family, is_session, is_ulid
from .journal import Journal
from .models import (
    DEFAULT_DEADLINE_S,
    GateTally,
    OwuiRefs,
    Session,
    Trigger,
    TriggerKind,
    Turn,
    Usage,
)
from .paths import (
    INBOX_DIR,
    OUTBOX_DIR,
    PI_DIR,
    SCRATCH_DIR,
    TURNS_DIR,
    family_sessions_dir,
    session_dir,
    session_file,
    turn_file,
    turns_dir,
)
from .states import SessionKind, TurnState

_SESSION_SUBDIRS = (TURNS_DIR, PI_DIR, INBOX_DIR, OUTBOX_DIR)

# Contract 02 §9: `scratch/` is thin jobs only, and it dies with the session.
_THIN_SUBDIRS = (*_SESSION_SUBDIRS, SCRATCH_DIR)


class SessionStore:
    """Reads and writes the on-disk shape of contract 02 §9."""

    def __init__(self, sessions_root: Path, journal: Journal | None = None) -> None:
        self._root = sessions_root
        self.journal = journal if journal is not None else Journal(sessions_root)

    @property
    def root(self) -> Path:
        return self._root

    def exists(self, family: str, session: str) -> bool:
        return session_file(self._root, family, session).is_file()

    def create(self, record: Session) -> None:
        """Lay out the session directory, then publish its record."""
        base = session_dir(self._root, record.family, record.session)

        for name in _subdirs_for(record.kind):
            (base / name).mkdir(parents=True, exist_ok=True)

        self.journal.register(record.family, record.session, record.journal_seq)
        self.save(record)

    def save(self, record: Session) -> None:
        write_json(
            session_file(self._root, record.family, record.session),
            record.to_file(),
            MODE_PUBLIC_READ,
        )

    def load(self, family: str, session: str) -> Session | None:
        raw = read_json(session_file(self._root, family, session))

        if raw is None:
            return None

        return _session_from_file(raw, family, session)

    def save_turn(self, record: Turn) -> None:
        write_json(
            turn_file(self._root, record.family, record.session, record.turn),
            record.to_file(),
            MODE_PUBLIC_READ,
        )

    def load_turns(self, family: str, session: str) -> list[Turn]:
        """Every turn of one session, oldest first. Turn ids are ULIDs."""
        directory = turns_dir(self._root, family, session)

        if not directory.is_dir():
            return []

        loaded: list[Turn] = []

        for path in sorted(directory.glob("*.json")):
            if not is_ulid(path.stem):
                continue

            raw = read_json(path)

            if raw is None:
                continue

            turn = _turn_from_file(raw, family, session)

            if turn is not None:
                loaded.append(turn)

        return loaded

    def families(self) -> list[str]:
        """Families that have a session directory on this host."""
        if not self._root.is_dir():
            return []

        return sorted(entry.name for entry in self._root.iterdir() if _is_family_dir(entry))

    def session_ids(self, family: str) -> list[str]:
        base = family_sessions_dir(self._root, family)

        if not base.is_dir():
            return []

        names: list[str] = []

        for entry in base.iterdir():
            if not entry.is_dir() or not is_session(entry.name):
                continue

            if (entry / "session.json").is_file():
                names.append(entry.name)

        return sorted(names)

    def scan(self) -> Iterator[Session]:
        """Every session on disk. The restart path reads this once."""
        for family in self.families():
            for session in self.session_ids(family):
                record = self.load(family, session)

                if record is not None:
                    yield record

    def delete(self, family: str, session: str) -> bool:
        """Remove the whole session directory (contract 02 §5.8)."""
        self.journal.close(family, session)
        base = session_dir(self._root, family, session)

        if not base.is_dir():
            return False

        shutil.rmtree(base, ignore_errors=True)
        return True


def _subdirs_for(kind: SessionKind) -> tuple[str, ...]:
    """Contract 02 §9's layout. Only a thin job owns a scratch directory."""
    if kind is SessionKind.THIN:
        return _THIN_SUBDIRS

    return _SESSION_SUBDIRS


def _is_family_dir(entry: Path) -> bool:
    return entry.is_dir() and is_family(entry.name)


def _as_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _as_int(raw: dict[str, Any], key: str, fallback: int) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _as_float(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0

    # A record from an older service can hold NaN, and no answer can hold
    # it. An integer past the range of a float has no float either.
    try:
        number = float(value)
    except OverflowError:
        return 0.0

    return number if math.isfinite(number) else 0.0


def _as_time(raw: dict[str, Any], key: str) -> datetime:
    text = _as_str(raw, key)
    parsed = parse_rfc3339(text) if text is not None else None
    return parsed if parsed is not None else now()


def _as_str_map(raw: dict[str, Any], key: str) -> dict[str, str]:
    typed = as_object(raw.get(key))

    if typed is None:
        return {}

    return {key_text: value for key_text, value in typed.items() if isinstance(value, str)}


def _session_from_file(raw: dict[str, Any], family: str, session: str) -> Session | None:
    kind_text = _as_str(raw, "kind")

    if kind_text is None:
        return None

    try:
        kind = SessionKind(kind_text)
    except ValueError:
        return None

    return Session(
        family=family,
        session=session,
        kind=kind,
        created_at=_as_time(raw, "created_at"),
        updated_at=_as_time(raw, "updated_at"),
        title=_as_str(raw, "title") or "",
        journal_seq=_as_int(raw, "journal_seq", 0),
        turns_total=_as_int(raw, "turns_total", 0),
        sandbox=_as_str(raw, "sandbox"),
        persona_hash=_as_str(raw, "persona_hash"),
        labels=_as_str_map(raw, "labels"),
        owui_map=_as_str_map(raw, "owui_map"),
        owner_session=_as_str(raw, "owner_session"),
        trigger=_trigger_from_file(raw),
        owui_chat=_as_str(raw, "owui_chat"),
        owui_leaf=_as_str(raw, "owui_leaf"),
        terminal_total=_as_int(raw, "terminal_total", 0),
        pi_cursor=_as_str(raw, "pi_cursor"),
    )


def _trigger_from_file(raw: dict[str, Any]) -> Trigger | None:
    """Contract 02 §13.2. A file this service wrote, still read defensively."""
    typed = as_object(raw.get("trigger"))

    if typed is None:
        return None

    kind_text = _as_str(typed, "kind")

    if kind_text is None:
        return None

    try:
        kind = TriggerKind(kind_text)
    except ValueError:
        return None

    fired_text = _as_str(typed, "fired_at")

    return Trigger(
        kind=kind,
        name=_as_str(typed, "name"),
        fired_at=parse_rfc3339(fired_text) if fired_text is not None else None,
        chain=_chain_from_file(typed),
    )


def _chain_from_file(typed: dict[str, Any]) -> tuple[str, ...]:
    """§13.2 rule 6's `chain`, absent on a timer and a webhook.

    The outcome record is built from the session, so a chain dropped here is
    a chain missing from the durable record a reader ends up with.
    """
    entries = as_array(typed.get("chain"))

    if entries is None:
        return ()

    return tuple(one for one in entries if isinstance(one, str))


def _usage_from_file(raw: dict[str, Any]) -> Usage:
    typed = as_object(raw.get("usage"))

    if typed is None:
        return Usage()

    return Usage(
        input=_as_int(typed, "input", 0),
        output=_as_int(typed, "output", 0),
        cache_read=_as_int(typed, "cache_read", 0),
        cache_write=_as_int(typed, "cache_write", 0),
        cost_usd=_as_float(typed, "cost_usd"),
    )


def _gates_from_file(raw: dict[str, Any]) -> GateTally:
    typed = as_object(raw.get("gates"))

    if typed is None:
        return GateTally()

    return GateTally(
        approved=_as_int(typed, "approved", 0),
        denied=_as_int(typed, "denied", 0),
        timed_out=_as_int(typed, "timed_out", 0),
    )


def _owui_from_file(raw: dict[str, Any]) -> OwuiRefs | None:
    typed = as_object(raw.get("owui"))

    if typed is None:
        return None

    chat_id = _as_str(typed, "chat_id")
    message_id = _as_str(typed, "message_id")

    if chat_id is None or message_id is None:
        return None

    return OwuiRefs(
        chat_id=chat_id,
        message_id=message_id,
        user_message_id=_as_str(typed, "user_message_id"),
        parent_id=_as_str(typed, "parent_id"),
    )


def _turn_from_file(raw: dict[str, Any], family: str, session: str) -> Turn | None:
    turn_id = _as_str(raw, "turn")
    state_text = _as_str(raw, "state")

    if turn_id is None or state_text is None:
        return None

    try:
        state = TurnState(state_text)
    except ValueError:
        return None

    reason_text = _as_str(raw, "reason")
    reason: TurnReason | None = None

    if reason_text is not None:
        try:
            reason = TurnReason(reason_text)
        except ValueError:
            reason = TurnReason.INTERNAL

    ended_text = _as_str(raw, "ended_at")
    truncated = raw.get("persona_truncated")

    return Turn(
        turn=turn_id,
        session=session,
        family=family,
        state=state,
        started_at=_as_time(raw, "started_at"),
        sandbox=_as_str(raw, "sandbox") or "",
        deadline_s=_as_int(raw, "deadline_s", DEFAULT_DEADLINE_S),
        reason=reason,
        ended_at=parse_rfc3339(ended_text) if ended_text is not None else None,
        idempotency_key=_as_str(raw, "idempotency_key"),
        usage=_usage_from_file(raw),
        approvals=_as_int(raw, "approvals", 0),
        gates=_gates_from_file(raw),
        owui=_owui_from_file(raw),
        persona_truncated=truncated is True,
        prompt_sha256=_as_str(raw, "prompt_sha256") or "",
    )
