//! The listeners of a service: the bind, and the serve loop.
//!
//! A service binds first and serves second. [`bind`] gives a [`Bound`] for
//! each address, and an error of a bind is an error before the first request.
//! [`serve`] then runs the router on each listener until the stop signal of
//! the process.
//!
//! A service binds a Unix socket or a LAN address. `BindAddress` of
//! `creche-contracts` refuses each address that means each interface, so no
//! check of that is here.
//!
//! [`serve`] does not remove the file of a Unix socket at a stop. The next
//! start removes it, as `attendance` does today.
//!
//! The Python origin is `attendance/src/attendance/__main__.py:68-186`, and
//! `uvicorn` for each other service.
//!
//! Each function body here is a stub. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::io;
use std::time::Duration;

use axum::Router;
use creche_contracts::config::{BindAddress, SocketPath};

use crate::tasks::{Drained, Tasks};

/// Where a service listens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Listen {
    /// A Unix socket.
    Unix {
        /// The path of the socket.
        socket: SocketPath,
        /// What [`bind`] does with the directory of the socket.
        dir: SocketDir,
    },
    /// A TCP address.
    Tcp(BindAddress),
}

/// What [`bind`] does with the directory of a Unix socket.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SocketDir {
    /// Make the directory with mode `2750`. A client in the group of the
    /// directory can then connect, and a new socket gets that group.
    PrepareSetgid,
    /// Do not change the directory. It must exist.
    LeaveAsItIs,
}

/// The step of a bind that failed.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindStep {
    /// Make the directory of the socket, or set its mode.
    PrepareDir,
    /// Remove the socket file of an earlier process.
    RemoveStale,
    /// Bind the address.
    Bind,
    /// Set the mode of the new socket.
    SetMode,
}

impl fmt::Display for BindStep {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::PrepareDir => "prepare the directory of the socket",
            Self::RemoveStale => "remove the old socket file",
            Self::Bind => "bind",
            Self::SetMode => "set the mode of the socket",
        })
    }
}

/// Why a service does not listen on an address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BindError {
    /// The step that failed.
    pub step: BindStep,
    /// The address: the path of the socket, or `host:port`.
    pub address: String,
    /// The kind of the error of the operating system.
    pub kind: io::ErrorKind,
    /// The text of the error, as `strerror` of Python gives it.
    pub os_text: String,
}

impl fmt::Display for BindError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "cannot listen on {}: the step \"{}\" failed: {}",
            self.address, self.step, self.os_text
        )
    }
}

impl Error for BindError {}

/// Binds one address.
///
/// For a Unix socket the function removes the path first, only when the path
/// is a socket. It sets the new socket to mode `0660` before a client can
/// connect.
///
/// # Errors
///
/// [`BindError`] with the step that failed.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-http-server writes this body"
)]
pub async fn bind(listen: Listen) -> Result<Bound, BindError> {
    todo!()
}

/// One address that the process holds and does not serve yet.
#[derive(Debug)]
pub struct Bound(());

impl Bound {
    /// The address as text, for the log: the path of the socket, or
    /// `host:port` with the port that the operating system gave.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-http-server writes this body"
    )]
    #[must_use]
    pub fn describe(&self) -> String {
        todo!()
    }

    /// The TCP port of the listener. `None` for a Unix socket.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-http-server writes this body"
    )]
    #[must_use]
    pub fn local_port(&self) -> Option<u16> {
        todo!()
    }
}

/// Why [`serve`] stopped before the stop signal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServeError {
    /// The accept loop of one listener stopped with an error.
    Accept {
        /// The address of the listener.
        address: String,
        /// The kind of the error of the operating system.
        kind: io::ErrorKind,
        /// The text of the error, as `strerror` of Python gives it.
        os_text: String,
    },
    /// The task of one listener panicked, or the runtime stopped it.
    ListenerLost {
        /// The address of the listener.
        address: String,
    },
}

impl fmt::Display for ServeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Accept {
                address, os_text, ..
            } => write!(f, "the listener on {address} stopped: {os_text}"),
            Self::ListenerLost { address } => {
                write!(f, "the task of the listener on {address} was lost")
            }
        }
    }
}

impl Error for ServeError {}

/// Serves `app` on each listener of `bound` until the stop signal of `tasks`.
///
/// After the stop signal the listeners take no new connection. The function
/// then waits for the open requests, for `drain` at most, and returns. A
/// stream that is still open ends there.
///
/// # Errors
///
/// [`ServeError`] when a listener stops before the stop signal.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-http-server writes this body"
)]
pub async fn serve(
    bound: Vec<Bound>,
    app: Router,
    tasks: &Tasks,
    drain: Duration,
) -> Result<Drained, ServeError> {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bind_error_names_the_address_the_step_and_the_answer_of_the_system() {
        let error = BindError {
            step: BindStep::Bind,
            address: String::from("192.0.2.10:8090"),
            kind: io::ErrorKind::AddrInUse,
            os_text: String::from("Address already in use"),
        };

        assert_eq!(
            error.to_string(),
            "cannot listen on 192.0.2.10:8090: the step \"bind\" failed: Address already in use"
        );
    }

    #[test]
    fn each_bind_step_has_its_own_text() {
        let steps = [
            BindStep::PrepareDir,
            BindStep::RemoveStale,
            BindStep::Bind,
            BindStep::SetMode,
        ];
        let mut texts: Vec<String> = steps.iter().map(ToString::to_string).collect();
        texts.sort();
        texts.dedup();

        assert_eq!(texts.len(), steps.len());
    }

    #[test]
    fn a_serve_error_names_the_listener() {
        let accept = ServeError::Accept {
            address: String::from("192.0.2.10:8090"),
            kind: io::ErrorKind::Other,
            os_text: String::from("Too many open files"),
        };
        let lost = ServeError::ListenerLost {
            address: String::from("/run/creche/sessiond.sock"),
        };

        assert_eq!(
            accept.to_string(),
            "the listener on 192.0.2.10:8090 stopped: Too many open files"
        );
        assert_eq!(
            lost.to_string(),
            "the task of the listener on /run/creche/sessiond.sock was lost"
        );
    }
}
