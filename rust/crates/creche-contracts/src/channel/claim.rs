//! What the host reads from one line of the playpen (contract 03 §5, §13).
//!
//! The playpen runs inside the sandbox, so each byte from it is a claim.
//! [`parse`] checks the shape and the size of a line before the host uses a
//! value. It returns a refusal and does not panic.
//!
//! The types here hold what the Python host holds after its parse. A session
//! id of a line is a [`Text`], not a `SessionId`: the Python parse accepts
//! each text there, and the check of rule 3 of §13 refuses an unknown one
//! later. [`TurnAddress`] is that later step as a type.

use std::error::Error;
use std::fmt;

use super::frame::Refusal;
use super::host::ProcessCap;
use super::json::{self, Dialect, Json, JsonObject, ReadError};
use super::number::{Cost, Count, Integer, TurnSeq};
use super::text::Text;
use super::vocabulary::{
    Cap, ExitReason, FailReason, LogLevel, OpenReason, PlaypenType, Role, Word,
};
use crate::ids::{SessionId, Ulid};

/// The largest count of bytes of one wrapped pi event (contract 03 §8, §13
/// rule 6). The host keeps only the type of a larger event.
pub const MAX_EVENT_BYTES: usize = 262_144;

/// The deepest nesting of objects and arrays in one event that the host
/// keeps, the event included.
// CONTRACT-QUESTION: contract 03 §13 rule 6 caps the bytes of an event and
// names no nesting limit. The Python host reads an event that nests past 64
// levels as oversized and keeps its type only. This reader does the same. To
// refuse the line costs the turn: a refused line leaves a gap in `turn_seq`.
pub const MAX_EVENT_DEPTH: usize = 64;

/// The largest count of code points that the host keeps of one free text of
/// a line: a `message`.
// CONTRACT-QUESTION: contract 03 §8 caps a `log` message at 4 KiB, which is a
// count of bytes. The Python host cuts at 4096 code points, which can be
// 16 KiB of UTF-8. This reader cuts where the Python host cuts.
pub const MAX_LOG_CHARS: usize = 4096;

/// The largest count of code points that the host keeps of the text of one
/// entry (contract 03 §8, `MAX_ENTRY_BYTES`). The Python host counts code
/// points here too.
pub const MAX_ENTRY_CHARS: usize = 65_536;

/// The largest count of entries that the host reads from one `entries`
/// answer (contract 03 §8).
pub const MAX_ENTRIES_PER_READ: usize = 64;

/// The largest count of code points that the host keeps of one reason name
/// that it does not match against a table.
pub const MAX_REASON_CHARS: usize = 64;

/// The count of resident processes that the host assumes when `ready` gives
/// no valid one (contract 03 §6 rule 8).
pub const MAX_RESIDENT_PROCESSES: u64 = ProcessCap::DEFAULT.get();

/// The type name of an event that gives no usable one.
const UNKNOWN_EVENT_TYPE: &str = "unknown";

/// A name from the sandbox that the host keeps as it is.
///
/// The host matches the name against a closed set `T`. A name outside the set
/// is not refused: the host keeps its text, because components release
/// separately and a newer playpen can send a new name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Claimed<T> {
    /// A name of the set.
    Known(T),
    /// A name outside the set, as the playpen sent it.
    Unknown(Text),
}

impl<T: Word> Claimed<T> {
    fn of(text: Text) -> Self {
        match text.as_str().and_then(T::from_wire) {
            Some(known) => Self::Known(known),
            None => Self::Unknown(text),
        }
    }

    /// The name of the set. `None` for a name outside the set.
    #[must_use]
    pub fn known(&self) -> Option<T> {
        match self {
            Self::Known(known) => Some(*known),
            Self::Unknown(_) => None,
        }
    }

    /// The name as the playpen sent it.
    #[must_use]
    pub fn to_text(&self) -> Text {
        match self {
            Self::Known(known) => Text::from(known.as_str()),
            Self::Unknown(text) => text.clone(),
        }
    }
}

/// The turn that a line names: a session id and a turn id that have the form
/// of an id (contract 03 §13 rule 3).
///
/// A line whose `session` or `turn` has no such form names no turn of the
/// host, because the host mints each id itself.
///
/// ```
/// use creche_contracts::channel::claim::TurnAddress;
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// assert_eq!(address.session().as_str(), "tui-1");
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::TurnAddress;
///
/// let address = TurnAddress { session: "tui-1".parse().unwrap(), turn: "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TurnAddress {
    session: SessionId,
    turn: Ulid,
}

impl TurnAddress {
    /// The address of this session and this turn.
    #[must_use]
    pub fn new(session: SessionId, turn: Ulid) -> Self {
        Self { session, turn }
    }

    /// The session.
    #[must_use]
    pub fn session(&self) -> &SessionId {
        &self.session
    }

    /// The turn.
    #[must_use]
    pub fn turn(&self) -> &Ulid {
        &self.turn
    }

    fn claimed(session: &Text, turn: &Text) -> Result<Self, Refusal> {
        let session = session.as_str().and_then(|text| text.parse().ok());
        let turn = turn.as_str().and_then(|text| text.parse().ok());
        match (session, turn) {
            (Some(session), Some(turn)) => Ok(Self { session, turn }),
            _ => Err(Refusal::UnknownAddress),
        }
    }
}

/// Why the playpen cannot serve, as the host reads it (contract 03 §5.7).
///
/// A reader accepts an unknown value as [`FatalClaim::Unknown`]. §3 version
/// rule 2 forbids a fatal unknown field, so a reason that a newer image sends
/// is still a fault that the operator sees.
///
/// The Python host knows one reason. It reads `mount_dir_unset` of §5.7 as
/// unknown, and this reader does the same.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FatalClaim {
    /// The control mount is not writable.
    ControlMountUnwritable,
    /// Each other value, and no value.
    Unknown,
}

impl FatalClaim {
    /// The name of the reason, as the Python host writes it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ControlMountUnwritable => "control_mount_unwritable",
            Self::Unknown => "unknown",
        }
    }
}

/// The token counts and the cost of one turn (contract 03 §5.2). Each value
/// is advisory (§13 rule 7).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, Usage, parse};
/// use creche_contracts::channel::number::Count;
///
/// let record = r#"{"type":"turn_settled","session":"s","turn":"t","turn_seq":2,"usage":{"input":7}}"#;
/// let usage: Option<Usage> = match parse(record) {
///     Ok(PlaypenLine::Settled(line)) => Some(line.usage().clone()),
///     _ => None,
/// };
/// assert_eq!(usage.unwrap().input(), &Count::from(7_u64));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, Usage, parse};
/// use creche_contracts::channel::number::Count;
///
/// let Ok(PlaypenLine::Settled(line)) = parse("{}") else { return };
/// let usage = Usage { input: Count::from(7_u64), ..line.usage().clone() };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct Usage {
    input: Count,
    output: Count,
    cache_read: Count,
    cache_write: Count,
    cost_usd: Cost,
}

impl Usage {
    /// The input tokens.
    #[must_use]
    pub fn input(&self) -> &Count {
        &self.input
    }

    /// The output tokens.
    #[must_use]
    pub fn output(&self) -> &Count {
        &self.output
    }

    /// The tokens that the model read from its cache.
    #[must_use]
    pub fn cache_read(&self) -> &Count {
        &self.cache_read
    }

