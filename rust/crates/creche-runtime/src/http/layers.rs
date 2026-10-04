//! What each router of a service needs at its edge.
//!
//! [`edge`] wraps a router with four things:
//!
//! 1. An answer for a path that no route has, and one for a method that a
//!    route does not take. The service states each answer through
//!    [`ErrorBodies`].
//! 2. One task for each request. A client that leaves does not stop the
//!    handler: the handler runs to its end, as a handler of a Python service
//!    does. A handler that panics ends its own task only, and the client gets
//!    the answer for [`EdgeFailure::Panic`].
//! 3. [`ClientGone`], for a handler that must stop when its client left.
//! 4. One access line for each request, when the service writes one today.
//!
//! A service that builds its router without [`edge`] loses a handler at each
//! client that leaves. No compiler check finds that
//! (`rust/AGENTS.md`, "The rules for a service", rule 6).
//!
//! The body of a streamed answer runs in the task of the connection and not
//! in the task of the handler. A body stream owns no cleanup, and it selects
//! on the stop signal.
//!
//! The Python origins are `attendance/src/attendance/api.py:38-67` and
//! `:232-244`, and the answers of the web framework itself.
//!
//! Each function body here is a stub. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::error::Error;
use std::fmt;

use axum::Router;
use axum::body::Body;
use axum::response::Response;
use bytes::Bytes;

use crate::readfile::ByteCap;
use crate::tasks::Tasks;

/// A request that the edge refuses before a handler answers, or in place of
/// a handler that gave no answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeFailure {
    /// No route has the path.
    NoRoute,
    /// A route has the path and does not take the method.
    WrongMethod,
    /// The handler panicked.
    Panic,
    /// The runtime stopped the task of the handler.
    HandlerLost,
    /// The body of the request holds more bytes than the cap.
    BodyTooLarge {
        /// The cap of the read.
        cap: ByteCap,
    },
    /// The client stopped in the middle of the body.
    BodyUnreadable,
}

impl fmt::Display for EdgeFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoRoute => f.write_str("no route has the path"),
            Self::WrongMethod => f.write_str("the route does not take the method"),
            Self::Panic => f.write_str("the handler panicked"),
            Self::HandlerLost => f.write_str("the task of the handler was lost"),
            Self::BodyTooLarge { cap } => {
                write!(f, "the body holds more than {} bytes", cap.get())
            }
            Self::BodyUnreadable => f.write_str("the body of the request stopped early"),
        }
    }
}

impl Error for EdgeFailure {}

/// The answers of one service for each [`EdgeFailure`].
///
/// Each service has its own error body: contract 02 §14 for `attendance`,
/// the OpenAI error body for `door-owui`. A service implements this trait
/// with the type of its contract. [`starlette::StarletteBodies`] gives the
/// answers of the Python framework.
pub trait ErrorBodies: Clone + Send + Sync + 'static {
    /// The answer for `failure`: the status, the headers and the body.
    fn answer(&self, failure: EdgeFailure) -> Response;
}

/// Whether [`edge`] writes one line of the log for each request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccessLog {
    /// Write one `INFO` line with the method, the path with no query, and
    /// the status. `door-owui`, the chaperone and the trigger listener do
    /// this today.
    Write,
    /// Write no line. `attendance` and the noticeboard do this today.
    Skip,
}

/// Wraps `routes` with the edge of a service. The module doc lists the four
/// parts.
///
/// Call it one time, on the whole router, after each route is in place.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-http-server writes this body"
)]
pub fn edge<E: ErrorBodies>(routes: Router, errors: E, tasks: Tasks, access: AccessLog) -> Router {
    todo!()
}

/// The signal that the client of a request left before the answer.
///
/// [`edge`] puts one value into the extensions of each request. A handler
/// that must not continue for a client that left takes it and selects on
/// [`ClientGone::gone`]. Each other handler runs to its end.
#[derive(Debug, Clone)]
pub struct ClientGone(());

impl ClientGone {
    /// Waits until the client left. The future never ends for a client that
    /// stays.
    ///
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-http-server writes this body"
    )]
    pub async fn gone(&self) {
        todo!()
    }
}

/// Reads the body of a request, up to `cap` bytes.
///
/// The function checks no `Content-Type` header. `attendance` reads a JSON
/// body with each content type, and the `Json` extractor of `axum` refuses
/// such a body.
///
/// # Errors
///
/// [`EdgeFailure::BodyTooLarge`] for a body past the cap, and
/// [`EdgeFailure::BodyUnreadable`] for a body that stops early.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-http-server writes this body"
)]
pub async fn read_body(body: Body, cap: ByteCap) -> Result<Bytes, EdgeFailure> {
    todo!()
}

/// The answers of the Python web framework.
pub mod starlette {
    use axum::response::Response;

    use super::{EdgeFailure, ErrorBodies};

    /// The answers that the Python framework of each service gives today: the
    /// status, the content type and the bytes.
    ///
    /// A port of a service uses it to keep each byte of those answers. A
    /// service with an error body of its own contract writes its own
    /// [`ErrorBodies`].
    #[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
    pub struct StarletteBodies;

    impl ErrorBodies for StarletteBodies {
        #[expect(
            clippy::todo,
            unused_variables,
            reason = "skeleton: packet foundation-http-server writes this body"
        )]
        fn answer(&self, failure: EdgeFailure) -> Response {
            todo!()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_failure_names_its_reason() {
        let table = [
            (EdgeFailure::NoRoute, "no route has the path"),
            (
                EdgeFailure::WrongMethod,
                "the route does not take the method",
            ),
            (EdgeFailure::Panic, "the handler panicked"),
            (EdgeFailure::HandlerLost, "the task of the handler was lost"),
            (
                EdgeFailure::BodyTooLarge {
                    cap: ByteCap::ONE_MIB,
                },
                "the body holds more than 1048576 bytes",
            ),
            (
                EdgeFailure::BodyUnreadable,
                "the body of the request stopped early",
            ),
        ];

        for (failure, text) in table {
            assert_eq!(failure.to_string(), text);
        }
    }

    #[test]
    fn the_signal_of_a_client_that_left_is_a_request_extension() {
        fn extension<T: Clone + Send + Sync + 'static>() {}

        extension::<ClientGone>();
        extension::<starlette::StarletteBodies>();
    }
}
