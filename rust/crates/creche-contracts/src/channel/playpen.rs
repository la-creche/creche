//! What the playpen writes to the host (contract 03 §5).
//!
//! [`PlaypenMessage`] is one message of that direction, as the contract
//! permits it. The host reads the same line as a
//! [`PlaypenLine`](super::claim::PlaypenLine) and trusts no field of it.
//!
//! The playpen of today is TypeScript, and no vector file holds its output.
//! The tests here read each line back with the parser of the host.

use std::error::Error;
use std::fmt;
use std::num::NonZeroU64;

use super::claim::{Event, EventError, MAX_ENTRIES_PER_READ, TurnAddress};
use super::frame::{EncodeError, MAX_LINE_BYTES, framed};
use super::host::{EntryId, Nonce, ProcessCap, ProtocolVersion};
use super::json::{ObjectWriter, array_text, flag_text, float_text};
use super::vocabulary::{
    Cap, ExitReason, FailReason, FatalReason, LogLevel, OpenReason, PlaypenType, Word,
};
use crate::ids::{SandboxName, SessionId, Ulid};

/// The largest count of bytes of one free text of a line: a `message`
/// (contract 03 §8).
pub const MAX_LOG_BYTES: usize = 4096;

/// The largest count of bytes of the text of one entry (contract 03 §8).
pub const MAX_ENTRY_BYTES: usize = 65_536;

/// The largest count of bytes of one label: a version, a role or the name of
/// a signal.
pub const MAX_LABEL_BYTES: usize = 200;

/// The `turn_seq` of a message that belongs to no turn (contract 03 §5.1).
const NO_TURN_SEQ: &str = "0";

/// The start of `text` that has `max` bytes at most and ends between two
/// characters.
fn cut(text: &str, max: usize) -> &str {
    let mut end = text.len().min(max);
    while !text.is_char_boundary(end) {
        end -= 1;
    }

    text.get(..end).unwrap_or_default()
}

/// Makes the type of one text that the playpen cuts at a count of bytes.
macro_rules! cut_text {
    ($(#[$attribute:meta])* $name:ident, $max:expr) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);

        impl $name {
            /// The start of `text` that fits the limit. The cut is between
            /// two characters, so the result holds no half of a character.
            #[must_use]
            pub fn cut(text: &str) -> Self {
                Self(cut(text, $max).to_owned())
            }

            /// The text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }
    };
}

cut_text! {
    /// The free text of one line: at most [`MAX_LOG_BYTES`] bytes (contract
    /// 03 §5.5, §8).
    ///
    /// ```
    /// use creche_contracts::channel::playpen::LogText;
    ///
    /// assert_eq!(LogText::cut("exit 137").as_str(), "exit 137");
    /// assert_eq!(LogText::cut(&"m".repeat(5000)).as_str().len(), 4096);
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::playpen::LogText;
    ///
    /// let text = LogText("m".repeat(5000));
    /// ```
    LogText,
    MAX_LOG_BYTES
}

cut_text! {
    /// The text of one pi entry: at most [`MAX_ENTRY_BYTES`] bytes (contract
    /// 03 §5.8, §8).
    ///
    /// ```
    /// use creche_contracts::channel::playpen::EntryText;
    ///
    /// assert_eq!(EntryText::cut("Yes.").as_str(), "Yes.");
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::playpen::EntryText;
    ///
    /// let text = EntryText("a".repeat(70_000));
    /// ```
    EntryText,
    MAX_ENTRY_BYTES
}

cut_text! {
    /// A short label: a version, a role or the name of a signal. At most
    /// [`MAX_LABEL_BYTES`] bytes.
    ///
    /// ```
    /// use creche_contracts::channel::playpen::Label;
    ///
    /// assert_eq!(Label::cut("agent-supervisor/0.1.0").as_str(), "agent-supervisor/0.1.0");
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::playpen::Label;
    ///
    /// let label = Label("a".repeat(300));
    /// ```
    Label,
    MAX_LABEL_BYTES
}

/// A cost in US dollars: a finite number that is 0 or more (contract 03
/// §5.2).
///
/// ```
/// use creche_contracts::channel::playpen::Usd;
///
/// assert_eq!(Usd::new(0.25).map(Usd::get), Ok(0.25));
/// assert!(Usd::new(f64::NAN).is_err());
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::playpen::Usd;
///
/// let cost = Usd(-1.0);
/// ```
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Usd(f64);

impl Usd {
    /// No cost.
    pub const ZERO: Self = Self(0.0);

    /// The cost `value`.
    ///
    /// # Errors
    ///
    /// [`UsdError`] for a value below zero and for a value that is not
    /// finite.
    pub fn new(value: f64) -> Result<Self, UsdError> {
        if value.is_finite() && value >= 0.0 {
            return Ok(Self(value));
        }

        Err(UsdError)
    }

    /// The cost.
    #[must_use]
    pub fn get(self) -> f64 {
        self.0
    }
}

/// A cost is a finite number that is 0 or more.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsdError;

impl fmt::Display for UsdError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a cost is a finite number that is 0 or more")
    }
}

impl Error for UsdError {}

/// The usage of one turn, in the field names of the host (contract 03
/// §5.2).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TurnUsage {
    /// The input tokens.
    pub input: u64,
    /// The output tokens.
    pub output: u64,
    /// The tokens that the model read from its cache.
    pub cache_read: u64,
    /// The tokens that the model wrote to its cache.
    pub cache_write: u64,
    /// The cost.
    pub cost_usd: Usd,
}

impl TurnUsage {
    fn encoded(&self) -> String {
        ObjectWriter::new()
            .raw("input", &self.input.to_string())
            .raw("output", &self.output.to_string())
            .raw("cache_read", &self.cache_read.to_string())
            .raw("cache_write", &self.cache_write.to_string())
            .raw("cost_usd", &float_text(self.cost_usd.get()))
            .finish()
    }
}

