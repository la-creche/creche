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
//! A result reads in a second step, because only its request says what a
//! result holds. The caller finds the request of the id. It then makes an
//! [`InitializeResult`], a [`ListToolsResult`] or a [`CallToolResult`] from
//! the [`Object`] of the line. MCP lets a result hold more members than it
//! names. The reader of a result thus skips a member that its type does not
//! keep.
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
//! use creche_contracts::mcp::{CallToolResult, Content, Line, Method, Object, RequestId};
//!
//! const CAP: ByteCap = ByteCap::new(4096);
//! const STYLE: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven);
//!
//! // A server reads a request and answers it with the same id.
//! let asked = br#"{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"add"}}"#;
//! let Line::Request { id, method, .. } = Line::parse(asked, CAP)? else {
//!     panic!("the text is a request");
//! };
//! assert_eq!(Method::of_name(&method), Some(Method::ToolsCall));
//!
//! let result = CallToolResult::new(vec![Content::Text("3".to_owned())]);
//! let answer = Line::Result { id, result: Object::of(&result)? };
//! let text = json::write(&answer, STYLE)?;
//! assert_eq!(text, br#"{"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"3"}]}}"#);
//!
//! // The client reads the answer and then the result of its request.
//! let Line::Result { id, result } = Line::parse(&text, CAP)? else {
//!     panic!("the text is a result");
//! };
//! assert_eq!(id, RequestId::from(7));
//! assert!(!CallToolResult::try_from(&result)?.is_error());
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

/// The `type` of a content block that holds a text.
const TEXT_BLOCK: &str = "text";

// The members of a message. The writer and the errors name each one.
const JSONRPC: &str = "jsonrpc";
const ID: &str = "id";
const METHOD: &str = "method";
const PARAMS: &str = "params";
const RESULT: &str = "result";
const ERROR: &str = "error";
const CODE: &str = "code";
const MESSAGE: &str = "message";

// The members of a result that an error names in more than one place.
const PROTOCOL_VERSION: &str = "protocolVersion";
const CAPABILITIES: &str = "capabilities";
const INPUT_SCHEMA: &str = "inputSchema";
const TOOLS: &str = "tools";
const CONTENT: &str = "content";

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
///   outside the set, [`InitializeResult`] refuses the result. The client
///   then ends the session.
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

/// The result of `initialize`: what the server says in the handshake.
///
/// The type keeps the members that MCP requires, and `instructions`. Of the
/// server it keeps the name and the version. The reader drops each other
/// member. The capabilities stay an [`Object`], because their members
/// differ from revision to revision.
///
/// A value holds a revision of [`ProtocolVersion`] only. A client thus
/// cannot continue a session in a revision that the platform does not
/// accept.
///
/// ```
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{InitializeResult, Object, ProtocolVersion, WireError};
///
/// let result = Object::read(
///     br#"{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},
///          "serverInfo":{"name":"example","version":"1.2.0"}}"#,
///     ByteCap::new(256),
/// )?;
/// let said = InitializeResult::try_from(&result)?;
///
/// assert_eq!(said.protocol_version(), ProtocolVersion::V2025_06_18);
/// assert_eq!((said.server_name(), said.server_version()), ("example", "1.2.0"));
/// assert_eq!(said.instructions(), None);
/// # Ok::<(), WireError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{InitializeResult, Object, ProtocolVersion, WireError};
///
/// let said = InitializeResult::new(
///     ProtocolVersion::OFFERED,
///     Object::read(b"{}", ByteCap::new(2)).unwrap(),
///     "example".to_owned(),
///     "1.2.0".to_owned(),
/// );
/// let other = InitializeResult { instructions: None, ..said };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InitializeResult {
    protocol_version: ProtocolVersion,
    capabilities: Object,
    server_info: Implementation,
    #[serde(skip_serializing_if = "Option::is_none")]
    instructions: Option<String>,
}

/// The name and the version of a program, as `serverInfo` holds them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct Implementation {
    name: String,
    version: String,
}

impl InitializeResult {
    /// A result with no instructions.
    #[must_use]
    pub const fn new(
        protocol_version: ProtocolVersion,
        capabilities: Object,
        server_name: String,
        server_version: String,
    ) -> Self {
        Self {
            protocol_version,
            capabilities,
            server_info: Implementation {
                name: server_name,
                version: server_version,
            },
            instructions: None,
        }
    }

    /// The same result with a text that tells a model how to use the server.
    #[must_use]
    pub fn with_instructions(mut self, instructions: String) -> Self {
        self.instructions = Some(instructions);
        self
    }

    /// The revision that the session uses.
    #[must_use]
    pub const fn protocol_version(&self) -> ProtocolVersion {
        self.protocol_version
    }

