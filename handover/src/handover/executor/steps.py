"""The ten steps, in order (`stage7-releases.md` §2.4).

```
 1 intake      parse the request as hostile bytes (§3.2)
 2 resolve     manifests at their tags + the live state -> contract 06 §9
 3 provenance  P1-P5 per deploying component, plus monotonic
 4 contracts   rules C1 to C5 over the resolved set
 5 approve     one tap, bound to manifest_sha256 (§2.5)
 6 lock        flock: two requests run one after the other
 7 quiesce     no turn mid-start
 8 stage       re-resolve, compare the hash, fetch, build into <to>.new
 9 switch      keep the unit, swap, refresh the unit in place, restart, verify
10 restore     put the previous tree and unit back, or record the success
```

A failed step stops the sequence. Every outcome is a ledger entry, and the
unit still exits 0 — non-zero means the executor could not do its own job.

Steps 8, 9 and 10 loop over the whole SET, in `order`. §2.1 is
why there is no second path for one component: a single component still
changes the resolved set, still needs the contract check and still needs
one approval, so the difference is one field — how many entries read
`action: deploy`.

**What is still refused**, with a fixed reason rather than half-doing it:
`kind: rollback` (open question 6). It is a ledger entry and never a
surprise on the host.

**`handover` is staged here and switched after the ledger** (contract 06
§1.1 rules 2 and 3). Step 9 writes its note and swaps nothing, and
`self_switch.py` does the rest once the entry is written and the lock is
released.
"""

from __future__ import annotations

import errno
import os
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from ..catalog import CATALOG_BY_NAME, Kind
from ..discovery import read_one
from ..errors import Refusal, RefusalCode
from ..manifest import ComponentManifest
from ..mcpserver import ServerFile, read_registry
from ..resolve import Resolution, ResolvedComponent, build_document, deploying_names, resolve
from ..state import ReleaseState
from .approval import (
    APPROVAL_TTL_S,
    Summary,
    Transport,
    action_id_of,
    check_decision,
    contracts_row,
    gate_id,
)
from .host import Host
from .install import (
    UNIT_KEPT_SUFFIX,
    Installer,
    Paths,
    StepFailed,
    VerifyOutcome,
    paths_of,
    remove_tree,
    restorable,
)
from .ledger import Entry, Outcome, Step, StepName, StepStatus
from .live_state import (
    AtCommitFn,
    LiveTree,
    Readers,
    build_state,
    select_manifests,
    write_manifest_stamp,
    write_stamp,
)
from .mcpbuild import Fetched, McpBuilder, ServerBuild
from .notice import Notifier, say_nothing
from .provenance import ApiGetFn, check_monotonic
from .provenance import verify as check_provenance
from .quiesce import Quiesce
from .quiet import NO_WINDOW, BusySeenFn, needs_window, wait_for_quiet
from .request import Kind as RequestKind
from .request import Request
from .roster import restore as restore_roster
from .roster import write as write_roster
from .source import require_trusted
from .spool import SelfNote, Spool, SwitchNote, VerifyHook, acquire, release_lock

#: §2.4 row 6: a wait past this is `failed, step: lock`.
LOCK_WAIT_S: Final = 1800.0
LOCK_POLL_S: Final = 2.0

#: §2.4 row 7.
QUIESCE_WAIT_S: Final = 180.0
QUIESCE_POLL_S: Final = 5.0

#: The C rules are checked inside the resolver, and the ledger must still
#: attribute them to step 4.
CONTRACT_CODES: Final = frozenset({RefusalCode.C1, RefusalCode.C2, RefusalCode.C3, RefusalCode.C4})

#: Contract 06 §1.1: the tool cannot replace the code under its own running
#: process, so step 9 swaps every other component and only notes this one.
SELF_COMPONENT: Final = "handover"

#: The one component whose release also installs the per-server trees
#: (contract 06 §1 row 6, §7). Every branch it takes below is a no-op for
#: every other component, which is why there is no second path through the
#: ten steps: an MCP release is a release, with more to stage.
MCP_COMPONENT: Final = "mcp-servers"

#: Where the registry checkout keeps the server files (contract 01b §1).
#: Root reads them out of the tree an operator-side process keeps current, the
#: same way it reads a component's source: by path, never by fetching.
REGISTRY_REPO: Final = "agent-registry"
REGISTRY_MCP_DIR: Final = "mcp"

#: §3.2's caps, applied to the registry too. Twenty servers is well past
#: the count contract 06 §7 says to revisit the one-component decision at.
MAX_SERVERS: Final = 20

#: Step 9's detail when the set holds `handover`. The entry is written
#: before the swap, so it says the swap is still to come.
SELF_AFTER: Final = f"{SELF_COMPONENT} switches after the ledger (contract 06 §1.1 rule 2)"
NOT_BUILT_ROLLBACK: Final = "kind rollback is not built"
NOTHING_TO_DO: Final = "the request moves no component"

#: §2.6's closed list again: root could not resolve this component's tag to
#: a commit, so it has no source to fetch. A fixed string, because the only
#: values that would vary are a component name and a tag, and both are
#: already in the refusal's subject.
NO_SOURCE_FACTS: Final = "its tag resolves to no commit: contract 06 §9 needs a sha"


def _no_signal() -> float | None:
    """§2.8's default reader: root can reach no session service at all.

    It is never quiet, on purpose. A host whose turns root cannot see is
    not an idle host, it is a host root cannot see (invariant 19's fail
    closed, applied to a wait).
    """
    return None


#: §2.6's `reason` is a fixed string from a closed list, never built from
#: input. These two are the ends step 10 can reach without restoring.
MANUAL_RESTORE: Final = "restore mode is manual"
NO_PLAN: Final = "the switch ran with no resolved plan"
NOTHING_SWAPPED: Final = "the switch failed before it swapped anything"

#: A step raises `Refusal` or `StepFailed` for each end it knows. This is
#: every other error. §2.6: a `reason` is never built from input, and the
#: text of an error can hold a path or the bytes of a file. So the ledger
#: gets the type of the error, and never its text.
UNNAMED_ERROR: Final = "an error this step does not name"

#: The directory of this package. A frame under it is this tool's own code.
_PACKAGE_DIR: Final = f"{Path(__file__).parent.parent}{os.sep}"
NO_OWN_FRAME: Final = "no code of this package"


def _unnamed(error: Exception) -> str:
    """§2.6's `detail` for an error no step names: its type, and the symbol
    of its number for an `OSError` that carries one.

    The number of an `OSError` that is built from two texts is the first
    text. Only a whole number goes on, so no text of an error does."""
    kind = type(error).__name__
    number: object = error.errno if isinstance(error, OSError) else None
    if not isinstance(number, int):
        return f"{UNNAMED_ERROR}: {kind}"

    return f"{UNNAMED_ERROR}: {kind} ({errno.errorcode.get(number, number)})"


def _raised_at(error: Exception) -> str:
    """The deepest frame of this package the error passed: file name, line
    and function. Read off the code objects, so it holds no input."""
    found = NO_OWN_FRAME
    for frame, line in traceback.walk_tb(error.__traceback__):
        code = frame.f_code
        if code.co_filename.startswith(_PACKAGE_DIR):
            found = f"{Path(code.co_filename).name}:{line} in {code.co_name}"

    return found


