"""The unit's entry point: repair, then every request, then the ledger.

`stage7-releases.md` §2.2 and §3.2. `requests/` is drained on EVERY run,
without exception: `creche-handover.path` is a `PathExistsGlob`, which re-fires
while any match remains, so one file left behind loops the unit into
systemd's start limit. Every path out of `handle` therefore ends with the
entry gone from `requests/` — ledgered, or renamed whole into `rejected/`.

Three things happen before the first request is read.

1. **Repair.** A crash between the swap and the verify leaves a switch note
   in `running/` and a tree nobody recorded. The note is root-owned, so it
   is a fact. The previous artifact goes back and the repair is ledgered.
   It does NOT retry the release (§2.4 row 10).
2. **The rate rule.** §3.2 rule 8: at most eight requests per requester.
   Extras are ledgered `refused`, check `rate`, and the file still leaves
   `requests/`. `requested_by` is a field the requester writes, so that cap
   alone stops nothing: `drain` caps the whole pass as well.
3. **The lock.** One release at a time (§2.4 row 6). The lock is taken at
   step 6, inside `Release`, and released here whatever the outcome.

One thing happens after a release's entry: `handover`'s own switch
(contract 06 §1.1 rules 2 and 3, `self_switch.py`). The process that
performs it runs the previous code from then on, so the pass ends there.

The unit exits 0 for every outcome a release can have, including a refusal
and a failed verify. Non-zero means the executor could not do its own job:
no spool, no root, a directory it cannot open.
"""

from __future__ import annotations

import os
import pwd
import sys
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..catalog import CATALOG_BY_NAME
from ..errors import Refusal, RefusalCode
from ..resolve import TAG_FORMAT
from ..site import operator_account
from .approval import deny_all
from .host import As, Command, Host, make_runner
from .install import StepFailed
from .ledger import Entry, Outcome, Step, StepName, StepStatus
from .live_state import InputDigestFn, Readers
from .notice import notice_of, say_nothing
from .phone import TOKEN_ENV as APPROVAL_TOKEN_ENV
from .phone import URL_ENV, build_phone
from .provenance import ApiGetFn, github_api, newest_version, tag_commit
from .quiet import attendance_signal
from .request import Request, request_id_of
from .self_switch import settle, switch_in
from .source import git_env, input_digest, require_trusted
from .spool import (
    MAX_PENDING_PER_REQUESTER,
    MAX_REQUESTS_PER_PASS,
    SPOOL_ROOT,
    NotAFile,
    SelfNote,
    Spool,
    SpoolError,
    Unfinished,
    UnfinishedSelf,
    note_name,
)
from .steps import SELF_COMPONENT, Release, Wiring, repair

#: Where root's own environment carries the GitHub token. The wrapper
#: decrypts it and execs this; the token never rides in argv or a URL
#: (invariant 13).
TOKEN_ENV: Final = "RELEASE_GITHUB_TOKEN"

#: What the executor's own process keeps besides the three secrets. The
#: wrapper runs it under `sops exec-env` over the site's sops file, so root's
#: environment arrives holding EVERY compose secret, for the whole run.
#: The executor needs three of them, and `host.child_env` builds each
#: child's environment from scratch anyway, so the rest are dropped.
KEPT_ENV: Final = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})

#: §3.2 rule 4: a refusal's reason is a FIXED string, never built from the
#: file. These four are every reason `handle` can give on its own.
RATE_REASON: Final = f"more than {MAX_PENDING_PER_REQUESTER} requests from one requester"
REPLAY_REASON: Final = "the id was already spent"
UNREADABLE_REASON: Final = "not a plain file root will read"

LOG_PREFIX: Final = "handover"

#: One `git rev-parse` in a clone root already vetted. It reads one object
#: id and prints 40 bytes, so the cap is a hung child and not a slow one.
GIT_READ_TIMEOUT_S: Final = 30.0

#: §3.1's second requester writes through the PEP, which runs as this user
#: (`systemd/creche-chaperone.service`). Root accepts a file it owns and still
#: believes nothing the file says.
CHAPERONE_USER: Final = "chaperone"


