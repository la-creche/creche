//! The wire types of the VM owner. Each type here is the one definition of
//! a value that crosses a process boundary: between the vmm, the guest
//! init, caregiver and attendance.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

/// The length of a session id: 32 hex digits and 4 hyphens.
const SESSION_ID_LEN: usize = 36;

/// The offsets of the 4 hyphens in a session id.
const HYPHENS: [usize; 4] = [8, 13, 18, 23];

/// The rule of `SessionId::from_str` as a regular expression, for the JSON
/// Schema. The generated Python model checks a session id with it.
#[cfg(test)]
const SESSION_ID_PATTERN: &str = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$";

/// The id of one session, and of the VM that serves it (decision CH1).
///
/// The host mints the id at `session/new`. The text is a UUID in its
/// lowercase hyphenated form, the form the facade mints today with
/// `randomUUID`. A value of this type is always in that form. On the wire it
/// is a JSON string, and a string of any other form is refused.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("pattern" = SESSION_ID_PATTERN)))]
pub struct SessionId(String);

/// Why a text is not a session id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionIdError {
    /// The text does not have 36 characters.
    Length,
    /// A character is not a lowercase hex digit, or a hyphen is not at
    /// offset 8, 13, 18 or 23.
    Shape,
}

impl SessionId {
    /// The id as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for SessionId {
    type Err = SessionIdError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.len() != SESSION_ID_LEN {
            return Err(SessionIdError::Length);
        }

        for (offset, character) in text.chars().enumerate() {
            let fits = if HYPHENS.contains(&offset) {
                character == '-'
            } else {
                matches!(character, '0'..='9' | 'a'..='f')
            };
            if !fits {
                return Err(SessionIdError::Shape);
            }
        }

        Ok(Self(text.to_owned()))
    }
}

impl TryFrom<String> for SessionId {
    type Error = SessionIdError;

    fn try_from(text: String) -> Result<Self, Self::Error> {
        text.parse()
    }
}

impl From<SessionId> for String {
    fn from(id: SessionId) -> Self {
        id.0
    }
}

impl fmt::Display for SessionId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl fmt::Display for SessionIdError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let reason = match self {
            Self::Length => "a session id has 36 characters",
            Self::Shape => "a session id is lowercase hex in the form 8-4-4-4-12",
        };
        formatter.write_str(reason)
    }
}

impl std::error::Error for SessionIdError {}

#[cfg(test)]
mod tests {
    use super::{SESSION_ID_PATTERN, SessionId, SessionIdError};

    const GOOD: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";

    #[test]
    fn a_lowercase_uuid_is_a_session_id() {
        let id: SessionId = GOOD.parse().unwrap();

        assert_eq!(id.as_str(), GOOD);
        assert_eq!(id.to_string(), GOOD);
    }

    #[test]
    fn a_text_of_the_wrong_length_is_refused() {
        for text in ["", "3f2b8c1e", &format!("{GOOD}0")] {
            assert_eq!(text.parse::<SessionId>(), Err(SessionIdError::Length));
        }
    }

    #[test]
    fn a_text_of_the_wrong_shape_is_refused() {
        for text in [
            "3F2B8C1E-9D4A-4E6B-8A7C-0B1D2E3F4A5B",
            "3f2b8c1e99d4a-4e6b-8a7c-0b1d2e3f4a5b",
            "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5g",
            "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a/b",
        ] {
            assert_eq!(text.parse::<SessionId>(), Err(SessionIdError::Shape));
        }
    }

    #[test]
    fn a_text_with_a_multibyte_character_is_refused() {
        let text = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4aé";

        assert!(text.parse::<SessionId>().is_err());
    }

    #[test]
    fn a_session_id_is_a_json_string_both_ways() {
        let json = format!("\"{GOOD}\"");
        let id: SessionId = serde_json::from_str(&json).unwrap();

        assert_eq!(id.as_str(), GOOD);
        assert_eq!(serde_json::to_string(&id).unwrap(), json);
    }

    #[test]
    fn a_json_string_of_the_wrong_form_is_refused() {
        for json in [
            "\"3F2B8C1E-9D4A-4E6B-8A7C-0B1D2E3F4A5B\"",
            "\"\"",
            "42",
            "null",
        ] {
            assert!(serde_json::from_str::<SessionId>(json).is_err(), "{json}");
        }
    }

    #[test]
    fn the_schema_of_a_session_id_is_a_string_with_the_pattern() {
        let schema = serde_json::to_value(schemars::schema_for!(SessionId)).unwrap();

        assert_eq!(schema["type"], "string");
        assert_eq!(schema["pattern"], SESSION_ID_PATTERN);
    }
}
