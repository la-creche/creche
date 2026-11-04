# vmm-store

The crate `creche-vmm-store`: what the vmm keeps per session on the host
disk. The root `AGENTS.md` and `rust/crates/vmm/AGENTS.md` apply here too.
The second file has the table of the three vmm crates.

## Rules

- The code is synchronous. It uses std, plus `creche-wire`, `serde`,
  `serde_json` and `sha2`. The crate uses no async runtime.
- The crate never calls `creche-vmm-confine` or `creche-vmm`.
- Decision CH44 rule 2 (PR D6, `images.rs`): a store copy that fails
  the identity check at VM start fails the start. `ImageStore::check`
  removes and repairs nothing. Item 42 Q42-2 adds that an operator step
  or a later re-import fixes the copy.
- Decision CH44 rule 3 (PR D8, `ch_remote.rs`): the timeout of a
  `ch-remote` call comes from its caller. This crate names no value. A
  pause takes about 4 ms (fact 72) and a 16 GiB snapshot about 12 s
  (gap G16).
- Decision CH44 rule 4 (PR D9, `snapshot.rs`): when a snapshot step
  fails, `Snapshotter::take` removes its temp dir and changes nothing
  else. It resumes nothing and stops nothing more. The caller decides.
  After a failure past `shutdown-vmm` the VM is gone, and the next start
  cold-boots (decision CH19 (5)). The code reads a rename that the sync
  of `snapshots/` cannot make durable as a failed step too, so `take`
  removes `snapshots/<n>` and returns the sync error.
- Decision CH44 rule 5 (PR D10, `key.rs`): a key mismatch or an
  unreadable key drops every generation of the session, not only the
  newest. `check_newest` reads no older key. So an older generation
  never restores onto a session disk that moved on (decision CH19 (5)).
  This also takes part of the D13 job of removing older generations.
- Decision CH44 rule 6 (PR D11, `claim.rs`): a failed make or sync of
  the `consumed` mark leaves no durable mark. So the claim renames
  `snapshots/<n>` to `snapshots/<n>.retired`, and the caller cold-boots
  (`ClaimError::Retired`). When the rename fails too, the caller fails
  the start (`ClaimError::Stuck`). The row names only the rename. The
  code reads a rename that it cannot make durable (the sync of
  `snapshots/` fails) as a failed rename too. D13 removes a leftover
  `<n>.retired`. CH39 rule 3 does not name it.

- Decisions CH2, CH8-7, CH36 G13 and CH44 rule 6 (PR D13,
  `retention.rs`): `sweep` removes leftover `<n>.retired` dirs, every
  generation but the newest of a session, the newest one past `MAX_AGE`
  (7 days), and then the oldest first while the allocated bytes
  (`st_blocks`) pass the budget. It never removes a session disk or a
  generation in use. It never removes a generation while an older one of
  the session stays, so an older one never becomes the newest.

## Known gaps

- Caller duty (PR D9, `snapshot.rs`): section 4 item 5.7 asks for a
  guest `sync` before each snapshot. The store has no link to the guest,
  so lifecycle syncs the guest through the control link before
  `Snapshotter::take`. No plan PR has that step yet. L17 and L19 need
  it. Without it, a dropped or a consumed snapshot cold-boots from a
  session disk that may lack the last guest writes.
- Host program (PR D9, `snapshot.rs`): the vmm execs the host
  `fallocate` (decision CH29). The exec needs no Landlock rule. Item 42
  notes that the vmm process itself is not under Landlock, because C6
  confines only the CH launch. L20 needs a config key for its path (the
  owner env rule). Its list has no such key yet. Item 42 lists config
  keys for `fallocate`, `zstd` and `mke2fs` as a lifecycle duty.
- Caller duties (PR D10, `key.rs`), for lifecycle: (a) run `write_key`
  after every `Snapshotter::take`, or the next check drops the snapshot
  as `Unreadable`. L17 and L19 snapshot but name no key write. (b) run
  `check_cloud_hypervisor` at the vmm start and refuse to start on an
  error (gap G10 option A). No lifecycle PR (L17, L20) calls it yet.
  Item 42 notes a better way for (a) and gives it to a later PR: `take`
  writes `key.json` into its temp dir before the rename, so no snapshot
  exists without a key.
- Caller duty (PR D12, `sleep_gate.rs`), for lifecycle: set
  `Activity::last_use`, the start of the idle time, at the VM start or
  restore, at each turn event, and at each open and each close of a door
  link (decision CH50 rule 5). L18 sets it.
- Caller duty (PR D12, `sleep_gate.rs`), for lifecycle (L17): a VM with
  an open door link counts as used now in the eviction order (decision
  CH50 rule 6). When L17 builds the `Running` list for
  `creche_vmm_confine::admission::decide`, it passes `last_use = now`
  for each VM with an open door link. D12 and C9 stay as built.
- Caller duties (PR D12, `sleep_gate.rs`), for lifecycle: (a) keep a
  `turn_seen` flag per VM that a turn event sets and each
  `ControlReady` clears, for decision CH43 rule 8.
  `creche_vmm::control::ControlState` has no such flag yet. L18 needs
  it. (b) pass `Activity::may_evict`, not `may_sleep`, as
  `admission::Gate::Idle`. Admission snapshots an idle VM sooner than
  `IDLE_TIMEOUT` (decision CH8-6), and the doc of `Gate::Idle` says
  "may sleep now". L17 maps the two.
- CONTRACT-QUESTION (PR D13, `retention.rs`): no decision row names the
  start of the 7 days of decision CH2. The narrowest reading: the take.
  The age runs from the last write of `memory-ranges`, and from the dir
  time when the file is absent.
- CONTRACT-QUESTION (PR D13, `retention.rs`): no decision row says
  whether a newest generation with the `consumed` mark and no VM on it
  drops at once. It never restores again (decision CH19 (5)). The
  narrowest reading: it stays until the age or the budget drops it.
- Caller duties (PR D13, `retention.rs`), for lifecycle (L18): (a) name
  in `InUse` each generation that a running VM restored from, from the
  key check of its start until its Cloud Hypervisor ends (fact 55). (b)
  pass the budget from the vmm config. L20 reads it from an env key
  with `BUDGET` as the default (CH36 G14).
