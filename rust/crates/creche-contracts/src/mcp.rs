//! The words of the MCP wire: one source for a client and for each server.
//!
//! Each word here comes from one of two public specifications:
//!
//! - JSON-RPC 2.0 gives the form of a message and five error codes.
//! - The Model Context Protocol (MCP) builds on JSON-RPC 2.0. It gives the
//!   id of a request, the handshake, the methods and their results.
//!
//! The module names no server and no tool of a deployment, and it has no
//! size limit. The caller gives the cap of each text to the reader.
//!
//! # How a message reads
//!
//! [`Line::parse`] reads one message. The strict reader of
//! [`json`](crate::json) checks the bytes and fills a raw type. One
//! conversion then makes the [`Line`]. The reader refuses a message with a
//! member that JSON-RPC 2.0 does not name.
//!
//! Some members hold what the two specifications leave open, for example the
//! arguments of a call. Such a member is an [`Object`]: the module keeps it
//! whole and reads no member of it.
//!
//! A result is an [`Object`] too, because only its request says what a
//! result holds. The caller finds the request of the id. It then reads the
//! members of the result.
//!
//! # How a message writes
//!
//! Each type of a message implements `Serialize`. Give the value to
//! [`json::write`](crate::json::write) with the style of the transport. The
//! stdio transport of MCP puts each message on a line of its own. For that
//! transport, select `Layout::Compact` or `Layout::Spaced`: each other
//! layout puts a line feed into the text.
//!
//! ```
//! use creche_contracts::json::{self, ByteCap, Charset, KeyOrder, Layout, Style};
//! use creche_contracts::mcp::{Line, Method, Object, RequestId};
//!
//! const CAP: ByteCap = ByteCap::new(4096);
//! const STYLE: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven);
//!
//! // A server reads a request and answers it with the same id.
//! let asked = br#"{"jsonrpc":"2.0","id":7,"method":"tools/list"}"#;
//! let Line::Request { id, method, .. } = Line::parse(asked, CAP)? else {
//!     panic!("the text is a request");
//! };
//! assert_eq!(Method::of_name(&method), Some(Method::ToolsList));
//!
//! let result = Object::read(br#"{"tools":[]}"#, CAP)?;
//! let text = json::write(&Line::Result { id, result }, STYLE)?;
//! assert_eq!(text, br#"{"jsonrpc":"2.0","id":7,"result":{"tools":[]}}"#);
//!
//! // The client reads the answer and finds its request by the id.
//! let Line::Result { id, .. } = Line::parse(&text, CAP)? else {
//!     panic!("the text is a result");
//! };
//! assert_eq!(id, RequestId::from(7));
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```

use std::error::Error;
use std::fmt;

use serde::de::DeserializeOwned;
use serde::ser::SerializeMap;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::json::{
    self, ByteCap, Charset, Found, Integer, KeyOrder, Layout, NotStrict, Opaque, ReadError, Shape,
    Style, WriteError,
};
use crate::slot::{Nested, Slot};

/// The value of the member `jsonrpc` of each message (JSON-RPC 2.0,
/// sections 4 and 5).
pub const JSONRPC_VERSION: &str = "2.0";

/// The style of the text that the module writes to read a value again.
const COMPACT: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven);

// The members of a message. The writer and the errors name each one.
const JSONRPC: &str = "jsonrpc";
const ID: &str = "id";
const METHOD: &str = "method";
const PARAMS: &str = "params";
const RESULT: &str = "result";
const ERROR: &str = "error";
const CODE: &str = "code";
const MESSAGE: &str = "message";

/// An error code that JSON-RPC 2.0 defines (section 5.1).
///
/// The set is closed, and a code crosses a process boundary. A peer can
/// send a number outside the set. The specification lets a server define
/// the codes from -32099 to -32000. It lets an application define each code
/// outside the range from -32768 to -32000. A reader does not refuse such a
/// number. [`ErrorCode::of_number`] gives `None` for it, and
/// [`Line::Error`] keeps the number.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorCode {
    /// -32700: the text is not JSON.
    ParseError,
    /// -32600: the JSON value is not a request.
    InvalidRequest,
    /// -32601: the peer has no such method.
    MethodNotFound,
    /// -32602: the parameters of the method are not valid.
    InvalidParams,
    /// -32603: a fault inside the peer.
    InternalError,
}

impl ErrorCode {
    /// Each code, in the order of the variants.
    const ALL: [Self; 5] = [
        Self::ParseError,
        Self::InvalidRequest,
        Self::MethodNotFound,
        Self::InvalidParams,
        Self::InternalError,
    ];

    /// The number of the code on the wire.
    #[must_use]
    pub const fn number(self) -> i64 {
        match self {
            Self::ParseError => -32700,
            Self::InvalidRequest => -32600,
            Self::MethodNotFound => -32601,
            Self::InvalidParams => -32602,
            Self::InternalError => -32603,
        }
    }

    /// The code that has this number. `None` for each other number.
    ///
    /// ```
    /// use creche_contracts::json::Integer;
    /// use creche_contracts::mcp::ErrorCode;
    ///
    /// let code = ErrorCode::of_number(Integer::from(-32601_i64));
    /// assert_eq!(code, Some(ErrorCode::MethodNotFound));
    /// assert_eq!(ErrorCode::of_number(Integer::from(-32000_i64)), None);
    /// ```
    #[must_use]
    pub fn of_number(number: Integer) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|code| Integer::from(*code) == number)
    }
}

impl From<ErrorCode> for Integer {
    fn from(code: ErrorCode) -> Self {
        Self::from(code.number())
    }
}

/// A method that a server of the platform answers.
///
/// The set is closed, and a name crosses a process boundary. MCP defines
/// more methods, and a peer can send each one. A reader does not refuse
/// another name. [`Method::of_name`] gives `None` for it, and
/// [`Line::Request`] keeps the text. A server answers such a request with
/// [`ErrorCode::MethodNotFound`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Method {
    /// `initialize`: the handshake. It is the first request of a session.
    Initialize,
    /// `tools/list`: the tools that the server has.
    ToolsList,
    /// `tools/call`: one call of one tool.
    ToolsCall,
}

impl Method {
    /// Each method, in the order of the variants.
    const ALL: [Self; 3] = [Self::Initialize, Self::ToolsList, Self::ToolsCall];

