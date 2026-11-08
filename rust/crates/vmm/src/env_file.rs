//! The reader of the env file of the vmm unit, for the two verify hooks
//! `vmm verify` and `vmm check-images` (decisions CH76 rules 2 and 6).
//!
//! handover gives a hook a small fixed env, never the env of its unit
//! (contract 06 §4 rule 7). The hook reads the file of the unit's
//! `EnvironmentFile=` itself, as its own user: one `KEY=value` per line,
//! no shell, no quoting, no expansion. `bin/fleet-up up` writes
//! `vmm.env` with `envfile_upsert`, after a `#` header line, in the order
//! of `VMM_KEYS` (decision CH59 rule 1). The guest init reader
//! (`creche-guest-init`, `env_file.rs`) refuses both, and the vmm does not
//! depend on that crate. So the vmm holds its own reader.
//!
//! The grammar:
//! 1. Each line ends with LF. The file has at most `CAP` bytes.
//! 2. A line whose first byte is `#` is a comment, such as the header of
//!    fleet-up.
//! 3. Each other line is `NAME=value`. NAME matches
//!    `[A-Za-z_][A-Za-z0-9_]*`, and the first `=` ends it. So an empty
//!    line refuses.
//! 4. The value is read as it stands. systemd reads a quote, a backslash
//!    or a space at either end in its own way, so a value that starts with
//!    `"` or `'`, holds a `\`, starts or ends with a space or a tab, or
//!    holds a CR or a NUL refuses. Then the hook and the unit read one
//!    value.
//! 5. A name comes once, in any order.
//!
//! A refusal names the line and the name, never the value, as in
//! `env/AGENTS.md`.
//!
//! CONTRACT-QUESTION (decision CH76 rules 2 and 6, `AGENTS.md` Known gap
//! 32): `creche-vmm.service` reads `vmm.env` and then the site file, and a
//! site value wins. A hook takes one `--env-file`, the `vmm.env` of gap
//! Q68-2 option A. That is the narrowest reading of contract 06 §4 rule 7.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fmt;
use std::fs::File;
use std::io::{self, Read as _};
use std::os::unix::ffi::OsStringExt as _;
use std::path::Path;

/// The cap on the file in bytes. `vmm.env` holds about 20 lines of paths
/// and sizes, less than 4 KiB. No row sets it.
pub const CAP: u64 = 64 * 1024;

/// The bytes that systemd reads in its own way in a value (rule 4).
const QUOTES: [u8; 2] = [b'"', b'\''];
const SPACES: [u8; 2] = [b' ', b'\t'];

/// The parsed file: each name with its value.
#[derive(Debug, PartialEq, Eq)]
pub struct EnvFile {
    vars: BTreeMap<String, OsString>,
}

impl EnvFile {
    /// The value of `key`, as the env of the unit holds it. The configs of
    /// `crate::config` take this lookup.
    #[must_use]
    pub fn lookup(&self, key: &str) -> Option<OsString> {
        self.vars.get(key).cloned()
    }
}

/// Why the reader refused the file.
#[derive(Debug)]
pub enum EnvFileError {
    /// The file cannot be opened or read.
    Read(io::Error),
    /// The file has more than `CAP` bytes.
    OverCap,
    /// One line breaks the grammar.
    Line {
        /// The line number, from 1.
        number: usize,
        /// What is wrong with the line.
        problem: Problem,
    },
}

/// What is wrong with one line.
#[derive(Debug, PartialEq, Eq)]
pub enum Problem {
    /// The last line has no LF.
    NoLineEnd,
    /// The line has no `=`.
    NoEquals,
    /// The name is not a variable name.
    BadName,
    /// The value of the name holds a byte of rule 4.
    Unread(String),
    /// The name comes twice.
    Twice(String),
}

impl fmt::Display for EnvFileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(error) => write!(formatter, "the env file cannot be read: {error}"),
            Self::OverCap => write!(formatter, "the env file has more than {CAP} bytes"),
            Self::Line { number, problem } => {
                write!(formatter, "the env file, line {number}: {problem}")
            }
        }
    }
}

impl fmt::Display for Problem {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoLineEnd => formatter.write_str("no LF at the end"),
            Self::NoEquals => formatter.write_str("no `=`"),
            Self::BadName => formatter.write_str("the name is not a variable name"),
            Self::Unread(name) => write!(
                formatter,
                "the value of {name} holds a quote, a backslash, a CR, a NUL or a space at an end"
            ),
            Self::Twice(name) => write!(formatter, "{name} comes twice"),
        }
    }
}

impl std::error::Error for EnvFileError {}

/// Reads and parses the env file at `path`. It reads at most one byte past
/// the cap. Blocks.
///
/// # Errors
///
/// A file that cannot be read, or one that breaks the grammar.
pub fn read(path: &Path) -> Result<EnvFile, EnvFileError> {
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(CAP + 1).read_to_end(&mut bytes))
        .map_err(EnvFileError::Read)?;
    if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > CAP {
        return Err(EnvFileError::OverCap);
    }
    parse(&bytes)
}

/// Parses the bytes of the file with the grammar of the module doc.
fn parse(bytes: &[u8]) -> Result<EnvFile, EnvFileError> {
    let mut vars = BTreeMap::new();
    let mut rest = bytes;
    let mut number = 0_usize;
    while !rest.is_empty() {
        number = number.saturating_add(1);
        let refuse = |problem| EnvFileError::Line { number, problem };
        let Some(end) = rest.iter().position(|&byte| byte == b'\n') else {
            return Err(refuse(Problem::NoLineEnd));
        };
        let (line, after) = rest.split_at(end);
        rest = after.get(1..).unwrap_or_default();
        if line.first() == Some(&b'#') {
            continue;
        }
        let Some(equals) = line.iter().position(|&byte| byte == b'=') else {
            return Err(refuse(Problem::NoEquals));
        };
        let (name, value) = line.split_at(equals);
        let value = value.get(1..).unwrap_or_default();
        let name = variable_name(name).ok_or_else(|| refuse(Problem::BadName))?;
        if !plain(value) {
            return Err(refuse(Problem::Unread(name)));
        }
        if vars.contains_key(&name) {
            return Err(refuse(Problem::Twice(name)));
        }
        vars.insert(name, OsString::from_vec(value.to_vec()));
    }
    Ok(EnvFile { vars })
}

/// The name, when it matches `[A-Za-z_][A-Za-z0-9_]*`.
fn variable_name(name: &[u8]) -> Option<String> {
    let (&first, tail) = name.split_first()?;
    let good = (first.is_ascii_alphabetic() || first == b'_')
        && tail
            .iter()
            .all(|&byte| byte.is_ascii_alphanumeric() || byte == b'_');
    good.then(|| String::from_utf8_lossy(name).into_owned())
}

/// Whether systemd reads `value` as it stands (rule 4).
fn plain(value: &[u8]) -> bool {
    let quoted = value.first().is_some_and(|byte| QUOTES.contains(byte));
    let padded = value.first().is_some_and(|byte| SPACES.contains(byte))
        || value.last().is_some_and(|byte| SPACES.contains(byte));
    let odd = value
        .iter()
        .any(|&byte| matches!(byte, b'\\' | b'\r' | b'\0'));
    !(quoted || padded || odd)
}
