//! The KVM smoke test (decisions CH8-4, CH36 G19). Cloud Hypervisor boots
//! the busybox guest of `bin/vm-smoke-setup.sh` with one vsock device, and
//! the guest writes its marker line to the serial file. The test runs CH
//! directly: no launch step, no scope and no Landlock ruleset.
//!
//! `#[ignore]`: only the job `kvm` runs it, after the setup script, with
//! `cargo test --locked -- --ignored kvm_`. A kvm_ test in any crate runs
//! there with no workflow edit. With no setup or no `/dev/kvm` it fails.
#![cfg(target_os = "linux")]

mod kvm;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use kvm::{BOOT, MARKER, Vm, booted, files};

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A new empty work directory for one VM.
#[cfg(test)]
fn work(files: &Path, name: &str) -> PathBuf {
    let work = files.join(format!("{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    work
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
