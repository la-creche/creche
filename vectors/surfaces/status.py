"""The status document (contract 05): one file, read by five packages.

`caregiver` writes one `status.json` per family. `attendance`, the
noticeboard and the three doors each read it with code of their own. Every
reader here gets the same documents, so a difference between two readers is
a difference between two vector files on one vector id.

Each surface writes the input to `<root>/families/<family>/status.json` in
a temporary directory and calls the reader's public entry point.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from agent_door_owui import families as owui_families
from agent_door_trigger import families as trigger_families
from agent_door_tui import status as tui_status
from agent_door_tui.errors import DoorError
from attendance.family_status import StatusReader

from noticeboard import statusdocs
from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    quiet_logs,
    raised,
    refused,
)

CONTRACT: Final = "contract 05"

FAMILY: Final = "chat"
FAMILIES_DIR: Final = "families"
STATUS_FILE: Final = "status.json"

#: A time no clock reaches, so a reader that asks the real clock never
#: calls the document stale.
FRESH: Final = "2999-01-01T00:00:00Z"
#: A time every clock is past, so such a reader always calls it stale.
OLD: Final = "2020-01-01T00:00:00Z"
#: The `now` the noticeboard is given: 30 seconds after `FRESH`.
NOW: Final = datetime(2999, 1, 1, 0, 0, 30, tzinfo=UTC)

#: What stands for the temporary directory in a message that names it.
ROOT_MARK: Final = "<root>"

HUGE_DIGITS: Final = 5_000

_STATE_DIR: Final = "/srv/agents/state/rework/families/chat"
_IMAGE: Final = "registry.example/playpen@sha256:" + "0123456789abcdef" * 4


def _env_path(name: str) -> str:
    return f"{_STATE_DIR}/sandboxes/{name}/supervisor.env"


def _sandbox(number: int, state: object = "ready", **fields: object) -> dict[str, object]:
    """One sandbox row, with the three fields every reader looks at."""
    name = f"{FAMILY}-s{number}"

    return {"id": name, "state": state, "supervisor_env": _env_path(name), **fields}


def _fault(code: object, *, blocks: bool, **fields: object) -> dict[str, object]:
    return {"code": code, "blocks_turns": blocks, **fields}


def _validation(**fields: object) -> dict[str, object]:
    return {"ok": True, "never_valid": False, **fields}


def _credentials(**fields: object) -> dict[str, object]:
    return {"epoch": 3, **fields}


def _body(**fields: object) -> dict[str, object]:
    """A small status document of an attended family that every reader serves.

    It holds the fields a reader decides on, and no other. `_full_body` is
    the whole document.
    """
    return {
        "family": FAMILY,
        "kind": "attended",
        "state": "in_sync",
        "written_at": FRESH,
        "config_rev": "cfg-000007",
        "sandboxes": [_sandbox(1)],
        "credentials": _credentials(),
        **fields,
    }


def _full_body(**fields: object) -> dict[str, object]:
    """Every field `caregiver` writes (contract 05 §2.1)."""
    sandbox = _sandbox(
        1,
        power="running",
        image=_IMAGE,
        spec_hash="0123456789abcdef",
        cpus=2,
        memory="2g",
        created_at=FRESH,
        ready_at=FRESH,
        channel="open",
    )

    return {
        "family": FAMILY,
        "kind": "attended",
        "state": "in_sync",
        "written_at": FRESH,
        "registry_rev": "reg-9f21c4",
        "applied_rev": "reg-9f21c4",
        "config_rev": "cfg-000007",
        "validation": {
            "rev": "reg-9f21c4",
            "checked_at": FRESH,
            "ok": True,
            "never_valid": False,
            "error_count": 0,
            "warning_count": 1,
            "report_path": f"{_STATE_DIR}/validation.json",
            "first_error": None,
        },
        "faults": [],
        "reconcile": None,
        "sandboxes": [sandbox],
        "credentials": {
            "epoch": 3,
            "key_id": "key-0001",
            "token_id": "token-0001",
            "rotated_at": FRESH,
            "next_rotation_at": None,
            "rotation_state": "settled",
        },
        "spend": {
            "spend_usd": 1.25,
            "budget_usd": 10.0,
            "window": "day",
            "source": "litellm",
            "as_of": FRESH,
        },
        "limits": {"max_running_turns": None, "max_queued_turns": 100, "job_timeout_s": None},
        "triggers": {"webhooks": [], "enqueue": False},
        **fields,
    }


def _encode(body: dict[str, object]) -> bytes:
    return json.dumps(body).encode("utf-8")


@dataclass(frozen=True)
class Document:
    """One status document: an id and its bytes."""

    id: str
    raw: bytes

    def given(self) -> dict[str, Json]:
        return bytes_input(self.raw)


def _doc(doc_id: str, **fields: object) -> Document:
    return Document(doc_id, _encode(_body(**fields)))


def _without(doc_id: str, *names: str) -> Document:
    body = _body()
    for name in names:
        del body[name]

    return Document(doc_id, _encode(body))


_TEXT: Final = json.dumps(_body())
_SPEND_NOT_FINITE: Final = ', "spend": {"spend_usd": NaN, "budget_usd": Infinity}}'

DOCUMENTS: Final[tuple[Document, ...]] = (
    # --- whole documents ----------------------------------------------------
    Document("full-attended", _encode(_full_body())),
    Document(
        "full-autonomous",
        _encode(
            _full_body(
                kind="autonomous",
                limits={"max_running_turns": 1, "max_queued_turns": 100, "job_timeout_s": None},
                triggers={"webhooks": [], "enqueue": True},
            )
        ),
    ),
    Document(
        "full-thin",
        _encode(
            _full_body(
                kind="thin",
                limits={"max_running_turns": None, "max_queued_turns": 100, "job_timeout_s": 120},
            )
        ),
    ),
    _doc("small-attended"),
    _doc("small-autonomous", kind="autonomous"),
    _doc("small-thin", kind="thin"),
    Document("empty-object", b"{}"),
    Document("only-kind", b'{"kind":"attended"}'),
    # --- kind and state ---
    _doc("kind-unknown", kind="robot"),
    _doc("kind-upper", kind="Attended"),
    _doc("kind-number", kind=5),
    _without("kind-missing", "kind"),
    _doc("state-reconciling", state="reconciling"),
    _doc("state-invalid", state="invalid"),
    _doc("state-degraded", state="degraded"),
    _doc("state-unknown-word", state="sleeping"),
    _doc("state-display-form", state="in sync"),
    _without("state-missing", "state"),
    _doc("family-other-name", family="code"),
    _doc("family-number", family=5),
    # --- validation ---
    _doc(
        "never-valid",
        state="invalid",
        sandboxes=[],
        validation=_validation(ok=False, never_valid=True, error_count=2, first_error="name: bad"),
    ),
    _doc("never-valid-in-sync", validation=_validation(never_valid=True)),
    _doc("never-valid-text", state="invalid", validation=_validation(never_valid="true")),
    _doc("never-valid-one", state="invalid", validation=_validation(never_valid=1)),
    _doc(
        "never-valid-needs-switch",
        state="invalid",
        validation=_validation(ok=False, never_valid=True),
        reconcile={"step": "validate", "attempts": 1, "needs_switch": True},
    ),
    _doc(
        "invalid-with-last-good",
        state="invalid",
        validation=_validation(ok=False, error_count=1, first_error="kind: 'bogus' is not a kind"),
    ),
    _doc("validation-null", validation=None),
    _doc("validation-list", validation=[True]),
    _doc("validation-wrong-types", validation=_validation(error_count="2", ok="yes", rev=7)),
    # --- the same, on an autonomous family ---
    _doc(
        "auto-never-valid",
        kind="autonomous",
        state="invalid",
        sandboxes=[],
        validation=_validation(ok=False, never_valid=True),
    ),
    _doc("auto-never-valid-in-sync", kind="autonomous", validation=_validation(never_valid=True)),
    _doc(
        "auto-invalid-with-last-good",
        kind="autonomous",
        state="invalid",
        validation=_validation(ok=False),
    ),
    _doc("auto-validation-list", kind="autonomous", state="invalid", validation=[True]),
    _doc("auto-no-sandbox", kind="autonomous", sandboxes=[]),
    _doc(
        "auto-fault-blocking",
        kind="autonomous",
        state="degraded",
        faults=[_fault("grants_stale", blocks=True)],
    ),
    _doc("auto-written-old", kind="autonomous", written_at=OLD),
    # --- written_at ---
    _doc("written-old", written_at=OLD),
    _without("written-missing", "written_at"),
    _doc("written-empty", written_at=""),
    _doc("written-not-a-time", written_at="yesterday"),
    _doc("written-offset", written_at="2999-01-01T02:00:00+02:00"),
    _doc("written-lower-z", written_at="2999-01-01T00:00:00z"),
    _doc("written-fraction", written_at="2999-01-01T00:00:00.123456Z"),
    _doc("written-space", written_at="2999-01-01 00:00:00Z"),
    _doc("written-number", written_at=32472144000),
    _doc("written-two-z", written_at="2999-01-01T00:00:00ZZ"),
    # --- sandboxes ---
    _doc("sandboxes-empty", sandboxes=[]),
    _without("sandboxes-missing", "sandboxes"),
    _doc("sandboxes-object", sandboxes={"chat-s1": "ready"}),
    _doc("sandbox-creating", sandboxes=[_sandbox(1, "creating", ready_at=None)]),
    _doc("sandbox-planned", sandboxes=[_sandbox(1, "planned", ready_at=None)]),
    _doc("sandbox-failed", sandboxes=[_sandbox(1, "failed")]),
    _doc("sandbox-unknown-state", sandboxes=[_sandbox(1, "sleeping")]),
    _doc("sandbox-state-upper", sandboxes=[_sandbox(1, "READY")]),
    _doc("sandbox-state-number", sandboxes=[_sandbox(1, state=1)]),
    _doc("sandbox-draining-and-ready", sandboxes=[_sandbox(1, "draining"), _sandbox(2)]),
    _doc("sandbox-two-ready", sandboxes=[_sandbox(2), _sandbox(10), _sandbox(9)]),
    _doc("sandbox-ready-and-creating", sandboxes=[_sandbox(3), _sandbox(4, "creating")]),
    _doc("sandbox-id-upper", sandboxes=[_sandbox(1, id="Chat-s1")]),
    _doc("sandbox-id-no-number", sandboxes=[_sandbox(1, id="chat")]),
    _doc("sandbox-id-other-family", sandboxes=[_sandbox(1, id="code-s1")]),
    _doc("sandbox-id-number", sandboxes=[_sandbox(1, id=1)]),
    _doc("sandbox-id-leading-zero", sandboxes=[_sandbox(1, id="chat-s007"), _sandbox(6)]),
    _doc("sandbox-env-empty", sandboxes=[_sandbox(1, supervisor_env="")]),
    _doc("sandbox-env-number", sandboxes=[_sandbox(1, supervisor_env=5)]),
    _doc("sandbox-env-relative", sandboxes=[_sandbox(1, supervisor_env="../supervisor.env")]),
    _doc("sandbox-not-objects", sandboxes=["chat-s1", None, 5, [_sandbox(1)]]),
    _doc("sandbox-21", sandboxes=[_sandbox(number, "gone") for number in range(1, 22)]),
    _doc(
        "sandbox-22-first-not-object",
        sandboxes=["chat-s0", *(_sandbox(number, "gone") for number in range(1, 22))],
    ),
    _doc("sandbox-wrong-types", sandboxes=[_sandbox(1, cpus="2", memory=2, image=None, power=7)]),
    # --- faults ---
    _doc("fault-blocking", state="degraded", faults=[_fault("grants_stale", blocks=True)]),
    _doc("fault-not-blocking", state="degraded", faults=[_fault("pep_unreachable", blocks=False)]),
    _doc(
        "fault-two",
        state="degraded",
        faults=[_fault("pep_unreachable", blocks=False), _fault("sandbox_lost", blocks=True)],
    ),
    _doc(
        "fault-blocks-text",
        state="degraded",
        faults=[_fault("grants_stale", blocks=True, blocks_turns="true")],
    ),
    _doc("fault-no-code", state="degraded", faults=[{"blocks_turns": True}]),
    _doc("fault-code-number", state="degraded", faults=[_fault(5, blocks=True)]),
    _doc("faults-object", faults={"grants_stale": True}),
    _doc("faults-not-objects", faults=["grants_stale", None]),
    _doc(
        "faults-21",
        state="degraded",
        faults=[_fault(f"fault_{number}", blocks=False) for number in range(21)],
    ),
    _doc(
        "faults-22-first-not-object",
        state="degraded",
        faults=[None, *(_fault(f"fault_{number}", blocks=False) for number in range(21))],
    ),
    # --- credentials, limits, triggers ---
    _doc("epoch-zero", credentials=_credentials(epoch=0)),
    _doc("epoch-negative", credentials=_credentials(epoch=-1)),
    _doc("epoch-true", credentials=_credentials(epoch=True)),
    _doc("epoch-float", credentials=_credentials(epoch=3.0)),
    _doc("epoch-text", credentials=_credentials(epoch="3")),
    _doc("epoch-past-64-bits", credentials=_credentials(epoch=2**70)),
    _doc("credentials-null", credentials=None),
    _doc("credentials-list", credentials=[3]),
    _doc("limits-negative", limits={"max_running_turns": -1, "job_timeout_s": -5}),
    _doc("limits-true", limits={"max_running_turns": True, "job_timeout_s": False}),
    _doc("limits-zero", limits={"max_running_turns": 0, "job_timeout_s": 0, "max_queued_turns": 0}),
    _doc("limits-text", limits={"max_running_turns": "1", "job_timeout_s": "120"}),
    _doc("limits-null", limits=None),
    _doc("triggers-enqueue-text", kind="autonomous", triggers={"enqueue": "true"}),
    _doc("triggers-enqueue-one", kind="autonomous", triggers={"enqueue": 1}),
    _doc("triggers-null", kind="autonomous", triggers=None),
    # --- reconcile and spend ---
    _doc(
        "reconcile-needs-switch",
        state="reconciling",
        reconcile={
            "since": FRESH,
            "from_rev": "reg-000001",
            "to_rev": "reg-9f21c4",
            "step": "validate",
            "attempts": 1,
            "needs_switch": True,
        },
    ),
    _doc(
        "reconcile-switch-step",
        state="reconciling",
        reconcile={"step": "switch_sandbox", "attempts": 2, "needs_switch": False},
    ),
    _doc(
        "reconcile-other-step",
        state="reconciling",
        reconcile={"step": "write_grants", "attempts": 1, "needs_switch": False},
    ),
    _doc(
        "reconcile-create-step",
        state="reconciling",
        reconcile={"step": "create_sandbox", "attempts": 1, "needs_switch": False},
    ),
    _doc(
        "reconcile-destroy-step",
        state="reconciling",
        reconcile={"step": "destroy_sandbox", "attempts": 1, "needs_switch": False},
    ),
    _doc(
        "reconcile-timers-step",
        state="reconciling",
        reconcile={"step": "write_timers", "attempts": 1, "needs_switch": False},
    ),
    _doc("reconcile-wrong-types", reconcile={"step": 5, "attempts": "1", "needs_switch": "yes"}),
    _doc("reconcile-list", reconcile=["switch_sandbox"]),
    _doc(
        "spend-known",
        spend={
            "spend_usd": 1.25,
            "budget_usd": 10,
            "window": "day",
            "source": "litellm",
            "as_of": FRESH,
        },
    ),
    _doc(
        "spend-old",
        spend={"spend_usd": 1.25, "budget_usd": 10.0, "window": "day", "as_of": OLD},
    ),
    _doc(
        "spend-as-old-as-document",
        written_at=OLD,
        spend={"spend_usd": 1.25, "budget_usd": 10.0, "window": "day", "as_of": OLD},
    ),
    _doc("spend-wrong-types", spend={"spend_usd": "1.25", "budget_usd": True, "as_of": 5}),
    _doc("spend-list", spend=[1.25, 10]),
    Document("spend-nan", _TEXT[:-1].encode() + _SPEND_NOT_FINITE.encode()),
    # --- the bytes and the JSON ---
    Document("bytes-empty", b""),
    Document("bytes-not-utf8", b'{"kind":"attended\xff"}'),
    Document("bytes-bom", b"\xef\xbb\xbf" + _TEXT.encode("utf-8")),
    Document("json-text", b"not json"),
    Document("json-truncated", _TEXT.encode("utf-8")[:-1]),
    Document("json-trailing-text", _TEXT.encode("utf-8") + b" x"),
    Document("json-top-array", b"[" + _TEXT.encode("utf-8") + b"]"),
    Document("json-top-null", b"null"),
    Document("json-top-text", b'"in_sync"'),
    Document(
        "json-duplicate-kind",
        _TEXT.replace('"kind": "attended"', '"kind": "thin", "kind": "attended"').encode(),
    ),
    Document(
        "json-huge-integer",
        _TEXT.replace('"epoch": 3', '"epoch": ' + "9" * HUGE_DIGITS).encode(),
    ),
    Document(
        "json-deep-unknown-field",
        _TEXT[:-1].encode() + b', "x": ' + b"[" * 200 + b"]" * 200 + b"}",
    ),
)


@dataclass(frozen=True)
class Reader:
    """One package's reader of the status document."""

    owner: str
    entry: str
    #: Reads the family's document under the root, and answers a vector body.
    read: Callable[[Path], tuple[bool, object]]
    notes: tuple[str, ...] = ()