/// `ready`: the first line of the channel (contract 03 §3).
///
/// ```
/// use creche_contracts::channel::playpen::{Label, PlaypenMessage, Ready, Versions};
/// use creche_contracts::channel::host::ProcessCap;
///
/// let versions = Versions {
///     supervisor: Label::cut("agent-supervisor/0.1.0"),
///     pi: Label::cut("0.99.1"),
///     node: Label::cut("24.1.0"),
/// };
/// let ready = Ready::new("chat-s3".parse().unwrap(), versions, ProcessCap::new(8), 0, Vec::new());
/// assert!(PlaypenMessage::Ready(ready).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{Label, PlaypenMessage, Ready, Versions};
/// use creche_contracts::channel::host::ProcessCap;
///
/// let versions = Versions {
///     supervisor: Label::cut("agent-supervisor/0.1.0"),
///     pi: Label::cut("0.99.1"),
///     node: Label::cut("24.1.0"),
/// };
/// let ready = Ready::new("chat-s3".parse().unwrap(), versions, ProcessCap::new(8), 0, Vec::new());
/// let ready = Ready { pi: Label::cut("0.99.2"), ..ready };
/// let message: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ready {
    protocol: ProtocolVersion,
    supervisor: Label,
    pi: Label,
    node: Label,
    sandbox: SandboxName,
    max_resident_processes: ProcessCap,
    foreign_pi_processes: u64,
    caps: Vec<Cap>,
}

/// The three versions that `ready` reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Versions {
    /// The name and the version of the playpen.
    pub supervisor: Label,
    /// The pi version inside the image.
    pub pi: Label,
    /// The Node version inside the image.
    pub node: Label,
}

impl Ready {
    /// The `ready` of the playpen of `sandbox`, for the protocol version
    /// of this code.
    #[must_use]
    pub fn new(
        sandbox: SandboxName,
        versions: Versions,
        max_resident_processes: ProcessCap,
        foreign_pi_processes: u64,
        caps: Vec<Cap>,
    ) -> Self {
        Self {
            protocol: ProtocolVersion::CURRENT,
            supervisor: versions.supervisor,
            pi: versions.pi,
            node: versions.node,
            sandbox,
            max_resident_processes,
            foreign_pi_processes,
            caps,
        }
    }

    fn body(&self) -> String {
        writer(PlaypenType::Ready)
            .text("protocol", &self.protocol.to_string())
            .text("supervisor", self.supervisor.as_str())
            .text("pi", self.pi.as_str())
            .text("node", self.node.as_str())
            .text("sandbox", self.sandbox.as_str())
            .raw(
                "max_resident_processes",
                &self.max_resident_processes.to_string(),
            )
            .raw(
                "foreign_pi_processes",
                &self.foreign_pi_processes.to_string(),
            )
            .raw(
                "caps",
                &array_text(self.caps.iter().map(|cap| cap.as_str())),
            )
            .finish()
    }
}

/// `session_opened`: the answer to `open_session` (contract 03 §5.6).
///
/// `resident` is `true` exactly when the reason is absent, so the type holds
/// one field for the two.
///
/// ```
/// use creche_contracts::channel::playpen::{LogText, PlaypenMessage, SessionOpened};
///
/// let opened = SessionOpened::resident("tui-1".parse().unwrap(), LogText::cut(""));
/// assert!(PlaypenMessage::SessionOpened(opened).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{LogText, PlaypenMessage, SessionOpened};
///
/// let opened = SessionOpened::resident("tui-1".parse().unwrap(), LogText::cut(""));
/// let opened = SessionOpened { refused: None, ..opened };
/// let message: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionOpened {
    session: SessionId,
    refused: Option<OpenReason>,
    message: LogText,
}

impl SessionOpened {
    /// A pi process is now resident for `session`.
    #[must_use]
    pub fn resident(session: SessionId, message: LogText) -> Self {
        Self {
            session,
            refused: None,
            message,
        }
    }

    /// No pi process is resident for `session`, for this reason.
    #[must_use]
    pub fn not_resident(session: SessionId, reason: OpenReason, message: LogText) -> Self {
        Self {
            session,
            refused: Some(reason),
            message,
        }
    }

    fn body(&self) -> String {
        writer(PlaypenType::SessionOpened)
            .text("session", self.session.as_str())
            .raw("resident", flag_text(self.refused.is_none()))
            .text_or_null("reason", self.refused.map(Word::as_str))
            .text("message", self.message.as_str())
            .finish()
    }
}

/// `event`: one wrapped pi event (contract 03 §5.1).
///
/// ```
/// use std::num::NonZeroU64;
///
/// use creche_contracts::channel::claim::{Event, TurnAddress};
/// use creche_contracts::channel::playpen::{EventMessage, PlaypenMessage};
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// let seq = NonZeroU64::MIN;
/// let event = Event::from_json(r#"{"type":"agent_start"}"#).unwrap();
/// let message = PlaypenMessage::Event(EventMessage::new(address, seq, event).unwrap());
/// assert!(message.encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use std::num::NonZeroU64;
///
/// use creche_contracts::channel::claim::{Event, TurnAddress};
/// use creche_contracts::channel::playpen::{EventMessage, PlaypenMessage};
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// let seq = NonZeroU64::MIN;
/// let event = Event::from_json("{}").unwrap();
/// let message = EventMessage { address, turn_seq: seq, event };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct EventMessage {
    address: TurnAddress,
    turn_seq: NonZeroU64,
    event: Event,
}

impl EventMessage {
    /// The event at the place `turn_seq` of the turn at `address`.
    ///
    /// # Errors
    ///
    /// [`EventError::NotFinite`] for an event with a number that is not
    /// finite. The host keeps such an event when it reads a line, and a
    /// record cannot hold it (contract 03 §2 rule 2).
    pub fn new(
        address: TurnAddress,
        turn_seq: NonZeroU64,
        event: Event,
    ) -> Result<Self, EventError> {
        if !event.is_finite() {
            return Err(EventError::NotFinite);
        }

        Ok(Self {
            address,
            turn_seq,
            event,
        })
    }

    fn body(&self) -> String {
        turn_writer(PlaypenType::Event, &self.address, self.turn_seq)
            .raw("event", &self.event.to_json())
            .finish()
    }
}

