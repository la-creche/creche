//! The program check of `root_owned` (decision CH67 rule 1). A test of
//! the rust job runs as the runner user and cannot make a root-owned
//! file. So it proves each refusal that a test user can make: a missing
//! file, a symlink, a dir and an owner other than root. Each fault names
//! its key. `src/compose/tests.rs` proves root-owned programs and a group
//! write bit in the job kvm.

use std::fs;
use std::io::ErrorKind;
use std::os::unix::fs::{MetadataExt, symlink};
use std::path::PathBuf;

use creche_vmm::config::{CH, CH_REMOTE};
use creche_vmm::root_owned::{OwnerError, ProgramError, check_program};

/// A scratch dir that its drop removes.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl Scratch {
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("vmm-root-owned-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        Self(dir)
    }
}

#[test]
fn a_missing_program_refuses() {
    let scratch = Scratch::new("missing");
    let refused = check_program(CH, &scratch.0.join("cloud-hypervisor")).unwrap_err();
    assert!(
        matches!(
            &refused,
            ProgramError {
                key: CH,
                fault: OwnerError::Io(error),
            } if error.kind() == ErrorKind::NotFound
        ),
        "{refused:?}"
    );
    assert!(
        refused
            .to_string()
            .starts_with("VMM_CH_PROGRAM: the program cannot be read: "),
        "{refused}"
    );
}

/// A symlink refuses also when its target is a regular file, and so does
/// a dir.
#[test]
fn a_symlink_or_a_dir_refuses() {
    let scratch = Scratch::new("regular");
    let target = scratch.0.join("target");
    fs::write(&target, "program").unwrap();
    let link = scratch.0.join("link");
    symlink(&target, &link).unwrap();
    let dir = scratch.0.join("dir");
    fs::create_dir(&dir).unwrap();
    for path in [link, dir] {
        let refused = check_program(CH_REMOTE, &path).unwrap_err();
        assert!(
            matches!(
                refused,
                ProgramError {
                    key: CH_REMOTE,
                    fault: OwnerError::NotRegular,
                }
            ),
            "{path:?}: {refused:?}"
        );
        assert_eq!(
            refused.to_string(),
            "VMM_CH_REMOTE_PROGRAM: the program is not a regular file"
        );
    }
}

/// A test that runs as root owns a root-owned file, so the case has
/// nothing to prove there.
#[test]
fn a_program_that_root_does_not_own_refuses() {
    let scratch = Scratch::new("owner");
    let program = scratch.0.join("ch-remote");
    fs::write(&program, "program").unwrap();
    let uid = fs::metadata(&program).unwrap().uid();
    if uid == 0 {
        return;
    }
    let refused = check_program(CH_REMOTE, &program).unwrap_err();
    assert!(
        matches!(
            refused,
            ProgramError {
                key: CH_REMOTE,
                fault: OwnerError::NotOwned { uid: found },
            } if found == uid
        ),
        "{refused:?}"
    );
    assert_eq!(
        refused.to_string(),
        format!("VMM_CH_REMOTE_PROGRAM: the program is owned by uid {uid}, not by root")
    );
}
