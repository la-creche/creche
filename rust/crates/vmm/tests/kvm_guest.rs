//! The kvm_ tests of the guest that the vmm composes (decisions CH7-6,
//! CH62 rules 17, 18 and 19). `guest_init::write_initramfs` packs the
//! static guest init of `bin/vm-smoke-setup.sh` into the digest store, and
//! Cloud Hypervisor boots that `store/<sha256>` file with the final
//! `CMDLINE` and a root disk that the test builds (decision CH62 rule
//! 12). The first test runs CH directly: no launch step, no scope and no
//! Landlock ruleset. `creche-vmm-confine/tests/kvm_vm.rs` proves those.
//!
//! `kvm_phase3_pulls_sleeps_restores_and_stops` is the phase 3 proof (plan
//! PR V10, section 5 of the design): `examples/phase3_vm.rs` plays the vmm
//! as a transient user service (decisions CH60 rule 1, CH62 rule 29), with
//! the stand-in facade in mode `linked-relay` on the root disk (CH62 rule
//! 12) and Rust stub planes in the test (CH62 rule 28). Its numbered steps
//! name what each one proves. `kvm_serial_cap_stops_the_vm_and_writes_the_fault`
//! proves the serial log cap of the idle tick (plan PR CP3, decision CH79
//! rule 7). The boot helpers sit in `tests/common/mod.rs`
//! and `tests/stand_in/mod.rs` (plan PR E3), which `tests/kvm_reach.rs`
//! shares.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. With no setup
//! or no `/dev/kvm` they fail. The runner's sudo makes the copies of
//! guest init and ch-remote root-owned, as the vmm row installs them
//! (decision CH36 G20).
#![cfg(target_os = "linux")]

mod common;
mod stand_in;

use std::fs::{self, OpenOptions};
use std::io::{self, BufRead as _, BufReader, Write as _};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::Receiver;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::guest_init::write_initramfs;
use creche_vmm::sleep::SERIAL_CAP;
use creche_vmm_confine::command::CMDLINE;
use creche_vmm_store::claim::CONSUMED;
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::layout::StateRoot;
use creche_wire::guest::{CHAPERONE_PORT, FACADE_STOP_WAIT, LITELLM_PORT, SESSION_DISK_MOUNT};
use creche_wire::{FacadeState, GuestPort};

use common::{BOOT, FAMILY, UP, files, lines, sudo, work};
use stand_in::{ROOT_DIRS, boot, ext4, stub_plane};

/// The image env of the test root disk (decision CH7-7).
const ENV: &[u8] = b"LANG=C.UTF-8\nPATH=/usr/bin:/bin\n";

