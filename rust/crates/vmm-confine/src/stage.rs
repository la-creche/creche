//! The launch stage `vmm launch-stage` (mechanism X, decision CH55, which
//! amends CH30). The launch step execs `systemd-run --scope`, and
//! `systemd-run` execs this stage as the payload of the VM scope. The
//! stage then runs four steps, in this order:
//!
//! 1. It writes `exec creche-ch-slot-<k>` to `/proc/self/attr/apparmor/exec`,
//!    or to `/proc/self/attr/exec` on a kernel without the first. The
//!    kernel puts the next exec of this process under the slot label.
//! 2. It sets no_new_privs.
//! 3. It applies the Landlock ruleset of the VM (decisions CH8-3, CH30).
//! 4. It execs the one installed CH.
//!
//! A step that fails ends the stage with its `StageError`, and no CH
//! starts. So a refused attribute write, for example under
//! `kernel.apparmor_restrict_unprivileged_unconfined` 1, fails the launch
//! closed (decision CH55).
//!
//! `run` is the one entry, and it always writes the real exec attribute
//! (decision CH58 rule 1). The rust job of CI loads no slot profile, so
//! there each launch fails closed at step 1 and starts no payload. The kvm
//! job loads the slot profiles and proves the full chain
//! (`tests/kvm_slot.rs`).
//!
//! The pid stays the same through all four execs: the launch step,
//! `systemd-run`, this stage and CH. `systemd-run --scope` execs its
//! payload in its own process. So the `Launched` handle of the spawner is
//! CH. The kernel keeps `PR_SET_PDEATHSIG` through an exec unless the
//! exec is a secure exec (set-user-ID, set-group-ID, file capabilities)
//! or a uid, a gid or a capability changes. This stage checks that the
//! signal is still SIGKILL before step 1, so a clear by the exec of
//! `systemd-run` fails closed, and a vmm that dies before CH runs ends
//! the stage. AppArmor clears the death signal at an exec that changes
//! the label (`apparmor_bprm_committing_creds` in
//! `security/apparmor/lsm.c`), so CH under its slot label has none. The
//! scope of CH is bound to the vmm unit, so systemd stops CH when the vmm
//! unit stops or dies (decision CH60 rule 1). `tests/kvm_slot.rs` proves
//! both stops through a vmm stand-in that runs as a user service.

use std::convert::Infallible;
use std::ffi::OsString;
use std::fmt;
use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::Path;

use creche_vmm_store::layout::Slot;
use serde::{Deserialize, Serialize};

use crate::landlock::{Rule, RulesetError, VmFiles};
use crate::launch::{Payload, Program};
use crate::slot_profile;

/// The argument of the stage mode. It comes before the stage plan.
pub const MODE: &str = "launch-stage";

/// The exec attributes, in the order the stage tries them (decision
/// CH55). The first is the AppArmor attribute of Linux 5.8 and later. The
/// second is the attribute of the major LSM.
pub const ATTRS: [&str; 2] = ["/proc/self/attr/apparmor/exec", "/proc/self/attr/exec"];

/// A slot number in a plan. A number at or above `SLOTS` does not parse.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u16", into = "u16")]
pub(crate) struct SlotNumber(pub(crate) Slot);

/// Why a number is not a slot.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NotASlot;

impl fmt::Display for NotASlot {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a slot number is below SLOTS")
    }
}

impl TryFrom<u16> for SlotNumber {
    type Error = NotASlot;

    fn try_from(number: u16) -> Result<Self, Self::Error> {
        Slot::new(number).map(Self).ok_or(NotASlot)
    }
}

impl From<SlotNumber> for u16 {
    fn from(slot: SlotNumber) -> Self {
        slot.0.number()
    }
}

/// The plan of the stage. The launch step writes it as one argument after
/// `MODE`. A plan has at most `launch::PLAN_MAX` (64 KiB) bytes, below
/// the 128 KiB that Linux allows for one argument.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Stage {
    slot: SlotNumber,
    payload: Payload,
    files: VmFiles,
}

impl Stage {
    pub(crate) fn new(slot: Slot, payload: Payload, files: VmFiles) -> Self {
        Self {
            slot: SlotNumber(slot),
            payload,
            files,
        }
    }

    /// The scope payload: `program` in the stage mode with this plan.
    pub(crate) fn command(&self, program: Program) -> Result<Payload, serde_json::Error> {
        let plan = serde_json::to_string(self)?;
        Ok(Payload::new(program, vec![MODE.to_owned(), plan]))
    }

    /// The plan in `args`, the arguments after `MODE`.
    fn parse(args: &[OsString]) -> Result<Self, StageError> {
        let [plan] = args else {
            return Err(StageError::Usage);
        };
        let plan = plan.to_str().ok_or(StageError::Usage)?;
        serde_json::from_str(plan).map_err(StageError::NotPlan)
    }
}

