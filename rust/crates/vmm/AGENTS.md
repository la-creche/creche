# vmm

The crate `creche-vmm`: a library and the program `vmm` (decisions CH9,
CH7-5). One `vmm` process owns every Cloud Hypervisor VM of the playpen.
The root `AGENTS.md` applies here too.

## The three crates

| Crate | Directory | Calls |
|---|---|---|
| `creche-vmm` | `rust/crates/vmm` | `creche-vmm-confine`, `creche-vmm-store`, `creche-wire` |
| `creche-vmm-confine` | `rust/crates/vmm-confine` | `creche-vmm-store`, `creche-wire` |
| `creche-vmm-store` | `rust/crates/vmm-store` | `creche-wire` |

- `creche-vmm` composes two libraries: `creche-vmm-confine` and
  `creche-vmm-store`. `creche-vmm-confine` reads the types of
  `creche-vmm-store`.
- A crate calls only the crates in its row. For these three crates, this
  table replaces the layer rule of the root `AGENTS.md`.
- Each `Cargo.toml` declares every path edge of the table. A change adds
  no path edge.

## The program

`src/main.rs` documents the modes of `vmm` and the exit status 78.
`vmm launch-stage <plan>` takes exactly one plan argument and calls
`creche_vmm_confine::stage::run`, the one entry of the stage. It always
writes the real exec attribute (plan PR L16c, decisions CH55, CH58 rule
1). `vmm kill-scopes` is the `ExecStopPost=` step of the unit (decision
CH63). It reads only `AGENT_SYSTEMCTL_PROGRAM` through
`config::KillScopesConfig`, with the check of `Config`, and calls
`creche_vmm_confine::leftovers::kill_all`. A bad key exits 78, and any
other failure exits 1. `vmm verify --json --env-file <path>` and `vmm
check-images --json --env-file <path>` are the verify hooks of the vmm
row and of the disks row (decision CH76 rules 2 and 6, see "The verify
hooks"). Each takes exactly those three words, in that order, and a
wrong list exits 78. `vmm serve` raises its soft `RLIMIT_NOFILE` to
`planes::fd_need` plus `family_state::fd_need` plus `proxy::fd_need`
in step 4 of the compose step, after the start checks and before the
bind (decision CH67 rule 4 for the bridge need, Known gap 23 for the
family-state need).

## The release row

The handover row `vmm` ships this workspace (CH7-5, CH9). Its manifest is
`rust/component.toml`, and `handover/AGENTS.md` ("The vmm, a binary
component of this repository") holds its rules. In short:

1. One archive holds `vmm`, `guest-init`, `cloud-hypervisor` and
   `ch-remote`. Root installs it at `/opt/components/vmm`, root-owned, so
   the operator runs each program and changes none. The paths are the
   defaults of `VMM_CH_PROGRAM`, `VMM_CH_REMOTE_PROGRAM` and
   `VMM_GUEST_INIT_PROGRAM` (CH62 rule 17), and the `ExecStart=` of
   `systemd/creche-vmm.service`.
2. The CH build is the argv of `bin/lib/chbuild.sh` (CH35 (3), CH75).
3. The verify hook is `vmm verify --json --env-file
   /srv/agents/state/creche/vmm.env` (CH76 rule 2).
4. Each path under `rust/` is a path of the row (item 12 (1)), so each
   merge that changes one mints a vmm tag.
5. The slot profile does not ship. A PR that changes
   `vmm-confine/apparmor/cloud-hypervisor-slots.profile` changes the pin
   in `bin/tests/test_vmm_manifest.py` and names the sudo step (CH76 rule
   9).
6. The gate job `vmm-archive` builds the archive on each PR that changes
   a file of the build, `rust/` among them (CH76 rule 4).

## The compose step

`src/compose.rs` builds the vmm from its config and serves until SIGTERM
(plan PR L20, decisions CH9, CH20). It runs these steps:

1. `shutdown::sigterm` registers the handler of SIGTERM inside the
   runtime, before any other step (caller duty 8).
2. In `spawn_blocking`: `leftovers::stop_all` stops each VM scope of an
   earlier vmm before any per-VM bind (decision CH19 (4), PR C3). Then
   `root_owned::check_program` checks the files of `VMM_CH_PROGRAM`,
   `VMM_CH_REMOTE_PROGRAM` and `VMM_GIT_PROGRAM` before any run of one
   (see "The root-owned files", decision CH69 rule 17). Then `check_cloud_hypervisor` checks the CH
   version, and a failed check refuses the start (caller duty 4, gap G10
   option A). Then
   `image_root::read` reads `digests.json`, and `Follow::start` puts
   each file that it names into the digest store with
   `ImageSet::import` (see "The image root").
   Then `guest_init::write_initramfs` writes the one guest init
   initramfs of this vmm start (see "The guest init initramfs"). Then
   `FaultBook::start` clears every fault file (contract 05 §3.3.1 rule
   8), and `Rows::open` loads the rows.
3. The digest store, the session disks, the spawner, `ChTools` and one
   `ColdBoot`. The vmm reads its own unit with `VmmUnit::of_self`
   (decision CH60 rule 1, no config key). In no service unit of a user
   manager it logs the error and still serves, and every VM launch
   refuses with that error and starts nothing. That `ColdBoot` is the `StartVm` and the `StopVm` hook,
   the `VmSet` of the shutdown and the `Sleeper` of `sleep::run`.
4. `nofile::raise` sets the soft `RLIMIT_NOFILE` to the `fd_need` of
   the compose step, and a hard limit below it exits 78 (decision CH67
   rule 4). The raise runs after the checks of step 2. So a bad
   program, image root or digest exits 78 with its own fault, also on a
   host whose hard limit is below the need, such as the 65536 of a
   GitHub runner. The raise runs before the bind and the first VM, so
   each listener and each CH gets the raised limit.
5. The handler set of `routes!` (`Creates`, `Lists`, `Acp`, `Drops`,
   `Checks`) binds `vmm.sock` in the run root. `sleep::run` runs beside it (caller
   duty 5), and so does `FaultBook::refresh` every `faults::REFRESH`,
   and so does the digests follow (see "The digests follow"). SIGTERM
   aborts the follow.
6. At SIGTERM the sleep loop gets its stop, and `shutdown::shutdown`
   runs with the end of `sleep::run` as its `sleep` future. `src/main.rs`
   then ends the runtime with `shutdown_timeout(RUNTIME_END)`, 4 s of
   the 5 s that `SHUTDOWN_WAIT` leaves (caller duty 8).

A failed start step, or a server that stops before SIGTERM, exits 78. The
end of a shutdown exits 0, also when a VM did not stop: the
`ExecStopPost=` step of the vmm unit kills its scope after the vmm has
exited (decision CH61 rule 1). The vmm has one start entry, `build`, and
it always requires root as the owner of CH, ch-remote, git, the guest
init program and `digests.json` (decision CH58 rule 1: no test-only
entry stays in production code). `build` runs `check` (the leftover
sweep, the CH, ch-remote and git files, the CH version, the image root,
the guest init initramfs), then `parts`. `src/compose/tests.rs` runs on temp paths with
no KVM:

1. In the rust job, a CH copy that the test user owns refuses `build`
   with `NotOwned`, and no digest reaches the store. With CH and
   ch-remote on the root-owned `/usr/bin/true`, a git copy that the test
   user owns refuses `build` with `NotOwned`, and a git symlink to a
   root-owned file with `NotRegular` (decision CH69 rule 17).
2. In the rust job, the serve tests start from `parts` after the checks
   that a test can run: the CH fixture, an import into the store and a
   made initramfs in the store.
   They prove an empty list page, a 412 on a bad protocol header, the
   end of the run at the signal, and a failed run when the bind fails.
3. The ignored `kvm_` tests run `build` on a root-owned `digests.json`
   and root-owned copies of the four programs. The job kvm runs them,
   and its runner may use sudo (decision CH36 G20). They prove that each
   key reaches its part (the `Debug` text of each part gives its program
   path, because the store crate gives no accessor), and that a missing
   image refuses the start. The guest init initramfs of a root-owned
   program is the same across two starts. A group write bit on any of
   the four programs refuses the start and names its key, and so does
   a `digests.json` that the test user owns.

`tests/binary.rs` runs `vmm serve` with no KVM. It proves the exit 78
of a missing CH program and of one that root does not own. Its ignored
`kvm_` test makes root the owner of the files that the start checks, as
`src/compose/tests.rs` does. Under a hard `RLIMIT_NOFILE` of 1024 the
vmm then exits 78 on the fd need itself, and no `vmm.sock` exists
(decision CH67 rule 4). The Hyperion proof of the unit (plan PR C13)
runs the binary end to end.

## The root-owned files

`src/root_owned.rs` holds the one check of each file that the vmm trusts
at its start: `digests.json` (decision CH57 rule 1) and the files of
`VMM_CH_PROGRAM`, `VMM_CH_REMOTE_PROGRAM`, `VMM_GIT_PROGRAM` and
`VMM_GUEST_INIT_PROGRAM` (decisions CH62 rule 17, CH67 rule 1, CH69 rule
17). Each must be a regular file, not
a symlink, owned by root, with no group or other write bit. `open` looks
at the path with no follow of a symlink, opens it, and checks the open
file, so a reader reads the bytes that it checked. `check_program` runs
the same check on CH, ch-remote and git, which the vmm runs by their
paths. A
failed check refuses the start, the fault names the key, and
`src/main.rs` exits 78. The config checks each program key for its form
only (absolute, no `..`). `tests/root_owned.rs` proves each refusal that
a test user can make: a missing file, a symlink, a dir and an owner
other than root.

## The guest init initramfs

`src/guest_init.rs` writes the guest init initramfs once per vmm start
(decisions CH7-6, CH62 rules 17 and 18). `VMM_GUEST_INIT_PROGRAM` names
the `guest-init` file of the vmm row. The row signature covers it, so
the vmm and guest init come from one release. `write_initramfs` opens
the file once with `root_owned::open_program` and reads the open file
(see "The root-owned files"). A failed check refuses the start, and
`src/main.rs` exits 78.

The archive holds `dev` (0755), `dev/console` 5:1 (0600) and `/init`
(the program, 0500), from `creche_vmm_store::initramfs::pack`. Equal
bytes give an equal archive, so one guest init build gives one
`store/<sha256>` across vmm restarts (CH62 rule 18). `put_made` keeps an
equal copy that is there and checks it. `StartConfig::initramfs` holds
its digest. Each VM start checks that store copy (decision CH44 rule 2),
each cold boot passes it as `--initramfs` with `CMDLINE`
`console=ttyS0 rdinit=/init` (CH62 rule 19), the snapshot key holds its
digest, and each restore carries it (CH61 rule 3, CH62 rule 24).

`src/guest_init/tests.rs` proves the entries and each refusal that a
test user can make: an owner other than root, a missing file, a
symlink and a dir. `src/compose/tests.rs` proves a root-owned program in
the job kvm, and `tests/kvm_guest.rs` boots the archive of the static
guest init of `bin/vm-smoke-setup.sh` to `guest-init: up`.

## The config

`src/config.rs` reads the env of `vmm serve` once, at its start (plan PR
L20). Each path and each size is a key with a default in
`env/src/creche_env/default.env` (the owner env rule, gap G5 option A).
The vmm compiles in no default, so a missing or empty key refuses the
start, and so does a bad one. `src/main.rs` then exits 78 (owner item 86).
A fault names the key and never the value, as in `env/AGENTS.md`.

| Key | Default | Use |
|---|---|---|
| `VMM_STATE_ROOT` | `${AGENT_OPERATOR_HOME}/.local/state/creche-vmm` | `StateRoot` (decision CH50 rule 11) |
| `RUNTIME_DIRECTORY` | set by systemd | `RunRoot` (gap G5) |
| `VMM_FAMILY_ROOT` | `${CAREGIVER_STATE_ROOT}/families` | `FamilyRoot` (gap G6) |
| `VMM_FAULTS_ROOT` | `${CAREGIVER_STATE_ROOT}/faults` | `FaultDir::open` (contract 05 §3.3.1) |
| `VMM_IMAGE_ROOT` | `${AGENT_INSTALL_ROOT}/disks` | `image_root::read` (decision CH57 rule 1) |
| `VMM_CH_PROGRAM` | `${AGENT_INSTALL_ROOT}/vmm/bin/cloud-hypervisor` | CH (decisions CH7-5, CH67 rule 1) |
| `VMM_CH_REMOTE_PROGRAM` | `${AGENT_INSTALL_ROOT}/vmm/bin/ch-remote` | `ChRemote` (decision CH67 rule 1) |
| `VMM_GUEST_INIT_PROGRAM` | `${AGENT_INSTALL_ROOT}/vmm/bin/guest-init` | `guest_init::write_initramfs` (decisions CH62 rule 17, CH67 rule 1) |
| `AGENT_SYSTEMD_RUN_PROGRAM` | existing key | `Launch` (PR C2) |
| `AGENT_SYSTEMCTL_PROGRAM` | existing key | `leftovers::stop_all` (PR C3) |
| `VMM_ZSTD_PROGRAM` | `/usr/bin/zstd` | `ImageStore` (gap G9) |
| `VMM_MKE2FS_PROGRAM` | `/usr/sbin/mke2fs` | `SessionDisks` (gap G7) |
| `VMM_FALLOCATE_PROGRAM` | `/usr/bin/fallocate` | `Snapshotter` (decision CH29) |
| `VMM_RAM_CAP_GIB` | `80` | `Limits::ram_cap` (gap G14) |
| `VMM_FREE_FLOOR_GIB` | `50` | `Limits::free_floor` (gap G14) |
| `VMM_SNAPSHOT_BUDGET_GIB` | `150` | `StartConfig::budget`, for `retention::sweep` (gap G14) |
| `VMM_LITELLM_ADDR` | `${AGENT_LAN_ADDRESS}:4000` | the bridge of host port 1101 (decision CH62 rule 20) |
| `VMM_CHAPERONE_ADDR` | `${AGENT_LAN_ADDRESS}:8300` | the bridge of host port 1102 (decision CH62 rule 20) |
| `VMM_EGRESS_LOG` | `${AGENT_OPERATOR_HOME}/.local/state/creche-vmm-egress.jsonl` | `egress::log::Log`, the JSONL log of host port 1104 (decision CH69 rule 10) |
| `VMM_MIRROR_ROOT` | `/srv/agents/code` | `mirror::Mirror::root`, the mirror of host port 1103 (decision CH69 rule 17, fact 13) |
| `VMM_GIT_PROGRAM` | `/usr/bin/git` | `mirror::Mirror::git`, the `upload-pack` of each clone (decisions CH67 rule 1, CH69 rule 17) |

Each path is absolute with no `..` component. The run root holds no
`:`, because systemd joins the paths of more than one
`RuntimeDirectory=` entry with `:` (gap G5 option A). A size is decimal
digits, a whole number of GiB. A plane address is an IP address and a
port, as `std::net::SocketAddr` parses it, and a host name refuses the
start (decision CH67 rule 2). Decision CH62
rule 20 names `10.0.0.39`, the LAN address of Hyperion. Rule 3 of
`env/AGENTS.md` keeps a deployment address out of `default.env`, so
each default takes `AGENT_LAN_ADDRESS`, as `CHAPERONE_LISTEN` does. On
Hyperion it resolves to the address of the rule. `tests/config.rs` holds each fault. It
holds each path default of the table literally, and the size defaults
equal to `Limits::DESIGN` and `retention::BUDGET`.
`tests/binary.rs` runs `vmm serve` with no KVM: a missing or bad key
exits 78 and names the key. `src/compose.rs` is the first reader of each
key. The egress log sits beside the state root, not in it, so it
amends no state-root layout (decision CH39 rule 3). A new key edits
`config.rs`, `default.env`, `VMM_KEYS` of `bin/fleet-up`, `GOOD` of
`tests/binary.rs`, the env of `src/compose/tests.rs` and the env of
`tests/kvm_guest.rs` in one change (decision CH59 rule 1).

## The image root

`src/image_root.rs` reads the `digests.json` of `VMM_IMAGE_ROOT`
(decision CH57 rule 1). The phase 5 disk row installs it there with the
root disks and the kernel. It holds the decompressed digest of each file
(decision CH41 rule 1). Handover checks the release signature at
install, so the vmm trusts the file only when root owns it and the vmm
user cannot write it. `image_root::read` runs these checks:

1. The path holds a regular file, not a symlink or a dir. A missing
   file is `DigestsError::Missing`.
2. It opens the file and checks that the open file is the file of step
   1. Each later check reads the open file, so the parsed bytes are the
   checked bytes.
3. Root owns the file. No caller passes another owner (decision CH58
   rule 1).
4. No group or other write bit is set. The vmm does not read its group
   list, so any group write bit refuses.
5. The file has at most `DIGESTS_CAP` (64 KiB) bytes, and it parses as
   strict JSON (decision CH58 rule 4): the key `kernel` plus one key for
   each value of the wire flavor enum `SandboxFlavor`, each `{"file",
   "sha256"}`, and no other key. Each key comes once. A file is one file
   name in the image root.

The vmm holds no list of the flavors. `PerFlavor` of `creche-wire`
holds one value for each value of `SandboxFlavor`, and its `get` gives
the value of any flavor. So a new flavor of `creche-wire` needs no vmm
change.

Each fault refuses the start, and `src/main.rs` exits 78. Then
`ImageSet::import` copies each file into the digest store with
`ImageStore::import` of `creche-vmm-store`, one at a time, and gives the
`Digests` of `StartConfig`. A store that already holds a digest with its
identity reads no byte. An entry that does not import (a missing disk, a
digest mismatch, a failed `zstd -d`) refuses the start too (decision
CH58 rule 5). `tests/image_root.rs` proves the file checks. In the rust
job it proves that a file the test user owns refuses and imports
nothing. Its ignored `kvm_` tests make a root-owned file with the sudo
of the runner (decision CH36 G20) and prove the accept path, a writable
file, a file past the cap and a malformed one. `src/image_root/tests.rs`
proves the parse and the import on the bytes, with no file check. It also
parses the shared vector `rust/crates/wire/vectors/digests.json`, the
shape that `disks/bin/playpen-release-disks` writes for the disks row
(decision CH76 rule 5).

## The digests follow

A disks release replaces `digests.json` and its files while the vmm
runs, and the disks row has no unit to restart. So the vmm follows the
file (plan PR V1, decisions CH76 rule 8, CH77 rules 2 and 3).
`src/image_root/follow.rs` runs these steps:

1. `Follow::start` imports the set of the vmm start and gives the
   `watch::Receiver<Digests>` of `StartConfig::digests`. Each start
   reads the set once, so its kernel and root disk come from one set.
2. Every `follow::POLL` (5 s) it reads the bytes of the file with the
   checks of `read`, in `spawn_blocking` (decision CH36 G21), as the
   edit follow polls the reach files (decision CH69 rule 11). Bytes
   equal to the bytes of the set of new starts import nothing and log
   nothing.
3. New bytes wait. The follow imports them only after they read the
   same at each poll for `follow::HOOK_TIMEOUT` (300 s), the timeout of
   the disks verify hook, plus one poll: `follow::SETTLE_POLLS` (61)
   polls, 305 s (decision CH77 rule 2). Any other reading ends the
   wait, and the next new bytes wait from the start. The start of a
   wait logs one line.
4. Settled bytes parse, and each entry goes into the digest store. Only
   when every entry imports does the follow swap the set, and it logs
   one line that names each digest. A parse or an import that fails
   keeps the old set (decision CH58 rule 5).
5. The follow tries the bytes of a failed parse or import again every
   `follow::RETRY_POLLS` polls (12 polls, one minute, decision CH77 rule
   3). So a fault that clears, such as a full state root or a killed
   `zstd -d`, still swaps the set with no vmm restart (decisions CH48
   (2), CH76 rule 8). The import skips each entry that the store holds,
   so a retry rereads only the entries that did not import. A retry of
   the same bytes needs no new wait.

Each fault logs one line (decision CH77 rule 3). A fault logs again only
when its reading or its text changes, and a read that takes no bytes is
such a reading.

A running VM keeps its store copies, since no step removes a store file.
Its snapshot holds the key of its start, which holds the root disk
digest (decisions CH7-6, CH62 rule 24). So after a swap the next start
of an idle session drops that snapshot and cold-boots on the new disk.
The follow is the only import after the vmm start, so the store still
runs one import at a time. An import that runs at SIGTERM ends with the
process, and its `<sha256>.tmp` file stays until the next import of that
digest truncates it. The handover swap of the disks tree is two renames,
so the follow may log one `is missing` line between them.

handover swaps the disks tree first and runs `vmm check-images` after it
for at most 300 s (decision CH76 rule 6, gap Q68-6). The wait of step 3
is one poll longer than the hook timeout (decision CH77 rule 2). The
first read of the new bytes comes no sooner than the rename. The extra
poll covers the start of the hook after the rename, and the kill of a
hook that times out and the restore after it. The wait assumes that
these take 5 s at most. So the wait outlasts the hook, also a hook that
times out, and the follow never decompresses a disk while the hook
does. A failed hook restores the previous tree inside the wait, and the
follow then reads the bytes of the set of new starts, which end the wait
and import nothing. So a release that its hook rolls back never reaches
a VM, and a disks release reaches new starts about 5 min after its
install. A restart of the vmm inside the wait imports the file that it
reads at once, as each vmm start does (decision CH58 rule 5).

`src/image_root/follow/tests.rs` proves the steps on the bytes in the
rust job, one `Follow::apply` for each poll: a swap only after every
import, a failed import that keeps the old set, unchanged bytes that
import nothing, and a failed import that tries again and swaps once its
fault clears. For the wait (decision CH77 rule 2) it proves that bytes
that stay the same for 61 polls import once, bytes that change inside
the wait import nothing, and a rolled-back file imports nothing. Each of
the three fails when the wait is removed. It also proves that bytes
whose import failed wait again in full once other bytes come between.
The ignored `kvm_a_disk_swap_cold_boots_a_snapshot_of_the_old_disk` of
`tests/kvm_reach.rs` makes the new `digests.json` root-owned with the
runner's sudo (decision CH36 G20). It runs the real `vmm serve`, as
the E4 tests do, so it proves the compose path: `Vmm::run` swaps the
set of new starts no sooner than `HOOK_TIMEOUT` plus one poll after the
rename, and within four polls after `HOOK_TIMEOUT`. The vmm log line of
the swap names the new set. The VM that started on the old disk sleeps
on the idle timeout in the same wait, and the next start drops its
snapshot and cold-boots on the new disk. This is the one 305 s wait of the kvm job
(decision CH75 rule 2): the job takes about 5.5 min more.

## The verify hooks

`src/verify.rs` and `src/check_images.rs` are the two verify hooks of
contract 06 §4 (decision CH76 rules 2 and 6). handover gives a hook a
small fixed env, never the env of its unit (§4 rule 7). The vmm compiles
in no default (decision CH59 rule 1). So each hook takes `--env-file
<path>`, the `vmm.env` of `systemd/creche-vmm.service`, and reads it
itself with `src/env_file.rs`. Each hook writes one `verify::Report` on
stdout, one JSON line `{"ok", "checks": [{"name", "ok", "detail"}]}`, as
`caregiver-verify --json` does. It exits 0 when every check passes, else
1. A detail names a key or a path, never a value of another key.

`src/env_file.rs` reads the form of §4 rule 7: one `KEY=value` per line,
no shell, no quoting, no expansion. It also takes the `#` header line of
`bin/fleet-up` and any key order. It refuses each value that systemd
reads in another way: a leading quote, a backslash, a space or a tab at
either end, a CR or a NUL. So the hook and the unit read one value. The
guest init reader refuses a comment and an unsorted file, and a path
edge to `creche-guest-init` is a lock-lane change, so the vmm holds its
own reader. `tests/env_file.rs` proves the fleet-up file and each
refusal.

`vmm verify` (CH76 rule 2) runs these checks, in this order:

1. `env-file`: the file reads, and `VerifyConfig` reads
   `VMM_CH_PROGRAM`, `VMM_CH_REMOTE_PROGRAM` and
   `VMM_GUEST_INIT_PROGRAM` from it with the checks of `Config`. A
   failure ends the run with this one check.
2. `vmm`: the vmm program itself, which has no key
   (`std::env::current_exe`), gets the check of `root_owned::open`
   (decision CH67 rule 1). Then each of the three keys gets the same
   check.
3. `ch-version`: `check_cloud_hypervisor` with `VERSION_WAIT` of the
   compose step (gap G10). It runs only on a CH that passed its owner
   check.
4. `guest-init-static`: the open file of the owner check is a 64-bit
   little-endian ELF program (`ET_EXEC`, or the `ET_DYN` of a static-pie
   musl build) for x86_64 with no `PT_INTERP` header, the rule of
   `bin/vm-smoke-setup.sh`. It runs only on a trusted guest init.

It opens no socket, reads no disk, writes no file and starts no VM. So it
passes on the first vmm release, when the unit is not installed and no
disk exists. A serve fault that needs a disk or KVM shows in the unit
journal, not in the hook.

`vmm check-images` (CH76 rule 6) reads `VMM_IMAGE_ROOT` and
`VMM_ZSTD_PROGRAM` with `CheckImagesConfig`, then `digests.json` with
`image_root::read`, the one reader of the file (decisions CH57 rule 1,
CH58 rules 4 and 5). `ImageSet::check` hashes each named file with
`hash_source` of `creche-vmm-store`, through `zstd -d` for a `.zst` file,
and compares the digest with its entry. It writes no file, also not in
the state root. Each entry gets its own check, named by its key, and a
failed entry names its file.

handover runs check-images after it swaps the disks tree, so the hook
does not run before the vmm reads the new files. The digests follow of
a running vmm imports the new set only after the 300 s of the hook
plus one 5 s poll (decision CH77 rule 2, see "The digests follow"). The
extra poll covers the start of the hook and a restore after a timeout.
A failed hook restores the previous tree before the follow imports it.

`tests/verify.rs` and `tests/check_images.rs` run the program in the rust
job. A test user owns each copy there, so they prove the refusals: an
owner other than root, a missing key, a missing `digests.json`, a
dynamic guest init, the JSON line and a wrong argument list.
`src/image_root/tests.rs` proves the digest check of each entry on the
bytes. The ignored `kvm_` tests of `tests/kvm_verify.rs` make root-owned
copies with the runner's sudo (decision CH36 G20). They prove that
verify passes on the CI build of CH (decision CH75), ch-remote, guest
init and the vmm itself, and that check-images passes on root-owned
`.zst` disks and fails on a changed one.

## The server

`src/server/` holds the host API server (decisions CH20, CH26).
`server::run_with` accepts on a socket path that its caller passes in and
serves one HTTP/1.1 connection per accept, with upgrades on. The protocol
header is the first check of each request (decisions CH33 rule 1, CH34
rule 1). `server::run` serves no route: it answers each request that
passes with `no_route`. `Stop` stops the server through `tokio::sync`
(decision CH26 rule 2).

`server::run_routes` serves the one route table (decision CH25). A
handler set implements `Serve<R>` of `creche-wire` for each JSON route
and `ServeUpgrade<R>` for each Upgrade route. `creche_vmm::routes!` then
expands `route_table!` for it, so a set that misses a route does not
build. The router checks the route and the method, then the `Upgrade`
header of an Upgrade route, then the body, then the request type
(decisions CH26 rule 3, CH33 rule 13, CH34 rule 7). It answers 101 only
after the handler admits the request. A `server::Relay` then owns the
stream.

After hyper ends a connection that it did not upgrade, the server shuts
its write half, then reads and drops request bytes until the client
closes, `LINGER_BYTES` or `LINGER_TIME` (`src/server/accept.rs`). A
refusal can come before the server reads the body. Without this step,
the close with bytes unread resets the socket on Linux, and a client that
writes the whole request before it reads loses its typed reply (decision
CH26).

`server::BodyCheck` holds the body checks that follow the route, in the
order of decisions CH26 and CH33 rule 13: `Content-Type`, then the 1 MiB
cap, then strict JSON. Each refusal writes the reply of a shared code.
The cap binds the JSON body only (decision CH34 rule 3). The strict JSON
check is `creche_wire::strict`, the check of each vsock JSON line too
(decision CH65 rule 2).

`examples/upgrade_echo/` is a server with one handler that echoes after
101. `tests/upgrade_body.rs` and `integration/proc/test_proc_vmm_upgrade.py`
use it to prove that the JSON body of an Upgrade request crosses
(`wire/AGENTS.md`, the `acp` route).

## The control link

`src/control.rs` opens the control link to guest init on guest port 1200
(decisions CH13, CH14). `control::open` dials through the vsock socket
of the VM (`VmRunDir::vsock_socket` of `creche-vmm-store`). It writes
`CONNECT 1200` and waits for `OK`. It retries on ENOENT and on an empty
reply, because both come before the guest listener exists (fact 74). It
also retries on ECONNREFUSED (decision CH43 rule 7). `DIAL_PAUSE` (10 ms,
decision CH43 rule 5) is the pause between two tries. It
then sends `ControlHello` with the wall time of the vmm (decision CH28)
and reads one `ControlReady` with the strict reader, `creche_wire::strict`
(rule 3 of "The vsock JSON links" in `wire/AGENTS.md`). `READY_WAIT` (10 s,
decision CH36 G26) bounds the whole open. A close before `ControlReady`
is a fault of the VM: `ControlError::needs_cold_boot` is true (decision
CH32 rule 3). Decision CH43 rule 6 allows a cold boot only when guest
init closes the link before `ControlReady`. The rule names four cases
that end the open with a reported fault and no cold boot: a bad line, a
timeout, a refused `CONNECT` and a failed dial. The rule does not name
`Io` (a failed read or write) or `Clock`. The code reads "only" to cover
every other error too, so each of them also ends the open with a
reported fault and no cold boot.

`control::ControlState` keeps the current link and the last facade state
of each VM. Each change names its `LinkId`, and only the current link
changes the state. So the late EOF of a replaced link changes nothing
(decision CH19 (2), fact 74). A link close keeps the facade state,
because it never stops the facade (decision CH19 (1)).

After `ControlReady`, `ControlLink::event` reads each `GuestEvent` that
guest init sends (decisions CH21, CH62 rule 11): a `facade` line at each
change of the facade state, a `synced` line, and each turn event of the
facade nested in a `turn` line. `ControlState::turn` applies each turn
event in memory: a start marks a turn running, and an end clears the
mark. `ControlState::report` keeps the facade state of each `facade`
line, and `ControlState::ready` does the same for each `ControlReady`.
The turn state then follows the flag of a `running` state, and any
other state clears it, so a facade that exits mid-turn blocks no
snapshot (decision CH62 rule 11). Each returns a `Change`. `Change::Applied` holds the turn
end, if any, and the caller writes it with `Rows::turn_end` of
`creche-vmm-store`. That write sets `updated_at`, and it sets the title
only while the row has none (decision CH32 rule 4). So an end that a
restore dropped still arrives through the next ready (fact 45). The
write syncs a file and a dir, so the caller runs it in `spawn_blocking`
(decision CH36 G21). A close of the link after `ControlReady` ends
`event` with `ControlError::Ended`, which asks for no cold boot.
`ControlState::turn_running` is the turn state for the sleep gate
(decision CH19 (6)). The vmm keeps the facade state that guest init
reported last, and turn events set `turn_running`.
`ControlLink::stop` sends the graceful stop
`{"stop":{}}` (decision CH19 (3)).

`ControlLink::split` splits a link after `ControlReady`: a reader of the
guest events (`GuestEvents`) and a `StopSender` for the graceful stop
and the sync (plan PR L18b).
`ColdBoot` splits the link when a start commits its VM. The table keeps
the sender, and one task per VM runs the reader. The reader passes each
`facade` line to `ControlState::report` and each turn event to
`ControlState::turn`. It writes each turn end with `Rows::turn_end` in
`spawn_blocking`, as for a ready (decision CH32 rule 4).
`StopSender::sync` sends `{"sync":{}}` before a take and waits
`SYNC_WAIT` (10 s) for `synced` (decision CH62 rule 23). Guest init runs
syncfs of the session disk, then sync. The reader hands each `synced`
to the sync that waits. A `synced` with no waiter, such as one after
`SYNC_WAIT`, is logged and dropped, and the reader reads on. The end of
the reader ends a sync that waits at once. A refused line ends the reader (decision CH43 rule 2). So does the
close of the link, or an event of a replaced link. The reader holds the
only strong handle of the stream, so its end closes the link. The sender
then sends nothing, and a stop skips the graceful step (decision CH50
rule 3). A VM whose link closed has no control link for the sleep gate,
so it never sleeps and is never evicted.

Decision CH54 rule 6: a refused line is a guest fault. The reader logs
the line with the session id (`ControlLink::next_event` gives its first
`REFUSED_LOG_CAP` bytes, 512). Then it stops the VM at once through the
stop path of the `StopVm` hook (`Stopper`). The link is gone, so the
stop sends SIGTERM, then SIGKILL. The VM leaves the table and frees its
RAM, so the next start of the session cold-boots (decision CH19 (5)).
A reader whose link is no longer current stops nothing. The stop names
the link too (`Vm::link_id`): it takes only the VM of that link, and a
later VM of the session runs on. `tests/start.rs` proves the events, the
row write, the gate, the stop after a refused line, a stale reader, and
a new VM of the session that the old reader leaves alone.

## The acp route

`src/handlers/acp.rs` admits or refuses an `acp` Upgrade before 101, in
the order of decision CH34 rule 7. It reads the row first (`not_found`
404 for an unknown id or another family, decisions CH21-9, CH33 rule
5). Then it claims the session in the relay registry (`busy` 409,
decision CH33 rule 7) and reads the row again. So another family never
touches the claim of the owner. Then the `StartVm` hook starts the VM: admission, the
boot or restore, and the first `ControlReady`, whatever facade state it
reports. Each `StartFault` is a typed code before 101. An unknown id
never reaches the hook, so it gets no boot. After `ControlReady` the
hook dials guest port 1201 with `AcpLink::dial` and returns the link, so
a failed dial is `sandbox_start_failed` before 101 (decision CH50 rule
2). A drop during the start or the dial gives `not_found`, even when the
drop made the dial fail. `start::ColdBoot` is the hook of the vmm.
`tests/acp.rs` fakes it, and a fake vsock socket plays guest port 1201.

`src/relay.rs` holds the relay registry with a stop signal (decision
CH26 rule 5). `Relays::claim` holds a session for one Upgrade until the
relay ends. `Relays::mark_drop` stops the open relay of a session and
marks the drop. An Upgrade under the mark gets `not_found` (decision
CH34 rule 6), and so does one whose start a drop overlapped. The drop
keeps its mark until the row is gone. The second row read comes after
the claim, so a drop that ends between the first read and the claim
leaves no row, and the Upgrade gets `not_found`.

`AcpLink::dial` dials guest port 1201 with `control::dial`, the dial of
the control link (decision CH13). It pauses 10 ms between two tries and
retries ECONNREFUSED, and `READY_WAIT` bounds it (decision CH43 rules 5
and 7). After 101, `AcpRelay` copies bytes both ways over that link and
sends the bytes that came after `OK` first (decision CH20). An EOF
in one direction shuts down the write half of the other stream, and the
other direction runs on, as with `copy_bidirectional`. The relay ends
when both directions reach EOF, a read or write fails, or the stop
signal fires. Then it drops both streams. It
parses no ACP byte and caps none (decision CH34 rule 3). `Relays::door`
gives the door of a session for the sleep gate (decision CH19 (6)): open
from the claim to its end, then the time that the last admitted Upgrade
ended. A refused Upgrade records no close, so the map of close times
grows only with sessions that ran. A drop clears the time.

## The create and list routes

`src/handlers/create.rs` serves `create` (plan PR L7, decisions CH21-4,
CH25 rule 2). It first reads the family with `FamilyRoot::exists` of
`creche-vmm-confine`. A family with no `config/` dir gets
`unknown_family` 404 (decision CH33 rule 3). Then `Rows::create` of
`creche-vmm-store` makes the row. The same typed request gives the same
row, and a different one gives `conflict` 409 (decision CH33 rule 12).
The family read can sleep between its tries (decision CH50 rule 9), and
the row write syncs a file and a dir. So both run in `spawn_blocking`
(decision CH36 G21). The first family read holds no lock of the rows.
caregiver removes the family files, then drops the family (decision
CH33 rule 3). So create reads the family a second time under the lock
of the rows, before the write. A drop of the family then sees the new
row, or that read sees no family, and no row outlives its family. A
create makes no session disk and boots no VM: the first boot makes the
disk (decision CH21-4).

`src/handlers/list.rs` serves `list` (decisions CH21-5, CH21-7). It
answers `Rows::page` from memory, on the runtime worker, so a list boots
no VM. The request type requires `family` (decision CH34 rule 4). A page
ends early under the 1 MiB cap with a `next` (decision CH33 rule 2).
`tests/create_list.rs` proves a repeat create, a conflict, an unknown
family, a repeat after the family went, `internal` for a failed family
read or row write, and a page of 4096-character cwds that ends early.

## The check route

`src/handlers/check.rs` serves `check` (decisions CH6-3, CH69 rules 1
and 2). It answers with `egress::rule`, the one decision that the proxy
of host port 1104 uses too. A check boots no VM.

1. `Target::parse` reads the target as `host:port` (decision CH6-4). An
   IPv4 or IPv6 address, bracketed or not, is `ip_literal`. So is each
   name that getaddrinfo reads as an IPv4 address with no lookup (the
   inet_aton forms, such as `2130706433` or `0x7f.1`). The host goes to
   lower case. A host that is no `HostName` (a trailing dot, `*`), no
   port, or a port that is not 1 to 65535 is `bad_target`.
2. The handler reads `FamilyRoot::read_egress` through
   `egress::read_list`, in `spawn_blocking` (decision CH36 G21). The
   proxy reads the list through the same function. An unknown family or an absent file is the empty
   list (decision CH69 rule 3).
3. `decide` refuses a host and port that the list does not hold with
   `not_listed`, before any lookup. Then tokio `lookup_host` resolves
   the name with the system resolver (decision CH69 rule 8).
4. `judge` maps each IPv4-mapped IPv6 record to IPv4. One record that is
   not public refuses the whole name with `not_public`. Not public: each
   IPv4 special-purpose range of RFC 6890, 100.64.0.0/10, 0.0.0.0/8,
   224.0.0.0/4 and 240.0.0.0/4, and each IPv6 address outside 2000::/3
   or in 2001:db8::/32 or 2001::/23. No record is `no_address`. Else the
   reply allows the target at the records, in the resolver's order, with
   no record twice.

An `egress.json` that is present and does not read gets the refusal
`not_valid` with status 200, and the vmm logs one line that names the
same reason (decision CH70 rule 2). `tests/egress_rule.rs` proves
each verdict of the rule: `localhost` resolves through `/etc/hosts`, and
`judge` takes the addresses that the test gives. For `no_address` it
lists a name under `.invalid` with a 64-letter label, so the resolver
may fail before a query, and else gets NXDOMAIN (RFC 6761). An IPv6
literal with a port, such as `::ffff:127.0.0.1:443`, is refused only by
its text before the last `:`, and a test holds that case.
`tests/egress_check.rs` proves the route: each target gets the verdict
of the rule, an absent file, a bad file (`not_valid` with 200, never
500), and the mismatch reply to a client of protocol 1.
`serve_lists_refuses_and_stops_on_the_signal` in
`src/compose/tests.rs` sends one `check` through the shipped handler
set.

## The stop

`src/stop.rs` stops one VM (decisions CH19 (3), CH34 rule 5). It sends
`stop` on the control link and waits `DROP_STOP_WAIT` (30 s) for Cloud
Hypervisor to exit. Then it sends SIGTERM through the `Launched` handle
of `creche-vmm-confine` and waits `DROP_SIGTERM_GRACE` (5 s). Then it
sends SIGKILL and waits `KILL_WAIT` (1 s, decision CH50 rule 4) at
most, so a stop ends within 36 s. A process that outlives it gives
`StopError::Hung`, and its files stay. The first two waits come from
`creche-wire`. A kill is a power cut
for the guest, and no error: `Stopped::Killed`. After the exit it runs
`remove_stale` of `creche-vmm-store` in `spawn_blocking`, which removes
`api.sock.lock` (fact 75) and the `vsock.sock` that a kill leaves. A stop
takes no snapshot, so the next start is a cold boot (decision CH19 (5)).
A step that did not go out skips its wait: no open control link, a
failed or stalled write of `stop`, or a failed SIGTERM (decision CH50
rule 3). The write of `stop` has a timeout counted inside
`DROP_STOP_WAIT`, so SIGTERM follows 30 s after the start at most.
`StopLink` and `VmProcess` let `tests/stop.rs` give fakes on paused time.

## The drop route

`src/handlers/drop.rs` serves `drop` (decisions CH21-2, CH21-10,
CH27). A session drop of an absent id, or of an id that another family
owns, gets `not_found` and stops nothing (decision CH33 rules 4 and 5).
A family drop takes the rows of the family, and a family with no rows
answers `Dropped`. The drop runs in a task of its own, and the handler
waits for that task. hyper drops the handler future when the client
hangs up, because `src/server/accept.rs` leaves half-close off. The task
runs on with its marks, so a retry joins the live slot and waits for the
same start. The drop runs in this order:

1. `Relays::mark_drop` marks each session. The mark stops its open
   relay (decision CH26 rule 5), and an Upgrade under the mark gets
   `not_found` (decision CH34 rule 6).
2. `DropMark::claim_ended` waits for the Upgrade that the mark stopped.
   Its start may still boot a VM, so the drop waits for that start to
   end. Then no Upgrade starts a VM of the session under the mark.
3. The `StopVm` hook stops every VM of the target at once (decision
   CH33 rule 11). `ColdBoot` is the hook of the vmm (see "The stop
   hook"). `tests/drop.rs` fakes it.
4. `remove_session` of `creche-vmm-store` removes the data of each
   session whose VM stopped, then its row (PR D14). It runs in
   `spawn_blocking`, one row at a time. The lock of the rows lasts one
   removal, so the other handlers get it between two removals (decision
   CH36 G21).
5. The marks drop after the removal, so they last until the row is gone.

A stop that fails gives `internal` 500 (decision CH50 rule 4). The row
and the files of that session stay, so a retried drop finishes the job.
`StopError::Hung` is one such stop. The drop logs the pid of each VM
that did not stop. A removal that fails gives `internal` too, and its
row stays. `tests/drop.rs` runs the handler on paused time with fake
VMs. It proves two drops that overlap a start, a retry after a drop
whose client hung up, a family drop with one hung VM, a failed removal,
and marks that last until the row is gone.

## The cold boot

`src/start.rs` holds `ColdBoot`, the `StartVm` hook of the vmm for a
session with no usable snapshot (plan PR L16, decisions CH1, CH8-2). It
keeps one table of VMs, one per session. A VM that runs gets a new dial
of guest port 1201 and no boot. A VM whose process exited leaves the
table, and the start boots it again. A cold boot runs these steps:

1. The family VM shape (`FamilyRoot::read_shape`) and the free bytes of
   the state root, in `spawn_blocking` (decision CH50 rule 9).
   Admission needs the guest RAM of the shape, so this read comes before
   admission.
2. `admission::decide` (decisions CH8-6, CH8-7, CH58 rule 2). The guest
   RAM of the start counts in the table from here on, so two starts never
   pass the cap together. The start also takes the lowest free slot, or
   with none free the slot of its first eviction (see "The slots"). `host_full` writes no fault. Then each eviction that
   admission names runs (see "The eviction").
3. In `spawn_blocking`: `RunRoot::link_vm` links `vm/<id>` to the slot
   (PR D16), then `remove_stale` (decision CH19 (4)), `planes::bind` of
   the listeners of host ports 1101 and 1102 (see "The plane bridges"),
   `family_state::bind` of the listener of host port 1100 (see "The
   family-state reply"), `mirror::bind` of the listener of host port
   1103 when `repos.json` of the family lists a repo or does not read
   (see "The git mirror"), `proxy::bind` of the listener of host port 1104
   when `egress.json` of the family lists a host or does not read (see
   "The egress proxy"). `watch::Reach::read` reads both files once for
   both binds. Then the store check of
   the kernel, the guest init initramfs and the root disk of the flavor
   (decision CH44 rule 2),
   `SessionDisks::for_boot` (decision CH21-4), `snapshots/` with mode
   0700, and a new `sessions/<id>/serial.log` in the state root with
   `SerialLog::fresh`, mode 0600 (gap G23, decision CH59 rule 2). Then the
   restore or cold boot choice (see "The restore"), and the launch with
   `ChCommand::boot` or `ChCommand::restore`, `VmLimits`, the Landlock
   files of the VM, the vmm unit and `Launch::in_slot` (decisions CH8-3,
   CH8-5, CH8-8, CH30, CH55, CH60 rule 1). A vmm with no unit fails the
   start before the link, with no file touched.
4. `Bound::serve` starts the plane bridges, the family-state reply, the
   mirror and the egress proxy.
   Then `control::open` for
   the first `ControlReady`. It keeps the facade
   state in `ControlState`, writes a turn end to the row, and clears the
   faults of the family (contract 05 §3.3.1 rule 8).
5. `deny::check` with the bound set 1100, 1101 and 1102 (plan PRs V6
   and V8), plus 1103 and 1104 when step 3 bound each (plan PRs M1 and
   E2, decisions CH49 rule 3, CH69 rule 13). `Reach::ports` gives the set.
6. `AcpLink::dial` of guest port 1201 (decision CH50 rule 2).
7. The VM joins the table, and the follow of its reach files starts
   from the reading of step 3 (see "The edit follow").

Each fault but `host_full` goes to the fault file of the family
(decisions CH6-3, CH21-1). A fault after the launch stops the VM with
`stop::stop`. After `ControlReady` the stop sends the graceful stop on
the control link first (decision CH19 (3)). Before it no link exists,
so the stop goes on to SIGTERM (decision CH50 rule 3). A VM whose stop
failed stays in the table, so admission counts its RAM (decision CH50
rule 4). A start that is dropped before its end sends SIGKILL to its VM
and keeps the VM in the table for the same reason. The launch step puts
the process in the start's reservation inside `spawn_blocking`, so a
start that hyper drops during the launch does the same. Admission first
removes each VM whose process exited, so an exited VM holds no RAM.
The `Launcher` trait
lets `tests/start.rs` fake the launch, the command line file and the
guest. Every other step there is the real one on a scratch dir.

## The plane bridges

`src/planes.rs` bridges the planes on host ports 1101 and 1102 (plan PR
V6, decisions CH6-2, CH14). Guest init splices guest 127.0.0.1:4000 and
:8300 to a vsock dial of 1101 and 1102, and CH connects each dial to
`vsock.sock_<port>` in the run dir (decision CH24). The vmm splices each
connection to a fresh TCP connect to `VMM_LITELLM_ADDR` or
`VMM_CHAPERONE_ADDR`, with no allowlist lookup and no rewrite (fact 46).

1. `planes::bind` binds both listeners after `link_vm` emptied the slot
   (decision CH56 rule 1) and after `remove_stale`, before CH starts. A
   cold boot and each restore run it, so a restored guest that dials
   again finds both (fact 45, decision CH62 rule 14).
2. `Bound::serve` gives each listener a task. Each connection gets a
   connect within `CONNECT_WAIT` (5 s). A connect that fails or takes
   longer closes the guest side, and nothing retries (decision CH62 rule
   14). Guest clients treat the close as transient (decision CH20).
3. Each listener holds at most `SPLICE_CAP` (64) open splices, with no
   idle timeout (decision CH62 rule 14). A connection past the cap
   closes at once (decision CH67 rule 3).
4. The copy is `relay::Splice`, the copy of the ACP relay, on the tokio
   `net` types. `copy_bidirectional` needs the tokio feature `io-util`,
   which adds a lock entry, so the vmm does not use it.
5. `Vm::listeners` holds the bridges, the family-state reply and the
   follow, which holds the mirror and the egress proxy (see "The edit
   follow"). The drop of the entry, when the VM leaves the table, ends
   each listener task and each open splice. A VM that the vmm killed
   drops them at the kill.
6. The deny check gets 1100, the ports of `Upstreams::rows`, 1103 for
   a family with a clone list and 1104 for a family with egress as its
   bound set, so a listener of any other port fails it (decisions CH6-3,
   CH49 rule 3, CH69 rule 13).
7. A full row holds 129 fds: its listener and two streams per splice.
   `planes::fd_need` is that for both rows of all `SLOTS` (64), plus a
   headroom of 4096: 20608. `vmm serve` raises its soft `RLIMIT_NOFILE`
   to it plus `family_state::fd_need`, `mirror::fd_need` and
   `proxy::fd_need` after its start checks and before it serves (step 4
   of "The compose step", see also "The family-state reply", "The git
   mirror" and "The egress proxy"), and a
   hard limit below the sum exits 78
   (`creche_vmm_confine::nofile`, decision CH67 rule 4, Known gap 23).
   So the busy guests of
   a few VMs cannot use every fd of the vmm.

`tests/planes.rs` runs the bridges on real Unix and TCP sockets in
process: each row reaches its own plane, the bytes pass unchanged, a
refused connect closes the guest side, the cap, and the drop.
`tests/start.rs` proves that both listeners take a dial at the launch of
a boot and of a restore, that a dial reaches the plane of the config,
that the listener closes with the VM, and the bound set of the deny
check.

## The family-state reply

`src/family_state.rs` serves the family-state reply on host port 1100
(plan PR V8, decisions CH5a, CH14, CH23). The facade pulls the state
before each prompt. Guest init splices each connection of
`/run/creche/family-state.sock` to a fresh vsock dial of 1100 (decision
CH62 rule 13), and CH connects each dial to `vsock.sock_1100` (decision
CH24).

1. `family_state::bind` binds the listener beside the plane listeners,
   with their lifecycle: after `link_vm` and `remove_stale`, before CH
   starts, for a cold boot and for each restore.
2. Each link reads pull lines of at most `FAMILY_STATE_CAP` bytes
   through `creche_wire::strict`, then as `FamilyStatePull` (decision
   CH65 rule 2). It reads them with the line reader of `ControlLink`,
   so the framing is the one of "The vsock JSON links" in
   `wire/AGENTS.md`. One link carries many pulls (decision CH43 rule 4).
3. Each pull runs `FamilyRoot::read_state` in `spawn_blocking`
   (decision CH50 rule 9) and answers with one `FamilyStateReply` line.
   A fault is its typed reply, and the link stays open (decision CH23
   rule 2). Nothing keeps a last good state (decision CH53 rule 2).
4. A `state` reply waits until the host time passes `model_live_at`, for
   at most `HOLD_CAP` (10 s, decision CH31 rule 1).
5. A refused line closes the link with no reply, and one stderr line
   names why, with no byte of the line (decision CH43 rule 2). An EOF
   at a line end closes it with no log, so after a restore the old link
   ends and the next pull dials fresh (fact 74).
6. A pull does not set `last_use` (decision CH62 rule 22). The server
   holds no handle on the VM table.
7. A good pull writes no log line (decision CH68 rule 3). A faulted
   pull writes one stderr line with the session, the reply kind, its time
   in microseconds and its hold in milliseconds. A refused pull writes
   one stderr line with the session and the reason. A guest that pulls
   in a loop cannot flood the journal with good pulls.
8. The listener holds at most `LINK_CAP` (64) open links (decision CH68
   rule 1). A link past it is accepted and closed at once.
9. `family_state::fd_need` counts per slot the listener, and per link its
   stream and the 6 fds that one `read_state` holds at most: 449 per
   slot, 28736 for all `SLOTS`. `vmm serve` adds it to
   `planes::fd_need`, so the raised soft limit is 49344 (decision CH68
   rule 2).

`tests/family_state.rs` runs the listener on real Unix sockets over a
scratch family root, with paused tokio time: many pulls on one link, a
reply at the cap, the hold and its cap, each typed error on an open
link, each refused line, the link cap and the drop. Its test
`a_bad_pull_logs_one_line_and_a_good_pull_none` runs its own binary as
a child and reads the real stderr: no line for a good pull, one for a
faulted pull and one for a refused line (rule 7). `tests/start.rs`
proves that the listener takes a dial at the launch of a boot and of a
restore, that 1100 is in the bound set of the deny check, and that a
pull does not set `last_use`. That pull gets the `state` of the family
of the session in the family root of the config, so the start hands the
server the right family and root.

## The egress proxy

`src/egress/proxy.rs` serves CONNECT on host port 1104 (plan PR E2,
decisions CH6-1, CH6-4, CH46 rules 1 and 3, CH49 rule 3, CH69 rules 7,
9, 10, 14, 20, 24 and 30, CH72). Guest init splices guest
127.0.0.1:3128 to a vsock dial of 1104 (decision CH46 rule 1), and CH
connects each dial to `vsock.sock_1104` (decision CH24). The vmm binds
it for a family whose `egress.json` lists a host (decision CH49 rule 3).
A present file that does not read binds it too, so each CONNECT gets
403 and a `not_valid` log line (decision CH72 rule 3). An absent file
or an empty list binds no listener. On a running VM the follow binds
and removes it after each edit (see "The edit follow").

1. `proxy::bind` binds the listener beside the plane listeners, with
   their lifecycle: after `link_vm` and `remove_stale`, before CH
   starts, for a cold boot and for each restore.
2. Each link reads one head: the bytes to the first CRLF CRLF, at most
   `HEAD_CAP` (8 KiB), within `HEAD_WAIT` (30 s). A head past the wait
   closes with no reply (decision CH69 rule 9).
3. Only `CONNECT <host>:<port> HTTP/1.1` or `HTTP/1.0` passes. Another
   method gets 405 with `Allow: CONNECT`. A head past the cap, an EOF
   before the head end, or a request line that does not read gets 400.
   The proxy reads no header line (decision CH69 rule 7).
4. Each CONNECT reads the list again with `egress::read_list` and calls
   `rule::decide`, the function of `check` (decisions CH6-4, CH69 rule
   2). A removal refuses the next CONNECT at once, and an open tunnel
   runs on (fact 43). `bad_target` gets 400, `no_address` 502, and
   `ip_literal`, `not_listed`, `not_public` and `not_valid` 403.
5. The proxy connects to the judged addresses in their order, within
   `CONNECT_WAIT` (5 s) in all (decisions CH6-4, CH62 rule 14). No
   address that takes the connect gets 502, the wait 504. Then `200
   Connection established`, and `relay::Splice` carries the bytes with
   the half-close of decision CH67 rule 9. Bytes that the guest sent
   after its head reach the target first. It reads no tunnel byte (fact
   42, decision CH46 rule 3) and has no idle timeout.
6. Each link writes one line to the log: a refusal at its refusal, a
   tunnel at its end, also when the drop of `Server` cuts it. The line
   goes out from the drop of the link, in `spawn_blocking`. At the end
   of the runtime it does not, as at a crash (Q61-10).
7. At most `LINK_CAP` (64) links. A link past the cap is accepted,
   logged with `link_cap` and closed at once (decisions CH67 rule 3,
   CH69 rule 14). The links that drain after `Server::close` count
   against the cap of `Draining::reopen` (decision CH74 rule 2). A
   failed accept is logged by kind, then the loop pauses `ACCEPT_PAUSE`
   (decisions CH40 rule 1, CH69 rule 30). CH67 rule 11 binds only the
   guest rows of guest init.
8. A link sets no `last_use` and blocks no snapshot (decision CH69 rule
   20), so a restore cuts each open tunnel (fact 45).
9. `proxy::fd_need` counts per slot the listener, and per link its
   stream and 4 more fds (the family dir and `egress.json` of the read, a
   resolver socket, the upstream socket): 321 per slot, 20544 for all
   `SLOTS`. With the 1103 need of "The git mirror", the raised soft
   limit is 102720 (decision CH69 rule 14).

`src/egress/log.rs` writes the log of `VMM_EGRESS_LOG` (decisions CH6-4,
CH69 rules 10 and 24). The compose step opens it at the start, for
append, with mode 0600, and a failed open exits 78. Each line has
`time`, `family`, `session`, `host`, `port`, `verdict` (`allowed` or
`refused`), `reason`, `address`, `bytes_up`, `bytes_down` and
`duration_ms` (decision CH72 rule 1). A refusal of the rule names the
reason that `check` gives (decision CH70 rule 2). The proxy reasons are `link_cap`,
`head_timeout`, `bad_head`, `not_connect`, `connect_failed` and
`connect_timeout` (decision CH72 rule 2). A write that would pass `LOG_CAP` (64
MiB) renames the file to `<path>.1` and opens a new one. When the open
fails after the rename, the lines go on to `<path>.1`, and each next
write tries only the open again, with no second rename. A log fault
writes one stderr line, the only stderr line of the proxy but an accept
fault (decision CH69 rule 24).

`tests/egress_proxy.rs` runs the proxy on real Unix sockets over a
scratch family root: an IP literal, `check` against CONNECT for one
table of targets with `no_address` among them, a list that does not
read, 405, 400, a head that two reads split, a head of exactly
`HEAD_CAP`, the head wait with paused tokio time, a removal, the fields
of each line, the rollover, a rollover whose open failed after its
rename, the link cap, `LINK_CAP` + 2 links one after another, the cap of a
reopen while links drain, the drop of `Server` (a cut link and its
line, then a refused dial) and the fd need. No test reaches the network.
The kvm job proves an allowed tunnel (plan PR E4, decision CH69 rule
22). `tests/start.rs` proves the bound set with and without a list, a
list that does not read, the bind at a restore, and an extra 1104
listener.

## The git mirror

`src/mirror.rs` serves `git://` clones on host port 1103 (plan PR M1,
decisions CH3, CH5f, CH10-10, CH46 rule 4, CH69 rules 13 to 20, 24 and
30). Guest init splices guest 127.0.0.1:9418 to a vsock dial of 1103
(decision CH46 rule 4), and CH connects each dial to `vsock.sock_1103`
(decision CH24). The vmm binds it only for a family whose `repos.json`
lists a repo (decision CH69 rule 13, which amends CH8-1, CH14 and
section 3.2.3 as CH49 rule 3 amended them for 1104), or is present and
does not read (decision CH73 rule 1, Known gap 29). An absent file or
an empty list binds no listener. On a running VM the follow binds and
removes it after each edit (see "The edit follow").

1. `mirror::bind` binds the listener beside the plane listeners, with
   their lifecycle: after `link_vm` and `remove_stale`, before CH
   starts, for a cold boot and for each restore.
2. Each link reads the first pkt-line within `LINE_WAIT` (10 s), at
   most `LINE_CAP` (1024) bytes with its 4 length digits. A line past
   the wait closes with no answer (decision CH69 rule 16). The read
   takes no byte past the line.
3. Only `git-upload-pack /<name>\0host=<any>\0` passes, with nothing
   more or with `\0version=2\0`, which sets `GIT_PROTOCOL=version=2`.
   `<name>` must be a `RepoName`: no `.git` suffix, no `~`, no `..`, no
   `/`. The host value is not read (decision CH69 rule 16).
4. Each link reads `repos.json` again (`FamilyRoot::read_repos`), so a
   removed repo refuses the next clone at once. The name must equal an
   entry byte for byte (decision CH46 rule 4), and
   `MirrorRoot::git_dir` must open `<name>` and `.git` as dirs with
   `O_NOFOLLOW` from the root handle (decision CH10-10). The path comes
   from the list entry, never from the client bytes.
5. Every refusal of steps 3 and 4, and a failed start of git, gets the
   one pkt-line `DENIED` (`ERR access denied`), then the close. So a
   family cannot tell a repo that is not listed from one that the mirror
   lacks (decision CH69 rule 16).
6. The vmm runs `<VMM_GIT_PROGRAM> upload-pack --strict --timeout=60
   <mirror>/<name>/.git` as david, with no extra confinement (decision
   CH69 rule 19), a cleared env that holds only `GIT_CONFIG_NOSYSTEM=1`,
   `GIT_CONFIG_GLOBAL=/dev/null` and the `GIT_PROTOCOL` of step 3
   (decision CH69 rule 17), and stderr on `/dev/null`. It advertises all
   refs (decision CH69 rule 18). `ChildIo` joins the stdout and the stdin
   of the child, and `relay::Splice` carries the bytes with the
   half-close of decision CH67 rule 9: a shutdown closes stdin. The link
   ends when both directions end, or once the stdout of the child ended
   and each of its bytes reached the guest, so a silent guest cannot hold
   an exited child. The child is the leader of a new process group. The
   end of the link kills that group with
   `creche_vmm_confine::group::kill`, so a `pack-objects` child of git
   ends too, and then reaps the child. The drop of a link that did not
   end kills the group, and tokio reaps the child (decision CH69 rule
   14).
7. A refused or faulted clone writes one stderr line that names the
   family, the session and the reason, at most `NOTE_CAP` (100) per
   `NOTE_WINDOW` (10 s) for one VM. A good clone writes none (decision
   CH69 rule 24).
8. At most `LINK_CAP` (64) links. A link past the cap is accepted and
   closed at once (decisions CH67 rule 3, CH69 rule 14). The links that
   drain after `Server::close` count against the cap of
   `Draining::reopen` (decision CH74 rule 2). A failed accept
   is logged by kind, then the loop pauses `ACCEPT_PAUSE` (decisions
   CH40 rule 1, CH69 rule 30).
9. A link sets no `last_use` and blocks no snapshot (decision CH69 rule
   20), so a restore cuts each open clone (fact 45).
10. `mirror::fd_need` counts per slot the listener, and per link its
    stream and 7 more fds (the spawn holds both ends of two pipes,
    `/dev/null` and both ends of the exec pipe of std): 513 per slot,
    32832 for all `SLOTS`. The raised soft limit is 102720 (decision
    CH69 rule 14, Known gap 28).

`tests/mirror.rs` runs the mirror on a real Unix socket over a scratch
family root and a scratch mirror of real repos. A forward in the test
carries `git clone git://127.0.0.1:<port>/<name>` to the socket, as guest
init bridges 9418. It proves the clone outside `repos`, the HEAD of a v0
clone with no v2 answer, a v2 clone, the exact argv, env and stderr of
`upload-pack`, the one error for a repo not listed and one not in
the mirror, a removal, each refused path form, `git-receive-pack`, a
symlinked repo, the line cap, the line wait with paused tokio time, the
link cap, `LINK_CAP` + 1 links one after another, the cap of a reopen
while links drain, the kill of the child and of its own child at the drop of
`Server`, the end and the reap of a link whose child exited, and the fd
need. Its test `a_bad_clone_logs_one_line_and_a_good_clone_none` runs
its own binary as a child and reads the real stderr: no line for a good
clone, and one line each for a repo not listed, a repo not in the
mirror, a missing mirror root and a link past the cap (rule 7). `tests/start.rs` proves the bound set with a clone list, with both
lists, with an absent or empty list, with a bad or zero-byte list, and
the bind at a restore. Its test
`a_clone_on_a_bad_clone_list_gets_denied_and_one_line` runs its own
binary as a child: a clone on a list that does not read gets `DENIED`
and one stderr line that names `not_valid` (decision CH73 rule 1).

## The edit follow

`src/egress/watch.rs` follows `egress.json` and `repos.json` of the
family on each running VM (plan PR E3, decisions CH6-3, CH49 rules 3
and 4, CH69 rules 11, 12, 13 and 28, CH72 rule 3, CH73 rule 1, CH74
rules 1 and 2). The start commits the VM with one `Watch`, a task that
starts from the reading of the launch, so an edit since the launch acts
at the first poll.

1. Every `POLL` (2 s) the task reads the bytes of both files with
   `FamilyRoot::read_repos_bytes` and `FamilyRoot::read_egress_bytes`,
   in `spawn_blocking` (decisions CH36 G21, CH69 rule 11). The task
   compares bytes (`Reach::same`). Bytes equal to the last ones change
   nothing and run no check. New bytes are a new reading, also when
   they read as the same list, or when they do not read before or
   after, so the deny check runs again (decision CH6-3). A read that
   takes no bytes (a symlink, a file that is not regular, a file past
   its cap, an error) has nothing to compare, so it is a new reading at
   each poll. The task reads the list of the bytes only to bind.
2. A new reading binds or removes the listeners of 1103 and 1104 by
   the rule of the start, `Reach::mirror` and `Reach::egress`: a list
   that holds an entry binds, and so does a present file that does not
   read. An absent file or an empty list binds none. A new listener
   gets the server of the start (`mirror::Bound::serve`,
   `proxy::Bound::serve`). A removed listener closes with
   `Server::close`, and then its socket file goes, so no new link
   starts.
   Each open tunnel or clone runs to its own end in `Draining` (decision
   CH74 rule 2, contract 01 section 3.7 rule 11, fact 43). A listener
   that binds again while links drain gets them with `Draining::reopen`.
   So a drained link counts against the `LINK_CAP` of the new listener,
   one VM holds at most `LINK_CAP` links per port, and the fd need holds.
3. Then `deny::check` runs with `Reach::ports` of the new reading, the
   check of the start (`Follow::check`, decisions CH6-3, CH49 rule 3).
4. A failed check, and a failed bind, serve or removal (decision CH74
   rule 1), ends the task. A task of its own then writes
   `egress_assert_failed` to the fault file and stops the VM of that
   control link, with the graceful stop first (decisions CH19 (3),
   CH21-1, CH54 rule 6, CH69 rule 12). The stop runs apart from the
   follow, because the stop drops the VM entry, and that drop ends the
   follow. The VM entry, and so the follow, lives on while a stop or a
   retire ends its CH, and a check in that window fails on the command
   line of a CH that exited. So the task writes the fault only when it
   takes the VM itself and its CH runs, or when the stop that it joined
   failed. A stop or a retire that ends the VM, and a CH that exited on
   its own, write no fault.
5. A removed host or repo does not wait for the poll: the proxy and the
   mirror read their file at each link (decisions CH6-4, CH69 rule 16).
   An added one waits one `POLL` at most.
6. The drop of the VM entry drops the `Watch`, which ends the task and
   drops both listeners with each link that drains, so no link outlives
   the VM. A restore binds from the files of that time and starts a new
   follow.
7. The reads and the check of the follow hold at most 2 fds of one VM
   at once, inside the headroom of `planes::fd_need`, so the fd need
   does not change.

The follow assumes that Cloud Hypervisor connects each guest dial to
`vsock.sock_<port>` at the dial (decision CH24). Then it finds a
listener that the follow bound after a boot or a restore. The CH49 rule
4 spike of decision CH69 rule 28 checks the assumption with two kvm
tests: `kvm_reach_binds_1104_after_an_egress_edit` and
`kvm_reach_binds_1104_after_a_restore_and_an_edit`. Both passed on
the kvm runner (gate run 42429466220), so the follow needs no fallback
of CH49 rule 4.

`tests/start.rs` runs the follow on paused tokio time, one `POLL` at a
time: an edit that binds 1104 and serves a 403, an emptied list that
removes 1104 and its file and a later edit that binds it again, an edit
that binds 1103 and serves `DENIED`, an emptied clone list that removes
1103, a failed check and a failed bind after an edit that each write
the fault and stop the VM, an open link on 1104 and an open clone on
1103 that each run to their own end after an emptied list, a refill
while a link drains, a draining link that ends with the VM, a follow
that ends with the VM, a failed check during a stop and after a
CH exit that write no fault, and an unchanged file that keeps the socket
file and runs no check. `tests/kvm_reach.rs` boots the vmm of
`examples/phase3_vm.rs` with the real guest init and the stand-in in
mode `reach`. A CONNECT to an IP literal from the guest gets no 403
before the edit and a 403 after it, after a boot and after a restore,
and no 403 again after an emptied list. These two tests need no
internet.

The phase 7 proof of plan PR E4 sits in the same file. Each of its
tests runs the real `vmm serve` under `systemd-run`, through
`common::boot_with`, so the fd limit of the user manager applies
(decision CH69 rule 31). It reads `check` over `vmm.sock`. The tests
prove an allowed tunnel to github.com:443 with bytes both ways (CH69
rule 22), the refusal of an IP literal, `check` that agrees with the
proxy, a removal that refuses the next CONNECT while an open tunnel
still carries bytes (fact 43), an emptied list that refuses each new
dial while an open tunnel still carries bytes (CH74 rule 2), a listed
repo that clones and a repo outside `repos` that fails. The github.com
tests need internet and DNS on the runner (plan risk 4).
The kvm_ test of the digests follow (plan PR V1) sits in the same file
and starts the same real `vmm serve`. Its `Served` sends each vmm log
line on a channel too, so the test reads the line of the swap.

## The slots

Plan PR L16b gives each VM one slot of the pool of
`creche_vmm_store::layout::SLOTS` (64, decisions CH51, CH55). Each entry
of the table holds its slot: a start, a VM that runs, a VM under
eviction or under a stop, and a VM whose stop failed. Admission takes
the lowest slot that no entry holds, under the table lock, so two starts
never take one slot. A full pool counts like a full RAM cap (decision
CH58 rule 2): with no free slot, admission evicts the least recently used
VM that `may_evict` passes (decision CH8-6), and the start takes the slot
of its first eviction. With no such VM, the start gets `host_full`
(decision CH21-3) and launches nothing. An evicted VM holds its slot as
`Evicting` until its CH ends. The start links that slot only after each
eviction ended, and a failed eviction fails the start, so no two live CH
share a slot. With a free slot, a start that evicts for RAM takes the
free slot.

The start runs `RunRoot::link_vm` before `remove_stale`, and passes the
slot to the launch with `Launch::in_slot` (PR C18). So the launch stage
enters the slot profile `creche-ch-slot-<k>`. A restore links the
`vm/<id>` of its snapshot to its new slot, and each path in the snapshot
holds. `link_vm` empties the slot (decision CH56 rule 1), so the start
takes only a slot that no live CH holds (caller duty (c) of PR D16).

An entry leaves the table only after its CH ended: a stop, a drop, the
stop after a guest fault, an eviction, an idle sleep, a shutdown, a start
that failed, or a VM that exited. Each of these runs `release`. It runs
`RunRoot::unlink_vm` under the table lock before the entry goes, so no
start of the session links before the unlink. It is one `unlink` on the
tmpfs of the run root, an accepted exception to the `spawn_blocking`
rule of CH36 G21 (decision CH58 rule 3). A failed unlink is logged,
and the slot still frees. `tests/start.rs` proves two slots, a reused
slot, a full pool of busy VMs, a full pool that evicts its idle VM, each
stop path and a restore into a new slot.

## The restore

`src/start.rs` restores a session from its newest snapshot when it can
(plan PR L17, decision CH2). `StartConfig::ch_version` is the
`CheckedCh` of the version check at vmm start (gap G10). With the
checked kernel, initramfs and root disk it gives the current
`SnapshotKey`. A snapshot with the current key names the initramfs of
the config, so `Start::Restore` carries that `store/<sha256>` file and
the ruleset reads it, as on a boot (decision CH61 rule 3). The config
always names one: the initramfs of this vmm start (CH62 rule 18). A
snapshot of an earlier vmm start keeps only while that start wrote the
same bytes.

1. `check_newest` of `creche-vmm-store` compares the key of the newest
   generation. A mismatch or an unreadable key drops every generation,
   and the VM cold-boots (decisions CH7-6, CH44 rule 5).
2. `claim` marks a keyed snapshot consumed before CH starts (decision
   CH19 (5)). A consumed mark, or a retired claim, gives a cold boot. A
   claim that cannot retire fails the start (decision CH44 rule 6).
3. The key check and the claim run under the lock of the claims, which
   the sweep holds too.
   The generation stays in the set from the key check until CH ends:
   the VM leaves the table, or its start ends with no process
   (decision CH50 rule 8). `ColdBoot::in_use` gives the set to the
   sweep.
4. `ChCommand::restore` runs with `resume=false`. Then
   `restore::ChApi::info` is read until CH answers, inside
   `READY_WAIT`. A reply that is not a `VmInfo` ends the wait at once.
   `restore::check_devices` reads the `VmInfo` of `creche-vmm-store`
   (PR D15). It requires `state` `Paused` and refuses `net`,
   `generic_vhost_user`, `fs`, `user_devices`, `vdpa`, `devices`, `pmem`
   and `tpm`, a disk with `vhost_user` or a `vhost_socket` (CH v54 opens
   the socket and ignores the path), any disk but the root disk and then
   the session disk, and a vsock socket other than the one of the VM. Then
   `ChApi::resume`, then the wait for `ControlReady` (decisions CH42
   rule 2, CH50 rule 10). `INFO_TIMEOUT` and `RESUME_TIMEOUT` (5 s each)
   bound each call (decision CH44 rule 3).
5. The deny check runs as for a boot. A restore has no `--vsock`
   argument.

`restore::ChTools`, the API of the vmm, reads the device set since PR
D15 (plan PR L18b). So every restore runs the device check, and the vmm
restores a snapshot that it finds. The claim consumes the snapshot, so
a restore that the check refuses never runs again, and the next start
cold-boots (decision CH50 rule 10). Each eviction and each sleep syncs
the guest before its take (caller duty 7, decision CH62 rule 23), so
the vmm restores a VM that it put to sleep. `tests/start.rs` gives one
fake API, and its `info` replies have the shape of CH v54.
`a_claim_always_runs_the_device_check` proves the refused restore and
the cold boot after it. Plan PR V9b removed the `ChApi` const that let
a test API skip the device read (decisions CH62 rule 23, CH58 rule 1).

## The eviction

When admission answers `EvictFirst`, `ColdBoot` takes each named VM out
of the table and marks its slot `Evicting`, so its RAM still counts
(plan PR L17, decision CH8-6). Then, one VM at a time in the order of
admission:

1. `Relays::close` stops the open door link of the VM and waits for its
   claim to end. The client reconnects with `session/load` (decision
   CH19 (6)).
2. A VM with a turn running goes back, and the start gets `host_full`.
3. `StopSender::sync` syncs the guest (item 5.7 of the design, decision
   CH62 rule 23). With no `synced` within `SYNC_WAIT` (10 s) the VM
   takes no snapshot.
4. `ChApi::snapshot` takes the snapshot with its key
   (`sleep::take_keyed`, PRs D9 and D10). `Snapshotter::take` writes the
   key before its rename, so no snapshot exists without a key (decision
   CH44, item 42). `sleep::EVICT_SNAPSHOT`
   bounds each `ch-remote` call of the take: pause 5 s, snapshot 60 s
   and `shutdown-vmm` 10 s (decisions CH44 rule 3, CH50 rule 1). With
   the sync, one retire waits at most 10 + 5 + 60 + 10 s for the guest
   and CH. The take ends CH. A failed take is logged, and the stop follows anyway
   (decision CH44 rule 4). With no take, the stop of step 5 is the
   graceful one (decision CH19 (3)).
5. `stop::stop` with the control link of the VM. A VM that does not
   stop goes back with no link, so admission counts its RAM (decision
   CH50 rule 4), and the start gets `host_full`. The VMs left in the
   list go back too.

Decision CH54 rule 6 binds a retire too, for an eviction and for an idle
sleep. A VM whose control link closed after its gate takes no snapshot.
A VM whose link closed on a refused line during the take or the stop
(`ControlState::refused`) gets its take consumed with `claim`, so the
next start cold-boots (decision CH19 (5)). Either VM stops and frees its
RAM, but an idle sleep does not count it as slept. `tests/start.rs`
proves a refused line during a take.

The list runs in a task of its own (`tokio::spawn`), and the start
waits for it. hyper drops the start future when the client hangs up
before 101. The task runs on, so no take or stop ends half way, and no
drop of an `Eviction` kills a VM during its snapshot. The retire takes
its read lock of the takes before the sync, so no sweep runs between
the sync and the take. The blocking step of a take owns that lock, so
the lock lasts as long as the take.

The gate of each VM is `Activity::may_evict` of `creche-vmm-store`, not
`may_sleep` (PR D12). Only a VM with a control link and a snapshot key
may be evicted. Its last use is the later of its start and its last
door close, and an open door counts as used now (decision CH50 rules 5
and 6). An Upgrade of a session under eviction gets `host_full`. So does
an Upgrade whose door an eviction closes after its start: `Relays::close`
stops the claim with `relay::Stopped::Evict`, and the acp handler answers
it with `host_full`. A drop stops it with `Stopped::Drop`, which gets
`not_found`. `tests/start.rs` proves the order, the door close,
`host_full`, a failed take, a failed stop, a start under eviction, the
sync before each take, and a guest with no `synced` that gets no
snapshot and the graceful stop.
`src/control.rs` proves the sync on paused time: the one `sync` line, a
late `synced` that changes nothing, and a closed link that ends the
wait at once. `tests/acp.rs` proves the eviction during
the dial.

## The sleep and the sweep

`sleep::run` times two passes until its stop future ends (plan PR L18).
Both run at once, then the idle check every `IDLE_CHECK` (30 s) and the
sweep every `SWEEP_EVERY` (1 h). A pass that runs ends before the stop
counts. A long pass delays the next tick, and missed ticks do not run
back to back. The sweep goes first when both are due, so long idle
passes never starve it. `sleep::Sleeper` lets `tests/sleep.rs` count
the passes on paused time.

1. `ColdBoot::sleep_idle` reads `Activity::may_sleep` of each VM with a
   control link and a key: the facade is up, no turn runs, no door link
   is open, and the VM is idle past `IDLE_TIMEOUT` (decisions CH2, CH19
   (6), CH36 G12). The gate and the take out run under the table lock.
   It takes out one VM, retires it in its own task as in step 2 to 4 of
   "The eviction", then reads the table again for the next. So a VM
   waits as `Evicting` only for its own retire, and a VM whose door
   opens meanwhile stays up. Each VM gets one try per pass. A sleep
   closes no door.
2. `ColdBoot::sweep` runs `retention::sweep` of `creche-vmm-store` with
   `StartConfig::budget`, the key `VMM_SNAPSHOT_BUDGET_GIB` (PR D13,
   decisions CH2, CH8-7, CH50 rule 8). It holds the
   lock of the claims, so no key check or claim runs, and it reads a
   copy of the `InUse` set. A VM that ends meanwhile only shrinks the
   set, so the copy names each generation in use, and no generation of
   a restored VM goes. The lock of `InUse` lasts one insert or remove,
   so no runtime worker waits behind the sweep (decision CH36 G21). The
   write lock of the takes keeps each take out of the sweep, as
   `retention::sweep` asks. The blocking step owns both locks.

The last use of a VM is the latest of its start, its last door close
and its last turn event (decision CH50 rule 5). `ControlState::turn`
keeps the time of each turn event. A VM is up only while the facade
state that guest init reported last is `running`, in a ready or in a
later `facade` line (decisions CH32 rule 6, CH62 rule 11). Admission
reads the same gates. `tests/start.rs` proves the sleep, a turn or an
open door that keeps a VM awake, a `starting` VM that a `running` line
opens, a door close through the acp handler as a use, a
sweep that skips a generation in use, an eviction that outlives its
dropped start, and an idle check that takes out one VM at a time.

## The stop hook

`ColdBoot` implements `StopVm` (plan PR L18b). A stop takes the VM out
of the table under a `Stopping` slot, so its RAM still counts (decision
CH50 rule 4). Then it calls `stop::stop` with the control link of the VM,
so the graceful stop goes first (decision CH19 (3)). A stop that worked
removes the VM from the table and its generation from `InUse`. A stop
that failed puts the VM back with no link, and `VmStopFailed` carries the
pid (`VmProcess::pid`). A second stop of the session waits for the first
and returns its outcome: the same `VmStopFailed`, whose error is shared
in an `Arc` (caller duty 6).

A slot that a start or a retire holds is read again every `SLOT_POLL`
(10 ms) until it ends. A launch step can outlive a dropped start future,
and its output then kills the VM and puts it in the table. A drop of the
stop future kills the VM and puts it back (`Eviction`), and a second stop
that waits then reads the slot again. A start of a session under a stop
gets `host_full` (decision CH54 rule 4). `tests/start.rs` proves a drop
through the hook, a failed stop that keeps the row and the RAM, two stops
that overlap, a stop during a launch, and the RAM of a VM under a stop. It
also proves a start of the session under a stop (`host_full`, one
launch) and a stop that waits for an idle sleep whose retire fails.

## The shutdown

`src/shutdown.rs` stops the vmm cleanly on SIGTERM (plan PR L19, gap
G16 option A). `shutdown::sigterm` registers the handler and gives the
future of the signal. `shutdown::shutdown` then runs these steps:

1. `Stop::stop`: the server accepts no further connection (decision
   CH26 rule 2). Then `VmSet::refuse_starts`: each start on an open
   connection gets `host_full`, so no VM joins the set (decision CH56
   rule 4). This covers a session with no VM too.
2. It waits for its `sleep` future, the end of `sleep::run`, so no idle
   retire runs beside a shutdown take (caller duty 5).
3. Each VM of the `VmSet`, all at once: `Relays::close` stops its door
   link and waits for its claim (decision CH19 (6)). Then
   `VmSet::take` retires the VM as an eviction does, when the gate of
   admission passes and the take gate passes (caller duty 7). Each other
   VM gets `StopVm::stop`, the stop of plan PR L14 through the `Stopper`
   (decision CH19 (3)). The take syncs the guest first, as a retire does
   (decision CH62 rule 23).
4. A start that passed admission before the refusal holds a slot, so
   the first read finds it. A session that a later read adds gets the
   next pass. Each session gets one pass, and no seen session gets a new
   VM.

`SHUTDOWN_WAIT` (85 s) bounds the whole shutdown from SIGTERM, 5 s
inside the `TimeoutStopSec` of 90 s (decision CH56 rule 3, gap G16). A
stop (36 s) and a take each fit inside it. A take syncs first, so it
costs 10 + 5 + 60 + 10 = 85 s, the whole wait (decision CH62 rule 23),
and the const assert of `src/shutdown.rs` pins that sum. One deadline bounds
every step, so a full take and then a full stop of one VM need not both
fit (decision CH56 rule 2). A VM still in the set at the deadline ends
after the vmm has exited: each VM scope has `BindsTo=` and `Before=` the
vmm unit (decision CH61 rule 2), and the `ExecStopPost=` step of the
unit kills its scope (decision CH61 rule 1). `ColdBoot` is the `VmSet` of the vmm.
`tests/shutdown.rs` runs the steps on paused time with a fake set.
`tests/start.rs` proves the take of an idle VM, the stop of a busy one
and the door close over the hook, the sync before the shutdown take,
and the graceful stop of a VM with no `synced`.

## The phase 3 proof

`tests/kvm_guest.rs` holds the phase 3 proof of section 5 of the design
(plan PR V10), `kvm_phase3_pulls_sleeps_restores_and_stops`. Only the
job kvm runs it. Its boot helpers and the env of each config key sit in
`tests/common/mod.rs` (plan PR E3), and the E3 tests of
`tests/kvm_reach.rs` boot the same vmm. `common::boot_with` takes the
start of the vmm, so the E4 tests start the real `vmm serve` in its
place. `common::boot` takes the mode of the stand-in and holds a lock,
so one vmm of the test binary runs at a time: each proof uses one
session, the run root of `RuntimeDirectory=vmm` and the state root of
the runner.

- `examples/phase3_vm.rs` plays the vmm (decision CH62 rule 29). It is
  an example in no release row (decision CH58 rule 1). It reads the vmm
  config with `Config::read` and composes the parts of the compose step
  with one `ColdBoot`. It runs the leftover sweep (decision CH19 (4))
  and the owner check of CH and ch-remote (decision CH67 rule 1), as
  the compose step does. Then `check_cloud_hypervisor` checks the
  version of `VMM_CH_PROGRAM`, the CH that the kvm job builds (decisions
  CH36 G19, CH75). It runs no digests follow (plan PR V1). It binds
  no `vmm.sock`. The test sends it one step per line: `start`,
  `facade`, `sleep` and `stop`.
- The test runs it as a transient user service with
  `RuntimeDirectory=vmm` and `NoNewPrivileges=yes`, so each VM scope
  binds to its unit (decisions CH55, CH60 rule 1). The env holds each
  key of "The config", with a root-owned `digests.json`, guest init and
  ch-remote (decision CH36 G20), a scratch family root with `shape.json` and a
  past `model_live_at` (decisions CH45 rule 2, CH47 rule 2), and the
  addresses of two stub planes on 127.0.0.1 in the test (decision CH62
  rule 28).
- The root disk holds the stand-in facade in mode `linked-relay` at
  `FACADE_PROGRAM` (decision CH62 rule 12) and an env file from
  `playpen/bin/creche-env-write` (decision CH7-7). The stand-in opens
  its local link, so it is `running` and may sleep (decision CH32 rule
  6).
- The steps: a cold boot with start 1, a `facade` line that says
  `running`, a `pull` on 1201 that gets `state` and a 200 from each stub
  plane, a sleep with a guest sync and one snapshot, a restore that
  claims it and whose ready says `running` with the same pid and start
  1, a second `pull` with the same answer, and a stop that leaves a
  clean session disk (decisions CH19 rules 1 to 3, CH62 rules 9, 13, 14
  and 23). The stop ends before `FACADE_STOP_WAIT`, the serial log
  holds the stop line of the stand-in, and guest init sent the facade
  no signal. The test dials 1201 itself, as
  `AcpLink::dial` does, because only the door relay of phase 6 carries
  the bytes of an `AcpLink`. It writes both pull times to the job log
  (decision CH23).

## Rules

- Each new dependency is its own pull request, with its lock delta
  (decision CH9-1).
- A library returns a typed error. Only `src/main.rs` turns an error into
  an exit status.
- A crate exposes `pub` items only for the crates that list it in their
  row (decision CH36, gap G24 of the phase 2 plan).

## Known gaps

1. Closed by decision CH54 rule 1: a drop waits for a start that its
   mark overlapped, with no bound (`src/handlers/drop.rs`). So a drop
   may outlast the 60 s wait of the client (decision CH34 rule 5). The
   drop runs in its own task, so a client that hangs up cannot cancel
   it, and the retry waits for the same start.
2. Closed by decision CH62 rule 11 (plan PR V1): guest init sends a
   `facade` line at each change of the facade state, and the sleep gate
   reads the state it reported last. The stand-in rule of decision CH43
   rule 8, `ControlState::turn_seen`, is gone.
3. Closed by decision CH54 rule 2: the deny check runs after
   `ControlReady` and before the dial of 1201 and the 101
   (`src/start.rs`). This departs from the order of plan PR L16, which
   put the check before the wait for `ControlReady`.
4. Closed by decision CH54 rule 4: an Upgrade of a session whose VM is
   being evicted, put to sleep or stopped gets `host_full`
   (`src/start.rs`). A door that an eviction closes between the end of
   the start and the 101 gets `host_full` the same way
   (`relay::Stopped::Evict`). Under the mark of a drop no such Upgrade
   reaches the hook (decision CH34 rule 6).
5. Closed by plan PR L18b: the two production pieces that no earlier
   plan PR named. `impl StopVm for ColdBoot` is "The stop hook", and the
   turn reader is in "The control link".
6. Closed by plan PR L18b: `ChTools::info` returns the `VmInfo` of PR
   D15, so every restore runs the device check and the vmm restores.
   Plan PR V9b removed the const that let an API skip it. A device kind
   that `VmConfig` does not name passes the read unseen
   (`creche-vmm-store` AGENTS.md). A new kind needs a field there and a
   line in `check_devices`.
7. Closed by decision CH54 rule 3: a restore whose VM is not `Paused`,
   or has a refused device, gives `egress_assert_failed`. A CH that does
   not answer `info` or `resume` gives `sandbox_start_failed`
   (`src/start.rs`). A reply that is not a `VmInfo` is a device set that
   the check cannot read, so it gives `egress_assert_failed` too.
8. Decision CH43 rule 6: a guest that closes the link before
   `ControlReady` asks for a cold boot. After a restore the start does
   not cold-boot then. It faults, stops the VM, and the next start
   cold-boots, because the claim consumed the snapshot.
9. Closed by plan PR V9 (decision CH62 rule 23): no take runs with no
   guest `sync`. `Retirer::retire` sends `sync` before each take and
   takes none with no `synced` within `SYNC_WAIT`. `ChApi::GUEST_SYNCS`
   is gone.
10. Closed by plan PR L18b: the turn reader calls `ControlState::turn`
    for each event, so the last turn time follows the guest (decision
    CH50 rule 5).
11. Closed by decision CH54 rule 5: the idle check runs every 30 s
    (`sleep::IDLE_CHECK`). So a VM sleeps between 5 min and 5 min 30 s
    after its last use.
12. Closed by decision CH54 rule 5: the sweep runs at the start of
    `sleep::run`, then each hour (`sleep::SWEEP_EVERY`).
13. Closed by decision CH57 rules 3 and 4 (plan PR L7): a family dir or
    `config` that is a symlink or a file gets `unknown_family`, and a
    repeat create whose family `config/` is gone gets `unknown_family`.
14. Closed by plan PR L20: the config and the compose step are one PR.
    `src/compose.rs` reads each field of `Config`, so each `VMM_*` key
    of `default.env` lands with its first reader.
15. Closed by decisions CH57 rule 1 and CH58 rules 4 and 5: the key
    `VMM_IMAGE_ROOT`, its root-owned `digests.json` with `kernel` plus
    one entry for each flavor, and a failed import that refuses the
    start ("The image root"). Caller duty 9 gives the start time to plan
    PR C13.
16. Closed by decision CH57 rule 2: the config refuses 0 GiB for
    `VMM_RAM_CAP_GIB` and `VMM_SNAPSHOT_BUDGET_GIB`, and takes 0 for
    `VMM_FREE_FLOOR_GIB`.
17. Closed by a read-only check on Hyperion, as david with no sudo
    (CH36 G31), on 2026-11-04: `/usr/sbin/mke2fs` (e2fsprogs) and
    `/usr/bin/fallocate` (util-linux) are executable on Ubuntu 24.04.5.
    The G9 check confirmed `/usr/bin/zstd`, and this check saw it again.
18. Closed by decision CH67 rule 1: `VMM_GUEST_INIT_PROGRAM`,
    `VMM_CH_PROGRAM` and `VMM_CH_REMOTE_PROGRAM` each must be a regular
    file, not a symlink, owned by root, with no group or other write
    bit, as `digests.json`. `src/root_owned.rs` holds that one check
    ("The root-owned files"). So the vmm row must install each file as
    a regular file, not a symlink.
19. Closed by decision CH67 rule 2: a plane key takes an IP address and
    a port, as `std::net::SocketAddr` parses them, and a host name exits
    78.
20. Closed by decision CH67 rule 3: `planes::bridge` accepts a
    connection past the 64 splice cap of a bridge row and closes it at
    once. So the guest client reads an EOF, a transient fault of
    decision CH20, and retries.
21. Closed by decision CH67 rule 4: `vmm serve` raises its soft
    `RLIMIT_NOFILE` to `planes::fd_need` (20608: the bridge need plus a
    headroom of 4096) and keeps the hard limit. A hard limit below the
    need exits 78. `systemd/creche-vmm.service` gets no `LimitNOFILE=`
    line, so systemd's default hard limit (524288) holds. Each CH
    inherits the raised soft limit.
22. Closed by decision CH68 rule 1: the 1100 listener takes at most 64
    links per VM (`family_state::LINK_CAP`), and a link past the cap is
    accepted and closed at once, as decision CH67 rule 3 does for a
    splice.
23. Closed by decision CH68 rule 2: the 1100 fd need (28736: per slot the
    listener, and per link its stream and the 6 fds of one `read_state`)
    adds to the raise of decision CH67 rule 4 (20608), so the raise is
    49344.
24. Closed by decision CH72 rule 1: each key of a log line is the field
    name of decision CH69 rule 10, `time` is RFC 3339 UTC to the second
    (`creche_vmm_confine::rfc3339`), `duration_ms` is whole ms, `address`
    is the `IpAddr` text, and a field that the link never reached is
    `null` (`src/egress/log.rs`).
25. Closed by decision CH72 rule 2: the log reasons of the proxy refusals
    that are not `check` reasons are `link_cap`, `head_timeout`,
    `bad_head`, `not_connect`, `connect_failed` and `connect_timeout`
    (`log::ProxyReason`).
26. Closed by decision CH69 rule 14: the 1104 listener takes at most 64
    links per VM, and its fd need (20544) adds to the raise of decision
    CH68 rule 2, so the raise is 69888. The plan estimate (16448)
    counted one fd for the allowlist read, which holds two.
27. Closed by decision CH72 rule 3: the vmm binds 1104 for a family whose
    `egress.json` is present, also when it does not read, so each CONNECT
    is refused 403 with a `not_valid` log line (decision CH70 rule 2). An
    absent file or an empty list binds no 1104 (decision CH49 rule 3).
    The start writes no stderr line (`Reach::egress` in
    `src/egress/watch.rs`).
    A zero-byte `egress.json` is present and does not read, so it binds
    1104 and each CONNECT gets `not_valid` (CH72 rule 3).
28. Closed by decision CH69 rule 14: the 1103 listener takes at most 64
    links per VM, and its fd need (32832) adds to the raise, so the raise
    is 102720. The plan estimate (20544) counted 5 fds per link. The
    spawn of `upload-pack` holds 7 at once.
29. Closed by decision CH73 rule 1: the vmm binds 1103 for a family whose
    `repos.json` is present, also when it does not read, so each clone is
    refused with `DENIED` and one stderr line that names `not_valid`
    (decision CH69 rule 24). An absent file or an empty list binds no
    1103 (decision CH69 rule 13). A zero-byte `repos.json` is present and
    does not read, so it binds 1103 (`Reach::mirror` in
    `src/egress/watch.rs`).
    Decision CH73 rule 2 sets `REPOS_CAP` of `creche-vmm-confine` to 4
    KiB.
30. Closed by decision CH74 rule 1: a failed bind or serve of a
    listener that a live edit adds, or a failed removal of its socket
    file, counts as a failed deny check, so the VM faults
    `egress_assert_failed` and stops (`Follow::apply` in
    `src/egress/watch.rs`).
31. Closed by decision CH74 rule 2: an edit that empties `egress.json`
    or `repos.json` closes only the listener and removes its socket
    file. Each open tunnel or clone runs to its own end in `Draining`,
    and a refill reopens with the same links (`Server::close`,
    `Draining::reopen`).
32. Closed by decision CH77 rule 1: `systemd/creche-vmm.service` reads
    one `EnvironmentFile=`, `vmm.env`, and no site file. The hooks take
    one `--env-file`, the same `vmm.env` (contract 06 §4 rule 7). So a
    hook checks the values that the unit runs. `bin/fleet-up up` writes
    each key of `vmm.env` as `creche-env get` resolves it against the
    site file, so a site edit reaches the vmm only after `fleet-up up`.
    `bin/tests/test_vmm_unit.py` pins the one line.
33. Closed by decision CH77 rule 2: the digests follow imports new
    bytes of `digests.json` only after they stay the same for
    `follow::HOOK_TIMEOUT`, the 300 s of the disks verify hook (gap
    Q68-6), plus one 5 s poll. The extra poll covers the start of the
    hook after the rename and a restore after a timeout, which the wait
    assumes take 5 s at most. So the follow never decompresses a disk
    while `vmm check-images` does, and a release that its hook rolls
    back never reaches a VM. A disks release reaches new starts about
    5 min after its install. Decision CH77 rule 3 sets the retry of a failed import
    every 12 polls (1 min), with one log line per distinct fault.

## Caller duties

1. Decision CH50 rule 1: the start of an `acp` Upgrade has no outer
   bound, and the handler adds none around the `StartVm` hook
   (`src/handlers/acp.rs`). Each step keeps its own bound.
   `control::SYNC_WAIT` bounds the guest sync of one eviction, and
   `sleep::EVICT_SNAPSHOT` bounds its snapshot (decision CH62 rule 23
   adds the 10 s of the sync). The bound of a start grows with the number of VMs that its admission
   evicts.
2. Decision CH50 rule 2: the `StartVm` hook of plan PR L16 dials guest
   port 1201 with `AcpLink::dial` before it returns. It answers a failed
   dial with `StartFault::SandboxStartFailed` and writes that fault to
   the fault file (decision CH21-1). The handler writes no fault. The
   rule holds only if guest init accepts 1201 in every facade state.
   Phase 3 confirms that. If guest init refuses 1201 until the facade
   runs, 101 waits for the facade, against decision CH34 rule 7, and
   David decides again.
3. Decision CH50 rule 4: the drop route maps `StopError::Hung` to
   `internal` 500, keeps the row and logs the pid
   (`src/handlers/drop.rs`). The `StopVm` hook of the vmm keeps a VM
   whose stop failed in its table, so admission keeps counting the RAM
   of that VM.
4. Held by `src/compose.rs`: the compose step of plan PR L20 runs
   `check_cloud_hypervisor` at vmm start and passes its `CheckedCh` as
   `StartConfig::ch_version`. A failed check refuses to start the vmm
   (gap G10 option A).
5. Held by `src/compose.rs`: the compose step of plan PR L20 runs
   `sleep::run` with the `ColdBoot` of the vmm on the runtime, and plan
   PR L19 ends its stop future before the shutdown snapshots. L20 fires
   that stop at SIGTERM and passes the end of `sleep::run` to
   `shutdown::shutdown` as its `sleep` future. `sleep::run` is the only caller of `ColdBoot::sweep` and
   `ColdBoot::sleep_idle` in the vmm.
6. Two drops of one session can overlap. The `StopVm` hook of the vmm
   makes a stop that finds a stop of the same VM running wait for it
   and return its outcome. So no drop removes the files of a VM that
   still runs.
7. Held by `src/start.rs` (plan PR V9, decision CH62 rule 23): item
   5.7 of the design and its item 42 note ask lifecycle to sync the
   guest through the control link before each snapshot, before
   `Snapshotter::take`. `Retirer::retire` sends `sync` on the control
   link before each take and waits `SYNC_WAIT` (10 s) for `synced`. No
   `synced` in time means no snapshot, and the graceful stop runs. The
   shutdown snapshots of plan PR L19 take through `Retirer::retire`, so
   they sync the same way. A new take path must sync first too: a take
   with no sync leaves the session disk as a power cut leaves it, for
   each cold boot after it (a key mismatch, a failed restore, an `Aged`
   or a `Budget` sweep).
8. Held by `src/compose.rs` and `src/main.rs`: the compose step of plan
   PR L20 calls `shutdown::sigterm` at vmm start, inside the runtime,
   and runs `shutdown::shutdown` when the signal fires. A take runs in
   `spawn_blocking`, and the drop of a runtime waits for each blocking
   task. So L20 ends the runtime with `shutdown_timeout(RUNTIME_END)`,
   and the vmm exits within the 5 s that `SHUTDOWN_WAIT` leaves.
9. Decision CH58 rule 5: the compose step imports each entry of
   `digests.json` before the bind of `vmm.sock`. The first import of a
   release decompresses each disk (`zstd -d`) and hashes it. So the unit
   of plan PR C13 must not bound the start of the vmm below that time.
   A later start finds each digest in the store and reads no byte.
10. Decision CH59 rule 1: the vmm reads `VMM_EGRESS_LOG` (plan PR E2),
    `VMM_MIRROR_ROOT` and `VMM_GIT_PROGRAM` (plan PR M1), so the
    `vmm.env` that `bin/fleet-up up` writes must hold them before this
    vmm starts on a host, or the start exits 78. Root must own the file
    of `VMM_GIT_PROGRAM` (`/usr/bin/git` of Ubuntu). The hard
    `RLIMIT_NOFILE` of the user unit must hold 102720 (decision CH67
    rule 4, plan risk 3).
11. Closed by plan PR E3 (decision CH69 rules 11 and 13): plan PR M1
    binds 1103 at each boot and each restore from the clone list of that
    time, and the follow binds or removes it after each edit on a
    running VM (see "The edit follow").
12. Closed by plan PR E3 (decision CH49 rules 3 and 4): plan PR E2
    binds 1104 at each boot and each restore from the list of that time,
    and the follow binds or removes it after each edit on a running VM.
    Each CONNECT reads the list again, so a removal refuses at once.
13. Closed by plan PR H4b (decision CH77 rule 2): `disks/component.toml`
    sets the verify `timeout_s` of the disks row to 300 s. It must equal
    `follow::HOOK_TIMEOUT`, or a hook may still run when the follow
    imports. `bin/tests/test_disks_manifest.py` holds the two equal.
