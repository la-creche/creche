"""The three advisory headers a sandbox sends (contract 04 §3).

All three are untrusted. The agent process builds them, so it can send any
value, including another session's id (invariant 12). Two rules hold that:

1. No header reaches a decision. `decide_family` takes the family, the grant
   file, the tool and the arguments, and nothing else (§3.1 rule 1).
2. The audit writes them under `claimed`, structurally separate from the
   trusted fields (§6.2), so a reader cannot mix the two up by accident.

A value that is not the shape contract 02 §2 fixes is dropped and reads as
absent. Dropping is not a denial: a header able to deny a call would be a
header that influences a decision, which is the thing §3.1 forbids. The drop
is logged, so a sandbox sending rubbish is visible.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from re import Pattern
from typing import Final

from .family_ids import SESSION_ID_RE, ULID_RE

log = logging.getLogger("chaperone.headers")

#: Contract 04 §3's table. Looked up lower case: HTTP header names are
#: case-insensitive and every ASGI server hands them over folded.
SESSION_ID_HEADER: Final = "x-session-id"
TURN_ID_HEADER: Final = "x-turn-id"
DELEGATION_ID_HEADER: Final = "x-delegation-id"

#: Contract 02 §2 caps neither a session id's length nor a header's. A ULID is
#: 26 characters, so only the session id needs a cap of its own. 128 matches
#: the other opaque strings the PEP reads from another process (contract 04
#: §1.2's `rev`).
MAX_SESSION_ID_CHARS: Final = 128
MAX_ULID_CHARS: Final = 26


@dataclass(frozen=True)
class Claimed:
    """What the caller says about itself. Never an input to a decision."""

    session_id: str | None = None
    turn_id: str | None = None
    delegation_id: str | None = None

    def as_record(self) -> dict[str, object]:
        """Contract 04 §6.2's object. All three keys always present, so a
        reader never has to tell "absent" from "this writer omits it"."""
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "delegation_id": self.delegation_id,
        }


NO_CLAIMS: Final = Claimed()


def _accept(name: str, value: str | None, shape: Pattern[str], cap: int) -> str | None:
    """One header, validated before use (invariant 12). Shape and size are
    checked before the value goes anywhere, including into a log line."""
    if value is None:
        return None
    if len(value) > cap:
        log.warning("dropping %s: %d characters exceeds the %d cap", name, len(value), cap)
        return None
    if shape.match(value) is None:
        # The value itself never reaches the log: it is caller-supplied and
        # would be the flood. Its length is enough to recognize.
        log.warning("dropping %s: %d characters, and not the contract's shape", name, len(value))
        return None
    return value


def read_claimed(headers: Mapping[str, str]) -> Claimed:
    """Contract 04 §3, for one request. Pure apart from the log."""
    folded = {key.lower(): value for key, value in headers.items()}
    return Claimed(
        session_id=_accept(
            SESSION_ID_HEADER, folded.get(SESSION_ID_HEADER), SESSION_ID_RE, MAX_SESSION_ID_CHARS
        ),
        turn_id=_accept(TURN_ID_HEADER, folded.get(TURN_ID_HEADER), ULID_RE, MAX_ULID_CHARS),
        delegation_id=_accept(
            DELEGATION_ID_HEADER, folded.get(DELEGATION_ID_HEADER), ULID_RE, MAX_ULID_CHARS
        ),
    )
