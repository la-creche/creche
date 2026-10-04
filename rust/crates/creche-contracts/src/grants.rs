//! The grant file, the call body and the approval body that the chaperone
//! reads, and the audit record (contract 04).
//!
//! | Type | What it is |
//! |---|---|
//! | [`GrantFile`] | The grant file of one family, version 2: read by the chaperone, written by the caregiver. |
//! | [`CallBody`], [`ApprovalBody`] | The two request bodies of the chaperone, with their HTTP statuses. |
//!
//! Each reader here does what the Python code does with the same bytes, and
//! each writer writes the same bytes. `vectors/data/chaperone` records the
//! Python behavior.
//!
//! The readers do not use `serde_json`. The Python code reads JSON that is not
//! strict, and it reports each issue of a document, not only the first.
//! [`Value`] is the document that the readers share.

/// Makes a text type with a cap on its count of characters, and its error
/// type. A character is one code point, as in the Python code.
macro_rules! bounded_text {
    (
        $(#[$attribute:meta])*
        $name:ident,
        $(#[$error_attribute:meta])*
        $error:ident,
        $noun:literal,
        $min:literal,
        $max:literal
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);

        impl $name {
            /// The text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }

            /// Checks `text` against the two caps of the type.
            fn check(text: &str) -> Result<(), $error> {
                let chars = text.chars().count();
                if chars < $min {
                    return Err($error::TooShort);
                }

                if chars > $max {
                    return Err($error::TooLong);
                }

                Ok(())
            }
        }

        impl std::str::FromStr for $name {
            type Err = $error;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                Self::check(text)?;

                Ok(Self(text.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = $error;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                Self::check(&text)?;

                Ok(Self(text))
            }
        }

        $(#[$error_attribute])*
        ///
        /// No variant holds the text. The text is untrusted, and a caller writes
        /// this error to a log.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum $error {
            /// The text has fewer characters than the type permits.
            TooShort,
            /// The text has more characters than the type permits.
            TooLong,
        }

        impl std::fmt::Display for $error {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                match self {
                    Self::TooShort if $min == 1 => write!(f, "{} has 1 character or more", $noun),
                    Self::TooShort => write!(f, "{} has {} characters or more", $noun, $min),
                    Self::TooLong => write!(f, "{} has {} characters or less", $noun, $max),
                }
            }
        }

        impl std::error::Error for $error {}

        impl From<$error> for $crate::grants::IssueKind {
            fn from(error: $error) -> Self {
                match error {
                    $error::TooShort => Self::TextTooShort { min: $min },
                    $error::TooLong => Self::TextTooLong { max: $max },
                }
            }
        }
    };
}

mod body;
mod file;
mod issue;
mod json;

pub use body::{
    ApprovalBody, Arguments, BODY_MAX_BYTES, BodyError, CallBody, CallTool, CallToolError, Verdict,
};
pub use file::{
    ActionName, ActionNameError, DEFAULT_MAX_INFLIGHT_DELEGATIONS, DEFAULT_MAX_OPEN_GATES,
    DEFAULT_PEP_RPM, GRANT_FILE_MAX_BYTES, GRANT_FILE_VERSION, GrantError, GrantFile,
    GrantFileError, GrantsRev, GrantsRevError, HaAllow, HaName, HaNameError, Limit, LimitError,
    Limits, ModelAlias, ModelAliasError, RawGrantFile, RawHaAllow, RawLimits, RawVerbFence,
    TokenDigests, Verb, VerbFence, VerbName, VerbNameError,
};
pub use issue::{Issue, IssueKind, NameError, Step};
pub use json::{Integer, JsonError, Map, Value};
