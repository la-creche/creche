//! The accept loop on `vmm.sock` (decisions CH20, CH26). The vmm owns it,
//! so the frozen mismatch reply stays in vmm code.

use std::convert::Infallible;
use std::future::{Future, poll_fn};
use std::io;
use std::net::Shutdown;
use std::os::fd::AsFd;
use std::path::Path;
use std::pin::{Pin, pin};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;

use creche_wire::HOST_API_BODY_CAP;
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, StatusCode};
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::oneshot;
use tokio::time::{sleep, timeout};

use super::Reply;
use super::header::ProtocolCheck;
use crate::ServeError;

/// The pause after a failed accept, so an error such as EMFILE does not
/// spin the loop (decision CH40 rule 1).
const ACCEPT_PAUSE: Duration = Duration::from_millis(100);

/// How long a client has to send the whole head of a request, the first
/// one and each one on a kept-alive connection. Past it, the server shuts
/// the write half and closes the socket at most `LINGER_TIME` later, so a
/// silent client holds no file and no task for long. Each open connection
/// holds two file descriptors: the stream and its twin for the lingering
/// close.
/// hyper skips this timeout when the connection has no timer.
// Decision CH40 rule 2: a request head must arrive within 30 s.
const HEAD_TIMEOUT: Duration = Duration::from_secs(30);

/// The most request bytes that the server reads and drops after hyper
/// ends a connection, so that the client still gets the last reply.
/// LINGER_TIME bounds the same step in time.
// Decision CH26: each refusal is a typed reply. The server can refuse a
// request before it reads the body: a bad Content-Type, a Content-Length
// over the cap, or a body that stops at the cap. If the socket then
// closes with request bytes unread, Linux resets it. The client that
// still writes gets EPIPE, and its read ends in ECONNRESET, so a client
// such as httpx, which writes the whole request before it reads, loses
// the reply. A client that sends more than LINGER_BYTES, or takes longer
// than LINGER_TIME, can still get the reset.
// CONTRACT-QUESTION: no row decides LINGER_BYTES (4 times the body cap)
// or LINGER_TIME (5 s). See Known gaps in rust/crates/vmm/AGENTS.md.
const LINGER_BYTES: usize = 4 * HOST_API_BODY_CAP;

/// How long the server reads and drops request bytes after hyper ends a
/// connection. See LINGER_BYTES.
const LINGER_TIME: Duration = Duration::from_secs(5);

/// Stops a running server, through `tokio::sync` (decision CH26 rule 2).
/// A dropped `Stop` stops the server too.
#[derive(Debug)]
pub struct Stop(oneshot::Sender<()>);

/// What the server watches for its stop.
#[derive(Debug)]
pub struct StopSignal(oneshot::Receiver<()>);

impl Stop {
    /// A new stop and the signal that a server watches.
    pub fn pair() -> (Self, StopSignal) {
        let (sender, receiver) = oneshot::channel();
        (Self(sender), StopSignal(receiver))
    }

    /// Stops the server: it accepts no further connection.
    pub fn stop(self) {
        // An Err means the server stopped already.
        let _ = self.0.send(());
    }
}

/// Serves on a new socket at `path` until `stop` fires. Each request
/// passes the protocol header check first, then goes to `handler`. The
/// connection keeps upgrades on, for the `acp` route (decision CH20).
/// A connection that ends without an upgrade ends with `linger`, so a
/// client refused before its body still gets the typed reply (decision
/// CH26).
///
/// The run directory of the unit is the guard of the socket (decision
/// CH20), so the bind refuses a path that exists. At the stop, open
/// connections keep running. The shutdown order of a later change closes
/// them.
pub async fn run_with<H, F>(path: &Path, stop: StopSignal, handler: H) -> Result<(), ServeError>
where
    H: Fn(Request<Incoming>) -> F + Clone + Send + Sync + 'static,
    F: Future<Output = Reply> + Send + 'static,
{
    let check = Arc::new(ProtocolCheck::new()?);
    let listener = UnixListener::bind(path).map_err(|error| ServeError::Bind(error.kind()))?;
    let accept = |context: &mut Context<'_>| {
        listener
            .poll_accept(context)
            .map(|accepted| accepted.map(|(stream, _)| stream))
    };
    accept_each(accept, stop, |stream| {
        let handler = handler.clone();
        let check = Arc::clone(&check);
        tokio::spawn(async move {
            // A second handle on the same socket, for the lingering close
            // after hyper drops its own. A failed dup, such as EMFILE,
            // serves the connection without it.
            let twin = stream
                .as_fd()
                .try_clone_to_owned()
                .ok()
                .map(std::os::unix::net::UnixStream::from);
            let upgraded = Arc::new(AtomicBool::new(false));
            let service = service_fn({
                let upgraded = Arc::clone(&upgraded);
                move |request| {
                    let refused = check.refuse(request.headers());
                    let handler = handler.clone();
                    let upgraded = Arc::clone(&upgraded);
                    async move {
                        let reply = match refused {
                            Some(reply) => reply,
                            None => handler(request).await,
                        };
                        if reply.status() == StatusCode::SWITCHING_PROTOCOLS {
                            upgraded.store(true, Ordering::Relaxed);
                        }
                        Ok::<_, Infallible>(reply)
                    }
                }
            });
            let connection = http1::Builder::new()
                .timer(TokioTimer::new())
                .header_read_timeout(HEAD_TIMEOUT)
                .serve_connection(TokioIo::new(stream), service)
                .with_upgrades();
            // An error ends this connection only. A client that hangs up
            // early is no fault of the server.
            let _ = connection.await;
            // After 101 the upgrade owns the socket, so the twin only
            // drops its handle.
            if let Some(twin) = twin
                && !upgraded.load(Ordering::Relaxed)
            {
                linger(twin).await;
            }
        });
    })
    .await;
    Ok(())
}

