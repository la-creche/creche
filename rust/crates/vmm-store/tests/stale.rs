//! A stale `vsock.sock`, `api.sock` and `api.sock.lock` go, and every
//! other file stays
//! (decision CH19 (4), fact 75).

use std::fs;
use std::path::PathBuf;

use creche_vmm_store::layout::{RunRoot, Slot, VmRunDir};
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
    let gone = [vm.vsock_socket(), vm.api_socket(), vm.api_socket_lock()];
    let kept = [vm.serial_log(), vm.host_listener(HostPort::Chaperone)];
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

// Decisions CH51 and CH55: through the `vm/<id>` link the files of the
// slot go, and after the unlink the slot is out of reach.
#[test]
fn removal_goes_through_the_slot_link() {
    let (run, _) = TempRun::new("slot");
    let root = RunRoot::new(run.0.clone()).expect("a run root");
    let id = ID.parse().expect("ID is a session id");
    let _ = fs::remove_dir_all(root.vms_dir());
    let vm = root
        .link_vm(&id, Slot::new(4).expect("slot 4"))
        .expect("the link");
    let slot = root.slot(Slot::new(4).expect("slot 4"));
    for name in ["vsock.sock", "api.sock", "api.sock.lock", "serial.log"] {
        fs::write(slot.join(name), b"").expect("a file");
    }

    remove_stale(&vm).expect("the stale files go");
    assert!(!slot.join("vsock.sock").exists());
    assert!(!slot.join("api.sock").exists());
    assert!(!slot.join("api.sock.lock").exists());
    assert!(slot.join("serial.log").exists());

    fs::write(slot.join("api.sock"), b"").expect("the next VM's socket");
    root.unlink_vm(&id).expect("the unlink");
    remove_stale(&vm).expect("a no-op");
    assert!(slot.join("api.sock").exists());
}