def _read_attendance(root: Path) -> tuple[bool, object]:
    status = StatusReader(root).read(FAMILY)

    return status is not None, status


def _read_noticeboard(root: Path) -> tuple[bool, object]:
    row = statusdocs.read_family(root / FAMILIES_DIR, FAMILY, NOW)

    return not row.problem, row


def _read_door_tui(root: Path) -> tuple[bool, object]:
    try:
        serving = tui_status.StatusFiles(root / FAMILIES_DIR).serving(FAMILY)
    except DoorError as exc:
        message = exc.message.replace(str(root), ROOT_MARK)
        return False, {"exit": exc.code.name.lower(), "message": message}

    return True, serving


def _read_door_trigger(root: Path) -> tuple[bool, object]:
    return FAMILY in trigger_families.StatusFiles(root / FAMILIES_DIR).servable(), None


def _read_door_owui(root: Path) -> tuple[bool, object]:
    return FAMILY in owui_families.StatusFiles(root / FAMILIES_DIR).serving(), None


_NOTE_PATH: Final = (
    "The input is the bytes of the status document of the family chat. The generator writes "
    "it to <root>/families/chat/status.json and gives the reader that root."
)
_NOTE_CLOCK: Final = (
    "This reader asks the real clock. A written_at in the year 2999 is never stale, and one "
    "in the year 2020 is always stale."
)