/// What the playpen read from pi after a turn (contract 03 §5.2).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TurnEntries {
    /// The pi entry id of the user message of the turn.
    pub user_entry_id: Option<EntryId>,
    /// The pi leaf entry id after the turn.
    pub leaf_id: Option<EntryId>,
    /// The count of entries that the turn added.
    pub entry_count: Option<u64>,
}

/// `turn_settled`: the turn ended and pi settled (contract 03 §5.2).
///
/// ```
/// use std::num::NonZeroU64;
///
/// use creche_contracts::channel::claim::TurnAddress;
/// use creche_contracts::channel::playpen::{PlaypenMessage, Residence, TurnEntries, TurnSettled, TurnUsage, Usd};
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// let seq = NonZeroU64::MIN;
/// let usage = TurnUsage { input: 7, output: 1, cache_read: 0, cache_write: 0, cost_usd: Usd::ZERO };
/// let settled =
///     TurnSettled::new(address, seq, Residence::Resident, TurnEntries::default(), usage, 1500);
/// assert!(PlaypenMessage::TurnSettled(settled).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use std::num::NonZeroU64;
///
/// use creche_contracts::channel::claim::TurnAddress;
/// use creche_contracts::channel::playpen::{PlaypenMessage, Residence, TurnEntries, TurnSettled, TurnUsage, Usd};
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// let seq = NonZeroU64::MIN;
/// let usage = TurnUsage { input: 7, output: 1, cache_read: 0, cache_write: 0, cost_usd: Usd::ZERO };
/// let settled =
///     TurnSettled::new(address, seq, Residence::Resident, TurnEntries::default(), usage, 1500);
/// let settled = TurnSettled { settled_ms: 0, ..settled };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct TurnSettled {
    address: TurnAddress,
    turn_seq: NonZeroU64,
    resident: bool,
    entries: TurnEntries,
    usage: TurnUsage,
    settled_ms: u64,
}

/// Whether the pi process stays open after a turn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Residence {
    /// The process stays open for the next turn.
    Resident,
    /// The playpen ends the process.
    Released,
}

impl TurnSettled {
    /// The turn at `address` settled after `settled_ms` milliseconds.
    #[must_use]
    pub fn new(
        address: TurnAddress,
        turn_seq: NonZeroU64,
        residence: Residence,
        entries: TurnEntries,
        usage: TurnUsage,
        settled_ms: u64,
    ) -> Self {
        Self {
            address,
            turn_seq,
            resident: residence == Residence::Resident,
            entries,
            usage,
            settled_ms,
        }
    }

    fn body(&self) -> String {
        let entries = &self.entries;

        turn_writer(PlaypenType::TurnSettled, &self.address, self.turn_seq)
            .raw("resident", flag_text(self.resident))
            .text_or_null("user_entry_id", id_text(entries.user_entry_id.as_ref()))
            .text_or_null("leaf_id", id_text(entries.leaf_id.as_ref()))
            .raw_or_null(
                "entry_count",
                entries.entry_count.map(|count| count.to_string()),
            )
            .raw("usage", &self.usage.encoded())
            .raw("settled_ms", &self.settled_ms.to_string())
            .finish()
    }
}

/// `turn_failed`: the turn ended and did not settle (contract 03 §5.3).
///
/// A message that belongs to no turn has no session, no turn and the
/// `turn_seq` 0 (§5.1). The type permits no other mix of the three.
///
/// ```
/// use creche_contracts::channel::playpen::{LogText, PlaypenMessage, TurnFailed};
/// use creche_contracts::channel::vocabulary::FailReason;
///
/// let failed = TurnFailed::of_no_turn(FailReason::LineTooLarge, LogText::cut("a line was too long"));
/// assert!(PlaypenMessage::TurnFailed(failed).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{LogText, PlaypenMessage, TurnFailed};
/// use creche_contracts::channel::vocabulary::FailReason;
///
/// let failed = TurnFailed { place: None, reason: FailReason::Internal, message: LogText::cut("") };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TurnFailed {
    place: Option<(TurnAddress, NonZeroU64)>,
    reason: FailReason,
    message: LogText,
}

impl TurnFailed {
    /// The turn at `address` failed.
    #[must_use]
    pub fn of_turn(
        address: TurnAddress,
        turn_seq: NonZeroU64,
        reason: FailReason,
        message: LogText,
    ) -> Self {
        Self {
            place: Some((address, turn_seq)),
            reason,
            message,
        }
    }

    /// A failure that belongs to no turn: a line from the host over the
    /// size limit.
    #[must_use]
    pub fn of_no_turn(reason: FailReason, message: LogText) -> Self {
        Self {
            place: None,
            reason,
            message,
        }
    }

    fn body(&self) -> String {
        let writer = match &self.place {
            Some((address, turn_seq)) => turn_writer(PlaypenType::TurnFailed, address, *turn_seq),
            None => writer(PlaypenType::TurnFailed)
                .text_or_null("session", None)
                .text_or_null("turn", None)
                .raw("turn_seq", NO_TURN_SEQ),
        };

        writer
            .text("reason", self.reason.as_str())
            .text("message", self.message.as_str())
            .finish()
    }
}

/// How a pi process ended, as the operating system reports it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ExitStatus {
    /// The process id inside the sandbox.
    pub pid: Option<u32>,
    /// The exit code.
    pub code: Option<i32>,
    /// The name of the signal that ended the process.
    pub signal: Option<Label>,
    /// The largest resident memory of the process, in megabytes.
    pub rss_peak_mb: Option<u64>,
}

/// `process_exit`: a pi process ended (contract 03 §5.4).
///
/// ```
/// use creche_contracts::channel::playpen::{ExitStatus, PlaypenMessage, ProcessExit};
/// use creche_contracts::channel::vocabulary::ExitReason;
///
/// let exit =
///     ProcessExit::new("tui-1".parse().unwrap(), ExitStatus::default(), None, ExitReason::Reaped);
/// assert!(PlaypenMessage::ProcessExit(exit).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{ExitStatus, PlaypenMessage, ProcessExit};
/// use creche_contracts::channel::vocabulary::ExitReason;
///
/// let exit =
///     ProcessExit::new("tui-1".parse().unwrap(), ExitStatus::default(), None, ExitReason::Reaped);
/// let exit = ProcessExit { turn: None, ..exit };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessExit {
    session: SessionId,
    status: ExitStatus,
    turn: Option<Ulid>,
    reason: ExitReason,
}

