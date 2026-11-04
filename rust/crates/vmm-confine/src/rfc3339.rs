//! RFC 3339 times of the vmm: UTC with `Z`, to the second (root
//! `AGENTS.md`, Time). The fault file of decision CH21-1 writes them. A
//! small civil-date formatter writes them, so the crate needs no date
//! crate.

use std::time::{SystemTime, UNIX_EPOCH};

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

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use super::format;

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
}
