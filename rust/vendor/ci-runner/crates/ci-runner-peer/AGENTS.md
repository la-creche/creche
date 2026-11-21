# crates/ci-runner-peer

The published peer check of design section 6.3. A service outside this
repository depends on it by a git `rev`. `README.md` is the guide for a
service author. The root `AGENTS.md` applies here too.

## Layout

| File | What it holds |
|---|---|
| `src/lib.rs` | The public items, `Refusal` and `LiveFault`, and the arch gate. |
| `src/unit.rs` | Pure: `Slot`, `SlotUnit::parse` of a cgroup path, `PathFault`, `CgroupId`, `CgroupFault`, the names `SLICE_PATH` and `UNIT_PREFIX`, `cgroup_root`, which requires the root of a cgroup v2 file system, and `unit_of`, which picks the folder of a cgroup id among the sub-folders of the slice. |
| `src/daemon.rs` | Pure: `DAEMON_USER`, the parse of `/etc/passwd`, `UserFault` and `DaemonRefusal`. |
| `src/check.rs` | Linux only: `check`, `check_daemon`, `SlotPeer`, the check of the cgroup mount, the walk to the slice folder on that mount, its list, and the poll of the pidfd. |

## Rules

1. The crate depends on no crate of the runner but `ci-runner-sys`. No
   crate of the runner may be added: a service would then build the
   runner. The crate names no consumer. It holds no program either:
   Cargo builds each dependency of a package for a service, also one
   that only a program needs. So the probe of the host proof lives in
   `crates/ci-runner-attach-probe`, which depends on this crate.
2. `ci-runner-sys` holds each call with no safe wrapper. It holds
   `struct pidfd_info` and `PIDFD_GET_INFO` itself, with their header,
   and takes `SO_PEERPIDFD`, `PIDFD_INFO_CGROUPID` and
   `CGROUP2_SUPER_MAGIC` from `libc`. This crate keeps
   `unsafe_code = "forbid"`.
3. The check reads no file of `/proc`. A service under
   `ProtectProc=invisible` cannot see it (measurement M2(a)).
4. Step 3, the poll, comes after the cgroup read. `PIDFD_GET_INFO` can
   give the cgroup id after the exit of the peer (M2(f)), so only the
   poll shows the exit. A failed step 2 polls the pidfd too, because an
   exited peer can fail it (`ESRCH` once the process is reaped, or a gone
   unit folder). An exit of a peer that passed step 1 gives
   `Live(Exited)`, unless the poll itself fails.
5. The slot comes from the caller, never from the peer.
6. `SLICE_PATH`, `UNIT_PREFIX` and `DAEMON_USER` repeat names of the
   units of the runner. `crates/ci-runner/tests/units.rs` holds them
   equal to the rendered units.

## Tests

- `src/unit.rs` and `src/daemon.rs` test each pure part on each OS.
- `src/check.rs` holds the tests that need the kernel. Each prints one
  line. Where the kernel has no `SO_PEERPIDFD` or no `PIDFD_GET_INFO`
  with `PIDFD_INFO_CGROUPID`, the line holds `not run`, and the CI step
  "Kernel tests ran" fails. The tests check a socketpair peer in the
  cgroup of the test (refused in step 2), a socket with no peer (refused
  in step 1), the poll of a child of the test binary before and after
  its exit, the check of its kept fd after the exit (`Live(Exited)`),
  the cgroup check of the temp folder (refused as no cgroup v2) and of
  `/sys/fs/cgroup`, and `check_daemon` on a socketpair (refused).
- An accepted check, and each refusal of a real peer of the runner host,
  need a slot unit. The proof in a VM shows them (design section 15,
  item 8).
