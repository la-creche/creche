//! A small reader for the arguments of a program.
//!
//! A service takes a few flags: `--check`, `--bind <address>`, a subcommand.
//! This reader gives the words one by one, and the program matches them. The
//! workspace takes no crate for that.
//!
//! ```
//! use std::ffi::OsString;
//!
//! use creche_runtime::args::{Args, UsageError, Word};
//!
//! /// What one command line asks for.
//! #[derive(Debug, Default, PartialEq)]
//! struct Asked {
//!     check: bool,
//!     bind: Option<String>,
//! }
//!
//! fn read(words: &[&str]) -> Result<Asked, UsageError> {
//!     let mut args = Args::from_os(words.iter().map(OsString::from))?;
//!     let mut asked = Asked::default();
//!
//!     while let Some(word) = args.next() {
//!         match word {
//!             Word::Flag(flag) if flag == "--check" => asked.check = true,
//!             Word::Flag(flag) if flag == "--bind" => asked.bind = Some(args.value(&flag)?),
//!             Word::Flag(word) | Word::Value(word) => {
//!                 return Err(UsageError::Unexpected { word });
//!             }
//!         }
//!     }
//!
//!     Ok(asked)
//! }
//!
//! let asked = read(&["noticeboard", "--check", "--bind=192.0.2.10:8370"])?;
//! assert!(asked.check);
//! assert_eq!(asked.bind.as_deref(), Some("192.0.2.10:8370"));
//!
//! let error = read(&["noticeboard", "--che"]).unwrap_err();
//! assert_eq!(error.to_string(), "unrecognized arguments: --che");
//! # Ok::<(), UsageError>(())
//! ```
//!
//! # The kind of a word
//!
//! 1. The first word is the name of the program. The reader does not give
//!    it.
//! 2. The word `--` ends the flags. The reader does not give it, and each
//!    later word is a [`Word::Value`].
//! 3. Before `--`, a word that starts with `-` and has more characters is a
//!    [`Word::Flag`]. The name of the flag is the text before the first `=`
//!    of the word, or the whole word when it has no `=`.
//! 4. Two kinds of such a word are a [`Word::Value`]: a negative number, for
//!    example `-5` or `-.5`, and a word with a space in that name.
//! 5. Each other word is a [`Word::Value`]: the empty word and `-` too.
//! 6. The text after the first `=` of a flag is the value of the flag, and
//!    [`Args::value`] gives it.
//!
//! # The Python origin
//!
//! The Python origin is the `argparse` parser of each service, for example
//! `_parse_args` of `door-trigger/src/agent_door_trigger/cli.py:62-89`. The
//! rules above are the rules of `_parse_optional` of CPython 3.13
//! (`argparse.py:2352-2396`), for a parser whose flags each start with `--`.
//! No Python service has a flag of another form.
//!
//! The reader differs from `argparse` in these ways. A plain test holds each
//! one.
//!
//! - `argparse` takes a unique start of a flag name: `--che` for `--check`
//!   (`argparse.py:1805`, `allow_abbrev`). The reader gives the word as it
//!   is, so a program takes a flag only with its full name.
//! - `argparse` answers `-h` and `--help` itself, with status 0. The reader
//!   gives each one as a flag. A program that has a help text matches the
//!   flag.
//! - `argparse` reads a number with a digit that is not ASCII as a value,
//!   for example `-` and the digit U+0663. It also reads `-5` with a newline
//!   after it as a value. The reader gives each one as a flag.
//! - For a name that is no flag of the parser, `argparse` reads
//!   `--name=a b` as a value, because the word holds a space. The reader
//!   gives the flag `--name`.
//! - `sys.argv` of Python holds a word that is not UTF-8 as a text with
//!   escapes, and a Python program can take it. The reader refuses the
//!   command line.
//! - `argparse` takes the word `--` only together with a positional word of
//!   its parser. It refuses a `--` that comes when the parser has no such
//!   word left to take: `unrecognized arguments: --`. A parser with no
//!   positional word thus refuses each `--`. A parser with one such word
//!   refuses `fam --check --`. The reader drops the first `--` in each
//!   place, so a program accepts such a command line.
//! - The text of an error can differ. For `--check=yes`, `argparse` says
//!   that the flag ignores the value. A program on this reader says that it
//!   does not take the word `--check=yes`. `argparse` names each word that
//!   it does not take in one message. [`UsageError::Unexpected`] names one
//!   word.
//!
//! No unit file, no component manifest, no hook and no script of this
//! repository writes a command line on which the reader and `argparse`
//! differ, with one exception. `library/Dockerfile` gives its program the
//! flag `--help`. The port of that program matches the flag.
//!
//! Python 3.14 reads more forms of a negative number as a value, for example
//! `-1e5`. The reader follows Python 3.13 (`rust/AGENTS.md`, "When two
//! Python versions differ").

