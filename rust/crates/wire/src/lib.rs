//! The wire types of the VM owner. Each type here is the one definition of
//! a value that crosses a process boundary: between the vmm, the guest
//! init, caregiver and attendance.

use std::fmt;
use std::num::NonZeroU32;
use std::str::FromStr;

use serde::de::value::MapAccessDeserializer;
use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

mod ports;
mod routes;
#[cfg(test)]
mod schema;
#[cfg(test)]
mod vectors;

pub use ports::{GUEST_INIT_PROTOCOL, GuestPort, HostPort};
pub use routes::{
    AcpError, AcpRoute, Body, CreateError, CreateRoute, DROP_CLIENT_TIMEOUT, DROP_SIGTERM_GRACE,
    DROP_STOP_WAIT, DropError, DropRoute, HOST_API_BODY_CAP, HOST_API_HEADER, HOST_API_PROTOCOL,
    JsonRoute, ListError, ListRoute, MISMATCH_STATUS, ProtocolMismatch, RouteError, SHARED_CODES,
    Serve, ServeUpgrade, UpgradeRoute,
};

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

pub(crate) use object_only;

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

/// What attendance sends to the vmm to make a row of the host session list
/// (decisions CH21-4, CH25 rule 2). It is a `SessionRow` without `title`
/// and `updated_at`. The vmm sets `title` to `null` and `updated_at` to
/// `created_at`. It makes no session disk at create (decision CH21-4).
///
/// Two creates are the same when their typed values are equal
/// (`PartialEq`), not when their bytes are equal. A client retries after
/// ENOENT, ECONNREFUSED or EOF (decision CH20). attendance stores the create
/// before it sends it the first time. A retry resends the same `created_at`,
/// so the repeat is the same create.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct CreateSession {
    /// When attendance created the session.
    pub created_at: UnixMillis,
    /// The `cwd` that the client sent in `session/new`, or that attendance
    /// chose for a job.
    pub cwd: SessionCwd,
    /// The family that serves the session.
    pub family: FamilyName,
    /// The id that attendance chose (decision CH25 rule 2).
    pub id: SessionId,
    /// Who made the session.
    pub origin: SessionOrigin,
}

object_only!(CreateSession);

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

/// A place on the stored order of the host session list (decision CH21-5).
/// The vmm gives it as `next` of a `SessionPage`, and the client sends it
/// back as `cursor` of the next `ListSessions`. It is opaque: a client never
/// makes one or reads a meaning from it.
///
/// On the wire it is a JSON integer from 1 to 2^53 - 1, so JavaScript reads
/// it exactly. Any such integer reads, so the list route has no cursor
/// error. attendance turns it into the string cursor of ACP `session/list`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "u64", into = "u64")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("minimum" = 1, "maximum" = JS_INTEGER_MAX)))]
pub struct ListCursor(u64);

/// Why an integer is not a `ListCursor`: it is 0 or larger than 2^53 - 1.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ListCursorError;

impl ListCursor {
    /// The place on the stored order.
    #[must_use]
    pub fn get(self) -> u64 {
        self.0
    }
}

impl TryFrom<u64> for ListCursor {
    type Error = ListCursorError;

    fn try_from(place: u64) -> Result<Self, Self::Error> {
        if place == 0 || place > JS_INTEGER_MAX {
            return Err(ListCursorError);
        }
        Ok(Self(place))
    }
}

impl From<ListCursor> for u64 {
    fn from(cursor: ListCursor) -> Self {
        cursor.0
    }
}

impl fmt::Display for ListCursorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a list cursor is 1 to 2^53 - 1")
    }
}

impl std::error::Error for ListCursorError {}

/// What a client sends to the vmm for one page of the host session list
/// (decisions CH21-5, CH21-7). The list is scoped to one family, as the
/// list entry of the route table plan gives it. A family with no rows, or
/// one the vmm does not know, gives an empty page, so the list route has no
/// error of its own.
///
/// Each key is required, and `null` is the one form of no value.
// The key `family` is required, so one request never reads the rows of
// two families (decision CH34 rule 4). The key comes from the list entry
// of reframe/route-table-plan.json.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = schema::require_each_key))]
pub struct ListSessions {
    /// The `next` of the page before, or `null` for the first page.
    #[serde(deserialize_with = "Option::deserialize")]
    pub cursor: Option<ListCursor>,
    /// Only the rows with this `cwd`, or `null` for every row. The vmm
    /// applies it before it cuts the page, as `session-index.ts` does today.
    #[serde(deserialize_with = "Option::deserialize")]
    pub cwd: Option<SessionCwd>,
    /// The family whose rows the page holds.
    pub family: FamilyName,
}

object_only!(ListSessions);

