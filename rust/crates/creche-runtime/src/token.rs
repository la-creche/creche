//! A token file into a `Secret`, and the bearer of a request.
//!
//! Each service reads a token from a file and never from a variable or from
//! its arguments (invariant 13). The five Python readers apply different
//! rules to that file. A [`TokenRule`] holds the rules of one reader, and
//! this module has one constant for each reader:
//!
//! | Constant | The Python reader |
//! |---|---|
//! | [`TokenRule::ATTENDANCE`] | `attendance/src/attendance/auth.py:242-265` |
//! | [`TokenRule::ATTENDANCE_PEP_READ`] | `attendance/src/attendance/auth.py:268-279` |
//! | [`TokenRule::DOOR`] | `door-owui/src/agent_door_owui/config.py:117-146`, `chaperone/src/chaperone/delegate.py:141-158` |
//! | [`TokenRule::WEBHOOK`] | `door-trigger/src/agent_door_trigger/tokens.py:35-66` |
//! | [`TokenRule::NOT_EMPTY`] | `caregiver/src/caregiver/switch.py:80-94` |
//!
//! A service states what it does with a [`TokenError`]. At start it gives the
//! error to `service::refuse_start`. No error holds the token, a part of it
//! or the count of its bytes.
//!
//! The bodies of [`read`], [`CachedToken`] and [`bearer_of`] are stubs.
//! `AGENTS.md` of this crate lists the stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::path::{Path, PathBuf};

use ::http::HeaderMap;
use creche_contracts::config::MIN_KEY_BYTES;
use creche_contracts::secret::Secret;

/// Who can read a token file, by its mode.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModeRule {
    /// `0600` or less: no bit for the group and no bit for each other user
    /// (contract 02 §3 rule 5).
    OwnerOnly,
    /// `0640` or less: the group can also read. The chaperone runs as
    /// another user and reads two token files of `attendance` (contract 02
    /// §3 rule 5, the exception).
    OwnerAndGroupRead,
    /// The reader does not check the mode.
    AnyMode,
}

impl fmt::Display for ModeRule {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::OwnerOnly => "0600",
            Self::OwnerAndGroupRead => "0600 or 0640",
            Self::AnyMode => "each mode",
        })
    }
}

/// Which space a reader removes from the two ends of the content.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trim {
    /// The six ASCII bytes that `bytes.strip` of Python removes: space, tab,
    /// line feed, carriage return, vertical tab and form feed.
    AsciiSpace,
    /// Each character that `str.strip` of Python removes:
    /// `creche_contracts::config::python_strip`.
    PythonStrip,
}

/// Which bytes a token can hold.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Encoding {
    /// Each byte.
    AnyBytes,
    /// Only UTF-8 text.
    Utf8,
}

/// The rules of one reader of a token file.
///
/// ```
/// use creche_runtime::token::{ModeRule, TokenRule};
///
/// assert_eq!(TokenRule::ATTENDANCE.min_bytes(), 32);
/// assert_eq!(TokenRule::ATTENDANCE.mode(), ModeRule::OwnerOnly);
/// assert_eq!(TokenRule::NOT_EMPTY.min_bytes(), 1);
/// ```
///
/// Code outside this module cannot build a rule. Each reader of the platform
/// has its constant, and a new reader gets a new constant here:
///
/// ```compile_fail,E0451
/// use creche_runtime::token::{Encoding, ModeRule, TokenRule, Trim};
///
/// let rule = TokenRule {
///     min_bytes: 0,
///     mode: ModeRule::AnyMode,
///     trim: Trim::AsciiSpace,
///     encoding: Encoding::AnyBytes,
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenRule {
    min_bytes: usize,
    mode: ModeRule,
    trim: Trim,
    encoding: Encoding,
}

