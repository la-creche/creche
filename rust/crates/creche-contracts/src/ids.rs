//! Identifiers that cross a process boundary.
//!
//! Each identifier is one type with a private field. A parsing constructor is
//! the only way to a value. [`FamilyName`] is the pattern for every later
//! type: no `Default`, no public field, and `serde` reads it through the same
//! constructor.
//!
//! Each grammar is ASCII, so one byte is one character. A check reads bytes
//! and uses no pattern engine: a `\d` in a Rust pattern also matches a digit
//! that is not ASCII.
//!
//! Most types are one run of bytes. `run_id!` makes such a type from one
//! `Run` constant, which is the whole grammar. A type with parts, for example
//! [`Tag`], holds its text and each part.
//!
//! The Python implementation holds more than one copy of most grammars.
//! The vectors of the `id.` surfaces record what each copy does. Where two
//! copies disagree, the type here takes the strictest copy, and a
//! `CONTRACT-QUESTION` comment marks the type. `rust/AGENTS.md` holds the rule.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize, Serializer};

// --- the classes of bytes ---

/// One class of ASCII bytes, and what an error text calls the class.
#[derive(Clone, Copy)]
struct ByteClass {
    holds: fn(u8) -> bool,
    said: &'static str,
}

const LOWER: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_lowercase(),
    said: "a to z",
};

const UPPER: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_uppercase(),
    said: "A to Z",
};

const LETTER: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_alphabetic(),
    said: "A to Z or a to z",
};

const DIGIT: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_digit(),
    said: "0 to 9",
};

const LETTER_OR_DIGIT: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_alphanumeric(),
    said: "A to Z, a to z or 0 to 9",
};

/// A byte that can follow the first byte of a name: no underscore, because a
/// call name is `<server>__<tool>` (contract 01 §2).
const NAME_TAIL: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-',
    said: "a to z, 0 to 9 or -",
};

/// A byte that is safe in one path segment.
const SEGMENT: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'),
    said: "A to Z, a to z, 0 to 9, ., _ or -",
};

const TOOL_TAIL: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'),
    said: "A to Z, a to z, 0 to 9, _ or -",
};

const ENV_TAIL: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_',
    said: "A to Z, 0 to 9 or _",
};

const SECRET_TAIL: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_',
    said: "a to z, 0 to 9 or _",
};

const PACKAGE_TAIL: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_alphanumeric() || byte == b'.',
    said: "A to Z, a to z, 0 to 9 or .",
};

/// Upper-case Crockford base32: no `I`, `L`, `O` or `U`.
const CROCKFORD: ByteClass = ByteClass {
    holds: |byte| {
        byte.is_ascii_digit()
            || (byte.is_ascii_uppercase() && !matches!(byte, b'I' | b'L' | b'O' | b'U'))
    },
    said: "0 to 9 or A to Z without I, L, O and U",
};

const LOWER_HEX: ByteClass = ByteClass {
    holds: |byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'),
    said: "0 to 9 or a to f",
};

// --- an id that is one run of bytes ---

/// The grammar of an id that is one run of ASCII bytes: a range for the count
/// of bytes, a class for the first byte and a class for each later byte.
struct Run {
    /// What an error text calls a value, with its article: `a family name`.
    noun: &'static str,
    /// The smallest count of bytes.
    min: usize,
    /// The largest count of bytes.
    max: usize,
    first: ByteClass,
    tail: ByteClass,
}

/// Which rule of a [`Run`] a text breaks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RunFault {
    TooShort,
    TooLong,
    BadFirstByte,
    BadByte { at: usize },
}

impl RunFault {
    /// Writes the rule of `run` that the text broke.
    fn describe(self, run: &Run, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Run {
            noun,
            min,
            max,
            first,
            tail,
        } = run;
        match self {
            Self::TooShort | Self::TooLong if min == max => write!(f, "{noun} has {min} bytes"),
            Self::TooShort if *min == 1 => write!(f, "{noun} has 1 byte or more"),
            Self::TooShort => write!(f, "{noun} has {min} bytes or more"),
            Self::TooLong => write!(f, "{noun} has {max} bytes or less"),
            Self::BadFirstByte => write!(f, "{noun} starts with {}", first.said),
            Self::BadByte { at } => write!(f, "byte {at} of {noun} is not {}", tail.said),
        }
    }
}

/// Checks `text` against `run`, the size first.
fn check_run(text: &str, run: &Run) -> Result<(), RunFault> {
    let bytes = text.as_bytes();
    if bytes.len() < run.min {
        return Err(RunFault::TooShort);
    }

    if bytes.len() > run.max {
        return Err(RunFault::TooLong);
    }

    if !bytes.first().is_some_and(|byte| (run.first.holds)(*byte)) {
        return Err(RunFault::BadFirstByte);
    }

    let bad = bytes
        .iter()
        .enumerate()
        .skip(1)
        .find(|(_, byte)| !(run.tail.holds)(**byte));
    if let Some((at, _)) = bad {
        return Err(RunFault::BadByte { at });
    }

    Ok(())
}

/// Gives an id type its text form: `Display` and `Serialize` write the text
/// that the parsing constructor took.
macro_rules! text_traits {
    ($name:ident) => {
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }

        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_str(self.as_str())
            }
        }
    };
}

