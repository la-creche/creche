//! The CONNECT proxy on host port 1104 (plan PR E2, decisions CH6-1,
//! CH6-4, CH46 rules 1 and 3, CH49 rule 3, CH69 rules 7, 9, 10, 14, 20,
//! 24 and 30). Guest init splices guest 127.0.0.1:3128 to a vsock dial of
//! host port 1104 (decision CH46 rule 1), and Cloud Hypervisor connects
//! each dial to `vsock.sock_1104` in the run dir of the VM (decision
//! CH24). The vmm listens there only for a family whose `egress.json`
//! lists a host (decision CH49 rule 3). Without the listener the guest
//! dial fails, so a VM with no list fails closed.
//!
//! 1. `bind` binds the listener beside the plane listeners, with their
//!    lifecycle: after `RunRoot::link_vm` emptied the slot and after
//!    `remove_stale`, before CH starts, for a cold boot and for each
//!    restore (plan PR V6, decision CH56 rule 1).
//! 2. Each link reads one request head: the bytes up to the first CRLF
//!    CRLF, at most `HEAD_CAP`, within `HEAD_WAIT` (decision CH69 rule
//!    9). A head that does not come whole within the wait closes the link
//!    with no reply, as on `vmm.sock` (decision CH40 rule 2).
//! 3. Only `CONNECT <host>:<port> HTTP/1.x` passes. Another method gets
//!    405, and a head that does not read gets 400 (decision CH69 rules 7
//!    and 9). The proxy reads no header line.
//! 4. Each CONNECT reads `egress.json` again (`read_list`) and calls
//!    `rule::decide`, the function of the route `check` (decisions CH6-4,
//!    CH69 rule 2). So a removal refuses the next CONNECT at once, and an
//!    open tunnel runs on (fact 43). A refusal of the list, of an IP
//!    literal or of a range gets 403, as does a list that does not read
//!    (decision CH70 rule 2). A failed lookup gets 502.
//! 5. The proxy connects to the addresses that the rule judged, in their
//!    order, within `CONNECT_WAIT` in all (decisions CH6-4, CH62 rule 14).
//!    No address that takes the connect gets 502, and the wait 504. Then
//!    it answers `200 Connection established`, and `Splice` carries the
//!    bytes both ways with the half-close of decision CH67 rule 9. It
//!    never reads the tunnel bytes (fact 42, decision CH46 rule 3) and has
//!    no idle timeout (decision CH69 rule 9).
//! 6. Each link writes one line to the JSONL log (`egress::log`): a
//!    refusal at its refusal, a tunnel at its end, also when the drop of
//!    `Server` cuts it (decisions CH6-4, CH69 rule 10). The proxy writes
//!    stderr only for a log fault and an accept fault (decision CH69
//!    rules 24 and 30).
//! 7. The listener holds at most `LINK_CAP` links. A link past the cap is
//!    accepted, logged and closed at once (decisions CH67 rule 3, CH69
//!    rule 14). The links that drain after a close count against the cap
//!    of the reopen. A failed accept is logged by kind, and the loop
//!    pauses `ACCEPT_PAUSE` and accepts again (decisions CH40 rule 1, CH69
//!    rule 30).
//! 8. A link does not set `last_use` and does not block a snapshot
//!    (decision CH69 rule 20). This module holds no handle on the VM
//!    table, so a restore cuts each open tunnel (fact 45).
//! 9. The drop of `Server` closes the listener and every link. The VM
//!    entry of the start table holds it, so the listener goes with the VM.
//! 10. `Server::close` closes only the listener, after an edit that
//!     empties `egress.json` (decision CH74 rule 2). Each open tunnel runs
//!     to its own end in `Draining`, and the drop of `Draining` cuts it.
//!     `Draining::reopen` serves a new listener with the same links.

use std::future::poll_fn;
use std::io;
use std::net::IpAddr;
use std::os::unix::net::UnixListener as StdListener;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::rfc3339;
use creche_vmm_store::layout::{SLOTS, VmRunDir};
use creche_wire::{CheckReason, CheckReply, FamilyName, HostPort, SessionId};
use tokio::net::{TcpStream, UnixListener, UnixStream};
use tokio::runtime::Handle;
use tokio::time::{sleep, timeout};

