//! An HTTP/1.1 client over a Unix socket or TCP.
//!
//! A door, the chaperone, `caregiver` and the noticeboard each call
//! `attendance` or another service of the platform. This client is the
//! transport of each such call. What an answer means stays in the service.
//!
//! The client makes one connection for each request and holds no pool. A
//! restart of the server then leaves no dead connection behind.
//!
//! Each call has its limits. [`Timeouts`] holds one limit for each phase:
//!
//! - The connect limit is the time of the connect.
//! - The write limit is the time of the write of the request, from its first
//!   byte to its last byte.
//! - The read limit is the longest time that the client waits for the next
//!   bytes of the answer. It starts when the request is written. It is not
//!   the time of the whole answer.
//!
//! [`Client::send`] also takes one limit for the whole call. Only a stream
//! that takes [`StreamLimit::UntilShutdown`] has no total limit.
//!
//! One future holds the connection and the request, and no task runs behind
//! a call. A caller that drops the future of [`Client::send`], or a
//! [`ReplyStream`], closes the socket at once. That close tells `attendance`
//! that its client left.
//!
//! The client follows no redirect and reads no proxy variable of the
//! environment. It asks for no compressed answer. It has no TLS: a [`Target`]
//! from an `https` URL is [`TargetError::TlsNotSupported`].
//!
//! No [`ClientError`] holds a header value or a byte of a body. A caller can
//! write the error to a log or onto a page.
//!
//! A call needs the runtime that `service::run` builds: a runtime of `tokio`
//! with the timer and the I/O driver. On a thread with no such runtime, a
//! call gives [`ClientError::Connect`] and does not panic.
//!
//! The Python services use `httpx` at ten call sites, for example
//! `noticeboard/src/noticeboard/attendancehttp.py` and the class
//! `HttpAttendance` of `door-owui/src/agent_door_owui/attendance.py`. This
//! module is a new design and not a translation of `httpx`. The doc comment
//! of a function says where the function differs from `httpx` 0.28.1.

use std::error::Error;
use std::fmt;
use std::fmt::Write as _;
use std::future::{Future, poll_fn};
use std::io;
use std::net::Ipv6Addr;
use std::panic::{self, AssertUnwindSafe};
use std::pin::{Pin, pin};
use std::sync::{Arc, OnceLock};
use std::task::{Context, Poll};
use std::time::Duration;

use ::http::header::{
    AUTHORIZATION, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, HOST, TRANSFER_ENCODING,
};
use ::http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, Uri};
use bytes::Bytes;
use creche_contracts::config::{
    AttendanceTarget, BindAddress, BindHost, HttpUrl, Port, SocketPath,
};
use creche_contracts::secret::Secret;
use http_body_util::{BodyExt, Full};
use hyper::body::{Body as _, Incoming};
use hyper::client::conn::http1::{self, Connection, SendRequest};
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{TcpStream, UnixStream};
use tokio::runtime::Handle;
use tokio::time::Sleep;

use crate::readfile::{ByteCap, os_text};
use crate::tasks::Shutdown;
use crate::token::BEARER;

/// The host of the `Host` header for `attendance` behind its socket. Each
/// Python client gives the socket the base URL `http://sessiond`, for example
/// `UDS_BASE_URL` of `door-owui/src/agent_door_owui/config.py:30`.
const ATTENDANCE_HOST: &str = "sessiond";

/// The scheme of a URL that the client can call.
const HTTP_SCHEME: &str = "http://";

/// The scheme of a URL that needs TLS.
const HTTPS_SCHEME: &str = "https://";

/// The port of an `http` URL that names none. The `Host` header of such a
/// target holds no port.
const DEFAULT_PORT: u16 = 80;

/// The content type of a [`Body::Json`].
const JSON: &str = "application/json";

/// The value of the `Connection` header of each request: the client uses a
/// connection one time.
const CLOSE: &str = "close";

/// The headers that the client writes itself, and the one header that
/// changes how a peer reads the end of a body. A [`Request`] holds no header
/// of a caller with one of these names.
const OWN_HEADERS: [HeaderName; 6] = [
    HOST,
    AUTHORIZATION,
    CONTENT_TYPE,
    CONTENT_LENGTH,
    CONNECTION,
    TRANSFER_ENCODING,
];

/// The methods whose request with no body gets the header
/// `content-length: 0`.
const LENGTH_METHODS: [Method; 3] = [Method::POST, Method::PUT, Method::PATCH];

/// What [`ClientError::Connect`] says on a thread with no runtime of `tokio`.
const NO_RUNTIME: &str = "no runtime runs on this thread";

/// What [`ClientError::Connect`] says in a runtime with no timer.
const NO_TIMER: &str = "the runtime of this thread has no timer";

/// What [`ClientError::Connect`] says in a runtime with no I/O driver.
const NO_IO_DRIVER: &str = "the runtime of this thread has no I/O driver";

/// How the client reaches a service. [`connect`] is the one function that
/// reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Dial {
    /// A Unix socket.
    Unix(SocketPath),
    /// A host and a port, over TCP. The host is a name or an IP address with
    /// no brackets.
    Tcp { host: String, port: u16 },
    /// A peer that completes no connect and refuses none. Only a test builds
    /// it. No listener gives that connect on each operating system: the
    /// system completes a connect up to the backlog of the listener.
    #[cfg(test)]
    Silent,
}

/// Where a service answers: a Unix socket, or a host and a port.
///
/// A service builds each target at its start, before the first socket opens.
/// It gives a [`TargetError`] to `service::refuse_start`.
///
/// A target of a URL keeps three parts of it: the host and the port of the
/// connect, the value of the `Host` header, and the path of the URL as the
/// start of each request path. The Python origin is the `base_url` of an
/// `httpx` client, for example `build_client` of
/// `noticeboard/src/noticeboard/attendancehttp.py:70-79`.
///
/// ```
/// use creche_contracts::config::{HttpUrl, SocketPath};
/// use creche_runtime::http::client::{Target, TargetError};
///
/// let socket: SocketPath = "/srv/agents/state/rework/sock/sessiond.sock".parse()?;
/// let by_socket = Target::unix(socket, "sessiond");
///
/// let url: HttpUrl = "http://192.0.2.10:8350/".parse()?;
/// let by_url = Target::try_from(&url)?;
/// assert_ne!(by_socket, by_url);
///
/// let tls: HttpUrl = "https://192.0.2.10:8350".parse()?;
/// assert_eq!(Target::try_from(&tls), Err(TargetError::TlsNotSupported));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change a part of a target. Each target
/// comes from a `SocketPath`, a `BindAddress` or an `HttpUrl`:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::{HttpUrl, SocketPath};
/// use creche_runtime::http::client::{Target, TargetError};
///
/// fn moved(target: Target) -> Target {
///     Target {
///         prefix: String::from("/admin"),
///         ..target
///     }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    dial: Dial,
    /// The value of the `Host` header.
    host: HeaderValue,
    /// The path of the base URL: the start of each request path. It is
    /// empty, or it starts with `/` and has no final `/`.
    prefix: String,
}

impl Target {
    /// The service behind the Unix socket `socket`. `host` is the value of
    /// the `Host` header of each request.
    ///
    /// A `host` with a control character is no value of a header. It gives
    /// an empty `Host` header, which HTTP/1.1 permits for a target with no
    /// authority.
    ///
    /// The Python origin is a client with `httpx.HTTPTransport(uds=...)` and
    /// a base URL that names nothing, for example `over_socket` of
    /// `caregiver/src/caregiver/switch.py:116-125`.
    #[must_use]
    pub fn unix(socket: SocketPath, host: &'static str) -> Self {
        Self {
            dial: Dial::Unix(socket),
            host: header_text(host),
            prefix: String::new(),
        }
    }

    /// The service at `host` and `port`, over TCP.
    ///
    /// The `Host` header is the one of `httpx`: a name in lower case, an
    /// IPv6 address in brackets, and no port for port 80.
    fn tcp(host: &BindHost, port: u16, prefix: String) -> Self {
        let text = host.as_str();
        // Only an IPv6 address holds a colon. `httpx` keeps its letters as
        // they are.
        let is_address = text.contains(':');
        let host = if is_address {
            text.to_owned()
        } else {
            text.to_ascii_lowercase()
        };
        let mut header = if is_address {
            format!("[{host}]")
        } else {
            host.clone()
        };
        if port != DEFAULT_PORT {
            // A write to a `String` gives no error.
            let _ = write!(header, ":{port}");
        }

        Self {
            dial: Dial::Tcp { host, port },
            host: header_text(&header),
            prefix,
        }
    }
}

impl From<&BindAddress> for Target {
    /// The service that binds `address`, over TCP.
    ///
    /// The Python origins are the verify hooks, which call the address that
    /// the service binds: `_healthy` of
    /// `noticeboard/src/noticeboard/verify.py:147-159` and of
    /// `chaperone/src/chaperone/verify.py:212-229`.
    fn from(address: &BindAddress) -> Self {
        Self::tcp(address.host(), address.port().get(), String::new())
    }
}

impl TryFrom<&HttpUrl> for Target {
    type Error = TargetError;

    /// The service at the base URL `url`, over TCP. The target keeps the path
    /// of the URL as the start of each request path.
    ///
    /// The path loses each `.` segment, each `..` segment with the segment
    /// before it, and each final `/`. A URL with no port has port 80.
    ///
    /// The Python origin is the `base_url` of an `httpx` client:
    /// `build_client` of `noticeboard/src/noticeboard/attendancehttp.py:70-79`
    /// and `_build_client` of
    /// `door-trigger/src/agent_door_trigger/attendance.py:131-134`.
    ///
    /// This function refuses four forms of a URL that `httpx` takes: a query,
    /// a fragment, a port that is not 1 to 65535 in ASCII digits, and a host
    /// that is no `BindHost`. `httpx` keeps a second final `/` of the path,
    /// and this function removes each final `/`, as `HttpUrl::base` does.
    ///
    /// # Errors
    ///
    /// [`TargetError::TlsNotSupported`] for an `https` URL.
    /// [`TargetError::NotAnAddress`] for a URL with a query or a fragment,
    /// and for a URL whose host or port the client cannot connect to.
    fn try_from(url: &HttpUrl) -> Result<Self, Self::Error> {
        let text = url.as_str();
        if text.starts_with(HTTPS_SCHEME) {
            return Err(TargetError::TlsNotSupported);
        }

        let rest = text
            .strip_prefix(HTTP_SCHEME)
            .ok_or(TargetError::NotAnAddress)?;
        // CONTRACT-QUESTION: no contract gives the base URL of a service a
        // grammar (contract 02 §3 rules 1 and 2 name only the socket and the
        // LAN address). `HttpUrl` checks only the scheme, the user part and
        // that a host is there. `httpx` takes more than a client can call.
        // This reading refuses each such URL at the start of the service: a
        // query, a fragment, a port that is not 1 to 65535 in ASCII digits,
        // and a host that is no `BindHost`. A laxer reading costs the check
        // below and the two functions `host_and_port` and `port_of`.
        if rest.contains(['?', '#']) {
            return Err(TargetError::NotAnAddress);
        }

        let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
        let (host, port) = host_and_port(authority).ok_or(TargetError::NotAnAddress)?;

        Ok(Self::tcp(&host, port, base_prefix(path)))
    }
}

impl TryFrom<&AttendanceTarget> for Target {
    type Error = TargetError;

    /// `attendance`, at the socket or at the URL of the config of a door.
    ///
    /// The `Host` header of the socket is `sessiond`, as each Python door
    /// writes it.
    ///
    /// The Python origins are `_build_client` of
    /// `door-owui/src/agent_door_owui/attendance.py:221-228`, of
    /// `door-trigger/src/agent_door_trigger/attendance.py:131-134` and of
    /// `door-tui/src/agent_door_tui/attendance.py:360-367`.
    ///
    /// # Errors
    ///
    /// [`TargetError`] for a URL that is no target, as
    /// `Target::try_from(&HttpUrl)` gives it.
    fn try_from(target: &AttendanceTarget) -> Result<Self, Self::Error> {
        match target {
            AttendanceTarget::Socket(socket) => Ok(Self::unix(socket.clone(), ATTENDANCE_HOST)),
            AttendanceTarget::Url(url) => Self::try_from(url),
        }
    }
}

/// The value of a header from a text of the code or of a config. A text that
/// a header cannot hold gives the empty value.
fn header_text(text: &str) -> HeaderValue {
    HeaderValue::from_str(text).unwrap_or_else(|_| HeaderValue::from_static(""))
}

/// The host and the port of the authority of a URL. `None` for a text that
/// is not `host`, `host:port`, `[address]` or `[address]:port`.
///
/// The host has the grammar of [`BindHost`]: an IPv4 address, a host name, or
/// an IPv6 address. A URL writes an IPv6 address in brackets, and only there.
/// The port is 1 to 65535 in ASCII digits.
fn host_and_port(authority: &str) -> Option<(BindHost, u16)> {
    let (host, port) = match authority.strip_prefix('[') {
        Some(rest) => {
            let (address, after) = rest.split_once(']')?;
            address.parse::<Ipv6Addr>().ok()?;
            let port = match after {
                "" => None,
                _ => Some(after.strip_prefix(':')?),
            };

            (address, port)
        }
        None => {
            let (host, port) = match authority.rsplit_once(':') {
                Some((host, port)) => (host, Some(port)),
                None => (authority, None),
            };
            if host.contains(':') {
                return None;
            }

            (host, port)
        }
    };
    let host = host.parse::<BindHost>().ok()?;

    match port {
        Some(port) => Some((host, port_of(port)?)),
        None => Some((host, DEFAULT_PORT)),
    }
}

/// The port of a URL: 1 to 65535, in ASCII digits only. [`Port`] holds the
/// range. It also takes a sign, a space and an underscore, as `int` of
/// Python does, so this function checks the digits first.
fn port_of(text: &str) -> Option<u16> {
    if text.is_empty() || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }

    text.parse::<Port>().ok().map(Port::get)
}

/// The start of each request path, from the path of a base URL. `path` is
/// the text after the first `/` of the URL.
///
/// The function removes each `.` segment, and each `..` segment with the
/// segment before it, as `httpx` does for a `base_url`. It removes each final
/// `/`, as `HttpUrl::base` does. It keeps each byte that `httpx` keeps in a
/// path, a `%` too, and writes each other byte as `%XX`.
fn base_prefix(path: &str) -> String {
    let mut kept: Vec<&str> = Vec::new();
    for segment in path.split('/') {
        match segment {
            "." => {}
            ".." => {
                kept.pop();
            }
            _ => kept.push(segment),
        }
    }
    while kept.last().is_some_and(|segment| segment.is_empty()) {
        kept.pop();
    }

    let mut prefix = String::new();
    for segment in kept {
        prefix.push('/');
        for byte in segment.bytes() {
            if stays_in_base_path(byte) {
                prefix.push(char::from(byte));
            } else {
                push_escape(&mut prefix, byte);
            }
        }
    }

    prefix
}

/// Whether `httpx` writes `byte` of a base path as it is. The set is each
/// printable ASCII character but the space and these eight: `"`, `#`, `<`,
/// `>`, `?`, `` ` ``, `{` and `}` (`PATH_SAFE` of `httpx` 0.28.1,
/// `_urlparse.py`). The request line of the `http` crate takes each byte of
/// the set.
const fn stays_in_base_path(byte: u8) -> bool {
    matches!(byte, b'!' | b'$'..=b';' | b'=' | b'@'..=b'_' | b'a'..=b'z' | b'|' | b'~')
}

/// Whether `byte` is in the unreserved set of RFC 3986: a character that a
/// path and a query hold as it is.
const fn is_unreserved(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~')
}

/// Adds `byte` to `text` as `%XX`, with the digits in upper case.
fn push_escape(text: &mut String, byte: u8) {
    // A write to a `String` gives no error.
    let _ = write!(text, "%{byte:02X}");
}

/// Why a URL is not a target of this client.
///
/// The set is closed. The type has no Python origin: `httpx` takes each URL
/// of the config, and a call to a URL that it cannot use fails later.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetError {
    /// The URL starts with `https://`. The client has no TLS.
    TlsNotSupported,
    /// The URL holds no host and port that the client can connect to. A URL
    /// with a query or with a fragment is in this case too: it is no base of
    /// a request path.
    NotAnAddress,
}

impl fmt::Display for TargetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::TlsNotSupported => "the URL is an https URL, and the client has no TLS",
            Self::NotAnAddress => "the URL holds no host and port to connect to",
        })
    }
}

impl Error for TargetError {}

/// The time limit of each phase of one request.
///
/// These are the phases of an `httpx.Timeout` of Python. The read limit is
/// the longest time between two chunks of the answer, and not the time of the
/// whole answer. `None` there means no limit: a stream of `attendance` sends
/// a heartbeat, and its reader waits for each one.
///
/// The Python origin is the `httpx.Timeout` of each call, for example
/// `door-owui/src/agent_door_owui/attendance.py:165` and `:193`. An
/// `httpx.Timeout` also has a limit for the wait for a connection of the
/// pool. This client has no pool, so this type has no such limit.
///
/// ```
/// use std::time::Duration;
///
/// use creche_runtime::http::client::Timeouts;
///
/// // `httpx.Timeout(5.0, read=120.0)` of Python.
/// let timeouts = Timeouts::each(Duration::from_secs(5))
///     .with_read_idle(Some(Duration::from_secs(120)));
///
/// assert_eq!(timeouts.connect(), Duration::from_secs(5));
/// assert_eq!(timeouts.write(), Duration::from_secs(5));
/// assert_eq!(timeouts.read_idle(), Some(Duration::from_secs(120)));
/// ```
///
/// Code outside this module cannot build the limits from raw values. It
/// starts with [`Timeouts::each`], so no phase is without a decided limit:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use creche_runtime::http::client::Timeouts;
///
/// let timeouts = Timeouts {
///     connect: Duration::from_secs(5),
///     write: Duration::from_secs(5),
///     read_idle: None,
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Timeouts {
    connect: Duration,
    write: Duration,
    read_idle: Option<Duration>,
}

impl Timeouts {
    /// The same limit for each phase: `httpx.Timeout(limit)` of Python.
    #[must_use]
    pub const fn each(limit: Duration) -> Self {
        Self {
            connect: limit,
            write: limit,
            read_idle: Some(limit),
        }
    }

    /// The limits with this limit for the connect: `connect=` of an
    /// `httpx.Timeout`.
    #[must_use]
    pub const fn with_connect(mut self, limit: Duration) -> Self {
        self.connect = limit;

        self
    }

    /// The limits with this limit for the write of the request: `write=` of
    /// an `httpx.Timeout`.
    #[must_use]
    pub const fn with_write(mut self, limit: Duration) -> Self {
        self.write = limit;

        self
    }

    /// The limits with this limit for the wait for the head of the answer
    /// and for each chunk after it: `read=` of an `httpx.Timeout`. `None` for
    /// no limit.
    #[must_use]
    pub const fn with_read_idle(mut self, limit: Option<Duration>) -> Self {
        self.read_idle = limit;

        self
    }

    /// The limit for the connect.
    #[must_use]
    pub const fn connect(&self) -> Duration {
        self.connect
    }

