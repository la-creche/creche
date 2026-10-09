//! A child program: exact words, a time limit, and one owner that kills it.
//!
//! A service starts other programs: `sbx`, `git`, `systemctl`, a decrypt
//! tool. This module is the one place that does it. A [`Command`] is data: the
//! words, the environment, the limits. A [`CommandRunner`] runs it. A service
//! takes a runner as a generic parameter, so a test gives a fake runner and
//! starts no program.
//!
//! Three rules hold for each child (`rust/AGENTS.md`, "The rules for a
//! service"):
//!
//! 1. The child gets the exact words. No shell reads them.
//! 2. One owner task holds the child and its pipes. No other task reads,
//!    writes, kills or waits.
//! 3. Each [`Command`] states what a stop of the process does with the
//!    child: [`AtShutdown`]. The type has no default.
//!
//! [`PipedCommand`] and [`spawn_piped`] are for a child that lives long and
//! talks on its pipes. The caller is then the owner.
//!
//! The Python services call `subprocess.run`, for example in `_run` of
//! `caregiver/src/caregiver/driver.py:84-105`. This module is a new design
//! and not a translation of that call.
//!
//! # The owner of a run
//!
//! [`TokioRunner`] gives each run one owner task, which is a task of
//! [`Tasks`]. The owner starts the program, writes its standard input, reads
//! its two output streams and waits for its end. The caller only waits for
//! the owner. The owner ends the run in one of six ways:
//!
//! 1. The child ended and each output stream is at its end: [`Finished`].
//! 2. The time limit passed: the owner kills the child, and the run gives
//!    [`RunError::TimedOut`].
//! 3. A stream held more than its cap: the owner kills the child, and the
//!    run gives [`RunError::OutputTooLarge`].
//! 4. The command says [`AtShutdown::Kill`], and the stop signal came or the
//!    caller went away: the owner kills the child, and the run gives
//!    [`RunError::Stopped`].
//! 5. The operating system did not start the program:
//!    [`RunError::NotStarted`].
//! 6. The operating system failed a read or a write of the owner: the owner
//!    kills the child, and the run gives [`RunError::OwnerLost`]. A wait
//!    call that fails gives the same error. The log holds one `ERROR` line
//!    for each such failure.
//!
//! With [`AtShutdown::Finish`], the stop signal and a caller that goes away
//! change nothing: the child runs to its end or to its time limit.
//!
//! A kill is SIGKILL to the child, and to no other process. After a kill the
//! owner waits for the end of the child, so a run that ended leaves no child
//! that runs. The owner then reads each of the two streams for 1 second at
//! most. A program that the child started can hold a stream open. It does
//! not hold the owner past that second, and the kill does not end it.
//!
//! Call each function of this module inside the runtime that `service::run`
//! builds. In a runtime with no timer or with no I/O driver, no program
//! starts and the run gives [`RunError::NotStarted`].
//!
//! # What this module does not do
//!
//! The Python `handover` limits the size of a file that a child writes
//! (`handover/src/handover/executor/host.py:359-376` and `:407`). A function
//! sets that limit in the child before the program starts. Such a function
//! needs `unsafe` code in Rust, and the lint gate forbids it. No part of this
//! module sets the limit.

use std::borrow::Cow;
use std::error::Error;
use std::fmt;
use std::future::{self, Future};
use std::io;
use std::os::unix::process::ExitStatusExt;
use std::panic;
use std::path::{Path, PathBuf};
use std::process::{ExitStatus, Stdio};
use std::str::Utf8Error;
use std::sync::Arc;
use std::time::Duration;

use creche_util::pytext;
use rustix::io::Errno;
use rustix::process::{Pid, Signal, kill_process};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::process::{Child, ChildStderr, ChildStdin, ChildStdout};
use tokio::runtime::Handle;
use tokio::task::unconstrained;
use tokio_util::sync::CancellationToken;

use crate::readfile::{ByteCap, os_text};
use crate::signals::SignalError;
use crate::tasks::{Shutdown, Tasks};

/// The target of each line that this module writes to the log.
const LOG_TARGET: &str = "command";

/// The name of each owner task. The log line of an owner that panicked
/// holds it.
const OWNER_TASK: &str = "command-owner";

/// How long an owner reads the two output streams of a child after a kill
/// and the end of that child.
const AFTER_KILL: Duration = Duration::from_secs(1);

/// The exit status of a child whose end the operating system did not give.
/// It is not 0, so no caller reads that end as a success.
const STATUS_UNKNOWN: u8 = 255;

/// The text of [`RunError::NotStarted`] for a thread with no runtime.
const NO_RUNTIME: &str = "no runtime runs on this thread";

/// The text of [`RunError::NotStarted`] for a runtime with no timer.
const NO_TIMER: &str = "the runtime of this thread has no timer";

/// The text of [`RunError::NotStarted`] for a runtime with no I/O driver.
const NO_IO_DRIVER: &str = "the runtime of this thread has no I/O driver";

/// The text of [`RunError::NotStarted`] for a name of a variable that holds
/// `=`. It is the text of the `ValueError` of Python for that name.
const ILLEGAL_NAME: &str = "illegal environment variable name";

/// The text of [`RunError::NotStarted`] for a list of words with no program.
/// No constructor makes such a list.
const NO_PROGRAM: &str = "the command names no program";

/// The text of [`RunError::NotStarted`] for a child that has no pipe for one
/// of its three streams. `tokio` gives each pipe that the command asks for.
const NO_PIPE: &str = "the child program has no pipe for a stream";

/// The text of [`SignalError`] for a process id that is no id of a child.
const NO_PROCESS_ID: &str = "the id of the child program is not valid";

/// How long a child program can run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeLimit {
    /// The owner kills the child after this time.
    After(Duration),
    /// No limit. Use it only for a program that holds the terminal of a
    /// person.
    None,
}

/// What the owner of a child does when the process stops, or when the caller
/// of the run goes away.
///
/// The type has no default: each [`Command`] states its choice.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AtShutdown {
    /// The child runs to its end or to its time limit. The stop of the
    /// process waits for it. Use it for a step that must end whole. This is
    /// what `subprocess.run` of Python does.
    Finish,
    /// The owner kills the child and waits for its end.
    Kill,
}

/// The environment of a child program.
///
/// `Debug` prints each name and no value: a value can be a secret.
#[derive(Clone, PartialEq, Eq)]
pub enum EnvPolicy {
    /// The environment of this process.
    Inherit,
    /// The environment of this process, and then these pairs. A pair
    /// replaces a variable of the same name.
    InheritAnd(Vec<(String, String)>),
    /// Only the variables of this process that have these names.
    InheritOnly(Vec<String>),
    /// Only these pairs.
    Exactly(Vec<(String, String)>),
}

/// The names of the pairs, for `Debug`.
struct Names<'a>(&'a [(String, String)]);

impl fmt::Debug for Names<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_list()
            .entries(self.0.iter().map(|(name, _)| name))
            .finish()
    }
}

impl fmt::Debug for EnvPolicy {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Inherit => f.write_str("Inherit"),
            Self::InheritAnd(pairs) => f.debug_tuple("InheritAnd").field(&Names(pairs)).finish(),
            Self::InheritOnly(names) => f.debug_tuple("InheritOnly").field(names).finish(),
            Self::Exactly(pairs) => f.debug_tuple("Exactly").field(&Names(pairs)).finish(),
        }
    }
}

/// The standard input of a child program.
///
/// `Debug` prints no byte and no count of the bytes: a service gives a secret
/// to a child in this way, and the count is the length of that secret.
#[derive(Clone, PartialEq, Eq)]
pub enum Stdin {
    /// The child reads end of file at once.
    Null,
    /// The child reads the standard input of this process.
    Inherit,
    /// The owner writes these bytes and closes the pipe.
    Bytes(Vec<u8>),
}

impl fmt::Debug for Stdin {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Null => f.write_str("Null"),
            Self::Inherit => f.write_str("Inherit"),
            Self::Bytes(_) => f.write_str("Bytes(..)"),
        }
    }
}

/// Where the standard output and the standard error of a child go.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Output {
    /// The owner reads each stream into memory, up to `cap` bytes for each
    /// one. A child that writes more gives [`RunError::OutputTooLarge`].
    Capture {
        /// The cap for each of the two streams.
        cap: ByteCap,
    },
    /// The child writes to the streams of this process.
    Inherit,
}

/// One run of a child program, as data.
///
/// A new command has these parts until a builder changes them: no argument,
/// the environment of this process, the working directory of this process,
/// an empty standard input, and a capture of 1 MiB for each output stream.
///
/// The last two parts differ from `subprocess.run` of Python. That call gives
/// the child the standard input of the process, and it captures output with
/// no cap. A port of such a call states its standard input: [`Stdin::Inherit`],
/// or [`Stdin::Null`] on purpose. It also states its cap. A port that keeps
/// one of the two defaults names that default in the doc comment of its
/// function.
///
/// Each word of a command is UTF-8 text. Python also gives a child a word
/// with bytes that are not UTF-8.
///
/// ```
/// use std::time::Duration;
///
/// use creche_runtime::command::{AtShutdown, Command, EnvPolicy, Stdin, TimeLimit};
///
/// let command = Command::new(
///     "git",
///     TimeLimit::After(Duration::from_secs(30)),
///     AtShutdown::Finish,
/// )
/// .args(["rev-parse", "HEAD"])
/// .env(EnvPolicy::InheritOnly(vec![String::from("PATH")]))
/// .cwd("/srv/registry")
/// .stdin(Stdin::Bytes(b"correct horse".to_vec()));
///
/// assert_eq!(command.program(), "git");
/// assert_eq!(command.argv(), ["git", "rev-parse", "HEAD"]);
/// assert_eq!(command.at_shutdown(), AtShutdown::Finish);
///
/// let text = format!("{command:?}");
/// assert!(text.contains("stdin: Bytes(..)"));
/// assert!(!text.contains("correct"));
/// ```
///
/// Code outside this module cannot build a command from raw parts, so each
/// command states its time limit and its [`AtShutdown`]:
///
/// ```compile_fail,E0451
/// use creche_runtime::command::{AtShutdown, Command, EnvPolicy, Output, Stdin, TimeLimit};
/// use creche_runtime::readfile::ByteCap;
///
/// let command = Command {
///     argv: vec![String::from("git")],
///     env: EnvPolicy::Inherit,
///     cwd: None,
///     stdin: Stdin::Null,
///     output: Output::Capture { cap: ByteCap::ONE_MIB },
///     limit: TimeLimit::None,
///     at_shutdown: AtShutdown::Kill,
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Command {
    /// The program, then each argument. The list is never empty.
    argv: Vec<String>,
    env: EnvPolicy,
    cwd: Option<PathBuf>,
    stdin: Stdin,
    output: Output,
    limit: TimeLimit,
    at_shutdown: AtShutdown,
}

impl Command {
    /// A run of `program` with no argument.
    ///
    /// `program` is a path, or a name that the operating system finds on
    /// `PATH`. The caller states the time limit and what a stop does. The doc
    /// comment of [`Command`] names the two parts that differ from
    /// `subprocess.run` of Python.
    #[must_use]
    pub fn new(program: impl Into<String>, limit: TimeLimit, at_shutdown: AtShutdown) -> Self {
        Self {
            argv: vec![program.into()],
            env: EnvPolicy::Inherit,
            cwd: None,
            stdin: Stdin::Null,
            output: Output::Capture {
                cap: ByteCap::ONE_MIB,
            },
            limit,
            at_shutdown,
        }
    }

    /// The command with one more argument. The child gets the word as it is.
    #[must_use]
    pub fn arg(mut self, word: impl Into<String>) -> Self {
        self.argv.push(word.into());

        self
    }

    /// The command with each of `words` as one more argument, in order.
    #[must_use]
    pub fn args<I, S>(mut self, words: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.argv.extend(words.into_iter().map(Into::into));

        self
    }

    /// The command with this environment.
    #[must_use]
    pub fn env(mut self, policy: EnvPolicy) -> Self {
        self.env = policy;

        self
    }

    /// The command with this working directory.
    #[must_use]
    pub fn cwd(mut self, dir: impl Into<PathBuf>) -> Self {
        self.cwd = Some(dir.into());

        self
    }

    /// The command with this standard input.
    #[must_use]
    pub fn stdin(mut self, stdin: Stdin) -> Self {
        self.stdin = stdin;

        self
    }

    /// The command with this place for its two output streams.
    #[must_use]
    pub fn output(mut self, output: Output) -> Self {
        self.output = output;

        self
    }

    /// The program: the first word.
    #[must_use]
    pub fn program(&self) -> &str {
        program_of(&self.argv)
    }

    /// The program, then each argument: the words that the child gets.
    #[must_use]
    pub fn argv(&self) -> &[String] {
        &self.argv
    }

    /// The environment of the child.
    #[must_use]
    pub fn env_policy(&self) -> &EnvPolicy {
        &self.env
    }

    /// The working directory of the child. `None` for the directory of this
    /// process.
    #[must_use]
    pub fn cwd_path(&self) -> Option<&Path> {
        self.cwd.as_deref()
    }

    /// The standard input of the child.
    #[must_use]
    pub fn stdin_kind(&self) -> &Stdin {
        &self.stdin
    }

    /// Where the two output streams of the child go.
    #[must_use]
    pub fn output_kind(&self) -> Output {
        self.output
    }

    /// How long the child can run.
    #[must_use]
    pub fn time_limit(&self) -> TimeLimit {
        self.limit
    }

    /// What a stop of the process does with the child.
    #[must_use]
    pub fn at_shutdown(&self) -> AtShutdown {
        self.at_shutdown
    }
}

/// The first word of a list of words that a constructor made. Each
/// constructor puts the program there, so the empty text never occurs.
fn program_of(argv: &[String]) -> &str {
    argv.first().map_or("", String::as_str)
}

/// How a child program ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ended {
    /// The child exited with this status.
    Code(u8),
    /// A signal with this number ended the child.
    Signal(i32),
}

impl Ended {
    /// The number that `returncode` of Python holds for this end: the exit
    /// status, or the negative number of the signal.
    ///
    /// The Python origin is each read of `returncode`, for example
    /// `caregiver/src/caregiver/timers.py:373`. CPython makes the number in
    /// `_handle_exitstatus` (`subprocess.py:1997-2004`, version 3.13).
    ///
    /// ```
    /// use creche_runtime::command::Ended;
    ///
    /// assert_eq!(Ended::Code(3).python_returncode(), 3);
    /// assert_eq!(Ended::Signal(15).python_returncode(), -15);
    /// ```
    #[must_use]
    pub fn python_returncode(self) -> i32 {
        match self {
            Self::Code(status) => i32::from(status),
            Self::Signal(number) => number.saturating_neg(),
        }
    }

    /// How a child ended, from the two readers of its wait status: the exit
    /// status and the signal.
    ///
    /// The operating system gives one of the two, and an exit status from 0
    /// to 255. Each other pair is an end that is not known.
    ///
    /// The Python origin is `_handle_exitstatus` of CPython
    /// (`subprocess.py:1997-2004`, version 3.13).
    fn from_status(code: Option<i32>, signal: Option<i32>) -> Self {
        match (code, signal) {
            (Some(code), _) => Self::Code(u8::try_from(code).unwrap_or(STATUS_UNKNOWN)),
            (None, Some(number)) => Self::Signal(number),
            (None, None) => Self::Code(STATUS_UNKNOWN),
        }
    }

    /// How a child ended, from the wait status that the operating system
    /// gave. [`Ended::from_status`] names the Python origin.
    fn of(status: ExitStatus) -> Self {
        Self::from_status(status.code(), status.signal())
    }
}

/// A child program that ran to its end.
///
/// `Debug` prints how the child ended. It prints no byte of a stream and no
/// count of the bytes: a child can write a secret to its output, and the
/// count is then the length of that secret.
///
/// A runner makes the value. A test makes one for the script of a fake
/// runner:
///
/// ```
/// use creche_runtime::command::{Ended, Finished};
///
/// let finished = Finished::new(Ended::Code(1)).with_stderr("no such sandbox\n");
///
/// assert_eq!(finished.ended(), Ended::Code(1));
/// assert_eq!(finished.stdout(), b"");
/// assert_eq!(finished.stderr(), b"no such sandbox\n");
/// assert_eq!(format!("{finished:?}"), "Finished { ended: Code(1), .. }");
/// ```
///
/// Code outside this module cannot build the value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_runtime::command::{Ended, Finished};
///
/// let finished = Finished {
///     ended: Ended::Code(1),
///     stdout: Vec::new(),
///     stderr: b"no such sandbox\n".to_vec(),
/// };
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Finished {
    ended: Ended,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

impl Finished {
    /// A child that ended in this way and wrote no byte.
    ///
    /// The Python origin is the `CompletedProcess` that `subprocess.run`
    /// returns, for example at `caregiver/src/caregiver/driver.py:87-89`.
    #[must_use]
    pub fn new(ended: Ended) -> Self {
        Self {
            ended,
            stdout: Vec::new(),
            stderr: Vec::new(),
        }
    }

    /// The value with these bytes as the standard output.
    ///
    /// The Python origin is the `stdout` argument of a `CompletedProcess`
    /// that a test makes, for example at
    /// `caregiver/tests/test_driver.py:33`.
    #[must_use]
    pub fn with_stdout(mut self, bytes: impl Into<Vec<u8>>) -> Self {
        self.stdout = bytes.into();

        self
    }

    /// The value with these bytes as the standard error.
    ///
    /// The Python origin is the `stderr` argument of a `CompletedProcess`
    /// that a test makes, for example at
    /// `caregiver/tests/test_driver.py:33`.
    #[must_use]
    pub fn with_stderr(mut self, bytes: impl Into<Vec<u8>>) -> Self {
        self.stderr = bytes.into();

        self
    }

    /// How the child ended. An exit status that is not 0 is a result here
    /// and not an error: the caller decides what it means.
    ///
    /// The Python origin is each read of `returncode`, for example
    /// `caregiver/src/caregiver/timers.py:373`.
    #[must_use]
    pub fn ended(&self) -> Ended {
        self.ended
    }

    /// Each byte of the standard output. Empty for [`Output::Inherit`].
    ///
    /// The Python origin is each read of `stdout`, for example
    /// `caregiver/src/caregiver/timers.py:388`. That value is text.
    /// [`python_text`] makes the same text from these bytes.
    #[must_use]
    pub fn stdout(&self) -> &[u8] {
        &self.stdout
    }

    /// Each byte of the standard error. Empty for [`Output::Inherit`].
    ///
    /// The Python origin is each read of `stderr`, for example
    /// `caregiver/src/caregiver/timers.py:374`.
    #[must_use]
    pub fn stderr(&self) -> &[u8] {
        &self.stderr
    }
}

impl fmt::Debug for Finished {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Finished")
            .field("ended", &self.ended)
            .finish_non_exhaustive()
    }
}

