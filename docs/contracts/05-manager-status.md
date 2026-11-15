# 05 — Manager status

Status: draft 22
Version: 0.21.0

Minor 17 adds the two plane URLs of the env file of a sandbox (section
4.1.1). Minor 18 drops the `sandboxes` list, the switch call and the codes
`protocol_mismatch`, `protocol_violation`, `orphan_processes` and
`image_behind`. It adds the vmm as the third fault writer and the codes
`vmm_protocol_mismatch` and `serial_log_full`. Minor 19 drops the sandbox
create, the canary probe and the policy rows of §4.3 and §4.4. The
vmm proxy carries the egress list, and the vmm check proves deny-by-default
(§3.5, §4.3). Minor 20 drops the steps `create_sandbox`, `switch_sandbox`
and `destroy_sandbox` and the field `needs_switch` from the reconcile block
(§3.4). Minor 21 drops the path of the previous playpen (decision CH10-8): the fault
`image_flavor_unconfigured` (§3.3) and the image per flavor of §4.1.

Amended on 2026-11-04 by decision CH5b and fact 36 of the design note
`docs/design/cloud-hypervisor-playpen.md`.
Sections 1, 6.1 and 6.3 follow contract 03 minor 15. No channel message
carries `epoch`, and no reader compares the `epoch` of `creds.json` with
anything. The status document still publishes `epoch`.

Amended on 2026-11-08 by decisions CH5d, CH19, CH21-1, CH25 rule 1, CH33
rule 10, CH79 rule 7 and CH82 rules 1 to 6 of the same design note. The
vmm owns each VM, one per session (decision CH1), and reports its faults in
a file of its own (§3.3.1). `caregiver` publishes no sandbox row and makes
no call to `attendance`. A code leaves §3.3 when the cut removes its only
source. Decision CH82
names `serial_log_full`, and no fault of the vmm blocks turns (§3.3). It
fixes the clear of `attendance` (§3.3.1 rule 8).

Amended on 2026-11-08 by decisions CH6-2, CH6-3, CH6-5, CH54 rule 2, CH69
rule 12 and CH82 rules 6 and 7 of the same design note. `caregiver` starts
and stops no VM, so no start fault blocks turns (§3.3). It writes the reach
files of contract 01 §6.1.1, and the vmm enforces them. A new shape reaches
a VM at its next start (§3.5).

Amended on 2026-11-08 by decisions CH1, CH21-1 and CH69 rule 6 of the same
design note. A pass of `caregiver` writes files and makes key calls, and
the vmm starts and stops each VM (§3.4).

`docs/invariants.md` lists each rule of this contract that the code does
not hold yet.

## 1. Scope

`caregiver` is a reconciler, not a command the operator runs. It watches the registry
and converges each family to its file. It owns the family
LiteLLM key and the family chaperone token. The vmm owns each VM (decision
CH21-1).

| Direction | What | Where |
|---|---|---|
| `caregiver` publishes | the per-family status document | a file, §2 |
| `attendance` reads | the status document | a file, §2 |
| `attendance` writes | its own open faults per family | a file, §3.3.1 |
| the chaperone writes | its own open faults per family | a file, §3.3.1 |
| the vmm writes | its own open faults per family | a file, §3.3.1 |
| `caregiver` reads | the three fault files | a file, §3.3.1 |
| the one view reads | the status document and the report | a file, §8 |
| the operator calls | `rotate` | `caregiver`, §6 |

`caregiver` makes no call to `attendance` (§5). Everything `attendance` needs
from `caregiver` arrives through the status document.

`attendance`, the chaperone and the vmm see faults `caregiver` cannot see for
itself. They report them in a file, never in a call (§3.3.1).

`caregiver` never writes a session, a journal or a turn. `attendance` never writes
a status document. Each file has one writer.

Contract 01 owns `family.toml` and its validation rules. This
contract owns how the result of that validation appears in the status document.
The two must not restate each other.

## 2. The status document

Path: `/srv/agents/state/creche/families/<family>/status.json`

1. `caregiver` is the only writer.
2. It writes a temp file in the same directory and calls `rename()`. `rename`
   is atomic. Unlink plus recreate is not. A
   reader therefore sees the old document or the new one, never a partial one.
3. Mode 0644. The document holds no secret. Credentials live in a different
   directory at mode 0600 (contract 03 §12).
4. `caregiver` rewrites it on every state change and at least every 30 seconds.
5. `written_at` older than 90 seconds means `caregiver` is not running. A reader
   shows the family as `unknown` and says why. It must not show the stale state
   as current.
6. A reader lists `/srv/agents/state/creche/families/` to find every family. No
   index file exists, so no index can go stale.
7. **One pass per family, and no family waits for another.** `caregiver`
   converges the families side by side, up to a bound it sets itself, and
   never runs two passes of one family at the same time. Rule 4 is therefore
   per family: a document is rewritten on its own schedule whatever another
   family's pass is doing. One family's slow replacement never holds another
   family's edit or document behind it.
8. **A rewrite may carry nothing but a new `written_at`.** A step of a
   pass can run past rule 4's 30 seconds and publish nothing of its own,
   which rule 4 forbids and rule 5 punishes. Until minor 20 that step was
   the sandbox create, which ran for minutes (§3.4). `caregiver` therefore rewrites
   the document it already published, with the time moved on and every
   other field unchanged. That is honest: the family IS still where the
   document says it is, which is `reconciling` at the step of §3.4.
   **Only a document this process published.** One written before a restart
   is a verdict about another commit, another image, or both, and it stays
   as old as it is until this process has looked. A reader then calls it
   `unknown` after rule 5's 90 seconds, which is the truth.

   **And this process looks at once.** A family that is due, cannot get a
   pass slot, and has only another process's document gets a LOOK: its own
   pass, with the stop already set, in a thread beyond the bound
   (`caregiver/src/caregiver/loop.py`, `_look_at`). Every cheap step runs,
   the pass halts in front of the first slow one, and it publishes what it
   found. A family with nothing slow to do is converged by its look and says
   `in_sync`. A family with a slow step to run says `reconciling` and never
   `in_sync`, records no revision, and takes the next free slot. From that
   document on, the first half of this rule applies and the loop restamps
   it.

   Before the look, rule 7 did not hold after a restart. On 2026-10-04 a
   release restarted `caregiver` while the fleet moved onto a new sandbox
   image. Four creates held the four slots for minutes, three families
   behind them kept the old process's document past 90 seconds, and the
   verify hook failed on `heartbeat` (ledger entry
   `01M42NEH3FY6QKQ37CKYVJ0M88`). So did the restore.
