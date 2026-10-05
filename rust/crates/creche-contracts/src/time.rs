//! One instant in UTC: the time that a file or a wire message holds.
//!
//! [`Timestamp`] is the one type of the workspace for such a time. The rule
//! is in `rust/AGENTS.md`, "Time":
//!
//! 1. A time text is a `date-time` of RFC 3339, section 5.6.
//! 2. A writer writes UTC with `Z`.
//! 3. A text with no UTC offset is not a time.
//!
//! This module also holds the one copy of the date arithmetic of the
//! workspace. The private type `Date` gives the day count of a date, and the
//! date of a day count. No other module computes one from the other.

use std::error::Error;
use std::fmt;
use std::str::FromStr;
use std::time::{SystemTime, UNIX_EPOCH};

const NANOS_PER_MICRO: i128 = 1_000;
const MICROS_PER_MILLI: i64 = 1_000;
const MICROS_PER_SECOND: i64 = 1_000_000;
const SECONDS_PER_MINUTE: i64 = 60;
const SECONDS_PER_HOUR: i64 = 3_600;
const SECONDS_PER_DAY: i64 = 86_400;
const MICROS_PER_DAY: i64 = SECONDS_PER_DAY * MICROS_PER_SECOND;

/// The count of digits of the year in a text, and of each other field.
const YEAR_DIGITS: usize = 4;
const FIELD_DIGITS: usize = 2;

/// The most digits that the fraction of a second has in a text.
const FRACTION_DIGITS_MAX: usize = 9;

/// The digits of a fraction that a microsecond holds. The reader drops each
/// digit after them.
const MICRO_DIGITS: usize = 6;

/// The first year and the last year of a time.
const YEAR_MIN: i64 = 1;
const YEAR_MAX: i64 = 9_999;

/// The largest value of each other field of a text.
const MONTH_MAX: i64 = 12;
const HOUR_MAX: i64 = 23;
const MINUTE_MAX: i64 = 59;
const SECOND_MAX: i64 = 59;

/// The Gregorian calendar repeats after one cycle: 400 years.
const YEARS_PER_CYCLE: i64 = 400;
const DAYS_PER_CYCLE: i64 = 146_097;

/// The days of 100 years with 24 leap days. Each century of a cycle has this
/// length, and the last one has one more day: the leap day of the cycle.
const YEARS_PER_CENTURY: i64 = 100;
const DAYS_PER_CENTURY: i64 = 36_524;

/// The days of 4 years with one leap day.
const YEARS_PER_LEAP_GROUP: i64 = 4;
const DAYS_PER_LEAP_GROUP: i64 = 1_461;

/// The days of a year with no leap day.
const DAYS_PER_YEAR: i64 = 365;

/// The number of the last century of a cycle and of the last year of a leap
/// group, from zero. Each of the two ends with a leap day.
const LAST_OF_FOUR: i64 = 3;

/// The day count of the calendar starts a year on March 1. A leap day is then
/// the last day of a year. March is month 3, and a year has 12 months.
const MARCH: i64 = 3;
const MONTHS_PER_YEAR: i64 = 12;

/// From March, the months have 31, 30, 31, 30 and 31 days, and the pattern
/// starts again. Five months have 153 days. The two formulas that use the
/// pattern add 2 before they divide. That term puts the two months of 30
/// days at the second place and at the fourth place.
const MONTHS_PER_PATTERN: i64 = 5;
const DAYS_PER_PATTERN: i64 = 153;

/// The days from 0000-03-01 to 1970-01-01.
const DAYS_TO_EPOCH: i64 = 719_468;

/// The first instant and the last instant of a time, in microseconds from
/// 1970-01-01T00:00:00Z.
const MICROS_MIN: i64 = Date::FIRST.days_from_epoch() * MICROS_PER_DAY;
const MICROS_MAX: i64 = (Date::LAST.days_from_epoch() + 1) * MICROS_PER_DAY - 1;

/// One day of the Gregorian calendar, in the years 0000 to 9999. The leap
/// year rule of today holds for each year.
///
/// This type is the one copy of the date arithmetic of the workspace.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Date {
    year: i64,
    month: i64,
    day: i64,
}

impl Date {
    /// The first day and the last day of a time in UTC.
    const FIRST: Self = Self {
        year: YEAR_MIN,
        month: 1,
        day: 1,
    };
    const LAST: Self = Self {
        year: YEAR_MAX,
        month: MONTH_MAX,
        day: 31,
    };

    /// The date, when the month is 1 to 12 and the month has the day.
    const fn new(year: i64, month: i64, day: i64) -> Option<Self> {
        if month < 1 || month > MONTH_MAX || day < 1 || day > days_in_month(year, month) {
            return None;
        }

        Some(Self { year, month, day })
    }

    /// The days from 1970-01-01 to the date. The count is negative for an
    /// earlier date.
    const fn days_from_epoch(self) -> i64 {
        let (year, month_from_march) = if self.month >= MARCH {
            (self.year, self.month - MARCH)
        } else {
            (self.year - 1, self.month + MONTHS_PER_YEAR - MARCH)
        };
        let cycle = year.div_euclid(YEARS_PER_CYCLE);
        let year_of_cycle = year.rem_euclid(YEARS_PER_CYCLE);
        let leap_days = year_of_cycle / YEARS_PER_LEAP_GROUP - year_of_cycle / YEARS_PER_CENTURY;
        let day_of_year = days_before_month(month_from_march) + self.day - 1;

        cycle * DAYS_PER_CYCLE + year_of_cycle * DAYS_PER_YEAR + leap_days + day_of_year
            - DAYS_TO_EPOCH
    }

