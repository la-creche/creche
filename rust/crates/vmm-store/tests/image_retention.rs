//! Image retention keeps every current or snapshotted image and removes
//! only complete, checked store pairs that no reference names, plus the
//! leftovers of the store: an orphan identity that no reference names and
//! each temp file. `plan` lists the same and removes nothing.

use std::collections::HashSet;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::time::Duration;

use creche_vmm_store::image_retention::{Collected, collect, plan};
use creche_vmm_store::images::{CheckedImage, ImageDigest, ImageStore};
use creche_vmm_store::key::{SnapshotKey, check_cloud_hypervisor, write_key};
use creche_vmm_store::layout::{SnapshotGeneration, StateRoot};
use creche_wire::{FamilyShape, SessionId};
use sha2::{Digest, Sha256};

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const SHAPE: &str = r#"{"cpus":1,"flavor":"base","memory_mib":256}"#;

/// A state root and image store in a new temp dir, removed on drop.
struct Temp {
    dir: PathBuf,
    state: StateRoot,
    store: ImageStore,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("vmm-image-retention-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("the temp dir");
        let state = StateRoot::new(dir.join("state")).expect("a state root");
        let store = ImageStore::new(&state, PathBuf::from("zstd"));
        Self { dir, state, store }
    }

    fn image(&self, name: &str) -> CheckedImage {
        let bytes = format!("image {name}");
        let source = self.dir.join(name);
        fs::write(&source, &bytes).expect("the source");
        let digest = digest_of(bytes.as_bytes());
        self.store.import(&source, digest).expect("the import");
        self.store.check(digest).expect("the check")
    }

