//! A stand-in for the vmm in the launch tests and the host spike. The
//! first argument picks the role:
//!
//! - `launch` is the launch step, as in `vmm launch`.
//! - `spawn <systemd-run> <root> <session id> <program> [args]` starts a
//!   spawner on `/proc/self/exe`, launches the program, prints its pid and
//!   waits to be killed. `<root>` is the state root and the run root of
//!   the VM files, with the root disk at `<root>/root.img`.
//! - `--user ...` is a fake `systemd-run` for a host with no systemd. It
//!   execs the arguments after `--`.
//! - `probe <run dir> <root disk> <session disk> <outside file> <port>`
//!   tries five accesses and writes each result to `<run dir>/probe.txt`.
//! - `say <text>` writes `<text> out` to stdout and `<text> err` to
//!   stderr, as CH writes its log lines.

use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::net::{Ipv4Addr, TcpStream};
use std::num::{NonZeroU32, NonZeroU64};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::thread;

use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_confine::launch::{self, Launch, MODE, Payload, Program};
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::spawner::{SELF_EXE, Spawner};
use creche_vmm_store::layout::{RunRoot, StateRoot};
use creche_wire::SessionId;

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
        Some((role, rest)) if role == "probe" => probe(rest),
        Some((role, rest)) if role == "say" => say(rest),
        _ => {
            Err("usage: launch_chain launch | spawn ... | --user ... | probe ... | say ...".into())
        }
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
    let [systemd_run, root, id, program, rest @ ..] = args else {
        return Err("spawn <systemd-run> <root> <session id> <program> [args]".into());
    };
    let text = |arg: &OsString| arg.to_str().map(str::to_owned).ok_or("not UTF-8");
    let id: SessionId = text(id)?.parse().map_err(|_| "not a session id")?;
    let files = vm_files(Path::new(root), &id)?;
    let payload = Payload::new(
        Program::new(PathBuf::from(program))?,
        rest.iter().map(text).collect::<Result<_, _>>()?,
    );
    let memory = NonZeroU64::new(MEMORY_MAX).ok_or("zero memory")?;
    let quota = NonZeroU32::new(CPU_QUOTA).ok_or("zero quota")?;
    let scope = Scope::new(id, memory, quota);
    let systemd_run = Program::new(PathBuf::from(systemd_run))?;
    let launch = Launch::new(systemd_run, scope, payload, files);

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

/// The VM files of `id` under `root`, for a restore (no kernel).
fn vm_files(root: &Path, id: &SessionId) -> Result<VmFiles, Failure> {
    let session = StateRoot::new(root.to_path_buf())?.session(id);
    let run = RunRoot::new(root.to_path_buf())?.vm(id);
    let root_disk = HostPath::new(root.join("root.img"))?;
    Ok(VmFiles::new(Start::Restore, root_disk, &session, &run)?)
}

/// The say role: one line on stdout, then one on stderr.
fn say(args: &[OsString]) -> Result<(), Failure> {
    let [text] = args else {
        return Err("say <text>".into());
    };
    let text = text.to_str().ok_or("not UTF-8")?;
    writeln!(io::stdout(), "{text} out")?;
    io::stdout().flush()?;
    writeln!(io::stderr(), "{text} err")?;
    Ok(())
}

/// The probe role. Each line of `probe.txt` is a name and `ok` or the
/// error kind.
fn probe(args: &[OsString]) -> Result<(), Failure> {
    let [run, root_disk, session_disk, outside, port] = args else {
        return Err("probe <run dir> <root disk> <session disk> <outside> <port>".into());
    };
    let port: u16 = port.to_str().ok_or("not UTF-8")?.parse()?;
    let write = |path| OpenOptions::new().write(true).open(path).map(drop);
    let results = [
        ("read_root_disk", File::open(root_disk).map(drop)),
        ("write_root_disk", write(root_disk)),
        ("write_session_disk", write(session_disk)),
        ("read_outside", File::open(outside).map(drop)),
        (
            "connect_tcp",
            TcpStream::connect((Ipv4Addr::LOCALHOST, port)).map(drop),
        ),
    ];
    let mut report = File::create(Path::new(run).join("probe.txt"))?;
    for (name, result) in results {
        match result {
            Ok(()) => writeln!(report, "{name} ok")?,
            Err(error) => writeln!(report, "{name} {:?}", error.kind())?,
        }
    }
    Ok(())
}
