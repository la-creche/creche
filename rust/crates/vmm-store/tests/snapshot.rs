//! A take pauses, snapshots and shuts Cloud Hypervisor down, digs the holes
//! of `memory-ranges` and renames a new generation into place (decisions
//! CH2, CH29, section 4 item 5.7).
//!
//! `tests/fixtures/ch-remote-snapshot` and `tests/fixtures/fallocate` stand
//! in for the programs. `tests/fixtures/fallocate-lock` also locks
//! `snapshots/` against a read. Each logs to `<root>/calls`, so a test reads
//! the call order. The last test runs the host `fallocate`, a Linux tool, so it
//! is Linux only. C16 takes a real snapshot.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

use creche_vmm_store::ch_remote::{ChCommand, ChRemote, ChRemoteError};
use creche_vmm_store::layout::{RunRoot, SessionDir, SnapshotGeneration, StateRoot, VmRunDir};
use creche_vmm_store::snapshot::{
    MEMORY_RANGES, Snapshot, SnapshotError, SnapshotTimeouts, Snapshotter, generations,
};
use creche_wire::SessionId;

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const TIMEOUT: Duration = Duration::from_secs(10);
const TIMEOUTS: SnapshotTimeouts = SnapshotTimeouts {
    pause: TIMEOUT,
    snapshot: TIMEOUT,
    shutdown: TIMEOUT,
};
const FIRST: SnapshotGeneration = SnapshotGeneration(1);

/// One dir under `/tmp` that is both the run root and the state root, so
/// both stubs find `<root>/calls`. Removed on drop.
struct Temp {
    root: PathBuf,
    vm: VmRunDir,
    session: SessionDir,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-snap-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let id: SessionId = ID.parse().expect("ID is a session id");
        let vm = RunRoot::new(root.clone()).expect("a run root").vm(&id);
        fs::create_dir_all(vm.path()).expect("the run dir");
        let session = StateRoot::new(root.clone())
            .expect("a state root")
            .session(&id);
        Self { root, vm, session }
    }

    fn take(&self, fallocate: &Path) -> Result<Snapshot, SnapshotError> {
        let ch = ChRemote::new(fixture("ch-remote-snapshot"));
        Snapshotter::new(ch, fallocate.to_owned()).take(&self.vm, &self.session, TIMEOUTS)
    }

    fn calls(&self) -> Vec<String> {
        let text = fs::read_to_string(self.root.join("calls")).expect("the calls");
        text.lines().map(str::to_owned).collect()
    }

    /// The names in the dir of `generation`, sorted.
    fn names(&self, generation: SnapshotGeneration) -> Vec<String> {
        let dir = fs::read_dir(self.session.snapshot(generation)).expect("the dir");
        let mut names: Vec<String> = dir
            .map(|entry| {
                entry
                    .expect("an entry")
                    .file_name()
                    .into_string()
                    .expect("UTF-8")
            })
            .collect();
        names.sort();
        names
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name)
}

#[cfg(test)]
fn mode(path: &Path) -> u32 {
    fs::symlink_metadata(path).expect("the entry").mode() & 0o7777
}

#[test]
fn a_take_runs_its_steps_in_order() {
    let temp = Temp::new("order");
    let snapshot = temp.take(&fixture("fallocate")).expect("the snapshot");
    let tmp = temp.session.snapshot_temp(FIRST);
    let tmp = tmp.to_str().expect("UTF-8");
    let memory = format!("{tmp}/{MEMORY_RANGES}");
    let dig = format!("fallocate --dig-holes -- {memory}");
    let snap = format!("snapshot file://{tmp}");
    assert_eq!(temp.calls(), ["pause", &snap, "shutdown-vmm", &dig]);
    assert_eq!(snapshot.generation, FIRST);
    assert_eq!(snapshot.dir, temp.session.snapshot(FIRST));
    assert!(!Path::new(tmp).exists());
    assert_eq!(generations(&temp.session).expect("a read"), [FIRST]);
}

#[test]
fn the_dir_is_0700_and_each_file_0600() {
    let temp = Temp::new("modes");
    let snapshot = temp.take(&fixture("fallocate")).expect("the snapshot");
    assert_eq!(mode(&temp.session.snapshots()), 0o700);
    assert_eq!(mode(&snapshot.dir), 0o700);
    assert_eq!(
        temp.names(FIRST),
        ["config.json", MEMORY_RANGES, "state.json"]
    );
    for name in temp.names(FIRST) {
        assert_eq!(mode(&snapshot.dir.join(&name)), 0o600, "{name}");
    }
}

#[test]
fn a_second_take_leaves_the_first_unchanged() {
    let temp = Temp::new("second");
    let first = temp.take(&fixture("fallocate")).expect("the first");
    let memory = first.dir.join(MEMORY_RANGES);
    let before = fs::metadata(&memory).expect("the file");
    let second = temp.take(&fixture("fallocate")).expect("the second");
    let after = fs::metadata(&memory).expect("the file");
    assert_eq!(second.generation, SnapshotGeneration(2));
    assert_eq!((after.ino(), after.mtime()), (before.ino(), before.mtime()));
    assert_eq!(
        (after.mtime_nsec(), after.ctime()),
        (before.mtime_nsec(), before.ctime())
    );
    let found = generations(&temp.session).expect("a read");
    assert_eq!(found, [FIRST, SnapshotGeneration(2)]);
}

#[test]
fn a_failed_snapshot_leaves_no_generation() {
    let temp = Temp::new("failed");
    fs::write(temp.root.join("fail-snapshot"), "").expect("the fail file");
    let error = temp.take(&fixture("fallocate")).expect_err("a failure");
    let SnapshotError::Ch(ChRemoteError::Failed { command, .. }) = error else {
        panic!("not Failed: {error}");
    };
    assert_eq!(command, ChCommand::Snapshot);
    assert_eq!(temp.calls().len(), 2, "no shutdown, no dig");
    let left = fs::read_dir(temp.session.snapshots()).expect("the dir");
    assert_eq!(left.count(), 0);
}

