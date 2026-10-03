"""One family starts another family's job (packet EQ).

Stage 5 proved a trigger can start an autonomous job and a phone can hold a
tool call open. This module adds the one path neither of those covered: a
granted family calling `enqueue`, and reading back what became of the job
with `job_status` (contract 04 §4.1, contract 02 §13.4).

    chat's turn ──enqueue──► the REAL PEP ──► POST /dispatch ──► attendance
      │                          │                                 │
      │                          `── audit/<day>.jsonl             ▼
      │                              chain ["chat","scrum-lead"]  auto-<ulid>
      ▼                                                             │
    job_status ──► POST /dispatch/jobs ──► the ledger + live state  │ runs
                                                    or the outcome ◄┘

    scrum-lead's job ──enqueue──► the gate ──► the fake phone ──► tap
                                                                  │
                          issue-worker's auto-<ulid> ◄────────────'

It extends `stage5.py` by import rather than by edit. `Stage5`
already holds the stack, the real `caregiver`, the real PEP, the fake approval
transport and the bridge driver; `StageEq` changes two things and adds one.

1. Its own fixture registry, `eq-registry`, with four families: an attended
   caller, a lead that may be dispatched and may dispatch, a worker that can
   be started no other way, and one family that declares no dispatch form at
   all.
2. `serving_eq` gives the PEP a `PEP_DISPATCH_TOKEN_FILE`, which is what
   makes `enqueue` and `job_status` more than seams. There is no webhook
   listener here: nothing in this packet is fired by one.

NO STAND-IN. Contract 02 §13.4.1 rule 3 makes `attendance` refuse every dispatch
to a family whose status document lacks `triggers.enqueue: true` (contract 05
§2.1). The real `caregiver` publishes that key from the family file, and
`published_dispatch_flags` only reads it back.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Final

import pytest
from attendance.auth import Principal
from caregiver.apply import ApplyResult, apply_once
from stack import repo_root
from stage5 import (
    APPROVAL_LIMIT_S,
    DELEGATE_TOKEN_MODE,
    FIXTURE_CALLBACK_TOKEN,
    FIXTURE_HOOK_TOKEN,
    HOOK_PATH,
    IMAGE,
    Stage5,
    build_gatekeeper,
)

from caregiver import paths as caregiver_paths

#: The four families of this packet's fixture registry.
CHAT: Final = "chat"
LEAD: Final = "scrum-lead"
WORKER: Final = "issue-worker"
NO_DISPATCH: Final = "ha-review"
EQ_FAMILIES: Final = (CHAT, LEAD, WORKER, NO_DISPATCH)

#: Checked in beside this suite, like every other fixture registry.
EQ_REGISTRY_ROOT: Final = repo_root() / "integration" / "fixtures" / "eq-registry"

#: The two verbs this packet is about, as the manifest names them.
ENQUEUE: Final = "enqueue"
JOB_STATUS: Final = "job_status"

#: Contract 02 §3 rule 5: the PEP reads this one as user `pep`, so it carries
#: the group-read bit on the host. Same mode as the delegate door's.
DISPATCH_TOKEN_MODE: Final = DELEGATE_TOKEN_MODE


class StageEq(Stage5):
    """Stage 5's stack, over this packet's own four-family registry."""

    def apply_all(self) -> tuple[ApplyResult, ...]:
        """One `apply_once` per family, in the order a cold host would."""
        return tuple(self.apply_one(name) for name in EQ_FAMILIES)

    def apply_one(self, family: str) -> ApplyResult:
        """The real manager, against THIS packet's fixture registry."""
        return apply_once(
            EQ_REGISTRY_ROOT,
            family,
            state_root=self.stack.state_root,
            image=IMAGE,
            driver=self.driver,
            litellm=self.litellm,
        )

    # ------------------------------------------- what caregiver published

    def published_dispatch_flags(self) -> tuple[str, ...]:
        """The families whose status document says `triggers.enqueue: true`
        (contract 05 §2.1), exactly as the real `caregiver` wrote it.

        Contract 02 §13.4.1 rule 3 asks the TARGET's own document whether it
        may be dispatched to, because a caller's grant file cannot answer
        that about another family. This harness once wrote the key by hand:
        `caregiver` did not publish it, and every dispatch on a real host was
        refused. It publishes it now, so this only reads.
        """
        dispatchable: list[str] = []

        for family in EQ_FAMILIES:
            path = caregiver_paths.status_path(self.stack.state_root, family)
            body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            triggers: dict[str, Any] = dict(body.get("triggers") or {})

            if triggers.get("enqueue") is True:
                dispatchable.append(family)

        return tuple(dispatchable)

    # ----------------------------------------------------- what to assert on

    def open_dispatch_door(self) -> Path:
        """Put `door-dispatch.token` at the mode the host deploys it with."""
        path = self.stack.state_root / "tokens" / f"{Principal.DOOR_DISPATCH.value}.token"
        path.chmod(DISPATCH_TOKEN_MODE)

        return path

    def ledger_entries(self, caller: str) -> list[dict[str, Any]]:
        """One caller family's dispatch ledger (contract 02 §13.4.3)."""
        directory = self.stack.state_root / "dispatch" / caller

        if not directory.is_dir():
            return []

        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]

    def enqueue_audit(self, family: str) -> list[dict[str, Any]]:
        """Every audit record one family wrote for `enqueue` (contract 04 §6)."""
        return [
            line
            for line in self.audit_lines()
            if line.get("tool") == ENQUEUE and line.get("family") == family
        ]


@contextmanager
def serving_eq(
    stage: StageEq,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    limit_s: float = APPROVAL_LIMIT_S,
) -> Iterator[StageEq]:
    """The fake phone rail and the real PEP, with a dispatch door.

    The same shape as `serving_stage5`, minus the webhook listener and plus
    `PEP_DISPATCH_TOKEN_FILE`. Without that one variable the PEP's decision
    core keeps both verbs as seams and the manifest never offers them, which
    is the fail-closed answer and not what this packet is testing.
    """
    from pep_harness import build_pep, free_port, serving

    socket = stage.stack.attendance_socket

    if socket is None:
        raise AssertionError("the stack is not serving yet")

    with ExitStack() as running:
        transport_url = running.enter_context(serving(stage.transport.app, free_port()))
        hook_url = f"{transport_url}{HOOK_PATH}"

        monkeypatch.setenv("PEP_SESSIOND_SOCKET", str(socket))
        monkeypatch.setenv("PEP_DELEGATE_TOKEN_FILE", str(stage.open_delegate_door()))
        monkeypatch.setenv("PEP_DISPATCH_TOKEN_FILE", str(stage.open_dispatch_door()))
        monkeypatch.setenv("PEP_APPROVAL_URL", hook_url)
        app = build_pep(
            monkeypatch,
            tmp_path,
            stage.stack.state_root,
            secrets={
                "approval_hook_token": FIXTURE_HOOK_TOKEN,
                "approval_callback_token": FIXTURE_CALLBACK_TOKEN,
            },
            gatekeeper=build_gatekeeper(hook_url, limit_s),
        )

        stage.pep_url = running.enter_context(serving(app, free_port()))
        stage.transport.pep_url = stage.pep_url

        yield stage
