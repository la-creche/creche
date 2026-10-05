//! A clock that a test moves by hand.
//!
//! Code of a service takes a `&dyn Clock` and never reads the clock of the
//! host itself. A test gives one of the two clocks here, and each time stamp
//! of the test is then known.
//!
//! - [`FixedClock`] stands still. The test moves it with a call.
//! - [`PausedClock`] reads the time of `tokio`. A test that pauses that time
//!   moves the clock and each timer together.
//!
//! The two types have no Python origin. A Python test that needs a clock
//! writes one by hand, for example the class `_Clock` of
//! `chaperone/tests/test_faults.py:13-21`. That class holds one time. A clock
//! here holds two: the wall time and the monotonic time.

use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::{Duration, SystemTime};

use creche_runtime::clock::{Clock, Monotonic};
use tokio::time::Instant;

/// A clock that stands still until the test moves it.
///
/// The wall time and the monotonic time move together with
/// [`FixedClock::advance`]. [`FixedClock::set`] moves only the wall time, as
/// an operator does who sets the clock of the host.
///
/// The type has no Python origin.
///
/// ```
/// use std::sync::Mutex;
/// use std::time::{Duration, SystemTime};
///
/// use creche_runtime::clock::{Clock, Monotonic};
/// use creche_testkit::clock::FixedClock;
///
/// let start = SystemTime::UNIX_EPOCH + Duration::from_secs(1_900_000_000);
/// let clock = FixedClock::new(start);
/// assert_eq!(clock.now(), start);
/// assert_eq!(clock.monotonic(), Monotonic::from_start(Duration::ZERO));
///
/// clock.advance(Duration::from_secs(90));
/// assert_eq!(clock.now(), start + Duration::from_secs(90));
/// assert_eq!(clock.monotonic(), Monotonic::from_start(Duration::from_secs(90)));
///
/// clock.set(start);
/// assert_eq!(clock.now(), start);
/// assert_eq!(clock.monotonic(), Monotonic::from_start(Duration::from_secs(90)));
/// ```
///
/// Code outside this module builds the clock only with [`FixedClock::new`],
/// so its monotonic time starts at zero:
///
/// ```compile_fail,E0423
/// use std::sync::Mutex;
/// use std::time::{Duration, SystemTime};
///
/// use creche_runtime::clock::{Clock, Monotonic};
/// use creche_testkit::clock::FixedClock;
///
/// let clock = FixedClock(Mutex::new((SystemTime::UNIX_EPOCH, Duration::MAX)));
/// ```
#[derive(Debug)]
pub struct FixedClock(Mutex<(SystemTime, Duration)>);

impl FixedClock {
    /// A clock that shows the wall time `at`. Its monotonic time starts at
    /// zero.
    #[must_use]
    pub fn new(at: SystemTime) -> Self {
        Self(Mutex::new((at, Duration::ZERO)))
    }

    /// Sets the wall time to `at`. The monotonic time does not move.
    pub fn set(&self, at: SystemTime) {
        self.reading().0 = at;
    }

    /// Moves the wall time and the monotonic time forward by `by`.
    ///
    /// The function never panics. A wall time that the type of the host
    /// cannot hold stays where it is. A monotonic time stops at the largest
    /// value of a `Duration`.
    pub fn advance(&self, by: Duration) {
        let mut reading = self.reading();
        let (wall, elapsed) = *reading;

        *reading = (
            wall.checked_add(by).unwrap_or(wall),
            elapsed.saturating_add(by),
        );
    }

    /// The wall time and the monotonic time, under the lock.
    ///
    /// A test that panicked with the lock leaves a poisoned lock. The pair
    /// under it is valid after each statement, so the function takes it.
    fn reading(&self) -> MutexGuard<'_, (SystemTime, Duration)> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl Clock for FixedClock {
    fn now(&self) -> SystemTime {
        self.reading().0
    }

    fn monotonic(&self) -> Monotonic {
        Monotonic::from_start(self.reading().1)
    }
}