/// One page of the host session list: the reply of the list route. The rows
/// are newest stored first (decision CH21-5).
///
/// The vmm sets the most rows of a page. The page also ends early when one
/// more row would take the reply past `HOST_API_BODY_CAP` (decision CH33
/// rule 2). A client then gets fewer rows and a `next` that is not `null`.
/// The largest row is far under the cap, so a page with a `next` holds at
/// least one row.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = schema::require_each_key))]
pub struct SessionPage {
    /// The cursor of the next page, or `null` when no row follows.
    #[serde(deserialize_with = "Option::deserialize")]
    pub next: Option<ListCursor>,
    /// The rows of this page.
    pub rows: Vec<SessionRow>,
}

object_only!(SessionPage);

/// One session to drop. It names the family that owns the session (decision
/// CH33 rule 5). The vmm answers `not_found` when no row has the id, and
/// when the row of the id belongs to another family. So a client learns
/// nothing of the sessions of another family.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct DropSession {
    /// The family that owns the session.
    pub family: FamilyName,
    /// The id of the session.
    pub id: SessionId,
}

object_only!(DropSession);

/// Every session of one family (decision CH21-10). A family with no rows
/// gives `Dropped` too, so this drop has no `not_found`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct DropFamily {
    /// The family whose sessions the vmm drops.
    pub family: FamilyName,
}

object_only!(DropFamily);

/// What a client sends to the vmm to drop sessions: the request of the drop
/// route. On the wire the variant name is the one key of an object, as in
/// `SessionOrigin`, and each variant has a `title` for its class name.
///
/// attendance drops one session on an ACP `session/delete` (decision CH27)
/// and at the end of a job (decision CH21-2). caregiver drops a family when
/// it deletes the family (decision CH21-10).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum DropTarget {
    /// One session of one family.
    #[cfg_attr(test, schemars(title = "SessionDrop"))]
    Session(DropSession),
    /// Every session of one family.
    #[cfg_attr(test, schemars(title = "FamilyDrop"))]
    Family(DropFamily),
}

/// The reply of the drop route. It carries nothing: the vmm sends it after
/// the target has no VM, no snapshot, no session disk and no row.
///
/// It is a type of its own so that the generated Python model is a class
/// with no field. An inline empty object generates a `dict` with lax keys.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct Dropped {}

object_only!(Dropped);

/// The session of an ACP stream: the request of the `acp` Upgrade route. It
/// names the family that owns the session (decision CH33 rule 5). The vmm
/// answers `not_found` when no row has the id, and when the row of the id
/// belongs to another family. So a client learns nothing of the sessions
/// of another family.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct OpenAcp {
    /// The family that owns the session.
    pub family: FamilyName,
    /// The id of the session.
    pub id: SessionId,
}

object_only!(OpenAcp);

/// A turn started. It carries nothing.
///
/// It is a type of its own so that the generated Python model is a class
/// with no field, as `Door` is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct TurnStart {}

object_only!(TurnStart);

/// A turn ended: it completed, the client cancelled it, or it failed. The
/// facade makes both fields as it makes them today, at the end of each
/// turn (`facade/src/turns.ts:139-151`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct TurnEnd {
    /// The title that `titleOf` makes from the prompt of this turn, with the
    /// end trimmed after the cut. The facade refuses a prompt with no text
    /// before the turn starts, so each turn has a title. The vmm stores it
    /// only while the row's title is `null`, so the first title stays.
    pub title: SessionTitle,
    /// The time of the last Flue record of this turn, or the facade's clock
    /// when the turn wrote no record. The vmm stores it at each end.
    pub updated_at: UnixMillis,
}

object_only!(TurnEnd);

/// What the facade tells guest init at each turn start and turn end, and
/// what guest init relays to the vmm on control link 1200 (decision CH21).
/// At each end the vmm sets the row's `updated_at`, and it sets the row's
/// `title` only while that is `null`, as `nameOnce` does today. It
/// snapshots a VM only between an end and the next start (decision CH19
/// rule 6).
///
/// The event names no session: a VM serves one session (decision CH1), and
/// the link names the VM. On the wire the variant name is the one key of an
/// object, and each variant has a `title` for its class name.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum TurnEvent {
    /// A turn started.
    #[cfg_attr(test, schemars(title = "TurnStartEvent"))]
    Start(TurnStart),
    /// A turn ended.
    #[cfg_attr(test, schemars(title = "TurnEndEvent"))]
    End(TurnEnd),
}

