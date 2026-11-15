//! The plane proxies on host ports 1101 and 1102 (plan PR V6, decisions
//! CH6-2, CH14, openshell ideas plan idea 1). Guest init splices guest
//! 127.0.0.1:4000 and :8300 to a vsock dial of host port 1101 and 1102.
//! Cloud Hypervisor turns each such dial into a connect to
//! `vsock.sock_<port>` in the run dir of the VM (decision CH24). The vmm
//! listens there and serves HTTP/1.1. It forwards each request to the
//! plane: LiteLLM for 1101, the chaperone for 1102 (decision CH62 rule
//! 20).
//!
//! 1. `bind` binds both listeners in the run dir of a VM. The start runs
//!    it after `RunRoot::link_vm` emptied the slot (decision CH56 rule 1)
//!    and after `remove_stale`, before CH starts, for a cold boot and for
//!    each restore. A restored guest dials again (decision CH62 rule 14),
//!    so the proxy needs no state of the old VM (fact 45).
//! 2. `Bound::serve` gives each listener a task of its own. Each accepted
//!    connection serves HTTP/1.1 with keep-alive and no idle timeout.
//! 3. Each request must carry exactly one `Authorization` header with the
//!    value `Bearer <placeholder>`, the placeholder of this session for
//!    this port (`Placeholders`). Pi's LiteLLM provider and Flue's MCP
//!    client send their key in that header. Any other value, two headers
//!    or none gets 403 and one stderr line with the session, the port and
//!    the reason, and no byte of the header.
//! 4. A request that passes reads `creds.json` of the family in
//!    `spawn_blocking`, once per request, so a rotation reaches the next
//!    request. The vmm puts `Bearer <real value>` in place of the header
//!    and keeps every other header, `Host` too (fact 46). Both sides keep
//!    the case of each header name as it came. A `creds.json`
//!    that does not read gets 503 and one stderr line.
//! 5. Each guest connection keeps at most one upstream connection, a TCP
//!    connect within `CONNECT_WAIT`. A later request reuses it while it is
//!    ready, else the proxy connects again. A connect that fails or takes
//!    longer gets 502, and nothing retries (decision CH62 rule 14). Guest
//!    clients treat it as transient (decision CH20).
//! 6. The bodies stream both ways, unbuffered. The task of the guest
//!    connection also drives the upstream connection: no task of its own.
//!    A burst of response chunks that arrived together goes out as one
//!    write of at most `GATHER_CAP` bytes, so a back-to-back stream does
//!    not pay one task wake per chunk. A paced stream goes out chunk by
//!    chunk, with no wait.
//! 7. Each listener holds at most `SPLICE_CAP` open connections (decision
//!    CH62 rule 14). A connection past the cap closes at once.
//! 8. The drop of `Planes` closes both listeners and every open
//!    connection. The VM entry of the start table holds it, so the
//!    proxies go with the VM.
//!
//! Host port 1100 is `family_state` (plan PR V8), 1103 is `mirror` (plan
//! PR M1, decision CH69 rule 13), and 1104 is `egress::proxy` (plan PR
//! E2, decision CH49 rule 3).

use std::convert::Infallible;
use std::fmt;
use std::future::{Future, poll_fn};
use std::io;
use std::net::SocketAddr;
use std::os::unix::net::UnixListener as StdListener;
use std::path::PathBuf;
use std::pin::{Pin, pin};
use std::sync::{Arc, Mutex, PoisonError};
use std::task::{Context, Poll, Waker};
use std::time::Duration;

use creche_vmm_confine::family::{FamilyRoot, StateFault};
use creche_vmm_store::layout::{SLOTS, VmRunDir};
use creche_vmm_store::placeholder::Placeholders;
use creche_wire::{FamilyCreds, FamilyName, HostPort, SessionId};
use http_body_util::{Either, Empty};
use hyper::body::{Body, Bytes, Frame, Incoming, SizeHint};
use hyper::client::conn::http1 as client;
use hyper::header::{AUTHORIZATION, HeaderMap, HeaderValue};
use hyper::server::conn::http1 as server;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::{TcpStream, UnixListener, UnixStream};
use tokio::task::{JoinHandle, JoinSet, spawn_blocking};
use tokio::time::{sleep, timeout};

