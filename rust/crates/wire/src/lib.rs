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

/// The rule of `is_family_name` as a regular expression, for the JSON Schema.
/// It is `FAMILY_NAME` of `family/grammar.py`, the authority for the family
/// file (decision CH23 rule 5), with `$` for `\Z`. `wire/tests` holds the
/// two equal.
#[cfg(test)]
const FAMILY_NAME_PATTERN: &str = "^[a-z][a-z0-9-]{1,30}$";

/// The length of a family name, in characters: 2 to 31.
const FAMILY_NAME_LEN: std::ops::RangeInclusive<usize> = 2..=31;

/// Whether `text` is a family name: a lowercase letter, then 1 to 30
/// lowercase letters, digits or hyphens.
fn is_family_name(text: &str) -> bool {
    let mut characters = text.chars();

    FAMILY_NAME_LEN.contains(&text.len())
        && characters
            .next()
            .is_some_and(|first| first.is_ascii_lowercase())
        && characters.all(|character| matches!(character, 'a'..='z' | '0'..='9' | '-'))
}

/// Why a text is not a family name or a webhook name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NameError;

impl fmt::Display for NameError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(
            "a name is a lowercase letter, then 1 to 30 lowercase letters, digits or hyphens",
        )
    }
}

impl std::error::Error for NameError {}

/// A name with the rule of a family name. On the wire it is a JSON string,
/// and a string of any other form is refused.
macro_rules! family_name {
    ($(#[$doc:meta])* $name:ident) => {
        $(#[$doc])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        #[cfg_attr(test, derive(schemars::JsonSchema))]
        #[cfg_attr(test, schemars(extend("pattern" = FAMILY_NAME_PATTERN)))]
        pub struct $name(String);

        impl $name {
            /// The name as text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = NameError;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                if !is_family_name(text) {
                    return Err(NameError);
                }
                Ok(Self(text.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = NameError;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                text.parse()
            }
        }

        impl From<$name> for String {
            fn from(name: $name) -> Self {
                name.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

family_name!(
    /// The name of a family: `FAMILY_NAME` of `family/grammar.py`.
    FamilyName
);

family_name!(
    /// The name of a declared webhook trigger: `WEBHOOK_NAME` of
    /// `family/grammar.py`, which has the rule of a family name.
    WebhookName
);

/// The most families a dispatch chain holds (contract 02 §13.2 rule 6).
const CHAIN_MAX: usize = 8;

/// Why a list of family names is not a dispatch chain.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChainError;

impl fmt::Display for ChainError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a dispatch chain holds 1 to 8 family names")
    }
}

impl std::error::Error for ChainError {}

/// The delegation chain that reached a dispatched job, caller first and the
/// job's family last (contract 02 §13.2 rule 6). It holds 1 to 8 family
/// names. The PEP builds it from the delegations it minted (contract 04
/// §6.3).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "Vec<FamilyName>", into = "Vec<FamilyName>")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("minItems" = 1, "maxItems" = CHAIN_MAX)))]
pub struct DispatchChain(Vec<FamilyName>);

impl DispatchChain {
    /// The families of the chain, caller first.
    #[must_use]
    pub fn families(&self) -> &[FamilyName] {
        &self.0
    }
}

impl TryFrom<Vec<FamilyName>> for DispatchChain {
    type Error = ChainError;

    fn try_from(families: Vec<FamilyName>) -> Result<Self, Self::Error> {
        if families.is_empty() || families.len() > CHAIN_MAX {
            return Err(ChainError);
        }
        Ok(Self(families))
    }
}

impl From<DispatchChain> for Vec<FamilyName> {
    fn from(chain: DispatchChain) -> Self {
        chain.0
    }
}

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

/// The trigger of a cron firing. It carries nothing, because a timer has no
/// name (contract 02 §13.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct Timer {}

object_only!(Timer);

/// The trigger of a webhook firing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct Webhook {
    /// The name of the webhook that the family declares.
    pub name: WebhookName,
}

object_only!(Webhook);

/// The trigger of a dispatch: the `enqueue` verb of another family started
/// the job (contract 01 §3.13, contract 02 §13.4).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct Dispatch {
    /// The delegation chain that reached the job. attendance today stores a
    /// dispatch with no chain when `POST /dispatch` carries none, so it must
    /// refuse a missing or empty chain before it writes this origin.
    pub chain: DispatchChain,
    /// The calling family. attendance takes it from the family token, never
    /// from the request body (contract 02 §13.2 rule 5).
    pub name: FamilyName,
}

object_only!(Dispatch);

/// What fired an autonomous job, typed per kind (decision CH21-4a). A timer
/// has no name, and only a dispatch has a chain. There is no fired time: the
/// created time of the row stands in for it.
///
/// On the wire the variant name is the one key of an object, as in
/// `SessionOrigin`, and each variant has a `title` for its class name.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum Trigger {
    /// A cron firing.
    #[cfg_attr(test, schemars(title = "TimerTrigger"))]
    Timer(Timer),
    /// A webhook firing.
    #[cfg_attr(test, schemars(title = "WebhookTrigger"))]
    Webhook(Webhook),
    /// A dispatch from another family.
    #[cfg_attr(test, schemars(title = "DispatchTrigger"))]
    Dispatch(Dispatch),
}

/// The origin of a session that attendance opened for one autonomous job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct Auto {
    /// The id that attendance gives the job. The `SessionId` of the row is a
    /// separate value.
    pub job: AutoJobId,
    /// What fired the job.
    pub trigger: Trigger,
}

