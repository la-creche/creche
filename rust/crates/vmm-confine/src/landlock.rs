//! The Landlock ruleset of one VM (decisions CH8-3 and CH30).
//!
//! The launch stage applies it after the slot label and no_new_privs and
//! before the exec of CH (decision CH55, which amends CH30). So only the
//! Cloud Hypervisor process runs under it, on boot and on restore, with
//! every CH thread inside it (fact 64). `systemd-run` runs before it and
//! needs no rule. The vmm itself never runs under it, so the `zstd` and
//! `mke2fs` that the vmm runs need no rule. The rules are the smallest set of fact
//! 77. The ruleset handles every file right and TCP bind and connect of
//! Landlock ABI 4, and no rule allows a TCP port (decision CH8-1). A
//! kernel below ABI 4 fails the launch.
//!
//! Decision CH59 rule 2 moves `serial.log` from the run dir of fact 77 to
//! `sessions/<id>/serial.log` in the state root. So that one file gets a
//! rule with write and truncate, and the run dir rule drops truncate,
//! which only the log used.
//!
//! Landlock ABI 4 covers neither UDP nor Unix sockets. The slot profile
//! of decisions CH8-1 and CH55 (`slot_profile`) denies those.

use std::fmt;
use std::io;
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

/// How Cloud Hypervisor starts the VM. Only a boot reads the kernel. A
/// boot and a restore both read the initramfs (decision CH61 rule 3).
///
/// A boot has no initramfs until phase 3 adds guest init (PR C7), and
/// the kvm job boots with one, so each variant holds an `Option`. The
/// snapshot of a boot with none has none, and its restore carries none.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum Start {
    /// A cold boot.
    Boot {
        kernel: HostPath,
        initramfs: Option<HostPath>,
    },
    /// A restore from a snapshot under the snapshot directory. CH opens
    /// the initramfs that the snapshot config names, so the restore
    /// carries the `store/<sha256>` initramfs of its snapshot (decisions
    /// CH60 rule 2, CH61 rule 3). The snapshot key holds that digest, so
    /// a snapshot with the current key names the initramfs of the vmm
    /// config, and the vmm passes that checked store copy.
    Restore { initramfs: Option<HostPath> },
}

/// The files of one VM that its ruleset names. Each one exists before the
/// launch: the open of a missing path fails the launch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VmFiles {
    start: Start,
    root_disk: HostPath,
    session_disk: HostPath,
    serial_log: HostPath,
    snapshots: HostPath,
    run_dir: HostPath,
}

impl VmFiles {
    /// The files of the VM whose state is in `session` and whose sockets
    /// are in `run`. Lifecycle passes the paths of the checked store
    /// copies that `command::Boot` renders, so that the rules and the argv
    /// name the same files.
    ///
    /// Plan deviation of PR C7: C6 said that C7 makes this take the
    /// `CheckedImage` of the root disk and the kernel. C7 does not. The
    /// plan of C7 names only `command.rs` and its test. `VmFiles` is serde
    /// data that the launch child reads, and `CheckedImage` is not, so the
    /// child keeps a `HostPath`. A follow-up PR may add a constructor on
    /// `&CheckedImage` in the parent. Until then a mismatch fails closed:
    /// the ruleset denies the file that the argv names, and CH exits.
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
            serial_log: HostPath::new(session.serial_log())?,
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

