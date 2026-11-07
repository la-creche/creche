//! The guest init initramfs with no KVM and no sudo (decisions CH62 rules
//! 17 and 18). A test of the rust job runs as the runner user and cannot
//! make a root-owned file. So it proves the archive entries, and each
//! refusal that comes before the owner check or is the owner check. The
//! kvm job proves a root-owned program end to end:
//! `compose::tests::kvm_guest_init_program_must_be_root_owned`, and
//! `tests/kvm_guest.rs` boots the archive to `guest-init: up`.

use std::fs;
use std::io::ErrorKind;
use std::os::unix::fs::{MetadataExt, symlink};
use std::path::PathBuf;

use creche_vmm_store::images::ImageStore;
use creche_vmm_store::initramfs::{Entry, Kind};
use creche_vmm_store::layout::StateRoot;

use super::{GuestInitError, entries, write_initramfs};

/// A scratch dir that its drop removes.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

impl Scratch {
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("vmm-guest-init-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        Self(dir)
    }

    fn store(&self) -> ImageStore {
        let state = StateRoot::new(self.0.join("state")).unwrap();
        ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"))
    }
}

/// The kernel opens `/dev/console` for PID 1 before any devtmpfs mount,
/// and it runs `/init` (decision CH62 rule 19). Root alone may read and
/// run `/init`.
#[test]
fn the_archive_holds_dev_the_console_and_init() {
    let entry = |name: &str, mode, kind| Entry {
        name: name.to_owned(),
        mode,
        kind,
    };
    let expected = [
        entry("dev", 0o755, Kind::Dir),
        entry(
            "dev/console",
            0o600,
            Kind::CharDevice { major: 5, minor: 1 },
        ),
        entry("init", 0o500, Kind::File(b"program".to_vec())),
    ];
    assert_eq!(entries(b"program".to_vec()), expected);
}

/// Decision CH62 rule 17: a program that root does not own refuses the
/// start, and no archive reaches the store. A test that runs as root owns
/// a root-owned file, so the case has nothing to prove there.
#[test]
fn a_guest_init_program_that_root_does_not_own_refuses() {
    let scratch = Scratch::new("owner");
    let program = scratch.0.join("guest-init");
    fs::write(&program, "program").unwrap();
    let uid = fs::metadata(&program).unwrap().uid();
    if uid == 0 {
        return;
    }
    let store = scratch.store();
    let refused = write_initramfs(&program, &store).err();
    assert!(
        matches!(refused, Some(GuestInitError::NotOwned { uid: found }) if found == uid),
        "{refused:?}"
    );
    assert_eq!(
        refused.unwrap().to_string(),
        format!("VMM_GUEST_INIT_PROGRAM: the program is owned by uid {uid}, not by root")
    );
    assert!(!scratch.0.join("state/store").exists());
}

/// A missing program, a symlink and a dir each refuse the start before
/// any owner check.
#[test]
fn a_program_that_is_no_regular_file_refuses() {
    let scratch = Scratch::new("regular");
    let store = scratch.store();
    let missing = write_initramfs(&scratch.0.join("missing"), &store).err();
    assert!(
        matches!(&missing, Some(GuestInitError::Io(error)) if error.kind() == ErrorKind::NotFound),
        "{missing:?}"
    );

    let target = scratch.0.join("target");
    fs::write(&target, "program").unwrap();
    let link = scratch.0.join("link");
    symlink(&target, &link).unwrap();
    let dir = scratch.0.join("dir");
    fs::create_dir(&dir).unwrap();
    for path in [link, dir] {
        let refused = write_initramfs(&path, &store).err();
        assert!(
            matches!(refused, Some(GuestInitError::NotRegular)),
            "{path:?}: {refused:?}"
        );
    }
}