/// A clock that reads the paused time of `tokio`.
///
/// A test that pauses the time of `tokio` moves this clock and each timer
/// together, with `tokio::time::advance`. A sleep of the code under test and
/// a time stamp of that code then agree.
///
/// In a runtime whose time is not paused, the clock runs with the clock of
/// the host, from the wall time that the test gave.
///
/// The type has no Python origin.
///
/// ```
/// use std::time::{Duration, SystemTime};
///
/// use creche_runtime::clock::{Clock, Monotonic};
/// use creche_testkit::clock::PausedClock;
/// use tokio::time::Instant;
///
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_time()
///     .start_paused(true)
///     .build()?;
///
/// runtime.block_on(async {
///     let start = SystemTime::UNIX_EPOCH + Duration::from_secs(1_900_000_000);
///     let clock = PausedClock::new(start);
///     assert_eq!(clock.now(), start);
///
///     tokio::time::advance(Duration::from_secs(90)).await;
///     assert_eq!(clock.now(), start + Duration::from_secs(90));
///     assert_eq!(clock.monotonic(), Monotonic::from_start(Duration::from_secs(90)));
/// });
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module builds the clock only with [`PausedClock::new`],
/// so the two times of the clock have one start:
///
/// ```compile_fail,E0451
/// use std::time::{Duration, SystemTime};
///
/// use creche_runtime::clock::{Clock, Monotonic};
/// use creche_testkit::clock::PausedClock;
/// use tokio::time::Instant;
///
/// let clock = PausedClock {
///     wall: SystemTime::UNIX_EPOCH,
///     start: Instant::now(),
/// };
/// ```
#[derive(Debug)]
pub struct PausedClock {
    /// The wall time at `start`.
    wall: SystemTime,
    /// The time of `tokio` at the call of [`PausedClock::new`].
    start: Instant,
}

impl PausedClock {
    /// A clock that shows the wall time `at` now. Call it inside a runtime
    /// whose time is paused.
    #[must_use]
    pub fn new(at: SystemTime) -> Self {
        Self {
            wall: at,
            start: Instant::now(),
        }
    }

    /// The time of `tokio` from the start of the clock to now.
    fn elapsed(&self) -> Duration {
        Instant::now().saturating_duration_since(self.start)
    }
}

impl Clock for PausedClock {
    /// The wall time of the start, and then the time of `tokio` from the
    /// start. A wall time that the type of the host cannot hold stays at the
    /// wall time of the start.
    fn now(&self) -> SystemTime {
        self.wall.checked_add(self.elapsed()).unwrap_or(self.wall)
    }

    fn monotonic(&self) -> Monotonic {
        Monotonic::from_start(self.elapsed())
    }
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use std::sync::Arc;

    use super::*;

