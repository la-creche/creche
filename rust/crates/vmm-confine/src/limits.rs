//! The limits of one family VM: the family VM shape mapped to the Cloud
//! Hypervisor sizes and to the limits of the VM scope (decisions CH8-5,
//! CH8-2).
//!
//! 1. `cpus` gives `--cpus boot=N` and `CPUQuota=N×100%`.
//! 2. `memory_mib` gives the guest RAM, `--memory size=<N>M`, and
//!    `MemoryMax` = guest RAM + `MEMORY_HEADROOM_MIB`.
//! 3. Every VM gets the balloon with free page reporting and deflate on
//!    OOM. An idle guest touches little of its RAM (fact 60), and the
//!    balloon gives freed pages back to the host (fact 72).
//! 4. `TasksMax` is `scope::TASKS_MAX` (256) for every VM.
//!
//! The flavor of the shape picks the image, not a limit, so this module
//! ignores it.

use std::num::{NonZeroU32, NonZeroU64};

use creche_wire::{FamilyCpus, FamilyMemoryMib, FamilyShape, SessionId};

use crate::scope::Scope;

/// One MiB in bytes.
pub const MIB: u64 = 1 << 20;

/// The scope memory above the guest RAM, for the CH process itself
/// (decision CH8-5). It is a first value: phase 8 measures it.
pub const MEMORY_HEADROOM_MIB: u64 = 512;

/// The `CPUQuota` of one vCPU, in percent of one host CPU (decision CH8-5).
pub const CPU_QUOTA_PER_VCPU: u32 = 100;

/// The `--balloon` value of every VM: no inflate at boot, free page
/// reporting on and deflate on OOM on (decision CH8-5). Fact 72 shows
/// that free page reporting works with no privilege.
pub const BALLOON: &str = "size=0,deflate_on_oom=on,free_page_reporting=on";

/// The limits of the VM of one family, from its VM shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VmLimits {
    cpus: FamilyCpus,
    memory: FamilyMemoryMib,
}

impl From<FamilyShape> for VmLimits {
    fn from(shape: FamilyShape) -> Self {
        Self {
            cpus: shape.cpus,
            memory: shape.memory_mib,
        }
    }
}

impl VmLimits {
    /// The vCPUs of the guest.
    #[must_use]
    pub fn vcpus(self) -> u8 {
        self.cpus.get()
    }

    /// The guest RAM in MiB.
    #[must_use]
    pub fn guest_ram_mib(self) -> u64 {
        u64::from(self.memory.get())
    }

    /// The guest RAM in bytes. Admission counts this value for each
    /// running VM (decision CH8-6).
    #[must_use]
    pub fn guest_ram(self) -> u64 {
        self.guest_ram_mib() * MIB
    }

    /// `MemoryMax` of the scope in MiB: the guest RAM plus the headroom.
    #[must_use]
    pub fn memory_max_mib(self) -> u64 {
        self.guest_ram_mib() + MEMORY_HEADROOM_MIB
    }

    /// `MemoryMax` of the scope in bytes.
    #[must_use]
    pub fn memory_max(self) -> NonZeroU64 {
        // The headroom is not zero, so the value is at least 1. MIN (1)
        // plus the value minus 1 is the value, with no `None` branch.
        NonZeroU64::MIN.saturating_add(self.memory_max_mib() * MIB - 1)
    }

    /// `CPUQuota` of the scope in percent of one host CPU: 100 per vCPU.
    #[must_use]
    pub fn cpu_quota(self) -> NonZeroU32 {
        // `FamilyCpus` is at least 1, so the value is at least 100.
        let quota = u32::from(self.vcpus()) * CPU_QUOTA_PER_VCPU;
        NonZeroU32::MIN.saturating_add(quota - 1)
    }

    /// The scope of the VM of `id` with these limits.
    #[must_use]
    pub fn scope(self, id: SessionId) -> Scope {
        Scope::new(id, self.memory_max(), self.cpu_quota())
    }

    /// The size arguments of the Cloud Hypervisor command line, in this
    /// order: `--cpus`, `--memory` and `--balloon`. CH reads the `M` of
    /// `size=` as MiB. Plan PR C7 puts them into the whole command line.
    #[must_use]
    pub fn ch_args(self) -> Vec<String> {
        vec![
            "--cpus".to_owned(),
            format!("boot={}", self.vcpus()),
            "--memory".to_owned(),
            format!("size={}M", self.guest_ram_mib()),
            "--balloon".to_owned(),
            BALLOON.to_owned(),
        ]
    }
}
