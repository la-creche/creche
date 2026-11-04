//! The host API server (decisions CH20, CH26). It accepts on `vmm.sock`
//! and serves one HTTP/1.1 connection per accept, with upgrades on. The
//! protocol header is the first check of each request (decisions CH25
//! rule 1, CH33 rule 1, CH34 rule 1).

mod accept;
mod header;

use std::path::Path;

use creche_wire::{ListError, RouteError};
use hyper::body::Bytes;
use hyper::header::{CONTENT_TYPE, HeaderValue};
use hyper::{Response, StatusCode};

use crate::ServeError;

pub use accept::{Stop, StopSignal, run_with};

/// Each answer of the server. A JSON reply holds the whole body.
pub type Reply = Response<http_body_util::Full<Bytes>>;

/// Serves the host API on a new socket at `path` until `stop` fires. A
/// request that passes the protocol header check gets `no_route` 404:
/// the server has no route yet.
pub async fn run(path: &Path, stop: StopSignal) -> Result<(), ServeError> {
    // Each route error writes `no_route` as the same bytes (decision CH33
    // rule 13). ListError holds only the shared codes.
    let no_route = ListError::NoRoute {};
    let status = status_of(no_route.status())?;
    let body = Bytes::from(serde_json::to_vec(&no_route).map_err(|_| ServeError::Reply)?);
    let answer = move |_request| {
        let reply = json_reply(status, body.clone());
        async move { reply }
    };
    run_with(path, stop, answer).await
}

/// The status of a wire constant. The server checks each one at the start,
/// so a request never meets a bad status.
fn status_of(status: u16) -> Result<StatusCode, ServeError> {
    StatusCode::from_u16(status).map_err(|_| ServeError::Reply)
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
