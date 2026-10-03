"""What a terminal wrote, read back and paired (contract 02 §10.5).

A terminal hands its tty to pi inside the sandbox (contract 03 §7.6), so its
exchanges reach the pi store and never reach this service as turns. When the
`tui` writer lease ends, `sessiond` asks the playpen for the entries after
the cursor it holds (contract 03 §4.8) and turns them into exchanges:

```
  e5 user       "what did the sensor read"   -.
  e6 assistant  "21.4 degrees at 09:12"       |-- one exchange, cursor -> e6
  e7 user       "and yesterday"              ---- no answer yet, so not yet
```

Two properties the rest of the service depends on:

1. **A trailing user entry is never an exchange.** The terminal may have been
   killed mid-turn, or pi may still be writing. The cursor stops before it,
   so the next read sees that entry again with its answer.
2. **An entry the host already knows is skipped.** A turn this service ran
   put its own entries in the same store, and re-reading them would copy
   the operator's chat turns into their chat a second time.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .wire import PiEntry

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Exchange:
    """One terminal prompt and its answer, with both pi entry ids."""

    user_entry_id: str
    entry_id: str
    prompt: str
    answer: str


@dataclass(frozen=True, slots=True)
class Paired:
    """What one read yielded, and where the next read starts."""

    exchanges: list[Exchange] = field(default_factory=list[Exchange])
    cursor: str | None = None


def pair(entries: Sequence[PiEntry], known: Iterable[str] = ()) -> Paired:
    """Contract 02 §10.5 rules 2 and 3. Complete exchanges, and the cursor."""
    seen = set(known)
    found: list[Exchange] = []
    cursor: str | None = None
    opened: PiEntry | None = None

    for entry in entries:
        if entry.role == ROLE_USER:
            # A second user entry with no answer between them replaces the
            # first. pi wrote them in that order, so the older one was
            # answered by nothing.
            opened = entry
            continue

        if entry.role != ROLE_ASSISTANT or opened is None:
            continue

        if entry.id not in seen and opened.id not in seen:
            found.append(
                Exchange(
                    user_entry_id=opened.id,
                    entry_id=entry.id,
                    prompt=opened.text,
                    answer=entry.text,
                )
            )

        # Rule 3: the cursor moves to the end of every COMPLETE exchange,
        # including one that was skipped. Leaving it behind a known entry
        # would read that entry forever.
        cursor = entry.id
        opened = None

    return Paired(exchanges=found, cursor=cursor)