    /// The serial log of the session (gap G23, decision CH59 rule 2).
    #[must_use]
    pub fn serial_log(&self) -> &HostPath {
        &self.serial_log
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

/// Run a program: CH and the loader.
const PROGRAM: &[Right] = &[Right::Execute, Right::ReadFile];
/// Read only: the kernel, the libraries and the root disk (decision CH8-8).
const READ: &[Right] = &[Right::ReadFile];
/// Read and write an existing file: `/dev/kvm` and the session disk.
const READ_WRITE: &[Right] = &[Right::ReadFile, Right::WriteFile];
/// The run directory: the sockets and the lock file of the API socket.
/// CH v54 makes the lock with no truncate
/// (`vmm/src/locked_unix_listener.rs`), so the dir needs no truncate.
const RUN_DIR: &[Right] = &[Right::MakeSock, Right::MakeReg, Right::WriteFile];
/// The serial log in the state root (gap G23, decision CH59 rule 2). The
/// vmm makes it before the launch, and CH opens it with `File::create`
/// (`vmm/src/console_devices.rs`), a write open that truncates. CH never
/// reads it.
const SERIAL_LOG: &[Right] = &[Right::WriteFile, Right::Truncate];
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

/// The rules of the VM of `files`, whose launch stage execs `payload`
/// (fact 77, decisions CH30 and CH55).
#[must_use]
pub fn rules(files: &VmFiles, payload: &Program) -> Vec<Rule> {
    let mut rules = vec![
        rule(payload.path(), PROGRAM),
        rule(LOADER, PROGRAM),
        rule(LIBRARY_DIR, READ),
        rule(KVM, READ_WRITE),
        rule(URANDOM, READ),
    ];
    let initramfs = match &files.start {
        Start::Boot { kernel, initramfs } => {
            rules.push(rule(kernel.path(), READ));
            initramfs
        }
        Start::Restore { initramfs } => initramfs,
    };
    rules.extend(initramfs.iter().map(|image| rule(image.path(), READ)));
    rules.extend([
        rule(files.root_disk.path(), READ),
        rule(files.session_disk.path(), READ_WRITE),
        rule(files.serial_log.path(), SERIAL_LOG),
        rule(files.run_dir.path(), RUN_DIR),
        rule(files.snapshots.path(), SNAPSHOTS),
    ]);
    rules
}

/// Why the ruleset is not in force.
#[derive(Debug)]
pub enum RulesetError {
    /// A path of a rule did not open. The value is the path.
    Open(PathBuf, io::Error),
    /// The landlock crate refused the ruleset, or the kernel has no
    /// Landlock ABI 4.
    #[cfg(target_os = "linux")]
    Landlock(::landlock::RulesetError),
    /// The kernel enforced less than the whole ruleset.
    NotEnforced,
}

impl fmt::Display for RulesetError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Open(path, error) => write!(formatter, "opening {}: {error}", path.display()),
            #[cfg(target_os = "linux")]
            Self::Landlock(error) => write!(formatter, "{error}"),
            Self::NotEnforced => formatter.write_str("the kernel did not enforce every rule"),
        }
    }
}

impl std::error::Error for RulesetError {}

