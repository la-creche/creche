//! The open ACP relays, one per session (decisions CH26 rule 5, CH33
//! rule 7). An `acp` Upgrade claims its session before the VM starts. A
//! second Upgrade of the session gets `busy` while the claim lives. A
//! drop marks the session first: the mark fires the stop signal of an
//! open relay, and an Upgrade under the mark gets `not_found` (decision
//! CH34 rule 6).
//!
//! The start dials guest port 1201 before 101 (decision CH50 rule 2), and
//! the relay takes that link. After 101 the relay copies the ACP bytes both
//! ways until both sides close (decisions CH13, CH20). It reads no ACP byte
//! and caps none (decision CH34 rule 3).

use std::collections::HashMap;
use std::future::{Future, poll_fn};
use std::path::Path;
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll};

use creche_wire::{GuestPort, SessionId};
use hyper::upgrade::Upgraded;
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::UnixStream;
use tokio::sync::{oneshot, watch};
use tokio::time::{Instant, timeout};

use crate::control::{ControlError, READY_WAIT, dial};
use crate::server::Relay;

/// The bytes that one direction of the relay holds at most between a read
/// and a write. It bounds memory, not the stream (decision CH34 rule 3).
const PIPE_BUFFER: usize = 16 * 1024;

/// What holds a session in the registry.
#[derive(Debug)]
enum Slot {
    /// The claim `claim` holds the session. `stop` is its stop signal.
    /// `ended` closes when the claim ends.
    Open {
        claim: u64,
        stop: oneshot::Sender<Stopped>,
        ended: watch::Receiver<()>,
    },
    /// This many drops of the session run. `ended` closes when the claim
    /// that the first drop stopped ends, if one held the session.
    Dropping {
        drops: usize,
        ended: Option<watch::Receiver<()>>,
    },
}

/// What stopped a claim. The acp handler answers each one in its own way
/// before 101.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stopped {
    /// A drop marked the session (`not_found`, decision CH34 rule 6).
    Drop,
    /// Admission closed the door to evict the VM (`host_full`, decisions
    /// CH19 (6), CH21-3).
    Evict,
}

/// Why a claim failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Taken {
    /// Another Upgrade holds the session (`busy`, decision CH33 rule 7).
    Busy,
    /// A drop of the session runs (`not_found`, decision CH34 rule 6).
    Dropping,
}

/// The door of a session, for the sleep gate (decision CH19 (6)).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Door {
    /// An Upgrade holds the session: its VM starts, or its relay runs.
    Open,
    /// The last admitted Upgrade of the session ended at this time.
    ClosedAt(Instant),
    /// No admitted Upgrade of the session ended since the vmm started or
    /// since a drop of the session began. A refused Upgrade adds no close.
    NotOpened,
}

/// The slot of each held session, the time each door closed, and the
/// number of the next claim.
#[derive(Debug, Default)]
struct Registry {
    slots: HashMap<SessionId, Slot>,
    closed: HashMap<SessionId, Instant>,
    next: u64,
}

/// The registry of the open relays. Each clone shares one registry.
#[derive(Debug, Clone, Default)]
pub struct Relays(Arc<Mutex<Registry>>);

impl Relays {
    /// The registry. Each change is one insert or one remove of a slot,
    /// so a panic of another holder leaves no half change, and a poisoned
    /// lock still holds a whole registry.
    fn registry(&self) -> MutexGuard<'_, Registry> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Claims `id` for one Upgrade. The claim frees the session when it
    /// drops.
    ///
    /// # Errors
    ///
    /// `Taken` when an Upgrade holds the session or a drop of it runs.
    pub fn claim(&self, id: &SessionId) -> Result<Claim, Taken> {
        let mut registry = self.registry();
        match registry.slots.get(id) {
            Some(Slot::Open { .. }) => return Err(Taken::Busy),
            Some(Slot::Dropping { .. }) => return Err(Taken::Dropping),
            None => {}
        }
        registry.next = registry.next.wrapping_add(1);
        let claim = registry.next;
        let (sender, stop) = oneshot::channel();
        let (ends, ended) = watch::channel(());
        let slot = Slot::Open {
            claim,
            stop: sender,
            ended,
        };
        registry.slots.insert(id.clone(), slot);
        Ok(Claim {
            relays: self.clone(),
            id: id.clone(),
            claim,
            stop,
            stopped: None,
            admitted: false,
            _ends: ends,
        })
    }

