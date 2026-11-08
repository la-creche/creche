//! `vmm verify`, the verify hook of the vmm row (contract 06 §4, decision
//! CH76 rule 2). handover runs it after each vmm release, also the first,
//! when the unit is not installed and no disk exists.
//!
//! It runs the start checks of the programs and nothing more:
//! 1. It reads the env file of the unit (`crate::env_file`) and the three
//!    program keys from it (`VerifyConfig`). The vmm compiles in no
//!    default (decision CH59 rule 1), so the hook needs the file.
//! 2. It runs `root_owned::check_program` on the vmm program itself, which
//!    has no key, and on the file of each key (decision CH67 rule 1).
//! 3. It runs `check_cloud_hypervisor` on a trusted CH (CH36 G10).
//! 4. It checks that a trusted guest init is a static x86_64 ELF program.
//!
//! It opens no socket, reads no disk, writes no file and starts no VM. A
//! check whose file is not trusted runs nothing of that file: its later
//! check fails and names the check that it needs. `Report` is the one
//! JSON line of stdout, which `vmm check-images` writes too. `src/main.rs`
//! exits 0 when every check passes, else 1.

use std::fmt;
use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt as _;
use std::path::Path;

use creche_vmm_store::key::check_cloud_hypervisor;
use serde::Serialize;

use crate::compose::VERSION_WAIT;
use crate::config::{CH, CH_REMOTE, GUEST_INIT, VerifyConfig};
use crate::env_file;
use crate::root_owned::{self, OwnerError};

/// The name of the check of the env file and its keys.
pub const ENV_FILE: &str = "env-file";
/// The name of the check of the vmm program itself.
pub const VMM: &str = "vmm";
/// The name of the CH version check.
pub const CH_VERSION: &str = "ch-version";
/// The name of the check that guest init is a static ELF program.
pub const GUEST_INIT_STATIC: &str = "guest-init-static";

/// The one JSON object that a hook writes on stdout: `ok` when every
/// check passed, and each check in its order.
#[derive(Debug, Serialize)]
pub struct Report {
    ok: bool,
    checks: Vec<Check>,
}

/// One check of a hook.
#[derive(Debug, Serialize)]
pub struct Check {
    name: &'static str,
    ok: bool,
    detail: String,
}

impl Check {
    /// A passed check.
    #[must_use]
    pub fn pass(name: &'static str, detail: impl fmt::Display) -> Self {
        Self {
            name,
            ok: true,
            detail: detail.to_string(),
        }
    }

    /// A failed check.
    #[must_use]
    pub fn fail(name: &'static str, detail: impl fmt::Display) -> Self {
        Self {
            name,
            ok: false,
            detail: detail.to_string(),
        }
    }
}

impl Report {
    /// The report of `checks`.
    #[must_use]
    pub fn new(checks: Vec<Check>) -> Self {
        Self {
            ok: checks.iter().all(|check| check.ok),
            checks,
        }
    }

    /// Whether every check passed.
    #[must_use]
    pub fn passed(&self) -> bool {
        self.ok
    }

    /// The report as one line of JSON, with no line end.
    ///
    /// # Errors
    ///
    /// A failed serialization.
    pub fn line(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

/// Runs the checks of the module doc with the env file at `env_file`.
/// Blocks.
#[must_use]
pub fn run(env_file: &Path) -> Report {
    let config = env_file::read(env_file)
        .map_err(|error| error.to_string())
        .and_then(|env| {
            VerifyConfig::from_lookup(|key| env.lookup(key)).map_err(|error| error.to_string())
        });
    let config = match config {
        Ok(config) => config,
        Err(error) => return Report::new(vec![Check::fail(ENV_FILE, error)]),
    };
    let mut checks = vec![Check::pass(ENV_FILE, env_file.display())];
    checks.push(match std::env::current_exe() {
        Ok(path) => owner_check(VMM, &path, root_owned::open(&path).err().as_ref()),
        Err(error) => Check::fail(VMM, format_args!("cannot find the program: {error}")),
    });
    let ch = config.ch.path();
    let ch_fault = root_owned::open(ch).err();
    checks.push(owner_check(CH, ch, ch_fault.as_ref()));
    let ch_remote = config.ch_remote.path();
    let ch_remote_fault = root_owned::open(ch_remote).err();
    checks.push(owner_check(CH_REMOTE, ch_remote, ch_remote_fault.as_ref()));
    let guest_init = config.guest_init.path();
    let guest_file = root_owned::open(guest_init);
    checks.push(owner_check(
        GUEST_INIT,
        guest_init,
        guest_file.as_ref().err(),
    ));

    // Decision CH67 rule 1: the hook runs no CH that it does not trust.
    checks.push(match ch_fault {
        None => match check_cloud_hypervisor(ch, VERSION_WAIT) {
            Ok(_) => Check::pass(CH_VERSION, creche_vmm_store::key::CH_VERSION),
            Err(error) => Check::fail(CH_VERSION, error),
        },
        Some(_) => Check::fail(CH_VERSION, format_args!("not run: {CH} failed")),
    });
    // The bytes that it reads are the bytes that the owner check checked.
    checks.push(match guest_file {
        Ok(file) => match static_elf(&file) {
            Ok(()) => Check::pass(GUEST_INIT_STATIC, "a static x86_64 ELF program"),
            Err(fault) => Check::fail(GUEST_INIT_STATIC, fault),
        },
        Err(_) => Check::fail(
            GUEST_INIT_STATIC,
            format_args!("not run: {GUEST_INIT} failed"),
        ),
    });
    Report::new(checks)
}

/// The check `name` of the program at `path`: its path when root owns it,
/// else the fault.
fn owner_check(name: &'static str, path: &Path, fault: Option<&OwnerError>) -> Check {
    match fault {
        None => Check::pass(name, path.display()),
        Some(fault) => Check::fail(
            name,
            format_args!("{}: the program {fault}", path.display()),
        ),
    }
}

/// Why guest init is not a static x86_64 ELF program.
#[derive(Debug, PartialEq, Eq)]
pub enum ElfFault {
    /// A read of the file failed.
    Read(io::ErrorKind),
    /// The file does not start with the ELF magic.
    NotElf,
    /// The file is not a 64-bit little-endian ELF file.
    NotElf64,
    /// The file is no program: its type is not `ET_EXEC` or `ET_DYN`.
    NotProgram(u16),
    /// The program is not for x86_64, the machine of the guest kernel.
    NotX86_64(u16),
    /// The program header table does not fit the file.
    BadHeaders,
    /// A `PT_INTERP` header names a loader, and the guest has none.
    Dynamic,
}

impl fmt::Display for ElfFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(kind) => write!(formatter, "cannot be read: {kind}"),
            Self::NotElf => formatter.write_str("is not an ELF file"),
            Self::NotElf64 => formatter.write_str("is not a 64-bit little-endian ELF file"),
            Self::NotProgram(kind) => write!(formatter, "has the ELF type {kind}, not a program"),
            Self::NotX86_64(machine) => {
                write!(formatter, "is for the ELF machine {machine}, not x86_64")
            }
            Self::BadHeaders => formatter.write_str("has a program header table past its end"),
            Self::Dynamic => formatter.write_str("has a PT_INTERP header, so it is not static"),
        }
    }
}

