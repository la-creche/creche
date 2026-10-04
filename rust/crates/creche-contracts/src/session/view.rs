//! The objects of an answer: a session, a turn and a lease (contract 02 §4.2,
//! §4.4, §7.1), and the small objects inside them.
//!
//! Each object has a raw type with public fields. A conversion that can fail
//! makes the valid type. The valid type writes the fields in the order that
//! `attendance` writes them.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde::{Deserialize, Serialize, Serializer};

use super::error::TurnReason;
use super::fields::{DeadlineS, Holder, IdempotencyKey, Labels, Title, TriggerKind};
use super::json::{self, Charset, EncodeError};
use super::state::{SessionKind, SessionState, TurnState};
use super::time::{self, Timestamp};
use crate::ids::{FamilyName, SandboxName, SessionId, Sha256Hex, Ulid};

/// Why the raw fields of an object are not a valid object. The error names the
/// first field that is not valid.
///
/// The error does not hold the value. The value is untrusted, and a caller
/// writes this error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FieldError {
    object: &'static str,
    field: &'static str,
}

impl FieldError {
    pub(super) const fn new(object: &'static str, field: &'static str) -> Self {
        Self { object, field }
    }

    /// The name of the field that is not valid.
    #[must_use]
    pub fn field(&self) -> &'static str {
        self.field
    }
}

impl fmt::Display for FieldError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "the field {} of {} is not valid",
            self.field, self.object
        )
    }
}

impl Error for FieldError {}

/// Reads one field of a raw object into its valid type.
fn field<T, Raw>(object: &'static str, name: &'static str, raw: Raw) -> Result<T, FieldError>
where
    T: TryFrom<Raw>,
{
    T::try_from(raw).map_err(|_| FieldError::new(object, name))
}

/// Reads one text field of a raw object into its valid type.
fn parsed<T: std::str::FromStr>(
    object: &'static str,
    name: &'static str,
    raw: &str,
) -> Result<T, FieldError> {
    raw.parse().map_err(|_| FieldError::new(object, name))
}

/// Reads one optional text field of a raw object into its valid type.
fn optional<T: std::str::FromStr>(
    object: &'static str,
    name: &'static str,
    raw: Option<&str>,
) -> Result<Option<T>, FieldError> {
    raw.map(|text| parsed(object, name, text)).transpose()
}

// --- usage ---

const USAGE: &str = "a usage";

/// The token counts and the cost of one turn (contract 02 §4.4). The numbers
/// are advisory.
///
/// ```
/// use creche_contracts::session::{RawUsage, Usage};
///
/// let usage = Usage::try_from(RawUsage {
///     input: 4120,
///     output: 188,
///     cache_read: 0,
///     cache_write: 0,
///     cost_usd: 0.014,
/// })?;
/// assert_eq!(usage.input(), 4120);
/// # Ok::<(), creche_contracts::session::FieldError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::Usage;
///
/// fn free(usage: Usage) -> Usage {
///     Usage { cost_usd: f64::NAN, ..usage }
/// }
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "RawUsage")]
pub struct Usage {
    input: u64,
    output: u64,
    cache_read: u64,
    cache_write: u64,
    cost_usd: f64,
}

/// The fields of a [`Usage`], before the check.
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
pub struct RawUsage {
    /// The count of input tokens.
    pub input: u64,
    /// The count of output tokens.
    pub output: u64,
    /// The count of tokens that the model read from its cache.
    pub cache_read: u64,
    /// The count of tokens that the model wrote to its cache.
    pub cache_write: u64,
    /// The cost in US dollars.
    pub cost_usd: f64,
}

impl TryFrom<RawUsage> for Usage {
    type Error = FieldError;

    /// The cost is a finite number that is zero or more.
    fn try_from(raw: RawUsage) -> Result<Self, Self::Error> {
        if !raw.cost_usd.is_finite() || raw.cost_usd < 0.0 {
            return Err(FieldError::new(USAGE, "cost_usd"));
        }

        Ok(Self {
            input: raw.input,
            output: raw.output,
            cache_read: raw.cache_read,
            cache_write: raw.cache_write,
            cost_usd: raw.cost_usd,
        })
    }
}

impl Usage {
    /// The count of input tokens.
    #[must_use]
    pub fn input(&self) -> u64 {
        self.input
    }

