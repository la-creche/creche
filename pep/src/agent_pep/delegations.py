"""The delegation ids the PEP mints, and the chains they name.

Contract 04 §7.4: the PEP mints one ULID per delegate call, and nothing else
mints one. That is what makes `chain` in §6.3 evidence while the
`X-Delegation-Id` header in §3 is not.

```
  chat --invoke_agent--> PEP mints D, records D -> ("chat", "vault-oracle")
                          |
                          `--> attendance /delegate --> vault-oracle's sandbox
                                                        |
   vault-oracle's own PEP calls carry X-Delegation-Id: D  |
                          .-----------------------------'
                          v
        chain_for(D, "vault-oracle") -> ("chat", "vault-oracle")
```

Two rules keep the header from becoming authority:

1. The id is only an index into this table. A value the PEP never minted names
   no chain, so the call is a chain of one (§3.1 rule 2).
2. The recorded chain ends in the family the PEP delegated TO. A different
   family presenting that id gets a chain of one as well, so no family can
   inherit a chain it was never in.

State is in memory and expires. A restart forgets it: the chain is a record
for the audit and a hop count, never a permission. Reach is per family and the
grant file is the only source of it (invariant 11).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: Contract 01 §3.6 rule 4: there is no depth field, because a thin family's
#: `delegates` must be empty and a chain is therefore at most one hop. The PEP
#: enforces the same bound itself rather than trusting that every family file
#: was written correctly (invariant 11, deny by default).
MAX_DELEGATION_HOPS: Final = 1

#: A chain is loop prevention and an audit record, not authority, so it may
#: expire, after one hour.
CHAIN_TTL_S: Final = 3600.0

#: Crockford base32, upper case, excluding I, L, O and U — contract 04 §4.1.
_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_TIME_CHARS: Final = 10
_ULID_RANDOM_CHARS: Final = 16
_BITS_PER_CHAR: Final = 5

Clock = Callable[[], float]


def _encode(value: int, chars: int) -> str:
    """`value` as `chars` Crockford base32 digits, most significant first."""
    out: list[str] = []
    for shift in range(chars - 1, -1, -1):
        out.append(_CROCKFORD[(value >> (shift * _BITS_PER_CHAR)) & 0x1F])
    return "".join(out)


def new_delegation_id() -> str:
    """One ULID: 48 bits of millisecond time, then 80 random bits.

    Upper case, and only the alphabet contract 04 §4.1 names, so the value the
    PEP mints is one its own `ULID_RE` accepts on the way back in.
    """
    millis = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(millis, _ULID_TIME_CHARS) + _encode(randomness, _ULID_RANDOM_CHARS)


@dataclass(frozen=True)
class _Chain:
    """One minted id: who the call passed through, and when it was minted."""

    families: tuple[str, ...]
    born: float


class DelegationTable:
    """The PEP's own record of the delegate calls it started.

    One object per process. `family_app` reads it for the hop count and the
    audit chain, and `delegate.py` never sees it: minting happens above the
    door client so a failed call still spends its id.
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._chains: dict[str, _Chain] = {}
        self._inflight: dict[str, int] = {}

    def mint(self, families: tuple[str, ...]) -> str:
        """Record one delegate call and answer with its id.

        `families` is the caller's own chain with the target appended, so the
        target's calls read their whole path back out of one lookup.
        """
        self._evict()
        minted = new_delegation_id()
        self._chains[minted] = _Chain(families=families, born=self._clock())
        return minted

    def chain_for(self, delegation_id: str | None, family: str) -> tuple[str, ...]:
        """Contract 04 §6.3's `chain`: caller first, this family last."""
        recorded = self._chains.get(delegation_id) if delegation_id else None
        if recorded is None:
            return (family,)

        # The chain the PEP recorded ends in the family it delegated to. A
        # different family presenting the id would otherwise inherit a chain
        # it was never in, which is the one way a header could still colour
        # the record (§3.1 rule 3).
        if recorded.families[-1] != family:
            return (family,)

        return recorded.families

    def hops(self, delegation_id: str | None, family: str) -> int:
        """How many delegate calls already led to this one. Top level is 0."""
        return len(self.chain_for(delegation_id, family)) - 1

    def inflight(self, family: str) -> int:
        """Contract 04 §5 row 7's counter, per family."""
        return self._inflight.get(family, 0)

    def begin(self, family: str) -> None:
        self._inflight[family] = self.inflight(family) + 1

    def end(self, family: str) -> None:
        left = self.inflight(family) - 1
        if left <= 0:
            self._inflight.pop(family, None)
            return
        self._inflight[family] = left

    def _evict(self) -> None:
        """Drop chains past the TTL, so a long-running PEP cannot grow one
        entry per delegate call for as long as it runs."""
        now = self._clock()
        stale = [key for key, chain in self._chains.items() if now - chain.born > CHAIN_TTL_S]
        for key in stale:
            del self._chains[key]
