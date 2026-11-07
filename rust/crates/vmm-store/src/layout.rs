//! The names of every file that the vmm keeps for one VM (gap G5 of the
//! phase 2 plan, taken by decision CH36).
//!
//! The state root holds what lives across a host restart: the session
//! rows, the session disks, the snapshots and the serial log of each
//! session. The run root is the runtime directory of the vmm unit, and
//! systemd removes it at each stop of the unit. It holds `vmm.sock`
//! (decision CH20) and one directory per VM with the sockets of Cloud
//! Hypervisor. So the serial log is in the state root, and a vmm restart
//! keeps it (decision CH59 rule 2). The vmm config
//! (PR L20) passes both roots in. No path is compiled in.
//!
//! Decisions CH51 and CH55 (PR D16): the run root also holds `SLOTS` slot
//! dirs `slot/<k>/`, one per AppArmor slot profile `creche-ch-slot-<k>`.
//! Each VM takes a free slot, and `vm/<id>` is a symlink to `../slot/<k>`.
//! So every `VmRunDir` path stays `vm/<id>/...` for its users, and the
//! vsock path in a snapshot holds across a restore into another slot.

use std::fmt;
use std::fs::{self, DirBuilder};
use std::io;
use std::os::unix::fs::{DirBuilderExt, symlink};
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

/// The key file in a snapshot dir (PR D10). `key::write_key` writes it
/// into the temp dir of a take, so it keeps this name after the rename.
pub(crate) const KEY_FILE: &str = "key.json";

/// A session id in the form that `SessionId` accepts. Every session id has
/// this length, so the run root check uses it for the longest path.
const ID_OF_MAX_LEN: &str = "00000000-0000-0000-0000-000000000000";

/// The pool size (decisions CH51, CH55). 80 GiB of host RAM over 2 GiB
/// per VM is 40 VMs, so 64 slots cover every VM that admission lets in
/// (item 45 C3, option 2).
pub const SLOTS: u16 = 64;

/// The mode of each dir that `RunRoot::link_vm` makes.
const DIR_MODE: u32 = 0o700;

/// One slot of the pool, below `SLOTS` (decisions CH51, CH55).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Slot(u16);

impl Slot {
    /// The last slot. Its dir name is the longest.
    pub const LAST: Self = Self(SLOTS - 1);

    /// The slot `number`, or `None` at or above `SLOTS`.
    #[must_use]
    pub fn new(number: u16) -> Option<Self> {
        (number < SLOTS).then_some(Self(number))
    }

    /// Every slot, from 0.
    pub fn all() -> impl Iterator<Item = Self> {
        (0..SLOTS).map(Self)
    }

    /// The slot number.
    #[must_use]
    pub fn number(self) -> u16 {
        self.0
    }
}

/// Why `RunRoot::link_vm` or `RunRoot::unlink_vm` failed.
#[derive(Debug)]
pub struct LinkError {
    /// The path that the failed step worked on.
    pub path: PathBuf,
    /// Why the step failed.
    pub source: io::Error,
}

impl fmt::Display for LinkError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "cannot link {}", self.path.display())
    }
}

impl std::error::Error for LinkError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// Wraps an IO error with the path of its step.
fn at(path: &Path) -> impl FnOnce(io::Error) -> LinkError + '_ {
    move |source| LinkError {
        path: path.to_owned(),
        source,
    }
}

/// Removes the file or link at `path`. A missing one is no error.
fn remove_if_present(path: &Path) -> Result<(), LinkError> {
    match fs::remove_file(path) {
        Err(source) if source.kind() != io::ErrorKind::NotFound => Err(at(path)(source)),
        _ => Ok(()),
    }
}

