//! A memory snapshot of one VM. The vmm snapshots a VM that went idle, and
//! a recent session resumes from the snapshot (decision CH2).
//!
//! `Snapshotter::take` runs these steps in order:
//!
//! 1. Make `snapshots/<generation>.tmp` with mode 0700. The generation is
//!    one more than the newest one, so a take never writes into the dir of
//!    a snapshot that a VM restored from. A copy-on-write restore needs the
//!    snapshot files to stay unchanged while that VM runs (fact 55).
//! 2. `ch-remote pause`, then `ch-remote snapshot` into the temp dir. A
//!    pause takes about 4 ms (fact 72).
//! 3. `ch-remote shutdown-vmm`. A graceful end removes the sockets of
//!    Cloud Hypervisor (fact 75).
//! 4. Refuse an entry that is no regular file, and set each file to mode
//!    0600 (section 4 item 5.7: files 0600 in a 0700 dir).
//! 5. `fallocate --dig-holes` on `memory-ranges` (decision CH29). The file
//!    holds the whole guest RAM, zero pages included. About 5% of it is
//!    not zero, and the dig cuts 2 GiB to about 100 MB in about 1 s (fact
//!    72). On ext4 each snapshot writes the whole guest RAM again (fact
//!    63), so the dig runs after every snapshot. No compression, so no
//!    decompress step sits before a restore.
//! 6. Sync each file and the temp dir. Rename the temp dir to
//!    `snapshots/<generation>` and sync `snapshots/`.
//!
//! The result holds the allocated bytes (`st_blocks`), the measure of the
//! snapshot budget (decision CH36 with gap G13, PR D13).
//!
//! Item 5.7 also asks for a guest `sync` before each snapshot. This crate
//! has no link to the guest, so the caller syncs the guest through the
//! control link before `take` (`AGENTS.md`, Known gaps).
//!
//! CONTRACT-QUESTION: no row decides what happens to the VM when a step
//! fails. The narrowest reading: `take` removes the temp dir, resumes
//! nothing and stops nothing more. The VM stays as the failed step left it,
//! and the caller decides. After a failure past `shutdown-vmm` the VM is
//! gone, and the next start cold-boots (decision CH19 (5)). `AGENTS.md`,
//! Known gaps, holds the question.

use std::fmt;
use std::fs::{self, DirBuilder, File};
use std::io;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::time::Duration;

use crate::ch_remote::{ChRemote, ChRemoteError};
use crate::layout::{SessionDir, SnapshotGeneration, VmRunDir};

/// The guest RAM file that Cloud Hypervisor writes into a snapshot dir.
pub const MEMORY_RANGES: &str = "memory-ranges";

/// The mode of `snapshots/` and of each snapshot dir (item 5.7).
const DIR_MODE: u32 = 0o700;

/// The mode of each snapshot file (item 5.7).
const FILE_MODE: u32 = 0o600;

/// The timeout of each `ch-remote` call of a take. The caller sets each
/// one (`AGENTS.md`, Known gaps, PR D8).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SnapshotTimeouts {
    /// The timeout of `pause`.
    pub pause: Duration,
    /// The timeout of `snapshot`. A 16 GiB guest takes about 12 s (gap
    /// G16).
    pub snapshot: Duration,
    /// The timeout of `shutdown-vmm`.
    pub shutdown: Duration,
}

/// A snapshot that `take` renamed into place.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snapshot {
    /// The generation of the snapshot.
    pub generation: SnapshotGeneration,
    /// The snapshot dir.
    pub dir: PathBuf,
    /// The bytes that the files take on the host disk, from `st_blocks`.
    pub allocated_bytes: u64,
}

/// Why a take made no snapshot. The snapshot dir then does not exist.
#[derive(Debug)]
pub enum SnapshotError {
    /// A make, a read, a mode set, a sync or a rename failed.
    Io {
        /// The file or dir that the step failed on.
        path: PathBuf,
        /// Why the step failed.
        source: io::Error,
    },
    /// A call of `ch-remote` failed.
    Ch(ChRemoteError),
    /// The snapshot dir holds an entry that is no regular file.
    NotAFile(PathBuf),
    /// `fallocate` ended with a failure.
    Dig {
        /// The exit status of `fallocate`.
        status: ExitStatus,
        /// What `fallocate` wrote to stderr.
        stderr: String,
    },
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, .. } => write!(formatter, "cannot snapshot to {}", path.display()),
            Self::Ch(_) => formatter.write_str("a ch-remote call of the snapshot failed"),
            Self::NotAFile(path) => write!(formatter, "{} is no regular file", path.display()),
            Self::Dig { status, stderr } => {
                write!(
                    formatter,
                    "fallocate ended with {status}: {}",
                    stderr.trim()
                )
            }
        }
    }
}

impl std::error::Error for SnapshotError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Ch(source) => Some(source),
            Self::NotAFile(_) | Self::Dig { .. } => None,
        }
    }
}

/// Maps an `io::Error` on `path` to a `SnapshotError`.
fn io_at(path: &Path) -> impl FnOnce(io::Error) -> SnapshotError {
    let path = path.to_owned();
    move |source| SnapshotError::Io { path, source }
}