object_only!(Auto);

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
    /// attendance opened the session for one autonomous job.
    #[cfg_attr(test, schemars(title = "AutoOrigin"))]
    Auto(Auto),
}

/// The largest integer that a JavaScript number holds exactly: 2^53 - 1,
/// `Number.MAX_SAFE_INTEGER`. No wire integer is larger (decision CH22 rule
/// 3). The schema test fails on an integer that can be larger.
const JS_INTEGER_MAX: u64 = (1 << 53) - 1;

/// A time on the wire: whole milliseconds since 1970-01-01T00:00:00Z, UTC
/// (decision CH22-1). It is the one wire form of a time. No wire type holds
/// a `date-time` string.
///
/// On the wire it is a JSON integer from 0 to 2^53 - 1, so JavaScript reads
/// it exactly. A larger integer, a negative integer, a float such as `1.0`
/// or `1e3`, and a string are refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "u64", into = "u64")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("minimum" = 0, "maximum" = JS_INTEGER_MAX)))]
pub struct UnixMillis(u64);

/// Why an integer is not a `UnixMillis`: it is larger than 2^53 - 1.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UnixMillisError;

impl UnixMillis {
    /// The milliseconds since 1970-01-01T00:00:00Z.
    #[must_use]
    pub fn as_u64(self) -> u64 {
        self.0
    }
}

impl TryFrom<u64> for UnixMillis {
    type Error = UnixMillisError;

    fn try_from(millis: u64) -> Result<Self, Self::Error> {
        if millis > JS_INTEGER_MAX {
            return Err(UnixMillisError);
        }
        Ok(Self(millis))
    }
}

impl From<UnixMillis> for u64 {
    fn from(time: UnixMillis) -> Self {
        time.0
    }
}

impl fmt::Display for UnixMillis {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl fmt::Display for UnixMillisError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a wire time is 0 to 2^53 - 1 milliseconds since 1970-01-01T00:00:00Z")
    }
}

impl std::error::Error for UnixMillisError {}

/// Why a text is not a session title or a session cwd. It holds the rule.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextError(&'static str);

impl fmt::Display for TextError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for TextError {}

