//! Retention of digest-store images that no current image set or snapshot
//! key references.
//!
//! `collect` has two phases. The mark phase reads every snapshot key and
//! changes nothing. One failed or unexpected read cancels the collection.
//! The sweep phase considers only regular files whose names are image
//! digests. It checks each unmarked store pair before it removes the
//! identity and then the image. It also removes the leftovers of the
//! store: the identity of an unmarked digest with no image, and each temp
//! file of an import or an identity write that ended early. Any other name
//! stays.
//!
//! `plan` runs the mark and the scan and removes nothing. It lists what
//! `collect` would remove, with no `ImageStore::check`, because a check
//! can record a new identity. `vmm gc` prints it as its dry run.

use std::collections::HashSet;
use std::fmt;
use std::fs::{self, File};
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use creche_wire::SessionId;

use crate::images::{CheckError, ImageDigest, ImageStore};
use crate::key::stored_image_digests;
use crate::layout::{SnapshotGeneration, StateRoot};

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

/// One leftover store file that the sweep removed: the identity of an
/// unmarked digest with no image, or a temp file of an import
/// (`<sha256>.tmp`) or of an identity write (`.<sha256>.identity.*.tmp`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Leftover {
    /// The removed file.
    pub path: PathBuf,
    /// Its allocated bytes.
    pub allocated_bytes: u64,
}

/// What one collection removed and could not remove. For a `plan`, what
/// `collect` would remove.
#[derive(Debug, Default)]
pub struct Collected {
    /// The removed pairs, in digest order.
    pub removed: Vec<Removed>,
    /// The removed leftovers, in name order.
    pub leftovers: Vec<Leftover>,
    /// The allocated bytes of all removed pairs and leftovers.
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
/// store pair that no mark names, and the leftovers of the module doc.
/// `current` names the active kernel, root images and guest init
/// initramfs. Run it under the image write gate, so no import, check or
/// start runs.
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
    mark_snapshots(state, &HashSet::new(), &mut marked)?;
    let scan = scan(store)?;
    Ok(remove_candidates(store, &marked, scan))
}

/// The mark and the scan of `collect` with no remove: what `collect` would
/// remove once snapshot retention removed each generation of `dropped`.
/// The mark skips those generations. A pair that `collect` would refuse
/// for no regular identity goes to `failures`. The plan runs no
/// `ImageStore::check`, so a pair that fails its check is listed too.
///
/// # Errors
///
/// As for `collect`. The plan then lists nothing.
pub fn plan(
    store: &ImageStore,
    state: &StateRoot,
    current: &HashSet<ImageDigest>,
    dropped: &HashSet<(SessionId, SnapshotGeneration)>,
) -> Result<Collected, ScanError> {
    let mut marked = current.clone();
    mark_snapshots(state, dropped, &mut marked)?;
    let scan = scan(store)?;
    Ok(list_candidates(store, &marked, scan))
}

/// Adds each digest of every strict snapshot key to `marked`, except the
/// keys of the generations in `skip`.
fn mark_snapshots(
    state: &StateRoot,
    skip: &HashSet<(SessionId, SnapshotGeneration)>,
    marked: &mut HashSet<ImageDigest>,
) -> Result<(), ScanError> {
    for (id, session) in session_dirs(state)? {
        let snapshots = session.join("snapshots");
        for (number, generation) in snapshot_dirs(&snapshots)? {
            if skip.contains(&(id.clone(), number)) {
                continue;
            }
            let key = generation.join("key.json");
            marked.extend(stored_image_digests(&key).map_err(|error| ScanError {
                path: error.path,
                source: error.source,
            })?);
        }
    }
    Ok(())
}

