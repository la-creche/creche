//! The system calls of `check` and `check_daemon`. Linux only.

use std::ffi::{CStr, OsString};
use std::fs;
use std::io;
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::unix::ffi::OsStringExt;

use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fs::{
    AtFlags, Dir, FileType, Mode, OFlags, Statx, StatxFlags, fstatfs, open, openat, statx,
};

use crate::daemon::{DaemonRefusal, PASSWD, UserFault, daemon_uid};
use crate::unit::{
    CgroupFault, CgroupId, SLICE_PATH, SliceEntry, Slot, SlotUnit, cgroup_root, unit_of,
};
use crate::{LiveFault, Refusal};

/// The mount point of the cgroup v2 file system.
const CGROUP_ROOT: &str = "/sys/fs/cgroup";

/// A peer that `check` accepted: a process in a unit of the slot of the
/// session, alive at the moment of the check.
#[derive(Debug)]
pub struct SlotPeer {
    pidfd: OwnedFd,
    cgroup_id: CgroupId,
    unit: SlotUnit,
}

impl SlotPeer {
    /// The cgroup id of the unit of the peer. Each guest connection of one
    /// run comes from the same unit, so a service compares the id of each
    /// later fd of a session with the id of its first one.
    #[must_use]
    pub const fn cgroup_id(&self) -> CgroupId {
        self.cgroup_id
    }

    /// The unit of the peer, with its slot and its instance.
    #[must_use]
    pub const fn unit(&self) -> &SlotUnit {
        &self.unit
    }

    /// The pidfd of the peer. It polls readable (`POLLIN`) once the peer
    /// has exited. A service that holds a session polls it, and ends the
    /// session at `POLLIN`.
    #[must_use]
    pub fn pidfd(&self) -> BorrowedFd<'_> {
        self.pidfd.as_fd()
    }

    /// The pidfd of the peer, for a service that keeps it longer than the
    /// `SlotPeer`.
    #[must_use]
    pub fn into_pidfd(self) -> OwnedFd {
        self.pidfd
    }
}

/// Checks that the peer of the Unix socket `socket` is a live process in
/// a unit `ci-runner-slot<slot>@<instance>.service` of the slice of the
/// job VMs (design section 6.3). `slot` comes from the `open` line of the
/// session, never from the peer.
///
/// 1. It reads a pidfd of the peer with `SO_PEERPIDFD`. The kernel
///    records the peer at the connect, so a process that passes the fd on
///    does not change it.
/// 2. It reads the cgroup id of the peer with `PIDFD_GET_INFO`. It
///    requires that `/sys/fs/cgroup` is a cgroup v2 file system that shows
///    the root of the hierarchy. It opens the slice folder
///    `ci.slice/ci-runner.slice/ci-runner-vms.slice` one part at a time
///    from there, with no symlink followed, and lists it from that fd. It
///    requires that each part and each sub-folder lies on the mount of the
///    root, and that the one sub-folder with that inode is a unit of
///    `slot`. It reads no file of `/proc`.
/// 3. It polls the pidfd and requires no event. The cgroup id can stay
///    readable after the exit of the peer (M2(f)), so only this step shows
///    the exit.
///
/// A peer that has exited can also fail step 2: `PIDFD_GET_INFO` gives
/// `ESRCH` once its process is reaped, and its unit folder can be gone.
/// So when step 2 fails, the check polls the pidfd too, and an exit gives
/// `Live(Exited)`. An exited peer that passes step 1 gives that refusal,
/// unless the poll itself fails: then the fault of step 2 stays. Step 1
/// fails with `Pidfd` on a kernel that gives no pidfd for a reaped peer.
///
/// # Errors
///
/// The `Refusal` of the first step that failed, or `Live(Exited)` for a
/// peer that has exited.
pub fn check(socket: BorrowedFd<'_>, slot: Slot) -> Result<SlotPeer, Refusal> {
    let pidfd = ci_runner_sys::peer_pidfd(socket).map_err(Refusal::Pidfd)?;
    let (cgroup_id, unit) = match slot_unit(pidfd.as_fd(), slot) {
        Ok(found) => found,
        Err(fault) => return Err(cgroup_refusal(pidfd.as_fd(), fault)),
    };
    alive(pidfd.as_fd()).map_err(Refusal::Live)?;
    Ok(SlotPeer {
        pidfd,
        cgroup_id,
        unit,
    })
}

/// Step 2 of `check`: the cgroup id of the peer of `pidfd`, and its unit
/// of `slot` in the slice folder.
fn slot_unit(pidfd: BorrowedFd<'_>, slot: Slot) -> Result<(CgroupId, SlotUnit), CgroupFault> {
    let id = ci_runner_sys::pidfd_cgroup_id(pidfd)
        .map_err(CgroupFault::Info)?
        .ok_or(CgroupFault::NoId)?;
    let cgroup_id = CgroupId(id);
    let entries = slice_entries()?;
    let unit = unit_of(cgroup_id, &entries, slot)?;
    Ok((cgroup_id, unit))
}

