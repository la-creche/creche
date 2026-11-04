//! Table tests of the family VM limits: the family VM shape mapped to the
//! Cloud Hypervisor sizes and the scope limits (decisions CH8-5, CH8-2).

use std::ffi::OsString;
use std::path::PathBuf;

use creche_vmm_confine::launch::{Payload, Program};
use creche_vmm_confine::limits::{BALLOON, MEMORY_HEADROOM_MIB, MIB, VmLimits};
use creche_wire::{FamilyShape, SessionId};

const ID: &str = "0123abcd-0000-4000-8000-00000000c4c4";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// The limits of the shape file text with `cpus` and `memory_mib`.
#[cfg(test)]
fn limits(cpus: u8, memory_mib: u16) -> VmLimits {
    let text = format!(r#"{{"cpus":{cpus},"flavor":"base","memory_mib":{memory_mib}}}"#);
    let shape: FamilyShape = serde_json::from_str(&text).unwrap();
    VmLimits::from(shape)
}

#[test]
fn the_headroom_is_512_mib() {
    assert_eq!(MEMORY_HEADROOM_MIB, 512);
    assert_eq!(MIB, 1_048_576);
}

#[test]
fn each_cpu_count_gives_its_vcpus_and_quota() {
    for (cpus, quota) in [(1, 100), (2, 200), (4, 400), (8, 800)] {
        let limits = limits(cpus, 2048);
        assert_eq!(limits.vcpus(), cpus);
        assert_eq!(limits.cpu_quota().get(), quota, "cpus {cpus}");
    }
}

#[test]
fn each_guest_ram_gives_its_memory_max() {
    let table = [
        (256, 768, 805_306_368),
        (2048, 2560, 2_684_354_560),
        (8192, 8704, 9_126_805_504),
        (16384, 16896, 17_716_740_096),
    ];
    for (memory, max_mib, max_bytes) in table {
        let limits = limits(2, memory);
        assert_eq!(limits.guest_ram_mib(), u64::from(memory));
        assert_eq!(limits.guest_ram(), u64::from(memory) * MIB);
        assert_eq!(limits.memory_max_mib(), max_mib, "memory {memory}");
        assert_eq!(limits.memory_max().get(), max_bytes, "memory {memory}");
    }
}

#[test]
fn the_ch_args_hold_the_cpus_the_ram_and_the_balloon() {
    let table = [
        (2, 2048, "boot=2", "size=2048M"),
        (4, 8192, "boot=4", "size=8192M"),
        (8, 16384, "boot=8", "size=16384M"),
    ];
    for (cpus, memory, boot, size) in table {
        let expected = ["--cpus", boot, "--memory", size, "--balloon", BALLOON];
        assert_eq!(limits(cpus, memory).ch_args(), expected);
    }
}

#[test]
fn the_balloon_reports_free_pages_and_deflates_on_oom() {
    assert_eq!(BALLOON, "size=0,deflate_on_oom=on,free_page_reporting=on");
}

#[test]
fn the_scope_carries_the_limits_into_systemd_run() {
    let id: SessionId = ID.parse().unwrap();
    let program = Program::new(PathBuf::from("/opt/ch/cloud-hypervisor")).unwrap();
    let payload = Payload::new(program, Vec::new());
    let scope = limits(8, 16384).scope(id);
    assert_eq!(scope.unit(), format!("creche-vm-{ID}"));
    let args = scope.systemd_run_args(&payload);
    for arg in ["MemoryMax=17716740096", "CPUQuota=800%", "TasksMax=256"] {
        assert!(args.contains(&OsString::from(arg)), "no {arg} in {args:?}");
    }
}

#[test]
fn the_flavor_changes_no_limit() {
    let text = r#"{"cpus":4,"flavor":"python","memory_mib":8192}"#;
    let python: FamilyShape = serde_json::from_str(text).unwrap();
    assert_eq!(VmLimits::from(python), limits(4, 8192));
}
