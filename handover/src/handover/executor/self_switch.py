"""`handover` switching its own tree (contract 06 §1.1 rules 2 and 3).

```
 release <id>   steps 1-8 as usual; step 9 swaps the rest, then writes
                running/<id>-handover.self and swaps nothing
 drain          done/<id>.json, lock released, outcome pushed
 switch_in      handover.new -> handover, handover -> handover.prev
                the new tree's hook runs as a child: a fresh process
                  ok     -> succeeded
                  failed -> the previous tree goes back, its hook runs
                done/<id>-handover.json, the note cleared, the pass ends
```

**Why the process that swapped also verifies.** Its code is the previous
version, which just performed a whole release, so the decision to put the
tree back is made by code known to work. A new tree that cannot start at
all is caught here, by a hook that fails, and never becomes the executor
that has to notice it is broken.

**What runs after the swap.** `sys.path` names the install path, so any
module imported from now on would come out of the NEW tree. Nothing is:
every import in this package sits at module top (contract 06 §1.1), the
hook is a child, and the pass ends as soon as the entry is written. A
request still in `requests/` re-fires the path unit, and that run starts on
whatever tree is live.

**A crash in the gap** leaves the note. The next run reads the live tree's
stamp: the new version means the swap happened and rule 3's hook runs now,
on the new code; anything else means it never did, and the staged tree
goes. Root never retries a swap (§2.4 row 10).

The entry goes under the note's own name, `<id>-handover`, as a repair's
does. Its `previous` is empty: the release's own entry already carries what
a rollback would target, and this one records one switch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from .install import NEW_SUFFIX, Installer, Paths, StepFailed, VerifyOutcome
from .ledger import Entry, Outcome, Step, StepName, StepStatus
from .live_state import installed_version
from .request import Kind as RequestKind
from .spool import SelfNote
from .steps import CRASH_VERIFY_FAILED, Wiring, remove_staged, verify_row

#: §2.6's closed list, for the two ends that are not a success.
NEVER_SWITCHED: Final = "switch: the run that staged it never swapped it"
VERIFY_FAILED: Final = "switch: the new tree did not verify"

#: Rule 4. `handover` carries `unit: null`, so the listener keeps the
#: previous code until root restarts it.
INTAKE_BY_HAND: Final = (
    "the intake still runs the previous handover; as root: systemctl restart creche-handover-intake"
)


def switch_in(wiring: Wiring, request_id: str, note: SelfNote) -> Entry:
    """Rule 2's swap, then rule 3. The caller has already written the
    release's entry and released the lock.

    The swap runs only while the staged tree is there. `swap_in` moves the
    live tree aside before it moves the new one in, so a missing `.new`
    would leave no executor at all. Without it, `settle` finds the old
    stamp and records a switch that never ran.

    An `OSError` out of the swap is not caught. It is a crash like any
    other, and the note is what the next run settles.
    """
    paths = _paths_of(note)
    if paths.new.is_dir():
        Installer(wiring.host).swap_in(paths)

    return settle(wiring, request_id, note)


def settle(wiring: Wiring, request_id: str, note: SelfNote) -> Entry:
    """The swap's outcome, right after it or on the run after a crash.
    The live tree's stamp says whether the swap happened."""
    entry = _entry_of(request_id, note)
    started = wiring.host.clock()
    paths = _paths_of(note)
    if installed_version(paths.to) != note.to_version:
        remove_staged(paths.new, note.component, entry)
        entry.reason = NEVER_SWITCHED
        _add(entry, StepName.SWITCH, StepStatus.FAILED, started, wiring, NEVER_SWITCHED)

        return entry

    entry.say(f"live {note.component} {note.to_version}")

    return _verify(wiring, note, entry, started)


def _verify(wiring: Wiring, note: SelfNote, entry: Entry, started: float) -> Entry:
    """Rule 3: the new tree's hook, as a child of this process."""
    installer = Installer(wiring.host)
    outcome = installer.run_hook(note.component, note.verify)
    _record(entry, outcome)
    if outcome.ok:
        entry.status = Outcome.SUCCEEDED
        entry.manual.append(INTAKE_BY_HAND)
        detail = f"{note.component} {note.to_version} live"
        _add(entry, StepName.SWITCH, StepStatus.OK, started, wiring, detail)

        return entry

    entry.reason = VERIFY_FAILED
    _add(entry, StepName.SWITCH, StepStatus.FAILED, started, wiring, VERIFY_FAILED)
    entry.status = _put_back(wiring, installer, note, entry)

    return entry


def _put_back(wiring: Wiring, installer: Installer, note: SelfNote, entry: Entry) -> Outcome:
    """The previous tree, then its own hook. Rollback is not built (open
    question 6), so the executor puts the previous tree back itself."""
    started = wiring.host.clock()
    paths = _paths_of(note)
    try:
        installer.swap_back(paths)
    except StepFailed as failure:
        _add(entry, StepName.RESTORE, StepStatus.FAILED, started, wiring, failure.detail)

        return Outcome.FAILED

    remove_staged(paths.new, note.component, entry)
    outcome = installer.run_hook(note.component, note.verify)
    _record(entry, outcome)
    if not outcome.ok:
        entry.reason = CRASH_VERIFY_FAILED
        _add(entry, StepName.RESTORE, StepStatus.FAILED, started, wiring, CRASH_VERIFY_FAILED)

        return Outcome.FAILED

    detail = f"{note.component} put back from {paths.prev.name}"
    _add(entry, StepName.RESTORE, StepStatus.OK, started, wiring, detail)

    return Outcome.RESTORED


def _entry_of(request_id: str, note: SelfNote) -> Entry:
    entry = Entry(id=request_id, kind=str(RequestKind.RELEASE), requested_by=note.requested_by)
    entry.say(f"switching {note.component} {note.from_version or 'absent'} → {note.to_version}")

    return entry


def _paths_of(note: SelfNote) -> Paths:
    """The three trees, out of the note root wrote from `paths_of` at
    step 8, so containment was already checked."""
    return Paths(to=Path(note.to), prev=Path(note.prev), new=Path(f"{note.to}{NEW_SUFFIX}"))


def _record(entry: Entry, outcome: VerifyOutcome) -> None:
    entry.verify.append(verify_row(outcome))
    entry.say(f"verify {outcome.component}: {outcome.detail}")


def _add(
    entry: Entry,
    name: StepName,
    status: StepStatus,
    started: float,
    wiring: Wiring,
    detail: str,
) -> None:
    seconds = round(wiring.host.clock() - started, 1)
    entry.add(Step(str(name), str(status), seconds, detail))
    entry.say(f"step {name}: {status} — {detail}")