/// The refusal of a failed step 2. A pidfd that polls ready shows that
/// the peer has exited, and that is the cause, so the refusal is
/// `Live(Exited)`. Else it is the fault of step 2. A poll that fails
/// keeps the fault of step 2.
fn cgroup_refusal(pidfd: BorrowedFd<'_>, fault: CgroupFault) -> Refusal {
    match alive(pidfd) {
        Err(LiveFault::Exited) => Refusal::Live(LiveFault::Exited),
        Ok(()) | Err(LiveFault::Poll(_)) => Refusal::Cgroup(fault),
    }
}

/// Checks that the peer of the Unix socket `socket` runs as the user
/// `ci-runner`, the user of the daemon (design section 6.2, step 2). A
/// service calls it on each control connection, before it reads the
/// `open` line.
///
/// 1. It reads the uid of `ci-runner` from `/etc/passwd`.
/// 2. It reads the uid of the peer with `SO_PEERCRED`. The kernel records
///    it at the connect.
/// 3. It requires that the two are equal.
///
/// # Errors
///
/// The `DaemonRefusal` of the first step that failed.
pub fn check_daemon(socket: BorrowedFd<'_>) -> Result<(), DaemonRefusal> {
    let passwd =
        fs::read_to_string(PASSWD).map_err(|error| DaemonRefusal::User(UserFault::Read(error)))?;
    let uid = daemon_uid(&passwd).map_err(DaemonRefusal::User)?;
    let peer = rustix::net::sockopt::socket_peercred(socket)
        .map_err(|error| DaemonRefusal::PeerCred(error.into()))?
        .uid
        .as_raw();
    if peer != uid {
        return Err(DaemonRefusal::OtherUid(peer));
    }
    Ok(())
}

/// The facts of `statx` that step 2 uses: the type, the inode and the
/// mount id.
const STATX_WANT: StatxFlags = StatxFlags::TYPE
    .union(StatxFlags::INO)
    .union(StatxFlags::MNT_ID);

/// The flags of each `open` of step 2: a folder, read only, with no
/// symlink followed.
const OPEN_FOLDER: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

/// The `statx` of `name` in the folder `folder`, with no symlink and no
/// automount followed. A kernel of 5.8 or later fills the mount id. An
/// older one gives an error here.
fn stat_at(folder: BorrowedFd<'_>, name: &CStr, flags: AtFlags) -> io::Result<Statx> {
    let flags = flags | AtFlags::SYMLINK_NOFOLLOW | AtFlags::NO_AUTOMOUNT;
    let stat = statx(folder, name, flags, STATX_WANT)?;
    if stat.stx_mask & StatxFlags::MNT_ID.bits() == 0 {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "statx gave no STATX_MNT_ID",
        ));
    }
    Ok(stat)
}

/// The `statx` of the open folder `folder` itself.
fn stat_of(folder: BorrowedFd<'_>) -> io::Result<Statx> {
    stat_at(folder, c"", AtFlags::EMPTY_PATH)
}

/// Requires that the folder `root` is the root of a cgroup v2 file
/// system: `fstatfs` gives `CGROUP2_SUPER_MAGIC`, and its inode is the
/// inode of the root of the hierarchy. Gives the mount id of `root`.
fn cgroup_view(root: BorrowedFd<'_>) -> Result<u64, CgroupFault> {
    let fs = fstatfs(root).map_err(|error| CgroupFault::Root(error.into()))?;
    let stat = stat_of(root).map_err(CgroupFault::Root)?;
    cgroup_root(
        fs.f_type == ci_runner_sys::CGROUP2_SUPER_MAGIC,
        stat.stx_ino,
    )?;
    Ok(stat.stx_mnt_id)
}

/// The sub-folders of the slice folder. The check opens the cgroup root,
/// then each part of the slice path from the fd of the part before. A
/// part on another mount than the root is `Mounted`. `openat2` with
/// `RESOLVE_NO_XDEV` would do this in one call, but systemd refuses
/// `openat2` with `ENOSYS` under `RestrictSUIDSGID=yes`, which
/// `DynamicUser=yes` implies.
fn slice_entries() -> Result<Vec<SliceEntry>, CgroupFault> {
    let root = open(CGROUP_ROOT, OPEN_FOLDER, Mode::empty())
        .map_err(|error| CgroupFault::Root(error.into()))?;
    let mount = cgroup_view(root.as_fd())?;
    let mut folder = root;
    for part in SLICE_PATH.split('/').filter(|part| !part.is_empty()) {
        let next = openat(&folder, part, OPEN_FOLDER, Mode::empty())
            .map_err(|error| CgroupFault::Slice(error.into()))?;
        let stat = stat_of(next.as_fd()).map_err(CgroupFault::Slice)?;
        if stat.stx_mnt_id != mount {
            return Err(CgroupFault::Mounted);
        }
        folder = next;
    }
    list(folder.as_fd(), mount).map_err(CgroupFault::Slice)
}

