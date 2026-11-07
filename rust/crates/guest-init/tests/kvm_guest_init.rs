//! The kvm_ tests of guest init as PID 1 (decisions CH9, CH62 rules 4,
//! 7, 19, 25 and 26) and of control link 1200 (decisions CH13, CH19, CH28,
//! CH32). CH boots the guest kernel with the `guest-init.cpio` of
//! `bin/vm-smoke-setup.sh` and the disks that each test builds. The test
//! runs CH directly, with no vmm, and speaks the link itself.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. With no setup
//! or no `/dev/kvm` they fail.
#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::process::Command;
use std::time::{Duration, Instant};

use common::{
    DUMPE2FS, E2FSCK, GOOD_ENV, IN_2030, Vm, booted, ch, dial, hello, lines, logged, not_started,
    now, session_disk, vm_with_env, wait_for, wait_for_exit, wait_for_line, work,
};
use creche_wire::{ControlCommand, ControlStop, ControlSync, ControlSynced, GuestEvent, GuestPort};

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

/// Guest init sends nothing before the hello, then answers one ready that
/// says the facade did not start (decisions CH28, CH34 rule 7).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_answers_the_hello() {
    let (work, mut vm) = booted("kvm-guest-init-hello");
    let mut link = dial(&work, GuestPort::Control);
    assert!(
        link.quiet_for(Duration::from_millis(300)),
        "a line came before the hello"
    );
    assert_eq!(hello(&mut link, now()), not_started());
    wait_for_line(
        &mut vm,
        &work.join("serial.log"),
        "guest-init: control link 1 ready",
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// The hello sets CLOCK_REALTIME to `sent_at` before the ready (decisions
/// CH28, CH62 rule 25). The guest has no RTC driver, so its clock starts
/// near 1970, and the console line shows the clock after the set. The
/// `sent_at` has a millisecond part, so a set that drops it reads below
/// the window.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_sets_the_guest_clock_from_the_hello() {
    let (work, mut vm) = booted("kvm-guest-init-clock");
    let mut link = dial(&work, GuestPort::Control);
    let sent_at = IN_2030 + 999;
    assert_eq!(hello(&mut link, sent_at), not_started());

    let serial = work.join("serial.log");
    let prefix = "guest-init: control link 1 clock set: realtime ";
    wait_for(&mut vm, &serial, "the clock line", |lines| {
        lines.iter().any(|line| line.starts_with(prefix))
    });
    let lines = lines(&serial);
    let realtime: u64 = lines
        .iter()
        .find_map(|line| line.strip_prefix(prefix))
        .unwrap_or_else(|| panic!("no clock line. Serial:\n{}", lines.join("\n")))
        .parse()
        .unwrap();
    assert!(
        (sent_at..sent_at + 1_000).contains(&realtime),
        "the guest clock reads {realtime}, not {sent_at}"
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A second link replaces the first: guest init closes the first one, and
/// the second gets its own ready (decisions CH19 rule 2, CH28).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_new_control_link_replaces_the_old_one() {
    let (work, mut vm) = booted("kvm-guest-init-replace");
    let mut old = dial(&work, GuestPort::Control);
    assert_eq!(hello(&mut old, now()), not_started());
    let mut new = dial(&work, GuestPort::Control);
    assert_eq!(hello(&mut new, now()), not_started());

    assert_eq!(old.line(), None, "the old link stays open");
    let serial = work.join("serial.log");
    for line in [
        "guest-init: control link 1 closed: a newer link replaced it",
        "guest-init: control link 2 ready",
    ] {
        wait_for_line(&mut vm, &serial, line);
    }
    assert!(vm.0.try_wait().unwrap().is_none(), "CH exited");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A sync gets `synced` on the same link, after guest init ran `syncfs`
/// of the session disk and `sync` (decision CH62 rule 23). The link stays
/// ready, so a second sync gets a second `synced`. Plan PR G8 proves that
/// the writes reach the disk.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_answers_sync_with_synced() {
    let (work, mut vm) = booted("kvm-guest-init-sync");
    let mut link = dial(&work, GuestPort::Control);
    assert_eq!(hello(&mut link, now()), not_started());

    for _ in 0..2 {
        link.send(&ControlCommand::Sync(ControlSync {}));
        let line = link.line().expect("no synced: the link ended");
        let event: GuestEvent =
            serde_json::from_str(&line).unwrap_or_else(|error| panic!("{line:?}: {error}"));
        assert_eq!(event, GuestEvent::Synced(ControlSynced {}));
    }
    wait_for_line(
        &mut vm,
        &work.join("serial.log"),
        "guest-init: control link 1 synced",
    );
    assert!(vm.0.try_wait().unwrap().is_none(), "CH exited");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A stop powers the VM off within the 10 s of the plan, and the session
/// disk is clean after it: guest init synced and remounted it read-only
/// (decision CH19 rule 3).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_powers_off_on_stop() {
    let (work, mut vm) = booted("kvm-guest-init-stop");
    let mut link = dial(&work, GuestPort::Control);
    assert_eq!(hello(&mut link, now()), not_started());

    let sent = Instant::now();
    link.send(&ControlCommand::Stop(ControlStop {}));
    let serial = work.join("serial.log");
    let status = wait_for_exit(&mut vm, &serial);
    assert!(
        sent.elapsed() < Duration::from_secs(10),
        "CH exited {:?} after the stop",
        sent.elapsed()
    );
    let log = lines(&serial).join("\n");
    for line in [
        "guest-init: stop on control link 1",
        "guest-init: session remounted ro",
        "guest-init: power off",
    ] {
        assert!(
            logged(&serial, line),
            "no {line} (CH {status}). Serial:\n{log}"
        );
    }

    let disk = session_disk(&work);
    let header = Command::new(DUMPE2FS)
        .arg("-h")
        .arg(&disk)
        .output()
        .unwrap();
    let header = String::from_utf8_lossy(&header.stdout);
    let field = |name: &str| {
        header
            .lines()
            .find_map(|line| line.strip_prefix(name))
            .map(str::trim)
            .unwrap_or_else(|| panic!("no {name} in:\n{header}"))
            .to_owned()
    };
    assert_eq!(field("Filesystem state:"), "clean");
    assert!(
        !field("Filesystem features:").contains("needs_recovery"),
        "the journal waits for a replay"
    );
    let check = Command::new(E2FSCK).arg("-fn").arg(&disk).output().unwrap();
    assert!(
        check.status.success(),
        "e2fsck -fn: {}{}",
        String::from_utf8_lossy(&check.stdout),
        String::from_utf8_lossy(&check.stderr)
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}
