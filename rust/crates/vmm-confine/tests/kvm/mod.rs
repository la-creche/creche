//! The helpers of the kvm_ tests: the files of `bin/vm-smoke-setup.sh`
//! and a CH process that a drop kills.
#![cfg(test)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::Duration;

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The line that the guest init writes once it runs.
pub const MARKER: &str = "CRECHE-KVM-BOOTED";

/// A boot takes about 0.6 s on the runner (fact 79).
pub const BOOT: Duration = Duration::from_secs(30);

/// A CH process, or a vmm stand-in that holds one, killed on drop, so a
/// failed test leaves no VM.
pub struct Vm(pub Child);

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// The directory of the setup script.
pub fn files() -> PathBuf {
    let dir = std::env::var_os(DIR)
        .unwrap_or_else(|| panic!("{DIR} is not set: run bin/vm-smoke-setup.sh first"));
    PathBuf::from(dir)
}

/// Whether the serial file holds the marker as a line of its own. The
/// serial file has CRLF line ends.
pub fn booted(serial: &Path) -> bool {
    fs::read_to_string(serial).is_ok_and(|text| text.lines().any(|line| line.trim_end() == MARKER))
}
