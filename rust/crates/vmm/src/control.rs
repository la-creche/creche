//! The control link to guest init on guest port 1200 (decisions CH13,
//! CH14). The vmm dials through the vsock socket of the VM's Cloud
//! Hypervisor: it writes `CONNECT 1200` and waits for `OK`. It then sends
//! `ControlHello` and reads one `ControlReady`. The framing is the one of
//! "The vsock JSON links" in `wire/AGENTS.md` (decision CH36 G11).
//!
//! After `ControlReady`, guest init relays each turn event of the facade
//! (decision CH21), and the vmm may send the graceful stop (decision CH19
//! (3)). `ControlState` applies each turn end to the session row.

use std::collections::HashMap;
use std::io;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use creche_vmm_store::rows::{RowWriteError, Rows};
use creche_wire::{
    CONTROL_LINE_CAP, ControlCommand, ControlHello, ControlReady, ControlStop, FacadeState,
    GuestPort, SessionId, TurnEnd, TurnEvent, UnixMillis,
};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::net::UnixStream;
use tokio::time::{sleep, timeout};

use crate::server::strict;

/// How long an open of the control link may take, from the first dial to
/// `ControlReady` (decision CH36 G26: 10 s). A boot takes about 0.3 s, and
/// guest init answers before its facade runs (decision CH34 rule 7).
pub const READY_WAIT: Duration = Duration::from_secs(10);

/// The pause between two dials.
// CONTRACT-QUESTION: no decision row sets the pause between two dials
// (fact 74 says only that the vmm retries). Fact 74 saw `OK` 45 to 51 ms
// after the start of a restore, so the reading is 10 ms: about 5 tries per
// restore. Only the vmm reads it, so a change costs one constant.
const DIAL_PAUSE: Duration = Duration::from_millis(10);

/// The most bytes of the answer of Cloud Hypervisor to `CONNECT`, the LF
/// not counted. `OK ` and a 32-bit port number fit with room to spare.
const CONNECT_REPLY_CAP: usize = 32;

/// Why the control link did not open.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControlError {
    /// No `ControlReady` came within `READY_WAIT`.
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
    /// A line broke the framing or is not the type that the link expects.
    /// The vmm closes the link (`wire/AGENTS.md`, rule 6 of "The vsock
    /// JSON links").
    Line,
    /// A read or a write failed.
    Io(io::ErrorKind),
}

impl ControlError {
    /// Whether the VM needs a cold boot. A hello that guest init refuses
    /// is a fault of the VM (decision CH32 rule 3).
    // CONTRACT-QUESTION: decision CH32 rule 3 asks for a cold boot only
    // when guest init closes the link on a hello for another session. No
    // row decides whether a bad line, a timeout, a refused `CONNECT`, a
    // failed dial or a failed read or write before `ControlReady` asks for
    // one. The reading is the narrowest one: only `Closed` asks for a cold
    // boot. Each other error ends the open with no cold boot, and the
    // caller reports it.
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
/// `ControlReady` for the next read.
#[derive(Debug)]
pub struct ControlLink {
    stream: UnixStream,
    pending: Vec<u8>,
}

impl ControlLink {
    /// Writes `value` as one compact JSON line (rule 1 of "The vsock JSON
    /// links").
    pub(crate) async fn write<T: Serialize>(&self, value: &T) -> Result<(), ControlError> {
        let mut line = serde_json::to_vec(value).map_err(|_| ControlError::Line)?;
        line.push(b'\n');
        self.write_raw(&line).await
    }

