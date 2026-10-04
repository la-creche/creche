//! Identifiers that cross a process boundary.
//!
//! Each identifier is one type with a private field. A parsing constructor is
//! the only way to a value. [`FamilyName`] is the pattern for every later
//! type: no `Default`, no public field, and `serde` reads it through the same
//! constructor.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize, Serializer};

/// The smallest count of bytes in a family name.
const FAMILY_NAME_MIN: usize = 2;

/// The largest count of bytes in a family name.
const FAMILY_NAME_MAX: usize = 31;

/// The name of one family: `[a-z][a-z0-9-]{1,30}` (contract 01 §2, contract 02
/// §2).
///
/// The grammar is ASCII, so one byte is one character. The check reads bytes
/// and uses no pattern engine: a `\d` in a Rust pattern also matches a digit
/// that is not ASCII.
///
/// The grammar accepts the reserved name `gate-probe`. The family file check
/// refuses that name (contract 01 §3.1). This type does not.
///
/// ```
/// use creche_contracts::ids::FamilyName;
///
/// let name: FamilyName = "chat".parse()?;
/// assert_eq!(name.as_str(), "chat");
/// # Ok::<(), creche_contracts::ids::FamilyNameError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::ids::FamilyName;
///
/// let name = FamilyName(String::from("chat"));
/// ```
///
/// A family name has no default value:
///
/// ```compile_fail,E0277
/// use creche_contracts::ids::FamilyName;
///
/// let name: FamilyName = Default::default();
/// ```
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Deserialize)]
#[serde(try_from = "String")]
pub struct FamilyName(String);

impl FamilyName {
    /// The name as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for FamilyName {
    type Err = FamilyNameError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        check_family_name(text)?;

        Ok(Self(text.to_owned()))
    }
}

impl TryFrom<String> for FamilyName {
    type Error = FamilyNameError;

    fn try_from(text: String) -> Result<Self, Self::Error> {
        check_family_name(&text)?;

        Ok(Self(text))
    }
}

impl fmt::Display for FamilyName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl Serialize for FamilyName {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0)
    }
}

/// Why a text is not a family name.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FamilyNameError {
    /// The text has fewer than 2 bytes.
    TooShort,
    /// The text has more than 31 bytes.
    TooLong,
    /// The first byte is not `a` to `z`.
    BadFirstByte,
    /// The byte at this offset, from 0, is not `a` to `z`, `0` to `9` or `-`.
    BadByte {
        /// The offset of the byte, from 0.
        at: usize,
    },
}

impl fmt::Display for FamilyNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooShort => write!(f, "a family name has {FAMILY_NAME_MIN} bytes or more"),
            Self::TooLong => write!(f, "a family name has {FAMILY_NAME_MAX} bytes or less"),
            Self::BadFirstByte => f.write_str("a family name starts with a to z"),
            Self::BadByte { at } => {
                write!(f, "byte {at} of a family name is not a to z, 0 to 9 or -")
            }
        }
    }
}

impl Error for FamilyNameError {}

/// Whether `byte` can follow the first byte of a family name.
const fn is_name_tail(byte: u8) -> bool {
    byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'
}

