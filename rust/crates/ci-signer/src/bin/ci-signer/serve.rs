//! The mode `serve`: the service on the attach interface of the runner
//! (the runner design at commit 131ce43, section 6.4). Linux only.
//!
//! `creche-ci-signer.socket` listens on `/run/ci-signer/attach.sock` with
//! `SocketGroup=ci-runner` and `SocketMode=0660`, so only the daemon can
//! connect. The service has `Accept=no` and `StandardInput=socket`: its
//! stdin is the listening socket, so it takes no fd by number. Each
//! accepted control connection is one session task (`session.rs`), and at
//! most `SESSIONS_MAX` run at once.

use std::fmt;
use std::io;
use std::os::fd::AsFd;
use std::os::unix::net::UnixListener as StdListener;
use std::sync::Arc;
use std::time::Duration;

use tokio::net::UnixListener;
use tokio::sync::Semaphore;

use crate::log;
use crate::session;
use crate::signer::Signer;

/// The most sessions at once. The runner gives one session to each matched
/// row of each running job, and one slot of the kind `creche-release`
/// runs one job (question 10 of the note). A first value.
pub const SESSIONS_MAX: usize = 16;

/// The wait after an accept that failed, so a lasting fault does not spin
/// the loop.
const ACCEPT_PAUSE: Duration = Duration::from_millis(100);

/// Why `serve` stopped.
#[derive(Debug)]
pub enum ServeError {
    /// stdin is not a socket that listens.
    NotListening,
    /// The check or the copy of stdin failed.
    Stdin(io::Error),
}

impl fmt::Display for ServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotListening => formatter.write_str(
                "stdin is no listening socket: run serve under creche-ci-signer.socket with StandardInput=socket",
            ),
            Self::Stdin(error) => write!(formatter, "stdin: {error}"),
        }
    }
}

impl std::error::Error for ServeError {}

/// The listening socket of stdin, for the runtime.
///
/// # Errors
///
/// `ServeError` when stdin is no listening socket.
pub fn listener() -> Result<StdListener, ServeError> {
    let stdin = io::stdin()
        .as_fd()
        .try_clone_to_owned()
        .map_err(ServeError::Stdin)?;
    let listening = rustix::net::sockopt::socket_acceptconn(&stdin)
        .map_err(|error| ServeError::Stdin(error.into()))?;
    if !listening {
        return Err(ServeError::NotListening);
    }
    let listener = StdListener::from(stdin);
    listener.set_nonblocking(true).map_err(ServeError::Stdin)?;
    Ok(listener)
}

/// Serves each control connection of `listener` until the unit stops.
///
/// # Errors
///
/// `ServeError` when the runtime cannot take the listener.
pub async fn serve(listener: StdListener, signer: Signer) -> Result<(), ServeError> {
    let listener = UnixListener::from_std(listener).map_err(ServeError::Stdin)?;
    let signer = Arc::new(signer);
    let permits = Arc::new(Semaphore::new(SESSIONS_MAX));
    log(format_args!("serve: listening"));
    loop {
        let control = match listener.accept().await {
            Ok((control, _)) => control,
            Err(error) => {
                log(format_args!("serve: an accept failed: {error}"));
                tokio::time::sleep(ACCEPT_PAUSE).await;
                continue;
            }
        };
        let Ok(permit) = Arc::clone(&permits).try_acquire_owned() else {
            log(format_args!(
                "serve: {SESSIONS_MAX} sessions run, so one more is closed"
            ));
            continue;
        };
        let signer = Arc::clone(&signer);
        tokio::spawn(async move {
            session::run(control, signer).await;
            drop(permit);
        });
    }
}
