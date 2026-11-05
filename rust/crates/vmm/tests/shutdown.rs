//! The shutdown of the vmm on paused time with a fake VM set (plan PR
//! L19, gap G16). `tests/start.rs` proves the take and the stop of the
//! set of the vmm.

use std::future::pending;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use creche_vmm::handlers::drop::{StopVm, VmStopFailed};
use creche_vmm::server::{Stop, run};
use creche_vmm::shutdown::{Ended, SHUTDOWN_WAIT, Taken, VmSet, shutdown, sigterm};
use creche_vmm::stop::StopError;
use creche_wire::SessionId;
use tokio::runtime::{Builder, Runtime};
use tokio::time::{Instant, sleep};

const A: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";
const B: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";
const C: &str = "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f";

/// `TimeoutStopSec` of the vmm unit (gap G16).
const UNIT_STOP_TIMEOUT: Duration = Duration::from_secs(90);

/// The time of a snapshot of one idle VM, and of a stop of one busy VM.
const TAKE: Duration = Duration::from_secs(20);
const STOP: Duration = Duration::from_secs(35);

/// What one fake VM does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    /// Its gate passes: it takes its snapshot.
    Idle,
    /// A turn runs: it stops.
    Busy,
    /// Its stop fails.
    Fails,
    /// Its stop never ends.
    Hangs,
}