/// Makes an id type that holds its text and nothing else. The type has one
/// private function `check`, and each constructor calls it.
macro_rules! text_id {
    ($(#[$attribute:meta])* $name:ident, $error:ident) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize)]
        #[serde(try_from = "String")]
        pub struct $name(String);

        impl $name {
            /// The id as text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
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

        text_traits!($name);
    };
}

/// Makes the error type of an id whose grammar is one [`Run`]. A grammar with
/// one more rule names it after the run: a variant and its text.
macro_rules! run_error {
    (
        $(#[$attribute:meta])*
        $error:ident for $run:ident
        $(, $(#[$extra_attribute:meta])* $extra:ident => $said:literal)*
    ) => {
        $(#[$attribute])*
        ///
        /// No variant holds the text. The text is untrusted, and a caller writes
        /// this error to a log.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum $error {
            /// The text has fewer bytes than the grammar permits.
            TooShort,
            /// The text has more bytes than the grammar permits.
            TooLong,
            /// The first byte is not in the class of a first byte.
            BadFirstByte,
            /// The byte at this offset, from 0, is not in the class of a later
            /// byte.
            BadByte {
                /// The offset of the byte, from 0.
                at: usize,
            },
            $(
                $(#[$extra_attribute])*
                $extra,
            )*
        }

        impl From<RunFault> for $error {
            fn from(fault: RunFault) -> Self {
                match fault {
                    RunFault::TooShort => Self::TooShort,
                    RunFault::TooLong => Self::TooLong,
                    RunFault::BadFirstByte => Self::BadFirstByte,
                    RunFault::BadByte { at } => Self::BadByte { at },
                }
            }
        }

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                let fault = match self {
                    Self::TooShort => RunFault::TooShort,
                    Self::TooLong => RunFault::TooLong,
                    Self::BadFirstByte => RunFault::BadFirstByte,
                    Self::BadByte { at } => RunFault::BadByte { at: *at },
                    $(Self::$extra => return f.write_str($said),)*
                };

                fault.describe(&$run, f)
            }
        }

        impl Error for $error {}
    };
}

/// Makes an id type whose whole grammar is one [`Run`], and its error type.
macro_rules! run_id {
    (
        $(#[$attribute:meta])*
        $name:ident,
        $(#[$error_attribute:meta])*
        $error:ident,
        $run:ident
    ) => {
        text_id! {
            $(#[$attribute])*
            $name,
            $error
        }

        impl $name {
            /// Checks `text` against the grammar of the type.
            fn check(text: &str) -> Result<(), $error> {
                check_run(text, &$run).map_err($error::from)
            }
        }

        run_error! {
            $(#[$error_attribute])*
            $error for $run
        }
    };
}

// --- the name grammar: five identifiers, one form ---

/// The grammar that a family, a server, a webhook, a skill and a component
/// share: `[a-z][a-z0-9-]{1,30}`.
const fn name_run(noun: &'static str) -> Run {
    Run {
        noun,
        min: 2,
        max: 31,
        first: LOWER,
        tail: NAME_TAIL,
    }
}

const FAMILY_NAME: Run = name_run("a family name");
const SERVER_NAME: Run = name_run("a server name");
const WEBHOOK_NAME: Run = name_run("a webhook name");
const SKILL_NAME: Run = name_run("a skill name");
const COMPONENT_NAME: Run = name_run("a component name");

run_id! {
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
    #[derive(PartialOrd, Ord)]
    FamilyName,
    /// Why a text is not a family name.
    FamilyNameError,
    FAMILY_NAME
}

run_id! {
    /// The name of one MCP server: `[a-z][a-z0-9-]{1,30}` (contract 01b §1).
    ///
    /// The name is also the directory of the server file in the registry.
    ///
    /// ```
    /// use creche_contracts::ids::ServerName;
    ///
    /// let name: ServerName = "web-search".parse()?;
    /// assert_eq!(name.as_str(), "web-search");
    /// # Ok::<(), creche_contracts::ids::ServerNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::ServerName;
    ///
    /// let name = ServerName(String::from("web-search"));
    /// ```
    #[derive(PartialOrd, Ord)]
    ServerName,
    /// Why a text is not a server name.
    ServerNameError,
    SERVER_NAME
}

run_id! {
    /// The name of one webhook of a family: `[a-z][a-z0-9-]{1,30}` (contract 01
    /// §3.13).
    ///
    /// ```
    /// use creche_contracts::ids::WebhookName;
    ///
    /// let name: WebhookName = "door-bell".parse()?;
    /// assert_eq!(name.as_str(), "door-bell");
    /// # Ok::<(), creche_contracts::ids::WebhookNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::WebhookName;
    ///
    /// let name = WebhookName(String::from("door-bell"));
    /// ```
    #[derive(PartialOrd, Ord)]
    WebhookName,
    /// Why a text is not a webhook name.
    WebhookNameError,
    WEBHOOK_NAME
}

run_id! {
    /// The name of one skill of a family: `[a-z][a-z0-9-]{1,30}` (contract 01
    /// §3.10).
    ///
    /// ```
    /// use creche_contracts::ids::SkillName;
    ///
    /// let name: SkillName = "weekly-report".parse()?;
    /// assert_eq!(name.as_str(), "weekly-report");
    /// # Ok::<(), creche_contracts::ids::SkillNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::SkillName;
    ///
    /// let name = SkillName(String::from("weekly-report"));
    /// ```
    #[derive(PartialOrd, Ord)]
    SkillName,
    /// Why a text is not a skill name.
    SkillNameError,
    SKILL_NAME
}

run_id! {
    /// The name of one component: `[a-z][a-z0-9-]{1,30}` (contract 06 §1, §8).
    ///
    /// ```
    /// use creche_contracts::ids::ComponentName;
    ///
    /// let name: ComponentName = "chaperone".parse()?;
    /// assert_eq!(name.as_str(), "chaperone");
    /// # Ok::<(), creche_contracts::ids::ComponentNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::ComponentName;
    ///
    /// let name = ComponentName(String::from("chaperone"));
    /// ```
    #[derive(PartialOrd, Ord)]
    ComponentName,
    /// Why a text is not a component name.
    ComponentNameError,
    COMPONENT_NAME
}

// --- sessions, turns and files ---

/// The largest count of bytes in a session id (contract 02 §2).
const SESSION_ID_MAX: usize = 128;

const SESSION_ID: Run = Run {
    noun: "a session id",
    min: 1,
    max: SESSION_ID_MAX,
    first: LETTER_OR_DIGIT,
    tail: SEGMENT,
};

run_id! {
    /// The id of one session: `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (contract 02 §2).
    ///
    /// A session id becomes the name of a directory. The first byte is a letter
    /// or a digit, so the id is never `.` or `..` and never starts with `-`.
    ///
    /// ```
    /// use creche_contracts::ids::SessionId;
    ///
    /// let id: SessionId = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK".parse()?;
    /// assert_eq!(id.as_str(), "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK");
    /// # Ok::<(), creche_contracts::ids::SessionIdError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::SessionId;
    ///
    /// let id = SessionId(String::from("../other-family"));
    /// ```
    #[derive(PartialOrd, Ord)]
    SessionId,
    /// Why a text is not a session id.
    SessionIdError,
    SESSION_ID
}

/// The count of bytes in a ULID.
const ULID_BYTES: usize = 26;

// CONTRACT-QUESTION: contract 02 §2 writes the ULID pattern with `$`. In a
// Python pattern, `$` also matches before a final newline. Each of the seven
// Python copies refuses a ULID with a final newline, and this type refuses
// it. A change to accept the newline costs every reader a strip of its own.
const ULID: Run = Run {
    noun: "a ULID",
    min: ULID_BYTES,
    max: ULID_BYTES,
    first: CROCKFORD,
    tail: CROCKFORD,
};

run_id! {
    /// A ULID: 26 bytes of upper-case Crockford base32, `[0-9A-HJKMNP-TV-Z]{26}`
    /// (contract 02 §2).
    ///
    /// A turn id, a delegation id, an outcome id and a release request id are
    /// ULIDs. The type checks the form. It does not check that the time part is
    /// in the 48 bits of a ULID.
    ///
    /// ```
    /// use creche_contracts::ids::Ulid;
    ///
    /// let id: Ulid = "01J9ZQ5V7Y8X4W3T2S1R0QPNMK".parse()?;
    /// assert_eq!(id.as_str(), "01J9ZQ5V7Y8X4W3T2S1R0QPNMK");
    /// # Ok::<(), creche_contracts::ids::UlidError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::Ulid;
    ///
    /// let id = Ulid(String::from("01J9ZQ5V7Y8X4W3T2S1R0QPNMK"));
    /// ```
    #[derive(PartialOrd, Ord)]
    Ulid,
    /// Why a text is not a ULID.
    UlidError,
    ULID
}

const ATTACHMENT_NAME: Run = Run {
    noun: "an attachment name",
    min: 1,
    max: 120,
    first: SEGMENT,
    tail: SEGMENT,
};

/// The two names that a file system reads as a directory.
const DOT_NAMES: [&str; 2] = [".", ".."];

text_id! {
    /// The name of one file in the inbox of a session: `[A-Za-z0-9._-]{1,120}`,
    /// and not `.` or `..` (contract 02 §5.4.1).
    ///
    /// The grammar permits a first byte of `.` or `-`, for example `.hidden` and
    /// `-rf`.
    ///
    /// ```
    /// use creche_contracts::ids::AttachmentName;
    ///
    /// let name: AttachmentName = "report-2.final_v1.pdf".parse()?;
    /// assert_eq!(name.as_str(), "report-2.final_v1.pdf");
    /// # Ok::<(), creche_contracts::ids::AttachmentNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::AttachmentName;
    ///
    /// let name = AttachmentName(String::from(".."));
    /// ```
    #[derive(PartialOrd, Ord)]
    AttachmentName,
    AttachmentNameError
}

impl AttachmentName {
    /// Checks `text` against the grammar of the type, the dot names first.
    fn check(text: &str) -> Result<(), AttachmentNameError> {
        if DOT_NAMES.contains(&text) {
            return Err(AttachmentNameError::DotName);
        }

        check_run(text, &ATTACHMENT_NAME).map_err(AttachmentNameError::from)
    }
}

run_error! {
    /// Why a text is not an attachment name.
    AttachmentNameError for ATTACHMENT_NAME,
    /// The text is `.` or `..`.
    DotName => "an attachment name is not . and not .."
}

// --- the Open WebUI door ---

/// What the Open WebUI door puts before a chat id to make a session id
/// (contract 02 §2).
const OWUI_SESSION_PREFIX: &str = "owui-";

/// The largest count of bytes in a chat id: what the prefix leaves of a
/// session id.
const OWUI_CHAT_ID_MAX: usize = SESSION_ID_MAX - OWUI_SESSION_PREFIX.len();

// CONTRACT-QUESTION: contract 02 §2 caps a session id at 128 bytes and gives
// no cap for a chat id. The Python door refuses a chat id of more than 123
// bytes, because the session id `owui-<chat id>` then has more than 128
// bytes. This type takes the same cap. A larger cap costs a session id that
// `attendance` refuses.
const OWUI_CHAT_ID: Run = Run {
    noun: "an Open WebUI chat id",
    min: 1,
    max: OWUI_CHAT_ID_MAX,
    first: LETTER_OR_DIGIT,
    tail: SEGMENT,
};

/// Whether `character` is white space that the Python door strips from a
/// header value.
///
/// The list is what Python's `str.strip` removes. It is the Unicode property
/// `White_Space` and the four separators U+001C to U+001F. The list is written
/// out, so a later Unicode version cannot change it.
const fn is_header_space(character: char) -> bool {
    matches!(
        character,
        '\t'..='\r'
            | '\u{1c}'..=' '
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

run_id! {
    /// The id of one chat of Open WebUI: the form of a session id, 1 to 123
    /// bytes (contract 02 §2, §10).
    ///
    /// The Open WebUI door reads the id from a request header and makes the
    /// session id `owui-<chat id>` from it.
    ///
    /// ```
    /// use creche_contracts::ids::OwuiChatId;
    ///
    /// let id = OwuiChatId::from_header(" 3f2b1c9e-8a55-4c1e-9f0a-2b6d7e8f9a12 ")?;
    /// assert_eq!(id.as_str(), "3f2b1c9e-8a55-4c1e-9f0a-2b6d7e8f9a12");
    /// # Ok::<(), creche_contracts::ids::OwuiChatIdError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::OwuiChatId;
    ///
    /// let id = OwuiChatId(String::from("../other-chat"));
    /// ```
    #[derive(PartialOrd, Ord)]
    OwuiChatId,
    /// Why a text is not an Open WebUI chat id.
    ///
    /// `TooShort` is the empty id. Open WebUI sends an empty header for a chat
    /// id that it does not have, so the door reports that case with its own
    /// code.
    OwuiChatIdError,
    OWUI_CHAT_ID
}

impl OwuiChatId {
    /// Reads the chat id from the value of the `X-OWUI-Chat-Id` header.
    ///
    /// The function removes white space from both ends first, as the Python
    /// door does. [`FromStr`] removes nothing.
    ///
    /// # Errors
    ///
    /// The error says which rule of the grammar the value breaks, after the
    /// function removed the white space.
    pub fn from_header(value: &str) -> Result<Self, OwuiChatIdError> {
        value.trim_matches(is_header_space).parse()
    }

    /// The session id of the chat: `owui-<chat id>` (contract 02 §2).
    ///
    /// # Errors
    ///
    /// The function checks the text as a session id. A chat id has 123 bytes
    /// or less and the form of a session id, so no value of this type gives
    /// an error.
    pub fn session_id(&self) -> Result<SessionId, SessionIdError> {
        SessionId::try_from(format!("{OWUI_SESSION_PREFIX}{}", self.0))
    }
}

/// What a model name of the Open WebUI door starts with.
const MODEL_PREFIX: &str = "agent:";

/// The model name that Open WebUI sends for one family: `agent:<family name>`
/// (`spec.md` §11.1, contract 02 §2).
///
/// ```
/// use creche_contracts::ids::OwuiModel;
///
/// let model: OwuiModel = "agent:chat".parse()?;
/// assert_eq!(model.as_str(), "agent:chat");
/// assert_eq!(model.family().as_str(), "chat");
/// # Ok::<(), creche_contracts::ids::OwuiModelError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::ids::OwuiModel;
///
/// let model = OwuiModel {
///     text: String::from("agent:chat"),
///     family: "chat".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize)]
#[serde(try_from = "String")]
pub struct OwuiModel {
    text: String,
    family: FamilyName,
}

impl OwuiModel {
    /// The model name as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The family that the model name selects.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }
}

impl FromStr for OwuiModel {
    type Err = OwuiModelError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let family = text
            .strip_prefix(MODEL_PREFIX)
            .ok_or(OwuiModelError::NoPrefix)?;
        let family = family.parse().map_err(OwuiModelError::Family)?;

        Ok(Self {
            text: text.to_owned(),
            family,
        })
    }
}

/// Why a text is not a model name of the Open WebUI door.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OwuiModelError {
    /// The text does not start with `agent:`.
    NoPrefix,
    /// The text after `agent:` is not a family name.
    Family(FamilyNameError),
}

impl fmt::Display for OwuiModelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoPrefix => write!(f, "a model name starts with {MODEL_PREFIX}"),
            Self::Family(error) => write!(f, "after {MODEL_PREFIX}, {error}"),
        }
    }
}

impl Error for OwuiModelError {}

// --- sandboxes and MCP servers ---

/// What stands between the family name and the number of a sandbox name.
const SANDBOX_SEPARATOR: &str = "-s";

/// The largest count of digits in the number of a sandbox name.
const SANDBOX_DIGITS_MAX: usize = 9;

/// The name of one sandbox: `<family name>-s<N>`, where `<N>` is 1 to 9 digits
/// (contract 05 §4.1).
///
/// The number can have a zero at its start, as in the Python implementation:
/// `chat-s01` and `chat-s1` are two names with the number 1.
///
/// ```
/// use creche_contracts::ids::SandboxName;
///
/// let name: SandboxName = "chat-s12".parse()?;
/// assert_eq!(name.as_str(), "chat-s12");
/// assert_eq!(name.family().as_str(), "chat");
/// assert_eq!(name.number(), 12);
/// # Ok::<(), creche_contracts::ids::SandboxNameError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::ids::SandboxName;
///
/// let name = SandboxName {
///     text: String::from("chat-s12"),
///     family: "chat".parse().unwrap(),
///     number: 12,
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize)]
#[serde(try_from = "String")]
pub struct SandboxName {
    text: String,
    family: FamilyName,
    number: u32,
}

impl SandboxName {
    /// The sandbox name as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The family that the sandbox runs.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The number of the sandbox in its family.
    #[must_use]
    pub fn number(&self) -> u32 {
        self.number
    }
}

impl FromStr for SandboxName {
    type Err = SandboxNameError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        // The number holds no `-`, so the last separator is the only place
        // where the text can split. A family name can hold the separator.
        let (family, digits) = text
            .rsplit_once(SANDBOX_SEPARATOR)
            .ok_or(SandboxNameError::NoNumber)?;
        if digits.is_empty() {
            return Err(SandboxNameError::NoNumber);
        }

        // `u32::from_str` also takes a `+`, so the digits get their own check.
        if digits.len() > SANDBOX_DIGITS_MAX || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(SandboxNameError::BadNumber);
        }

        let number = digits.parse().map_err(|_| SandboxNameError::BadNumber)?;
        let family = family.parse().map_err(SandboxNameError::Family)?;

        Ok(Self {
            text: text.to_owned(),
            family,
            number,
        })
    }
}

/// Why a text is not a sandbox name.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxNameError {
    /// The text has no `-s`, or nothing after the last `-s`.
    NoNumber,
    /// The text after the last `-s` is not 1 to 9 ASCII digits.
    BadNumber,
    /// The text before the last `-s` is not a family name.
    Family(FamilyNameError),
}