    /// What the server can do, as the object of the wire.
    #[must_use]
    pub const fn capabilities(&self) -> &Object {
        &self.capabilities
    }

    /// The name of the server program.
    #[must_use]
    pub fn server_name(&self) -> &str {
        &self.server_info.name
    }

    /// The version of the server program.
    #[must_use]
    pub fn server_version(&self) -> &str {
        &self.server_info.version
    }

    /// The text that tells a model how to use the server, if the result has
    /// one.
    #[must_use]
    pub fn instructions(&self) -> Option<&str> {
        self.instructions.as_deref()
    }
}

impl TryFrom<&Object> for InitializeResult {
    type Error = WireError;

    fn try_from(result: &Object) -> Result<Self, WireError> {
        let raw: RawInitialize = result.parse()?;
        let answered = required(raw.protocol_version, PROTOCOL_VERSION)?;
        let protocol_version =
            ProtocolVersion::of_text(&answered).ok_or(WireError::Member(PROTOCOL_VERSION))?;
        let capabilities = object(required(raw.capabilities, CAPABILITIES)?, CAPABILITIES)?;
        let server = required(raw.server_info, "serverInfo")?;

        Ok(Self {
            protocol_version,
            capabilities,
            server_info: Implementation {
                name: required(server.name, "name")?,
                version: required(server.version, "version")?,
            },
            instructions: optional(raw.instructions, "instructions")?,
        })
    }
}

#[derive(Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct RawInitialize {
    protocol_version: Slot<String>,
    capabilities: Slot<Opaque>,
    server_info: Slot<RawImplementation>,
    instructions: Slot<String>,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct RawImplementation {
    name: Slot<String>,
    version: Slot<String>,
}

impl Nested for RawImplementation {}

/// A hint about a tool: one member of the `annotations` of MCP.
///
/// The set is closed, and a hint crosses a process boundary. A reader drops
/// an annotation with another name. A hint is a claim of the server and no
/// proof: MCP tells a client not to trust the hint of a server that it does
/// not trust.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Hint {
    /// `readOnlyHint`: the tool changes nothing. MCP reads an absent hint
    /// as `false`.
    ReadOnly,
    /// `destructiveHint`: the tool can destroy data. MCP reads an absent
    /// hint as `true`.
    Destructive,
    /// `idempotentHint`: a second call with the same arguments changes
    /// nothing more. MCP reads an absent hint as `false`.
    Idempotent,
    /// `openWorldHint`: the tool reaches systems outside the server. MCP
    /// reads an absent hint as `true`.
    OpenWorld,
}

/// What a server says with each [`Hint`]. `None` is an absent hint.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
struct Hints {
    #[serde(skip_serializing_if = "Option::is_none")]
    read_only_hint: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    destructive_hint: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    idempotent_hint: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    open_world_hint: Option<bool>,
}

impl Hints {
    fn is_empty(&self) -> bool {
        *self == Self::default()
    }

    fn of(&mut self, hint: Hint) -> &mut Option<bool> {
        match hint {
            Hint::ReadOnly => &mut self.read_only_hint,
            Hint::Destructive => &mut self.destructive_hint,
            Hint::Idempotent => &mut self.idempotent_hint,
            Hint::OpenWorld => &mut self.open_world_hint,
        }
    }
}

/// One tool of a `tools/list` result.
///
/// The type keeps the name, the description, the input schema and the four
/// hints. The reader drops each other member, for example the title and the
/// output schema. The module reads no member of the schema: it checks only
/// that the schema is an object.
///
/// ```
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{Hint, Object, Tool, WireError};
///
/// let schema = Object::read(br#"{"type":"object"}"#, ByteCap::new(64))?;
/// let tool = Tool::new("add".to_owned(), schema).with_hint(Hint::ReadOnly);
///
/// assert_eq!(tool.name(), "add");
/// assert_eq!(tool.hint(Hint::ReadOnly), Some(true));
/// assert_eq!(tool.hint(Hint::Destructive), None);
/// # Ok::<(), WireError>(())
/// ```
///
/// Code outside this module cannot build a tool from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{Hint, Object, Tool, WireError};
///
/// let schema = Object::read(br#"{"type":"object"}"#, ByteCap::new(64)).unwrap();
/// let tool = Tool::new("add".to_owned(), schema);
/// let other = Tool { name: String::new(), ..tool };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Tool {
    name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<String>,
    input_schema: Object,
    #[serde(skip_serializing_if = "Hints::is_empty")]
    annotations: Hints,
}

