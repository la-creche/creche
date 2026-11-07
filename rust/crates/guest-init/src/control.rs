//! Control link 1200: the guest end of the guest init handshake
//! (decisions CH13, CH14, CH28, CH32).
//!
//! The vmm dials, and guest init listens on AF_VSOCK port 1200 of any CID
//! (decision CH13). The framing is "The vsock JSON links" of
//! `wire/AGENTS.md` (decision CH36 G11): one compact JSON value per LF
//! line, at most `CONTROL_LINE_CAP` bytes.
//!
//! 1. Each accepted link gets a new `LinkId`. A new link replaces the old
//!    one, and guest init closes the old one (decision CH19 rule 2).
//! 2. A line or an EOF of a replaced link changes nothing (decision CH28,
//!    fact 74: the EOF of the old link can follow the new accept).
//! 3. The vmm sends `ControlHello` first. Guest init sets
//!    `CLOCK_REALTIME` to `sent_at`, on every hello, a backward step
//!    included (decision CH62 rule 25). Then it answers one
//!    `ControlReady` with the facade state (decisions CH28, CH34 rule 7).
//!    Guest init sends nothing before the hello.
//! 4. A failed clock set closes the link before `ControlReady` (decision
//!    CH62 rule 25). So does a hello that names a session other than the
//!    first hello of the boot (decision CH32 rule 3). The vmm then
//!    cold-boots the VM (decision CH43 rule 6).
//! 5. A refused line closes the link with no error line, and guest init
//!    writes why on the console (decisions CH43 rules 1 and 2, CH62 rule
//!    26). A line is refused when it is over the cap, is not one JSON
//!    value, or is not the type that the link expects at that point.
//! 6. After `ControlReady`, `ControlCommand::Stop` runs the stop of the
//!    supervisor: the stop line on the local link, up to 25 s, SIGTERM to
//!    the facade group, up to 2 s, then the power-off (decisions CH19
//!    rule 3, CH62 rule 9).
//! 7. After `ControlReady`, `ControlCommand::Sync` runs `syncfs` of the
//!    session disk, then `sync`, and guest init answers
//!    `GuestEvent::Synced` on the same link (decision CH62 rule 23). A
//!    sync of a replaced link is a stale line and changes nothing.
//! 8. The clock set of the first hello starts the facade, and the ready
//!    of that hello reports it (decisions CH21-8, CH25 rule 2). The
//!    facade lives for the VM life: a link close never touches it
//!    (decision CH19 rule 1). `supervisor` has the rules.
//! 9. After `ControlReady`, each facade state change goes out on the link
//!    as `GuestEvent::Facade` (decision CH43 rule 8). A change before the
//!    ready of a link goes out in that ready.
//! 10. The local link of the facade feeds the turn events, and each goes
//!     out on the link after its ready as `GuestEvent::Turn`, then the
//!     facade state that it changed (decisions CH21, CH62 rules 10 and
//!     11). `local_link` has the rules.
//!
//! `Control` is the pure state machine: inputs in, actions out, so the
//! unit tests drive it on any OS. `Lines` is the pure framing. The
//! Linux shell, `serve`, runs the vsock listener and std threads
//! (decisions CH62 rules 1 and 2).

use std::fmt;
use std::num::NonZeroU32;
use std::time::Duration;

use creche_wire::{
    CONTROL_LINE_CAP, ControlCommand, ControlHello, ControlReady, ControlSynced, FacadeState,
    GuestEvent, SessionId, TurnEvent, UnixMillis,
};
use rustix::io::Errno;

use crate::local_link::LocalId;
use crate::supervisor::{Order, Supervisor};

/// The identity of one control link (decision CH28). Each accepted link
/// gets a new one, in accept order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct LinkId(u64);

impl fmt::Display for LinkId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "control link {}", self.0)
    }
}

/// What the shell does next.
#[derive(Debug, PartialEq, Eq)]
pub enum Action {
    /// Set `CLOCK_REALTIME`, then call `Control::clock_set` with the
    /// result (decision CH28).
    SetClock(LinkId, UnixMillis),
    /// Write this `ControlReady` line, its LF included, to the link.
    Ready(LinkId, Vec<u8>),
    /// Run `syncfs` of the session disk, then `sync`, then call
    /// `Control::synced` (decision CH62 rule 23).
    Sync(LinkId),
    /// Write this `GuestEvent::Synced` line, its LF included, to the link.
    Synced(LinkId, Vec<u8>),
    /// Start the facade for this session, then call `Control::started` or
    /// `Control::start_failed` (decision CH62 rule 6).
    Start(SessionId),
    /// Write this `GuestEvent::Facade` line, its LF included, to the link
    /// (decision CH43 rule 8).
    Facade(LinkId, Vec<u8>),
    /// Run a timer of this length, then call `Control::timer`. A new timer
    /// replaces the one that runs.
    Timer(Duration),
    /// Send SIGTERM to the process group of the facade (decision CH62
    /// rule 9).
    Terminate(NonZeroU32),
    /// Send SIGKILL to the process group of the facade: its local link
    /// closed (decision CH32 rule 7), or it exited.
    Kill(NonZeroU32),
    /// Close the link and write why on the console (decision CH43 rule 2).
    Close(LinkId, Why),
    /// Write this `GuestEvent::Turn` line, its LF included, to the link
    /// (decision CH21).
    Turn(LinkId, Vec<u8>),
    /// Write on the console that the turn event of this local link is
    /// dropped: no control link got its ready (decision CH62 rule 11).
    Dropped(LocalId),
    /// Write `local_link::STOP_LINE` to the local link (decision CH62
    /// rule 9). A failed write calls `Control::local_ended`.
    LocalStop(LocalId),
    /// Close the local link and write why on the console.
    LocalClose(LocalId, Why),
    /// Write why on the console and change nothing.
    Ignore(LinkId, Ignored),
    /// The vmm sent `stop` on the link. Write it on the console, then call
    /// `Control::stop` (decision CH19 rule 3).
    Stop(LinkId),
    /// Power the VM off. The power-off sends SIGKILL to every process left
    /// first (decision CH62 rule 9).
    PowerOff,
}

/// Why guest init closes a link.
#[derive(Debug, PartialEq, Eq)]
pub enum Why {
    /// A newer link replaced it (decision CH19 rule 2).
    Replaced,
    /// A line is over `CONTROL_LINE_CAP` (decision CH43 rule 1).
    TooLong,
    /// The link ended inside a line: bytes with no LF before the EOF.
    CutLine,
    /// A line is not the type that the link expects at that point. It
    /// holds the error of the reader.
    Refused(String),
    /// A line came while the clock of the hello was not set yet.
    Early,
    /// The hello names a session other than the first hello (decision
    /// CH32 rule 3).
    OtherSession(SessionId),
    /// `clock_settime` failed (decision CH62 rule 25).
    Clock(Errno),
    /// A line of guest init did not encode. It holds the error of the
    /// writer.
    Encode(String),
    /// The local link peer is not the live facade that waits for its
    /// link (decision CH62 rule 10). It holds the peer pid.
    NotFacade(NonZeroU32),
    /// The peer of a local link ended it, or its read failed.
    Ended,
    /// The facade of a local link exited.
    FacadeExited,
}

