//! The vmm: the one process that owns every Cloud Hypervisor VM of the
//! playpen (decision CH9). It serves the host API (decision CH20) and
//! composes the two crates below it: `creche-vmm-confine` and
//! `creche-vmm-store`.

use std::fmt;

/// Why the host API server stopped or did not start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServeError {
    /// The server has no code yet. A start that reaches it fails closed
    /// (owner item 86).
    NotBuilt,
}

impl fmt::Display for ServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let reason = match self {
            Self::NotBuilt => "the host API server is not built yet",
        };
        formatter.write_str(reason)
    }
}

impl std::error::Error for ServeError {}

/// Runs the host API server until it stops.
pub fn serve() -> Result<(), ServeError> {
    Err(ServeError::NotBuilt)
}

#[cfg(test)]
mod tests {
    use super::{ServeError, serve};

    #[test]
    fn serve_refuses_until_built() {
        assert_eq!(serve(), Err(ServeError::NotBuilt));
    }
}