use crate::egress::log::{Line, Log, ProxyReason, Reason, Verdict};
use crate::egress::read_list;
use crate::egress::rule::{Target, decide};
use crate::links::{Accept, Links};
use crate::planes::{BindError, CONNECT_WAIT, SPLICE_CAP};
use crate::relay::Splice;
use crate::server::ACCEPT_PAUSE;

/// The most bytes of a request head, its CRLF CRLF included (decision
/// CH69 rule 9).
pub const HEAD_CAP: usize = 8 * 1024;

/// The bound of the wait for a whole request head (decision CH69 rule 9,
/// as decision CH40 rule 2).
pub const HEAD_WAIT: Duration = Duration::from_secs(30);

/// The most open links of the listener of one VM (decision CH69 rule 14,
/// as decision CH68 rule 1 for 1100).
pub const LINK_CAP: usize = SPLICE_CAP;

/// The most fds of one link beside its guest stream: the family dir and
/// `egress.json` of the read, a resolver socket of the lookup and the
/// upstream socket. The steps run one after another, so the sum bounds
/// the fds at once with room for a resolver that opens one more.
const LINK_FDS: u64 = 4;

/// The fds that the listeners of all `SLOTS` need when each one holds
/// `LINK_CAP` links: per VM the listener, and per link its stream and
/// `LINK_FDS`. The one log file sits in the headroom of `planes::fd_need`.
/// `vmm serve` adds it to the raise of decision CH67 rule 4.
// Decision CH69 rule 14: this need (20544) adds to the raise of 49344
// (decision CH68 rule 2), so the raise is 69888. The plan estimate was
// 16448: it counted one fd for the allowlist read, which holds two.
#[must_use]
pub fn fd_need() -> u64 {
    let links = u64::try_from(LINK_CAP).unwrap_or(u64::MAX);
    links
        .saturating_mul(LINK_FDS.saturating_add(1))
        .saturating_add(1)
        .saturating_mul(u64::from(SLOTS))
}

/// The answer to an allowed CONNECT.
const ESTABLISHED: &[u8] = b"HTTP/1.1 200 Connection established\r\n\r\n";

/// The listener of one VM, bound and not yet served. A dial that the guest
/// makes before `serve` waits in the backlog. The drop closes it.
#[derive(Debug)]
pub struct Bound(StdListener);

/// Binds `vsock.sock_1104` in `run` (decision CH24). It blocks, so the
/// start runs it in `spawn_blocking`. The slot is empty, so a path that is
/// there fails the bind.
///
/// # Errors
///
/// A `BindError` when the listener did not bind.
pub fn bind(run: &VmRunDir) -> Result<Bound, BindError> {
    let path = run.host_listener(HostPort::Egress);
    StdListener::bind(&path)
        .and_then(|listener| listener.set_nonblocking(true).map(|()| listener))
        .map(Bound)
        .map_err(|source| BindError { path, source })
}

/// What each link of one VM reads and names: the families dir and the
/// family, for the allowlist, the session and the log, for the line.
#[derive(Debug)]
struct Proxy {
    families: FamilyRoot,
    family: FamilyName,
    session: SessionId,
    log: Arc<Log>,
}

impl Bound {
    /// Serves the listener in a task of its own. Each CONNECT reads the
    /// allowlist of `family` in `families`. Each link writes one line to
    /// `log` that names `session`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take.
    pub fn serve(
        self,
        families: FamilyRoot,
        family: FamilyName,
        session: SessionId,
        log: Arc<Log>,
    ) -> io::Result<Server> {
        let proxy = Arc::new(Proxy {
            families,
            family,
            session,
            log,
        });
        let links = Links::default();
        Draining { proxy, links }.reopen(self)
    }
}

/// The served listener of one VM. Its drop ends the listener task and
/// cuts its open links.
#[derive(Debug)]
pub struct Server {
    /// The accept task. Its drop aborts it.
    accept: Accept,
    /// The open links and what a link reads.
    draining: Draining,
}