/// Why a run gave no [`Finished`].
///
/// No variant holds a byte of the output or of the input of the child.
/// `TimeoutExpired` of Python holds the output up to the time limit
/// (`subprocess.py:1263-1272`, version 3.13), and [`RunError::TimedOut`]
/// holds none.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunError {
    /// The operating system did not start the program.
    NotStarted {
        /// The text of the error, as `strerror` of Python gives it.
        os_text: String,
    },
    /// The child ran past its time limit, and the owner killed it.
    TimedOut {
        /// The time limit of the command.
        after: Duration,
    },
    /// The child wrote more than the cap to one stream, and the owner killed
    /// it.
    OutputTooLarge {
        /// The cap of the command.
        cap: ByteCap,
    },
    /// The process stops, the command says [`AtShutdown::Kill`], and the
    /// owner killed the child.
    Stopped,
    /// The owner gave no result, for one of three causes. The program can
    /// have run, in part or to its end.
    ///
    /// 1. The owner task panicked.
    /// 2. The runtime stopped the owner task, or no runtime ran on the
    ///    thread of the caller.
    /// 3. The operating system failed a read, a write or the wait call of
    ///    the owner. The log holds one `ERROR` line with the text of that
    ///    error.
    OwnerLost,
}

impl fmt::Display for RunError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotStarted { os_text } => write!(f, "the program did not start: {os_text}"),
            Self::TimedOut { after } => write!(
                f,
                "the program ran past its limit of {} seconds",
                after.as_secs_f64()
            ),
            Self::OutputTooLarge { cap } => {
                write!(f, "the program wrote more than {} bytes", cap.get())
            }
            Self::Stopped => f.write_str("the process stops, and the program was killed"),
            Self::OwnerLost => f.write_str("the task that owns the program was lost"),
        }
    }
}

impl Error for RunError {}

/// Runs a [`Command`] to its end.
///
/// A service takes a runner as a generic parameter: `R: CommandRunner`.
/// [`TokioRunner`] starts a real program. A test gives the fake runner of
/// `creche-testkit`.
///
/// The future of [`CommandRunner::run`] is `Send`, so a caller can run it in
/// a task. Write the method as `async fn` in an implementation.
pub trait CommandRunner: Send + Sync {
    /// Runs `command` and waits for its end.
    ///
    /// A caller that drops the future does not leave a child with no owner.
    /// The owner applies the [`AtShutdown`] of the command.
    ///
    /// # Errors
    ///
    /// [`RunError`] when the run gives no [`Finished`]. An exit status that
    /// is not 0 is no error.
    fn run(&self, command: Command) -> impl Future<Output = Result<Finished, RunError>> + Send;
}

impl<R: CommandRunner> CommandRunner for Arc<R> {
    fn run(&self, command: Command) -> impl Future<Output = Result<Finished, RunError>> + Send {
        (**self).run(command)
    }
}

impl<R: CommandRunner> CommandRunner for &R {
    fn run(&self, command: Command) -> impl Future<Output = Result<Finished, RunError>> + Send {
        (**self).run(command)
    }
}

/// The runner that starts a real program, with `tokio`.
///
/// A clone is the same runner: its owner tasks are tasks of the same
/// [`Tasks`].
///
/// ```
/// use std::time::Duration;
///
/// use creche_runtime::command::{
///     AtShutdown, Command, CommandRunner, Ended, TimeLimit, TokioRunner,
/// };
/// use creche_runtime::tasks::{Tasks, shutdown_pair};
///
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
/// let (_trigger, shutdown) = shutdown_pair();
/// let runner = TokioRunner::new(Tasks::new(shutdown));
///
/// let limit = TimeLimit::After(Duration::from_secs(30));
/// let command =
///     Command::new("/bin/sh", limit, AtShutdown::Finish).args(["-c", "echo done; exit 3"]);
/// let finished = runtime.block_on(runner.run(command))?;
///
/// // An exit status that is not 0 is a result, and the caller reads it.
/// assert_eq!(finished.ended(), Ended::Code(3));
/// assert_eq!(finished.stdout(), b"done\n");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a runner from its parts. Only
/// [`TokioRunner::new`] gives one:
///
/// ```compile_fail,E0451
/// use creche_runtime::command::TokioRunner;
/// use creche_runtime::tasks::{Tasks, shutdown_pair};
///
/// let (_trigger, shutdown) = shutdown_pair();
/// let runner = TokioRunner {
///     tasks: Tasks::new(shutdown),
/// };
/// ```
///
/// The same lines build with the constructor:
///
/// ```
/// use creche_runtime::command::TokioRunner;
/// use creche_runtime::tasks::{Tasks, shutdown_pair};
///
/// let (_trigger, shutdown) = shutdown_pair();
/// let runner = TokioRunner::new(Tasks::new(shutdown));
/// ```
#[derive(Debug, Clone)]
pub struct TokioRunner {
    tasks: Tasks,
}

impl TokioRunner {
    /// A runner whose owner tasks are tasks of `tasks`. A drain of `tasks`
    /// then waits for each child that must end whole.
    ///
    /// The function has no Python origin: `subprocess.run` blocks its
    /// thread, and no task owns the child.
    #[must_use]
    pub fn new(tasks: Tasks) -> Self {
        Self { tasks }
    }
}

impl CommandRunner for TokioRunner {
    /// Starts the program of `command` and waits for its end. The module
    /// comment says how the owner task ends a run.
    ///
    /// The child gets the exact words of the command. No shell reads them.
    ///
    /// With [`AtShutdown::Kill`], a run that begins after the stop signal
    /// starts no program and gives [`RunError::Stopped`]. With
    /// [`AtShutdown::Finish`], such a run starts its program and runs to its
    /// end.
    ///
    /// A run whose caller went away gives its result to no code. The owner
    /// then writes one `WARNING` line for a result that is no success. The
    /// line holds the program and the reason. It holds no byte of the
    /// output.
    ///
    /// The Python origin is each `subprocess.run` call of a service, for
    /// example `_run` of `caregiver/src/caregiver/driver.py:84-105`. The
    /// handler of SIGTERM in `caregiver/src/caregiver/loop.py:1237-1240`
    /// only sets a flag, so such a call runs to its end at a stop:
    /// [`AtShutdown::Finish`] is that behavior.
    ///
    /// # Errors
    ///
    /// - [`RunError::NotStarted`] when the operating system does not start
    ///   the program, when a name of the environment holds `=`, and in a
    ///   runtime with no timer or with no I/O driver.
    /// - [`RunError::TimedOut`] and [`RunError::OutputTooLarge`] after the
    ///   owner killed the child for that reason.
    /// - [`RunError::Stopped`] under [`AtShutdown::Kill`], after the stop
    ///   signal.
    /// - [`RunError::OwnerLost`] when the owner task panicked, when the
    ///   runtime stopped it, on a thread with no runtime, and when the
    ///   operating system failed a read, a write or the wait call.
    async fn run(&self, command: Command) -> Result<Finished, RunError> {
        let caller = CancellationToken::new();
        // The caller drops this value when it goes away, and at its own
        // end. The owner reads the first case from the token.
        let _here = caller.clone().drop_guard();
        let owner = own(command, self.tasks.shutdown().clone(), caller);

        match self.tasks.spawn_must_complete(OWNER_TASK, owner).await {
            Ok(result) => result,
            Err(_lost) => Err(RunError::OwnerLost),
        }
    }
}

/// The owner task of one run: the run itself, and then the line for a
/// result that no caller reads.
///
/// The function has no Python origin: `subprocess.run` has no owner task.
async fn own(
    command: Command,
    shutdown: Shutdown,
    caller: CancellationToken,
) -> Result<Finished, RunError> {
    let result = run_child(&command, &shutdown, &caller).await;

    if caller.is_cancelled() {
        report_unread(command.program(), &result);
    }

    result
}

/// Writes the one line of a result that no caller reads, when the result is
/// no success.
///
/// Two results get no line. A child that the owner killed because its caller
/// went away: the command asked for that kill. A run that ended with
/// [`RunError::OwnerLost`]: the log holds the `ERROR` line of its cause.
///
/// The Python origin is the done-callback of
/// `attendance/src/attendance/tasks.py:17-31`, which writes one line for a
/// task that failed and that no caller waits for.
fn report_unread(program: &str, result: &Result<Finished, RunError>) {
    let reason = match result {
        Ok(finished) => match finished.ended {
            Ended::Code(0) => return,
            Ended::Code(status) => format!("exit status {status}"),
            Ended::Signal(number) => format!("signal {number}"),
        },
        Err(RunError::Stopped | RunError::OwnerLost) => return,
        Err(error) => error.to_string(),
    };

    crate::warning!(
        LOG_TARGET,
        "the program {program} gave its result to no caller: {reason}"
    );
}

/// Why the owner stopped its wait for the child.
enum Verdict {
    /// The child ended and each stream is at its end, or the exchange was
    /// cut.
    Done(Result<Streams, Cut>),
    /// The time limit of the command passed.
    PastLimit(Duration),
    /// The command says [`AtShutdown::Kill`], and the stop signal came or
    /// the caller went away.
    Stop,
}

/// What a child that ran to its end gives: the bytes of its standard
/// output, the bytes of its standard error, and the result of the wait call.
type Streams = (Vec<u8>, Vec<u8>, io::Result<ExitStatus>);

/// Why an exchange with a child ended before the end of the child.
#[derive(Debug, PartialEq, Eq)]
enum Cut {
    /// A stream of the child held more than this cap.
    PastCap(ByteCap),
    /// The operating system failed a read from a stream, or a write to the
    /// standard input. The log holds the one line of that error.
    Failed,
}

impl Cut {
    /// The error of a run whose exchange ended in this way.
    ///
    /// The function has no Python origin: `subprocess.run` raises the error
    /// of the operating system itself and has no cap.
    fn error(self) -> RunError {
        match self {
            Self::PastCap(cap) => RunError::OutputTooLarge { cap },
            Self::Failed => RunError::OwnerLost,
        }
    }
}

/// Starts the program of `command` and holds it to the end of the run.
///
/// The Python origin is `run` of CPython (`subprocess.py:512-580`, version
/// 3.13), which each service calls.
async fn run_child(
    command: &Command,
    shutdown: &Shutdown,
    caller: &CancellationToken,
) -> Result<Finished, RunError> {
    let kills = command.at_shutdown == AtShutdown::Kill;
    if kills && (shutdown.is_cancelled() || caller.is_cancelled()) {
        return Err(RunError::Stopped);
    }

    let mut program = tokio_command(&command.argv, &command.env, command.cwd.as_deref())?;
    let (input, cap) = set_streams(&mut program, &command.stdin, command.output);
    let mut child = start(program)?;

    let name = command.program();
    let stdin = child.stdin.take();
    let mut stdout = child.stdout.take().zip(cap);
    let mut stderr = child.stderr.take().zip(cap);

    // The exchange borrows the child and its pipes. It is gone at the end
    // of the statement, so the code below it can kill the child and read
    // the pipes.
    let verdict = first_event(
        exchange(
            name,
            &mut child,
            stdin,
            input,
            stdout.as_mut(),
            stderr.as_mut(),
        ),
        stop_asked(command.at_shutdown, shutdown, caller),
        past_limit(command.limit),
    )
    .await;

    let error = match verdict {
        Verdict::Done(Ok((stdout, stderr, waited))) => {
            return whole(name, &waited, stdout, stderr);
        }
        Verdict::Done(Err(cut)) => cut.error(),
        Verdict::PastLimit(after) => RunError::TimedOut { after },
        Verdict::Stop => RunError::Stopped,
    };
    kill(name, &mut child, stdout.as_mut(), stderr.as_mut()).await;

    Err(error)
}

/// The result of a child `program` that ran to its end, from the result of
/// the wait call and the bytes of its two streams.
///
/// A wait call that fails gives no status: another part of the process took
/// it, or the process ignores SIGCHLD. The child ended, and how is not known.
/// The function then writes one `ERROR` line and gives
/// [`RunError::OwnerLost`]. CPython reads such an end as the return code 0
/// (`subprocess.py:2040-2049`, version 3.13).
///
/// The owner drops the child after that error, and the drop sends SIGKILL to
/// the id of the child: `tokio` holds that flag. The operating system can
/// give the id to another process by then.
fn whole(
    program: &str,
    waited: &io::Result<ExitStatus>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
) -> Result<Finished, RunError> {
    match waited {
        Ok(status) => Ok(Finished {
            ended: Ended::of(*status),
            stdout,
            stderr,
        }),
        Err(error) => {
            report_no_end(program, error);

            Err(RunError::OwnerLost)
        }
    }
}

/// Writes the one line of a wait call that failed.
///
/// The function has no Python origin: CPython reads such an end as the
/// return code 0 and writes no line (`subprocess.py:2040-2049`, version
/// 3.13).
fn report_no_end(program: &str, error: &io::Error) {
    crate::error!(
        LOG_TARGET,
        "the wait for the program {program} failed, and its end is not known: {}",
        os_text(error)
    );
}

/// Waits for the first of the three events of a run: the end of the
/// exchange, the event that makes the owner kill the child, and the time
/// limit.
///
/// When two events are there at one poll, the end of the exchange is first.
/// A child that ended whole then gives its result.
///
/// The function has no Python origin. CPython checks its time limit between
/// two reads (`subprocess.py:2154-2155`, version 3.13).
async fn first_event<X, S, L>(exchange: X, stop: S, limit: L) -> Verdict
where
    X: Future<Output = Result<Streams, Cut>>,
    S: Future<Output = ()>,
    L: Future<Output = Duration>,
{
    // `unconstrained`: `tokio` gives a task a budget of steps for one poll.
    // An exchange whose stream always holds bytes uses the whole budget. A
    // timer and a stop signal then get no step in that poll, and in no
    // later poll. Without the budget, the two read their event at each
    // poll.
    tokio::select! {
        biased;

        done = exchange => Verdict::Done(done),
        () = unconstrained(stop) => Verdict::Stop,
        after = unconstrained(limit) => Verdict::PastLimit(after),
    }
}

/// The `tokio` command for these words, this environment and this working
/// directory. The child gets each word as it is.
///
/// The command kills its child when the code drops the child. An owner that
/// panics, or that the runtime stops, then leaves no child that runs.
///
/// Each form of the environment has a Python origin:
///
/// - [`EnvPolicy::Inherit`] is a call with no `env`, for example
///   `caregiver/src/caregiver/timers.py:392-398`.
/// - [`EnvPolicy::InheritAnd`] is `caregiver/src/caregiver/driver.py:85`.
/// - [`EnvPolicy::InheritOnly`] is
///   `noticeboard/src/noticeboard/registrywrite.py:461`. As that line does,
///   the function reads the variables of this process at the call.
/// - [`EnvPolicy::Exactly`] is `handover/src/handover/executor/host.py:399`.
fn tokio_command(
    argv: &[String],
    env: &EnvPolicy,
    cwd: Option<&Path>,
) -> Result<tokio::process::Command, RunError> {
    let Some((program, arguments)) = argv.split_first() else {
        return Err(not_started(NO_PROGRAM));
    };
    let mut command = tokio::process::Command::new(program);
    command.args(arguments);

    match env {
        EnvPolicy::Inherit => {}
        EnvPolicy::InheritAnd(pairs) => set_pairs(&mut command, pairs)?,
        EnvPolicy::InheritOnly(names) => {
            command.env_clear();
            command.envs(
                std::env::vars_os()
                    .filter(|(name, _)| names.iter().any(|kept| name == kept.as_str())),
            );
        }
        EnvPolicy::Exactly(pairs) => {
            command.env_clear();
            set_pairs(&mut command, pairs)?;
        }
    }

    if let Some(dir) = cwd {
        command.current_dir(dir);
    }
    command.kill_on_drop(true);

    Ok(command)
}

/// Gives each of `pairs` to the environment of `command`. A later pair
/// replaces an earlier pair of the same name.
///
/// A name that holds `=` is refused, as CPython refuses it
/// (`subprocess.py:1906-1907`, version 3.13). The operating system would
/// read such a pair as another name and another value.
fn set_pairs(
    command: &mut tokio::process::Command,
    pairs: &[(String, String)],
) -> Result<(), RunError> {
    for (name, value) in pairs {
        if name.contains('=') {
            return Err(not_started(ILLEGAL_NAME));
        }

        command.env(name, value);
    }

    Ok(())
}

/// Sets the three streams of `program`. Returns the bytes that the owner
/// writes to the standard input, and the cap of each output stream that the
/// owner reads.
///
/// The Python origin of the inherited streams is the call of the terminal
/// door, `door-tui/src/agent_door_tui/launch.py:70`. The origin of the input
/// bytes is `handover/src/handover/executor/host.py:241-249`, which gives a
/// secret to a child in that way.
fn set_streams<'a>(
    program: &mut tokio::process::Command,
    stdin: &'a Stdin,
    output: Output,
) -> (&'a [u8], Option<ByteCap>) {
    let input: &[u8] = match stdin {
        Stdin::Null => {
            program.stdin(Stdio::null());

            &[]
        }
        Stdin::Inherit => {
            program.stdin(Stdio::inherit());

            &[]
        }
        Stdin::Bytes(bytes) => {
            program.stdin(Stdio::piped());

            bytes
        }
    };
    let cap = match output {
        Output::Capture { cap } => {
            program.stdout(Stdio::piped()).stderr(Stdio::piped());

            Some(cap)
        }
        Output::Inherit => {
            program.stdout(Stdio::inherit()).stderr(Stdio::inherit());

            None
        }
    };

    (input, cap)
}

/// A [`RunError::NotStarted`] with a text of this module.
///
/// The function has no Python origin.
fn not_started(text: &str) -> RunError {
    RunError::NotStarted {
        os_text: text.to_owned(),
    }
}

/// Starts `program`, when the runtime of this thread can hold a child.
///
/// The Python origin is the `OSError` that each service takes from
/// `subprocess.run`, for example at
/// `door-tui/src/agent_door_tui/launch.py:69-76`: a program that does not
/// start is a result.
///
/// The child gets each descriptor of this process that has no close-on-exec
/// flag. CPython closes each descriptor past 2 in the child
/// (`subprocess.py:819`, version 3.13). The Rust standard library and `tokio`
/// open each descriptor with the flag, and `readfile` of this crate does the
/// same.
///
/// A program file can have no `#!` line and be no binary program. CPython
/// refuses such a file with the error "Exec format error"
/// (`subprocess.py:1912-1921`, version 3.13). This function does not refuse
/// each such file. For a program name with no `/`, the standard library
/// calls `execvp` when the command clears the environment or sets `PATH`,
/// and `execvp` gives the file to `/bin/sh`. Those commands have
/// [`EnvPolicy::InheritOnly`], [`EnvPolicy::Exactly`] or a `PATH` pair of
/// [`EnvPolicy::InheritAnd`]. For a path with a `/`, Linux refuses the file
/// as CPython does, and macOS gives it to `/bin/sh`.
///
/// One error leaves a program with no owner. `tokio` starts the program
/// first and gives its pipes to the I/O driver after that. When the driver
/// refuses a pipe, the program runs and this function gives
/// [`RunError::NotStarted`]. The pipes of that program are then closed.
/// [`can_hold_child`] gives the driver a pipe first, so this case needs a
/// driver that takes one pipe and refuses the next one.
fn start(mut program: tokio::process::Command) -> Result<Child, RunError> {
    can_hold_child()?;

    program.spawn().map_err(|error| RunError::NotStarted {
        os_text: os_text(&error),
    })
}