/// What the vmm sends first on each new control link, guest port 1200
/// (decisions CH13, CH14). A new link replaces the old one (decision CH19
/// rule 2). Guest init sets `CLOCK_REALTIME` to `sent_at` with
/// `clock_settime` before it answers with `ControlReady` (decision CH28),
/// because after a restore the guest clock is behind by the time the VM
/// spent as a snapshot (fact 73).
///
/// The hello carries no family name and no protocol version (decision CH32
/// rules 1 and 2). The vmm writes guest init at each cold boot (decision
/// CH7-6). A restore brings back the guest init of the snapshot, so the
/// vmm restores a snapshot only when its key holds the same guest-init
/// protocol version (decision CH7-6). So the two never skew in protocol.
/// A later hello can name a session that differs from the first one. Guest
/// init then closes the link, and the vmm records a fault and cold-boots
/// the VM (decision CH32 rule 3). No wire type carries that case.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct ControlHello {
    /// The wall time of the vmm when it sent the hello.
    pub sent_at: UnixMillis,
    /// The session that the VM serves (decisions CH1, CH5e). The facade
    /// opens this one session at start, and creates it when it is new
    /// (decisions CH21-8, CH25 rule 2).
    pub session: SessionId,
}

object_only!(ControlHello);

/// How many times guest init started the facade since the VM booted
/// (decision CH19 rule 1). A restore keeps guest init's memory, so it keeps
/// the count. It is at least 1: a facade that never started has no count.
///
/// On the wire it is a JSON integer from 1 to 2^53 - 1, so JavaScript reads
/// it exactly.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "u64", into = "u64")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("minimum" = 1, "maximum" = JS_INTEGER_MAX)))]
pub struct StartCount(u64);

/// Why an integer is not a `StartCount`: it is 0 or larger than 2^53 - 1.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StartCountError;

impl StartCount {
    /// The count.
    #[must_use]
    pub fn get(self) -> u64 {
        self.0
    }
}

impl TryFrom<u64> for StartCount {
    type Error = StartCountError;

    fn try_from(count: u64) -> Result<Self, Self::Error> {
        if count == 0 || count > JS_INTEGER_MAX {
            return Err(StartCountError);
        }
        Ok(Self(count))
    }
}

impl From<StartCount> for u64 {
    fn from(count: StartCount) -> Self {
        count.0
    }
}

impl fmt::Display for StartCountError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a start count is 1 to 2^53 - 1")
    }
}

impl std::error::Error for StartCountError {}

/// The facade before guest init starts it the first time. It carries
/// nothing: no turn can end before a facade starts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct FacadeNotStarted {}

object_only!(FacadeNotStarted);

/// A facade process that guest init started, which did not open its local
/// link to guest init yet (decision CH32 rule 6). Guest init gets no turn
/// event before that link opens, so this state has no turn flag.
///
/// Each key is required. `last_turn_end` reads with `Option::deserialize`,
/// so a missing key is refused.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = schema::require_each_key))]
pub struct FacadeStarting {
    /// The last turn end that guest init got since the VM booted, or `null`
    /// when no turn ended. A facade that exited and starts again keeps it,
    /// because guest init keeps it. On the first start (`starts` 1) it is
    /// always `null`, because no local link opened yet (decision CH32 rule
    /// 7). The type does not refuse a value there. `FacadeState` says how
    /// the vmm applies it.
    #[serde(deserialize_with = "Option::deserialize")]
    pub last_turn_end: Option<TurnEnd>,
    /// The pid of the facade in the guest, as in `FacadeRunning`.
    pub pid: NonZeroU32,
    /// How many times guest init started the facade, this start too.
    pub starts: StartCount,
}

object_only!(FacadeStarting);

/// A facade process that runs and opened its local link to guest init.
///
/// Each key is required. `last_turn_end` reads with `Option::deserialize`,
/// so a missing key is refused.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = schema::require_each_key))]
pub struct FacadeRunning {
    /// The last turn end that guest init got since the VM booted, or `null`
    /// when no turn ended. `FacadeState` says how the vmm applies it.
    #[serde(deserialize_with = "Option::deserialize")]
    pub last_turn_end: Option<TurnEnd>,
    /// The pid of the facade in the guest. Guest init is pid 1, and a
    /// 64-bit Linux keeps each pid below 2^22, so the facade's pid is 2 to
    /// 2^22 - 1. The type accepts 1 to 2^32 - 1, the range of `Child::id`.
    pub pid: NonZeroU32,
    /// How many times guest init started the facade, this start too.
    pub starts: StartCount,
    /// True when the facade sent a turn start and no turn end after it
    /// (decision CH21).
    pub turn_running: bool,
}

object_only!(FacadeRunning);

/// A facade that exited: `waitpid` reported it gone, and guest init did not
/// start the next one yet (decision CH19 rule 1). No turn runs.
///
/// Each key is required. `last_turn_end` reads with `Option::deserialize`,
/// so a missing key is refused.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = schema::require_each_key))]
pub struct FacadeExited {
    /// The last turn end that guest init got since the VM booted, or `null`
    /// when no turn ended. `FacadeState` says how the vmm applies it.
    #[serde(deserialize_with = "Option::deserialize")]
    pub last_turn_end: Option<TurnEnd>,
    /// How many times guest init started the facade.
    pub starts: StartCount,
}

object_only!(FacadeExited);

