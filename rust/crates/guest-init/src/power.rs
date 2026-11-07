//! The power-off of the VM: sync, a read-only remount of the session
//! disk, then `reboot(RB_POWER_OFF)` (decision CH19 rule 3). Every fatal
//! path of PID 1 ends here, because the guest kernel has
//! `PANIC_TIMEOUT=0`: an exit of PID 1 panics the kernel and the VM hangs
//! (fact 53). CH exits when the guest powers off.

use std::process::ExitCode;

use creche_wire::guest::SESSION_DISK_MOUNT;
use rustix::mount::mount_remount;
use rustix::system::{RebootCommand, reboot};

/// Syncs every file system, remounts the session disk read-only and
/// powers the VM off. It returns only when the power-off call fails. PID
/// 1 then exits, and the vmm stop chain ends the hung VM (plan risk 9).
pub fn off() -> ExitCode {
    crate::say("power off");
    rustix::fs::sync();
    // The read-only remount flushes the ext4 journal and marks the file
    // system clean, so the next boot finds no journal to replay. Before
    // the switch root, the session disk is not at its mount point, and
    // the remount fails with its own console line.
    match mount_remount(SESSION_DISK_MOUNT, crate::boot::session_remount_flags(), "") {
        Ok(()) => crate::say("session remounted ro"),
        Err(error) => crate::say(&format!("session remount ro failed: {error}")),
    }
    if let Err(error) = reboot(RebootCommand::PowerOff) {
        crate::say(&format!("power off failed: {error}"));
    }
    ExitCode::FAILURE
}