impl fmt::Display for SandboxNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoNumber => write!(
                f,
                "a sandbox name ends with {SANDBOX_SEPARATOR} and a number"
            ),
            Self::BadNumber => write!(
                f,
                "the number of a sandbox name has 1 to {SANDBOX_DIGITS_MAX} digits, each 0 to 9"
            ),
            Self::Family(error) => write!(f, "before {SANDBOX_SEPARATOR} and the number, {error}"),
        }
    }
}

impl Error for SandboxNameError {}

/// What the account name of an MCP server starts with.
const MCP_USER_PREFIX: &str = "mcp-";

/// The account that one MCP server runs as: `mcp-<server name>` (contract 01b
/// §9).
///
/// ```
/// use creche_contracts::ids::McpUser;
///
/// let user: McpUser = "mcp-web-search".parse()?;
/// assert_eq!(user.as_str(), "mcp-web-search");
/// assert_eq!(user.server().as_str(), "web-search");
/// # Ok::<(), creche_contracts::ids::McpUserError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::ids::McpUser;
///
/// let user = McpUser {
///     text: String::from("root"),
///     server: "web-search".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize)]
#[serde(try_from = "String")]
pub struct McpUser {
    text: String,
    server: ServerName,
}

impl McpUser {
    /// The account name as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The server that runs as this account.
    #[must_use]
    pub fn server(&self) -> &ServerName {
        &self.server
    }
}

impl FromStr for McpUser {
    type Err = McpUserError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let server = text
            .strip_prefix(MCP_USER_PREFIX)
            .ok_or(McpUserError::NoPrefix)?;
        let server = server.parse().map_err(McpUserError::Server)?;

        Ok(Self {
            text: text.to_owned(),
            server,
        })
    }
}

/// Why a text is not the account name of an MCP server.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum McpUserError {
    /// The text does not start with `mcp-`.
    NoPrefix,
    /// The text after `mcp-` is not a server name.
    Server(ServerNameError),
}

impl fmt::Display for McpUserError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoPrefix => write!(f, "an MCP account name starts with {MCP_USER_PREFIX}"),
            Self::Server(error) => write!(f, "after {MCP_USER_PREFIX}, {error}"),
        }
    }
}

impl Error for McpUserError {}

// CONTRACT-QUESTION: contract 01 §3.4 and contract 01b §5 give a tool name the
// form `[A-Za-z][A-Za-z0-9_-]*` and no cap. The three Python copies disagree:
// `agent_family.grammar.TOOL_NAME` has no cap, the grant file parser of the
// chaperone caps a name at 128 characters, and `handover.mcpserver` caps it at
// 64. This type takes the strictest copy: 64 bytes. A server file must pass
// `handover.mcpserver` before a tool exists, so no tool has a longer name. A
// change to a larger cap costs the one number `max` here.
const TOOL_NAME: Run = Run {
    noun: "a tool name",
    min: 1,
    max: 64,
    first: LETTER,
    tail: TOOL_TAIL,
};

run_id! {
    /// The name of one tool of an MCP server: `[A-Za-z][A-Za-z0-9_-]{0,63}`
    /// (contract 01 §3.4, contract 01b §5).
    ///
    /// The grammar permits an underscore, and two in sequence. A call name is
    /// `<server>__<tool>`. A server name holds no underscore, so the first `__`
    /// of a call name is where the tool name starts.
    ///
    /// ```
    /// use creche_contracts::ids::ToolName;
    ///
    /// let name: ToolName = "get_weather".parse()?;
    /// assert_eq!(name.as_str(), "get_weather");
    /// # Ok::<(), creche_contracts::ids::ToolNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::ToolName;
    ///
    /// let name = ToolName(String::from("get_weather"));
    /// ```
    #[derive(PartialOrd, Ord)]
    ToolName,
    /// Why a text is not a tool name.
    ToolNameError,
    TOOL_NAME
}

// CONTRACT-QUESTION: contract 01b §4.1 gives no grammar for the name of an
// environment variable. The two Python copies disagree:
// `agent_family.grammar.ENV_VAR_NAME` is `[A-Z][A-Z0-9_]*` with no cap, and
// `handover.mcpserver.ENV_NAME_RE` caps the name at 64 characters. This type
// takes the strictest copy: 64 bytes. A change to a larger cap costs the one
// number `max` here.
const ENV_NAME: Run = Run {
    noun: "an environment variable name",
    min: 1,
    max: 64,
    first: UPPER,
    tail: ENV_TAIL,
};

run_id! {
    /// The name of one environment variable of an MCP server:
    /// `[A-Z][A-Z0-9_]{0,63}` (contract 01b §4.1).
    ///
    /// ```
    /// use creche_contracts::ids::EnvName;
    ///
    /// let name: EnvName = "API_BASE_URL".parse()?;
    /// assert_eq!(name.as_str(), "API_BASE_URL");
    /// # Ok::<(), creche_contracts::ids::EnvNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::EnvName;
    ///
    /// let name = EnvName(String::from("API_BASE_URL"));
    /// ```
    #[derive(PartialOrd, Ord)]
    EnvName,
    /// Why a text is not the name of an environment variable.
    EnvNameError,
    ENV_NAME
}

const SECRET_NAME: Run = Run {
    noun: "a secret name",
    min: 2,
    max: 63,
    first: LOWER,
    tail: SECRET_TAIL,
};

run_id! {
    /// The name of one secret in the encrypted store: `[a-z][a-z0-9_]{1,62}`
    /// (contract 01b §4.1, contract 06 §8).
    ///
    /// The type holds the name. It never holds the value of the secret.
    ///
    /// ```
    /// use creche_contracts::ids::SecretName;
    ///
    /// let name: SecretName = "search_api_key".parse()?;
    /// assert_eq!(name.as_str(), "search_api_key");
    /// # Ok::<(), creche_contracts::ids::SecretNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::SecretName;
    ///
    /// let name = SecretName(String::from("search_api_key"));
    /// ```
    #[derive(PartialOrd, Ord)]
    SecretName,
    /// Why a text is not a secret name.
    SecretNameError,
    SECRET_NAME
}

// CONTRACT-QUESTION: contract 01b §3.1 says that the version of a package is
// exact and never a range. It gives no grammar. The two Python copies
// disagree: `agent_family.grammar.EXACT_VERSION` is `[0-9][0-9A-Za-z.+-]*`
// with no cap, and `handover.mcpserver.VERSION_RE` is `[0-9][0-9A-Za-z.]{0,63}`.
// This type takes the strictest copy: no `+`, no `-`, 64 bytes. A server file
// must pass both copies before a release installs the package. A change to
// permit `+` or `-` costs the one byte class `PACKAGE_TAIL` here.
const PACKAGE_VERSION: Run = Run {
    noun: "a package version",
    min: 1,
    max: 64,
    first: DIGIT,
    tail: PACKAGE_TAIL,
};

run_id! {
    /// The exact version of one package that a server file pins:
    /// `[0-9][0-9A-Za-z.]{0,63}` (contract 01b §3.1).
    ///
    /// The type has no order. The order of the text is not the order of the
    /// versions.
    ///
    /// ```
    /// use creche_contracts::ids::PackageVersion;
    ///
    /// let version: PackageVersion = "1.0.0rc1".parse()?;
    /// assert_eq!(version.as_str(), "1.0.0rc1");
    /// # Ok::<(), creche_contracts::ids::PackageVersionError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::PackageVersion;
    ///
    /// let version = PackageVersion(String::from(">=1.0"));
    /// ```
    PackageVersion,
    /// Why a text is not a package version.
    PackageVersionError,
    PACKAGE_VERSION
}

// --- digests ---

/// Makes the [`Run`] of an id that is a fixed count of lower-case hex bytes.
const fn hex_run(noun: &'static str, bytes: usize) -> Run {
    Run {
        noun,
        min: bytes,
        max: bytes,
        first: LOWER_HEX,
        tail: LOWER_HEX,
    }
}

const SHA256_HEX: Run = hex_run("a SHA-256 digest", 64);
const GIT_OBJECT_ID: Run = hex_run("a git object id", 40);
const GATE_ID: Run = hex_run("a gate id", 16);

run_id! {
    /// A SHA-256 digest as 64 lower-case hex bytes: `[0-9a-f]{64}` (contract 01b
    /// §3, contract 04 §1.2).
    ///
    /// The digest of a family token and the checksum of a release asset have
    /// this form. The type holds no `sha256:` prefix.
    ///
    /// ```
    /// use creche_contracts::ids::Sha256Hex;
    ///
    /// let text = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    /// let digest: Sha256Hex = text.parse()?;
    /// assert_eq!(digest.as_str(), text);
    /// # Ok::<(), creche_contracts::ids::Sha256HexError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::Sha256Hex;
    ///
    /// let digest = Sha256Hex(String::from("0123"));
    /// ```
    #[derive(PartialOrd, Ord)]
    Sha256Hex,
    /// Why a text is not a SHA-256 digest in lower-case hex.
    Sha256HexError,
    SHA256_HEX
}

run_id! {
    /// The id of one git object as 40 lower-case hex bytes: `[0-9a-f]{40}`
    /// (contract 06 §9).
    ///
    /// ```
    /// use creche_contracts::ids::GitObjectId;
    ///
    /// let text = "0123456789abcdef0123456789abcdef01234567";
    /// let id: GitObjectId = text.parse()?;
    /// assert_eq!(id.as_str(), text);
    /// # Ok::<(), creche_contracts::ids::GitObjectIdError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::GitObjectId;
    ///
    /// let id = GitObjectId(String::from("HEAD"));
    /// ```
    #[derive(PartialOrd, Ord)]
    GitObjectId,
    /// Why a text is not a git object id.
    GitObjectIdError,
    GIT_OBJECT_ID
}

run_id! {
    /// The id of one approval gate as 16 lower-case hex bytes: `[0-9a-f]{16}`
    /// (contract 04 §8.2).
    ///
    /// ```
    /// use creche_contracts::ids::GateId;
    ///
    /// let id: GateId = "0123456789abcdef".parse()?;
    /// assert_eq!(id.as_str(), "0123456789abcdef");
    /// # Ok::<(), creche_contracts::ids::GateIdError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::GateId;
    ///
    /// let id = GateId(String::from("0123456789abcdef"));
    /// ```
    #[derive(PartialOrd, Ord)]
    GateId,
    /// Why a text is not a gate id.
    GateIdError,
    GATE_ID
}

// --- versions and tags ---

/// What stands between two numbers of a version.
const VERSION_SEPARATOR: char = '.';

// CONTRACT-QUESTION: contract 06 §2 and §3 give no cap on the count of digits
// of a number. The Python implementation reads each number as an integer, and
// Python reads a text of 4300 digits at most as an integer. This cap is that
// count, so `Version`, `ContractVersion` and `Tag` accept no number that the
// Python implementation cannot read. A smaller cap, for example one that fits
// `u64`, refuses a version that each Python copy accepts today.
/// The largest count of digits of one number of a version.
const NUMBER_DIGITS_MAX: usize = 4300;

/// The grammar of a version: a fixed count of numbers with `.` between them.
/// A number is one ASCII digit or more, and [`NUMBER_DIGITS_MAX`] at most.
struct Dotted {
    /// What an error text calls a value, with its article: `a version`.
    noun: &'static str,
    /// The count of numbers.
    numbers: usize,
}

/// Which rule of a [`Dotted`] a text breaks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DottedFault {
    BadByte { at: usize },
    WrongCount,
    EmptyNumber,
    LongNumber,
}

