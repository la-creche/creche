//! The journal line and the record of the event stream (contract 02 §8).
//!
//! Three types, for three users:
//!
//! - [`JournalLine`] is a line that `attendance` writes. Its body is a
//!   [`JournalBody`], an enum with the data of each kind.
//! - [`StreamRecord`] is one record of the event stream: a journal line, or
//!   one of the two records that the stream makes itself.
//! - [`StoredLine`] is a line that a replay reads from a journal file. It
//!   keeps the body as its JSON text, as the Python reader keeps each body.
//!   [`StoredLine::body`] gives the typed body.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize, Serializer};
use serde_json::value::RawValue;
use serde_json::{Map, Value};

use super::error::TurnReason;
use super::fields::{
    DeadlineS, Holder, IdempotencyKey, JournalSeq, LeaseReason, Prompt, QueueDepth, SwitchMode,
    Title, TurnRef, words,
};
use super::json::{self, EncodeError, Fault, Kind, Object};
use super::time::Timestamp;
use super::view::{SessionView, Usage};
use crate::ids::{GateId, SandboxName, Sha256Hex, Ulid};

words! {
    /// The kind of one line of the event stream (contract 02 §8.1).
    ///
    /// The set is closed: the contract says that a reader never meets a new
    /// kind. The reader of a journal file refuses a line with another kind, as
    /// the Python reader does.
    LineKind,
    /// Why a text is not the kind of a line.
    LineKindError,
    "the kind of a line",
    {
        /// A session was made. The line holds the session as an answer shows it.
        SessionCreated => "session_created",
        /// The title of a session changed.
        SessionTitled => "session_titled",
        /// The writer lease was granted or released.
        WriterChanged => "writer_changed",
        /// A turn went into the queue.
        TurnQueued => "turn_queued",
        /// `attendance` sent `start_turn`.
        TurnStarted => "turn_started",
        /// One event of pi, as pi sent it.
        PiEvent => "pi_event",
        /// The chaperone holds a tool call.
        ApprovalRequested => "approval_requested",
        /// The phone answered, or the wait ended.
        ApprovalResolved => "approval_resolved",
        /// pi settled. This line means that the turn is over.
        TurnSettled => "turn_settled",
        /// The turn ended and did not settle.
        TurnFailed => "turn_failed",
        /// A caller or the platform stopped the turn.
        TurnAborted => "turn_aborted",
        /// pi refused a fork, or the parent of a message has no entry.
        BranchFallback => "branch_fallback",
        /// One exchange that a terminal wrote to the session store.
        TerminalExchange => "terminal_exchange",
        /// A note for the noticeboard. The body has free form.
        Note => "note",
        /// The stream is idle. No journal file holds this kind.
        Heartbeat => "heartbeat",
    }
}

impl LineKind {
    /// Whether a line of this kind ends a turn.
    #[must_use]
    pub const fn ends_a_turn(self) -> bool {
        matches!(
            self,
            Self::TurnSettled | Self::TurnFailed | Self::TurnAborted
        )
    }
}

/// How the chaperone resolved one gate: the `decision` of an
/// `approval_resolved` line (contract 02 §4.3, contract 04 §6.1).
///
/// The chaperone releases separately, and `attendance` copies its word into
/// the journal. A word that this type does not know is the variant `Other`. A
/// reader accepts it.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum GateReason {
    /// The phone approved the call.
    Approved,
    /// The phone denied the call.
    ApprovalDenied,
    /// No person decided in time.
    ApprovalTimeout,
    /// No phone can see the gate.
    ApprovalUndeliverable,
    /// The family lost the grant before a person decided.
    ApprovalRevoked,
    /// The caller left before a person decided.
    ApprovalAbandoned,
    /// A word that a newer chaperone writes.
    Other(OtherGateReason),
}

/// A word of a gate that [`GateReason`] does not know. Only the conversion
/// from a text makes one, so a value never holds a word that the type knows.
///
/// ```
/// use creche_contracts::session::{GateReason, OtherGateReason};
///
/// let reason = GateReason::from("approval_escalated".to_owned());
/// let GateReason::Other(other) = &reason else {
///     return;
/// };
/// let other: &OtherGateReason = other;
/// assert_eq!(other.as_str(), "approval_escalated");
/// ```
///
/// Code outside this module cannot give a known word the variant `Other`:
///
/// ```compile_fail,E0423
/// use creche_contracts::session::{GateReason, OtherGateReason};
///
/// let reason = GateReason::Other(OtherGateReason("approved".to_owned()));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct OtherGateReason(String);