/// Checks that the runtime of this thread can hold a child program.
///
/// `tokio` starts the program first and builds its own parts for the child
/// after that. On a thread with no runtime, and in a runtime with no I/O
/// driver, it panics at that second step. The program then runs with no
/// owner. This check refuses before the program starts.
///
/// The signal driver of `tokio` is a part of its I/O driver, so one check
/// holds the two. The owner also needs the timer, for the time limit and
/// for the read of the streams after a kill.
///
/// The function has no Python origin: `subprocess.run` needs no runtime.
fn can_hold_child() -> Result<(), RunError> {
    if Handle::try_current().is_err() {
        return Err(not_started(NO_RUNTIME));
    }

    // `sleep` panics in a runtime with no timer, when the code makes the
    // future.
    if panic::catch_unwind(|| drop(tokio::time::sleep(Duration::ZERO))).is_err() {
        return Err(not_started(NO_TIMER));
    }

    // A pipe of `tokio` panics in a runtime with no I/O driver.
    match panic::catch_unwind(tokio::net::unix::pipe::pipe) {
        Ok(Ok(pipe)) => {
            drop(pipe);

            Ok(())
        }
        // The operating system gives no pipe, so it gives the child none.
        Ok(Err(error)) => Err(RunError::NotStarted {
            os_text: os_text(&error),
        }),
        Err(payload) => {
            drop(payload);

            Err(not_started(NO_IO_DRIVER))
        }
    }
}

/// Writes the input, reads the two output streams and waits for the end of
/// the child, all at one time. A child that reads its input late, or that
/// fills one pipe before it reads, then does not stop the exchange.
///
/// The future ends when the child ended and each stream is at its end. A
/// program that the child started can hold a stream open after the end of
/// the child. The exchange then continues to the time limit of the command,
/// as the exchange of CPython does. It ends early, with a [`Cut`], when a
/// stream holds more than its cap and when the operating system fails a
/// read or a write.
///
/// CPython does the same exchange in `_communicate`
/// (`subprocess.py:2094-2203`, version 3.13).
async fn exchange(
    program: &str,
    child: &mut Child,
    stdin: Option<ChildStdin>,
    input: &[u8],
    stdout: Option<&mut (ChildStdout, ByteCap)>,
    stderr: Option<&mut (ChildStderr, ByteCap)>,
) -> Result<Streams, Cut> {
    let ((), stdout, stderr, waited) = tokio::try_join!(
        feed(program, stdin, input),
        capture(program, stdout),
        capture(program, stderr),
        async { Ok(child.wait().await) },
    )?;

    Ok((stdout, stderr, waited))
}

/// Writes `input` to the standard input of the child `program` and closes
/// the pipe.
///
/// A child can end, or close its input, before it read each byte. The write
/// then fails with a broken pipe, and that is no error of the run: the end
/// of the child says what occurred. CPython ignores the same error
/// (`subprocess.py:2164-2168`, version 3.13).
///
/// Each other error of the write cuts the exchange, as it ends the call of
/// CPython. The function writes one `ERROR` line for it. The line holds no
/// byte of the input.
async fn feed<W>(program: &str, stdin: Option<W>, input: &[u8]) -> Result<(), Cut>
where
    W: AsyncWrite + Unpin,
{
    let Some(mut pipe) = stdin else {
        return Ok(());
    };

    match pipe.write_all(input).await {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::BrokenPipe => Ok(()),
        Err(error) => {
            crate::error!(
                LOG_TARGET,
                "the write to the input of the program {program} failed: {}",
                os_text(&error)
            );

            Err(Cut::Failed)
        }
    }
}

/// Reads a stream of the child `program` to its end. An absent stream gives
/// no byte. A stream that holds more than its cap gives [`Cut::PastCap`] at
/// the first byte past the cap.
///
/// A read error cuts the exchange, as it ends the call of CPython. The
/// function writes one `ERROR` line for it. The line holds no byte of the
/// stream.
///
/// CPython reads a stream with no cap (`subprocess.py:2173-2177`, version
/// 3.13).
async fn capture<R>(program: &str, stream: Option<&mut (R, ByteCap)>) -> Result<Vec<u8>, Cut>
where
    R: AsyncRead + Unpin,
{
    let Some((pipe, cap)) = stream else {
        return Ok(Vec::new());
    };
    // One byte past the cap shows that the child wrote more than the cap.
    let most = u64::try_from(cap.get())
        .unwrap_or(u64::MAX)
        .saturating_add(1);
    let mut bytes = Vec::new();

    if let Err(error) = pipe.take(most).read_to_end(&mut bytes).await {
        crate::error!(
            LOG_TARGET,
            "a read from the program {program} failed: {}",
            os_text(&error)
        );

        return Err(Cut::Failed);
    }

    if bytes.len() > cap.get() {
        return Err(Cut::PastCap(*cap));
    }

    Ok(bytes)
}

/// Waits for the event that makes the owner kill the child: the stop signal,
/// or a caller that went away. For [`AtShutdown::Finish`] the future never
/// ends.
///
/// The function has no Python origin: `subprocess.run` reads no stop signal
/// (`caregiver/src/caregiver/loop.py:1237-1240` only sets a flag).
async fn stop_asked(at_shutdown: AtShutdown, shutdown: &Shutdown, caller: &CancellationToken) {
    match at_shutdown {
        AtShutdown::Finish => future::pending().await,
        AtShutdown::Kill => tokio::select! {
            () = shutdown.cancelled() => {}
            () = caller.cancelled() => {}
        },
    }
}

/// Waits for the time limit and gives it. For [`TimeLimit::None`] the future
/// never ends.
///
/// The Python origin is the `timeout` of each `subprocess.run` call, for
/// example `caregiver/src/caregiver/driver.py:88`.
async fn past_limit(limit: TimeLimit) -> Duration {
    match limit {
        TimeLimit::After(after) => {
            tokio::time::sleep(after).await;

            after
        }
        TimeLimit::None => future::pending().await,
    }
}

/// The two calls that [`kill`] makes on a child program. The child of
/// `tokio` is the one implementation outside the tests. A test gives a
/// child whose end it controls.
///
/// The trait has no Python origin. A Python test replaces `subprocess.run`
/// as a whole (`caregiver/tests/test_driver.py:23-52`).
trait Ending {
    /// Sends SIGKILL to the child and does not wait.
    fn kill_now(&mut self) -> io::Result<()>;

    /// Waits for the end of the child.
    fn end(&mut self) -> impl Future<Output = io::Result<ExitStatus>> + Send;
}

impl Ending for Child {
    fn kill_now(&mut self) -> io::Result<()> {
        self.start_kill()
    }

    fn end(&mut self) -> impl Future<Output = io::Result<ExitStatus>> + Send {
        self.wait()
    }
}

/// Kills the child `program` and waits for its end. Then reads each of its
/// streams to the end, for [`AFTER_KILL`] at most.
///
/// The wait for the end of the child has no time limit. SIGKILL ends a
/// child, and the owner leaves no child that runs. When the operating system
/// refuses the signal, the function writes one `ERROR` line, and the owner
/// still waits for the end of the child.
///
/// A program that the child started can hold a stream open after the kill.
/// The time limit of the read keeps the owner from a wait with no end.
///
/// The Python origin is the kill at the time limit in `run` of CPython
/// (`subprocess.py:557-570`, version 3.13). That code waits for the end of
/// the child in the same way and reads no stream after it. This function
/// reads each stream for [`AFTER_KILL`] at most.
async fn kill<C, O, E>(
    program: &str,
    child: &mut C,
    stdout: Option<&mut (O, ByteCap)>,
    stderr: Option<&mut (E, ByteCap)>,
) where
    C: Ending,
    O: AsyncRead + Unpin,
    E: AsyncRead + Unpin,
{
    // The call does nothing when the child ended first.
    if let Err(error) = child.kill_now() {
        crate::error!(
            LOG_TARGET,
            "the kill of the program {program} failed, and its owner waits for its end: {}",
            os_text(&error)
        );
    }

    if let Err(error) = child.end().await {
        report_no_end(program, &error);
    }

    let streams = async {
        tokio::join!(
            discard(stdout.map(|(pipe, _)| pipe)),
            discard(stderr.map(|(pipe, _)| pipe)),
        )
    };

    // After the limit, a program that the child started holds a stream. The
    // owner drops its end of the stream and does not wait for that program.
    drop(tokio::time::timeout(AFTER_KILL, streams).await);
}

/// Reads a stream of the child to its end and drops each byte. A read error
/// is the end of the stream.
///
/// The Python origin is `_drained` of
/// `attendance/src/attendance/exec_channel.py:231-241`.
async fn discard<R>(pipe: Option<&mut R>)
where
    R: AsyncRead + Unpin,
{
    let Some(pipe) = pipe else {
        return;
    };

    drop(tokio::io::copy(pipe, &mut tokio::io::sink()).await);
}

/// What [`python_text`] does with bytes that are not UTF-8.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decode {
    /// Refuse them, as `text=True` of Python does.
    Strict,
    /// Read each bad sequence as U+FFFD, as `errors="replace"` of Python
    /// does.
    Replace,
}

/// The output of a child as text, as `subprocess.run` of Python gives it
/// with `text=True`: UTF-8, and each CR LF and each CR as one LF.
///
/// The function reads UTF-8 in each locale. `text=True` of Python reads the
/// encoding of the locale (`subprocess.py:367-384`, version 3.13).
///
/// The Python origin is `_translate_newlines` of CPython
/// (`subprocess.py:1098-1100`, version 3.13). `text=True` at
/// `caregiver/src/caregiver/driver.py:88` is the strict form, and
/// `errors="replace"` at `handover/src/handover/executor/host.py:404` is the
/// other form.
///
/// The text mode of a Python file has the same rule. `token` thus reads the
/// content of a token file with this function, as `Path.read_text` does at
/// `chaperone/src/chaperone/delegate.py:148`. The rule for the line ends
/// has one home in the workspace: `creche_util::pytext::universal_newlines`.
/// This function calls it.
///
/// ```
/// use creche_runtime::command::{Decode, python_text};
///
/// assert_eq!(
///     python_text(b"one\r\ntwo\rthree\n", Decode::Strict).as_deref(),
///     Ok("one\ntwo\nthree\n")
/// );
/// assert!(python_text(b"caf\xe9", Decode::Strict).is_err());
/// assert_eq!(
///     python_text(b"caf\xe9", Decode::Replace).as_deref(),
///     Ok("caf\u{fffd}")
/// );
/// ```
///
/// # Errors
///
/// [`Utf8Error`] for bytes that are not UTF-8, under [`Decode::Strict`].
pub fn python_text(bytes: &[u8], decode: Decode) -> Result<String, Utf8Error> {
    let text = match decode {
        Decode::Strict => Cow::Borrowed(std::str::from_utf8(bytes)?),
        Decode::Replace => String::from_utf8_lossy(bytes),
    };

    Ok(pytext::universal_newlines(&text).into_owned())
}

/// A child program that lives long and talks on its three pipes, as data.
///
/// A new command has these parts until a builder changes them: no argument,
/// the environment of this process and the working directory of this
/// process. It has no time limit and no [`AtShutdown`]: the caller owns the
/// child and stops it through its [`ChildGuard`].
///
/// ```
/// use creche_runtime::command::{EnvPolicy, PipedCommand};
///
/// let command = PipedCommand::new("sbx")
///     .args(["exec", "chat-s1"])
///     .arg("--")
///     .arg("playpen");
///
/// assert_eq!(command.program(), "sbx");
/// assert_eq!(command.argv(), ["sbx", "exec", "chat-s1", "--", "playpen"]);
/// assert_eq!(command.env_policy(), &EnvPolicy::Inherit);
/// assert_eq!(command.cwd_path(), None);
/// ```
///
/// Code outside this module cannot build a command from raw parts:
///
/// ```compile_fail,E0451
/// use creche_runtime::command::{EnvPolicy, PipedCommand};
///
/// let command = PipedCommand {
///     argv: Vec::new(),
///     env: EnvPolicy::Inherit,
///     cwd: None,
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PipedCommand {
    /// The program, then each argument. The list is never empty.
    argv: Vec<String>,
    env: EnvPolicy,
    cwd: Option<PathBuf>,
}

impl PipedCommand {
    /// A run of `program` with no argument.
    #[must_use]
    pub fn new(program: impl Into<String>) -> Self {
        Self {
            argv: vec![program.into()],
            env: EnvPolicy::Inherit,
            cwd: None,
        }
    }

    /// The command with one more argument. The child gets the word as it is.
    #[must_use]
    pub fn arg(mut self, word: impl Into<String>) -> Self {
        self.argv.push(word.into());

        self
    }

    /// The command with each of `words` as one more argument, in order.
    #[must_use]
    pub fn args<I, S>(mut self, words: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.argv.extend(words.into_iter().map(Into::into));

        self
    }

    /// The command with this environment.
    #[must_use]
    pub fn env(mut self, policy: EnvPolicy) -> Self {
        self.env = policy;

        self
    }

    /// The command with this working directory.
    #[must_use]
    pub fn cwd(mut self, dir: impl Into<PathBuf>) -> Self {
        self.cwd = Some(dir.into());

        self
    }

    /// The program: the first word.
    #[must_use]
    pub fn program(&self) -> &str {
        program_of(&self.argv)
    }

    /// The program, then each argument: the words that the child gets.
    #[must_use]
    pub fn argv(&self) -> &[String] {
        &self.argv
    }

    /// The environment of the child.
    #[must_use]
    pub fn env_policy(&self) -> &EnvPolicy {
        &self.env
    }

    /// The working directory of the child. `None` for the directory of this
    /// process.
    #[must_use]
    pub fn cwd_path(&self) -> Option<&Path> {
        self.cwd.as_deref()
    }
}

/// Starts `command` with a pipe for each of its three streams.
///
/// The caller is the owner of the child: one task holds the [`Piped`], and
/// each other task asks that task through a message.
///
/// The child gets the exact words of the command. No shell reads them.
///
/// Call the function inside the runtime. On a thread with no runtime, and in
/// a runtime with no timer or with no I/O driver, no program starts.
///
/// The Python origin is `ExecChannel.start` of
/// `attendance/src/attendance/exec_channel.py:103-121`.
///
/// # Errors
///
/// [`RunError::NotStarted`] when the operating system does not start the
/// program, when a name of the environment holds `=`, and when the thread
/// has no runtime that can hold a child.
pub fn spawn_piped(command: PipedCommand) -> Result<Piped, RunError> {
    let mut program = tokio_command(&command.argv, &command.env, command.cwd.as_deref())?;
    program
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = start(program)?;

    let (Some(stdin), Some(stdout), Some(stderr)) =
        (child.stdin.take(), child.stdout.take(), child.stderr.take())
    else {
        // The drop of the child kills it.
        return Err(not_started(NO_PIPE));
    };

    Ok(Piped {
        stdin,
        stdout,
        stderr,
        child: ChildGuard {
            child,
            program: command.program().to_owned(),
            ended: None,
        },
    })
}

/// A child program that runs, with its three pipes.
///
/// Only [`spawn_piped`] gives a value. [`Piped::into_parts`] gives each pipe
/// and the guard of the child to the task that owns it:
///
/// ```
/// use creche_runtime::command::{ChildGuard, Piped};
/// use tokio::process::{ChildStderr, ChildStdin, ChildStdout};
///
/// fn parts(piped: Piped) -> (ChildStdin, ChildStdout, ChildStderr, ChildGuard) {
///     piped.into_parts()
/// }
/// ```
///
/// Code outside this module cannot build a value. The three pipes and the
/// guard of a value are thus parts of one child:
///
/// ```compile_fail,E0451
/// use creche_runtime::command::{ChildGuard, Piped};
/// use tokio::process::{ChildStderr, ChildStdin, ChildStdout};
///
/// fn whole(
///     stdin: ChildStdin,
///     stdout: ChildStdout,
///     stderr: ChildStderr,
///     child: ChildGuard,
/// ) -> Piped {
///     Piped {
///         stdin,
///         stdout,
///         stderr,
///         child,
///     }
/// }
/// ```
#[derive(Debug)]
pub struct Piped {
    stdin: ChildStdin,
    stdout: ChildStdout,
    stderr: ChildStderr,
    child: ChildGuard,
}

impl Piped {
    /// The four parts of the child, each one as a value that one task owns:
    /// the standard input, the standard output, the standard error, and the
    /// guard that stops the child and waits for it.
    ///
    /// To drop the standard input closes the pipe. To drop the guard kills
    /// the child.
    ///
    /// The Python origin is the three pipes of the process that
    /// `ExecChannel.start` keeps
    /// (`attendance/src/attendance/exec_channel.py:108-113`).
    #[must_use]
    pub fn into_parts(self) -> (ChildStdin, ChildStdout, ChildStderr, ChildGuard) {
        (self.stdin, self.stdout, self.stderr, self.child)
    }
}

/// The one value that stops a child program and waits for its end.
///
/// To drop the guard kills the child. The runtime then reads the end of the
/// child, so the operating system keeps no entry for it.
///
/// Call each function inside the runtime that started the child.
///
/// Only [`spawn_piped`] gives a guard:
///
/// ```
/// use creche_runtime::command::{ChildGuard, Ended, PipedCommand, RunError, spawn_piped};
///
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
/// let ended = runtime.block_on(async {
///     let (stdin, _stdout, _stderr, child) = spawn_piped(PipedCommand::new("cat"))?.into_parts();
///     let mut child: ChildGuard = child;
///
///     // The child reads the end of its input and exits.
///     drop(stdin);
///
///     Ok::<Ended, RunError>(child.wait().await)
/// })?;
///
/// assert_eq!(ended, Ended::Code(0));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a guard around a child that it
/// started itself. Such a child has no flag that kills it at the drop:
///
/// ```compile_fail,E0451
/// use creche_runtime::command::{ChildGuard, Ended, PipedCommand, RunError, spawn_piped};
///
/// fn guard(child: tokio::process::Child) -> ChildGuard {
///     ChildGuard {
///         child,
///         program: String::new(),
///         ended: None,
///     }
/// }
/// ```
#[derive(Debug)]
pub struct ChildGuard {
    child: Child,
    /// The program of the child, for a line in the log.
    program: String,
    /// How the child ended. `None` until a wait gives the end.
    ended: Option<Ended>,
}

impl ChildGuard {
    /// Sends SIGTERM to the child. The call does nothing after the child
    /// ended, so it never signals another process with the same id.
    ///
    /// The Python origin is the `process.terminate()` call of
    /// `attendance/src/attendance/exec_channel.py:176-177`.
    ///
    /// # Errors
    ///
    /// [`SignalError`] when the operating system refuses the signal.
    pub fn terminate(&self) -> Result<(), SignalError> {
        // After the end, the operating system can give the id of the child
        // to a new process.
        if self.ended.is_some() {
            return Ok(());
        }
        let Some(id) = self.child.id() else {
            return Ok(());
        };
        let Some(pid) = i32::try_from(id).ok().and_then(Pid::from_raw) else {
            return Err(SignalError {
                kind: io::ErrorKind::InvalidInput,
                os_text: String::from(NO_PROCESS_ID),
            });
        };

        signal_sent(kill_process(pid, Signal::TERM))
    }