/// The finished snapshot generations of a session, oldest first. A name
/// counts only when it is a dir with a plain decimal name, so a temp dir
/// or `007` does not count.
///
/// # Errors
///
/// A failed read of `snapshots/`. An absent dir gives no generation.
pub fn generations(session: &SessionDir) -> io::Result<Vec<SnapshotGeneration>> {
    let entries = match fs::read_dir(session.snapshots()) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let mut found = Vec::new();
    for entry in entries {
        let entry = entry?;
        let name = entry.file_name();
        let number = name.to_str().and_then(|text| {
            let number: u64 = text.parse().ok()?;
            (number.to_string() == text).then_some(number)
        });
        if let Some(number) = number
            && entry.file_type()?.is_dir()
        {
            found.push(SnapshotGeneration(number));
        }
    }
    found.sort_unstable();
    Ok(found)
}

/// Takes the snapshots of the vmm.
#[derive(Debug, Clone)]
pub struct Snapshotter {
    ch: ChRemote,
    fallocate: PathBuf,
}

impl Snapshotter {
    /// `fallocate` is the host program that digs the holes (decision CH29).
    /// The caller passes its path, so no path is compiled in (the owner env
    /// rule: a host path becomes a key with a default). No config key and
    /// no exec rule name it yet (`AGENTS.md`, Known gaps).
    #[must_use]
    pub fn new(ch: ChRemote, fallocate: PathBuf) -> Self {
        Self { ch, fallocate }
    }

    /// Snapshots the VM of `vm` into a new generation of `session` and ends
    /// its Cloud Hypervisor process. The dig of a 16 GiB guest takes about
    /// 8 s (fact 72) and has no timeout. The vmm runs one take of a session
    /// at a time, so two takes never share a temp dir.
    ///
    /// # Errors
    ///
    /// `Ch` when a `ch-remote` call fails, `NotAFile` for an entry that is
    /// no regular file, `Dig` when `fallocate` fails, `Io` on a failed
    /// step. Then no snapshot dir exists for the new generation.
    pub fn take(
        &self,
        vm: &VmRunDir,
        session: &SessionDir,
        timeouts: SnapshotTimeouts,
    ) -> Result<Snapshot, SnapshotError> {
        let snapshots = session.snapshots();
        make_dir(&snapshots, true)?;
        let newest = generations(session).map_err(io_at(&snapshots))?.pop();
        let generation = SnapshotGeneration(newest.map_or(1, |last| last.0.saturating_add(1)));

        let temp = session.snapshot_temp(generation);
        match fs::remove_dir_all(&temp) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(io_at(&temp)(error)),
        }
        make_dir(&temp, false)?;
        let dir = session.snapshot(generation);
        let taken = self.write(vm, &temp, timeouts).and_then(|allocated_bytes| {
            fs::rename(&temp, &dir).map_err(io_at(&dir))?;
            Ok(allocated_bytes)
        });
        if taken.is_err() {
            let _ = fs::remove_dir_all(&temp);
        }
        let allocated_bytes = taken?;
        sync_dir(&snapshots).map_err(io_at(&snapshots))?;
        Ok(Snapshot {
            generation,
            dir,
            allocated_bytes,
        })
    }

    /// Steps 2 to 5 and the syncs of step 6. Returns the allocated bytes.
    fn write(
        &self,
        vm: &VmRunDir,
        temp: &Path,
        timeouts: SnapshotTimeouts,
    ) -> Result<u64, SnapshotError> {
        self.ch
            .pause(vm, timeouts.pause)
            .map_err(SnapshotError::Ch)?;
        self.ch
            .snapshot(vm, temp, timeouts.snapshot)
            .map_err(SnapshotError::Ch)?;
        self.ch
            .shutdown_vmm(vm, timeouts.shutdown)
            .map_err(SnapshotError::Ch)?;

        let mut files = Vec::new();
        for entry in fs::read_dir(temp).map_err(io_at(temp))? {
            let path = entry.map_err(io_at(temp))?.path();
            if !fs::symlink_metadata(&path).map_err(io_at(&path))?.is_file() {
                return Err(SnapshotError::NotAFile(path));
            }
            fs::set_permissions(&path, fs::Permissions::from_mode(FILE_MODE))
                .map_err(io_at(&path))?;
            files.push(path);
        }
        self.dig(&temp.join(MEMORY_RANGES))?;

        let mut allocated_bytes = 0_u64;
        for path in &files {
            let file = File::open(path).map_err(io_at(path))?;
            file.sync_all().map_err(io_at(path))?;
            let blocks = file.metadata().map_err(io_at(path))?.blocks();
            allocated_bytes = allocated_bytes.saturating_add(blocks.saturating_mul(512));
        }
        sync_dir(temp).map_err(io_at(temp))?;
        Ok(allocated_bytes)
    }

    /// Runs `fallocate --dig-holes -- <file>` (decision CH29).
    fn dig(&self, file: &Path) -> Result<(), SnapshotError> {
        let output = Command::new(&self.fallocate)
            .args(["--dig-holes", "--"])
            .arg(file)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .map_err(io_at(&self.fallocate))?;
        if !output.status.success() {
            return Err(SnapshotError::Dig {
                status: output.status,
                stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            });
        }
        Ok(())
    }
}

/// Makes `dir` with mode 0700 and sets that mode, which the umask may
/// have cut (item 5.7). `recursive` accepts a dir that exists.
fn make_dir(dir: &Path, recursive: bool) -> Result<(), SnapshotError> {
    DirBuilder::new()
        .recursive(recursive)
        .mode(DIR_MODE)
        .create(dir)
        .and_then(|()| fs::set_permissions(dir, fs::Permissions::from_mode(DIR_MODE)))
        .map_err(io_at(dir))
}

/// Syncs `dir`, so that a new or renamed entry in it lasts across a crash.
fn sync_dir(dir: &Path) -> io::Result<()> {
    File::open(dir)?.sync_all()
}
