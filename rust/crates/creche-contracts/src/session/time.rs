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

/// The least count of characters of a text, after the parser replaced a final
/// `Z`. Python refuses a shorter text.
const TEXT_MIN_CHARS: usize = 7;

/// One instant in UTC, to the microsecond, from year 1 to year 9999.
///
/// The parser takes each text that `attendance.clock.parse_rfc3339` takes with
/// the same result under each supported Python version. The text is a date,
/// then optionally one separator character and a time, then optionally an
/// offset:
///
/// - The date is `YYYY-MM-DD` or `YYYYMMDD`, or a week date: `YYYY-Www`,
///   `YYYYWww`, `YYYY-Www-D` or `YYYYWwwD`. A week date with no day is the
///   Monday of the week.
/// - The separator is one character. Each character is a separator.
/// - The time is `HH`, `HH:MM`, `HH:MM:SS`, `HHMM` or `HHMMSS`. A time with
///   seconds can end with `.` or `,` and one digit or more. The digits after
///   the sixth are dropped. Digits directly after `HHMMSS` are a fraction too.
/// - The offset is `Z` at the end of the text, or `+` or `-` and a time. An
///   offset is less than 24 hours.
/// - A text with no offset is in UTC.
///
/// The reader of Python is lax in three more ways, and this parser does the
/// same:
///
/// - With an offset, one character can be between the time and the offset,
///   for example `06:00:00 +0200`. The reader does not look at it.
/// - With an offset, a fraction of six digits or more can have each text
///   after it, for example `06:00:00.123456abc+05:30`.
/// - The byte NUL ends the text. The reader does not look at what is after
///   `Z` and a NUL.
///
/// The parser refuses each form that two Python versions read in different
/// ways: a fraction with no digit, a fraction after the hours or after the
/// minutes, a `:` after the seconds, hour 24, and an offset of a fraction of
/// a second alone.
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
// takes the forms that each supported version reads in the same way. A change
// to RFC 3339 alone refuses a date with no time, which a caller of
// `job_status` can send today. It also refuses `2026-10-06 06:00:00 +0200`.
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
        if text.chars().count() < TEXT_MIN_CHARS {
            return Err(TimestampError::BadDate);
        }

        let bytes = text.as_bytes();
        let date_len = date_len(bytes).ok_or(TimestampError::BadDate)?;
        let date = read_date(bytes, date_len)?;
        let (clock, offset_micros) = match text.get(date_len..) {
            None => return Err(TimestampError::BadDate),
            Some("") => (Clock::default(), 0),
            Some(rest) => {
                let mut after_separator = rest.chars();
                after_separator.next();
                read_time(after_separator.as_str().as_bytes())?
            }
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
    /// The date does not have one of the forms of the type, or it is not a
    /// day of the calendar.
    BadDate,
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
                "the date of a time is YYYY-MM-DD, YYYYMMDD or a week date, and a day of the calendar"
            }
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
    fn of([hour, minute, second]: [i64; CLOCK_PARTS], micros: i64) -> Self {
        Self {
            hour,
            minute,
            second,
            micros,
        }
    }

    fn seconds(&self) -> i64 {
        self.hour * SECONDS_PER_HOUR + self.minute * SECONDS_PER_MINUTE + self.second
    }

    fn is_time_of_day(&self) -> bool {
        self.hour < 24 && self.minute < 60 && self.second < 60
    }
}

/// The byte that ends a text for the reader of Python. That reader works on a
/// text with this byte after its last character, and it reads this byte
/// inside a text as the end.
const NUL: u8 = 0;

