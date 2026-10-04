//! One instant, as the session API reads it and writes it.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::Serializer;

const MICROS_PER_MILLI: i64 = 1_000;
const MICROS_PER_SECOND: i64 = 1_000_000;
const SECONDS_PER_MINUTE: i64 = 60;
const SECONDS_PER_HOUR: i64 = 3_600;
const SECONDS_PER_DAY: i64 = 86_400;
const MICROS_PER_DAY: i64 = SECONDS_PER_DAY * MICROS_PER_SECOND;

/// 0001-01-01T00:00:00Z, the first instant of a Python `datetime`.
const FIRST_MICROS: i64 = -62_135_596_800 * MICROS_PER_SECOND;

/// 9999-12-31T23:59:59.999999Z, the last instant of a Python `datetime`.
const LAST_MICROS: i64 = 253_402_300_799 * MICROS_PER_SECOND + 999_999;

/// The first and the last year of a Python `datetime`.
const YEAR_MIN: i64 = 1;
const YEAR_MAX: i64 = 9_999;

/// The count of digits of a fraction that a microsecond holds.
const FRACTION_DIGITS: usize = 6;

/// What the parser reads a final `Z` as.
const ZULU: char = 'Z';
const ZULU_OFFSET: &str = "+00:00";

/// One instant in UTC, to the microsecond, from year 1 to year 9999.
///
/// The parser takes each text that `attendance.clock.parse_rfc3339` takes with
/// the same result under each supported Python version, but for three forms.
/// The text is a date, then optionally one separator character and a time,
/// then optionally an offset:
///
/// - The date is `YYYY-MM-DD` or `YYYYMMDD`.
/// - The separator is one character. Each character is a separator.
/// - The time is `HH`, `HH:MM`, `HH:MM:SS`, `HHMM` or `HHMMSS`. A time with
///   seconds can end with `.` or `,` and one digit or more. The digits after
///   the sixth are dropped.
/// - The offset is `Z` at the end of the text, or `+` or `-` and a time. An
///   offset is less than 24 hours.
/// - A text with no offset is in UTC.
///
/// Python takes three more forms, and this parser refuses them:
///
/// - A week date, for example `2026-W38-6`.
/// - Text between the time and the offset, for example `06:00:00 Z` and
///   `06:00:009Z`.
/// - Digits after `HHMMSS`, which Python reads as a fraction, for example
///   `06000530`.
///
/// ```
/// use creche_contracts::session::Timestamp;
///
/// let fired_at: Timestamp = "2026-10-06T08:00:00+02:00".parse()?;
/// assert_eq!(fired_at.rfc3339(), "2026-10-06T06:00:00Z");
/// # Ok::<(), creche_contracts::session::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::Timestamp;
///
/// let instant = Timestamp { micros: 0 };
/// ```
// CONTRACT-QUESTION: contract 02 §13.2 rule 4 and §13.4.2 say RFC 3339. The
// Python code reads the text with `datetime.fromisoformat`, which takes more
// forms, and the set of forms differs between two Python versions. This type
// takes the forms above. A change to RFC 3339 alone refuses a date with no
// time, which a caller of `job_status` can send today.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Timestamp {
    /// Microseconds from 1970-01-01T00:00:00Z.
    micros: i64,
}

impl Timestamp {
    /// The instant that is `micros` microseconds after 1970-01-01T00:00:00Z.
    pub fn from_unix_micros(micros: i64) -> Result<Self, TimestampError> {
        if !(FIRST_MICROS..=LAST_MICROS).contains(&micros) {
            return Err(TimestampError::OutOfRange);
        }

        Ok(Self { micros })
    }

    /// The count of microseconds from 1970-01-01T00:00:00Z.
    #[must_use]
    pub fn unix_micros(self) -> i64 {
        self.micros
    }

    /// The instant with whole seconds, `YYYY-MM-DDTHH:MM:SSZ`. The fraction of
    /// a second is dropped. A field of a session, a turn and a lease has this
    /// form (contract 02 §4.2).
    #[must_use]
    pub fn rfc3339(self) -> String {
        let Parts {
            year,
            month,
            day,
            hour,
            minute,
            second,
            ..
        } = self.parts();

        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
    }