impl fmt::Display for Why {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Replaced => formatter.write_str("a newer link replaced it"),
            Self::TooLong => write!(formatter, "a line is over {CONTROL_LINE_CAP} bytes"),
            Self::CutLine => formatter.write_str("the link ended inside a line"),
            Self::Refused(error) => write!(formatter, "a refused line: {error}"),
            Self::Early => formatter.write_str("a line came before the ready"),
            Self::OtherSession(session) => {
                write!(formatter, "the hello names another session: {session}")
            }
            Self::Clock(error) => write!(formatter, "the clock set failed: {error}"),
            Self::Encode(error) => write!(formatter, "a line did not encode: {error}"),
            Self::NotFacade(pid) => {
                write!(
                    formatter,
                    "pid {pid} is not a facade that waits for its link"
                )
            }
            Self::Ended => formatter.write_str("the link ended"),
            Self::FacadeExited => formatter.write_str("the facade exited"),
        }
    }
}

/// Why guest init changes nothing for a valid line.
#[derive(Debug, PartialEq, Eq)]
pub enum Ignored {
    /// The line came on a link that is no longer the current one: a newer
    /// link replaced it, or guest init closed it (decision CH28).
    Stale,
}

impl fmt::Display for Ignored {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Stale => "a line of a closed or replaced link is ignored",
        })
    }
}

/// Where the current link is in the handshake.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Stage {
    /// The link waits for `ControlHello`.
    Hello,
    /// The shell sets the clock of the hello.
    Clock,
    /// `ControlReady` went out. The link carries `ControlCommand`.
    Ready,
}

/// The control state of the VM: the current link, the session of the
/// first hello and the facade.
#[derive(Debug)]
pub struct Control {
    /// The id of the last accepted link.
    accepted: u64,
    /// The current link, or `None` after it closed.
    current: Option<(LinkId, Stage)>,
    /// The session of the first hello of the boot (decision CH32 rule 3).
    session: Option<SessionId>,
    /// The facade for the VM life (decision CH19 rule 1).
    facade: Supervisor,
    /// The facade state after the last input, so a change is seen
    /// (decision CH43 rule 8).
    reported: FacadeState,
    /// The id of the last accepted local link.
    local_accepted: u64,
    /// The open local link of the facade (decision CH62 rule 10).
    local: Option<LocalId>,
}

impl Default for Control {
    /// The state at boot: no link, no session and no facade.
    fn default() -> Self {
        let facade = Supervisor::default();
        let reported = facade.state();
        Self {
            accepted: 0,
            current: None,
            session: None,
            facade,
            reported,
            local_accepted: 0,
            local: None,
        }
    }
}

impl Control {
    /// A new link was accepted. It becomes the current link, and the old
    /// one, if any, closes (decision CH19 rule 2).
    pub fn accept(&mut self) -> (LinkId, Option<Action>) {
        self.accepted = self.accepted.saturating_add(1);
        let link = LinkId(self.accepted);
        let old = self.current.replace((link, Stage::Hello));
        (link, old.map(|(old, _)| Action::Close(old, Why::Replaced)))
    }

    /// A line of `link`, its LF cut.
    // CONTRACT-QUESTION: CH65 rule 2 decides it. One wire lane PR moves
    // the check `strict` of the vmm crate into creche-wire, and both ends
    // read each line through it. Until that PR merges, the typed serde
    // reader is the strict reader here. Each type of this link denies
    // unknown fields and holds no free JSON value, so it refuses a key
    // given twice, a second value, a float for an integer and an array
    // for an object, as `strict` does. A unit test holds that. A later
    // commit on this branch calls the shared check once the wire PR
    // merges.
    pub fn line(&mut self, link: LinkId, line: &[u8]) -> Option<Action> {
        let Some(stage) = self.stage(link) else {
            return Some(Action::Ignore(link, Ignored::Stale));
        };
        match stage {
            Stage::Hello => match serde_json::from_slice::<ControlHello>(line) {
                Ok(hello) => Some(self.hello(link, hello)),
                Err(error) => Some(self.close(link, Why::Refused(error.to_string()))),
            },
            Stage::Clock => Some(self.close(link, Why::Early)),
            Stage::Ready => match serde_json::from_slice::<ControlCommand>(line) {
                Ok(ControlCommand::Stop(_)) => Some(Action::Stop(link)),
                Ok(ControlCommand::Sync(_)) => Some(Action::Sync(link)),
                Err(error) => Some(self.close(link, Why::Refused(error.to_string()))),
            },
        }
    }

    /// The reader of `link` refused its bytes: `TooLong` or `CutLine`.
    pub fn refused(&mut self, link: LinkId, why: Why) -> Option<Action> {
        self.stage(link)?;
        Some(self.close(link, why))
    }

    /// The link ended at a line end, or its read failed. The shell drops
    /// it. An end of a replaced link changes nothing (decision CH28).
    pub fn ended(&mut self, link: LinkId) {
        if self.stage(link).is_some() {
            self.current = None;
        }
    }

    /// The result of `Action::SetClock`. On success guest init answers
    /// `ControlReady`. On a failure it closes the link before the ready
    /// (decision CH62 rule 25). The first success starts the facade
    /// first, and the ready waits for the start (decisions CH21-8, CH25
    /// rule 2).
    pub fn clock_set(&mut self, link: LinkId, result: Result<(), Errno>) -> Option<Action> {
        if self.stage(link) != Some(Stage::Clock) {
            return None;
        }
        if let Err(error) = result {
            return Some(self.close(link, Why::Clock(error)));
        }
        match self.facade.hello() {
            Some(order) => self.order(order),
            None => Some(self.ready(link)),
        }
    }

    /// The facade of `Action::Start` runs as `pid`. A ready that waits
    /// for the start goes out now.
    pub fn started(&mut self, pid: NonZeroU32) -> Vec<Action> {
        let order = self.facade.started(pid);
        let mut actions = self.follow(order);
        actions.extend(self.waiting_ready());
        actions
    }

    /// The facade of `Action::Start` did not start. A ready that waits
    /// for the start goes out now.
    pub fn start_failed(&mut self) -> Vec<Action> {
        let order = self.facade.start_failed();
        let mut actions = self.follow(order);
        actions.extend(self.waiting_ready());
        actions
    }

    /// The reaper reaped `pid` (decision CH19 rule 1). The exit of the
    /// facade closes its local link, so a child that holds the socket
    /// cannot keep it.
    pub fn reaped(&mut self, pid: NonZeroU32) -> Vec<Action> {
        let order = self.facade.reaped(pid);
        let mut actions = self.follow(order);
        if self.facade.pid().is_none()
            && let Some(local) = self.local.take()
        {
            actions.push(Action::LocalClose(local, Why::FacadeExited));
        }
        actions
    }

