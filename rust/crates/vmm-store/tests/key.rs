//! A snapshot keeps only while its key equals the current key: the CH rev,
//! the kernel digest, the initramfs digest or none, the root disk digest
//! and the guest init protocol (decisions CH7-6, CH61 rule 3). The vmm
//! accepts its `cloud-hypervisor` only when
//! `--version` reports `CH_VERSION` (decision CH36 with gap G10).
//!
//! `tests/fixtures/cloud-hypervisor` stands in for the program. A test
//! runs it through a symlink, and the link name sets the reply. A symlink
//! opens no file for write, so no other test thread can hold a write fd of
//! the stub when it runs.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::time::Duration;

use creche_vmm_store::images::{CheckedImage, ImageDigest, ImageStore};
use creche_vmm_store::key::{
    CH_REV, ChVersionError, CheckedCh, DropReason, KeyCheck, SnapshotKey, check_cloud_hypervisor,
    check_newest, write_key,
};
use creche_vmm_store::layout::{SessionDir, SnapshotGeneration, StateRoot};
use creche_wire::{GUEST_INIT_PROTOCOL, SessionId};
use serde_json::Value;
use sha2::{Digest, Sha256};

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const TIMEOUT: Duration = Duration::from_secs(10);
const FIRST: SnapshotGeneration = SnapshotGeneration(1);

