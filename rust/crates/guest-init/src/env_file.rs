//! The strict reader of `/etc/creche/env`, the image env of the root disk
//! (decisions CH7-7, CH49 rule 1, CH62 rule 7).
//!
//! The last build step of each flavor writes the file with
//! `playpen/bin/creche-env-write`, and the grammar is what that script
//! writes (decision CH62 rule 7):
//! 1. Each line is `NAME=value`, ended by LF. At least one line.
//! 2. NAME matches `[A-Za-z_][A-Za-z0-9_]*`. The first `=` ends it.
//! 3. The value is any bytes but LF and NUL, and may be empty. A CR is a
//!    value byte, because the script refuses only a line break. No
//!    comment, no quoting.
//! 4. The names are in strictly ascending byte order, so no name is
//!    listed twice.
//!
//! One rule goes beyond the script: a cap of 64 KiB on the file. A NUL
//! is not a rule beyond it: an environment value cannot hold one, so the
//! script cannot write one. A unit test runs the real script, so the two
//! cannot drift. The reader is pure apart from `read`, so the tests run
//! on any OS (decision CH22 rule 1: strict readers).

use std::fmt;
use std::io::{self, Read as _};
use std::path::Path;

/// The env file of the switched root (decision CH7-7). Only PID 1 on
/// Linux reads it.
#[cfg(target_os = "linux")]
pub const PATH: &str = "/etc/creche/env";

/// The cap on the file in bytes (decision CH62 rule 7). A file of
/// exactly `CAP` bytes passes.
pub const CAP: usize = 64 * 1024;

/// The parsed file: each name with its value, in the order of the file.
/// It becomes the env of the facade (decision CH49 rule 1).
#[derive(Debug, PartialEq, Eq)]
pub struct Env {
    vars: Vec<(String, Vec<u8>)>,
}

impl Env {
    /// The number of names. A parsed file holds at least one.
    pub fn len(&self) -> usize {
        self.vars.len()
    }
}

/// Why the reader refused the file. Its text goes into the console line
/// of the fault (decision CH62 rules 7 and 26).
#[derive(Debug)]
pub enum Refusal {
    Read(io::Error),
    OverCap,
    Empty,
    Line { number: usize, problem: Problem },
}

/// What is wrong with one line.
#[derive(Debug, PartialEq, Eq)]
pub enum Problem {
    /// The last line has no LF.
    NoLineEnd,
    NoEquals,
    /// The name, lossy, is not a variable name.
    BadName(String),
    /// The value holds a NUL.
    Nul(String),
    Twice(String),
    /// The name sorts before the name of the line above it.
    Unsorted {
        name: String,
        above: String,
    },
}

impl fmt::Display for Refusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(error) => write!(formatter, "the file is unreadable: {error}"),
            Self::OverCap => write!(formatter, "the file is over {CAP} bytes"),
            Self::Empty => write!(formatter, "the file is empty"),
            Self::Line { number, problem } => write!(formatter, "line {number}: {problem}"),
        }
    }
}

impl fmt::Display for Problem {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoLineEnd => write!(formatter, "no LF at the end"),
            Self::NoEquals => write!(formatter, "no ="),
            Self::BadName(name) => write!(formatter, "{name:?} is not a variable name"),
            Self::Nul(name) => write!(formatter, "the value of {name} holds a NUL"),
            Self::Twice(name) => write!(formatter, "{name} is listed twice"),
            Self::Unsorted { name, above } => {
                write!(formatter, "{name} sorts before {above}")
            }
        }
    }
}

/// Reads and parses the env file at `path`. It reads at most one byte
/// past the cap.
///
/// # Errors
///
/// A file that cannot be read, or a refusal of `parse`.
pub fn read(path: &Path) -> Result<Env, Refusal> {
    // One byte past the cap is enough for `parse` to refuse the file.
    let limit = u64::try_from(CAP + 1).unwrap_or(u64::MAX);
    let mut bytes = Vec::new();
    std::fs::File::open(path)
        .and_then(|file| file.take(limit).read_to_end(&mut bytes))
        .map_err(Refusal::Read)?;
    parse(&bytes)
}

