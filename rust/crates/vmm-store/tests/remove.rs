//! A drop removes the snapshots, the session disk, the session dir and
//! then the row of one session. A crash or a failed step before the row
//! leaves the row, and a retried drop finishes (decisions CH27, CH21-10,
//! CH33 rules 4 and 5).

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use creche_vmm_store::layout::{SessionDir, SnapshotGeneration, StateRoot};
use creche_vmm_store::remove::remove_session;
use creche_vmm_store::rows::Rows;
use creche_wire::{CreateSession, DropSession, SessionId};

const A: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const B: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";

/// A state root in a new temp dir, removed on drop.
struct TempState(StateRoot);

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl TempState {
    fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!("vmm-remove-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        Self(StateRoot::new(path).expect("a state root"))
    }

    fn open(&self) -> Rows {
        Rows::open(self.0.clone()).expect("the rows load")
    }

    fn session(&self, id: &str) -> SessionDir {
        self.0.session(&parse(id))
    }

    /// Makes the row of `id` in family `anna`, a session disk, two
    /// generations with keys, a leftover retired dir and a disk temp file.
    fn booted(&self, rows: &mut Rows, id: &str) -> SessionDir {
        let create: CreateSession = serde_json::from_value(serde_json::json!({
            "created_at": 1000,
            "cwd": "/w",
            "family": "anna",
            "id": id,
            "origin": {"door": {}},
        }))
        .expect("a create");
        rows.create(create).expect("a row");
        let session = self.session(id);
        for generation in [1, 2] {
            let dir = session.snapshot(SnapshotGeneration(generation));
            fs::create_dir_all(&dir).expect("the dir");
            fs::write(dir.join("key.json"), "{}").expect("a key");
            fs::write(dir.join("memory-ranges"), "memory").expect("the memory");
        }
        fs::create_dir(session.snapshots().join("3.retired")).expect("a retired dir");
        fs::write(session.disk(), "disk").expect("the disk");
        fs::write(session.disk_temp(), "temp").expect("a temp disk");
        session
    }
}

impl Drop for TempState {
    fn drop(&mut self) {
        let _ = fs::set_permissions(self.0.rows_dir(), fs::Permissions::from_mode(0o700));
        let _ = fs::remove_dir_all(self.0.path());
    }
}

#[cfg(test)]
fn parse(id: &str) -> SessionId {
    id.parse().expect("a session id")
}

#[cfg(test)]
fn target(family: &str, id: &str) -> DropSession {
    serde_json::from_value(serde_json::json!({"family": family, "id": id})).expect("a drop")
}

/// Sets the mode of `path`.
#[cfg(test)]
fn set_mode(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).expect("a mode");
}

#[test]
fn a_drop_removes_every_file_and_then_the_row() {
    let state = TempState::new("all");
    let mut rows = state.open();
    let dropped = state.booted(&mut rows, A);
    let kept = state.booted(&mut rows, B);

    let removed = remove_session(&mut rows, &target("anna", A)).expect("the drop");

    assert_eq!(removed.map(|row| row.id), Some(parse(A)));
    assert!(!dropped.path().exists());
    assert!(!state.0.row_file(&parse(A)).exists());
    assert!(kept.disk().exists());
    assert!(kept.snapshot(SnapshotGeneration(2)).exists());
    for rows in [rows, state.open()] {
        assert_eq!(rows.get(&parse(A)), None);
        assert!(rows.get(&parse(B)).is_some());
    }
}

/// A session that never booted has a row and no session dir.
#[test]
fn a_drop_before_the_first_boot_removes_the_row() {
    let state = TempState::new("unbooted");
    let mut rows = state.open();
    state.booted(&mut rows, A);
    fs::remove_dir_all(state.0.sessions_dir()).expect("no session dirs");

    let removed = remove_session(&mut rows, &target("anna", A)).expect("the drop");

    assert!(removed.is_some());
    assert_eq!(state.open().get(&parse(A)), None);
}

/// Decision CH33 rules 4 and 5: an absent id, or the id of another family,
/// gives `None` and removes nothing.
#[test]
fn a_drop_of_an_unknown_target_removes_nothing() {
    let state = TempState::new("unknown");
    let mut rows = state.open();
    let session = state.booted(&mut rows, A);

    for unknown in [target("otto", A), target("anna", B)] {
        let removed = remove_session(&mut rows, &unknown).expect("no drop");
        assert_eq!(removed, None, "{unknown:?}");
    }
    assert!(session.disk().exists());
    assert!(session.snapshot(SnapshotGeneration(1)).exists());
    assert!(rows.get(&parse(A)).is_some());
}

/// A crash after the disk step leaves the row file and no session dir. The
/// vmm loads the row after a restart, and the retried drop finishes.
#[test]
fn a_retry_after_a_crash_finishes_the_drop() {
    let state = TempState::new("crash");
    let mut rows = state.open();
    let session = state.booted(&mut rows, A);
    fs::remove_dir_all(session.path()).expect("the steps before the row");
    drop(rows);

    let mut rows = state.open();
    assert!(rows.get(&parse(A)).is_some(), "the row stays");
    let removed = remove_session(&mut rows, &target("anna", A)).expect("the retry");

    assert!(removed.is_some());
    assert_eq!(state.open().get(&parse(A)), None);
    let again = remove_session(&mut rows, &target("anna", A)).expect("a second retry");
    assert_eq!(again, None, "the route answers not_found");
}

/// Mode 0500 on the rows dir fails the remove of the row file. The files
/// are gone and the row stays. Root ignores the mode, so the test skips
/// when a probe file opens in the dir.
#[test]
fn a_failed_row_step_leaves_the_row_for_a_retry() {
    let state = TempState::new("row-fails");
    let mut rows = state.open();
    let session = state.booted(&mut rows, A);
    let rows_dir = state.0.rows_dir();
    set_mode(&rows_dir, 0o500);
    if fs::write(rows_dir.join("probe"), "").is_ok() {
        return;
    }

    let error = remove_session(&mut rows, &target("anna", A)).expect_err("a failure");

    assert_eq!(error.path, state.0.row_file(&parse(A)));
    assert!(!session.path().exists());
    assert!(rows.get(&parse(A)).is_some());
    assert!(state.open().get(&parse(A)).is_some());
    set_mode(&rows_dir, 0o700);
    let removed = remove_session(&mut rows, &target("anna", A)).expect("the retry");
    assert!(removed.is_some());
    assert_eq!(state.open().get(&parse(A)), None);
}
