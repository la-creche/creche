//! The tests of `creche_runtime::clock` and of `creche_runtime::entropy` that
//! use the clock and the random source of the testkit.
//!
//! A test of a service gives `FixedClock` or `PausedClock` and
//! `CountingEntropy` to the code under test. The test then knows each time
//! stamp, each id and each token of that code. The tests here hold the texts
//! that such a test reads. Each text is the text that the Python code gives
//! for the same time and for the same bytes.
//!
//! The source of the testkit counts from one fill to the next fill. The tests
//! here thus also hold that a mint makes one fill. The sources of the tests
//! in `src/entropy.rs` give the same bytes at each fill, so a second fill
//! passes there.
//!
//! The testkit depends on `creche-runtime`, so a test inside `src/` sees
//! other types than the testkit sees. A test with a clock or a source of the
//! testkit is thus in this directory.
//!
//! No difference from the Python code is new here. The `DEVIATIONS` tables of
//! `src/clock.rs` and of `src/entropy.rs` hold each one.

#[cfg(test)]
mod tests {
    use std::num::NonZeroUsize;
    use std::sync::Arc;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use creche_contracts::{manifest, session, status};
    use creche_runtime::clock::{Clock, unix_micros, unix_seconds};
    use creche_runtime::entropy::{Entropy, MintError, new_ulid, url_token};
    use creche_testkit::clock::{FixedClock, PausedClock};
    use creche_testkit::entropy::CountingEntropy;
    use tokio::runtime::Builder;

    /// The start of each clock of this file: 2026-10-01T15:50:49.637Z, as
    /// seconds and nanoseconds from 1970.
    ///
    /// The start is on a whole millisecond, 849.637 seconds. Its Python float
    /// is below that millisecond. Each Python copy thus mints the millisecond
    /// 849.636 at the start, and the time stamp of a journal line holds
    /// `.637`. The sum of the seconds and of the fraction gives another
    /// float, and that float gives the millisecond 849.637.
    const START_SECONDS: u64 = 1_790_869_849;
    const START_NANOS: u32 = 637_000_000;

    /// The float that Python `time.time()` gives at the start.
    const START_PYTHON_FLOAT: f64 = 1790869849.6369998;

    /// The id that each Python copy mints at the start for the bytes 0 to 9,
    /// and the id for the bytes 10 to 19. The copies are the five of
    /// `creche_runtime::entropy::new_ulid` and the first mint of
    /// `attendance/src/attendance/ids.py:110-138`.
    const ID_AT_START_BYTES_0: &str = "01M3W2JHH4000G40R40M30E209";
    const ID_AT_START_BYTES_10: &str = "01M3W2JHH4185GR38E1W8124GK";

    /// The id of the Python copies 5 milliseconds after the start, for the
    /// bytes 10 to 19.
    const ID_AFTER_5_MS_BYTES_10: &str = "01M3W2JHHA185GR38E1W8124GK";

    /// The id of the Python copies 90 seconds after the start, for the bytes
    /// 20 to 29.
    const ID_AFTER_90_S_BYTES_20: &str = "01M3W2N9DM2GAHC5RR34D1P70X";

    /// The text of `secrets.token_urlsafe(32)` of Python for the bytes 0 to
    /// 31, 32 to 63 and 64 to 95. `mint_webhook_token` of the caregiver and
    /// `mint_csrf` of the noticeboard give the same texts.
    const PYTHON_TOKENS: [&str; 3] = [
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
        "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8",
        "QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8",
    ];

    /// The id of the Python copies at the start for the bytes 32 to 41, and
    /// the Python token for the bytes 42 to 73. A test that mints a token, an
    /// id and a token from one source reads these two after the first token.
    const ID_AT_START_BYTES_32: &str = "01M3W2JHH440GJ48S44MK2EA19";
    const TOKEN_OF_BYTES_42: &str = "KissLS4vMDEyMzQ1Njc4OTo7PD0-P0BBQkNERUZHSEk";

    /// The count of bytes of a token of the Python services.
    const TOKEN_BYTES: usize = 32;

    fn start() -> SystemTime {
        UNIX_EPOCH + Duration::new(START_SECONDS, START_NANOS)
    }

    /// The text of the next id of `clock` and of `entropy`.
    fn mint(clock: &dyn Clock, entropy: &dyn Entropy) -> String {
        new_ulid(clock, entropy).unwrap().as_str().to_owned()
    }

    #[test]
    fn each_id_of_the_test_seams_is_the_python_text() {
        let clock = FixedClock::new(start());
        let entropy = CountingEntropy::new();

        // The clock stands still. Only the bytes of the source keep the two
        // ids apart.
        assert_eq!(mint(&clock, &entropy), ID_AT_START_BYTES_0);
        assert_eq!(mint(&clock, &entropy), ID_AT_START_BYTES_10);

        clock.advance(Duration::from_secs(90));

        assert_eq!(mint(&clock, &entropy), ID_AFTER_90_S_BYTES_20);
    }

