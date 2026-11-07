//! The control link to guest init on guest port 1200 (decisions CH13,
//! CH14). The vmm dials through the vsock socket of the VM's Cloud
//! Hypervisor: it writes `CONNECT 1200` and waits for `OK`. It then sends
//! `ControlHello` and reads one `ControlReady`. The framing is the one of
//! "The vsock JSON links" in `wire/AGENTS.md` (decision CH36 G11).
//!
//! After `ControlReady`, guest init sends a `GuestEvent` line at each
//! change of the facade state and for each turn event of the facade
//! (decisions CH21, CH62 rule 11), and the vmm may send the graceful stop
//! (decision CH19 (3)) or a `sync` before a take (decision CH62 rule 23).
//! `ControlLink::split` gives a reader of the guest events and a
//! `StopSender` for the stop and the sync. `ControlState` keeps the facade
//! state and applies each turn end to the session row.

use std::borrow::Borrow;
use std::collections::HashMap;
use std::io;
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use creche_wire::{
    CONTROL_LINE_CAP, ControlCommand, ControlHello, ControlReady, ControlStop, ControlSync,
    FacadeState, GuestEvent, GuestPort, SessionId, TurnEnd, TurnEvent, UnixMillis, strict,
};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::net::UnixStream;
use tokio::sync::oneshot;
use tokio::time::{sleep, timeout};

/// How long an open of the control link may take, from the first dial to
/// `ControlReady` (decision CH36 G26: 10 s). A boot takes about 0.3 s, and
/// guest init answers before its facade runs (decision CH34 rule 7).
pub const READY_WAIT: Duration = Duration::from_secs(10);

/// The pause between two dials.
// Decision CH43 rule 5: the pause is 10 ms. Fact 74 saw `OK` 45 to 51 ms
// after the start of a restore, so that is about 5 tries per restore.
// Only the vmm reads it, so a change costs one constant.
pub(crate) const DIAL_PAUSE: Duration = Duration::from_millis(10);

/// How long a take waits for `synced` after it sends `sync` (decision
/// CH62 rule 23: 10 s). The bound covers the write of `sync` too. No
/// `synced` in time means no snapshot, and the graceful stop runs.
pub const SYNC_WAIT: Duration = Duration::from_secs(10);

/// The most bytes of the answer of Cloud Hypervisor to `CONNECT`, the LF
/// not counted. `OK ` and a 32-bit port number fit with room to spare.
const CONNECT_REPLY_CAP: usize = 32;

/// Why the control link did not open.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControlError {
    /// No `ControlReady` came within `READY_WAIT`, or no `synced` within
    /// `SYNC_WAIT`.
    Timeout,
    /// The host clock is before 1970 or past the range of `UnixMillis`.
    Clock,
    /// The dial failed with an error that is not retried.
    Dial(io::ErrorKind),
    /// Cloud Hypervisor answered `CONNECT` with a line that is not `OK`.
    Connect,
    /// Guest init closed the link before `ControlReady`. It does so when
    /// the hello names another session than the VM serves (decision CH32
    /// rule 3).
    Closed,
    /// Guest init closed the link at a line end after `ControlReady`. A
    /// power-off after `stop` ends a link so, and so does a restore that
    /// replaced the link (fact 74). It asks for no cold boot.
    Ended,
    /// A line broke the framing or is not the type that the link expects.
    /// The vmm closes the link (decision CH43 rule 2, `wire/AGENTS.md`,
    /// rule 6 of "The vsock JSON links").
    Line,
    /// A read or a write failed.
    Io(io::ErrorKind),
}

impl ControlError {
    /// Whether the VM needs a cold boot. A hello that guest init refuses
    /// is a fault of the VM (decision CH32 rule 3).
    // Decision CH43 rule 6: a cold boot follows only when guest init
    // closes the link before `ControlReady`. So only `Closed` asks for a
    // cold boot. The rule names four cases that end the open with a
    // reported fault and no cold boot: a bad line, a timeout, a refused
    // `CONNECT` and a failed dial. The rule does not name `Io` (a failed
    // read or write) or `Clock`. The code reads "only" to cover them too,
    // so each of them also ends the open with a reported fault and no
    // cold boot.
    #[must_use]
    pub fn needs_cold_boot(self) -> bool {
        self == Self::Closed
    }
}