    /// The tokens that the model wrote to its cache.
    #[must_use]
    pub fn cache_write(&self) -> &Count {
        &self.cache_write
    }

    /// The cost in US dollars.
    #[must_use]
    pub fn cost_usd(&self) -> Cost {
        self.cost_usd
    }
}

/// One wrapped pi event (contract 03 §5.1). Its content is opaque: the host
/// records it and does not act on it (§13 rule 5).
///
/// An event is an object of at most [`MAX_EVENT_BYTES`] bytes that nests at
/// most [`MAX_EVENT_DEPTH`] levels. For a larger event the host keeps three
/// fields: `type`, `truncated` and `original_bytes` (§13 rule 6).
#[derive(Debug, Clone, PartialEq)]
pub struct Event(JsonObject);

impl Event {
    /// The fields of the event.
    #[must_use]
    pub fn fields(&self) -> &JsonObject {
        &self.0
    }

    /// The `type` of the event, when it is a text.
    #[must_use]
    pub fn kind(&self) -> Option<&Text> {
        self.0.get("type").and_then(Json::as_text)
    }

    /// Whether the host kept only the type of the event.
    #[must_use]
    pub fn is_truncated(&self) -> bool {
        self.0.get("truncated").is_some_and(Json::is_true)
    }

    /// The event as compact JSON: the bytes that the Python host counts
    /// against [`MAX_EVENT_BYTES`].
    #[must_use]
    pub fn to_json(&self) -> String {
        // An event holds no lone surrogate, so the writer gives a text.
        json::object_text(&self.0).unwrap_or_default()
    }

    /// Reads one pi event that the playpen sends whole.
    ///
    /// The playpen truncates a larger event before it sends it (contract 03
    /// §8). This constructor does not truncate. It refuses.
    ///
    /// It also refuses a number that is not finite, for example `1e999`. A
    /// record is JSON (§2 rule 2), and JSON has no text for such a number.
    ///
    /// ```
    /// use creche_contracts::channel::claim::{Event, EventError};
    ///
    /// let event = Event::from_json(r#"{"type":"message_update","delta":"S"}"#)?;
    /// assert_eq!(event.to_json(), r#"{"type":"message_update","delta":"S"}"#);
    /// assert_eq!(Event::from_json("[]"), Err(EventError::NotObject));
    /// assert_eq!(Event::from_json(r#"{"n":1e999}"#), Err(EventError::NotFinite));
    /// # Ok::<(), EventError>(())
    /// ```
    ///
    /// Code outside this module cannot build an event from raw fields:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::claim::Event;
    /// use creche_contracts::channel::json::JsonObject;
    ///
    /// let event = Event(JsonObject::default());
    /// ```
    ///
    /// # Errors
    ///
    /// Why a line cannot hold the text as an event.
    pub fn from_json(text: &str) -> Result<Self, EventError> {
        let value = json::read(text, Dialect::Strict).map_err(|_| EventError::NotJson)?;
        let size = json::compact_size(&value).map_err(|_| EventError::LoneSurrogate)?;
        if size > MAX_EVENT_BYTES {
            return Err(EventError::TooLarge);
        }

        if json::nests_past(&value, MAX_EVENT_DEPTH) {
            return Err(EventError::TooDeep);
        }

        let event = Self(value.into_object().ok_or(EventError::NotObject)?);
        if !event.is_finite() {
            return Err(EventError::NotFinite);
        }

        Ok(event)
    }

    /// Whether each number of the event is finite. JSON has no text for
    /// `NaN`, `Infinity` and `-Infinity`. The Python host reads the three
    /// names, so an event of a line from the playpen can hold one.
    #[must_use]
    pub fn is_finite(&self) -> bool {
        !json::holds_non_finite(&self.0)
    }

    /// Applies rule 6 of §13 to the `event` of a line.
    fn capped(fields: JsonObject) -> Result<Self, Refusal> {
        let whole = Json::Object(fields);

        // Python raises `UnicodeEncodeError` on a lone surrogate here.
        let size = json::compact_size(&whole).map_err(|_| Refusal::Malformed)?;
        let small = size <= MAX_EVENT_BYTES && !json::nests_past(&whole, MAX_EVENT_DEPTH);
        let fields = whole.into_object().unwrap_or_default();
        if small {
            return Ok(Self(fields));
        }

        let kind = fields
            .get("type")
            .and_then(Json::as_text)
            .filter(|kind| !kind.is_empty())
            .cloned()
            .unwrap_or_else(|| Text::from(UNKNOWN_EVENT_TYPE));
        let size = u64::try_from(size).map_err(|_| Refusal::Malformed)?;

        Ok(Self(JsonObject::of(vec![
            (Text::from("type"), Json::Text(kind)),
            (Text::from("truncated"), Json::Bool(true)),
            (Text::from("original_bytes"), Json::Int(Integer::from(size))),
        ])))
    }
}

/// Why a text is not an event that a line can hold.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventError {
    /// The text is not JSON.
    NotJson,
    /// The text is JSON and not an object.
    NotObject,
    /// A string of the event holds a lone surrogate. The host refuses such
    /// a line.
    LoneSurrogate,
    /// The event has more than [`MAX_EVENT_BYTES`] bytes.
    TooLarge,
    /// The event nests more than [`MAX_EVENT_DEPTH`] levels.
    TooDeep,
    /// A number of the event is not finite. JSON has no text for it, so a
    /// record cannot hold it (contract 03 §2 rule 2).
    NotFinite,
}

impl fmt::Display for EventError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotJson => f.write_str("an event is JSON"),
            Self::NotObject => f.write_str("an event is a JSON object"),
            Self::LoneSurrogate => f.write_str("an event holds no lone surrogate"),
            Self::TooLarge => write!(f, "an event has {MAX_EVENT_BYTES} bytes at most"),
            Self::TooDeep => write!(f, "an event nests {MAX_EVENT_DEPTH} levels at most"),
            Self::NotFinite => f.write_str("each number of an event is finite"),
        }
    }
}

impl Error for EventError {}

/// The first line of the playpen (contract 03 §3).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, Ready, parse};
///
/// let line: Option<Ready> = match parse(r#"{"type":"ready","protocol":"1.0","sandbox":"chat-s1"}"#) {
///     Ok(PlaypenLine::Ready(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, Ready, parse};
///
/// let Ok(PlaypenLine::Ready(line)) = parse("{}") else { return };
/// let line = Ready { caps: Vec::new(), ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ready {
    protocol: Text,
    sandbox: Text,
    playpen: Text,
    pi: Text,
    node: Text,
    max_resident_processes: Count,
    foreign_pi_processes: Count,
    caps: Vec<Text>,
}

impl Ready {
    /// The protocol version, `<major>.<minor>`.
    #[must_use]
    pub fn protocol(&self) -> &Text {
        &self.protocol
    }

    /// The part of the protocol version before its first `.`. The host
    /// closes the channel when it is not the major version of the host.
    #[must_use]
    pub fn major(&self) -> Text {
        self.protocol.before('.')
    }

    /// The sandbox id that the playpen claims. The host compares it with the
    /// sandbox id that it dialled (§3 version rule 3).
    #[must_use]
    pub fn sandbox(&self) -> &Text {
        &self.sandbox
    }

