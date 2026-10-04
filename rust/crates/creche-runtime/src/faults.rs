//! The fault file of contract 05 §3.3.1, onto the disk.
//!
//! `attendance` and the chaperone each publish the open faults of a family
//! as one file. `caregiver` reads the files and puts the faults into the
//! status document. `creche_contracts::status::fault_file::FaultFile` holds
//! the content and its bytes. This module holds the write: the path, the mode
//! of the file and the mode of its directory.
//!
//! The rules of an open fault stay in each service: when a fault opens, its
//! `since` and when the service writes the file again.
//!
//! The Python writers are `attendance/src/attendance/faults.py:128-138` and
//! `chaperone/src/chaperone/faults.py:118-133`.
//!
//! The body of [`publish`] is a stub. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::error::Error;
use std::fmt;

use creche_contracts::status::fault_file::FaultFile;

use crate::atomic::WriteError;
use crate::layout::StateRoot;

/// Why a fault file is not on the disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PublishError {
    /// The source of the file is `caregiver`, which writes no fault file.
    SourceWritesNoFile,
    /// The write failed.
    Write(WriteError),
}

impl fmt::Display for PublishError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SourceWritesNoFile => {
                f.write_str("the source of the faults writes no fault file")
            }
            Self::Write(error) => write!(f, "{error}"),
        }
    }
}

impl Error for PublishError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::SourceWritesNoFile => None,
            Self::Write(error) => Some(error),
        }
    }
}

/// Writes `file` to its path under `root`.
///
/// A reader gets the old file or the new file. The function blocks: call it
/// through `Tasks::spawn_blocking`.
///
/// # Errors
///
/// [`PublishError::SourceWritesNoFile`] for a file of `caregiver`, and
/// [`PublishError::Write`] when the write fails.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-token-faults writes this body"
)]
pub fn publish(root: &StateRoot, file: &FaultFile) -> Result<(), PublishError> {
    todo!()
}

#[cfg(test)]
mod tests {
    use std::io;
    use std::path::PathBuf;

    use super::*;
    use crate::atomic::WriteStep;

    #[test]
    fn an_error_names_its_cause() {
        let write = WriteError {
            step: WriteStep::SyncTemp,
            path: PathBuf::from("/srv/state/faults/pep/.chat.json.7.0.tmp"),
            kind: io::ErrorKind::StorageFull,
            os_text: String::from("No space left on device"),
        };
        let error = PublishError::Write(write.clone());

        assert_eq!(error.to_string(), write.to_string());
        assert!(error.source().is_some());
        assert_eq!(
            PublishError::SourceWritesNoFile.to_string(),
            "the source of the faults writes no fault file"
        );
        assert!(PublishError::SourceWritesNoFile.source().is_none());
    }
}
