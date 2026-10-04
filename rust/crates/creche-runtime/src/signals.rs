//! The signals of a service: SIGTERM, SIGINT and SIGHUP.
//!
//! SIGTERM and SIGINT trigger the stop signal of the process. SIGHUP asks for
//! a reload in a service that has one. In a service with no reload, SIGHUP
//! keeps its default action and ends the process, as it ends the Python
//! service today.
//!
//! The Python origins are `attendance/src/attendance/__main__.py:189-208`,
//! the class `SignalControl` of `caregiver/src/caregiver/loop.py` and the
//! SIGHUP handler of `chaperone/src/chaperone/reload_wiring.py`. This module
//! is a new design and not a translation of them.
//!
//! The bodies of [`install`] and [`Hangups::next`] are stubs. `AGENTS.md` of
//! this crate lists the stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::io;

use crate::tasks::ShutdownTrigger;

/// What a process does at SIGHUP.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OnHangup {
    /// The process takes the signal, and [`Hangups`] gives one item for it.
    /// The service then reads its files again.
    Reload,
    /// The process installs no handler. SIGHUP ends it, as the operating
    /// system does by default.
    DefaultAction,
}

/// Why a signal call failed: the install of a handler, or the signal to a
/// child program.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignalError {
    /// The kind of the error of the operating system.
    pub kind: io::ErrorKind,
    /// The text of the error, as `strerror` of Python gives it.
    pub os_text: String,
}

impl fmt::Display for SignalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the signal call failed: {}", self.os_text)
    }
}

impl Error for SignalError {}

/// Installs the signal handlers of the process. Call it inside the runtime.
///
/// SIGTERM and SIGINT each call `trigger`. With [`OnHangup::Reload`] the
/// function also takes SIGHUP and returns the [`Hangups`]. With
/// [`OnHangup::DefaultAction`] it returns `None`.
///
/// # Errors
///
/// [`SignalError`] when the operating system refuses a handler.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-tasks-signals writes this body"
)]
pub fn install(
    trigger: ShutdownTrigger,
    on_hangup: OnHangup,
) -> Result<Option<Hangups>, SignalError> {
    todo!()
}

/// The SIGHUP signals of a process, one item for each reload to do.
///
/// Signals that arrive while a reload runs give one more item, and not one
/// item for each signal.
#[derive(Debug)]
pub struct Hangups(());

impl Hangups {
    /// Waits for the next SIGHUP. `None` after the stop signal of the
    /// process.
    ///
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again, and no signal is lost.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    pub async fn next(&mut self) -> Option<()> {
        todo!()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_error_names_the_answer_of_the_system() {
        let error = SignalError {
            kind: io::ErrorKind::InvalidInput,
            os_text: String::from("Invalid argument"),
        };

        assert_eq!(
            error.to_string(),
            "the signal call failed: Invalid argument"
        );
    }
}
