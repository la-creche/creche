//! A stand-in for the vmm in the launch tests and the Hyperion spike. The
//! first argument picks the role:
//!
//! - `launch` is the launch step, as in `vmm launch`.
//! - `spawn <systemd-run> <session id> <program> [args]` starts a spawner
//!   on `/proc/self/exe`, launches the program, prints its pid and waits
//!   to be killed.
//! - `--user ...` is a fake `systemd-run` for a host with no systemd. It
//!   execs the arguments after `--`.

use std::ffi::OsString;
use std::io::{self, Write};
use std::num::{NonZeroU32, NonZeroU64};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, ExitCode};
use std::thread;

use creche_vmm_confine::launch::{self, Launch, MODE, Payload, Program};
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::spawner::{SELF_EXE, Spawner};

/// The `MemoryMax` of the spawn role: 2 GiB guest plus 512 MiB (CH8-5).
const MEMORY_MAX: u64 = 2_684_354_560;

/// The `CPUQuota` of the spawn role in percent, one CPU.
const CPU_QUOTA: u32 = 100;

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let result = match args.split_first() {
        Some((role, _)) if role == MODE => launch::run()
            .map(|never| match never {})
            .map_err(Failure::from),
        Some((role, rest)) if role == "spawn" => spawn(rest),
        Some((role, rest)) if role == "--user" => fake_systemd_run(rest),
        _ => Err("usage: launch_chain launch | spawn ... | --user ...".into()),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("launch_chain: {error}");
            ExitCode::from(78)
        }
    }
}

type Failure = Box<dyn std::error::Error>;

/// The spawn role. It returns only on an error.
fn spawn(args: &[OsString]) -> Result<(), Failure> {
    let [systemd_run, id, program, rest @ ..] = args else {
        return Err("spawn <systemd-run> <session id> <program> [args]".into());
    };
    let text = |arg: &OsString| arg.to_str().map(str::to_owned).ok_or("not UTF-8");
    let id = text(id)?.parse().map_err(|_| "not a session id")?;
    let payload = Payload::new(
        Program::new(PathBuf::from(program))?,
        rest.iter().map(text).collect::<Result<_, _>>()?,
    );
    let memory = NonZeroU64::new(MEMORY_MAX).ok_or("zero memory")?;
    let quota = NonZeroU32::new(CPU_QUOTA).ok_or("zero quota")?;
    let scope = Scope::new(id, memory, quota);
    let launch = Launch::new(Program::new(PathBuf::from(systemd_run))?, scope, payload);

    let spawner = Spawner::start(PathBuf::from(SELF_EXE))?;
    let launched = spawner.launch(launch)?;
    writeln!(io::stdout(), "{}", launched.id())?;
    io::stdout().flush()?;
    loop {
        thread::park();
    }
}

/// The fake `systemd-run`. It returns only on an error.
fn fake_systemd_run(args: &[OsString]) -> Result<(), Failure> {
    let after = args.iter().position(|arg| arg == "--").ok_or("no --")?;
    let (program, rest) = args
        .get(after + 1..)
        .and_then(<[OsString]>::split_first)
        .ok_or("no payload")?;
    Err(Command::new(program).args(rest).exec().into())
}
