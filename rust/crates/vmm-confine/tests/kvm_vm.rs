//! One VM through the code of the vmm, on the KVM of the runner (decision
//! CH8-4, plan PR C16). The example `launch_chain` in its role `hold`
//! plays the vmm: it holds the spawner (PR C2) and launches what the test
//! makes. Each launch goes through the real chain of `tests/kvm_slot.rs`:
//! the launch step, `systemd-run --user --scope` with the scope limits of
//! the family VM shape (PR C4), and the launch stage, which enters the
//! slot label and applies the Landlock ruleset of the VM before it execs
//! the installed CH (decisions CH8-3, CH55, CH58 rule 1). Each start takes
//! a new slot with `RunRoot::link_vm` and then runs `remove_stale`, as
//! lifecycle does (PR L16b). So each restore moves the VM to another slot,
//! and the paths in the snapshot hold through the `vm/<id>` link (PR
//! D16). The run root is the real `/run/user/<uid>/vmm` and the state root
//! is the default `~/.local/state/creche-vmm`, which the slot profiles
//! name (decisions CH20, CH50 rule 11).
//!
//! 1. A cold boot with the argv of PR C7 from checked store copies (PRs
//!    D6, D7). The test reads the scope limits back from the cgroup, and
//!    the label of the slot from CH.
//! 2. A snapshot with dug holes (PR D9, decision CH29), keyed (PR D10)
//!    and claimed (PR D11, decision CH19 (5)).
//! 3. A copy-on-write restore (decisions CH2, CH29): the restored CH maps
//!    the snapshot's `memory-ranges`, and the guest runs on.
//! 4. A second snapshot to a new generation. The first one stays
//!    byte-identical (fact 55).
//! 5. A restore of the second one, then a SIGKILL of the vmm stand-in. CH
//!    is meant to die by its death signal and leave no scope (decision
//!    CH19 (4), the phase 2 proof line). This step fails today: AppArmor
//!    clears the death signal at the exec into the slot label (the
//!    CONTRACT-QUESTION in the crate AGENTS.md). The step proves the
//!    mechanism that David picks there.
//!
//! The busybox guest has no guest init, so the control link, ControlReady
//! and the guest `sync` before a snapshot wait for phase 3. The test reads
//! readiness from the serial marker and from `ch-remote info`.
//!
//! `#[ignore]`: only the job `kvm` runs it, after `bin/vm-smoke-setup.sh`.
#![cfg(target_os = "linux")]

mod kvm;
mod scope_guard;

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_confine::command::{Boot, ChCommand, DiskLimit};
use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::limits::VmLimits;
use creche_vmm_confine::slot_profile::{CH_BINARY, label};
use creche_vmm_store::ch_remote::{ChRemote, VmState};
use creche_vmm_store::claim::{ClaimedSnapshot, claim};
use creche_vmm_store::images::{CheckedImage, ImageDigest, ImageStore};
use creche_vmm_store::key::{
    CH_VERSION, ChVersionError, CheckedCh, KeyCheck, SnapshotKey, check_cloud_hypervisor,
    check_newest, write_key,
};
use creche_vmm_store::layout::{RunRoot, SessionDir, Slot, StateRoot, VmRunDir, scope_unit};
use creche_vmm_store::session_disk::{SessionDisk, SessionDisks};
use creche_vmm_store::snapshot::{MEMORY_RANGES, Snapshot, SnapshotTimeouts, Snapshotter};
use creche_vmm_store::stale::remove_stale;
use creche_wire::{FamilyShape, SessionId};

use kvm::{BOOT, Vm, booted, files};
use scope_guard::ScopeGuard;

const ID: &str = "0123abcd-0000-4000-8000-0000000c16c1";

/// The slot of the cold boot, then of the first and the second restore.
/// `tests/kvm_slot.rs` uses none of them.
const SLOTS: [u16; 3] = [32, 33, 34];

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

/// Fails if `path` exists. A live vmm may own a path under the two
/// roots, so the test stops at one it did not make, and a drop removes
/// only its own.
#[cfg(test)]
fn absent(path: &Path) {
    assert!(
        fs::symlink_metadata(path).is_err(),
        "{} exists: a vmm may be live, so the test stops",
        path.display()
    );
}

