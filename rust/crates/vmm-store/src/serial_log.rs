//! The guest serial log of one session (gap G23, decision CH59 rule 2).
//!
//! Cloud Hypervisor writes the serial port of the guest to
//! `sessions/<id>/serial.log` in the state root (`SessionDir::serial_log`).
//! Systemd removes the run root at each stop of the vmm unit, a crash
//! included, so a log there would be gone when it matters most. The state
//! root stays, so the log of the last VM of a session stays across a vmm
//! restart, until the next start of that session.
//!
//! The vmm makes the file new, with mode 0600, before each launch, a boot
//! and a restore alike. The Landlock ruleset of the VM opens the file
//! before CH starts, so the file must exist first. CH v54 opens it with
//! `File::create` (`vmm/src/console_devices.rs`): a write open that
//! truncates. A restore opens the path in its snapshot config, which is the
//! same path.

use std::fmt;
use std::fs::{OpenOptions, Permissions};
use std::io;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use crate::layout::SessionDir;

/// The mode of a serial log: read and write for the vmm user only (gap
/// G23).
pub const SERIAL_MODE: u32 = 0o600;

/// The serial log of one session. Only `SerialLog::fresh` makes one, so
/// the file exists, is empty and has mode `SERIAL_MODE`. The confine track
/// renders it as the `--serial file=` argument of a boot (PR C7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SerialLog(PathBuf);

/// Why no new serial log is ready.
#[derive(Debug)]
pub struct SerialLogError {
    /// The serial log path.
    pub path: PathBuf,
    /// Why the make failed.
    pub source: io::Error,
}

impl fmt::Display for SerialLogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "cannot make {}", self.path.display())
    }
}

impl std::error::Error for SerialLogError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

impl SerialLog {
    /// Makes the serial log of `session` new: it truncates a log of an
    /// earlier start, and it sets mode `SERIAL_MODE` also on a file that
    /// has another mode. The session dir exists:
    /// `SessionDisks::for_boot` makes it. Call it once before each launch
    /// of the session, after the last CH of the session has ended.
    ///
    /// # Errors
    ///
    /// A `SerialLogError` when the open or the mode change fails.
    pub fn fresh(session: &SessionDir) -> Result<Self, SerialLogError> {
        let path = session.serial_log();
        OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(SERIAL_MODE)
            .open(&path)
            .and_then(|file| file.set_permissions(Permissions::from_mode(SERIAL_MODE)))
            .map_err(|source| SerialLogError {
                path: path.clone(),
                source,
            })?;
        Ok(Self(path))
    }

    /// The log file.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}
