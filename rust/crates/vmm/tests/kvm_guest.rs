//! The kvm_ tests of the guest that the vmm composes (decisions CH7-6,
//! CH62 rules 17, 18 and 19). `guest_init::write_initramfs` packs the
//! static guest init of `bin/vm-smoke-setup.sh` into the digest store, and
//! Cloud Hypervisor boots that `store/<sha256>` file with the final
//! `CMDLINE` and a root disk that the test builds (decision CH62 rule
//! 12). The test runs CH directly: no launch step, no scope and no
//! Landlock ruleset. `creche-vmm-confine/tests/kvm_vm.rs` proves those.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. With no setup
//! or no `/dev/kvm` they fail. The runner's sudo makes the copy of guest
//! init root-owned, as the vmm row installs it (decision CH36 G20).
#![cfg(target_os = "linux")]

use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::guest_init::write_initramfs;
use creche_vmm_confine::command::CMDLINE;
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::layout::StateRoot;
use creche_wire::guest::SESSION_DISK_MOUNT;

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The host `mke2fs` of the vmm (`VMM_MKE2FS_PROGRAM`).
const MKE2FS: &str = "/usr/sbin/mke2fs";

/// The size of each test disk.
const DISK_BYTES: u64 = 16 << 20;

/// The image env of the test root disk (decision CH7-7).
const ENV: &[u8] = b"LANG=C.UTF-8\nPATH=/usr/bin:/bin\n";

/// The dirs of the test root disk: each mount point of the guest init
/// boot plan.
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

/// The line that guest init writes once it is up.
const UP: &str = "guest-init: up";

/// A boot takes about 0.6 s on the runner (fact 79).
const BOOT: Duration = Duration::from_secs(30);

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A CH process, killed and reaped on drop, so a failed test leaves no VM.
struct Vm(Child);

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// The directory of the setup script.
#[cfg(test)]
fn files() -> PathBuf {
    let dir = std::env::var_os(DIR)
        .unwrap_or_else(|| panic!("{DIR} is not set: run bin/vm-smoke-setup.sh first"));
    PathBuf::from(dir)
}

/// Runs `command` on `path` with the sudo of the runner. Decision CH36
/// G20 allows sudo on GitHub's throwaway runners only, never on the host.
#[cfg(test)]
fn sudo(command: &[&str], path: &Path) {
    assert_eq!(
        std::env::var("GITHUB_ACTIONS").as_deref(),
        Ok("true"),
        "sudo runs only on a GitHub runner (decision CH36 G20)"
    );
    let status = Command::new("sudo")
        .arg("-n")
        .args(command)
        .arg(path)
        .status()
        .unwrap();
    assert!(status.success(), "sudo {command:?}: {status}");
}

/// Writes an ext4 disk at `disk`. With `tree`, the disk holds a copy of
/// it. The root dir is owned by root, as on the session disk of the vmm.
#[cfg(test)]
fn ext4(disk: &Path, tree: Option<&Path>) {
    File::create(disk).unwrap().set_len(DISK_BYTES).unwrap();
    let mut command = Command::new(MKE2FS);
    command.args(["-q", "-F", "-t", "ext4", "-E", "root_owner=0:0"]);
    if let Some(tree) = tree {
        command.arg("-d").arg(tree);
    }
    let output = command.arg("--").arg(disk).output().unwrap();
    assert!(
        output.status.success(),
        "mke2fs failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// The serial lines, with the CR of each CRLF line end cut.
fn lines(serial: &Path) -> Vec<String> {
    fs::read_to_string(serial)
        .unwrap_or_default()
        .lines()
        .map(|line| line.trim_end().to_owned())
        .collect()
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
