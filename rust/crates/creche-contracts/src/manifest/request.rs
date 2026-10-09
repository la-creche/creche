//! The release request (`stage7-releases.md` §2.3 and §3.2, contract 06 §9).
//!
//! A requester states intent and nothing else: which components, and a
//! version for each. Root reads each byte of a request file as hostile.
//!
//! The requester and the executor share this module. [`Request::to_bytes`] is
//! the one writer and [`Request::parse`] is the one parser.
//! [`Request::plan`] writes the draft of a requester and then parses the
//! bytes, so a requester gets the refusal that root gives for the same bytes.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::json::{self, Charset, Value};
use super::{MAX_REQUEST_COMPONENTS, RefusalCode};
use crate::ids::{ComponentName, FamilyName, FamilyNameError, SessionId, Ulid, Version};

/// The largest request file, in bytes. A complete request is about 300
/// bytes. The parser applies the cap before the parse.
pub const MAX_REQUEST_BYTES: usize = 4096;

/// What a request writes in place of a version for the newest released
/// version.
const LATEST: &str = "latest";

const KIND_RELEASE: &str = "release";
const KIND_ROLLBACK: &str = "rollback";

/// The seven keys of a request, in the order of the writer.
const REQUEST_KEYS: [&str; 7] = [
    "id",
    "kind",
    "components",
    "rollback_of",
    "requested_by",
    "requester_session",
    "ts",
];

/// The version that a request asks for.
///
/// The set is closed by `stage7-releases.md` §2.3. A reader refuses each
/// other text.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Wanted {
    /// The newest released version. Root reads the tags to find it.
    Latest,
    /// This version.
    Version(Version),
}

impl Wanted {
    /// The value as the text of a request file.
    #[must_use]
    pub fn as_str(&self) -> &str {
        match self {
            Self::Latest => LATEST,
            Self::Version(version) => version.as_str(),
        }
    }
}

/// What a request asks root to do.
///
/// The set is closed by `stage7-releases.md` §2.3. A reader refuses a request
/// with another kind. A rollback holds the id of the release that it undoes,
/// so a release with a target and a rollback with no target have no value of
/// this type.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum RequestKind {
    /// Move each named component to its version.
    Release,
    /// Undo the release with this id. The executor parses the kind and
    /// refuses it at its first step: the reversal is not built.
    Rollback(Ulid),
}

impl RequestKind {
    /// The kind as the word of a request file.
    #[must_use]
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::Release => KIND_RELEASE,
            Self::Rollback(_) => KIND_ROLLBACK,
        }
    }

    /// The id of the release that a rollback undoes.
    #[must_use]
    pub const fn rollback_of(&self) -> Option<&Ulid> {
        match self {
            Self::Release => None,
            Self::Rollback(target) => Some(target),
        }
    }
}

// CONTRACT-QUESTION: contract 06 §9 says that `requested_by` is a family name
// or `human`. `stage7-releases.md` §2.3 adds `follow` and `ci`. The Python
// code checks the grammar of a name and no list of words. This type does the
// same: each word of the grammar can be a family name. A closed list costs a
// release of `handover` for each new requester.
/// Who asks for a release: a family name, `human`, `follow` or `ci`
/// (`stage7-releases.md` §2.3). The grammar is the grammar of a family name:
/// `[a-z][a-z0-9-]{1,30}`.
///
/// The value is for the record. Nothing decides on it.
///
/// ```
/// use creche_contracts::manifest::Requester;
///
/// let requester: Requester = "agent-control".parse()?;
/// assert_eq!(requester.as_str(), "agent-control");
/// # Ok::<(), creche_contracts::manifest::RequesterError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::Requester;
///
/// let requester = Requester("Not A Name".parse().unwrap());
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Requester(FamilyName);

impl Requester {
    /// The requester as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        self.0.as_str()
    }
}

impl FromStr for Requester {
    type Err = RequesterError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        text.parse().map(Self).map_err(RequesterError)
    }
}