9. The document is a strict JSON text. A reader that
   refuses it treats the family as a reader treats a document that is not
   JSON.

### 2.1 Fields

| Field | Type | Meaning |
|---|---|---|
| `family` | string | family name |
| `kind` | enum | `attended`, `thin`, `autonomous` |
| `state` | enum | `in_sync`, `reconciling`, `invalid`, `degraded`. §3 |
| `written_at` | RFC 3339 | when `caregiver` wrote this document |
| `registry_rev` | string | the registry revision `caregiver` last read |
| `applied_rev` | string | the revision now live |
| `config_rev` | string | revision of the family config mount. One counter for the whole directory, contract 01 §6.1 |
| `validation` | object | §3.2 |
| `faults` | list | §3.3. Empty unless `degraded`. |
| `reconcile` | object or null | §3.4. Null unless `reconciling`. |
| `credentials` | object | §6.1. Ids and an epoch. Never a value. |
| `spend` | object | §7 |
| `limits` | object | `max_running_turns`, `max_queued_turns`, `job_timeout_s`. Two come from `family.toml`. `max_queued_turns` is `attendance`'s constant of **100**, mirrored here for the noticeboard (contract 02 §13 rule 4) |
| `triggers` | object | §6.4. `webhooks`: one row per declared webhook, naming where its bearer lives, never a value. `enqueue`: `true` when the family file declares the dispatch trigger `enqueue: true` (contract 01 §3.13), else `false`. It is the only way `attendance` learns that another family's `enqueue` verb may start a job here (contract 02 §13.4.1 rule 3). A reader treats a missing key as `false`. |
| `pep` | object | §2.2. What the chaperone watch last saw. Never null. |

The document holds no `sandboxes` list since minor 18 (§4.1).

### 2.2 The `pep` block

The chaperone is the one door to every action (invariant 12), so a chaperone that is
away is every family's outage. This block is what `caregiver` last saw.

| Field | Type | Meaning |
|---|---|---|
| `watch` | enum | `off`, `ok` or `unreachable` |
| `url` | string | where the watch probes. Empty when `watch` is `off` |
| `checked_at` | RFC 3339 or null | the last probe. Null when none has run |
| `unreachable_since` | RFC 3339 or null | when the chaperone first went silent. Null unless `watch` is `unreachable` |

Four rules:

1. **`off` means nobody asked.** It is not `ok`. A `caregiver` started
   with no chaperone address publishes `off`, and so does every verb that runs
   one pass in one process — `apply-once`, `reconcile-once` — because a
   watch is a fact over time and one pass has no interval to probe over.
2. `ok` means `/healthz` answered 200 at `checked_at`. Nothing else
   counts: a proxy in front of a chaperone that is starting answers 204 or
   503, and neither is the chaperone.
3. `unreachable` means it has not answered since `unreachable_since`, for
   longer than the threshold of §3.3's row. §3.3's fault is raised at the
   same moment and carries the same `since`.
4. The block is never null and never absent. A reader that finds no `chaperone`
   key at all is reading a document an older `caregiver` wrote.

```json
{
  "pep": {
    "watch": "ok",
    "url": "http://192.0.2.10:8300",
    "checked_at": "2026-09-21T14:19:02Z",
    "unreachable_since": null
  }
}
```

`state` uses `in_sync` in JSON. The display name is "in sync". That is the only
place a name differs between the file and the invariant text.

## 3. Family states

| `state` | Meaning | Sessions | New turns |
|---|---|---|---|
| `in_sync` | the live family matches `applied_rev`, and `applied_rev` equals `registry_rev` | run | accepted |
| `reconciling` | `caregiver` is applying a change | run | accepted |
| `invalid` | the newest file failed validation. The last good definition still serves. | run | accepted |
| `invalid`, never valid | no revision of this family ever validated | none exist | refused |
| `degraded` | the family runs with a fault `caregiver` cannot fix by itself | run | per fault, §3.3 |

A `degraded` family with EVERY fault `blocks_turns: false` is serving, and
every reader of this document treats it as one. `attendance` already does
(§3.3), and so must anything that waits for a fleet to converge or decides
that a family is fit to take work.

### 3.1 `invalid` does not stop the family

A bad definition yields a report. It never crashes the system and never
half-applies (invariant 19). The last good state stays:
push a broken file, get a report, and the family keeps working.

So `invalid` is a reporting state, not an outage. `attendance` keeps accepting
turns against `applied_rev`.

The one exception is a family whose very first file is invalid. Nothing was
ever applied, so nothing can serve. `caregiver` sets
`validation.never_valid: true` and `attendance` refuses with `family_invalid`
(contract 02 §14).

### 3.2 The validation block

```json
{
  "validation": {
    "rev": "reg-9f21c4",
    "checked_at": "2026-09-18T19:20:11Z",
    "ok": false,
    "never_valid": false,
    "error_count": 2,
    "warning_count": 1,
    "report_path": "/srv/agents/state/creche/families/chat/validation.json",
    "first_error": "tools.kagi: unknown MCP server"
  }
}
```

The status document carries counts and a pointer. The report itself lives at
`report_path`, written by the same temp-file-and-rename discipline.

Contract 01 §7 defines the entries inside the report: `severity`, `loc`, `msg`
and `downgraded`. This contract defines only the block above, so a reader can
tell at a glance whether a family needs attention without opening a second
file.

`first_error` is a convenience for the noticeboard, capped at 200 characters. A reader
that needs more opens the report.

### 3.3 Faults

`degraded` means the family runs with a fault `caregiver` cannot clear on its
own. Each fault says whether it stops turns.

