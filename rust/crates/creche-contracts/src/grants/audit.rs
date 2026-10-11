//! The audit record v2 (contract 04 §6), the record of a request that names
//! no family, and the three advisory headers (contract 04 §3).
//!
//! The chaperone writes one line of JSON for each record. `to_line` gives the
//! bytes that the Python chaperone writes for the same record, so a reader of
//! an audit file sees no difference between the two writers.

use std::error::Error;
use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::ids::{FamilyName, GateId, SandboxName, SessionId, Sha256Hex, Ulid};
use crate::time::Timestamp;

use super::body::{Arguments, BODY_MAX_BYTES, CallTool};
use super::decision::{AuditOutcome, Held};
use super::file::GrantsRev;
use super::json::{self, Charset, Integer, KeyOrder, Layout, Map, Style, Value};

/// The largest count of characters of one string of `args` in an audit
/// record: 8 KiB (contract 04 §6.1). A character is one code point.
pub const AUDIT_TEXT_MAX_CHARS: usize = 8 * 1024;

/// What the chaperone writes after a string that it cut.
const TRUNCATION_MARKER: &str = "...<truncated>";

/// The `tool` of the record of a manifest fetch (contract 04 §6). A `$` cannot
/// start the name of a tool.
const MANIFEST_ACTION: &str = "$manifest";

/// The `tool` of the record of a body over the cap. The chaperone refuses the
/// body before it reads a tool.
const OVERSIZED_ACTION: &str = "$oversized";

/// The `reason` of the record of a body over the cap. It is not a reason of
/// contract 04 §5: no decision ran.
const REASON_BODY_TOO_LARGE: &str = "body_too_large";

/// The header that holds the session id (contract 04 §3). The name is in lower
/// case: an HTTP header name has no case.
pub const SESSION_ID_HEADER: &str = "x-session-id";

/// The header that holds the ULID of the turn (contract 04 §3).
pub const TURN_ID_HEADER: &str = "x-turn-id";

/// The header that holds the delegation id (contract 04 §3).
pub const DELEGATION_ID_HEADER: &str = "x-delegation-id";

// --- the time ---

/// The microseconds of one millisecond.
const MICROS_PER_MS: i64 = 1000;

/// The time of one record: a UTC time in whole milliseconds, from 1970 to the
/// end of the year 9999 (contract 04 §6.1).
///
/// The type holds a [`Timestamp`], and that type writes each time text of a
/// record. The last time of this type is the last millisecond of a
/// [`Timestamp`].
///
/// ```
/// use creche_contracts::grants::AuditTime;
/// use creche_contracts::time::Timestamp;
///
/// let time = AuditTime::from_unix_ms(1_789_760_467_412)?;
/// assert_eq!(time.unix_ms(), 1_789_760_467_412);
/// assert_eq!(time.file_name(), "2026-09-18.jsonl");
///
/// let last_ms = Timestamp::MAX.unix_micros().unsigned_abs() / 1000;
/// assert_eq!(AuditTime::from_unix_ms(last_ms)?.file_name(), "9999-12-31.jsonl");
/// assert!(AuditTime::from_unix_ms(last_ms + 1).is_err());
/// # Ok::<(), creche_contracts::grants::AuditTimeError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw instant:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::AuditTime;
/// use creche_contracts::time::Timestamp;
///
/// let time = AuditTime { instant: Timestamp::MIN };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AuditTime {
    /// An instant of whole milliseconds that is not before 1970.
    instant: Timestamp,
}

impl AuditTime {
    /// The time that is `unix_ms` milliseconds after 1970-01-01T00:00:00Z.
    ///
    /// # Errors
    ///
    /// [`AuditTimeError::AfterYear9999`] for a time after the last
    /// millisecond of the year 9999.
    pub fn from_unix_ms(unix_ms: u64) -> Result<Self, AuditTimeError> {
        let micros = i64::try_from(unix_ms)
            .ok()
            .and_then(|unix_ms| unix_ms.checked_mul(MICROS_PER_MS))
            .ok_or(AuditTimeError::AfterYear9999)?;
        let instant =
            Timestamp::from_unix_micros(micros).map_err(|_| AuditTimeError::AfterYear9999)?;

        Ok(Self { instant })
    }

    /// The milliseconds after 1970-01-01T00:00:00Z.
    #[must_use]
    pub fn unix_ms(&self) -> u64 {
        // No time of this type is before 1970, so the count has no sign.
        (self.instant.unix_micros() / MICROS_PER_MS).unsigned_abs()
    }

    /// The name of the file that takes a record of this time: the UTC day,
    /// `YYYY-MM-DD.jsonl` (contract 04 §6).
    #[must_use]
    pub fn file_name(&self) -> String {
        // The date of a time text ends at its `T`.
        let text = self.instant.to_rfc3339();
        let day = text.split_once('T').map_or(text.as_str(), |(day, _)| day);

        format!("{day}.jsonl")
    }
}

impl TryFrom<SystemTime> for AuditTime {
    type Error = AuditTimeError;

    /// The time of a clock, cut to whole milliseconds.
    fn try_from(time: SystemTime) -> Result<Self, Self::Error> {
        let since = time
            .duration_since(UNIX_EPOCH)
            .map_err(|_| AuditTimeError::BeforeEpoch)?;
        let unix_ms =
            u64::try_from(since.as_millis()).map_err(|_| AuditTimeError::AfterYear9999)?;

        Self::from_unix_ms(unix_ms)
    }
}

/// Why a time is not the time of a record.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuditTimeError {
    /// The time is before 1970.
    BeforeEpoch,
    /// The time is after the year 9999.
    AfterYear9999,
}

impl fmt::Display for AuditTimeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BeforeEpoch => f.write_str("the time of a record is in 1970 or later"),
            Self::AfterYear9999 => f.write_str("the time of a record is in 9999 or before"),
        }
    }
}

impl Error for AuditTimeError {}

// --- the advisory headers ---

/// One of the three advisory headers (contract 04 §3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ClaimedHeader {
    /// `X-Session-Id`.
    SessionId,
    /// `X-Turn-Id`.
    TurnId,
    /// `X-Delegation-Id`.
    DelegationId,
}