/// Checks `text` against `dotted`, the bytes first.
fn check_dotted(text: &str, dotted: &Dotted) -> Result<(), DottedFault> {
    // The offset of a character is a count of bytes.
    let bad = text
        .char_indices()
        .find(|(_, character)| !character.is_ascii_digit() && *character != VERSION_SEPARATOR);
    if let Some((at, _)) = bad {
        return Err(DottedFault::BadByte { at });
    }

    if text.split(VERSION_SEPARATOR).count() != dotted.numbers {
        return Err(DottedFault::WrongCount);
    }

    if text.split(VERSION_SEPARATOR).any(str::is_empty) {
        return Err(DottedFault::EmptyNumber);
    }

    // Each byte is a digit or the separator, so the count of bytes of a number
    // is its count of digits.
    let longest = text.split(VERSION_SEPARATOR).map(str::len).max();
    if longest.is_some_and(|digits| digits > NUMBER_DIGITS_MAX) {
        return Err(DottedFault::LongNumber);
    }

    Ok(())
}

/// Makes an id type whose whole grammar is one [`Dotted`], and its error type.
macro_rules! dotted_id {
    (
        $(#[$attribute:meta])*
        $name:ident,
        $(#[$error_attribute:meta])*
        $error:ident,
        $dotted:ident
    ) => {
        text_id! {
            $(#[$attribute])*
            $name,
            $error
        }

        impl $name {
            /// Checks `text` against the grammar of the type.
            fn check(text: &str) -> Result<(), $error> {
                check_dotted(text, &$dotted).map_err($error::from)
            }
        }

        $(#[$error_attribute])*
        ///
        /// No variant holds the text. The text is untrusted, and a caller writes
        /// this error to a log.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum $error {
            /// The byte at this offset, from 0, is not `0` to `9` or `.`.
            BadByte {
                /// The offset of the byte, from 0.
                at: usize,
            },
            /// The text has more numbers or fewer numbers than the grammar
            /// permits.
            WrongCount,
            /// A number of the text has no digit.
            EmptyNumber,
            /// A number of the text has more digits than the grammar permits.
            LongNumber,
        }

        impl From<DottedFault> for $error {
            fn from(fault: DottedFault) -> Self {
                match fault {
                    DottedFault::BadByte { at } => Self::BadByte { at },
                    DottedFault::WrongCount => Self::WrongCount,
                    DottedFault::EmptyNumber => Self::EmptyNumber,
                    DottedFault::LongNumber => Self::LongNumber,
                }
            }
        }

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                let Dotted { noun, numbers } = $dotted;
                match self {
                    Self::BadByte { at } => write!(f, "byte {at} of {noun} is not 0 to 9 or ."),
                    Self::WrongCount => write!(f, "{noun} has {numbers} numbers"),
                    Self::EmptyNumber => write!(f, "each number of {noun} has 1 digit or more"),
                    Self::LongNumber => write!(
                        f,
                        "each number of {noun} has {NUMBER_DIGITS_MAX} digits or less"
                    ),
                }
            }
        }

        impl Error for $error {}
    };
}

const VERSION: Dotted = Dotted {
    noun: "a version",
    numbers: 3,
};

const CONTRACT_VERSION: Dotted = Dotted {
    noun: "a contract version",
    numbers: 2,
};

dotted_id! {
    /// The version of one component: `MAJOR.MINOR.PATCH`, three numbers of
    /// ASCII digits (contract 06 §2).
    ///
    /// A number has 4300 digits or less. The grammar permits a zero at the
    /// start of a number, as the Python implementation does. The type has no
    /// order. The order of the text is not the order of the versions.
    ///
    /// ```
    /// use creche_contracts::ids::Version;
    ///
    /// let version: Version = "1.12.0".parse()?;
    /// assert_eq!(version.as_str(), "1.12.0");
    /// # Ok::<(), creche_contracts::ids::VersionError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::Version;
    ///
    /// let version = Version(String::from("latest"));
    /// ```
    Version,
    /// Why a text is not a version.
    VersionError,
    VERSION
}

impl Version {
    /// The same version with no zero at the start of a number: `01.02.03`
    /// gives `1.2.3`.
    ///
    /// The Python implementation reads each number of a tag as an integer and
    /// writes the version again in this form.
    #[must_use]
    pub fn normalized(&self) -> Self {
        let mut text = String::with_capacity(self.0.len());
        for (index, number) in self.0.split(VERSION_SEPARATOR).enumerate() {
            if index > 0 {
                text.push(VERSION_SEPARATOR);
            }

            text.push_str(match number.trim_start_matches('0') {
                "" => "0",
                digits => digits,
            });
        }

        Self(text)
    }
}

dotted_id! {
    /// The version of one contract: `MAJOR.MINOR`, two numbers of ASCII digits
    /// (contract 06 §3, §8).
    ///
    /// A number has 4300 digits or less. The grammar permits a zero at the
    /// start of a number, as the Python implementation does. The type has no
    /// order.
    ///
    /// ```
    /// use creche_contracts::ids::ContractVersion;
    ///
    /// let version: ContractVersion = "0.6".parse()?;
    /// assert_eq!(version.as_str(), "0.6");
    /// # Ok::<(), creche_contracts::ids::ContractVersionError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::ids::ContractVersion;
    ///
    /// let version = ContractVersion(String::from("1"));
    /// ```
    ContractVersion,
    /// Why a text is not a contract version.
    ContractVersionError,
    CONTRACT_VERSION
}

/// What stands between the component name and the version of a tag.
const TAG_SEPARATOR: &str = "-v";

/// The release tag of one component: `<component name>-v<version>` (contract
/// 06 §2).
///
/// A component name can hold `-v`. The version cannot, so the last `-v` of the
/// text is where the version starts.
///
/// ```
/// use creche_contracts::ids::Tag;
///
/// let tag: Tag = "chaperone-v1.2.3".parse()?;
/// assert_eq!(tag.as_str(), "chaperone-v1.2.3");
/// assert_eq!(tag.component().as_str(), "chaperone");
/// assert_eq!(tag.version().as_str(), "1.2.3");
/// # Ok::<(), creche_contracts::ids::TagError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::ids::Tag;
///
/// let tag = Tag {
///     text: String::from("chaperone-v1.2.3"),
///     component: "chaperone".parse().unwrap(),
///     version: "9.9.9".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize)]
#[serde(try_from = "String")]
pub struct Tag {
    text: String,
    component: ComponentName,
    version: Version,
}

impl Tag {
    /// The tag as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The component that the tag releases.
    #[must_use]
    pub fn component(&self) -> &ComponentName {
        &self.component
    }

    /// The version that the tag gives the component, as the tag writes it.
    #[must_use]
    pub fn version(&self) -> &Version {
        &self.version
    }
}

impl FromStr for Tag {
    type Err = TagError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let (component, version) = text.rsplit_once(TAG_SEPARATOR).ok_or(TagError::NoVersion)?;
        let version = version.parse().map_err(TagError::Version)?;
        let component = component.parse().map_err(TagError::Component)?;

        Ok(Self {
            text: text.to_owned(),
            component,
            version,
        })
    }
}

/// Why a text is not a release tag.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TagError {
    /// The text has no `-v`.
    NoVersion,
    /// The text after the last `-v` is not a version.
    Version(VersionError),
    /// The text before the last `-v` is not a component name.
    Component(ComponentNameError),
}

impl fmt::Display for TagError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoVersion => write!(f, "a tag holds {TAG_SEPARATOR} and a version"),
            Self::Version(error) => write!(f, "after the last {TAG_SEPARATOR} of a tag, {error}"),
            Self::Component(error) => {
                write!(f, "before the last {TAG_SEPARATOR} of a tag, {error}")
            }
        }
    }
}

impl Error for TagError {}

/// Gives an id type with parts its text form and its `TryFrom<String>`. The
/// type has its own `FromStr` and its own `as_str`.
macro_rules! parts_traits {
    ($($name:ident),+) => {
        $(
            impl TryFrom<String> for $name {
                type Error = <Self as FromStr>::Err;

                fn try_from(text: String) -> Result<Self, Self::Error> {
                    text.parse()
                }
            }

            text_traits!($name);
        )+
    };
}