def _ledger_only(request_id: str, kind: str, requested_by: str, reason: str) -> Entry:
    """A request that never reached step 1 still gets §2.6's entry: one
    refused `intake` row, and the closed-list reason."""
    entry = Entry(id=request_id, kind=kind, requested_by=requested_by)
    entry.status = Outcome.REFUSED
    entry.refused_check = str(RefusalCode.REQUEST)
    entry.reason = reason
    entry.add(Step(str(StepName.INTAKE), str(StepStatus.REFUSED), 0.0, reason))
    entry.say(f"refused: {reason}")

    return entry


def _refuse_unparsed(spool: Spool, name: str, request_id: str, reason: str) -> str:
    """Ledger under the id the FILE NAME carried, then drop the file. Its
    bytes reach neither the ledger nor the log (§3.2 rule 4)."""
    entry = _ledger_only(request_id, "unknown", "unknown", reason)
    spool.finish(request_id, entry.as_dict(), entry.log)
    spool.drop_request(name)

    return f"{request_id}: refused ({reason})"


class Counter:
    """How many requests one requester has had in this pass (§3.2 rule 8).

    The design says "pending", and the executor drains `requests/` whole on
    every run, so the count that can be made is the count within one pass.
    CONTRACT-QUESTION: §3.2 rule 8 gives "pending" no window root can
    measure, and a literal reading refuses nothing.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        #: Set once this process has swapped `handover`'s tree. Every
        #: request after that would run on code that is no longer live.
        self.ended = False

    def over_limit(self, requester: str) -> bool:
        self._seen[requester] = self._seen.get(requester, 0) + 1

        return self._seen[requester] > MAX_PENDING_PER_REQUESTER


def handle(spool: Spool, name: str, wiring: Wiring, counter: Counter) -> str:
    """Exactly one entry of `requests/` leaves `requests/`.

    Returns one line for the journal. Every branch below ends with the file
    gone, because the path unit re-fires while one remains.
    """
    stamp = int(wiring.host.clock())
    request_id = request_id_of(name)
    if request_id is None:
        return f"rejected (not <ULID>.json) -> rejected/{spool.quarantine(name, stamp)}"

    if spool.seen(request_id):
        # §6 row 5: a replayed id must never overwrite a ledger entry.
        return f"rejected ({REPLAY_REASON}) -> rejected/{spool.quarantine(name, stamp)}"

    try:
        request = spool.read_request(name, request_id)
    except NotAFile:
        return f"rejected ({UNREADABLE_REASON}) -> rejected/{spool.quarantine(name, stamp)}"
    except Refusal as refusal:
        return _refuse_unparsed(spool, name, request_id, refusal.detail)

    if counter.over_limit(request.requested_by):
        spool.drop_request(name)
        entry = _ledger_only(request_id, str(request.kind), request.requested_by, RATE_REASON)
        entry.refused_check = str(RefusalCode.RATE)
        spool.finish(request_id, entry.as_dict(), entry.log)

        return f"{request_id}: refused (rate)"

    return f"{request_id}: {_process(spool, request, wiring, counter)}"


def _process(spool: Spool, request: Request, wiring: Wiring, counter: Counter) -> str:
    """The ten steps, then the ledger, then the lock — in that order.

    The entry is written whatever happened, because a release with no ledger
    entry is a release nobody can read afterwards (§2.6). Then, and only
    then, the phone is told: a push that fails must not change an outcome
    root has already recorded.
    """
    spool.start(request)
    walk = Release(request, wiring, spool)
    try:
        outcome = walk.run()
    finally:
        walk.release_lock()

    walk.entry.status = outcome
    spool.finish(request.id, walk.entry.as_dict(), walk.entry.log)
    _tell(wiring, walk.entry)
    note = walk.self_note
    if note is None:
        return str(outcome)

    # Contract 06 §1.1 rule 2: after the entry and the lock, never before.
    counter.ended = True
    entry = switch_in(wiring, request.id, note)

    return f"{outcome}; {_finish_self(spool, wiring, note, entry)}"


def _finish_self(spool: Spool, wiring: Wiring, note: SelfNote, entry: Entry) -> str:
    """The switch's own entry, under the note's name. Writing it spends
    the note (`spool.finish`)."""
    spool.finish(note_name(entry.id, note.component), entry.as_dict(), entry.log)
    _tell(wiring, entry, f"{note.component} {note.from_version or 'absent'} → {note.to_version}")

    return f"{note.component} {note.to_version} {entry.status}"


def _tell(wiring: Wiring, entry: Entry, component: str | None = None) -> None:
    """§2.6's push. Never raises: an unreachable phone is a journal line,
    not a release that changes its mind about what it did."""
    notice = notice_of(entry, component)
    try:
        delivered = wiring.notify(notice)
    except OSError as exc:
        print(f"{LOG_PREFIX}: the outcome push failed ({type(exc).__name__})", flush=True)

        return

    said = "pushed" if delivered else "not delivered"
    print(f"{LOG_PREFIX}: outcome {said}: {notice.line()}", flush=True)


def repair_unfinished(spool: Spool, wiring: Wiring) -> list[str]:
    """Every switch a previous run recorded and never cleared (§2.4 row 10).

    A crash between the swap and the verify left the new tree live. The
    previous artifact goes back — the state the last resolved, checked and
    approved set described — its unit restarts, its own verify hook runs,
    and the whole repair becomes a ledger entry.
    """
    lines: list[str] = []
    for found in spool.unfinished():
        # Every error, not two types of it: the repairs run before
        # `requests/` is drained, so an error that left here ended each run
        # at the same note.
        try:
            lines.append(_repair_one(spool, wiring, found))
        except Exception as exc:
            spool.clear_switch(found.name)
            lines.append(f"{found.request_id}: repair failed ({type(exc).__name__})")

    return lines


def _repair_one(spool: Spool, wiring: Wiring, found: Unfinished) -> str:
    """One repair, ledgered under the note's own name and SAYING which
    request it belongs to.

    Two ids already have an entry, and each means the note is spent.

    1. **The run that wrote it.** Its entry says what it decided, so this
       switch is not an unfinished one — `spool.finish` clears the notes
       of a run it records — and a repair that restored here would undo a
       release root had already settled.
    2. **A previous repair of this note.** The entry is the record, and a
       repair that overwrote one would lose the only account of what the
       crashed run did before it crashed.

    The entry is filed under the note's name and not the request's, so a
    crash that left notes for two components is two repairs and two
    entries, neither overwriting the other. Its `id` field is the request
    id, which is what a reader greps for (§2.6).
    """
    if spool.ledgered(found.request_id) or spool.ledgered(found.name):
        spool.clear_switch(found.name)

        return f"{found.request_id}: {found.note.component}: already ledgered, the note is cleared"

    entry = repair(spool, wiring, found.request_id, found.note)
    spool.finish(found.name, entry.as_dict(), entry.log)
    _tell(wiring, entry, found.note.component)

    return f"{found.request_id}: {found.note.component} repaired ({entry.status})"


def settle_itself(spool: Spool, wiring: Wiring) -> tuple[list[str], bool]:
    """Every self note a crash left (contract 06 §1.1 rule 3). The lines,
    and whether any was settled: a run that settled one drains nothing, so
    what is left runs on whichever tree is live now."""
    lines: list[str] = []
    settled = False
    for found in spool.unfinished_self():
        if spool.ledgered(found.name):
            spool.clear_self(found.name)
            said = f"{found.note.component}: already ledgered, the note is cleared"
            lines.append(f"{found.request_id}: {said}")
            continue

        settled = True
        lines.append(_settle_one(spool, wiring, found))

    return lines, settled


def _settle_one(spool: Spool, wiring: Wiring, found: UnfinishedSelf) -> str:
    entry = settle(wiring, found.request_id, found.note)

    return f"{found.request_id}: {_finish_self(spool, wiring, found.note, entry)}"


def drain(spool: Spool, wiring: Wiring) -> int:
    """ONE snapshot of `requests/`, in id order, capped.

    Two rules.

    1. One snapshot, never a loop over re-listings. `requests/` is
       operator-writable, so a writer that adds faster than root removes kept
       root inside the loop until `TimeoutStartSec`. The path unit re-fires
       for whatever arrived after the snapshot, which is what it is for.
    2. At most `MAX_REQUESTS_PER_PASS` are parsed and ledgered. The rest of
       the snapshot is quarantined unread, so the glob still empties and a
       flood cannot grow `done/` by one entry and one log per file.
    """
    counter = Counter()
    stamp = int(wiring.host.clock())
    handled, flooded = 0, 0
    for name in spool.pending():
        if handled >= MAX_REQUESTS_PER_PASS:
            spool.quarantine(name, stamp)
            flooded += 1
            continue

        print(f"{LOG_PREFIX}: {handle(spool, name, wiring, counter)}", flush=True)
        handled += 1
        if counter.ended:
            print(f"{LOG_PREFIX}: {SELF_COMPONENT} moved, the pass ends here", flush=True)
            break

    if flooded:
        print(f"{LOG_PREFIX}: {flooded} request(s) past the pass cap -> rejected/", flush=True)

    return handled


def build_wiring(operator_uid: int, secrets: Secrets, operator_gid: int = 0) -> Wiring:
    """The production wiring. No GitHub token means step 3 refuses, which
    is the fail-closed end: a predicate that cannot ask is not a predicate
    that passed.

    A host with no phone hook configured gets `deny_all` and `say_nothing`.
    That is the other fail-closed end: an unconfigured approval path must
    refuse every release rather than wave one through (invariant 10).

    §2.8's signal is `attendance`: no turn in flight for five consecutive
    minutes. A host whose session service root cannot reach reads nothing,
    and nothing is never quiet, so `infra` waits out its hour and fails the
    switch instead of recreating `ai-litellm` under live calls. `operator_uid`
    is the owner root demands of both the socket and the `view-ro` token.

    `operator_gid` defaults to root's, which refuses every operator-owned corpus
    clone (`executor/source.py`). That is the fail-closed end of the
    trust rule, and `main` passes the real number.
    """
    host = Host(
        run=make_runner(operator_uid), source_owner_uid=operator_uid, source_owner_gid=operator_gid
    )
    transport, notify = build_phone(
        secrets.approval_url, secrets.approval_token, host.sleep, host.clock
    )
    api = github_api(secrets.github_token) if secrets.github_token else None

    return Wiring(
        host=host,
        transport=transport or deny_all,
        readers=root_readers(host, api),
        api=api,
        notify=notify or say_nothing,
        last_busy_seen=attendance_signal(
            host.attendance_socket, host.view_token_file, operator_uid, host.clock
        ),
    )


def root_readers(host: Host, api: ApiGetFn | None) -> Readers:
    """Contract 06 §11's two off-disk answers, as ROOT gets them.

    **Tags and commits both come from GitHub**, and `executor/live_state.py`
    carries why: `facts.sha` is `provenance.tag_commit` whatever happens, so
    a `latest` read out of the operator-owned corpus would let one planted tag
    name a version GitHub has no Release for and refuse every `request
    <name>` with no version, for ever.

    **The digest comes from the corpus**, because it is a read of SOURCE and
    the corpus is the one place root reads source (contract 06 §3.4). It is
    the same directory the fetch uses a moment later and it is vetted the
    same way, by `require_trusted`, before any git process starts in it.

    No token means no reader at all: `latest` then names nothing and a
    deploying component has no sha, so step 2 refuses. That is the same
    fail-closed end `api=None` gives step 3.
    """
    if api is None:
        return Readers(input_digest=_digest_reader(host))

    def newest(component: str) -> str | None:
        return newest_version(api, component, str(CATALOG_BY_NAME[component].repo))

    def sha_of(component: str, version: str) -> str | None:
        repo = str(CATALOG_BY_NAME[component].repo)

        return tag_commit(api, component, repo, TAG_FORMAT.format(name=component, version=version))

    return Readers(newest_version=newest, tag_sha=sha_of, input_digest=_digest_reader(host))


def _digest_reader(host: Host) -> InputDigestFn:
    """`source.input_digest`, run through root's one child starter."""

    def run_git(argv: Sequence[str], cwd: Path) -> str | None:
        command = Command(
            argv=tuple(argv),
            identity=As.ROOT,
            timeout_s=GIT_READ_TIMEOUT_S,
            cwd=cwd,
            env=tuple(git_env().items()),
        )
        # A child that cannot start (a clone re-made under root's feet, no
        # file descriptor left) fails THIS step, as `Installer._run` does.
        # Left to propagate it would end the whole drain pass.
        try:
            result = host.run(command)
        except OSError as exc:
            raise StepFailed(f"{argv[0]}: {type(exc).__name__}") from None

        if result.code != 0:
            return None

        return result.stdout

    def digest(component: str, sha: str) -> str | None:
        repo = str(CATALOG_BY_NAME[component].repo)
        path = host.source_root / repo
        require_trusted(repo, path, host.source_owner_uid, host.source_owner_gid)

        return input_digest(run_git, path, component, sha)

    return digest