READERS: Final[tuple[Reader, ...]] = (
    Reader(
        "attendance",
        "attendance.family_status.StatusReader.read",
        _read_attendance,
        (
            "value is what the reader takes from the document. A refused vector is a document "
            "that the reader does not use: it returns None.",
            "written_at is a time in UTC, or null when the reader cannot read the field.",
            "kind and state are null when the document has no word that the reader knows. "
            "The reader has no default for these two fields.",
        ),
    ),
    Reader(
        "noticeboard",
        "noticeboard.statusdocs.read_family",
        _read_noticeboard,
        (
            "The entry point takes the time now. The generator gives it "
            f"{NOW.isoformat()}, which is 30 seconds after the written_at of most documents.",
            "The reader returns a row for every document. A refused vector is a row with a "
            "problem: the reader could not parse the document. refusal is that row.",
        ),
    ),
    Reader(
        "door_tui",
        "agent_door_tui.status.StatusFiles.serving",
        _read_door_tui,
        (
            "value is the sandbox the terminal door attaches to. refusal holds the exit code "
            f"name and the message. {ROOT_MARK} in a message stands for the root directory.",
            _NOTE_CLOCK,
        ),
    ),
    Reader(
        "door_trigger",
        "agent_door_trigger.families.StatusFiles.servable",
        _read_door_trigger,
        ("accepted means that the reader lists the family as one a trigger may start.",),
    ),
    Reader(
        "door_owui",
        "agent_door_owui.families.StatusFiles.serving",
        _read_door_owui,
        ("accepted means that the reader lists the family in the model picker.",),
    ),
)


def _vector(reader: Reader, document: Document, scratch: Path) -> Vector:
    given = document.given()
    root = scratch / reader.owner / document.id
    target = root / FAMILIES_DIR / FAMILY / STATUS_FILE
    target.parent.mkdir(parents=True)
    target.write_bytes(document.raw)
    with quiet_logs():
        outcome = attempt(lambda: reader.read(root))

    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc)

    took, detail = outcome
    if took:
        return accepted(document.id, given, detail)

    return refused(document.id, given, detail)


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix="vectors-status-") as scratch_name:
        scratch = Path(scratch_name)

        return tuple(
            Surface(
                name=f"status.{reader.owner}",
                path=f"status/{reader.owner}.json",
                entry=reader.entry,
                contract=f"{CONTRACT} §2, §3, §4",
                notes=(_NOTE_PATH, *reader.notes),
                vectors=tuple(_vector(reader, document, scratch) for document in DOCUMENTS),
            )
            for reader in READERS
        )
