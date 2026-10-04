"""ULID minting: 26 characters, upper-case Crockford base32 (contract 02 §2).

Every ULID in the seven rework contracts is this exact alphabet and this
exact length, "a session id's `<ulid>` suffix" named among them. `attendance`
mints turn ids this same way; this door mints the `auto-<ulid>` session id
for each firing and the turn's own `idempotency_key`, so the shape must
already match before the two processes ever talk (contract 02 §2, §6).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable

#: Contract 02 §2's own spelling: excludes I, L, O and U so a human reading
#: one aloud never confuses a letter for a digit.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ULID_LENGTH = 26
ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")

_RANDOM_BITS = 80
_RANDOM_BYTES = _RANDOM_BITS // 8
_CHAR_BITS = 5
_FIRST_SHIFT = (ULID_LENGTH * _CHAR_BITS) - _CHAR_BITS  # 125: 26 chars * 5 bits, top char first


def new_ulid(*, now: Callable[[], float] = time.time) -> str:
    """A fresh ULID: 48 bits of milliseconds since the epoch, then 80 bits
    of randomness. Two ULIDs minted in the same millisecond still almost
    certainly differ, because the random half dominates the collision odds.
    `now` is a seam for a test that wants a fixed timestamp.
    """
    timestamp_ms = int(now() * 1000)
    randomness = int.from_bytes(os.urandom(_RANDOM_BYTES), "big")
    value = (timestamp_ms << _RANDOM_BITS) | randomness

    return _encode(value)


def _encode(value: int) -> str:
    """Every character reads the next 5 bits, most significant first.

    The loop asks for 26 * 5 = 130 bits from a value that only ever holds
    128 (48 + 80), so the top character's own top 2 bits are always 0 —
    Python's arbitrary-precision integers already read that way with no
    explicit padding, because a shift past the value's own width answers
    zero rather than raising.
    """
    return "".join(
        _ALPHABET[(value >> shift) & 0b11111]
        for shift in range(_FIRST_SHIFT, -_CHAR_BITS, -_CHAR_BITS)
    )
