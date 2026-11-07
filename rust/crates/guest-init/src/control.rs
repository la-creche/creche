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
//! 6. After `ControlReady`, `ControlCommand::Stop` powers the VM off
//!    (decision CH19 rule 3).
//!
//! `Control` is the pure state machine: inputs in, one `Action` out, so
//! the unit tests drive it on any OS. `Lines` is the pure framing. The
//! Linux shell, `serve`, runs the vsock listener and std threads
//! (decisions CH62 rules 1 and 2).

use std::fmt;

use creche_wire::{
    CONTROL_LINE_CAP, ControlCommand, ControlHello, ControlReady, FacadeNotStarted, FacadeState,
    SessionId, UnixMillis,
};
use rustix::io::Errno;

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
    /// Close the link and write why on the console (decision CH43 rule 2).
    Close(LinkId, Why),
    /// Write why on the console and change nothing.
    Ignore(LinkId, Ignored),
    /// Power the VM off (decision CH19 rule 3).
    PowerOff(LinkId),
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
    /// The ready did not encode. It holds the error of the writer.
    Encode(String),
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
            Self::Encode(error) => write!(formatter, "the ready did not encode: {error}"),
        }
    }
}

/// Why guest init changes nothing for a valid line.
#[derive(Debug, PartialEq, Eq)]
pub enum Ignored {
    /// The line came on a link that is no longer the current one: a newer
    /// link replaced it, or guest init closed it (decision CH28).
    Stale,
    /// `ControlCommand::Sync`. Plan PR G8 answers it. With no `synced`
    /// the vmm takes no snapshot and runs the graceful stop (decision
    /// CH62 rule 23).
    Sync,
}

