//! The power-off of the VM: SIGKILL to every process left, sync, a
//! read-only remount of the session disk, then `reboot(RB_POWER_OFF)`
//! (decisions CH19 rule 3, CH62 rule 9). Every fatal
//! path of PID 1 ends here, because the guest kernel has
//! `PANIC_TIMEOUT=0`: an exit of PID 1 panics the kernel and the VM hangs
//! (fact 53). CH exits when the guest powers off.

use std::process::ExitCode;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_wire::guest::SESSION_DISK_MOUNT;
use rustix::io::Errno;
use rustix::mount::mount_remount;
use rustix::process::{Pid, Signal, WaitOptions, kill_process_group, wait};
use rustix::system::{RebootCommand, reboot};

/// How long the power-off waits for the killed processes to be gone
/// before the sync.
// CONTRACT-QUESTION: no row decides this wait. CH62 rule 9 names
// SIGKILL, sync, power off with no wait between. The reading taken: up
// to 1 s, so a process in D state cannot hang the power-off, and one
// console line names a wait that ran out. A change costs this constant.
const KILL_WAIT: Duration = Duration::from_secs(1);

/// The pause between two reap tries of `reap_all`.
const REAP_PAUSE: Duration = Duration::from_millis(10);

/// Kills every process left, syncs every file system, remounts the
/// session disk read-only and powers the VM off. It returns only when the
/// power-off call fails. PID 1 then exits, and the vmm stop chain ends
/// the hung VM (plan risk 9).
pub fn off() -> ExitCode {
    crate::say("power off");
    // `kill(-1, SIGKILL)`: rustix sends a signal to the group of
    // `Pid::INIT` as `kill(-1)`, which reaches every process but PID 1
    // and the kernel threads. ESRCH means that no process was left.
    match kill_process_group(Pid::INIT, Signal::KILL) {
        Ok(()) => crate::say("every process killed"),
        Err(Errno::SRCH) => {}
        Err(error) => crate::say(&format!("kill of every process failed: {error}")),
    }
    // `kill` only queues the signal. A target can still finish a write
    // on another vCPU and holds its files until it exits. So the sync
    // waits until no child of PID 1 is left: then no process writes the
    // session disk after the sync, and the remount finds no file open to
    // write.
    reap_all();
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

/// Reaps each child of PID 1 until none is left (ECHILD), for up to
/// `KILL_WAIT`. Each process left is a child of PID 1 once its parent is
/// gone. The reaper thread may reap some of them first, and the loop
/// still ends on ECHILD.
fn reap_all() {
    let end = Instant::now() + KILL_WAIT;
    loop {
        match wait(WaitOptions::NOHANG) {
            Ok(Some(_)) | Err(Errno::INTR) => {}
            Ok(None) if Instant::now() >= end => {
                crate::say(&format!("processes left after {KILL_WAIT:?}"));
                return;
            }
            Ok(None) => sleep(REAP_PAUSE),
            Err(Errno::CHILD) => return,
            Err(error) => {
                crate::say(&format!("reap of every process failed: {error}"));
                return;
            }
        }
    }
}