    /// The date that is `days` after 1970-01-01.
    const fn of_day(days: i64) -> Self {
        let from_march = days + DAYS_TO_EPOCH;
        let cycle = from_march.div_euclid(DAYS_PER_CYCLE);
        let day_of_cycle = from_march.rem_euclid(DAYS_PER_CYCLE);

        // Each step takes the whole periods of one length from the count:
        // the centuries, then the groups of 4 years, then the years. The
        // last century of a cycle and the last year of a group end with a
        // leap day. On that day the quotient is one too large, and `at_most`
        // corrects it.
        let century = at_most(day_of_cycle / DAYS_PER_CENTURY, LAST_OF_FOUR);
        let day_of_century = day_of_cycle - century * DAYS_PER_CENTURY;
        let leap_group = day_of_century / DAYS_PER_LEAP_GROUP;
        let day_of_group = day_of_century - leap_group * DAYS_PER_LEAP_GROUP;
        let year_of_group = at_most(day_of_group / DAYS_PER_YEAR, LAST_OF_FOUR);
        let day_of_year = day_of_group - year_of_group * DAYS_PER_YEAR;

        let month_from_march = (MONTHS_PER_PATTERN * day_of_year + 2) / DAYS_PER_PATTERN;
        let day = day_of_year - days_before_month(month_from_march) + 1;
        let year_from_march = cycle * YEARS_PER_CYCLE
            + century * YEARS_PER_CENTURY
            + leap_group * YEARS_PER_LEAP_GROUP
            + year_of_group;

        if month_from_march + MARCH > MONTHS_PER_YEAR {
            Self {
                year: year_from_march + 1,
                month: month_from_march + MARCH - MONTHS_PER_YEAR,
                day,
            }
        } else {
            Self {
                year: year_from_march,
                month: month_from_march + MARCH,
                day,
            }
        }
    }
}

/// `value`, or `most` when `value` is larger.
const fn at_most(value: i64, most: i64) -> i64 {
    if value > most { most } else { value }
}

/// The days from March 1 to the first day of the month that is
/// `month_from_march` months after March.
const fn days_before_month(month_from_march: i64) -> i64 {
    (DAYS_PER_PATTERN * month_from_march + 2) / MONTHS_PER_PATTERN
}

const fn is_leap_year(year: i64) -> bool {
    year % YEARS_PER_LEAP_GROUP == 0
        && (year % YEARS_PER_CENTURY != 0 || year % YEARS_PER_CYCLE == 0)
}

const fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if is_leap_year(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

// CONTRACT-QUESTION: the contracts name RFC 3339 for a time and say no more
// about its grammar, for example contract 02 §13.2 rule 4 and contract 05
// §2.1. Section 5.6 of RFC 3339 permits three texts that this type refuses:
// 1. A fraction of more than 9 digits. The section gives a fraction no cap.
// 2. Second 60, the form of a leap second. A count of microseconds from 1970
//    has no place for that second.
// 3. The year 0000. No instant of the type is in that year. One day of the
//    year 0000 can name an instant of the year 0001 through its offset, for
//    example `0000-12-31T23:30:00-01:00`. The type refuses that text too: a
//    `datetime` of Python holds no year 0000, so no Python reader of the
//    platform takes such a text.
// To take one of the three costs one check of the reader and the rows of
// that text in the two test tables.
/// One instant in UTC, to the microsecond, in the years 0001 to 9999.
///
/// This is the time of a file and of a wire message. The contracts name the
/// form of its text as RFC 3339, for example contract 02 §13.2 rule 4 and
/// contract 05 §2.1.
///
/// The reader is [`FromStr`]. It takes the `date-time` of RFC 3339, section
/// 5.6, and no other text. The parts of a text are, in this sequence:
///
/// 1. The date, `YYYY-MM-DD`. The month has the day.
/// 2. `T` or `t`.
/// 3. The time of the day, `HH:MM:SS`: hour 00 to 23, minute 00 to 59 and
///    second 00 to 59.
/// 4. An optional fraction of the second: `.` and 1 to 9 digits. The type
///    keeps the first six digits and drops the others. It does not round.
/// 5. The UTC offset: `Z`, `z`, `+HH:MM` or `-HH:MM`, with hours 00 to 23 and
///    minutes 00 to 59. `-00:00` is UTC.
///
/// The value is the instant in UTC. The type does not keep the offset of the
/// text. The year is 0001 to 9999, in the text and in UTC.
///
/// The reader refuses each other text. Some examples: a space in place of
/// the `T`, a text with no offset, a date with no time, a week date, the
/// offset `+0530`, a comma before the fraction, a digit that is not ASCII, a
/// space at an end and a final newline. What a caller does with a refused
/// text is the rule of that caller.
///
/// The type has three writers and no `Display`, so each writer names its
/// form. It implements no `serde` trait. A raw type reads the text, and one
/// conversion calls the reader.
///
/// ```
/// use creche_contracts::time::Timestamp;
///
/// let written: Timestamp = "2031-04-18T08:42:35.25+02:00".parse()?;
/// assert_eq!(written.to_rfc3339(), "2031-04-18T06:42:35Z");
/// assert_eq!(written.to_rfc3339_millis(), "2031-04-18T06:42:35.250Z");
/// assert!("2031-04-18T06:42:35".parse::<Timestamp>().is_err());
/// assert!("2031-04-18 06:42:35Z".parse::<Timestamp>().is_err());
/// # Ok::<(), creche_contracts::time::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0451
/// use creche_contracts::time::Timestamp;
///
/// let written = Timestamp { micros: i64::MAX };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Timestamp {
    /// Microseconds from 1970-01-01T00:00:00Z.
    micros: i64,
}

impl Timestamp {
    /// The first instant of the type: `0001-01-01T00:00:00Z`.
    pub const MIN: Self = Self { micros: MICROS_MIN };

    /// The last instant of the type: `9999-12-31T23:59:59.999999Z`.
    pub const MAX: Self = Self { micros: MICROS_MAX };

    /// The instant that is `micros` microseconds after 1970-01-01T00:00:00Z.
    /// A negative count is an instant before that moment.
    ///
    /// # Errors
    ///
    /// [`TimestampError::OutOfRange`] for an instant before [`Self::MIN`] or
    /// after [`Self::MAX`].
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

    /// The instant with whole seconds: `YYYY-MM-DDTHH:MM:SSZ`. The writer
    /// drops the fraction of the second.
    #[must_use]
    pub fn to_rfc3339(self) -> String {
        format!("{}Z", self.civil().seconds())
    }

    /// The instant with milliseconds: `YYYY-MM-DDTHH:MM:SS.mmmZ`. The writer
    /// drops the digits after the third.
    #[must_use]
    pub fn to_rfc3339_millis(self) -> String {
        let civil = self.civil();

        format!("{}.{:03}Z", civil.seconds(), civil.millis())
    }