/// The facade as guest init sees it (decision CH19 rule 2). Only a live
/// facade has a pid, only a running facade has a turn, and a facade that
/// never started has no start count.
///
/// The facade is up for decision CH19 rule 6 only in `running`. So the vmm
/// snapshots a VM only when the facade is `running` and `turn_running` is
/// false. A `starting` facade is not up: guest init cannot see its turns
/// yet, so the vmm never snapshots a turn that guest init cannot see
/// (decision CH32 rule 6). When the local link of a live facade closes,
/// guest init kills the facade, and the state moves to `exited` after
/// `waitpid` (decision CH32 rule 7). So a facade never goes back from
/// `running` to `starting`.
///
/// Each state after the first start holds `last_turn_end`, so
/// `not_started` cannot carry one (decision CH32 rule 5). A restore can
/// drop a turn event on the old link (fact 45), so guest init reports the
/// last `TurnEnd`, not a current title (decision CH32 rule 4). The vmm
/// applies it as it applies `TurnEvent::End`: it sets the row's
/// `updated_at`, and it sets the row's title only while that is `null`.
/// So an end that a restore dropped still arrives.
///
/// On the wire the variant name is the one key of an object, and each
/// variant has a `title` for its class name.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum FacadeState {
    /// Guest init did not start the facade yet.
    #[cfg_attr(test, schemars(title = "FacadeNotStartedState"))]
    NotStarted(FacadeNotStarted),
    /// The facade process is alive and did not open its local link yet.
    #[cfg_attr(test, schemars(title = "FacadeStartingState"))]
    Starting(FacadeStarting),
    /// The facade runs and opened its local link.
    #[cfg_attr(test, schemars(title = "FacadeRunningState"))]
    Running(FacadeRunning),
    /// The facade exited, and the next one did not start yet.
    #[cfg_attr(test, schemars(title = "FacadeExitedState"))]
    Exited(FacadeExited),
}

/// What guest init answers to `ControlHello`, after it sets its clock
/// (decisions CH28, CH32). It holds the facade state (decision CH19 rule 2).
/// The last turn end sits in the facade state. The answer stays an object,
/// so a later field has a place.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct ControlReady {
    /// The facade.
    pub facade: FacadeState,
}

object_only!(ControlReady);

/// The most bytes of one line on control link 1200, the LF not counted
/// (decision CH36 G11). A reader that gets this many bytes and no LF
/// refuses the line. The largest line of each control link type is far
/// under it. A test holds that.
// Decision CH43 rule 1: the cap is 64 KiB. That is 32 times the largest
// line, a `ControlReady` of about 1.7 KiB. A change of the value later is
// a change of the framing, so it needs a new `GUEST_INIT_PROTOCOL` once
// guest init is deployed (decision CH14).
pub const CONTROL_LINE_CAP: usize = 64 << 10;

/// The graceful stop. It carries nothing.
///
/// It is a type of its own so that the generated Python model is a class
/// with no field, as `TurnStart` is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct ControlStop {}

object_only!(ControlStop);

/// What the vmm sends on control link 1200 after `ControlHello` (decision
/// CH36 G11).
///
/// `stop` starts the graceful stop of decision CH19 rule 3. Guest init
/// stops the facade, for up to 30 s when a turn runs. It then syncs the
/// disks and powers the VM off. The vmm sends SIGTERM, then SIGKILL, only
/// when the VM does not exit in time, because a kill is a power cut for
/// the guest.
///
/// On the wire the variant name is the one key of an object, and each
/// variant has a `title` for its class name. An externally tagged enum has
/// no unit variant, so `stop` holds an empty object (decision CH22 rule 3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum ControlCommand {
    /// Stop the facade, sync and power off.
    #[cfg_attr(test, schemars(title = "ControlStopCommand"))]
    Stop(ControlStop),
}

family_name!(
    /// The name of a granted skill: `SKILL_NAME` of `family/grammar.py`,
    /// which has the rule of a family name.
    SkillName
);

/// The rule of `is_model_alias` as a regular expression, for the JSON Schema.
/// It is `MODEL_ALIAS` of `family/grammar.py` with `$` for `\Z`.
/// `wire/tests` holds the two equal.
#[cfg(test)]
const MODEL_ALIAS_PATTERN: &str = "^[a-z0-9][a-z0-9._/-]*$";

/// Whether `text` is a LiteLLM alias: a lowercase letter or a digit, then
/// lowercase letters, digits, `.`, `_`, `/` or `-`.
fn is_model_alias(text: &str) -> bool {
    let mut characters = text.chars();

    characters
        .next()
        .is_some_and(|first| matches!(first, 'a'..='z' | '0'..='9'))
        && characters
            .all(|character| matches!(character, 'a'..='z' | '0'..='9' | '.' | '_' | '/' | '-'))
}