@dataclass(frozen=True)
class Secrets:
    """The three values root keeps out of `sops exec-env`'s whole set."""

    github_token: str
    approval_url: str
    approval_token: str


def keep_only_the_secrets(environ: MutableMapping[str, str]) -> Secrets:
    """Take what the executor needs out of the environment, clear the rest.

    The wrapper runs this under `sops exec-env` over the site's sops file, so
    root's environment arrives holding EVERY compose secret for the whole
    run. `host.child_env` builds each child's environment from scratch, so
    no child inherits one; what this bounds is the executor's own process.

    It does NOT rewrite `/proc/<pid>/environ`, which keeps what `execve`
    was given. That file is root-only, so the reader this bounds is a
    child started outside `Host`, or anything that prints `os.environ`.
    """
    found = Secrets(
        github_token=environ.get(TOKEN_ENV, ""),
        approval_url=environ.get(URL_ENV, ""),
        approval_token=environ.get(APPROVAL_TOKEN_ENV, ""),
    )
    for name in [one for one in environ if one not in KEPT_ENV]:
        del environ[name]

    return found


def _chaperone_uids() -> frozenset[int]:
    """The PEP's accounts that this host has.

    §3.1's second requester is the `agent-control` family, and its path is
    the PEP's `release` verb. The PEP runs as `chaperone`, so the file it
    writes is chaperone-owned and root must accept that owner or the verb can
    file nothing. An absent account is not an error: a host with no PEP
    simply has one requester.
    """
    try:
        return frozenset({pwd.getpwnam(CHAPERONE_USER).pw_uid})
    except KeyError:
        return frozenset()


