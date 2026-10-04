"""Identifier forms and the one ULID this door mints (contract 02 §2).

The door names the session it creates. `attendance` names turns. A session id
becomes a directory name on the host and inside the sandbox, so its form is
checked here before anything builds a path or a command from it (invariants
12 and 14).

These rules are copied from the contract, not imported from `attendance`. The
two programs are separate processes and the wire is the only thing between
them: copy a proven fact, not a module.
"""

from __future__ import annotations

import re
import secrets
import time

# Crockford base32: no I, L, O or U, so a read-back id cannot be mistyped.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_BITS_PER_CHAR = 5
_CHAR_MASK = 0x1F

ULID_LENGTH = 26
_ULID_TIME_CHARS = 10
_ULID_RANDOM_CHARS = 16
_ULID_RANDOM_BITS = 80
_MILLIS_PER_SECOND = 1000

# Contract 02 §2. A session id is 1 to 128 characters, and `tui-<ulid>` is 30.
SESSION_ID_MAX = 128
TITLE_MAX = 200

#: Contract 02 §2's prefix for a session this door makes.
TUI_PREFIX = "tui-"

_FAMILY_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")
_SANDBOX_RE = re.compile(r"^([a-z][a-z0-9-]{1,30})-s([0-9]{1,9})\Z")
_DOT_NAMES = frozenset({".", ".."})

#: Contract 02 §2's four prefixes, without the trailing dash.
_DOORS = ("tui", "owui", "job", "auto")


def is_family(value: str) -> bool:
    """A family name, `[a-z][a-z0-9-]{1,30}`."""
    return bool(_FAMILY_RE.match(value))


def is_session(value: str) -> bool:
    """A session id, capped at `SESSION_ID_MAX` and never `.` or `..`."""
    if len(value) > SESSION_ID_MAX or value in _DOT_NAMES:
        return False

    return bool(_SESSION_RE.match(value))


def is_ulid(value: str) -> bool:
    """26 characters of upper-case Crockford base32 (contract 02 §2)."""
    return bool(_ULID_RE.match(value))


def is_sandbox(value: str) -> bool:
    """A sandbox id, `<family>-s<N>` (contract 05 §4.1)."""
    return bool(_SANDBOX_RE.match(value))


def sandbox_number(value: str) -> int:
    """`N` from `<family>-s<N>`, so the newest sandbox can be picked.

    `N` is monotonic and never reused, so the highest one is the newest. A
    value that is not a sandbox id sorts below every real one.
    """
    found = _SANDBOX_RE.match(value)

    return int(found.group(2)) if found is not None else -1


def door_of(session: str) -> str:
    """Which door made a session, from its prefix. Empty when none claims it."""
    for door in _DOORS:
        if session.startswith(f"{door}-"):
            return door

    return ""


def new_session_id() -> str:
    """A fresh `tui-<ulid>` for a session this terminal is starting."""
    return TUI_PREFIX + _new_ulid()


def _new_ulid() -> str:
    """A ULID: 48 bits of milliseconds, then 80 bits of randomness."""
    millis = int(time.time() * _MILLIS_PER_SECOND)
    random_part = secrets.randbits(_ULID_RANDOM_BITS)

    return _encode(millis, _ULID_TIME_CHARS) + _encode(random_part, _ULID_RANDOM_CHARS)


def _encode(value: int, chars: int) -> str:
    """Crockford base32, most significant character first."""
    out = ["0"] * chars

    for index in range(chars - 1, -1, -1):
        out[index] = _CROCKFORD[value & _CHAR_MASK]
        value >>= _BITS_PER_CHAR

    return "".join(out)
