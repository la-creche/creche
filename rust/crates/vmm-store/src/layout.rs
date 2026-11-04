//! The names of every file that the vmm keeps for one VM (gap G5 of the
//! phase 2 plan, taken by decision CH36).
//!
//! The state root holds what lives across a host restart: the session
//! rows, the session disks and the snapshots. The run root is the runtime
//! directory of the vmm unit. It holds `vmm.sock` (decision CH20) and one
//! directory per VM with the sockets of Cloud Hypervisor. The vmm config
//! (PR L20) passes both roots in. No path is compiled in.

use std::fmt;
use std::path::{Component, Path, PathBuf};

use creche_wire::{HostPort, SessionId};

/// The prefix of the systemd scope of each VM. The vmm finds a leftover
/// scope by this prefix (fact 76).
pub const SCOPE_PREFIX: &str = "creche-vm-";

/// The most bytes in the path of a Unix socket: `sun_path` holds 108
/// bytes, and the last one is the NUL.
pub const SOCKET_PATH_MAX: usize = 107;

/// The vsock socket of Cloud Hypervisor. A host listener for a port is
/// this name plus `_<port>` (decision CH24).
const VSOCK_SOCKET: &str = "vsock.sock";

/// A session id in the form that `SessionId` accepts. Every session id has
/// this length, so the run root check uses it for the longest path.
const ID_OF_MAX_LEN: &str = "00000000-0000-0000-0000-000000000000";

/// Why a path is not a root.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RootError {
    /// The path is not absolute.
    NotAbsolute,
    /// The path has a `..` component.
    ParentComponent,
    /// A socket path under the run root has more bytes than
    /// `SOCKET_PATH_MAX`. The value is the byte count of the longest one.
    SocketPathTooLong(usize),
}

impl fmt::Display for RootError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotAbsolute => formatter.write_str("a root is an absolute path"),
            Self::ParentComponent => formatter.write_str("a root has no `..` component"),
            Self::SocketPathTooLong(bytes) => write!(
                formatter,
                "a socket path under the run root has {bytes} bytes, more than {SOCKET_PATH_MAX}"
            ),
        }
    }
}

impl std::error::Error for RootError {}

/// Checks the two rules that both roots share.
fn check_root(path: &Path) -> Result<(), RootError> {
    if !path.is_absolute() {
        return Err(RootError::NotAbsolute);
    }
    if path.components().any(|part| part == Component::ParentDir) {
        return Err(RootError::ParentComponent);
    }
    Ok(())
}

/// The state root: an absolute path with no `..` component.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateRoot(PathBuf);

impl StateRoot {
    /// Checks `path` and makes the state root.
    ///
    /// # Errors
    ///
    /// `NotAbsolute` or `ParentComponent`.
    pub fn new(path: PathBuf) -> Result<Self, RootError> {
        check_root(&path)?;
        Ok(Self(path))
    }

    /// The root itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }

    // Decision CH39 rule 3 names the files under the state root:
    // `rows/<id>.json`, `sessions/<id>/disk.ext4` (temp `disk.ext4.tmp`),
    // `sessions/<id>/snapshots/<generation>/`, and `store/<sha256>` with
    // `store/<sha256>.identity`. A snapshot holds the disk path, so a
    // rename costs a cold boot of every VM (plan risk 8) and an edit of the
    // confine rules that name these paths.

    /// The directory of every session row file (PR D2).
    #[must_use]
    pub fn rows_dir(&self) -> PathBuf {
        self.0.join("rows")
    }

    /// The file of the session row of `id` (PR D2).
    #[must_use]
    pub fn row_file(&self, id: &SessionId) -> PathBuf {
        self.rows_dir().join(format!("{id}.json"))
    }

    /// The temp file that a write of the row of `id` renames over the row
    /// file (PR D2). A crash can leave it, and a load skips it.
    #[must_use]
    pub fn row_temp_file(&self, id: &SessionId) -> PathBuf {
        self.rows_dir().join(format!("{id}.json.tmp"))
    }

    /// The digest store: one read-only copy of each root disk and kernel,
    /// named by its SHA-256 (fact 56, PR D5).
    #[must_use]
    pub fn store_dir(&self) -> PathBuf {
        self.0.join("store")
    }