use crate::server::ACCEPT_PAUSE;

/// The most open connections of one proxy row of one VM (decision CH62
/// rule 14).
pub const SPLICE_CAP: usize = 64;

/// The bound of the connect to a plane (decision CH62 rule 14).
pub const CONNECT_WAIT: Duration = Duration::from_secs(5);

/// The most response bytes that one write gathers from chunks that
/// arrived together (rule 6 of the module docs).
pub const GATHER_CAP: usize = 64 * 1024;

/// The proxy rows of each VM: 1101 and 1102.
const ROWS: usize = 2;

/// The fds that one read of `creds.json` holds at once: the family dir,
/// `creds/` and the file (`FamilyRoot::read_creds`).
const CREDS_FDS: u64 = 3;

/// The fds that the vmm holds beside the plane proxies: `vmm.sock` and
/// its links, the links and run files of each VM, the store and the CH
/// children (decision CH67 rule 4).
const FD_HEADROOM: u64 = 4096;

/// The prefix of the `Authorization` value of both planes.
const BEARER: &str = "Bearer ";

/// The fds that the vmm needs when each slot holds a VM whose proxy rows
/// are full: per row its listener, and per connection a Unix stream, a
/// TCP stream and the `CREDS_FDS` of one `creds.json` read, plus
/// `FD_HEADROOM`. `vmm serve` raises its soft `RLIMIT_NOFILE` to it
/// (`creche_vmm_confine::nofile`), so a busy guest cannot use the fds of
/// every other VM.
#[must_use]
pub fn fd_need() -> u64 {
    let each = CREDS_FDS.saturating_add(2);
    let streams = u64::try_from(SPLICE_CAP).map_or(u64::MAX, |cap| cap.saturating_mul(each));
    let rows = u64::try_from(ROWS).unwrap_or(u64::MAX);
    streams
        .saturating_add(1)
        .saturating_mul(rows)
        .saturating_mul(u64::from(SLOTS))
        .saturating_add(FD_HEADROOM)
}

/// The address of each plane (decision CH62 rule 20): the keys
/// `VMM_LITELLM_ADDR` and `VMM_CHAPERONE_ADDR`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Upstreams {
    /// LiteLLM, for host port 1101.
    pub litellm: SocketAddr,
    /// The chaperone, for host port 1102.
    pub chaperone: SocketAddr,
}

impl Upstreams {
    /// Each proxy row: the host port and the address of its plane
    /// (decisions CH6-2, CH14). Its ports are the bound set of the deny
    /// check (decision CH6-3).
    #[must_use]
    pub fn rows(self) -> [(HostPort, SocketAddr); ROWS] {
        [
            (HostPort::Litellm, self.litellm),
            (HostPort::Chaperone, self.chaperone),
        ]
    }
}

/// Why `bind` failed.
#[derive(Debug)]
pub struct BindError {
    /// The listener path.
    pub path: PathBuf,
    /// The error of the bind.
    pub source: io::Error,
}

impl fmt::Display for BindError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "binding {}", self.path.display())
    }
}

impl std::error::Error for BindError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// The listeners of one VM, bound and not yet served. A connection that
/// the guest makes before `serve` waits in the backlog. The drop closes
/// both listeners.
#[derive(Debug)]
pub struct Bound(Vec<(StdListener, HostPort, SocketAddr)>);

/// Binds the listener `vsock.sock_<port>` of each row of `upstreams` in
/// `run` (decision CH24). It blocks, so the start runs it in
/// `spawn_blocking`. The slot is empty, so a path that is there fails the
/// bind.
///
/// # Errors
///
/// A `BindError` for the first listener that did not bind.
pub fn bind(run: &VmRunDir, upstreams: Upstreams) -> Result<Bound, BindError> {
    let mut bound = Vec::new();
    for (port, upstream) in upstreams.rows() {
        let path = run.host_listener(port);
        let listener = StdListener::bind(&path)
            .and_then(|listener| listener.set_nonblocking(true).map(|()| listener))
            .map_err(|source| BindError { path, source })?;
        bound.push((listener, port, upstream));
    }
    Ok(Bound(bound))
}

