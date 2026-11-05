//! The `create` route (decisions CH21-4, CH25 rule 2). attendance makes a
//! row of the host session list. The vmm makes no session disk here: the
//! first boot makes it (decision CH21-4), because Panda sends
//! `session/new` on every page load. A create boots no VM.

use std::sync::{Arc, Mutex};

use creche_vmm_confine::family::{FamilyError, FamilyRoot};
use creche_vmm_store::rows::{self, Rows};
use creche_wire::{CreateError, CreateRoute, CreateSession, Serve, SessionRow};
use tokio::task::spawn_blocking;

/// The handler of `create`. It shares the rows with the other handlers.
#[derive(Debug)]
pub struct Creates {
    rows: Arc<Mutex<Rows>>,
    families: FamilyRoot,
}

impl Creates {
    /// The handler over `rows`, with caregiver's families dir `families`.
    #[must_use]
    pub fn new(rows: Arc<Mutex<Rows>>, families: FamilyRoot) -> Self {
        Self { rows, families }
    }
}

impl Serve<CreateRoute> for Creates {
    /// Checks the family, then makes the row. The family read can sleep
    /// between its tries (decision CH50 rule 9), and the row write syncs a
    /// file and a dir. So both run in `spawn_blocking` (decision CH36 G21).
    async fn serve(&self, request: CreateSession) -> Result<SessionRow, CreateError> {
        let (rows, families) = (Arc::clone(&self.rows), self.families.clone());
        spawn_blocking(move || create(&rows, &families, request))
            .await
            .unwrap_or(Err(CreateError::Internal {}))
    }
}

/// The body of one create, off the runtime.
fn create(
    rows: &Mutex<Rows>,
    families: &FamilyRoot,
    request: CreateSession,
) -> Result<SessionRow, CreateError> {
    // Decision CH33 rule 3: a family with no `config/` dir gets
    // `unknown_family`. The check comes first, so a repeat of a create
    // whose family is gone gets `unknown_family` too. The read holds no
    // lock of the rows while it sleeps between its tries.
    match families.exists(&request.family) {
        Ok(()) => {}
        Err(FamilyError::Unknown) => return Err(CreateError::UnknownFamily {}),
        // CONTRACT-QUESTION (decision CH33 rule 3): a family dir or a
        // `config/` that is a symlink or a file is no `config/` dir, so
        // this reads it as `unknown_family`. No row decides it. The vmm
        // logs it, because caregiver never writes such a family. A change
        // to `internal` costs this arm and one test line.
        Err(FamilyError::NotDir) => {
            eprintln!("vmm: create in {}: config/ is no dir", request.family);
            return Err(CreateError::UnknownFamily {});
        }
        Err(error) => {
            eprintln!("vmm: create in {}: {error:?}", request.family);
            return Err(CreateError::Internal {});
        }
    }
    let id = request.id.clone();
    let mut rows = rows.lock().map_err(|_| CreateError::Internal {})?;
    // Decisions CH25 rule 2, CH33 rule 12: the same typed request gives
    // the same row, and a different one gives `conflict` 409.
    rows.create(request).map_err(|error| match error {
        rows::CreateError::Conflict => CreateError::Conflict {},
        rows::CreateError::OrderSpent | rows::CreateError::Write(_) => {
            eprintln!("vmm: create of {id}: {error}");
            CreateError::Internal {}
        }
    })
}
