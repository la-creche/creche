//! A server and a client that a test scripts byte by byte.
//!
//! The client and the server of `creche-runtime` have their tests against
//! these two, so neither one uses that client or that server. Each one reads
//! and writes HTTP/1.1 with code of this module only.
//!
//! [`HttpStub`] is the server. It does these steps for each connection:
//!
//! 1. It reads one request: the head, and then a body with a
//!    `Content-Length` header or in the chunked form.
//! 2. It keeps the request as a [`Recorded`].
//! 3. It takes the next [`Answer`] of the script and gives each byte of it.
//! 4. It closes the connection.
//!
//! A request with no answer in the script gets no byte, and the connection
//! closes. The stub never makes an answer of its own. A test reads how each
//! connection ended with [`HttpStub::ends`].
//!
//! The stub takes the end of the bytes of a client as a client that left. A
//! client that closes its side after the request thus stops an answer that
//! has a pause.
//!
//! [`RawHttp`] is the client. It sends the bytes of the test and returns each
//! byte of the answer.
//!
//! Each pause of an answer is a timer of `tokio`. A test that pauses the time
//! of `tokio` moves it by hand. The sockets are real. The runtime moves a
//! paused time forward when no task has work, also while bytes are on their
//! way through the operating system.
//!
//! The two types have no Python origin. A Python test of a client gives
//! `httpx` a transport of the test, for example `httpx.MockTransport` in
//! `door-owui/tests/test_owui_transport.py:44`.

use std::collections::VecDeque;
use std::fmt;
use std::io;
use std::net::Ipv4Addr;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use http::StatusCode;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream, UnixListener, UnixStream};
use tokio::runtime::Handle;
use tokio::sync::watch;
use tokio::task::{AbortHandle, JoinSet};

use crate::root::TempRoot;

/// The bytes that end a line of a head.
const CRLF: &[u8] = b"\r\n";

/// The bytes that end the head of a request.
const HEAD_END: &[u8] = b"\r\n\r\n";

/// The most bytes of the head of a request. A longer head ends the
/// connection as [`End::BadRequest`].
const HEAD_MAX: usize = 64 * 1024;

/// The most bytes of the body of a request.
const BODY_MAX: usize = 16 * 1024 * 1024;

/// The most bytes of the line that starts a chunk: the size and its
/// extension.
const CHUNK_LINE_MAX: usize = 1024;

/// The most hexadecimal digits of the size of a chunk. Eight digits hold
/// each size up to [`BODY_MAX`].
const CHUNK_SIZE_DIGITS: usize = 8;

/// The count of bytes that one read takes from a connection.
const READ_SIZE: usize = 16 * 1024;

/// The pause after an accept that failed. The operating system can refuse an
/// accept again and again, for example with no free descriptor.
const ACCEPT_RETRY: Duration = Duration::from_millis(10);

/// The most bytes of an answer that [`RawHttp`] returns.
const ANSWER_MAX: u64 = 16 * 1024 * 1024;

/// The count of the Unix sockets that this process made. The name of a
/// socket holds it, so two stubs under one root have two sockets.
static SOCKETS: AtomicU64 = AtomicU64::new(0);

/// How one connection of an [`HttpStub`] ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum End {
    /// The stub gave each byte of the scripted answer and closed the
    /// connection.
    Answered,
    /// The client closed the connection first: before the stub read a whole
    /// request, or before the stub gave each byte of the answer.
    ClientLeft,
    /// The stub read a whole request, and the script held no answer. The
    /// stub closed the connection with no byte.
    NoScript,
    /// The bytes of the client are no HTTP/1.1 request that the stub reads.
    /// The stub closed the connection with no byte.
    BadRequest,
}

/// One request that an [`HttpStub`] read whole.
///
/// `Debug` prints the method, the target and the name of each header. It
/// prints no header value and no byte of the body: a request holds a token.
///
/// ```
/// use creche_testkit::stub::{Answer, HttpStub, RawHttp, Recorded};
/// use http::StatusCode;
///
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
/// let requests = runtime.block_on(async {
///     let stub = HttpStub::loopback().await?;
///     stub.script(Answer::status(StatusCode::NO_CONTENT).no_body());
///
///     let port = stub.port().unwrap_or_default();
///     let request = b"POST /v1/turns?wait=settled HTTP/1.1\r\nHost: stub\r\n\
///         Authorization: Bearer correct-horse\r\nContent-Length: 2\r\n\r\n{}";
///     RawHttp::tcp(port, request).await?;
///
///     Ok::<Vec<Recorded>, std::io::Error>(stub.requests())
/// })?;
/// let request = requests.first().ok_or("the stub read no request")?;
///
/// assert_eq!(request.method(), "POST");
/// assert_eq!(request.target(), "/v1/turns?wait=settled");
/// assert_eq!(request.header("authorization"), Some(b"Bearer correct-horse".as_slice()));
/// assert_eq!(request.body(), b"{}");
/// assert!(request.bytes().starts_with(b"POST /v1/turns?wait=settled HTTP/1.1\r\n"));
/// assert!(!format!("{request:?}").contains("correct-horse"));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a request. Each value holds what a
/// client sent:
///
/// ```compile_fail,E0451
/// use creche_testkit::stub::{Answer, HttpStub, RawHttp, Recorded};
/// use http::StatusCode;
///
/// let request = Recorded {
///     method: String::from("GET"),
///     target: String::from("/"),
///     headers: Vec::new(),
///     body: Vec::new(),
///     bytes: Vec::new(),
/// };
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Recorded {
    method: String,
    target: String,
    headers: Vec<(String, Vec<u8>)>,
    body: Vec<u8>,
    bytes: Vec<u8>,
}

impl Recorded {
    /// The method, as the client wrote it.
    #[must_use]
    pub fn method(&self) -> &str {
        &self.method
    }

    /// The target of the request line: the path, and the query when the
    /// request has one.
    #[must_use]
    pub fn target(&self) -> &str {
        &self.target
    }

    /// Each header in the order of the request: the name as the client wrote
    /// it, and the bytes of the value with no space and no tab at its two
    /// ends.
    #[must_use]
    pub fn headers(&self) -> &[(String, Vec<u8>)] {
        &self.headers
    }

    /// The value of the first header with the name `name`. The compare of
    /// the name ignores the case of an ASCII letter.
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&[u8]> {
        self.headers
            .iter()
            .find(|(held, _)| held.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_slice())
    }

    /// The body. For a request in the chunked form, the bytes of the chunks
    /// with no size line.
    #[must_use]
    pub fn body(&self) -> &[u8] {
        &self.body
    }

    /// Each byte of the request as the client sent it: the head, the empty
    /// line and the body in its form on the wire.
    #[must_use]
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
}

impl fmt::Debug for Recorded {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let names: Vec<&str> = self.headers.iter().map(|(name, _)| name.as_str()).collect();

        f.debug_struct("Recorded")
            .field("method", &self.method)
            .field("target", &self.target)
            .field("headers", &names)
            .finish_non_exhaustive()
    }
}

/// One part of a scripted answer.
#[derive(Clone, PartialEq, Eq)]
enum Step {
    /// The stub writes these bytes.
    Send(Vec<u8>),
    /// The stub waits for this time.
    Pause(Duration),
    /// The stub writes these bytes, waits for the time, and starts again,
    /// with no end.
    Repeat { bytes: Vec<u8>, every: Duration },
    /// The stub writes nothing and keeps the connection open.
    Hold,
}

impl fmt::Debug for Step {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Send(_) => f.write_str("Send(..)"),
            Self::Pause(time) => write!(f, "Pause({time:?})"),
            Self::Repeat { every, .. } => write!(f, "Repeat({every:?})"),
            Self::Hold => f.write_str("Hold"),
        }
    }
}

/// One scripted answer of an [`HttpStub`].
///
/// [`Answer::status`] starts an answer with a status line. The stub adds
/// the header `connection: close` to such an answer, and the header that
/// gives the form of the body. [`Answer::raw`] gives each byte of an answer
/// by hand, for an answer that no server sends.
///
/// `Debug` prints the kind of each part and no byte.
///
/// ```
/// use std::time::Duration;
///
/// use creche_testkit::stub::{Answer, HttpStub, RawHttp};
/// use http::StatusCode;
///
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
/// let answers = runtime.block_on(async {
///     let stub = HttpStub::loopback().await?;
///     let port = stub.port().unwrap_or_default();
///     let request = b"GET /healthz HTTP/1.1\r\nHost: stub\r\n\r\n";
///
///     stub.script(
///         Answer::status(StatusCode::OK)
///             .header("content-type", "application/json")
///             .body("{\"ok\":true}"),
///     );
///     stub.script(Answer::raw("HTTP/1.1 200 OK\r\ncontent-length: 9\r\n\r\nhalf"));
///     stub.script(Answer::close());
///
///     let whole = RawHttp::tcp(port, request).await?;
///     let half = RawHttp::tcp(port, request).await?;
///     let nothing = RawHttp::tcp(port, request).await?;
///
///     Ok::<_, std::io::Error>([whole, half, nothing])
/// })?;
///
/// assert_eq!(
///     answers[0],
///     b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
///         content-length: 11\r\nconnection: close\r\n\r\n{\"ok\":true}"
/// );
/// assert_eq!(answers[1], b"HTTP/1.1 200 OK\r\ncontent-length: 9\r\n\r\nhalf");
/// assert_eq!(answers[2], b"");
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module builds an answer only with the functions of
/// this type, of [`Head`] and of [`Chunked`]. It cannot build one from the
/// parts of another answer:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use creche_testkit::stub::{Answer, HttpStub, RawHttp};
/// use http::StatusCode;
///
/// let answer = Answer { ..Answer::close() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Answer {
    steps: Vec<Step>,
}