parts_traits!(OwuiModel, SandboxName, McpUser, Tag);

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

    // --- each later type: one table of accepted texts, one of refused texts ---

    mod tables {
        use serde::de::DeserializeOwned;

        use super::super::*;
        use super::{LONGEST, ONE_TOO_LONG};

        /// A ULID that every Python copy accepts.
        const A_ULID: &str = "01J9ZQ5V7Y8X4W3T2S1R0QPNMK";

        /// 64 bytes of lower-case hex.
        const HEX_64: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

        /// 40 bytes of lower-case hex.
        const HEX_40: &str = "0123456789abcdef0123456789abcdef01234567";

        /// 64 bytes: the longest tool name and the longest package version.
        const LETTERS_64: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

        /// Checks both tables of one type through each constructor and through
        /// `serde`.
        fn tables_hold<T>(accepted: &[&str], refused: &[(&str, T::Err)])
        where
            T: FromStr + TryFrom<String, Error = <T as FromStr>::Err>,
            T: fmt::Display + fmt::Debug + PartialEq + Serialize + DeserializeOwned,
            T::Err: fmt::Debug + fmt::Display + PartialEq,
        {
            for text in accepted {
                let id: T = match text.parse() {
                    Ok(id) => id,
                    Err(why) => panic!("{text:?}: {why}"),
                };
                let json = serde_json::to_string(&id).unwrap();

                assert_eq!(id.to_string(), *text);
                assert_eq!(T::try_from((*text).to_owned()).as_ref(), Ok(&id));
                assert_eq!(json, serde_json::to_string(text).unwrap());
                assert_eq!(serde_json::from_str::<T>(&json).unwrap(), id);
            }

            for (text, why) in refused {
                let json = serde_json::to_string(text).unwrap();
                let error = serde_json::from_str::<T>(&json).unwrap_err();

                assert_eq!(text.parse::<T>().as_ref(), Err(why), "{text:?}");
                assert_eq!(
                    T::try_from((*text).to_owned()).as_ref(),
                    Err(why),
                    "{text:?}"
                );
                assert!(
                    error.to_string().starts_with(&why.to_string()),
                    "{text:?}: {error}"
                );
            }
        }

        /// A table of refused texts, with the error type of one id type.
        fn faults<'a, E: From<RunFault>>(rows: &[(&'a str, RunFault)]) -> Vec<(&'a str, E)> {
            rows.iter()
                .map(|(text, fault)| (*text, E::from(*fault)))
                .collect()
        }

        const NAMES: &[&str] = &["ab", "a0", "a-", "a--b", "web-search", "reader-2", LONGEST];

        const NOT_NAMES: &[(&str, RunFault)] = &[
            ("", RunFault::TooShort),
            ("a", RunFault::TooShort),
            (ONE_TOO_LONG, RunFault::TooLong),
            ("Ab", RunFault::BadFirstByte),
            ("1a", RunFault::BadFirstByte),
            ("-a", RunFault::BadFirstByte),
            ("a_b", RunFault::BadByte { at: 1 }),
            ("a.b", RunFault::BadByte { at: 1 }),
            ("ab\n", RunFault::BadByte { at: 2 }),
            ("a\u{0663}", RunFault::BadByte { at: 1 }),
        ];

        #[test]
        fn a_server_name_has_the_name_grammar() {
            tables_hold::<ServerName>(NAMES, &faults(NOT_NAMES));
        }

        #[test]
        fn a_webhook_name_has_the_name_grammar() {
            tables_hold::<WebhookName>(NAMES, &faults(NOT_NAMES));
        }

        #[test]
        fn a_skill_name_has_the_name_grammar() {
            tables_hold::<SkillName>(NAMES, &faults(NOT_NAMES));
        }

        #[test]
        fn a_component_name_has_the_name_grammar() {
            tables_hold::<ComponentName>(NAMES, &faults(NOT_NAMES));
        }

        /// The form that a session id and an Open WebUI chat id share.
        const SESSION_FORMS: &[&str] = &[
            "a",
            "0",
            "A.b_c-d",
            "a..b",
            "a.",
            "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK",
            "owui-3f2b1c9e-8a55-4c1e-9f0a-2b6d7e8f9a10",
        ];

        const NOT_SESSION_FORMS: &[(&str, RunFault)] = &[
            ("", RunFault::TooShort),
            (".", RunFault::BadFirstByte),
            ("..", RunFault::BadFirstByte),
            (".hidden", RunFault::BadFirstByte),
            ("-x", RunFault::BadFirstByte),
            ("_x", RunFault::BadFirstByte),
            ("a/b", RunFault::BadByte { at: 1 }),
            ("a\\b", RunFault::BadByte { at: 1 }),
            ("a b", RunFault::BadByte { at: 1 }),
            ("a:b", RunFault::BadByte { at: 1 }),
            ("a\0", RunFault::BadByte { at: 1 }),
            ("a\n", RunFault::BadByte { at: 1 }),
            ("a\u{0663}", RunFault::BadByte { at: 1 }),
            ("\u{e4}", RunFault::BadFirstByte),
        ];

        #[test]
        fn a_session_id_is_one_safe_path_segment() {
            let longest = "a".repeat(SESSION_ID_MAX);
            let too_long = "a".repeat(SESSION_ID_MAX + 1);

            tables_hold::<SessionId>(SESSION_FORMS, &faults(NOT_SESSION_FORMS));
            tables_hold::<SessionId>(&[&longest], &[(too_long.as_str(), SessionIdError::TooLong)]);
        }

        #[test]
        fn an_owui_chat_id_has_the_form_of_a_session_id() {
            let longest = "a".repeat(OWUI_CHAT_ID_MAX);
            let too_long = "a".repeat(OWUI_CHAT_ID_MAX + 1);

            tables_hold::<OwuiChatId>(SESSION_FORMS, &faults(NOT_SESSION_FORMS));
            tables_hold::<OwuiChatId>(
                &[&longest],
                &[(too_long.as_str(), OwuiChatIdError::TooLong)],
            );
        }

        #[test]
        fn a_chat_id_from_a_header_has_no_white_space_at_its_ends() {
            // U+001F is white space for Python and not for `str::trim`.
            for value in [
                "a1",
                " a1",
                "a1 ",
                "\ta1\r\n",
                "\u{1f}a1",
                "a1\u{a0}",
                "\u{2028}a1\u{3000}",
            ] {
                assert_eq!(
                    OwuiChatId::from_header(value).unwrap().as_str(),
                    "a1",
                    "{value:?}"
                );
            }

            assert_eq!(OwuiChatId::from_header(""), Err(OwuiChatIdError::TooShort));
            assert_eq!(
                OwuiChatId::from_header(" \t\r\n"),
                Err(OwuiChatIdError::TooShort)
            );
            assert_eq!(
                OwuiChatId::from_header("a 1"),
                Err(OwuiChatIdError::BadByte { at: 1 })
            );
            // ZERO WIDTH SPACE is not white space, for Python and for Rust.
            assert_eq!(
                OwuiChatId::from_header("a1\u{200b}"),
                Err(OwuiChatIdError::BadByte { at: 2 })
            );
            assert_eq!(
                " a1".parse::<OwuiChatId>(),
                Err(OwuiChatIdError::BadFirstByte)
            );
        }

        #[test]
        fn the_longest_chat_id_makes_the_longest_session_id() {
            let longest = "a".repeat(OWUI_CHAT_ID_MAX);
            let chat: OwuiChatId = "3f2b1c9e".parse().unwrap();
            let session = longest.parse::<OwuiChatId>().unwrap().session_id();

            assert_eq!(chat.session_id().unwrap().as_str(), "owui-3f2b1c9e");
            assert_eq!(session.unwrap().as_str(), format!("owui-{longest}"));
            assert_eq!(OWUI_SESSION_PREFIX.len() + longest.len(), SESSION_ID_MAX);
        }

        #[test]
        fn a_ulid_is_26_bytes_of_crockford_base32() {
            let zeros = "0".repeat(ULID_BYTES);
            let largest_time = format!("7{}", "Z".repeat(ULID_BYTES - 1));

            tables_hold::<Ulid>(
                &[A_ULID, &zeros, &largest_time],
                &faults(&[
                    ("", RunFault::TooShort),
                    ("01J9ZQ5V7Y8X4W3T2S1R0QPNM", RunFault::TooShort),
                    ("01J9ZQ5V7Y8X4W3T2S1R0QPNMK0", RunFault::TooLong),
                    ("01J9ZQ5V7Y8X4W3T2S1R0QPNMK\n", RunFault::TooLong),
                    ("01J9ZQ5V7Y8X4W3T2S1R0QPNM\n", RunFault::BadByte { at: 25 }),
                    ("I1J9ZQ5V7Y8X4W3T2S1R0QPNMK", RunFault::BadFirstByte),
                    ("0LJ9ZQ5V7Y8X4W3T2S1R0QPNMK", RunFault::BadByte { at: 1 }),
                    ("01O9ZQ5V7Y8X4W3T2S1R0QPNMK", RunFault::BadByte { at: 2 }),
                    ("01JUZQ5V7Y8X4W3T2S1R0QPNMK", RunFault::BadByte { at: 3 }),
                    ("01j9zq5v7y8x4w3t2s1r0qpnmk", RunFault::BadByte { at: 2 }),
                    (
                        "01J9ZQ5V7Y8X4W3T2S1R0QPN\u{0663}",
                        RunFault::BadByte { at: 24 },
                    ),
                ]),
            );
        }

        #[test]
        fn an_attachment_name_is_one_file_name_and_no_dot_name() {
            let longest = "a".repeat(120);
            let too_long = "a".repeat(121);
            let mut refused: Vec<(&str, AttachmentNameError)> = faults(&[
                ("", RunFault::TooShort),
                (&too_long, RunFault::TooLong),
                ("/a", RunFault::BadFirstByte),
                ("a/b", RunFault::BadByte { at: 1 }),
                ("a\\b", RunFault::BadByte { at: 1 }),
                ("a b", RunFault::BadByte { at: 1 }),
                ("a.txt\n", RunFault::BadByte { at: 5 }),
                ("a\u{0663}", RunFault::BadByte { at: 1 }),
            ]);
            refused.push((".", AttachmentNameError::DotName));
            refused.push(("..", AttachmentNameError::DotName));

            tables_hold::<AttachmentName>(
                &[
                    "a",
                    "report-2.final_v1.pdf",
                    ".hidden",
                    "...",
                    "-rf",
                    &longest,
                ],
                &refused,
            );
        }

        #[test]
        fn a_tool_name_starts_with_a_letter_and_has_64_bytes_or_less() {
            let too_long = format!("{LETTERS_64}a");

            tables_hold::<ToolName>(
                &[
                    "a",
                    "A",
                    "get_weather",
                    "list-items",
                    "Search",
                    "a__b",
                    LETTERS_64,
                ],
                &faults(&[
                    ("", RunFault::TooShort),
                    (&too_long, RunFault::TooLong),
                    ("1tool", RunFault::BadFirstByte),
                    ("_tool", RunFault::BadFirstByte),
                    ("-tool", RunFault::BadFirstByte),
                    ("*", RunFault::BadFirstByte),
                    ("tool.name", RunFault::BadByte { at: 4 }),
                    ("tool name", RunFault::BadByte { at: 4 }),
                    ("a\n", RunFault::BadByte { at: 1 }),
                    ("a\u{0663}", RunFault::BadByte { at: 1 }),
                ]),
            );
        }

        #[test]
        fn an_env_name_is_upper_case_and_has_64_bytes_or_less() {
            let longest = "A".repeat(64);
            let too_long = "A".repeat(65);

            tables_hold::<EnvName>(
                &["A", "HOME", "A_", "A1", "API_BASE_URL", &longest],
                &faults(&[
                    ("", RunFault::TooShort),
                    (&too_long, RunFault::TooLong),
                    ("a", RunFault::BadFirstByte),
                    ("1A", RunFault::BadFirstByte),
                    ("_A", RunFault::BadFirstByte),
                    ("Api", RunFault::BadByte { at: 1 }),
                    ("A-B", RunFault::BadByte { at: 1 }),
                    ("A=B", RunFault::BadByte { at: 1 }),
                    ("A\n", RunFault::BadByte { at: 1 }),
                    ("A\u{0663}", RunFault::BadByte { at: 1 }),
                ]),
            );
        }

        #[test]
        fn a_secret_name_is_lower_case_with_underscores() {
            let longest = "a".repeat(63);
            let too_long = "a".repeat(64);

            tables_hold::<SecretName>(
                &["ab", "a_", "a1", "search_api_key", &longest],
                &faults(&[
                    ("", RunFault::TooShort),
                    ("a", RunFault::TooShort),
                    (&too_long, RunFault::TooLong),
                    ("A_b", RunFault::BadFirstByte),
                    ("1ab", RunFault::BadFirstByte),
                    ("_ab", RunFault::BadFirstByte),
                    ("a-b", RunFault::BadByte { at: 1 }),
                    ("a.b", RunFault::BadByte { at: 1 }),
                    ("ab\n", RunFault::BadByte { at: 2 }),
                    ("a\u{0663}", RunFault::BadByte { at: 1 }),
                ]),
            );
        }

        #[test]
        fn a_package_version_is_exact_and_has_64_bytes_or_less() {
            let longest = format!("1{}", "0".repeat(63));
            let too_long = format!("1{}", "0".repeat(64));

            tables_hold::<PackageVersion>(
                &["1", "2024.1.1", "1.0.0rc1", "1.2.3", &longest],
                &faults(&[
                    ("", RunFault::TooShort),
                    (&too_long, RunFault::TooLong),
                    ("v1.0", RunFault::BadFirstByte),
                    ("^1.0", RunFault::BadFirstByte),
                    (">=1.0", RunFault::BadFirstByte),
                    ("latest", RunFault::BadFirstByte),
                    ("1.0.0-rc1", RunFault::BadByte { at: 5 }),
                    ("1.0.0+local", RunFault::BadByte { at: 5 }),
                    ("1.*", RunFault::BadByte { at: 2 }),
                    ("1.0, 2.0", RunFault::BadByte { at: 3 }),
                    ("1.0\n", RunFault::BadByte { at: 3 }),
                    ("1.\u{0663}", RunFault::BadByte { at: 2 }),
                    ("\u{0663}.1", RunFault::BadFirstByte),
                ]),
            );
        }

        /// The refused texts of a fixed count of lower-case hex bytes, from one
        /// accepted text.
        fn not_hex(accepted: &'static str) -> Vec<(String, RunFault)> {
            let last = accepted.len() - 1;
            let short = accepted.chars().take(last).collect::<String>();

            vec![
                (String::new(), RunFault::TooShort),
                (short.clone(), RunFault::TooShort),
                (format!("{accepted}0"), RunFault::TooLong),
                (format!("{accepted}\n"), RunFault::TooLong),
                (format!("{short}\n"), RunFault::BadByte { at: last }),
                (format!("{short}g"), RunFault::BadByte { at: last }),
                (accepted.to_uppercase(), RunFault::BadByte { at: 10 }),
                (accepted.replacen('0', "g", 1), RunFault::BadFirstByte),
                // ARABIC-INDIC DIGIT ONE has 2 bytes, so the count of bytes stays.
                (
                    accepted.replacen("12", "\u{0661}", 1),
                    RunFault::BadByte { at: 1 },
                ),
            ]
        }

        /// Checks one type of a fixed count of lower-case hex bytes.
        fn hex_holds<T>(accepted: &'static str)
        where
            T: FromStr + TryFrom<String, Error = <T as FromStr>::Err>,
            T: fmt::Display + fmt::Debug + PartialEq + Serialize + DeserializeOwned,
            T::Err: fmt::Debug + fmt::Display + PartialEq + From<RunFault>,
        {
            let refused = not_hex(accepted);
            let refused: Vec<(&str, T::Err)> = refused
                .iter()
                .map(|(text, fault)| (text.as_str(), T::Err::from(*fault)))
                .collect();
            let zeros = "0".repeat(accepted.len());

            tables_hold::<T>(&[accepted, &zeros], &refused);
        }

        #[test]
        fn a_sha256_digest_is_64_bytes_of_lower_case_hex() {
            hex_holds::<Sha256Hex>(HEX_64);
            assert_eq!(
                format!("sha256:{HEX_64}").parse::<Sha256Hex>(),
                Err(Sha256HexError::TooLong)
            );
        }

        #[test]
        fn a_git_object_id_is_40_bytes_of_lower_case_hex() {
            hex_holds::<GitObjectId>(HEX_40);
            assert_eq!(
                HEX_64.parse::<GitObjectId>(),
                Err(GitObjectIdError::TooLong)
            );
        }

        #[test]
        fn a_gate_id_is_16_bytes_of_lower_case_hex() {
            hex_holds::<GateId>("0123456789abcdef");
        }

        #[test]
        fn an_owui_model_is_the_prefix_and_a_family_name() {
            let model: OwuiModel = "agent:night-watch".parse().unwrap();

            assert_eq!(model.family().as_str(), "night-watch");
            tables_hold::<OwuiModel>(
                &["agent:ab", "agent:chat", "agent:gate-probe"],
                &[
                    ("", OwuiModelError::NoPrefix),
                    ("chat", OwuiModelError::NoPrefix),
                    ("Agent:chat", OwuiModelError::NoPrefix),
                    (" agent:chat", OwuiModelError::NoPrefix),
                    ("agent:", OwuiModelError::Family(FamilyNameError::TooShort)),
                    ("agent:a", OwuiModelError::Family(FamilyNameError::TooShort)),
                    (
                        "agent:Chat",
                        OwuiModelError::Family(FamilyNameError::BadFirstByte),
                    ),
                    (
                        "agent:chat\n",
                        OwuiModelError::Family(FamilyNameError::BadByte { at: 4 }),
                    ),
                    (
                        "agent:chat-\u{0663}",
                        OwuiModelError::Family(FamilyNameError::BadByte { at: 5 }),
                    ),
                ],
            );
        }

        #[test]
        fn a_sandbox_name_is_a_family_name_and_a_number() {
            let name: SandboxName = "chat-s1-s02".parse().unwrap();

            assert_eq!(name.family().as_str(), "chat-s1");
            assert_eq!(name.number(), 2);
            assert_eq!(
                "ab-s999999999".parse::<SandboxName>().unwrap().number(),
                999_999_999
            );
            tables_hold::<SandboxName>(
                &[
                    "chat-s0",
                    "chat-s12",
                    "chat-s01",
                    "ab-s1",
                    "chat-s1-s2",
                    "chat-s123456789",
                ],
                &[
                    ("", SandboxNameError::NoNumber),
                    ("chat", SandboxNameError::NoNumber),
                    ("chat-1", SandboxNameError::NoNumber),
                    ("chat-S1", SandboxNameError::NoNumber),
                    ("chat_s1", SandboxNameError::NoNumber),
                    ("chat-s", SandboxNameError::NoNumber),
                    ("chat-s1234567890", SandboxNameError::BadNumber),
                    ("chat-s-1", SandboxNameError::BadNumber),
                    ("chat-s+1", SandboxNameError::BadNumber),
                    ("chat-s1\n", SandboxNameError::BadNumber),
                    ("chat-s\u{0663}", SandboxNameError::BadNumber),
                    ("a-s1", SandboxNameError::Family(FamilyNameError::TooShort)),
                    (
                        "Chat-s1",
                        SandboxNameError::Family(FamilyNameError::BadFirstByte),
                    ),
                    ("-s1", SandboxNameError::Family(FamilyNameError::TooShort)),
                ],
            );
        }

        #[test]
        fn an_mcp_user_is_the_prefix_and_a_server_name() {
            let user: McpUser = "mcp-web-search".parse().unwrap();

            assert_eq!(user.server().as_str(), "web-search");
            tables_hold::<McpUser>(
                &["mcp-ab", "mcp-web-search", "mcp-reader-2"],
                &[
                    ("", McpUserError::NoPrefix),
                    ("root", McpUserError::NoPrefix),
                    ("mcp", McpUserError::NoPrefix),
                    ("MCP-search", McpUserError::NoPrefix),
                    ("mcp_search", McpUserError::NoPrefix),
                    ("mcp-", McpUserError::Server(ServerNameError::TooShort)),
                    ("mcp-a", McpUserError::Server(ServerNameError::TooShort)),
                    (
                        "mcp-1x",
                        McpUserError::Server(ServerNameError::BadFirstByte),
                    ),
                    (
                        "mcp-search\n",
                        McpUserError::Server(ServerNameError::BadByte { at: 6 }),
                    ),
                    (
                        "mcp-a\u{0663}",
                        McpUserError::Server(ServerNameError::BadByte { at: 1 }),
                    ),
                ],
            );
        }

        #[test]
        fn a_version_is_three_numbers_of_ascii_digits() {
            tables_hold::<Version>(
                &[
                    "0.0.0",
                    "1.2.3",
                    "10.20.30",
                    "01.2.3",
                    "99999999999999999999.0.0",
                ],
                &[
                    ("", VersionError::WrongCount),
                    ("1", VersionError::WrongCount),
                    ("1.2", VersionError::WrongCount),
                    ("1.2.3.4", VersionError::WrongCount),
                    ("1..3", VersionError::EmptyNumber),
                    (".1.2", VersionError::EmptyNumber),
                    ("1.2.", VersionError::EmptyNumber),
                    ("v1.2.3", VersionError::BadByte { at: 0 }),
                    ("1.2.x", VersionError::BadByte { at: 4 }),
                    ("1.2.3-rc1", VersionError::BadByte { at: 5 }),
                    ("1.2.3+build", VersionError::BadByte { at: 5 }),
                    ("-1.2.3", VersionError::BadByte { at: 0 }),
                    ("1,2,3", VersionError::BadByte { at: 1 }),
                    ("1.2.3\n", VersionError::BadByte { at: 5 }),
                    ("1.2.\u{0663}", VersionError::BadByte { at: 4 }),
                    (
                        "\u{0661}.\u{0662}.\u{0663}",
                        VersionError::BadByte { at: 0 },
                    ),
                    ("1.2.\u{ff11}", VersionError::BadByte { at: 4 }),
                ],
            );
        }

        #[test]
        fn a_number_of_a_version_has_4300_digits_or_less() {
            let longest = "9".repeat(NUMBER_DIGITS_MAX);
            let too_long = format!("{longest}9");
            // A zero at the start of a number counts as a digit.
            let zeros = "0".repeat(NUMBER_DIGITS_MAX + 1);

            assert_eq!(NUMBER_DIGITS_MAX, 4300);
            tables_hold::<Version>(
                &[
                    &format!("{longest}.0.0"),
                    &format!("0.{longest}.0"),
                    &format!("0.0.{longest}"),
                    &format!("{longest}.{longest}.{longest}"),
                ],
                &[
                    (&format!("{too_long}.0.0"), VersionError::LongNumber),
                    (&format!("0.{too_long}.0"), VersionError::LongNumber),
                    (&format!("0.0.{too_long}"), VersionError::LongNumber),
                    (&format!("0.0.{zeros}"), VersionError::LongNumber),
                    (&format!("{too_long}.0"), VersionError::WrongCount),
                    (&format!("{too_long}..0"), VersionError::EmptyNumber),
                ],
            );
            tables_hold::<ContractVersion>(
                &[&format!("{longest}.0"), &format!("0.{longest}")],
                &[
                    (&format!("{too_long}.0"), ContractVersionError::LongNumber),
                    (&format!("0.{too_long}"), ContractVersionError::LongNumber),
                    (&format!("0.{zeros}"), ContractVersionError::LongNumber),
                ],
            );
            tables_hold::<Tag>(
                &[&format!("ab-v{longest}.0.0"), &format!("ab-v0.0.{longest}")],
                &[
                    (
                        &format!("ab-v{too_long}.0.0"),
                        TagError::Version(VersionError::LongNumber),
                    ),
                    (
                        &format!("ab-v0.0.{too_long}"),
                        TagError::Version(VersionError::LongNumber),
                    ),
                ],
            );
        }

        #[test]
        fn a_normalized_version_has_no_zero_at_the_start_of_a_number() {
            for (text, normalized) in [
                ("1.2.3", "1.2.3"),
                ("01.02.03", "1.2.3"),
                ("0.0.0", "0.0.0"),
                ("000.00.0", "0.0.0"),
                ("10.020.0030", "10.20.30"),
                ("099999999999999999999.0.0", "99999999999999999999.0.0"),
            ] {
                let version: Version = text.parse().unwrap();

                assert_eq!(version.normalized().as_str(), normalized);
                assert_eq!(version.normalized().normalized(), version.normalized());
            }
        }

        #[test]
        fn a_contract_version_is_two_numbers_of_ascii_digits() {
            tables_hold::<ContractVersion>(
                &["0.6", "1.0", "0.06", "99999999999999999999.0"],
                &[
                    ("", ContractVersionError::WrongCount),
                    ("1", ContractVersionError::WrongCount),
                    ("1.0.0", ContractVersionError::WrongCount),
                    (".1", ContractVersionError::EmptyNumber),
                    ("1.", ContractVersionError::EmptyNumber),
                    ("a.b", ContractVersionError::BadByte { at: 0 }),
                    ("1 .0", ContractVersionError::BadByte { at: 1 }),
                    ("1.0\n", ContractVersionError::BadByte { at: 3 }),
                    ("0.\u{0663}", ContractVersionError::BadByte { at: 2 }),
                ],
            );
        }

        #[test]
        fn a_tag_is_a_component_name_and_a_version() {
            let tag: Tag = "chaperone-v-v01.2.3".parse().unwrap();

            assert_eq!(tag.component().as_str(), "chaperone-v");
            assert_eq!(tag.version().as_str(), "01.2.3");
            tables_hold::<Tag>(
                &[
                    "chaperone-v1.2.3",
                    "ab-v0.0.0",
                    "mcp-servers-v0.0.1",
                    "chaperone-v-v1.2.3",
                ],
                &[
                    ("", TagError::NoVersion),
                    ("v1.2.3", TagError::NoVersion),
                    ("chaperone-1.2.3", TagError::NoVersion),
                    ("chaperone-V1.2.3", TagError::NoVersion),
                    (
                        "chaperone-v1.2",
                        TagError::Version(VersionError::WrongCount),
                    ),
                    ("schema-v3", TagError::Version(VersionError::WrongCount)),
                    ("chaperone-v", TagError::Version(VersionError::WrongCount)),
                    (
                        "chaperone-v1.2.3-rc1",
                        TagError::Version(VersionError::BadByte { at: 5 }),
                    ),
                    (
                        "chaperone-v1.2.3\n",
                        TagError::Version(VersionError::BadByte { at: 5 }),
                    ),
                    (
                        "chaperone-v1.2.\u{0663}",
                        TagError::Version(VersionError::BadByte { at: 4 }),
                    ),
                    (
                        "a-v1.0.0",
                        TagError::Component(ComponentNameError::TooShort),
                    ),
                    ("-v1.0.0", TagError::Component(ComponentNameError::TooShort)),
                    (
                        "Chaperone-v1.2.3",
                        TagError::Component(ComponentNameError::BadFirstByte),
                    ),
                    (
                        "chaperone_x-v1.2.3",
                        TagError::Component(ComponentNameError::BadByte { at: 9 }),
                    ),
                ],
            );
        }

        #[test]
        fn an_error_says_which_rule_failed() {
            let said = [
                ServerNameError::TooShort.to_string(),
                SessionIdError::TooShort.to_string(),
                SessionIdError::TooLong.to_string(),
                UlidError::TooShort.to_string(),
                UlidError::TooLong.to_string(),
                UlidError::BadFirstByte.to_string(),
                GateIdError::BadByte { at: 3 }.to_string(),
                AttachmentNameError::DotName.to_string(),
                AttachmentNameError::TooLong.to_string(),
                VersionError::WrongCount.to_string(),
                VersionError::EmptyNumber.to_string(),
                VersionError::LongNumber.to_string(),
                ContractVersionError::BadByte { at: 1 }.to_string(),
                OwuiModelError::NoPrefix.to_string(),
                OwuiModelError::Family(FamilyNameError::TooShort).to_string(),
                SandboxNameError::NoNumber.to_string(),
                SandboxNameError::BadNumber.to_string(),
                SandboxNameError::Family(FamilyNameError::BadFirstByte).to_string(),
                McpUserError::NoPrefix.to_string(),
                McpUserError::Server(ServerNameError::TooLong).to_string(),
                TagError::NoVersion.to_string(),
                TagError::Version(VersionError::WrongCount).to_string(),
                TagError::Component(ComponentNameError::BadFirstByte).to_string(),
            ];

            assert_eq!(
                said,
                [
                    "a server name has 2 bytes or more",
                    "a session id has 1 byte or more",
                    "a session id has 128 bytes or less",
                    "a ULID has 26 bytes",
                    "a ULID has 26 bytes",
                    "a ULID starts with 0 to 9 or A to Z without I, L, O and U",
                    "byte 3 of a gate id is not 0 to 9 or a to f",
                    "an attachment name is not . and not ..",
                    "an attachment name has 120 bytes or less",
                    "a version has 3 numbers",
                    "each number of a version has 1 digit or more",
                    "each number of a version has 4300 digits or less",
                    "byte 1 of a contract version is not 0 to 9 or .",
                    "a model name starts with agent:",
                    "after agent:, a family name has 2 bytes or more",
                    "a sandbox name ends with -s and a number",
                    "the number of a sandbox name has 1 to 9 digits, each 0 to 9",
                    "before -s and the number, a family name starts with a to z",
                    "an MCP account name starts with mcp-",
                    "after mcp-, a server name has 31 bytes or less",
                    "a tag holds -v and a version",
                    "after the last -v of a tag, a version has 3 numbers",
                    "before the last -v of a tag, a component name starts with a to z",
                ]
            );
        }

        #[test]
        fn each_error_is_a_std_error() {
            let errors: [Box<dyn Error + Send + Sync>; 6] = [
                Box::new(UlidError::TooShort),
                Box::new(AttachmentNameError::DotName),
                Box::new(VersionError::WrongCount),
                Box::new(SandboxNameError::NoNumber),
                Box::new(McpUserError::NoPrefix),
                Box::new(TagError::NoVersion),
            ];

            assert!(errors.iter().all(|error| error.source().is_none()));
        }
    }

    // --- each type against the Python implementation ---

    mod python {
        use std::collections::HashSet;

        use creche_vectors::{self as vectors, Disagreement, Outcome, Vector};
        use serde_json::{Value, json};

        use super::super::*;

        /// What the code did with one input, as a vector file writes it: the
        /// `value` of an accepted input, or the `refusal` of a refused input.
        /// `None` stands for a key that the vector does not have.
        type Replay = Result<Option<Value>, Option<Value>>;

        /// How a type stands to one Python copy of its grammar.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        enum Stance {
            /// The type and the copy do the same with each vector.
            Equal,
            /// The copy accepts an input that a stricter copy refuses. The type
            /// refuses each such input of `ids/disagreements.json`. On each other
            /// vector the type and the copy do the same.
            Stricter,
        }

        /// One surface that a type implements.
        struct Against {
            surface: &'static str,
            stance: Stance,
            replay: fn(&str) -> Replay,
        }

        const fn equal(surface: &'static str, replay: fn(&str) -> Replay) -> Against {
            Against {
                surface,
                stance: Stance::Equal,
                replay,
            }
        }

        const fn stricter(surface: &'static str, replay: fn(&str) -> Replay) -> Against {
            Against {
                surface,
                stance: Stance::Stricter,
                replay,
            }
        }

        /// A surface that only accepts or refuses: no value, no refusal code.
        fn takes<T: FromStr>(text: &str) -> Replay {
            match text.parse::<T>() {
                Ok(_) => Ok(None),
                Err(_) => Err(None),
            }
        }

        /// `agent_door_owui.openai_api.family_of`: the value is the family name,
        /// and the door has one refusal code.
        fn owui_family_of(text: &str) -> Replay {
            match text.parse::<OwuiModel>() {
                Ok(model) => Ok(Some(json!(model.family().as_str()))),
                Err(_) => Err(Some(json!({"code": "bad_model"}))),
            }
        }

        /// `agent_door_owui.headers.read_ids`: the value holds the chat id and the
        /// session id. The door has one code for an empty header and one code for
        /// each other refusal.
        fn owui_read_ids(text: &str) -> Replay {
            match OwuiChatId::from_header(text) {
                Ok(chat) => {
                    let session = chat.session_id().ok();
                    let session = session.as_ref().map(SessionId::as_str);

                    Ok(Some(json!({"chat_id": chat.as_str(), "session": session})))
                }
                Err(OwuiChatIdError::TooShort) => Err(Some(json!({"code": "missing_chat_id"}))),
                Err(_) => Err(Some(json!({"code": "bad_id"}))),
            }
        }

        /// `handover.executor.source.newest_tagged_version` on one tag: the value
        /// is the version, with each number written as an integer.
        fn newest_tagged(text: &str) -> Replay {
            match text.parse::<Tag>() {
                Ok(tag) => Ok(Some(json!(tag.version().normalized().as_str()))),
                Err(_) => Err(None),
            }
        }

        const FAMILY_NAME: &[Against] = &[
            equal("id.family_name.attendance", takes::<FamilyName>),
            equal("id.family_name.chaperone", takes::<FamilyName>),
            equal("id.family_name.chaperone_grants", takes::<FamilyName>),
            equal("id.family_name.door_owui", takes::<FamilyName>),
            equal("id.family_name.door_tui", takes::<FamilyName>),
            equal("id.family_name.family_file", takes::<FamilyName>),
            equal("id.family_name.handover_executor", takes::<FamilyName>),
            equal("id.family_name.handover_requester", takes::<FamilyName>),
            equal("id.family_name.noticeboard", takes::<FamilyName>),
        ];

        const SERVER_NAME: &[Against] = &[
            equal("id.server_name.caregiver", takes::<ServerName>),
            equal("id.server_name.chaperone_grants", takes::<ServerName>),
            equal("id.server_name.family_file", takes::<ServerName>),
            equal("id.server_name.handover_intake", takes::<ServerName>),
            equal("id.server_name.handover_mcpserver", takes::<ServerName>),
        ];

        const WEBHOOK_NAME: &[Against] =
            &[equal("id.webhook_name.family_file", takes::<WebhookName>)];

        const SKILL_NAME: &[Against] = &[equal("id.skill_name.family_file", takes::<SkillName>)];

        const COMPONENT_NAME: &[Against] = &[
            equal(
                "id.component_name.handover_executor",
                takes::<ComponentName>,
            ),
            equal(
                "id.component_name.handover_manifest",
                takes::<ComponentName>,
            ),
        ];

        const MCP_USER: &[Against] = &[
            equal("id.mcp_user.chaperone", takes::<McpUser>),
            equal("id.mcp_user.handover_executor", takes::<McpUser>),
        ];

        const SESSION_ID: &[Against] = &[
            equal("id.session_id.attendance", takes::<SessionId>),
            equal("id.session_id.chaperone", takes::<SessionId>),
            equal("id.session_id.door_tui", takes::<SessionId>),
            equal("id.session_id.handover_executor", takes::<SessionId>),
        ];

        const OWUI_CHAT_ID: &[Against] = &[equal("id.owui_chat_id.door_owui", owui_read_ids)];

        const OWUI_MODEL: &[Against] = &[equal("id.owui_model.door_owui", owui_family_of)];

        const ULID: &[Against] = &[
            equal("id.ulid.attendance", takes::<Ulid>),
            equal("id.ulid.caregiver", takes::<Ulid>),
            equal("id.ulid.chaperone", takes::<Ulid>),
            equal("id.ulid.door_trigger", takes::<Ulid>),
            equal("id.ulid.door_tui", takes::<Ulid>),
            equal("id.ulid.handover_executor", takes::<Ulid>),
            equal("id.ulid.handover_requester", takes::<Ulid>),
        ];

        const SANDBOX_NAME: &[Against] = &[
            equal("id.sandbox_name.attendance", takes::<SandboxName>),
            equal("id.sandbox_name.door_tui", takes::<SandboxName>),
        ];

        const TOOL_NAME: &[Against] = &[
            stricter("id.tool_name.chaperone_grants", takes::<ToolName>),
            stricter("id.tool_name.family_file", takes::<ToolName>),
            equal("id.tool_name.handover_mcpserver", takes::<ToolName>),
        ];

        const ENV_NAME: &[Against] = &[
            stricter("id.env_name.family_file", takes::<EnvName>),
            equal("id.env_name.handover_mcpserver", takes::<EnvName>),
        ];

        const SECRET_NAME: &[Against] = &[
            equal("id.secret_name.caregiver", takes::<SecretName>),
            equal("id.secret_name.chaperone", takes::<SecretName>),
            equal("id.secret_name.handover_intake_store", takes::<SecretName>),
            equal("id.secret_name.handover_intake_token", takes::<SecretName>),
            equal("id.secret_name.handover_manifest", takes::<SecretName>),
            equal("id.secret_name.handover_mcpserver", takes::<SecretName>),
        ];

        const VERSION: &[Against] = &[
            equal("id.version.handover_executor", takes::<Version>),
            equal("id.version.handover_manifest", takes::<Version>),
            equal("id.version.handover_provenance", takes::<Version>),
            equal("id.version.handover_state", takes::<Version>),
        ];

        const CONTRACT_VERSION: &[Against] = &[
            equal(
                "id.contract_version.handover_manifest",
                takes::<ContractVersion>,
            ),
            equal(
                "id.contract_version.handover_state",
                takes::<ContractVersion>,
            ),
        ];

        const TAG: &[Against] = &[
            equal("id.tag.handover_allocate", takes::<Tag>),
            equal("id.tag.handover_source", newest_tagged),
        ];

        const PACKAGE_VERSION: &[Against] = &[
            stricter("id.package_version.family_file", takes::<PackageVersion>),
            equal(
                "id.package_version.handover_mcpserver",
                takes::<PackageVersion>,
            ),
        ];

        const SHA256_HEX: &[Against] = &[
            equal("id.sha256_hex.chaperone_grants", takes::<Sha256Hex>),
            equal("id.sha256_hex.family_file", takes::<Sha256Hex>),
            equal("id.sha256_hex.handover_mcpserver", takes::<Sha256Hex>),
        ];

        const GIT_OBJECT_ID: &[Against] = &[
            equal("id.git_object_id.handover_provenance", takes::<GitObjectId>),
            equal("id.git_object_id.handover_source", takes::<GitObjectId>),
            equal("id.git_object_id.handover_state", takes::<GitObjectId>),
        ];

        const GATE_ID: &[Against] = &[equal("id.gate_id.chaperone", takes::<GateId>)];

        const ATTACHMENT_NAME: &[Against] = &[equal(
            "id.attachment_name.attendance",
            takes::<AttachmentName>,
        )];

        /// Each type of this module and the surfaces that it implements.
        const TYPES: &[(&str, &[Against])] = &[
            ("FamilyName", FAMILY_NAME),
            ("ServerName", SERVER_NAME),
            ("WebhookName", WEBHOOK_NAME),
            ("SkillName", SKILL_NAME),
            ("ComponentName", COMPONENT_NAME),
            ("McpUser", MCP_USER),
            ("SessionId", SESSION_ID),
            ("OwuiChatId", OWUI_CHAT_ID),
            ("OwuiModel", OWUI_MODEL),
            ("Ulid", ULID),
            ("SandboxName", SANDBOX_NAME),
            ("ToolName", TOOL_NAME),
            ("EnvName", ENV_NAME),
            ("SecretName", SECRET_NAME),
            ("Version", VERSION),
            ("ContractVersion", CONTRACT_VERSION),
            ("Tag", TAG),
            ("PackageVersion", PACKAGE_VERSION),
            ("Sha256Hex", SHA256_HEX),
            ("GitObjectId", GIT_OBJECT_ID),
            ("GateId", GATE_ID),
            ("AttachmentName", ATTACHMENT_NAME),
        ];

        /// How the Rust code differs from the Python code on one vector.
        #[derive(Debug, Clone, Copy)]
        enum Differs {
            /// The Python code accepts the input. The Rust code refuses it.
            #[expect(
                dead_code,
                reason = "DEVIATIONS holds no row today, so no code builds this variant"
            )]
            Refuses,
        }

        /// One decision to differ from the Python code. It holds for each vector
        /// of `vectors` in each surface of `surfaces`.
        struct Deviation {
            surfaces: &'static [&'static str],
            vectors: &'static [&'static str],
            differs: Differs,
            /// The contract section that the decision reads.
            contract: &'static str,
            /// The decision, and its reason.
            decision: &'static str,
        }

        /// Each vector on which the Rust code differs from the Python code on
        /// purpose. A vector outside this table and outside
        /// `ids/disagreements.json` must be equal.
        const DEVIATIONS: &[Deviation] = &[];

        /// The decision that covers one vector of one surface.
        fn deviation_of(surface: &str, vector: &str) -> Option<&'static Deviation> {
            DEVIATIONS.iter().find(|deviation| {
                deviation.surfaces.contains(&surface) && deviation.vectors.contains(&vector)
            })
        }

        /// The count of vectors of one surface that `DEVIATIONS` covers.
        fn deviations_in(surface: &str) -> usize {
            DEVIATIONS
                .iter()
                .filter(|deviation| deviation.surfaces.contains(&surface))
                .map(|deviation| deviation.vectors.len())
                .sum()
        }

        /// Whether `ids/disagreements.json` says that this surface accepts an
        /// input that another copy of the grammar refuses.
        fn is_laxer(rows: &[Disagreement], surface: &str, vector: &str) -> bool {
            rows.iter().any(|row| {
                row.id() == vector
                    && row
                        .surfaces(Outcome::Accepted)
                        .iter()
                        .any(|name| name == surface)
                    && !row.surfaces(Outcome::Refused).is_empty()
            })
        }

        /// What the Python code did with one input.
        fn python_did(vector: &Vector) -> Replay {
            match vector.result() {
                Outcome::Accepted => Ok(vector.value().cloned()),
                Outcome::Refused | Outcome::Raised => Err(vector.refusal().cloned()),
            }
        }

        /// Makes sure that the Rust code differs from the vector as the decision
        /// says, and in no other way.
        fn differs_as_decided(differs: Differs, vector: &Vector, rust: &Replay, at: &str) {
            assert_eq!(vector.result(), Outcome::Accepted, "{at}: the Python code");
            match differs {
                Differs::Refuses => assert!(rust.is_err(), "{at}: the Rust code accepts"),
            }
        }

        /// The table of one type.
        fn table_of(name: &str) -> &'static [Against] {
            let found = TYPES.iter().find(|(type_name, _)| *type_name == name);

            found.unwrap().1
        }

        /// Walks each vector of each surface of one type. It prints the counts,
        /// and a run with `--nocapture` shows them.
        fn walk(name: &str) {
            let table = table_of(name);
            let disagreements = vectors::disagreements().unwrap();
            let mut equal = 0;
            let mut stricter = 0;
            let mut deviated = 0;

            assert!(
                table.iter().any(|against| against.stance == Stance::Equal),
                "{name} is equal to no Python copy"
            );
            for against in table {
                let surface = vectors::surface(against.surface).unwrap();
                let mut laxer_here = 0;
                let mut deviated_here = 0;
                for vector in surface.vectors() {
                    let at = format!("{} {}", against.surface, vector.id());
                    let rust = (against.replay)(vector.input().text().unwrap());

                    if let Some(deviation) = deviation_of(against.surface, vector.id()) {
                        assert!(!deviation.contract.is_empty() && !deviation.decision.is_empty());
                        differs_as_decided(deviation.differs, vector, &rust, &at);
                        deviated_here += 1;
                    } else if vector.result() == Outcome::Raised {
                        assert!(rust.is_err(), "{at}: the Python code raises");
                        equal += 1;
                    } else if against.stance == Stance::Stricter
                        && is_laxer(&disagreements, against.surface, vector.id())
                    {
                        assert_eq!(vector.result(), Outcome::Accepted, "{at}");
                        assert!(rust.is_err(), "{at}: a stricter copy refuses");
                        laxer_here += 1;
                    } else {
                        assert_eq!(rust, python_did(vector), "{at}");
                        equal += 1;
                    }
                }

                assert_eq!(
                    laxer_here > 0,
                    against.stance == Stance::Stricter,
                    "{}: the stance is wrong",
                    against.surface
                );
                assert_eq!(
                    deviated_here,
                    deviations_in(against.surface),
                    "{}",
                    against.surface
                );
                stricter += laxer_here;
                deviated += deviated_here;
            }

            println!(
                "{name}: {} vectors of {} surfaces: {equal} equal, {stricter} refused with the \
                 strictest copy, {deviated} deviations",
                equal + stricter + deviated,
                table.len()
            );
        }

        #[test]
        fn the_tables_hold_each_id_surface_of_the_index_one_time() {
            let listed: Vec<&str> = TYPES
                .iter()
                .flat_map(|(_, table)| table.iter().map(|against| against.surface))
                .collect();
            let unique: HashSet<&str> = listed.iter().copied().collect();
            let index = vectors::index().unwrap();
            let in_index: HashSet<&str> = index
                .iter()
                .map(|row| row.surface())
                .filter(|surface| surface.starts_with("id."))
                .collect();

            assert_eq!(unique.len(), listed.len(), "two tables hold one surface");
            assert_eq!(unique, in_index);
        }

        #[test]
        fn each_deviation_names_a_surface_of_a_table() {
            let listed: HashSet<&str> = TYPES
                .iter()
                .flat_map(|(_, table)| table.iter().map(|against| against.surface))
                .collect();
            for deviation in DEVIATIONS {
                assert!(deviation.contract.starts_with("contract "));
                for surface in deviation.surfaces {
                    assert!(listed.contains(surface), "{surface}");
                }
            }
        }

        #[test]
        fn each_disagreement_is_in_a_table_as_a_stricter_stance() {
            let stricter: HashSet<&str> = TYPES
                .iter()
                .flat_map(|(_, table)| table.iter())
                .filter(|against| against.stance == Stance::Stricter)
                .map(|against| against.surface)
                .collect();
            for row in vectors::disagreements().unwrap() {
                assert!(
                    row.surfaces(Outcome::Raised).is_empty(),
                    "{} {}",
                    row.grammar(),
                    row.id()
                );
                for surface in row.surfaces(Outcome::Accepted) {
                    assert!(
                        stricter.contains(surface.as_str()),
                        "{surface} {}",
                        row.id()
                    );
                }
            }
        }

        #[test]
        fn a_family_name_is_what_each_python_copy_takes() {
            walk("FamilyName");
        }

        #[test]
        fn a_server_name_is_what_each_python_copy_takes() {
            walk("ServerName");
        }

        #[test]
        fn a_webhook_name_is_what_the_python_copy_takes() {
            walk("WebhookName");
        }

        #[test]
        fn a_skill_name_is_what_the_python_copy_takes() {
            walk("SkillName");
        }

        #[test]
        fn a_component_name_is_what_each_python_copy_takes() {
            walk("ComponentName");
        }

        #[test]
        fn an_mcp_user_is_what_each_python_copy_takes() {
            walk("McpUser");
        }

        #[test]
        fn a_session_id_is_what_each_python_copy_takes() {
            walk("SessionId");
        }

        #[test]
        fn an_owui_chat_id_is_what_the_python_door_reads() {
            walk("OwuiChatId");
        }

        #[test]
        fn an_owui_model_is_what_the_python_door_reads() {
            walk("OwuiModel");
        }

        #[test]
        fn a_ulid_is_what_each_python_copy_takes() {
            walk("Ulid");
        }

        #[test]
        fn a_sandbox_name_is_what_each_python_copy_takes() {
            walk("SandboxName");
        }

        #[test]
        fn a_tool_name_is_what_the_strictest_python_copy_takes() {
            walk("ToolName");
        }

        #[test]
        fn an_env_name_is_what_the_strictest_python_copy_takes() {
            walk("EnvName");
        }

        #[test]
        fn a_secret_name_is_what_each_python_copy_takes() {
            walk("SecretName");
        }

        #[test]
        fn a_version_is_what_each_python_copy_takes() {
            walk("Version");
        }

        #[test]
        fn a_contract_version_is_what_each_python_copy_takes() {
            walk("ContractVersion");
        }

        #[test]
        fn a_tag_is_what_each_python_copy_takes() {
            walk("Tag");
        }

        #[test]
        fn a_package_version_is_what_the_strictest_python_copy_takes() {
            walk("PackageVersion");
        }

        #[test]
        fn a_sha256_digest_is_what_each_python_copy_takes() {
            walk("Sha256Hex");
        }

        #[test]
        fn a_git_object_id_is_what_each_python_copy_takes() {
            walk("GitObjectId");
        }

        #[test]
        fn a_gate_id_is_what_the_python_copy_takes() {
            walk("GateId");
        }

        #[test]
        fn an_attachment_name_is_what_the_python_copy_takes() {
            walk("AttachmentName");
        }
    }
}
