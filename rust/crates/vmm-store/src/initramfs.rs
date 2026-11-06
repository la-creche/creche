//! The newc archive of the guest init initramfs (decision CH7-6). The vmm
//! writes one archive at its own start, and each VM start names that file
//! (decision CH62 rule 18). `ImageStore::put_made` puts it into the store
//! as `store/<sha256>`, so the slot profiles' `store/*` rule covers it
//! (decision CH60 rule 2).
//!
//! The archive is reproducible: equal entries give equal bytes, so one
//! guest init build gives one digest across vmm restarts (CH62 rule 18).
//! Each header has uid 0, gid 0 and mtime 0, and the inode numbers count
//! the entries in order from 1. The format is the `070701` newc format with
//! no checksum, which the kernel unpacks (`init/initramfs.c`).

use std::collections::HashSet;
use std::fmt;

/// The magic of a newc header with no checksum.
const MAGIC: &[u8] = b"070701";

/// The name of the entry that ends a newc archive.
const TRAILER: &str = "TRAILER!!!";

/// The largest name size of a header, the NUL included. The kernel skips
/// an entry with a larger one (`name_len > PATH_MAX` in `do_header`).
pub const NAME_SIZE_LIMIT: usize = 4096;

/// The file type bits of a dir, a regular file and a char device.
const DIR_TYPE: u32 = 0o040_000;
const FILE_TYPE: u32 = 0o100_000;
const CHAR_TYPE: u32 = 0o020_000;

/// The permission bits that a mode may hold.
const PERMISSION_BITS: u32 = 0o7777;

/// What one entry of the archive is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Kind {
    /// A dir.
    Dir,
    /// A regular file with these bytes.
    File(Vec<u8>),
    /// A char device node with this device number.
    CharDevice {
        /// The major number.
        major: u32,
        /// The minor number.
        minor: u32,
    },
}

/// One entry of the archive.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// The path in the guest root, with no leading `/`, such as
    /// `dev/console`. Its parent dir is an earlier entry.
    pub name: String,
    /// The permission bits, at most 0o7777.
    pub mode: u32,
    /// What the entry is.
    pub kind: Kind,
}

/// Why `pack` made no archive.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// The name is empty, holds a NUL, starts with `/`, holds an empty, a
    /// `.` or a `..` component, or is the trailer name.
    BadName,
    /// The name size, the NUL included, passes `NAME_SIZE_LIMIT`.
    NameTooLong,
    /// No earlier entry is the parent dir of the name.
    NoParent,
    /// An earlier entry has the same name.
    Duplicate,
    /// The mode holds bits other than the permission bits.
    BadMode,
    /// A number passes the 8 hex digits of a header field: the size of a
    /// file, or the inode number of an entry past 2^32 - 1.
    TooLarge,
}

/// The entry that `pack` refused, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackError {
    /// The name of the refused entry.
    pub name: String,
    /// Why `pack` refused it.
    pub reason: Reason,
}

impl fmt::Display for PackError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let why = match self.reason {
            Reason::BadName => "is not a relative path of plain components",
            Reason::NameTooLong => "passes the name size limit of 4096 bytes",
            Reason::NoParent => "has no earlier dir entry as its parent",
            Reason::Duplicate => "is in the archive twice",
            Reason::BadMode => "has a mode with bits past 0o7777",
            Reason::TooLarge => "has a number past 8 hex digits",
        };
        // `{:?}`: a bad name may hold a NUL or a line end.
        write!(formatter, "the initramfs entry {:?} {why}", self.name)
    }
}

impl std::error::Error for PackError {}