impl fmt::Display for Requester {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Why a text is not a requester: the rule of the name grammar that it
/// breaks.
///
/// Only the parse of a [`Requester`] makes a value.
///
/// ```
/// use creche_contracts::ids::FamilyNameError;
/// use creche_contracts::manifest::{Requester, RequesterError};
///
/// let error: RequesterError = "Human".parse::<Requester>().unwrap_err();
/// assert_eq!(error.fault(), FamilyNameError::BadFirstByte);
/// ```
///
/// Code outside this module cannot build an error from a rule:
///
/// ```compile_fail,E0423
/// use creche_contracts::ids::FamilyNameError;
/// use creche_contracts::manifest::{Requester, RequesterError};
///
/// let error = RequesterError(FamilyNameError::BadFirstByte);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RequesterError(FamilyNameError);

impl RequesterError {
    /// The rule of the name grammar that the text breaks.
    #[must_use]
    pub const fn fault(&self) -> FamilyNameError {
        self.0
    }
}

impl fmt::Display for RequesterError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "a requester is a name: {}", self.0)
    }
}

impl Error for RequesterError {}

/// The clock of a requester: Unix seconds, finite and not below zero
/// (`stage7-releases.md` §2.3, the field `ts`).
///
/// ```
/// use creche_contracts::manifest::Timestamp;
///
/// let time = Timestamp::new(1758153590.5)?;
/// assert_eq!(time.get(), 1758153590.5);
/// assert!(Timestamp::new(f64::NAN).is_err());
/// assert!(Timestamp::new(-1.0).is_err());
/// # Ok::<(), creche_contracts::manifest::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::Timestamp;
///
/// let time = Timestamp(f64::NAN);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, PartialOrd)]
pub struct Timestamp(f64);

impl Timestamp {
    /// Checks that `seconds` is finite and not below zero.
    ///
    /// # Errors
    ///
    /// Gives [`TimestampError`] for each other number.
    pub fn new(seconds: f64) -> Result<Self, TimestampError> {
        if !seconds.is_finite() || seconds < 0.0 {
            return Err(TimestampError);
        }

        Ok(Self(seconds))
    }

    /// The time in seconds.
    #[must_use]
    pub const fn get(self) -> f64 {
        self.0
    }
}

/// A time that is not finite, or is below zero.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimestampError;

impl fmt::Display for TimestampError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a time is a finite count of seconds that is not below zero")
    }
}

impl Error for TimestampError {}

/// The arguments of a requester, before root's parser checks them.
///
/// This is the raw type of [`Request`]. Each field but the id is a text as
/// the caller holds it. [`Request::plan`] makes the valid type.
#[derive(Debug, Clone, PartialEq)]
pub struct Draft {
    /// The id of the request. The requester mints it with [`mint_ulid`].
    pub id: Ulid,
    /// The kind: `release` or `rollback`.
    pub kind: String,
    /// Each component name and the version that the requester wants.
    pub components: BTreeMap<String, String>,
    /// The id of the release that a rollback undoes.
    pub rollback_of: Option<String>,
    /// Who asks.
    pub requested_by: String,
    /// The session that asks.
    pub requester_session: Option<String>,
    /// The clock of the requester, in seconds.
    pub now: f64,
}

