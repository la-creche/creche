//! The time fields of contract 05, and the staleness rule.
//!
//! [`Timestamp`] is one instant in UTC. The rule for the UTC offset is:
//!
//! 1. A text names its offset: `Z`, or a sign with hours and minutes.
//! 2. A text with no offset is not a time.
//! 3. The value is the instant in UTC. The offset of the text is not kept.
//! 4. A writer writes `Z` and whole seconds.
//!
//! Each writer of contract 05 writes `YYYY-MM-DDTHH:MM:SSZ`. A reader also
//! takes a fraction of a second, an offset and a space in place of the `T`,
//! because `vectors/data/status` holds such a text and each Python reader
//! takes it.

use std::error::Error;
use std::fmt;
use std::str::FromStr;
use std::time::{SystemTime, UNIX_EPOCH};

/// A file whose `written_at` is older than this is stale: contract 05 §2
/// rule 5 for a status document, and §3.3.1 rule 7 for a fault file.
pub const STALE_AFTER_SECONDS: i64 = 90;

const MICROS_PER_SECOND: i64 = 1_000_000;
const SECONDS_PER_MINUTE: i64 = 60;
const SECONDS_PER_HOUR: i64 = 3600;
const SECONDS_PER_DAY: i64 = 86_400;

/// The count of digits that a microsecond has.
const MICRO_DIGITS: usize = 6;

/// The days of one era of the calendar: 400 years.
const DAYS_PER_ERA: i64 = 146_097;
const YEARS_PER_ERA: i64 = 400;

/// The days from 0000-03-01 to 1970-01-01.
const EPOCH_SHIFT: i64 = 719_468;

/// The first year and the last year of a time. Python holds no other year,
/// so no Python writer makes one.
const YEAR_MIN: i64 = 1;
const YEAR_MAX: i64 = 9999;

/// The first instant and the last instant of a time, in microseconds.
const MICROS_MIN: i64 = days_from_civil(YEAR_MIN, 1, 1) * SECONDS_PER_DAY * MICROS_PER_SECOND;
const MICROS_MAX: i64 =
    (days_from_civil(YEAR_MAX, 12, 31) + 1) * SECONDS_PER_DAY * MICROS_PER_SECOND - 1;

/// The shortest text: `YYYY-MM-DDTHH:MM:SS`, with no offset.
const DATE_TIME_BYTES: usize = 19;

/// The days from 1970-01-01 to a date of the Gregorian calendar.
const fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    // The year starts in March, so a leap day is the last day of a year.
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(YEARS_PER_ERA);
    let year_of_era = year.rem_euclid(YEARS_PER_ERA);
    let month_from_march = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * month_from_march + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;

    era * DAYS_PER_ERA + day_of_era - EPOCH_SHIFT
}

/// The date of the Gregorian calendar that is `days` after 1970-01-01.
const fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let days = days + EPOCH_SHIFT;
    let era = days.div_euclid(DAYS_PER_ERA);
    let day_of_era = days.rem_euclid(DAYS_PER_ERA);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_from_march = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_from_march + 2) / 5 + 1;
    let month = if month_from_march < 10 {
        month_from_march + 3
    } else {
        month_from_march - 9
    };
    let year = year_of_era + era * YEARS_PER_ERA;

    (if month <= 2 { year + 1 } else { year }, month, day)
}

