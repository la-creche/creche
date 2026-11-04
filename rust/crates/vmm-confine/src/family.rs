//! The read of a family's `config/` and of its VM shape file (decisions
//! CH5b, CH23 (3), CH36 G6, CH43 rule 3).
//!
//! caregiver is the only writer of `config/` (decision CH5b). It builds a
//! whole revision in `config.tmp`, renames `config` to `config.old`,
//! renames `config.tmp` to `config`, then removes `config.old`
//! (`atomic.py` `atomic_replace_dir`). So a reader can find no `config/`
//! for a short window, or find files gone from a handle it already holds.
//! Each read here goes through one directory handle:
//!
//! 1. Open the family dir and `openat` its `config/` with `O_NOFOLLOW`.
//! 2. Read through the `config/` handle.
//! 3. Compare the device and inode of the handle with
//!    `fstatat(family_dir, "config")`. A mismatch is a swap, so the read
//!    starts again.
//! 4. An absent `config/` is the rename window, so the read starts again.
//!    After `READ_TRIES` tries it is `FamilyError::Unknown`, which create
//!    answers with `unknown_family` (decision CH33 rule 3).
//!
//! `FamilyRoot` has no default path. Lifecycle passes it from the vmm
//! config (decision CH36 G5). caregiver writes `shape.json` only from the
//! caregiver PR of the cut (decision CH45 rule 2), so on a live host before
//! the cut each shape read gives `NotValid(Absent)`. A proof uses a scratch
//! family root with a hand-written `shape.json` (decision CH47 rule 2).
//!
//! CONTRACT-QUESTION (decisions CH5b, CH23 (3)): no row sets the number of
//! tries, the pause between two tries or the size cap of `shape.json`.
//! Here they are `READ_TRIES` (5), `RETRY_PAUSE` (10 ms) and `SHAPE_CAP`
//! (4 KiB). AGENTS.md lists this in Known gaps.

use std::fs::File;
use std::io::{self, Read};
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::path::{Component, Path, PathBuf};
use std::thread;
use std::time::Duration;

use creche_vmm_store::layout::RootError;
use creche_wire::{FAMILY_SHAPE_FILE, FamilyName, FamilyShape};
use rustix::fs::{AtFlags, FileType, Mode, OFlags, Stat};
use rustix::io::Errno;

/// The name of the config dir in each family dir (contract 01 §6.1).
pub const CONFIG_DIR: &str = "config";

/// The most reads of one request before it gives up.
pub const READ_TRIES: u32 = 5;

/// The pause before each try after the first.
pub const RETRY_PAUSE: Duration = Duration::from_millis(10);

/// The most bytes of `shape.json`. The file holds three short fields.
pub const SHAPE_CAP: u64 = 4096;

/// The flags of each directory open. `O_NOFOLLOW` refuses a symlink as the
/// last part of the path (decision CH23 (3)).
const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

/// Why `shape.json` is not a family VM shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShapeFault {
    /// `config/` has no `shape.json`.
    Absent,
    /// `shape.json` is a symlink or not a regular file.
    NotFile,
    /// `shape.json` has more than `SHAPE_CAP` bytes.
    TooLarge,
    /// The bytes are not one `FamilyShape`.
    NotShape,
}

/// Why a read of a family's `config/` failed.
#[derive(Debug)]
pub enum FamilyError {
    /// The family has no dir or no `config/` dir (decision CH33 rule 3).
    Unknown,
    /// The family dir or its `config/` is a symlink or not a dir.
    NotDir,
    /// `config/` holds no valid `shape.json`.
    NotValid(ShapeFault),
    /// caregiver swapped `config/` during each of the `READ_TRIES` reads.
    Swapped,
    /// Any other error of the file system.
    Io(io::Error),
}

impl From<Errno> for FamilyError {
    fn from(errno: Errno) -> Self {
        Self::Io(errno.into())
    }
}

/// caregiver's families dir: one dir per family, each with its `config/`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FamilyRoot(PathBuf);

impl FamilyRoot {
    /// Checks `path` and makes the family root.
    ///
    /// # Errors
    ///
    /// `NotAbsolute` or `ParentComponent` when the path breaks that rule.
    pub fn new(path: PathBuf) -> Result<Self, RootError> {
        if !path.is_absolute() {
            return Err(RootError::NotAbsolute);
        }
        if path.components().any(|part| part == Component::ParentDir) {
            return Err(RootError::ParentComponent);
        }
        Ok(Self(path))
    }

    /// The root itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }

