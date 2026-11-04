//! The host API server (decisions CH20, CH26). It accepts on `vmm.sock`
//! and serves one HTTP/1.1 connection per accept, with upgrades on. The
//! protocol header is the first check of each request (decisions CH25
//! rule 1, CH33 rule 1, CH34 rule 1). The router comes from the one route
//! table (decision CH25). The body checks follow the route (decisions
//! CH26, CH33 rule 13).

mod accept;
mod check;
mod header;
mod router;

use std::path::Path;

use creche_wire::{ListError, RouteError};
use hyper::body::Bytes;
use hyper::header::{CONTENT_TYPE, HeaderValue};
use hyper::{Response, StatusCode};

use crate::ServeError;

pub use accept::{Stop, StopSignal, run_with};
pub use check::BodyCheck;
pub(crate) use check::strict;
pub use router::{Relay, Route, Routes, run_routes};

/// The wire crate, for the expansion of `routes!` in another crate.
#[doc(hidden)]
pub use creche_wire as wire;

/// Each answer of the server. A JSON reply holds the whole body.
pub type Reply = Response<http_body_util::Full<Bytes>>;

/// Serves the host API with no route on a new socket at `path` until
/// `stop` fires. A request that passes the protocol header check gets
/// `no_route` 404. `run_routes` serves the route table.
pub async fn run(path: &Path, stop: StopSignal) -> Result<(), ServeError> {
    let no_route = error_reply(&ListError::NoRoute {})?;
    let answer = move |_request| {
        let reply = no_route.clone();
        async move { reply }
    };
    run_with(path, stop, answer).await
}

/// The status of a wire constant. The server checks each one at the start,
/// so a request never meets a bad status.
fn status_of(status: u16) -> Result<StatusCode, ServeError> {
    StatusCode::from_u16(status).map_err(|_| ServeError::Reply)
}

/// The reply of a shared code (decision CH33 rule 13), made at the start
/// of the server. Each route error writes a shared code as the same bytes
/// with the same status, so the server takes the codes from `ListError`,
/// which holds only the shared codes.
fn error_reply<E: RouteError>(error: &E) -> Result<Reply, ServeError> {
    let body = serde_json::to_vec(error).map_err(|_| ServeError::Reply)?;
    Ok(json_reply(status_of(error.status())?, Bytes::from(body)))
}

/// A reply with a JSON body that the server wrote at its start.
fn json_reply(status: StatusCode, body: Bytes) -> Reply {
    let mut reply = Response::new(http_body_util::Full::new(body));
    *reply.status_mut() = status;
    reply
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    reply
}
