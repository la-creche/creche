//! A snapshot restores once (decision CH19 (5)). The first claim of a keyed
//! snapshot gives a `ClaimedSnapshot`, and every later claim gives `None`.
//!
//! A `KeyedSnapshot` comes only from the key check (PR D10), so each test
//! imports two images, runs the `cloud-hypervisor` stub of
//! `tests/fixtures` and checks the key first.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::time::Duration;

use creche_vmm_store::claim::{CONSUMED, ClaimError, claim};
use creche_vmm_store::images::{CheckedImage, ImageDigest, ImageStore};
use creche_vmm_store::key::{
    KeyCheck, KeyedSnapshot, SnapshotKey, check_cloud_hypervisor, check_newest, write_key,
};
use creche_vmm_store::layout::{SessionDir, SnapshotGeneration, StateRoot};
use creche_wire::SessionId;
use sha2::{Digest, Sha256};

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const TIMEOUT: Duration = Duration::from_secs(10);
const FIRST: SnapshotGeneration = SnapshotGeneration(1);
const SECOND: SnapshotGeneration = SnapshotGeneration(2);

/// A state root with a kernel, a root disk and one session, in a new temp
/// dir, removed on drop.
struct Temp {
    root: PathBuf,
    session: SessionDir,
    key: SnapshotKey,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-claim-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("the temp dir");
        let state = StateRoot::new(root.join("state")).expect("a state root");
        let store = ImageStore::new(&state, PathBuf::from("zstd"));
        let images: [CheckedImage; 3] = ["kernel", "initramfs", "disk"].map(|text| {
            let source = root.join(text);
            fs::write(&source, text).expect("a source file");
            let digest = digest_of(text.as_bytes());
            store.import(&source, digest).expect("the import");
            store.check(digest).expect("the check")
        });
        let link = root.join("v54.0.0");
        symlink(fixture("cloud-hypervisor"), &link).expect("the link");
        let ch = check_cloud_hypervisor(&link, TIMEOUT).expect("the version check");
        let id: SessionId = ID.parse().expect("ID is a session id");
        Self {
            session: state.session(&id),
            key: SnapshotKey::current(ch, &images[0], &images[1], &images[2]),
            root,
        }
    }

    /// Makes a generation dir with one file and the current key.
    fn snapshot(&self, generation: SnapshotGeneration) {
        let dir = self.session.snapshot(generation);
        fs::create_dir_all(&dir).expect("the dir");
        fs::write(dir.join("memory-ranges"), "ram").expect("a file");
        write_key(&dir, &self.key).expect("the key");
    }

    /// The newest snapshot after a key check, as at each VM start.
    fn keyed(&self) -> KeyedSnapshot {
        match check_newest(&self.session, &self.key).expect("the check") {
            KeyCheck::Keyed(keyed) => keyed,
            other => panic!("not Keyed: {other:?}"),
        }
    }

    /// The consumed mark of `generation`.
    fn mark(&self, generation: SnapshotGeneration) -> PathBuf {
        self.session.snapshot(generation).join(CONSUMED)
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
fn digest_of(bytes: &[u8]) -> ImageDigest {
    let hex: String = Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    hex.parse().expect("a digest")
}

#[test]
fn the_first_claim_marks_the_snapshot() {
    let temp = Temp::new("first");
    temp.snapshot(FIRST);
    let claimed = claim(temp.keyed())
        .expect("the claim")
        .expect("a claimed snapshot");
    assert_eq!(claimed.generation(), FIRST);
    assert_eq!(claimed.dir(), temp.session.snapshot(FIRST));
    let mark = fs::symlink_metadata(temp.mark(FIRST)).expect("the mark");
    assert!(mark.is_file());
    assert_eq!(mark.mode() & 0o777, 0o600);
    assert!(
        claimed.dir().join("memory-ranges").exists(),
        "a claim removes nothing"
    );
}

#[test]
fn a_second_claim_gives_none() {
    let temp = Temp::new("second");
    temp.snapshot(FIRST);
    let keyed = temp.keyed();
    claim(keyed.clone())
        .expect("the claim")
        .expect("a claimed snapshot");
    assert_eq!(claim(keyed).expect("the claim"), None);
}

#[test]
fn the_mark_survives_a_new_key_check() {
    let temp = Temp::new("reload");
    temp.snapshot(FIRST);
    claim(temp.keyed())
        .expect("the claim")
        .expect("a claimed snapshot");
    // A vmm restart reads the state root again. The key still matches, so
    // the check keeps the snapshot, and the mark refuses the claim.
    let keyed = temp.keyed();
    assert_eq!(keyed.generation(), FIRST);
    assert_eq!(claim(keyed).expect("the claim"), None);
}

#[test]
fn a_crash_before_the_restore_leaves_no_restorable_snapshot() {
    let temp = Temp::new("crash");
    temp.snapshot(FIRST);
    let claimed = claim(temp.keyed())
        .expect("the claim")
        .expect("a claimed snapshot");
    // The vmm ends before Cloud Hypervisor starts.
    drop(claimed);
    assert_eq!(claim(temp.keyed()).expect("the claim"), None);
}

#[test]
fn a_mark_of_any_kind_refuses_the_claim() {
    let temp = Temp::new("kinds");
    temp.snapshot(FIRST);
    let mark = temp.mark(FIRST);
    let cases: [(&str, &dyn Fn()); 3] = [
        ("a file", &|| fs::write(&mark, "x").expect("a write")),
        ("a dir", &|| fs::create_dir(&mark).expect("a dir")),
        ("a dangling link", &|| {
            symlink(temp.root.join("absent"), &mark).expect("a link");
        }),
    ];
    for (name, make) in cases {
        make();
        assert_eq!(claim(temp.keyed()).expect(name), None, "{name}");
        let _ = fs::remove_file(&mark);
        let _ = fs::remove_dir(&mark);
    }
}

#[test]
fn a_fresh_snapshot_after_a_claim_restores_once() {
    let temp = Temp::new("fresh");
    temp.snapshot(FIRST);
    claim(temp.keyed())
        .expect("the claim")
        .expect("a claimed snapshot");
    temp.snapshot(SECOND);
    let claimed = claim(temp.keyed())
        .expect("the claim")
        .expect("a claimed snapshot");
    assert_eq!(claimed.generation(), SECOND);
    assert_eq!(claim(temp.keyed()).expect("the claim"), None);
}

/// Sets the mode of `path`.
#[cfg(test)]
fn set_mode(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).expect("a mode");
}

