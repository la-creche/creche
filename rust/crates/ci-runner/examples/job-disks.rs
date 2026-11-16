//! The disks of one job, written by the crate, for the proof of the image
//! (`ci-runner-image/bin/ci-runner-image-proof`, design section 8, PR 5).
//!
//! ```text
//! job-disks --runs <dir> --root <root.qcow2> --data-gib <n>
//!           --registration <file> --name <runner name> --labels <a,b>
//!           --origin http://<address>:<port> --handle <job handle>
//!           --mac <MAC> --gateway <IPv4> --address <IPv4> --dns <IPv4>
//! ```
//!
//! 1. It writes the run folder of the job in `--runs` with
//!    `JobDisks::write`.
//! 2. It prints `folder=<run folder>` and `root-copy=<reflink|sparse>`.
//! 3. It holds the disks until its stdin closes. The proof boots the VM in
//!    that time.
//! 4. It removes the run folder with `JobDisks::remove`, and prints
//!    `removed`.
//!
//! `--registration` names a JSON file in the shape of the registration
//! reply of Forgejo: `id`, `uuid` and `token`. The proof writes a fake one.
//! The token is in a file, never on the argv, and the program prints no
//! byte of it.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::str::FromStr;

use creche_ci_runner::disks::{DataDiskSize, JobDisks, RootImage};
use creche_ci_runner::names::{Label, Labels};
use creche_ci_runner::net::{BridgeNet, GuestNetwork};
use creche_ci_runner::reply::Registration;
use creche_ci_runner::seed::{EphemeralRunner, JobSeed};

/// Each flag that the program takes. Each one is required.
const FLAGS: [&str; 12] = [
    "--runs",
    "--root",
    "--data-gib",
    "--registration",
    "--name",
    "--labels",
    "--origin",
    "--handle",
    "--mac",
    "--gateway",
    "--address",
    "--dns",
];

/// The most bytes of the registration file. A reply of Forgejo holds a
/// few hundred.
const REGISTRATION_CAP: u64 = 64 * 1024;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("job-disks: {message}");
            ExitCode::FAILURE
        }
    }
}

/// The steps of the module doc.
fn run() -> Result<(), String> {
    let flags = flags(std::env::args().skip(1))?;
    let value = |flag: &str| flags.get(flag).map(String::as_str).unwrap_or_default();

    let labels = value("--labels")
        .split(',')
        .map(|text| parsed::<Label>("--labels", text))
        .collect::<Result<Vec<_>, _>>()?;
    let labels = Labels::new(labels).map_err(|error| format!("--labels: {error}"))?;
    let runner = EphemeralRunner::new(
        parsed("--origin", value("--origin"))?,
        parsed("--name", value("--name"))?,
        labels,
        registration(Path::new(value("--registration")))?,
    );

    let bridge = BridgeNet::new(parsed("--gateway", value("--gateway"))?)
        .map_err(|error| format!("--gateway: {error}"))?;
    let address = bridge
        .guest(parsed("--address", value("--address"))?)
        .map_err(|error| format!("--address: {error}"))?;
    let network = GuestNetwork::new(
        parsed("--mac", value("--mac"))?,
        address,
        parsed("--dns", value("--dns"))?,
    )
    .map_err(|error| format!("--dns: {error}"))?;
    let seed = JobSeed::new(runner, parsed("--handle", value("--handle"))?, network);

    let size = DataDiskSize::from_gib(parsed("--data-gib", value("--data-gib"))?)
        .map_err(|error| format!("--data-gib: {error}"))?;
    let root = RootImage::new(PathBuf::from(value("--root")));
    let disks = JobDisks::write(Path::new(value("--runs")), &seed, &root, size)
        .map_err(|error| error.to_string())?;
    drop(seed);

    say(&format!("folder={}", disks.folder().display()))?;
    say(&format!("root-copy={}", disks.root_copy()))?;

    // The proof closes the stdin of the program after the boot.
    io::stdin()
        .read_to_end(&mut Vec::new())
        .map_err(|error| format!("stdin: {error}"))?;
    disks.remove().map_err(|error| error.to_string())?;
    say("removed")
}

/// Each flag of `args` and its value. A flag that is unknown, given twice
/// or absent is a fault.
fn flags(mut args: impl Iterator<Item = String>) -> Result<BTreeMap<String, String>, String> {
    let mut flags = BTreeMap::new();
    while let Some(flag) = args.next() {
        if !FLAGS.contains(&flag.as_str()) {
            return Err(format!("unknown flag {flag:?}"));
        }
        let value = args.next().ok_or_else(|| format!("{flag} has no value"))?;
        if flags.insert(flag.clone(), value).is_some() {
            return Err(format!("{flag} is given twice"));
        }
    }
    if let Some(flag) = FLAGS.iter().find(|flag| !flags.contains_key(**flag)) {
        return Err(format!("{flag} is required"));
    }
    Ok(flags)
}

/// `text` as a `T`, or the fault of `flag`.
fn parsed<T: FromStr>(flag: &str, text: &str) -> Result<T, String>
where
    T::Err: std::fmt::Display,
{
    text.parse().map_err(|error| format!("{flag}: {error}"))
}

/// The registration in the file at `path`. A fault names the kind and the
/// place of the fault, never a value of the file.
fn registration(path: &Path) -> Result<Registration, String> {
    let fail = |what: &str| format!("the registration file {}: {what}", path.display());

    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(REGISTRATION_CAP).read_to_end(&mut bytes))
        .map_err(|error| fail(&error.to_string()))?;
    serde_json::from_slice(&bytes).map_err(|error| {
        fail(&format!(
            "{:?} fault at line {}, column {}",
            error.classify(),
            error.line(),
            error.column()
        ))
    })
}

/// Writes `line` to stdout at once, so the proof reads it while the
/// program waits.
fn say(line: &str) -> Result<(), String> {
    let mut out = io::stdout().lock();
    writeln!(out, "{line}")
        .and_then(|()| out.flush())
        .map_err(|error| format!("stdout: {error}"))
}