/// A text that `$check` accepts. On the wire it is a JSON string with the
/// schema keys `$key = $value`, and a string that breaks the rule is refused.
macro_rules! checked_text {
    ($(#[$doc:meta])* $name:ident, $check:ident, $rule:literal, $($key:literal = $value:expr),+) => {
        $(#[$doc])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        #[cfg_attr(test, derive(schemars::JsonSchema))]
        #[cfg_attr(test, schemars(extend($($key = $value),+)))]
        pub struct $name(String);

        impl $name {
            /// The text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = TextError;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                if !$check(text) {
                    return Err(TextError($rule));
                }
                Ok(Self(text.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = TextError;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                text.parse()
            }
        }

        impl From<$name> for String {
            fn from(text: $name) -> Self {
                text.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

/// The most characters of a session title. `titleOf` in
/// `facade/src/sessions.ts` cuts a title to 256 bytes of UTF-8, and a
/// character takes at least one byte, so no title it cuts passes this cap.
const TITLE_MAX: usize = 256;

/// White space to JavaScript, as ranges of code points: the set of `\s` and
/// of `trim`, which `titleOf` folds. It is not Rust's `char::is_whitespace`,
/// which holds U+0085 and not U+FEFF. `is_js_space` and `title_pattern` read
/// this one table, so the Rust rule and the schema rule cannot differ.
const JS_SPACE: &[(char, char)] = &[
    ('\u{9}', '\u{D}'),
    ('\u{20}', '\u{20}'),
    ('\u{A0}', '\u{A0}'),
    ('\u{1680}', '\u{1680}'),
    ('\u{2000}', '\u{200A}'),
    ('\u{2028}', '\u{2028}'),
    ('\u{2029}', '\u{2029}'),
    ('\u{202F}', '\u{202F}'),
    ('\u{205F}', '\u{205F}'),
    ('\u{3000}', '\u{3000}'),
    ('\u{FEFF}', '\u{FEFF}'),
];

/// Whether `character` is white space to JavaScript (`JS_SPACE`).
fn is_js_space(character: char) -> bool {
    JS_SPACE
        .iter()
        .any(|&(first, last)| (first..=last).contains(&character))
}

/// The rule of `is_session_title` as a regular expression, for the JSON
/// Schema: words with no character of `JS_SPACE`, one space apart.
#[cfg(test)]
fn title_pattern() -> String {
    let class: String = JS_SPACE
        .iter()
        .map(|&(first, last)| {
            let (first, last) = (u32::from(first), u32::from(last));
            if first == last {
                format!("\\u{first:04X}")
            } else {
                format!("\\u{first:04X}-\\u{last:04X}")
            }
        })
        .collect();
    format!("^[^{class}]+( [^{class}]+)*$")
}

/// Whether `text` is a session title: 1 to 256 characters, words with no
/// white space, one U+0020 between two words. It is what `titleOf` makes
/// when the facade also trims the end after the cut. Today `titleOf` cuts
/// after it folds, so a cut can end in a space, and this rule refuses that
/// title. The facade adds the trim before it sends a turn event (plan
/// phase 6, PR 6).
fn is_session_title(text: &str) -> bool {
    text.chars().count() <= TITLE_MAX
        && text
            .split(' ')
            .all(|word| !word.is_empty() && !word.chars().any(is_js_space))
}

checked_text!(
    /// The title of a session on the host session list (decision CH21). The
    /// facade makes it at the end of each turn, as `titleOf` makes it today,
    /// with a trim of the end after the cut: the first line of the prompt
    /// with words, white space folded to one space, cut to 256 bytes, then
    /// trimmed.
    SessionTitle,
    is_session_title,
    "a session title is 1 to 256 characters of words one space apart",
    "minLength" = 1,
    "maxLength" = TITLE_MAX,
    "pattern" = title_pattern()
);

/// The most characters of a session cwd: `PATH_MAX` of Linux, 4096 bytes. A
/// character takes at least one byte, so each path Linux names fits.
const CWD_MAX: usize = 4096;

/// The rule of `is_session_cwd` as a regular expression, for the JSON Schema.
#[cfg(test)]
const CWD_PATTERN: &str = "^/[^\\u0000]*$";

/// Whether `text` is an absolute path: a slash first, no NUL, at most 4096
/// characters.
fn is_session_cwd(text: &str) -> bool {
    text.starts_with('/') && !text.contains('\0') && text.chars().count() <= CWD_MAX
}

checked_text!(
    /// The `cwd` of a session: the absolute path that the client sent in
    /// `session/new`, or that attendance chose for a job. It is a display
    /// value and a `session/list` filter, never a guest path (decision CH5e,
    /// CH21-4).
    SessionCwd,
    is_session_cwd,
    "a session cwd is an absolute path of at most 4096 characters, with no NUL",
    "maxLength" = CWD_MAX,
    "pattern" = CWD_PATTERN
);

/// One row of the host session list (decision CH1, CH21, CH21-4). The vmm
/// keeps it, so `session/list` and the noticeboard need no VM boot.
///
/// The list holds the rows in the order the vmm stored them, newest first,
/// as `session-index.ts:130-158` does today with its `rowid` (decision
/// CH21-5). Two creates can share a millisecond, so the order is not the
/// order of `created_at`. The list reply carries an opaque cursor on that
/// stored order. The order is no field of the row.
///
/// Each key is required. A field that may be `null` reads with
/// `Option::deserialize`, so a missing key is refused, and the schema lists
/// it in `required`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = schema::require_each_key))]
pub struct SessionRow {
    /// When attendance created the session.
    pub created_at: UnixMillis,
    /// The `cwd` that attendance sent in the create.
    pub cwd: SessionCwd,
    /// The family that serves the session.
    pub family: FamilyName,
    /// The id that attendance chose (decision CH25 rule 2).
    pub id: SessionId,
    /// Who made the session.
    pub origin: SessionOrigin,
    /// The first title from the facade, or `null` before a turn names the
    /// session. The vmm sets it only while it is `null`, so the first title
    /// stays, as `nameOnce` keeps it today (`session-index.ts:122-125`).
    #[serde(deserialize_with = "Option::deserialize")]
    pub title: Option<SessionTitle>,
    /// The time of the last Flue record of the last turn, from the facade.
    /// At create the vmm sets it to `created_at`, as `SessionIndex.create`
    /// does today. Each turn end sets it again.
    pub updated_at: UnixMillis,
}

object_only!(SessionRow);

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