const fn is_leap_year(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

const fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if is_leap_year(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

// CONTRACT-QUESTION: contract 05 §2.1 names RFC 3339 and no more. The Python
// readers take what `datetime.fromisoformat` takes. That set differs between
// two Python versions, and it holds forms that RFC 3339 does not: a date with
// no time, a time with no seconds, a week date, each character in place of
// the `T`, a comma before the fraction and an offset with seconds. Three
// readers read a time with no offset as UTC. The type takes RFC 3339 with
// `T` or a space, with `Z` in upper case, and with a year from 0001 to 9999
// in UTC. A reader calls a file with another text stale. To take a further
// form, add it here and add a vector for each reader.
/// One instant in UTC, to the microsecond (contract 05 §2.1, "RFC 3339").
///
/// The text names its UTC offset. A text with no offset is not a time.
///
/// ```
/// use creche_contracts::status::time::Timestamp;
///
/// let written: Timestamp = "2031-04-18T08:42:35+02:00".parse()?;
/// assert_eq!(written.to_rfc3339(), "2031-04-18T06:42:35Z");
/// assert!("2031-04-18T06:42:35".parse::<Timestamp>().is_err());
/// # Ok::<(), creche_contracts::status::time::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::time::Timestamp;
///
/// let written = Timestamp { micros: i64::MAX };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Timestamp {
    /// Microseconds from 1970-01-01T00:00:00Z.
    micros: i64,
}

/// The fields of a text, before the offset moves them to UTC.
struct Fields {
    year: i64,
    month: i64,
    day: i64,
    hour: i64,
    minute: i64,
    second: i64,
}

impl Timestamp {
    /// The instant that is `micros` microseconds after 1970-01-01T00:00:00Z.
    ///
    /// # Errors
    ///
    /// [`TimestampError::OutOfRange`] for an instant before the year 0001 or
    /// after the year 9999.
    pub fn from_unix_micros(micros: i64) -> Result<Self, TimestampError> {
        if !(MICROS_MIN..=MICROS_MAX).contains(&micros) {
            return Err(TimestampError::OutOfRange);
        }

        Ok(Self { micros })
    }

    /// The microseconds from 1970-01-01T00:00:00Z to the instant.
    #[must_use]
    pub fn unix_micros(self) -> i64 {
        self.micros
    }

    /// The text that each writer of contract 05 writes: UTC, whole seconds
    /// and `Z`. The fraction of a second is not written.
    #[must_use]
    pub fn to_rfc3339(self) -> String {
        let seconds = self.micros.div_euclid(MICROS_PER_SECOND);
        let (year, month, day) = civil_from_days(seconds.div_euclid(SECONDS_PER_DAY));
        let of_day = seconds.rem_euclid(SECONDS_PER_DAY);
        let hour = of_day / SECONDS_PER_HOUR;
        let minute = of_day % SECONDS_PER_HOUR / SECONDS_PER_MINUTE;
        let second = of_day % SECONDS_PER_MINUTE;

        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
    }

    /// The microseconds of the second of the instant: 0 to 999999.
    #[must_use]
    pub fn subsecond_micros(self) -> i64 {
        self.micros.rem_euclid(MICROS_PER_SECOND)
    }

    /// How old the instant is at `now`. A time after `now` has the age zero:
    /// the clocks of two processes can differ.
    #[must_use]
    pub fn age_at(self, now: Self) -> Age {
        Age {
            micros: now.micros.saturating_sub(self.micros).max(0),
        }
    }

    /// Whether a file that was written at this instant is stale at `now`.
    #[must_use]
    pub fn freshness_at(self, now: Self) -> Freshness {
        self.age_at(now).freshness()
    }

    fn read(text: &str) -> Result<Self, TimestampError> {
        let bytes = text.as_bytes();
        let fields = Fields {
            year: number(bytes, 0, 4)?,
            month: number(bytes, 5, 2)?,
            day: number(bytes, 8, 2)?,
            hour: number(bytes, 11, 2)?,
            minute: number(bytes, 14, 2)?,
            second: number(bytes, 17, 2)?,
        };
        let marks = [(4, b'-'), (7, b'-'), (13, b':'), (16, b':')];
        if marks.iter().any(|(at, mark)| bytes.get(*at) != Some(mark)) {
            return Err(TimestampError::Form);
        }

        if !matches!(bytes.get(10), Some(b'T' | b' ')) {
            return Err(TimestampError::Form);
        }

        let rest = bytes.get(DATE_TIME_BYTES..).unwrap_or_default();
        let (micros, rest) = fraction(rest)?;
        let offset_seconds = offset(rest)?;
        let local_seconds = fields.seconds()?;
        let utc = (local_seconds - offset_seconds) * MICROS_PER_SECOND + micros;

        Self::from_unix_micros(utc)
    }
}

impl Fields {
    /// The seconds from 1970-01-01T00:00:00 to the fields, with no offset.
    fn seconds(&self) -> Result<i64, TimestampError> {
        let in_month = 1..=days_in_month(self.year, self.month);
        if !(YEAR_MIN..=YEAR_MAX).contains(&self.year)
            || !(1..=12).contains(&self.month)
            || !in_month.contains(&self.day)
        {
            return Err(TimestampError::NotADate);
        }

        if self.hour > 23 || self.minute > 59 || self.second > 59 {
            return Err(TimestampError::NotATimeOfDay);
        }

        let days = days_from_civil(self.year, self.month, self.day);

        Ok(days * SECONDS_PER_DAY
            + self.hour * SECONDS_PER_HOUR
            + self.minute * SECONDS_PER_MINUTE
            + self.second)
    }
}

/// The number that `count` ASCII digits at the offset `at` make.
fn number(bytes: &[u8], at: usize, count: usize) -> Result<i64, TimestampError> {
    let digits = bytes.get(at..at + count).ok_or(TimestampError::Form)?;
    let mut value = 0_i64;
    for digit in digits {
        if !digit.is_ascii_digit() {
            return Err(TimestampError::Form);
        }

        value = value * 10 + i64::from(digit - b'0');
    }

    Ok(value)
}

/// The fraction of a second at the start of `rest`, in microseconds, and the
/// bytes after it. A digit after the sixth is cut off, not rounded.
fn fraction(rest: &[u8]) -> Result<(i64, &[u8]), TimestampError> {
    let Some((b'.', after)) = rest.split_first() else {
        return Ok((0, rest));
    };
    let count = after
        .iter()
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    if count == 0 {
        return Err(TimestampError::Form);
    }

    let (digits, after) = after.split_at_checked(count).ok_or(TimestampError::Form)?;
    let mut micros = 0_i64;
    for place in 0..MICRO_DIGITS {
        let digit = digits.get(place).map_or(0, |digit| i64::from(digit - b'0'));
        micros = micros * 10 + digit;
    }

    Ok((micros, after))
}

/// The offset from UTC that `rest` names, in seconds.
fn offset(rest: &[u8]) -> Result<i64, TimestampError> {
    let sign = match rest {
        [] => return Err(TimestampError::NoOffset),
        [b'Z'] => return Ok(0),
        [b'+', ..] => 1,
        [b'-', ..] => -1,
        _ => return Err(TimestampError::Form),
    };
    if rest.len() != "+00:00".len() || rest.get(3) != Some(&b':') {
        return Err(TimestampError::Form);
    }

    let hours = number(rest, 1, 2)?;
    let minutes = number(rest, 4, 2)?;
    if hours > 23 || minutes > 59 {
        return Err(TimestampError::NotAnOffset);
    }

    Ok(sign * (hours * SECONDS_PER_HOUR + minutes * SECONDS_PER_MINUTE))
}

impl FromStr for Timestamp {
    type Err = TimestampError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::read(text)
    }
}

