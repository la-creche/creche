//! The `list` route (decisions CH21-5, CH21-7). A client reads one page of
//! the rows of one family. The page comes from the rows in memory, so a
//! list boots no VM and reads no family dir (decision CH21-7).
//!
//! Each row of the page carries the state of its VM (decisions CH33 rule
//! 9, CH79 rule 4). The vmm reads it at each list: the VM table for
//! `running`, then the session dir in the state root for `none`, `off` and
//! `asleep`. The rows keep no state, so a vmm restart lists no stale one.

use std::collections::HashSet;
use std::fs;
use std::io;
use std::sync::{Arc, Mutex};

use creche_vmm_store::claim::CONSUMED;
use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::rows::Rows;
use creche_vmm_store::snapshot::generations;
use creche_wire::{ListError, ListRoute, ListSessions, Serve, SessionId, SessionPage, VmState};
use tokio::task::spawn_blocking;

/// The sessions whose Cloud Hypervisor process runs, for the `running`
/// state of a list row (decision CH79 rule 4). `ColdBoot` gives them from
/// its VM table.
pub trait RunningVms: Send + Sync + 'static {
    /// The sessions whose CH process runs now.
    fn running_sessions(&self) -> HashSet<SessionId>;
}

/// The handler of `list`. It shares the rows with the other handlers.
#[derive(Debug)]
pub struct Lists<V> {
    rows: Arc<Mutex<Rows>>,
    state: StateRoot,
    vms: V,
}

impl<V: RunningVms> Lists<V> {
    /// The handler over `rows`, with the state root `state` that holds the
    /// session dirs and the VM table `vms`.
    #[must_use]
    pub fn new(rows: Arc<Mutex<Rows>>, state: StateRoot, vms: V) -> Self {
        Self { rows, state, vms }
    }
}

impl<V: RunningVms> Serve<ListRoute> for Lists<V> {
    /// One page, newest stored first (decision CH21-5). `Rows::page`
    /// applies the family and the cwd filter before the cut, and ends the
    /// page early under the 1 MiB cap (decision CH33 rule 2). The request
    /// type requires `family` (decision CH34 rule 4), so one page never
    /// holds rows of two families. A family with no rows, or one that the
    /// vmm does not know, gets an empty page.
    ///
    /// The VM table is read first, then each session dir. A VM that stops
    /// between the two reads lists as `running` once more.
    // The VM state reads the session dir of each row, so the page runs in
    // `spawn_blocking` under the lock of the rows (decision CH36 G21), as
    // a create does.
    async fn serve(&self, request: ListSessions) -> Result<SessionPage, ListError> {
        let running = self.vms.running_sessions();
        let (rows, state) = (Arc::clone(&self.rows), self.state.clone());
        spawn_blocking(move || {
            let rows = rows.lock().map_err(|_| ListError::Internal {})?;
            rows.page(&request, |row| {
                vm_state(&state, &running, &row.id).map_err(|error| {
                    eprintln!(
                        "vmm: list of {}: the VM state of {}: {error}",
                        request.family, row.id
                    );
                    ListError::Internal {}
                })
            })
        })
        .await
        .unwrap_or(Err(ListError::Internal {}))
    }
}

/// The VM state of the session `id` (decision CH79 rule 4).
///
/// 1. `running`: `running` holds the session.
/// 2. `none`: the session dir holds no regular file at the disk path. A
///    boot makes the disk with a rename, so a disk path holds a whole disk
///    or nothing (`session_disk.rs`).
/// 3. `off`: no finished snapshot generation, or the newest one holds the
///    consumed mark of a claim. Such a start cold-boots (decision CH19
///    (5)).
/// 4. `asleep`: the newest generation holds no consumed mark.
///
/// # Errors
///
/// A failed read of the session dir other than an absent entry.
// CONTRACT-QUESTION: CH79 rule 4 names the four values, and no row says how
// the vmm reads them. Three readings are taken here. (a) `asleep` runs no
// key check, so a start can still drop the snapshot and cold-boot (decision
// CH44 rule 5). (b) A VM in a stop or an eviction is `running`, and a start
// before its CH launch reads as its disk. (c) A failed read of a session
// dir fails the whole page with `internal`, because the enum has no
// unknown state. The vmm `AGENTS.md` Known gaps names it.
fn vm_state(
    state: &StateRoot,
    running: &HashSet<SessionId>,
    id: &SessionId,
) -> io::Result<VmState> {
    if running.contains(id) {
        return Ok(VmState::Running);
    }
    let session = state.session(id);
    match fs::symlink_metadata(session.disk()) {
        Ok(meta) if meta.is_file() => {}
        Ok(_) => return Ok(VmState::None),
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(VmState::None),
        Err(error) => return Err(error),
    }
    let Some(newest) = generations(&session)?.pop() else {
        return Ok(VmState::Off);
    };
    match fs::symlink_metadata(session.snapshot(newest).join(CONSUMED)) {
        Ok(_) => Ok(VmState::Off),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(VmState::Asleep),
        Err(error) => Err(error),
    }
}