impl ProcessExit {
    /// The process of `session` ended. `turn` is the turn that ran then.
    #[must_use]
    pub fn new(
        session: SessionId,
        status: ExitStatus,
        turn: Option<Ulid>,
        reason: ExitReason,
    ) -> Self {
        Self {
            session,
            status,
            turn,
            reason,
        }
    }

    fn body(&self) -> String {
        let status = &self.status;

        writer(PlaypenType::ProcessExit)
            .text("session", self.session.as_str())
            .raw_or_null("pid", status.pid.map(|pid| pid.to_string()))
            .raw_or_null("code", status.code.map(|code| code.to_string()))
            .text_or_null("signal", status.signal.as_ref().map(Label::as_str))
            .text_or_null("turn", self.turn.as_ref().map(Ulid::as_str))
            .text("reason", self.reason.as_str())
            .raw_or_null(
                "rss_peak_mb",
                status.rss_peak_mb.map(|peak| peak.to_string()),
            )
            .finish()
    }
}

/// `pong`: the answer to `ping` (contract 03 §5.5).
///
/// ```
/// use creche_contracts::channel::playpen::{PlaypenMessage, Pong};
///
/// let pong = Pong::new(Some("5f3a".parse().unwrap()), 1_800_000_000_000, 2, Some(400));
/// assert!(PlaypenMessage::Pong(pong).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{PlaypenMessage, Pong};
///
/// let pong = Pong { nonce: None, ts_ms: 0, resident: 0, rss_mb: None };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pong {
    nonce: Option<Nonce>,
    ts_ms: u64,
    resident: u64,
    rss_mb: Option<u64>,
}

impl Pong {
    /// The answer to the `ping` with `nonce`, at the time `ts_ms` of the
    /// sandbox, with `resident` pi processes.
    #[must_use]
    pub fn new(nonce: Option<Nonce>, ts_ms: u64, resident: u64, rss_mb: Option<u64>) -> Self {
        Self {
            nonce,
            ts_ms,
            resident,
            rss_mb,
        }
    }

    fn body(&self) -> String {
        writer(PlaypenType::Pong)
            .text_or_null("nonce", self.nonce.as_ref().map(Nonce::as_str))
            .raw("ts_ms", &self.ts_ms.to_string())
            .raw("resident", &self.resident.to_string())
            .raw_or_null("rss_mb", self.rss_mb.map(|rss| rss.to_string()))
            .finish()
    }
}

/// `log`: free text, also the stderr of a pi process (contract 03 §5.5).
///
/// ```
/// use creche_contracts::channel::playpen::{Log, LogText, PlaypenMessage};
/// use creche_contracts::channel::vocabulary::LogLevel;
///
/// let log = Log::new(LogLevel::Warn, None, LogText::cut("slow reader"));
/// assert!(PlaypenMessage::Log(log).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{Log, LogText, PlaypenMessage};
/// use creche_contracts::channel::vocabulary::LogLevel;
///
/// let log = Log { level: LogLevel::Warn, session: None, message: LogText::cut("") };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Log {
    level: LogLevel,
    session: Option<SessionId>,
    message: LogText,
}

impl Log {
    /// One line of free text.
    #[must_use]
    pub fn new(level: LogLevel, session: Option<SessionId>, message: LogText) -> Self {
        Self {
            level,
            session,
            message,
        }
    }

    fn body(&self) -> String {
        writer(PlaypenType::Log)
            .text("level", self.level.as_str())
            .text_or_null("session", self.session.as_ref().map(SessionId::as_str))
            .text("message", self.message.as_str())
            .finish()
    }
}

/// One pi entry of an `entries` answer (contract 03 §5.8).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionEntry {
    /// The pi entry id.
    pub id: EntryId,
    /// The role, as pi names it.
    pub role: Label,
    /// The text of the entry. An entry with no text has the empty text.
    pub text: EntryText,
}

impl SessionEntry {
    fn encoded(&self) -> String {
        ObjectWriter::new()
            .text("id", self.id.as_str())
            .text("role", self.role.as_str())
            .text("text", self.text.as_str())
            .finish()
    }
}

/// What the playpen read for one `get_entries`.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Reading {
    /// The entries. `ok` is `true`.
    Read {
        since_matched: bool,
        truncated: bool,
        leaf_id: Option<EntryId>,
        entries: Vec<SessionEntry>,
    },
    /// Nothing. `ok` is `false`, the list is empty and `leaf_id` is `null`
    /// (§5.8 rule 3).
    Refused(Option<FailReason>),
}

/// Whether pi knew the cursor of a read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cursor {
    /// pi knew the cursor. The answer holds the entries after it.
    Matched,
    /// pi did not know the cursor. The answer holds the whole history.
    Unknown,
}

/// Whether an answer holds each entry that pi has.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fill {
    /// The answer holds each entry.
    Whole,
    /// A cap cut the answer. More entries wait.
    Truncated,
}

/// `entries`: the answer to `get_entries` (contract 03 §5.8).
///
/// ```
/// use creche_contracts::channel::playpen::{Entries, PlaypenMessage};
///
/// let refused = Entries::refused("01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(), "tui-1".parse().unwrap(), None);
/// assert!(PlaypenMessage::Entries(refused).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{Entries, PlaypenMessage};
///
/// let refused = Entries::refused("01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(), "tui-1".parse().unwrap(), None);
/// let entries = Entries { session: "tui-2".parse().unwrap(), ..refused };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entries {
    request: Ulid,
    session: SessionId,
    reading: Reading,
}

impl Entries {
    /// The entries that the playpen read, oldest first.
    ///
    /// # Errors
    ///
    /// [`TooManyEntries`] for more than 64 entries. The playpen sends 64 and
    /// says that more entries wait (§8).
    pub fn read(
        request: Ulid,
        session: SessionId,
        cursor: Cursor,
        fill: Fill,
        leaf_id: Option<EntryId>,
        entries: Vec<SessionEntry>,
    ) -> Result<Self, TooManyEntries> {
        if entries.len() > MAX_ENTRIES_PER_READ {
            return Err(TooManyEntries);
        }

        Ok(Self {
            request,
            session,
            reading: Reading::Read {
                since_matched: cursor == Cursor::Matched,
                truncated: fill == Fill::Truncated,
                leaf_id,
                entries,
            },
        })
    }

