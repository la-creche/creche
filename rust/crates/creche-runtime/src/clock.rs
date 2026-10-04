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
//! [`unix_micros`] and [`unix_seconds`] give the two counts that a contract
//! type takes: whole microseconds, and the seconds of Python `time.time()`.

use std::fmt::Debug;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// The count of nanoseconds in one second, as the float that CPython divides
/// by.
const NANOS_PER_SECOND: f64 = 1e9;

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
    /// The wall time of the host, as `datetime.now(UTC)` of
    /// `attendance/src/attendance/clock.py:15-17`, of
    /// `caregiver/src/caregiver/clock.py:16-17` and of
    /// `chaperone/src/chaperone/faults.py:54-55` reads it.
    fn now(&self) -> SystemTime {
        SystemTime::now()
    }

    /// The time since [`SystemClock::new`], from the clock of the host that
    /// never goes back. The Python services read `time.monotonic`, for example
    /// `caregiver/src/caregiver/loop.py:184` and
    /// `chaperone/src/chaperone/delegations.py:93`.
    fn monotonic(&self) -> Monotonic {
        Monotonic::from_start(Instant::now().saturating_duration_since(self.start))
    }
}

/// The microseconds from 1970-01-01T00:00:00Z to `at`. `None` for a time
/// before that moment and for a time whose count does not fit 64 bits.
///
/// The function cuts the part of `at` below one microsecond. A Python
/// `datetime` holds microseconds, and `attendance/src/attendance/clock.py:15-17`
/// gives one for each time stamp.
///
/// The result is the input of
/// `creche_contracts::session::time::Timestamp::from_unix_micros`.
///
/// ```
/// use std::time::{Duration, UNIX_EPOCH};
///
/// use creche_runtime::clock::unix_micros;
///
/// let at = UNIX_EPOCH + Duration::new(1, 500_000_999);
/// assert_eq!(unix_micros(at), Some(1_500_000));
/// assert_eq!(unix_micros(UNIX_EPOCH - Duration::from_secs(1)), None);
/// ```
#[must_use]
pub fn unix_micros(at: SystemTime) -> Option<i64> {
    let since = at.duration_since(UNIX_EPOCH).ok()?;

    i64::try_from(since.as_micros()).ok()
}

/// The seconds from 1970-01-01T00:00:00Z to `at`, as Python `time.time()`
/// gives them. `None` for a time before that moment.
///
/// The Python services read `time.time`, for example
/// `door-trigger/src/agent_door_trigger/ulid.py:30`,
/// `caregiver/src/caregiver/mcp_release.py:906-908` and
/// `handover/src/handover/cli.py:144`.
///
/// CPython holds a time as a count of nanoseconds. For a whole second its
/// float is the count of seconds. For each other time its float is the count
/// of nanoseconds as a float, divided by `1e9`. This function does the same,
/// so it gives each bit of the Python float. `Duration::as_secs_f64` adds the
/// seconds and the fraction, and its last bit differs from the Python float
/// for about one time in four.
///
/// The result is the input of `creche_contracts::manifest::Timestamp::new`.
///
/// ```
/// use std::time::{Duration, UNIX_EPOCH};
///
/// use creche_runtime::clock::unix_seconds;
///
/// let at = UNIX_EPOCH + Duration::from_millis(1_500);
/// assert_eq!(unix_seconds(at), Some(1.5));
/// assert_eq!(unix_seconds(UNIX_EPOCH - Duration::from_secs(1)), None);
/// ```
#[must_use]
pub fn unix_seconds(at: SystemTime) -> Option<f64> {
    let since = at.duration_since(UNIX_EPOCH).ok()?;
    if since.subsec_nanos() == 0 {
        return Some(nearest_float(u128::from(since.as_secs())));
    }

    Some(nearest_float(since.as_nanos()) / NANOS_PER_SECOND)
}

/// The float that is nearest to `count`. A count in the middle of two floats
/// goes to the float with an even last bit, as the cast of C does.
#[expect(
    clippy::as_conversions,
    reason = "std has no other conversion from a 128-bit count to a float, and each count of a `Duration` is below 2^94"
)]
fn nearest_float(count: u128) -> f64 {
    count as f64
}

#[cfg(test)]
mod tests {
    use creche_contracts::manifest;
    use creche_contracts::session;
    use creche_contracts::status;

    use super::*;

    /// The count of microseconds from 1970 to 2020-01-01T00:00:00Z. The
    /// clock of a host that runs this test shows a later time.
    const YEAR_2020_MICROS: i64 = 1_577_836_800_000_000;

    /// The largest count of whole seconds that fits 64 bits of microseconds,
    /// and the microseconds of the largest count above those seconds.
    const LAST_MICROS_SECONDS: u64 = 9_223_372_036_854;
    const LAST_MICROS_REST: u32 = 775_807;

    /// The largest count of whole seconds that fits 63 bits of nanoseconds,
    /// and the nanoseconds of the largest count above those seconds. CPython
    /// holds no later time.
    const LAST_PYTHON_SECONDS: u64 = 9_223_372_036;
    const LAST_PYTHON_NANOS: u32 = 854_775_807;