impl ClaimedHeader {
    /// The name of the header, in lower case.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::SessionId => SESSION_ID_HEADER,
            Self::TurnId => TURN_ID_HEADER,
            Self::DelegationId => DELEGATION_ID_HEADER,
        }
    }
}

/// The values of the three advisory headers of one request, as the caller sent
/// them. `None` stands for a header that the request does not have.
///
/// This is the raw form. Each value is untrusted: the agent process writes it.
/// [`Claimed::read`] is the one check of the three values.
///
/// ```
/// use creche_contracts::grants::RawClaimed;
///
/// let raw = RawClaimed::new().with_turn_id("01JBQ7WZ0X4T9V6K2H8M3N5PQR");
/// assert_eq!(raw.session_id(), None);
/// assert_eq!(raw.turn_id(), Some("01JBQ7WZ0X4T9V6K2H8M3N5PQR"));
/// assert_eq!(raw.delegation_id(), None);
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::RawClaimed;
///
/// let raw = RawClaimed { session_id: None, turn_id: None, delegation_id: None };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RawClaimed<'a> {
    session_id: Option<&'a str>,
    turn_id: Option<&'a str>,
    delegation_id: Option<&'a str>,
}

impl<'a> RawClaimed<'a> {
    /// The headers of a request with no advisory header.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The same headers with this value of `X-Session-Id`.
    #[must_use]
    pub fn with_session_id(mut self, value: &'a str) -> Self {
        self.session_id = Some(value);

        self
    }

    /// The same headers with this value of `X-Turn-Id`.
    #[must_use]
    pub fn with_turn_id(mut self, value: &'a str) -> Self {
        self.turn_id = Some(value);

        self
    }

    /// The same headers with this value of `X-Delegation-Id`.
    #[must_use]
    pub fn with_delegation_id(mut self, value: &'a str) -> Self {
        self.delegation_id = Some(value);

        self
    }

    /// The value of `X-Session-Id`.
    #[must_use]
    pub fn session_id(&self) -> Option<&'a str> {
        self.session_id
    }

    /// The value of `X-Turn-Id`.
    #[must_use]
    pub fn turn_id(&self) -> Option<&'a str> {
        self.turn_id
    }

    /// The value of `X-Delegation-Id`.
    #[must_use]
    pub fn delegation_id(&self) -> Option<&'a str> {
        self.delegation_id
    }
}

/// One header whose value does not have the shape of its id. The chaperone
/// writes this to its log.
///
/// The type holds the count of characters and never the value: the value is
/// text that the caller chose (contract 04 §3.1 rule 4).
///
/// Only [`Claimed::read`] builds a value:
///
/// ```
/// use creche_contracts::grants::{Claimed, ClaimedHeader, DroppedHeader, RawClaimed};
///
/// let (_, dropped) = Claimed::read(&RawClaimed::new().with_turn_id("not a ULID"));
/// let dropped: &DroppedHeader = &dropped[0];
/// assert_eq!(dropped.header(), ClaimedHeader::TurnId);
/// assert_eq!(dropped.chars(), 10);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{Claimed, ClaimedHeader, DroppedHeader, RawClaimed};
///
/// let dropped = DroppedHeader { header: ClaimedHeader::TurnId, chars: 10 };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DroppedHeader {
    header: ClaimedHeader,
    chars: usize,
}

impl DroppedHeader {
    /// The header.
    #[must_use]
    pub fn header(&self) -> ClaimedHeader {
        self.header
    }

    /// The count of characters of its value.
    #[must_use]
    pub fn chars(&self) -> usize {
        self.chars
    }
}

/// What the caller says about itself (contract 04 §3, §6.2).
///
/// No field is evidence. No decision reads a field (contract 04 §3.1 rule 1).
/// An audit record writes the three fields under `claimed`, apart from the
/// trusted fields.
///
/// A header with a value of the wrong shape reads as a header that is not
/// there. It never denies the call.
///
/// ```
/// use creche_contracts::grants::{Claimed, ClaimedHeader, RawClaimed};
///
/// let raw = RawClaimed::new()
///     .with_session_id("tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK")
///     .with_turn_id("not a ULID");
/// let (claimed, dropped) = Claimed::read(&raw);
///
/// assert_eq!(claimed.session_id().map(|id| id.as_str()), Some("tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"));
/// assert_eq!(claimed.turn_id(), None);
/// assert_eq!(dropped.len(), 1);
/// assert_eq!(dropped[0].header(), ClaimedHeader::TurnId);
/// assert_eq!(dropped[0].chars(), 10);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::Claimed;
///
/// let claimed = Claimed { session_id: None, turn_id: None, delegation_id: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Claimed {
    session_id: Option<SessionId>,
    turn_id: Option<Ulid>,
    delegation_id: Option<Ulid>,
}

impl Claimed {
    /// The claims of a request with no advisory header.
    #[must_use]
    pub fn none() -> Self {
        Self {
            session_id: None,
            turn_id: None,
            delegation_id: None,
        }
    }

    /// Reads the three headers of one request. The second value names each
    /// header that the function dropped, for the log.
    ///
    /// This is a process edge. A value of the wrong shape is dropped, and the
    /// call goes on.
    #[must_use]
    pub fn read(raw: &RawClaimed<'_>) -> (Self, Vec<DroppedHeader>) {
        let mut dropped = Vec::new();
        let mut note = |header: ClaimedHeader, value: &str| {
            let chars = value.chars().count();
            dropped.push(DroppedHeader { header, chars });
        };
        let session_id = raw.session_id.and_then(|value| {
            let read = value.parse().ok();
            if read.is_none() {
                note(ClaimedHeader::SessionId, value);
            }

            read
        });
        let mut ulid = |header: ClaimedHeader, value: Option<&str>| {
            value.and_then(|value| {
                let read = value.parse::<Ulid>().ok();
                if read.is_none() {
                    note(header, value);
                }

                read
            })
        };
        let turn_id = ulid(ClaimedHeader::TurnId, raw.turn_id);
        let delegation_id = ulid(ClaimedHeader::DelegationId, raw.delegation_id);
        let claimed = Self {
            session_id,
            turn_id,
            delegation_id,
        };

        (claimed, dropped)
    }