    /// The instant with milliseconds and the offset `+00:00` in place of `Z`:
    /// `YYYY-MM-DDTHH:MM:SS.mmm+00:00`.
    ///
    /// One log needs this form. The chaperone keeps a log of each request
    /// that names no family, and the lines of that log have this offset
    /// today. Use the writer for that log only. Delete the writer when that
    /// log writes `Z`.
    #[must_use]
    pub fn to_rfc3339_millis_plus_00_00(self) -> String {
        let civil = self.civil();

        format!("{}.{:03}+00:00", civil.seconds(), civil.millis())
    }

    /// The fields of the instant in UTC.
    fn civil(self) -> Civil {
        let of_day = self.micros.rem_euclid(MICROS_PER_DAY);
        let seconds = of_day / MICROS_PER_SECOND;

        Civil {
            date: Date::of_day(self.micros.div_euclid(MICROS_PER_DAY)),
            hour: seconds / SECONDS_PER_HOUR,
            minute: seconds % SECONDS_PER_HOUR / SECONDS_PER_MINUTE,
            second: seconds % SECONDS_PER_MINUTE,
            micros: of_day % MICROS_PER_SECOND,
        }
    }
}

/// The fields of one instant in UTC.
struct Civil {
    date: Date,
    hour: i64,
    minute: i64,
    second: i64,
    /// The microseconds of the second: 0 to 999999.
    micros: i64,
}

impl Civil {
    /// `YYYY-MM-DDTHH:MM:SS`, the start of each writer form.
    fn seconds(&self) -> String {
        let Self {
            date: Date { year, month, day },
            hour,
            minute,
            second,
            ..
        } = self;

        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}")
    }

    /// The whole milliseconds of the second: 0 to 999.
    fn millis(&self) -> i64 {
        self.micros / MICROS_PER_MILLI
    }
}

/// The numbers of a text in the form of the grammar. This is the raw form of
/// a time: no number has a bound yet.
struct Fields {
    year: i64,
    month: i64,
    day: i64,
    hour: i64,
    minute: i64,
    second: i64,
    /// The fraction of the second, in microseconds.
    micros: i64,
    offset: Offset,
}

/// The UTC offset of a text: its sign, then its two numbers.
struct Offset {
    side: Side,
    hours: i64,
    minutes: i64,
}

/// The side of UTC that an offset names.
enum Side {
    /// The sign `+`: the time of the text is later than UTC.
    Ahead,
    /// The sign `-`: the time of the text is earlier than UTC.
    Behind,
}

impl Offset {
    /// The offset of `Z` and of `z`.
    const UTC: Self = Self {
        side: Side::Ahead,
        hours: 0,
        minutes: 0,
    };

    /// The seconds that the time of the text is later than UTC. The count is
    /// negative for the sign `-`.
    fn seconds_ahead(&self) -> i64 {
        let seconds = self.hours * SECONDS_PER_HOUR + self.minutes * SECONDS_PER_MINUTE;

        match self.side {
            Side::Ahead => seconds,
            Side::Behind => -seconds,
        }
    }
}

impl Fields {
    /// Reads the form of a text. The only errors are
    /// [`TimestampError::Form`] and [`TimestampError::NoOffset`].
    fn read(text: &str) -> Result<Self, TimestampError> {
        let mut scanner = Scanner {
            rest: text.as_bytes(),
        };
        let year = scanner.number(YEAR_DIGITS)?;
        scanner.mark(b"-")?;
        let month = scanner.number(FIELD_DIGITS)?;
        scanner.mark(b"-")?;
        let day = scanner.number(FIELD_DIGITS)?;
        scanner.mark(b"Tt")?;
        let hour = scanner.number(FIELD_DIGITS)?;
        scanner.mark(b":")?;
        let minute = scanner.number(FIELD_DIGITS)?;
        scanner.mark(b":")?;
        let second = scanner.number(FIELD_DIGITS)?;
        let micros = scanner.fraction()?;
        let offset = scanner.offset()?;

        Ok(Self {
            year,
            month,
            day,
            hour,
            minute,
            second,
            micros,
            offset,
        })
    }

    /// The instant in UTC, when each number is in its bounds.
    fn instant(&self) -> Result<Timestamp, TimestampError> {
        let date = Date::new(self.year, self.month, self.day).ok_or(TimestampError::NotADate)?;
        if self.hour > HOUR_MAX || self.minute > MINUTE_MAX || self.second > SECOND_MAX {
            return Err(TimestampError::NotATimeOfDay);
        }

        if self.offset.hours > HOUR_MAX || self.offset.minutes > MINUTE_MAX {
            return Err(TimestampError::NotAnOffset);
        }

        if self.year < YEAR_MIN {
            return Err(TimestampError::OutOfRange);
        }

        let local = date.days_from_epoch() * SECONDS_PER_DAY
            + self.hour * SECONDS_PER_HOUR
            + self.minute * SECONDS_PER_MINUTE
            + self.second;
        let utc = local - self.offset.seconds_ahead();

        Timestamp::from_unix_micros(utc * MICROS_PER_SECOND + self.micros)
    }
}

/// The bytes of a text that the reader did not take yet.
struct Scanner<'a> {
    rest: &'a [u8],
}

