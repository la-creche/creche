//! The program `vmm` (decision CH7-5). The one argument picks the mode:
//!
//! - `vmm serve` reads its config from the env (`creche_vmm::config`) and
//!   runs the vmm on the multi-thread tokio runtime until SIGTERM. A
//!   missing or bad key, or a failed start step, exits 78. The end of a
//!   shutdown exits 0.
//! - `vmm launch` is the launch step of one Cloud Hypervisor process. It
//!   runs on the main thread with no runtime, because the access rules of
//!   Landlock bind only the thread that sets them.

use std::ffi::OsString;
use std::fmt;
use std::process::ExitCode;

use creche_vmm::config::Config;
use creche_vmm::shutdown::RUNTIME_END;
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

/// Reads the config, then builds the tokio runtime and runs the vmm on it
/// (decision CH26 rule 1). Only this mode builds a runtime.
fn serve() -> ExitCode {
    let config = match Config::read() {
        Ok(config) => config,
        Err(error) => return refuse(&error),
    };
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

    let served = runtime.block_on(creche_vmm::serve(config));
    // Caller duty 8: a take runs in `spawn_blocking`, and a plain drop of
    // the runtime waits for each blocking task. So the end of the runtime
    // has a bound inside the 5 s that `SHUTDOWN_WAIT` leaves.
    runtime.shutdown_timeout(RUNTIME_END);
    match served {
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
    }

    #[test]
    fn parse_refuses_a_wrong_list() {
        assert_eq!(Mode::parse(&args(&[])), None);
        assert_eq!(Mode::parse(&args(&["start"])), None);
        assert_eq!(Mode::parse(&args(&["serve", "extra"])), None);
    }

    /// The test env has no vmm key and no launch plan.
    #[test]
    fn each_mode_refuses_a_bare_env() {
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