| `code` | Meaning | `blocks_turns` |
|---|---|---|
| `sandbox_start_failed` | a sandbox or a VM did not start. On the vmm: a VM did not boot or restore, or guest init sent no `ControlReady` (decision CH6-3) | no (decision CH82 rule 6) |
| `key_mint_failed` | LiteLLM refused to mint or update the family key | yes |
| `token_publish_failed` | the chaperone would not take the family token | yes |
| `grants_stale` | the chaperone has not re-read the grant file since `applied_rev` | yes |
| `egress_assert_failed` | the denied egress of a sandbox or a VM did not assert as denied (decision CH6-3) | no (decision CH82 rule 6) |
| `pep_unreachable` | the chaperone's `/healthz` has not answered 200 for longer than the watch's threshold, so no tool call, delegation or `enqueue` can be authorized | no |
| `spend_unknown` | LiteLLM did not answer a spend read | no |
| `timer_enable_failed` | a generated trigger timer is not enabled, so that schedule does not fire | no |
| `vmm_protocol_mismatch` | a client of the vmm got the mismatch reply on each of its tries, so the client and the vmm speak two host API protocol numbers (decisions CH25 rule 1, CH33 rule 10). A vmm-only fix clears it with no `attendance` restart (decision CH82 rules 3 and 4) | no |
| `serial_log_full` | the `serial.log` of a VM passed 64 MiB, so the vmm stopped that VM gracefully. The next start of the session writes a new log (decisions CH59 rule 2, CH79 rule 7, CH82 rules 1 and 2) | no |
| `audit_unreadable` | `attendance` cannot read the chaperone's audit, so no session can show `waiting-approval` | no |
| `mcp_install_held` | a declared MCP server is not installed and root has already answered the release that would install it, so `caregiver` files no new request | no |

| Field | Type | Meaning |
|---|---|---|
| `code` | enum | one code from the table above |
| `blocks_turns` | bool | the table above fixes it per code |
| `since` | RFC 3339 | when the detecting service first saw this fault |
| `source` | enum | `caregiver`, `attendance`, `pep` or `vmm`. Who detected it. §3.3.1 |
| `stale` | bool | optional. True when the source's fault file has gone stale. §3.3.1 rule 7 |

A fault may carry extra keys that name its detail, as `message` does below.
A reader ignores a key it does not know.

```json
{
  "faults": [
    {
      "code": "key_mint_failed",
      "blocks_turns": true,
      "since": "2026-09-18T19:14:02Z",
      "source": "caregiver",
      "message": "LiteLLM refused the key update"
    }
  ]
}
```

`attendance` answers `family_degraded` (HTTP 503) when any fault has
`blocks_turns: true`. Otherwise it serves normally and the noticeboard shows the
fault.

`audit_unreadable` earns its place because the alternative is silence.
Contract 04 §8.6 puts a session in `waiting-approval` while a phone decides,
and the only artefact of an open gate outside the chaperone's memory is the audit
file. That file is 0640, owned by the
the chaperone's user, group the noticeboard's user (contract 04 §6). A host that leaves
`attendance`'s user out of that group gets `running` where it should read
`waiting-approval`, on every family at once, and nothing anywhere says why.
It does not stop turns: the tail is how `attendance` LABELS a held call, never
how it decides to run one. Three rules:

1. Only a permission failure raises it. A missing audit file is not a fault,
   because the chaperone may not have written one today.
2. One audit serves every family, so the fault is raised on every family
   `caregiver` publishes.
3. It clears by itself on the first poll that reads the file, with no
   restart of anything.

`pep_unreachable` earns its place because the alternative is silence: with
the chaperone down no tool call works, and without this fault every document still
reads `in_sync`. `caregiver` is the service that already publishes what every family
is, so it is the service that says the action plane is away.

Seven rules:

1. **One probe per interval, for the whole fleet.** Never one per family.
   Eleven families asking the same question eleven times answer it no
   better, and the probe must never sit on a family's pass.
2. **The threshold is 90 seconds and the interval is 10.** Both are
   choices, both are flags (`--pep-unreachable-after-s`,
   `--pep-probe-interval-s`), and §10 rule 8 records that no measurement
   fixes them. 90 is contract 05 §2 rule 5's own number for "long enough
   that a reader must stop believing", and `creche-deploy`
   restarts the chaperone for about 10 to 15 s on every deploy, so it is six
   times the longest flap this host produces on purpose.
3. **One failed probe raises nothing.** The fault needs unbroken silence
   longer than the threshold. `since` is the FIRST failed probe, not the
   moment the threshold ran out: the outage began when the answers
   stopped.
4. **It is raised on every family `caregiver` publishes**, the same
   reading `audit_unreadable` gets in this section: one chaperone serves the
   fleet, so one outage belongs to all of it.
5. **`caregiver` clears it itself at the first answer.** No turn, no
   restart and no human. §3.3.1 rule 8 exists because this contract has
   already met faults nothing could clear, and a fault about the chaperone must
   not join them: a family whose every tool call fails is not a family
   anyone dials on purpose, so nothing else would ever observe the
   recovery.
6. **It does not block turns.** A chat with no tools still answers, and a
   chaperone restart of a few seconds would otherwise become refused turns
   across the whole fleet — turning a 15-second flap into an outage the
   platform caused. The cost is real and belongs here rather than in a
   comment: **an autonomous family that fires during the outage does not
   fail fast, it burns model spend.** A pi process started while the chaperone
   is away fetches the manifest three times over about three seconds and
   then registers NO tools, for the whole life of that process, and the
   turn answers from text alone. A pi process already resident gets
   `pep_unreachable` back from each `/call`, and the tool error says the
   call did not run — without the "do not retry" sentence a policy denial
   carries, so the model may try again. Either way the turn runs to the
   end and LiteLLM bills it.
7. **A probe that hangs or raises is a probe that did not answer.** Every
   transport error is mapped and none escapes: one unmapped client error
   raised five times reaches the unit's start limit.

`mcp_install_held` earns its place because the alternative is a loop or a
silence: without it, the next pass files a refused `mcp-servers` request
again two seconds later, and root pushes the same refusal to the phone again.
Stopping that is invariant 18's near end (the release design, step 3): root's answer holds the next identical request back. A request nobody
files is a server that never arrives, so the holding has to be said.