impl Scanner<'_> {
    /// Takes `count` bytes. Each one must be an ASCII digit. Gives the number
    /// that the digits make.
    fn number(&mut self, count: usize) -> Result<i64, TimestampError> {
        let (digits, rest) = self
            .rest
            .split_at_checked(count)
            .ok_or(TimestampError::Form)?;
        let mut number = 0_i64;
        for digit in digits {
            if !digit.is_ascii_digit() {
                return Err(TimestampError::Form);
            }

            number = number * 10 + i64::from(digit - b'0');
        }
        self.rest = rest;

        Ok(number)
    }

    /// Takes one byte. It must be one of `marks`.
    fn mark(&mut self, marks: &[u8]) -> Result<(), TimestampError> {
        match self.rest.split_first() {
            Some((byte, rest)) if marks.contains(byte) => {
                self.rest = rest;

                Ok(())
            }
            _ => Err(TimestampError::Form),
        }
    }

    /// Takes the fraction of a second, when the next byte is `.`. Gives the
    /// fraction in microseconds. A text with no fraction gives zero.
    fn fraction(&mut self) -> Result<i64, TimestampError> {
        let Some((b'.', after)) = self.rest.split_first() else {
            return Ok(0);
        };
        let count = after
            .iter()
            .take_while(|byte| byte.is_ascii_digit())
            .count();
        if !(1..=FRACTION_DIGITS_MAX).contains(&count) {
            return Err(TimestampError::Form);
        }

        let (digits, rest) = after.split_at_checked(count).ok_or(TimestampError::Form)?;
        let mut micros = 0_i64;
        for place in 0..MICRO_DIGITS {
            let digit = digits.get(place).map_or(0, |digit| i64::from(digit - b'0'));
            micros = micros * 10 + digit;
        }
        self.rest = rest;

        Ok(micros)
    }

    /// Takes the UTC offset. It must be the end of the text.
    fn offset(mut self) -> Result<Offset, TimestampError> {
        let (side, after_sign) = match self.rest {
            [] => return Err(TimestampError::NoOffset),
            [b'Z' | b'z'] => return Ok(Offset::UTC),
            [b'+', after_sign @ ..] => (Side::Ahead, after_sign),
            [b'-', after_sign @ ..] => (Side::Behind, after_sign),
            _ => return Err(TimestampError::Form),
        };
        self.rest = after_sign;
        let hours = self.number(FIELD_DIGITS)?;
        self.mark(b":")?;
        let minutes = self.number(FIELD_DIGITS)?;
        if !self.rest.is_empty() {
            return Err(TimestampError::Form);
        }

        Ok(Offset {
            side,
            hours,
            minutes,
        })
    }
}

impl FromStr for Timestamp {
    type Err = TimestampError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Fields::read(text)?.instant()
    }
}

impl TryFrom<SystemTime> for Timestamp {
    type Error = TimestampError;

    /// The instant of a clock reading. A part of a microsecond is cut toward
    /// the earlier microsecond, also for a reading before 1970.
    ///
    /// # Errors
    ///
    /// [`TimestampError::OutOfRange`] for a reading before [`Timestamp::MIN`]
    /// or after [`Timestamp::MAX`].
    fn try_from(reading: SystemTime) -> Result<Self, Self::Error> {
        let nanos = match reading.duration_since(UNIX_EPOCH) {
            Ok(after) => i128::try_from(after.as_nanos()),
            Err(before) => i128::try_from(before.duration().as_nanos()).map(|nanos| -nanos),
        }
        .map_err(|_| TimestampError::OutOfRange)?;
        let micros = i64::try_from(nanos.div_euclid(NANOS_PER_MICRO))
            .map_err(|_| TimestampError::OutOfRange)?;

        Self::from_unix_micros(micros)
    }
}

/// Why a text or a number is not a [`Timestamp`].
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TimestampError {
    /// The text does not have the form of the grammar: the date, `T`, the
    /// time of the day, an optional fraction of 1 to 9 digits and an offset.
    Form,
    /// The text ends after the seconds or after the fraction. It names no UTC
    /// offset.
    NoOffset,
    /// The month is not 01 to 12, or the month does not have the day.
    NotADate,
    /// The hour is above 23, or the minute or the second is above 59.
    NotATimeOfDay,
    /// The hours of the offset are above 23, or its minutes are above 59.
    NotAnOffset,
    /// The year of the text is 0000, or the instant in UTC is before the
    /// year 0001 or after the year 9999.
    OutOfRange,
}

impl fmt::Display for TimestampError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Form => {
                "a time is YYYY-MM-DD, T, HH:MM:SS, an optional fraction of 1 to 9 digits \
                 and a UTC offset"
            }
            Self::NoOffset => "a time names its UTC offset: Z, or a sign with hours and minutes",
            Self::NotADate => "the date of a time has a month from 01 to 12 and a day of it",
            Self::NotATimeOfDay => "a time has an hour to 23, a minute to 59 and a second to 59",
            Self::NotAnOffset => "a UTC offset has hours to 23 and minutes to 59",
            Self::OutOfRange => "a time is in a year from 0001 to 9999, in its text and in UTC",
        })
    }
}