impl Answer {
    /// Starts an answer with the status line of `status`. The stub writes
    /// the line as `HTTP/1.1 200 OK`.
    #[must_use]
    pub const fn status(status: StatusCode) -> Head {
        Head {
            status,
            headers: Vec::new(),
            wait: Duration::ZERO,
        }
    }

    /// An answer of these bytes and no other byte. The stub writes them and
    /// closes the connection.
    ///
    /// Use it for an answer that a server does not send: half a head, a body
    /// that is shorter than its `Content-Length`, a line that is no status
    /// line.
    #[must_use]
    pub fn raw(bytes: impl Into<Vec<u8>>) -> Self {
        Self {
            steps: vec![Step::Send(bytes.into())],
        }
    }

    /// An answer of no byte. The stub reads the request and closes the
    /// connection.
    #[must_use]
    pub const fn close() -> Self {
        Self { steps: Vec::new() }
    }

    /// An answer that never starts. The stub reads the request, writes no
    /// byte and keeps the connection open until the client leaves or the
    /// stub drops.
    #[must_use]
    pub fn hold() -> Self {
        Self {
            steps: vec![Step::Hold],
        }
    }
}

/// The head of an [`Answer`]: the status line and the headers.
///
/// ```
/// use std::time::Duration;
///
/// use creche_testkit::stub::{Answer, Head};
/// use http::StatusCode;
///
/// let head: Head = Answer::status(StatusCode::ACCEPTED)
///     .header("x-turn", "01JABCDEFGHJKMNPQRSTVWXYZ0")
///     .after(Duration::from_millis(20));
/// let answer: Answer = head.clone().body("{}");
///
/// assert_ne!(answer, head.no_body());
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module starts a head only with [`Answer::status`]:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use creche_testkit::stub::{Answer, Head};
/// use http::StatusCode;
///
/// let head = Head {
///     status: StatusCode::ACCEPTED,
///     headers: Vec::new(),
///     wait: Duration::ZERO,
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Head {
    status: StatusCode,
    headers: Vec<(String, String)>,
    /// The time that the stub waits before the first byte of the head.
    wait: Duration,
}

impl Head {
    /// The head with one more header. The stub writes the name and the value
    /// as they are.
    #[must_use]
    pub fn header(mut self, name: &str, value: &str) -> Self {
        self.headers.push((name.to_owned(), value.to_owned()));

        self
    }

    /// The head that the stub sends late: it waits for `pause` after it read
    /// the request, and then writes the first byte.
    #[must_use]
    pub const fn after(mut self, pause: Duration) -> Self {
        self.wait = pause;

        self
    }

    /// The answer with this head and the body `bytes`. The stub adds the
    /// headers `content-length` and `connection: close`.
    #[must_use]
    pub fn body(self, bytes: impl Into<Vec<u8>>) -> Answer {
        let body = bytes.into();
        let mut head = self.bytes(&[("content-length", &body.len().to_string())]);
        head.extend_from_slice(&body);

        Answer {
            steps: self.late(head),
        }
    }

    /// The answer with this head and no body. The stub adds the header
    /// `connection: close` and no `content-length`. Use it for the answer to
    /// a `HEAD` request and for a status with no body, for example 204.
    #[must_use]
    pub fn no_body(self) -> Answer {
        let head = self.bytes(&[]);

        Answer {
            steps: self.late(head),
        }
    }

    /// Starts a body in the chunked form. The stub adds the headers
    /// `transfer-encoding: chunked` and `connection: close`, and it writes
    /// the head before the first chunk.
    #[must_use]
    pub fn chunked(self) -> Chunked {
        let head = self.bytes(&[("transfer-encoding", "chunked")]);

        Chunked {
            steps: self.late(head),
        }
    }

    /// The bytes of the head: the status line, each header of the test, the
    /// headers of `form`, `connection: close` and the empty line.
    fn bytes(&self, form: &[(&str, &str)]) -> Vec<u8> {
        let reason = self.status.canonical_reason().unwrap_or("Status");
        let mut head = format!("HTTP/1.1 {} {reason}\r\n", self.status.as_u16());
        let scripted = self
            .headers
            .iter()
            .map(|(name, value)| (name.as_str(), value.as_str()));

        for (name, value) in scripted
            .chain(form.iter().copied())
            .chain([("connection", "close")])
        {
            head.push_str(name);
            head.push_str(": ");
            head.push_str(value);
            head.push_str("\r\n");
        }
        head.push_str("\r\n");

        head.into_bytes()
    }

    /// The first steps of an answer: the pause of [`Head::after`], and then
    /// `first`.
    fn late(&self, first: Vec<u8>) -> Vec<Step> {
        if self.wait.is_zero() {
            return vec![Step::Send(first)];
        }

        vec![Step::Pause(self.wait), Step::Send(first)]
    }
}

/// The body of an [`Answer`] in the chunked form, chunk by chunk.
///
/// ```
/// use std::time::Duration;
///
/// use creche_testkit::stub::{Answer, Chunked};
/// use http::StatusCode;
///
/// let stream: Chunked = Answer::status(StatusCode::OK)
///     .header("content-type", "text/event-stream")
///     .chunked()
///     .chunk("data: one\n\n")
///     .pause(Duration::from_millis(20))
///     .chunk("data: two\n\n");
/// let whole: Answer = stream.clone().end();
/// let cut: Answer = stream.clone().cut();
/// let endless: Answer = stream.forever(": keep-alive\n\n", Duration::from_secs(15));
///
/// assert_ne!(whole, cut);
/// assert_ne!(cut, endless);
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module starts a chunked body only with
/// [`Head::chunked`], so its head names the chunked form. It cannot build a
/// body from the parts of another body:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use creche_testkit::stub::{Answer, Chunked};
/// use http::StatusCode;
///
/// let stream = Chunked {
///     ..Answer::status(StatusCode::OK).chunked()
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chunked {
    steps: Vec<Step>,
}

impl Chunked {
    /// The body with one more chunk. The stub writes the size line, the
    /// bytes and the line end in one write.
    ///
    /// A chunk of no byte is the end of a chunked body. The function adds
    /// nothing for it. Use [`Chunked::end`].
    #[must_use]
    pub fn chunk(mut self, bytes: impl Into<Vec<u8>>) -> Self {
        let bytes = bytes.into();
        if !bytes.is_empty() {
            self.steps.push(Step::Send(chunk_bytes(&bytes)));
        }

        self
    }

    /// The body with a pause at this place: the stub waits for `time` before
    /// its next write.
    #[must_use]
    pub fn pause(mut self, time: Duration) -> Self {
        self.steps.push(Step::Pause(time));

        self
    }

    /// The answer whose body ends here. The stub writes the last chunk, of
    /// size zero, and closes the connection.
    #[must_use]
    pub fn end(mut self) -> Answer {
        self.steps.push(Step::Send(b"0\r\n\r\n".to_vec()));

        Answer { steps: self.steps }
    }

    /// The answer that the stub cuts here. The stub writes no last chunk and
    /// closes the connection, as a server does that stops in the middle of a
    /// stream.
    #[must_use]
    pub fn cut(self) -> Answer {
        Answer { steps: self.steps }
    }

    /// The answer whose body has no end. After the chunks before, the stub
    /// writes `bytes` as one chunk, waits for `every`, and starts again. It
    /// stops when the client leaves or the stub drops.
    ///
    /// For `bytes` of no byte, the stub writes nothing and keeps the
    /// connection open.
    #[must_use]
    pub fn forever(mut self, bytes: impl Into<Vec<u8>>, every: Duration) -> Answer {
        let bytes = bytes.into();
        self.steps.push(if bytes.is_empty() {
            Step::Hold
        } else {
            Step::Repeat {
                bytes: chunk_bytes(&bytes),
                every,
            }
        });

        Answer { steps: self.steps }
    }
}

/// One chunk on the wire: the size in hexadecimal, a line end, the bytes
/// and a line end.
fn chunk_bytes(bytes: &[u8]) -> Vec<u8> {
    let mut chunk = format!("{:x}\r\n", bytes.len()).into_bytes();
    chunk.extend_from_slice(bytes);
    chunk.extend_from_slice(CRLF);

    chunk
}

/// What a stub holds for its test.
#[derive(Debug, Default)]
struct State {
    /// The answers that no request took yet, in order.
    script: VecDeque<Answer>,
    /// Each request that the stub read whole, in the order of the reads.
    requests: Vec<Recorded>,
    /// How each connection ended, in the order of the ends.
    ends: Vec<End>,
    /// True after [`HttpStub::stop_accepting`].
    stopped: bool,
}

/// Stops the accept task of a stub when the stub drops. That task holds the
/// listener and the task of each connection.
struct Abort(AbortHandle);