/// The host programs that only the phase 3 proof runs.
const E2FSCK: &str = "/usr/sbin/e2fsck";
const DUMPE2FS: &str = "/usr/sbin/dumpe2fs";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A CH process, killed and reaped on drop, so a failed test leaves no VM.
struct Vm(Child);

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// The vmm packs a root-owned guest init into `store/<sha256>`, and CH
/// boots it with `CMDLINE` to `guest-init: up`. A wrong header, a wrong
/// `/init` mode or a wrong cmdline keeps the line from coming.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_boots_the_vmm_initramfs() {
    let files = files();
    let work = files.join(format!("kvm-guest-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();

    let program = work.join("guest-init");
    fs::copy(files.join("guest-init"), &program).unwrap();
    sudo(&["chown", "0:0"], &program);
    let state = StateRoot::new(work.join("state")).unwrap();
    let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
    let initramfs = write_initramfs(&program, &store).unwrap();
    assert!(initramfs.path().starts_with(work.join("state/store")));

    let tree = work.join("tree");
    let session_dir = SESSION_DISK_MOUNT.trim_start_matches('/');
    for dir in ROOT_DIRS.into_iter().chain([session_dir, "etc/creche"]) {
        fs::create_dir_all(tree.join(dir)).unwrap();
    }
    // guest init reads `/etc/creche/env` before `up` and powers off when
    // it is missing (decision CH62 rule 7). Two sorted lines in the
    // grammar of `creche-env-write`.
    fs::write(tree.join("etc/creche/env"), ENV).unwrap();
    let root = work.join("root.img");
    let session = work.join("session.img");
    ext4(&root, Some(&tree));
    ext4(&session, None);

    let serial = work.join("serial.log");
    // The root disk first, read-only, then the session disk (decisions
    // CH8-7, CH8-8, CH50 rule 10).
    let mut vm = Vm(Command::new(files.join("cloud-hypervisor"))
        .arg("--kernel")
        .arg(files.join("vmlinux"))
        .arg("--initramfs")
        .arg(initramfs.path())
        .args(["--cmdline", CMDLINE])
        .args(["--cpus", "boot=1", "--memory", "size=256M"])
        .arg("--disk")
        .arg(format!(
            "path={},readonly=on,image_type=raw",
            root.display()
        ))
        .arg("--disk")
        .arg(format!("path={},image_type=raw", session.display()))
        .arg("--serial")
        .arg(format!("file={}", serial.display()))
        .args(["--console", "off"])
        .stdin(Stdio::null())
        .spawn()
        .unwrap());

    let deadline = Instant::now() + BOOT;
    while !lines(&serial).iter().any(|line| line == UP) {
        let log = fs::read_to_string(&serial).unwrap_or_default();
        if let Some(status) = vm.0.try_wait().unwrap() {
            panic!("CH exited with {status} before {UP}. Serial:\n{log}");
        }
        assert!(
            Instant::now() < deadline,
            "no {UP} in {BOOT:?}. Serial:\n{log}"
        );
        sleep(Duration::from_millis(20));
    }
    assert!(vm.0.try_wait().unwrap().is_none(), "CH exited after up");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// The stub `plane` got one request with the `Host` header of guest
/// `port`, so the splice kept the bytes (decision CH6-2, fact 46).
#[cfg(test)]
fn hit(plane: &Receiver<String>, port: u16) {
    let head = plane.recv_timeout(BOOT).unwrap();
    let host = format!("\r\nHost: 127.0.0.1:{port}\r\n");
    assert!(head.contains(&host), "no {host:?} in {head:?}");
}

/// Sends `pull` on a new link to guest port 1201: `CONNECT 1201` on the
/// vsock socket of CH, as `AcpLink::dial` dials (decision CH13). The
/// stand-in pulls 1100 and GETs both planes (decision CH62 rule 13). It
/// gives the words after `pulled`: the facade pid, the reply key, the
/// status of :4000 and of :8300, and the pull time in ms. A fault fails
/// the proof `step`.
#[cfg(test)]
fn pull(vsock: &Path, step: &str) -> Vec<String> {
    let stream = UnixStream::connect(vsock).unwrap();
    stream.set_read_timeout(Some(BOOT)).unwrap();
    let mut reader = BufReader::new(&stream);
    writeln!(&stream, "CONNECT {}", GuestPort::Acp.number()).unwrap();
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    assert!(line.starts_with("OK "), "{step}: CONNECT gave {line:?}");
    writeln!(&stream, "pull").unwrap();
    line.clear();
    reader.read_line(&mut line).unwrap();
    let words: Vec<String> = line.split_whitespace().map(str::to_owned).collect();
    assert_eq!(words.len(), 6, "{step}: {line:?}");
    assert_eq!(words[0], "pulled", "{step}: {line:?}");
    words[1..].to_vec()
}

/// The phase 3 proof (plan PR V10, section 5 of the design): a facade in
/// a VM gets the family-state reply and reaches the stub planes after a
/// restore, and after the restore the vmm opens 1200 and 1201 again at
/// once, and the same facade answers (decision CH19 rules 1, 2 and 6).
/// The plan names three proofs, and each is one group of steps here, so
/// one VM serves all of them (plan risk 10). Each step names the plan PR
/// whose idea it needs. The job log holds both pull times (decision
/// CH23).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_phase3_pulls_sleeps_restores_and_stops() {
    let (litellm, litellm_heads) = stub_plane();
    let (chaperone, chaperone_heads) = stub_plane();
    let booted = boot("phase3", "linked-relay", (litellm, chaperone));
    let (mut vmm, vsock, session) = (booted.vmm, booted.vsock, booted.session);

    // 1. Cold boot: the start ends after the first ControlReady (G3). Its
    // facade is `starting` with start 1 (CH21-8, CH25 rule 2), or already
    // `running` when the facade line of step 2 came first.
    let (_, ready) = vmm.start_vm("step 1 cold boot (G3)");
    let (pid, starts) = match &ready {
        FacadeState::Starting(first) => (first.pid, first.starts.get()),
        FacadeState::Running(first) => (first.pid, first.starts.get()),
        other => panic!("step 1 cold boot (G3): the facade is {other:?}"),
    };
    assert_eq!(starts, 1, "step 1 cold boot (G3)");

    // 2. The facade opens its local link, and a facade line says
    // `running` with the same pid (G5, CH62 rule 11).
    let deadline = Instant::now() + BOOT;
    loop {
        let answer = vmm.ask("facade");
        let facade = answer.strip_prefix("facade ").unwrap();
        if let Ok(FacadeState::Running(running)) = serde_json::from_str::<FacadeState>(facade) {
            assert_eq!(running.pid, pid, "step 2 running (G4): another pid");
            break;
        }
        assert!(Instant::now() < deadline, "step 2 running (G5): {answer}");
        sleep(Duration::from_millis(100));
    }

    // 3. A pull on 1201 reaches the same facade (G6), which gets a
    // `state` reply on 1100 (G7, V8) and a 200 from each stub plane (V6).
    let first_pull = pull(&vsock, "step 3 first pull (G6)");
    assert_eq!(
        first_pull[..4],
        [pid.to_string(), "state".into(), "200".into(), "200".into()],
        "step 3 first pull (G6, G7, V6, V8)"
    );
    hit(&litellm_heads, LITELLM_PORT);
    hit(&chaperone_heads, CHAPERONE_PORT);

    // 4. Sleep: the guest syncs, then the VM snapshots into one
    // generation (G8, V9, CH62 rule 23).
    assert_eq!(vmm.ask("sleep"), "slept 1", "step 4 sleep (G8, V9)");
    let generations: Vec<PathBuf> = fs::read_dir(session.snapshots())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(generations.len(), 1, "step 4 sleep (V9): {generations:?}");

    // 5. Restore: the start claims that generation, and control::open and
    // AcpLink::dial succeed at once. The ready says `running` with the same
    // pid and start 1, so the same facade answers (CH19 rules 1 and 2). The
    // new serial log has no `up` line, which a cold boot writes.
    let (restore_ms, ready) = vmm.start_vm("step 5 restore (G8, V9)");
    assert!(
        generations[0].join(CONSUMED).is_file(),
        "step 5 restore (V9): no claim"
    );
    assert!(
        !lines(&session.serial_log()).iter().any(|line| line == UP),
        "step 5 restore (V9): the VM booted"
    );
    let FacadeState::Running(restored) = ready else {
        panic!("step 5 restore (G4, CH19 rule 2): the ready is {ready:?}");
    };
    assert_eq!(
        (restored.pid, restored.starts.get()),
        (pid, 1),
        "step 5 restore (G4)"
    );

    // 6. A second pull after the restore: the same pid, a state reply and
    // both planes, through fresh dials (CH6-2, CH62 rule 14, fact 74).
    let second_pull = pull(&vsock, "step 6 second pull (G6)");
    assert_eq!(
        second_pull[..4],
        first_pull[..4],
        "step 6 second pull (G6, G7, V6, V8)"
    );
    hit(&litellm_heads, LITELLM_PORT);
    hit(&chaperone_heads, CHAPERONE_PORT);
    // Decision CH23: the proof measures the pull. A direct write skips
    // the capture of the test harness, so the job log holds the line.
    let times = format!(
        "phase 3 proof: pull {} ms cold, {} ms after a restore, restore start {restore_ms} ms\n",
        first_pull[4], second_pull[4]
    );
    io::stderr().write_all(times.as_bytes()).unwrap();

    // 7. Stop: the graceful stop ends CH, and guest init synced and
    // remounted the session disk, so e2fsck finds it clean (CH19 rule 3,
    // CH62 rule 9). The facade got the stop line and exited by itself:
    // guest init sent it no signal. Guest init signals the facade only
    // after `FACADE_STOP_WAIT`, so the stop also ends before that.
    let answer = vmm.ask("stop");
    let ms: u64 = answer
        .strip_prefix("stopped ")
        .unwrap_or_else(|| panic!("step 7 stop: {answer}"))
        .parse()
        .unwrap();
    assert!(
        Duration::from_millis(ms) < FACADE_STOP_WAIT,
        "step 7 stop (CH19 rule 3): {ms} ms"
    );
    let serial = lines(&session.serial_log());
    for line in [
        "standin: stop line",
        "guest-init: session remounted ro",
        "guest-init: power off",
    ] {
        assert!(
            serial.iter().any(|each| each == line),
            "step 7 stop: no {line}: {serial:?}"
        );
    }
    for signal in ["SIGTERM", "SIGKILL"] {
        let line = format!("guest-init: facade pid {pid} sent {signal}");
        assert!(!serial.contains(&line), "step 7 stop (CH19 rule 3): {line}");
    }
    let header = Command::new(DUMPE2FS)
        .arg("-h")
        .arg(session.disk())
        .output()
        .unwrap();
    let header = String::from_utf8_lossy(&header.stdout);
    assert!(
        !header.contains("needs_recovery"),
        "step 7 stop: the journal waits for a replay"
    );
    let check = Command::new(E2FSCK)
        .arg("-fn")
        .arg(session.disk())
        .output()
        .unwrap();
    assert!(check.status.success(), "step 7 stop: e2fsck -fn: {check:?}");

    drop(vmm);
}

/// The serial log cap of the idle tick (plan PR CP3, decisions CH62 rule
/// 26, CH79 rule 7). The test writes the serial log of a running VM past
/// `SERIAL_CAP`, as a runaway guest would. The cap check stops the VM
/// with the graceful stop and writes `serial_log_full`, which does not
/// stop turns. The next start boots with a new log (decision CH59 rule 2),
/// and its `ControlReady` clears the fault.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_serial_cap_stops_the_vm_and_writes_the_fault() {
    let (litellm, _litellm_heads) = stub_plane();
    let (chaperone, _chaperone_heads) = stub_plane();
    let booted = boot("serial-cap", "linked-relay", (litellm, chaperone));
    let (mut vmm, session) = (booted.vmm, booted.session);
    let faults = work("serial-cap").join(format!("faults/vmm/{FAMILY}.json"));

    // 1. Cold boot.
    vmm.start_vm("step 1 cold boot");

    // 2. The log passes the cap. CH writes at its own offset, so its
    // later lines land inside these filler lines, each on a line of its
    // own.
    let mut filler = vec![b'x'; 1 << 20];
    for line_end in filler.iter_mut().skip(1023).step_by(1024) {
        *line_end = b'\n';
    }
    let mut log = OpenOptions::new()
        .append(true)
        .open(session.serial_log())
        .unwrap();
    for _ in 0..=SERIAL_CAP >> 20 {
        log.write_all(&filler).unwrap();
    }
    drop(log);
    assert!(fs::metadata(session.serial_log()).unwrap().len() > SERIAL_CAP);

    // 3. The cap check stops the VM gracefully (CH19 rule 3) and writes
    // the fault (CH79 rule 7).
    assert_eq!(vmm.ask("cap"), "capped 1", "step 3 cap (CH79 rule 7)");
    let serial = lines(&session.serial_log());
    for line in ["standin: stop line", "guest-init: power off"] {
        assert!(
            serial.iter().any(|each| each == line),
            "step 3 cap (CH19 rule 3): no {line}"
        );
    }
    let body: serde_json::Value = serde_json::from_slice(&fs::read(&faults).unwrap()).unwrap();
    assert_eq!(body["faults"][0]["code"], "serial_log_full", "step 3 cap");
    assert_eq!(body["faults"][0]["blocks_turns"], false, "step 3 cap");

    // 4. The next start cold-boots with a new log, and its ControlReady
    // clears the fault (CH59 rule 2, contract 05 §3.3.1 rule 8).
    vmm.start_vm("step 4 next start (CH59 rule 2)");
    assert!(
        fs::metadata(session.serial_log()).unwrap().len() < 1 << 20,
        "step 4 next start (CH59 rule 2): the log is not new"
    );
    assert!(
        lines(&session.serial_log()).iter().any(|line| line == UP),
        "step 4 next start: no cold boot"
    );
    let body: serde_json::Value = serde_json::from_slice(&fs::read(&faults).unwrap()).unwrap();
    assert_eq!(body["faults"], serde_json::json!([]), "step 4 next start");
    assert!(vmm.ask("stop").starts_with("stopped "), "step 5 stop");
    drop(vmm);
}