impl Server {
    /// Closes the listener and keeps each open link (decision CH74 rule
    /// 2). When it returns, the listener is closed, and the caller removes
    /// its socket file.
    pub async fn close(self) -> Draining {
        self.accept.end().await;
        self.draining
    }
}

/// The open links of a closed listener of one VM. Each one runs to its
/// own end. The drop cuts each link that still runs.
#[derive(Debug)]
pub struct Draining {
    proxy: Arc<Proxy>,
    links: Links,
}

impl Draining {
    /// Serves `bound`, a new listener at the path of the closed one, with
    /// the links of the closed one. They count against `LINK_CAP`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take.
    pub fn reopen(self, bound: Bound) -> io::Result<Server> {
        let listener = UnixListener::from_std(bound.0)?;
        let task = accept(listener, Arc::clone(&self.proxy), self.links.clone());
        Ok(Server {
            accept: Accept::spawn(task),
            draining: self,
        })
    }
}

/// Accepts on `listener` until its task ends, and serves each link in
/// `links`.
// Decision CH69 rule 30: as on vmm.sock (decision CH40 rule 1) and on
// 1100 to 1102, a failed accept, such as EMFILE, is logged by kind, and
// the loop pauses ACCEPT_PAUSE and goes on. CH67 rule 11 binds only the
// guest rows of guest init.
async fn accept(listener: UnixListener, proxy: Arc<Proxy>, links: Links) {
    loop {
        match listener.accept().await {
            Ok((guest, _)) => {
                if !links.admit(LINK_CAP, serve_link(guest, Arc::clone(&proxy))) {
                    // Decision CH67 rule 3: a link past the cap closes at
                    // once. Its line is the one line of the link (decision
                    // CH6-4).
                    Link::new(Arc::clone(&proxy)).refuse(ProxyReason::LinkCap.into());
                }
            }
            Err(error) => {
                eprintln!("vmm: an egress accept failed: {}", error.kind());
                sleep(ACCEPT_PAUSE).await;
            }
        }
    }
}

/// Serves one link. The drop of `link` writes its line, also when the
/// drop of `Server` ends the task first.
async fn serve_link(guest: UnixStream, proxy: Arc<Proxy>) {
    let mut link = Link::new(proxy);
    link.run(guest).await;
}

/// One link and what its log line says. Its drop writes the line.
#[derive(Debug)]
struct Link {
    proxy: Arc<Proxy>,
    accepted: Instant,
    target: Option<Target>,
    reason: Option<Reason>,
    address: Option<IpAddr>,
    /// The splice of the tunnel, after the 200.
    splice: Option<Splice>,
}

/// A head that came whole: its bytes before the CRLF CRLF, and the bytes
/// after it, which the guest sent before the 200.
#[derive(Debug)]
struct Head {
    head: Vec<u8>,
    rest: Vec<u8>,
}

/// Why a request head did not pass.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Refused {
    /// A head that does not read as a request (400).
    Bad,
    /// A method other than CONNECT (405).
    NotConnect,
}

impl Link {
    fn new(proxy: Arc<Proxy>) -> Self {
        Self {
            proxy,
            accepted: Instant::now(),
            target: None,
            reason: None,
            address: None,
            splice: None,
        }
    }

    /// Records the refusal `reason`.
    fn refuse(&mut self, reason: Reason) {
        self.reason = Some(reason);
    }

