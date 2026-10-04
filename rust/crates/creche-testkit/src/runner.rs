//! A `CommandRunner` that starts no program.
//!
//! The Python tests replace `subprocess.run` with a fake, for example
//! `caregiver/tests/test_driver.py:23-52`. A Rust service takes its runner as
//! a generic parameter, and its test gives this one.
//!
//! [`FakeRunner`] is the port of that fake, with one difference on purpose:
//! a command that no script names gets an error. The Python fake answers
//! such a command with exit status 0. The table `DEVIATIONS` in the test of
//! this module holds the difference.

use std::sync::{Mutex, MutexGuard, PoisonError};

use creche_runtime::command::{Command, CommandRunner, Finished, RunError};

/// One script: the first words of a command, and the result for it.
type Script = (Vec<String>, Result<Finished, RunError>);

/// A runner that answers each command from a script of the test and keeps
/// each command that it got.
///
/// A command that no script names gets an error and never a success: a test
/// then fails on a command that it did not expect.
///
/// The Python origin is the class `FakeProcess` of
/// `caregiver/tests/test_driver.py:23-45`.
///
/// ```
/// use std::sync::Mutex;
/// use std::time::Duration;
///
/// use creche_runtime::command::{
///     AtShutdown, Command, CommandRunner, Ended, Finished, RunError, TimeLimit,
/// };
/// use creche_testkit::runner::FakeRunner;
///
/// let runner = FakeRunner::new();
/// runner.script(
///     &["sbx", "ls"],
///     Ok(Finished {
///         ended: Ended::Code(0),
///         stdout: b"chat-s1\n".to_vec(),
///         stderr: Vec::new(),
///     }),
/// );
///
/// let limit = TimeLimit::After(Duration::from_secs(30));
/// let listed = Command::new("sbx", limit, AtShutdown::Finish).args(["ls", "--json"]);
/// let removed = Command::new("sbx", limit, AtShutdown::Finish).args(["rm", "chat-s1"]);
///
/// let runtime = tokio::runtime::Builder::new_current_thread().build()?;
/// let first = runtime.block_on(runner.run(listed.clone()));
/// let second = runtime.block_on(runner.run(removed.clone()));
///
/// assert_eq!(first.map(|finished| finished.stdout), Ok(b"chat-s1\n".to_vec()));
/// assert_eq!(
///     second,
///     Err(RunError::NotStarted {
///         os_text: String::from(FakeRunner::NO_SCRIPT),
///     })
/// );
/// assert_eq!(runner.calls(), [listed, removed]);
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module builds the runner only with [`FakeRunner::new`]
/// and gives it a script only with [`FakeRunner::script`]:
///
/// ```compile_fail,E0451
/// use std::sync::Mutex;
/// use std::time::Duration;
///
/// use creche_runtime::command::{
///     AtShutdown, Command, CommandRunner, Ended, Finished, RunError, TimeLimit,
/// };
/// use creche_testkit::runner::FakeRunner;
///
/// let runner = FakeRunner {
///     scripts: Mutex::new(Vec::new()),
///     calls: Mutex::new(Vec::new()),
/// };
/// ```
#[derive(Debug)]
pub struct FakeRunner {
    /// Each script, in the order of the calls of [`FakeRunner::script`]. No
    /// two scripts have the same words.
    scripts: Mutex<Vec<Script>>,
    /// Each command that the runner got, in order.
    calls: Mutex<Vec<Command>>,
}

impl FakeRunner {
    /// The text of the error for a command that no script names. The error
    /// is `RunError::NotStarted` with this text as its `os_text`.
    pub const NO_SCRIPT: &'static str = "the fake runner has no script for this command";

    /// A runner with no script.
    #[must_use]
    pub fn new() -> Self {
        Self {
            scripts: Mutex::new(Vec::new()),
            calls: Mutex::new(Vec::new()),
        }
    }

    /// Scripts `result` for each command whose words start with `prefix`.
    /// The words of a command are its program and then each argument. For a
    /// command that two scripts name, the script with the longer prefix
    /// answers.
    ///
    /// The script stays: it answers each later command with those words. A
    /// second script with the same words replaces the first one. An empty
    /// `prefix` names each command.
    ///
    /// The Python origin is `FakeProcess.answer` of
    /// `caregiver/tests/test_driver.py:35-36`.
    pub fn script(&self, prefix: &[&str], result: Result<Finished, RunError>) {
        let words: Vec<String> = prefix.iter().map(|word| (*word).to_owned()).collect();
        let mut scripts = locked(&self.scripts);

        scripts.retain(|(held, _)| *held != words);
        scripts.push((words, result));
    }

    /// Each command that the runner got, in order. The list also holds each
    /// command that no script names.
    ///
    /// The Python origin is the list `FakeProcess.calls` of
    /// `caregiver/tests/test_driver.py:30`. The Python fake keeps the
    /// environment of a call in a second list. A [`Command`] holds its
    /// environment, so this runner has one list.
    #[must_use]
    pub fn calls(&self) -> Vec<Command> {
        locked(&self.calls).clone()
    }

