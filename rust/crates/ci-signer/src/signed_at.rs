//! The time of a statement, in whole seconds of UTC. The text form is
//! RFC 3339 section 5.6 with `Z` and no fraction, `YYYY-MM-DDTHH:MM:SSZ`
//! (root `AGENTS.md`, "Time"). The statement is no message of the wire
//! types, so the time is a date string, not `UnixMillis`.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

/// The seconds of one day.
const DAY_SECONDS: u64 = 86_400;

/// The first and the last year of a `SignedAt`.
const FIRST_YEAR: u64 = 1970;
const LAST_YEAR: u64 = 9999;

/// The days from 0000-03-01 to 1970-01-01 in the proleptic Gregorian
/// calendar.
const EPOCH_DAYS: u64 = 719_468;

/// The days of one 400-year era.
const ERA_DAYS: u64 = 146_097;

/// A time of a statement: whole seconds since 1970-01-01T00:00:00Z, in the
/// years 1970 to 9999.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(
    test,
    schemars(
        with = "String",
        extend("pattern" = "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
    )
)]
pub struct SignedAt(u64);

/// Why a value is no `SignedAt`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeError {
    /// The time is past 9999-12-31T23:59:59Z.
    OutOfRange,
    /// The text is not `YYYY-MM-DDTHH:MM:SSZ`.
    Format,
    /// The date or the time of day does not exist.
    NoSuchTime,
}

impl fmt::Display for TimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::OutOfRange => "the time is outside the years 1970 to 9999",
            Self::Format => "the time is not YYYY-MM-DDTHH:MM:SSZ",
            Self::NoSuchTime => "the date or the time of day does not exist",
        })
    }
}

impl std::error::Error for TimeError {}

/// A date of the proleptic Gregorian calendar.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Civil {
    year: u64,
    month: u64,
    day: u64,
}

/// The days since 1970-01-01 of a date in or after 1970 (the algorithm
/// `days_from_civil` of H. Hinnant, for years that are not negative).
fn days_from_civil(date: Civil) -> u64 {
    let year = if date.month <= 2 {
        date.year - 1
    } else {
        date.year
    };
    let era = year / 400;
    let year_of_era = year - era * 400;
    let month_index = if date.month > 2 {
        date.month - 3
    } else {
        date.month + 9
    };
    let day_of_year = (153 * month_index + 2) / 5 + date.day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * ERA_DAYS + day_of_era - EPOCH_DAYS
}

/// The date of a day count since 1970-01-01 (`civil_from_days` of
/// H. Hinnant).
fn civil_from_days(days: u64) -> Civil {
    let days = days + EPOCH_DAYS;
    let era = days / ERA_DAYS;
    let day_of_era = days - era * ERA_DAYS;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / (ERA_DAYS - 1)) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + u64::from(month <= 2);
    Civil { year, month, day }
}