    /// The count of output tokens.
    #[must_use]
    pub fn output(&self) -> u64 {
        self.output
    }

    /// The count of tokens that the model read from its cache.
    #[must_use]
    pub fn cache_read(&self) -> u64 {
        self.cache_read
    }

    /// The count of tokens that the model wrote to its cache.
    #[must_use]
    pub fn cache_write(&self) -> u64 {
        self.cache_write
    }

    /// The cost in US dollars.
    #[must_use]
    pub fn cost_usd(&self) -> f64 {
        self.cost_usd
    }
}

// --- the Open WebUI ids of a turn ---

const OWUI_REFS: &str = "the Open WebUI ids";

/// The four Open WebUI ids that a turn can carry (contract 02 §10).
///
/// Each id is free text. The chat id and the message id have one character or
/// more. The two other ids are absent or have one character or more.
///
/// ```
/// use creche_contracts::session::OwuiRefs;
///
/// let refs = OwuiRefs::new("3f2a9c41".to_owned(), "b7c1e2d0".to_owned(), None, None)?;
/// assert_eq!(refs.chat_id(), "3f2a9c41");
/// # Ok::<(), creche_contracts::session::FieldError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::OwuiRefs;
///
/// fn no_chat(refs: OwuiRefs) -> OwuiRefs {
///     OwuiRefs { chat_id: String::new(), ..refs }
/// }
/// ```
// CONTRACT-QUESTION: contract 02 §5.4 and §10 give the four ids no grammar and
// no cap. The Python parser takes each text with one character or more. This
// type does the same. A cap refuses a body that the Python code takes today.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "RawOwuiRefs")]
pub struct OwuiRefs {
    chat_id: String,
    message_id: String,
    user_message_id: Option<String>,
    parent_id: Option<String>,
}

/// The fields of an [`OwuiRefs`], before the check.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct RawOwuiRefs {
    /// The id of the chat.
    pub chat_id: String,
    /// The id of the assistant message that Open WebUI makes.
    pub message_id: String,
    /// The id of the user message that started the turn.
    pub user_message_id: Option<String>,
    /// The id of the message that the user message follows.
    pub parent_id: Option<String>,
}

impl TryFrom<RawOwuiRefs> for OwuiRefs {
    type Error = FieldError;

    fn try_from(raw: RawOwuiRefs) -> Result<Self, Self::Error> {
        Self::new(
            raw.chat_id,
            raw.message_id,
            raw.user_message_id,
            raw.parent_id,
        )
    }
}

impl OwuiRefs {
    /// The ids of one turn.
    pub fn new(
        chat_id: String,
        message_id: String,
        user_message_id: Option<String>,
        parent_id: Option<String>,
    ) -> Result<Self, FieldError> {
        let empty = |id: &Option<String>| id.as_deref() == Some("");
        let bad = [
            ("chat_id", chat_id.is_empty()),
            ("message_id", message_id.is_empty()),
            ("user_message_id", empty(&user_message_id)),
            ("parent_id", empty(&parent_id)),
        ];
        if let Some((name, _)) = bad.into_iter().find(|(_, is_bad)| *is_bad) {
            return Err(FieldError::new(OWUI_REFS, name));
        }

        Ok(Self {
            chat_id,
            message_id,
            user_message_id,
            parent_id,
        })
    }

    /// The id of the chat.
    #[must_use]
    pub fn chat_id(&self) -> &str {
        &self.chat_id
    }

    /// The id of the assistant message that Open WebUI makes.
    #[must_use]
    pub fn message_id(&self) -> &str {
        &self.message_id
    }

    /// The id of the user message that started the turn.
    #[must_use]
    pub fn user_message_id(&self) -> Option<&str> {
        self.user_message_id.as_deref()
    }

    /// The id of the message that the user message follows.
    #[must_use]
    pub fn parent_id(&self) -> Option<&str> {
        self.parent_id.as_deref()
    }
}

// --- the trigger of a job ---

const TRIGGER: &str = "a trigger";

/// The largest count of families in a delegation chain (contract 02 §13.2
/// rule 6).
const CHAIN_MAX: usize = 8;