impl TryFrom<SystemTime> for Timestamp {
    type Error = TimestampError;

    /// The instant of a clock reading. No clock of a host gives a time before
    /// 1970, so such a time is out of range.
    fn try_from(time: SystemTime) -> Result<Self, Self::Error> {
        let since = time
            .duration_since(UNIX_EPOCH)
            .map_err(|_| TimestampError::OutOfRange)?;
        let micros = i64::try_from(since.as_micros()).map_err(|_| TimestampError::OutOfRange)?;

        Self::from_unix_micros(micros)
    }
}

impl fmt::Display for Timestamp {
    /// The instant with its fraction: `2031-04-18T06:42:35.250000Z`. A whole
    /// second has no fraction.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let seconds = self.to_rfc3339();
        let micros = self.subsecond_micros();
        if micros == 0 {
            return f.write_str(&seconds);
        }

        write!(f, "{}.{micros:06}Z", seconds.trim_end_matches('Z'))
    }
}

/// Why a text is not a time.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimestampError {
    /// The text is not `YYYY-MM-DD`, `T` or a space, `HH:MM:SS`, an optional
    /// fraction and an offset.
    Form,
    /// The text ends with no UTC offset.
    NoOffset,
    /// The year, the month and the day are not a date from 0001 to 9999.
    NotADate,
    /// The hour is above 23, or the minute or the second is above 59.
    NotATimeOfDay,
    /// The hours of the offset are above 23, or its minutes are above 59.
    NotAnOffset,
    /// The instant in UTC is before the year 0001 or after the year 9999.
    OutOfRange,
}

impl fmt::Display for TimestampError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Form => "a time is YYYY-MM-DDTHH:MM:SS, an optional fraction and a UTC offset",
            Self::NoOffset => "a time names its UTC offset: Z, or a sign with hours and minutes",
            Self::NotADate => "the date of a time is a day of a year from 0001 to 9999",
            Self::NotATimeOfDay => "a time has an hour to 23, a minute to 59 and a second to 59",
            Self::NotAnOffset => "a UTC offset has hours to 23 and minutes to 59",
            Self::OutOfRange => "a time in UTC is in a year from 0001 to 9999",
        })
    }
}

