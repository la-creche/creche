//! The kvm_ tests of guest init as PID 1 (decisions CH9, CH62 rules 4,
//! 7, 19 and 26). CH boots the guest kernel with the `guest-init.cpio` of
//! `bin/vm-smoke-setup.sh` and the disks that each test builds. The test
//! runs CH directly, with no vmm.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. With no setup
//! or no `/dev/kvm` they fail.
#![cfg(target_os = "linux")]

mod common;

use std::fs;

use common::{Vm, ch, lines, vm_with_env, wait_for, wait_for_exit, work};

/// A valid env file, as `playpen/bin/creche-env-write` writes it.
const GOOD_ENV: &[u8] = b"LANG=C.UTF-8\nPATH=/usr/bin:/bin\n";

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
    let status = wait_for_exit(&mut vm, &serial);

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
/// guest init reaches `up` after the switch root, `lo` and the env file.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_mounts_both_disks() {
    let work = work("kvm-guest-init-disks");
    let serial = work.join("serial.log");
    let mut vm = Vm(vm_with_env(&work, GOOD_ENV).spawn().unwrap());

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
        "guest-init: env read: 2 names",
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

/// An env file that lists a name twice: guest init names line 2 on the
/// console and powers off before `up`, so CH exits (decision CH62
/// rule 7).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_powers_off_on_a_bad_env_file() {
    let work = work("kvm-guest-init-bad-env");
    let serial = work.join("serial.log");
    let mut vm = Vm(vm_with_env(&work, b"A=1\nA=2\n").spawn().unwrap());
    let status = wait_for_exit(&mut vm, &serial);

    let lines = lines(&serial);
    let log = lines.join("\n");
    assert!(
        lines
            .iter()
            .any(|line| line == "guest-init: env refused: line 2: A is listed twice"),
        "no env refusal (CH {status}). Serial:\n{log}"
    );
    assert!(
        lines.iter().any(|line| line == "guest-init: power off"),
        "no power off line. Serial:\n{log}"
    );
    assert!(!lines.iter().any(|line| line == "guest-init: up"), "{log}");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}
