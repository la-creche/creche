//! Pins every path of the per-VM layout (gap G5, decisions CH20, CH24),
//! and the slot dirs with their `vm/<id>` links (decisions CH51, CH55).

use std::fs;
use std::path::{Path, PathBuf};

use creche_vmm_store::layout::{
    RootError, RunRoot, SCOPE_PREFIX, SLOTS, SOCKET_PATH_MAX, Slot, SnapshotGeneration, StateRoot,
    scope_unit,
};
use creche_wire::{HostPort, SessionId};

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";

// `cfg(test)` lets the helper use `expect` (rust/clippy.toml).
#[cfg(test)]
fn id() -> SessionId {
    ID.parse().expect("ID is a session id")
}

#[test]
fn state_paths_are_pinned() {
    let state = StateRoot::new(PathBuf::from("/var/lib/vmm")).expect("a state root");
    let session = state.session(&id());

    assert_eq!(state.path(), Path::new("/var/lib/vmm"));
    assert_eq!(
        state.row_file(&id()),
        Path::new(&format!("/var/lib/vmm/rows/{ID}.json"))
    );
    assert_eq!(state.rows_dir(), Path::new("/var/lib/vmm/rows"));
    assert_eq!(
        state.row_temp_file(&id()),
        Path::new(&format!("/var/lib/vmm/rows/{ID}.json.tmp"))
    );
    assert_eq!(
        session.path(),
        Path::new(&format!("/var/lib/vmm/sessions/{ID}"))
    );
    assert_eq!(session.disk(), session.path().join("disk.ext4"));
    assert_eq!(session.disk_temp(), session.path().join("disk.ext4.tmp"));
    assert_eq!(session.snapshots(), session.path().join("snapshots"));
    assert_eq!(
        session.snapshot(SnapshotGeneration(7)),
        session.path().join("snapshots/7")
    );
    assert_eq!(
        session.snapshot_key(SnapshotGeneration(7)),
        session.path().join("snapshots/7/key.json")
    );
}

#[test]
fn run_paths_are_pinned() {
    let run = RunRoot::new(PathBuf::from("/run/user/1000/vmm")).expect("a run root");
    let vm = run.vm(&id());
    let dir = format!("/run/user/1000/vmm/vm/{ID}");

    assert_eq!(run.vmm_socket(), Path::new("/run/user/1000/vmm/vmm.sock"));
    assert_eq!(vm.path(), Path::new(&dir));
    assert_eq!(vm.vsock_socket(), Path::new(&format!("{dir}/vsock.sock")));
    assert_eq!(vm.api_socket(), Path::new(&format!("{dir}/api.sock")));
    assert_eq!(
        vm.api_socket_lock(),
        Path::new(&format!("{dir}/api.sock.lock"))
    );
    assert_eq!(vm.serial_log(), Path::new(&format!("{dir}/serial.log")));
    assert_eq!(
        vm.host_listener(HostPort::Egress),
        Path::new(&format!("{dir}/vsock.sock_1104"))
    );
}

// Decision CH24: one `<socket>_*` pattern covers every host listener.
#[test]
fn listeners_match_the_socket_pattern() {
    let vm = RunRoot::new(PathBuf::from("/run/vmm"))
        .expect("a run root")
        .vm(&id());
    let pattern = format!("{}_", vm.vsock_socket().display());

    for &port in HostPort::ALL {
        let listener = vm.host_listener(port).display().to_string();
        let suffix = listener.strip_prefix(&pattern).expect("the pattern prefix");
        assert_eq!(suffix.parse::<u32>().expect("a port number"), port.number());
    }
}

#[test]
fn scope_unit_has_the_prefix() {
    assert_eq!(SCOPE_PREFIX, "creche-vm-");
    assert_eq!(scope_unit(&id()), format!("creche-vm-{ID}"));
}

#[test]
fn a_root_is_absolute_with_no_parent() {
    for path in ["vmm", "", "./vmm"] {
        assert_eq!(StateRoot::new(path.into()), Err(RootError::NotAbsolute));
        assert_eq!(RunRoot::new(path.into()), Err(RootError::NotAbsolute));
    }
    let parent = PathBuf::from("/run/../etc");
    assert_eq!(
        StateRoot::new(parent.clone()),
        Err(RootError::ParentComponent)
    );
    assert_eq!(RunRoot::new(parent), Err(RootError::ParentComponent));
}

// The longest socket is `<run>/vm/<36-char id>/vsock.sock_1104`: the run
// root plus 56 bytes. A run root of 51 bytes fits in 107, and 52 does not.
// A real id gives exactly 107, so the check counts the real id length.
#[test]
fn the_run_root_keeps_sockets_in_the_limit() {
    let fits = format!("/{}", "r".repeat(50));
    let run = RunRoot::new(fits.into()).expect("a run root of 51 bytes");
    let longest = run.vm(&id()).host_listener(HostPort::Egress);
    assert_eq!(longest.as_os_str().len(), SOCKET_PATH_MAX);

    let long = format!("/{}", "r".repeat(51));
    assert_eq!(
        RunRoot::new(long.into()),
        Err(RootError::SocketPathTooLong(SOCKET_PATH_MAX + 1))
    );
}

