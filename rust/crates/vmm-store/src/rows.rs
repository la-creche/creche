//! The host session list (decisions CH1, CH2): one JSON file per row under
//! the state root, as gap G22 of the phase 2 plan reads it (taken by
//! decision CH36).
//!
//! Each file holds the row and its place on the stored order (decision
//! CH21-5). A write goes to a temp file, syncs it, renames it over the row
//! file and syncs the rows dir. So a crash leaves the old row or the new
//! row, never a part of one. The vmm loads every row at start. A row stays
//! until a delete (decision CH2), so an abandoned create keeps its row
//! (decision CH25 rule 2).

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use creche_wire::{CreateSession, ListCursor, SessionId, SessionRow};
use serde::{Deserialize, Serialize};

use crate::layout::StateRoot;

/// What one row file holds. The place on the stored order is a
/// `ListCursor`, so the list reply gives it as `next` with no conversion.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RowFile<R> {
    order: ListCursor,
    row: R,
}

/// Why a write of a row file failed.
#[derive(Debug)]
pub struct RowWriteError {
    /// The file or dir that the write failed on.
    pub path: PathBuf,
    /// Why the write failed.
    pub source: io::Error,
}

impl fmt::Display for RowWriteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "cannot write {}", self.path.display())
    }
}

impl std::error::Error for RowWriteError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

// Decision CH39 rule 4: a row file that does not load stops the vmm at
// start, and the error names the file. Only a leftover `<id>.json.tmp` is
// skipped. A skipped row would be a session that `session/list` loses and
// a create can then make again.

/// Why the rows did not load. The vmm does not start.
#[derive(Debug)]
pub enum LoadError {
    /// A read of the rows dir or a row file failed.
    Io {
        /// The file or dir that the read failed on.
        path: PathBuf,
        /// Why the read failed.
        source: io::Error,
    },
    /// A row file does not hold a row.
    Parse {
        /// The row file.
        path: PathBuf,
        /// Why the text is not a row.
        source: serde_json::Error,
    },
    /// A file in the rows dir is no row file and no temp file, or a row
    /// file holds the row of another id.
    Misnamed(PathBuf),
    /// Two row files hold one place on the stored order.
    OrderTaken(PathBuf),
}

impl fmt::Display for LoadError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, .. } => write!(formatter, "cannot read {}", path.display()),
            Self::Parse { path, .. } => write!(formatter, "{} holds no row", path.display()),
            Self::Misnamed(path) => write!(formatter, "{} is no row file", path.display()),
            Self::OrderTaken(path) => {
                write!(formatter, "{} has the order of another row", path.display())
            }
        }
    }
}

impl std::error::Error for LoadError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Parse { source, .. } => Some(source),
            Self::Misnamed(_) | Self::OrderTaken(_) => None,
        }
    }
}

/// Why a create made no row.
#[derive(Debug)]
pub enum CreateError {
    /// A row with this id exists, and a different request made it
    /// (decision CH25 rule 2). The route answers `conflict`.
    Conflict,
    /// The stored order has no place left: 2^53 - 1 creates.
    OrderSpent,
    /// The row file may not have been written. The list has no new row.
    /// The file may still be on disk: a rename that succeeded before a
    /// failed dir sync leaves it. Its order is spent, so no other row
    /// takes it, and the next load lists the row.
    Write(RowWriteError),
}

impl fmt::Display for CreateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Conflict => formatter.write_str("a different create made the row of this id"),
            Self::OrderSpent => formatter.write_str("the stored order has no place left"),
            Self::Write(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for CreateError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Write(error) => Some(error),
            Self::Conflict | Self::OrderSpent => None,
        }
    }
}

impl From<RowWriteError> for CreateError {
    fn from(error: RowWriteError) -> Self {
        Self::Write(error)
    }
}

/// Every row of the host session list, in memory and on disk.
#[derive(Debug)]
pub struct Rows {
    root: StateRoot,
    by_order: BTreeMap<ListCursor, SessionRow>,
    order_of: HashMap<SessionId, ListCursor>,
    /// The newest place that this process loaded or handed out, or 0. It
    /// only grows, so a write that failed after its rename never gives its
    /// place to a second row file.
    newest: u64,
}

impl Rows {
    /// Makes the rows dir if it is missing and loads every row file. A
    /// leftover temp file stays and does not load.
    ///
    /// # Errors
    ///
    /// A `LoadError` for the first file that does not load.
    pub fn open(root: StateRoot) -> Result<Self, LoadError> {
        let dir = root.rows_dir();
        let read_error = |path: &Path| {
            let path = path.to_owned();
            move |source| LoadError::Io { path, source }
        };
        fs::create_dir_all(&dir).map_err(read_error(&dir))?;
        sync_dir(root.path()).map_err(read_error(root.path()))?;

        let mut rows = Self {
            root,
            by_order: BTreeMap::new(),
            order_of: HashMap::new(),
            newest: 0,
        };
        for entry in fs::read_dir(&dir).map_err(read_error(&dir))? {
            let path = entry.map_err(read_error(&dir))?.path();
            rows.load(path)?;
        }
        rows.newest = rows
            .by_order
            .last_key_value()
            .map_or(0, |(order, _)| order.get());
        Ok(rows)
    }