    /// The door of `id`. The door is open from the claim on, so the sleep
    /// gate never snapshots a VM that an Upgrade starts or relays
    /// (decision CH19 (6)).
    #[must_use]
    pub fn door(&self, id: &SessionId) -> Door {
        let registry = self.registry();
        if let Some(Slot::Open { .. }) = registry.slots.get(id) {
            return Door::Open;
        }
        registry
            .closed
            .get(id)
            .map_or(Door::NotOpened, |&at| Door::ClosedAt(at))
    }

    /// Stops the open relay of `id`, if any, and waits until its claim
    /// ends. Admission does so before it evicts the VM, and the client
    /// reconnects with `session/load` (decision CH19 (6)). The slot goes
    /// at once, so a new Upgrade may claim the session. Its start then
    /// finds the VM under eviction.
    pub async fn close(&self, id: &SessionId) {
        let ended = {
            let mut registry = self.registry();
            match registry.slots.remove(id) {
                Some(Slot::Open { stop, ended, .. }) => {
                    // An Err means the relay ended already.
                    let _ = stop.send(Stopped::Evict);
                    Some(ended)
                }
                Some(slot) => {
                    registry.slots.insert(id.clone(), slot);
                    None
                }
                None => None,
            }
        };
        if let Some(mut ended) = ended {
            // No sender ever sends, so `changed` returns only when the
            // claim drops its sender.
            let _ = ended.changed().await;
        }
    }

    /// Marks a drop of `id` and stops its open relay, if any (decision
    /// CH26 rule 5). The mark lasts until `DropMark` drops. The drop
    /// keeps it until the row of `id` is gone, so no Upgrade starts a VM
    /// of a dropped session.
    pub fn mark_drop(&self, id: &SessionId) -> DropMark {
        let mut registry = self.registry();
        // A dropped session needs no door time, and no claim can add one
        // under the mark.
        registry.closed.remove(id);
        let (drops, ended) = match registry.slots.remove(id) {
            Some(Slot::Dropping { drops, ended }) => (drops.saturating_add(1), ended),
            Some(Slot::Open { stop, ended, .. }) => {
                // An Err means the relay ended already.
                let _ = stop.send(Stopped::Drop);
                (1, Some(ended))
            }
            None => (1, None),
        };
        let slot = Slot::Dropping {
            drops,
            ended: ended.clone(),
        };
        registry.slots.insert(id.clone(), slot);
        DropMark {
            relays: self.clone(),
            id: id.clone(),
            ended,
        }
    }
}

/// One Upgrade's hold on its session.
#[derive(Debug)]
pub struct Claim {
    relays: Relays,
    id: SessionId,
    claim: u64,
    stop: oneshot::Receiver<Stopped>,
    /// What stopped the claim, once `stop_reason` read it.
    stopped: Option<Stopped>,
    /// Whether the Upgrade passed each check and its VM started. Only an
    /// admitted claim records a door close when it ends.
    admitted: bool,
    /// Its drop closes the `ended` link of the slot, so a drop that
    /// stopped the claim learns that it ended (`DropMark::claim_ended`).
    _ends: watch::Sender<()>,
}

impl Claim {
    /// Marks the claim admitted: its VM started and its relay follows.
    pub(crate) fn admit(&mut self) {
        self.admitted = true;
    }

    /// Whether a drop or an eviction stopped the claim.
    pub fn stopped(&mut self) -> bool {
        self.stop_reason().is_some()
    }

    /// What stopped the claim, if anything did. A dropped sender with no
    /// reason counts as a drop, as before the reasons existed.
    pub fn stop_reason(&mut self) -> Option<Stopped> {
        if self.stopped.is_none() {
            self.stopped = match self.stop.try_recv() {
                Ok(reason) => Some(reason),
                Err(oneshot::error::TryRecvError::Empty) => None,
                Err(oneshot::error::TryRecvError::Closed) => Some(Stopped::Drop),
            };
        }
        self.stopped
    }
}

impl Drop for Claim {
    fn drop(&mut self) {
        let mut registry = self.relays.registry();
        // A drop can replace this claim's slot with its mark, and a later
        // claim can follow the mark. Only this claim's own slot goes.
        let own = |slot: &Slot| matches!(slot, Slot::Open { claim, .. } if *claim == self.claim);
        if registry.slots.get(&self.id).is_some_and(own) {
            registry.slots.remove(&self.id);
            // Only an admitted claim closes a door (decision CH19 (6)). A
            // refused Upgrade (a start fault, or a drop during the start)
            // opened no door link. So it leaves the last close time as it
            // was and adds no entry to `closed`.
            if self.admitted {
                registry.closed.insert(self.id.clone(), Instant::now());
            }
        }
    }
}

