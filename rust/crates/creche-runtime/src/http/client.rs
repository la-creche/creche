//! An HTTP/1.1 client over a Unix socket or TCP.
//!
//! A door, the chaperone, `caregiver` and the noticeboard each call
//! `attendance` or another service of the platform. This client is the
//! transport of each such call. What an answer means stays in the service.
//!
//! The client makes one connection for each request and holds no pool. A
//! restart of the server then leaves no dead connection behind.
//!
//! Each call has its limits. [`Timeouts`] holds one limit for each phase, and
//! the limit of a read is the time between two chunks. [`Client::send`] also
//! takes one limit for the whole call. Only a stream that takes
//! [`StreamLimit::UntilShutdown`] has no total limit.
//!
//! The client follows no redirect and reads no proxy variable of the
//! environment. It has no TLS: a [`Target`] from an `https` URL is
//! [`TargetError::TlsNotSupported`].
//!
//! The Python services use `httpx` at ten call sites, for example
//! `noticeboard/src/noticeboard/attendancehttp.py` and the class
//! `HttpAttendance` of `door-owui/src/agent_door_owui/attendance.py`.
//!
//! [`Timeouts`] is complete. Each other function body is a stub. `AGENTS.md`
//! of this crate lists the stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::time::Duration;

use ::http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode};
use bytes::Bytes;
use creche_contracts::config::{AttendanceTarget, BindAddress, HttpUrl, SocketPath};
use creche_contracts::secret::Secret;

use crate::readfile::ByteCap;
use crate::tasks::Shutdown;

/// Where a service answers: a Unix socket, or a host and a port.
///
/// A service builds each target at its start, before the first socket opens.
/// It gives a [`TargetError`] to `service::refuse_start`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target(());

impl Target {
    /// The service behind the Unix socket `socket`. `host` is the value of
    /// the `Host` header of each request.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    #[must_use]
    pub fn unix(socket: SocketPath, host: &'static str) -> Self {
        todo!()
    }
}

impl From<&BindAddress> for Target {
    /// The service that binds `address`, over TCP.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    fn from(address: &BindAddress) -> Self {
        todo!()
    }
}

impl TryFrom<&HttpUrl> for Target {
    type Error = TargetError;

    /// The service at the base URL `url`, over TCP. The target keeps the path
    /// of the URL as the start of each request path.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    fn try_from(url: &HttpUrl) -> Result<Self, Self::Error> {
        todo!()
    }
}

impl TryFrom<&AttendanceTarget> for Target {
    type Error = TargetError;

    /// `attendance`, at the socket or at the URL of the config of a door.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    fn try_from(target: &AttendanceTarget) -> Result<Self, Self::Error> {
        todo!()
    }
}

/// Why a URL is not a target of this client.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetError {
    /// The URL starts with `https://`. The client has no TLS.
    TlsNotSupported,
    /// The URL holds no host and port that the client can connect to.
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
/// ```
/// use std::time::Duration;
///
/// use creche_runtime::http::client::Timeouts;
///
/// // `httpx.Timeout(5.0, read=120.0)` of Python.
/// let timeouts = Timeouts {
///     read_idle: Some(Duration::from_secs(120)),
///     ..Timeouts::each(Duration::from_secs(5))
/// };
///
/// assert_eq!(timeouts.connect, Duration::from_secs(5));
/// assert_eq!(timeouts.write, Duration::from_secs(5));
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Timeouts {
    /// The limit for the connect.
    pub connect: Duration,
    /// The limit for the write of the request.
    pub write: Duration,
    /// The limit for the wait for the head of the answer and for each chunk
    /// after it. `None` for no limit.
    pub read_idle: Option<Duration>,
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
}

/// The path and the query of a request, with each part percent-encoded.
///
/// The caller gives each segment and each query value as plain text. No
/// caller writes a `/` or a `?` by hand, so an id from a request cannot add a
/// segment or a parameter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathAndQuery(());

impl PathAndQuery {
    /// The path with one `/` before each of `segments`.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    #[must_use]
    pub fn from_segments(segments: &[&str]) -> Self {
        todo!()
    }

    /// The path with these query pairs, in order.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    #[must_use]
    pub fn with_query(self, pairs: &[(&str, &str)]) -> Self {
        todo!()
    }
}

/// The body of a request.
///
/// `Debug` prints the count of the bytes and never a byte: a body can hold a
/// prompt.
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
            Self::Json(bytes) => write!(f, "Json(<{} bytes>)", bytes.len()),
        }
    }
}

/// One request.
///
/// The bearer is a [`Secret`], and the client is the one place of this crate
/// that reads its bytes. `Debug` prints no byte of the bearer and no byte of
/// the body.
#[derive(Debug, Clone)]
pub struct Request<'a> {
    /// The method.
    pub method: Method,
    /// The path and the query.
    pub path: PathAndQuery,
    /// The token of the `Authorization` header. `None` for a request with no
    /// such header.
    pub bearer: Option<&'a Secret>,
    /// Each other header. The client writes `Host`, `Authorization`,
    /// `Content-Type`, `Content-Length` and `Connection` itself.
    pub headers: Vec<(HeaderName, HeaderValue)>,
    /// The body.
    pub body: Body,
    /// The limit of each phase.
    pub timeouts: Timeouts,
}

