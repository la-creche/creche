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
- Decisions CH7-6, CH60 rule 2 and CH62 rule 18 (plan PR V4,
  `initramfs.rs`, `images.rs`): `initramfs::pack` writes the guest init
  initramfs as a newc archive with uid 0, gid 0, mtime 0 and inode
  numbers in entry order, so equal entries give equal bytes and one
  digest across vmm restarts. It refuses a name that the kernel would
  skip (a name size past 4096) and an entry whose parent dir is not an
  earlier entry. `ImageStore::put_made` puts the bytes at
  `store/<sha256>` with an identity file, as an import does. When an
  identity for the digest exists, it writes no byte and returns the check
  of that copy, so a changed copy fails (CH44 rule 2).
- Decision CH76 rule 6 (plan PR V0, `images.rs`): `hash_source` reads
  a source as `ImageStore::import` reads it, through `zstd -d` for a
  `.zst` file, and gives the SHA-256 of the bytes with no file written.
  `vmm check-images` compares it with each entry of `digests.json`.
- Decision CH44 rule 3 (PR D8, `ch_remote.rs`): the timeout of a
  `ch-remote` call comes from its caller. This crate names no value. A
  pause takes about 4 ms (fact 72) and a 16 GiB snapshot about 12 s
  (gap G16).
- Decisions CH42 rule 2 and CH50 rule 10 (PR D15, `ch_remote.rs`):
  `ChRemote::info` returns the state and the device set of the VM: the
  disks, the vsock and each kind that the check of a restore refuses.
  Each list has its own struct with the names of the CH v54
  `VmConfig`. A missing list and `null` read as no device. The type
  skips every other field, so a device kind that it does not name passes
  the read unseen. A new kind needs a field here and in the check
  (plan PR D15). Each refused kind is a list of `Present`, which reads
  no field. A disk keeps `path`, `vhost_user` and `vhost_socket`. CH
  v54 opens the socket and ignores the path of a vhost-user disk, so
  the L17 check must refuse a disk with `vhost_user` or with a
  `vhost_socket`, even on the path of a fixed slot.
- Decision CH44 rule 4 (PR D9, `snapshot.rs`): when a snapshot step
  fails, `Snapshotter::take` removes its temp dir and changes nothing
  else. It resumes nothing and stops nothing more. The caller decides.
  After a failure past `shutdown-vmm` the VM is gone, and the next start
  cold-boots (decision CH19 (5)). The code reads a rename that the sync
  of `snapshots/` cannot make durable as a failed step too, so `take`
  removes `snapshots/<n>` and returns the sync error. It removes
  `key.json` first, so a remove that fails part way leaves a dir with no
  key. The key check drops that dir, and the next start cold-boots
  (decision CH19 (5)). Only a failed remove of the key itself leaves a
  keyed dir, and that dir holds every synced file.
- Decision CH44 and item 42 (plan PR V9, `snapshot.rs`, `key.rs`):
  `Snapshotter::take` writes `key.json` into its temp dir with
  `write_key` before the rename, so no snapshot exists without a key. A
  failed key write is a failed step: `take` removes the temp dir, and no
  generation exists.
- Section 4 item 5.7 and decision CH62 rule 23 (plan PR V9): a guest
  `sync` comes before each snapshot. The store has no link to the guest,
  so the vmm syncs the guest through the control link before
  `Snapshotter::take` (`vmm/src/start.rs`, the D9 caller duty).
- Decision CH44 rule 5 (PR D10, `key.rs`): a key mismatch or an
  unreadable key drops every generation of the session, not only the
  newest. `check_newest` reads no older key. So an older generation
  never restores onto a session disk that moved on (decision CH19 (5)).
  This also takes part of the D13 job of removing older generations.
- Decisions CH61 rule 3, CH62 rules 18 and 24 (plan PR V5, `key.rs`):
  the key names the CH rev, the kernel, the guest init initramfs, the
  root disk and `GUEST_INIT_PROTOCOL`. `SnapshotKey::current` takes the
  checked initramfs, and the key file holds its digest as a string. A
  key file with `null` there holds no key, so it drops as unreadable.
  "Disk digest" of CH7-6 means the root disk only (CH62 rule 24).
- Decision CH44 rule 6 (PR D11, `claim.rs`): a failed make or sync of
  the `consumed` mark leaves no durable mark. So the claim renames
  `snapshots/<n>` to `snapshots/<n>.retired`, and the caller cold-boots
  (`ClaimError::Retired`). When the rename fails too, the caller fails
  the start (`ClaimError::Stuck`). The row names only the rename. The
  code reads a rename that it cannot make durable (the sync of
  `snapshots/` fails) as a failed rename too. D13 removes a leftover
  `<n>.retired`. CH39 rule 3 does not name it.

