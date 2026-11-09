//! The fault file of the vmm, one per family (decision CH21-1, gap G18 of
//! the phase 2 plan, taken by decision CH36).
//!
//! The vmm is the third writer of contract 05 §3.3.1. It reports the two
//! start faults of decision CH6-3 and the serial log cap of decision CH79
//! rule 7 in `<faults root>/vmm/<family>.json`, in the shape of §3.3.1,
//! with `source` `vmm`. The rules of §3.3.1 hold:
//!
//! 1. The file holds the open faults of the vmm for that family.
//! 2. Each change rewrites the whole file. An empty list clears.
//! 3. A write goes to a temporary file in the same directory, then
//!    `fsync`, then `rename`.
//! 4. While a fault is open, lifecycle calls `FaultBook::refresh` every
//!    `REFRESH`, so `written_at` never passes the 90 s of rule 7.
//! 5. A `ControlReady` of the family clears its faults (rule 8, a
//!    completed handshake). The vmm start clears every file (rule 8).
//!
//! Every time is RFC 3339 in UTC with `Z`, to the second (root
//! `AGENTS.md`, Time), from `rfc3339::format`.
//!
//! Contract 05 0.18.0 gives the vmm its row of §3.3.1 (decisions CH21-1,
//! CH36 G18). Its group is the user of caregiver. The vmm sets the modes
//! only, and the group is the one the directory gets on the host.
//! caregiver folds the file into the status document. Its test reads
//! `tests/fixtures/caregiver_fault.json`, the bytes of
//! `the_bytes_match_the_caregiver_fixture`.
//!
//! No vmm fault stops turns (decision CH82 rules 2 and 6): each entry
//! carries `blocks_turns` false. The next turn tries a start, and each
//! start reruns every check, which fails closed.

use std::collections::BTreeMap;
use std::fs::{self, DirBuilder, File, OpenOptions, Permissions};
use std::io::{self, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, SystemTime};

use creche_wire::FamilyName;
use serde::Serialize;

use crate::rfc3339;

/// The name of the writer: the directory under the faults root and the
/// `source` of each fault.
pub const SOURCE: &str = "vmm";

/// How often lifecycle rewrites a file with an open fault. Contract 05
/// §3.3.1 rule 7 makes a file stale after 90 s, and the PEP refreshes
/// every 30 s.
pub const REFRESH: Duration = Duration::from_secs(30);

/// The end of the temporary file of a write.
const TEMP: &str = ".tmp";

/// The mode of `<faults root>/vmm`. The group may list it.
const DIR_MODE: u32 = 0o750;

/// The mode of each fault file. The group may read it.
const FILE_MODE: u32 = 0o640;

/// A fault that the vmm raises (decisions CH6-3, CH79 rule 7, CH82 rule
/// 1). None stops turns (decision CH82 rules 2 and 6).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FaultCode {
    /// The VM did not boot or restore, or guest init sent no `ControlReady`.
    SandboxStartFailed,
    /// The VM failed the deny-by-default check of `deny::check`.
    EgressAssertFailed,
    /// The serial log of a VM passed its cap, so the vmm stopped the VM.
    SerialLogFull,
}

/// One entry of the file: the fault object of contract 05 §3.3.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct Entry {
    code: FaultCode,
    blocks_turns: bool,
    since: String,
    source: &'static str,
    message: String,
}

/// The whole file (contract 05 §3.3.1).
#[derive(Debug, Serialize)]
struct Body<'a> {
    family: &'a FamilyName,
    source: &'static str,
    written_at: String,
    faults: &'a [Entry],
}

/// The directory `<faults root>/vmm`. The vmm is its one writer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FaultDir(PathBuf);

