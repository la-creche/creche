"""The action string the live Node-RED flow will accept.

One regular expression stands between a release request and a working tap:

    ACTION_RE = /^AGENT_(APPROVE|DENY)_[0-9a-z]{26}_[0-9a-f]{16}$/

(`docs/node-red-mcp.md` §A.2, measured on the live flow.)

The flow builds the action string from the push body's middle field and its
gate. So root's push must put a 26-character LOWER-CASE Crockford ULID in
the middle field, or the operator's tap is dropped by the flow before it reaches
anything.

Two facts make this a real blocker and not a style point.

1. The literal `release` in that field is seven characters, so no tap
   could ever arrive.
2. Every rework ULID is UPPER-case Crockford (contract 06 §9, contract 02
   §2). `[0-9a-z]{26}` refuses upper case, so sending the
   request id as it is written on disk would ALSO be dropped. The lower
   case conversion is the fix, and it is exact: Crockford's alphabet
   lower-cased is a subset of `[0-9a-z]`.
"""

from __future__ import annotations

import re
from typing import Final

from agent_release.executor.approval import (
    ACTION_RE,
    Decision,
    Summary,
    Verdict,
    action_id_of,
    deny_all,
    release_action,
)
from agent_release.executor.phone import make_transport

#: One valid request id, as `request.ULID_RE` accepts it: 26 upper-case
#: Crockford characters, I, L, O and U absent.
REQUEST_ID: Final = "01K5J8M2Q7V3X9R4T6N0B8C2DE"

#: What `approval.gate_id` produces: 16 lower-case hexadecimal characters.
GATE: Final = "4f2a8c31b09d6e57"


def _summary() -> Summary:
    return Summary(
        review="safe: 1 component(s), service",
        components="chaperone 2.0.3 -> 2.1.0",
        contracts="6 contracts satisfied",
        restarts="creche-chaperone.service",
        restore="automatic",
        requested_by="agent-control",
        manifest="6b84c0e5aa10",
    )


def test_the_live_flow_pattern_refuses_what_r7_3_sent() -> None:
    """The regression this test exists for, stated directly."""
    assert ACTION_RE.fullmatch(f"AGENT_APPROVE_release_{GATE}") is None


def test_an_upper_case_ulid_is_refused_by_the_live_flow() -> None:
    """Why the id is lower-cased and not merely passed through."""
    assert ACTION_RE.fullmatch(f"AGENT_APPROVE_{REQUEST_ID}_{GATE}") is None


def test_the_action_id_is_the_request_id_in_lower_case() -> None:
    assert action_id_of(REQUEST_ID) == REQUEST_ID.lower()


def test_the_action_string_matches_the_live_flow() -> None:
    action = release_action(action_id_of(REQUEST_ID), GATE)

    assert ACTION_RE.fullmatch(action) is not None


def test_every_crockford_character_survives_the_conversion() -> None:
    """Crockford's alphabet, lower-cased, stays inside `[0-9a-z]{26}`."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    for start in range(len(alphabet) - 25):
        candidate = alphabet[start : start + 26]
        assert ACTION_RE.fullmatch(f"AGENT_DENY_{action_id_of(candidate)}_{GATE}") is not None


def test_the_push_carries_an_id_the_flow_accepts() -> None:
    """The transport's own body, read as the flow reads it."""
    sent: list[dict[str, object]] = []

    def post(body: dict[str, object]) -> dict[str, object] | None:
        sent.append(body)
        if body.get("kind") == "approval":
            return {}

        return {"gate": GATE, "decision": "approve", "at": 1758153712.0}

    transport = make_transport("https://hook.invalid/x", "t", lambda _: None, lambda: 0.0, post)
    decision = transport(action_id_of(REQUEST_ID), GATE, _summary(), 900.0)

    assert decision.verdict is Verdict.GRANTED
    push = sent[0]
    assert ACTION_RE.fullmatch(f"AGENT_APPROVE_{push['family']}_{push['gate']}") is not None


def test_the_push_still_says_what_it_is() -> None:
    """The id is opaque, so the body carries a label a human reads."""
    sent: list[dict[str, object]] = []

    def post(body: dict[str, object]) -> dict[str, object] | None:
        sent.append(body)

        return {"gate": GATE, "decision": "deny", "at": 1.0}

    transport = make_transport("https://hook.invalid/x", "t", lambda _: None, lambda: 0.0, post)
    transport(action_id_of(REQUEST_ID), GATE, _summary(), 900.0)

    assert sent[0]["label"] == "release"


def test_a_push_whose_id_the_flow_would_drop_never_goes_out() -> None:
    """Fail closed: a tap that can never arrive is a fifteen minute stall.

    Root checks its own action string against the flow's rule before it
    pushes, and denies at once when it would not match. That is contract 04
    §8.4's rule for an undeliverable push, applied one step earlier.
    """
    sent: list[dict[str, object]] = []

    def post(body: dict[str, object]) -> dict[str, object] | None:
        sent.append(body)

        return {}

    transport = make_transport("https://hook.invalid/x", "t", lambda _: None, lambda: 0.0, post)
    decision = transport("release", GATE, _summary(), 900.0)

    assert decision.verdict is Verdict.DENIED
    assert sent == []


def test_deny_all_still_names_the_gate_it_was_asked_about() -> None:
    """The fail-closed transport keeps its contract under the new shape."""
    decision = deny_all(action_id_of(REQUEST_ID), GATE, _summary(), 900.0)

    assert decision == Decision(Verdict.DENIED, GATE)


def test_the_flow_pattern_here_matches_the_one_the_old_path_asserts() -> None:
    """One rule, two copies. A drift between them is a dropped tap."""
    mirrored = re.compile(r"^AGENT_(APPROVE|DENY)_([0-9a-z]{26})_([0-9a-f]{16})$")

    assert ACTION_RE.pattern == mirrored.pattern
