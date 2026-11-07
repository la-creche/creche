# guest-init

The crate `creche-guest-init`: a library and the program `guest-init`,
PID 1 of each Cloud Hypervisor VM (decisions CH9, CH7-5). It is the fourth
program of the vmm row. Only the root `AGENTS.md` applies here.

## Layer rule

`creche-guest-init` calls `creche-wire` only. It depends on no vmm crate,
and no vmm crate depends on it. It runs blocking std threads, not tokio
(CH62 rule 2).

## The program

- Off Linux, and on Linux in a process that is not PID 1, `run()` starts
  nothing and returns exit status 78 (CH38).
- As PID 1, `run()` runs the boot plan of `src/boot.rs` (CH62 rule 4):
  devtmpfs, devpts and `/dev/shm`, the root disk `/dev/vda` ext4 ro and
  nodev, the session disk `/dev/vdb` ext4 rw, nosuid and nodev at
  `creche_wire::guest::SESSION_DISK_MOUNT`, tmpfs on `/tmp`, `/run`,
  `/root`, `/var/cache` and `/var/tmp`, proc and sysfs. Then `/dev` moves
  into the new root, the new root moves onto `/` (MS_MOVE) and guest init
  chroots into it. It stays PID 1. Last, `src/lo.rs` brings `lo` up with
  one RTM_NEWLINK message, and `run()` writes `guest-init: up` and waits.
- Each step writes one `guest-init: <step>` line to the console (CH62
  rule 26). A fault writes `guest-init: <step> failed: <error>`.
- A boot fault and a panic both end in `power::off`: sync, then
  `reboot(RB_POWER_OFF)` (CH19 rule 3). The guest kernel has
  `PANIC_TIMEOUT=0`, so an exit of PID 1 would hang the VM (fact 53). No
  fsck runs (CH62 rule 4).
- `examples/standin_facade.rs` is a stub that calls `run()`.

## Tests

- The unit tests check the boot plan and the netlink bytes on any OS.
- `tests/kvm_guest_init.rs` boots CH with `$CRECHE_KVM_DIR/guest-init.cpio`
  of `bin/vm-smoke-setup.sh`, the cmdline `console=ttyS0 rdinit=/init`
  (CH62 rule 19) and ext4 disks that `tests/common/mod.rs` builds with
  `mke2fs -d` (CH62 rule 12). Only the `kvm` job runs them.

## Known gaps

None.