/// A drop's mark on a session. It ends when the last mark of the session
/// drops.
#[derive(Debug)]
pub struct DropMark {
    relays: Relays,
    id: SessionId,
    /// Closes when the claim that the first mark stopped ends.
    ended: Option<watch::Receiver<()>>,
}

impl DropMark {
    /// Waits until the Upgrade that held the session at the first mark
    /// has ended, or returns at once when none held it. A start that the
    /// drop overlapped may have booted a VM, and the start has no outer
    /// bound (decision CH50 rule 1). After this wait no Upgrade starts a
    /// VM of the session under the mark, so a stop that follows finds
    /// every VM of the session.
    pub async fn claim_ended(&mut self) {
        if let Some(ended) = &mut self.ended {
            // No sender ever sends, so `changed` returns only when the
            // claim drops its sender.
            let _ = ended.changed().await;
        }
    }
}

impl Drop for DropMark {
    fn drop(&mut self) {
        let mut registry = self.relays.registry();
        match registry.slots.remove(&self.id) {
            Some(Slot::Dropping { drops, ended }) if drops > 1 => {
                let slot = Slot::Dropping {
                    drops: drops - 1,
                    ended,
                };
                registry.slots.insert(self.id.clone(), slot);
            }
            // The last mark of the session removes its slot.
            _ => {}
        }
    }
}

/// A dialed link to guest port 1201, and the bytes that guest init sent
/// right after `OK`. The `StartVm` hook dials it before 101 (decision CH50
/// rule 2), and the relay copies both.
#[derive(Debug)]
pub struct AcpLink {
    stream: UnixStream,
    pending: Vec<u8>,
}

impl AcpLink {
    /// Dials guest port 1201 through the vsock socket `vsock` of the VM
    /// (`VmRunDir::vsock_socket`). The dial pauses 10 ms between two
    /// tries, retries ECONNREFUSED, and `READY_WAIT` bounds it (decision
    /// CH43 rules 5 and 7).
    ///
    /// # Errors
    ///
    /// `ControlError`: `Timeout` when no `OK` came within `READY_WAIT`,
    /// or the error of the dial. The hook answers each one with
    /// `StartFault::SandboxStartFailed` (decision CH50 rule 2).
    // Decision CH50 rule 2 holds only if guest init accepts 1201 in every
    // facade state. Phase 3 confirms that. If guest init refuses 1201
    // until the facade runs, this dial makes 101 wait for the facade,
    // against decision CH34 rule 7, and David decides again.
    pub async fn dial(vsock: &Path) -> Result<Self, ControlError> {
        let link = timeout(READY_WAIT, dial(vsock, GuestPort::Acp))
            .await
            .unwrap_or(Err(ControlError::Timeout))?;
        let (stream, pending) = link.into_parts();
        Ok(Self { stream, pending })
    }
}

/// The relay of one admitted Upgrade. It holds the claim until the stream
/// ends.
#[derive(Debug)]
pub struct AcpRelay {
    claim: Claim,
    link: AcpLink,
}

impl AcpRelay {
    /// The relay that holds `claim` and copies bytes over `link`.
    pub(crate) fn new(mut claim: Claim, link: AcpLink) -> Self {
        claim.admit();
        Self { claim, link }
    }
}

impl Relay for AcpRelay {
    /// Copies bytes both ways over the link that the start dialed. An EOF
    /// in one direction shuts down the write half of the other stream, and
    /// the other direction runs on. The relay ends when both directions
    /// reach EOF, when a read or a write fails, or when a drop stops it.
    /// Its end drops both streams.
    async fn run(self, mut client: TokioIo<Upgraded>) {
        let Self { mut claim, link } = self;
        let AcpLink {
            stream: mut guest,
            pending,
        } = link;
        // Guest init may send bytes right after `OK`. They go out first.
        let mut splice = Splice::new(pending);
        poll_fn(|context| {
            if Pin::new(&mut claim.stop).poll(context).is_ready() {
                return Poll::Ready(());
            }
            splice.carry(context, &mut client, &mut guest)
        })
        .await;
    }
}

/// Both directions of one byte copy. The ACP relay, the plane bridges
/// (`crate::planes`), the mirror (`crate::mirror`) and the egress proxy
/// (`crate::egress::proxy`) carry their bytes with it.
#[derive(Debug)]
pub(crate) struct Splice {
    /// From the near stream to the far one.
    up: Pipe,
    /// From the far stream to the near one.
    down: Pipe,
}

impl Splice {
    /// A splice whose far-to-near direction writes `pending` first.
    pub(crate) fn new(pending: Vec<u8>) -> Self {
        Self {
            up: Pipe::new(Vec::new()),
            down: Pipe::new(pending),
        }
    }