impl OtherGateReason {
    /// The word on the wire.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Each word of a [`GateReason`] that this type knows.
const GATE_REASONS: [(&str, GateReason); 6] = [
    ("approved", GateReason::Approved),
    ("approval_denied", GateReason::ApprovalDenied),
    ("approval_timeout", GateReason::ApprovalTimeout),
    ("approval_undeliverable", GateReason::ApprovalUndeliverable),
    ("approval_revoked", GateReason::ApprovalRevoked),
    ("approval_abandoned", GateReason::ApprovalAbandoned),
];

impl GateReason {
    /// The word on the wire.
    #[must_use]
    pub fn as_str(&self) -> &str {
        match self {
            Self::Other(word) => word.as_str(),
            known => GATE_REASONS
                .iter()
                .find(|(_, reason)| reason == known)
                .map_or("", |(word, _)| word),
        }
    }
}

impl From<String> for GateReason {
    fn from(word: String) -> Self {
        GATE_REASONS
            .iter()
            .find(|(known, _)| *known == word)
            .map_or(Self::Other(OtherGateReason(word)), |(_, reason)| {
                reason.clone()
            })
    }
}

impl Serialize for GateReason {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for GateReason {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer).map(Self::from)
    }
}

// --- the body of each kind ---

/// The body of a `session_titled` line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionTitled {
    /// The new title.
    pub title: Title,
}

/// The body of a `writer_changed` line.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct WriterChanged {
    /// The door that the change is about.
    pub holder: Holder,
    /// What occurred.
    pub reason: LeaseReason,
}

/// The body of a `turn_queued` line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TurnQueued {
    /// The user text. The journal holds it before the turn enters the queue.
    pub prompt: Prompt,
    /// The idempotency key of the turn.
    pub idempotency_key: Option<IdempotencyKey>,
    /// The place of the turn in the queue of its family, from 1.
    pub queue_depth: QueueDepth,
}

/// The body of a `turn_started` line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TurnStarted {
    /// The user text. It is on disk before `attendance` sends `start_turn`.
    pub prompt: Prompt,
    /// The sandbox that serves the turn.
    pub sandbox: SandboxName,
    /// The limit of the turn.
    pub deadline_s: DeadlineS,
    /// The SHA-256 digest of the persona text of the turn.
    pub persona_hash: Option<Sha256Hex>,
    /// Whether the status document of the family was more than 90 seconds old
    /// when the turn started (contract 02 §5.1).
    pub status_stale: bool,
}

/// The body of an `approval_requested` line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApprovalRequested {
    /// The name of the call that the chaperone holds.
    pub tool: String,
    /// What the call does, for a person. `attendance` writes the empty text.
    pub summary: String,
    /// The id of the gate.
    pub gate_id: GateId,
}

/// The body of an `approval_resolved` line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApprovalResolved {
    /// How the chaperone resolved the gate.
    pub decision: GateReason,
    /// How many seconds the call waited.
    pub waited_s: u64,
}

/// The body of a `turn_settled` line.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TurnSettled {
    /// The token counts and the cost of the turn.
    pub usage: Usage,
    /// The newest pi entry after the turn.
    pub leaf_id: Option<String>,
    /// The pi entry of the user message of the turn.
    pub user_entry_id: Option<String>,
}

/// The body of a `turn_failed` line and of a `turn_aborted` line: a reason,
/// and a message when there is one.
///
/// ```
/// use creche_contracts::session::{TurnEnded, TurnReason};
///
/// let ended = TurnEnded::new(TurnReason::ChannelLost, "");
/// assert_eq!(ended.message(), None);
/// ```
///
/// Code outside this module cannot build a body with an empty message:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{TurnEnded, TurnReason};
///
/// let ended = TurnEnded { reason: TurnReason::ChannelLost, message: Some(String::new()) };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(from = "RawTurnEnded")]
pub struct TurnEnded {
    reason: TurnReason,
    #[serde(skip_serializing_if = "Option::is_none")]
    message: Option<String>,
}

#[derive(Deserialize)]
struct RawTurnEnded {
    reason: TurnReason,
    message: Option<String>,
}

impl From<RawTurnEnded> for TurnEnded {
    fn from(raw: RawTurnEnded) -> Self {
        Self::new(raw.reason, raw.message.as_deref().unwrap_or_default())
    }
}

impl TurnEnded {
    /// Why the turn ended, and a message. An empty message is no message: the
    /// body then holds no `message` key.
    #[must_use]
    pub fn new(reason: TurnReason, message: &str) -> Self {
        Self {
            reason,
            message: Some(message)
                .filter(|message| !message.is_empty())
                .map(str::to_owned),
        }
    }

    /// Why the turn ended.
    #[must_use]
    pub fn reason(&self) -> TurnReason {
        self.reason
    }

    /// The message for a person.
    #[must_use]
    pub fn message(&self) -> Option<&str> {
        self.message.as_deref()
    }
}

/// The body of a `branch_fallback` line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BranchFallback {
    /// The pi entry that the turn was to fork from.
    pub wanted_entry: Option<String>,
    /// Why the turn runs as a plain prompt.
    pub reason: String,
}

/// The body of a `terminal_exchange` line (contract 02 §10.5).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TerminalExchange {
    /// The pi entry of the answer.
    pub entry_id: String,
    /// The pi entry of the prompt.
    pub parent_entry_id: String,
    /// What the person typed in the terminal.
    pub prompt: String,
    /// What the model answered.
    pub answer: String,
}