/// The wall time of the vmm, for `ControlHello` (decision CH28).
///
/// # Errors
///
/// `Clock` when the host clock is outside the range of `UnixMillis`.
pub fn wall_time() -> Result<UnixMillis, ControlError> {
    let since = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ControlError::Clock)?;
    let millis = u64::try_from(since.as_millis()).map_err(|_| ControlError::Clock)?;
    UnixMillis::try_from(millis).map_err(|_| ControlError::Clock)
}

/// An open control link. It keeps the bytes that guest init sent after
/// `ControlReady` for the next read. `S` holds the stream: the stream
/// itself, or the `Arc` of the reader after `split`.
#[derive(Debug)]
pub struct ControlLink<S = UnixStream> {
    stream: S,
    pending: Vec<u8>,
}

impl ControlLink {
    /// A link on a stream that a host listener accepted. The family-state
    /// link of host port 1100 has the framing of this link ("The vsock
    /// JSON links" in `wire/AGENTS.md`), so it reads and writes through
    /// this type (`family_state`).
    pub(crate) fn accepted(stream: UnixStream) -> Self {
        Self {
            stream,
            pending: Vec::new(),
        }
    }

    /// The stream and the bytes that came after the answer to `CONNECT`.
    /// The ACP relay copies both (plan PR L13).
    pub(crate) fn into_parts(self) -> (UnixStream, Vec<u8>) {
        (self.stream, self.pending)
    }

    /// Splits the link after `ControlReady`: the sender of the graceful
    /// stop and of the sync, and the reader of the guest events (plan PR
    /// L18b). The reader holds the only strong handles of the stream and
    /// of the sync waiter, so its end closes the link (decision CH43 rule
    /// 2), and the sender then sends nothing.
    #[must_use]
    pub fn split(self) -> (StopSender, GuestEvents) {
        let stream = Arc::new(self.stream);
        let waiter = Arc::new(Mutex::new(None));
        let sender = StopSender {
            stream: Arc::downgrade(&stream),
            waiter: Arc::downgrade(&waiter),
        };
        let link = ControlLink {
            stream,
            pending: self.pending,
        };
        (sender, GuestEvents { link, waiter })
    }
}

impl<S: Borrow<UnixStream>> ControlLink<S> {
    /// Writes `value` as one compact JSON line (rule 1 of "The vsock JSON
    /// links").
    pub(crate) async fn write<T: Serialize>(&self, value: &T) -> Result<(), ControlError> {
        let mut line = serde_json::to_vec(value).map_err(|_| ControlError::Line)?;
        line.push(b'\n');
        self.write_raw(&line).await
    }

    /// Writes all of `bytes`.
    pub(crate) async fn write_raw(&self, bytes: &[u8]) -> Result<(), ControlError> {
        let mut left = bytes;
        while !left.is_empty() {
            self.stream.borrow().writable().await.map_err(io_error)?;
            match self.stream.borrow().try_write(left) {
                Ok(written) => left = left.get(written..).unwrap_or_default(),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(io_error(error)),
            }
        }
        Ok(())
    }

    /// Reads one line as `T` with the strict reader (rules 2 and 3 of "The
    /// vsock JSON links").
    pub(crate) async fn read<T: DeserializeOwned>(&mut self) -> Result<T, ControlError> {
        let line = self.line(CONTROL_LINE_CAP).await?;
        if !strict(&line) {
            return Err(ControlError::Line);
        }
        serde_json::from_slice(&line).map_err(|_| ControlError::Line)
    }

    /// Reads the next line that guest init sends after `ControlReady`: a
    /// facade state, a `synced` or a relayed turn event (decisions CH21,
    /// CH62 rules 11 and 23).
    ///
    /// # Errors
    ///
    /// `Ended` when guest init closed the link at a line end. `Line` for
    /// a line that is not a `GuestEvent`. The caller then closes the link.
    pub async fn event(&mut self) -> Result<GuestEvent, ControlError> {
        self.next_event().await.map_err(|end| match end {
            EventEnd::Refused(_) => ControlError::Line,
            EventEnd::Error(error) => error,
        })
    }

