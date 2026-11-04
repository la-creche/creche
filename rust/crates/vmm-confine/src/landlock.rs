//! The Landlock ruleset of one VM (decisions CH8-3 and CH30).
//!
//! The launch step applies it after `RLIMIT_CORE` and before the exec of
//! `systemd-run`. So `systemd-run` and the Cloud Hypervisor process run
//! under it, on boot and on restore, with every CH thread inside it (fact
//! 64). The vmm itself never runs under it, so the `zstd` and `mke2fs`
//! that the vmm runs need no rule. The rules are the smallest set of fact
//! 77. The ruleset handles every file right and TCP bind and connect of
//! Landlock ABI 4, and no rule allows a TCP port (decision CH8-1). A
//! kernel below ABI 4 fails the launch.
//!
//! Landlock ABI 4 covers neither UDP nor Unix sockets. The AppArmor
//! profile of decision CH8-1 (PR C15) denies those.

use std::path::{Path, PathBuf};

use creche_vmm_store::layout::{SessionDir, VmRunDir};
use serde::{Deserialize, Serialize};

use crate::launch::{NotAbsolute, Program};

/// The KVM device (fact 77).
pub const KVM: &str = "/dev/kvm";

/// The random device (fact 77).
pub const URANDOM: &str = "/dev/urandom";

/// The system library directory of the x86_64 Ubuntu hosts: the host and
/// the CI runner (fact 77, decision CH30). On a host without it the open
/// of the path fails, so the launch fails closed.
pub const LIBRARY_DIR: &str = "/usr/lib/x86_64-linux-gnu";

/// The dynamic loader of those hosts (decision CH30). `/lib64` links here.
pub const LOADER: &str = "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2";

/// An absolute path of a file or a directory that a rule names.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "PathBuf", into = "PathBuf")]
pub struct HostPath(PathBuf);

impl HostPath {
    /// Checks `path` and makes the host path.
    ///
    /// # Errors
    ///
    /// `NotAbsolute` for a relative path.
    pub fn new(path: PathBuf) -> Result<Self, NotAbsolute> {
        if path.is_absolute() {
            Ok(Self(path))
        } else {
            Err(NotAbsolute)
        }
    }

    /// The path itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl TryFrom<PathBuf> for HostPath {
    type Error = NotAbsolute;

    fn try_from(path: PathBuf) -> Result<Self, Self::Error> {
        Self::new(path)
    }
}

impl From<HostPath> for PathBuf {
    fn from(path: HostPath) -> Self {
        path.0
    }
}

/// How Cloud Hypervisor starts the VM. Only a boot reads the kernel.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum Start {
    /// A cold boot. PR C7 makes the initramfs optional until phase 3, and
    /// the CI boot of PR C16 uses one, so the rules allow it too.
    Boot {
        kernel: HostPath,
        initramfs: Option<HostPath>,
    },
    /// A restore from a snapshot under the snapshot directory.
    Restore,
}

/// The files of one VM that its ruleset names. Each one exists before the
/// launch: the open of a missing path fails the launch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VmFiles {
    start: Start,
    root_disk: HostPath,
    session_disk: HostPath,
    snapshots: HostPath,
    run_dir: HostPath,
}

impl VmFiles {
    /// The files of the VM whose state is in `session` and whose sockets
    /// are in `run`. PR C7 takes the checked image types of the store for
    /// the root disk and the kernel.
    ///
    /// # Errors
    ///
    /// `NotAbsolute` if a path of `session` or `run` is relative. The
    /// store roots are absolute, so this does not happen.
    pub fn new(
        start: Start,
        root_disk: HostPath,
        session: &SessionDir,
        run: &VmRunDir,
    ) -> Result<Self, NotAbsolute> {
        Ok(Self {
            start,
            root_disk,
            session_disk: HostPath::new(session.disk())?,
            snapshots: HostPath::new(session.snapshots())?,
            run_dir: HostPath::new(run.path().to_path_buf())?,
        })
    }

    /// How CH starts the VM.
    #[must_use]
    pub fn start(&self) -> &Start {
        &self.start
    }

    /// The shared root disk, read only (decision CH8-8).
    #[must_use]
    pub fn root_disk(&self) -> &HostPath {
        &self.root_disk
    }

    /// The session disk.
    #[must_use]
    pub fn session_disk(&self) -> &HostPath {
        &self.session_disk
    }

    /// The directory of every snapshot generation of the session.
    #[must_use]
    pub fn snapshots(&self) -> &HostPath {
        &self.snapshots
    }

    /// The run directory of the VM.
    #[must_use]
    pub fn run_dir(&self) -> &HostPath {
        &self.run_dir
    }
}

/// One Landlock access right that a rule grants. The set has no right to
/// remove a file, make a directory or link a file elsewhere.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Right {
    Execute,
    ReadFile,
    WriteFile,
    Truncate,
    MakeReg,
    MakeSock,
}

/// Run a program: CH, `systemd-run` and the loader.
const PROGRAM: &[Right] = &[Right::Execute, Right::ReadFile];
/// Read only: the kernel, the libraries and the root disk (decision CH8-8).
const READ: &[Right] = &[Right::ReadFile];
/// Read and write an existing file: `/dev/kvm` and the session disk.
const READ_WRITE: &[Right] = &[Right::ReadFile, Right::WriteFile];
/// The run directory: the sockets and `serial.log` (gap G23).
const RUN_DIR: &[Right] = &[
    Right::MakeSock,
    Right::MakeReg,
    Right::WriteFile,
    Right::Truncate,
];
/// The snapshot directory: write a snapshot, read one on restore.
const SNAPSHOTS: &[Right] = &[Right::MakeReg, Right::WriteFile, Right::ReadFile];

