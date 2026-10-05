//! The random source of a service, as a seam that a test replaces, and the
//! two mints that use it.
//!
//! Code that needs random bytes takes a `&dyn Entropy`. A test gives a source
//! that counts, so the test knows each id and each token that the code mints.
//!
//! [`new_ulid`] mints an id of contract 02 §2. Five Python copies do that by
//! hand, for example `door-trigger/src/agent_door_trigger/ulid.py:30-55`.
//! [`url_token`] mints a random token, as
//! `caregiver/src/caregiver/webhook_tokens.py:64-67` does.

use std::error::Error;
use std::fmt::{self, Debug};
use std::fs::File;
use std::io::{self, Read};
use std::num::NonZeroUsize;
use std::path::Path;

use creche_contracts::ids::Ulid;
use creche_contracts::manifest::{self, Timestamp};
use creche_contracts::secret::Secret;

use crate::clock::{self, Clock};
use crate::readfile::os_text;

/// The random device of the host.
const DEVICE: &str = "/dev/urandom";

/// The count of random bytes of a ULID: 80 bits (contract 02 §2).
const ULID_RANDOM_BYTES: usize = 10;

/// The alphabet of URL-safe base64 (RFC 4648 §5).
const URL_SAFE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/// The count of bytes that one group of base64 holds.
const GROUP_BYTES: usize = 3;

/// The count of characters of one full group of base64.
const GROUP_CHARS: usize = 4;

/// The shift of each character in the 24 bits of one group, first character
/// first.
const CHAR_SHIFTS: [u32; GROUP_CHARS] = [18, 12, 6, 0];

/// The bits of one character of base64.
const CHAR_MASK: u32 = 0b11_1111;

/// What an error says when the device ends before the fill is complete. No
/// error of the operating system holds that case.
const DEVICE_ENDED: &str = "the random device gave less bytes than the fill needs";

/// What an error says when the process cannot hold the bytes of a token.
/// This is the text of `ENOMEM`.
const NO_MEMORY: &str = "Cannot allocate memory";

/// What an error says when a token does not hold each of its characters.
const TOKEN_NOT_COMPLETE: &str = "the token does not hold each of its characters";

/// A source of random bytes.
///
/// The trait is object safe. A service holds an `Arc<dyn Entropy>` or takes
/// a `&dyn Entropy`.
pub trait Entropy: Send + Sync + Debug {
    /// Fills each byte of `bytes` with a random byte.
    ///
    /// # Errors
    ///
    /// [`EntropyError`] when the source gives no byte or less bytes than the
    /// slice holds. The slice then holds no value that the caller can use.
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyError>;
}

/// Why a source gave no random bytes.
///
/// The error never holds a random byte.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntropyError {
    /// The kind of the error of the operating system.
    pub kind: io::ErrorKind,
    /// The text of the error, as `strerror` of Python gives it.
    pub os_text: String,
}

impl fmt::Display for EntropyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the random source gave no bytes: {}", self.os_text)
    }
}

impl Error for EntropyError {}

/// The random source of the operating system.
///
/// `service::run` makes one for each process.
///
/// ```
/// use creche_runtime::entropy::OsEntropy;
///
/// let entropy = OsEntropy::new();
/// assert_eq!(format!("{entropy:?}"), "OsEntropy");
/// ```
///
/// Code outside this module builds the source only with [`OsEntropy::new`]:
///
/// ```compile_fail,E0423
/// use creche_runtime::entropy::OsEntropy;
///
/// let entropy = OsEntropy(());
/// ```
#[derive(Clone)]
pub struct OsEntropy(());

impl OsEntropy {
    /// The random source of the operating system. The call opens no file.
    #[must_use]
    pub const fn new() -> Self {
        Self(())
    }
}

impl Default for OsEntropy {
    fn default() -> Self {
        Self::new()
    }
}

impl Debug for OsEntropy {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("OsEntropy")
    }
}

