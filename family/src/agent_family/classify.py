"""Live versus replacement, field by field (contract 01 §6, contract 05 §5.4).

Pure: two parsed files in, one diff out. No clock, no disk, no host. The
reconciler decides what to do from this answer, and refuses a change to an
immutable field with it.

`Direction` describes what a change does to REACH, not to the text. Adding an
`approval` entry narrows, so its direction is `remove`. That is the reading
contract 05 §5.4 needs: a revision that removes reach interrupts, and one that
only adds drains."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .grammar import ALL_TOOLS, duration_s, memory_mb
from .model import FamilyFile, FileMount, ToolGrant


class Landing(StrEnum):
    LIVE = "live"
    REPLACE = "replace"
    IMMUTABLE = "immutable"


class Direction(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    BOTH = "both"
    CHANGE = "change"


class Step(StrEnum):
    """Contract 05 §3.4's reconcile steps, plus `none` for the fields that
    land through the status document, the timer set or the trigger door."""

    WRITE_GRANTS = "write_grants"
    UPDATE_KEY = "update_key"
    WRITE_CONFIG = "write_config"
    SET_EGRESS = "set_egress"
    CREATE_SANDBOX = "create_sandbox"
    NONE = "none"


class SwitchMode(StrEnum):
    DRAIN = "drain"
    INTERRUPT = "interrupt"


@dataclass(frozen=True)
class FieldChange:
    field: str
    landing: Landing
    direction: Direction
    step: Step
    detail: str


@dataclass(frozen=True)
class Diff:
    changes: tuple[FieldChange, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    @property
    def refused(self) -> tuple[FieldChange, ...]:
        """Immutable fields that moved. A `name` change is a new family and a
        `kind` change is refused outright (contract 01 §3.1)."""
        return tuple(one for one in self.changes if one.landing is Landing.IMMUTABLE)

    @property
    def needs_switch(self) -> bool:
        return any(one.landing is Landing.REPLACE for one in self.changes)

    @property
    def switch_mode(self) -> SwitchMode | None:
        """Contract 05 §5.4. A revision that both adds and removes is a
        removal: the removal decides."""
        replacing = [one for one in self.changes if one.landing is Landing.REPLACE]
        if not replacing:
            return None

        narrowing = any(
            one.direction in (Direction.REMOVE, Direction.BOTH, Direction.CHANGE)
            for one in replacing
        )
        return SwitchMode.INTERRUPT if narrowing else SwitchMode.DRAIN

    def steps(self) -> tuple[Step, ...]:
        """Each step once, in the order this module lists the fields."""
        ordered: list[Step] = []
        for one in self.changes:
            if one.step is not Step.NONE and one.step not in ordered:
                ordered.append(one.step)

        return tuple(ordered)

    def reason(self) -> str:
        """One line for the switch call's `reason` (contract 05 §5.1)."""
        return "; ".join(one.detail for one in self.changes) or "no change"


def classify(old: FamilyFile, new: FamilyFile) -> Diff:
    """Every field of contract 01 §6's table, in that table's order."""
    changes: list[FieldChange] = []
    _immutables(old, new, changes)
    _model(old, new, changes)
    _grants(old, new, changes)
    _config(old, new, changes)
    _kind_fields(old, new, changes)
    _sandbox(old, new, changes)
    return Diff(tuple(changes))


def _add(
    changes: list[FieldChange],
    field: str,
    landing: Landing,
    direction: Direction,
    step: Step,
    detail: str,
) -> None:
    changes.append(FieldChange(field, landing, direction, step, detail))


def _immutables(old: FamilyFile, new: FamilyFile, changes: list[FieldChange]) -> None:
    if old.name != new.name:
        _add(
            changes,
            "name",
            Landing.IMMUTABLE,
            Direction.CHANGE,
            Step.NONE,
            f"name changed: '{old.name}' to '{new.name}' is a new family, and the old one is "
            "deleted credentials first",
        )

    if old.kind != new.kind:
        _add(
            changes,
            "kind",
            Landing.IMMUTABLE,
            Direction.CHANGE,
            Step.NONE,
            f"kind changed: '{old.kind}' to '{new.kind}' is refused; live sessions were created "
            "under the old rule",
        )

    if old.description != new.description:
        _add(
            changes,
            "description",
            Landing.LIVE,
            Direction.CHANGE,
            Step.NONE,
            "description changed: registry only",
        )


