//! The clean stop of the vmm on SIGTERM (plan PR L19, gap G16 option A of
//! decision CH36). The steps:
//!
//! 1. The server accepts no further connection (decision CH26 rule 2).
//!    The set refuses each start, so no Upgrade on a connection of
//!    before the stop starts a VM (decision CH56 rule 4: `host_full`).
//! 2. The sleep loop ends, so no idle retire runs beside a shutdown take
//!    (caller duty 5 of `AGENTS.md`).
//! 3. Each VM of the set, all at once: its door link closes, then it
//!    takes its snapshot when its gate and the take gate pass. Each other
//!    VM gets the stop of plan PR L14 (decision CH19 (3)).
//!
//! Each VM scope has `BindsTo=` and `Before=` the vmm unit (decision CH60
//! rule 1). systemd stops the scopes only after the vmm unit has stopped,
//! so a stop or a restart of the unit reaches this code before any VM
//! stops (decision CH19 (4)). The kvm tests of
//! `rust/crates/vmm-confine/tests/kvm_slot.rs` prove both orders. A
//! client that finds the socket gone treats ENOENT, ECONNREFUSED and EOF
//! as transient (decision CH20).

use std::collections::HashSet;
use std::future::Future;
use std::io;
use std::time::Duration;

use creche_wire::{DROP_SIGTERM_GRACE, DROP_STOP_WAIT, SessionId};
use tokio::signal::unix::{SignalKind, signal};
use tokio::time::{Instant, timeout_at};

use crate::handlers::drop::{StopVm, VmStopFailed, all};
use crate::server::Stop;
use crate::sleep::EVICT_SNAPSHOT;
use crate::stop::KILL_WAIT;

/// `TimeoutStopSec` of the vmm unit (gap G16: 90 s). Plan PR C13 writes
/// the unit. Past it systemd sends SIGKILL to the vmm.
const UNIT_STOP_TIMEOUT: Duration = Duration::from_secs(90);

/// The time that the vmm keeps for its exit after the shutdown: the end
/// of the runtime and the exit of the process.
// Decision CH56 rule 3: the exit margin is 5 s, so `SHUTDOWN_WAIT` is
// 85 s inside the `TimeoutStopSec` of 90 s (gap G16). Caller duty 8 gets
// these 5 s for its exit.
const EXIT_TIME: Duration = Duration::from_secs(5);

/// The longest shutdown. Past it the shutdown ends. Each VM still in the
/// set ends with the vmm unit: when the vmm has exited, systemd stops each
/// VM scope bound to the unit (decisions CH56 rule 2, CH60 rule 1).
pub const SHUTDOWN_WAIT: Duration = UNIT_STOP_TIMEOUT.saturating_sub(EXIT_TIME);

/// The longest wait of `Runtime::shutdown_timeout` after the shutdown
/// (caller duty 8 of `AGENTS.md`). A take runs in `spawn_blocking`, and a
/// plain drop of the runtime waits for each blocking task with no bound.
// No row sets the split of the 5 s of `EXIT_TIME`. The runtime end gets
// 4 s, and the exit of the process keeps 1 s. Only main.rs reads it, so
// a change costs one constant.
pub const RUNTIME_END: Duration = Duration::from_secs(4);

const _: () = assert!(
    RUNTIME_END.as_secs() < EXIT_TIME.as_secs(),
    "the runtime ends inside the exit time",
);

// A stop (30 + 5 + 1 s, decision CH50 rule 4) and a take (5 + 60 + 10 s,
// decision CH44 rule 3) each fit inside the wait.
// Decision CH56 rule 2: one deadline from SIGTERM bounds every step, so
// a full take and then a full stop of one VM need not both fit. A VM
// still in the set at the deadline ends with the vmm unit (decision CH60
// rule 1): systemd stops its scope after the vmm exits, a power cut for
// its guest.
const _: () = assert!(
    SHUTDOWN_WAIT.as_secs() + EXIT_TIME.as_secs() <= UNIT_STOP_TIMEOUT.as_secs()
        && DROP_STOP_WAIT.as_secs() + DROP_SIGTERM_GRACE.as_secs() + KILL_WAIT.as_secs()
            <= SHUTDOWN_WAIT.as_secs()
        && EVICT_SNAPSHOT.pause.as_secs()
            + EVICT_SNAPSHOT.snapshot.as_secs()
            + EVICT_SNAPSHOT.shutdown.as_secs()
            <= SHUTDOWN_WAIT.as_secs(),
    "a stop and a take each end before the unit kills the vmm",
);

