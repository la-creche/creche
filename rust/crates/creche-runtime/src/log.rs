//! The log of a service: one line on stderr for each event.
//!
//! systemd reads stderr of a unit into the journal, so a service writes its
//! log there and to no file. A line has four parts, with one space between
//! two parts:
//!
//! ```text
//! 2031-04-18T06:43:10.123Z INFO attendance token files reloaded
//! ```
//!
//! 1. The time in UTC, in the form of RFC 3339, with milliseconds.
//! 2. The level: `INFO`, `WARNING` or `ERROR`.
//! 3. The target: the name of the program or of the part that writes.
//! 4. The message.
//!
//! One event is one line. [`format_line`] writes each control character of
//! the target and of the message as an escape, so a text from another process
//! cannot start a second line.
//!
//! The macros [`info!`](crate::info), [`warning!`](crate::warning) and
//! [`error!`](crate::error) write a line. [`init`] sets the panic hook.
//! [`out_line`] and [`err_line`] write a line that is not a log event, for
//! example the report of `--check`.
//!
//! Do not write `println!` or `eprintln!` in a service. Each one stops the
//! process when its stream is closed. Each function here drops the error of
//! a write.
//!
//! A [`Secret`](creche_contracts::secret::Secret) has no `Display`, so the
//! macros do not take one as `{}`. Its `Debug` prints no byte.
//!
//! The Python services write a line in five forms. Four are formats of
//! `logging.basicConfig`:
//!
//! | The format | Where |
//! |---|---|
//! | The time, the level, the message. | `attendance/src/attendance/__main__.py:50`, `noticeboard/src/noticeboard/__main__.py:35` |
//! | The time, the name of the logger, the message. | `door-owui/src/agent_door_owui/__main__.py:24`, `chaperone/src/chaperone/__main__.py:104`, `door-tui/src/agent_door_tui/__main__.py:32`, `door-trigger/src/agent_door_trigger/cli.py:49` |
//! | The time, the level, the name, the message. | `caregiver/src/caregiver/cli.py:82` |
//! | The level, the name, the message. No time. | `library/src/library/__main__.py:68` |
//!
//! The fifth form is the line of `uvicorn`: the level and the message, with
//! no time. Each time of a Python line is the local time of the host. This
//! module replaces the five forms with one.

// CONTRACT-QUESTION: no contract gives the form of a log line, and no program
// reads one. The five Python forms differ. Three of them stamp the local
// time of the host, and two have no time. The reading here is one form for
// each Rust service, with the time in UTC. A change of the form costs one
// function, `format_line`. An operator who compares a Python line with a
// Rust line reads two different hours until each service is a Rust service.

use std::fmt;
use std::io::{self, Write};
use std::panic::{self, Location};
use std::time::{SystemTime, UNIX_EPOCH};

/// The nanoseconds of one millisecond.
const NANOS_PER_MILLI: i128 = 1_000_000;

/// The milliseconds of one second, one minute, one hour and one day.
const MILLIS_PER_SECOND: i128 = 1000;
const MILLIS_PER_MINUTE: i128 = 60 * MILLIS_PER_SECOND;
const MILLIS_PER_HOUR: i128 = 60 * MILLIS_PER_MINUTE;
const MILLIS_PER_DAY: i128 = 24 * MILLIS_PER_HOUR;

/// The days from 0000-03-01 to 1970-01-01, in the Gregorian calendar.
const DAYS_TO_EPOCH: i128 = 719_468;

/// The days of 400 years, of 100 years, of 4 years and of 1 year.
const DAYS_PER_ERA: i128 = 146_097;
const DAYS_PER_CENTURY: i128 = 36_524;
const DAYS_PER_4_YEARS: i128 = 1460;
const DAYS_PER_YEAR: i128 = 365;

/// How important one line of the log is.
///
/// The set is closed. The three names are the names that the Python
/// `logging` module writes for the same levels.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    /// The service did a step that an operator can want to know.
    Info,
    /// The service continues, and an operator must look at the cause.
    Warning,
    /// A step failed.
    Error,
}

impl Level {
    /// The name of the level in a line: `INFO`, `WARNING` or `ERROR`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Info => "INFO",
            Self::Warning => "WARNING",
            Self::Error => "ERROR",
        }
    }
}

