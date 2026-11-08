//! The digests follow of `vmm serve` (plan PR V1, decision CH76 rule 8).
//! A disks release replaces `digests.json` and its files while the vmm
//! runs. The disks row has no unit, and handover restarts only the unit
//! of the released row. So the vmm follows the file itself:
//!
//! 1. Every `POLL` it reads the bytes of `digests.json` with the checks
//!    of `image_root::read`, in `spawn_blocking` (decision CH36 G21), as
//!    the edit follow polls the reach files (decision CH69 rule 11).
//!    Bytes equal to the bytes of the set of new starts import nothing.
//! 2. New bytes parse with `image_root::parse`, and `ImageSet::import`
//!    puts each entry into the digest store, one file at a time.
//! 3. Only when every entry imports does the follow swap the `Digests`
//!    that each new start reads (`StartConfig::digests`). A parse or an
//!    import that fails keeps the old set (decision CH58 rule 5: a bad
//!    entry never serves).
//! 4. The follow tries the bytes of a failed parse or import again every
//!    `RETRY_POLLS` polls, so a fault that clears, such as a full state
//!    root, still swaps the set with no vmm restart (decisions CH48 (2),
//!    CH76 rule 8). Import skips each entry that the store holds, so a
//!    retry rereads only the entries that did not import.
//!
//! Each fault logs one line: a fault logs again only when its reading or
//! its text changes. A read that takes no bytes (a missing file, a failed
//! owner check, a file past the cap) is such a reading.
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
//! logs one `is missing` line, and its next poll reads the new file.
//!
//! handover swaps the tree first and runs `vmm check-images` after it
//! (decision CH76 rule 6). So the follow may import and swap to the new
//! set while the hook still runs. Each import checks each digest itself
//! (decision CH58 rule 5), so no entry that fails its digest serves. A
//! failed hook puts the previous tree back, and the follow then reads the
//! previous bytes and swaps back to their set, which the store holds
//! already. A VM that cold-booted on the new set in that window drops its
//! snapshot again at its next start. CONTRACT-QUESTION (vmm AGENTS.md
//! Known gaps 33): no row says whether the follow must wait for the end
//! of the hook.

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

/// The polls between two tries of the same bytes whose parse or import
/// failed: 12 polls of `POLL`, one minute. No row sets it. A failed
/// import of a 2.5 GB disk rereads that disk, so a try at each poll would
/// keep the disk and `zstd -d` busy for as long as the fault holds.
pub const RETRY_POLLS: u32 = 12;

/// One read of `digests.json`: its bytes, or the text of a read that took
/// no bytes.
#[derive(Debug, PartialEq, Eq)]
enum Reading {
    Bytes(Vec<u8>),
    Unread(String),
}

/// The last reading that did not swap the set, with the text of its
/// fault.
#[derive(Debug)]
struct Fault {
    reading: Reading,
    text: String,
    /// The polls of the same reading that pass before the next try.
    wait: u32,
}

/// The follow of the `digests.json` of one image root. `run` polls it.
#[derive(Debug)]
pub struct Follow {
    root: PathBuf,
    images: ImageStore,
    /// The bytes of the set of new starts.
    applied: Vec<u8>,
    /// The fault of the last reading, or `None` when it swapped the set
    /// or equals `applied`.
    fault: Option<Fault>,
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
            applied: set.bytes,
            fault: None,
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

    /// Applies one read of the file. It gives the one log line of the
    /// read, or `None` when the read changes nothing, or when it repeats
    /// the fault of the last reading. Blocks.
    fn apply(&mut self, read: Result<Vec<u8>, DigestsError>) -> Option<String> {
        let reading = match read {
            Ok(bytes) => Reading::Bytes(bytes),
            Err(fault) => Reading::Unread(fault.to_string()),
        };
        let text = match &reading {
            Reading::Unread(text) => text.clone(),
            Reading::Bytes(bytes) => {
                if self.fault.is_none() && *bytes == self.applied {
                    return None;
                }
                if let Some(fault) = &mut self.fault
                    && fault.reading == reading
                    && fault.wait > 0
                {
                    fault.wait -= 1;
                    return None;
                }
                match self.switch(bytes.clone()) {
                    Ok(digests) => {
                        self.applied.clone_from(bytes);
                        self.fault = None;
                        return Some(format!("new starts use the new {DIGESTS_FILE}: {digests}"));
                    }
                    Err(text) => text,
                }
            }
        };
        let repeat = self
            .fault
            .as_ref()
            .is_some_and(|fault| fault.reading == reading && fault.text == text);
        let line = (!repeat).then(|| format!("{text}, so new starts keep the last set"));
        self.fault = Some(Fault {
            reading,
            text,
            wait: RETRY_POLLS,
        });
        line
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
