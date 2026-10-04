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

use super::body::{Arguments, BODY_MAX_BYTES, CallTool};
use super::decision::AuditOutcome;
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

/// The last millisecond of the year 9999, in milliseconds after the epoch. A
/// record writes its year with four digits.
const UNIX_MS_MAX: u64 = 253_402_300_799_999;

const MS_PER_SECOND: u64 = 1000;
const SECONDS_PER_MINUTE: u64 = 60;
const MINUTES_PER_HOUR: u64 = 60;
const HOURS_PER_DAY: u64 = 24;
const MS_PER_DAY: u64 = MS_PER_SECOND * SECONDS_PER_MINUTE * MINUTES_PER_HOUR * HOURS_PER_DAY;

/// The time of one record: a UTC time in whole milliseconds, from 1970 to the
/// end of the year 9999 (contract 04 §6.1).
///
/// ```
/// use creche_contracts::grants::AuditTime;
///
/// let time = AuditTime::from_unix_ms(1_789_760_467_412)?;
/// assert_eq!(time.unix_ms(), 1_789_760_467_412);
/// assert_eq!(time.file_name(), "2026-09-18.jsonl");
/// # Ok::<(), creche_contracts::grants::AuditTimeError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::AuditTime;
///
/// let time = AuditTime { unix_ms: u64::MAX };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AuditTime {
    unix_ms: u64,
}

/// The parts of a UTC time.
struct Civil {
    year: u64,
    month: u64,
    day: u64,
    hour: u64,
    minute: u64,
    second: u64,
    millisecond: u64,
}

impl AuditTime {
    /// The time that is `unix_ms` milliseconds after 1970-01-01T00:00:00Z.
    pub fn from_unix_ms(unix_ms: u64) -> Result<Self, AuditTimeError> {
        if unix_ms > UNIX_MS_MAX {
            return Err(AuditTimeError::AfterYear9999);
        }

        Ok(Self { unix_ms })
    }

    /// The milliseconds after 1970-01-01T00:00:00Z.
    #[must_use]
    pub fn unix_ms(&self) -> u64 {
        self.unix_ms
    }

    /// The name of the file that takes a record of this time: the UTC day,
    /// `YYYY-MM-DD.jsonl` (contract 04 §6).
    #[must_use]
    pub fn file_name(&self) -> String {
        let Civil {
            year, month, day, ..
        } = self.civil();

        format!("{year:04}-{month:02}-{day:02}.jsonl")
    }

