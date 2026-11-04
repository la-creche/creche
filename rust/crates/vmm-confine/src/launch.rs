//! The launch step `vmm launch`. The vmm runs the program again in a child
//! process with this mode, and the child starts one Cloud Hypervisor
//! process (decision CH8-2).

use std::convert::Infallible;
use std::fmt;

/// Why the launch step did not start the Cloud Hypervisor process.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LaunchError {
    /// The launch step has no code yet. A start that reaches it fails
    /// closed (owner item 86).
    NotBuilt,
}

impl fmt::Display for LaunchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let reason = match self {
            Self::NotBuilt => "the launch step is not built yet",
        };
        formatter.write_str(reason)
    }
}

impl std::error::Error for LaunchError {}

/// Runs the launch step. A launch that works replaces this process with
/// the next program, so this function returns only an error.
pub fn run() -> Result<Infallible, LaunchError> {
    Err(LaunchError::NotBuilt)
}

#[cfg(test)]
mod tests {
    use super::{LaunchError, run};

    #[test]
    fn run_refuses_until_built() {
        assert_eq!(run(), Err(LaunchError::NotBuilt));
    }
}