impl Drop for Abort {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// A server that gives the answers of a script and keeps each request that
/// it got.
///
/// It listens on a loopback port that the operating system selects, or on a
/// Unix socket under a `TempRoot`. It never binds each interface.
///
/// The stub reads one request from each connection, gives one answer and
/// closes the connection. The first request that it reads whole takes the
/// first answer of the script. The stub stops when the value drops: it
/// closes the listener and each open connection.
///
/// Do not connect to the port of a stub that dropped. The operating system
/// can give that port to the stub of another test. For a connect that the
/// operating system refuses, drop a stub on a Unix socket and connect to
/// its path: the file stays, and no listener holds it.
///
/// The type has no Python origin.
///
/// ```
/// use creche_testkit::root::TempRoot;
/// use creche_testkit::stub::{Answer, End, HttpStub, RawHttp};
/// use http::StatusCode;
///
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
/// runtime.block_on(async {
///     let root = TempRoot::new()?;
///     let stub = HttpStub::unix(&root).await?;
///     stub.script(Answer::status(StatusCode::OK).body("ok"));
///
///     let socket = stub.socket().unwrap_or(root.path());
///     let request = b"GET /healthz HTTP/1.1\r\nHost: stub\r\n\r\n";
///     let first = RawHttp::unix(socket, request).await?;
///     let second = RawHttp::unix(socket, request).await?;
///
///     assert!(first.ends_with(b"\r\n\r\nok"));
///     assert_eq!(second, b"");
///     assert_eq!(stub.wait_ends(2).await, [End::Answered, End::NoScript]);
///     assert_eq!(stub.requests().len(), 2);
///
///     Ok::<(), std::io::Error>(())
/// })?;
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module cannot build a stub or change its address:
///
/// ```compile_fail,E0451
/// use creche_testkit::root::TempRoot;
/// use creche_testkit::stub::{Answer, End, HttpStub, RawHttp};
/// use http::StatusCode;
///
/// fn moved(stub: HttpStub) -> HttpStub {
///     HttpStub {
///         port: Some(80),
///         ..stub
///     }
/// }
/// ```
pub struct HttpStub {
    /// The loopback port, for a stub on TCP.
    port: Option<u16>,
    /// The path of the socket, for a stub on a Unix socket.
    socket: Option<PathBuf>,
    state: Arc<watch::Sender<State>>,
    /// Stops the accept task when the stub drops. No code reads the field.
    _accept: Abort,
}

impl HttpStub {
    /// A stub on a free port of the loopback address `127.0.0.1`.
    ///
    /// Call it inside a runtime of `tokio` with the I/O driver. The stub
    /// runs as a task of that runtime.
    ///
    /// # Errors
    ///
    /// The error of the operating system when it refuses the bind, and an
    /// error for a call outside a runtime.
    pub async fn loopback() -> io::Result<Self> {
        let runtime = Handle::try_current().map_err(io::Error::other)?;
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await?;
        let port = listener.local_addr()?.port();

        Ok(Self::start(
            &runtime,
            Listener::Tcp(listener),
            Some(port),
            None,
        ))
    }

    /// A stub on a Unix socket under `root`. Two stubs under one root have
    /// two sockets.
    ///
    /// Call it inside a runtime of `tokio` with the I/O driver. The stub
    /// runs as a task of that runtime.
    ///
    /// # Errors
    ///
    /// The error of the operating system when it refuses the bind, and an
    /// error for a call outside a runtime.
    pub async fn unix(root: &TempRoot) -> io::Result<Self> {
        let runtime = Handle::try_current().map_err(io::Error::other)?;
        let count = SOCKETS.fetch_add(1, Ordering::Relaxed);
        let socket = root.path().join(format!("stub-{count}.sock"));
        let listener = UnixListener::bind(&socket)?;

        Ok(Self::start(
            &runtime,
            Listener::Unix(listener),
            None,
            Some(socket),
        ))
    }

    /// Starts the accept task of a stub on `listener`.
    fn start(
        runtime: &Handle,
        listener: Listener,
        port: Option<u16>,
        socket: Option<PathBuf>,
    ) -> Self {
        let state = Arc::new(watch::Sender::new(State::default()));
        let accept = runtime.spawn(accept_each(listener, Arc::clone(&state)));

        Self {
            port,
            socket,
            state,
            _accept: Abort(accept.abort_handle()),
        }
    }

    /// The loopback port of the stub. `None` for a stub on a Unix socket.
    #[must_use]
    pub const fn port(&self) -> Option<u16> {
        self.port
    }

    /// The path of the socket of the stub. `None` for a stub on a loopback
    /// port.
    #[must_use]
    pub fn socket(&self) -> Option<&Path> {
        self.socket.as_deref()
    }

    /// Adds `answer` to the end of the script. Each request that the stub
    /// reads whole takes the first answer that is left.
    pub fn script(&self, answer: Answer) {
        self.state
            .send_modify(|state| state.script.push_back(answer));
    }

    /// Makes the stub a listener that never accepts. The call has no way
    /// back.
    ///
    /// The operating system still completes the connect of a client, up to
    /// the backlog of the listener. No code reads what that client writes,
    /// and no code answers. A connection that the stub accepted before the
    /// call continues to its end.
    pub fn stop_accepting(&self) {
        self.state.send_modify(|state| state.stopped = true);
    }

    /// Each request that the stub read whole, in the order of the reads.
    #[must_use]
    pub fn requests(&self) -> Vec<Recorded> {
        self.state.borrow().requests.clone()
    }

    /// How each connection ended, in the order of the ends. A connection
    /// that is open has no entry.
    ///
    /// The stub adds the entry before it closes the connection. A client
    /// that read the end of an answer thus finds the entry.
    #[must_use]
    pub fn ends(&self) -> Vec<End> {
        self.state.borrow().ends.clone()
    }

    /// Waits until the stub read `count` requests whole, and returns each
    /// request.
    ///
    /// The function has no time limit. A test that can wait with no end
    /// gives it one, with `tokio::time::timeout`.
    pub async fn wait_requests(&self, count: usize) -> Vec<Recorded> {
        let mut changes = self.state.subscribe();

        match changes
            .wait_for(|state| state.requests.len() >= count)
            .await
        {
            Ok(state) => state.requests.clone(),
            // The stub holds the sender, so the channel is open.
            Err(_) => self.requests(),
        }
    }

    /// Waits until `count` connections ended, and returns how each one
    /// ended.
    ///
    /// The function has no time limit. A test that can wait with no end
    /// gives it one, with `tokio::time::timeout`.
    pub async fn wait_ends(&self, count: usize) -> Vec<End> {
        let mut changes = self.state.subscribe();

        match changes.wait_for(|state| state.ends.len() >= count).await {
            Ok(state) => state.ends.clone(),
            // The stub holds the sender, so the channel is open.
            Err(_) => self.ends(),
        }
    }
}

impl fmt::Debug for HttpStub {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HttpStub")
            .field("port", &self.port)
            .field("socket", &self.socket)
            .finish_non_exhaustive()
    }
}

/// The listener of a stub.
enum Listener {
    Tcp(TcpListener),
    Unix(UnixListener),
}

/// One connection of a stub.
enum Stream {
    Tcp(TcpStream),
    Unix(UnixStream),
}

impl Listener {
    /// Takes the next connection.
    async fn accept(&self) -> io::Result<Stream> {
        match self {
            Self::Tcp(listener) => {
                let (stream, _) = listener.accept().await?;
                // A chunk then goes out at once and does not wait for the
                // next one. A stream with no such option still works.
                let _ = stream.set_nodelay(true);

                Ok(Stream::Tcp(stream))
            }
            Self::Unix(listener) => {
                let (stream, _) = listener.accept().await?;

                Ok(Stream::Unix(stream))
            }
        }
    }
}

/// The accept task of a stub: one task for each connection, until
/// [`HttpStub::stop_accepting`].
///
/// The runtime drops this future when the stub drops. The listener and the
/// task of each connection then go with it.
async fn accept_each(listener: Listener, state: Arc<watch::Sender<State>>) {
    let mut changes = state.subscribe();
    let mut connections = JoinSet::new();
    let mut not_served = Vec::new();

    loop {
        if changes.borrow_and_update().stopped {
            // The task keeps the listener, so the operating system still
            // takes the connect of a client.
            return std::future::pending().await;
        }

        let accepted = tokio::select! {
            biased;
            changed = changes.changed() => {
                if changed.is_err() {
                    return;
                }
                continue;
            }
            Some(_) = connections.join_next() => continue,
            accepted = listener.accept() => accepted,
        };

        match accepted {
            // In a runtime with more than one thread, the test can stop the
            // accepts while this task takes a connection. The task keeps
            // that connection open and reads nothing, as the operating
            // system does for a connection that no code accepts.
            Ok(stream) if state.borrow().stopped => not_served.push(stream),
            Ok(Stream::Tcp(stream)) => {
                let _ = connections.spawn(converse(stream, Arc::clone(&state)));
            }
            Ok(Stream::Unix(stream)) => {
                let _ = connections.spawn(converse(stream, Arc::clone(&state)));
            }
            Err(_) => tokio::time::sleep(ACCEPT_RETRY).await,
        }
    }
}

/// The task of one connection: one request, one answer, then the close.
async fn converse<S>(stream: S, state: Arc<watch::Sender<State>>)
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let (mut reader, mut writer) = tokio::io::split(stream);
    let end = exchange(&mut reader, &mut writer, &state).await;

    state.send_modify(|state| state.ends.push(end));
    // The close of a connection that the client already left has an error
    // and nothing to do.
    let _ = writer.shutdown().await;
}

/// Reads one request, keeps it and gives the next answer of the script.
async fn exchange<R, W>(reader: &mut R, writer: &mut W, state: &watch::Sender<State>) -> End
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let request = match read_request(reader).await {
        Ok(request) => request,
        Err(end) => return end,
    };
    let mut answer = None;
    state.send_modify(|state| {
        state.requests.push(request);
        answer = state.script.pop_front();
    });

    match answer {
        Some(answer) => give(&answer, reader, writer).await,
        None => End::NoScript,
    }
}

/// Reads the bytes of a connection until they hold one whole request.
async fn read_request<R>(reader: &mut R) -> Result<Recorded, End>
where
    R: AsyncRead + Unpin,
{
    let mut bytes = Vec::new();
    let mut chunk = vec![0_u8; READ_SIZE];

    loop {
        match request_of(&bytes) {
            Parsed::Whole(request) => return Ok(request),
            Parsed::Bad => return Err(End::BadRequest),
            Parsed::Partial => {}
        }

        match reader.read(&mut chunk).await {
            Ok(0) | Err(_) => return Err(End::ClientLeft),
            // A read gives no more bytes than the buffer holds.
            Ok(count) => bytes.extend_from_slice(chunk.get(..count).unwrap_or(&chunk)),
        }
    }
}