checked_text!(
    /// The LiteLLM alias of the family's model: `model.router` of the family
    /// file, which caregiver writes as `model_alias` in `config/runtime.json`
    /// (`steps.py:242`).
    ModelAlias,
    is_model_alias,
    "a model alias is a lowercase letter or digit, then lowercase letters, digits, '.', '_', '/' or '-'",
    "pattern" = MODEL_ALIAS_PATTERN
);

/// How the family's instructions meet the agent's own prompt: `SystemPrompt`
/// of `family/grammar.py`. On the wire it is the name of a mode, a JSON
/// string. The schema holds it inline in `FamilyConfig`, so the generated
/// Python is a field of a model, which `creche_wire.read` takes. Its `title`
/// names the generated class, so a later wire type cannot rename it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(inline, title = "SystemPrompt"))]
pub enum SystemPrompt {
    Append,
    Replace,
}

/// The built-in sandbox tools of Flue that the family grants: `sandbox_tools`
/// of the family file, a subset of `SandboxTool` of `family/grammar.py`. The
/// validator refuses a tool listed twice, so a grant is a set, and each tool
/// is one key here. An empty grant gives no sandbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[expect(
    clippy::struct_excessive_bools,
    reason = "one bool per tool of a closed set of six"
)]
pub struct SandboxGrant {
    /// `bash`, the one way to run commands.
    pub bash: bool,
    /// `edit`.
    pub edit: bool,
    /// `glob`.
    pub glob: bool,
    /// `grep`.
    pub grep: bool,
    /// `read`.
    pub read: bool,
    /// `write`.
    pub write: bool,
}

object_only!(SandboxGrant);

/// The granted skills: each name, and the text of its `SKILL.md`, in the
/// order of the wire. caregiver writes `config/skills/<name>/SKILL.md` and
/// no other file (`config_mount.py:76-81`).
///
/// On the wire it is a JSON object from name to text. A name given twice is
/// refused, as `creche_wire.read` refuses it. The order is kept, as the
/// generated Python model keeps it.
///
/// The vmm writes the skills in ascending name order, the order the agent
/// loads them in today (`agent/src/skills.ts:122`). A reader compares two
/// sets of skills by name, not by order, as `==` here and a Python `dict` do.
#[derive(Debug, Clone)]
pub struct FamilySkills(Vec<(SkillName, String)>);

impl PartialEq for FamilySkills {
    fn eq(&self, other: &Self) -> bool {
        self.0.len() == other.0.len() && self.0.iter().all(|skill| other.0.contains(skill))
    }
}

impl Eq for FamilySkills {}

impl FamilySkills {
    /// Each skill's name and `SKILL.md` text, in the order of the wire.
    #[must_use]
    pub fn skills(&self) -> &[(SkillName, String)] {
        &self.0
    }
}

impl Serialize for FamilySkills {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_map(self.0.iter().map(|(name, text)| (name, text)))
    }
}

impl<'de> Deserialize<'de> for FamilySkills {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct SkillsVisitor;

        impl<'de> Visitor<'de> for SkillsVisitor {
            type Value = FamilySkills;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a JSON object from skill name to SKILL.md text")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<FamilySkills, A::Error> {
                let mut skills: Vec<(SkillName, String)> = Vec::new();
                while let Some((name, text)) = map.next_entry::<SkillName, String>()? {
                    if skills.iter().any(|(seen, _)| *seen == name) {
                        return Err(serde::de::Error::custom("a skill is named twice"));
                    }
                    skills.push((name, text));
                }
                Ok(FamilySkills(skills))
            }
        }

        deserializer.deserialize_map(SkillsVisitor)
    }
}

#[cfg(test)]
impl schemars::JsonSchema for FamilySkills {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "FamilySkills".into()
    }

    fn json_schema(generator: &mut schemars::SchemaGenerator) -> schemars::Schema {
        schemars::json_schema!({
            "description": "The granted skills: each name, and the text of its `SKILL.md`.",
            "type": "object",
            "propertyNames": generator.subschema_for::<SkillName>(),
            "additionalProperties": { "type": "string" },
        })
    }
}

/// The family's part of `config/` that the guest reads: what caregiver writes
/// in `write_config` (`steps.py:229-249`). Instructions, skills, sandbox
/// tools and the model change at the next prompt of a running session
/// (decision CH5c).
///
/// `shell` of `runtime.json` is not here: the agent reads it and grants
/// nothing with it (`agent/src/runtime-config.ts:18`).
///
/// `model_live_at` of `runtime.json` is not here either. The vmm holds the
/// reply on it (`FamilyStateReply`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct FamilyConfig {
    /// The text of `config/instructions.md`. The agent trims it, and an empty
    /// text gives no instructions, as today (`agent/src/agent.ts:72-85`).
    pub instructions: String,
    /// The alias of the model.
    pub model_alias: ModelAlias,
    /// The granted sandbox tools.
    pub sandbox_tools: SandboxGrant,
    /// The granted skills.
    pub skills: FamilySkills,
    /// How the instructions meet the agent's own prompt. caregiver leaves
    /// `system_prompt` out of `runtime.json` for `append`
    /// (`config_mount.py:54`), so the vmm reads a missing key as `append`.
    pub system_prompt: SystemPrompt,
}