impl Tool {
    /// A tool with no description and no hint.
    #[must_use]
    pub fn new(name: String, input_schema: Object) -> Self {
        Self {
            name,
            description: None,
            input_schema,
            annotations: Hints::default(),
        }
    }

    /// The same tool with a description for a model.
    #[must_use]
    pub fn with_description(mut self, description: String) -> Self {
        self.description = Some(description);
        self
    }

    /// The same tool with a hint that says `true`.
    #[must_use]
    pub fn with_hint(mut self, hint: Hint) -> Self {
        *self.annotations.of(hint) = Some(true);
        self
    }

    /// The same tool with a hint that says `false`.
    #[must_use]
    pub fn with_hint_denied(mut self, hint: Hint) -> Self {
        *self.annotations.of(hint) = Some(false);
        self
    }

    /// The name that a call gives.
    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    /// The description for a model, if the tool has one.
    #[must_use]
    pub fn description(&self) -> Option<&str> {
        self.description.as_deref()
    }

    /// The JSON Schema of the arguments, as the object of the wire.
    #[must_use]
    pub const fn input_schema(&self) -> &Object {
        &self.input_schema
    }

    /// What the server says with this hint. `None` for an absent hint: the
    /// doc of each [`Hint`] gives the value that MCP then assumes.
    #[must_use]
    pub const fn hint(&self, hint: Hint) -> Option<bool> {
        match hint {
            Hint::ReadOnly => self.annotations.read_only_hint,
            Hint::Destructive => self.annotations.destructive_hint,
            Hint::Idempotent => self.annotations.idempotent_hint,
            Hint::OpenWorld => self.annotations.open_world_hint,
        }
    }
}

impl Tool {
    /// The tool that an item of `tools` holds.
    fn of_raw(raw: RawTool) -> Result<Self, WireError> {
        let hints = optional(raw.annotations, "annotations")?.unwrap_or_default();

        Ok(Self {
            name: required(raw.name, "name")?,
            description: optional(raw.description, "description")?,
            input_schema: object(required(raw.input_schema, INPUT_SCHEMA)?, INPUT_SCHEMA)?,
            annotations: Hints {
                read_only_hint: optional(hints.read_only_hint, "readOnlyHint")?,
                destructive_hint: optional(hints.destructive_hint, "destructiveHint")?,
                idempotent_hint: optional(hints.idempotent_hint, "idempotentHint")?,
                open_world_hint: optional(hints.open_world_hint, "openWorldHint")?,
            },
        })
    }
}

#[derive(Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct RawTool {
    name: Slot<String>,
    description: Slot<String>,
    input_schema: Slot<Opaque>,
    annotations: Slot<RawHints>,
}

impl Nested for RawTool {}

#[derive(Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct RawHints {
    read_only_hint: Slot<bool>,
    destructive_hint: Slot<bool>,
    idempotent_hint: Slot<bool>,
    open_world_hint: Slot<bool>,
}

impl Nested for RawHints {}

/// The result of `tools/list`: one page of the tools of a server.
///
/// ```
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{ListToolsResult, Object, WireError};
///
/// let result = Object::read(
///     br#"{"tools":[{"name":"add","inputSchema":{"type":"object"}}],"nextCursor":"p2"}"#,
///     ByteCap::new(256),
/// )?;
/// let page = ListToolsResult::try_from(&result)?;
///
/// assert_eq!(page.tools().len(), 1);
/// assert_eq!(page.next_cursor(), Some("p2"));
/// # Ok::<(), WireError>(())
/// ```
///
/// Code outside this module cannot build a page from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{ListToolsResult, Object, WireError};
///
/// let page = ListToolsResult { tools: Vec::new(), next_cursor: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ListToolsResult {
    tools: Vec<Tool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    next_cursor: Option<String>,
}

impl ListToolsResult {
    /// A page that is the last one.
    #[must_use]
    pub const fn new(tools: Vec<Tool>) -> Self {
        Self {
            tools,
            next_cursor: None,
        }
    }

    /// The same page with the cursor of the next page. A client gives the
    /// cursor back in its next `tools/list` request and reads no part of
    /// it.
    #[must_use]
    pub fn with_next_cursor(mut self, next_cursor: String) -> Self {
        self.next_cursor = Some(next_cursor);
        self
    }

    /// The tools of this page.
    #[must_use]
    pub fn tools(&self) -> &[Tool] {
        &self.tools
    }

    /// The cursor of the next page. `None` on the last page.
    #[must_use]
    pub fn next_cursor(&self) -> Option<&str> {
        self.next_cursor.as_deref()
    }
}

impl TryFrom<&Object> for ListToolsResult {
    type Error = WireError;