use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::io;
use std::process::ExitCode;

use crate::log;

/// The word that ends the flags of a command line.
const END_OF_FLAGS: &str = "--";

/// The character between a flag and its value in one word.
const VALUE_MARK: char = '=';

/// The exit status of a command line that a program does not take. `argparse`
/// of Python ends with it (`argparse.py:2686`, CPython 3.13).
const USAGE_STATUS: u8 = 2;

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
        /// The word. For `--name=value` it is `--name`. It is the whole
        /// word when the program takes the flag `--name` and gives it no
        /// value.
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

/// What one word is, before the word `--`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind<'a> {
    /// The word `--`.
    EndOfFlags,
    /// A flag, and the text after its first `=` when the word has one.
    Flag(&'a str, Option<&'a str>),
    /// A word that is no flag.
    Value,
}

/// The kind of `word`, by the rules 2 to 5 of the module.
///
/// The Python origin is `_parse_optional` of CPython 3.13
/// (`argparse.py:2352-2396`).
fn kind_of(word: &str) -> Kind<'_> {
    if word == END_OF_FLAGS {
        return Kind::EndOfFlags;
    }

    // The empty word and `-` are values too: the second has no character
    // after its dash.
    if !word.starts_with('-') || word.len() == 1 || is_negative_number(word) {
        return Kind::Value;
    }

    let (name, value) = match word.split_once(VALUE_MARK) {
        Some((name, value)) => (name, Some(value)),
        None => (word, None),
    };

    if name.contains(' ') {
        return Kind::Value;
    }

    Kind::Flag(name, value)
}

/// Whether `word` is `-` and a number. A number has one of two forms. The
/// first form is one ASCII digit or more. The second form is a `.` with one
/// ASCII digit or more after it. ASCII digits can be before that `.`.
///
/// The Python origin is the pattern `^-\d+$|^-\d*\.\d+$` of CPython 3.13
/// (`argparse.py:1427`). That pattern also takes a digit that is not ASCII,
/// and a newline after the number. This function takes neither.
fn is_negative_number(word: &str) -> bool {
    let Some(number) = word.strip_prefix('-') else {
        return false;
    };
    let all_digits = |text: &str| text.bytes().all(|byte| byte.is_ascii_digit());

    match number.split_once('.') {
        None => !number.is_empty() && all_digits(number),
        Some((whole, fraction)) => {
            all_digits(whole) && !fraction.is_empty() && all_digits(fraction)
        }
    }
}

