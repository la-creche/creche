# vmm-store

The crate `creche-vmm-store`: what the vmm keeps per session on the host
disk. The root `AGENTS.md` and `rust/crates/vmm/AGENTS.md` apply here too.
The second file has the table of the three vmm crates.

## Rules

- The code is synchronous. It uses std, plus `creche-wire`, `serde`,
  `serde_json` and `sha2`. The crate uses no async runtime.
- The crate never calls `creche-vmm-confine` or `creche-vmm`.

## Known gaps

- CONTRACT-QUESTION (PR D6, `images.rs`): no row decides what the vmm
  does with a store copy that fails the check at VM start. `import`
  returns the recorded entry and does not copy again, so nothing repairs
  the copy. The narrowest reading: `ImageStore::check` removes and
  repairs nothing, and the start fails. The options are a re-import at
  the next start or an operator step.
- CONTRACT-QUESTION (PR D8, `ch_remote.rs`): no row decides the timeout
  of a `ch-remote` call. A pause takes about 4 ms (fact 72) and a 16 GiB
  snapshot about 12 s (gap G16). The narrowest reading: each call takes
  its timeout from its caller, and this crate names no value.
- CONTRACT-QUESTION (PR D9, `snapshot.rs`): no row decides what happens
  to the VM when a step of a snapshot fails. The narrowest reading:
  `Snapshotter::take` removes its temp dir, resumes nothing and stops
  nothing more. The VM stays as the failed step left it, and the caller
  decides. After a failure past `shutdown-vmm` the VM is gone, and the
  next start cold-boots (decision CH19 (5)).
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
- CONTRACT-QUESTION (PR D10, `key.rs`): CH7-6 and the D10 plan entry
  say a key mismatch "drops the snapshot". No row decides whether the
  drop removes the older generations too. `check_newest` removes every
  generation on a mismatch or an unreadable key, and it reads no older
  key. The reason: an older generation could become the newest and
  restore onto a session disk that moved on (decision CH19 (5)). This
  also takes part of the D13 job of removing older generations. The
  other reading: drop the newest only, and leave the older ones to D13.
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