/// The valid session directories with their ids, sorted. An unexpected
/// entry cancels the mark phase.
fn session_dirs(state: &StateRoot) -> Result<Vec<(SessionId, PathBuf)>, ScanError> {
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
        let id = name
            .to_str()
            .and_then(|text| text.parse::<SessionId>().ok());
        let is_dir = entry
            .file_type()
            .map_err(|source| ScanError {
                path: path.clone(),
                source,
            })?
            .is_dir();
        let Some(id) = id.filter(|_| is_dir) else {
            return Err(unexpected(
                &path,
                "an entry under sessions is no session directory",
            ));
        };
        found.push((id, path));
    }
    found.sort_unstable();
    Ok(found)
}

/// The numeric snapshot generation directories with their numbers,
/// sorted. A numeric temp or retired directory is expected and ignored.
/// Any other entry cancels the mark phase.
fn snapshot_dirs(root: &Path) -> Result<Vec<(SnapshotGeneration, PathBuf)>, ScanError> {
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
        if is_dir && let Ok(number) = name.parse::<u64>() {
            found.push((SnapshotGeneration(number), path));
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

/// What one complete scan of the store directory found.
#[derive(Debug, Default)]
struct Scan {
    /// Every regular image file whose name is one digest, sorted.
    images: Vec<ImageDigest>,
    /// Every digest with a regular identity file and no image file, sorted.
    orphans: Vec<ImageDigest>,
    /// Every regular temp file of an import or an identity write, sorted.
    temps: Vec<PathBuf>,
}

/// One complete scan of the store directory. It ends before the sweep
/// starts.
fn scan(store: &ImageStore) -> Result<Scan, ScanError> {
    let root = store.dir();
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Scan::default()),
        Err(source) => {
            return Err(ScanError {
                path: root.to_owned(),
                source,
            });
        }
    };
    let mut found = Scan::default();
    let mut identities = Vec::new();
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
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        match StoreName::of(name) {
            Some(StoreName::Image(digest)) => found.images.push(digest),
            Some(StoreName::Identity(digest)) => identities.push(digest),
            Some(StoreName::Temp) => found.temps.push(entry.path()),
            None => {}
        }
    }
    found.images.sort_unstable();
    let images: HashSet<ImageDigest> = found.images.iter().copied().collect();
    found.orphans = identities
        .into_iter()
        .filter(|digest| !images.contains(digest))
        .collect();
    found.orphans.sort_unstable();
    found.temps.sort_unstable();
    Ok(found)
}

/// The kinds of store name that the sweep reads. Any other name stays.
enum StoreName {
    /// `<sha256>`.
    Image(ImageDigest),
    /// `<sha256>.identity`.
    Identity(ImageDigest),
    /// `<sha256>.tmp` of `ImageStore::import` or `put_made`, or
    /// `.<sha256>.identity.<pid>.<n>.tmp` of an identity write.
    Temp,
}

impl StoreName {
    fn of(name: &str) -> Option<Self> {
        let digest = |text: &str| text.parse::<ImageDigest>().ok();
        if let Some(found) = digest(name) {
            return Some(Self::Image(found));
        }
        if let Some(found) = name.strip_suffix(".identity").and_then(digest) {
            return Some(Self::Identity(found));
        }
        let stem = name.strip_suffix(".tmp")?;
        let import = digest(stem).is_some();
        let identity = stem
            .strip_prefix('.')
            .and_then(|rest| rest.split_once(".identity."))
            .is_some_and(|(found, _)| digest(found).is_some());
        (import || identity).then_some(Self::Temp)
    }
}