impl TokenRule {
    /// A token of `attendance` that only `attendance` and its client read
    /// (`attendance/src/attendance/auth.py:242-265`): 32 bytes or more, mode
    /// `0600`, ASCII space removed, each byte.
    pub const ATTENDANCE: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::OwnerOnly,
        trim: Trim::AsciiSpace,
        encoding: Encoding::AnyBytes,
    };

    /// One of the two tokens of `attendance` that the chaperone reads
    /// (`attendance/src/attendance/auth.py:268-279`): as
    /// [`TokenRule::ATTENDANCE`], and the group can read the file.
    pub const ATTENDANCE_PEP_READ: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::OwnerAndGroupRead,
        trim: Trim::AsciiSpace,
        encoding: Encoding::AnyBytes,
    };

    /// The key or the token of a door
    /// (`door-owui/src/agent_door_owui/config.py:117-146`,
    /// `chaperone/src/chaperone/delegate.py:141-158`): 32 bytes or more, no
    /// mode check, `str.strip` of Python, UTF-8.
    pub const DOOR: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::AnyMode,
        trim: Trim::PythonStrip,
        encoding: Encoding::Utf8,
    };

    /// The token of a webhook
    /// (`door-trigger/src/agent_door_trigger/tokens.py:35-66`): 32 bytes or
    /// more, mode `0600`, ASCII space removed, then UTF-8.
    pub const WEBHOOK: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::OwnerOnly,
        trim: Trim::AsciiSpace,
        encoding: Encoding::Utf8,
    };

    /// A token that only must not be empty
    /// (`caregiver/src/caregiver/switch.py:80-94`): 1 byte or more, no mode
    /// check, `str.strip` of Python, UTF-8.
    pub const NOT_EMPTY: Self = Self {
        min_bytes: 1,
        mode: ModeRule::AnyMode,
        trim: Trim::PythonStrip,
        encoding: Encoding::Utf8,
    };

    /// The smallest count of bytes of the token, after the trim.
    #[must_use]
    pub const fn min_bytes(self) -> usize {
        self.min_bytes
    }

    /// Who can read the file.
    #[must_use]
    pub const fn mode(self) -> ModeRule {
        self.mode
    }

    /// Which space the reader removes.
    #[must_use]
    pub const fn trim(self) -> Trim {
        self.trim
    }

    /// Which bytes the token can hold.
    #[must_use]
    pub const fn encoding(self) -> Encoding {
        self.encoding
    }
}

/// Why a token file gives no token.
///
/// No variant holds the token, a part of it or the count of its bytes. A
/// caller writes this error to a log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TokenError {
    /// The file is absent, or the operating system refused the read.
    Unreadable {
        /// The text of the error, as `strerror` of Python gives it.
        os_text: String,
    },
    /// The file holds more than 1 MiB.
    TooLarge,
    /// The file holds no byte, or only space.
    Empty,
    /// The token has less bytes than the rule demands.
    TooShort {
        /// The smallest count of bytes of the rule.
        min_bytes: usize,
    },
    /// More users can read the file than the rule permits.
    ModeTooWide {
        /// The mode that the rule permits.
        allowed: ModeRule,
    },
    /// The content is not UTF-8, and the rule demands text.
    NotUtf8,
}

impl fmt::Display for TokenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unreadable { os_text } => write!(f, "the token file is not readable: {os_text}"),
            Self::TooLarge => f.write_str("the token file is too large"),
            Self::Empty => f.write_str("the token file is empty"),
            Self::TooShort { min_bytes } => {
                write!(f, "the token has less than {min_bytes} bytes")
            }
            Self::ModeTooWide { allowed } => {
                write!(f, "the mode of the token file is not {allowed}")
            }
            Self::NotUtf8 => f.write_str("the token is not UTF-8"),
        }
    }
}

impl Error for TokenError {}

/// Reads the token file at `path` under `rule`.
///
/// # Errors
///
/// [`TokenError`] with the first rule that the file breaks.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-token-faults writes this body"
)]
pub fn read(path: &Path, rule: TokenRule) -> Result<Secret, TokenError> {
    todo!()
}

/// A token file that the service reads again only after the file moved.
///
/// A token can rotate while the service runs, and the writer of the file can
/// start after its reader. This type reads at the time of the call. It
/// compares the facts of the file with the facts of its last read, and reads
/// the file only when a fact moved. A `stat` that fails never serves the
/// value of the last read.
///
/// The Python origin is `chaperone/src/chaperone/delegate.py:161-206`.
#[derive(Debug)]
pub struct CachedToken(());