/// The words of a command line, in order.
///
/// The type is an iterator of [`Word`]. [`Args::value`] takes the value of
/// the flag that the iterator gave last.
///
/// ```
/// use std::ffi::OsString;
///
/// use creche_runtime::args::{Args, Word};
///
/// let words = ["agent-trigger", "fire", "chat", "--trigger=mail", "--", "-x"];
/// let mut args = Args::from_os(words.map(OsString::from))?;
///
/// assert_eq!(args.next(), Some(Word::Value(String::from("fire"))));
/// assert_eq!(args.next(), Some(Word::Value(String::from("chat"))));
/// assert_eq!(args.next(), Some(Word::Flag(String::from("--trigger"))));
/// assert_eq!(args.value("--trigger")?, "mail");
/// assert_eq!(args.next(), Some(Word::Value(String::from("-x"))));
/// assert_eq!(args.next(), None);
/// # Ok::<(), creche_runtime::args::UsageError>(())
/// ```
///
/// Code outside this module cannot build a reader from raw parts, and cannot
/// change a part. Each word that a program gets then comes from the command
/// line:
///
/// ```compile_fail,E0451
/// use std::ffi::OsString;
///
/// use creche_runtime::args::{Args, Word};
///
/// let args = Args {
///     words: vec![String::from("--check")].into_iter(),
///     inline: None,
///     values_only: false,
/// };
/// ```
#[derive(Debug, Clone)]
pub struct Args {
    /// The words that the iterator did not give yet, in order.
    words: std::vec::IntoIter<String>,
    /// The flag that the iterator gave last and the text after its first
    /// `=`, when the word has that form and the program did not take the
    /// text yet. The text can be empty.
    inline: Option<(String, String)>,
    /// Whether the word `--` came. Each later word is then a value.
    values_only: bool,
}

impl Args {
    /// The reader of the words of `std::env::args_os()`: the name of the
    /// program first, then each argument. The reader does not give the name
    /// of the program, and that word can be each text.
    ///
    /// The Python origin is `sys.argv[1:]` in the `main` of each service,
    /// for example `attendance/src/attendance/__main__.py:49`. Python holds
    /// a word that is not UTF-8 as a text with escapes. This function
    /// refuses the command line.
    ///
    /// # Errors
    ///
    /// [`UsageError::NotUtf8`] for a word that is not UTF-8.
    pub fn from_os<I>(words: I) -> Result<Self, UsageError>
    where
        I: IntoIterator<Item = OsString>,
    {
        let mut arguments = Vec::new();

        for word in words.into_iter().skip(1) {
            let Ok(text) = word.into_string() else {
                return Err(UsageError::NotUtf8);
            };
            arguments.push(text);
        }

        Ok(Self {
            words: arguments.into_iter(),
            inline: None,
            values_only: false,
        })
    }

    /// The value of `flag`: the text after `=` in `--name=value`, or the
    /// next word in `--name value`. Call it for the flag that the iterator
    /// gave last. `flag` is the name in the error.
    ///
    /// The next word is a value only when it is a [`Word::Value`]. A flag
    /// and the word `--` are no value: write `--name=-x` for a value that
    /// starts with a dash.
    ///
    /// The Python origin is `_match_argument` of CPython 3.13
    /// (`argparse.py:2313-2333`), with its message `expected one argument`.
    ///
    /// # Errors
    ///
    /// [`UsageError::NoValue`] when the command line holds no value for the
    /// flag.
    pub fn value(&mut self, flag: &str) -> Result<String, UsageError> {
        if let Some((_name, value)) = self.inline.take() {
            return Ok(value);
        }

        self.take_value().ok_or_else(|| UsageError::NoValue {
            flag: flag.to_owned(),
        })
    }

    /// Takes the next word when it is a value. `None` leaves each word in
    /// its place.
    fn take_value(&mut self) -> Option<String> {
        let word = self.words.as_slice().first()?;

        if self.values_only || kind_of(word) == Kind::Value {
            self.words.next()
        } else {
            None
        }
    }
}

impl Iterator for Args {
    type Item = Word;

    /// The next word. `None` after the last one.
    ///
    /// For `--name=value` the word is the flag `--name`. Take the value with
    /// [`Args::value`] before the next call. When the program does not take
    /// it, the next call gives the flag `--name=value`: the whole word. No
    /// program has a flag with `=` in its name, so the program refuses that
    /// word, and a flag that takes no value gets none.
    ///
    /// The Python origin is `_parse_optional` of CPython 3.13
    /// (`argparse.py:2352-2396`). For a value that a flag does not take,
    /// `argparse` has an error of its own (`argparse.py:2117`).
    fn next(&mut self) -> Option<Word> {
        if let Some((name, value)) = self.inline.take() {
            return Some(Word::Flag(format!("{name}{VALUE_MARK}{value}")));
        }

        loop {
            let word = self.words.next()?;

            if self.values_only {
                return Some(Word::Value(word));
            }

            match kind_of(&word) {
                Kind::EndOfFlags => self.values_only = true,
                Kind::Value => return Some(Word::Value(word)),
                Kind::Flag(_, None) => return Some(Word::Flag(word)),
                Kind::Flag(name, Some(value)) => {
                    let name = name.to_owned();
                    self.inline = Some((name.clone(), value.to_owned()));

                    return Some(Word::Flag(name));
                }
            }
        }
    }
}

