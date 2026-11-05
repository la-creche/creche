//! The launch step `vmm launch`. The spawner thread runs the vmm program
//! again with this mode, and the child starts one Cloud Hypervisor process
//! in its own user scope (decision CH8-2).
//!
//! The step reads its plan as JSON on fd 0. It then sets
//! `PR_SET_PDEATHSIG` to SIGKILL, checks that its parent is still the
//! spawner, sets `RLIMIT_CORE` to 0 and execs `systemd-run` (decisions
//! CH19 (4), CH8-5, CH36 G3 and G27). The payload of the scope is the
//! launch stage of `stage`, which enters the slot label, applies the
//! Landlock ruleset and execs CH (decision CH55, which amends CH30).

use std::convert::Infallible;
use std::ffi::OsString;
use std::fmt;
use std::fs::File;
use std::io::{self, Read};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use creche_vmm_store::layout::Slot;
use serde::{Deserialize, Serialize};

use crate::landlock::VmFiles;
use crate::scope::Scope;
use crate::spawner::SELF_EXE;
use crate::stage::{SlotNumber, Stage};

/// The argument of the launch mode. `rust/crates/vmm/src/main.rs` parses
/// the same word.
pub const MODE: &str = "launch";

/// The stdin of `systemd-run` and the payload.
const NULL: &str = "/dev/null";

/// An absolute path of a program to exec. `systemd-run` and the Landlock
/// rules need the full path, never a search of `PATH`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "PathBuf", into = "PathBuf")]
pub struct Program(PathBuf);

/// Why a path is not a program path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NotAbsolute;

impl fmt::Display for NotAbsolute {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a program path is absolute")
    }
}

impl std::error::Error for NotAbsolute {}

impl Program {
    /// Checks `path` and makes the program path.
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

impl TryFrom<PathBuf> for Program {
    type Error = NotAbsolute;

    fn try_from(path: PathBuf) -> Result<Self, Self::Error> {
        Self::new(path)
    }
}

impl From<Program> for PathBuf {
    fn from(program: Program) -> Self {
        program.0
    }
}

/// The program that runs in the scope and its arguments. PR C7 renders
/// the Cloud Hypervisor command line into one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Payload {
    program: Program,
    args: Vec<String>,
}

impl Payload {
    /// The payload `program` with `args`.
    #[must_use]
    pub fn new(program: Program, args: Vec<String>) -> Self {
        Self { program, args }
    }

    /// The program.
    #[must_use]
    pub fn program(&self) -> &Program {
        &self.program
    }

    /// The arguments after the program.
    #[must_use]
    pub fn args(&self) -> &[String] {
        &self.args
    }
}

/// What one launch starts: the payload in its scope, through the
/// `systemd-run` at a path that the vmm config gives, under the slot label
/// of `slot` and the Landlock ruleset of `files` (decision CH55).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Launch {
    systemd_run: Program,
    scope: Scope,
    payload: Payload,
    files: VmFiles,
    slot: Option<SlotNumber>,
}

impl Launch {
    /// The launch of `payload` in `scope` through `systemd_run`, with
    /// access to `files` only. It has no slot yet, and the launch step
    /// refuses it with `LaunchError::NoSlot` until `in_slot` gives one.
    #[must_use]
    pub fn new(systemd_run: Program, scope: Scope, payload: Payload, files: VmFiles) -> Self {
        Self {
            systemd_run,
            scope,
            payload,
            files,
            slot: None,
        }
    }

    /// The same launch in `slot`, whose label the payload runs under
    /// (decision CH55). Lifecycle passes the slot that `vm/<id>` links to.
    #[must_use]
    pub fn in_slot(self, slot: Slot) -> Self {
        Self {
            slot: Some(SlotNumber(slot)),
            ..self
        }
    }

    /// The slot, if `in_slot` gave one.
    #[must_use]
    pub fn slot(&self) -> Option<Slot> {
        self.slot.map(|slot| slot.0)
    }

    /// The `systemd-run` program.
    #[must_use]
    pub fn systemd_run(&self) -> &Program {
        &self.systemd_run
    }

    /// The scope of the launch.
    #[must_use]
    pub fn scope(&self) -> &Scope {
        &self.scope
    }