def _model(old: FamilyFile, new: FamilyFile, changes: list[FieldChange]) -> None:
    if old.model.router != new.model.router:
        # A swap adds one alias and removes another, and a removal takes up to
        # 10 s to be refused on every LiteLLM worker (contract 01 §6.2).
        _add(
            changes,
            "model.router",
            Landing.LIVE,
            Direction.BOTH,
            Step.UPDATE_KEY,
            f"model.router changed: '{old.model.router}' to '{new.model.router}'",
        )

    old_budget = old.model.budget_usd_per_day
    new_budget = new.model.budget_usd_per_day
    if old_budget != new_budget:
        _add(
            changes,
            "model.budget_usd_per_day",
            Landing.LIVE,
            _number(old_budget, new_budget),
            Step.UPDATE_KEY,
            f"model.budget_usd_per_day changed: {old_budget} to {new_budget}",
        )


def _grants(old: FamilyFile, new: FamilyFile, changes: list[FieldChange]) -> None:
    tools = _tools_direction(old.tools, new.tools)
    if tools is not None:
        _add(
            changes,
            "tools",
            Landing.LIVE,
            tools,
            Step.WRITE_GRANTS,
            f"tools changed ({tools}): the PEP re-reads the grant file per call",
        )

    if old.verbs != new.verbs:
        _add(
            changes,
            "verbs",
            Landing.LIVE,
            _verbs_direction(old, new),
            Step.WRITE_GRANTS,
            "verbs changed",
        )

    delegates = _members(old.delegates, new.delegates)
    if delegates is not None:
        _add(changes, "delegates", Landing.LIVE, delegates, Step.WRITE_GRANTS, "delegates changed")

    old_cap = old.max_inflight_delegations
    new_cap = new.max_inflight_delegations
    if old_cap != new_cap:
        # It rides in the grant file, which the PEP re-reads per call
        # (contract 01 §6, contract 04 §1.4), so no sandbox is replaced.
        _add(
            changes,
            "max_inflight_delegations",
            Landing.LIVE,
            _number(old_cap, new_cap),
            Step.WRITE_GRANTS,
            f"max_inflight_delegations changed: {old_cap} to {new_cap}",
        )

    approval = _members(old.approval, new.approval)
    if approval is not None:
        # An approval entry only narrows, so an added entry removes reach.
        _add(
            changes,
            "approval",
            Landing.LIVE,
            _flip(approval),
            Step.WRITE_GRANTS,
            "approval changed: an added entry narrows",
        )


def _config(old: FamilyFile, new: FamilyFile, changes: list[FieldChange]) -> None:
    egress = _members(old.egress, new.egress)
    if egress is not None:
        _add(
            changes,
            "egress",
            Landing.LIVE,
            egress,
            Step.SET_EGRESS,
            "egress changed: a policy change on the running sandbox",
        )

    skills = _members(old.skills, new.skills)
    if skills is not None:
        _add(
            changes,
            "skills",
            Landing.LIVE,
            skills,
            Step.WRITE_CONFIG,
            "skills changed: read at turn start",
        )

    if old.shell != new.shell:
        _add(
            changes,
            "shell",
            Landing.LIVE,
            Direction.ADD if new.shell else Direction.REMOVE,
            Step.WRITE_CONFIG,
            f"shell changed: {old.shell} to {new.shell}, read when a pi process next starts",
        )

    sandbox_tools = _members(old.sandbox_tools, new.sandbox_tools)
    if sandbox_tools is not None:
        _add(
            changes,
            "sandbox_tools",
            Landing.LIVE,
            sandbox_tools,
            Step.WRITE_CONFIG,
            "sandbox_tools changed: read when a pi process next starts",
        )

    if old.system_prompt != new.system_prompt:
        _add(
            changes,
            "system_prompt",
            Landing.LIVE,
            Direction.CHANGE,
            Step.WRITE_CONFIG,
            f"system_prompt changed: {old.system_prompt} to {new.system_prompt}, "
            "read when a pi process next starts",
        )