    /// The instant with milliseconds, `YYYY-MM-DDTHH:MM:SS.mmmZ`. The digits
    /// after the third are dropped. The `ts` of a journal line has this form
    /// (contract 02 §8).
    #[must_use]
    pub fn rfc3339_millis(self) -> String {
        let Parts {
            year,
            month,
            day,
            hour,
            minute,
            second,
            micros,
        } = self.parts();
        let millis = micros / MICROS_PER_MILLI;

        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}Z")
    }

    /// The fields of the instant in UTC.
    fn parts(self) -> Parts {
        let days = self.micros.div_euclid(MICROS_PER_DAY);
        let in_day = self.micros.rem_euclid(MICROS_PER_DAY);
        let (year, month, day) = civil_from_days(days);
        let seconds = in_day / MICROS_PER_SECOND;

        Parts {
            year,
            month,
            day,
            hour: seconds / SECONDS_PER_HOUR,
            minute: seconds % SECONDS_PER_HOUR / SECONDS_PER_MINUTE,
            second: seconds % SECONDS_PER_MINUTE,
            micros: in_day % MICROS_PER_SECOND,
        }
    }
}

/// Writes an instant with whole seconds. A field of an answer uses it.
pub(super) fn seconds<S: Serializer>(
    instant: &Timestamp,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&instant.rfc3339())
}

/// Writes an instant with whole seconds, or null.
pub(super) fn optional_seconds<S: Serializer>(
    instant: &Option<Timestamp>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    match instant {
        Some(instant) => serializer.serialize_str(&instant.rfc3339()),
        None => serializer.serialize_none(),
    }
}

/// The fields of one instant in UTC.
struct Parts {
    year: i64,
    month: i64,
    day: i64,
    hour: i64,
    minute: i64,
    second: i64,
    micros: i64,
}

impl FromStr for Timestamp {
    type Err = TimestampError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.is_empty() {
            return Err(TimestampError::Empty);
        }

        let with_offset;
        let text = match text.strip_suffix(ZULU) {
            Some(head) => {
                with_offset = format!("{head}{ZULU_OFFSET}");
                with_offset.as_str()
            }
            None => text,
        };
        let (date, rest) = read_date(text)?;
        let (clock, offset_micros) = match rest {
            Some(rest) => read_time(rest)?,
            None => (Clock::default(), 0),
        };
        if !date.is_valid() {
            return Err(TimestampError::BadDate);
        }

        if !clock.is_time_of_day() {
            return Err(TimestampError::BadTime);
        }

        let local = days_from_civil(date.year, date.month, date.day) * MICROS_PER_DAY
            + clock.seconds() * MICROS_PER_SECOND
            + clock.micros;

        Self::from_unix_micros(local - offset_micros)
    }
}

/// Why a text is not a [`Timestamp`].
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimestampError {
    /// The text is empty.
    Empty,
    /// The date is not `YYYY-MM-DD` or `YYYYMMDD`, or it is not a day of the
    /// calendar.
    BadDate,
    /// The date is a week date. Python reads it. This type does not.
    WeekDate,
    /// The time does not have one of the forms of the type, or it is not a
    /// time of a day.
    BadTime,
    /// The offset does not have one of the forms of the type, or it is 24
    /// hours or more.
    BadOffset,
    /// The instant in UTC is before year 1 or after year 9999.
    OutOfRange,
}

impl fmt::Display for TimestampError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Empty => "a time is not empty",
            Self::BadDate => {
                "the date of a time is YYYY-MM-DD or YYYYMMDD, and a day of the calendar"
            }
            Self::WeekDate => "the date of a time is not a week date",
            Self::BadTime => "the time of a day is HH, HH:MM, HH:MM:SS, HHMM or HHMMSS",
            Self::BadOffset => "the offset of a time is Z, or a sign and less than 24 hours",
            Self::OutOfRange => "a time in UTC is in the years 1 to 9999",
        })
    }
}

impl Error for TimestampError {}

/// A date as the text gives it. The parser checks it against the calendar
/// afterwards.
struct Date {
    year: i64,
    month: i64,
    day: i64,
}

impl Date {
    fn is_valid(&self) -> bool {
        (YEAR_MIN..=YEAR_MAX).contains(&self.year)
            && (1..=12).contains(&self.month)
            && (1..=days_in_month(self.year, self.month)).contains(&self.day)
    }
}

/// Hours, minutes, seconds and microseconds, as a time of a day or as an
/// offset.
#[derive(Default)]
struct Clock {
    hour: i64,
    minute: i64,
    second: i64,
    micros: i64,
}

impl Clock {
    fn seconds(&self) -> i64 {
        self.hour * SECONDS_PER_HOUR + self.minute * SECONDS_PER_MINUTE + self.second
    }

    fn is_time_of_day(&self) -> bool {
        self.hour < 24 && self.minute < 60 && self.second < 60
    }
}

