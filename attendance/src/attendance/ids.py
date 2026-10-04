"""Identifier forms and ULID minting (contract 02 §2).

Every id that crosses a process boundary is validated here before anything
else touches it (invariants 12 and 14). A session id becomes a directory name,
so a bad one is a path problem, not only a data problem.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from enum import StrEnum

# Crockford base32: no I, L, O or U, so a read-back id cannot be mistyped.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_BITS_PER_CHAR = 5
_CHAR_MASK = 0x1F

ULID_LENGTH = 26
_ULID_TIME_CHARS = 10
_ULID_RANDOM_CHARS = 16
_ULID_RANDOM_BITS = 80
_ULID_MAX_RANDOM = (1 << _ULID_RANDOM_BITS) - 1
_MILLIS_PER_SECOND = 1000

# Contract 02 §2 caps a session id at 128 characters. It becomes a directory
# name and arrives from a door, so it needs a bound (invariant 14). 128 holds
# every prefixed form the contract names with room to spare: `owui-<uuid>`
# is 41 characters and `tui-<ulid>` is 30.
SESSION_ID_MAX = 128
TITLE_MAX = 200
ATTACHMENT_NAME_MAX = 120
LABEL_KEYS_MAX = 10
LABEL_VALUE_MAX = 200

_FAMILY_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")
_SANDBOX_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}-s[0-9]{1,9}\Z")
_ATTACHMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\Z")
_DOT_NAMES = frozenset({".", ".."})


class SessionPrefix(StrEnum):
    """Which door made a session (contract 02 §2)."""

    OWUI = "owui-"
    TUI = "tui-"
    JOB = "job-"
    AUTO = "auto-"


def is_family(value: str) -> bool:
    """A family name, `[a-z][a-z0-9-]{1,30}`."""
    return bool(_FAMILY_RE.match(value))


def is_session(value: str) -> bool:
    """A session id, capped at SESSION_ID_MAX and never `.` or `..`."""
    if len(value) > SESSION_ID_MAX:
        return False

    if value in _DOT_NAMES:
        return False

    return bool(_SESSION_RE.match(value))


def is_ulid(value: str) -> bool:
    """A 26-character Crockford base32 ULID. Turn ids take this form."""
    return bool(_ULID_RE.match(value))


def is_sandbox(value: str) -> bool:
    """A sandbox id, `<family>-s<N>`."""
    return bool(_SANDBOX_RE.match(value))


def is_attachment(value: str) -> bool:
    """An inbox file name (contract 02 §5.4.1)."""
    if value in _DOT_NAMES:
        return False

    return bool(_ATTACHMENT_RE.match(value))


def session_prefix(value: str) -> SessionPrefix | None:
    """Which door a session id claims to come from. None when it claims none."""
    for prefix in SessionPrefix:
        if value.startswith(prefix.value):
            return prefix

    return None


def _encode(value: int, chars: int) -> str:
    """Crockford base32, most significant character first."""
    out = ["0"] * chars

    for index in range(chars - 1, -1, -1):
        out[index] = _CROCKFORD[value & _CHAR_MASK]
        value >>= _BITS_PER_CHAR

    return "".join(out)


class UlidFactory:
    """Mints ULIDs that sort by time and never repeat inside one process.

    Two turns can start in the same millisecond. Plain randomness would then
    order them arbitrarily in a listing, so the random half increments instead
    (the monotonic rule of the ULID spec).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = 0
        self._last_random = 0

    def mint(self) -> str:
        with self._lock:
            millis = int(time.time() * _MILLIS_PER_SECOND)

            # A clock that steps backwards must not produce a smaller id, so
            # the previous millisecond is reused until the wall clock catches up.
            if millis <= self._last_ms:
                millis = self._last_ms
                self._last_random = (self._last_random + 1) & _ULID_MAX_RANDOM
            else:
                self._last_random = secrets.randbits(_ULID_RANDOM_BITS)

            self._last_ms = millis
            random_part = self._last_random

        return _encode(millis, _ULID_TIME_CHARS) + _encode(random_part, _ULID_RANDOM_CHARS)


_FACTORY = UlidFactory()


def new_ulid() -> str:
    """A fresh ULID from the process-wide factory."""
    return _FACTORY.mint()


def sha256_hex(text: str) -> str:
    """The digest behind `persona_hash` and the stored prompt digest.

    A digest stands in for text the service must compare but need not keep:
    the persona of a turn, and the prompt of an idempotency key (contract 02
    §4.2, §6 rule 5).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