/// Gives each step of `answer` in order. The function stops when the client
/// leaves.
async fn give<R, W>(answer: &Answer, reader: &mut R, writer: &mut W) -> End
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let gone = client_gone(reader);
    tokio::pin!(gone);

    for step in &answer.steps {
        let done = tokio::select! {
            biased;
            done = run(step, writer) => done,
            () = &mut gone => return End::ClientLeft,
        };
        if done.is_err() {
            return End::ClientLeft;
        }
    }

    End::Answered
}

/// Does one step of an answer.
async fn run<W>(step: &Step, writer: &mut W) -> io::Result<()>
where
    W: AsyncWrite + Unpin,
{
    match step {
        Step::Send(bytes) => {
            writer.write_all(bytes).await?;
            writer.flush().await
        }
        Step::Pause(time) => {
            tokio::time::sleep(*time).await;

            Ok(())
        }
        Step::Repeat { bytes, every } => loop {
            writer.write_all(bytes).await?;
            writer.flush().await?;
            tokio::time::sleep(*every).await;
        },
        Step::Hold => std::future::pending().await,
    }
}

/// Ends when the client closed its side of the connection, or when a read
/// fails. The function drops each byte that the client sends after its
/// request.
async fn client_gone<R>(reader: &mut R)
where
    R: AsyncRead + Unpin,
{
    let mut scratch = [0_u8; 1024];

    loop {
        match reader.read(&mut scratch).await {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
    }
}

/// What the bytes of a client hold so far.
#[derive(Debug, PartialEq, Eq)]
enum Parsed<T> {
    /// A whole value.
    Whole(T),
    /// The start of a value. More bytes can make it whole.
    Partial,
    /// No value. More bytes cannot change that.
    Bad,
}

/// How the head of a request gives the size of its body.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Framing {
    /// The request has no body.
    NoBody,
    /// A `Content-Length` header gives the count of the bytes.
    Length(usize),
    /// A `Transfer-Encoding` header names the chunked form.
    Chunked,
}

/// The head of a request.
#[derive(Debug, PartialEq, Eq)]
struct RequestHead {
    method: String,
    target: String,
    headers: Vec<(String, Vec<u8>)>,
    /// The count of the bytes of the head, with its empty line.
    len: usize,
    framing: Framing,
}

/// The request that `bytes` starts with.
fn request_of(bytes: &[u8]) -> Parsed<Recorded> {
    let head = match head_of(bytes) {
        Parsed::Whole(head) => head,
        Parsed::Partial => return Parsed::Partial,
        Parsed::Bad => return Parsed::Bad,
    };
    let Some(rest) = bytes.get(head.len..) else {
        return Parsed::Bad;
    };
    let (body, used) = match body_of(head.framing, rest) {
        Parsed::Whole(body) => body,
        Parsed::Partial => return Parsed::Partial,
        Parsed::Bad => return Parsed::Bad,
    };
    let Some(whole) = bytes.get(..head.len.saturating_add(used)) else {
        return Parsed::Bad;
    };

    Parsed::Whole(Recorded {
        method: head.method,
        target: head.target,
        headers: head.headers,
        body,
        bytes: whole.to_vec(),
    })
}

/// The head that `bytes` starts with.
fn head_of(bytes: &[u8]) -> Parsed<RequestHead> {
    let Some(end) = find(bytes, HEAD_END) else {
        return if bytes.len() > HEAD_MAX {
            Parsed::Bad
        } else {
            Parsed::Partial
        };
    };
    let Some(head) = bytes.get(..end).filter(|head| head.len() <= HEAD_MAX) else {
        return Parsed::Bad;
    };

    let mut lines = head
        .split(|byte| *byte == b'\n')
        .map(|line| line.strip_suffix(b"\r").unwrap_or(line));
    let Some((method, target)) = lines.next().and_then(request_line) else {
        return Parsed::Bad;
    };
    let Some(headers) = lines.map(header_line).collect::<Option<Vec<_>>>() else {
        return Parsed::Bad;
    };
    let Some(framing) = framing_of(&headers) else {
        return Parsed::Bad;
    };

    Parsed::Whole(RequestHead {
        method,
        target,
        headers,
        len: end.saturating_add(HEAD_END.len()),
        framing,
    })
}

/// The method and the target of a request line: three words with one space
/// between two words, and the last word names HTTP/1.
fn request_line(line: &[u8]) -> Option<(String, String)> {
    let mut words = line.split(|byte| *byte == b' ');
    let (Some(method), Some(target), Some(version), None) =
        (words.next(), words.next(), words.next(), words.next())
    else {
        return None;
    };
    if method.is_empty() || target.is_empty() || !version.starts_with(b"HTTP/1.") {
        return None;
    }

    Some((
        String::from_utf8(method.to_vec()).ok()?,
        String::from_utf8(target.to_vec()).ok()?,
    ))
}

/// The name and the value of a header line. The value has no space and no
/// tab at its two ends.
fn header_line(line: &[u8]) -> Option<(String, Vec<u8>)> {
    let colon = line.iter().position(|byte| *byte == b':')?;
    let (name, rest) = line.split_at_checked(colon)?;
    let value = trim_blanks(rest.get(1..)?);
    if name.is_empty() || name.iter().any(|byte| is_blank(*byte)) {
        return None;
    }

    Some((String::from_utf8(name.to_vec()).ok()?, value.to_vec()))
}

/// Whether `byte` is a space or a tab.
const fn is_blank(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t')
}

/// `bytes` with no space and no tab at its two ends.
fn trim_blanks(mut bytes: &[u8]) -> &[u8] {
    while let [first, rest @ ..] = bytes
        && is_blank(*first)
    {
        bytes = rest;
    }
    while let [rest @ .., last] = bytes
        && is_blank(*last)
    {
        bytes = rest;
    }

    bytes
}

/// How the headers give the size of the body. `None` for a
/// `Content-Length` that is no count of at most [`BODY_MAX`] bytes.
fn framing_of(headers: &[(String, Vec<u8>)]) -> Option<Framing> {
    let values = |name: &'static str| {
        headers
            .iter()
            .filter(move |(held, _)| held.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_slice())
    };

    if values("transfer-encoding").any(|value| {
        value
            .to_ascii_lowercase()
            .windows(b"chunked".len())
            .any(|window| window == b"chunked")
    }) {
        return Some(Framing::Chunked);
    }

    match values("content-length").next() {
        None => Some(Framing::NoBody),
        Some(value) => {
            let text = std::str::from_utf8(value).ok()?;
            if text.is_empty() || !text.bytes().all(|byte| byte.is_ascii_digit()) {
                return None;
            }

            text.parse()
                .ok()
                .filter(|length| *length <= BODY_MAX)
                .map(Framing::Length)
        }
    }
}

/// The body that `rest` starts with, and the count of the bytes that it
/// takes on the wire.
fn body_of(framing: Framing, rest: &[u8]) -> Parsed<(Vec<u8>, usize)> {
    match framing {
        Framing::NoBody => Parsed::Whole((Vec::new(), 0)),
        Framing::Length(length) => match rest.get(..length) {
            Some(body) => Parsed::Whole((body.to_vec(), length)),
            None => Parsed::Partial,
        },
        Framing::Chunked => chunked_body(rest),
    }
}

/// The body in the chunked form that `rest` starts with: each chunk, the
/// last chunk of size zero, and each trailer line up to the empty line.
fn chunked_body(rest: &[u8]) -> Parsed<(Vec<u8>, usize)> {
    let mut body = Vec::new();
    let mut at = 0_usize;

    loop {
        let (size, data_at) = match line_at(rest, at) {
            Parsed::Whole((line, next)) => match chunk_size(line) {
                Some(size) => (size, next),
                None => return Parsed::Bad,
            },
            Parsed::Partial => return Parsed::Partial,
            Parsed::Bad => return Parsed::Bad,
        };

        if size == 0 {
            return trailers(rest, data_at).map_whole(|end| (body, end));
        }
        if body.len().saturating_add(size) > BODY_MAX {
            return Parsed::Bad;
        }

        let data_end = data_at.saturating_add(size);
        let (Some(data), Some(line_end)) = (
            rest.get(data_at..data_end),
            rest.get(data_end..data_end.saturating_add(CRLF.len())),
        ) else {
            return Parsed::Partial;
        };
        if line_end != CRLF {
            return Parsed::Bad;
        }

        body.extend_from_slice(data);
        at = data_end.saturating_add(CRLF.len());
    }
}

impl<T> Parsed<T> {
    /// The same answer, with the whole value changed by `change`.
    fn map_whole<U>(self, change: impl FnOnce(T) -> U) -> Parsed<U> {
        match self {
            Self::Whole(value) => Parsed::Whole(change(value)),
            Self::Partial => Parsed::Partial,
            Self::Bad => Parsed::Bad,
        }
    }
}

/// The line of `rest` that starts at `at`, with no line end, and the place
/// of the byte after its line end.
fn line_at(rest: &[u8], at: usize) -> Parsed<(&[u8], usize)> {
    let Some(tail) = rest.get(at..) else {
        return Parsed::Partial;
    };
    let Some(length) = find(tail, CRLF) else {
        return if tail.len() > CHUNK_LINE_MAX {
            Parsed::Bad
        } else {
            Parsed::Partial
        };
    };

    match tail.get(..length) {
        Some(line) if length <= CHUNK_LINE_MAX => {
            Parsed::Whole((line, at.saturating_add(length).saturating_add(CRLF.len())))
        }
        _ => Parsed::Bad,
    }
}