    /// A peer with pid `peer` connected to the local link. It is the link
    /// of the facade only when the supervisor takes it (decision CH62 rule
    /// 10). Each other peer is closed at once.
    pub fn local_accept(&mut self, peer: NonZeroU32) -> (LocalId, Vec<Action>) {
        self.local_accepted = self.local_accepted.saturating_add(1);
        let local = LocalId(self.local_accepted);
        if !self.facade.linked(peer) {
            return (local, vec![Action::LocalClose(local, Why::NotFacade(peer))]);
        }
        self.local = Some(local);
        (local, self.follow(None))
    }

    /// A line of local link `local`, its LF cut: one `TurnEvent`
    /// (decisions CH21, CH62 rule 11). A refused line closes the link.
    pub fn local_line(&mut self, local: LocalId, line: &[u8]) -> Vec<Action> {
        if self.local != Some(local) {
            return Vec::new();
        }
        // The typed reader of this link (the strict reader gap of
        // `AGENTS.md` Known gaps).
        let event = match serde_json::from_slice::<TurnEvent>(line) {
            Ok(event) => event,
            Err(error) => return self.local_refused(local, Why::Refused(error.to_string())),
        };
        self.facade.turn(&event);
        let relay = match self.current {
            Some((link, Stage::Ready)) => match encode(&GuestEvent::Turn(event)) {
                Ok(line) => Action::Turn(link, line),
                Err(why) => self.close(link, why),
            },
            Some((_, Stage::Hello | Stage::Clock)) | None => Action::Dropped(local),
        };
        let mut actions = vec![relay];
        actions.extend(self.follow(None));
        actions
    }

    /// The reader of local link `local` refused its bytes, or a line of
    /// it. The link closes, and the facade is killed (decision CH32 rule
    /// 7).
    pub fn local_refused(&mut self, local: LocalId, why: Why) -> Vec<Action> {
        if self.local != Some(local) {
            return Vec::new();
        }
        self.local = None;
        let mut actions = vec![Action::LocalClose(local, why)];
        let order = self.facade.unlinked();
        actions.extend(self.follow(order));
        actions
    }

    /// Local link `local` ended, or its read or a write failed. The
    /// facade is killed (decision CH32 rule 7).
    pub fn local_ended(&mut self, local: LocalId) -> Vec<Action> {
        self.local_refused(local, Why::Ended)
    }

    /// The timer of the last `Action::Timer` ended.
    pub fn timer(&mut self) -> Vec<Action> {
        let order = self.facade.timer();
        self.follow(order)
    }

    /// The stop of `Action::Stop` (decisions CH19 rule 3, CH62 rule 9).
    pub fn stop(&mut self) -> Vec<Action> {
        let orders = self.facade.stop();
        self.follow(orders)
    }

    /// The pid of the live facade.
    #[cfg(target_os = "linux")]
    pub fn facade_pid(&self) -> Option<NonZeroU32> {
        self.facade.pid()
    }

    /// The disks of `link`'s sync are synced. Guest init answers
    /// `GuestEvent::Synced` on the same link (decision CH62 rule 23). A
    /// link that is no longer current gets nothing.
    pub fn synced(&mut self, link: LinkId) -> Option<Action> {
        if self.stage(link) != Some(Stage::Ready) {
            return None;
        }
        match encode(&GuestEvent::Synced(ControlSynced {})) {
            Ok(line) => Some(Action::Synced(link, line)),
            Err(why) => Some(self.close(link, why)),
        }
    }

    /// The `ControlReady` of `link`, with the facade state now.
    fn ready(&mut self, link: LinkId) -> Action {
        let ready = ControlReady {
            facade: self.facade.state(),
        };
        match encode(&ready) {
            Ok(line) => {
                self.current = Some((link, Stage::Ready));
                Action::Ready(link, line)
            }
            Err(why) => self.close(link, why),
        }
    }

    /// The ready of a current link whose clock set waits for the facade
    /// start.
    fn waiting_ready(&mut self) -> Option<Action> {
        match self.current {
            Some((link, Stage::Clock)) => Some(self.ready(link)),
            Some((_, Stage::Hello | Stage::Ready)) | None => None,
        }
    }

    /// The actions of supervisor `orders`, after a facade line when the
    /// state changed and the current link got its ready (decision CH43
    /// rule 8).
    fn follow(&mut self, orders: impl IntoIterator<Item = Order>) -> Vec<Action> {
        let mut actions = Vec::new();
        let state = self.facade.state();
        if state != self.reported {
            self.reported = state.clone();
            if let Some((link, Stage::Ready)) = self.current {
                actions.push(match encode(&GuestEvent::Facade(state)) {
                    Ok(line) => Action::Facade(link, line),
                    Err(why) => self.close(link, why),
                });
            }
        }
        actions.extend(orders.into_iter().filter_map(|order| self.order(order)));
        actions
    }

    /// The action of one supervisor order.
    fn order(&self, order: Order) -> Option<Action> {
        match order {
            // The first hello sets the session before it starts the
            // facade, so a start always has one.
            Order::Start => self.session.clone().map(Action::Start),
            Order::Timer(length) => Some(Action::Timer(length)),
            // The supervisor asks only with an open link.
            Order::AskStop => self.local.map(Action::LocalStop),
            Order::Terminate(pid) => Some(Action::Terminate(pid)),
            Order::Kill(pid) => Some(Action::Kill(pid)),
            Order::PowerOff => Some(Action::PowerOff),
        }
    }

    /// The stage of `link` when it is the current link.
    fn stage(&self, link: LinkId) -> Option<Stage> {
        self.current
            .and_then(|(current, stage)| (current == link).then_some(stage))
    }

    /// Checks the session of a hello, then asks for the clock set.
    fn hello(&mut self, link: LinkId, hello: ControlHello) -> Action {
        match &self.session {
            Some(first) if *first != hello.session => {
                self.close(link, Why::OtherSession(hello.session))
            }
            Some(_) => self.set_clock(link, hello.sent_at),
            None => {
                self.session = Some(hello.session);
                self.set_clock(link, hello.sent_at)
            }
        }
    }

    fn set_clock(&mut self, link: LinkId, at: UnixMillis) -> Action {
        self.current = Some((link, Stage::Clock));
        Action::SetClock(link, at)
    }

    /// Closes the current link.
    fn close(&mut self, link: LinkId, why: Why) -> Action {
        self.current = None;
        Action::Close(link, why)
    }
}

/// One compact JSON line of guest init, its LF included.
fn encode<T: serde::Serialize>(value: &T) -> Result<Vec<u8>, Why> {
    let mut line = serde_json::to_vec(value).map_err(|error| Why::Encode(error.to_string()))?;
    line.push(b'\n');
    Ok(line)
}