/// Checks `text` against the family name grammar, the size first.
fn check_family_name(text: &str) -> Result<(), FamilyNameError> {
    let bytes = text.as_bytes();
    if bytes.len() < FAMILY_NAME_MIN {
        return Err(FamilyNameError::TooShort);
    }

    if bytes.len() > FAMILY_NAME_MAX {
        return Err(FamilyNameError::TooLong);
    }

    if !bytes.first().is_some_and(u8::is_ascii_lowercase) {
        return Err(FamilyNameError::BadFirstByte);
    }

    let bad = bytes
        .iter()
        .enumerate()
        .skip(1)
        .find(|(_, byte)| !is_name_tail(**byte));
    if let Some((at, _)) = bad {
        return Err(FamilyNameError::BadByte { at });
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 31 bytes: the longest family name.
    const LONGEST: &str = "abcdefghijklmnopqrstuvwxyz01234";

    /// 32 bytes: one byte too long.
    const ONE_TOO_LONG: &str = "abcdefghijklmnopqrstuvwxyz012345";

    /// 16 characters and 32 bytes: the size rule counts bytes.
    const WIDE_AND_TOO_LONG: &str = "éééééééééééééééé";

    const ACCEPTED: &[&str] = &[
        "ab",
        "a0",
        "a-",
        "a--b",
        "chat",
        "night-watch",
        "reader-2",
        "gate-probe",
        LONGEST,
    ];

    const REFUSED: &[(&str, FamilyNameError)] = &[
        ("", FamilyNameError::TooShort),
        ("a", FamilyNameError::TooShort),
        ("-", FamilyNameError::TooShort),
        (ONE_TOO_LONG, FamilyNameError::TooLong),
        (WIDE_AND_TOO_LONG, FamilyNameError::TooLong),
        ("Ab", FamilyNameError::BadFirstByte),
        ("0a", FamilyNameError::BadFirstByte),
        ("-a", FamilyNameError::BadFirstByte),
        ("_a", FamilyNameError::BadFirstByte),
        (" ab", FamilyNameError::BadFirstByte),
        ("\nab", FamilyNameError::BadFirstByte),
        ("\u{e9}a", FamilyNameError::BadFirstByte),
        ("\u{ff41}b", FamilyNameError::BadFirstByte),
        ("aB", FamilyNameError::BadByte { at: 1 }),
        ("a_b", FamilyNameError::BadByte { at: 1 }),
        ("a.b", FamilyNameError::BadByte { at: 1 }),
        ("a/b", FamilyNameError::BadByte { at: 1 }),
        ("a b", FamilyNameError::BadByte { at: 1 }),
        ("ab ", FamilyNameError::BadByte { at: 2 }),
        ("ab\0", FamilyNameError::BadByte { at: 2 }),
        ("ab\r\n", FamilyNameError::BadByte { at: 2 }),
        ("a\u{e9}", FamilyNameError::BadByte { at: 1 }),
        ("chat__tool", FamilyNameError::BadByte { at: 4 }),
        ("../etc", FamilyNameError::BadFirstByte),
    ];

    #[test]
    fn a_name_in_the_grammar_parses() {
        for text in ACCEPTED {
            let name: FamilyName = text.parse().unwrap();

            assert_eq!(name.as_str(), *text);
            assert_eq!(name.to_string(), *text);
            assert_eq!(FamilyName::try_from((*text).to_owned()), Ok(name));
        }
    }

    #[test]
    fn a_text_outside_the_grammar_is_refused() {
        for (text, why) in REFUSED {
            assert_eq!(text.parse::<FamilyName>(), Err(*why), "{text:?}");
            assert_eq!(
                FamilyName::try_from((*text).to_owned()),
                Err(*why),
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_trailing_newline_is_refused() {
        assert_eq!(
            "chat\n".parse::<FamilyName>(),
            Err(FamilyNameError::BadByte { at: 4 })
        );
    }

    #[test]
    fn a_digit_that_is_not_ascii_is_refused() {
        // ARABIC-INDIC DIGIT THREE, FULLWIDTH DIGIT THREE, DEVANAGARI DIGIT THREE.
        for text in ["a\u{0663}", "a\u{ff13}", "a\u{0969}"] {
            assert_eq!(
                text.parse::<FamilyName>(),
                Err(FamilyNameError::BadByte { at: 1 })
            );
        }
    }

    #[test]
    fn a_valid_name_deserializes() {
        let name: FamilyName = serde_json::from_str(r#""chat""#).unwrap();

        assert_eq!(name.as_str(), "chat");
    }

    #[test]
    fn an_invalid_name_never_deserializes() {
        for (text, why) in REFUSED {
            let json = serde_json::to_string(text).unwrap();
            let error = serde_json::from_str::<FamilyName>(&json).unwrap_err();

            assert!(
                error.to_string().starts_with(&why.to_string()),
                "{text:?}: {error}"
            );
        }
    }

    #[test]
    fn a_value_that_is_not_a_string_never_deserializes() {
        for json in ["7", "null", "true", r#"["chat"]"#, r#"{"name": "chat"}"#] {
            assert!(serde_json::from_str::<FamilyName>(json).is_err(), "{json}");
        }
    }

    #[test]
    fn a_name_serializes_as_its_text() {
        let name: FamilyName = "night-watch".parse().unwrap();

        assert_eq!(serde_json::to_string(&name).unwrap(), r#""night-watch""#);
    }

    #[test]
    fn the_error_says_which_rule_failed() {
        let said: Vec<String> = [
            FamilyNameError::TooShort,
            FamilyNameError::TooLong,
            FamilyNameError::BadFirstByte,
            FamilyNameError::BadByte { at: 4 },
        ]
        .iter()
        .map(ToString::to_string)
        .collect();

        assert_eq!(
            said,
            [
                "a family name has 2 bytes or more",
                "a family name has 31 bytes or less",
                "a family name starts with a to z",
                "byte 4 of a family name is not a to z, 0 to 9 or -",
            ]
        );
    }

    #[test]
    fn the_error_is_a_std_error() {
        let error: Box<dyn Error + Send + Sync> = Box::new(FamilyNameError::TooShort);

        assert!(error.source().is_none());
    }
}
