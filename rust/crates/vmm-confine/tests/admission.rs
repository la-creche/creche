//! Table tests of admission: the RAM cap, the slot pool, the LRU idle
//! order and the free-space floor (decisions CH8-6, CH8-7, CH21-3, CH36
//! G14, CH42 rule 3, CH58 rule 2).

use std::path::Path;
use std::time::{Duration, Instant};

use creche_vmm_confine::admission::{
    Admission, FREE_FLOOR, Full, GIB, Gate, Limits, RAM_CAP, Running, decide, free_bytes,
};
use creche_wire::SessionId;

/// Free bytes well above the floor.
const ROOMY: u64 = 500 * GIB;

/// One free slot, so the pool is not full.
const SLOT: usize = 1;

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// The session id that ends in `n`.
#[cfg(test)]
fn id(n: u8) -> SessionId {
    format!("0123abcd-0000-4000-8000-0000000000{n:02x}")
        .parse()
        .unwrap()
}

/// A running VM with `gib` GiB of guest RAM, last used `age` seconds
/// before `now`.
#[cfg(test)]
fn vm(n: u8, gib: u64, age: u64, gate: Gate, now: Instant) -> Running {
    Running {
        session: id(n),
        guest_ram: gib * GIB,
        last_use: now.checked_sub(Duration::from_secs(age)).unwrap(),
        gate,
    }
}

#[test]
fn the_design_values_are_80_and_50_gib() {
    assert_eq!(RAM_CAP, 85_899_345_920);
    assert_eq!(FREE_FLOOR, 53_687_091_200);
    assert_eq!(Limits::DESIGN.ram_cap, RAM_CAP);
    assert_eq!(Limits::DESIGN.free_floor, FREE_FLOOR);
}

#[test]
fn the_ram_table() {
    let now = Instant::now() + Duration::from_secs(3600);
    // 64 GiB running: two idle VMs, one busy.
    let running = [
        vm(1, 16, 10, Gate::Idle, now),
        vm(2, 16, 300, Gate::Idle, now),
        vm(3, 32, 900, Gate::Busy, now),
    ];
    let busy = [
        vm(3, 32, 900, Gate::Busy, now),
        vm(4, 48, 5, Gate::Busy, now),
    ];
    let cases: &[(&str, u64, &[Running], Admission)] = &[
        ("nothing runs", 16 * GIB, &[], Admission::Admit),
        ("at the cap", 16 * GIB, &running, Admission::Admit),
        (
            "one byte over takes the LRU idle VM",
            16 * GIB + 1,
            &running,
            Admission::EvictFirst(vec![id(2)]),
        ),
        (
            "more than one idle VM frees",
            32 * GIB + 1,
            &running,
            Admission::EvictFirst(vec![id(2), id(1)]),
        ),
        (
            "the idle VMs free too little",
            48 * GIB + 1,
            &running,
            Admission::HostFull(Full::Ram),
        ),
        ("busy only, at the cap", 0, &busy, Admission::Admit),
        (
            "busy only, one byte over",
            1,
            &busy,
            Admission::HostFull(Full::Ram),
        ),
        (
            "more than the cap alone",
            RAM_CAP + 1,
            &[],
            Admission::HostFull(Full::Ram),
        ),
        ("the whole cap alone", RAM_CAP, &[], Admission::Admit),
    ];
    for (name, wanted, running, answer) in cases {
        let got = decide(*wanted, running, ROOMY, SLOT, Limits::DESIGN);
        assert_eq!(got, *answer, "{name}");
    }
}

#[test]
fn a_small_lru_idle_vm_goes_with_the_next_one() {
    let now = Instant::now() + Duration::from_secs(3600);
    // The LRU idle VM is small, so the next one is taken too.
    let running = [
        vm(1, 4, 600, Gate::Idle, now),
        vm(2, 32, 60, Gate::Idle, now),
        vm(3, 44, 1, Gate::Busy, now),
    ];
    let got = decide(8 * GIB, &running, ROOMY, SLOT, Limits::DESIGN);
    assert_eq!(got, Admission::EvictFirst(vec![id(1), id(2)]));
}

#[test]
fn the_floor_table() {
    let cases = [
        (
            "under the floor",
            FREE_FLOOR - 1,
            Admission::HostFull(Full::Disk),
        ),
        ("at the floor", FREE_FLOOR, Admission::Admit),
        ("no free byte", 0, Admission::HostFull(Full::Disk)),
    ];
    for (name, free, answer) in cases {
        assert_eq!(
            decide(GIB, &[], free, SLOT, Limits::DESIGN),
            answer,
            "{name}"
        );
    }
}

#[test]
fn the_floor_comes_before_an_eviction() {
    let now = Instant::now() + Duration::from_secs(3600);
    let running = [vm(1, 80, 600, Gate::Idle, now)];
    let low = decide(GIB, &running, FREE_FLOOR - 1, SLOT, Limits::DESIGN);
    assert_eq!(low, Admission::HostFull(Full::Disk));
}

