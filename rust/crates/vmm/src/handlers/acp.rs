//! The `acp` Upgrade route (decisions CH20, CH21-7). It admits or refuses
//! an Upgrade before 101 in the order of decision CH34 rule 7: the
//! session (`not_found`, then `busy`), then the start of the VM (admission,
//! start, the first `ControlReady`, the dial of guest port 1201). Every
//! refusal is a typed reply before 101 (decisions CH21-9, CH33 rule 6,
//! CH50 rule 2).

use std::future::Future;
use std::sync::{Arc, Mutex};

use creche_vmm_store::rows::Rows;
use creche_wire::{AcpError, AcpRoute, OpenAcp, ServeUpgrade, SessionRow};

use crate::relay::{AcpLink, AcpRelay, Relays, Stopped, Taken};

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

/// The start of the VM of a session: admission, the boot or restore, the
/// wait for the first `ControlReady`, and the dial of guest port 1201.
/// Plan PR L16 adds the hook of the vmm. The tests fake it.
pub trait StartVm: Send + Sync + 'static {
    /// Starts the VM that serves `row`, or returns at once when it runs.
    /// `Ok` means the VM sent its first `ControlReady`, whatever facade
    /// state it reports (decision CH34 rule 7). The hook keeps that state
    /// in `ControlState` (decision CH43 rule 8). Then the hook dials guest
    /// port 1201 with `AcpLink::dial`, and `Ok` holds that link. The
    /// relay copies its bytes after 101 (decision CH50 rule 2).
    ///
    /// The wait for `ControlReady` is `control::READY_WAIT` (decision
    /// CH36 G26: 10 s). `control::open` holds that bound. Past it the
    /// hook returns `SandboxStartFailed`. A failed dial of 1201 returns
    /// `SandboxStartFailed` too, so the client gets a typed code before
    /// 101 (decisions CH33 rule 6, CH50 rule 2). The hook of plan PR L16
    /// writes each fault to the fault file, a failed dial included
    /// (decision CH21-1). The handler writes no fault.
    ///
    /// # Errors
    ///
    /// `StartFault` when the VM did not start.
    // Decision CH50 rule 1: the start has no outer bound, so the handler
    // adds no `timeout` around this hook. Each step keeps its own bound:
    // admission answers at once (decision CH21-3), the caller of each
    // `ch-remote` call sets its timeout (decision CH44 rule 3), and
    // `READY_WAIT` bounds the wait for `ControlReady`. One admission can
    // evict several VMs in series, so the bound of the start grows with
    // the evictions. `sleep::EVICT_SNAPSHOT` bounds the `ch-remote` calls
    // of one eviction's snapshot.
    fn start(&self, row: &SessionRow) -> impl Future<Output = Result<AcpLink, StartFault>> + Send;
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
        let started = self.start.start(&row).await;
        // A drop that began during the start or the dial stopped the
        // claim. The Upgrade then gets `not_found` (decision CH34 rule 6),
        // even when the drop made the dial fail. An eviction that closed
        // the door after the start gets `host_full`, as an Upgrade of a
        // session under eviction does (decisions CH19 (6), CH21-3). A
        // dialed link drops here, so the guest end closes.
        match claim.stop_reason() {
            Some(Stopped::Drop) => return Err(AcpError::NotFound {}),
            Some(Stopped::Evict) => return Err(AcpError::HostFull {}),
            None => {}
        }
        let link = started.map_err(StartFault::error)?;
        Ok(AcpRelay::new(claim, link))
    }
}
