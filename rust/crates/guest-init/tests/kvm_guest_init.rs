//! The kvm_ tests of guest init as PID 1 (decisions CH9, CH62 rules 4,
//! 19 and 26). CH boots the guest kernel with the `guest-init.cpio` of
//! `bin/vm-smoke-setup.sh` and the disks that each test builds. The test
//! runs CH directly, with no vmm.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. With no setup
//! or no `/dev/kvm` they fail.
#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::thread::sleep;
use std::time::{Duration, Instant};

use common::{BOOT, Vm, ch, ext4, lines, wait_for, work};
use creche_wire::guest::SESSION_DISK_MOUNT;

/// The dirs of the test root disk: each mount point of the boot plan.
const ROOT_DIRS: [&str; 8] = [
    "dev",
    "proc",
    "sys",
    "tmp",
    "run",
    "root",
    "var/cache",
    "var/tmp",
];

/// With no disk, the root mount fails. Guest init names it on the console
/// and powers the VM off, so CH exits. Without the power-off, PID 1 exits,
/// the kernel panics, `PANIC_TIMEOUT=0` keeps CH running and the test
/// times out (fact 53).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_powers_off_with_no_root_disk() {
    let work = work("kvm-guest-init-off");
    let serial = work.join("serial.log");
    let mut vm = Vm(ch(&serial).spawn().unwrap());

    let deadline = Instant::now() + BOOT;
    let status = loop {
        if let Some(status) = vm.0.try_wait().unwrap() {
            break status;
        }
        let log = fs::read_to_string(&serial).unwrap_or_default();
        assert!(
            Instant::now() < deadline,
            "CH runs after {BOOT:?}. Serial:\n{log}"
        );
        sleep(Duration::from_millis(20));
    };

    let lines = lines(&serial);
    let log = lines.join("\n");
    assert!(
        lines
            .iter()
            .any(|line| line.starts_with("guest-init: root mount failed: ")),
        "no root mount fault (CH {status}). Serial:\n{log}"
    );
    assert!(
        lines.iter().any(|line| line == "guest-init: power off"),
        "no power off line. Serial:\n{log}"
    );
    assert!(!lines.iter().any(|line| line == "guest-init: up"), "{log}");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// The root disk mounts read-only and the session disk read-write, and
/// guest init reaches `up` after the switch root and `lo`.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_mounts_both_disks() {
    let work = work("kvm-guest-init-disks");
    let tree = work.join("tree");
    let session_dir = SESSION_DISK_MOUNT.trim_start_matches('/');
    for dir in ROOT_DIRS.into_iter().chain([session_dir]) {
        fs::create_dir_all(tree.join(dir)).unwrap();
    }
    let root = work.join("root.img");
    let session = work.join("session.img");
    ext4(&root, Some(&tree));
    ext4(&session, None);

    let serial = work.join("serial.log");
    // The root disk first, read-only, then the session disk (decisions
    // CH8-7, CH8-8, CH50 rule 10).
    let mut vm = Vm(ch(&serial)
        .arg("--disk")
        .arg(format!(
            "path={},readonly=on,image_type=raw",
            root.display()
        ))
        .arg("--disk")
        .arg(format!("path={},image_type=raw", session.display()))
        .spawn()
        .unwrap());

    wait_for(&mut vm, &serial, "guest-init: up", |lines| {
        lines.iter().any(|line| line == "guest-init: up")
    });
    let lines = lines(&serial);
    let log = lines.join("\n");
    for line in [
        "guest-init: root mounted ro",
        "guest-init: session mounted rw",
        "guest-init: root switched",
        "guest-init: lo up",
    ] {
        assert!(
            lines.iter().any(|each| each == line),
            "no {line}. Serial:\n{log}"
        );
    }
    assert!(vm.0.try_wait().unwrap().is_none(), "CH exited after up");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}