    /// The name of the method on the wire.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Initialize => "initialize",
            Self::ToolsList => "tools/list",
            Self::ToolsCall => "tools/call",
        }
    }

    /// The method that has this name. `None` for each other text. The
    /// function takes no other letter case and no white space.
    #[must_use]
    pub fn of_name(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|method| method.name() == name)
    }
}

/// A revision of MCP that the platform accepts in a handshake.
///
/// A revision is a date, and the set has no order. Code asks if a text is a
/// member of the set. It never compares two revisions, so the type has no
/// `Ord`.
///
/// The set is closed, and a revision crosses a process boundary.
/// [`ProtocolVersion::of_text`] gives `None` for each other text. What a
/// reader then does depends on its side of the handshake:
///
/// - A server reads the revision that a client asks for. For a text outside
///   the set, it answers with [`ProtocolVersion::OFFERED`].
///   [`ProtocolVersion::agreed`] holds that rule.
/// - A client reads the revision that the server answers with. For a text
///   outside the set, the client refuses the result and ends the session.
///
/// ```
/// use creche_contracts::mcp::ProtocolVersion;
///
/// assert_eq!(ProtocolVersion::OFFERED.text(), "2025-11-25");
/// assert_eq!(ProtocolVersion::agreed("2024-11-05").text(), "2024-11-05");
/// assert_eq!(ProtocolVersion::agreed("1999-01-01"), ProtocolVersion::OFFERED);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ProtocolVersion {
    /// The revision `2025-11-25`.
    V2025_11_25,
    /// The revision `2025-06-18`.
    V2025_06_18,
    /// The revision `2025-03-26`.
    V2025_03_26,
    /// The revision `2024-11-05`.
    V2024_11_05,
}

impl ProtocolVersion {
    /// The revision that the platform speaks. A client asks for it in
    /// `initialize`. A server answers with it when the client asks for a
    /// revision outside the set.
    pub const OFFERED: Self = Self::V2025_11_25;

    /// Each revision of the set.
    const ACCEPTED: [Self; 4] = [
        Self::V2025_11_25,
        Self::V2025_06_18,
        Self::V2025_03_26,
        Self::V2024_11_05,
    ];

    /// The text of the revision on the wire.
    #[must_use]
    pub const fn text(self) -> &'static str {
        match self {
            Self::V2025_11_25 => "2025-11-25",
            Self::V2025_06_18 => "2025-06-18",
            Self::V2025_03_26 => "2025-03-26",
            Self::V2024_11_05 => "2024-11-05",
        }
    }

    /// The revision that has this text. `None` for each other text.
    #[must_use]
    pub fn of_text(text: &str) -> Option<Self> {
        Self::ACCEPTED
            .into_iter()
            .find(|revision| revision.text() == text)
    }

    /// The revision that a server answers with, for the text that a client
    /// asks for. The answer is the same revision when the set holds it. For
    /// each other text, the answer is [`ProtocolVersion::OFFERED`].
    #[must_use]
    pub fn agreed(asked: &str) -> Self {
        Self::of_text(asked).unwrap_or(Self::OFFERED)
    }
}

impl Serialize for ProtocolVersion {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.text())
    }
}

/// The id of a request: a text or an integer (MCP, base protocol).
///
/// JSON-RPC 2.0 also permits `null` and a number with a fraction. MCP
/// permits neither one, and the reader refuses both. Each text is an id,
/// and so is each integer that the strict reader takes.
// CONTRACT-QUESTION: JSON-RPC 2.0 section 4 and MCP say "integer" for an id
// and for an error code. Neither one says which JSON tokens are an integer.
// The reading here takes a token with no fraction and no exponent only, as
// `json::Integer` does. The reader thus refuses `1.0` and `1e0`. A change
// costs a read through `json::Number` and one check for a whole value.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum RequestId {
    /// A text. The empty text is an id too.
    Text(String),
    /// An integer in the range of 64 bits.
    Integer(Integer),
}

impl From<u64> for RequestId {
    fn from(id: u64) -> Self {
        Self::Integer(Integer::from(id))
    }
}

impl From<&str> for RequestId {
    fn from(id: &str) -> Self {
        Self::Text(id.to_owned())
    }
}

impl Serialize for RequestId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Text(text) => serializer.serialize_str(text),
            Self::Integer(integer) => integer.serialize(serializer),
        }
    }
}

/// The id member of a raw message. A value of another kind is `Other` with
/// that kind, so the read fails for no value.
impl<'de> Deserialize<'de> for Slot<RequestId> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = match Slot::<Opaque>::deserialize(deserializer)? {
            Slot::Value(value) => value,
            Slot::Missing => return Ok(Self::Missing),
            Slot::Null => return Ok(Self::Null),
            Slot::Other(found) => return Ok(Self::Other(found)),
        };
        let id = match value.kind() {
            Found::Text => reread(&value).map(RequestId::Text).ok(),
            Found::Integer => reread(&value).map(RequestId::Integer).ok(),
            Found::Null | Found::Boolean | Found::Float | Found::List | Found::Table => None,
        };

        Ok(id.map_or(Self::Other(value.kind()), Self::Value))
    }
}

/// Reads `value` again as a `T`.
///
/// An [`Opaque`] gives no member of its value. The function thus writes the
/// value in its compact form and gives that text to the strict reader. The
/// writer holds each rule of the reader, so the check accepts the text.
fn reread<T: DeserializeOwned>(value: &Opaque) -> Result<T, WireError> {
    let text = json::write(value, COMPACT).map_err(WireError::NoJsonForm)?;

    Ok(json::read(&text, ByteCap::new(text.len()))?)
}

