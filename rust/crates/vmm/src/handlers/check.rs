//! The `check` route (decisions CH6-3, CH69 rules 1 and 2, CH70 rule 2).
//! A client asks whether a family may reach a target, and the vmm answers
//! with the one decision of its egress proxy, `egress::rule`. A check
//! boots no VM.

use creche_vmm_confine::family::FamilyRoot;
use creche_wire::{
    CheckError, CheckReason, CheckRefused, CheckReply, CheckRequest, CheckRoute, Serve,
};
use tokio::task::spawn_blocking;

use crate::egress::rule::{Target, decide};

/// The handler of `check`. It reads caregiver's families dir.
#[derive(Debug)]
pub struct Checks {
    families: FamilyRoot,
}

impl Checks {
    /// The handler with caregiver's families dir `families`.
    #[must_use]
    pub fn new(families: FamilyRoot) -> Self {
        Self { families }
    }
}

impl Serve<CheckRoute> for Checks {
    /// Reads the target, then the allowlist of the family, then decides
    /// (decision CH69 rule 2). A bad target is a refusal before any read.
    /// An unknown family or an absent `egress.json` has an empty list, so
    /// the answer is `not_listed` (decision CH69 rule 3). A file that is
    /// present and does not read is `not_valid` (decision CH70 rule 2).
    /// The file read blocks, so it runs in `spawn_blocking` (decision CH36
    /// G21). The lookup runs off the runtime in tokio.
    async fn serve(&self, request: CheckRequest) -> Result<CheckReply, CheckError> {
        let target = match Target::parse(&request.target) {
            Ok(target) => target,
            Err(reason) => return Ok(CheckReply::Refused(CheckRefused { reason })),
        };
        let (families, family) = (self.families.clone(), request.family.clone());
        let read = spawn_blocking(move || families.read_egress(&family))
            .await
            .map_err(|_| CheckError::Internal {})?;
        // Decision CH70 rule 2: an `egress.json` that is present and does
        // not read is the refusal `not_valid`, not a route error. It allows
        // nothing, and the log line names the same reason as the reply.
        let list = match read {
            Ok(list) => list,
            Err(fault) => {
                let family = request.family;
                eprintln!("vmm: check in {family}: egress.json is not_valid: {fault:?}");
                let reason = CheckReason::NotValid;
                return Ok(CheckReply::Refused(CheckRefused { reason }));
            }
        };
        Ok(decide(&list, &target).await)
    }
}
