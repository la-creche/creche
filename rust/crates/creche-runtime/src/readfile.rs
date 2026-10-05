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
//! The table `DEVIATIONS` in the tests of this module names each difference.
//!
//! [`read_capped`] opens the path one time. It reads the facts and the bytes
//! from that open file, so the facts and the bytes are of one file.

use std::error::Error;
use std::fmt;
use std::fs::{self, File, Metadata};
use std::io::{self, Read};
use std::os::fd::OwnedFd;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use rustix::fs::{Mode, OFlags};
use rustix::io::Errno;

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
    /// The operating system refused the open or the read. The open of a
    /// socket is one such case.
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
    /// The path is a directory, a FIFO or a device.
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
///
/// The steps, in this order:
///
/// 1. Open the path for a read that does not block. With
///    [`Follow::Refuse`], the open refuses a symlink at the last part of the
///    path. A symlink in a directory above the last part is followed.
/// 2. Read the facts of the open file.
/// 3. Refuse a file that is not a regular file.
/// 4. Refuse a file whose size is more than the cap.
/// 5. Read the cap plus one byte at most. Refuse a file that gave that last
///    byte: the file grew after step 2.
///
/// The Python origins are these readers. Each one has its own cap.
///
/// - A stat of the path, then a read: `attendance/src/attendance/atomic.py:71-82`,
///   `door-owui/src/agent_door_owui/families.py:78-83`,
///   `door-trigger/src/agent_door_trigger/families.py:73-78`,
///   `door-tui/src/agent_door_tui/status.py:129-135`,
///   `caregiver/src/caregiver/verify.py:162-166`,
///   `door-trigger/src/agent_door_trigger/quiet/state.py:55-59` and
///   `door-trigger/src/agent_door_trigger/quiet/records.py:116-121`.
/// - A read of the cap plus one byte:
///   `noticeboard/src/noticeboard/jsonfiles.py:48-65` and
///   `caregiver/src/caregiver/mcp_release.py:376-385`.
/// - An open that refuses a symlink and does not block, then a check for a
///   regular file: `caregiver/src/caregiver/released.py:68` and `:95-111`,
///   `handover/src/handover/executor/spool.py:311-351`.
/// - A check for a symlink before the read:
///   `caregiver/src/caregiver/live_manifest.py:158-165` and
///   `caregiver/src/caregiver/mcp_release.py:832-842`.
/// - A read with no cap: `caregiver/src/caregiver/atomic.py:79-82`, which
///   `caregiver/src/caregiver/faults.py:116` calls, and
///   `caregiver/src/caregiver/mcp_release.py:890-894`.
///
/// ```
/// use std::path::Path;
///
/// use creche_runtime::readfile::{read_capped, ByteCap, FileRead, Follow};
///
/// let read = read_capped(Path::new("Cargo.toml"), ByteCap::ONE_MIB, Follow::Refuse);
/// let FileRead::Bytes { bytes, facts } = read else {
///     return Err("the manifest of this crate is a regular file");
/// };
/// assert_eq!(u64::try_from(bytes.len()), Ok(facts.len()));
///
/// let none = read_capped(Path::new("no-such-file"), ByteCap::ONE_MIB, Follow::Follow);
/// assert_eq!(none, FileRead::Absent);
/// # Ok::<(), &str>(())
/// ```
#[must_use]
pub fn read_capped(path: &Path, cap: ByteCap, follow: Follow) -> FileRead {
    match open_path(path, open_flags(follow)) {
        Ok(descriptor) => read_open(File::from(descriptor), cap),
        Err(errno) if errno == Errno::NOENT => FileRead::Absent,
        Err(errno) => FileRead::Refused(open_refusal(path, follow, errno)),
    }
}

