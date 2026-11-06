//! The `list` route (decisions CH21-5, CH21-7). A client reads one page of
//! the rows of one family. The page comes from the rows in memory, so a
//! list boots no VM and reads no family dir (decision CH21-7).

use std::sync::{Arc, Mutex};

use creche_vmm_store::rows::Rows;
use creche_wire::{ListError, ListRoute, ListSessions, Serve, SessionPage};

/// The handler of `list`. It shares the rows with the other handlers.
#[derive(Debug)]
pub struct Lists {
    rows: Arc<Mutex<Rows>>,
}

impl Lists {
    /// The handler over `rows`.
    #[must_use]
    pub fn new(rows: Arc<Mutex<Rows>>) -> Self {
        Self { rows }
    }
}

impl Serve<ListRoute> for Lists {
    /// One page, newest stored first (decision CH21-5). `Rows::page`
    /// applies the family and the cwd filter before the cut, and ends the
    /// page early under the 1 MiB cap (decision CH33 rule 2). The request
    /// type requires `family` (decision CH34 rule 4), so one page never
    /// holds rows of two families. A family with no rows, or one that the
    /// vmm does not know, gets an empty page.
    // The page reads memory only and writes no file, so it runs on the
    // runtime worker under the lock of the rows (decision CH36 G21).
    async fn serve(&self, request: ListSessions) -> Result<SessionPage, ListError> {
        let rows = self.rows.lock().map_err(|_| ListError::Internal {})?;
        Ok(rows.page(&request))
    }
}
