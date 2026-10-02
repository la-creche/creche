"""A `SessiondClient` test double, scoped to what this door calls: create-
or-find (contract 02 §5.1) and one `accepted`-mode turn (§5.4). Not a
wire-level fake — `test_trigger_fire.py`'s own HTTP-transport tests cover
`HttpSessiond` against a real ASGI-shaped server. A standalone,
protocol-level fake is a different tool (contract 02 §15.1's
`fake-sessiond/`).

Named `trigger_fake_sessiond.py`, not `fake_sessiond.py`: that basename is
already `door-owui/tests/fake_sessiond.py`, and this workspace's rootless
pytest import mode needs a unique module basename repo-wide
(`door-owui/AGENTS.md`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_door_trigger.errors import SessiondError
from agent_door_trigger.sessiond import AcceptedTurn, TurnRequest

_DEFAULT_ACCEPTED = AcceptedTurn(turn="01TESTTURN00000000000000", state="queued", journal_seq=1)


@dataclass
class FakeSessiond:
    """One script, played back to whichever request arrives next.

    Settable before a call:
      `accepted`     -- the AcceptedTurn a turn call returns.
      `turn_error`   -- a SessiondError raised by the next `accepted_turn`
                        call, then cleared.
      `ensure_error` -- a SessiondError raised by the next `ensure_session`
                        call, then cleared.

    Recorded for assertions:
      `ensured`  -- every (family, session) `ensure_session` saw.
      `requests` -- every `TurnRequest` a turn call saw, in order.
    """

    accepted: AcceptedTurn = field(default_factory=lambda: _DEFAULT_ACCEPTED)
    turn_error: SessiondError | None = None
    ensure_error: SessiondError | None = None

    ensured: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    requests: list[TurnRequest] = field(default_factory=list[TurnRequest])

    def ensure_session(self, family: str, session: str) -> None:
        self.ensured.append((family, session))
        if self.ensure_error is not None:
            error, self.ensure_error = self.ensure_error, None
            raise error

    def accepted_turn(self, request: TurnRequest) -> AcceptedTurn:
        self.requests.append(request)
        if self.turn_error is not None:
            error, self.turn_error = self.turn_error, None
            raise error

        return self.accepted