/// One rule: `rights` on `path` and, for a directory, on all beneath it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rule {
    pub path: PathBuf,
    pub rights: &'static [Right],
}

fn rule(path: impl Into<PathBuf>, rights: &'static [Right]) -> Rule {
    Rule {
        path: path.into(),
        rights,
    }
}

/// The rules of the VM of `files`, whose launch runs `systemd_run` and
/// then `payload` (fact 77, decision CH30).
#[must_use]
pub fn rules(files: &VmFiles, systemd_run: &Program, payload: &Program) -> Vec<Rule> {
    let mut rules = vec![
        rule(payload.path(), PROGRAM),
        rule(systemd_run.path(), PROGRAM),
        rule(LOADER, PROGRAM),
        rule(LIBRARY_DIR, READ),
        rule(KVM, READ_WRITE),
        rule(URANDOM, READ),
    ];
    if let Start::Boot { kernel, initramfs } = &files.start {
        rules.push(rule(kernel.path(), READ));
        rules.extend(initramfs.iter().map(|image| rule(image.path(), READ)));
    }
    rules.extend([
        rule(files.root_disk.path(), READ),
        rule(files.session_disk.path(), READ_WRITE),
        rule(files.run_dir.path(), RUN_DIR),
        rule(files.snapshots.path(), SNAPSHOTS),
    ]);
    rules
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use creche_vmm_store::layout::{RunRoot, StateRoot};

    use super::{HostPath, Right, Rule, Start, VmFiles};
    use crate::launch::Program;

    const ID: &str = "0123abcd-0000-4000-8000-00000000c6c6";
    const CH: &str = "/opt/components/vmm/bin/cloud-hypervisor";

    fn host(path: &str) -> HostPath {
        HostPath::new(PathBuf::from(path)).unwrap()
    }

    /// The rules of a launch of CH through `/usr/bin/systemd-run`.
    fn rules(start: Start) -> Vec<Rule> {
        let id = ID.parse().unwrap();
        let session = StateRoot::new(PathBuf::from("/state"))
            .unwrap()
            .session(&id);
        let run = RunRoot::new(PathBuf::from("/run/vmm")).unwrap().vm(&id);
        let files = VmFiles::new(start, host("/state/store/root"), &session, &run).unwrap();
        let payload = Program::new(PathBuf::from(CH)).unwrap();
        let systemd_run = Program::new(PathBuf::from("/usr/bin/systemd-run")).unwrap();
        super::rules(&files, &systemd_run, &payload)
    }

    fn table(rules: &[Rule]) -> Vec<(String, &'static [Right])> {
        let path = |rule: &Rule| rule.path.to_str().unwrap().to_owned();
        rules.iter().map(|rule| (path(rule), rule.rights)).collect()
    }

    #[test]
    fn a_boot_reads_its_kernel_and_initramfs() {
        use Right::{Execute, MakeReg, MakeSock, ReadFile, Truncate, WriteFile};
        let start = Start::Boot {
            kernel: host("/state/store/kernel"),
            initramfs: Some(host("/state/store/initramfs")),
        };
        let session = format!("/state/sessions/{ID}");
        let expected: Vec<(String, &[Right])> = vec![
            (CH.to_owned(), &[Execute, ReadFile]),
            ("/usr/bin/systemd-run".to_owned(), &[Execute, ReadFile]),
            (
                "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2".to_owned(),
                &[Execute, ReadFile],
            ),
            ("/usr/lib/x86_64-linux-gnu".to_owned(), &[ReadFile]),
            ("/dev/kvm".to_owned(), &[ReadFile, WriteFile]),
            ("/dev/urandom".to_owned(), &[ReadFile]),
            ("/state/store/kernel".to_owned(), &[ReadFile]),
            ("/state/store/initramfs".to_owned(), &[ReadFile]),
            ("/state/store/root".to_owned(), &[ReadFile]),
            (format!("{session}/disk.ext4"), &[ReadFile, WriteFile]),
            (
                format!("/run/vmm/vm/{ID}"),
                &[MakeSock, MakeReg, WriteFile, Truncate],
            ),
            (
                format!("{session}/snapshots"),
                &[MakeReg, WriteFile, ReadFile],
            ),
        ];
        assert_eq!(table(&rules(start)), expected);
    }

    #[test]
    fn a_restore_reads_no_kernel() {
        let boot = Start::Boot {
            kernel: host("/state/store/kernel"),
            initramfs: None,
        };
        let mut boot = table(&rules(boot));
        boot.retain(|(path, _)| path != "/state/store/kernel");
        assert_eq!(table(&rules(Start::Restore)), boot);
    }

    #[test]
    fn the_root_disk_is_read_only() {
        let rules = rules(Start::Restore);
        let root = rules.iter().find(|rule| rule.path.ends_with("root"));
        assert_eq!(root.map(|rule| rule.rights), Some(&[Right::ReadFile][..]));
    }

    #[test]
    fn a_start_is_snake_case_json() {
        let json = r#"{"boot":{"kernel":"/k","initramfs":null}}"#;
        let start: Start = serde_json::from_str(json).unwrap();
        assert_eq!(serde_json::to_string(&start).unwrap(), json);
        assert!(
            serde_json::from_str::<Start>(r#"{"boot":{"kernel":"k","initramfs":null}}"#).is_err()
        );
        assert_eq!(
            serde_json::to_string(&Start::Restore).unwrap(),
            r#""restore""#
        );
    }
}