object_only!(FamilyConfig);

/// The name of the family VM shape file in the family's `config/`
/// (decision CH36 G6). caregiver writes it in the same folder swap as the
/// rest of `config/`, and the vmm reads it. The guest never reads it.
// CONTRACT-QUESTION: decision CH36 G6 puts the shape in a separate file of
// `config/` and gives the file no name. The reading here is `shape.json`.
// Contract 01 §6.1 lists the entries of `config/` and needs an amendment
// for it, as decision CH23-1 amended it for `model_live_at`. A change of
// the name later changes caregiver and the vmm together.
pub const FAMILY_SHAPE_FILE: &str = "shape.json";

/// A size of the family VM shape, with the range of `family/grammar.py`.
/// On the wire it is a JSON integer from `$min` to `$max`, and any other
/// value is refused.
macro_rules! shape_size {
    ($(#[$doc:meta])* $name:ident, $error:ident, $int:ty, $min:literal, $max:literal, $rule:literal) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        #[serde(try_from = "u64", into = "u64")]
        #[cfg_attr(test, derive(schemars::JsonSchema))]
        #[cfg_attr(test, schemars(extend("minimum" = $min, "maximum" = $max)))]
        pub struct $name($int);

        #[doc = concat!("Why an integer is not a `", stringify!($name), "`: it is outside its range.")]
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $error;

        impl $name {
            /// The smallest value.
            pub const MIN: $int = $min;
            /// The largest value.
            pub const MAX: $int = $max;

            /// The value.
            #[must_use]
            pub fn get(self) -> $int {
                self.0
            }
        }

        impl TryFrom<u64> for $name {
            type Error = $error;

            fn try_from(value: u64) -> Result<Self, Self::Error> {
                match <$int>::try_from(value) {
                    Ok(value) if (Self::MIN..=Self::MAX).contains(&value) => Ok(Self(value)),
                    _ => Err($error),
                }
            }
        }

        impl From<$name> for u64 {
            fn from(size: $name) -> Self {
                u64::from(size.0)
            }
        }

        impl fmt::Display for $error {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str($rule)
            }
        }

        impl std::error::Error for $error {}
    };
}

shape_size!(
    /// The vCPUs of the family VM: `sandbox.cpus` of the family file,
    /// `CPUS_MIN` to `CPUS_MAX` of `family/grammar.py`. The vmm gives the
    /// guest this many vCPUs and the scope this many CPUs of quota
    /// (decision CH8-5).
    FamilyCpus,
    FamilyCpusError,
    u8,
    1,
    8,
    "the vCPUs of a family VM are 1 to 8"
);

shape_size!(
    /// The guest RAM of the family VM in MiB: `sandbox.memory` of the
    /// family file as `memory_mb` of `family/grammar.py` reads it, which
    /// counts `1g` as 1024. The range is `MEMORY_MIN_MB` to
    /// `MEMORY_MAX_MB` there. The scope gets a memory cap above it
    /// (decision CH8-5).
    FamilyMemoryMib,
    FamilyMemoryMibError,
    u16,
    256,
    16384,
    "the guest RAM of a family VM is 256 to 16384 MiB"
);

/// The image flavor of the family VM: `sandbox.image` of the family file,
/// `SandboxFlavor` of `family/grammar.py`. It names a flavor, never an
/// image (contract 01 §3.9). On the wire it is the name of a flavor, a JSON
/// string. The schema holds it inline in `FamilyShape`, as `SystemPrompt`
/// is held in `FamilyConfig`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(inline, title = "SandboxFlavor"))]
pub enum SandboxFlavor {
    Base,
    Python,
}

/// The family VM shape: the file `FAMILY_SHAPE_FILE` in the family's
/// `config/` (decision CH36 G6). caregiver writes it from the family file.
/// The vmm reads it before it starts a VM of the family (decision CH8-5).
///
/// It is a file of its own, not a part of `runtime.json`, so the guest
/// reads none of it. `family/grammar.py` stays the authority for the
/// ranges and the flavors (decision CH23 rule 5).
/// `wire/tests/test_wire_grammar.py` holds them equal. No route reaches
/// the type, so it needs no new `HOST_API_PROTOCOL` (decision CH33 rule 8).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct FamilyShape {
    /// The vCPUs.
    pub cpus: FamilyCpus,
    /// The image flavor: `sandbox.image` of the family file.
    pub flavor: SandboxFlavor,
    /// The guest RAM in MiB.
    pub memory_mib: FamilyMemoryMib,
}

object_only!(FamilyShape);