impl fmt::Display for Ignored {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Stale => "a line of a closed or replaced link is ignored",
            Self::Sync => "sync is not answered yet",
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
/// first hello and the facade state.
#[derive(Debug)]
pub struct Control {
    /// The id of the last accepted link.
    accepted: u64,
    /// The current link, or `None` after it closed.
    current: Option<(LinkId, Stage)>,
    /// The session of the first hello of the boot (decision CH32 rule 3).
    session: Option<SessionId>,
    /// The facade, as `ControlReady` reports it (decision CH19 rule 2).
    facade: FacadeState,
}

impl Default for Control {
    /// The state at boot: no link, no session and no facade.
    fn default() -> Self {
        Self {
            accepted: 0,
            current: None,
            session: None,
            facade: FacadeState::NotStarted(FacadeNotStarted {}),
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
    // CONTRACT-QUESTION: rule 3 of "The vsock JSON links" in
    // `wire/AGENTS.md` names the check `strict` of the vmm crate, and
    // guest init may call creche-wire only (layer rule). The reading
    // taken: the typed serde reader is the strict reader here. Each type
    // of this link denies unknown fields and holds no free JSON value, so
    // it refuses a key given twice, a second value, a float for an
    // integer and an array for an object, as `strict` does. A unit test
    // holds that. A change costs one wire lane PR that moves `strict`
    // into creche-wire, and one call here.
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
                Ok(ControlCommand::Stop(_)) => Some(Action::PowerOff(link)),
                Ok(ControlCommand::Sync(_)) => Some(Action::Ignore(link, Ignored::Sync)),
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
    /// (decision CH62 rule 25).
    pub fn clock_set(&mut self, link: LinkId, result: Result<(), Errno>) -> Option<Action> {
        if self.stage(link) != Some(Stage::Clock) {
            return None;
        }
        if let Err(error) = result {
            return Some(self.close(link, Why::Clock(error)));
        }
        let ready = ControlReady {
            facade: self.facade.clone(),
        };
        match serde_json::to_vec(&ready) {
            Ok(mut line) => {
                line.push(b'\n');
                self.current = Some((link, Stage::Ready));
                Some(Action::Ready(link, line))
            }
            Err(error) => Some(self.close(link, Why::Encode(error.to_string()))),
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

/// The vsock listener and the threads (decisions CH62 rules 1 and 2). The
/// kvm tests prove it.
#[cfg(target_os = "linux")]
mod shell {
    use std::collections::HashMap;
    use std::fs::File;
    use std::io::{self, Read as _, Write as _};
    use std::os::fd::{AsRawFd as _, OwnedFd};
    use std::sync::mpsc::{Receiver, Sender, channel};
    use std::thread;

    use creche_wire::{GuestPort, UnixMillis};
    use nix::sys::socket::{AddressFamily, SockFlag, SockType, VsockAddr, bind, socket};
    use rustix::io::Errno;
    use rustix::net::{Shutdown, SocketFlags, accept_with, shutdown};
    use rustix::time::{ClockId, Timespec, clock_gettime, clock_settime};

    use super::{Action, Control, Lines, LinkId, Why};

    /// The bytes of one read, as the vmm reader takes them.
    const CHUNK: usize = 4096;

    /// The listen backlog. The vmm dials one control link at a time
    /// (decision CH13), so a few slots are room.
    const BACKLOG: i32 = 4;

    /// What the threads tell the control loop.
    #[derive(Debug)]
    enum Event {
        Accepted(OwnedFd),
        /// The accept loop stopped on this error.
        AcceptFailed(io::Error),
        Line(LinkId, Vec<u8>),
        Refused(LinkId, Why),
        /// The link ended at a line end, or its read failed.
        Ended(LinkId),
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

    /// Serves control link 1200 until a stop or a fault. It returns the
    /// console line of the end, and the caller powers off.
    pub fn serve(listener: OwnedFd) -> String {
        let (events, inbox) = channel();
        let accepts = events.clone();
        let spawned = thread::Builder::new()
            .name("control-accept".to_owned())
            .spawn(move || accept_loop(&listener, &accepts));
        if let Err(error) = spawned {
            return format!("control accept thread failed: {error}");
        }
        run(&events, &inbox)
    }

    /// The control loop: one thread owns the state and every write.
    fn run(events: &Sender<Event>, inbox: &Receiver<Event>) -> String {
        let mut control = Control::default();
        let mut links: HashMap<LinkId, File> = HashMap::new();
        for event in inbox {
            let action = match event {
                Event::Accepted(fd) => {
                    let (link, old) = control.accept();
                    crate::say(&format!("{link} open"));
                    match start_reader(link, fd, events) {
                        Ok(file) => {
                            links.insert(link, file);
                        }
                        Err(error) => {
                            crate::say(&format!("{link} reader failed: {error}"));
                            control.ended(link);
                        }
                    }
                    // The close of the replaced link.
                    old
                }
                Event::AcceptFailed(error) => return format!("control accept failed: {error}"),
                Event::Line(link, line) => control.line(link, &line),
                Event::Refused(link, why) => control.refused(link, why),
                Event::Ended(link) => {
                    control.ended(link);
                    links.remove(&link);
                    None
                }
            };
            if let Some(end) = action.and_then(|action| act(&mut control, &mut links, action)) {
                return end;
            }
        }
        "control events ended".to_owned()
    }

    /// Runs `action` and each action that follows from it. It returns the
    /// console line of a stop.
    fn act(
        control: &mut Control,
        links: &mut HashMap<LinkId, File>,
        action: Action,
    ) -> Option<String> {
        let mut next = Some(action);
        while let Some(action) = next.take() {
            match action {
                Action::SetClock(link, at) => {
                    let result = set_clock(at);
                    if let Ok(realtime) = result {
                        crate::say(&format!("{link} clock set: realtime {realtime}"));
                    }
                    next = control.clock_set(link, result.map(|_| ()));
                }
                Action::Ready(link, line) => {
                    let written = links.get_mut(&link).map(|file| file.write_all(&line));
                    match written {
                        Some(Ok(())) => crate::say(&format!("{link} ready")),
                        Some(Err(error)) => {
                            crate::say(&format!("{link} write failed: {error}"));
                            close(links, link);
                        }
                        None => {}
                    }
                }
                Action::Close(link, why) => {
                    crate::say(&format!("{link} closed: {why}"));
                    close(links, link);
                }
                Action::Ignore(link, ignored) => crate::say(&format!("{link}: {ignored}")),
                Action::PowerOff(link) => return Some(format!("stop on {link}")),
            }
        }
        None
    }

    /// Shuts the link down, so its reader thread reads an EOF and ends,
    /// and drops the write end.
    fn close(links: &mut HashMap<LinkId, File>, link: LinkId) {
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
    fn accept_loop(listener: &OwnedFd, events: &Sender<Event>) {
        loop {
            match accept_with(listener, SocketFlags::CLOEXEC) {
                Ok(fd) => {
                    if events.send(Event::Accepted(fd)).is_err() {
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

    /// Starts the reader thread of `link` and returns the write end.
    fn start_reader(link: LinkId, fd: OwnedFd, events: &Sender<Event>) -> io::Result<File> {
        let file = File::from(fd);
        let reader = file.try_clone()?;
        let events = events.clone();
        thread::Builder::new()
            .name(format!("control-{}", link.0))
            .spawn(move || read_loop(link, reader, &events))?;
        Ok(file)
    }

    /// Reads the lines of one link until its end or a refused line.
    fn read_loop(link: LinkId, mut reader: File, events: &Sender<Event>) {
        let mut lines = Lines::default();
        let mut chunk = [0_u8; CHUNK];
        loop {
            let read = match reader.read(&mut chunk) {
                Ok(0) => {
                    let end = match lines.end() {
                        Ok(()) => Event::Ended(link),
                        Err(why) => Event::Refused(link, why),
                    };
                    let _ = events.send(end);
                    return;
                }
                Ok(read) => read,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    crate::say(&format!("{link} read failed: {error}"));
                    let _ = events.send(Event::Ended(link));
                    return;
                }
            };
            match lines.push(chunk.get(..read).unwrap_or_default()) {
                Ok(each) => {
                    for line in each {
                        if events.send(Event::Line(link, line)).is_err() {
                            return;
                        }
                    }
                }
                Err(why) => {
                    let _ = events.send(Event::Refused(link, why));
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

    fn ready_line() -> Vec<u8> {
        let mut line = br#"{"facade":{"not_started":{}}}"#.to_vec();
        line.push(b'\n');
        line
    }

    fn stop() -> Vec<u8> {
        serde_json::to_vec(&ControlCommand::Stop(ControlStop {})).unwrap()
    }

    /// A link that got its ready.
    fn ready(control: &mut Control) -> LinkId {
        let (link, _) = control.accept();
        assert_eq!(
            control.line(link, &hello(SESSION)),
            Some(Action::SetClock(link, at()))
        );
        assert_eq!(
            control.clock_set(link, Ok(())),
            Some(Action::Ready(link, ready_line()))
        );
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

        // The ready reports not_started: no facade starts before plan PR
        // G4.
        let mut control = Control::default();
        let link = ready(&mut control);
        assert_eq!(control.line(link, &stop()), Some(Action::PowerOff(link)));
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

    #[test]
    fn a_sync_is_ignored_until_plan_pr_g8() {
        let mut control = Control::default();
        let link = ready(&mut control);
        assert_eq!(
            control.line(link, br#"{"sync":{}}"#),
            Some(Action::Ignore(link, Ignored::Sync))
        );
        assert_eq!(control.line(link, &stop()), Some(Action::PowerOff(link)));
    }
}
