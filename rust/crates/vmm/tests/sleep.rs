//! The timers of the idle sleep and the snapshot sweep on paused time
//! (plan PR L18). A fake counts each pass. `tests/start.rs` proves each
//! pass over the start hook.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use creche_vmm::sleep::{IDLE_CHECK, SWEEP_EVERY, Sleeper, run};
use tokio::runtime::{Builder, Runtime};
use tokio::time::sleep;

/// The times of each pass, from the start of the test.
#[derive(Debug, Default)]
struct Fake {
    idle: Mutex<Vec<Duration>>,
    sweeps: Mutex<Vec<Duration>>,
    /// The paused clock at the start of the test.
    start: Mutex<Option<Instant>>,
    /// How long each idle pass takes, in order. A pass past the list
    /// takes no time.
    passes: Mutex<Vec<Duration>>,
}

#[cfg(test)]
impl Fake {
    fn since(&self, now: Instant) -> Duration {
        let start = self.start.lock().unwrap().unwrap();
        now.duration_since(start)
    }
}

#[cfg(test)]
impl Sleeper for Fake {
    async fn idle_tick(&self, now: Instant) {
        self.idle.lock().unwrap().push(self.since(now));
        let pass = {
            let mut passes = self.passes.lock().unwrap();
            if passes.is_empty() {
                Duration::ZERO
            } else {
                passes.remove(0)
            }
        };
        sleep(pass).await;
    }

    async fn sweep_tick(&self) {
        let now = tokio::time::Instant::now().into_std();
        self.sweeps.lock().unwrap().push(self.since(now));
    }
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
fn paused() -> Runtime {
    Builder::new_current_thread()
        .enable_time()
        .start_paused(true)
        .build()
        .unwrap()
}

/// Runs the timers of `fake` for `span` of paused time.
#[cfg(test)]
fn run_for(fake: &Fake, span: Duration) {
    paused().block_on(async {
        *fake.start.lock().unwrap() = Some(tokio::time::Instant::now().into_std());
        run(fake, sleep(span)).await;
    });
}

/// Both passes run at once. Then the idle check runs every `IDLE_CHECK`
/// and the sweep every `SWEEP_EVERY`, until the stop.
#[test]
fn each_pass_runs_on_its_own_timer_until_the_stop() {
    let fake = Fake::default();
    run_for(&fake, SWEEP_EVERY + IDLE_CHECK / 2);
    let idle = fake.idle.lock().unwrap();
    assert_eq!(idle.len(), 121);
    assert_eq!(idle[0], Duration::ZERO);
    assert_eq!(idle[1], IDLE_CHECK);
    assert_eq!(*fake.sweeps.lock().unwrap(), [Duration::ZERO, SWEEP_EVERY]);
}

/// An idle pass that takes an hour delays the next idle check, and the
/// missed ticks do not run back to back. The sweep still runs on time.
#[test]
fn a_long_pass_delays_the_next_one() {
    let fake = Fake {
        passes: Mutex::new(vec![SWEEP_EVERY]),
        ..Fake::default()
    };
    run_for(&fake, SWEEP_EVERY + IDLE_CHECK * 5 / 2);
    let after = |checks: u32| SWEEP_EVERY + IDLE_CHECK * checks;
    let idle = fake.idle.lock().unwrap();
    assert_eq!(*idle, [Duration::ZERO, after(0), after(1), after(2)]);
    assert_eq!(*fake.sweeps.lock().unwrap(), [Duration::ZERO, SWEEP_EVERY]);
}
