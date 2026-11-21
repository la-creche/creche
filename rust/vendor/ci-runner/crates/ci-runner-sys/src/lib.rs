//! The Linux system calls of the slot program (design section 10.6) and
//! of the peer check `ci-runner-peer` (design section 6.3) that no safe
//! wrapper offers. Each function makes one call and returns a typed value
//! or the `errno` of the call.
//!
//! | Function | Call | Step |
//! |---|---|---|
//! | `project_attr` | `ioctl(FS_IOC_FSGETXATTR)` | 10.6, 6: the project id and the inherit flag of the run folder |
//! | `cloexec_from` | `close_range(.., CLOSE_RANGE_CLOEXEC)` | 10.6, 9.1: no fd above 2 reaches CH |
//! | `landlock_abi`, `Ruleset` | `landlock_create_ruleset`, `landlock_add_rule`, `landlock_restrict_self` | 10.6, 9.5: the Landlock ruleset |
//! | `seccomp_filter_all_threads` | `seccomp(SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC)` | 10.6, 1: the tap guard of the slot program |
//! | `ioctl_no_fd` | `ioctl(-1, ..)` | 10.6, 9.6: the self-probe of the tap guard |
//! | `peer_pidfd` | `getsockopt(SOL_SOCKET, SO_PEERPIDFD)` | 6.3, 1: the peer of a passed fd |
//! | `pidfd_cgroup_id` | `ioctl(PIDFD_GET_INFO)` with `PIDFD_INFO_CGROUPID` | 6.3, 2: the cgroup of that peer |
//!
//! The constants of the kernel ABI of each call live next to it, with the
//! header that they come from.
//!
//! `rustix` 1.1.5 has none of these calls. Each `unsafe` block holds one
//! call, and its SAFETY comment names why the arguments are valid. The
//! crate is empty on another OS.

#![cfg(target_os = "linux")]

use std::io;
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use std::ptr;

use libc::{c_int, c_long, c_uint, c_ulong};

/// The value of a system call, or the `errno` that it set.
fn checked(result: c_long) -> io::Result<c_long> {
    if result < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(result)
}

/// `struct fsxattr` of `linux/fs.h`.
#[repr(C)]
#[derive(Debug, Default)]
struct FsXattr {
    /// `fsx_xflags`.
    xflags: u32,
    /// `fsx_extsize`.
    extsize: u32,
    /// `fsx_nextents`.
    nextents: u32,
    /// `fsx_projid`.
    projid: u32,
    /// `fsx_cowextsize`.
    cowextsize: u32,
    /// `fsx_pad`.
    pad: [u8; 8],
}

/// The size of `struct fsxattr` that the number of the ioctl holds.
const FS_XATTR_SIZE: usize = 28;

const _: () = assert!(size_of::<FsXattr>() == FS_XATTR_SIZE);

/// `FS_IOC_FSGETXATTR`: `_IOR('X', 31, struct fsxattr)`. The read
/// direction 2 in bits 30 and 31, the size 28 in bits 16 to 29, `'X'` in
/// bits 8 to 15 and 31 in bits 0 to 7.
const FS_IOC_FSGETXATTR: c_ulong = 0x801c_581f;

/// `FS_XFLAG_PROJINHERIT`: each new entry of the folder gets the project
/// id of the folder.
const FS_XFLAG_PROJINHERIT: u32 = 0x0000_0200;

/// The project id and the inherit flag of one file or folder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProjectAttr {
    /// `fsx_projid`.
    pub project_id: u32,
    /// Whether `fsx_xflags` holds `FS_XFLAG_PROJINHERIT`.
    pub inherit: bool,
}

/// The project id and the inherit flag of the open file `file`. The call
/// needs no privilege.
///
/// # Errors
///
/// The `errno` of the ioctl. A file system with no such call gives
/// `ENOTTY` or `EOPNOTSUPP`.
#[expect(
    unsafe_code,
    reason = "FS_IOC_FSGETXATTR has no safe wrapper in rustix 1.1.5"
)]
pub fn project_attr(file: BorrowedFd<'_>) -> io::Result<ProjectAttr> {
    let mut attr = FsXattr::default();
    let fd: RawFd = file.as_raw_fd();
    // SAFETY: `fd` is open for the life of `file`. The kernel writes
    // `size_of::<FsXattr>()` bytes, the size that the request number
    // holds, into `attr`, which lives on this stack frame until the call
    // returns.
    let result = unsafe { libc::syscall(libc::SYS_ioctl, fd, FS_IOC_FSGETXATTR, &raw mut attr) };
    checked(result)?;
    Ok(ProjectAttr {
        project_id: attr.projid,
        inherit: attr.xflags & FS_XFLAG_PROJINHERIT != 0,
    })
}