def remove_staged(path: Path, name: str, entry: Entry) -> None:
    """One `<install.to>.new` a run is finished with, and the ledger line
    that says what happened to it.

    A tree that goes says so in the log. A tree that will NOT go is a
    `manual` line carrying the exact command root must run, because the
    only thing worse than a leftover is a leftover nobody is told about:
    the next release stages into the same name and dies on it, and so
    does the cutover, with `rm: cannot remove 'noticeboard.new': Permission
    denied` and no ledger entry anywhere naming the path.

    Public, and not a method, because the crash repair leaves the same
    tree in the same place and two expressions of one rule is how two
    releases end up with two answers.
    """
    if not path.exists() and not path.is_symlink():
        return

    try:
        remove_tree(path)
    except OSError as exc:
        entry.manual.append(
            f"{name}: cannot remove {path} ({exc.strerror}); as root: rm -rf {path}"
        )

        return

    entry.say(f"removed {path}")


def _hook_of(manifest: ComponentManifest) -> VerifyHook:
    """Contract 06 §4's three fields, as a note carries them."""
    return VerifyHook(
        command=tuple(manifest.verify.command),
        user=str(manifest.verify.user),
        timeout_s=manifest.verify.timeout_s,
    )


def verify_row(outcome: VerifyOutcome) -> dict[str, object]:
    """§2.6's `verify` entry, one per hook step 9 or a repair ran.

    `detail` is the hook's own report, so a reader of `done/<id>.json`
    learns what a failed hook said and not only that it failed. Built in one
    place because `Release._record_verify` and `_restart_restored` (a
    repair has no `Release`) both write this same row."""
    return {
        "component": outcome.component,
        "status": "ok" if outcome.ok else "failed",
        "seconds": outcome.seconds,
        "detail": outcome.detail,
    }


@dataclass(frozen=True)
class Wiring:
    """Everything the steps reach the world through. A test fakes all of it."""

    host: Host
    transport: Transport
    #: Where step 2 learns the two things the install trees cannot say: what
    #: `latest` names, and what commit a tag is. Contract 06 §11's document
    #: is BUILT from these, never read off disk.
    readers: Readers = field(default_factory=Readers)
    #: None means root's environment held no token, which fails step 3
    #: closed rather than skipping it.
    api: ApiGetFn | None = None
    #: §2.6's outcome push. Its default is silence, which is safe: a push
    #: that never goes out cannot approve anything.
    notify: Notifier = say_nothing
    #: §2.8's signal: when root last saw a turn in flight. The default
    #: answers None, which is never quiet — a host whose session service
    #: root cannot reach is a host root cannot say is idle.
    last_busy_seen: BusySeenFn = _no_signal


@dataclass
class Plan:
    """What steps 2 to 4 decided, and step 8 re-checks."""

    resolution: Resolution
    manifests: dict[str, ComponentManifest]
    document: dict[str, object]
    #: Every component this release deploys, in `order` (contract 06 §9).
    #: One entry needs no branch of its own: a single
    #: component still changes the resolved set, still needs the contract
    #: check and still needs one approval (§2.1).
    order: tuple[str, ...]

    def manifest(self, name: str) -> ComponentManifest:
        return self.manifests[name]

    def hash(self) -> str:
        return str(self.document["manifest_sha256"])

    def resolved_at(self) -> float:
        stamp = self.document["resolved_at"]

        return float(stamp) if isinstance(stamp, int | float) else 0.0

    def _row(self, name: str) -> ResolvedComponent:
        return next(one for one in self.resolution.components if one.name == name)

    def to_version(self, name: str) -> str:
        return str(self._row(name).to_version)

    def from_version(self, name: str) -> str | None:
        return self._row(name).from_version

    def tag(self, name: str) -> str:
        return str(self._row(name).tag())

    def sha(self, name: str) -> str:
        entry = next(one for one in _component_rows(self.document) if one.get("name") == name)

        return str(entry.get("sha"))

    def moves(self) -> list[str]:
        """`chaperone 2.0.3 → 2.1.0`, one per deploying component, in order."""
        return [
            f"{name} {self.from_version(name) or 'absent'} → {self.to_version(name)}"
            for name in self.order
        ]


def _component_rows(document: dict[str, object]) -> list[dict[str, object]]:
    rows = document.get("components")
    if not isinstance(rows, list):
        return []

    listed = cast("list[object]", rows)

    return [cast("dict[str, object]", one) for one in listed if isinstance(one, dict)]


