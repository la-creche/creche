//! The key of a memory snapshot (decision CH7-6). A snapshot restores only
//! into the same Cloud Hypervisor, kernel, root disk and guest init that
//! wrote it. The key names those four parts:
//!
//! 1. The CH rev: the constant `CH_REV`, equal to the rev of the CH7-4
//!    build argv (decision CH35 rule 3). CH7-6 keys on the rev, not on a
//!    binary hash. The vmm checks `cloud-hypervisor --version` at its start
//!    (decision CH36 with gap G10, option A). A `--rev` build prints the
//!    crate version `v54.0.0`, not the commit (fact 78). So the check
//!    proves the release, and a phase 5 test ties `CH_REV` to the build
//!    argv (gap G10).
//! 2. The SHA-256 of the kernel, from its checked store copy (PR D6).
//! 3. The SHA-256 of the root disk, from its checked store copy (PR D6).
//! 4. `GUEST_INIT_PROTOCOL` of the wire crate. A port renumber bumps it
//!    (decision CH14).
//!
//! `write_key` puts the key in `snapshots/<generation>/key.json` after the
//! take. A crash before the write leaves a snapshot with no key, and the
//! next check drops it. `check_newest` reads the key of the newest
//! generation. A key that differs from the current key, or a key file that
//! does not read, drops the snapshot, and the VM cold-boots from its
//! session disk (decision CH7-6).
//!
//! Decision CH44 rule 5: a mismatch or an unreadable key drops every
//! generation of the session. This code removes them, the oldest first and
//! the newest last, and it reads no older key. The reason: only the newest
//! generation is a restore candidate. The session disk moved on past every
//! older one (decision CH19 (5)), so an older one must never become the
//! newest and restore. A crash during the drop leaves the newest one, and
//! the next check drops it again. While the newest key matches, retention
//! removes the older ones (PR D13).
//!
//! Caller duties (`AGENTS.md`, Known gaps): lifecycle runs `write_key`
//! after each `Snapshotter::take`, and it runs `check_cloud_hypervisor` at
//! the vmm start. No plan PR has either step yet.

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::time::Duration;

use creche_wire::GUEST_INIT_PROTOCOL;
use serde::{Deserialize, Serialize};

use crate::ch_remote::output_until;
use crate::images::CheckedImage;
use crate::layout::{SessionDir, SnapshotGeneration};
use crate::snapshot::generations;

/// The Cloud Hypervisor rev of the CH7-4 build argv: the signed v54.0 tag
/// (decision CH35 rule 3, fact 54).
pub const CH_REV: &str = "038ce5a72d761380a02f714a4eb4ca63e0ea073d";

/// What `cloud-hypervisor --version` prints for a build of `CH_REV`
/// (fact 78).
pub const CH_VERSION: &str = "v54.0.0";

/// The program name that clap may print before the version.
const CH_NAME: &str = "cloud-hypervisor";

/// The most bytes of a key file. A key is about 200 bytes.
const KEY_MAX: u64 = 4096;

/// The mode of a key file, as of each snapshot file (item 5.7).
const KEY_MODE: u32 = 0o600;

/// A `cloud-hypervisor` that reported `CH_VERSION`. Only
/// `check_cloud_hypervisor` makes one, so a key exists only after the
/// check (gap G10).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CheckedCh(());

/// Why the vmm refuses its `cloud-hypervisor` at start (gap G10).
#[derive(Debug)]
pub enum ChVersionError {
    /// A start, a wait or a read of the program failed.
    Io {
        /// The `cloud-hypervisor` program.
        program: PathBuf,
        /// Why the step failed.
        source: io::Error,
    },
    /// The program did not end before the timeout. The check killed it.
    Timeout(Duration),
    /// The program ended with a failure.
    Failed {
        /// The exit status.
        status: ExitStatus,
        /// What the program wrote to stderr.
        stderr: String,
    },
    /// The program reported another version than `CH_VERSION`.
    Mismatch(String),
}

impl fmt::Display for ChVersionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { program, .. } => write!(formatter, "cannot run {}", program.display()),
            Self::Timeout(timeout) => write!(
                formatter,
                "cloud-hypervisor --version did not end in {timeout:?}"
            ),
            Self::Failed { status, stderr } => write!(
                formatter,
                "cloud-hypervisor --version ended with {status}: {}",
                stderr.trim()
            ),
            Self::Mismatch(found) => write!(
                formatter,
                "cloud-hypervisor reports {found:?}, not {CH_VERSION}"
            ),
        }
    }
}