// The slot dirs are shorter than the `vm/<id>` dirs, so the check of a
// root of 51 bytes holds for them too (decisions CH51, CH55).
#[test]
fn slot_sockets_are_shorter() {
    let run = RunRoot::new(format!("/{}", "r".repeat(50)).into()).expect("a run root");
    let slot = run.slot(Slot::LAST).join("vsock.sock_1104");
    let vm = run.vm(&id()).host_listener(HostPort::Egress);
    assert!(slot.as_os_str().len() < vm.as_os_str().len());
}

#[test]
fn slot_paths_are_pinned() {
    let run = RunRoot::new(PathBuf::from("/run/vmm")).expect("a run root");

    assert_eq!(SLOTS, 64);
    assert_eq!(Slot::all().count(), 64);
    assert_eq!(Slot::LAST.number(), 63);
    assert_eq!(Slot::new(63), Some(Slot::LAST));
    assert_eq!(Slot::new(64), None);
    assert_eq!(run.slots_dir(), Path::new("/run/vmm/slot"));
    assert_eq!(run.vms_dir(), Path::new("/run/vmm/vm"));
    let slot = Slot::new(5).expect("slot 5");
    assert_eq!(run.slot(slot), Path::new("/run/vmm/slot/5"));
}

/// A run root in a new directory under `/tmp`, removed on drop. The temp
/// dir of some hosts is too long for the socket limit of `RunRoot`.
struct TempRun(PathBuf, RunRoot);

// `cfg(test)` lets the helper use `expect` (rust/clippy.toml).
#[cfg(test)]
impl TempRun {
    fn new(name: &str) -> Self {
        let path = PathBuf::from(format!("/tmp/vmm-layout-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir(&path).expect("the run root");
        let run = RunRoot::new(path.clone()).expect("a run root");
        Self(path, run)
    }
}

impl Drop for TempRun {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
fn slot(number: u16) -> Slot {
    Slot::new(number).expect("a slot")
}

/// The slot dir that `vm/<id>` resolves to.
#[cfg(test)]
fn resolved(run: &RunRoot) -> PathBuf {
    fs::canonicalize(run.vm(&id()).path()).expect("the link resolves")
}

#[test]
fn the_link_resolves_to_the_slot_dir() {
    let temp = TempRun::new("link");
    let run = &temp.1;

    let vm = run.link_vm(&id(), slot(3)).expect("the link");

    assert_eq!(vm, run.vm(&id()));
    assert_eq!(
        fs::read_link(vm.path()).expect("a symlink"),
        Path::new("../slot/3")
    );
    assert_eq!(
        resolved(run),
        fs::canonicalize(run.slot(slot(3))).expect("the slot")
    );
    fs::write(vm.api_socket(), b"").expect("a file through the link");
    assert!(run.slot(slot(3)).join("api.sock").exists());
}

#[test]
fn a_relink_moves_the_vm_to_another_slot() {
    let temp = TempRun::new("relink");
    let run = &temp.1;
    run.link_vm(&id(), slot(3)).expect("the first link");
    fs::write(run.vm(&id()).serial_log(), b"old").expect("a log in slot 3");

    let vm = run.link_vm(&id(), slot(7)).expect("the re-link");

    assert_eq!(
        resolved(run),
        fs::canonicalize(run.slot(slot(7))).expect("the slot")
    );
    assert!(!vm.serial_log().exists());
    assert!(run.slot(slot(3)).join("serial.log").exists());
    let names: Vec<_> = fs::read_dir(run.vms_dir())
        .expect("the vm dir")
        .map(|entry| entry.expect("an entry").file_name())
        .collect();
    assert_eq!(names, [ID]);
}

#[test]
fn a_stale_link_and_temp_are_replaced() {
    let temp = TempRun::new("stale");
    let run = &temp.1;
    fs::create_dir(run.vms_dir()).expect("the vm dir");
    std::os::unix::fs::symlink("../slot/99", run.vm(&id()).path()).expect("a stale link");
    fs::write(run.vms_dir().join(format!(".{ID}.link")), b"").expect("a stale temp");

    run.link_vm(&id(), slot(0)).expect("the link");

    assert_eq!(
        resolved(run),
        fs::canonicalize(run.slot(slot(0))).expect("the slot")
    );
    assert!(!run.vms_dir().join(format!(".{ID}.link")).exists());
}

#[test]
fn a_real_dir_stays_and_the_link_fails() {
    let temp = TempRun::new("dir");
    let run = &temp.1;
    fs::create_dir_all(run.vm(&id()).path()).expect("a real dir");

    let error = run.link_vm(&id(), slot(1)).expect_err("a dir was replaced");

    assert_eq!(error.path, run.vm(&id()).path());
    assert!(
        fs::symlink_metadata(run.vm(&id()).path())
            .expect("the dir")
            .is_dir()
    );
    assert!(!run.vms_dir().join(format!(".{ID}.link")).exists());
}

#[test]
fn unlink_keeps_the_slot() {
    let temp = TempRun::new("unlink");
    let run = &temp.1;
    let vm = run.link_vm(&id(), slot(2)).expect("the link");
    fs::write(vm.serial_log(), b"").expect("a log");

    run.unlink_vm(&id()).expect("the unlink");
    run.unlink_vm(&id()).expect("a second unlink");

    assert!(fs::symlink_metadata(vm.path()).is_err());
    assert!(run.slot(slot(2)).join("serial.log").exists());
}
