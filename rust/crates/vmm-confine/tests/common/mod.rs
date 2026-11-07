//! The helpers of the process tests: the example binary, the vmm unit of
//! each launch and the VM files that a launch needs before its Landlock
//! ruleset opens them.
#![cfg(test)]

use std::fs;
use std::path::PathBuf;

use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_confine::vmm_unit::VmmUnit;
use creche_vmm_store::layout::{RunRoot, StateRoot};
use creche_vmm_store::serial_log::SerialLog;

/// The example binary. Cargo builds it next to the test's `deps` dir.
pub fn example() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let debug = exe.parent().and_then(|deps| deps.parent()).unwrap();
    debug.join("examples").join("launch_chain")
}

/// The vmm unit of each launch (decision CH61 rule 2). The fake
/// `systemd-run` of the example makes no scope, so no unit of this name
/// needs to run.
pub fn vmm_unit() -> VmmUnit {
    VmmUnit::new("creche-vmm.service".to_owned()).unwrap()
}

/// The files of one VM under a new directory in `/tmp`, removed on drop.
/// The state root and the run root are both the directory, the root disk
/// is `root.img` and the initramfs of the restore is `initramfs`.
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
        fs::write(root.join("initramfs"), "initramfs").unwrap();
        fs::write(session.disk(), "session").unwrap();
        SerialLog::fresh(&session).unwrap();
        let root_disk = HostPath::new(root.join("root.img")).unwrap();
        let initramfs = HostPath::new(root.join("initramfs")).unwrap();
        let start = Start::Restore { initramfs };
        let files = VmFiles::new(start, root_disk, &session, &run).unwrap();
        Self { root, files }
    }
}

impl Drop for TempVm {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