impl Entropy for OsEntropy {
    /// Reads the bytes from the random device of the host, `/dev/urandom`.
    /// A fill of no byte opens no file.
    ///
    /// This is the one read with `std::fs` that the runtime permits on a
    /// thread of the runtime (`rust/AGENTS.md`, "The rules for a service",
    /// rule 7). After the start of the host the device gives each byte with
    /// no wait, so the read does not hold the thread.
    ///
    /// The Python code reads the same source with `os.urandom`, for example
    /// `door-trigger/src/agent_door_trigger/ulid.py:37`, and with
    /// `secrets.token_bytes`, for example
    /// `caregiver/src/caregiver/webhook_tokens.py:66`.
    ///
    /// # Errors
    ///
    /// [`EntropyError`] when the open or the read fails, and when the device
    /// ends before the slice is full. Each byte of the slice is then zero:
    /// the slice never holds a part of a fill.
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyError> {
        fill_at(Path::new(DEVICE), bytes)
    }
}

/// Fills `bytes` from the file at `device`. A test gives another path.
fn fill_at(device: &Path, bytes: &mut [u8]) -> Result<(), EntropyError> {
    if bytes.is_empty() {
        return Ok(());
    }

    let read = File::open(device).and_then(|mut file| file.read_exact(bytes));
    let Err(error) = read else {
        return Ok(());
    };

    // A read that fails can leave some bytes of the device in the slice.
    bytes.fill(0);

    Err(failed_read(&error))
}

/// The error for a read of the device that failed. The text of an error of
/// the operating system is the text of `strerror` of Python, with no number
/// of the error: [`os_text`] holds that rule. A device that ends early gets
/// [`DEVICE_ENDED`].
fn failed_read(error: &io::Error) -> EntropyError {
    let os_text = if error.kind() == io::ErrorKind::UnexpectedEof {
        String::from(DEVICE_ENDED)
    } else {
        os_text(error)
    };

    EntropyError {
        kind: error.kind(),
        os_text,
    }
}

/// The error for a count of bytes that the process cannot hold.
fn no_memory() -> EntropyError {
    EntropyError {
        kind: io::ErrorKind::OutOfMemory,
        os_text: String::from(NO_MEMORY),
    }
}

/// The error for a token that lacks a character. No input gives it: each
/// group of bytes has its characters in [`URL_SAFE`].
fn token_not_complete() -> EntropyError {
    EntropyError {
        kind: io::ErrorKind::InvalidData,
        os_text: String::from(TOKEN_NOT_COMPLETE),
    }
}

/// Why [`new_ulid`] minted no id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MintError {
    /// The random source gave no bytes.
    Entropy(EntropyError),
    /// The time of the clock is before 1970, or its milliseconds do not fit
    /// the 48 bits of a ULID.
    TimeOutOfRange,
}

impl fmt::Display for MintError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Entropy(error) => write!(f, "{error}"),
            Self::TimeOutOfRange => {
                f.write_str("the time of the clock does not fit the 48 bits of a ULID")
            }
        }
    }
}

impl Error for MintError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Entropy(error) => Some(error),
            Self::TimeOutOfRange => None,
        }
    }
}

