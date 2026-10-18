//! The read of the `.env` of the host: one open, one `fstat` of the opened
//! file, the trust check, and the bytes.
//!
//! `creche_contracts::config::envfile` holds the grammar, the layers and
//! the trust check. This module is the edge that reads the file. The Python
//! origin is `read_trusted` of `env/src/creche_env/envfile.py:676-679`,
//! with `_read` and `_bytes_of` at `:637-673`.

use std::error::Error;
use std::fmt;
use std::fs::File;
use std::io::{self, Read};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use creche_contracts::config::envfile::{self, EnvError, Text, Uid};

use crate::readfile::{Follow, open_flags, open_path, os_text};

/// Why [`read_site`] gives no bytes.
///
/// No variant holds a byte of the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SiteReadError {
    /// The system refused the open, the `fstat` or the read.
    Unreadable {
        /// The path, as the fault of a text names it.
        file: String,
        /// The kind of the error.
        kind: io::ErrorKind,
        /// The text of the error, as `strerror` of Python gives it.
        os_text: String,
    },
    /// The path names no regular file, for example a directory or a FIFO.
    NotAFile {
        /// The path, as the fault of a text names it.
        file: String,
    },
    /// Another account can change the file: [`envfile::check_trust`].
    Untrusted(EnvError),
}

impl fmt::Display for SiteReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unreadable { file, os_text, .. } => {
                write!(f, "{file}: cannot be read: {os_text}")
            }
            Self::NotAFile { file } => write!(f, "{file}: cannot be read: no regular file"),
            Self::Untrusted(error) => write!(f, "{error}"),
        }
    }
}

impl Error for SiteReadError {}

/// The bytes of a `.env` at `path`, after the trust check of the opened
/// file. The account of the process reads the file.
///
/// The steps, in this order:
///
/// 1. Open the path for a read that does not block. A symlink is followed:
///    its target is the file that the check judges.
/// 2. Read the owner, the mode and the type of the open file with one
///    `fstat`.
/// 3. Refuse a file that is not a regular file.
/// 4. Give the owner and the mode to [`envfile::check_trust`].
/// 5. Read [`envfile::MAX_BYTES`] and one byte more at most. The parse of
///    the text then refuses a file that is too large.
///
/// The Python origin names the `errno` of a failed call, for example
/// `ENOENT`. This function gives the text of `strerror`, as each error of
/// this crate does.
///
/// # Errors
///
/// [`SiteReadError`] for a file that the process cannot read, for a path
/// that is no regular file, and for a file that fails the trust check.
pub fn read_site(path: &Path) -> Result<Text, SiteReadError> {
    read_as(path, Uid::new(rustix::process::getuid().as_raw()))
}