    fn reading(seconds: u64) -> Monotonic {
        Monotonic::from_start(Duration::from_secs(seconds))
    }

    /// The time that is `seconds` and `nanos` after 1970-01-01T00:00:00Z.
    fn after_1970(seconds: u64, nanos: u32) -> SystemTime {
        UNIX_EPOCH + Duration::new(seconds, nanos)
    }

    /// The time that is `seconds` and `nanos` before 1970-01-01T00:00:00Z.
    fn before_1970(seconds: u64, nanos: u32) -> SystemTime {
        UNIX_EPOCH - Duration::new(seconds, nanos)
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

    #[test]
    fn the_clock_of_the_host_shows_a_time_that_each_contract_type_takes() {
        let now = SystemClock::new().now();
        let micros = unix_micros(now).unwrap();
        let seconds = unix_seconds(now).unwrap();

        assert!(micros > YEAR_2020_MICROS);
        assert!(session::Timestamp::from_unix_micros(micros).is_ok());
        assert!(status::time::Timestamp::try_from(now).is_ok());
        assert!(manifest::Timestamp::new(seconds).is_ok());
    }

    #[test]
    fn a_reading_of_the_host_never_goes_back() {
        let clock = SystemClock::new();
        let copy = clock.clone();
        let first = clock.monotonic();

        let mut last = first;
        for _ in 0..10_000 {
            let next = clock.monotonic();
            assert!(next >= last);
            last = next;
        }

        assert!(
            copy.monotonic() >= last,
            "a copy of the clock counts from the same start"
        );
    }

    #[test]
    fn the_readings_of_the_host_count_from_the_new_clock() {
        let clock = SystemClock::new();
        let first = clock.monotonic();
        std::thread::sleep(Duration::from_millis(20));
        let second = clock.monotonic();

        assert!(first.since(reading(0)) < Duration::from_secs(3600));
        assert!(second.since(first) >= Duration::from_millis(1));
    }

    #[test]
    fn a_time_from_1970_is_a_count_of_whole_microseconds() {
        let accepted: [(u64, u32, i64); 8] = [
            (0, 0, 0),
            (0, 999, 0),
            (0, 1_000, 1),
            (0, 1_999, 1),
            (1, 500_000_000, 1_500_000),
            (1_934_260_990, 123_456_789, 1_934_260_990_123_456),
            (LAST_MICROS_SECONDS, LAST_MICROS_REST * 1_000, i64::MAX),
            (
                LAST_MICROS_SECONDS,
                LAST_MICROS_REST * 1_000 + 999,
                i64::MAX,
            ),
        ];

        for (seconds, nanos, micros) in accepted {
            assert_eq!(
                unix_micros(after_1970(seconds, nanos)),
                Some(micros),
                "{seconds} s and {nanos} ns"
            );
        }
    }

    #[test]
    fn a_time_outside_64_bits_of_microseconds_is_refused() {
        let refused: [SystemTime; 5] = [
            before_1970(0, 1),
            before_1970(0, 999),
            before_1970(1, 0),
            after_1970(LAST_MICROS_SECONDS, (LAST_MICROS_REST + 1) * 1_000),
            after_1970(LAST_MICROS_SECONDS + 1, 0),
        ];

        for at in refused {
            assert_eq!(unix_micros(at), None, "{at:?}");
        }
    }

    #[test]
    fn the_microseconds_are_the_instant_of_a_journal_line() {
        let micros = unix_micros(after_1970(1_934_260_990, 123_456_789)).unwrap();
        let instant = session::Timestamp::from_unix_micros(micros).unwrap();

        assert_eq!(instant.rfc3339(), "2031-04-18T06:43:10Z");
        assert_eq!(instant.rfc3339_millis(), "2031-04-18T06:43:10.123Z");
    }

    /// A time and the float that CPython gives for it. The float of each row
    /// is `float(ns) / 1e9` for the count `ns` of nanoseconds, and
    /// `float(ns // 10**9)` for a whole second. `time.time()` applies that
    /// rule to the reading of the clock. CPython 3.12, 3.13 and 3.14 hold the
    /// same rule.
    ///
    /// On each row with `Differs::Yes`, the sum of the seconds and of the
    /// fraction is another float. The whole milliseconds of the two floats
    /// then differ by one.
    ///
    /// The three rows after the year 2116 are whole seconds. For each one,
    /// `float(ns) / 1e9` is another float than the count of seconds.
    const PYTHON_SECONDS: [(u64, u32, f64, Differs); 18] = [
        (0, 0, 0.0, Differs::No),
        (0, 1, 1e-09, Differs::No),
        (0, 999_999_999, 0.999999999, Differs::No),
        (1, 0, 1.0, Differs::No),
        (1, 500_000_000, 1.5, Differs::No),
        (1_758_153_590, 0, 1758153590.0, Differs::No),
        (1_758_153_590, 500_000_000, 1758153590.5, Differs::No),
        (1_758_153_590, 123_456_789, 1758153590.1234567, Differs::No),
        (1_934_347_390, 123_456_789, 1934347390.1234567, Differs::No),
        (4_611_686_019, 0, 4611686019.0, Differs::No),
        (6_340_888_753, 0, 6340888753.0, Differs::No),
        (8_251_055_967, 0, 8251055967.0, Differs::No),
        (1_790_869_849, 637_000_000, 1790869849.6369998, Differs::Yes),
        (1_799_740_127, 453_000_000, 1799740127.4529998, Differs::Yes),
        (1_791_856_483, 210_000_000, 1791856483.2099998, Differs::Yes),
        (
            281_474_976_710,
            655_000_000,
            281474976710.65497,
            Differs::Yes,
        ),
        (253_402_300_799, 999_999_999, 253402300800.0, Differs::No),
        (
            LAST_PYTHON_SECONDS,
            LAST_PYTHON_NANOS,
            9223372036.854776,
            Differs::No,
        ),
    ];

    /// Whether the sum of the seconds and of the fraction gives another
    /// float than CPython gives.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Differs {
        Yes,
        No,
    }

