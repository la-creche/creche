//! A small reader for the arguments of a program.
//!
//! A service takes a few flags: `--check`, `--bind <address>`, a subcommand.
//! This reader gives the words one by one, and the program matches them. The
//! workspace takes no crate for that.
//!
//! The reader takes a flag only with its full name. `argparse` of Python
//! also takes a unique start of a name: `--che` for `--check`. A unit file
//! and a script of this repository write each flag in full.
//!
//! The Python origin is the `argparse` parser of each service, for example
//! `door-trigger/src/agent_door_trigger/cli.py`.
//!
//! Each function body here is a stub. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::process::ExitCode;

/// One word of the command line, as [`Args`] reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Word {
    /// A flag, with its dashes: `--check`.
    Flag(String),
    /// A word that is no flag: a subcommand or a positional value.
    Value(String),
}

/// Why a command line is not one that the program takes.
///
/// A command line holds no secret (`AGENTS.md`, "Security rules that hold
/// everywhere"), so an error can name a word of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UsageError {
    /// A word is not UTF-8.
    NotUtf8,
    /// A flag takes a value, and the command line gives none.
    NoValue {
        /// The flag, with its dashes.
        flag: String,
    },
    /// The program does not take this word.
    Unexpected {
        /// The word. For `--name=value` it is `--name`.
        word: String,
    },
    /// A flag or a word that the program needs is absent.
    Missing {
        /// The name of the flag or of the word.
        name: &'static str,
    },
    /// The value of a flag has not the form that the program takes.
    BadValue {
        /// The flag, with its dashes.
        flag: String,
        /// The rule that the value breaks. It does not hold the value.
        reason: String,
    },
}

impl fmt::Display for UsageError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotUtf8 => f.write_str("an argument is not UTF-8"),
            Self::NoValue { flag } => write!(f, "argument {flag}: expected one argument"),
            Self::Unexpected { word } => write!(f, "unrecognized arguments: {word}"),
            Self::Missing { name } => {
                write!(f, "the following arguments are required: {name}")
            }
            Self::BadValue { flag, reason } => write!(f, "argument {flag}: {reason}"),
        }
    }
}

impl Error for UsageError {}

/// The words of a command line, in order.
///
/// The type is an iterator of [`Word`]. [`Args::value`] takes the value of
/// the flag that the iterator gave last.
#[derive(Debug, Clone)]
pub struct Args(());

impl Args {
    /// The reader of the words of `std::env::args_os()`: the name of the
    /// program first, then each argument.
    ///
    /// # Errors
    ///
    /// [`UsageError::NotUtf8`] for a word that is not UTF-8.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-service writes this body"
    )]
    pub fn from_os<I>(words: I) -> Result<Self, UsageError>
    where
        I: IntoIterator<Item = OsString>,
    {
        todo!()
    }

    /// The value of `flag`: the text after `=` in `--name=value`, or the
    /// next word in `--name value`.
    ///
    /// # Errors
    ///
    /// [`UsageError::NoValue`] when the command line holds no value for the
    /// flag.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-service writes this body"
    )]
    pub fn value(&mut self, flag: &str) -> Result<String, UsageError> {
        todo!()
    }
}

impl Iterator for Args {
    type Item = Word;

    /// The next word. `None` after the last one.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-service writes this body"
    )]
    fn next(&mut self) -> Option<Word> {
        todo!()
    }
}

/// Writes the usage line and the error to stderr, and gives exit status 2,
/// as `argparse` of Python does.
///
/// The two lines are `usage` and `<program>: error: <message>`.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-service writes this body"
)]
pub fn usage_exit(program: &str, usage: &str, error: &UsageError) -> ExitCode {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_error_has_the_text_of_argparse_for_the_same_case() {
        // CPython 3.13, argparse.py: the three messages of `parse_args`.
        let table = [
            (UsageError::NotUtf8, "an argument is not UTF-8"),
            (
                UsageError::NoValue {
                    flag: String::from("--bind"),
                },
                "argument --bind: expected one argument",
            ),
            (
                UsageError::Unexpected {
                    word: String::from("--verbose"),
                },
                "unrecognized arguments: --verbose",
            ),
            (
                UsageError::Missing { name: "family" },
                "the following arguments are required: family",
            ),
            (
                UsageError::BadValue {
                    flag: String::from("--family"),
                    reason: String::from("a family name has 1 to 32 characters"),
                },
                "argument --family: a family name has 1 to 32 characters",
            ),
        ];

        for (error, text) in table {
            assert_eq!(error.to_string(), text);
        }
    }
}