    fn try_from(result: &Object) -> Result<Self, WireError> {
        let raw: RawListTools = result.parse()?;
        let tools = required(raw.tools, TOOLS)?
            .into_iter()
            .map(|tool| required(tool, TOOLS).and_then(Tool::of_raw))
            .collect::<Result<_, _>>()?;

        Ok(Self {
            tools,
            next_cursor: optional(raw.next_cursor, "nextCursor")?,
        })
    }
}

#[derive(Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct RawListTools {
    tools: Slot<Vec<Slot<RawTool>>>,
    next_cursor: Slot<String>,
}

/// One block of the content of a tool result.
///
/// MCP gives each block a `type`. The set of the types is closed in each
/// revision, and a newer revision can add a type. The module reads the
/// block of the type `text`. A reader keeps a block of each other type
/// whole, as [`Content::Other`], and does not refuse it.
///
/// The `Debug` form shows the length of a text and no character of it: the
/// output of a tool can hold a secret.
#[derive(Clone, PartialEq, Eq)]
pub enum Content {
    /// A block of the type `text`: its text. The reader drops each other
    /// member of such a block.
    Text(String),
    /// A block of another type, for example `image`.
    Other(OtherContent),
}

impl Content {
    /// The block that an item of `content` holds.
    fn of_block(block: Opaque) -> Result<Self, WireError> {
        let block = object(block, CONTENT)?;
        let raw: RawBlock = block.parse()?;
        let kind = required(raw.kind, "type")?;
        if kind == TEXT_BLOCK {
            return required(raw.text, "text").map(Self::Text);
        }

        Ok(Self::Other(OtherContent { kind, block }))
    }
}

impl fmt::Debug for Content {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Text(text) => f.debug_struct("Text").field("bytes", &text.len()).finish(),
            Self::Other(other) => f.debug_tuple("Other").field(other).finish(),
        }
    }
}

impl Serialize for Content {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Text(text) => TextBlock {
                kind: TEXT_BLOCK,
                text,
            }
            .serialize(serializer),
            Self::Other(other) => other.block.serialize(serializer),
        }
    }
}

/// A block of the type `text`, as the writer gives it.
#[derive(Serialize)]
struct TextBlock<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    text: &'a str,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct RawBlock {
    #[serde(rename = "type")]
    kind: Slot<String>,
    text: Slot<String>,
}

/// A content block that the module does not read: an object whose `type` is
/// a text other than `text`.
///
/// Only the read of a [`CallToolResult`] makes a value, so each value holds
/// that rule. The writer gives the block back as it came.
///
/// ```
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{CallToolResult, Content, Object, OtherContent, WireError};
///
/// let result = Object::read(
///     br#"{"content":[{"type":"image","data":"AA==","mimeType":"image/png"}]}"#,
///     ByteCap::new(256),
/// )?;
/// let said = CallToolResult::try_from(&result)?;
/// let [Content::Other(block)] = said.content() else {
///     panic!("the result holds one block that is no text");
/// };
/// let block: &OtherContent = block;
///
/// assert_eq!(block.kind(), "image");
/// assert_eq!(Object::of(&said)?, result);
/// # Ok::<(), WireError>(())
/// ```
///
/// Code outside this module cannot give an object that proof:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{CallToolResult, Content, Object, OtherContent, WireError};
///
/// let block = Object::read(b"{}", ByteCap::new(2)).unwrap();
/// let other = OtherContent { kind: String::new(), block };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OtherContent {
    kind: String,
    block: Object,
}

impl OtherContent {
    /// The `type` of the block.
    #[must_use]
    pub fn kind(&self) -> &str {
        &self.kind
    }

    /// The whole block.
    #[must_use]
    pub const fn block(&self) -> &Object {
        &self.block
    }
}

/// The result of `tools/call`: what a tool gives back.
///
/// The type keeps the content and the mark of a failed call. The reader
/// drops each other member, for example `structuredContent`.
///
/// A tool that fails at its work gives a result with the mark, so the model
/// can read why the call failed. An error of JSON-RPC is for a fault of the
/// protocol, for example a tool that the server does not have. A result
/// with no `isError` member is the result of a call that did not fail.
///
/// ```
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{CallToolResult, Content, Object, WireError};
///
/// let result = Object::read(
///     br#"{"content":[{"type":"text","text":"no such file"}],"isError":true}"#,
///     ByteCap::new(256),
/// )?;
/// let said = CallToolResult::try_from(&result)?;
///
/// assert_eq!(said.content(), [Content::Text("no such file".to_owned())]);
/// assert!(said.is_error());
/// # Ok::<(), WireError>(())
/// ```
///
/// Code outside this module cannot build a result from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::ByteCap;
/// use creche_contracts::mcp::{CallToolResult, Content, Object, WireError};
///
/// let said = CallToolResult { content: Vec::new(), is_error: false };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CallToolResult {
    content: Vec<Content>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    is_error: bool,
}