/// Writes the usage line and the error to stderr, and gives exit status 2,
/// as `argparse` of Python does.
///
/// The two lines are `usage` and `<program>: error: <message>`. A control
/// character of the second line is an escape there, as in a line of the
/// log. A word of a command line thus cannot start a third line.
///
/// The function is for a command that runs to its end and that systemd does
/// not start again, for example a verify hook. A daemon must not call it. A
/// daemon passes its [`UsageError`] to
/// [`service::refuse_start`](crate::service::refuse_start) and ends with
/// `EX_CONFIG`: no restart repairs a command line (`rust/AGENTS.md`, "The
/// rules for a service", rule 17).
///
/// The Python origin is `error` of CPython 3.13 (`argparse.py:2675-2686`).
/// The function drops the error of a write, and Python raises there.
pub fn usage_exit(program: &str, usage: &str, error: &UsageError) -> ExitCode {
    usage_to(&mut io::stderr().lock(), program, usage, error)
}

/// [`usage_exit`] on the given writer.
fn usage_to(
    writer: &mut dyn io::Write,
    program: &str,
    usage: &str,
    error: &UsageError,
) -> ExitCode {
    let mut refusal = String::new();
    log::escape_into(&mut refusal, &format!("{program}: error: {error}"));

    log::write_line(writer, usage);
    log::write_line(writer, &refusal);

    ExitCode::from(USAGE_STATUS)
}

#[cfg(test)]
mod tests {
    use std::os::unix::ffi::OsStringExt;

    use super::*;
    use crate::service::tests::{Closed, lines_of, same_status};

    /// What one command line asks of the test program. The program has the
    /// flag `--check`, the flag `--bind` with one value and one optional
    /// word, as a parser of `argparse` with `store_true`, one argument and
    /// `nargs="?"`.
    #[derive(Debug, Default, PartialEq, Eq)]
    struct Asked {
        check: bool,
        bind: Option<String>,
        family: Option<String>,
    }

    fn asked(check: bool, bind: Option<&str>, family: Option<&str>) -> Asked {
        Asked {
            check,
            bind: bind.map(str::to_owned),
            family: family.map(str::to_owned),
        }
    }

    fn reader(words: &[&str]) -> Args {
        let command_line = std::iter::once("probe").chain(words.iter().copied());

        Args::from_os(command_line.map(OsString::from)).unwrap()
    }

    /// Reads a command line as a service does: one `match` over the words.
    fn read(words: &[&str]) -> Result<Asked, UsageError> {
        let mut args = reader(words);
        let mut asked = Asked::default();

        while let Some(word) = args.next() {
            match word {
                Word::Flag(flag) if flag == "--check" => asked.check = true,
                Word::Flag(flag) if flag == "--bind" => asked.bind = Some(args.value(&flag)?),
                Word::Value(word) if asked.family.is_none() => asked.family = Some(word),
                Word::Flag(word) | Word::Value(word) => {
                    return Err(UsageError::Unexpected { word });
                }
            }
        }

        Ok(asked)
    }

    /// The result of a command line as one text: the three values, or the
    /// text of the error.
    fn result_of(words: &[&str]) -> Result<Asked, String> {
        read(words).map_err(|error| error.to_string())
    }

    fn refused(text: &str) -> Result<Asked, String> {
        Err(text.to_owned())
    }

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

