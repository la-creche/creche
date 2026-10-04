//! A file read with a size cap. The read gives each failure as a value and
//! does not panic.
//!
//! A service reads files that another process writes: a status document, a
//! fault file, a token file. Such a file can be absent, too large, a
//! directory, a FIFO or a symlink. [`read_capped`] gives each case as a value
//! of [`FileRead`], so the caller states what it does with each one.
//!
//! The Python services hold more than ten copies of this read, for example
//! `noticeboard/src/noticeboard/jsonfiles.py:41-67` and
//! `attendance/src/attendance/atomic.py:64-91`. They differ in the cap, in
//! the order of the stat and the read, and in what they do with a symlink.
//!
//! The bodies of [`read_capped`], [`facts`] and [`os_text`] are stubs.
//! `AGENTS.md` of this crate lists the stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::fs::Metadata;
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

/// The bits of a mode that are the permission bits, with setuid, setgid and
/// the sticky bit.
const PERMISSION_BITS: u32 = 0o7777;

/// The nanoseconds of one second.
const NANOS_PER_SECOND: i128 = 1_000_000_000;

/// The largest count of bytes that a reader takes from one file or from one
/// stream. The count is 1 or more.
///
/// A cap of 0 reads nothing, so the type refuses it. A reader with no cap
/// reads a file of each size into memory.
///
/// ```
/// use creche_runtime::readfile::ByteCap;
///
/// const STATUS_CAP: Option<ByteCap> = ByteCap::new(256 * 1024);
///
/// assert_eq!(STATUS_CAP.map(ByteCap::get), Some(262_144));
/// assert_eq!(ByteCap::new(0), None);
/// assert_eq!(ByteCap::ONE_MIB.get(), 1_048_576);
/// ```
///
/// Code outside this module cannot build a cap from a raw number:
///
/// ```compile_fail,E0423
/// use creche_runtime::readfile::ByteCap;
///
/// let cap = ByteCap(0);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct ByteCap(usize);

impl ByteCap {
    /// 1 MiB: the cap of a token file and of a fault file.
    pub const ONE_MIB: Self = Self(1024 * 1024);

    /// A cap of `bytes` bytes. `None` for 0.
    ///
    /// The function is `const`, so a module can hold its cap as a constant
    /// and no code path can panic on it.
    #[must_use]
    pub const fn new(bytes: usize) -> Option<Self> {
        if bytes == 0 {
            return None;
        }

        Some(Self(bytes))
    }

    /// The count of bytes.
    #[must_use]
    pub const fn get(self) -> usize {
        self.0
    }
}

/// What [`read_capped`] does with a path whose last part is a symlink.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Follow {
    /// Read the file that the symlink names.
    Follow,
    /// Refuse the path with [`ReadRefusal::Symlink`]. Use it for a file in a
    /// directory that another user can write.
    Refuse,
}

/// The facts of one file at one moment: where it is, its size, its last
/// change and its mode.
///
/// Two values are equal when no fact moved. A reader that holds the content
/// of a file compares the facts and reads again only after a change. The
/// facts come from one `stat`, so they are facts of one moment.
///
/// The `len` of a token file gives the length of the token. `Debug` prints
/// each fact, so do not write the facts of a token file to a log line
/// (contract 02 §3 rule 6).
///
/// ```
/// use creche_runtime::readfile::FileFacts;
///
/// let facts = FileFacts::from(&std::fs::metadata("Cargo.toml")?);
/// assert!(facts.len() > 0);
/// assert_eq!(facts.mode() & 0o7000, 0);
/// assert_eq!(facts, FileFacts::from(&std::fs::metadata("Cargo.toml")?));
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module cannot build the facts from raw numbers:
///
/// ```compile_fail,E0451
/// use creche_runtime::readfile::FileFacts;
///
/// let facts = FileFacts {
///     dev: 1,
///     ino: 2,
///     len: 3,
///     modified_ns: 4,
///     mode: 0o600,
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileFacts {
    dev: u64,
    ino: u64,
    len: u64,
    modified_ns: i128,
    mode: u32,
}

impl FileFacts {
    /// The id of the device that holds the file.
    #[must_use]
    pub const fn dev(self) -> u64 {
        self.dev
    }

