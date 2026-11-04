//! The `acp` Upgrade route (decisions CH20, CH21-7). It admits or refuses
//! an Upgrade before 101 in the order of decision CH34 rule 7: the
//! session (`not_found`, then `busy`), then the start of the VM (admission,
//! start, the first `ControlReady`). Every refusal is a typed reply
//! before 101 (decisions CH21-9, CH33 rule 6).

use std::future::Future;
use std::sync::{Arc, Mutex};

use creche_vmm_store::rows::Rows;
use creche_wire::{AcpError, AcpRoute, OpenAcp, ServeUpgrade, SessionRow};

use crate::relay::{AcpRelay, Relays, Taken};

/// Why a VM did not start. Each is a code of `AcpError` before 101.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StartFault {
    /// The VM does not fit, and no idle VM can make room (decisions
    /// CH8-6, CH21-3). The vmm never queues.
    HostFull,
    /// The VM did not boot or restore, or guest init sent no
    /// `ControlReady` (decisions CH6-3, CH33 rule 6).
    SandboxStartFailed,
    /// The VM failed the deny-by-default check (decision CH6-3).
    EgressAssertFailed,
}

impl StartFault {
    /// The code of the fault.
    // No `From` impl: a second `From` for `AcpError` breaks the type
    // inference of `?` in code that converts into a generic error.
    #[must_use]
    pub fn error(self) -> AcpError {
        match self {
            Self::HostFull => AcpError::HostFull {},
            Self::SandboxStartFailed => AcpError::SandboxStartFailed {},
            Self::EgressAssertFailed => AcpError::EgressAssertFailed {},
        }
    }
}

/// The start of the VM of a session: admission, the boot or restore, and
/// the wait for the first `ControlReady`. Plan PR L16 adds the hook of
/// the vmm. The tests fake it.
pub trait StartVm: Send + Sync + 'static {
    /// Starts the VM that serves `row`, or returns at once when it runs.
    /// `Ok` means the VM sent its first `ControlReady`, whatever facade
    /// state it reports (decision CH34 rule 7). The hook keeps that state
    /// in `ControlState` (decision CH43 rule 8).
    ///
    /// The wait for `ControlReady` is `control::READY_WAIT` (decision
    /// CH36 G26: 10 s). `control::open` holds that bound. Past it the
    /// hook returns `SandboxStartFailed`. The hook also writes each fault
    /// to the fault file (decision CH21-1).
    ///
    /// # Errors
    ///
    /// `StartFault` when the VM did not start.
    // CONTRACT-QUESTION: decision CH36 G26 bounds only the wait for
    // `ControlReady`. No row bounds the whole start: admission and an
    // eviction under pressure (plan PR L17) come before the dial. The
    // narrowest reading adds no bound of the handler around this hook. A
    // row that bounds the whole start costs one `timeout` in `admit`.
    // AGENTS.md lists it in Known gaps.
    fn start(&self, row: &SessionRow) -> impl Future<Output = Result<(), StartFault>> + Send;
}

/// The handler of `acp`. It shares the rows and the relay registry with
/// the other handlers.
#[derive(Debug)]
pub struct Acp<S> {
    rows: Arc<Mutex<Rows>>,
    relays: Relays,
    start: S,
}

impl<S: StartVm> Acp<S> {
    /// The handler over `rows` and `relays`, with the hook `start`.
    pub fn new(rows: Arc<Mutex<Rows>>, relays: Relays, start: S) -> Self {
        Self {
            rows,
            relays,
            start,
        }
    }

    /// The row of `request`, when it exists and `request.family` owns it
    /// (decision CH33 rule 5).
    fn row(&self, request: &OpenAcp) -> Result<SessionRow, AcpError> {
        let rows = self.rows.lock().map_err(|_| AcpError::Internal {})?;
        match rows.get(&request.id) {
            Some(row) if row.family == request.family => Ok(row.clone()),
            _ => Err(AcpError::NotFound {}),
        }
    }
}

impl<S: StartVm> ServeUpgrade<AcpRoute> for Acp<S> {
    type Relay = AcpRelay;

    async fn admit(&self, request: OpenAcp) -> Result<AcpRelay, AcpError> {
        // The session comes first, then `busy` (decision CH34 rule 7). An
        // unknown id gets `not_found` with no boot (decision CH21-9). An id
        // of another family gets `not_found` too and never touches the
        // claim of the owner, so it learns nothing of the session
        // (decision CH33 rule 5).
        self.row(&request)?;
        let mut claim = self
            .relays
            .claim(&request.id)
            .map_err(|taken| match taken {
                Taken::Busy => AcpError::Busy {},
                Taken::Dropping => AcpError::NotFound {},
            })?;
        // A drop marks the session before it removes the row, and keeps
        // the mark until the row is gone (`Relays::mark_drop`). So a drop
        // that ended between the first read and the claim left no row, and
        // this second read gives `not_found`, never a VM.
        let row = self.row(&request)?;
        self.start.start(&row).await.map_err(StartFault::error)?;
        // A drop that began during the start stopped the claim. The
        // Upgrade then gets `not_found` (decision CH34 rule 6).
        if claim.stopped() {
            return Err(AcpError::NotFound {});
        }
        Ok(AcpRelay::new(claim))
    }
}