    /// Reads the next guest event as `event` does. A refused line comes
    /// back with its first `REFUSED_LOG_CAP` bytes, so the caller logs it
    /// (decision CH54 rule 6).
    ///
    /// # Errors
    ///
    /// `EventEnd::Refused` for a line that is not a `GuestEvent`, and
    /// `EventEnd::Error` with the error of `event` for every other end.
    pub(crate) async fn next_event(&mut self) -> Result<GuestEvent, EventEnd> {
        let line = match self.line(CONTROL_LINE_CAP).await {
            Ok(line) => line,
            Err(ControlError::Closed) => return Err(EventEnd::Error(ControlError::Ended)),
            // A line past the cap stays in `pending`.
            Err(ControlError::Line) => return Err(refused(&self.pending)),
            Err(error) => return Err(EventEnd::Error(error)),
        };
        if !strict(&line) {
            return Err(refused(&line));
        }
        serde_json::from_slice(&line).map_err(|_| refused(&line))
    }

    /// Sends the graceful stop: guest init stops the facade, syncs and
    /// powers off (decision CH19 (3)). The line is `{"stop":{}}` (decision
    /// CH36 G11).
    ///
    /// # Errors
    ///
    /// `Io` when the write failed.
    pub async fn stop(&self) -> Result<(), ControlError> {
        self.write(&ControlCommand::Stop(ControlStop {})).await
    }

    /// Reads one line of at most `cap` bytes and drops its LF. An end of
    /// the stream before any byte of the line is `Closed`. An end inside a
    /// line, or more than `cap` bytes with no LF, is `Line`.
    pub(crate) async fn line(&mut self, cap: usize) -> Result<Vec<u8>, ControlError> {
        let mut chunk = [0_u8; 4096];
        loop {
            if let Some(end) = self.pending.iter().position(|&byte| byte == b'\n') {
                if end > cap {
                    return Err(ControlError::Line);
                }
                let mut line: Vec<u8> = self.pending.drain(..=end).collect();
                line.pop();
                return Ok(line);
            }
            if self.pending.len() > cap {
                return Err(ControlError::Line);
            }
            self.stream.borrow().readable().await.map_err(io_error)?;
            match self.stream.borrow().try_read(&mut chunk) {
                Ok(0) if self.pending.is_empty() => return Err(ControlError::Closed),
                Ok(0) => return Err(ControlError::Line),
                Ok(read) => self
                    .pending
                    .extend_from_slice(chunk.get(..read).unwrap_or_default()),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(io_error(error)),
            }
        }
    }
}

/// The most bytes of a refused line that the vmm logs. A line may hold
/// up to `CONTROL_LINE_CAP` bytes. No row sets it. Only the vmm reads it,
/// so a change costs one constant.
pub(crate) const REFUSED_LOG_CAP: usize = 512;

/// Why `ControlLink::next_event` read no guest event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum EventEnd {
    /// The strict reader refused a line (decision CH43 rule 2). It holds
    /// the first `REFUSED_LOG_CAP` bytes of the line, for the log.
    Refused(Vec<u8>),
    /// Any other end, as `ControlLink::event` gives it.
    Error(ControlError),
}

/// The refused `line`, cut to `REFUSED_LOG_CAP` bytes.
fn refused(line: &[u8]) -> EventEnd {
    EventEnd::Refused(line.iter().take(REFUSED_LOG_CAP).copied().collect())
}

/// The waiter of the `synced` that answers one `sync`.
type SyncWaiter = Mutex<Option<oneshot::Sender<()>>>;