    /// The date and the time of day. The algorithm is the `civil_from_days` of
    /// Howard Hinnant, for a day that is not before 1970.
    fn civil(&self) -> Civil {
        let ms_of_day = self.unix_ms % MS_PER_DAY;
        let seconds_of_day = ms_of_day / MS_PER_SECOND;
        let minutes_of_day = seconds_of_day / SECONDS_PER_MINUTE;

        // 719 468 days are between 0000-03-01 and 1970-01-01. An era is 400
        // years, which is 146 097 days. The year starts on March 1, so a leap
        // day is the last day of its year.
        let days = self.unix_ms / MS_PER_DAY + 719_468;
        let era = days / 146_097;
        let day_of_era = days % 146_097;
        let year_of_era =
            (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
        let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
        let shifted_month = (5 * day_of_year + 2) / 153;
        let month = if shifted_month < 10 {
            shifted_month + 3
        } else {
            shifted_month - 9
        };

        Civil {
            year: year_of_era + era * 400 + u64::from(month <= 2),
            month,
            day: day_of_year - (153 * shifted_month + 2) / 5 + 1,
            hour: minutes_of_day / MINUTES_PER_HOUR,
            minute: minutes_of_day % MINUTES_PER_HOUR,
            second: seconds_of_day % SECONDS_PER_MINUTE,
            millisecond: ms_of_day % MS_PER_SECOND,
        }
    }

    /// `YYYY-MM-DDTHH:MM:SS.mmm`: the part that the two records share.
    fn text(&self) -> String {
        let Civil {
            year,
            month,
            day,
            hour,
            minute,
            second,
            millisecond,
        } = self.civil();

        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millisecond:03}")
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
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RawClaimed<'a> {
    /// The value of `X-Session-Id`.
    pub session_id: Option<&'a str>,
    /// The value of `X-Turn-Id`.
    pub turn_id: Option<&'a str>,
    /// The value of `X-Delegation-Id`.
    pub delegation_id: Option<&'a str>,
}

/// One header whose value does not have the shape of its id. The chaperone
/// writes this to its log.
///
/// The type holds the count of characters and never the value: the value is
/// text that the caller chose (contract 04 §3.1 rule 4).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DroppedHeader {
    /// The header.
    pub header: ClaimedHeader,
    /// The count of characters of its value.
    pub chars: usize,
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
/// let (claimed, dropped) = Claimed::read(&RawClaimed {
///     session_id: Some("tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"),
///     turn_id: Some("not a ULID"),
///     delegation_id: None,
/// });
///
/// assert_eq!(claimed.session_id().map(|id| id.as_str()), Some("tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"));
/// assert_eq!(claimed.turn_id(), None);
/// assert_eq!(dropped.len(), 1);
/// assert_eq!(dropped[0].header, ClaimedHeader::TurnId);
/// assert_eq!(dropped[0].chars, 10);
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
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SandboxEvidence {
    /// The chaperone could not map the connection to a sandbox. The Python
    /// chaperone writes this value for each record.
    Unknown,
    // CONTRACT-QUESTION: contract 04 §3.2 says that a sandbox id with no proof
    // moves into `claimed`, and §6.2 gives `claimed` three keys and none for
    // a sandbox. §6.1 keeps `sandbox_id_trusted` for an id that is not
    // evidence. The Python chaperone writes `null` and `false` in each
    // record, and its writer also takes an id with `false`. This variant
    // writes what that writer writes: the id in `sandbox_id`, and
    // `sandbox_id_trusted: false`. The port of the chaperone must not build
    // this variant before the contract says where such an id goes. A change
    // costs this variant and the vector `sandbox-claimed`.
    /// The caller names this sandbox, and the chaperone has no proof.
    Claimed(SandboxName),
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

/// One audit record, version 2: one decision of the chaperone for one family
/// (contract 04 §6).
///
/// Each field has a valid type, and each field is public. The type holds no
/// rule between two fields. The contract has one such rule: the two records
/// of a gated call name the same gate, and the record of a call with no gate
/// names none (contract 04 §6.4). The writer of the port holds that rule. It
/// takes `gate` from the [`Held`](super::Held) of the call.
///
/// A record keeps the trusted fields apart from the claimed fields. The claimed
/// fields come only from a [`Claimed`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditRecord {
    /// The time of the write.
    pub at: AuditTime,
    /// The family that the bearer named.
    pub family: FamilyName,
    /// The sandbox of the caller.
    pub sandbox: SandboxEvidence,
    /// The revision of the grant file that made the decision.
    pub grants_rev: Option<GrantsRev>,
    /// What the caller asked for.
    pub action: AuditAction,
    /// The full arguments. `to_line` cuts each string of more than
    /// [`AUDIT_TEXT_MAX_CHARS`] characters.
    pub args: Arguments,
    /// The decision and its reason.
    pub outcome: AuditOutcome,
    /// The milliseconds of the execution, with no wait for a gate. `None`
    /// when nothing ran.
    pub latency_ms: Option<u64>,
    /// The milliseconds of the wait for the operator. 0 with no gate.
    pub waited_ms: u64,
    /// The gate of the call, when the call has one.
    pub gate: Option<GateId>,
    /// What the caller says about itself.
    pub claimed: Claimed,
    /// The families before this family in the delegation chain, the first
    /// caller first (contract 04 §6.3). The chaperone builds the chain from
    /// the delegation ids that it minted itself. Empty for a call with no
    /// known delegation id.
    pub callers: Vec<FamilyName>,
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
            SandboxEvidence::Claimed(sandbox) => (Some(sandbox), false),
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
        record.insert("ts", text(&format!("{}Z", self.at.text())));
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
        record.insert("waited_ms", number(self.waited_ms));
        record.insert("gate", optional(self.gate.as_ref(), GateId::as_str));
        record.insert("claimed", Value::Map(claims));
        record.insert("chain", Value::List(chain.collect()));

        line(record)
    }
}

// --- the record of a request that names no family ---

/// What stands for the arguments in the record of a request that names no
/// family: their size and their digest, and never their content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArgsDigest {
    /// The count of bytes of [`Arguments::digest_input`].
    pub bytes: u64,
    /// The SHA-256 of [`Arguments::digest_input`].
    pub sha256: Sha256Hex,
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
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnidentifiedRecord {
    /// The time of the write.
    pub at: AuditTime,
    /// What the request was.
    pub request: UnidentifiedRequest,
}