class Release:
    """One request's walk through the ten steps."""

    def __init__(self, request: Request, wiring: Wiring, spool: Spool) -> None:
        self.request = request
        self.wiring = wiring
        self.spool = spool
        self.entry = Entry(id=request.id, kind=str(request.kind), requested_by=request.requested_by)
        self.installer = Installer(wiring.host)
        self.mcp = McpBuilder(wiring.host, wiring.host.mcp_root)
        #: The servers this release stages, and what staging each produced.
        #: Both stay empty unless the set holds `mcp-servers`.
        self.servers: tuple[ServerFile, ...] = ()
        self.builds: tuple[ServerBuild, ...] = ()
        #: Server trees that actually swapped. Step 10 puts back exactly
        #: these, for the same reason `deployed` exists for components.
        self.swapped: list[ServerBuild] = []
        #: Whether this release replaced the PEP's upstream roster. Step 10
        #: puts it back only if it did: a `.prev` from an EARLIER release is
        #: not this one's to restore.
        self.roster_written = False
        self.plan: Plan | None = None
        #: One entry per component that STAGED, by name.
        self.paths: dict[str, Paths] = {}
        #: What actually swapped, in switch order. Step 10 restores exactly
        #: these, in reverse (contract 06 §5.1).
        self.deployed: list[str] = []
        #: The unit file each switch kept before replacing it, by component
        #: (`install.py` rule 6). Step 10 puts back exactly these: a
        #: `<unit>.prev` an EARLIER release left is not this one's.
        self.units: dict[str, Path] = {}
        #: The sibling unit files each switch kept, by component
        #: (`install.py` rule 8). Step 10 puts back exactly these.
        self.siblings: dict[str, tuple[Path, ...]] = {}
        #: The siblings that ran when each switch began. Step 10 starts
        #: exactly these again, whatever state the new tree left them in.
        self.running: dict[str, tuple[Path, ...]] = {}
        self.contract_refusal: Refusal | None = None
        self.approved_gate = ""
        #: Components whose manifest root read at a SHA the live-state
        #: document supplied, because no release has stamped one into their
        #: install tree yet. §2.5's `review` field names the count, so
        #: the operator reads it before the tap.
        self.unverified: list[str] = []
        #: What the install roots held when step 2 read them, once. Both the
        #: state builder and the manifest reader work from this, so the two
        #: never give different answers about one tree.
        self.trees: dict[str, LiveTree] = {}
        #: Every component whose source this release has cloned, in the
        #: order it cloned them. Row 5 of `_read_manifests` reads out of
        #: these and out of nothing else.
        self.fetched: list[str] = []
        #: What step 8's compose dry run said each `kind: compose`
        #: component would recreate. Step 9 reads it for §2.8's window.
        self.recreates: dict[str, tuple[str, ...]] = {}
        #: `handover`'s switch, noted at step 9 and performed by the
        #: drain after the entry and the lock (contract 06 §1.1 rule 2).
        self.self_note: SelfNote | None = None
        #: The component whose moves raised an error no step names: the
        #: swap of step 9, or the moves back of step 10. `_swap` has the
        #: reason.
        self.interrupted: str | None = None
        self._lock: list[int] = []

    # -- the sequence ---------------------------------------------------

    def run(self) -> Outcome:
        """Every step, then the sweep that must happen whatever they did."""
        outcome = self._walk()
        if outcome is not Outcome.SUCCEEDED:
            self._remove_staged()

        return outcome

    def _walk(self) -> Outcome:
        """Every step, stopping at the first that does not pass."""
        ordered: list[tuple[StepName, Callable[[], str]]] = [
            (StepName.INTAKE, self._intake),
            (StepName.RESOLVE, self._resolve),
            (StepName.PROVENANCE, self._provenance),
            (StepName.CONTRACTS, self._contracts),
            (StepName.APPROVE, self._approve),
            (StepName.LOCK, self._lock_spool),
            (StepName.QUIESCE, self._quiesce),
            (StepName.STAGE, self._stage),
        ]
        for name, action in ordered:
            if self.contract_refusal is not None and name is StepName.PROVENANCE:
                self._skip(name, "the contract check refused this set")
                continue

            if not self._step(name, action):
                return self._stopped()

        return self._switch_and_settle()

    def _remove_staged(self) -> None:
        """A release that did not land leaves no tree behind (§2.4 row 8).

        Every `<install.to>.new` this run made goes, for every component
        and every MCP server it staged. Three ends reach this, and all
        three leave the same directory full:

        1. refused or failed BEFORE the swap — the staged tree is there,
        2. failed AT the swap — the components after the failing one are,
        3. restored after it — `swap_back` moves each failed tree to
           `.new`, which is where it was left.

        A kept tree stops the next cutover, which stages into the same
        `<install.to>.new` as the operator: a `root:root 750` tree in the operator's home
        is `rm: cannot remove 'noticeboard.new': Permission denied`. Nothing here is
        unrecoverable: every tree is rebuilt from a SHA the ledger records.

        `keep` (contract 06 §5) is untouched. That is the `.prev` of a
        release that SUCCEEDED, and this removes neither.
        """
        for name, paths in self.paths.items():
            remove_staged(paths.new, name, self.entry)

        for build in self.builds:
            remove_staged(build.paths.new, build.name, self.entry)

    def _switch_and_settle(self) -> Outcome:
        """Steps 9 and 10. A failed verify is the executor's job, not
        the operator's at 02:00 (contract 06 §5)."""
        if self._step(StepName.SWITCH, self._switch):
            self._step(StepName.RECORD, self._record)

            return Outcome.SUCCEEDED

        if self.entry.refused_check is not None:
            return Outcome.REFUSED

        return self._restore_or_stop()

    def _restore_or_stop(self) -> Outcome:
        """Contract 06 §5.2's table, over the whole set.

        ANY deployed component with `mode: manual` stops the restore, not
        only the failing one: a forward-only migration is not reversed by
        reinstalling a previous artifact. The contract check ran against
        ONE resolved set, so restoring a subset would land the host in a
        combination nobody resolved, checked or approved (§5.1).
        """
        plan = self.plan
        if not self.deployed:
            # The switch failed before it swapped anything — a quiet window
            # that never came (§2.8), or a build that vanished. Nothing on
            # the host changed, so there is nothing to put back.
            self._skip(StepName.RESTORE, NOTHING_SWAPPED)

            return Outcome.FAILED

        blocked = self._manual_components(plan)
        if plan is None or blocked:
            reason = NO_PLAN if plan is None else f"{MANUAL_RESTORE}: {', '.join(blocked)}"
            self._skip(StepName.RESTORE, reason)
            self.entry.manual.append(f"restore: {reason}")

            return Outcome.FAILED

        if self._step(StepName.RESTORE, self._restore):
            return Outcome.RESTORED

        return Outcome.FAILED

    def _manual_components(self, plan: Plan | None) -> list[str]:
        if plan is None:
            return []

        return [name for name in self.deployed if not restorable(plan.manifest(name))]

    def _stopped(self) -> Outcome:
        return Outcome.REFUSED if self.entry.refused_check else Outcome.FAILED

    def _step(self, name: StepName, action: Callable[[], str]) -> bool:
        started = self.wiring.host.clock()
        status, detail = StepStatus.OK, ""
        try:
            detail = action()
        except Refusal as refusal:
            status, detail = StepStatus.REFUSED, refusal.detail
            self.entry.refused_check = str(refusal.code)
            self.entry.reason = refusal.detail
            # §2.6's `reason` is the DETAIL, and the detail alone cannot
            # say which file said it. One `mcp-servers` release reads
            # eleven `mcp/<name>/server.yaml`, so "pins v0.4.6, this
            # release deploys mcp-servers-v0.1.0" alone names nothing a
            # reader of `done/<ULID>.json` could open. The subject
            # is already `safe_token`ed where the refusal is raised, so
            # this adds no untrusted text to the ledger, and it goes in
            # the LOG rather than in `reason`: `reason` is what
            # `caregiver` folds into its marker and what the phone push
            # carries, and both are bounded on purpose.
            self.entry.say(refusal.as_line())
        except StepFailed as failure:
            status, detail = StepStatus.FAILED, failure.detail
            self.entry.reason = f"{name}: {failure.detail}"
        except Exception as error:
            if self.interrupted is not None:
                raise

            # Every outcome is a ledger entry (§2.4). An error that left
            # here wrote none, kept every staged tree and ended the pass.
            status, detail = StepStatus.FAILED, _unnamed(error)
            self.entry.reason = f"{name}: {detail}"
            self.entry.say(f"step {name}: {type(error).__name__} raised at {_raised_at(error)}")

        seconds = round(self.wiring.host.clock() - started, 1)
        self.entry.add(Step(str(name), str(status), seconds, detail))
        self.entry.say(f"step {name}: {status} — {detail}")

        return status is StepStatus.OK

    def _skip(self, name: StepName, why: str) -> None:
        self.entry.add(Step(str(name), str(StepStatus.SKIPPED), 0.0, why))
        self.entry.say(f"step {name}: skipped — {why}")

    def release_lock(self) -> None:
        for fd in self._lock:
            release_lock(fd)

        self._lock.clear()

    # -- 1 intake -------------------------------------------------------

    def _intake(self) -> str:
        """The bytes are already validated (`spool.read_request`). What is
        left is the kind root does not perform: `rollback`."""
        if self.request.kind is not RequestKind.RELEASE:
            raise Refusal(RefusalCode.REQUEST, "request", NOT_BUILT_ROLLBACK)

        return f"{len(self.request.components)} component(s)"

    # -- 2 resolve ------------------------------------------------------

    def _resolve(self) -> str:
        state = self._build_state()
        manifests = self._read_manifests(state)
        try:
            resolution = resolve(manifests, state, self.request.wanted())
        except Refusal as refusal:
            if refusal.code not in CONTRACT_CODES:
                raise

            # Step 4 reports it, because only its message says what to do.
            self.contract_refusal = refusal

            return "set resolved, contract check pending"

        plan = self._plan_of(resolution, manifests, state)
        self.plan = plan
        for name in self.unverified:
            self.entry.manual.append(f"manifest read at this release's commit: {name}")

        # Contract 06 §3.2's C1, second half: a requirement whose contract
        # has no provider in the set. Reported, never refused, and named
        # here so `done/<ULID>.json` says what root could not check.
        for item in resolution.unprovided:
            self.entry.manual.append(item.line())

        self.entry.manifest = plan.document
        # §2.6's `previous` is what a rollback targets, so it carries every
        # component this release moves, not only the first.
        self.entry.previous = {name: plan.from_version(name) for name in plan.order}

        return ", ".join(plan.moves())

    def _build_state(self) -> ReleaseState:
        """Contract 06 §11's document, built by root out of its own reads.

        Nothing is read off disk: a document file would be an operator-written
        input root parses on its way to an install.

        Each note the builder returns is a thing root could not establish.
        They go in the ledger and never into a decision: a missing tag makes
        `latest` name nothing, and `resolve` refuses that with its own
        reason.
        """
        built = build_state(
            self.wiring.host.install_roots, self.request.wanted(), self.wiring.readers
        )
        self.trees = built.trees
        for line in built.notes:
            self.entry.say(f"live state: {line}")

        return built.state

    def _stamp_of(self, name: str) -> str | None:
        """What is stamped on the live tree, from step 2's one walk."""
        return self.trees.get(name, LiveTree()).version

    def _read_manifests(self, state: ReleaseState) -> dict[str, ComponentManifest]:
        """§2.4 row 2, with contract 06 §10.2's rule applied.

        **Root believes a manifest at a SHA it verifies, or out of a tree it
        owns. There is no third source.** Only the deploying component is
        tied to a verified tag by P2 at step 3, and the other manifests
        feed the contract table: read at a SHA an operator-written document
        supplied, a requester that DROPPED a `requires` entry from a
        consumer's manifest could ship a breaking major bump with rule C4
        satisfied.

        Five sources, in the order this method tries them.

        1. **A clone at the resolved SHA**, when the component deploys.
           Root believes everything, because step 3 ties that SHA to a
           bot-cut Release.
        2. **The tree's stamped manifest**, when it does not deploy and has
           one. Root believes everything: it wrote that file itself, into
           a tree it owns, at the last approved release.
        3. **Nothing**, when it has no install tree at all. It declares
           nothing, because an interface nobody runs cannot be broken by
           this release.
        4. **A clone at its live version's tag**, when it does not deploy,
           is stamped with a version, and has no stamped manifest.
        5. **A clone THIS release already fetched and verified**, when it
           does not deploy, is installed, and carries no stamp at all — so
           root cannot name its version and has no tag of its own to
           resolve.

        Rows 4 and 5 are the upgrade path, and row 5 is the one the host's
        first release met: `bin/rework-cutover.sh` installed every tree and
        stamped none.
        Without it, eight installed components read as "not installed",
        declared nothing, and `noticeboard` — which requires three contracts — was
        refused at step 4 with C1, "the set provides it nowhere". The
        first release was impossible for that reason as well as for the
        missing document.

        What row 5 costs, said plainly: the manifest comes from the
        DEPLOYING component's commit, not from the commit the installed
        version was built at. Root verified that commit, so the file is not
        a claim anybody planted — it may simply be newer than what runs.
        Every such component is named in the ledger and counted in §2.5's
        `review` field as `suspect` before the operator taps, and the window
        closes component by component: each release stamps the tree it
        deploys.

        The TABLE lives in `live_state.select_manifests`, and the requester
        runs the same function over the same `trees` with its own source
        for "the manifest at a commit". Two implementations of that
        decision is how a preview promises what root refuses.
        """
        deploying = deploying_names(state, self.request.wanted())
        chosen = select_manifests(deploying, self.trees, self._at_commit(state, deploying))
        self.unverified = list(chosen.unverified)
        for name in chosen.unverified:
            self.entry.say(f"installed and unstamped, read at this release's commit: {name}")

        for name in chosen.unreadable:
            self.entry.manual.append(f"installed and unstamped, nothing root can read: {name}")

        return chosen.manifests

    def _at_commit(self, state: ReleaseState, deploying: frozenset[str]) -> AtCommitFn:
        """Root's source for a manifest at a commit, per §10.2's rows.

        A component that deploys, and one that is stamped with a version,
        each resolve a tag of their OWN. One that carries no stamp has no
        tag to resolve, so it is read out of a clone this release already
        fetched — the commit step 3 verifies for the deploying component.
        """

        def at_commit(name: str) -> ComponentManifest | None:
            if name in deploying or self._stamp_of(name) is not None:
                return self._fetched_manifest(name, state)

            return self._from_a_fetched_clone(name)

        return at_commit

    def _from_a_fetched_clone(self, name: str) -> ComponentManifest | None:
        """Row 5: this component's manifest out of a clone this release
        already fetched at a SHA step 3 verifies, when the two share a
        repository. None when no such clone exists."""
        repo = CATALOG_BY_NAME[name].repo
        for other in sorted(self.fetched):
            if CATALOG_BY_NAME[other].repo is not repo:
                continue

            clone = self.wiring.host.work_root / self.request.id / other
            try:
                return read_one(clone, name).manifest
            except Refusal:
                return None

        return None

    def _fetched_manifest(self, name: str, state: ReleaseState) -> ComponentManifest:
        """One clone per component, because each resolves to its own SHA.

        Each clone is a WHOLE repository, and seven components share
        `agent-control`: a walk of every clone would find each manifest
        seven times and refuse the set. `read_one` takes the one file the
        catalog places in that clone.
        """
        facts = state.facts.get(name)
        if facts is None or facts.sha is None:
            detail = NO_SOURCE_FACTS
            raise Refusal(RefusalCode.STATE, name, detail)

        into = self.wiring.host.work_root / self.request.id / name
        self.installer.fetch(str(CATALOG_BY_NAME[name].repo), facts.sha, into)
        self.fetched.append(name)

        return read_one(into, name).manifest

    def _stamped_manifest(self, name: str) -> ComponentManifest | None:
        """What the last approved release stamped into the live tree, as
        step 2's one walk of the install roots already parsed it."""
        return self.trees.get(name, LiveTree()).manifest

    def _plan_of(
        self,
        resolution: Resolution,
        manifests: dict[str, ComponentManifest],
        state: ReleaseState,
    ) -> Plan:
        """The whole set, in `order`. §2.1: a release of ONE component and
        a release of a SET are the same flow, and the difference is one
        field — how many entries read `action: deploy`.
        """
        if not resolution.deploying:
            raise Refusal(RefusalCode.REQUEST, "request", NOTHING_TO_DO)

        document = build_document(
            resolution,
            state,
            self.request.id,
            self.request.requested_by,
            self.wiring.host.clock(),
        )

        return Plan(resolution, manifests, document, resolution.order)

    # -- 3 provenance ---------------------------------------------------

    def _provenance(self) -> str:
        """P1 to P5 per deploying component, plus the monotonic rule.

        Every component in the set, not only the first: one unverified
        entry in a set of three is a set nobody verified.
        """
        plan = self._require_plan()
        if self.wiring.api is None:
            raise Refusal(RefusalCode.P1, plan.order[0], "root holds no GitHub token")

        found: list[str] = []
        for name in plan.order:
            manifest = plan.manifest(name)
            reply = check_provenance(
                self.wiring.api, name, str(manifest.repo), plan.tag(name), plan.sha(name)
            )
            check_monotonic(name, plan.to_version(name), plan.from_version(name))
            found.append(f"{name} PR #{reply.pr}")

        return f"P1-P5 pass: {', '.join(found)}"

    # -- 4 contracts ----------------------------------------------------

    def _contracts(self) -> str:
        contract_refusal = self.contract_refusal
        if contract_refusal is not None:
            raise contract_refusal

        plan = self._require_plan()
        unprovided = plan.resolution.unprovided
        if not unprovided:
            return f"{len(plan.resolution.contracts)} contracts, 0 refused"

        # C1's second half. The count is in the step's own detail as well
        # as in `manual`, because a reader of `done/<ULID>.json` reads the
        # steps first and must not have to infer it from a list further
        # down (contract 06 §3.2).
        return (
            f"{len(plan.resolution.contracts)} contracts, 0 refused, "
            f"{len(unprovided)} requirement(s) not verified"
        )

    # -- 5 approve ------------------------------------------------------

    def _approve(self) -> str:
        plan = self._require_plan()
        self.approved_gate = gate_id(plan.hash())
        # The action id is this release's own id, lower-cased, because that
        # is the only shape the live phone flow accepts (`phone.ACTION_RE`).
        action_id = action_id_of(self.request.id)
        decision = self.wiring.transport(
            action_id, self.approved_gate, self._summary(plan), APPROVAL_TTL_S
        )
        self.entry.approved_at = check_decision(
            decision, self.approved_gate, self.wiring.host.clock()
        )

        return f"gate {self.approved_gate}"

    def _summary(self, plan: Plan) -> Summary:
        """§2.5's seven fields, built from root's OWN resolution. The
        requester cannot show the operator one thing and deploy another.

        Every field is per SET now. `restore` names the components that
        would stop a restore, because §2.5 says `restore: manual` on the
        phone is the signal that a failed verify will stop rather than
        reverse — and the operator must see it before the tap, not after.
        """
        manual = [name for name in plan.order if not restorable(plan.manifest(name))]
        units = [plan.manifest(name).unit or f"{name}: no unit" for name in plan.order]

        return Summary(
            review=self._review(plan),
            components="; ".join(plan.moves()),
            contracts=contracts_row(plan.resolution),
            restarts=", ".join(units),
            restore="automatic" if not manual else f"manual: {', '.join(manual)}",
            requested_by=self.request.requested_by,
            manifest=plan.hash().removeprefix("sha256:")[:12],
        )

    def _review(self, plan: Plan) -> str:
        """§2.5's first field: the adversarial verdict, before anything
        else. An unstamped manifest is the one thing root read at a SHA it
        did not verify, so it says `suspect` and names the count."""
        if not self.unverified:
            kinds = sorted({str(plan.manifest(name).kind) for name in plan.order})

            return f"safe: {len(plan.order)} component(s), {'/'.join(kinds)}"

        named = ", ".join(sorted(self.unverified))

        return f"suspect: {len(self.unverified)} manifest(s) not verified ({named})"

    # -- 6 lock ---------------------------------------------------------

    def _lock_spool(self) -> str:
        fd = self.spool.open_lock()
        waited = acquire(
            fd, LOCK_WAIT_S, LOCK_POLL_S, self.wiring.host.clock, self.wiring.host.sleep
        )
        if waited is None:
            os.close(fd)
            raise StepFailed(f"no lock in {LOCK_WAIT_S:.0f}s")

        self._lock.append(fd)

        return f"waited {waited:.1f}s"

    # -- 7 quiesce ------------------------------------------------------

    def _quiesce(self) -> str:
        # One watcher for the whole wait: a turn root has been watching for
        # longer than the launch grace stops counting, whatever its own
        # `started_at` says.
        watcher = Quiesce()
        waited = 0.0
        while True:
            busy = watcher.starting(self.wiring.host.sessions_root, self.wiring.host.clock())
            if not busy:
                return f"no turn mid-start (waited {waited:.0f}s)"

            if waited >= QUIESCE_WAIT_S:
                raise StepFailed(f"{len(busy)} turn(s) still mid-start")

            self.wiring.host.sleep(QUIESCE_POLL_S)
            waited += QUIESCE_POLL_S

    # -- 8 stage --------------------------------------------------------

    def _stage(self) -> str:
        """Re-resolve, compare with what was approved, then build.

        The compare is §2.5's second consequence: an approval cannot be
        replayed onto a changed set. Any drift is refused AFTER the tap,
        fail closed and audited, with nothing swapped.
        """
        plan = self._require_plan()
        state = self._build_state()
        again = resolve(plan.manifests, state, self.request.wanted())
        # The same clock as step 2, so the compare is over the SET and not
        # over the seconds between the two resolutions.
        document = build_document(
            again, state, self.request.id, self.request.requested_by, plan.resolved_at()
        )
        if str(document["manifest_sha256"]) != plan.hash():
            raise Refusal(RefusalCode.DRIFT, "manifest", "the set changed after the approval")

        # Every component is built before any is swapped (§2.4 row 8): a
        # failed stage removes the `.new` trees and NOTHING on the host has
        # changed. That property is what makes a set safe to order.
        for name in plan.order:
            self._stage_one(plan, name)

        return f"staged in order: {', '.join(plan.order)}"

    def _stage_one(self, plan: Plan, name: str) -> None:
        manifest = plan.manifest(name)
        paths = paths_of(manifest, self.wiring.host.install_roots)
        source = self.wiring.host.work_root / self.request.id / name
        # Before the build and long before the swap: a release whose unit
        # starts a program outside the tree it installs would swap, restart,
        # verify out of the new tree and change nothing that runs. Refusing
        # here leaves the host exactly as it was (contract 06 §1 rule 8).
        self.installer.check_unit_binds(manifest, source)
        self.paths[name] = paths
        self.installer.build(manifest, source, paths)
        write_stamp(paths.new, plan.to_version(name))
        # The manifest goes into the artifact beside the version stamp, so
        # the NEXT release reads this component's interface out of a tree
        # root owns rather than at a SHA a document claimed.
        write_manifest_stamp(paths.new, read_one(source, name).text)
        # The unit file goes into the artifact too, so step 9 installs it
        # from a tree its owner can read. The clone is root's `0750`, and a
        # user unit is installed as the operator.
        self.installer.stage_unit(manifest, source, paths.new)
        # After everything this stage writes, so the pass sees the
        # two stamps and the unit file too. The release unit's own umask
        # otherwise leaves the tree unreadable by the user whose unit runs it.
        self.installer.normalize_modes(paths.new)
        if manifest.kind is Kind.COMPOSE:
            # §2.8: the dry run is the only thing that knows what compose
            # would recreate, and it needs the fetched files. Step 9 reads
            # this to decide whether it must wait for a quiet window.
            self.recreates[name] = self.installer.compose_recreates(manifest, source)

        if name == MCP_COMPONENT:
            self._stage_servers(plan, source)

    def _stage_servers(self, plan: Plan, source: Path) -> None:
        """§4.2, once per declared server. Still step 8: every `<name>.new`
        is built and NOTHING is swapped, so a hash that does not match
        leaves the host exactly as it was (`mcpbuild` assumption 10).

        `source` is the `agent-mcp` tree this release already fetched at
        the approved SHA. A `source: agent-mcp` server installs from it,
        which is why root needs no second fetch after the tap, and each
        `pypi` server installs from the closure committed beside its own
        declaration in the registry checkout (contract 01b §3.4).
        """
        # The corpus, not the platform root. The platform root is a rw
        # sandbox mount, so the declarations that decide which `mcp-<name>`
        # runs which pinned package would come out of a directory a model
        # could rewrite between the tap and the build. The trust check
        # runs here too, because this read is not a git call and would
        # otherwise be the one door left open.
        registry = self.wiring.host.source_root / REGISTRY_REPO
        require_trusted(
            REGISTRY_REPO,
            registry,
            self.wiring.host.source_owner_uid,
            self.wiring.host.source_owner_gid,
        )
        self.servers = read_registry(registry / REGISTRY_MCP_DIR, MAX_SERVERS)
        work = self.wiring.host.work_root / self.request.id / REGISTRY_MCP_DIR
        fetched = Fetched(registry=registry, tree=source, tag=plan.tag(MCP_COMPONENT))
        self.builds = self.mcp.stage_all(
            self.servers, work, fetched, traversable_from=self.wiring.host.work_root
        )
        for build in self.builds:
            # The ledger says what was installed and the closure root
            # resolved for it. Names and hashes only: a pin is a public
            # fact, and no `run.env` value is read at all (invariant 13).
            self.entry.say(f"staged {build.name}: {build.artifact}")
            for line in build.closure:
                self.entry.say(f"  closure {build.name}: {line}")
            if build.state_dir is not None:
                self.entry.say(f"  state {build.name}: {build.state_dir}")

    # -- 9 switch -------------------------------------------------------

    def _switch(self) -> str:
        """Each component in `order`: quiet window, swap, unit, restart,
        verify. The first failure stops the loop and step 10 acts on what
        is already deployed.

        `handover` is last in `order` (contract 06 §1.1 rule 1) and is
        only noted here. Every other component is live and verified by the
        time the note is written, so a set that fails never reaches it.
        """
        plan = self._require_plan()
        live = [name for name in plan.order if name != SELF_COMPONENT]
        for name in live:
            self._switch_one(plan, name)

        said = [f"live in order: {', '.join(live)}"] if live else []
        if SELF_COMPONENT in plan.order:
            self._note_self(plan)
            said.append(SELF_AFTER)

        return "; ".join(said)

    def _note_self(self, plan: Plan) -> None:
        """Rule 2's half that runs under the lock. A visit that takes the
        lock after this run finds the note and refuses to rebuild."""
        manifest = plan.manifest(SELF_COMPONENT)
        paths = self._require_paths(SELF_COMPONENT)
        note = SelfNote(
            component=SELF_COMPONENT,
            to=str(paths.to),
            prev=str(paths.prev),
            to_version=plan.to_version(SELF_COMPONENT),
            from_version=plan.from_version(SELF_COMPONENT),
            requested_by=self.request.requested_by,
            verify=_hook_of(manifest),
        )
        self.spool.note_self(self.request.id, note)
        self.self_note = note
        self.entry.say(f"staged {SELF_COMPONENT} {note.to_version}: {SELF_AFTER}")

    def _switch_one(self, plan: Plan, name: str) -> None:
        manifest = plan.manifest(name)
        paths = self._require_paths(name)
        self._wait_for_window(name)
        kept = self._keep_unit(name, manifest, paths.new)
        siblings = self._keep_siblings(name, manifest, paths.new)

        # Before the move, never after: a crash in between is repairable
        # only if the record already exists (§2.4 row 10). The hook goes in
        # too, because a repair has to verify the version it puts back and
        # the run that resolved the manifest is the one that crashed.
        # So does the kept unit, for the same reason: the
        # unit moves after the tree, and only this run knows where the old
        # one went.
        note = SwitchNote(
            name,
            str(paths.to),
            str(paths.prev),
            manifest.unit,
            _hook_of(manifest),
            None if kept is None else str(kept),
            tuple(str(one) for one in siblings),
        )
        self.spool.note_switch(self.request.id, note)
        self._swap(name, paths)

        # From the LIVE tree, never the clone: the unit's owner installs it
        # (`install.py` rule 7).
        manual = self.installer.refresh_unit(manifest, paths.to)
        if manual is not None:
            self.entry.manual.append(manual)

        # `install.py` rule 8: the siblings go in with the main unit. Which
        # ones run is asked BEFORE the restart, so a stopped door stays
        # stopped, and each running one restarts after the main restart's
        # `daemon-reload`. One that does not settle fails the switch.
        #
        # EVERY running sibling restarts, its file changed or not: the tree
        # under it changed, and one that is not `PartOf=` the main unit
        # would go on running the old code.
        self.installer.refresh_siblings(manifest, paths.to, siblings)
        running = self.installer.active_units(self.installer.sibling_units(manifest, paths.to))
        self.running[name] = running
        self.installer.restart(manifest)
        self.installer.try_restart(running)
        self._reload_upstreams(name)
        outcome = self.installer.verify(manifest)
        self._record_verify(outcome)
        if not outcome.ok:
            raise StepFailed(f"{name} verify failed")

    def _swap(self, name: str, paths: Paths) -> None:
        """The moves that the switch note is written for.

        An error that no step names leaves the run from here, as it did
        before `_step` caught every other one. The run cannot say which
        tree is live, so it ends as a crash does: it writes no entry, the
        note stays, and the next run repairs (§2.4 row 10).
        `self_switch.switch_in` has the same rule for `handover`'s own swap.

        CONTRACT-QUESTION: `stage7-releases.md` §2.4 rows 9 and 10 name no
        end for an error between the switch note and the end of the swap.
        The reading taken changes nothing here. The other reading restores
        in this run and writes the entry. That costs a restore that can
        tell a tree that moved from a tree that did not: `swap_in` removes
        `.prev` first, and a restore from a `.prev` that is half removed
        replaces the live tree with it.

        The same rows name no end for an error inside the moves of the
        restore. `_move_back` takes the same reading there.
        """
        try:
            self._swap_servers(name)
            self.installer.swap_in(paths)
        except (Refusal, StepFailed):
            raise
        except Exception:
            self.interrupted = name
            raise

        self.deployed.append(name)

    def _keep_unit(self, name: str, manifest: ComponentManifest, staged: Path) -> Path | None:
        """`install.py` rule 6, before the note: the unit file this switch
        is about to replace, kept beside itself for step 10."""
        kept = self.installer.keep_unit(manifest, staged)
        if kept is None:
            return None

        self.units[name] = kept
        self.entry.say(f"unit kept: {manifest.unit} at {kept}")

        return kept

    def _keep_siblings(
        self, name: str, manifest: ComponentManifest, staged: Path
    ) -> tuple[Path, ...]:
        """`install.py` rule 8, before the note: every sibling unit this
        switch is about to replace, kept beside itself for step 10."""
        kept = self.installer.keep_siblings(manifest, staged)
        self.siblings[name] = kept
        for one in kept:
            self.entry.say(f"sibling kept: {one.name.removesuffix(UNIT_KEPT_SUFFIX)} at {one}")

        return kept

    def _swap_servers(self, name: str) -> None:
        """The per-server trees go live before the component's own, so the
        `mcp-servers` verify hook reads the versions this release built."""
        if name != MCP_COMPONENT:
            return

        for build in self.builds:
            self.mcp.swap_in(build)
            self.swapped.append(build)
            self.entry.say(f"live {build.name}: {build.artifact}")

        self._write_roster()

    def _write_roster(self) -> None:
        """§4.4 step 1's first half: root writes the upstream set.

        It runs AFTER the trees are live and BEFORE the signal, because the
        roster names commands inside those trees and the PEP probes each
        added upstream the moment it re-reads the file.

        It exists because nothing else writes the roster: without it, the
        signal made the PEP re-read an unchanged file and compute
        `added=()`. The servers were installed and never served, and
        §4.1's "one file, one secret, one tap" was that plus a hand-edit
        of a second repository.
        """
        host = self.wiring.host
        count = write_roster(host.roster_file, self.servers, host.mcp_root, host.mcp_state_root)
        self.roster_written = True
        # Names and a count. No `run.env` value is read here either: a
        # literal is public and a `secret:` reference is a name, and the
        # ledger carries neither (invariant 13).
        self.entry.say(f"roster: {count} upstream(s) in {self.wiring.host.roster_file}")
        self._say_unapplied_allows()

    def _say_unapplied_allows(self) -> None:
        """Contract 01b §7 declares `arg_allows` and the PEP applies none.

        Root carries the declaration and writes no allow row, because the
        PEP's reader would refuse the whole roster and the reload would
        keep the old set. Saying so in `manual` is the difference between
        a fence the operator knows is not on and one that vanished.
        """
        named = sorted(one.name for one in self.servers if one.arg_allows)
        if not named:
            return

        self.entry.manual.append(f"arg_allows is declared and not applied for: {', '.join(named)}")

    def _reload_upstreams(self, name: str) -> None:
        """§4.4 step 1, and the last action of an `mcp-servers` release.

        The PEP re-reads its upstream set on `SIGHUP`, so the new server
        answers with no restart and no blocked approval is lost. A signal
        that does not land is recorded and does not fail the release: the
        trees are correct, and the verify hook is what decides whether the
        upstream actually serves.

        The condition is the ROSTER and not the builds. A release that
        removes the last declared server builds nothing and writes an
        empty roster, and without a signal that removal would never reach
        the PEP: the upstream would keep serving a tool the registry no
        longer declares. Invariant 18's other direction.
        """
        if name != MCP_COMPONENT or not self.roster_written:
            return

        result = self.mcp.reload_chaperone()
        if result.code != 0:
            self.entry.manual.append("the PEP did not take the reload signal")

        self.entry.say(f"reload signal to the PEP: exit {result.code}")

    def _wait_for_window(self, name: str) -> None:
        """§2.8. A diff that leaves LiteLLM alone needs no window. One that
        changes it waits for five quiet minutes, and an hour with none is
        `failed, step: switch`."""
        recreates = self.recreates.get(name, ())
        if not needs_window(recreates):
            return

        waited = wait_for_quiet(
            self.wiring.last_busy_seen, self.wiring.host.clock, self.wiring.host.sleep
        )
        if waited is None:
            raise StepFailed(NO_WINDOW)

        self.entry.say(f"quiet window for {name}: waited {waited:.0f}s")

    def _record_verify(self, outcome: VerifyOutcome) -> None:
        self.entry.verify.append(verify_row(outcome))
        self.entry.say(f"verify {outcome.component}: {outcome.detail}")

    # -- 10 restore or record -------------------------------------------

    def _restore(self) -> str:
        """Contract 06 §5.1: the unit of restore is the whole release.

        Every component this release deployed goes back, in REVERSE deploy
        order, and every verify runs again. Reverse, because the order was
        computed from `depends_on`: taking a dependency back before its
        dependents would leave the dependents talking to a version that is
        no longer there.
        """
        plan = self._require_plan()
        put_back: list[str] = []
        for name in reversed(self.deployed):
            self._restore_one(plan, name)
            put_back.append(name)

        put_back.extend(self._restore_orphaned_servers())
        # `reason` is NOT overwritten here. `_step` already recorded why the
        # switch failed, and that is the fact a reader needs: `switch: chaperone
        # verify failed` and `switch: no quiet window in 3600s` are two very
        # different releases, and "verify failed" said the same for both.

        return f"back in reverse: {', '.join(put_back)}"

    def _restore_orphaned_servers(self) -> list[str]:
        """The server trees and the roster, when the component itself never
        deployed.

        Step 9 swaps the per-server trees and writes the roster BEFORE it
        swaps the component's own tree, so a failure in between leaves the
        new servers live and `mcp-servers` out of `deployed` — and the loop
        above, which walks `deployed`, would put nothing back. The PEP would
        keep serving a set no release ever finished.

        It is a list rather than a flag because §2.6's `detail` names what
        went back, and "the servers only" is a different fact from "the
        component and its servers".
        """
        if MCP_COMPONENT in self.deployed or not (self.swapped or self.roster_written):
            return []

        self._restore_servers(MCP_COMPONENT)

        return [f"{MCP_COMPONENT} servers"]

    def _restore_one(self, plan: Plan, name: str) -> None:
        manifest = plan.manifest(name)
        paths = self._require_paths(name)
        # BEFORE the component's own tree, and not after it. `swap_back`
        # refuses a component that has no previous artifact, which is
        # every FIRST install — and `mcp-servers` is one on the day the
        # first MCP server ships. Restored after it, the servers would
        # stay live behind that refusal: the roster naming them, no signal
        # sent, and the PEP serving a release that failed its own verify.
        # The component's tree is one thing to put back;
        # what the PEP SERVES is another, and the second must not depend
        # on the first succeeding.
        self._move_back(name, paths)
        # `swap_back` moves the tree that failed verify to `.new`, and it
        # does not stay: `run` removes every tree this release staged,
        # because a kept tree stops the next cutover (`_remove_staged`).
        back = self._restore_units(name)
        self.installer.restart(manifest)
        # What ran before the switch runs again, even one the new tree
        # left `failed`. A put-back sibling this run never saw running is
        # restarted only when it is not stopped.
        running = self.running.get(name, ())
        self.installer.restart_units(running)
        self.installer.revive(tuple(one for one in back if one not in running))
        outcome = self.installer.verify(manifest)
        self._record_verify(outcome)
        if not outcome.ok:
            raise StepFailed(f"{name} did not verify after the restore")

    def _move_back(self, name: str, paths: Paths) -> None:
        """The moves of the restore, with the rule of `_swap`.

        An error that no step names leaves the run from here. A move back
        that stops half-way leaves no tree in service, and the tree that
        failed its hook is at `.new`. A ledger entry would end the run by
        the normal way: it removes each staged tree and spends the note.
        Nothing would then put the previous tree back. So the run writes no
        entry, the note stays, and the next run repairs. The
        CONTRACT-QUESTION of `_swap` covers this place too.
        """
        try:
            self._restore_servers(name)
            self.installer.swap_back(paths)
        except (Refusal, StepFailed):
            raise
        except Exception:
            self.interrupted = name
            raise

    def _restore_units(self, name: str) -> tuple[Path, ...]:
        """The unit and the siblings this release replaced, back BEFORE
        the restart. The previous tree must not restart under the unit this
        release installed: the PEP's 0.1.4 unit grants two capabilities for a
        launcher 0.1.3 does not carry. Answers the siblings put back."""
        unit = self.units.get(name)
        siblings = self.siblings.get(name, ())

        return _put_all_back(self.installer, unit, siblings, self.entry)

    def _restore_servers(self, name: str) -> None:
        """Every server tree that swapped goes back, in reverse, then the
        roster, then the signal.

        One method and not two halves, because the orphaned path and the
        deployed path put back exactly the same three things and a second
        expression of that is how two releases leave two hosts. The signal
        is inside it for the same reason: a restore whose signal sat in
        the caller lost it the moment anything before it raised.
        """
        if name != MCP_COMPONENT:
            return

        for build in reversed(self.swapped):
            self.mcp.swap_back(build)
            self.entry.say(f"back {build.name}: {build.artifact}")

        self._restore_roster()
        self._reload_upstreams(name)

    def _restore_roster(self) -> None:
        """The roster goes back with the trees, before the signal.

        A restore that put the trees back and left the new roster in place
        would leave the PEP probing a command that is no longer installed:
        the removed server would fail its probe, which is fail closed and
        reported, but every reader would be looking at a roster that names
        a release root had already undone.
        """
        if not self.roster_written:
            return

        self.entry.say(f"roster: {restore_roster(self.wiring.host.roster_file)}")

    def _record(self) -> str:
        # The notes are NOT cleared here. `spool.finish` clears every note
        # of this run once its entry is on disk, whatever the outcome: a
        # run that recorded itself is not a crash, and this path was one
        # of only two that ever reached a clear.
        return "ledger written"

    # -- plumbing -------------------------------------------------------

    def _require_plan(self) -> Plan:
        if self.plan is None:
            raise StepFailed("no resolved plan")

        return self.plan

    def _require_paths(self, name: str) -> Paths:
        found = self.paths.get(name)
        if found is None:
            raise StepFailed(f"{name} was never staged")

        return found


