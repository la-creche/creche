"""What survives an autonomous job (contract 02 §13.1).

`outcomes.build()` sums `spend_usd` from each turn's advisory usage
(contract 03 §5.2's `turn_settled.usage`, folded by the playpen from
pi's own `cost.total`). A job with tool calls through the PEP and a model
answer can still sum to `spend_usd: 0.0`. That fold is unverified against
what pi actually sends, and contract 05 §7 names LiteLLM the one authority
for spend — this module cannot read LiteLLM or the family status
document's `spend` block itself (`outcomes` and `family_status` are peers,
`sessiond/AGENTS.md` structure rule 4: layers talk downward only). So the
rule stays inside what `outcomes.py` already holds: a turn that used real
tokens and reports zero cost almost never really cost nothing, and that
combination is `null` with a reason, never a false `$0.00`. A turn that
never reached a model still reports an honest zero.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_sessiond.models import Session, Trigger, TriggerKind, Turn, Usage
from agent_sessiond.outcomes import SPEND_UNKNOWN_REASON, build
from agent_sessiond.states import SessionKind, TurnState

FAMILY = "scrum-lead"
SESSION = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
TURN = "01JBQ80M4F7S2YQ1VZK6W3TDEN"
SANDBOX = "scrum-lead-s2"
STARTED = datetime(2026, 9, 21, 6, 0, 1, tzinfo=UTC)
ENDED = datetime(2026, 9, 21, 6, 4, 37, tzinfo=UTC)


def _session() -> Session:
    return Session(
        family=FAMILY,
        session=SESSION,
        kind=SessionKind.AUTONOMOUS,
        created_at=STARTED,
        updated_at=ENDED,
        sandbox=SANDBOX,
        trigger=Trigger(kind=TriggerKind.TIMER, name="morning-triage", fired_at=STARTED),
    )


def _turn(usage: Usage, *, state: TurnState = TurnState.SETTLED) -> Turn:
    return Turn(
        turn=TURN,
        session=SESSION,
        family=FAMILY,
        state=state,
        started_at=STARTED,
        ended_at=ENDED,
        sandbox=SANDBOX,
        usage=usage,
    )


def test_a_real_cost_is_reported_as_is() -> None:
    """The ordinary case: contract 03 §5.2's advisory number is nonzero,
    and contract 02 §13.1 still says `summed from turn usage`."""
    record = build(_session(), [_turn(Usage(input=4120, output=188, cost_usd=0.014))])

    assert record.spend_usd == 0.014
    assert record.spend_reason is None


def test_two_turns_sum() -> None:
    turns = [
        _turn(Usage(input=100, output=10, cost_usd=0.01)),
        _turn(Usage(input=200, output=20, cost_usd=0.02)),
    ]

    record = build(_session(), turns)

    assert record.spend_usd == 0.03
    assert record.spend_reason is None


def test_nothing_ran_is_an_honest_zero() -> None:
    """A turn that never reached a model (contract 05 §3.3's PEP outage,
    for example) truly cost nothing, and the record must say so plainly,
    not hide a real zero behind `null`."""
    record = build(_session(), [_turn(Usage(), state=TurnState.FAILED)])

    assert record.spend_usd == 0.0
    assert record.spend_reason is None


def test_tokens_with_no_cost_is_unknown_not_free() -> None:
    """Real tokens and zero cost is the unverified advisory fold showing, not
    evidence that the job was free."""
    record = build(_session(), [_turn(Usage(input=4120, output=188, cost_usd=0.0))])

    assert record.spend_usd is None
    assert record.spend_reason == SPEND_UNKNOWN_REASON


def test_cache_tokens_alone_are_enough_to_call_a_zero_cost_unknown() -> None:
    record = build(_session(), [_turn(Usage(cache_read=500, cost_usd=0.0))])

    assert record.spend_usd is None
    assert record.spend_reason == SPEND_UNKNOWN_REASON


def test_one_turn_with_tokens_outweighs_one_turn_without() -> None:
    """The whole job is one record. One turn's real tokens make the
    summed zero suspect even if a second turn genuinely did nothing."""
    turns = [
        _turn(Usage(), state=TurnState.FAILED),
        _turn(Usage(input=50, output=5, cost_usd=0.0)),
    ]

    record = build(_session(), turns)

    assert record.spend_usd is None
    assert record.spend_reason == SPEND_UNKNOWN_REASON


def test_a_real_cost_wins_over_an_unreported_turn() -> None:
    """A nonzero sum is trusted outright, the same as before this fix."""
    turns = [
        _turn(Usage(input=4120, output=188, cost_usd=0.014)),
        _turn(Usage(input=50, output=5, cost_usd=0.0)),
    ]

    record = build(_session(), turns)

    assert record.spend_usd == 0.014
    assert record.spend_reason is None


def test_the_record_still_serializes_a_null_spend() -> None:
    """`to_file()` is what lands on disk, and the one view already reads a
    `None` here (`view/src/agent_view/statusdocs.py`'s `OutcomeRow`, and
    `family.html`'s template already renders it as "—")."""
    body = build(_session(), [_turn(Usage(input=10, cost_usd=0.0))]).to_file()

    assert body["spend_usd"] is None
    assert body["spend_reason"] == SPEND_UNKNOWN_REASON


def test_a_known_spend_still_serializes_with_no_reason() -> None:
    body = build(_session(), [_turn(Usage(input=10, output=1, cost_usd=0.02))]).to_file()

    assert body["spend_usd"] == 0.02
    assert body["spend_reason"] is None