    /// The session id that the caller claims.
    #[must_use]
    pub fn session_id(&self) -> Option<&SessionId> {
        self.session_id.as_ref()
    }

    /// The turn id that the caller claims.
    #[must_use]
    pub fn turn_id(&self) -> Option<&Ulid> {
        self.turn_id.as_ref()
    }

    /// The delegation id that the caller claims.
    #[must_use]
    pub fn delegation_id(&self) -> Option<&Ulid> {
        self.delegation_id.as_ref()
    }
}

// --- the audit record v2 ---

/// Which sandbox made the call, and whether that is evidence (contract 04
/// §3.2).
///
/// A record names a sandbox only with proof: `sandbox_id` holds a name only
/// when `sandbox_id_trusted` is `true`. The type has no value for a name that
/// a caller gives with no proof.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SandboxEvidence {
    /// The chaperone could not map the connection to a sandbox. The Python
    /// chaperone writes this value for each record.
    Unknown,
    /// The connection comes from the one address of this sandbox.
    Trusted(SandboxName),
}

/// The `tool` of one record: what the caller asked for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuditAction {
    /// A `POST /call` with this tool.
    Call(CallTool),
    /// A `GET /manifest`. The record says `$manifest`.
    Manifest,
}

impl AuditAction {
    /// The text of the `tool` field.
    #[must_use]
    pub fn as_str(&self) -> &str {
        match self {
            Self::Call(tool) => tool.as_str(),
            Self::Manifest => MANIFEST_ACTION,
        }
    }
}

/// The gate of a call and the wait for the operator at that gate. The two are
/// one fact of a record: a record has a wait only with a gate.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Gated {
    gate: GateId,
    waited_ms: u64,
}

