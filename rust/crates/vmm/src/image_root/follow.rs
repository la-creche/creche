//! The digests follow of `vmm serve` (plan PR V1, decision CH76 rule 8).
//! A disks release replaces `digests.json` and its files while the vmm
//! runs. The disks row has no unit, and handover restarts only the unit
//! of the released row. So the vmm follows the file itself:
//!
//! 1. Every `POLL` it reads the bytes of `digests.json` with the checks
//!    of `image_root::read`, in `spawn_blocking` (decision CH36 G21), as
//!    the edit follow polls the reach files (decision CH69 rule 11).
//!    Bytes equal to the last reading change nothing and import nothing.
//!    A read that takes no bytes (a missing file, a failed owner check, a
//!    file past the cap) is a new reading only when its fault text
//!    changes, so each fault logs one line.
//! 2. New bytes parse with `image_root::parse`, and `ImageSet::import`
//!    puts each entry into the digest store, one file at a time.
//! 3. Only when every entry imports does the follow swap the `Digests`
//!    that each new start reads (`StartConfig::digests`). A parse or an
//!    import that fails keeps the old set and logs one line (decision
//!    CH58 rule 5: a bad entry never serves). The follow tries those
//!    bytes again only after they change.
//!
//! A running VM keeps its store copies: no step removes a store file. A
//! snapshot holds the key of its start, which holds the root disk digest
//! (decisions CH7-6, CH62 rule 24). After a swap, the key of a new start
//! differs from that key, so the snapshot drops at the next start and
//! the VM cold-boots on the new disk.
//!
//! The follow is the only import after the vmm start, so the store still
//! runs one import at a time. The handover swap of the disks tree has a
//! short window with no `digests.json` (two renames). The follow then
//! logs one `is missing` line, and its next poll reads the new file. A
//! failed verify hook puts the previous tree back, and the follow then
//! reads the previous bytes and swaps back to their set, which the store
//! holds already.

use std::path::PathBuf;
use std::time::Duration;

use creche_vmm_store::images::ImageStore;
use tokio::sync::watch;
use tokio::task::spawn_blocking;
use tokio::time::sleep;

use super::{
    DIGESTS_FILE, DigestsError, ImageImportError, ImageSet, KERNEL_KEY, parse, read_bytes,
};
use crate::start::Digests;

/// The pause between two reads of `digests.json` (decision CH76 rule 8).
/// No row sets it. A disks release happens a few times a month, so 5 s
/// costs one small read per poll.
pub const POLL: Duration = Duration::from_secs(5);

/// One read of `digests.json`: its bytes, or the text of a read that took
/// no bytes.
#[derive(Debug, PartialEq, Eq)]
enum Reading {
    Bytes(Vec<u8>),
    Unread(String),
}

/// The follow of the `digests.json` of one image root. `run` polls it.
#[derive(Debug)]
pub struct Follow {
    root: PathBuf,
    images: ImageStore,
    last: Reading,
    current: watch::Sender<Digests>,
}

impl Follow {
    /// Imports each entry of `set`, the image set of the vmm start, into
    /// `images`. It gives the follow of the file of `set` and the
    /// receiver of the `Digests` of each new start. Blocks.
    ///
    /// # Errors
    ///
    /// The first import that failed. It refuses the start of the vmm
    /// (decision CH58 rule 5).
    pub fn start(
        set: ImageSet,
        images: ImageStore,
    ) -> Result<(Self, watch::Receiver<Digests>), ImageImportError> {
        let digests = set.import(&images)?;
        let (current, receiver) = watch::channel(digests);
        let follow = Self {
            root: set.root,
            images,
            last: Reading::Bytes(set.bytes),
            current,
        };
        Ok((follow, receiver))
    }

    /// Reads the file every `POLL` and applies each new reading. It runs
    /// until its task is aborted, or until a poll panics.
    pub async fn run(mut self) {
        loop {
            sleep(POLL).await;
            self = match spawn_blocking(move || {
                self.poll();
                self
            })
            .await
            {
                Ok(follow) => follow,
                Err(_) => {
                    eprintln!("vmm: the digests follow panicked, so new starts keep the last set");
                    return;
                }
            };
        }
    }

    /// One poll: the read, then `apply`. Blocks.
    fn poll(&mut self) {
        if let Some(line) = self.apply(read_bytes(&self.root)) {
            eprintln!("vmm: {line}");
        }
    }

    /// Applies one read of the file. It gives the one log line of a new
    /// reading, or `None` when the reading equals the last one. Blocks.
    fn apply(&mut self, read: Result<Vec<u8>, DigestsError>) -> Option<String> {
        let reading = match read {
            Ok(bytes) => Reading::Bytes(bytes),
            Err(fault) => Reading::Unread(fault.to_string()),
        };
        if reading == self.last {
            return None;
        }
        let outcome = match &reading {
            Reading::Unread(fault) => Err(fault.clone()),
            Reading::Bytes(bytes) => self.switch(bytes.clone()),
        };
        self.last = reading;
        Some(match outcome {
            Ok(digests) => format!("new starts use the new {DIGESTS_FILE}: {digests}"),
            Err(fault) => format!("{fault}, so new starts keep the last set"),
        })
    }

    /// Parses `bytes`, imports each entry, and only then swaps the set of
    /// new starts. Blocks.
    fn switch(&self, bytes: Vec<u8>) -> Result<String, String> {
        let set = parse(&self.root, bytes).map_err(|fault| fault.to_string())?;
        let digests = set
            .import(&self.images)
            .map_err(|fault| fault.to_string())?;
        let text = std::iter::once(format!("{KERNEL_KEY} {}", digests.kernel))
            .chain(
                digests
                    .roots
                    .iter()
                    .map(|(flavor, digest)| format!("{} {digest}", flavor.name())),
            )
            .collect::<Vec<_>>()
            .join(", ");
        self.current.send_replace(digests);
        Ok(text)
    }
}

#[cfg(test)]
mod tests;