/// `CLOSE_RANGE_CLOEXEC`: mark the fds close-on-exec, and close none now.
const CLOSE_RANGE_CLOEXEC: c_uint = libc::CLOSE_RANGE_CLOEXEC;

/// Marks each fd from `first` on close-on-exec, so the next exec closes
/// it. An fd that is open now stays open until then.
///
/// # Errors
///
/// The `errno` of `close_range`. A kernel before 5.11 gives `EINVAL`.
#[expect(
    unsafe_code,
    reason = "close_range has no safe wrapper in rustix 1.1.5"
)]
pub fn cloexec_from(first: c_uint) -> io::Result<()> {
    // SAFETY: the call takes three integers and touches no memory of the
    // process. It marks fds and closes none, so no `OwnedFd` of the
    // process loses its fd.
    let result = unsafe {
        libc::syscall(
            libc::SYS_close_range,
            first,
            c_uint::MAX,
            CLOSE_RANGE_CLOEXEC,
        )
    };
    checked(result).map(drop)
}

/// `LANDLOCK_CREATE_RULESET_VERSION`.
const CREATE_RULESET_VERSION: c_uint = 1;

/// `LANDLOCK_RULE_PATH_BENEATH`.
const RULE_PATH_BENEATH: c_uint = 1;

/// `struct landlock_ruleset_attr` up to `handled_access_fs`, the part of
/// Landlock ABI 1 to 3. The kernel takes a shorter struct of an older ABI.
#[repr(C)]
#[derive(Debug)]
struct RulesetAttr {
    /// `handled_access_fs`.
    handled_access_fs: u64,
}

/// `struct landlock_path_beneath_attr`, which the kernel declares packed.
#[repr(C, packed)]
#[derive(Debug)]
struct PathBeneathAttr {
    /// `allowed_access`.
    allowed_access: u64,
    /// `parent_fd`.
    parent_fd: i32,
}

/// The Landlock ABI of the kernel: 1 or more.
///
/// # Errors
///
/// `ENOSYS` when the kernel has no Landlock, and `EOPNOTSUPP` when the
/// boot turned it off. A seccomp filter can give another `errno`.
#[expect(
    unsafe_code,
    reason = "landlock_create_ruleset has no safe wrapper in rustix 1.1.5"
)]
pub fn landlock_abi() -> io::Result<u32> {
    // SAFETY: with the flag `LANDLOCK_CREATE_RULESET_VERSION` the kernel
    // takes a null attribute and the size 0, and reads no memory.
    let result = unsafe {
        libc::syscall(
            libc::SYS_landlock_create_ruleset,
            ptr::null::<RulesetAttr>(),
            0_usize,
            CREATE_RULESET_VERSION,
        )
    };
    u32::try_from(checked(result)?).map_err(|_| io::Error::from(io::ErrorKind::InvalidData))
}

/// A Landlock ruleset that is not applied yet.
#[derive(Debug)]
pub struct Ruleset(OwnedFd);

