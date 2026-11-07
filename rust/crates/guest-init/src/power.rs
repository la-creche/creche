//! The power-off of the VM: SIGKILL to every process left, sync, a
//! read-only remount of the session disk, then `reboot(RB_POWER_OFF)`
//! (decisions CH19 rule 3, CH62 rule 9). Every fatal
//! path of PID 1 ends here, because the guest kernel has
//! `PANIC_TIMEOUT=0`: an exit of PID 1 panics the kernel and the VM hangs
//! (fact 53). CH exits when the guest powers off.

use std::process::ExitCode;

use creche_wire::guest::SESSION_DISK_MOUNT;
use rustix::io::Errno;
use rustix::mount::mount_remount;
use rustix::process::{Pid, Signal, kill_process_group};
use rustix::system::{RebootCommand, reboot};

/// Kills every process left, syncs every file system, remounts the
/// session disk read-only and powers the VM off. It returns only when the
/// power-off call fails. PID 1 then exits, and the vmm stop chain ends
/// the hung VM (plan risk 9).
pub fn off() -> ExitCode {
    crate::say("power off");
    // `kill(-1, SIGKILL)`: rustix sends a signal to the group of
    // `Pid::INIT` as `kill(-1)`, which reaches every process but PID 1
    // and the kernel threads. So no process writes the session disk
    // after the sync, and the remount finds no file open to write.
    // ESRCH means that no process was left.
    match kill_process_group(Pid::INIT, Signal::KILL) {
        Ok(()) => crate::say("every process killed"),
        Err(Errno::SRCH) => {}
        Err(error) => crate::say(&format!("kill of every process failed: {error}")),
    }
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