    /// The name and the version of the playpen: the field `supervisor`.
    #[must_use]
    pub fn playpen(&self) -> &Text {
        &self.playpen
    }

    /// The pi version inside the image.
    #[must_use]
    pub fn pi(&self) -> &Text {
        &self.pi
    }

    /// The Node version inside the image.
    #[must_use]
    pub fn node(&self) -> &Text {
        &self.node
    }

    /// The ceiling of the playpen for resident pi processes.
    #[must_use]
    pub fn max_resident_processes(&self) -> &Count {
        &self.max_resident_processes
    }

    /// The count of pi processes that the playpen did not start.
    #[must_use]
    pub fn foreign_pi_processes(&self) -> &Count {
        &self.foreign_pi_processes
    }

    /// The optional behaviors that the playpen supports, as it names them.
    #[must_use]
    pub fn caps(&self) -> &[Text] {
        &self.caps
    }

    /// Whether the playpen says that it supports `cap`.
    #[must_use]
    pub fn supports(&self, cap: Cap) -> bool {
        self.caps.iter().any(|name| name == cap.as_str())
    }
}

/// The answer to `open_session` (contract 03 §5.6). The host reads it and
/// acts on nothing in it.
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, OpenedLine, parse};
///
/// let line: Option<OpenedLine> = match parse(r#"{"type":"session_opened","session":"tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"}"#) {
///     Ok(PlaypenLine::Opened(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, OpenedLine, parse};
///
/// let Ok(PlaypenLine::Opened(line)) = parse("{}") else { return };
/// let line = OpenedLine { resident: true, ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenedLine {
    session: Text,
    resident: bool,
    reason: Option<Claimed<OpenReason>>,
    message: Text,
}

impl OpenedLine {
    /// The session that the line names.
    #[must_use]
    pub fn session(&self) -> &Text {
        &self.session
    }

    /// Whether a pi process is now resident for the session.
    #[must_use]
    pub fn resident(&self) -> bool {
        self.resident
    }

    /// Why no process is resident. At most [`MAX_REASON_CHARS`] code points.
    #[must_use]
    pub fn reason(&self) -> Option<&Claimed<OpenReason>> {
        self.reason.as_ref()
    }

    /// Free text. At most [`MAX_LOG_CHARS`] code points.
    #[must_use]
    pub fn message(&self) -> &Text {
        &self.message
    }
}

/// One wrapped pi event and its place in a turn (contract 03 §5.1).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, EventLine, parse};
///
/// let line: Option<EventLine> = match parse(r#"{"type":"event","session":"s","turn":"t","turn_seq":1,"event":{}}"#) {
///     Ok(PlaypenLine::Event(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, EventLine, parse};
///
/// let Ok(PlaypenLine::Event(line)) = parse("{}") else { return };
/// let line = EventLine { session: "s".into(), ..line };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct EventLine {
    session: Text,
    turn: Text,
    turn_seq: TurnSeq,
    event: Event,
}

/// The turn ended and pi settled (contract 03 §5.2).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, SettledLine, parse};
///
/// let line: Option<SettledLine> = match parse(r#"{"type":"turn_settled","session":"s","turn":"t","turn_seq":2}"#) {
///     Ok(PlaypenLine::Settled(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, SettledLine, parse};
///
/// let Ok(PlaypenLine::Settled(line)) = parse("{}") else { return };
/// let line = SettledLine { resident: true, ..line };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct SettledLine {
    session: Text,
    turn: Text,
    turn_seq: TurnSeq,
    resident: bool,
    usage: Usage,
    user_entry_id: Option<Text>,
    leaf_id: Option<Text>,
    entry_count: Option<Count>,
    settled_ms: Count,
}

/// The turn ended and did not settle (contract 03 §5.3).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, FailedLine, parse};
///
/// let line: Option<FailedLine> = match parse(r#"{"type":"turn_failed","session":"s","turn":"t","turn_seq":2,"reason":"internal"}"#) {
///     Ok(PlaypenLine::Failed(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, FailedLine, parse};
///
/// let Ok(PlaypenLine::Failed(line)) = parse("{}") else { return };
/// let line = FailedLine { session: "s".into(), ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailedLine {
    session: Text,
    turn: Text,
    turn_seq: TurnSeq,
    reason: FailReason,
    message: Text,
}

/// Gives a line of a turn its three shared fields.
macro_rules! turn_fields {
    ($name:ident) => {
        impl $name {
            /// The session that the line names. It is a claim.
            #[must_use]
            pub fn session(&self) -> &Text {
                &self.session
            }

            /// The turn that the line names. It is a claim.
            #[must_use]
            pub fn turn(&self) -> &Text {
                &self.turn
            }

            /// The place of the line in its turn.
            #[must_use]
            pub fn turn_seq(&self) -> &TurnSeq {
                &self.turn_seq
            }

            /// The turn that the line names, as two ids.
            ///
            /// # Errors
            ///
            /// [`Refusal::UnknownAddress`] when `session` is no session id or
            /// `turn` is no ULID. The host has no such turn in flight.
            pub fn address(&self) -> Result<TurnAddress, Refusal> {
                TurnAddress::claimed(&self.session, &self.turn)
            }
        }
    };
}

turn_fields!(EventLine);
turn_fields!(SettledLine);
turn_fields!(FailedLine);

impl EventLine {
    /// The pi event.
    #[must_use]
    pub fn event(&self) -> &Event {
        &self.event
    }
}

impl SettledLine {
    /// Whether the pi process stays open for the next turn.
    #[must_use]
    pub fn resident(&self) -> bool {
        self.resident
    }

    /// The usage of the turn.
    #[must_use]
    pub fn usage(&self) -> &Usage {
        &self.usage
    }

    /// The pi entry id of the user message of the turn.
    #[must_use]
    pub fn user_entry_id(&self) -> Option<&Text> {
        self.user_entry_id.as_ref()
    }

    /// The pi leaf entry id after the turn.
    #[must_use]
    pub fn leaf_id(&self) -> Option<&Text> {
        self.leaf_id.as_ref()
    }

    /// The count of entries that the turn added.
    #[must_use]
    pub fn entry_count(&self) -> Option<&Count> {
        self.entry_count.as_ref()
    }

    /// The milliseconds from `start_turn` to `agent_settled`.
    #[must_use]
    pub fn settled_ms(&self) -> &Count {
        &self.settled_ms
    }
}

impl FailedLine {
    /// Why the turn failed.
    #[must_use]
    pub fn reason(&self) -> FailReason {
        self.reason
    }

    /// Free text. At most [`MAX_LOG_CHARS`] code points.
    #[must_use]
    pub fn message(&self) -> &Text {
        &self.message
    }
}

/// A pi process ended (contract 03 §5.4).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, ProcessExitLine, parse};
///
/// let line: Option<ProcessExitLine> = match parse(r#"{"type":"process_exit","session":"s","code":0}"#) {
///     Ok(PlaypenLine::ProcessExit(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, ProcessExitLine, parse};
///
/// let Ok(PlaypenLine::ProcessExit(line)) = parse("{}") else { return };
/// let line = ProcessExitLine { code: None, ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessExitLine {
    session: Text,
    code: Option<Integer>,
    reason: Claimed<ExitReason>,
    turn: Option<Text>,
}