    /// Writes all of `bytes`.
    async fn write_raw(&self, bytes: &[u8]) -> Result<(), ControlError> {
        let mut left = bytes;
        while !left.is_empty() {
            self.stream.writable().await.map_err(io_error)?;
            match self.stream.try_write(left) {
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

    /// Reads the next turn event that guest init relays (decision CH21).
    ///
    /// # Errors
    ///
    /// `Closed` when guest init closed the link at a line end. `Line` for
    /// a line that is not a `TurnEvent`. The caller then closes the link.
    pub async fn event(&mut self) -> Result<TurnEvent, ControlError> {
        self.read().await
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
    /// the stream before any byte of the line is `Closed`.
    async fn line(&mut self, cap: usize) -> Result<Vec<u8>, ControlError> {
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
            self.stream.readable().await.map_err(io_error)?;
            match self.stream.try_read(&mut chunk) {
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
/// retries on ECONNREFUSED too.
// CONTRACT-QUESTION: fact 74 names only ENOENT and an empty reply. No
// row says whether the vmm retries on ECONNREFUSED. A socket file exists
// from `bind` on, and a connect gets ECONNREFUSED until Cloud Hypervisor
// calls `listen`. A socket file that a dead Cloud Hypervisor left behind
// gets it too. The reading: retry it the same way as ENOENT. `READY_WAIT`
// still bounds the open, so a stale socket ends as `Timeout`.
async fn dial(vsock: &Path, port: GuestPort) -> Result<ControlLink, ControlError> {
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

    /// Keeps the facade state of `ready` when `link` is current, and
    /// applies its last turn end to the row of `vm`. A restore can drop a
    /// turn event of the old link (fact 45), so the end arrives here
    /// (decision CH32 rule 4). A repeat of a seen end writes nothing.
    /// Returns whether `link` is current.
    ///
    /// # Errors
    ///
    /// A `RowWriteError` when the row write failed. The facade state and
    /// the turn state change all the same. The next `ControlReady` applies
    /// the end again.
    pub fn ready(
        &mut self,
        rows: &mut Rows,
        vm: &SessionId,
        link: LinkId,
        ready: ControlReady,
    ) -> Result<bool, RowWriteError> {
        let Some(control) = self.current(vm, link) else {
            return Ok(false);
        };
        control.turn_running =
            matches!(&ready.facade, FacadeState::Running(running) if running.turn_running);
        let facade = control.facade.insert(ready.facade);
        if let Some(end) = last_turn_end(facade) {
            rows.turn_end(vm, end)?;
        }
        Ok(true)
    }

    /// Applies a turn event of `link` when `link` is current (decision
    /// CH21). A start marks a turn running. An end clears the mark, sets
    /// the row's `updated_at` and sets its title only while that is `null`
    /// (decision CH32 rule 4). An event of a replaced link changes nothing:
    /// the `ControlReady` of the new link carries its end (fact 74).
    /// Returns whether `link` is current.
    ///
    /// # Errors
    ///
    /// A `RowWriteError` when the row write failed. The turn state changes
    /// all the same.
    pub fn turn(
        &mut self,
        rows: &mut Rows,
        vm: &SessionId,
        link: LinkId,
        event: &TurnEvent,
    ) -> Result<bool, RowWriteError> {
        let Some(control) = self.current(vm, link) else {
            return Ok(false);
        };
        control.turn_running = matches!(event, TurnEvent::Start(_));
        if let TurnEvent::End(end) = event {
            rows.turn_end(vm, end)?;
        }
        Ok(true)
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

    /// The current link of `vm`, if one is open.
    #[must_use]
    pub fn link(&self, vm: &SessionId) -> Option<LinkId> {
        self.vms.get(vm).and_then(|control| control.link)
    }

    /// The last facade state that a current link of `vm` reported in its
    /// `ControlReady`. A turn event does not change it.
    // CONTRACT-QUESTION: after `ControlReady`, no message on the link
    // reports a change of the facade state. A turn event shows that the
    // facade runs (decision CH32 rule 6), but it carries no pid and no
    // start count, and an exit sends nothing. The reading: this stays the
    // state of the last `ControlReady`, and `turn_running` follows the
    // events. The sleep gate (decision CH19 (6)) must say how it reads
    // "the facade is up" from the two.
    #[must_use]
    pub fn facade(&self, vm: &SessionId) -> Option<&FacadeState> {
        self.vms.get(vm).and_then(|control| control.facade.as_ref())
    }

    /// Whether a turn runs in `vm`: the last `ControlReady` or turn event
    /// of a current link said so (decisions CH21, CH19 (6)).
    #[must_use]
    pub fn turn_running(&self, vm: &SessionId) -> bool {
        self.vms.get(vm).is_some_and(|control| control.turn_running)
    }
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixListener;
    use std::thread::spawn;
    use std::time::Duration;

    use creche_wire::UnixMillis;
    use tokio::runtime::Builder;

    use super::{ControlError, open_within};

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
}
