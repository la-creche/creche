//! The helpers of the process tests: the example binary and the VM files
//! that a launch needs before its Landlock ruleset opens them.
#![cfg(test)]

use std::fs;
use std::path::{Path, PathBuf};

use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_store::layout::{RunRoot, StateRoot};

/// The example binary. Cargo builds it next to the test's `deps` dir.
pub fn example() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let debug = exe.parent().and_then(|deps| deps.parent()).unwrap();
    debug.join("examples").join("launch_chain")
}

/// The files of one VM under a new directory in `/tmp`, removed on drop.
/// The layout is the one of the `spawn` role of the example: the state
/// root and the run root are both the directory, and the root disk is
/// `root.img`.
pub struct TempVm {
    root: PathBuf,
    pub files: VmFiles,
}

impl TempVm {
    pub fn new(name: &str, id: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-confine-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let id = id.parse().unwrap();
        let session = StateRoot::new(root.clone()).unwrap().session(&id);
        let run = RunRoot::new(root.clone()).unwrap().vm(&id);
        fs::create_dir_all(session.snapshots()).unwrap();
        fs::create_dir_all(run.path()).unwrap();
        fs::write(root.join("root.img"), "root").unwrap();
        fs::write(session.disk(), "session").unwrap();
        let root_disk = HostPath::new(root.join("root.img")).unwrap();
        let files = VmFiles::new(Start::Restore, root_disk, &session, &run).unwrap();
        Self { root, files }
    }

    pub fn path(&self) -> &Path {
        &self.root
    }
}

impl Drop for TempVm {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
