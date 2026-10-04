//! The valid status document (contract 05 §2.1), and its writer.
//!
//! [`StatusDocument`] is the document that `caregiver` writes. A value holds
//! only what contract 05 permits. Two ways lead to a value, and both go
//! through [`StatusDocument::new`]:
//!
//! - `StatusDocument::try_from(&raw)` checks each field of a
//!   [`RawStatus`].
//! - A writer fills a [`DocumentParts`] with valid parts.
//!
//! A block of the document is a record, for example [`Sandbox`]. Each field of
//! a record has a type that permits only valid values, and the record adds no
//! rule of its own. Its fields are thus public: no check exists that a public
//! field can skip. [`Fault`] and [`StatusDocument`] have a rule between two
//! fields, so their fields are private.
//!
//! The Python writer checks no field. Where it writes what contract 05 does
//! not state, and a document on the host has that form, the type takes the
//! form. A `CONTRACT-QUESTION` comment marks each such place.

use std::error::Error;
use std::fmt;
use std::num::NonZeroU64;
use std::str::FromStr;

use super::json::{Charset, Integer, Json, JsonKind, Layout, Object};
use super::raw::{
    FAULT_KEYS, Number, RawCredentials, RawFault, RawLimits, RawPep, RawReconcile, RawSandbox,
    RawSpend, RawStatus, RawTriggers, RawValidation, RawWebhook, ReadError, Reader, Slot,
};
use super::time::{Timestamp, TimestampError};
use super::words::{
    ChannelState, FamilyState, FaultCode, FaultSource, Kind, ReconcileStep, RotationState,
    SandboxLifecycle, SandboxPower, SpendSource, SpendWindow, WatchState,
};
use crate::ids::{
    FamilyName, FamilyNameError, SandboxName, SandboxNameError, WebhookName, WebhookNameError,
};

// --- three small types ---

/// The largest count of characters in `first_error` (contract 05 §3.2).
pub const FIRST_ERROR_CHARS_MAX: usize = 200;

/// The first error of a validation report: 200 characters at most (contract
/// 05 §3.2).
///
/// ```
/// use creche_contracts::status::document::FirstError;
///
/// let first: FirstError = "tools.kagi: unknown MCP server".parse()?;
/// assert_eq!(first.as_str(), "tools.kagi: unknown MCP server");
/// assert!("e".repeat(201).parse::<FirstError>().is_err());
/// # Ok::<(), creche_contracts::status::document::FirstErrorError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw text:
///
/// ```compile_fail,E0423
/// use creche_contracts::status::document::FirstError;
///
/// let first = FirstError("e".repeat(201));
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FirstError(String);

impl FirstError {
    /// The error as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for FirstError {
    type Err = FirstErrorError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.chars().count() > FIRST_ERROR_CHARS_MAX {
            return Err(FirstErrorError);
        }

        Ok(Self(text.to_owned()))
    }
}

/// A text has more characters than `first_error` permits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FirstErrorError;

impl fmt::Display for FirstErrorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "first_error has {FIRST_ERROR_CHARS_MAX} characters at most"
        )
    }
}

impl Error for FirstErrorError {}

// CONTRACT-QUESTION: contract 05 calls `report_path`, `supervisor_env` and
// `token_path` a host path and gives no grammar. The type takes a text that
// starts with `/` and holds no control character. The Python writer writes
// each text. The Python readers of `attendance` and of the terminal door take
// a relative `supervisor_env`. A looser grammar here lets a writer publish a
// path that depends on the working directory of the reader.
/// A path on the host: `report_path`, `supervisor_env` and `token_path` of
/// contract 05.
///
/// The path is absolute and holds no control character.
///
/// ```
/// use creche_contracts::status::document::HostPath;
///
/// let path: HostPath = "/srv/state/families/chat/validation.json".parse()?;
/// assert_eq!(path.as_str(), "/srv/state/families/chat/validation.json");
/// assert!("../supervisor.env".parse::<HostPath>().is_err());
/// # Ok::<(), creche_contracts::status::document::HostPathError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw text:
///
/// ```compile_fail,E0423
/// use creche_contracts::status::document::HostPath;
///
/// let path = HostPath(String::from("../supervisor.env"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct HostPath(String);

impl HostPath {
    /// The path as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for HostPath {
    type Err = HostPathError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if !text.starts_with('/') {
            return Err(HostPathError::NotAbsolute);
        }

        if text.contains(char::is_control) {
            return Err(HostPathError::HoldsControl);
        }

        Ok(Self(text.to_owned()))
    }
}

/// Why a text is not a host path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostPathError {
    /// The text does not start with `/`.
    NotAbsolute,
    /// The text holds a control character, for example a NUL or a newline.
    HoldsControl,
}

impl fmt::Display for HostPathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NotAbsolute => "a host path starts with /",
            Self::HoldsControl => "a host path holds no control character",
        })
    }
}

impl Error for HostPathError {}

/// An amount of US dollars in the `spend` block (contract 05 §7): a finite
/// number.
///
/// A JSON text has no form for a number that is not finite. The Python writer
/// writes `NaN` for one, and a strict reader then refuses the whole document.
///
/// ```
/// use creche_contracts::status::document::Usd;
///
/// assert_eq!(Usd::new(3.42)?.get(), 3.42);
/// assert!(Usd::new(f64::NAN).is_err());
/// # Ok::<(), creche_contracts::status::document::UsdError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::status::document::Usd;
///
/// let spend = Usd(f64::NAN);
/// ```
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Usd(f64);

impl Usd {
    /// A finite amount.
    ///
    /// # Errors
    ///
    /// [`UsdError`] for `NaN` and for an infinity.
    pub fn new(amount: f64) -> Result<Self, UsdError> {
        if !amount.is_finite() {
            return Err(UsdError);
        }

        Ok(Self(amount))
    }

    /// The amount.
    #[must_use]
    pub fn get(self) -> f64 {
        self.0
    }
}

/// A number is not finite, so it is not an amount.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsdError;

impl fmt::Display for UsdError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("an amount is a finite number")
    }
}

impl Error for UsdError {}

// --- the blocks of the document ---

/// The `validation` block (contract 05 §3.2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Validation {
    /// The registry revision that `caregiver` validated.
    pub rev: String,
    /// When `caregiver` validated it.
    pub checked_at: Timestamp,
    /// Whether the family file passed.
    pub ok: bool,
    /// Whether no revision of the family passed, ever (§3.1).
    pub never_valid: bool,
    /// The count of errors in the report.
    pub error_count: u64,
    /// The count of warnings in the report.
    pub warning_count: u64,
    /// Where the report is.
    pub report_path: HostPath,
    /// The first error of the report. `None` when the report has no error.
    pub first_error: Option<FirstError>,
}

/// One row of `sandboxes` (contract 05 §4.1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sandbox {
    /// The name of the sandbox.
    pub id: SandboxName,
    /// The lifecycle state (§4.2).
    pub state: SandboxLifecycle,
    /// Whether the VM runs.
    pub power: SandboxPower,
    /// The image digest.
    pub image: String,
    /// The hash of the fields that force a replacement.
    pub spec_hash: String,
    /// The count of CPUs.
    pub cpus: u64,
    /// The memory, as the family file writes it.
    pub memory: String,
    /// The time of the `sbx create` call.
    pub created_at: Timestamp,
    /// The time of the first handshake with `attendance`. `None` when no
    /// handshake passed.
    pub ready_at: Option<Timestamp>,
    /// The state of the channel.
    pub channel: ChannelState,
    // CONTRACT-QUESTION: contract 05 §4.1.1 rule 4 calls a row with no path
    // a fault. `caregiver.sandboxes` reads a row of its ledger with no
    // `supervisor_env` key as the empty text, and the Python writer writes
    // that text for a sandbox in each lifecycle state. The type takes that
    // form as `None`, in each lifecycle state too. To refuse it for a sandbox
    // that serves, `caregiver` must first write a path into each row of its
    // ledger.
    /// The path of the `supervisor.env` of the sandbox (§4.1.1). `None` is
    /// the empty text in the file: the ledger of `caregiver` holds no path
    /// for the sandbox.
    pub supervisor_env: Option<HostPath>,
}

/// The `credentials` block (contract 05 §6.1): ids and an epoch, and no
/// value of a credential.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Credentials {
    // CONTRACT-QUESTION: contract 05 §6.1 gives no range for the epoch. §6.2
    // starts it at 1 and §6.3 adds 1 at each rotation, so the type refuses 0
    // and a negative number. The Python reader of `attendance` takes 0. To
    // take 0, change the type of the field to `u64`.
    /// The epoch of the credentials: 1 at the first mint.
    pub epoch: NonZeroU64,
    /// The id of the family key.
    pub key_id: String,
    /// The id of the family token.
    pub token_id: String,
    /// When the credentials last rotated.
    pub rotated_at: Timestamp,
    /// When the next rotation is due. `None` when none is planned.
    pub next_rotation_at: Option<Timestamp>,
    /// The state of the rotation.
    pub rotation_state: RotationState,
}

