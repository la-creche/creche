//! RFC 3339 times of the vmm: UTC with `Z`, to the second (root
//! `AGENTS.md`, Time). The fault file of decision CH21-1 writes them, and
//! the family state read takes `model_live_at` of `config/runtime.json` in
//! the same form (decision CH23-1). A small civil-date formatter writes
//! them and its inverse reads them, so the crate needs no date crate.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Writes `time` as RFC 3339 in UTC, to the second. A time before 1970
/// writes as 1970-01-01T00:00:00Z: the host clock is never there.
#[must_use]
pub fn format(time: SystemTime) -> String {
    let secs = time
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let (days, rest) = (secs / 86_400, secs % 86_400);
    // The civil date of a day count (Howard Hinnant, `civil_from_days`).
    let shifted = days + 719_468;
    let era = shifted / 146_097;
    let day_of_era = shifted % 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = era * 400 + year_of_era + u64::from(month <= 2);
    let (hour, minute, second) = (rest / 3600, rest % 3600 / 60, rest % 60);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

/// Reads `text` in the one form that `format` writes:
/// `YYYY-MM-DDTHH:MM:SSZ`, the form of caregiver's clock (`clock.py`
/// `RFC3339_FORMAT`). Any other text gives `None`: an offset, a fraction,
/// a day that its month lacks, or a time before 1970.
#[must_use]
pub fn parse(text: &str) -> Option<SystemTime> {
    let bytes: [u8; 20] = text.as_bytes().try_into().ok()?;
    let [
        y0,
        y1,
        y2,
        y3,
        b'-',
        m0,
        m1,
        b'-',
        d0,
        d1,
        b'T',
        h0,
        h1,
        b':',
        n0,
        n1,
        b':',
        s0,
        s1,
        b'Z',
    ] = bytes
    else {
        return None;
    };
    let year = digits(&[y0, y1, y2, y3])?;
    let month = digits(&[m0, m1])?;
    let day = digits(&[d0, d1])?;
    let (hour, minute, second) = (digits(&[h0, h1])?, digits(&[n0, n1])?, digits(&[s0, s1])?);
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) || hour > 23 || minute > 59 {
        return None;
    }
    if second > 59 || year < 1970 {
        return None;
    }
    // The day count of a civil date (Howard Hinnant, `days_from_civil`).
    let shifted_year = year - u64::from(month <= 2);
    let era = shifted_year / 400;
    let year_of_era = shifted_year % 400;
    let shifted_month = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = (era * 146_097 + day_of_era).checked_sub(719_468)?;
    let time = UNIX_EPOCH + Duration::from_secs(days * 86_400 + hour * 3600 + minute * 60 + second);
    // A day that its month lacks, such as 02-30, counts on into the next
    // month. Only a real date writes back as the same text.
    (format(time) == text).then_some(time)
}

/// The value of ASCII decimal digits, or `None` for any other byte.
fn digits(bytes: &[u8]) -> Option<u64> {
    bytes.iter().try_fold(0, |value: u64, byte| {
        byte.is_ascii_digit()
            .then(|| value * 10 + u64::from(byte - b'0'))
    })
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use super::{format, parse};

    fn at(secs: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(secs)
    }

    #[test]
    fn the_plan_vectors_write_utc_with_z() {
        assert_eq!(format(UNIX_EPOCH), "1970-01-01T00:00:00Z");
        assert_eq!(format(at(1_835_438_400)), "2028-02-29T12:00:00Z");
    }

    #[test]
    fn the_caregiver_fixture_times_match() {
        assert_eq!(format(at(1_789_819_121)), "2026-09-19T11:58:41Z");
        assert_eq!(format(at(1_789_819_195)), "2026-09-19T11:59:55Z");
    }

    #[test]
    fn a_month_and_a_year_roll_over() {
        assert_eq!(format(at(951_782_399)), "2000-02-28T23:59:59Z");
        assert_eq!(format(at(951_782_400)), "2000-02-29T00:00:00Z");
        assert_eq!(format(at(1_798_761_599)), "2026-12-31T23:59:59Z");
        assert_eq!(format(at(1_798_761_600)), "2027-01-01T00:00:00Z");
    }

    #[test]
    fn a_fraction_is_cut_and_a_time_before_1970_is_the_epoch() {
        let since = at(1_789_819_121) + Duration::from_millis(999);
        assert_eq!(format(since), "2026-09-19T11:58:41Z");
        let before = UNIX_EPOCH - Duration::from_secs(1);
        assert_eq!(format(before), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn parse_reads_what_format_writes() {
        for secs in [0, 951_782_400, 1_789_819_121, 1_798_761_599, 1_835_438_400] {
            assert_eq!(parse(&format(at(secs))), Some(at(secs)));
        }
    }

    #[test]
    fn parse_refuses_any_other_form() {
        let refused = [
            "2026-09-19T11:58:41",
            "2026-09-19T11:58:41.5Z",
            "2026-09-19T11:58:41+00:00",
            "2026-09-19 11:58:41Z",
            "2026-9-19T11:58:41ZZ",
            "2026-02-29T00:00:00Z",
            "2026-04-31T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-00-01T00:00:00Z",
            "2026-01-00T00:00:00Z",
            "2026-01-01T24:00:00Z",
            "2026-01-01T00:60:00Z",
            "2026-01-01T00:00:60Z",
            "1969-12-31T23:59:59Z",
            "+026-01-01T00:00:00Z",
            "",
        ];
        for text in refused {
            assert_eq!(parse(text), None, "{text}");
        }
    }
}