/// The fake set. Each step records its session, its name and its time.
#[derive(Debug)]
struct Fake {
    /// The VMs of each read of `sessions`. The last one repeats.
    reads: Mutex<Vec<Vec<(&'static str, Kind)>>>,
    steps: Mutex<Vec<(String, &'static str, Duration)>>,
    start: Instant,
    /// The shutdown refused each start.
    refused: AtomicBool,
}

#[cfg(test)]
impl Fake {
    fn new(reads: Vec<Vec<(&'static str, Kind)>>) -> Self {
        Self {
            reads: Mutex::new(reads),
            steps: Mutex::default(),
            start: Instant::now(),
            refused: AtomicBool::new(false),
        }
    }

    fn kind(&self, id: &SessionId) -> Kind {
        let reads = self.reads.lock().unwrap();
        let mut vms = reads.iter().flatten().copied();
        let id = id.to_string();
        vms.find(|(vm, _)| *vm == id).unwrap().1
    }

    fn record(&self, id: &SessionId, step: &'static str) {
        let at = self.start.elapsed();
        self.steps.lock().unwrap().push((id.to_string(), step, at));
    }

    /// The steps of `id`, with their times.
    fn steps(&self, id: &str) -> Vec<(&'static str, Duration)> {
        let steps = self.steps.lock().unwrap();
        let mine = steps.iter().filter(|(vm, ..)| vm == id);
        mine.map(|(_, step, at)| (*step, *at)).collect()
    }
}

#[cfg(test)]
impl StopVm for Fake {
    async fn stop(&self, id: &SessionId) -> Result<(), VmStopFailed> {
        self.record(id, "stop");
        match self.kind(id) {
            Kind::Hangs => pending().await,
            Kind::Fails => Err(VmStopFailed {
                pid: 4242,
                error: Arc::new(StopError::Hung),
            }),
            Kind::Idle | Kind::Busy => {
                sleep(STOP).await;
                Ok(())
            }
        }
    }
}

#[cfg(test)]
impl VmSet for Fake {
    fn refuse_starts(&self) {
        self.refused.store(true, Ordering::SeqCst);
    }

    /// Each read comes after the refusal, so no start adds a VM that a
    /// read misses (plan PR L19).
    fn sessions(&self) -> Vec<SessionId> {
        assert!(self.refused.load(Ordering::SeqCst), "a read before the refusal");
        let mut reads = self.reads.lock().unwrap();
        let read = if reads.len() > 1 {
            reads.remove(0)
        } else {
            reads[0].clone()
        };
        read.iter().map(|(id, _)| id.parse().unwrap()).collect()
    }

    async fn close_door(&self, id: &SessionId) {
        self.record(id, "door");
    }

    async fn take(&self, id: &SessionId) -> Taken {
        self.record(id, "take");
        if self.kind(id) != Kind::Idle {
            return Taken::Skipped;
        }
        sleep(TAKE).await;
        Taken::Slept
    }
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
fn paused() -> Runtime {
    Builder::new_current_thread()
        .enable_all()
        .start_paused(true)
        .build()
        .unwrap()
}

/// Runs the shutdown of the VMs of `reads`, after a sleep loop that ends
/// in `sleep_end`. Gives the end and its time.
#[cfg(test)]
fn shut(reads: Vec<Vec<(&'static str, Kind)>>, sleep_end: Duration) -> (Fake, Ended, Duration) {
    let runtime = paused();
    let fake = runtime.block_on(async { Fake::new(reads) });
    let (stop, _signal) = Stop::pair();
    let (ended, at) = runtime.block_on(async {
        let ended = shutdown(&fake, stop, async move { sleep(sleep_end).await }).await;
        (ended, fake.start.elapsed())
    });
    (fake, ended, at)
}

/// The sleep loop ends first. Then each VM closes its door, the idle
/// ones take their snapshot, and the busy one stops, all at once and
/// within 90 s (gap G16).
#[test]
fn idle_vms_snapshot_and_busy_ones_stop_at_once() {
    let loop_end = Duration::from_secs(10);
    let vms = vec![(A, Kind::Idle), (B, Kind::Busy), (C, Kind::Idle)];
    let (fake, ended, at) = shut(vec![vms], loop_end);
    assert_eq!(ended, Ended::Clean);
    for idle in [A, C] {
        assert_eq!(fake.steps(idle), [("door", loop_end), ("take", loop_end)]);
    }
    let busy = [("door", loop_end), ("take", loop_end), ("stop", loop_end)];
    assert_eq!(fake.steps(B), busy);
    assert_eq!(at, loop_end + STOP);
    assert!(at < UNIT_STOP_TIMEOUT);
}

/// A stop that never ends cuts the shutdown at `SHUTDOWN_WAIT`, before
/// systemd kills the vmm. The wait counts from SIGTERM.
#[test]
fn a_hung_stop_ends_the_shutdown_at_its_wait() {
    let vms = vec![(A, Kind::Idle), (B, Kind::Hangs)];
    let (fake, ended, at) = shut(vec![vms], Duration::from_secs(10));
    assert_eq!(ended, Ended::Late);
    assert_eq!(at, SHUTDOWN_WAIT);
    assert!(SHUTDOWN_WAIT < UNIT_STOP_TIMEOUT);
    assert_eq!(fake.steps(A).len(), 2);
}

/// A session that a later read adds gets the next pass. A failed stop
/// ends the shutdown as `Kept`, and its VM gets no second try.
#[test]
fn a_late_vm_gets_the_next_pass() {
    let first = vec![(A, Kind::Fails)];
    let reads = vec![first.clone(), vec![(A, Kind::Fails), (B, Kind::Busy)]];
    let (fake, ended, at) = shut(reads, Duration::ZERO);
    assert_eq!(ended, Ended::Kept);
    assert_eq!(fake.steps(A).len(), 3);
    let late = [("door", Duration::ZERO), ("take", Duration::ZERO)];
    assert_eq!(fake.steps(B)[..2], late);
    assert_eq!(at, STOP);
}

/// The shutdown stops the server: its accept loop returns.
#[test]
fn the_shutdown_stops_the_server() {
    let path = PathBuf::from(format!("/tmp/vmm-shutdown-{}.sock", std::process::id()));
    let _ = std::fs::remove_file(&path);
    let runtime = paused();
    let fake = runtime.block_on(async { Fake::new(vec![Vec::new()]) });
    let (stop, signal) = Stop::pair();
    let served = runtime.block_on(async {
        let server = tokio::spawn({
            let path = path.clone();
            async move { run(&path, signal).await }
        });
        sleep(Duration::from_millis(10)).await;
        assert_eq!(shutdown(&fake, stop, async {}).await, Ended::Clean);
        server.await.unwrap()
    });
    assert_eq!(served, Ok(()));
    let _ = std::fs::remove_file(&path);
}

/// A SIGTERM after the registration ends the signal future, and the
/// process lives on.
#[test]
fn a_sigterm_ends_the_signal_future() {
    let runtime = Builder::new_current_thread().enable_all().build().unwrap();
    runtime.block_on(async {
        let term = sigterm().unwrap();
        let pid = std::process::id().to_string();
        let sent = Command::new("kill").args(["-TERM", &pid]).status().unwrap();
        assert!(sent.success());
        tokio::time::timeout(Duration::from_secs(5), term)
            .await
            .unwrap();
    });
}
