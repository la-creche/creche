//! The launch step `vmm launch`. The spawner thread runs the vmm program
//! again with this mode, and the child starts one Cloud Hypervisor process
//! in its own user scope (decision CH8-2).
//!
//! The step reads its plan as JSON on fd 0. It then sets
//! `PR_SET_PDEATHSIG` to SIGKILL, checks that its parent is still the
//! spawner, sets `RLIMIT_CORE` to 0 and execs `systemd-run` (decisions
//! CH19 (4), CH8-5, CH36 G3 and G27). PR C6 adds the Landlock step before
//! the exec (decision CH30).

use std::convert::Infallible;
use std::fmt;
use std::io::{self, Read};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde::{Deserialize, Serialize};

use crate::scope::Scope;

/// The argument of the launch mode. `rust/crates/vmm/src/main.rs` parses
/// the same word.
pub const MODE: &str = "launch";

/// An absolute path of a program to exec. `systemd-run` and the Landlock
/// rules of PR C6 need the full path, never a search of `PATH`.
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
/// `systemd-run` at a path that the vmm config gives.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Launch {
    systemd_run: Program,
    scope: Scope,
    payload: Payload,
}

impl Launch {
    /// The launch of `payload` in `scope` through `systemd_run`.
    #[must_use]
    pub fn new(systemd_run: Program, scope: Scope, payload: Payload) -> Self {
        Self {
            systemd_run,
            scope,
            payload,
        }
    }

    /// The scope of the launch.
    #[must_use]
    pub fn scope(&self) -> &Scope {
        &self.scope
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

    let launch = plan.launch;
    let error = Command::new(launch.systemd_run.path())
        .args(launch.scope.systemd_run_args(&launch.payload))
        .stdin(Stdio::null())
        .exec();
    Err(LaunchError::Exec(error))
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
/// The signal stays through the exec of `systemd-run` and of the payload
/// (fact 76).
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
    use std::path::PathBuf;

    use super::{NotAbsolute, Payload, Program};

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

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn run_refuses_off_linux() {
        assert!(matches!(super::run(), Err(super::LaunchError::NotLinux)));
    }
}