/// The firing that started one autonomous job (contract 02 §13.2).
///
/// Only a trigger of the kind `dispatch` has a chain, and a chain has 8
/// families at most. The object holds no `chain` key when the chain is empty.
///
/// ```
/// use creche_contracts::session::{Trigger, TriggerKind};
///
/// let trigger = Trigger::new(TriggerKind::Webhook, Some("boiler-alert".to_owned()), None, vec![])?;
/// assert_eq!(trigger.name(), Some("boiler-alert"));
///
/// let chain = vec!["chat".parse()?];
/// assert!(Trigger::new(TriggerKind::Timer, None, None, chain).is_err());
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{Trigger, TriggerKind};
///
/// fn as_timer(trigger: Trigger) -> Trigger {
///     Trigger { kind: TriggerKind::Timer, ..trigger }
/// }
/// ```
// CONTRACT-QUESTION: contract 02 §13.2 gives the name of a trigger no grammar
// and no cap. It says that the name is a webhook name or a family name. The
// Python parser takes each text. This type does the same.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Trigger {
    kind: TriggerKind,
    name: Option<String>,
    #[serde(serialize_with = "time::optional_seconds")]
    fired_at: Option<Timestamp>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    chain: Vec<FamilyName>,
}

impl Trigger {
    /// The trigger of one job.
    pub fn new(
        kind: TriggerKind,
        name: Option<String>,
        fired_at: Option<Timestamp>,
        chain: Vec<FamilyName>,
    ) -> Result<Self, FieldError> {
        let permitted = match kind {
            TriggerKind::Dispatch => chain.len() <= CHAIN_MAX,
            TriggerKind::Timer | TriggerKind::Webhook => chain.is_empty(),
        };
        if !permitted {
            return Err(FieldError::new(TRIGGER, "chain"));
        }

        Ok(Self {
            kind,
            name,
            fired_at,
            chain,
        })
    }

    /// What fired the job.
    #[must_use]
    pub fn kind(&self) -> TriggerKind {
        self.kind
    }

    /// For a webhook, its name. For a dispatch, the name of the calling family.
    #[must_use]
    pub fn name(&self) -> Option<&str> {
        self.name.as_deref()
    }

    /// When the trigger fired. `None` means the start of the turn.
    #[must_use]
    pub fn fired_at(&self) -> Option<Timestamp> {
        self.fired_at
    }

    /// Each family on the way to the job, the first caller first.
    #[must_use]
    pub fn chain(&self) -> &[FamilyName] {
        &self.chain
    }
}

// --- the writer lease ---

const LEASE: &str = "a lease";

/// The writer lease of one session (contract 02 §7.1).
///
/// ```
/// use creche_contracts::session::{Lease, RawLease};
///
/// let lease = Lease::try_from(RawLease {
///     holder: "tui".to_owned(),
///     door_instance: "tui.4242".to_owned(),
///     since: "2026-10-06T08:15:20Z".to_owned(),
///     expires_at: "2026-10-06T08:16:20Z".to_owned(),
///     turn: None,
/// })?;
/// assert_eq!(lease.door_instance(), "tui.4242");
/// # Ok::<(), creche_contracts::session::FieldError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::Lease;
///
/// fn no_turn(lease: Lease) -> Lease {
///     Lease { turn: None, ..lease }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "RawLease")]
pub struct Lease {
    holder: Holder,
    door_instance: String,
    #[serde(serialize_with = "time::seconds")]
    since: Timestamp,
    #[serde(serialize_with = "time::seconds")]
    expires_at: Timestamp,
    turn: Option<Ulid>,
}

/// The fields of a [`Lease`], before the check.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct RawLease {
    /// The door that holds the lease.
    pub holder: String,
    /// The process of that door. `attendance` does not parse the text.
    pub door_instance: String,
    /// When the holder took the lease.
    pub since: String,
    /// When the lease ends.
    pub expires_at: String,
    /// The turn that runs under the lease.
    pub turn: Option<String>,
}

impl TryFrom<RawLease> for Lease {
    type Error = FieldError;

    fn try_from(raw: RawLease) -> Result<Self, Self::Error> {
        Ok(Self {
            holder: parsed(LEASE, "holder", &raw.holder)?,
            door_instance: raw.door_instance,
            since: parsed(LEASE, "since", &raw.since)?,
            expires_at: parsed(LEASE, "expires_at", &raw.expires_at)?,
            turn: optional(LEASE, "turn", raw.turn.as_deref())?,
        })
    }
}

