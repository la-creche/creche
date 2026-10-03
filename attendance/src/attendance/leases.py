"""One writer per session, many readers (contract 02 §7).

`attendance` enforces this because pi enforces nothing. Two concurrent pi
writers on one session file cross-contaminate context, orphan a branch and
both report success (contract 02 §7, measured). Invariant 3 makes Open WebUI
and the TUI two views of one session, so the refusal has to be explicit and
visible rather than a race nobody sees.

This service is one host process, so the in-process dictionary is the whole
lock. The mirror file exists only so a restarted service can report who held
a lease. It is never read to decide anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, StrEnum
from pathlib import Path

from .atomic import MODE_PUBLIC_READ, write_json
from .clock import now
from .errors import ApiError, ErrorCode
from .models import LEASE_TTL_S, Holder, WriterLease
from .paths import lease_file


class Takeover(Enum):
    """Whether the caller asked to take a lease it does not hold.

    Contract 02 §7.3 rule 6 is all this still decides: take an idle lease
    from another INSTANCE of the caller's own door. Another door's idle lease
    passes without it (rule 5), and no flag takes an active session (rule 4).
    """

    POLITE = "polite"
    FORCE = "force"


class TurnActivity(Enum):
    """Whether the session has a turn that is not finished.

    ACTIVE covers `queued` as well as `running` and `waiting-approval`
    (contract 02 §7.3). A queued turn has a prompt on disk and a slot coming,
    so handing its session to another door would strand it.
    """

    IDLE = "idle"
    ACTIVE = "active"


class Intent(Enum):
    """Whether the caller is taking a lease or keeping one (contract 02 §7.4).

    The two differ in exactly one way: an acquire may take an idle lease from
    another door, and a renew may not take anything at all. Read as an
    acquire, a renewal timer would take back a session the human had just
    moved to another door, and the two would trade it every few seconds.
    """

    ACQUIRE = "acquire"
    RENEW = "renew"


class LeaseReason(StrEnum):
    """What a `writer_changed` line says happened (contract 02 §7.3)."""

    GRANTED = "granted"
    RENEWED = "renewed"
    TAKEN_OVER = "taken_over"
    RELEASED = "released"


@dataclass(slots=True, frozen=True)
class Grant:
    """One decision: the lease the caller now holds, and how it got it."""

    lease: WriterLease
    reason: LeaseReason


class LeaseBook:
    """The writer leases of every session, plus their on-disk mirror."""

    def __init__(self, state_root: Path, ttl_s: int = LEASE_TTL_S) -> None:
        self._state_root = state_root
        self._ttl = timedelta(seconds=ttl_s)
        self._leases: dict[tuple[str, str], WriterLease] = {}

    def get(self, family: str, session: str) -> WriterLease | None:
        """The live lease, or None when none is held or it has expired."""
        lease = self._leases.get((family, session))

        if lease is None:
            return None

        if lease.is_expired(now()):
            return None

        return lease

    def last_holder(self, family: str, session: str) -> Holder | None:
        """Who held this session last, expired or not.

        `get` answers "who may write now", which is what §7.3's table reads.
        This answers "who was here", which contract 02 §10.5 needs: a
        terminal that was killed rather than closed leaves a lease that
        expires, and §7.3 rule 2 then GRANTS rather than taking over, so a
        reader of `get` alone never learns a terminal was ever here.
        """
        lease = self._leases.get((family, session))

        return lease.holder if lease is not None else None

    def take(
        self,
        family: str,
        session: str,
        holder: Holder,
        door_instance: str,
        takeover: Takeover = Takeover.POLITE,
        activity: TurnActivity = TurnActivity.IDLE,
        intent: Intent = Intent.ACQUIRE,
    ) -> Grant:
        """Contract 02 §7.3's table, read top to bottom. First match wins."""
        current = self.get(family, session)

        # Rules 1 and 2: nothing holds this session, or what held it expired.
        if current is None:
            if intent is Intent.RENEW:
                raise taken_over_error(family, session, None)

            return self._grant(family, session, holder, door_instance, LeaseReason.GRANTED)

        # Rule 3: the caller already holds it. This is the whole of a renew.
        if _same_writer(current, holder, door_instance):
            return self._grant(
                family, session, holder, door_instance, LeaseReason.RENEWED, current.turn
            )

        # §7.4 rule 2: a renew that reaches here lost the lease to someone.
        if intent is Intent.RENEW:
            raise taken_over_error(family, session, current)

        # Rule 4: an active session refuses every door and every flag. Stop
        # the turn first, then take the lease.
        if activity is TurnActivity.ACTIVE:
            raise busy_error(family, session, current)

        # Rule 5: another door's IDLE lease passes without a flag. No turn
        # runs, so nothing is lost, and invariant 3 stops costing 60 seconds.
        if current.holder is not holder:
            return self._grant(family, session, holder, door_instance, LeaseReason.TAKEN_OVER)

        # Rule 6: another instance of the caller's own door. For the TUI that
        # is a second terminal, and a human types the word that takes it.
        if takeover is Takeover.FORCE:
            return self._grant(family, session, holder, door_instance, LeaseReason.TAKEN_OVER)

        raise busy_error(family, session, current)

    def renew(self, family: str, session: str, turn: str | None = None) -> None:
        """Push the expiry out. Run a turn, steer and stop all renew."""
        lease = self.get(family, session)

        if lease is None:
            return

        lease.expires_at = now() + self._ttl

        if turn is not None:
            lease.turn = turn

        self._mirror(lease, family, session)

    def clear_turn(self, family: str, session: str) -> None:
        """Forget which turn runs under the lease. The lease itself stays.

        A settled turn must not stay named in the lease: the noticeboard would show
        a writer working on a turn that ended (contract 02 §7.1).
        """
        lease = self.get(family, session)

        if lease is None:
            return

        lease.turn = None
        lease.expires_at = now() + self._ttl
        self._mirror(lease, family, session)

    def release(
        self, family: str, session: str, holder: Holder, door_instance: str
    ) -> WriterLease | None:
        """Contract 02 §5.10. Drop the lease the caller holds.

        None means the session held none, which is a success and not an
        error: a door on its way out must never have to ask first.
        """
        current = self.get(family, session)

        if current is None:
            return None

        if not _same_writer(current, holder, door_instance):
            raise busy_error(family, session, current)

        self._leases.pop((family, session), None)
        current.turn = None
        self._mirror(current, family, session, released=True)
        return current

    def forget(self, family: str, session: str) -> None:
        """Remove the lease and its mirror. Used when a session is deleted."""
        self._leases.pop((family, session), None)
        lease_file(self._state_root, family, session).unlink(missing_ok=True)

    def _grant(
        self,
        family: str,
        session: str,
        holder: Holder,
        door_instance: str,
        reason: LeaseReason,
        turn: str | None = None,
    ) -> Grant:
        moment = now()
        lease = WriterLease(
            holder=holder,
            door_instance=door_instance,
            since=moment,
            expires_at=moment + self._ttl,
            turn=turn,
        )
        self._leases[(family, session)] = lease
        self._mirror(lease, family, session)
        return Grant(lease=lease, reason=reason)

    def _mirror(
        self,
        lease: WriterLease,
        family: str,
        session: str,
        released: bool = False,
    ) -> None:
        payload = lease.to_api()
        payload["family"] = family
        payload["session"] = session
        payload["released"] = released
        write_json(lease_file(self._state_root, family, session), payload, MODE_PUBLIC_READ)