// CONTRACT-QUESTION: `stage7-releases.md` §2.3 calls the id of a request a
// lower-case ULID. Contract 06 §9 and contract 02 §2 call each ULID upper
// case, and the Python parser takes upper case. This type takes upper case:
// the id is `ids::Ulid`. A change to lower case costs a second ULID type and
// a new name for each file of the spool.
/// One valid release request.
///
/// A value exists only through [`Request::parse`] and [`Request::plan`]. Each
/// value has 1 to 8 components. The bytes of a value from [`Request::plan`]
/// fit [`MAX_REQUEST_BYTES`], so the parser reads them.
///
/// The parser does not read the bytes of each value from [`Request::parse`].
/// The writer puts a space after each `,` and each `:`. For a file near the
/// largest size with no such spaces, the bytes of its value are past the
/// largest size. The Python writer and parser do the same.
///
/// ```
/// use creche_contracts::manifest::{Request, RequestKind};
///
/// let id = "01K5J8M2Q7V3X9R4T6N0B8C2DE".parse()?;
/// let bytes = br#"{"id": "01K5J8M2Q7V3X9R4T6N0B8C2DE", "kind": "release", "components": {"chaperone": "2.1.0"}, "rollback_of": null, "requested_by": "human", "requester_session": null, "ts": 1758153590.0}"#;
/// let request = Request::parse(bytes, &id)?;
/// assert_eq!(request.kind(), &RequestKind::Release);
/// assert_eq!(request.to_bytes(), bytes);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change a field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::Request;
///
/// fn clear(mut request: Request) {
///     request.components.clear();
/// }
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    id: Ulid,
    kind: RequestKind,
    components: BTreeMap<ComponentName, Wanted>,
    requested_by: Requester,
    requester_session: Option<SessionId>,
    ts: Timestamp,
}

impl Request {
    /// The id of the request, which is the stem of its file name.
    #[must_use]
    pub fn id(&self) -> &Ulid {
        &self.id
    }

    /// What the request asks root to do.
    #[must_use]
    pub fn kind(&self) -> &RequestKind {
        &self.kind
    }

    /// Each component and the version that the request wants, in the order of
    /// the names. The reader does not check a name against the catalog: the
    /// resolver does.
    #[must_use]
    pub fn components(&self) -> &BTreeMap<ComponentName, Wanted> {
        &self.components
    }

    /// Who asks. Nothing decides on it.
    #[must_use]
    pub fn requested_by(&self) -> &Requester {
        &self.requested_by
    }

    /// The session that asks, for the audit.
    #[must_use]
    pub fn requester_session(&self) -> Option<&SessionId> {
        self.requester_session.as_ref()
    }

    /// The clock of the requester.
    #[must_use]
    pub const fn ts(&self) -> Timestamp {
        self.ts
    }

    /// The bytes of the request file, as the Python writer writes them.
    ///
    /// Root writes its own copy of a request with this function too, from
    /// the fields. It never copies the bytes of a requester.
    #[must_use]
    pub fn to_bytes(&self) -> Vec<u8> {
        let components = self
            .components
            .iter()
            .map(|(name, wanted)| (name.as_str(), wanted.as_str()));
        let body = Body {
            id: self.id.as_str(),
            kind: self.kind.as_str(),
            rollback_of: self.kind.rollback_of().map(Ulid::as_str),
            requested_by: self.requested_by.as_str(),
            requester_session: self.requester_session.as_ref().map(SessionId::as_str),
            ts: self.ts.get(),
        };

        encode(&body, components).into_bytes()
    }

