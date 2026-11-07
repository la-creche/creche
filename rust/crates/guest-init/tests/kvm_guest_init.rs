//! The kvm_ tests of guest init as PID 1 (decisions CH9, CH62 rules 4,
//! 7, 19, 25 and 26), of control link 1200 (decisions CH13, CH19, CH28,
//! CH32), of the facade supervisor (decisions CH19 rule 1, CH62 rules
//! 6, 8, 9 and 12) and of the local link (decisions CH21, CH32 rules 6
//! and 7, CH62 rules 10 and 11). CH boots the guest kernel with the `guest-init.cpio`
//! of `bin/vm-smoke-setup.sh` and the disks that each test builds, with
//! the stand-in facade of the script on the root disk. The test runs CH
//! directly, with no vmm, and speaks the link itself.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. With no setup
//! or no `/dev/kvm` they fail.
#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::num::NonZeroU32;
use std::thread;
use std::time::{Duration, Instant};

use common::{
    GOOD_ENV, IN_2030, Link, SESSION, Standin, Vm, assert_clean, booted, booted_with, ch, dial,
    exited, exited_after, hello, lines, logged, now, running, standin_turn_end, starting,
    vm_with_env, wait_for, wait_for_exit, wait_for_line, work,
};
use creche_wire::guest::WORKSPACE;
use creche_wire::{
    ControlCommand, ControlStop, ControlSync, ControlSynced, FacadeState, GuestEvent, GuestPort,
    TurnEvent, TurnStart,
};

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
/// says the facade of the hello is starting (decisions CH21-8, CH28, CH34
/// rule 7).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_answers_the_hello() {
    let (work, mut vm) = booted("kvm-guest-init-hello");
    let mut link = dial(&work, GuestPort::Control);
    assert!(
        link.quiet_for(Duration::from_millis(300)),
        "a line came before the hello"
    );
    starting(&hello(&mut link, now()));
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
    starting(&hello(&mut link, sent_at));

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
/// the second gets its own ready with the same facade (decisions CH19
/// rules 1 and 2, CH28).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_new_control_link_replaces_the_old_one() {
    let (work, mut vm) = booted("kvm-guest-init-replace");
    let mut old = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut old, now()));
    let mut new = dial(&work, GuestPort::Control);
    assert_eq!(starting(&hello(&mut new, now())), pid);

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
    starting(&hello(&mut link, now()));

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