/// Why the stage did not exec CH. No CH starts after any of them.
#[derive(Debug)]
pub enum StageError {
    /// The host is not Linux, which has AppArmor and Landlock.
    NotLinux,
    /// The arguments are not exactly one UTF-8 plan.
    Usage,
    /// The plan is not the JSON of a stage plan.
    NotPlan(serde_json::Error),
    /// The death signal did not read.
    DeathSignal(io::Error),
    /// The death signal is no longer SIGKILL, so the stage would outlive
    /// a vmm that dies before CH runs (fact 80).
    DeathSignalLost,
    /// The kernel refused the slot label (decision CH55). The value is the
    /// label.
    Enter(String, io::Error),
    /// The kernel refused no_new_privs, or it is not set after the call.
    NoNewPrivs(io::Error),
    /// The Landlock ruleset is not in force (decision CH30).
    Landlock(RulesetError),
    /// The exec of CH failed.
    Exec(io::Error),
}

impl fmt::Display for StageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotLinux => formatter.write_str("the launch stage runs only on Linux"),
            Self::Usage => write!(formatter, "usage: vmm {MODE} <plan>"),
            Self::NotPlan(error) => write!(formatter, "the stage plan is not valid: {error}"),
            Self::DeathSignal(error) => write!(formatter, "reading the death signal: {error}"),
            Self::DeathSignalLost => formatter.write_str("the death signal is not SIGKILL"),
            Self::Enter(label, error) => write!(formatter, "entering {label}: {error}"),
            Self::NoNewPrivs(error) => write!(formatter, "setting no_new_privs: {error}"),
            Self::Landlock(error) => write!(formatter, "applying the Landlock ruleset: {error}"),
            Self::Exec(error) => write!(formatter, "running cloud-hypervisor: {error}"),
        }
    }
}

impl std::error::Error for StageError {}

/// Writes `value` to the first of `paths` that exists. Only a missing
/// path moves on to the next. Any other error ends the write, and so does
/// a write error, because ENOENT on the write means that no profile has
/// the label (C3 spike).
fn write_first(paths: &[&Path], value: &str) -> io::Result<()> {
    for path in paths {
        let mut file = match OpenOptions::new().write(true).open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        let written = file.write(value.as_bytes())?;
        return if written == value.len() {
            Ok(())
        } else {
            Err(io::Error::other(
                "the exec attribute took part of the value",
            ))
        };
    }
    Err(io::ErrorKind::NotFound.into())
}

/// The kernel calls of the stage. Only `Kernel` implements it outside
/// the unit tests of this file, which fake it to check the step order.
trait Steps {
    /// Whether the death signal is SIGKILL.
    fn death_signal_is_kill(&mut self) -> io::Result<bool>;
    /// Writes `exec <label>` to the exec attribute.
    fn enter(&mut self, label: &str) -> io::Result<()>;
    /// Sets no_new_privs and checks it.
    fn no_new_privs(&mut self) -> io::Result<()>;
    /// Applies the Landlock ruleset.
    fn restrict(&mut self, rules: &[Rule]) -> Result<(), RulesetError>;
    /// Execs `payload`. It returns only on an error.
    fn exec(&mut self, payload: &Payload) -> io::Error;
}

/// Runs the steps in their order. The first failure ends the stage.
fn steps(steps: &mut impl Steps, stage: &Stage) -> StageError {
    match steps.death_signal_is_kill() {
        Ok(true) => {}
        Ok(false) => return StageError::DeathSignalLost,
        Err(error) => return StageError::DeathSignal(error),
    }
    let label = slot_profile::label(stage.slot.0);
    if let Err(error) = steps.enter(&format!("exec {label}")) {
        return StageError::Enter(label, error);
    }
    if let Err(error) = steps.no_new_privs() {
        return StageError::NoNewPrivs(error);
    }
    let rules = crate::landlock::rules(&stage.files, stage.payload.program());
    if let Err(error) = steps.restrict(&rules) {
        return StageError::Landlock(error);
    }
    StageError::Exec(steps.exec(&stage.payload))
}

/// Runs the stage with `args`, the arguments after `MODE`. It is the one
/// entry of the stage, and it always writes the real exec attribute of
/// `ATTRS` (decision CH58 rule 1). A stage that works replaces this
/// process with CH, so this returns only an error.
///
/// # Errors
///
/// Each step that fails gives its `StageError`.
pub fn run(args: &[OsString]) -> Result<Infallible, StageError> {
    if !cfg!(target_os = "linux") {
        return Err(StageError::NotLinux);
    }
    let stage = Stage::parse(args)?;
    Err(steps(&mut Kernel, &stage))
}

