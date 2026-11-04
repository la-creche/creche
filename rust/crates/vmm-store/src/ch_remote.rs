//! The Cloud Hypervisor API of one VM, driven through `ch-remote`.
//!
//! `ch-remote` ships in the vmm release row next to `cloud-hypervisor`
//! (decision CH7-5). The vmm runs it with `--api-socket` for pause, resume,
//! snapshot, info and shutdown-vmm. The vmm has an HTTP server only and no
//! HTTP client (decision CH26), so this module adds no client crate.
//!
//! Each call waits for `ch-remote` to end, up to a timeout that the caller
//! passes. At the timeout the call kills `ch-remote` and fails. The kill
//! does not cancel the request: Cloud Hypervisor may still complete it.
//!
//! Decision CH44 rule 3: the timeout of a call comes from its caller. This
//! crate names no value. A pause takes about 4 ms (fact 72) and a 16 GiB
//! snapshot about 12 s (gap G16).

use std::ffi::OsString;
use std::fmt;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Output, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::layout::VmRunDir;

/// The time between two checks for the end of `ch-remote`. A pause takes
/// about 4 ms (fact 72).
const POLL: Duration = Duration::from_millis(2);

/// The most bytes that a call keeps of stdout or stderr. An `info` reply
/// is a few KiB.
const REPLY_MAX: u64 = 1 << 20;

/// A command of `ch-remote`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChCommand {
    /// `pause`: stop the vCPUs.
    Pause,
    /// `resume`: start the vCPUs again.
    Resume,
    /// `snapshot <url>`: write the state of a paused VM to a dir.
    Snapshot,
    /// `info`: read the VM state.
    Info,
    /// `shutdown-vmm`: end the Cloud Hypervisor process.
    ShutdownVmm,
}

impl ChCommand {
    /// The command name on the `ch-remote` command line.
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Self::Pause => "pause",
            Self::Resume => "resume",
            Self::Snapshot => "snapshot",
            Self::Info => "info",
            Self::ShutdownVmm => "shutdown-vmm",
        }
    }
}

/// The state of a VM, as `info` reports it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub enum VmState {
    /// Made and not booted.
    Created,
    /// The vCPUs run.
    Running,
    /// The guest is off.
    Shutdown,
    /// The vCPUs are stopped.
    Paused,
    /// A debugger stopped the vCPUs.
    BreakPoint,
}

/// The part of the `info` reply that the vmm reads. Every other field is
/// skipped.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub struct VmInfo {
    /// The VM state.
    pub state: VmState,
}

/// Why a call of `ch-remote` failed.
#[derive(Debug)]
pub enum ChRemoteError {
    /// A start, a wait or a read of `ch-remote` failed.
    Io {
        /// The `ch-remote` program.
        program: PathBuf,
        /// Why the step failed.
        source: io::Error,
    },
    /// The snapshot dir is not an absolute UTF-8 path, so it cannot be a
    /// `file://` URL.
    BadDestination(PathBuf),
    /// `ch-remote` did not end before the timeout. The call killed it.
    Timeout {
        /// The command.
        command: ChCommand,
        /// The timeout.
        timeout: Duration,
    },
    /// `ch-remote` ended with a failure.
    Failed {
        /// The command.
        command: ChCommand,
        /// The exit status of `ch-remote`.
        status: ExitStatus,
        /// What `ch-remote` wrote to stderr.
        stderr: String,
    },
    /// The `info` reply is not a VM info.
    NotInfo(serde_json::Error),
}

impl fmt::Display for ChRemoteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { program, .. } => write!(formatter, "cannot run {}", program.display()),
            Self::BadDestination(dir) => {
                write!(formatter, "{} is not an absolute UTF-8 path", dir.display())
            }
            Self::Timeout { command, timeout } => write!(
                formatter,
                "ch-remote {} did not end in {timeout:?}",
                command.name()
            ),
            Self::Failed {
                command,
                status,
                stderr,
            } => write!(
                formatter,
                "ch-remote {} ended with {status}: {}",
                command.name(),
                stderr.trim()
            ),
            Self::NotInfo(_) => formatter.write_str("the info reply is not a VM info"),
        }
    }
}

impl std::error::Error for ChRemoteError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::NotInfo(source) => Some(source),
            Self::BadDestination(_) | Self::Timeout { .. } | Self::Failed { .. } => None,
        }
    }
}

/// The `ch-remote` program of the vmm.
#[derive(Debug, Clone)]
pub struct ChRemote {
    program: PathBuf,
}

impl ChRemote {
    /// `program` is the shipped `ch-remote` (decision CH7-5). The vmm
    /// config passes it in, so no path is compiled in.
    #[must_use]
    pub fn new(program: PathBuf) -> Self {
        Self { program }
    }

    /// Pauses the VM of `vm`.
    ///
    /// # Errors
    ///
    /// `Io`, `Timeout` or `Failed`.
    pub fn pause(&self, vm: &VmRunDir, timeout: Duration) -> Result<(), ChRemoteError> {
        self.run(vm, ChCommand::Pause, None, timeout).map(drop)
    }

    /// Resumes the VM of `vm`.
    ///
    /// # Errors
    ///
    /// `Io`, `Timeout` or `Failed`.
    pub fn resume(&self, vm: &VmRunDir, timeout: Duration) -> Result<(), ChRemoteError> {
        self.run(vm, ChCommand::Resume, None, timeout).map(drop)
    }