#: §2.6's `reason` for the entry a repair writes. A fixed string, like every
#: other reason (§3.2 rule 4).
CRASH_REASON: Final = "a crash between the switch and the verify"
CRASH_VERIFY_FAILED: Final = "restore: the restored version did not verify"
CRASH_UNIT_NOT_BACK: Final = "restore: the previous unit file could not be put back"
NO_RECORDED_HOOK: Final = "verify: the switch note recorded no hook"


class UnitNotBack(StepFailed):
    """The kept unit file did not go back, so nothing restarted: the
    previous tree must not start under this release's unit."""


class UnitRole(StrEnum):
    """Which unit file a ledger line names: the manifest's own, or a
    sibling (`install.py` rule 8)."""

    UNIT = "unit"
    SIBLING = "sibling"


def _put_all_back(
    installer: Installer, unit: Path | None, siblings: tuple[Path, ...], entry: Entry
) -> tuple[Path, ...]:
    """Every kept copy back, the main unit's first. Answers the siblings
    put back, for their `try-restart`.

    Every copy is tried, so each one that will not go back gets its own
    `manual` line. Then the first failure stops the restore BEFORE the
    restart, as `_put_unit_back` alone did.
    """
    failures: list[UnitNotBack] = []
    if unit is not None:
        try:
            _put_unit_back(installer, unit, entry, UnitRole.UNIT)
        except UnitNotBack as failure:
            failures.append(failure)

    back: list[Path] = []
    for kept in siblings:
        try:
            back.append(_put_unit_back(installer, kept, entry, UnitRole.SIBLING))
        except UnitNotBack as failure:
            failures.append(failure)

    if failures:
        raise failures[0]

    return tuple(back)