/// The real kernel calls.
struct Kernel;

#[cfg(target_os = "linux")]
impl Steps for Kernel {
    fn death_signal_is_kill(&mut self) -> io::Result<bool> {
        let signal = rustix::process::parent_process_death_signal()?;
        Ok(signal == Some(rustix::process::Signal::KILL))
    }

    fn enter(&mut self, value: &str) -> io::Result<()> {
        write_first(&ATTRS.map(Path::new), value)
    }

    fn no_new_privs(&mut self) -> io::Result<()> {
        rustix::thread::set_no_new_privs(true)?;
        if rustix::thread::no_new_privs()? {
            Ok(())
        } else {
            Err(io::Error::other("no_new_privs is not set"))
        }
    }

    fn restrict(&mut self, rules: &[Rule]) -> Result<(), RulesetError> {
        crate::landlock::restrict(rules)
    }

    fn exec(&mut self, payload: &Payload) -> io::Error {
        use std::os::unix::process::CommandExt;
        // Fds 0 to 2 stay as the launch step left them: `/dev/null` and
        // the output pipe of the spawner (decision CH50 rule 12).
        std::process::Command::new(payload.program().path())
            .args(payload.args())
            .exec()
    }
}

/// No other host has AppArmor or Landlock (decision CH38).
#[cfg(not(target_os = "linux"))]
impl Steps for Kernel {
    fn death_signal_is_kill(&mut self) -> io::Result<bool> {
        Err(io::ErrorKind::Unsupported.into())
    }

    fn enter(&mut self, value: &str) -> io::Result<()> {
        write_first(&ATTRS.map(Path::new), value)
    }

