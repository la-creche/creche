"""The `_meta` keys of a job equal the facade's vector file (phase 2 plan (d))."""

from __future__ import annotations

from attendance.acp_jobs import META_DELEGATION, META_JOB, META_TRIGGER
from attendance.acp_link import METHOD_NEW_SESSION, METHOD_PROMPT
from attendance.clock import parse_rfc3339
from attendance.dispatch import DispatchEntry
from creche_vectors import session_meta


def test_each_meta_key_and_its_method_are_the_vector_files() -> None:
    table = session_meta()
    sent = {
        "job": (META_JOB, METHOD_NEW_SESSION),
        "delegation": (META_DELEGATION, METHOD_PROMPT),
        "trigger": (META_TRIGGER, METHOD_PROMPT),
    }

    # `created_at` is the one key that the facade sends and attendance does not.
    assert set(table) == {"about", "info_update", "created_at", *sent}
    assert {role: (table[role]["key"], table[role]["method"]) for role in sent} == sent


def test_the_vector_trigger_is_a_dispatch_entrys_trigger() -> None:
    """The facade checks the shape that `DispatchEntry.trigger()` sends."""
    table = session_meta()
    value = table["trigger"]["value"]
    fired_at = parse_rfc3339(value["fired_at"])
    assert fired_at is not None

    entry = DispatchEntry(
        session=table["job"]["value"],
        caller_family=value["name"],
        family=value["chain"][-1],
        enqueued_at=fired_at,
        delegation_id=table["delegation"]["value"]["id"],
        chain=tuple(value["chain"]),
    )

    assert entry.trigger().to_api() == value