/// Steps 2 to 5 of [`read_capped`], on the file that step 1 opened.
///
/// The function takes no path. Each fact and each byte thus comes from the
/// open file, and a rename onto the path after the open changes neither.
fn read_open(file: File, cap: ByteCap) -> FileRead {
    let facts = match file.metadata() {
        Ok(metadata) if metadata.is_file() => FileFacts::from(&metadata),
        Ok(_) => return FileRead::Refused(ReadRefusal::NotAFile),
        Err(error) => return FileRead::Refused(unreadable(&error)),
    };

    if facts.len() > as_u64(cap.get()) {
        return FileRead::Refused(ReadRefusal::TooLarge { cap });
    }

    match take_capped(file, cap) {
        Ok(bytes) => FileRead::Bytes { bytes, facts },
        Err(refusal) => FileRead::Refused(refusal),
    }
}

/// The flags of the one open of [`read_capped`].
///
/// - `NONBLOCK`: the open of a FIFO with no writer returns at once.
/// - `CLOEXEC`: a child program does not get the descriptor.
/// - `NOCTTY`: the open of a terminal does not make it the terminal of the
///   process.
/// - `NOFOLLOW`, for [`Follow::Refuse`]: the kernel refuses a symlink at the
///   last part of the path. The check and the open are one call.
fn open_flags(follow: Follow) -> OFlags {
    let flags = OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC | OFlags::NOCTTY;

    match follow {
        Follow::Follow => flags,
        Follow::Refuse => flags | OFlags::NOFOLLOW,
    }
}

/// Opens `path`. When a signal stops the call, the function makes the call
/// again, as the open of the standard library and the open of Python do.
fn open_path(path: &Path, flags: OFlags) -> Result<OwnedFd, Errno> {
    loop {
        match rustix::fs::open(path, flags, Mode::empty()) {
            Err(errno) if errno == Errno::INTR => {}
            result => return result,
        }
    }
}

/// Why the open of `path` failed, for each error but "no such file".
///
/// With `NOFOLLOW`, the kernel answers `ELOOP` for a symlink at the last part
/// of the path. It gives the same answer for a chain of symlinks with no end
/// in a directory above. One look at the last part tells the two apart. The
/// result is a refusal for each answer of that look.
fn open_refusal(path: &Path, follow: Follow, errno: Errno) -> ReadRefusal {
    let last_part_is_symlink =
        || fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink());

    if follow == Follow::Refuse && errno == Errno::LOOP && last_part_is_symlink() {
        return ReadRefusal::Symlink;
    }

    unreadable(&io::Error::from(errno))
}

/// Reads `source` to its end, and refuses a source of more than `cap` bytes.
///
/// The read stops one byte past the cap. That byte proves that the source is
/// too large, so the size of a file never sets the memory of the process.
fn take_capped(source: impl Read, cap: ByteCap) -> Result<Vec<u8>, ReadRefusal> {
    let limit = as_u64(cap.get()).saturating_add(1);
    let mut bytes = Vec::new();

    if let Err(error) = source.take(limit).read_to_end(&mut bytes) {
        return Err(unreadable(&error));
    }

    if bytes.len() > cap.get() {
        return Err(ReadRefusal::TooLarge { cap });
    }

    Ok(bytes)
}

/// A count of bytes as 64 bits. A count that 64 bits cannot hold reads as
/// the largest count, and no file has that size.
fn as_u64(count: usize) -> u64 {
    u64::try_from(count).unwrap_or(u64::MAX)
}

/// The refusal for an error of the operating system.
fn unreadable(error: &io::Error) -> ReadRefusal {
    ReadRefusal::Unreadable {
        kind: error.kind(),
        os_text: os_text(error),
    }
}

/// The facts of the file at `path`, from one `stat`. The result is `None`
/// for a path that the system cannot `stat`.
///
/// The `stat` follows a symlink. The Python origin is the `stat` of a cache
/// that reads a file again only after a change:
/// `chaperone/src/chaperone/family_grants.py:219-223`.
///
/// ```
/// use std::path::Path;
///
/// use creche_runtime::readfile::facts;
///
/// assert!(facts(Path::new("Cargo.toml")).is_some());
/// assert_eq!(facts(Path::new("no-such-file")), None);
/// ```
#[must_use]
pub fn facts(path: &Path) -> Option<FileFacts> {
    fs::metadata(path)
        .ok()
        .map(|metadata| FileFacts::from(&metadata))
}