/// The sub-folders of the folder `slice`, each with its inode and whether
/// it lies on another mount than `mount`. An entry that is gone before its
/// `statx` is skipped: a unit that just ended is no live peer.
fn list(slice: BorrowedFd<'_>, mount: u64) -> io::Result<Vec<SliceEntry>> {
    let mut entries = Vec::new();
    for entry in Dir::read_from(slice)? {
        let entry = entry?;
        let name = entry.file_name();
        if name == c"." || name == c".." {
            continue;
        }
        let stat = match stat_at(slice, name, AtFlags::empty()) {
            Ok(stat) => stat,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if FileType::from_raw_mode(stat.stx_mode.into()) == FileType::Directory {
            entries.push(SliceEntry {
                name: OsString::from_vec(name.to_bytes().to_vec()),
                inode: stat.stx_ino,
                other_mount: stat.stx_mnt_id != mount,
            });
        }
    }
    Ok(entries)
}

/// Requires no event on the pidfd `pidfd`, with no wait. A pidfd polls
/// readable once its process has exited (measurement M2(f)).
fn alive(pidfd: BorrowedFd<'_>) -> Result<(), LiveFault> {
    let mut fds = [PollFd::new(&pidfd, PollFlags::IN)];
    let now = Timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    let ready = poll(&mut fds, Some(&now)).map_err(|error| LiveFault::Poll(error.into()))?;
    if ready != 0 || fds.iter().any(|fd| !fd.revents().is_empty()) {
        return Err(LiveFault::Exited);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    //! These tests need the kernel. Each one prints one line: what it
    //! showed, or that it did not run and why. A test that did not run
    //! prints `not run`, and the CI step "Kernel tests ran" fails on it.

    use std::io::{Read, Write};
    use std::os::fd::AsFd;
    use std::os::unix::net::{UnixDatagram, UnixListener, UnixStream};

    use super::{alive, cgroup_view, check, check_daemon};
    use crate::unit::{CgroupFault, Slot};
    use crate::{DaemonRefusal, LiveFault, Refusal};

    /// The variable that makes `peer_child` act. Only
    /// `the_pidfd_of_a_peer_polls_ready_after_its_exit` sets it, for its
    /// own child: the path of the socket to connect to.
    const PEER_CHILD: &str = "CI_RUNNER_PEER_TEST_CHILD";

    /// `None` with a line when the kernel offers neither `SO_PEERPIDFD`
    /// nor `PIDFD_GET_INFO` with `PIDFD_INFO_CGROUPID`.
    fn kernel_has_the_calls(name: &str) -> Option<()> {
        let (a, _b) = UnixStream::pair().unwrap();
        let pidfd = match ci_runner_sys::peer_pidfd(a.as_fd()) {
            Ok(pidfd) => pidfd,
            Err(error) => {
                println!(
                    "{name}: not run: the kernel gives no SO_PEERPIDFD ({error}). The proof in a VM runs it."
                );
                return None;
            }
        };
        match ci_runner_sys::pidfd_cgroup_id(pidfd.as_fd()) {
            Ok(Some(_)) => Some(()),
            Ok(None) => {
                println!(
                    "{name}: not run: the kernel gives no PIDFD_INFO_CGROUPID. The proof in a VM runs it."
                );
                None
            }
            Err(error) => {
                println!(
                    "{name}: not run: the kernel has no PIDFD_GET_INFO ({error}). The proof in a VM runs it."
                );
                None
            }
        }
    }

    #[test]
    fn a_socketpair_peer_in_the_cgroup_of_the_test_is_refused() {
        let name = "peer check of a socketpair";
        if kernel_has_the_calls(name).is_none() {
            return;
        }
        let (a, _b) = UnixStream::pair().unwrap();
        // The peer is the process of the test. It runs in no slot unit.
        let refusal = check(a.as_fd(), Slot::new(0)).unwrap_err();
        assert!(matches!(refusal, Refusal::Cgroup(_)), "{refusal:?}");
        println!("{name}: refused in step 2: {refusal}");
    }

    #[test]
    fn a_socket_with_no_peer_is_refused_in_step_1() {
        let name = "peer check of a socket with no peer";
        if kernel_has_the_calls(name).is_none() {
            return;
        }
        let socket = UnixDatagram::unbound().unwrap();
        let refusal = check(socket.as_fd(), Slot::new(0)).unwrap_err();
        // The kernel gives ENODATA when the socket recorded no peer
        // (`net/core/sock.c`, case `SO_PEERPIDFD`).
        let errno = match &refusal {
            Refusal::Pidfd(error) => error.raw_os_error(),
            _ => None,
        };
        assert_eq!(
            errno,
            Some(rustix::io::Errno::NODATA.raw_os_error()),
            "{refusal:?}"
        );
        println!("{name}: refused in step 1: {refusal}");
    }

    /// The child: it connects to the socket of `PEER_CHILD`, waits for one
    /// byte, and exits. With no `PEER_CHILD` it does nothing.
    #[test]
    fn peer_child() {
        let Some(path) = std::env::var_os(PEER_CHILD) else {
            return;
        };
        let mut stream = UnixStream::connect(path).unwrap();
        let mut byte = [0_u8; 1];
        stream.read_exact(&mut byte).unwrap();
    }

    #[test]
    fn the_pidfd_of_a_peer_polls_ready_after_its_exit() {
        let name = "liveness of a peer";
        if kernel_has_the_calls(name).is_none() {
            return;
        }
        let folder =
            std::env::temp_dir().join(format!("ci-runner-peer-live-{}", std::process::id()));
        std::fs::create_dir_all(&folder).unwrap();
        let path = folder.join("peer.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "check::tests::peer_child",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(PEER_CHILD, &path)
            .stdout(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let (mut stream, _) = listener.accept().unwrap();
        let pidfd = ci_runner_sys::peer_pidfd(stream.as_fd()).unwrap();
        let before = alive(pidfd.as_fd());
        stream.write_all(b"x").unwrap();
        let status = child.wait().unwrap();
        let after = alive(pidfd.as_fd());
        // The check of the fd after the exit, as a service that kept it.
        // The child is reaped, so a kernel 7.0 gives ESRCH in step 2 (the
        // host proof, step attach-match, 2026-11-21). The child ran in the
        // cgroup of the test, so a kernel that still gives its cgroup id
        // fails step 2 with NotInSlice. Each must give Live(Exited).
        let kept = check(stream.as_fd(), Slot::new(0));
        std::fs::remove_dir_all(&folder).unwrap();
        assert!(status.success(), "the child failed: {status}");
        assert!(before.is_ok(), "a live peer: {before:?}");
        assert!(matches!(after, Err(LiveFault::Exited)), "{after:?}");
        assert!(
            matches!(kept, Err(Refusal::Live(LiveFault::Exited))),
            "{kept:?}"
        );
        let id = ci_runner_sys::pidfd_cgroup_id(pidfd.as_fd());
        println!(
            "{name}: no event while the peer ran, and POLLIN after its exit. PIDFD_GET_INFO after the exit gave {id:?}, and the check of the kept fd refused with Live(Exited)."
        );
    }

    #[test]
    fn a_folder_of_another_file_system_is_no_cgroup_view() {
        let name = "cgroup view of the temp folder";
        // The temp folder is a tmpfs, an overlay or a disk file system,
        // never cgroup v2.
        let folder = std::fs::File::open(std::env::temp_dir()).unwrap();
        let fault = cgroup_view(folder.as_fd()).unwrap_err();
        assert!(matches!(fault, CgroupFault::NotCgroup2), "{fault:?}");
        println!("{name}: refused: {fault}");
    }

    #[test]
    fn the_cgroup_mount_of_the_test_shows_the_root_or_is_refused() {
        let name = "cgroup view of /sys/fs/cgroup";
        let root = std::fs::File::open("/sys/fs/cgroup").unwrap();
        // The root in the initial cgroup namespace, and the root of its own
        // namespace in a container of a CI job.
        match cgroup_view(root.as_fd()) {
            Ok(mount) => println!("{name}: the root of the cgroup v2 hierarchy, mount id {mount}"),
            Err(fault @ CgroupFault::NotCgroupRoot(_)) => {
                println!("{name}: refused, a cgroup namespace of its own: {fault}");
            }
            Err(fault) => panic!("{name}: {fault:?}"),
        }
    }

    #[test]
    fn check_daemon_refuses_a_socketpair_peer() {
        let name = "daemon check of a socketpair";
        let (a, _b) = UnixStream::pair().unwrap();
        // The peer is the process of the test, which does not run as
        // ci-runner, or a host with no such user.
        let refusal = check_daemon(a.as_fd()).unwrap_err();
        assert!(
            matches!(refusal, DaemonRefusal::User(_) | DaemonRefusal::OtherUid(_)),
            "{refusal:?}"
        );
        println!("{name}: refused: {refusal}");
    }
}
