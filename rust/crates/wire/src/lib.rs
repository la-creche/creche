//! The wire types of the VM owner. Each type here is the one definition of
//! a value that crosses a process boundary: between the vmm, the guest
//! init, caregiver and attendance.

use std::fmt;
use std::str::FromStr;

use serde::de::value::MapAccessDeserializer;
use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

#[cfg(test)]
mod schema;
#[cfg(test)]
mod vectors;

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

/// The length of a ULID.
const ULID_LEN: usize = 26;

/// Whether `text` is a ULID: 26 characters of upper-case Crockford base32,
/// with no I, L, O or U (contract 02 §2).
fn is_ulid(text: &str) -> bool {
    text.len() == ULID_LEN
        && text.chars().all(|character| {
            matches!(character, '0'..='9' | 'A'..='H' | 'J' | 'K' | 'M' | 'N' | 'P'..='T' | 'V'..='Z')
        })
}

/// Why a text is not a job id or a delegation id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UlidIdError {
    /// The text does not start with the prefix of its kind.
    Prefix,
    /// The text after the prefix is not a ULID.
    Ulid,
}

impl fmt::Display for UlidIdError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let reason = match self {
            Self::Prefix => "the id does not start with the prefix of its kind",
            Self::Ulid => "the id does not end with a ULID",
        };
        formatter.write_str(reason)
    }
}

impl std::error::Error for UlidIdError {}

/// A text id made of a fixed prefix and a ULID. On the wire it is a JSON
/// string, and a string of any other form is refused.
macro_rules! ulid_id {
    ($(#[$doc:meta])* $name:ident, $prefix:literal) => {
        $(#[$doc])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        #[cfg_attr(test, derive(schemars::JsonSchema))]
        #[cfg_attr(test, schemars(extend("pattern" = concat!("^", $prefix, "[0-9A-HJKMNP-TV-Z]{26}$"))))]
        pub struct $name(String);

        impl $name {
            /// The id as text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = UlidIdError;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                let ulid = text.strip_prefix($prefix).ok_or(UlidIdError::Prefix)?;
                if !is_ulid(ulid) {
                    return Err(UlidIdError::Ulid);
                }
                Ok(Self(text.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = UlidIdError;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                text.parse()
            }
        }

        impl From<$name> for String {
            fn from(id: $name) -> Self {
                id.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

ulid_id!(
    /// The id that attendance gives a thin job of the delegate door:
    /// `job-<ulid>` (contract 02 §2 and §12). Contract 02 calls it a session
    /// id. The `SessionId` of the job's row on the host session list is a
    /// separate value.
    JobId,
    "job-"
);

ulid_id!(
    /// The id that attendance gives an autonomous job of a trigger or of the
    /// dispatch door: `auto-<ulid>` (contract 02 §2 and §13). Contract 02 calls
    /// it a session id. The `SessionId` of the job's row on the host session
    /// list is a separate value.
    AutoJobId,
    "auto-"
);

ulid_id!(
    /// The id the PEP mints for one delegation, a ULID (contract 04 §7.4).
    DelegationId,
    ""
);

/// The `Serialize` and `Deserialize` of a struct on the wire. The struct
/// derives both with `#[serde(remote = "Self")]`, so the derived code is a
/// pair of inherent functions, and these trait impls call them.
///
/// The `Deserialize` runs the derived reader only on a JSON object. A
/// derived reader of a struct also reads a JSON array, its fields in order,
/// and `deny_unknown_fields` does not stop that. The schema and the
/// generated Python model take only an object (wire/AGENTS.md).
macro_rules! object_only {
    ($name:ident) => {
        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                $name::serialize(self, serializer)
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                struct ObjectVisitor;

                impl<'de> Visitor<'de> for ObjectVisitor {
                    type Value = $name;

                    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                        formatter.write_str(concat!("a JSON object for ", stringify!($name)))
                    }

                    fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<$name, A::Error> {
                        $name::deserialize(MapAccessDeserializer::new(map))
                    }
                }

                deserializer.deserialize_map(ObjectVisitor)
            }
        }
    };
}

/// The origin of a session that a door client opened. It carries nothing.
///
/// It is a type of its own so that the generated Python model is a class
/// with no field. An inline empty object generates a `dict` with lax keys.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct Door {}

object_only!(Door);

/// The origin of a session that the delegate door opened for one thin job.
///
/// It is a type of its own so that the generated Python class has a name
/// of its own. An inline struct variant gets the class name of its key.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct Delegate {
    /// The delegation that asked for the job.
    pub delegation: DelegationId,
    /// The id that attendance gives the job. The `SessionId` of the row is a
    /// separate value.
    pub job: JobId,
}

object_only!(Delegate);

/// Who made a session, kept on its row of the host session list (decision
/// CH21-4). On the wire the variant name is the one key of an object.
///
/// Each variant has a `title`. datamodel-codegen names a class of a `oneOf`
/// member by its title, and by its position without one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum SessionOrigin {
    /// A door client opened the session. No job owns it.
    #[cfg_attr(test, schemars(title = "DoorOrigin"))]
    Door(Door),
    /// The delegate door opened the session for one thin job.
    #[cfg_attr(test, schemars(title = "DelegateOrigin"))]
    Delegate(Delegate),
}

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
    fn the_schema_of_a_session_id_is_a_string_with_the_pattern() {
        let schema = serde_json::to_value(schemars::schema_for!(SessionId)).unwrap();

        assert_eq!(schema["type"], "string");
        assert_eq!(schema["pattern"], SESSION_ID_PATTERN);
    }
}