impl fmt::Display for Level {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Starts the log of a program. Call it first in `main`.
///
/// The function sets the panic hook of the process. The hook writes one
/// `ERROR` line with the target `program`, and with the file, the line and
/// the column of the panic. It never writes the message of the panic: that
/// message can hold a part of a request or of a file.
///
/// A second call replaces the hook of the first call.
///
/// The Python origin is the `logging.basicConfig` call of each service, for
/// example `attendance/src/attendance/__main__.py:50`. Python writes the
/// message and the trace of an exception that no code handles. This hook
/// writes the place only.
///
/// ```
/// creche_runtime::log::init("creche-probe");
/// creche_runtime::info!("creche-probe", "started with {} routes", 3);
/// ```
pub fn init(program: &'static str) {
    panic::set_hook(Box::new(move |info| {
        line(
            Level::Error,
            program,
            format_args!("{}", panic_text(info.location())),
        );
    }));
}

/// The message of the line that the panic hook writes: the place of the
/// panic and nothing of its message.
fn panic_text(location: Option<&Location<'_>>) -> String {
    match location {
        Some(place) => format!(
            "panic at {}:{}:{}",
            place.file(),
            place.line(),
            place.column()
        ),
        None => String::from("panic at a place that is not known"),
    }
}

/// Writes one line of the log to stderr, with the time of the call.
///
/// The macros [`info!`](crate::info), [`warning!`](crate::warning) and
/// [`error!`](crate::error) call this function. The write is one call for
/// the whole line, so two tasks do not mix their lines. The function drops
/// the error of the write: a log that cannot write must not stop the
/// service.
///
/// The write blocks the thread until stderr takes the line. This is one of
/// the two blocking calls that the runtime permits on a runtime thread.
pub fn line(level: Level, target: &str, message: fmt::Arguments<'_>) {
    let text = format_line(SystemTime::now(), level, target, &message.to_string());

    err_line(&text);
}

/// The text of one line of the log, with no newline at its end.
///
/// The function is pure: the caller gives the time. Each control character
/// of `target` and of `message` becomes an escape, for example `\n` or
/// `\u{1b}`, so the result is always one line.
///
/// ```
/// use std::time::{Duration, UNIX_EPOCH};
///
/// use creche_runtime::log::{Level, format_line};
///
/// let at = UNIX_EPOCH + Duration::from_millis(1_934_260_990_123);
/// assert_eq!(
///     format_line(at, Level::Info, "attendance", "token files reloaded"),
///     "2031-04-18T06:43:10.123Z INFO attendance token files reloaded"
/// );
/// assert_eq!(
///     format_line(UNIX_EPOCH, Level::Error, "probe", "first\nsecond"),
///     "1970-01-01T00:00:00.000Z ERROR probe first\\nsecond"
/// );
/// ```
#[must_use]
pub fn format_line(at: SystemTime, level: Level, target: &str, message: &str) -> String {
    let mut text = stamp(at);
    text.push(' ');
    text.push_str(level.as_str());
    text.push(' ');
    escape_into(&mut text, target);
    text.push(' ');
    escape_into(&mut text, message);

    text
}

/// Adds `text` to `line`, with each control character as an escape.
fn escape_into(line: &mut String, text: &str) {
    for character in text.chars() {
        if character.is_control() {
            line.extend(character.escape_default());
        } else {
            line.push(character);
        }
    }
}

/// The milliseconds from 1970-01-01T00:00:00Z to `at`. The count is negative
/// for a time before that moment. A part of a millisecond is cut toward the
/// earlier millisecond.
fn unix_millis(at: SystemTime) -> i128 {
    let nanos = match at.duration_since(UNIX_EPOCH) {
        Ok(after) => i128::try_from(after.as_nanos()).unwrap_or(i128::MAX),
        Err(before) => {
            i128::try_from(before.duration().as_nanos()).map_or(i128::MIN, |nanos| -nanos)
        }
    };

    nanos.div_euclid(NANOS_PER_MILLI)
}

/// The year, the month and the day of a count of days from 1970-01-01, in the
/// Gregorian calendar. The steps are the steps of the algorithm
/// `civil_from_days` of Howard Hinnant.
fn civil_from_days(days: i128) -> (i128, i128, i128) {
    // The count starts at 0000-03-01, so the leap day is the last day of a
    // year of the count.
    let shifted = days.saturating_add(DAYS_TO_EPOCH);
    let era = shifted.div_euclid(DAYS_PER_ERA);
    let day_of_era = shifted.rem_euclid(DAYS_PER_ERA);
    let year_of_era = (day_of_era - day_of_era / DAYS_PER_4_YEARS + day_of_era / DAYS_PER_CENTURY
        - day_of_era / (DAYS_PER_ERA - 1))
        / DAYS_PER_YEAR;
    let day_of_year =
        day_of_era - (DAYS_PER_YEAR * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_from_march = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_from_march + 2) / 5 + 1;
    let month = if month_from_march < 10 {
        month_from_march + 3
    } else {
        month_from_march - 9
    };
    let year = year_of_era + era.saturating_mul(400) + i128::from(month <= 2);

    (year, month, day)
}

/// The time in UTC, in the form of RFC 3339, with milliseconds:
/// `2031-04-18T06:43:10.123Z`.
///
/// The standard library has no calendar, and the workspace takes no crate
/// for one. A year past 9999 has more than four digits, and RFC 3339 does
/// not define that form. No clock of a host gives such a time.
fn stamp(at: SystemTime) -> String {
    let millis = unix_millis(at);
    let (year, month, day) = civil_from_days(millis.div_euclid(MILLIS_PER_DAY));
    let of_day = millis.rem_euclid(MILLIS_PER_DAY);
    let hour = of_day / MILLIS_PER_HOUR;
    let minute = of_day % MILLIS_PER_HOUR / MILLIS_PER_MINUTE;
    let second = of_day % MILLIS_PER_MINUTE / MILLIS_PER_SECOND;
    let milli = of_day % MILLIS_PER_SECOND;

    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{milli:03}Z")
}

/// Writes `text` and one newline to stdout, with one write call. The function
/// drops the error of the write.
///
/// This is the `print` of a Python service: the report of `--check`, a plan,
/// a usage text. `println!` stops the process when stdout is closed, for
/// example when the reader of a pipe left.
pub fn out_line(text: &str) {
    write_line(&mut io::stdout().lock(), text);
}

/// Writes `text` and one newline to stderr, with one write call. The function
/// drops the error of the write.
///
/// Use it for a line that is not a log event, for example the reason of a
/// refused start. [`line()`] writes a log event.
pub fn err_line(text: &str) {
    write_line(&mut io::stderr().lock(), text);
}

/// Writes `text` and one newline with one `write_all` call, then flushes.
/// A write that fails is dropped: the caller has no better place to report
/// it.
fn write_line(writer: &mut dyn Write, text: &str) {
    let mut bytes = Vec::with_capacity(text.len().saturating_add(1));
    bytes.extend_from_slice(text.as_bytes());
    bytes.push(b'\n');

    if writer.write_all(&bytes).is_err() {
        return;
    }

    // A flush that fails has the same answer as a write that fails.
    let _ = writer.flush();
}

/// Writes one `INFO` line: the target, then a format text and its values.
///
/// ```
/// creche_runtime::info!("noticeboard", "listening on {}", "127.0.0.1:8090");
/// ```
#[macro_export]
macro_rules! info {
    ($target:expr, $($message:tt)+) => {
        $crate::log::line(
            $crate::log::Level::Info,
            $target,
            ::core::format_args!($($message)+),
        )
    };
}

/// Writes one `WARNING` line: the target, then a format text and its values.
///
/// ```
/// creche_runtime::warning!("attendance", "the socket directory has no setgid bit");
/// ```
#[macro_export]
macro_rules! warning {
    ($target:expr, $($message:tt)+) => {
        $crate::log::line(
            $crate::log::Level::Warning,
            $target,
            ::core::format_args!($($message)+),
        )
    };
}

/// Writes one `ERROR` line: the target, then a format text and its values.
///
/// ```
/// creche_runtime::error!("caregiver", "the task {} stopped with a panic", "reconcile");
/// ```
#[macro_export]
macro_rules! error {
    ($target:expr, $($message:tt)+) => {
        $crate::log::line(
            $crate::log::Level::Error,
            $target,
            ::core::format_args!($($message)+),
        )
    };
}

#[cfg(test)]
mod tests {
    use std::process::Command;
    use std::time::Duration;