/// A stop sends SIGTERM to a facade with no local link and powers the VM
/// off within the 10 s of the plan, and the session disk is clean after
/// it: guest init synced and remounted it read-only (decisions CH19 rule
/// 3, CH62 rule 9, the no-link stop gap of `AGENTS.md` Known gaps). The
/// facade exit goes out on the link first (decision CH43 rule 8).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_powers_off_on_stop() {
    let (work, mut vm) = booted("kvm-guest-init-stop");
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));

    let sent = Instant::now();
    link.send(&ControlCommand::Stop(ControlStop {}));
    assert_eq!(link.event(), exited(1));
    let serial = work.join("serial.log");
    let status = wait_for_exit(&mut vm, &serial);
    assert!(
        sent.elapsed() < Duration::from_secs(10),
        "CH exited {:?} after the stop",
        sent.elapsed()
    );
    let log = lines(&serial).join("\n");
    for line in [
        "guest-init: stop on control link 1".to_owned(),
        format!("guest-init: facade pid {pid} sent SIGTERM"),
        format!("guest-init: facade pid {pid} exited: signal 15"),
        "guest-init: stop done".to_owned(),
        "guest-init: session remounted ro".to_owned(),
        "guest-init: power off".to_owned(),
    ] {
        assert!(
            logged(&serial, &line),
            "no {line} (CH {status}). Serial:\n{log}"
        );
    }

    assert_clean(&work);

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A process outside the facade group misses the SIGTERM of the stop
/// and holds a file of the session disk open for writing. The power-off
/// sends it SIGKILL and waits until it is gone, so the read-only remount
/// passes and the session disk is clean (decisions CH62 rule 9, CH66
/// rule 2).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_kills_every_process_left_at_the_power_off() {
    let (work, mut vm) = booted_with("kvm-guest-init-holder", Standin::Holder);
    let serial = work.join("serial.log");
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));
    wait_for_line(&mut vm, &serial, "standin: holder up");

    link.send(&ControlCommand::Stop(ControlStop {}));
    assert_eq!(link.event(), exited(1));
    let status = wait_for_exit(&mut vm, &serial);
    let log = lines(&serial).join("\n");
    for line in [
        format!("guest-init: facade pid {pid} exited: signal 15"),
        "guest-init: every process killed".to_owned(),
        "guest-init: session remounted ro".to_owned(),
        "guest-init: power off".to_owned(),
    ] {
        assert!(
            logged(&serial, &line),
            "no {line} (CH {status}). Serial:\n{log}"
        );
    }
    assert!(
        !lines(&serial)
            .iter()
            .any(|line| line.starts_with("guest-init: processes left")),
        "the power-off wait ran out. Serial:\n{log}"
    );
    assert_clean(&work);

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// Guest init starts the stand-in only after the hello, with the session
/// id of the hello, the image env and the workspace as its cwd (decisions
/// CH21-8, CH49 rule 1, CH62 rules 5 and 6). Its stderr reaches the
/// console, and its stdout does not (decision CH62 rule 26).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_starts_the_facade_with_the_session_id() {
    let (work, mut vm) = booted("kvm-guest-init-facade");
    let serial = work.join("serial.log");
    let mut link = dial(&work, GuestPort::Control);
    assert!(
        !lines(&serial)
            .iter()
            .any(|line| line.starts_with("standin: ")),
        "the facade started before the hello"
    );

    let pid = starting(&hello(&mut link, now()));
    for line in [
        format!("guest-init: facade pid {pid} started"),
        format!("standin: session {SESSION}"),
        "standin: env CRECHE_SESSION_ID LANG PATH".to_owned(),
        format!("standin: cwd {WORKSPACE}"),
        "standin: up".to_owned(),
    ] {
        wait_for_line(&mut vm, &serial, &line);
    }
    assert!(
        !logged(&serial, "standin: stdout"),
        "stdout reached the console"
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// An exited facade starts again after the restart pause, so the link gets
/// `starting` 1, `exited` 1, then `starting` 2 with a new pid (decisions
/// CH19 rule 1, CH43 rule 8, CH62 rule 8). The child that the first start
/// left in its process group gets SIGKILL before the second start
/// (decision CH66 rule 3).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_restarts_an_exited_facade() {
    let (work, mut vm) = booted_with("kvm-guest-init-restart", Standin::ExitsOnce);
    let mut link = dial(&work, GuestPort::Control);
    let first = starting(&hello(&mut link, now()));

    assert_eq!(link.event(), exited(1));
    let second = match link.event() {
        GuestEvent::Facade(FacadeState::Starting(second)) => second,
        other => panic!("no second start: {other:?}"),
    };
    assert_eq!(second.starts.get(), 2);
    assert_eq!(second.last_turn_end, None);
    assert_ne!(second.pid, first, "the second start has the first pid");

    let serial = work.join("serial.log");
    for line in [
        format!("guest-init: facade pid {first} exited: exit status 3"),
        format!("guest-init: facade pid {first} sent SIGKILL"),
        format!("guest-init: facade pid {} started", second.pid),
        "standin: first child gone".to_owned(),
        "standin: up".to_owned(),
    ] {
        wait_for_line(&mut vm, &serial, &line);
    }

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A control link that closes leaves the facade running: the ready of a
/// new link names the same pid and one start (decision CH19 rule 1).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_keeps_the_facade_when_the_control_link_closes() {
    let (work, mut vm) = booted("kvm-guest-init-keep");
    let serial = work.join("serial.log");
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));
    wait_for_line(&mut vm, &serial, "standin: up");
    drop(link);

    let mut next = dial(&work, GuestPort::Control);
    assert_eq!(starting(&hello(&mut next, now())), pid);
    let exit = format!("guest-init: facade pid {pid} exited");
    assert!(
        !lines(&serial).iter().any(|line| line.starts_with(&exit)),
        "the facade exited"
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// Reads the lines that the stand-in of mode `turn` causes on `link`
/// after the ready: the open of its local link, then its turn start and
/// its turn end, each followed by the facade state it changed (decisions
/// CH21, CH32 rule 6, CH43 rule 8).
fn read_the_turn(link: &mut Link, pid: NonZeroU32) {
    let end = standin_turn_end();
    for expected in [
        GuestEvent::Facade(running(pid, false, None)),
        GuestEvent::Turn(TurnEvent::Start(TurnStart {})),
        GuestEvent::Facade(running(pid, true, None)),
        GuestEvent::Turn(TurnEvent::End(end.clone())),
        GuestEvent::Facade(running(pid, false, Some(end.clone()))),
    ] {
        assert_eq!(link.event(), expected);
    }
}

/// The stand-in sends a turn start, then a turn end, on its local link,
/// and both arrive on 1200. The ready of a new link carries the end
/// (decisions CH21, CH32 rule 4, CH62 rules 10 and 11).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_relays_turn_events() {
    let (work, mut vm) = booted_with("kvm-guest-init-turn", Standin::Turn);
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));
    read_the_turn(&mut link, pid);
    wait_for_line(&mut vm, &work.join("serial.log"), "standin: turn sent");

    let mut next = dial(&work, GuestPort::Control);
    let ready = hello(&mut next, now());
    assert_eq!(ready.facade, running(pid, false, Some(standin_turn_end())));

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A child of the stand-in opens the local link too. Guest init closes it
/// at once, because its pid is not the facade, and the facade stays
/// running with its link (decision CH62 rule 10).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_refuses_a_stranger_on_the_local_link() {
    let (work, mut vm) = booted_with("kvm-guest-init-stranger", Standin::Turn);
    let serial = work.join("serial.log");
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));
    read_the_turn(&mut link, pid);
    for line in [
        "standin: stranger closed",
        "standin: stranger exit status: 0",
    ] {
        wait_for_line(&mut vm, &serial, line);
    }
    let closed = "guest-init: local link 2 closed: pid ";
    assert!(
        lines(&serial).iter().any(|line| line.starts_with(closed)
            && line.ends_with(" is not a facade that waits for its link")),
        "no refusal of the stranger. Serial:\n{}",
        lines(&serial).join("\n")
    );
    assert!(
        link.quiet_for(Duration::from_millis(500)),
        "the facade state changed"
    );

    let mut next = dial(&work, GuestPort::Control);
    assert_eq!(
        hello(&mut next, now()).facade,
        running(pid, false, Some(standin_turn_end()))
    );
    assert!(vm.0.try_wait().unwrap().is_none(), "CH exited");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A stop on 1200 writes the stop line on the local link. The stand-in