/// A note that `attendance` writes. Each one has a fixed set of fields.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "note", rename_all = "snake_case", deny_unknown_fields)]
pub enum ServiceNote {
    /// `caregiver` replaced the sandbox of the family (contract 05 §5.3).
    SandboxSwitched {
        /// The sandbox before the switch.
        from: Option<String>,
        /// The sandbox after the switch.
        to: String,
        /// Whether the running turns ended first or stopped.
        mode: SwitchMode,
        /// Why `caregiver` switched.
        reason: String,
    },
    /// `attendance` cut the persona text at its cap (contract 02 §11 rule 6).
    PersonaTruncated {
        /// The cap in bytes.
        cap_bytes: u64,
    },
    /// The playpen gave a reason that this stage never asks for.
    UnexpectedPlaypenReason {
        /// The reason of the playpen.
        reason: String,
    },
    /// The pi process of the session ended.
    ProcessExit {
        /// Why the process ended.
        reason: String,
        /// The exit status.
        code: Option<i64>,
    },
    /// The playpen logged a warning or an error about the session.
    PlaypenLog {
        /// The level of the log line.
        level: String,
        /// The text of the log line.
        message: String,
    },
}

impl ServiceNote {
    /// The count of members of the body: the fields and the word `note`.
    const fn members(&self) -> usize {
        match self {
            Self::SandboxSwitched { .. } => 5,
            Self::ProcessExit { .. } | Self::PlaypenLog { .. } => 3,
            Self::PersonaTruncated { .. } | Self::UnexpectedPlaypenReason { .. } => 2,
        }
    }
}

/// The body of a note that is not a note of `attendance`. Only the conversion
/// from a JSON object makes one, so a value never holds the exact fields of a
/// [`ServiceNote`].
///
/// ```
/// use creche_contracts::session::{Note, OtherNote};
/// use serde_json::{Map, json};
///
/// let mut body = Map::new();
/// body.insert("note".to_owned(), json!("written_by_hand"));
/// let note = Note::from(body);
/// let Note::Other(other) = &note else {
///     return;
/// };
/// let other: &OtherNote = other;
/// assert_eq!(other.body().get("note"), Some(&json!("written_by_hand")));
/// ```
///
/// Code outside this module cannot build one from a raw object:
///
/// ```compile_fail,E0423
/// use creche_contracts::session::{Note, OtherNote};
/// use serde_json::{Map, json};
///
/// let note = Note::Other(OtherNote(Map::new()));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OtherNote(Map<String, Value>);

impl OtherNote {
    /// The body. The writer sorts its keys.
    #[must_use]
    pub fn body(&self) -> &Map<String, Value> {
        &self.0
    }
}

/// The body of a `note` line (contract 02 §8.1). The contract leaves it free.
///
/// A body with the exact fields of a note that `attendance` writes is that
/// note. The writer gives its fields the order of `attendance`. Each other
/// body stays as it is, and the writer sorts its keys.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum Note {
    /// A note that `attendance` writes.
    Service(ServiceNote),
    /// A note of another form.
    Other(OtherNote),
}

impl From<Map<String, Value>> for Note {
    fn from(body: Map<String, Value>) -> Self {
        let members = body.len();
        let body = Value::Object(body);
        match (ServiceNote::deserialize(&body), body) {
            (Ok(note), _) if note.members() == members => Self::Service(note),
            (_, Value::Object(body)) => Self::Other(OtherNote(body)),
            (_, _) => Self::Other(OtherNote(Map::new())),
        }
    }
}

/// The body of one journal line: the data of its kind (contract 02 §8.1).
///
/// Each field of each body is a valid type, so each value of this enum is a
/// body that `attendance` can write.
///
/// The body of a `pi_event` is the event of pi. The contract says that the
/// host does not change it, so it stays a JSON object. The writer sorts the
/// keys of that object. The Python writer keeps the order of pi.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum JournalBody {
    /// `session_created`: the session object.
    SessionCreated(Box<SessionView>),
    /// `session_titled`.
    SessionTitled(SessionTitled),
    /// `writer_changed`.
    WriterChanged(WriterChanged),
    /// `turn_queued`.
    TurnQueued(TurnQueued),
    /// `turn_started`.
    TurnStarted(TurnStarted),
    /// `pi_event`: the event, as pi sent it.
    PiEvent(Map<String, Value>),
    /// `approval_requested`.
    ApprovalRequested(ApprovalRequested),
    /// `approval_resolved`.
    ApprovalResolved(ApprovalResolved),
    /// `turn_settled`.
    TurnSettled(TurnSettled),
    /// `turn_failed`.
    TurnFailed(TurnEnded),
    /// `turn_aborted`.
    TurnAborted(TurnEnded),
    /// `branch_fallback`.
    BranchFallback(BranchFallback),
    /// `terminal_exchange`.
    TerminalExchange(TerminalExchange),
    /// `note`.
    Note(Note),
}