    use super::*;

    /// The variable that makes [`the_child_panics_under_the_hook`] act.
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_LOG_TEST_CHILD";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "log::tests::the_child_panics_under_the_hook";

    /// The message of the panic of the child. No line of the log can hold it.
    const PANIC_MESSAGE: &str = "zebra-crossing-secret-4711";

    /// The target of the hook of the child.
    const CHILD_PROGRAM: &str = "hook-test";

    fn after_epoch(millis: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_millis(millis)
    }

    #[test]
    fn a_line_has_the_time_the_level_the_target_and_the_message() {
        // Each time is a count of milliseconds that Python gives for the
        // text: `datetime.fromisoformat(text).timestamp() * 1000`.
        let table: [(SystemTime, Level, &str, &str, &str); 9] = [
            (
                UNIX_EPOCH,
                Level::Info,
                "probe",
                "started",
                "1970-01-01T00:00:00.000Z INFO probe started",
            ),
            (
                after_epoch(1_934_260_990_123),
                Level::Info,
                "attendance",
                "token files reloaded",
                "2031-04-18T06:43:10.123Z INFO attendance token files reloaded",
            ),
            (
                after_epoch(1_709_251_199_999),
                Level::Warning,
                "caregiver",
                "the last millisecond of a leap day",
                "2024-02-29T23:59:59.999Z WARNING caregiver the last millisecond of a leap day",
            ),
            (
                after_epoch(1_709_251_200_000),
                Level::Error,
                "caregiver",
                "the day after a leap day",
                "2024-03-01T00:00:00.000Z ERROR caregiver the day after a leap day",
            ),
            (
                after_epoch(951_782_400_000),
                Level::Info,
                "probe",
                "a leap day of a century",
                "2000-02-29T00:00:00.000Z INFO probe a leap day of a century",
            ),
            (
                after_epoch(4_107_542_399_999),
                Level::Info,
                "probe",
                "2100 is no leap year",
                "2100-02-28T23:59:59.999Z INFO probe 2100 is no leap year",
            ),
            (
                after_epoch(4_107_542_400_000),
                Level::Info,
                "probe",
                "2100 is no leap year",
                "2100-03-01T00:00:00.000Z INFO probe 2100 is no leap year",
            ),
            (
                after_epoch(946_684_799_001),
                Level::Info,
                "probe",
                "the end of a year",
                "1999-12-31T23:59:59.001Z INFO probe the end of a year",
            ),
            (
                after_epoch(253_402_300_799_999),
                Level::Info,
                "probe",
                "the last millisecond of RFC 3339",
                "9999-12-31T23:59:59.999Z INFO probe the last millisecond of RFC 3339",
            ),
        ];

        for (at, level, target, message, line) in table {
            assert_eq!(format_line(at, level, target, message), line);
        }
    }