    /// The limit for the write of the request.
    #[must_use]
    pub const fn write(&self) -> Duration {
        self.write
    }

    /// The limit for the wait for the head of the answer and for each chunk
    /// after it. `None` for no limit.
    #[must_use]
    pub const fn read_idle(&self) -> Option<Duration> {
        self.read_idle
    }
}

/// The path and the query of a request, with each part percent-encoded.
///
/// The caller gives each segment and each query value as plain text. No
/// caller writes a `/` or a `?` by hand, so an id from a request cannot add a
/// segment or a parameter.
///
/// A path keeps the unreserved characters of RFC 3986: an ASCII letter, a
/// digit, `-`, `.`, `_` and `~`. Each other byte is `%XX`. A segment that is
/// `.` or `..` is `%2E` or `%2E%2E`, so no party reads it as a step to
/// another directory. A query has the same rule, and a space is `+` there, as
/// `httpx` writes it.
///
/// The Python clients write a path with an f-string and give the query as
/// `params`, for example `any_live` of
/// `door-trigger/src/agent_door_trigger/attendance.py:113-128`.
///
/// ```
/// use creche_runtime::http::client::PathAndQuery;
///
/// let list = PathAndQuery::from_segments(&["v1", "sessions"]).with_query(&[("family", "chat")]);
/// assert_ne!(list, PathAndQuery::from_segments(&["v1", "sessions"]));
///
/// // A `/` in an id stays in its segment.
/// assert_ne!(
///     PathAndQuery::from_segments(&["v1", "a/b"]),
///     PathAndQuery::from_segments(&["v1", "a", "b"])
/// );
/// ```
///
/// Code outside this module cannot build a path from a raw text:
///
/// ```compile_fail,E0451
/// use creche_runtime::http::client::PathAndQuery;
///
/// let path = PathAndQuery {
///     target: String::from("/v1/../admin?x=1"),
///     ..PathAndQuery::from_segments(&[])
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathAndQuery {
    /// The path, and then the query when the value has one.
    target: String,
    /// Whether `target` holds a query.
    has_query: bool,
}

impl PathAndQuery {
    /// The path with one `/` before each of `segments`. With no segment, the
    /// path is `/`.
    ///
    /// The Python origin is the f-string of a path, for example
    /// `_turns_path` of `door-owui/src/agent_door_owui/attendance.py:243-244`.
    /// In that f-string, a `/` in an id starts a new segment, and `httpx`
    /// removes a `.` segment and a `..` segment. This function keeps each
    /// segment whole. For each id that the contracts permit, the two write
    /// the same bytes.
    #[must_use]
    pub fn from_segments(segments: &[&str]) -> Self {
        let mut target = String::new();
        for segment in segments {
            target.push('/');
            if matches!(*segment, "." | "..") {
                for _ in segment.bytes() {
                    push_escape(&mut target, b'.');
                }
                continue;
            }

            for byte in segment.bytes() {
                if is_unreserved(byte) {
                    target.push(char::from(byte));
                } else {
                    push_escape(&mut target, byte);
                }
            }
        }
        if target.is_empty() {
            target.push('/');
        }

        Self {
            target,
            has_query: false,
        }
    }

    /// The path with these query pairs, in order. A second call adds its
    /// pairs after the pairs of the first one.
    ///
    /// The Python origin is the `params` of an `httpx` call, for example in
    /// `HttpTransport.get` of
    /// `noticeboard/src/noticeboard/attendancehttp.py:45-54`. The function
    /// writes the bytes that `httpx` writes.
    #[must_use]
    pub fn with_query(mut self, pairs: &[(&str, &str)]) -> Self {
        for (name, value) in pairs {
            self.target.push(if self.has_query { '&' } else { '?' });
            self.has_query = true;
            push_query_text(&mut self.target, name);
            self.target.push('=');
            push_query_text(&mut self.target, value);
        }

        self
    }

    /// The target of the request line: the base path of a [`Target`], and
    /// then this path and its query.
    fn after(&self, prefix: &str) -> String {
        format!("{prefix}{}", self.target)
    }
}

/// Adds the name or the value of a query pair to `text`.
fn push_query_text(text: &mut String, part: &str) {
    for byte in part.bytes() {
        if is_unreserved(byte) {
            text.push(char::from(byte));
        } else if byte == b' ' {
            text.push('+');
        } else {
            push_escape(text, byte);
        }
    }
}

/// The body of a request.
///
/// `Debug` prints no byte and no count of the bytes: a body can hold a prompt
/// or a key, and the count can give the length of that key.
///
/// The Python origin is the `json` argument of an `httpx` call, for example
/// in `ensure_session` of
/// `door-trigger/src/agent_door_trigger/attendance.py:89-95`. `httpx` writes
/// the JSON text of that argument. Here the caller writes the text.
#[derive(Clone, PartialEq, Eq)]
pub enum Body {
    /// No body.
    Empty,
    /// These bytes, with the content type `application/json`. The caller
    /// writes the JSON text.
    Json(Vec<u8>),
}

impl fmt::Debug for Body {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("Empty"),
            Self::Json(_) => f.write_str("Json(..)"),
        }
    }
}

/// One request.
///
/// The bearer is a [`Secret`], and the client is the one place of this crate
/// that reads its bytes. `Debug` prints no byte of the bearer and no byte of
/// the body.
///
/// A new request has no bearer, no header of the caller and no body. The
/// method, the path and the limits have no default.
///
/// The Python origin is the argument list of an `httpx` call, for example in
/// `_call` of `door-tui/src/agent_door_tui/attendance.py:275-297`.
///
/// ```
/// use std::time::Duration;
///
/// use creche_contracts::secret::Secret;
/// use creche_runtime::http::client::{Body, PathAndQuery, Request, Timeouts};
/// use http::{HeaderName, HeaderValue, Method};
///
/// let token = Secret::try_from(String::from("correct-horse-battery-staple-0123"))?;
/// let request = Request::new(
///     Method::POST,
///     PathAndQuery::from_segments(&["v1", "sessions"]),
///     Timeouts::each(Duration::from_secs(5)),
/// )
/// .with_bearer(&token)
/// .with_headers(vec![(
///     HeaderName::from_static("x-door-instance"),
///     HeaderValue::from_static("tui.4711"),
/// )])
/// .with_body(Body::Json(br#"{"family":"chat"}"#.to_vec()));
///
/// assert_eq!(request.method(), Method::POST);
/// assert_eq!(request.headers().len(), 1);
/// assert!(request.bearer().is_some());
/// assert!(!format!("{request:?}").contains("correct-horse"));
/// # Ok::<(), creche_contracts::secret::SecretError>(())
/// ```
///
/// Code outside this module cannot build a request from raw values, so no
/// request holds a header that the client writes itself:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use creche_contracts::secret::Secret;
/// use creche_runtime::http::client::{Body, PathAndQuery, Request, Timeouts};
/// use http::{HeaderName, HeaderValue, Method};
///
/// let request = Request {
///     method: Method::POST,
///     path: PathAndQuery::from_segments(&["v1", "sessions"]),
///     bearer: None,
///     headers: vec![(
///         HeaderName::from_static("host"),
///         HeaderValue::from_static("other"),
///     )],
///     body: Body::Empty,
///     timeouts: Timeouts::each(Duration::from_secs(5)),
/// };
/// ```
#[derive(Debug, Clone)]
pub struct Request<'a> {
    method: Method,
    path: PathAndQuery,
    /// The token of the `Authorization` header. `None` for a request with no
    /// such header.
    bearer: Option<&'a Secret>,
    /// Each header of the caller. No header here has a name of
    /// [`OWN_HEADERS`].
    headers: Vec<(HeaderName, HeaderValue)>,
    body: Body,
    timeouts: Timeouts,
}

impl<'a> Request<'a> {
    /// A request with this method, this path and these limits. It has no
    /// bearer, no header of the caller and no body.
    #[must_use]
    pub const fn new(method: Method, path: PathAndQuery, timeouts: Timeouts) -> Self {
        Self {
            method,
            path,
            bearer: None,
            headers: Vec::new(),
            body: Body::Empty,
            timeouts,
        }
    }

    /// The request with `bearer` as the token of its `Authorization` header.
    #[must_use]
    pub const fn with_bearer(mut self, bearer: &'a Secret) -> Self {
        self.bearer = Some(bearer);

        self
    }

    /// The request with these headers of the caller. A second call replaces
    /// the headers of the first one.
    ///
    /// The client writes `Host`, `Authorization`, `Content-Type`,
    /// `Content-Length` and `Connection` itself. The function drops a header
    /// with one of those names, and one with the name `Transfer-Encoding`. A
    /// caller thus cannot replace the token or change where a body ends.
    ///
    /// The client writes the headers of the caller after its own headers. It
    /// writes the headers of one name together, at the place of the first
    /// one.
    #[must_use]
    pub fn with_headers(mut self, headers: Vec<(HeaderName, HeaderValue)>) -> Self {
        self.headers = headers
            .into_iter()
            .filter(|(name, _)| !OWN_HEADERS.contains(name))
            .collect();

        self
    }

    /// The request with this body.
    #[must_use]
    pub fn with_body(mut self, body: Body) -> Self {
        self.body = body;

        self
    }

    /// The method.
    #[must_use]
    pub const fn method(&self) -> &Method {
        &self.method
    }

    /// The path and the query.
    #[must_use]
    pub const fn path(&self) -> &PathAndQuery {
        &self.path
    }

    /// The token of the `Authorization` header. `None` for a request with no
    /// such header.
    #[must_use]
    pub const fn bearer(&self) -> Option<&'a Secret> {
        self.bearer
    }

    /// Each header of the caller that the client sends.
    #[must_use]
    pub fn headers(&self) -> &[(HeaderName, HeaderValue)] {
        &self.headers
    }

    /// The body.
    #[must_use]
    pub const fn body(&self) -> &Body {
        &self.body
    }

    /// The limit of each phase.
    #[must_use]
    pub const fn timeouts(&self) -> Timeouts {
        self.timeouts
    }
}

/// When a stream of [`Client::open`] ends at the latest.
///
/// The stream of a Python door has no such end: its `httpx` call has no read
/// limit and no total limit
/// (`door-owui/src/agent_door_owui/attendance.py:188-194`).
#[derive(Debug, Clone)]
pub enum StreamLimit {
    /// The whole stream ends inside this time.
    Within(Duration),
    /// The stream has no total limit. It ends at the stop signal of the
    /// process.
    UntilShutdown(Shutdown),
}

/// The client of one [`Target`].
///
/// A clone is a client of the same target. The value holds no connection:
/// each call makes its own and closes it.
///
/// The Python origin is the `httpx` client of each service, for example
/// `HttpTransport` of `noticeboard/src/noticeboard/attendancehttp.py:39-67`.
/// One `httpx` client holds a pool and uses a connection for more than one
/// request.
///
/// ```
/// use std::time::Duration;
///
/// use creche_contracts::config::SocketPath;
/// use creche_runtime::http::client::{Client, PathAndQuery, Request, Target, Timeouts};
/// use creche_runtime::readfile::ByteCap;
/// use creche_testkit::root::TempRoot;
/// use creche_testkit::stub::{Answer, HttpStub};
/// use http::{Method, StatusCode};
///
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
/// runtime.block_on(async {
///     // A stub stands in for `attendance` on its socket.
///     let root = TempRoot::new()?;
///     let stub = HttpStub::unix(&root).await?;
///     stub.script(Answer::status(StatusCode::OK).body(r#"{"sessions":[]}"#));
///     let socket: SocketPath = stub
///         .socket()
///         .and_then(|path| path.to_str())
///         .unwrap_or_default()
///         .parse()?;
///
///     let client = Client::new(Target::unix(socket, "sessiond"));
///     let request = Request::new(
///         Method::GET,
///         PathAndQuery::from_segments(&["v1", "sessions"]).with_query(&[("family", "chat")]),
///         Timeouts::each(Duration::from_secs(5)),
///     );
///     let reply = client
///         .send(request, ByteCap::ONE_MIB, Duration::from_secs(15))
///         .await?;
///
///     assert_eq!(reply.status(), StatusCode::OK);
///     assert_eq!(reply.body(), br#"{"sessions":[]}"#);
///     let sent = stub.requests();
///     let target = sent.first().map(|request| request.target());
///     assert_eq!(target, Some("/v1/sessions?family=chat"));
///
///     Ok::<(), Box<dyn std::error::Error>>(())
/// })?;
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot give a client another target:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use creche_contracts::config::SocketPath;
/// use creche_runtime::http::client::{Client, PathAndQuery, Request, Target, Timeouts};
/// use creche_runtime::readfile::ByteCap;
/// use creche_testkit::root::TempRoot;
/// use creche_testkit::stub::{Answer, HttpStub};
/// use http::{Method, StatusCode};
///
/// fn moved(client: Client, target: Target) -> Client {
///     Client { target }
/// }
/// ```
#[derive(Debug, Clone)]
pub struct Client {
    target: Target,
}

impl Client {
    /// A client of `target`. The call opens no connection.
    #[must_use]
    pub const fn new(target: Target) -> Self {
        Self { target }
    }

    /// Sends `request` and reads the whole answer, up to `cap` bytes of body
    /// and inside `total`.
    ///
    /// A caller that drops the future closes the connection.
    ///
    /// The steps are these, and `total` covers each one:
    ///
    /// 1. The connect, under the connect limit.
    /// 2. The write of the request, under the write limit.
    /// 3. The read of the head and of each chunk, each under the read limit.
    ///
    /// The Python origins are the calls with a whole answer, for example
    /// `HttpTransport.get` of
    /// `noticeboard/src/noticeboard/attendancehttp.py:45-67`, `_post` of
    /// `door-owui/src/agent_door_owui/attendance.py:211-218` and `switch` of
    /// `caregiver/src/caregiver/switch.py:127-140`. Such a call has no limit
    /// for the whole call and reads a body of each size. Only the delegate
    /// call has a total limit, the `asyncio.wait_for` of
    /// `chaperone/src/chaperone/delegate.py:276-279`.
    ///
    /// # Errors
    ///
    /// [`ClientError`] when the call gives no answer. An answer with each
    /// status is a [`Reply`] and no error.
    pub async fn send(
        &self,
        request: Request<'_>,
        cap: ByteCap,
        total: Duration,
    ) -> Result<Reply, ClientError> {
        let (wire, timeouts) = self.wire(request)?;
        let limit = timer(total)?;
        let answer = async {
            let opened = self.begin(wire, timeouts).await?;

            opened.whole(cap).await
        };

        tokio::select! {
            biased;
            reply = answer => reply,
            () = limit => Err(ClientError::TimedOut(Phase::Total)),
        }
    }

    /// Sends `request` and returns after the head of the answer. The caller
    /// reads the body with [`ReplyStream::chunk`].
    ///
    /// `limit` starts at this call. With a stop signal that was triggered
    /// before the call, the function opens no connection.
    ///
    /// The Python origin is `stream_turn` of
    /// `door-owui/src/agent_door_owui/attendance.py:183-209`.
    ///
    /// # Errors
    ///
    /// [`ClientError`] when the call gives no head.
    pub async fn open(
        &self,
        request: Request<'_>,
        limit: StreamLimit,
    ) -> Result<ReplyStream, ClientError> {
        let (wire, timeouts) = self.wire(request)?;
        let mut life = Life::new(limit)?;
        let opened = tokio::select! {
            biased;
            error = life.ended() => Err(error),
            opened = self.begin(wire, timeouts) => opened,
        }?;

        Ok(ReplyStream {
            status: opened.status,
            headers: opened.headers,
            flow: Flow::Open(Box::new(Reading {
                body: opened.body,
                link: opened.link,
                _sender: opened.sender,
            })),
            life,
        })
    }

    /// The request as hyper sends it, and its limits. The function opens no
    /// connection, so a request that the client refuses reaches no peer.
    ///
    /// The headers are in this order: `host`, `authorization`,
    /// `content-type`, `content-length`, `connection`, and then each header
    /// of the caller. The headers of one name are together, at the place of
    /// the first one.
    ///
    /// `httpx` also writes the headers `Accept`, `Accept-Encoding` and
    /// `User-Agent`, and its `Connection` header is `keep-alive`. This
    /// function writes none of the three, and its `Connection` header is
    /// `close`.
    fn wire(&self, request: Request<'_>) -> Result<(Wire, Timeouts), ClientError> {
        let authorization = request.bearer.map(bearer_value).transpose()?;
        // The parts of a target are a base path and a `PathAndQuery`, and
        // each one holds only bytes that a request line can hold. `Uri`
        // refuses a target of more than 65,534 bytes.
        let target = Uri::try_from(request.path.after(&self.target.prefix))
            .map_err(|_| ClientError::Protocol)?;
        let (payload, content_type, length) = match request.body {
            Body::Json(bytes) => {
                let length = bytes.len();

                (Bytes::from(bytes), Some(JSON), Some(length))
            }
            // `httpx` gives three methods the length 0, and no length to a
            // request with another method.
            Body::Empty => {
                let has_length = LENGTH_METHODS.contains(&request.method);

                (Bytes::new(), None, has_length.then_some(0))
            }
        };

        let mut wire = ::http::Request::new(Full::new(payload));
        *wire.method_mut() = request.method;
        *wire.uri_mut() = target;
        let headers = wire.headers_mut();
        headers.insert(HOST, self.target.host.clone());
        if let Some(authorization) = authorization {
            headers.insert(AUTHORIZATION, authorization);
        }
        if let Some(content_type) = content_type {
            headers.insert(CONTENT_TYPE, HeaderValue::from_static(content_type));
        }
        if let Some(length) = length {
            headers.insert(CONTENT_LENGTH, HeaderValue::from(length));
        }
        headers.insert(CONNECTION, HeaderValue::from_static(CLOSE));
        for (name, value) in request.headers {
            headers.append(name, value);
        }

        Ok((wire, request.timeouts))
    }

    /// Connects, sends `wire` and reads the head of the answer.
    async fn begin(&self, wire: Wire, timeouts: Timeouts) -> Result<Opened, ClientError> {
        let stream = dial(&self.target.dial, timeouts.connect).await?;
        let stall = Arc::new(OnceLock::new());
        let io = TokioIo::new(Watched::new(stream, timeouts, Arc::clone(&stall)));
        // The handshake of HTTP/1.1 reads and writes no byte.
        let (mut sender, conn) = http1::handshake(io)
            .await
            .map_err(|_| ClientError::Protocol)?;
        let mut link = Link {
            conn: Some(conn),
            stall,
        };

        let Some(Ok(answer)) = link.drive(sender.send_request(wire)).await else {
            return Err(link.failure());
        };
        let (head, body) = answer.into_parts();

        Ok(Opened {
            status: head.status,
            headers: head.headers,
            body,
            link,
            sender,
        })
    }
}

/// The request that hyper sends.
type Wire = ::http::Request<Full<Bytes>>;

/// The connection future of hyper for one request.
type Conn = Connection<TokioIo<Watched>, Full<Bytes>>;