    /// Parses the bytes of one request file. `id` is the id of the file name.
    ///
    /// The failure action of a caller: move the file to `rejected/`, write a
    /// ledger entry with the code `request` and the detail, and change
    /// nothing on the host (`stage7-releases.md` §3.2).
    ///
    /// # Errors
    ///
    /// Gives the first [`RequestError`], in the order of the checks of the
    /// Python parser.
    pub fn parse(raw: &[u8], id: &Ulid) -> Result<Self, RequestError> {
        if raw.len() > MAX_REQUEST_BYTES {
            return Err(RequestError::TooLarge);
        }

        let text = std::str::from_utf8(raw).map_err(|_| RequestError::NotObject)?;
        let Ok(Value::Object(body)) = json::parse(text) else {
            return Err(RequestError::NotObject);
        };
        let same_keys = body.len() == REQUEST_KEYS.len()
            && REQUEST_KEYS.iter().all(|key| body.get(key).is_some());
        if !same_keys {
            return Err(RequestError::KeysDiffer);
        }

        let field = |key: &str| body.get(key).unwrap_or(&Value::Null);
        if !matches!(field("id"), Value::Text(text) if text == id.as_str()) {
            return Err(RequestError::IdDiffers);
        }

        let rollback = match field("kind") {
            Value::Text(kind) if kind == KIND_RELEASE => false,
            Value::Text(kind) if kind == KIND_ROLLBACK => true,
            _ => return Err(RequestError::Malformed(RequestField::Kind)),
        };
        let components = read_components(field("components"))?;
        let target: Option<Ulid> = optional_text(field("rollback_of"), RequestField::RollbackOf)?;
        let kind = match (rollback, target) {
            (false, None) => RequestKind::Release,
            (false, Some(_)) => return Err(RequestError::RollbackOnRelease),
            (true, None) => return Err(RequestError::RollbackUnset),
            (true, Some(target)) => RequestKind::Rollback(target),
        };
        let requested_by = required_text(field("requested_by"), RequestField::RequestedBy)?;
        let requester_session =
            optional_text(field("requester_session"), RequestField::RequesterSession)?;
        let ts = match field("ts") {
            Value::Number(number) => Timestamp::new(*number).ok(),
            _ => None,
        }
        .ok_or(RequestError::Malformed(RequestField::Ts))?;

        Ok(Self {
            id: id.clone(),
            kind,
            components,
            requested_by,
            requester_session,
            ts,
        })
    }

    /// Makes the request of a requester: writes the draft as a request file,
    /// and parses the bytes as root does.
    ///
    /// # Errors
    ///
    /// Gives the [`RequestError`] that root gives for the same bytes.
    pub fn plan(draft: &Draft) -> Result<Self, RequestError> {
        let components = draft
            .components
            .iter()
            .map(|(name, wanted)| (name.as_str(), wanted.as_str()));
        let body = Body {
            id: draft.id.as_str(),
            kind: &draft.kind,
            rollback_of: draft.rollback_of.as_deref(),
            requested_by: &draft.requested_by,
            requester_session: draft.requester_session.as_deref(),
            ts: draft.now,
        };
        let raw = encode(&body, components);

        Self::parse(raw.as_bytes(), &draft.id)
    }
}

/// The fields of a request file that are no component, as texts.
struct Body<'a> {
    id: &'a str,
    kind: &'a str,
    rollback_of: Option<&'a str>,
    requested_by: &'a str,
    requester_session: Option<&'a str>,
    ts: f64,
}

fn push_key(out: &mut String, key: &str) {
    json::push_string(out, key, Charset::Ascii);
    out.push_str(": ");
}

fn push_optional(out: &mut String, text: Option<&str>) {
    match text {
        Some(text) => json::push_string(out, text, Charset::Ascii),
        None => out.push_str("null"),
    }
}

/// The text of a request file: what Python `json.dumps` gives for the seven
/// fields, in the order of [`REQUEST_KEYS`].
fn encode<'a>(body: &Body<'_>, components: impl Iterator<Item = (&'a str, &'a str)>) -> String {
    let [
        id,
        kind,
        components_key,
        rollback_of,
        requested_by,
        requester_session,
        ts,
    ] = REQUEST_KEYS;
    let mut out = String::from("{");
    push_key(&mut out, id);
    json::push_string(&mut out, body.id, Charset::Ascii);
    out.push_str(", ");
    push_key(&mut out, kind);
    json::push_string(&mut out, body.kind, Charset::Ascii);
    out.push_str(", ");
    push_key(&mut out, components_key);
    out.push('{');
    for (index, (name, wanted)) in components.enumerate() {
        if index > 0 {
            out.push_str(", ");
        }

        push_key(&mut out, name);
        json::push_string(&mut out, wanted, Charset::Ascii);
    }

    out.push_str("}, ");
    push_key(&mut out, rollback_of);
    push_optional(&mut out, body.rollback_of);
    out.push_str(", ");
    push_key(&mut out, requested_by);
    json::push_string(&mut out, body.requested_by, Charset::Ascii);
    out.push_str(", ");
    push_key(&mut out, requester_session);
    push_optional(&mut out, body.requester_session);
    out.push_str(", ");
    push_key(&mut out, ts);
    out.push_str(&json::float_text(body.ts));
    out.push('}');

    out
}