    /// The playpen read nothing. `reason` is absent when pi refused the
    /// read itself.
    #[must_use]
    pub fn refused(request: Ulid, session: SessionId, reason: Option<FailReason>) -> Self {
        Self {
            request,
            session,
            reading: Reading::Refused(reason),
        }
    }

    fn body(&self) -> String {
        let head = writer(PlaypenType::Entries)
            .text("request", self.request.as_str())
            .text("session", self.session.as_str());

        match &self.reading {
            Reading::Read {
                since_matched,
                truncated,
                leaf_id,
                entries,
            } => {
                let items: Vec<String> = entries.iter().map(SessionEntry::encoded).collect();

                head.raw("ok", flag_text(true))
                    .text_or_null("reason", None)
                    .raw("since_matched", flag_text(*since_matched))
                    .raw("truncated", flag_text(*truncated))
                    .text_or_null("leaf_id", id_text(leaf_id.as_ref()))
                    .raw("entries", &format!("[{}]", items.join(",")))
                    .finish()
            }
            Reading::Refused(reason) => head
                .raw("ok", flag_text(false))
                .text_or_null("reason", reason.map(Word::as_str))
                .raw("since_matched", flag_text(false))
                .raw("truncated", flag_text(false))
                .text_or_null("leaf_id", None)
                .raw("entries", "[]")
                .finish(),
        }
    }
}

/// One `entries` answer holds 64 entries at most.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TooManyEntries;

impl fmt::Display for TooManyEntries {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "an entries answer holds {MAX_ENTRIES_PER_READ} entries at most"
        )
    }
}

impl Error for TooManyEntries {}

/// `fatal`: the playpen cannot serve and exits (contract 03 §5.7).
///
/// ```
/// use creche_contracts::channel::playpen::{Fatal, LogText, PlaypenMessage};
/// use creche_contracts::channel::vocabulary::FatalReason;
///
/// let fatal = Fatal::new(FatalReason::MountDirUnset, LogText::cut("no control directory"));
/// assert!(PlaypenMessage::Fatal(fatal).encode().is_ok());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{Fatal, LogText, PlaypenMessage};
/// use creche_contracts::channel::vocabulary::FatalReason;
///
/// let fatal = Fatal { reason: FatalReason::MountDirUnset, message: LogText::cut("") };
/// let none: Option<PlaypenMessage> = None;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fatal {
    reason: FatalReason,
    message: LogText,
}

impl Fatal {
    /// The last line of a playpen that cannot serve.
    #[must_use]
    pub fn new(reason: FatalReason, message: LogText) -> Self {
        Self { reason, message }
    }

    fn body(&self) -> String {
        writer(PlaypenType::Fatal)
            .text("reason", self.reason.as_str())
            .text("message", self.message.as_str())
            .finish()
    }
}

/// The text of an optional pi entry id.
fn id_text(id: Option<&EntryId>) -> Option<&str> {
    id.map(EntryId::as_str)
}

/// The start of each line: `{"type":"<type>"`.
fn writer(kind: PlaypenType) -> ObjectWriter {
    ObjectWriter::new().text("type", kind.as_str())
}

/// The start of each line of a turn: the type, the session, the turn and the
/// place in the turn.
fn turn_writer(kind: PlaypenType, address: &TurnAddress, turn_seq: NonZeroU64) -> ObjectWriter {
    writer(kind)
        .text("session", address.session().as_str())
        .text("turn", address.turn().as_str())
        .raw("turn_seq", &turn_seq.to_string())
}

/// One message from the playpen to the host, as the playpen writes it
/// (contract 03 §5).
///
/// The host reads the line with [`parse`](super::claim::parse). That reader
/// says what it does with an unknown value.
///
/// ```
/// use creche_contracts::channel::claim::{parse, PlaypenLine};
/// use creche_contracts::channel::playpen::{Log, LogText, PlaypenMessage};
/// use creche_contracts::channel::vocabulary::LogLevel;
///
/// let log = PlaypenMessage::Log(Log::new(LogLevel::Warn, None, LogText::cut("slow reader")));
/// let line = log.encode().unwrap();
///
/// assert_eq!(
///     line,
///     "{\"type\":\"log\",\"level\":\"warn\",\"session\":null,\"message\":\"slow reader\"}\n"
/// );
/// assert!(matches!(parse(line.trim_end()), Ok(PlaypenLine::Log(_))));
/// ```
///
/// Code outside this module cannot build a message from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::playpen::{Log, LogText};
/// use creche_contracts::channel::vocabulary::LogLevel;
///
/// let log = Log { level: LogLevel::Warn, session: None, message: LogText::cut("") };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub enum PlaypenMessage {
    /// `ready`.
    Ready(Ready),
    /// `session_opened`.
    SessionOpened(SessionOpened),
    /// `event`.
    Event(EventMessage),
    /// `turn_settled`.
    TurnSettled(TurnSettled),
    /// `turn_failed`.
    TurnFailed(TurnFailed),
    /// `process_exit`.
    ProcessExit(ProcessExit),
    /// `pong`.
    Pong(Pong),
    /// `log`.
    Log(Log),
    /// `entries`.
    Entries(Entries),
    /// `fatal`.
    Fatal(Fatal),
}

impl PlaypenMessage {
    /// The type of the message.
    #[must_use]
    pub fn kind(&self) -> PlaypenType {
        match self {
            Self::Ready(_) => PlaypenType::Ready,
            Self::SessionOpened(_) => PlaypenType::SessionOpened,
            Self::Event(_) => PlaypenType::Event,
            Self::TurnSettled(_) => PlaypenType::TurnSettled,
            Self::TurnFailed(_) => PlaypenType::TurnFailed,
            Self::ProcessExit(_) => PlaypenType::ProcessExit,
            Self::Pong(_) => PlaypenType::Pong,
            Self::Log(_) => PlaypenType::Log,
            Self::Entries(_) => PlaypenType::Entries,
            Self::Fatal(_) => PlaypenType::Fatal,
        }
    }

