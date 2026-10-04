//! The signals of a service: SIGTERM, SIGINT and SIGHUP.
//!
//! SIGTERM and SIGINT trigger the stop signal of the process. SIGHUP asks for
//! a reload in a service that has one. In a service with no reload, SIGHUP
//! keeps its default action and ends the process, as it ends the Python
//! service today.
//!
//! The Python origins are `attendance/src/attendance/__main__.py:189-208`,
//! the class `SignalControl` of `caregiver/src/caregiver/loop.py:1205-1240`,
//! the SIGHUP handler of `chaperone/src/chaperone/reload_wiring.py:357-372`
//! and of `door-trigger/src/agent_door_trigger/webhooks.py:229-259`. This
//! module is a new design and not a translation of them.
//!
//! A handler of this module stays for the life of the process. `tokio` does
//! not give a signal its default action back. The door for a terminal puts
//! the old handlers back (`door-tui/src/agent_door_tui/signals.py:68-74`).
//! No function here does that.

use std::error::Error;
use std::fmt;
use std::io;
use std::panic;

use tokio::runtime::Handle;
use tokio::signal::unix::{Signal, SignalKind, signal};

use crate::tasks::{Shutdown, ShutdownTrigger};

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

/// The text of [`SignalError`] for a thread with no runtime.
const NO_RUNTIME: &str = "no runtime runs on this thread";

/// The text of [`SignalError`] for a runtime that takes no signal.
const NO_SIGNAL_DRIVER: &str = "the runtime of this thread has no signal driver";

/// Installs the signal handlers of the process. Call it inside the runtime.
///
/// SIGTERM and SIGINT each call `trigger`. With [`OnHangup::Reload`] the
/// function also takes SIGHUP and returns the [`Hangups`]. With
/// [`OnHangup::DefaultAction`] it returns `None`.
///
/// Each handler is in place when the function returns. A signal after the
/// return finds its handler. A second SIGTERM or SIGINT has no other effect:
/// the stop signal stays triggered, and the process does not end at once.
///
/// The function never panics. On a thread with no runtime, and in a runtime
/// with no signal driver, it returns an error.
///
/// The Python origin is `_install_signals` of
/// `attendance/src/attendance/__main__.py:189-208`. `SignalControl.install`
/// of `caregiver/src/caregiver/loop.py:1217-1220` has the same three
/// signals.
///
/// # Errors
///
/// [`SignalError`] when the operating system refuses a handler, and when the
/// thread has no runtime that takes a signal. The handlers that the function
/// installed before the error stay. A SIGTERM handler that stays still
/// triggers the stop signal.
pub fn install(
    trigger: ShutdownTrigger,
    on_hangup: OnHangup,
) -> Result<Option<Hangups>, SignalError> {
    let Ok(runtime) = Handle::try_current() else {
        return Err(SignalError {
            kind: io::ErrorKind::Other,
            os_text: String::from(NO_RUNTIME),
        });
    };

    for kind in [SignalKind::terminate(), SignalKind::interrupt()] {
        let stop = listen(kind)?;

        // The task starts before the next handler. An error at that handler
        // then leaves no signal that the process takes and that stops
        // nothing.
        drop(runtime.spawn(trigger_at(stop, trigger.clone())));
    }

    match on_hangup {
        OnHangup::Reload => Ok(Some(Hangups {
            signal: listen(SignalKind::hangup())?,
            shutdown: trigger.shutdown(),
        })),
        OnHangup::DefaultAction => Ok(None),
    }
}

/// Installs the handler of one signal and gives its listener.
fn listen(kind: SignalKind) -> Result<Signal, SignalError> {
    // `signal` panics in a runtime that has no signal driver. `install`
    // already refused a thread with no runtime.
    match panic::catch_unwind(|| signal(kind)) {
        Ok(Ok(listener)) => Ok(listener),
        Ok(Err(error)) => Err(SignalError {
            kind: error.kind(),
            os_text: os_text(&error),
        }),
        Err(payload) => {
            drop(payload);

            Err(SignalError {
                kind: io::ErrorKind::Other,
                os_text: String::from(NO_SIGNAL_DRIVER),
            })
        }
    }
}