/// The session of one VM: the family whose `creds.json` each request
/// reads, the session that each log line names, and its placeholders.
#[derive(Debug, Clone)]
pub struct Session {
    /// The family root of the vmm config.
    pub families: FamilyRoot,
    /// The family of the session.
    pub family: FamilyName,
    /// The session.
    pub id: SessionId,
    /// The placeholders of the session, from its row.
    pub placeholders: Placeholders,
}

/// One proxy row of one VM.
#[derive(Debug)]
struct Row {
    port: HostPort,
    upstream: SocketAddr,
    session: Session,
}

impl Bound {
    /// Serves each listener in a task of its own, for `session`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take. The tasks
    /// that started end with the drop of the partial `Planes`.
    pub fn serve(self, session: &Session) -> io::Result<Planes> {
        let mut planes = Planes(Vec::new());
        for (listener, port, upstream) in self.0 {
            let listener = UnixListener::from_std(listener)?;
            let row = Arc::new(Row {
                port,
                upstream,
                session: session.clone(),
            });
            planes.0.push(tokio::spawn(accept(listener, row)));
        }
        Ok(planes)
    }
}

/// The served proxies of one VM. Its drop ends each listener task, and the
/// end of a task drops its listener and its open connections.
#[derive(Debug)]
pub struct Planes(Vec<JoinHandle<()>>);

impl Drop for Planes {
    fn drop(&mut self) {
        for task in &self.0 {
            task.abort();
        }
    }
}

/// Accepts on `listener` until its task ends, and serves each connection.
// Decision CH40 rule 1, as on vmm.sock: a failed accept, such as EMFILE,
// is logged by kind, and the loop pauses ACCEPT_PAUSE and goes on.
async fn accept(listener: UnixListener, row: Arc<Row>) {
    let mut connections = JoinSet::new();
    loop {
        let accepted = listener.accept().await;
        while connections.try_join_next().is_some() {}
        match accepted {
            Ok((guest, _)) if connections.len() < SPLICE_CAP => {
                connections.spawn(serve_guest(guest, Arc::clone(&row)));
            }
            // Decision CH67 rule 3: a connection past the cap of CH62
            // rule 14 closes at once, and the client retries.
            Ok((guest, _)) => drop(guest),
            Err(error) => {
                eprintln!("vmm: a plane accept failed: {}", error.kind());
                sleep(ACCEPT_PAUSE).await;
            }
        }
    }
}

/// The upstream connection that one guest connection keeps between two
/// requests.
type Kept = Arc<Mutex<Option<Upstream>>>;

/// Serves HTTP/1.1 on `guest` until it closes. hyper serves one request
/// at a time, so `kept` is never used by two.
async fn serve_guest(guest: UnixStream, row: Arc<Row>) {
    let kept: Kept = Arc::default();
    let service = service_fn(move |request| forward(Arc::clone(&row), Arc::clone(&kept), request));
    // An error ends this connection only. A guest that hangs up early is
    // no fault of the vmm.
    let _ = server::Builder::new()
        .preserve_header_case(true)
        .serve_connection(TokioIo::new(guest), service)
        .await;
}

/// The body of each answer: a relayed response, or no body.
type Reply = Either<Relayed, Empty<Bytes>>;

/// Why a request gets 403. The text names no byte of the header.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Refusal {
    None,
    Two,
    Wrong,
}

impl Refusal {
    fn text(self) -> &'static str {
        match self {
            Self::None => "no authorization header",
            Self::Two => "two authorization headers",
            Self::Wrong => "an authorization that is not the placeholder",
        }
    }
}

/// The placeholder of `port` in `placeholders`.
fn placeholder(placeholders: &Placeholders, port: HostPort) -> &str {
    match port {
        HostPort::Litellm => placeholders.litellm_key().reveal(),
        _ => placeholders.pep_token().reveal(),
    }
}