/// The `spend` block (contract 05 §7).
#[derive(Debug, Clone, PartialEq)]
pub struct Spend {
    /// The window of the budget.
    pub window: SpendWindow,
    /// What the family spent in the window.
    pub spend_usd: Usd,
    /// The budget of the window. `None` when the family key has none.
    pub budget_usd: Option<Usd>,
    /// When `caregiver` read the numbers.
    pub as_of: Timestamp,
    /// The authority of the numbers.
    pub source: SpendSource,
}

/// The `limits` block (contract 05 §2.1). `None` is `null` in the file: the
/// kind of the family has no such limit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// The most turns that run at one time.
    pub max_running_turns: Option<u64>,
    /// The most turns that wait in the queue.
    pub max_queued_turns: Option<u64>,
    /// The seconds after which a thin job stops.
    pub job_timeout_s: Option<u64>,
}

/// One declared webhook (contract 05 §6.4): its name and where its bearer
/// is. The document holds no value of a bearer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Webhook {
    /// The name of the webhook.
    pub name: WebhookName,
    /// The path of the token file.
    pub token_path: HostPath,
}

/// The `triggers` block (contract 05 §2.1, §6.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Triggers {
    /// One row for each declared webhook.
    pub webhooks: Vec<Webhook>,
    /// Whether another family can start a job here. A file with no such key
    /// reads as `false`.
    pub enqueue: bool,
}

/// The `reconcile` block (contract 05 §3.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reconcile {
    /// When the pass started.
    pub since: Timestamp,
    /// The revision that is live.
    pub from_rev: String,
    /// The revision that the pass applies.
    pub to_rev: String,
    /// The step in flight.
    pub step: ReconcileStep,
    /// The count of attempts.
    pub attempts: u64,
    /// Whether the pass needs a sandbox switch.
    pub needs_switch: bool,
}

/// The `pep` block (contract 05 §2.2): what the watch of the chaperone last
/// saw.
///
/// Each variant holds only the fields that §2.2 permits for its state. A
/// document thus cannot say `off` with a URL, or `ok` with a time of silence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pep {
    /// No watch runs. The file holds an empty URL and two `null` times.
    Off,
    /// The health check answered at `checked_at`.
    Ok {
        /// The URL of the health check.
        url: String,
        /// The time of the newest health check. `None` when no check ran.
        checked_at: Option<Timestamp>,
    },
    /// The health check did not answer since `since`.
    Unreachable {
        /// The URL of the health check.
        url: String,
        /// The time of the newest health check.
        checked_at: Option<Timestamp>,
        /// When the chaperone first did not answer.
        since: Timestamp,
    },
}

// --- one fault ---

/// The fields of one fault, for [`Fault::new`].
#[derive(Debug, Clone, PartialEq)]
pub struct FaultParts {
    /// The code.
    pub code: FaultCode,
    /// Whether the fault stops new turns.
    pub blocks_turns: bool,
    /// When the service first saw the fault.
    pub since: Timestamp,
    /// The service that detected the fault.
    pub source: FaultSource,
    /// Whether the fault file of the source is stale (§3.3.1 rule 7).
    pub stale: bool,
    /// Each extra key of the fault, in the order that the writer keeps.
    pub detail: Object,
}

/// One fault of the document (contract 05 §3.3).
///
/// ```
/// use creche_contracts::status::document::{Fault, FaultParts};
/// use creche_contracts::status::json::Object;
/// use creche_contracts::status::words::{FaultCode, FaultSource};
///
/// let parts = FaultParts {
///     code: FaultCode::GrantsStale,
///     blocks_turns: true,
///     since: "2031-04-18T09:52:40Z".parse()?,
///     source: FaultSource::Pep,
///     stale: false,
///     detail: Object::new(),
/// };
/// let wrong_source = FaultParts { source: FaultSource::Sessiond, ..parts.clone() };
/// assert!(Fault::new(parts).is_ok());
/// assert!(Fault::new(wrong_source).is_err());
/// # Ok::<(), creche_contracts::status::time::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::document::{Fault, FaultParts};
/// use creche_contracts::status::json::Object;
/// use creche_contracts::status::words::{FaultCode, FaultSource};
///
/// let fault = Fault {
///     code: FaultCode::GrantsStale,
///     blocks_turns: false,
///     since: "2031-04-18T09:52:40Z".parse().unwrap(),
///     source: FaultSource::Sessiond,
///     stale: false,
///     detail: Object::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct Fault {
    code: FaultCode,
    blocks_turns: bool,
    since: Timestamp,
    source: FaultSource,
    stale: bool,
    detail: Object,
}

impl Fault {
    /// One fault.
    ///
    /// # Errors
    ///
    /// [`FaultError`] when the parts break a rule of contract 05 §3.3.
    pub fn new(parts: FaultParts) -> Result<Self, FaultError> {
        if !parts.code.is_detected_by(parts.source) {
            return Err(FaultError::SourceDoesNotDetect);
        }

        // CONTRACT-QUESTION: contract 05 §3.3 says that the table fixes
        // `blocks_turns` for each code. `caregiver.faults.rescope_by_fleet`
        // writes `false` for `sandbox_start_failed` when the fault names a
        // sandbox and another sandbox serves. The type takes that one
        // difference. To refuse it, a family that replaces a sandbox stops
        // turns for a fault of the new sandbox.
        let rescoped = parts.code == FaultCode::SandboxStartFailed && !parts.blocks_turns;
        if parts.blocks_turns != parts.code.blocks_turns() && !rescoped {
            return Err(FaultError::BlocksTurnsAgainstTable);
        }

        if parts
            .detail
            .iter()
            .any(|(key, _)| FAULT_KEYS.contains(&key))
        {
            return Err(FaultError::DetailNamesField);
        }

        Ok(Self {
            code: parts.code,
            blocks_turns: parts.blocks_turns,
            since: parts.since,
            source: parts.source,
            stale: parts.stale,
            detail: parts.detail,
        })
    }

    /// The code.
    #[must_use]
    pub fn code(&self) -> FaultCode {
        self.code
    }

    /// Whether the fault stops new turns.
    #[must_use]
    pub fn blocks_turns(&self) -> bool {
        self.blocks_turns
    }

    /// When the service first saw the fault.
    #[must_use]
    pub fn since(&self) -> Timestamp {
        self.since
    }

    /// The service that detected the fault.
    #[must_use]
    pub fn source(&self) -> FaultSource {
        self.source
    }

    /// Whether the fault file of the source is stale.
    #[must_use]
    pub fn is_stale(&self) -> bool {
        self.stale
    }

    /// Each extra key of the fault. A reader ignores a key that it does not
    /// know (§3.3).
    #[must_use]
    pub fn detail(&self) -> &Object {
        &self.detail
    }

    fn to_json(&self) -> Json {
        let mut object = Object::new();
        object.insert("code", word(self.code.as_str()));
        object.insert("blocks_turns", Json::Bool(self.blocks_turns));
        object.insert("since", time(self.since));
        object.insert("source", word(self.source.as_str()));
        object.insert("stale", Json::Bool(self.stale));
        // `Fault::new` refuses a detail with the name of a field.
        object.append(&self.detail);

        Json::Object(object)
    }
}

/// Why the parts of a fault are not a fault of contract 05 §3.3.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultError {
    /// The source is not a service that detects the code (§3.3.1).
    SourceDoesNotDetect,
    /// `blocks_turns` is not what the table of §3.3 fixes for the code.
    BlocksTurnsAgainstTable,
    /// A key of the detail is one of the five keys that §3.3 names.
    DetailNamesField,
}

impl fmt::Display for FaultError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::SourceDoesNotDetect => "the source of the fault does not detect its code",
            Self::BlocksTurnsAgainstTable => "blocks_turns differs from the table of the code",
            Self::DetailNamesField => "a detail of the fault has the name of a field",
        })
    }
}

impl Error for FaultError {}

// --- the document ---