/// The text of `error` with no ` (os error N)` at its end: the text of
/// `strerror`.
///
/// `readfile::os_text` replaces this copy when its body exists.
fn os_text(error: &io::Error) -> String {
    let text = error.to_string();
    let Some(code) = error.raw_os_error() else {
        return text;
    };

    match text.strip_suffix(&format!(" (os error {code})")) {
        Some(plain) => plain.to_owned(),
        None => text,
    }
}

/// Triggers the stop signal at the first signal of `stop`.
///
/// The task ends at the stop signal, also when another signal or the end of
/// `main` triggered it. The handler of the operating system stays, so a later
/// signal of this kind does nothing.
async fn trigger_at(mut stop: Signal, trigger: ShutdownTrigger) {
    let shutdown = trigger.shutdown();

    tokio::select! {
        () = shutdown.cancelled() => {}
        _ = stop.recv() => trigger.trigger(),
    }
}

/// The SIGHUP signals of a process, one item for each reload to do.
///
/// Signals that arrive while a reload runs give one more item, and not one
/// item for each signal. Each SIGHUP thus has a reload that starts after it,
/// and that reload reads the files as the last signal found them.
///
/// The Python origin of that rule is `signal_arrived` of
/// `chaperone/src/chaperone/reload_wiring.py:357-372`. The look event of
/// `caregiver/src/caregiver/loop.py:1225-1235` has the same rule.
///
/// Only [`install`] gives a value:
///
/// ```no_run
/// use creche_runtime::signals::{Hangups, OnHangup, install};
/// use creche_runtime::tasks::shutdown_pair;
///
/// # async fn reload() {}
/// # async fn serve() -> Result<(), creche_runtime::signals::SignalError> {
/// let (trigger, _shutdown) = shutdown_pair();
/// let hangups: Option<Hangups> = install(trigger, OnHangup::Reload)?;
///
/// if let Some(mut hangups) = hangups {
///     while hangups.next().await.is_some() {
///         reload().await;
///     }
/// }
/// # Ok(())
/// # }
/// ```
///
/// Code outside this module cannot build a value from a listener of its
/// own. Such a value has a stop signal that no handler triggers:
///
/// ```compile_fail,E0451
/// use creche_runtime::signals::{Hangups, OnHangup, install};
/// use creche_runtime::tasks::shutdown_pair;
///
/// # fn forge(listener: tokio::signal::unix::Signal) {
/// let (_trigger, shutdown) = shutdown_pair();
/// let hangups = Hangups {
///     signal: listener,
///     shutdown,
/// };
/// # }
/// ```
#[derive(Debug)]
pub struct Hangups {
    signal: Signal,
    shutdown: Shutdown,
}

impl Hangups {
    /// Waits for the next SIGHUP. `None` after the stop signal of the
    /// process.
    ///
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again, and no signal is lost.
    ///
    /// After the stop signal, each call gives `None` at once, also when a
    /// SIGHUP waits. Leave the loop at the first `None`.
    pub async fn next(&mut self) -> Option<()> {
        tokio::select! {
            biased;
            () = self.shutdown.cancelled() => None,
            item = self.signal.recv() => item,
        }
    }
}

#[cfg(test)]
mod tests {
    use tokio::runtime::Builder;

    use super::*;
    use crate::tasks::shutdown_pair;