impl Error for TimestampError {}

/// How old a file is: zero or more microseconds.
///
/// ```
/// use creche_contracts::status::time::{Age, Freshness, Timestamp};
///
/// let written: Timestamp = "2031-04-18T06:42:35Z".parse()?;
/// let now: Timestamp = "2031-04-18T06:44:06Z".parse()?;
/// let age: Age = written.age_at(now);
/// assert_eq!(age.whole_seconds(), 91);
/// assert_eq!(age.freshness(), Freshness::Stale);
/// # Ok::<(), creche_contracts::status::time::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::time::{Age, Freshness, Timestamp};
///
/// let age = Age { micros: -1 };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Age {
    micros: i64,
}

impl Age {
    /// The age in whole seconds. A part of a second is cut off.
    #[must_use]
    pub fn whole_seconds(self) -> i64 {
        self.micros / MICROS_PER_SECOND
    }

    /// The age in seconds, as the nearest `f64`.
    #[must_use]
    pub fn seconds(self) -> f64 {
        // The decimal text has the exact value, and `parse` gives the nearest
        // float. Python divides two integers to the same result.
        let text = format!(
            "{}.{:06}",
            self.whole_seconds(),
            self.micros % MICROS_PER_SECOND
        );

        text.parse().unwrap_or(f64::INFINITY)
    }

    /// Whether a file of this age is stale: older than
    /// [`STALE_AFTER_SECONDS`], and not equal to it.
    #[must_use]
    pub fn freshness(self) -> Freshness {
        if self.micros > STALE_AFTER_SECONDS * MICROS_PER_SECOND {
            Freshness::Stale
        } else {
            Freshness::Fresh
        }
    }
}

/// Whether a reader can take a file as the state now.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Freshness {
    /// The writer wrote the file 90 seconds ago or less.
    Fresh,
    /// The writer wrote the file more than 90 seconds ago, or the file has
    /// no time that a reader can read. The writer is not running.
    Stale,
}

