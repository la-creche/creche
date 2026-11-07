//! The helpers of the kvm_ tests of guest init: the files of
//! `bin/vm-smoke-setup.sh`, the ext4 disks that each test builds with
//! `mke2fs -d` (decision CH62 rule 12) and a CH process killed on drop.
#![cfg(test)]

use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The host `mke2fs`, as the vmm kvm tests name it.
const MKE2FS: &str = "/usr/sbin/mke2fs";

/// The size of each test disk.
const DISK_BYTES: u64 = 16 << 20;

/// A boot takes about 0.6 s on the runner (fact 79).
pub const BOOT: Duration = Duration::from_secs(30);

/// The directory of the setup script.
pub fn files() -> PathBuf {
    let dir = std::env::var_os(DIR)
        .unwrap_or_else(|| panic!("{DIR} is not set: run bin/vm-smoke-setup.sh first"));
    PathBuf::from(dir)
}

/// A new empty work directory for one VM.
pub fn work(name: &str) -> PathBuf {
    let work = files().join(format!("{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    work
}

/// Writes an ext4 disk at `disk`. With `tree`, the disk holds a copy of
/// it. The root dir is owned by root, as on the session disk of the vmm.
pub fn ext4(disk: &Path, tree: Option<&Path>) {
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

/// The CH command of one VM: the guest kernel, the guest-init initramfs
/// of the setup script, the cmdline of decision CH62 rule 19 and the
/// serial port in `serial`. The caller adds the disks.
pub fn ch(serial: &Path) -> Command {
    let files = files();
    let mut command = Command::new(files.join("cloud-hypervisor"));
    command
        .arg("--kernel")
        .arg(files.join("vmlinux"))
        .arg("--initramfs")
        .arg(files.join("guest-init.cpio"))
        .args(["--cmdline", "console=ttyS0 rdinit=/init"])
        .args(["--cpus", "boot=1", "--memory", "size=256M"])
        .arg("--serial")
        .arg(format!("file={}", serial.display()))
        .args(["--console", "off"])
        .stdin(Stdio::null());
    command
}

/// A CH process of the test, killed and reaped on drop, so a failed test
/// leaves no VM.
pub struct Vm(pub Child);

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// The serial lines, with the CR of each CRLF line end cut.
pub fn lines(serial: &Path) -> Vec<String> {
    fs::read_to_string(serial)
        .unwrap_or_default()
        .lines()
        .map(|line| line.trim_end().to_owned())
        .collect()
}

/// Waits until `done` holds for the serial lines. It panics with the
/// serial text when CH exits first or `BOOT` passes.
pub fn wait_for(vm: &mut Vm, serial: &Path, what: &str, done: impl Fn(&[String]) -> bool) {
    let deadline = Instant::now() + BOOT;
    while !done(&lines(serial)) {
        if let Some(status) = vm.0.try_wait().unwrap() {
            let log = fs::read_to_string(serial).unwrap_or_default();
            panic!("CH exited with {status} before {what}. Serial:\n{log}");
        }
        let log = fs::read_to_string(serial).unwrap_or_default();
        assert!(
            Instant::now() < deadline,
            "no {what} in {BOOT:?}. Serial:\n{log}"
        );
        sleep(Duration::from_millis(20));
    }
}
