//! The JSONL log of the egress proxy (decisions CH6-4, CH69 rules 10 and
//! 24). One file for every VM, at the path of the key `VMM_EGRESS_LOG`.
//!
//! 1. `Log::open` opens the file at the start of `vmm serve`, for append,
//!    with mode 0600. The vmm runs as david, so david owns a new file. A
//!    file that has another mode gets 0600, as `SerialLog::fresh` does.
//!    A failed open refuses the start.
//! 2. `Log::write` appends one `Line` and a LF. The proxy writes one line
//!    per link: a refusal at its refusal, a tunnel at its end.
//! 3. A write that would take the file past `LOG_CAP` first renames it to
//!    `<path>.1`, which replaces the old `.1`, and opens a new file. So
//!    the log holds at most two files.
//! 4. A failed write, rename or open writes one stderr line, the only
//!    stderr line of the proxy (decision CH69 rule 24). A failed rename
//!    keeps the old file, so no line is lost. The next write tries again.
//!    A failed open after the rename keeps the old file, now `<path>.1`,
//!    and the next write tries only the open again. It never renames a
//!    second time, so no rename replaces the `.1` that it still writes.
//!
//! The writes block, so the proxy runs them in `spawn_blocking`.

use std::fs::{File, OpenOptions, Permissions};
use std::io::{self, Write};
use std::net::IpAddr;
use std::num::NonZeroU16;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, PoisonError};

use creche_wire::{CheckReason, FamilyName, HostName, SessionId};
use serde::Serialize;

/// The most bytes of one log file: 64 MiB (decision CH69 rule 10).
pub const LOG_CAP: u64 = 64 * 1024 * 1024;

/// The mode of each log file (decision CH69 rule 10).
pub const LOG_MODE: u32 = 0o600;

/// One line of the log: one link of the proxy (decision CH69 rule 10).
/// Each key is the field name, `time` is RFC 3339 UTC, `duration_ms` is
/// whole ms, and a field that the link never reached is `null` (decision
/// CH72 rule 1).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Line {
    /// The time of the refusal, or of the end of a tunnel.
    pub time: String,
    /// The family of the VM.
    pub family: FamilyName,
    /// The session of the VM.
    pub session: SessionId,
    /// The host of the CONNECT target, when the target read.
    pub host: Option<HostName>,
    /// The port of the CONNECT target, when the target read.
    pub port: Option<NonZeroU16>,
    /// Whether the guest got a tunnel.
    pub verdict: Verdict,
    /// Why the guest got no tunnel. `None` for a tunnel.
    pub reason: Option<Reason>,
    /// The address of the tunnel, when the connect succeeded.
    pub address: Option<IpAddr>,
    /// The bytes from the guest to the target.
    pub bytes_up: u64,
    /// The bytes from the target to the guest.
    pub bytes_down: u64,
    /// The time from the accept to the line, in ms.
    pub duration_ms: u64,
}

/// Whether the guest got a tunnel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// The proxy answered 200 and carried the tunnel.
    Allowed,
    /// The proxy answered with an error status, or closed the link.
    Refused,
}

/// Why a link got no tunnel. A refusal of the rule has the name that the
/// route `check` gives (decision CH70 rule 2).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum Reason {
    /// The verdict of `rule::decide`, or `not_valid` for a list that does
    /// not read.
    Check(CheckReason),
    /// A refusal before or after the rule.
    Proxy(ProxyReason),
}

/// The refusals of the proxy that the rule does not give: one snake_case
/// name per refusal of decisions CH69 rule 9 and CH67 rule 3 that is not
/// a `check` reason (decision CH72 rule 2).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProxyReason {
    /// The link came past `LINK_CAP` and closed at once.
    LinkCap,
    /// No whole head came within the head wait.
    HeadTimeout,
    /// The head passed the cap or did not read as a request (400).
    BadHead,
    /// The method is not CONNECT (405).
    NotConnect,
    /// No checked address took the connect (502).
    ConnectFailed,
    /// The connect passed its wait (504).
    ConnectTimeout,
}

/// The open file and its size.
#[derive(Debug)]
struct Open {
    file: File,
    size: u64,
    /// Whether `file` is now `<path>.1`: the rename of a rollover went,
    /// and its open did not.
    moved: bool,
}

/// The log file. Each clone of its `Arc` shares one file and one lock.
#[derive(Debug)]
pub struct Log {
    path: PathBuf,
    rolled: PathBuf,
    open: Mutex<Open>,
}

impl Log {
    /// Opens the log at `path` for append. It blocks.
    ///
    /// # Errors
    ///
    /// The error of the open or of the mode change.
    pub fn open(path: PathBuf) -> io::Result<Self> {
        let open = open_file(&path)?;
        let mut rolled = path.clone().into_os_string();
        rolled.push(".1");
        Ok(Self {
            path,
            rolled: PathBuf::from(rolled),
            open: Mutex::new(open),
        })
    }

    /// Appends `line` and a LF. A line that would take the file past
    /// `LOG_CAP` rolls the file over first. A fault writes one stderr line.
    /// It blocks.
    pub fn write(&self, line: &Line) {
        // serde_json fails only on a map key that is not a string, or on a
        // writer error, and a `Line` has neither.
        let Ok(mut bytes) = serde_json::to_vec(line) else {
            return;
        };
        bytes.push(b'\n');
        let added = u64::try_from(bytes.len()).unwrap_or(u64::MAX);
        // Each change of `Open` is whole before the next statement, so a
        // poisoned lock still holds a whole file and size.
        let mut open = self.open.lock().unwrap_or_else(PoisonError::into_inner);
        if open.size > 0
            && open.size.saturating_add(added) > LOG_CAP
            && let Err(error) = self.roll(&mut open)
        {
            eprintln!("vmm: egress log rollover failed: {}", error.kind());
        }
        match open.file.write_all(&bytes) {
            Ok(()) => open.size = open.size.saturating_add(added),
            Err(error) => eprintln!("vmm: egress log write failed: {}", error.kind()),
        }
    }

    /// Renames the file to `<path>.1`, unless an earlier roll did, and
    /// opens a new one in place of `open`.
    fn roll(&self, open: &mut Open) -> io::Result<()> {
        if !open.moved {
            std::fs::rename(&self.path, &self.rolled)?;
            open.moved = true;
        }
        *open = open_file(&self.path)?;
        Ok(())
    }
}

/// Opens `path` for append with `LOG_MODE`, and gives its size.
fn open_file(path: &Path) -> io::Result<Open> {
    let file = OpenOptions::new()
        .append(true)
        .create(true)
        .mode(LOG_MODE)
        .open(path)?;
    file.set_permissions(Permissions::from_mode(LOG_MODE))?;
    let size = file.metadata()?.len();
    Ok(Open {
        file,
        size,
        moved: false,
    })
}