Six rules:

1. **It is raised while a declared MCP server is missing AND root has
   answered the release for it.** Either alone raises nothing: a missing
   server with no answer yet has a request in flight, and an answered
   release with nothing missing did its job.
2. **It is raised on every family `caregiver` publishes**, the same
   reading `audit_unreadable` and `pep_unreachable` get. One registry and
   one `/opt/mcp` serve the fleet, so a server nobody can install belongs
   to all of it.
3. **It does not block turns.** A family whose grants name that server
   has one tool fewer. Every other turn it takes is unaffected, and
   taking eleven families off the air over one missing upstream would
   turn a missing tool into an outage the platform caused.
4. **`since` is when `caregiver` read the answer**, not a time root
   stamped. The floor runs off a clock this service owns, and the value
   lives in `caregiver`'s own marker file, so it survives a restart and
   does not move on every pass.
5. **Three things clear it, and one of them needs no person**: the set of
   servers the request is for moves, the registry revision moves, or the
   floor runs out and the request goes out again. The floor exists
   because the third thing that could turn the refusal into a success —
   a `mcp-servers` tag appearing in agent-mcp — is in a repository this
   host never reads.
6. **Root's reason is data.** It is root-written and trustworthy about
   the release, and it is still bounded and made printable before it
   reaches a document, because a document is read by a person and a
   newline in a message is a second line that person may believe.

The fault carries `release_id`, `release_status`, `servers`,
`retry_after` and `message` beside §3.3's five fields. A reader that
knows none of them still reads `message`.

```json
{
  "code": "mcp_install_held",
  "blocks_turns": false,
  "since": "2026-09-21T14:22:07Z",
  "source": "caregiver",
  "release_id": "01K5D1XH7GK9MZAQGCCMA5J8NF",
  "release_status": "refused",
  "servers": ["weather"],
  "retry_after": "2026-09-22T14:22:07Z",
  "message": "mcp-servers release 01K5D1XH7GK9MZAQGCCMA5J8NF was refused (request): mcp-servers=latest names no released tag: name a version instead. weather still not installed, no new request before 2026-09-22T14:22:07Z"
}
```

`egress_assert_failed` earns its place because the vmm proves
deny-by-default (invariant 11) and never assumes it (decision CH6-3). At
each VM start and after each egress edit, the vmm checks that the Cloud
Hypervisor command line has no `--net` and that the VM has exactly the
vsock listeners of the ports it bound (§4.3). A failed check is a real hole
in deny-by-default, so the VM stops. At a start, the start fails with the
same code (decision CH34 rule 6). The fault does not stop turns: the next
turn tries a start, and each start runs the check again (decision CH82 rule
6).

### 3.3.1 Faults detected outside `caregiver`

Six codes in §3.3's table are detected by another service. `attendance`
detects `vmm_protocol_mismatch` and `audit_unreadable`. The chaperone detects
`grants_stale`. The vmm detects `sandbox_start_failed`,
`egress_assert_failed` and `serial_log_full` (decisions CH21-1, CH79 rule
7). Each service reports what it sees in a file. `caregiver` reads the three
files and folds the entries into the status document.

`caregiver` raises neither `sandbox_start_failed` nor
`egress_assert_failed` since minor 19, because it starts no VM (§4.3). It
raises `vmm_protocol_mismatch` itself when its own call to the vmm gets the
mismatch reply (decision CH25 rule 1). It raises the same code as
`attendance` (decision CH82 rule 5).

| Property | Value |
|---|---|
| `attendance` writes | `/srv/agents/state/creche/faults/attendance/<family>.json` |
| the chaperone writes | `/srv/agents/state/creche/faults/pep/<family>.json` |
| the vmm writes | `/srv/agents/state/creche/faults/vmm/<family>.json`, under its key `VMM_FAULTS_ROOT` |
| Writer | one service per directory, and nothing else |
| Reader | `caregiver`, and nothing else |
| File mode | 0640, owner the writing service's user, group `caregiver`'s user |
| Directory mode | 0750, same owner and group |
| One file | one family |

The mode and group follow contract 04 §6's audit pattern: the writer owns the
file, the one reader gets it by group. The chaperone runs as its own user and
writes its own directory, so `caregiver` never writes into any of the three
directories and each file keeps one writer (§1).

Rules:

1. The file holds that writer's current open faults for that family. It is not
   an event log. Each entry is §3.3's fault object with `source` already set.
2. The writer rewrites the whole file on every change. An empty `faults` list
   clears every fault that writer raised.
3. The write is atomic: a temporary file in the same directory, `fsync`, then
   `rename`. Same shape as §2 rule 2 and contract 04 §1.3.
4. `caregiver` watches the three directories. It merges what it reads with
   the faults it found itself and rewrites the status document (§2 rule 4).
5. A missing file means that writer raised nothing. A missing file is not a
   fault.
6. `caregiver` drops an entry whose `code` is outside §3.3's table, and an
   entry whose `code` belongs to a different writer. It counts each refusal in
   its own log. A fault file is input from another process, so `caregiver`
   validates it the way contract 03 §13 validates a playpen line.
7. `written_at` older than 90 seconds makes the file stale. `caregiver` keeps
   the entries and sets `stale: true` on each, because a dead writer is not
   proof that the fault cleared. A writer with an open fault refreshes it
   faster than that: the chaperone and the vmm every 30 seconds (contract 04 §1.6
   rule 4).