/// The size of a chunk from its first line: hexadecimal digits, and then an
/// extension that the stub does not read.
fn chunk_size(line: &[u8]) -> Option<usize> {
    let digits = trim_blanks(line.split(|byte| *byte == b';').next()?);
    if digits.is_empty()
        || digits.len() > CHUNK_SIZE_DIGITS
        || !digits.iter().all(u8::is_ascii_hexdigit)
    {
        return None;
    }

    usize::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok()
}

/// The place of the byte after the trailer lines that start at `at`: each
/// line up to the first empty line.
fn trailers(rest: &[u8], mut at: usize) -> Parsed<usize> {
    loop {
        match line_at(rest, at) {
            Parsed::Whole(([], next)) => return Parsed::Whole(next),
            Parsed::Whole((_, next)) => at = next,
            Parsed::Partial => return Parsed::Partial,
            Parsed::Bad => return Parsed::Bad,
        }
    }
}

/// The place of the first `needle` in `bytes`.
fn find(bytes: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() {
        return Some(0);
    }

    bytes
        .windows(needle.len())
        .position(|window| window == needle)
}

/// What [`RawHttp`] does after it wrote the request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AfterWrite {
    /// It keeps its side of the connection open until the answer ends.
    KeepOpen,
    /// It closes its side of the connection, so the server reads the end of
    /// the bytes.
    CloseWrite,
}

/// A client that sends raw bytes and returns the raw answer.
///
/// A test of a server uses it to send a request that no HTTP client sends:
/// half a head, a wrong length, a path with a final slash.
///
/// Each function works in this way:
///
/// 1. It writes the request and reads the answer at the same time. A server
///    that answers before it read the whole request thus blocks nothing.
/// 2. The answer ends when the server closes the connection. An error of
///    the connection after the first byte of the answer also ends it.
/// 3. An answer of more than 16 MiB is an error.
///
/// The functions have no time limit. A server that never closes the
/// connection makes a function wait with no end. A test that can wait with
/// no end gives it a limit, with `tokio::time::timeout`.
///
/// A server of HTTP/1.1 keeps a connection open after its answer, for the
/// next request. Send the header `Connection: close` in a whole request.
/// The server then closes the connection after the answer.
///
/// The type has no Python origin.
#[derive(Debug, Clone, Copy)]
pub struct RawHttp;

impl RawHttp {
    /// Sends `request` to the loopback port `port` and returns each byte of
    /// the answer, until the server closes the connection.
    ///
    /// The function keeps its side of the connection open while it reads.
    ///
    /// # Errors
    ///
    /// The error of the operating system for the connect, the write or the
    /// read. An error after the first byte of the answer is no error: the
    /// function returns the bytes that it has.
    pub async fn tcp(port: u16, request: &[u8]) -> io::Result<Vec<u8>> {
        let stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port)).await?;

        send(stream, request, AfterWrite::KeepOpen, ANSWER_MAX).await
    }

    /// Sends `request` to the Unix socket `socket` and returns each byte of
    /// the answer, until the server closes the connection.
    ///
    /// The function keeps its side of the connection open while it reads.
    ///
    /// # Errors
    ///
    /// The error of the operating system for the connect, the write or the
    /// read. An error after the first byte of the answer is no error: the
    /// function returns the bytes that it has.
    pub async fn unix(socket: &Path, request: &[u8]) -> io::Result<Vec<u8>> {
        let stream = UnixStream::connect(socket).await?;

        send(stream, request, AfterWrite::KeepOpen, ANSWER_MAX).await
    }

    /// Sends `request` to the loopback port `port`, closes its side of the
    /// connection, and returns each byte of the answer.
    ///
    /// The server reads the end of the bytes after the request. Use it for
    /// a request that stops in the middle: a server that waits for the rest
    /// then sees that no more byte comes.
    ///
    /// Do not use it for a whole request. A server can take the end of the
    /// bytes as a client that left, and then it gives no answer.
    ///
    /// # Errors
    ///
    /// The errors of [`RawHttp::tcp`].
    pub async fn tcp_then_eof(port: u16, request: &[u8]) -> io::Result<Vec<u8>> {
        let stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port)).await?;

        send(stream, request, AfterWrite::CloseWrite, ANSWER_MAX).await
    }

    /// Sends `request` to the Unix socket `socket`, closes its side of the
    /// connection, and returns each byte of the answer.
    ///
    /// The server reads the end of the bytes after the request. Use it for
    /// a request that stops in the middle, and not for a whole request, as
    /// [`RawHttp::tcp_then_eof`] says.
    ///
    /// # Errors
    ///
    /// The errors of [`RawHttp::unix`].
    pub async fn unix_then_eof(socket: &Path, request: &[u8]) -> io::Result<Vec<u8>> {
        let stream = UnixStream::connect(socket).await?;

        send(stream, request, AfterWrite::CloseWrite, ANSWER_MAX).await
    }
}

/// Writes `request` to `stream` and reads the answer, up to `cap` bytes.
async fn send<S>(stream: S, request: &[u8], after: AfterWrite, cap: u64) -> io::Result<Vec<u8>>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let (reader, mut writer) = tokio::io::split(stream);
    let write = async {
        writer.write_all(request).await?;
        writer.flush().await?;
        if after == AfterWrite::CloseWrite {
            writer.shutdown().await?;
        }

        Ok::<(), io::Error>(())
    };
    let read = async {
        let mut answer = Vec::new();
        // One byte past the cap shows an answer that is too long.
        let read = reader
            .take(cap.saturating_add(1))
            .read_to_end(&mut answer)
            .await;

        (answer, read)
    };
    let (wrote, (answer, read)) = tokio::join!(write, read);

    if u64::try_from(answer.len()).is_ok_and(|length| length > cap) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("the answer has more than {cap} bytes"),
        ));
    }
    if !answer.is_empty() {
        return Ok(answer);
    }

    read?;
    wrote?;

    Ok(answer)
}

#[cfg(test)]
mod tests {
    use std::future::Future;

    use tokio::time::{Instant, timeout};

    use super::*;

    /// A request with no body.
    const GET: &[u8] = b"GET /healthz HTTP/1.1\r\nHost: stub\r\n\r\n";

    /// The time that a test gives to a step that must not end.
    const SHORT: Duration = Duration::from_millis(150);

    /// The time that a test gives to a step that must end. The machine can
    /// be under load, so the limit is long.
    const LONG: Duration = Duration::from_secs(60);

