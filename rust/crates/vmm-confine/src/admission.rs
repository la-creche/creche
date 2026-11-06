//! Admission of one VM start: the RAM cap, the slot pool and the
//! free-space floor (decisions CH8-6, CH8-7, CH58 rule 2).
//!
//! `decide` is a pure decision. Lifecycle passes in the running VMs, the
//! sleep-gate result of each, the free bytes and the free slots of the
//! pool. It runs each eviction that the answer names, then starts the VM.
//! The rules:
//!
//! 1. The free bytes of the state root are below the floor: `HostFull`
//!    with `Full::Disk`.
//! 2. The configured guest RAM of the running VMs plus the RAM wanted is at
//!    or below the cap, and a slot is free: `Admit`.
//! 3. Else the idle VMs, least recently used first, free enough RAM and at
//!    least one slot when none is free, and the floor holds after the
//!    writes of the shortest such prefix of that order: `EvictFirst` with
//!    that prefix. A full slot pool counts like a full RAM cap (decision
//!    CH58 rule 2), so it needs a prefix of one VM at least. The snapshot
//!    of an evicted VM writes its whole guest RAM to disk (fact 72). So
//!    the free bytes minus the guest RAM of each evicted VM must stay at
//!    or above the floor (decision CH42 rule 3).
//! 4. Else the shortest such prefix breaks the floor: `HostFull` with
//!    `Full::Disk`. A longer prefix writes no fewer bytes, so no prefix
//!    frees enough and holds the floor.
//! 5. Else `HostFull` with `Full::Ram` when the RAM does not fit, or with
//!    `Full::Slots` when only the slot pool is full. The vmm never queues
//!    (decision CH21-3). It answers `host_full` (503) before 101
//!    (decisions CH21-9, CH34 rule 6).
//!
//! A partial eviction never happens: when no prefix of the idle VMs frees
//! enough and holds the floor, the answer is `HostFull` and no VM stops.
//! A start with no eviction writes no snapshot, so rule 1 alone checks
//! its floor.

use std::io;
use std::path::Path;
use std::time::Instant;

use creche_wire::SessionId;

/// One GiB in bytes.
pub const GIB: u64 = 1 << 30;

/// The design cap on the configured guest RAM of the running VMs
/// (decision CH8-6). PR L20 makes it the default of an env key (CH36 G14).
pub const RAM_CAP: u64 = 80 * GIB;

/// The design floor of free bytes on the state root (CH36 G14 for
/// decision CH8-7). PR L20 makes it the default of an env key.
pub const FREE_FLOOR: u64 = 50 * GIB;

/// The cap and the floor that admission holds. Lifecycle reads them from
/// the vmm config.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// The most configured guest RAM of all running VMs, in bytes.
    pub ram_cap: u64,
    /// The fewest free bytes on the state root that still admit a start.
    pub free_floor: u64,
}

impl Limits {
    /// The design values: 80 GiB and 50 GiB.
    pub const DESIGN: Self = Self {
        ram_cap: RAM_CAP,
        free_floor: FREE_FLOOR,
    };
}

/// The sleep-gate result of a running VM (decision CH19 (6)). Lifecycle
/// computes it. Only an idle VM may be evicted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Gate {
    /// The VM may sleep now.
    Idle,
    /// The VM may not sleep now.
    Busy,
}

/// One running VM, as admission sees it.
#[derive(Debug, Clone)]
pub struct Running {
    /// The session of the VM.
    pub session: SessionId,
    /// The configured guest RAM of the VM, in bytes (decision CH8-6).
    pub guest_ram: u64,
    /// The last use of the VM. The smallest is the least recently used.
    pub last_use: Instant,
    /// The sleep-gate result of the VM.
    pub gate: Gate,
}

/// Why a start gets `host_full`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Full {
    /// The free bytes are below the floor, or the snapshots of the
    /// evictions would take them below it (decisions CH8-7, CH42 rule 3).
    Disk,
    /// No set of idle VMs frees enough RAM (decision CH8-6).
    Ram,
    /// Each slot of the pool is held, and no VM is idle to free one
    /// (decision CH58 rule 2).
    Slots,
}

/// The answer of admission.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Admission {
    /// Start the VM now.
    Admit,
    /// Snapshot and stop these VMs in this order, then start the VM.
    EvictFirst(Vec<SessionId>),
    /// Answer `host_full` and start nothing.
    HostFull(Full),
}

/// Decides whether a VM with `wanted` bytes of guest RAM may start beside
/// `running`, with `free` bytes free on the state root and `free_slots`
/// slots of the pool that no VM holds.
#[must_use]
pub fn decide(
    wanted: u64,
    running: &[Running],
    free: u64,
    free_slots: usize,
    limits: Limits,
) -> Admission {
    if free < limits.free_floor {
        return Admission::HostFull(Full::Disk);
    }
    let used = running
        .iter()
        .fold(0_u64, |sum, vm| sum.saturating_add(vm.guest_ram));
    let total = used.saturating_add(wanted);
    // The RAM that the evictions must free, 0 when the start fits the cap.
    let need = total.saturating_sub(limits.ram_cap);
    if need == 0 && free_slots > 0 {
        return Admission::Admit;
    }
    // Each evicted VM frees its slot, so any prefix of one VM or more
    // frees the slot that a full pool needs (decision CH58 rule 2).
    let mut idle: Vec<&Running> = running.iter().filter(|vm| vm.gate == Gate::Idle).collect();
    idle.sort_by_key(|vm| vm.last_use);
    let mut freed = 0_u64;
    let mut evict = Vec::new();
    for vm in idle {
        freed = freed.saturating_add(vm.guest_ram);
        evict.push(vm.session.clone());
        if freed >= need {
            // Each snapshot writes the whole guest RAM of its VM, so the
            // bytes written equal the RAM freed (decision CH42 rule 3).
            let holds = free
                .checked_sub(freed)
                .is_some_and(|left| left >= limits.free_floor);
            return if holds {
                Admission::EvictFirst(evict)
            } else {
                Admission::HostFull(Full::Disk)
            };
        }
    }
    Admission::HostFull(if need > 0 { Full::Ram } else { Full::Slots })
}

/// The bytes that an unprivileged user may still write on the filesystem
/// of `path`: the available blocks times the fragment size (statvfs).
///
/// # Errors
///
/// The error of `statvfs`, for example when `path` does not exist.
pub fn free_bytes(path: &Path) -> io::Result<u64> {
    let stat = rustix::fs::statvfs(path)?;
    Ok(stat.f_bavail.saturating_mul(stat.f_frsize))
}