/// The lock of `waiter`. It holds one sender, so a poisoned lock is whole.
fn lock_waiter(waiter: &SyncWaiter) -> MutexGuard<'_, Option<oneshot::Sender<()>>> {
    waiter.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The reader of the guest events of a split control link (plan PR
/// L18b). It owns the stream and the sync waiter, so its end closes the
/// link and ends a `StopSender::sync` that waits.
#[derive(Debug)]
pub struct GuestEvents {
    link: ControlLink<Arc<UnixStream>>,
    waiter: Arc<SyncWaiter>,
}

impl GuestEvents {
    /// Reads the next guest event, as `ControlLink::next_event` does.
    pub(crate) async fn next_event(&mut self) -> Result<GuestEvent, EventEnd> {
        self.link.next_event().await
    }

    /// Hands a `synced` line to the `StopSender::sync` that waits for it
    /// (decision CH62 rule 23). False when none waits: no take sent
    /// `sync`, or its `SYNC_WAIT` passed. The caller then logs the line
    /// and reads on.
    pub(crate) fn synced(&self) -> bool {
        lock_waiter(&self.waiter)
            .take()
            .is_some_and(|waiter| waiter.send(()).is_ok())
    }
}

/// The sender of the graceful stop and of the sync of a split control
/// link. It holds weak handles, so the end of the reader closes the link.
#[derive(Debug)]
pub struct StopSender {
    stream: Weak<UnixStream>,
    waiter: Weak<SyncWaiter>,
}

impl StopSender {
    /// Whether the reader of the link still runs, so the link is open.
    #[must_use]
    pub fn is_open(&self) -> bool {
        self.stream.strong_count() > 0
    }

    /// Sends `{"stop":{}}` as `ControlLink::stop` does.
    ///
    /// # Errors
    ///
    /// `Io` with `NotConnected` when the reader ended and closed the link,
    /// or when the write failed.
    pub async fn stop(&self) -> Result<(), ControlError> {
        let stream = self
            .stream
            .upgrade()
            .ok_or(ControlError::Io(io::ErrorKind::NotConnected))?;
        let link = ControlLink {
            stream,
            pending: Vec::new(),
        };
        link.stop().await
    }

    /// Syncs the guest before a take (item 5.7 of the design, decision
    /// CH62 rule 23). It registers the waiter of the next `synced`, sends
    /// `{"sync":{}}`, and waits for the reader to hand it that `synced`.
    /// Guest init runs syncfs of the session disk, then sync. `SYNC_WAIT`
    /// bounds the write and the wait. The sender keeps no strong handle
    /// while it waits, so the end of the reader ends the wait at once.
    ///
    /// # Errors
    ///
    /// `Timeout` when no `synced` came within `SYNC_WAIT`. `Io` with
    /// `NotConnected` when the reader ended, before or during the wait.
    /// `Io` when the write failed.
    pub async fn sync(&self) -> Result<(), ControlError> {
        let closed = ControlError::Io(io::ErrorKind::NotConnected);
        let (Some(stream), Some(waiter)) = (self.stream.upgrade(), self.waiter.upgrade()) else {
            return Err(closed);
        };
        let (sender, synced) = oneshot::channel();
        *lock_waiter(&waiter) = Some(sender);
        drop(waiter);
        let link = ControlLink {
            stream,
            pending: Vec::new(),
        };
        let sync = async move {
            link.write(&ControlCommand::Sync(ControlSync {})).await?;
            drop(link);
            synced.await.map_err(|_| closed)
        };
        timeout(SYNC_WAIT, sync)
            .await
            .unwrap_or(Err(ControlError::Timeout))
    }
}

/// The error of a failed read or write.
fn io_error(error: io::Error) -> ControlError {
    ControlError::Io(error.kind())
}

/// Opens the control link of the VM that serves `session` through its
/// vsock socket `vsock`, with `ControlHello` stamped by `clock`. Returns
/// the link and the first `ControlReady`. The whole open takes at most
/// `READY_WAIT`. Each new link sends the wall time of the vmm (decision
/// CH28). The caller gives the link a new `LinkId` first, with
/// `ControlState::replace`.
///
/// # Errors
///
/// `ControlError`. `Closed` asks for a cold boot.
pub async fn open(
    vsock: &Path,
    session: &SessionId,
    clock: impl FnOnce() -> Result<UnixMillis, ControlError>,
) -> Result<(ControlLink, ControlReady), ControlError> {
    open_within(vsock, session, clock, READY_WAIT).await
}

/// `open` with the bound `wait` in place of `READY_WAIT`, so a test can
/// hold the bound with a short wait.
async fn open_within(
    vsock: &Path,
    session: &SessionId,
    clock: impl FnOnce() -> Result<UnixMillis, ControlError>,
    wait: Duration,
) -> Result<(ControlLink, ControlReady), ControlError> {
    let open = async {
        let mut link = dial(vsock, GuestPort::Control).await?;
        let hello = ControlHello {
            sent_at: clock()?,
            session: session.clone(),
        };
        link.write(&hello).await?;
        let ready = link.read::<ControlReady>().await?;
        Ok((link, ready))
    };
    timeout(wait, open)
        .await
        .unwrap_or(Err(ControlError::Timeout))
}

/// Dials `port` through the vsock socket of Cloud Hypervisor. It retries
/// while the socket does not exist (ENOENT) and after an empty reply,
/// because both come before the guest listener exists (fact 74). It
/// retries on ECONNREFUSED too. It has no bound of its own, so the
/// caller bounds it with `READY_WAIT`. Both guest ports use it (plan PR L13).
// Decision CH43 rule 7: the dial retries ECONNREFUSED inside `READY_WAIT`,
// the same way as ENOENT. A socket file exists from `bind` on, and a
// connect gets ECONNREFUSED until Cloud Hypervisor calls `listen`. A
// socket file that a dead Cloud Hypervisor left behind gets it too.
// `READY_WAIT` still bounds the open, so a stale socket ends as `Timeout`.
pub(crate) async fn dial(vsock: &Path, port: GuestPort) -> Result<ControlLink, ControlError> {
    let connect = format!("CONNECT {}\n", port.number());
    loop {
        let stream = match UnixStream::connect(vsock).await {
            Ok(stream) => stream,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused
                ) =>
            {
                sleep(DIAL_PAUSE).await;
                continue;
            }
            Err(error) => return Err(ControlError::Dial(error.kind())),
        };
        let mut link = ControlLink {
            stream,
            pending: Vec::new(),
        };
        link.write_raw(connect.as_bytes()).await?;
        match link.line(CONNECT_REPLY_CAP).await {
            Ok(reply) if connected(&reply) => return Ok(link),
            Ok(_) | Err(ControlError::Line) => return Err(ControlError::Connect),
            Err(ControlError::Closed) => sleep(DIAL_PAUSE).await,
            Err(error) => return Err(error),
        }
    }
}