impl Lease {
    /// The bytes of an answer that holds this object, as `attendance` writes
    /// them.
    pub fn body(&self) -> Result<Vec<u8>, EncodeError> {
        json::encode(self, Charset::Utf8)
    }

    /// The door that holds the lease.
    #[must_use]
    pub fn holder(&self) -> Holder {
        self.holder
    }

    /// The process of that door.
    #[must_use]
    pub fn door_instance(&self) -> &str {
        &self.door_instance
    }

    /// When the holder took the lease.
    #[must_use]
    pub fn since(&self) -> Timestamp {
        self.since
    }

    /// When the lease ends.
    #[must_use]
    pub fn expires_at(&self) -> Timestamp {
        self.expires_at
    }

    /// The turn that runs under the lease.
    #[must_use]
    pub fn turn(&self) -> Option<&Ulid> {
        self.turn.as_ref()
    }
}

// --- the session object ---

const SESSION: &str = "a session";

/// One session, as an answer of `attendance` holds it (contract 02 §4.2).
///
/// ```
/// use creche_contracts::session::{RawSession, SessionView};
///
/// let raw: RawSession = serde_json::from_str(
///     r#"{"family": "chat", "session": "owui-8f1c2e", "kind": "attended", "title": "",
///         "state": "idle", "created_at": "2026-10-05T19:21:47Z",
///         "updated_at": "2026-10-05T19:21:47Z", "journal_seq": 0, "writer": null,
///         "turns_total": 0, "terminal_total": 0, "turns_running": 0, "sandbox": null,
///         "persona_hash": null, "labels": {}}"#,
/// )?;
/// let session = SessionView::try_from(raw)?;
/// assert_eq!(session.family().as_str(), "chat");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::SessionView;
///
/// fn renumber(session: SessionView) -> SessionView {
///     SessionView { journal_seq: 0, ..session }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "RawSession")]
pub struct SessionView {
    family: FamilyName,
    session: SessionId,
    kind: SessionKind,
    title: Title,
    state: SessionState,
    #[serde(serialize_with = "time::seconds")]
    created_at: Timestamp,
    #[serde(serialize_with = "time::seconds")]
    updated_at: Timestamp,
    journal_seq: u64,
    writer: Option<Lease>,
    turns_total: u64,
    terminal_total: u64,
    turns_running: u64,
    sandbox: Option<SandboxName>,
    persona_hash: Option<Sha256Hex>,
    labels: Labels,
}

/// The fields of a [`SessionView`], before the check.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct RawSession {
    /// The name of the family.
    pub family: String,
    /// The id of the session.
    pub session: String,
    /// The kind of the family.
    pub kind: String,
    /// The display title.
    pub title: String,
    /// The state of the session.
    pub state: String,
    /// When the session was made.
    pub created_at: String,
    /// When `attendance` wrote the newest journal line.
    pub updated_at: String,
    /// The sequence number of the newest journal line. 0 for no line.
    pub journal_seq: u64,
    /// The writer lease.
    pub writer: Option<RawLease>,
    /// The count of turns and of terminal exchanges.
    pub turns_total: u64,
    /// The count of terminal exchanges in `turns_total`.
    pub terminal_total: u64,
    /// The count of turns that run or wait for an approval.
    pub turns_running: u64,
    /// The sandbox that served the last turn.
    pub sandbox: Option<String>,
    /// The SHA-256 digest of the persona text of the last turn.
    pub persona_hash: Option<String>,
    /// The labels.
    pub labels: BTreeMap<String, String>,
}

impl TryFrom<RawSession> for SessionView {
    type Error = FieldError;

    fn try_from(raw: RawSession) -> Result<Self, Self::Error> {
        Ok(Self {
            family: parsed(SESSION, "family", &raw.family)?,
            session: parsed(SESSION, "session", &raw.session)?,
            kind: parsed(SESSION, "kind", &raw.kind)?,
            title: field(SESSION, "title", raw.title)?,
            state: parsed(SESSION, "state", &raw.state)?,
            created_at: parsed(SESSION, "created_at", &raw.created_at)?,
            updated_at: parsed(SESSION, "updated_at", &raw.updated_at)?,
            journal_seq: raw.journal_seq,
            writer: raw
                .writer
                .map(Lease::try_from)
                .transpose()
                .map_err(|_| FieldError::new(SESSION, "writer"))?,
            turns_total: raw.turns_total,
            terminal_total: raw.terminal_total,
            turns_running: raw.turns_running,
            sandbox: optional(SESSION, "sandbox", raw.sandbox.as_deref())?,
            persona_hash: optional(SESSION, "persona_hash", raw.persona_hash.as_deref())?,
            labels: field(SESSION, "labels", raw.labels)?,
        })
    }
}

