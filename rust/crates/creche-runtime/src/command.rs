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
//! `caregiver/src/caregiver/driver.py`. This module is a new design and not
//! a translation of that call.
//!
//! The builders and the readers of [`Command`] and of [`PipedCommand`] are
//! complete. Each other function body is a stub. `AGENTS.md` of this crate
//! lists the stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::str::Utf8Error;
use std::sync::Arc;
use std::time::Duration;

use tokio::process::{ChildStderr, ChildStdin, ChildStdout};

use crate::readfile::ByteCap;
use crate::signals::SignalError;
use crate::tasks::Tasks;

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
/// `Debug` prints the count of the bytes and never a byte: a service gives a
/// secret to a child in this way.
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
            Self::Bytes(bytes) => write!(f, "Bytes(<{} bytes>)", bytes.len()),
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
/// assert!(text.contains("Bytes(<13 bytes>)"));
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
    /// `PATH`. The caller states the time limit and what a stop does.
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
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-command writes this body"
    )]
    #[must_use]
    pub fn python_returncode(self) -> i32 {
        todo!()
    }
}

/// A child program that ran to its end.
///
/// `Debug` prints the count of the bytes of each stream and never a byte: a
/// child can write a secret to its output.
#[derive(Clone, PartialEq, Eq)]
pub struct Finished {
    /// How the child ended. An exit status that is not 0 is a result here
    /// and not an error: the caller decides what it means.
    pub ended: Ended,
    /// Each byte of the standard output. Empty for [`Output::Inherit`].
    pub stdout: Vec<u8>,
    /// Each byte of the standard error. Empty for [`Output::Inherit`].
    pub stderr: Vec<u8>,
}

impl fmt::Debug for Finished {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Finished")
            .field("ended", &self.ended)
            .field("stdout", &format_args!("<{} bytes>", self.stdout.len()))
            .field("stderr", &format_args!("<{} bytes>", self.stderr.len()))
            .finish()
    }
}

/// Why a run gave no [`Finished`].
///
/// No variant holds a byte of the output or of the input of the child.
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
    /// The owner task panicked, or the runtime stopped it.
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
#[derive(Debug, Clone)]
pub struct TokioRunner(());

impl TokioRunner {
    /// A runner whose owner tasks are tasks of `tasks`. A drain of `tasks`
    /// then waits for each child that must end whole.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-command writes this body"
    )]
    #[must_use]
    pub fn new(tasks: Tasks) -> Self {
        todo!()
    }
}

impl CommandRunner for TokioRunner {
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-command writes this body"
    )]
    async fn run(&self, command: Command) -> Result<Finished, RunError> {
        todo!()
    }
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
/// # Errors
///
/// [`Utf8Error`] for bytes that are not UTF-8, under [`Decode::Strict`].
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-command writes this body"
)]
pub fn python_text(bytes: &[u8], decode: Decode) -> Result<String, Utf8Error> {
    todo!()
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
/// # Errors
///
/// [`RunError::NotStarted`] when the operating system does not start the
/// program.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-command writes this body"
)]
pub fn spawn_piped(command: PipedCommand) -> Result<Piped, RunError> {
    todo!()
}

/// A child program that runs, with its three pipes.
#[derive(Debug)]
pub struct Piped {
    /// The standard input of the child. To drop it closes the pipe.
    pub stdin: ChildStdin,
    /// The standard output of the child.
    pub stdout: ChildStdout,
    /// The standard error of the child.
    pub stderr: ChildStderr,
    /// The child itself: the one value that stops it and waits for it.
    pub child: ChildGuard,
}

/// The one value that stops a child program and waits for its end.
///
/// To drop the guard kills the child.
#[derive(Debug)]
pub struct ChildGuard(());

impl ChildGuard {
    /// Sends SIGTERM to the child. The call does nothing after the child
    /// ended, so it never signals another process with the same id.
    ///
    /// # Errors
    ///
    /// [`SignalError`] when the operating system refuses the signal.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-command writes this body"
    )]
    pub fn terminate(&self) -> Result<(), SignalError> {
        todo!()
    }

    /// Sends SIGKILL to the child and does not wait.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-command writes this body"
    )]
    pub fn start_kill(&mut self) {
        todo!()
    }

    /// Waits for the end of the child.
    ///
    /// The future is cancel safe: a caller can drop it and call the function
    /// again.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-command writes this body"
    )]
    pub async fn wait(&mut self) -> Ended {
        todo!()
    }

    /// Stops the child in order: SIGTERM, a wait of `grace` at most, SIGKILL,
    /// a second wait of `grace` at most.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-command writes this body"
    )]
    pub async fn end(&mut self, grace: Duration) -> EndOutcome {
        todo!()
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
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    /// The value of a variable of the tests. No output of `Debug` holds it.
    const SECRET_VALUE: &str = "correct horse battery staple";

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

            Ok(Finished {
                ended: Ended::Code(0),
                stdout: command.argv().join(" ").into_bytes(),
                stderr: Vec::new(),
            })
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
        assert!(text.contains("Bytes(<28 bytes>)"), "{text}");
        assert!(!text.contains("correct"), "{text}");
        assert!(!text.contains("99, 111"), "{text}");
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
    fn the_debug_of_each_input_prints_no_byte() {
        assert_eq!(format!("{:?}", Stdin::Null), "Null");
        assert_eq!(format!("{:?}", Stdin::Inherit), "Inherit");
        assert_eq!(
            format!("{:?}", Stdin::Bytes(SECRET_VALUE.as_bytes().to_vec())),
            "Bytes(<28 bytes>)"
        );
    }

    #[test]
    fn the_debug_of_a_result_prints_no_byte_of_the_output() {
        let finished = Finished {
            ended: Ended::Signal(9),
            stdout: SECRET_VALUE.as_bytes().to_vec(),
            stderr: b"warning".to_vec(),
        };

        assert_eq!(
            format!("{finished:?}"),
            "Finished { ended: Signal(9), stdout: <28 bytes>, stderr: <7 bytes> }"
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
            assert_eq!(finished.ended, Ended::Code(0));
            assert_eq!(finished.stdout, b"git rev-parse HEAD");
        }

        assert_eq!(shared.calls.load(Ordering::SeqCst), 3);

        let owned = block_on(sendable(head_of(Echo::default()))).unwrap();

        assert_eq!(owned.stdout, b"git rev-parse HEAD");
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
}