    /// Each difference from a Python copy, on purpose. No vector covers this
    /// module, so a row names the Python file and the line.
    const DEVIATIONS: [(&str, &str); 6] = [
        (
            "attendance/src/attendance/__main__.py:205",
            "Python continues with no handler on a system that has no signal support. \
             `install` returns an error, and the program does not start.",
        ),
        (
            "chaperone/src/chaperone/reload_wiring.py:394-397",
            "Python writes a warning and serves with no SIGHUP handler. `install` returns an \
             error.",
        ),
        (
            "door-trigger/src/agent_door_trigger/webhooks.py:258-259",
            "Python writes a line and serves with no SIGHUP handler. `install` returns an \
             error.",
        ),
        (
            "attendance/src/attendance/__main__.py:206",
            "Python runs one reload for each SIGHUP that its loop takes. `Hangups` gives one \
             item for all the signals that arrive while a reload runs.",
        ),
        (
            "door-trigger/src/agent_door_trigger/webhooks.py:257",
            "Python runs one reload for each SIGHUP that its loop takes. `Hangups` gives one \
             item for all the signals that arrive while a reload runs.",
        ),
        (
            "door-tui/src/agent_door_tui/signals.py:68-74",
            "The Python door puts the old handlers back when the child program ended. A \
             handler of `install` stays for the life of the process.",
        ),
    ];

    /// The number of the error `EINVAL`, on Linux and on macOS.
    const EINVAL: i32 = 22;

    /// The number of SIGKILL, on Linux and on macOS. No process takes that
    /// signal.
    const SIGKILL: i32 = 9;

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

    #[test]
    fn each_deviation_names_a_python_file_and_a_line() {
        for (place, difference) in DEVIATIONS {
            let (file, lines) = place.rsplit_once(':').unwrap();

            assert!(file.ends_with(".py"), "{place}");
            assert!(
                lines
                    .split('-')
                    .all(|line| !line.is_empty() && line.bytes().all(|byte| byte.is_ascii_digit())),
                "{place}"
            );
            assert!(difference.ends_with('.'), "{place}");
        }
    }

    #[test]
    fn the_text_of_a_system_error_has_no_error_number() {
        let error = io::Error::from_raw_os_error(EINVAL);

        assert_eq!(error.to_string(), "Invalid argument (os error 22)");
        assert_eq!(os_text(&error), "Invalid argument");
    }

    #[test]
    fn the_text_of_an_error_with_no_number_stays_as_it_is() {
        let error = io::Error::other("Refusing to register signal 9");

        assert_eq!(os_text(&error), "Refusing to register signal 9");
    }

    // A test of this file must not install a handler: the handler stays in
    // the test program and takes the signal from each other test. The three
    // tests below get an error before the first handler. `tests/signals.rs`
    // holds each test that installs one, in a child.

    #[test]
    fn install_on_a_thread_with_no_runtime_gives_an_error() {
        for on_hangup in [OnHangup::Reload, OnHangup::DefaultAction] {
            let (trigger, shutdown) = shutdown_pair();
            let error = install(trigger, on_hangup).unwrap_err();

            assert_eq!(
                error,
                SignalError {
                    kind: io::ErrorKind::Other,
                    os_text: String::from(NO_RUNTIME),
                }
            );
            assert_eq!(
                error.to_string(),
                "the signal call failed: no runtime runs on this thread"
            );
            assert!(!shutdown.is_cancelled());
        }
    }

    #[test]
    fn install_in_a_runtime_with_no_signal_driver_gives_an_error() {
        // A runtime with no I/O driver has no signal driver.
        let plain = Builder::new_current_thread().build().unwrap();

        for on_hangup in [OnHangup::Reload, OnHangup::DefaultAction] {
            let (trigger, shutdown) = shutdown_pair();
            let error = plain
                .block_on(async { install(trigger, on_hangup) })
                .unwrap_err();

            assert_eq!(
                error,
                SignalError {
                    kind: io::ErrorKind::Other,
                    os_text: String::from(NO_SIGNAL_DRIVER),
                }
            );
            assert!(!shutdown.is_cancelled());
        }
    }

    #[test]
    fn a_refused_handler_gives_the_kind_and_the_text_of_its_error() {
        // `tokio` refuses SIGKILL before it asks the operating system, so
        // the call installs nothing.
        let runtime = Builder::new_current_thread().enable_all().build().unwrap();
        let error = runtime
            .block_on(async { listen(SignalKind::from_raw(SIGKILL)) })
            .unwrap_err();

        assert_eq!(
            error,
            SignalError {
                kind: io::ErrorKind::Other,
                os_text: String::from("Refusing to register signal 9"),
            }
        );
    }
}