impl SessionView {
    /// The bytes of an answer that holds this object, as `attendance` writes
    /// them.
    pub fn body(&self) -> Result<Vec<u8>, EncodeError> {
        json::encode(self, Charset::Utf8)
    }

    /// The name of the family.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The id of the session.
    #[must_use]
    pub fn session(&self) -> &SessionId {
        &self.session
    }

    /// The kind of the family.
    #[must_use]
    pub fn kind(&self) -> SessionKind {
        self.kind
    }

    /// The display title.
    #[must_use]
    pub fn title(&self) -> &Title {
        &self.title
    }

    /// The state of the session.
    #[must_use]
    pub fn state(&self) -> SessionState {
        self.state
    }

    /// When the session was made.
    #[must_use]
    pub fn created_at(&self) -> Timestamp {
        self.created_at
    }

    /// When `attendance` wrote the newest journal line.
    #[must_use]
    pub fn updated_at(&self) -> Timestamp {
        self.updated_at
    }

    /// The sequence number of the newest journal line. 0 for no line.
    #[must_use]
    pub fn journal_seq(&self) -> u64 {
        self.journal_seq
    }

    /// The writer lease.
    #[must_use]
    pub fn writer(&self) -> Option<&Lease> {
        self.writer.as_ref()
    }

    /// The count of turns and of terminal exchanges.
    #[must_use]
    pub fn turns_total(&self) -> u64 {
        self.turns_total
    }

    /// The count of terminal exchanges in `turns_total`.
    #[must_use]
    pub fn terminal_total(&self) -> u64 {
        self.terminal_total
    }

    /// The count of turns that run or wait for an approval.
    #[must_use]
    pub fn turns_running(&self) -> u64 {
        self.turns_running
    }

    /// The sandbox that served the last turn.
    #[must_use]
    pub fn sandbox(&self) -> Option<&SandboxName> {
        self.sandbox.as_ref()
    }

    /// The SHA-256 digest of the persona text of the last turn.
    #[must_use]
    pub fn persona_hash(&self) -> Option<&Sha256Hex> {
        self.persona_hash.as_ref()
    }

    /// The labels.
    #[must_use]
    pub fn labels(&self) -> &Labels {
        &self.labels
    }
}

// --- the turn object ---

const TURN: &str = "a turn";

/// Writes the sandbox of a turn. A turn that no sandbox served has the empty
/// text there, as the Python code writes it.
fn sandbox_or_empty<S: Serializer>(
    sandbox: &Option<SandboxName>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(sandbox.as_ref().map_or("", SandboxName::as_str))
}

/// One turn, as an answer of `attendance` holds it (contract 02 §4.4).
///
/// The object is a record. It does not check the reason against the state:
/// [`Turn`](super::Turn) is the type that holds only the moves of the
/// contract.
///
/// ```
/// use creche_contracts::session::{RawTurn, TurnState, TurnView};
///
/// let raw: RawTurn = serde_json::from_str(
///     r#"{"turn": "01JBQ7WZ0X4T9V6K2H8M3N5PQR", "state": "queued", "reason": null,
///         "started_at": "2026-10-05T19:22:05Z", "ended_at": null, "deadline_s": 3600,
///         "idempotency_key": null, "sandbox": "",
///         "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
///                   "cost_usd": 0.0},
///         "approvals": 0, "owui": null, "persona_truncated": false}"#,
/// )?;
/// let turn = TurnView::try_from(raw)?;
/// assert_eq!(turn.state(), TurnState::Queued);
/// assert_eq!(turn.sandbox(), None);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::TurnView;
///
/// fn recount(turn: TurnView) -> TurnView {
///     TurnView { approvals: 0, ..turn }
/// }
/// ```
// CONTRACT-QUESTION: contract 02 §4.4 says that `sandbox` is a string. The
// Python code writes the empty text for a turn in the queue, which no sandbox
// served. This type reads the empty text as no sandbox and writes it back as
// the empty text. A change to null changes the answer of a release.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "RawTurn")]
pub struct TurnView {
    turn: Ulid,
    state: TurnState,
    reason: Option<TurnReason>,
    #[serde(serialize_with = "time::seconds")]
    started_at: Timestamp,
    #[serde(serialize_with = "time::optional_seconds")]
    ended_at: Option<Timestamp>,
    deadline_s: DeadlineS,
    idempotency_key: Option<IdempotencyKey>,
    #[serde(serialize_with = "sandbox_or_empty")]
    sandbox: Option<SandboxName>,
    usage: Usage,
    approvals: u64,
    owui: Option<OwuiRefs>,
    persona_truncated: bool,
}