impl ProcessExitLine {
    /// The session of the process.
    #[must_use]
    pub fn session(&self) -> &Text {
        &self.session
    }

    /// The exit code. The Python host keeps an integer of any size.
    #[must_use]
    pub fn code(&self) -> Option<&Integer> {
        self.code.as_ref()
    }

    /// Why the process ended. The host reads no reason as `crashed`.
    #[must_use]
    pub fn reason(&self) -> &Claimed<ExitReason> {
        &self.reason
    }

    /// The turn that ran when the process ended.
    #[must_use]
    pub fn turn(&self) -> Option<&Text> {
        self.turn.as_ref()
    }
}

/// The answer to `ping` (contract 03 §5.5).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, PongLine, parse};
///
/// let line: Option<PongLine> = match parse(r#"{"type":"pong","nonce":"5f3a"}"#) {
///     Ok(PlaypenLine::Pong(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, PongLine, parse};
///
/// let read = parse("{}");
/// let line = PongLine { nonce: "5f3a".into() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PongLine {
    nonce: Text,
}

impl PongLine {
    /// The nonce of the `ping` that the line answers.
    #[must_use]
    pub fn nonce(&self) -> &Text {
        &self.nonce
    }
}

/// Free text, also the stderr of a pi process (contract 03 §5.5).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, LogLine, parse};
///
/// let line: Option<LogLine> = match parse(r#"{"type":"log","level":"warn","message":"slow reader"}"#) {
///     Ok(PlaypenLine::Log(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, LogLine, parse};
///
/// let Ok(PlaypenLine::Log(line)) = parse("{}") else { return };
/// let line = LogLine { session: None, ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogLine {
    level: Claimed<LogLevel>,
    message: Text,
    session: Option<Text>,
}

impl LogLine {
    /// The level. The host reads no level as `info`.
    #[must_use]
    pub fn level(&self) -> &Claimed<LogLevel> {
        &self.level
    }

    /// The text. At most [`MAX_LOG_CHARS`] code points.
    #[must_use]
    pub fn message(&self) -> &Text {
        &self.message
    }

    /// The session that the text belongs to.
    #[must_use]
    pub fn session(&self) -> Option<&Text> {
        self.session.as_ref()
    }
}

/// One pi entry of an `entries` answer (contract 03 §5.8).
///
/// ```
/// use creche_contracts::channel::claim::{PiEntry, PlaypenLine, parse};
///
/// let record = r#"{"type":"entries","request":"r","session":"s","entries":[{"id":"e5","text":"Yes."}]}"#;
/// let entries: Vec<PiEntry> = match parse(record) {
///     Ok(PlaypenLine::Entries(line)) => line.entries().to_vec(),
///     _ => Vec::new(),
/// };
/// assert_eq!(entries[0].id(), &"e5");
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PiEntry, PlaypenLine, parse};
///
/// let Ok(PlaypenLine::Entries(line)) = parse("{}") else { return };
/// let Some(entry) = line.entries().first().cloned() else { return };
/// let entry = PiEntry { id: "e5".into(), ..entry };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PiEntry {
    id: Text,
    role: Claimed<Role>,
    text: Text,
}

impl PiEntry {
    /// The pi entry id: a cursor into the session.
    #[must_use]
    pub fn id(&self) -> &Text {
        &self.id
    }

    /// The role. The host matches `user` and `assistant` and ignores each
    /// other name (§5.8 rule 2).
    #[must_use]
    pub fn role(&self) -> &Claimed<Role> {
        &self.role
    }

    /// The text of the entry. At most [`MAX_ENTRY_CHARS`] code points.
    #[must_use]
    pub fn text(&self) -> &Text {
        &self.text
    }
}

/// The answer to `get_entries` (contract 03 §5.8). It names no turn.
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, EntriesLine, parse};
///
/// let line: Option<EntriesLine> = match parse(r#"{"type":"entries","request":"01JBQ7WZ0X4T9V6K2H8M3N5PQR","session":"s","ok":true}"#) {
///     Ok(PlaypenLine::Entries(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, EntriesLine, parse};
///
/// let Ok(PlaypenLine::Entries(line)) = parse("{}") else { return };
/// let line = EntriesLine { ok: true, ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntriesLine {
    request: Text,
    session: Text,
    ok: bool,
    entries: Vec<PiEntry>,
    since_matched: bool,
    truncated: bool,
    leaf_id: Option<Text>,
    reason: Option<Claimed<FailReason>>,
}

impl EntriesLine {
    /// The correlation id of the `get_entries` that the line answers.
    #[must_use]
    pub fn request(&self) -> &Text {
        &self.request
    }

    /// The session that the line names.
    #[must_use]
    pub fn session(&self) -> &Text {
        &self.session
    }

    /// Whether the playpen read the entries.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.ok
    }

    /// The entries, oldest first. At most [`MAX_ENTRIES_PER_READ`].
    #[must_use]
    pub fn entries(&self) -> &[PiEntry] {
        &self.entries
    }

    /// Whether pi knew the cursor of the request.
    #[must_use]
    pub fn since_matched(&self) -> bool {
        self.since_matched
    }

    /// Whether more entries wait.
    #[must_use]
    pub fn truncated(&self) -> bool {
        self.truncated
    }

    /// The newest pi entry id after the read.
    #[must_use]
    pub fn leaf_id(&self) -> Option<&Text> {
        self.leaf_id.as_ref()
    }

    /// Why the playpen refused the read. At most [`MAX_REASON_CHARS`] code
    /// points.
    #[must_use]
    pub fn reason(&self) -> Option<&Claimed<FailReason>> {
        self.reason.as_ref()
    }
}

/// The playpen cannot serve and exits (contract 03 §5.7).
///
/// ```
/// use creche_contracts::channel::claim::{PlaypenLine, FatalLine, parse};
///
/// let line: Option<FatalLine> = match parse(r#"{"type":"fatal","reason":"control_mount_unwritable"}"#) {
///     Ok(PlaypenLine::Fatal(line)) => Some(line),
///     _ => None,
/// };
/// assert!(line.is_some());
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::{PlaypenLine, FatalLine, parse};
///
/// let Ok(PlaypenLine::Fatal(line)) = parse("{}") else { return };
/// let line = FatalLine { message: "".into(), ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FatalLine {
    reason: FatalClaim,
    message: Text,
}

impl FatalLine {
    /// Why the playpen cannot serve.
    #[must_use]
    pub fn reason(&self) -> FatalClaim {
        self.reason
    }

    /// Free text. At most [`MAX_LOG_CHARS`] code points.
    #[must_use]
    pub fn message(&self) -> &Text {
        &self.message
    }
}

/// One message from the playpen, as the host reads it (contract 03 §5).
///
/// The set of message types is closed. A reader refuses a line with an
/// unknown `type` and counts the refusal (§13 rules 1 and 2). Inside a known
/// message a reader ignores each field that it does not know (§3 version
/// rule 2).
#[derive(Debug, Clone, PartialEq)]
pub enum PlaypenLine {
    /// `ready`: the first line of the channel.
    Ready(Ready),
    /// `session_opened`: the answer to `open_session`.
    Opened(OpenedLine),
    /// `event`: one wrapped pi event.
    Event(EventLine),
    /// `turn_settled`: the turn ended and pi settled.
    Settled(SettledLine),
    /// `turn_failed`: the turn ended and did not settle.
    Failed(FailedLine),
    /// `process_exit`: a pi process ended.
    ProcessExit(ProcessExitLine),
    /// `pong`: the answer to `ping`.
    Pong(PongLine),
    /// `log`: free text.
    Log(LogLine),
    /// `entries`: the answer to `get_entries`.
    Entries(EntriesLine),
    /// `fatal`: the playpen cannot serve and exits.
    Fatal(FatalLine),
}