/// How the take of one VM ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Taken {
    /// The VM took its snapshot and left the set.
    Slept,
    /// The VM left the set with no snapshot to restore (decision CH54
    /// rule 6).
    Stopped,
    /// No take ran, or the VM is in the set still. The shutdown stops it.
    Skipped,
}

/// How the shutdown ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ended {
    /// Each VM left the set.
    Clean,
    /// A VM did not stop (decision CH50 rule 4). Its CH ends with the vmm
    /// unit (decision CH60 rule 1).
    Kept,
    /// `SHUTDOWN_WAIT` passed first.
    Late,
}

/// The VMs that a shutdown retires. `start::ColdBoot` is the set of the
/// vmm, and a test gives a fake. Its `StopVm::stop` is the stop path of
/// plan PR L14, through the `Stopper` of plan PR L18b.
pub trait VmSet: StopVm {
    /// Refuses each start from now on with `host_full`, so no VM joins
    /// the set during the shutdown. A start that passed admission before
    /// holds a slot, so the next read of `sessions` holds it.
    // Decision CH56 rule 4: from SIGTERM on, every start gets
    // `host_full`, also for a session with no VM (it extends decision
    // CH54 rule 4).
    fn refuse_starts(&self);

    /// The sessions with a slot in the set: a VM, a start or a retire.
    fn sessions(&self) -> Vec<SessionId>;

    /// Stops the open door link of `id`, if any, and waits until its claim
    /// ends. The client reconnects with `session/load` (decision CH19
    /// (6)).
    fn close_door(&self, id: &SessionId) -> impl Future<Output = ()> + Send;

    /// Snapshots the VM of `id`, then stops it, when its gate passes
    /// (decision CH19 (6), PR D12) and the take gate passes (caller duty
    /// 7 of `AGENTS.md`).
    fn take(&self, id: &SessionId) -> impl Future<Output = Taken> + Send;
}

/// Registers the handler of SIGTERM and gives the future of the signal.
/// The caller registers it at the start of the vmm, inside the runtime,
/// so a SIGTERM never takes the default action.
///
/// # Errors
///
/// The registration failed.
pub fn sigterm() -> io::Result<impl Future<Output = ()>> {
    let mut terminate = signal(SignalKind::terminate())?;
    Ok(async move {
        // A `None` means the driver ended, so the vmm stops too.
        let _ = terminate.recv().await;
    })
}

/// Shuts the vmm down (gap G16): stops `server`, waits for `sleep`, the
/// end of the sleep loop, then retires each VM of `vms` at once. `vms`
/// refuses each start first, so a seen session gets no new VM. A session
/// that a later read adds gets the next pass. `SHUTDOWN_WAIT` bounds the
/// whole shutdown.
pub async fn shutdown<V: VmSet>(vms: &V, server: Stop, sleep: impl Future<Output = ()>) -> Ended {
    let deadline = Instant::now() + SHUTDOWN_WAIT;
    server.stop();
    vms.refuse_starts();
    let passes = async {
        sleep.await;
        let (mut seen, mut whole) = (HashSet::new(), true);
        loop {
            let ids: Vec<SessionId> = vms
                .sessions()
                .into_iter()
                .filter(|id| seen.insert(id.clone()))
                .collect();
            if ids.is_empty() {
                return whole;
            }
            let ends = all(ids.iter().map(|id| retire(vms, id)).collect()).await;
            whole &= ends.into_iter().all(|stopped| stopped);
        }
    };
    match timeout_at(deadline, passes).await {
        Ok(true) => Ended::Clean,
        Ok(false) => Ended::Kept,
        Err(_) => {
            eprintln!(
                "vmm: the shutdown passed {SHUTDOWN_WAIT:?}, so its VMs end with the vmm unit"
            );
            Ended::Late
        }
    }
}

/// Closes the door of `id`, then takes its snapshot or stops it. True
/// when the VM left the set.
async fn retire<V: VmSet>(vms: &V, id: &SessionId) -> bool {
    vms.close_door(id).await;
    match vms.take(id).await {
        Taken::Slept => eprintln!("vmm: the VM of {id} took its snapshot"),
        Taken::Stopped => {}
        Taken::Skipped => {
            if let Err(VmStopFailed { pid, error }) = vms.stop(id).await {
                eprintln!("vmm: the VM of {id} (pid {pid}) did not stop: {error}");
                return false;
            }
        }
    }
    true
}
