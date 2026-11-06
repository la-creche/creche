//! A stand-in for the vmm in the launch tests. The first argument picks
//! the role:
//!
//! - `launch` is the launch step, as in `vmm launch`.
//! - `launch-stage <plan>` is the launch stage, as in `vmm launch-stage`
//!   (decisions CH55, CH58 rule 1). It calls `stage::run`, which writes
//!   the real exec attribute. The rust job of CI loads no slot profile,
//!   so the stage fails closed at its label step and starts no payload.
//! - `--user ...` is a fake `systemd-run` for a host with no systemd. It
//!   writes `core <soft> <hard>`, its own core limits, to stderr. It then
//!   execs the arguments after `--`. For the scope unit of `HOLD` it
//!   execs `/bin/sleep 60` instead, so a test has a live process to
//!   signal.
//! - `probe <run dir> <root disk> <session disk> <outside file> <port>`
//!   tries five accesses and writes each result to `<run dir>/probe.txt`.
//! - `say <text>` writes `<text> out` to stdout and `<text> err` to
//!   stderr, as CH writes its log lines.

use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::net::{Ipv4Addr, TcpStream};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, ExitCode};

use creche_vmm_confine::launch::{self, MODE};
use creche_vmm_confine::stage;

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let result = match args.split_first() {
        Some((role, _)) if role == MODE => launch::run()
            .map(|never| match never {})
            .map_err(Failure::from),
        Some((role, rest)) if role == stage::MODE => stage::run(rest)
            .map(|never| match never {})
            .map_err(Failure::from),
        Some((role, rest)) if role == "--user" => fake_systemd_run(rest),
        Some((role, rest)) if role == "probe" => probe(rest),
        Some((role, rest)) if role == "say" => say(rest),
        _ => Err(
            "usage: launch_chain launch | launch-stage ... | --user ... | probe ... | say ..."
                .into(),
        ),
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

/// The scope unit whose launch the fake `systemd-run` holds in
/// `/bin/sleep 60`. `tests/launch.rs` launches the session id
/// `0123abcd-0000-4000-8000-00000000d0d0` for it.
const HOLD: &str = "--unit=creche-vm-0123abcd-0000-4000-8000-00000000d0d0";

/// The fake `systemd-run`. It returns only on an error. Its core limits
/// are the ones that the launch step set (decision CH8-5, CH36 G27), so
/// the line on stderr lets a test check them.
fn fake_systemd_run(args: &[OsString]) -> Result<(), Failure> {
    let core = rustix::process::getrlimit(rustix::process::Resource::Core);
    let limit = |value: Option<u64>| value.map_or("unlimited".to_owned(), |v| v.to_string());
    writeln!(
        io::stderr(),
        "core {} {}",
        limit(core.current),
        limit(core.maximum)
    )?;
    if args.iter().any(|arg| arg == HOLD) {
        return Err(Command::new("/bin/sleep").arg("60").exec().into());
    }
    let after = args.iter().position(|arg| arg == "--").ok_or("no --")?;
    let (program, rest) = args
        .get(after + 1..)
        .and_then(<[OsString]>::split_first)
        .ok_or("no payload")?;
    Err(Command::new(program).args(rest).exec().into())
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
