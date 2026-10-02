"""An Open WebUI edit or regenerate becomes a pi branch (contract 02 §10.2).

Open WebUI sends `owui.parent_id` on every turn: the message the new user
message attaches to. Where that lands in the pi transcript is what decides
whether this turn continues the conversation or branches from an older point.

`owui_map` (§10.1) is the only thing that answers it. It is written in turn
order, two rows per settled turn, so its keys read

```
  u1  a1  u2  a2  u3  a3        <- Open WebUI message ids, oldest first
                      ^ parent_id here  = the current leaf  = continue
              ^ parent_id here          = an older entry    = fork at u3
```

The fork target is the USER entry that followed the parent, because pi's
`fork` branches from a previous user message on the active branch.

Nothing here talks to pi. It returns a decision, and `sessiond` puts
`branch: {fork_from: ...}` on `start_turn` (contract 03 §4.1) or does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .family_status import FamilyStatus
from .persona import Persona
from .requests import RunTurnRequest

# Contract 02 §10.2 rule 4 and §8.1's `branch_fallback` body.
UNMAPPED_PARENT = "unmapped_parent"
FORK_REFUSED = "fork_refused"


class Branch(Enum):
    """What contract 02 §10.2's four rules decide between."""

    PROMPT = "prompt"
    FORK = "fork"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class Decision:
    """One turn's branch decision, with the line it may owe the journal."""

    branch: Branch
    fork_from: str | None = None
    reason: str = ""

    @property
    def wire_branch(self) -> dict[str, str] | None:
        """Contract 03 §4.1's `branch` field, or None for a plain prompt."""
        if self.branch is not Branch.FORK or self.fork_from is None:
            return None

        return {"fork_from": self.fork_from}


@dataclass(slots=True)
class Fork:
    """What a refused fork needs to run the same turn without one.

    Contract 03 §4.1: pi may refuse a fork target that is not on the active
    branch, and the fallback decision stays on the host because policy is
    the host's job.
    """

    request: RunTurnRequest
    persona: Persona
    status: FamilyStatus
    sandbox: str


def decide(owui_map: dict[str, str], parent_id: str | None, turns_total: int) -> Decision:
    """Contract 02 §10.2's four rules, the first that matches."""
    # Rule 1: the first turn of a session has nothing to attach to.
    if parent_id is None and turns_total == 0:
        return Decision(Branch.PROMPT)

    # Rule 4: a parent the map never saw names no entry to fork from. A null
    # parent on a session that already has turns lands here too: it is an
    # edit of the first message, and §10.2 gives it no rule of its own.
    if parent_id is None or parent_id not in owui_map:
        return Decision(Branch.FALLBACK, reason=UNMAPPED_PARENT)

    keys = list(owui_map)
    index = keys.index(parent_id)

    # Rule 2: the newest mapped entry is the current pi leaf.
    if index == len(keys) - 1:
        return Decision(Branch.PROMPT)

    # Rule 3: the user entry that followed that parent.
    return Decision(Branch.FORK, fork_from=owui_map[keys[index + 1]])
