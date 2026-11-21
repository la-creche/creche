//! One control connection of the runner daemon: one session (the runner
//! design at commit 131ce43, sections 6.2 to 6.4). Linux only.
//!
//! 1. `check_daemon`: the peer of the connection runs as the user
//!    `ci-runner`, before the signer reads a byte (section 6.3).
//! 2. The `open` line, the checks of section 4.4 and the answer, all
//!    within `ANSWER_BOUND` of the accept. The daemon waits 10 s from its
//!    connect to the answer (section 6.2, rule 1). A refusal, a fault or a
//!    timeout answers `accept:false` when the bound still allows it, and
//!    ends the session.
//! 3. After `accept`, each `conn` line of this session and port comes with
//!    one fd. The signer checks the fd with `ci_runner_peer::check` and the
//!    slot of the `open` line before it reads a byte, and requires the
//!    cgroup of the first accepted fd for each later fd (the README of
//!    `ci-runner-peer`, items 3 and 4). Only then does `guest::serve` read
//!    the fd, in a task of the session.
//! 4. The session ends at its `close` line, at the end of the control
//!    connection, at the exit of the first accepted peer (its pidfd polls
//!    readable), at `SESSION_BOUND`, and at each fault: a line that breaks
//!    a rule, a `conn` or `close` line of another session or port, a `conn`
//!    line with no fd, a refused peer, more than `CONN_MAX` fds. At the
//!    end the session aborts its guest tasks and drops each fd it holds.
//!
//! The session is keyed by its control connection, never by the `session`
//! of the line (section 6.4, item 8). The signer writes one line on the
//! connection, its answer, and no byte after it.

use std::collections::VecDeque;
use std::fmt;
use std::future::Future;
use std::io::{self, IoSliceMut};
use std::mem::MaybeUninit;
use std::os::fd::{AsFd, OwnedFd};
use std::pin::pin;
use std::sync::Arc;
use std::task::Poll;
use std::time::Duration;

use ci_runner_peer::{CgroupId, Slot, check, check_daemon};
use creche_ci_signer::eligibility::Eligible;
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::net::{RecvAncillaryBuffer, RecvAncillaryMessage, RecvFlags, ReturnFlags};
use tokio::io::Interest;
use tokio::net::UnixStream;
use tokio::task::JoinSet;
use tokio::time::{Instant, timeout, timeout_at};

use crate::guest;
use crate::line::{self, ACCEPT, Control, LINE_MAX, LineFault, Lines, Open, REFUSE};
use crate::log;
use crate::signer::Signer;

/// The bound from the accept of the control connection to the end of the
/// answer: the bound of a session of the daemon, 10 s, less a margin.
pub const ANSWER_BOUND: Duration = Duration::from_secs(9);

/// The longest session after `accept`: the wall clock of a kind of the
/// runner policy, 180 minutes (the runner design, section 5), and a margin.
pub const SESSION_BOUND: Duration = Duration::from_secs(4 * 60 * 60);

/// The most fds of one session. The daemon passes at most 8 for each port
/// (section 6.2, step 4), and a session has one port.
pub const CONN_MAX: usize = 8;

/// The time between two polls of the pidfd of the first accepted peer.
/// So a session ends at most this long after the exit of its peer.
pub const PEER_POLL: Duration = Duration::from_secs(1);

/// Why a session ended.
#[derive(Debug)]
pub enum End {
    /// The control connection ended.
    Eof,
    /// A read of the control connection failed.
    Read,
    /// A control line broke a rule.
    Line(LineFault),
    /// The `open` line or the answer did not come within `ANSWER_BOUND`.
    OpenTimeout,
    /// The signer refused the run. The word of the refusal.
    Declined(&'static str),
    /// The answer could not be written.
    Answer,
    /// The `close` line of the session.
    Closed,
    /// A `conn` or `close` line named another session or port.
    OtherSession,
    /// A `conn` line came with no fd, or an fd came with no `conn` line.
    NoFd,
    /// More than `CONN_MAX` fds.
    TooManyConns,
    /// The peer check refused an fd.
    Peer,
    /// An fd came from another unit than the first fd of the session.
    OtherUnit,
    /// The first accepted peer exited.
    PeerExited,
    /// The session passed `SESSION_BOUND`.
    Bound,
}

impl fmt::Display for End {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Line(fault) => write!(formatter, "line {}", fault.code()),
            Self::Declined(code) => write!(formatter, "declined {code}"),
            other => write!(formatter, "{other:?}"),
        }
    }
}