/// The real value of `port` in `creds`.
fn real(creds: &FamilyCreds, port: HostPort) -> &str {
    match port {
        HostPort::Litellm => creds.litellm_key.reveal(),
        _ => creds.pep_token.reveal(),
    }
}

/// Whether `headers` hold exactly one `Authorization` with the value
/// `Bearer <expected>`.
fn check(headers: &HeaderMap, expected: &str) -> Result<(), Refusal> {
    let mut values = headers.get_all(AUTHORIZATION).iter();
    let Some(value) = values.next() else {
        return Err(Refusal::None);
    };
    if values.next().is_some() {
        return Err(Refusal::Two);
    }
    let given = value.as_bytes().strip_prefix(BEARER.as_bytes());
    if given != Some(expected.as_bytes()) {
        return Err(Refusal::Wrong);
    }
    Ok(())
}

/// An answer of `status` with no body.
fn status(status: StatusCode) -> Response<Reply> {
    let mut answer = Response::new(Either::Right(Empty::new()));
    *answer.status_mut() = status;
    answer
}

/// The log line of a refused request.
fn refused_log(row: &Row, why: &str) -> String {
    let (session, port) = (&row.session.id, row.port.number());
    format!("vmm: session {session} plane {port} refused: {why}")
}

/// The log line of a request whose `creds.json` did not read.
fn creds_log(row: &Row, fault: StateFault) -> String {
    let (session, port) = (&row.session.id, row.port.number());
    format!("vmm: session {session} plane {port} has no creds: {fault:?}")
}

/// Answers one request: 403 for a wrong `Authorization`, 503 for no creds,
/// 502 for no upstream, else the response of the plane.
async fn forward(
    row: Arc<Row>,
    kept: Kept,
    mut request: Request<Incoming>,
) -> Result<Response<Reply>, Infallible> {
    let expected = placeholder(&row.session.placeholders, row.port);
    if let Err(refusal) = check(request.headers(), expected) {
        eprintln!("{}", refused_log(&row, refusal.text()));
        return Ok(status(StatusCode::FORBIDDEN));
    }
    let reader = Arc::clone(&row);
    let read = spawn_blocking(move || {
        let session = &reader.session;
        session.families.read_creds(&session.family)
    })
    .await;
    let creds = match read {
        Ok(Ok(creds)) => creds,
        Ok(Err(fault)) => {
            eprintln!("{}", creds_log(&row, fault));
            return Ok(status(StatusCode::SERVICE_UNAVAILABLE));
        }
        Err(_) => return Ok(status(StatusCode::SERVICE_UNAVAILABLE)),
    };
    // A value of the wire type is visible ASCII, so it is a header value.
    let Ok(mut value) = HeaderValue::try_from(format!("{BEARER}{}", real(&creds, row.port))) else {
        return Ok(status(StatusCode::SERVICE_UNAVAILABLE));
    };
    value.set_sensitive(true);
    request.headers_mut().insert(AUTHORIZATION, value);

    let taken = kept.lock().unwrap_or_else(PoisonError::into_inner).take();
    let reused = taken.and_then(Upstream::ready_now);
    let mut upstream = match reused {
        Some(upstream) => upstream,
        None => match Upstream::connect(row.upstream).await {
            Some(upstream) => upstream,
            None => return Ok(status(StatusCode::BAD_GATEWAY)),
        },
    };
    match upstream.send(request).await {
        Ok(response) => Ok(response.map(|body| Either::Left(Relayed::new(body, upstream, kept)))),
        Err(_) => Ok(status(StatusCode::BAD_GATEWAY)),
    }
}

/// The HTTP/1.1 client connection of one TCP connect to the plane.
type Connection = client::Connection<TokioIo<TcpStream>, Incoming>;

/// One upstream connection: its sender and its connection future. The
/// task of the guest connection drives the future (rule 6).
struct Upstream {
    sender: client::SendRequest<Incoming>,
    /// `None` after the connection ended.
    connection: Option<Pin<Box<Connection>>>,
}

