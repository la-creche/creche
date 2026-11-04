//! A host API server with one handler that echoes after 101. The proc
//! test `integration/proc/test_proc_vmm_upgrade.py` drives it with httpx,
//! to prove that httpx sends the JSON body of an Upgrade request (decision
//! CH20, `wire/AGENTS.md`).
//!
//! Usage: `upgrade_echo <socket path>`. It serves until a signal ends it.

use std::path::Path;
use std::process::ExitCode;

use creche_vmm::server::{Stop, run_with};
use tokio::runtime::Builder;

mod echo;

fn main() -> ExitCode {
    let Some(path) = std::env::args_os().nth(1) else {
        eprintln!("usage: upgrade_echo <socket path>");
        return ExitCode::FAILURE;
    };
    let runtime = match Builder::new_multi_thread()
        .enable_io()
        .enable_time()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("upgrade_echo: {error}");
            return ExitCode::FAILURE;
        }
    };
    // The process holds the stop until a signal ends it.
    let (_stop, signal) = Stop::pair();
    match runtime.block_on(run_with(Path::new(&path), signal, echo::answer)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("upgrade_echo: {error}");
            ExitCode::FAILURE
        }
    }
}