def run_pass(spool: Spool, wiring: Wiring) -> int:
    """One activation: what a crash left, then `requests/`."""
    for line in repair_unfinished(spool, wiring):
        print(f"{LOG_PREFIX}: {line}", flush=True)

    lines, settled = settle_itself(spool, wiring)
    for line in lines:
        print(f"{LOG_PREFIX}: {line}", flush=True)

    if settled:
        return 0

    return drain(spool, wiring)


def main() -> int:
    """Root only. Every release outcome exits 0; only a broken spool or a
    site file this host cannot run under does not."""
    if os.geteuid() != 0:
        print(f"{LOG_PREFIX}: root only (creche-handover.service)", file=sys.stderr)

        return 1

    # Before the spool: nothing can be ledgered without the operator's uid.
    # One line and 1, so the journal says which value to fix.
    try:
        operator = operator_account()
        operator_uid = operator.pw_uid
        wiring = build_wiring(operator_uid, keep_only_the_secrets(os.environ), operator.pw_gid)
    except Refusal as refused:
        print(f"{LOG_PREFIX}: {refused.as_line()}", file=sys.stderr)

        return 1

    try:
        spool = Spool(SPOOL_ROOT, operator_uid, also_owned_by=_chaperone_uids())
    except SpoolError as exc:
        print(f"{LOG_PREFIX}: {exc}", file=sys.stderr)

        return 1

    try:
        handled = run_pass(spool, wiring)
    except (SpoolError, OSError) as exc:
        print(f"{LOG_PREFIX}: cannot drain the spool: {exc}", file=sys.stderr)

        return 1
    finally:
        spool.close()

    print(f"{LOG_PREFIX}: {handled} request(s) handled", flush=True)

    return 0