/// One audit record, version 2: one decision of the chaperone for one family
/// (contract 04 §6).
///
/// Each field has a valid type. The type holds one rule between two keys of
/// the line: a record has a wait for the operator only with a gate.
/// [`with_gate`] sets the two together, and it takes the gate from the
/// [`Held`] of the call. The two records of a gated call then name the same
/// gate (contract 04 §6.4).
///
/// The type holds no rule between the outcome and the gate. The type permits a
/// record with the outcome [`AuditOutcome::Pending`] and no gate. The writer of
/// the port holds that rule.
///
/// A record keeps the trusted fields apart from the claimed fields. The claimed
/// fields come only from a [`Claimed`].
///
/// ```
/// use creche_contracts::grants::{
///     Arguments, AuditAction, AuditOutcome, AuditRecord, AuditTime, Claimed,
/// };
///
/// let record = AuditRecord::new(
///     AuditTime::from_unix_ms(1_789_760_467_412)?,
///     "chat".parse().unwrap(),
///     AuditAction::Manifest,
///     Arguments::empty(),
///     AuditOutcome::Granted,
///     Claimed::none(),
/// )
/// .with_grants_rev("reg-9f21c4".parse().unwrap());
///
/// assert_eq!(record.family().as_str(), "chat");
/// assert_eq!(record.gate(), None);
/// assert_eq!(record.waited_ms(), 0);
/// assert!(record.to_line().ends_with(b"\"chain\": [\"chat\"]}\n"));
/// # Ok::<(), creche_contracts::grants::AuditTimeError>(())
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{
///     Arguments, AuditAction, AuditOutcome, AuditRecord, AuditTime, Claimed,
/// };
///
/// let record = AuditRecord::new(
///     AuditTime::from_unix_ms(1_789_760_467_412).unwrap(),
///     "chat".parse().unwrap(),
///     AuditAction::Manifest,
///     Arguments::empty(),
///     AuditOutcome::Granted,
///     Claimed::none(),
/// );
/// let record = AuditRecord { latency_ms: Some(38), ..record };
/// ```
///
/// [`with_gate`]: AuditRecord::with_gate
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditRecord {
    at: AuditTime,
    family: FamilyName,
    sandbox: SandboxEvidence,
    grants_rev: Option<GrantsRev>,
    action: AuditAction,
    args: Arguments,
    outcome: AuditOutcome,
    latency_ms: Option<u64>,
    gated: Option<Gated>,
    claimed: Claimed,
    callers: Vec<FamilyName>,
}

fn text(value: &str) -> Value {
    Value::Text(value.to_owned())
}

fn optional<T>(value: Option<&T>, as_str: impl Fn(&T) -> &str) -> Value {
    value.map_or(Value::Null, |value| text(as_str(value)))
}

fn number(value: u64) -> Value {
    Value::Integer(Integer::from(value))
}

/// One line of JSON, as `json.dumps(record, ensure_ascii=False)` writes it,
/// and a newline.
fn line(record: Map) -> Vec<u8> {
    let style = Style {
        layout: Layout::Line,
        charset: Charset::Unicode,
        keys: KeyOrder::Kept,
    };
    let mut out = String::new();
    json::write(&Value::Map(record), style, &mut out);
    out.push('\n');

    out.into_bytes()
}

/// `value` with each string of more than [`AUDIT_TEXT_MAX_CHARS`] characters
/// cut, and a marker after the cut. A key is not cut.
fn truncated(value: &Value) -> Value {
    match value {
        Value::Text(long) if long.chars().count() > AUDIT_TEXT_MAX_CHARS => {
            let mut kept: String = long.chars().take(AUDIT_TEXT_MAX_CHARS).collect();
            kept.push_str(TRUNCATION_MARKER);

            Value::Text(kept)
        }
        Value::List(items) => Value::List(items.iter().map(truncated).collect()),
        Value::Map(entries) => Value::Map(entries.map_values(truncated)),
        _ => value.clone(),
    }
}

impl AuditRecord {
    /// The record of one decision for `family`, written at the time `at`.
    ///
    /// The record names no sandbox, no revision of a grant file, no latency,
    /// no gate and no caller before `family`. A `with_` method sets each one.
    #[must_use]
    pub fn new(
        at: AuditTime,
        family: FamilyName,
        action: AuditAction,
        args: Arguments,
        outcome: AuditOutcome,
        claimed: Claimed,
    ) -> Self {
        Self {
            at,
            family,
            sandbox: SandboxEvidence::Unknown,
            grants_rev: None,
            action,
            args,
            outcome,
            latency_ms: None,
            gated: None,
            claimed,
            callers: Vec::new(),
        }
    }

    /// The same record with this evidence for the sandbox of the caller.
    #[must_use]
    pub fn with_sandbox(mut self, sandbox: SandboxEvidence) -> Self {
        self.sandbox = sandbox;

        self
    }

    /// The same record with the revision of the grant file that made the
    /// decision.
    #[must_use]
    pub fn with_grants_rev(mut self, grants_rev: GrantsRev) -> Self {
        self.grants_rev = Some(grants_rev);

        self
    }

    /// The same record with the milliseconds of the execution. The wait for
    /// a gate is not a part of them.
    #[must_use]
    pub fn with_latency_ms(mut self, latency_ms: u64) -> Self {
        self.latency_ms = Some(latency_ms);

        self
    }

    /// The same record with the gate of `held` and the milliseconds of the
    /// wait for the operator at that gate.
    ///
    /// The gate and the wait are one fact. No method sets one of the two
    /// alone, so a record has a wait only with a gate. Each record of one
    /// gated call takes the gate from the same [`Held`], so the records name
    /// the same gate (contract 04 §6.4). The first record of a gated call has
    /// a wait of 0.
    ///
    /// ```
    /// use creche_contracts::grants::{AuditRecord, Held};
    ///
    /// fn with_the_gate_of(record: AuditRecord, call: &Held, waited_ms: u64) -> AuditRecord {
    ///     record.with_gate(call, waited_ms)
    /// }
    /// ```
    #[must_use]
    pub fn with_gate(mut self, held: &Held, waited_ms: u64) -> Self {
        self.gated = Some(Gated {
            gate: held.gate().clone(),
            waited_ms,
        });

        self
    }

    /// The same record with the families before this family in the
    /// delegation chain, the first caller first.
    #[must_use]
    pub fn with_callers(mut self, callers: Vec<FamilyName>) -> Self {
        self.callers = callers;

        self
    }

    /// The time of the write.
    #[must_use]
    pub fn at(&self) -> AuditTime {
        self.at
    }

    /// The family that the bearer named.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The sandbox of the caller.
    #[must_use]
    pub fn sandbox(&self) -> &SandboxEvidence {
        &self.sandbox
    }

    /// The revision of the grant file that made the decision.
    #[must_use]
    pub fn grants_rev(&self) -> Option<&GrantsRev> {
        self.grants_rev.as_ref()
    }

    /// What the caller asked for.
    #[must_use]
    pub fn action(&self) -> &AuditAction {
        &self.action
    }

    /// The full arguments. `to_line` cuts each string of more than
    /// [`AUDIT_TEXT_MAX_CHARS`] characters.
    #[must_use]
    pub fn args(&self) -> &Arguments {
        &self.args
    }

    /// The decision and its reason.
    #[must_use]
    pub fn outcome(&self) -> AuditOutcome {
        self.outcome
    }

    /// The milliseconds of the execution, with no wait for a gate. `None`
    /// when nothing ran.
    #[must_use]
    pub fn latency_ms(&self) -> Option<u64> {
        self.latency_ms
    }

    /// The milliseconds of the wait for the operator. 0 with no gate.
    #[must_use]
    pub fn waited_ms(&self) -> u64 {
        self.gated.as_ref().map_or(0, |gated| gated.waited_ms)
    }

    /// The gate of the call, when the call has one.
    #[must_use]
    pub fn gate(&self) -> Option<&GateId> {
        self.gated.as_ref().map(|gated| &gated.gate)
    }

    /// What the caller says about itself.
    #[must_use]
    pub fn claimed(&self) -> &Claimed {
        &self.claimed
    }

    /// The families before this family in the delegation chain, the first
    /// caller first (contract 04 §6.3). The chaperone builds the chain from
    /// the delegation ids that it minted itself. Empty for a call with no
    /// known delegation id.
    #[must_use]
    pub fn callers(&self) -> &[FamilyName] {
        &self.callers
    }

    /// The chain of the record: each caller, then this family.
    pub fn chain(&self) -> impl Iterator<Item = &FamilyName> {
        self.callers.iter().chain(std::iter::once(&self.family))
    }

    /// The bytes of the one line that the chaperone appends to the file
    /// [`AuditTime::file_name`], with its newline.
    #[must_use]
    pub fn to_line(&self) -> Vec<u8> {
        let (sandbox, trusted) = match &self.sandbox {
            SandboxEvidence::Unknown => (None, false),
            SandboxEvidence::Trusted(sandbox) => (Some(sandbox), true),
        };
        let claimed = &self.claimed;
        let mut claims = Map::new();
        claims.insert(
            "session_id",
            optional(claimed.session_id(), SessionId::as_str),
        );
        claims.insert("turn_id", optional(claimed.turn_id(), Ulid::as_str));
        claims.insert(
            "delegation_id",
            optional(claimed.delegation_id(), Ulid::as_str),
        );
        let chain = self.chain().map(|family| text(family.as_str()));

        let mut record = Map::new();
        record.insert("ts", text(&self.at.instant.to_rfc3339_millis()));
        record.insert("family", text(self.family.as_str()));
        record.insert("sandbox_id", optional(sandbox, SandboxName::as_str));
        record.insert("sandbox_id_trusted", Value::Bool(trusted));
        record.insert(
            "grants_rev",
            optional(self.grants_rev.as_ref(), GrantsRev::as_str),
        );
        record.insert("tool", text(self.action.as_str()));
        record.insert("args", Value::Map(self.args.as_map().map_values(truncated)));
        record.insert("decision", text(self.outcome.decision().as_str()));
        record.insert("reason", text(self.outcome.reason()));
        record.insert("latency_ms", self.latency_ms.map_or(Value::Null, number));
        record.insert("waited_ms", number(self.waited_ms()));
        record.insert("gate", optional(self.gate(), GateId::as_str));
        record.insert("claimed", Value::Map(claims));
        record.insert("chain", Value::List(chain.collect()));

        line(record)
    }
}

// --- the record of a request that names no family ---

/// What stands for the arguments in the record of a request that names no
/// family: their size and their digest, and never their content.
///
/// ```
/// use creche_contracts::grants::ArgsDigest;
/// use creche_contracts::ids::Sha256Hex;
///
/// let sha256: Sha256Hex =
///     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a".parse().unwrap();
/// let digest = ArgsDigest::new(2, sha256.clone());
/// assert_eq!(digest.bytes(), 2);
/// assert_eq!(digest.sha256(), &sha256);
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::ArgsDigest;
/// use creche_contracts::ids::Sha256Hex;
///
/// let sha256: Sha256Hex =
///     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a".parse().unwrap();
/// let digest = ArgsDigest { bytes: 2, sha256 };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArgsDigest {
    bytes: u64,
    sha256: Sha256Hex,
}

