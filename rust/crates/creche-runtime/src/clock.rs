//! The clock of a service, as a seam that a test replaces.
//!
//! Code that needs the time takes a `&dyn Clock` and never calls
//! `SystemTime::now` itself. A test then gives a clock that it moves by hand,
//! and each time stamp of the test is known.
//!
//! A [`Clock`] gives two times. [`Clock::now`] is the wall time: it goes into
//! a file and onto a wire, and an operator can set it back. [`Clock::monotonic`]
//! never goes back: use it for an age, a limit and a pause.
//!
//! This module holds no format of a time stamp. The contract types hold each
//! format, for example `creche_contracts::status::time::Timestamp`.
//!
//! The Python services pass a clock as a callable, for example
//! `chaperone/src/chaperone/faults.py:51`, or call
//! `attendance/src/attendance/clock.py`.
//!
//! The bodies of [`SystemClock::now`], [`SystemClock::monotonic`],
//! [`unix_micros`] and [`unix_seconds`] are stubs. `AGENTS.md` of this crate
//! lists the stubs and the packet that writes them.

use std::fmt::Debug;
use std::time::{Duration, Instant, SystemTime};

/// The source of the time for a service.
///
/// The trait is object safe. A service holds an `Arc<dyn Clock>` or takes a
/// `&dyn Clock`.
pub trait Clock: Send + Sync + Debug {
    /// The wall time: the time that a file or a wire holds.
    fn now(&self) -> SystemTime;

    /// A time that never goes back. Compare two values with
    /// [`Monotonic::since`].
    fn monotonic(&self) -> Monotonic;
}

/// One reading of a clock that never goes back: the time from the start of
/// that clock.
///
/// A value has a meaning only beside another value of the same clock.
///
/// ```
/// use std::time::Duration;
///
/// use creche_runtime::clock::Monotonic;
///
/// let earlier = Monotonic::from_start(Duration::from_secs(5));
/// let later = Monotonic::from_start(Duration::from_secs(8));
///
/// assert_eq!(later.since(earlier), Duration::from_secs(3));
/// assert_eq!(earlier.since(later), Duration::ZERO);
/// assert!(earlier < later);
/// ```
///
/// Code outside this module cannot build a reading from a raw value. It
/// calls [`Monotonic::from_start`], which a test clock also uses:
///
/// ```compile_fail,E0423
/// use std::time::Duration;
///
/// use creche_runtime::clock::Monotonic;
///
/// let reading = Monotonic(Duration::ZERO);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Monotonic(Duration);

impl Monotonic {
    /// The reading of a clock that started `elapsed` ago.
    #[must_use]
    pub const fn from_start(elapsed: Duration) -> Self {
        Self(elapsed)
    }

    /// The time from `earlier` to this reading. The result is zero when
    /// `earlier` is the later one of the two: the function never panics and
    /// never gives a negative time.
    #[must_use]
    pub const fn since(self, earlier: Self) -> Duration {
        self.0.saturating_sub(earlier.0)
    }
}

/// The clock of the host.
///
/// `service::run` makes one for each process. The monotonic time counts from
/// the moment of [`SystemClock::new`].
///
/// ```
/// use creche_runtime::clock::SystemClock;
///
/// let clock = SystemClock::new();
/// assert!(format!("{clock:?}").starts_with("SystemClock"));
/// ```
///
/// Code outside this module cannot give the clock another start:
///
/// ```compile_fail,E0451
/// use creche_runtime::clock::SystemClock;
///
/// let clock = SystemClock {
///     start: std::time::Instant::now(),
/// };
/// ```
#[derive(Debug, Clone)]
pub struct SystemClock {
    #[expect(
        dead_code,
        reason = "skeleton: packet foundation-clock-entropy reads this field in `monotonic`"
    )]
    start: Instant,
}

impl SystemClock {
    /// A clock whose monotonic time starts now.
    #[must_use]
    pub fn new() -> Self {
        Self {
            start: Instant::now(),
        }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for SystemClock {
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-clock-entropy writes this body"
    )]
    fn now(&self) -> SystemTime {
        todo!()
    }

    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-clock-entropy writes this body"
    )]
    fn monotonic(&self) -> Monotonic {
        todo!()
    }
}

/// The microseconds from 1970-01-01T00:00:00Z to `at`. `None` for a time
/// before that moment and for a time whose count does not fit 64 bits.
///
/// The result is the input of
/// `creche_contracts::session::time::Timestamp::from_unix_micros`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-clock-entropy writes this body"
)]
#[must_use]
pub fn unix_micros(at: SystemTime) -> Option<i64> {
    todo!()
}

/// The seconds from 1970-01-01T00:00:00Z to `at`, as Python `time.time()`
/// gives them. `None` for a time before that moment.
///
/// The result is the input of `creche_contracts::manifest::Timestamp::new`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-clock-entropy writes this body"
)]
#[must_use]
pub fn unix_seconds(at: SystemTime) -> Option<f64> {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reading(seconds: u64) -> Monotonic {
        Monotonic::from_start(Duration::from_secs(seconds))
    }

    #[test]
    fn the_time_between_two_readings_is_never_negative() {
        assert_eq!(reading(8).since(reading(5)), Duration::from_secs(3));
        assert_eq!(reading(5).since(reading(5)), Duration::ZERO);
        assert_eq!(reading(5).since(reading(8)), Duration::ZERO);
        assert_eq!(
            Monotonic::from_start(Duration::MAX).since(reading(0)),
            Duration::MAX
        );
    }

    #[test]
    fn a_later_reading_sorts_after_an_earlier_one() {
        assert!(reading(5) < reading(8));
        assert_eq!(reading(5), reading(5));
    }

    #[test]
    fn a_clock_is_usable_behind_a_trait_object() {
        fn takes(_clock: &dyn Clock) {}

        takes(&SystemClock::new());
        takes(&SystemClock::default());
    }
}