/// When a stream of [`Client::open`] ends at the latest.
#[derive(Debug, Clone)]
pub enum StreamLimit {
    /// The whole stream ends inside this time.
    Within(Duration),
    /// The stream has no total limit. It ends at the stop signal of the
    /// process.
    UntilShutdown(Shutdown),
}

/// The client of one [`Target`].
#[derive(Debug, Clone)]
pub struct Client(());

impl Client {
    /// A client of `target`. The call opens no connection.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    #[must_use]
    pub fn new(target: Target) -> Self {
        todo!()
    }

    /// Sends `request` and reads the whole answer, up to `cap` bytes of body
    /// and inside `total`.
    ///
    /// A caller that drops the future closes the connection.
    ///
    /// # Errors
    ///
    /// [`ClientError`] when the call gives no answer. An answer with each
    /// status is a [`Reply`] and no error.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    pub async fn send(
        &self,
        request: Request<'_>,
        cap: ByteCap,
        total: Duration,
    ) -> Result<Reply, ClientError> {
        todo!()
    }

    /// Sends `request` and returns after the head of the answer. The caller
    /// reads the body with [`ReplyStream::chunk`].
    ///
    /// # Errors
    ///
    /// [`ClientError`] when the call gives no head.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    pub async fn open(
        &self,
        request: Request<'_>,
        limit: StreamLimit,
    ) -> Result<ReplyStream, ClientError> {
        todo!()
    }
}

/// One whole answer.
///
/// `Debug` prints the count of the bytes of the body and never a byte.
#[derive(Clone, PartialEq, Eq)]
pub struct Reply {
    /// The status.
    pub status: StatusCode,
    /// Each header.
    pub headers: HeaderMap,
    /// Each byte of the body.
    pub body: Vec<u8>,
}

impl fmt::Debug for Reply {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Reply")
            .field("status", &self.status)
            .field("headers", &self.headers)
            .field("body", &format_args!("<{} bytes>", self.body.len()))
            .finish()
    }
}

/// An answer whose body the caller reads chunk by chunk.
///
/// To drop the stream closes the connection. The server then sees that its
/// client left.
#[derive(Debug)]
pub struct ReplyStream(());

impl ReplyStream {
    /// The status of the answer.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    #[must_use]
    pub fn status(&self) -> StatusCode {
        todo!()
    }

    /// Each header of the answer.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    #[must_use]
    pub fn headers(&self) -> &HeaderMap {
        todo!()
    }

    /// The next chunk of the body. `None` at the end of the body.
    ///
    /// The chunks are not lines. The caller splits a stream of lines itself.
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again, and no byte is lost.
    ///
    /// # Errors
    ///
    /// [`ClientError`] when the stream stops before its end.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-http-client writes this body"
    )]
    pub async fn chunk(&mut self) -> Result<Option<Bytes>, ClientError> {
        todo!()
    }
}

/// The phase of a request that ran past its limit.
///
/// The set is closed.
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
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClientError {
    /// The connect failed.
    Connect {
        /// The text of the error, as `strerror` of Python gives it.
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
    /// the middle of an answer.
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
    use super::*;

    #[test]
    fn one_limit_for_each_phase_is_the_single_number_of_httpx() {
        let limit = Duration::from_secs(5);

        assert_eq!(
            Timeouts::each(limit),
            Timeouts {
                connect: limit,
                write: limit,
                read_idle: Some(limit),
            }
        );
    }

    #[test]
    fn a_stream_takes_no_read_limit_and_keeps_the_two_other_limits() {
        // `httpx.Timeout(5.0, read=None, write=10.0)` of the stream of a
        // door (door-owui/src/agent_door_owui/attendance.py:193).
        let timeouts = Timeouts {
            read_idle: None,
            write: Duration::from_secs(10),
            ..Timeouts::each(Duration::from_secs(5))
        };

        assert_eq!(timeouts.connect, Duration::from_secs(5));
        assert_eq!(timeouts.write, Duration::from_secs(10));
        assert_eq!(timeouts.read_idle, None);
    }

    #[test]
    fn the_debug_of_a_body_and_of_an_answer_prints_no_byte() {
        let body = Body::Json(br#"{"prompt":"correct horse"}"#.to_vec());
        let reply = Reply {
            status: StatusCode::OK,
            headers: HeaderMap::new(),
            body: b"correct horse".to_vec(),
        };

        assert_eq!(format!("{body:?}"), "Json(<26 bytes>)");
        assert_eq!(format!("{:?}", Body::Empty), "Empty");
        assert_eq!(
            format!("{reply:?}"),
            "Reply { status: 200, headers: {}, body: <13 bytes> }"
        );
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
}
