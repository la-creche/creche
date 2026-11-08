//! The snapshot that puts a VM to sleep (decisions CH2, CH8-6). An
//! eviction of admission takes it (plan PR L17), and so does the idle
//! timer of `run` (plan PR L18). The same idle tick caps the serial log
//! of each VM (decision CH79 rule 7). `run` also runs the snapshot sweep
//! of PR D13 on its own timer.

use std::future::{Future, poll_fn};
use std::pin::pin;
use std::task::Poll;
use std::time::Duration;

use creche_vmm_store::key::SnapshotKey;
use creche_vmm_store::layout::{SessionDir, SnapshotGeneration, VmRunDir};
use creche_vmm_store::snapshot::{SnapshotTimeouts, Snapshotter};
use tokio::time::{Instant, MissedTickBehavior, interval};

use crate::restore::ChApi;
use crate::start::{ColdBoot, Launcher};

/// The time between two idle checks. A VM sleeps between `IDLE_TIMEOUT`
/// and `IDLE_TIMEOUT` plus this after its last use.
// Decision CH54 rule 5: the idle check runs every 30 s, a tenth of the 5
// minutes of `IDLE_TIMEOUT`.
pub const IDLE_CHECK: Duration = Duration::from_secs(30);

/// The size past which the vmm stops a VM for its serial log, in bytes.
// Decision CH79 rule 7 (gap Q71-7 option A): the idle check reads the
// size of the serial log of each running VM. So a runaway guest writes
// at most 64 MiB plus `IDLE_CHECK` of output per start. Decision CH62
// rule 26 asks for the cap before the cut.
pub const SERIAL_CAP: u64 = 64 * 1024 * 1024;

/// The time between two snapshot sweeps.
// Decision CH54 rule 5: the sweep runs at the start of `run`, then each
// hour. An hour is small next to the 7 days of `MAX_AGE`, and a consumed
// snapshot takes about 100 MB (decision CH29).
pub const SWEEP_EVERY: Duration = Duration::from_secs(60 * 60);

/// The timeouts of the snapshot of one eviction or one idle sleep
/// (decisions CH44 rule 3, CH50 rule 1). A pause takes about 4 ms (fact 72). A snapshot of the
/// largest guest, 16 GiB, takes about 12 s (gap G16), so 60 s leaves
/// room for a slow disk. The dig of the holes has no timeout (PR D9).
/// The guest sync waits `control::SYNC_WAIT` (10 s) before the take, so
/// the eviction bound of CH50 rule 1 grows by 10 s to 10 + 5 + 60 + 10 s
/// (decision CH62 rule 23).
pub const EVICT_SNAPSHOT: SnapshotTimeouts = SnapshotTimeouts {
    pause: Duration::from_secs(5),
    snapshot: Duration::from_secs(60),
    shutdown: Duration::from_secs(10),
};

/// Snapshots the VM of `vm` into a new generation of `session` with `key`
/// and ends its CH (PRs D9 and D10). The take writes the key before its
/// rename, so no snapshot exists without a key (decision CH44, item 42).
/// Blocks.
///
/// # Errors
///
/// The `SnapshotError` of the take, as text. A failed key write is one:
/// the take then leaves no generation.
pub fn take_keyed(
    snapshotter: &Snapshotter,
    vm: &VmRunDir,
    session: &SessionDir,
    key: &SnapshotKey,
    timeouts: SnapshotTimeouts,
) -> Result<SnapshotGeneration, String> {
    let snapshot = snapshotter
        .take(vm, session, key, timeouts)
        .map_err(|error| format!("{error}: {error:?}"))?;
    Ok(snapshot.generation)
}

/// The two passes that `run` times. `ColdBoot` is the one of the vmm, and
/// a test gives a fake.
pub trait Sleeper: Sync {
    /// Stops each VM past `SERIAL_CAP` (`ColdBoot::cap_serial`), then
    /// puts each idle VM to sleep at `now` (`ColdBoot::sleep_idle`).
    fn idle_tick(&self, now: std::time::Instant) -> impl Future<Output = ()> + Send;

    /// Sweeps the snapshots (`ColdBoot::sweep`).
    fn sweep_tick(&self) -> impl Future<Output = ()> + Send;
}

impl<L: Launcher, A: ChApi> Sleeper for ColdBoot<L, A> {
    async fn idle_tick(&self, now: std::time::Instant) {
        // A VM past the cap stops before the sleep reads its gate, so it
        // takes no snapshot.
        for id in self.cap_serial().await {
            eprintln!("vmm: the VM of {id} stopped: its serial log passed {SERIAL_CAP} bytes");
        }
        for id in self.sleep_idle(now).await {
            eprintln!("vmm: the VM of {id} sleeps");
        }
    }

    async fn sweep_tick(&self) {
        match self.sweep().await {
            Ok(swept) => {
                for failure in swept.failures {
                    let path = failure.path.display();
                    eprintln!("vmm: the sweep failed at {path}: {}", failure.source);
                }
            }
            Err(error) => eprintln!("vmm: the sweep failed: {error}"),
        }
    }
}

/// Which timer fired.
enum Tick {
    Idle,
    Sweep,
    Stop,
}

/// Runs the idle check every `IDLE_CHECK` and the sweep every
/// `SWEEP_EVERY` until `stop` ends. Both first run at once. A pass that
/// runs ends before the stop counts, so no take stops halfway. A pass
/// that takes long delays the next tick, and no ticks pile up.
pub async fn run<S: Sleeper>(sleeper: &S, stop: impl Future<Output = ()>) {
    let mut stop = pin!(stop);
    let mut idle = interval(IDLE_CHECK);
    let mut sweep = interval(SWEEP_EVERY);
    idle.set_missed_tick_behavior(MissedTickBehavior::Delay);
    sweep.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        let tick = poll_fn(|context| {
            if stop.as_mut().poll(context).is_ready() {
                return Poll::Ready(Tick::Stop);
            }
            // The sweep comes first: it fires once an hour, so a run of
            // long idle passes never starves it.
            if sweep.poll_tick(context).is_ready() {
                return Poll::Ready(Tick::Sweep);
            }
            idle.poll_tick(context).map(|_| Tick::Idle)
        })
        .await;
        match tick {
            Tick::Idle => sleeper.idle_tick(Instant::now().into_std()).await,
            Tick::Sweep => sleeper.sweep_tick().await,
            Tick::Stop => return,
        }
    }
}
