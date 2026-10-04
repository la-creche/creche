//! A server and a client that a test scripts byte by byte.
//!
//! The client and the server of `creche-runtime` have their tests against
//! these two, so neither one uses that client or that server.
//!
//! Each body here is a stub. `AGENTS.md` of this crate lists the stubs and
//! the packet that writes them. That packet also adds the type of a scripted
//! answer and the type of a recorded request.

use std::io;
use std::path::Path;

use crate::root::TempRoot;

/// A server that gives the answers of a script and keeps each request that
/// it got.
///
/// It listens on a loopback port that the operating system selects, or on a
/// Unix socket under a `TempRoot`. It never binds each interface.
#[derive(Debug)]
pub struct HttpStub(());

impl HttpStub {
    /// A stub on a free port of the loopback address.
    ///
    /// # Errors
    ///
    /// The error of the operating system when it refuses the bind.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    pub async fn loopback() -> io::Result<Self> {
        todo!()
    }

    /// A stub on a Unix socket under `root`.
    ///
    /// # Errors
    ///
    /// The error of the operating system when it refuses the bind.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    pub async fn unix(root: &TempRoot) -> io::Result<Self> {
        todo!()
    }
}

/// A client that sends raw bytes and returns the raw answer.
///
/// A test of a server uses it to send a request that no HTTP client sends:
/// half a head, a wrong length, a path with a final slash.
#[derive(Debug, Clone, Copy)]
pub struct RawHttp;

impl RawHttp {
    /// Sends `request` to the loopback port `port` and returns each byte of
    /// the answer, until the server closes the connection.
    ///
    /// # Errors
    ///
    /// The error of the operating system for the connect, the write or the
    /// read.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    pub async fn tcp(port: u16, request: &[u8]) -> io::Result<Vec<u8>> {
        todo!()
    }

    /// Sends `request` to the Unix socket `socket` and returns each byte of
    /// the answer, until the server closes the connection.
    ///
    /// # Errors
    ///
    /// The error of the operating system for the connect, the write or the
    /// read.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    pub async fn unix(socket: &Path, request: &[u8]) -> io::Result<Vec<u8>> {
        todo!()
    }
}