    /// The payload.
    #[must_use]
    pub fn payload(&self) -> &Payload {
        &self.payload
    }

    /// The files of the VM that the ruleset names.
    #[must_use]
    pub fn files(&self) -> &VmFiles {
        &self.files
    }
}

/// The most bytes in a plan: the default pipe capacity of Linux. It is an
/// upper bound, not a guarantee. Linux gives a new pipe only 8 KiB when an
/// unprivileged user is over `fs.pipe-user-pages-soft`. So the spawner
/// writes the whole plan without blocking, before the spawn, and a plan
/// that does not fit fails with `SpawnError::TooLarge`.
pub(crate) const PLAN_MAX: usize = 65_536;

/// The plan that the spawner writes on fd 0 of the launch step.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Plan {
    /// The pid of the vmm. The step checks it against its parent.
    pub(crate) spawner: u32,
    pub(crate) launch: Launch,
}

/// Why the launch step did not start the payload.
#[derive(Debug)]
pub enum LaunchError {
    /// The host is not Linux, which has `PR_SET_PDEATHSIG`.
    NotLinux,
    /// Fd 0 is not a pipe, so no spawner wrote a plan there. A terminal
    /// on fd 0 would block the read.
    NotPiped,
    /// The pipe on fd 0 is empty. The spawner writes the whole plan before
    /// the spawn, so an empty pipe is not from the spawner, and a read of
    /// it could block forever.
    NoPlan,
    /// The plan did not arrive.
    Read(io::Error),
    /// The plan is not the JSON of a `Plan`.
    NotPlan(serde_json::Error),
    /// The kernel refused `PR_SET_PDEATHSIG`.
    DeathSignal(io::Error),
    /// The parent is not the spawner, so the vmm exited before the death
    /// signal was set.
    SpawnerGone,
    /// The kernel refused `RLIMIT_CORE` 0.
    CoreLimit(io::Error),
    /// The launch has no slot, so the payload would run under no slot
    /// label (decision CH55).
    NoSlot,
    /// The path of the running vmm binary, the program of the launch
    /// stage, did not read, or its file is deleted.
    SelfExe(io::Error),
    /// The stage plan did not serialize.
    StagePlan(serde_json::Error),
    /// The open of `/dev/null`, the stdin of `systemd-run`, failed.
    NullInput(io::Error),
    /// The exec of `systemd-run` failed.
    Exec(io::Error),
}

impl fmt::Display for LaunchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotLinux => formatter.write_str("the launch step runs only on Linux"),
            Self::NotPiped => formatter.write_str("the launch step reads its plan from a pipe"),
            Self::NoPlan => formatter.write_str("the pipe on fd 0 holds no launch plan"),
            Self::Read(error) => write!(formatter, "reading the launch plan: {error}"),
            Self::NotPlan(error) => write!(formatter, "the launch plan is not valid: {error}"),
            Self::DeathSignal(error) => write!(formatter, "setting the death signal: {error}"),
            Self::SpawnerGone => formatter.write_str("the vmm exited before the launch"),
            Self::CoreLimit(error) => write!(formatter, "setting the core limit: {error}"),
            Self::NoSlot => formatter.write_str("the launch has no slot"),
            Self::SelfExe(error) => write!(formatter, "reading {SELF_EXE}: {error}"),
            Self::StagePlan(error) => write!(formatter, "serializing the stage plan: {error}"),
            Self::NullInput(error) => write!(formatter, "opening {NULL}: {error}"),
            Self::Exec(error) => write!(formatter, "running systemd-run: {error}"),
        }
    }
}

impl std::error::Error for LaunchError {}

