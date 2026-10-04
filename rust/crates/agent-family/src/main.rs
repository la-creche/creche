//! The `agent-family` program: `agent-family validate <registry path>`.

use std::io;
use std::process::ExitCode;

fn main() -> ExitCode {
    // An argument that is not UTF-8 text names no registry and no family
    // that this program can print. The program refuses the command line.
    let args: Option<Vec<String>> = std::env::args_os()
        .skip(1)
        .map(|arg| arg.into_string().ok())
        .collect();
    let Some(args) = args else {
        eprintln!("agent-family: an argument is not UTF-8 text");

        return ExitCode::from(agent_family::EXIT_USAGE);
    };

    let status = agent_family::run(&args, &mut io::stdout().lock(), &mut io::stderr().lock());

    ExitCode::from(status)
}