/// Removes every entry of the slot dir `dir` that is not a dir (decision
/// CH56 rule 1). The file type comes from the dir entry, so a link is
/// removed and never followed. An entry that is gone first is no error.
fn empty_slot(dir: &Path) -> Result<(), LinkError> {
    for entry in fs::read_dir(dir).map_err(at(dir))? {
        let entry = entry.map_err(at(dir))?;
        let path = entry.path();
        if !entry.file_type().map_err(at(&path))?.is_dir() {
            remove_if_present(&path)?;
        }
    }
    Ok(())
}

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
    // `store/<sha256>.identity`. Decision CH59 rule 2 adds
    // `sessions/<id>/serial.log`. A snapshot holds the disk path and the
    // serial log path, so a rename costs a cold boot of every VM (plan
    // risk 8) and an edit of the confine rules that name these paths.

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

    /// The directory that holds one directory per session. Retention reads
    /// it to find every snapshot (PR D13).
    #[must_use]
    pub fn sessions_dir(&self) -> PathBuf {
        self.0.join("sessions")
    }

    /// The directory of the session disk and the snapshots of `id`.
    #[must_use]
    pub fn session(&self, id: &SessionId) -> SessionDir {
        SessionDir(self.sessions_dir().join(id.as_str()))
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

    /// The temp file that the make of the session disk renames over the
    /// disk (PR D7). A crash can leave it, and the next make starts it
    /// again.
    #[must_use]
    pub fn disk_temp(&self) -> PathBuf {
        self.0.join("disk.ext4.tmp")
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

    /// The temp dir that a snapshot renames over the dir of `generation`
    /// (PR D9). A crash can leave it, and the next snapshot of that
    /// generation removes it first.
    #[must_use]
    pub fn snapshot_temp(&self, generation: SnapshotGeneration) -> PathBuf {
        self.snapshots().join(format!("{}.tmp", generation.0))
    }

    /// The key file of one snapshot generation (PR D10). It sits in the
    /// generation dir, so the names of decision CH39 rule 3 hold.
    #[must_use]
    pub fn snapshot_key(&self, generation: SnapshotGeneration) -> PathBuf {
        self.snapshot(generation).join(KEY_FILE)
    }

    /// The guest serial output of the session (gap G23, decision CH59
    /// rule 2). Each session has its own, so two VMs never share one file.
    /// `serial_log::SerialLog::fresh` makes it new before each launch,
    /// and it stays until the next start of the session. A drop of the
    /// session removes it with the session dir.
    #[must_use]
    pub fn serial_log(&self) -> PathBuf {
        self.0.join("serial.log")
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
        // The vmm and CH name each socket by its `vm/<id>` path. A slot
        // name is shorter, and the check counts it too.
        let dirs = [
            VmRunDir(root.vms_dir().join(ID_OF_MAX_LEN)),
            VmRunDir(root.slot(Slot::LAST)),
        ];
        let longest = dirs
            .iter()
            .flat_map(|vm| {
                HostPort::ALL
                    .iter()
                    .map(|&port| vm.host_listener(port))
                    .chain([vm.vsock_socket(), vm.api_socket()])
            })
            .chain([root.vmm_socket()])
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

    /// The directory of every `vm/<id>` link (PR D16).
    #[must_use]
    pub fn vms_dir(&self) -> PathBuf {
        self.0.join("vm")
    }

    /// The directory of every slot dir (PR D16).
    #[must_use]
    pub fn slots_dir(&self) -> PathBuf {
        self.0.join("slot")
    }

    /// The dir of `slot`, `slot/<k>` (decisions CH51, CH55). The slot
    /// profile `creche-ch-slot-<k>` allows only the sockets under it.
    #[must_use]
    pub fn slot(&self, slot: Slot) -> PathBuf {
        self.slots_dir().join(slot.number().to_string())
    }

    /// The run directory of the VM of `id` (gap G5). After `link_vm` it is
    /// a symlink to the dir of the slot of the VM.
    #[must_use]
    pub fn vm(&self, id: &SessionId) -> VmRunDir {
        VmRunDir(self.vms_dir().join(id.as_str()))
    }

    // Decision CH56 rule 1: a VM that takes a slot empties it first. A
    // slot that held another VM can keep its `vsock.sock_*` listeners. A leftover listener fails the next bind,
    // and the deny check refuses an extra listener (CH6-3).
    /// Makes `vm/<id>` a symlink to `../slot/<k>`, and makes the slot dir
    /// if it is not there (decisions CH51, CH55).
    ///
    /// Before the link, it empties the slot dir (decision CH56 rule 1). It
    /// removes every entry that is not a dir: each regular file, socket
    /// and link. It removes no dir. A free slot holds no live Cloud
    /// Hypervisor (CH19 (4)), so the files of the old VM are stale. The
    /// serial log is in the state root (decision CH59 rule 2), so the
    /// empty step never reaches it.
    ///
    /// A link that is there, to any target, is replaced in one rename, so
    /// a re-link moves the VM to another slot. A real dir at `vm/<id>`
    /// stays, and the rename fails. The run root is a tmpfs, so no step
    /// syncs.
    ///
    /// The caller runs one link of an id at a time. The link names the
    /// slot only. Which VM holds which slot is the caller's (PR L16b). The
    /// caller links a VM only to a free slot, since the link empties it.
    ///
    /// # Errors
    ///
    /// A `LinkError` for the first step that fails.
    pub fn link_vm(&self, id: &SessionId, slot: Slot) -> Result<VmRunDir, LinkError> {
        let dir = self.slot(slot);
        let vms = self.vms_dir();
        for path in [&dir, &vms] {
            DirBuilder::new()
                .recursive(true)
                .mode(DIR_MODE)
                .create(path)
                .map_err(at(path))?;
        }
        empty_slot(&dir)?;
        let vm = self.vm(id);
        let temp = vms.join(format!(".{id}.link"));
        remove_if_present(&temp)?;
        let target = Path::new("..").join("slot").join(slot.number().to_string());
        symlink(target, &temp).map_err(at(&temp))?;
        if let Err(source) = fs::rename(&temp, vm.path()) {
            let _ = fs::remove_file(&temp);
            return Err(at(vm.path())(source));
        }
        Ok(vm)
    }

    /// Removes the `vm/<id>` link. A missing link is no error. The slot
    /// dir and its files stay. Run it when the VM frees its slot, so that
    /// no later step of the VM reaches the files of the next VM in the
    /// slot.
    ///
    /// # Errors
    ///
    /// A `LinkError` when the removal fails.
    pub fn unlink_vm(&self, id: &SessionId) -> Result<(), LinkError> {
        remove_if_present(self.vm(id).path())
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
}

/// The systemd scope unit name of the VM of `id` (fact 76).
#[must_use]
pub fn scope_unit(id: &SessionId) -> String {
    format!("{SCOPE_PREFIX}{id}")
}
