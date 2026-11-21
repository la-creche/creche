//! The published peer check for a service on the attach interface of
//! ci-runner (design section 6.3). `README.md` of this crate is the guide
//! for a service author: what to call when, which unit settings the
//! service needs, and what an accepted check proves.
//!
//! | Item | What it does |
//! |---|---|
//! | `check(fd, slot)` | Checks a guest connection that the daemon passed: its peer is a live process in a unit `ci-runner-slot<slot>@<instance>.service` of the slice of the job VMs. Gives a `SlotPeer` or a `Refusal`. Linux only. |
//! | `check_daemon(fd)` | Checks a control connection: its peer runs as the user `ci-runner`. Gives a `DaemonRefusal` on a refusal. Linux only. |
//! | `SlotUnit::parse` | The pure parse of the cgroup path of a slot unit, which step 2 of `check` uses. |
//!
//! The crate depends on no crate of the runner but `ci-runner-sys`, which
//! holds the two calls with no safe wrapper. It names no consumer.

#[cfg(all(
    target_os = "linux",
    not(any(target_arch = "x86_64", target_arch = "aarch64"))
))]
compile_error!("ci-runner-peer knows the kernel ABI of x86_64 and aarch64 only");

#[cfg(target_os = "linux")]
mod check;
mod daemon;
mod unit;

use std::fmt;
use std::io;

#[cfg(target_os = "linux")]
pub use check::{SlotPeer, check, check_daemon};
pub use daemon::{DAEMON_USER, DaemonRefusal, UserFault};
pub use unit::{CgroupFault, CgroupId, PathFault, SLICE_PATH, Slot, SlotUnit, UNIT_PREFIX};

/// Why `check` refused a passed fd. Each variant is one step of the
/// check, in its order.
#[derive(Debug)]
pub enum Refusal {
    /// Step 1: `SO_PEERPIDFD` failed. A kernel before 6.5 gives
    /// `ENOPROTOOPT`, and a socket with no peer `ENODATA`.
    Pidfd(io::Error),
    /// Step 2: the cgroup of the peer is no unit of the slot.
    Cgroup(CgroupFault),
    /// Step 3: the peer is not alive at the moment of the check. A peer
    /// that has exited gives it also when step 2 failed.
    Live(LiveFault),
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Pidfd(error) => write!(f, "step 1, SO_PEERPIDFD failed: {error}"),
            Self::Cgroup(fault) => write!(f, "step 2, the cgroup: {fault}"),
            Self::Live(fault) => write!(f, "step 3, the liveness: {fault}"),
        }
    }
}

impl std::error::Error for Refusal {}

/// Why the peer is not alive (step 3 of `check`).
#[derive(Debug)]
pub enum LiveFault {
    /// The poll of the pidfd failed.
    Poll(io::Error),
    /// The pidfd polled ready: the peer has exited.
    Exited,
}

impl fmt::Display for LiveFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Poll(error) => write!(f, "the poll of the pidfd failed: {error}"),
            Self::Exited => f.write_str("the pidfd polled ready, so the peer has exited"),
        }
    }
}