/// Whether a make in `dir` fails. A root user makes files in any dir, so a
/// test that needs a failed make skips then.
#[cfg(test)]
fn make_fails(dir: &Path) -> bool {
    let probe = dir.join("probe");
    let made = fs::write(&probe, "").is_ok();
    let _ = fs::remove_file(&probe);
    !made
}

/// A failed make of the mark retires the generation, so no later check
/// gives it as `Keyed` (decision CH19 (5)).
#[test]
fn a_failed_mark_retires_the_generation() {
    let temp = Temp::new("retire");
    temp.snapshot(FIRST);
    let keyed = temp.keyed();
    let dir = temp.session.snapshot(FIRST);
    set_mode(&dir, 0o500);
    if !make_fails(&dir) {
        return;
    }
    let error = claim(keyed).expect_err("a failure");
    assert!(matches!(error, ClaimError::Retired { .. }), "{error}");
    let retired = dir.with_extension("retired");
    if retired.exists() {
        set_mode(&retired, 0o700);
    }
    let check = check_newest(&temp.session, &temp.key).expect("the check");
    assert_eq!(check, KeyCheck::NoSnapshot);
}

/// A failed retire leaves a snapshot that may restore, so the claim
/// reports `Stuck` and the caller fails the start.
#[test]
fn a_failed_retire_is_stuck() {
    let temp = Temp::new("stuck");
    temp.snapshot(FIRST);
    let keyed = temp.keyed();
    let dir = temp.session.snapshot(FIRST);
    let snapshots = temp.session.snapshots();
    set_mode(&dir, 0o500);
    set_mode(&snapshots, 0o500);
    let denied = make_fails(&dir) && make_fails(&snapshots);
    let claimed = denied.then(|| claim(keyed));
    set_mode(&snapshots, 0o700);
    set_mode(&dir, 0o700);
    let Some(claimed) = claimed else {
        return;
    };
    let error = claimed.expect_err("a failure");
    assert!(matches!(error, ClaimError::Stuck { .. }), "{error}");
    assert_eq!(temp.keyed().generation(), FIRST);
}