def _kind_fields(old: FamilyFile, new: FamilyFile, changes: list[FieldChange]) -> None:
    old_timeout = old.job.timeout if old.job is not None else None
    new_timeout = new.job.timeout if new.job is not None else None
    if old_timeout != new_timeout:
        _add(
            changes,
            "job.timeout",
            Landing.LIVE,
            _number(duration_s(old_timeout or ""), duration_s(new_timeout or "")),
            Step.NONE,
            f"job.timeout changed: {old_timeout} to {new_timeout}, read per job",
        )

    if old.triggers != new.triggers:
        _add(
            changes,
            "triggers",
            Landing.LIVE,
            _triggers_direction(old, new),
            Step.NONE,
            "triggers changed: the timer set is rewritten",
        )

    if old.max_running_turns != new.max_running_turns:
        _add(
            changes,
            "max_running_turns",
            Landing.LIVE,
            _number(old.max_running_turns, new.max_running_turns),
            Step.NONE,
            f"max_running_turns changed: {old.max_running_turns} to {new.max_running_turns}",
        )

    # `change`: the check grants and removes no reach, it only decides
    # whether a cron firing wakes the family (§3.15).
    if old.quiet != new.quiet:
        _add(
            changes,
            "quiet",
            Landing.LIVE,
            Direction.CHANGE,
            Step.NONE,
            "quiet changed: read at the next cron firing",
        )


def _sandbox(old: FamilyFile, new: FamilyFile, changes: list[FieldChange]) -> None:
    mounts = _mounts_direction(old.files, new.files)
    if mounts is not None:
        _add(
            changes,
            "files",
            Landing.REPLACE,
            mounts,
            Step.CREATE_SANDBOX,
            _mounts_detail(old.files, new.files),
        )

    if old.sandbox.image != new.sandbox.image:
        # `change`, not `add` or `remove`: the flavors are not ordered.
        # `python` is not more reach than `base` and `base` is not a subset
        # of `python`, so contract 05 §5.4's narrowing reading applies and
        # the switch interrupts rather than drains.
        _add(
            changes,
            "sandbox.image",
            Landing.REPLACE,
            Direction.CHANGE,
            Step.CREATE_SANDBOX,
            f"sandbox.image changed: '{old.sandbox.image}' to '{new.sandbox.image}'",
        )

    if old.sandbox.cpus != new.sandbox.cpus:
        _add(
            changes,
            "sandbox.cpus",
            Landing.REPLACE,
            _number(old.sandbox.cpus, new.sandbox.cpus),
            Step.CREATE_SANDBOX,
            f"sandbox.cpus changed: {old.sandbox.cpus} to {new.sandbox.cpus}",
        )

    if old.sandbox.memory != new.sandbox.memory:
        _add(
            changes,
            "sandbox.memory",
            Landing.REPLACE,
            _number(memory_mb(old.sandbox.memory), memory_mb(new.sandbox.memory)),
            Step.CREATE_SANDBOX,
            f"sandbox.memory changed: {old.sandbox.memory} to {new.sandbox.memory}",
        )

    old_resident = old.sandbox.max_resident_processes
    new_resident = new.sandbox.max_resident_processes
    if old_resident != new_resident:
        # Contract 01 §6: it rides the channel's `hello` (§3.9), which a new
        # sandbox always delivers, so it is classified as a replacement.
        _add(
            changes,
            "sandbox.max_resident_processes",
            Landing.REPLACE,
            _number(old_resident, new_resident),
            Step.CREATE_SANDBOX,
            f"sandbox.max_resident_processes changed: {old_resident} to {new_resident}",
        )