/// The lines and the fds of one control connection.
struct Reader {
    stream: UnixStream,
    lines: Lines,
    fds: VecDeque<OwnedFd>,
}

impl Reader {
    /// The next line of the daemon, with no LF. Each fd that comes with
    /// the bytes joins `fds`.
    async fn line(&mut self) -> Result<Vec<u8>, End> {
        loop {
            if let Some(line) = self.lines.next_line().map_err(End::Line)? {
                return Ok(line);
            }
            let mut bytes = [0_u8; LINE_MAX];
            let stream = &self.stream;
            let (got, fds) = stream
                .async_io(Interest::READABLE, || receive(stream, &mut bytes))
                .await
                .map_err(|_| End::Read)?;
            self.fds.extend(fds);
            if got == 0 {
                return Err(End::Eof);
            }
            self.lines.push(bytes.get(..got).unwrap_or_default());
        }
    }
}

/// One `recvmsg` of `stream`: the count of bytes and the fds that came
/// with them. A cut of the control data is a fault: an fd would be lost.
fn receive(stream: &UnixStream, bytes: &mut [u8]) -> io::Result<(usize, Vec<OwnedFd>)> {
    let mut space = [MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(1))];
    let mut control = RecvAncillaryBuffer::new(&mut space);
    let got = rustix::net::recvmsg(
        stream,
        &mut [IoSliceMut::new(bytes)],
        &mut control,
        RecvFlags::CMSG_CLOEXEC,
    )?;
    let mut fds = Vec::new();
    for message in control.drain() {
        if let RecvAncillaryMessage::ScmRights(passed) = message {
            fds.extend(passed);
        }
    }
    if got.flags.contains(ReturnFlags::CTRUNC) {
        return Err(io::Error::other("the control data of a line was cut"));
    }
    Ok((got.bytes, fds))
}

