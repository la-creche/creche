//! The drop of one session: its snapshots, its session disk, its session
//! dir and its row (decisions CH27, CH21-2, CH21-10). A client delete, a
//! job end and a family delete all end here. Nothing undoes it.
//!
//! The row goes last. A crash or a failed step before it leaves the row,
//! so the vmm loads it after a restart, a retried drop finds it, and the
//! retry finishes the steps. Each step counts a file that is already gone
//! as removed. After the row is gone, a retry answers `not_found`
//! (decision CH33 rule 4), and the client counts that as done.
//!
//! The snapshot files go on delete (section 4 item 5.7). The generations
//! go oldest first, each with its key first (`key::remove_generation`). So
//! a crash never leaves an older generation as the newest, which would
//! restore onto a session disk that moved on (decision CH19 (5)). Then one
//! remove of the session dir takes the session disk, its temp file and any
//! `<n>.tmp` or `<n>.retired` dir.
//!
//! The caller stops the VM of the session first (decisions CH19 (3), CH33
//! rule 11). It runs no take, claim or sweep of the session during the
//! drop.

use std::fmt;
use std::fs::{self, File};
use std::io;
use std::path::{Path, PathBuf};

use creche_wire::{DropSession, SessionRow};

use crate::key::{KeyError, remove_generation};
use crate::layout::{SessionDir, StateRoot};
use crate::rows::{RowWriteError, Rows};
use crate::snapshot::generations;

/// A failed step of a drop. The row stays, so a retried drop finds it.
#[derive(Debug)]
pub struct RemoveError {
    /// The file or dir that the step failed on.
    pub path: PathBuf,
    /// Why the step failed.
    pub source: io::Error,
}

impl fmt::Display for RemoveError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "cannot remove {}", self.path.display())
    }
}

impl std::error::Error for RemoveError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

impl From<KeyError> for RemoveError {
    fn from(KeyError { path, source }: KeyError) -> Self {
        Self { path, source }
    }
}

impl From<RowWriteError> for RemoveError {
    fn from(RowWriteError { path, source }: RowWriteError) -> Self {
        Self { path, source }
    }
}

/// Removes the snapshots, the session disk, the session dir and then the
/// row of `target`. It returns the removed row. It returns `None` and
/// removes nothing when no row has the id, or when the row belongs to
/// another family (decision CH33 rules 4 and 5). The route then answers
/// `not_found`.
///
/// # Errors
///
/// A `RemoveError` for the first step that failed. The steps before it
/// stay done, and the row stays in the list and on disk.
pub fn remove_session(
    rows: &mut Rows,
    target: &DropSession,
) -> Result<Option<SessionRow>, RemoveError> {
    let DropSession { family, id } = target;
    if rows.get(id).is_none_or(|row| row.family != *family) {
        return Ok(None);
    }
    remove_files(rows.root(), &rows.root().session(id))?;
    Ok(rows.remove(id)?)
}

/// Every step of a drop before the row.
fn remove_files(state: &StateRoot, session: &SessionDir) -> Result<(), RemoveError> {
    let snapshots = session.snapshots();
    for generation in generations(session).map_err(io_at(&snapshots))? {
        remove_generation(session, generation)?;
    }
    let dir = session.path();
    gone(fs::remove_dir_all(dir)).map_err(io_at(dir))?;
    let sessions = state.sessions_dir();
    gone(File::open(&sessions).and_then(|opened| opened.sync_all())).map_err(io_at(&sessions))
}

/// Counts an absent file or dir as done.
fn gone(result: io::Result<()>) -> io::Result<()> {
    match result {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

/// Maps an `io::Error` on `path` to a `RemoveError`.
fn io_at(path: &Path) -> impl FnOnce(io::Error) -> RemoveError {
    let path = path.to_owned();
    move |source| RemoveError { path, source }
}
