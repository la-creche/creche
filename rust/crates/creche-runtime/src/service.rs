//! The steps of the start of a program.
//!
//! Each program of the platform runs inside [`run`]: a daemon, a verify hook
//! and a command that runs to its end. [`run`] builds the one `tokio` runtime
//! of the process. The attribute `#[tokio::main]` is not for a service: its
//! code calls `expect`.
//!
//! The parse of the config runs before the runtime starts, and no `await` is
//! in it. [`load`] applies the failure action of the config type
//! (`rust/AGENTS.md`, "The config of a process"). A start that fails for a
//! reason outside the config type goes through [`refuse_start`]. Two examples
//! are a token file that is not valid and an `https` URL.
//!
//! [`run`], [`load`] and [`refuse_start`] each set the panic hook of the
//! process first, as [`log::init`](crate::log::init) does, with the name of
//! the program. The `main` of a program calls `log::init` first. The three
//! hold the hook for a `main` that omits the call. A test that calls one of
//! the three in its own process replaces the panic hook of the test program.
//! Keep the rest of each body in a private function, and give a test that
//! function.
//!
//! A service tells no program that it is ready: each unit has `Type=simple`.
//! [`ready`] writes one line for each listener, for the operator. No program
//! reads it.
//!
//! The Python origin is the `main` of each service, for example
//! `attendance/src/attendance/__main__.py` and
//! `noticeboard/src/noticeboard/__main__.py`.
//!
//! Each function body here is a stub. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::fmt;
use std::future::Future;
use std::num::NonZeroUsize;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use creche_contracts::config::roster::RosterErrors;
use creche_contracts::config::site::SiteErrors;
use creche_contracts::config::{Checked, ConfigErrors};

use crate::clock::SystemClock;
use crate::entropy::OsEntropy;
use crate::http::server::Bound;
use crate::signals::{Hangups, OnHangup};
use crate::tasks::{Shutdown, Tasks};

/// How many threads run the tasks of a program.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Threads {
    /// One thread: the thread of `main`. Use it for a program that runs to
    /// its end.
    One,
    /// This count of worker threads. Two handlers then run at one time, so
    /// each piece of state needs one owner task or a lock.
    Workers(NonZeroUsize),
}

/// What [`run`] must know about a program.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Program {
    /// The name of the program: the target of its log lines.
    pub name: &'static str,
    /// How many threads run its tasks.
    pub threads: Threads,
    /// What the program does at SIGHUP.
    pub on_hangup: OnHangup,
    /// How long the program waits for its tasks after the stop signal. Keep
    /// it below `TimeoutStopSec=` of the unit: systemd kills the process at
    /// that time.
    pub drain: Duration,
}

/// What [`run`] gives to the `main` of a program.
#[derive(Debug)]
pub struct Context {
    /// The tasks that must end before the process exits.
    pub tasks: Tasks,
    /// The stop signal of the process.
    pub shutdown: Shutdown,
    /// The SIGHUP signals. `None` for a program with
    /// [`OnHangup::DefaultAction`].
    pub hangups: Option<Hangups>,
    /// The clock of the host.
    pub clock: Arc<SystemClock>,
    /// The random source of the operating system.
    pub entropy: Arc<OsEntropy>,
}

/// Runs `main` inside the runtime of the process and returns its exit status.
///
/// The function first sets the panic hook of the process, as
/// [`log::init`](crate::log::init) does, with the name of the program. A
/// panic of a task then writes its place and never its message, also when
/// the `main` of the program did not call `log::init`.
///
/// The function builds the runtime, makes the stop signal and the [`Tasks`],
/// and installs the signal handlers. After `main` returns, it triggers the
/// stop signal and waits for the tracked tasks, for the drain limit of the
/// program at most. Then the process exits, also when a blocking call still
/// runs.
///
/// Return the result from the `main` of the program. The lint gate refuses
/// `std::process::exit`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-service writes this body"
)]
pub fn run<F, Fut>(program: Program, main: F) -> ExitCode
where
    F: FnOnce(Context) -> Fut,
    Fut: Future<Output = ExitCode>,
{
    crate::log::init(program.name);

    todo!()
}

/// The errors of one parse, as one line of text for each error.
///
/// [`load`] writes each line to stderr. A line names the variable or the key
/// and the reason. It never holds a value: a value can be a secret.
pub trait ErrorLines {
    /// One line for each error, in the order of the parse.
    fn lines(&self) -> Vec<String>;
}

impl ErrorLines for ConfigErrors {
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-service writes this body"
    )]
    fn lines(&self) -> Vec<String> {
        todo!()
    }
}

impl ErrorLines for SiteErrors {
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-service writes this body"
    )]
    fn lines(&self) -> Vec<String> {
        todo!()
    }
}

impl ErrorLines for RosterErrors {
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-service writes this body"
    )]
    fn lines(&self) -> Vec<String> {
        todo!()
    }
}

/// What a program does after the parse of its config at start.
#[derive(Debug)]
pub enum Loaded<C, E> {
    /// The config is valid. The program runs with it.
    Run(C),
    /// The config is not valid, and its type says that the program exits.
    /// [`load`] wrote each error to stderr. Return this status from `main`.
    Exit(ExitCode),
    /// The config is not valid, and its type says that the program starts
    /// and refuses each call. The program publishes these errors as a fault.
    RefuseEachCall(E),
}

