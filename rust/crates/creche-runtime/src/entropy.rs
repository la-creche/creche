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
//!
//! The bodies of [`OsEntropy::fill`], [`new_ulid`] and [`url_token`] are
//! stubs. `AGENTS.md` of this crate lists the stubs and the packet that
//! writes them.

use std::error::Error;
use std::fmt::{self, Debug};
use std::io;
use std::num::NonZeroUsize;

use creche_contracts::ids::Ulid;
use creche_contracts::secret::Secret;

use crate::clock::Clock;

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
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-clock-entropy writes this body"
    )]
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyError> {
        todo!()
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

/// Mints one ULID: the time of `clock`, then 80 random bits of `entropy`
/// (contract 02 §2).
///
/// Two calls in one millisecond give two ids that can sort in each order. A
/// service that needs ids which never repeat and always sort holds its own
/// state on top of this function.
///
/// # Errors
///
/// [`MintError::Entropy`] when the random source fails, and
/// [`MintError::TimeOutOfRange`] for a time that a ULID cannot hold.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-clock-entropy writes this body"
)]
pub fn new_ulid(clock: &dyn Clock, entropy: &dyn Entropy) -> Result<Ulid, MintError> {
    todo!()
}

/// Mints one random token: `bytes` random bytes, as URL-safe base64 with no
/// padding.
///
/// 32 bytes give 43 characters. This is the text of `secrets.token_urlsafe`
/// of Python.
///
/// # Errors
///
/// [`EntropyError`] when the random source fails.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-clock-entropy writes this body"
)]
pub fn url_token(entropy: &dyn Entropy, bytes: NonZeroUsize) -> Result<Secret, EntropyError> {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn failed() -> EntropyError {
        EntropyError {
            kind: io::ErrorKind::UnexpectedEof,
            os_text: String::from("the source ended early"),
        }
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
}
