//! Retention of digest-store images that no current image set or snapshot
//! key references.
//!
//! `collect` has two phases. The mark phase reads every snapshot key and
//! changes nothing. One failed or unexpected read cancels the collection.
//! The sweep phase considers only regular files whose names are image
//! digests. It checks each unmarked store pair before it removes the
//! identity and then the image.

use std::collections::HashSet;
use std::fmt;
use std::fs::{self, File};
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use creche_wire::SessionId;

use crate::images::{CheckError, ImageDigest, ImageStore};
use crate::key::stored_image_digests;
use crate::layout::StateRoot;

/// One image pair that the sweep removed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Removed {
    /// The digest that named the image.
    pub digest: ImageDigest,
    /// The allocated bytes of the image and its identity.
    pub allocated_bytes: u64,
}

/// One store candidate whose check or remove failed.
#[derive(Debug)]
pub enum Failure {
    /// The image and identity pair did not pass `ImageStore::check`.
    Check {
        /// The digest that named the image.
        digest: ImageDigest,
        /// Why the pair did not pass.
        source: CheckError,
    },
    /// A metadata read, remove or directory sync failed.
    Io {
        /// The file or directory of the failed step.
        path: PathBuf,
        /// Why the step failed.
        source: io::Error,
    },
}

/// What one collection removed and could not remove.
#[derive(Debug, Default)]
pub struct Collected {
    /// The removed pairs, in digest order.
    pub removed: Vec<Removed>,
    /// The allocated bytes of all removed pairs.
    pub reclaimed_bytes: u64,
    /// The unreferenced entries that stayed after a failed step.
    pub failures: Vec<Failure>,
}

/// A failed mark or store-directory scan. The collector then removes
/// nothing.
#[derive(Debug)]
pub struct ScanError {
    /// The file or directory of the failed scan.
    pub path: PathBuf,
    /// Why the scan failed.
    pub source: io::Error,
}

impl fmt::Display for ScanError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "cannot scan image retention at {}",
            self.path.display()
        )
    }
}

impl std::error::Error for ScanError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// Marks every current and snapshotted digest, then removes each checked
/// store pair that no mark names. `current` names the active kernel, root
/// images and guest init initramfs.
//
// CONTRACT-QUESTION: CH39 rule 3 and CH76 rule 8 do not specify removal
// of an unused digest-store copy. This implementation removes only after
// a complete strict snapshot scan. Online use also needs a stable current
// image set and no VM start. Changing that rule needs lifecycle reference
// tracking and a migration for any new store layout.
//
/// # Errors
///
/// A failed or unexpected snapshot scan, or a failed complete scan of the
/// store directory. The function then removes nothing.
pub fn collect(
    store: &ImageStore,
    state: &StateRoot,
    current: &HashSet<ImageDigest>,
) -> Result<Collected, ScanError> {
    let mut marked = current.clone();
    mark_snapshots(state, &mut marked)?;
    let candidates = candidates(store)?;
    Ok(remove_candidates(store, &marked, candidates))
}

/// Adds each digest of every strict snapshot key to `marked`.
fn mark_snapshots(state: &StateRoot, marked: &mut HashSet<ImageDigest>) -> Result<(), ScanError> {
    for session in session_dirs(state)? {
        let snapshots = session.join("snapshots");
        for generation in snapshot_dirs(&snapshots)? {
            let key = generation.join("key.json");
            marked.extend(stored_image_digests(&key).map_err(|error| ScanError {
                path: error.path,
                source: error.source,
            })?);
        }
    }
    Ok(())
}

/// The valid session directories, sorted. An unexpected entry cancels the
/// mark phase.
fn session_dirs(state: &StateRoot) -> Result<Vec<PathBuf>, ScanError> {
    let root = state.sessions_dir();
    let entries = match fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(source) => return Err(ScanError { path: root, source }),
    };
    let mut found = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|source| ScanError {
            path: root.clone(),
            source,
        })?;
        let path = entry.path();
        let name = entry.file_name();
        let valid_name = name
            .to_str()
            .and_then(|text| text.parse::<SessionId>().ok())
            .is_some();
        let is_dir = entry
            .file_type()
            .map_err(|source| ScanError {
                path: path.clone(),
                source,
            })?
            .is_dir();
        if !(valid_name && is_dir) {
            return Err(unexpected(
                &path,
                "an entry under sessions is no session directory",
            ));
        }
        found.push(path);
    }
    found.sort_unstable();
    Ok(found)
}