/// Writes `entries` in their order as a newc archive with its trailer.
///
/// # Errors
///
/// A `PackError` names the first entry that the kernel would skip or
/// unpack wrong (see `Reason`).
pub fn pack(entries: &[Entry]) -> Result<Vec<u8>, PackError> {
    let mut out = Vec::new();
    let mut names = HashSet::new();
    let mut dirs = HashSet::new();
    for (index, entry) in entries.iter().enumerate() {
        let refuse = |reason| PackError {
            name: entry.name.clone(),
            reason,
        };
        let name = entry.name.as_str();
        check_name(name).map_err(refuse)?;
        if let Some((parent, _)) = name.rsplit_once('/')
            && !dirs.contains(parent)
        {
            return Err(refuse(Reason::NoParent));
        }
        if !names.insert(name) {
            return Err(refuse(Reason::Duplicate));
        }
        if entry.mode & !PERMISSION_BITS != 0 {
            return Err(refuse(Reason::BadMode));
        }
        let inode = u32::try_from(index.saturating_add(1)).map_err(|_| refuse(Reason::TooLarge))?;
        let (file_type, links, data, (major, minor)) = match &entry.kind {
            Kind::Dir => {
                dirs.insert(name);
                (DIR_TYPE, 2, &[][..], (0, 0))
            }
            Kind::File(bytes) => (FILE_TYPE, 1, bytes.as_slice(), (0, 0)),
            Kind::CharDevice { major, minor } => (CHAR_TYPE, 1, &[][..], (*major, *minor)),
        };
        let size = u32::try_from(data.len()).map_err(|_| refuse(Reason::TooLarge))?;
        let header = Header {
            inode,
            mode: file_type | entry.mode,
            links,
            size,
            device: (major, minor),
        };
        header.write(name, &mut out);
        out.extend_from_slice(data);
        pad(&mut out);
    }
    let trailer = Header {
        inode: 0,
        mode: 0,
        links: 1,
        size: 0,
        device: (0, 0),
    };
    trailer.write(TRAILER, &mut out);
    Ok(out)
}

/// Checks `name` against the rules of `Reason::BadName` and
/// `Reason::NameTooLong`.
fn check_name(name: &str) -> Result<(), Reason> {
    if name.len().saturating_add(1) > NAME_SIZE_LIMIT {
        return Err(Reason::NameTooLong);
    }
    let plain = |part: &str| !part.is_empty() && part != "." && part != "..";
    if name == TRAILER || name.contains('\0') || !name.split('/').all(plain) {
        return Err(Reason::BadName);
    }
    Ok(())
}

/// The fields of one header that differ between entries. Every header
/// has uid 0, gid 0, mtime 0, the filesystem device 0:0 and check 0.
struct Header {
    inode: u32,
    /// The mode with the file type bits.
    mode: u32,
    links: u32,
    size: u32,
    /// The device number of a char device, else 0:0.
    device: (u32, u32),
}

impl Header {
    /// Appends the header, `name` with its NUL, and the pad to 4 bytes.
    /// The archive starts at offset 0 and each entry ends on a 4 byte
    /// boundary, so the pad of `out` is the pad of the entry.
    fn write(&self, name: &str, out: &mut Vec<u8>) {
        let name_size = name.len().saturating_add(1);
        out.extend_from_slice(MAGIC);
        let (major, minor) = self.device;
        for field in [
            self.inode, self.mode, 0, 0, self.links, 0, self.size, 0, 0, major, minor,
        ] {
            out.extend_from_slice(format!("{field:08x}").as_bytes());
        }
        // `check_name` holds every name size under 4096, and the trailer
        // name is short, so the size fits in 8 hex digits.
        out.extend_from_slice(format!("{name_size:08x}").as_bytes());
        out.extend_from_slice(b"00000000");
        out.extend_from_slice(name.as_bytes());
        out.push(0);
        pad(out);
    }
}

/// Pads `out` with NUL bytes to a multiple of 4 bytes.
fn pad(out: &mut Vec<u8>) {
    out.resize(out.len().next_multiple_of(4), 0);
}

#[cfg(test)]
mod tests {
    use sha2::{Digest, Sha256};

    use super::{Entry, Kind, NAME_SIZE_LIMIT, PackError, Reason, pack};

    fn entry(name: &str, mode: u32, kind: Kind) -> Entry {
        Entry {
            name: name.to_owned(),
            mode,
            kind,
        }
    }

    /// A dir, a char device in it and a file: one entry of each kind.
    fn entries() -> Vec<Entry> {
        vec![
            entry("dev", 0o755, Kind::Dir),
            entry(
                "dev/console",
                0o600,
                Kind::CharDevice { major: 5, minor: 1 },
            ),
            entry("init", 0o500, Kind::File(b"hi!".to_vec())),
        ]
    }