/// The fields of one status document, for [`StatusDocument::new`].
#[derive(Debug, Clone, PartialEq)]
pub struct DocumentParts {
    /// The name of the family.
    pub family: FamilyName,
    // CONTRACT-QUESTION: contract 05 §2.1 says that `kind` is one of three
    // words. `caregiver` writes the empty text for a family that had no valid
    // revision, because it knows no kind then. The type takes that form as
    // `None`. To refuse it, `caregiver` must publish no document for such a
    // family, and a reader then cannot tell it from a family that does not
    // exist. The type does not tie `None` to `never_valid` or to an empty
    // `sandboxes`: `caregiver.apply` takes the kind from the last document
    // and `never_valid` from the applied state, so the two can differ.
    /// The kind of the family. `None` is the empty text in the file: no
    /// revision of the family was valid, so `caregiver` knows no kind.
    pub kind: Option<Kind>,
    /// The state of the family.
    pub state: FamilyState,
    /// When `caregiver` wrote the document.
    pub written_at: Timestamp,
    /// The registry revision that `caregiver` last read.
    pub registry_rev: String,
    /// The revision that is live. Empty when no revision was applied.
    pub applied_rev: String,
    /// The revision of the family config mount. Empty when none was written.
    pub config_rev: String,
    /// The validation block.
    pub validation: Validation,
    // CONTRACT-QUESTION: contract 05 §2.1 says that `faults` is empty in
    // each state but `degraded`. `caregiver` writes the open faults of a
    // family in the state `invalid` too. The type takes a fault in each state.
    // To refuse it, a reader of an invalid family does not see a fault that
    // stops its turns. `caregiver` writes no fault in the states `in_sync`
    // and `reconciling`, and the type does not refuse one there.
    /// The open faults.
    pub faults: Vec<Fault>,
    /// The reconcile block. `Some` only in the state `reconciling`.
    pub reconcile: Option<Reconcile>,
    /// One row for each sandbox.
    pub sandboxes: Vec<Sandbox>,
    // CONTRACT-QUESTION: contract 05 §2.1 says that `credentials` is an
    // object. `caregiver` writes `null` for a family that never had
    // credentials. The type takes that form as `None`. To refuse it,
    // `caregiver` must publish no document before the first mint.
    /// The credentials block. `None` is `null` in the file: the family never
    /// had credentials.
    pub credentials: Option<Credentials>,
    /// The spend block. `None` is `null` in the file: no pass read the spend
    /// (§7 rule 3).
    pub spend: Option<Spend>,
    /// The limits block.
    pub limits: Limits,
    /// The triggers block.
    pub triggers: Triggers,
    /// The `pep` block.
    pub pep: Pep,
}

/// One status document of a family (contract 05 §2.1).
///
/// ```
/// use creche_contracts::status::document::StatusDocument;
///
/// assert!(StatusDocument::read(br#"{"family": "chat"}"#).is_err());
/// ```
///
/// Code outside this module cannot build a value from raw parts. The first
/// example compiles, and the second example does not:
///
/// ```
/// use creche_contracts::status::document::{DocumentParts, StatusDocument};
///
/// fn build(parts: DocumentParts) -> bool {
///     StatusDocument::new(parts).is_ok()
/// }
/// ```
///
/// ```compile_fail,E0451
/// use creche_contracts::status::document::{DocumentParts, StatusDocument};
///
/// fn build(parts: DocumentParts) -> StatusDocument {
///     StatusDocument { parts }
/// }
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct StatusDocument {
    parts: DocumentParts,
}

impl StatusDocument {
    /// One document.
    ///
    /// # Errors
    ///
    /// [`StatusError`] when two parts break a rule of contract 05:
    ///
    /// - a sandbox of another family (§4.1),
    /// - two sandboxes with one number (§4.1),
    /// - a reconcile block outside the state `reconciling` (§2.1),
    /// - `never_valid` outside the state `invalid` (§3.1).
    pub fn new(parts: DocumentParts) -> Result<Self, StatusError> {
        let other_family = parts
            .sandboxes
            .iter()
            .position(|sandbox| sandbox.id.family() != &parts.family);
        if let Some(item) = other_family {
            return Err(StatusError::SandboxOfOtherFamily { item });
        }

        // Each sandbox is a sandbox of one family here, so the number alone
        // names it. `chat-s01` and `chat-s1` are two texts with one number.
        let numbers = || parts.sandboxes.iter().map(|sandbox| sandbox.id.number());
        let twice = numbers()
            .enumerate()
            .position(|(place, number)| numbers().take(place).any(|earlier| earlier == number));
        if let Some(item) = twice {
            return Err(StatusError::SandboxTwice { item });
        }

        if parts.reconcile.is_some() && parts.state != FamilyState::Reconciling {
            return Err(StatusError::field("reconcile", FieldFault::NotPermitted));
        }

        // A family with no valid revision has an invalid family file now.
        // Each place of `caregiver` that writes `never_valid: true` also
        // writes the state `invalid`.
        if parts.validation.never_valid && parts.state != FamilyState::Invalid {
            return Err(StatusError::field(
                "validation.never_valid",
                FieldFault::NotPermitted,
            ));
        }

        Ok(Self { parts })
    }

    /// Reads the bytes of one `status.json` as a valid document. The size cap
    /// and the byte order mark rule are the rules of `attendance`.
    ///
    /// # Errors
    ///
    /// [`StatusError`] says which field is not what contract 05 states.
    pub fn read(bytes: &[u8]) -> Result<Self, StatusError> {
        let raw = RawStatus::read(bytes, Reader::Attendance).map_err(StatusError::Read)?;

        Self::try_from(&raw)
    }

    /// The fields of the document.
    #[must_use]
    pub fn parts(&self) -> &DocumentParts {
        &self.parts
    }

    /// The same document with a new `written_at` (contract 05 §2 rule 8).
    #[must_use]
    pub fn restamped(&self, written_at: Timestamp) -> Self {
        let mut parts = self.parts.clone();
        parts.written_at = written_at;

        Self { parts }
    }

    /// The bytes of `status.json`, as `caregiver` writes them: two spaces of
    /// indent, ASCII only, and one final newline.
    #[must_use]
    pub fn encode(&self) -> Vec<u8> {
        let mut text = Json::Object(self.to_object()).encode(Layout::Indented, Charset::Ascii);
        text.push('\n');

        text.into_bytes()
    }

    /// The document as a reader finds it in the file. Each view of
    /// [`super::views`] takes this form.
    #[must_use]
    pub fn raw(&self) -> RawStatus {
        RawStatus::from_object(&self.to_object())
    }

    fn to_object(&self) -> Object {
        let parts = &self.parts;
        let kind = parts.kind.map_or("", Kind::as_str);
        let mut object = Object::new();
        object.insert("family", word(parts.family.as_str()));
        object.insert("kind", word(kind));
        object.insert("state", word(parts.state.as_str()));
        object.insert("written_at", time(parts.written_at));
        object.insert("registry_rev", word(&parts.registry_rev));
        object.insert("applied_rev", word(&parts.applied_rev));
        object.insert("config_rev", word(&parts.config_rev));
        object.insert("validation", parts.validation.to_json());
        object.insert("faults", array(&parts.faults, Fault::to_json));
        object.insert(
            "reconcile",
            nullable(parts.reconcile.as_ref(), Reconcile::to_json),
        );
        object.insert("sandboxes", array(&parts.sandboxes, Sandbox::to_json));
        object.insert(
            "credentials",
            nullable(parts.credentials.as_ref(), Credentials::to_json),
        );
        object.insert("spend", nullable(parts.spend.as_ref(), Spend::to_json));
        object.insert("limits", parts.limits.to_json());
        object.insert("triggers", parts.triggers.to_json());
        object.insert("pep", parts.pep.to_json());

        object
    }
}

// --- the writer: each block as JSON, with its keys in the order of the Python writer ---

fn word(text: &str) -> Json {
    Json::String(text.to_owned())
}

fn time(instant: Timestamp) -> Json {
    Json::String(instant.to_rfc3339())
}

fn count(value: u64) -> Json {
    Json::Integer(Integer::from(value))
}

fn nullable<T>(value: Option<&T>, to_json: fn(&T) -> Json) -> Json {
    value.map_or(Json::Null, to_json)
}

fn array<T>(items: &[T], to_json: fn(&T) -> Json) -> Json {
    Json::Array(items.iter().map(to_json).collect())
}