    /// Sends SIGKILL to the child and does not wait. The call does nothing
    /// after the child ended.
    ///
    /// The Python origin is the `process.kill()` call of
    /// `attendance/src/attendance/exec_channel.py:282-283`. It ignores the
    /// error of a child that ended, and this function ignores each error of
    /// the signal: [`ChildGuard::wait`] says if the child ended.
    pub fn start_kill(&mut self) {
        if self.ended.is_some() {
            return;
        }

        drop(self.child.start_kill());
    }

    /// Waits for the end of the child. A second call gives the same end.
    ///
    /// The future is cancel safe: a caller can drop it and call the function
    /// again.
    ///
    /// When the wait call of the operating system fails, the function
    /// writes one `ERROR` line and gives the exit status 255. The end of the
    /// child is then not known, and no caller reads it as a success. The
    /// child watcher of `asyncio` gives the same status for such an end
    /// (`asyncio/unix_events.py:1000-1012` of CPython, version 3.13). After
    /// that, [`ChildGuard::terminate`] and [`ChildGuard::start_kill`] send
    /// no signal. The drop of the guard still sends SIGKILL to the id of the
    /// child: `tokio` holds that flag. A wait call fails only in a process
    /// that ignores SIGCHLD, or that waits for its children in a second
    /// place.
    ///
    /// The Python origin is the `process.wait()` call of
    /// `attendance/src/attendance/exec_channel.py:265`.
    pub async fn wait(&mut self) -> Ended {
        if let Some(ended) = self.ended {
            return ended;
        }

        let ended = match self.child.wait().await {
            Ok(status) => Ended::of(status),
            Err(error) => {
                report_no_end(&self.program, &error);

                Ended::Code(STATUS_UNKNOWN)
            }
        };
        self.ended = Some(ended);

        ended
    }

    /// Stops the child in order: SIGTERM, a wait of `grace` at most, SIGKILL,
    /// a second wait of `grace` at most.
    ///
    /// The function waits only for the child, and it holds no pipe. The
    /// owner of the three pipes reads them or drops them. Drop the standard
    /// input first: a child in good condition reads the end of its input and
    /// exits.
    ///
    /// A caller that drops the future stops the sequence at its current
    /// step. A second call starts the sequence again.
    ///
    /// The Python origin is `_reap` of
    /// `attendance/src/attendance/exec_channel.py:268-287`, after the
    /// `terminate` call of `close` (`:176-177`). That code differs in two
    /// ways. It closes the standard input before SIGTERM (`:172-174`).
    /// Inside each time limit it waits for the end of the child and of each
    /// pipe (`:244-265`).
    pub async fn end(&mut self, grace: Duration) -> EndOutcome {
        // An error of the signal changes no step: the wait and the kill
        // follow.
        drop(self.terminate());

        if let Ok(ended) = tokio::time::timeout(grace, self.wait()).await {
            return EndOutcome::Ended(ended);
        }

        self.start_kill();

        match tokio::time::timeout(grace, self.wait()).await {
            Ok(ended) => EndOutcome::Ended(ended),
            Err(_elapsed) => EndOutcome::LeftRunning,
        }
    }
}

/// What the result of a signal call means for a child that a guard holds.
///
/// The error "no such process" is no error: the child ended, and the wait
/// did not read its end yet. CPython ignores the same error
/// (`subprocess.py:2244-2248`, version 3.13). Each other error is a
/// [`SignalError`] with the text of the operating system.
fn signal_sent(result: Result<(), Errno>) -> Result<(), SignalError> {
    match result {
        Ok(()) | Err(Errno::SRCH) => Ok(()),
        Err(errno) => {
            let error = io::Error::from(errno);

            Err(SignalError {
                kind: error.kind(),
                os_text: os_text(&error),
            })
        }
    }
}