impl Error for TimestampError {}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;
    use std::time::Duration;

    use super::*;

    fn time(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    /// Each text that the reader takes, with its count of microseconds from
    /// 1970-01-01T00:00:00Z.
    const ACCEPTED: &[(&str, i64)] = &[
        // The texts of `TIMES` in `vectors/surfaces/session_cases.py` that
        // are in the grammar, in the sequence of that list.
        ("2026-10-06T06:00:00Z", 1_791_266_400_000_000),
        ("2026-10-06T06:00:00+00:00", 1_791_266_400_000_000),
        ("2026-10-06T06:00:00-00:00", 1_791_266_400_000_000),
        ("2026-10-06T06:00:00+05:30", 1_791_246_600_000_000),
        ("2026-10-06T06:00:00-05:30", 1_791_286_200_000_000),
        ("2026-10-06T06:00:00+23:59", 1_791_180_060_000_000),
        ("2026-10-06T06:00:00-23:59", 1_791_352_740_000_000),
        ("2026-12-31T23:30:00-01:00", 1_798_763_400_000_000),
        ("2024-02-29T23:30:00-01:00", 1_709_253_000_000_000),
        ("2026-10-06t06:00:00Z", 1_791_266_400_000_000),
        ("2026-10-06T06:00:00.1Z", 1_791_266_400_100_000),
        ("2026-10-06T06:00:00.123Z", 1_791_266_400_123_000),
        ("2026-10-06T06:00:00.123456Z", 1_791_266_400_123_456),
        ("2026-10-06T06:00:00.1234567Z", 1_791_266_400_123_456),
        ("2026-10-06T06:00:00.1234+05:30", 1_791_246_600_123_400),
        ("2026-10-06T06:00:00z", 1_791_266_400_000_000),
        ("2024-02-29T00:00:00Z", 1_709_164_800_000_000),
        ("0001-01-01T00:00:00Z", -62_135_596_800_000_000),
        ("9999-12-31T23:59:59.999999Z", 253_402_300_799_999_999),
        ("1970-01-01T00:00:00Z", 0),
        ("1969-12-31T23:59:59.999999Z", -1),
        ("0001-01-01T00:01:00+00:01", -62_135_596_800_000_000),
        // Midnight UTC in three forms.
        ("2999-01-01T00:00:00Z", 32_472_144_000_000_000),
        ("2999-01-01T00:00:00z", 32_472_144_000_000_000),
        ("2999-01-01T02:00:00+02:00", 32_472_144_000_000_000),
        ("2026-10-06t06:00:00z", 1_791_266_400_000_000),
        // A fraction of 9 digits. The reader drops the digits after the
        // sixth, and it does not round.
        ("2026-10-06T06:00:00.123456789Z", 1_791_266_400_123_456),
        ("9999-12-31T23:59:59.999999999Z", 253_402_300_799_999_999),
        ("1969-12-31T23:59:59.9999999Z", -1),
        ("2026-10-06T06:00:00.5+05:30", 1_791_246_600_500_000),
        ("2026-10-06T06:00:00.5-05:30", 1_791_286_200_500_000),
        // The first and the last instant, each through the largest offset.
        ("0001-01-01T00:00:00.000000+00:00", -62_135_596_800_000_000),
        ("0001-01-01T23:59:00+23:59", -62_135_596_800_000_000),
        ("9999-12-31T00:00:59.999999-23:59", 253_402_300_799_999_999),
        // The two sides of 1970.
        ("1969-12-31T23:59:59Z", -1_000_000),
        ("1970-01-01T00:00:00.000001Z", 1),
        ("1970-01-01T01:00:00+01:00", 0),
        // A leap day of a year that 400 divides, and the last second of a day.
        ("2000-02-29T00:00:00Z", 951_782_400_000_000),
        ("2400-02-29T23:59:59Z", 13_574_649_599_000_000),
        ("2026-10-06T23:59:59Z", 1_791_331_199_000_000),
    ];

    /// Each text that the reader refuses, with the rule that it breaks.
    const REFUSED: &[(&str, TimestampError)] = &[
        // The texts of `TIMES` in `vectors/surfaces/session_cases.py` that
        // are not in the grammar, in the sequence of that list.
        ("2026-10-06T06:00:00+0530", TimestampError::Form),
        ("2026-10-06T06:00:00+05", TimestampError::Form),
        ("2026-10-06T06:00:00+05:30:15", TimestampError::Form),
        ("2026-10-06T06:00:00+05:30:15.5", TimestampError::Form),
        ("2026-10-06T06:00:00+24:00", TimestampError::NotAnOffset),
        ("2026-10-06T06:00:00+00:60", TimestampError::NotAnOffset),
        ("2026-10-06T06:00:00+5", TimestampError::Form),
        ("2026-10-06T06:00:00+", TimestampError::Form),
        ("2026-10-06T06:00:00+00:00Z", TimestampError::Form),
        ("2026-10-06T06:00:00", TimestampError::NoOffset),
        ("2026-10-06", TimestampError::Form),
        ("2026-10-06Z", TimestampError::Form),
        ("2026-10-06+06:00", TimestampError::Form),
        ("2026-10-06T06", TimestampError::Form),
        ("2026-10-06T06:00", TimestampError::Form),
        ("2026-10-06T06:00Z", TimestampError::Form),
        ("2026-10-06 06:00:00Z", TimestampError::Form),
        ("2026-10-06\u{e9}06:00:00Z", TimestampError::Form),
        ("2026-10-06\u{1f600}06:00:00Z", TimestampError::Form),
        ("2026-10-06TT06:00:00Z", TimestampError::Form),
        ("2026-10-0606:00:00Z", TimestampError::Form),
        ("20261006", TimestampError::Form),
        ("20261006T060000Z", TimestampError::Form),
        ("2026-10-06T060000Z", TimestampError::Form),
        ("20261006T06:00:00Z", TimestampError::Form),
        ("2026-10-06T0600+05", TimestampError::Form),
        ("2026-10-06T06:0000Z", TimestampError::Form),
        ("2026-W38-6", TimestampError::Form),
        ("2026-W38-6T06:00:00Z", TimestampError::Form),
        ("2026W386", TimestampError::Form),
        (
            "2026-10-06T06:00:00.12345678901234567890Z",
            TimestampError::Form,
        ),
        ("2026-10-06T06:00:00,5Z", TimestampError::Form),
        ("2026-10-06T06:00:00.5.5Z", TimestampError::Form),
        ("2026-10-06T06:00:00.\u{661}Z", TimestampError::Form),
        ("2026-10-06T06:00:00 Z", TimestampError::Form),
        ("2026-10-06T06:00:009Z", TimestampError::Form),
        ("2026-10-06T06:00:+05:30", TimestampError::Form),
        ("2026-10-06T06:00:00.123456abc+05:30", TimestampError::Form),
        ("2026-10-06T06000530", TimestampError::Form),
        ("2026-10-06T06:00:00ZZ", TimestampError::Form),
        ("2026-10-06T06:00:00UTC", TimestampError::Form),
        (" 2026-10-06T06:00:00Z", TimestampError::Form),
        ("2026-10-06T06:00:00Z ", TimestampError::Form),
        ("2026-10-06T06:00:00Z\n", TimestampError::Form),
        ("2026-9-19T06:00:00Z", TimestampError::Form),
        ("2026-10-06T6:00:00Z", TimestampError::Form),
        ("26-09-19T06:00:00Z", TimestampError::Form),
        (
            "\u{662}\u{660}\u{662}\u{666}-09-19T06:00:00Z",
            TimestampError::Form,
        ),
        ("2026-02-29T00:00:00Z", TimestampError::NotADate),
        ("2026-13-01T00:00:00Z", TimestampError::NotADate),
        ("2026-09-31T00:00:00Z", TimestampError::NotADate),
        ("2026-09-00T00:00:00Z", TimestampError::NotADate),
        ("2026-10-06T25:00:00Z", TimestampError::NotATimeOfDay),
        ("2026-10-06T23:60:00Z", TimestampError::NotATimeOfDay),
        ("2026-10-06T23:59:60Z", TimestampError::NotATimeOfDay),
        ("0000-01-01T00:00:00Z", TimestampError::OutOfRange),
        ("10000-01-01T00:00:00Z", TimestampError::Form),
        ("Z", TimestampError::Form),
        ("last Tuesday", TimestampError::Form),
        ("1789797600", TimestampError::Form),
        ("2026-10-06 06:00:00 +0200", TimestampError::Form),
        ("2026-10-06T06:00:00  Z", TimestampError::Form),
        ("2026-10-06T06:00:00 ", TimestampError::Form),
        ("2026-10-06T06:00:00.12 Z", TimestampError::Form),
        ("2026-10-06T06:00:00Z\0abc", TimestampError::Form),
        ("2026-10-06T0600003", TimestampError::Form),
        ("2026-10-06T060005301234.5+00:00", TimestampError::Form),
        ("2026-W38", TimestampError::Form),
        ("2026W38", TimestampError::Form),
        ("2026-W38-06:00", TimestampError::Form),
        ("2026W386060000", TimestampError::Form),
        ("2026-W53-1", TimestampError::Form),
        ("2025-W53-1", TimestampError::Form),
        ("2026-W38-8", TimestampError::Form),
        ("9999-W52-6", TimestampError::Form),
        ("0001-01-01T00:00:00+00:01", TimestampError::OutOfRange),
        ("9999-12-31T23:59:59-00:01", TimestampError::OutOfRange),
        // A space for the `T`, no offset and hour 24.
        ("2999-01-01 00:00:00Z", TimestampError::Form),
        ("2999-01-01T00:00:00", TimestampError::NoOffset),
        ("2026-10-06T06:00:00.5", TimestampError::NoOffset),
        ("2999-01-01T24:00:00Z", TimestampError::NotATimeOfDay),
        ("2026-10-06T24:00:00Z", TimestampError::NotATimeOfDay),
        ("2026-10-06T06:00:61Z", TimestampError::NotATimeOfDay),
        // A fraction of no digit and of 10 digits, and a number in it.
        ("2026-10-06T06:00:00.Z", TimestampError::Form),
        ("2026-10-06T06:00:00.1234567890Z", TimestampError::Form),
        ("2026-10-06T06:00:00.-5Z", TimestampError::Form),
        ("2026-10-06T06:00:00.+5Z", TimestampError::Form),
        ("2026-10-06T06:00:00.5e1Z", TimestampError::Form),
        // The year 0000, also when the offset moves the instant to 0001.
        ("0000-12-31T23:30:00-01:00", TimestampError::OutOfRange),
        ("0000-02-29T00:00:00Z", TimestampError::OutOfRange),
        ("0000-12-31T23:59:59.999999Z", TimestampError::OutOfRange),
        // One microsecond before the first instant, and one second after
        // the last second, each through the largest offset.
        (
            "0001-01-01T23:58:59.999999+23:59",
            TimestampError::OutOfRange,
        ),
        ("9999-12-31T00:01:00-23:59", TimestampError::OutOfRange),
        // An offset outside the form or outside its bounds.
        ("2026-10-06T06:00:00+05:3", TimestampError::Form),
        ("2026-10-06T06:00:00+05:300", TimestampError::Form),
        ("2026-10-06T06:00:00+5:30", TimestampError::Form),
        ("2026-10-06T06:00:00+-5:30", TimestampError::Form),
        ("2026-10-06T06:00:00+05:-3", TimestampError::Form),
        ("2026-10-06T06:00:00+23:60", TimestampError::NotAnOffset),
        ("2026-10-06T06:00:00-24:00", TimestampError::NotAnOffset),
        // A sign that is not ASCII, a digit that is not ASCII and a letter
        // that is not ASCII.
        ("2026-10-06T06:00:00\u{2212}05:30", TimestampError::Form),
        (
            "2026-10-06T06:00:00+\u{660}\u{665}:30",
            TimestampError::Form,
        ),
        ("2026-10-06T06:00:0\u{660}Z", TimestampError::Form),
        ("2026-10-06T06:00:00\u{ff3a}", TimestampError::Form),
        ("2026\u{2010}10-06T06:00:00Z", TimestampError::Form),
        // White space at an end, a sign at the start and no text.
        ("2026-10-06T06:00:00Z\r\n", TimestampError::Form),
        ("\n2026-10-06T06:00:00Z", TimestampError::Form),
        ("2026-10-06T06:00:00Z\t", TimestampError::Form),
        ("+2026-10-06T06:00:00Z", TimestampError::Form),
        ("-2026-10-06T06:00:00Z", TimestampError::Form),
        ("2026-10-06T-6:00:00Z", TimestampError::Form),
        ("", TimestampError::Form),
        // A date that the calendar does not have.
        ("1900-02-29T00:00:00Z", TimestampError::NotADate),
        ("2100-02-29T00:00:00Z", TimestampError::NotADate),
        ("2026-00-06T06:00:00Z", TimestampError::NotADate),
        ("2026-10-32T06:00:00Z", TimestampError::NotADate),
        ("2026-04-31T06:00:00Z", TimestampError::NotADate),
    ];

    /// A text of an instant, and what `to_rfc3339` writes for it.
    const WHOLE_SECONDS: &[(&str, &str)] = &[
        ("2031-04-18T06:42:35Z", "2031-04-18T06:42:35Z"),
        ("2031-04-18t08:42:35+02:00", "2031-04-18T06:42:35Z"),
        ("2031-04-18T06:42:35.999999Z", "2031-04-18T06:42:35Z"),
        ("2026-10-01T15:50:49.637z", "2026-10-01T15:50:49Z"),
        ("2024-02-29T23:59:59Z", "2024-02-29T23:59:59Z"),
        ("2024-02-29T23:30:00-01:00", "2024-03-01T00:30:00Z"),
        ("2026-12-31T23:30:00-01:00", "2027-01-01T00:30:00Z"),
        ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"),
        ("1969-12-31T23:59:59.5Z", "1969-12-31T23:59:59Z"),
        ("0999-12-31T23:59:59.999999Z", "0999-12-31T23:59:59Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
        ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59Z"),
    ];

    /// A text of an instant, and what `to_rfc3339_millis` writes for it.
    const MILLIS: &[(&str, &str)] = &[
        ("2031-04-18T06:42:35Z", "2031-04-18T06:42:35.000Z"),
        ("2031-04-18t08:42:35.25+02:00", "2031-04-18T06:42:35.250Z"),
        ("2031-04-18T06:42:35.123456Z", "2031-04-18T06:42:35.123Z"),
        ("2031-04-18T06:42:35.999999Z", "2031-04-18T06:42:35.999Z"),
        ("2031-04-18T06:42:35.000999Z", "2031-04-18T06:42:35.000Z"),
        ("2031-04-18T06:42:35.001Z", "2031-04-18T06:42:35.001Z"),
        ("2026-10-01T15:50:49.637z", "2026-10-01T15:50:49.637Z"),
        ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00.000Z"),
        ("1969-12-31T23:59:59.999999Z", "1969-12-31T23:59:59.999Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000Z"),
        ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999Z"),
    ];

    /// A text of an instant, and what `to_rfc3339_millis_plus_00_00` writes
    /// for it.
    const MILLIS_PLUS_00_00: &[(&str, &str)] = &[
        ("2026-09-18T19:41:07.412Z", "2026-09-18T19:41:07.412+00:00"),
        (
            "2026-09-18T21:41:07.412999+02:00",
            "2026-09-18T19:41:07.412+00:00",
        ),
        ("2031-04-18T06:42:35z", "2031-04-18T06:42:35.000+00:00"),
        ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00.000+00:00"),
        (
            "1969-12-31T23:59:59.999999Z",
            "1969-12-31T23:59:59.999+00:00",
        ),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000+00:00"),
        (
            "9999-12-31T23:59:59.999999Z",
            "9999-12-31T23:59:59.999+00:00",
        ),
    ];

    #[test]
    fn a_text_in_the_grammar_is_the_instant_in_utc() {
        for (text, micros) in ACCEPTED {
            let read = text.parse::<Timestamp>();

            assert_eq!(read.map(Timestamp::unix_micros), Ok(*micros), "{text:?}");
        }
    }

    #[test]
    fn a_text_outside_the_grammar_is_refused() {
        for (text, error) in REFUSED {
            assert_eq!(text.parse::<Timestamp>(), Err(*error), "{text:?}");
        }
    }

    #[test]
    fn no_text_is_in_a_table_two_times() {
        let texts = ACCEPTED
            .iter()
            .map(|(text, _)| *text)
            .chain(REFUSED.iter().map(|(text, _)| *text));
        let mut seen = HashSet::new();

        for text in texts {
            assert!(seen.insert(text), "{text:?}");
        }
    }

    #[test]
    fn a_writer_writes_whole_seconds_and_z() {
        for (text, written) in WHOLE_SECONDS {
            assert_eq!(time(text).to_rfc3339(), *written, "{text}");
        }
    }

    #[test]
    fn a_writer_writes_milliseconds_and_z() {
        for (text, written) in MILLIS {
            assert_eq!(time(text).to_rfc3339_millis(), *written, "{text}");
        }
    }

    #[test]
    fn one_writer_writes_milliseconds_and_the_offset_of_one_log() {
        for (text, written) in MILLIS_PLUS_00_00 {
            assert_eq!(
                time(text).to_rfc3339_millis_plus_00_00(),
                *written,
                "{text}"
            );
        }
    }

    #[test]
    fn what_a_writer_writes_reads_back() {
        for (text, micros) in ACCEPTED {
            let instant = time(text);
            let whole_seconds = micros.div_euclid(MICROS_PER_SECOND) * MICROS_PER_SECOND;
            let whole_millis = micros.div_euclid(MICROS_PER_MILLI) * MICROS_PER_MILLI;

            assert_eq!(
                time(&instant.to_rfc3339()).unix_micros(),
                whole_seconds,
                "{text}"
            );
            assert_eq!(
                time(&instant.to_rfc3339_millis()).unix_micros(),
                whole_millis,
                "{text}"
            );
            assert_eq!(
                time(&instant.to_rfc3339_millis_plus_00_00()).unix_micros(),
                whole_millis,
                "{text}"
            );
        }
    }

    #[test]
    fn the_range_is_the_years_0001_to_9999() {
        assert_eq!(Timestamp::MIN, time("0001-01-01T00:00:00Z"));
        assert_eq!(Timestamp::MAX, time("9999-12-31T23:59:59.999999Z"));
        assert_eq!(Timestamp::MIN.unix_micros(), -62_135_596_800_000_000);
        assert_eq!(Timestamp::MAX.unix_micros(), 253_402_300_799_999_999);
        assert_eq!(
            Timestamp::MIN.to_rfc3339_millis(),
            "0001-01-01T00:00:00.000Z"
        );
        assert_eq!(
            Timestamp::MAX.to_rfc3339_millis(),
            "9999-12-31T23:59:59.999Z"
        );
    }

    #[test]
    fn a_raw_number_outside_the_range_is_refused() {
        let first = Timestamp::MIN.unix_micros();
        let last = Timestamp::MAX.unix_micros();
        let inside = [first, first + 1, -1, 0, 1, last - 1, last];
        let outside = [i64::MIN, first - 1, last + 1, i64::MAX];

        for micros in inside {
            assert_eq!(
                Timestamp::from_unix_micros(micros).map(Timestamp::unix_micros),
                Ok(micros)
            );
        }

        for micros in outside {
            assert_eq!(
                Timestamp::from_unix_micros(micros),
                Err(TimestampError::OutOfRange),
                "{micros}"
            );
        }
    }

    #[test]
    fn a_clock_reading_is_cut_to_the_earlier_microsecond() {
        let readings = [
            (UNIX_EPOCH, 0),
            (UNIX_EPOCH + Duration::from_micros(1_500_000), 1_500_000),
            (UNIX_EPOCH + Duration::from_nanos(999), 0),
            (UNIX_EPOCH + Duration::from_nanos(1_000), 1),
            (UNIX_EPOCH - Duration::from_nanos(1), -1),
            (UNIX_EPOCH - Duration::from_nanos(1_000), -1),
            (UNIX_EPOCH - Duration::from_nanos(1_001), -2),
            (UNIX_EPOCH - Duration::from_secs(1), -1_000_000),
        ];

        for (reading, micros) in readings {
            assert_eq!(
                Timestamp::try_from(reading).map(Timestamp::unix_micros),
                Ok(micros),
                "{reading:?}"
            );
        }
    }

    #[test]
    fn a_clock_reading_has_the_text_of_its_instant() {
        let reading = UNIX_EPOCH + Duration::new(1_934_260_990, 123_456_789);
        let instant = Timestamp::try_from(reading).unwrap();

        assert_eq!(instant.to_rfc3339(), "2031-04-18T06:43:10Z");
        assert_eq!(instant.to_rfc3339_millis(), "2031-04-18T06:43:10.123Z");
        assert_eq!(instant.unix_micros(), 1_934_260_990_123_456);
        assert!(Timestamp::try_from(SystemTime::now()).is_ok());
    }

    #[test]
    fn a_clock_reading_outside_the_range_is_refused() {
        let first = UNIX_EPOCH - Duration::from_secs(62_135_596_800);
        let last = UNIX_EPOCH + Duration::new(253_402_300_799, 999_999_999);

        assert_eq!(Timestamp::try_from(first), Ok(Timestamp::MIN));
        assert_eq!(Timestamp::try_from(last), Ok(Timestamp::MAX));
        assert_eq!(
            Timestamp::try_from(first - Duration::from_nanos(1)),
            Err(TimestampError::OutOfRange)
        );
        assert_eq!(
            Timestamp::try_from(last + Duration::from_nanos(1)),
            Err(TimestampError::OutOfRange)
        );
    }

    #[test]
    fn two_texts_of_one_instant_are_equal_and_the_order_is_the_order_of_time() {
        let midnight = time("2999-01-01T00:00:00Z");
        let mut hashes = HashSet::new();

        assert_eq!(time("2999-01-01T02:00:00+02:00"), midnight);
        assert_eq!(time("2998-12-31t19:00:00.000-05:00"), midnight);
        assert!(hashes.insert(midnight));
        assert!(!hashes.insert(time("2999-01-01T02:00:00+02:00")));
        assert!(Timestamp::MIN < time("1969-12-31T23:59:59.999999Z"));
        assert!(time("1969-12-31T23:59:59.999999Z") < time("1970-01-01T00:00:00Z"));
        assert!(time("2999-01-01T00:00:00+00:01") < midnight);
        assert!(midnight < time("2999-01-01T00:00:00.000001Z"));
        assert!(midnight < Timestamp::MAX);
    }

    /// The day after `date`, by the rules of the calendar and with no day
    /// count.
    fn next_day(date: Date) -> Date {
        if date.day < days_in_month(date.year, date.month) {
            return Date {
                day: date.day + 1,
                ..date
            };
        }

        if date.month < MONTH_MAX {
            return Date {
                month: date.month + 1,
                day: 1,
                ..date
            };
        }

        Date {
            year: date.year + 1,
            month: 1,
            day: 1,
        }
    }

    #[test]
    fn each_day_of_the_range_has_its_date_and_its_count() {
        let mut date = Date {
            year: 0,
            month: 1,
            day: 1,
        };
        let mut days = date.days_from_epoch();
        let mut leap_days = 0;

        while date.year <= YEAR_MAX {
            assert_eq!(date.days_from_epoch(), days, "{date:?}");
            assert_eq!(Date::of_day(days), date, "{days}");
            assert_eq!(
                Date::new(date.year, date.month, date.day),
                Some(date),
                "{date:?}"
            );
            if date.month == 2 && date.day == 29 {
                leap_days += 1;
            }

            date = next_day(date);
            days += 1;
        }

        // 10,000 years are 25 cycles of 97 leap days.
        assert_eq!(leap_days, 2_425);
        assert_eq!(
            days,
            Date::LAST.days_from_epoch() + 1,
            "the day after the last day"
        );
    }

    #[test]
    fn the_day_count_starts_at_1970() {
        let epoch = Date {
            year: 1970,
            month: 1,
            day: 1,
        };

        assert_eq!(epoch.days_from_epoch(), 0);
        assert_eq!(Date::of_day(0), epoch);
        assert_eq!(Date::FIRST.days_from_epoch(), -719_162);
        assert_eq!(Date::LAST.days_from_epoch(), 2_932_896);
        assert_eq!(Date::of_day(-719_162), Date::FIRST);
        assert_eq!(Date::of_day(2_932_896), Date::LAST);
    }

    #[test]
    fn each_midnight_of_400_years_reads_back() {
        let first = Date {
            year: 1999,
            month: 12,
            day: 31,
        };
        let mut date = first;

        for days in first.days_from_epoch()..first.days_from_epoch() + DAYS_PER_CYCLE + 2 {
            let Date { year, month, day } = date;
            let text = format!("{year:04}-{month:02}-{day:02}T00:00:00Z");
            let read = time(&text);

            assert_eq!(read.unix_micros(), days * MICROS_PER_DAY, "{text}");
            assert_eq!(read.to_rfc3339(), text);
            date = next_day(date);
        }
    }

    #[test]
    fn the_error_says_which_rule_failed() {
        let messages = [
            (
                TimestampError::Form,
                "a time is YYYY-MM-DD, T, HH:MM:SS, an optional fraction of 1 to 9 digits and a \
                 UTC offset",
            ),
            (
                TimestampError::NoOffset,
                "a time names its UTC offset: Z, or a sign with hours and minutes",
            ),
            (
                TimestampError::NotADate,
                "the date of a time has a month from 01 to 12 and a day of it",
            ),
            (
                TimestampError::NotATimeOfDay,
                "a time has an hour to 23, a minute to 59 and a second to 59",
            ),
            (
                TimestampError::NotAnOffset,
                "a UTC offset has hours to 23 and minutes to 59",
            ),
            (
                TimestampError::OutOfRange,
                "a time is in a year from 0001 to 9999, in its text and in UTC",
            ),
        ];

        for (error, message) in messages {
            let boxed: Box<dyn Error> = Box::new(error);

            assert_eq!(boxed.to_string(), message);
        }
    }
}