impl JournalBody {
    /// The kind of a line with this body.
    #[must_use]
    pub const fn kind(&self) -> LineKind {
        match self {
            Self::SessionCreated(_) => LineKind::SessionCreated,
            Self::SessionTitled(_) => LineKind::SessionTitled,
            Self::WriterChanged(_) => LineKind::WriterChanged,
            Self::TurnQueued(_) => LineKind::TurnQueued,
            Self::TurnStarted(_) => LineKind::TurnStarted,
            Self::PiEvent(_) => LineKind::PiEvent,
            Self::ApprovalRequested(_) => LineKind::ApprovalRequested,
            Self::ApprovalResolved(_) => LineKind::ApprovalResolved,
            Self::TurnSettled(_) => LineKind::TurnSettled,
            Self::TurnFailed(_) => LineKind::TurnFailed,
            Self::TurnAborted(_) => LineKind::TurnAborted,
            Self::BranchFallback(_) => LineKind::BranchFallback,
            Self::TerminalExchange(_) => LineKind::TerminalExchange,
            Self::Note(_) => LineKind::Note,
        }
    }

    /// The typed body of a line of one kind, from the JSON object of the
    /// body.
    ///
    /// A body that lacks a field of its kind, or that holds a field with a
    /// value that is not valid, is a [`BodyError`]. A field that the kind does
    /// not have is ignored. No Python code reads a body in this way: each door
    /// reads only the fields that it uses.
    pub fn read(kind: LineKind, body: Map<String, Value>) -> Result<Self, BodyError> {
        fn typed<T: serde::de::DeserializeOwned>(
            kind: LineKind,
            body: Map<String, Value>,
        ) -> Result<T, BodyError> {
            serde_json::from_value(Value::Object(body)).map_err(|_| BodyError { kind })
        }

        Ok(match kind {
            LineKind::SessionCreated => Self::SessionCreated(Box::new(typed(kind, body)?)),
            LineKind::SessionTitled => Self::SessionTitled(typed(kind, body)?),
            LineKind::WriterChanged => Self::WriterChanged(typed(kind, body)?),
            LineKind::TurnQueued => Self::TurnQueued(typed(kind, body)?),
            LineKind::TurnStarted => Self::TurnStarted(typed(kind, body)?),
            LineKind::PiEvent => Self::PiEvent(body),
            LineKind::ApprovalRequested => Self::ApprovalRequested(typed(kind, body)?),
            LineKind::ApprovalResolved => Self::ApprovalResolved(typed(kind, body)?),
            LineKind::TurnSettled => Self::TurnSettled(typed(kind, body)?),
            LineKind::TurnFailed => Self::TurnFailed(typed(kind, body)?),
            LineKind::TurnAborted => Self::TurnAborted(typed(kind, body)?),
            LineKind::BranchFallback => Self::BranchFallback(typed(kind, body)?),
            LineKind::TerminalExchange => Self::TerminalExchange(typed(kind, body)?),
            LineKind::Note => Self::Note(Note::from(body)),
            LineKind::Heartbeat => return Err(BodyError { kind }),
        })
    }
}

/// Why a JSON object is not the body of a journal line of one kind.
///
/// The error does not hold the body. The body is untrusted, and a caller
/// writes this error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BodyError {
    kind: LineKind,
}

impl BodyError {
    /// The kind of the line.
    #[must_use]
    pub fn kind(&self) -> LineKind {
        self.kind
    }
}

impl fmt::Display for BodyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "the body is not the body of a {} line of a journal",
            self.kind
        )
    }
}

impl Error for BodyError {}

// --- the line that attendance writes ---

/// The five members of one line, in the order that `attendance` writes them.
#[derive(Serialize)]
struct Envelope<'a, Body: Serialize + ?Sized> {
    journal_seq: Option<u64>,
    ts: String,
    kind: LineKind,
    turn: Option<&'a str>,
    body: &'a Body,
}

/// One line of a journal, as `attendance` writes it (contract 02 §8).
///
/// ```
/// use creche_contracts::session::{
///     Holder, JournalBody, JournalLine, JournalSeq, LeaseReason, Timestamp, WriterChanged,
/// };
///
/// let body = JournalBody::WriterChanged(WriterChanged {
///     holder: Holder::Tui,
///     reason: LeaseReason::Granted,
/// });
/// let ts: Timestamp = "2026-10-05T19:22:05.118Z".parse()?;
/// let line = JournalLine::new(JournalSeq::try_from(42_u64)?, ts, None, body);
/// assert_eq!(
///     String::from_utf8(line.encode()?).ok().as_deref(),
///     Some(concat!(
///         r#"{"journal_seq":42,"ts":"2026-10-05T19:22:05.118Z","kind":"writer_changed","#,
///         r#""turn":null,"body":{"holder":"tui","reason":"granted"}}"#,
///         "\n"
///     ))
/// );
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a line from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::JournalLine;
///
/// fn no_turn(line: JournalLine) -> JournalLine {
///     JournalLine { turn: None, ..line }
/// }
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct JournalLine {
    journal_seq: JournalSeq,
    ts: Timestamp,
    turn: Option<Ulid>,
    body: JournalBody,
}

