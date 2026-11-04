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
//!
//! A claim fails closed. When the make or the sync of the mark fails, the
//! snapshot has no durable mark, so a later start could restore it onto a
//! session disk that moved on. So the claim retires the generation: it
//! renames `snapshots/<n>` to `snapshots/<n>.retired`, which no generation
//! list counts, and syncs `snapshots/`. Then the caller cold-boots. When
//! the retire fails too, the caller fails the start and does not
//! cold-boot.
//!
//! Decision CH44 rule 6: a failed claim retires the generation, and the
//! caller cold-boots. A failed retire fails the start. This keeps CH19 (5),
//! a single use. The name `<n>.retired` is not in the CH39 rule 3 names.
//! D13 removes a leftover one.

use std::fmt;
use std::fs::{self, File, OpenOptions};
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

/// Why a claim gave no `ClaimedSnapshot` and no `None`: the make or the
/// sync of the consumed mark failed.
#[derive(Debug)]
pub enum ClaimError {
    /// The claim retired the generation, so it never restores. The caller
    /// cold-boots.
    Retired {
        /// The file or dir that the mark step failed on.
        path: PathBuf,
        /// Why the mark step failed.
        source: io::Error,
    },
    /// The retire failed too, so the snapshot may restore at a later
    /// start. The caller fails the start and does not cold-boot.
    Stuck {
        /// The file or dir that the mark step failed on.
        path: PathBuf,
        /// Why the mark step failed.
        source: io::Error,
        /// Why the retire failed.
        retire: io::Error,
    },
}

impl fmt::Display for ClaimError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Retired { path, .. } => write!(
                formatter,
                "cannot mark the snapshot at {} as consumed, so it is retired",
                path.display()
            ),
            Self::Stuck { path, retire, .. } => write!(
                formatter,
                "cannot mark the snapshot at {} as consumed or retire it: {retire}",
                path.display()
            ),
        }
    }
}

impl std::error::Error for ClaimError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Retired { source, .. } | Self::Stuck { source, .. } => Some(source),
        }
    }
}

/// Makes the consumed mark of `keyed` and returns the snapshot to restore.
/// `None` when the mark exists, whatever its kind: the snapshot was
/// claimed before, and the VM cold-boots. Run it before Cloud Hypervisor
/// starts, so a crash after it gives a cold boot (decision CH19 (5)).
///
/// # Errors
///
/// A failed make of the mark, or a failed sync of the generation dir.
/// `Retired` when the claim then retired the generation: the caller
/// cold-boots. `Stuck` when the retire failed too: the caller fails the
/// start, because a later start could restore the snapshot.
pub fn claim(keyed: KeyedSnapshot) -> Result<Option<ClaimedSnapshot>, ClaimError> {
    let dir = keyed.dir().to_owned();
    let mark = dir.join(CONSUMED);
    let made = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(MARK_MODE)
        .open(&mark);
    let failed = match made {
        Ok(_) => File::open(&dir)
            .and_then(|handle| handle.sync_all())
            .err()
            .map(|source| (dir.clone(), source)),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => return Ok(None),
        Err(source) => Some((mark, source)),
    };
    if let Some((path, source)) = failed {
        return Err(match retire(&dir) {
            Ok(()) => ClaimError::Retired { path, source },
            Err(retire) => ClaimError::Stuck {
                path,
                source,
                retire,
            },
        });
    }
    Ok(Some(ClaimedSnapshot {
        generation: keyed.generation(),
        dir,
    }))
}

/// Renames the generation `dir` to `<n>.retired` and syncs `snapshots/`,
/// so no later start restores it. Then it tries to remove the retired dir.
/// A leftover one is no generation, and D13 removes it.
fn retire(dir: &Path) -> io::Result<()> {
    let retired = dir.with_extension("retired");
    let _ = fs::remove_dir_all(&retired);
    fs::rename(dir, &retired)?;
    let snapshots = dir.parent().ok_or(io::ErrorKind::NotFound)?;
    File::open(snapshots)?.sync_all()?;
    let _ = fs::remove_dir_all(&retired);
    Ok(())
}
