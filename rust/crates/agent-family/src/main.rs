//! The `agent-family` program: `agent-family validate <registry path>`.
//! `agent_family::entry` holds each rule of the program.
//!
//! This `main` has three steps and no other code (`rust/AGENTS.md`, "The
//! panic rule", clause 2). No boundary for a panic is around a step here, and
//! no test of the library runs this file.

use std::process::ExitCode;

use creche_contracts::config::Env;
use creche_runtime::log;

fn main() -> ExitCode {
    log::init(agent_family::NAME);
    let env = Env::from_os(std::env::vars_os());

    agent_family::entry(std::env::args_os(), &env)
}
