"""One sandbox: how it is specified, created, recorded and destroyed
(contract 05 §4.1 to §4.4).

This module is the ONE creation path. `apply_once` and the reconciler both
come through it, so the egress proof of §4.3 step 2 cannot be skipped by
whichever caller happens to run.

    counter moves -> record `planned` -> sbx create -> allow -> assert
                  -> supervisor.env -> record `creating`

**A failed create burns its id.** The counter moves BEFORE `sbx create`, so
a half-built VM is destroyed and the retry takes the next name. Contract 05
§10 row 7 leaves what `sbx create` does with an existing name unverified.
The alternative — write the counter
after the create — makes a re-apply take the early return and report a
sandbox whose egress was never proved, which is the failure mode that hides
a hole in deny-by-default. An id costs nothing. A hole
costs everything.

It sits beside `apply.py` rather than under it: it calls four siblings
(`driver`, `egress`, `supervisor_env`, `status`), which a leaf may not do
(`AGENTS.md`, layers)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, cast

from agent_family import FamilyFile, SwitchMode

from . import paths
from .atomic import atomic_write
from .clock import now_rfc3339
from .driver import DriverError, Mount, SandboxDriver, SandboxSpec
from .egress import EgressConfig
from .faults import FaultEntry
from .images import IMAGE_FLAVOR_UNCONFIGURED, SandboxImages
from .status import ChannelState, SandboxLifecycle, SandboxPower, SandboxStatus
from .supervisor_env import write_supervisor_env
from .switch import SwitchClient, SwitchError, SwitchRequest

#: The ledger names sandboxes, never a secret (contract 05 §2 rule 3).
LEDGER_FILE_MODE: Final = 0o644

#: Contract 05 §4.1's example `spec_hash` is eight hex characters.
SPEC_HASH_CHARS: Final = 8

#: Contract 05 §3.4's step name for the one call of §1. It lives here
#: because this module is what makes that call, and `reconcile.py` re-exports
#: it for the step lists it builds.
SWITCH_STEP: Final = "switch_sandbox"

#: The states in which a sandbox is the family's live one. `planned` and
#: `creating` are here because §4.2 has `sessiond` dial a `creating` sandbox:
#: running the handshake is what promotes it.
LIVE_STATES: Final = (
    SandboxLifecycle.PLANNED,
    SandboxLifecycle.CREATING,
    SandboxLifecycle.READY,
)


@dataclass(frozen=True)
class SandboxRecord:
    """What `managerd` knows about one sandbox without asking anybody.

    `allow` is the exact list the create granted. A destroy needs it to
    remove every policy row, including the two plane endpoints no family
    file names (contract 05 §4.4 step 3)."""

    id: str
    state: SandboxLifecycle
    image: str
    spec_hash: str
    cpus: int
    memory: str
    created_at: str
    ready_at: str | None
    allow: tuple[str, ...]
    supervisor_env: str

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": str(self.state),
            "image": self.image,
            "spec_hash": self.spec_hash,
            "cpus": self.cpus,
            "memory": self.memory,
            "created_at": self.created_at,
            "ready_at": self.ready_at,
            "allow": list(self.allow),
            "supervisor_env": self.supervisor_env,
        }

    def with_state(self, state: SandboxLifecycle) -> SandboxRecord:
        return replace(self, state=state)


@dataclass(frozen=True)
class CreateOutcome:
    """Exactly one of the two is set."""

    record: SandboxRecord | None
    fault: FaultEntry | None


def spec_hash_of(family: FamilyFile, image: str) -> str:
    """Contract 05 §4.1: "hash of the fields that force a replacement" —
    §3.5's last row, and nothing else. A change here forces a new sandbox,
    so a field that lands live must never reach this hash."""
    material = {
        "image": image,
        "cpus": family.sandbox.cpus,
        "memory": family.sandbox.memory,
        "max_resident_processes": family.sandbox.max_resident_processes,
        "files": [[mount.path, mount.mode] for mount in family.files],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:SPEC_HASH_CHARS]


def sandbox_spec(name: str, family: FamilyFile, state_root: Path, image: str) -> SandboxSpec:
    """Mount order (contract 03 §7.1): the session store first — sbx's only
    allowed primary, read-write — then the three managerd-owned
    directories, then the family's own mounts in file order. Each mount
    carries ONE path, because sbx attaches a host directory at that same
    path inside the VM and takes no target."""
    mounts = [
        Mount(str(paths.session_dir(family.name)), readonly=False),
        Mount(str(paths.creds_dir(state_root, family.name)), readonly=True),
        Mount(str(paths.config_dir(state_root, family.name)), readonly=True),
        Mount(str(paths.control_dir(state_root, family.name, name)), readonly=False),
    ]
    mounts.extend(Mount(mount.path, readonly=mount.mode == "ro") for mount in family.files)
    return SandboxSpec(
        name=name,
        image=image,
        mounts=tuple(mounts),
        cpus=family.sandbox.cpus,
        memory=family.sandbox.memory,
    )


def create_sandbox(
    family: FamilyFile,
    *,
    state_root: Path,
    image: str,
    driver: SandboxDriver,
    egress: EgressConfig,
) -> CreateOutcome:
    """Contract 05 §4.3, steps 1 to 5a. Step 6 (the channel handshake) is
    `sessiond`'s: it holds the only channel, and running the handshake is
    what promotes the sandbox to `ready` (§4.2)."""
    name = _take_next_id(state_root, family.name)
    allowed = egress.allowed(family.egress)
    record = SandboxRecord(
        id=name,
        state=SandboxLifecycle.PLANNED,
        image=image,
        spec_hash=spec_hash_of(family, image),
        cpus=family.sandbox.cpus,
        memory=family.sandbox.memory,
        created_at=now_rfc3339(),
        ready_at=None,
        allow=allowed,
        supervisor_env=str(paths.supervisor_env_path(state_root, family.name, name)),
    )
    # Recorded before the VM exists, never after: a crash between the two
    # would otherwise leave a VM nothing knows the name of.
    _put(state_root, family.name, record)
    empty_control_dir(state_root, family.name, name)

    try:
        driver.create(sandbox_spec(name, family, state_root, image))
        driver.set_egress(name, allowed)
        driver.assert_egress(name, allowed=allowed, denied=egress.denied(family.egress))
    except DriverError as exc:
        return _failed(state_root, family.name, record, driver, exc)

    # After the proof, never before: a file naming a sandbox whose egress
    # was not proved would point `sessiond` at a hole (invariant 11).
    write_supervisor_env(
        paths.supervisor_env_path(state_root, family.name, name),
        state_root=state_root,
        family=family.name,
        sandbox=name,
    )
    created = record.with_state(SandboxLifecycle.CREATING)
    _put(state_root, family.name, created)
    return CreateOutcome(record=created, fault=None)


def unconfigured_image_fault(family: FamilyFile, images: SandboxImages) -> FaultEntry:
    """Contract 05 §3.3, `image_flavor_unconfigured`. The family asked for
    a flavor this host was given no image for, so no sandbox is created.

    It does NOT stop turns. The gap is a platform argument, not a
    permission: a family already serving keeps the sandbox its last good
    revision built (invariant 19's reading), and one that has no sandbox
    yet has no turns to stop. The detail names the flavor and what this
    host does have, so the fix is one argument."""
    return FaultEntry(
        code=IMAGE_FLAVOR_UNCONFIGURED,
        blocks_turns=False,
        since=now_rfc3339(),
        source="managerd",
        detail={
            "flavor": family.sandbox.image,
            "configured": list(images.configured()),
            "message": (
                f"no image for sandbox flavor '{family.sandbox.image}'; "
                f"this manager was started with {', '.join(images.configured()) or 'none'}"
            ),
        },
    )


def fail_planned(state_root: Path, family_name: str, driver: SandboxDriver) -> tuple[str, ...]:
    """Retire every `planned` row a killed process left behind. Answers the
    ids it retired.

    `create_sandbox` writes `planned`, runs §4.3 steps 1 to 5a, and only
    then writes `creating`. So a `planned` row that outlives the pass that
    wrote it is evidence of a kill inside those steps, and whatever `sbx
    create` had already built may carry no egress at all, or egress that
    was never proved.

    §4.2 puts `planned` in `LIVE_STATES` because §4.2 rule 2 has
    `sessiond` wait for one. That also made the reconciler ADOPT one as
    the family's serving sandbox and ask for its handshake, which would
    hand turns to a VM that never passed step 2's canary probe -- a hole
    in deny-by-default (invariant 11). Retiring the row closes it: the id
    is burnt, §4.1 never reuses one, and the next pass creates a new
    sandbox by the ordinary path that does prove its egress.

    Called at the top of a pass, never inside one: two passes of one
    family never run at once, so a `planned` row seen here
    was written by a process that is gone."""
    retired: list[str] = []
    for record in read_ledger(state_root, family_name):
        if record.state is not SandboxLifecycle.PLANNED:
            continue

        # Best effort, like every other destroy after a failed create: a
        # VM that was never made answers "not found", which `destroy`
        # already tolerates.
        with contextlib.suppress(DriverError):
            driver.destroy(record.id, record.allow)

        _put(state_root, family_name, record.with_state(SandboxLifecycle.FAILED))
        _forget_control(state_root, family_name, record.id)
        retired.append(record.id)

    return tuple(retired)


def promote_sandbox(
    state_root: Path, family_name: str, record: SandboxRecord, switch: SwitchClient
) -> tuple[SandboxRecord, str]:
    """Contract 05 §4.3 steps 6 and 7: ask for the handshake, write down the
    answer. Returns the record and the step that ran, empty when none did.

    §5.1's `from` is "null on a first create" and §5.3 rule 8 says a first
    switch always names a `creating` sandbox, "because the switch is what
    asks for the handshake". `sessiond` holds the only channel (§4.2) and
    never writes this service's document (§1), so that answer is the only
    evidence `managerd` can have that a sandbox is `ready`.

    A refusal changes nothing and raises no fault. §3.3's
    `sandbox_start_failed` is "no sandbox reached `ready` AFTER THE RETRY
    BUDGET", not after one refused call, and `sessiond` still dials a
    `creating` sandbox (§4.2 rule 1), so the family keeps serving and the
    next apply asks again.

    Both `apply_once` and the reconcile loop come through here, so the one
    verb and the forever loop agree about what `ready` means.
    """
    if record.state is SandboxLifecycle.READY:
        return record, ""

    request = SwitchRequest(
        family=family_name,
        outgoing=None,
        to=record.id,
        mode=SwitchMode.DRAIN,
        reason=f"first handshake for {record.id}",
    )

    try:
        switch.switch(request)
    except SwitchError as exc:
        return record, f"{SWITCH_STEP}:refused({exc})"

    return mark_ready(state_root, family_name, record.id) or record, SWITCH_STEP


def mark_ready(state_root: Path, family_name: str, sandbox_id: str) -> SandboxRecord | None:
    """Contract 05 §4.3 step 7: "on a passing handshake, set `ready_at` and
    `state: ready`".

    The evidence is `sessiond`'s answer to §5 and nothing else. §4.2 rule 4
    forbids writing `ready` after `sbx create` alone, `sessiond` holds the
    only channel, and §1 leaves that one answer as the only thing that
    crosses back. `ready_at` keeps its first value: a handshake that passed
    is an event, not a level."""
    for record in read_ledger(state_root, family_name):
        if record.id != sandbox_id:
            continue

        if record.state is SandboxLifecycle.READY:
            return record

        promoted = replace(record, state=SandboxLifecycle.READY, ready_at=now_rfc3339())
        _put(state_root, family_name, promoted)
        return promoted

    return None


def destroy_sandbox(
    state_root: Path, family_name: str, record: SandboxRecord, driver: SandboxDriver
) -> None:
    """Contract 05 §4.4 steps 3 to 6: the policy rows, then the VM, then
    this sandbox's own files, then the record. `sbx rm -f` removes no
    policy row by itself (README §6), and the rows to remove are the ones
    the create granted — the family's list AND the two plane endpoints."""
    driver.destroy(record.id, record.allow)
    _forget_control(state_root, family_name, record.id)
    _drop(state_root, family_name, record.id)


def set_allow(state_root: Path, family_name: str, allow: tuple[str, ...]) -> None:
    """Record the egress rows every LIVE sandbox now carries.

    A destroy removes exactly `record.allow` (contract 05 §4.4 step 3), and
    an egress edit lands on the running sandbox without replacing it
    (§3.5). A record left unwritten would therefore leak every row the edit
    added. A `failed`, `stopping` or `gone` sandbox keeps its own list: its
    rows are the ones its own create granted, and nothing edited those."""
    records = read_ledger(state_root, family_name)
    updated = tuple(
        replace(one, allow=allow) if one.state in LIVE_STATES else one for one in records
    )
    write_ledger(state_root, family_name, updated)


def empty_control_dir(state_root: Path, family_name: str, sandbox: str) -> None:
    """Contract 05 §4.3 rule 5: a `supervisor.lock` left by a DEAD
    supervisor would make `sessiond` wait for nothing (contract 03 §11.4
    rule 4).

    The directory is this sandbox's own, so emptying it is safe even while
    another sandbox of the family is serving turns: an id is never reused,
    so nothing here was written by a supervisor that is still alive."""
    control = paths.control_dir(state_root, family_name, sandbox)
    if control.exists():
        shutil.rmtree(control)

    control.mkdir(parents=True)


def _forget_control(state_root: Path, family_name: str, sandbox: str) -> None:
    """Contract 05 §4.4 step 5. Both files are per sandbox and an id is
    never reused, so nothing reads them again. Best effort: a destroy that
    already removed the VM must not fail over a leftover directory."""
    with contextlib.suppress(OSError):
        shutil.rmtree(paths.control_dir(state_root, family_name, sandbox), ignore_errors=True)
        paths.supervisor_env_path(state_root, family_name, sandbox).unlink(missing_ok=True)


def live_record(state_root: Path, family_name: str) -> SandboxRecord | None:
    """The newest sandbox that may serve. `failed`, `stopping` and `gone`
    ones are not it, which is what makes a burnt id invisible to a caller
    asking "is this family up?"."""
    for record in reversed(read_ledger(state_root, family_name)):
        if record.state in LIVE_STATES:
            return record

    return None


def status_of(record: SandboxRecord) -> SandboxStatus:
    """The ledger row as contract 05 §4.1's status entry.

    `power` and `channel` are what `managerd` can attest to on its own:
    `sbx create` does not boot a VM, the first exec does (README §6), and
    the channel belongs to `sessiond`.

    There is no `turns_running`: `managerd` cannot count a family's running
    turns. The `managerd` principal holds `Access.INTERNAL`, which reaches
    `/internal/*` and nothing else (contract 02 §3.1), and a session row's
    `sandbox` names the sandbox that served the LAST turn, not the one a
    turn runs on now. A field it cannot fill would be a number that lies
    quietly. `sessiond` is the one authority for
    a live count, and contract 05 §8 already sends the view there."""
    running = record.state in (SandboxLifecycle.READY, SandboxLifecycle.DRAINING)
    return SandboxStatus(
        id=record.id,
        state=record.state,
        power=SandboxPower.RUNNING if running else SandboxPower.STOPPED,
        image=record.image,
        spec_hash=record.spec_hash,
        cpus=record.cpus,
        memory=record.memory,
        created_at=record.created_at,
        ready_at=record.ready_at,
        channel=ChannelState.OPEN if running else ChannelState.CLOSED,
        supervisor_env=record.supervisor_env,
    )


def read_ledger(state_root: Path, family_name: str) -> tuple[SandboxRecord, ...]:
    """Oldest first. An unreadable ledger answers empty, the same way a
    missing one does: `managerd`'s own state is not a reason to crash."""
    try:
        body = json.loads(paths.sandboxes_path(state_root, family_name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()

    if not isinstance(body, dict):
        return ()

    rows = cast("dict[str, Any]", body).get("sandboxes")
    if not isinstance(rows, list):
        return ()

    parsed = (_one_record(raw) for raw in cast("list[Any]", rows))
    return tuple(one for one in parsed if one is not None)


def write_ledger(state_root: Path, family_name: str, records: tuple[SandboxRecord, ...]) -> None:
    body = {
        "family": family_name,
        "written_at": now_rfc3339(),
        "sandboxes": [one.as_json() for one in records],
    }
    atomic_write(
        paths.sandboxes_path(state_root, family_name),
        json.dumps(body, indent=2).encode("utf-8") + b"\n",
        mode=LEDGER_FILE_MODE,
    )


def _take_next_id(state_root: Path, family_name: str) -> str:
    """Move the counter first. The id is spent whether or not the create
    that follows succeeds (the module docstring says why)."""
    sequence = paths.read_sandbox_sequence(state_root, family_name) + 1
    seq_path = paths.sandbox_seq_path(state_root, family_name)
    seq_path.parent.mkdir(parents=True, exist_ok=True)
    seq_path.write_text(f"{sequence}\n", encoding="utf-8")
    return paths.sandbox_id(family_name, sequence)


def _failed(
    state_root: Path,
    family_name: str,
    record: SandboxRecord,
    driver: SandboxDriver,
    exc: DriverError,
) -> CreateOutcome:
    """Destroy what step 1 already made, mark the id spent, and report.

    The destroy is best effort: a driver that could not create is a driver
    that may not be able to destroy either, and a second exception here
    would hide the first."""
    with contextlib.suppress(DriverError):
        driver.destroy(record.id, record.allow)

    _put(state_root, family_name, record.with_state(SandboxLifecycle.FAILED))
    fault = FaultEntry(
        code="sandbox_start_failed",
        blocks_turns=True,
        since=now_rfc3339(),
        source="managerd",
        detail={"message": str(exc), "sandbox": record.id},
    )
    return CreateOutcome(record=None, fault=fault)


def _put(state_root: Path, family_name: str, record: SandboxRecord) -> None:
    """Add or replace one row, keeping creation order."""
    existing = read_ledger(state_root, family_name)
    replaced = tuple(record if one.id == record.id else one for one in existing)
    if all(one.id != record.id for one in existing):
        replaced = (*existing, record)

    write_ledger(state_root, family_name, replaced)


def _drop(state_root: Path, family_name: str, sandbox_id: str) -> None:
    existing = read_ledger(state_root, family_name)
    write_ledger(state_root, family_name, tuple(one for one in existing if one.id != sandbox_id))


def _one_record(raw: Any) -> SandboxRecord | None:
    if not isinstance(raw, dict):
        return None

    row = cast("dict[str, Any]", raw)
    try:
        return SandboxRecord(
            id=str(row["id"]),
            state=SandboxLifecycle(str(row["state"])),
            image=str(row["image"]),
            spec_hash=str(row["spec_hash"]),
            cpus=int(row["cpus"]),
            memory=str(row["memory"]),
            created_at=str(row["created_at"]),
            ready_at=_optional_text(row.get("ready_at")),
            allow=tuple(str(one) for one in cast("list[Any]", row.get("allow", []))),
            supervisor_env=str(row.get("supervisor_env", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) else None