/// The AppArmor label of `pid`.
#[cfg(test)]
fn label_of(pid: u32) -> String {
    let attr = format!("/proc/{pid}/attr/apparmor/current");
    fs::read_to_string(attr).unwrap().trim_end().to_owned()
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

/// A vmm stand-in that holds one CH. A drop kills the stand-in, then the
/// scope of the VM with CH in it. Each test takes it after its `Setup`,
/// so it drops first.
struct Held {
    vmm: Vm,
    _scope: ScopeGuard,
    pid: u32,
}

/// One session: the store copies, the session disk and the VM link in
/// each slot of `SLOTS`. A drop removes only what the setup made.
struct Setup {
    id: SessionId,
    work: PathBuf,
    run: RunRoot,
    session: SessionDir,
    vm: VmRunDir,
    store: Vec<PathBuf>,
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
        let runtime = format!("/run/user/{uid}");
        assert_eq!(
            std::env::var("XDG_RUNTIME_DIR").ok().as_deref(),
            Some(runtime.as_str()),
            "systemd-run --user needs XDG_RUNTIME_DIR: bin/vm-smoke-setup.sh exports it"
        );
        let run = RunRoot::new(PathBuf::from(format!("{runtime}/vmm"))).unwrap();
        let vm = run.vm(&id);
        absent(vm.path());
        for slot in SLOTS {
            absent(&run.slot(Slot::new(slot).unwrap()));
        }
        let home = PathBuf::from(std::env::var_os("HOME").unwrap());
        let state = StateRoot::new(home.join(".local/state/creche-vmm")).unwrap();
        let session = state.session(&id);
        absent(session.path());
        let work = files.join(format!("kvm-vm-{}", std::process::id()));
        let _ = fs::remove_dir_all(&work);
        fs::create_dir(&work).unwrap();

        // The guest runs from its initramfs and mounts no root, so 1 MiB
        // of zeros stands in for the root disk.
        let root = work.join("root.img");
        fs::write(&root, vec![0_u8; 1 << 20]).unwrap();
        let images = ImageStore::new(&state, PathBuf::from(ZSTD));
        let mut store = Vec::new();
        let mut import = |source: &Path| {
            let line = digests(&[source.to_owned()]);
            let digest: ImageDigest = line.split_whitespace().next().unwrap().parse().unwrap();
            for path in [images.path(&digest), images.identity_path(&digest)] {
                absent(&path);
                store.push(path);
            }
            images.import(source, digest).unwrap();
            images.check(digest).unwrap()
        };
        let kernel = import(&files.join("vmlinux"));
        let initramfs = import(&files.join("initramfs.cpio"));
        let root = import(&root);
        let disk = SessionDisks::new(&state, PathBuf::from(MKE2FS))
            .for_boot(&id)
            .unwrap();
        // The ruleset opens the snapshot dir, so it exists before a launch.
        fs::create_dir_all(session.snapshots()).unwrap();

        let ch = PathBuf::from(CH_BINARY);
        let checked_ch = checked_ch(&ch, &work);
        let shape: FamilyShape = serde_json::from_str(SHAPE).unwrap();
        Self {
            id,
            work,
            run,
            session,
            vm,
            store,
            ch: Program::new(ch).unwrap(),
            remote: ChRemote::new(files.join("ch-remote")),
            checked_ch,
            kernel,
            initramfs,
            root,
            disk,
            limits: VmLimits::from(shape),
        }
    }

    /// Takes `slot` for the next start, as lifecycle does: the link of
    /// `vm/<id>` to the slot, which empties the slot dir, then
    /// `remove_stale` of PR D1 (decision CH56 rule 1, fact 75).
    fn take(&self, slot: Slot) {
        self.run.link_vm(&self.id, slot).unwrap();
        remove_stale(&self.vm).unwrap();
    }

    /// The launch of a cold boot in `slot`.
    fn boot(&self, slot: Slot) -> Launch {
        self.take(slot);
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
        self.launch(start, payload, slot)
    }

    /// The launch of a restore of `snapshot` in `slot`. The new slot dir is
    /// empty, so each tick that the test reads comes from the restored
    /// guest. The snapshot names its files by the `vm/<id>` path, which
    /// now links to `slot`.
    fn restore(&self, snapshot: ClaimedSnapshot, slot: Slot) -> Launch {
        self.take(slot);
        let payload = ChCommand::restore(snapshot, &self.vm)
            .unwrap()
            .payload(self.ch.clone());
        self.launch(Start::Restore, payload, slot)
    }

    /// `payload` in the scope of the VM, under its ruleset and the label
    /// of `slot`.
    fn launch(&self, start: Start, payload: Payload, slot: Slot) -> Launch {
        let root = HostPath::new(self.root.path().to_owned()).unwrap();
        let files = VmFiles::new(start, root, &self.session, &self.vm).unwrap();
        let systemd_run = Program::new(PathBuf::from(SYSTEMD_RUN)).unwrap();
        Launch::new(
            systemd_run,
            self.limits.scope(self.id.clone()),
            payload,
            files,
        )
        .in_slot(slot)
    }

    /// Starts the vmm stand-in with `launch`. Returns it with the guard
    /// of the VM scope, both killed on drop, and the pid that becomes CH.
    /// The guard comes first, so a failed assert in any phase leaves no
    /// CH and no scope.
    fn hold(&self, launch: &Launch) -> Held {
        let scope = ScopeGuard::new(&self.id);
        let mut vmm = Vm(Command::new(example())
            .arg("hold")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap());
        let plan = serde_json::to_vec(launch).unwrap();
        vmm.0.stdin.take().unwrap().write_all(&plan).unwrap();
        // The chain writes to the output pipe of the spawner, so the one
        // line on the stdout of the stand-in is the pid.
        let mut line = String::new();
        BufReader::new(vmm.0.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        Held {
            vmm,
            _scope: scope,
            pid: line.trim().parse().unwrap(),
        }
    }

    /// Waits for the marker of a cold boot, then checks the label of
    /// `slot` (decision CH55).
    fn wait_booted(&self, pid: u32, slot: Slot) {
        until("boot marker", || {
            assert!(running(pid), "CH exited before the marker");
            booted(&self.vm.serial_log())
        });
        assert_eq!(label_of(pid), format!("{} (enforce)", label(slot)));
    }

    /// Waits for `ch-remote info` to report the VM running, then checks
    /// the label of `slot` (decision CH55).
    fn wait_running(&self, pid: u32, slot: Slot) {
        until("running VM", || {
            assert!(running(pid), "CH exited before it ran");
            self.remote
                .info(&self.vm, CALL)
                .is_ok_and(|info| info.state == VmState::Running)
        });
        assert_eq!(label_of(pid), format!("{} (enforce)", label(slot)));
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
        let _ = self.run.unlink_vm(&self.id);
        for slot in SLOTS.into_iter().filter_map(Slot::new) {
            let _ = fs::remove_dir_all(self.run.slot(slot));
        }
        let _ = fs::remove_dir_all(self.session.path());
        for path in &self.store {
            let _ = fs::remove_file(path);
        }
        let _ = fs::remove_dir_all(&self.work);
    }
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_vm_snapshot_restore_kill() {
    let setup = Setup::new();

    // 1 and 2: a cold boot in its scope, then a snapshot.
    let [boot, restore, last] = SLOTS.map(|slot| Slot::new(slot).unwrap());
    let held = setup.hold(&setup.boot(boot));
    let pid = held.pid;
    setup.wait_booted(pid, boot);
    setup.check_scope(pid);
    let first = setup.snapshot(pid);
    drop(held);
    let first_files = snapshot_files(&first.dir);
    let first_digests = digests(&first_files);

    // 3: a copy-on-write restore in a new slot. The guest runs on.
    let held = setup.hold(&setup.restore(setup.claim(&first), restore));
    let pid = held.pid;
    setup.wait_running(pid, restore);
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
    drop(held);
    assert_eq!(second.generation.0, first.generation.0 + 1);
    assert_eq!(digests(&first_files), first_digests);

    // 5: a SIGKILL of the vmm is meant to take CH and its scope down.
    // This fails today under the slot label (the CONTRACT-QUESTION in the
    // crate AGENTS.md).
    let mut held = setup.hold(&setup.restore(setup.claim(&second), last));
    let pid = held.pid;
    setup.wait_running(pid, last);
    assert!(!setup.units().trim().is_empty(), "no VM scope");
    held.vmm.0.kill().unwrap();
    held.vmm.0.wait().unwrap();
    until("CH death after the vmm kill", || !running(pid));
    until("scope removal", || setup.units().trim().is_empty());
}
