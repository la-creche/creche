//! The stop of one VM (decisions CH19 (3), CH34 rule 5). The vmm sends
//! `stop` on the control link and waits `DROP_STOP_WAIT` for Cloud
//! Hypervisor to exit. Then it sends SIGTERM through the launch handle of
//! `creche-vmm-confine` and waits `DROP_SIGTERM_GRACE`. Then it sends
//! SIGKILL. A kill is a power cut for the guest, and no error.
//!
//! After the exit the vmm removes `api.sock.lock`, which Cloud Hypervisor
//! cannot remove under Landlock (fact 75). A stop takes no snapshot, so
//! the next start of the VM is a cold boot (decision CH19 (5)).

use std::fmt;
use std::io;
use std::time::Duration;

use creche_vmm_confine::spawner::Launched;
use creche_vmm_store::layout::VmRunDir;
use creche_vmm_store::stale::{StaleError, remove_stale};
use creche_wire::{DROP_SIGTERM_GRACE, DROP_STOP_WAIT};
use tokio::task::spawn_blocking;
use tokio::time::{Instant, sleep};

use crate::control::{ControlError, ControlLink};

/// The pause between two status checks of the process. No row sets it.
/// Only the vmm reads it, so a change costs one constant.
const EXIT_POLL: Duration = Duration::from_millis(10);

/// The link that carries the graceful stop. `ControlLink` is the one of
/// the vmm, and a test gives a fake.
pub trait StopLink {
    /// Sends `{"stop":{}}` (decision CH36 G11).
    fn send_stop(&self) -> impl Future<Output = Result<(), ControlError>> + Send;
}

impl StopLink for ControlLink {
    fn send_stop(&self) -> impl Future<Output = Result<(), ControlError>> + Send {
        self.stop()
    }
}

/// The process of one Cloud Hypervisor. `Launched` is the one of the vmm,
/// and a test gives a fake.
pub trait VmProcess {
    /// Whether the process has exited. A check reaps an exit.
    ///
    /// # Errors
    ///
    /// The status check failed.
    fn exited(&mut self) -> io::Result<bool>;

    /// Sends SIGTERM.
    ///
    /// # Errors
    ///
    /// The signal failed.
    fn terminate(&mut self) -> io::Result<()>;

    /// Sends SIGKILL.
    ///
    /// # Errors
    ///
    /// The signal failed.
    fn kill(&mut self) -> io::Result<()>;
}

impl VmProcess for Launched {
    fn exited(&mut self) -> io::Result<bool> {
        Ok(self.try_wait()?.is_some())
    }

    fn terminate(&mut self) -> io::Result<()> {
        Self::terminate(self)
    }

    fn kill(&mut self) -> io::Result<()> {
        Self::kill(self)
    }
}

/// How the process ended. Each one is a stop that worked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stopped {
    /// The process had exited before the stop.
    Gone,
    /// Guest init powered the VM off after `stop`.
    PoweredOff,
    /// The process exited after SIGTERM.
    Terminated,
    /// SIGKILL ended the process: a power cut for the guest.
    Killed,
}

/// Why a stop did not end with the process gone and its files removed.
#[derive(Debug)]
pub enum StopError {
    /// A status check of the process failed.
    Wait(io::Error),
    /// SIGKILL failed, so the process may still run.
    Kill(io::Error),
    /// The removal of a stale file failed.
    Stale(StaleError),
    /// The removal task panicked.
    Removal,
}

impl fmt::Display for StopError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Wait(error) => write!(formatter, "checking the VM process: {error}"),
            Self::Kill(error) => write!(formatter, "sending SIGKILL: {error}"),
            Self::Stale(error) => write!(formatter, "after the stop: {error}"),
            Self::Removal => formatter.write_str("the removal of the stale files panicked"),
        }
    }
}

impl std::error::Error for StopError {}

/// Stops the VM of `process`, then removes its stale files in `vm`.
/// `link` is its control link, if one is open.
///
/// # Errors
///
/// `StopError`. A kill is no error.
pub async fn stop(
    link: Option<&impl StopLink>,
    process: &mut impl VmProcess,
    vm: &VmRunDir,
) -> Result<Stopped, StopError> {
    let stopped = end(link, process).await?;
    // The process is gone, so no Cloud Hypervisor holds the files. This
    // also removes the `vsock.sock` that a kill leaves (fact 75).
    let vm = vm.clone();
    spawn_blocking(move || remove_stale(&vm))
        .await
        .map_err(|_| StopError::Removal)?
        .map_err(StopError::Stale)?;
    Ok(stopped)
}

/// Ends the process: `stop`, SIGTERM, then SIGKILL (decision CH34 rule 5).
// CONTRACT-QUESTION: no row says what a stop does when `stop` does not go
// out (no open link, or a failed write) or SIGTERM fails. The wait after
// a step only gives that step time to work. So this reading skips the wait
// of a step that did not go out and goes to the next step at once.
async fn end(
    link: Option<&impl StopLink>,
    process: &mut impl VmProcess,
) -> Result<Stopped, StopError> {
    if process.exited().map_err(StopError::Wait)? {
        return Ok(Stopped::Gone);
    }
    let sent = match link {
        Some(link) => link.send_stop().await.is_ok(),
        None => false,
    };
    if sent && exits_within(process, DROP_STOP_WAIT).await? {
        return Ok(Stopped::PoweredOff);
    }
    if process.terminate().is_ok() && exits_within(process, DROP_SIGTERM_GRACE).await? {
        return Ok(Stopped::Terminated);
    }
    process.kill().map_err(StopError::Kill)?;
    // SIGKILL ends the process, so this wait has no bound.
    while !process.exited().map_err(StopError::Wait)? {
        sleep(EXIT_POLL).await;
    }
    Ok(Stopped::Killed)
}

/// Whether `process` exits within `wait`.
async fn exits_within(process: &mut impl VmProcess, wait: Duration) -> Result<bool, StopError> {
    let deadline = Instant::now() + wait;
    loop {
        if process.exited().map_err(StopError::Wait)? {
            return Ok(true);
        }
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Ok(false);
        }
        sleep(left.min(EXIT_POLL)).await;
    }
}