    /// A wall time of the year 2030.
    fn start() -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(1_900_000_000)
    }

    fn reading(seconds: u64) -> Monotonic {
        Monotonic::from_start(Duration::from_secs(seconds))
    }

    /// Runs `future` in a runtime whose time is paused.
    fn block_on_paused<F: Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap()
            .block_on(future)
    }

    #[test]
    fn a_fixed_clock_stands_still() {
        let clock = FixedClock::new(start());

        assert_eq!(clock.now(), start());
        assert_eq!(clock.now(), start());
        assert_eq!(clock.monotonic(), reading(0));
        assert_eq!(clock.monotonic(), reading(0));
    }

    #[test]
    fn advance_moves_the_two_times_of_a_fixed_clock() {
        let clock = FixedClock::new(start());
        clock.advance(Duration::from_secs(5));
        clock.advance(Duration::from_millis(1500));

        assert_eq!(clock.now(), start() + Duration::from_millis(6500));
        assert_eq!(
            clock.monotonic(),
            Monotonic::from_start(Duration::from_millis(6500))
        );
    }

    #[test]
    fn set_moves_only_the_wall_time_of_a_fixed_clock() {
        let clock = FixedClock::new(start());
        clock.advance(Duration::from_secs(5));
        let before = clock.monotonic();

        clock.set(start() - Duration::from_secs(3600));

        assert_eq!(clock.now(), start() - Duration::from_secs(3600));
        assert_eq!(clock.monotonic(), before);
        assert_eq!(clock.monotonic().since(reading(0)), Duration::from_secs(5));
    }

    #[test]
    fn a_fixed_clock_does_not_panic_at_the_end_of_its_range() {
        let clock = FixedClock::new(start());
        clock.advance(Duration::MAX);
        clock.advance(Duration::MAX);

        assert_eq!(clock.now(), start());
        assert_eq!(clock.monotonic(), Monotonic::from_start(Duration::MAX));
    }

    #[test]
    fn a_fixed_clock_is_usable_after_a_panic_under_its_lock() {
        let clock = Arc::new(FixedClock::new(start()));
        let held = Arc::clone(&clock);
        let panicked = std::thread::spawn(move || {
            let _reading = held.reading();
            panic!("the test poisons the lock");
        })
        .join();

        assert!(panicked.is_err());
        assert!(clock.0.is_poisoned());

        clock.advance(Duration::from_secs(1));

        assert_eq!(clock.now(), start() + Duration::from_secs(1));
    }

    #[test]
    fn a_fixed_clock_is_usable_behind_a_trait_object_in_two_threads() {
        let clock: Arc<dyn Clock> = Arc::new(FixedClock::new(start()));
        let shared = Arc::clone(&clock);

        let seen = std::thread::spawn(move || shared.now()).join().unwrap();

        assert_eq!(seen, start());
        assert!(format!("{clock:?}").starts_with("FixedClock"));
    }

    #[test]
    fn a_paused_clock_stands_still_while_the_time_of_tokio_is_paused() {
        block_on_paused(async {
            let clock = PausedClock::new(start());
            tokio::task::yield_now().await;

            assert_eq!(clock.now(), start());
            assert_eq!(clock.monotonic(), reading(0));
        });
    }

    #[test]
    fn advance_of_tokio_moves_the_two_times_of_a_paused_clock() {
        block_on_paused(async {
            let clock = PausedClock::new(start());
            tokio::time::advance(Duration::from_secs(90)).await;

            assert_eq!(clock.now(), start() + Duration::from_secs(90));
            assert_eq!(clock.monotonic(), reading(90));
        });
    }

    #[test]
    fn a_sleep_moves_a_paused_clock_by_the_time_of_the_sleep() {
        block_on_paused(async {
            let clock = PausedClock::new(start());
            let before = clock.monotonic();

            // The runtime has no other work, so it moves its paused time to
            // the end of the sleep at once.
            tokio::time::sleep(Duration::from_secs(3600)).await;

            assert_eq!(clock.monotonic().since(before), Duration::from_secs(3600));
            assert_eq!(clock.now(), start() + Duration::from_secs(3600));
        });
    }

    #[test]
    fn a_paused_clock_does_not_panic_at_the_end_of_the_wall_time() {
        // The last second that the wall time of a Unix host holds.
        let last = SystemTime::UNIX_EPOCH
            .checked_add(Duration::from_secs(u64::try_from(i64::MAX).unwrap()))
            .unwrap();

        block_on_paused(async {
            let clock = PausedClock::new(last);
            tokio::time::advance(Duration::from_secs(10)).await;

            assert_eq!(clock.now(), last);
            assert_eq!(clock.monotonic(), reading(10));
        });
    }

    #[test]
    fn a_paused_clock_outside_a_runtime_reads_the_clock_of_the_host() {
        let clock = PausedClock::new(start());

        assert!(clock.now() >= start());
        assert!(format!("{clock:?}").starts_with("PausedClock"));
    }

    #[test]
    fn a_paused_clock_is_usable_behind_a_trait_object() {
        fn takes(_clock: &dyn Clock) {}

        takes(&PausedClock::new(start()));
        takes(&FixedClock::new(start()));
    }
}
