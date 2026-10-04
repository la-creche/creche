"""Which sandbox serves a terminal, read from `caregiver`'s status document.

Contract 05 §2: `caregiver` writes
`/srv/agents/state/rework/families/<family>/status.json` and is its only
writer. A reader lists that directory to find every family, so no index
exists and no index can go stale.

The door needs two values before it can exec: the sandbox id and the host
path of that sandbox's `supervisor.env`. It refuses when a sandbox switch
is in progress, or when no sandbox serves.

`ready` is the only state that serves a terminal. Contract 05 §4.2 lets
`attendance` dial a `creating` sandbox because running the handshake is what
promotes it. A terminal runs no handshake: it is `sbx exec -it` into a VM
that may not have booted, so a half-built sandbox is refused instead.

The document is untrusted input. Its size is bounded before it is parsed and
every field is checked before it is read (invariants 12 and 14).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .errors import DoorError, Exit
from .ids import is_family, is_sandbox, sandbox_number
from .untrusted import as_list, as_object, field_text, is_object

STATUS_FILE = "status.json"

#: Contract 02 §3.1. The `door-tui` token reaches attended families only.
ATTENDED = "attended"

#: Contract 05 §4.2. The one sandbox state that serves a terminal.
STATE_READY = "ready"
#: A sandbox in this state proves a replacement exists (contract 05 §4.2).
STATE_DRAINING = "draining"

#: Contract 05 §3.4's steps that create, swap or destroy a sandbox.
_SWITCH_STEPS = frozenset({"create_sandbox", "switch_sandbox", "destroy_sandbox"})

#: Contract 05 §2 rule 5. Older than this, `caregiver` is not running.
STALE_AFTER_S = 90

# A status document is small. A larger file is a fault, not a document.
_MAX_STATUS_BYTES = 256 * 1024


@dataclass(frozen=True)
class Serving:
    """The sandbox a terminal will exec into, and anything odd about it."""

    sandbox: str
    playpen_env: str
    #: One line for the operator when the family is not fully healthy. Empty
    #: when nothing is wrong. It never stops the terminal.
    warning: str = ""


class FamilyDirectory(Protocol):
    """Where the door learns which families exist and which sandbox serves."""

    def attended(self) -> list[str]:
        """Attended family names, sorted. Contract 02 §3.1."""
        ...

    def serving(self, family: str) -> Serving:
        """The sandbox that serves this family, or raise `DoorError`."""
        ...


class StatusFiles:
    """Reads the status documents `caregiver` publishes."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def attended(self) -> list[str]:
        try:
            entries = sorted(entry for entry in self._root.iterdir() if entry.is_dir())
        except OSError:
            # No directory means no family is configured yet. An empty list is
            # the honest answer, and the caller says so in its own words.
            return []

        return [entry.name for entry in entries if self._is_attended(entry.name)]

    def serving(self, family: str) -> Serving:
        document = self._require(family)
        _check_kind(family, document)
        _check_never_valid(family, document)
        _check_switch(family, document)

        return Serving(*_ready_sandbox(family, document), warning=_warning(document))

    def _is_attended(self, family: str) -> bool:
        if not is_family(family):
            return False

        document = self._read(family)

        return document is not None and field_text(document, "kind") == ATTENDED

    def _require(self, family: str) -> dict[str, object]:
        if not is_family(family):
            raise DoorError(
                Exit.BAD_USAGE,
                f"{family!r} is not a family name: [a-z][a-z0-9-]{{1,30}}.",
            )

        document = self._read(family)

        if document is None:
            raise DoorError(
                Exit.NO_SANDBOX,
                f"no readable status document for family {family} under {self._root}. "
                "caregiver publishes it; check that caregiver runs and the family exists.",
            )

        return document

    def _read(self, family: str) -> dict[str, object] | None:
        path = self._root / family / STATUS_FILE

        try:
            if path.stat().st_size > _MAX_STATUS_BYTES:
                raise DoorError(Exit.NO_SANDBOX, f"{path} is too large to be a status document.")

            raw = path.read_bytes()
        except OSError:
            return None

        try:
            parsed: object = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            # ValueError covers bytes that are not UTF-8, text that is not
            # JSON and a number past the digit limit of the interpreter. A
            # document that nests too deep raises RecursionError.
            return None

        return parsed if is_object(parsed) else None