impl Ruleset {
    /// A new ruleset that handles each file right in `handled_fs`. A
    /// handled right is denied on each path that no rule names.
    ///
    /// # Errors
    ///
    /// The `errno` of `landlock_create_ruleset`. A right that the kernel
    /// does not know gives `EINVAL`.
    #[expect(
        unsafe_code,
        reason = "landlock_create_ruleset has no safe wrapper in rustix 1.1.5"
    )]
    pub fn new(handled_fs: u64) -> io::Result<Self> {
        let attr = RulesetAttr {
            handled_access_fs: handled_fs,
        };
        // SAFETY: `attr` lives on this stack frame until the call returns,
        // and the size is its own size. The kernel only reads it.
        let result = unsafe {
            libc::syscall(
                libc::SYS_landlock_create_ruleset,
                &raw const attr,
                size_of::<RulesetAttr>(),
                0_u32,
            )
        };
        let fd = RawFd::try_from(checked(result)?)
            .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))?;
        Ok(Self(owned(fd)))
    }

    /// Allows the rights `access` on the file or the folder `parent`, and
    /// beneath a folder. `parent` is open with `O_PATH` or for reading.
    ///
    /// # Errors
    ///
    /// The `errno` of `landlock_add_rule`. A folder right on a file gives
    /// `EINVAL`.
    #[expect(
        unsafe_code,
        reason = "landlock_add_rule has no safe wrapper in rustix 1.1.5"
    )]
    pub fn allow_beneath(&self, parent: BorrowedFd<'_>, access: u64) -> io::Result<()> {
        let attr = PathBeneathAttr {
            allowed_access: access,
            parent_fd: parent.as_raw_fd(),
        };
        // SAFETY: the ruleset fd and `parent` stay open for the call.
        // `attr` lives on this stack frame until the call returns, and the
        // kernel only reads it.
        let result = unsafe {
            libc::syscall(
                libc::SYS_landlock_add_rule,
                self.0.as_raw_fd(),
                RULE_PATH_BENEATH,
                &raw const attr,
                0_u32,
            )
        };
        checked(result).map(drop)
    }

    /// Applies the ruleset to the calling thread and each process that it
    /// starts. No call lifts it. The kernel requires no_new_privs or
    /// `CAP_SYS_ADMIN`.
    ///
    /// # Errors
    ///
    /// The `errno` of `landlock_restrict_self`. With neither no_new_privs
    /// nor `CAP_SYS_ADMIN` it gives `EPERM`.
    #[expect(
        unsafe_code,
        reason = "landlock_restrict_self has no safe wrapper in rustix 1.1.5"
    )]
    pub fn restrict_self(self) -> io::Result<()> {
        // SAFETY: the ruleset fd stays open for the call, which reads no
        // memory of the process.
        let result =
            unsafe { libc::syscall(libc::SYS_landlock_restrict_self, self.0.as_raw_fd(), 0_u32) };
        checked(result).map(drop)
    }
}

/// One instruction of a classic BPF program: `struct sock_filter` of
/// `linux/filter.h`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BpfInsn {
    /// `code`: the class, the size, the mode or the operation.
    pub code: u16,
    /// `jt`: the instructions to skip when a jump holds.
    pub jt: u8,
    /// `jf`: the instructions to skip when a jump does not hold.
    pub jf: u8,
    /// `k`: the constant.
    pub k: u32,
}

/// The flag that installs a filter on each thread of the process at once.
/// The slot program has one thread at its first step, so the flag is a
/// second layer: no thread of a later version of the program can miss
/// the filter. The kernel refuses the call when a thread has another
/// filter tree. Measurement M1(d) of the build plan of the attach interface,
/// on 2026-11-18 in a rehearsal VM with kernel 7.0, read the count of
/// filters of a second thread of a process in a slot-shaped unit: it went
/// from 28 to 29 at an install with this flag on the main thread.
const FILTER_ALL_THREADS: c_ulong = libc::SECCOMP_FILTER_FLAG_TSYNC;

/// Installs the seccomp filter `program` on each thread of the process.
/// Each child and each exec inherit it, and no call lifts it. The kernel
/// requires no_new_privs or `CAP_SYS_ADMIN`.
///
/// # Errors
///
/// The `errno` of `seccomp`: `EACCES` with neither no_new_privs nor
/// `CAP_SYS_ADMIN`, `EINVAL` for a program that the kernel refuses or
/// that has more than 65535 instructions, and `ENOSYS` on a kernel with no
/// seccomp. A thread that the kernel could not move to the filter gives
/// `ErrorKind::ResourceBusy`, with no filter installed.
#[expect(
    unsafe_code,
    reason = "seccomp(SECCOMP_SET_MODE_FILTER) has no safe wrapper in rustix 1.1.5"
)]
pub fn seccomp_filter_all_threads(program: &[BpfInsn]) -> io::Result<()> {
    let mut filter: Vec<libc::sock_filter> = program
        .iter()
        .map(|insn| libc::sock_filter {
            code: insn.code,
            jt: insn.jt,
            jf: insn.jf,
            k: insn.k,
        })
        .collect();
    let len =
        u16::try_from(filter.len()).map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
    let prog = libc::sock_fprog {
        len,
        filter: filter.as_mut_ptr(),
    };
    // SAFETY: `prog` points at `len` instructions of `filter`, which lives
    // on this stack frame until the call returns. The kernel copies the
    // program before it returns and writes no memory of the process.
    let result = unsafe {
        libc::syscall(
            libc::SYS_seccomp,
            libc::SECCOMP_SET_MODE_FILTER,
            FILTER_ALL_THREADS,
            &raw const prog,
        )
    };
    // With the flag, a result above 0 is the id of the thread that the
    // kernel could not move to the filter.
    if checked(result)? > 0 {
        return Err(io::Error::from(io::ErrorKind::ResourceBusy));
    }
    Ok(())
}