    #[test]
    fn a_command_line_gives_what_argparse_gives() {
        // Each row is the result of CPython 3.13 for the same parser:
        // `--check` with `store_true`, `--bind` with one argument and
        // `family` with `nargs="?"`. For an error, the text is the message
        // of `argparse` after `probe: error: `.
        const ONE_ARGUMENT: &str = "argument --bind: expected one argument";
        let table: Vec<(&[&str], Result<Asked, String>)> = vec![
            (&[], Ok(asked(false, None, None))),
            (&["--check"], Ok(asked(true, None, None))),
            (&["--check", "--check"], Ok(asked(true, None, None))),
            (&["--bind", "a"], Ok(asked(false, Some("a"), None))),
            (&["--bind=a"], Ok(asked(false, Some("a"), None))),
            (&["--bind="], Ok(asked(false, Some(""), None))),
            (&["--bind", ""], Ok(asked(false, Some(""), None))),
            (&["--bind=a=b"], Ok(asked(false, Some("a=b"), None))),
            (&["--bind=a b"], Ok(asked(false, Some("a b"), None))),
            (
                &["--bind", "a", "--bind", "b"],
                Ok(asked(false, Some("b"), None)),
            ),
            (&["--bind"], refused(ONE_ARGUMENT)),
            (&["--bind", "--check"], refused(ONE_ARGUMENT)),
            (&["--bind", "--", "x"], refused(ONE_ARGUMENT)),
            (&["--bind", "-x"], refused(ONE_ARGUMENT)),
            // A negative number is a value.
            (&["--bind", "-5"], Ok(asked(false, Some("-5"), None))),
            (&["--bind", "-.5"], Ok(asked(false, Some("-.5"), None))),
            (&["--bind", "-5.5"], Ok(asked(false, Some("-5.5"), None))),
            (&["-5"], Ok(asked(false, None, Some("-5")))),
            // These are no number for Python 3.13.
            (&["--bind", "-5."], refused(ONE_ARGUMENT)),
            (&["--bind", "-1e5"], refused(ONE_ARGUMENT)),
            (&["--bind", "-0x10"], refused(ONE_ARGUMENT)),
            (&["--bind", "-1_000"], refused(ONE_ARGUMENT)),
            // One dash and the empty word are values.
            (&["--bind", "-"], Ok(asked(false, Some("-"), None))),
            (&["-"], Ok(asked(false, None, Some("-")))),
            (&[""], Ok(asked(false, None, Some("")))),
            // A word with a space is a value.
            (&["--bind", "-x y"], Ok(asked(false, Some("-x y"), None))),
            (&["-x y"], Ok(asked(false, None, Some("-x y")))),
            // The word `--` ends the flags.
            (&["--"], Ok(asked(false, None, None))),
            (&["--", "--check"], Ok(asked(false, None, Some("--check")))),
            (&["--", "-x"], Ok(asked(false, None, Some("-x")))),
            (&["--", "--"], Ok(asked(false, None, Some("--")))),
            (
                &["fam", "--", "more"],
                refused("unrecognized arguments: more"),
            ),
            (&["fam"], Ok(asked(false, None, Some("fam")))),
            (&["fam", "--check"], Ok(asked(true, None, Some("fam")))),
            (&["extra", "more"], refused("unrecognized arguments: more")),
            (&["--verbose"], refused("unrecognized arguments: --verbose")),
            (&["-c"], refused("unrecognized arguments: -c")),
        ];

        for (words, result) in table {
            assert_eq!(result_of(words), result, "{words:?}");
        }
    }