/// Writes all of `bytes` on `stream`.
async fn write_all(stream: &UnixStream, mut bytes: &[u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        stream.writable().await?;
        match stream.try_write(bytes) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(count) => bytes = bytes.get(count..).unwrap_or_default(),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

/// Which of two futures ended first.
enum First<A, B> {
    Left(A),
    Right(B),
}

/// Runs `left` and `right` together, and gives the output of the first one
/// that ends. The other one is dropped.
async fn first<A, B>(left: impl Future<Output = A>, right: impl Future<Output = B>) -> First<A, B> {
    let mut left = pin!(left);
    let mut right = pin!(right);
    std::future::poll_fn(|context| {
        if let Poll::Ready(value) = left.as_mut().poll(context) {
            return Poll::Ready(First::Left(value));
        }
        if let Poll::Ready(value) = right.as_mut().poll(context) {
            return Poll::Ready(First::Right(value));
        }
        Poll::Pending
    })
    .await
}

/// Whether the pidfd `pidfd` polls readable with no wait: the peer exited
/// (the runner design, section 6.3, step 3). A poll that fails counts as
/// an exit, so the session ends (fail closed).
fn has_exited(pidfd: &OwnedFd) -> bool {
    let mut fds = [PollFd::new(pidfd, PollFlags::IN)];
    let now = Timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    match poll(&mut fds, Some(&now)) {
        Ok(ready) => ready != 0 || fds.iter().any(|fd| !fd.revents().is_empty()),
        Err(_) => true,
    }
}

/// Waits until the pidfd of `watch` polls readable, with one poll each
/// `PEER_POLL`. With no peer yet, it waits for ever. tokio offers no safe
/// registration of a raw fd (its `AsyncFd` constructors are `unsafe` or
/// deprecated), so the wait is a poll on a timer.
async fn exited(watch: Option<&OwnedFd>) {
    let Some(pidfd) = watch else {
        return std::future::pending::<()>().await;
    };
    loop {
        tokio::time::sleep(PEER_POLL).await;
        if has_exited(pidfd) {
            return;
        }
    }
}

/// Serves one control connection of the listener, from the check of the
/// daemon to the end of the session.
pub async fn run(control: UnixStream, signer: Arc<Signer>) {
    if let Err(refusal) = check_daemon(control.as_fd()) {
        log(format_args!("control: refused: {refusal}"));
        return;
    }
    let end = serve(control, &signer).await;
    log(format_args!("session end: {end}"));
}

/// The session of a control connection whose daemon check passed: the
/// steps 2 to 4 of the module docs. It gives the reason of the end.
pub async fn serve(control: UnixStream, signer: &Arc<Signer>) -> End {
    let start = Instant::now();
    let mut reader = Reader {
        stream: control,
        lines: Lines::default(),
        fds: VecDeque::new(),
    };

    let decided = timeout_at(start + ANSWER_BOUND, async {
        let bytes = reader.line().await?;
        let open = line::open(&bytes).map_err(End::Line)?;
        log(format_args!("open {open}"));
        match signer.decide(open.key()).await {
            Ok(eligible) => Ok((open, eligible)),
            Err(decline) => {
                log(format_args!("refuse session={}: {decline}", open.session()));
                Err(End::Declined(decline.code()))
            }
        }
    })
    .await
    .unwrap_or(Err(End::OpenTimeout));

    let (open, eligible) = match decided {
        Ok(found) => found,
        Err(end) => {
            let answer = timeout_at(start + ANSWER_BOUND, write_all(&reader.stream, REFUSE));
            let _sent = answer.await;
            return end;
        }
    };
    let sent = timeout_at(start + ANSWER_BOUND, write_all(&reader.stream, ACCEPT)).await;
    if !matches!(sent, Ok(Ok(()))) {
        return End::Answer;
    }
    log(format_args!("accept session={}", open.session()));

    let mut guests = JoinSet::new();
    let end = timeout(
        SESSION_BOUND,
        accepted(&mut reader, &open, eligible, signer, &mut guests),
    )
    .await
    .unwrap_or(End::Bound);
    guests.abort_all();
    end
}

/// The state of an accepted session.
struct Held {
    eligible: Option<Eligible>,
    first: Option<CgroupId>,
    watch: Option<OwnedFd>,
    conns: usize,
}

/// The lines of an accepted session, until its end.
async fn accepted(
    reader: &mut Reader,
    open: &Open,
    eligible: Eligible,
    signer: &Arc<Signer>,
    guests: &mut JoinSet<()>,
) -> End {
    let mut held = Held {
        eligible: Some(eligible),
        first: None,
        watch: None,
        conns: 0,
    };
    loop {
        let event = first(reader.line(), exited(held.watch.as_ref())).await;
        let bytes = match event {
            First::Left(Ok(bytes)) => bytes,
            First::Left(Err(end)) => return end,
            First::Right(()) => return End::PeerExited,
        };
        let control = match line::control(&bytes) {
            Ok(control) => control,
            Err(fault) => return End::Line(fault),
        };
        match control {
            Control::Conn { session, port }
                if session == *open.session() && port == open.port() =>
            {
                let Some(fd) = reader.fds.pop_front() else {
                    return End::NoFd;
                };
                if let Err(end) = conn(open, fd, &mut held, signer, guests) {
                    return end;
                }
            }
            Control::Close { session } if session == *open.session() => {
                if !reader.fds.is_empty() {
                    return End::NoFd;
                }
                return End::Closed;
            }
            Control::Conn { .. } | Control::Close { .. } => return End::OtherSession,
        }
    }
}

/// One passed fd: the cap, the peer check against the slot of the session,
/// the unit of the first fd, then the guest exchange in a task.
fn conn(
    open: &Open,
    fd: OwnedFd,
    held: &mut Held,
    signer: &Arc<Signer>,
    guests: &mut JoinSet<()>,
) -> Result<(), End> {
    held.conns = held.conns.saturating_add(1);
    if held.conns > CONN_MAX {
        return Err(End::TooManyConns);
    }
    let slot = Slot::new(open.slot());
    let peer = match check(fd.as_fd(), slot) {
        Ok(peer) => peer,
        Err(refusal) => {
            log(format_args!(
                "conn session={}: slot {slot}: refused: {refusal}",
                open.session()
            ));
            return Err(End::Peer);
        }
    };
    let id = peer.cgroup_id();
    if held.first.is_some_and(|first| first != id) {
        return Err(End::OtherUnit);
    }
    held.first = Some(id);
    log(format_args!(
        "conn session={}: slot {slot}: accepted {}",
        open.session(),
        peer.unit()
    ));
    if held.watch.is_none() {
        held.watch = Some(peer.into_pidfd());
    }

    let stream = std::os::unix::net::UnixStream::from(fd);
    stream.set_nonblocking(true).map_err(|_| End::Peer)?;
    let guest = UnixStream::from_std(stream).map_err(|_| End::Peer)?;
    guests.spawn(guest::serve(
        guest,
        held.eligible.take(),
        Arc::clone(signer),
    ));
    Ok(())
}
