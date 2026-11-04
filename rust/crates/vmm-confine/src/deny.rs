//! The proof of deny-by-default for one running VM (decision CH6-3).
//!
//! Lifecycle runs `check` at create, before the VM is ready, and after each
//! egress edit. A failure is the fault `egress_assert_failed` (decisions
//! CH6-3, CH21-1). The check has two parts:
//!
//! 1. The command line of the Cloud Hypervisor process has no argument
//!    that starts with `--net` (decision CH8-1, fact 44). A boot has
//!    exactly one `--vsock`. A restore has none, because CH v54 takes
//!    every device of a restore from the snapshot and refuses a device
//!    argument with `--restore` (spike 2026-11-03, `--restore` with
//!    `--landlock` gave a usage error that asks for `--kernel`).
//! 2. The run directory of the VM holds exactly the listener files
//!    `vsock.sock_<port>` of the host ports that the vmm bound for the
//!    family (decisions CH14, CH24). That set is empty in phase 2.
//!
//! CONTRACT-QUESTION: on a restore the command line shows no device, so
//! part 1 cannot see a network card that the snapshot holds. The check
//! relies on the snapshot of a boot that passed it. No decision row says
//! whether the check must also read the device set of a restored VM, for
//! example from `ch-remote info`. AGENTS.md lists this in Known gaps.

use std::ffi::OsString;
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use creche_vmm_store::layout::VmRunDir;
use creche_wire::HostPort;

use crate::landlock::Start;

/// The prefix of every argument that adds a network card (fact 44).
const NET: &[u8] = b"--net";

/// The argument that adds the vsock device.
const VSOCK: &[u8] = b"--vsock";

/// Why the proof of deny-by-default failed. Each value is the fault
/// `egress_assert_failed` (decision CH6-3).
#[derive(Debug)]
pub enum EgressAssertFailed {
    /// The command line of the CH process did not read.
    Cmdline(io::Error),
    /// An argument starts with `--net` (decision CH8-1).
    Net,
    /// The command line has this many `--vsock` arguments. A boot needs
    /// exactly one and a restore none.
    Vsock(usize),
    /// The run directory did not read.
    RunDir(io::Error),
    /// A listener file of a port that the vmm did not bind.
    Extra(OsString),
    /// A port that the vmm bound has no listener file.
    Missing(HostPort),
}

impl fmt::Display for EgressAssertFailed {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Cmdline(error) => write!(formatter, "reading the CH command line: {error}"),
            Self::Net => formatter.write_str("the CH command line adds a network card"),
            Self::Vsock(count) => write!(formatter, "the CH command line has {count} --vsock"),
            Self::RunDir(error) => write!(formatter, "reading the VM run dir: {error}"),
            Self::Extra(name) => write!(formatter, "an unbound listener: {}", name.display()),
            Self::Missing(port) => write!(formatter, "no listener for port {}", port.number()),
        }
    }
}

impl std::error::Error for EgressAssertFailed {}

/// The command line file of the process `pid`. The pid is the one of
/// `Launched::id`: CH keeps it after the exec of `systemd-run`.
#[must_use]
pub fn cmdline_of(pid: u32) -> PathBuf {
    PathBuf::from(format!("/proc/{pid}/cmdline"))
}

/// Checks the CH process whose command line is in `cmdline` and the run
/// directory `run` against the host ports in `bound`. Run it after CH is
/// up: before its exec, the pid holds `vmm launch` with no `--vsock`, so a
/// boot check fails closed.
///
/// # Errors
///
/// The first `EgressAssertFailed` that the check finds.
pub fn check(
    cmdline: &Path,
    start: &Start,
    run: &VmRunDir,
    bound: &[HostPort],
) -> Result<(), EgressAssertFailed> {
    let bytes = fs::read(cmdline).map_err(EgressAssertFailed::Cmdline)?;
    check_args(&bytes, start)?;
    check_listeners(run, bound)
}

/// Part 1: the arguments, NUL-separated as in `/proc/<pid>/cmdline`.
fn check_args(bytes: &[u8], start: &Start) -> Result<(), EgressAssertFailed> {
    let args = || bytes.split(|&byte| byte == 0);
    if args().any(|arg| arg.starts_with(NET)) {
        return Err(EgressAssertFailed::Net);
    }
    let is_vsock = |arg: &[u8]| {
        arg.strip_prefix(VSOCK)
            .is_some_and(|rest| rest.is_empty() || rest.starts_with(b"="))
    };
    let vsock = args().filter(|arg| is_vsock(arg)).count();
    let wanted = match start {
        Start::Boot { .. } => 1,
        Start::Restore => 0,
    };
    if vsock == wanted {
        Ok(())
    } else {
        Err(EgressAssertFailed::Vsock(vsock))
    }
}

/// Part 2: each `vsock.sock_*` entry of the run directory is the listener
/// of a bound port, and each bound port has one. Other entries (the CH
/// sockets and `serial.log`) are not listeners.
fn check_listeners(run: &VmRunDir, bound: &[HostPort]) -> Result<(), EgressAssertFailed> {
    let name = |path: PathBuf| path.file_name().map(OsString::from).unwrap_or_default();
    let mut prefix = name(run.vsock_socket());
    prefix.push("_");
    let wanted: Vec<(HostPort, OsString)> = bound
        .iter()
        .map(|&port| (port, name(run.host_listener(port))))
        .collect();
    let mut found = Vec::new();
    for entry in fs::read_dir(run.path()).map_err(EgressAssertFailed::RunDir)? {
        let entry = entry.map_err(EgressAssertFailed::RunDir)?.file_name();
        if !entry
            .as_encoded_bytes()
            .starts_with(prefix.as_encoded_bytes())
        {
            continue;
        }
        if !wanted.iter().any(|(_, listener)| *listener == entry) {
            return Err(EgressAssertFailed::Extra(entry));
        }
        found.push(entry);
    }
    match wanted
        .iter()
        .find(|(_, listener)| !found.contains(listener))
    {
        Some(&(port, _)) => Err(EgressAssertFailed::Missing(port)),
        None => Ok(()),
    }
}