impl JournalLine {
    /// One line: its sequence number, its time, its turn and its body.
    #[must_use]
    pub fn new(
        journal_seq: JournalSeq,
        ts: Timestamp,
        turn: Option<Ulid>,
        body: JournalBody,
    ) -> Self {
        Self {
            journal_seq,
            ts,
            turn,
            body,
        }
    }

    /// The sequence number.
    #[must_use]
    pub fn journal_seq(&self) -> JournalSeq {
        self.journal_seq
    }

    /// The time at which `attendance` wrote the line.
    #[must_use]
    pub fn ts(&self) -> Timestamp {
        self.ts
    }

    /// The kind of the line.
    #[must_use]
    pub fn kind(&self) -> LineKind {
        self.body.kind()
    }

    /// The turn of the line. `None` for a line of the session.
    #[must_use]
    pub fn turn(&self) -> Option<&Ulid> {
        self.turn.as_ref()
    }

    /// The body.
    #[must_use]
    pub fn body(&self) -> &JournalBody {
        &self.body
    }

    /// The bytes of the line, with its LF.
    pub fn encode(&self) -> Result<Vec<u8>, EncodeError> {
        json::encode_line(&Envelope {
            journal_seq: Some(self.journal_seq.get()),
            ts: self.ts.rfc3339_millis(),
            kind: self.kind(),
            turn: self.turn.as_ref().map(Ulid::as_str),
            body: &self.body,
        })
    }
}

// --- the record of the event stream ---

/// The word of the note that ends the stream of a slow reader (contract 02
/// §5.5).
const STREAM_OVERRUN: &str = "stream_overrun";

#[derive(Serialize)]
struct HeartbeatBody {
    last_seq: u64,
}

#[derive(Serialize)]
struct OverrunBody {
    note: &'static str,
    last_seq: u64,
}

/// One record of the event stream (contract 02 §5.5, §8.1).
///
/// A journal line has a sequence number. The two records that the stream makes
/// itself have none: they are not in a journal file, and they hold the newest
/// sequence number that the reader got.
///
/// ```
/// use creche_contracts::session::{StreamRecord, Timestamp};
///
/// let ts: Timestamp = "2026-10-05T19:22:31.070Z".parse()?;
/// let record = StreamRecord::Heartbeat { ts, last_seq: 118 };
/// assert_eq!(
///     String::from_utf8(record.encode()?).ok().as_deref(),
///     Some(concat!(
///         r#"{"journal_seq":null,"ts":"2026-10-05T19:22:31.070Z","kind":"heartbeat","#,
///         r#""turn":null,"body":{"last_seq":118}}"#,
///         "\n"
///     ))
/// );
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
#[derive(Debug, Clone, PartialEq)]
pub enum StreamRecord {
    /// A line of the journal.
    Line(JournalLine),
    /// The stream is idle. A reader tells a live stream from a dead socket by
    /// this record.
    Heartbeat {
        /// When the stream made the record.
        ts: Timestamp,
        /// The newest sequence number that the reader got. 0 for none.
        last_seq: u64,
    },
    /// The reader fell behind. The stream ends after this record, and the
    /// reader attaches again after `last_seq`.
    Overrun {
        /// When the stream made the record.
        ts: Timestamp,
        /// The newest sequence number that the reader got. 0 for none.
        last_seq: u64,
    },
}

impl StreamRecord {
    /// The kind of the record. An overrun is a `note`.
    #[must_use]
    pub fn kind(&self) -> LineKind {
        match self {
            Self::Line(line) => line.kind(),
            Self::Heartbeat { .. } => LineKind::Heartbeat,
            Self::Overrun { .. } => LineKind::Note,
        }
    }

    /// The bytes of the record, with its LF.
    pub fn encode(&self) -> Result<Vec<u8>, EncodeError> {
        match self {
            Self::Line(line) => line.encode(),
            Self::Heartbeat { ts, last_seq } => json::encode_line(&Envelope {
                journal_seq: None,
                ts: ts.rfc3339_millis(),
                kind: LineKind::Heartbeat,
                turn: None,
                body: &HeartbeatBody {
                    last_seq: *last_seq,
                },
            }),
            Self::Overrun { ts, last_seq } => json::encode_line(&Envelope {
                journal_seq: None,
                ts: ts.rfc3339_millis(),
                kind: LineKind::Note,
                turn: None,
                body: &OverrunBody {
                    note: STREAM_OVERRUN,
                    last_seq: *last_seq,
                },
            }),
        }
    }
}

// --- the line that a replay reads ---

/// The JSON text of a body that a line does not have.
const EMPTY_BODY: &str = "{}";

/// The byte that ends a line of a journal file.
const LF: u8 = b'\n';

