"""Bearer tokens and what each one may do (contract 02 §3, §3.1).

The service fails closed. A missing, empty or short token file stops the
service from starting at all. An empty key would turn a LAN admin surface
into an open one, and this rule exists so that cannot happen (§3 rule 7).

Nothing here ever logs a token, a prefix of a token, or its length. An error
message names the door, never the value.
"""

from __future__ import annotations

import secrets
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .atomic import is_group_or_world_readable
from .errors import ApiError, ErrorCode
from .ids import SessionPrefix
from .models import Holder
from .paths import token_file
from .states import SessionKind

# Contract 02 §3 rule 7. 32 bytes is the floor, not the recommendation.
MIN_TOKEN_BYTES = 32

# Contract 02 §3 rule 5's exception: the two tokens the PEP reads, the
# delegate door's and the dispatch door's (§13.4), may carry the group-read
# bit, because the PEP runs as a different user
# (`chaperone`). Nothing wider: no world bit, and no group write or execute. Every
# other token stays 0600.
_PEP_ALLOWED_GROUP_OTHER_BITS = stat.S_IRGRP

_BEARER = "Bearer "


class Principal(StrEnum):
    """One row of contract 02 §3.1. The token file is named after it."""

    DOOR_OWUI = "door-owui"
    DOOR_TUI = "door-tui"
    DOOR_DELEGATE = "door-delegate"
    #: The PEP's second token (contract 02 §3.1, §13.4). It is not a widened
    #: `door-delegate`, so one stolen bearer reaches one family kind.
    DOOR_DISPATCH = "door-dispatch"
    DOOR_TRIGGER = "door-trigger"
    VIEW_RO = "view-ro"
    #: The old name: the host minted `tokens/managerd.token` under it.
    CAREGIVER = "managerd"


#: The two token files the PEP reads as user `chaperone`, and the only ones rule 5's
#: mode exception covers (contract 02 §3 rule 5).
PEP_READ_TOKENS: frozenset[Principal] = frozenset(
    {Principal.DOOR_DELEGATE, Principal.DOOR_DISPATCH}
)


class Access(StrEnum):
    """What a principal may do to a session."""

    WRITE = "write"
    READ = "read"
    INTERNAL = "internal"


@dataclass(slots=True, frozen=True)
class Grant:
    """Contract 02 §3.1's row, as the code reads it."""

    access: Access
    holder: Holder | None
    prefix: SessionPrefix | None
    kind: SessionKind | None


# Deny by default (invariant 11): a principal reaches exactly its own row.
_GRANTS: dict[Principal, Grant] = {
    Principal.DOOR_OWUI: Grant(Access.WRITE, Holder.OWUI, SessionPrefix.OWUI, SessionKind.ATTENDED),
    Principal.DOOR_TUI: Grant(Access.WRITE, Holder.TUI, SessionPrefix.TUI, SessionKind.ATTENDED),
    Principal.DOOR_DELEGATE: Grant(
        Access.WRITE, Holder.DELEGATE, SessionPrefix.JOB, SessionKind.THIN
    ),
    Principal.DOOR_DISPATCH: Grant(
        Access.WRITE, Holder.DISPATCH, SessionPrefix.AUTO, SessionKind.AUTONOMOUS
    ),
    Principal.DOOR_TRIGGER: Grant(
        Access.WRITE, Holder.TRIGGER, SessionPrefix.AUTO, SessionKind.AUTONOMOUS
    ),
    Principal.VIEW_RO: Grant(Access.READ, None, None, None),
    Principal.CAREGIVER: Grant(Access.INTERNAL, None, None, None),
}


class TokenError(Exception):
    """A token file cannot be used, so the service must not start."""


def grant_of(principal: Principal) -> Grant:
    """What contract 02 §3.1 lets this principal do."""
    return _GRANTS[principal]