impl FaultDir {
    /// Makes `<faults_root>/vmm` with mode 0750 when it is missing. The
    /// faults root must exist.
    ///
    /// # Errors
    ///
    /// `InvalidInput` when `faults_root` is not absolute or has a `..`
    /// component, else the error of the directory create.
    pub fn open(faults_root: &Path) -> io::Result<Self> {
        if !faults_root.is_absolute() || faults_root.components().any(|c| c == Component::ParentDir)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "the faults root is an absolute path with no `..` component",
            ));
        }
        let dir = faults_root.join(SOURCE);
        match DirBuilder::new().mode(DIR_MODE).create(&dir) {
            Err(error) if error.kind() != io::ErrorKind::AlreadyExists => return Err(error),
            _ => {}
        }
        // The umask masks the mode of the create, and several house units
        // set UMask=0077. So the mode is set again, whatever the umask
        // says, as attendance's ensure_dir does.
        fs::set_permissions(&dir, Permissions::from_mode(DIR_MODE))?;
        Ok(Self(dir))
    }

    /// The fault file of `family`.
    #[must_use]
    pub fn file(&self, family: &FamilyName) -> PathBuf {
        self.0.join(format!("{family}.json"))
    }

    /// Rewrites the file of `family` with `faults`, atomically (contract
    /// 05 §3.3.1 rule 3). The file gets mode 0640.
    fn write(&self, family: &FamilyName, faults: &[Entry], now: SystemTime) -> io::Result<()> {
        let body = Body {
            family,
            source: SOURCE,
            written_at: rfc3339::format(now),
            faults,
        };
        let mut bytes = serde_json::to_vec_pretty(&body).map_err(io::Error::other)?;
        bytes.push(b'\n');
        let path = self.file(family);
        let temp = self.0.join(format!(".{family}.json{TEMP}"));
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(FILE_MODE)
            .open(&temp)?;
        // The umask masks the mode of the open too, so fchmod sets it.
        file.set_permissions(Permissions::from_mode(FILE_MODE))?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&temp, &path)?;
        File::open(&self.0)?.sync_all()
    }
}

/// The open faults of the vmm, per family. Lifecycle holds one book for
/// the life of the vmm.
#[derive(Debug)]
pub struct FaultBook {
    dir: FaultDir,
    open: BTreeMap<FamilyName, Vec<Entry>>,
}

impl FaultBook {
    /// The book of a vmm start. It writes an empty list into every fault
    /// file of the directory and removes a temporary file that a crash
    /// left (contract 05 §3.3.1 rule 8: a fresh process has observed no
    /// fault).
    ///
    /// # Errors
    ///
    /// The first error of the directory read, a remove or a write.
    pub fn start(dir: FaultDir, now: SystemTime) -> io::Result<Self> {
        for entry in fs::read_dir(&dir.0)? {
            let file_name = entry?.file_name();
            let name = file_name.to_string_lossy();
            if name.starts_with('.') && name.ends_with(TEMP) {
                fs::remove_file(dir.0.join(&file_name))?;
            } else if let Some(family) = name.strip_suffix(".json")
                && let Ok(family) = family.parse()
            {
                dir.write(&family, &[], now)?;
            }
        }
        Ok(Self {
            dir,
            open: BTreeMap::new(),
        })
    }

    /// Raises `code` for `family` and rewrites its file. A fault with the
    /// same code that is already open keeps its `since`, the time the vmm
    /// first saw it (contract 05 §3.3), and takes the new message.
    ///
    /// # Errors
    ///
    /// The error of the write. The book keeps the fault, so the next
    /// `refresh` writes it again.
    pub fn raise(
        &mut self,
        family: &FamilyName,
        code: FaultCode,
        message: String,
        now: SystemTime,
    ) -> io::Result<()> {
        let faults = self.open.entry(family.clone()).or_default();
        match faults.iter_mut().find(|fault| fault.code == code) {
            Some(fault) => fault.message = message,
            None => faults.push(Entry {
                code,
                // Decision CH82 rules 2 and 6: no vmm code stops turns.
                // A full serial log gets a new log at the next start
                // (decision CH59 rule 2). A start fault reruns every
                // check at the next start, which fails closed.
                blocks_turns: false,
                since: rfc3339::format(now),
                source: SOURCE,
                message,
            }),
        }
        self.dir.write(family, faults, now)
    }

    /// Clears every fault of `family` on its `ControlReady` (contract 05
    /// §3.3.1 rule 8) and writes the empty list. A family with no open
    /// fault needs no write.
    ///
    /// # Errors
    ///
    /// The error of the write. The book keeps the faults, so the file
    /// never lists a fault that the book forgot: the next `refresh` writes
    /// them again, and a later `clear` tries again.
    pub fn clear(&mut self, family: &FamilyName, now: SystemTime) -> io::Result<()> {
        if self.open.contains_key(family) {
            self.dir.write(family, &[], now)?;
            self.open.remove(family);
        }
        Ok(())
    }

    /// Rewrites the file of each family with an open fault, so it gets a
    /// new `written_at`. Lifecycle calls it every `REFRESH`.
    ///
    /// # Errors
    ///
    /// The first error of a write. The other files are still written.
    pub fn refresh(&self, now: SystemTime) -> io::Result<()> {
        let mut first = Ok(());
        for (family, faults) in &self.open {
            let written = self.dir.write(family, faults, now);
            if first.is_ok() {
                first = written;
            }
        }
        first
    }
}