- Decisions CH2, CH8-7, CH36 G13, CH44 rule 6 and CH50 rules 7 and 8
  (PR D13, `retention.rs`): `sweep` removes leftover `<n>.retired` dirs,
  every generation but the newest of a session, the newest one when a
  claim consumed it, the newest one past `MAX_AGE` (7 days from the
  take, the mtime of `memory-ranges`), and then the oldest first while
  the allocated bytes (`st_blocks`) pass the budget. It never removes a
  session disk or a generation in use. It never removes a generation
  while an older one of the session stays, so an older one never becomes
  the newest.
- Decisions CH27 and CH21-10 (PR D14, `remove.rs`): `remove_session`
  removes the generations oldest first, then the session dir, then the
  row last. The row-last order comes from the plan entry of D14, not
  from a decision row. A failed step leaves the row, and a retry
  finishes. It removes nothing when no row of the family has the id, and
  the route answers `not_found` (decision CH33 rules 4 and 5). The
  caller (L15) stops the VM first and drops a family one row at a time.
- Decisions CH51 and CH55 (PR D16, `layout.rs`): the run root holds
  `SLOTS` = 64 slot dirs `slot/<k>/`, one per slot profile
  `creche-ch-slot-<k>`. `RunRoot::link_vm` makes `vm/<id>` a symlink to
  `../slot/<k>` and replaces any link there in one rename, so a re-link
  moves the VM to another slot. Every `VmRunDir` path stays
  `vm/<id>/...`, so a snapshot's vsock path holds across a restore into
  another slot. `remove_stale` works through the link.
- Decision CH56 rule 1 (PR D16, `layout.rs`): `link_vm` empties
  `slot/<k>/` before it links `vm/<id>` to it. It removes every entry that
  is not a dir (each regular file, socket and link) and no dir, since a
  free slot holds no live CH (CH19 (4)). The fault file of the old VM
  stays in the faults dir, and its serial log stays in the state root.
- Decision CH59 rule 2 (PR D17, `serial_log.rs`): the serial log of a
  session is `sessions/<id>/serial.log` in the state root, not in the
  run root, because systemd removes the run root at each stop of the vmm
  unit, a crash included. `SerialLog::fresh` makes it new with mode 0600
  (gap G23) before each launch, a boot and a restore alike, so the log
  of the last VM stays until the next start of the session. A drop of
  the session removes it with the session dir. `tests/serial_log.rs`
  proves that a removal of the run root keeps it. The kvm tests of
  vmm-confine prove it under systemd: the vmm stand-in copies
  `RuntimeDirectory=vmm`, and the log outlives a unit restart and a
  crash.
- The caller duties of PR D16 (`layout.rs`). Plan PR L16b meets all
  three in `rust/crates/vmm/src/start.rs` ("The slots" in the vmm
  `AGENTS.md`). (a) Each start runs `link_vm` before `remove_stale`, so
  an old link to a slot that another VM took removes no file of that VM.
  (b) A VM that frees its slot runs `unlink_vm`. (c) A start links a VM
  only to a free slot, one that no live CH holds. `link_vm` empties the
  slot (CH56 rule 1), so a link to a held slot would remove the sockets
  of the CH in it.

## Known gaps

- Host program (PR D9, `snapshot.rs`): the vmm execs the host
  `fallocate` (decision CH29). The exec needs no Landlock rule. Item 42
  notes that the vmm process itself is not under Landlock, because C6
  confines only the CH launch. L20 needs a config key for its path (the
  owner env rule). Its list has no such key yet. Item 42 lists config
  keys for `fallocate`, `zstd` and `mke2fs` as a lifecycle duty.
- Caller duty (PR D12, `sleep_gate.rs`), for lifecycle (L17): a VM with
  an open door link counts as used now in the eviction order (decision
  CH50 rule 6). When L17 builds the `Running` list for
  `creche_vmm_confine::admission::decide`, it passes `last_use = now`
  for each VM with an open door link. D12 and C9 stay as built.
- Caller duty (PR D12, `sleep_gate.rs`), for lifecycle: pass
  `Activity::facade` as the facade state that guest init reported last,
  in a `ControlReady` or a later `facade` line (decision CH62 rule 11).
  The `turn_seen` flag of decision CH43 rule 8 is gone with that line.
- Caller duty (PR D12, `sleep_gate.rs`), for lifecycle: pass
  `Activity::may_evict`, not `may_sleep`, as
  `admission::Gate::Idle`. Admission snapshots an idle VM sooner than
  `IDLE_TIMEOUT` (decision CH8-6), and the doc of `Gate::Idle` says
  "may sleep now". L17 maps the two.
- Caller duties (PR D13, `retention.rs`), for lifecycle (L18): (a) name
  in `InUse` each generation that a VM restores from, from the key check
  of its start until its Cloud Hypervisor ends (fact 55, decision CH50
  rule 8). The key check comes before the claim. A sweep drops a
  consumed newest generation that is not in `InUse`, so a miss here can
  remove the files of a restore that is starting. (b) pass the budget
  from the vmm config. L20 reads it from an env key with `BUDGET` as the
  default (CH36 G14).
