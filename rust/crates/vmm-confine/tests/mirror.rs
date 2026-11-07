//! The read side of the git mirror in temp directories (decisions CH46
//! rule 4, CH69 rules 16 and 17): the dir of a repo and its `.git` must
//! each be a dir that no symlink reaches.

use std::fs;
use std::os::unix::fs::symlink;
use std::path::PathBuf;

use creche_vmm_confine::mirror::{MirrorFault, MirrorRoot};
use creche_vmm_store::layout::RootError;
use creche_wire::RepoName;

/// A mirror root in a new directory in `/tmp`, removed on drop.
struct Mirror(PathBuf);

impl Drop for Mirror {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).
#[cfg(test)]
impl Mirror {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-mirror-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        Self(root)
    }

    fn root(&self) -> MirrorRoot {
        MirrorRoot::new(self.0.clone()).unwrap()
    }

    fn git_dir(&self, name: &str) -> Result<PathBuf, MirrorFault> {
        self.root().git_dir(&repo(name))
    }
}

#[cfg(test)]
fn repo(name: &str) -> RepoName {
    name.parse().unwrap()
}

#[test]
fn a_repo_with_its_git_dir_is_held() {
    let mirror = Mirror::new("held");
    fs::create_dir_all(mirror.0.join("creche/.git")).unwrap();
    assert_eq!(
        mirror.git_dir("creche").unwrap(),
        mirror.0.join("creche/.git")
    );
}

#[test]
fn an_absent_repo_or_git_dir_is_absent() {
    let mirror = Mirror::new("absent");
    let read = mirror.git_dir("creche");
    assert!(matches!(read, Err(MirrorFault::Absent)), "{read:?}");
    fs::create_dir(mirror.0.join("creche")).unwrap();
    let read = mirror.git_dir("creche");
    assert!(matches!(read, Err(MirrorFault::Absent)), "{read:?}");
}

/// A symlink at `<name>` or at `.git` refuses the repo, also when it
/// points at a real repo, and so does a file in place of a dir.
#[test]
fn a_symlink_or_a_file_is_not_a_dir() {
    let mirror = Mirror::new("symlink");
    fs::create_dir_all(mirror.0.join("real/.git")).unwrap();
    symlink(mirror.0.join("real"), mirror.0.join("linked")).unwrap();
    let read = mirror.git_dir("linked");
    assert!(matches!(read, Err(MirrorFault::NotDir)), "{read:?}");
    fs::create_dir(mirror.0.join("inner")).unwrap();
    symlink(mirror.0.join("real/.git"), mirror.0.join("inner/.git")).unwrap();
    let read = mirror.git_dir("inner");
    assert!(matches!(read, Err(MirrorFault::NotDir)), "{read:?}");
    fs::create_dir(mirror.0.join("worktree")).unwrap();
    fs::write(mirror.0.join("worktree/.git"), "gitdir: ../real/.git\n").unwrap();
    let read = mirror.git_dir("worktree");
    assert!(matches!(read, Err(MirrorFault::NotDir)), "{read:?}");
}

#[test]
fn a_missing_root_is_an_io_fault() {
    let mirror = Mirror::new("missing");
    let root = MirrorRoot::new(mirror.0.join("none")).unwrap();
    let read = root.git_dir(&"creche".parse().unwrap());
    assert!(matches!(read, Err(MirrorFault::Io(_))), "{read:?}");
}

#[test]
fn the_root_is_absolute_with_no_parent_component() {
    let relative = MirrorRoot::new(PathBuf::from("srv/agents/code"));
    assert_eq!(relative, Err(RootError::NotAbsolute));
    let parent = MirrorRoot::new(PathBuf::from("/srv/../code"));
    assert_eq!(parent, Err(RootError::ParentComponent));
}
