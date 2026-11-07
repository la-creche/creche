//! The power-off of the VM: sync, then `reboot(RB_POWER_OFF)` (decision
//! CH19 rule 3). Every fatal path of PID 1 ends here, because the guest
//! kernel has `PANIC_TIMEOUT=0`: an exit of PID 1 panics the kernel and
//! the VM hangs (fact 53). CH exits when the guest powers off.

use std::process::ExitCode;

use rustix::system::{RebootCommand, reboot};

/// Syncs every file system, then powers the VM off. It returns only when
/// the power-off call fails. PID 1 then exits, and the vmm stop chain
/// ends the hung VM (plan risk 9).
pub fn off() -> ExitCode {
    crate::say("power off");
    rustix::fs::sync();
    if let Err(error) = reboot(RebootCommand::PowerOff) {
        crate::say(&format!("power off failed: {error}"));
    }
    ExitCode::FAILURE
}