/// The framing of one link: bytes in, lines out (rule 2 of "The vsock
/// JSON links" in `wire/AGENTS.md`). A line of exactly
/// `CONTROL_LINE_CAP` bytes passes, as the vmm reader passes it.
#[derive(Debug, Default)]
pub struct Lines {
    /// The bytes after the last LF.
    pending: Vec<u8>,
}

impl Lines {
    /// Adds the bytes of one read and returns each line that they end,
    /// LF cut.
    ///
    /// # Errors
    ///
    /// `Why::TooLong` once a line has more than `CONTROL_LINE_CAP` bytes.
    /// The reader stops then and does not read the rest.
    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<Vec<u8>>, Why> {
        self.pending.extend_from_slice(bytes);
        let mut lines = Vec::new();
        while let Some(end) = self.pending.iter().position(|&byte| byte == b'\n') {
            if end > CONTROL_LINE_CAP {
                return Err(Why::TooLong);
            }
            let mut line: Vec<u8> = self.pending.drain(..=end).collect();
            line.pop();
            lines.push(line);
        }
        if self.pending.len() > CONTROL_LINE_CAP {
            return Err(Why::TooLong);
        }
        Ok(lines)
    }

    /// The link ended. Bytes with no LF are a cut line.
    ///
    /// # Errors
    ///
    /// `Why::CutLine` when bytes wait for an LF.
    pub fn end(&self) -> Result<(), Why> {
        if self.pending.is_empty() {
            Ok(())
        } else {
            Err(Why::CutLine)
        }
    }
}

#[cfg(target_os = "linux")]
pub use shell::{listen, serve};