    fn snapshot(&self, images: [&CheckedImage; 3]) {
        let link = self.dir.join("v54.0.0");
        symlink(fixture("cloud-hypervisor"), &link).expect("the program link");
        let ch = check_cloud_hypervisor(&link, Duration::from_secs(10)).expect("the version");
        let shape: FamilyShape = serde_json::from_str(SHAPE).expect("the shape");
        let key = SnapshotKey::current(ch, images[0], images[1], images[2], shape);
        let id: SessionId = ID.parse().expect("the session id");
        let dir = self.state.session(&id).snapshot(SnapshotGeneration(1));
        fs::create_dir_all(&dir).expect("the snapshot dir");
        write_key(&dir, &key).expect("the key");
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
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
fn current_and_snapshot_images_stay() {
    let temp = Temp::new("kept");
    let images = [
        temp.image("current-kernel"),
        temp.image("current-root"),
        temp.image("current-initramfs"),
        temp.image("snapshot-kernel"),
        temp.image("snapshot-initramfs"),
        temp.image("snapshot-root"),
        temp.image("unused"),
    ];
    temp.snapshot([&images[3], &images[4], &images[5]]);
    let keep = images[..3]
        .iter()
        .map(CheckedImage::digest)
        .collect::<HashSet<_>>();

    let collected = collect(&temp.store, &temp.state, &keep).expect("the collection");

    assert_eq!(
        collected
            .removed
            .iter()
            .map(|removed| removed.digest)
            .collect::<Vec<_>>(),
        [images[6].digest()]
    );
    assert!(collected.reclaimed_bytes > 0);
    assert!(collected.failures.is_empty());
    for image in &images[..6] {
        assert!(image.path().exists(), "{}", image.digest());
    }
    assert!(!images[6].path().exists());
    assert!(!temp.store.identity_path(&images[6].digest()).exists());
}

#[test]
fn a_bad_snapshot_key_cancels_before_any_remove() {
    let temp = Temp::new("bad-key");
    let unused = temp.image("unused");
    let id: SessionId = ID.parse().expect("the session id");
    let dir = temp.state.session(&id).snapshot(SnapshotGeneration(1));
    fs::create_dir_all(&dir).expect("the snapshot dir");
    fs::write(dir.join("key.json"), "not JSON").expect("the bad key");

    collect(&temp.store, &temp.state, &HashSet::new()).expect_err("a refused scan");

    assert!(unused.path().exists());
    assert!(temp.store.identity_path(&unused.digest()).exists());
}

#[test]
fn an_unexpected_snapshot_entry_cancels_before_any_remove() {
    let temp = Temp::new("unexpected-snapshot");
    let unused = temp.image("unused");
    let id: SessionId = ID.parse().expect("the session id");
    let snapshots = temp.state.session(&id).snapshots();
    fs::create_dir_all(&snapshots).expect("the snapshots dir");
    fs::write(snapshots.join("surprise"), "unknown").expect("the unknown entry");

    collect(&temp.store, &temp.state, &HashSet::new()).expect_err("a refused scan");

    assert!(unused.path().exists());
}

#[test]
fn a_bad_store_pair_stays_and_reports_a_failure() {
    let temp = Temp::new("bad-pair");
    let image = temp.image("bad");
    fs::set_permissions(image.path(), fs::Permissions::from_mode(0o644)).expect("the bad mode");

    let collected = collect(&temp.store, &temp.state, &HashSet::new()).expect("the collection");

    assert!(collected.removed.is_empty());
    assert_eq!(collected.failures.len(), 1);
    assert!(image.path().exists());
    assert!(temp.store.identity_path(&image.digest()).exists());
}

/// A name that is no image, identity or temp file of the store stays, and
/// so does an orphan identity of a marked digest.
#[test]
fn unknown_store_names_and_marked_orphan_identities_stay() {
    let temp = Temp::new("unknown-store");
    let store = temp.state.store_dir();
    fs::create_dir_all(&store).expect("the store dir");
    let marked = digest_of(b"marked");
    let kept = [
        "notes".to_owned(),
        format!("{marked}.identity"),
        format!("{marked}.part"),
        ".notes.tmp".to_owned(),
        format!("{marked}x.tmp"),
    ];
    for name in &kept {
        fs::write(store.join(name), "keep").expect("the file");
    }

    let current = HashSet::from([marked]);
    let collected = collect(&temp.store, &temp.state, &current).expect("the collection");

    assert!(collected.removed.is_empty());
    assert!(collected.leftovers.is_empty(), "{:?}", collected.leftovers);
    assert!(collected.failures.is_empty());
    for name in &kept {
        assert!(store.join(name).exists(), "{name}");
    }
}

/// The leftovers of the store go: an orphan identity that no reference
/// names, the temp file of an import or `put_made`, and the temp file of
/// an identity write, also for a marked digest.
#[test]
fn leftovers_are_removed_and_counted() {
    let temp = Temp::new("leftovers");
    let current = temp.image("current");
    let store = temp.state.store_dir();
    let absent = digest_of(b"absent");
    let leftovers = [
        store.join(format!("{absent}.identity")),
        store.join(format!("{absent}.tmp")),
        store.join(format!("{}.tmp", current.digest())),
        store.join(format!(".{}.identity.77.3.tmp", current.digest())),
    ];
    for path in &leftovers {
        fs::write(path, "left").expect("the leftover");
    }

    let keep = HashSet::from([current.digest()]);
    let collected = collect(&temp.store, &temp.state, &keep).expect("the collection");

    let mut removed: Vec<_> = collected
        .leftovers
        .iter()
        .map(|leftover| leftover.path.clone())
        .collect();
    removed.sort_unstable();
    let mut expected = leftovers.to_vec();
    expected.sort_unstable();
    assert_eq!(removed, expected);
    assert!(collected.removed.is_empty());
    assert!(collected.failures.is_empty(), "{:?}", collected.failures);
    let bytes: u64 = collected
        .leftovers
        .iter()
        .map(|leftover| leftover.allocated_bytes)
        .sum();
    assert_eq!(collected.reclaimed_bytes, bytes);
    for path in &leftovers {
        assert!(!path.exists(), "{}", path.display());
    }
    assert!(current.path().exists());
    assert!(temp.store.identity_path(&current.digest()).exists());
}

/// The digests of `collected.removed`.
fn removed_digests(collected: &Collected) -> Vec<ImageDigest> {
    collected
        .removed
        .iter()
        .map(|removed| removed.digest)
        .collect()
}

/// `plan` lists the pairs and the leftovers that `collect` removes, and
/// removes nothing.
#[test]
fn a_plan_lists_the_collection_and_removes_nothing() {
    let temp = Temp::new("plan");
    let kept = temp.image("kept");
    let unused = temp.image("unused");
    let store = temp.state.store_dir();
    let leftover = store.join(format!("{}.tmp", digest_of(b"import")));
    fs::write(&leftover, "left").expect("the leftover");
    let keep = HashSet::from([kept.digest()]);

    let listed = plan(&temp.store, &temp.state, &keep, &HashSet::new()).expect("the plan");

    assert_eq!(removed_digests(&listed), [unused.digest()]);
    assert_eq!(listed.leftovers.len(), 1, "{:?}", listed.leftovers);
    assert_eq!(listed.leftovers[0].path, leftover);
    assert!(listed.failures.is_empty(), "{:?}", listed.failures);
    assert!(unused.path().exists() && leftover.exists());
    assert!(temp.store.identity_path(&unused.digest()).exists());

    let collected = collect(&temp.store, &temp.state, &keep).expect("the collection");
    assert_eq!(removed_digests(&collected), removed_digests(&listed));
    assert_eq!(collected.reclaimed_bytes, listed.reclaimed_bytes);
    assert!(!unused.path().exists() && !leftover.exists());
    assert!(kept.path().exists());
}

/// `plan` skips the key of each generation that snapshot retention would
/// drop, so the images of that key are listed.
#[test]
fn a_plan_skips_the_keys_of_dropped_generations() {
    let temp = Temp::new("plan-dropped");
    let images = [
        temp.image("snapshot-kernel"),
        temp.image("snapshot-initramfs"),
        temp.image("snapshot-root"),
    ];
    temp.snapshot([&images[0], &images[1], &images[2]]);
    let id: SessionId = ID.parse().expect("the session id");

    let listed =
        plan(&temp.store, &temp.state, &HashSet::new(), &HashSet::new()).expect("the plan");
    assert!(listed.removed.is_empty(), "{:?}", listed.removed);

    let dropped = HashSet::from([(id, SnapshotGeneration(1))]);
    let listed = plan(&temp.store, &temp.state, &HashSet::new(), &dropped).expect("the plan");
    let mut expected: Vec<_> = images.iter().map(CheckedImage::digest).collect();
    expected.sort_unstable();
    assert_eq!(removed_digests(&listed), expected);
    for image in &images {
        assert!(image.path().exists());
    }
}

/// `plan` gives the failure of `collect` for a pair with no identity.
#[test]
fn a_plan_reports_a_pair_with_no_identity() {
    let temp = Temp::new("plan-no-identity");
    let image = temp.image("bare");
    fs::remove_file(temp.store.identity_path(&image.digest())).expect("no identity");

    let listed =
        plan(&temp.store, &temp.state, &HashSet::new(), &HashSet::new()).expect("the plan");
    assert!(listed.removed.is_empty());
    assert_eq!(listed.failures.len(), 1, "{:?}", listed.failures);

    let collected = collect(&temp.store, &temp.state, &HashSet::new()).expect("the collection");
    assert!(collected.removed.is_empty());
    assert_eq!(collected.failures.len(), 1, "{:?}", collected.failures);
    assert!(image.path().exists());
}

#[test]
fn expected_snapshot_leftovers_do_not_block_collection() {
    let temp = Temp::new("snapshot-leftovers");
    let unused = temp.image("unused");
    let id: SessionId = ID.parse().expect("the session id");
    let snapshots = temp.state.session(&id).snapshots();
    fs::create_dir_all(snapshots.join("1.tmp")).expect("the temp generation");
    fs::create_dir_all(snapshots.join("2.retired")).expect("the retired generation");

    let collected = collect(&temp.store, &temp.state, &HashSet::new()).expect("the collection");

    let [removed] = collected.removed.as_slice() else {
        panic!("not one removal: {:?}", collected.removed);
    };
    assert_eq!(removed.digest, unused.digest());
}

#[test]
fn a_link_at_a_digest_name_stays() {
    let temp = Temp::new("digest-link");
    let store = temp.state.store_dir();
    fs::create_dir_all(&store).expect("the store dir");
    let target = temp.dir.join("target");
    fs::write(&target, "bytes").expect("the target");
    let link = store.join(digest_of(b"linked").to_string());
    symlink(&target, &link).expect("the link");

    let collected = collect(&temp.store, &temp.state, &HashSet::new()).expect("the collection");

    assert!(collected.removed.is_empty());
    assert!(collected.failures.is_empty());
    assert!(link.is_symlink());
}

#[test]
fn an_unexpected_session_entry_cancels_before_any_remove() {
    let temp = Temp::new("unexpected-session");
    let unused = temp.image("unused");
    let sessions = temp.state.sessions_dir();
    fs::create_dir_all(&sessions).expect("the sessions dir");
    fs::write(sessions.join("unknown"), "entry").expect("the unknown entry");

    collect(&temp.store, &temp.state, &HashSet::new()).expect_err("a refused scan");

    assert!(unused.path().exists());
}

#[test]
fn absent_sessions_and_store_are_an_empty_collection() {
    let temp = Temp::new("absent");

    let collected = collect(&temp.store, &temp.state, &HashSet::new()).expect("the collection");

    assert!(collected.removed.is_empty());
    assert!(collected.failures.is_empty());
    assert_eq!(collected.reclaimed_bytes, 0);
}
