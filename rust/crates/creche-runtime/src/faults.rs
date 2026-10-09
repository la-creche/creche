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
//! `chaperone/src/chaperone/faults.py:118-133`. No vector covers a write. The
//! doc comment of [`publish`] names each difference from the two writers, and
//! one plain test holds each one.

use std::error::Error;
use std::fmt;

use creche_contracts::status::fault_file::FaultFile;

use crate::atomic::{self, DirMode, DirSync, FileMode, Parents, Write, WriteError};
use crate::layout::{FaultWriter, StateRoot};

/// The mode of the directory of one writer: `0750` (contract 05 §3.3.1). The
/// one reader, `caregiver`, gets the directory and each file by the group.
const DIR_MODE: DirMode = DirMode::Group;

/// How [`publish`] writes a fault file.
///
/// - The mode is `0640` (contract 05 §3.3.1).
/// - [`publish`] makes the directory with its mode before the write, so the
///   write makes none.
/// - The write syncs the directory. It is the strictest level, and the level
///   of the Python chaperone (`chaperone/src/chaperone/faults.py:83-89`).
const HOW: Write = Write {
    mode: FileMode::GroupRead,
    parents: Parents::MustExist,
    dir_sync: DirSync::Sync,
};

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
/// The steps, in this order:
///
/// 1. Take the writer of the file from its source. The directory of the
///    writer is `faults/sessiond` or `faults/pep` under the root.
/// 2. Make that directory, and give it the mode `0750`.
/// 3. Write the bytes of `FaultFile::encode` to `<family>.json` in that
///    directory, with the mode `0640`: a temporary file, a sync, a rename and
///    a sync of the directory.
///
/// The Python origins are `attendance/src/attendance/faults.py:128-138` and
/// `chaperone/src/chaperone/faults.py:68-89`, `:105-106` and `:118-133`.
///
/// The function differs from its Python origins in five ways:
///
/// 1. The writer of `attendance` does not sync the directory
///    (`attendance/src/attendance/atomic.py:42-61`). This function syncs it
///    after the rename, as the chaperone does.
/// 2. The temporary file of the chaperone is `<family>.json.tmp`
///    (`chaperone/src/chaperone/faults.py:71`). This function writes
///    `.<family>.json.<pid>.<count>.tmp`, the form of `attendance`.
/// 3. The chaperone makes its directory and sets the mode one time, when
///    its writer starts (`chaperone/src/chaperone/faults.py:105-106`). This
///    function does both at each call, as `attendance` does.
/// 4. The chaperone takes the family as a text. For a text that is no
///    family name, it writes a log line and no file
///    (`chaperone/src/chaperone/faults.py:108-116`). This function takes a
///    file that holds a `FamilyName`.
/// 5. The chaperone writes a log line for a write that fails, and its
///    caller gets `False` (`chaperone/src/chaperone/faults.py:128-133`).
///    This function returns the error. The service writes the log line.
///
/// ```
/// use creche_contracts::status::fault_file::FaultFile;
/// use creche_contracts::status::words::FaultSource;
/// use creche_runtime::faults;
/// use creche_runtime::layout::{FaultWriter, StateRoot};
/// use creche_testkit::root::TempRoot;
///
/// let temp = TempRoot::new()?;
/// let root = StateRoot::new(temp.path().to_str().ok_or("the path is not text")?.parse()?);
/// let family = "chat".parse()?;
/// let written_at = "2031-04-18T06:43:10Z".parse()?;
/// let file = FaultFile::new(family, FaultSource::Pep, written_at, Vec::new())?;
///
/// faults::publish(&root, &file)?;
///
/// let path = root.fault_file(FaultWriter::Pep, file.family());
/// assert_eq!(std::fs::read(path)?, file.encode());
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// # Errors
///
/// [`PublishError::SourceWritesNoFile`] for a file of `caregiver`, and
/// [`PublishError::Write`] when the write fails.
pub fn publish(root: &StateRoot, file: &FaultFile) -> Result<(), PublishError> {
    publish_as(FaultWriter::of(file), root, file)
}

