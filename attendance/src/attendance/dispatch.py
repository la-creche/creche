"""The dispatch ledger and the job view (contract 02 §13.4).

One family enqueues a job in another through the PEP's `enqueue` verb
(contract 04 §4.1). The job runs as an ordinary autonomous session, so it is
deleted when it ends (§13 rule 7) and its outcome record is filed under the
TARGET family by an id nobody else knows.

```
  POST /dispatch --> entry written --> auto-<ulid> job --> one ACP prompt
                         |                    |                    |
                         |      acp_session <-' session/new        | ends
  POST /dispatch/jobs <--'  live state from the job queue          |
                         `-----------------------------------> outcome_id
```

The facade mints the conversation id (ACP `session/new`). The job keeps
its `auto-<ulid>` id, and the entry's `acp_session` maps one to the other.

The ledger is what closes that loop. Without it, "what became of the job I
started" means opening every outcome record of every family and reading its
trigger. With it, the read is one directory listing plus one file per entry,
and a caller can only ever list its own directory.

Nothing here decides anything. The service owns sessions and turns, and this
module owns one small durable record and the shapes around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from .atomic import MODE_GROUP_READ, as_array, read_json, write_json
from .clock import now, parse_rfc3339, rfc3339
from .ids import is_family, is_session, is_ulid
from .models import CHAIN_MAX_FAMILIES, Trigger, TriggerKind
from .paths import dispatch_dir, dispatch_file, trigger_dir, trigger_file

# Contract 02 §13.4.2. Newest first, and a caller that names no limit gets a
# standup-sized page rather than every job it ever started.
JOBS_LIMIT_DEFAULT = 20
JOBS_LIMIT_MAX = 200

# Contract 04 §4.1: `idempotency_key` deduplicates a retry for one hour.
IDEMPOTENCY_WINDOW = timedelta(hours=1)


class LedgerDoor(StrEnum):
    """The door that started the jobs of a ledger. Each door has a ledger."""

    DISPATCH = "chaperone-dispatch"
    TRIGGER = "door-trigger"


class JobStatus(StrEnum):
    """Contract 02 §13.4.2's `status`, one value per meaning.

    A job over ACP is `queued` or `running` while it lives. `DISPATCHED` and
    `WAITING_APPROVAL` stay in the enum for the contract: no job over ACP
    reads either, because a gate no longer holds a turn (phase 2 plan,
    correction 9). `ENDED` means the job is over.
    """

    DISPATCHED = "dispatched"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting-approval"
    ENDED = "ended"


@dataclass(slots=True)
class DispatchEntry:
    """One row of contract 02 §13.4.3, read or written.

    It outlives its session on purpose: `outcome_id` is filled in when the
    job ends, and the session is gone a moment later.
    """

    session: str
    #: The key of the ledger: the family that enqueued, or the family that a
    #: trigger fired in.
    caller_family: str
    family: str
    enqueued_at: datetime
    #: The PEP's delegation id. None for a trigger.
    delegation_id: str | None = None
    chain: tuple[str, ...] = ()
    caller_session: str | None = None
    idempotency_key: str | None = None
    outcome_id: str | None = None
    #: The conversation id the facade minted for this job, once it has one.
    acp_session: str | None = None
    #: What fired the job: another family's `enqueue`, or a timer or a
    #: webhook of `door-trigger`.
    kind: TriggerKind = TriggerKind.DISPATCH
    #: The name of a webhook trigger. None for each other kind.
    trigger_name: str | None = None

    def to_file(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "caller_family": self.caller_family,
            "family": self.family,
            "enqueued_at": rfc3339(self.enqueued_at),
            "delegation_id": self.delegation_id,
            "chain": list(self.chain),
            "caller_session": self.caller_session,
            "idempotency_key": self.idempotency_key,
            "outcome_id": self.outcome_id,
            "acp_session": self.acp_session,
            "kind": self.kind.value,
            "trigger_name": self.trigger_name,
        }

    def trigger(self) -> Trigger:
        """Contract 02 §13.2's object for this job.

        For a dispatch, `name` is the CALLING family, from the entry this
        service wrote after checking the token. A caller never names itself
        (§13.2 rule 5). For a webhook, `name` is the trigger's name.
        """
        dispatched = self.kind is TriggerKind.DISPATCH
        return Trigger(
            kind=self.kind,
            name=self.caller_family if dispatched else self.trigger_name,
            fired_at=self.enqueued_at,
            chain=self.chain,
        )


@dataclass(slots=True, frozen=True)
class DispatchJob:
    """One job that waits for a slot, or runs. The message is data."""

    entry: DispatchEntry
    message: str


def status_of(live: JobStatus | None, outcome_id: str | None) -> JobStatus:
    """Contract 02 §13.4.2 rules 3 and 4, in one place.

    The outcome wins over the live state: the record is written before the
    job leaves the queue, so for one moment both exist, and the job is over.
    """
    if outcome_id is not None or live is None:
        # No live job and no outcome: a restart ended it. The job is over in
        # every way that matters, and nothing can say how it went.
        return JobStatus.ENDED

    return live


def job_view(
    entry: DispatchEntry, live: JobStatus | None, outcome: dict[str, Any] | None
) -> dict[str, Any]:
    """One entry of §13.4.2's `jobs` list.

    The outcome record is copied whole (contract 04 §4.1 rule 3). Summarizing
    it here would be a second definition of §13.1, and the two would drift.
    """
    return {
        "session": entry.session,
        "family": entry.family,
        "enqueued_at": rfc3339(entry.enqueued_at),
        "status": status_of(live, entry.outcome_id).value,
        "outcome": outcome,
    }


class DispatchLedger:
    """Every dispatch this service started, by caller family.

    One object, one state root. The service holds it and nothing else writes
    the directory.
    """

    def __init__(self, state_root: Path, door: LedgerDoor = LedgerDoor.DISPATCH) -> None:
        self._state_root = state_root
        self._door = door

    def write(self, entry: DispatchEntry) -> None:
        """Save one entry. Temp file and rename, like every durable record."""
        write_json(
            self._file(entry.caller_family, entry.session),
            entry.to_file(),
            MODE_GROUP_READ,
        )

    def read(self, caller_family: str, session: str) -> DispatchEntry | None:
        """One entry, or None. A caller reaches its own directory only."""
        if not is_family(caller_family) or not is_session(session):
            return None

        raw = read_json(self._file(caller_family, session))

        if raw is None:
            return None

        return _entry_from_file(raw, caller_family)

    def list(
        self, caller_family: str, since: datetime | None = None, limit: int = JOBS_LIMIT_DEFAULT
    ) -> list[DispatchEntry]:
        """This family's entries, newest first (contract 02 §13.4.2).

        The file name is the session id, whose ULID half sorts by time, so the
        listing is ordered before a single file is opened. `limit` then bounds
        how many are read.
        """
        found: list[DispatchEntry] = []

        for path in self._paths(caller_family):
            raw = read_json(path)

            if raw is None:
                continue

            entry = _entry_from_file(raw, caller_family)

            if entry is None or (since is not None and entry.enqueued_at < since):
                continue

            found.append(entry)

            if len(found) >= limit:
                break

        return found

    def note_outcome(self, caller_family: str, session: str, outcome_id: str) -> None:
        """Record which outcome belongs to this job (§13.4.3).

        Called as the record is written and before the session is deleted. An
        entry that has gone is not recreated: the ledger is a caller's index,
        not a second copy of the outcome.
        """
        entry = self.read(caller_family, session)

        if entry is None:
            return

        entry.outcome_id = outcome_id
        self.write(entry)

    def note_acp_session(self, caller_family: str, session: str, acp_session: str) -> None:
        """Map the job to the conversation the facade minted for it."""
        entry = self.read(caller_family, session)

        if entry is None:
            return

        entry.acp_session = acp_session
        self.write(entry)

    def remove(self, entry: DispatchEntry) -> None:
        """Remove an entry whose job never started (§13.4.1 rule 5)."""
        self._file(entry.caller_family, entry.session).unlink(missing_ok=True)

    def find_repeat(self, caller_family: str, key: str) -> DispatchEntry | None:
        """The entry an in-window `idempotency_key` already created (§13.4.4).

        The window is an hour, so the scan stops at the first entry older than
        that: the listing is newest first.
        """
        floor = now() - IDEMPOTENCY_WINDOW

        for path in self._paths(caller_family):
            raw = read_json(path)
            entry = _entry_from_file(raw, caller_family) if raw is not None else None

            if entry is None:
                continue

            if entry.enqueued_at < floor:
                return None

            if entry.idempotency_key == key:
                return entry

        return None

    def _paths(self, caller_family: str) -> list[Path]:
        """This family's entry files, newest first by session id."""
        if not is_family(caller_family):
            return []

        base = self._dir(caller_family)

        if not base.is_dir():
            return []

        return sorted((one for one in base.iterdir() if one.is_file()), reverse=True)

    def _dir(self, caller_family: str) -> Path:
        if self._door is LedgerDoor.TRIGGER:
            return trigger_dir(self._state_root, caller_family)

        return dispatch_dir(self._state_root, caller_family)

    def _file(self, caller_family: str, session: str) -> Path:
        if self._door is LedgerDoor.TRIGGER:
            return trigger_file(self._state_root, caller_family, session)

        return dispatch_file(self._state_root, caller_family, session)


