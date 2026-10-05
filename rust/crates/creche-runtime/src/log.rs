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
//! cannot start a second line. It also writes the line separator U+2028 and
//! the paragraph separator U+2029 as an escape. The two end a line in
//! Unicode, and they are not control characters.
//!
//! [`format_line`] also writes the 12 controls of the bidirectional algorithm
//! of Unicode as an escape, for example the right-to-left override U+202E. A
//! terminal shows the characters after such a control in another order. A
//! text from another process thus cannot change the order of the parts of a
//! line. Each other format character stays as it is, for example a zero
//! width joiner.
//!
//! A backslash stays as it is. A reader thus cannot tell the escape `\n`
//! from the two characters `\n` of a message. The log is for a person, and
//! no program reads the escapes back.
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
//
// No contract says which characters a line holds, and the Python log writes
// each character as it is. The reading here writes an escape for each
// character that ends a line and for each control of the bidirectional
// algorithm. A change of that set costs one function, `is_escaped`.

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

/// The line separator and the paragraph separator of Unicode. Each one ends
/// a line, for example in `str.splitlines` of Python, and `char::is_control`
/// is false for the two.
const LINE_SEPARATOR: char = '\u{2028}';
const PARAGRAPH_SEPARATOR: char = '\u{2029}';

/// The 12 controls of the bidirectional algorithm of Unicode. A terminal
/// shows the characters after such a control in another order, and
/// `char::is_control` is false for each one.
///
/// The three marks: the Arabic letter mark, the left-to-right mark and the
/// right-to-left mark.
const ARABIC_LETTER_MARK: char = '\u{61c}';
const LEFT_TO_RIGHT_MARK: char = '\u{200e}';
const RIGHT_TO_LEFT_MARK: char = '\u{200f}';

/// The two embeddings, their end and the two overrides: U+202A to U+202E.
const FIRST_EMBEDDING: char = '\u{202a}';
const LAST_OVERRIDE: char = '\u{202e}';

/// The three isolates and their end: U+2066 to U+2069.
const FIRST_ISOLATE: char = '\u{2066}';
const LAST_ISOLATE: char = '\u{2069}';

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
/// `\u{1b}`. The separators U+2028 and U+2029 become an escape too. The
/// result is thus always one line. Each control of the bidirectional
/// algorithm becomes an escape, so the parts of the line keep their order.
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

/// Whether `character` becomes an escape in a line: each control character,
/// the two separators of Unicode that end a line and are no control
/// characters, and each control of the bidirectional algorithm.
fn is_escaped(character: char) -> bool {
    character.is_control()
        || matches!(character, LINE_SEPARATOR | PARAGRAPH_SEPARATOR)
        || is_bidi_control(character)
}

/// Whether `character` is one of the 12 controls of the bidirectional
/// algorithm of Unicode.
const fn is_bidi_control(character: char) -> bool {
    matches!(
        character,
        ARABIC_LETTER_MARK
            | LEFT_TO_RIGHT_MARK
            | RIGHT_TO_LEFT_MARK
            | FIRST_EMBEDDING..=LAST_OVERRIDE
            | FIRST_ISOLATE..=LAST_ISOLATE
    )
}