    fn refusal(entries: &[Entry]) -> Reason {
        pack(entries).expect_err("a refusal").reason
    }

    #[test]
    fn equal_entries_give_an_equal_digest() {
        let first = pack(&entries()).unwrap();
        let second = pack(&entries()).unwrap();
        assert_eq!(first, second);
        assert_eq!(Sha256::digest(&first), Sha256::digest(&second));

        let mut other = entries();
        other[2].mode = 0o555;
        assert_ne!(
            Sha256::digest(pack(&other).unwrap()),
            Sha256::digest(&first)
        );
    }

    #[test]
    fn the_archive_has_each_header_field_and_the_trailer() {
        // Each header: magic, then inode, mode, uid, gid, links, mtime,
        // size, device major and minor, rdev major and minor, name size
        // and check, each 8 hex digits. Then the name, its NUL and the pad
        // to 4 bytes, then the data and its pad.
        let zero = "00000000";
        #[rustfmt::skip]
        let expected = [
            "070701", "00000001", "000041ed", zero, zero, "00000002", zero, zero,
            zero, zero, zero, zero, "00000004", zero, "dev\0", "\0\0",
            "070701", "00000002", "00002180", zero, zero, "00000001", zero, zero,
            zero, zero, "00000005", "00000001", "0000000c", zero, "dev/console\0", "\0\0",
            "070701", "00000003", "00008140", zero, zero, "00000001", zero, "00000003",
            zero, zero, zero, zero, "00000005", zero, "init\0", "\0", "hi!", "\0",
            "070701", zero, zero, zero, zero, "00000001", zero, zero,
            zero, zero, zero, zero, "0000000b", zero, "TRAILER!!!\0", "\0\0\0",
        ]
        .concat();
        let archive = pack(&entries()).unwrap();
        assert_eq!(String::from_utf8(archive).unwrap(), expected);
    }

    #[test]
    fn a_name_over_the_newc_limit_is_refused() {
        let longest = "a".repeat(NAME_SIZE_LIMIT - 1);
        pack(&[entry(&longest, 0o644, Kind::File(Vec::new()))]).unwrap();

        let over = "a".repeat(NAME_SIZE_LIMIT);
        let error = pack(&[entry(&over, 0o644, Kind::File(Vec::new()))]).unwrap_err();
        assert_eq!(
            error,
            PackError {
                name: over,
                reason: Reason::NameTooLong
            }
        );
    }

    #[test]
    fn a_bad_name_is_refused() {
        for name in [
            "",
            "/init",
            "./init",
            "dev//console",
            "dev/../init",
            "dev/",
            "in\0it",
            "TRAILER!!!",
        ] {
            let entries = [
                entry("dev", 0o755, Kind::Dir),
                entry(name, 0o644, Kind::Dir),
            ];
            assert_eq!(refusal(&entries), Reason::BadName, "{name:?}");
        }
    }

    #[test]
    fn an_entry_needs_an_earlier_parent_dir() {
        let file = |name: &str| entry(name, 0o644, Kind::File(Vec::new()));
        assert_eq!(refusal(&[file("dev/console")]), Reason::NoParent);
        assert_eq!(
            refusal(&[file("dev"), file("dev/console")]),
            Reason::NoParent
        );
        assert_eq!(
            refusal(&[file("dev/console"), entry("dev", 0o755, Kind::Dir)]),
            Reason::NoParent
        );

        // The parent is the whole name before the last slash, not the
        // first component.
        let dir = |name: &str| entry(name, 0o755, Kind::Dir);
        assert_eq!(refusal(&[dir("a"), file("a/b/c")]), Reason::NoParent);
        pack(&[dir("a"), dir("a/b"), file("a/b/c")]).unwrap();
    }

    #[test]
    fn a_duplicate_name_or_a_bad_mode_is_refused() {
        let dir = entry("dev", 0o755, Kind::Dir);
        assert_eq!(refusal(&[dir.clone(), dir]), Reason::Duplicate);
        assert_eq!(
            refusal(&[entry("dev", 0o10_000, Kind::Dir)]),
            Reason::BadMode
        );
    }
}