impl ArgsDigest {
    /// The size and the digest of the arguments of one request: the count of
    /// bytes of [`Arguments::digest_input`] and the SHA-256 of those bytes.
    #[must_use]
    pub fn new(bytes: u64, sha256: Sha256Hex) -> Self {
        Self { bytes, sha256 }
    }

    /// The count of bytes of [`Arguments::digest_input`].
    #[must_use]
    pub fn bytes(&self) -> u64 {
        self.bytes
    }

    /// The SHA-256 of [`Arguments::digest_input`].
    #[must_use]
    pub fn sha256(&self) -> &Sha256Hex {
        &self.sha256
    }
}

/// Why the chaperone refused a request with a bearer that names no family
/// (contract 04 §5 row 1).
///
/// The type is for the writer of a record. The set is closed for one release
/// of the chaperone.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Unidentified {
    /// The chaperone answered `unknown_token`.
    UnknownToken,
    /// The bucket for such requests was empty. The chaperone answered
    /// `rate_limited`.
    RateLimited,
}

/// What one request that names no family was.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UnidentifiedRequest {
    /// A request with no bearer, or with a bearer that names no family.
    NoFamily {
        /// What the caller asked for.
        action: AuditAction,
        /// Which of the two answers the chaperone gave.
        refusal: Unidentified,
        /// The size and the digest of the arguments.
        args: ArgsDigest,
    },
    /// A body of more than [`BODY_MAX_BYTES`] bytes. The chaperone refused it
    /// before it read a bearer or a tool.
    Oversized {
        /// The count of bytes that came before the chaperone stopped.
        seen_bytes: u64,
    },
}

// CONTRACT-QUESTION: contract 04 does not describe the log of a request that
// names no family. §6 says that each decision is recorded, and each record
// of §6.1 names a family. The Python chaperone writes such a request to a
// second log with the fields below. Its `ts` has the offset `+00:00`, and
// the `ts` of §6.1 ends with `Z`. This type writes what the Python chaperone
// writes. A change to one form of time costs each reader of the second log.
/// The record of one request that names no family. It goes to its own log,
/// and not to the audit of contract 04 §6: each record there names a family.
///
/// The record holds no argument of the caller. A caller with no token must
/// not put its bytes into a file that the host keeps.
///
/// ```
/// use creche_contracts::grants::{AuditTime, UnidentifiedRecord, UnidentifiedRequest};
///
/// let at = AuditTime::from_unix_ms(0)?;
/// let request = UnidentifiedRequest::Oversized { seen_bytes: 262_145 };
/// let record = UnidentifiedRecord::new(at, request.clone());
/// assert_eq!(record.at(), at);
/// assert_eq!(record.request(), &request);
/// # Ok::<(), creche_contracts::grants::AuditTimeError>(())
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{AuditTime, UnidentifiedRecord, UnidentifiedRequest};
///
/// let at = AuditTime::from_unix_ms(0).unwrap();
/// let request = UnidentifiedRequest::Oversized { seen_bytes: 262_145 };
/// let record = UnidentifiedRecord { at, request };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnidentifiedRecord {
    at: AuditTime,
    request: UnidentifiedRequest,
}

impl UnidentifiedRecord {
    /// The record of `request`, written at the time `at`.
    #[must_use]
    pub fn new(at: AuditTime, request: UnidentifiedRequest) -> Self {
        Self { at, request }
    }

    /// The time of the write.
    #[must_use]
    pub fn at(&self) -> AuditTime {
        self.at
    }

    /// What the request was.
    #[must_use]
    pub fn request(&self) -> &UnidentifiedRequest {
        &self.request
    }