/// [`read_site`] for the account `reader`.
fn read_as(path: &Path, reader: Uid) -> Result<Text, SiteReadError> {
    let file = path.display().to_string();
    let unreadable = |error: &io::Error| SiteReadError::Unreadable {
        file: file.clone(),
        kind: error.kind(),
        os_text: os_text(error),
    };

    let descriptor = open_path(path, open_flags(Follow::Follow))
        .map_err(|errno| unreadable(&io::Error::from(errno)))?;
    let opened = File::from(descriptor);
    let facts = opened.metadata().map_err(|error| unreadable(&error))?;
    if !facts.is_file() {
        return Err(SiteReadError::NotAFile { file });
    }

    envfile::check_trust(&file, Uid::new(facts.uid()), facts.mode(), reader)
        .map_err(SiteReadError::Untrusted)?;

    let limit = u64::try_from(envfile::MAX_BYTES).map_or(u64::MAX, |bytes| bytes.saturating_add(1));
    let mut data = Vec::new();
    opened
        .take(limit)
        .read_to_end(&mut data)
        .map_err(|error| unreadable(&error))?;

    Ok(Text::new(file, data))
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::PathBuf;
    use std::process::Command;

    use creche_contracts::config::envfile::{Fault, FileKind, parse};
    use creche_testkit::root::TempRoot;

    use super::*;

    /// The uid of an account that no test runs as.
    const NOBODY: u32 = 65_534;

    /// A `.env` with the mode `mode` in `root`, and the uid of its owner.
    fn site_file(root: &TempRoot, mode: u32) -> (PathBuf, u32) {
        let path = root.path().join("site.env");
        fs::write(&path, "AGENT_LAN_ADDRESS=192.0.2.10\n").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
        let owner = fs::metadata(&path).unwrap().uid();

        (path, owner)
    }

    /// The fault of a read that the trust check refused.
    fn untrusted(read: Result<Text, SiteReadError>) -> Fault {
        match read {
            Err(SiteReadError::Untrusted(error)) => error.problems()[0].fault(),
            other => panic!("the check did not refuse the file: {other:?}"),
        }
    }

    #[test]
    fn a_file_of_the_reader_gives_its_bytes() {
        let root = TempRoot::new().unwrap();
        let (path, _) = site_file(&root, 0o600);

        let text = read_site(&path).unwrap();

        assert_eq!(text.name(), path.display().to_string());
        assert_eq!(text.data(), b"AGENT_LAN_ADDRESS=192.0.2.10\n");
    }

    #[test]
    fn a_file_that_the_group_can_write_is_refused() {
        let root = TempRoot::new().unwrap();
        let (path, _) = site_file(&root, 0o620);

        assert_eq!(untrusted(read_site(&path)), Fault::OthersWrite);
    }

    #[test]
    fn a_file_of_another_owner_is_refused() {
        let root = TempRoot::new().unwrap();
        let (path, owner) = site_file(&root, 0o644);
        // A file of root passes for each reader. A test that runs as root
        // gives its file to another account first.
        let owner = if owner == 0 {
            std::os::unix::fs::chown(&path, Some(NOBODY), None).unwrap();
            NOBODY
        } else {
            owner
        };

        let other = Uid::new(owner.checked_add(1).unwrap());
        assert_eq!(untrusted(read_as(&path, other)), Fault::WrongOwner);
        assert_eq!(untrusted(read_as(&path, Uid::ROOT)), Fault::WrongOwner);
        assert!(read_as(&path, Uid::new(owner)).is_ok());
    }

    #[test]
    fn a_link_gives_its_target_to_the_check() {
        let root = TempRoot::new().unwrap();
        let (path, _) = site_file(&root, 0o664);
        let link = root.path().join("link.env");
        symlink(&path, &link).unwrap();

        assert_eq!(untrusted(read_site(&link)), Fault::OthersWrite);
    }

    #[test]
    fn a_path_that_is_no_regular_file_is_refused_without_a_wait() {
        let root = TempRoot::new().unwrap();
        let fifo = root.path().join("fifo.env");
        assert!(
            Command::new("mkfifo")
                .arg(&fifo)
                .status()
                .unwrap()
                .success()
        );
        let absent = root.path().join("absent.env");

        for path in [root.path().to_owned(), fifo] {
            let file = path.display().to_string();
            assert_eq!(read_site(&path), Err(SiteReadError::NotAFile { file }));
        }

        let Err(SiteReadError::Unreadable { kind, os_text, .. }) = read_site(&absent) else {
            panic!("an absent file gave bytes");
        };
        assert_eq!(
            (kind, os_text.as_str()),
            (io::ErrorKind::NotFound, "No such file or directory")
        );
    }

    #[test]
    fn a_file_past_the_limit_gives_one_byte_more_and_its_parse_refuses_it() {
        let root = TempRoot::new().unwrap();
        let (path, _) = site_file(&root, 0o600);
        fs::write(&path, vec![b'#'; envfile::MAX_BYTES * 2]).unwrap();

        let text = read_site(&path).unwrap();
        let error = parse(&text, FileKind::Env).unwrap_err();

        assert_eq!(text.data().len(), envfile::MAX_BYTES + 1);
        assert_eq!(error.problems()[0].fault(), Fault::TooLarge);
        assert_eq!(
            SiteReadError::NotAFile {
                file: String::from("a")
            }
            .to_string(),
            "a: cannot be read: no regular file"
        );
    }
}
