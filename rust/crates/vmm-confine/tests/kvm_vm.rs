//! One VM through the code of the vmm, on the KVM of the runner (decision
//! CH8-4, plan PR C16). The example `launch_chain` in its role `hold`
//! plays the vmm: it holds the spawner (PR C2) and launches what the test
//! makes. Each launch goes through the real `systemd-run --user --scope`
//! with the scope limits of the family VM shape (PR C4) and under the
//! Landlock ruleset of the VM (PR C6, decisions CH8-3 and CH30).
//!
//! 1. A cold boot with the argv of PR C7 from checked store copies (PRs
//!    D6, D7). The test reads the scope limits back from the cgroup.
//! 2. A snapshot with dug holes (PR D9, decision CH29), keyed (PR D10)
//!    and claimed (PR D11, decision CH19 (5)).
//! 3. A copy-on-write restore (decisions CH2, CH29): the restored CH maps
//!    the snapshot's `memory-ranges`, and the guest runs on.
//! 4. A second snapshot to a new generation. The first one stays
//!    byte-identical (fact 55).
//! 5. A restore of the second one, then a SIGKILL of the vmm stand-in. CH
//!    dies by its death signal and leaves no scope (decision CH19 (4), the
//!    phase 2 proof line).
//!
//! The busybox guest has no guest init, so the control link, ControlReady
//! and the guest `sync` before a snapshot wait for phase 3. The test reads
//! readiness from the serial marker and from `ch-remote info`. CH is the
//! copy in `CRECHE_KVM_DIR`, which no AppArmor profile confines.
//!
//! `#[ignore]`: only the job `kvm` runs it, after `bin/vm-smoke-setup.sh`.
#![cfg(target_os = "linux")]

mod kvm;

use std::fs;
use std::io::{BufRead, BufReader, ErrorKind, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_confine::command::{Boot, ChCommand, DiskLimit};
use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::limits::VmLimits;
use creche_vmm_store::ch_remote::{ChRemote, VmState};
use creche_vmm_store::claim::{ClaimedSnapshot, claim};
use creche_vmm_store::images::{CheckedImage, ImageStore};
use creche_vmm_store::key::{
    CH_VERSION, ChVersionError, CheckedCh, KeyCheck, SnapshotKey, check_cloud_hypervisor,
    check_newest, write_key,
};
use creche_vmm_store::layout::{RunRoot, SessionDir, StateRoot, VmRunDir, scope_unit};
use creche_vmm_store::session_disk::{SessionDisk, SessionDisks};
use creche_vmm_store::snapshot::{MEMORY_RANGES, Snapshot, SnapshotTimeouts, Snapshotter};
use creche_vmm_store::stale::remove_stale;
use creche_wire::{FamilyShape, SessionId};

use kvm::{BOOT, Vm, booted, files};

const ID: &str = "0123abcd-0000-4000-8000-0000000c16c1";

/// The smallest family VM shape: 1 vCPU and 256 MiB.
const SHAPE: &str = r#"{"cpus":1,"flavor":"base","memory_mib":256}"#;

/// The host programs of the ubuntu-24.04 runner. The vmm config passes
/// each one (plan PR L20).
const SYSTEMD_RUN: &str = "/usr/bin/systemd-run";
const SYSTEMCTL: &str = "/usr/bin/systemctl";
const MKE2FS: &str = "/usr/sbin/mke2fs";
const FALLOCATE: &str = "/usr/bin/fallocate";
const ZSTD: &str = "/usr/bin/zstd";
const SHA256SUM: &str = "/usr/bin/sha256sum";

/// The timeout of each `ch-remote` call. The caller sets it (decision
/// CH44 rule 3).
const CALL: Duration = Duration::from_secs(10);

const TIMEOUTS: SnapshotTimeouts = SnapshotTimeouts {
    pause: CALL,
    snapshot: Duration::from_secs(30),
    shutdown: CALL,
};

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// The example binary, as in `tests/common`. Cargo builds it next to the
/// test's `deps` dir.
#[cfg(test)]
fn example() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let debug = exe.parent().and_then(|deps| deps.parent()).unwrap();
    debug.join("examples").join("launch_chain")
}

/// Polls `check` until it holds. Fails after `BOOT`.
#[cfg(test)]
fn until(what: &str, mut check: impl FnMut() -> bool) {
    let deadline = Instant::now() + BOOT;
    while !check() {
        assert!(Instant::now() < deadline, "no {what} in {BOOT:?}");
        sleep(Duration::from_millis(20));
    }
}