/// A JSON object that the module keeps whole and does not read.
///
/// The parameters of a request and the result of a request are such an
/// object. So is each member that the two specifications leave open. Three
/// examples are the capabilities of a server, the input schema of a tool
/// and the arguments of a tool call. `rust/AGENTS.md`, rule 7, calls such a
/// member opaque.
///
/// A value exists only through [`Object::read`], [`Object::of`] and
/// `TryFrom<Opaque>`. Each one refuses a JSON value of another kind.
/// [`Object::parse`] gives the members to a raw type of the caller.
///
/// ```
/// use creche_contracts::json::{ByteCap, Opaque};
/// use creche_contracts::mcp::{Object, WireError};
/// use serde::Serialize;
///
/// #[derive(Serialize)]
/// struct Call {
///     name: &'static str,
/// }
///
/// let params = Object::of(&Call { name: "add" })?;
/// assert_eq!(params, Object::read(br#" {"name": "add"} "#, ByteCap::new(64))?);
///
/// let list = Opaque::read(b"[1]", ByteCap::new(64)).unwrap();
/// assert!(matches!(Object::try_from(list), Err(WireError::NotObject(_))));
/// # Ok::<(), WireError>(())
/// ```
///
/// Code outside this module cannot make an object from a value of another
/// kind:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{ByteCap, Opaque};
/// use creche_contracts::mcp::{Object, WireError};
///
/// let list = Opaque::read(b"[1]", ByteCap::new(64)).unwrap();
/// let params = Object { value: list };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct Object {
    /// A value of the kind `Found::Table`.
    value: Opaque,
}

impl Object {
    /// The object that `bytes` hold, through the strict reader with the cap
    /// of the surface.
    ///
    /// # Errors
    ///
    /// [`WireError::NotStrict`] for a text that is not strict JSON, and
    /// [`WireError::NotObject`] for a value of another kind.
    pub fn read(bytes: &[u8], cap: ByteCap) -> Result<Self, WireError> {
        Self::try_from(Opaque::read(bytes, cap)?)
    }

    /// The object that `value` writes: the parameters or the result that a
    /// typed value stands for.
    ///
    /// # Errors
    ///
    /// [`WireError::NoJsonForm`] for a value that the writer refuses, and
    /// [`WireError::NotObject`] for a value that writes no object.
    pub fn of<T: Serialize + ?Sized>(value: &T) -> Result<Self, WireError> {
        let text = json::write(value, COMPACT).map_err(WireError::NoJsonForm)?;

        Self::read(&text, ByteCap::new(text.len()))
    }

    /// The members of the object as a `T`. A caller reads the parameters of
    /// a request in this way, with a raw type of its own.
    ///
    /// # Errors
    ///
    /// [`WireError::Shape`] when `T` does not take the object.
    pub fn parse<T: DeserializeOwned>(&self) -> Result<T, WireError> {
        reread(&self.value)
    }

    /// The object as the opaque value that it is.
    #[must_use]
    pub const fn as_opaque(&self) -> &Opaque {
        &self.value
    }
}

impl TryFrom<Opaque> for Object {
    type Error = WireError;

    fn try_from(value: Opaque) -> Result<Self, WireError> {
        match value.kind() {
            Found::Table => Ok(Self { value }),
            other => Err(WireError::NotObject(other)),
        }
    }
}

/// Why the module gives no value for a text or for an object.
///
/// No variant holds a byte of the text, so a log line can print the error.
/// A member has its name of the specification, for example `params`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WireError {
    /// The text is not strict JSON.
    NotStrict(NotStrict),
    /// The text is strict JSON, and a raw type does not take its value. A
    /// message gives this error for a member that JSON-RPC 2.0 does not
    /// name.
    Shape(Shape),
    /// The value is strict JSON and no object. A list is a batch of
    /// JSON-RPC, and the module reads no batch.
    NotObject(Found),
    /// The writer of [`json`](crate::json) refuses the value.
    NoJsonForm(WriteError),
    /// The object is not one message. A message has exactly one of the
    /// members `method`, `result` and `error`.
    NotOneKind,
    /// A member is not valid, for one of three reasons:
    ///
    /// - The specification requires the member, and the object has none.
    /// - The specification does not permit the value of the member.
    /// - The form of the message does not have such a member.
    Member(&'static str),
}

impl WireError {
    /// The refusal of the strict check, for a text that is not strict JSON.
    /// A service builds the notice for the operator from this value.
    #[must_use]
    pub const fn not_strict(&self) -> Option<&NotStrict> {
        match self {
            Self::NotStrict(refusal) => Some(refusal),
            Self::Shape(_)
            | Self::NotObject(_)
            | Self::NoJsonForm(_)
            | Self::NotOneKind
            | Self::Member(_) => None,
        }
    }
}

impl fmt::Display for WireError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotStrict(refusal) => write!(f, "the text is not strict JSON: {refusal}"),
            Self::Shape(shape) => write!(f, "the JSON value has another shape: {shape}"),
            Self::NotObject(_) => f.write_str("the JSON value is not an object"),
            Self::NoJsonForm(refusal) => write!(f, "the value has no JSON text: {refusal}"),
            Self::NotOneKind => f.write_str("the object is not one message"),
            Self::Member(name) => write!(f, "the member `{name}` is not valid"),
        }
    }
}

impl Error for WireError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::NotStrict(refusal) => Some(refusal),
            Self::Shape(shape) => Some(shape),
            Self::NoJsonForm(refusal) => Some(refusal),
            Self::NotObject(_) | Self::NotOneKind | Self::Member(_) => None,
        }
    }
}

impl From<ReadError> for WireError {
    fn from(error: ReadError) -> Self {
        match error {
            ReadError::NotStrict(refusal) => Self::NotStrict(refusal),
            ReadError::Shape(shape) => Self::Shape(shape),
        }
    }
}