/// Applies the failure action of the config type `C` to the result of its
/// parse.
///
/// The exit status is the status that `creche_contracts::config::start`
/// gives. This function adds no status of its own.
///
/// The function first sets the panic hook of the process, as
/// [`log::init`](crate::log::init) does, with `program`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-service writes this body"
)]
pub fn load<C: Checked, E: ErrorLines>(
    program: &'static str,
    parsed: Result<C, E>,
) -> Loaded<C, E> {
    crate::log::init(program);

    todo!()
}

/// The exit status of a start that the program refuses for a reason outside
/// its config type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefusedStart {
    /// `EX_CONFIG`, status 78. The unit file must then hold
    /// `RestartPreventExitStatus=78`.
    Config,
    /// The status that the service names.
    Status(u8),
}

/// Writes the reason of a refused start to stderr and gives the exit status.
///
/// A service calls it for a token file, a key file or a URL that it cannot
/// use. The call is the one place where the service states the status of
/// that case. `reason` is an error of this crate or of `creche-contracts`,
/// and no such error holds a secret.
///
/// The function first sets the panic hook of the process, as
/// [`log::init`](crate::log::init) does, with `program`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-service writes this body"
)]
pub fn refuse_start(
    program: &'static str,
    reason: &dyn fmt::Display,
    exit: RefusedStart,
) -> ExitCode {
    crate::log::init(program);

    todo!()
}

/// Writes one `INFO` line for each listener: `listening on <address>`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-service writes this body"
)]
pub fn ready(bound: &[Bound]) {
    todo!()
}

#[cfg(test)]
mod tests {
    use std::panic;
    use std::process::Command;

    use creche_contracts::config::{AtReload, AtStart, FailureAction};

    use super::*;

    /// The variable that makes [`the_child_panics_in_one_entry`] act. Its
    /// value names the entry.
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_SERVICE_TEST_CHILD";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "service::tests::the_child_panics_in_one_entry";

    /// The name of the program of the child.
    const CHILD_PROGRAM: &str = "entry-test";

    /// The message of the panic of the child. No line of the child can hold
    /// it.
    const PANIC_MESSAGE: &str = "zebra-crossing-secret-4711";

    /// The message of the panic of a stub. The standard panic hook writes it.
    const STUB_MESSAGE: &str = "not yet implemented";

    /// Each function that a `main` can call first with the name of its
    /// program, as a value of [`CHILD_VARIABLE`].
    const ENTRIES: [&str; 3] = ["run", "load", "refuse_start"];

    /// A config whose type says that the program exits.
    #[derive(Debug)]
    struct Exits;

    impl Checked for Exits {
        const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
    }

    /// The errors of a parse that failed for one variable.
    #[derive(Debug)]
    struct OneError;

    impl ErrorLines for OneError {
        fn lines(&self) -> Vec<String> {
            vec![String::from("PORT: the variable is not set")]
        }
    }

    async fn main_that_panics(_context: Context) -> ExitCode {
        panic!("{PANIC_MESSAGE}")
    }

    /// Calls one entry and then panics. The code calls no `log::init`.
    fn enter_and_panic(entry: &str) {
        match entry {
            "run" => {
                let program = Program {
                    name: CHILD_PROGRAM,
                    threads: Threads::One,
                    on_hangup: OnHangup::DefaultAction,
                    drain: Duration::from_secs(1),
                };
                let _status = run(program, main_that_panics);
            }
            "load" => {
                let _loaded = load::<Exits, OneError>(CHILD_PROGRAM, Err(OneError));
            }
            "refuse_start" => {
                let reason = "the token file is empty";
                let _status = refuse_start(CHILD_PROGRAM, &reason, RefusedStart::Status(3));
            }
            other => panic!("no entry has the name {other}"),
        }

        panic!("{PANIC_MESSAGE}");
    }

    /// The child of the test below. Without the variable it does nothing.
    /// With the variable it calls one entry and panics, in the entry or
    /// after it. The harness takes the panic, so the child test passes.
    #[test]
    fn the_child_panics_in_one_entry() {
        let Some(entry) = std::env::var_os(CHILD_VARIABLE) else {
            return;
        };
        let entry = entry.into_string().unwrap();
        let caught = panic::catch_unwind(|| enter_and_panic(&entry));

        assert!(caught.is_err());
    }

    #[test]
    fn each_entry_sets_the_panic_hook() {
        // The hook is one for the whole process, so each entry runs in a
        // child: this test program again, with only the child test.
        for entry in ENTRIES {
            let child = Command::new(std::env::current_exe().unwrap())
                .args([CHILD_TEST, "--exact", "--nocapture", "--test-threads=1"])
                .env(CHILD_VARIABLE, entry)
                .output()
                .unwrap();
            let stderr = String::from_utf8(child.stderr).unwrap();
            let stdout = String::from_utf8(child.stdout).unwrap();
            let hook_line = format!(" ERROR {CHILD_PROGRAM} panic at ");

            assert!(child.status.success(), "{entry}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{entry}: {stdout}");
            assert!(
                stderr.lines().any(|line| line.contains(&hook_line)),
                "{entry}: {stderr}"
            );

            for message in [PANIC_MESSAGE, STUB_MESSAGE] {
                assert!(!stderr.contains(message), "{entry}: {stderr}");
                assert!(!stdout.contains(message), "{entry}: {stdout}");
            }
        }
    }
}