/// Whether `reply` is `OK <host port>`, the answer of Cloud Hypervisor to
/// a `CONNECT` that reached a guest listener.
fn connected(reply: &[u8]) -> bool {
    reply
        .strip_prefix(b"OK ")
        .is_some_and(|port| !port.is_empty() && port.iter().all(u8::is_ascii_digit))
}

/// The identity of one control link. A new link of a VM gets a new one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LinkId(u64);

/// The control state of one VM.
#[derive(Debug, Default)]
struct VmControl {
    link: Option<LinkId>,
    facade: Option<FacadeState>,
    turn_running: bool,
    /// The time of the last turn event (decision CH50 rule 5).
    last_turn: Option<Instant>,
    /// The link that closed on a refused line (decision CH54 rule 6).
    refused: Option<LinkId>,
}

/// The last turn end that `facade` holds. Only a state that can follow a
/// turn holds one (decision CH32 rule 5).
fn last_turn_end(facade: &FacadeState) -> Option<&TurnEnd> {
    match facade {
        FacadeState::NotStarted(_) => None,
        FacadeState::Starting(starting) => starting.last_turn_end.as_ref(),
        FacadeState::Running(running) => running.last_turn_end.as_ref(),
        FacadeState::Exited(exited) => exited.last_turn_end.as_ref(),
    }
}

/// What a change that names a link did.
#[derive(Debug, Clone, PartialEq, Eq)]
#[must_use]
pub enum Change {
    /// The link is not the current link of the VM. Nothing changed.
    Stale,
    /// The link is current, and the state changed. `Some` holds a turn
    /// end for the row of the VM. The caller writes it with
    /// `creche_vmm_store::rows::Rows::turn_end`. That write syncs a file
    /// and a dir, so the caller runs it in `spawn_blocking` (decision CH36
    /// G21) and holds no lock of `ControlState` across it.
    Applied(Option<TurnEnd>),
}

/// The control state of every VM, keyed on the session that the VM serves
/// (decision CH1). Each change names the link it came from, and only the
/// current link changes the state. So the late EOF of a replaced link
/// changes nothing (decision CH28, fact 74).
#[derive(Debug, Default)]
pub struct ControlState {
    next: u64,
    vms: HashMap<SessionId, VmControl>,
}

impl ControlState {
    /// Makes a new link the current link of `vm`. The old link, if any,
    /// changes nothing after this (decision CH19 (2)).
    pub fn replace(&mut self, vm: &SessionId) -> LinkId {
        self.next += 1;
        let link = LinkId(self.next);
        self.vms.entry(vm.clone()).or_default().link = Some(link);
        link
    }

    /// The state of `vm` when `link` is its current link.
    fn current(&mut self, vm: &SessionId, link: LinkId) -> Option<&mut VmControl> {
        self.vms
            .get_mut(vm)
            .filter(|control| control.link == Some(link))
    }