/// Whether `pid` runs and is not a zombie.
fn running(pid: u32) -> bool {
    fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|stat| {
        stat.rsplit_once(") ")
            .is_some_and(|(_, rest)| !rest.starts_with('Z'))
    })
}

/// The `sha256sum` lines of `paths`.
#[cfg(test)]
fn digests(paths: &[PathBuf]) -> String {
    let output = Command::new(SHA256SUM)
        .arg("--")
        .args(paths)
        .output()
        .unwrap();
    assert!(output.status.success(), "sha256sum: {output:?}");
    String::from_utf8(output.stdout).unwrap()
}

/// The files of a snapshot dir, sorted.
#[cfg(test)]
fn snapshot_files(dir: &Path) -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    paths.sort();
    paths
}

/// Whether the serial file holds a `tick` line of the guest loop.
fn ticked(serial: &Path) -> bool {
    fs::read_to_string(serial).is_ok_and(|text| text.lines().any(|line| line.trim_end() == "tick"))
}

/// The CH key check of gap G10. The upstream asset of fact 71 may print
/// its git tag, not the `CH_VERSION` of a `--rev` build (fact 78). Then
/// the test keys with a stand-in that prints `CH_VERSION`, so the key and
/// the claim still run. Phase 4 brings the CI-built CH (decision CH36
/// G19).
#[cfg(test)]
fn checked_ch(ch: &Path, work: &Path) -> CheckedCh {
    match check_cloud_hypervisor(ch, CALL) {
        Ok(checked) => checked,
        Err(ChVersionError::Mismatch(text)) => {
            assert!(text.contains("v54.0"), "not CH v54.0: {text}");
            let stand_in = work.join("ch-version");
            fs::write(&stand_in, format!("#!/bin/sh\necho {CH_VERSION}\n")).unwrap();
            fs::set_permissions(&stand_in, fs::Permissions::from_mode(0o755)).unwrap();
            check_cloud_hypervisor(&stand_in, CALL).unwrap()
        }
        Err(error) => panic!("cloud-hypervisor --version: {error:?}"),
    }
}

/// One session: the store copies, the session disk and the VM run dir. A
/// drop removes the state root and the run dir.
struct Setup {
    id: SessionId,
    uid: u32,
    work: PathBuf,
    session: SessionDir,
    vm: VmRunDir,
    ch: Program,
    remote: ChRemote,
    checked_ch: CheckedCh,
    kernel: CheckedImage,
    initramfs: CheckedImage,
    root: CheckedImage,
    disk: SessionDisk,
    limits: VmLimits,
}

#[cfg(test)]
impl Setup {
    fn new() -> Self {
        let files = files();
        let id: SessionId = ID.parse().unwrap();
        let uid = rustix::process::getuid().as_raw();
        // The real run root of decision CH20, as in `kvm_apparmor.rs`.
        let run = RunRoot::new(PathBuf::from(format!("/run/user/{uid}/vmm"))).unwrap();
        let vm = run.vm(&id);
        let _ = fs::remove_dir_all(vm.path());
        fs::create_dir_all(vm.path()).unwrap();
        let work = files.join(format!("kvm-vm-{}", std::process::id()));
        let _ = fs::remove_dir_all(&work);
        fs::create_dir(&work).unwrap();
        let state = StateRoot::new(work.clone()).unwrap();

        // The guest runs from its initramfs and mounts no root, so 1 MiB
        // of zeros stands in for the root disk.
        let root = work.join("root.img");
        fs::write(&root, vec![0_u8; 1 << 20]).unwrap();
        let store = ImageStore::new(&state, PathBuf::from(ZSTD));
        let import = |source: &Path| {
            let line = digests(&[source.to_owned()]);
            let digest = line.split_whitespace().next().unwrap().parse().unwrap();
            store.import(source, digest).unwrap();
            store.check(digest).unwrap()
        };
        let kernel = import(&files.join("vmlinux"));
        let initramfs = import(&files.join("initramfs.cpio"));
        let root = import(&root);
        let disk = SessionDisks::new(&state, PathBuf::from(MKE2FS))
            .for_boot(&id)
            .unwrap();
        let session = state.session(&id);
        // The ruleset opens the snapshot dir, so it exists before a launch.
        fs::create_dir_all(session.snapshots()).unwrap();

        let ch_path = files.join("cloud-hypervisor");
        let checked_ch = checked_ch(&ch_path, &work);
        let shape: FamilyShape = serde_json::from_str(SHAPE).unwrap();
        Self {
            id,
            uid,
            work,
            session,
            vm,
            ch: Program::new(ch_path).unwrap(),
            remote: ChRemote::new(files.join("ch-remote")),
            checked_ch,
            kernel,
            initramfs,
            root,
            disk,
            limits: VmLimits::from(shape),
        }
    }