    /// The line of the message: compact JSON and one LF.
    ///
    /// # Errors
    ///
    /// [`EncodeError::TooLarge`] when the line has more than
    /// [`MAX_LINE_BYTES`] bytes. The playpen never writes such a line
    /// (contract 03 §2 rule 6). For `entries` it sends fewer entries and
    /// says that more wait (§8).
    pub fn encode(&self) -> Result<String, EncodeError> {
        let body = match self {
            Self::Ready(message) => message.body(),
            Self::SessionOpened(message) => message.body(),
            Self::Event(message) => message.body(),
            Self::TurnSettled(message) => message.body(),
            Self::TurnFailed(message) => message.body(),
            Self::ProcessExit(message) => message.body(),
            Self::Pong(message) => message.body(),
            Self::Log(message) => message.body(),
            Self::Entries(message) => message.body(),
            Self::Fatal(message) => message.body(),
        };

        framed(body, MAX_LINE_BYTES)
    }
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::*;
    use crate::channel::claim::tests::normalized;
    use crate::channel::claim::{PlaypenLine, parse};
    use crate::channel::frame::Refusal;

    const SESSION: &str = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK";
    const TURN: &str = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";

    fn session() -> SessionId {
        SESSION.parse().unwrap()
    }

    fn address() -> TurnAddress {
        TurnAddress::new(session(), TURN.parse().unwrap())
    }

    fn seq(place: u64) -> NonZeroU64 {
        NonZeroU64::new(place).unwrap()
    }

    fn entry_id(text: &str) -> Option<EntryId> {
        Some(text.parse().unwrap())
    }

    /// The record of a message: its line with no LF.
    fn record(message: &PlaypenMessage) -> String {
        let line = message.encode().unwrap();
        let record = line.strip_suffix('\n').unwrap();

        assert!(!record.contains('\n'), "{record}");

        record.to_owned()
    }

    /// What the host reads from the line of a message, as a vector file
    /// writes a value.
    fn read(message: &PlaypenMessage) -> Value {
        let record = record(message);
        let line = parse(&record).unwrap_or_else(|refusal| panic!("{record}: {refusal}"));

        normalized(&line)
    }

    fn ready() -> PlaypenMessage {
        PlaypenMessage::Ready(Ready::new(
            "chat-s3".parse().unwrap(),
            Versions {
                supervisor: Label::cut("agent-supervisor/0.1.0"),
                pi: Label::cut("0.99.1"),
                node: Label::cut("24.1.0"),
            },
            ProcessCap::new(8),
            0,
            Cap::ALL.to_vec(),
        ))
    }

    fn settled() -> PlaypenMessage {
        PlaypenMessage::TurnSettled(TurnSettled::new(
            address(),
            seq(9),
            Residence::Resident,
            TurnEntries {
                user_entry_id: entry_id("a1b2c3d4"),
                leaf_id: entry_id("e5f6a7b8"),
                entry_count: Some(4),
            },
            TurnUsage {
                input: 1200,
                output: 80,
                cache_read: 0,
                cache_write: 0,
                cost_usd: Usd::new(0.25).unwrap(),
            },
            1500,
        ))
    }

    fn entries() -> PlaypenMessage {
        let entry = |id: &str, role: &str, text: &str| SessionEntry {
            id: id.parse().unwrap(),
            role: Label::cut(role),
            text: EntryText::cut(text),
        };
        let read = Entries::read(
            TURN.parse().unwrap(),
            session(),
            Cursor::Matched,
            Fill::Whole,
            entry_id("e6"),
            vec![
                entry("e5", "user", "What did the sensor read?"),
                entry("e6", "assistant", "21.4 degrees."),
                entry("e7", "toolResult", ""),
            ],
        );

        PlaypenMessage::Entries(read.unwrap())
    }