/// The numeric snapshot generation directories, sorted. A numeric temp or
/// retired directory is expected and ignored. Any other entry cancels the
/// mark phase.
fn snapshot_dirs(root: &Path) -> Result<Vec<PathBuf>, ScanError> {
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(source) => {
            return Err(ScanError {
                path: root.to_owned(),
                source,
            });
        }
    };
    let mut found = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|source| ScanError {
            path: root.to_owned(),
            source,
        })?;
        let path = entry.path();
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            return Err(unexpected(&path, "a snapshot entry name is not UTF-8"));
        };
        let is_dir = entry
            .file_type()
            .map_err(|source| ScanError {
                path: path.clone(),
                source,
            })?
            .is_dir();
        if is_dir && name.parse::<u64>().is_ok() {
            found.push(path);
            continue;
        }
        let expected_leftover = is_dir
            && [".tmp", ".retired"].iter().any(|suffix| {
                name.strip_suffix(suffix)
                    .is_some_and(|number| number.parse::<u64>().is_ok())
            });
        if !expected_leftover {
            return Err(unexpected(
                &path,
                "an entry under snapshots is not a generation",
            ));
        }
    }
    found.sort_unstable();
    Ok(found)
}

/// Every regular image file whose name is one digest, sorted. The complete
/// directory scan ends before the sweep starts.
fn candidates(store: &ImageStore) -> Result<Vec<ImageDigest>, ScanError> {
    let root = store.dir();
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(source) => {
            return Err(ScanError {
                path: root.to_owned(),
                source,
            });
        }
    };
    let mut found = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|source| ScanError {
            path: root.to_owned(),
            source,
        })?;
        let file_type = entry.file_type().map_err(|source| ScanError {
            path: entry.path(),
            source,
        })?;
        if !file_type.is_file() {
            continue;
        }
        let digest = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<ImageDigest>().ok());
        if let Some(digest) = digest {
            found.push(digest);
        }
    }
    found.sort_unstable();
    Ok(found)
}

/// Removes each unmarked candidate, identity first.
fn remove_candidates(
    store: &ImageStore,
    marked: &HashSet<ImageDigest>,
    candidates: Vec<ImageDigest>,
) -> Collected {
    let mut collected = Collected::default();
    for digest in candidates {
        if marked.contains(&digest) {
            continue;
        }
        match remove_one(store, digest) {
            Ok(removed) => {
                collected.reclaimed_bytes = collected
                    .reclaimed_bytes
                    .saturating_add(removed.allocated_bytes);
                collected.removed.push(removed);
            }
            Err(failure) => collected.failures.push(failure),
        }
    }
    collected
}

/// Checks and removes one pair. A failed step leaves the image, or leaves
/// it without an identity after the identity removal succeeded.
fn remove_one(store: &ImageStore, digest: ImageDigest) -> Result<Removed, Failure> {
    let image = store.path(&digest);
    let identity = store.identity_path(&digest);
    let identity_meta = fs::symlink_metadata(&identity).map_err(|source| Failure::Io {
        path: identity.clone(),
        source,
    })?;
    if !identity_meta.file_type().is_file() {
        return Err(Failure::Io {
            path: identity,
            source: io::Error::new(
                io::ErrorKind::InvalidData,
                "the image identity is no regular file",
            ),
        });
    }
    store
        .check(digest)
        .map_err(|source| Failure::Check { digest, source })?;
    let identity_blocks = fs::metadata(&identity)
        .map_err(|source| Failure::Io {
            path: identity.clone(),
            source,
        })?
        .blocks();
    let image_blocks = fs::metadata(&image)
        .map_err(|source| Failure::Io {
            path: image.clone(),
            source,
        })?
        .blocks();
    let allocated_bytes = image_blocks
        .saturating_add(identity_blocks)
        .saturating_mul(512);

    remove_and_sync(&identity, store.dir())?;
    remove_and_sync(&image, store.dir())?;
    Ok(Removed {
        digest,
        allocated_bytes,
    })
}

/// Removes one file and syncs its parent directory.
fn remove_and_sync(path: &Path, parent: &Path) -> Result<(), Failure> {
    fs::remove_file(path).map_err(|source| Failure::Io {
        path: path.to_owned(),
        source,
    })?;
    File::open(parent)
        .and_then(|dir| dir.sync_all())
        .map_err(|source| Failure::Io {
            path: parent.to_owned(),
            source,
        })
}

/// One conservative scan refusal.
fn unexpected(path: &Path, message: &str) -> ScanError {
    ScanError {
        path: path.to_owned(),
        source: io::Error::new(io::ErrorKind::InvalidData, message.to_owned()),
    }
}