/// The vsock listener, the local link, the threads and the facade
/// process (decisions CH62 rules 1, 2 and 10). The kvm tests prove it.
#[cfg(target_os = "linux")]
mod shell {
    use std::collections::{HashMap, VecDeque};
    use std::fs::File;
    use std::hash::Hash;
    use std::io::{self, Read as _, Write as _};
    use std::num::NonZeroU32;
    use std::os::fd::{AsRawFd as _, OwnedFd};
    use std::os::unix::net::UnixListener;
    use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender, sync_channel};
    use std::thread;
    use std::time::Instant;

    use creche_wire::guest::SESSION_DISK_MOUNT;
    use creche_wire::{GuestPort, UnixMillis};
    use nix::sys::socket::{AddressFamily, SockFlag, SockType, VsockAddr, bind, socket};
    use rustix::io::Errno;
    use rustix::net::{Shutdown, SocketFlags, accept_with, shutdown};
    use rustix::process::Signal;
    use rustix::time::{ClockId, Timespec, clock_gettime, clock_settime};

    use super::{Action, Control, Lines, LinkId, Why};
    use crate::env_file::Env;
    use crate::local_link::{self, LocalId, STOP_LINE};
    use crate::supervisor::{Facade, Reaped, signal};

    /// The bytes of one read, as the vmm reader takes them.
    const CHUNK: usize = 4096;

    /// The listen backlog. The vmm dials one control link at a time
    /// (decision CH13), so a few slots are room.
    const BACKLOG: i32 = 4;

    /// The events that the inbox holds before a send blocks. A full
    /// inbox stops each reader thread before its next socket read, so a
    /// peer that writes faster than the control loop acts fills only its
    /// own socket buffer, and the inbox of PID 1 stays bounded.
    const INBOX: usize = 64;

    /// What the threads tell the control loop.
    #[derive(Debug)]
    enum Event {
        /// A new link, and the sender that its reader thread gets.
        Accepted(OwnedFd, SyncSender<Event>),
        /// The accept loop stopped on this error.
        AcceptFailed(io::Error),
        Line(LinkId, Vec<u8>),
        Refused(LinkId, Why),
        /// The link ended at a line end, or its read failed.
        Ended(LinkId),
        /// The reaper reaped a child of PID 1.
        Reaped(Reaped),
        /// The reaper stopped on this wait error.
        ReapFailed(io::Error),
        /// A new local link, its peer pid, and the sender that its reader
        /// thread gets.
        LocalAccepted(OwnedFd, NonZeroU32, SyncSender<Event>),
        /// The local accept loop stopped on this error.
        LocalAcceptFailed(io::Error),
        LocalLine(LocalId, Vec<u8>),
        LocalRefused(LocalId, Why),
        /// The local link ended at a line end, or its read failed.
        LocalEnded(LocalId),
    }

    /// What one reader thread got.
    enum Got {
        Line(Vec<u8>),
        Refused(Why),
        /// The link ended at a line end, or its read failed.
        Ended,
    }

    /// Listens on vsock port 1200 of any CID (decisions CH13, CH14).
    ///
    /// # Errors
    ///
    /// The error of `socket`, `bind` or `listen`.
    pub fn listen() -> io::Result<OwnedFd> {
        let listener = socket(
            AddressFamily::Vsock,
            SockType::Stream,
            SockFlag::SOCK_CLOEXEC,
            None,
        )?;
        let address = VsockAddr::new(nix::libc::VMADDR_CID_ANY, GuestPort::Control.number());
        bind(listener.as_raw_fd(), &address)?;
        rustix::net::listen(&listener, BACKLOG)?;
        Ok(listener)
    }

    /// Serves control link 1200, the local link on `local` and the
    /// facade, which gets `env`, until a stop or a fault. It returns the
    /// console line of the end, and the caller powers off.
    pub fn serve(listener: OwnedFd, local: UnixListener, env: Env) -> String {
        let (events, inbox) = sync_channel(INBOX);
        let reaps = events.clone();
        let facade = match Facade::new(env, move |reaped| {
            let event = match reaped {
                Ok(reaped) => Event::Reaped(reaped),
                Err(error) => Event::ReapFailed(error),
            };
            reaps.send(event).is_ok()
        }) {
            Ok(facade) => facade,
            Err(error) => return format!("reaper thread failed: {error}"),
        };
        let local_events = events.clone();
        let spawned = thread::Builder::new()
            .name("control-accept".to_owned())
            .spawn(move || accept_loop(&listener, &events));
        if let Err(error) = spawned {
            return format!("control accept thread failed: {error}");
        }
        let spawned = thread::Builder::new()
            .name("local-accept".to_owned())
            .spawn(move || local_accept_loop(&local, &local_events));
        if let Err(error) = spawned {
            return format!("local link accept thread failed: {error}");
        }
        run(&inbox, &facade)
    }

    /// The control loop: one thread owns the state, every write and the
    /// one timer of the supervisor.
    ///
    /// The loop holds no sender. The two accept threads, the reaper
    /// thread and each reader thread hold one, so the inbox ends only when
    /// all of them are gone. Then no vmm can reach the guest again, and the
    /// caller powers off (decision CH19 rule 3). A panic in any thread
    /// powers off through the panic hook of `as_pid_1`.
    fn run(inbox: &Receiver<Event>, facade: &Facade) -> String {
        let mut control = Control::default();
        let mut links: HashMap<LinkId, File> = HashMap::new();
        let mut locals: HashMap<LocalId, File> = HashMap::new();
        let mut timer: Option<Instant> = None;
        loop {
            // The timer goes first. `recv_timeout` hands out a queued event
            // before it reads the clock, so a full inbox would starve a
            // timer that the loop reads only on a timeout, and the stop
            // waits of decision CH62 rule 9 would have no bound.
            if timer.is_some_and(|end| Instant::now() >= end) {
                timer = None;
                let actions = control.timer();
                if let Some(end) = act(
                    &mut control,
                    &mut links,
                    &mut locals,
                    &mut timer,
                    facade,
                    actions,
                ) {
                    return end;
                }
                continue;
            }
            let event = match timer {
                Some(end) => {
                    match inbox.recv_timeout(end.saturating_duration_since(Instant::now())) {
                        Ok(event) => event,
                        // The next pass runs the timer.
                        Err(RecvTimeoutError::Timeout) => continue,
                        Err(RecvTimeoutError::Disconnected) => break,
                    }
                }
                None => match inbox.recv() {
                    Ok(event) => event,
                    Err(_) => break,
                },
            };
            let actions = match event {
                Event::Accepted(fd, events) => {
                    let (link, old) = control.accept();
                    crate::say(&format!("{link} open"));
                    let reader = move |got| {
                        let event = match got {
                            Got::Line(line) => Event::Line(link, line),
                            Got::Refused(why) => Event::Refused(link, why),
                            Got::Ended => Event::Ended(link),
                        };
                        events.send(event).is_ok()
                    };
                    match start_reader(&link.to_string(), fd, reader) {
                        Ok(file) => {
                            links.insert(link, file);
                        }
                        Err(error) => {
                            crate::say(&format!("{link} reader failed: {error}"));
                            control.ended(link);
                        }
                    }
                    // The close of the replaced link.
                    old.into_iter().collect()
                }
                Event::AcceptFailed(error) => return format!("control accept failed: {error}"),
                Event::Line(link, line) => control.line(link, &line).into_iter().collect(),
                Event::Refused(link, why) => control.refused(link, why).into_iter().collect(),
                Event::Ended(link) => {
                    control.ended(link);
                    links.remove(&link);
                    Vec::new()
                }
                Event::Reaped(reaped) => {
                    if control.facade_pid() == Some(reaped.pid) {
                        crate::say(&format!("facade pid {} exited: {reaped}", reaped.pid));
                    }
                    control.reaped(reaped.pid)
                }
                Event::ReapFailed(error) => return format!("reaper failed: {error}"),
                Event::LocalAccepted(fd, peer, events) => {
                    let (local, mut actions) = control.local_accept(peer);
                    crate::say(&format!("{local} open: peer pid {peer}"));
                    // A refused peer gets no reader: its fd closes here.
                    let refused = actions.iter().any(
                        |action| matches!(action, Action::LocalClose(closed, _) if *closed == local),
                    );
                    if !refused {
                        let reader = move |got| {
                            let event = match got {
                                Got::Line(line) => Event::LocalLine(local, line),
                                Got::Refused(why) => Event::LocalRefused(local, why),
                                Got::Ended => Event::LocalEnded(local),
                            };
                            events.send(event).is_ok()
                        };
                        match start_reader(&local.to_string(), fd, reader) {
                            Ok(file) => {
                                locals.insert(local, file);
                            }
                            Err(error) => {
                                crate::say(&format!("{local} reader failed: {error}"));
                                actions.extend(control.local_ended(local));
                            }
                        }
                    }
                    actions
                }
                Event::LocalAcceptFailed(error) => {
                    return format!("local link accept failed: {error}");
                }
                Event::LocalLine(local, line) => control.local_line(local, &line),
                Event::LocalRefused(local, why) => control.local_refused(local, why),
                Event::LocalEnded(local) => control.local_ended(local),
            };
            if let Some(end) = act(
                &mut control,
                &mut links,
                &mut locals,
                &mut timer,
                facade,
                actions,
            ) {
                return end;
            }
        }
        "control events ended".to_owned()
    }

    /// Runs `actions` and each action that follows from them, in order. It
    /// returns the console line of the end of a stop.
    fn act(
        control: &mut Control,
        links: &mut HashMap<LinkId, File>,
        locals: &mut HashMap<LocalId, File>,
        timer: &mut Option<Instant>,
        facade: &Facade,
        actions: Vec<Action>,
    ) -> Option<String> {
        let mut next = VecDeque::from(actions);
        while let Some(action) = next.pop_front() {
            match action {
                Action::SetClock(link, at) => {
                    let result = set_clock(at);
                    if let Ok(realtime) = result {
                        crate::say(&format!("{link} clock set: realtime {realtime}"));
                    }
                    next.extend(control.clock_set(link, result.map(|_| ())));
                }
                Action::Ready(link, line) => write(links, link, &line, "ready"),
                Action::Start(session) => match facade.start(&session) {
                    Ok(pid) => {
                        crate::say(&format!("facade pid {pid} started"));
                        next.extend(control.started(pid));
                    }
                    Err(error) => {
                        crate::say(&format!("facade start failed: {error}"));
                        next.extend(control.start_failed());
                    }
                },
                Action::Facade(link, line) => write(links, link, &line, "facade"),
                Action::Timer(length) => *timer = Some(Instant::now() + length),
                Action::Terminate(pid) => match signal(pid, Signal::TERM) {
                    Ok(()) => crate::say(&format!("facade pid {pid} sent SIGTERM")),
                    Err(error) => crate::say(&format!("facade pid {pid} SIGTERM failed: {error}")),
                },
                Action::Kill(pid) => match signal(pid, Signal::KILL) {
                    Ok(()) => crate::say(&format!("facade pid {pid} sent SIGKILL")),
                    // No process of the group is left.
                    Err(Errno::SRCH) => {}
                    Err(error) => crate::say(&format!("facade pid {pid} SIGKILL failed: {error}")),
                },
                Action::Turn(link, line) => write(links, link, &line, "turn"),
                Action::Dropped(local) => crate::say(&format!(
                    "{local}: a turn event with no ready control link is dropped"
                )),
                Action::LocalStop(local) => {
                    match locals.get_mut(&local).map(|file| file.write_all(STOP_LINE)) {
                        Some(Ok(())) => crate::say(&format!("{local} stop")),
                        Some(Err(error)) => {
                            crate::say(&format!("{local} write failed: {error}"));
                            next.extend(control.local_ended(local));
                        }
                        None => {}
                    }
                }
                Action::LocalClose(local, why) => {
                    crate::say(&format!("{local} closed: {why}"));
                    close(locals, local);
                }
                Action::Sync(link) => match sync_session() {
                    Ok(()) => next.extend(control.synced(link)),
                    // CH65 rule 3: guest init sends no `synced` and keeps
                    // the link, so the vmm's SYNC_WAIT runs out, it takes
                    // no snapshot and it runs the graceful stop (decision
                    // CH62 rule 23).
                    Err(error) => crate::say(&format!("{link} sync failed: {error}")),
                },
                Action::Synced(link, line) => write(links, link, &line, "synced"),
                Action::Close(link, why) => {
                    crate::say(&format!("{link} closed: {why}"));
                    close(links, link);
                }
                Action::Ignore(link, ignored) => crate::say(&format!("{link}: {ignored}")),
                Action::Stop(link) => {
                    crate::say(&format!("stop on {link}"));
                    next.extend(control.stop());
                }
                Action::PowerOff => return Some("stop done".to_owned()),
            }
        }
        None
    }

    /// Writes `line` to `link` and names it on the console as `what`. A
    /// failed write closes the link.
    fn write(links: &mut HashMap<LinkId, File>, link: LinkId, line: &[u8], what: &str) {
        match links.get_mut(&link).map(|file| file.write_all(line)) {
            Some(Ok(())) => crate::say(&format!("{link} {what}")),
            Some(Err(error)) => {
                crate::say(&format!("{link} write failed: {error}"));
                close(links, link);
            }
            None => {}
        }
    }

    /// Runs `syncfs` of the session disk, then `sync` (decision CH62 rule
    /// 23). `syncfs` reports a write-back error of the session disk.
    /// `sync` reports none.
    fn sync_session() -> io::Result<()> {
        use rustix::fs::{Mode, OFlags, open, sync, syncfs};

        let dir = open(
            SESSION_DISK_MOUNT,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        syncfs(&dir)?;
        sync();
        Ok(())
    }

    /// Shuts the link down, so its reader thread reads an EOF and ends,
    /// and drops the write end.
    fn close<K: Eq + Hash>(links: &mut HashMap<K, File>, link: K) {
        if let Some(file) = links.remove(&link) {
            let _ = shutdown(&file, Shutdown::Both);
        }
    }

    /// Sets `CLOCK_REALTIME` to `at` (decision CH62 rule 25) and returns
    /// the clock after the set, in Unix milliseconds, for the console.
    fn set_clock(at: UnixMillis) -> Result<i64, Errno> {
        let millis = u64::from(at);
        let tv_sec = i64::try_from(millis / 1000).map_err(|_| Errno::INVAL)?;
        let tv_nsec = i64::try_from(millis % 1000 * 1_000_000).map_err(|_| Errno::INVAL)?;
        clock_settime(ClockId::Realtime, Timespec { tv_sec, tv_nsec })?;
        let now = clock_gettime(ClockId::Realtime);
        Ok(now
            .tv_sec
            .saturating_mul(1000)
            .saturating_add(now.tv_nsec / 1_000_000))
    }

    /// Accepts each link and hands it to the control loop. EINTR and
    /// ECONNABORTED are retried. Each other error ends the loop.
    fn accept_loop(listener: &OwnedFd, events: &SyncSender<Event>) {
        loop {
            match accept_with(listener, SocketFlags::CLOEXEC) {
                Ok(fd) => {
                    if events.send(Event::Accepted(fd, events.clone())).is_err() {
                        return;
                    }
                }
                Err(Errno::INTR | Errno::CONNABORTED) => {}
                Err(error) => {
                    let _ = events.send(Event::AcceptFailed(error.into()));
                    return;
                }
            }
        }
    }

    /// Accepts each local link, reads its peer pid and hands both to the
    /// control loop (decision CH62 rule 10). A failed peer read closes
    /// that link. Each other error ends the loop.
    fn local_accept_loop(listener: &UnixListener, events: &SyncSender<Event>) {
        loop {
            match listener.accept() {
                Ok((stream, _)) => match local_link::peer(&stream) {
                    Ok(peer) => {
                        let event = Event::LocalAccepted(stream.into(), peer, events.clone());
                        if events.send(event).is_err() {
                            return;
                        }
                    }
                    Err(error) => crate::say(&format!("local link peer read failed: {error}")),
                },
                Err(error) if error.kind() == io::ErrorKind::ConnectionAborted => {}
                Err(error) => {
                    let _ = events.send(Event::LocalAcceptFailed(error));
                    return;
                }
            }
        }
    }

    /// Starts the reader thread of the link `name` and returns the write
    /// end. The thread hands each `Got` to `send` and stops when `send`
    /// returns false.
    fn start_reader(
        name: &str,
        fd: OwnedFd,
        mut send: impl FnMut(Got) -> bool + Send + 'static,
    ) -> io::Result<File> {
        let file = File::from(fd);
        let reader = file.try_clone()?;
        let link = name.to_owned();
        thread::Builder::new()
            .name(link.clone())
            .spawn(move || read_loop(&link, reader, &mut send))?;
        Ok(file)
    }

    /// Reads the lines of one link until its end or a refused line.
    fn read_loop(link: &str, mut reader: File, send: &mut impl FnMut(Got) -> bool) {
        let mut lines = Lines::default();
        let mut chunk = [0_u8; CHUNK];
        loop {
            let read = match reader.read(&mut chunk) {
                Ok(0) => {
                    let _ = send(match lines.end() {
                        Ok(()) => Got::Ended,
                        Err(why) => Got::Refused(why),
                    });
                    return;
                }
                Ok(read) => read,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    crate::say(&format!("{link} read failed: {error}"));
                    let _ = send(Got::Ended);
                    return;
                }
            };
            match lines.push(chunk.get(..read).unwrap_or_default()) {
                Ok(each) => {
                    for line in each {
                        if !send(Got::Line(line)) {
                            return;
                        }
                    }
                }
                Err(why) => {
                    let _ = send(Got::Refused(why));
                    return;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use creche_wire::{ControlCommand, ControlStop};
    use serde_json::json;

    use crate::supervisor::{RESTART_PAUSE, SIGTERM_GRACE};

    const SESSION: &str = "0123abcd-0000-4000-8000-00000000c7c7";
    const OTHER: &str = "0123abcd-0000-4000-8000-00000000d8d8";

    /// 2030-01-01T00:00:00Z.
    const SENT_AT: u64 = 1_893_456_000_000;

    fn hello(session: &str) -> Vec<u8> {
        serde_json::to_vec(&json!({"sent_at": SENT_AT, "session": session})).unwrap()
    }

    fn at() -> UnixMillis {
        UnixMillis::try_from(SENT_AT).unwrap()
    }

    fn session() -> SessionId {
        SESSION.parse().unwrap()
    }

    /// The pid of the first facade.
    const FACADE: u32 = 7;

    fn pid(raw: u32) -> NonZeroU32 {
        NonZeroU32::new(raw).unwrap()
    }

    /// A line of `{"facade": <state>}`: the shape of `ControlReady` and of
    /// `GuestEvent::Facade` both.
    fn facade_line(state: &str) -> Vec<u8> {
        format!(r#"{{"facade":{state}}}"#)
            .into_bytes()
            .into_iter()
            .chain([b'\n'])
            .collect()
    }

    fn starting(raw: u32, starts: u64) -> Vec<u8> {
        facade_line(&format!(
            r#"{{"starting":{{"last_turn_end":null,"pid":{raw},"starts":{starts}}}}}"#
        ))
    }

    fn exited(starts: u64) -> Vec<u8> {
        facade_line(&format!(
            r#"{{"exited":{{"last_turn_end":null,"starts":{starts}}}}}"#
        ))
    }

    fn stop() -> Vec<u8> {
        serde_json::to_vec(&ControlCommand::Stop(ControlStop {})).unwrap()
    }

    /// A link that got its ready. The first ready of the boot starts the
    /// facade as `FACADE` first.
    fn ready(control: &mut Control) -> LinkId {
        let (link, _) = control.accept();
        assert_eq!(
            control.line(link, &hello(SESSION)),
            Some(Action::SetClock(link, at()))
        );
        let mut action = control.clock_set(link, Ok(()));
        if action == Some(Action::Start(session())) {
            let mut actions = control.started(pid(FACADE));
            action = actions.pop();
            assert_eq!(actions, vec![]);
        }
        let line = encode(&ControlReady {
            facade: control.facade.state(),
        })
        .unwrap();
        assert_eq!(action, Some(Action::Ready(link, line)));
        link
    }

    fn refused(action: Option<Action>, link: LinkId) -> String {
        match action {
            Some(Action::Close(closed, Why::Refused(error))) if closed == link => error,
            other => panic!("no refusal of {link}: {other:?}"),
        }
    }

    #[test]
    fn a_hello_sets_the_clock_before_the_ready() {
        let mut control = Control::default();
        let (link, old) = control.accept();
        assert_eq!(old, None);
        // The clock comes first, and nothing goes out before it is set.
        assert_eq!(
            control.line(link, &hello(SESSION)),
            Some(Action::SetClock(link, at()))
        );
        assert_eq!(
            control.line(link, &stop()),
            Some(Action::Close(link, Why::Early))
        );

        let mut control = Control::default();
        let link = ready(&mut control);
        assert_eq!(control.line(link, &stop()), Some(Action::Stop(link)));
    }

    #[test]
    fn every_hello_sets_the_clock() {
        let mut control = Control::default();
        ready(&mut control);
        let (second, _) = control.accept();
        // A backward step is allowed (decision CH62 rule 25).
        let earlier = serde_json::to_vec(&json!({"sent_at": 0, "session": SESSION})).unwrap();
        assert_eq!(
            control.line(second, &earlier),
            Some(Action::SetClock(second, UnixMillis::try_from(0).unwrap()))
        );
    }

    #[test]
    fn a_newer_link_replaces_the_old_one() {
        let mut control = Control::default();
        let old = ready(&mut control);
        let (new, closed) = control.accept();
        assert_ne!(new, old);
        assert_eq!(closed, Some(Action::Close(old, Why::Replaced)));
        // A line of the old link changes nothing, a stop included.
        assert_eq!(
            control.line(old, &stop()),
            Some(Action::Ignore(old, Ignored::Stale))
        );
        assert_eq!(
            control.line(new, &hello(SESSION)),
            Some(Action::SetClock(new, at()))
        );
    }

    #[test]
    fn an_eof_from_a_replaced_link_changes_nothing() {
        let mut control = Control::default();
        let old = ready(&mut control);
        let (new, _) = control.accept();
        control.ended(old);
        assert_eq!(control.refused(old, Why::CutLine), None);
        assert_eq!(control.clock_set(old, Ok(())), None);
        assert_eq!(
            control.line(new, &hello(SESSION)),
            Some(Action::SetClock(new, at()))
        );
        // The end of the current link does end it.
        control.ended(new);
        assert_eq!(
            control.line(new, &hello(SESSION)),
            Some(Action::Ignore(new, Ignored::Stale))
        );
    }

    #[test]
    fn a_hello_for_another_session_closes_the_link() {
        let mut control = Control::default();
        ready(&mut control);
        let (link, _) = control.accept();
        let session: SessionId = OTHER.parse().unwrap();
        assert_eq!(
            control.line(link, &hello(OTHER)),
            Some(Action::Close(link, Why::OtherSession(session)))
        );
        // The first session stays, so a hello for it still passes.
        let (next, _) = control.accept();
        assert_eq!(
            control.line(next, &hello(SESSION)),
            Some(Action::SetClock(next, at()))
        );
    }

    #[test]
    fn a_line_over_the_cap_closes_the_link() {
        let mut lines = Lines::default();
        assert_eq!(lines.push(&vec![b' '; CONTROL_LINE_CAP]), Ok(vec![]));
        assert_eq!(lines.push(b" "), Err(Why::TooLong));

        let mut over = vec![b' '; CONTROL_LINE_CAP + 1];
        over.push(b'\n');
        assert_eq!(Lines::default().push(&over), Err(Why::TooLong));

        let mut control = Control::default();
        let (link, _) = control.accept();
        assert_eq!(
            control.refused(link, Why::TooLong),
            Some(Action::Close(link, Why::TooLong))
        );
        assert_eq!(
            control.line(link, &hello(SESSION)),
            Some(Action::Ignore(link, Ignored::Stale))
        );
    }

    #[test]
    fn a_line_of_exactly_the_cap_passes() {
        let mut line = vec![b' '; CONTROL_LINE_CAP];
        line.extend_from_slice(b"\n{}\n{");
        let mut lines = Lines::default();
        let out = lines.push(&line).unwrap();
        assert_eq!(out, vec![vec![b' '; CONTROL_LINE_CAP], b"{}".to_vec()]);
        assert_eq!(lines.end(), Err(Why::CutLine));
        assert_eq!(lines.push(b"}\n"), Ok(vec![b"{}".to_vec()]));
        assert_eq!(lines.end(), Ok(()));
    }

    #[test]
    fn an_unknown_field_closes_the_link() {
        let mut control = Control::default();
        let (link, _) = control.accept();
        let line = json!({"sent_at": SENT_AT, "session": SESSION, "family": "x"});
        let error = refused(
            control.line(link, &serde_json::to_vec(&line).unwrap()),
            link,
        );
        assert!(error.contains("unknown field `family`"), "{error}");

        let mut control = Control::default();
        let link = ready(&mut control);
        let error = refused(control.line(link, br#"{"stop":{"now":true}}"#), link);
        assert!(error.contains("unknown field `now`"), "{error}");
    }

    /// The strict reader rules that the types of this link need (rule 3
    /// of "The vsock JSON links"): a key given twice, a second value, a
    /// float for an integer and an array for an object are refused.
    #[test]
    fn a_line_that_is_not_one_strict_value_closes_the_link() {
        for line in [
            format!(r#"{{"sent_at":{SENT_AT},"sent_at":{SENT_AT},"session":"{SESSION}"}}"#),
            format!(r#"{{"sent_at":{SENT_AT},"session":"{SESSION}"}} {{}}"#),
            format!(r#"{{"sent_at":{SENT_AT}.0,"session":"{SESSION}"}}"#),
            format!(r#"[{SENT_AT},"{SESSION}"]"#),
            String::new(),
            "hello".to_owned(),
        ] {
            let mut control = Control::default();
            let (link, _) = control.accept();
            refused(control.line(link, line.as_bytes()), link);
        }
        for line in [
            r#"{"stop":{},"stop":{}}"#,
            r#"{"stop":{},"sync":{}}"#,
            r#"{"stop":[]}"#,
            r#""stop""#,
        ] {
            let mut control = Control::default();
            let link = ready(&mut control);
            refused(control.line(link, line.as_bytes()), link);
        }
    }

    #[test]
    fn a_failed_clock_set_closes_before_the_ready() {
        let mut control = Control::default();
        let (link, _) = control.accept();
        control.line(link, &hello(SESSION));
        assert_eq!(
            control.clock_set(link, Err(Errno::PERM)),
            Some(Action::Close(link, Why::Clock(Errno::PERM)))
        );
        assert_eq!(
            control.line(link, &stop()),
            Some(Action::Ignore(link, Ignored::Stale))
        );
    }

    /// A sync asks the shell for the syncs, and only then does guest init
    /// answer `synced` on the same link (decision CH62 rule 23).
    #[test]
    fn sync_answers_synced_after_syncfs() {
        let mut control = Control::default();
        let link = ready(&mut control);
        assert_eq!(
            control.line(link, br#"{"sync":{}}"#),
            Some(Action::Sync(link))
        );
        assert_eq!(
            control.synced(link),
            Some(Action::Synced(link, b"{\"synced\":{}}\n".to_vec()))
        );
        // The link stays ready: a second sync and a stop still pass.
        assert_eq!(
            control.line(link, br#"{"sync":{}}"#),
            Some(Action::Sync(link))
        );
        assert_eq!(control.line(link, &stop()), Some(Action::Stop(link)));
    }

    /// A sync before the ready is not a hello, so it closes the link.
    #[test]
    fn a_sync_before_the_ready_closes_the_link() {
        let mut control = Control::default();
        let (link, _) = control.accept();
        refused(control.line(link, br#"{"sync":{}}"#), link);
        assert_eq!(control.synced(link), None);
    }

    #[test]
    fn a_sync_from_a_replaced_link_is_ignored() {
        let mut control = Control::default();
        let old = ready(&mut control);
        let (new, _) = control.accept();
        assert_eq!(
            control.line(old, br#"{"sync":{}}"#),
            Some(Action::Ignore(old, Ignored::Stale))
        );
        assert_eq!(control.synced(old), None);
        // The new link gets no synced before its own ready.
        assert_eq!(control.synced(new), None);
    }

    /// The clock set of the first hello starts the facade, and its ready
    /// says `starting`. Each later ready reports the state at that time,
    /// and a link close never touches the facade (decisions CH19 rule 1,
    /// CH21-8, CH25 rule 2).
    #[test]
    fn the_ready_reports_the_current_facade_state() {
        let mut control = Control::default();
        let (link, _) = control.accept();
        control.line(link, &hello(SESSION));
        assert_eq!(
            control.clock_set(link, Ok(())),
            Some(Action::Start(session()))
        );
        assert_eq!(
            control.started(pid(FACADE)),
            vec![Action::Ready(link, starting(FACADE, 1))]
        );

        // The link closes, and the next link finds the same facade.
        control.ended(link);
        let (next, _) = control.accept();
        control.line(next, &hello(SESSION));
        assert_eq!(
            control.clock_set(next, Ok(())),
            Some(Action::Ready(next, starting(FACADE, 1)))
        );

        // An exit with no link open sends no line, and the next ready
        // carries it.
        control.ended(next);
        assert_eq!(
            control.reaped(pid(FACADE)),
            vec![Action::Kill(pid(FACADE)), Action::Timer(RESTART_PAUSE)]
        );
        let (last, _) = control.accept();
        control.line(last, &hello(SESSION));
        assert_eq!(
            control.clock_set(last, Ok(())),
            Some(Action::Ready(last, exited(1)))
        );
    }

    /// A failed first start lets the ready go out with `not_started`, and
    /// the next try follows the restart pause.
    #[test]
    fn a_failed_start_reports_not_started() {
        let mut control = Control::default();
        let (link, _) = control.accept();
        control.line(link, &hello(SESSION));
        control.clock_set(link, Ok(()));
        assert_eq!(
            control.start_failed(),
            vec![
                Action::Timer(RESTART_PAUSE),
                Action::Ready(link, facade_line(r#"{"not_started":{}}"#)),
            ]
        );
        assert_eq!(control.timer(), vec![Action::Start(session())]);
    }

    /// After the ready, each state change goes out as a facade line, and
    /// an orphan exit changes nothing (decisions CH19 rule 1, CH43 rule 8).
    #[test]
    fn sends_a_facade_line_on_each_state_change() {
        let mut control = Control::default();
        let link = ready(&mut control);
        assert_eq!(control.reaped(pid(8)), vec![]);
        assert_eq!(
            control.reaped(pid(FACADE)),
            vec![
                Action::Facade(link, exited(1)),
                Action::Kill(pid(FACADE)),
                Action::Timer(RESTART_PAUSE)
            ]
        );
        assert_eq!(control.timer(), vec![Action::Start(session())]);
        assert_eq!(
            control.started(pid(9)),
            vec![Action::Facade(link, starting(9, 2))]
        );

        // The stop: SIGTERM and the grace, then the exit line and the
        // power-off (decision CH62 rule 9).
        assert_eq!(control.line(link, &stop()), Some(Action::Stop(link)));
        assert_eq!(
            control.stop(),
            vec![Action::Terminate(pid(9)), Action::Timer(SIGTERM_GRACE)]
        );
        assert_eq!(
            control.reaped(pid(9)),
            vec![Action::Facade(link, exited(2)), Action::PowerOff]
        );
    }

    /// A facade line goes to the current link only, after its ready.
    #[test]
    fn a_link_before_its_ready_gets_no_facade_line() {
        let mut control = Control::default();
        let old = ready(&mut control);
        let (new, _) = control.accept();
        assert_eq!(
            control.reaped(pid(FACADE)),
            vec![Action::Kill(pid(FACADE)), Action::Timer(RESTART_PAUSE)]
        );
        assert_eq!(
            control.line(old, &stop()),
            Some(Action::Ignore(old, Ignored::Stale))
        );
        control.line(new, &hello(SESSION));
        assert_eq!(
            control.clock_set(new, Ok(())),
            Some(Action::Ready(new, exited(1)))
        );
    }
}