def _put_unit_back(installer: Installer, kept: Path, entry: Entry, role: UnitRole) -> Path:
    """`install.py` rule 6's restore, for both ends that restore: the run
    whose verify failed, and the repair of one that crashed.

    One function and not two halves, for `remove_staged`'s reason. A copy
    that will not go back is a `manual` line with the commands, and a
    `UnitNotBack` that stops the restore BEFORE the restart.
    """
    try:
        installed = installer.restore_unit(kept)
    except StepFailed as failure:
        entry.manual.append(installer.unit_by_hand(kept))
        raise UnitNotBack(failure.detail) from None

    entry.say(f"{role} back: {installed.name} from {kept}")

    return installed


def repair(spool: Spool, wiring: Wiring, request_id: str, note: SwitchNote) -> Entry:
    """A switch a previous run recorded and never cleared (§2.4 row 10).

    A crash between the swap and the verify leaves the new tree live and
    nobody's ledger entry saying so. The repair is a STEP of its own, not a
    journal line: the previous artifact goes back, its unit restarts, its
    own verify hook runs, and all of it lands in `done/`.
    It does NOT retry the release.
    """
    entry = Entry(id=request_id, kind=str(RequestKind.RELEASE), requested_by="unknown")
    entry.reason = CRASH_REASON
    entry.previous = {note.component: None}
    entry.say(f"repairing an unfinished switch of {note.component}")
    started = wiring.host.clock()
    try:
        detail = _put_back(wiring, note, entry)
        status, outcome = StepStatus.OK, Outcome.RESTORED
    except StepFailed as failure:
        detail, status, outcome = failure.detail, StepStatus.FAILED, Outcome.FAILED
        not_back = isinstance(failure, UnitNotBack)
        entry.reason = CRASH_UNIT_NOT_BACK if not_back else CRASH_VERIFY_FAILED
    except Exception as error:
        # Every outcome is a ledger entry (§2.4), as in `Release._step`. An
        # error that left here spent the note with no entry and no push,
        # and nothing said that the component needs a person.
        detail, status, outcome = _unnamed(error), StepStatus.FAILED, Outcome.FAILED
        entry.reason = f"{StepName.RESTORE}: {detail}"
        entry.manual.append(f"restore: the repair of {note.component} did not finish")
        entry.say(f"step {StepName.RESTORE}: {type(error).__name__} raised at {_raised_at(error)}")

    entry.status = outcome
    entry.add(
        Step(
            str(StepName.RESTORE),
            str(status),
            round(wiring.host.clock() - started, 1),
            detail,
        )
    )
    entry.say(f"step {StepName.RESTORE}: {status} — {detail}")

    return entry


