//! A session disk is a 16 GiB sparse file, made and formatted at the first
//! boot only (decisions CH8-7, CH21-4, CH36 with gap G7).
//!
//! `true` and `false` stand in for `mke2fs`, so these tests run on any
//! host. The last test runs the host `mke2fs` and reads the result with
//! `dumpe2fs` and `debugfs`. e2fsprogs is a Linux tool, and the vmm runs
//! only on Linux, so that test is Linux only.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::PathBuf;

use creche_vmm_store::layout::{SessionDir, StateRoot};
use creche_vmm_store::session_disk::{DiskError, SESSION_DISK_BYTES, SessionDisks};
use creche_wire::SessionId;

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";

/// A state root in a new temp dir, removed on drop.
struct TempState(StateRoot);

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl TempState {
    fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!("vmm-disk-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        Self(StateRoot::new(path).expect("a state root"))
    }

    /// The session disks, formatted by `mke2fs`.
    fn disks(&self, mke2fs: &str) -> SessionDisks {
        SessionDisks::new(&self.0, PathBuf::from(mke2fs))
    }

    fn session(&self) -> SessionDir {
        self.0.session(&id())
    }
}

impl Drop for TempState {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(self.0.path());
    }
}

#[cfg(test)]
fn id() -> SessionId {
    ID.parse().expect("ID is a session id")
}

/// The bytes that a file takes on the host disk. A sparse file takes far
/// fewer than its apparent size.
#[cfg(test)]
fn used_bytes(metadata: &fs::Metadata) -> u64 {
    metadata.blocks().saturating_mul(512)
}

#[test]
fn the_first_boot_makes_a_sparse_16_gib_disk() {
    let state = TempState::new("first");

    let disk = state.disks("true").for_boot(&id()).expect("a disk");

    assert_eq!(disk.path(), state.session().disk());
    let metadata = fs::metadata(disk.path()).expect("the disk");
    assert_eq!(metadata.len(), SESSION_DISK_BYTES);
    assert_eq!(SESSION_DISK_BYTES, 16 * 1024 * 1024 * 1024);
    assert!(used_bytes(&metadata) < SESSION_DISK_BYTES / 64);
    assert_eq!(metadata.permissions().mode() & 0o7777, 0o600);
    assert!(!state.session().disk_temp().exists());
}

// The second call has a program that cannot run. It succeeds, so it ran no
// format.
#[test]
fn a_second_call_does_not_format_again() {
    let state = TempState::new("second");
    let first = state.disks("true").for_boot(&id()).expect("a disk");
    let before = fs::metadata(first.path()).expect("the disk");

    let second = state
        .disks("/nonexistent/mke2fs")
        .for_boot(&id())
        .expect("the same disk");

    assert_eq!(second, first);
    let after = fs::metadata(second.path()).expect("the disk");
    assert_eq!(
        (after.ino(), after.mtime(), after.mtime_nsec()),
        (before.ino(), before.mtime(), before.mtime_nsec())
    );
}

#[test]
fn a_failed_format_leaves_no_disk() {
    let state = TempState::new("failed");

    let error = state.disks("false").for_boot(&id()).expect_err("a failure");

    assert!(matches!(error, DiskError::Format { .. }), "{error}");
    assert!(!state.session().disk().exists());
    assert!(!state.session().disk_temp().exists());
    state.disks("true").for_boot(&id()).expect("a retry");
}

#[test]
fn a_dir_at_the_disk_path_refuses() {
    let state = TempState::new("dir");
    fs::create_dir_all(state.session().disk()).expect("a dir");

    let error = state.disks("true").for_boot(&id()).expect_err("a refusal");

    assert!(matches!(error, DiskError::NotAFile(_)), "{error}");
}

// A link at the disk path is refused, even when it points to a regular
// file. Cloud Hypervisor must never get a file outside the session dir.
#[test]
fn a_link_at_the_disk_path_refuses() {
    let state = TempState::new("link");
    let session = state.session();
    fs::create_dir_all(session.path()).expect("the session dir");
    let target = session.path().join("other");
    fs::write(&target, b"").expect("a plain file");
    std::os::unix::fs::symlink(&target, session.disk()).expect("a link");

    let error = state.disks("true").for_boot(&id()).expect_err("a refusal");

    assert!(matches!(error, DiskError::NotAFile(_)), "{error}");
}

/// The text that `program` writes to stdout, with each run of white space
/// cut to one space.
#[cfg(test)]
#[cfg(target_os = "linux")]
fn stdout_of(program: &str, args: &[&str], disk: &std::path::Path) -> String {
    let output = std::process::Command::new(program)
        .args(args)
        .arg(disk)
        .output()
        .expect("e2fsprogs runs");
    assert!(output.status.success(), "{program} failed");
    String::from_utf8_lossy(&output.stdout)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

// The test user is not root, so a root dir owned by 0:0 shows that the
// make set `root_owner`.
#[cfg(target_os = "linux")]
#[test]
fn the_host_mke2fs_makes_an_ext4_disk_owned_by_root() {
    let state = TempState::new("ext4");

    let disk = state
        .disks("/usr/sbin/mke2fs")
        .for_boot(&id())
        .expect("a disk");

    let metadata = fs::metadata(disk.path()).expect("the disk");
    assert_eq!(metadata.len(), SESSION_DISK_BYTES);
    let used = used_bytes(&metadata);
    assert!(used < SESSION_DISK_BYTES / 64, "{used} bytes used");
    let header = stdout_of("/usr/sbin/dumpe2fs", &["-h"], disk.path());
    assert!(
        header.contains("Filesystem magic number: 0xEF53"),
        "{header}"
    );
    assert!(header.contains("has_journal"), "{header}");
    assert!(header.contains("extent"), "{header}");
    let root = stdout_of("/usr/sbin/debugfs", &["-R", "stat /"], disk.path());
    assert!(root.contains("User: 0 Group: 0"), "{root}");

    let again = state
        .disks("/nonexistent/mke2fs")
        .for_boot(&id())
        .expect("the same disk");
    assert_eq!(again, disk);
}