/// The byte at `at`, or [`NUL`] after the last byte.
fn byte(bytes: &[u8], at: usize) -> u8 {
    bytes.get(at).copied().unwrap_or(NUL)
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

/// The count of bytes of `YYYYWww`, of `YYYYMMDD` and `YYYY-Www`, and of
/// `YYYY-MM-DD` and `YYYY-Www-D`.
const SHORT_DATE: usize = 7;
const MIDDLE_DATE: usize = 8;
const LONG_DATE: usize = 10;

/// The count of bytes of the date at the start of a text. The separator
/// character is after them. `None` for a text that has no such count.
///
/// Each character can be the separator, a digit too. A week date then has
/// more than one reading, and the function takes the reading of Python:
///
/// - `YYYY-Www-` and a digit two bytes later: the date is `YYYY-Www`, and the
///   `-` is the separator.
/// - `YYYYWww` and a run of digits: the date takes one digit of the run as the
///   day when the run has an odd count of digits or one digit.
fn date_len(bytes: &[u8]) -> Option<usize> {
    let len = bytes.len();
    if len == SHORT_DATE {
        return Some(SHORT_DATE);
    }

    if byte(bytes, 4) == b'-' {
        if byte(bytes, 5) != WEEK {
            return Some(LONG_DATE);
        }

        if byte(bytes, MIDDLE_DATE) != b'-' {
            return Some(MIDDLE_DATE);
        }

        if len == MIDDLE_DATE + 1 {
            return None;
        }

        if byte(bytes, LONG_DATE).is_ascii_digit() {
            return Some(MIDDLE_DATE);
        }

        return Some(LONG_DATE);
    }

    if byte(bytes, 4) != WEEK {
        return Some(MIDDLE_DATE);
    }

    let digits_end = (SHORT_DATE..len)
        .find(|at| !byte(bytes, *at).is_ascii_digit())
        .unwrap_or(len);
    if digits_end <= MIDDLE_DATE {
        return Some(digits_end);
    }

    Some(if digits_end % 2 == 0 {
        SHORT_DATE
    } else {
        MIDDLE_DATE
    })
}

/// The date in the first `len` bytes of a text: a day of a month, or a day of
/// a week of a year.
fn read_date(bytes: &[u8], len: usize) -> Result<Date, TimestampError> {
    let year = digits(bytes, 0, 4).ok_or(TimestampError::BadDate)?;
    let extended = byte(bytes, 4) == b'-';
    let after_year = if extended { 5 } else { 4 };
    if byte(bytes, after_year) == WEEK {
        return read_week_date(bytes, len, year, after_year + 1, extended);
    }

    let month = digits(bytes, after_year, 2).ok_or(TimestampError::BadDate)?;
    let after_month = after_year + 2;
    if extended && byte(bytes, after_month) != b'-' {
        return Err(TimestampError::BadDate);
    }

    let day_at = if extended {
        after_month + 1
    } else {
        after_month
    };
    let day = digits(bytes, day_at, 2).ok_or(TimestampError::BadDate)?;

    Ok(Date { year, month, day })
}

/// The first and the last day of a week: Monday and Sunday.
const WEEK_DAY_MIN: i64 = 1;
const WEEK_DAY_MAX: i64 = 7;

/// The day of a week, from 0 for Monday, of Thursday and of Wednesday.
const THURSDAY: i64 = 3;
const WEDNESDAY: i64 = 2;

/// The last week of each year, and the last week of a long year.
const WEEKS: i64 = 52;
const WEEKS_LONG: i64 = 53;

/// The day of the calendar of a week date. The week starts at `week_at`. A
/// week date with no day is the Monday of the week.
fn read_week_date(
    bytes: &[u8],
    len: usize,
    year: i64,
    week_at: usize,
    extended: bool,
) -> Result<Date, TimestampError> {
    let week = digits(bytes, week_at, 2).ok_or(TimestampError::BadDate)?;
    let after_week = week_at + 2;
    let day = if len <= after_week {
        WEEK_DAY_MIN
    } else {
        if extended && byte(bytes, after_week) != b'-' {
            return Err(TimestampError::BadDate);
        }

        let day_at = if extended { after_week + 1 } else { after_week };

        digits(bytes, day_at, 1).ok_or(TimestampError::BadDate)?
    };
    if !(YEAR_MIN..=YEAR_MAX).contains(&year) {
        return Err(TimestampError::BadDate);
    }

    // 1970-01-01 is a Thursday.
    let first_day = days_from_civil(year, 1, 1);
    let first_weekday = (first_day + THURSDAY).rem_euclid(7);
    let weeks = if first_weekday == THURSDAY || (first_weekday == WEDNESDAY && is_leap(year)) {
        WEEKS_LONG
    } else {
        WEEKS
    };
    if !(1..=weeks).contains(&week) || !(WEEK_DAY_MIN..=WEEK_DAY_MAX).contains(&day) {
        return Err(TimestampError::BadDate);
    }

    // Week 1 is the week that holds the first Thursday of the year.
    let first_monday = if first_weekday > THURSDAY {
        first_day - first_weekday + 7
    } else {
        first_day - first_weekday
    };
    let (year, month, day) = civil_from_days(first_monday + (week - 1) * 7 + day - 1);

    Ok(Date { year, month, day })
}

/// The time of a day and the offset in microseconds, from the text after the
/// separator.
///
/// The first `Z`, `+` or `-` starts the offset. With an offset, the reader of
/// Python does not look at what is between the time and the offset, and this
/// function does the same.
fn read_time(bytes: &[u8]) -> Result<(Clock, i64), TimestampError> {
    let marker = bytes
        .iter()
        .position(|byte| matches!(byte, b'Z' | b'+' | b'-'));
    let Some(marker) = marker else {
        let (clock, more) = read_clock(bytes, 0, bytes.len()).ok_or(TimestampError::BadTime)?;
        if more {
            return Err(TimestampError::BadTime);
        }

        return Ok((clock, 0));
    };
    let (clock, _) = read_clock(bytes, 0, marker).ok_or(TimestampError::BadTime)?;
    let sign = byte(bytes, marker);
    if sign == b'Z' {
        // The parser replaced a final `Z`, so this one is not the last byte.
        if byte(bytes, marker + 1) != NUL {
            return Err(TimestampError::BadOffset);
        }

        return Ok((clock, 0));
    }

    let (offset, more) =
        read_clock(bytes, marker + 1, bytes.len()).ok_or(TimestampError::BadOffset)?;
    if more {
        return Err(TimestampError::BadOffset);
    }

    // An offset of a fraction of a second alone: Python 3.12 reads it as no
    // offset, and a later version applies it.
    if offset.seconds() == 0 && offset.micros != 0 {
        return Err(TimestampError::BadOffset);
    }

    let magnitude = offset.seconds() * MICROS_PER_SECOND + offset.micros;
    if magnitude >= MICROS_PER_DAY {
        return Err(TimestampError::BadOffset);
    }

    Ok((clock, if sign == b'-' { -magnitude } else { magnitude }))
}

/// The count of the parts of a time: hours, minutes and seconds.
const CLOCK_PARTS: usize = 3;

/// A time of a day or an offset in the bytes from `start` to `end`: `HH`,
/// `HH:MM`, `HH:MM:SS`, `HHMM` or `HHMMSS`, and a fraction.
///
/// The second value says whether the reader stopped before the end of the
/// text. `None` for a text that Python's reader refuses. The function checks
/// no range. It is a port of `parse_hh_mm_ss_ff` of CPython, step for step.
fn read_clock(bytes: &[u8], start: usize, end: usize) -> Option<(Clock, bool)> {
    let mut parts = [0_i64; CLOCK_PARTS];
    let mut at = start;
    let mut separated = false;
    for (index, part) in parts.iter_mut().enumerate() {
        *part = digits(bytes, at, 2)?;
        let next = byte(bytes, at + 2);
        at += 3;
        if index == 0 {
            separated = next == b':';
        }

        let fraction_mark = matches!(next, b'.' | b',');
        if at >= end {
            // A fraction mark with no digit after it: Python 3.12 reads the
            // text, and a later version refuses it.
            return (!fraction_mark).then(|| (Clock::of(parts, 0), next != NUL));
        }

        // A `:` after the seconds, and a fraction after the hours or after
        // the minutes: Python 3.14 refuses them, and an older version reads
        // a fraction there.
        let last = index + 1 == CLOCK_PARTS;
        if separated && next == b':' && !last {
            continue;
        }

        if fraction_mark && last {
            break;
        }

        if separated || fraction_mark {
            return None;
        }

        // `HHMM` and `HHMMSS`: the byte after a part starts the next part,
        // or a fraction after the seconds.
        at -= 1;
    }

    let count = end.checked_sub(at)?.min(FRACTION_DIGITS);
    let value = digits(bytes, at, count)?;
    let scale = (count..FRACTION_DIGITS).fold(1, |scale, _| scale * 10);
    let after = (at + count..bytes.len())
        .find(|at| !byte(bytes, *at).is_ascii_digit())
        .unwrap_or(bytes.len());

    Some((Clock::of(parts, value * scale), byte(bytes, after) != NUL))
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
        // A week date.
        ("2026-W38-6", "2026-09-19T00:00:00.000Z"),
        ("2026W386", "2026-09-19T00:00:00.000Z"),
        ("2026-W38", "2026-09-14T00:00:00.000Z"),
        ("2026W38", "2026-09-14T00:00:00.000Z"),
        ("2026-W38-6T06:00:00Z", "2026-09-19T06:00:00.000Z"),
        ("2026-W38-06:00", "2026-09-14T06:00:00.000Z"),
        ("2026W386060000", "2026-09-14T06:00:00.000Z"),
        ("2026-W53-1", "2026-12-28T00:00:00.000Z"),
        ("2020-W53-7", "2021-01-03T00:00:00.000Z"),
        ("0001-W01-1", "0001-01-01T00:00:00.000Z"),
        ("9999-W52-5", "9999-12-31T00:00:00.000Z"),
        // One character between the time and the offset.
        ("2026-10-06T06:00:00 Z", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06:00:009Z", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06:00:+05:30", "2026-10-06T00:30:00.000Z"),
        ("2026-10-06 06:00:00 +0200", "2026-10-06T04:00:00.000Z"),
        ("2026-10-06T06 Z", "2026-10-06T06:00:00.000Z"),
        // Text after a fraction of six digits or more, before an offset.
        (
            "2026-10-06T06:00:00.123456abc+05:30",
            "2026-10-06T00:30:00.123Z",
        ),
        ("2026-10-06T06:00:00.1234567 Z", "2026-10-06T06:00:00.123Z"),
        // Digits after HHMMSS are a fraction.
        ("2026-10-06T06000530", "2026-10-06T06:00:05.300Z"),
        (
            "2026-10-06T060005301234.5+00:00",
            "2026-10-06T06:00:05.301Z",
        ),
        // A NUL ends the text.
        ("2026-10-06T06:00:00Z\0abc", "2026-10-06T06:00:00.000Z"),
        ("2026-10-06T06:00:00\0", "2026-10-06T06:00:00.000Z"),
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
        ("2026-10", TimestampError::BadDate),
        ("2025-W53-1", TimestampError::BadDate),
        ("2026-W54-1", TimestampError::BadDate),
        ("2026-W00-1", TimestampError::BadDate),
        ("2026-W38-0", TimestampError::BadDate),
        ("2026-W38-8", TimestampError::BadDate),
        ("2026-W38-", TimestampError::BadDate),
        ("2026-W3", TimestampError::BadDate),
        ("0000-W01-1", TimestampError::BadDate),
        ("9999-W52-6", TimestampError::BadDate),
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
        ("2026-W38x6", TimestampError::BadTime),
        ("2026-10-06T06:00:00:5Z", TimestampError::BadTime),
        ("2026-10-06T06:00:00,Z", TimestampError::BadTime),
        ("2026-10-06T06.Z", TimestampError::BadTime),
        ("2026-10-06T06:00:00  Z", TimestampError::BadTime),
        ("2026-10-06T06:00:00 ", TimestampError::BadTime),
        ("2026-10-06T06:00:00.12 Z", TimestampError::BadTime),
        ("2026-10-06T0600003", TimestampError::BadTime),
        ("2026-10-06T06:00:00z", TimestampError::BadTime),
        ("2026-10-06T06:00:00UTC", TimestampError::BadTime),
        ("2026-10-06T06:00:00Z ", TimestampError::BadOffset),
        ("2026-10-06T06:00:00Z\n", TimestampError::BadOffset),
        ("2026-10-06T06:00:00ZZ", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+5", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+24:00", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+00:00Z", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+05:30 ", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+05.5", TimestampError::BadOffset),
        ("2026-10-06T06:00:00+00:00:00.5", TimestampError::BadOffset),
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
        let error: Box<dyn Error> = Box::new(TimestampError::Empty);

        assert_eq!(error.to_string(), "a time is not empty");
    }
}