    /// Moves bytes both ways between `near` and `far`. An EOF in one
    /// direction shuts down the write half of the other stream, and the
    /// other direction runs on. A peer that half-closes after its last
    /// request still gets each reply, as with `copy_bidirectional`. Ready
    /// when both directions reached EOF, or when a read, a write or a
    /// shutdown failed.
    pub(crate) fn carry<N, F>(
        &mut self,
        context: &mut Context<'_>,
        near: &mut N,
        far: &mut F,
    ) -> Poll<()>
    where
        N: AsyncRead + AsyncWrite + Unpin,
        F: AsyncRead + AsyncWrite + Unpin,
    {
        let sent = self.up.carry(context, near, far);
        let received = self.down.carry(context, far, near);
        match (sent, received) {
            (Poll::Ready(Carried::Failed), _)
            | (_, Poll::Ready(Carried::Failed))
            | (Poll::Ready(Carried::Shut), Poll::Ready(Carried::Shut)) => Poll::Ready(()),
            _ => Poll::Pending,
        }
    }

    /// Whether the far stream reached EOF, each of its bytes reached the
    /// near stream, and the write half of the near stream is shut down.
    /// The mirror then ends its link (decision CH69 rule 14).
    pub(crate) fn far_done(&self) -> bool {
        self.down.shut
    }

    /// The bytes that reached `far` and the bytes that reached `near` so
    /// far. The egress log writes both (decision CH69 rule 10).
    pub(crate) fn carried(&self) -> (u64, u64) {
        (self.up.carried, self.down.carried)
    }
}

/// How one direction of the relay ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Carried {
    /// The source reached EOF, each byte reached the sink, and the write
    /// half of the sink is shut down.
    Shut,
    /// A read, a write or the shutdown failed.
    Failed,
}

/// One direction of the relay: the bytes read from one stream and not yet
/// written to the other.
#[derive(Debug)]
struct Pipe {
    buffer: Vec<u8>,
    start: usize,
    end: usize,
    /// The source reached EOF.
    closed: bool,
    /// The write half of the sink is shut down.
    shut: bool,
    /// The bytes written to the sink.
    carried: u64,
}

impl Pipe {
    /// A pipe that holds `pending` to write first.
    fn new(mut pending: Vec<u8>) -> Self {
        let end = pending.len();
        pending.resize(end.max(PIPE_BUFFER), 0);
        Self {
            buffer: pending,
            start: 0,
            end,
            closed: false,
            shut: false,
            carried: 0,
        }
    }