/// Reads one record from the playpen (contract 03 §13 rule 1).
///
/// The input is one record of [`super::frame::LineSplitter`]. This is a
/// process edge: the caller drops a refused line and counts it against the
/// refusal budget of §13 rule 2. The function accepts and refuses what
/// `attendance.wire.parse` of the Python host does.
///
/// ```
/// use creche_contracts::channel::claim::{parse, PlaypenLine};
/// use creche_contracts::channel::frame::Refusal;
///
/// let Ok(PlaypenLine::Pong(pong)) = parse(r#"{"type":"pong","nonce":"5f3a"}"#) else {
///     return;
/// };
/// assert_eq!(pong.nonce(), &"5f3a");
/// assert_eq!(parse("[]").unwrap_err(), Refusal::NotObject);
/// ```
///
/// # Errors
///
/// The reason the host drops the line: [`Refusal::NotJson`],
/// [`Refusal::NotObject`], [`Refusal::UnknownType`] or
/// [`Refusal::Malformed`].
pub fn parse(text: &str) -> Result<PlaypenLine, Refusal> {
    let value = json::read(text, Dialect::Python).map_err(|error| match error {
        ReadError::NotJson => Refusal::NotJson,
        // Python raises `RecursionError` and `ValueError` here, and the Python
        // host reads each exception as a malformed line.
        ReadError::TooDeep | ReadError::IntegerTooLong => Refusal::Malformed,
    })?;
    let mut record = value.into_object().ok_or(Refusal::NotObject)?;
    let kind = text_of(&record, "type");
    let kind = kind
        .as_ref()
        .and_then(Text::as_str)
        .and_then(PlaypenType::from_wire);

    match kind.ok_or(Refusal::UnknownType)? {
        PlaypenType::Ready => ready(&record).map(PlaypenLine::Ready),
        PlaypenType::SessionOpened => opened(&record).map(PlaypenLine::Opened),
        PlaypenType::Event => event(&mut record).map(PlaypenLine::Event),
        PlaypenType::TurnSettled => settled(&record).map(PlaypenLine::Settled),
        PlaypenType::TurnFailed => failed(&record).map(PlaypenLine::Failed),
        PlaypenType::ProcessExit => process_exit(&record).map(PlaypenLine::ProcessExit),
        PlaypenType::Pong => pong(&record).map(PlaypenLine::Pong),
        PlaypenType::Log => Ok(PlaypenLine::Log(log(&record))),
        PlaypenType::Entries => entries(&record).map(PlaypenLine::Entries),
        PlaypenType::Fatal => Ok(PlaypenLine::Fatal(fatal(&record))),
    }
}

/// The field `key` when it is a text.
fn text_of(record: &JsonObject, key: &str) -> Option<Text> {
    record.get(key).and_then(Json::as_text).cloned()
}

/// The field `key` when it is a text, and the empty text when it is not.
fn text_or_empty(record: &JsonObject, key: &str) -> Text {
    text_of(record, key).unwrap_or_else(Text::empty)
}

/// The free text of the field `message`, cut at [`MAX_LOG_CHARS`].
fn message_of(record: &JsonObject) -> Text {
    text_or_empty(record, "message").truncated(MAX_LOG_CHARS)
}

/// The field `key` when it is a text, cut at [`MAX_REASON_CHARS`].
fn short_text(record: &JsonObject, key: &str) -> Option<Text> {
    text_of(record, key).map(|text| text.truncated(MAX_REASON_CHARS))
}

/// Whether the field `key` is `true`. Each other value is not, also 1.
fn flag(record: &JsonObject, key: &str) -> bool {
    record.get(key).is_some_and(Json::is_true)
}

/// The field `key` when it is an integer that is 0 or more. A float is no
/// count, also `2.0`.
fn count_of(record: &JsonObject, key: &str) -> Option<Count> {
    let integer = record.get(key).and_then(Json::as_integer)?;

    Count::try_from(integer.clone()).ok()
}

fn count_or(record: &JsonObject, key: &str, fallback: u64) -> Count {
    count_of(record, key).unwrap_or_else(|| Count::from(fallback))
}

/// The three shared fields of a line of a turn.
fn turn_fields(record: &JsonObject) -> Result<(Text, Text, TurnSeq), Refusal> {
    let session = text_of(record, "session").ok_or(Refusal::Malformed)?;
    let turn = text_of(record, "turn").ok_or(Refusal::Malformed)?;
    let seq = count_of(record, "turn_seq").ok_or(Refusal::Malformed)?;
    let seq = TurnSeq::try_from(seq).map_err(|_| Refusal::Malformed)?;

    Ok((session, turn, seq))
}

fn ready(record: &JsonObject) -> Result<Ready, Refusal> {
    let caps = record.get("caps").and_then(Json::as_array);
    let caps = caps.map(|items| items.iter().filter_map(Json::as_text).cloned().collect());

    Ok(Ready {
        protocol: text_of(record, "protocol").ok_or(Refusal::Malformed)?,
        sandbox: text_of(record, "sandbox").ok_or(Refusal::Malformed)?,
        playpen: text_or_empty(record, "supervisor"),
        pi: text_or_empty(record, "pi"),
        node: text_or_empty(record, "node"),
        max_resident_processes: count_or(record, "max_resident_processes", MAX_RESIDENT_PROCESSES),
        foreign_pi_processes: count_or(record, "foreign_pi_processes", 0),
        caps: caps.unwrap_or_default(),
    })
}

fn opened(record: &JsonObject) -> Result<OpenedLine, Refusal> {
    Ok(OpenedLine {
        session: text_of(record, "session").ok_or(Refusal::Malformed)?,
        resident: flag(record, "resident"),
        reason: short_text(record, "reason").map(Claimed::of),
        message: message_of(record),
    })
}

fn event(record: &mut JsonObject) -> Result<EventLine, Refusal> {
    let (session, turn, turn_seq) = turn_fields(record)?;
    let fields = record.take("event").and_then(Json::into_object);

    Ok(EventLine {
        session,
        turn,
        turn_seq,
        event: Event::capped(fields.ok_or(Refusal::Malformed)?)?,
    })
}

fn usage(value: Option<&Json>) -> Result<Usage, Refusal> {
    let empty = JsonObject::default();
    let record = value.and_then(Json::as_object).unwrap_or(&empty);
    let cost = match record.get("cost_usd") {
        Some(Json::Float(float)) => Cost::of(*float),
        Some(Json::Int(integer)) if integer.is_negative() => Cost::ZERO,
        // Python raises `OverflowError` for an integer past the range of a
        // float.
        Some(Json::Int(integer)) => Cost::of(integer.to_f64().ok_or(Refusal::Malformed)?),
        _ => Cost::ZERO,
    };

    Ok(Usage {
        input: count_or(record, "input", 0),
        output: count_or(record, "output", 0),
        cache_read: count_or(record, "cache_read", 0),
        cache_write: count_or(record, "cache_write", 0),
        cost_usd: cost,
    })
}