    /// The bytes of the one line that the chaperone appends to the file
    /// [`AuditTime::file_name`] of the other log, with its newline.
    #[must_use]
    pub fn to_line(&self) -> Vec<u8> {
        // This log writes the time with an offset, and the audit record v2
        // writes it with `Z`. The Python chaperone does the same.
        let mut record = Map::new();
        record.insert("ts", text(&self.at.instant.to_rfc3339_millis_plus_00_00()));
        match &self.request {
            UnidentifiedRequest::NoFamily {
                action,
                refusal,
                args,
            } => {
                let reason = match refusal {
                    Unidentified::UnknownToken => "unknown_token",
                    Unidentified::RateLimited => "rate_limited",
                };
                record.insert("tool", text(action.as_str()));
                record.insert("decision", text("deny"));
                record.insert("reason", text(reason));
                record.insert("detail", Value::Null);
                record.insert("args_bytes", number(args.bytes));
                record.insert("args_sha256", text(args.sha256.as_str()));
            }
            UnidentifiedRequest::Oversized { seen_bytes } => {
                let detail = format!("body exceeds {BODY_MAX_BYTES} bytes");
                record.insert("tool", text(OVERSIZED_ACTION));
                record.insert("decision", text("deny"));
                record.insert("reason", text(REASON_BODY_TOO_LARGE));
                record.insert("detail", text(&detail));
                record.insert("args_bytes", number(*seen_bytes));
            }
        }

        line(record)
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::super::body::CallBody;
    use super::super::decision::{Executor, Reason};
    use super::*;

    const GATE: &str = "0123456789abcdef";
    const OTHER_GATE: &str = "fedcba9876543210";

    /// The last millisecond of the year 9999, in milliseconds after the
    /// epoch. The tests write the value as a number. A range of the one time
    /// type that moves then fails a test.
    const UNIX_MS_MAX: u64 = 253_402_300_799_999;

    fn at(unix_ms: u64) -> AuditTime {
        AuditTime::from_unix_ms(unix_ms).unwrap()
    }

    /// The record of one allowed call of `embed` with these arguments.
    fn record_of(args: Arguments) -> AuditRecord {
        AuditRecord::new(
            at(1_789_760_467_412),
            "chat".parse().unwrap(),
            AuditAction::Call("embed".parse().unwrap()),
            args,
            AuditOutcome::Granted,
            Claimed::none(),
        )
        .with_grants_rev("reg-9f21c4".parse().unwrap())
        .with_latency_ms(38)
    }

    fn record() -> AuditRecord {
        record_of(Arguments::empty())
    }

    /// A call of `chat` that waits for the operator at the gate `gate`.
    fn held_at(gate: &str) -> Held {
        Held::in_test(
            "chat".parse().unwrap(),
            "reg-9f21c4".parse().unwrap(),
            Executor::Delegate,
            Arguments::empty(),
            gate.parse().unwrap(),
        )
    }

    /// A call of `chat` that waits for the operator at [`GATE`].
    fn held() -> Held {
        held_at(GATE)
    }

    fn line_of(record: &AuditRecord) -> String {
        String::from_utf8(record.to_line()).unwrap()
    }

    #[test]
    fn a_time_writes_its_day_and_its_milliseconds() {
        for (unix_ms, text, file) in [
            (0, "1970-01-01T00:00:00.000", "1970-01-01.jsonl"),
            (1, "1970-01-01T00:00:00.001", "1970-01-01.jsonl"),
            (86_399_999, "1970-01-01T23:59:59.999", "1970-01-01.jsonl"),
            (86_400_000, "1970-01-02T00:00:00.000", "1970-01-02.jsonl"),
            (
                951_782_400_000,
                "2000-02-29T00:00:00.000",
                "2000-02-29.jsonl",
            ),
            (
                951_868_800_000,
                "2000-03-01T00:00:00.000",
                "2000-03-01.jsonl",
            ),
            (
                1_709_210_096_789,
                "2024-02-29T12:34:56.789",
                "2024-02-29.jsonl",
            ),
            (
                1_740_787_199_999,
                "2025-02-28T23:59:59.999",
                "2025-02-28.jsonl",
            ),
            (
                1_740_787_200_000,
                "2025-03-01T00:00:00.000",
                "2025-03-01.jsonl",
            ),
            (
                1_789_760_467_412,
                "2026-09-18T19:41:07.412",
                "2026-09-18.jsonl",
            ),
            (
                1_798_761_599_999,
                "2026-12-31T23:59:59.999",
                "2026-12-31.jsonl",
            ),
            (
                4_102_444_800_000,
                "2100-01-01T00:00:00.000",
                "2100-01-01.jsonl",
            ),
            (
                4_107_542_400_000,
                "2100-03-01T00:00:00.000",
                "2100-03-01.jsonl",
            ),
            (UNIX_MS_MAX, "9999-12-31T23:59:59.999", "9999-12-31.jsonl"),
        ] {
            let instant = at(unix_ms).instant;

            assert_eq!(instant.to_rfc3339_millis(), format!("{text}Z"));
            assert_eq!(
                instant.to_rfc3339_millis_plus_00_00(),
                format!("{text}+00:00")
            );
            assert_eq!(at(unix_ms).file_name(), file);
            assert_eq!(at(unix_ms).unix_ms(), unix_ms);
        }
    }

    #[test]
    fn a_time_is_from_1970_to_9999() {
        let epoch = UNIX_EPOCH;
        let cut = epoch + Duration::from_micros(1_789_760_467_412_999);
        let late = epoch + Duration::from_millis(UNIX_MS_MAX + 1);

        assert_eq!(AuditTime::try_from(epoch), Ok(at(0)));
        assert_eq!(AuditTime::try_from(cut), Ok(at(1_789_760_467_412)));
        assert_eq!(
            AuditTime::try_from(epoch - Duration::from_nanos(1)),
            Err(AuditTimeError::BeforeEpoch)
        );
        assert_eq!(
            AuditTime::try_from(late),
            Err(AuditTimeError::AfterYear9999)
        );
        for after in [UNIX_MS_MAX + 1, i64::MAX.unsigned_abs(), u64::MAX] {
            assert_eq!(
                AuditTime::from_unix_ms(after),
                Err(AuditTimeError::AfterYear9999),
                "{after}"
            );
        }

        assert_eq!(
            at(UNIX_MS_MAX).instant.to_rfc3339_millis(),
            Timestamp::MAX.to_rfc3339_millis()
        );
        assert_eq!(
            AuditTimeError::BeforeEpoch.to_string(),
            "the time of a record is in 1970 or later"
        );
        assert_eq!(
            AuditTimeError::AfterYear9999.to_string(),
            "the time of a record is in 9999 or before"
        );
    }

    #[test]
    fn a_header_of_the_wrong_shape_reads_as_absent() {
        let session = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK";
        let ulid = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";
        let lower = ulid.to_lowercase();
        let raw = RawClaimed::new()
            .with_session_id(session)
            .with_turn_id(ulid)
            .with_delegation_id(ulid);
        let (claimed, dropped) = Claimed::read(&raw);

        assert_eq!(claimed.session_id().unwrap().as_str(), session);
        assert_eq!(claimed.turn_id().unwrap().as_str(), ulid);
        assert_eq!(claimed.delegation_id().unwrap().as_str(), ulid);
        assert_eq!(dropped, []);

        let raw = RawClaimed::new()
            .with_session_id("../other\u{e9}")
            .with_turn_id(&lower)
            .with_delegation_id("");
        let (claimed, dropped) = Claimed::read(&raw);
        let dropped: Vec<(ClaimedHeader, usize)> = dropped
            .iter()
            .map(|dropped| (dropped.header(), dropped.chars()))
            .collect();

        assert_eq!(claimed, Claimed::none());
        assert_eq!(
            dropped,
            [
                (ClaimedHeader::SessionId, 9),
                (ClaimedHeader::TurnId, 26),
                (ClaimedHeader::DelegationId, 0),
            ]
        );
        assert_eq!(
            Claimed::read(&RawClaimed::default()),
            (Claimed::none(), vec![])
        );
    }

    #[test]
    fn the_raw_headers_give_each_value_back() {
        let none = RawClaimed::new();
        let each = none
            .with_session_id("tui-1")
            .with_turn_id("a turn")
            .with_delegation_id("a delegation");

        assert_eq!(none, RawClaimed::default());
        assert_eq!(
            (none.session_id(), none.turn_id(), none.delegation_id()),
            (None, None, None)
        );
        assert_eq!(each.session_id(), Some("tui-1"));
        assert_eq!(each.turn_id(), Some("a turn"));
        assert_eq!(each.delegation_id(), Some("a delegation"));
    }

    #[test]
    fn a_header_has_its_name_in_lower_case() {
        assert_eq!(ClaimedHeader::SessionId.name(), "x-session-id");
        assert_eq!(ClaimedHeader::TurnId.name(), "x-turn-id");
        assert_eq!(ClaimedHeader::DelegationId.name(), "x-delegation-id");
    }

    #[test]
    fn a_record_writes_each_key_in_the_order_of_the_contract() {
        assert_eq!(
            line_of(&record()),
            "{\"ts\": \"2026-09-18T19:41:07.412Z\", \"family\": \"chat\", \"sandbox_id\": null, \
             \"sandbox_id_trusted\": false, \"grants_rev\": \"reg-9f21c4\", \"tool\": \"embed\", \
             \"args\": {}, \"decision\": \"allow\", \"reason\": \"granted\", \"latency_ms\": 38, \
             \"waited_ms\": 0, \"gate\": null, \"claimed\": {\"session_id\": null, \"turn_id\": \
             null, \"delegation_id\": null}, \"chain\": [\"chat\"]}\n"
        );
    }

    #[test]
    fn a_record_writes_the_sandbox_and_the_chain() {
        let sandbox: SandboxName = "chat-s3".parse().unwrap();
        let trusted = record().with_sandbox(SandboxEvidence::Trusted(sandbox));
        let chained = AuditRecord::new(
            at(1_789_760_467_412),
            "vault-oracle".parse().unwrap(),
            AuditAction::Manifest,
            Arguments::empty(),
            AuditOutcome::Denied(Reason::RateLimited),
            Claimed::none(),
        )
        .with_grants_rev("reg-9f21c4".parse().unwrap())
        .with_callers(vec!["chat".parse().unwrap()]);

        assert!(
            line_of(&trusted).contains("\"sandbox_id\": \"chat-s3\", \"sandbox_id_trusted\": true")
        );
        assert!(line_of(&chained).contains("\"tool\": \"$manifest\""));
        assert!(line_of(&chained).contains(
            "\"decision\": \"deny\", \"reason\": \"rate_limited\", \"latency_ms\": null"
        ));
        assert!(line_of(&chained).ends_with("\"chain\": [\"chat\", \"vault-oracle\"]}\n"));
        assert_eq!(chained.chain().count(), 2);
    }

    /// The input of the vector `sandbox-claimed`, which left the surface
    /// `chaperone.audit_line`: the sandbox `chat-s3` with
    /// `sandbox_id_trusted: false`. The Python writer takes that pair and
    /// writes it. No value of `SandboxEvidence` holds it.
    #[test]
    fn a_record_names_a_sandbox_only_with_proof() {
        let sandbox: SandboxName = "chat-s3".parse().unwrap();
        let no_proof = "\"sandbox_id\": \"chat-s3\", \"sandbox_id_trusted\": false";

        for evidence in [SandboxEvidence::Unknown, SandboxEvidence::Trusted(sandbox)] {
            // The match has no wildcard arm, so a new variant does not build
            // here before it has a row.
            let written = match &evidence {
                SandboxEvidence::Unknown => "\"sandbox_id\": null, \"sandbox_id_trusted\": false",
                SandboxEvidence::Trusted(_) => {
                    "\"sandbox_id\": \"chat-s3\", \"sandbox_id_trusted\": true"
                }
            };
            let line = line_of(&record().with_sandbox(evidence));

            assert!(line.contains(written), "{line}");
            assert!(!line.contains(no_proof), "{line}");
        }
    }

    #[test]
    fn a_record_has_a_wait_only_with_a_gate() {
        let held = held();
        let no_gate = record();
        let first = AuditRecord::new(
            at(1_789_760_467_412),
            "chat".parse().unwrap(),
            AuditAction::Call("embed".parse().unwrap()),
            Arguments::empty(),
            AuditOutcome::Pending,
            Claimed::none(),
        )
        .with_gate(&held, 0);
        let second = record().with_gate(&held, 41_250);

        assert_eq!((no_gate.gate(), no_gate.waited_ms()), (None, 0));
        assert!(line_of(&no_gate).contains("\"waited_ms\": 0, \"gate\": null, "));

        // The two records of one gated call take the gate from one `Held`.
        assert_eq!(first.gate(), Some(held.gate()));
        assert_eq!(second.gate(), first.gate());
        assert_eq!((first.waited_ms(), second.waited_ms()), (0, 41_250));
        assert!(line_of(&first).contains(&format!("\"waited_ms\": 0, \"gate\": \"{GATE}\", ")));
        assert!(
            line_of(&second).contains(&format!("\"waited_ms\": 41250, \"gate\": \"{GATE}\", "))
        );

        // A second call of the method replaces the gate and the wait together.
        let other = held_at(OTHER_GATE);
        let again = second.clone().with_gate(&other, 7);

        assert_ne!(other.gate(), held.gate());
        assert_eq!((again.gate(), again.waited_ms()), (Some(other.gate()), 7));
        assert!(
            line_of(&again).contains(&format!("\"waited_ms\": 7, \"gate\": \"{OTHER_GATE}\", "))
        );
    }

    #[test]
    fn a_record_gives_each_field_back() {
        let sandbox: SandboxName = "chat-s3".parse().unwrap();
        let caller: FamilyName = "chat".parse().unwrap();
        let session = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK";
        let (claimed, _) = Claimed::read(&RawClaimed::new().with_session_id(session));
        let bare = AuditRecord::new(
            at(7),
            "vault-oracle".parse().unwrap(),
            AuditAction::Manifest,
            Arguments::empty(),
            AuditOutcome::Denied(Reason::RateLimited),
            claimed.clone(),
        );
        let full = bare
            .clone()
            .with_sandbox(SandboxEvidence::Trusted(sandbox.clone()))
            .with_grants_rev("reg-9f21c4".parse().unwrap())
            .with_latency_ms(38)
            .with_callers(vec![caller.clone()]);

        assert_eq!(bare.at(), at(7));
        assert_eq!(bare.family().as_str(), "vault-oracle");
        assert_eq!(bare.sandbox(), &SandboxEvidence::Unknown);
        assert_eq!(bare.grants_rev(), None);
        assert_eq!(bare.action(), &AuditAction::Manifest);
        assert!(bare.args().as_map().is_empty());
        assert_eq!(bare.outcome(), AuditOutcome::Denied(Reason::RateLimited));
        assert_eq!(bare.latency_ms(), None);
        assert_eq!((bare.gate(), bare.waited_ms()), (None, 0));
        assert_eq!(bare.claimed(), &claimed);
        assert!(bare.callers().is_empty());

        assert_eq!(full.sandbox(), &SandboxEvidence::Trusted(sandbox));
        assert_eq!(full.grants_rev().unwrap().as_str(), "reg-9f21c4");
        assert_eq!(full.latency_ms(), Some(38));
        assert_eq!(full.callers(), [caller]);
        assert_eq!(full.family(), bare.family());
    }

    #[test]
    fn a_record_cuts_a_long_string_and_no_key() {
        let at_cap = "a".repeat(AUDIT_TEXT_MAX_CHARS);
        let key = format!("{at_cap}k");
        let body = format!(
            "{{\"tool\":\"embed\",\"args\":{{\"at\":\"{at_cap}\",\"over\":[\"{at_cap}b\"],\"{key}\":1}}}}"
        );
        let args = CallBody::parse(body.as_bytes()).unwrap().into_parts().1;
        let line = line_of(&record_of(args));

        assert!(line.contains(&format!("\"at\": \"{at_cap}\", ")));
        assert!(line.contains(&format!("\"over\": [\"{at_cap}...<truncated>\"]")));
        assert!(line.contains(&format!("\"{key}\": 1")));
    }

    #[test]
    fn a_record_with_many_keys_keeps_each_key_in_its_place() {
        // One body of 256 KiB can hold this count of keys. A writer that
        // searches the map for each key takes seconds here.
        const KEYS: usize = 20_000;

        let cut = "a".repeat(AUDIT_TEXT_MAX_CHARS);
        let long = format!("{cut}b");
        let entries: Vec<String> = (0..KEYS).rev().map(|key| format!("\"k{key}\":1")).collect();
        let body = format!(
            "{{\"tool\":\"embed\",\"args\":{{{},\"last\":\"{long}\"}}}}",
            entries.join(",")
        );
        let args = CallBody::parse(body.as_bytes()).unwrap().into_parts().1;
        let line = line_of(&record_of(args));
        let written: Vec<String> = (0..KEYS)
            .rev()
            .map(|key| format!("\"k{key}\": 1"))
            .collect();

        assert!(body.len() <= BODY_MAX_BYTES);
        assert_eq!(line.matches("\": 1").count(), KEYS);
        assert!(line.contains(&format!(
            "\"args\": {{{}, \"last\": \"{cut}...<truncated>\"}}, \"decision\"",
            written.join(", ")
        )));
    }

    #[test]
    fn a_request_with_no_family_writes_no_argument() {
        let digest: Sha256Hex = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            .parse()
            .unwrap();
        let args = ArgsDigest::new(2, digest.clone());
        let no_family = UnidentifiedRequest::NoFamily {
            action: AuditAction::Manifest,
            refusal: Unidentified::RateLimited,
            args: args.clone(),
        };
        let too_large = UnidentifiedRequest::Oversized {
            seen_bytes: 262_145,
        };
        let refused = UnidentifiedRecord::new(at(1_789_760_467_412), no_family.clone());
        let oversized = UnidentifiedRecord::new(at(0), too_large.clone());

        assert_eq!((args.bytes(), args.sha256()), (2, &digest));
        assert_eq!(refused.at(), at(1_789_760_467_412));
        assert_eq!(refused.request(), &no_family);
        assert_eq!(oversized.at(), at(0));
        assert_eq!(oversized.request(), &too_large);

        assert_eq!(
            String::from_utf8(refused.to_line()).unwrap(),
            "{\"ts\": \"2026-09-18T19:41:07.412+00:00\", \"tool\": \"$manifest\", \"decision\": \
             \"deny\", \"reason\": \"rate_limited\", \"detail\": null, \"args_bytes\": 2, \
             \"args_sha256\": \"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a\"}\n"
        );
        assert_eq!(
            String::from_utf8(oversized.to_line()).unwrap(),
            "{\"ts\": \"1970-01-01T00:00:00.000+00:00\", \"tool\": \"$oversized\", \"decision\": \
             \"deny\", \"reason\": \"body_too_large\", \"detail\": \"body exceeds 262144 bytes\", \
             \"args_bytes\": 262145}\n"
        );
    }
}