/// The fd that no process has: `ioctl` on it reaches no file.
const NO_FD: c_int = -1;

/// Calls `ioctl(-1, request, 0)`. The kernel gives `EBADF` for each
/// request, so only a seccomp filter of the process gives another
/// `errno`. `request` is the whole register, so a test can set bits above
/// bit 31, which the kernel drops and a seccomp filter sees.
///
/// # Errors
///
/// The `errno` of the call: `EBADF`, or the `errno` of a filter.
#[expect(
    unsafe_code,
    reason = "an ioctl with a request number of the caller has no safe wrapper in rustix 1.1.5"
)]
pub fn ioctl_no_fd(request: u64) -> io::Result<()> {
    let request =
        c_ulong::try_from(request).map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
    // SAFETY: the fd -1 names no file, so the kernel reaches no driver
    // and reads or writes no memory of the process. The argument is the
    // integer 0, not a pointer.
    let result = unsafe { libc::syscall(libc::SYS_ioctl, NO_FD, request, 0_usize) };
    checked(result).map(drop)
}

// The kernel ABI of the peer check of `ci-runner-peer` (design section
// 6.3). Each value holds for each arch that takes `asm-generic/socket.h`
// and the generic `_IOWR` encoding, x86_64 and aarch64 among them. Alpha,
// mips, parisc, powerpc and sparc encode some of them in another way, so
// these items exist only on x86_64 and aarch64.

// `libc` 0.2.191 gives `SO_PEERPIDFD` (77, since Linux 6.5) and
// `PIDFD_INFO_CGROUPID` (`1 << 2`) of these headers, and the calls below
// take them from there.

/// `struct pidfd_info` of `include/uapi/linux/pidfd.h` of Linux 7.0, the
/// struct of 80 bytes that measurement M2 of the build plan of the attach
/// interface used. `libc` 0.2.191 has `pidfd_info` only up to `exit_code`
/// (64 bytes), marked `#[non_exhaustive]`, and `PIDFD_GET_INFO` with that
/// size. The crate keeps its own struct of 80 bytes and its own request
/// number. The kernel reads the size from the request number, and the
/// check uses only `mask` (offset 0) and `cgroupid` (offset 8). A kernel of
/// 6.13 or later takes any size from 64 bytes on, and fills each byte that
/// it does not know with zero.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
#[repr(C)]
#[derive(Debug, Default)]
struct PidfdInfo {
    /// `mask`: the caller sets the parts that it asks for, and the kernel
    /// sets the parts that it gave.
    mask: u64,
    /// `cgroupid`: the id of the cgroup v2 folder of the process.
    cgroupid: u64,
    /// `pid`, `tgid`, `ppid`, `ruid`, `rgid`, `euid`, `egid`, `suid`,
    /// `sgid`, `fsuid` and `fsgid`, in this order.
    ids: [u32; 11],
    /// `exit_code`.
    exit_code: i32,
    /// `coredump_mask`.
    coredump_mask: u32,
    /// `coredump_signal`.
    coredump_signal: u32,
    /// `supported_mask`.
    supported_mask: u64,
}

/// The size of `struct pidfd_info` that the number of the ioctl holds.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
const PIDFD_INFO_SIZE: usize = 80;

#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
const _: () = assert!(size_of::<PidfdInfo>() == PIDFD_INFO_SIZE);

