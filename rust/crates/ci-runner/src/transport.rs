//! The one HTTP/1.1 exchange of each call, on a TCP connection of its own.
//! This module holds the socket and the hyper mechanics. `client.rs`
//! speaks in calls and replies.
//!
//! 1. Connect to Forgejo over TCP.
//! 2. Send the one request.
//! 3. Read the status and at most `REPLY_CAP` bytes of the body.
//! 4. Close the connection.
//!
//! The caller gives one bound for all four steps. A poll of the jobs list
//! comes every few seconds, so a new connection per call costs little and
//! keeps no state between calls.

use std::time::Duration;

use http_body_util::{BodyExt, Full, LengthLimitError, Limited};
use hyper::body::Bytes;
use hyper::client::conn::http1;
use hyper::{Request, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::TcpStream;
use tokio::task::JoinHandle;
use tokio::time::timeout;

use crate::client::ForgejoOrigin;
use crate::error::Fault;

/// The most bytes of a reply body. A waiting job is about 300 bytes, so
/// the cap holds a list of about 3000 jobs.
const REPLY_CAP: usize = 1 << 20;

/// The status and the body of one reply.
pub(crate) struct Exchange {
    /// The status.
    pub(crate) status: StatusCode,
    /// The body, at most `REPLY_CAP` bytes.
    pub(crate) body: Bytes,
}

/// The task that drives the connection. Its drop ends the task, so no
/// connection outlives its call, also after a timeout.
struct Driver(JoinHandle<()>);

impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Sends `request` to `origin` and reads the reply, all within `wait`.
pub(crate) async fn exchange(
    origin: &ForgejoOrigin,
    request: Request<Full<Bytes>>,
    wait: Duration,
) -> Result<Exchange, Fault> {
    timeout(wait, send(origin, request))
        .await
        .map_err(|_| Fault::Timeout(wait))?
}

/// The four steps of the module docs, with no bound.
async fn send(origin: &ForgejoOrigin, request: Request<Full<Bytes>>) -> Result<Exchange, Fault> {
    let stream = TcpStream::connect(origin.address())
        .await
        .map_err(Fault::Connect)?;
    let (mut sender, connection) = http1::handshake(TokioIo::new(stream))
        .await
        .map_err(Fault::Http)?;

    // An error of the connection also fails the request or the body
    // read, so the task drops it.
    let _driver = Driver(tokio::spawn(async move {
        let _ended = connection.await;
    }));

    let response = sender.send_request(request).await.map_err(Fault::Http)?;
    let status = response.status();
    let body = Limited::new(response.into_body(), REPLY_CAP)
        .collect()
        .await
        .map_err(|error| {
            if error.is::<LengthLimitError>() {
                Fault::TooLarge
            } else {
                Fault::Body(error)
            }
        })?
        .to_bytes();

    Ok(Exchange { status, body })
}
