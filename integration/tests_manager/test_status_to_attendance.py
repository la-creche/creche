"""Seam 4: the status document `apply_once` wrote, read by the real `attendance`.

`attendance` never calls `caregiver`. One file carries everything it knows
about a family (contract 05 §2), and `attendance`'s own tests have always
hand-written that file. These tests hand it the real one.

    apply_once ──► families/chat/status.json ──► StatusReader

The scenarios that ran a turn over the channel left with the session API
(phase 2 plan, row 21). The sandbox rows and the env file left when
`caregiver` stopped creating sandboxes (decision CH1): the vmm runs one VM
per session.
"""

from __future__ import annotations

import pytest
from attendance.family_status import FamilyState, StatusReader, check_may_serve
from attendance.states import SessionKind

from .conftest import FAMILY, Applied


@pytest.fixture
def reader(applied: Applied) -> StatusReader:
    return StatusReader(applied.state_root)


# --- the document itself -------------------------------------------------


def test_the_reader_accepts_the_document(reader: StatusReader, applied: Applied) -> None:
    status = reader.read(FAMILY)
    assert status is not None
    assert status.family == FAMILY
    assert status.kind is SessionKind.ATTENDED
    assert status.state is FamilyState.IN_SYNC
    assert status.config_rev == applied.result.status.config_rev


def test_the_document_is_not_stale_when_written(reader: StatusReader) -> None:
    status = reader.read(FAMILY)
    assert status is not None
    assert status.is_stale() is False


def test_the_family_may_serve(reader: StatusReader) -> None:
    status = reader.read(FAMILY)
    assert status is not None
    check_may_serve(status)  # contract 02 §5.1: `in_sync` is accepted


def test_the_family_appears_in_the_listing(reader: StatusReader) -> None:
    assert reader.families() == [FAMILY]


def test_a_reapply_keeps_the_document_readable(reader: StatusReader, applied: Applied) -> None:
    applied.reapply()
    status = reader.read(FAMILY)
    assert status is not None
    assert status.state is FamilyState.IN_SYNC
    assert status.sandboxes == []


# --- the env file caregiver wrote, carried by the command attendance builds --