/// `PIDFD_GET_INFO` of `include/uapi/linux/pidfd.h`, since Linux 6.13:
/// `_IOWR(PIDFS_IOCTL_MAGIC, 11, struct pidfd_info)`. The direction 3 (read
/// and write) in bits 30 and 31, the size 80 in bits 16 to 29, the magic
/// 0xFF in bits 8 to 15 and 11 in bits 0 to 7.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
const PIDFD_GET_INFO: c_ulong = 0xc050_ff0b;

/// `CGROUP2_SUPER_MAGIC` of `include/uapi/linux/magic.h`: the `f_type`
/// that `fstatfs` gives for a cgroup v2 file system. The peer check
/// requires it at `/sys/fs/cgroup`. The type is the `f_type` of
/// `rustix::fs::StatFs` on x86_64 and aarch64.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
pub const CGROUP2_SUPER_MAGIC: c_long = libc::CGROUP2_SUPER_MAGIC;

/// A pidfd of the process that connected the Unix socket `socket`:
/// `getsockopt(SOL_SOCKET, SO_PEERPIDFD)`. The kernel records that process
/// at the connect, or at the `socketpair` call. A process that passes the
/// fd on does not change it. The new fd is close-on-exec.
///
/// # Errors
///
/// The `errno` of `getsockopt`: `ENOPROTOOPT` on a kernel before 6.5,
/// `ENODATA` on a socket with no peer, and `ESRCH` on some kernels when
/// the peer has exited.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
#[expect(
    unsafe_code,
    reason = "getsockopt(SO_PEERPIDFD) has no safe wrapper in rustix 1.1.5"
)]
pub fn peer_pidfd(socket: BorrowedFd<'_>) -> io::Result<OwnedFd> {
    let mut fd: c_int = -1;
    let mut len = libc::socklen_t::try_from(size_of::<c_int>())
        .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))?;
    // SAFETY: `socket` is open for the call. The kernel writes at most
    // `len` bytes, the size of `fd`, into `fd`, and the new length into
    // `len`. Both live on this stack frame until the call returns.
    let result = unsafe {
        libc::getsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERPIDFD,
            (&raw mut fd).cast(),
            &raw mut len,
        )
    };
    checked(c_long::from(result))?;
    if usize::try_from(len).ok() != Some(size_of::<c_int>()) || fd < 0 {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    Ok(owned(fd))
}

/// The cgroup id of the process of the pidfd `pidfd`: `ioctl(PIDFD_GET_INFO)`
/// with `PIDFD_INFO_CGROUPID`. `None` when the kernel did not set that bit
/// in the returned mask. The id equals the inode number of the cgroup v2
/// folder of the process (measurement M2(b)). The call can give the id
/// also after the process exited, so it proves no liveness: M2(f) read the
/// old id after the exit of CH. A test on a kernel 7.0 got `ESRCH` for a
/// child that its parent had reaped.
///
/// # Errors
///
/// The `errno` of the ioctl: `ENOTTY` or `EINVAL` on a kernel before
/// 6.13, and `ESRCH` when the kernel no longer holds the process.
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
#[expect(
    unsafe_code,
    reason = "ioctl(PIDFD_GET_INFO) has no safe wrapper in rustix 1.1.5"
)]
pub fn pidfd_cgroup_id(pidfd: BorrowedFd<'_>) -> io::Result<Option<u64>> {
    let cgroupid = u64::from(libc::PIDFD_INFO_CGROUPID);
    let mut info = PidfdInfo {
        mask: cgroupid,
        ..PidfdInfo::default()
    };
    // SAFETY: `pidfd` is open for the call. The kernel reads `mask` and
    // writes at most `size_of::<PidfdInfo>()` bytes, the size that the
    // request number holds, into `info`, which lives on this stack frame
    // until the call returns.
    let result = unsafe {
        libc::syscall(
            libc::SYS_ioctl,
            pidfd.as_raw_fd(),
            PIDFD_GET_INFO,
            &raw mut info,
        )
    };
    checked(result)?;
    Ok((info.mask & cgroupid != 0).then_some(info.cgroupid))
}

/// The new fd `fd` that the kernel just returned, as an `OwnedFd`.
#[expect(unsafe_code, reason = "an fd number becomes an OwnedFd only unsafely")]
fn owned(fd: RawFd) -> OwnedFd {
    // SAFETY: the kernel returned `fd` as a new fd of this process, and no
    // other value owns it.
    unsafe { OwnedFd::from_raw_fd(fd) }
}