impl fmt::Debug for Upstream {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Upstream")
            .field("ended", &self.connection.is_none())
            .finish_non_exhaustive()
    }
}

impl Upstream {
    /// A TCP connect to `address` within `CONNECT_WAIT`, then the HTTP/1.1
    /// handshake. `None` when either fails.
    async fn connect(address: SocketAddr) -> Option<Self> {
        let stream = timeout(CONNECT_WAIT, TcpStream::connect(address))
            .await
            .ok()?
            .ok()?;
        // Each head and each gathered write goes out at once.
        stream.set_nodelay(true).ok()?;
        let (sender, connection) = client::Builder::new()
            .preserve_header_case(true)
            .handshake(TokioIo::new(stream))
            .await
            .ok()?;
        let mut upstream = Self {
            sender,
            connection: Some(Box::pin(connection)),
        };
        poll_fn(|context| upstream.poll_ready(context)).await.ok()?;
        Some(upstream)
    }

    /// Polls the connection once, so it reads and writes what it can. An
    /// ended connection is not polled again.
    fn drive(&mut self, context: &mut Context<'_>) {
        if let Some(connection) = self.connection.as_mut()
            && connection.as_mut().poll(context).is_ready()
        {
            self.connection = None;
        }
    }

    /// Whether the sender takes a request, with the connection driven.
    fn poll_ready(&mut self, context: &mut Context<'_>) -> Poll<hyper::Result<()>> {
        self.drive(context);
        self.sender.poll_ready(context)
    }

    /// The kept connection if it takes a request now. One that does not
    /// is dropped, and the proxy connects again.
    fn ready_now(mut self) -> Option<Self> {
        let mut context = Context::from_waker(Waker::noop());
        let ready = matches!(self.poll_ready(&mut context), Poll::Ready(Ok(())));
        (ready && self.connection.is_some()).then_some(self)
    }

    /// Sends `request` and waits for the response head, with the
    /// connection driven.
    async fn send(&mut self, request: Request<Incoming>) -> hyper::Result<Response<Incoming>> {
        let mut response = pin!(self.sender.send_request(request));
        poll_fn(|context| {
            self.drive(context);
            response.as_mut().poll(context)
        })
        .await
    }
}

/// The response body of the plane on its way to the guest. Its poll
/// drives the upstream connection and gathers the chunks that are ready
/// (rule 6). At the end of the body the upstream goes back to `kept`.
#[derive(Debug)]
pub struct Relayed {
    body: Incoming,
    upstream: Option<Upstream>,
    kept: Kept,
    /// A trailers frame that waits behind gathered data.
    held: Option<Frame<Bytes>>,
    ended: bool,
}

impl Relayed {
    fn new(body: Incoming, upstream: Upstream, kept: Kept) -> Self {
        Self {
            body,
            upstream: Some(upstream),
            kept,
            held: None,
            ended: false,
        }
    }

    /// Gives the upstream back to `kept` if its connection still runs.
    fn finish(&mut self) {
        self.ended = true;
        if let Some(upstream) = self.upstream.take()
            && upstream.connection.is_some()
        {
            *self.kept.lock().unwrap_or_else(PoisonError::into_inner) = Some(upstream);
        }
    }

    /// Drives the upstream connection once. `false` when there is none.
    fn drive(&mut self, context: &mut Context<'_>) -> bool {
        match self.upstream.as_mut() {
            Some(upstream) if upstream.connection.is_some() => {
                upstream.drive(context);
                true
            }
            _ => false,
        }
    }
}

/// The chunks of one gathered write: the first one as it came, and a copy
/// once a second one joins it.
#[derive(Default)]
struct Gathered {
    first: Option<Bytes>,
    joined: Vec<u8>,
}

impl Gathered {
    fn push(&mut self, data: Bytes) {
        match self.first.take() {
            None if self.joined.is_empty() => self.first = Some(data),
            None => self.joined.extend_from_slice(&data),
            Some(first) => {
                self.joined.reserve(first.len() + data.len());
                self.joined.extend_from_slice(&first);
                self.joined.extend_from_slice(&data);
            }
        }
    }

