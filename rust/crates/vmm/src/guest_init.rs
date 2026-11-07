//! The guest init initramfs that the vmm writes once, at its own start
//! (decisions CH7-6, CH62 rules 17 and 18).
//!
//! `VMM_GUEST_INIT_PROGRAM` names the `guest-init` file of the vmm row,
//! `/opt/components/vmm/bin/guest-init` (CH62 rule 17). The row signature
//! covers that file, so the vmm and guest init come from one release
//! (CH7-6). `write_initramfs` opens the file once with
//! `root_owned::open_program`, the one check of the three program keys
//! (decision CH67 rule 1) and of `digests.json` (CH57 rule 1): a regular
//! file, not a symlink, owned by root, that no group or other user may
//! write. It reads the open file, so the bytes that it packs are
//! the bytes that it checked. A failed check refuses the start of the
//! vmm, and `main.rs` exits 78 (owner item 86).
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
use std::io::Read;
use std::path::Path;

use creche_vmm_store::images::{CheckedImage, ImageStore, MadeError};
use creche_vmm_store::initramfs::{Entry, Kind, PackError, pack};

use crate::config::GUEST_INIT;
use crate::root_owned::{OwnerError, ProgramError, open_program};

/// The mode of `/init`: root reads and runs it, and no one else may.
const INIT_MODE: u32 = 0o500;

/// The device number of `/dev/console`: the system console, 5:1.
const CONSOLE: (u32, u32) = (5, 1);

/// Why the vmm wrote no guest init initramfs. Each refuses the start.
#[derive(Debug)]
pub enum GuestInitError {
    /// The program failed a check of `root_owned::open`, or its read
    /// failed.
    Program(ProgramError),
    /// The archive did not pack: the program passes the newc size limit.
    Pack(PackError),
    /// The archive did not go into the digest store.
    Store(MadeError),
}

impl fmt::Display for GuestInitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Program(error) => write!(formatter, "{error}"),
            Self::Pack(error) => {
                write!(
                    formatter,
                    "{GUEST_INIT}: the program does not pack: {error}"
                )
            }
            Self::Store(error) => {
                write!(
                    formatter,
                    "{GUEST_INIT}: the initramfs is not stored: {error}"
                )
            }
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
    let file = open_program(GUEST_INIT, program).map_err(GuestInitError::Program)?;
    // A newc header names at most u32::MAX bytes, and `pack` refuses a
    // larger file. So the read stops one byte past that limit.
    let mut bytes = Vec::new();
    file.take(u64::from(u32::MAX).saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| {
            GuestInitError::Program(ProgramError {
                key: GUEST_INIT,
                fault: OwnerError::Io(error),
            })
        })?;
    Ok(bytes)
}

#[cfg(test)]
mod tests;