    /// Reads the head, decides, connects, answers and carries the tunnel.
    async fn run(&mut self, mut guest: UnixStream) {
        let head = match timeout(HEAD_WAIT, read_head(&mut guest)).await {
            Err(_) => return self.refuse(ProxyReason::HeadTimeout.into()),
            Ok(None) => {
                return self
                    .answer(&mut guest, 400, ProxyReason::BadHead.into())
                    .await;
            }
            Ok(Some(head)) => head,
        };
        let text = match request_target(&head.head) {
            Ok(text) => text,
            Err(Refused::Bad) => {
                return self
                    .answer(&mut guest, 400, ProxyReason::BadHead.into())
                    .await;
            }
            Err(Refused::NotConnect) => {
                return self
                    .answer(&mut guest, 405, ProxyReason::NotConnect.into())
                    .await;
            }
        };
        let target = match Target::parse(text) {
            Ok(target) => self.target.insert(target).clone(),
            Err(reason) => return self.answer(&mut guest, status(reason), reason.into()).await,
        };
        let proxy = Arc::clone(&self.proxy);
        // Decision CH70 rule 2: a list that does not read allows nothing,
        // and the line names `not_valid`, as `check` answers it.
        let reply = match read_list(&proxy.families, &proxy.family).await {
            None => return,
            Some(Ok(list)) => decide(&list, &target).await,
            Some(Err(_)) => return self.refused(&mut guest, CheckReason::NotValid).await,
        };
        let addresses = match reply {
            CheckReply::Allowed(allowed) => allowed.addresses,
            CheckReply::Refused(refused) => return self.refused(&mut guest, refused.reason).await,
        };
        // The wire text of each address is the text of its `IpAddr`
        // (decision CH70 rule 3), so each one reads back.
        let addresses: Vec<IpAddr> = addresses
            .into_iter()
            .filter_map(|address| String::from(address).parse().ok())
            .collect();
        let port = target.port().get();
        let (mut upstream, address) = match timeout(CONNECT_WAIT, connect(&addresses, port)).await {
            Err(_) => {
                return self
                    .answer(&mut guest, 504, ProxyReason::ConnectTimeout.into())
                    .await;
            }
            Ok(None) => {
                return self
                    .answer(&mut guest, 502, ProxyReason::ConnectFailed.into())
                    .await;
            }
            Ok(Some(connected)) => connected,
        };
        self.address = Some(address);
        if write_all(&guest, ESTABLISHED).await.is_err() {
            return;
        }
        // The near stream is the upstream, so the bytes that the guest
        // sent after its head reach the upstream first.
        let splice = self.splice.insert(Splice::new(head.rest));
        poll_fn(|context| splice.carry(context, &mut upstream, &mut guest)).await;
    }

    /// Answers the refusal of the rule `reason` with its status.
    async fn refused(&mut self, guest: &mut UnixStream, reason: CheckReason) {
        self.answer(guest, status(reason), reason.into()).await;
    }

    /// Writes the error status `code` with no body, and records `reason`.
    /// The drop of the link then closes it.
    async fn answer(&mut self, guest: &mut UnixStream, code: u16, reason: Reason) {
        self.refuse(reason);
        let (text, allow) = match code {
            400 => ("Bad Request", ""),
            403 => ("Forbidden", ""),
            405 => ("Method Not Allowed", "Allow: CONNECT\r\n"),
            502 => ("Bad Gateway", ""),
            _ => ("Gateway Timeout", ""),
        };
        let reply = format!(
            "HTTP/1.1 {code} {text}\r\n{allow}Connection: close\r\nContent-Length: 0\r\n\r\n"
        );
        // A guest that is gone gets no reply, and its line still goes out.
        let _ = write_all(guest, reply.as_bytes()).await;
    }

    /// The log line of the link as it stands.
    fn line(&self) -> Line {
        let (down, up) = self.splice.as_ref().map_or((0, 0), Splice::carried);
        let millis = self.accepted.elapsed().as_millis();
        Line {
            time: rfc3339::format(SystemTime::now()),
            family: self.proxy.family.clone(),
            session: self.proxy.session.clone(),
            host: self.target.as_ref().map(|target| target.host().clone()),
            port: self.target.as_ref().map(Target::port),
            verdict: if self.splice.is_some() {
                Verdict::Allowed
            } else {
                Verdict::Refused
            },
            reason: self.reason,
            address: self.address,
            bytes_up: up,
            bytes_down: down,
            duration_ms: u64::try_from(millis).unwrap_or(u64::MAX),
        }
    }
}