/// A state root with three imported images and one session, in a new temp
/// dir, removed on drop.
struct Temp {
    root: PathBuf,
    session: SessionDir,
    images: [CheckedImage; 3],
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-key-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("the temp dir");
        let state = StateRoot::new(root.join("state")).expect("a state root");
        let store = ImageStore::new(&state, PathBuf::from("zstd"));
        let images = ["a", "b", "c"].map(|text| {
            let source = root.join(text);
            fs::write(&source, text).expect("a source file");
            let digest = digest_of(text.as_bytes());
            store.import(&source, digest).expect("the import");
            store.check(digest).expect("the check")
        });
        let id: SessionId = ID.parse().expect("ID is a session id");
        let session = state.session(&id);
        Self {
            root,
            session,
            images,
        }
    }

    /// Runs the stub through a link named `name`.
    fn ch(&self, name: &str) -> Result<CheckedCh, ChVersionError> {
        check_cloud_hypervisor(&self.link(name), TIMEOUT)
    }

    /// Makes a link named `name` to the stub.
    fn link(&self, name: &str) -> PathBuf {
        let link = self.root.join(name);
        let _ = fs::remove_file(&link);
        symlink(fixture("cloud-hypervisor"), &link).expect("the link");
        link
    }

    /// The key of a VM with images `kernel` and `root_disk` and no
    /// initramfs.
    fn key(&self, kernel: usize, root_disk: usize) -> SnapshotKey {
        self.key_with(kernel, None, root_disk)
    }

    /// The key of a VM with images `kernel`, `initramfs` and `root_disk`.
    fn key_with(&self, kernel: usize, initramfs: Option<usize>, root_disk: usize) -> SnapshotKey {
        let ch = self.ch("v54.0.0").expect("the version check");
        let initramfs = initramfs.map(|index| &self.images[index]);
        SnapshotKey::current(ch, &self.images[kernel], initramfs, &self.images[root_disk])
    }

    /// Makes a generation dir with one file and writes `key` into it.
    fn snapshot(&self, generation: SnapshotGeneration, key: &SnapshotKey) {
        let dir = self.session.snapshot(generation);
        fs::create_dir_all(&dir).expect("the dir");
        fs::write(dir.join("memory-ranges"), "ram").expect("a file");
        write_key(&self.session, generation, key).expect("the key");
    }

    /// Sets `field` of the key file of `generation` to `value`.
    fn edit_key(&self, generation: SnapshotGeneration, field: &str, value: Value) {
        let path = self.session.snapshot_key(generation);
        let mut key: Value =
            serde_json::from_slice(&fs::read(&path).expect("the key")).expect("a JSON key");
        key[field] = value;
        fs::write(&path, serde_json::to_vec(&key).expect("JSON")).expect("the edit");
    }

    /// Checks against the key of images 0 and 1 and asserts a drop.
    fn assert_dropped(&self, reason: DropReason) {
        let check = check_newest(&self.session, &self.key(0, 1)).expect("the check");
        assert_eq!(
            check,
            KeyCheck::Dropped {
                generation: FIRST,
                reason
            }
        );
        assert!(!self.session.snapshot(FIRST).exists());
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
fn the_version_check_takes_v54_0_0_only() {
    let temp = Temp::new("version");
    temp.ch("v54.0.0").expect("with the name");
    temp.ch("bare-v54.0.0").expect("alone");
    for name in ["v54.0.1", "v54.0.0-dirty", "bare-v54.0", "bare-"] {
        let error = temp.ch(name).expect_err(name);
        assert!(
            matches!(error, ChVersionError::Mismatch(_)),
            "{name}: {error}"
        );
    }
    let error = temp.ch("fail").expect_err("a failure");
    assert!(matches!(error, ChVersionError::Failed { .. }), "{error}");
}

#[test]
fn an_equal_key_keeps_the_snapshot() {
    let temp = Temp::new("equal");
    temp.snapshot(FIRST, &temp.key(0, 1));
    let key = temp.session.snapshot_key(FIRST);
    assert_eq!(fs::metadata(&key).expect("the key").mode() & 0o777, 0o600);
    let KeyCheck::Keyed(keyed) = check_newest(&temp.session, &temp.key(0, 1)).expect("the check")
    else {
        panic!("not Keyed");
    };
    assert_eq!(keyed.generation(), FIRST);
    assert_eq!(keyed.dir(), temp.session.snapshot(FIRST));
    assert!(keyed.dir().join("memory-ranges").exists());
}

#[test]
fn another_ch_rev_drops_the_snapshot() {
    let temp = Temp::new("rev");
    temp.snapshot(FIRST, &temp.key(0, 1));
    assert_eq!(CH_REV, "038ce5a72d761380a02f714a4eb4ca63e0ea073d");
    temp.edit_key(FIRST, "ch_rev", Value::from("0".repeat(40)));
    temp.assert_dropped(DropReason::Differs);
}

#[test]
fn another_kernel_drops_the_snapshot() {
    let temp = Temp::new("kernel");
    temp.snapshot(FIRST, &temp.key(2, 1));
    temp.assert_dropped(DropReason::Differs);
}

/// The key holds the initramfs digest, so a restore reads the file of
/// its snapshot (decision CH61 rule 3). A key with one keeps only
/// against the same one, and a key with none keeps only against none.
#[test]
fn another_initramfs_drops_the_snapshot() {
    let temp = Temp::new("initramfs");
    let with = |index| temp.key_with(0, Some(index), 1);
    temp.snapshot(FIRST, &with(2));
    let text = fs::read_to_string(temp.session.snapshot_key(FIRST)).expect("the key");
    let key: Value = serde_json::from_str(&text).expect("a JSON key");
    assert_eq!(
        key["initramfs"],
        Value::from(temp.images[2].digest().to_string())
    );
    let check = check_newest(&temp.session, &with(2)).expect("the check");
    assert!(matches!(check, KeyCheck::Keyed(_)), "{check:?}");
    for current in [with(0), temp.key(0, 1)] {
        temp.snapshot(FIRST, &with(2));
        let check = check_newest(&temp.session, &current).expect("the check");
        let dropped = KeyCheck::Dropped {
            generation: FIRST,
            reason: DropReason::Differs,
        };
        assert_eq!(check, dropped);
    }
    temp.snapshot(FIRST, &temp.key(0, 1));
    let key: Value =
        serde_json::from_slice(&fs::read(temp.session.snapshot_key(FIRST)).expect("the key"))
            .expect("a JSON key");
    assert_eq!(key["initramfs"], Value::Null);
    let check = check_newest(&temp.session, &with(2)).expect("the check");
    assert!(matches!(check, KeyCheck::Dropped { .. }), "{check:?}");
}

#[test]
fn another_root_disk_drops_the_snapshot() {
    let temp = Temp::new("disk");
    temp.snapshot(FIRST, &temp.key(0, 2));
    temp.assert_dropped(DropReason::Differs);
}

#[test]
fn a_swap_of_kernel_and_disk_drops_the_snapshot() {
    let temp = Temp::new("swap");
    temp.snapshot(FIRST, &temp.key(1, 0));
    temp.assert_dropped(DropReason::Differs);
}

#[test]
fn another_guest_init_protocol_drops_the_snapshot() {
    let temp = Temp::new("protocol");
    temp.snapshot(FIRST, &temp.key(0, 1));
    let next = GUEST_INIT_PROTOCOL.saturating_add(1);
    temp.edit_key(FIRST, "guest_init_protocol", Value::from(next));
    temp.assert_dropped(DropReason::Differs);
}

#[test]
fn an_unreadable_key_drops_the_snapshot() {
    let temp = Temp::new("unreadable");
    let key = temp.session.snapshot_key(FIRST);
    let outside = temp.root.join("outside-key.json");
    let cases: [(&str, &dyn Fn()); 6] = [
        ("absent", &|| fs::remove_file(&key).expect("a remove")),
        ("not JSON", &|| fs::write(&key, "{").expect("a write")),
        // A valid key with trailing spaces fails only on the size cap.
        ("too large", &|| {
            let mut text = fs::read(&key).expect("the key");
            text.extend(b" ".repeat(5000));
            fs::write(&key, text).expect("a write");
        }),
        // The link names a matching key, so only the link check drops it.
        ("a link", &|| {
            fs::rename(&key, &outside).expect("a move");
            symlink(&outside, &key).expect("a link");
        }),
        ("a dir", &|| {
            fs::remove_file(&key).expect("a remove");
            fs::create_dir(&key).expect("a dir");
        }),
        ("an extra field", &|| {
            temp.edit_key(FIRST, "extra", Value::from(1))
        }),
    ];
    for (name, spoil) in cases {
        temp.snapshot(FIRST, &temp.key(0, 1));
        spoil();
        let check = check_newest(&temp.session, &temp.key(0, 1)).expect(name);
        let dropped = KeyCheck::Dropped {
            generation: FIRST,
            reason: DropReason::Unreadable,
        };
        assert_eq!(check, dropped, "{name}");
        assert!(!temp.session.snapshot(FIRST).exists(), "{name}");
    }
}

/// A key file that exists but does not open is no drop reason. The check
/// fails and removes nothing (a root user opens any file, so it skips).
#[test]
fn a_failed_read_drops_nothing() {
    let temp = Temp::new("denied");
    temp.snapshot(FIRST, &temp.key(0, 1));
    let key = temp.session.snapshot_key(FIRST);
    fs::set_permissions(&key, fs::Permissions::from_mode(0o000)).expect("a mode");
    if fs::File::open(&key).is_ok() {
        return;
    }
    let error = check_newest(&temp.session, &temp.key(0, 1)).expect_err("a failure");
    assert_eq!(error.path, key);
    assert_eq!(error.source.kind(), std::io::ErrorKind::PermissionDenied);
    fs::set_permissions(&key, fs::Permissions::from_mode(0o600)).expect("a mode");
    let check = check_newest(&temp.session, &temp.key(0, 1)).expect("the check");
    assert!(matches!(check, KeyCheck::Keyed(_)), "{check:?}");
}

#[test]
fn a_drop_removes_every_older_generation() {
    let temp = Temp::new("older");
    let second = SnapshotGeneration(2);
    temp.snapshot(FIRST, &temp.key(0, 1));
    temp.snapshot(second, &temp.key(0, 2));
    let check = check_newest(&temp.session, &temp.key(0, 1)).expect("the check");
    let dropped = KeyCheck::Dropped {
        generation: second,
        reason: DropReason::Differs,
    };
    assert_eq!(check, dropped);
    assert!(!temp.session.snapshot(FIRST).exists(), "its key matches");
    let again = check_newest(&temp.session, &temp.key(0, 1)).expect("the check");
    assert_eq!(again, KeyCheck::NoSnapshot);
}