impl CallToolResult {
    /// The result of a call that did not fail.
    #[must_use]
    pub const fn new(content: Vec<Content>) -> Self {
        Self {
            content,
            is_error: false,
        }
    }

    /// The same result with the mark of a failed call.
    #[must_use]
    pub fn with_error(mut self) -> Self {
        self.is_error = true;
        self
    }

    /// The blocks, in the order of the wire.
    #[must_use]
    pub fn content(&self) -> &[Content] {
        &self.content
    }

    /// Whether the tool says that the call failed.
    #[must_use]
    pub const fn is_error(&self) -> bool {
        self.is_error
    }
}

impl TryFrom<&Object> for CallToolResult {
    type Error = WireError;

    fn try_from(result: &Object) -> Result<Self, WireError> {
        let raw: RawCallTool = result.parse()?;
        let content = required(raw.content, CONTENT)?
            .into_iter()
            .map(|block| required(block, CONTENT).and_then(Content::of_block))
            .collect::<Result<_, _>>()?;

        Ok(Self {
            content,
            is_error: optional(raw.is_error, "isError")?.unwrap_or(false),
        })
    }
}

#[derive(Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct RawCallTool {
    content: Slot<Vec<Slot<Opaque>>>,
    is_error: Slot<bool>,
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

    // --- the three results ---

    /// The result of each method, in the form that MCP gives it.
    const INITIALIZED: &str = r#"{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"example-server","version":"1.0.0"},"instructions":"Call add for a sum."}"#;
    const LISTED: &str = r#"{"tools":[{"name":"add","description":"Adds two numbers.","inputSchema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]},"annotations":{"readOnlyHint":true,"openWorldHint":false}}],"nextCursor":"page-2"}"#;
    const CALLED: &str = r#"{"content":[{"type":"text","text":"3"}]}"#;
    const FAILED: &str = r#"{"content":[{"type":"text","text":"no such tool"}],"isError":true}"#;

    /// The member `type` of a schema or of a content block.
    #[derive(Deserialize)]
    struct Kind {
        #[serde(rename = "type")]
        kind: String,
    }

    fn tool(name: &str) -> Tool {
        Tool::new(name.to_owned(), object(r#"{"type":"object"}"#))
    }

    #[test]
    fn each_result_of_the_specification_reads_and_writes() {
        // A result comes in a line, and the line gives its object.
        let Line::Result { result, .. } =
            line(&message(&format!(r#""id":1,"result":{INITIALIZED}"#)))
        else {
            panic!("the text is a result");
        };
        let said = InitializeResult::try_from(&result).unwrap();
        assert_eq!(said.protocol_version(), ProtocolVersion::OFFERED);
        assert_eq!(
            said.capabilities(),
            &object(r#"{"tools":{"listChanged":false}}"#)
        );
        assert_eq!(said.server_name(), "example-server");
        assert_eq!(said.server_version(), "1.0.0");
        assert_eq!(said.instructions(), Some("Call add for a sum."));
        assert_eq!(written(&said), INITIALIZED);

        let page = ListToolsResult::try_from(&object(LISTED)).unwrap();
        let [add] = page.tools() else {
            panic!("the page holds one tool");
        };
        assert_eq!(add.name(), "add");
        assert_eq!(add.description(), Some("Adds two numbers."));
        assert_eq!(add.input_schema().parse::<Kind>().unwrap().kind, "object");
        let hints = [
            Hint::ReadOnly,
            Hint::Destructive,
            Hint::Idempotent,
            Hint::OpenWorld,
        ];
        assert_eq!(
            hints.map(|hint| add.hint(hint)),
            [Some(true), None, None, Some(false)]
        );
        assert_eq!(page.next_cursor(), Some("page-2"));
        assert_eq!(written(&page), LISTED);

        let done = CallToolResult::try_from(&object(CALLED)).unwrap();
        assert_eq!(done.content(), [Content::Text("3".to_owned())]);
        assert!(!done.is_error());
        assert_eq!(written(&done), CALLED);

        let failed = CallToolResult::try_from(&object(FAILED)).unwrap();
        assert_eq!(failed.content(), [Content::Text("no such tool".to_owned())]);
        assert!(failed.is_error());
        assert_eq!(written(&failed), FAILED);
    }

    #[test]
    fn a_result_from_typed_parts_equals_the_result_of_its_text() {
        let said = InitializeResult::new(
            ProtocolVersion::V2025_03_26,
            object("{}"),
            "example-server".to_owned(),
            "2".to_owned(),
        );
        let text = r#"{"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"example-server","version":"2"}}"#;
        assert_eq!(written(&said), text);
        assert_eq!(InitializeResult::try_from(&object(text)).unwrap(), said);
        assert_eq!(said.instructions(), None);
        let guided = said.with_instructions("Call add.".to_owned());
        assert_eq!(guided.instructions(), Some("Call add."));
        assert_eq!(
            InitializeResult::try_from(&Object::of(&guided).unwrap()).unwrap(),
            guided
        );

        let hinted = tool("delete")
            .with_description("Deletes a file.".to_owned())
            .with_hint(Hint::Destructive)
            .with_hint(Hint::Idempotent)
            .with_hint_denied(Hint::ReadOnly)
            .with_hint_denied(Hint::OpenWorld);
        assert_eq!(tool("add").description(), None);
        assert_eq!(hinted.hint(Hint::ReadOnly), Some(false));
        assert_eq!(hinted.hint(Hint::Destructive), Some(true));
        assert_eq!(hinted.hint(Hint::Idempotent), Some(true));
        assert_eq!(hinted.hint(Hint::OpenWorld), Some(false));
        let page = ListToolsResult::new(vec![tool("add"), hinted]);
        let text = r#"{"tools":[{"name":"add","inputSchema":{"type":"object"}},{"name":"delete","description":"Deletes a file.","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":false,"destructiveHint":true,"idempotentHint":true,"openWorldHint":false}}]}"#;
        assert_eq!(written(&page), text);
        assert_eq!(ListToolsResult::try_from(&object(text)).unwrap(), page);
        assert_eq!(page.next_cursor(), None);
        let more = page.with_next_cursor("p2".to_owned());
        assert_eq!(more.next_cursor(), Some("p2"));
        assert_eq!(
            ListToolsResult::try_from(&Object::of(&more).unwrap()).unwrap(),
            more
        );

        let texts = vec![Content::Text("a".to_owned()), Content::Text(String::new())];
        let done = CallToolResult::new(texts);
        let text = r#"{"content":[{"type":"text","text":"a"},{"type":"text","text":""}]}"#;
        assert_eq!(written(&done), text);
        assert_eq!(CallToolResult::try_from(&object(text)).unwrap(), done);
        let failed = done.with_error();
        assert!(failed.is_error());
        assert_eq!(
            CallToolResult::try_from(&Object::of(&failed).unwrap()).unwrap(),
            failed
        );
    }

    #[test]
    fn a_result_reads_with_no_optional_member_and_with_more_members() {
        // The reader drops a member that the type does not keep.
        let said = InitializeResult::try_from(&object(
            r#"{"protocolVersion":"2024-11-05","capabilities":{"logging":{}},"_meta":{"k":1},
                "serverInfo":{"name":"s","version":"0","title":"S","icons":[]}}"#,
        ))
        .unwrap();
        assert_eq!(said.protocol_version(), ProtocolVersion::V2024_11_05);
        assert_eq!((said.server_name(), said.server_version()), ("s", "0"));

        let page = ListToolsResult::try_from(&object(
            r#"{"tools":[{"name":"a","title":"A","inputSchema":{"type":"object"},
                "outputSchema":{"type":"object"},"annotations":{}},
               {"name":"b","inputSchema":{},"annotations":{"title":"B","laterHint":7}}]}"#,
        ))
        .unwrap();
        assert_eq!(
            page.tools(),
            [tool("a"), Tool::new("b".to_owned(), object("{}"))]
        );
        let none = ListToolsResult::try_from(&object(r#"{"tools":[]}"#)).unwrap();
        assert_eq!(none, ListToolsResult::new(Vec::new()));

        // An `isError` of `false` is the same as no member.
        for text in [
            r#"{"content":[],"structuredContent":{"n":1}}"#,
            r#"{"content":[],"isError":false}"#,
        ] {
            let said = CallToolResult::try_from(&object(text)).unwrap();
            assert_eq!(said, CallToolResult::new(Vec::new()), "{text}");
            assert_eq!(written(&said), r#"{"content":[]}"#, "{text}");
        }
    }

    #[test]
    fn a_block_of_another_type_stays_whole() {
        let blocks = [
            r#"{"type":"image","data":"AA==","mimeType":"image/png"}"#,
            r#"{"type":"audio","data":"AA==","mimeType":"audio/wav"}"#,
            r#"{"type":"resource_link","uri":"file:///a","name":"a"}"#,
            r#"{"type":"resource","resource":{"uri":"file:///a","text":"t"}}"#,
            // A type of a later revision, a type in another letter case and
            // the empty type.
            r#"{"type":"video","text":"no text of a text block"}"#,
            r#"{"type":"Text","text":"t"}"#,
            r#"{"type":""}"#,
        ];
        for block in blocks {
            let text = format!(r#"{{"content":[{{"type":"text","text":"t"}},{block}]}}"#);
            let said = CallToolResult::try_from(&object(&text)).unwrap();
            let [Content::Text(first), Content::Other(other)] = said.content() else {
                panic!("{block} is a block of another type");
            };

            assert_eq!(first, "t");
            assert_eq!(other.block(), &object(block), "{block}");
            assert_eq!(
                other.kind(),
                other.block().parse::<Kind>().unwrap().kind,
                "{block}"
            );
            assert_eq!(written(&said), text);
        }
    }

    #[test]
    fn the_debug_form_of_a_content_block_shows_no_text() {
        let output = Content::Text("the output of a tool".to_owned());
        assert_eq!(format!("{output:?}"), "Text { bytes: 20 }");

        let image = object(r#"{"content":[{"type":"image","data":"QUJD"}]}"#);
        let shown = format!("{:?}", CallToolResult::try_from(&image).unwrap());
        assert!(shown.contains(r#"kind: "image""#), "{shown}");
        assert!(!shown.contains("QUJD"), "{shown}");
    }

    /// A result of `initialize` with one member replaced. `None` removes the
    /// member.
    fn initialized(member: &str, value: Option<&str>) -> Object {
        let mut members = vec![
            ("protocolVersion", r#""2025-11-25""#),
            ("capabilities", "{}"),
            ("serverInfo", r#"{"name":"s","version":"0"}"#),
        ];
        members.retain(|(name, _)| *name != member);
        members.extend(value.map(|value| (member, value)));
        let texts: Vec<String> = members
            .iter()
            .map(|(name, value)| format!(r#""{name}":{value}"#))
            .collect();

        object(&format!("{{{}}}", texts.join(",")))
    }

    /// A member of a result of `initialize`, a value for it, and the member
    /// that the reader then names. No value removes the member.
    const NOT_INITIALIZED: &[(&str, Option<&str>, &str)] = &[
        ("protocolVersion", None, "protocolVersion"),
        ("protocolVersion", Some("20251125"), "protocolVersion"),
        ("protocolVersion", Some("null"), "protocolVersion"),
        // A revision outside the set, and a text that only starts with one.
        (
            "protocolVersion",
            Some(r#""2099-01-01""#),
            "protocolVersion",
        ),
        (
            "protocolVersion",
            Some(r#""2025-11-25\n""#),
            "protocolVersion",
        ),
        ("capabilities", None, "capabilities"),
        ("capabilities", Some("null"), "capabilities"),
        ("capabilities", Some("[]"), "capabilities"),
        ("capabilities", Some(r#""tools""#), "capabilities"),
        ("serverInfo", None, "serverInfo"),
        ("serverInfo", Some("null"), "serverInfo"),
        ("serverInfo", Some(r#""s 0""#), "serverInfo"),
        // A list is no object: the reader takes no member by its place.
        ("serverInfo", Some(r#"["s","0"]"#), "serverInfo"),
        ("serverInfo", Some(r#"{"version":"0"}"#), "name"),
        ("serverInfo", Some(r#"{"name":7,"version":"0"}"#), "name"),
        ("serverInfo", Some(r#"{"name":null,"version":"0"}"#), "name"),
        ("serverInfo", Some(r#"{"name":"s"}"#), "version"),
        (
            "serverInfo",
            Some(r#"{"name":"s","version":1.0}"#),
            "version",
        ),
        ("instructions", Some("null"), "instructions"),
        ("instructions", Some(r#"["a"]"#), "instructions"),
    ];

    /// An object that is no result of `tools/list`, with the member that the
    /// reader names.
    const NOT_LISTED: &[(&str, &str)] = &[
        ("{}", "tools"),
        (r#"{"tools":null}"#, "tools"),
        (r#"{"tools":{"add":{}}}"#, "tools"),
        (r#"{"tools":["add"]}"#, "tools"),
        (r#"{"tools":[null]}"#, "tools"),
        (r#"{"tools":[["add",{}]]}"#, "tools"),
        (r#"{"tools":[],"nextCursor":null}"#, "nextCursor"),
        (r#"{"tools":[],"nextCursor":2}"#, "nextCursor"),
        // The second tool is at fault.
        (
            r#"{"tools":[{"name":"a","inputSchema":{}},{"name":"b"}]}"#,
            "inputSchema",
        ),
    ];

    /// An object that is no tool, with the member that the reader names.
    const NOT_A_TOOL: &[(&str, &str)] = &[
        (r#"{"inputSchema":{}}"#, "name"),
        (r#"{"name":7,"inputSchema":{}}"#, "name"),
        (r#"{"name":null,"inputSchema":{}}"#, "name"),
        (
            r#"{"name":"a","description":null,"inputSchema":{}}"#,
            "description",
        ),
        (
            r#"{"name":"a","description":["d"],"inputSchema":{}}"#,
            "description",
        ),
        (r#"{"name":"a"}"#, "inputSchema"),
        (r#"{"name":"a","inputSchema":null}"#, "inputSchema"),
        (r#"{"name":"a","inputSchema":[]}"#, "inputSchema"),
        (r#"{"name":"a","inputSchema":"object"}"#, "inputSchema"),
        (
            r#"{"name":"a","inputSchema":{},"annotations":null}"#,
            "annotations",
        ),
        (
            r#"{"name":"a","inputSchema":{},"annotations":[true]}"#,
            "annotations",
        ),
    ];

    /// The annotations of a tool with a hint that is not `true` or `false`,
    /// and the hint that the reader names.
    const NOT_HINTS: &[(&str, &str)] = &[
        (r#"{"readOnlyHint":"true"}"#, "readOnlyHint"),
        (r#"{"readOnlyHint":1}"#, "readOnlyHint"),
        (r#"{"destructiveHint":null}"#, "destructiveHint"),
        (r#"{"idempotentHint":0}"#, "idempotentHint"),
        (r#"{"openWorldHint":[]}"#, "openWorldHint"),
    ];

    /// An object that is no result of `tools/call`, with the member that the
    /// reader names.
    const NOT_CALLED: &[(&str, &str)] = &[
        ("{}", "content"),
        (r#"{"content":null}"#, "content"),
        (r#"{"content":{"type":"text","text":"t"}}"#, "content"),
        (r#"{"content":"t"}"#, "content"),
        (r#"{"content":["t"]}"#, "content"),
        (r#"{"content":[null]}"#, "content"),
        (r#"{"content":[["text","t"]]}"#, "content"),
        (r#"{"content":[{"text":"t"}]}"#, "type"),
        (r#"{"content":[{"type":7,"text":"t"}]}"#, "type"),
        (r#"{"content":[{"type":null,"text":"t"}]}"#, "type"),
        (r#"{"content":[{"type":"text"}]}"#, "text"),
        (r#"{"content":[{"type":"text","text":7}]}"#, "text"),
        (r#"{"content":[{"type":"text","text":null}]}"#, "text"),
        (
            r#"{"content":[{"type":"text","text":"t"},{"type":"text"}]}"#,
            "text",
        ),
        (r#"{"content":[],"isError":"true"}"#, "isError"),
        (r#"{"content":[],"isError":1}"#, "isError"),
        (r#"{"content":[],"isError":null}"#, "isError"),
    ];

    #[test]
    fn each_result_refuses_an_object_that_breaks_the_specification() {
        for (member, value, named) in NOT_INITIALIZED {
            let refused = InitializeResult::try_from(&initialized(member, *value));
            assert_eq!(
                refused,
                Err(WireError::Member(named)),
                "{member}: {value:?}"
            );
        }
        // The same object with no change is a result.
        assert!(InitializeResult::try_from(&initialized("instructions", None)).is_ok());

        let whole = NOT_LISTED
            .iter()
            .map(|(text, named)| ((*text).to_owned(), named));
        let tools = NOT_A_TOOL
            .iter()
            .map(|(tool, named)| (format!(r#"{{"tools":[{tool}]}}"#), named));
        let hints = NOT_HINTS.iter().map(|(hints, named)| {
            let tool = format!(r#"{{"name":"a","inputSchema":{{}},"annotations":{hints}}}"#);
            (format!(r#"{{"tools":[{tool}]}}"#), named)
        });
        for (text, named) in whole.chain(tools).chain(hints) {
            let refused = ListToolsResult::try_from(&object(&text));
            assert_eq!(refused, Err(WireError::Member(named)), "{text}");
        }

        for (text, named) in NOT_CALLED {
            let refused = CallToolResult::try_from(&object(text));
            assert_eq!(refused, Err(WireError::Member(named)), "{text}");
        }
    }

    #[test]
    fn each_raw_type_of_a_result_reads_an_object_with_no_member() {
        assert!(reads_empty_table::<RawInitialize>());
        assert!(reads_empty_table::<RawImplementation>());
        assert!(reads_empty_table::<RawListTools>());
        assert!(reads_empty_table::<RawTool>());
        assert!(reads_empty_table::<RawHints>());
        assert!(reads_empty_table::<RawCallTool>());
        assert!(reads_empty_table::<RawBlock>());
    }
}
