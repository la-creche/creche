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
  removes and repairs nothing. An operator step or a later re-import
  fixes the copy.
- Decision CH44 rule 3 (PR D8, `ch_remote.rs`): the timeout of a
  `ch-remote` call comes from its caller. This crate names no value. A
  pause takes about 4 ms (fact 72) and a 16 GiB snapshot about 12 s
  (gap G16).
- Decision CH44 rule 4 (PR D9, `snapshot.rs`): when a snapshot step
  fails, `Snapshotter::take` removes its temp dir and changes nothing
  else. It resumes nothing and stops nothing more. The caller decides.
  After a failure past `shutdown-vmm` the VM is gone, and the next start
  cold-boots (decision CH19 (5)).
- Decision CH44 rule 5 (PR D10, `key.rs`): a key mismatch or an
  unreadable key drops every generation of the session, not only the
  newest. `check_newest` reads no older key. So an older generation
  never restores onto a session disk that moved on (decision CH19 (5)).
  This also takes part of the D13 job of removing older generations.

## Known gaps

- Caller duty (PR D9, `snapshot.rs`): section 4 item 5.7 asks for a
  guest `sync` before each snapshot. The store has no link to the guest,
  so lifecycle syncs the guest through the control link before
  `Snapshotter::take`. No plan PR has that step yet. L17 and L19 need
  it. Without it, a dropped or a consumed snapshot cold-boots from a
  session disk that may lack the last guest writes.
- Host program (PR D9, `snapshot.rs`): the vmm execs the host
  `fallocate` (decision CH29). No row names it as an exec of the vmm.
  Track C must allow that exec, as the CH39 note asks for `zstd` and
  `mke2fs` (C6). L20 needs a config key for its path (the owner env
  rule). Its list has no such key yet.
- Caller duties (PR D10, `key.rs`), for lifecycle: (a) run `write_key`
  after every `Snapshotter::take`, or the next check drops the snapshot
  as `Unreadable`. L17 and L19 snapshot but name no key write. (b) run
  `check_cloud_hypervisor` at the vmm start and refuse to start on an
  error (gap G10 option A). No lifecycle PR (L17, L20) calls it yet.
  Another option for (a): `take` writes `key.json` into the temp dir
  before the rename, so no snapshot exists without a key.
- CONTRACT-QUESTION (PR D11, `claim.rs`): no row decides what a failed
  claim does. A failed make or sync of the `consumed` mark leaves no
  durable mark, so a later start could restore stale RAM onto a session
  disk that moved on. The reading that keeps CH19 (5): the claim renames
  `snapshots/<n>` to `snapshots/<n>.retired` and the caller cold-boots
  (`ClaimError::Retired`). When the rename or its sync fails, the caller
  fails the start (`ClaimError::Stuck`). CH39 rule 3 does not name
  `<n>.retired`. D13 must remove a leftover one.