class TokenBook:
    """Every door token, and the checks contract 02 §3.1 pins to each one."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        # Tokens stay bytes. Nothing decodes them, so no encoding question can
        # make two different files compare equal.
        self._tokens: dict[Principal, bytes] = {}

    def load(self) -> None:
        """Read every token file. Raises TokenError rather than starting weak."""
        loaded: dict[Principal, bytes] = {}

        for principal in Principal:
            loaded[principal] = _read_token(self._state_root, principal)

        self._tokens = loaded

    def reload(self) -> None:
        """SIGHUP. A bad file leaves the live set untouched (§3 rule 8)."""
        self.load()

    def identify(self, header: str | None) -> Principal:
        """Which principal sent this request, or `unauthorized`.

        Every token is compared, with no early exit, so the time taken says
        nothing about which one matched or how far a guess got.
        """
        offered = _bearer_value(header)

        if offered is None:
            raise _unauthorized()

        encoded = offered.encode("utf-8")
        found: Principal | None = None

        for principal, token in self._tokens.items():
            if secrets.compare_digest(encoded, token):
                found = principal

        if found is None:
            raise _unauthorized()

        return found


def check_access(principal: Principal, wanted: Access) -> None:
    """Refuse a principal that may not do this (contract 02 §3.1)."""
    held = grant_of(principal).access

    if held is wanted:
        return

    # A writer reads its own sessions. A reader never writes, and the internal
    # principal reaches /internal/* and nothing else.
    if wanted is Access.READ and held is Access.WRITE:
        return

    raise ApiError(
        ErrorCode.FORBIDDEN,
        f"{principal.value} may not {wanted.value}",
        detail={"principal": principal.value},
    )


def check_family_kind(principal: Principal, family: str, kind: SessionKind) -> None:
    """A door token reaches one family kind only (contract 02 §3.1)."""
    allowed = grant_of(principal).kind

    if allowed is None or allowed is kind:
        return

    # No article. `a attended` is what one reads otherwise, and the message
    # is what `agent-trigger fire` prints into systemd's journal.
    raise ApiError(
        ErrorCode.FORBIDDEN,
        f"{principal.value} may not touch families of kind {kind.value}",
        family=family,
        detail={"principal": principal.value, "kind": kind.value},
    )


def check_session_prefix(principal: Principal, family: str, session: str) -> None:
    """A door creates one session-id prefix only (contract 02 §3.1).

    §3.1 runs this check on create and nowhere else. Read as every request,
    the TUI could never write to an `owui-` session, which contradicts
    invariant 3 ("one session, every UI") and §7.2's own example of the TUI
    meeting an `owui` lease holder. The family-kind check still runs on every
    call.
    """
    allowed = grant_of(principal).prefix

    if allowed is None:
        return

    if session.startswith(allowed.value):
        return

    raise ApiError(
        ErrorCode.FORBIDDEN,
        f"{principal.value} may not touch this session id",
        family=family,
        session=session,
        detail={"principal": principal.value, "expected_prefix": allowed.value},
    )


def holder_of(principal: Principal, family: str, session: str) -> Holder:
    """Which lease holder this principal writes as (contract 02 §7.1)."""
    holder = grant_of(principal).holder

    if holder is None:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"{principal.value} is not a writer",
            family=family,
            session=session,
            detail={"principal": principal.value},
        )

    return holder


def _unauthorized() -> ApiError:
    return ApiError(ErrorCode.UNAUTHORIZED, "missing or unknown token")


def _bearer_value(header: str | None) -> str | None:
    if header is None or not header.startswith(_BEARER):
        return None

    value = header[len(_BEARER) :].strip()
    return value if value else None


def _read_token(state_root: Path, principal: Principal) -> bytes:
    """One token file, checked before it is trusted (contract 02 §3 rules 5-7)."""
    path = token_file(state_root, principal.value)

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise TokenError(f"token file for {principal.value} is unreadable: {path}") from error

    value = raw.strip()

    if not value:
        raise TokenError(f"token file for {principal.value} is empty: {path}")

    if len(value) < MIN_TOKEN_BYTES:
        raise TokenError(
            f"token file for {principal.value} is under {MIN_TOKEN_BYTES} bytes: {path}"
        )

    if _mode_too_wide(path, principal):
        allowed = "0600 or 0640" if principal in PEP_READ_TOKENS else "0600"
        raise TokenError(f"token file for {principal.value} is not mode {allowed}: {path}")

    return value


def _mode_too_wide(path: Path, principal: Principal) -> bool:
    """Contract 02 §3 rules 5-7, and rule 5's exception.

    Every principal the PEP does not read must fail
    `is_group_or_world_readable` exactly as before. The two it does read may
    also carry the group-read bit, and nothing past it.
    """
    if principal not in PEP_READ_TOKENS:
        return is_group_or_world_readable(path)

    bits = stat.S_IMODE(path.stat().st_mode) & 0o077
    return bits & ~_PEP_ALLOWED_GROUP_OTHER_BITS != 0