    /// The launch of a cold boot.
    fn boot(&self) -> Launch {
        let initramfs = HostPath::new(self.initramfs.path().to_owned()).unwrap();
        let boot = Boot {
            kernel: &self.kernel,
            initramfs: Some(&initramfs),
            root: &self.root,
            session: &self.disk,
            disk: DiskLimit::SESSION,
            limits: self.limits,
        };
        let payload = ChCommand::boot(boot, &self.vm)
            .unwrap()
            .payload(self.ch.clone());
        let kernel = HostPath::new(self.kernel.path().to_owned()).unwrap();
        let start = Start::Boot {
            kernel,
            initramfs: Some(initramfs),
        };
        self.launch(start, payload)
    }

    /// The launch of a restore of `snapshot`. CH under the ruleset cannot
    /// unlink, so its sockets stay after it exits, and a stale
    /// `vsock.sock` fails the next start (fact 75). The test calls
    /// `remove_stale` of PR D1 first, as lifecycle does. A stale
    /// `api.sock` alone does not block a start, so it stays. The test
    /// also removes `serial.log`, so each tick it reads comes from the
    /// restored guest.
    fn restore(&self, snapshot: ClaimedSnapshot) -> Launch {
        remove_stale(&self.vm).unwrap();
        let serial = self.vm.serial_log();
        if let Err(error) = fs::remove_file(&serial)
            && error.kind() != ErrorKind::NotFound
        {
            panic!("remove {}: {error}", serial.display());
        }
        let payload = ChCommand::restore(snapshot, &self.vm)
            .unwrap()
            .payload(self.ch.clone());
        self.launch(Start::Restore, payload)
    }

    /// `payload` in the scope of the VM, under its ruleset.
    fn launch(&self, start: Start, payload: Payload) -> Launch {
        let root = HostPath::new(self.root.path().to_owned()).unwrap();
        let files = VmFiles::new(start, root, &self.session, &self.vm).unwrap();
        let systemd_run = Program::new(PathBuf::from(SYSTEMD_RUN)).unwrap();
        Launch::new(
            systemd_run,
            self.limits.scope(self.id.clone()),
            payload,
            files,
        )
    }

    /// The env of a program that talks to the user manager. A runner step
    /// may have no `XDG_RUNTIME_DIR` of its own (the C0 spike set it).
    fn user_env(&self, command: &mut Command) {
        command.env("XDG_RUNTIME_DIR", format!("/run/user/{}", self.uid));
    }

