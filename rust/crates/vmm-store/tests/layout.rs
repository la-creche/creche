//! Pins every path of the per-VM layout (gap G5, decisions CH20, CH24).

use std::path::{Path, PathBuf};

use creche_vmm_store::layout::{
    RootError, RunRoot, SCOPE_PREFIX, SOCKET_PATH_MAX, SnapshotGeneration, StateRoot, scope_unit,
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
