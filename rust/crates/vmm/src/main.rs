//! The program `vmm` (decision CH7-5). The one argument picks the mode:
//!
//! - `vmm serve` runs the host API server on the multi-thread tokio
//!   runtime.
//! - `vmm launch` is the launch step of one Cloud Hypervisor process. It
//!   runs on the main thread with no runtime, because the access rules of
//!   Landlock bind only the thread that sets them.
//! - `vmm launch-stage <plan>` is the launch stage (plan PR L16c, decision
//!   CH55). `systemd-run` execs it in the VM scope. It enters the slot
//!   label, applies Landlock and execs CH, with no runtime either. It
//!   takes exactly one plan argument and calls `stage::run`, the one entry
//!   that writes the real exec attribute.

use std::ffi::OsString;
use std::fmt;
use std::process::ExitCode;

use creche_vmm_confine::{launch, stage};
use tokio::runtime::Builder;

/// The exit status of a refused start (owner item 86). A unit with
/// `RestartPreventExitStatus=78` then stays stopped, because a restart
/// changes nothing until the operator fixes the cause.
const EX_CONFIG: u8 = 78;

/// The usage line for a wrong argument list.
const USAGE: &str = "usage: vmm serve | vmm launch | vmm launch-stage <plan>";

/// The modes of the program.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode<'a> {
    Serve,
    Launch,
    /// The launch stage with its one plan argument.
    Stage(&'a OsString),
}

impl<'a> Mode<'a> {
    /// The mode of the arguments after the program name. Any list that is
    /// not exactly one known mode, or the stage mode and one plan, is
    /// refused.
    fn parse(args: &'a [OsString]) -> Option<Self> {
        match args {
            [mode] if mode == "serve" => Some(Self::Serve),
            [mode] if mode == launch::MODE => Some(Self::Launch),
            [mode, plan] if mode == stage::MODE => Some(Self::Stage(plan)),
            _ => None,
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    dispatch(&args)
}

/// Runs the mode that `args` names and gives the exit status.
fn dispatch(args: &[OsString]) -> ExitCode {
    let Some(mode) = Mode::parse(args) else {
        return refuse(&USAGE);
    };

    match mode {
        Mode::Serve => serve(),
        Mode::Launch => {
            let Err(error) = launch::run();
            refuse(&error)
        }
        // Never `stage::run_with_fake_attr_for_tests`: it skips the slot
        // label (decision CH55).
        Mode::Stage(plan) => {
            let Err(error) = stage::run(std::slice::from_ref(plan));
            refuse(&error)
        }
    }
}

/// Builds the tokio runtime and runs the host API server on it (decision
/// CH26 rule 1). Only this mode builds a runtime.
fn serve() -> ExitCode {
    // The multi-thread runtime (decision CH36, gap G21), so one stray
    // blocking call cannot stall every relay. The server needs the IO
    // driver for its socket and the time driver for its timeouts.
    let runtime = match Builder::new_multi_thread()
        .enable_io()
        .enable_time()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => return refuse(&error),
    };

    match runtime.block_on(creche_vmm::serve()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => refuse(&error),
    }
}

/// Writes the reason to stderr and gives the status of a refused start.
fn refuse(reason: &dyn fmt::Display) -> ExitCode {
    eprintln!("vmm: {reason}");
    ExitCode::from(EX_CONFIG)
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::process::ExitCode;

    use super::{EX_CONFIG, Mode, dispatch};

    fn args(args: &[&str]) -> Vec<OsString> {
        args.iter().map(OsString::from).collect()
    }

    fn status(list: &[&str]) -> ExitCode {
        dispatch(&args(list))
    }

    #[test]
    fn parse_names_each_mode() {
        assert_eq!(Mode::parse(&args(&["serve"])), Some(Mode::Serve));
        assert_eq!(Mode::parse(&args(&["launch"])), Some(Mode::Launch));
        let stage = args(&["launch-stage", "{}"]);
        assert_eq!(Mode::parse(&stage), Some(Mode::Stage(&stage[1])));
    }

    #[test]
    fn parse_refuses_a_wrong_list() {
        assert_eq!(Mode::parse(&args(&[])), None);
        assert_eq!(Mode::parse(&args(&["start"])), None);
        assert_eq!(Mode::parse(&args(&["serve", "extra"])), None);
        assert_eq!(Mode::parse(&args(&["launch", "extra"])), None);
        assert_eq!(Mode::parse(&args(&["launch-stage"])), None);
        assert_eq!(Mode::parse(&args(&["launch-stage", "a", "b"])), None);
    }

    /// Each mode names the word of its crate, so the launch step execs the
    /// stage that this program routes.
    #[test]
    fn each_mode_is_the_word_of_its_crate() {
        assert_eq!(creche_vmm_confine::launch::MODE, "launch");
        assert_eq!(creche_vmm_confine::stage::MODE, "launch-stage");
    }

    #[test]
    fn each_mode_refuses_until_built() {
        assert_eq!(status(&["serve"]), ExitCode::from(EX_CONFIG));
        assert_eq!(status(&["launch"]), ExitCode::from(EX_CONFIG));
    }

    /// A plan that does not parse ends the stage with exit 78 before any
    /// kernel call. Off Linux the stage refuses first.
    #[test]
    fn a_bad_stage_plan_is_refused() {
        assert_eq!(status(&["launch-stage", "{"]), ExitCode::from(EX_CONFIG));
    }

    #[test]
    fn a_wrong_argument_list_is_refused() {
        assert_eq!(status(&[]), ExitCode::from(EX_CONFIG));
        assert_eq!(status(&["start"]), ExitCode::from(EX_CONFIG));
        assert_eq!(status(&["serve", "extra"]), ExitCode::from(EX_CONFIG));
        assert_eq!(status(&["launch-stage"]), ExitCode::from(EX_CONFIG));
    }
}
