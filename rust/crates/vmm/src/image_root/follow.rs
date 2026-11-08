//! The digests follow of `vmm serve` (plan PR V1, decisions CH76 rule 8,
//! CH77 rules 2 and 3).
//! A disks release replaces `digests.json` and its files while the vmm
//! runs. The disks row has no unit, and handover restarts only the unit
//! of the released row. So the vmm follows the file itself:
//!
//! 1. Every `POLL` it reads the bytes of `digests.json` with the checks
//!    of `image_root::read`, in `spawn_blocking` (decision CH36 G21), as
//!    the edit follow polls the reach files (decision CH69 rule 11).
//!    Bytes equal to the bytes of the set of new starts import nothing.
//! 2. New bytes wait: the follow imports them only after they read the
//!    same at each poll for `HOOK_TIMEOUT`, the timeout of the disks
//!    verify hook (decision CH77 rule 2). Any other reading ends the
//!    wait. So a release that its hook rolls back never reaches a new
//!    start, and the import never decompresses a disk while the hook
//!    does.
//! 3. Settled bytes parse with `image_root::parse`, and
//!    `ImageSet::import` puts each entry into the digest store, one file
//!    at a time.
//! 4. Only when every entry imports does the follow swap the `Digests`
//!    that each new start reads (`StartConfig::digests`). A parse or an
//!    import that fails keeps the old set (decision CH58 rule 5: a bad
//!    entry never serves).
//! 5. The follow tries the bytes of a failed parse or import again every
//!    `RETRY_POLLS` polls (decision CH77 rule 3), so a fault that clears,
//!    such as a full state root, still swaps the set with no vmm restart
//!    (decisions CH48 (2), CH76 rule 8). Import skips each entry that the
//!    store holds, so a retry rereads only the entries that did not
//!    import.
//!
//! Each fault logs one line (decision CH77 rule 3): a fault logs again
//! only when its reading or its text changes. A read that takes no bytes
//! (a missing file, a failed owner check, a file past the cap) is such a
//! reading. The start of a wait logs one line too.
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
//! for at most `HOOK_TIMEOUT` (decision CH76 rule 6, gap Q68-6). The wait
//! of step 2 outlasts the hook (decision CH77 rule 2). A failed hook puts
//! the previous tree back inside the wait, and the follow then reads the
//! bytes of the set of new starts again, which end the wait and import
//! nothing. So a disks release reaches new starts about 5 min after its
//! install. A restart of the vmm inside the wait imports the file that it
//! reads at once, as each vmm start does (decision CH58 rule 5).

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
/// Decision CH77 rule 3 counts 12 polls as one minute, so 5 s. A disks
/// release happens a few times a month, so 5 s costs one small read per
/// poll.
pub const POLL: Duration = Duration::from_secs(5);

/// The polls between two tries of the same bytes whose parse or import
/// failed: 12 polls of `POLL`, one minute (decision CH77 rule 3). A
/// failed import of a 2.5 GB disk rereads that disk, so a try at each
/// poll would keep the disk and `zstd -d` busy for as long as the fault
/// holds.
pub const RETRY_POLLS: u32 = 12;

/// The timeout of the disks verify hook `vmm check-images`: 300 s (gap
/// Q68-6, the `timeout` of the disks row of plan PR H4b). New bytes of
/// `digests.json` import only after they stay the same this long
/// (decision CH77 rule 2).
pub const HOOK_TIMEOUT: Duration = Duration::from_secs(300);

/// The polls that new bytes must wait before their import: `HOOK_TIMEOUT`
/// over `POLL`, rounded up, so 60 (decision CH77 rule 2). Each poll
/// sleeps `POLL` before its read, so these polls span at least
/// `HOOK_TIMEOUT`.
pub const SETTLE_POLLS: u64 = HOOK_TIMEOUT.as_secs().div_ceil(POLL.as_secs());

/// One read of `digests.json`: its bytes, or the text of a read that took
/// no bytes.
#[derive(Debug, PartialEq, Eq)]
enum Reading {
    Bytes(Vec<u8>),
    Unread(String),
}

/// New bytes that wait to stay the same for `HOOK_TIMEOUT` (decision
/// CH77 rule 2).
#[derive(Debug)]
struct Pending {
    bytes: Vec<u8>,
    /// The polls since the first read of `bytes`.
    polls: u64,
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
    /// The new bytes that wait, or `None` when no new bytes wait.
    pending: Option<Pending>,
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
            pending: None,
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
    /// read, or `None` when the read changes nothing, waits, or repeats
    /// the fault of the last reading. Blocks.
    fn apply(&mut self, read: Result<Vec<u8>, DigestsError>) -> Option<String> {
        let reading = match read {
            Ok(bytes) => Reading::Bytes(bytes),
            Err(fault) => Reading::Unread(fault.to_string()),
        };
        // Decision CH77 rule 2: each reading but the same new bytes ends
        // the wait of new bytes. So a rolled-back file imports nothing.
        let pending = self.pending.take();
        let text = match &reading {
            Reading::Unread(text) => text.clone(),
            Reading::Bytes(bytes) => {
                if self.fault.is_none() && *bytes == self.applied {
                    return None;
                }
                if let Some(fault) = &mut self.fault
                    && fault.reading == reading
                {
                    // Decision CH77 rule 3: the same bytes after a failed
                    // try wait `RETRY_POLLS` polls for the next try.
                    if fault.wait > 0 {
                        fault.wait -= 1;
                        return None;
                    }
                } else if *bytes != self.applied {
                    let polls = pending
                        .filter(|pending| pending.bytes == *bytes)
                        .map_or(0, |pending| pending.polls + 1);
                    if polls < SETTLE_POLLS {
                        self.pending = Some(Pending {
                            bytes: bytes.clone(),
                            polls,
                        });
                        if polls > 0 {
                            return None;
                        }
                        self.fault = None;
                        return Some(format!(
                            "a new {DIGESTS_FILE} imports once its bytes stay the same for {} s, \
                             so new starts keep the last set until then",
                            HOOK_TIMEOUT.as_secs()
                        ));
                    }
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