/// Removes each unmarked image pair, each unmarked orphan identity and
/// each temp file.
fn remove_candidates(store: &ImageStore, marked: &HashSet<ImageDigest>, scan: Scan) -> Collected {
    let mut collected = Collected::default();
    for digest in scan.images {
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
    let orphans = scan
        .orphans
        .into_iter()
        .filter(|digest| !marked.contains(digest))
        .map(|digest| store.identity_path(&digest));
    for path in orphans.chain(scan.temps) {
        match remove_leftover(store, path) {
            Ok(leftover) => {
                collected.reclaimed_bytes = collected
                    .reclaimed_bytes
                    .saturating_add(leftover.allocated_bytes);
                collected.leftovers.push(leftover);
            }
            Err(failure) => collected.failures.push(failure),
        }
    }
    collected
}

/// Lists what `remove_candidates` would remove, with no check and no
/// remove.
fn list_candidates(store: &ImageStore, marked: &HashSet<ImageDigest>, scan: Scan) -> Collected {
    let mut collected = Collected::default();
    for digest in scan.images {
        if marked.contains(&digest) {
            continue;
        }
        match measure_pair(store, digest) {
            Ok(removed) => {
                collected.reclaimed_bytes = collected
                    .reclaimed_bytes
                    .saturating_add(removed.allocated_bytes);
                collected.removed.push(removed);
            }
            Err(failure) => collected.failures.push(failure),
        }
    }
    let orphans = scan
        .orphans
        .into_iter()
        .filter(|digest| !marked.contains(digest))
        .map(|digest| store.identity_path(&digest));
    for path in orphans.chain(scan.temps) {
        match allocated(&path) {
            Ok(allocated_bytes) => {
                collected.reclaimed_bytes =
                    collected.reclaimed_bytes.saturating_add(allocated_bytes);
                collected.leftovers.push(Leftover {
                    path,
                    allocated_bytes,
                });
            }
            Err(failure) => collected.failures.push(failure),
        }
    }
    collected
}

/// Checks that `digest` has a regular identity, then gives the allocated
/// bytes of the pair. A pair with no regular identity is the failure that
/// `remove_one` gives.
fn measure_pair(store: &ImageStore, digest: ImageDigest) -> Result<Removed, Failure> {
    let identity = store.identity_path(&digest);
    regular_identity(&identity)?;
    let allocated_bytes = allocated(&store.path(&digest))?.saturating_add(allocated(&identity)?);
    Ok(Removed {
        digest,
        allocated_bytes,
    })
}

/// Fails unless `identity` is a regular file.
fn regular_identity(identity: &Path) -> Result<(), Failure> {
    let meta = fs::symlink_metadata(identity).map_err(|source| Failure::Io {
        path: identity.to_owned(),
        source,
    })?;
    if meta.file_type().is_file() {
        Ok(())
    } else {
        Err(Failure::Io {
            path: identity.to_owned(),
            source: io::Error::new(
                io::ErrorKind::InvalidData,
                "the image identity is no regular file",
            ),
        })
    }
}

/// The allocated bytes of the file at `path`, with no follow of a link.
fn allocated(path: &Path) -> Result<u64, Failure> {
    fs::symlink_metadata(path)
        .map(|meta| meta.blocks().saturating_mul(512))
        .map_err(|source| Failure::Io {
            path: path.to_owned(),
            source,
        })
}

/// Removes one leftover file. An orphan identity whose image came back
/// stays: the identity is then no leftover.
fn remove_leftover(store: &ImageStore, path: PathBuf) -> Result<Leftover, Failure> {
    let allocated_bytes = allocated(&path)?;
    let image = path
        .file_name()
        .and_then(|name| name.to_str())
        .and_then(|name| name.strip_suffix(".identity"))
        .and_then(|digest| digest.parse::<ImageDigest>().ok())
        .map(|digest| store.path(&digest));
    if let Some(image) = image {
        match fs::symlink_metadata(&image) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Ok(_) => {
                return Err(Failure::Io {
                    path: image,
                    source: io::Error::new(
                        io::ErrorKind::AlreadyExists,
                        "the image of an orphan identity is back",
                    ),
                });
            }
            Err(source) => {
                return Err(Failure::Io {
                    path: image,
                    source,
                });
            }
        }
    }
    remove_and_sync(&path, store.dir())?;
    Ok(Leftover {
        path,
        allocated_bytes,
    })
}

/// Checks and removes one pair. A failed step leaves the image, or leaves
/// it without an identity after the identity removal succeeded.
fn remove_one(store: &ImageStore, digest: ImageDigest) -> Result<Removed, Failure> {
    let image = store.path(&digest);
    let identity = store.identity_path(&digest);
    regular_identity(&identity)?;
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