8. **Two events clear a writer's faults, and both are evidence.** A
   turn-blocking fault would otherwise be permanent: a blocked family gets no
   new turn, so nothing dials its sandbox, so nothing ever observes that the
   fault has gone.

   | Event | Clears | Why it is evidence |
   |---|---|---|
   | a completed handshake: a `ControlReady` of a VM of that family for the vmm, a vmm reply with no mismatch for `attendance` (decision CH82 rule 4) | every fault that writer raised for that family | a healthy link is proof the complaint no longer holds |
   | the writer's own service start | the same | the file holds "this writer's current open faults", and a fresh process has observed none |

   Neither hides anything. A fault that still holds is raised again on the
   next dial.

   The `attendance` clear also clears its `audit_unreadable`. If the audit
   is still unreadable, the next poll raises it again.

   No fault of the vmm blocks turns (decision CH82 rules 2 and 6), and
   neither does `vmm_protocol_mismatch` (rule 3). So `attendance` lets the
   next turn ask for a start, and each start runs every check again: the
   digests and the deny check, which fail closed. A broken family spends
   one boot try per turn, and the caller gets the start error each time. No
   vmm fault then waits for a `ControlReady` that no turn can bring.

9. **A `grants_stale` older than the grant file on disk is not current.**
   `caregiver` leaves it out of the status document: it does not make the
   family `degraded` and it does not stop turns. The chaperone raises that code
   about one FILE (contract 04 §1.6 rule 1). Once `caregiver` has written that
   file again, a complaint from before the write describes a revision that is
   no longer on disk, and publishing it reports a failure this pass already
   repaired.

   ```
   probe raises        caregiver writes        the chaperone's sweep re-reads
   grants_stale   ->   grants/<f>.json   ->   and clears it or renews it
        |                     |
        `-- since ------------'   older => this fault is not about that file
   ```

   Four rules:

   1. Compare the fault's `since` with the grant file's own modification
      time. One host, one clock, so the two compare directly.
   2. `since` is second-resolution and truncated, so allow one second of
      slack. The slack only ever KEEPS a fault that might still hold. It
      never drops one that does.
   3. No grant file means nothing to compare against, and every fault is
      kept. A revoked family keeps its fault.
   4. Only the chaperone's `grants_stale` is judged this way. A channel fault or a
      sandbox fault says nothing about a grant file, so a grant file's write
      time says nothing about them.

   Nothing is lost. `caregiver` logs each entry it drops, with the two times,
   so the journal holds what the document no longer says. If the fault still
   holds, contract 04 §1.6 rule 7's sweep raises it again within its
   interval, with a `since` this rule then keeps.

   Both halves are needed: without rule 9 the stale entry blocks
   the family, and without contract 04 §1.6 rule 7 nothing clears a fault
   that IS current.

   `caregiver` reads the fault files again after writing the grant file, not
   only at the start of a pass. Read only at the start, there is no newer
   file to compare against and this rule can never fire.

```json
{
  "family": "chat",
  "source": "vmm",
  "written_at": "2026-11-08T19:21:07Z",
  "faults": [
    {
      "code": "serial_log_full",
      "blocks_turns": false,
      "since": "2026-11-08T19:20:55Z",
      "source": "vmm",
      "message": "serial.log passed 64 MiB, the VM was stopped"
    }
  ]
}
```

`attendance` and the vmm never ask `caregiver` for anything. They report, and
`caregiver` decides.

### 3.4 The reconcile block

```json
{
  "reconcile": {
    "since": "2026-09-18T19:19:40Z",
    "from_rev": "reg-8c01aa",
    "to_rev": "reg-9f21c4",
    "step": "write_config",
    "attempts": 1
  }
}
```

`step` is one of `validate`, `write_grants`, `update_key`, `write_config`,
`set_egress`, `write_timers`. `set_egress` writes `egress.json` and
`repos.json` (contract 01 §6.1.1, decision CH69 rule 6). `write_timers`
writes the timer set of the family's triggers.

Minor 20 drops `create_sandbox`, `switch_sandbox`, `destroy_sandbox` and the
field `needs_switch`. `caregiver` creates, switches and destroys no VM
(decisions CH1, CH21-1). The vmm starts a VM at a session's first prompt
and stops it itself (§4.3, §4.4).

`step` is the step IN FLIGHT, and the block is published **before** that
step runs. **A pass that is inside a step is `reconciling`, whatever
moved.** A new root disk moves no registry revision, so `from_rev` and
`to_rev` can be equal, and that is not a contradiction: the revisions say
what the registry asked for, and the block says a step is running. A reader
that sees the same `step` twice is looking at a step that is still running,
not at a repeat.

`write_config` rewrites the family config mount: `instructions.md`, `skills/`,
`runtime.json` and `shape.json` (contract 01 §6.1, whose rule 7 says when it
starts to write `shape.json`). One step covers all four, because one revision
counter covers the directory.

### 3.5 Which change needs which step

The reload rules fix this. Contract 01 owns the field-by-field mapping. The
summary here exists so a reader of this contract knows which change needs a
new VM.

| Change to a family | How it lands | Needs a new VM |
|---|---|---|
| tools, MCP grants | the chaperone reads the family's grants live, per call | no |
| egress | `caregiver` rewrites `egress.json` (contract 01 §6.1.1). The vmm proxy reads it on every CONNECT | no |
| model, budget | key update at LiteLLM | no |
| instructions, skills | `caregiver` rewrites the family config mount, read at turn start | no |
| `shell`, `sandbox_tools` | `caregiver` rewrites `runtime.json` in the same mount. The facade reads `sandbox_tools` on the family-state pull before each prompt (decision CH5c). The pull carries no `shell` (contract 01 §3.8 rule 4) | no |
| resources, image | `caregiver` rewrites `shape.json` in the same mount. A running VM keeps its shape until it sleeps or stops. A restore whose snapshot shape differs from `shape.json` drops the snapshot and cold-boots, as a disk digest change does (decision CH82 rule 7) | at its next start |

Egress needs no new VM (decisions CH6-4, CH69 rules 11 and 12, CH74):

1. A removed host is refused at the next CONNECT. An open tunnel runs to
   its own end (contract 01 §3.7 rule 11).
2. An added host waits at most 2 s. The vmm reads `egress.json` of each
   running VM every 2 s. It binds or unbinds host port 1104 by the rule of
   §4.3 step 1.
3. After each such edit the vmm runs the deny check of §4.3 again. A failed
   check, or a failed bind, raises `egress_assert_failed` and stops that VM.

No session notices any row in this table. A permission
change never ends a session (invariant 9). Mounts leave the table, because
decision CH10-10 drops `files`.

## 4. Sandboxes

### 4.1 Identity

**The status document holds no sandbox row** (minor 18, decision CH21-1).
The vmm owns each VM, one per session (decision CH1). The noticeboard reads
the VM state and the session state from `attendance`, which passes on the
`vm` field of the vmm list row (decisions CH21-1, CH33 rule 9, CH79 rule 4,
§8). `caregiver` publishes no sandbox id.

**`caregiver` passes no image.** The vmm starts each VM from the root disk
of the family's flavor, which it reads from the live `disks` tree
(contract 01 §3.9, contract 06 §1 rule 15). Minor 21 drops the image
argument per flavor and the fault `image_flavor_unconfigured`.

#### 4.1.1 `supervisor_env`

The field left with the sandbox row in minor 18. Under the vmm the playpen
reads fixed guest paths, and no supervisor env file exists (decisions CH5e,
CH21-1).

### 4.2 Lifecycle states

The sandbox states left with the sandbox row in minor 18. The vmm reports
the state of each VM (decisions CH5d, CH21-1), and `attendance` dials no
sandbox: it routes each request by session id (decision CH21-7).

### 4.3 Creating one

`caregiver` creates no VM (decisions CH1, CH6-5, CH21-1). Its part is the
family dir: `creds.json`, `config/`, `egress.json` and `repos.json`
(contract 01 §6.1 and §6.1.1). The vmm starts one VM per session, at its
first prompt (decision CH1). Four steps of that start carry egress, and
their fault reaches the status document through the vmm fault file
(§3.3.1):

1. **Bind the host listeners of the VM** (decisions CH6-2, CH14, CH49 rule
   3, CH72 rule 3, CH73 rule 1). The family state port 1100 and the plane
   ports 1101 and 1102 bind on every VM. The mirror port 1103 binds when
   `repos.json` lists a repo or does not read. The egress port 1104 binds
   when `egress.json` lists a host or does not read. An absent file or an
   empty list binds no port.
2. **Start Cloud Hypervisor with vsock only.** The VM has no network card
   (decision CH8-1, design note fact 44). The plane ports splice their
   bytes to LiteLLM and the chaperone with no allowlist lookup (decision CH6-2).
   So the two plane endpoints are never egress rows.
3. **Run the deny check** after the guest's `ControlReady` and before the
   VM serves (decisions CH6-3, CH54 rule 2). The command line has no
   `--net`, and the run dir of the VM holds exactly the listeners of step
   1.
4. **A failed check raises `egress_assert_failed`** in the vmm fault file,
   and the VM stops. The start fails with the same code (decision CH34 rule
   6).

The sandbox-create steps of minor 18 left in minor 19 (decision CH6-5): the
create with network denied, the three canaries, the policy rows and the
mounts. A VM has no route for a canary to prove denied, so the check of
step 3 replaces the probe.

### 4.4 Destroying one

`caregiver` destroys no VM, and no policy row exists to remove (decisions
CH6-5, CH21-1). The vmm stops each VM (decision CH19), and the host
listeners of step 1 of §4.3 go with it. Steps 3 and 4 of minor 18 left in
minor 19 with the policy removal and the sandbox removal.

Credentials die before processes (invariant 13). On a **family deletion**,
`caregiver` deletes the LiteLLM key and the chaperone token first. Then it removes
the family files. It unlinks `egress.json` and `repos.json` next to
`creds.json` (contract 01 §6.1.1 rule 7, decision CH69 rule 23). Last, it
asks the vmm to drop the sessions of the family (decisions CH21-10, CH33
rule 3).

## 5. No switch call

Minor 18 drops the call `POST /internal/switch-sandbox`, and §5.1 to §5.4
with it (decisions CH1, CH21-1). A session has no sandbox to move between:
the vmm runs one VM per session, and `attendance` routes each request by
session id (decision CH21-7). §5.5 stays.

### 5.5 A model change is not instant

A model change lands through the family key, so LiteLLM's key cache sets its
pace. Probe 0b measured that cache on the host on 2026-09-18, on the live
`litellm-headroom:0.37.0` image:

1. A model added with `/key/update` works at once on some workers.
2. A model removed with `/key/update` is refused on every worker only after up
   to about 10 s. Measured 7 s across four workers, with answers flipping
   between workers until then.
3. A budget drop refused the next request at once.

So a revision that narrows `model.router` leaves a window of up to 10 seconds
in which a worker still answers with the old model. §3.5's table calls a
model change "no switch" for that reason: a switch would not help. Contract
01 §6.2 holds the same numbers for the family file's own table.

The window costs which model answers. It never widens reach, because the
perimeter is mounts, egress, the chaperone and the key itself (invariant 12).

## 6. Credentials and the `rotate` verb

One LiteLLM key and one chaperone token per family.
`caregiver` mints both, publishes their epoch, and rotates them.

### 6.1 What the status document publishes

```json
{
  "credentials": {
    "epoch": 7,
    "key_id": "sk-live-chat-7",
    "token_id": "pep-chat-7",
    "rotated_at": "2026-09-18T19:20:00Z",
    "next_rotation_at": "2026-10-18T19:20:00Z",
    "rotation_state": "settled"
  }
}
```

Ids only. No value ever appears here, and the document is mode 0644 precisely
so that rule is easy to check.

`rotation_state` is `settled`, `rotating` or `failed`.

`epoch` is an integer from 1 to 9007199254740991. The first epoch is 1.

No channel message carries `epoch`, and no reader compares it with anything
(contract 03 §12 rule 3, amended on 2026-11-04 by decision CH5b).

### 6.2 Minting, on family create

1. Mint a LiteLLM key with `max_budget` from the family file and a daily
   `budget_duration`.
2. Mint a chaperone token for the family and publish it to the chaperone (contract 04).
3. Write `creds.json` with `epoch: 1`, mode 0600, by temp file and `rename()`.
4. The vmm serves the key and the token to the facade on the family-state
   pull (contract 03 §12 rule 4).

### 6.3 `rotate`

`POST /families/{name}/rotate` on `caregiver`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `scope` | enum | `both` | `key`, `token` or `both`. `token` covers the chaperone token AND every declared webhook's bearer (§6.4 rule 3) |
| `mode` | enum | `graceful` | `graceful` or `immediate` |
| `reason` | enum | `manual` | `scheduled`, `suspicion`, `manual` |

Rotation exists because of a known and accepted cost. A key copied out of a
sandbox stays valid until the family key is rotated. Its reach is LiteLLM on
the LAN address, and the daily budget caps the spend.

**Graceful**, the default:

1. Mint the new key and token.
2. Write `creds.json` with `epoch + 1`.
3. Publish the new `epoch` in the status document.
4. The facade gets the new key and token on its next pull, before its next
   prompt. A turn that started before the rotation keeps the old key and
   the old token (contract 03 §12 rule 9).
5. After `rotation_grace_s`, default 300, delete the old key and the old token.
6. A turn still running on the old key at that moment fails with turn
   `reason: model_error`. `rotation_grace_s` is set above the thin job limit of
   120 seconds for that reason.

The key half does not overlap today. One alias names the key of a family, and
no probe shows that LiteLLM holds two keys under one alias. So the key half
runs: lower the budget of the old key, delete the old key, mint the new key.
For a few calls the family has no key. The token half overlaps as steps 1 to
5 say.

The key half may overlap after one probe on the image of the host shows one
of two facts: a second key under a live alias together with a delete of one
key by its value, or a change of the alias of a key.

**Immediate**, for `reason: suspicion`: delete the old key and token at once.
Every running turn fails with `reason: model_error`. Sessions survive.

Two things rotation never does:

1. It never restarts a VM. The credential reaches the facade on the
   family-state pull, not at the start of the VM (contract 03 §12).
2. It never ends a session (invariant 9).

Rotation is not a removal of reach, so invariant 9's "a removal applies at
once" does not force the immediate mode. A suspicion does.

**What `immediate` actually buys.** Probe 0b, the host, 2026-09-18, on the
live `litellm-headroom:0.37.0` image: a key deleted or narrowed with
`/key/update` is refused on every LiteLLM worker only after up to about 10 s.
Measured 7 s across four workers, with answers flipping between workers until
then. A budget drop refused the next request at once.

1. `immediate` closes the leaked key in about 10 seconds, not instantly.
   `rotation_state` stays `rotating` until that window passes, so the status
   document never claims a key is dead before it is.
2. A budget drop is the faster brake. On `reason: suspicion`, `caregiver`
   lowers the old key's `max_budget` to its current spend before it deletes
   the key. That refusal lands at once, and the delete then closes the window
   properly.
3. The same 10 s applies to `graceful`, but it is invisible there:
   `rotation_grace_s` is 300 by default, which is 30 times the window.

Contract 01 §6.2 carries these numbers for the family file's live-change
table. They come from one probe on one image, so treat 10 s as the measured
ceiling on this host, not as a LiteLLM guarantee.

### 6.4 One bearer per declared webhook

A family file that declares a webhook trigger must open a live route with
no second, hand-made act (invariant 16). `caregiver` mints that route's
bearer beside the family's other credentials.

```
/srv/agents/state/creche/triggers/webhooks/<family>/<name>.token
```

| Property | Value |
|---|---|
| Writer | `caregiver`, and nothing else |
| Reader | `door-trigger`, and nothing else |
| Value | 32 bytes from the system random source, base64, no padding |
| File mode | 0600, owner the user `caregiver` runs as |
| Directory mode | 0700, same owner |
| One file | one declared webhook of one family |

The directory sits outside `families/<family>/`, because `door-trigger`
reads this tree and has no business in the directory that also holds
`creds/` and the config mount.

Rules:

1. **An existing token is kept.** Every apply and every reconcile pass
   calls this. Minting each time would break whatever holds the bearer,
   several times an hour.
2. **A withdrawn webhook loses its file.** The credential exists because
   the family file declares the trigger. When the declaration goes, the
   reach goes with it (invariant 9).
3. **`rotate` with `scope: token` or `scope: both` moves it** (§6.3). There
   is no overlap: one file holds one value, so whatever calls the route is
   refused until the new value is pasted in. Rotating a shared secret costs
   that.
4. **Deleting a family removes the directory**, before any sandbox is
   touched (invariant 13, credentials die before processes).
5. **The status document names the PATH, never the value.** The document is
   0644. A value there would be world-readable, and the path is what an
   operator actually needs.
6. `door-trigger` reads a new or rotated file within its own `refresh_s`,
   with no restart. A route whose file is missing, short, non-UTF-8 or
   readable by more than its owner is simply absent from its table, which
   answers 404 like any unknown route.

```json
{
  "triggers": {
    "webhooks": [
      {
        "name": "boiler-alert",
        "token_path": "/srv/agents/state/creche/triggers/webhooks/ha-review/boiler-alert.token"
      }
    ],
    "enqueue": false
  }
}
```

A family with no webhook trigger publishes `{"webhooks": []}`.

## 7. Spend and budget

1. The family key carries its own `max_budget` and `budget_duration`. No
   LiteLLM team object is needed, because one key per family makes the key's
   own budget the family budget.
2. **LiteLLM is the authority for spend.** The `usage` numbers a playpen
   reports are advisory, because the sandbox is untrusted (contract 03 §13,
   rule 7).
3. `caregiver` reads spend from LiteLLM and publishes it. This is the
   watch-and-reconcile loop's job, not a one-shot apply's: rule 6's "must not
   report `in_sync` before 10 seconds have passed" is a fact something has to
   hold over time. A one-shot apply publishes `spend: null` and raises no
   `spend_unknown`, because nothing was attempted that could fail.

```json
{
  "spend": {
    "window": "day",
    "spend_usd": 3.42,
    "budget_usd": 15.0,
    "as_of": "2026-09-18T19:19:58Z",
    "source": "litellm"
  }
}
```

4. A LiteLLM read that fails raises the non-blocking fault `spend_unknown`
   (§3.3) and leaves the previous numbers with their old `as_of`. A reader
   compares `as_of` with `written_at` and must not present a stale number as
   current.
5. A key past its budget makes LiteLLM answer `BudgetExceededError`. `attendance`
   records the turn `reason: budget_exceeded` (contract 02 §14).
6. A change made with `/key/update` can be served stale by other LiteLLM
   workers for up to 10 seconds (`user_api_key_cache_ttl`). So `caregiver` must
   not treat a model change as live until 10 seconds have passed, and must not
   report `in_sync` before then. Probe 0b measured this on the host on
   2026-09-18: 7 s across four workers for a removed model, answers flipping
   between workers meanwhile, while a budget drop refused the next request at
   once. `caregiver` therefore waits the 10 seconds for a model change and need
   not wait for a budget change.
7. Three open upstream issues touch these numbers: a changed budget keeping its
   old window (#38634), spend counters inflating across two pods (#30460), and
   a metric misreporting remaining budget (#38087). This list comes from a
   source read of LiteLLM v1.103.0. Probe 0b measured point 6 on the host's
   own `litellm-headroom:0.37.0`, but it did not test these three issues.
   They stay open (§10).

## 8. What the one view may read

One view shows every family, VM and session, and that view is the truth
(invariant 20).

| Item | Where | How |
|---|---|---|
| family state, credentials epoch, spend, faults | `/srv/agents/state/creche/families/<family>/status.json` | read the file |
| the validation report | `/srv/agents/state/creche/families/<family>/validation.json` | read the file |
| sessions | the view of the session API with the `view-ro` token | contract 02 §7 |
| the VM state of each session | `attendance`, which passes on the `vm` field of the vmm list row | decisions CH21-1, CH33 rule 9, CH79 rule 4 |
| a session's transcript | the transcript route of the view | contract 02 §7 rule 3 |
| outcome records | `/srv/agents/state/creche/outcomes/<family>/*.json` | read the files |
| audit records | `/srv/agents/state/creche/audit/YYYY-MM-DD.jsonl` | read the files, contract 04 §6 |

The audit files are 0640, owned by the chaperone's user, group the noticeboard's user. The
noticeboard is the only reader, and it sees full tool arguments. The chaperone serves no
read endpoint for them (contract 04 §6).

The noticeboard may not:

1. Read `/srv/agents/state/creche/families/<family>/creds/`.
2. Read any door token file.
3. Write any status document, validation report or outcome record.
4. Write a session or a turn with the `view-ro` token.

The noticeboard changes a family by editing that family's one registry file
(invariant 16). `caregiver` then converges. One action, no manual apply
(invariant 9). The noticeboard never writes runtime state, because runtime state is
derived and the file is the source.

## 9. A full status document

```json
{
  "family": "chat",
  "kind": "attended",
  "state": "reconciling",
  "written_at": "2026-09-18T19:20:12Z",
  "registry_rev": "reg-9f21c4",
  "applied_rev": "reg-8c01aa",
  "config_rev": "reg-8c01aa",
  "validation": {
    "rev": "reg-9f21c4",
    "checked_at": "2026-09-18T19:20:11Z",
    "ok": true,
    "never_valid": false,
    "error_count": 0,
    "warning_count": 1,
    "report_path": "/srv/agents/state/creche/families/chat/validation.json",
    "first_error": null
  },
  "faults": [],
  "reconcile": {
    "since": "2026-09-18T19:19:40Z",
    "from_rev": "reg-8c01aa",
    "to_rev": "reg-9f21c4",
    "step": "write_config",
    "attempts": 1
  },
  "credentials": {
    "epoch": 7,
    "key_id": "sk-live-chat-7",
    "token_id": "pep-chat-7",
    "rotated_at": "2026-09-18T19:20:00Z",
    "next_rotation_at": "2026-10-18T19:20:00Z",
    "rotation_state": "settled"
  },
  "spend": {
    "window": "day",
    "spend_usd": 3.42,
    "budget_usd": 15.0,
    "as_of": "2026-09-18T19:19:58Z",
    "source": "litellm"
  },
  "limits": {
    "max_running_turns": null,
    "max_queued_turns": null,
    "job_timeout_s": null
  },
  "pep": {
    "watch": "ok",
    "url": "http://192.0.2.10:8300",
    "checked_at": "2026-09-18T19:20:10Z",
    "unreachable_since": null
  }
}
```

`limits` is null throughout for an attended family. Only autonomous families
have a turn limit and a queue, and only thin families have a job timeout
(contract 01 §4).

## 10. Not verified

1. **The 10-second rule of §7.6** is one probe on one image: treat 10 s as
   this host's ceiling rather than a LiteLLM guarantee.

2. `rotation_grace_s` of 300 seconds is a choice. No measurement supports it.

3. The 30-second write cadence and the 90-second staleness limit of §2 are
   choices.

4. This item left in minor 18 with the switch call (§5).

5. **Whether LiteLLM's `/key/generate` accepts a second key under an alias a
   live key already holds.** §1 fixes one alias per family, and §6.3's
   graceful rotation keeps both keys live for `rotation_grace_s`. Probe 0b
   proved generate, update and delete-by-alias. It did not probe a duplicate
   alias. The conservative path adds no untested mechanism: rotation mints
   under the same alias and lets LiteLLM decide. If LiteLLM refuses, the fix
   is a suffixed alias during the overlap and a rename back with
   `/key/update`'s `key_alias` once the old key is gone.

6. **The chaperone watch's two numbers of §3.3**, 90 seconds of silence and a
   10-second interval. Both are reasoned from measurements this project
   already has — a deploy's 10 to 15 second restart, §2 rule 5's own 90 —
   and neither was measured as a threshold. The one real outage lasted 91
   minutes, which any threshold under an hour would have caught, so the
   number is not yet under any pressure. What is unmeasured is the other
   side: how often, if ever, this host produces a flap between 15 and 90
   seconds that would fault the fleet for nothing.

7. This item left in minor 19 with the sandbox create (§4.3).

8. This item left when decision CH82 rules 1 to 5 fixed the five readings
   of minor 18 (§3.3, §3.3.1).

9. This item left when decision CH82 rule 6 gave the vmm start faults
   `blocks_turns: false` (§3.3.1 rule 8).

10. This item left when decision CH82 rule 7 fixed when a new shape
    reaches a VM (§3.5).