    /// The inode of the file. A rename onto the path gives a new inode.
    #[must_use]
    pub const fn ino(self) -> u64 {
        self.ino
    }

    /// The size of the file in bytes.
    #[must_use]
    pub const fn len(self) -> u64 {
        self.len
    }

    /// Whether the file holds no byte.
    #[must_use]
    pub const fn is_empty(self) -> bool {
        self.len == 0
    }

    /// The time of the last change of the content, in nanoseconds from
    /// 1970-01-01T00:00:00Z. Python calls it `st_mtime_ns`.
    #[must_use]
    pub const fn modified_ns(self) -> i128 {
        self.modified_ns
    }

    /// The permission bits of the file, for example `0o640`. The bits of the
    /// file type are not in the value.
    #[must_use]
    pub const fn mode(self) -> u32 {
        self.mode
    }
}

impl From<&Metadata> for FileFacts {
    /// The facts of the file of one `stat`. The conversion does no I/O.
    fn from(metadata: &Metadata) -> Self {
        // 128 bits hold each count of seconds of 64 bits as nanoseconds, so
        // the sum cannot overflow.
        let modified_ns =
            i128::from(metadata.mtime()) * NANOS_PER_SECOND + i128::from(metadata.mtime_nsec());

        Self {
            dev: metadata.dev(),
            ino: metadata.ino(),
            len: metadata.len(),
            modified_ns,
            mode: metadata.mode() & PERMISSION_BITS,
        }
    }
}

/// What [`read_capped`] found at a path.
///
/// `Debug` prints no byte of the file, no count of the bytes and no fact of
/// the file. The file can be a token file, and its size then gives the
/// length of the token (contract 02 §3 rule 6).
#[derive(Clone, PartialEq, Eq)]
pub enum FileRead {
    /// The content of the file, and the facts of the file that the read had
    /// open.
    Bytes {
        /// Each byte of the file. The count is the cap or less.
        bytes: Vec<u8>,
        /// The facts of the open file. A caller checks the mode here and
        /// needs no second `stat`.
        facts: FileFacts,
    },
    /// No file has that name.
    Absent,
    /// A file is there, and the reader does not take it.
    Refused(ReadRefusal),
}

impl fmt::Debug for FileRead {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Bytes { .. } => f.debug_struct("Bytes").finish_non_exhaustive(),
            Self::Absent => f.write_str("Absent"),
            Self::Refused(refusal) => f.debug_tuple("Refused").field(refusal).finish(),
        }
    }
}

/// Why [`read_capped`] does not take a file that is there.
///
/// No variant holds a byte of the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadRefusal {
    /// The operating system refused the open or the read.
    Unreadable {
        /// The kind of the error.
        kind: io::ErrorKind,
        /// The text of the error, as `strerror` of Python gives it.
        os_text: String,
    },
    /// The file holds more bytes than the cap.
    TooLarge {
        /// The cap of the read.
        cap: ByteCap,
    },
    /// The path is a directory, a FIFO, a socket or a device.
    NotAFile,
    /// The last part of the path is a symlink, and the caller said
    /// [`Follow::Refuse`].
    Symlink,
}

impl fmt::Display for ReadRefusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unreadable { os_text, .. } => write!(f, "the file is not readable: {os_text}"),
            Self::TooLarge { cap } => write!(f, "the file has more than {} bytes", cap.get()),
            Self::NotAFile => f.write_str("the path is not a regular file"),
            Self::Symlink => f.write_str("the path is a symlink"),
        }
    }
}

impl Error for ReadRefusal {}

/// Reads the file at `path`, up to `cap` bytes.
///
/// The function has no error to return, and it does not panic. It does not
/// block on a FIFO. It reads the facts from the open file, so the facts and
/// the bytes are of one file.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-files writes this body"
)]
#[must_use]
pub fn read_capped(path: &Path, cap: ByteCap, follow: Follow) -> FileRead {
    todo!()
}

/// The facts of the file at `path`, from one `stat`. The result is `None`
/// for a path that the system cannot `stat`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-files writes this body"
)]
#[must_use]
pub fn facts(path: &Path) -> Option<FileFacts> {
    todo!()
}

