//! A `CommandRunner` that starts no program.
//!
//! The Python tests replace `subprocess.run` with a fake, for example
//! `caregiver/tests/test_driver.py:23-52`. A Rust service takes its runner as
//! a generic parameter, and its test gives this one.
//!
//! Each body here is a stub. `AGENTS.md` of this crate lists the stubs and
//! the packet that writes them.

use creche_runtime::command::{Command, CommandRunner, Finished, RunError};

/// A runner that answers each command from a script of the test and keeps
/// each command that it got.
///
/// A command that no script names gets an error and never a success: a test
/// then fails on a command that it did not expect.
#[derive(Debug)]
pub struct FakeRunner(());

impl FakeRunner {
    /// A runner with no script.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    #[must_use]
    pub fn new() -> Self {
        todo!()
    }

    /// Scripts `result` for one command whose words start with `prefix`. For
    /// a command that two scripts name, the script with the longer prefix
    /// answers.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    pub fn script(&self, prefix: &[&str], result: Result<Finished, RunError>) {
        todo!()
    }

    /// Each command that the runner got, in order.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    #[must_use]
    pub fn calls(&self) -> Vec<Command> {
        todo!()
    }
}

impl Default for FakeRunner {
    fn default() -> Self {
        Self::new()
    }
}

impl CommandRunner for FakeRunner {
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-testkit writes this body"
    )]
    async fn run(&self, command: Command) -> Result<Finished, RunError> {
        todo!()
    }
}
