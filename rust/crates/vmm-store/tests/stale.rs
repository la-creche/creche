//! A stale `vsock.sock` and `api.sock.lock` go, and every other file stays
//! (decision CH19 (4), fact 75).

use std::fs;
use std::path::PathBuf;

use creche_vmm_store::layout::{RunRoot, VmRunDir};
use creche_vmm_store::stale::remove_stale;
use creche_wire::HostPort;

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";

/// A run root in a new directory under `/tmp`, removed on drop. The temp
/// dir of some hosts is too long for the socket limit of `RunRoot`.
struct TempRun(PathBuf);

// `cfg(test)` lets the helper use `expect` (rust/clippy.toml).
#[cfg(test)]
impl TempRun {
    fn new(name: &str) -> (Self, VmRunDir) {
        let path = PathBuf::from(format!("/tmp/vmm-stale-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        let run = RunRoot::new(path.clone()).expect("a run root");
        let vm = run.vm(&ID.parse().expect("ID is a session id"));
        fs::create_dir_all(vm.path()).expect("the VM run dir");
        (Self(path), vm)
    }
}

impl Drop for TempRun {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn stale_files_go_and_others_stay() {
    let (_run, vm) = TempRun::new("mixed");
    let gone = [vm.vsock_socket(), vm.api_socket_lock()];
    let kept = [
        vm.api_socket(),
        vm.serial_log(),
        vm.host_listener(HostPort::Chaperone),
    ];
    for path in gone.iter().chain(&kept) {
        fs::write(path, b"").expect("a file");
    }

    remove_stale(&vm).expect("the stale files go");

    for path in &gone {
        assert!(!path.exists(), "{} stayed", path.display());
    }
    for path in &kept {
        assert!(path.exists(), "{} went", path.display());
    }
}

#[test]
fn absent_files_are_no_error() {
    let (_run, vm) = TempRun::new("empty");

    remove_stale(&vm).expect("the stale files go");
    remove_stale(&vm).expect("the stale files go");
}

#[test]
fn a_failed_removal_names_the_file() {
    let (_run, vm) = TempRun::new("dir");
    fs::create_dir(vm.vsock_socket()).expect("a dir");
    fs::write(vm.api_socket_lock(), b"").expect("a file");

    let error = remove_stale(&vm).expect_err("a directory was removed");

    assert_eq!(error.path, vm.vsock_socket());
    assert!(vm.api_socket_lock().exists());
}