fn required_text<T: FromStr>(value: &Value, field: RequestField) -> Result<T, RequestError> {
    match value {
        Value::Text(text) => text.parse().map_err(|_| RequestError::Malformed(field)),
        _ => Err(RequestError::Malformed(field)),
    }
}

fn optional_text<T: FromStr>(
    value: &Value,
    field: RequestField,
) -> Result<Option<T>, RequestError> {
    match value {
        Value::Null => Ok(None),
        _ => required_text(value, field).map(Some),
    }
}

/// `components`: 1 to 8 entries, each a component name and a version.
fn read_components(value: &Value) -> Result<BTreeMap<ComponentName, Wanted>, RequestError> {
    let Value::Object(entries) = value else {
        return Err(RequestError::ComponentsNotObject);
    };
    if entries.is_empty() {
        return Err(RequestError::ComponentsEmpty);
    }

    if entries.len() > MAX_REQUEST_COMPONENTS {
        return Err(RequestError::ComponentsTooMany);
    }

    let mut wanted = BTreeMap::new();
    // The Python parser reads the entries in the order of the names.
    for key in entries.sorted_keys() {
        let name: ComponentName = key.parse().map_err(|_| RequestError::ComponentName)?;
        let version = match entries.get(key) {
            Some(Value::Text(text)) if text == LATEST => Wanted::Latest,
            Some(Value::Text(text)) => text
                .parse()
                .map(Wanted::Version)
                .map_err(|_| RequestError::ComponentVersion)?,
            _ => return Err(RequestError::ComponentVersion),
        };
        wanted.insert(name, version);
    }

    Ok(wanted)
}

/// A field of a request whose refusal says only that the field is malformed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestField {
    /// `kind`.
    Kind,
    /// `rollback_of`.
    RollbackOf,
    /// `requested_by`.
    RequestedBy,
    /// `requester_session`.
    RequesterSession,
    /// `ts`.
    Ts,
}

impl RequestField {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Kind => "kind",
            Self::RollbackOf => "rollback_of",
            Self::RequestedBy => "requested_by",
            Self::RequesterSession => "requester_session",
            Self::Ts => "ts",
        }
    }
}

/// Why bytes are not a release request.
///
/// The text of each variant is a fixed string. It can go to the ledger, so it
/// never shows the file (`stage7-releases.md` §3.2 rule 4).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestError {
    /// The file is larger than [`MAX_REQUEST_BYTES`].
    TooLarge,
    /// The bytes are not UTF-8, not JSON, or not a JSON object.
    NotObject,
    /// The keys are not exactly the seven keys of a request.
    KeysDiffer,
    /// `id` is not the id of the file name.
    IdDiffers,
    /// A field does not have its form.
    Malformed(RequestField),
    /// `components` is not an object.
    ComponentsNotObject,
    /// `components` names no component.
    ComponentsEmpty,
    /// `components` names more than 8 components.
    ComponentsTooMany,
    /// A key of `components` is not a component name.
    ComponentName,
    /// A value of `components` is not a version and not `latest`.
    ComponentVersion,
    /// A release names a release to undo.
    RollbackOnRelease,
    /// A rollback names no release to undo.
    RollbackUnset,
}

impl RequestError {
    /// The check that the ledger names: `request`.
    #[must_use]
    pub const fn code(&self) -> RefusalCode {
        RefusalCode::Request
    }

    /// The subject of the refusal: `request`.
    #[must_use]
    pub const fn subject(&self) -> &'static str {
        "request"
    }

    /// The detail of the refusal, as the Python parser writes it.
    #[must_use]
    pub fn detail(&self) -> String {
        self.to_string()
    }
}