    fn no_new_privs(&mut self) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into())
    }

    fn restrict(&mut self, _rules: &[Rule]) -> Result<(), RulesetError> {
        Err(RulesetError::NotEnforced)
    }

    fn exec(&mut self, _payload: &Payload) -> io::Error {
        io::ErrorKind::Unsupported.into()
    }
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::fs;
    use std::io;
    use std::path::{Path, PathBuf};

    use creche_vmm_store::layout::{RunRoot, Slot, StateRoot};

    use super::{Stage, StageError, Steps, write_first};
    use crate::landlock::{HostPath, Rule, RulesetError, Start, VmFiles};
    use crate::launch::{Payload, Program};

    const ID: &str = "0123abcd-0000-4000-8000-00000000c18c";
    const CH: &str = "/opt/components/vmm/bin/cloud-hypervisor";

    /// The stage of a restore of CH in slot 5.
    fn stage() -> Stage {
        let id = ID.parse().unwrap();
        let session = StateRoot::new(PathBuf::from("/state"))
            .unwrap()
            .session(&id);
        let run = RunRoot::new(PathBuf::from("/run/vmm")).unwrap().vm(&id);
        let root = HostPath::new(PathBuf::from("/state/store/root")).unwrap();
        let files = VmFiles::new(Start::Restore, root, &session, &run).unwrap();
        let program = Program::new(PathBuf::from(CH)).unwrap();
        let payload = Payload::new(program, vec!["--restore".to_owned()]);
        Stage::new(Slot::new(5).unwrap(), payload, files)
    }

    /// The steps of a stage, with the one step that fails.
    #[derive(Default)]
    struct Fake {
        calls: Vec<String>,
        fail: Option<&'static str>,
        lost: bool,
    }

    impl Fake {
        fn call(&mut self, name: &'static str, detail: String) -> io::Result<()> {
            self.calls
                .push(format!("{name} {detail}").trim_end().to_owned());
            if self.fail == Some(name) {
                Err(io::Error::other(name))
            } else {
                Ok(())
            }
        }
    }

    impl Steps for Fake {
        fn death_signal_is_kill(&mut self) -> io::Result<bool> {
            self.call("death_signal", String::new())?;
            Ok(!self.lost)
        }

        fn enter(&mut self, value: &str) -> io::Result<()> {
            self.call("enter", value.to_owned())
        }

        fn no_new_privs(&mut self) -> io::Result<()> {
            self.call("no_new_privs", String::new())
        }

        fn restrict(&mut self, rules: &[Rule]) -> Result<(), RulesetError> {
            let first = rules.first().map(|rule| rule.path.display().to_string());
            self.call("restrict", first.unwrap_or_default())
                .map_err(|_| RulesetError::NotEnforced)
        }

        fn exec(&mut self, payload: &Payload) -> io::Error {
            let line = format!(
                "{} {}",
                payload.program().path().display(),
                payload.args().join(" ")
            );
            let _ = self.call("exec", line);
            io::Error::other("exec")
        }
    }

    fn run(fake: &mut Fake) -> StageError {
        super::steps(fake, &stage())
    }

    #[test]
    fn the_steps_enter_the_label_before_the_ruleset_and_the_exec() {
        let mut fake = Fake::default();
        assert!(matches!(run(&mut fake), StageError::Exec(_)));
        let expected = [
            "death_signal",
            "enter exec creche-ch-slot-5",
            "no_new_privs",
            &format!("restrict {CH}"),
            &format!("exec {CH} --restore"),
        ];
        assert_eq!(fake.calls, expected);
    }

    #[test]
    fn a_failed_step_ends_the_stage_and_starts_no_ch() {
        type Check = fn(&StageError) -> bool;
        let cases: [(&str, Check); 4] = [
            ("death_signal", |error| {
                matches!(error, StageError::DeathSignal(_))
            }),
            (
                "enter",
                |error| matches!(error, StageError::Enter(label, _) if label == "creche-ch-slot-5"),
            ),
            ("no_new_privs", |error| {
                matches!(error, StageError::NoNewPrivs(_))
            }),
            ("restrict", |error| matches!(error, StageError::Landlock(_))),
        ];
        for (step, check) in cases {
            let mut fake = Fake {
                fail: Some(step),
                ..Fake::default()
            };
            let error = run(&mut fake);
            assert!(check(&error), "{step}: {error}");
            let last = fake.calls.last().unwrap();
            assert!(last.starts_with(step), "{step}: {:?}", fake.calls);
            assert!(!fake.calls.iter().any(|call| call.starts_with("exec")));
        }
    }

    #[test]
    fn a_lost_death_signal_enters_no_label() {
        let mut fake = Fake {
            lost: true,
            ..Fake::default()
        };
        assert!(matches!(run(&mut fake), StageError::DeathSignalLost));
        assert_eq!(fake.calls, ["death_signal"]);
    }

    #[test]
    fn the_plan_is_one_argument_after_the_mode() {
        let program = Program::new(PathBuf::from("/opt/components/vmm/bin/vmm")).unwrap();
        let payload = stage().command(program).unwrap();
        let [mode, plan] = payload.args() else {
            panic!("{:?}", payload.args());
        };
        assert_eq!(mode, super::MODE);
        let parsed = Stage::parse(&[OsString::from(plan)]).unwrap();
        assert_eq!(parsed, stage());
        assert!(matches!(Stage::parse(&[]), Err(StageError::Usage)));
        let two = [OsString::from(plan), OsString::from(plan)];
        assert!(matches!(Stage::parse(&two), Err(StageError::Usage)));
    }

    #[test]
    fn a_plan_with_a_slot_past_the_pool_is_refused() {
        let mut plan = serde_json::to_value(stage()).unwrap();
        assert_eq!(plan["slot"], 5);
        plan["slot"] = 64.into();
        let error = Stage::parse(&[OsString::from(plan.to_string())]);
        assert!(matches!(error, Err(StageError::NotPlan(_))), "{error:?}");
    }

    /// A new empty directory for one test, removed first if it exists.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("stage-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir(&dir).unwrap();
        dir
    }

    #[test]
    fn the_write_falls_back_only_past_a_missing_attribute() {
        let dir = scratch("fallback");
        let (apparmor, lsm) = (dir.join("apparmor-exec"), dir.join("exec"));
        fs::write(&lsm, "").unwrap();
        let paths = [apparmor.as_path(), lsm.as_path()];
        write_first(&paths, "exec creche-ch-slot-0").unwrap();
        assert_eq!(fs::read_to_string(&lsm).unwrap(), "exec creche-ch-slot-0");

        fs::write(&apparmor, "").unwrap();
        write_first(&paths, "exec creche-ch-slot-1").unwrap();
        assert_eq!(
            fs::read_to_string(&apparmor).unwrap(),
            "exec creche-ch-slot-1"
        );
        assert_eq!(fs::read_to_string(&lsm).unwrap(), "exec creche-ch-slot-0");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn the_write_fails_closed() {
        let dir = scratch("closed");
        let missing = [
            Path::new("/nonexistent/apparmor"),
            Path::new("/nonexistent/exec"),
        ];
        let error = write_first(&missing, "exec x").unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
        // A directory does not open for a write, and the next path then
        // gets no write either.
        let lsm = dir.join("exec");
        fs::write(&lsm, "").unwrap();
        assert!(write_first(&[dir.as_path(), lsm.as_path()], "exec x").is_err());
        assert_eq!(fs::read_to_string(&lsm).unwrap(), "");
        fs::remove_dir_all(dir).unwrap();
    }
}