fn settled(record: &JsonObject) -> Result<SettledLine, Refusal> {
    let (session, turn, turn_seq) = turn_fields(record)?;

    Ok(SettledLine {
        session,
        turn,
        turn_seq,
        resident: flag(record, "resident"),
        usage: usage(record.get("usage"))?,
        user_entry_id: text_of(record, "user_entry_id"),
        leaf_id: text_of(record, "leaf_id"),
        entry_count: count_of(record, "entry_count"),
        settled_ms: count_or(record, "settled_ms", 0),
    })
}

fn failed(record: &JsonObject) -> Result<FailedLine, Refusal> {
    let (session, turn, turn_seq) = turn_fields(record)?;
    let reason = text_of(record, "reason").ok_or(Refusal::Malformed)?;

    // An unknown reason is still a failed turn. The Python host reads it as
    // `internal`, the conservative outcome of §5.3.
    let reason = reason.as_str().and_then(FailReason::from_wire);

    Ok(FailedLine {
        session,
        turn,
        turn_seq,
        reason: reason.unwrap_or(FailReason::Internal),
        message: message_of(record),
    })
}

fn process_exit(record: &JsonObject) -> Result<ProcessExitLine, Refusal> {
    let reason = text_of(record, "reason").filter(|reason| !reason.is_empty());

    Ok(ProcessExitLine {
        session: text_of(record, "session").ok_or(Refusal::Malformed)?,
        code: record.get("code").and_then(Json::as_integer).cloned(),
        reason: reason.map_or(Claimed::Known(ExitReason::Crashed), Claimed::of),
        turn: text_of(record, "turn"),
    })
}

fn pong(record: &JsonObject) -> Result<PongLine, Refusal> {
    Ok(PongLine {
        nonce: text_of(record, "nonce").ok_or(Refusal::Malformed)?,
    })
}

fn log(record: &JsonObject) -> LogLine {
    let level = text_of(record, "level").filter(|level| !level.is_empty());

    LogLine {
        level: level.map_or(Claimed::Known(LogLevel::Info), Claimed::of),
        message: message_of(record),
        session: text_of(record, "session"),
    }
}

/// One item of `entries`. `None` for an item with no usable id: it is no
/// cursor, so the host drops it.
fn entry(item: &Json) -> Option<PiEntry> {
    let record = item.as_object()?;

    Some(PiEntry {
        id: text_of(record, "id")?,
        role: Claimed::of(text_or_empty(record, "role")),
        text: text_or_empty(record, "text").truncated(MAX_ENTRY_CHARS),
    })
}

fn entries(record: &JsonObject) -> Result<EntriesLine, Refusal> {
    let request = text_of(record, "request").ok_or(Refusal::Malformed)?;
    let session = text_of(record, "session").ok_or(Refusal::Malformed)?;
    let items = record.get("entries").and_then(Json::as_array);

    // The cap counts the items of the line, before the host drops one.
    let read = items.map(|items| {
        let first = items.iter().take(MAX_ENTRIES_PER_READ);

        first.filter_map(entry).collect()
    });

    Ok(EntriesLine {
        request,
        session,
        ok: flag(record, "ok"),
        entries: read.unwrap_or_default(),
        since_matched: flag(record, "since_matched"),
        truncated: flag(record, "truncated"),
        leaf_id: text_of(record, "leaf_id"),
        reason: short_text(record, "reason").map(Claimed::of),
    })
}

fn fatal(record: &JsonObject) -> FatalLine {
    let known = FatalClaim::ControlMountUnwritable;
    let reason = text_of(record, "reason").filter(|reason| reason == known.as_str());

    FatalLine {
        reason: reason.map_or(FatalClaim::Unknown, |_| known),
        message: message_of(record),
    }
}

#[cfg(test)]
pub(super) mod tests {
    use serde_json::{Map, Value, json};

    use super::*;
    use crate::vectors::{self, Outcome};

    const SURFACE: &str = "channel.parse";

    /// A text as a vector file writes it.
    pub(in super::super) fn text(text: &Text) -> Value {
        match text.as_str() {
            Some(plain) => json!(plain),
            None => json!({"$utf16": text.to_utf16()}),
        }
    }

    /// An integer as a vector file writes it: a marker past 64 bits.
    fn integer(integer: &Integer) -> Value {
        if let Some(small) = integer.to_i64() {
            return json!(small);
        }

        match integer.to_u64() {
            Some(large) => json!(large),
            None => json!({"$int": integer.as_str()}),
        }
    }

    /// A float as a vector file writes it: a marker when it is not finite.
    fn float(float: f64) -> Value {
        if float.is_nan() {
            return json!({"$float": "NaN"});
        }

        if float.is_infinite() {
            let name = if float > 0.0 { "Infinity" } else { "-Infinity" };
            return json!({"$float": name});
        }

        json!(float)
    }

    fn object(fields: &JsonObject) -> Value {
        let fields = fields.iter().map(|(key, value)| {
            let key = key.as_str().expect("an event holds no lone surrogate");

            (key.to_owned(), value_of(value))
        });

        Value::Object(fields.collect::<Map<String, Value>>())
    }

    /// A value of an event as a vector file writes it.
    fn value_of(value: &Json) -> Value {
        match value {
            Json::Null => Value::Null,
            Json::Bool(flag) => json!(flag),
            Json::Int(number) => integer(number),
            Json::Float(number) => float(*number),
            Json::Text(string) => text(string),
            Json::Array(items) => items.iter().map(value_of).collect(),
            Json::Object(fields) => object(fields),
        }
    }

    fn count(count: &Count) -> Value {
        integer(count.as_integer())
    }

    fn seq(seq: &TurnSeq) -> Value {
        count(seq.as_count())
    }

    fn option<T>(value: Option<&T>, write: fn(&T) -> Value) -> Value {
        value.map_or(Value::Null, write)
    }

    fn claimed<T: Word>(claimed: &Claimed<T>) -> Value {
        text(&claimed.to_text())
    }

