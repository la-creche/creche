//! The user scope of one Cloud Hypervisor process: the `systemd-run`
//! arguments that put the payload in its own scope with its limits
//! (decisions CH8-2 and CH8-5), bound to the vmm unit (decision CH60 rule
//! 1).
//!
//! `BindsTo=<vmm unit>` makes systemd stop the scope when the vmm unit
//! stops or dies. AppArmor clears the death signal at the exec of CH into
//! its slot label, so this binding is what ends CH with the vmm (decision
//! CH60 rule 1). `Before=<vmm unit>` orders the two stops. systemd runs
//! stop jobs in the reverse of the start order, so a scope ordered before
//! the vmm unit stops only after the vmm unit has stopped. On a stop or a
//! restart of the vmm unit, the shutdown of
//! `rust/crates/vmm/src/shutdown.rs` therefore runs before any VM stops
//! (decision CH19 (4)). After a crash the vmm unit is down at once, and
//! systemd stops every VM scope.

use std::ffi::OsString;
use std::num::{NonZeroU32, NonZeroU64};

use creche_vmm_store::layout::scope_unit;
use creche_wire::SessionId;
use serde::{Deserialize, Serialize};

use crate::launch::Payload;
use crate::vmm_unit::VmmUnit;

/// The most tasks in one VM scope (decision CH8-5).
pub const TASKS_MAX: u32 = 256;

/// The scope of the VM of one session and its limits.
/// `limits::VmLimits::scope` makes it from the family VM shape.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scope {
    id: SessionId,
    memory_max: NonZeroU64,
    cpu_quota: NonZeroU32,
}

impl Scope {
    /// The scope of the VM of `id`. `memory_max` is `MemoryMax` in bytes.
    /// `cpu_quota` is `CPUQuota` in percent of one CPU.
    #[must_use]
    pub fn new(id: SessionId, memory_max: NonZeroU64, cpu_quota: NonZeroU32) -> Self {
        Self {
            id,
            memory_max,
            cpu_quota,
        }
    }

    /// The session id of the VM.
    #[must_use]
    pub fn id(&self) -> &SessionId {
        &self.id
    }

    /// The scope unit name, `creche-vm-<id>` (fact 76). `systemd-run`
    /// refuses the name of a live leftover scope, so the launch step then
    /// exits with an error status, and the caller reads it as a start
    /// fault. PR C3 stops each leftover at vmm start.
    #[must_use]
    pub fn unit(&self) -> String {
        scope_unit(&self.id)
    }

    /// The arguments of `systemd-run` that run `payload` in this scope,
    /// bound to `vmm` and ordered before it (decision CH60 rule 1).
    /// `systemd-run --scope` execs the payload in its own process, so the
    /// payload keeps the pid of the launch step (decision CH8-2). The
    /// launch step passes the launch stage as the payload, and the stage
    /// execs CH in the same process (decision CH55).
    #[must_use]
    pub fn systemd_run_args(&self, payload: &Payload, vmm: &VmmUnit) -> Vec<OsString> {
        let mut args: Vec<OsString> = [
            "--user".to_owned(),
            "--scope".to_owned(),
            format!("--unit={}", self.unit()),
            "-p".to_owned(),
            format!("MemoryMax={}", self.memory_max),
            "-p".to_owned(),
            format!("CPUQuota={}%", self.cpu_quota),
            "-p".to_owned(),
            format!("TasksMax={TASKS_MAX}"),
            "-p".to_owned(),
            format!("BindsTo={}", vmm.name()),
            "-p".to_owned(),
            format!("Before={}", vmm.name()),
            "--".to_owned(),
        ]
        .into_iter()
        .map(OsString::from)
        .collect();
        args.push(payload.program().path().as_os_str().to_owned());
        args.extend(payload.args().iter().map(OsString::from));
        args
    }
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::num::{NonZeroU32, NonZeroU64};
    use std::path::PathBuf;

    use creche_wire::SessionId;

    use super::Scope;
    use crate::launch::{Payload, Program};
    use crate::vmm_unit::VmmUnit;

    const ID: &str = "0123abcd-0000-4000-8000-00000000beef";

    fn scope() -> Scope {
        let id: SessionId = ID.parse().unwrap();
        let memory = NonZeroU64::new(2_684_354_560).unwrap();
        Scope::new(id, memory, NonZeroU32::new(200).unwrap())
    }

    #[test]
    fn the_unit_has_the_vm_prefix() {
        assert_eq!(scope().unit(), format!("creche-vm-{ID}"));
    }

    /// The scope binds to the vmm unit and is ordered before it (decision
    /// CH60 rule 1), then the payload follows the limits.
    #[test]
    fn the_arguments_bind_the_scope_and_put_the_payload_last() {
        let program = Program::new(PathBuf::from("/opt/ch/cloud-hypervisor")).unwrap();
        let payload = Payload::new(program, vec!["--cpus".to_owned(), "boot=2".to_owned()]);
        let vmm = VmmUnit::new("creche-vmm.service".to_owned()).unwrap();
        let args = scope().systemd_run_args(&payload, &vmm);
        let unit = format!("--unit=creche-vm-{ID}");
        let expected = [
            "--user",
            "--scope",
            unit.as_str(),
            "-p",
            "MemoryMax=2684354560",
            "-p",
            "CPUQuota=200%",
            "-p",
            "TasksMax=256",
            "-p",
            "BindsTo=creche-vmm.service",
            "-p",
            "Before=creche-vmm.service",
            "--",
            "/opt/ch/cloud-hypervisor",
            "--cpus",
            "boot=2",
        ];
        assert_eq!(args, expected.map(OsString::from));
    }
}
