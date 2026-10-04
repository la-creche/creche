"""Reading `caregiver`'s status document (contract 05 §2, §3, §4).

`attendance` never calls `caregiver`. Everything it needs about a family arrives
through this one file: the family's kind, whether it may serve turns, which
sandbox is ready, and the credential epoch it must put on the channel.

The document comes from another process, so every field is a claim until it
is validated (invariants 12 and 14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .atomic import as_array, as_object, read_json
from .clock import age_seconds, parse_rfc3339
from .errors import ApiError, ErrorCode
from .ids import is_family, is_sandbox
from .paths import families_dir, status_file
from .states import SessionKind

STALE_AFTER_S = 90.0
FIRST_EPOCH = 1

#: Contract 05 §3.3. `caregiver` raises it when the PEP's `/healthz` has been
#: silent for longer than the watch's threshold. `blocks_turns` is false, so
#: `check_may_serve` lets every family serve, and one caller looks for this
#: code by name: an autonomous firing, whose whole purpose is tool calls.
PEP_UNREACHABLE = "pep_unreachable"


class FamilyState(StrEnum):
    """Contract 05 §3's family states."""

    IN_SYNC = "in_sync"
    RECONCILING = "reconciling"
    INVALID = "invalid"
    DEGRADED = "degraded"


class SandboxState(StrEnum):
    """Contract 05 §4.2's lifecycle states."""

    PLANNED = "planned"
    CREATING = "creating"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    GONE = "gone"
    FAILED = "failed"


# A sandbox in either of these is a cold start in progress. Contract 02 §5.1
# says a cold start is not `sandbox_unavailable`: the service waits for it.
COLD_START_STATES: frozenset[SandboxState] = frozenset(
    {SandboxState.PLANNED, SandboxState.CREATING}
)

# The two states this service will dial. `ready` means the handshake passed,
# and `creating` is what running that handshake promotes (contract 05 §4.2).
# Nothing else serves a turn, so nothing else may be switched onto either.
STARTABLE_STATES: frozenset[SandboxState] = frozenset({SandboxState.READY, SandboxState.CREATING})


@dataclass(slots=True)
class SandboxInfo:
    """One row of the status document's `sandboxes` list."""

    id: str
    state: SandboxState
    # Contract 05 §4.1. The host path of this sandbox's `supervisor.env`,
    # which this service hands to `sbx exec --env-file`. Empty means the
    # document did not publish one, which is a fault and never a guess
    # (contract 03 §7.1): no fixed in-VM mount path exists to fall back to.
    playpen_env: str = ""

    @property
    def index(self) -> int:
        """The monotonic N of `<family>-s<N>`. Newer sandboxes sort higher."""
        _, _, suffix = self.id.rpartition("-s")
        return int(suffix) if suffix.isdigit() else 0


