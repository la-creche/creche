//! One request through a router, with no socket.
//!
//! The body here is a stub. `AGENTS.md` of this crate lists the stubs and the
//! packet that writes them.

use axum::Router;
use axum::body::Body;
use axum::response::Response;
use http::Request;

/// Gives `request` to `router` and returns its answer.
///
/// A test of a handler needs no listener and no client for that.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-testkit writes this body"
)]
pub async fn call(router: Router, request: Request<Body>) -> Response {
    todo!()
}