/// The ELF header of a 64-bit file, in bytes.
const EHDR: usize = 64;
/// `\x7fELF`, then `ELFCLASS64` and `ELFDATA2LSB`.
const IDENT: [u8; 6] = [0x7f, b'E', b'L', b'F', 2, 1];
/// `ET_EXEC` and `ET_DYN`: a static-pie musl build is `ET_DYN`.
const PROGRAM_TYPES: [u16; 2] = [2, 3];
/// `EM_X86_64`.
const X86_64: u16 = 62;
/// The `p_type` of a loader header.
const PT_INTERP: u32 = 3;
/// The size of one 64-bit program header.
const PHDR: u16 = 56;
/// `PN_XNUM`: the count sits in a section header, which no static
/// program of the vmm row needs.
const PN_XNUM: u16 = 0xffff;

/// Checks that `file` is a static x86_64 ELF program: a 64-bit
/// little-endian `ET_EXEC` or `ET_DYN` file for `EM_X86_64` with no
/// `PT_INTERP` header. `bin/vm-smoke-setup.sh` runs the same loader rule
/// on the CI build.
///
/// # Errors
///
/// The first `ElfFault`.
pub fn static_elf(file: &File) -> Result<(), ElfFault> {
    let mut header = [0_u8; EHDR];
    read_at(file, &mut header, 0)?;
    if !header.starts_with(&IDENT[..4]) {
        return Err(ElfFault::NotElf);
    }
    if !header.starts_with(&IDENT) {
        return Err(ElfFault::NotElf64);
    }
    let kind = u16_at(&header, 0x10)?;
    if !PROGRAM_TYPES.contains(&kind) {
        return Err(ElfFault::NotProgram(kind));
    }
    let machine = u16_at(&header, 0x12)?;
    if machine != X86_64 {
        return Err(ElfFault::NotX86_64(machine));
    }
    let table = u64::from_le_bytes(bytes_at(&header, 0x20)?);
    let size = u16_at(&header, 0x36)?;
    let count = u16_at(&header, 0x38)?;
    if size != PHDR || count == PN_XNUM {
        return Err(ElfFault::BadHeaders);
    }
    let mut entry = [0_u8; 4];
    for index in 0..u64::from(count) {
        let offset = index
            .checked_mul(u64::from(PHDR))
            .and_then(|step| step.checked_add(table))
            .ok_or(ElfFault::BadHeaders)?;
        read_at(file, &mut entry, offset)?;
        if u32::from_le_bytes(entry) == PT_INTERP {
            return Err(ElfFault::Dynamic);
        }
    }
    Ok(())
}

/// Reads `buffer.len()` bytes at `offset`. A short file is `BadHeaders`
/// past the ELF header, and `NotElf` inside it.
fn read_at(file: &File, buffer: &mut [u8], offset: u64) -> Result<(), ElfFault> {
    file.read_exact_at(buffer, offset)
        .map_err(|error| match (error.kind(), offset) {
            (io::ErrorKind::UnexpectedEof, 0) => ElfFault::NotElf,
            (io::ErrorKind::UnexpectedEof, _) => ElfFault::BadHeaders,
            (kind, _) => ElfFault::Read(kind),
        })
}

/// The `N` bytes of `header` at `at`.
fn bytes_at<const N: usize>(header: &[u8], at: usize) -> Result<[u8; N], ElfFault> {
    header
        .get(at..at.saturating_add(N))
        .and_then(|bytes| bytes.try_into().ok())
        .ok_or(ElfFault::NotElf64)
}

/// The little-endian `u16` of `header` at `at`.
fn u16_at(header: &[u8], at: usize) -> Result<u16, ElfFault> {
    bytes_at(header, at).map(u16::from_le_bytes)
}

#[cfg(test)]
mod tests;
