//! The launch step `vmm launch`. The vmm runs the program again in a child
//! process with this mode, and the child starts one Cloud Hypervisor
//! process (decision CH8-2).

use std::convert::Infallible;
use std::fmt;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// An absolute path of a program to exec. `systemd-run` and the Landlock
/// rules of PR C6 need the full path, never a search of `PATH`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "PathBuf", into = "PathBuf")]
pub struct Program(PathBuf);

/// Why a path is not a program path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NotAbsolute;

impl fmt::Display for NotAbsolute {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a program path is absolute")
    }
}

impl std::error::Error for NotAbsolute {}

impl Program {
    /// Checks `path` and makes the program path.
    ///
    /// # Errors
    ///
    /// `NotAbsolute` for a relative path.
    pub fn new(path: PathBuf) -> Result<Self, NotAbsolute> {
        if path.is_absolute() {
            Ok(Self(path))
        } else {
            Err(NotAbsolute)
        }
    }

    /// The path itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl TryFrom<PathBuf> for Program {
    type Error = NotAbsolute;

    fn try_from(path: PathBuf) -> Result<Self, Self::Error> {
        Self::new(path)
    }
}

impl From<Program> for PathBuf {
    fn from(program: Program) -> Self {
        program.0
    }
}

/// The program that runs in the scope and its arguments. PR C7 renders
/// the Cloud Hypervisor command line into one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Payload {
    program: Program,
    args: Vec<String>,
}

impl Payload {
    /// The payload `program` with `args`.
    #[must_use]
    pub fn new(program: Program, args: Vec<String>) -> Self {
        Self { program, args }
    }

    /// The program.
    #[must_use]
    pub fn program(&self) -> &Program {
        &self.program
    }

    /// The arguments after the program.
    #[must_use]
    pub fn args(&self) -> &[String] {
        &self.args
    }
}

/// Why the launch step did not start the Cloud Hypervisor process.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LaunchError {
    /// The launch step has no code yet. A start that reaches it fails
    /// closed (owner item 86).
    NotBuilt,
}

impl fmt::Display for LaunchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let reason = match self {
            Self::NotBuilt => "the launch step is not built yet",
        };
        formatter.write_str(reason)
    }
}

/// Runs the launch step. A launch that works replaces this process with
/// the next program, so this function returns only an error.
pub fn run() -> Result<Infallible, LaunchError> {
    Err(LaunchError::NotBuilt)
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::{LaunchError, NotAbsolute, Payload, Program, run};

    #[test]
    fn a_program_path_is_absolute() {
        assert_eq!(Program::new(PathBuf::from("sleep")), Err(NotAbsolute));
        assert!(Program::new(PathBuf::from("/usr/bin/sleep")).is_ok());
    }

    #[test]
    fn a_payload_with_a_relative_program_is_refused() {
        let json = r#"{"program":"sleep","args":["60"]}"#;
        assert!(serde_json::from_str::<Payload>(json).is_err());
        let json = r#"{"program":"/usr/bin/sleep","args":["60"]}"#;
        let payload: Payload = serde_json::from_str(json).unwrap();
        assert_eq!(payload.args(), ["60"]);
    }

    #[test]
    fn run_refuses_until_built() {
        assert_eq!(run(), Err(LaunchError::NotBuilt));
    }
}
