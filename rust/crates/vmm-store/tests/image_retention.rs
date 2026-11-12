//! Image retention keeps every current or snapshotted image and removes
//! only complete, checked store pairs that no reference names.

use std::collections::HashSet;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::time::Duration;

use creche_vmm_store::image_retention::collect;
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

#[test]
fn unknown_store_names_and_orphan_identities_stay() {
    let temp = Temp::new("unknown-store");
    let store = temp.state.store_dir();
    fs::create_dir_all(&store).expect("the store dir");
    fs::write(store.join("notes"), "keep").expect("the unknown file");
    let orphan = format!("{}.identity", digest_of(b"absent"));
    fs::write(store.join(&orphan), "keep").expect("the orphan identity");

    let collected = collect(&temp.store, &temp.state, &HashSet::new()).expect("the collection");

    assert!(collected.removed.is_empty());
    assert!(collected.failures.is_empty());
    assert!(store.join("notes").exists());
    assert!(store.join(orphan).exists());
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