/// A secret text that `$check` accepts. On the wire it is a JSON string with
/// the schema pattern `$pattern`, and a string that breaks the rule is
/// refused. Its `Debug` writes no value and it has no `Display`, so a log
/// line or an error never holds the secret (invariant 13). The generated
/// Python model does not keep this rule (`wire/AGENTS.md`).
macro_rules! secret_text {
    ($(#[$doc:meta])* $name:ident, $check:expr, $rule:literal, $pattern:expr) => {
        $(#[$doc])*
        #[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        #[cfg_attr(test, derive(schemars::JsonSchema))]
        #[cfg_attr(test, schemars(extend("pattern" = $pattern)))]
        pub struct $name(String);

        impl $name {
            /// The secret as text, for the one header that carries it.
            #[must_use]
            pub fn reveal(&self) -> &str {
                &self.0
            }
        }

        impl TryFrom<String> for $name {
            type Error = TextError;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                if !$check(&text) {
                    return Err(TextError($rule));
                }
                Ok(Self(text))
            }
        }

        impl From<$name> for String {
            fn from(secret: $name) -> Self {
                secret.0
            }
        }

        impl fmt::Debug for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(concat!(stringify!($name), "(..)"))
            }
        }
    };
}

/// The length of a family token: 32 bytes as lowercase hex.
const PEP_TOKEN_LEN: usize = 64;

secret_text!(
    /// The family's LiteLLM key, `litellm_key` of `creds.json`. caregiver
    /// keeps only a key that starts with `sk-` (`litellm_keys.py:155`). The
    /// rest is visible ASCII, with no space or control character, so the key
    /// cannot break the header that carries it. Decision CH31 keeps this rule
    /// and gives caregiver's check the same rule in a later change.
    LitellmKey,
    |text: &str| text
        .strip_prefix("sk-")
        .is_some_and(|rest| rest.chars().all(|character| matches!(character, '!'..='~'))),
    "a LiteLLM key is sk- and then visible ASCII",
    "^sk-[!-~]*$"
);

secret_text!(
    /// The family token for the chaperone, `pep_token` of `creds.json`: 32
    /// bytes as 64 lowercase hex digits, the one form the chaperone takes
    /// (`credentials.py:21-25`).
    PepToken,
    |text: &str| text.len() == PEP_TOKEN_LEN
        && text.chars().all(|character| matches!(character, '0'..='9' | 'a'..='f')),
    "a family token is 64 lowercase hex digits",
    "^[0-9a-f]{64}$"
);

/// The family's part of `creds.json` that the guest reads (decision CH23
/// default 1). `epoch`, `written_at`, `previous_pep_token` and
/// `previous_expires_at` stay on the host, so the old token never sits in
/// guest memory or in a snapshot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct FamilyCreds {
    /// The key for the LiteLLM plane.
    pub litellm_key: LitellmKey,
    /// The token for the chaperone plane.
    pub pep_token: PepToken,
}

object_only!(FamilyCreds);

/// The whole family state of one pull: the full typed state, every time
/// (decision CH23). The facade compares the fields itself.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct FamilyState {
    /// What the vmm read from `config/`.
    pub config: FamilyConfig,
    /// What the vmm read from `creds.json`.
    pub creds: FamilyCreds,
}

object_only!(FamilyState);

/// The reply of a pull when `creds.json` is absent. It carries nothing.
/// When `config/` is absent too, this reply wins.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct CredsMissing {}

object_only!(CredsMissing);

/// The reply of a pull when `config/` is absent. It carries nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct ConfigMissing {}

object_only!(ConfigMissing);

/// The reply of a pull when `creds.json` or `config/` is present and does
/// not read as `FamilyState`. It carries nothing, so it holds no value of
/// the files. The vmm names the file that failed in its host log only.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct StateNotValid {}

object_only!(StateNotValid);

/// The reply of a pull when the files read, and the `state` reply is over
/// the cap of 256 KiB (decision CH31). It carries nothing, not even the
/// size. The vmm writes the size in its host log only.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct StateTooLarge {}

object_only!(StateTooLarge);

