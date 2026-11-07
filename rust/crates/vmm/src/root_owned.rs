//! The owner check of each file that the vmm trusts at its start: the
//! `digests.json` of the image root (decision CH57 rule 1) and the files
//! of `VMM_CH_PROGRAM`, `VMM_CH_REMOTE_PROGRAM` and
//! `VMM_GUEST_INIT_PROGRAM` (decisions CH62 rule 17, CH67 rule 1).
//!
//! Each file must be a regular file, not a symlink, owned by root, with
//! no group or other write bit. The vmm does not read its group list, so
//! any group write bit refuses. `open` looks at the path with no follow of
//! a symlink, opens it, and runs each check on the open file. So a caller
//! that reads the file reads the bytes that `open` checked. A failed check
//! refuses the start of the vmm, and `main.rs` exits 78 (owner item 86).

use std::fmt;
use std::fs::{self, File};
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

/// The mode bits that let a user other than the owner write a file.
const GROUP_OR_OTHER_WRITE: u32 = 0o022;

/// The uid that must own each checked file: root.
const ROOT_UID: u32 = 0;

/// Why the vmm does not trust a file.
#[derive(Debug)]
pub enum OwnerError {
    /// An open, a look or a read of the file failed. A missing file gives
    /// `NotFound`.
    Io(io::Error),
    /// The path holds a symlink, a dir or another file that is not a
    /// regular file.
    NotRegular,
    /// The file that the open found is not the file that the first look
    /// found, so the path changed between the two.
    Replaced,
    /// The file has an owner other than root.
    NotOwned {
        /// The uid of the owner of the file.
        uid: u32,
    },
    /// The group or other users may write the file.
    Writable {
        /// The permission bits of the file.
        mode: u32,
    },
}

impl fmt::Display for OwnerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "cannot be read: {error}"),
            Self::NotRegular => formatter.write_str("is not a regular file"),
            Self::Replaced => formatter.write_str("changed during the read"),
            Self::NotOwned { uid } => write!(formatter, "is owned by uid {uid}, not by root"),
            Self::Writable { mode } => {
                write!(formatter, "has mode {mode:04o}, so others may write it")
            }
        }
    }
}

impl std::error::Error for OwnerError {}

/// A program key whose file the vmm does not trust.
#[derive(Debug)]
pub struct ProgramError {
    /// The key, such as `VMM_CH_PROGRAM`.
    pub key: &'static str,
    /// Why the vmm does not trust the file.
    pub fault: OwnerError,
}

impl fmt::Display for ProgramError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: the program {}", self.key, self.fault)
    }
}

impl std::error::Error for ProgramError {}

/// Opens `path` after the checks of the module doc. Blocks.
///
/// # Errors
///
/// An `OwnerError`. Each one refuses the start of the vmm.
pub fn open(path: &Path) -> Result<File, OwnerError> {
    let first = fs::symlink_metadata(path).map_err(OwnerError::Io)?;
    if !first.file_type().is_file() {
        return Err(OwnerError::NotRegular);
    }
    let file = File::open(path).map_err(OwnerError::Io)?;
    let open = file.metadata().map_err(OwnerError::Io)?;
    if (open.dev(), open.ino()) != (first.dev(), first.ino()) || !open.file_type().is_file() {
        return Err(OwnerError::Replaced);
    }
    if open.uid() != ROOT_UID {
        return Err(OwnerError::NotOwned { uid: open.uid() });
    }
    if open.mode() & GROUP_OR_OTHER_WRITE != 0 {
        return Err(OwnerError::Writable {
            mode: open.mode() & 0o7777,
        });
    }
    Ok(file)
}

/// Opens the program of `key` at `path` after the checks of `open`.
/// Blocks.
///
/// # Errors
///
/// A `ProgramError` that names `key`. Each one refuses the start of the
/// vmm.
pub fn open_program(key: &'static str, path: &Path) -> Result<File, ProgramError> {
    open(path).map_err(|fault| ProgramError { key, fault })
}

/// Runs the checks of `open` on the program of `key` at `path`, for a
/// program that the vmm runs by its path. Blocks.
///
/// # Errors
///
/// A `ProgramError` that names `key`. Each one refuses the start of the
/// vmm.
pub fn check_program(key: &'static str, path: &Path) -> Result<(), ProgramError> {
    open_program(key, path).map(drop)
}