/// The value of the `Authorization` header for `secret`.
///
/// The Python origin is the header of each call, for example
/// `HttpTransport.get` of
/// `noticeboard/src/noticeboard/attendancehttp.py:52`. `httpx` refuses a
/// token with a byte that is not ASCII, with a NUL, or with one of the four
/// characters from line feed to carriage return. It also refuses a token
/// with a space or a tab as its last byte (`attendancehttp.py:55-60`). This
/// function refuses each of those tokens. It also refuses each other control
/// character but the tab, and the byte 0x7F. `httpx` sends such a token: the
/// `http` crate takes no header value with such a byte.
fn bearer_value(secret: &Secret) -> Result<HeaderValue, ClientError> {
    // CONTRACT-QUESTION: contract 02 §3 rules 4 and 7 give a token a least
    // count of bytes and no set of bytes. A token of the platform is ASCII
    // text with no control character, so no writer of the platform makes a
    // token that this function refuses. This reading refuses the token before
    // the connect. A laxer reading needs a header type that takes a control
    // character.
    let token = secret.expose_secret();
    let fits = token.iter().all(|byte| matches!(byte, b' '..=b'~' | b'\t'));
    let open_end = token.last().is_none_or(|byte| matches!(byte, b' ' | b'\t'));
    if !fits || open_end {
        return Err(ClientError::BadBearer);
    }

    let mut value = Vec::with_capacity(BEARER.len().saturating_add(token.len()));
    value.extend_from_slice(BEARER);
    value.extend_from_slice(token);
    let mut value =
        HeaderValue::from_maybe_shared(Bytes::from(value)).map_err(|_| ClientError::BadBearer)?;
    // The `Debug` of a sensitive value prints no byte.
    value.set_sensitive(true);

    Ok(value)
}

/// The error of a call that cannot start, because the thread has no runtime
/// or the runtime lacks a driver. `lacks` says which driver.
fn not_started(lacks: &'static str) -> ClientError {
    let os_text = match Handle::try_current() {
        Ok(_) => lacks,
        Err(_) => NO_RUNTIME,
    };

    ClientError::Connect {
        os_text: os_text.to_owned(),
    }
}

/// A timer that ends after `limit`.
///
/// `tokio` panics when code makes a timer on a thread with no runtime, and
/// in a runtime with no timer. The function gives an error there. Each call
/// of the client makes its first timer here, so no later timer of the call
/// can panic for that reason.
fn timer(limit: Duration) -> Result<Pin<Box<Sleep>>, ClientError> {
    panic::catch_unwind(|| Box::pin(tokio::time::sleep(limit))).map_err(|_| not_started(NO_TIMER))
}

/// One connection to a service.
///
/// The handover plan adds a TLS stream as one more variant here and one more
/// variant of [`Dial`].
enum Stream {
    Unix(UnixStream),
    Tcp(TcpStream),
}

/// Opens the connection of one request. This is the one function of the
/// client that opens a socket.
///
/// The Python origin is the transport of an `httpx` client, for example
/// `_build_client` of
/// `door-trigger/src/agent_door_trigger/attendance.py:131-134`. For a target
/// with no socket, `httpx` reads the proxy variables of the environment and
/// can send the request to a proxy (`httpx` 0.28.1, `_client.py:201`). This
/// function reads no variable and connects only to the target.
async fn connect(dial: &Dial) -> io::Result<Stream> {
    match dial {
        Dial::Unix(socket) => UnixStream::connect(socket.as_path())
            .await
            .map(Stream::Unix),
        Dial::Tcp { host, port } => {
            let stream = TcpStream::connect((host.as_str(), *port)).await?;
            // The request then goes out at once, as with `httpx`. A stream
            // that refuses the option still works.
            let _ = stream.set_nodelay(true);

            Ok(Stream::Tcp(stream))
        }
        #[cfg(test)]
        Dial::Silent => std::future::pending().await,
    }
}

/// Connects under the connect limit.
async fn dial(to: &Dial, limit: Duration) -> Result<Stream, ClientError> {
    let mut connect = pin!(tokio::time::timeout(limit, connect(to)));
    // `tokio` panics at the first poll of a connect in a runtime with no I/O
    // driver. The code does not poll the future again after a panic.
    let connected =
        poll_fn(
            |cx| match panic::catch_unwind(AssertUnwindSafe(|| connect.as_mut().poll(cx))) {
                Ok(poll) => poll.map(Some),
                Err(_) => Poll::Ready(None),
            },
        )
        .await;

    match connected {
        Some(Ok(Ok(stream))) => Ok(stream),
        Some(Ok(Err(error))) => Err(ClientError::Connect {
            os_text: os_text(&error),
        }),
        Some(Err(_)) => Err(ClientError::TimedOut(Phase::Connect)),
        None => Err(not_started(NO_IO_DRIVER)),
    }
}

impl AsyncRead for Stream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match self.get_mut() {
            Self::Unix(stream) => Pin::new(stream).poll_read(cx, buffer),
            Self::Tcp(stream) => Pin::new(stream).poll_read(cx, buffer),
        }
    }
}

impl AsyncWrite for Stream {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        match self.get_mut() {
            Self::Unix(stream) => Pin::new(stream).poll_write(cx, bytes),
            Self::Tcp(stream) => Pin::new(stream).poll_write(cx, bytes),
        }
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        slices: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        match self.get_mut() {
            Self::Unix(stream) => Pin::new(stream).poll_write_vectored(cx, slices),
            Self::Tcp(stream) => Pin::new(stream).poll_write_vectored(cx, slices),
        }
    }

    fn is_write_vectored(&self) -> bool {
        match self {
            Self::Unix(stream) => stream.is_write_vectored(),
            Self::Tcp(stream) => stream.is_write_vectored(),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            Self::Unix(stream) => Pin::new(stream).poll_flush(cx),
            Self::Tcp(stream) => Pin::new(stream).poll_flush(cx),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            Self::Unix(stream) => Pin::new(stream).poll_shutdown(cx),
            Self::Tcp(stream) => Pin::new(stream).poll_shutdown(cx),
        }
    }
}

/// One limit of a socket, and the timer of the wait that runs now.
struct Wait {
    /// The longest wait. `None` for no limit.
    limit: Option<Duration>,
    /// The timer of the wait. `None` while no wait runs.
    timer: Option<Pin<Box<Sleep>>>,
}

impl Wait {
    const fn new(limit: Option<Duration>) -> Self {
        Self { limit, timer: None }
    }

    /// Whether a wait runs.
    const fn runs(&self) -> bool {
        self.timer.is_some()
    }

    /// Ends the wait.
    fn stop(&mut self) {
        self.timer = None;
    }

    /// Starts a wait when none runs, and says whether the wait passed its
    /// limit. The task of `cx` gets a wake at the limit.
    fn passed(&mut self, cx: &mut Context<'_>) -> bool {
        let Some(limit) = self.limit else {
            return false;
        };

        self.timer
            .get_or_insert_with(|| Box::pin(tokio::time::sleep(limit)))
            .as_mut()
            .poll(cx)
            .is_ready()
    }
}

/// The connection of one request, with the write limit and the read limit of
/// its socket.
///
/// hyper reads and writes the socket through this type. A request whose
/// write takes longer than the write limit fails, and a read that waits
/// longer than the read limit fails. The type then keeps the phase in
/// `stall`: hyper gives its caller an error with no reason that this crate
/// can read.
///
/// The wait of the write starts at the first write of the request. It ends
/// when hyper flushes the socket: hyper does that only after it wrote each
/// byte that it holds, and it holds the whole request before its first write.
///
/// A read waits from the first read that finds no byte. While the write of
/// the request runs, the peer has no whole request and cannot answer, so the
/// read limit does not run. It starts when the write ends.
///
/// The Python origin is the `httpx.Timeout` of each call. `httpx` gives the
/// write limit to the send of one buffer and the read limit to one read of
/// the socket, for example in
/// `door-owui/src/agent_door_owui/attendance.py:193`.
struct Watched {
    stream: Stream,
    /// The wait for the end of the write of the request.
    write: Wait,
    /// The wait for the next bytes of the answer.
    read: Wait,
    /// Whether the last read found no byte.
    read_waits: bool,
    /// The limit that ended the connection. [`Link::failure`] reads it.
    stall: Arc<OnceLock<Phase>>,
}

impl Watched {
    fn new(stream: Stream, timeouts: Timeouts, stall: Arc<OnceLock<Phase>>) -> Self {
        Self {
            stream,
            write: Wait::new(Some(timeouts.write)),
            read: Wait::new(timeouts.read_idle),
            read_waits: false,
            stall,
        }
    }

    /// The error of a wait that passed its limit.
    fn stalled(&self, phase: Phase) -> io::Error {
        // The first limit that passes ends the connection, so a second value
        // has no meaning.
        let _ = self.stall.set(phase);

        io::Error::from(io::ErrorKind::TimedOut)
    }

    /// Starts the wait of the write when none runs. An error when the write
    /// of the request is past its limit.
    fn start_write(&mut self, cx: &mut Context<'_>) -> io::Result<()> {
        // The request is on its way, so the peer cannot answer. The wait for
        // the answer does not run.
        self.read.stop();
        if self.write.passed(cx) {
            return Err(self.stalled(Phase::Write));
        }

        Ok(())
    }

    /// Ends the wait of the write. hyper wrote each byte that it holds.
    fn end_write(&mut self, cx: &mut Context<'_>) {
        if !self.write.runs() {
            return;
        }

        self.write.stop();
        // The wait for the answer starts here. hyper does not read again
        // before the next wake, so this code starts the timer. A limit of
        // zero passes at once and gives no wake.
        if self.read_waits && self.read.passed(cx) {
            cx.waker().wake_by_ref();
        }
    }
}

impl AsyncRead for Watched {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let read = Pin::new(&mut this.stream).poll_read(cx, buffer);
        if read.is_ready() {
            this.read_waits = false;
            this.read.stop();

            return read;
        }

        this.read_waits = true;
        if this.write.runs() {
            this.read.stop();

            return Poll::Pending;
        }

        if this.read.passed(cx) {
            return Poll::Ready(Err(this.stalled(Phase::ReadIdle)));
        }

        Poll::Pending
    }
}

impl AsyncWrite for Watched {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        if let Err(error) = this.start_write(cx) {
            return Poll::Ready(Err(error));
        }

        Pin::new(&mut this.stream).poll_write(cx, bytes)
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        slices: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        if let Err(error) = this.start_write(cx) {
            return Poll::Ready(Err(error));
        }

        Pin::new(&mut this.stream).poll_write_vectored(cx, slices)
    }

    fn is_write_vectored(&self) -> bool {
        self.stream.is_write_vectored()
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let flushed = Pin::new(&mut this.stream).poll_flush(cx);
        if matches!(flushed, Poll::Ready(Ok(()))) {
            this.end_write(cx);
        }

        flushed
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().stream).poll_shutdown(cx)
    }
}

/// The connection of one request, as the caller of hyper holds it.
///
/// The client spawns no task for the connection future of hyper. Each wait
/// for the head or for a chunk runs that future too, with [`Link::drive`]. A
/// caller that drops its own future then drops the connection, and the
/// socket closes.
struct Link {
    /// The connection future of hyper. `None` after it ended.
    conn: Option<Conn>,
    /// The limit of the socket that ended the connection, when one did.
    stall: Arc<OnceLock<Phase>>,
}

impl Link {
    /// Runs the connection until `work` gives its value. `None` when the
    /// connection ended and `work` has no value.
    ///
    /// The future is cancel safe when `work` is.
    async fn drive<F: Future>(&mut self, work: F) -> Option<F::Output> {
        let mut work = pin!(work);

        poll_fn(|cx| {
            if let Poll::Ready(value) = work.as_mut().poll(cx) {
                return Poll::Ready(Some(value));
            }

            if self.poll_conn(cx).is_pending() {
                return Poll::Pending;
            }

            // The connection ended. Its last step gave `work` its value, or
            // no value comes.
            Poll::Ready(match work.as_mut().poll(cx) {
                Poll::Ready(value) => Some(value),
                Poll::Pending => None,
            })
        })
        .await
    }

    /// Runs the connection. Ready after the connection ended.
    fn poll_conn(&mut self, cx: &mut Context<'_>) -> Poll<()> {
        let Some(conn) = self.conn.as_mut() else {
            return Poll::Ready(());
        };

        if Pin::new(conn).poll(cx).is_pending() {
            return Poll::Pending;
        }

        // The error of hyper has no reason that this crate can read.
        // `failure` gives the reason.
        self.conn = None;

        Poll::Ready(())
    }

    /// Why the connection gave no head or no whole body.
    fn failure(&self) -> ClientError {
        match self.stall.get() {
            Some(phase) => ClientError::TimedOut(*phase),
            None => ClientError::Protocol,
        }
    }
}

/// One answer after its head.
struct Opened {
    status: StatusCode,
    headers: HeaderMap,
    body: Incoming,
    link: Link,
    sender: SendRequest<Full<Bytes>>,
}

impl Opened {
    /// Reads the whole body, up to `cap` bytes.
    async fn whole(self, cap: ByteCap) -> Result<Reply, ClientError> {
        let too_large = ClientError::BodyTooLarge { cap };
        // The length that the head gives. A body with no such length has the
        // lower bound 0, and the count below finds its size.
        let declared = usize::try_from(self.body.size_hint().lower()).unwrap_or(usize::MAX);
        if declared > cap.get() {
            return Err(too_large);
        }

        let mut reading = Reading {
            body: self.body,
            link: self.link,
            _sender: self.sender,
        };
        let mut body = Vec::new();
        while let Some(chunk) = reading.chunk().await? {
            if body.len().saturating_add(chunk.len()) > cap.get() {
                return Err(too_large);
            }

            body.extend_from_slice(&chunk);
        }

        Ok(Reply {
            status: self.status,
            headers: self.headers,
            body,
        })
    }
}

/// The body of one answer, and the connection that gives it.
struct Reading {
    body: Incoming,
    link: Link,
    /// The sender of hyper. No code reads the field. hyper ends a connection
    /// when it can take a request and the sender is gone, so the value lives
    /// as long as the body.
    _sender: SendRequest<Full<Bytes>>,
}

impl Reading {
    /// The next chunk of the body. `None` at the end of the body.
    ///
    /// The future is cancel safe: it keeps no chunk that it took from the
    /// body.
    async fn chunk(&mut self) -> Result<Option<Bytes>, ClientError> {
        loop {
            let Some(next) = self.link.drive(self.body.frame()).await else {
                return Err(self.link.failure());
            };

            match next {
                Some(Ok(frame)) => {
                    // A frame that is no chunk holds the trailers of the
                    // body. The loop reads the next frame.
                    if let Ok(chunk) = frame.into_data() {
                        return Ok(Some(chunk));
                    }
                }
                Some(Err(_)) => return Err(self.link.failure()),
                None => return Ok(None),
            }
        }
    }
}

/// One whole answer.
///
/// Only [`Client::send`] makes one. `Debug` prints the status and the
/// headers. It prints no byte of the body and no count of the bytes: an
/// answer can hold a key, and the count can give the length of that key.
///
/// The Python origin is the `httpx.Response` of a call, for example in
/// `HttpTransport.get` of
/// `noticeboard/src/noticeboard/attendancehttp.py:67`.
///
/// ```
/// use creche_runtime::http::client::Reply;
/// use http::StatusCode;
///
/// fn is_json(reply: &Reply) -> bool {
///     reply.status() == StatusCode::OK
///         && reply.headers().contains_key("content-type")
///         && reply.body().first() == Some(&b'{')
/// }
/// ```
///
/// Code outside this module cannot build an answer or change a part of one:
///
/// ```compile_fail,E0451
/// use creche_runtime::http::client::Reply;
/// use http::StatusCode;
///
/// let reply = Reply {
///     status: StatusCode::OK,
///     headers: http::HeaderMap::new(),
///     body: Vec::new(),
/// };
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Reply {
    status: StatusCode,
    headers: HeaderMap,
    body: Vec<u8>,
}

impl Reply {
    /// The status.
    #[must_use]
    pub const fn status(&self) -> StatusCode {
        self.status
    }

    /// Each header.
    #[must_use]
    pub const fn headers(&self) -> &HeaderMap {
        &self.headers
    }

    /// Each byte of the body.
    #[must_use]
    pub fn body(&self) -> &[u8] {
        &self.body
    }

    /// Each byte of the body, with no copy.
    #[must_use]
    pub fn into_body(self) -> Vec<u8> {
        self.body
    }
}

impl fmt::Debug for Reply {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Reply")
            .field("status", &self.status)
            .field("headers", &self.headers)
            .finish_non_exhaustive()
    }
}

/// When a stream ends at the latest, as the stream holds it.
enum Life {
    /// The timer of [`StreamLimit::Within`]. It started at
    /// [`Client::open`].
    Within(Pin<Box<Sleep>>),
    /// The stop signal of [`StreamLimit::UntilShutdown`].
    UntilShutdown(Shutdown),
}

impl Life {
    fn new(limit: StreamLimit) -> Result<Self, ClientError> {
        match limit {
            StreamLimit::Within(total) => timer(total).map(Self::Within),
            StreamLimit::UntilShutdown(shutdown) => {
                // The stream has no total limit. The check of the timer
                // stays: the limits of the phases need it.
                drop(timer(Duration::ZERO)?);

                Ok(Self::UntilShutdown(shutdown))
            }
        }
    }

    /// Waits for the end of the stream, and gives the error of that end.
    ///
    /// The future is cancel safe.
    async fn ended(&mut self) -> ClientError {
        match self {
            Self::Within(limit) => {
                limit.as_mut().await;

                ClientError::TimedOut(Phase::Total)
            }
            Self::UntilShutdown(shutdown) => {
                shutdown.cancelled().await;

                ClientError::Closed
            }
        }
    }
}

/// What a stream still gives.
enum Flow {
    /// The body has more chunks, or its end is not read yet.
    Open(Box<Reading>),
    /// The stream ended. The connection is closed. Each later call of
    /// [`ReplyStream::chunk`] gives this end again.
    Ended(Result<(), ClientError>),
}

/// An answer whose body the caller reads chunk by chunk.
///
/// To drop the stream closes the connection. The server then sees that its
/// client left. The stream also closes the connection at its end and at its
/// first error.
///
/// `Debug` prints the status and the headers, and no byte of the body.
///
/// The Python origin is the response of `httpx.AsyncClient.stream` in
/// `stream_turn` of `door-owui/src/agent_door_owui/attendance.py:183-209`.
/// A cancelled call there closes the connection too
/// (`chaperone/src/chaperone/delegate.py:280-282`).
///
/// ```no_run
/// use creche_runtime::http::client::{ClientError, ReplyStream};
///
/// async fn lines(mut stream: ReplyStream) -> Result<usize, ClientError> {
///     let mut count = 0;
///     while let Some(chunk) = stream.chunk().await? {
///         count += chunk.iter().filter(|byte| **byte == b'\n').count();
///     }
///
///     Ok(count)
/// }
/// ```
///
/// Code outside this module cannot build a stream or change its head. Each
/// stream comes from [`Client::open`]:
///
/// ```compile_fail,E0451
/// use creche_runtime::http::client::{ClientError, ReplyStream};
///
/// fn moved(stream: ReplyStream) -> ReplyStream {
///     ReplyStream {
///         status: http::StatusCode::OK,
///         ..stream
///     }
/// }
/// ```
pub struct ReplyStream {
    status: StatusCode,
    headers: HeaderMap,
    flow: Flow,
    life: Life,
}