@dataclass(slots=True)
class FamilyStatus:
    """What this service takes from one status document."""

    family: str
    #: None when the document states no kind that this service knows. No
    #: default exists: `served_kind` says why.
    kind: SessionKind | None
    #: None when the document states no state that this service knows.
    state: FamilyState | None
    written_at: datetime | None
    config_rev: str = ""
    epoch: int = FIRST_EPOCH
    never_valid: bool = False
    blocking_fault: str | None = None
    #: Every open fault's code, blocking or not. `blocking_fault` answers
    #: "may this family serve at all"; this answers "what else is wrong",
    #: which a non-blocking fault like `pep_unreachable` needs.
    fault_codes: frozenset[str] = frozenset()
    sandboxes: list[SandboxInfo] = field(default_factory=list[SandboxInfo])
    max_running_turns: int | None = None
    job_timeout_s: int | None = None
    #: Contract 02 §13.4.1 rule 3. `caregiver` publishes `triggers.enqueue`
    #: when the family file carries contract 01 §3.13's dispatch form. A
    #: document without it reads `False`, and no family may be dispatched to:
    #: absence is denial (invariant 11).
    accepts_dispatch: bool = False

    @property
    def state_text(self) -> str | None:
        """The state as the detail of a refusal carries it.

        CONTRACT-QUESTION: contract 05 §3 names four states and does not say
        what a reader does with another value. This reader keeps no state for
        such a document, and the detail then holds null. The old reading,
        `in_sync`, reported a health that nobody published. No decision reads
        the state, so another reading changes one word of a detail.
        """
        return self.state.value if self.state is not None else None

    def has_fault(self, code: str) -> bool:
        """True when the document carries this fault, blocking or not."""
        return code in self.fault_codes

    def is_stale(self, stale_after_s: float = STALE_AFTER_S) -> bool:
        """True when `caregiver` has not written for longer than the limit."""
        if self.written_at is None:
            return True

        return age_seconds(self.written_at) > stale_after_s

    def ready_sandbox(self) -> SandboxInfo | None:
        """The sandbox that serves new turns: the newest one that is ready.

        During a switch the incoming sandbox is `ready` while the outgoing one
        drains, so the highest N is the one to dial (contract 05 §4.1, §5.3).
        """
        ready = [box for box in self.sandboxes if box.state is SandboxState.READY]

        if not ready:
            return None

        return max(ready, key=lambda box: box.index)

    def has_cold_start(self) -> bool:
        """True while a sandbox is being created (contract 02 §5.1)."""
        return any(box.state in COLD_START_STATES for box in self.sandboxes)

    def sandbox_by_id(self, sandbox: str) -> SandboxInfo | None:
        """One row by its id, or None when the document has no such row."""
        return next((box for box in self.sandboxes if box.id == sandbox), None)

    def startable_by_id(self, sandbox: str) -> SandboxInfo | None:
        """One row this service would dial, or None (contract 05 §4.2).

        A switch names its incoming sandbox and a pin remembers it, and both
        are only good while the document still offers that sandbox.
        """
        found = self.sandbox_by_id(sandbox)

        if found is None or found.state not in STARTABLE_STATES:
            return None

        return found

    def startable_sandbox(self) -> SandboxInfo | None:
        """The sandbox to dial: a ready one, else the newest `creating` one.

        Contract 05 §4.2 defines `ready` as "the channel handshake passed",
        and §4.3 puts "open the channel, run the handshake" (steps 6 and 7)
        after `sbx create` (step 1). This service is the only process that
        holds a channel, so it is the only one that can run that handshake.
        Waiting for someone else to write `ready` would wait forever.

        `planned` is excluded: §4.2 says nothing exists yet, so there is
        nothing to dial and the cold-start wait still covers it. A ready
        sandbox always wins, so a switch never moves a session onto a
        half-built replacement.
        """
        ready = self.ready_sandbox()

        if ready is not None:
            return ready

        creating = [box for box in self.sandboxes if box.state is SandboxState.CREATING]

        if not creating:
            return None

        return max(creating, key=lambda box: box.index)


class StatusReader:
    """Reads and validates one status document per family."""

    def __init__(self, state_root: Path, stale_after_s: float = STALE_AFTER_S) -> None:
        self._state_root = state_root
        self._stale_after_s = stale_after_s

    def read(self, family: str) -> FamilyStatus | None:
        """The family's status, or None when no document exists."""
        if not is_family(family):
            return None

        raw = read_json(status_file(self._state_root, family))

        if raw is None:
            return None

        return _status_from_file(raw, family)

    def families(self) -> list[str]:
        """Every family `caregiver` publishes. No index file exists to rot."""
        base = families_dir(self._state_root)

        if not base.is_dir():
            return []

        return sorted(entry.name for entry in base.iterdir() if _is_family_dir(entry))

    def require(self, family: str) -> FamilyStatus:
        """The family's status, or `family_unknown` (contract 02 §14)."""
        status = self.read(family)

        if status is None:
            raise ApiError(
                ErrorCode.FAMILY_UNKNOWN,
                f"no status document for family {family}",
                family=family,
            )

        return status

    @property
    def stale_after_s(self) -> float:
        return self._stale_after_s


def check_may_serve(status: FamilyStatus) -> None:
    """Contract 02 §5.1's table. Raises when the family cannot serve.

    A family whose newest file failed validation still serves against the last
    good definition, because a bad definition yields a report rather than an
    outage (invariant 19). Only a family that was never valid has nothing to
    serve.
    """
    if status.never_valid:
        raise _never_valid(status)

    if status.blocking_fault is not None:
        raise ApiError(
            ErrorCode.FAMILY_DEGRADED,
            f"family {status.family} has a fault that blocks turns",
            family=status.family,
            detail={"family_state": status.state_text, "fault": status.blocking_fault},
        )