    fn len(&self) -> usize {
        self.first.as_ref().map_or(self.joined.len(), Bytes::len)
    }

    fn frame(self) -> Option<Frame<Bytes>> {
        match self.first {
            Some(first) => Some(Frame::data(first)),
            None if self.joined.is_empty() => None,
            None => Some(Frame::data(Bytes::from(self.joined))),
        }
    }
}

impl Body for Relayed {
    type Data = Bytes;
    type Error = hyper::Error;

    fn poll_frame(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, hyper::Error>>> {
        let this = self.get_mut();
        if let Some(held) = this.held.take() {
            return Poll::Ready(Some(Ok(held)));
        }
        if this.ended {
            return Poll::Ready(None);
        }
        let mut gathered = Gathered::default();
        let mut drove = false;
        loop {
            match Pin::new(&mut this.body).poll_frame(context) {
                Poll::Ready(Some(Ok(frame))) => {
                    drove = false;
                    match frame.into_data() {
                        Ok(data) => gathered.push(data),
                        Err(frame) => {
                            let Some(data) = gathered.frame() else {
                                return Poll::Ready(Some(Ok(frame)));
                            };
                            this.held = Some(frame);
                            return Poll::Ready(Some(Ok(data)));
                        }
                    }
                    // hyper drops a body that says it ended, with no
                    // poll after its last frame, so the end gives the
                    // upstream back here.
                    if this.body.is_end_stream() {
                        this.finish();
                        return Poll::Ready(gathered.frame().map(Ok));
                    }
                    if gathered.len() >= GATHER_CAP {
                        return Poll::Ready(gathered.frame().map(Ok));
                    }
                }
                Poll::Ready(Some(Err(error))) => return Poll::Ready(Some(Err(error))),
                Poll::Ready(None) => {
                    this.finish();
                    return Poll::Ready(gathered.frame().map(Ok));
                }
                Poll::Pending if !drove && this.drive(context) => drove = true,
                Poll::Pending => {
                    return match gathered.frame() {
                        Some(data) => Poll::Ready(Some(Ok(data))),
                        None => Poll::Pending,
                    };
                }
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        self.held.is_none() && (self.ended || self.body.is_end_stream())
    }

    fn size_hint(&self) -> SizeHint {
        self.body.size_hint()
    }
}

#[cfg(test)]
mod tests {
    use hyper::header::{AUTHORIZATION, HeaderMap, HeaderValue};

    use super::{Refusal, check};

    const PLACEHOLDER: &str = "sk-AAAAAAAAAAAAAAAAAAAAAA";

    fn headers(values: &[&'static str]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        for value in values {
            headers.append(AUTHORIZATION, HeaderValue::from_static(value));
        }
        headers
    }

    /// Only one header with the exact bearer passes.
    #[test]
    fn the_check_takes_only_the_exact_placeholder() {
        let good = "Bearer sk-AAAAAAAAAAAAAAAAAAAAAA";
        assert_eq!(check(&headers(&[good]), PLACEHOLDER), Ok(()));
        assert_eq!(check(&headers(&[]), PLACEHOLDER), Err(Refusal::None));
        assert_eq!(
            check(&headers(&[good, good]), PLACEHOLDER),
            Err(Refusal::Two)
        );
        for wrong in [
            "Bearer sk-AAAAAAAAAAAAAAAAAAAAAB",
            "bearer sk-AAAAAAAAAAAAAAAAAAAAAA",
            "Bearer  sk-AAAAAAAAAAAAAAAAAAAAAA",
            "Bearer sk-AAAAAAAAAAAAAAAAAAAAAA ",
            "sk-AAAAAAAAAAAAAAAAAAAAAA",
            "Bearer sk-real-key",
        ] {
            assert_eq!(
                check(&headers(&[wrong]), PLACEHOLDER),
                Err(Refusal::Wrong),
                "{wrong}"
            );
        }
    }
}