/// Runs the launch step. A launch that works replaces this process with
/// `systemd-run`, so this function returns only an error.
///
/// # Errors
///
/// Each step that fails gives its `LaunchError`.
pub fn run() -> Result<Infallible, LaunchError> {
    if !cfg!(target_os = "linux") {
        return Err(LaunchError::NotLinux);
    }
    let plan = read_plan()?;
    // The death signal comes first. The parent check after it then closes
    // the race with a vmm that exited before the signal was set.
    set_death_signal()?;
    let parent = rustix::process::Pid::as_raw(rustix::process::getppid());
    if u32::try_from(parent) != Ok(plan.spawner) {
        return Err(LaunchError::SpawnerGone);
    }
    let no_core = rustix::process::Rlimit {
        current: Some(0),
        maximum: Some(0),
    };
    rustix::process::setrlimit(rustix::process::Resource::Core, no_core)
        .map_err(|errno| LaunchError::CoreLimit(errno.into()))?;

    let null = File::open(NULL).map_err(LaunchError::NullInput)?;
    let args = systemd_run_args(&plan.launch, stage_program()?)?;

    // Fds 1 and 2 stay the output pipe of the spawner, so `systemd-run`,
    // the stage and CH write their lines there (decision CH50 rule 12).
    let error = Command::new(plan.launch.systemd_run.path())
        .args(args)
        .stdin(null)
        .exec();
    Err(LaunchError::Exec(error))
}

/// The arguments of `systemd-run` for `launch`. The scope payload is
/// `stage` in the stage mode with the stage plan (decision CH55).
pub(crate) fn systemd_run_args(
    launch: &Launch,
    stage: Program,
) -> Result<Vec<OsString>, LaunchError> {
    let slot = launch.slot().ok_or(LaunchError::NoSlot)?;
    let plan = Stage::new(slot, launch.payload.clone(), launch.files.clone());
    let payload = plan.command(stage).map_err(LaunchError::StagePlan)?;
    Ok(launch.scope.systemd_run_args(&payload))
}

/// The path of the running vmm binary. The launch step runs it through
/// `SELF_EXE`, but `systemd-run` would resolve `SELF_EXE` to itself, so
/// the stage gets the resolved path. A release renames the old file to
/// `.prev` (fact 56), and the link then names `.prev`, the binary of this
/// plan. A deleted file fails the launch closed.
///
/// CONTRACT-QUESTION (decision CH55): no row says where the program of
/// the launch stage comes from. This is the narrowest reading: the same
/// binary, with no new key in the vmm config.
fn stage_program() -> Result<Program, LaunchError> {
    let path = std::fs::read_link(SELF_EXE).map_err(LaunchError::SelfExe)?;
    if path.as_os_str().as_encoded_bytes().ends_with(b" (deleted)") {
        return Err(LaunchError::SelfExe(io::ErrorKind::NotFound.into()));
    }
    Program::new(path).map_err(|error| LaunchError::SelfExe(io::Error::other(error)))
}

/// Reads the whole plan from fd 0. The spawner closes its end of the pipe
/// before the spawn, so the read ends.
fn read_plan() -> Result<Plan, LaunchError> {
    let mut stdin = io::stdin().lock();
    let stat = rustix::fs::fstat(&stdin).map_err(|errno| LaunchError::Read(errno.into()))?;
    if rustix::fs::FileType::from_raw_mode(stat.st_mode) != rustix::fs::FileType::Fifo {
        return Err(LaunchError::NotPiped);
    }
    let queued =
        rustix::io::ioctl_fionread(&stdin).map_err(|errno| LaunchError::Read(errno.into()))?;
    if queued == 0 {
        return Err(LaunchError::NoPlan);
    }
    let mut bytes = Vec::new();
    stdin.read_to_end(&mut bytes).map_err(LaunchError::Read)?;
    serde_json::from_slice(&bytes).map_err(LaunchError::NotPlan)
}

/// Sets SIGKILL as the death signal of this process (decision CH19 (4)).
/// The signal stays through the execs of `systemd-run`, the stage and CH
/// (facts 76 and 80). The `stage` module says why.
#[cfg(target_os = "linux")]
fn set_death_signal() -> Result<(), LaunchError> {
    let kill = Some(rustix::process::Signal::KILL);
    rustix::process::set_parent_process_death_signal(kill)
        .map_err(|errno| LaunchError::DeathSignal(errno.into()))
}

/// No other host has `PR_SET_PDEATHSIG`.
#[cfg(not(target_os = "linux"))]
fn set_death_signal() -> Result<(), LaunchError> {
    Err(LaunchError::NotLinux)
}

#[cfg(test)]
mod tests {
    use std::num::{NonZeroU32, NonZeroU64};
    use std::path::PathBuf;

    use creche_vmm_store::layout::{RunRoot, Slot, StateRoot};

