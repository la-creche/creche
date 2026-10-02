"""The delegation ids the PEP mints, and what they make trustworthy.

Contract 04 §6.3 and §7.4. The sandbox never mints a delegation id, so a
`chain` built from the PEP's own mints is evidence even though the header that
carries the id is not (§3).
"""

from __future__ import annotations

import re

from agent_pep.delegations import (
    CHAIN_TTL_S,
    MAX_DELEGATION_HOPS,
    DelegationTable,
    new_delegation_id,
)
from agent_pep.family_ids import ULID_RE

CHAT = "chat"
ORACLE = "vault-oracle"
CODE = "code-sandbox"


def test_a_minted_id_is_an_upper_case_ulid() -> None:
    minted = new_delegation_id()
    assert ULID_RE.match(minted) is not None
    assert re.match(r"^[0-9A-HJKMNP-TV-Z]{26}\Z", minted) is not None


def test_two_mints_differ() -> None:
    assert new_delegation_id() != new_delegation_id()


def test_a_top_level_call_has_a_chain_of_one() -> None:
    table = DelegationTable()
    assert table.chain_for(None, CHAT) == (CHAT,)


def test_an_unknown_id_has_a_chain_of_one() -> None:
    """A forged id names no chain, so it widens nothing (§3.1 rule 2)."""
    table = DelegationTable()
    assert table.chain_for("01K5J9QWB2M4N6Q8S0V2W4Y6A8", ORACLE) == (ORACLE,)


def test_a_minted_id_names_the_caller_first() -> None:
    table = DelegationTable()
    minted = table.mint((CHAT, ORACLE))
    assert table.chain_for(minted, ORACLE) == (CHAT, ORACLE)


def test_a_minted_id_presented_by_another_family_is_dropped() -> None:
    """The recorded chain ends in the family the PEP delegated to. Another
    family presenting that id would inherit a chain it was never in."""
    table = DelegationTable()
    minted = table.mint((CHAT, ORACLE))
    assert table.chain_for(minted, CODE) == (CODE,)


def test_hops_count_the_delegate_calls_already_taken() -> None:
    table = DelegationTable()
    assert table.hops(None, CHAT) == 0
    minted = table.mint((CHAT, ORACLE))
    assert table.hops(minted, ORACLE) == MAX_DELEGATION_HOPS


def test_a_stale_chain_is_forgotten() -> None:
    clock = _Clock()
    table = DelegationTable(clock=clock.read)
    minted = table.mint((CHAT, ORACLE))
    clock.value += CHAIN_TTL_S + 1
    # The eviction runs on the next mint, so the table cannot grow forever.
    table.mint((CHAT, CODE))
    assert table.chain_for(minted, ORACLE) == (ORACLE,)


def test_in_flight_counts_up_and_down() -> None:
    table = DelegationTable()
    assert table.inflight(CHAT) == 0
    table.begin(CHAT)
    table.begin(CHAT)
    assert table.inflight(CHAT) == 2
    table.end(CHAT)
    assert table.inflight(CHAT) == 1
    table.end(CHAT)
    assert table.inflight(CHAT) == 0


def test_in_flight_never_goes_negative() -> None:
    table = DelegationTable()
    table.end(CHAT)
    assert table.inflight(CHAT) == 0


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def read(self) -> float:
        return self.value