/// Writes `file` into the directory of `writer`: steps 2 and 3 of
/// [`publish`]. `None` is a file whose source writes no fault file.
///
/// `FaultFile::new` refuses that source, so no code can build such a file
/// today. The function takes the writer as a value. A later change to
/// `FaultFile` is then an error here, and not a file in a wrong directory.
fn publish_as(
    writer: Option<FaultWriter>,
    root: &StateRoot,
    file: &FaultFile,
) -> Result<(), PublishError> {
    let Some(writer) = writer else {
        return Err(PublishError::SourceWritesNoFile);
    };

    atomic::ensure_dir(&root.fault_dir(writer), DIR_MODE).map_err(PublishError::Write)?;

    let path = root.fault_file(writer, file.family());

    atomic::write(&path, &file.encode(), HOW).map_err(PublishError::Write)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::io;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};

    use creche_contracts::ids::FamilyName;
    use creche_contracts::status::fault_file::{FaultFileError, OpenFault};
    use creche_contracts::status::json::{Json, Object};
    use creche_contracts::status::time::Timestamp;
    use creche_contracts::status::words::{FaultCode, FaultSource};
    use creche_testkit::root::TempRoot;

    use super::*;
    use crate::atomic::WriteStep;

    /// The bits of a mode that are the permission bits, with setuid, setgid
    /// and the sticky bit.
    const PERMISSION_BITS: u32 = 0o7777;

    /// The family of each file of these tests.
    const FAMILY: &str = "chat";

    /// The name of the fault file of that family.
    const FILE_NAME: &str = "chat.json";

    /// Each writer, the name of its directory and one code that it detects.
    const WRITERS: [(FaultWriter, &str, FaultCode); 2] = [
        (
            FaultWriter::Sessiond,
            "sessiond",
            FaultCode::ProtocolMismatch,
        ),
        (FaultWriter::Pep, "pep", FaultCode::GrantsStale),
    ];

    fn state_root(temp: &TempRoot) -> StateRoot {
        StateRoot::new(temp.path().to_str().unwrap().parse().unwrap())
    }

    fn family() -> FamilyName {
        FAMILY.parse().unwrap()
    }

    fn written_at() -> Timestamp {
        "2031-04-18T06:43:10Z".parse().unwrap()
    }

    /// One open fault with the detail that a Python writer gives it.
    fn open_fault(code: FaultCode) -> OpenFault {
        let mut detail = Object::new();
        detail.insert(
            "message",
            Json::String(String::from("grants/chat.json: absent")),
        );

        OpenFault {
            code,
            since: "2031-04-18T06:42:58Z".parse().unwrap(),
            detail,
        }
    }

    fn fault_file(writer: FaultWriter, faults: Vec<OpenFault>) -> FaultFile {
        FaultFile::new(family(), writer.into(), written_at(), faults).unwrap()
    }

    fn mode_of(path: &Path) -> u32 {
        fs::metadata(path).unwrap().permissions().mode() & PERMISSION_BITS
    }

    fn set_mode(path: &Path, mode: u32) {
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    }

    /// The name of each entry of `dir`, in sorted order.
    fn names_in(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(dir)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();

        names
    }

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

    /// The port of `test_modes_are_pinned`
    /// (`chaperone/tests/test_faults.py:59-66`), for each of the two writers.
    #[test]
    fn publish_writes_the_bytes_with_mode_0640_into_a_directory_of_mode_0750() {
        for (writer, dir_name, code) in WRITERS {
            let temp = TempRoot::new().unwrap();
            let file = fault_file(writer, vec![open_fault(code)]);
            let dir = temp.path().join("faults").join(dir_name);
            let path = dir.join(FILE_NAME);

            publish(&state_root(&temp), &file).unwrap();

            assert_eq!(mode_of(&dir), 0o750, "{dir_name}");
            assert_eq!(mode_of(&path), 0o640, "{dir_name}");
            assert_eq!(fs::read(&path).unwrap(), file.encode(), "{dir_name}");
            // The write leaves no temporary file.
            assert_eq!(names_in(&dir), [FILE_NAME], "{dir_name}");
            // The other writer has no directory: each writer has its own.
            assert_eq!(names_in(&temp.path().join("faults")), [dir_name]);
        }
    }

    #[test]
    fn the_file_holds_the_source_of_its_directory() {
        for (writer, dir_name, code) in WRITERS {
            let temp = TempRoot::new().unwrap();
            let root = state_root(&temp);
            let file = fault_file(writer, vec![open_fault(code)]);

            publish(&root, &file).unwrap();

            let text = fs::read_to_string(root.fault_file(writer, &family())).unwrap();
            let source = FaultSource::from(writer).as_str();

            assert_eq!(source, dir_name);
            assert!(text.contains(&format!("\"{source}\"")), "{text}");
            assert!(text.ends_with('\n'), "{text}");
        }
    }

    #[test]
    fn a_second_publish_replaces_the_whole_file() {
        let temp = TempRoot::new().unwrap();
        let root = state_root(&temp);
        let path = root.fault_file(FaultWriter::Pep, &family());
        let open = fault_file(FaultWriter::Pep, vec![open_fault(FaultCode::GrantsStale)]);
        // A file with no fault clears each fault of its writer (contract 05
        // §3.3.1 rule 2).
        let cleared = fault_file(FaultWriter::Pep, Vec::new());

        publish(&root, &open).unwrap();
        publish(&root, &cleared).unwrap();

        assert_ne!(open.encode(), cleared.encode());
        assert_eq!(fs::read(&path).unwrap(), cleared.encode());
        assert_eq!(mode_of(&path), 0o640);
        assert_eq!(names_in(&root.fault_dir(FaultWriter::Pep)), [FILE_NAME]);
    }

    #[test]
    fn a_file_whose_source_writes_no_file_is_refused() {
        let temp = TempRoot::new().unwrap();
        let root = state_root(&temp);
        let file = fault_file(FaultWriter::Pep, Vec::new());

        assert_eq!(
            publish_as(None, &root, &file),
            Err(PublishError::SourceWritesNoFile)
        );
        // The refusal comes before the first step: the root holds no entry.
        assert_eq!(names_in(temp.path()), Vec::<String>::new());
    }

    #[test]
    fn only_caregiver_is_a_source_with_no_writer() {
        // `FaultFile::new` refuses the source that has no writer, so
        // `publish` gets no such file from code that builds one today.
        assert_eq!(
            FaultFile::new(family(), FaultSource::Managerd, written_at(), Vec::new()),
            Err(FaultFileError::SourceWritesNoFile)
        );

        for source in FaultSource::ALL {
            let Ok(file) = FaultFile::new(family(), *source, written_at(), Vec::new()) else {
                assert_eq!(*source, FaultSource::Managerd);
                continue;
            };
            let temp = TempRoot::new().unwrap();

            assert_eq!(publish(&state_root(&temp), &file), Ok(()), "{source:?}");
        }
    }

    #[test]
    fn a_directory_that_cannot_exist_fails_the_publish() {
        let temp = TempRoot::new().unwrap();
        // A regular file has the name of the directory of each writer.
        fs::write(temp.path().join("faults"), b"").unwrap();
        let file = fault_file(FaultWriter::Sessiond, Vec::new());

        let Err(PublishError::Write(error)) = publish(&state_root(&temp), &file) else {
            panic!("the publish made a directory below a regular file");
        };

        assert_eq!(error.step, WriteStep::MakeParents);
        assert_eq!(error.path, temp.path().join("faults").join("sessiond"));
        assert_eq!(names_in(temp.path()), ["faults"]);
    }

    /// Difference 1 of [`publish`]. The writer of `attendance` makes no sync
    /// of the directory (`attendance/src/attendance/atomic.py:42-61`).
    #[test]
    fn the_write_syncs_the_directory() {
        // No test can see a sync from outside the write. `HOW` is the one
        // value that `publish` gives the write, and the tests of `atomic`
        // hold what the write does with it.
        assert_eq!(HOW.dir_sync, DirSync::Sync);
        assert_eq!(HOW.parents, Parents::MustExist);
        assert_eq!(HOW.mode, FileMode::GroupRead);
    }

    /// Difference 2 of [`publish`]. The temporary file of the chaperone is
    /// `<family>.json.tmp` (`chaperone/src/chaperone/faults.py:71`).
    #[test]
    fn the_write_does_not_use_the_name_of_the_chaperone() {
        let temp = TempRoot::new().unwrap();
        let root = state_root(&temp);
        let dir = root.fault_dir(FaultWriter::Pep);
        let python_name = dir.join("chat.json.tmp");
        let file = fault_file(FaultWriter::Pep, vec![open_fault(FaultCode::GrantsStale)]);

        // A Python chaperone that stopped in the middle of a write left this
        // file.
        fs::create_dir_all(&dir).unwrap();
        fs::write(&python_name, b"left over").unwrap();

        publish(&root, &file).unwrap();

        assert_eq!(fs::read(&python_name).unwrap(), b"left over");
        assert_eq!(fs::read(dir.join(FILE_NAME)).unwrap(), file.encode());
        assert_eq!(names_in(&dir), [FILE_NAME, "chat.json.tmp"]);
    }

    /// Difference 3 of [`publish`]. The chaperone makes its directory one
    /// time (`chaperone/src/chaperone/faults.py:105-106`).
    #[test]
    fn each_publish_makes_the_directory_with_its_mode() {
        let temp = TempRoot::new().unwrap();
        let root = state_root(&temp);
        let dir = root.fault_dir(FaultWriter::Pep);
        let file = fault_file(FaultWriter::Pep, Vec::new());

        publish(&root, &file).unwrap();
        // Another program gave the directory a mode that shuts the reader
        // out.
        set_mode(&dir, 0o700);
        publish(&root, &file).unwrap();

        assert_eq!(mode_of(&dir), 0o750);

        fs::remove_dir_all(&dir).unwrap();
        publish(&root, &file).unwrap();

        assert_eq!(mode_of(&dir), 0o750);
        assert_eq!(names_in(&dir), [FILE_NAME]);
    }

    /// Difference 4 of [`publish`]. The chaperone takes the family as a text
    /// (`chaperone/src/chaperone/faults.py:108-116`).
    #[test]
    fn the_path_is_one_file_name_in_the_directory() {
        let temp = TempRoot::new().unwrap();
        let root = state_root(&temp);

        // A text that leaves the directory is no family name, so no file
        // can hold it.
        assert!("../chat".parse::<FamilyName>().is_err());
        assert!("".parse::<FamilyName>().is_err());

        for (writer, _, _) in WRITERS {
            let path = root.fault_file(writer, &family());

            assert_eq!(path.parent(), Some(root.fault_dir(writer).as_path()));
            assert_eq!(path.file_name().unwrap().to_str(), Some(FILE_NAME));
        }
    }

    /// Difference 5 of [`publish`]. The caller of the Python chaperone gets
    /// `False` (`chaperone/src/chaperone/faults.py:128-133`).
    #[test]
    fn a_write_that_fails_names_its_step() {
        let temp = TempRoot::new().unwrap();
        let root = state_root(&temp);
        let path = root.fault_file(FaultWriter::Pep, &family());
        let file = fault_file(FaultWriter::Pep, Vec::new());

        // A directory has the name of the fault file, so the rename fails.
        fs::create_dir_all(path.join("held")).unwrap();

        let Err(PublishError::Write(error)) = publish(&root, &file) else {
            panic!("the publish replaced a directory that is not empty");
        };

        assert_eq!(error.step, WriteStep::Rename);
        assert_eq!(error.path, path);
        assert!(!error.os_text.is_empty());
        // The write that failed leaves no temporary file.
        assert_eq!(names_in(&root.fault_dir(FaultWriter::Pep)), [FILE_NAME]);
    }
}