    #[test]
    fn a_time_before_1970_has_its_date_and_cuts_toward_the_earlier_millisecond() {
        let table: [(Duration, &str); 4] = [
            (Duration::from_millis(1), "1969-12-31T23:59:59.999Z"),
            (Duration::from_nanos(1), "1969-12-31T23:59:59.999Z"),
            (Duration::from_secs(86_400), "1969-12-31T00:00:00.000Z"),
            // 1900 is no leap year: the day before 1900-03-01 is the 28th.
            (
                Duration::from_secs(2_203_891_200 + 1),
                "1900-02-28T23:59:59.000Z",
            ),
        ];

        for (before, stamp) in table {
            let line = format_line(UNIX_EPOCH - before, Level::Info, "probe", "m");

            assert_eq!(line, format!("{stamp} INFO probe m"));
        }
    }

    #[test]
    fn a_part_of_a_millisecond_is_cut() {
        let at = UNIX_EPOCH + Duration::from_nanos(1_999_999);

        assert_eq!(
            format_line(at, Level::Info, "probe", "m"),
            "1970-01-01T00:00:00.001Z INFO probe m"
        );
    }

    #[test]
    fn a_control_character_becomes_an_escape_and_the_line_stays_one_line() {
        let table: [(&str, &str); 9] = [
            ("first\nsecond", "first\\nsecond"),
            ("first\r\nsecond", "first\\r\\nsecond"),
            ("a\tb", "a\\tb"),
            ("\u{1b}[31mred", "\\u{1b}[31mred"),
            ("nul\0", "nul\\u{0}"),
            ("del\u{7f}", "del\\u{7f}"),
            ("next line\u{85}", "next line\\u{85}"),
            // A character that is not a control character stays as it is.
            (
                "caf\u{e9} \u{2028} \\n \"quoted\"",
                "caf\u{e9} \u{2028} \\n \"quoted\"",
            ),
            ("", ""),
        ];

        for (message, escaped) in table {
            let line = format_line(UNIX_EPOCH, Level::Info, "probe", message);

            assert_eq!(
                line,
                format!("1970-01-01T00:00:00.000Z INFO probe {escaped}")
            );
            assert!(!line.contains(['\n', '\r']));
        }
    }

