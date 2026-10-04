"""The files of contract 05 beside the five readers of `status.json`.

Five surfaces:

- `status.write`: the fields of one status document to the bytes that
  `caregiver` writes.
- `status.fault_file.attendance`: the open faults of one family to the bytes
  of the fault file that `attendance` writes.
- `status.fault_file.chaperone`: the same, for the fault file of the
  chaperone.
- `status.fault_file.caregiver`: the bytes of one fault file to what
  `caregiver` takes from it.
- `status.outcome.noticeboard`: the bytes of one outcome record to the row
  that the noticeboard shows.

A writer stamps its file with the time now. The generator gives each writer
a clock of its own, so a vector holds no time of the machine.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from unittest.mock import patch

from agent_family import FamilyState
from attendance.faults import FaultCode, FaultReporter
from attendance.models import Trigger, TriggerKind
from attendance.outcomes import Approvals, Outcome, OutcomeRecord
from attendance.paths import fault_file, outcome_file
from caregiver.chaperone_watch import PepReach, PepReport
from caregiver.faults import FaultEntry, read_fault_file
from caregiver.litellm_keys import SPEND_WINDOW
from caregiver.status import (
    ChannelState,
    CredentialsBlock,
    LimitsBlock,
    RotationState,
    SandboxLifecycle,
    SandboxPower,
    SandboxStatus,
    StatusDocument,
    TriggersBlock,
    ValidationBlock,
)
from caregiver.webhook_tokens import WebhookToken
from chaperone.faults import FaultWriter

from attendance import faults as attendance_faults
from attendance import outcomes
from caregiver import status as caregiver_status
from noticeboard import statusdocs
from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    normalize,
    quiet_logs,
    raised,
    refused,
)
from vectors.surfaces.status import CONTRACT, FAMILY, FRESH, NOW, OLD

STATE_ROOT: Final = "/srv/agents/state/rework"
_FAMILY_DIR: Final = f"{STATE_ROOT}/families/{FAMILY}"
_IMAGE: Final = "registry.example/playpen@sha256:" + "0123456789abcdef" * 4
_REV: Final = "reg-9f21c4"
_OLD_REV: Final = "reg-8c01aa"
_PEP_URL: Final = "http://192.0.2.10:8300"

SOURCE_SESSIOND: Final = "sessiond"
SOURCE_PEP: Final = "pep"
SOURCE_MANAGERD: Final = "managerd"

#: Each character class that a JSON writer treats in its own way: a letter
#: outside ASCII, a character outside the first plane, a line separator, a
#: quote, a backslash, a slash, each short escape, two control characters
#: with no short escape and the delete character.
_ODD_TEXT: Final = 'caf\u00e9 \U0001f600 \u2028 "quoted" back\\slash a/b \n\r\t\b\f \x00\x1f \x7f'


# --- status.write ---------------------------------------------------------------


def _sandbox(
    number: int, state: SandboxLifecycle = SandboxLifecycle.READY, **changes: object
) -> SandboxStatus:
    """One sandbox row as `caregiver.sandboxes.status_of` makes it."""
    name = f"{FAMILY}-s{number}"
    running = state in (SandboxLifecycle.READY, SandboxLifecycle.DRAINING)
    row = SandboxStatus(
        id=name,
        state=state,
        power=SandboxPower.RUNNING if running else SandboxPower.STOPPED,
        image=_IMAGE,
        spec_hash="0123456789abcdef",
        cpus=2,
        memory="2g",
        created_at=FRESH,
        ready_at=FRESH if running else None,
        channel=ChannelState.OPEN if running else ChannelState.CLOSED,
        playpen_env=f"{_FAMILY_DIR}/supervisor-{name}.env",
    )

    return replace(row, **changes)


def _validation(**changes: object) -> ValidationBlock:
    block = ValidationBlock(
        rev=_REV,
        checked_at=FRESH,
        ok=True,
        never_valid=False,
        error_count=0,
        warning_count=1,
        report_path=f"{_FAMILY_DIR}/validation.json",
        first_error=None,
    )

    return replace(block, **changes)


def _credentials(**changes: object) -> CredentialsBlock:
    block = CredentialsBlock(
        epoch=3,
        key_id=f"family-{FAMILY}",
        token_id=f"family-{FAMILY}",
        rotated_at=FRESH,
        next_rotation_at=None,
        rotation_state=RotationState.SETTLED,
    )

    return replace(block, **changes)


def _spend(
    spend_usd: float, budget_usd: float | None = 10.0, as_of: str = FRESH
) -> dict[str, object]:
    """The spend block, with its keys in the order `caregiver.reconcile` writes."""
    return {
        "window": SPEND_WINDOW,
        "spend_usd": spend_usd,
        "budget_usd": budget_usd,
        "as_of": as_of,
        "source": "litellm",
    }


def _reconcile(
    step: str, *, from_rev: str = _OLD_REV, attempts: int = 1, needs_switch: bool = False
) -> dict[str, object]:
    """The reconcile block, with its keys in the order `caregiver.reconcile` writes."""
    return {
        "since": FRESH,
        "from_rev": from_rev,
        "to_rev": _REV,
        "step": step,
        "attempts": attempts,
        "needs_switch": needs_switch,
    }


def _fault(
    code: str, *, blocks: bool, source: str = SOURCE_MANAGERD, stale: bool = False, **detail: object
) -> FaultEntry:
    return FaultEntry(
        code=code, blocks_turns=blocks, since=FRESH, source=source, stale=stale, detail=detail
    )


_PEP_OK: Final = PepReport(reach=PepReach.OK, url=_PEP_URL, checked_at=FRESH)
_PEP_AWAY: Final = PepReport(
    reach=PepReach.UNREACHABLE, url=_PEP_URL, checked_at=FRESH, unreachable_since=OLD
)

_SERVING: Final = StatusDocument(
    family=FAMILY,
    kind="attended",
    state=FamilyState.IN_SYNC,
    written_at=FRESH,
    registry_rev=_REV,
    applied_rev=_REV,
    config_rev=_REV,
    validation=_validation(),
    sandboxes=(_sandbox(1),),
    credentials=_credentials(),
    spend=_spend(1.25),
    chaperone=_PEP_OK,
)


def _document(**changes: object) -> StatusDocument:
    """A document of an attended family that serves, but for `changes`."""
    return replace(_SERVING, **changes)


def _degraded(*faults: FaultEntry, **changes: object) -> StatusDocument:
    return _document(state=FamilyState.DEGRADED, faults=faults, **changes)


def _spent(spend_usd: float, budget_usd: float | None) -> StatusDocument:
    return _document(spend=_spend(spend_usd, budget_usd))


@dataclass(frozen=True)
class Written:
    """One status document that `caregiver` writes."""

    id: str
    document: StatusDocument


_WEBHOOKS: Final = (
    WebhookToken(
        "boiler-alert", Path(f"{STATE_ROOT}/triggers/webhooks/{FAMILY}/boiler-alert.token")
    ),
    WebhookToken("door-bell", Path(f"{STATE_ROOT}/triggers/webhooks/{FAMILY}/door-bell.token")),
)

_RELEASE_ID: Final = "01JBQ7WZ0X4T9V6K2H8M3N5PQS"
#: A detail of a fault with each kind of JSON value in it.
_NESTED: Final[dict[str, object]] = {"empty": {}, "list": [1, [2.5, None, True], {"deep": []}]}
_HELD_LINE: Final = (
    f"root refused the release {_RELEASE_ID} of the MCP servers. weather is not installed, "
    "and no new request goes out before 2999-01-02T00:00:00Z"
)

WRITTEN: Final[tuple[Written, ...]] = (
    # --- a family that serves ------------------------------------------------
    Written("attended", _SERVING),
    Written(
        "autonomous",
        _document(
            kind="autonomous",
            limits=LimitsBlock(max_running_turns=1),
            triggers=TriggersBlock(_WEBHOOKS, enqueue=True),
        ),
    ),
    Written("thin", _document(kind="thin", limits=LimitsBlock(job_timeout_s=120))),
    Written("one-pass", _document(spend=None, chaperone=PepReport(reach=PepReach.OFF, url=""))),
    Written("pep-not-probed-yet", _document(chaperone=PepReport(reach=PepReach.OK, url=_PEP_URL))),
    Written(
        "limits-each",
        _document(limits=LimitsBlock(max_running_turns=4, max_queued_turns=7, job_timeout_s=30)),
    ),
    Written("limits-no-queue", _document(limits=LimitsBlock(max_queued_turns=None))),
    Written(
        "credentials-rotating",
        _document(
            credentials=_credentials(
                epoch=18446744073709551615,
                next_rotation_at="2999-01-31T00:00:00Z",
                rotation_state=RotationState.ROTATING,
            )
        ),
    ),
    Written(
        "credentials-failed",
        _document(credentials=_credentials(rotation_state=RotationState.FAILED)),
    ),
    Written("spend-no-budget", _spent(0.0, None)),
    # --- reconciling and invalid --------------------------------------------
    Written(
        "reconciling-switch",
        _document(
            state=FamilyState.RECONCILING,
            applied_rev=_OLD_REV,
            config_rev=_OLD_REV,
            reconcile=_reconcile("switch_sandbox", needs_switch=True),
            sandboxes=(
                _sandbox(3, SandboxLifecycle.DRAINING),
                _sandbox(4, SandboxLifecycle.CREATING),
            ),
        ),
    ),
    Written(
        "reconciling-image",
        _document(
            state=FamilyState.RECONCILING,
            reconcile=_reconcile("create_sandbox", from_rev=_REV, attempts=2),
            sandboxes=(_sandbox(1), _sandbox(2, SandboxLifecycle.PLANNED, playpen_env="")),
        ),
    ),
    Written(
        "invalid-last-good",
        _document(
            state=FamilyState.INVALID,
            applied_rev=_OLD_REV,
            config_rev=_OLD_REV,
            validation=_validation(
                ok=False, error_count=2, first_error="tools.kagi: unknown MCP server"
            ),
        ),
    ),
    Written(
        "invalid-never-valid",
        _document(
            kind="",
            state=FamilyState.INVALID,
            applied_rev="",
            config_rev="",
            validation=_validation(
                ok=False, never_valid=True, error_count=1, warning_count=0, first_error="kind: bad"
            ),
            sandboxes=(),
            credentials=None,
            spend=None,
        ),
    ),
    Written(
        "invalid-with-faults",
        _document(
            state=FamilyState.INVALID,
            applied_rev=_OLD_REV,
            config_rev=_OLD_REV,
            validation=_validation(ok=False, error_count=1, first_error="egress: bad host"),
            faults=(
                _fault("orphan_processes", blocks=False, source=SOURCE_SESSIOND, sandbox="chat-s1"),
            ),
        ),
    ),
    Written(
        "first-error-at-cap",
        _document(
            state=FamilyState.INVALID,
            validation=_validation(ok=False, error_count=1, first_error="e" * 199 + "\u00e9"),
        ),
    ),
    # --- sandboxes -------------------------------------------------------------
    Written(
        "sandbox-each-state",
        _document(
            sandboxes=tuple(
                _sandbox(number, state) for number, state in enumerate(SandboxLifecycle, start=1)
            )
        ),
    ),
    Written(
        "sandbox-channel-lost",
        _document(sandboxes=(_sandbox(1, channel=ChannelState.LOST),)),
    ),
    Written("sandbox-none", _document(sandboxes=())),
    # --- faults ----------------------------------------------------------------
    Written(
        "fault-of-caregiver",
        _degraded(
            _fault(
                "sandbox_start_failed",
                blocks=True,
                attempts=3,
                message="the sandbox did not start",
                sandbox="chat-s4",
            ),
            _fault("spend_unknown", blocks=False, message="read_spend: no answer"),
            _fault("key_mint_failed", blocks=True, message="key/generate: 500"),
            _fault("egress_assert_failed", blocks=True, message="example.com:443 is reachable"),
        ),
    ),
    Written(
        "fault-of-other-writers",
        _degraded(
            _fault(
                "orphan_processes",
                blocks=False,
                source=SOURCE_SESSIOND,
                stale=True,
                message="foreign_pi_processes=2",
                sandbox="chat-s3",
            ),
            _fault("protocol_mismatch", blocks=True, source=SOURCE_SESSIOND),
            _fault("protocol_violation", blocks=True, source=SOURCE_SESSIOND),
            _fault("audit_unreadable", blocks=False, source=SOURCE_SESSIOND),
            _fault("grants_stale", blocks=True, source=SOURCE_PEP, message="not read", rev=None),
        ),
    ),
    Written(
        "fault-pep-unreachable",
        _degraded(
            _fault(
                "pep_unreachable",
                blocks=False,
                url=_PEP_URL,
                message=f"{_PEP_URL}/healthz has not answered 200 since {OLD}",
            ),
            chaperone=_PEP_AWAY,
        ),
    ),
    Written(
        "fault-mcp-held",
        _degraded(
            _fault(
                "mcp_install_held",
                blocks=False,
                release_id=_RELEASE_ID,
                release_status="refused",
                servers=["weather"],
                retry_after="2999-01-02T00:00:00Z",
                message=_HELD_LINE,
            )
        ),
    ),
    Written(
        "fault-detail-shapes",
        _degraded(
            _fault(
                "timer_enable_failed",
                blocks=False,
                timers=["creche-trigger-chat-0.timer", "creche-trigger-chat-1.timer"],
                message="systemd does not report 2 timers as enabled",
            ),
            _fault(
                "image_flavor_unconfigured",
                blocks=False,
                flavor="python",
                configured=[],
                nested=_NESTED,
                message="",
            ),
            _fault("image_behind", blocks=False),
            _fault("token_publish_failed", blocks=True),
        ),
    ),
    Written(
        "fault-rescoped",
        _degraded(
            _fault(
                "sandbox_start_failed",
                blocks=False,
                source=SOURCE_SESSIOND,
                message="the playpen answered fatal",
                sandbox="chat-s2",
            ),
            sandboxes=(_sandbox(1), _sandbox(2, SandboxLifecycle.CREATING)),
        ),
    ),
    Written(
        "text-escapes",
        _degraded(
            _fault("spend_unknown", blocks=False, message=_ODD_TEXT),
            validation=_validation(first_error=_ODD_TEXT),
        ),
    ),
    # --- the forms of a float ------------------------------------------------
    Written("spend-float-plain", _spent(7.25, 12.5)),
    Written("spend-float-tenth", _spent(0.1, 1e16)),
    Written("spend-float-small", _spent(1e-05, 0.0001)),
    Written("spend-float-edges", _spent(5e-324, 1.7976931348623157e308)),
    Written("spend-float-third", _spent(1 / 3, 1e22)),
    Written("spend-float-long", _spent(123456789.0, 1e15)),
    Written("spend-float-exponent", _spent(2.5e-07, 1.2345678901234568e17)),
    Written("spend-float-zero", _spent(-0.0, 0.0)),
    # --- what the writer takes and contract 05 does not ----------------------
    Written("lax-kind-unknown", _document(kind="robot")),
    Written("lax-written-at-not-a-time", _document(written_at="soon")),
    Written("lax-written-at-no-offset", _document(written_at="2999-01-01T00:00:00")),
    Written("lax-epoch-zero", _document(credentials=_credentials(epoch=0))),
    Written("lax-epoch-negative", _document(credentials=_credentials(epoch=-1))),
    Written("lax-count-negative", _document(validation=_validation(warning_count=-1))),
    Written(
        "lax-first-error-over-cap",
        _document(
            state=FamilyState.INVALID,
            validation=_validation(ok=False, error_count=1, first_error="e" * 201),
        ),
    ),
    Written("lax-reconcile-in-sync", _document(reconcile=_reconcile("validate"))),
    Written(
        "lax-reconcile-step-unknown",
        _document(state=FamilyState.RECONCILING, reconcile=_reconcile("wait")),
    ),
    Written("lax-sandbox-of-other-family", _document(sandboxes=(_sandbox(1, id="code-s1"),))),
    Written("lax-fault-code-unknown", _degraded(_fault("sandbox_lost", blocks=True))),
    Written("lax-fault-blocks-against-table", _degraded(_fault("key_mint_failed", blocks=False))),
    Written(
        "lax-fault-of-other-writer",
        _degraded(_fault("grants_stale", blocks=True, source=SOURCE_SESSIOND)),
    ),
    Written(
        "lax-fault-source-unknown", _degraded(_fault("spend_unknown", blocks=False, source="root"))
    ),
    Written("lax-spend-not-finite", _spent(float("nan"), float("inf"))),
    Written(
        "lax-pep-off-with-url", _document(chaperone=PepReport(reach=PepReach.OFF, url=_PEP_URL))
    ),
    Written(
        "lax-pep-ok-with-since",
        _document(chaperone=replace(_PEP_OK, unreachable_since=OLD)),
    ),
)


def _document_args(document: StatusDocument) -> dict[str, Json]:
    """The fields of the document, by the names of contract 05 §2.1."""
    sandboxes = [
        {
            "id": one.id,
            "state": one.state,
            "power": one.power,
            "image": one.image,
            "spec_hash": one.spec_hash,
            "cpus": one.cpus,
            "memory": one.memory,
            "created_at": one.created_at,
            "ready_at": one.ready_at,
            "channel": one.channel,
            "supervisor_env": one.playpen_env,
        }
        for one in document.sandboxes
    ]
    faults = [
        {
            "code": one.code,
            "blocks_turns": one.blocks_turns,
            "since": one.since,
            "source": one.source,
            "stale": one.stale,
            "detail": [[key, value] for key, value in one.detail.items()],
        }
        for one in document.faults
    ]
    webhooks = [
        {"name": one.name, "token_path": str(one.path)} for one in document.triggers.webhooks
    ]
    fields = {
        "family": document.family,
        "kind": document.kind,
        "state": document.state,
        "written_at": document.written_at,
        "registry_rev": document.registry_rev,
        "applied_rev": document.applied_rev,
        "config_rev": document.config_rev,
        "validation": document.validation,
        "faults": faults,
        "reconcile": document.reconcile,
        "sandboxes": sandboxes,
        "credentials": document.credentials,
        "spend": document.spend,
        "limits": document.limits,
        "triggers": {"webhooks": webhooks, "enqueue": document.triggers.enqueue},
        "pep": {
            "watch": document.chaperone.reach,
            "url": document.chaperone.url,
            "checked_at": document.chaperone.checked_at,
            "unreachable_since": document.chaperone.unreachable_since,
        },
    }

    return {key: normalize(value) for key, value in fields.items()}


def _write_vector(written: Written, scratch: Path) -> Vector:
    given: dict[str, Json] = {"args": _document_args(written.document)}
    target = scratch / "write" / written.id / "status.json"
    target.parent.mkdir(parents=True)
    outcome = attempt(lambda: caregiver_status.write_status(target, written.document))
    if isinstance(outcome, Raised):
        return raised(written.id, given, outcome.exc)

    return accepted(written.id, given, output=bytes_input(target.read_bytes()))


# --- the fault file of attendance -------------------------------------------------

_CLOCK_START: Final = datetime(2999, 1, 1, tzinfo=UTC)
_CLOCK_STEP: Final = timedelta(seconds=7)


def _stepping_clock() -> tuple[Callable[[], datetime], Callable[[], datetime]]:
    """A clock that moves 7 seconds at each reading, and the last time it gave."""
    last = [_CLOCK_START]

    def read() -> datetime:
        last[0] += _CLOCK_STEP

        return last[0]

    return read, lambda: last[0]


@dataclass(frozen=True)
class Raise:
    """One call of `FaultReporter.raise_fault`."""

    code: FaultCode
    message: str | None = None
    sandbox: str | None = None


@dataclass(frozen=True)
class Clear:
    """One call of `FaultReporter.clear`."""

    code: FaultCode


@dataclass(frozen=True)
class Reported:
    """The calls that bring one fault file of `attendance` to its last form."""

    id: str
    calls: tuple[Raise | Clear, ...]


REPORTED: Final[tuple[Reported, ...]] = (
    Reported(
        "one-fault",
        (
            Raise(
                FaultCode.ORPHAN_PROCESSES,
                "foreign_pi_processes=2",
                f"{FAMILY}-s3",
            ),
        ),
    ),
    *(Reported(f"code-{code.value}", (Raise(code),)) for code in FaultCode),
    Reported("message-only", (Raise(FaultCode.PROTOCOL_VIOLATION, "the refusal budget ran out"),)),
    Reported("sandbox-only", (Raise(FaultCode.SANDBOX_START_FAILED, None, f"{FAMILY}-s4"),)),
    Reported("message-empty", (Raise(FaultCode.AUDIT_UNREADABLE, "", ""),)),
    Reported(
        "each-code", tuple(Raise(code, f"about {code.value}") for code in reversed(FaultCode))
    ),
    Reported(
        "raised-again",
        (
            Raise(FaultCode.PROTOCOL_MISMATCH, "first", f"{FAMILY}-s1"),
            Raise(FaultCode.PROTOCOL_MISMATCH, "second", f"{FAMILY}-s2"),
        ),
    ),
    Reported(
        "one-cleared",
        (
            Raise(FaultCode.PROTOCOL_MISMATCH),
            Raise(FaultCode.ORPHAN_PROCESSES),
            Clear(FaultCode.PROTOCOL_MISMATCH),
        ),
    ),
    Reported("all-cleared", (Raise(FaultCode.ORPHAN_PROCESSES), Clear(FaultCode.ORPHAN_PROCESSES))),
    Reported("text-escapes", (Raise(FaultCode.ORPHAN_PROCESSES, _ODD_TEXT, _ODD_TEXT),)),
)


def _reported_file(reported: Reported, root: Path) -> tuple[dict[str, Json], bytes]:
    """The open faults after the calls, and the bytes of the file."""
    read, last = _stepping_clock()
    reporter = FaultReporter(root)
    with patch.object(attendance_faults, "now", read):
        for call in reported.calls:
            if isinstance(call, Raise):
                reporter.raise_fault(FAMILY, call.code, call.message, call.sandbox)
            else:
                reporter.clear(FAMILY, call.code)

    args: dict[str, Json] = {
        "family": FAMILY,
        "written_at": normalize(last()),
        "faults": normalize(reporter.open_faults(FAMILY)),
    }

    return args, fault_file(root, FAMILY).read_bytes()


def _reported_vector(reported: Reported, scratch: Path) -> Vector:
    args, raw = _reported_file(reported, scratch / "attendance" / reported.id)

    return accepted(reported.id, {"args": args}, output=bytes_input(raw))


# --- the fault file of the chaperone ----------------------------------------------

#: `chaperone.faults.FAULT_REFRESH_S` is 30 seconds. A second call with the
#: same message after this long writes the file again and keeps `since`.
_REFRESHED_AFTER: Final = timedelta(seconds=45)


@dataclass(frozen=True)
class Stale:
    """The calls that bring one fault file of the chaperone to its last form."""

    id: str
    family: str = FAMILY
    message: str = "the grant file did not parse"
    rev: str | None = _REV
    #: The fault is raised a second time, after the refresh interval.
    refreshed: bool = False
    #: The fault is cleared after it is raised.
    cleared: bool = False


STALE: Final[tuple[Stale, ...]] = (
    Stale("raised"),
    Stale("never-parsed", rev=None),
    Stale("refreshed", refreshed=True),
    Stale("cleared", cleared=True),
    Stale("message-empty", message=""),
    Stale("text-escapes", message=_ODD_TEXT),
    Stale("family-not-a-name", family="Chat"),
)


def _stale_vector(stale: Stale, scratch: Path) -> Vector:
    directory = scratch / "chaperone" / stale.id
    since = _CLOCK_START
    written_at = since + _REFRESHED_AFTER if stale.refreshed or stale.cleared else since
    times: Iterator[datetime] = iter((since, written_at))
    faults: list[dict[str, object]] = []
    with quiet_logs():
        writer = FaultWriter(directory, clock=lambda: next(times))
        writer.raise_stale(stale.family, stale.message, stale.rev)
        if stale.refreshed:
            writer.raise_stale(stale.family, stale.message, stale.rev)

        if stale.cleared:
            writer.clear(stale.family)

    if not stale.cleared:
        faults.append({"since": since, "message": stale.message, "rev": stale.rev})

    args: dict[str, Json] = {
        "family": stale.family,
        "written_at": normalize(written_at),
        "faults": normalize(faults),
    }
    target = directory / f"{stale.family}.json"
    if not target.is_file():
        return refused(stale.id, {"args": args})

    return accepted(stale.id, {"args": args}, output=bytes_input(target.read_bytes()))


# --- the reader of a fault file -----------------------------------------------------


@dataclass(frozen=True)
class FaultDocument:
    """One fault file, and the directory that `caregiver` reads it from."""

    id: str
    raw: bytes
    source: str = SOURCE_SESSIOND


def _entry(code: object = "orphan_processes", **fields: object) -> dict[str, object]:
    return {
        "code": code,
        "blocks_turns": False,
        "since": FRESH,
        "source": SOURCE_SESSIOND,
        **fields,
    }


def _file(**fields: object) -> dict[str, object]:
    """A fault file of `attendance` with one open fault, but for `fields`."""
    return {
        "family": FAMILY,
        "source": SOURCE_SESSIOND,
        "written_at": FRESH,
        "faults": [_entry()],
        **fields,
    }


def _fault_doc(doc_id: str, read_as: str = SOURCE_SESSIOND, **fields: object) -> FaultDocument:
    return FaultDocument(doc_id, json.dumps(_file(**fields)).encode("utf-8"), read_as)


def _fault_without(doc_id: str, name: str) -> FaultDocument:
    body = _file()
    del body[name]

    return FaultDocument(doc_id, json.dumps(body).encode("utf-8"))


def _written_fault_files(scratch: Path) -> tuple[FaultDocument, ...]:
    """Files that the two writers made, each read from its own directory and
    from the directory of the other writer."""
    each_code = next(one for one in REPORTED if one.id == "each-code")
    _, of_attendance = _reported_file(each_code, scratch / "written" / "attendance")
    directory = scratch / "written" / "chaperone"
    FaultWriter(directory, clock=lambda: _CLOCK_START).raise_stale(FAMILY, "not read", _REV)
    of_chaperone = (directory / f"{FAMILY}.json").read_bytes()

    return (
        FaultDocument("written-by-attendance", of_attendance),
        FaultDocument("written-by-attendance-read-as-pep", of_attendance, SOURCE_PEP),
        FaultDocument("written-by-chaperone", of_chaperone, SOURCE_PEP),
        FaultDocument("written-by-chaperone-read-as-sessiond", of_chaperone),
    )


_FAULT_TEXT: Final = json.dumps(_file())

FAULT_DOCUMENTS: Final[tuple[FaultDocument, ...]] = (
    _fault_doc("one-fault"),
    _fault_doc("no-fault", faults=[]),
    _fault_doc("read-as-managerd", SOURCE_MANAGERD),
    _fault_doc("source-key-of-other-writer", source=SOURCE_PEP),
    _fault_doc("family-other-name", family="code"),
    _fault_doc("family-not-a-name", family="Not A Family"),
    _fault_doc("family-number", family=5),
    _fault_without("family-missing", "family"),
    # --- written_at ---
    _fault_doc("written-old", written_at=OLD),
    _fault_doc("written-90-seconds-ago", written_at="2998-12-31T23:59:00Z"),
    _fault_doc("written-91-seconds-ago", written_at="2998-12-31T23:58:59Z"),
    _fault_doc("written-offset", written_at="2999-01-01T02:00:00+02:00"),
    _fault_doc("written-fraction", written_at="2999-01-01T00:00:00.5Z"),
    _fault_doc("written-not-a-time", written_at="yesterday"),
    _fault_doc("written-empty", written_at=""),
    _fault_doc("written-number", written_at=32472144000),
    _fault_without("written-missing", "written_at"),
    # --- the list ---
    _fault_without("faults-missing", "faults"),
    _fault_doc("faults-object", faults={"orphan_processes": True}),
    _fault_doc("faults-null", faults=None),
    _fault_doc("faults-not-objects", faults=["orphan_processes", None, 5, [_entry()]]),
    _fault_doc("fault-two", faults=[_entry(), _entry("protocol_mismatch")]),
    _fault_doc("fault-twice", faults=[_entry(), _entry(message="again")]),
    # --- one entry ---
    _fault_doc("code-unknown", faults=[_entry("sandbox_lost")]),
    _fault_doc("code-of-other-writer", faults=[_entry("grants_stale")]),
    _fault_doc("code-of-caregiver", faults=[_entry("spend_unknown")]),
    _fault_doc("code-upper", faults=[_entry("ORPHAN_PROCESSES")]),
    _fault_doc("code-number", faults=[_entry(5)]),
    _fault_doc("code-missing", faults=[{"since": FRESH}]),
    _fault_doc("since-missing", faults=[{"code": "orphan_processes"}]),
    _fault_doc("since-number", faults=[_entry(since=5)]),
    _fault_doc("since-not-a-time", faults=[_entry(since="yesterday")]),
    _fault_doc("since-empty", faults=[_entry(since="")]),
    _fault_doc("blocks-against-table", faults=[_entry("protocol_mismatch", blocks_turns=False)]),
    _fault_doc("blocks-text", faults=[_entry(blocks_turns="true")]),
    _fault_doc("stale-claimed", faults=[_entry(stale=True)]),
    _fault_doc("source-claimed", faults=[_entry(source=SOURCE_PEP)]),
    _fault_doc("only-code-and-since", faults=[{"code": "audit_unreadable", "since": FRESH}]),
    _fault_doc(
        "detail-kept",
        faults=[
            _entry(
                "sandbox_start_failed",
                message="the playpen answered fatal",
                sandbox="chat-s4",
                attempts=3,
                nested=_NESTED,
            )
        ],
    ),
    _fault_doc("grants-stale", SOURCE_PEP, faults=[_entry("grants_stale", message="m", rev=None)]),
    _fault_doc("grants-stale-old", SOURCE_PEP, written_at=OLD, faults=[_entry("grants_stale")]),
    # --- the bytes and the JSON ---
    FaultDocument("bytes-empty", b""),
    FaultDocument("bytes-bom", b"\xef\xbb\xbf" + _FAULT_TEXT.encode("utf-8")),
    FaultDocument("json-text", b"not json"),
    FaultDocument("json-truncated", _FAULT_TEXT.encode("utf-8")[:-1]),
    FaultDocument("json-trailing-text", _FAULT_TEXT.encode("utf-8") + b" x"),
    FaultDocument("json-top-array", b"[" + _FAULT_TEXT.encode("utf-8") + b"]"),
    FaultDocument("json-top-null", b"null"),
    FaultDocument(
        "json-duplicate-code",
        _FAULT_TEXT.replace(
            '"code": "orphan_processes"', '"code": "grants_stale", "code": "orphan_processes"'
        ).encode(),
    ),
    FaultDocument(
        "json-not-finite",
        _FAULT_TEXT.replace('"blocks_turns": false', '"ratio": NaN, "limit": -Infinity').encode(),
    ),
    FaultDocument(
        "json-deep-unknown-field",
        _FAULT_TEXT[:-1].encode() + b', "x": ' + b"[" * 200 + b"]" * 200 + b"}",
    ),
)


def _fault_vector(document: FaultDocument, scratch: Path) -> Vector:
    given = bytes_input(document.raw)
    params = {"source": document.source}
    target = scratch / "read" / document.id / f"{FAMILY}.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(document.raw)
    outcome = attempt(lambda: read_fault_file(target, document.source, now=NOW))
    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc, params=params)

    if outcome is None:
        return refused(document.id, given, params=params)

    return accepted(document.id, given, outcome, params=params)


# --- the outcome record -------------------------------------------------------------

#: The stem of the file that every outcome vector is read from.
OUTCOME_ID: Final = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"
_SESSION: Final = "auto-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"
_STARTED: Final = datetime(2999, 1, 1, 6, 0, 2, tzinfo=UTC)
_ENDED: Final = datetime(2999, 1, 1, 6, 7, 45, tzinfo=UTC)
_FIRED: Final = datetime(2999, 1, 1, 6, 0, 0, tzinfo=UTC)
#: As long as the cap that `attendance` puts on the error text, in bytes.
_LONG_ERROR: Final = "x" * 4096


def _record(**changes: object) -> OutcomeRecord:
    """The record of one job that a timer started and that ended well."""
    record = OutcomeRecord(
        id=OUTCOME_ID,
        family=FAMILY,
        session=_SESSION,
        trigger=Trigger(TriggerKind.TIMER, "morning-triage", _FIRED),
        started_at=_STARTED,
        ended_at=_ENDED,
        status=Outcome.OK,
        error=None,
        turns=4,
        approvals=Approvals(requested=2, approved=1, denied=1),
        spend_usd=0.5,
        spend_reason=None,
        sandbox=f"{FAMILY}-s2",
    )

    return replace(record, **changes)


@dataclass(frozen=True)
class OutcomeDocument:
    """One outcome file: an id, and the record or the bytes."""

    id: str
    record: OutcomeRecord | None = None
    raw: bytes = b""


def _outcome_doc(doc_id: str, **fields: object) -> OutcomeDocument:
    return OutcomeDocument(doc_id, raw=json.dumps(fields).encode("utf-8"))


_OUTCOME_TEXT: Final = json.dumps({"id": OUTCOME_ID, "family": FAMILY, "turns": 3})

OUTCOME_DOCUMENTS: Final[tuple[OutcomeDocument, ...]] = (
    # --- records that attendance writes -------------------------------------
    OutcomeDocument("written-timer-ok", _record()),
    OutcomeDocument(
        "written-webhook-failed",
        _record(
            trigger=Trigger(TriggerKind.WEBHOOK, "boiler-alert", _FIRED),
            status=Outcome.FAILED,
            error="model_error: the model gave no answer",
            approvals=Approvals(),
        ),
    ),
    OutcomeDocument(
        "written-dispatch",
        _record(
            trigger=Trigger(TriggerKind.DISPATCH, "scrum-lead", _FIRED, ("chat", "scrum-lead")),
            status=Outcome.DENIED,
        ),
    ),
    OutcomeDocument(
        "written-cron-no-name",
        _record(trigger=Trigger(TriggerKind.TIMER), status=Outcome.TIMEOUT),
    ),
    OutcomeDocument("written-no-trigger", _record(trigger=None, status=Outcome.CANCELLED)),
    OutcomeDocument(
        "written-spend-unknown",
        _record(spend_usd=None, spend_reason=outcomes.SPEND_UNKNOWN_REASON),
    ),
    OutcomeDocument("written-spend-zero", _record(spend_usd=0.0, turns=0, sandbox="")),
    OutcomeDocument("written-error-long", _record(status=Outcome.FAILED, error=_LONG_ERROR)),
    OutcomeDocument("written-text-escapes", _record(status=Outcome.FAILED, error=_ODD_TEXT)),
    # --- each field ----------------------------------------------------------
    _outcome_doc("empty-object"),
    _outcome_doc("id-other", id=_RELEASE_ID),
    _outcome_doc("id-empty", id=""),
    _outcome_doc("id-number", id=5),
    _outcome_doc("texts-wrong-types", family=5, session=None, started_at=1, ended_at=[], status={}),
    _outcome_doc("status-unknown", status="exploded"),
    _outcome_doc("error-null", error=None),
    _outcome_doc("error-number", error=5),
    _outcome_doc("sandbox-not-a-name", sandbox="Chat"),
    _outcome_doc("trigger-kind-only", trigger={"kind": "timer"}),
    _outcome_doc("trigger-name-only", trigger={"name": "morning-triage"}),
    _outcome_doc("trigger-empty", trigger={}),
    _outcome_doc("trigger-null", trigger=None),
    _outcome_doc("trigger-list", trigger=["timer", "morning-triage"]),
    _outcome_doc("trigger-text", trigger="timer morning-triage"),
    _outcome_doc("trigger-wrong-types", trigger={"kind": 5, "name": True}),
    _outcome_doc("trigger-kind-unknown", trigger={"kind": "manual", "name": "by-hand"}),
    _outcome_doc("turns-zero", turns=0),
    _outcome_doc("turns-negative", turns=-1),
    _outcome_doc("turns-true", turns=True),
    _outcome_doc("turns-float", turns=3.0),
    _outcome_doc("turns-text", turns="3"),
    _outcome_doc("turns-past-64-bits", turns=2**70),
    _outcome_doc("spend-integer", spend_usd=2),
    _outcome_doc("spend-negative", spend_usd=-0.5),
    _outcome_doc("spend-true", spend_usd=True),
    _outcome_doc("spend-text", spend_usd="0.5"),
    _outcome_doc("spend-null", spend_usd=None),
    _outcome_doc("spend-past-64-bits", spend_usd=10**20),
    OutcomeDocument("spend-not-finite", raw=b'{"spend_usd": NaN}'),
    # --- the bytes and the JSON ----------------------------------------------
    OutcomeDocument("bytes-empty"),
    OutcomeDocument("bytes-bom", raw=b"\xef\xbb\xbf" + _OUTCOME_TEXT.encode("utf-8")),
    OutcomeDocument("bytes-not-utf8", raw=b'{"family": "\xff"}'),
    OutcomeDocument("json-text", raw=b"not json"),
    OutcomeDocument("json-truncated", raw=_OUTCOME_TEXT.encode("utf-8")[:-1]),
    OutcomeDocument("json-trailing-text", raw=_OUTCOME_TEXT.encode("utf-8") + b" x"),
    OutcomeDocument("json-top-array", raw=b"[" + _OUTCOME_TEXT.encode("utf-8") + b"]"),
    OutcomeDocument("json-top-null", raw=b"null"),
    OutcomeDocument(
        "json-duplicate-family",
        raw=_OUTCOME_TEXT.replace(
            '"family": "chat"', '"family": "code", "family": "chat"'
        ).encode(),
    ),
    OutcomeDocument(
        "json-huge-integer",
        raw=_OUTCOME_TEXT.replace('"turns": 3', '"turns": ' + "9" * 5000).encode(),
    ),
    OutcomeDocument(
        "json-deep-unknown-field",
        raw=_OUTCOME_TEXT[:-1].encode() + b', "x": ' + b"[" * 200 + b"]" * 200 + b"}",
    ),
)


def _outcome_bytes(document: OutcomeDocument, scratch: Path) -> bytes:
    """The bytes of the document: what `attendance` writes for a record."""
    if document.record is None:
        return document.raw

    root = scratch / "written-outcomes" / document.id
    outcomes.write(root, document.record)

    return outcome_file(root, FAMILY, OUTCOME_ID).read_bytes()


def _outcome_vector(document: OutcomeDocument, scratch: Path) -> Vector:
    raw = _outcome_bytes(document, scratch)
    given = bytes_input(raw)
    directory = scratch / "outcomes" / document.id
    target = directory / FAMILY / f"{OUTCOME_ID}.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    rows = attempt(lambda: statusdocs.read_outcomes(directory, FAMILY, limit=1))
    if isinstance(rows, Raised):
        return raised(document.id, given, rows.exc)

    (row,) = rows
    if row.problem:
        return refused(document.id, given, row)

    return accepted(document.id, given, row)


# --- the surfaces -----------------------------------------------------------------------

_NOTE_ARGS_ORDER: Final = (
    "Every object inside args has its keys in sorted order. The writer puts the keys of the "
    "file in the order of contract 05."
)
_NOTE_FAULT_ARGS: Final = (
    "The input is the open faults of one family when the writer last wrote the file. "
    "args.written_at and since are times in UTC."
)


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix="vectors-status-files-") as scratch_name:
        scratch = Path(scratch_name)
        fault_documents = (*_written_fault_files(scratch), *FAULT_DOCUMENTS)
        built = (
            Surface(
                name="status.write",
                path="status/write.json",
                entry="caregiver.status.write_status",
                contract=f"{CONTRACT} §2, §9",
                notes=(
                    "The input is the fields of one status document, by the names of contract "
                    "05 §2.1. The generator makes the dataclasses of caregiver.status from them.",
                    "args.faults[].detail is the extra keys of one fault, in the order that the "
                    "writer keeps: each item is [key, value].",
                    _NOTE_ARGS_ORDER,
                    "output is the bytes of the file.",
                    "A vector whose id starts with lax- is a document that contract 05 does "
                    "not permit. The writer checks no field.",
                ),
                vectors=tuple(_write_vector(one, scratch) for one in WRITTEN),
            ),
            Surface(
                name="status.fault_file.attendance",
                path="status/fault_file.attendance.json",
                entry="attendance.faults.FaultReporter.raise_fault",
                contract=f"{CONTRACT} §3.3, §3.3.1",
                notes=(
                    _NOTE_FAULT_ARGS,
                    "The generator calls raise_fault and clear until the reporter holds "
                    "args.faults, with a clock that moves 7 seconds at each reading.",
                    "message and sandbox are null when the caller gave none. The file then "
                    "has no such key.",
                    _NOTE_ARGS_ORDER,
                    "output is the bytes of the file.",
                ),
                vectors=tuple(_reported_vector(one, scratch) for one in REPORTED),
            ),
            Surface(
                name="status.fault_file.chaperone",
                path="status/fault_file.chaperone.json",
                entry="chaperone.faults.FaultWriter.raise_stale",
                contract=f"{CONTRACT} §3.3, §3.3.1",
                notes=(
                    _NOTE_FAULT_ARGS,
                    "The one code of this writer is grants_stale. Each fault of args.faults "
                    "holds the message and the revision of that fault.",
                    "A refused vector is a family for which the writer writes no file.",
                    _NOTE_ARGS_ORDER,
                    "output is the bytes of the file. The writer does not escape a character "
                    "outside ASCII.",
                ),
                vectors=tuple(_stale_vector(one, scratch) for one in STALE),
            ),
            Surface(
                name="status.fault_file.caregiver",
                path="status/fault_file.caregiver.json",
                entry="caregiver.faults.read_fault_file",
                contract=f"{CONTRACT} §3.3.1",
                notes=(
                    "The input is the bytes of one fault file. params.source is the writer "
                    "whose directory holds the file: sessiond or pep.",
                    "The entry point takes the time now. The generator gives it "
                    f"{NOW.isoformat()}.",
                    "value is what the reader takes from the file. A refused vector is a file "
                    "that the reader does not use: it returns None.",
                    "value.faults[].detail holds each key of the entry that the reader does "
                    "not know.",
                ),
                vectors=tuple(_fault_vector(one, scratch) for one in fault_documents),
            ),
            Surface(
                name="status.outcome.noticeboard",
                path="status/outcome.noticeboard.json",
                entry="noticeboard.statusdocs.read_outcomes",
                contract=f"{CONTRACT} §8, contract 02 §13.1",
                notes=(
                    "The input is the bytes of one outcome record. The generator writes it to "
                    "<root>/<context.family>/<context.stem>.json and gives the reader that root.",
                    "A vector whose id starts with written- holds the bytes that "
                    "attendance.outcomes.write makes for one record.",
                    "The reader returns a row for every file. A refused vector is a row with a "
                    "problem: the reader could not parse the file. refusal is that row.",
                ),
                context={"family": FAMILY, "stem": OUTCOME_ID},
                vectors=tuple(_outcome_vector(one, scratch) for one in OUTCOME_DOCUMENTS),
            ),
        )

    caregiver_status.forget_published()

    return built