/// The value of `count` ASCII digits that start at `at`. `None` when a byte
/// there is not a digit of ASCII, or when the text ends first.
fn digits(bytes: &[u8], at: usize, count: usize) -> Option<i64> {
    let run = bytes.get(at..at.checked_add(count)?)?;
    if !run.iter().all(u8::is_ascii_digit) {
        return None;
    }

    Some(
        run.iter()
            .fold(0, |value, byte| value * 10 + i64::from(byte - b'0')),
    )
}

/// The marker of a week date, as in `2026-W38-6`.
const WEEK: u8 = b'W';

/// The date at the start of `text`, and the text after one separator
/// character. The rest is `None` when the text ends with the date.
fn read_date(text: &str) -> Result<(Date, Option<&str>), TimestampError> {
    let bytes = text.as_bytes();
    let year = digits(bytes, 0, 4).ok_or(TimestampError::BadDate)?;
    let extended = bytes.get(4) == Some(&b'-');
    let after_year = if extended { 5 } else { 4 };
    if bytes.get(after_year) == Some(&WEEK) {
        return Err(TimestampError::WeekDate);
    }

    let month = digits(bytes, after_year, 2).ok_or(TimestampError::BadDate)?;
    let after_month = after_year + 2;
    if extended && bytes.get(after_month) != Some(&b'-') {
        return Err(TimestampError::BadDate);
    }

    let day_at = if extended {
        after_month + 1
    } else {
        after_month
    };
    let day = digits(bytes, day_at, 2).ok_or(TimestampError::BadDate)?;
    let date = Date { year, month, day };
    let rest = text.get(day_at + 2..).ok_or(TimestampError::BadDate)?;
    let mut after_separator = rest.chars();
    if after_separator.next().is_none() {
        return Ok((date, None));
    }

    Ok((date, Some(after_separator.as_str())))
}

/// The time of a day and the offset in microseconds, from the text after the
/// separator.
fn read_time(text: &str) -> Result<(Clock, i64), TimestampError> {
    let marker = text
        .bytes()
        .position(|byte| matches!(byte, b'Z' | b'+' | b'-'));
    let Some(marker) = marker else {
        return Ok((read_clock(text).ok_or(TimestampError::BadTime)?, 0));
    };
    let (time, zone) = text
        .split_at_checked(marker)
        .ok_or(TimestampError::BadTime)?;
    let clock = read_clock(time).ok_or(TimestampError::BadTime)?;
    let offset = match zone.strip_prefix(['+', '-']) {
        Some(offset) => read_clock(offset).ok_or(TimestampError::BadOffset)?,
        None if zone.len() == 1 => Clock::default(),
        None => return Err(TimestampError::BadOffset),
    };
    let magnitude = offset.seconds() * MICROS_PER_SECOND + offset.micros;
    if magnitude >= MICROS_PER_DAY {
        return Err(TimestampError::BadOffset);
    }

    Ok((
        clock,
        if zone.starts_with('-') {
            -magnitude
        } else {
            magnitude
        },
    ))
}

/// `HH`, `HH:MM`, `HH:MM:SS`, `HHMM` or `HHMMSS`, and a fraction after the
/// seconds. `None` for each other text. The function checks no range.
fn read_clock(text: &str) -> Option<Clock> {
    let bytes = text.as_bytes();
    let hour = digits(bytes, 0, 2)?;
    let mut clock = Clock {
        hour,
        ..Clock::default()
    };
    if bytes.len() == 2 {
        return Some(clock);
    }

    let separated = bytes.get(2) == Some(&b':');
    let width = if separated { 3 } else { 2 };
    clock.minute = digits(bytes, width, 2)?;
    let after_minute = width + 2;
    if bytes.len() == after_minute {
        return Some(clock);
    }

    if separated && bytes.get(after_minute) != Some(&b':') {
        return None;
    }

    let second_at = if separated {
        after_minute + 1
    } else {
        after_minute
    };
    clock.second = digits(bytes, second_at, 2)?;
    let after_second = second_at + 2;
    if bytes.len() == after_second {
        return Some(clock);
    }

    if !matches!(bytes.get(after_second), Some(b'.' | b',')) {
        return None;
    }

    clock.micros = read_fraction(bytes.get(after_second + 1..)?)?;

    Some(clock)
}

/// The microseconds of a fraction: one digit or more, and the digits after
/// the sixth are dropped.
fn read_fraction(bytes: &[u8]) -> Option<i64> {
    if bytes.is_empty() || !bytes.iter().all(u8::is_ascii_digit) {
        return None;
    }

    let kept = bytes.len().min(FRACTION_DIGITS);
    let value = digits(bytes, 0, kept)?;
    let scale = (kept..FRACTION_DIGITS).fold(1, |scale, _| scale * 10);

    Some(value * scale)
}