    /// Whether `family` has a `config/` dir. Create calls it (decision
    /// CH33 rule 3).
    ///
    /// # Errors
    ///
    /// `Unknown` when it has none, else the error of `with_config`.
    pub fn exists(&self, family: &FamilyName) -> Result<(), FamilyError> {
        self.with_config(family, |_| Ok(()))
    }

    /// The VM shape of `family`, from `config/shape.json` (decisions CH36
    /// G6, CH43 rule 3, CH8-5).
    ///
    /// # Errors
    ///
    /// `NotValid` when the file is absent or bad, else the error of
    /// `with_config`.
    pub fn read_shape(&self, family: &FamilyName) -> Result<FamilyShape, FamilyError> {
        let bytes = self.with_config(family, read_shape_file)?;
        serde_json::from_slice(&bytes).map_err(|_| FamilyError::NotValid(ShapeFault::NotShape))
    }

    /// Runs `read` on one handle of the `config/` of `family`, and returns
    /// its result only when `config/` is still the dir of that handle
    /// afterwards. A swap or an absent `config/` starts the read again, at
    /// most `READ_TRIES` times (decisions CH5b, CH23 (3)).
    ///
    /// # Errors
    ///
    /// `Unknown` when the family dir or its `config/` is absent, `NotDir`
    /// for a symlink or a file, `Swapped` when every try saw a swap, `Io`
    /// for any other error, else the error of `read`.
    pub fn with_config<T>(
        &self,
        family: &FamilyName,
        mut read: impl FnMut(BorrowedFd<'_>) -> Result<T, FamilyError>,
    ) -> Result<T, FamilyError> {
        let dir = open_dir(rustix::fs::open(
            self.0.join(family.as_str()),
            DIR_FLAGS,
            Mode::empty(),
        ))?;
        let mut last = FamilyError::Unknown;
        for attempt in 0..READ_TRIES {
            if attempt > 0 {
                thread::sleep(RETRY_PAUSE);
            }
            let config = match open_dir(rustix::fs::openat(
                &dir,
                CONFIG_DIR,
                DIR_FLAGS,
                Mode::empty(),
            )) {
                Err(FamilyError::Unknown) => {
                    last = FamilyError::Unknown;
                    continue;
                }
                other => other?,
            };
            let held = rustix::fs::fstat(&config)?;
            let result = read(config.as_fd());
            match rustix::fs::statat(&dir, CONFIG_DIR, AtFlags::SYMLINK_NOFOLLOW) {
                Ok(now) if same_file(&held, &now) => return result,
                Ok(_) | Err(Errno::NOENT) => last = FamilyError::Swapped,
                Err(errno) => return Err(errno.into()),
            }
        }
        Err(last)
    }
}

/// The dir of an open, or why it is not one. `ELOOP` is the refusal of
/// `O_NOFOLLOW` and `ENOTDIR` the refusal of `O_DIRECTORY`.
fn open_dir(opened: Result<OwnedFd, Errno>) -> Result<OwnedFd, FamilyError> {
    match opened {
        Ok(dir) => Ok(dir),
        Err(Errno::NOENT) => Err(FamilyError::Unknown),
        Err(Errno::LOOP | Errno::NOTDIR) => Err(FamilyError::NotDir),
        Err(errno) => Err(errno.into()),
    }
}

/// Whether two stats name one file: the same device and inode.
fn same_file(one: &Stat, other: &Stat) -> bool {
    one.st_dev == other.st_dev && one.st_ino == other.st_ino
}

/// The bytes of `shape.json` in the dir `config`, at most `SHAPE_CAP`.
fn read_shape_file(config: BorrowedFd<'_>) -> Result<Vec<u8>, FamilyError> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK;
    let fd = match rustix::fs::openat(config, FAMILY_SHAPE_FILE, flags, Mode::empty()) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Err(FamilyError::NotValid(ShapeFault::Absent)),
        Err(Errno::LOOP) => return Err(FamilyError::NotValid(ShapeFault::NotFile)),
        Err(errno) => return Err(errno.into()),
    };
    let stat = rustix::fs::fstat(&fd)?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Err(FamilyError::NotValid(ShapeFault::NotFile));
    }
    let mut bytes = Vec::new();
    File::from(fd)
        .take(SHAPE_CAP + 1)
        .read_to_end(&mut bytes)
        .map_err(FamilyError::Io)?;
    if u64::try_from(bytes.len()).map_or(true, |len| len > SHAPE_CAP) {
        return Err(FamilyError::NotValid(ShapeFault::TooLarge));
    }
    Ok(bytes)
}