// CONTRACT-QUESTION: contract 02 §2 gives the length and the alphabet of a
// ULID and no layout of its bits. Each Python copy writes 48 bits of
// milliseconds, then 80 random bits. The contract does not say what a mint
// does when the clock shows a time before 1970 or a time past those 48 bits.
// Each Python copy mints 26 characters for both times. `new_ulid` refuses
// both, as `manifest::mint_ulid` refuses them for a request id. No host shows
// such a time today. A change costs one more mint in `creche-contracts`:
// `manifest::Timestamp` holds no time below zero.
/// Mints one ULID: the time of `clock`, then 80 random bits of `entropy`
/// (contract 02 §2).
///
/// Two calls in one millisecond give two ids that can sort in each order. A
/// service that needs ids which never repeat and always sort holds its own
/// state on top of this function.
///
/// The function makes one fill of 10 bytes for each id. It makes no fill for
/// a time before 1970. For a time past 48 bits of milliseconds it makes the
/// fill and then refuses. A test with a source that counts thus knows the
/// bytes of each id.
///
/// Five Python copies mint the same text from the same time and the same
/// bytes: `door-trigger/src/agent_door_trigger/ulid.py:30-55`,
/// `door-tui/src/agent_door_tui/ids.py:95-111`,
/// `chaperone/src/chaperone/delegations.py:58-74`,
/// `caregiver/src/caregiver/mcp_release.py:533-541` and
/// `handover/src/handover/requester/file.py:87-100`. Each one cuts the
/// milliseconds of `time.time()`, and [`clock::unix_seconds`] gives that
/// float.
///
/// ```
/// use creche_runtime::clock::SystemClock;
/// use creche_runtime::entropy::{OsEntropy, new_ulid};
///
/// let id = new_ulid(&SystemClock::new(), &OsEntropy::new())?;
/// assert_eq!(id.as_str().len(), 26);
/// # Ok::<(), creche_runtime::entropy::MintError>(())
/// ```
///
/// # Errors
///
/// [`MintError::Entropy`] when the random source fails, and
/// [`MintError::TimeOutOfRange`] for a time that a ULID cannot hold.
pub fn new_ulid(clock: &dyn Clock, entropy: &dyn Entropy) -> Result<Ulid, MintError> {
    let seconds = clock::unix_seconds(clock.now()).ok_or(MintError::TimeOutOfRange)?;
    let now = Timestamp::new(seconds).map_err(|_| MintError::TimeOutOfRange)?;

    let mut random = [0_u8; ULID_RANDOM_BYTES];
    entropy.fill(&mut random).map_err(MintError::Entropy)?;

    manifest::mint_ulid(now, random).map_err(|_| MintError::TimeOutOfRange)
}

/// Mints one random token: `bytes` random bytes, as URL-safe base64 with no
/// padding. The function makes one fill of `bytes` bytes.
///
/// 32 bytes give 43 characters. This is the text of `secrets.token_urlsafe`
/// of Python: `noticeboard/src/noticeboard/security.py:74-76` and
/// `handover/src/handover/intake/token.py:101`.
/// `caregiver/src/caregiver/webhook_tokens.py:64-67` writes the same text by
/// hand.
///
/// ```
/// use std::num::NonZeroUsize;
///
/// use creche_runtime::entropy::{OsEntropy, url_token};
///
/// let token = url_token(&OsEntropy::new(), NonZeroUsize::new(32).unwrap())?;
/// assert_eq!(token.expose_secret().len(), 43);
/// assert_eq!(format!("{token:?}"), "Secret(<redacted>)");
/// # Ok::<(), creche_runtime::entropy::EntropyError>(())
/// ```
///
/// # Errors
///
/// [`EntropyError`] when the random source fails, and for a count of bytes
/// that the process cannot hold in memory.
pub fn url_token(entropy: &dyn Entropy, bytes: NonZeroUsize) -> Result<Secret, EntropyError> {
    let count = bytes.get();
    let mut raw = Vec::new();
    raw.try_reserve_exact(count).map_err(|_| no_memory())?;
    raw.resize(count, 0_u8);
    entropy.fill(&mut raw)?;

    Secret::try_from(base64_url(&raw)?).map_err(|_| token_not_complete())
}

