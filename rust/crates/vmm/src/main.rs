//! The program `vmm` (decision CH7-5). The one argument picks the mode:
//!
//! - `vmm serve` runs the host API server on the multi-thread tokio
//!   runtime.
//! - `vmm launch` is the launch step of one Cloud Hypervisor process. It
//!   runs on the main thread with no runtime, because the access rules of
//!   Landlock bind only the thread that sets them.

use std::ffi::OsString;
use std::fmt;
use std::process::ExitCode;

use tokio::runtime::Builder;

/// The exit status of a refused start (owner item 86). A unit with
/// `RestartPreventExitStatus=78` then stays stopped, because a restart
/// changes nothing until the operator fixes the cause.
const EX_CONFIG: u8 = 78;

/// The usage line for a wrong argument list.
const USAGE: &str = "usage: vmm serve | vmm launch";

/// The modes of the program.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Serve,
    Launch,
}

impl Mode {
    /// The mode of the arguments after the program name. Any list that is
    /// not exactly one known mode is refused.
    fn parse(args: &[OsString]) -> Option<Self> {
        match args {
            [mode] if mode == "serve" => Some(Self::Serve),
            [mode] if mode == "launch" => Some(Self::Launch),
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
            let Err(error) = creche_vmm_confine::launch::run();
            refuse(&error)
        }
    }
}

/// Builds the tokio runtime and runs the host API server on it (decision
/// CH26 rule 1). Only this mode builds a runtime.
fn serve() -> ExitCode {
    let runtime = match Builder::new_multi_thread().build() {
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

    use super::{EX_CONFIG, dispatch};

    fn status(args: &[&str]) -> ExitCode {
        let args: Vec<OsString> = args.iter().map(OsString::from).collect();
        dispatch(&args)
    }

    #[test]
    fn each_mode_refuses_until_built() {
        assert_eq!(status(&["serve"]), ExitCode::from(EX_CONFIG));
        assert_eq!(status(&["launch"]), ExitCode::from(EX_CONFIG));
    }

    #[test]
    fn a_wrong_argument_list_is_refused() {
        assert_eq!(status(&[]), ExitCode::from(EX_CONFIG));
        assert_eq!(status(&["start"]), ExitCode::from(EX_CONFIG));
        assert_eq!(status(&["serve", "extra"]), ExitCode::from(EX_CONFIG));
    }
}
