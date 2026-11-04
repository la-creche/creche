//! The control link to guest init on guest port 1200 (decisions CH13,
//! CH14). The vmm dials through the vsock socket of the VM's Cloud
//! Hypervisor: it writes `CONNECT 1200` and waits for `OK`. It then sends
//! `ControlHello` and reads one `ControlReady`. The framing is the one of
//! "The vsock JSON links" in `wire/AGENTS.md` (decision CH36 G11).

use std::collections::HashMap;
use std::io;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use creche_wire::{
    CONTROL_LINE_CAP, ControlHello, ControlReady, FacadeState, GuestPort, SessionId, UnixMillis,
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
    timeout(READY_WAIT, open)
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

    /// Keeps the facade state of `ready` when `link` is current. Returns
    /// whether it did.
    pub fn ready(&mut self, vm: &SessionId, link: LinkId, ready: ControlReady) -> bool {
        match self.vms.get_mut(vm) {
            Some(control) if control.link == Some(link) => {
                control.facade = Some(ready.facade);
                true
            }
            _ => false,
        }
    }

    /// Marks the close of `link` when it is current. Returns whether it
    /// did. The last facade state stays: a link close never stops the
    /// facade (decision CH19 (1)).
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

    /// The last facade state that a current link of `vm` reported.
    #[must_use]
    pub fn facade(&self, vm: &SessionId) -> Option<&FacadeState> {
        self.vms.get(vm).and_then(|control| control.facade.as_ref())
    }
}