impl Drop for Link {
    /// Writes the one line of the link in `spawn_blocking`. A link lives
    /// only in a task of the runtime, so the runtime is there. At the end
    /// of the runtime the write does not run, and the line is lost, as at
    /// a crash of the vmm (Q61-10, decision CH69 rule 10).
    fn drop(&mut self) {
        let (line, log) = (self.line(), Arc::clone(&self.proxy.log));
        if let Ok(runtime) = Handle::try_current() {
            drop(runtime.spawn_blocking(move || log.write(&line)));
        }
    }
}

impl From<CheckReason> for Reason {
    fn from(reason: CheckReason) -> Self {
        Self::Check(reason)
    }
}

impl From<ProxyReason> for Reason {
    fn from(reason: ProxyReason) -> Self {
        Self::Proxy(reason)
    }
}

/// The status of the refusal `reason` of the rule (decision CH69 rule 9):
/// 400 for a target that does not read, 502 for a failed lookup, else 403.
fn status(reason: CheckReason) -> u16 {
    match reason {
        CheckReason::BadTarget => 400,
        CheckReason::NoAddress => 502,
        CheckReason::IpLiteral
        | CheckReason::NotListed
        | CheckReason::NotPublic
        | CheckReason::NotValid => 403,
    }
}

/// Reads a head of at most `HEAD_CAP` bytes. `None` for a head past the
/// cap, a failed read, or an EOF before the CRLF CRLF.
async fn read_head(guest: &mut UnixStream) -> Option<Head> {
    let mut buffer = vec![0; HEAD_CAP];
    let mut filled = 0;
    loop {
        let free = buffer.get_mut(filled..).filter(|free| !free.is_empty())?;
        let read = read_some(guest, free).await.ok().filter(|&read| read > 0)?;
        let searched = filled.saturating_sub(3);
        filled = filled.saturating_add(read);
        let window = buffer.get(searched..filled)?;
        if let Some(at) = window.windows(4).position(|four| four == b"\r\n\r\n") {
            let end = searched.saturating_add(at);
            let head = buffer.get(..end)?.to_vec();
            let rest = buffer.get(end.saturating_add(4)..filled)?.to_vec();
            return Some(Head { head, rest });
        }
    }
}

/// The target of the request line of `head`, which must be `CONNECT
/// <target> HTTP/1.1` or `HTTP/1.0` with single spaces.
fn request_target(head: &[u8]) -> Result<&str, Refused> {
    let head = std::str::from_utf8(head).map_err(|_| Refused::Bad)?;
    let line = head.split("\r\n").next().unwrap_or(head);
    let mut parts = line.split(' ');
    let (Some(method), Some(target), Some(version), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Err(Refused::Bad);
    };
    let token = |text: &str| !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_graphic());
    if !token(method) || !token(target) || !matches!(version, "HTTP/1.1" | "HTTP/1.0") {
        return Err(Refused::Bad);
    }
    if method != "CONNECT" {
        return Err(Refused::NotConnect);
    }
    Ok(target)
}

/// Reads at least one byte of `guest` into `buffer`, or 0 at an EOF. The
/// mirror of host port 1103 reads its first pkt-line with it too.
pub(crate) async fn read_some(guest: &UnixStream, buffer: &mut [u8]) -> io::Result<usize> {
    loop {
        guest.readable().await?;
        match guest.try_read(buffer) {
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
            read => return read,
        }
    }
}

/// Writes all of `bytes` to `guest`, as `ControlLink::write_raw` does.
pub(crate) async fn write_all(guest: &UnixStream, bytes: &[u8]) -> io::Result<()> {
    let mut left = bytes;
    while !left.is_empty() {
        guest.writable().await?;
        match guest.try_write(left) {
            Ok(written) => left = left.get(written..).unwrap_or_default(),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

/// Connects to the first of `addresses` that takes a connect on `port`.
/// The caller bounds the whole try.
async fn connect(addresses: &[IpAddr], port: u16) -> Option<(TcpStream, IpAddr)> {
    for &address in addresses {
        if let Ok(stream) = TcpStream::connect((address, port)).await {
            return Some((stream, address));
        }
    }
    None
}