fn object(pairs: Vec<(&str, Json)>) -> Json {
    Json::Object(
        pairs
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

impl Validation {
    fn to_json(&self) -> Json {
        object(vec![
            ("rev", word(&self.rev)),
            ("checked_at", time(self.checked_at)),
            ("ok", Json::Bool(self.ok)),
            ("never_valid", Json::Bool(self.never_valid)),
            ("error_count", count(self.error_count)),
            ("warning_count", count(self.warning_count)),
            ("report_path", word(self.report_path.as_str())),
            (
                "first_error",
                nullable(self.first_error.as_ref(), |first| word(first.as_str())),
            ),
        ])
    }
}

impl Reconcile {
    fn to_json(&self) -> Json {
        object(vec![
            ("since", time(self.since)),
            ("from_rev", word(&self.from_rev)),
            ("to_rev", word(&self.to_rev)),
            ("step", word(self.step.as_str())),
            ("attempts", count(self.attempts)),
            ("needs_switch", Json::Bool(self.needs_switch)),
        ])
    }
}

impl Sandbox {
    fn to_json(&self) -> Json {
        let supervisor_env = self.supervisor_env.as_ref().map_or("", HostPath::as_str);

        object(vec![
            ("id", word(self.id.as_str())),
            ("state", word(self.state.as_str())),
            ("power", word(self.power.as_str())),
            ("image", word(&self.image)),
            ("spec_hash", word(&self.spec_hash)),
            ("cpus", count(self.cpus)),
            ("memory", word(&self.memory)),
            ("created_at", time(self.created_at)),
            ("ready_at", nullable(self.ready_at.as_ref(), |at| time(*at))),
            ("channel", word(self.channel.as_str())),
            ("supervisor_env", word(supervisor_env)),
        ])
    }
}

impl Credentials {
    fn to_json(&self) -> Json {
        object(vec![
            ("epoch", count(self.epoch.get())),
            ("key_id", word(&self.key_id)),
            ("token_id", word(&self.token_id)),
            ("rotated_at", time(self.rotated_at)),
            (
                "next_rotation_at",
                nullable(self.next_rotation_at.as_ref(), |at| time(*at)),
            ),
            ("rotation_state", word(self.rotation_state.as_str())),
        ])
    }
}

impl Spend {
    fn to_json(&self) -> Json {
        object(vec![
            ("window", word(self.window.as_str())),
            ("spend_usd", Json::Float(self.spend_usd.get())),
            (
                "budget_usd",
                nullable(self.budget_usd.as_ref(), |budget| Json::Float(budget.get())),
            ),
            ("as_of", time(self.as_of)),
            ("source", word(self.source.as_str())),
        ])
    }
}

impl Limits {
    fn to_json(self) -> Json {
        let limit = |value: Option<u64>| value.map_or(Json::Null, count);

        object(vec![
            ("max_running_turns", limit(self.max_running_turns)),
            ("max_queued_turns", limit(self.max_queued_turns)),
            ("job_timeout_s", limit(self.job_timeout_s)),
        ])
    }
}

impl Triggers {
    fn to_json(&self) -> Json {
        let webhook = |one: &Webhook| {
            object(vec![
                ("name", word(one.name.as_str())),
                ("token_path", word(one.token_path.as_str())),
            ])
        };

        object(vec![
            (
                "webhooks",
                Json::Array(self.webhooks.iter().map(webhook).collect()),
            ),
            ("enqueue", Json::Bool(self.enqueue)),
        ])
    }
}

impl Pep {
    /// The state of the watch.
    #[must_use]
    pub fn watch(&self) -> WatchState {
        match self {
            Self::Off => WatchState::Off,
            Self::Ok { .. } => WatchState::Ok,
            Self::Unreachable { .. } => WatchState::Unreachable,
        }
    }

    fn to_json(&self) -> Json {
        let (url, checked_at, since) = match self {
            Self::Off => ("", None, None),
            Self::Ok { url, checked_at } => (url.as_str(), checked_at.as_ref(), None),
            Self::Unreachable {
                url,
                checked_at,
                since,
            } => (url.as_str(), checked_at.as_ref(), Some(since)),
        };

        object(vec![
            ("watch", word(self.watch().as_str())),
            ("url", word(url)),
            ("checked_at", nullable(checked_at, |at| time(*at))),
            ("unreachable_since", nullable(since, |at| time(*at))),
        ])
    }
}

// --- the conversion from the raw document ---

/// Why a document is not a status document of contract 05.
///
/// No variant holds a text of the document. The text is untrusted, and a
/// caller writes this error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusError {
    /// The bytes are not one JSON object.
    Read(ReadError),
    /// One field does not hold what contract 05 states.
    Field {
        /// The name of the field, for example `sandboxes[].id`.
        field: &'static str,
        /// The place of the item in its list, from 0, for a field of a list.
        item: Option<usize>,
        /// What is wrong with the field.
        fault: FieldFault,
    },
    /// One fault breaks a rule of §3.3.
    Fault {
        /// The place of the fault in `faults`, from 0.
        item: usize,
        /// The rule.
        error: FaultError,
    },
    /// The name of one sandbox does not start with the name of the family
    /// (§4.1).
    SandboxOfOtherFamily {
        /// The place of the sandbox in `sandboxes`, from 0.
        item: usize,
    },
    /// An earlier row of `sandboxes` has the number of this sandbox. A number
    /// names one sandbox of a family (§4.1).
    SandboxTwice {
        /// The place of the second row in `sandboxes`, from 0.
        item: usize,
    },
}

/// What is wrong with one field of a document.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FieldFault {
    /// The document has no such key.
    Missing,
    /// The value is `null`, and the field is not optional.
    Null,
    /// The value has another JSON type.
    WrongType(JsonKind),
    /// An item of a list is not an object.
    NotAnObject,
    /// The text is not a word of the vocabulary of the field.
    UnknownWord,
    /// The text is not a time.
    NotATime(TimestampError),
    /// The text is not a family name.
    NotAFamilyName(FamilyNameError),
    /// The text is not a sandbox name.
    NotASandboxName(SandboxNameError),
    /// The text is not a webhook name.
    NotAWebhookName(WebhookNameError),
    /// The text is not a host path.
    NotAHostPath(HostPathError),
    /// The number is below the range of the field, or above 2^64 - 1.
    OutOfRange,
    /// The number is not finite.
    NotFinite,
    /// The text has more characters than the field permits.
    TooLong,
    /// The field holds a value that the state of the document, or of its
    /// block, does not permit.
    NotPermitted,
}

impl StatusError {
    const fn field(field: &'static str, fault: FieldFault) -> Self {
        Self::Field {
            field,
            item: None,
            fault,
        }
    }

    /// The same error, for the item at `place` of a list.
    fn in_item(self, place: usize) -> Self {
        match self {
            Self::Field { field, fault, .. } => Self::Field {
                field,
                item: Some(place),
                fault,
            },
            other => other,
        }
    }
}

impl fmt::Display for StatusError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(error) => write!(f, "{error}"),
            Self::Field {
                field,
                item: None,
                fault,
            } => write!(f, "{field}: {fault}"),
            Self::Field {
                field,
                item: Some(item),
                fault,
            } => write!(f, "{field}, item {item}: {fault}"),
            Self::Fault { item, error } => write!(f, "faults[], item {item}: {error}"),
            Self::SandboxOfOtherFamily { item } => write!(
                f,
                "sandboxes[].id, item {item}: the sandbox is not a sandbox of the family"
            ),
            Self::SandboxTwice { item } => write!(
                f,
                "sandboxes[].id, item {item}: an earlier row has the number of the sandbox"
            ),
        }
    }
}

impl Error for StatusError {}

impl fmt::Display for FieldFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing => f.write_str("the document has no such key"),
            Self::Null => f.write_str("the value is null"),
            Self::WrongType(kind) => write!(f, "the value is {kind:?}"),
            Self::NotAnObject => f.write_str("the item is not an object"),
            Self::UnknownWord => f.write_str("the text is not a word of the vocabulary"),
            Self::NotATime(error) => write!(f, "{error}"),
            Self::NotAFamilyName(error) => write!(f, "{error}"),
            Self::NotASandboxName(error) => write!(f, "{error}"),
            Self::NotAWebhookName(error) => write!(f, "{error}"),
            Self::NotAHostPath(error) => write!(f, "{error}"),
            Self::OutOfRange => f.write_str("the number is out of the range of the field"),
            Self::NotFinite => f.write_str("the number is not finite"),
            Self::TooLong => f.write_str("the text has too many characters"),
            Self::NotPermitted => {
                f.write_str("the state of the document does not permit the value")
            }
        }
    }
}

type Field<T> = Result<T, StatusError>;

/// The value of a field that is not optional.
fn required<'a, T>(slot: &'a Slot<T>, field: &'static str) -> Field<&'a T> {
    match optional(slot, field)? {
        Some(value) => Ok(value),
        None => Err(StatusError::field(field, FieldFault::Null)),
    }
}

/// The value of a field that can be `null`. The key itself is not optional.
fn optional<'a, T>(slot: &'a Slot<T>, field: &'static str) -> Field<Option<&'a T>> {
    match slot {
        Slot::Value(value) => Ok(Some(value)),
        Slot::Null => Ok(None),
        Slot::Missing => Err(StatusError::field(field, FieldFault::Missing)),
        Slot::Other(kind) => Err(StatusError::field(field, FieldFault::WrongType(*kind))),
    }
}

