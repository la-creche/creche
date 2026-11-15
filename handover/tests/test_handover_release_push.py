"""The text of the two phone pushes of a release (`executor/phone.py`).

The operator decided each text. The approval push shows the moves of the
set and the components that have no automatic rollback. The outcome push
shows `All succeeded`, or the place of the end and each component that the
restore put back. Each arrow is ASCII.
"""

from __future__ import annotations

from typing import Final

from handover.executor.approval import Move, Summary, action_id_of
from handover.executor.ledger import Entry, Outcome, Step, StepName, StepStatus
from handover.executor.notice import notice_of
from handover.executor.phone import (
    MESSAGE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    make_notifier,
    make_transport,
)

REQUEST_ID: Final = "01K5J8M2Q7V3X9R4T6N0B8C2DE"
GATE: Final = "4f2a8c31b09d6e57"

CAREGIVER: Final = Move("caregiver", "0.1.90", "0.1.94")
VMM: Final = Move("vmm", "0.1.17", "0.1.19")


def _summary(moves: tuple[Move, ...], unsafe: tuple[str, ...] = ()) -> Summary:
    """The seven fields of the record do not reach the push."""
    return Summary(
        review="safe: 2 component(s), binary/venv",
        components="record only",
        contracts="6 contracts satisfied",
        restarts="creche-vmm.service",
        restore="automatic",
        requested_by="creche",
        manifest="6b84c0e5aa10",
        moves=moves,
        unsafe=unsafe,
    )


def _pushed(summary: Summary) -> dict[str, object]:
    """The body of the approval push for `summary`."""
    bodies: list[dict[str, object]] = []

    def post(body: dict[str, object]) -> dict[str, object] | None:
        bodies.append(body)

        return {"gate": GATE, "decision": "deny", "at": 1.0}

    transport = make_transport("https://hook.invalid/x", "t", lambda _: None, lambda: 0.0, post)
    transport(action_id_of(REQUEST_ID), GATE, summary, 900.0)

    return bodies[0]


# -- the approval push ----------------------------------------------------


def test_the_approval_push_has_the_text_of_the_operator() -> None:
    body = _pushed(_summary((VMM, CAREGIVER)))
    message = "Components: caregiver 0.1.90 -> 0.1.94, vmm 0.1.17 -> 0.1.19\nUnsafe: none"

    assert body == {
        "kind": "approval",
        "family": action_id_of(REQUEST_ID),
        "label": "release",
        "gate": GATE,
        "title": "Release Approval",
        "message": message,
        "summary": message,
    }


def test_a_first_install_moves_from_absent() -> None:
    summary = _summary((Move("disks", None, "0.1.0"),))

    assert summary.message() == "Components: disks absent -> 0.1.0\nUnsafe: none"


def test_the_unsafe_line_names_manual_and_unverified_in_name_order() -> None:
    summary = _summary((VMM, CAREGIVER), unsafe=("vmm", "noticeboard", "vmm"))

    assert summary.message().splitlines()[1] == "Unsafe: noticeboard, vmm"


def test_the_summary_is_the_message_cut_to_200() -> None:
    moves = tuple(Move(f"component-{n:02d}", "0.1.0", "0.2.0") for n in range(12))
    body = _pushed(_summary(moves))

    assert len(str(body["message"])) > SUMMARY_MAX_CHARS
    assert body["summary"] == str(body["message"])[:SUMMARY_MAX_CHARS]


def test_the_message_is_cut_to_900_and_the_title_to_60() -> None:
    moves = tuple(Move(f"component-{n:02d}", "10.10.10", "10.10.11") for n in range(40))
    summary = _summary(moves)
    body = _pushed(summary)

    assert len(summary.message()) > MESSAGE_MAX_CHARS
    assert body["message"] == summary.message()[:MESSAGE_MAX_CHARS]
    assert len(str(body["title"])) <= TITLE_MAX_CHARS


# -- the outcome push -----------------------------------------------------


def _entry(status: Outcome, *steps: tuple[StepName, StepStatus]) -> Entry:
    entry = Entry(id=REQUEST_ID, kind="release", requested_by="creche", status=status)
    for name, state in steps:
        entry.add(Step(str(name), str(state), 0.0, None))

    return entry


def _outcome_body(entry: Entry) -> dict[str, object]:
    bodies: list[dict[str, object]] = []

    def post(body: dict[str, object]) -> dict[str, object] | None:
        bodies.append(body)

        return {}

    make_notifier("https://hook.invalid/x", "t", post)(notice_of(entry))

    return bodies[0]


def test_a_success_says_all_succeeded() -> None:
    entry = _entry(Outcome.SUCCEEDED, (StepName.SWITCH, StepStatus.OK))

    assert _outcome_body(entry) == {
        "kind": "release-outcome",
        "family": "release",
        "label": "release",
        "title": "Release Result",
        "message": "All succeeded",
        "summary": "All succeeded",
        "fields": {"id": REQUEST_ID},
    }


def test_a_failed_switch_names_its_component_and_the_restore() -> None:
    entry = _entry(
        Outcome.RESTORED,
        (StepName.STAGE, StepStatus.OK),
        (StepName.SWITCH, StepStatus.FAILED),
        (StepName.RESTORE, StepStatus.OK),
    )
    entry.failed_component = "vmm"
    entry.rolled_back = ["vmm", "caregiver"]

    assert notice_of(entry).message == (
        "Release failed at vmm release.\nRolled back components: vmm, caregiver"
    )


def test_a_refusal_before_any_swap_names_its_step() -> None:
    entry = _entry(
        Outcome.REFUSED,
        (StepName.INTAKE, StepStatus.OK),
        (StepName.RESOLVE, StepStatus.OK),
        (StepName.PROVENANCE, StepStatus.REFUSED),
    )

    assert notice_of(entry).message == (
        "Release failed at provenance step.\nRolled back components: none"
    )


def test_a_denied_gate_names_the_approve_step() -> None:
    entry = _entry(
        Outcome.REFUSED,
        (StepName.CONTRACTS, StepStatus.OK),
        (StepName.APPROVE, StepStatus.REFUSED),
    )

    assert _outcome_body(entry)["message"] == (
        "Release failed at approve step.\nRolled back components: none"
    )


def test_a_failed_restore_still_names_the_step_that_ended_the_release() -> None:
    entry = _entry(
        Outcome.FAILED,
        (StepName.LOCK, StepStatus.FAILED),
        (StepName.RESTORE, StepStatus.SKIPPED),
    )

    assert notice_of(entry).message.splitlines()[0] == "Release failed at lock step."


def test_the_outcome_message_is_cut_to_900() -> None:
    entry = _entry(Outcome.RESTORED, (StepName.SWITCH, StepStatus.FAILED))
    entry.failed_component = "vmm"
    entry.rolled_back = [f"component-{n:02d}" for n in range(80)]
    body = _outcome_body(entry)

    assert len(notice_of(entry).message) > MESSAGE_MAX_CHARS
    assert body["message"] == notice_of(entry).message[:MESSAGE_MAX_CHARS]
    assert body["summary"] == notice_of(entry).message[:SUMMARY_MAX_CHARS]
