//! The guest init initramfs that the vmm writes once, at its own start
//! (decisions CH7-6, CH62 rules 17 and 18).
//!
//! `VMM_GUEST_INIT_PROGRAM` names the `guest-init` file of the vmm row,
//! `/opt/components/vmm/bin/guest-init` (CH62 rule 17). The row signature
//! covers that file, so the vmm and guest init come from one release
//! (CH7-6). `write_initramfs` opens the file once and runs each check on
//! the open file, as `image_root::read` checks `digests.json`: a regular
//! file, owned by root, that no group or other user may write. So the bytes
//! that it packs are the bytes that it checked. A failed check refuses the
//! start of the vmm, and `main.rs` exits 78 (owner item 86).
//!
//! CONTRACT-QUESTION (CH62 rule 17, `AGENTS.md` Known gap 18): the rule
//! says "checked as `VMM_CH_PROGRAM` is", but the config checks that key
//! for its form only. This module takes the text of gap Q54-17 option A
//! (root-owned, not writable by david) for guest init alone.
//!
//! The archive holds `dev`, `dev/console` 5:1, which the kernel opens for
//! PID 1 before any devtmpfs mount, and `/init`, the guest init program
//! with mode 0500. The kernel runs `/init` (`rdinit=/init`, CH62 rule 19).
//! `creche_vmm_store::initramfs::pack` writes it reproducibly, so one guest
//! init build gives one `store/<sha256>` across vmm restarts (CH62 rule
//! 18). `ImageStore::put_made` puts it into the digest store, so the slot
//! profiles' `store/*` rule covers it (decision CH60 rule 2). Each VM
//! start, cold boot or restore, names that one file (CH61 rule 3, CH62
//! rule 18).

use std::fmt;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use creche_vmm_store::images::{CheckedImage, ImageStore, MadeError};
use creche_vmm_store::initramfs::{Entry, Kind, PackError, pack};

use crate::config::GUEST_INIT;
use crate::image_root::{GROUP_OR_OTHER_WRITE, ROOT_UID};

/// The mode of `/init`: root reads and runs it, and no one else may.
const INIT_MODE: u32 = 0o500;

/// The device number of `/dev/console`: the system console, 5:1.
const CONSOLE: (u32, u32) = (5, 1);

/// Why the vmm wrote no guest init initramfs. Each refuses the start.
#[derive(Debug)]
pub enum GuestInitError {
    /// An open, a look or a read of the program failed. A missing file
    /// gives `NotFound`.
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
    /// The archive did not pack: the program passes the newc size limit.
    Pack(PackError),
    /// The archive did not go into the digest store.
    Store(MadeError),
}

impl fmt::Display for GuestInitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{GUEST_INIT}: ")?;
        match self {
            Self::Io(error) => write!(formatter, "the program cannot be read: {error}"),
            Self::NotRegular => formatter.write_str("the program is not a regular file"),
            Self::Replaced => formatter.write_str("the program changed during the read"),
            Self::NotOwned { uid } => {
                write!(formatter, "the program is owned by uid {uid}, not by root")
            }
            Self::Writable { mode } => write!(
                formatter,
                "the program has mode {mode:04o}, so others may write it"
            ),
            Self::Pack(error) => write!(formatter, "the program does not pack: {error}"),
            Self::Store(error) => write!(formatter, "the initramfs is not stored: {error}"),
        }
    }
}

impl std::error::Error for GuestInitError {}

/// Reads the guest init `program`, packs the initramfs and puts it into
/// `store`. Blocks. The compose step runs it once per vmm start.
///
/// # Errors
///
/// A `GuestInitError`. Each one refuses the start of the vmm.
pub fn write_initramfs(program: &Path, store: &ImageStore) -> Result<CheckedImage, GuestInitError> {
    let bytes = read_program(program)?;
    let archive = pack(&entries(bytes)).map_err(GuestInitError::Pack)?;
    store.put_made(&archive).map_err(GuestInitError::Store)
}

/// The entries of the archive, in order: each parent dir comes before
/// its entries.
fn entries(program: Vec<u8>) -> [Entry; 3] {
    let (major, minor) = CONSOLE;
    [
        Entry {
            name: "dev".to_owned(),
            mode: 0o755,
            kind: Kind::Dir,
        },
        Entry {
            name: "dev/console".to_owned(),
            mode: 0o600,
            kind: Kind::CharDevice { major, minor },
        },
        Entry {
            name: "init".to_owned(),
            mode: INIT_MODE,
            kind: Kind::File(program),
        },
    ]
}

/// The bytes of `program` after the checks of the module doc.
fn read_program(program: &Path) -> Result<Vec<u8>, GuestInitError> {
    let first = fs::symlink_metadata(program).map_err(GuestInitError::Io)?;
    if !first.file_type().is_file() {
        return Err(GuestInitError::NotRegular);
    }
    let file = File::open(program).map_err(GuestInitError::Io)?;
    let open = file.metadata().map_err(GuestInitError::Io)?;
    if (open.dev(), open.ino()) != (first.dev(), first.ino()) || !open.file_type().is_file() {
        return Err(GuestInitError::Replaced);
    }
    if open.uid() != ROOT_UID {
        return Err(GuestInitError::NotOwned { uid: open.uid() });
    }
    if open.mode() & GROUP_OR_OTHER_WRITE != 0 {
        return Err(GuestInitError::Writable {
            mode: open.mode() & 0o7777,
        });
    }
    // A newc header names at most u32::MAX bytes, and `pack` refuses a
    // larger file. So the read stops one byte past that limit.
    let mut bytes = Vec::new();
    file.take(u64::from(u32::MAX).saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(GuestInitError::Io)?;
    Ok(bytes)
}

#[cfg(test)]
mod tests;
