"""The identifiers a door mints, from contract 02 §2.

A test that plays a door mints the ids that door mints. No package of a
service gives them: a test imports none.
"""

from __future__ import annotations

import os
import time
from typing import Final

#: Contract 02 §2: upper-case Crockford base32. It has no I, L, O and U.
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_CHARS: Final = 26
_TIME_BITS: Final = 48
_RANDOM_BYTES: Final = 10
_BITS_PER_CHAR: Final = 5

#: The prefix of a session that the trigger door makes (contract 02 §2).
AUTO_PREFIX: Final = "auto-"


def new_ulid() -> str:
    """One ULID: 48 bits of the time in milliseconds, then 80 random bits."""
    stamp = int(time.time() * 1000) & ((1 << _TIME_BITS) - 1)
    number = (stamp << (_RANDOM_BYTES * 8)) | int.from_bytes(os.urandom(_RANDOM_BYTES), "big")
    chars = [
        _ALPHABET[(number >> (_BITS_PER_CHAR * place)) & (len(_ALPHABET) - 1)]
        for place in range(_ULID_CHARS)
    ]

    return "".join(reversed(chars))


def auto_session() -> str:
    """The id of one autonomous session, as the trigger door mints it."""
    return f"{AUTO_PREFIX}{new_ulid()}"