    /// Keeps the facade state of `ready` when `link` is current, as
    /// `report` does. A restore can drop a turn event of the old link
    /// (fact 45), so the end arrives here (decision CH32 rule 4). A failed
    /// write changes no state here, and the next `ControlReady` carries
    /// the end again.
    pub fn ready(&mut self, vm: &SessionId, link: LinkId, ready: ControlReady) -> Change {
        self.report(vm, link, ready.facade)
    }

    /// Keeps `facade`, the state of a `ControlReady` or of a `facade` line
    /// of `link`, when `link` is current (decisions CH19 rule 2, CH62 rule
    /// 11). The turn state follows the flag of a `running` state. Any
    /// other state runs no turn, so a facade that leaves `running` clears
    /// `turn_running`, and a facade that exited mid-turn blocks no
    /// snapshot. Returns `Applied` with the last turn end of the state,
    /// which the caller writes to the row of `vm`. A repeat of a seen end
    /// writes nothing.
    pub fn report(&mut self, vm: &SessionId, link: LinkId, facade: FacadeState) -> Change {
        let Some(control) = self.current(vm, link) else {
            return Change::Stale;
        };
        control.turn_running =
            matches!(&facade, FacadeState::Running(running) if running.turn_running);
        let facade = control.facade.insert(facade);
        Change::Applied(last_turn_end(facade).cloned())
    }

    /// Applies a turn event of `link` when `link` is current (decision
    /// CH21). A start marks a turn running. An end clears the mark and
    /// returns `Applied` with the end. The caller writes it to the row of
    /// `vm`: that sets `updated_at`, and it sets the title only while that
    /// is `null` (decision CH32 rule 4). An event of a replaced link
    /// changes nothing: the `ControlReady` of the new link carries its end
    /// (fact 74).
    pub fn turn(&mut self, vm: &SessionId, link: LinkId, event: &TurnEvent) -> Change {
        let Some(control) = self.current(vm, link) else {
            return Change::Stale;
        };
        control.turn_running = matches!(event, TurnEvent::Start(_));
        // The tokio clock, as the door close time of `Relays::door`, so a
        // test on paused time sees one clock.
        control.last_turn = Some(tokio::time::Instant::now().into_std());
        match event {
            TurnEvent::Start(_) => Change::Applied(None),
            TurnEvent::End(end) => Change::Applied(Some(end.clone())),
        }
    }

    /// Marks the close of `link` when it is current. Returns whether it
    /// did. The last facade state stays: a link close never stops the
    /// facade (decision CH19 (1)). The turn state stays too: the turn goes
    /// on in the guest, and the next `ControlReady` reports it.
    pub fn closed(&mut self, vm: &SessionId, link: LinkId) -> bool {
        match self.vms.get_mut(vm) {
            Some(control) if control.link == Some(link) => {
                control.link = None;
                true
            }
            _ => false,
        }
    }

    /// Marks the close of `link` after a refused line when it is current
    /// (decision CH54 rule 6), as `closed` does. A retire then reads the
    /// mark with `refused`. Returns whether it did.
    pub fn refuse(&mut self, vm: &SessionId, link: LinkId) -> bool {
        let current = self.closed(vm, link);
        if let Some(control) = self.vms.get_mut(vm).filter(|_| current) {
            control.refused = Some(link);
        }
        current
    }

    /// Whether `link` of `vm` closed on a refused line (decision CH54
    /// rule 6).
    #[must_use]
    pub fn refused(&self, vm: &SessionId, link: LinkId) -> bool {
        self.vms
            .get(vm)
            .is_some_and(|control| control.refused == Some(link))
    }

    /// The current link of `vm`, if one is open.
    #[must_use]
    pub fn link(&self, vm: &SessionId) -> Option<LinkId> {
        self.vms.get(vm).and_then(|control| control.link)
    }

    /// The last facade state that a current link of `vm` reported, in its
    /// `ControlReady` or in a later `facade` line (decision CH62 rule 11).
    /// A turn event does not change it: `turn_running` follows the turn
    /// events.
    #[must_use]
    pub fn facade(&self, vm: &SessionId) -> Option<&FacadeState> {
        self.vms.get(vm).and_then(|control| control.facade.as_ref())
    }

    /// Whether a turn runs in `vm`: the last facade state or turn event
    /// of a current link said so (decisions CH21, CH19 (6)).
    #[must_use]
    pub fn turn_running(&self, vm: &SessionId) -> bool {
        self.vms.get(vm).is_some_and(|control| control.turn_running)
    }