/// The text of an error of the operating system, as `strerror` of Python
/// gives it: `No such file or directory`.
///
/// The `Display` of [`io::Error`] adds ` (os error 2)` to that text. An
/// error type of this crate holds the text without that part, so an operator
/// reads one text from a Python service and from a Rust service.
///
/// An error that the operating system did not make keeps its own text.
///
/// The Python origin is each log line that writes `exc.strerror`, for
/// example `noticeboard/src/noticeboard/jsonfiles.py:58`.
///
/// ```
/// use std::io;
///
/// use creche_runtime::readfile::os_text;
///
/// let error = io::Error::from_raw_os_error(2);
/// assert_eq!(error.to_string(), "No such file or directory (os error 2)");
/// assert_eq!(os_text(&error), "No such file or directory");
/// ```
#[must_use]
pub fn os_text(error: &io::Error) -> String {
    let text = error.to_string();
    let Some(code) = error.raw_os_error() else {
        return text;
    };

    match text.strip_suffix(&format!(" (os error {code})")) {
        Some(plain) => plain.to_owned(),
        None => text,
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::os::unix::net::UnixListener;
    use std::process::Command;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;

    use creche_testkit::root::TempRoot;

    use super::*;

    /// How long a read that must not block can take. The limit is long,
    /// because the machine of a test run can be under load.
    const NO_BLOCK_LIMIT: Duration = Duration::from_secs(30);

    /// The text of `ELOOP` on Linux and on macOS.
    const TOO_MANY_LINKS: &str = "Too many levels of symbolic links";

    fn cap(bytes: usize) -> ByteCap {
        ByteCap::new(bytes).unwrap()
    }

    /// The bytes and the facts of a read that took the file.
    fn taken(read: FileRead) -> (Vec<u8>, FileFacts) {
        match read {
            FileRead::Bytes { bytes, facts } => (bytes, facts),
            other => panic!("the read did not take the file: {other:?}"),
        }
    }

    /// Makes a file with `bytes` in a new directory.
    fn file_with(bytes: &[u8]) -> (TempRoot, std::path::PathBuf) {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("status.json");
        fs::write(&path, bytes).unwrap();

        (root, path)
    }

    /// Makes a FIFO with the program `mkfifo` of the host. The standard
    /// library has no call for it.
    fn make_fifo(path: &Path) {
        let status = Command::new("mkfifo").arg(path).status().unwrap();

        assert!(status.success(), "mkfifo failed: {status}");
    }

    /// The result of `work`, or `None` when `work` takes more than
    /// [`NO_BLOCK_LIMIT`]. The work runs on its own thread, so a read that
    /// blocks fails the test and does not hold the test run.
    fn within_the_limit<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static) -> Option<T> {
        let (sender, receiver) = mpsc::channel();
        thread::spawn(move || {
            let _ = sender.send(work());
        });

        receiver.recv_timeout(NO_BLOCK_LIMIT).ok()
    }

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

    #[test]
    fn a_file_of_the_cap_is_read_whole() {
        let (_root, path) = file_with(b"0123456789");

        for follow in [Follow::Follow, Follow::Refuse] {
            let (bytes, facts) = taken(read_capped(&path, cap(10), follow));

            assert_eq!(bytes, b"0123456789");
            assert_eq!(facts.len(), 10);
        }
    }

    #[test]
    fn a_file_of_the_cap_plus_one_byte_is_too_large() {
        let (_root, path) = file_with(b"0123456789");

        for follow in [Follow::Follow, Follow::Refuse] {
            assert_eq!(
                read_capped(&path, cap(9), follow),
                FileRead::Refused(ReadRefusal::TooLarge { cap: cap(9) })
            );
        }
    }

    #[test]
    fn a_source_that_gives_more_than_the_cap_is_too_large() {
        // The size check of the stat does not see a file that grows after
        // the stat. The read itself stops one byte past the cap.
        let ten: &[u8] = b"0123456789";

        assert_eq!(take_capped(ten, cap(10)), Ok(ten.to_vec()));
        assert_eq!(take_capped(ten, cap(11)), Ok(ten.to_vec()));
        assert_eq!(
            take_capped(ten, cap(9)),
            Err(ReadRefusal::TooLarge { cap: cap(9) })
        );
        assert_eq!(
            take_capped(ten, cap(1)),
            Err(ReadRefusal::TooLarge { cap: cap(1) })
        );
    }

    #[test]
    fn the_largest_cap_reads_a_file_and_does_not_overflow() {
        let (_root, path) = file_with(b"0123456789");
        let (bytes, _facts) = taken(read_capped(&path, cap(usize::MAX), Follow::Refuse));

        assert_eq!(bytes, b"0123456789");
        assert_eq!(as_u64(usize::MAX).saturating_add(1), u64::MAX);
    }

    #[test]
    fn the_read_gives_each_byte_as_it_is_and_checks_no_content() {
        // The Python tests of `read_json` give these contents, in
        // `caregiver/tests/test_atomic.py`. The Python reader parses them.
        // This reader gives the bytes, and the caller parses them.
        let contents: [&[u8]; 6] = [
            b" {\"family\": \"chat\", \"n\": [1, 2.5, null]}\r\n",
            b"{not json",
            b"{\"a\": 1} x",
            b"\xef\xbb\xbf{}",
            b"{\"family\": \"\xff\"}",
            b"\xff\xfe{\x00\"\x00f\x00\"\x00:\x001\x00}\x00",
        ];

        for content in contents {
            let (_root, path) = file_with(content);
            let (bytes, _facts) = taken(read_capped(&path, ByteCap::ONE_MIB, Follow::Refuse));

            assert_eq!(bytes, content);
        }
    }

    #[test]
    fn an_empty_file_reads_as_no_byte() {
        let (_root, path) = file_with(b"");
        let (bytes, facts) = taken(read_capped(&path, cap(1), Follow::Refuse));

        assert!(bytes.is_empty());
        assert!(facts.is_empty());
    }

    #[test]
    fn the_facts_of_a_read_are_the_facts_of_the_file() {
        let (_root, path) = file_with(b"0123456789abcdef");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
        let (bytes, read_facts) = taken(read_capped(&path, ByteCap::ONE_MIB, Follow::Refuse));

        assert_eq!(read_facts.mode(), 0o640);
        assert_eq!(u64::try_from(bytes.len()), Ok(read_facts.len()));
        assert_eq!(Some(read_facts), facts(&path));
    }

    #[test]
    fn a_missing_file_is_absent() {
        let root = TempRoot::new().unwrap();

        for follow in [Follow::Follow, Follow::Refuse] {
            assert_eq!(
                read_capped(&root.path().join("status.json"), cap(8), follow),
                FileRead::Absent
            );
            // A directory above the file is absent too.
            assert_eq!(
                read_capped(&root.path().join("chat/status.json"), cap(8), follow),
                FileRead::Absent
            );
        }
    }

    #[test]
    fn a_path_through_a_file_is_unreadable_and_not_absent() {
        let (_root, path) = file_with(b"{}");
        let read = read_capped(&path.join("status.json"), cap(8), Follow::Refuse);

        assert_eq!(
            read,
            FileRead::Refused(ReadRefusal::Unreadable {
                kind: io::ErrorKind::NotADirectory,
                os_text: String::from("Not a directory"),
            })
        );
    }

    #[test]
    fn a_directory_is_not_a_file() {
        let root = TempRoot::new().unwrap();

        for follow in [Follow::Follow, Follow::Refuse] {
            assert_eq!(
                read_capped(root.path(), cap(8), follow),
                FileRead::Refused(ReadRefusal::NotAFile)
            );
        }
    }

    #[test]
    fn a_symlink_is_followed_or_refused_as_the_caller_says() {
        let (root, path) = file_with(b"{\"epoch\":1}");
        let link = root.path().join("link.json");
        symlink(&path, &link).unwrap();

        let (bytes, link_facts) = taken(read_capped(&link, cap(64), Follow::Follow));
        assert_eq!(bytes, b"{\"epoch\":1}");
        // The facts are the facts of the file and not of the symlink.
        assert_eq!(Some(link_facts), facts(&path));

        assert_eq!(
            read_capped(&link, cap(64), Follow::Refuse),
            FileRead::Refused(ReadRefusal::Symlink)
        );
    }

    #[test]
    fn a_symlink_to_no_file_is_absent_or_a_symlink() {
        let root = TempRoot::new().unwrap();
        let link = root.path().join("link.json");
        symlink(root.path().join("no-such-file"), &link).unwrap();

        assert_eq!(
            read_capped(&link, cap(64), Follow::Follow),
            FileRead::Absent
        );
        assert_eq!(
            read_capped(&link, cap(64), Follow::Refuse),
            FileRead::Refused(ReadRefusal::Symlink)
        );
    }

    #[test]
    fn a_symlink_above_the_last_part_is_followed() {
        let root = TempRoot::new().unwrap();
        let dir = root.path().join("chat");
        fs::create_dir(&dir).unwrap();
        fs::write(dir.join("status.json"), b"{}").unwrap();
        let link = root.path().join("link");
        symlink(&dir, &link).unwrap();

        let (bytes, _facts) = taken(read_capped(
            &link.join("status.json"),
            cap(64),
            Follow::Refuse,
        ));

        assert_eq!(bytes, b"{}");
    }

    #[test]
    fn a_chain_of_symlinks_with_no_end_is_a_refusal() {
        let root = TempRoot::new().unwrap();
        let one = root.path().join("one");
        let two = root.path().join("two");
        symlink(&one, &two).unwrap();
        symlink(&two, &one).unwrap();
        let too_many = || {
            FileRead::Refused(ReadRefusal::Unreadable {
                kind: io::Error::from(Errno::LOOP).kind(),
                os_text: String::from(TOO_MANY_LINKS),
            })
        };

        // The last part is a symlink.
        assert_eq!(read_capped(&one, cap(8), Follow::Follow), too_many());
        assert_eq!(
            read_capped(&one, cap(8), Follow::Refuse),
            FileRead::Refused(ReadRefusal::Symlink)
        );

        // The chain is above the last part, and the last part is no symlink.
        let below = one.join("status.json");
        assert_eq!(read_capped(&below, cap(8), Follow::Follow), too_many());
        assert_eq!(read_capped(&below, cap(8), Follow::Refuse), too_many());
    }

    #[test]
    fn a_fifo_does_not_block_and_is_not_a_file() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("fifo");
        make_fifo(&path);

        for follow in [Follow::Follow, Follow::Refuse] {
            let path = path.clone();
            let read = within_the_limit(move || read_capped(&path, cap(8), follow));

            assert_eq!(
                read,
                Some(FileRead::Refused(ReadRefusal::NotAFile)),
                "{follow:?}"
            );
        }
    }

    #[test]
    fn a_device_is_not_a_file() {
        assert_eq!(
            read_capped(Path::new("/dev/null"), cap(8), Follow::Follow),
            FileRead::Refused(ReadRefusal::NotAFile)
        );
    }

    #[test]
    fn a_socket_is_unreadable() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("s.sock");
        let _listener = UnixListener::bind(&path).unwrap();

        for follow in [Follow::Follow, Follow::Refuse] {
            let read = read_capped(&path, cap(8), follow);

            assert!(
                matches!(read, FileRead::Refused(ReadRefusal::Unreadable { .. })),
                "{read:?}"
            );
        }
    }

    #[test]
    fn a_file_with_no_read_bit_is_unreadable() {
        // The superuser reads each file, so the mode refuses nothing there.
        if rustix::process::geteuid().is_root() {
            return;
        }

        let (_root, path) = file_with(b"0123456789abcdef");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();

        assert_eq!(
            read_capped(&path, ByteCap::ONE_MIB, Follow::Refuse),
            FileRead::Refused(ReadRefusal::Unreadable {
                kind: io::ErrorKind::PermissionDenied,
                os_text: String::from("Permission denied"),
            })
        );
    }

    #[test]
    fn a_path_with_a_nul_byte_is_a_refusal_and_no_panic() {
        let read = read_capped(Path::new("/tmp/status\0.json"), cap(8), Follow::Refuse);

        assert_eq!(
            read,
            FileRead::Refused(ReadRefusal::Unreadable {
                kind: io::ErrorKind::InvalidInput,
                os_text: String::from("Invalid argument"),
            })
        );
    }

    #[test]
    fn the_facts_of_a_path_come_from_one_stat_that_follows_a_symlink() {
        let (root, path) = file_with(b"0123456789");
        let link = root.path().join("link.json");
        symlink(&path, &link).unwrap();
        let of_path = facts(&path).unwrap();

        assert_eq!(of_path, FileFacts::from(&fs::metadata(&path).unwrap()));
        assert_eq!(facts(&link), Some(of_path));
        assert_eq!(facts(&root.path().join("no-such-file")), None);
        assert_eq!(facts(Path::new("/tmp/status\0.json")), None);
    }

    #[test]
    fn the_text_of_an_error_is_the_text_of_strerror() {
        // The numbers 2, 13, 17, 20 and 21 have one meaning on Linux and on
        // macOS.
        for (code, text) in [
            (2, "No such file or directory"),
            (13, "Permission denied"),
            (17, "File exists"),
            (20, "Not a directory"),
            (21, "Is a directory"),
        ] {
            let error = io::Error::from_raw_os_error(code);

            assert_eq!(error.to_string(), format!("{text} (os error {code})"));
            assert_eq!(os_text(&error), text);
        }
    }

    #[test]
    fn an_error_that_the_system_did_not_make_keeps_its_text() {
        let made = io::Error::other("the file ended (os error 2)");
        let plain = io::Error::from(io::ErrorKind::UnexpectedEof);

        assert_eq!(os_text(&made), "the file ended (os error 2)");
        assert_eq!(os_text(&plain), plain.to_string());
    }

    /// One difference between this module and a Python copy of the read.
    struct Deviation {
        /// The Python file and the lines of the copy.
        python: &'static str,
        /// What the copy does.
        copy: &'static str,
        /// What this module does.
        here: &'static str,
        /// The check that this module does what the row says.
        holds: fn(),
    }

    const SYMLINK_ASKED_FIRST: &str = "The copy asks if the path is a symlink with a call of \
        its own, before the stat and the open.";

    const SYMLINK_AT_THE_OPEN: &str = "With Follow::Refuse, the open itself refuses a symlink. \
        The check and the open are one call.";

    /// Each difference on purpose between [`read_capped`] and a Python copy.
    /// No vector covers a read of a file, so a row names the Python lines.
    const DEVIATIONS: &[Deviation] = &[
        Deviation {
            python: "attendance/src/attendance/atomic.py:71-82",
            copy: "The copy reads the size with a stat of the path. Then it opens the path for \
                   the read.",
            here: "One open. The facts and the bytes come from that open file.",
            holds: the_facts_are_of_the_open_file_and_not_of_the_path,
        },
        Deviation {
            python: "attendance/src/attendance/atomic.py:76-80",
            copy: "The copy checks the size of the stat. Then it reads the file to its end.",
            here: "The read also stops one byte past the cap, and it refuses a file that gives \
                   that byte.",
            holds: a_read_stops_one_byte_past_the_cap,
        },
        Deviation {
            python: "noticeboard/src/noticeboard/jsonfiles.py:49-52",
            copy: "The open waits for a writer when the path is a FIFO.",
            here: "The open does not wait. A FIFO is not a regular file, and the read refuses \
                   it.",
            holds: a_fifo_is_refused_at_once,
        },
        Deviation {
            python: "noticeboard/src/noticeboard/jsonfiles.py:49-58",
            copy: "The open of a directory fails, and the copy gives the text of the system \
                   error: Is a directory.",
            here: "The open of a directory succeeds. The read refuses it with NotAFile, which \
                   holds no text of the system.",
            holds: a_directory_has_no_system_text,
        },
        Deviation {
            python: "noticeboard/src/noticeboard/jsonfiles.py:49-52",
            copy: "The copy reads a device as a file. The device /dev/null reads as a file \
                   with no byte.",
            here: "A device is not a regular file, and the read refuses it with NotAFile.",
            holds: a_device_is_refused_and_not_read,
        },
        Deviation {
            python: "handover/src/handover/executor/spool.py:311-351",
            copy: "The copy also checks the owner of the open file, and it opens the name \
                   relative to a directory descriptor.",
            here: "read_capped takes a path, and FileFacts holds no owner.",
            holds: the_facts_hold_no_owner,
        },
        Deviation {
            python: "caregiver/src/caregiver/atomic.py:79-82",
            copy: "The copy has no cap. caregiver/src/caregiver/faults.py:116 reads a fault \
                   file of each size with it.",
            here: "Each read has a cap of 1 byte or more. No value of ByteCap means no cap.",
            holds: each_read_has_a_cap,
        },
        Deviation {
            python: "caregiver/src/caregiver/mcp_release.py:890-894",
            copy: "The copy has no cap.",
            here: "Each read has a cap of 1 byte or more. No value of ByteCap means no cap.",
            holds: each_read_has_a_cap,
        },
        Deviation {
            python: "caregiver/src/caregiver/live_manifest.py:158-165",
            copy: SYMLINK_ASKED_FIRST,
            here: SYMLINK_AT_THE_OPEN,
            holds: the_open_refuses_a_symlink,
        },
        Deviation {
            python: "caregiver/src/caregiver/mcp_release.py:832-842",
            copy: SYMLINK_ASKED_FIRST,
            here: SYMLINK_AT_THE_OPEN,
            holds: the_open_refuses_a_symlink,
        },
        Deviation {
            python: "caregiver/src/caregiver/released.py:104-107",
            copy: "The copy takes the bytes of one read call of the cap.",
            here: "The read continues to the end of the file, and it reads one byte past the \
                   cap at most.",
            holds: a_read_stops_one_byte_past_the_cap,
        },
        Deviation {
            python: "chaperone/src/chaperone/family_grants.py:226-236",
            copy: "The cache compares the device, the inode, the size and the time of the last \
                   change.",
            here: "FileFacts also holds the mode. A change of the mode alone makes two values \
                   differ, and a cache on the facts reads the file again.",
            holds: a_new_mode_moves_the_facts,
        },
    ];

    fn the_facts_are_of_the_open_file_and_not_of_the_path() {
        let (root, path) = file_with(b"first");
        let first = facts(&path).unwrap();
        let file = File::open(&path).unwrap();

        // Another writer renames a new file onto the path after the open.
        let next = root.path().join("next");
        fs::write(&next, b"second, and longer").unwrap();
        fs::rename(&next, &path).unwrap();

        let (bytes, read_facts) = taken(read_open(file, cap(64)));

        assert_eq!(bytes, b"first");
        assert_eq!(read_facts, first);
        assert_ne!(Some(read_facts), facts(&path));
    }

    fn a_directory_has_no_system_text() {
        let root = TempRoot::new().unwrap();

        // `NotAFile` has no field, so the refusal holds no text of the
        // system.
        assert_eq!(
            read_capped(root.path(), cap(8), Follow::Follow),
            FileRead::Refused(ReadRefusal::NotAFile)
        );
    }

    fn a_device_is_refused_and_not_read() {
        assert_eq!(
            read_capped(Path::new("/dev/null"), cap(8), Follow::Follow),
            FileRead::Refused(ReadRefusal::NotAFile)
        );
    }

    fn the_facts_hold_no_owner() {
        let (_root, path) = file_with(b"{}");
        let (_bytes, read_facts) = taken(read_capped(&path, cap(8), Follow::Refuse));

        // The pattern names each field of the facts. A new field, for
        // example an owner, does not build here, and the row then changes.
        let FileFacts {
            dev: _,
            ino: _,
            len: _,
            modified_ns: _,
            mode: _,
        } = read_facts;
    }

    fn a_read_stops_one_byte_past_the_cap() {
        /// A source with no end. It counts the bytes that a reader took.
        struct Endless(usize);

        impl Read for Endless {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                buffer.fill(b'x');
                self.0 += buffer.len();

                Ok(buffer.len())
            }
        }

        let mut source = Endless(0);

        assert_eq!(
            take_capped(&mut source, cap(4096)),
            Err(ReadRefusal::TooLarge { cap: cap(4096) })
        );
        assert_eq!(source.0, 4097);
    }

    fn a_fifo_is_refused_at_once() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("fifo");
        make_fifo(&path);
        let read = within_the_limit(move || read_capped(&path, cap(8), Follow::Follow));

        assert_eq!(read, Some(FileRead::Refused(ReadRefusal::NotAFile)));
    }

    fn each_read_has_a_cap() {
        let (_root, path) = file_with(b"0123456789");

        assert_eq!(ByteCap::new(0), None);
        assert_eq!(
            read_capped(&path, cap(9), Follow::Follow),
            FileRead::Refused(ReadRefusal::TooLarge { cap: cap(9) })
        );
    }

    fn the_open_refuses_a_symlink() {
        let (root, path) = file_with(b"{}");
        let link = root.path().join("link.json");
        symlink(&path, &link).unwrap();

        assert_eq!(
            read_capped(&link, cap(64), Follow::Refuse),
            FileRead::Refused(ReadRefusal::Symlink)
        );
        assert!(open_flags(Follow::Refuse).contains(OFlags::NOFOLLOW));
        assert!(!open_flags(Follow::Follow).contains(OFlags::NOFOLLOW));
    }

    fn a_new_mode_moves_the_facts() {
        let (_root, path) = file_with(b"0123456789");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
        let first = facts(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let second = facts(&path).unwrap();

        assert_ne!(first, second);
        assert_eq!(
            (first.dev(), first.ino(), first.len(), first.modified_ns()),
            (
                second.dev(),
                second.ino(),
                second.len(),
                second.modified_ns()
            )
        );
    }

    #[test]
    fn each_deviation_names_its_python_lines_and_holds() {
        for row in DEVIATIONS {
            let (file, lines) = row.python.rsplit_once(':').unwrap();

            assert!(file.ends_with(".py"), "{}", row.python);
            assert!(
                lines
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || byte == b'-'),
                "{}",
                row.python
            );
            assert!(
                !row.copy.is_empty() && !row.here.is_empty(),
                "{}",
                row.python
            );
            assert_ne!(row.copy, row.here, "{}", row.python);

            (row.holds)();
        }
    }

    #[test]
    fn the_open_never_blocks_and_gives_no_descriptor_to_a_child() {
        for follow in [Follow::Follow, Follow::Refuse] {
            let flags = open_flags(follow);

            assert!(flags.contains(OFlags::NONBLOCK | OFlags::CLOEXEC | OFlags::NOCTTY));
            assert!(!flags.intersects(OFlags::WRONLY | OFlags::RDWR | OFlags::CREATE));
        }
    }
}