    /// The seconds of a time as the sum of the whole seconds and of the
    /// fraction: the rule that `unix_seconds` does not use.
    fn sum_of_parts(seconds: u64, nanos: u32) -> f64 {
        nearest_float(u128::from(seconds)) + f64::from(nanos) / NANOS_PER_SECOND
    }

    #[test]
    fn a_time_from_1970_is_the_float_of_python() {
        for (seconds, nanos, float, differs) in PYTHON_SECONDS {
            let found = unix_seconds(after_1970(seconds, nanos)).unwrap();
            let sum_differs = if sum_of_parts(seconds, nanos).to_bits() == float.to_bits() {
                Differs::No
            } else {
                Differs::Yes
            };

            assert_eq!(
                found.to_bits(),
                float.to_bits(),
                "{seconds} s and {nanos} ns"
            );
            assert_eq!(sum_differs, differs, "{seconds} s and {nanos} ns");
        }
    }

    #[test]
    fn a_time_before_1970_has_no_seconds() {
        for at in [
            before_1970(0, 1),
            before_1970(0, 500_000),
            before_1970(1, 0),
        ] {
            assert_eq!(unix_seconds(at), None, "{at:?}");
        }
    }

    #[test]
    fn the_latest_time_of_the_host_has_seconds() {
        let seconds = u64::try_from(i64::MAX).unwrap();
        let Some(latest) = UNIX_EPOCH.checked_add(Duration::new(seconds, 999_999_999)) else {
            // The clock of this host holds no such time.
            return;
        };
        let found = unix_seconds(latest).unwrap();

        assert!(found.is_finite());
        assert_eq!(found.to_bits(), 9223372036854775808.0_f64.to_bits());
        assert_eq!(unix_micros(latest), None);
    }

    #[test]
    fn the_seconds_are_the_time_of_a_release_request() {
        let seconds = unix_seconds(after_1970(1_758_153_590, 500_000_000)).unwrap();

        assert_eq!(
            manifest::Timestamp::new(seconds).unwrap().get(),
            1758153590.5
        );
    }

    /// One difference from the Python code on purpose. No vector covers a
    /// clock, so each row names a Python line.
    struct Deviation {
        /// The Python line.
        python: &'static str,
        /// What the Python line does, and what this module does.
        difference: &'static str,
        /// Whether this module does what `difference` says.
        holds: fn() -> bool,
    }

    const DEVIATIONS: [Deviation; 3] = [
        Deviation {
            python: "attendance/src/attendance/clock.py:17",
            difference: "`datetime.now(UTC)` gives a time before 1970 when the clock of the host \
                         shows one. `unix_micros` gives `None`.",
            holds: || unix_micros(before_1970(0, 1)).is_none(),
        },
        Deviation {
            python: "door-trigger/src/agent_door_trigger/ulid.py:30",
            difference: "`time.time()` gives a float below zero when the clock of the host shows \
                         a time before 1970. `unix_seconds` gives `None`.",
            holds: || unix_seconds(before_1970(0, 1)).is_none(),
        },
        Deviation {
            python: "door-trigger/src/agent_door_trigger/ulid.py:30",
            difference: "CPython holds a time as 63 bits of nanoseconds, so `time.time()` gives \
                         no float after 2262-04-11. `unix_seconds` gives a float for each later \
                         time, by the same rule.",
            holds: || {
                unix_seconds(after_1970(LAST_PYTHON_SECONDS, LAST_PYTHON_NANOS + 1))
                    .is_some_and(|seconds| seconds.to_bits() == 9223372036.854776_f64.to_bits())
            },
        },
    ];

    #[test]
    fn each_deviation_names_a_python_line_and_holds() {
        for row in &DEVIATIONS {
            let (file, line) = row.python.rsplit_once(':').unwrap();

            assert!(file.ends_with(".py"), "{}", row.python);
            assert!(line.parse::<u32>().is_ok(), "{}", row.python);
            assert!(row.difference.ends_with('.'), "{}", row.python);
            assert!((row.holds)(), "{}: {}", row.python, row.difference);
        }
    }
}