/// The text of an error of the operating system, as `strerror` of Python
/// gives it: `No such file or directory`.
///
/// The `Display` of [`io::Error`] adds ` (os error 2)` to that text. An
/// error type of this crate holds the text without that part, so an operator
/// reads one text from a Python service and from a Rust service.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-files writes this body"
)]
#[must_use]
pub fn os_text(error: &io::Error) -> String {
    todo!()
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

    use creche_testkit::root::TempRoot;

    use super::*;

    #[test]
    fn a_cap_is_one_byte_or_more() {
        assert_eq!(ByteCap::new(0), None);
        assert_eq!(ByteCap::new(1).map(ByteCap::get), Some(1));
        assert_eq!(ByteCap::new(usize::MAX).map(ByteCap::get), Some(usize::MAX));
        assert_eq!(ByteCap::ONE_MIB.get(), 1 << 20);
        assert!(ByteCap::new(1) < ByteCap::new(2));
    }

    #[test]
    fn the_facts_are_the_facts_of_the_stat() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("token");
        fs::write(&path, b"0123456789").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
        let metadata = fs::metadata(&path).unwrap();
        let facts = FileFacts::from(&metadata);

        assert_eq!(facts.len(), 10);
        assert!(!facts.is_empty());
        assert_eq!(facts.mode(), 0o640);
        assert_eq!(facts.dev(), metadata.dev());
        assert_eq!(facts.ino(), metadata.ino());
        assert_eq!(
            facts.modified_ns(),
            i128::from(metadata.mtime()) * 1_000_000_000 + i128::from(metadata.mtime_nsec())
        );
    }

    #[test]
    fn the_facts_move_when_the_file_moves() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("token");
        fs::write(&path, b"first").unwrap();
        let first = FileFacts::from(&fs::metadata(&path).unwrap());

        assert_eq!(first, FileFacts::from(&fs::metadata(&path).unwrap()));

        // A rename onto the path gives a new inode, as each rotation does.
        let next = root.path().join("token.next");
        fs::write(&next, b"second, and longer").unwrap();
        fs::rename(&next, &path).unwrap();
        let second = FileFacts::from(&fs::metadata(&path).unwrap());

        assert_ne!(first, second);
        assert_ne!(first.ino(), second.ino());
        assert_ne!(first.len(), second.len());
    }

    #[test]
    fn the_mode_holds_the_setgid_bit_and_no_type_bit() {
        let root = TempRoot::new().unwrap();
        let dir = root.path().join("sock");
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o2750)).unwrap();

        assert_eq!(FileFacts::from(&fs::metadata(&dir).unwrap()).mode(), 0o2750);
    }

    #[test]
    fn an_empty_file_is_empty() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("empty");
        fs::write(&path, b"").unwrap();

        assert!(FileFacts::from(&fs::metadata(&path).unwrap()).is_empty());
    }

    #[test]
    fn the_debug_of_a_read_prints_no_byte_and_no_size_of_the_file() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("token");
        fs::write(&path, b"correct horse").unwrap();
        let read = FileRead::Bytes {
            bytes: b"correct horse".to_vec(),
            facts: FileFacts::from(&fs::metadata(&path).unwrap()),
        };

        // The size of a token file gives the length of the token, so the
        // text holds neither the count of the bytes nor the facts.
        assert_eq!(format!("{read:?}"), "Bytes { .. }");
        assert_eq!(format!("{:?}", FileRead::Absent), "Absent");
        assert_eq!(
            format!("{:?}", FileRead::Refused(ReadRefusal::Symlink)),
            "Refused(Symlink)"
        );
    }

    #[test]
    fn a_refusal_names_its_reason() {
        let table = [
            (
                ReadRefusal::Unreadable {
                    kind: io::ErrorKind::PermissionDenied,
                    os_text: String::from("Permission denied"),
                },
                "the file is not readable: Permission denied",
            ),
            (
                ReadRefusal::TooLarge {
                    cap: ByteCap::ONE_MIB,
                },
                "the file has more than 1048576 bytes",
            ),
            (ReadRefusal::NotAFile, "the path is not a regular file"),
            (ReadRefusal::Symlink, "the path is a symlink"),
        ];

        for (refusal, text) in table {
            assert_eq!(refusal.to_string(), text);
        }
    }
}