    /// The time of the last turn event of `vm`, for its last use
    /// (decision CH50 rule 5).
    #[must_use]
    pub fn last_turn(&self, vm: &SessionId) -> Option<Instant> {
        self.vms.get(vm).and_then(|control| control.last_turn)
    }
}

#[cfg(test)]
mod tests {
    use std::future::{Future, poll_fn};
    use std::io::{ErrorKind, Read, Write};
    use std::os::unix::net::UnixListener;
    use std::pin::pin;
    use std::task::Poll;
    use std::thread::spawn;
    use std::time::Duration;

    use creche_wire::{CONTROL_LINE_CAP, GuestEvent, UnixMillis};
    use tokio::net::UnixStream;
    use tokio::runtime::{Builder, Runtime};
    use tokio::time::Instant;

    use super::{ControlError, ControlLink, GuestEvents, SYNC_WAIT, open_within};

    /// A guest that takes the hello and never answers ends the open at
    /// the bound (decision CH36 G26).
    #[test]
    fn a_silent_guest_ends_the_open_at_the_bound() {
        let dir = std::env::temp_dir().join(format!("creche-vmm-{}-silent", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("vsock.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let guest = spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut byte = [0_u8];
            while stream.read(&mut byte).unwrap() == 1 && byte != *b"\n" {}
            stream.write_all(b"OK 1073741824\n").unwrap();
            // Read the hello, then stay silent until the vmm closes.
            while matches!(stream.read(&mut byte), Ok(1)) {}
        });

        let runtime = Builder::new_current_thread()
            .enable_io()
            .enable_time()
            .build()
            .unwrap();
        let session = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let clock = || UnixMillis::try_from(1_793_577_600_000).map_err(|_| ControlError::Clock);
        // The outer 1 s bound ends the test early when the inner bound is
        // gone.
        let open = async {
            let wait = Duration::from_millis(50);
            let open = open_within(&path, &session, clock, wait);
            tokio::time::timeout(Duration::from_secs(1), open).await
        };
        let error = runtime.block_on(open).unwrap().map(|_| ()).unwrap_err();
        assert_eq!(error, ControlError::Timeout);
        guest.join().unwrap();
    }

    /// A runtime on real time. A test whose proof is that the sync ends
    /// before `SYNC_WAIT` uses it: on paused time the runtime can jump the
    /// clock past `SYNC_WAIT` before the socket event of the closed link
    /// reaches the reader, which the rust job saw on Linux (run
    /// 42395164546).
    fn real() -> Runtime {
        Builder::new_current_thread()
            .enable_io()
            .enable_time()
            .build()
            .unwrap()
    }

    /// A runtime on paused time, so a wait of `SYNC_WAIT` costs no real
    /// time.
    fn paused() -> Runtime {
        Builder::new_current_thread()
            .enable_io()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap()
    }

    /// Reads each guest event as the reader of the vmm does, and hands
    /// each `synced` to a waiting sync. Returns each event with whether a
    /// sync took it, until the link ends.
    async fn read_all(mut events: GuestEvents) -> Vec<(GuestEvent, bool)> {
        let mut read = Vec::new();
        while let Ok(event) = events.next_event().await {
            let took = matches!(event, GuestEvent::Synced(_)) && events.synced();
            read.push((event, took));
        }
        read
    }

    /// A socket pair: the split vmm end and the guest end.
    fn pair() -> (super::StopSender, GuestEvents, ControlLink) {
        let (vmm, guest) = UnixStream::pair().unwrap();
        let (sender, events) = ControlLink::accepted(vmm).split();
        (sender, events, ControlLink::accepted(guest))
    }

    const SYNCED: &[u8] = b"{\"synced\":{}}\n";
    const RUNNING: &[u8] = b"{\"facade\":{\"running\":{\"last_turn_end\":null,\"pid\":42,\"starts\":1,\"turn_running\":false}}}\n";

    /// `sync` sends one `sync` line, and the `synced` of the guest ends it
    /// (decision CH62 rule 23).
    #[test]
    fn sync_sends_one_line_and_takes_the_synced() {
        paused().block_on(async {
            let (sender, events, mut guest) = pair();
            let reader = tokio::spawn(read_all(events));
            let answer = tokio::spawn(async move {
                let line = guest.line(CONTROL_LINE_CAP).await.unwrap();
                guest.write_raw(SYNCED).await.unwrap();
                line
            });
            assert_eq!(sender.sync().await, Ok(()));
            assert_eq!(answer.await.unwrap(), br#"{"sync":{}}"#);
            drop(sender);
            let read = reader.await.unwrap();
            assert!(
                matches!(&read[..], [(GuestEvent::Synced(_), true)]),
                "{read:?}"
            );
        });
    }

    /// `sync` registers its waiter before it writes `sync`, so a `synced`
    /// that comes while the write still waits ends the sync (decision
    /// CH62 rule 23). The test fills the send buffer of the vmm end, so
    /// the write of `sync` must wait. One poll of the sync then parks on
    /// that write. The reader takes a `synced` in that state, and the
    /// guest end drains the buffer only after it.
    #[test]
    fn a_synced_during_the_write_ends_the_sync() {
        paused().block_on(async {
            let (vmm, guest) = UnixStream::pair().unwrap();
            let link = ControlLink::accepted(vmm);
            let filler = [b'x'; 4096];
            let mut filled = 0;
            loop {
                match link.stream.try_write(&filler) {
                    Ok(written) => filled += written,
                    Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                    Err(error) => panic!("the fill: {error}"),
                }
            }
            let (sender, mut events) = link.split();
            let guest = ControlLink::accepted(guest);

            let mut sync = pin!(sender.sync());
            let first = poll_fn(|cx| Poll::Ready(sync.as_mut().poll(cx))).await;
            assert!(first.is_pending());
            guest.write_raw(SYNCED).await.unwrap();
            let event = events.next_event().await.unwrap();
            assert!(matches!(event, GuestEvent::Synced(_)), "{event:?}");
            assert!(events.synced());

            let line = br#"{"sync":{}}"#.len() + 1;
            let drain = tokio::spawn(async move {
                let mut read = Vec::new();
                let mut buffer = [0_u8; 4096];
                while read.len() < filled + line {
                    guest.stream.readable().await.unwrap();
                    match guest.stream.try_read(&mut buffer) {
                        Ok(0) => break,
                        Ok(count) => read.extend_from_slice(&buffer[..count]),
                        Err(error) if error.kind() == ErrorKind::WouldBlock => {}
                        Err(error) => panic!("the drain: {error}"),
                    }
                }
                read.split_off(filled)
            });
            assert_eq!(sync.await, Ok(()));
            assert_eq!(drain.await.unwrap(), b"{\"sync\":{}}\n");
        });
    }

    /// A `synced` after `SYNC_WAIT` finds no waiter, so it changes
    /// nothing: the sync ended with `Timeout`, and the reader reads on
    /// (decision CH62 rule 23).
    #[test]
    fn a_late_synced_after_the_wait_changes_nothing() {
        paused().block_on(async {
            let (sender, events, mut guest) = pair();
            let reader = tokio::spawn(read_all(events));
            let answer = tokio::spawn(async move {
                guest.line(CONTROL_LINE_CAP).await.unwrap();
                tokio::time::sleep(SYNC_WAIT + Duration::from_secs(1)).await;
                guest.write_raw(SYNCED).await.unwrap();
                guest.write_raw(RUNNING).await.unwrap();
            });
            let start = Instant::now();
            assert_eq!(sender.sync().await, Err(ControlError::Timeout));
            assert_eq!(start.elapsed(), SYNC_WAIT);
            answer.await.unwrap();
            let read = reader.await.unwrap();
            assert!(
                matches!(
                    &read[..],
                    [
                        (GuestEvent::Synced(_), false),
                        (GuestEvent::Facade(_), false)
                    ]
                ),
                "{read:?}"
            );
        });
    }

    /// The end of the reader ends a sync that waits at once, with no wait
    /// of `SYNC_WAIT`. A later sync finds the link closed.
    #[test]
    fn a_closed_link_ends_the_sync_at_once() {
        real().block_on(async {
            let (sender, events, mut guest) = pair();
            let reader = tokio::spawn(read_all(events));
            let answer = tokio::spawn(async move {
                guest.line(CONTROL_LINE_CAP).await.unwrap();
            });
            let start = Instant::now();
            let closed = ControlError::Io(std::io::ErrorKind::NotConnected);
            assert_eq!(sender.sync().await, Err(closed));
            assert!(start.elapsed() < SYNC_WAIT);
            answer.await.unwrap();
            assert!(reader.await.unwrap().is_empty());
            assert_eq!(sender.sync().await, Err(closed));
        });
    }
}