/// logs it and exits with status 0, with no SIGTERM, and CH exits
/// (decision CH62 rule 9).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_stops_the_facade_through_the_local_link() {
    let (work, mut vm) = booted_with("kvm-guest-init-local-stop", Standin::Turn);
    let serial = work.join("serial.log");
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));
    read_the_turn(&mut link, pid);
    // The stand-in reads its link only after the stranger is done.
    wait_for_line(&mut vm, &serial, "standin: stranger closed");

    let sent = Instant::now();
    link.send(&ControlCommand::Stop(ControlStop {}));
    assert_eq!(link.event(), exited_after(1, Some(standin_turn_end())));
    let status = wait_for_exit(&mut vm, &serial);
    assert!(
        sent.elapsed() < Duration::from_secs(10),
        "CH exited {:?} after the stop",
        sent.elapsed()
    );
    let log = lines(&serial).join("\n");
    for line in [
        "guest-init: local link 1 stop".to_owned(),
        "standin: stop line".to_owned(),
        format!("guest-init: facade pid {pid} exited: exit status 0"),
        "guest-init: stop done".to_owned(),
    ] {
        assert!(
            logged(&serial, &line),
            "no {line} (CH {status}). Serial:\n{log}"
        );
    }
    assert!(
        !logged(
            &serial,
            &format!("guest-init: facade pid {pid} sent SIGTERM")
        ),
        "the stop sent SIGTERM. Serial:\n{log}"
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A facade that never reads the stop line and streams turns on its
/// local link keeps the inbox of guest init full. The stop still sends
/// SIGTERM when `FACADE_STOP_WAIT` (25 s) ends, because the control loop
/// checks its timer before it reads the inbox (decision CH62 rule 9).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_ends_the_stop_wait_under_a_stream_of_turns() {
    let (work, mut vm) = booted_with("kvm-guest-init-stream", Standin::Stream);
    let serial = work.join("serial.log");
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));
    assert_eq!(link.event(), GuestEvent::Facade(running(pid, false, None)));

    let sent = Instant::now();
    link.send(&ControlCommand::Stop(ControlStop {}));
    // The guest writes each turn on 1200, so the link is read to its end
    // in a thread, and the loop never blocks on a full vsock buffer.
    let drained = thread::spawn(move || link.last_line());
    let sigterm = format!("guest-init: facade pid {pid} sent SIGTERM");
    wait_for_line(&mut vm, &serial, &sigterm);
    let waited = sent.elapsed();
    assert!(
        waited >= Duration::from_secs(25) && waited < Duration::from_secs(30),
        "SIGTERM came {waited:?} after the stop"
    );
    let status = wait_for_exit(&mut vm, &serial);
    let log = lines(&serial).join("\n");
    for line in [
        "guest-init: local link 1 stop".to_owned(),
        format!("guest-init: facade pid {pid} exited: signal 15"),
        "guest-init: stop done".to_owned(),
    ] {
        assert!(
            logged(&serial, &line),
            "no {line} (CH {status}). Serial:\n{log}"
        );
    }
    let last = drained.join().unwrap().expect("no line on 1200");
    assert!(
        matches!(
            serde_json::from_str(&last),
            Ok(GuestEvent::Facade(FacadeState::Exited(_)))
        ),
        "the last line on 1200 is {last:?}"
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// A local link that closes outside a stop kills the facade group with
/// SIGKILL, and the restart follows: `running`, `exited` 1, then
/// `starting` 2 on 1200 (decisions CH32 rule 7, CH62 rule 8).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_init_kills_the_facade_when_its_local_link_closes() {
    let (work, mut vm) = booted_with("kvm-guest-init-close", Standin::CloseOnce);
    let serial = work.join("serial.log");
    let mut link = dial(&work, GuestPort::Control);
    let pid = starting(&hello(&mut link, now()));

    assert_eq!(link.event(), GuestEvent::Facade(running(pid, false, None)));
    assert_eq!(link.event(), exited(1));
    let second = match link.event() {
        GuestEvent::Facade(FacadeState::Starting(second)) => second,
        other => panic!("no second start: {other:?}"),
    };
    assert_eq!(second.starts.get(), 2);
    assert_ne!(second.pid, pid, "the second start has the first pid");
    for line in [
        "standin: link closed".to_owned(),
        format!("guest-init: facade pid {pid} sent SIGKILL"),
        format!("guest-init: facade pid {pid} exited: signal 9"),
        format!("guest-init: facade pid {} started", second.pid),
        "standin: up".to_owned(),
    ] {
        wait_for_line(&mut vm, &serial, &line);
    }
    assert!(
        !logged(
            &serial,
            &format!("guest-init: facade pid {pid} sent SIGTERM")
        ),
        "the close sent SIGTERM"
    );

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}