/// What [`ChildGuard::end`] found.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EndOutcome {
    /// The child ended.
    Ended(Ended),
    /// The child still runs after SIGKILL and the second wait.
    LeftRunning,
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::PermissionsExt;
    use std::pin::{Pin, pin};
    use std::process::Output as ChildOutput;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::task::{Context, Poll, Waker};
    use std::thread;
    use std::time::Instant;

    use creche_testkit::program::write_program;
    use creche_testkit::root::TempRoot;
    use rustix::io::{FdFlags, fcntl_setfd};
    use rustix::process::{WaitOptions, waitpid};
    use tokio::io::{AsyncBufReadExt, BufReader, ReadBuf};
    use tokio::runtime::{Builder, Runtime};

    use super::*;
    use crate::tasks::{Drained, ShutdownTrigger, shutdown_pair};

    /// The value of a variable of the tests. No output of `Debug` holds it.
    const SECRET_VALUE: &str = "correct horse battery staple";

    /// The longest time that a test waits for a step. The time is real, and
    /// a host with much load is slow.
    const LIMIT: Duration = Duration::from_secs(60);

    /// The time between two looks at a file or at a process.
    const TICK: Duration = Duration::from_millis(10);

    /// A time limit that no test waits for. A test moves the clock of
    /// `tokio` past it with [`skip`], after the program wrote its process
    /// id. The speed of the host then decides no result.
    const HOUR: Duration = Duration::from_secs(3600);

    /// A time limit that a scenario waits for in real time.
    const SHORT_LIMIT: Duration = Duration::from_secs(3);

    /// A time in which a test shows that an event does not come: a child
    /// that waits does not end, and a caller that waits gets no result.
    const GRACE: Duration = Duration::from_millis(300);

    /// The longest time of a test that shows a wait with no end. It is
    /// shorter than [`LIMIT`], so a defect fails the test soon.
    const STARVED: Duration = Duration::from_secs(10);

    /// A program that writes its process id to the file `pid` and then runs
    /// for longer than [`LIMIT`]: a test that waits for its end passes only
    /// when some code ended it. `exec` keeps the id, so the child is one
    /// process.
    const HOLD: &str = "echo $$ > \"$1/pid\"\nexec sleep 300\n";

    /// As [`HOLD`], and the program ignores SIGTERM. The program that `exec`
    /// starts ignores the signal too, so only SIGKILL ends the child.
    const DEAF_HOLD: &str = "trap '' TERM\necho $$ > \"$1/pid\"\nexec sleep 300\n";

    /// A program that writes its process id, waits for the file `go`, writes
    /// the file `done` and exits with status 0.
    const GATED: &str = "echo $$ > \"$1/pid\"\n\
                         while [ ! -e \"$1/go\" ]; do sleep 0.05; done\n\
                         echo whole > \"$1/done\"\n\
                         echo finished\n";

    /// A program that writes `$1` bytes to its standard output and `$2`
    /// bytes to its standard error.
    const WRITER: &str = "head -c \"$1\" /dev/zero\nhead -c \"$2\" /dev/zero >&2\n";

    /// A program that prints its process id and then runs for longer than
    /// [`LIMIT`].
    const PIPED_HOLD: &str = "echo $$\nexec sleep 300\n";

    /// As [`PIPED_HOLD`], and the program ignores SIGTERM. The program that
    /// `exec` starts ignores the signal too.
    const PIPED_DEAF: &str = "trap '' TERM\necho $$\nexec sleep 300\n";

    /// The program that prints its environment, one variable for each line.
    const ENV: &str = "/usr/bin/env";

    /// The shell of the tests that need no script file.
    const SHELL: &str = "/bin/sh";

    /// The number of SIGKILL.
    const KILLED: i32 = 9;

    /// The number of SIGPIPE.
    const BROKEN_PIPE: i32 = 13;

    /// The number of SIGTERM.
    const TERMINATED: i32 = 15;

    /// The number of the error EIO, on Linux and on macOS. Its text is
    /// `Input/output error` on both.
    const IO_ERROR: i32 = 5;

    /// The number of the error ECHILD, on Linux and on macOS. Its text is
    /// `No child processes` on both.
    const NO_CHILD: i32 = 10;

    /// The number of the error EPIPE, on Linux and on macOS.
    const PIPE_ERROR: i32 = 32;

    /// The number of the error EPERM, on Linux and on macOS. Its text is
    /// `Operation not permitted` on both.
    const NOT_PERMITTED: i32 = 1;

    fn limit() -> TimeLimit {
        TimeLimit::After(Duration::from_secs(30))
    }

    /// A runner that starts no program. It counts each call and answers with
    /// the words of the command as the standard output.
    #[derive(Debug, Default)]
    struct Echo {
        calls: AtomicUsize,
    }

    impl CommandRunner for Echo {
        async fn run(&self, command: Command) -> Result<Finished, RunError> {
            self.calls.fetch_add(1, Ordering::SeqCst);

            Ok(Finished::new(Ended::Code(0)).with_stdout(command.argv().join(" ")))
        }
    }

    /// A caller as a service writes it: generic over the runner.
    async fn head_of<R: CommandRunner>(runner: R) -> Result<Finished, RunError> {
        let command = Command::new("git", limit(), AtShutdown::Finish).args(["rev-parse", "HEAD"]);

        runner.run(command).await
    }

    /// Takes only a future that a task can run.
    fn sendable<F: Future + Send>(future: F) -> F {
        future
    }

    fn block_on<F: Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(future)
    }

    #[test]
    fn a_new_command_has_the_parts_that_the_doc_comment_names() {
        let command = Command::new("sbx", limit(), AtShutdown::Kill);

        assert_eq!(command.program(), "sbx");
        assert_eq!(command.argv(), ["sbx"]);
        assert_eq!(command.env_policy(), &EnvPolicy::Inherit);
        assert_eq!(command.cwd_path(), None);
        assert_eq!(command.stdin_kind(), &Stdin::Null);
        assert_eq!(
            command.output_kind(),
            Output::Capture {
                cap: ByteCap::ONE_MIB
            }
        );
        assert_eq!(command.time_limit(), limit());
        assert_eq!(command.at_shutdown(), AtShutdown::Kill);
    }

    #[test]
    fn each_builder_sets_its_part_and_no_other_part() {
        let env = EnvPolicy::Exactly(vec![(String::from("HOME"), String::from("/srv/home"))]);
        let command = Command::new("sbx", TimeLimit::None, AtShutdown::Finish)
            .arg("create")
            .args(["--name", "chat-s1"])
            .args(Vec::<String>::new())
            .env(env.clone())
            .cwd("/srv/registry")
            .stdin(Stdin::Inherit)
            .output(Output::Inherit);

        assert_eq!(command.program(), "sbx");
        assert_eq!(command.argv(), ["sbx", "create", "--name", "chat-s1"]);
        assert_eq!(command.env_policy(), &env);
        assert_eq!(command.cwd_path(), Some(Path::new("/srv/registry")));
        assert_eq!(command.stdin_kind(), &Stdin::Inherit);
        assert_eq!(command.output_kind(), Output::Inherit);
        assert_eq!(command.time_limit(), TimeLimit::None);
        assert_eq!(command.at_shutdown(), AtShutdown::Finish);
    }

    #[test]
    fn a_word_goes_to_the_child_as_it_is() {
        let words = ["a b", "", "--flag=x y", "$HOME", "'quoted'", "caf\u{e9}"];
        let command = Command::new("echo", limit(), AtShutdown::Kill).args(words);

        assert_eq!(
            command.argv().get(1..),
            Some(words.map(String::from).as_slice())
        );
    }

    #[test]
    fn the_debug_of_a_command_prints_no_byte_of_the_input_and_no_value() {
        let command = Command::new("age", limit(), AtShutdown::Kill)
            .arg("--decrypt")
            .env(EnvPolicy::InheritAnd(vec![(
                String::from("AGE_KEY"),
                String::from(SECRET_VALUE),
            )]))
            .stdin(Stdin::Bytes(SECRET_VALUE.as_bytes().to_vec()));
        let text = format!("{command:?}");

        assert!(text.contains("\"--decrypt\""), "{text}");
        assert!(text.contains("InheritAnd([\"AGE_KEY\"])"), "{text}");
        assert!(text.contains("stdin: Bytes(..)"), "{text}");
        assert!(!text.contains("correct"), "{text}");
        assert!(!text.contains("99, 111"), "{text}");
        // The input can be a token, so the text holds no count of its bytes.
        assert!(!text.contains(&SECRET_VALUE.len().to_string()), "{text}");
    }

    #[test]
    fn the_debug_of_each_environment_prints_names_only() {
        let pairs = vec![(String::from("TOKEN"), String::from(SECRET_VALUE))];
        let table = [
            (EnvPolicy::Inherit, "Inherit"),
            (
                EnvPolicy::InheritAnd(pairs.clone()),
                "InheritAnd([\"TOKEN\"])",
            ),
            (
                EnvPolicy::InheritOnly(vec![String::from("PATH")]),
                "InheritOnly([\"PATH\"])",
            ),
            (EnvPolicy::Exactly(pairs), "Exactly([\"TOKEN\"])"),
        ];

        for (policy, text) in table {
            assert_eq!(format!("{policy:?}"), text);
        }
    }

    #[test]
    fn the_debug_of_each_input_prints_no_byte_and_no_count() {
        assert_eq!(format!("{:?}", Stdin::Null), "Null");
        assert_eq!(format!("{:?}", Stdin::Inherit), "Inherit");
        assert_eq!(
            format!("{:?}", Stdin::Bytes(SECRET_VALUE.as_bytes().to_vec())),
            "Bytes(..)"
        );
    }

    #[test]
    fn the_debug_of_a_result_prints_no_byte_and_no_count() {
        let finished = Finished::new(Ended::Signal(9))
            .with_stdout(SECRET_VALUE)
            .with_stderr("warning");
        let text = format!("{finished:?}");

        assert_eq!(text, "Finished { ended: Signal(9), .. }");
        // The output can be a secret, so the text holds no count of the
        // bytes of a stream: not the 28 of stdout and not the 7 of stderr.
        assert!(!text.contains(&SECRET_VALUE.len().to_string()), "{text}");
        assert!(!text.contains('7'), "{text}");
    }

    #[test]
    fn a_result_gives_its_end_and_the_bytes_of_each_stream() {
        let plain = Finished::new(Ended::Code(2));

        assert_eq!(plain.ended(), Ended::Code(2));
        assert_eq!(plain.stdout(), b"");
        assert_eq!(plain.stderr(), b"");

        let full = plain
            .clone()
            .with_stdout(b"to-output\n".to_vec())
            .with_stderr("to-error\n");

        assert_eq!(full.ended(), Ended::Code(2));
        assert_eq!(full.stdout(), b"to-output\n");
        assert_eq!(full.stderr(), b"to-error\n");
        assert_ne!(full, plain);

        // A second call replaces the bytes of the first call.
        let replaced = full.clone().with_stdout("second").with_stderr(Vec::new());

        assert_eq!(replaced.stdout(), b"second");
        assert_eq!(replaced.stderr(), b"");
        assert_eq!(
            Finished::new(Ended::Code(2)).with_stdout("second"),
            replaced
        );
    }

    #[test]
    fn a_piped_command_has_the_same_builders_and_readers() {
        let env = EnvPolicy::InheritOnly(vec![String::from("PATH")]);
        let command = PipedCommand::new("sbx")
            .arg("exec")
            .args(["chat-s1", "--", "playpen"])
            .env(env.clone())
            .cwd("/srv/state");

        assert_eq!(command.program(), "sbx");
        assert_eq!(command.argv(), ["sbx", "exec", "chat-s1", "--", "playpen"]);
        assert_eq!(command.env_policy(), &env);
        assert_eq!(command.cwd_path(), Some(Path::new("/srv/state")));

        let plain = PipedCommand::new("pi");

        assert_eq!(plain.argv(), ["pi"]);
        assert_eq!(plain.env_policy(), &EnvPolicy::Inherit);
        assert_eq!(plain.cwd_path(), None);
    }

    #[test]
    fn a_generic_caller_takes_a_runner_a_reference_and_an_arc() {
        let echo = Echo::default();
        let by_reference = block_on(sendable(head_of(&echo))).unwrap();
        let shared = Arc::new(echo);
        let by_arc = block_on(sendable(head_of(Arc::clone(&shared)))).unwrap();
        let by_arc_reference = block_on(sendable(head_of(&shared))).unwrap();

        for finished in [by_reference, by_arc, by_arc_reference] {
            assert_eq!(finished.ended(), Ended::Code(0));
            assert_eq!(finished.stdout(), b"git rev-parse HEAD");
        }

        assert_eq!(shared.calls.load(Ordering::SeqCst), 3);

        let owned = block_on(sendable(head_of(Echo::default()))).unwrap();

        assert_eq!(owned.stdout(), b"git rev-parse HEAD");
    }

    #[test]
    fn a_run_error_names_its_reason_and_no_byte_of_the_output() {
        let table = [
            (
                RunError::NotStarted {
                    os_text: String::from("No such file or directory"),
                },
                "the program did not start: No such file or directory",
            ),
            (
                RunError::TimedOut {
                    after: Duration::from_millis(1500),
                },
                "the program ran past its limit of 1.5 seconds",
            ),
            (
                RunError::OutputTooLarge {
                    cap: ByteCap::ONE_MIB,
                },
                "the program wrote more than 1048576 bytes",
            ),
            (
                RunError::Stopped,
                "the process stops, and the program was killed",
            ),
            (
                RunError::OwnerLost,
                "the task that owns the program was lost",
            ),
        ];

        for (error, text) in table {
            assert_eq!(error.to_string(), text);
        }
    }

    /// A runtime with a timer and an I/O driver, as `service::run` builds it.
    fn runtime() -> Runtime {
        Builder::new_current_thread().enable_all().build().unwrap()
    }

    /// The parts of one test: a directory, the stop signal, the tasks and a
    /// runner.
    struct Bench {
        root: TempRoot,
        trigger: ShutdownTrigger,
        tasks: Tasks,
        runner: TokioRunner,
    }

    impl Bench {
        fn new() -> Self {
            let (trigger, shutdown) = shutdown_pair();
            let tasks = Tasks::new(shutdown);

            Self {
                root: TempRoot::new().unwrap(),
                trigger,
                runner: TokioRunner::new(tasks.clone()),
                tasks,
            }
        }

        /// Writes the script `text` as the program `name` and gives its path.
        fn program(&self, name: &str, text: &str) -> String {
            write_program(&self.root, name, text)
                .unwrap()
                .into_os_string()
                .into_string()
                .unwrap()
        }

        /// The directory of the test, as a word of a command.
        fn dir(&self) -> String {
            self.root.path().to_str().unwrap().to_owned()
        }

        /// The path of the file `name` in the directory of the test.
        fn file(&self, name: &str) -> PathBuf {
            self.root.path().join(name)
        }

        /// A run of the script `text` that gets the directory of the test as
        /// its first argument.
        fn command(&self, text: &str, limit: TimeLimit, at_shutdown: AtShutdown) -> Command {
            Command::new(self.program("program", text), limit, at_shutdown).arg(self.dir())
        }

        /// Runs `command` to its end, in a runtime of its own.
        fn run(&self, command: Command) -> Result<Finished, RunError> {
            runtime().block_on(within(self.runner.run(command)))
        }

        /// Opens the gate of [`GATED`].
        fn open_gate(&self) {
            fs::write(self.file("go"), b"").unwrap();
        }

        /// Waits for the end of each owner task.
        async fn drained(&self) {
            assert_eq!(self.tasks.drain(LIMIT).await, Drained::Clean);
        }
    }

    /// Waits for `work`, for [`LIMIT`] at most. A step that does not end
    /// then fails its test and does not hold the test program.
    async fn within<F: Future>(work: F) -> F::Output {
        tokio::time::timeout(LIMIT, work)
            .await
            .unwrap_or_else(|_| panic!("a step did not end in {} seconds", LIMIT.as_secs()))
    }

    /// Moves the clock of `tokio` forward by `time` and then lets it run
    /// again. Each timer of the code under test that ends in that time is
    /// then past its end.
    ///
    /// Call it on a runtime with one thread, at a moment when the test
    /// itself waits on no timer: [`within`] also ends at a jump of the
    /// clock.
    async fn skip(time: Duration) {
        tokio::time::pause();
        tokio::time::advance(time).await;
        tokio::time::resume();
    }

    /// Waits for a whole line in the file `path` and gives it with no
    /// newline.
    async fn line_of(path: &Path) -> String {
        let read = async {
            loop {
                if let Ok(text) = fs::read_to_string(path)
                    && let Some(line) = text.strip_suffix('\n')
                {
                    return line.to_owned();
                }

                tokio::time::sleep(TICK).await;
            }
        };

        tokio::time::timeout(LIMIT, read)
            .await
            .unwrap_or_else(|_| panic!("no line in {}", path.display()))
    }

    /// Waits for the process id that a program wrote to the file `path`.
    async fn pid_of(path: PathBuf) -> u32 {
        line_of(&path).await.parse().unwrap()
    }

    /// Polls the run `run` until its program wrote a process id to the file
    /// `name` of the bench. The run must not end in that time.
    async fn id_in<F>(run: &mut Pin<&mut F>, bench: &Bench, name: &str) -> u32
    where
        F: Future<Output = Result<Finished, RunError>>,
    {
        tokio::select! {
            biased;

            result = run => panic!("the run ended: {result:?}"),
            pid = pid_of(bench.file(name)) => pid,
        }
    }

    /// One column of `ps` for the process `pid`. `None` when no such
    /// process is there.
    fn ps(pid: u32, column: &str) -> Option<String> {
        let listed = std::process::Command::new("ps")
            .args(["-o", column, "-p", &pid.to_string()])
            .output()
            .unwrap();
        let text = String::from_utf8_lossy(&listed.stdout).trim().to_owned();

        (!text.is_empty()).then_some(text)
    }

    /// Whether the process `pid` is a child of this test program. A child
    /// that ended is a child until some code reads its end.
    ///
    /// The check reads the parent and not only the id: the operating system
    /// can give the id of a child that is gone to a new process.
    fn is_child(pid: u32) -> bool {
        ps(pid, "ppid=") == Some(std::process::id().to_string())
    }

    /// Whether some code of this process read the end of the child `pid`.
    /// The operating system then holds no child with that id for this
    /// process, and a wait call for the id fails with "no child processes".
    ///
    /// The call does not wait and starts no program, as [`is_child`] does.
    /// A test thus reads the state at the moment that a run returned. A
    /// child that the drop of its owner killed is still there at that
    /// moment.
    fn end_was_read(pid: u32) -> bool {
        let id = Pid::from_raw(i32::try_from(pid).unwrap());

        matches!(waitpid(id, WaitOptions::NOHANG), Err(Errno::CHILD))
    }

    /// Waits until `ps` shows no child of this test program with the id
    /// `pid`: the child ended, and its owner read its end.
    async fn gone(pid: u32) {
        let left = async {
            while is_child(pid) {
                tokio::time::sleep(TICK).await;
            }
        };

        assert!(
            tokio::time::timeout(LIMIT, left).await.is_ok(),
            "the child {pid} is still there"
        );
    }

    /// Whether the process `pid` runs: `ps` shows it, and not as a process
    /// that ended and waits for its parent.
    fn runs(pid: u32) -> bool {
        ps(pid, "state=").is_some_and(|state| !state.starts_with('Z'))
    }

    /// Sends SIGKILL to the process `pid`, which is no child of the test.
    fn kill_stray(pid: u32) {
        let killed = std::process::Command::new("kill")
            .args(["-9", &pid.to_string()])
            .status()
            .unwrap();

        assert!(killed.success(), "the process {pid} was not there");
    }

    /// The lines of the standard output of a child, in order of their text.
    fn sorted_lines(finished: &Finished) -> Vec<String> {
        let mut lines: Vec<String> = std::str::from_utf8(finished.stdout())
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect();
        lines.sort();

        lines
    }

    /// The standard output of a child, as text.
    fn output_of(finished: &Finished) -> &str {
        std::str::from_utf8(finished.stdout()).unwrap()
    }

    /// Each variable of this process that a child must get: the name and
    /// the value are one line of text, as `env` prints it.
    ///
    /// macOS removes each `DYLD_` variable at the start of a program of the
    /// system, and `env` is such a program.
    fn own_variables() -> Vec<(String, String)> {
        std::env::vars_os()
            .filter_map(|(name, value)| Some((name.into_string().ok()?, value.into_string().ok()?)))
            .filter(|(name, value)| !name.contains('\n') && !value.contains('\n'))
            .filter(|(name, _)| !name.starts_with("DYLD_"))
            .collect()
    }

    #[test]
    fn the_python_return_code_is_the_status_or_the_negative_signal() {
        let table = [
            (Ended::Code(0), 0),
            (Ended::Code(1), 1),
            (Ended::Code(255), 255),
            (Ended::Signal(KILLED), -9),
            (Ended::Signal(TERMINATED), -15),
            (Ended::Signal(i32::MAX), -i32::MAX),
            (Ended::Signal(i32::MIN), i32::MAX),
        ];

        for (ended, code) in table {
            assert_eq!(ended.python_returncode(), code, "{ended:?}");
        }
    }

    #[test]
    fn a_wait_status_gives_the_exit_status_or_the_signal() {
        let table = [
            ((Some(0), None), Ended::Code(0)),
            ((Some(3), None), Ended::Code(3)),
            ((Some(255), None), Ended::Code(255)),
            ((None, Some(KILLED)), Ended::Signal(KILLED)),
            ((None, Some(TERMINATED)), Ended::Signal(TERMINATED)),
            // The operating system gives none of the next four.
            ((Some(256), None), Ended::Code(STATUS_UNKNOWN)),
            ((Some(-1), None), Ended::Code(STATUS_UNKNOWN)),
            ((Some(3), Some(KILLED)), Ended::Code(3)),
            ((None, None), Ended::Code(STATUS_UNKNOWN)),
        ];

        for ((code, signal), ended) in table {
            assert_eq!(Ended::from_status(code, signal), ended);
        }

        assert_ne!(Ended::Code(STATUS_UNKNOWN).python_returncode(), 0);
    }

    #[test]
    fn the_status_of_the_operating_system_gives_its_end() {
        // The wait status of a child: the exit status in the second byte,
        // or the number of the signal in the first byte.
        let exited = ExitStatus::from_raw(3 << 8);
        let killed = ExitStatus::from_raw(KILLED);

        assert_eq!(Ended::of(exited), Ended::Code(3));
        assert_eq!(Ended::of(killed), Ended::Signal(KILLED));
    }

    #[test]
    fn a_child_that_ran_to_its_end_gives_its_status_and_its_streams() {
        let exited = Ok(ExitStatus::from_raw(3 << 8));
        let finished = whole("tool", &exited, b"out".to_vec(), b"err".to_vec());

        assert_eq!(
            finished,
            Ok(Finished::new(Ended::Code(3))
                .with_stdout("out")
                .with_stderr("err"))
        );
    }

    #[test]
    fn each_cut_of_an_exchange_has_its_error() {
        let cap = ByteCap::new(7).unwrap();

        assert_eq!(Cut::PastCap(cap).error(), RunError::OutputTooLarge { cap });
        assert_eq!(Cut::Failed.error(), RunError::OwnerLost);
    }

    /// Each row: the bytes, the text under `Strict`, the text under
    /// `Replace`. `None` for bytes that `Strict` refuses. CPython 3.13 gave
    /// each text, with `decode("utf-8", errors)` and then the two `replace`
    /// calls of `subprocess.py:1098-1100`.
    const TEXTS: [(&[u8], Option<&str>, &str); 35] = [
        (b"", Some(""), ""),
        (b"plain\n", Some("plain\n"), "plain\n"),
        // The content of a token file: `token` reads it with this function.
        (b"token", Some("token"), "token"),
        (b"token\r\n", Some("token\n"), "token\n"),
        (b"a\nb", Some("a\nb"), "a\nb"),
        (b"a\rb", Some("a\nb"), "a\nb"),
        (b"a\r\nb", Some("a\nb"), "a\nb"),
        (b"a\r\r\nb", Some("a\n\nb"), "a\n\nb"),
        (b"a\n\rb", Some("a\n\nb"), "a\n\nb"),
        (b"one\r\ntwo", Some("one\ntwo"), "one\ntwo"),
        (b"one\rtwo", Some("one\ntwo"), "one\ntwo"),
        (b"\r", Some("\n"), "\n"),
        (b"\r\n", Some("\n"), "\n"),
        (b"\n\r", Some("\n\n"), "\n\n"),
        (b"\r\r\n", Some("\n\n"), "\n\n"),
        (b"\r\n\r\n", Some("\n\n"), "\n\n"),
        (b"\r\n\n\r", Some("\n\n\n"), "\n\n\n"),
        (b"a\rb\r\nc\nd\r", Some("a\nb\nc\nd\n"), "a\nb\nc\nd\n"),
        (b"\0", Some("\0"), "\0"),
        // No other line break changes.
        (
            b"\x0b\x0c\x1c",
            Some("\u{b}\u{c}\u{1c}"),
            "\u{b}\u{c}\u{1c}",
        ),
        (b"\xc2\x85", Some("\u{85}"), "\u{85}"),
        (
            b"\xe2\x80\xa8\xe2\x80\xa9",
            Some("\u{2028}\u{2029}"),
            "\u{2028}\u{2029}",
        ),
        (
            b"caf\xc3\xa9 \xe2\x82\xac \xf0\x9f\x98\x80\r\n",
            Some("caf\u{e9} \u{20ac} \u{1f600}\n"),
            "caf\u{e9} \u{20ac} \u{1f600}\n",
        ),
        // A byte order mark stays.
        (b"\xef\xbb\xbfbom", Some("\u{feff}bom"), "\u{feff}bom"),
        (b"caf\xe9", None, "caf\u{fffd}"),
        (b"\xff\r\n", None, "\u{fffd}\n"),
        (b"\x80\x80", None, "\u{fffd}\u{fffd}"),
        // The start of a character with no end is one U+FFFD.
        (b"\xf0\x9f\x98", None, "\u{fffd}"),
        (b"a\xe2\x82b", None, "a\u{fffd}b"),
        (b"\xe2\x28\xa1", None, "\u{fffd}(\u{fffd}"),
        // A surrogate, a long form and a number past U+10FFFF: one U+FFFD
        // for each byte.
        (b"\xed\xa0\x80", None, "\u{fffd}\u{fffd}\u{fffd}"),
        (b"\xc0\xaf", None, "\u{fffd}\u{fffd}"),
        (b"\xe0\x80\xaf", None, "\u{fffd}\u{fffd}\u{fffd}"),
        (
            b"\xf4\x90\x80\x80",
            None,
            "\u{fffd}\u{fffd}\u{fffd}\u{fffd}",
        ),
        (
            b"\xf8\x88\x80\x80\x80\r",
            None,
            "\u{fffd}\u{fffd}\u{fffd}\u{fffd}\u{fffd}\n",
        ),
    ];

    #[test]
    fn the_text_of_an_output_is_the_text_that_python_gives() {
        for (bytes, strict, replaced) in TEXTS {
            assert_eq!(
                python_text(bytes, Decode::Strict).ok().as_deref(),
                strict,
                "{bytes:?}"
            );
            assert_eq!(
                python_text(bytes, Decode::Replace).as_deref(),
                Ok(replaced),
                "{bytes:?}"
            );
        }
    }

    #[test]
    fn a_strict_decode_says_where_the_bytes_are_not_text() {
        let error = python_text(b"abc\xff", Decode::Strict).unwrap_err();

        assert_eq!(error.valid_up_to(), 3);
    }

    /// `text=True` of Python reads the encoding of the locale
    /// (`subprocess.py:367-384`, version 3.13). `python_text` reads UTF-8,
    /// and no variable of the locale is an input of the function.
    #[test]
    fn the_text_of_an_output_is_utf_8_in_each_locale() {
        // The two bytes of U+00E9 in UTF-8. A Python process in a Latin-1
        // locale reads them as two characters.
        assert_eq!(
            python_text(b"caf\xc3\xa9", Decode::Strict).as_deref(),
            Ok("caf\u{e9}")
        );
        // The one byte of U+00E9 in Latin-1. A Python process in a Latin-1
        // locale reads it, and this function refuses it.
        assert!(python_text(b"caf\xe9", Decode::Strict).is_err());
    }

    /// A stream of a child that gives some bytes and then an error of the
    /// operating system.
    struct BrokenStream {
        /// The bytes before the error. Empty after the first read.
        first: &'static [u8],
    }

    impl AsyncRead for BrokenStream {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _context: &mut Context<'_>,
            buffer: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            if self.first.is_empty() {
                return Poll::Ready(Err(io::Error::from_raw_os_error(IO_ERROR)));
            }

            buffer.put_slice(self.first);
            self.first = &[];

            Poll::Ready(Ok(()))
        }
    }

    /// The standard input of a child that takes no byte: each write gives
    /// the error of the operating system with this number.
    struct RefusingPipe {
        errno: i32,
    }

    impl AsyncWrite for RefusingPipe {
        fn poll_write(
            self: Pin<&mut Self>,
            _context: &mut Context<'_>,
            _bytes: &[u8],
        ) -> Poll<io::Result<usize>> {
            Poll::Ready(Err(io::Error::from_raw_os_error(self.errno)))
        }

        fn poll_flush(self: Pin<&mut Self>, _context: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }

        fn poll_shutdown(self: Pin<&mut Self>, _context: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    /// Reads the stream `bytes` with the cap `cap`.
    fn captured(bytes: &[u8], cap: ByteCap) -> Result<Vec<u8>, Cut> {
        let mut stream = (bytes, cap);

        block_on(capture("tool", Some(&mut stream)))
    }

    #[test]
    fn a_stream_is_whole_up_to_its_cap_and_refused_one_byte_past_it() {
        let cap = ByteCap::new(5).unwrap();

        for (length, whole) in [(0, true), (4, true), (5, true), (6, false), (5000, false)] {
            let bytes = vec![b'x'; length];
            let expected = if whole {
                Ok(bytes.clone())
            } else {
                Err(Cut::PastCap(cap))
            };

            assert_eq!(captured(&bytes, cap), expected, "{length}");
        }
    }

    #[test]
    fn the_largest_cap_reads_a_stream_and_does_not_overflow() {
        // One byte past this cap is no count of bytes. The reader must not
        // add 1 to the cap.
        let cap = ByteCap::new(usize::MAX).unwrap();

        assert_eq!(captured(b"each byte", cap), Ok(b"each byte".to_vec()));
    }

    #[test]
    fn an_absent_stream_gives_no_byte() {
        let read = block_on(capture::<&[u8]>("tool", None));

        assert_eq!(read, Ok(Vec::new()));
    }

    #[test]
    fn an_absent_input_and_an_input_that_the_child_takes_are_no_cut() {
        // `within`: a write that does not end fails the test and does not
        // hold the test program.
        runtime().block_on(async {
            let absent = within(feed::<Vec<u8>>("tool", None, b"input")).await;
            let taken = within(feed("tool", Some(Vec::new()), b"input")).await;

            assert_eq!(absent, Ok(()));
            assert_eq!(taken, Ok(()));
        });
    }

    /// An exchange that never ends and never waits. Each turn takes one step
    /// of the budget that `tokio` gives its task for one poll, as a read
    /// from a stream that always holds bytes does.
    async fn busy() -> Result<Streams, Cut> {
        loop {
            tokio::task::consume_budget().await;
        }
    }

    #[test]
    fn an_exchange_that_never_waits_does_not_hold_back_the_time_limit() {
        runtime().block_on(async {
            let event = first_event(
                busy(),
                future::pending(),
                past_limit(TimeLimit::After(TICK)),
            );
            let verdict = tokio::time::timeout(STARVED, event).await;

            assert!(matches!(verdict, Ok(Verdict::PastLimit(after)) if after == TICK));
        });
    }

    #[test]
    fn an_exchange_that_never_waits_does_not_hold_back_the_stop_signal() {
        runtime().block_on(async {
            let (trigger, shutdown) = shutdown_pair();
            let caller = CancellationToken::new();
            let stop = tokio::spawn(async move {
                tokio::time::sleep(TICK).await;
                trigger.trigger();
            });
            let event = first_event(
                busy(),
                stop_asked(AtShutdown::Kill, &shutdown, &caller),
                future::pending(),
            );
            let verdict = tokio::time::timeout(STARVED, event).await;

            assert!(matches!(verdict, Ok(Verdict::Stop)));
            stop.await.unwrap();
        });
    }

    #[test]
    fn an_exchange_that_ended_is_first_when_each_event_is_there() {
        runtime().block_on(async {
            let done = async { Ok((b"out".to_vec(), Vec::new(), Ok(ExitStatus::from_raw(0)))) };
            let verdict = first_event(done, future::ready(()), future::ready(TICK)).await;

            assert!(matches!(
                verdict,
                Verdict::Done(Ok((stdout, _, Ok(status)))) if stdout == b"out" && status.success()
            ));

            // The event that kills is before the time limit.
            let verdict = first_event(busy(), future::ready(()), future::ready(TICK)).await;

            assert!(matches!(verdict, Verdict::Stop));
        });
    }

    #[test]
    fn the_wait_for_a_kill_event_never_ends_under_finish() {
        runtime().block_on(async {
            let (trigger, shutdown) = shutdown_pair();
            let caller = CancellationToken::new();

            trigger.trigger();
            caller.cancel();

            let asked = stop_asked(AtShutdown::Finish, &shutdown, &caller);

            assert!(tokio::time::timeout(GRACE, asked).await.is_err());

            // Under `Kill`, each of the two events ends the wait.
            let (_trigger, running) = shutdown_pair();
            let here = CancellationToken::new();

            within(stop_asked(AtShutdown::Kill, &shutdown, &here)).await;
            within(stop_asked(AtShutdown::Kill, &running, &caller)).await;

            let asked = stop_asked(AtShutdown::Kill, &running, &here);

            assert!(tokio::time::timeout(GRACE, asked).await.is_err());
        });
    }

    /// A child for [`kill`] whose end the test controls.
    struct FakeChild {
        /// The number of the error that the kill call gives. `None` for a
        /// kill that the operating system takes.
        refused: Option<i32>,
        /// The count of the kill calls.
        kills: usize,
        /// The wait ends when the test cancels this token.
        ended: CancellationToken,
    }

    impl FakeChild {
        fn new(refused: Option<i32>) -> Self {
            Self {
                refused,
                kills: 0,
                ended: CancellationToken::new(),
            }
        }
    }

    impl Ending for FakeChild {
        fn kill_now(&mut self) -> io::Result<()> {
            self.kills += 1;

            match self.refused {
                Some(errno) => Err(io::Error::from_raw_os_error(errno)),
                None => Ok(()),
            }
        }

        fn end(&mut self) -> impl Future<Output = io::Result<ExitStatus>> + Send {
            let ended = self.ended.clone();

            async move {
                ended.cancelled().await;

                Ok(ExitStatus::from_raw(KILLED))
            }
        }
    }

    /// A stream of a child that another program holds open: no byte comes,
    /// and no end.
    struct HeldStream;

    impl AsyncRead for HeldStream {
        fn poll_read(
            self: Pin<&mut Self>,
            _context: &mut Context<'_>,
            _buffer: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            Poll::Pending
        }
    }

    /// Kills `child`, which has no stream, and holds that the owner waits
    /// for its end: the kill returns only after the end came.
    async fn killed_and_waited_for(child: &mut FakeChild) {
        let ended = child.ended.clone();
        let mut killing = pin!(kill::<_, &[u8], &[u8]>("tool", child, None, None));

        // The kill call came, and the end did not: the owner waits.
        assert!(tokio::time::timeout(GRACE, &mut killing).await.is_err());

        ended.cancel();
        within(&mut killing).await;
    }

    #[test]
    fn the_owner_waits_for_the_end_of_a_child_that_it_killed() {
        runtime().block_on(async {
            let mut child = FakeChild::new(None);

            killed_and_waited_for(&mut child).await;

            assert_eq!(child.kills, 1);
        });
    }

    #[test]
    fn the_owner_reads_a_held_stream_for_one_second_after_the_end_and_no_more() {
        runtime().block_on(async {
            let cap = ByteCap::new(8).unwrap();
            let mut held = (HeldStream, cap);
            let mut at_its_end = (b"rest".as_slice(), cap);
            let mut child = FakeChild::new(None);
            child.ended.cancel();

            let mut killing = pin!(kill(
                "tool",
                &mut child,
                Some(&mut held),
                Some(&mut at_its_end)
            ));

            // The child ended, and one stream is still open: the owner reads.
            assert!(tokio::time::timeout(GRACE, &mut killing).await.is_err());

            skip(AFTER_KILL).await;
            within(&mut killing).await;
        });
    }

    #[test]
    fn a_runner_and_its_future_go_to_another_thread() {
        fn shareable<T: Send + Sync + Clone>() {}
        fn movable<T: Send>() {}

        shareable::<TokioRunner>();
        movable::<ChildGuard>();
        movable::<Piped>();

        let bench = Bench::new();
        let command = Command::new("true", limit(), AtShutdown::Kill);

        // The call starts nothing: no code polls the future.
        drop(sendable(bench.runner.run(command)));
    }

    #[test]
    fn the_child_gets_each_word_as_it_is() {
        let bench = Bench::new();
        let program = bench.program(
            "words",
            "for word in \"$@\"; do printf '<%s>\\n' \"$word\"; done\n",
        );
        let words = [
            "a b",
            "",
            "--flag=x y",
            "$HOME",
            "'quoted'",
            "caf\u{e9}",
            "*",
            "two\nlines",
            "; echo no",
            "-n",
            "\\",
            "`id`",
        ];
        let command = Command::new(program, limit(), AtShutdown::Kill).args(words);
        let finished = bench.run(command).unwrap();
        let expected: String = words.iter().map(|word| format!("<{word}>\n")).collect();

        assert_eq!(finished.ended(), Ended::Code(0));
        assert_eq!(output_of(&finished), expected);
        assert_eq!(finished.stderr(), b"");
    }

    #[test]
    fn a_run_gives_the_two_streams_and_the_exit_status() {
        let bench = Bench::new();
        let command = bench.command(
            "echo to-output\necho to-error >&2\nexit 3\n",
            limit(),
            AtShutdown::Kill,
        );
        let finished = bench.run(command).unwrap();

        assert_eq!(finished.ended(), Ended::Code(3));
        assert_eq!(finished.ended().python_returncode(), 3);
        assert_eq!(finished.stdout(), b"to-output\n");
        assert_eq!(finished.stderr(), b"to-error\n");
    }

    #[test]
    fn a_child_that_a_signal_ends_gives_the_signal() {
        let bench = Bench::new();
        let command = bench.command("kill -TERM $$\nsleep 30\n", limit(), AtShutdown::Kill);
        let finished = bench.run(command).unwrap();

        assert_eq!(finished.ended(), Ended::Signal(TERMINATED));
        assert_eq!(finished.ended().python_returncode(), -15);
    }

    #[test]
    fn the_child_runs_in_the_working_directory_of_the_command() {
        let bench = Bench::new();
        let command = bench
            .command("pwd -P\n", limit(), AtShutdown::Kill)
            .cwd(bench.root.path());
        let finished = bench.run(command).unwrap();
        let expected = format!(
            "{}\n",
            fs::canonicalize(bench.root.path()).unwrap().display()
        );

        assert_eq!(output_of(&finished), expected);
    }

    #[test]
    fn inherit_gives_the_child_each_variable_of_this_process() {
        let bench = Bench::new();
        let command = Command::new(ENV, limit(), AtShutdown::Kill).env(EnvPolicy::Inherit);
        let lines = sorted_lines(&bench.run(command).unwrap());
        let own = own_variables();

        assert!(own.iter().any(|(name, _)| name == "PATH"));

        for (name, value) in own {
            assert!(lines.contains(&format!("{name}={value}")), "{name}");
        }
    }

    #[test]
    fn inherit_and_adds_its_pairs_and_the_last_pair_of_a_name_stays() {
        let bench = Bench::new();
        let pairs = [
            ("CRECHE_TEST_ADDED", "first"),
            ("PATH", "/replaced"),
            ("CRECHE_TEST_ADDED", "two words"),
        ]
        .map(|(name, value)| (name.to_owned(), value.to_owned()));
        let command =
            Command::new(ENV, limit(), AtShutdown::Kill).env(EnvPolicy::InheritAnd(pairs.to_vec()));
        let lines = sorted_lines(&bench.run(command).unwrap());

        assert!(lines.contains(&String::from("CRECHE_TEST_ADDED=two words")));
        assert!(lines.contains(&String::from("PATH=/replaced")));
        assert!(!lines.contains(&String::from("CRECHE_TEST_ADDED=first")));

        let kept: Vec<(String, String)> = own_variables()
            .into_iter()
            .filter(|(name, _)| name != "PATH")
            .collect();

        assert!(!kept.is_empty());

        for (name, value) in kept {
            assert!(lines.contains(&format!("{name}={value}")), "{name}");
        }
    }

    #[test]
    fn inherit_only_gives_the_named_variables_and_no_other() {
        let bench = Bench::new();
        let names = ["PATH", "CRECHE_TEST_NOT_SET"].map(String::from).to_vec();
        let command =
            Command::new(ENV, limit(), AtShutdown::Kill).env(EnvPolicy::InheritOnly(names));
        let lines = sorted_lines(&bench.run(command).unwrap());

        assert_eq!(lines, [format!("PATH={}", std::env::var("PATH").unwrap())]);
    }

    #[test]
    fn exactly_gives_its_pairs_and_no_other() {
        let bench = Bench::new();
        let pairs = [("B", "two words"), ("A", "1"), ("EMPTY", "")]
            .map(|(name, value)| (name.to_owned(), value.to_owned()))
            .to_vec();
        let some = Command::new(ENV, limit(), AtShutdown::Kill).env(EnvPolicy::Exactly(pairs));
        let none = Command::new(ENV, limit(), AtShutdown::Kill).env(EnvPolicy::Exactly(Vec::new()));

        assert_eq!(
            sorted_lines(&bench.run(some).unwrap()),
            ["A=1", "B=two words", "EMPTY="]
        );
        assert_eq!(bench.run(none).unwrap().stdout(), b"");
    }

    #[test]
    fn the_child_finds_a_program_on_the_path_of_its_own_environment() {
        let bench = Bench::new();
        bench.program("creche-test-tool", "echo found\n");

        // Python looks for the program on the `PATH` of the environment
        // that the call gives, and so does this runner.
        let path = vec![(String::from("PATH"), bench.dir())];
        let command = Command::new("creche-test-tool", limit(), AtShutdown::Kill)
            .env(EnvPolicy::Exactly(path));

        assert_eq!(bench.run(command).unwrap().stdout(), b"found\n");
    }

    #[test]
    fn a_name_of_the_environment_with_an_equal_sign_starts_no_program() {
        let bench = Bench::new();
        let program = bench.program("program", "touch \"$1/started\"\n");
        let pair = vec![(String::from("A=B"), String::from(SECRET_VALUE))];

        for policy in [
            EnvPolicy::InheritAnd(pair.clone()),
            EnvPolicy::Exactly(pair),
        ] {
            let command = Command::new(&program, limit(), AtShutdown::Kill)
                .arg(bench.dir())
                .env(policy);

            // The text is the text of the `ValueError` of Python.
            assert_eq!(
                bench.run(command),
                Err(RunError::NotStarted {
                    os_text: String::from("illegal environment variable name"),
                })
            );
            assert!(!bench.file("started").exists());
        }
    }

    #[test]
    fn a_program_that_does_not_start_is_a_result_with_the_text_of_the_system() {
        let bench = Bench::new();
        let plain = bench.file("plain");
        fs::write(&plain, b"#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(&plain, fs::Permissions::from_mode(0o600)).unwrap();

        let table = [
            (bench.file("no-such-program"), "No such file or directory"),
            (plain, "Permission denied"),
            (bench.root.path().to_owned(), "Permission denied"),
        ];

        for (program, text) in table {
            let command = Command::new(program.to_str().unwrap(), limit(), AtShutdown::Kill);

            assert_eq!(
                bench.run(command),
                Err(RunError::NotStarted {
                    os_text: String::from(text),
                }),
                "{}",
                program.display()
            );
        }

        // A name with no `/`: the child looks on its own `PATH`, which holds
        // only the directory of the test.
        let path = vec![(String::from("PATH"), bench.dir())];
        let no_name = Command::new("creche-test-no-such-program", limit(), AtShutdown::Finish)
            .env(EnvPolicy::Exactly(path));

        assert_eq!(
            bench.run(no_name),
            Err(RunError::NotStarted {
                os_text: String::from("No such file or directory"),
            })
        );
    }

    /// Writes `text` as the file `name` of the bench, with mode `0700` and
    /// with no `#!` line.
    ///
    /// A shell writes the file, as `write_program` does. This process never
    /// holds the file open for a write, so no start of the file fails with
    /// "Text file busy".
    fn plain_program(bench: &Bench, name: &str, text: &str) -> PathBuf {
        let path = bench.file(name);
        let written = std::process::Command::new(SHELL)
            .args([
                "-c",
                "printf '%s' \"$2\" > \"$1\" && chmod 700 \"$1\"",
                SHELL,
            ])
            .arg(&path)
            .arg(text)
            .status()
            .unwrap();

        assert!(written.success(), "no file {}", path.display());

        path
    }

    /// CPython refuses a program file with no `#!` line that is no binary
    /// program: "Exec format error" (`subprocess.py:1912-1921`, version
    /// 3.13). The runner does not refuse each such file.
    #[test]
    fn a_program_file_with_no_script_line_goes_to_the_shell_by_its_name() {
        let bench = Bench::new();
        let file = plain_program(&bench, "creche-test-plain", "echo \"ran $1\"\n");

        // A name with no `/`, and an environment that gives the `PATH`: the
        // standard library calls `execvp`, which gives the file to the
        // shell. The words of the command stay the words of the program.
        let path = vec![(String::from("PATH"), bench.dir())];
        let by_name = Command::new("creche-test-plain", limit(), AtShutdown::Kill)
            .arg("two words")
            .env(EnvPolicy::Exactly(path));
        let finished = bench.run(by_name).unwrap();

        assert_eq!(finished.ended(), Ended::Code(0));
        assert_eq!(finished.stdout(), b"ran two words\n");

        // A path with a `/`: Linux refuses the file, as CPython does. macOS
        // gives it to the shell.
        let by_path = Command::new(file.to_str().unwrap(), limit(), AtShutdown::Kill).arg("whole");
        let result = bench.run(by_path);

        if cfg!(target_os = "linux") {
            assert_eq!(
                result,
                Err(RunError::NotStarted {
                    os_text: String::from("Exec format error"),
                })
            );
        } else {
            assert_eq!(result.unwrap().stdout(), b"ran whole\n");
        }
    }

    #[test]
    fn a_word_with_a_nul_byte_and_an_absent_directory_start_no_program() {
        let bench = Bench::new();
        let with_nul = bench
            .command("touch \"$1/started\"\n", limit(), AtShutdown::Kill)
            .arg("a\0b");
        let no_dir = Command::new(SHELL, limit(), AtShutdown::Kill).cwd(bench.file("no-such-dir"));

        assert!(matches!(
            bench.run(with_nul),
            Err(RunError::NotStarted { .. })
        ));
        assert!(!bench.file("started").exists());
        assert_eq!(
            bench.run(no_dir),
            Err(RunError::NotStarted {
                os_text: String::from("No such file or directory"),
            })
        );
    }

    /// CPython closes each descriptor past 2 in the child
    /// (`subprocess.py:819`, version 3.13). The Rust child keeps a
    /// descriptor that has no close-on-exec flag.
    #[test]
    fn the_child_gets_a_descriptor_only_when_it_has_no_close_on_exec_flag() {
        let bench = Bench::new();
        // The standard library opens each file with the flag.
        let flagged = fs::File::open(bench.root.path()).unwrap();
        let plain = fs::File::open(bench.root.path()).unwrap();
        fcntl_setfd(&plain, FdFlags::empty()).unwrap();

        // The shell reads the program from a word, so it opens no file of
        // its own.
        let listed = "for fd in \"$@\"; do \
                      if [ -e \"/dev/fd/$fd\" ]; then echo open; else echo closed; fi; \
                      done";
        let command = Command::new(SHELL, limit(), AtShutdown::Kill).args([
            String::from("-c"),
            String::from(listed),
            String::from(SHELL),
            flagged.as_raw_fd().to_string(),
            plain.as_raw_fd().to_string(),
        ]);
        let finished = bench.run(command).unwrap();

        assert_eq!(output_of(&finished), "closed\nopen\n");
    }

    #[test]
    fn the_child_reads_the_bytes_of_its_input_and_then_the_end() {
        let bench = Bench::new();
        // More than a pipe holds: the owner writes and reads at one time.
        let large: Vec<u8> = (0..300_000_u32)
            .map(|n| b'a' + u8::try_from(n % 26).unwrap())
            .collect();

        for input in [b"one line\n".to_vec(), Vec::new(), large] {
            let command =
                Command::new("cat", limit(), AtShutdown::Kill).stdin(Stdin::Bytes(input.clone()));
            let finished = bench.run(command).unwrap();

            assert_eq!(finished.ended(), Ended::Code(0));
            assert_eq!(finished.stdout(), input);
        }
    }

    #[test]
    fn an_empty_input_is_the_end_at_once() {
        let bench = Bench::new();
        let command = Command::new("cat", limit(), AtShutdown::Kill).stdin(Stdin::Null);
        let finished = bench.run(command).unwrap();

        assert_eq!(finished.ended(), Ended::Code(0));
        assert_eq!(finished.stdout(), b"");
    }

    #[test]
    fn a_child_that_reads_no_input_is_no_error() {
        let bench = Bench::new();
        // The child ends before the owner wrote each byte: the write fails.
        let command = bench
            .command("echo done\nexit 4\n", limit(), AtShutdown::Kill)
            .stdin(Stdin::Bytes(vec![b'x'; 1 << 20]));
        let finished = bench.run(command).unwrap();

        assert_eq!(finished.ended(), Ended::Code(4));
        assert_eq!(finished.stdout(), b"done\n");
    }

    #[test]
    fn the_owner_reads_the_two_streams_at_one_time() {
        let bench = Bench::new();
        // Each stream holds more than a pipe holds. An owner that reads one
        // stream to its end first never gets the second.
        let command = bench.command(
            "head -c 200000 /dev/zero >&2\nhead -c 200000 /dev/zero\n",
            limit(),
            AtShutdown::Kill,
        );
        let finished = bench.run(command).unwrap();

        assert_eq!(finished.ended(), Ended::Code(0));
        assert_eq!(finished.stdout().len(), 200_000);
        assert_eq!(finished.stderr().len(), 200_000);
    }

    #[test]
    fn inherited_streams_give_the_status_and_no_byte() {
        let bench = Bench::new();
        let command = Command::new("true", TimeLimit::None, AtShutdown::Finish)
            .stdin(Stdin::Inherit)
            .output(Output::Inherit);

        assert_eq!(bench.run(command), Ok(Finished::new(Ended::Code(0))));
    }

    #[test]
    fn a_stream_of_exactly_the_cap_is_whole() {
        let bench = Bench::new();
        let cap = ByteCap::new(1000).unwrap();
        let command = Command::new(bench.program("writer", WRITER), limit(), AtShutdown::Kill)
            .args(["1000", "1000"])
            .output(Output::Capture { cap });
        let finished = bench.run(command).unwrap();

        assert_eq!(finished.ended(), Ended::Code(0));
        assert_eq!(finished.stdout(), vec![0_u8; 1000]);
        assert_eq!(finished.stderr(), vec![0_u8; 1000]);
    }

    #[test]
    fn one_byte_past_the_cap_on_a_stream_gives_output_too_large() {
        let bench = Bench::new();
        let cap = ByteCap::new(1000).unwrap();
        let program = bench.program("writer", WRITER);

        for sizes in [["1001", "0"], ["0", "1001"], ["1001", "1001"]] {
            let command = Command::new(&program, limit(), AtShutdown::Kill)
                .args(sizes)
                .output(Output::Capture { cap });

            assert_eq!(
                bench.run(command),
                Err(RunError::OutputTooLarge { cap }),
                "{sizes:?}"
            );
        }
    }

    /// `subprocess.run` of Python reads the output of a child with no cap
    /// (`caregiver/src/caregiver/driver.py:87-89`). A new command has a cap
    /// of 1 MiB for each stream.
    #[test]
    fn a_new_command_reads_one_mib_of_a_stream_and_no_more() {
        let bench = Bench::new();
        let program = bench.program("writer", WRITER);
        let whole = Command::new(&program, limit(), AtShutdown::Kill).args(["1048576", "0"]);
        let past = Command::new(&program, limit(), AtShutdown::Kill).args(["1048577", "0"]);

        assert_eq!(bench.run(whole).unwrap().stdout().len(), 1_048_576);
        assert_eq!(
            bench.run(past),
            Err(RunError::OutputTooLarge {
                cap: ByteCap::ONE_MIB
            })
        );
    }

    #[test]
    fn a_child_that_writes_with_no_end_is_killed_at_the_cap() {
        let bench = Bench::new();
        let cap = ByteCap::new(4096).unwrap();
        let command = bench
            .command(
                "echo $$ > \"$1/pid\"\nexec yes\n",
                limit(),
                AtShutdown::Finish,
            )
            .output(Output::Capture { cap });

        runtime().block_on(async {
            let result = within(bench.runner.run(command)).await;

            assert_eq!(result, Err(RunError::OutputTooLarge { cap }));

            let pid = pid_of(bench.file("pid")).await;

            // The owner read the end of the child before it gave the error.
            assert!(end_was_read(pid));
            assert!(!is_child(pid));

            bench.drained().await;
        });
    }

    #[test]
    fn the_owner_kills_a_child_at_its_time_limit_and_no_process_is_left() {
        let bench = Bench::new();
        // The kill is SIGKILL: it ends a child that ignores SIGTERM.
        let command = bench.command(DEAF_HOLD, TimeLimit::After(HOUR), AtShutdown::Finish);

        runtime().block_on(async {
            let mut run = pin!(bench.runner.run(command));
            let pid = id_in(&mut run, &bench, "pid").await;

            // Before the time limit, the owner lets the child run.
            assert!(tokio::time::timeout(GRACE, &mut run).await.is_err());
            assert!(runs(pid));

            skip(HOUR).await;

            assert_eq!(
                within(&mut run).await,
                Err(RunError::TimedOut { after: HOUR })
            );
            // The owner read the end of the child before it gave the error.
            assert!(end_was_read(pid));
            assert!(!is_child(pid));

            bench.drained().await;
        });
    }

    #[test]
    fn a_time_limit_of_zero_ends_the_run_at_once() {
        let bench = Bench::new();
        let command = Command::new(
            "sleep",
            TimeLimit::After(Duration::ZERO),
            AtShutdown::Finish,
        )
        .arg("300");

        assert_eq!(
            bench.run(command),
            Err(RunError::TimedOut {
                after: Duration::ZERO
            })
        );
    }

    /// At the time limit, CPython kills the child, waits for its end and
    /// reads no stream after it (`subprocess.py:557-570`, version 3.13).
    /// The owner reads each stream for 1 second at most after that end.
    #[test]
    fn a_program_that_holds_a_stream_does_not_hold_the_owner() {
        let bench = Bench::new();
        // The child ends at once. The program that it started keeps the two
        // streams open for longer than the test runs.
        let command = bench.command(
            "sleep 300 &\necho $! > \"$1/stray\"\necho early\n",
            TimeLimit::After(HOUR),
            AtShutdown::Finish,
        );

        runtime().block_on(async {
            let mut run = pin!(bench.runner.run(command));
            let stray = id_in(&mut run, &bench, "stray").await;

            // The child ended, and a stream is still open. The exchange
            // continues to the time limit, as the exchange of CPython does.
            assert!(tokio::time::timeout(GRACE, &mut run).await.is_err());

            let asked = Instant::now();
            skip(HOUR).await;
            let result = within(&mut run).await;

            // The error holds no byte of the output: not the line `early`.
            assert_eq!(result, Err(RunError::TimedOut { after: HOUR }));
            // The owner read the streams for its whole second, and it did
            // not wait for the program that holds them.
            assert!(asked.elapsed() >= AFTER_KILL);
            assert!(runs(stray));

            kill_stray(stray);
            bench.drained().await;
        });
    }

    #[test]
    fn a_dropped_caller_with_kill_leaves_no_process() {
        let bench = Bench::new();
        // No time limit: only the caller that goes away ends the child.
        let command = bench.command(HOLD, TimeLimit::None, AtShutdown::Kill);

        runtime().block_on(async {
            let pid = {
                let mut run = pin!(bench.runner.run(command));
                let pid = id_in(&mut run, &bench, "pid").await;

                assert!(runs(pid));

                pid
                // The future of the run drops here: the caller left.
            };

            bench.drained().await;

            assert!(!is_child(pid));
        });
    }

    #[test]
    fn a_dropped_caller_with_finish_lets_the_child_end_whole() {
        let bench = Bench::new();
        let command = bench.command(GATED, limit(), AtShutdown::Finish);

        runtime().block_on(async {
            let pid = {
                let mut run = pin!(bench.runner.run(command));

                id_in(&mut run, &bench, "pid").await
                // The future of the run drops here: the caller left.
            };

            // The owner lets the child run.
            tokio::time::sleep(GRACE).await;

            assert!(runs(pid));
            assert!(!bench.file("done").exists());

            bench.open_gate();
            bench.drained().await;

            assert_eq!(fs::read(bench.file("done")).unwrap(), b"whole\n");
            assert!(!is_child(pid));
        });
    }

    #[test]
    fn the_stop_signal_with_finish_lets_the_child_end_whole_while_the_drain_waits() {
        let bench = Bench::new();
        let command = bench.command(GATED, limit(), AtShutdown::Finish);

        runtime().block_on(async {
            let caller = tokio::spawn({
                let runner = bench.runner.clone();

                async move { runner.run(command).await }
            });
            let pid = pid_of(bench.file("pid")).await;

            bench.trigger.trigger();

            // The owner is a tracked task: a drain counts it while the child
            // runs.
            assert_eq!(
                bench.tasks.drain(GRACE).await,
                Drained::TimedOut { left: 1 }
            );

            let open_late = async {
                tokio::time::sleep(GRACE).await;

                // The stop signal did not end the child, and the drain still
                // waits for its owner.
                assert!(runs(pid));
                assert!(!bench.file("done").exists());

                bench.open_gate();
            };
            let (drained, ()) = tokio::join!(bench.tasks.drain(LIMIT), open_late);

            assert_eq!(drained, Drained::Clean);
            assert_eq!(fs::read(bench.file("done")).unwrap(), b"whole\n");
            assert!(!is_child(pid));

            let finished = within(caller).await.unwrap().unwrap();

            assert_eq!(finished.ended(), Ended::Code(0));
            assert_eq!(finished.stdout(), b"finished\n");
        });
    }

    #[test]
    fn the_stop_signal_with_kill_gives_stopped_and_leaves_no_process() {
        let bench = Bench::new();
        // No time limit: only the stop signal ends the child. The child
        // ignores SIGTERM, and the kill of the owner is SIGKILL.
        let command = bench.command(DEAF_HOLD, TimeLimit::None, AtShutdown::Kill);

        runtime().block_on(async {
            let mut run = pin!(bench.runner.run(command));
            let pid = id_in(&mut run, &bench, "pid").await;

            assert!(runs(pid));

            bench.trigger.trigger();

            assert_eq!(within(&mut run).await, Err(RunError::Stopped));
            // The owner read the end of the child before it gave the error.
            assert!(end_was_read(pid));
            assert!(!is_child(pid));

            bench.drained().await;
        });
    }

    #[test]
    fn a_killed_child_with_no_pipe_is_gone_when_the_run_returns() {
        let bench = Bench::new();
        // The child has the output streams of this process and writes
        // nothing. No pipe says when the child ended: only the wait of the
        // owner does.
        let command = bench
            .command(DEAF_HOLD, TimeLimit::None, AtShutdown::Kill)
            .output(Output::Inherit);

        runtime().block_on(async {
            let mut run = pin!(bench.runner.run(command));
            let pid = id_in(&mut run, &bench, "pid").await;

            assert!(runs(pid));

            bench.trigger.trigger();

            assert_eq!(within(&mut run).await, Err(RunError::Stopped));
            // The owner read the end of the child before it gave the error.
            assert!(end_was_read(pid));
            assert!(!is_child(pid));

            bench.drained().await;
        });
    }

    #[test]
    fn a_run_with_kill_after_the_stop_signal_starts_no_program() {
        let bench = Bench::new();
        let command = bench.command("touch \"$1/started\"\n", limit(), AtShutdown::Kill);

        bench.trigger.trigger();

        assert_eq!(bench.run(command), Err(RunError::Stopped));
        assert!(!bench.file("started").exists());

        // The owner does not try the start. A try gives `NotStarted` for a
        // program that is absent.
        let absent = bench.file("no-such-program");
        let command = Command::new(absent.to_str().unwrap(), limit(), AtShutdown::Kill);

        assert_eq!(bench.run(command), Err(RunError::Stopped));
    }

    #[test]
    fn a_run_with_kill_whose_caller_left_starts_no_program() {
        let bench = Bench::new();
        // No stop signal came: only the caller left, before the first poll
        // of the owner.
        let (_trigger, shutdown) = shutdown_pair();
        let caller = CancellationToken::new();
        caller.cancel();

        // The owner does not try the start. A try gives `NotStarted` for a
        // program that is absent.
        let absent = bench.file("no-such-program");
        let program = absent.to_str().unwrap();
        let kills = Command::new(program, limit(), AtShutdown::Kill);
        let finishes = Command::new(program, limit(), AtShutdown::Finish);

        runtime().block_on(async {
            assert_eq!(
                within(run_child(&kills, &shutdown, &caller)).await,
                Err(RunError::Stopped)
            );
            // Under `Finish`, a caller that left changes nothing: the owner
            // tries the start.
            assert_eq!(
                within(run_child(&finishes, &shutdown, &caller)).await,
                Err(RunError::NotStarted {
                    os_text: String::from("No such file or directory"),
                })
            );
        });
    }

    #[test]
    fn a_run_with_finish_after_the_stop_signal_runs_to_its_end() {
        let bench = Bench::new();
        let command = bench.command("touch \"$1/started\"\n", limit(), AtShutdown::Finish);

        bench.trigger.trigger();

        assert_eq!(bench.run(command).unwrap().ended(), Ended::Code(0));
        assert!(bench.file("started").exists());
    }

    #[test]
    fn a_runner_works_on_a_runtime_with_more_than_one_thread() {
        let bench = Bench::new();
        let program = bench.program("echo", "echo \"$1\"\n");
        let runtime = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();

        let outputs = runtime.block_on(async {
            let callers: Vec<_> = (0..8_u8)
                .map(|n| {
                    let runner = bench.runner.clone();
                    let command =
                        Command::new(&program, limit(), AtShutdown::Kill).arg(n.to_string());

                    tokio::spawn(async move { runner.run(command).await })
                })
                .collect();
            let mut outputs = Vec::new();

            for caller in callers {
                let finished = within(caller).await.unwrap().unwrap();

                outputs.push(finished.stdout().to_vec());
            }
            bench.drained().await;

            outputs
        });
        let expected: Vec<Vec<u8>> = (0..8_u8).map(|n| format!("{n}\n").into_bytes()).collect();

        assert_eq!(outputs, expected);
    }

    #[test]
    fn the_stop_of_the_runtime_kills_the_child_and_the_caller_gets_owner_lost() {
        let bench = Bench::new();
        // No time limit: only the drop of the child ends it.
        let command = bench.command(HOLD, TimeLimit::None, AtShutdown::Finish);
        let first = runtime();
        let mut run = Box::pin(bench.runner.run(command));

        let pid = first.block_on(async {
            tokio::select! {
                biased;

                result = &mut run => panic!("the run ended: {result:?}"),
                pid = pid_of(bench.file("pid")) => pid,
            }
        });

        assert!(runs(pid));

        // The runtime drops the owner task, and the drop of the child kills
        // it. No code of this runtime reads its end, so `ps` can still show
        // the child as a process that ended.
        drop(first);

        let second = runtime();

        assert_eq!(second.block_on(within(run)), Err(RunError::OwnerLost));
        second.block_on(async {
            let dead = async {
                while runs(pid) {
                    tokio::time::sleep(TICK).await;
                }
            };

            assert!(tokio::time::timeout(LIMIT, dead).await.is_ok());
        });
    }

    /// Reads one line of a pipe of a child, with its newline.
    async fn next_line<R: AsyncRead + Unpin>(reader: &mut BufReader<R>) -> String {
        let mut line = String::new();
        let read = tokio::time::timeout(LIMIT, reader.read_line(&mut line)).await;

        read.unwrap().unwrap();

        line
    }

    /// Reads one line of a pipe of a child that holds a process id.
    async fn next_id<R: AsyncRead + Unpin>(reader: &mut BufReader<R>) -> u32 {
        next_line(reader).await.trim().parse().unwrap()
    }

    /// Starts the script `text` with three pipes and reads the process id
    /// that it prints first.
    async fn piped_with_pid(bench: &Bench, text: &str) -> (u32, ChildStdin, ChildGuard) {
        let piped = spawn_piped(PipedCommand::new(bench.program("piped", text))).unwrap();
        let (stdin, stdout, _stderr, child) = piped.into_parts();
        let pid = next_id(&mut BufReader::new(stdout)).await;

        (pid, stdin, child)
    }

    #[test]
    fn a_piped_child_gets_its_words_its_environment_and_its_directory() {
        let bench = Bench::new();
        let program = bench.program(
            "show",
            "printf '%s|%s|%s\\n' \"$1\" \"$SHOWN\" \"$(pwd -P)\"\n",
        );
        let command = PipedCommand::new(program)
            .arg("a b")
            .env(EnvPolicy::Exactly(vec![
                (String::from("SHOWN"), String::from("yes")),
                (String::from("PATH"), std::env::var("PATH").unwrap()),
            ]))
            .cwd(bench.root.path());
        let expected = format!(
            "a b|yes|{}\n",
            fs::canonicalize(bench.root.path()).unwrap().display()
        );

        runtime().block_on(async {
            let (_stdin, stdout, _stderr, mut child) = spawn_piped(command).unwrap().into_parts();
            let mut reader = BufReader::new(stdout);

            assert_eq!(next_line(&mut reader).await, expected);
            assert_eq!(within(child.wait()).await, Ended::Code(0));
        });
    }

    #[test]
    fn a_piped_child_talks_on_its_pipes_and_ends_at_the_end_of_its_input() {
        runtime().block_on(async {
            let (mut stdin, stdout, stderr, mut child) =
                spawn_piped(PipedCommand::new("cat")).unwrap().into_parts();
            let mut reader = BufReader::new(stdout);

            for line in ["first\n", "second\n"] {
                stdin.write_all(line.as_bytes()).await.unwrap();

                assert_eq!(next_line(&mut reader).await, line);
            }

            drop(stdin);

            assert_eq!(within(child.wait()).await, Ended::Code(0));
            assert_eq!(next_line(&mut reader).await, "");
            assert_eq!(next_line(&mut BufReader::new(stderr)).await, "");

            // After the end, each call gives the same end and sends no
            // signal.
            assert_eq!(within(child.wait()).await, Ended::Code(0));
            assert_eq!(child.terminate(), Ok(()));
            child.start_kill();
            assert_eq!(child.end(GRACE).await, EndOutcome::Ended(Ended::Code(0)));
        });
    }

    #[test]
    fn the_child_has_the_default_action_for_a_broken_pipe() {
        // This process ignores SIGPIPE, as each Rust program does. A child
        // must not: a Python child gets the default action too. The child
        // writes to a pipe that no process reads, and the signal ends it.
        //
        // The child writes more than one time. A program that another test
        // thread starts at this moment holds a copy of the read end until
        // its own start completes, and a write in that time is no error. A
        // child that ignores the signal writes 200 times and exits with
        // status 0.
        let bench = Bench::new();
        let program = bench.program(
            "writer",
            "read line\n\
             count=0\n\
             while [ \"$count\" -lt 200 ]; do\n\
             echo written\n\
             sleep 0.05\n\
             count=$((count + 1))\n\
             done\n\
             exit 0\n",
        );

        let ended = runtime().block_on(async {
            let (mut stdin, stdout, _stderr, mut child) = spawn_piped(PipedCommand::new(program))
                .unwrap()
                .into_parts();

            drop(stdout);
            stdin.write_all(b"go\n").await.unwrap();

            within(child.wait()).await
        });

        assert_eq!(ended, Ended::Signal(BROKEN_PIPE));
    }

    #[test]
    fn the_wait_for_a_piped_child_is_cancel_safe() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (_pid, stdin, mut child) =
                piped_with_pid(&bench, "echo $$\nread line\nexit 5\n").await;

            // Each turn drops a wait that did not end.
            for _ in 0..3 {
                assert!(
                    tokio::time::timeout(TICK, child.wait()).await.is_err(),
                    "the child ended early"
                );
            }

            drop(stdin);

            assert_eq!(within(child.wait()).await, Ended::Code(5));
        });
    }

    #[test]
    fn terminate_ends_a_piped_child_with_sigterm() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (pid, _stdin, mut child) = piped_with_pid(&bench, PIPED_HOLD).await;

            assert!(is_child(pid));
            assert_eq!(child.terminate(), Ok(()));
            assert_eq!(within(child.wait()).await, Ended::Signal(TERMINATED));
            assert!(!is_child(pid));
        });
    }

    #[test]
    fn a_refused_signal_is_an_error_and_a_child_that_ended_is_none() {
        let refused = SignalError {
            kind: io::ErrorKind::PermissionDenied,
            os_text: String::from("Operation not permitted"),
        };
        let table = [
            (Ok(()), Ok(())),
            // No such process: the child ended, and no wait read its end.
            (Err(Errno::SRCH), Ok(())),
            (Err(Errno::PERM), Err(refused)),
        ];

        for (result, wanted) in table {
            assert_eq!(signal_sent(result), wanted, "{result:?}");
        }
    }

    #[test]
    fn start_kill_ends_a_piped_child_that_ignores_sigterm() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (pid, _stdin, mut child) = piped_with_pid(&bench, PIPED_DEAF).await;

            assert_eq!(child.terminate(), Ok(()));
            assert!(
                tokio::time::timeout(GRACE, child.wait()).await.is_err(),
                "the child ended at SIGTERM"
            );

            child.start_kill();

            assert_eq!(within(child.wait()).await, Ended::Signal(KILLED));
            assert!(!is_child(pid));
        });
    }

    /// The Python origin closes the standard input of the child before
    /// SIGTERM (`attendance/src/attendance/exec_channel.py:172-174`).
    /// `ChildGuard::end` holds no pipe: here the input stays open to the end.
    #[test]
    fn end_stops_a_piped_child_with_sigterm_inside_the_grace() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (pid, stdin, mut child) = piped_with_pid(&bench, PIPED_HOLD).await;

            assert_eq!(
                within(child.end(HOUR)).await,
                EndOutcome::Ended(Ended::Signal(TERMINATED))
            );
            assert!(!is_child(pid));

            drop(stdin);
        });
    }

    #[test]
    fn end_kills_a_piped_child_that_ignores_sigterm_after_the_grace() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (pid, _stdin, mut child) = piped_with_pid(&bench, PIPED_DEAF).await;

            {
                let mut ending = pin!(child.end(HOUR));

                // The first wait: SIGTERM does not end the child, and no
                // kill comes before the grace passed.
                assert!(tokio::time::timeout(GRACE, &mut ending).await.is_err());
                assert!(runs(pid));

                skip(HOUR).await;

                // The grace of the first wait passed, and the kill follows.
                assert_eq!(
                    within(&mut ending).await,
                    EndOutcome::Ended(Ended::Signal(KILLED))
                );
            }

            assert!(!is_child(pid));
        });
    }

    #[test]
    fn end_with_no_grace_does_not_wait_and_the_guard_still_reads_the_end() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (pid, _stdin, mut child) = piped_with_pid(&bench, PIPED_DEAF).await;
            let outcome = child.end(Duration::ZERO).await;

            // The kill came, and the second wait had no time. On a slow
            // host the child can end inside the one look of that wait.
            assert!(
                outcome == EndOutcome::LeftRunning
                    || outcome == EndOutcome::Ended(Ended::Signal(KILLED)),
                "{outcome:?}"
            );
            assert_eq!(within(child.wait()).await, Ended::Signal(KILLED));
            assert!(!is_child(pid));
        });
    }

    /// Inside each time limit, the Python origin waits for the end of the
    /// child and of each pipe
    /// (`attendance/src/attendance/exec_channel.py:244-265`).
    /// `ChildGuard::end` waits for the child only.
    #[test]
    fn end_does_not_wait_for_a_pipe_that_another_program_holds() {
        let bench = Bench::new();
        // The child starts a program that keeps the three pipes open for
        // longer than the test runs. It prints the id of that program and
        // then its own id.
        let script = "sleep 300 &\necho $!\necho $$\nexec sleep 300\n";

        runtime().block_on(async {
            let piped = spawn_piped(PipedCommand::new(bench.program("piped", script))).unwrap();
            let (stdin, stdout, _stderr, mut child) = piped.into_parts();
            let mut reader = BufReader::new(stdout);
            let stray = next_id(&mut reader).await;
            let pid = next_id(&mut reader).await;

            assert_eq!(
                within(child.end(HOUR)).await,
                EndOutcome::Ended(Ended::Signal(TERMINATED))
            );
            assert!(!is_child(pid));
            // The pipes are still open: the other program holds them.
            assert!(runs(stray));

            kill_stray(stray);
            drop(stdin);
        });
    }

    #[test]
    fn the_drop_of_a_guard_kills_the_child_and_the_runtime_reads_its_end() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (pid, stdin, child) = piped_with_pid(&bench, PIPED_DEAF).await;

            assert!(runs(pid));

            drop(child);
            gone(pid).await;
            drop(stdin);
        });
    }

    #[test]
    fn a_guard_sends_no_signal_after_its_wait_gave_an_end() {
        let bench = Bench::new();

        runtime().block_on(async {
            let (pid, _stdin, mut child) = piped_with_pid(&bench, PIPED_HOLD).await;

            // The state after a wait call that failed: the guard holds an
            // end, and the operating system can give the id of the child to
            // a new process. Here the program still runs, so a signal shows.
            child.ended = Some(Ended::Code(STATUS_UNKNOWN));

            assert_eq!(child.terminate(), Ok(()));
            child.start_kill();
            assert_eq!(
                child.end(GRACE).await,
                EndOutcome::Ended(Ended::Code(STATUS_UNKNOWN))
            );
            assert_eq!(child.wait().await, Ended::Code(STATUS_UNKNOWN));

            tokio::time::sleep(GRACE).await;

            assert!(runs(pid));

            // The drop still kills: `tokio` holds that flag.
            drop(child);
            gone(pid).await;
        });
    }

    #[test]
    fn a_piped_program_that_does_not_start_is_a_result() {
        let bench = Bench::new();
        let absent = bench.file("no-such-program");
        let refused = runtime().block_on(async {
            spawn_piped(PipedCommand::new(absent.to_str().unwrap())).map(|_piped| ())
        });

        assert_eq!(
            refused,
            Err(RunError::NotStarted {
                os_text: String::from("No such file or directory"),
            })
        );
    }

    /// The variable that selects the scenario of
    /// [`the_child_runs_one_scenario`].
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_COMMAND_TEST_CHILD";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "command::tests::the_child_runs_one_scenario";

    /// The target of the panic hook of the child.
    const CHILD_PROGRAM: &str = "child";

    /// The text that each scenario gets on its standard input. Only the
    /// scenario `inherit` gives it to a child.
    const CHILD_INPUT: &[u8] = b"from-the-input\n";

    /// A program of [`SHELL`] that waits for the file `go` in the directory
    /// `$0` and then exits with the status `$1`.
    const GATE_THEN_EXIT: &str = "while [ ! -e \"$0/go\" ]; do sleep 0.05; done; exit \"$1\"";

    /// A scenario that writes to the log, to the output of the test program
    /// or to its panic hook. It runs in a child, so the test can read what
    /// it wrote.
    struct Scenario {
        /// The value of [`CHILD_VARIABLE`] that selects the scenario.
        name: &'static str,
        /// What the child does.
        run: fn(),
        /// Each line that this module and `tasks` must write to the log:
        /// the level, the target and the message.
        lines: &'static [&'static str],
    }

    const SCENARIOS: [Scenario; 17] = [
        Scenario {
            name: "unread-failure",
            run: a_failure_with_no_caller,
            lines: &[
                "WARNING command the program /bin/sh gave its result to no caller: exit status 3",
            ],
        },
        Scenario {
            name: "unread-limit",
            run: a_time_limit_with_no_caller,
            lines: &[
                "WARNING command the program /bin/sh gave its result to no caller: the \
                      program ran past its limit of 3 seconds",
            ],
        },
        Scenario {
            name: "unread-success",
            run: a_success_with_no_caller,
            lines: &[],
        },
        Scenario {
            name: "read-failure",
            run: a_failure_with_a_caller,
            lines: &[],
        },
        Scenario {
            name: "unread-kill",
            run: a_kill_with_no_caller,
            lines: &[],
        },
        Scenario {
            name: "inherit",
            run: a_child_with_the_streams_of_the_process,
            lines: &[],
        },
        Scenario {
            name: "null-input",
            run: a_child_with_an_empty_input,
            lines: &[],
        },
        Scenario {
            name: "wait-failed",
            run: a_wait_call_of_an_owner_that_fails,
            lines: &[
                "ERROR command the wait for the program tool failed, and its end is not \
                      known: No child processes",
            ],
        },
        Scenario {
            name: "guard-wait-failed",
            run: a_wait_call_of_a_guard_that_fails,
            lines: &[
                "ERROR command the wait for the program cat failed, and its end is not \
                      known: No child processes",
            ],
        },
        Scenario {
            name: "kill-refused",
            run: a_kill_that_the_system_refuses,
            lines: &[
                "ERROR command the kill of the program tool failed, and its owner waits for \
                      its end: Operation not permitted",
            ],
        },
        Scenario {
            name: "read-failed",
            run: a_read_that_fails,
            lines: &["ERROR command a read from the program tool failed: Input/output error"],
        },
        Scenario {
            name: "write-failed",
            run: a_write_that_fails,
            lines: &[
                "ERROR command the write to the input of the program tool failed: \
                      Input/output error",
            ],
        },
        Scenario {
            name: "write-broken-pipe",
            run: a_write_to_a_broken_pipe,
            lines: &[],
        },
        Scenario {
            name: "no-runtime",
            run: a_run_with_no_runtime,
            lines: &[
                "ERROR tasks the task command-owner did not start: no runtime runs on this \
                      thread",
            ],
        },
        Scenario {
            name: "no-io-driver",
            run: a_run_with_no_io_driver,
            lines: &[],
        },
        Scenario {
            name: "no-timer",
            run: a_run_with_no_timer,
            lines: &[],
        },
        Scenario {
            name: "piped-no-runtime",
            run: a_piped_child_with_no_runtime,
            lines: &[],
        },
    ];

    /// Runs one scenario in a child: this test program again, with only the
    /// child test. The child gets [`CHILD_INPUT`] on its standard input and
    /// must end inside [`LIMIT`].
    fn run_scenario(scenario: &str) -> ChildOutput {
        use std::io::Write;

        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([CHILD_TEST, "--exact", "--nocapture", "--test-threads=1"])
            .env(CHILD_VARIABLE, scenario)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        // A scenario that reads no input can end before this write, and the
        // write then fails. The scenario `inherit` checks that the text
        // came.
        let _ = child.stdin.take().unwrap().write_all(CHILD_INPUT);

        let deadline = Instant::now() + LIMIT;

        // A scenario writes a few lines, so a pipe never fills.
        while child.try_wait().unwrap().is_none() {
            if Instant::now() > deadline {
                child.kill().unwrap();
                child.wait().unwrap();

                panic!("the scenario {scenario} did not end");
            }

            thread::sleep(TICK);
        }

        child.wait_with_output().unwrap()
    }

    /// The child of [`each_scenario_writes_its_lines`]. Without the variable
    /// it does nothing. With the variable it sets the panic hook, as a
    /// service does, and runs the one scenario that the variable names.
    #[test]
    fn the_child_runs_one_scenario() {
        let Some(name) = std::env::var_os(CHILD_VARIABLE) else {
            return;
        };
        let scenario = SCENARIOS
            .iter()
            .find(|scenario| name == scenario.name)
            .unwrap();

        crate::log::init(CHILD_PROGRAM);
        (scenario.run)();
    }

    #[test]
    fn each_scenario_writes_its_lines() {
        for scenario in SCENARIOS {
            let name = scenario.name;
            let child = run_scenario(name);
            let stdout = String::from_utf8(child.stdout).unwrap();
            let stderr = String::from_utf8(child.stderr).unwrap();

            assert!(child.status.success(), "{name}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{name}: {stdout}");

            // A line of the log: the time, the level, the target, the
            // message. The hook of the child writes with its own target.
            let written: Vec<&str> = stderr
                .lines()
                .filter_map(|line| line.split_once(' '))
                .map(|(_time, line)| line)
                .filter(|line| {
                    ["command", "tasks"].iter().any(|target| {
                        ["WARNING", "ERROR", "INFO"]
                            .iter()
                            .any(|level| line.starts_with(&format!("{level} {target} ")))
                    })
                })
                .collect();

            assert_eq!(written, scenario.lines, "{name}: {stderr}");
            // No line holds a byte of an input or of an output.
            assert!(!stderr.contains(SECRET_VALUE), "{name}: {stderr}");

            if name == "inherit" {
                assert!(stdout.contains("from-the-input\n"), "{stdout}");
                assert!(stderr.contains("to-the-error\n"), "{stderr}");
            }
        }
    }

    /// What a scenario does with the gate of [`GATE_THEN_EXIT`] after the
    /// caller left.
    #[derive(Clone, Copy, PartialEq, Eq)]
    enum Gate {
        /// The scenario opens the gate, and the child exits.
        Open,
        /// The gate stays closed. The owner ends the child.
        Closed,
    }

    /// Starts a run of [`GATE_THEN_EXIT`], drops its caller and waits for
    /// the owner.
    fn leave(status: &str, limit: TimeLimit, at_shutdown: AtShutdown, gate: Gate) {
        let bench = Bench::new();
        let command = Command::new(SHELL, limit, at_shutdown).args([
            "-c",
            GATE_THEN_EXIT,
            &bench.dir(),
            status,
        ]);

        runtime().block_on(async {
            // The caller starts the run and leaves before its end.
            let left = tokio::time::timeout(GRACE, bench.runner.run(command)).await;

            assert!(left.is_err(), "the run ended: {left:?}");

            if gate == Gate::Open {
                bench.open_gate();
            }
            bench.drained().await;
        });
    }

    /// A child exits with status 3 after its caller left. The owner writes
    /// one line with the status.
    fn a_failure_with_no_caller() {
        leave("3", limit(), AtShutdown::Finish, Gate::Open);
    }

    /// A child runs past its time limit after its caller left. The owner
    /// writes one line with the reason.
    fn a_time_limit_with_no_caller() {
        let limit = TimeLimit::After(SHORT_LIMIT);

        leave("0", limit, AtShutdown::Finish, Gate::Closed);
    }

    /// A child exits with status 0 after its caller left. The owner writes
    /// no line.
    fn a_success_with_no_caller() {
        leave("0", limit(), AtShutdown::Finish, Gate::Open);
    }

    /// A child exits with status 3, and its caller reads that. The owner
    /// writes no line: the caller decides what the status means.
    fn a_failure_with_a_caller() {
        let bench = Bench::new();
        let command = Command::new(SHELL, limit(), AtShutdown::Finish).args(["-c", "exit 3"]);

        assert_eq!(bench.run(command).unwrap().ended(), Ended::Code(3));
    }

    /// The owner kills a child because its caller left. The command asked
    /// for that, so the owner writes no line.
    fn a_kill_with_no_caller() {
        leave("3", limit(), AtShutdown::Kill, Gate::Closed);
    }

    /// A child reads the standard input of the process and writes to its
    /// two output streams: the terminal, for the door that needs it.
    fn a_child_with_the_streams_of_the_process() {
        let bench = Bench::new();
        let command = Command::new(SHELL, TimeLimit::None, AtShutdown::Finish)
            .args(["-c", "cat; echo to-the-error >&2"])
            .stdin(Stdin::Inherit)
            .output(Output::Inherit);

        assert_eq!(bench.run(command), Ok(Finished::new(Ended::Code(0))));
    }

    /// The standard input of the process holds a line. A child with
    /// `Stdin::Null`, which is the input of a new command, reads none of it.
    /// `subprocess.run` of Python gives the child the standard input of the
    /// process (`caregiver/src/caregiver/driver.py:87-89`).
    fn a_child_with_an_empty_input() {
        let bench = Bench::new();
        let finished = bench
            .run(Command::new("cat", limit(), AtShutdown::Kill))
            .unwrap();

        assert_eq!(finished.ended(), Ended::Code(0));
        assert_eq!(finished.stdout(), b"");
    }

    /// The wait call of an owner fails: the operating system holds no child
    /// with that id. The run then gives an error and never a success, and
    /// the log holds one line. CPython gives the return code 0
    /// (`subprocess.py:2040-2049`, version 3.13).
    fn a_wait_call_of_an_owner_that_fails() {
        let no_child = Err(io::Error::from_raw_os_error(NO_CHILD));
        let result = whole(
            "tool",
            &no_child,
            SECRET_VALUE.as_bytes().to_vec(),
            Vec::new(),
        );

        assert_eq!(result, Err(RunError::OwnerLost));
    }

    /// The wait call of a guard fails: another part of the process read the
    /// end of the child first. The end is then the exit status 255 and
    /// never a success, and the log holds one line. The child watcher of
    /// `asyncio` gives the same status (`asyncio/unix_events.py:1000-1012`
    /// of CPython, version 3.13).
    fn a_wait_call_of_a_guard_that_fails() {
        let seen = runtime().block_on(async {
            let (_stdin, _stdout, _stderr, mut child) =
                spawn_piped(PipedCommand::new("cat")).unwrap().into_parts();
            let id = i32::try_from(child.child.id().unwrap()).unwrap();

            child.start_kill();
            // This call takes the end of the child away from the guard.
            waitpid(Pid::from_raw(id), WaitOptions::empty()).unwrap();

            let first = within(child.wait()).await;
            let second = within(child.wait()).await;

            // After that end, the guard sends no signal to the id.
            (first, second, child.terminate())
        });
        let unknown = Ended::Code(STATUS_UNKNOWN);

        assert_eq!(seen, (unknown, unknown, Ok(())));
        assert_ne!(unknown.python_returncode(), 0);
    }

    /// The operating system refuses the kill of a child: the child runs as
    /// another user. The owner writes one line and still waits for the end
    /// of the child. CPython waits for that end too, when the kill raises
    /// an error (`subprocess.py:1130-1131`, version 3.13).
    fn a_kill_that_the_system_refuses() {
        runtime().block_on(async {
            let mut child = FakeChild::new(Some(NOT_PERMITTED));

            killed_and_waited_for(&mut child).await;

            assert_eq!(child.kills, 1);
        });
    }

    /// A read from a stream of a child fails after 7 bytes. The read cuts
    /// the exchange, and the owner writes one line.
    fn a_read_that_fails() {
        let cap = ByteCap::new(100).unwrap();
        let mut stream = (BrokenStream { first: b"partial" }, cap);
        let read = block_on(capture("tool", Some(&mut stream)));

        assert_eq!(read, Err(Cut::Failed));
    }

    /// A write to the standard input of a child fails, and the error is no
    /// broken pipe. The write cuts the exchange, and the owner writes one
    /// line, with no byte of the input.
    fn a_write_that_fails() {
        let pipe = RefusingPipe { errno: IO_ERROR };
        let written = block_on(feed("tool", Some(pipe), SECRET_VALUE.as_bytes()));

        assert_eq!(written, Err(Cut::Failed));
    }

    /// The child closed its standard input before it read each byte. That
    /// is no error of the run, and the owner writes no line.
    fn a_write_to_a_broken_pipe() {
        let pipe = RefusingPipe { errno: PIPE_ERROR };
        let written = block_on(feed("tool", Some(pipe), SECRET_VALUE.as_bytes()));

        assert_eq!(written, Ok(()));
    }

    /// A run on a thread with no runtime starts no program and gives
    /// `OwnerLost` at its first poll.
    fn a_run_with_no_runtime() {
        let bench = Bench::new();
        let command = bench.command("touch \"$1/started\"\n", limit(), AtShutdown::Finish);
        let run = pin!(bench.runner.run(command));
        let mut context = Context::from_waker(Waker::noop());

        assert_eq!(
            run.poll(&mut context),
            Poll::Ready(Err(RunError::OwnerLost))
        );
        assert!(!bench.file("started").exists());
    }

    /// A run in a runtime that `builder` made starts no program and gives
    /// `NotStarted` with `text`.
    fn refused_by_the_runtime(mut builder: Builder, text: &str) {
        let bench = Bench::new();
        let command = bench.command("touch \"$1/started\"\n", limit(), AtShutdown::Finish);
        let runtime = builder.build().unwrap();

        assert_eq!(
            runtime.block_on(bench.runner.run(command)),
            Err(RunError::NotStarted {
                os_text: text.to_owned(),
            })
        );
        assert!(!bench.file("started").exists());
    }

    /// In a runtime with no I/O driver, `tokio` panics after the start of a
    /// program. The owner refuses before that start.
    fn a_run_with_no_io_driver() {
        let mut builder = Builder::new_current_thread();
        builder.enable_time();

        refused_by_the_runtime(builder, "the runtime of this thread has no I/O driver");
    }

    /// In a runtime with no timer, the owner has no time limit. It starts
    /// no program.
    fn a_run_with_no_timer() {
        let mut builder = Builder::new_current_thread();
        builder.enable_io();

        refused_by_the_runtime(builder, "the runtime of this thread has no timer");
    }

    /// `spawn_piped` on a thread with no runtime starts no program.
    fn a_piped_child_with_no_runtime() {
        let bench = Bench::new();
        let program = bench.program("program", "touch \"$1/started\"\n");
        let refused = spawn_piped(PipedCommand::new(program).arg(bench.dir())).map(|_piped| ());

        assert_eq!(
            refused,
            Err(RunError::NotStarted {
                os_text: String::from("no runtime runs on this thread"),
            })
        );

        thread::sleep(GRACE);

        assert!(!bench.file("started").exists());
    }
}
