# ci-runner-peer

The peer check for a service on the attach interface of ci-runner
(`docs/design.md`, sections 6.2, 6.3 and 6.4). The daemon of the runner
connects to the service once per session, and passes each guest
connection to it as an fd. This crate checks both ends before the service
reads a byte.

The crate names no consumer. It depends on no crate of the runner but
`ci-runner-sys`, which holds the two system calls with no safe wrapper.
The probe of the host proof, `ci-runner-attach-probe`, is a service of
this kind in a crate of its own (`crates/ci-runner-attach-probe`), so a
service that depends on this crate builds nothing of it. Do not copy one
part of the probe: it keeps each passed fd after the `close` of its
session, on purpose, until its peer exits, so the proof can check the
kept fd once more. A service ends each fd at the end of its session.

## Depend on it

Pin one commit of `main` of the runner repository:

```toml
[target.'cfg(target_os = "linux")'.dependencies]
ci-runner-peer = { git = "https://<forgejo-host>/infra/ci-runner.git", rev = "<commit>" }
```

The crate builds on Linux for x86_64 and aarch64. On another OS it holds
only the pure parts: `SlotUnit::parse`, the names and the refusal types.

The crate pins `rustix =1.1.5` and `libc =0.2.191` exactly, as each crate
of the runner does. The dependency graph of the service must accept
these versions. A newer version needs a newer `rev` of the runner.

## What to call when

1. On each control connection, before the service reads the `open` line,
   call `check_daemon(fd)`. It requires that the peer runs as the user
   `ci-runner`, the user of the daemon.
2. Keep the `slot` of the `open` line with the session. It is the only
   source of the slot. Never take the slot from the peer. Key the
   session by its control connection, not by the `session` of the line:
   a job that the daemon offers again after a failed start repeats its
   session (design section 6.4, item 8).
3. On each fd of a `conn` line, before the service reads a byte, call
   `check(fd, Slot::new(slot))`. Close the fd at a `Refusal`.
4. Pin the session to one unit instance. Keep the `cgroup_id()` of the
   first accepted fd of a session. Refuse a later fd of that session with
   another id. Each guest connection of one run comes from the same unit.
5. Keep the `pidfd()` of the `SlotPeer` while the service serves the fd.
   Poll it for `POLLIN`. At `POLLIN` the peer has exited: end the session.

`check` holds three steps, in this order. A `Refusal` names the step that
failed:

| Step | What it does | Refusal |
|---|---|---|
| 1 | `SO_PEERPIDFD` gives a pidfd of the process that connected the socket. | `Refusal::Pidfd` |
| 2 | `PIDFD_GET_INFO` gives the cgroup id of that process. The check requires that `/sys/fs/cgroup` is a cgroup v2 file system that shows the root of the hierarchy. It opens `ci.slice/ci-runner.slice/ci-runner-vms.slice` from there one part at a time, and requires that each part and each sub-folder lies on that same mount. It lists the slice folder and requires that the one sub-folder with that inode is named `ci-runner-slot<slot>@<instance>.service`. Any instance with no `/` is accepted. | `Refusal::Cgroup` |
| 3 | A poll of the pidfd with no wait must show no event. | `Refusal::Live` |

A peer that has exited can fail step 2 too: `PIDFD_GET_INFO` gives
`ESRCH` once its process is reaped, and its unit folder can be gone. So
when step 2 fails, the check polls the pidfd as well, and an exit gives
`Refusal::Live(LiveFault::Exited)`. An exited peer that passes step 1
gives that refusal, unless the poll itself fails: then the
`Refusal::Cgroup` of step 2 stays. A kernel 7.0 gave `ESRCH` for a kept fd of a CH that had
exited, in the host proof in a rehearsal VM on 2026-11-21.

The check reads no file of `/proc`.

`check_daemon` reads the uid of `ci-runner` from `/etc/passwd`, reads
the uid of the peer with `SO_PEERCRED`, and requires that the two are
equal. A `DaemonRefusal` names the step that failed.