    /// One line as the vector file writes it: the name of the Python class
    /// and the fields of the value.
    pub(in super::super) fn normalized(line: &PlaypenLine) -> Value {
        let (kind, message) = match line {
            PlaypenLine::Ready(line) => (
                "Ready",
                json!({
                    "protocol": text(line.protocol()),
                    "sandbox": text(line.sandbox()),
                    "playpen": text(line.playpen()),
                    "pi": text(line.pi()),
                    "node": text(line.node()),
                    "max_resident_processes": count(line.max_resident_processes()),
                    "foreign_pi_processes": count(line.foreign_pi_processes()),
                    "caps": line.caps().iter().map(text).collect::<Vec<Value>>(),
                }),
            ),
            PlaypenLine::Opened(line) => (
                "OpenedLine",
                json!({
                    "session": text(line.session()),
                    "resident": line.resident(),
                    "reason": option(line.reason(), claimed),
                    "message": text(line.message()),
                }),
            ),
            PlaypenLine::Event(line) => (
                "EventLine",
                json!({
                    "session": text(line.session()),
                    "turn": text(line.turn()),
                    "turn_seq": seq(line.turn_seq()),
                    "event": object(line.event().fields()),
                }),
            ),
            PlaypenLine::Settled(line) => (
                "SettledLine",
                json!({
                    "session": text(line.session()),
                    "turn": text(line.turn()),
                    "turn_seq": seq(line.turn_seq()),
                    "resident": line.resident(),
                    "usage": {
                        "input": count(line.usage().input()),
                        "output": count(line.usage().output()),
                        "cache_read": count(line.usage().cache_read()),
                        "cache_write": count(line.usage().cache_write()),
                        "cost_usd": float(line.usage().cost_usd().get()),
                    },
                    "user_entry_id": option(line.user_entry_id(), text),
                    "leaf_id": option(line.leaf_id(), text),
                    "entry_count": option(line.entry_count(), count),
                    "settled_ms": count(line.settled_ms()),
                }),
            ),
            PlaypenLine::Failed(line) => (
                "FailedLine",
                json!({
                    "session": text(line.session()),
                    "turn": text(line.turn()),
                    "turn_seq": seq(line.turn_seq()),
                    "reason": line.reason().as_str(),
                    "message": text(line.message()),
                }),
            ),
            PlaypenLine::ProcessExit(line) => (
                "ProcessExitLine",
                json!({
                    "session": text(line.session()),
                    "code": option(line.code(), integer),
                    "reason": claimed(line.reason()),
                    "turn": option(line.turn(), text),
                }),
            ),
            PlaypenLine::Pong(line) => ("PongLine", json!({"nonce": text(line.nonce())})),
            PlaypenLine::Log(line) => (
                "LogLine",
                json!({
                    "level": claimed(line.level()),
                    "message": text(line.message()),
                    "session": option(line.session(), text),
                }),
            ),
            PlaypenLine::Entries(line) => (
                "EntriesLine",
                json!({
                    "request": text(line.request()),
                    "session": text(line.session()),
                    "ok": line.ok(),
                    "entries": line.entries().iter().map(|entry| json!({
                        "id": text(entry.id()),
                        "role": claimed(entry.role()),
                        "text": text(entry.text()),
                    })).collect::<Vec<Value>>(),
                    "since_matched": line.since_matched(),
                    "truncated": line.truncated(),
                    "leaf_id": option(line.leaf_id(), text),
                    "reason": option(line.reason(), claimed),
                }),
            ),
            PlaypenLine::Fatal(line) => (
                "FatalLine",
                json!({
                    "reason": line.reason().as_str(),
                    "message": text(line.message()),
                }),
            ),
        };

        json!({"kind": kind, "message": message})
    }

    /// Whether two values of a vector are the same. An integer is not a
    /// float, and `-0.0` is not `0.0`.
    fn same(left: &Value, right: &Value) -> bool {
        match (left, right) {
            (Value::Number(left), Value::Number(right)) if left.is_f64() || right.is_f64() => {
                let bits = |number: &serde_json::Number| number.as_f64().map(f64::to_bits);

                left.is_f64() && right.is_f64() && bits(left) == bits(right)
            }
            (Value::Array(left), Value::Array(right)) => {
                left.len() == right.len() && left.iter().zip(right).all(|(a, b)| same(a, b))
            }
            (Value::Object(left), Value::Object(right)) => {
                left.len() == right.len()
                    && left
                        .iter()
                        .all(|(key, a)| right.get(key).is_some_and(|b| same(a, b)))
            }
            _ => left == right,
        }
    }

    #[test]
    fn two_values_are_the_same_only_with_the_same_number_types() {
        assert!(same(
            &json!({"a": [1, 2.0, "x"]}),
            &json!({"a": [1, 2.0, "x"]})
        ));
        assert!(!same(&json!(2), &json!(2.0)));
        assert!(!same(&json!(0.0), &json!(-0.0)));
        assert!(!same(&json!([1]), &json!([1, 2])));
        assert!(!same(&json!({"a": 1}), &json!({"b": 1})));
        assert!(!same(&json!({"a": 1}), &json!({"a": 1, "b": 1})));
    }

    #[test]
    fn the_parser_does_with_each_line_what_the_python_host_does() {
        let surface = vectors::surface(SURFACE);
        let mut accepted = 0;

        assert_eq!(surface.entry, "attendance.wire.parse");
        for vector in &surface.vectors {
            let id = &vector.id;
            let input = vector
                .input
                .text()
                .unwrap_or_else(|| panic!("{id}: no text"));
            let parsed = parse(&input);

            match vector.result {
                Outcome::Accepted => {
                    let line = parsed.unwrap_or_else(|refusal| panic!("{id}: {refusal}"));
                    let expected = vector.value().unwrap_or_else(|| panic!("{id}: no value"));

                    assert!(same(&normalized(&line), expected), "{id}: {line:?}");
                    accepted += 1;
                }
                Outcome::Refused => {
                    let refusal = parsed.err().unwrap_or_else(|| panic!("{id}: accepted"));

                    assert_eq!(Some(&json!(refusal.as_str())), vector.refusal(), "{id}");
                }
                // The Python host raised. The parser refuses.
                Outcome::Raised => assert!(parsed.is_err(), "{id}"),
            }
        }

        assert!(accepted > 0, "{SURFACE} holds no accepted vector");
    }

    fn line(text: &str) -> PlaypenLine {
        parse(text).unwrap_or_else(|refusal| panic!("{text}: {refusal}"))
    }

    const EVENT_HEAD: &str = r#"{"type":"event","session":"tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK","turn":"01JBQ7WZ0X4T9V6K2H8M3N5PQR","turn_seq":1,"event":"#;

    /// The refusal of a line with this event.
    fn event_refusal(event: &str) -> Option<Refusal> {
        parse(&format!("{EVENT_HEAD}{event}}}")).err()
    }

    /// The line with this event.
    fn event_line(event: &str) -> EventLine {
        match line(&format!("{EVENT_HEAD}{event}}}")) {
            PlaypenLine::Event(line) => line,
            other => panic!("{other:?} is no event"),
        }
    }

