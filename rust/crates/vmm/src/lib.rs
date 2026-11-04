//! The vmm: the one process that owns every Cloud Hypervisor VM of the
//! playpen (decision CH9). It serves the host API (decision CH20) and
//! composes two libraries: `creche-vmm-confine` and `creche-vmm-store`.

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

/// Runs the host API server until it stops. The caller runs it on the
/// multi-thread tokio runtime (decision CH36, gap G21). Blocking work goes
/// to `spawn_blocking`.
pub async fn serve() -> Result<(), ServeError> {
    Err(ServeError::NotBuilt)
}

#[cfg(test)]
mod tests {
    use tokio::runtime::Builder;

    use super::{ServeError, serve};

    #[test]
    fn serve_refuses_until_built() {
        let runtime = Builder::new_current_thread().build().unwrap();
        assert_eq!(runtime.block_on(serve()), Err(ServeError::NotBuilt));
    }
}
