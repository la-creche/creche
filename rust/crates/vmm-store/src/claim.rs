//! The single use of a snapshot (decision CH19 (5)). Cloud Hypervisor
//! restores one snapshot as often as asked (fact 74), so the vmm enforces
//! the single use itself.
//!
//! `claim` takes a `KeyedSnapshot`, which only the key check makes (PR
//! D10). It makes the mark `snapshots/<generation>/consumed` and then
//! returns a `ClaimedSnapshot`. The vmm restores only from a
//! `ClaimedSnapshot`, and only `claim` makes one. The type is not `Clone`,
//! so one claim gives one restore. A snapshot that has the mark gives no
//! `ClaimedSnapshot`, and the VM cold-boots from its session disk.
//!
//! The vmm claims before Cloud Hypervisor starts. A crash between the
//! claim and the restore leaves the mark, so the next start cold-boots.
//! The session disk is then the only state, as after any stop without a
//! fresh snapshot (decision CH19 (5)).
//!
//! The mark is an empty file, made with `O_CREAT | O_EXCL`. That make is
//! atomic. Unlike a rename, it fails when the mark exists, so of two claims
//! of one snapshot at most one gets a `ClaimedSnapshot`. The mark sits in
//! the generation dir, beside `key.json`, so the names of decision CH39
//! rule 3 hold.
//!
//! A claim removes nothing. A VM restored from the snapshot maps its files
//! while it runs (fact 55), so the claimed generation stays until a newer
//! one replaces it and retention removes it (PR D13).

use std::fmt;
use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use crate::key::KeyedSnapshot;
use crate::layout::SnapshotGeneration;

/// The name of the consumed mark in a generation dir.
pub const CONSUMED: &str = "consumed";

/// The mode of the mark, as of each snapshot file (item 5.7).
const MARK_MODE: u32 = 0o600;

/// A snapshot that the vmm may restore once. Only `claim` makes one, and
/// it is not `Clone` (decision CH19 (5)).
#[derive(Debug, PartialEq, Eq)]
pub struct ClaimedSnapshot {
    generation: SnapshotGeneration,
    dir: PathBuf,
}

impl ClaimedSnapshot {
    /// The generation of the snapshot.
    #[must_use]
    pub fn generation(&self) -> SnapshotGeneration {
        self.generation
    }

    /// The snapshot dir, the source of the restore.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.dir
    }
}

/// A failed make or sync of the consumed mark.
#[derive(Debug)]
pub struct ClaimError {
    /// The file or dir that the step failed on.
    pub path: PathBuf,
    /// Why the step failed.
    pub source: io::Error,
}

impl fmt::Display for ClaimError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "cannot mark the snapshot at {} as consumed",
            self.path.display()
        )
    }
}

impl std::error::Error for ClaimError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// Makes the consumed mark of `keyed` and returns the snapshot to restore.
/// `None` when the mark exists, whatever its kind: the snapshot was
/// claimed before, and the VM cold-boots. Run it before Cloud Hypervisor
/// starts, so a crash after it gives a cold boot (decision CH19 (5)).
///
/// # Errors
///
/// A failed make of the mark, or a failed sync of the generation dir. The
/// mark may then exist. The caller cold-boots and does not restore.
pub fn claim(keyed: KeyedSnapshot) -> Result<Option<ClaimedSnapshot>, ClaimError> {
    let dir = keyed.dir().to_owned();
    let mark = dir.join(CONSUMED);
    let made = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(MARK_MODE)
        .open(&mark);
    match made {
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => return Ok(None),
        Err(source) => return Err(ClaimError { path: mark, source }),
    }
    if let Err(source) = File::open(&dir).and_then(|handle| handle.sync_all()) {
        return Err(ClaimError { path: dir, source });
    }
    Ok(Some(ClaimedSnapshot {
        generation: keyed.generation(),
        dir,
    }))
}
