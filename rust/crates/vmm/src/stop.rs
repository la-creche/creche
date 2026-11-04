//! The stop of one VM (decisions CH19 (3), CH34 rule 5). The vmm sends
//! `stop` on the control link and waits `DROP_STOP_WAIT` for Cloud
//! Hypervisor to exit. Then it sends SIGTERM through the launch handle of
//! `creche-vmm-confine` and waits `DROP_SIGTERM_GRACE`. Then it sends
//! SIGKILL and waits `KILL_WAIT` at most. A kill is a power cut for the
//! guest, and no error.
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
use tokio::time::{Instant, sleep, timeout_at};

use crate::control::{ControlError, ControlLink};

/// The pause between two status checks of the process. No row sets it.
/// Only the vmm reads it, so a change costs one constant.
const EXIT_POLL: Duration = Duration::from_millis(10);

/// The longest wait for the exit after SIGKILL. A process in the D state
/// can outlive SIGKILL for a while.
// CONTRACT-QUESTION: no row sets this wait, or what a stop answers when
// the process outlives it. Decision CH33 rule 11 bounds a drop at about
// 35 s, and decision CH34 rule 5 gives the client 60 s. The narrowest
// reading bounds the wait, so a stop ends within 40 s, inside the wait of
// the client. Past it the stop gives `StopError::Hung` and removes no
// file. AGENTS.md lists it in Known gaps.
pub const KILL_WAIT: Duration = Duration::from_secs(5);

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
    /// The process did not exit within `KILL_WAIT` after SIGKILL.
    Hung,
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
            Self::Hung => formatter.write_str("the VM process outlived SIGKILL"),
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
// Decision CH50 rule 3: a step that did not go out skips its wait. That
// covers no open link, a failed write of `stop`, and a failed SIGTERM.
// The write of `stop` gets a timeout counted inside `DROP_STOP_WAIT`: a
// full send buffer can stall the write for ever. A stalled write counts as
// not sent, and SIGTERM still follows `DROP_STOP_WAIT` after the start.
async fn end(
    link: Option<&impl StopLink>,
    process: &mut impl VmProcess,
) -> Result<Stopped, StopError> {
    if process.exited().map_err(StopError::Wait)? {
        return Ok(Stopped::Gone);
    }
    let deadline = Instant::now() + DROP_STOP_WAIT;
    let sent = match link {
        Some(link) => matches!(timeout_at(deadline, link.send_stop()).await, Ok(Ok(()))),
        None => false,
    };
    if sent && exits_by(process, deadline).await? {
        return Ok(Stopped::PoweredOff);
    }
    if process.terminate().is_ok() && exits_within(process, DROP_SIGTERM_GRACE).await? {
        return Ok(Stopped::Terminated);
    }
    process.kill().map_err(StopError::Kill)?;
    if exits_within(process, KILL_WAIT).await? {
        return Ok(Stopped::Killed);
    }
    Err(StopError::Hung)
}

/// Whether `process` exits within `wait`.
async fn exits_within(process: &mut impl VmProcess, wait: Duration) -> Result<bool, StopError> {
    exits_by(process, Instant::now() + wait).await
}

/// Whether `process` exits by `deadline`.
async fn exits_by(process: &mut impl VmProcess, deadline: Instant) -> Result<bool, StopError> {
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