def _entry_from_file(raw: dict[str, Any], caller_family: str) -> DispatchEntry | None:
    """Read one entry defensively. This service wrote it, and a file on disk
    is still a file another process could have touched (invariant 12)."""
    session = _text(raw.get("session"))
    family = _text(raw.get("family"))
    delegation_id = _text(raw.get("delegation_id"))
    stamped = _text(raw.get("enqueued_at"))
    enqueued_at = parse_rfc3339(stamped) if stamped is not None else None

    if session is None or family is None or enqueued_at is None:
        return None

    if not is_session(session) or not is_family(family):
        return None

    kind = _kind(raw.get("kind"))

    if kind is None:
        return None

    outcome_id = _text(raw.get("outcome_id"))

    return DispatchEntry(
        session=session,
        caller_family=caller_family,
        family=family,
        enqueued_at=enqueued_at,
        delegation_id=delegation_id,
        chain=_chain(raw.get("chain")),
        caller_session=_text(raw.get("caller_session")),
        idempotency_key=_text(raw.get("idempotency_key")),
        outcome_id=outcome_id if outcome_id is not None and is_ulid(outcome_id) else None,
        acp_session=_text(raw.get("acp_session")),
        kind=kind,
        trigger_name=_text(raw.get("trigger_name")),
    )


def _kind(value: object) -> TriggerKind | None:
    """An entry with no kind is a dispatch. An unknown kind is no entry."""
    if value is None:
        return TriggerKind.DISPATCH

    known = {kind.value: kind for kind in TriggerKind}
    return known.get(value) if isinstance(value, str) else None


def _chain(value: object) -> tuple[str, ...]:
    entries = as_array(value)

    if entries is None:
        return ()

    names = [one for one in entries if isinstance(one, str) and is_family(one)]
    return tuple(names[:CHAIN_MAX_FAMILIES])


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
