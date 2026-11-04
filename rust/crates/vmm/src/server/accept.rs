//! The accept loop on `vmm.sock` (decisions CH20, CH26). The vmm owns it,
//! so the frozen mismatch reply stays in vmm code.

use std::convert::Infallible;
use std::future::{Future, poll_fn};
use std::io;
use std::path::Path;
use std::pin::{Pin, pin};
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use hyper::Request;
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::net::UnixListener;
use tokio::sync::oneshot;
use tokio::time::sleep;

use super::Reply;
use super::header::ProtocolCheck;
use crate::ServeError;

/// The pause after a failed accept, so an error such as EMFILE does not
/// spin the loop (decision CH40 rule 1).
const ACCEPT_PAUSE: Duration = Duration::from_millis(100);

/// How long a client has to send the whole head of a request, the first
/// one and each one on a kept-alive connection. Past it, the server closes
/// the connection, so a silent client holds no file and no task for long.
/// hyper skips this timeout when the connection has no timer.
// Decision CH40 rule 2: a request head must arrive within 30 s.
const HEAD_TIMEOUT: Duration = Duration::from_secs(30);

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
        let service = service_fn(move |request| {
            let refused = check.refuse(request.headers());
            let handler = handler.clone();
            async move {
                Ok::<_, Infallible>(match refused {
                    Some(reply) => reply,
                    None => handler(request).await,
                })
            }
        });
        tokio::spawn(async move {
            let connection = http1::Builder::new()
                .timer(TokioTimer::new())
                .header_read_timeout(HEAD_TIMEOUT)
                .serve_connection(TokioIo::new(stream), service)
                .with_upgrades();
            // An error ends this connection only. A client that hangs up
            // early is no fault of the server.
            let _ = connection.await;
        });
    })
    .await;
    Ok(())
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