impl fmt::Display for RequestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge => write!(f, "is larger than {MAX_REQUEST_BYTES} bytes"),
            Self::NotObject => f.write_str("is not a JSON object"),
            Self::KeysDiffer => f.write_str("keys differ from the request shape"),
            Self::IdDiffers => f.write_str("field 'id' differs from the file name"),
            Self::Malformed(field) => write!(f, "field '{}' is malformed", field.as_str()),
            Self::ComponentsNotObject => f.write_str("field 'components' must be an object"),
            Self::ComponentsEmpty => f.write_str("field 'components' names nothing"),
            Self::ComponentsTooMany => write!(
                f,
                "field 'components' names more than {MAX_REQUEST_COMPONENTS}"
            ),
            Self::ComponentName => f.write_str("field 'components' has a malformed component name"),
            Self::ComponentVersion => f.write_str("field 'components' holds a version or 'latest'"),
            Self::RollbackOnRelease => f.write_str("field 'rollback_of' is set on a release"),
            Self::RollbackUnset => f.write_str("field 'rollback_of' is unset on a rollback"),
        }
    }
}

impl Error for RequestError {}

// --- the mint of a request id ---

/// The alphabet of a ULID: Crockford base32, upper case.
const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// The count of characters of a ULID.
const ULID_CHARS: u32 = 26;

/// The count of bits of one character of a ULID.
const CHAR_BITS: u32 = 5;

/// The count of random bytes of a ULID.
const ENTROPY_BYTES: usize = 10;

/// The count of random bits of a ULID.
const ENTROPY_BITS: u32 = 80;

// CONTRACT-QUESTION: contract 02 §2 gives a ULID 48 bits of milliseconds and
// says nothing about a later time. For such a time the Python requester
// mints 26 characters that hold more than 48 bits of time, and `ids::Ulid`
// accepts that text. `mint_ulid` refuses the time. The first such time is in
// the year 10889, so a change costs nothing today.
/// The first count of milliseconds that does not fit the 48 bits of a ULID.
const MILLISECONDS_END: f64 = 281_474_976_710_656.0;

/// The whole milliseconds of a time, when they fit the 48 bits of a ULID.
#[expect(
    clippy::as_conversions,
    reason = "a float has no other conversion to an integer, and the range check comes first"
)]
fn whole_milliseconds(now: Timestamp) -> Option<u64> {
    let milliseconds = now.get() * 1000.0;
    if !(0.0..MILLISECONDS_END).contains(&milliseconds) {
        return None;
    }

    // The cast cuts the fraction, as Python `int` does.
    Some(milliseconds as u64)
}

/// Mints a request id: 48 bits of milliseconds, then 80 random bits (contract
/// 02 §2).
///
/// The time part sorts, so a later request sorts after an earlier one. The
/// random part keeps two requests of one millisecond apart. The caller gives
/// the random bytes: this crate reads no random source.
///
/// # Errors
///
/// Gives [`MintError`] for a time whose milliseconds do not fit 48 bits.
pub fn mint_ulid(now: Timestamp, entropy: [u8; ENTROPY_BYTES]) -> Result<Ulid, MintError> {
    let milliseconds = whole_milliseconds(now).ok_or(MintError)?;
    let random = entropy
        .iter()
        .fold(0_u128, |random, byte| (random << 8) | u128::from(*byte));
    let value = (u128::from(milliseconds) << ENTROPY_BITS) | random;
    let text: String = (0..ULID_CHARS)
        .rev()
        .filter_map(|place| {
            let digit = usize::try_from((value >> (place * CHAR_BITS)) & 0b1_1111).ok()?;
            CROCKFORD.get(digit).copied().map(char::from)
        })
        .collect();

    text.parse().map_err(|_| MintError)
}

/// A time that does not fit the 48 bits of a ULID.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MintError;

impl fmt::Display for MintError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the milliseconds of the time do not fit the 48 bits of a ULID")
    }
}

impl Error for MintError {}