    /// The result of the script with the longest prefix of `argv`.
    fn answer(&self, argv: &[String]) -> Result<Finished, RunError> {
        let scripts = locked(&self.scripts);
        let best = scripts
            .iter()
            .filter(|(prefix, _)| argv.starts_with(prefix))
            .max_by_key(|(prefix, _)| prefix.len());

        match best {
            Some((_, result)) => result.clone(),
            None => Err(RunError::NotStarted {
                os_text: String::from(Self::NO_SCRIPT),
            }),
        }
    }
}

impl Default for FakeRunner {
    fn default() -> Self {
        Self::new()
    }
}

impl CommandRunner for FakeRunner {
    /// Keeps `command` and answers it from the scripts. The function starts
    /// no program and waits for nothing.
    ///
    /// The Python origin is `FakeProcess.__call__` of
    /// `caregiver/tests/test_driver.py:38-45`.
    async fn run(&self, command: Command) -> Result<Finished, RunError> {
        let result = self.answer(command.argv());
        locked(&self.calls).push(command);

        result
    }
}

/// The value under `mutex`.
///
/// A test that panicked with the lock leaves a poisoned lock. Each list of
/// the runner is valid after each statement, so the function takes it.
fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use std::sync::Arc;
    use std::time::Duration;

    use creche_runtime::command::{AtShutdown, Ended, EnvPolicy, TimeLimit};

    use super::*;

    /// One difference on purpose between this runner and the Python fake.
    struct Deviation {
        /// The place of the Python behavior.
        python: &'static str,
        /// What the Python fake does there.
        python_does: &'static str,
        /// What this runner does.
        rust_does: &'static str,
        /// The test of what this runner does.
        holds: fn(),
    }

    /// Each difference on purpose from the Python fake. Each other behavior
    /// of the fake is the behavior of this runner.
    const DEVIATIONS: &[Deviation] = &[Deviation {
        python: "caregiver/tests/test_driver.py:33 and :45",
        python_does: "A call that no answer names gets exit status 0 and no output.",
        rust_does: "A command that no script names gets RunError::NotStarted.",
        holds: a_command_with_no_script_is_an_error,
    }];

    fn block_on<F: Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(future)
    }

    fn command(words: &[&str]) -> Command {
        let (program, arguments) = words.split_first().unwrap();

        Command::new(
            *program,
            TimeLimit::After(Duration::from_secs(30)),
            AtShutdown::Finish,
        )
        .args(arguments.iter().copied())
    }

    fn exited(status: u8, stdout: &str) -> Result<Finished, RunError> {
        Ok(Finished {
            ended: Ended::Code(status),
            stdout: stdout.as_bytes().to_vec(),
            stderr: Vec::new(),
        })
    }

    fn no_script() -> Result<Finished, RunError> {
        Err(RunError::NotStarted {
            os_text: String::from(FakeRunner::NO_SCRIPT),
        })
    }

    fn a_command_with_no_script_is_an_error() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "ls"], exited(0, ""));

        assert_eq!(
            block_on(runner.run(command(&["git", "status"]))),
            no_script()
        );
        assert_eq!(block_on(runner.run(command(&["sbx"]))), no_script());
        assert_eq!(block_on(runner.run(command(&["sbx", "rm"]))), no_script());
    }

    #[test]
    fn each_deviation_holds() {
        for deviation in DEVIATIONS {
            assert!(deviation.python.contains(".py:"), "{}", deviation.python);
            assert!(!deviation.python_does.is_empty(), "{}", deviation.python);
            assert!(!deviation.rust_does.is_empty(), "{}", deviation.python);

            (deviation.holds)();
        }
    }

    #[test]
    fn a_new_runner_answers_no_command_and_holds_no_call() {
        let runner = FakeRunner::default();

        assert_eq!(runner.calls(), []);
        assert_eq!(block_on(runner.run(command(&["sbx", "ls"]))), no_script());
    }

    #[test]
    fn the_error_of_a_command_with_no_script_says_so() {
        let runner = FakeRunner::new();
        let error = block_on(runner.run(command(&["sbx", "ls"]))).unwrap_err();

        assert_eq!(
            error.to_string(),
            "the program did not start: the fake runner has no script for this command"
        );
    }

    #[test]
    fn a_script_answers_a_command_whose_words_start_with_its_prefix() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "ls"], exited(0, "chat-s1\n"));

        assert_eq!(
            block_on(runner.run(command(&["sbx", "ls"]))),
            exited(0, "chat-s1\n")
        );
        assert_eq!(
            block_on(runner.run(command(&["sbx", "ls", "--json"]))),
            exited(0, "chat-s1\n")
        );
    }

    #[test]
    fn a_prefix_is_whole_words() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "l"], exited(0, ""));

        assert_eq!(block_on(runner.run(command(&["sbx", "ls"]))), no_script());
    }

    #[test]
    fn the_longest_prefix_answers() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "policy", "check"], exited(0, "allowed\n"));
        runner.script(
            &["sbx", "policy", "check", "chat-s1", "example.org"],
            exited(1, "denied\n"),
        );
        runner.script(&["sbx"], exited(2, ""));

        assert_eq!(
            block_on(runner.run(command(&[
                "sbx",
                "policy",
                "check",
                "chat-s1",
                "example.com"
            ]))),
            exited(0, "allowed\n")
        );
        assert_eq!(
            block_on(runner.run(command(&[
                "sbx",
                "policy",
                "check",
                "chat-s1",
                "example.org"
            ]))),
            exited(1, "denied\n")
        );
        assert_eq!(
            block_on(runner.run(command(&["sbx", "rm", "chat-s1"]))),
            exited(2, "")
        );
    }

    #[test]
    fn the_order_of_the_scripts_does_not_select_the_answer() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "policy", "check"], exited(1, ""));
        runner.script(&["sbx"], exited(2, ""));
        runner.script(&["sbx", "policy"], exited(3, ""));

        assert_eq!(
            block_on(runner.run(command(&["sbx", "policy", "check", "chat-s1"]))),
            exited(1, "")
        );
    }

    #[test]
    fn an_empty_prefix_names_each_command() {
        let runner = FakeRunner::new();
        runner.script(&[], exited(0, ""));
        runner.script(&["git"], exited(128, ""));

        assert_eq!(block_on(runner.run(command(&["sbx", "ls"]))), exited(0, ""));
        assert_eq!(
            block_on(runner.run(command(&["git", "status"]))),
            exited(128, "")
        );
    }

    #[test]
    fn a_script_stays_for_each_later_command() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "ls"], exited(0, "chat-s1\n"));

        for _ in 0..3 {
            assert_eq!(
                block_on(runner.run(command(&["sbx", "ls"]))),
                exited(0, "chat-s1\n")
            );
        }
    }

    #[test]
    fn a_second_script_with_the_same_words_replaces_the_first() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "ls"], exited(0, "chat-s1\n"));
        runner.script(&["sbx", "ls"], exited(0, "chat-s2\n"));

        assert_eq!(
            block_on(runner.run(command(&["sbx", "ls"]))),
            exited(0, "chat-s2\n")
        );
        assert_eq!(locked(&runner.scripts).len(), 1);
    }

    #[test]
    fn a_script_can_hold_an_error() {
        let runner = FakeRunner::new();
        let timed_out = Err(RunError::TimedOut {
            after: Duration::from_secs(30),
        });
        runner.script(&["sbx", "create"], timed_out.clone());

        assert_eq!(
            block_on(runner.run(command(&["sbx", "create", "chat-s1"]))),
            timed_out
        );
    }

    #[test]
    fn calls_holds_each_command_in_order() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "ls"], exited(0, ""));
        let listed = command(&["sbx", "ls"]);
        let removed = command(&["sbx", "rm", "chat-s1"]).env(EnvPolicy::InheritAnd(vec![(
            String::from("SBX_NO_TELEMETRY"),
            String::from("1"),
        )]));

        let _ = block_on(runner.run(listed.clone()));
        let _ = block_on(runner.run(removed.clone()));
        let _ = block_on(runner.run(listed.clone()));
        let calls = runner.calls();

        assert_eq!(calls, [listed.clone(), removed.clone(), listed]);
        assert_eq!(calls[1].program(), "sbx");
        assert_eq!(calls[1].argv(), ["sbx", "rm", "chat-s1"]);
        assert_eq!(calls[1].env_policy(), removed.env_policy());
        assert_eq!(calls[1].at_shutdown(), AtShutdown::Finish);
    }

    #[test]
    fn a_runner_is_usable_as_a_generic_runner_and_in_a_task() {
        async fn version<R: CommandRunner>(runner: R) -> Result<Finished, RunError> {
            runner.run(command(&["git", "--version"])).await
        }

        let runner = Arc::new(FakeRunner::new());
        runner.script(&["git"], exited(0, "git version 2\n"));

        let by_reference = block_on(version(&*runner));
        let in_a_task = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .build()
            .unwrap()
            .block_on(async { tokio::spawn(version(Arc::clone(&runner))).await.unwrap() });

        assert_eq!(by_reference, exited(0, "git version 2\n"));
        assert_eq!(in_a_task, exited(0, "git version 2\n"));
        assert_eq!(runner.calls().len(), 2);
    }

    #[test]
    fn the_debug_of_a_runner_prints_no_byte_of_an_output() {
        let runner = FakeRunner::new();
        runner.script(&["sbx", "secret"], exited(0, "correct horse"));
        let text = format!("{runner:?}");

        assert!(text.starts_with("FakeRunner"), "{text}");
        assert!(text.contains("sbx"), "{text}");
        assert!(!text.contains("correct"), "{text}");
    }

    #[test]
    fn a_runner_is_usable_after_a_panic_under_its_lock() {
        let runner = Arc::new(FakeRunner::new());
        let held = Arc::clone(&runner);
        let panicked = std::thread::spawn(move || {
            let _calls = locked(&held.calls);
            panic!("the test poisons the lock");
        })
        .join();

        assert!(panicked.is_err());
        assert!(runner.calls.is_poisoned());

        let _ = block_on(runner.run(command(&["sbx", "ls"])));

        assert_eq!(runner.calls().len(), 1);
    }
}