    use super::{Launch, LaunchError, NotAbsolute, Payload, Program};
    use crate::landlock::{HostPath, Start, VmFiles};
    use crate::scope::Scope;

    const ID: &str = "0123abcd-0000-4000-8000-00000000c18a";
    const VMM: &str = "/opt/components/vmm/bin/vmm";

    /// A restore of CH through `/usr/bin/systemd-run`, with no slot.
    fn launch() -> Launch {
        let id = ID.parse().unwrap();
        let session = StateRoot::new(PathBuf::from("/state"))
            .unwrap()
            .session(&id);
        let run = RunRoot::new(PathBuf::from("/run/vmm")).unwrap().vm(&id);
        let root = HostPath::new(PathBuf::from("/state/store/root")).unwrap();
        let files = VmFiles::new(Start::Restore, root, &session, &run).unwrap();
        let ch = Program::new(PathBuf::from("/opt/components/vmm/bin/cloud-hypervisor")).unwrap();
        let payload = Payload::new(ch, vec!["--restore".to_owned()]);
        let scope = Scope::new(
            id,
            NonZeroU64::new(1 << 30).unwrap(),
            NonZeroU32::new(100).unwrap(),
        );
        let systemd_run = Program::new(PathBuf::from("/usr/bin/systemd-run")).unwrap();
        Launch::new(systemd_run, scope, payload, files)
    }

    fn vmm() -> Program {
        Program::new(PathBuf::from(VMM)).unwrap()
    }

    #[test]
    fn the_scope_payload_is_the_stage_with_its_plan() {
        let launch = launch().in_slot(Slot::new(7).unwrap());
        let args = super::systemd_run_args(&launch, vmm()).unwrap();
        let split = args.iter().position(|arg| arg == "--").unwrap();
        let (scope, payload) = args.split_at(split);
        let limits = launch
            .scope()
            .systemd_run_args(&Payload::new(vmm(), Vec::new()));
        assert_eq!(scope, &limits[..split]);
        let [_, program, mode, plan] = payload else {
            panic!("{payload:?}");
        };
        assert_eq!(
            (program.as_os_str(), mode.as_os_str()),
            (VMM.as_ref(), "launch-stage".as_ref())
        );
        let plan: serde_json::Value = serde_json::from_str(plan.to_str().unwrap()).unwrap();
        assert_eq!(plan["slot"], 7);
        assert_eq!(
            plan["payload"],
            serde_json::to_value(launch.payload()).unwrap()
        );
        assert_eq!(plan["files"], serde_json::to_value(launch.files()).unwrap());
    }

    #[test]
    fn a_launch_with_no_slot_is_refused() {
        let error = super::systemd_run_args(&launch(), vmm());
        assert!(matches!(error, Err(LaunchError::NoSlot)), "{error:?}");
        assert_eq!(launch().slot(), None);
        let json = serde_json::to_value(launch()).unwrap();
        assert_eq!(json["slot"], serde_json::Value::Null);
    }

    #[test]
    fn a_program_path_is_absolute() {
        assert_eq!(Program::new(PathBuf::from("sleep")), Err(NotAbsolute));
        assert!(Program::new(PathBuf::from("/usr/bin/sleep")).is_ok());
    }

    #[test]
    fn a_payload_with_a_relative_program_is_refused() {
        let json = r#"{"program":"sleep","args":["60"]}"#;
        assert!(serde_json::from_str::<Payload>(json).is_err());
        let json = r#"{"program":"/usr/bin/sleep","args":["60"]}"#;
        let payload: Payload = serde_json::from_str(json).unwrap();
        assert_eq!(payload.args(), ["60"]);
    }

    /// `systemd-run` would resolve `SELF_EXE` to itself, so the stage
    /// program is the resolved binary (decision CH55).
    #[cfg(target_os = "linux")]
    #[test]
    fn the_stage_program_is_the_resolved_binary() {
        let program = super::stage_program().unwrap();
        assert_eq!(program.path(), std::env::current_exe().unwrap());
        assert_ne!(program.path(), std::path::Path::new(super::SELF_EXE));
    }

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn run_refuses_off_linux() {
        assert!(matches!(super::run(), Err(super::LaunchError::NotLinux)));
    }
}
