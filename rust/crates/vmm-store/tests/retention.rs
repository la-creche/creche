//! Retention drops snapshots by age and by a budget of allocated bytes,
//! skips the ones in use and keeps the session disks (decisions CH2,
//! CH8-7, CH36 G13, CH44 rule 6, CH50 rule 8).
//!
//! A fixed `NOW` is the clock. Each test sets the time of each
//! `memory-ranges` relative to it.

use std::fs::{self, File};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use creche_vmm_store::claim::CONSUMED;
use creche_vmm_store::layout::{SessionDir, SnapshotGeneration, StateRoot};
use creche_vmm_store::retention::{BUDGET, InUse, MAX_AGE, Reason, Swept, sweep};
use creche_vmm_store::snapshot::{MEMORY_RANGES, generations};
use creche_wire::SessionId;

const A: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const B: &str = "1f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const C: &str = "2f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const DAY: Duration = Duration::from_secs(24 * 60 * 60);
const FIRST: SnapshotGeneration = SnapshotGeneration(1);
const SECOND: SnapshotGeneration = SnapshotGeneration(2);
/// The bytes that each test snapshot writes.
const WRITTEN: usize = 64 * 1024;

/// The fake clock.
fn now() -> SystemTime {
    SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000)
}

/// A state root in a new temp dir, removed on drop.
struct Temp {
    root: PathBuf,
    state: StateRoot,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-keep-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let state = StateRoot::new(root.join("state")).expect("a state root");
        Self { root, state }
    }

    fn session(&self, id: &str) -> SessionDir {
        self.state.session(&parse(id))
    }

    /// Makes a generation of `id` whose `memory-ranges` holds `WRITTEN`
    /// bytes, written `age` before `NOW`, and a session disk.
    fn snapshot(&self, id: &str, generation: SnapshotGeneration, age: Duration) -> PathBuf {
        let session = self.session(id);
        let dir = session.snapshot(generation);
        fs::create_dir_all(&dir).expect("the dir");
        fs::write(session.disk(), "disk").expect("the disk");
        fs::write(dir.join("key.json"), "{}").expect("a key");
        let path = dir.join(MEMORY_RANGES);
        fs::write(&path, vec![1_u8; WRITTEN]).expect("the file");
        File::options()
            .write(true)
            .open(&path)
            .and_then(|file| file.set_modified(now() - age))
            .expect("a time");
        dir
    }

    fn sweep(&self, budget: u64, in_use: &InUse) -> Swept {
        sweep(&self.state, now(), budget, in_use).expect("the sweep")
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[cfg(test)]
fn parse(id: &str) -> SessionId {
    id.parse().expect("a session id")
}

/// The allocated bytes of the entries of `dir`, as `st_blocks` counts.
#[cfg(test)]
fn allocated(dir: &Path) -> u64 {
    fs::read_dir(dir)
        .expect("the dir")
        .map(|entry| {
            entry
                .expect("an entry")
                .metadata()
                .expect("a stat")
                .blocks()
                * 512
        })
        .sum()
}

/// Sets the mode of `dir`.
#[cfg(test)]
fn set_mode(dir: &Path, mode: u32) {
    fs::set_permissions(dir, fs::Permissions::from_mode(mode)).expect("the mode");
}

/// The removed generations as (session, generation, reason).
fn dropped(swept: &Swept) -> Vec<(String, u64, Reason)> {
    swept
        .dropped
        .iter()
        .map(|each| (each.session.to_string(), each.generation.0, each.reason))
        .collect()
}

#[test]
fn the_design_values() {
    assert_eq!(MAX_AGE, Duration::from_secs(7 * 24 * 60 * 60));
    assert_eq!(BUDGET, 150 << 30);
}

#[test]
fn no_sessions_dir_gives_an_empty_sweep() {
    let temp = Temp::new("empty");
    let swept = temp.sweep(0, &InUse::new());
    assert!(swept.dropped.is_empty() && swept.failures.is_empty());
    assert_eq!(swept.kept_bytes, 0);
}

#[test]
fn a_snapshot_past_seven_days_drops_and_its_disk_stays() {
    let temp = Temp::new("age");
    temp.snapshot(A, FIRST, MAX_AGE + Duration::from_secs(1));
    let kept = temp.snapshot(B, FIRST, MAX_AGE - Duration::from_secs(1));
    let swept = temp.sweep(BUDGET, &InUse::new());
    assert_eq!(dropped(&swept), [(A.to_owned(), 1, Reason::Aged)]);
    assert!(swept.failures.is_empty(), "{:?}", swept.failures);
    assert!(temp.session(A).disk().exists(), "the disk stays");
    assert!(generations(&temp.session(A)).expect("a list").is_empty());
    assert_eq!(swept.kept_bytes, allocated(&kept));
}

#[test]
fn an_older_generation_drops_before_the_newest() {
    let temp = Temp::new("older");
    temp.snapshot(A, FIRST, DAY);
    temp.snapshot(A, SECOND, Duration::ZERO);
    let swept = temp.sweep(BUDGET, &InUse::new());
    assert_eq!(dropped(&swept), [(A.to_owned(), 1, Reason::Superseded)]);
    assert_eq!(generations(&temp.session(A)).expect("a list"), [SECOND]);
}

#[test]
fn the_budget_counts_allocated_bytes_not_the_apparent_size() {
    let temp = Temp::new("sparse");
    let dir = temp.snapshot(A, FIRST, DAY);
    // 1 GiB of apparent size with no block allocated, like a dug file.
    File::create(dir.join("sparse"))
        .and_then(|file| file.set_len(1 << 30))
        .expect("a sparse file");
    let bytes = allocated(&dir);
    assert!(bytes < 1 << 20, "{bytes} bytes are allocated");
    let swept = temp.sweep(bytes, &InUse::new());
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
    assert_eq!(swept.kept_bytes, bytes);
}

#[test]
fn the_budget_drops_the_oldest_first() {
    let temp = Temp::new("budget");
    let dirs = [
        temp.snapshot(B, FIRST, 2 * DAY),
        temp.snapshot(A, FIRST, DAY),
        temp.snapshot(C, FIRST, 3 * DAY),
    ];
    let sizes = dirs.map(|dir| allocated(&dir));
    let total: u64 = sizes.iter().sum();

    // One byte past the budget drops only the oldest, C.
    let swept = temp.sweep(total - 1, &InUse::new());
    assert_eq!(dropped(&swept), [(C.to_owned(), 1, Reason::Budget)]);
    assert_eq!(swept.kept_bytes, sizes[0] + sizes[1]);

    // A budget of 0 drops the rest, B before A.
    let swept = temp.sweep(0, &InUse::new());
    assert_eq!(
        dropped(&swept),
        [
            (B.to_owned(), 1, Reason::Budget),
            (A.to_owned(), 1, Reason::Budget),
        ]
    );
    assert_eq!(swept.kept_bytes, 0);
}

#[test]
fn a_snapshot_in_use_stays() {
    let temp = Temp::new("in-use");
    let used = temp.snapshot(A, FIRST, 2 * MAX_AGE);
    temp.snapshot(B, FIRST, DAY);
    let in_use = InUse::from([(parse(A), FIRST)]);
    let swept = temp.sweep(0, &in_use);
    assert_eq!(dropped(&swept), [(B.to_owned(), 1, Reason::Budget)]);
    assert!(used.exists());
    assert_eq!(swept.kept_bytes, allocated(&used));
}

/// A consumed newest generation with no VM on it never restores again, so
/// it drops at once (decisions CH19 (5), CH50 rule 8).
#[test]
fn a_consumed_unused_newest_generation_drops() {
    let temp = Temp::new("consumed");
    let dir = temp.snapshot(A, FIRST, DAY);
    fs::write(dir.join(CONSUMED), "").expect("the mark");
    let kept = temp.snapshot(B, FIRST, DAY);
    let swept = temp.sweep(BUDGET, &InUse::new());
    assert_eq!(dropped(&swept), [(A.to_owned(), 1, Reason::Consumed)]);
    assert!(swept.failures.is_empty(), "{:?}", swept.failures);
    assert!(temp.session(A).disk().exists(), "the disk stays");
    assert!(generations(&temp.session(A)).expect("a list").is_empty());
    assert_eq!(swept.kept_bytes, allocated(&kept));
}

/// A consumed generation in use stays: a VM restored from it maps its
/// files (fact 55, decision CH50 rule 8).
#[test]
fn a_consumed_generation_in_use_stays() {
    let temp = Temp::new("consumed-in-use");
    let dir = temp.snapshot(A, FIRST, DAY);
    fs::write(dir.join(CONSUMED), "").expect("the mark");
    let in_use = InUse::from([(parse(A), FIRST)]);
    let swept = temp.sweep(BUDGET, &in_use);
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
    assert!(dir.join(MEMORY_RANGES).exists());
    assert_eq!(swept.kept_bytes, allocated(&dir));
}

/// An older generation in use keeps every newer one, so the older one
/// never becomes the newest (decision CH19 (5)).
#[test]
fn an_older_generation_in_use_keeps_the_newer_ones() {
    let temp = Temp::new("older-in-use");
    temp.snapshot(A, FIRST, 2 * MAX_AGE);
    temp.snapshot(A, SECOND, 2 * MAX_AGE);
    let in_use = InUse::from([(parse(A), FIRST)]);
    let swept = temp.sweep(0, &in_use);
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
    assert_eq!(
        generations(&temp.session(A)).expect("a list"),
        [FIRST, SECOND]
    );
}

#[test]
fn a_leftover_retired_dir_is_removed_and_a_temp_dir_stays() {
    let temp = Temp::new("retired");
    temp.snapshot(A, FIRST, Duration::ZERO);
    let session = temp.session(A);
    let retired = session.snapshots().join("2.retired");
    let taking = session.snapshot_temp(SnapshotGeneration(3));
    for dir in [&retired, &taking] {
        fs::create_dir_all(dir).expect("a dir");
        fs::write(dir.join(MEMORY_RANGES), "ram").expect("a file");
    }
    let swept = temp.sweep(BUDGET, &InUse::new());
    assert_eq!(swept.retired, std::slice::from_ref(&retired));
    assert!(!retired.exists());
    assert!(taking.exists(), "a take in progress keeps its temp dir");
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
}

/// A failed read of an older generation keeps the newer one, even past
/// `MAX_AGE`. So the older one never becomes the newest (decision CH19
/// (5)).
#[test]
fn an_unreadable_older_generation_keeps_the_newer_one() {
    let temp = Temp::new("unreadable");
    let older = temp.snapshot(A, FIRST, 2 * MAX_AGE);
    let newer = temp.snapshot(A, SECOND, 2 * MAX_AGE);
    set_mode(&older, 0o000);
    let swept = temp.sweep(0, &InUse::new());
    set_mode(&older, 0o755);
    assert_eq!(swept.failures.len(), 1, "{:?}", swept.failures);
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
    assert!(newer.exists(), "the newer generation stays");
}

/// A failed read of an older generation keeps a consumed newer one too.
/// So the older one never becomes the newest (decisions CH19 (5), CH50
/// rule 8).
#[test]
fn an_unreadable_older_generation_keeps_a_consumed_newer_one() {
    let temp = Temp::new("unreadable-consumed");
    let older = temp.snapshot(A, FIRST, DAY);
    let newer = temp.snapshot(A, SECOND, DAY);
    fs::write(newer.join(CONSUMED), "").expect("the mark");
    set_mode(&older, 0o000);
    let swept = temp.sweep(BUDGET, &InUse::new());
    set_mode(&older, 0o755);
    assert_eq!(swept.failures.len(), 1, "{:?}", swept.failures);
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
    assert!(newer.exists(), "the newer generation stays");
}

/// A failed budget drop keeps its bytes in `kept_bytes`.
#[test]
fn a_failed_budget_drop_counts_its_bytes() {
    let temp = Temp::new("budget-fails");
    let dir = temp.snapshot(A, FIRST, DAY);
    // The key and the files cannot leave a dir with no write bit.
    set_mode(&dir, 0o500);
    let swept = temp.sweep(0, &InUse::new());
    set_mode(&dir, 0o755);
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
    assert_eq!(swept.failures.len(), 1, "{:?}", swept.failures);
    assert_eq!(swept.kept_bytes, allocated(&dir));
}

/// A failed drop of a superseded generation is not tried again by the
/// budget step.
#[test]
fn a_failed_superseded_drop_is_tried_once() {
    let temp = Temp::new("superseded-fails");
    let older = temp.snapshot(A, FIRST, DAY);
    let newer = temp.snapshot(A, SECOND, Duration::ZERO);
    set_mode(&older, 0o500);
    let swept = temp.sweep(0, &InUse::new());
    set_mode(&older, 0o755);
    assert!(swept.dropped.is_empty(), "{:?}", swept.dropped);
    assert_eq!(swept.failures.len(), 1, "{:?}", swept.failures);
    assert!(older.exists() && newer.exists());
}