impl UnidentifiedRecord {
    /// The bytes of the one line that the chaperone appends to the file
    /// [`AuditTime::file_name`] of the other log, with its newline.
    #[must_use]
    pub fn to_line(&self) -> Vec<u8> {
        // This log writes the time with an offset, and the audit record v2
        // writes it with `Z`. The Python chaperone does the same.
        let mut record = Map::new();
        record.insert("ts", text(&format!("{}+00:00", self.at.text())));
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
    use super::super::decision::Reason;
    use super::*;

    fn at(unix_ms: u64) -> AuditTime {
        AuditTime::from_unix_ms(unix_ms).unwrap()
    }

    fn record() -> AuditRecord {
        AuditRecord {
            at: at(1_789_760_467_412),
            family: "chat".parse().unwrap(),
            sandbox: SandboxEvidence::Unknown,
            grants_rev: Some("reg-9f21c4".parse().unwrap()),
            action: AuditAction::Call("embed".parse().unwrap()),
            args: Arguments::empty(),
            outcome: AuditOutcome::Granted,
            latency_ms: Some(38),
            waited_ms: 0,
            gate: None,
            claimed: Claimed::none(),
            callers: Vec::new(),
        }
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
            assert_eq!(at(unix_ms).text(), text);
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
        assert_eq!(
            AuditTime::from_unix_ms(UNIX_MS_MAX + 1),
            Err(AuditTimeError::AfterYear9999)
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
        let (claimed, dropped) = Claimed::read(&RawClaimed {
            session_id: Some(session),
            turn_id: Some(ulid),
            delegation_id: Some(ulid),
        });

        assert_eq!(claimed.session_id().unwrap().as_str(), session);
        assert_eq!(claimed.turn_id().unwrap().as_str(), ulid);
        assert_eq!(claimed.delegation_id().unwrap().as_str(), ulid);
        assert_eq!(dropped, []);

        let (claimed, dropped) = Claimed::read(&RawClaimed {
            session_id: Some("../other\u{e9}"),
            turn_id: Some(&lower),
            delegation_id: Some(""),
        });

        assert_eq!(claimed, Claimed::none());
        assert_eq!(
            dropped,
            [
                DroppedHeader {
                    header: ClaimedHeader::SessionId,
                    chars: 9
                },
                DroppedHeader {
                    header: ClaimedHeader::TurnId,
                    chars: 26
                },
                DroppedHeader {
                    header: ClaimedHeader::DelegationId,
                    chars: 0
                },
            ]
        );
        assert_eq!(
            Claimed::read(&RawClaimed::default()),
            (Claimed::none(), vec![])
        );
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
        let trusted = AuditRecord {
            sandbox: SandboxEvidence::Trusted(sandbox.clone()),
            ..record()
        };
        let claimed = AuditRecord {
            sandbox: SandboxEvidence::Claimed(sandbox),
            family: "vault-oracle".parse().unwrap(),
            callers: vec!["chat".parse().unwrap()],
            action: AuditAction::Manifest,
            outcome: AuditOutcome::Denied(Reason::RateLimited),
            latency_ms: None,
            ..record()
        };

        assert!(
            line_of(&trusted).contains("\"sandbox_id\": \"chat-s3\", \"sandbox_id_trusted\": true")
        );
        assert!(
            line_of(&claimed)
                .contains("\"sandbox_id\": \"chat-s3\", \"sandbox_id_trusted\": false")
        );
        assert!(line_of(&claimed).contains("\"tool\": \"$manifest\""));
        assert!(line_of(&claimed).contains(
            "\"decision\": \"deny\", \"reason\": \"rate_limited\", \"latency_ms\": null"
        ));
        assert!(line_of(&claimed).ends_with("\"chain\": [\"chat\", \"vault-oracle\"]}\n"));
        assert_eq!(claimed.chain().count(), 2);
    }

    #[test]
    fn a_record_cuts_a_long_string_and_no_key() {
        let at_cap = "a".repeat(AUDIT_TEXT_MAX_CHARS);
        let key = format!("{at_cap}k");
        let body = format!(
            "{{\"tool\":\"embed\",\"args\":{{\"at\":\"{at_cap}\",\"over\":[\"{at_cap}b\"],\"{key}\":1}}}}"
        );
        let args = CallBody::parse(body.as_bytes()).unwrap().into_parts().1;
        let line = line_of(&AuditRecord { args, ..record() });

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
        let line = line_of(&AuditRecord { args, ..record() });
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
        let refused = UnidentifiedRecord {
            at: at(1_789_760_467_412),
            request: UnidentifiedRequest::NoFamily {
                action: AuditAction::Manifest,
                refusal: Unidentified::RateLimited,
                args: ArgsDigest {
                    bytes: 2,
                    sha256: digest,
                },
            },
        };
        let oversized = UnidentifiedRecord {
            at: at(0),
            request: UnidentifiedRequest::Oversized {
                seen_bytes: 262_145,
            },
        };

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