fn is_leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => 31,
    }
}

/// The count of days of one era of the calendar: 400 years.
const DAYS_PER_ERA: i64 = 146_097;

/// The count of days from 0000-03-01 to 1970-01-01.
const DAYS_TO_EPOCH: i64 = 719_468;

/// The count of days from 1970-01-01 to a day of the calendar.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let month_from_march = (month + 9) % 12;
    let day_of_year = (153 * month_from_march + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;

    era * DAYS_PER_ERA + day_of_era - DAYS_TO_EPOCH
}

/// The day of the calendar that is `days` days after 1970-01-01.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let days = days + DAYS_TO_EPOCH;
    let era = days.div_euclid(DAYS_PER_ERA);
    let day_of_era = days.rem_euclid(DAYS_PER_ERA);
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_from_march = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_from_march + 2) / 5 + 1;
    let month = if month_from_march < 10 {
        month_from_march + 3
    } else {
        month_from_march - 9
    };
    let year = year_of_era + era * 400;

    (if month <= 2 { year + 1 } else { year }, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ACCEPTED: &[(&str, &str)] = &[
        ("2026-10-06T06:00:00Z", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06:00:00+00:00", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06:00:00", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06:00:00+05:30", "2026-10-06T00:30:00.000Z"),
        ("2026-10-06T06:00:00-05:30", "2026-10-06T11:30:00.000Z"),
        ("2026-10-06T06:00:00+0530", "2026-10-06T00:30:00.000Z"),
        ("2026-10-06T06:00:00+05", "2026-10-06T01:00:00.000Z"),
        ("2026-10-06T06:00:00+05:30:15.5", "2026-10-06T00:29:44.500Z"),
        ("2026-10-06T06:00:00+00:60", "2026-10-06T05:00:00.000Z"),
        ("2026-10-06", "2026-10-06T00:00:00.000Z"),
        ("2026-10-06Z", "2026-10-06T00:00:00.000Z"),
        ("2026-10-06+06:00", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06:00", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06 06:00:00Z", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06\u{e9}06:00:00Z", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06\u{1f600}06:00:00Z", "2026-10-06T06:00:00.000Z"),
        ("20261006", "2026-10-06T00:00:00.000Z"),
        ("20261006T060000Z", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T0600+05", "2026-10-06T01:00:00.000Z"),
        ("2026-10-06T06:00:00.1Z", "2026-10-06T06:00:00.100Z"),
        ("2026-10-06T06:00:00,5Z", "2026-10-06T06:00:00.500Z"),
        ("2026-10-06T06:00:00.1239999Z", "2026-10-06T06:00:00.123Z"),
        ("2026-12-31T23:30:00-01:00", "2027-01-01T00:30:00.000Z"),
        ("2024-02-29T23:30:00-01:00", "2024-03-01T00:30:00.000Z"),
        ("2024-02-29T00:00:00Z", "2024-02-29T00:00:00.000Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000Z"),
        ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999Z"),
        ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00.000Z"),
        ("1969-12-31T23:59:59.999999Z", "1969-12-31T23:59:59.999Z"),
    ];

    const REFUSED: &[(&str, TimestampError)] = &[
        ("", TimestampError::Empty),
        ("Z", TimestampError::BadDate),
        ("last Tuesday", TimestampError::BadDate),
        ("1789797600", TimestampError::BadTime),
        ("26-09-19T06:00:00Z", TimestampError::BadDate),
        ("2026-9-19T06:00:00Z", TimestampError::BadDate),
        ("2026-09", TimestampError::BadDate),
        (" 2026-10-06T06:00:00Z", TimestampError::BadDate),
        (
            "\u{662}\u{660}\u{662}\u{666}-09-19T06:00:00Z",
            TimestampError::BadDate,
        ),
        ("2026-02-29T00:00:00Z", TimestampError::BadDate),
        ("2026-13-01T00:00:00Z", TimestampError::BadDate),
        ("2026-09-31T00:00:00Z", TimestampError::BadDate),
        ("2026-09-00T00:00:00Z", TimestampError::BadDate),
        ("0000-01-01T00:00:00Z", TimestampError::BadDate),
        ("10000-01-01T00:00:00Z", TimestampError::BadDate),
        ("2026-W38-6", TimestampError::WeekDate),
        ("2026W386", TimestampError::WeekDate),
        ("2026-10-06T", TimestampError::BadTime),
        ("2026-10-06TT06:00:00Z", TimestampError::BadTime),
        ("2026-10-0606:00:00Z", TimestampError::BadTime),
        ("2026-10-06T6:00:00Z", TimestampError::BadTime),
        ("2026-10-06T06:0000Z", TimestampError::BadTime),
        ("2026-10-06T25:00:00Z", TimestampError::BadTime),
        ("2026-10-06T24:00:00Z", TimestampError::BadTime),
        ("2026-10-06T23:60:00Z", TimestampError::BadTime),
        ("2026-10-06T23:59:60Z", TimestampError::BadTime),
        ("2026-10-06T06:00:00.Z", TimestampError::BadTime),
        ("2026-10-06T06:00.5Z", TimestampError::BadTime),
        ("2026-10-06T06:00:00.5.5Z", TimestampError::BadTime),
        ("2026-10-06T06:00:00.\u{661}Z", TimestampError::BadTime),
        ("2026-10-06T06:00:00 Z", TimestampError::BadTime),
        ("2026-10-06T06:00:009Z", TimestampError::BadTime),
        ("2026-10-06T06:00:+05:30", TimestampError::BadTime),
        (
            "2026-10-06T06:00:00.123456abc+05:30",
            TimestampError::BadTime,
        ),
        ("2026-10-06T06000530", TimestampError::BadTime),
        ("2026-10-06T06:00:00z", TimestampError::BadTime),
        ("2026-10-06T06:00:00UTC", TimestampError::BadTime),
        ("2026-10-06T06:00:00Z ", TimestampError::BadOffset),
        ("2026-10-06T06:00:00Z\n", TimestampError::BadOffset),
        ("2026-10-06T06:00:00ZZ", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+5", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+24:00", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+00:00Z", TimestampError::BadOffset),
    ];

    #[test]
    fn a_text_in_the_grammar_parses() {
        for (text, millis) in ACCEPTED {
            let parsed: Timestamp = text
                .parse()
                .unwrap_or_else(|error| panic!("{text}: {error}"));

            assert_eq!(parsed.rfc3339_millis(), *millis, "{text}");
        }
    }

    #[test]
    fn a_text_outside_the_grammar_is_refused() {
        let wrong: Vec<_> = REFUSED
            .iter()
            .map(|(text, error)| (text, text.parse::<Timestamp>(), Err(*error)))
            .filter(|(_, found, wanted)| found != wanted)
            .collect();

        assert!(wrong.is_empty(), "{wrong:?}");
    }

    #[test]
    fn the_two_forms_drop_the_digits_that_they_do_not_hold() {
        let instant: Timestamp = "2026-10-05T19:22:05.118999Z".parse().unwrap();

        assert_eq!(instant.rfc3339(), "2026-10-05T19:22:05Z");
        assert_eq!(instant.rfc3339_millis(), "2026-10-05T19:22:05.118Z");
    }

    #[test]
    fn an_instant_is_a_count_of_microseconds() {
        let epoch: Timestamp = "1970-01-01T00:00:00Z".parse().unwrap();
        let first = Timestamp::from_unix_micros(FIRST_MICROS).unwrap();
        let last = Timestamp::from_unix_micros(LAST_MICROS).unwrap();

        assert_eq!(epoch.unix_micros(), 0);
        assert_eq!(first.rfc3339(), "0001-01-01T00:00:00Z");
        assert_eq!(last.rfc3339(), "9999-12-31T23:59:59Z");
        assert_eq!(
            Timestamp::from_unix_micros(FIRST_MICROS - 1),
            Err(TimestampError::OutOfRange)
        );
        assert_eq!(
            Timestamp::from_unix_micros(LAST_MICROS + 1),
            Err(TimestampError::OutOfRange)
        );
        assert!(first < epoch && epoch < last);
    }

    #[test]
    fn each_day_of_the_calendar_reads_back() {
        let mut days = days_from_civil(1, 1, 1);
        for year in [1, 4, 100, 400, 1969, 1970, 2000, 2024, 2026, 9999] {
            for month in 1..=12 {
                for day in 1..=days_in_month(year, month) {
                    assert_eq!(
                        civil_from_days(days_from_civil(year, month, day)),
                        (year, month, day)
                    );
                }
            }
            days = days.max(days_from_civil(year, 12, 31));
        }

        assert_eq!(days_from_civil(1970, 1, 1), 0);
        assert_eq!(days, days_from_civil(9999, 12, 31));
    }

    #[test]
    fn the_error_is_a_std_error() {
        let error: Box<dyn Error> = Box::new(TimestampError::WeekDate);

        assert_eq!(error.to_string(), "the date of a time is not a week date");
    }
}
