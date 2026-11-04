//! The check at VM start passes an imported copy and refuses an edited
//! copy, a replaced inode and a mode change (decision CH8-8 as read by
//! CH36 with gap G8).

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

use creche_vmm_store::images::{
    CheckError, CheckedImage, FileIdentity, ImageDigest, ImageStore, StoredImage,
};
use creche_vmm_store::layout::StateRoot;
use sha2::{Digest, Sha256};

const BYTES: &[u8] = b"a root disk, short for a test";

/// A state root with one imported copy in a new temp dir, removed on drop.
struct Temp {
    dir: PathBuf,
    store: ImageStore,
    stored: StoredImage,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("vmm-check-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("the temp dir");
        let root = StateRoot::new(dir.join("state")).expect("a state root");
        let store = ImageStore::new(&root, PathBuf::from("zstd"));
        let source = dir.join("disk.ext4");
        fs::write(&source, BYTES).expect("the source file");
        let stored = store.import(&source, digest_of(BYTES)).expect("the import");
        Self { dir, store, stored }
    }

    fn set_mode(&self, mode: u32) {
        fs::set_permissions(&self.stored.path, fs::Permissions::from_mode(mode)).expect("the mode");
    }

    fn check(&self) -> Result<CheckedImage, CheckError> {
        self.store.check(self.stored.digest)
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
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
fn an_imported_copy_passes() {
    let temp = Temp::new("pass");
    let checked = temp.check().expect("the check");
    assert_eq!(checked.digest(), temp.stored.digest);
    assert_eq!(checked.path(), temp.stored.path);
}

#[test]
fn an_edited_copy_is_refused() {
    let temp = Temp::new("edited");
    temp.set_mode(0o644);
    OpenOptions::new()
        .append(true)
        .open(&temp.stored.path)
        .expect("the copy opens")
        .write_all(b"more")
        .expect("the edit");
    temp.set_mode(0o444);

    let error = temp.check().expect_err("a refusal");
    let CheckError::Changed {
        recorded, found, ..
    } = error
    else {
        panic!("not Changed: {error}");
    };
    assert_eq!(*recorded, temp.stored.identity);
    assert_ne!(found.size, recorded.size);
}

#[test]
fn a_replaced_inode_is_refused() {
    let temp = Temp::new("replaced");
    let other = temp.dir.join("other");
    fs::write(&other, BYTES).expect("the same bytes");
    fs::set_permissions(&other, fs::Permissions::from_mode(0o444)).expect("the mode");
    fs::rename(&other, &temp.stored.path).expect("the rename");

    let error = temp.check().expect_err("a refusal");
    let CheckError::Changed { found, .. } = error else {
        panic!("not Changed: {error}");
    };
    assert_ne!(found.inode, temp.stored.identity.inode);
}

#[test]
fn a_mode_change_is_refused() {
    let temp = Temp::new("mode");
    temp.set_mode(0o644);

    let error = temp.check().expect_err("a refusal");
    assert!(
        matches!(
            error,
            CheckError::BadMode {
                mode: 0o100_644,
                ..
            }
        ),
        "{error}"
    );
}

#[test]
fn a_record_of_mode_644_does_not_pass() {
    let temp = Temp::new("record");
    temp.set_mode(0o644);
    let metadata = fs::metadata(&temp.stored.path).expect("the copy");
    let text = serde_json::to_vec(&FileIdentity::of(&metadata)).expect("json");
    fs::write(temp.store.identity_path(&temp.stored.digest), text).expect("the record");

    let error = temp.check().expect_err("a refusal");
    assert!(matches!(error, CheckError::BadMode { .. }), "{error}");
}

#[test]
fn a_link_at_the_store_path_is_refused() {
    let temp = Temp::new("link");
    let target = temp.dir.join("target");
    fs::rename(&temp.stored.path, &target).expect("the move");
    std::os::unix::fs::symlink(&target, &temp.stored.path).expect("the link");

    let error = temp.check().expect_err("a refusal");
    assert!(matches!(error, CheckError::BadMode { .. }), "{error}");
}

#[test]
fn a_copy_never_imported_is_refused() {
    let temp = Temp::new("absent");
    let error = temp
        .store
        .check(digest_of(b"another disk"))
        .expect_err("a refusal");
    let CheckError::Io { source, .. } = error else {
        panic!("not Io: {error}");
    };
    assert_eq!(source.kind(), std::io::ErrorKind::NotFound);
}