## The unit of the service

Design section 6.4 holds the shape of the units of a service. The
socket unit listens on the `socket` of the row with
`SocketGroup=ci-runner`, `SocketMode=0660` and `RemoveOnStop=yes`.
Only the daemon runs as `ci-runner`, and no slot unit has that group, so
only the daemon can connect. `SocketUser=` is optional.

The check was measured in a service unit with these settings (design
section 6.3, measurement M2):

1. `DynamicUser=yes`, `ProtectProc=invisible` and `ProcSubset=pid` work.
   Under them `/proc/<pid>` of the peer is not visible. The check does
   not read it.
2. `ProtectControlGroups=no` works: the proof in a VM ran the check
   under it. Under `ProtectControlGroups=yes` the slice folder stays
   readable (M2(c)), but the check did not run under `yes`. Not
   measured.
3. `ProtectControlGroups=strict` or `private`, and any other cgroup
   namespace of the service, make each check refuse with
   `CgroupFault::NotCgroupRoot`.
4. A file system that the service mounts over `/sys/fs/cgroup` or over
   a part of the slice path makes each check refuse. A tmpfs there
   (`TemporaryFileSystem=`) gives `CgroupFault::NotCgroup2`, and a bind
   mount on the slice path or on a sub-folder gives
   `CgroupFault::Mounted`.

Not measured: `PrivateUsers=` and `PrivatePIDs=`. A user namespace maps
the uid of the peer, so `check_daemon` most likely refuses under
`PrivateUsers=yes`. Keep both off.

The kernel must offer `SO_PEERPIDFD` (Linux 6.5) and `PIDFD_GET_INFO`
with `PIDFD_INFO_CGROUPID` (Linux 6.13). An older kernel makes each
check refuse. The cgroup v2 file system must be mounted at
`/sys/fs/cgroup`, as systemd does.

## What an accepted check proves

1. `check_daemon`: the peer of the control connection runs as the user
   `ci-runner`. Only the daemon runs as that user, so each control line
   comes from the daemon.
2. `check`: at the moment of the check, the process that connected the
   peer end of the fd is a live process in the unit `ci-runner-slot<slot>@<instance>.service` of the
   slice of the job VMs. Only systemd as root puts a process into a
   cgroup under that path. The check reads that path from the root of
   the cgroup v2 hierarchy, on one mount, so a cgroup namespace or a
   mount of the service cannot show it another tree. So the peer is not
   another slot, not the daemon, not a user unit under `/user.slice/`
   and not another service.
3. In a slot unit that the runner started, that process is the CH of that
   VM, or an escape out of that same VM.

## What it does not prove

1. The program of the peer. The check proves the unit, not the program.
   Root can start an instance of a slot unit with another program, and
   that instance passes the check for its slot (measurement M2(e)).
2. Which job runs in the slot. That rests on the daemon and on the CI
   system. The service checks the job itself, with its own credential.
3. Anything after the moment of the check. The peer can exit at once
   after it. Only the poll of step 5 above shows that.
4. The bytes on the fd. They come from the guest. Parse them as hostile
   input.
5. Root on the host.
6. Which process holds the other end of the connection now. The check
   names the process that connected the socket. If that process passes
   its end on, for example by SCM_RIGHTS, the check still accepts while
   that process lives, and the bytes come from the new holder. The
   sandbox of the slot unit limits where CH can pass an fd. The check
   does not.

## Tests

The pure parts have unit tests on each OS: the parse of a slot unit path,
the check of the cgroup root, the choice of the unit folder, and the
parse of `/etc/passwd`. The tests
that need the kernel run on Linux only (`src/check.rs`). Each prints one
line. Where the kernel has no `SO_PEERPIDFD` or no `PIDFD_GET_INFO`, the
line says `not run`. An accepted check needs a real slot unit, so only
the proof in a VM shows it. That proof passed on 2026-11-20 (design
section 6.3).