    #[test]
    fn a_control_character_of_the_target_becomes_an_escape() {
        assert_eq!(
            format_line(UNIX_EPOCH, Level::Info, "a\nb", "m"),
            "1970-01-01T00:00:00.000Z INFO a\\nb m"
        );
    }

    #[test]
    fn each_level_has_the_name_of_the_python_level() {
        assert_eq!(Level::Info.to_string(), "INFO");
        assert_eq!(Level::Warning.to_string(), "WARNING");
        assert_eq!(Level::Error.to_string(), "ERROR");
    }

    #[test]
    fn a_line_is_one_write_with_one_newline() {
        /// Keeps each write call apart.
        struct Calls(Vec<Vec<u8>>);

        impl Write for Calls {
            fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
                self.0.push(bytes.to_vec());

                Ok(bytes.len())
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        let mut calls = Calls(Vec::new());
        write_line(&mut calls, "one line");

        assert_eq!(calls.0, [b"one line\n".to_vec()]);
    }

    #[test]
    fn a_write_that_fails_is_dropped() {
        /// A stream that is closed.
        struct Closed;

        impl Write for Closed {
            fn write(&mut self, _bytes: &[u8]) -> io::Result<usize> {
                Err(io::Error::from(io::ErrorKind::BrokenPipe))
            }

            fn flush(&mut self) -> io::Result<()> {
                Err(io::Error::from(io::ErrorKind::BrokenPipe))
            }
        }

        write_line(&mut Closed, "a line that no reader takes");
    }

    #[test]
    fn the_panic_text_names_the_place() {
        let place = Location::caller();
        let text = panic_text(Some(place));

        assert_eq!(
            text,
            format!("panic at {}:{}:{}", file!(), place.line(), place.column())
        );
        assert_eq!(panic_text(None), "panic at a place that is not known");
    }

    /// The child of the test below. Without the variable it does nothing.
    /// With the variable it sets the hook and panics. The harness takes the
    /// panic, so the child test passes.
    #[test]
    fn the_child_panics_under_the_hook() {
        if std::env::var_os(CHILD_VARIABLE).is_none() {
            return;
        }

        init(CHILD_PROGRAM);
        let caught = panic::catch_unwind(|| panic!("{PANIC_MESSAGE}"));

        assert!(caught.is_err());
    }

    #[test]
    fn the_panic_hook_writes_the_place_and_never_the_message() {
        // The hook is one for the whole process, so the panic runs in a
        // child: this test program again, with only the child test.
        let child = Command::new(std::env::current_exe().unwrap())
            .args([CHILD_TEST, "--exact", "--nocapture", "--test-threads=1"])
            .env(CHILD_VARIABLE, "1")
            .output()
            .unwrap();
        let stderr = String::from_utf8(child.stderr).unwrap();
        let stdout = String::from_utf8(child.stdout).unwrap();

        assert!(child.status.success(), "{stdout}\n{stderr}");
        assert!(stdout.contains("1 passed"), "{stdout}");

        let lines: Vec<&str> = stderr
            .lines()
            .filter(|line| line.contains(" ERROR "))
            .collect();
        let [line] = lines.as_slice() else {
            panic!("the hook writes one ERROR line: {stderr}");
        };
        let start = format!(" ERROR {CHILD_PROGRAM} panic at ");

        assert!(line.contains(&start), "{line}");
        assert!(line.contains(file!()), "{line}");
        assert!(!stderr.contains(PANIC_MESSAGE), "{stderr}");
        assert!(!stdout.contains(PANIC_MESSAGE), "{stdout}");
    }
}