def _number(old: float | None, new: float | None) -> Direction:
    if old is None or new is None:
        return Direction.CHANGE

    if new > old:
        return Direction.ADD

    return Direction.REMOVE if new < old else Direction.CHANGE


def _flip(direction: Direction) -> Direction:
    if direction is Direction.ADD:
        return Direction.REMOVE

    return Direction.ADD if direction is Direction.REMOVE else direction


def _members(old: Iterable[str], new: Iterable[str]) -> Direction | None:
    """None when the two lists hold the same members. Order never matters: a
    list here is a set written down."""
    before, after = set(old), set(new)
    if before == after:
        return None

    return _from_sets(before, after)


def _from_sets(before: set[str], after: set[str]) -> Direction:
    added, removed = after - before, before - after
    if added and removed:
        return Direction.BOTH

    return Direction.ADD if added else Direction.REMOVE


_GRANT_ALL: Final = ALL_TOOLS


def _tools_direction(
    old: Mapping[str, ToolGrant], new: Mapping[str, ToolGrant]
) -> Direction | None:
    if old == new:
        return None

    servers = _from_sets(set(old), set(new)) if set(old) != set(new) else None
    moves: set[Direction] = {servers} if servers is not None else set()
    for server in set(old) & set(new):
        move = _grant_direction(old[server], new[server])
        if move is not None:
            moves.add(move)

    if not moves:
        return None

    return moves.pop() if len(moves) == 1 else Direction.BOTH


def _grant_direction(old: ToolGrant, new: ToolGrant) -> Direction | None:
    if old == new:
        return None

    if isinstance(old, list) and isinstance(new, list):
        return _members(old, new)

    # `all` resolves against the server file at read time, so a list cannot be
    # compared with it here. Widening to `all` adds; narrowing from it may
    # both add and remove, and the conservative answer is `both`.
    return Direction.ADD if new == _GRANT_ALL else Direction.BOTH


def _verbs_direction(old: FamilyFile, new: FamilyFile) -> Direction:
    before, after = set(old.verbs.granted()), set(new.verbs.granted())
    if before != after:
        return _from_sets(before, after)

    # The same verbs with a different fence. A fence both widens and narrows
    # unless every entry is compared, which the PEP does per call anyway.
    return Direction.BOTH


def _triggers_direction(old: FamilyFile, new: FamilyFile) -> Direction:
    before = {_trigger_key(one) for one in old.triggers or ()}
    after = {_trigger_key(one) for one in new.triggers or ()}
    if before == after:
        return Direction.CHANGE

    return _from_sets(before, after)


def _trigger_key(trigger: object) -> str:
    return repr(trigger)


def _mount_key(mount: FileMount) -> str:
    return mount.path


def _mounts_direction(old: list[FileMount], new: list[FileMount]) -> Direction | None:
    before = {_mount_key(mount): mount.mode for mount in old}
    after = {_mount_key(mount): mount.mode for mount in new}
    if before == after:
        return None

    added = set(after) - set(before)
    removed = set(before) - set(after)
    for path in set(before) & set(after):
        if before[path] == after[path]:
            continue

        # rw to ro removes reach; ro to rw adds it (contract 05 §5.4).
        target = removed if after[path] == "ro" else added
        target.add(path)

    if added and removed:
        return Direction.BOTH

    return Direction.ADD if added else Direction.REMOVE


def _mounts_detail(old: list[FileMount], new: list[FileMount]) -> str:
    before = {mount.path: mount.mode for mount in old}
    after = {mount.path: mount.mode for mount in new}
    parts: list[str] = []
    for path in sorted(set(after) - set(before)):
        parts.append(f"added {path} {after[path]}")

    for path in sorted(set(before) - set(after)):
        parts.append(f"removed {path} {before[path]}")

    for path in sorted(set(before) & set(after)):
        if before[path] != after[path]:
            parts.append(f"{path} {before[path]} to {after[path]}")

    return "mounts changed: " + ", ".join(parts)