/// One line of a journal file, as a replay reads it (contract 02 §8, §9).
///
/// The reader is the reader of `attendance.journal`: it is lax where that
/// reader is lax.
///
/// - `journal_seq` is a whole number that is 1 or more. The JSON value `true`
///   is the number 1.
/// - `kind` is one of the kinds of the contract.
/// - `turn` is each text. A `turn` of another type is absent.
/// - `ts` is a time. A `ts` that the reader cannot read is absent, and the
///   caller gives the line the time of the read.
/// - `body` is an object. A `body` of another type is the empty object. The
///   reader does not check the body against the kind.
///
/// ```
/// use creche_contracts::session::{LineKind, StoredLine};
///
/// let line = StoredLine::parse(
///     br#"{"journal_seq":42,"ts":"2026-10-05T19:22:05.118Z","kind":"note","turn":null,"body":[]}"#,
/// )?;
/// assert_eq!(line.journal_seq().get(), 42);
/// assert_eq!(line.kind(), LineKind::Note);
/// assert_eq!(line.body_json(), "{}");
/// # Ok::<(), creche_contracts::session::LineError>(())
/// ```
///
/// Code outside this module cannot build a line from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::StoredLine;
///
/// fn no_turn(line: StoredLine) -> StoredLine {
///     StoredLine { turn: None, ..line }
/// }
/// ```
#[derive(Debug, Clone)]
pub struct StoredLine {
    journal_seq: JournalSeq,
    ts: Option<Timestamp>,
    kind: LineKind,
    turn: Option<TurnRef>,
    body: Box<RawValue>,
}

impl StoredLine {
    /// The line that the bytes of one line of a journal file hold. The bytes
    /// can end with white space.
    ///
    /// A journal file has a line that does not parse only after a crash in
    /// the middle of a write. A replay skips such a line.
    ///
    /// The bytes hold no LF before the white space at their end. A replay
    /// reads a file line by line, so no line of a file holds one. This type
    /// keeps the body as its text, and an LF there goes out as two lines of
    /// the event stream.
    pub fn parse(line: &[u8]) -> Result<Self, LineError> {
        if line.trim_ascii_end().contains(&LF) {
            return Err(LineError::NotOneLine);
        }

        let object = Object::read(line)?;
        let journal_seq = match object.kind("journal_seq") {
            Some(Kind::Integer) => object.int("journal_seq"),
            Some(Kind::True) => Some(1),
            _ => None,
        };
        let journal_seq = journal_seq
            .and_then(|number| JournalSeq::try_from(number).ok())
            .ok_or(LineError::BadSeq)?;
        let kind = object
            .text("kind")?
            .and_then(|kind| kind.parse().ok())
            .ok_or(LineError::BadKind)?;
        let turn = object.text("turn")?.map(TurnRef::from);
        let ts = object
            .text("ts")
            .ok()
            .flatten()
            .and_then(|ts| ts.parse().ok());
        let body = object
            .get("body")
            .filter(|body| json::kind_of(body) == Kind::Object)
            .map(RawValue::to_owned);
        let body = match body {
            Some(body) => body,
            None => RawValue::from_string(EMPTY_BODY.to_owned()).map_err(|_| LineError::NotJson)?,
        };

        Ok(Self {
            journal_seq,
            ts,
            kind,
            turn,
            body,
        })
    }

    /// The sequence number.
    #[must_use]
    pub fn journal_seq(&self) -> JournalSeq {
        self.journal_seq
    }

    /// The time at which `attendance` wrote the line. `None` for a line whose time the
    /// reader cannot read.
    #[must_use]
    pub fn ts(&self) -> Option<Timestamp> {
        self.ts
    }

    /// The kind of the line.
    #[must_use]
    pub fn kind(&self) -> LineKind {
        self.kind
    }

    /// The turn of the line.
    #[must_use]
    pub fn turn(&self) -> Option<&TurnRef> {
        self.turn.as_ref()
    }

    /// The body, as the JSON text of an object.
    #[must_use]
    pub fn body_json(&self) -> &str {
        self.body.get()
    }

    /// The typed body of the line.
    pub fn body(&self) -> Result<JournalBody, BodyError> {
        let kind = self.kind;
        let body = serde_json::from_str(self.body.get()).map_err(|_| BodyError { kind })?;

        JournalBody::read(kind, body)
    }

    /// The bytes of the line for a reader of the event stream, with its LF.
    /// `read_at` is the time for a line whose time the reader cannot read.
    ///
    /// The body goes out as the text that the file holds. For a line that
    /// `attendance` wrote, the bytes are the bytes of the file.
    pub fn encode(&self, read_at: Timestamp) -> Result<Vec<u8>, EncodeError> {
        json::encode_line(&Envelope {
            journal_seq: Some(self.journal_seq.get()),
            ts: self.ts.unwrap_or(read_at).rfc3339_millis(),
            kind: self.kind,
            turn: self.turn.as_ref().map(TurnRef::as_str),
            body: self.body.as_ref(),
        })
    }
}

