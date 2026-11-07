//! The kernel unpacks the archive of the vmm-store writer (decisions
//! CH7-6, CH62 rule 18). The test packs the static busybox of
//! `bin/vm-smoke-setup.sh` with the init script of the busybox guest, puts
//! the archive into a store as `store/<sha256>` (decision CH60 rule 2),
//! and Cloud Hypervisor boots that store copy to the marker line. A wrong
//! header makes the kernel skip `/init`, so the marker never comes. The
//! test runs CH directly: no launch step, no scope and no Landlock ruleset.
//!
//! `#[ignore]`: only the job `kvm` runs it, after the setup script, with
//! `cargo test --locked -- --ignored kvm_`. With no setup or no `/dev/kvm`
//! it fails. It reads `$CRECHE_KVM_DIR/busybox`, the program that plan PR
//! S2 keeps there.
#![cfg(target_os = "linux")]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_store::images::ImageStore;
use creche_vmm_store::initramfs::{Entry, Kind, pack};
use creche_vmm_store::layout::StateRoot;

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The line that the guest init writes once it runs.
const MARKER: &str = "CRECHE-KVM-BOOTED";

/// The init script of the busybox guest of `bin/vm-smoke-setup.sh`.
const INIT: &str = "#!/bin/busybox sh
/bin/busybox --install -s /bin
mount -t proc proc /proc
echo CRECHE-KVM-BOOTED
while :; do echo tick; sleep 1; done
";

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

/// Whether the serial file holds the marker as a line of its own. The
/// serial file has CRLF line ends.
fn booted(serial: &Path) -> bool {
    fs::read_to_string(serial).is_ok_and(|text| text.lines().any(|line| line.trim_end() == MARKER))
}

/// The busybox guest as writer entries: one entry of each kind.
#[cfg(test)]
fn entries(busybox: Vec<u8>) -> Vec<Entry> {
    let entry = |name: &str, mode, kind| Entry {
        name: name.to_owned(),
        mode,
        kind,
    };
    vec![
        entry("bin", 0o755, Kind::Dir),
        entry("bin/busybox", 0o755, Kind::File(busybox)),
        entry("dev", 0o755, Kind::Dir),
        entry(
            "dev/console",
            0o600,
            Kind::CharDevice { major: 5, minor: 1 },
        ),
        entry("init", 0o755, Kind::File(INIT.as_bytes().to_vec())),
        entry("proc", 0o755, Kind::Dir),
    ]
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_boots_a_writer_initramfs() {
    let files = files();
    let work = files.join(format!("kvm-initramfs-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();

    let busybox = fs::read(files.join("busybox")).unwrap();
    let archive = pack(&entries(busybox)).unwrap();
    let root = StateRoot::new(work.join("state")).unwrap();
    let store = ImageStore::new(&root, PathBuf::from("zstd"));
    let initramfs = store.put_made(&archive).unwrap();
    assert_eq!(fs::read(initramfs.path()).unwrap(), archive);

    let serial = work.join("serial.log");
    let mut vm = Vm(Command::new(files.join("cloud-hypervisor"))
        .arg("--api-socket")
        .arg(format!("path={}", work.join("api.sock").display()))
        .arg("--kernel")
        .arg(files.join("vmlinux"))
        .arg("--initramfs")
        .arg(initramfs.path())
        .args(["--cmdline", "console=ttyS0 rdinit=/init"])
        .args(["--cpus", "boot=1", "--memory", "size=256M"])
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