/// Ends a connection that hyper has ended, so that the client gets the
/// last reply whole (decision CH26). The server shuts its write half, so
/// the client reads the end of the reply. It then reads and drops request
/// bytes until the client closes, LINGER_BYTES, or LINGER_TIME. The drop
/// of `twin` closes the socket.
async fn linger(twin: std::os::unix::net::UnixStream) {
    // hyper may have shut the write half already. A second shutdown, or
    // one on a socket that the client closed, changes nothing.
    let _ = twin.shutdown(Shutdown::Write);
    let Ok(twin) = UnixStream::from_std(twin) else {
        return;
    };
    let drain = async {
        let mut scratch = vec![0_u8; 64 * 1024];
        let mut left = LINGER_BYTES;
        while left > 0 {
            if twin.readable().await.is_err() {
                return;
            }
            match twin.try_read(&mut scratch) {
                // 0 is the close of the client. An error other than
                // WouldBlock ends the connection too.
                Ok(0) => return,
                Ok(read) => left = left.saturating_sub(read),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(_) => return,
            }
        }
    };
    let _ = timeout(LINGER_TIME, drain).await;
}

/// Gives each stream that `accept` yields to `serve`, until `stop` fires.
// Decision CH40 rule 1: the loop logs an accept error, such as EMFILE,
// by kind. It then pauses ACCEPT_PAUSE and accepts again. An end would
// exit 78 (EX_CONFIG), and the unit does not restart on 78, so one
// EMFILE would stop every VM (decision CH19 (4)).
async fn accept_each<S>(
    mut accept: impl FnMut(&mut Context<'_>) -> Poll<io::Result<S>>,
    stop: StopSignal,
    mut serve: impl FnMut(S),
) {
    let StopSignal(mut stop) = stop;
    loop {
        let accepted = poll_fn(|context| {
            if Pin::new(&mut stop).poll(context).is_ready() {
                return Poll::Ready(None);
            }
            accept(context).map(Some)
        })
        .await;
        match accepted {
            None => return,
            Some(Ok(stream)) => serve(stream),
            Some(Err(error)) => {
                eprintln!("vmm: accept failed: {}", error.kind());
                let mut pause = pin!(sleep(ACCEPT_PAUSE));
                let stopped = poll_fn(|context| {
                    if Pin::new(&mut stop).poll(context).is_ready() {
                        return Poll::Ready(true);
                    }
                    pause.as_mut().poll(context).map(|()| false)
                })
                .await;
                if stopped {
                    return;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::io;
    use std::task::{Context, Poll};
    use std::time::Duration;

    use tokio::runtime::Builder;

    use super::{Stop, accept_each};

    /// A listener whose every accept fails, as after EMFILE, does not spin:
    /// the loop pauses between two accepts, and the stop still ends it.
    #[test]
    fn a_failing_accept_does_not_spin() {
        let runtime = Builder::new_current_thread().enable_time().build().unwrap();
        let (stop, signal) = Stop::pair();
        let mut calls = 0_u32;
        let failing = |_: &mut Context<'_>| {
            calls += 1;
            Poll::Ready(Err::<(), _>(io::Error::from(io::ErrorKind::OutOfMemory)))
        };
        runtime.spawn(async move {
            tokio::time::sleep(Duration::from_millis(350)).await;
            stop.stop();
        });
        runtime.block_on(accept_each(failing, signal, |()| {}));
        assert!((2..=6).contains(&calls), "{calls} accepts in 350 ms");
    }
}