/// Why the bytes of one line are not a line of a journal.
///
/// No variant holds the line. The line is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LineError {
    /// The bytes hold an LF before their end, so they are not one line.
    NotOneLine,
    /// The bytes are not one JSON text.
    NotJson,
    /// The JSON text is not an object.
    NotObject,
    /// `journal_seq` is absent, is not a whole number, or is not 1 to
    /// 2^64 - 1.
    BadSeq,
    /// `kind` is absent, is not a text, or is not a kind of the contract.
    BadKind,
}

impl From<Fault> for LineError {
    fn from(fault: Fault) -> Self {
        match fault {
            Fault::NotJson => Self::NotJson,
            Fault::NotObject => Self::NotObject,
        }
    }
}

impl fmt::Display for LineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NotOneLine => "a line of a journal holds no LF before its end",
            Self::NotJson => "a line of a journal is one JSON text",
            Self::NotObject => "a line of a journal is a JSON object",
            Self::BadSeq => "the journal_seq of a line is a whole number from 1",
            Self::BadKind => "the kind of a line is a kind of the contract",
        })
    }
}

impl Error for LineError {}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn object(value: Value) -> Map<String, Value> {
        match value {
            Value::Object(object) => object,
            other => panic!("{other} is not an object"),
        }
    }

    fn at(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    #[test]
    fn a_gate_reason_that_is_not_known_is_kept() {
        for (word, reason) in &GATE_REASONS {
            assert_eq!(&GateReason::from((*word).to_owned()), reason);
            assert_eq!(reason.as_str(), *word);
        }

        let other = GateReason::from("approval_escalated".to_owned());

        assert!(matches!(&other, GateReason::Other(word) if word.as_str() == "approval_escalated"));
        assert_eq!(
            serde_json::to_string(&other).unwrap(),
            "\"approval_escalated\""
        );
        assert_eq!(
            serde_json::from_str::<GateReason>("\"approved\"").unwrap(),
            GateReason::Approved
        );
    }

    #[test]
    fn a_note_of_the_service_has_its_exact_fields() {
        let exit = Note::from(object(
            json!({"note": "process_exit", "reason": "idle", "code": null}),
        ));
        let extra = object(json!({"note": "process_exit", "reason": "idle", "code": 1, "x": 1}));
        let missing = object(json!({"note": "process_exit", "reason": "idle"}));
        let wrong_type = object(json!({"note": "persona_truncated", "cap_bytes": "16384"}));
        let other_word = object(json!({"note": "written_by_hand"}));

        assert_eq!(
            exit,
            Note::Service(ServiceNote::ProcessExit {
                reason: "idle".to_owned(),
                code: None
            })
        );
        for body in [extra, missing, wrong_type, other_word, Map::new()] {
            assert!(
                matches!(Note::from(body.clone()), Note::Other(other) if other.body() == &body)
            );
        }
    }

    #[test]
    fn a_note_of_the_service_keeps_the_order_of_the_service() {
        let switched = Note::from(object(json!({
            "to": "chat-s3", "reason": "", "note": "sandbox_switched", "mode": "drain", "from": null
        })));

        assert_eq!(
            serde_json::to_string(&switched).unwrap(),
            r#"{"note":"sandbox_switched","from":null,"to":"chat-s3","mode":"drain","reason":""}"#
        );
    }

    #[test]
    fn a_body_that_is_not_of_its_kind_is_refused() {
        let titled = JournalBody::read(LineKind::SessionTitled, object(json!({"title": "A"})));
        let no_title = JournalBody::read(LineKind::SessionTitled, Map::new());
        let long = object(json!({"title": "t".repeat(201)}));
        let heartbeat = JournalBody::read(LineKind::Heartbeat, object(json!({"last_seq": 1})));

        assert_eq!(titled.unwrap().kind(), LineKind::SessionTitled);
        assert_eq!(no_title.unwrap_err().kind(), LineKind::SessionTitled);
        assert!(JournalBody::read(LineKind::SessionTitled, long).is_err());
        assert_eq!(
            heartbeat.unwrap_err().to_string(),
            "the body is not the body of a heartbeat line of a journal"
        );
    }

    #[test]
    fn an_ended_turn_has_a_message_or_no_message_key() {
        let quiet = TurnEnded::new(TurnReason::QueueLost, "");
        let loud: TurnEnded =
            serde_json::from_value(json!({"reason": "model_error", "message": "x"})).unwrap();
        let empty: TurnEnded =
            serde_json::from_value(json!({"reason": "model_error", "message": ""})).unwrap();

        assert_eq!(
            serde_json::to_string(&quiet).unwrap(),
            r#"{"reason":"queue_lost"}"#
        );
        assert_eq!(loud.message(), Some("x"));
        assert_eq!(loud.reason(), TurnReason::ModelError);
        assert_eq!(empty.message(), None);
    }

    #[test]
    fn a_stored_line_is_read_as_the_python_reader_reads_it() {
        let line = StoredLine::parse(
            br#"{"journal_seq":true,"ts":"just now","kind":"turn_failed","turn":"x","body":5,"y":1}"#,
        )
        .unwrap();

        assert_eq!(line.journal_seq().get(), 1);
        assert_eq!(line.ts(), None);
        assert_eq!(line.kind(), LineKind::TurnFailed);
        assert_eq!(line.turn().map(TurnRef::as_str), Some("x"));
        assert_eq!(line.turn().and_then(TurnRef::id), None);
        assert_eq!(line.body_json(), "{}");
        assert_eq!(line.body().unwrap_err().kind(), LineKind::TurnFailed);
    }

    #[test]
    fn a_line_with_a_bad_seq_or_a_bad_kind_is_refused() {
        let line = |seq: &str, kind: &str| {
            StoredLine::parse(format!(r#"{{"journal_seq":{seq},"kind":{kind}}}"#).as_bytes())
                .map(|line| line.journal_seq().get())
        };

        assert_eq!(line("1", "\"note\""), Ok(1));
        assert_eq!(line("18446744073709551615", "\"note\""), Ok(u64::MAX));
        for seq in [
            "0",
            "-1",
            "false",
            "null",
            "\"1\"",
            "1.0",
            "1e0",
            "18446744073709551616",
        ] {
            assert_eq!(line(seq, "\"note\""), Err(LineError::BadSeq), "{seq}");
        }
        for kind in ["\"Note\"", "\"stream_overrun\"", "\"\"", "null", "5"] {
            assert_eq!(line("1", kind), Err(LineError::BadKind), "{kind}");
        }

        assert_eq!(StoredLine::parse(b"").unwrap_err(), LineError::NotJson);
        assert_eq!(StoredLine::parse(b"[]").unwrap_err(), LineError::NotObject);
        assert_eq!(StoredLine::parse(b"{}").unwrap_err(), LineError::BadSeq);
    }

    #[test]
    fn a_stored_line_is_one_line() {
        let inside = b"{\"journal_seq\":7,\"kind\":\"note\",\"body\":{\"a\":\n1}}";
        let before = b"\n{\"journal_seq\":7,\"kind\":\"note\"}";
        let at_the_end = b"{\"journal_seq\":7,\"kind\":\"note\"} \r\n";
        let escaped = br#"{"journal_seq":7,"kind":"note","body":{"a":"\n"}}"#;

        assert_eq!(
            StoredLine::parse(inside).unwrap_err(),
            LineError::NotOneLine
        );
        assert_eq!(
            StoredLine::parse(before).unwrap_err(),
            LineError::NotOneLine
        );
        assert!(StoredLine::parse(at_the_end).is_ok());
        assert_eq!(
            StoredLine::parse(escaped).unwrap().body_json(),
            r#"{"a":"\n"}"#
        );
    }

    #[test]
    fn a_stored_line_goes_out_with_the_text_of_its_body() {
        let text = concat!(
            r#"{"journal_seq":7,"ts":"2026-10-05T19:22:05.118Z","kind":"pi_event","#,
            r#""turn":"01JBQ7WZ0X4T9V6K2H8M3N5PQR","body":{"b":1.50,"a":[1, 2]}}"#
        );
        let line = StoredLine::parse(text.as_bytes()).unwrap();
        let untimed = StoredLine::parse(br#"{"journal_seq":8,"kind":"note"}"#).unwrap();

        assert_eq!(
            line.encode(at("2000-01-01T00:00:00Z")).unwrap(),
            format!("{text}\n").into_bytes()
        );
        assert_eq!(
            String::from_utf8(untimed.encode(at("2000-01-01T00:00:00Z")).unwrap()).unwrap(),
            concat!(
                r#"{"journal_seq":8,"ts":"2000-01-01T00:00:00.000Z","kind":"note","turn":null,"#,
                r#""body":{}}"#,
                "\n"
            )
        );
        assert!(line.turn().and_then(TurnRef::id).is_some());
    }

    #[test]
    fn an_overrun_is_a_note_with_no_sequence_number() {
        let record = StreamRecord::Overrun {
            ts: at("2026-10-05T19:22:31.070Z"),
            last_seq: 77,
        };

        assert_eq!(record.kind(), LineKind::Note);
        assert_eq!(
            String::from_utf8(record.encode().unwrap()).unwrap(),
            concat!(
                r#"{"journal_seq":null,"ts":"2026-10-05T19:22:31.070Z","kind":"note","#,
                r#""turn":null,"body":{"note":"stream_overrun","last_seq":77}}"#,
                "\n"
            )
        );
    }

    #[test]
    fn the_three_terminal_kinds_end_a_turn() {
        let ending: Vec<&str> = LineKind::ALL
            .iter()
            .filter(|kind| kind.ends_a_turn())
            .map(|kind| kind.as_str())
            .collect();

        assert_eq!(ending, ["turn_settled", "turn_failed", "turn_aborted"]);
        assert_eq!(LineKind::ALL.len(), 15);
    }
}