/// Adds `text` to `line`, with each character of [`is_escaped`] as an escape.
fn escape_into(line: &mut String, text: &str) {
    for character in text.chars() {
        if is_escaped(character) {
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

    /// The variable that makes [`the_child_writes_each_kind_of_line`] act.
    const WRITER_VARIABLE: &str = "CRECHE_RUNTIME_LOG_TEST_WRITER";

    /// The name of that child test, as the test program takes it.
    const WRITER_TEST: &str = "log::tests::the_child_writes_each_kind_of_line";

    /// The target of each log line of that child.
    const WRITER_PROGRAM: &str = "writer-test";

    /// The level and the message of the line of each macro of that child.
    const MACRO_LINES: [(&str, &str); 3] = [
        ("INFO", "from-info 1"),
        ("WARNING", "from-warning 2"),
        ("ERROR", "from-error 3"),
    ];

    /// The line that the child gives to [`out_line`] and to [`err_line`].
    const TO_STDOUT: &str = "to-stdout";
    const TO_STDERR: &str = "to-stderr";

    /// Each character that ends a line in Unicode: line feed, vertical tab,
    /// form feed, carriage return, next line, and the two separators.
    const LINE_ENDS: [char; 7] = [
        '\n', '\u{b}', '\u{c}', '\r', '\u{85}', '\u{2028}', '\u{2029}',
    ];

    /// Each control of the bidirectional algorithm of Unicode: the three
    /// marks, then the two embeddings and the two overrides with their end,
    /// then the three isolates with their end.
    const BIDI_CONTROLS: [char; 12] = [
        '\u{61c}', '\u{200e}', '\u{200f}', '\u{202a}', '\u{202b}', '\u{202c}', '\u{202d}',
        '\u{202e}', '\u{2066}', '\u{2067}', '\u{2068}', '\u{2069}',
    ];

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
        let table: [(Duration, &str); 5] = [
            (Duration::from_millis(1), "1969-12-31T23:59:59.999Z"),
            (Duration::from_nanos(1), "1969-12-31T23:59:59.999Z"),
            (Duration::from_secs(86_400), "1969-12-31T00:00:00.000Z"),
            // 1900 is no leap year: the day before 1900-03-01 is the 28th.
            (
                Duration::from_secs(2_203_891_200 + 1),
                "1900-02-28T23:59:59.000Z",
            ),
            // A year below 1000 keeps its four digits.
            (
                Duration::from_secs(35_615_857_891),
                "0841-05-18T10:08:29.000Z",
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
        let table: [(&str, &str); 15] = [
            ("first\nsecond", "first\\nsecond"),
            ("first\r\nsecond", "first\\r\\nsecond"),
            ("a\tb", "a\\tb"),
            ("\u{1b}[31mred", "\\u{1b}[31mred"),
            ("nul\0", "nul\\u{0}"),
            ("del\u{7f}", "del\\u{7f}"),
            ("vertical tab\u{b}", "vertical tab\\u{b}"),
            ("form feed\u{c}", "form feed\\u{c}"),
            ("next line\u{85}", "next line\\u{85}"),
            // The two characters that end a line and are no control
            // characters.
            ("first\u{2028}second", "first\\u{2028}second"),
            ("first\u{2029}second", "first\\u{2029}second"),
            // Two controls of the bidirectional algorithm: an override, and
            // an isolate with its end.
            ("report\u{202e}txt.exe", "report\\u{202e}txt.exe"),
            ("a\u{2066}b\u{2069}c", "a\\u{2066}b\\u{2069}c"),
            // Each other character stays as it is, a backslash too.
            (
                "caf\u{e9} \u{a0} \\n \"quoted\"",
                "caf\u{e9} \u{a0} \\n \"quoted\"",
            ),
            ("", ""),
        ];

        for (message, escaped) in table {
            let line = format_line(UNIX_EPOCH, Level::Info, "probe", message);

            assert_eq!(
                line,
                format!("1970-01-01T00:00:00.000Z INFO probe {escaped}")
            );
            assert!(!line.contains(LINE_ENDS), "{line:?}");
        }
    }

    #[test]
    fn no_character_of_a_message_ends_the_line() {
        // Each character that ends a line for a reader: the mandatory breaks
        // of Unicode, which are also the breaks of `str.splitlines` of
        // Python, and the three separators U+001C to U+001E of that function.
        let mut message = String::from("start");

        for end in LINE_ENDS.iter().chain(&['\u{1c}', '\u{1d}', '\u{1e}']) {
            message.push(*end);
            message.push_str("next");
        }

        let line = format_line(UNIX_EPOCH, Level::Info, "probe", &message);

        assert!(!line.contains(LINE_ENDS), "{line:?}");
        assert!(!line.chars().any(char::is_control), "{line:?}");
        assert_eq!(line.matches("next").count(), 10);
    }

    #[test]
    fn no_character_of_a_message_changes_the_order_of_the_line() {
        let mut message = String::from("start");

        for control in BIDI_CONTROLS {
            message.push(control);
            message.push_str("next");
        }

        let line = format_line(UNIX_EPOCH, Level::Info, "probe", &message);

        assert!(!line.contains(BIDI_CONTROLS), "{line:?}");
        assert_eq!(line.matches("\\u{").count(), BIDI_CONTROLS.len());
        assert_eq!(line.matches("next").count(), BIDI_CONTROLS.len());
    }

    #[test]
    fn a_format_character_with_no_direction_stays_as_it_is() {
        // A zero width space, a zero width joiner, a soft hyphen and a byte
        // order mark. None ends a line and none changes the order of a line.
        let message = "a\u{200b}b\u{200d}c\u{ad}d\u{feff}e";

        assert_eq!(
            format_line(UNIX_EPOCH, Level::Info, "probe", message),
            format!("1970-01-01T00:00:00.000Z INFO probe {message}")
        );
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

    /// The child of the test below. Without the variable it does nothing.
    /// With the variable it writes one line with each macro and one line
    /// with each plain writer.
    #[test]
    fn the_child_writes_each_kind_of_line() {
        if std::env::var_os(WRITER_VARIABLE).is_none() {
            return;
        }

        crate::info!(WRITER_PROGRAM, "from-info {}", 1);
        crate::warning!(WRITER_PROGRAM, "from-warning {}", 2);
        crate::error!(WRITER_PROGRAM, "from-error {}", 3);
        out_line(TO_STDOUT);
        err_line(TO_STDERR);
    }

    #[test]
    fn each_macro_and_each_writer_writes_one_line_to_its_own_stream() {
        // The streams are the streams of the process, so the writes run in a
        // child: this test program again, with only the child test.
        let before = stamp(SystemTime::now());
        let child = Command::new(std::env::current_exe().unwrap())
            .args([WRITER_TEST, "--exact", "--nocapture", "--test-threads=1"])
            .env(WRITER_VARIABLE, "1")
            .output()
            .unwrap();
        let after = stamp(SystemTime::now());
        let stderr = String::from_utf8(child.stderr).unwrap();
        let stdout = String::from_utf8(child.stdout).unwrap();

        assert!(child.status.success(), "{stdout}\n{stderr}");
        assert!(stdout.contains("1 passed"), "{stdout}");

        for (level, message) in MACRO_LINES {
            // The line of a macro is on stderr, one time, with the level of
            // that macro and with the time of the call.
            let end = format!(" {level} {WRITER_PROGRAM} {message}");
            let lines: Vec<&str> = stderr
                .lines()
                .filter(|line| line.contains(message))
                .collect();
            let [line] = lines.as_slice() else {
                panic!("one line holds {message}: {stderr}");
            };
            let (at, rest) = line.split_once(' ').unwrap();

            assert_eq!(format!(" {rest}"), end);
            // Two stamps have the same form, so the order of two texts is
            // the order of their times.
            assert!(before.as_str() <= at, "{at} is before {before}");
            assert!(at <= after.as_str(), "{at} is after {after}");
            assert!(!stdout.contains(message), "{stdout}");
        }

        // The test program writes the name of the child test before the
        // line of `out_line`, with no newline between the two.
        let on_stdout: Vec<&str> = stdout
            .lines()
            .filter(|line| line.contains(TO_STDOUT))
            .collect();
        let [line] = on_stdout.as_slice() else {
            panic!("one line holds {TO_STDOUT}: {stdout}");
        };

        assert!(line.ends_with(TO_STDOUT), "{line}");
        assert!(!stderr.contains(TO_STDOUT), "{stderr}");
        assert_eq!(
            stderr.lines().filter(|line| *line == TO_STDERR).count(),
            1,
            "{stderr}"
        );
        assert!(!stdout.contains(TO_STDERR), "{stdout}");
    }
}
