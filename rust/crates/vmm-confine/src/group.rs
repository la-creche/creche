//! The kill of a process group that the vmm spawned (decision CH69 rule
//! 14). `git upload-pack` of the mirror runs `git pack-objects` as a child
//! of its own. A SIGKILL of the `upload-pack` pid alone skips the cleanup
//! of git, so `pack-objects` would run on as an orphan. The vmm spawns
//! each `upload-pack` as the leader of a new process group, and the end
//! of its link kills the whole group.
//!
//! The caller kills the group before it reaps the leader. An unreaped
//! leader holds its pid, so the group id names no other process.

use std::io;

use rustix::io::Errno;
use rustix::process::{Pid, Signal, kill_process_group};

/// Sends SIGKILL to the process group whose leader has the pid `leader`.
///
/// # Errors
///
/// The error of `kill`. ESRCH means that no process of the group is left.
/// A pid of 0 or past `i32::MAX` is `EINVAL`.
pub fn kill(leader: u32) -> io::Result<()> {
    let pid = i32::try_from(leader)
        .ok()
        .and_then(Pid::from_raw)
        .ok_or(Errno::INVAL)?;
    kill_process_group(pid, Signal::KILL).map_err(io::Error::from)
}