impl fmt::Debug for ReplyStream {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ReplyStream")
            .field("status", &self.status)
            .field("headers", &self.headers)
            .finish_non_exhaustive()
    }
}

impl ReplyStream {
    /// The status of the answer.
    #[must_use]
    pub const fn status(&self) -> StatusCode {
        self.status
    }

    /// Each header of the answer.
    #[must_use]
    pub const fn headers(&self) -> &HeaderMap {
        &self.headers
    }

    /// The next chunk of the body. `None` at the end of the body.
    ///
    /// The chunks are not lines. The caller splits a stream of lines itself.
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again, and no byte is lost.
    ///
    /// The end of the stream wins over a chunk. A stream with a chunk ready
    /// at each call thus still ends at its limit or at the stop signal.
    ///
    /// After `None` or an error, the connection is closed. Each later call
    /// gives the same result.
    ///
    /// The Python origin is the loop over `response.aiter_bytes()` in
    /// `_iter_lines` of `door-owui/src/agent_door_owui/attendance.py:247-277`.
    ///
    /// # Errors
    ///
    /// [`ClientError`] when the stream stops before its end:
    /// [`ClientError::TimedOut`] with [`Phase::ReadIdle`] or with
    /// [`Phase::Total`], [`ClientError::Closed`] at the stop signal, and
    /// [`ClientError::Protocol`] for a body that the peer cut.
    pub async fn chunk(&mut self) -> Result<Option<Bytes>, ClientError> {
        let next = match &mut self.flow {
            Flow::Ended(end) => return end.clone().map(|()| None),
            Flow::Open(reading) => {
                tokio::select! {
                    biased;
                    error = self.life.ended() => Err(error),
                    next = reading.chunk() => next,
                }
            }
        };

        match next {
            Ok(Some(chunk)) => Ok(Some(chunk)),
            Ok(None) => {
                self.flow = Flow::Ended(Ok(()));

                Ok(None)
            }
            Err(error) => {
                self.flow = Flow::Ended(Err(error.clone()));

                Err(error)
            }
        }
    }
}

/// The phase of a request that ran past its limit.
///
/// The set is closed. `httpx` has one error for each of the first three
/// phases: `ConnectTimeout`, `WriteTimeout` and `ReadTimeout`. Only the
/// delegate call of Python has a limit for the whole call
/// (`chaperone/src/chaperone/delegate.py:279`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    /// The connect.
    Connect,
    /// The write of the request.
    Write,
    /// The wait for the head of the answer, or for its next chunk.
    ReadIdle,
    /// The whole call.
    Total,
}

impl fmt::Display for Phase {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Connect => "the connect",
            Self::Write => "the write of the request",
            Self::ReadIdle => "the wait for the answer",
            Self::Total => "the whole call",
        })
    }
}

/// Why a call gave no answer.
///
/// No variant holds a header value or a byte of a body. A caller writes this
/// error to a log or onto a page.
///
/// The Python origin is an `httpx.HTTPError`. A Python client writes the
/// name and the text of that error into its own message, for example
/// `noticeboard/src/noticeboard/attendancehttp.py:61-65`. This type holds no
/// name and no text of `httpx`, so a service writes its own sentence for
/// each variant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClientError {
    /// The connect failed.
    Connect {
        /// The text of the error, as `strerror` of Python gives it. On a
        /// thread with no runtime of `tokio`, and in a runtime with no timer
        /// or no I/O driver, the text says which one is absent.
        os_text: String,
    },
    /// A phase ran past its limit.
    TimedOut(Phase),
    /// The body of the answer holds more bytes than the cap.
    BodyTooLarge {
        /// The cap of the call.
        cap: ByteCap,
    },
    /// The peer sent bytes that are not HTTP/1.1, or closed the connection in
    /// the middle of an answer. A request that the client cannot write is in
    /// this case too: a target of more than 65,534 bytes.
    Protocol,
    /// The bearer holds a byte that a header value cannot hold. The client
    /// refuses it before the connect.
    BadBearer,
    /// The stream ended at the stop signal of the process.
    Closed,
}

impl fmt::Display for ClientError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Connect { os_text } => write!(f, "the connect failed: {os_text}"),
            Self::TimedOut(phase) => write!(f, "{phase} ran past its limit"),
            Self::BodyTooLarge { cap } => {
                write!(f, "the answer holds more than {} bytes", cap.get())
            }
            Self::Protocol => f.write_str("the answer is not HTTP/1.1, or it stopped early"),
            Self::BadBearer => f.write_str("the token holds a byte that a header cannot hold"),
            Self::Closed => f.write_str("the stream ended because the process stops"),
        }
    }
}

impl Error for ClientError {}

#[cfg(test)]
mod tests {
    use std::os::unix::net::UnixListener;
    use std::path::Path;
    use std::process::Stdio;
    use std::task::Waker;
    use std::time::Instant;

    use creche_testkit::root::TempRoot;
    use creche_testkit::stub::{Answer, End, HttpStub, Recorded, refused_socket};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::runtime::{Builder, Runtime};

    use super::*;
    use crate::tasks::shutdown_pair;

    /// The longest time that a test waits for a step that must occur, and a
    /// limit that a test must not reach. The time is real, and a host with
    /// much load is slow.
    const PATIENT: Duration = Duration::from_secs(60);

    /// A limit that a test makes pass. No other limit of that test is
    /// shorter than [`PATIENT`], so only this one can pass.
    const SHORT: Duration = Duration::from_millis(100);

    /// A read limit that a test makes pass after it read one chunk. The
    /// chunk must come inside the limit, so the limit is not short.
    const GAP: Duration = Duration::from_secs(1);

    /// A pause of a stub that no test waits for. The stub stops the pause
    /// when its client leaves.
    const HOLD: Duration = Duration::from_secs(600);

    /// The pause between two chunks of a stream that a test reads whole.
    const BEAT: Duration = Duration::from_millis(30);

    /// The token of each request with a bearer. No error can hold it.
    const TOKEN: &str = "correct-horse-battery-staple-0123";

    /// A text of a request body, of an answer header and of an answer body.
    /// No error can hold one.
    const PROMPT_MARK: &str = "walrus-prompt-7731";
    const HEADER_MARK: &str = "walrus-header-7731";
    const BODY_MARK: &str = "walrus-body-7731";

    /// The variable that gives the child test the port of its stub.
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_CLIENT_TEST_PORT";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "http::client::tests::the_child_calls_the_port_of_its_variable";

    /// Each variable that names a proxy for `httpx`.
    const PROXY_VARIABLES: [&str; 4] = ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"];

    /// Each variable that keeps a host away from the proxy for `httpx`.
    const NO_PROXY_VARIABLES: [&str; 2] = ["NO_PROXY", "no_proxy"];

    /// How a test reaches its stub.
    #[derive(Debug, Clone, Copy)]
    enum Transport {
        Unix,
        Loopback,
    }

    const TRANSPORTS: [Transport; 2] = [Transport::Unix, Transport::Loopback];

    /// One stub and the client of its address.
    struct Peer {
        stub: HttpStub,
        client: Client,
        /// The value of the `Host` header that the client writes.
        host: String,
        /// The directory of the socket. It lives as long as the stub.
        _root: TempRoot,
    }

    fn runtime() -> Runtime {
        Builder::new_current_thread().enable_all().build().unwrap()
    }

    fn token() -> Secret {
        Secret::try_from(String::from(TOKEN)).unwrap()
    }

    fn socket_of(stub: &HttpStub) -> SocketPath {
        stub.socket().unwrap().to_str().unwrap().parse().unwrap()
    }

    async fn peer(transport: Transport) -> Peer {
        let root = TempRoot::new().unwrap();

        match transport {
            Transport::Unix => {
                let stub = HttpStub::unix(&root).await.unwrap();
                let client = Client::new(Target::unix(socket_of(&stub), ATTENDANCE_HOST));

                Peer {
                    stub,
                    client,
                    host: String::from(ATTENDANCE_HOST),
                    _root: root,
                }
            }
            Transport::Loopback => {
                let stub = HttpStub::loopback().await.unwrap();
                let host = format!("127.0.0.1:{}", stub.port().unwrap());
                let url: HttpUrl = format!("http://{host}").parse().unwrap();

                Peer {
                    stub,
                    client: Client::new(Target::try_from(&url).unwrap()),
                    host,
                    _root: root,
                }
            }
        }
    }

    /// A client of a peer that completes no connect.
    fn silent() -> Client {
        Client::new(Target {
            dial: Dial::Silent,
            host: header_text(ATTENDANCE_HOST),
            prefix: String::new(),
        })
    }

    /// A request with no bearer, no header and no body. No limit of a phase
    /// passes in a test.
    fn request<'a>(method: Method, segments: &[&str]) -> Request<'a> {
        Request::new(
            method,
            PathAndQuery::from_segments(segments),
            Timeouts::each(PATIENT),
        )
    }

    fn get<'a>() -> Request<'a> {
        request(Method::GET, &["v1", "sessions"])
    }

    /// The request of [`get`] with these limits.
    fn get_within<'a>(limits: Timeouts) -> Request<'a> {
        Request::new(
            Method::GET,
            PathAndQuery::from_segments(&["v1", "sessions"]),
            limits,
        )
    }

    /// A client of the socket at `path`.
    fn client_of(path: &Path) -> Client {
        Client::new(Target::unix(
            path.to_str().unwrap().parse().unwrap(),
            ATTENDANCE_HOST,
        ))
    }

    /// How the first `count` connections of `stub` ended.
    async fn ends(stub: &HttpStub, count: usize) -> Vec<End> {
        tokio::time::timeout(PATIENT, stub.wait_ends(count))
            .await
            .unwrap()
    }

    /// The one request that `stub` read.
    fn sole(stub: &HttpStub) -> Recorded {
        let mut requests = stub.requests();
        assert_eq!(requests.len(), 1);

        requests.remove(0)
    }

    fn utf8(bytes: &[u8]) -> &str {
        std::str::from_utf8(bytes).unwrap()
    }

    /// Reads a stream to its end or to its first error.
    async fn drain(stream: &mut ReplyStream) -> (Vec<u8>, Result<(), ClientError>) {
        let mut bytes = Vec::new();

        loop {
            match stream.chunk().await {
                Ok(Some(chunk)) => bytes.extend_from_slice(&chunk),
                Ok(None) => return (bytes, Ok(())),
                Err(error) => return (bytes, Err(error)),
            }
        }
    }

    fn sendable<T: Send>(value: T) {
        drop(value);
    }

    // --- the skeleton tests ---

    #[test]
    fn one_limit_for_each_phase_is_the_single_number_of_httpx() {
        let limit = Duration::from_secs(5);

        let timeouts = Timeouts::each(limit);

        assert_eq!(timeouts.connect(), limit);
        assert_eq!(timeouts.write(), limit);
        assert_eq!(timeouts.read_idle(), Some(limit));
    }

    #[test]
    fn a_stream_takes_no_read_limit_and_keeps_the_two_other_limits() {
        // `httpx.Timeout(5.0, read=None, write=10.0)` of the stream of a
        // door (door-owui/src/agent_door_owui/attendance.py:193).
        let timeouts = Timeouts::each(Duration::from_secs(5))
            .with_read_idle(None)
            .with_write(Duration::from_secs(10));

        assert_eq!(timeouts.connect(), Duration::from_secs(5));
        assert_eq!(timeouts.write(), Duration::from_secs(10));
        assert_eq!(timeouts.read_idle(), None);
    }

    #[test]
    fn each_limit_changes_only_its_own_phase() {
        let base = Timeouts::each(Duration::from_secs(5));
        let other = Duration::from_secs(9);

        assert_eq!(
            base.with_connect(other),
            Timeouts {
                connect: other,
                write: Duration::from_secs(5),
                read_idle: Some(Duration::from_secs(5)),
            }
        );
        assert_eq!(
            base.with_write(other),
            Timeouts {
                connect: Duration::from_secs(5),
                write: other,
                read_idle: Some(Duration::from_secs(5)),
            }
        );
        assert_eq!(
            base.with_read_idle(Some(other)),
            Timeouts {
                connect: Duration::from_secs(5),
                write: Duration::from_secs(5),
                read_idle: Some(other),
            }
        );
        assert_eq!(base.with_read_idle(None).read_idle(), None);
    }