/// What the vmm answers on host port 1100 when the facade pulls the family
/// state, before each prompt (decisions CH5a, CH5b, CH23). The vmm reads
/// caregiver's `creds.json` and `config/` at each pull.
///
/// A read failure is a typed reply, and the link stays open (decision CH23
/// default 2). The facade fails that prompt and keeps running. At start it
/// pulls again with a backoff, and never exits on a typed error. When
/// `creds.json` and `config/` are both absent, `creds_missing` wins
/// (decision CH31).
///
/// A reply text has a cap of 256 KiB (decision CH23 default 4). The cap is
/// a rule of the read surface, not of this type: the vmm's writer and the
/// `cap` of `creche_wire.read` hold it, because JSON Schema counts code
/// points, not bytes. A `state` reply over the cap is `too_large`, not
/// `not_valid` (decision CH31).
///
/// The vmm reads `model_live_at` from `config/runtime.json` (decision
/// CH23-1). It holds the `state` reply until the host time passes that
/// time, for at most 10 s (decision CH31). No wire type carries
/// `model_live_at`, and the guest needs no clock for it. A held pull can
/// take up to 10 s longer.
///
/// On the wire the variant name is the one key of an object, and each
/// variant has a `title` for its class name.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum FamilyStateReply {
    /// The vmm read the whole state.
    #[cfg_attr(test, schemars(title = "StateReply"))]
    State(FamilyState),
    /// `creds.json` is absent.
    #[cfg_attr(test, schemars(title = "CredsMissingReply"))]
    CredsMissing(CredsMissing),
    /// `config/` is absent.
    #[cfg_attr(test, schemars(title = "ConfigMissingReply"))]
    ConfigMissing(ConfigMissing),
    /// A file is present and does not read.
    #[cfg_attr(test, schemars(title = "NotValidReply"))]
    NotValid(StateNotValid),
    /// The `state` reply is over the cap.
    #[cfg_attr(test, schemars(title = "TooLargeReply"))]
    TooLarge(StateTooLarge),
}

#[cfg(test)]
mod tests {
    use super::{
        CONTROL_LINE_CAP, ControlReady, FamilySkills, HOST_API_BODY_CAP, JS_INTEGER_MAX,
        ListCursor, LitellmKey, PepToken, SESSION_ID_PATTERN, SessionId, SessionIdError,
        SessionPage,
    };

    const GOOD: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";

    /// A page of the largest row is under 1/32 of the body cap. So a page
    /// that ends early at the cap holds at least one row (decision CH33
    /// rule 2). serde_json writes a control character in 6 bytes, the most
    /// of any character, and a dispatch origin is the longest origin.
    #[test]
    fn a_page_of_the_largest_row_is_far_under_the_body_cap() {
        let name = "a".repeat(31);
        let trigger = serde_json::json!({"dispatch": {"chain": vec![&name; 8], "name": name}});
        let row = serde_json::json!({
            "created_at": JS_INTEGER_MAX,
            "cwd": format!("/{}", "\u{1}".repeat(4095)),
            "family": name,
            "id": GOOD,
            "origin": {"auto": {"job": "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK", "trigger": trigger}},
            "title": "\u{1}".repeat(256),
            "updated_at": JS_INTEGER_MAX,
        });
        let page = SessionPage {
            next: Some(ListCursor::try_from(JS_INTEGER_MAX).unwrap()),
            rows: vec![serde_json::from_value(row).unwrap()],
        };
        let size = serde_json::to_vec(&page).unwrap().len();

        assert!(size < HOST_API_BODY_CAP / 32, "{size} bytes");
    }

    /// The largest line of the control link is under 1/32 of its cap
    /// (decision CH36 G11). A running facade with a title of 256 control
    /// characters is the largest `ControlReady`, and `ControlReady` is the
    /// largest type of the link. serde_json writes no LF in a value, so
    /// one value fills one line.
    #[test]
    fn the_largest_control_line_is_far_under_its_cap() {
        let ready = serde_json::json!({"facade": {"running": {
            "last_turn_end": {"title": "\u{1}".repeat(256), "updated_at": JS_INTEGER_MAX},
            "pid": u32::MAX,
            "starts": JS_INTEGER_MAX,
            "turn_running": false,
        }}});
        let ready: ControlReady = serde_json::from_value(ready).unwrap();
        let line = serde_json::to_vec(&ready).unwrap();

        assert!(!line.contains(&b'\n'));
        assert!(line.len() < CONTROL_LINE_CAP / 32, "{} bytes", line.len());
    }

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
    fn two_sets_of_skills_compare_by_name_not_by_order() {
        let read = |json: &str| serde_json::from_str::<FamilySkills>(json).unwrap();

        assert_eq!(
            read(r#"{"ha":"x","vault":"y"}"#),
            read(r#"{"vault":"y","ha":"x"}"#)
        );
        assert_ne!(
            read(r#"{"ha":"x","vault":"y"}"#),
            read(r#"{"ha":"x","vault":"z"}"#)
        );
        assert_ne!(read(r#"{"ha":"x"}"#), read(r#"{"ha":"x","vault":"y"}"#));
    }

    #[test]
    fn the_debug_of_a_secret_holds_no_value() {
        let key = LitellmKey::try_from("sk-test-key-0001".to_owned()).unwrap();
        let token = PepToken::try_from("9c1e".repeat(16)).unwrap();

        assert_eq!(format!("{key:?} {token:?}"), "LitellmKey(..) PepToken(..)");
    }

    #[test]
    fn the_schema_of_a_session_id_is_a_string_with_the_pattern() {
        let schema = serde_json::to_value(schemars::schema_for!(SessionId)).unwrap();

        assert_eq!(schema["type"], "string");
        assert_eq!(schema["pattern"], SESSION_ID_PATTERN);
    }
}