    #[test]
    fn a_time_before_1970_takes_no_byte_of_the_counting_source() {
        let clock = FixedClock::new(UNIX_EPOCH - Duration::from_secs(1));
        let entropy = CountingEntropy::new();

        assert_eq!(new_ulid(&clock, &entropy), Err(MintError::TimeOutOfRange));

        clock.set(start());

        // The count of the source is where it was. The id holds the bytes 0
        // to 9.
        assert_eq!(mint(&clock, &entropy), ID_AT_START_BYTES_0);
    }

    #[test]
    fn a_service_mints_through_two_shared_pointers() {
        let clock: Arc<dyn Clock> = Arc::new(FixedClock::new(start()));
        let entropy: Arc<dyn Entropy> = Arc::new(CountingEntropy::new());
        let (other_clock, other_entropy) = (Arc::clone(&clock), Arc::clone(&entropy));

        let first = std::thread::spawn(move || mint(other_clock.as_ref(), other_entropy.as_ref()))
            .join()
            .unwrap();
        let second = mint(clock.as_ref(), entropy.as_ref());

        assert_eq!(first, ID_AT_START_BYTES_0);
        assert_eq!(second, ID_AT_START_BYTES_10);
    }

    #[test]
    fn an_id_follows_the_paused_time_of_tokio() {
        let runtime = Builder::new_current_thread()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap();

        let (first, second) = runtime.block_on(async {
            let clock = PausedClock::new(start());
            let entropy = CountingEntropy::new();
            let first = mint(&clock, &entropy);
            tokio::time::advance(Duration::from_millis(5)).await;

            (first, mint(&clock, &entropy))
        });

        assert_eq!(first, ID_AT_START_BYTES_0);
        assert_eq!(second, ID_AFTER_5_MS_BYTES_10);
    }

    // No assertion on a token prints the token. Each one is `assert!` with
    // `Secret::matches`, which prints no operand.

    #[test]
    fn each_token_of_the_counting_source_is_the_python_text() {
        let entropy = CountingEntropy::new();
        let bytes = NonZeroUsize::new(TOKEN_BYTES).unwrap();

        for (row, text) in PYTHON_TOKENS.into_iter().enumerate() {
            let token = url_token(&entropy, bytes).unwrap();

            assert!(token.matches(text.as_bytes()), "row {row}");
        }
    }

    #[test]
    fn an_id_and_a_token_take_their_bytes_from_one_count() {
        let clock = FixedClock::new(start());
        let entropy = CountingEntropy::new();
        let bytes = NonZeroUsize::new(TOKEN_BYTES).unwrap();
        let [first_text, _, _] = PYTHON_TOKENS;

        let first = url_token(&entropy, bytes).unwrap();
        let id = mint(&clock, &entropy);
        let second = url_token(&entropy, bytes).unwrap();

        // The first token takes the bytes 0 to 31. The id takes the bytes 32
        // to 41, and the second token takes the bytes 42 to 73.
        assert!(first.matches(first_text.as_bytes()));
        assert_eq!(id, ID_AT_START_BYTES_32);
        assert!(second.matches(TOKEN_OF_BYTES_42.as_bytes()));
    }

    #[test]
    fn each_time_stamp_of_the_test_clock_is_the_python_text() {
        let clock = FixedClock::new(start());
        let now = clock.now();
        let journal = session::Timestamp::from_unix_micros(unix_micros(now).unwrap()).unwrap();
        let status = status::time::Timestamp::try_from(now).unwrap();
        let request = manifest::Timestamp::new(unix_seconds(now).unwrap()).unwrap();

        // `rfc3339` and `rfc3339_ms` of `attendance/src/attendance/clock.py:20-34`.
        assert_eq!(journal.rfc3339(), "2026-10-01T15:50:49Z");
        assert_eq!(journal.rfc3339_millis(), "2026-10-01T15:50:49.637Z");
        // `rfc3339` of `caregiver/src/caregiver/clock.py:20-23` and
        // `rfc3339_s` of `chaperone/src/chaperone/family_ids.py:39-41`.
        assert_eq!(status.to_rfc3339(), "2026-10-01T15:50:49Z");
        // `time.time()` of `handover/src/handover/cli.py:144`.
        assert_eq!(request.get().to_bits(), START_PYTHON_FLOAT.to_bits());

        clock.advance(Duration::from_secs(90));
        let later = clock.now();
        let journal = session::Timestamp::from_unix_micros(unix_micros(later).unwrap()).unwrap();
        let status = status::time::Timestamp::try_from(later).unwrap();

        assert_eq!(journal.rfc3339_millis(), "2026-10-01T15:52:19.637Z");
        assert_eq!(status.to_rfc3339(), "2026-10-01T15:52:19Z");
    }
}
