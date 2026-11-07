//! The egress of a VM (decisions CH6-1, CH6-3, CH6-4). `rule` holds the
//! one decision of a target. The route `check` and the CONNECT proxy of
//! host port 1104 (`proxy`) both call it (decision CH69 rule 2). `log`
//! writes the JSONL line of each proxy link (decision CH69 rule 10).

pub mod log;
pub mod proxy;
pub mod rule;

use creche_vmm_confine::family::{FamilyRoot, ReachFault};
use creche_wire::{FamilyEgress, FamilyName};
use tokio::task::spawn_blocking;

/// The allowlist of `family` in `families`, read fresh (decision CH6-4).
/// The route `check` and each CONNECT read it here. The read blocks, so it
/// runs in `spawn_blocking` (decision CH36 G21). `None` when the blocking
/// read did not end, as at the end of the runtime.
pub async fn read_list(
    families: &FamilyRoot,
    family: &FamilyName,
) -> Option<Result<FamilyEgress, ReachFault>> {
    let (families, family) = (families.clone(), family.clone());
    spawn_blocking(move || families.read_egress(&family))
        .await
        .ok()
}
