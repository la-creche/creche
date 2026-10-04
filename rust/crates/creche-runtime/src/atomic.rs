//! A file write that a reader never sees half done.
//!
//! [`write()`] writes the bytes to a temporary file beside the target, syncs
//! that file and renames it onto the target. A reader gets the old file or
//! the new file. A process that stops in the middle leaves the old file.
//!
//! No function here is `async`. A write blocks, and a write that started must
//! end. Run it with `Tasks::spawn_blocking` and await the `Completion`
//! (`rust/AGENTS.md`, "The rules for a service", rule 2 and rule 7).
//!
//! The Python services hold three copies of this write:
//! `attendance/src/attendance/atomic.py`, `caregiver/src/caregiver/atomic.py`
//! and `chaperone/src/chaperone/faults.py`. The three differ in the name of
//! the temporary file and in what they sync. [`Write`] names each choice.
//!
//! Each function body here is a stub. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};

/// The mode of a file that [`write()`] or [`write_new`] makes.
///
/// The set is closed. A caller cannot ask for a mode that the group or each
/// user can write.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileMode {
    /// `0600`: only the owner reads and writes. A token file has this mode.
    Private,
    /// `0640`: the group also reads. A fault file has this mode.
    GroupRead,
    /// `0644`: each user reads.
    PublicRead,
}

/// The mode of a directory that [`ensure_dir`] makes.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DirMode {
    /// `0700`: only the owner enters.
    Private,
    /// `0750`: the group also reads and enters.
    Group,
    /// `2750`: as [`DirMode::Group`], and a new entry gets the group of the
    /// directory. The directory of a shared socket has this mode.
    SetgidGroup,
}

/// What [`write()`] does when the directory of the target is absent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Parents {
    /// Make each directory that is absent.
    Create,
    /// Refuse the write. The caller made the directory, with its own mode.
    MustExist,
}

/// Whether [`write()`] syncs the directory after the rename.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DirSync {
    /// Sync the directory. After a loss of power the new name is then on the
    /// disk. This is the strictest level, and the default of a port.
    Sync,
    /// Do not sync the directory. Use it only for a file that the service
    /// makes again at its next start.
    Skip,
}

/// How [`write()`] writes one file.
///
/// Each field is a decision that a Python copy makes without a name. A port
/// of a service states each one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Write {
    /// The mode of the new file.
    pub mode: FileMode,
    /// What to do when the directory is absent.
    pub parents: Parents,
    /// Whether to sync the directory after the rename.
    pub dir_sync: DirSync,
}

/// The step of a write that failed.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteStep {
    /// Make the directories above the target.
    MakeParents,
    /// Create the temporary file.
    CreateTemp,
    /// Write the bytes into the temporary file.
    WriteTemp,
    /// Sync the temporary file.
    SyncTemp,
    /// Set the mode of a file or of a directory.
    SetMode,
    /// Rename a file or a directory onto its target.
    Rename,
    /// Link the temporary file to a name that must not exist.
    Link,
    /// Sync the directory of the target.
    SyncDir,
    /// Remove an old copy, or the temporary file after the link.
    RemoveOld,
}

impl fmt::Display for WriteStep {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::MakeParents => "make the directories above it",
            Self::CreateTemp => "create the temporary file",
            Self::WriteTemp => "write the temporary file",
            Self::SyncTemp => "sync the temporary file",
            Self::SetMode => "set the mode",
            Self::Rename => "rename onto the target",
            Self::Link => "link the new name",
            Self::SyncDir => "sync the directory",
            Self::RemoveOld => "remove the old copy",
        })
    }
}

/// Why a write did not complete.
///
/// The error names the step, the path and the answer of the operating system.
/// It never holds a byte of the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteError {
    /// The step that failed.
    pub step: WriteStep,
    /// The path that the step was about.
    pub path: PathBuf,
    /// The kind of the error of the operating system.
    pub kind: io::ErrorKind,
    /// The text of the error, as `strerror` of Python gives it.
    pub os_text: String,
}

impl fmt::Display for WriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "cannot write {}: the step \"{}\" failed: {}",
            self.path.display(),
            self.step,
            self.os_text
        )
    }
}

impl Error for WriteError {}

/// Writes `bytes` to `path` through a temporary file, a sync and a rename.
///
/// A reader of `path` gets the old content or the new content, and never a
/// part of one. When a step fails, the function removes the temporary file,
/// and `path` keeps its old content.
///
/// # Errors
///
/// [`WriteError`] with the step that failed.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-files writes this body"
)]
pub fn write(path: &Path, bytes: &[u8], how: Write) -> Result<(), WriteError> {
    todo!()
}

/// Writes `bytes` to `path` only when no file has that name.
///
/// The function never replaces a file. Two writers of one name cannot both
/// succeed. The directory must exist.
///
/// # Errors
///
/// [`WriteError`] with the step that failed. A name that exists fails at
/// [`WriteStep::Link`], with the kind `AlreadyExists`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-files writes this body"
)]
pub fn write_new(path: &Path, bytes: &[u8], mode: FileMode) -> Result<(), WriteError> {
    todo!()
}

/// Puts the directory `staging` in the place of the directory `target`.
///
/// The caller fills `staging` first. A reader then finds the old tree or the
/// new tree at `target`.
///
/// # Errors
///
/// [`WriteError`] with the step that failed.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-files writes this body"
)]
pub fn replace_dir(staging: &Path, target: &Path) -> Result<(), WriteError> {
    todo!()
}

/// Makes the directory `path` with `mode`. A directory that exists gets the
/// mode.
///
/// # Errors
///
/// [`WriteError`] with the step that failed.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-files writes this body"
)]
pub fn ensure_dir(path: &Path, mode: DirMode) -> Result<(), WriteError> {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_error_names_the_step_the_path_and_the_answer_of_the_system() {
        let error = WriteError {
            step: WriteStep::Rename,
            path: PathBuf::from("/srv/state/faults/pep/chat.json"),
            kind: io::ErrorKind::PermissionDenied,
            os_text: String::from("Permission denied"),
        };

        assert_eq!(
            error.to_string(),
            "cannot write /srv/state/faults/pep/chat.json: the step \"rename onto the target\" \
             failed: Permission denied"
        );
    }

    #[test]
    fn each_step_has_its_own_text() {
        let steps = [
            WriteStep::MakeParents,
            WriteStep::CreateTemp,
            WriteStep::WriteTemp,
            WriteStep::SyncTemp,
            WriteStep::SetMode,
            WriteStep::Rename,
            WriteStep::Link,
            WriteStep::SyncDir,
            WriteStep::RemoveOld,
        ];
        let mut texts: Vec<String> = steps.iter().map(ToString::to_string).collect();
        texts.sort();
        texts.dedup();

        assert_eq!(texts.len(), steps.len());
    }
}