    #[test]
    fn the_host_reads_each_line_that_the_playpen_writes() {
        let event = Event::from_json(r#"{"type":"message_update","delta":"Sensor "}"#).unwrap();
        let written = [
            (
                ready(),
                json!({"kind": "Ready", "message": {
                    "protocol": "1.0", "sandbox": "chat-s3", "playpen": "agent-supervisor/0.1.0",
                    "pi": "0.99.1", "node": "24.1.0", "max_resident_processes": 8,
                    "foreign_pi_processes": 0,
                    "caps": ["steer", "coalesce", "workspace_link", "get_entries"],
                }}),
            ),
            (
                PlaypenMessage::SessionOpened(SessionOpened::resident(
                    session(),
                    LogText::cut("the pi process is resident"),
                )),
                json!({"kind": "OpenedLine", "message": {
                    "session": SESSION, "resident": true, "reason": null,
                    "message": "the pi process is resident",
                }}),
            ),
            (
                PlaypenMessage::SessionOpened(SessionOpened::not_resident(
                    session(),
                    OpenReason::NotHeld,
                    LogText::cut(""),
                )),
                json!({"kind": "OpenedLine", "message": {
                    "session": SESSION, "resident": false, "reason": "not_held", "message": "",
                }}),
            ),
            (
                PlaypenMessage::Event(EventMessage::new(address(), seq(17), event).unwrap()),
                json!({"kind": "EventLine", "message": {
                    "session": SESSION, "turn": TURN, "turn_seq": 17,
                    "event": {"type": "message_update", "delta": "Sensor "},
                }}),
            ),
            (
                settled(),
                json!({"kind": "SettledLine", "message": {
                    "session": SESSION, "turn": TURN, "turn_seq": 9, "resident": true,
                    "user_entry_id": "a1b2c3d4", "leaf_id": "e5f6a7b8", "entry_count": 4,
                    "usage": {
                        "input": 1200, "output": 80, "cache_read": 0, "cache_write": 0,
                        "cost_usd": 0.25,
                    },
                    "settled_ms": 1500,
                }}),
            ),
            (
                PlaypenMessage::TurnFailed(TurnFailed::of_turn(
                    address(),
                    seq(3),
                    FailReason::ForkRefused,
                    LogText::cut("pi refused the fork"),
                )),
                json!({"kind": "FailedLine", "message": {
                    "session": SESSION, "turn": TURN, "turn_seq": 3, "reason": "fork_refused",
                    "message": "pi refused the fork",
                }}),
            ),
            (
                PlaypenMessage::ProcessExit(ProcessExit::new(
                    session(),
                    ExitStatus {
                        pid: Some(7),
                        code: Some(-9),
                        signal: Some(Label::cut("SIGKILL")),
                        rss_peak_mb: Some(300),
                    },
                    Some(TURN.parse().unwrap()),
                    ExitReason::Crashed,
                )),
                json!({"kind": "ProcessExitLine", "message": {
                    "session": SESSION, "code": -9, "reason": "crashed", "turn": TURN,
                }}),
            ),
            (
                PlaypenMessage::Pong(Pong::new(
                    Some("9f13".parse().unwrap()),
                    1_800_000_000_000,
                    3,
                    Some(400),
                )),
                json!({"kind": "PongLine", "message": {"nonce": "9f13"}}),
            ),
            (
                PlaypenMessage::Log(Log::new(
                    LogLevel::Info,
                    Some(session()),
                    LogText::cut("pi wrote to stderr"),
                )),
                json!({"kind": "LogLine", "message": {
                    "level": "info", "session": SESSION, "message": "pi wrote to stderr",
                }}),
            ),
            (
                entries(),
                json!({"kind": "EntriesLine", "message": {
                    "request": TURN, "session": SESSION, "ok": true, "reason": null,
                    "since_matched": true, "truncated": false, "leaf_id": "e6",
                    "entries": [
                        {"id": "e5", "role": "user", "text": "What did the sensor read?"},
                        {"id": "e6", "role": "assistant", "text": "21.4 degrees."},
                        {"id": "e7", "role": "toolResult", "text": ""},
                    ],
                }}),
            ),
            (
                PlaypenMessage::Entries(Entries::refused(
                    TURN.parse().unwrap(),
                    session(),
                    Some(FailReason::SessionBusyInSandbox),
                )),
                json!({"kind": "EntriesLine", "message": {
                    "request": TURN, "session": SESSION, "ok": false,
                    "reason": "session_busy_in_sandbox", "since_matched": false,
                    "truncated": false, "leaf_id": null, "entries": [],
                }}),
            ),
            (
                PlaypenMessage::Fatal(Fatal::new(
                    FatalReason::ControlMountUnwritable,
                    LogText::cut("cannot write the lock: EACCES"),
                )),
                json!({"kind": "FatalLine", "message": {
                    "reason": "control_mount_unwritable",
                    "message": "cannot write the lock: EACCES",
                }}),
            ),
        ];
        let mut kinds = Vec::new();

        for (message, expected) in &written {
            assert_eq!(&read(message), expected, "{message:?}");
            kinds.push(message.kind());
        }

        kinds.dedup();
        assert_eq!(kinds, PlaypenType::ALL);
    }

    #[test]
    fn a_line_has_the_keys_of_the_contract() {
        let exit = PlaypenMessage::ProcessExit(ProcessExit::new(
            session(),
            ExitStatus::default(),
            None,
            ExitReason::Reaped,
        ));
        let pong = PlaypenMessage::Pong(Pong::new(None, 5, 0, None));

        assert_eq!(
            record(&settled()),
            format!(
                "{{\"type\":\"turn_settled\",\"session\":\"{SESSION}\",\"turn\":\"{TURN}\",\
                 \"turn_seq\":9,\"resident\":true,\"user_entry_id\":\"a1b2c3d4\",\
                 \"leaf_id\":\"e5f6a7b8\",\"entry_count\":4,\"usage\":{{\"input\":1200,\
                 \"output\":80,\"cache_read\":0,\"cache_write\":0,\"cost_usd\":0.25}},\
                 \"settled_ms\":1500}}"
            )
        );
        assert_eq!(
            record(&exit),
            format!(
                "{{\"type\":\"process_exit\",\"session\":\"{SESSION}\",\"pid\":null,\
                 \"code\":null,\"signal\":null,\"turn\":null,\"reason\":\"reaped\",\
                 \"rss_peak_mb\":null}}"
            )
        );
        assert_eq!(
            record(&pong),
            "{\"type\":\"pong\",\"nonce\":null,\"ts_ms\":5,\"resident\":0,\"rss_mb\":null}"
        );
        assert_eq!(
            record(&ready()),
            "{\"type\":\"ready\",\"protocol\":\"1.0\",\"supervisor\":\"agent-supervisor/0.1.0\",\
             \"pi\":\"0.99.1\",\"node\":\"24.1.0\",\"sandbox\":\"chat-s3\",\
             \"max_resident_processes\":8,\"foreign_pi_processes\":0,\
             \"caps\":[\"steer\",\"coalesce\",\"workspace_link\",\"get_entries\"]}"
        );
    }

    #[test]
    fn the_python_host_refuses_two_lines_that_the_contract_permits() {
        // Contract 03 §5.1 gives a message of no turn the `turn_seq` 0, with
        // no session and no turn. §5.5 lets `pong` send no nonce back. The
        // Python host reads each of the two lines as malformed, and the
        // parser of the host does the same.
        let no_turn = PlaypenMessage::TurnFailed(TurnFailed::of_no_turn(
            FailReason::LineTooLarge,
            LogText::cut("an inbound line passed the limit"),
        ));
        let no_nonce = PlaypenMessage::Pong(Pong::new(None, 5, 0, None));
        let unset = PlaypenMessage::Fatal(Fatal::new(
            FatalReason::MountDirUnset,
            LogText::cut("the environment names no control directory"),
        ));

        assert_eq!(
            record(&no_turn),
            "{\"type\":\"turn_failed\",\"session\":null,\"turn\":null,\"turn_seq\":0,\
             \"reason\":\"line_too_large\",\"message\":\"an inbound line passed the limit\"}"
        );
        assert_eq!(parse(&record(&no_turn)), Err(Refusal::Malformed));
        assert_eq!(parse(&record(&no_nonce)), Err(Refusal::Malformed));

        // The Python host knows no `mount_dir_unset`. It reads it as unknown.
        assert_eq!(read(&unset)["message"]["reason"], "unknown");
    }

    #[test]
    fn a_free_text_is_cut_between_two_characters() {
        let long = format!("{}\u{e9}", "m".repeat(MAX_LOG_BYTES - 1));
        let astral = format!("{}\u{1f600}", "m".repeat(MAX_LOG_BYTES - 3));

        assert_eq!(LogText::cut("").as_str(), "");
        assert_eq!(LogText::cut(&long).as_str(), "m".repeat(MAX_LOG_BYTES - 1));
        assert_eq!(
            LogText::cut(&astral).as_str(),
            "m".repeat(MAX_LOG_BYTES - 3)
        );
        assert_eq!(
            LogText::cut(&"m".repeat(MAX_LOG_BYTES)).as_str().len(),
            MAX_LOG_BYTES
        );
        assert_eq!(
            EntryText::cut(&"\u{e9}".repeat(MAX_ENTRY_BYTES))
                .as_str()
                .len(),
            MAX_ENTRY_BYTES
        );
        assert_eq!(Label::cut(&"a".repeat(300)).as_str().len(), MAX_LABEL_BYTES);

        // The host keeps a text that the playpen cut, with no second cut.
        let log = PlaypenMessage::Log(Log::new(LogLevel::Warn, None, LogText::cut(&long)));
        assert_eq!(
            read(&log)["message"]["message"],
            "m".repeat(MAX_LOG_BYTES - 1)
        );
    }

    #[test]
    fn an_answer_holds_64_entries_at_most() {
        let entry = SessionEntry {
            id: "e1".parse().unwrap(),
            role: Label::cut("user"),
            text: EntryText::cut(""),
        };
        let answer = |count: usize| {
            Entries::read(
                TURN.parse().unwrap(),
                session(),
                Cursor::Unknown,
                Fill::Truncated,
                None,
                vec![entry.clone(); count],
            )
        };
        let full = PlaypenMessage::Entries(answer(MAX_ENTRIES_PER_READ).unwrap());
        let read = read(&full);

        assert_eq!(answer(MAX_ENTRIES_PER_READ + 1), Err(TooManyEntries));
        assert!(TooManyEntries.to_string().contains("64"));
        assert_eq!(read["message"]["entries"].as_array().unwrap().len(), 64);
        assert_eq!(read["message"]["since_matched"], false);
        assert_eq!(read["message"]["truncated"], true);
    }

    #[test]
    fn a_line_over_the_limit_is_not_written() {
        let entry = SessionEntry {
            id: "e1".parse().unwrap(),
            role: Label::cut("user"),
            text: EntryText::cut(&"a".repeat(MAX_ENTRY_BYTES)),
        };
        let answer = Entries::read(
            TURN.parse().unwrap(),
            session(),
            Cursor::Matched,
            Fill::Whole,
            None,
            vec![entry; 17],
        );
        let line = PlaypenMessage::Entries(answer.unwrap()).encode();

        assert!(
            matches!(line, Err(EncodeError::TooLarge { .. })),
            "{line:?}"
        );
    }

    #[test]
    fn a_cost_is_finite_and_not_below_zero() {
        assert_eq!(Usd::ZERO.get().to_bits(), 0.0_f64.to_bits());
        assert_eq!(Usd::new(2.0).map(Usd::get), Ok(2.0));
        for value in [-0.5, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(Usd::new(value), Err(UsdError));
        }

        assert!(!UsdError.to_string().is_empty());
    }

    #[test]
    fn an_event_that_a_line_cannot_hold_is_refused() {
        let deep =
            |levels: usize| format!("{{\"a\":{}1{}}}", "[".repeat(levels), "]".repeat(levels));
        let large = format!("{{\"text\":\"{}\"}}", "a".repeat(262_144));

        assert!(Event::from_json(&deep(63)).is_ok());
        assert_eq!(Event::from_json(&deep(64)), Err(EventError::TooDeep));
        assert_eq!(Event::from_json(&large), Err(EventError::TooLarge));
        assert_eq!(Event::from_json("{\"a\":NaN}"), Err(EventError::NotJson));
        assert_eq!(Event::from_json("7"), Err(EventError::NotObject));
        for text in ["{\"n\":1e999}", "{\"n\":-1e999}", "{\"a\":[{\"n\":1e999}]}"] {
            assert_eq!(Event::from_json(text), Err(EventError::NotFinite), "{text}");
        }

        assert!(Event::from_json("{\"n\":1e308}").is_ok());
        assert_eq!(
            Event::from_json("{\"a\":\"\\ud800\"}"),
            Err(EventError::LoneSurrogate)
        );
        for error in [
            EventError::NotJson,
            EventError::NotObject,
            EventError::LoneSurrogate,
            EventError::TooLarge,
            EventError::TooDeep,
            EventError::NotFinite,
        ] {
            assert!(!error.to_string().is_empty());
        }
    }

    #[test]
    fn no_message_holds_an_event_with_a_number_that_is_not_finite() {
        // The Python host reads `NaN` and `Infinity` in an event and keeps
        // them, so the parser of the host does too. No line can hold them.
        for number in ["NaN", "Infinity", "-Infinity", "1e999"] {
            let record = format!(
                "{{\"type\":\"event\",\"session\":\"{SESSION}\",\"turn\":\"{TURN}\",\
                 \"turn_seq\":1,\"event\":{{\"type\":\"x\",\"a\":[{number}]}}}}"
            );
            let Ok(PlaypenLine::Event(line)) = parse(&record) else {
                panic!("{record}");
            };

            assert!(!line.event().is_finite(), "{number}");
            assert_eq!(
                EventMessage::new(address(), seq(1), line.event().clone()),
                Err(EventError::NotFinite),
                "{number}"
            );
        }

        let event = Event::from_json("{\"type\":\"x\",\"a\":[1.5,-0.0,1e308]}").unwrap();
        let message = PlaypenMessage::Event(EventMessage::new(address(), seq(1), event).unwrap());

        assert!(serde_json::from_str::<Value>(&record(&message)).is_ok());
    }
}