    #[test]
    fn the_debug_of_a_body_and_of_an_answer_prints_no_byte_and_no_count() {
        let body = Body::Json(br#"{"prompt":"correct horse"}"#.to_vec());
        let reply = Reply {
            status: StatusCode::OK,
            headers: HeaderMap::new(),
            body: b"correct horse".to_vec(),
        };

        // A body can hold a key, so the text holds no count of its bytes:
        // not the 26 of the request and not the 13 of the answer.
        assert_eq!(format!("{body:?}"), "Json(..)");
        assert_eq!(format!("{:?}", Body::Empty), "Empty");
        assert_eq!(
            format!("{reply:?}"),
            "Reply { status: 200, headers: {}, .. }"
        );
    }

    #[test]
    fn a_new_request_has_no_bearer_no_header_and_no_body() {
        let limits = Timeouts::each(Duration::from_secs(5));
        let path = PathAndQuery::from_segments(&["v1", "sessions"]);

        let request = Request::new(Method::DELETE, path.clone(), limits);

        assert_eq!(request.method(), Method::DELETE);
        assert_eq!(request.path(), &path);
        assert_eq!(request.timeouts(), limits);
        assert!(request.bearer().is_none());
        assert!(request.headers().is_empty());
        assert_eq!(request.body(), &Body::Empty);
    }

    #[test]
    fn a_request_holds_what_its_caller_gave_it() {
        let secret = token();
        let header = (
            HeaderName::from_static("x-door-instance"),
            HeaderValue::from_static("tui.4711"),
        );
        let body = Body::Json(b"{}".to_vec());

        let request = get()
            .with_bearer(&secret)
            .with_headers(vec![header.clone()])
            .with_body(body.clone());

        assert!(request.bearer().unwrap().matches(TOKEN.as_bytes()));
        assert_eq!(request.headers(), [header]);
        assert_eq!(request.body(), &body);
        // Each other part stays.
        assert_eq!(request.method(), Method::GET);
        assert_eq!(request.timeouts(), Timeouts::each(PATIENT));
    }

    #[test]
    fn a_request_holds_no_header_that_the_client_writes_itself() {
        let header = |name: &'static str, value: &'static str| {
            (
                HeaderName::from_static(name),
                HeaderValue::from_static(value),
            )
        };

        let request = get().with_headers(vec![
            header("host", "other"),
            header("authorization", "Bearer other"),
            header("content-type", "text/plain"),
            header("content-length", "999"),
            header("connection", "keep-alive"),
            header("transfer-encoding", "chunked"),
            header("x-door-instance", "one"),
            header("accept", "application/x-ndjson"),
            header("x-door-instance", "two"),
        ]);

        assert_eq!(
            request.headers(),
            [
                header("x-door-instance", "one"),
                header("accept", "application/x-ndjson"),
                header("x-door-instance", "two"),
            ]
        );

        // A second call replaces the headers of the first one.
        let request = request.with_headers(vec![header("x-turn", "t-1")]);

        assert_eq!(request.headers(), [header("x-turn", "t-1")]);
    }

    #[test]
    fn an_answer_gives_its_status_its_headers_and_its_body() {
        let mut headers = HeaderMap::new();
        headers.insert("x-turn", HeaderValue::from_static("t-1"));
        let reply = Reply {
            status: StatusCode::ACCEPTED,
            headers: headers.clone(),
            body: b"{}".to_vec(),
        };

        assert_eq!(reply.status(), StatusCode::ACCEPTED);
        assert_eq!(reply.headers(), &headers);
        assert_eq!(reply.body(), b"{}");
        assert_eq!(reply.into_body(), b"{}");
    }

    #[test]
    fn a_client_error_names_its_reason() {
        let table = [
            (
                ClientError::Connect {
                    os_text: String::from("Connection refused"),
                },
                "the connect failed: Connection refused",
            ),
            (
                ClientError::TimedOut(Phase::Connect),
                "the connect ran past its limit",
            ),
            (
                ClientError::TimedOut(Phase::Write),
                "the write of the request ran past its limit",
            ),
            (
                ClientError::TimedOut(Phase::ReadIdle),
                "the wait for the answer ran past its limit",
            ),
            (
                ClientError::TimedOut(Phase::Total),
                "the whole call ran past its limit",
            ),
            (
                ClientError::BodyTooLarge {
                    cap: ByteCap::ONE_MIB,
                },
                "the answer holds more than 1048576 bytes",
            ),
            (
                ClientError::Protocol,
                "the answer is not HTTP/1.1, or it stopped early",
            ),
            (
                ClientError::BadBearer,
                "the token holds a byte that a header cannot hold",
            ),
            (
                ClientError::Closed,
                "the stream ended because the process stops",
            ),
        ];

        for (error, text) in table {
            assert_eq!(error.to_string(), text);
        }
    }

    #[test]
    fn a_target_error_names_its_reason() {
        assert_eq!(
            TargetError::TlsNotSupported.to_string(),
            "the URL is an https URL, and the client has no TLS"
        );
        assert_eq!(
            TargetError::NotAnAddress.to_string(),
            "the URL holds no host and port to connect to"
        );
    }

    // --- the target ---

    #[test]
    fn a_socket_target_dials_the_socket_and_names_its_host() {
        let socket: SocketPath = "/srv/agents/state/rework/sock/sessiond.sock"
            .parse()
            .unwrap();
        let target = Target::unix(socket.clone(), "sessiond");

        assert_eq!(target.dial, Dial::Unix(socket));
        assert_eq!(target.host, "sessiond");
        assert_eq!(target.prefix, "");
    }

    #[test]
    fn a_host_that_a_header_cannot_hold_gives_an_empty_host_header() {
        let socket: SocketPath = "/tmp/root/sessiond.sock".parse().unwrap();

        for host in ["sessiond\n", "a\rb", "a\0b", "a\u{7f}b"] {
            assert_eq!(Target::unix(socket.clone(), host).host, "", "{host:?}");
        }
    }

    #[test]
    fn a_bind_address_is_a_tcp_target() {
        for (bind, host, port, header) in [
            ("192.0.2.10:8300", "192.0.2.10", 8300, "192.0.2.10:8300"),
            ("[::1]:8340", "::1", 8340, "[::1]:8340"),
            ("::1:8340", "::1", 8340, "[::1]:8340"),
            ("localhost:80", "localhost", 80, "localhost"),
            ("LocalHost:8300", "localhost", 8300, "localhost:8300"),
        ] {
            let address: BindAddress = bind.parse().unwrap();
            let target = Target::from(&address);

            assert_eq!(
                target.dial,
                Dial::Tcp {
                    host: String::from(host),
                    port
                },
                "{bind}"
            );
            assert_eq!(target.host, header, "{bind}");
            assert_eq!(target.prefix, "", "{bind}");
        }
    }

    /// What `httpx` 0.28.1 makes of each base URL: the host and the port of
    /// its connect, its `Host` header, and what it writes before the request
    /// path `/v1/sessions`. A probe of `httpx` on a socket gave each row.
    const URLS: [(&str, &str, u16, &str, &str); 13] = [
        (
            "http://192.0.2.10:8300",
            "192.0.2.10",
            8300,
            "192.0.2.10:8300",
            "",
        ),
        (
            "http://192.0.2.10:8300/",
            "192.0.2.10",
            8300,
            "192.0.2.10:8300",
            "",
        ),
        (
            "http://192.0.2.10:8300/api",
            "192.0.2.10",
            8300,
            "192.0.2.10:8300",
            "/api",
        ),
        (
            "http://192.0.2.10:8300/api/",
            "192.0.2.10",
            8300,
            "192.0.2.10:8300",
            "/api",
        ),
        (
            "http://192.0.2.10:8300/api/v2",
            "192.0.2.10",
            8300,
            "192.0.2.10:8300",
            "/api/v2",
        ),
        ("http://192.0.2.10:80", "192.0.2.10", 80, "192.0.2.10", ""),
        ("http://192.0.2.10", "192.0.2.10", 80, "192.0.2.10", ""),
        ("http://sessiond", "sessiond", 80, "sessiond", ""),
        (
            "http://Example.COM:8300",
            "example.com",
            8300,
            "example.com:8300",
            "",
        ),
        ("http://[::1]:8300", "::1", 8300, "[::1]:8300", ""),
        (
            "http://[2001:DB8::1]",
            "2001:DB8::1",
            80,
            "[2001:DB8::1]",
            "",
        ),
        (
            "http://192.0.2.10:8300/a/../b/./c",
            "192.0.2.10",
            8300,
            "192.0.2.10:8300",
            "/b/c",
        ),
        (
            "http://192.0.2.10:8300/a%20b/c\"d<e>f{g}/\u{e9}/%zz/%41",
            "192.0.2.10",
            8300,
            "192.0.2.10:8300",
            "/a%20b/c%22d%3Ce%3Ef%7Bg%7D/%C3%A9/%zz/%41",
        ),
    ];

    #[test]
    fn a_url_is_a_tcp_target_with_the_parts_that_httpx_takes() {
        for (text, host, port, header, prefix) in URLS {
            let url: HttpUrl = text.parse().unwrap();
            let target = Target::try_from(&url).unwrap();

            assert_eq!(
                target.dial,
                Dial::Tcp {
                    host: String::from(host),
                    port
                },
                "{text}"
            );
            assert_eq!(target.host, header, "{text}");
            assert_eq!(target.prefix, prefix, "{text}");
        }
    }

    #[test]
    fn the_base_path_of_a_url_loses_each_dot_segment_and_each_final_slash() {
        for (path, prefix) in [
            ("", ""),
            ("/", ""),
            ("//", ""),
            ("/api//", "/api"),
            ("/a//b", "/a//b"),
            ("/.", ""),
            ("/..", ""),
            ("/../..", ""),
            ("/a/..", ""),
            ("/a/../..", ""),
            ("/a/./b/../c/", "/a/c"),
            ("/a/b/../../c", "/c"),
            ("/...", "/..."),
            ("/%2E%2E/a", "/%2E%2E/a"),
            ("/a:b@c;d=e,f+g", "/a:b@c;d=e,f+g"),
            ("/a^b|c\\d`e", "/a^b|c\\d%60e"),
        ] {
            let url: HttpUrl = format!("http://192.0.2.10:8300{path}").parse().unwrap();

            assert_eq!(Target::try_from(&url).unwrap().prefix, prefix, "{path}");
        }
    }

    #[test]
    fn a_base_path_with_each_byte_is_the_start_of_a_request_line() {
        // A target that passes at the start of a service must not fail at
        // each call. The `http` crate takes each base path as the start of
        // a request line, whatever bytes the URL holds.
        for byte in 0..=u8::MAX {
            let path = String::from_utf8_lossy(&[b'a', byte, b'b']).into_owned();
            let prefix = base_prefix(&path);
            let line = PathAndQuery::from_segments(&["v1", "sessions"]).after(&prefix);

            assert!(prefix.starts_with("/a"), "{byte:#04x}: {prefix}");
            assert!(prefix.ends_with('b'), "{byte:#04x}: {prefix}");
            assert!(prefix.is_ascii(), "{byte:#04x}: {prefix}");
            assert_eq!(
                Uri::try_from(line.clone()).unwrap().path(),
                line,
                "{byte:#04x}"
            );
        }
    }

    #[test]
    fn a_url_with_https_gives_tls_not_supported() {
        for text in [
            "https://192.0.2.10",
            "https://192.0.2.10:8350/api",
            "https://[::1]:8350",
            "https://192.0.2.10:8350/?x=1",
            "https://192.0.2.10:not-a-port",
        ] {
            let url: HttpUrl = text.parse().unwrap();

            assert_eq!(
                Target::try_from(&url),
                Err(TargetError::TlsNotSupported),
                "{text}"
            );
            assert_eq!(
                Target::try_from(&AttendanceTarget::Url(url)),
                Err(TargetError::TlsNotSupported),
                "{text}"
            );
        }
    }

    /// Each URL that `HttpUrl` accepts and that is no target. A probe of
    /// `httpx` 0.28.1 gave what the comment of each group says of it.
    const NOT_TARGETS: [&str; 27] = [
        // `httpx` writes the query into the middle of each request path, and
        // it drops the fragment.
        "http://192.0.2.10:8300/api?x=1",
        "http://192.0.2.10:8300?x=1",
        "http://192.0.2.10:8300/api#top",
        "http://192.0.2.10:8300#top",
        // `httpx` reads each of these four ports as port 80 or as port 44.
        "http://192.0.2.10:",
        "http://192.0.2.10:/api",
        "http://192.0.2.10:+80",
        "http://192.0.2.10:8_0",
        "http://192.0.2.10:\u{664}\u{664}",
        // `httpx` takes the two ports, and the connect then fails.
        "http://192.0.2.10:0",
        "http://192.0.2.10:65536",
        // `httpx` takes each of these hosts.
        "http://[::1]:",
        "http://[fe80::1%25eth0]:80",
        "http://exa!mple:8300",
        "http://exa%6dple:8300",
        "http://ex_ample:8300",
        "http://\u{e9}.example:8300",
        "http://0.0.0.0:8300",
        "http://[::]:8300",
        "http://192.0.2:8300",
        "http://-host:8300",
        // `httpx` takes the URL with no host, and it tries a connect.
        "http://:8300",
        // `httpx` refuses each of these URLs too.
        "http://192.0.2.10:80:80",
        "http://::1:8300",
        "http://[::1",
        "http://[::1]x",
        "http://[192.0.2.10]:80",
    ];

    #[test]
    fn a_url_with_no_host_and_port_of_a_connect_is_refused() {
        for text in NOT_TARGETS {
            let url: HttpUrl = text.parse().unwrap();

            assert_eq!(
                Target::try_from(&url),
                Err(TargetError::NotAnAddress),
                "{text}"
            );
            assert_eq!(
                Target::try_from(&AttendanceTarget::Url(url)),
                Err(TargetError::NotAnAddress),
                "{text}"
            );
        }
    }

    #[test]
    fn each_final_slash_of_a_base_path_is_removed() {
        // `httpx` removes one final slash. The base path `/api//` gives the
        // request path `/api//v1/sessions` there.
        let url: HttpUrl = "http://192.0.2.10:8300/api//".parse().unwrap();
        let target = Target::try_from(&url).unwrap();
        let path = PathAndQuery::from_segments(&["v1", "sessions"]);

        assert_eq!(path.after(&target.prefix), "/api/v1/sessions");
        // `HttpUrl::base` has the same rule.
        assert_eq!(url.base(), "http://192.0.2.10:8300/api");
    }

    #[test]
    fn an_attendance_target_is_the_socket_or_the_url() {
        let socket: SocketPath = "/tmp/root/sessiond.sock".parse().unwrap();
        let url: HttpUrl = "http://192.0.2.10:8350/".parse().unwrap();

        assert_eq!(
            Target::try_from(&AttendanceTarget::Socket(socket.clone())),
            Ok(Target::unix(socket, "sessiond"))
        );
        assert_eq!(
            Target::try_from(&AttendanceTarget::Url(url.clone())),
            Target::try_from(&url)
        );
    }

    // --- the path and the query ---

    #[test]
    fn a_path_keeps_each_segment_whole() {
        let table: [(&[&str], &str); 16] = [
            (&[], "/"),
            (&["v1", "sessions"], "/v1/sessions"),
            (
                &["v1", "sessions", "chat", "tui-01"],
                "/v1/sessions/chat/tui-01",
            ),
            (&["A-z_0.9~"], "/A-z_0.9~"),
            (&["a/b"], "/a%2Fb"),
            (&["a b?c#d%e"], "/a%20b%3Fc%23d%25e"),
            (&["x&y=z+w:v@u"], "/x%26y%3Dz%2Bw%3Av%40u"),
            (&["\u{e9}"], "/%C3%A9"),
            (&["a\nb\0c"], "/a%0Ab%00c"),
            (&["."], "/%2E"),
            (&[".."], "/%2E%2E"),
            (&["v1", "..", "admin"], "/v1/%2E%2E/admin"),
            (&["..."], "/..."),
            (&[".a"], "/.a"),
            (&[""], "/"),
            (&["a", "", "b"], "/a//b"),
        ];

        for (segments, target) in table {
            let path = PathAndQuery::from_segments(segments);

            assert_eq!(path.target, target, "{segments:?}");
            assert!(!path.has_query, "{segments:?}");
        }
    }

    #[test]
    fn a_segment_with_the_bytes_of_an_id_is_written_as_it_is() {
        // Each id of a request path holds only these bytes (contract 02
        // §2). The f-string of a Python client and `httpx` write them as
        // they are, and this function does the same.
        let id = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-";

        assert_eq!(
            PathAndQuery::from_segments(&["v1", "sessions", id]).target,
            format!("/v1/sessions/{id}")
        );
    }

    #[test]
    fn a_query_has_the_bytes_that_httpx_writes() {
        // The second row is what `httpx` 0.28.1 writes for these `params`.
        let table: [(&[(&str, &str)], &str); 5] = [
            (&[], "/v1/s"),
            (
                &[("family", "chat"), ("limit", "1")],
                "/v1/s?family=chat&limit=1",
            ),
            (
                &[
                    ("a b", "c d/e?f&g=h#i+j%k~l.m-n_o"),
                    ("\u{e9}", "\u{fc}"),
                    ("empty", ""),
                ],
                "/v1/s?a+b=c+d%2Fe%3Ff%26g%3Dh%23i%2Bj%25k~l.m-n_o&%C3%A9=%C3%BC&empty=",
            ),
            (&[("", "")], "/v1/s?="),
            (&[("a", "1\n2")], "/v1/s?a=1%0A2"),
        ];

        for (pairs, target) in table {
            let path = PathAndQuery::from_segments(&["v1", "s"]).with_query(pairs);

            assert_eq!(path.target, target, "{pairs:?}");
        }
    }

    #[test]
    fn a_path_and_a_query_with_each_byte_are_a_request_line() {
        // The `http` crate takes each target that the type writes, so only
        // the count of the bytes can make a request that the client cannot
        // write.
        for byte in 0..=u8::MAX {
            let text = String::from_utf8_lossy(&[b'a', byte, b'b']).into_owned();
            let target = PathAndQuery::from_segments(&["v1", &text])
                .with_query(&[(&text, &text)])
                .after("");
            let uri = Uri::try_from(target.clone()).unwrap();

            assert!(target.is_ascii(), "{byte:#04x}: {target}");
            assert_eq!(
                uri.path_and_query().map(|parts| parts.as_str()),
                Some(target.as_str()),
                "{byte:#04x}"
            );
            // The text adds no segment and no pair.
            assert_eq!(target.matches('/').count(), 2, "{byte:#04x}: {target}");
            assert_eq!(target.matches('?').count(), 1, "{byte:#04x}: {target}");
            assert_eq!(target.matches('=').count(), 1, "{byte:#04x}: {target}");
            assert_eq!(target.matches('&').count(), 0, "{byte:#04x}: {target}");
            assert_eq!(target.matches('#').count(), 0, "{byte:#04x}: {target}");
        }
    }

    #[test]
    fn a_second_query_adds_its_pairs_after_the_first() {
        let path = PathAndQuery::from_segments(&["v1", "s"])
            .with_query(&[("a", "1")])
            .with_query(&[])
            .with_query(&[("b", "2"), ("a", "3")]);

        assert_eq!(path.target, "/v1/s?a=1&b=2&a=3");
    }

    #[test]
    fn a_target_is_the_base_path_and_then_the_request_path() {
        let path = PathAndQuery::from_segments(&["v1", "sessions"]).with_query(&[("limit", "1")]);

        assert_eq!(path.after(""), "/v1/sessions?limit=1");
        assert_eq!(path.after("/api"), "/api/v1/sessions?limit=1");
        assert_eq!(PathAndQuery::from_segments(&[]).after(""), "/");
        assert_eq!(PathAndQuery::from_segments(&[]).after("/api"), "/api/");
    }

    // --- the bearer ---

    #[test]
    fn a_bearer_is_one_sensitive_header_value() {
        for token in [
            "correct-horse",
            "a",
            "with space inside",
            "with\ttab",
            " space-first",
            "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",
        ] {
            let secret = Secret::try_from(String::from(token)).unwrap();
            let value = bearer_value(&secret).unwrap();

            assert_eq!(value.as_bytes(), format!("Bearer {token}").as_bytes());
            assert!(value.is_sensitive(), "{token:?}");
            assert!(!format!("{value:?}").contains(token), "{token:?}");
        }
    }

    #[test]
    fn a_bearer_holds_only_the_bytes_that_a_header_holds() {
        // A probe of `httpx` 0.28.1 gave what each comment says of it. A
        // Python client gives `httpx` the token as text.
        for byte in 0..=u8::MAX {
            let secret = Secret::try_from(vec![b't', byte, b'x']).unwrap();
            let fits = match byte {
                // `httpx` sends each of these bytes too.
                b'\t' | b' '..=b'~' => true,
                // `httpx` refuses the NUL and the four characters from line
                // feed to carriage return. It sends each other control
                // character and the byte 0x7F.
                0x00..=0x1f | 0x7f => false,
                // `httpx` refuses a character that is not ASCII.
                0x80..=0xff => false,
            };

            assert_eq!(bearer_value(&secret).is_ok(), fits, "{byte:#04x}");
        }
    }

    #[test]
    fn a_bearer_does_not_end_with_a_space_or_a_tab() {
        // `httpx` refuses each of the two tokens too.
        for last in [b' ', b'\t'] {
            let middle = Secret::try_from(vec![b't', last, b'x']).unwrap();
            let end = Secret::try_from(vec![b't', last]).unwrap();
            let only = Secret::try_from(vec![last]).unwrap();

            assert!(bearer_value(&middle).is_ok(), "{last:#04x}");
            assert_eq!(bearer_value(&end), Err(ClientError::BadBearer));
            assert_eq!(bearer_value(&only), Err(ClientError::BadBearer));
        }
    }

    /// One token for each reason of a refusal: a character that is not
    /// ASCII in two encodings, a byte past ASCII, a line feed, a carriage
    /// return, a NUL, two more control characters, and a space or a tab at
    /// the end.
    const BAD_BEARERS: [&[u8]; 10] = [
        b"t\xc3\xa9",
        b"t\xe9",
        b"\x80",
        b"t\nx",
        b"t\rx",
        b"t\0x",
        b"t\x1bx",
        b"t\x7fx",
        b"t ",
        b"t\t",
    ];

    #[test]
    fn a_bearer_that_a_header_cannot_hold_is_refused_before_the_connect() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;

            for bytes in BAD_BEARERS {
                let secret = Secret::try_from(bytes.to_vec()).unwrap();
                let sent = peer
                    .client
                    .send(get().with_bearer(&secret), ByteCap::ONE_MIB, PATIENT)
                    .await;
                let opened = peer
                    .client
                    .open(get().with_bearer(&secret), StreamLimit::Within(PATIENT))
                    .await;

                assert_eq!(sent, Err(ClientError::BadBearer), "{bytes:?}");
                assert!(matches!(opened, Err(ClientError::BadBearer)), "{bytes:?}");
            }

            // One more call proves that the stub is in order, and that no
            // refused call reached it.
            peer.stub.script(Answer::status(StatusCode::OK).body("ok"));
            let reply = peer
                .client
                .send(get(), ByteCap::ONE_MIB, PATIENT)
                .await
                .unwrap();

            assert_eq!(reply.body(), b"ok");
            assert_eq!(ends(&peer.stub, 1).await, [End::Answered]);
            assert_eq!(peer.stub.requests().len(), 1);
        });
    }

    // --- the bytes on the wire ---

    #[test]
    fn a_get_with_a_query_is_these_bytes_on_the_wire() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let secret = token();
                peer.stub.script(
                    Answer::status(StatusCode::OK)
                        .header("content-type", "application/json")
                        .body(r#"{"sessions":[]}"#),
                );

                let reply = peer
                    .client
                    .send(
                        Request::new(
                            Method::GET,
                            PathAndQuery::from_segments(&["v1", "sessions"])
                                .with_query(&[("family", "chat"), ("limit", "1")]),
                            Timeouts::each(PATIENT),
                        )
                        .with_bearer(&secret),
                        ByteCap::ONE_MIB,
                        PATIENT,
                    )
                    .await
                    .unwrap();

                assert_eq!(reply.status(), StatusCode::OK);
                assert_eq!(reply.headers()["content-type"], "application/json");
                assert_eq!(reply.body(), br#"{"sessions":[]}"#);
                assert_eq!(
                    utf8(sole(&peer.stub).bytes()),
                    format!(
                        "GET /v1/sessions?family=chat&limit=1 HTTP/1.1\r\n\
                         host: {}\r\n\
                         authorization: Bearer {TOKEN}\r\n\
                         connection: close\r\n\r\n",
                        peer.host
                    ),
                    "{transport:?}"
                );
                assert_eq!(ends(&peer.stub, 1).await, [End::Answered]);
            }
        });
    }

    #[test]
    fn a_post_with_a_json_body_is_these_bytes_on_the_wire() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let secret = token();
                let body = r#"{"family":"chat","session":"tui-01","labels":{"door":"tui"}}"#;
                peer.stub.script(
                    Answer::status(StatusCode::CREATED)
                        .header("content-type", "application/json")
                        .body(r#"{"session":"tui-01"}"#),
                );

                let reply = peer
                    .client
                    .send(
                        request(Method::POST, &["v1", "sessions"])
                            .with_bearer(&secret)
                            .with_headers(vec![(
                                HeaderName::from_static("x-door-instance"),
                                HeaderValue::from_static("01JABCDEFGHJKMNPQRSTVWXYZ0"),
                            )])
                            .with_body(Body::Json(body.as_bytes().to_vec())),
                        ByteCap::ONE_MIB,
                        PATIENT,
                    )
                    .await
                    .unwrap();

                assert_eq!(reply.status(), StatusCode::CREATED);
                assert_eq!(reply.body(), br#"{"session":"tui-01"}"#);
                assert_eq!(
                    utf8(sole(&peer.stub).bytes()),
                    format!(
                        "POST /v1/sessions HTTP/1.1\r\n\
                         host: {}\r\n\
                         authorization: Bearer {TOKEN}\r\n\
                         content-type: application/json\r\n\
                         content-length: {}\r\n\
                         connection: close\r\n\
                         x-door-instance: 01JABCDEFGHJKMNPQRSTVWXYZ0\r\n\r\n\
                         {body}",
                        peer.host,
                        body.len()
                    ),
                    "{transport:?}"
                );
                assert_eq!(ends(&peer.stub, 1).await, [End::Answered]);
            }
        });
    }

    #[test]
    fn a_request_with_no_bearer_and_no_body_has_three_lines() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            peer.stub
                .script(Answer::status(StatusCode::UNAUTHORIZED).body("{}"));

            let reply = peer
                .client
                .send(get(), ByteCap::ONE_MIB, PATIENT)
                .await
                .unwrap();

            // The verify hook of `attendance` wants this answer
            // (attendance/src/attendance/verify.py:146-160).
            assert_eq!(reply.status(), StatusCode::UNAUTHORIZED);
            assert_eq!(
                utf8(sole(&peer.stub).bytes()),
                "GET /v1/sessions HTTP/1.1\r\nhost: sessiond\r\nconnection: close\r\n\r\n"
            );
        });
    }

    #[test]
    fn a_body_of_no_byte_has_the_length_that_httpx_gives_its_method() {
        runtime().block_on(async {
            for (method, length) in [
                (Method::POST, Some("0")),
                (Method::PUT, Some("0")),
                (Method::PATCH, Some("0")),
                (Method::GET, None),
                (Method::DELETE, None),
                (Method::HEAD, None),
            ] {
                let peer = peer(Transport::Unix).await;
                peer.stub
                    .script(Answer::status(StatusCode::NO_CONTENT).no_body());

                let reply = peer
                    .client
                    .send(
                        request(method.clone(), &["v1", "x"]),
                        ByteCap::ONE_MIB,
                        PATIENT,
                    )
                    .await
                    .unwrap();
                let sent = sole(&peer.stub);

                assert_eq!(reply.status(), StatusCode::NO_CONTENT, "{method}");
                assert_eq!(reply.body(), b"", "{method}");
                assert_eq!(sent.method(), method.as_str());
                assert_eq!(
                    sent.header("content-length"),
                    length.map(str::as_bytes),
                    "{method}"
                );
                assert_eq!(sent.header("content-type"), None, "{method}");
            }
        });
    }

    #[test]
    fn a_header_of_a_caller_with_a_name_of_the_client_is_not_sent() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let secret = token();
            let header = |name: &'static str, value: &'static str| {
                (
                    HeaderName::from_static(name),
                    HeaderValue::from_static(value),
                )
            };
            peer.stub.script(Answer::status(StatusCode::OK).body("{}"));

            peer.client
                .send(
                    request(Method::POST, &["v1", "x"])
                        .with_bearer(&secret)
                        .with_headers(vec![
                            header("host", "other"),
                            header("authorization", "Bearer other"),
                            header("content-type", "text/plain"),
                            header("content-length", "999"),
                            header("connection", "keep-alive"),
                            header("transfer-encoding", "chunked"),
                            header("x-door-instance", "one"),
                            header("accept", "application/x-ndjson"),
                            header("x-door-instance", "two"),
                        ])
                        .with_body(Body::Json(b"{}".to_vec())),
                    ByteCap::ONE_MIB,
                    PATIENT,
                )
                .await
                .unwrap();

            assert_eq!(
                utf8(sole(&peer.stub).bytes()),
                format!(
                    "POST /v1/x HTTP/1.1\r\n\
                     host: sessiond\r\n\
                     authorization: Bearer {TOKEN}\r\n\
                     content-type: application/json\r\n\
                     content-length: 2\r\n\
                     connection: close\r\n\
                     x-door-instance: one\r\n\
                     x-door-instance: two\r\n\
                     accept: application/x-ndjson\r\n\r\n\
                     {{}}"
                )
            );
        });
    }

    #[test]
    fn a_url_with_a_path_keeps_the_path_before_each_request_path() {
        runtime().block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();

            for (base, target) in [
                ("", "/v1/sessions?limit=1"),
                ("/", "/v1/sessions?limit=1"),
                ("/api", "/api/v1/sessions?limit=1"),
                ("/api/", "/api/v1/sessions?limit=1"),
                ("/api/v2", "/api/v2/v1/sessions?limit=1"),
                ("/a/../b/./c", "/b/c/v1/sessions?limit=1"),
            ] {
                let url: HttpUrl = format!("http://127.0.0.1:{port}{base}").parse().unwrap();
                let client = Client::new(Target::try_from(&url).unwrap());
                stub.script(Answer::status(StatusCode::OK).body("{}"));

                client
                    .send(
                        Request::new(
                            Method::GET,
                            PathAndQuery::from_segments(&["v1", "sessions"])
                                .with_query(&[("limit", "1")]),
                            Timeouts::each(PATIENT),
                        ),
                        ByteCap::ONE_MIB,
                        PATIENT,
                    )
                    .await
                    .unwrap();

                let sent = stub.requests().pop().unwrap();
                assert_eq!(sent.target(), target, "{base:?}");
                assert_eq!(
                    sent.header("host"),
                    Some(format!("127.0.0.1:{port}").as_bytes()),
                    "{base:?}"
                );
            }
        });
    }

    #[test]
    fn a_target_with_a_host_name_connects_to_the_address_of_the_name() {
        runtime().block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            let url: HttpUrl = format!("http://LocalHost:{port}").parse().unwrap();
            let client = Client::new(Target::try_from(&url).unwrap());
            stub.script(Answer::status(StatusCode::OK).body("{}"));

            let reply = client.send(get(), ByteCap::ONE_MIB, PATIENT).await.unwrap();

            assert_eq!(reply.status(), StatusCode::OK);
            assert_eq!(
                sole(&stub).header("host"),
                Some(format!("localhost:{port}").as_bytes())
            );
        });
    }

    #[test]
    fn a_target_of_more_bytes_than_a_request_line_holds_is_refused() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let long = "a".repeat(70_000);

            let sent = peer
                .client
                .send(
                    request(Method::GET, &[long.as_str()]),
                    ByteCap::ONE_MIB,
                    PATIENT,
                )
                .await;

            assert_eq!(sent, Err(ClientError::Protocol));
            assert!(peer.stub.requests().is_empty());
        });
    }

    // --- the answer ---

    #[test]
    fn an_answer_with_each_status_is_a_reply() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;

                for status in [
                    StatusCode::OK,
                    StatusCode::ACCEPTED,
                    StatusCode::FOUND,
                    StatusCode::UNAUTHORIZED,
                    StatusCode::NOT_FOUND,
                    StatusCode::CONFLICT,
                    StatusCode::INTERNAL_SERVER_ERROR,
                    StatusCode::SERVICE_UNAVAILABLE,
                ] {
                    peer.stub.script(
                        Answer::status(status)
                            .header("x-turn", "t-1")
                            .body(r#"{"error":{"code":"x"}}"#),
                    );

                    let reply = peer
                        .client
                        .send(get(), ByteCap::ONE_MIB, PATIENT)
                        .await
                        .unwrap();

                    assert_eq!(reply.status(), status);
                    assert_eq!(reply.headers()["x-turn"], "t-1");
                    assert_eq!(reply.body(), br#"{"error":{"code":"x"}}"#);
                }
            }
        });
    }

    #[test]
    fn a_body_in_each_form_is_read_whole() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let head = || Answer::status(StatusCode::OK);
                // A body with a length, a body in chunks, and a body that
                // ends at the close of the connection.
                peer.stub.script(head().body("one two three"));
                peer.stub.script(
                    head()
                        .chunked()
                        .chunk("one ")
                        .pause(BEAT)
                        .chunk("two ")
                        .chunk("three")
                        .end(),
                );
                peer.stub.script(Answer::raw(
                    "HTTP/1.1 200 OK\r\nconnection: close\r\n\r\none two three",
                ));
                peer.stub.script(head().no_body());

                for body in ["one two three", "one two three", "one two three", ""] {
                    let reply = peer
                        .client
                        .send(get(), ByteCap::ONE_MIB, PATIENT)
                        .await
                        .unwrap();

                    assert_eq!(utf8(reply.body()), body, "{transport:?}");
                }
            }
        });
    }

    #[test]
    fn a_body_with_a_trailer_is_read_whole() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let trailed = || {
                Answer::raw(
                    "HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\ntrailer: x-sum\r\n\r\n\
                     3\r\none\r\n4\r\n two\r\n0\r\nx-sum: 7\r\n\r\n",
                )
            };
            peer.stub.script(trailed());
            peer.stub.script(trailed());

            let reply = peer
                .client
                .send(get(), ByteCap::ONE_MIB, PATIENT)
                .await
                .unwrap();
            let mut stream = peer
                .client
                .open(get(), StreamLimit::Within(PATIENT))
                .await
                .unwrap();

            // The trailer is no chunk and no error.
            assert_eq!(reply.body(), b"one two");
            assert_eq!(drain(&mut stream).await, (b"one two".to_vec(), Ok(())));
        });
    }

    #[test]
    fn a_redirect_is_an_answer_and_the_client_follows_none() {
        // `httpx` follows no redirect by default, and no Python call site
        // asks for one. The two clients thus do the same.
        runtime().block_on(async {
            let peer = peer(Transport::Loopback).await;
            peer.stub.script(
                Answer::status(StatusCode::FOUND)
                    .header("location", "/v1/elsewhere")
                    .body(""),
            );
            peer.stub
                .script(Answer::status(StatusCode::OK).body("second"));

            let reply = peer
                .client
                .send(get(), ByteCap::ONE_MIB, PATIENT)
                .await
                .unwrap();

            assert_eq!(reply.status(), StatusCode::FOUND);
            assert_eq!(reply.headers()["location"], "/v1/elsewhere");
            assert_eq!(ends(&peer.stub, 1).await, [End::Answered]);
            assert_eq!(sole(&peer.stub).target(), "/v1/sessions");
        });
    }

    #[test]
    fn bytes_that_are_no_answer_give_a_protocol_error() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let answers = [
                    Answer::close(),
                    Answer::raw("these bytes are no status line\r\n\r\n"),
                    Answer::raw("HTTP/1.1 200 OK\r\ncontent-len"),
                    Answer::raw("HTTP/1.1 200 OK\r\ncontent-length: 9\r\n\r\nhalf"),
                    Answer::raw(
                        "HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\nzz\r\nhalf\r\n",
                    ),
                    Answer::status(StatusCode::OK).chunked().chunk("one").cut(),
                ];

                for answer in answers {
                    peer.stub.script(answer.clone());

                    let sent = peer.client.send(get(), ByteCap::ONE_MIB, PATIENT).await;

                    assert_eq!(sent, Err(ClientError::Protocol), "{answer:?}");
                }
            }
        });
    }

    #[test]
    fn a_head_past_the_bounds_of_the_parser_is_refused() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let head = |lines: &str| {
                Answer::raw(format!(
                    "HTTP/1.1 200 OK\r\n{lines}content-length: 2\r\n\r\nok"
                ))
            };
            // hyper reads a head of about 400 KiB at most, and 100 headers
            // at most. The client thus holds no head with no end.
            let megabyte = "x-pad: 0123456789012345678901234567890123456789\r\n".repeat(21_000);
            let many = "x-pad: 1\r\n".repeat(150);

            for lines in [megabyte.as_str(), many.as_str()] {
                peer.stub.script(head(lines));

                let sent = peer.client.send(get(), ByteCap::ONE_MIB, PATIENT).await;

                assert_eq!(sent, Err(ClientError::Protocol), "{}", lines.len());
            }

            peer.stub.script(head("x-pad: 1\r\n"));
            let reply = peer
                .client
                .send(get(), ByteCap::ONE_MIB, PATIENT)
                .await
                .unwrap();

            assert_eq!(reply.body(), b"ok");
        });
    }

    #[test]
    fn a_body_past_the_cap_is_refused() {
        runtime().block_on(async {
            let cap = ByteCap::new(8).unwrap();
            let head = || Answer::status(StatusCode::OK);
            let table = [
                (head().body("12345678"), Ok("12345678")),
                (
                    head().chunked().chunk("1234").chunk("5678").end(),
                    Ok("12345678"),
                ),
                (
                    head().body("123456789"),
                    Err(ClientError::BodyTooLarge { cap }),
                ),
                (
                    head()
                        .chunked()
                        .chunk("12345")
                        .pause(BEAT)
                        .chunk("6789")
                        .end(),
                    Err(ClientError::BodyTooLarge { cap }),
                ),
                (
                    Answer::raw("HTTP/1.1 200 OK\r\nconnection: close\r\n\r\n123456789"),
                    Err(ClientError::BodyTooLarge { cap }),
                ),
                // The length of the head is past the cap. The client reads
                // no byte of the body.
                (
                    Answer::raw("HTTP/1.1 200 OK\r\ncontent-length: 9000000000\r\n\r\n12"),
                    Err(ClientError::BodyTooLarge { cap }),
                ),
            ];

            for transport in TRANSPORTS {
                let peer = peer(transport).await;

                for (answer, result) in &table {
                    peer.stub.script(answer.clone());

                    let sent = peer.client.send(get(), cap, PATIENT).await;

                    assert_eq!(
                        sent.map(|reply| String::from_utf8(reply.into_body()).unwrap()),
                        result.clone().map(String::from),
                        "{answer:?}"
                    );
                }
            }
        });
    }

    // --- the limits ---

    #[test]
    fn a_connect_that_no_peer_completes_ends_at_the_connect_limit() {
        runtime().block_on(async {
            let client = silent();
            let limits = Timeouts::each(PATIENT).with_connect(SHORT);

            let sent = client
                .send(get_within(limits), ByteCap::ONE_MIB, PATIENT)
                .await;
            let opened = client
                .open(get_within(limits), StreamLimit::Within(PATIENT))
                .await;

            assert_eq!(sent, Err(ClientError::TimedOut(Phase::Connect)));
            assert!(matches!(opened, Err(ClientError::TimedOut(Phase::Connect))));
        });
    }

    /// The count of bytes of a body that no socket buffer holds. The write
    /// of such a request waits for its peer.
    const LARGE_BODY: usize = 8 * 1024 * 1024;

    /// A request with a body of [`LARGE_BODY`] bytes and these limits.
    fn large<'a>(limits: Timeouts) -> Request<'a> {
        Request::new(
            Method::POST,
            PathAndQuery::from_segments(&["v1", "sessions"]),
            limits,
        )
        .with_body(Body::Json(vec![b' '; LARGE_BODY]))
    }

    #[test]
    fn a_write_that_the_peer_does_not_take_ends_at_the_write_limit() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            // The system completes the connect and then takes bytes only
            // until its buffers are full.
            peer.stub.stop_accepting();
            // The read limit is the shorter one, and it must not run: a
            // request that is on its way is not an answer that is late.
            let limits = Timeouts::each(PATIENT)
                .with_write(Duration::from_millis(300))
                .with_read_idle(Some(Duration::from_millis(50)));

            let sent = peer
                .client
                .send(large(limits), ByteCap::ONE_MIB, PATIENT)
                .await;
            let opened = peer
                .client
                .open(large(limits), StreamLimit::Within(PATIENT))
                .await;

            assert_eq!(sent, Err(ClientError::TimedOut(Phase::Write)));
            assert!(matches!(opened, Err(ClientError::TimedOut(Phase::Write))));
        });
    }

    #[test]
    fn a_request_that_the_peer_takes_too_slowly_ends_at_the_write_limit() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = root.path().join("slow.sock");
            let listener = tokio::net::UnixListener::bind(&path).unwrap();
            // A peer that takes 1 KiB each 10 ms. Each write of the client
            // moves some bytes, and the whole request needs more than one
            // minute. The limit is for the whole write, so it passes.
            let slow = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut taken = [0_u8; 1024];
                while stream.read(&mut taken).await.unwrap_or(0) > 0 {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            });
            let limits = Timeouts::each(PATIENT)
                .with_write(Duration::from_millis(300))
                .with_read_idle(Some(Duration::from_millis(50)));

            let sent = client_of(&path)
                .send(large(limits), ByteCap::ONE_MIB, PATIENT)
                .await;

            assert_eq!(sent, Err(ClientError::TimedOut(Phase::Write)));
            // The call closed its connection, so the peer read to the end.
            tokio::time::timeout(PATIENT, slow).await.unwrap().unwrap();
        });
    }

    #[test]
    fn the_read_limit_starts_when_the_request_is_written() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = root.path().join("late.sock");
            let listener = tokio::net::UnixListener::bind(&path).unwrap();
            let pause = Duration::from_millis(600);
            let read_idle = Duration::from_millis(300);
            // A peer that takes no byte for the pause, then takes each byte,
            // and gives no answer.
            let late = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                tokio::time::sleep(pause).await;
                let mut taken = vec![0_u8; 64 * 1024];
                while stream.read(&mut taken).await.unwrap_or(0) > 0 {}
            });
            let started = Instant::now();

            let sent = client_of(&path)
                .send(
                    large(Timeouts::each(PATIENT).with_read_idle(Some(read_idle))),
                    ByteCap::ONE_MIB,
                    PATIENT,
                )
                .await;

            // The read limit is shorter than the pause. It did not end the
            // call while the request was on its way, and it ran in full
            // after the write.
            assert_eq!(sent, Err(ClientError::TimedOut(Phase::ReadIdle)));
            assert!(started.elapsed() >= pause + read_idle);
            // The call closed its connection, so the peer read to the end.
            tokio::time::timeout(PATIENT, late).await.unwrap().unwrap();
        });
    }

    #[test]
    fn a_limit_of_zero_and_the_longest_limit_do_not_panic() {
        runtime().block_on(async {
            let zero = Timeouts::each(Duration::ZERO);
            let sent = silent()
                .send(get_within(zero), ByteCap::ONE_MIB, PATIENT)
                .await;
            let total = silent().send(get(), ByteCap::ONE_MIB, Duration::ZERO).await;

            assert_eq!(sent, Err(ClientError::TimedOut(Phase::Connect)));
            assert_eq!(total, Err(ClientError::TimedOut(Phase::Total)));

            // A time past the range of the clock is a limit that never
            // passes.
            let peer = peer(Transport::Unix).await;
            let longest = Timeouts::each(Duration::MAX);
            peer.stub.script(Answer::status(StatusCode::OK).body("one"));
            peer.stub.script(
                Answer::status(StatusCode::OK)
                    .chunked()
                    .chunk("two")
                    .pause(BEAT)
                    .chunk("three")
                    .end(),
            );

            let reply = peer
                .client
                .send(get_within(longest), ByteCap::ONE_MIB, Duration::MAX)
                .await
                .unwrap();
            let mut stream = peer
                .client
                .open(get_within(longest), StreamLimit::Within(Duration::MAX))
                .await
                .unwrap();

            assert_eq!(reply.body(), b"one");
            assert_eq!(drain(&mut stream).await, (b"twothree".to_vec(), Ok(())));
        });
    }

    /// One read of `watched` that does not wait.
    async fn read_now(watched: &mut Watched) -> Poll<io::Result<()>> {
        let mut found = [0_u8; 8];

        poll_fn(|cx| {
            let mut buffer = ReadBuf::new(&mut found);

            Poll::Ready(Pin::new(&mut *watched).poll_read(cx, &mut buffer))
        })
        .await
    }

    /// Writes `bytes` to `watched` when the socket takes them.
    async fn write_to(watched: &mut Watched, bytes: &[u8]) -> io::Result<usize> {
        poll_fn(|cx| Pin::new(&mut *watched).poll_write(cx, bytes)).await
    }

    #[test]
    fn the_wait_of_the_read_does_not_run_while_the_request_is_on_its_way() {
        runtime().block_on(async {
            let (near, _far) = UnixStream::pair().unwrap();
            let mut watched = Watched::new(
                Stream::Unix(near),
                Timeouts::each(PATIENT),
                Arc::new(OnceLock::new()),
            );

            // No byte comes, so the read starts its wait.
            assert!(read_now(&mut watched).await.is_pending());
            assert!(watched.read.runs());
            assert!(!watched.write.runs());

            // The first write of the request starts the wait of the write.
            // The peer has no whole request, so the wait of the read stops.
            assert_eq!(write_to(&mut watched, b"GET / ").await.unwrap(), 6);
            assert!(watched.write.runs());
            assert!(!watched.read.runs());

            // A read with no byte in that time starts no wait of its own.
            assert!(read_now(&mut watched).await.is_pending());
            assert!(!watched.read.runs());

            // The next write keeps the wait of the write.
            let slices = [io::IoSlice::new(b"HTTP/1.1\r\n"), io::IoSlice::new(b"\r\n")];
            let wrote = poll_fn(|cx| Pin::new(&mut watched).poll_write_vectored(cx, &slices)).await;
            assert_eq!(wrote.unwrap(), 12);
            assert!(watched.write.runs());
            assert!(!watched.read.runs());

            // The flush ends the write, and the wait of the read starts
            // again.
            poll_fn(|cx| Pin::new(&mut watched).poll_flush(cx))
                .await
                .unwrap();
            assert!(!watched.write.runs());
            assert!(watched.read.runs());

            // A flush with no write before it changes nothing.
            poll_fn(|cx| Pin::new(&mut watched).poll_flush(cx))
                .await
                .unwrap();
            assert!(!watched.write.runs());
            assert!(watched.read.runs());
        });
    }

    #[test]
    fn a_write_past_its_limit_gives_an_error_and_names_its_phase() {
        runtime().block_on(async {
            let (near, _far) = UnixStream::pair().unwrap();
            let stall = Arc::new(OnceLock::new());
            let mut watched = Watched::new(
                Stream::Unix(near),
                Timeouts::each(PATIENT).with_write(SHORT),
                Arc::clone(&stall),
            );

            assert_eq!(write_to(&mut watched, b"GET / ").await.unwrap(), 6);
            assert_eq!(stall.get(), None);

            // No flush ends the write. After the limit, the next write
            // fails, also when the socket can take its bytes.
            tokio::time::sleep(SHORT * 2).await;
            let error = write_to(&mut watched, b"HTTP/1.1").await.unwrap_err();

            assert_eq!(error.kind(), io::ErrorKind::TimedOut);
            assert_eq!(stall.get(), Some(&Phase::Write));
        });
    }

    #[test]
    fn a_read_past_its_limit_gives_an_error_and_names_its_phase() {
        runtime().block_on(async {
            let (near, _far) = UnixStream::pair().unwrap();
            let stall = Arc::new(OnceLock::new());
            let mut watched = Watched::new(
                Stream::Unix(near),
                Timeouts::each(PATIENT).with_read_idle(Some(SHORT)),
                Arc::clone(&stall),
            );
            let mut found = [0_u8; 8];

            // No byte comes. The read waits to its limit and no longer.
            let error = poll_fn(|cx| {
                let mut buffer = ReadBuf::new(&mut found);

                Pin::new(&mut watched).poll_read(cx, &mut buffer)
            })
            .await
            .unwrap_err();

            assert_eq!(error.kind(), io::ErrorKind::TimedOut);
            assert_eq!(stall.get(), Some(&Phase::ReadIdle));
        });
    }

    #[test]
    fn a_read_with_no_limit_starts_no_wait() {
        runtime().block_on(async {
            let (near, _far) = UnixStream::pair().unwrap();
            let mut watched = Watched::new(
                Stream::Unix(near),
                Timeouts::each(PATIENT).with_read_idle(None),
                Arc::new(OnceLock::new()),
            );

            assert!(read_now(&mut watched).await.is_pending());
            assert!(!watched.read.runs());
        });
    }

    #[test]
    fn a_small_request_to_a_peer_that_never_reads_ends_at_the_read_limit() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                peer.stub.stop_accepting();

                let sent = peer
                    .client
                    .send(
                        get_within(Timeouts::each(PATIENT).with_read_idle(Some(SHORT))),
                        ByteCap::ONE_MIB,
                        PATIENT,
                    )
                    .await;

                assert_eq!(
                    sent,
                    Err(ClientError::TimedOut(Phase::ReadIdle)),
                    "{transport:?}"
                );
            }
        });
    }

    #[test]
    fn a_head_that_comes_late_ends_at_the_read_limit() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let limits = Timeouts::each(PATIENT).with_read_idle(Some(SHORT));
                peer.stub
                    .script(Answer::status(StatusCode::OK).after(HOLD).body("late"));
                peer.stub.script(Answer::hold());

                let sent = peer
                    .client
                    .send(get_within(limits), ByteCap::ONE_MIB, PATIENT)
                    .await;
                let opened = peer
                    .client
                    .open(get_within(limits), StreamLimit::Within(PATIENT))
                    .await;

                assert_eq!(sent, Err(ClientError::TimedOut(Phase::ReadIdle)));
                assert!(matches!(
                    opened,
                    Err(ClientError::TimedOut(Phase::ReadIdle))
                ));
                // Each call closed its connection at the limit.
                assert_eq!(
                    ends(&peer.stub, 2).await,
                    [End::ClientLeft, End::ClientLeft],
                    "{transport:?}"
                );
            }
        });
    }

    #[test]
    fn a_pause_between_two_chunks_ends_at_the_read_limit() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let late = || {
                    Answer::status(StatusCode::OK)
                        .chunked()
                        .chunk("one\n")
                        .pause(HOLD)
                        .chunk("two\n")
                        .end()
                };
                peer.stub.script(late());
                peer.stub.script(late());

                let sent = peer
                    .client
                    .send(
                        get_within(Timeouts::each(PATIENT).with_read_idle(Some(SHORT))),
                        ByteCap::ONE_MIB,
                        PATIENT,
                    )
                    .await;
                assert_eq!(sent, Err(ClientError::TimedOut(Phase::ReadIdle)));

                let mut stream = peer
                    .client
                    .open(
                        get_within(Timeouts::each(PATIENT).with_read_idle(Some(GAP))),
                        StreamLimit::Within(PATIENT),
                    )
                    .await
                    .unwrap();

                assert_eq!(stream.chunk().await.unwrap().unwrap(), "one\n");
                assert_eq!(
                    stream.chunk().await,
                    Err(ClientError::TimedOut(Phase::ReadIdle))
                );
                // The stream closed its connection at the limit, and it
                // gives the same end again.
                assert_eq!(
                    ends(&peer.stub, 2).await,
                    [End::ClientLeft, End::ClientLeft],
                    "{transport:?}"
                );
                assert_eq!(
                    stream.chunk().await,
                    Err(ClientError::TimedOut(Phase::ReadIdle))
                );
            }
        });
    }

    #[test]
    fn the_read_limit_is_the_time_between_two_chunks_and_not_of_the_answer() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let pause = Duration::from_millis(500);
            let read_idle = Duration::from_secs(2);
            // Six chunks, one each 500 ms. The answer takes longer than the
            // read limit, and no chunk comes later than the limit.
            let slow = || {
                let mut body = Answer::status(StatusCode::OK).chunked();
                for _ in 0..6 {
                    body = body.pause(pause).chunk("beat\n");
                }

                body.end()
            };
            let limits = Timeouts::each(PATIENT).with_read_idle(Some(read_idle));
            peer.stub.script(slow());
            peer.stub.script(slow());
            let started = Instant::now();

            let (sent, opened) = tokio::join!(
                peer.client
                    .send(get_within(limits), ByteCap::ONE_MIB, PATIENT),
                peer.client
                    .open(get_within(limits), StreamLimit::Within(PATIENT)),
            );
            let mut stream = opened.unwrap();
            let streamed = drain(&mut stream).await;

            assert_eq!(utf8(sent.unwrap().body()), "beat\n".repeat(6));
            assert_eq!(streamed, ("beat\n".repeat(6).into_bytes(), Ok(())));
            assert!(started.elapsed() > read_idle);
        });
    }

    #[test]
    fn a_stream_with_no_read_limit_waits_for_a_late_chunk() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            peer.stub.script(
                Answer::status(StatusCode::OK)
                    .chunked()
                    .chunk("one\n")
                    .pause(Duration::from_millis(400))
                    .chunk("two\n")
                    .end(),
            );

            let mut stream = peer
                .client
                .open(
                    get_within(Timeouts::each(PATIENT).with_read_idle(None)),
                    StreamLimit::Within(PATIENT),
                )
                .await
                .unwrap();

            assert_eq!(drain(&mut stream).await, (b"one\ntwo\n".to_vec(), Ok(())));
        });
    }

    #[test]
    fn chunks_with_no_end_stop_at_the_total_limit() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let endless = || {
                    Answer::status(StatusCode::OK)
                        .chunked()
                        .forever("beat\n", Duration::from_millis(10))
                };
                let total = Duration::from_millis(300);
                peer.stub.script(endless());
                peer.stub.script(endless());

                // No phase passes its limit: a chunk comes each 10 ms.
                let sent = peer.client.send(get(), ByteCap::ONE_MIB, total).await;
                assert_eq!(sent, Err(ClientError::TimedOut(Phase::Total)));

                let mut stream = peer
                    .client
                    .open(get(), StreamLimit::Within(total))
                    .await
                    .unwrap();
                let (bytes, end) = drain(&mut stream).await;

                assert_eq!(end, Err(ClientError::TimedOut(Phase::Total)));
                assert!(
                    bytes.chunks(5).all(|beat| beat == b"beat\n"),
                    "{transport:?}"
                );
                assert_eq!(
                    stream.chunk().await,
                    Err(ClientError::TimedOut(Phase::Total))
                );
                assert_eq!(
                    ends(&peer.stub, 2).await,
                    [End::ClientLeft, End::ClientLeft],
                    "{transport:?}"
                );
            }
        });
    }

    #[test]
    fn the_total_limit_covers_the_connect_and_the_wait_for_the_head() {
        runtime().block_on(async {
            let sent = silent().send(get(), ByteCap::ONE_MIB, SHORT).await;
            let opened = silent().open(get(), StreamLimit::Within(SHORT)).await;

            assert_eq!(sent, Err(ClientError::TimedOut(Phase::Total)));
            assert!(matches!(opened, Err(ClientError::TimedOut(Phase::Total))));

            let peer = peer(Transport::Unix).await;
            peer.stub.script(Answer::hold());
            peer.stub.script(Answer::hold());

            let sent = peer.client.send(get(), ByteCap::ONE_MIB, SHORT).await;
            let opened = peer.client.open(get(), StreamLimit::Within(SHORT)).await;

            assert_eq!(sent, Err(ClientError::TimedOut(Phase::Total)));
            assert!(matches!(opened, Err(ClientError::TimedOut(Phase::Total))));
        });
    }

    // --- the connect ---

    #[test]
    fn a_connect_that_the_system_refuses_gives_its_text() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            // A socket file that refuses each connect, and a path with no
            // file.
            let closed = refused_socket(&root).unwrap();
            let absent = root.path().join("absent.sock");

            for (path, os_text) in [
                (closed, "Connection refused"),
                (absent, "No such file or directory"),
            ] {
                let client = client_of(&path);
                // The text has no path, no error number and no name of a
                // type.
                let error = ClientError::Connect {
                    os_text: String::from(os_text),
                };

                let sent = client.send(get(), ByteCap::ONE_MIB, PATIENT).await;
                let opened = client.open(get(), StreamLimit::Within(PATIENT)).await;

                assert_eq!(sent, Err(error.clone()));
                assert_eq!(opened.unwrap_err(), error);
            }
        });
    }

    // --- the stream ---

    #[test]
    fn a_stream_gives_each_chunk_and_then_its_end() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let secret = token();
                peer.stub.script(
                    Answer::status(StatusCode::OK)
                        .header("content-type", "application/x-ndjson")
                        .chunked()
                        .chunk("{\"seq\":1}\n")
                        .pause(BEAT)
                        .chunk("{\"seq\":2}\n{\"se")
                        .pause(BEAT)
                        .chunk("q\":3}\n")
                        .end(),
                );

                let mut stream = peer
                    .client
                    .open(
                        request(Method::POST, &["v1", "sessions", "chat", "s-1", "turns"])
                            .with_bearer(&secret)
                            .with_body(Body::Json(br#"{"wait":"stream"}"#.to_vec())),
                        StreamLimit::Within(PATIENT),
                    )
                    .await
                    .unwrap();

                assert_eq!(stream.status(), StatusCode::OK);
                assert_eq!(stream.headers()["content-type"], "application/x-ndjson");
                assert!(!format!("{stream:?}").contains("seq"));
                // The future of a chunk can move to another thread, and a
                // caller can drop it.
                sendable(stream.chunk());

                let mut chunks = Vec::new();
                while let Some(chunk) = stream.chunk().await.unwrap() {
                    chunks.push(chunk);
                }

                assert_eq!(
                    chunks,
                    ["{\"seq\":1}\n", "{\"seq\":2}\n{\"se", "q\":3}\n"],
                    "{transport:?}"
                );
                assert_eq!(stream.chunk().await, Ok(None));
                assert_eq!(stream.chunk().await, Ok(None));
                assert_eq!(ends(&peer.stub, 1).await, [End::Answered]);

                let sent = sole(&peer.stub);
                assert_eq!(sent.target(), "/v1/sessions/chat/s-1/turns");
                assert_eq!(sent.body(), br#"{"wait":"stream"}"#);
                assert_eq!(
                    sent.header("authorization"),
                    Some(format!("Bearer {TOKEN}").as_bytes())
                );
            }
        });
    }

    #[test]
    fn a_stream_of_an_answer_with_a_length_gives_each_byte() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            peer.stub.script(
                Answer::status(StatusCode::CONFLICT).body(r#"{"error":{"code":"turn_running"}}"#),
            );
            peer.stub
                .script(Answer::status(StatusCode::NO_CONTENT).no_body());

            // A door reads the refusal of a turn from the stream
            // (door-owui/src/agent_door_owui/attendance.py:198-207).
            let mut refusal = peer
                .client
                .open(get(), StreamLimit::Within(PATIENT))
                .await
                .unwrap();
            assert_eq!(refusal.status(), StatusCode::CONFLICT);
            assert_eq!(
                drain(&mut refusal).await,
                (br#"{"error":{"code":"turn_running"}}"#.to_vec(), Ok(()))
            );

            let mut empty = peer
                .client
                .open(get(), StreamLimit::Within(PATIENT))
                .await
                .unwrap();
            assert_eq!(drain(&mut empty).await, (Vec::new(), Ok(())));
        });
    }

    #[test]
    fn a_stream_that_the_server_cuts_gives_its_chunks_and_then_an_error() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                peer.stub.script(
                    Answer::status(StatusCode::OK)
                        .chunked()
                        .chunk("one\n")
                        .pause(BEAT)
                        .chunk("two\n")
                        .cut(),
                );
                peer.stub.script(Answer::raw(
                    "HTTP/1.1 200 OK\r\ncontent-length: 9\r\n\r\nhalf",
                ));

                for bytes in ["one\ntwo\n", "half"] {
                    let mut stream = peer
                        .client
                        .open(get(), StreamLimit::Within(PATIENT))
                        .await
                        .unwrap();

                    assert_eq!(
                        drain(&mut stream).await,
                        (bytes.as_bytes().to_vec(), Err(ClientError::Protocol)),
                        "{transport:?}"
                    );
                    assert_eq!(stream.chunk().await, Err(ClientError::Protocol));
                }
            }
        });
    }

    #[test]
    fn a_stream_at_its_end_closes_the_connection() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = root.path().join("open.sock");
            let listener = tokio::net::UnixListener::bind(&path).unwrap();
            // A peer that gives a whole answer and keeps its side open. It
            // then writes one byte after the other, until the system refuses
            // a byte. The system does that only after the client closed its
            // socket.
            let served = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut seen = Vec::new();
                let mut part = [0_u8; 1024];
                while !seen.ends_with(b"\r\n\r\n") {
                    let count = stream.read(&mut part).await.unwrap();
                    assert!(count > 0, "the client left before its request");
                    seen.extend_from_slice(&part[..count]);
                }
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 3\r\n\r\none")
                    .await
                    .unwrap();

                while stream.write_all(b"x").await.is_ok() {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            });

            let mut stream = client_of(&path)
                .open(get(), StreamLimit::Within(PATIENT))
                .await
                .unwrap();
            assert_eq!(drain(&mut stream).await, (b"one".to_vec(), Ok(())));

            // The stream is not dropped, and its socket is closed.
            tokio::time::timeout(PATIENT, served)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(stream.chunk().await, Ok(None));
        });
    }

    #[test]
    fn a_dropped_stream_closes_the_connection() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                // One chunk, and then an open connection with no byte.
                peer.stub.script(
                    Answer::status(StatusCode::OK)
                        .chunked()
                        .chunk("one\n")
                        .forever("", HOLD),
                );

                let mut stream = peer
                    .client
                    .open(
                        get_within(Timeouts::each(PATIENT).with_read_idle(None)),
                        StreamLimit::Within(PATIENT),
                    )
                    .await
                    .unwrap();
                assert_eq!(stream.chunk().await.unwrap().unwrap(), "one\n");
                assert!(peer.stub.ends().is_empty());

                drop(stream);

                assert_eq!(
                    ends(&peer.stub, 1).await,
                    [End::ClientLeft],
                    "{transport:?}"
                );
            }
        });
    }

    #[test]
    fn a_dropped_call_closes_the_connection() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                peer.stub.script(Answer::hold());
                peer.stub.script(Answer::hold());

                let mut sent = Box::pin(peer.client.send(get(), ByteCap::ONE_MIB, PATIENT));
                tokio::select! {
                    biased;
                    reply = &mut sent => panic!("the stub gave no answer: {reply:?}"),
                    requests = peer.stub.wait_requests(1) => assert_eq!(requests.len(), 1),
                }
                drop(sent);
                assert_eq!(ends(&peer.stub, 1).await, [End::ClientLeft]);

                let mut opened = Box::pin(peer.client.open(get(), StreamLimit::Within(PATIENT)));
                tokio::select! {
                    biased;
                    stream = &mut opened => panic!("the stub gave no head: {stream:?}"),
                    requests = peer.stub.wait_requests(2) => assert_eq!(requests.len(), 2),
                }
                drop(opened);
                assert_eq!(
                    ends(&peer.stub, 2).await,
                    [End::ClientLeft, End::ClientLeft],
                    "{transport:?}"
                );
            }
        });
    }

    #[test]
    fn a_chunk_future_that_a_caller_drops_loses_no_byte() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let pause = Duration::from_millis(200);
                peer.stub.script(
                    Answer::status(StatusCode::OK)
                        .chunked()
                        .chunk("one\n")
                        .pause(pause)
                        .chunk("two\n")
                        .pause(pause)
                        .chunk("three\n")
                        .end(),
                );

                let mut stream = peer
                    .client
                    .open(get(), StreamLimit::Within(PATIENT))
                    .await
                    .unwrap();
                let mut bytes = Vec::new();
                let mut dropped = 0_u32;

                loop {
                    // The select of a door: the chunk, or one more event.
                    let next = tokio::select! {
                        biased;
                        next = stream.chunk() => Some(next),
                        () = tokio::time::sleep(Duration::from_millis(5)) => None,
                    };

                    match next {
                        Some(Ok(Some(chunk))) => bytes.extend_from_slice(&chunk),
                        Some(Ok(None)) => break,
                        Some(Err(error)) => panic!("the stream stopped: {error}"),
                        None => dropped += 1,
                    }
                }

                assert_eq!(utf8(&bytes), "one\ntwo\nthree\n", "{transport:?}");
                // Each pause of the stub is longer than the wait of the
                // select, so the caller dropped a future in each pause.
                assert!(dropped >= 2, "{dropped}");
            }
        });
    }

    #[test]
    fn a_stream_ends_at_the_stop_signal() {
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                let (trigger, shutdown) = shutdown_pair();
                let no_read_limit = Timeouts::each(PATIENT).with_read_idle(None);
                peer.stub.script(
                    Answer::status(StatusCode::OK)
                        .chunked()
                        .chunk("one\n")
                        .forever("", HOLD),
                );

                let mut stream = peer
                    .client
                    .open(
                        get_within(no_read_limit),
                        StreamLimit::UntilShutdown(shutdown.clone()),
                    )
                    .await
                    .unwrap();
                assert_eq!(stream.chunk().await.unwrap().unwrap(), "one\n");

                // The signal comes while a call waits for the next chunk.
                let (next, ()) = tokio::join!(stream.chunk(), async {
                    tokio::time::sleep(SHORT).await;
                    trigger.trigger();
                });

                assert_eq!(next, Err(ClientError::Closed), "{transport:?}");
                // The stream is not dropped, and its connection is closed.
                assert_eq!(ends(&peer.stub, 1).await, [End::ClientLeft]);
                assert_eq!(stream.chunk().await, Err(ClientError::Closed));

                // After the signal, a call opens no connection.
                let opened = peer
                    .client
                    .open(get(), StreamLimit::UntilShutdown(shutdown))
                    .await;

                assert!(matches!(opened, Err(ClientError::Closed)));

                // One more call shows each connection that the stub got: the
                // stream and this call, and none between the two.
                peer.stub.script(Answer::status(StatusCode::OK).body("ok"));
                peer.client
                    .send(get(), ByteCap::ONE_MIB, PATIENT)
                    .await
                    .unwrap();

                assert_eq!(
                    ends(&peer.stub, 2).await,
                    [End::ClientLeft, End::Answered],
                    "{transport:?}"
                );
                assert_eq!(peer.stub.requests().len(), 2);
            }
        });
    }

    #[test]
    fn a_stream_that_ended_gives_its_end_again_after_the_stop_signal() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let (trigger, shutdown) = shutdown_pair();
            peer.stub.script(
                Answer::status(StatusCode::OK)
                    .chunked()
                    .chunk("one\n")
                    .end(),
            );

            let mut stream = peer
                .client
                .open(get(), StreamLimit::UntilShutdown(shutdown))
                .await
                .unwrap();
            assert_eq!(drain(&mut stream).await, (b"one\n".to_vec(), Ok(())));

            // The stream read its end before the signal. The signal does not
            // change that end into an error.
            trigger.trigger();

            assert_eq!(stream.chunk().await, Ok(None));
        });
    }

    #[test]
    fn the_stop_signal_wins_over_a_chunk_that_is_ready() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let (trigger, shutdown) = shutdown_pair();
            // A chunk comes each millisecond, so a chunk is ready at most
            // calls.
            peer.stub.script(
                Answer::status(StatusCode::OK)
                    .chunked()
                    .forever("beat\n", Duration::from_millis(1)),
            );

            let mut stream = peer
                .client
                .open(get(), StreamLimit::UntilShutdown(shutdown))
                .await
                .unwrap();
            assert_eq!(stream.chunk().await.unwrap().unwrap(), "beat\n");

            // Let the socket fill with chunks.
            tokio::time::sleep(SHORT).await;
            trigger.trigger();

            assert_eq!(stream.chunk().await, Err(ClientError::Closed));
        });
    }

    #[test]
    fn the_end_of_a_stream_wins_over_a_chunk_that_the_stream_holds() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let (trigger, shutdown) = shutdown_pair();
            peer.stub.script(
                Answer::status(StatusCode::OK)
                    .chunked()
                    .forever("beat\n", Duration::from_millis(1)),
            );

            let mut stream = peer
                .client
                .open(get(), StreamLimit::UntilShutdown(shutdown))
                .await
                .unwrap();
            assert_eq!(stream.chunk().await.unwrap().unwrap(), "beat\n");
            // Let the socket fill with chunks.
            tokio::time::sleep(SHORT).await;

            // A caller polls a future one time and drops it. That poll moved
            // a chunk from the socket into the stream, and the dropped
            // future did not take it.
            for _ in 0..100 {
                let mut next = Box::pin(stream.chunk());
                let polled = poll_fn(|cx| Poll::Ready(next.as_mut().poll(cx))).await;
                drop(next);

                match polled {
                    Poll::Ready(found) => assert_eq!(found.unwrap().unwrap(), "beat\n"),
                    Poll::Pending => break,
                }
            }

            trigger.trigger();

            assert_eq!(stream.chunk().await, Err(ClientError::Closed));
        });
    }

    // --- what an error holds ---

    #[test]
    fn no_error_text_holds_the_bearer_a_header_or_a_body() {
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            let root = TempRoot::new().unwrap();
            let refused = client_of(&refused_socket(&root).unwrap());
            let secret = token();
            let cap = ByteCap::new(4).unwrap();
            let head = || Answer::status(StatusCode::OK).header("x-secret", HEADER_MARK);
            let marked = |limits: Timeouts| {
                Request::new(
                    Method::POST,
                    PathAndQuery::from_segments(&["v1", "sessions", "chat", "s-1", "turns"]),
                    limits,
                )
                .with_bearer(&secret)
                .with_body(Body::Json(
                    format!(r#"{{"prompt":"{PROMPT_MARK}"}}"#).into_bytes(),
                ))
            };
            let patient = Timeouts::each(PATIENT);
            let hasty = patient.with_read_idle(Some(SHORT));
            let mut errors = Vec::new();

            // A connect that the system refuses.
            errors.push(
                refused
                    .send(marked(patient), cap, PATIENT)
                    .await
                    .unwrap_err(),
            );
            // An answer that is no HTTP/1.1, with the marks in its bytes.
            peer.stub
                .script(Answer::raw(format!("{HEADER_MARK} {BODY_MARK}\r\n\r\n")));
            errors.push(
                peer.client
                    .send(marked(patient), cap, PATIENT)
                    .await
                    .unwrap_err(),
            );
            // A body past the cap.
            peer.stub.script(head().body(BODY_MARK));
            errors.push(
                peer.client
                    .send(marked(patient), cap, PATIENT)
                    .await
                    .unwrap_err(),
            );
            // A body that the peer cuts.
            peer.stub.script(head().chunked().chunk(BODY_MARK).cut());
            errors.push(
                peer.client
                    .send(marked(patient), ByteCap::ONE_MIB, PATIENT)
                    .await
                    .unwrap_err(),
            );
            // A body that stops for longer than the read limit.
            peer.stub
                .script(head().chunked().chunk(BODY_MARK).forever("", HOLD));
            errors.push(
                peer.client
                    .send(marked(hasty), ByteCap::ONE_MIB, PATIENT)
                    .await
                    .unwrap_err(),
            );
            // A call past its total limit.
            peer.stub
                .script(head().chunked().chunk(BODY_MARK).forever("", HOLD));
            errors.push(
                peer.client
                    .send(marked(patient), ByteCap::ONE_MIB, SHORT)
                    .await
                    .unwrap_err(),
            );
            // A stream at the stop signal.
            let (trigger, shutdown) = shutdown_pair();
            peer.stub
                .script(head().chunked().chunk(BODY_MARK).forever("", HOLD));
            let mut stream = peer
                .client
                .open(marked(patient), StreamLimit::UntilShutdown(shutdown))
                .await
                .unwrap();
            trigger.trigger();
            errors.push(stream.chunk().await.unwrap_err());
            // A token that a header cannot hold.
            let bad = Secret::try_from(format!("{TOKEN}\n{TOKEN}")).unwrap();
            errors.push(
                peer.client
                    .send(marked(patient).with_bearer(&bad), cap, PATIENT)
                    .await
                    .unwrap_err(),
            );

            assert_eq!(
                errors,
                [
                    ClientError::Connect {
                        os_text: String::from("Connection refused")
                    },
                    ClientError::Protocol,
                    ClientError::BodyTooLarge { cap },
                    ClientError::Protocol,
                    ClientError::TimedOut(Phase::ReadIdle),
                    ClientError::TimedOut(Phase::Total),
                    ClientError::Closed,
                    ClientError::BadBearer,
                ]
            );
            for error in errors {
                let shown = format!("{error} {error:?}");

                for mark in [TOKEN, PROMPT_MARK, HEADER_MARK, BODY_MARK, "walrus"] {
                    assert!(!shown.contains(mark), "{shown}");
                }
            }
        });
    }

    #[test]
    fn the_debug_of_a_request_prints_no_byte_of_the_bearer_or_of_the_body() {
        let secret = token();
        let shown = format!(
            "{:?}",
            request(Method::POST, &["v1", "sessions"])
                .with_bearer(&secret)
                .with_body(Body::Json(PROMPT_MARK.as_bytes().to_vec()))
        );

        assert!(shown.contains("Secret(<redacted>)"), "{shown}");
        assert!(shown.contains("Json(..)"), "{shown}");
        assert!(!shown.contains(TOKEN), "{shown}");
        assert!(!shown.contains(PROMPT_MARK), "{shown}");
    }

    // --- the runtime ---

    #[test]
    fn a_runtime_with_no_timer_gives_an_error_and_no_panic() {
        let no_driver = Builder::new_current_thread().build().unwrap();
        let (_trigger, shutdown) = shutdown_pair();
        let error = ClientError::Connect {
            os_text: String::from(NO_TIMER),
        };

        no_driver.block_on(async {
            let client = silent();

            let sent = client.send(get(), ByteCap::ONE_MIB, PATIENT).await;
            let within = client.open(get(), StreamLimit::Within(PATIENT)).await;
            let until = client
                .open(get(), StreamLimit::UntilShutdown(shutdown))
                .await;

            assert_eq!(sent, Err(error.clone()));
            assert_eq!(within.unwrap_err(), error);
            assert_eq!(until.unwrap_err(), error);
        });
    }

    #[test]
    fn a_runtime_with_no_io_driver_gives_an_error_and_no_panic() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("listener.sock");
        // A listener of the standard library takes the connect with no
        // runtime.
        let _listener = UnixListener::bind(&path).unwrap();
        let client = client_of(&path);
        let timer_only = Builder::new_current_thread().enable_time().build().unwrap();
        let error = ClientError::Connect {
            os_text: String::from(NO_IO_DRIVER),
        };

        timer_only.block_on(async {
            let sent = client.send(get(), ByteCap::ONE_MIB, PATIENT).await;
            let opened = client.open(get(), StreamLimit::Within(PATIENT)).await;

            assert_eq!(sent, Err(error.clone()));
            assert_eq!(opened.unwrap_err(), error);
        });
    }

    #[test]
    fn a_thread_with_no_runtime_gives_an_error_and_no_panic() {
        let client = silent();
        let mut context = Context::from_waker(Waker::noop());
        let error = ClientError::Connect {
            os_text: String::from(NO_RUNTIME),
        };

        let sent = pin!(client.send(get(), ByteCap::ONE_MIB, PATIENT));
        let opened = pin!(client.open(get(), StreamLimit::Within(PATIENT)));

        assert_eq!(sent.poll(&mut context), Poll::Ready(Err(error.clone())));
        assert!(matches!(
            opened.poll(&mut context),
            Poll::Ready(Err(found)) if found == error
        ));
    }

    #[test]
    fn each_future_and_each_value_of_the_client_can_move_to_another_thread() {
        fn shareable<T: Send + Sync>() {}

        let client = silent();
        let secret = token();

        sendable(client.send(get().with_bearer(&secret), ByteCap::ONE_MIB, PATIENT));
        sendable(client.open(get().with_bearer(&secret), StreamLimit::Within(PATIENT)));
        shareable::<Client>();
        shareable::<Target>();
        shareable::<PathAndQuery>();
        shareable::<ClientError>();
        sendable::<Option<ReplyStream>>(None);
    }

    // --- where the client differs from httpx ---
    //
    // No vector records a transport. A probe of `httpx` 0.28.1 on a socket
    // gave what a comment below says of it.

    #[test]
    fn a_proxy_variable_moves_no_request() {
        // With no socket, `httpx` reads the proxy variables of the
        // environment. It then sends a request for a loopback address to the
        // proxy (door-trigger/src/agent_door_trigger/attendance.py:131-134).
        runtime().block_on(async {
            let target = HttpStub::loopback().await.unwrap();
            let proxy = HttpStub::loopback().await.unwrap();
            target.script(Answer::status(StatusCode::OK).body("target"));
            proxy.script(Answer::status(StatusCode::OK).body("proxy"));

            let mut child = tokio::process::Command::new(std::env::current_exe().unwrap());
            child
                .args([CHILD_TEST, "--exact", "--nocapture", "--test-threads=1"])
                .env(CHILD_VARIABLE, target.port().unwrap().to_string())
                .stdin(Stdio::null())
                .kill_on_drop(true);
            for name in PROXY_VARIABLES {
                child.env(name, format!("http://127.0.0.1:{}", proxy.port().unwrap()));
            }
            for name in NO_PROXY_VARIABLES {
                child.env_remove(name);
            }
            let output = tokio::time::timeout(PATIENT, child.output())
                .await
                .unwrap()
                .unwrap();
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);

            assert!(output.status.success(), "{stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{stdout}");
            assert_eq!(ends(&target, 1).await, [End::Answered]);
            // A request through a proxy names the whole URL in its first
            // line. This one names the path.
            assert_eq!(sole(&target).target(), "/healthz");
            assert!(proxy.requests().is_empty());
            assert!(proxy.ends().is_empty());
        });
    }

    /// The child of [`a_proxy_variable_moves_no_request`]. Without the
    /// variable it does nothing. With the variable it calls the stub at that
    /// port, in a process whose environment names a proxy.
    #[test]
    fn the_child_calls_the_port_of_its_variable() {
        let Some(port) = std::env::var_os(CHILD_VARIABLE) else {
            return;
        };
        for name in PROXY_VARIABLES {
            assert!(std::env::var_os(name).is_some(), "{name}");
        }
        let url: HttpUrl = format!("http://127.0.0.1:{}", port.to_str().unwrap())
            .parse()
            .unwrap();
        let client = Client::new(Target::try_from(&url).unwrap());

        let reply = runtime()
            .block_on(client.send(
                request(Method::GET, &["healthz"]),
                ByteCap::ONE_MIB,
                PATIENT,
            ))
            .unwrap();

        assert_eq!(reply.status(), StatusCode::OK);
        assert_eq!(reply.body(), b"target");
    }

    #[test]
    fn a_request_asks_for_no_compressed_answer() {
        // `httpx` adds three headers to each request: `Accept`,
        // `User-Agent`, and `Accept-Encoding` with gzip and deflate. It
        // decodes a body that the peer compressed
        // (noticeboard/src/noticeboard/attendancehttp.py:47-54).
        runtime().block_on(async {
            let peer = peer(Transport::Unix).await;
            // The first bytes of a gzip stream. The client does not read
            // them as one.
            let packed = [0x1f_u8, 0x8b, 0x08, 0x00];
            peer.stub.script(
                Answer::status(StatusCode::OK)
                    .header("content-encoding", "gzip")
                    .body(packed),
            );

            let reply = peer
                .client
                .send(get(), ByteCap::ONE_MIB, PATIENT)
                .await
                .unwrap();
            let sent = sole(&peer.stub);

            assert_eq!(reply.body(), packed);
            assert_eq!(reply.headers()["content-encoding"], "gzip");
            for name in ["accept", "accept-encoding", "user-agent"] {
                assert_eq!(sent.header(name), None, "{name}");
            }
            let names: Vec<&str> = sent
                .headers()
                .iter()
                .map(|(name, _)| name.as_str())
                .collect();
            assert_eq!(names, ["host", "connection"]);
        });
    }

    #[test]
    fn each_request_has_its_own_connection() {
        // One `httpx` client holds a pool. It writes `Connection:
        // keep-alive` and uses a connection for more than one request
        // (door-tui/src/agent_door_tui/attendance.py:360-367).
        runtime().block_on(async {
            for transport in TRANSPORTS {
                let peer = peer(transport).await;
                // The second answer permits a second request on its
                // connection. The client sends none there.
                peer.stub.script(Answer::status(StatusCode::OK).body("one"));
                peer.stub.script(Answer::raw(
                    "HTTP/1.1 200 OK\r\ncontent-length: 3\r\nconnection: keep-alive\r\n\r\ntwo",
                ));
                peer.stub
                    .script(Answer::status(StatusCode::OK).body("three"));

                for body in ["one", "two", "three"] {
                    let reply = peer
                        .client
                        .send(get(), ByteCap::ONE_MIB, PATIENT)
                        .await
                        .unwrap();

                    assert_eq!(utf8(reply.body()), body);
                }

                assert_eq!(ends(&peer.stub, 3).await.len(), 3, "{transport:?}");
                for sent in peer.stub.requests() {
                    assert_eq!(sent.header("connection"), Some(b"close".as_slice()));
                }
            }
        });
    }

    #[test]
    fn an_id_adds_no_segment_and_removes_none() {
        // A Python client writes a path with an f-string, so a `/` in an id
        // starts a new segment there. `httpx` removes a `.` segment, and a
        // `..` segment with the segment before it
        // (door-owui/src/agent_door_owui/attendance.py:243-244).
        for (id, segment) in [
            ("a/b", "a%2Fb"),
            ("..", "%2E%2E"),
            (".", "%2E"),
            ("a?b", "a%3Fb"),
            ("a#b", "a%23b"),
        ] {
            let path = PathAndQuery::from_segments(&["v1", "sessions", id, "turns"]);

            assert_eq!(path.target, format!("/v1/sessions/{segment}/turns"));
            assert_eq!(path.target.matches('/').count(), 4, "{id}");
        }
    }
}