impl std::error::Error for ChVersionError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Timeout(_) | Self::Failed { .. } | Self::Mismatch(_) => None,
        }
    }
}

/// Runs `<program> --version` and accepts it only when stdout is
/// `CH_VERSION`, alone or after `cloud-hypervisor`. A `v54.0.0-dirty` or a
/// `v54.0` from a tag build fails. Gap G10 option A has the vmm run this at
/// its start and refuse to start on an error. No plan PR calls it yet
/// (`AGENTS.md`, Known gaps). The caller passes the timeout, as for
/// each `ch-remote` call (decision CH44 rule 3).
///
/// # Errors
///
/// `Io`, `Timeout`, `Failed` or `Mismatch`.
pub fn check_cloud_hypervisor(
    program: &Path,
    timeout: Duration,
) -> Result<CheckedCh, ChVersionError> {
    let io_error = |source| ChVersionError::Io {
        program: program.to_owned(),
        source,
    };
    let mut child = Command::new(program)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(io_error)?;
    let Some(output) = output_until(&mut child, timeout).map_err(io_error)? else {
        return Err(ChVersionError::Timeout(timeout));
    };
    if !output.status.success() {
        return Err(ChVersionError::Failed {
            status: output.status,
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        });
    }
    let text = String::from_utf8_lossy(&output.stdout);
    match text.split_whitespace().collect::<Vec<_>>().as_slice() {
        [version] | [CH_NAME, version] if *version == CH_VERSION => Ok(CheckedCh(())),
        _ => Err(ChVersionError::Mismatch(text.trim().to_owned())),
    }
}

/// The key file. Each field is text or a number, so a key from another
/// release reads and then differs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct KeyFile {
    ch_rev: String,
    kernel: String,
    root_disk: String,
    guest_init_protocol: u32,
}

/// The key of the VM that the vmm starts now (decision CH7-6).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotKey(KeyFile);

impl SnapshotKey {
    /// The key of a VM with the checked `kernel` and `root_disk`.
    #[must_use]
    pub fn current(_ch: CheckedCh, kernel: &CheckedImage, root_disk: &CheckedImage) -> Self {
        Self(KeyFile {
            ch_rev: CH_REV.to_owned(),
            kernel: kernel.digest().to_string(),
            root_disk: root_disk.digest().to_string(),
            guest_init_protocol: GUEST_INIT_PROTOCOL,
        })
    }
}

/// A failed write, read or remove under the snapshots of a session.
#[derive(Debug)]
pub struct KeyError {
    /// The file or dir that the step failed on.
    pub path: PathBuf,
    /// Why the step failed.
    pub source: io::Error,
}

impl fmt::Display for KeyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "cannot key the snapshot at {}",
            self.path.display()
        )
    }
}

impl std::error::Error for KeyError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// Maps an `io::Error` on `path` to a `KeyError`.
fn io_at(path: &Path) -> impl FnOnce(io::Error) -> KeyError {
    let path = path.to_owned();
    move |source| KeyError { path, source }
}

/// Writes `key` into the dir of `generation` through a temp file and a
/// rename, then syncs the dir. Lifecycle runs it right after each take:
/// a snapshot with no key drops at the next check (`AGENTS.md`, Known
/// gaps).
///
/// # Errors
///
/// A failed write, sync or rename. The next check then drops the
/// generation.
pub fn write_key(
    session: &SessionDir,
    generation: SnapshotGeneration,
    key: &SnapshotKey,
) -> Result<(), KeyError> {
    let path = session.snapshot_key(generation);
    let temp = path.with_extension("json.tmp");
    let text = serde_json::to_vec(&key.0).map_err(|error| io_at(&path)(error.into()))?;
    let written = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(KEY_MODE)
        .open(&temp)
        .and_then(|mut file| {
            file.write_all(&text)?;
            file.sync_all()
        });
    if let Err(error) = written {
        let _ = fs::remove_file(&temp);
        return Err(io_at(&temp)(error));
    }
    fs::rename(&temp, &path).map_err(io_at(&path))?;
    let dir = session.snapshot(generation);
    sync_dir(&dir).map_err(io_at(&dir))
}