    #[test]
    fn the_reader_differs_from_argparse_on_these_command_lines() {
        // The comment of a row is what CPython 3.13 gives for the parser of
        // the test above. The doc comment of the module names each
        // difference.
        let table: Vec<(&[&str], Result<Asked, String>)> = vec![
            // `check` is true: a unique start of a flag name is the flag.
            (&["--che"], refused("unrecognized arguments: --che")),
            // `bind` is `a`.
            (&["--bi", "a"], refused("unrecognized arguments: --bi")),
            // `bind` is `a`.
            (&["--bi=a"], refused("unrecognized arguments: --bi")),
            // Status 0, and the help text on stdout.
            (&["-h"], refused("unrecognized arguments: -h")),
            // Status 0, and the help text on stdout.
            (&["--help"], refused("unrecognized arguments: --help")),
            // `bind` is the word: the digit is a digit for Python.
            (
                &["--bind", "-\u{663}"],
                refused("argument --bind: expected one argument"),
            ),
            // `family` is the word.
            (&["-\u{663}"], refused("unrecognized arguments: -\u{663}")),
            // `bind` is the word, with its newline.
            (
                &["--bind", "-5\n"],
                refused("argument --bind: expected one argument"),
            ),
            // `family` is the whole word: it holds a space, and no flag has
            // the name.
            (
                &["--unknown=a b"],
                refused("unrecognized arguments: --unknown"),
            ),
            // An error: `unrecognized arguments: --`. The parser took its
            // one word before the flag, so no word is left to take the `--`.
            (
                &["fam", "--check", "--"],
                Ok(asked(true, None, Some("fam"))),
            ),
            // The same error.
            (
                &["fam", "--bind", "a", "--"],
                Ok(asked(false, Some("a"), Some("fam"))),
            ),
            // An error with another text: the flag ignores the value `yes`.
            (
                &["--check=yes"],
                refused("unrecognized arguments: --check=yes"),
            ),
            // An error with another text: the word can be the start of
            // three flags.
            (&["--=x"], refused("unrecognized arguments: --")),
        ];

        for (words, result) in table {
            assert_eq!(result_of(words), result, "{words:?}");
        }

        // A parser of `argparse` with no positional word refuses `--`:
        // `unrecognized arguments: --`. The reader gives a program no word
        // for it, so such a program cannot refuse it.
        assert_eq!(reader(&["--"]).next(), None);
    }

    #[test]
    fn a_word_that_is_not_utf8_refuses_the_command_line() {
        // Python takes such a word as a text with escapes.
        let words = [
            OsString::from("probe"),
            OsString::from("--bind"),
            OsString::from_vec(vec![b'a', 0xff]),
        ];

        assert_eq!(Args::from_os(words).unwrap_err(), UsageError::NotUtf8);
    }

    #[test]
    fn the_name_of_the_program_is_no_word_and_can_be_each_text() {
        let words = [
            OsString::from_vec(vec![b'/', 0xff, b'/', b'p']),
            OsString::from("--check"),
        ];
        let given: Vec<Word> = Args::from_os(words).unwrap().collect();

        assert_eq!(given, [Word::Flag(String::from("--check"))]);
        assert_eq!(Args::from_os([]).unwrap().next(), None);
    }

    #[test]
    fn the_iterator_gives_each_word_with_its_kind() {
        let words = ["serve", "--check", "-v", "--bind", "a", "--", "--x", "--"];
        let given: Vec<Word> = reader(&words).collect();

        assert_eq!(
            given,
            [
                Word::Value(String::from("serve")),
                Word::Flag(String::from("--check")),
                Word::Flag(String::from("-v")),
                Word::Flag(String::from("--bind")),
                Word::Value(String::from("a")),
                Word::Value(String::from("--x")),
                Word::Value(String::from("--")),
            ]
        );
    }

    #[test]
    fn a_value_that_the_program_does_not_take_comes_back_as_one_flag() {
        let mut args = reader(&["--check=yes", "fam"]);

        assert_eq!(args.next(), Some(Word::Flag(String::from("--check"))));
        assert_eq!(args.next(), Some(Word::Flag(String::from("--check=yes"))));
        assert_eq!(args.next(), Some(Word::Value(String::from("fam"))));
        assert_eq!(args.next(), None);

        // The last word of a command line comes back too, and an empty
        // value.
        let mut args = reader(&["--check="]);

        assert_eq!(args.next(), Some(Word::Flag(String::from("--check"))));
        assert_eq!(args.next(), Some(Word::Flag(String::from("--check="))));
        assert_eq!(args.next(), None);
    }