    /// The directory of the session disk and the snapshots of `id`.
    #[must_use]
    pub fn session(&self, id: &SessionId) -> SessionDir {
        SessionDir(self.0.join("sessions").join(id.as_str()))
    }
}

/// A snapshot generation. Each snapshot of a session gets a new one
/// (PR D9).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SnapshotGeneration(pub u64);

/// The directory of one session under the state root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionDir(PathBuf);

impl SessionDir {
    /// The directory itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }

    /// The session disk (PR D7).
    #[must_use]
    pub fn disk(&self) -> PathBuf {
        self.0.join("disk.ext4")
    }

    /// The directory that holds every snapshot generation.
    #[must_use]
    pub fn snapshots(&self) -> PathBuf {
        self.0.join("snapshots")
    }

    /// The directory of one snapshot generation.
    #[must_use]
    pub fn snapshot(&self, generation: SnapshotGeneration) -> PathBuf {
        self.snapshots().join(generation.0.to_string())
    }
}

/// The run root: an absolute path with no `..` component, short enough
/// that every socket path under it fits in `SOCKET_PATH_MAX`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunRoot(PathBuf);

impl RunRoot {
    /// Checks `path` and makes the run root.
    ///
    /// # Errors
    ///
    /// `NotAbsolute`, `ParentComponent` or `SocketPathTooLong`.
    pub fn new(path: PathBuf) -> Result<Self, RootError> {
        check_root(&path)?;

        let root = Self(path);
        let vm = VmRunDir(root.0.join("vm").join(ID_OF_MAX_LEN));
        let longest = HostPort::ALL
            .iter()
            .map(|&port| vm.host_listener(port))
            .chain([vm.vsock_socket(), vm.api_socket(), root.vmm_socket()])
            .map(|socket| socket.as_os_str().len())
            .max()
            .unwrap_or(0);
        if longest > SOCKET_PATH_MAX {
            return Err(RootError::SocketPathTooLong(longest));
        }
        Ok(root)
    }

    /// The root itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }

    /// The socket of the host API (decision CH20).
    #[must_use]
    pub fn vmm_socket(&self) -> PathBuf {
        self.0.join("vmm.sock")
    }

    /// The run directory of the VM of `id` (gap G5).
    #[must_use]
    pub fn vm(&self, id: &SessionId) -> VmRunDir {
        VmRunDir(self.0.join("vm").join(id.as_str()))
    }
}

/// The run directory of one VM. Cloud Hypervisor makes its sockets here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VmRunDir(PathBuf);

impl VmRunDir {
    /// The directory itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }

    /// The vsock socket (`--vsock socket=`). The vmm connects here to reach
    /// a guest port.
    #[must_use]
    pub fn vsock_socket(&self) -> PathBuf {
        self.0.join(VSOCK_SOCKET)
    }

    /// The listener for a host port, `vsock.sock_<port>` (decision CH24).
    /// The guest dials the port, and Cloud Hypervisor connects here.
    #[must_use]
    pub fn host_listener(&self, port: HostPort) -> PathBuf {
        self.0.join(format!("{VSOCK_SOCKET}_{}", port.number()))
    }

    /// The API socket (`--api-socket`).
    #[must_use]
    pub fn api_socket(&self) -> PathBuf {
        self.0.join("api.sock")
    }

    /// The lock file of the API socket. Under Landlock Cloud Hypervisor
    /// cannot remove it, so the vmm does (fact 75).
    #[must_use]
    pub fn api_socket_lock(&self) -> PathBuf {
        self.0.join("api.sock.lock")
    }

    /// The guest serial output, kept until the next start (gap G23). The
    /// gap writes `<run>/serial.log`. This reading puts it in the run
    /// directory of the VM, so that two VMs do not share one file.
    #[must_use]
    pub fn serial_log(&self) -> PathBuf {
        self.0.join("serial.log")
    }
}

/// The systemd scope unit name of the VM of `id` (fact 76).
#[must_use]
pub fn scope_unit(id: &SessionId) -> String {
    format!("{SCOPE_PREFIX}{id}")
}
