//! The open ACP relays, one per session (decisions CH26 rule 5, CH33
//! rule 7). An `acp` Upgrade claims its session before the VM starts. A
//! second Upgrade of the session gets `busy` while the claim lives. A
//! drop marks the session first: the mark fires the stop signal of an
//! open relay, and an Upgrade under the mark gets `not_found` (decision
//! CH34 rule 6).

use std::collections::HashMap;
use std::future::{Future, poll_fn};
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::Poll;

use creche_wire::SessionId;
use hyper::upgrade::Upgraded;
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::oneshot;

use crate::server::Relay;

/// What holds a session in the registry.
#[derive(Debug)]
enum Slot {
    /// The claim `claim` holds the session. `stop` is its stop signal.
    Open {
        claim: u64,
        stop: oneshot::Sender<()>,
    },
    /// This many drops of the session run.
    Dropping(usize),
}

/// Why a claim failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Taken {
    /// Another Upgrade holds the session (`busy`, decision CH33 rule 7).
    Busy,
    /// A drop of the session runs (`not_found`, decision CH34 rule 6).
    Dropping,
}

/// The slot of each held session, and the number of the next claim.
#[derive(Debug, Default)]
struct Registry {
    slots: HashMap<SessionId, Slot>,
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
            Some(Slot::Dropping(_)) => return Err(Taken::Dropping),
            None => {}
        }
        registry.next = registry.next.wrapping_add(1);
        let claim = registry.next;
        let (sender, stop) = oneshot::channel();
        let slot = Slot::Open {
            claim,
            stop: sender,
        };
        registry.slots.insert(id.clone(), slot);
        Ok(Claim {
            relays: self.clone(),
            id: id.clone(),
            claim,
            stop,
        })
    }

    /// Marks a drop of `id` and stops its open relay, if any (decision
    /// CH26 rule 5). The mark lasts until `DropMark` drops. The drop
    /// keeps it until the row of `id` is gone, so no Upgrade starts a VM
    /// of a dropped session.
    pub fn mark_drop(&self, id: &SessionId) -> DropMark {
        let mut registry = self.registry();
        let drops = match registry.slots.remove(id) {
            Some(Slot::Dropping(drops)) => drops.saturating_add(1),
            Some(Slot::Open { stop, .. }) => {
                // An Err means the relay ended already.
                let _ = stop.send(());
                1
            }
            None => 1,
        };
        registry.slots.insert(id.clone(), Slot::Dropping(drops));
        DropMark {
            relays: self.clone(),
            id: id.clone(),
        }
    }
}

/// One Upgrade's hold on its session.
#[derive(Debug)]
pub struct Claim {
    relays: Relays,
    id: SessionId,
    claim: u64,
    stop: oneshot::Receiver<()>,
}

impl Claim {
    /// Whether a drop stopped the claim. A sent stop and a dropped sender
    /// both count.
    pub fn stopped(&mut self) -> bool {
        !matches!(
            self.stop.try_recv(),
            Err(oneshot::error::TryRecvError::Empty)
        )
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
        }
    }
}

/// A drop's mark on a session. It ends when the last mark of the session
/// drops.
#[derive(Debug)]
pub struct DropMark {
    relays: Relays,
    id: SessionId,
}

impl Drop for DropMark {
    fn drop(&mut self) {
        let mut registry = self.relays.registry();
        match registry.slots.remove(&self.id) {
            Some(Slot::Dropping(drops)) if drops > 1 => {
                registry
                    .slots
                    .insert(self.id.clone(), Slot::Dropping(drops - 1));
            }
            // The last mark of the session removes its slot.
            _ => {}
        }
    }
}

/// The relay of one admitted Upgrade. It holds the claim until the stream
/// ends.
// Plan PR L13 relays the ACP bytes to guest port 1201. Until then the
// relay reads and drops the bytes of the client.
#[derive(Debug)]
pub struct AcpRelay {
    claim: Claim,
}

impl AcpRelay {
    /// The relay that holds `claim`.
    pub(crate) fn new(claim: Claim) -> Self {
        Self { claim }
    }
}

impl Relay for AcpRelay {
    /// Runs until the client closes the stream or a drop stops the relay.
    async fn run(self, mut stream: TokioIo<Upgraded>) {
        let mut claim = self.claim;
        let mut buffer = [0_u8; 4096];
        poll_fn(|context| {
            if Pin::new(&mut claim.stop).poll(context).is_ready() {
                return Poll::Ready(());
            }
            loop {
                let mut read = ReadBuf::new(&mut buffer);
                match Pin::new(&mut stream).poll_read(context, &mut read) {
                    Poll::Ready(Ok(())) if !read.filled().is_empty() => {}
                    Poll::Ready(_) => return Poll::Ready(()),
                    Poll::Pending => return Poll::Pending,
                }
            }
        })
        .await;
    }
}

#[cfg(test)]
mod tests {
    use creche_wire::SessionId;

    use super::{Relays, Taken};

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
}