/// One message of JSON-RPC 2.0, as MCP narrows it.
///
/// [`Line::parse`] refuses each text that is not exactly one of the four
/// forms. It makes these checks in this sequence, and gives the first
/// failure:
///
/// 1. The text is strict JSON under the cap of the caller.
/// 2. The value is an object. A list is a batch, and the reader refuses it.
/// 3. Each member has a name that JSON-RPC 2.0 gives a message. The same
///    applies to the members of the error object.
/// 4. `jsonrpc` is the text `2.0`.
/// 5. The object has exactly one of `method`, `result` and `error`.
/// 6. Each member of that form has the value that its variant names. A
///    result and an error have no `params`.
///
/// `Serialize` writes the members in the order of the specification, with
/// `jsonrpc` first.
///
/// ```
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{Line, WireError};
///
/// const CAP: ByteCap = ByteCap::new(256);
///
/// let note = Line::parse(br#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#, CAP)?;
/// assert!(matches!(note, Line::Notification { params: None, .. }));
///
/// let batch = Line::parse(b"[]", CAP);
/// assert!(matches!(batch, Err(WireError::NotObject(_))));
///
/// let twice = Line::parse(br#"{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}"#, CAP);
/// assert!(twice.is_err_and(|error| error.not_strict().is_some()));
/// # Ok::<(), WireError>(())
/// ```
// CONTRACT-QUESTION: the revision 2025-03-26 of MCP says that a receiver
// takes a batch, which is a list of messages. Each other revision of
// `ProtocolVersion` has no batch. The reading here is the reading of those
// revisions: the reader refuses a list, also from a peer that speaks
// 2025-03-26. A change costs one more variant of `Line` and the read of
// each item of the list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Line {
    /// A request. The peer answers it with a result or with an error that
    /// has the same id.
    Request {
        /// The id. MCP permits no `null` here.
        id: RequestId,
        /// The name of the method. [`Method::of_name`] reads it.
        method: String,
        /// The parameters. MCP permits an object only, and no list.
        params: Option<Object>,
    },
    /// A notification: a request with no id. The peer sends no answer.
    Notification {
        /// The name of the method.
        method: String,
        /// The parameters, as for a request.
        params: Option<Object>,
    },
    /// The result of a request.
    Result {
        /// The id of the request.
        id: RequestId,
        /// The result. In MCP each result is an object.
        result: Object,
    },
    /// The error of a request.
    Error {
        /// The id of the request. `None` when the peer found no id, for
        /// example in a text that is not JSON.
        id: Option<RequestId>,
        /// The number of the error. [`ErrorCode::of_number`] reads it.
        code: Integer,
        /// A short description of the error.
        message: String,
        /// More about the error, in a form that the sender selects. The
        /// reader gives `None` for the value `null` too.
        data: Option<Opaque>,
    },
}

impl Line {
    /// Reads one message, with the cap of the transport.
    ///
    /// White space around the object is no part of the message, so a text
    /// with the line feed of the stdio transport reads too.
    ///
    /// # Errors
    ///
    /// The first check of the type doc that the text fails.
    pub fn parse(bytes: &[u8], cap: ByteCap) -> Result<Self, WireError> {
        let raw = match json::read::<Slot<RawLine>>(bytes, cap)? {
            Slot::Value(raw) => raw,
            Slot::Other(found) => return Err(WireError::NotObject(found)),
            Slot::Missing | Slot::Null => return Err(WireError::NotObject(Found::Null)),
        };
        if raw.jsonrpc.value().map(String::as_str) != Some(JSONRPC_VERSION) {
            return Err(WireError::Member(JSONRPC));
        }

        match (
            present(&raw.method),
            present(&raw.result),
            present(&raw.error),
        ) {
            (true, false, false) => raw.into_call(),
            (false, true, false) => raw.into_result(),
            (false, false, true) => raw.into_error(),
            _ => Err(WireError::NotOneKind),
        }
    }
}

// CONTRACT-QUESTION: JSON-RPC 2.0 section 5 says that the id of an error is
// `null` when the peer found no id. The schema of MCP 2025-11-25 makes that
// id optional and gives it no `null`. The reader takes both forms. The
// writer writes `null`, the form of JSON-RPC 2.0. A reader that follows
// only the schema of MCP can refuse that form. A change costs one branch of
// this function.
impl Serialize for Line {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut line = serializer.serialize_map(None)?;
        line.serialize_entry(JSONRPC, JSONRPC_VERSION)?;
        match self {
            Self::Request { id, method, params } => {
                line.serialize_entry(ID, id)?;
                line.serialize_entry(METHOD, method)?;
                if let Some(params) = params {
                    line.serialize_entry(PARAMS, params)?;
                }
            }
            Self::Notification { method, params } => {
                line.serialize_entry(METHOD, method)?;
                if let Some(params) = params {
                    line.serialize_entry(PARAMS, params)?;
                }
            }
            Self::Result { id, result } => {
                line.serialize_entry(ID, id)?;
                line.serialize_entry(RESULT, result)?;
            }
            Self::Error {
                id,
                code,
                message,
                data,
            } => {
                line.serialize_entry(ID, id)?;
                line.serialize_entry(
                    ERROR,
                    &ErrorBody {
                        code,
                        message,
                        data,
                    },
                )?;
            }
        }

        line.end()
    }
}

/// The error object of JSON-RPC 2.0, section 5.1, as the writer gives it.
#[derive(Serialize)]
struct ErrorBody<'a> {
    code: &'a Integer,
    message: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: &'a Option<Opaque>,
}

/// The raw form of a message. Each field is a `Slot`, so the read fails for
/// no value of a member. It fails for a member with another name.
// CONTRACT-QUESTION: JSON-RPC 2.0, sections 4, 5 and 5.1, names the members
// of a message and of its error object. It does not say what a receiver
// does with one more member. No revision of `ProtocolVersion` adds a member
// there. The reading here is the strict one: the reader refuses the
// message. A change costs the attribute `deny_unknown_fields` of `RawLine`
// and of `RawError`.
#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct RawLine {
    jsonrpc: Slot<String>,
    id: Slot<RequestId>,
    method: Slot<String>,
    params: Slot<Opaque>,
    result: Slot<Opaque>,
    error: Slot<RawError>,
}

impl Nested for RawLine {}

impl RawLine {
    /// A request, or a notification when the object has no `id`.
    fn into_call(self) -> Result<Line, WireError> {
        let method = required(self.method, METHOD)?;
        let params = optional(self.params, PARAMS)?
            .map(|params| object(params, PARAMS))
            .transpose()?;

        match self.id {
            Slot::Missing => Ok(Line::Notification { method, params }),
            Slot::Value(id) => Ok(Line::Request { id, method, params }),
            Slot::Null | Slot::Other(_) => Err(WireError::Member(ID)),
        }
    }

    fn into_result(self) -> Result<Line, WireError> {
        absent(&self.params, PARAMS)?;
        let id = required(self.id, ID)?;
        let result = object(required(self.result, RESULT)?, RESULT)?;

        Ok(Line::Result { id, result })
    }

    fn into_error(self) -> Result<Line, WireError> {
        absent(&self.params, PARAMS)?;
        let id = match self.id {
            Slot::Missing | Slot::Null => None,
            Slot::Value(id) => Some(id),
            Slot::Other(_) => return Err(WireError::Member(ID)),
        };
        let error = required(self.error, ERROR)?;
        let code = required(error.code, CODE)?;
        let message = required(error.message, MESSAGE)?;
        let data = match error.data {
            Slot::Value(data) => Some(data),
            Slot::Missing | Slot::Null | Slot::Other(_) => None,
        };

        Ok(Line::Error {
            id,
            code,
            message,
            data,
        })
    }
}