/// The text of `raw` as URL-safe base64 with no padding, as
/// `base64.urlsafe_b64encode(raw).rstrip(b"=")` of Python gives it.
///
/// A group of 3 bytes gives 4 characters. A last group of 1 byte gives 2
/// characters, and a last group of 2 bytes gives 3.
fn base64_url(raw: &[u8]) -> Result<String, EntropyError> {
    let chars = raw
        .len()
        .div_ceil(GROUP_BYTES)
        .checked_mul(GROUP_CHARS)
        .ok_or_else(no_memory)?;
    let mut text = String::new();
    text.try_reserve_exact(chars).map_err(|_| no_memory())?;

    for group in raw.chunks(GROUP_BYTES) {
        // The bytes of the group are the first bytes of 24 bits. A short
        // group has zero bits after its last byte.
        let bits = (0..GROUP_BYTES).fold(0_u32, |bits, place| {
            (bits << u8::BITS) | group.get(place).map_or(0, |byte| u32::from(*byte))
        });

        for shift in CHAR_SHIFTS.iter().take(group.len() + 1) {
            let byte = usize::try_from((bits >> shift) & CHAR_MASK)
                .ok()
                .and_then(|digit| URL_SAFE.get(digit))
                .ok_or_else(token_not_complete)?;
            text.push(char::from(*byte));
        }
    }

    Ok(text)
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use creche_testkit::root::TempRoot;

    use super::*;
    use crate::clock::Monotonic;

    /// A clock that shows one wall time.
    #[derive(Debug)]
    struct At(SystemTime);

    impl Clock for At {
        fn now(&self) -> SystemTime {
            self.0
        }

        fn monotonic(&self) -> Monotonic {
            Monotonic::from_start(Duration::ZERO)
        }
    }

    /// A source that gives the same bytes at each fill. It fails for a fill
    /// of another count of bytes, so a test also knows the count that the
    /// code asks for.
    struct Fixed(&'static [u8]);

    impl Debug for Fixed {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("Fixed")
        }
    }

    impl Entropy for Fixed {
        fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyError> {
            if bytes.len() != self.0.len() {
                return Err(failed());
            }
            bytes.copy_from_slice(self.0);

            Ok(())
        }
    }

    /// A source that counts from zero at each fill: 0, 1, 2 and so on. It
    /// fills each count of bytes, as `bytes(range(n))` of Python.
    #[derive(Debug)]
    struct Counted;

    impl Entropy for Counted {
        fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyError> {
            for (byte, value) in bytes.iter_mut().zip((0..=u8::MAX).cycle()) {
                *byte = value;
            }

            Ok(())
        }
    }

    /// A source that gives no byte.
    #[derive(Debug)]
    struct Broken;

    impl Entropy for Broken {
        fn fill(&self, _bytes: &mut [u8]) -> Result<(), EntropyError> {
            Err(failed())
        }
    }

    fn failed() -> EntropyError {
        EntropyError {
            kind: io::ErrorKind::UnexpectedEof,
            os_text: String::from("the source ended early"),
        }
    }

    /// The time that is `seconds` and `nanos` after 1970-01-01T00:00:00Z.
    fn after_1970(seconds: u64, nanos: u32) -> At {
        At(UNIX_EPOCH + Duration::new(seconds, nanos))
    }

    /// The time that is `seconds` and `nanos` before 1970-01-01T00:00:00Z.
    fn before_1970(seconds: u64, nanos: u32) -> At {
        At(UNIX_EPOCH - Duration::new(seconds, nanos))
    }

    fn count(bytes: usize) -> NonZeroUsize {
        NonZeroUsize::new(bytes).unwrap()
    }

    /// Whether each byte of `text` is in the alphabet of URL-safe base64. The
    /// padding `=` is not in it.
    fn is_url_safe(text: &[u8]) -> bool {
        text.iter().all(|byte| URL_SAFE.contains(byte))
    }

    #[test]
    fn an_error_names_the_answer_of_the_system() {
        assert_eq!(
            failed().to_string(),
            "the random source gave no bytes: the source ended early"
        );
    }

    #[test]
    fn a_mint_error_names_its_cause() {
        let error = MintError::Entropy(failed());

        assert_eq!(error.to_string(), failed().to_string());
        assert!(error.source().is_some());
        assert_eq!(
            MintError::TimeOutOfRange.to_string(),
            "the time of the clock does not fit the 48 bits of a ULID"
        );
        assert!(MintError::TimeOutOfRange.source().is_none());
    }

    #[test]
    fn a_source_is_usable_behind_a_trait_object() {
        fn takes(_entropy: &dyn Entropy) {}

        takes(&OsEntropy::new());
        takes(&OsEntropy::default());
    }

    // --- the random source of the host ---

    // No test here prints a byte of the host. An assertion on random bytes
    // is `assert!`, which prints no operand.

    #[test]
    fn the_host_fills_no_byte_one_byte_and_4096_bytes() {
        let source = OsEntropy::new();
        let mut none = [0_u8; 0];
        let mut many = vec![0_u8; 4096];
        // 64 fills of one byte. The chance that the host gives one value 64
        // times is 2^-504.
        let ones: Vec<u8> = (0..64)
            .map(|_| {
                let mut one = [0_u8; 1];
                source.fill(&mut one).unwrap();
                u8::from_ne_bytes(one)
            })
            .collect();

        assert_eq!(source.fill(&mut none), Ok(()));
        assert_eq!(source.fill(&mut many), Ok(()));
        assert!(ones.iter().any(|one| Some(one) != ones.first()));
        // The fill reaches each part of the slice. The chance that the host
        // gives 64 zero bytes in a row is 2^-512.
        assert!(
            many.chunks(64)
                .all(|part| part.iter().any(|byte| *byte != 0))
        );
    }

    #[test]
    fn two_fills_of_the_host_differ() {
        let source = OsEntropy::new();
        let mut first = [0_u8; 32];
        let mut second = [0_u8; 32];
        source.fill(&mut first).unwrap();
        source.fill(&mut second).unwrap();

        assert!(first != second);
    }

    #[test]
    fn a_fill_of_no_byte_opens_no_file() {
        let root = TempRoot::new().unwrap();
        let mut none = [0_u8; 0];

        assert_eq!(fill_at(&root.path().join("absent"), &mut none), Ok(()));
    }

    #[test]
    fn a_fill_holds_the_bytes_of_the_device() {
        let root = TempRoot::new().unwrap();
        let device = root.path().join("device");
        std::fs::write(&device, [7, 6, 5, 4, 3, 2, 1, 0]).unwrap();
        let mut whole = [0xff_u8; 8];
        let mut start = [0xff_u8; 3];

        assert_eq!(fill_at(&device, &mut whole), Ok(()));
        assert_eq!(fill_at(&device, &mut start), Ok(()));
        assert_eq!(whole, [7, 6, 5, 4, 3, 2, 1, 0]);
        assert_eq!(start, [7, 6, 5]);
    }

    #[test]
    fn a_device_that_ends_early_is_an_error_and_never_a_part_of_a_fill() {
        let root = TempRoot::new().unwrap();
        let device = root.path().join("device");
        std::fs::write(&device, [7, 6, 5]).unwrap();
        let mut bytes = [0xff_u8; 8];

        assert_eq!(
            fill_at(&device, &mut bytes),
            Err(EntropyError {
                kind: io::ErrorKind::UnexpectedEof,
                os_text: String::from(DEVICE_ENDED),
            })
        );
        assert_eq!(bytes, [0; 8]);
    }

    #[test]
    fn a_device_that_does_not_open_is_an_error() {
        let root = TempRoot::new().unwrap();
        let mut bytes = [0xff_u8; 8];

        assert_eq!(
            fill_at(&root.path().join("absent"), &mut bytes),
            Err(EntropyError {
                kind: io::ErrorKind::NotFound,
                os_text: String::from("No such file or directory"),
            })
        );
        assert_eq!(bytes, [0; 8]);
    }

    #[test]
    fn a_device_that_gives_no_read_is_an_error() {
        let root = TempRoot::new().unwrap();
        let mut bytes = [0xff_u8; 8];

        // A directory opens, and each read of it fails.
        assert_eq!(
            fill_at(root.path(), &mut bytes),
            Err(EntropyError {
                kind: io::ErrorKind::IsADirectory,
                os_text: String::from("Is a directory"),
            })
        );
        assert_eq!(bytes, [0; 8]);
    }

    #[test]
    fn the_text_of_an_error_has_no_error_number() {
        let no_entry = io::Error::from_raw_os_error(2);
        let plain = io::Error::other("a text with no number");

        assert!(no_entry.to_string().ends_with(" (os error 2)"));
        assert_eq!(
            failed_read(&no_entry),
            EntropyError {
                kind: io::ErrorKind::NotFound,
                os_text: String::from("No such file or directory"),
            }
        );
        assert_eq!(
            failed_read(&plain),
            EntropyError {
                kind: io::ErrorKind::Other,
                os_text: String::from("a text with no number"),
            }
        );
    }

    // --- the mint of a ULID ---

    const ZEROS: &[u8] = &[0x00; 10];
    const ONES: &[u8] = &[0xff; 10];
    const COUNTED: &[u8] = &[0, 1, 2, 3, 4, 5, 6, 7, 8, 9];

    /// A time, 10 random bytes and the id that each of the five Python copies
    /// mints for them. The time of a copy is the float that `time.time()`
    /// gives for the seconds and the nanoseconds of the row.
    ///
    /// The time of the first row is on a whole millisecond, 849.637 seconds.
    /// Its Python float is below that millisecond, so each copy mints the
    /// millisecond 849.636.
    const PYTHON_ULIDS: [(u64, u32, &[u8], &str); 8] = [
        (
            1_790_869_849,
            637_000_000,
            COUNTED,
            "01M3W2JHH4000G40R40M30E209",
        ),
        (
            1_758_153_590,
            123_456_789,
            ONES,
            "01K5D1XHBBZZZZZZZZZZZZZZZZ",
        ),
        (
            1_934_347_390,
            123_456_789,
            COUNTED,
            "01R9G1DK5B000G40R40M30E209",
        ),
        (0, 0, ZEROS, "00000000000000000000000000"),
        (0, 1, COUNTED, "0000000000000G40R40M30E209"),
        (0, 999_999, ONES, "0000000000ZZZZZZZZZZZZZZZZ"),
        (
            281_474_976_710,
            655_000_000,
            ONES,
            "7ZZZZZZZZYZZZZZZZZZZZZZZZZ",
        ),
        (
            281_474_976_710,
            655_200_000,
            ONES,
            "7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
        ),
    ];

    #[test]
    fn an_id_of_a_fixed_time_and_fixed_bytes_is_the_python_text() {
        for (seconds, nanos, random, text) in PYTHON_ULIDS {
            let minted = new_ulid(&after_1970(seconds, nanos), &Fixed(random)).unwrap();

            assert_eq!(minted.as_str(), text, "{seconds} s and {nanos} ns");
        }
    }

    #[test]
    fn an_id_of_a_later_millisecond_sorts_after_an_earlier_one() {
        let ids: Vec<Ulid> = [637_000_000, 638_000_000, 639_000_000]
            .into_iter()
            .map(|nanos| new_ulid(&after_1970(1_790_869_849, nanos), &Fixed(ZEROS)).unwrap())
            .collect();
        let texts: Vec<&str> = ids.iter().map(Ulid::as_str).collect();

        assert_eq!(
            texts,
            [
                "01M3W2JHH40000000000000000",
                "01M3W2JHH60000000000000000",
                "01M3W2JHH70000000000000000",
            ]
        );
        assert!(texts.is_sorted());
    }

    #[test]
    fn a_mint_asks_the_source_for_10_bytes() {
        let now = after_1970(1_790_869_849, 637_000_000);

        assert!(new_ulid(&now, &Fixed(&[0; 10])).is_ok());
        assert_eq!(
            new_ulid(&now, &Fixed(&[0; 9])),
            Err(MintError::Entropy(failed()))
        );
        assert_eq!(
            new_ulid(&now, &Fixed(&[0; 11])),
            Err(MintError::Entropy(failed()))
        );
    }

    #[test]
    fn a_time_that_a_ulid_cannot_hold_is_refused() {
        let refused = [
            before_1970(0, 1),
            before_1970(0, 500_000),
            before_1970(1, 0),
            // The Python float of this time is 281474976710.656 seconds.
            after_1970(281_474_976_710, 655_999_999),
            after_1970(281_474_976_710, 656_000_000),
            after_1970(9_223_372_036_854, 0),
        ];

        for clock in refused {
            assert_eq!(
                new_ulid(&clock, &Counted),
                Err(MintError::TimeOutOfRange),
                "{clock:?}"
            );
        }
    }

    #[test]
    fn a_time_before_1970_asks_the_source_for_no_byte() {
        // A source that fails gives `MintError::Entropy` when the mint asks
        // it for a byte. The mint thus refuses the time first.
        assert_eq!(
            new_ulid(&before_1970(1, 0), &Broken),
            Err(MintError::TimeOutOfRange)
        );
    }

    #[test]
    fn a_source_that_fails_gives_no_id() {
        assert_eq!(
            new_ulid(&after_1970(1_790_869_849, 0), &Broken),
            Err(MintError::Entropy(failed()))
        );
    }

    #[test]
    fn two_ids_of_the_host_differ() {
        // One fixed time for the two mints: only the random bytes of the
        // host keep the two ids apart.
        let clock = after_1970(1_700_000_000, 0);
        let entropy = OsEntropy::new();
        let first = new_ulid(&clock, &entropy).unwrap();
        let second = new_ulid(&clock, &entropy).unwrap();

        assert_eq!(first.as_str().len(), 26);
        assert_ne!(first, second);
    }

    // --- the mint of a token ---

    // No test here prints a token. An assertion on a token is `assert!`
    // with `Secret::matches`, which prints no operand.

    /// The bytes 200 to 231. Their text holds `-` and `_`.
    const HIGH: &[u8] = &[
        200, 201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217,
        218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 229, 230, 231,
    ];

    /// Random bytes and the text that Python gives for them:
    /// `secrets.token_urlsafe` with those bytes from `secrets.token_bytes`,
    /// `mint_csrf` of the noticeboard and, for 32 bytes, `mint_webhook_token`
    /// of the caregiver. The three give one text.
    const PYTHON_TOKENS: [(&[u8], &str); 8] = [
        (&[0xff; 32], "__________________________________________8"),
        (HIGH, "yMnKy8zNzs_Q0dLT1NXW19jZ2tvc3d7f4OHi4-Tl5uc"),
        (&[0xfb, 0xff, 0xbf], "-_-_"),
        (&[0x00], "AA"),
        (&[0xff], "_w"),
        (&[0xff, 0xff], "__8"),
        (&[0, 1, 2, 3], "AAECAw"),
        (&[0, 1, 2, 3, 4], "AAECAwQ"),
    ];

    /// The text that Python gives for the bytes 0 to 31.
    const PYTHON_TOKEN_OF_32: &[u8] = b"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

    #[test]
    fn a_token_of_32_fixed_bytes_is_the_python_text() {
        let token = url_token(&Counted, count(32)).unwrap();

        assert!(token.matches(PYTHON_TOKEN_OF_32));
        assert_eq!(token.expose_secret().len(), 43);
        assert!(is_url_safe(token.expose_secret()));
    }

    #[test]
    fn a_token_of_fixed_bytes_is_the_python_text() {
        for (row, (raw, text)) in PYTHON_TOKENS.into_iter().enumerate() {
            let token = url_token(&Fixed(raw), count(raw.len())).unwrap();

            assert!(token.matches(text.as_bytes()), "row {row}");
        }
    }

    #[test]
    fn a_token_has_no_padding_for_each_count_of_bytes() {
        for bytes in 1..=100 {
            let token = url_token(&Counted, count(bytes)).unwrap();
            let text = token.expose_secret();

            // 3 bytes give 4 characters, and a last group of 1 or 2 bytes
            // gives 2 or 3.
            assert_eq!(text.len(), (bytes * 4).div_ceil(3), "{bytes} bytes");
            assert!(is_url_safe(text), "{bytes} bytes");
        }
    }

    #[test]
    fn a_token_of_the_host_has_43_characters_and_no_padding() {
        let entropy = OsEntropy::new();
        let first = url_token(&entropy, count(32)).unwrap();
        let second = url_token(&entropy, count(32)).unwrap();

        assert_eq!(first.expose_secret().len(), 43);
        assert!(is_url_safe(first.expose_secret()));
        assert!(!first.matches(second.expose_secret()));
        assert_eq!(format!("{first:?}"), "Secret(<redacted>)");
    }

    #[test]
    fn a_source_that_fails_gives_no_token() {
        assert_eq!(url_token(&Broken, count(32)).unwrap_err(), failed());
    }

    #[test]
    fn a_count_of_bytes_that_no_process_holds_is_an_error() {
        let error = url_token(&Counted, NonZeroUsize::MAX).unwrap_err();

        assert_eq!(
            error,
            EntropyError {
                kind: io::ErrorKind::OutOfMemory,
                os_text: String::from("Cannot allocate memory"),
            }
        );
    }

    // --- the differences from the Python code ---

    /// One difference from the Python code on purpose. No vector covers a
    /// mint, so each row names a Python line.
    struct Deviation {
        /// The Python line.
        python: &'static str,
        /// What the Python line does, and what this module does.
        difference: &'static str,
        /// Whether this module does what `difference` says.
        holds: fn() -> bool,
    }

    const DEVIATIONS: [Deviation; 4] = [
        Deviation {
            python: "door-trigger/src/agent_door_trigger/ulid.py:36",
            difference: "For a time before 1970 the copy mints 26 characters from a count below \
                         zero. `new_ulid` gives `MintError::TimeOutOfRange`.",
            holds: || new_ulid(&before_1970(1, 0), &Counted) == Err(MintError::TimeOutOfRange),
        },
        Deviation {
            python: "door-trigger/src/agent_door_trigger/ulid.py:36",
            difference: "For a time past 48 bits of milliseconds the copy mints 26 characters \
                         that hold more than 48 bits of time. `new_ulid` gives \
                         `MintError::TimeOutOfRange`.",
            holds: || {
                new_ulid(&after_1970(281_474_976_710, 656_000_000), &Counted)
                    == Err(MintError::TimeOutOfRange)
            },
        },
        Deviation {
            python: "door-trigger/src/agent_door_trigger/ulid.py:37",
            difference: "On Linux, `os.urandom` asks the kernel with `getrandom` and opens no \
                         file. `OsEntropy::fill` opens the random device. It gives an error \
                         when the device does not open. `os.urandom` also waits until the \
                         kernel has seeded its pool, and the read of the device does not wait.",
            holds: || {
                TempRoot::new()
                    .is_ok_and(|root| fill_at(&root.path().join("absent"), &mut [0_u8; 1]).is_err())
            },
        },
        Deviation {
            python: "noticeboard/src/noticeboard/security.py:76",
            difference: "`secrets.token_urlsafe` takes the count 0 and gives the empty text. \
                         `url_token` takes no count of 0: the type of its argument has no such \
                         value. The smallest count gives a token of 2 characters.",
            holds: || {
                url_token(&Counted, NonZeroUsize::MIN)
                    .is_ok_and(|token| token.expose_secret().len() == 2)
            },
        },
    ];

    #[test]
    fn each_deviation_names_a_python_line_and_holds() {
        for row in &DEVIATIONS {
            let (file, line) = row.python.rsplit_once(':').unwrap();

            assert!(file.ends_with(".py"), "{}", row.python);
            assert!(line.parse::<u32>().is_ok(), "{}", row.python);
            assert!(row.difference.ends_with('.'), "{}", row.python);
            assert!((row.holds)(), "{}: {}", row.python, row.difference);
        }
    }
}