    /// Writes a snapshot of the paused VM of `vm` into `dir`. Cloud
    /// Hypervisor needs `dir` to exist. It reads the URL `file://<dir>`
    /// with no decode, so `dir` must be an absolute UTF-8 path.
    ///
    /// # Errors
    ///
    /// `BadDestination`, `Io`, `Timeout` or `Failed`.
    pub fn snapshot(
        &self,
        vm: &VmRunDir,
        dir: &Path,
        timeout: Duration,
    ) -> Result<(), ChRemoteError> {
        let Some(text) = dir.to_str().filter(|_| dir.is_absolute()) else {
            return Err(ChRemoteError::BadDestination(dir.to_owned()));
        };
        let url = OsString::from(format!("file://{text}"));
        self.run(vm, ChCommand::Snapshot, Some(url), timeout)
            .map(drop)
    }

    /// Reads the state of the VM of `vm`.
    ///
    /// # Errors
    ///
    /// `Io`, `Timeout`, `Failed` or `NotInfo`.
    pub fn info(&self, vm: &VmRunDir, timeout: Duration) -> Result<VmInfo, ChRemoteError> {
        let stdout = self.run(vm, ChCommand::Info, None, timeout)?;
        serde_json::from_slice(&stdout).map_err(ChRemoteError::NotInfo)
    }

    /// Ends the Cloud Hypervisor process of `vm`. A graceful end removes
    /// `vsock.sock` and `api.sock` (fact 75).
    ///
    /// # Errors
    ///
    /// `Io`, `Timeout` or `Failed`.
    pub fn shutdown_vmm(&self, vm: &VmRunDir, timeout: Duration) -> Result<(), ChRemoteError> {
        self.run(vm, ChCommand::ShutdownVmm, None, timeout)
            .map(drop)
    }

    /// Runs `ch-remote --api-socket <socket> <command> [<arg>]` and
    /// returns its stdout.
    fn run(
        &self,
        vm: &VmRunDir,
        command: ChCommand,
        arg: Option<OsString>,
        timeout: Duration,
    ) -> Result<Vec<u8>, ChRemoteError> {
        let io_error = |source| ChRemoteError::Io {
            program: self.program.clone(),
            source,
        };
        let mut child = Command::new(&self.program)
            .arg("--api-socket")
            .arg(vm.api_socket())
            .arg(command.name())
            .args(arg)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(io_error)?;
        let Some(output) = output_until(&mut child, timeout).map_err(io_error)? else {
            return Err(ChRemoteError::Timeout { command, timeout });
        };
        if !output.status.success() {
            return Err(ChRemoteError::Failed {
                command,
                status: output.status,
                stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            });
        }
        Ok(output.stdout)
    }
}

/// Waits up to `timeout` for `child` to end and returns its exit status,
/// stdout and stderr, each cut at `REPLY_MAX`. `None` when the timeout
/// passed first. A timeout or a failed wait kills and reaps `child`. The
/// readers end when the pipes close, and the call does not wait for them.
/// The key check of `cloud-hypervisor --version` uses it too (PR D10).
pub(crate) fn output_until(child: &mut Child, timeout: Duration) -> io::Result<Option<Output>> {
    let waited = take_readers(child).and_then(|readers| Ok((readers, wait_until(child, timeout)?)));
    let ((stdout, stderr), status) = match waited {
        Ok((readers, Some(status))) => (readers, status),
        Ok((_, None)) => {
            stop(child);
            return Ok(None);
        }
        Err(error) => {
            stop(child);
            return Err(error);
        }
    };
    Ok(Some(Output {
        status,
        stdout: join(stdout)?,
        stderr: join(stderr).unwrap_or_default(),
    }))
}

/// A thread that reads one pipe to its end.
type Reader = JoinHandle<io::Result<Vec<u8>>>;

/// Starts a reader for stdout and one for stderr of `child`.
fn take_readers(child: &mut Child) -> io::Result<(Reader, Reader)> {
    let stdout = child.stdout.take().ok_or(io::ErrorKind::BrokenPipe)?;
    let stderr = child.stderr.take().ok_or(io::ErrorKind::BrokenPipe)?;
    Ok((read_capped(stdout)?, read_capped(stderr)?))
}

/// Reads `pipe` to its end on a new thread, so a full pipe never blocks
/// `ch-remote`. The thread keeps `REPLY_MAX` bytes. Every byte from
/// another process is untrusted (root `AGENTS.md`), so more bytes give
/// `FileTooLarge`.
fn read_capped(mut pipe: impl Read + Send + 'static) -> io::Result<Reader> {
    thread::Builder::new().spawn(move || {
        let mut bytes = Vec::new();
        pipe.by_ref().take(REPLY_MAX).read_to_end(&mut bytes)?;
        if io::copy(&mut pipe, &mut io::sink())? > 0 {
            return Err(io::ErrorKind::FileTooLarge.into());
        }
        Ok(bytes)
    })
}

/// The bytes of a reader. A reader that panicked gives an error.
fn join(reader: Reader) -> io::Result<Vec<u8>> {
    reader
        .join()
        .unwrap_or_else(|_| Err(io::ErrorKind::Other.into()))
}

/// Kills `child` and reaps it.
fn stop(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

/// Waits for `child` to end. `None` when `timeout` passed first.
fn wait_until(child: &mut Child, timeout: Duration) -> io::Result<Option<ExitStatus>> {
    let start = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(Some(status));
        }
        if start.elapsed() >= timeout {
            return Ok(None);
        }
        thread::sleep(POLL);
    }
}