/// Restricts this thread and every program it execs to `rules`. A kernel
/// below Landlock ABI 4 is an error, never a weaker ruleset.
///
/// # Errors
///
/// The `RulesetError` of the step that failed.
#[cfg(target_os = "linux")]
pub(crate) fn restrict(rules: &[Rule]) -> Result<(), RulesetError> {
    use ::landlock::{
        ABI, Access, AccessFs, AccessNet, CompatLevel, Compatible, PathBeneath, Ruleset,
        RulesetAttr, RulesetCreatedAttr, RulesetStatus,
    };
    use rustix::fs::{Mode, OFlags};

    const ABI_4: ABI = ABI::V4;
    let mut ruleset = Ruleset::default()
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(AccessFs::from_all(ABI_4))
        .and_then(|ruleset| ruleset.handle_access(AccessNet::from_all(ABI_4)))
        .and_then(Ruleset::create)
        .map_err(RulesetError::Landlock)?;
    for rule in rules {
        let flags = OFlags::PATH | OFlags::CLOEXEC;
        let fd = rustix::fs::open(&rule.path, flags, Mode::empty())
            .map_err(|errno| RulesetError::Open(rule.path.clone(), errno.into()))?;
        let access =
            rule.rights
                .iter()
                .fold(::landlock::BitFlags::<AccessFs>::EMPTY, |all, right| {
                    all | match right {
                        Right::Execute => AccessFs::Execute,
                        Right::ReadFile => AccessFs::ReadFile,
                        Right::WriteFile => AccessFs::WriteFile,
                        Right::Truncate => AccessFs::Truncate,
                        Right::MakeReg => AccessFs::MakeReg,
                        Right::MakeSock => AccessFs::MakeSock,
                    }
                });
        ruleset = ruleset
            .add_rule(PathBeneath::new(fd, access))
            .map_err(RulesetError::Landlock)?;
    }
    let status = ruleset.restrict_self().map_err(RulesetError::Landlock)?;
    if status.ruleset == RulesetStatus::FullyEnforced && status.no_new_privs {
        Ok(())
    } else {
        Err(RulesetError::NotEnforced)
    }
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

    /// The rules of a launch of CH.
    fn rules(start: Start) -> Vec<Rule> {
        let id = ID.parse().unwrap();
        let session = StateRoot::new(PathBuf::from("/state"))
            .unwrap()
            .session(&id);
        let run = RunRoot::new(PathBuf::from("/run/vmm")).unwrap().vm(&id);
        let files = VmFiles::new(start, host("/state/store/root"), &session, &run).unwrap();
        let payload = Program::new(PathBuf::from(CH)).unwrap();
        super::rules(&files, &payload)
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
            (format!("{session}/serial.log"), &[WriteFile, Truncate]),
            (format!("/run/vmm/vm/{ID}"), &[MakeSock, MakeReg, WriteFile]),
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
        let restore = Start::Restore { initramfs: None };
        assert_eq!(table(&rules(restore)), boot);
    }

    /// A restore reads the initramfs of its snapshot, as a boot reads
    /// its own (decision CH61 rule 3), and no kernel.
    #[test]
    fn a_restore_reads_the_initramfs_of_its_snapshot() {
        let boot = Start::Boot {
            kernel: host("/state/store/kernel"),
            initramfs: Some(host("/state/store/initramfs")),
        };
        let mut boot = table(&rules(boot));
        boot.retain(|(path, _)| path != "/state/store/kernel");
        let restore = Start::Restore {
            initramfs: Some(host("/state/store/initramfs")),
        };
        let rules = rules(restore);
        assert_eq!(table(&rules), boot);
        let initramfs = rules
            .iter()
            .find(|rule| rule.path.ends_with("initramfs"))
            .map(|rule| rule.rights);
        assert_eq!(initramfs, Some(&[Right::ReadFile][..]));
    }

    #[test]
    fn the_root_disk_is_read_only() {
        let rules = rules(Start::Restore { initramfs: None });
        let root = rules.iter().find(|rule| rule.path.ends_with("root"));
        assert_eq!(root.map(|rule| rule.rights), Some(&[Right::ReadFile][..]));
    }

    /// The ruleset denies a TCP connect (decision CH8-1). This test is the
    /// proof of the TCP rule. Under the chain, the slot profile refuses
    /// the inet socket first, so no kvm_ test reaches the rule. Landlock
    /// confines only the thread that calls `restrict`, so the ruleset runs
    /// on a new thread. The control: a second new thread with no ruleset
    /// connects to the same listener.
    #[cfg(target_os = "linux")]
    #[test]
    fn the_ruleset_denies_a_tcp_connect() {
        use std::fs;
        use std::io::ErrorKind;
        use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
        use std::thread;

        let root = PathBuf::from(format!("/tmp/vmm-confine-{}-tcp", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let id = ID.parse().unwrap();
        let session = StateRoot::new(root.clone()).unwrap().session(&id);
        let run = RunRoot::new(root.clone()).unwrap().vm(&id);
        fs::create_dir_all(session.snapshots()).unwrap();
        fs::create_dir_all(run.path()).unwrap();
        fs::write(root.join("root.img"), "root").unwrap();
        fs::write(session.disk(), "session").unwrap();
        fs::write(session.serial_log(), "").unwrap();
        let root_disk = HostPath::new(root.join("root.img")).unwrap();
        let start = Start::Restore { initramfs: None };
        let files = VmFiles::new(start, root_disk, &session, &run).unwrap();
        let payload = Program::new(std::env::current_exe().unwrap()).unwrap();
        let rules = super::rules(&files, &payload);

        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let connect = |address: SocketAddr| {
            TcpStream::connect(address)
                .map(drop)
                .map_err(|error| error.kind())
        };
        let confined = thread::spawn(move || {
            super::restrict(&rules).unwrap();
            connect(address)
        })
        .join()
        .unwrap();
        let control = thread::spawn(move || connect(address)).join().unwrap();
        fs::remove_dir_all(&root).unwrap();
        assert_eq!(confined, Err(ErrorKind::PermissionDenied));
        assert_eq!(control, Ok(()));
    }

    #[test]
    fn a_start_is_snake_case_json() {
        let json = r#"{"boot":{"kernel":"/k","initramfs":null}}"#;
        let start: Start = serde_json::from_str(json).unwrap();
        assert_eq!(serde_json::to_string(&start).unwrap(), json);
        assert!(
            serde_json::from_str::<Start>(r#"{"boot":{"kernel":"k","initramfs":null}}"#).is_err()
        );
        let json = r#"{"restore":{"initramfs":"/i"}}"#;
        let start: Start = serde_json::from_str(json).unwrap();
        assert_eq!(serde_json::to_string(&start).unwrap(), json);
        assert!(serde_json::from_str::<Start>(r#""restore""#).is_err());
    }
}