def served_kind(status: FamilyStatus) -> SessionKind:
    """The kind that the document states. Raises when it states none.

    The kind decides which door reaches the family (contract 02 §3.1) and
    whether the family has a turn limit (contract 02 §13). A document that
    states no kind this service knows proves neither, so this fails closed.

    `caregiver` writes an empty kind on purpose for a family that never
    validated (contract 05 §3.1). No door can be the wrong door for that
    family, and contract 02 §14 gives it `family_invalid`.

    CONTRACT-QUESTION: contract 05 §2.1 names three kinds and does not say
    what a reader does with another value. For each other document with no
    known kind, this reader refuses each door with `forbidden`: a door
    reaches one kind, and no kind is proven. The three doors refuse the same
    field. The old reading, `attended`, gave an attended door a family of
    another kind and took an autonomous family out of its turn limit. A
    reading that serves such a document must name the kind that it serves.
    """
    if status.kind is not None:
        return status.kind

    if status.never_valid:
        raise _never_valid(status)

    raise ApiError(
        ErrorCode.FORBIDDEN,
        f"the status document of family {status.family} states no kind, so no door reaches it",
        family=status.family,
        detail={"kind": None},
    )


def _never_valid(status: FamilyStatus) -> ApiError:
    """Contract 02 §14's `family_invalid` (contract 05 §3.1)."""
    return ApiError(
        ErrorCode.FAMILY_INVALID,
        f"no revision of family {status.family} ever validated",
        family=status.family,
        detail={"family_state": status.state_text},
    )


def _is_family_dir(entry: Path) -> bool:
    return entry.is_dir() and is_family(entry.name)


def _status_from_file(raw: dict[str, Any], family: str) -> FamilyStatus:
    """Read the document defensively. An unreadable field takes its default.

    Two fields have no default: the kind and the state. Each one reads as
    None, and `served_kind` and `state_text` say what follows.
    """
    validation = as_object(raw.get("validation")) or {}
    credentials = as_object(raw.get("credentials")) or {}
    limits = as_object(raw.get("limits")) or {}
    triggers = as_object(raw.get("triggers")) or {}

    return FamilyStatus(
        family=family,
        kind=_enum(raw.get("kind"), SessionKind),
        state=_enum(raw.get("state"), FamilyState),
        written_at=_time(raw.get("written_at")),
        config_rev=_text(raw.get("config_rev")) or "",
        epoch=_positive_int(credentials.get("epoch"), FIRST_EPOCH),
        never_valid=validation.get("never_valid") is True,
        blocking_fault=_blocking_fault(raw.get("faults")),
        fault_codes=_fault_codes(raw.get("faults")),
        sandboxes=_sandboxes(raw.get("sandboxes")),
        max_running_turns=_optional_int(limits.get("max_running_turns")),
        job_timeout_s=_optional_int(limits.get("job_timeout_s")),
        # `is True`, not truthiness: a document that writes a string here says
        # nothing, and a guess would open a family nobody declared open.
        accepts_dispatch=triggers.get("enqueue") is True,
    )


def _blocking_fault(value: object) -> str | None:
    """The first fault whose `blocks_turns` is true, by code."""
    entries = as_array(value)

    if entries is None:
        return None

    for entry in entries:
        fault = as_object(entry)

        if fault is None or fault.get("blocks_turns") is not True:
            continue

        code = _text(fault.get("code"))

        if code is not None:
            return code

    return None


def _fault_codes(value: object) -> frozenset[str]:
    """Every fault code the document carries. An entry with no code is skipped."""
    entries = as_array(value)

    if entries is None:
        return frozenset()

    found: set[str] = set()

    for entry in entries:
        fault = as_object(entry)

        if fault is None:
            continue

        code = _text(fault.get("code"))

        if code is not None:
            found.add(code)

    return frozenset(found)


def _sandboxes(value: object) -> list[SandboxInfo]:
    entries = as_array(value)

    if entries is None:
        return []

    found: list[SandboxInfo] = []

    for entry in entries:
        box = as_object(entry)

        if box is None:
            continue

        box_id = _text(box.get("id"))
        state_text = _text(box.get("state"))

        if box_id is None or state_text is None or not is_sandbox(box_id):
            continue

        try:
            state = SandboxState(state_text)
        except ValueError:
            continue

        found.append(
            SandboxInfo(
                id=box_id,
                state=state,
                playpen_env=_text(box.get("supervisor_env")) or "",
            )
        )

    return found


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _time(value: object) -> datetime | None:
    return parse_rfc3339(value) if isinstance(value, str) else None


def _positive_int(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback

    return value


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None

    return value


def _enum[EnumT: StrEnum](value: object, kind: type[EnumT]) -> EnumT | None:
    if not isinstance(value, str):
        return None

    try:
        return kind(value)
    except ValueError:
        return None
