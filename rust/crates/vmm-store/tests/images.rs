//! The digest store copies each file once, refuses a wrong digest, keeps
//! its copy when the source goes and decompresses a `.zst` source
//! (fact 56, decisions CH35 rule 4, CH36 with gaps G8 and G9).

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::Command;

use creche_vmm_store::images::{ImageDigest, ImageStore, ImportError, STORE_MODE};
use creche_vmm_store::layout::StateRoot;
use sha2::{Digest, Sha256};

const BYTES: &[u8] = b"a root disk, short for a test";

/// A state root and a source dir in a new temp dir, removed on drop.
struct Temp {
    dir: PathBuf,
    store: ImageStore,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("vmm-images-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("source")).expect("the source dir");
        let root = StateRoot::new(dir.join("state")).expect("a state root");
        let store = ImageStore::new(&root, PathBuf::from("zstd"));
        Self { dir, store }
    }

    fn source(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let path = self.dir.join("source").join(name);
        fs::write(&path, bytes).expect("the source file");
        path
    }

    /// Every file name in the store dir, sorted.
    fn store_files(&self) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(self.dir.join("state").join("store"))
            .expect("the store dir")
            .map(|entry| {
                entry
                    .expect("an entry")
                    .file_name()
                    .into_string()
                    .expect("utf-8")
            })
            .collect();
        names.sort();
        names
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
fn a_digest_is_64_lowercase_hex_characters() {
    let text = "ab".repeat(32);
    let digest: ImageDigest = text.parse().expect("a digest");
    assert_eq!(digest.to_string(), text);
    for bad in [
        "AB".repeat(32),
        "ab".repeat(31),
        "ab".repeat(33),
        "zz".repeat(32),
    ] {
        assert!(bad.parse::<ImageDigest>().is_err(), "{bad}");
    }
}

#[test]
fn the_same_file_twice_gives_one_entry() {
    let temp = Temp::new("twice");
    let source = temp.source("disk.ext4", BYTES);
    let digest = digest_of(BYTES);

    let first = temp.store.import(&source, digest).expect("the import");
    assert_eq!(first.path, temp.store.path(&digest));
    assert_eq!(fs::read(&first.path).expect("the copy"), BYTES);
    let mode = fs::metadata(&first.path)
        .expect("the copy")
        .permissions()
        .mode();
    assert_eq!(mode & 0o7777, STORE_MODE);
    assert_eq!(first.identity.mode & 0o7777, STORE_MODE);

    let second = temp.store.import(&source, digest).expect("the repeat");
    assert_eq!(second, first);
    assert_eq!(
        temp.store_files(),
        [digest.to_string(), format!("{digest}.identity")]
    );
}

#[test]
fn a_wrong_digest_refuses_and_leaves_no_file() {
    let temp = Temp::new("wrong");
    let source = temp.source("disk.ext4", BYTES);
    let wrong = digest_of(b"another disk");

    let error = temp.store.import(&source, wrong).expect_err("a mismatch");
    let ImportError::Mismatch { expected, actual } = error else {
        panic!("not a mismatch: {error}");
    };
    assert_eq!(expected, wrong);
    assert_eq!(actual, digest_of(BYTES));
    assert!(temp.store_files().is_empty());
}

#[test]
fn a_removed_source_leaves_the_store_copy() {
    let temp = Temp::new("removed");
    let source = temp.source("vmlinux", BYTES);
    let stored = temp
        .store
        .import(&source, digest_of(BYTES))
        .expect("the import");

    fs::remove_dir_all(temp.dir.join("source")).expect("the source goes");
    assert_eq!(fs::read(&stored.path).expect("the copy"), BYTES);
}

#[test]
fn a_zst_source_is_decompressed_and_named_by_its_bytes() {
    let temp = Temp::new("zst");
    let plain = temp.source("disk.ext4", BYTES);
    let packed = temp.dir.join("source").join("disk.ext4.zst");
    let status = Command::new("zstd")
        .args(["-q", "-o"])
        .arg(&packed)
        .arg(&plain)
        .status()
        .expect("zstd runs");
    assert!(status.success());

    let stored = temp
        .store
        .import(&packed, digest_of(BYTES))
        .expect("the import");
    assert_eq!(fs::read(&stored.path).expect("the copy"), BYTES);
}

#[test]
fn a_bad_zst_source_refuses_and_leaves_no_file() {
    let temp = Temp::new("bad-zst");
    let source = temp.source("disk.ext4.zst", BYTES);

    let error = temp
        .store
        .import(&source, digest_of(BYTES))
        .expect_err("a failure");
    assert!(matches!(error, ImportError::Decompress { .. }), "{error}");
    assert!(temp.store_files().is_empty());
}