/// Whether a file with this `written_at` is stale at `now`. A file with no
/// time that a reader can read is stale.
#[must_use]
pub fn freshness(written_at: Option<Timestamp>, now: Timestamp) -> Freshness {
    written_at.map_or(Freshness::Stale, |written| written.freshness_at(now))
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    fn time(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    const ACCEPTED: &[(&str, &str)] = &[
        ("2031-04-18T06:42:35Z", "2031-04-18T06:42:35Z"),
        ("2031-04-18 06:42:35Z", "2031-04-18T06:42:35Z"),
        ("2031-04-18T06:42:35+00:00", "2031-04-18T06:42:35Z"),
        ("2031-04-18T06:42:35-00:00", "2031-04-18T06:42:35Z"),
        ("2031-04-18T08:42:35+02:00", "2031-04-18T06:42:35Z"),
        ("2031-04-18T01:12:35-05:30", "2031-04-18T06:42:35Z"),
        ("2031-04-19T06:41:35+23:59", "2031-04-18T06:42:35Z"),
        ("2031-04-18T06:42:35.5Z", "2031-04-18T06:42:35.500000Z"),
        ("2031-04-18T06:42:35.123456Z", "2031-04-18T06:42:35.123456Z"),
        (
            "2031-04-18T06:42:35.1234569Z",
            "2031-04-18T06:42:35.123456Z",
        ),
        ("2031-04-18T06:42:35.000000Z", "2031-04-18T06:42:35Z"),
        (
            "2031-04-18T08:42:35.25+02:00",
            "2031-04-18T06:42:35.250000Z",
        ),
        ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"),
        ("1969-12-31T23:59:59.999999Z", "1969-12-31T23:59:59.999999Z"),
        ("2024-02-29T12:00:00Z", "2024-02-29T12:00:00Z"),
        ("2000-02-29T00:00:00Z", "2000-02-29T00:00:00Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
        ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999999Z"),
        ("0001-01-01T02:00:00+02:00", "0001-01-01T00:00:00Z"),
        ("2999-01-01T00:00:00Z", "2999-01-01T00:00:00Z"),
    ];

    const REFUSED: &[(&str, TimestampError)] = &[
        ("", TimestampError::Form),
        ("yesterday", TimestampError::Form),
        ("2031-04-18", TimestampError::Form),
        ("2031-04-18T19:20", TimestampError::Form),
        ("2031-04-18T06:42:35", TimestampError::NoOffset),
        ("2031-04-18T06:42:35.5", TimestampError::NoOffset),
        ("2031-04-18T06:42:35z", TimestampError::Form),
        ("2031-04-18t06:42:35Z", TimestampError::Form),
        ("2031-04-18_06:42:35Z", TimestampError::Form),
        ("2031-04-18T06:42:35ZZ", TimestampError::Form),
        ("2031-04-18T06:42:35Z\n", TimestampError::Form),
        ("2031-04-18T06:42:35Z ", TimestampError::Form),
        (" 2031-04-18T06:42:35Z", TimestampError::Form),
        ("2031-04-18T06:42:35.Z", TimestampError::Form),
        ("2031-04-18T06:42:35,5Z", TimestampError::Form),
        ("2031-04-18T06:42:35+02", TimestampError::Form),
        ("2031-04-18T06:42:35+0200", TimestampError::Form),
        ("2031-04-18T06:42:35+02:00:30", TimestampError::Form),
        ("2031-04-18T06:42:35+2:00", TimestampError::Form),
        ("2031-04-18T06:42:35 Z", TimestampError::Form),
        ("2031-04-18T06:42:35+00:00Z", TimestampError::Form),
        ("20310418T192012Z", TimestampError::Form),
        ("2031-W16-5T06:42:35Z", TimestampError::Form),
        ("2031-4-18T06:42:35Z", TimestampError::Form),
        ("\u{ff12}031-04-18T06:42:35Z", TimestampError::Form),
        ("2031-04-18T06:42:3\u{665}Z", TimestampError::Form),
        ("2031-04-18T06:42:35\u{2212}02:00", TimestampError::Form),
        ("0000-01-01T00:00:00Z", TimestampError::NotADate),
        ("2031-00-18T06:42:35Z", TimestampError::NotADate),
        ("2031-13-18T06:42:35Z", TimestampError::NotADate),
        ("2031-04-00T06:42:35Z", TimestampError::NotADate),
        ("2031-04-31T06:42:35Z", TimestampError::NotADate),
        ("2031-02-29T06:42:35Z", TimestampError::NotADate),
        ("1900-02-29T06:42:35Z", TimestampError::NotADate),
        ("2031-04-18T24:00:00Z", TimestampError::NotATimeOfDay),
        ("2031-04-18T06:60:35Z", TimestampError::NotATimeOfDay),
        ("2031-04-18T23:59:60Z", TimestampError::NotATimeOfDay),
        ("2031-04-18T06:42:35+24:00", TimestampError::NotAnOffset),
        ("2031-04-18T06:42:35+00:60", TimestampError::NotAnOffset),
        ("0001-01-01T00:00:00+00:01", TimestampError::OutOfRange),
        ("9999-12-31T23:59:59-00:01", TimestampError::OutOfRange),
    ];

    #[test]
    fn a_text_in_the_grammar_is_the_instant_in_utc() {
        for (text, utc) in ACCEPTED {
            assert_eq!(time(text).to_string(), *utc, "{text}");
        }
    }

    #[test]
    fn a_text_outside_the_grammar_is_refused() {
        for (text, error) in REFUSED {
            assert_eq!(text.parse::<Timestamp>(), Err(*error), "{text:?}");
        }
    }

    #[test]
    fn a_writer_writes_whole_seconds_and_z() {
        assert_eq!(
            time("2031-04-18T08:42:35.999999+02:00").to_rfc3339(),
            "2031-04-18T06:42:35Z"
        );
        assert_eq!(
            time("1969-12-31T23:59:59.5Z").to_rfc3339(),
            "1969-12-31T23:59:59Z"
        );
        assert_eq!(
            time("0001-01-01T00:00:00Z").to_rfc3339(),
            "0001-01-01T00:00:00Z"
        );
    }

    #[test]
    fn each_day_of_four_centuries_reads_back() {
        let first = days_from_civil(1999, 12, 31);
        let mut text = String::new();
        for days in first..first + DAYS_PER_ERA + 2 {
            let (year, month, day) = civil_from_days(days);
            text.clear();
            text.push_str(&format!("{year:04}-{month:02}-{day:02}T00:00:00Z"));
            let read = time(&text);

            assert_eq!(days_from_civil(year, month, day), days, "{text}");
            assert_eq!(
                read.unix_micros(),
                days * SECONDS_PER_DAY * MICROS_PER_SECOND,
                "{text}"
            );
            assert_eq!(read.to_rfc3339(), text);
        }
    }

    #[test]
    fn the_unix_epoch_is_zero() {
        assert_eq!(time("1970-01-01T00:00:00Z").unix_micros(), 0);
        assert_eq!(time("1970-01-01T00:00:01.5Z").unix_micros(), 1_500_000);
        assert_eq!(
            time("2999-01-01T00:00:00Z").unix_micros(),
            32_472_144_000_000_000
        );
        assert_eq!(
            time("2020-01-01T00:00:00Z").unix_micros(),
            1_577_836_800_000_000
        );
    }

    #[test]
    fn a_raw_number_outside_the_years_is_refused() {
        let first = time("0001-01-01T00:00:00Z").unix_micros();
        let last = time("9999-12-31T23:59:59.999999Z").unix_micros();

        assert_eq!(
            Timestamp::from_unix_micros(first),
            Ok(time("0001-01-01T00:00:00Z"))
        );
        assert_eq!(
            Timestamp::from_unix_micros(first - 1),
            Err(TimestampError::OutOfRange)
        );
        assert!(Timestamp::from_unix_micros(last).is_ok());
        assert_eq!(
            Timestamp::from_unix_micros(last + 1),
            Err(TimestampError::OutOfRange)
        );
        assert_eq!(
            Timestamp::from_unix_micros(i64::MIN),
            Err(TimestampError::OutOfRange)
        );
    }

    #[test]
    fn a_clock_reading_is_an_instant() {
        let reading = UNIX_EPOCH + Duration::from_micros(1_500_000);
        let too_late = UNIX_EPOCH + Duration::from_secs(400_000_000_000);

        assert_eq!(
            Timestamp::try_from(reading),
            Ok(time("1970-01-01T00:00:01.5Z"))
        );
        assert_eq!(
            Timestamp::try_from(UNIX_EPOCH - Duration::from_secs(1)),
            Err(TimestampError::OutOfRange)
        );
        assert_eq!(
            Timestamp::try_from(too_late),
            Err(TimestampError::OutOfRange)
        );
        assert!(Timestamp::try_from(SystemTime::now()).is_ok());
    }

    #[test]
    fn a_file_is_stale_after_more_than_90_seconds() {
        let now = time("2999-01-01T00:00:30Z");
        let ages = [
            ("2999-01-01T00:00:00Z", 30, Freshness::Fresh),
            ("2998-12-31T23:59:00Z", 90, Freshness::Fresh),
            ("2998-12-31T23:58:59.999999Z", 90, Freshness::Stale),
            ("2998-12-31T23:58:59Z", 91, Freshness::Stale),
            ("2020-01-01T00:00:00Z", 30_894_307_230, Freshness::Stale),
            ("2999-01-01T00:00:30Z", 0, Freshness::Fresh),
            ("2999-01-01T00:01:00Z", 0, Freshness::Fresh),
        ];

        for (written, seconds, expected) in ages {
            let age = time(written).age_at(now);

            assert_eq!(age.whole_seconds(), seconds, "{written}");
            assert_eq!(age.freshness(), expected, "{written}");
            assert_eq!(freshness(Some(time(written)), now), expected, "{written}");
        }

        assert_eq!(freshness(None, now), Freshness::Stale);
    }

    #[test]
    fn an_age_in_seconds_is_the_nearest_float() {
        let now = time("2999-01-01T00:00:30Z");
        let ages = [
            ("2999-01-01T00:00:00Z", 30.0),
            ("2999-01-01T00:00:00.123456Z", 29.876_544),
            ("2020-01-01T00:00:00Z", 30_894_307_230.0),
            ("2999-01-01T00:00:30Z", 0.0),
            ("2999-01-01T00:00:31Z", 0.0),
        ];

        for (written, seconds) in ages {
            assert_eq!(
                time(written).age_at(now).seconds().to_bits(),
                f64::to_bits(seconds)
            );
        }
    }

    #[test]
    fn the_error_says_which_rule_failed() {
        let boxed: Box<dyn Error> = Box::new(TimestampError::NoOffset);

        assert_eq!(
            boxed.to_string(),
            "a time names its UTC offset: Z, or a sign with hours and minutes"
        );
    }
}
