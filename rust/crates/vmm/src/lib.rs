//! The vmm: the one process that owns every Cloud Hypervisor VM of the
//! playpen (decision CH9). It serves the host API (decision CH20) and
//! composes two libraries: `creche-vmm-confine` and `creche-vmm-store`.

use std::{fmt, io};

use config::Config;

pub mod config;
pub mod control;
pub mod handlers;
pub mod relay;
pub mod restore;
pub mod server;
pub mod shutdown;
pub mod sleep;
pub mod start;
pub mod stop;

/// Why the host API server stopped or did not start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServeError {
    /// The server has no code yet. A start that reaches it fails closed
    /// (owner item 86).
    NotBuilt,
    /// The server could not bind its socket.
    Bind(io::ErrorKind),
    /// A fixed reply of the server failed to build at its start.
    Reply,
}

impl fmt::Display for ServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotBuilt => formatter.write_str("the host API server is not built yet"),
            Self::Bind(kind) => write!(formatter, "cannot bind the socket: {kind}"),
            Self::Reply => formatter.write_str("a fixed reply failed to build"),
        }
    }
}

/// Runs the host API server until it stops. The caller runs it on the
/// multi-thread tokio runtime (decision CH36, gap G21). Blocking work goes
/// to `spawn_blocking`.
///
/// `config` holds every path and size (plan PR L20). The compose step of
/// L20 builds the handler set from it, and `routes!` needs a handler for
/// each route of `route_table!`. No handler of `create` and `list` is on
/// main yet (plan PR L7), so this start still fails closed after the
/// config read (owner item 86).
pub async fn serve(_config: Config) -> Result<(), ServeError> {
    Err(ServeError::NotBuilt)
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use tokio::runtime::Builder;

    use super::{ServeError, serve};
    use crate::config::Config;

    #[test]
    fn serve_refuses_until_built() {
        let config = Config::from_lookup(|key| {
            let value = if key.ends_with("_GIB") { "1" } else { "/x" };
            Some(OsString::from(value))
        })
        .unwrap();
        let runtime = Builder::new_current_thread().build().unwrap();
        assert_eq!(runtime.block_on(serve(config)), Err(ServeError::NotBuilt));
    }
}