    #[test]
    fn the_value_of_a_flag_is_its_own_text_or_the_next_value() {
        let mut args = reader(&["--bind=a", "b", "--bind", "c", "--bind"]);

        assert_eq!(args.next(), Some(Word::Flag(String::from("--bind"))));
        assert_eq!(args.value("--bind"), Ok(String::from("a")));
        // The value after `=` is taken one time. The next call takes the
        // next word.
        assert_eq!(args.value("--bind"), Ok(String::from("b")));
        assert_eq!(args.next(), Some(Word::Flag(String::from("--bind"))));
        assert_eq!(args.value("--bind"), Ok(String::from("c")));
        assert_eq!(args.next(), Some(Word::Flag(String::from("--bind"))));
        assert_eq!(
            args.value("--bind"),
            Err(UsageError::NoValue {
                flag: String::from("--bind")
            })
        );
        assert_eq!(args.next(), None);
    }

    #[test]
    fn a_value_is_not_taken_from_a_flag_or_from_the_end_of_the_flags() {
        for rest in [["--check", "x"], ["--", "x"]] {
            let mut args = reader(&["--bind", rest[0], rest[1]]);

            assert_eq!(args.next(), Some(Word::Flag(String::from("--bind"))));
            assert_eq!(
                args.value("--bind"),
                Err(UsageError::NoValue {
                    flag: String::from("--bind")
                })
            );
            // The refusal takes no word.
            assert_eq!(args.len_left(), 2);
        }

        // After `--`, each word is a value, also for a flag before it.
        let mut args = reader(&["--", "--bind", "-x"]);

        assert_eq!(args.next(), Some(Word::Value(String::from("--bind"))));
        assert_eq!(args.value("--bind"), Ok(String::from("-x")));
    }

    #[test]
    fn a_negative_number_has_ascii_digits_only() {
        let numbers = ["-0", "-5", "-007", "-.5", "-0.5", "-12.75"];
        let others = [
            "-",
            "--",
            "-.",
            "-5.",
            "-5.5.5",
            "-1e5",
            "-0x10",
            "-1_000",
            "-+5",
            "- 5",
            "-5 ",
            "-5\n",
            "-\u{663}",
            "-5\u{663}",
            "5",
            "",
            ".5",
            "-a",
        ];

        for word in numbers {
            assert!(is_negative_number(word), "{word:?}");
        }
        for word in others {
            assert!(!is_negative_number(word), "{word:?}");
        }
    }

    #[test]
    fn a_clone_reads_the_same_words_again() {
        let mut args = reader(&["--bind=a", "fam"]);
        assert_eq!(args.next(), Some(Word::Flag(String::from("--bind"))));

        let mut copy = args.clone();

        assert_eq!(args.value("--bind"), Ok(String::from("a")));
        assert_eq!(copy.value("--bind"), Ok(String::from("a")));
        assert_eq!(copy.next(), args.next());
    }

    impl Args {
        /// The count of the words that the reader did not give yet.
        fn len_left(&self) -> usize {
            self.words.as_slice().len()
        }
    }

    #[test]
    fn a_usage_error_writes_two_lines_and_gives_the_status_of_argparse() {
        // CPython 3.13 writes the same two lines for the parser of the
        // table tests, and ends with status 2.
        let usage = "usage: probe [-h] [--check] [--bind BIND] [family]";
        let error = read(&["--verbose"]).unwrap_err();
        let mut written = Vec::new();

        let status = usage_to(&mut written, "probe", usage, &error);

        assert!(same_status(status, 2));
        assert!(!same_status(status, 0));
        assert_eq!(
            lines_of(&written),
            [usage, "probe: error: unrecognized arguments: --verbose"]
        );
        assert!(written.ends_with(b"\n"));
    }

    #[test]
    fn a_word_with_a_control_character_stays_on_the_line_of_the_error() {
        let error = read(&["--one\ntwo\u{1b}[2J"]).unwrap_err();
        let mut written = Vec::new();

        let _status = usage_to(&mut written, "probe", "usage: probe", &error);

        assert_eq!(
            lines_of(&written),
            [
                "usage: probe",
                "probe: error: unrecognized arguments: --one\\ntwo\\u{1b}[2J"
            ]
        );
    }

    #[test]
    fn a_closed_stream_does_not_stop_the_usage_error() {
        let error = UsageError::Missing { name: "family" };

        let status = usage_to(&mut Closed, "probe", "usage: probe family", &error);

        assert!(same_status(status, 2));
    }
}
