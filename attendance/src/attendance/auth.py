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

    CHAPERONE_DELEGATE = "chaperone-delegate"
    #: The PEP's second token (contract 02 §3.1, §13.4). It is not a widened
    #: `chaperone-delegate`, so one stolen bearer reaches one family kind.
    CHAPERONE_DISPATCH = "chaperone-dispatch"
    DOOR_TRIGGER = "door-trigger"
    NOTICEBOARD = "noticeboard"
    #: The ACP door (phase 2 plan, row 17). It reaches `WS /acp/{family}` and
    #: nothing else.
    DOOR_ACP = "door-acp"
    CAREGIVER = "caregiver"


#: The two token files the PEP reads as user `chaperone`, and the only ones rule 5's
#: mode exception covers (contract 02 §3 rule 5).
PEP_READ_TOKENS: frozenset[Principal] = frozenset(
    {Principal.CHAPERONE_DELEGATE, Principal.CHAPERONE_DISPATCH}
)


class Access(StrEnum):
    """What a principal may do to a session."""

    WRITE = "write"
    READ = "read"
    INTERNAL = "internal"
    #: A byte pipe to one sandbox's facade. No session route takes it.
    ACP = "acp"


@dataclass(slots=True, frozen=True)
class Grant:
    """Contract 02 §3.1's row, as the code reads it."""

    access: Access
    kind: SessionKind | None


# Deny by default (invariant 11): a principal reaches exactly its own row.
_GRANTS: dict[Principal, Grant] = {
    Principal.CHAPERONE_DELEGATE: Grant(Access.WRITE, SessionKind.THIN),
    Principal.CHAPERONE_DISPATCH: Grant(Access.WRITE, SessionKind.AUTONOMOUS),
    Principal.DOOR_TRIGGER: Grant(Access.WRITE, SessionKind.AUTONOMOUS),
    Principal.NOTICEBOARD: Grant(Access.READ, None),
    # CONTRACT-QUESTION: the phase 2 plan names no family kind for the ACP
    # door. Reading taken: attended families only, the kind of the two doors
    # it replaces. A change costs this one row.
    Principal.DOOR_ACP: Grant(Access.ACP, SessionKind.ATTENDED),
    Principal.CAREGIVER: Grant(Access.INTERNAL, None),
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