#[test]
fn the_floor_holds_after_the_eviction_writes() {
    let now = Instant::now() + Duration::from_secs(3600);
    // 80 GiB running: two idle VMs of 16 GiB, one busy VM of 48 GiB.
    let running = [
        vm(1, 16, 10, Gate::Idle, now),
        vm(2, 16, 300, Gate::Idle, now),
        vm(3, 48, 900, Gate::Busy, now),
    ];
    let one = 16 * GIB;
    let two = 32 * GIB;
    let cases = [
        (
            "one snapshot leaves the floor",
            GIB,
            FREE_FLOOR + one,
            Admission::EvictFirst(vec![id(2)]),
        ),
        (
            "one snapshot breaks the floor by a byte",
            GIB,
            FREE_FLOOR + one - 1,
            Admission::HostFull(Full::Disk),
        ),
        (
            "two snapshots leave the floor",
            one + 1,
            FREE_FLOOR + two,
            Admission::EvictFirst(vec![id(2), id(1)]),
        ),
        (
            "two snapshots break the floor by a byte",
            one + 1,
            FREE_FLOOR + two - 1,
            Admission::HostFull(Full::Disk),
        ),
        (
            "a start with no eviction needs only the floor",
            0,
            FREE_FLOOR,
            Admission::Admit,
        ),
        (
            "too little RAM even with every idle VM",
            two + 1,
            ROOMY,
            Admission::HostFull(Full::Ram),
        ),
    ];
    for (name, wanted, free, answer) in cases {
        assert_eq!(
            decide(wanted, &running, free, SLOT, Limits::DESIGN),
            answer,
            "{name}"
        );
    }
}

#[test]
fn a_zero_floor_still_needs_room_for_the_snapshots() {
    let now = Instant::now() + Duration::from_secs(3600);
    let limits = Limits {
        ram_cap: 8 * GIB,
        free_floor: 0,
    };
    let running = [vm(1, 8, 60, Gate::Idle, now)];
    assert_eq!(
        decide(GIB, &running, 8 * GIB, SLOT, limits),
        Admission::EvictFirst(vec![id(1)])
    );
    assert_eq!(
        decide(GIB, &running, 8 * GIB - 1, SLOT, limits),
        Admission::HostFull(Full::Disk)
    );
}

#[test]
fn other_limits_hold() {
    let limits = Limits {
        ram_cap: 8 * GIB,
        free_floor: 0,
    };
    assert_eq!(decide(8 * GIB, &[], 0, SLOT, limits), Admission::Admit);
    assert_eq!(
        decide(8 * GIB + 1, &[], 0, SLOT, limits),
        Admission::HostFull(Full::Ram)
    );
}

/// 64 VMs of 1 GiB, all busy but VM `idle`, which is the only idle one.
/// They take each slot of the pool and 64 of the 80 GiB.
#[cfg(test)]
fn full_pool(idle: Option<u8>, now: Instant) -> Vec<Running> {
    (0..64)
        .map(|n| {
            let gate = if Some(n) == idle {
                Gate::Idle
            } else {
                Gate::Busy
            };
            vm(n, 1, u64::from(n) + 1, gate, now)
        })
        .collect()
}

#[test]
fn a_full_pool_evicts_its_one_idle_vm() {
    let now = Instant::now() + Duration::from_secs(3600);
    // The 65th start fits the RAM cap, but no slot is free. The one idle
    // VM goes, and the start takes its slot (decision CH58 rule 2).
    let running = full_pool(Some(7), now);
    assert_eq!(
        decide(GIB, &running, ROOMY, 0, Limits::DESIGN),
        Admission::EvictFirst(vec![id(7)])
    );
    // A free slot admits the start with no eviction.
    assert_eq!(
        decide(GIB, &running, ROOMY, SLOT, Limits::DESIGN),
        Admission::Admit
    );
}

#[test]
fn a_full_pool_of_busy_vms_is_host_full() {
    let now = Instant::now() + Duration::from_secs(3600);
    let running = full_pool(None, now);
    assert_eq!(
        decide(GIB, &running, ROOMY, 0, Limits::DESIGN),
        Admission::HostFull(Full::Slots)
    );
}

#[test]
fn a_full_pool_evicts_the_lru_idle_vm_once() {
    let now = Instant::now() + Duration::from_secs(3600);
    // VM 1 is used least recently. One eviction frees the one slot.
    let running = [
        vm(1, 8, 600, Gate::Idle, now),
        vm(2, 8, 60, Gate::Idle, now),
    ];
    assert_eq!(
        decide(GIB, &running, ROOMY, 0, Limits::DESIGN),
        Admission::EvictFirst(vec![id(1)])
    );
    // The floor holds after the snapshot of the evicted VM.
    assert_eq!(
        decide(GIB, &running, FREE_FLOOR + 8 * GIB - 1, 0, Limits::DESIGN),
        Admission::HostFull(Full::Disk)
    );
    // When the RAM needs more, the RAM sets the prefix.
    assert_eq!(
        decide(RAM_CAP - 8 * GIB + 1, &running, ROOMY, 0, Limits::DESIGN),
        Admission::EvictFirst(vec![id(1), id(2)])
    );
}

#[test]
fn free_bytes_reads_a_real_filesystem() {
    assert!(free_bytes(Path::new("/tmp")).unwrap() > 0);
    assert!(free_bytes(Path::new("/nonexistent/vmm-admission")).is_err());
}