    /// Moves bytes from `from` to `to`. After an EOF of `from` and the
    /// last write, it shuts down the write half of `to`. A later call
    /// gives `Shut` again and touches no stream.
    fn carry<R, W>(&mut self, context: &mut Context<'_>, from: &mut R, to: &mut W) -> Poll<Carried>
    where
        R: AsyncRead + Unpin,
        W: AsyncWrite + Unpin,
    {
        if self.shut {
            return Poll::Ready(Carried::Shut);
        }
        loop {
            if let Some(bytes) = self
                .buffer
                .get(self.start..self.end)
                .filter(|b| !b.is_empty())
            {
                match Pin::new(&mut *to).poll_write(context, bytes) {
                    Poll::Ready(Ok(0) | Err(_)) => return Poll::Ready(Carried::Failed),
                    Poll::Ready(Ok(written)) => {
                        self.start = self.start.saturating_add(written);
                        let written = u64::try_from(written).unwrap_or(u64::MAX);
                        self.carried = self.carried.saturating_add(written);
                    }
                    Poll::Pending => return Poll::Pending,
                }
                continue;
            }
            if self.closed {
                let shutdown = match Pin::new(&mut *to).poll_flush(context) {
                    Poll::Ready(Ok(())) => Pin::new(&mut *to).poll_shutdown(context),
                    flushed => flushed,
                };
                return shutdown.map(|shutdown| {
                    self.shut = shutdown.is_ok();
                    if self.shut {
                        Carried::Shut
                    } else {
                        Carried::Failed
                    }
                });
            }
            let mut read = ReadBuf::new(&mut self.buffer);
            let polled = Pin::new(&mut *from).poll_read(context, &mut read);
            let filled = read.filled().len();
            match polled {
                Poll::Ready(Ok(())) => {
                    (self.start, self.end, self.closed) = (0, filled, filled == 0);
                }
                Poll::Ready(Err(_)) => return Poll::Ready(Carried::Failed),
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use creche_wire::SessionId;
    use tokio::time::Instant;

    use super::{Door, Relays, Splice, Stopped, Taken};

    /// A claim that a drop stopped ends after a later claim of its
    /// session. Its end leaves the later claim in place.
    #[test]
    fn a_stopped_claim_leaves_a_later_claim() {
        let relays = Relays::default();
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let mut first = relays.claim(&id).unwrap();
        drop(relays.mark_drop(&id));
        assert!(first.stopped());
        let second = relays.claim(&id).unwrap();
        drop(first);
        assert_eq!(relays.claim(&id).map(|_| ()), Err(Taken::Busy));
        drop(second);
        assert!(relays.claim(&id).is_ok());
    }

    /// A claim opens the door, and the end of an admitted claim records
    /// the close. A drop clears the record, and a stopped claim adds none
    /// (decision CH19 (6)).
    #[test]
    fn a_claim_opens_the_door_and_its_end_closes_it() {
        let relays = Relays::default();
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        assert_eq!(relays.door(&id), Door::NotOpened);
        let mut claim = relays.claim(&id).unwrap();
        assert_eq!(relays.door(&id), Door::Open);
        claim.admit();
        let before = Instant::now();
        drop(claim);
        assert!(matches!(relays.door(&id), Door::ClosedAt(at) if at >= before));

        let mut claim = relays.claim(&id).unwrap();
        claim.admit();
        let mark = relays.mark_drop(&id);
        drop(claim);
        drop(mark);
        assert_eq!(relays.door(&id), Door::NotOpened);
    }

    /// A claim that ends with no relay, as for a refused Upgrade, records
    /// no close. It leaves an earlier close time as it was.
    #[test]
    fn a_refused_claim_records_no_close() {
        let relays = Relays::default();
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        drop(relays.claim(&id).unwrap());
        assert_eq!(relays.door(&id), Door::NotOpened);

        let mut claim = relays.claim(&id).unwrap();
        claim.admit();
        drop(claim);
        let closed = relays.door(&id);
        drop(relays.claim(&id).unwrap());
        assert_eq!(relays.door(&id), closed);
    }

    /// `carried` gives the bytes that reached the far stream, then the
    /// bytes that reached the near one, the pending bytes counted (decision
    /// CH69 rule 10). Each peer writes, then shuts its write half, so the
    /// splice ends when both directions reach EOF.
    #[test]
    fn a_splice_counts_the_bytes_of_each_direction() {
        use std::io::{Read, Write};
        use std::net::Shutdown;
        use std::os::unix::net::UnixStream;

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_io()
            .build()
            .unwrap();
        let (mut near_peer, near) = UnixStream::pair().unwrap();
        let (far, mut far_peer) = UnixStream::pair().unwrap();
        near_peer.write_all(b"1234").unwrap();
        near_peer.shutdown(Shutdown::Write).unwrap();
        far_peer.write_all(b"xyz").unwrap();
        far_peer.shutdown(Shutdown::Write).unwrap();
        let mut splice = Splice::new(b"ab".to_vec());
        runtime.block_on(async {
            let open = |stream: UnixStream| {
                stream.set_nonblocking(true).unwrap();
                tokio::net::UnixStream::from_std(stream).unwrap()
            };
            let (mut near, mut far) = (open(near), open(far));
            std::future::poll_fn(|context| splice.carry(context, &mut near, &mut far)).await;
        });
        assert_eq!(splice.carried(), (4, 5));
        let (mut up, mut down) = (Vec::new(), Vec::new());
        far_peer.read_to_end(&mut up).unwrap();
        near_peer.read_to_end(&mut down).unwrap();
        assert_eq!(
            (up.as_slice(), down.as_slice()),
            (&b"1234"[..], &b"abxyz"[..])
        );
    }

    /// A close stops a claim as an eviction, and a mark stops it as a
    /// drop. The reason stays after the first read.
    #[test]
    fn a_close_and_a_mark_give_their_reason() {
        let relays = Relays::default();
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let mut claim = relays.claim(&id).unwrap();
        assert_eq!(claim.stop_reason(), None);
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        // The close waits for the claim to end, so it ends first.
        let mut close = Box::pin(relays.close(&id));
        let waker = std::task::Waker::noop();
        let mut context = std::task::Context::from_waker(waker);
        assert!(close.as_mut().poll(&mut context).is_pending());
        assert_eq!(claim.stop_reason(), Some(Stopped::Evict));
        assert_eq!(claim.stop_reason(), Some(Stopped::Evict));
        drop(claim);
        runtime.block_on(close);

        let mut claim = relays.claim(&id).unwrap();
        let mark = relays.mark_drop(&id);
        assert_eq!(claim.stop_reason(), Some(Stopped::Drop));
        drop((claim, mark));
    }
}
