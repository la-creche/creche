//! The user scope of one Cloud Hypervisor process: the `systemd-run`
//! arguments that put the payload in its own scope with its limits
//! (decisions CH8-2 and CH8-5).

use std::ffi::OsString;
use std::num::{NonZeroU32, NonZeroU64};

use creche_vmm_store::layout::scope_unit;
use creche_wire::SessionId;
use serde::{Deserialize, Serialize};

use crate::launch::Payload;

/// The most tasks in one VM scope (decision CH8-5).
pub const TASKS_MAX: u32 = 256;

/// The scope of the VM of one session and its limits. PR C4 maps the
/// family VM shape to these limits.
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

    /// The scope unit name, `creche-vm-<id>` (fact 76). `systemd-run`
    /// refuses the name of a live leftover scope, so the launch step then
    /// exits with an error status, and the caller reads it as a start
    /// fault. PR C3 stops each leftover at vmm start.
    #[must_use]
    pub fn unit(&self) -> String {
        scope_unit(&self.id)
    }

    /// The arguments of `systemd-run` that run `payload` in this scope.
    /// `systemd-run --scope` execs the payload in its own process, so the
    /// payload keeps the pid of the launch step (decision CH8-2).
    #[must_use]
    pub fn systemd_run_args(&self, payload: &Payload) -> Vec<OsString> {
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

    #[test]
    fn the_arguments_put_the_payload_after_the_limits() {
        let program = Program::new(PathBuf::from("/opt/ch/cloud-hypervisor")).unwrap();
        let payload = Payload::new(program, vec!["--cpus".to_owned(), "boot=2".to_owned()]);
        let args = scope().systemd_run_args(&payload);
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
            "--",
            "/opt/ch/cloud-hypervisor",
            "--cpus",
            "boot=2",
        ];
        assert_eq!(args, expected.map(OsString::from));
    }
}