/// A failed pause or a failed `shutdown-vmm` ends the take. Then no dig
/// runs and no generation exists, so no generation names a snapshot of a
/// Cloud Hypervisor that may still run.
#[test]
fn a_failed_pause_or_shutdown_leaves_no_generation() {
    for (command, calls) in [(ChCommand::Pause, 1), (ChCommand::ShutdownVmm, 3)] {
        let temp = Temp::new(command.name());
        let fail = format!("fail-{}", command.name());
        fs::write(temp.root.join(fail), "").expect("the fail file");
        let error = temp.take(&fixture("fallocate")).expect_err("a failure");
        let SnapshotError::Ch(ChRemoteError::Failed {
            command: failed, ..
        }) = error
        else {
            panic!("not Failed: {error}");
        };
        assert_eq!(failed, command);
        let log = temp.calls();
        assert_eq!(log.len(), calls, "{log:?}");
        assert!(!log.iter().any(|call| call.starts_with("fallocate")));
        let left = fs::read_dir(temp.session.snapshots()).expect("the dir");
        assert_eq!(left.count(), 0);
    }
}

/// A symlink from Cloud Hypervisor ends the take before any mode set
/// follows it, so its target keeps its mode.
#[test]
fn a_planted_link_is_refused() {
    let temp = Temp::new("link");
    let target = temp.root.join("target");
    fs::write(&target, "").expect("the target");
    fs::set_permissions(&target, fs::Permissions::from_mode(0o644)).expect("a mode");
    fs::write(temp.root.join("plant-link"), "").expect("the plant file");
    let error = temp.take(&fixture("fallocate")).expect_err("a refusal");
    let SnapshotError::NotAFile(path) = error else {
        panic!("not NotAFile: {error}");
    };
    assert_eq!(
        path.file_name().and_then(|name| name.to_str()),
        Some("link")
    );
    assert_eq!(mode(&target), 0o644);
    let left = fs::read_dir(temp.session.snapshots()).expect("the dir");
    assert_eq!(left.count(), 0);
}

#[test]
fn a_failed_dig_leaves_no_generation() {
    let temp = Temp::new("no-dig");
    let error = temp.take(Path::new("false")).expect_err("a failure");
    assert!(matches!(error, SnapshotError::Dig { .. }), "{error}");
    assert_eq!(
        temp.calls().last().map(String::as_str),
        Some("shutdown-vmm")
    );
    let left = fs::read_dir(temp.session.snapshots()).expect("the dir");
    assert_eq!(left.count(), 0);
}

/// Mode 0300 keeps the rename into `snapshots/` but fails the open for
/// its sync. Then `take` removes the renamed dir (decision CH44 rule 4).
/// Root ignores the mode, so the test skips when a probe dir opens.
#[test]
fn a_failed_sync_after_the_rename_leaves_no_generation() {
    let temp = Temp::new("no-sync");
    let probe = temp.root.join("probe");
    fs::create_dir(&probe).expect("the probe");
    fs::set_permissions(&probe, fs::Permissions::from_mode(0o300)).expect("a mode");
    if fs::File::open(&probe).is_ok() {
        return;
    }
    let taken = temp.take(&fixture("fallocate-lock"));
    let snapshots = temp.session.snapshots();
    fs::set_permissions(&snapshots, fs::Permissions::from_mode(0o700)).expect("a mode");
    let error = taken.expect_err("a failure");
    let SnapshotError::Io { path, .. } = error else {
        panic!("not Io: {error}");
    };
    assert_eq!(path, snapshots);
    let left = fs::read_dir(&snapshots).expect("the dir");
    assert_eq!(left.count(), 0);
}

#[test]
fn a_leftover_temp_dir_is_replaced() {
    let temp = Temp::new("leftover");
    let leftover = temp.session.snapshot_temp(FIRST);
    fs::create_dir_all(&leftover).expect("the leftover");
    fs::write(leftover.join("stale"), "").expect("a stale file");
    temp.take(&fixture("fallocate")).expect("the snapshot");
    assert!(!temp.names(FIRST).contains(&"stale".to_owned()));
}

#[test]
fn only_plain_numbered_dirs_are_generations() {
    let temp = Temp::new("names");
    let snapshots = temp.session.snapshots();
    for dir in ["2", "007", "x", "5.tmp"] {
        fs::create_dir_all(snapshots.join(dir)).expect("a dir");
    }
    fs::write(snapshots.join("4"), "").expect("a file");
    assert_eq!(
        generations(&temp.session).expect("a read"),
        [SnapshotGeneration(2)]
    );
    let snapshot = temp.take(&fixture("fallocate")).expect("the snapshot");
    assert_eq!(snapshot.generation, SnapshotGeneration(3));
}

/// `true` digs nothing, so the first take keeps every zero page. The host
/// `fallocate` then frees them (decision CH29).
#[cfg(target_os = "linux")]
#[test]
fn the_dig_frees_the_zero_pages() {
    const MIB: u64 = 1 << 20;
    let temp = Temp::new("dig");
    let undug = temp.take(Path::new("true")).expect("the first");
    assert!(
        undug.allocated_bytes >= 16 * MIB,
        "{}",
        undug.allocated_bytes
    );
    let dug = temp.take(Path::new("fallocate")).expect("the second");
    assert!(dug.allocated_bytes < MIB, "{}", dug.allocated_bytes);
    let memory = fs::metadata(dug.dir.join(MEMORY_RANGES)).expect("the file");
    assert_eq!(memory.len(), 16 * MIB);
}
