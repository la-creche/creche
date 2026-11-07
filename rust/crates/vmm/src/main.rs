//! The program `vmm` (decision CH7-5). The one argument picks the mode:
//!
//! - `vmm serve` reads its config from the env (`creche_vmm::config`),
//!   raises its soft `RLIMIT_NOFILE` to `planes::fd_need` and runs the vmm
//!   on the multi-thread tokio runtime until SIGTERM. A missing or bad
//!   key, a hard fd limit below the need, or a failed start step, exits
//!   78. The end of a shutdown exits 0.
//! - `vmm launch` is the launch step of one Cloud Hypervisor process. It
//!   runs on the main thread with no runtime, because the access rules of
//!   Landlock bind only the thread that sets them.
//! - `vmm launch-stage <plan>` is the launch stage (plan PR L16c, decision
//!   CH55). `systemd-run` execs it in the VM scope. It enters the slot
//!   label, applies Landlock and execs CH, with no runtime either. It
//!   takes exactly one plan argument and calls `stage::run`, the one entry
//!   of the stage, which always writes the real exec attribute (decision
//!   CH58 rule 1).
//! - `vmm kill-scopes` is the `ExecStopPost=` step of the vmm unit
//!   (decisions CH61 rule 1, CH63). It reads only
//!   `AGENT_SYSTEMCTL_PROGRAM` (`config::KillScopesConfig`) and calls
//!   `leftovers::kill_all`, which sends SIGKILL to each `creche-vm-*`
//!   unit and queues no stop job. A missing or bad key exits 78. A unit
//!   that is gone at its kill has ended. Any other failure exits 1, so
//!   the unit ends `failed` only on a real failure.

use std::ffi::OsString;
use std::fmt;
use std::process::ExitCode;

use creche_vmm::config::{Config, KillScopesConfig};
use creche_vmm::planes::fd_need;
use creche_vmm::shutdown::RUNTIME_END;
use creche_vmm_confine::{launch, leftovers, nofile, stage};
use tokio::runtime::Builder;

/// The exit status of a refused start (owner item 86). A unit with
/// `RestartPreventExitStatus=78` then stays stopped, because a restart
/// changes nothing until the operator fixes the cause.
const EX_CONFIG: u8 = 78;

/// The usage line for a wrong argument list.
const USAGE: &str = "usage: vmm serve | vmm launch | vmm launch-stage <plan> | vmm kill-scopes";

/// The modes of the program.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode<'a> {
    Serve,
    Launch,
    /// The launch stage with its one plan argument.
    Stage(&'a OsString),
    /// The `ExecStopPost=` step of the vmm unit (decision CH63).
    KillScopes,
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
            [mode] if mode == "kill-scopes" => Some(Self::KillScopes),
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
        Mode::Stage(plan) => {
            let Err(error) = stage::run(std::slice::from_ref(plan));
            refuse(&error)
        }
        Mode::KillScopes => kill_scopes(),
    }
}

/// Reads `AGENT_SYSTEMCTL_PROGRAM`, then sends SIGKILL to each VM unit
/// (decision CH63). A bad key exits 78, and a failed kill exits 1.
fn kill_scopes() -> ExitCode {
    let config = match KillScopesConfig::read() {
        Ok(config) => config,
        Err(error) => return refuse(&error),
    };
    match leftovers::kill_all(&config.systemctl) {
        Ok(kills) => {
            eprintln!("vmm: {kills}");
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("vmm: {error}");
            ExitCode::FAILURE
        }
    }
}

/// Reads the config, raises the fd limit, then builds the tokio runtime
/// and runs the vmm on it (decision CH26 rule 1). Only this mode builds a
/// runtime.
fn serve() -> ExitCode {
    let config = match Config::read() {
        Ok(config) => config,
        Err(error) => return refuse(&error),
    };
    // Before any fd of a VM: the soft limit of systemd (1024) holds the
    // bridges of a few busy guests only (crate AGENTS.md, Known gap 21).
    if let Err(error) = nofile::raise(fd_need()) {
        return refuse(&error);
    }
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
        let stage = args(&["launch-stage", "{}"]);
        assert_eq!(Mode::parse(&stage), Some(Mode::Stage(&stage[1])));
        let kill = args(&["kill-scopes"]);
        assert_eq!(Mode::parse(&kill), Some(Mode::KillScopes));
    }

    #[test]
    fn parse_refuses_a_wrong_list() {
        assert_eq!(Mode::parse(&args(&[])), None);
        assert_eq!(Mode::parse(&args(&["start"])), None);
        assert_eq!(Mode::parse(&args(&["serve", "extra"])), None);
        assert_eq!(Mode::parse(&args(&["launch", "extra"])), None);
        assert_eq!(Mode::parse(&args(&["launch-stage"])), None);
        assert_eq!(Mode::parse(&args(&["launch-stage", "a", "b"])), None);
        assert_eq!(Mode::parse(&args(&["kill-scopes", "extra"])), None);
        assert_eq!(Mode::parse(&args(&["kill-scope"])), None);
    }

    /// Each mode names the word of its crate, so the launch step execs the
    /// stage that this program routes.
    #[test]
    fn each_mode_is_the_word_of_its_crate() {
        assert_eq!(creche_vmm_confine::launch::MODE, "launch");
        assert_eq!(creche_vmm_confine::stage::MODE, "launch-stage");
    }

    /// The test env has no vmm key and no launch plan.
    #[test]
    fn each_mode_refuses_a_bare_env() {
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
        let extra = ["kill-scopes", "extra"];
        assert_eq!(status(&extra), ExitCode::from(EX_CONFIG));
    }
}