    #[test]
    fn a_line_that_nests_deep_is_refused_and_does_not_exhaust_the_stack() {
        let nested = |levels: usize| format!("{}1{}", "[".repeat(levels), "]".repeat(levels));

        // The line object and the event are two levels above the arrays.
        let deepest = event_line(&format!(r#"{{"type":"x","a":{}}}"#, nested(8998)));
        assert!(deepest.event().is_truncated());
        assert_eq!(
            event_refusal(&format!(r#"{{"type":"x","a":{}}}"#, nested(8999))),
            Some(Refusal::Malformed)
        );

        for levels in [9001, 400_000, 520_000] {
            assert_eq!(parse(&nested(levels)), Err(Refusal::Malformed), "{levels}");
            assert_eq!(
                parse(&"[".repeat(levels)),
                Err(Refusal::Malformed),
                "{levels}"
            );
            assert_eq!(
                parse(&"{\"a\":".repeat(levels)),
                Err(Refusal::Malformed),
                "{levels}"
            );
        }

        assert_eq!(parse(&nested(9000)), Err(Refusal::NotObject));
    }

    #[test]
    fn no_cut_and_no_changed_byte_of_a_line_stops_the_parser() {
        let lines = [
            format!("{EVENT_HEAD}{{\"type\":\"x\",\"a\":[1,-2.5e3,true,null,\"\\u00e9\\ud83d\"]}}}}"),
            r#"{"type":"turn_settled","session":"s","turn":"t","turn_seq":1,"usage":{"cost_usd":1e3}}"#
                .to_owned(),
            r#"{"type":"entries","request":"r","session":"s","entries":[{"id":"e","text":"x"}]}"#
                .to_owned(),
        ];
        let bytes = [
            b'"', b'\\', b'{', b'}', b'[', b']', b',', b':', b'-', b'e', b'9', b'u', 0,
        ];
        let mut read = 0;

        for line in &lines {
            for cut in 0..=line.len() {
                read += usize::from(line.get(..cut).is_some_and(|text| parse(text).is_ok()));
            }

            for at in 0..line.len() {
                for byte in bytes {
                    let mut changed = line.clone().into_bytes();
                    changed[at] = byte;
                    let Ok(text) = String::from_utf8(changed) else {
                        continue;
                    };

                    read += usize::from(parse(&text).is_ok());
                }
            }
        }

        assert!(read > lines.len());
    }

    #[test]
    fn a_huge_number_is_refused_or_kept_as_the_python_host_does() {
        let digits = "9".repeat(4301);
        let pong = |field: &str| format!(r#"{{"type":"pong","nonce":"x","n":{field}}}"#);
        let cost = |cost: &str| {
            let text = format!(
                r#"{{"type":"turn_settled","session":"s","turn":"t","turn_seq":1,"usage":{{"cost_usd":{cost}}}}}"#
            );

            parse(&text).map(|line| match line {
                PlaypenLine::Settled(line) => line.usage().cost_usd().get(),
                other => panic!("{other:?} is no settled line"),
            })
        };

        assert_eq!(parse(&pong(&digits)), Err(Refusal::Malformed));
        assert_eq!(parse(&pong(&format!("-{digits}"))), Err(Refusal::Malformed));
        assert!(parse(&pong(&"9".repeat(4300))).is_ok());
        assert!(parse(&pong(&format!("{digits}.5"))).is_ok());
        assert!(parse(&pong("1e99999")).is_ok());

        // An integer past the range of a float is `OverflowError` in Python.
        assert_eq!(cost(&format!("1{}", "0".repeat(308))), Ok(1e308));
        assert_eq!(
            cost(&format!("1{}", "0".repeat(309))),
            Err(Refusal::Malformed)
        );
        assert_eq!(cost(&format!("-1{}", "0".repeat(309))), Ok(0.0));
        assert_eq!(cost("9007199254740993"), Ok(9_007_199_254_740_992.0));
        assert_eq!(cost("1e999"), Ok(f64::INFINITY));
        assert_eq!(cost("-1e999"), Ok(0.0));
        assert_eq!(cost("-0.0").map(f64::to_bits), Ok((-0.0_f64).to_bits()));
        assert!(cost("NaN").unwrap().is_nan());
    }

    #[test]
    fn a_lone_surrogate_stays_in_a_text_and_refuses_an_event() {
        let PlaypenLine::Log(log) = line(r#"{"type":"log","message":"a\ud800b"}"#) else {
            panic!("no log line");
        };

        assert_eq!(log.message().as_str(), None);
        assert_eq!(log.message().to_utf16(), vec![0x61, 0xd800, 0x62]);
        assert_eq!(log.message().to_string_lossy(), "a\u{fffd}b");
        assert_eq!(
            event_refusal(r#"{"type":"x","text":"\ud800"}"#),
            Some(Refusal::Malformed)
        );
        assert_eq!(event_refusal(r#"{"\udc00":1}"#), Some(Refusal::Malformed));
        assert_eq!(
            parse(r#"{"type":"pon\ud800g","nonce":"x"}"#),
            Err(Refusal::UnknownType)
        );
        assert_eq!(event_refusal(r#"{"text":"\ud83d\ude00"}"#), None);
    }

    #[test]
    fn an_event_over_a_limit_keeps_its_type_only() {
        let fill = "a".repeat(MAX_EVENT_BYTES);
        let large = event_line(&format!(r#"{{"type":"big","text":"{fill}"}}"#));
        let small = event_line(r#"{"type":"small","n":[1.0,1e100]}"#);
        let untyped = event_line(&format!(r#"{{"type":"","text":"{fill}"}}"#));

        assert!(large.event().is_truncated());
        assert_eq!(large.event().kind(), Some(&Text::from("big")));
        assert_eq!(
            large.event().to_json(),
            format!(
                r#"{{"type":"big","truncated":true,"original_bytes":{}}}"#,
                MAX_EVENT_BYTES + 24
            )
        );
        assert!(!small.event().is_truncated());
        assert_eq!(
            small.event().to_json(),
            r#"{"type":"small","n":[1.0,1e+100]}"#
        );
        assert_eq!(small.event().fields().len(), 2);
        assert_eq!(untyped.event().kind(), Some(&Text::from("unknown")));
    }

    #[test]
    fn the_address_of_a_line_is_two_ids_or_unknown() {
        let good = event_line("{}");
        let address = good.address().unwrap();
        let PlaypenLine::Failed(bad) = line(
            r#"{"type":"turn_failed","session":"../x","turn":"t","turn_seq":2,"reason":"new"}"#,
        ) else {
            panic!("no failed line");
        };

        assert_eq!(address.session().as_str(), "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK");
        assert_eq!(address.turn().as_str(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR");
        assert_eq!(
            address,
            TurnAddress::new(address.session().clone(), address.turn().clone())
        );
        assert_eq!(good.turn_seq().to_u64(), Some(1));
        assert_eq!(bad.address(), Err(Refusal::UnknownAddress));
        assert_eq!(bad.reason(), FailReason::Internal);
        assert_eq!(bad.session(), &"../x");
        assert_eq!(bad.turn(), &"t");
    }

    #[test]
    fn ready_gives_its_major_version_and_its_caps() {
        let PlaypenLine::Ready(ready) = line(
            r#"{"type":"ready","protocol":"1.4","sandbox":"chat-s1","caps":["get_entries","new"]}"#,
        ) else {
            panic!("no ready line");
        };

        assert_eq!(ready.major(), "1");
        assert_eq!(ready.sandbox(), &"chat-s1");
        assert!(ready.supports(Cap::GetEntries));
        assert!(!ready.supports(Cap::Steer));
        assert_eq!(ready.max_resident_processes().to_u64(), Some(12));
    }

    #[test]
    fn a_claimed_name_is_known_or_kept_as_text() {
        let PlaypenLine::Log(log) = line(r#"{"type":"log","level":"LOUD"}"#) else {
            panic!("no log line");
        };
        let PlaypenLine::ProcessExit(exit) = line(r#"{"type":"process_exit","session":"s"}"#)
        else {
            panic!("no exit line");
        };
        let PlaypenLine::Fatal(fatal) = line(r#"{"type":"fatal","reason":"mount_dir_unset"}"#)
        else {
            panic!("no fatal line");
        };

        assert_eq!(log.level().known(), None);
        assert_eq!(log.level().to_text(), "LOUD");
        assert_eq!(exit.reason().known(), Some(ExitReason::Crashed));
        assert_eq!(exit.reason().to_text(), "crashed");
        assert_eq!(fatal.reason(), FatalClaim::Unknown);
    }
}