def _same_writer(lease: WriterLease, holder: Holder, door_instance: str) -> bool:
    return lease.holder is holder and lease.door_instance == door_instance


def busy_error(family: str, session: str, lease: WriterLease) -> ApiError:
    """Contract 02 §7.2's body, with the holder block a caller needs."""
    return ApiError(
        ErrorCode.SESSION_BUSY,
        "another door holds the writer lease",
        family=family,
        session=session,
        detail=_holder_block(lease),
    )


def taken_over_error(family: str, session: str, lease: WriterLease | None) -> ApiError:
    """Contract 02 §7.4 rule 2. A renew that found the lease gone.

    Distinct from `session_busy` because the two ask for different things. A
    busy caller waits and tries again. A caller told this has lost the
    session to a human at another door, and retrying is the wrong move.
    """
    return ApiError(
        ErrorCode.LEASE_TAKEN_OVER,
        "another door took this idle lease",
        family=family,
        session=session,
        detail=_holder_block(lease) if lease is not None else {},
    )


def _holder_block(lease: WriterLease) -> dict[str, object]:
    """§7.2's block: which door, since when, and the turn under it.

    `door_instance` is left out. It names a process of that door and answers
    nothing a caller can act on.
    """
    detail = lease.to_api()
    detail.pop("door_instance", None)
    return detail
