//! The snapshot that puts a VM to sleep (decisions CH2, CH8-6). An
//! eviction of admission takes it now (plan PR L17). Plan PR L18 adds the
//! idle timer.

use std::time::Duration;

use creche_vmm_store::key::{SnapshotKey, write_key};
use creche_vmm_store::layout::{SessionDir, SnapshotGeneration, VmRunDir};
use creche_vmm_store::snapshot::{SnapshotTimeouts, Snapshotter};

/// The timeouts of the snapshot of one eviction (decisions CH44 rule 3,
/// CH50 rule 1). A pause takes about 4 ms (fact 72). A snapshot of the
/// largest guest, 16 GiB, takes about 12 s (gap G16), so 60 s leaves
/// room for a slow disk. The dig of the holes has no timeout (PR D9).
pub const EVICT_SNAPSHOT: SnapshotTimeouts = SnapshotTimeouts {
    pause: Duration::from_secs(5),
    snapshot: Duration::from_secs(60),
    shutdown: Duration::from_secs(10),
};

/// Snapshots the VM of `vm` into a new generation of `session`, ends its
/// CH (PR D9), then writes `key` into the generation (PR D10). Blocks.
///
/// # Errors
///
/// The `SnapshotError` of the take, as text. A failed key write is no
/// error: the next key check drops the generation, and that start
/// cold-boots (decision CH7-6).
pub fn take_keyed(
    snapshotter: &Snapshotter,
    vm: &VmRunDir,
    session: &SessionDir,
    key: &SnapshotKey,
    timeouts: SnapshotTimeouts,
) -> Result<SnapshotGeneration, String> {
    let snapshot = snapshotter
        .take(vm, session, timeouts)
        .map_err(|error| format!("{error}: {error:?}"))?;
    if let Err(error) = write_key(session, snapshot.generation, key) {
        eprintln!("vmm: the snapshot has no key, so it drops: {error}");
    }
    Ok(snapshot.generation)
}