fn parsed<T, E>(text: &str, field: &'static str, fault: fn(E) -> FieldFault) -> Field<T>
where
    T: FromStr<Err = E>,
{
    text.parse()
        .map_err(|error| StatusError::field(field, fault(error)))
}

fn text_of(slot: &Slot<String>, field: &'static str) -> Field<String> {
    required(slot, field).cloned()
}

fn word_of<W: FromStr>(slot: &Slot<String>, field: &'static str) -> Field<W> {
    parsed(required(slot, field)?, field, |_| FieldFault::UnknownWord)
}

fn time_of(slot: &Slot<String>, field: &'static str) -> Field<Timestamp> {
    parsed(required(slot, field)?, field, FieldFault::NotATime)
}

fn optional_time(slot: &Slot<String>, field: &'static str) -> Field<Option<Timestamp>> {
    optional(slot, field)?
        .map(|text| parsed(text, field, FieldFault::NotATime))
        .transpose()
}

fn path_of(slot: &Slot<String>, field: &'static str) -> Field<HostPath> {
    parsed(required(slot, field)?, field, FieldFault::NotAHostPath)
}

fn flag_of(slot: &Slot<bool>, field: &'static str) -> Field<bool> {
    required(slot, field).copied()
}

/// A flag whose key is optional: a document with no such key says `false`.
fn flag_or_false(slot: &Slot<bool>, field: &'static str) -> Field<bool> {
    match slot {
        Slot::Missing => Ok(false),
        present => flag_of(present, field),
    }
}

fn count_from(integer: &Integer, field: &'static str) -> Field<u64> {
    integer
        .as_u64()
        .ok_or(StatusError::field(field, FieldFault::OutOfRange))
}

fn count_of(slot: &Slot<Integer>, field: &'static str) -> Field<u64> {
    count_from(required(slot, field)?, field)
}

fn optional_count(slot: &Slot<Integer>, field: &'static str) -> Field<Option<u64>> {
    optional(slot, field)?
        .map(|integer| count_from(integer, field))
        .transpose()
}

fn usd_from(number: &Number, field: &'static str) -> Field<Usd> {
    // An integer past the range of a float is not finite as a float.
    let amount = number.to_f64().unwrap_or(f64::INFINITY);

    Usd::new(amount).map_err(|_| StatusError::field(field, FieldFault::NotFinite))
}

/// Each item of a list, read with `read`. An error names the place of its item.
fn items_of<R, T>(
    slot: &Slot<Vec<Option<R>>>,
    field: &'static str,
    read: fn(&R) -> Field<T>,
) -> Field<Vec<T>> {
    required(slot, field)?
        .iter()
        .enumerate()
        .map(|(place, item)| {
            let item = item
                .as_ref()
                .ok_or(StatusError::field(field, FieldFault::NotAnObject))
                .map_err(|error| error.in_item(place))?;

            read(item).map_err(|error| error.in_item(place))
        })
        .collect()
}

impl TryFrom<&RawValidation> for Validation {
    type Error = StatusError;

    fn try_from(raw: &RawValidation) -> Field<Self> {
        let first_error = optional(&raw.first_error, "validation.first_error")?
            .map(|text| parsed(text, "validation.first_error", |_| FieldFault::TooLong))
            .transpose()?;

        Ok(Self {
            rev: text_of(&raw.rev, "validation.rev")?,
            checked_at: time_of(&raw.checked_at, "validation.checked_at")?,
            ok: flag_of(&raw.ok, "validation.ok")?,
            never_valid: flag_of(&raw.never_valid, "validation.never_valid")?,
            error_count: count_of(&raw.error_count, "validation.error_count")?,
            warning_count: count_of(&raw.warning_count, "validation.warning_count")?,
            report_path: path_of(&raw.report_path, "validation.report_path")?,
            first_error,
        })
    }
}

fn fault_from(raw: &RawFault) -> Field<FaultParts> {
    Ok(FaultParts {
        code: word_of(&raw.code, "faults[].code")?,
        blocks_turns: flag_of(&raw.blocks_turns, "faults[].blocks_turns")?,
        since: time_of(&raw.since, "faults[].since")?,
        source: word_of(&raw.source, "faults[].source")?,
        stale: flag_or_false(&raw.stale, "faults[].stale")?,
        detail: raw.detail.clone(),
    })
}

fn faults_from(slot: &Slot<Vec<Option<RawFault>>>) -> Field<Vec<Fault>> {
    items_of(slot, "faults[]", fault_from)?
        .into_iter()
        .enumerate()
        .map(|(item, parts)| Fault::new(parts).map_err(|error| StatusError::Fault { item, error }))
        .collect()
}

fn reconcile_from(raw: &RawReconcile) -> Field<Reconcile> {
    Ok(Reconcile {
        since: time_of(&raw.since, "reconcile.since")?,
        from_rev: text_of(&raw.from_rev, "reconcile.from_rev")?,
        to_rev: text_of(&raw.to_rev, "reconcile.to_rev")?,
        step: word_of(&raw.step, "reconcile.step")?,
        attempts: count_of(&raw.attempts, "reconcile.attempts")?,
        needs_switch: flag_of(&raw.needs_switch, "reconcile.needs_switch")?,
    })
}

fn sandbox_from(raw: &RawSandbox) -> Field<Sandbox> {
    let env_field = "sandboxes[].supervisor_env";
    let supervisor_env = match required(&raw.supervisor_env, env_field)?.as_str() {
        "" => None,
        path => Some(parsed(path, env_field, FieldFault::NotAHostPath)?),
    };

    Ok(Sandbox {
        id: parsed(
            required(&raw.id, "sandboxes[].id")?,
            "sandboxes[].id",
            FieldFault::NotASandboxName,
        )?,
        state: word_of(&raw.state, "sandboxes[].state")?,
        power: word_of(&raw.power, "sandboxes[].power")?,
        image: text_of(&raw.image, "sandboxes[].image")?,
        spec_hash: text_of(&raw.spec_hash, "sandboxes[].spec_hash")?,
        cpus: count_of(&raw.cpus, "sandboxes[].cpus")?,
        memory: text_of(&raw.memory, "sandboxes[].memory")?,
        created_at: time_of(&raw.created_at, "sandboxes[].created_at")?,
        ready_at: optional_time(&raw.ready_at, "sandboxes[].ready_at")?,
        channel: word_of(&raw.channel, "sandboxes[].channel")?,
        supervisor_env,
    })
}

fn credentials_from(raw: &RawCredentials) -> Field<Credentials> {
    let epoch = count_of(&raw.epoch, "credentials.epoch")?;
    let epoch = NonZeroU64::new(epoch).ok_or(StatusError::field(
        "credentials.epoch",
        FieldFault::OutOfRange,
    ))?;

    Ok(Credentials {
        epoch,
        key_id: text_of(&raw.key_id, "credentials.key_id")?,
        token_id: text_of(&raw.token_id, "credentials.token_id")?,
        rotated_at: time_of(&raw.rotated_at, "credentials.rotated_at")?,
        next_rotation_at: optional_time(&raw.next_rotation_at, "credentials.next_rotation_at")?,
        rotation_state: word_of(&raw.rotation_state, "credentials.rotation_state")?,
    })
}

fn spend_from(raw: &RawSpend) -> Field<Spend> {
    let window = word_of(&raw.window, "spend.window")?;
    let spend_usd = usd_from(
        required(&raw.spend_usd, "spend.spend_usd")?,
        "spend.spend_usd",
    )?;
    let budget_usd = optional(&raw.budget_usd, "spend.budget_usd")?
        .map(|number| usd_from(number, "spend.budget_usd"))
        .transpose()?;

    Ok(Spend {
        window,
        spend_usd,
        budget_usd,
        as_of: time_of(&raw.as_of, "spend.as_of")?,
        source: word_of(&raw.source, "spend.source")?,
    })
}

fn limits_from(raw: &RawLimits) -> Field<Limits> {
    Ok(Limits {
        max_running_turns: optional_count(&raw.max_running_turns, "limits.max_running_turns")?,
        max_queued_turns: optional_count(&raw.max_queued_turns, "limits.max_queued_turns")?,
        job_timeout_s: optional_count(&raw.job_timeout_s, "limits.job_timeout_s")?,
    })
}

fn webhook_from(raw: &RawWebhook) -> Field<Webhook> {
    Ok(Webhook {
        name: parsed(
            required(&raw.name, "triggers.webhooks[].name")?,
            "triggers.webhooks[].name",
            FieldFault::NotAWebhookName,
        )?,
        token_path: path_of(&raw.token_path, "triggers.webhooks[].token_path")?,
    })
}

fn triggers_from(raw: &RawTriggers) -> Field<Triggers> {
    Ok(Triggers {
        webhooks: items_of(&raw.webhooks, "triggers.webhooks[]", webhook_from)?,
        enqueue: flag_or_false(&raw.enqueue, "triggers.enqueue")?,
    })
}

fn pep_from(raw: &RawPep) -> Field<Pep> {
    let not_permitted = |field| Err(StatusError::field(field, FieldFault::NotPermitted));
    let url = text_of(&raw.url, "pep.url")?;
    let checked_at = optional_time(&raw.checked_at, "pep.checked_at")?;
    let since = optional_time(&raw.unreachable_since, "pep.unreachable_since")?;
    match (word_of(&raw.watch, "pep.watch")?, since) {
        (WatchState::Off, _) if !url.is_empty() => not_permitted("pep.url"),
        (WatchState::Off, _) if checked_at.is_some() => not_permitted("pep.checked_at"),
        (WatchState::Off | WatchState::Ok, Some(_)) => not_permitted("pep.unreachable_since"),
        (WatchState::Off, None) => Ok(Pep::Off),
        (WatchState::Ok, None) => Ok(Pep::Ok { url, checked_at }),
        (WatchState::Unreachable, Some(since)) => Ok(Pep::Unreachable {
            url,
            checked_at,
            since,
        }),
        (WatchState::Unreachable, None) => Err(StatusError::field(
            "pep.unreachable_since",
            FieldFault::Null,
        )),
    }
}

impl TryFrom<&RawStatus> for StatusDocument {
    type Error = StatusError;

    fn try_from(raw: &RawStatus) -> Field<Self> {
        let kind = match required(&raw.kind, "kind")?.as_str() {
            "" => None,
            kind => Some(parsed(kind, "kind", |_| FieldFault::UnknownWord)?),
        };
        let parts = DocumentParts {
            family: parsed(
                required(&raw.family, "family")?,
                "family",
                FieldFault::NotAFamilyName,
            )?,
            kind,
            state: word_of(&raw.state, "state")?,
            written_at: time_of(&raw.written_at, "written_at")?,
            registry_rev: text_of(&raw.registry_rev, "registry_rev")?,
            applied_rev: text_of(&raw.applied_rev, "applied_rev")?,
            config_rev: text_of(&raw.config_rev, "config_rev")?,
            validation: Validation::try_from(required(&raw.validation, "validation")?)?,
            faults: faults_from(&raw.faults)?,
            reconcile: optional(&raw.reconcile, "reconcile")?
                .map(reconcile_from)
                .transpose()?,
            sandboxes: items_of(&raw.sandboxes, "sandboxes[]", sandbox_from)?,
            credentials: optional(&raw.credentials, "credentials")?
                .map(credentials_from)
                .transpose()?,
            spend: optional(&raw.spend, "spend")?.map(spend_from).transpose()?,
            limits: limits_from(required(&raw.limits, "limits")?)?,
            triggers: triggers_from(required(&raw.triggers, "triggers")?)?,
            pep: pep_from(required(&raw.pep, "pep")?)?,
        };

        Self::new(parts)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ids::WebhookNameError;
    use crate::status::json::JsonError;

    /// A document with each block in it: a family that replaces its sandbox
    /// while the chaperone does not answer.
    const FULL: &str = r#"{
        "family": "chat",
        "kind": "attended",
        "state": "reconciling",
        "written_at": "2031-04-18T10:20:30Z",
        "registry_rev": "reg-2",
        "applied_rev": "reg-1",
        "config_rev": "reg-1",
        "validation": {
            "rev": "reg-2",
            "checked_at": "2031-04-18T10:20:29Z",
            "ok": true,
            "never_valid": false,
            "error_count": 0,
            "warning_count": 1,
            "report_path": "/state/families/chat/validation.json",
            "first_error": null
        },
        "faults": [
            {
                "code": "orphan_processes",
                "blocks_turns": false,
                "since": "2031-04-18T10:00:00Z",
                "source": "sessiond",
                "stale": true,
                "sandbox": "chat-s1"
            }
        ],
        "reconcile": {
            "since": "2031-04-18T10:20:00Z",
            "from_rev": "reg-1",
            "to_rev": "reg-2",
            "step": "switch_sandbox",
            "attempts": 1,
            "needs_switch": true
        },
        "sandboxes": [
            {
                "id": "chat-s1",
                "state": "draining",
                "power": "running",
                "image": "registry.example/playpen@sha256:aa",
                "spec_hash": "aa",
                "cpus": 2,
                "memory": "2g",
                "created_at": "2031-04-11T08:00:00Z",
                "ready_at": "2031-04-11T08:00:20Z",
                "channel": "open",
                "supervisor_env": "/state/families/chat/supervisor-chat-s1.env"
            },
            {
                "id": "chat-s2",
                "state": "planned",
                "power": "stopped",
                "image": "registry.example/playpen@sha256:bb",
                "spec_hash": "bb",
                "cpus": 2,
                "memory": "2g",
                "created_at": "2031-04-18T10:20:10Z",
                "ready_at": null,
                "channel": "closed",
                "supervisor_env": ""
            }
        ],
        "credentials": {
            "epoch": 3,
            "key_id": "family-chat",
            "token_id": "family-chat",
            "rotated_at": "2031-04-01T00:00:00Z",
            "next_rotation_at": null,
            "rotation_state": "settled"
        },
        "spend": {
            "window": "day",
            "spend_usd": 1.25,
            "budget_usd": 10.0,
            "as_of": "2031-04-18T10:20:25Z",
            "source": "litellm"
        },
        "limits": {"max_running_turns": null, "max_queued_turns": 100, "job_timeout_s": null},
        "triggers": {
            "webhooks": [
                {"name": "boiler-alert", "token_path": "/state/triggers/chat/boiler-alert.token"}
            ],
            "enqueue": false
        },
        "pep": {
            "watch": "unreachable",
            "url": "http://192.0.2.10:8300",
            "checked_at": "2031-04-18T10:20:28Z",
            "unreachable_since": "2031-04-18T10:18:00Z"
        }
    }"#;

    fn full() -> Object {
        let Json::Object(object) = Json::parse(FULL).unwrap() else {
            panic!("the document is an object");
        };

        object
    }

    fn json(text: &str) -> Json {
        Json::parse(text).unwrap()
    }

    /// `object` with the value at `path` changed. `None` takes the key away.
    /// A number in the path is a place in a list.
    fn changed(object: &Object, path: &[&str], value: Option<&Json>) -> Object {
        let (first, rest) = path.split_first().unwrap();
        let mut out = Object::new();
        for (key, held) in object.iter() {
            if key != *first {
                out.insert(key, held.clone());
            } else if rest.is_empty() {
                if let Some(value) = value {
                    out.insert(key, value.clone());
                }
            } else {
                out.insert(key, changed_inside(held, rest, value));
            }
        }

        if rest.is_empty() && object.get(first).is_none() {
            out.insert(first, value.unwrap().clone());
        }

        out
    }

    fn changed_inside(held: &Json, path: &[&str], value: Option<&Json>) -> Json {
        match held {
            Json::Object(inner) => Json::Object(changed(inner, path, value)),
            Json::Array(items) => {
                let (place, rest) = path.split_first().unwrap();
                let place: usize = place.parse().unwrap();
                let mut items = items.clone();
                items[place] = if rest.is_empty() {
                    value.unwrap().clone()
                } else {
                    changed_inside(&items[place], rest, value)
                };

                Json::Array(items)
            }
            other => panic!("{path:?} goes through {other:?}"),
        }
    }

    /// The valid document of `FULL` with one value changed.
    fn with(path: &[&str], value: &str) -> Result<StatusDocument, StatusError> {
        StatusDocument::try_from(&RawStatus::from_object(&changed(
            &full(),
            path,
            Some(&json(value)),
        )))
    }

    /// The valid document of `FULL` with one key taken away.
    fn without(path: &[&str]) -> Result<StatusDocument, StatusError> {
        StatusDocument::try_from(&RawStatus::from_object(&changed(&full(), path, None)))
    }

    fn document() -> StatusDocument {
        StatusDocument::try_from(&RawStatus::from_object(&full())).unwrap()
    }

    const fn field(field: &'static str, fault: FieldFault) -> StatusError {
        StatusError::field(field, fault)
    }

    const fn item(field: &'static str, place: usize, fault: FieldFault) -> StatusError {
        StatusError::Field {
            field,
            item: Some(place),
            fault,
        }
    }

    #[test]
    fn a_document_with_each_block_is_valid() {
        let document = document();
        let parts = document.parts();

        assert_eq!(parts.family.as_str(), "chat");
        assert_eq!(parts.kind, Some(Kind::Attended));
        assert_eq!(parts.state, FamilyState::Reconciling);
        assert_eq!(parts.written_at.to_rfc3339(), "2031-04-18T10:20:30Z");
        assert_eq!(parts.validation.first_error, None);
        assert_eq!(parts.faults.len(), 1);
        assert_eq!(parts.faults[0].code(), FaultCode::OrphanProcesses);
        assert_eq!(parts.faults[0].source(), FaultSource::Sessiond);
        assert_eq!(parts.faults[0].since().to_rfc3339(), "2031-04-18T10:00:00Z");
        assert!(parts.faults[0].is_stale());
        assert!(!parts.faults[0].blocks_turns());
        assert_eq!(
            parts.faults[0].detail().get("sandbox"),
            Some(&json(r#""chat-s1""#))
        );
        assert_eq!(
            parts.sandboxes[0]
                .supervisor_env
                .as_ref()
                .map(HostPath::as_str),
            Some("/state/families/chat/supervisor-chat-s1.env")
        );
        assert_eq!(parts.sandboxes[1].supervisor_env, None);
        assert_eq!(parts.sandboxes[1].ready_at, None);
        assert_eq!(parts.credentials.as_ref().unwrap().epoch.get(), 3);
        assert_eq!(parts.spend.as_ref().unwrap().spend_usd.get(), 1.25);
        assert_eq!(parts.limits.max_queued_turns, Some(100));
        assert_eq!(parts.triggers.webhooks[0].name.as_str(), "boiler-alert");
        assert_eq!(parts.pep.watch(), WatchState::Unreachable);
    }

    #[test]
    fn a_form_that_caregiver_writes_for_a_family_with_no_valid_revision_is_valid() {
        let empty_kind = with(&["kind"], r#""""#).unwrap();
        let no_credentials = with(&["credentials"], "null").unwrap();
        let no_spend = with(&["spend"], "null").unwrap();
        let no_budget = with(&["spend", "budget_usd"], "null").unwrap();
        let no_revision = with(&["applied_rev"], r#""""#).unwrap();

        assert_eq!(empty_kind.parts().kind, None);
        assert_eq!(no_credentials.parts().credentials, None);
        assert_eq!(no_spend.parts().spend, None);
        assert_eq!(no_budget.parts().spend.as_ref().unwrap().budget_usd, None);
        assert_eq!(no_revision.parts().applied_rev, "");
    }

    #[test]
    fn a_key_that_the_contract_calls_optional_can_be_missing() {
        let no_stale = without(&["faults", "0", "stale"]);
        let no_enqueue = without(&["triggers", "enqueue"]);

        // The fault of `FULL` is the list item at place 0.
        assert!(!no_stale.unwrap().parts().faults[0].is_stale());
        assert!(!no_enqueue.unwrap().parts().triggers.enqueue);
    }

    #[test]
    fn a_number_with_no_fraction_is_an_amount() {
        let spend = with(&["spend", "spend_usd"], "2").unwrap();

        assert_eq!(spend.parts().spend.as_ref().unwrap().spend_usd.get(), 2.0);
    }

    #[test]
    fn a_field_that_the_contract_does_not_permit_is_refused() {
        use FieldFault::{
            Missing, NotAFamilyName, NotAHostPath, NotASandboxName, NotATime, NotAWebhookName,
            NotAnObject, NotFinite, NotPermitted, Null, OutOfRange, TooLong, UnknownWord,
            WrongType,
        };

        let long = format!(r#""{}""#, "e".repeat(FIRST_ERROR_CHARS_MAX + 1));
        let past_u64 = "18446744073709551616";
        let refused: Vec<(Result<StatusDocument, StatusError>, StatusError)> = vec![
            (without(&["family"]), field("family", Missing)),
            (
                with(&["family"], "5"),
                field("family", WrongType(JsonKind::Integer)),
            ),
            (
                with(&["family"], r#""Chat""#),
                field("family", NotAFamilyName(FamilyNameError::BadFirstByte)),
            ),
            (without(&["kind"]), field("kind", Missing)),
            (with(&["kind"], "null"), field("kind", Null)),
            (with(&["kind"], r#""robot""#), field("kind", UnknownWord)),
            (with(&["kind"], r#""Attended""#), field("kind", UnknownWord)),
            (
                with(&["state"], r#""sleeping""#),
                field("state", UnknownWord),
            ),
            (
                with(&["state"], r#""in sync""#),
                field("state", UnknownWord),
            ),
            (
                with(&["written_at"], r#""2031-04-18T10:20:30""#),
                field("written_at", NotATime(TimestampError::NoOffset)),
            ),
            (
                with(&["written_at"], "1934705230"),
                field("written_at", WrongType(JsonKind::Integer)),
            ),
            (with(&["registry_rev"], "null"), field("registry_rev", Null)),
            (with(&["validation"], "null"), field("validation", Null)),
            (
                with(&["validation", "ok"], r#""yes""#),
                field("validation.ok", WrongType(JsonKind::String)),
            ),
            (
                without(&["validation", "first_error"]),
                field("validation.first_error", Missing),
            ),
            (
                with(&["validation", "first_error"], &long),
                field("validation.first_error", TooLong),
            ),
            (
                with(&["validation", "error_count"], "-1"),
                field("validation.error_count", OutOfRange),
            ),
            (
                with(&["validation", "error_count"], past_u64),
                field("validation.error_count", OutOfRange),
            ),
            (
                with(&["validation", "error_count"], "true"),
                field("validation.error_count", WrongType(JsonKind::Bool)),
            ),
            (
                with(&["validation", "report_path"], r#""validation.json""#),
                field(
                    "validation.report_path",
                    NotAHostPath(HostPathError::NotAbsolute),
                ),
            ),
            (with(&["faults"], "null"), field("faults[]", Null)),
            (
                with(&["faults"], r#"{"orphan_processes": true}"#),
                field("faults[]", WrongType(JsonKind::Object)),
            ),
            (
                with(&["faults", "0"], "5"),
                item("faults[]", 0, NotAnObject),
            ),
            (
                with(&["faults", "0", "stale"], "null"),
                item("faults[].stale", 0, Null),
            ),
            (
                with(&["faults", "0", "code"], r#""sandbox_lost""#),
                item("faults[].code", 0, UnknownWord),
            ),
            (
                with(&["faults", "0", "since"], r#""yesterday""#),
                item("faults[].since", 0, NotATime(TimestampError::Form)),
            ),
            (without(&["reconcile"]), field("reconcile", Missing)),
            (
                with(&["reconcile", "step"], r#""wait""#),
                field("reconcile.step", UnknownWord),
            ),
            (
                with(&["state"], r#""in_sync""#),
                field("reconcile", NotPermitted),
            ),
            (
                with(&["sandboxes", "1", "id"], r#""chat""#),
                item(
                    "sandboxes[].id",
                    1,
                    NotASandboxName(SandboxNameError::NoNumber),
                ),
            ),
            (
                with(
                    &["sandboxes", "1", "supervisor_env"],
                    r#""../supervisor.env""#,
                ),
                item(
                    "sandboxes[].supervisor_env",
                    1,
                    NotAHostPath(HostPathError::NotAbsolute),
                ),
            ),
            (
                with(&["sandboxes", "0", "cpus"], "2.0"),
                item("sandboxes[].cpus", 0, WrongType(JsonKind::Float)),
            ),
            (
                with(&["sandboxes", "0", "state"], r#""READY""#),
                item("sandboxes[].state", 0, UnknownWord),
            ),
            (
                with(&["sandboxes", "0", "created_at"], "null"),
                item("sandboxes[].created_at", 0, Null),
            ),
            (
                with(&["credentials", "epoch"], "0"),
                field("credentials.epoch", OutOfRange),
            ),
            (
                with(&["credentials", "epoch"], "-1"),
                field("credentials.epoch", OutOfRange),
            ),
            (
                with(&["credentials", "epoch"], "3.0"),
                field("credentials.epoch", WrongType(JsonKind::Float)),
            ),
            (
                with(&["spend", "window"], r#""week""#),
                field("spend.window", UnknownWord),
            ),
            (
                with(&["spend", "source"], r#""guess""#),
                field("spend.source", UnknownWord),
            ),
            (
                with(&["spend", "spend_usd"], "NaN"),
                field("spend.spend_usd", NotFinite),
            ),
            (
                with(&["spend", "budget_usd"], "1e999"),
                field("spend.budget_usd", NotFinite),
            ),
            (
                with(&["spend", "spend_usd"], &format!("1{}", "0".repeat(400))),
                field("spend.spend_usd", NotFinite),
            ),
            (without(&["limits"]), field("limits", Missing)),
            (
                without(&["limits", "job_timeout_s"]),
                field("limits.job_timeout_s", Missing),
            ),
            (
                with(&["limits", "job_timeout_s"], "-5"),
                field("limits.job_timeout_s", OutOfRange),
            ),
            (
                with(&["triggers", "enqueue"], r#""true""#),
                field("triggers.enqueue", WrongType(JsonKind::String)),
            ),
            (
                with(&["triggers", "webhooks", "0", "name"], r#""Boiler""#),
                item(
                    "triggers.webhooks[].name",
                    0,
                    NotAWebhookName(WebhookNameError::BadFirstByte),
                ),
            ),
            (without(&["pep"]), field("pep", Missing)),
            (with(&["pep"], "null"), field("pep", Null)),
            (
                with(&["pep", "watch"], r#""maybe""#),
                field("pep.watch", UnknownWord),
            ),
            (
                with(&["pep", "unreachable_since"], "null"),
                field("pep.unreachable_since", Null),
            ),
            (
                with(&["pep", "watch"], r#""ok""#),
                field("pep.unreachable_since", NotPermitted),
            ),
            (
                with(&["pep", "watch"], r#""off""#),
                field("pep.url", NotPermitted),
            ),
        ];

        for (place, (found, wanted)) in refused.into_iter().enumerate() {
            assert_eq!(found, Err(wanted), "row {place}: {wanted}");
        }
    }

    #[test]
    fn a_sandbox_of_another_family_is_refused() {
        assert_eq!(
            with(&["sandboxes", "1", "id"], r#""code-s2""#),
            Err(StatusError::SandboxOfOtherFamily { item: 1 })
        );
        assert_eq!(
            with(&["family"], r#""code""#),
            Err(StatusError::SandboxOfOtherFamily { item: 0 })
        );
    }

    #[test]
    fn two_rows_with_one_sandbox_number_are_refused() {
        assert_eq!(
            with(&["sandboxes", "1", "id"], r#""chat-s1""#),
            Err(StatusError::SandboxTwice { item: 1 })
        );
        // `chat-s01` and `chat-s1` are two names with the number 1.
        assert_eq!(
            with(&["sandboxes", "1", "id"], r#""chat-s01""#),
            Err(StatusError::SandboxTwice { item: 1 })
        );
    }

    #[test]
    fn never_valid_is_refused_outside_the_state_invalid() {
        let never = &["validation", "never_valid"];
        let invalid = changed(&full(), &["state"], Some(&json(r#""invalid""#)));
        let invalid = changed(&invalid, &["reconcile"], Some(&Json::Null));
        let never_valid = changed(&invalid, never, Some(&Json::Bool(true)));
        let document = StatusDocument::try_from(&RawStatus::from_object(&never_valid)).unwrap();

        assert!(document.parts().validation.never_valid);
        assert_eq!(document.parts().state, FamilyState::Invalid);
        for state in ["in_sync", "reconciling", "degraded"] {
            let state = json(&format!(r#""{state}""#));
            let reconcile = if state == json(r#""reconciling""#) {
                full().get("reconcile").unwrap().clone()
            } else {
                Json::Null
            };
            let other = changed(&never_valid, &["state"], Some(&state));
            let other = changed(&other, &["reconcile"], Some(&reconcile));

            assert_eq!(
                StatusDocument::try_from(&RawStatus::from_object(&other)),
                Err(field("validation.never_valid", FieldFault::NotPermitted)),
                "{state:?}"
            );
        }
    }

    #[test]
    fn the_pep_block_holds_only_what_its_state_permits() {
        let off = r#"{"watch": "off", "url": "", "checked_at": null, "unreachable_since": null}"#;
        let off_checked = r#"{"watch": "off", "url": "", "checked_at": "2031-04-18T10:20:28Z",
            "unreachable_since": null}"#;
        let ok = r#"{"watch": "ok", "url": "http://192.0.2.10:8300", "checked_at": null,
            "unreachable_since": null}"#;

        assert_eq!(with(&["pep"], off).unwrap().parts().pep, Pep::Off);
        assert_eq!(
            with(&["pep"], off_checked),
            Err(field("pep.checked_at", FieldFault::NotPermitted))
        );
        assert_eq!(
            with(&["pep"], ok).unwrap().parts().pep,
            Pep::Ok {
                url: "http://192.0.2.10:8300".to_owned(),
                checked_at: None,
            }
        );
    }

    fn fault_parts(code: FaultCode, source: FaultSource, blocks_turns: bool) -> FaultParts {
        FaultParts {
            code,
            blocks_turns,
            since: "2031-04-18T10:00:00Z".parse().unwrap(),
            source,
            stale: false,
            detail: Object::new(),
        }
    }

    #[test]
    fn a_fault_follows_the_table_of_its_code() {
        for code in FaultCode::ALL {
            for source in FaultSource::ALL {
                for blocks_turns in [true, false] {
                    let rescoped = *code == FaultCode::SandboxStartFailed && !blocks_turns;
                    let wanted = if !code.is_detected_by(*source) {
                        Err(FaultError::SourceDoesNotDetect)
                    } else if blocks_turns != code.blocks_turns() && !rescoped {
                        Err(FaultError::BlocksTurnsAgainstTable)
                    } else {
                        Ok(())
                    };
                    let found = Fault::new(fault_parts(*code, *source, blocks_turns));

                    assert_eq!(found.map(|_| ()), wanted, "{code} {source} {blocks_turns}");
                }
            }
        }
    }

    #[test]
    fn a_detail_of_a_fault_cannot_take_the_name_of_a_field() {
        for key in FAULT_KEYS {
            let mut parts = fault_parts(FaultCode::SpendUnknown, FaultSource::Managerd, false);
            parts.detail.insert(key, Json::Null);

            assert_eq!(
                Fault::new(parts),
                Err(FaultError::DetailNamesField),
                "{key}"
            );
        }
    }

    #[test]
    fn the_writer_keeps_the_order_of_the_keys_and_ends_with_a_newline() {
        let bytes = document().encode();
        let text = String::from_utf8(bytes.clone()).unwrap();
        let keys: Vec<&str> = text
            .lines()
            .filter(|line| line.starts_with("  \""))
            .filter_map(|line| line.split('"').nth(1))
            .collect();

        assert_eq!(
            keys,
            [
                "family",
                "kind",
                "state",
                "written_at",
                "registry_rev",
                "applied_rev",
                "config_rev",
                "validation",
                "faults",
                "reconcile",
                "sandboxes",
                "credentials",
                "spend",
                "limits",
                "triggers",
                "pep",
            ]
        );
        assert!(text.starts_with("{\n  \"family\": \"chat\",\n"));
        assert!(text.ends_with("\n}\n"));
        assert!(text.is_ascii());
        assert_eq!(StatusDocument::read(&bytes), Ok(document()));
    }

    #[test]
    fn a_restamp_moves_the_time_and_no_other_field() {
        let later: Timestamp = "2031-04-18T10:21:00Z".parse().unwrap();
        let restamped = document().restamped(later);
        let mut parts = document().parts().clone();
        parts.written_at = later;

        assert_eq!(restamped.parts(), &parts);
        assert_eq!(StatusDocument::new(parts), Ok(restamped));
    }

    #[test]
    fn bytes_that_are_not_an_object_are_refused() {
        assert_eq!(
            StatusDocument::read(b"[]"),
            Err(StatusError::Read(ReadError::NotAnObject(JsonKind::Array)))
        );
        assert_eq!(
            StatusDocument::read(b"{"),
            Err(StatusError::Read(ReadError::NotJson(JsonError::Syntax {
                at: 1
            })))
        );
        assert_eq!(
            StatusDocument::read(b"{}"),
            Err(field("kind", FieldFault::Missing))
        );
    }

    #[test]
    fn first_error_counts_characters_and_not_bytes() {
        let at_cap = "\u{e9}".repeat(FIRST_ERROR_CHARS_MAX);
        let over_cap = "e".repeat(FIRST_ERROR_CHARS_MAX + 1);

        assert_eq!(at_cap.parse::<FirstError>().unwrap().as_str(), at_cap);
        assert_eq!("".parse::<FirstError>().unwrap().as_str(), "");
        assert_eq!(over_cap.parse::<FirstError>(), Err(FirstErrorError));
    }

    #[test]
    fn a_host_path_is_absolute_and_has_no_control_character() {
        let accepted = ["/", "/a", "/srv/state/a b", "/srv/\u{e9}", "//a/../b"];
        let refused = [
            ("", HostPathError::NotAbsolute),
            ("a", HostPathError::NotAbsolute),
            ("a/b", HostPathError::NotAbsolute),
            ("../supervisor.env", HostPathError::NotAbsolute),
            (" /a", HostPathError::NotAbsolute),
            ("/a\0b", HostPathError::HoldsControl),
            ("/a\n", HostPathError::HoldsControl),
            ("/a\u{7f}", HostPathError::HoldsControl),
            ("/a\u{85}", HostPathError::HoldsControl),
        ];

        for text in accepted {
            assert_eq!(text.parse::<HostPath>().unwrap().as_str(), text);
        }

        for (text, error) in refused {
            assert_eq!(text.parse::<HostPath>(), Err(error), "{text:?}");
        }
    }

    #[test]
    fn an_amount_is_finite() {
        for amount in [0.0, -0.0, -1.5, 3.25, f64::MAX, f64::MIN_POSITIVE] {
            assert_eq!(Usd::new(amount).unwrap().get().to_bits(), amount.to_bits());
        }

        for amount in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(Usd::new(amount), Err(UsdError));
        }
    }

    #[test]
    fn the_error_says_which_field_and_why() {
        let errors: Vec<(StatusError, &str)> = vec![
            (
                field("kind", FieldFault::Missing),
                "kind: the document has no such key",
            ),
            (
                item(
                    "sandboxes[].cpus",
                    1,
                    FieldFault::WrongType(JsonKind::Float),
                ),
                "sandboxes[].cpus, item 1: the value is Float",
            ),
            (
                StatusError::Fault {
                    item: 2,
                    error: FaultError::BlocksTurnsAgainstTable,
                },
                "faults[], item 2: blocks_turns differs from the table of the code",
            ),
            (
                StatusError::SandboxOfOtherFamily { item: 0 },
                "sandboxes[].id, item 0: the sandbox is not a sandbox of the family",
            ),
            (
                StatusError::SandboxTwice { item: 1 },
                "sandboxes[].id, item 1: an earlier row has the number of the sandbox",
            ),
            (
                field("written_at", FieldFault::NotATime(TimestampError::NoOffset)),
                "written_at: a time names its UTC offset: Z, or a sign with hours and minutes",
            ),
        ];

        for (error, text) in errors {
            let boxed: Box<dyn Error> = Box::new(error);

            assert_eq!(boxed.to_string(), text);
        }

        assert_eq!(
            FirstErrorError.to_string(),
            "first_error has 200 characters at most"
        );
        assert_eq!(UsdError.to_string(), "an amount is a finite number");
        assert_eq!(
            HostPathError::NotAbsolute.to_string(),
            "a host path starts with /"
        );
    }
}