/// The raw form of the error object of a message.
#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct RawError {
    code: Slot<Integer>,
    message: Slot<String>,
    data: Slot<Opaque>,
}

impl Nested for RawError {}

/// Whether the object holds the key of this field, with each value.
const fn present<T>(slot: &Slot<T>) -> bool {
    !matches!(slot, Slot::Missing)
}

/// Refuses a member that the form of the message does not have.
fn absent<T>(slot: &Slot<T>, name: &'static str) -> Result<(), WireError> {
    if present(slot) {
        return Err(WireError::Member(name));
    }

    Ok(())
}

/// The value of a member that the specification requires.
fn required<T>(slot: Slot<T>, name: &'static str) -> Result<T, WireError> {
    optional(slot, name)?.ok_or(WireError::Member(name))
}

/// The value of a member that a message can omit. The value `null` is not
/// an absent member: the specification gives such a member no `null`.
fn optional<T>(slot: Slot<T>, name: &'static str) -> Result<Option<T>, WireError> {
    match slot {
        Slot::Missing => Ok(None),
        Slot::Value(value) => Ok(Some(value)),
        Slot::Null | Slot::Other(_) => Err(WireError::Member(name)),
    }
}

/// An opaque member as an object. Each other kind is a fault of the member.
fn object(value: Opaque, name: &'static str) -> Result<Object, WireError> {
    Object::try_from(value).map_err(|_| WireError::Member(name))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::Rule;
    use crate::slot::tests::reads_empty_table;

    const CAP: ByteCap = ByteCap::new(4096);

    /// The text of a message with these members after the version word.
    fn message(members: &str) -> String {
        format!(r#"{{"jsonrpc":"2.0",{members}}}"#)
    }

    fn line(text: &str) -> Line {
        Line::parse(text.as_bytes(), CAP).unwrap()
    }

    fn object(text: &str) -> Object {
        Object::read(text.as_bytes(), CAP).unwrap()
    }

    fn opaque(text: &str) -> Opaque {
        Opaque::read(text.as_bytes(), CAP).unwrap()
    }

    /// The compact text of a value, as the writer gives it.
    fn written<T: Serialize>(value: &T) -> String {
        String::from_utf8(json::write(value, COMPACT).unwrap()).unwrap()
    }

    fn request(id: RequestId, method: &str, params: Option<&str>) -> Line {
        Line::Request {
            id,
            method: method.to_owned(),
            params: params.map(object),
        }
    }

    fn note(method: &str, params: Option<&str>) -> Line {
        Line::Notification {
            method: method.to_owned(),
            params: params.map(object),
        }
    }

    fn error(id: Option<u64>, code: i64, message: &str, data: Option<&str>) -> Line {
        Line::Error {
            id: id.map(RequestId::from),
            code: Integer::from(code),
            message: message.to_owned(),
            data: data.map(opaque),
        }
    }

    // --- each constant against a line of the two specifications ---

    /// A request of each method, in the form that MCP gives it.
    const REQUESTS: [(Method, &str); 3] = [
        (
            Method::Initialize,
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"example-client","version":"1.0.0"}}}"#,
        ),
        (
            Method::ToolsList,
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#,
        ),
        (
            Method::ToolsCall,
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add","arguments":{"a":1,"b":2}}}"#,
        ),
    ];

    /// A result, in the form that JSON-RPC 2.0 gives it.
    const RESULT_LINE: &str = r#"{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}"#;

    /// An error of each code, with the message of JSON-RPC 2.0, section 5.1.
    const ERRORS: [(ErrorCode, &str); 5] = [
        (
            ErrorCode::ParseError,
            r#"{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}"#,
        ),
        (
            ErrorCode::InvalidRequest,
            r#"{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Invalid Request"}}"#,
        ),
        (
            ErrorCode::MethodNotFound,
            r#"{"jsonrpc":"2.0","id":4,"error":{"code":-32601,"message":"Method not found"}}"#,
        ),
        (
            ErrorCode::InvalidParams,
            r#"{"jsonrpc":"2.0","id":"call-5","error":{"code":-32602,"message":"Invalid params"}}"#,
        ),
        (
            ErrorCode::InternalError,
            r#"{"jsonrpc":"2.0","id":6,"error":{"code":-32603,"message":"Internal error"}}"#,
        ),
    ];

    /// The parameter of `initialize` that names a revision.
    #[derive(Deserialize)]
    #[serde(rename_all = "camelCase")]
    struct Asked {
        protocol_version: String,
    }

    #[test]
    fn each_constant_is_the_word_of_a_line_of_the_specification() {
        for (method, text) in REQUESTS {
            let request = line(text);
            let Line::Request { method: name, .. } = &request else {
                panic!("{text} is a request");
            };

            assert_eq!(name, method.name(), "{text}");
            assert_eq!(Method::of_name(name), Some(method), "{text}");
            // The writer gives the same line. It thus holds the word of
            // `jsonrpc` and the name of each member.
            assert_eq!(written(&request), text);
        }

        let (_, initialize) = REQUESTS[0];
        let Line::Request {
            params: Some(params),
            ..
        } = line(initialize)
        else {
            panic!("the request has parameters");
        };
        let asked: Asked = params.parse().unwrap();
        assert_eq!(asked.protocol_version, ProtocolVersion::OFFERED.text());

        let answer = line(RESULT_LINE);
        let result = object(r#"{"tools":[]}"#);
        assert_eq!(
            answer,
            Line::Result {
                id: 2.into(),
                result
            }
        );
        assert_eq!(written(&answer), RESULT_LINE);
        assert_eq!(message(r#""id":2,"result":{"tools":[]}"#), RESULT_LINE);
        assert!(RESULT_LINE.contains(&format!(r#""jsonrpc":"{JSONRPC_VERSION}""#)));

        for (code, text) in ERRORS {
            let failed = line(text);
            let Line::Error { code: number, .. } = &failed else {
                panic!("{text} is an error");
            };

            assert_eq!(ErrorCode::of_number(*number), Some(code), "{text}");
            assert_eq!(number.to_i64(), Some(code.number()), "{text}");
            assert_eq!(written(&failed), text);
        }
    }

    #[test]
    fn the_set_of_revisions_holds_four_dates_of_the_specification() {
        let texts = ProtocolVersion::ACCEPTED.map(ProtocolVersion::text);
        assert_eq!(
            texts,
            ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
        );

        for text in texts {
            let revision = ProtocolVersion::of_text(text).unwrap();
            assert_eq!(revision.text(), text);
            assert_eq!(ProtocolVersion::agreed(text), revision);
            assert_eq!(written(&revision), format!("\"{text}\""));
        }

        // A final line feed, white space and a digit that is not ASCII.
        let others = [
            "",
            "2025-11-26",
            "2025-11-25\n",
            " 2025-11-25",
            "20251125",
            "2025-11-2\u{665}",
        ];
        for other in others {
            assert_eq!(ProtocolVersion::of_text(other), None, "{other:?}");
            assert_eq!(
                ProtocolVersion::agreed(other),
                ProtocolVersion::OFFERED,
                "{other:?}"
            );
        }
    }

    #[test]
    fn a_word_outside_a_closed_set_is_no_member() {
        let numbers = ErrorCode::ALL.map(ErrorCode::number);
        assert_eq!(numbers, [-32700, -32600, -32601, -32602, -32603]);
        for number in [0, 1, -1, 32700, -32000, -32099, -32002, -32604, i64::MIN] {
            assert_eq!(
                ErrorCode::of_number(Integer::from(number)),
                None,
                "{number}"
            );
        }
        assert_eq!(ErrorCode::of_number(Integer::from(u64::MAX)), None);

        let names = Method::ALL.map(Method::name);
        assert_eq!(names, ["initialize", "tools/list", "tools/call"]);
        for name in [
            "",
            "Initialize",
            "initialize ",
            "tools/list\n",
            "tools",
            "ping",
        ] {
            assert_eq!(Method::of_name(name), None, "{name:?}");
        }
    }

    // --- the line ---

    /// The members of a message after the version word, and its value.
    fn accepted() -> Vec<(&'static str, Line)> {
        let ping = |id: RequestId| request(id, "ping", None);
        let empty = || Some("{}");

        vec![
            // A notification has no id.
            (
                r#""method":"notifications/initialized""#,
                note("notifications/initialized", None),
            ),
            (
                r#""method":"m","params":{"n":1}"#,
                note("m", Some(r#"{"n":1}"#)),
            ),
            // The empty text is an id, and an escape is no part of an id.
            (r#""id":"","method":"ping""#, ping("".into())),
            (r#""id":"\u0061-1","method":"ping""#, ping("a-1".into())),
            // The token `-0` is the integer 0. Then the two limits of an id.
            (r#""id":-0,"method":"ping""#, ping(0.into())),
            (
                r#""id":18446744073709551615,"method":"ping""#,
                ping(u64::MAX.into()),
            ),
            (
                r#""id":-9223372036854775808,"method":"ping""#,
                ping(RequestId::Integer(Integer::from(i64::MIN))),
            ),
            // A method that this module gives no name, and empty parameters.
            (
                r#""id":1,"method":"resources/list","params":{}"#,
                request(1.into(), "resources/list", empty()),
            ),
            // The order of the members is free.
            (r#""method":"ping","id":1"#, ping(1.into())),
            (
                r#""id":"r","result":{}"#,
                Line::Result {
                    id: "r".into(),
                    result: object("{}"),
                },
            ),
            // An error with no id member and with the id `null`.
            (
                r#""error":{"code":-32700,"message":"Parse error"}"#,
                error(None, -32700, "Parse error", None),
            ),
            (
                r#""id":null,"error":{"code":-32700,"message":""}"#,
                error(None, -32700, "", None),
            ),
            // A code outside the five, and data of more than one kind.
            (
                r#""id":9,"error":{"code":-32002,"message":"m","data":{"uri":"file:///x"}}"#,
                error(Some(9), -32002, "m", Some(r#"{"uri":"file:///x"}"#)),
            ),
            (
                r#""id":9,"error":{"code":1,"message":"m","data":"more"}"#,
                error(Some(9), 1, "m", Some(r#""more""#)),
            ),
            (
                r#""id":9,"error":{"code":-0,"message":"m","data":null}"#,
                error(Some(9), 0, "m", None),
            ),
        ]
    }

    /// The members of an object after the version word. The object does not
    /// have exactly one of `method`, `result` and `error`.
    const NOT_ONE_KIND: &[&str] = &[
        r#""id":1"#,
        r#""id":1,"params":{}"#,
        r#""id":1,"method":"ping","result":{}"#,
        r#""id":1,"method":"ping","error":null"#,
        r#""id":1,"result":null,"error":{"code":1,"message":"m"}"#,
    ];

    /// The members of an object after the version word, and the member that
    /// the reader names. The object is strict JSON and no message.
    const BAD_MEMBER: &[(&str, &str)] = &[
        // The method is a text.
        (r#""id":1,"method":null"#, "method"),
        (r#""id":1,"method":7"#, "method"),
        (r#""method":["ping"]"#, "method"),
        // The id of a request. MCP permits no `null`. A number with a
        // fraction or with an exponent is no integer.
        (r#""id":null,"method":"ping""#, "id"),
        (r#""id":1.0,"method":"ping""#, "id"),
        (r#""id":1e0,"method":"ping""#, "id"),
        (r#""id":true,"method":"ping""#, "id"),
        (r#""id":[1],"method":"ping""#, "id"),
        (r#""id":{"n":1},"method":"ping""#, "id"),
        // The parameters are an object, or the member is absent.
        (r#""id":1,"method":"ping","params":null"#, "params"),
        (r#""id":1,"method":"ping","params":[1,2]"#, "params"),
        (r#""method":"ping","params":"all""#, "params"),
        (r#""method":"ping","params":7"#, "params"),
        // The check of the parameters comes before the check of the id.
        (r#""id":null,"method":"ping","params":[]"#, "params"),
        // A result has an id and an object, and no parameters.
        (r#""id":1,"result":{},"params":{}"#, "params"),
        (r#""result":{}"#, "id"),
        (r#""id":null,"result":{}"#, "id"),
        (r#""id":1.5,"result":{}"#, "id"),
        (r#""id":1,"result":null"#, "result"),
        (r#""id":1,"result":[]"#, "result"),
        (r#""id":1,"result":"ok""#, "result"),
        (r#""id":1,"result":7"#, "result"),
        // An error has an id or none, an integer code and a message. It has
        // no parameters.
        (
            r#""id":1,"error":{"code":1,"message":"m"},"params":null"#,
            "params",
        ),
        (r#""id":1.5,"error":{"code":1,"message":"m"}"#, "id"),
        (r#""id":false,"error":{"code":1,"message":"m"}"#, "id"),
        (r#""id":1,"error":null"#, "error"),
        (r#""id":1,"error":"failed""#, "error"),
        (r#""id":1,"error":[-32600,"m"]"#, "error"),
        (r#""id":1,"error":{"message":"m"}"#, "code"),
        (r#""id":1,"error":{"code":-32600.0,"message":"m"}"#, "code"),
        (r#""id":1,"error":{"code":"-32600","message":"m"}"#, "code"),
        (r#""id":1,"error":{"code":null,"message":"m"}"#, "code"),
        (r#""id":1,"error":{"code":1}"#, "message"),
        (r#""id":1,"error":{"code":1,"message":7}"#, "message"),
        (r#""id":1,"error":{"code":1,"message":null}"#, "message"),
    ];

    /// A text that is strict JSON and no object, with its kind. A list is a
    /// batch.
    const NOT_OBJECTS: &[(&str, Found)] = &[
        ("[]", Found::List),
        (r#"[{"jsonrpc":"2.0","method":"ping"}]"#, Found::List),
        ("null", Found::Null),
        ("true", Found::Boolean),
        ("7", Found::Integer),
        ("7.5", Found::Float),
        (r#""ping""#, Found::Text),
    ];

    /// An object whose version word is not the text `2.0`. The last one has
    /// no form of a message: the reader checks the word first.
    const BAD_VERSION: &[&str] = &[
        "{}",
        r#"{"id":1,"method":"ping"}"#,
        r#"{"jsonrpc":"1.0","id":1,"method":"ping"}"#,
        r#"{"jsonrpc":"2.0\n","id":1,"method":"ping"}"#,
        r#"{"jsonrpc":2.0,"id":1,"method":"ping"}"#,
        r#"{"jsonrpc":null,"id":1,"method":"ping"}"#,
        r#"{"jsonrpc":"2","id":1}"#,
    ];

    /// The members of an object after the version word. The text is not
    /// strict JSON, and it breaks the rule beside it.
    const NOT_STRICT: &[(&str, Rule)] = &[
        (r#""method":"ping"}{"#, Rule::TrailingData),
        (r#""method":"ping","#, Rule::Syntax),
        (r#""id":1,"id":2,"method":"ping""#, Rule::DuplicateKey),
        // The check reads each member of an object that the module keeps
        // whole.
        (
            r#""method":"ping","params":{"n":{"k":1,"k":2}}"#,
            Rule::DuplicateKey,
        ),
        (r#""method":"ping","params":{"n":NaN}"#, Rule::Constant),
        (r#""method":"ping","params":{"n":1e999}"#, Rule::FloatRange),
        (
            r#""method":"ping","params":{"s":"\ud800"}"#,
            Rule::LoneSurrogate,
        ),
        (
            r#""id":18446744073709551616,"method":"ping""#,
            Rule::IntegerRange,
        ),
    ];

    #[test]
    fn the_reader_takes_each_message_and_the_writer_gives_it_back() {
        for (members, value) in accepted() {
            let text = message(members);

            assert_eq!(line(&text), value, "{text}");
            // The text of the writer reads as the same value.
            assert_eq!(line(&written(&value)), value, "{text}");
        }

        // White space around the object and between its tokens, with the
        // line end of a transport.
        let spaced = " {\"jsonrpc\" : \"2.0\",\t\"id\" : 1, \"method\" : \"ping\"} \r\n";
        assert_eq!(line(spaced), request(1.into(), "ping", None));
    }

    #[test]
    fn the_reader_refuses_each_text_that_is_no_message() {
        let refused = |text: &str| {
            let refused = Line::parse(text.as_bytes(), CAP).unwrap_err();
            assert_eq!(refused.not_strict(), None, "{text}");

            refused
        };

        for (text, kind) in NOT_OBJECTS {
            assert_eq!(refused(text), WireError::NotObject(*kind), "{text}");
        }
        for text in BAD_VERSION {
            assert_eq!(refused(text), WireError::Member("jsonrpc"), "{text}");
        }
        // An object with the version word and no other member.
        assert_eq!(refused(r#"{"jsonrpc":"2.0"}"#), WireError::NotOneKind);
        for members in NOT_ONE_KIND {
            let text = message(members);
            assert_eq!(refused(&text), WireError::NotOneKind, "{text}");
        }
        for (members, named) in BAD_MEMBER {
            let text = message(members);
            assert_eq!(refused(&text), WireError::Member(named), "{text}");
        }
    }

    /// The members of an object after the version word, and the one member
    /// whose name JSON-RPC 2.0 does not give.
    const UNKNOWN_MEMBER: &[(&str, &str)] = &[
        (r#""id":1,"method":"ping","later":[1,{"x":null}]"#, "later"),
        (r#""id":1,"result":{},"_meta":{}"#, "_meta"),
        (r#""id":1,"Method":"ping""#, "Method"),
        (
            r#""id":1,"error":{"code":1,"message":"m","stack":[]}"#,
            "stack",
        ),
        // The member is at fault before the form of the message.
        (r#""later":true"#, "later"),
    ];

    #[test]
    fn the_reader_refuses_a_member_that_the_specification_does_not_name() {
        for (members, name) in UNKNOWN_MEMBER {
            let text = message(members);
            let refused = Line::parse(text.as_bytes(), CAP).unwrap_err();
            let WireError::Shape(shape) = &refused else {
                panic!("{text} has a member with no name: {refused:?}");
            };
            // The shape names the last quote of the member name, from 1.
            let key = format!("\"{name}\"");
            let column = text.find(&key).unwrap() + key.len();

            assert_eq!((shape.line(), shape.column()), (1, column), "{text}");
            assert_eq!(refused.not_strict(), None, "{text}");
        }
    }

    #[test]
    fn the_error_keeps_the_refusal_of_a_text_that_is_not_strict() {
        let rule_of = |bytes: &[u8], cap: ByteCap| {
            let refused = Line::parse(bytes, cap).unwrap_err();
            assert!(matches!(refused, WireError::NotStrict(_)));

            refused.not_strict().map(NotStrict::rule)
        };

        for (members, rule) in NOT_STRICT {
            let text = message(members);
            assert_eq!(rule_of(text.as_bytes(), CAP), Some(*rule), "{text}");
        }
        assert_eq!(rule_of(b"", CAP), Some(Rule::Syntax));
        assert_eq!(
            rule_of(b"{\"jsonrpc\":\"2.0\",\"method\":\"\xff\"}", CAP),
            Some(Rule::NotUtf8)
        );
        assert_eq!(rule_of(b"\xef\xbb\xbf{}", CAP), Some(Rule::ByteOrderMark));

        // The cap of the caller is the size rule.
        let (_, call) = REQUESTS[2];
        assert!(Line::parse(call.as_bytes(), ByteCap::new(call.len())).is_ok());
        assert_eq!(
            rule_of(call.as_bytes(), ByteCap::new(call.len() - 1)),
            Some(Rule::TooLarge)
        );

        // The line and its parameters are two levels of the 64.
        let deep = |levels: usize| {
            let nested = format!("{}1{}", "[".repeat(levels), "]".repeat(levels));
            message(&format!(r#""method":"ping","params":{{"n":{nested}}}"#))
        };
        assert!(Line::parse(deep(json::DEPTH_MAX - 2).as_bytes(), CAP).is_ok());
        assert_eq!(
            rule_of(deep(json::DEPTH_MAX - 1).as_bytes(), CAP),
            Some(Rule::TooDeep)
        );
    }

    #[test]
    fn the_writer_gives_the_members_in_the_order_of_the_specification() {
        // An error with no id gets the id `null`.
        let failed = error(None, ErrorCode::ParseError.number(), "Parse error", None);
        assert_eq!(
            written(&failed),
            message(r#""id":null,"error":{"code":-32700,"message":"Parse error"}"#)
        );

        let detailed = error(Some(3), -32602, "Invalid params", Some(r#"[1, "a"]"#));
        assert_eq!(
            written(&detailed),
            message(r#""id":3,"error":{"code":-32602,"message":"Invalid params","data":[1,"a"]}"#)
        );

        // The style of the caller applies to each part of a line.
        let spaced = Style::new(Layout::Spaced, Charset::Ascii, KeyOrder::AsGiven);
        let asked = request("caf\u{e9}".into(), "tools/list", Some(r#"{"cursor":"p2"}"#));
        assert_eq!(
            json::write(&asked, spaced).unwrap(),
            br#"{"jsonrpc": "2.0", "id": "caf\u00e9", "method": "tools/list", "params": {"cursor": "p2"}}"#
        );
    }

    #[test]
    fn each_raw_type_of_a_line_reads_an_object_with_no_member() {
        assert!(reads_empty_table::<RawLine>());
        assert!(reads_empty_table::<RawError>());
    }

    // --- the object and the error ---

    #[test]
    fn an_object_is_a_json_object_and_no_other_value() {
        for text in ["{}", " {\"a\": [1, {\"b\": null}]}\n"] {
            let value = object(text);

            assert_eq!(value.as_opaque().kind(), Found::Table, "{text}");
            assert_eq!(Object::of(&value).unwrap(), value, "{text}");
            assert_eq!(Object::try_from(opaque(text)).unwrap(), value, "{text}");
        }

        let others = [
            ("[]", Found::List),
            ("null", Found::Null),
            ("false", Found::Boolean),
            ("7", Found::Integer),
            ("7.5", Found::Float),
            (r#""{}""#, Found::Text),
        ];
        for (text, kind) in others {
            let read = Object::read(text.as_bytes(), CAP);

            assert_eq!(read, Err(WireError::NotObject(kind)), "{text}");
            assert_eq!(Object::try_from(opaque(text)), read, "{text}");
        }

        let rule_of = |bytes: &[u8], cap: ByteCap| {
            let refused = Object::read(bytes, cap).unwrap_err();
            refused.not_strict().map(NotStrict::rule)
        };
        assert_eq!(rule_of(br#"{"a":1,"a":2}"#, CAP), Some(Rule::DuplicateKey));
        assert_eq!(rule_of(b"{}", ByteCap::new(1)), Some(Rule::TooLarge));
    }

    #[test]
    fn an_object_of_a_typed_value_holds_what_the_value_writes() {
        #[derive(Serialize)]
        struct Call<'a> {
            name: &'a str,
            arguments: &'a Object,
        }

        let arguments = object(r#"{"a": 1.50, "b": -0}"#);
        let call = Call {
            name: "add",
            arguments: &arguments,
        };
        let params = Object::of(&call).unwrap();
        assert_eq!(
            params,
            object(r#"{"name":"add","arguments":{"a":1.5,"b":0}}"#)
        );

        assert_eq!(Object::of(&7_u8), Err(WireError::NotObject(Found::Integer)));
        assert_eq!(Object::of("text"), Err(WireError::NotObject(Found::Text)));
        assert_eq!(
            Object::of(&[f64::NAN]),
            Err(WireError::NoJsonForm(WriteError::NotFinite))
        );
        assert!(matches!(
            params.parse::<Vec<u8>>(),
            Err(WireError::Shape(_))
        ));
    }

    #[test]
    fn an_error_says_what_it_refuses_and_holds_no_text() {
        let not_strict = Line::parse(b"{", CAP).unwrap_err();
        let shape = object("{}").parse::<Vec<u8>>().unwrap_err();
        let no_form = WireError::NoJsonForm(WriteError::NotFinite);
        let texts = [
            (not_strict, "the text is not strict JSON: syntax at byte 1"),
            (shape, "the JSON value has another shape: line 1, column 0"),
            (
                WireError::NotObject(Found::List),
                "the JSON value is not an object",
            ),
            (
                no_form,
                "the value has no JSON text: a float that is not finite",
            ),
            (WireError::NotOneKind, "the object is not one message"),
            (WireError::Member("id"), "the member `id` is not valid"),
        ];
        let with_source = [true, true, false, true, false, false];
        for ((error, text), has_source) in texts.into_iter().zip(with_source) {
            assert_eq!(error.to_string(), text);
            assert_eq!(error.source().is_some(), has_source, "{text}");
        }
    }
}
