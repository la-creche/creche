//! The session disk: one 16 GiB sparse ext4 file per session (decision
//! CH8-7). It holds `flue.db` and the workspace of the session (decision
//! CH1), and it stays until a delete (decision CH2).
//!
//! The vmm makes the disk at the first boot of the VM, never at create,
//! because Panda sends `session/new` on every page load (decision CH21-4).
//! The vmm formats it with the host `mke2fs`: ext4, with the root dir owned
//! by 0:0 (decision CH36 with gap G7, option A).
//!
//! The make goes to a temp file: `set_len`, then `mke2fs`, then a sync and a
//! rename over the disk path. So a disk path holds a formatted disk or
//! nothing, and a call that finds the disk never formats it again.

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};

use creche_wire::SessionId;

use crate::layout::StateRoot;

/// The apparent size of a session disk: 16 GiB (decision CH8-7).
pub const SESSION_DISK_BYTES: u64 = 16 << 30;

/// The mode of a session disk: read and write for the vmm user only.
const DISK_MODE: u32 = 0o600;

/// The session disk of one session. Only `SessionDisks::for_boot` makes
/// one, so the disk exists and holds an ext4 filesystem. The confine
/// track renders it as the `--disk` argument of Cloud Hypervisor (PR C7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionDisk(PathBuf);

impl SessionDisk {
    /// The disk file.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

/// Why no session disk is ready.
#[derive(Debug)]
pub enum DiskError {
    /// A make, a write, a sync or a rename failed.
    Io {
        /// The file or dir that the step failed on.
        path: PathBuf,
        /// Why the step failed.
        source: io::Error,
    },
    /// The disk path holds a dir, a link or another non-regular file.
    NotAFile(PathBuf),
    /// `mke2fs` ended with a failure.
    Format {
        /// The exit status of `mke2fs`.
        status: ExitStatus,
        /// What `mke2fs` wrote to stderr.
        stderr: String,
    },
}

impl fmt::Display for DiskError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, .. } => write!(formatter, "cannot make {}", path.display()),
            Self::NotAFile(path) => write!(formatter, "{} is no regular file", path.display()),
            Self::Format { status, stderr } => {
                write!(formatter, "mke2fs ended with {status}: {}", stderr.trim())
            }
        }
    }
}

impl std::error::Error for DiskError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::NotAFile(_) | Self::Format { .. } => None,
        }
    }
}

/// Maps an `io::Error` on `path` to a `DiskError`.
fn io_at(path: &Path) -> impl FnOnce(io::Error) -> DiskError {
    let path = path.to_owned();
    move |source| DiskError::Io { path, source }
}

/// The session disks under the state root.
#[derive(Debug, Clone)]
pub struct SessionDisks {
    root: StateRoot,
    mke2fs: PathBuf,
}

impl SessionDisks {
    /// The session disks of `root`. `mke2fs` is the host program that
    /// formats a new disk. The vmm config passes it in, so no path is
    /// compiled in (gap G7).
    #[must_use]
    pub fn new(root: &StateRoot, mke2fs: PathBuf) -> Self {
        Self {
            root: root.clone(),
            mke2fs,
        }
    }

    /// The disk of `id` for a boot. The first call makes and formats it.
    /// A later call finds it and returns it with no write. Call it at a
    /// boot only, never at create (decision CH21-4). The vmm boots one VM
    /// of a session at a time, so two calls never share a temp file.
    ///
    /// # Errors
    ///
    /// `NotAFile` when the disk path holds no regular file, `Format` when
    /// `mke2fs` fails, `Io` on a failed step. The disk path then holds no
    /// new file.
    pub fn for_boot(&self, id: &SessionId) -> Result<SessionDisk, DiskError> {
        let session = self.root.session(id);
        let disk = session.disk();
        match fs::symlink_metadata(&disk) {
            Ok(metadata) if metadata.is_file() => return Ok(SessionDisk(disk)),
            Ok(_) => return Err(DiskError::NotAFile(disk)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(io_at(&disk)(error)),
        }

        let dir = session.path();
        fs::create_dir_all(dir).map_err(io_at(dir))?;
        let temp = session.disk_temp();
        let made = self.make(&temp);
        if made.is_err() {
            let _ = fs::remove_file(&temp);
        }
        made?;
        fs::rename(&temp, &disk).map_err(io_at(&disk))?;
        sync_dir(dir).map_err(io_at(dir))?;
        if let Some(sessions) = dir.parent() {
            sync_dir(sessions).map_err(io_at(sessions))?;
        }
        Ok(SessionDisk(disk))
    }

    /// Makes a sparse file of `SESSION_DISK_BYTES` at `temp`, formats it
    /// and syncs it. A temp file that a crash left is made again.
    fn make(&self, temp: &Path) -> Result<(), DiskError> {
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(DISK_MODE)
            .open(temp)
            .map_err(io_at(temp))?;
        file.set_len(SESSION_DISK_BYTES).map_err(io_at(temp))?;
        drop(file);

        // `-F` formats a regular file with no question. `root_owner=0:0`
        // sets the root dir owner, which is otherwise the vmm user (gap
        // G7).
        let output = Command::new(&self.mke2fs)
            .args(["-q", "-F", "-t", "ext4", "-E", "root_owner=0:0", "--"])
            .arg(temp)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .map_err(io_at(&self.mke2fs))?;
        if !output.status.success() {
            return Err(DiskError::Format {
                status: output.status,
                stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            });
        }
        File::open(temp)
            .and_then(|file| file.sync_all())
            .map_err(io_at(temp))
    }
}

/// Syncs `dir`, so that a new or renamed entry in it lasts across a crash.
fn sync_dir(dir: &Path) -> io::Result<()> {
    File::open(dir)?.sync_all()
}
