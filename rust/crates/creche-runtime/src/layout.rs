//! The paths under the state root that more than one service uses.
//!
//! One service writes a file and another service reads it. The two must give
//! the path one name. [`StateRoot`] holds each such path one time. A path
//! that only one service uses stays in the crate of that service.
//!
//! Each path takes typed parts: a `FamilyName`, a `SandboxName`, a
//! [`FaultWriter`]. No function here takes a raw text, so no caller can make
//! a path that leaves the state root.
//!
//! The Python services hold two copies of these paths:
//! `attendance/src/attendance/paths.py` and
//! `caregiver/src/caregiver/paths.py`.
//!
//! The bodies of [`StateRoot`] are stubs. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::path::PathBuf;

use creche_contracts::config::DirPath;
use creche_contracts::ids::{FamilyName, SandboxName, WebhookName};
use creche_contracts::status::fault_file::FaultFile;
use creche_contracts::status::words::FaultSource;

/// A service that writes a fault file (contract 05 §3.3.1).
///
/// `attendance` and the chaperone each write one fault file for a family.
/// `caregiver` reads the files and writes none. `FaultSource` has `caregiver`
/// as its third member, so that type cannot name a fault directory. This type
/// holds only the two that can.
///
/// ```
/// use creche_contracts::status::words::FaultSource;
/// use creche_runtime::layout::FaultWriter;
///
/// assert_eq!(FaultSource::from(FaultWriter::Sessiond), FaultSource::Sessiond);
/// assert_eq!(FaultSource::from(FaultWriter::Pep), FaultSource::Pep);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FaultWriter {
    /// `attendance`. Its files are in `faults/sessiond/`.
    Sessiond,
    /// The chaperone. Its files are in `faults/pep/`.
    Pep,
}

impl FaultWriter {
    /// The writer of `file`. `None` for a file whose source is `caregiver`.
    ///
    /// `FaultFile::new` refuses that source, so `None` does not occur for a
    /// file that the code built. The function still gives it as a value: a
    /// later change to `FaultFile` must not become a panic here.
    #[must_use]
    pub fn of(file: &FaultFile) -> Option<Self> {
        match file.source() {
            FaultSource::Sessiond => Some(Self::Sessiond),
            FaultSource::Pep => Some(Self::Pep),
            FaultSource::Managerd => None,
        }
    }
}

impl From<FaultWriter> for FaultSource {
    fn from(writer: FaultWriter) -> Self {
        match writer {
            FaultWriter::Sessiond => Self::Sessiond,
            FaultWriter::Pep => Self::Pep,
        }
    }
}

/// The state root of the platform: the directory that holds the state of
/// each family.
///
/// Each function returns one path under the root. No function reads the
/// disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateRoot(());

impl StateRoot {
    /// The layout under the directory `root`.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn new(root: DirPath) -> Self {
        todo!()
    }

    /// The directory that holds one directory for each family.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn families_dir(&self) -> PathBuf {
        todo!()
    }

    /// The directory of one family.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn family_dir(&self, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The status document of one family (contract 05 §2).
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn status_file(&self, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The validation report of one family.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn validation_file(&self, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The directory that holds the credentials mount of one family.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn creds_dir(&self, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The directory that holds the config mount of one family.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn config_dir(&self, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The control directory of one sandbox. The function takes the family
    /// from the sandbox name, so the two cannot differ.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn control_dir(&self, sandbox: &SandboxName) -> PathBuf {
        todo!()
    }

    /// The grant file of one family (contract 04 §1).
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn grant_file(&self, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The directory that holds each fault file of one writer.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn fault_dir(&self, writer: FaultWriter) -> PathBuf {
        todo!()
    }

    /// The fault file of one writer for one family (contract 05 §3.3.1).
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn fault_file(&self, writer: FaultWriter, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The directory that holds the outcome records of one family.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn outcomes_dir(&self, family: &FamilyName) -> PathBuf {
        todo!()
    }

    /// The directory that holds the audit log of the chaperone.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn audit_dir(&self) -> PathBuf {
        todo!()
    }

    /// The directory that holds one token file for each principal of
    /// `attendance`.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn tokens_dir(&self) -> PathBuf {
        todo!()
    }

    /// The token file of one webhook of one family.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-files writes this body"
    )]
    #[must_use]
    pub fn webhook_token_file(&self, family: &FamilyName, name: &WebhookName) -> PathBuf {
        todo!()
    }
}

#[cfg(test)]
mod tests {
    use creche_contracts::status::time::Timestamp;

    use super::*;

    fn fault_file(source: FaultSource) -> FaultFile {
        let family: FamilyName = "chat".parse().unwrap();
        let written_at: Timestamp = "2031-04-18T06:43:10Z".parse().unwrap();

        FaultFile::new(family, source, written_at, Vec::new()).unwrap()
    }

    #[test]
    fn each_writer_is_its_fault_source() {
        assert_eq!(
            FaultSource::from(FaultWriter::Sessiond),
            FaultSource::Sessiond
        );
        assert_eq!(FaultSource::from(FaultWriter::Pep), FaultSource::Pep);
    }

    #[test]
    fn the_writer_of_a_file_is_the_source_of_the_file() {
        for writer in [FaultWriter::Sessiond, FaultWriter::Pep] {
            let file = fault_file(writer.into());

            assert_eq!(FaultWriter::of(&file), Some(writer));
        }
    }

    #[test]
    fn caregiver_is_no_writer_and_no_file_has_it_as_source() {
        // `FaultFile::new` refuses the one source that `FaultWriter::of`
        // gives `None` for, so no test can build that file.
        let family: FamilyName = "chat".parse().unwrap();
        let written_at: Timestamp = "2031-04-18T06:43:10Z".parse().unwrap();

        assert!(FaultFile::new(family, FaultSource::Managerd, written_at, Vec::new()).is_err());
        assert!(
            FaultSource::ALL
                .iter()
                .filter(|source| **source != FaultSource::Managerd)
                .all(|source| [FaultWriter::Sessiond, FaultWriter::Pep]
                    .iter()
                    .any(|writer| FaultSource::from(*writer) == *source))
        );
    }
}