/// The fields of a [`TurnView`], before the check.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RawTurn {
    /// The id of the turn.
    pub turn: String,
    /// The state of the turn.
    pub state: String,
    /// Why the turn failed or stopped.
    pub reason: Option<String>,
    /// When `attendance` sent `start_turn`.
    pub started_at: String,
    /// When the turn ended.
    pub ended_at: Option<String>,
    /// The limit of the turn in seconds.
    pub deadline_s: u64,
    /// The idempotency key.
    pub idempotency_key: Option<String>,
    /// The sandbox that served the turn. The empty text for none.
    pub sandbox: String,
    /// The token counts and the cost.
    pub usage: RawUsage,
    /// The count of tool calls that the chaperone held for a person.
    pub approvals: u64,
    /// The Open WebUI ids.
    pub owui: Option<RawOwuiRefs>,
    /// Whether `attendance` cut the persona text at its cap.
    pub persona_truncated: bool,
}

impl TryFrom<RawTurn> for TurnView {
    type Error = FieldError;

    fn try_from(raw: RawTurn) -> Result<Self, Self::Error> {
        let sandbox = Some(raw.sandbox.as_str()).filter(|sandbox| !sandbox.is_empty());

        Ok(Self {
            turn: parsed(TURN, "turn", &raw.turn)?,
            state: parsed(TURN, "state", &raw.state)?,
            reason: optional(TURN, "reason", raw.reason.as_deref())?,
            started_at: parsed(TURN, "started_at", &raw.started_at)?,
            ended_at: optional(TURN, "ended_at", raw.ended_at.as_deref())?,
            deadline_s: field(TURN, "deadline_s", raw.deadline_s)?,
            idempotency_key: optional(TURN, "idempotency_key", raw.idempotency_key.as_deref())?,
            sandbox: optional(TURN, "sandbox", sandbox)?,
            usage: field(TURN, "usage", raw.usage)?,
            approvals: raw.approvals,
            owui: raw
                .owui
                .map(OwuiRefs::try_from)
                .transpose()
                .map_err(|_| FieldError::new(TURN, "owui"))?,
            persona_truncated: raw.persona_truncated,
        })
    }
}

impl TurnView {
    /// The bytes of an answer that holds this object, as `attendance` writes
    /// them.
    pub fn body(&self) -> Result<Vec<u8>, EncodeError> {
        json::encode(self, Charset::Utf8)
    }

    /// The id of the turn.
    #[must_use]
    pub fn turn(&self) -> &Ulid {
        &self.turn
    }

    /// The state of the turn.
    #[must_use]
    pub fn state(&self) -> TurnState {
        self.state
    }

    /// Why the turn failed or stopped.
    #[must_use]
    pub fn reason(&self) -> Option<TurnReason> {
        self.reason
    }

    /// When `attendance` sent `start_turn`.
    #[must_use]
    pub fn started_at(&self) -> Timestamp {
        self.started_at
    }

    /// When the turn ended.
    #[must_use]
    pub fn ended_at(&self) -> Option<Timestamp> {
        self.ended_at
    }

    /// The limit of the turn.
    #[must_use]
    pub fn deadline_s(&self) -> DeadlineS {
        self.deadline_s
    }

    /// The idempotency key.
    #[must_use]
    pub fn idempotency_key(&self) -> Option<&IdempotencyKey> {
        self.idempotency_key.as_ref()
    }