    fn block_on<F: Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(future)
    }

    /// Waits for `future`, and fails the test when it does not end.
    async fn soon<F: Future>(future: F) -> F::Output {
        timeout(LONG, future).await.expect("the step did not end")
    }

    fn ok(body: &str) -> Answer {
        Answer::status(StatusCode::OK).body(body)
    }

    #[test]
    fn a_stub_on_a_loopback_port_gives_the_scripted_answer() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(
                Answer::status(StatusCode::OK)
                    .header("content-type", "application/json")
                    .body("{\"ok\":true}"),
            );
            let port = stub.port().unwrap();
            let answer = soon(RawHttp::tcp(port, GET)).await.unwrap();

            assert_ne!(port, 0);
            assert_eq!(stub.socket(), None);
            assert_eq!(
                String::from_utf8(answer).unwrap(),
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
                 content-length: 11\r\nconnection: close\r\n\r\n{\"ok\":true}"
            );
            assert_eq!(stub.ends(), [End::Answered]);
        });
    }

    #[test]
    fn a_stub_on_a_unix_socket_gives_the_scripted_answer() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let stub = HttpStub::unix(&root).await.unwrap();
            stub.script(ok("ok"));
            let socket = stub.socket().unwrap().to_owned();
            let answer = soon(RawHttp::unix(&socket, GET)).await.unwrap();

            assert_eq!(stub.port(), None);
            assert_eq!(socket.parent().unwrap(), root.path());
            assert!(answer.ends_with(b"\r\n\r\nok"), "{answer:?}");
            assert_eq!(stub.ends(), [End::Answered]);
            assert_eq!(stub.requests().len(), 1);
        });
    }

    #[test]
    fn two_stubs_under_one_root_have_two_sockets_that_fit_a_socket_path() {
        // The longest path of a Unix socket on macOS, in bytes.
        const SOCKET_PATH_MAX: usize = 104;

        block_on(async {
            let root = TempRoot::new().unwrap();
            let first = HttpStub::unix(&root).await.unwrap();
            let second = HttpStub::unix(&root).await.unwrap();
            first.script(ok("first"));
            second.script(ok("second"));

            let from_first = soon(RawHttp::unix(first.socket().unwrap(), GET)).await;
            let from_second = soon(RawHttp::unix(second.socket().unwrap(), GET)).await;

            assert_ne!(first.socket(), second.socket());
            assert!(from_first.unwrap().ends_with(b"first"));
            assert!(from_second.unwrap().ends_with(b"second"));
            assert!(first.socket().unwrap().as_os_str().len() < SOCKET_PATH_MAX);
        });
    }

    #[test]
    fn the_stub_keeps_each_part_of_a_request() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(Answer::status(StatusCode::NO_CONTENT).no_body());
            let request = b"POST /v1/turns?wait=settled&x=%20 HTTP/1.1\r\nHost: stub\r\n\
                Authorization:   Bearer correct-horse \t\r\nX-Empty:\r\nx-twice: 1\r\n\
                X-Twice: 2\r\nContent-Length: 9\r\n\r\n{\"n\": 1}\n";
            let answer = soon(RawHttp::tcp(stub.port().unwrap(), request)).await;
            let requests = stub.requests();
            let [recorded] = requests.as_slice() else {
                panic!("the stub read {} requests", requests.len());
            };

            assert_eq!(
                answer.unwrap(),
                b"HTTP/1.1 204 No Content\r\nconnection: close\r\n\r\n"
            );
            assert_eq!(recorded.method(), "POST");
            assert_eq!(recorded.target(), "/v1/turns?wait=settled&x=%20");
            assert_eq!(
                recorded.headers(),
                [
                    (String::from("Host"), b"stub".to_vec()),
                    (
                        String::from("Authorization"),
                        b"Bearer correct-horse".to_vec()
                    ),
                    (String::from("X-Empty"), Vec::new()),
                    (String::from("x-twice"), b"1".to_vec()),
                    (String::from("X-Twice"), b"2".to_vec()),
                    (String::from("Content-Length"), b"9".to_vec()),
                ]
            );
            assert_eq!(
                recorded.header("authorization"),
                Some(b"Bearer correct-horse".as_slice())
            );
            assert_eq!(recorded.header("X-TWICE"), Some(b"1".as_slice()));
            assert_eq!(recorded.header("x-empty"), Some(b"".as_slice()));
            assert_eq!(recorded.header("cookie"), None);
            assert_eq!(recorded.body(), b"{\"n\": 1}\n");
            assert_eq!(recorded.bytes(), request);
        });
    }

    #[test]
    fn the_stub_reads_a_body_in_the_chunked_form() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(ok(""));
            let request =
                b"PUT /upload HTTP/1.1\r\nHost: stub\r\nTransfer-Encoding: Chunked\r\n\r\n\
                4\r\nWiki\r\nA;name=value\r\npedia in c\r\n0\r\nX-Sum: 1\r\n\r\n";
            soon(RawHttp::tcp(stub.port().unwrap(), request))
                .await
                .unwrap();
            let requests = stub.requests();

            assert_eq!(requests[0].body(), b"Wikipedia in c");
            assert_eq!(requests[0].bytes(), request);
            assert_eq!(requests[0].header("x-sum"), None);
        });
    }

    #[test]
    fn the_stub_keeps_a_header_value_that_is_not_utf8() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(ok(""));
            let request = b"GET / HTTP/1.1\r\nAuthorization: Bearer \xff\xfe\r\n\r\n";
            soon(RawHttp::tcp(stub.port().unwrap(), request))
                .await
                .unwrap();

            assert_eq!(
                stub.requests()[0].header("authorization"),
                Some(b"Bearer \xff\xfe".as_slice())
            );
        });
    }

    #[test]
    fn the_answers_go_out_in_the_order_of_the_script() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(ok("first"));
            stub.script(Answer::status(StatusCode::NOT_FOUND).body("second"));

            let first = soon(RawHttp::tcp(port, GET)).await.unwrap();
            stub.script(Answer::status(StatusCode::IM_A_TEAPOT).no_body());
            let second = soon(RawHttp::tcp(port, GET)).await.unwrap();
            let third = soon(RawHttp::tcp(port, GET)).await.unwrap();

            assert!(first.starts_with(b"HTTP/1.1 200 OK\r\n"));
            assert!(first.ends_with(b"first"));
            assert!(second.starts_with(b"HTTP/1.1 404 Not Found\r\n"));
            assert!(second.ends_with(b"second"));
            assert!(third.starts_with(b"HTTP/1.1 418 I'm a teapot\r\n"));
            assert_eq!(stub.requests().len(), 3);
            assert_eq!(stub.ends(), [End::Answered; 3]);
        });
    }

    #[test]
    fn a_request_with_no_scripted_answer_gets_no_byte() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let answer = soon(RawHttp::tcp(stub.port().unwrap(), GET)).await.unwrap();

            assert_eq!(answer, b"");
            assert_eq!(stub.ends(), [End::NoScript]);
            assert_eq!(stub.requests().len(), 1);
            assert_eq!(stub.requests()[0].target(), "/healthz");
        });
    }

    #[test]
    fn a_status_with_no_known_name_still_has_a_status_line() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(Answer::status(StatusCode::from_u16(599).unwrap()).no_body());
            let answer = soon(RawHttp::tcp(stub.port().unwrap(), GET)).await.unwrap();

            assert_eq!(answer, b"HTTP/1.1 599 Status\r\nconnection: close\r\n\r\n");
        });
    }

    #[test]
    fn a_chunked_answer_goes_out_chunk_by_chunk_with_its_pause() {
        const PAUSE: Duration = Duration::from_millis(80);

        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(
                Answer::status(StatusCode::OK)
                    .header("content-type", "text/event-stream")
                    .chunked()
                    .chunk("data: one\n\n")
                    .chunk("")
                    .pause(PAUSE)
                    .chunk(b"data: two\n\n".to_vec())
                    .end(),
            );
            let started = Instant::now();
            let answer = soon(RawHttp::tcp(stub.port().unwrap(), GET)).await.unwrap();

            assert!(started.elapsed() >= PAUSE, "{:?}", started.elapsed());
            assert_eq!(
                String::from_utf8(answer).unwrap(),
                "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n\
                 transfer-encoding: chunked\r\nconnection: close\r\n\r\n\
                 b\r\ndata: one\n\n\r\nb\r\ndata: two\n\n\r\n0\r\n\r\n"
            );
            assert_eq!(stub.ends(), [End::Answered]);
        });
    }

    #[test]
    fn the_first_chunk_reaches_the_client_before_the_pause_ends() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(
                Answer::status(StatusCode::OK)
                    .chunked()
                    .chunk("one")
                    .pause(LONG)
                    .chunk("two")
                    .end(),
            );
            let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, stub.port().unwrap()))
                .await
                .unwrap();
            stream.write_all(GET).await.unwrap();
            let mut seen = Vec::new();
            let mut chunk = [0_u8; 256];

            while !seen.ends_with(b"3\r\none\r\n") {
                let count = soon(stream.read(&mut chunk)).await.unwrap();
                assert_ne!(count, 0, "the stub closed the connection");
                seen.extend_from_slice(&chunk[..count]);
            }

            assert!(timeout(SHORT, stream.read(&mut chunk)).await.is_err());
            assert_eq!(stub.ends(), []);
        });
    }

    #[test]
    fn a_late_head_goes_out_after_its_pause() {
        const PAUSE: Duration = Duration::from_millis(80);

        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(Answer::status(StatusCode::OK).after(PAUSE).body("late"));
            let started = Instant::now();
            let answer = soon(RawHttp::tcp(stub.port().unwrap(), GET)).await.unwrap();

            assert!(started.elapsed() >= PAUSE, "{:?}", started.elapsed());
            assert!(answer.starts_with(b"HTTP/1.1 200 OK\r\n"));
            assert!(answer.ends_with(b"\r\n\r\nlate"));
        });
    }

    #[test]
    fn a_pause_is_a_timer_of_tokio_that_a_paused_time_moves() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .start_paused(true)
            .build()
            .unwrap();

        runtime.block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(
                Answer::status(StatusCode::OK)
                    .after(Duration::from_secs(3600))
                    .body("late"),
            );
            let started = Instant::now();
            let answer = RawHttp::tcp(stub.port().unwrap(), GET).await.unwrap();

            assert!(answer.ends_with(b"late"));
            assert!(started.elapsed() >= Duration::from_secs(3600));
        });
    }

    #[test]
    fn a_cut_answer_has_no_last_chunk() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(Answer::status(StatusCode::OK).chunked().chunk("one").cut());
            let answer = soon(RawHttp::tcp(stub.port().unwrap(), GET)).await.unwrap();

            assert!(answer.ends_with(b"\r\n\r\n3\r\none\r\n"), "{answer:?}");
            assert_eq!(stub.ends(), [End::Answered]);
        });
    }

    #[test]
    fn a_raw_answer_goes_out_byte_for_byte() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(Answer::raw(
                "HTTP/1.1 200 OK\r\ncontent-length: 9\r\n\r\nhalf",
            ));
            stub.script(Answer::raw(b"\x00\xff no status line".to_vec()));
            stub.script(Answer::close());

            let short_body = soon(RawHttp::tcp(port, GET)).await.unwrap();
            let no_status = soon(RawHttp::tcp(port, GET)).await.unwrap();
            let nothing = soon(RawHttp::tcp(port, GET)).await.unwrap();

            assert_eq!(
                short_body,
                b"HTTP/1.1 200 OK\r\ncontent-length: 9\r\n\r\nhalf"
            );
            assert_eq!(no_status, b"\x00\xff no status line");
            assert_eq!(nothing, b"");
            assert_eq!(stub.ends(), [End::Answered; 3]);
        });
    }

    #[test]
    fn an_answer_that_never_starts_keeps_the_connection_until_the_client_leaves() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(Answer::hold());
            let port = stub.port().unwrap();

            let waited = timeout(SHORT, RawHttp::tcp(port, GET)).await;

            assert!(waited.is_err(), "the stub answered: {waited:?}");
            assert_eq!(soon(stub.wait_ends(1)).await, [End::ClientLeft]);
            assert_eq!(stub.requests().len(), 1);
        });
    }

    #[test]
    fn an_answer_with_no_end_stops_when_the_client_leaves() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(
                Answer::status(StatusCode::OK)
                    .chunked()
                    .chunk("first")
                    .forever("tick", Duration::from_millis(5)),
            );
            let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, stub.port().unwrap()))
                .await
                .unwrap();
            stream.write_all(GET).await.unwrap();
            let mut seen = Vec::new();
            let mut chunk = [0_u8; 256];

            while find(&seen, b"4\r\ntick\r\n4\r\ntick\r\n4\r\ntick\r\n").is_none() {
                let count = soon(stream.read(&mut chunk)).await.unwrap();
                assert_ne!(count, 0, "the stub closed the connection");
                seen.extend_from_slice(&chunk[..count]);
            }

            assert!(find(&seen, b"\r\n\r\n5\r\nfirst\r\n4\r\ntick\r\n").is_some());
            assert_eq!(stub.ends(), []);

            drop(stream);

            assert_eq!(soon(stub.wait_ends(1)).await, [End::ClientLeft]);
        });
    }

    #[test]
    fn an_endless_answer_of_no_byte_keeps_the_connection_open() {
        let silent = Answer::status(StatusCode::OK)
            .chunked()
            .forever("", Duration::from_millis(1));

        assert_eq!(silent.steps.last(), Some(&Step::Hold));
    }

    #[test]
    fn a_listener_that_never_accepts_takes_the_connect_and_reads_nothing() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(ok("before"));
            let before = soon(RawHttp::tcp(port, GET)).await.unwrap();

            stub.script(ok("after"));
            stub.stop_accepting();
            let after = timeout(SHORT, RawHttp::tcp(port, GET)).await;

            assert!(before.ends_with(b"before"));
            assert!(after.is_err(), "the stub answered: {after:?}");
            assert_eq!(stub.requests().len(), 1);
            assert_eq!(stub.ends(), [End::Answered]);
        });
    }

    #[test]
    fn a_listener_on_a_unix_socket_that_never_accepts_reads_nothing() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let stub = HttpStub::unix(&root).await.unwrap();
            stub.stop_accepting();
            let waited = timeout(SHORT, RawHttp::unix(stub.socket().unwrap(), GET)).await;

            assert!(waited.is_err(), "the stub answered: {waited:?}");
            assert_eq!(stub.requests(), []);
            assert_eq!(stub.ends(), []);
        });
    }

    #[test]
    fn bytes_that_are_no_request_end_the_connection_with_no_byte() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(ok("unused"));

            for request in [
                b"hello\r\n\r\n".as_slice(),
                b"GET /\r\n\r\n",
                b"GET / HTTP/2\r\n\r\n",
                b"GET / HTTP/1.1\r\nno colon\r\n\r\n",
                b"POST / HTTP/1.1\r\nContent-Length: nine\r\n\r\n",
                b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
            ] {
                let answer = soon(RawHttp::tcp(port, request)).await.unwrap();

                assert_eq!(answer, b"", "{:?}", String::from_utf8_lossy(request));
            }

            assert_eq!(stub.ends(), [End::BadRequest; 6]);
            assert_eq!(stub.requests(), []);
        });
    }

    #[test]
    fn a_client_that_leaves_in_the_middle_of_a_request_is_no_request() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(ok("unused"));

            let half_head = RawHttp::tcp_then_eof(port, b"GET /healthz HTTP/1.1\r\nHost: st");
            let half_body =
                RawHttp::tcp_then_eof(port, b"POST / HTTP/1.1\r\nContent-Length: 9\r\n\r\nhalf");
            let nothing = RawHttp::tcp_then_eof(port, b"");

            assert_eq!(soon(half_head).await.unwrap(), b"");
            assert_eq!(soon(half_body).await.unwrap(), b"");
            assert_eq!(soon(nothing).await.unwrap(), b"");
            assert_eq!(stub.ends(), [End::ClientLeft; 3]);
            assert_eq!(stub.requests(), []);
        });
    }

    #[test]
    fn a_request_that_comes_in_small_parts_is_one_request() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            stub.script(ok("whole"));
            let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, stub.port().unwrap()))
                .await
                .unwrap();
            stream.set_nodelay(true).unwrap();
            let request = b"POST /parts HTTP/1.1\r\nContent-Length: 4\r\n\r\nbody";

            for part in request.chunks(7) {
                stream.write_all(part).await.unwrap();
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
            let mut answer = Vec::new();
            soon(stream.read_to_end(&mut answer)).await.unwrap();

            assert!(answer.ends_with(b"whole"));
            assert_eq!(stub.requests()[0].bytes(), request);
        });
    }

    #[test]
    fn two_connections_at_one_time_each_get_an_answer() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(Answer::hold());
            stub.script(ok("second"));

            let mut held = TcpStream::connect((Ipv4Addr::LOCALHOST, port))
                .await
                .unwrap();
            held.write_all(GET).await.unwrap();
            assert_eq!(soon(stub.wait_requests(1)).await.len(), 1);

            let second = soon(RawHttp::tcp(port, GET)).await.unwrap();

            assert!(second.ends_with(b"second"));
            assert_eq!(stub.ends(), [End::Answered]);
        });
    }

    #[test]
    fn the_wait_for_a_request_ends_when_the_stub_read_it() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(ok("ok"));

            let none = timeout(SHORT, stub.wait_requests(1)).await;
            let (requests, answer) =
                tokio::join!(soon(stub.wait_requests(1)), RawHttp::tcp(port, GET));

            assert!(none.is_err());
            assert_eq!(requests.len(), 1);
            assert!(answer.unwrap().ends_with(b"ok"));
            assert_eq!(stub.wait_requests(0).await.len(), 1);
            assert_eq!(stub.wait_ends(0).await, [End::Answered]);
        });
    }

    #[test]
    fn a_dropped_stub_closes_its_listener_and_its_connections() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let stub = HttpStub::unix(&root).await.unwrap();
            let socket = stub.socket().unwrap().to_owned();
            stub.script(Answer::hold());
            let mut held = UnixStream::connect(&socket).await.unwrap();
            held.write_all(GET).await.unwrap();
            soon(stub.wait_requests(1)).await;

            drop(stub);
            let mut rest = Vec::new();
            let closed = soon(held.read_to_end(&mut rest)).await;
            // The path of the socket is the path of this test only, so the
            // connect reaches no stub of another test.
            let refused = soon(async {
                loop {
                    match UnixStream::connect(&socket).await {
                        Err(error) => return error,
                        Ok(_) => tokio::time::sleep(Duration::from_millis(5)).await,
                    }
                }
            })
            .await;

            assert!(matches!(closed, Ok(0) | Err(_)), "{closed:?}");
            assert_eq!(rest, b"");
            assert_eq!(refused.kind(), io::ErrorKind::ConnectionRefused);
        });
    }

    #[test]
    fn a_stub_outside_a_runtime_is_an_error() {
        use std::task::{Context, Poll, Waker};

        let root = TempRoot::new().unwrap();
        let mut context = Context::from_waker(Waker::noop());
        let loopback = std::pin::pin!(HttpStub::loopback());
        let unix = std::pin::pin!(HttpStub::unix(&root));

        let Poll::Ready(Err(on_a_port)) = loopback.poll(&mut context) else {
            panic!("a stub started with no runtime");
        };
        let Poll::Ready(Err(on_a_socket)) = unix.poll(&mut context) else {
            panic!("a stub started with no runtime");
        };

        assert_eq!(on_a_port.kind(), io::ErrorKind::Other);
        assert_eq!(on_a_socket.kind(), io::ErrorKind::Other);
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 0);
    }

    #[test]
    fn the_debug_of_a_request_prints_no_header_value_and_no_body() {
        let Parsed::Whole(request) = request_of(
            b"POST /v1/turns HTTP/1.1\r\nAuthorization: Bearer correct-horse\r\n\
              Content-Length: 14\r\n\r\nbattery-staple",
        ) else {
            panic!("the bytes are one request");
        };
        let text = format!("{request:?}");

        assert_eq!(
            text,
            "Recorded { method: \"POST\", target: \"/v1/turns\", \
             headers: [\"Authorization\", \"Content-Length\"], .. }"
        );
        assert!(!text.contains("correct-horse"));
        assert!(!text.contains("battery-staple"));
        assert!(!text.contains("14"));
    }

    #[test]
    fn the_debug_of_a_stub_and_of_an_answer_prints_no_byte() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let answer = Answer::status(StatusCode::OK)
                .after(Duration::from_millis(5))
                .chunked()
                .chunk("correct-horse")
                .forever("battery-staple", Duration::from_secs(1));
            let text = format!("{answer:?}");
            stub.script(answer);

            assert_eq!(
                text,
                "Answer { steps: [Pause(5ms), Send(..), Send(..), Repeat(1s)] }"
            );
            assert_eq!(
                format!("{stub:?}"),
                format!(
                    "HttpStub {{ port: Some({}), socket: None, .. }}",
                    stub.port().unwrap()
                )
            );
            assert_eq!(format!("{:?}", Answer::hold()), "Answer { steps: [Hold] }");
        });
    }

    #[test]
    fn the_head_of_a_request_reads_in_each_form() {
        let whole = |bytes: &[u8]| match head_of(bytes) {
            Parsed::Whole(head) => head,
            other => panic!("{other:?}: {:?}", String::from_utf8_lossy(bytes)),
        };

        let plain = whole(b"GET / HTTP/1.1\r\n\r\nrest");
        let old = whole(b"HEAD /x HTTP/1.0\r\nA: 1\r\n\r\n");
        let bare_newline = whole(b"GET / HTTP/1.1\nHost: stub\r\n\r\n");
        let length = whole(b"POST / HTTP/1.1\r\ncontent-length: 0012\r\n\r\n");
        let chunked = whole(
            b"POST / HTTP/1.1\r\nContent-Length: 5\r\nTransfer-Encoding: gzip, chunked\r\n\r\n",
        );

        assert_eq!(
            plain,
            RequestHead {
                method: String::from("GET"),
                target: String::from("/"),
                headers: Vec::new(),
                len: 18,
                framing: Framing::NoBody,
            }
        );
        assert_eq!(old.method, "HEAD");
        assert_eq!(old.headers, [(String::from("A"), b"1".to_vec())]);
        assert_eq!(bare_newline.headers.len(), 1);
        assert_eq!(length.framing, Framing::Length(12));
        assert_eq!(chunked.framing, Framing::Chunked);
    }

    #[test]
    fn a_head_that_is_not_whole_or_not_a_head_is_no_head() {
        let partial: [&[u8]; 4] = [
            b"",
            b"GET / HTTP/1.1",
            b"GET / HTTP/1.1\r\n",
            b"GET / HTTP/1.1\r\nHost: stub\r\n\r",
        ];
        let bad: [&[u8]; 12] = [
            b"\r\n\r\n",
            b"GET\r\n\r\n",
            b"GET /\r\n\r\n",
            b"GET / HTTP/1.1 extra\r\n\r\n",
            b"GET  / HTTP/1.1\r\n\r\n",
            b" / HTTP/1.1\r\n\r\n",
            b"GET / SPDY/3\r\n\r\n",
            b"GET /\xff HTTP/1.1\r\n\r\n",
            b"GET / HTTP/1.1\r\n: value\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost : stub\r\n\r\n",
            b"POST / HTTP/1.1\r\nContent-Length: +9\r\n\r\n",
            b"POST / HTTP/1.1\r\nContent-Length: 16777217\r\n\r\n",
        ];
        let mut long = b"GET / HTTP/1.1\r\nX-Long: ".to_vec();
        long.resize(HEAD_MAX + 1, b'a');

        for bytes in partial {
            assert_eq!(head_of(bytes), Parsed::Partial, "{bytes:?}");
        }
        for bytes in bad {
            assert_eq!(
                head_of(bytes),
                Parsed::Bad,
                "{:?}",
                String::from_utf8_lossy(bytes)
            );
        }

        assert_eq!(head_of(&long), Parsed::Bad);
        long.extend_from_slice(HEAD_END);
        assert_eq!(head_of(&long), Parsed::Bad);
    }

    #[test]
    fn a_body_reads_in_each_form() {
        let whole = |bytes: &[u8], used: usize| Parsed::Whole((bytes.to_vec(), used));
        let two_chunks = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n";
        let with_trailers = b"1 ;ext=1\r\na\r\n0\r\nTrailer: 1\r\nOther: 2\r\n\r\n";
        let long_size = b"00000A\r\n0123456789\r\n0\r\n\r\n";

        assert_eq!(body_of(Framing::NoBody, b"next"), whole(b"", 0));
        assert_eq!(body_of(Framing::Length(0), b""), whole(b"", 0));
        assert_eq!(body_of(Framing::Length(4), b"bodynext"), whole(b"body", 4));
        assert_eq!(body_of(Framing::Length(4), b"bod"), Parsed::Partial);
        assert_eq!(body_of(Framing::Chunked, b"0\r\n\r\n"), whole(b"", 5));
        assert_eq!(
            body_of(Framing::Chunked, &[two_chunks.as_slice(), b"next"].concat()),
            whole(b"Wikipedia", two_chunks.len())
        );
        assert_eq!(
            body_of(Framing::Chunked, with_trailers),
            whole(b"a", with_trailers.len())
        );
        assert_eq!(
            body_of(Framing::Chunked, long_size),
            whole(b"0123456789", long_size.len())
        );
    }

    #[test]
    fn a_chunked_body_that_is_not_whole_or_not_chunked_is_no_body() {
        let partial: [&[u8]; 8] = [
            b"",
            b"4",
            b"4\r\n",
            b"4\r\nWik",
            b"4\r\nWiki",
            b"4\r\nWiki\r",
            b"4\r\nWiki\r\n0\r\n",
            b"4\r\nWiki\r\n0\r\nTrailer: 1\r\n",
        ];
        let bad: [&[u8]; 8] = [
            b"\r\n",
            b"zz\r\n",
            b"-1\r\n",
            b"+1\r\na\r\n",
            b"0x1\r\na\r\n",
            b"4\r\nWikiXX",
            b"123456789\r\n",
            b"1000001\r\n",
        ];
        let mut long_line = b"1;".to_vec();
        long_line.resize(CHUNK_LINE_MAX + 2, b'x');

        for bytes in partial {
            assert_eq!(chunked_body(bytes), Parsed::Partial, "{bytes:?}");
        }
        for bytes in bad {
            assert_eq!(
                chunked_body(bytes),
                Parsed::Bad,
                "{:?}",
                String::from_utf8_lossy(bytes)
            );
        }

        assert_eq!(chunked_body(&long_line), Parsed::Bad);
    }

    #[test]
    fn blanks_at_the_two_ends_are_spaces_and_tabs_only() {
        assert_eq!(trim_blanks(b" \t a b \t "), b"a b");
        assert_eq!(trim_blanks(b"\t"), b"");
        assert_eq!(trim_blanks(b""), b"");
        assert_eq!(trim_blanks(b"\x0ba\x0c"), b"\x0ba\x0c");
    }

    /// A server of the test on a Unix socket: it takes one connection and
    /// gives it to `serve`.
    async fn one_connection<F, Fut>(root: &TempRoot, serve: F) -> PathBuf
    where
        F: FnOnce(UnixStream) -> Fut + Send + 'static,
        Fut: Future<Output = ()> + Send,
    {
        let socket = root.path().join("server.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            serve(stream).await;
        });

        socket
    }

    #[test]
    fn the_raw_client_can_close_its_side_after_the_request() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let socket = one_connection(&root, |mut stream| async move {
                // The read ends only when the client closes its side.
                let mut request = Vec::new();
                stream.read_to_end(&mut request).await.unwrap();
                stream.write_all(b"got ").await.unwrap();
                stream.write_all(&request).await.unwrap();
            })
            .await;

            let answer = soon(RawHttp::unix_then_eof(&socket, b"half a he")).await;

            assert_eq!(answer.unwrap(), b"got half a he");
        });
    }

    #[test]
    fn the_raw_client_keeps_its_side_open_until_the_answer_ends() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let socket = one_connection(&root, |mut stream| async move {
                let mut request = [0_u8; 4];
                stream.read_exact(&mut request).await.unwrap();
                // A client that closed its side makes this read end at once.
                let more = timeout(SHORT, stream.read(&mut request)).await;
                let seen: &[u8] = if more.is_err() { b"open" } else { b"closed" };
                stream.write_all(seen).await.unwrap();
            })
            .await;

            let answer = soon(RawHttp::unix(&socket, b"ping")).await;

            assert_eq!(answer.unwrap(), b"open");
        });
    }

    #[test]
    fn the_raw_client_returns_an_answer_that_came_before_the_whole_request() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let socket = one_connection(&root, |mut stream| async move {
                // The server reads no byte, answers and closes. The write of
                // the client then fails in its middle.
                stream.write_all(b"early").await.unwrap();
            })
            .await;
            let request = vec![b'a'; 8 * 1024 * 1024];

            let answer = soon(RawHttp::unix(&socket, &request)).await;

            assert_eq!(answer.unwrap(), b"early");
        });
    }

    #[test]
    fn the_raw_client_gives_the_error_of_a_write_with_no_answer() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let socket = one_connection(&root, |stream| async move {
                drop(stream);
            })
            .await;
            let request = vec![b'a'; 8 * 1024 * 1024];

            let error = soon(RawHttp::unix(&socket, &request)).await.unwrap_err();

            assert!(
                matches!(
                    error.kind(),
                    io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset
                ),
                "{error:?}"
            );
        });
    }

    #[test]
    fn the_raw_client_refuses_an_answer_past_its_cap() {
        block_on(async {
            let stub = HttpStub::loopback().await.unwrap();
            let port = stub.port().unwrap();
            stub.script(Answer::raw("12345678"));
            stub.script(Answer::raw("123456789"));

            let at_the_cap = TcpStream::connect((Ipv4Addr::LOCALHOST, port))
                .await
                .unwrap();
            let at_the_cap = soon(send(at_the_cap, GET, AfterWrite::KeepOpen, 8)).await;
            let past_the_cap = TcpStream::connect((Ipv4Addr::LOCALHOST, port))
                .await
                .unwrap();
            let past_the_cap = soon(send(past_the_cap, GET, AfterWrite::KeepOpen, 8)).await;

            assert_eq!(at_the_cap.unwrap(), b"12345678");
            assert_eq!(
                past_the_cap.unwrap_err().to_string(),
                "the answer has more than 8 bytes"
            );
        });
    }

    #[test]
    fn the_raw_client_gives_the_error_of_a_connect() {
        block_on(async {
            let root = TempRoot::new().unwrap();
            let absent = root.path().join("absent.sock");
            // The file of a socket whose listener closed. The operating
            // system refuses each connect to it.
            let closed = root.path().join("closed.sock");
            drop(UnixListener::bind(&closed).unwrap());

            let no_file = RawHttp::unix(&absent, GET).await.unwrap_err();
            let no_file_then_eof = RawHttp::unix_then_eof(&absent, GET).await.unwrap_err();
            let no_listener = RawHttp::unix(&closed, GET).await.unwrap_err();
            let no_listener_then_eof = RawHttp::unix_then_eof(&closed, GET).await.unwrap_err();
            // No listener has the port 0, so the test connects to no port
            // that another test can hold. The operating system selects the
            // kind of the error: Linux refuses the connect, and macOS has no
            // such address.
            let no_port = soon(RawHttp::tcp(0, GET)).await;
            let no_port_then_eof = soon(RawHttp::tcp_then_eof(0, GET)).await;

            assert_eq!(no_file.kind(), io::ErrorKind::NotFound);
            assert_eq!(no_file_then_eof.kind(), io::ErrorKind::NotFound);
            assert_eq!(no_listener.kind(), io::ErrorKind::ConnectionRefused);
            assert_eq!(
                no_listener_then_eof.kind(),
                io::ErrorKind::ConnectionRefused
            );
            assert!(no_port.is_err(), "{no_port:?}");
            assert!(no_port_then_eof.is_err(), "{no_port_then_eof:?}");
        });
    }
}