def _put_back(wiring: Wiring, note: SwitchNote, entry: Entry) -> str:
    """Swap the previous tree back, then the unit file the switch kept,
    restart the unit, run its hook."""
    paths = Paths(to=Path(note.to), prev=Path(note.prev), new=Path(f"{note.to}.new"))
    installer = Installer(wiring.host)
    if not paths.prev.is_dir():
        entry.manual.append(f"restore: nothing to put back for {note.component}")

        return f"nothing to put back for {note.component}"

    installer.swap_back(paths)
    # The same sweep `_restore_one`'s run makes, for the crash-repair path:
    # `swap_back` has just put the tree of the crashed switch at `.new`,
    # and a tree root leaves there is what the next release and the next
    # cutover both die on. It is rebuilt from the SHA in the ledger.
    remove_staged(paths.new, note.component, entry)
    # Before the restart, as `_restore_one` does it, and from the note: the
    # run that knew what it kept is the one that crashed.
    unit = None if note.unit_kept is None else Path(note.unit_kept)
    siblings = tuple(Path(one) for one in note.siblings_kept)
    back = _put_all_back(installer, unit, siblings, entry)
    _restart_restored(installer, note, back, entry)

    return f"{note.component} put back from {paths.prev.name}"


def _restart_restored(
    installer: Installer, note: SwitchNote, siblings: tuple[Path, ...], entry: Entry
) -> None:
    """The two halves of a repair besides the swap: the unit and the hook.

    A `SwitchNote` carries the unit name and the hook root resolved before
    the swap, so neither is re-read from anything the operator owns. A note an
    older executor wrote carries no hook: the restore still happens and the
    ledger says what root could not do, because guessing a command is worse
    than saying so.
    """
    installer.restart_unit(note.unit)
    # The run that knew which siblings ran is the one that crashed.
    installer.revive(siblings)
    hook = note.verify
    if hook is None:
        entry.manual.append(NO_RECORDED_HOOK)

        return

    outcome = installer.run_hook(note.component, hook)
    entry.verify.append(verify_row(outcome))
    entry.say(f"verify {outcome.component}: {outcome.detail}")
    if not outcome.ok:
        raise StepFailed(f"{note.component} did not verify after the repair")