impl CachedToken {
    /// A reader of the token file at `path` under `rule`. The call reads no
    /// file.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-token-faults writes this body"
    )]
    #[must_use]
    pub fn new(path: PathBuf, rule: TokenRule) -> Self {
        todo!()
    }

    /// The token of the file as it is now.
    ///
    /// # Errors
    ///
    /// [`TokenError`] when the file gives no token now, also when an earlier
    /// call gave one.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-token-faults writes this body"
    )]
    pub fn current(&mut self) -> Result<&Secret, TokenError> {
        todo!()
    }
}

/// How [`bearer_of`] treats the space around the value of the header.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BearerTrim {
    /// Remove the space, as `str.strip` of Python does. `attendance` and the
    /// trigger listener do this.
    PythonStrip,
    /// Keep the value as it is. `door-owui` does this.
    Exact,
}

/// The bearer of a request: the bytes after `Bearer ` in the `Authorization`
/// header. `None` for a request with no such header and for an empty value.
///
/// The result is the bytes that the Python service compares. Give them to
/// `Secret::matches`. The Python framework decodes a header as latin-1, and
/// each Python copy then encodes the text as UTF-8. A token with a byte above
/// 127 thus never matches, and this function keeps that.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-token-faults writes this body"
)]
#[must_use]
pub fn bearer_of(headers: &HeaderMap, trim: BearerTrim) -> Option<Vec<u8>> {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_constant_holds_the_rules_of_its_python_reader() {
        // The rule, then: the smallest count of bytes, the mode, the trim and
        // the encoding of the Python reader that the doc comment names.
        let table = [
            (
                TokenRule::ATTENDANCE,
                32,
                ModeRule::OwnerOnly,
                Trim::AsciiSpace,
                Encoding::AnyBytes,
            ),
            (
                TokenRule::ATTENDANCE_PEP_READ,
                32,
                ModeRule::OwnerAndGroupRead,
                Trim::AsciiSpace,
                Encoding::AnyBytes,
            ),
            (
                TokenRule::DOOR,
                32,
                ModeRule::AnyMode,
                Trim::PythonStrip,
                Encoding::Utf8,
            ),
            (
                TokenRule::WEBHOOK,
                32,
                ModeRule::OwnerOnly,
                Trim::AsciiSpace,
                Encoding::Utf8,
            ),
            (
                TokenRule::NOT_EMPTY,
                1,
                ModeRule::AnyMode,
                Trim::PythonStrip,
                Encoding::Utf8,
            ),
        ];

        for (rule, min_bytes, mode, trim, encoding) in table {
            assert_eq!(rule.min_bytes(), min_bytes);
            assert_eq!(rule.mode(), mode);
            assert_eq!(rule.trim(), trim);
            assert_eq!(rule.encoding(), encoding);
        }
    }

    #[test]
    fn the_five_constants_are_five_rules() {
        let rules = [
            TokenRule::ATTENDANCE,
            TokenRule::ATTENDANCE_PEP_READ,
            TokenRule::DOOR,
            TokenRule::WEBHOOK,
            TokenRule::NOT_EMPTY,
        ];

        for (at, rule) in rules.iter().enumerate() {
            for other in rules.iter().skip(at + 1) {
                assert_ne!(rule, other);
            }
        }
    }

    #[test]
    fn an_error_names_the_rule_and_no_count_of_the_bytes_of_the_token() {
        let table = [
            (
                TokenError::Unreadable {
                    os_text: String::from("No such file or directory"),
                },
                "the token file is not readable: No such file or directory",
            ),
            (TokenError::TooLarge, "the token file is too large"),
            (TokenError::Empty, "the token file is empty"),
            (
                TokenError::TooShort { min_bytes: 32 },
                "the token has less than 32 bytes",
            ),
            (
                TokenError::ModeTooWide {
                    allowed: ModeRule::OwnerOnly,
                },
                "the mode of the token file is not 0600",
            ),
            (
                TokenError::ModeTooWide {
                    allowed: ModeRule::OwnerAndGroupRead,
                },
                "the mode of the token file is not 0600 or 0640",
            ),
            (TokenError::NotUtf8, "the token is not UTF-8"),
        ];

        for (error, text) in table {
            assert_eq!(error.to_string(), text);
        }
    }
}