/// The days of a month.
fn month_days(year: u64, month: u64) -> u64 {
    let leap = year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// The first second after 9999-12-31T23:59:59Z.
fn end_seconds() -> u64 {
    days_from_civil(Civil {
        year: LAST_YEAR + 1,
        month: 1,
        day: 1,
    }) * DAY_SECONDS
}

impl SignedAt {
    /// The time `seconds` after 1970-01-01T00:00:00Z.
    ///
    /// # Errors
    ///
    /// `TimeError::OutOfRange` for a time past the year 9999.
    pub fn from_unix_seconds(seconds: u64) -> Result<Self, TimeError> {
        if seconds >= end_seconds() {
            return Err(TimeError::OutOfRange);
        }
        Ok(Self(seconds))
    }

    /// The seconds since 1970-01-01T00:00:00Z.
    #[must_use]
    pub fn unix_seconds(self) -> u64 {
        self.0
    }
}

/// The number of a run of ASCII digits.
fn digits(bytes: &[u8]) -> Option<u64> {
    bytes.iter().try_fold(0_u64, |total, byte| {
        byte.is_ascii_digit()
            .then(|| total * 10 + u64::from(byte - b'0'))
    })
}

impl FromStr for SignedAt {
    type Err = TimeError;

    fn from_str(text: &str) -> Result<Self, TimeError> {
        let bytes = text.as_bytes();
        let shape = bytes.len() == 20
            && bytes.get(4) == Some(&b'-')
            && bytes.get(7) == Some(&b'-')
            && bytes.get(10) == Some(&b'T')
            && bytes.get(13) == Some(&b':')
            && bytes.get(16) == Some(&b':')
            && bytes.get(19) == Some(&b'Z');
        if !shape {
            return Err(TimeError::Format);
        }

        let field = |from: usize, to: usize| {
            bytes
                .get(from..to)
                .and_then(digits)
                .ok_or(TimeError::Format)
        };
        let date = Civil {
            year: field(0, 4)?,
            month: field(5, 7)?,
            day: field(8, 10)?,
        };
        let (hour, minute, second) = (field(11, 13)?, field(14, 16)?, field(17, 19)?);

        if date.year < FIRST_YEAR || !(1..=12).contains(&date.month) {
            return Err(TimeError::NoSuchTime);
        }
        if !(1..=month_days(date.year, date.month)).contains(&date.day) {
            return Err(TimeError::NoSuchTime);
        }
        if hour > 23 || minute > 59 || second > 59 {
            return Err(TimeError::NoSuchTime);
        }

        let seconds = days_from_civil(date) * DAY_SECONDS + hour * 3600 + minute * 60 + second;
        Ok(Self(seconds))
    }
}

impl fmt::Display for SignedAt {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let date = civil_from_days(self.0 / DAY_SECONDS);
        let rest = self.0 % DAY_SECONDS;
        write!(
            formatter,
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
            date.year,
            date.month,
            date.day,
            rest / 3600,
            rest % 3600 / 60,
            rest % 60
        )
    }
}

impl TryFrom<String> for SignedAt {
    type Error = TimeError;

    fn try_from(text: String) -> Result<Self, TimeError> {
        text.parse()
    }
}

impl From<SignedAt> for String {
    fn from(value: SignedAt) -> String {
        value.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_times() {
        assert_eq!(
            SignedAt::from_unix_seconds(0).unwrap().to_string(),
            "1970-01-01T00:00:00Z"
        );

        let note: SignedAt = "2026-11-15T12:00:00Z".parse().unwrap();
        assert_eq!(note.unix_seconds(), 1_794_744_000);
        assert_eq!(note.to_string(), "2026-11-15T12:00:00Z");

        let leap: SignedAt = "2028-02-29T23:59:59Z".parse().unwrap();
        assert_eq!(leap.to_string(), "2028-02-29T23:59:59Z");
        assert_eq!(
            "2000-02-29T00:00:00Z"
                .parse::<SignedAt>()
                .unwrap()
                .to_string(),
            "2000-02-29T00:00:00Z"
        );

        let last: SignedAt = "9999-12-31T23:59:59Z".parse().unwrap();
        assert!(SignedAt::from_unix_seconds(last.unix_seconds()).is_ok());
        assert_eq!(
            SignedAt::from_unix_seconds(last.unix_seconds() + 1),
            Err(TimeError::OutOfRange)
        );
    }

    #[test]
    fn every_day_round_trips() {
        // Each day from 1970 to the end of 2100: display, then parse.
        for day in 0..47_847_u64 {
            let at = SignedAt::from_unix_seconds(day * DAY_SECONDS + 3661).unwrap();
            assert_eq!(at.to_string().parse::<SignedAt>().unwrap(), at);
        }
    }

    #[test]
    fn refusals() {
        for (text, error) in [
            ("2026-02-30T00:00:00Z", TimeError::NoSuchTime),
            ("2100-02-29T00:00:00Z", TimeError::NoSuchTime),
            ("2026-13-01T00:00:00Z", TimeError::NoSuchTime),
            ("2026-11-15T24:00:00Z", TimeError::NoSuchTime),
            ("2026-11-15T23:59:60Z", TimeError::NoSuchTime),
            ("1969-12-31T23:59:59Z", TimeError::NoSuchTime),
            ("2026-11-15T12:00:00.5Z", TimeError::Format),
            ("2026-11-15T12:00:00+00:00", TimeError::Format),
            ("2026-11-15T12:00:00z", TimeError::Format),
            ("2026-11-15t12:00:00Z", TimeError::Format),
            ("2026-11-15T12:00:0aZ", TimeError::Format),
            ("+026-11-15T12:00:00Z", TimeError::Format),
        ] {
            assert_eq!(text.parse::<SignedAt>(), Err(error), "{text}");
        }
    }
}
