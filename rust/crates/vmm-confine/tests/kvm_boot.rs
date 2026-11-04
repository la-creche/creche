//! The KVM smoke test (decisions CH8-4, CH36 G19). Cloud Hypervisor boots
//! the busybox guest of `bin/vm-smoke-setup.sh` with one vsock device, and
//! the guest writes its marker line to the serial file. The test runs CH
//! directly: no launch step, no scope and no Landlock ruleset.
//!
//! `#[ignore]`: only the job `kvm` runs it, after the setup script, with
//! `cargo test --locked -- --ignored kvm_`. A kvm_ test in any crate runs
//! there with no workflow edit. With no setup or no `/dev/kvm` it fails.
#![cfg(target_os = "linux")]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The line that the guest init writes once it runs.
const MARKER: &str = "CRECHE-KVM-BOOTED";

/// A boot takes about 0.6 s on the runner (fact 79).
const BOOT: Duration = Duration::from_secs(30);

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A CH process, killed on drop, so a failed test leaves no VM.
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

/// A new empty work directory for one VM.
#[cfg(test)]
fn work(files: &Path, name: &str) -> PathBuf {
    let work = files.join(format!("{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    work
}

/// Whether the serial file holds the marker as a line of its own. The
/// serial file has CRLF line ends.
fn booted(serial: &Path) -> bool {
    fs::read_to_string(serial).is_ok_and(|text| text.lines().any(|line| line.trim_end() == MARKER))
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_boots_the_busybox_guest() {
    let files = files();
    let work = work(&files, "kvm-boot");
    let serial = work.join("serial.log");
    let mut vm = Vm(Command::new(files.join("cloud-hypervisor"))
        .arg("--api-socket")
        .arg(format!("path={}", work.join("api.sock").display()))
        .arg("--kernel")
        .arg(files.join("vmlinux"))
        .arg("--initramfs")
        .arg(files.join("initramfs.cpio"))
        .args(["--cmdline", "console=ttyS0 rdinit=/init"])
        .args(["--cpus", "boot=1", "--memory", "size=256M"])
        .arg("--vsock")
        .arg(format!(
            "cid=3,socket={}",
            work.join("vsock.sock").display()
        ))
        .arg("--serial")
        .arg(format!("file={}", serial.display()))
        .args(["--console", "off"])
        .stdin(Stdio::null())
        .spawn()
        .unwrap());

    let deadline = Instant::now() + BOOT;
    while !booted(&serial) {
        if let Some(status) = vm.0.try_wait().unwrap() {
            let log = fs::read_to_string(&serial).unwrap_or_default();
            panic!("CH exited with {status} before the marker. Serial:\n{log}");
        }
        assert!(Instant::now() < deadline, "no {MARKER} in {BOOT:?}");
        sleep(Duration::from_millis(20));
    }

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}