    /// Starts the vmm stand-in with `launch`. Returns it, killed on drop,
    /// and the pid that becomes CH.
    fn hold(&self, launch: &Launch) -> (Vm, u32) {
        let mut command = Command::new(example());
        command
            .arg("hold")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped());
        self.user_env(&mut command);
        let mut vmm = Vm(command.spawn().unwrap());
        let plan = serde_json::to_vec(launch).unwrap();
        vmm.0.stdin.take().unwrap().write_all(&plan).unwrap();
        // `systemd-run` shares the stdout, so the pid line may not come
        // first.
        let mut stdout = BufReader::new(vmm.0.stdout.as_mut().unwrap());
        let mut line = String::new();
        let pid = loop {
            line.clear();
            let read = stdout.read_line(&mut line).unwrap();
            assert!(read > 0, "the stand-in printed no pid");
            if let Ok(pid) = line.trim().parse() {
                break pid;
            }
        };
        drop(stdout);
        (vmm, pid)
    }

    /// Waits for the marker of a cold boot.
    fn wait_booted(&self, pid: u32) {
        until("boot marker", || {
            assert!(running(pid), "CH exited before the marker");
            booted(&self.vm.serial_log())
        });
    }

    /// Waits for `ch-remote info` to report the VM running.
    fn wait_running(&self, pid: u32) {
        until("running VM", || {
            assert!(running(pid), "CH exited before it ran");
            self.remote
                .info(&self.vm, CALL)
                .is_ok_and(|info| info.state == VmState::Running)
        });
    }

    /// Checks that `pid` runs in the VM scope with the C4 limits.
    fn check_scope(&self, pid: u32) {
        let cgroup = fs::read_to_string(format!("/proc/{pid}/cgroup")).unwrap();
        let path = cgroup.trim_end().strip_prefix("0::").unwrap();
        let dir = PathBuf::from(format!("/sys/fs/cgroup{path}"));
        let unit = format!("{}.scope", scope_unit(&self.id));
        assert_eq!(dir.file_name().unwrap().to_str(), Some(unit.as_str()));
        let read = |name: &str| fs::read_to_string(dir.join(name)).unwrap();
        let memory_max = self.limits.memory_max().get();
        assert_eq!(read("memory.max").trim_end(), memory_max.to_string());
        assert_eq!(read("cpu.max").trim_end(), "100000 100000");
        assert_eq!(read("pids.max").trim_end(), "256");
    }

    /// The loaded user units of the VM scope, in any state.
    fn units(&self) -> String {
        let mut command = Command::new(SYSTEMCTL);
        command
            .args(["--user", "list-units", "--all", "--plain", "--no-legend"])
            .args(["--no-pager", "--"])
            .arg(format!("{}.scope", scope_unit(&self.id)));
        self.user_env(&mut command);
        let output = command.output().unwrap();
        assert!(output.status.success(), "systemctl: {output:?}");
        String::from_utf8(output.stdout).unwrap()
    }

    /// Snapshots the VM of `pid`, which then exits, and checks the dig.
    fn snapshot(&self, pid: u32) -> Snapshot {
        let snapshotter = Snapshotter::new(self.remote.clone(), PathBuf::from(FALLOCATE));
        let snapshot = snapshotter.take(&self.vm, &self.session, TIMEOUTS).unwrap();
        until("CH exit after shutdown-vmm", || !running(pid));
        // The next launch needs the unit name (fact 76).
        until("scope removal after the exit", || {
            self.units().trim().is_empty()
        });
        // Without the dig the file takes the whole guest RAM (fact 72).
        assert!(
            snapshot.allocated_bytes < self.limits.guest_ram(),
            "no holes: {} bytes",
            snapshot.allocated_bytes
        );
        snapshot
    }

    /// Keys `snapshot`, checks the key and claims it.
    fn claim(&self, snapshot: &Snapshot) -> ClaimedSnapshot {
        let key = SnapshotKey::current(self.checked_ch, &self.kernel, &self.root);
        write_key(&self.session, snapshot.generation, &key).unwrap();
        let check = check_newest(&self.session, &key).unwrap();
        let KeyCheck::Keyed(keyed) = check else {
            panic!("the key check gave {check:?}");
        };
        assert_eq!(keyed.generation(), snapshot.generation);
        claim(keyed).unwrap().unwrap()
    }
}

impl Drop for Setup {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(self.vm.path());
        let _ = fs::remove_dir_all(&self.work);
    }
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_vm_snapshot_restore_kill() {
    let setup = Setup::new();

    // 1 and 2: a cold boot in its scope, then a snapshot.
    let (vmm, pid) = setup.hold(&setup.boot());
    setup.wait_booted(pid);
    setup.check_scope(pid);
    let first = setup.snapshot(pid);
    drop(vmm);
    let first_files = snapshot_files(&first.dir);
    let first_digests = digests(&first_files);

    // 3: a copy-on-write restore. The guest runs on.
    let (vmm, pid) = setup.hold(&setup.restore(setup.claim(&first)));
    setup.wait_running(pid);
    setup.check_scope(pid);
    let maps = fs::read_to_string(format!("/proc/{pid}/maps")).unwrap();
    let memory = first.dir.join(MEMORY_RANGES);
    assert!(
        maps.contains(memory.to_str().unwrap()),
        "the restored CH does not map {}",
        memory.display()
    );
    let serial = setup.vm.serial_log();
    until("guest tick after the restore", || ticked(&serial));

    // 4: a second snapshot to a new generation.
    let second = setup.snapshot(pid);
    drop(vmm);
    assert_eq!(second.generation.0, first.generation.0 + 1);
    assert_eq!(digests(&first_files), first_digests);

    // 5: a SIGKILL of the vmm takes CH and its scope down.
    let (mut vmm, pid) = setup.hold(&setup.restore(setup.claim(&second)));
    setup.wait_running(pid);
    assert!(!setup.units().trim().is_empty(), "no VM scope");
    vmm.0.kill().unwrap();
    vmm.0.wait().unwrap();
    until("CH death after the vmm kill", || !running(pid));
    until("scope removal", || setup.units().trim().is_empty());
}