/// A snapshot whose key equals the current key. Only `check_newest` makes
/// one, so a claim (PR D11) never skips the key check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyedSnapshot {
    generation: SnapshotGeneration,
    dir: PathBuf,
}

impl KeyedSnapshot {
    /// The generation of the snapshot.
    #[must_use]
    pub fn generation(&self) -> SnapshotGeneration {
        self.generation
    }

    /// The snapshot dir.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.dir
    }
}

/// Why `check_newest` dropped a generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DropReason {
    /// The key differs from the current key.
    Differs,
    /// The key file is absent, is no regular file, is too large or holds
    /// no key. A failed read is no drop reason: it gives a `KeyError`.
    Unreadable,
}

/// What `check_newest` found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KeyCheck {
    /// The session has no snapshot.
    NoSnapshot,
    /// The newest snapshot has the current key.
    Keyed(KeyedSnapshot),
    /// The newest snapshot did not have the current key. The check removed
    /// it and every older one, and the VM cold-boots.
    Dropped {
        /// The removed generation.
        generation: SnapshotGeneration,
        /// Why the check removed it.
        reason: DropReason,
    },
}

/// Compares the key of the newest generation of `session` with `current`.
/// A mismatch or an unreadable key removes every generation (decisions
/// CH7-6 and CH44 rule 5). Run it only while no VM of the session runs,
/// so that no VM maps a snapshot that it removes (fact 55).
///
/// # Errors
///
/// A failed read of `snapshots/` or of the key file, or a failed remove.
/// A failed read removes nothing, so the caller fails the start or tries
/// again, and a snapshot that may restore stays.
pub fn check_newest(session: &SessionDir, current: &SnapshotKey) -> Result<KeyCheck, KeyError> {
    let snapshots = session.snapshots();
    let mut older = generations(session).map_err(io_at(&snapshots))?;
    let Some(generation) = older.pop() else {
        return Ok(KeyCheck::NoSnapshot);
    };
    let reason = match read_key(&session.snapshot_key(generation))? {
        Some(found) if found == current.0 => {
            return Ok(KeyCheck::Keyed(KeyedSnapshot {
                generation,
                dir: session.snapshot(generation),
            }));
        }
        Some(_) => DropReason::Differs,
        None => DropReason::Unreadable,
    };
    for each in older {
        remove_generation(session, each)?;
    }
    remove_generation(session, generation)?;
    Ok(KeyCheck::Dropped { generation, reason })
}

/// The key in `path`. `None` when the file is absent, is no regular file,
/// is too large or holds no key: each one drops the snapshot. Any other
/// failure, such as `EMFILE` or `EIO`, may pass, so it is an error and
/// drops nothing.
fn read_key(path: &Path) -> Result<Option<KeyFile>, KeyError> {
    let absent = |error: &io::Error| error.kind() == io::ErrorKind::NotFound;
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(error) if absent(&error) => return Ok(None),
        Err(error) => return Err(io_at(path)(error)),
    };
    if !meta.is_file() {
        return Ok(None);
    }
    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) if absent(&error) => return Ok(None),
        Err(error) => return Err(io_at(path)(error)),
    };
    let mut text = Vec::new();
    file.take(KEY_MAX.saturating_add(1))
        .read_to_end(&mut text)
        .map_err(io_at(path))?;
    if u64::try_from(text.len()).map_or(true, |len| len > KEY_MAX) {
        return Ok(None);
    }
    Ok(serde_json::from_slice(&text).ok())
}

/// Removes the key file first, so a crash during the remove leaves a dir
/// that the next check drops again. Then removes the dir.
fn remove_generation(session: &SessionDir, generation: SnapshotGeneration) -> Result<(), KeyError> {
    let key = session.snapshot_key(generation);
    if fs::symlink_metadata(&key).is_ok_and(|meta| !meta.is_dir()) {
        fs::remove_file(&key).map_err(io_at(&key))?;
    }
    let dir = session.snapshot(generation);
    sync_dir(&dir).map_err(io_at(&dir))?;
    fs::remove_dir_all(&dir).map_err(io_at(&dir))?;
    let snapshots = session.snapshots();
    sync_dir(&snapshots).map_err(io_at(&snapshots))
}

/// Syncs `dir`, so that a new, renamed or removed entry in it lasts across
/// a crash.
fn sync_dir(dir: &Path) -> io::Result<()> {
    File::open(dir)?.sync_all()
}
