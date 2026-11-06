//! The vmm: the one process that owns every Cloud Hypervisor VM of the
//! playpen (decision CH9). It serves the host API (decision CH20) and
//! composes two libraries: `creche-vmm-confine` and `creche-vmm-store`.

use std::{fmt, io};

mod compose;
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

pub use compose::{StartError, serve};

/// Why the host API server stopped or did not start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServeError {
    /// The server could not bind its socket.
    Bind(io::ErrorKind),
    /// A fixed reply of the server failed to build at its start.
    Reply,
}

impl fmt::Display for ServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Bind(kind) => write!(formatter, "cannot bind the socket: {kind}"),
            Self::Reply => formatter.write_str("a fixed reply failed to build"),
        }
    }
}
