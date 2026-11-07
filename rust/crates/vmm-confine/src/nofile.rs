//! The open file limit of the vmm (`RLIMIT_NOFILE`).
//!
//! A guest makes the vmm open fds: each plane splice holds a Unix stream
//! and a TCP stream (plan PR V6, decision CH62 rule 14). systemd starts a
//! service with a soft limit of 1024 by default, and the vmm unit sets no
//! `LimitNOFILE=`. So a few busy guests could use every fd of the vmm,
//! and each other VM and `vmm.sock` would then fail with `EMFILE`.
//!
//! `raise` sets the soft limit to the need of every VM before the vmm
//! serves. It leaves the hard limit as it is, and it refuses a hard limit
//! below the need, so the vmm never runs with fewer fds than its caps
//! allow. Each CH that the vmm launches inherits the soft limit. Decision
//! CH67 rule 4 sets the raise, its headroom of 4096 and the refusal of a
//! short hard limit, with no `LimitNOFILE=` line in the unit.

use std::fmt;
use std::io;

use rustix::process::{Resource, Rlimit, getrlimit, setrlimit};

/// Why the vmm cannot hold the fds of its caps.
#[derive(Debug)]
pub enum NofileError {
    /// The hard limit is below the need.
    Short {
        /// The hard limit.
        hard: u64,
        /// The fds that the vmm needs.
        need: u64,
    },
    /// The kernel refused the new soft limit.
    Set(io::Error),
}

impl fmt::Display for NofileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Short { hard, need } => write!(
                formatter,
                "the hard RLIMIT_NOFILE is {hard}, and the vmm needs {need} fds"
            ),
            Self::Set(error) => write!(formatter, "setting RLIMIT_NOFILE: {error}"),
        }
    }
}

impl std::error::Error for NofileError {}

/// Sets the soft `RLIMIT_NOFILE` to at least `need`. A soft limit at or
/// above `need` stays. A `None` limit is `RLIM_INFINITY`.
///
/// # Errors
///
/// `Short` when the hard limit is below `need`, `Set` when the kernel
/// refuses the soft limit.
pub fn raise(need: u64) -> Result<(), NofileError> {
    let limit = getrlimit(Resource::Nofile);
    if let Some(hard) = limit.maximum
        && hard < need
    {
        return Err(NofileError::Short { hard, need });
    }
    if let Some(soft) = limit.current
        && soft < need
    {
        let raised = Rlimit {
            current: Some(need),
            maximum: limit.maximum,
        };
        setrlimit(Resource::Nofile, raised).map_err(|errno| NofileError::Set(errno.into()))?;
    }
    Ok(())
}