def _check_kind(family: str, document: dict[str, object]) -> None:
    kind = field_text(document, "kind")

    if kind == ATTENDED:
        return

    raise DoorError(
        Exit.BAD_USAGE,
        f"family {family} is {kind or 'of no stated kind'}, and a terminal "
        "reaches an attended family only (contract 02 §3.1).",
    )


def _check_never_valid(family: str, document: dict[str, object]) -> None:
    validation = as_object(document.get("validation"))

    if validation.get("never_valid") is not True:
        return

    raise DoorError(
        Exit.NO_SANDBOX,
        f"no revision of family {family} has ever validated, so nothing can serve it.",
    )


def _check_switch(family: str, document: dict[str, object]) -> None:
    """Refuse while a sandbox is being replaced.

    A terminal that started here would attach to a sandbox `caregiver` is
    about to tear down, and pi would die mid-sentence with nothing on either
    side able to say why.
    """
    reconcile = as_object(document.get("reconcile"))
    step = field_text(reconcile, "step")

    if reconcile.get("needs_switch") is True or step in _SWITCH_STEPS:
        raise DoorError(
            Exit.NO_SANDBOX,
            f"family {family} is switching sandboxes (step {step or 'unknown'}). "
            "Wait for the switch to finish, then run this again.",
        )

    draining = [box for box in _sandboxes(document) if field_text(box, "state") == STATE_DRAINING]

    if draining:
        raise DoorError(
            Exit.NO_SANDBOX,
            f"family {family} is switching sandboxes: "
            f"{field_text(draining[0], 'id')} is draining. Wait, then run this again.",
        )


def _ready_sandbox(family: str, document: dict[str, object]) -> tuple[str, str]:
    """The newest `ready` sandbox and its `supervisor.env` path."""
    rows = [box for box in _sandboxes(document) if is_sandbox(field_text(box, "id"))]
    ready = [box for box in rows if field_text(box, "state") == STATE_READY]

    if not ready:
        states = ", ".join(sorted({field_text(box, "state") or "?" for box in rows})) or "none"
        raise DoorError(
            Exit.NO_SANDBOX,
            f"no sandbox of family {family} is ready (states: {states}). "
            "A terminal runs no handshake, so only a ready sandbox serves one.",
        )

    # `N` is monotonic and never reused (contract 05 §4.1), so the highest
    # number is the newest sandbox.
    newest = max(ready, key=lambda box: sandbox_number(field_text(box, "id")))
    sandbox = field_text(newest, "id")
    env_path = field_text(newest, "supervisor_env")

    if not env_path:
        raise DoorError(
            Exit.NO_SANDBOX,
            f"sandbox {sandbox} publishes no supervisor.env path. Contract 05 §4.1.1 "
            "calls that a fault: without it the terminal has no mounts.",
        )

    return sandbox, env_path


def _sandboxes(document: dict[str, object]) -> list[dict[str, object]]:
    return [as_object(box) for box in as_list(document.get("sandboxes")) if is_object(box)]


def _warning(document: dict[str, object]) -> str:
    """What is odd about this family, said in one line. Never a refusal.

    A fault that blocks TURNS does not block a terminal. `attendance` refuses a
    turn with `family_degraded`, and a human at a keyboard is exactly who
    wants a terminal then. So the door says what is wrong and opens it.
    """
    parts: list[str] = []
    blocking = [
        field_text(fault, "code")
        for fault in _faults(document)
        if fault.get("blocks_turns") is True
    ]

    if blocking:
        parts.append(f"turns are blocked by {', '.join(blocking)}")

    if _is_stale(field_text(document, "written_at")):
        parts.append(f"the status document is stale (over {STALE_AFTER_S}s old)")

    return "; ".join(parts)


def _faults(document: dict[str, object]) -> list[dict[str, object]]:
    return [as_object(one) for one in as_list(document.get("faults")) if is_object(one)]


def _is_stale(written_at: str) -> bool:
    if not written_at:
        return True

    try:
        moment = datetime.fromisoformat(written_at.replace("Z", "+00:00"))
    except ValueError:
        return True

    # CONTRACT-QUESTION: contract 05 §2.1 gives `written_at` as RFC 3339 and
    # does not say what a reader does with a time that has no UTC offset.
    # This reader takes it as no time, so the document reads as stale and
    # the door warns. `attendance` and the noticeboard read such a time as
    # UTC. To read it as UTC here removes the warning for a document that
    # is not RFC 3339.
    if moment.tzinfo is None:
        return True

    return (datetime.now(UTC) - moment).total_seconds() > STALE_AFTER_S