    /// Loads one file of the rows dir.
    fn load(&mut self, path: PathBuf) -> Result<(), LoadError> {
        let Some(id) = file_id(&path) else {
            return Err(LoadError::Misnamed(path));
        };
        if path == self.root.row_temp_file(&id) {
            return Ok(());
        }
        if path != self.root.row_file(&id) {
            return Err(LoadError::Misnamed(path));
        }

        let text = match fs::read(&path) {
            Ok(text) => text,
            Err(source) => return Err(LoadError::Io { path, source }),
        };
        let file: RowFile<SessionRow> = match serde_json::from_slice(&text) {
            Ok(file) => file,
            Err(source) => return Err(LoadError::Parse { path, source }),
        };
        if file.row.id != id {
            return Err(LoadError::Misnamed(path));
        }
        if self.by_order.contains_key(&file.order) {
            return Err(LoadError::OrderTaken(path));
        }
        self.order_of.insert(id, file.order);
        self.by_order.insert(file.order, file.row);
        Ok(())
    }

    /// The row of `id`, if one exists.
    #[must_use]
    pub fn get(&self, id: &SessionId) -> Option<&SessionRow> {
        let order = self.order_of.get(id)?;
        self.by_order.get(order)
    }

    /// Every row with its place on the stored order, newest stored first
    /// (decision CH21-5).
    pub fn newest_first(&self) -> impl Iterator<Item = (ListCursor, &SessionRow)> {
        self.by_order.iter().rev().map(|(order, row)| (*order, row))
    }

    /// Makes the row of `request`, with no title and `updated_at` equal to
    /// `created_at`. A repeat of an equal typed request returns the current
    /// row, with any title and time that turns set since (decision CH25
    /// rule 2).
    ///
    /// # Errors
    ///
    /// `Conflict` when a different request made the row of this id,
    /// `OrderSpent`, or `Write` when the row file was not written.
    pub fn create(&mut self, request: CreateSession) -> Result<SessionRow, CreateError> {
        if let Some(row) = self.get(&request.id) {
            if made_from(row, &request) {
                return Ok(row.clone());
            }
            return Err(CreateError::Conflict);
        }

        let order = self.next_order().ok_or(CreateError::OrderSpent)?;
        // The place is spent before the write, so a failed write never
        // gives it out again.
        self.newest = order.get();
        let CreateSession {
            created_at,
            cwd,
            family,
            id,
            origin,
        } = request;
        let row = SessionRow {
            created_at,
            cwd,
            family,
            id,
            origin,
            title: None,
            updated_at: created_at,
        };
        self.write(order, &row)?;
        self.order_of.insert(row.id.clone(), order);
        self.by_order.insert(order, row.clone());
        Ok(row)
    }

    /// The place after the newest place that this process loaded or handed
    /// out. A failed create spends its place too. A write can fail after
    /// its rename, when the dir sync fails, and then its file holds that
    /// place. A second row file at one place would refuse the next load
    /// (`LoadError::OrderTaken`), so the vmm would not start.
    fn next_order(&self) -> Option<ListCursor> {
        ListCursor::try_from(self.newest.checked_add(1)?).ok()
    }

    /// Writes the row file of `row`: temp file, sync, rename, dir sync.
    fn write(&self, order: ListCursor, row: &SessionRow) -> Result<(), RowWriteError> {
        let temp = self.root.row_temp_file(&row.id);
        let dir = self.root.rows_dir();
        let write_error = |path: &Path| {
            let path = path.to_owned();
            move |source| RowWriteError { path, source }
        };

        let mut text = serde_json::to_vec(&RowFile { order, row })
            .map_err(io::Error::from)
            .map_err(write_error(&temp))?;
        text.push(b'\n');
        let mut file = File::create(&temp).map_err(write_error(&temp))?;
        file.write_all(&text).map_err(write_error(&temp))?;
        file.sync_all().map_err(write_error(&temp))?;
        drop(file);

        fs::rename(&temp, self.root.row_file(&row.id)).map_err(write_error(&temp))?;
        sync_dir(&dir).map_err(write_error(&dir))
    }
}

/// Whether `request` is the create that made `row`. It names each field of
/// the request, so a new field fails the build here.
fn made_from(row: &SessionRow, request: &CreateSession) -> bool {
    let CreateSession {
        created_at,
        cwd,
        family,
        id,
        origin,
    } = request;
    row.created_at == *created_at
        && row.cwd == *cwd
        && row.family == *family
        && row.id == *id
        && row.origin == *origin
}

/// The session id that starts the file name of `path`. A session id has no
/// dot, so the id is the text before the first dot.
fn file_id(path: &Path) -> Option<SessionId> {
    let name = path.file_name()?.to_str()?;
    let (id, _) = name.split_once('.')?;
    id.parse().ok()
}

/// Syncs a dir, so that a new or renamed entry in it stays after a crash.
fn sync_dir(dir: &Path) -> io::Result<()> {
    File::open(dir)?.sync_all()
}