/// Parses the bytes of an env file.
///
/// # Errors
///
/// The first rule of the grammar that the bytes break.
pub fn parse(bytes: &[u8]) -> Result<Env, Refusal> {
    if bytes.len() > CAP {
        return Err(Refusal::OverCap);
    }
    let Some(body) = bytes.strip_suffix(b"\n") else {
        return Err(if bytes.is_empty() {
            Refusal::Empty
        } else {
            Refusal::Line {
                number: bytes.split(|byte| *byte == b'\n').count(),
                problem: Problem::NoLineEnd,
            }
        });
    };
    let mut vars: Vec<(String, Vec<u8>)> = Vec::new();
    for (index, line) in body.split(|byte| *byte == b'\n').enumerate() {
        let (name, value) = parse_line(line, vars.last().map(|(above, _)| above.as_str()))
            .map_err(|problem| Refusal::Line {
                number: index + 1,
                problem,
            })?;
        vars.push((name, value.to_vec()));
    }
    Ok(Env { vars })
}

/// Parses one line without its LF. `above` is the name of the line above.
fn parse_line<'line>(
    line: &'line [u8],
    above: Option<&str>,
) -> Result<(String, &'line [u8]), Problem> {
    let equals = line
        .iter()
        .position(|byte| *byte == b'=')
        .ok_or(Problem::NoEquals)?;
    let (name, value) = line.split_at(equals);
    let value = value.get(1..).unwrap_or_default();
    let valid = match name.split_first() {
        Some((first, rest)) => {
            (first.is_ascii_alphabetic() || *first == b'_')
                && rest
                    .iter()
                    .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
        }
        None => false,
    };
    if !valid {
        return Err(Problem::BadName(String::from_utf8_lossy(name).into_owned()));
    }
    // Each byte is ASCII, so each byte is one char.
    let name: String = name.iter().copied().map(char::from).collect();
    if value.contains(&0) {
        return Err(Problem::Nul(name));
    }
    match above {
        Some(above) if name.as_str() == above => Err(Problem::Twice(name)),
        Some(above) if name.as_str() < above => Err(Problem::Unsorted {
            name,
            above: above.to_owned(),
        }),
        _ => Ok((name, value)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::process::Command;

    fn vars(pairs: &[(&str, &[u8])]) -> Env {
        Env {
            vars: pairs
                .iter()
                .map(|(name, value)| ((*name).to_owned(), value.to_vec()))
                .collect(),
        }
    }

    fn problem(bytes: &[u8]) -> (usize, Problem) {
        match parse(bytes) {
            Err(Refusal::Line { number, problem }) => (number, problem),
            other => panic!("no line refusal: {other:?}"),
        }
    }

    /// The real `playpen/bin/creche-env-write`, with no env but the
    /// listed names, an unsorted name list and a PATH with repeats. Every
    /// file it writes must parse to the same names and values.
    #[test]
    fn parses_the_output_of_creche_env_write() {
        let script =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../playpen/bin/creche-env-write");
        let output = Command::new(script)
            .env_clear()
            .env("PATH", "/usr/bin:/bin:/usr/bin:/usr/sbin:/bin")
            .env("lower", "l")
            .env("Z_LAST", "z")
            .env("A1", "after A")
            .env("A", "x=y")
            .env("EMPTY", "")
            .env("CR", "a\rb\r")
            .env("_U", " spaced ")
            .args(["PATH", "lower", "Z_LAST", "A1", "A", "EMPTY", "CR", "_U"])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "creche-env-write failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            parse(&output.stdout).unwrap(),
            vars(&[
                ("A", b"x=y"),
                ("A1", b"after A"),
                ("CR", b"a\rb\r"),
                ("EMPTY", b""),
                ("PATH", b"/usr/bin:/bin:/usr/sbin"),
                ("Z_LAST", b"z"),
                ("_U", b" spaced "),
                ("lower", b"l"),
            ])
        );
    }

    #[test]
    fn refuses_a_name_listed_twice() {
        assert_eq!(problem(b"A=1\nA=2\n"), (2, Problem::Twice("A".to_owned())));
    }

    #[test]
    fn refuses_an_unsorted_file() {
        assert_eq!(
            problem(b"A=1\nC=3\nB=2\n"),
            (
                3,
                Problem::Unsorted {
                    name: "B".to_owned(),
                    above: "C".to_owned()
                }
            )
        );
    }

    #[test]
    fn keeps_a_cr_in_a_value() {
        assert_eq!(parse(b"A=1\r\n").unwrap(), vars(&[("A", b"1\r")]));
    }

    #[test]
    fn refuses_a_line_without_an_equals_sign() {
        assert_eq!(problem(b"A=1\nB\n"), (2, Problem::NoEquals));
    }

    #[test]
    fn refuses_a_bad_name() {
        for name in ["", "1A", "A-B", "A B", "A\r"] {
            let line = format!("{name}=1\n");
            assert_eq!(
                problem(line.as_bytes()),
                (1, Problem::BadName(name.to_owned()))
            );
        }
    }

    #[test]
    fn keeps_an_empty_value() {
        assert_eq!(parse(b"A=\n").unwrap(), vars(&[("A", b"")]));
    }

    #[test]
    fn refuses_an_empty_file() {
        assert!(matches!(parse(b""), Err(Refusal::Empty)));
    }

    #[test]
    fn refuses_a_last_line_with_no_lf() {
        assert_eq!(problem(b"A=1\nB=2"), (2, Problem::NoLineEnd));
        assert_eq!(problem(b"\n\nA=1"), (3, Problem::NoLineEnd));
    }

    #[test]
    fn refuses_an_empty_line() {
        assert_eq!(problem(b"A=1\n\n"), (2, Problem::NoEquals));
    }

    #[test]
    fn refuses_a_nul_in_a_value() {
        assert_eq!(problem(b"A=1\0\n"), (1, Problem::Nul("A".to_owned())));
    }

    #[test]
    fn refuses_a_file_over_the_cap() {
        let line = |len: usize| {
            let mut bytes = b"A=".to_vec();
            bytes.resize(len - 1, b'x');
            bytes.push(b'\n');
            bytes
        };
        assert_eq!(parse(&line(CAP)).unwrap().len(), 1);
        assert!(matches!(parse(&line(CAP + 1)), Err(Refusal::OverCap)));
    }

    /// A file over the cap whose first `CAP` bytes end on an LF. `read`
    /// must read past the cap, or it would cut the file at a line end and
    /// drop `B` with no refusal (CH62 rule 7).
    #[test]
    fn read_refuses_a_file_over_the_cap() {
        let mut bytes = b"A=".to_vec();
        bytes.resize(CAP - 1, b'x');
        bytes.extend_from_slice(b"\nB=2\n");
        assert_eq!(bytes.len(), CAP + 4);
        assert_eq!(bytes.get(CAP - 1), Some(&b'\n'));
        let path = std::env::temp_dir().join(format!(
            "creche-guest-init-env-over-cap-{}",
            std::process::id()
        ));
        std::fs::write(&path, &bytes).unwrap();
        let result = read(&path);
        std::fs::remove_file(&path).unwrap();
        assert!(matches!(result, Err(Refusal::OverCap)), "{result:?}");
    }

    #[test]
    fn refuses_a_missing_file() {
        let refusal = read(Path::new("/nonexistent/creche/env")).unwrap_err();
        assert!(matches!(refusal, Refusal::Read(_)), "{refusal:?}");
    }

    #[test]
    fn a_refusal_line_names_the_line() {
        let refusal = parse(b"A=1\nA=2\n").unwrap_err();
        assert_eq!(refusal.to_string(), "line 2: A is listed twice");
    }
}