    /// The sandbox that served the turn.
    #[must_use]
    pub fn sandbox(&self) -> Option<&SandboxName> {
        self.sandbox.as_ref()
    }

    /// The token counts and the cost.
    #[must_use]
    pub fn usage(&self) -> &Usage {
        &self.usage
    }

    /// The count of tool calls that the chaperone held for a person.
    #[must_use]
    pub fn approvals(&self) -> u64 {
        self.approvals
    }

    /// The Open WebUI ids.
    #[must_use]
    pub fn owui(&self) -> Option<&OwuiRefs> {
        self.owui.as_ref()
    }

    /// Whether `attendance` cut the persona text at its cap.
    #[must_use]
    pub fn persona_truncated(&self) -> bool {
        self.persona_truncated
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn a_cost_is_a_finite_number_that_is_not_negative() {
        let usage = |cost_usd: f64| RawUsage {
            input: 1,
            output: 2,
            cache_read: 3,
            cache_write: 4,
            cost_usd,
        };

        assert_eq!(
            Usage::try_from(usage(0.0)).map(|usage| usage.cost_usd()),
            Ok(0.0)
        );
        for cost in [f64::NAN, f64::INFINITY, -0.01] {
            assert_eq!(
                Usage::try_from(usage(cost)).unwrap_err().field(),
                "cost_usd"
            );
        }

        assert!(serde_json::from_value::<Usage>(json!({"input": -1})).is_err());
    }

    #[test]
    fn an_owui_id_is_absent_or_has_one_character() {
        let id = || "x".to_owned();

        assert!(OwuiRefs::new(id(), id(), Some(id()), None).is_ok());
        assert_eq!(
            OwuiRefs::new(String::new(), id(), None, None)
                .unwrap_err()
                .field(),
            "chat_id"
        );
        assert_eq!(
            OwuiRefs::new(id(), String::new(), None, None)
                .unwrap_err()
                .field(),
            "message_id"
        );
        assert_eq!(
            OwuiRefs::new(id(), id(), None, Some(String::new()))
                .unwrap_err()
                .field(),
            "parent_id"
        );
    }

    #[test]
    fn only_a_dispatch_trigger_has_a_chain() {
        let family = || "chat".parse::<FamilyName>().unwrap();
        let chain = |count: usize| vec![family(); count];

        assert!(Trigger::new(TriggerKind::Dispatch, None, None, chain(CHAIN_MAX)).is_ok());
        assert!(Trigger::new(TriggerKind::Dispatch, None, None, chain(CHAIN_MAX + 1)).is_err());
        assert!(Trigger::new(TriggerKind::Webhook, None, None, chain(1)).is_err());
        assert_eq!(
            serde_json::to_value(Trigger::new(TriggerKind::Timer, None, None, chain(0)).unwrap())
                .unwrap(),
            json!({"kind": "timer", "name": null, "fired_at": null})
        );
        assert_eq!(
            serde_json::to_value(
                Trigger::new(
                    TriggerKind::Dispatch,
                    Some("chat".to_owned()),
                    Some("2026-10-06T06:00:00.5Z".parse().unwrap()),
                    chain(1)
                )
                .unwrap()
            )
            .unwrap(),
            json!({
                "kind": "dispatch", "name": "chat", "fired_at": "2026-10-06T06:00:00Z",
                "chain": ["chat"]
            })
        );
    }

    #[test]
    fn a_raw_object_with_a_bad_field_names_the_field() {
        let lease = RawLease {
            holder: "view".to_owned(),
            door_instance: String::new(),
            since: "2026-10-06T08:15:20Z".to_owned(),
            expires_at: "soon".to_owned(),
            turn: Some("not a turn".to_owned()),
        };
        let error = Lease::try_from(lease.clone()).unwrap_err();

        assert_eq!(error.field(), "holder");
        assert_eq!(
            error.to_string(),
            "the field holder of a lease is not valid"
        );
        assert_eq!(
            Lease::try_from(RawLease {
                holder: "tui".to_owned(),
                ..lease.clone()
            })
            .unwrap_err()
            .field(),
            "expires_at"
        );
        assert_eq!(
            Lease::try_from(RawLease {
                holder: "tui".to_owned(),
                expires_at: "2026-10-06T08:16:20Z".to_owned(),
                ..lease
            })
            .unwrap_err()
            .field(),
            "turn"
        );
    }
}
