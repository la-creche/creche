//! The reader of the env files: `default.env`, `example.env` and a `.env`.
//!
//! A product ships a pair of files. `default.env` gives each key its
//! default, and `example.env` names each key that has no default. The host
//! gives one `.env`. It overrides a default, key by key, and it sets each
//! required key. `env/AGENTS.md` holds the grammar, the order of the layers
//! and the cause of each fault.
//!
//! The Python origin is `creche_env.envfile` (`env/src/creche_env/envfile.py`).
//! The vectors of the surfaces `envfile.layer`, `envfile.raw` and
//! `envfile.trust` hold the two readers equal.
//!
//! | Item | What it is |
//! |---|---|
//! | [`parse`] | The assignments of one file of a [`FileKind`]. |
//! | [`read_pair`], [`Pair::product`] | The catalog of a pair. `product` reads the pair of the product, which this crate embeds. |
//! | [`load`] | The layers of a pair and a `.env`: the [`Settings`]. |
//! | [`load_environment`] | The layers with the environment of the process in the place of a `.env`. |
//! | [`check_environment`] | The rule for a catalog key in the environment of a program of root. |
//! | [`raw`] | One file in the grammar-only mode: no catalog and no reference. |
//! | [`check_trust`] | The owner and the mode of a `.env`. |
//! | [`Settings::to_env`] | The [`Env`] of the settings, for the typed config of a program. |
//!
//! Each check stops at its first fault. The check of the required keys and
//! the check of the environment name each key that fails. A fault never
//! holds a value. No item here reads a file, the environment or the
//! password database: the caller gives the bytes, the variables and the
//! lookup of a home. `creche_runtime::envfile::read_site` reads the `.env`
//! of the host.

use std::borrow::Borrow;
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::{Env, EnvValue};
use crate::ids::EnvName;

/// The most bytes of one file, and of one value after its references.
pub const MAX_BYTES: usize = 64 * 1024;

/// The most references in one chain. `A=${B}` is a chain of one.
pub const MAX_DEPTH: usize = 8;

/// The `.env` of the product on a host.
pub const SITE_FILE: &str = "/etc/creche/site.env";

/// The name that a fault gives the `default.env` of the product.
pub const DEFAULT_FILE: &str = "default.env";

/// The name that a fault gives the `example.env` of the product.
pub const EXAMPLE_FILE: &str = "example.env";

/// The name that a fault gives the environment of the process. Such a
/// fault names line 0.
pub const ENVIRONMENT: &str = "environment";

/// The pair of the product. The Python package `creche-env` holds the two
/// files as package data, and this crate embeds the same bytes. A binary
/// that calls [`Pair::product`] thus bundles `env` (`rust/AGENTS.md`, "The
/// env files").
const PRODUCT_DEFAULTS: &str = include_str!("../../../../../env/src/creche_env/default.env");
const PRODUCT_EXAMPLE: &str = include_str!("../../../../../env/src/creche_env/example.env");

/// The write bits of the group and of each other account.
const OTHERS_WRITE_BITS: u32 = 0o022;

const COMMENT: char = '#';
const LINE_END: char = '\n';
const SPACE: char = ' ';
const REFERENCE_SIGN: char = '$';

// --- the key and the faults ---

/// A key of an env file: an [`EnvName`], the grammar of the name of a
/// variable. `ids` holds that one grammar.
///
/// ```
/// use creche_contracts::{config::envfile::Key, ids::EnvName};
///
/// let key: Key = "AGENT_LAN_ADDRESS".parse()?;
/// let name: EnvName = "AGENT_LAN_ADDRESS".parse().unwrap();
/// assert_eq!(key.as_str(), name.as_str());
/// assert!("agent".parse::<Key>().is_err());
/// # Ok::<(), creche_contracts::config::envfile::KeyError>(())
/// ```
///
/// Code outside this module cannot build a key from a name:
///
/// ```compile_fail,E0423
/// use creche_contracts::{config::envfile::Key, ids::EnvName};
///
/// let name: EnvName = "AGENT_LAN_ADDRESS".parse().unwrap();
/// let key = Key(name);
/// ```
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Key(EnvName);

impl Key {
    /// The key as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        self.0.as_str()
    }
}

impl FromStr for Key {
    type Err = KeyError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        text.parse().map(Self).map_err(|_| KeyError)
    }
}

impl Borrow<str> for Key {
    fn borrow(&self) -> &str {
        self.0.as_str()
    }
}

impl fmt::Display for Key {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0.as_str())
    }
}

/// Why a text is not a [`Key`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KeyError;

impl fmt::Display for KeyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "a key has the form {}", EnvName::form())
    }
}

impl Error for KeyError {}

/// Each fault of the reader. [`Fault::word`] gives the name of the Python
/// enum.
///
/// The set is closed. A program writes the name of a fault, and no reader
/// reads one, so the set has no variant for an unknown name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    /// A file, or a value after its references, has more than [`MAX_BYTES`].
    TooLarge,
    /// A file, or a value of the environment, is not UTF-8.
    NotUtf8,
    /// A file starts with a byte order mark.
    ByteOrderMark,
    /// A line holds a CR.
    CarriageReturn,
    /// The last byte of a file that has bytes is not an LF.
    NoFinalNewline,
    /// A line is no comment, no empty line and no assignment.
    NotKeyValue,
    /// A key is not in the grammar.
    BadKey,
    /// A value is not in the grammar, or a home is no value of a `.env`.
    BadValue,
    /// A value is empty where the file needs a value.
    EmptyValue,
    /// A key is on two lines, or in the two files of the pair.
    DuplicateKey,
    /// The `.env` sets a key outside the catalog.
    UnknownKey,
    /// A value outside `default.env` holds `$`.
    ReferenceInEnv,
    /// A reference names a key outside the catalog.
    UnknownReference,
    /// A reference names a key with no value.
    UnsetReference,
    /// A chain of references comes back to a key of the chain.
    ReferenceCycle,
    /// A chain has more than [`MAX_DEPTH`] references.
    ReferenceTooDeep,
    /// The lookup of the homes has no account of that name.
    UnknownAccount,
    /// The `.env` does not set a required key.
    RequiredUnset,
    /// A catalog key of the environment has another value than the files.
    EnvironmentDiffers,
    /// The owner of the `.env` is not root and not the reader.
    WrongOwner,
    /// The group or each other account can write the `.env`.
    OthersWrite,
}

impl Fault {
    /// The name of the fault, as the Python reader writes it.
    #[must_use]
    pub const fn word(self) -> &'static str {
        match self {
            Self::TooLarge => "too-large",
            Self::NotUtf8 => "not-utf8",
            Self::ByteOrderMark => "byte-order-mark",
            Self::CarriageReturn => "carriage-return",
            Self::NoFinalNewline => "no-final-newline",
            Self::NotKeyValue => "not-key-value",
            Self::BadKey => "bad-key",
            Self::BadValue => "bad-value",
            Self::EmptyValue => "empty-value",
            Self::DuplicateKey => "duplicate-key",
            Self::UnknownKey => "unknown-key",
            Self::ReferenceInEnv => "reference-in-env",
            Self::UnknownReference => "unknown-reference",
            Self::UnsetReference => "unset-reference",
            Self::ReferenceCycle => "reference-cycle",
            Self::ReferenceTooDeep => "reference-too-deep",
            Self::UnknownAccount => "unknown-account",
            Self::RequiredUnset => "required-unset",
            Self::EnvironmentDiffers => "environment-differs",
            Self::WrongOwner => "wrong-owner",
            Self::OthersWrite => "others-write",
        }
    }
}

impl fmt::Display for Fault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.word())
    }
}

/// One fault: the file, the line and the key. Line 0 is a fault of the
/// whole file. A fault holds no value.
///
/// ```
/// use creche_contracts::config::envfile::{Fault, FileKind, Text, parse};
///
/// let error = parse(&Text::new("site.env", "A=1\nlower=2\n"), FileKind::Env).unwrap_err();
/// let [problem] = error.problems() else { unreachable!() };
/// assert_eq!(problem.fault(), Fault::BadKey);
/// assert_eq!((problem.file(), problem.line(), problem.key()), ("site.env", 2, None));
/// assert_eq!(problem.to_string(), "site.env:2: bad-key");
/// ```
///
/// Code outside this module cannot build a fault:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::envfile::{Fault, Problem};
///
/// let problem = Problem { fault: Fault::BadKey, file: String::new(), line: 0, key: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Problem {
    fault: Fault,
    file: String,
    line: usize,
    key: Option<Key>,
}

impl Problem {
    fn new(fault: Fault, file: &str, line: usize, key: Option<&Key>) -> Self {
        let file = file.to_owned();

        Self {
            fault,
            file,
            line,
            key: key.cloned(),
        }
    }

    /// The fault.
    #[must_use]
    pub const fn fault(&self) -> Fault {
        self.fault
    }

    /// The name of the file, or [`ENVIRONMENT`].
    #[must_use]
    pub fn file(&self) -> &str {
        &self.file
    }

    /// The number of the line, from 1. Line 0 is the whole file.
    #[must_use]
    pub const fn line(&self) -> usize {
        self.line
    }

    /// The key. `None` for a fault that names no key, and for a bad key.
    #[must_use]
    pub const fn key(&self) -> Option<&Key> {
        self.key.as_ref()
    }
}

impl fmt::Display for Problem {
    /// One line, for example `site.env:3: unknown-key AGENT_X`.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.file)?;
        if self.line > 0 {
            write!(f, ":{}", self.line)?;
        }

        write!(f, ": {}", self.fault)?;
        match &self.key {
            Some(key) => write!(f, " {key}"),
            None => Ok(()),
        }
    }
}

/// The files give no settings: one fault or more.
///
/// ```
/// use creche_contracts::config::envfile::{EnvError, Text, read_pair};
///
/// let (default, example) = (Text::new("default.env", "A=1\nA=2\n"), Text::new("example.env", ""));
/// let error: EnvError = read_pair(&default, &example).unwrap_err();
/// assert_eq!(error.to_string(), "default.env:2: duplicate-key A");
/// ```
///
/// Code outside this module cannot build an error with no fault:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::envfile::EnvError;
///
/// let error = EnvError(Vec::new());
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvError(Vec<Problem>);

impl EnvError {
    /// The error of one fault.
    fn at(fault: Fault, file: &str, line: usize, key: Option<&Key>) -> Self {
        Self(vec![Problem::new(fault, file, line, key)])
    }

    /// Each fault, in the order of the reader. The slice is never empty.
    #[must_use]
    pub fn problems(&self) -> &[Problem] {
        &self.0
    }
}

impl fmt::Display for EnvError {
    /// Each fault, with `; ` between two of them.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (at, problem) in self.0.iter().enumerate() {
            let between = if at > 0 { "; " } else { "" };
            write!(f, "{between}{problem}")?;
        }

        Ok(())
    }
}

impl Error for EnvError {}

// --- the grammar ---

/// The bytes of one file, and the name that a fault gives the file.
///
/// `Debug` prints the name only: a file in the grammar-only mode can hold a
/// secret.
///
/// ```
/// use creche_contracts::config::envfile::Text;
///
/// let text = Text::new(".env", "A=1\n");
/// assert_eq!((text.name(), text.data()), (".env", b"A=1\n".as_slice()));
/// assert_eq!(format!("{text:?}"), r#"Text { name: ".env", .. }"#);
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::envfile::Text;
///
/// let text = Text { name: String::new(), data: Vec::new() };
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Text {
    name: String,
    data: Vec<u8>,
}

impl Text {
    /// The text of the file `name`, with the bytes `data`.
    pub fn new(name: impl Into<String>, data: impl Into<Vec<u8>>) -> Self {
        Self {
            name: name.into(),
            data: data.into(),
        }
    }

    /// The name that a fault gives the file.
    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    /// The bytes of the file.
    #[must_use]
    pub fn data(&self) -> &[u8] {
        &self.data
    }
}

impl fmt::Debug for Text {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Text")
            .field("name", &self.name)
            .finish_non_exhaustive()
    }
}

/// The three files, each with its rules for a value.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileKind {
    /// `default.env`. A value can be empty, and it can hold a reference.
    Default,
    /// `example.env`. Each line is a comment. `#KEY=VALUE` declares a
    /// required key, with a value that has the rules of a `.env`.
    Example,
    /// A `.env`. A value is not empty and holds no `$`.
    Env,
}

/// One assignment of a file: the key, the value and the number of its line.
/// Only the readers of this module make one.
///
/// `Debug` prints the key and the number: a file in the grammar-only mode
/// can hold a secret.
///
/// ```
/// use creche_contracts::config::envfile::{FileKind, Text, parse};
///
/// let lines = parse(&Text::new(".env", "# a\nA=1\n"), FileKind::Env)?;
/// let [line] = lines.as_slice() else { unreachable!() };
/// assert_eq!((line.key().as_str(), line.value(), line.number()), ("A", "1", 2));
/// # Ok::<(), creche_contracts::config::envfile::EnvError>(())
/// ```
///
/// Code outside this module cannot build a line:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::envfile::{Key, Line};
///
/// let key: Key = "A".parse().unwrap();
/// let line = Line { key, value: String::new(), number: 1 };
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Line {
    key: Key,
    value: String,
    number: usize,
}

impl Line {
    /// The key.
    #[must_use]
    pub const fn key(&self) -> &Key {
        &self.key
    }

    /// The value, as the file writes it.
    #[must_use]
    pub fn value(&self) -> &str {
        &self.value
    }

    /// The number of the line, from 1.
    #[must_use]
    pub const fn number(&self) -> usize {
        self.number
    }
}

impl fmt::Debug for Line {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut line = f.debug_struct("Line");

        line.field("key", &self.key)
            .field("number", &self.number)
            .finish_non_exhaustive()
    }
}

/// A fault of one line and its key. [`parse`] adds the file and the line.
type LineFault = (Fault, Option<Key>);

/// The lines of one file, each without its LF, after the checks of the
/// whole file.
fn lines_of(source: &Text) -> Result<Vec<&str>, EnvError> {
    let whole = |fault| EnvError::at(fault, source.name(), 0, None);
    if source.data().len() > MAX_BYTES {
        return Err(whole(Fault::TooLarge));
    }

    let text = std::str::from_utf8(source.data()).map_err(|_| whole(Fault::NotUtf8))?;
    if text.starts_with('\u{feff}') {
        return Err(whole(Fault::ByteOrderMark));
    }

    if let Some((before, _)) = text.split_once('\r') {
        let line = before.matches(LINE_END).count().saturating_add(1);

        return Err(EnvError::at(
            Fault::CarriageReturn,
            source.name(),
            line,
            None,
        ));
    }

    if !text.is_empty() && !text.ends_with(LINE_END) {
        return Err(whole(Fault::NoFinalNewline));
    }

    let mut lines: Vec<&str> = text.split(LINE_END).collect();
    lines.pop();

    Ok(lines)
}

/// Each assignment of one file, in the order of its lines. For
/// `example.env`, each declared required key.
///
/// # Errors
///
/// [`EnvError`] with the first fault of the file.
pub fn parse(source: &Text, kind: FileKind) -> Result<Vec<Line>, EnvError> {
    let mut found = Vec::new();
    let mut seen = BTreeSet::new();
    for (number, text) in (1..).zip(lines_of(source)?) {
        let at = |(fault, key): LineFault| EnvError::at(fault, source.name(), number, key.as_ref());
        let Some(line) = line_of(text, number, kind).map_err(at)? else {
            continue;
        };

        if !seen.insert(line.key.clone()) {
            return Err(at((Fault::DuplicateKey, Some(line.key))));
        }

        found.push(line);
    }

    Ok(found)
}

/// The assignment of one line. `None` for an empty line and for a comment.
fn line_of(text: &str, number: usize, kind: FileKind) -> Result<Option<Line>, LineFault> {
    let Some(body) = text.strip_prefix(COMMENT) else {
        return match (text.is_empty(), kind) {
            (true, _) => Ok(None),
            // A copy of `example.env` sets nothing: each line is a comment.
            (false, FileKind::Example) => Err((Fault::NotKeyValue, None)),
            (false, FileKind::Default | FileKind::Env) => assignment(text, number, kind).map(Some),
        };
    };

    if kind == FileKind::Example && !body.is_empty() && !body.starts_with(SPACE) {
        // `#text` with no space declares a key, so it must be an
        // assignment. The owner confirmed this reading of the Python reader.
        return assignment(body, number, FileKind::Env).map(Some);
    }

    // A comment holds no backslash and no control character, U+0000 to
    // U+001F and U+007F to U+009F: the rule of the Python reader, which the
    // owner confirmed. An older systemd reads a comment that ends with a
    // backslash as one comment with the next line.
    if body.chars().any(|one| one.is_control() || one == '\\') {
        return Err((Fault::NotKeyValue, None));
    }

    Ok(None)
}

fn assignment(text: &str, number: usize, kind: FileKind) -> Result<Line, LineFault> {
    let (key, value) = text.split_once('=').ok_or((Fault::NotKeyValue, None))?;
    let key: Key = key.parse().map_err(|_| (Fault::BadKey, None))?;
    if let Err(fault) = check_value(value, kind) {
        return Err((fault, Some(key)));
    }

    Ok(Line {
        key,
        value: value.to_owned(),
        number,
    })
}

fn check_value(value: &str, kind: FileKind) -> Result<(), Fault> {
    if !in_value_grammar(value) {
        return Err(Fault::BadValue);
    }

    match (value.is_empty(), value.contains(REFERENCE_SIGN), kind) {
        (true, _, FileKind::Default) | (false, false, _) => Ok(()),
        (true, _, FileKind::Example | FileKind::Env) => Err(Fault::EmptyValue),
        (false, true, FileKind::Example | FileKind::Env) => Err(Fault::ReferenceInEnv),
        (false, true, FileKind::Default) => parts_of(value).map(|_| ()).ok_or(Fault::BadValue),
    }
}

/// Whether each character of `value` is printable ASCII or the space, but
/// `"`, `#`, `'` and the backslash, and no space is at its two ends.
fn in_value_grammar(value: &str) -> bool {
    let character = |one: char| matches!(one, ' ' | '!' | '$'..='&' | '('..='[' | ']'..='~');

    value.chars().all(character) && !value.starts_with(SPACE) && !value.ends_with(SPACE)
}

/// Whether `value` is a value that a `.env` can hold.
fn is_plain(value: &str) -> bool {
    !value.is_empty() && in_value_grammar(value) && !value.contains(REFERENCE_SIGN)
}

/// One part of a value of `default.env`.
enum Part<'a> {
    /// Text with no reference.
    Text(&'a str),
    /// `${home:KEY}`: the home of the account in KEY.
    Home(Key),
    /// `${KEY}`: the value of KEY.
    Reference(Key),
}

/// The parts of a value of `default.env`. `None` when a `$` starts no
/// reference.
fn parts_of(value: &str) -> Option<Vec<Part<'_>>> {
    let mut parts = Vec::new();
    let mut rest = value;
    while let Some(at) = rest.find(REFERENCE_SIGN) {
        let (text, place) = rest.split_at_checked(at)?;
        let inner = place.strip_prefix("${")?;
        let (home, inner) = inner
            .strip_prefix("home:")
            .map_or((false, inner), |after| (true, after));
        let (name, after) = inner.split_once('}')?;
        let key: Key = name.parse().ok()?;

        parts.push(Part::Text(text));
        parts.push(if home {
            Part::Home(key)
        } else {
            Part::Reference(key)
        });
        rest = after;
    }

    parts.push(Part::Text(rest));

    Some(parts)
}

// --- the pair ---

/// The catalog: the defaults of `default.env` and the required keys of
/// `example.env`, with the names of the two files. Only [`read_pair`] and
/// [`Pair::product`] make one.
///
/// ```
/// use creche_contracts::config::envfile::{Text, read_pair};
///
/// let pair = read_pair(&Text::new("default.env", "A=1\n"), &Text::new("example.env", "#B=x\n"))?;
/// let catalog: Vec<&str> = pair.catalog().map(|key| key.as_str()).collect();
/// assert_eq!(catalog, ["A", "B"]);
/// # Ok::<(), creche_contracts::config::envfile::EnvError>(())
/// ```
///
/// Code outside this module cannot build a pair:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::envfile::Pair;
///
/// let pair = Pair {
///     defaults: Vec::new(),
///     required: Vec::new(),
///     default_file: String::new(),
///     example_file: String::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pair {
    defaults: Vec<Line>,
    required: Vec<Line>,
    default_file: String,
    example_file: String,
}

impl Pair {
    /// The pair of the product: `env/src/creche_env/default.env` and
    /// `example.env`, which this crate embeds at its build.
    ///
    /// # Errors
    ///
    /// [`EnvError`] when the embedded pair breaks a rule. A test of this
    /// module holds that it does not.
    pub fn product() -> Result<Self, EnvError> {
        let defaults = Text::new(DEFAULT_FILE, PRODUCT_DEFAULTS);

        read_pair(&defaults, &Text::new(EXAMPLE_FILE, PRODUCT_EXAMPLE))
    }

    /// Each assignment of `default.env`.
    #[must_use]
    pub fn defaults(&self) -> &[Line] {
        &self.defaults
    }

    /// Each declared required key of `example.env`.
    #[must_use]
    pub fn required(&self) -> &[Line] {
        &self.required
    }

    /// Each catalog key: the defaults, then the required keys.
    pub fn catalog(&self) -> impl Iterator<Item = &Key> {
        self.defaults.iter().chain(&self.required).map(Line::key)
    }
}

/// The catalog of one pair. A required key with a default is refused.
///
/// # Errors
///
/// [`EnvError`] with the first fault of the two files.
pub fn read_pair(default: &Text, example: &Text) -> Result<Pair, EnvError> {
    let defaults = parse(default, FileKind::Default)?;
    let required = parse(example, FileKind::Example)?;
    let known: BTreeSet<&Key> = defaults.iter().map(Line::key).collect();
    if let Some(twice) = required.iter().find(|one| known.contains(&one.key)) {
        let (number, key) = (twice.number, Some(&twice.key));

        return Err(EnvError::at(
            Fault::DuplicateKey,
            example.name(),
            number,
            key,
        ));
    }

    let (default_file, example_file) = (default.name().to_owned(), example.name().to_owned());

    Ok(Pair {
        defaults,
        required,
        default_file,
        example_file,
    })
}

// --- the layers ---

/// What the layers give: each catalog key that has a value, and the file
/// and the line of the text that each catalog key took. Only [`load`] and
/// [`load_environment`] make one.
///
/// `Debug` prints each key and no value: the environment of a process can
/// give a secret.
///
/// ```
/// use creche_contracts::config::envfile::{Text, load, read_pair};
///
/// let pair = read_pair(&Text::new("default.env", "A=1\nB=${A}/b\nC=\n"), &Text::new("example.env", ""))?;
/// let settings = load(&pair, &Text::new(".env", "A=2\n"), |_| None)?;
/// assert_eq!((settings.value("B"), settings.value("C")), (Some("2/b"), None));
/// assert_eq!(format!("{settings:?}"), r#"Settings {"A", "B"}"#);
/// # Ok::<(), creche_contracts::config::envfile::EnvError>(())
/// ```
///
/// Code outside this module cannot build the settings:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::envfile::Settings;
///
/// let settings = Settings { sources: Vec::new(), values: Default::default() };
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Settings {
    /// Each catalog key, in the order of the catalog, with its file and its
    /// line.
    sources: Vec<(Key, String, usize)>,
    values: BTreeMap<Key, String>,
}

impl Settings {
    /// The value of `key` after each layer. `None` for a key outside the
    /// catalog, and for an optional key with no value.
    #[must_use]
    pub fn value(&self, key: &str) -> Option<&str> {
        self.values.get(key).map(String::as_str)
    }

    /// Each catalog key that has a value, with that value, in the order of
    /// the keys.
    pub fn values(&self) -> impl Iterator<Item = (&Key, &str)> {
        self.values.iter().map(|(key, value)| (key, value.as_str()))
    }

    /// Each catalog key in the order of the catalog, with the file and the
    /// line of its text.
    pub fn sources(&self) -> impl Iterator<Item = (&Key, &str, usize)> {
        self.sources
            .iter()
            .map(|(key, file, line)| (key, file.as_str(), *line))
    }

    /// The variables of the settings, for the typed config of a program:
    /// each catalog key that has a value.
    #[must_use]
    pub fn to_env(&self) -> Env {
        Env::from_pairs(self.values().map(|(key, value)| (key.as_str(), value)))
    }
}

impl fmt::Debug for Settings {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Settings ")?;

        f.debug_set()
            .entries(self.values.keys().map(Key::as_str))
            .finish()
    }
}

/// The settings of one pair and one `.env`: each layer of `env/AGENTS.md`.
/// `homes` gives the home of an account, or `None` for an account that it
/// does not have.
///
/// The Python reader looks an account up in the password database of the
/// host when its caller gives no lookup. This function always takes the
/// lookup of its caller, because no item of this crate reads a file.
///
/// # Errors
///
/// [`EnvError`] with the first fault, or with each required key that the
/// `.env` does not set.
pub fn load(
    pair: &Pair,
    env: &Text,
    homes: impl Fn(&str) -> Option<String>,
) -> Result<Settings, EnvError> {
    let catalog: BTreeSet<&Key> = pair.catalog().collect();
    let lines = parse(env, FileKind::Env)?;
    if let Some(unknown) = lines.iter().find(|one| !catalog.contains(&one.key)) {
        let (number, key) = (unknown.number, Some(&unknown.key));

        return Err(EnvError::at(Fault::UnknownKey, env.name(), number, key));
    }

    layers(pair, &lines, env.name(), &homes)
}

/// The settings of one pair and the environment of the process, which takes
/// the place of a `.env`. A server that gets each value from its
/// environment uses this layer.
///
/// The function has no Python origin. The rules of a key and the rule of
/// the catalog apply to `environ`. The rules of a value of a `.env` do not:
/// the typed config of the program checks each value, and a secret passes
/// as it is. No layer reads a reference in such a value. The size rule of a
/// value applies: a value of the environment has [`MAX_BYTES`] at most, as
/// a value of `default.env` after its references. A longer value is
/// [`Fault::TooLarge`].
///
/// 1. A catalog key takes its value from `environ`. A value that is not
///    UTF-8 is [`Fault::NotUtf8`].
/// 2. `free` names each other variable that the environment can hold, for
///    example `PATH`. Such a variable is no input of the layers.
/// 3. Each other variable is [`Fault::BadKey`] when its name is no key, and
///    [`Fault::UnknownKey`] when it is one. This also applies to a variable
///    that the program does not read, for example `LANG`.
///
/// [`Env::from_os`] drops a variable whose name is not UTF-8. Such a
/// variable thus never reaches this function, and no fault names it.
///
/// A fault of the environment names [`ENVIRONMENT`] and line 0.
///
/// # Errors
///
/// [`EnvError`] with the first fault, or with each required key that the
/// environment does not set.
pub fn load_environment(
    pair: &Pair,
    environ: &Env,
    free: &[&str],
    homes: impl Fn(&str) -> Option<String>,
) -> Result<Settings, EnvError> {
    let catalog: BTreeMap<&str, &Key> = pair.catalog().map(|key| (key.as_str(), key)).collect();
    let mut lines = Vec::new();
    for (name, value) in &environ.0 {
        let fault = |fault, key: Option<&Key>| EnvError::at(fault, ENVIRONMENT, 0, key);
        let Some(&key) = catalog.get(name.as_str()) else {
            if free.contains(&name.as_str()) {
                continue;
            }

            let key = name.parse::<Key>().ok();
            let unknown = key.as_ref().map_or(Fault::BadKey, |_| Fault::UnknownKey);

            return Err(fault(unknown, key.as_ref()));
        };

        let EnvValue::Text(value) = value else {
            return Err(fault(Fault::NotUtf8, Some(key)));
        };

        // CONTRACT-QUESTION: no contract text says what an empty value of
        // the environment is. Reading taken: no value, so the default
        // holds. Each Python service reads an empty variable as a variable
        // that is not set (`Env::text`). A change costs this check.
        // The same question holds the reading of `free`: a variable outside
        // the catalog is refused unless the program names it there, also a
        // variable that the program does not read.
        if !value.is_empty() {
            lines.push(Line {
                key: key.clone(),
                value: value.clone(),
                number: 0,
            });
        }
    }

    layers(pair, &lines, ENVIRONMENT, &homes)
}

/// One catalog key with its text, between layer 2 and layer 5.
struct Node<'a> {
    key: &'a Key,
    file: &'a str,
    line: usize,
    text: &'a str,
    /// The text came from the `.env` layer, and no layer reads a reference
    /// in it.
    given: bool,
}

impl Node<'_> {
    fn fault(&self, fault: Fault) -> EnvError {
        EnvError::at(fault, self.file, self.line, Some(self.key))
    }
}

/// One piece of a text after layer 4.
enum Piece {
    Text(String),
    Reference(Key),
}

/// Layers 2 to 5 for the lines of the `.env` layer, which came from `file`:
/// the `.env` layer, the required keys, the homes and the references.
fn layers(
    pair: &Pair,
    given: &[Line],
    file: &str,
    homes: &dyn Fn(&str) -> Option<String>,
) -> Result<Settings, EnvError> {
    let given: BTreeMap<&str, &Line> = given.iter().map(|one| (one.key.as_str(), one)).collect();
    let mut missing = Vec::new();
    let mut nodes = Vec::new();
    let required = pair.required.iter().map(|line| (line, false));
    for (line, default) in pair
        .defaults
        .iter()
        .map(|line| (line, true))
        .chain(required)
    {
        let node = match (given.get(line.key.as_str()), default) {
            (Some(one), _) => (file, one.number, &one.value, true),
            (None, true) => (pair.default_file.as_str(), line.number, &line.value, false),
            (None, false) => {
                let at = Problem::new(
                    Fault::RequiredUnset,
                    &pair.example_file,
                    line.number,
                    Some(&line.key),
                );
                missing.push(at);
                continue;
            }
        };

        let (file, number, text, given) = node;
        nodes.push(Node {
            key: &line.key,
            file,
            line: number,
            text,
            given,
        });
    }

    if !missing.is_empty() {
        return Err(EnvError(missing));
    }

    let texts: BTreeMap<&str, &Node<'_>> =
        nodes.iter().map(|node| (node.key.as_str(), node)).collect();
    let mut resolver = Resolver {
        texts: &texts,
        pieces: BTreeMap::new(),
        done: BTreeMap::new(),
    };
    for node in &nodes {
        resolver
            .pieces
            .insert(node.key.as_str(), with_homes(node, &texts, homes)?);
    }

    let mut values = BTreeMap::new();
    for node in &nodes {
        let (value, _) = resolver.visit(node, node, &mut vec![node.key.as_str()])?;
        if !value.is_empty() {
            values.insert(node.key.clone(), value);
        }
    }

    let sources = nodes
        .iter()
        .map(|node| (node.key.clone(), node.file.to_owned(), node.line));

    Ok(Settings {
        sources: sources.collect(),
        values,
    })
}

/// Layer 4 for one key: each `${home:KEY}` as the home of the account in
/// KEY. The account is the text of KEY before layers 4 and 5, so a text
/// with a reference names no account.
///
/// A home is a value that a `.env` can hold: the rule of the Python reader,
/// which the owner confirmed.
fn with_homes(
    node: &Node<'_>,
    texts: &BTreeMap<&str, &Node<'_>>,
    homes: &dyn Fn(&str) -> Option<String>,
) -> Result<Vec<Piece>, EnvError> {
    if node.given {
        return Ok(vec![Piece::Text(node.text.to_owned())]);
    }

    let parts = parts_of(node.text).ok_or_else(|| node.fault(Fault::BadValue))?;
    let mut pieces = Vec::new();
    let mut text = String::new();
    for part in parts {
        let name = match part {
            Part::Text(more) => {
                text.push_str(more);
                continue;
            }
            Part::Reference(name) => {
                pieces.push(Piece::Text(std::mem::take(&mut text)));
                pieces.push(Piece::Reference(name));
                continue;
            }
            Part::Home(name) => name,
        };

        let account = texts
            .get(name.as_str())
            .ok_or_else(|| node.fault(Fault::UnknownReference))?;
        if account.text.is_empty() {
            return Err(node.fault(Fault::UnsetReference));
        }

        let home = homes(account.text).ok_or_else(|| node.fault(Fault::UnknownAccount))?;
        if !is_plain(&home) {
            return Err(node.fault(Fault::BadValue));
        }

        text.push_str(&home);
    }

    pieces.push(Piece::Text(text));

    Ok(pieces)
}

/// Layer 5: each `${KEY}` as the value of KEY.
struct Resolver<'a, 'b> {
    /// Each catalog key and its text.
    texts: &'b BTreeMap<&'a str, &'b Node<'a>>,
    /// The pieces of each text after layer 4.
    pieces: BTreeMap<&'a str, Vec<Piece>>,
    /// The value of each key that a visit completed, and its longest chain.
    done: BTreeMap<&'a str, (String, usize)>,
}

impl<'a> Resolver<'a, '_> {
    /// The value of `node`, the last key of `path`, and its longest chain.
    ///
    /// A fault names the key whose text holds the reference that fails. A
    /// chain that is too long is the one exception: the fault names `top`,
    /// the first key of `path`. The cycle check comes before the depth check,
    /// as in the Python reader. A cycle of 9 keys, [`MAX_DEPTH`] and one
    /// more, is thus [`Fault::ReferenceCycle`]. A cycle of 10 keys or more
    /// is a chain that is too long: its 9th reference comes before the
    /// reference that goes back to a key of the cycle.
    ///
    /// A value has [`MAX_BYTES`] at most after its references, the limit of
    /// a file: the rule of the Python reader, which the owner confirmed.
    fn visit(
        &mut self,
        node: &Node<'a>,
        top: &Node<'a>,
        path: &mut Vec<&'a str>,
    ) -> Result<(String, usize), EnvError> {
        let key = node.key.as_str();
        if let Some(known) = self.done.get(key) {
            return Ok(known.clone());
        }

        let (mut value, mut size, mut pending, mut height) = (String::new(), 0_usize, 0_usize, 0);
        for piece in self.pieces.remove(key).unwrap_or_default() {
            let name = match piece {
                Piece::Text(text) => {
                    pending = pending.saturating_add(text.len());
                    value.push_str(&text);
                    continue;
                }
                Piece::Reference(name) => name,
            };

            let Some((&name, target)) = self.texts.get_key_value(name.as_str()) else {
                return Err(node.fault(Fault::UnknownReference));
            };
            if path.contains(&name) {
                return Err(node.fault(Fault::ReferenceCycle));
            }
            if target.text.is_empty() {
                return Err(node.fault(Fault::UnsetReference));
            }
            if path.len() > MAX_DEPTH {
                return Err(top.fault(Fault::ReferenceTooDeep));
            }

            path.push(name);
            let visited = self.visit(target, top, path);
            path.pop();
            let (below, chain) = visited?;
            height = height.max(chain.saturating_add(1));
            if height > MAX_DEPTH {
                return Err(top.fault(Fault::ReferenceTooDeep));
            }

            for count in [std::mem::take(&mut pending), below.len()] {
                size = size.saturating_add(count);
                if size > MAX_BYTES {
                    return Err(node.fault(Fault::TooLarge));
                }
            }

            value.push_str(&below);
        }

        if size.saturating_add(pending) > MAX_BYTES {
            return Err(node.fault(Fault::TooLarge));
        }

        self.done.insert(key, (value.clone(), height));

        Ok((value, height))
    }
}

/// Refuses each catalog key of `environ` whose value is not the value of
/// the files. An optional key with no value has the empty value, and a
/// value that is not UTF-8 differs. A variable outside the catalog is free.
/// The fault names the line that gave the key its text. A program of root
/// calls this at its start.
///
/// # Errors
///
/// [`EnvError`] with one [`Fault::EnvironmentDiffers`] for each such key,
/// in the order of the catalog.
pub fn check_environment(settings: &Settings, environ: &Env) -> Result<(), EnvError> {
    let differs = |key: &Key| match environ.0.get(key.as_str()) {
        None => false,
        Some(EnvValue::NotUtf8) => true,
        Some(EnvValue::Text(value)) => value != settings.value(key.as_str()).unwrap_or_default(),
    };
    let problems: Vec<Problem> = settings
        .sources()
        .filter(|(key, _, _)| differs(key))
        .map(|(key, file, line)| Problem::new(Fault::EnvironmentDiffers, file, line, Some(key)))
        .collect();
    if !problems.is_empty() {
        return Err(EnvError(problems));
    }

    Ok(())
}

/// The grammar-only mode: each assignment of one file with the rules of a
/// `.env`, in the order of its lines, with no catalog and no reference. The
/// Python reader gives the same assignments as a map.
///
/// The mode takes the rules of a value of a `.env`, so no empty value and
/// no `$`: the rule of the Python reader, which the owner confirmed.
///
/// # Errors
///
/// [`EnvError`] with the first fault of the file.
pub fn raw(source: &Text) -> Result<Vec<Line>, EnvError> {
    parse(source, FileKind::Env)
}

/// The id of an account, as the system gives it.
///
/// ```
/// use creche_contracts::config::envfile::Uid;
///
/// assert_eq!(Uid::new(0), Uid::ROOT);
/// assert_eq!(Uid::new(1000).get(), 1000);
/// ```
///
/// Code outside this module cannot build an id from its field:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::envfile::Uid;
///
/// let uid = Uid(0);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Uid(u32);

impl Uid {
    /// The id of root.
    pub const ROOT: Self = Self(0);

    /// The id `raw`. Each number is the id of an account.
    #[must_use]
    pub const fn new(raw: u32) -> Self {
        Self(raw)
    }

    /// The number of the id.
    #[must_use]
    pub const fn get(self) -> u32 {
        self.0
    }
}

/// Refuses a `.env` that another account can change. The owner is root or
/// `reader`, and the group and each other account cannot write the file.
/// For root, only a file of root passes.
///
/// The `.env` says which account root drops to and where root installs, so
/// an account that can write the file chooses the two. The caller takes the
/// owner and the mode from one `fstat` of the open file, and reads the file
/// through the same descriptor: `creche_runtime::envfile::read_site`.
///
/// # Errors
///
/// [`EnvError`] with [`Fault::WrongOwner`] or [`Fault::OthersWrite`], for
/// the file `name` and line 0.
pub fn check_trust(name: &str, owner: Uid, mode: u32, reader: Uid) -> Result<(), EnvError> {
    if owner != Uid::ROOT && owner != reader {
        return Err(EnvError::at(Fault::WrongOwner, name, 0, None));
    }

    if mode & OTHERS_WRITE_BITS != 0 {
        return Err(EnvError::at(Fault::OthersWrite, name, 0, None));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;

    use super::*;
    use crate::config::site::{OperatorHome, OperatorUser, SiteKey};

    /// A written pair: a root, a reference, a home and one required key.
    const DEFAULTS: &str = "ROOT=/srv\nSTATE=${ROOT}/state\nHOME_DIR=${home:USER}\nOPTIONAL=\n";
    const EXAMPLE: &str = "#USER=operator\n";

    fn pair() -> Pair {
        read_pair(
            &Text::new(DEFAULT_FILE, DEFAULTS),
            &Text::new(EXAMPLE_FILE, EXAMPLE),
        )
        .unwrap()
    }

    /// The password database of each test: one account.
    fn homes(account: &str) -> Option<String> {
        (account == "operator").then(|| String::from("/home/operator"))
    }

    /// The faults of an error, as one line each.
    fn lines(error: &EnvError) -> Vec<String> {
        error.problems().iter().map(ToString::to_string).collect()
    }

    #[test]
    fn a_key_has_the_grammar_of_a_variable_name() {
        for text in ["A", "A1_2", "PATH", &format!("K{}", "_".repeat(63))] {
            assert_eq!(text.parse::<Key>().unwrap().as_str(), text);
        }

        for text in [
            "",
            "a",
            "1A",
            "_A",
            "A-B",
            "A B",
            "K\u{c9}Y",
            "A\u{661}",
            "A\n",
            &format!("K{}", "_".repeat(64)),
        ] {
            assert_eq!(text.parse::<Key>(), Err(KeyError), "{text:?}");
        }

        assert_eq!(
            KeyError.to_string(),
            "a key has the form [A-Z][A-Z0-9_]{0,63}"
        );
    }

    #[test]
    fn a_cycle_of_nine_keys_is_a_cycle_and_a_cycle_of_ten_is_too_deep() {
        let fault = |count: usize| {
            let cycle: String = (0..count)
                .map(|index| format!("R{index}=${{R{}}}\n", (index + 1) % count))
                .collect();
            let pair = read_pair(
                &Text::new(DEFAULT_FILE, cycle),
                &Text::new(EXAMPLE_FILE, ""),
            )
            .unwrap();

            lines(&load(&pair, &Text::new(".env", ""), homes).unwrap_err())
        };

        assert_eq!(fault(MAX_DEPTH + 1), ["default.env:9: reference-cycle R8"]);
        assert_eq!(
            fault(MAX_DEPTH + 2),
            ["default.env:1: reference-too-deep R0"]
        );
    }

    #[test]
    fn the_pair_of_the_product_gives_settings_for_a_written_env() {
        let pair = Pair::product().unwrap();
        let required: Vec<&str> = pair
            .required()
            .iter()
            .map(|line| line.key().as_str())
            .collect();
        let env = "AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER=operator\n\
                   AGENT_LAN_ADDRESS=192.0.2.10\n";
        let settings = load(&pair, &Text::new(".env", env), homes).unwrap();

        assert_eq!(
            required,
            [
                "AGENT_GITHUB_OWNER",
                "AGENT_OPERATOR_USER",
                "AGENT_LAN_ADDRESS"
            ]
        );
        let home: OperatorHome = settings
            .value(SiteKey::OperatorHome.as_str())
            .unwrap()
            .parse()
            .unwrap();
        let user: OperatorUser = settings
            .value(SiteKey::OperatorUser.as_str())
            .unwrap()
            .parse()
            .unwrap();
        assert_eq!(
            (home.as_str(), user.as_str()),
            ("/home/operator", "operator")
        );
    }

    #[test]
    fn a_copy_of_the_example_of_the_product_names_each_required_key() {
        let pair = Pair::product().unwrap();
        let error = load(&pair, &Text::new(".env", PRODUCT_EXAMPLE), homes).unwrap_err();

        let faults: Vec<(Fault, &str, &str)> = error
            .problems()
            .iter()
            .map(|one| (one.fault(), one.file(), one.key().map_or("", Key::as_str)))
            .collect();

        assert_eq!(
            faults,
            [
                (Fault::RequiredUnset, EXAMPLE_FILE, "AGENT_GITHUB_OWNER"),
                (Fault::RequiredUnset, EXAMPLE_FILE, "AGENT_OPERATOR_USER"),
                (Fault::RequiredUnset, EXAMPLE_FILE, "AGENT_LAN_ADDRESS"),
            ]
        );
    }

    #[test]
    fn the_settings_give_the_env_of_a_typed_config() {
        let settings = load(&pair(), &Text::new(".env", "USER=operator\n"), homes).unwrap();
        let env = settings.to_env();

        assert_eq!(
            format!("{env:?}"),
            r#"Env {"HOME_DIR", "ROOT", "STATE", "USER"}"#
        );
        assert_eq!(settings.value("STATE"), Some("/srv/state"));
        assert_eq!(settings.value("OPTIONAL"), None);
        let sources: Vec<(&str, &str, usize)> = settings
            .sources()
            .map(|(key, file, line)| (key.as_str(), file, line))
            .collect();
        assert_eq!(
            sources,
            [
                ("ROOT", DEFAULT_FILE, 1),
                ("STATE", DEFAULT_FILE, 2),
                ("HOME_DIR", DEFAULT_FILE, 3),
                ("OPTIONAL", DEFAULT_FILE, 4),
                ("USER", ".env", 1),
            ]
        );
    }

    #[test]
    fn debug_prints_no_value() {
        let secret = "TOKEN=hunter-two\n";
        let lines = raw(&Text::new("hooks.env", secret)).unwrap();
        let settings = load(&pair(), &Text::new(".env", "USER=operator\n"), homes).unwrap();

        for printed in [
            format!("{:?}", Text::new("hooks.env", secret)),
            format!("{lines:?}"),
            format!("{settings:?}"),
        ] {
            assert!(
                !printed.contains("hunter") && !printed.contains("/srv"),
                "{printed}"
            );
        }
    }

    #[test]
    fn the_environment_takes_the_place_of_a_dot_env() {
        let environ = Env::from_pairs([
            ("USER", "operator"),
            ("ROOT", "/data"),
            ("OPTIONAL", "a \"quoted\" #value with ${STATE}"),
            ("STATE", ""),
            ("PATH", "/usr/bin"),
        ]);
        let settings = load_environment(&pair(), &environ, &["PATH"], homes).unwrap();

        assert_eq!(settings.value("ROOT"), Some("/data"));
        assert_eq!(settings.value("STATE"), Some("/data/state"));
        assert_eq!(settings.value("HOME_DIR"), Some("/home/operator"));
        assert_eq!(
            settings.value("OPTIONAL"),
            Some("a \"quoted\" #value with ${STATE}")
        );
        assert_eq!(settings.value("PATH"), None);
        let sources: Vec<(&str, usize)> = settings
            .sources()
            .map(|(_, file, line)| (file, line))
            .collect();
        assert_eq!(&sources[..2], [(ENVIRONMENT, 0), (DEFAULT_FILE, 2)]);
    }

    #[test]
    fn the_environment_holds_no_variable_outside_the_catalog() {
        let not_utf8 = |name: &str| {
            Env::from_os([
                (OsString::from("USER"), OsString::from("operator")),
                (OsString::from(name), OsString::from_vec(vec![0xff])),
            ])
        };

        for (environ, free, wanted) in [
            (
                Env::from_pairs([("PATH", "/bin")]),
                &[][..],
                "environment: unknown-key PATH",
            ),
            (
                Env::from_pairs([("http_proxy", "x")]),
                &[],
                "environment: bad-key",
            ),
            (
                Env::from_pairs([("USER", "")]),
                &[],
                "example.env:1: required-unset USER",
            ),
            (not_utf8("ROOT"), &["ROOT"], "environment: not-utf8 ROOT"),
            (not_utf8("OTHER"), &[], "environment: unknown-key OTHER"),
        ] {
            let error = load_environment(&pair(), &environ, free, homes).unwrap_err();

            assert_eq!(lines(&error), [wanted], "{environ:?}");
        }

        assert!(load_environment(&pair(), &not_utf8("LANG"), &["LANG"], homes).is_ok());
    }

    #[test]
    fn a_value_of_the_environment_has_the_size_limit_of_a_value() {
        let environ = |size: usize| {
            let value = "a".repeat(size);

            Env::from_pairs([("USER", "operator"), ("OPTIONAL", value.as_str())])
        };

        let settings = load_environment(&pair(), &environ(MAX_BYTES), &[], homes).unwrap();
        assert_eq!(settings.value("OPTIONAL").map(str::len), Some(MAX_BYTES));
        let error = load_environment(&pair(), &environ(MAX_BYTES + 1), &[], homes).unwrap_err();
        assert_eq!(lines(&error), ["environment: too-large OPTIONAL"]);
    }

    #[test]
    fn a_value_of_the_environment_that_is_not_utf8_differs() {
        let settings = load(&pair(), &Text::new(".env", "USER=operator\n"), homes).unwrap();
        let environ = Env::from_os([(OsString::from("ROOT"), OsString::from_vec(vec![0xff]))]);
        let error = check_environment(&settings, &environ).unwrap_err();

        assert_eq!(lines(&error), ["default.env:1: environment-differs ROOT"]);
        assert_eq!(
            check_environment(&settings, &Env::from_pairs([("OPTIONAL", "")])),
            Ok(())
        );
    }

    /// The known answers of the trust check of the site file, from the
    /// tests of `site::check_trust` before the check moved here.
    #[test]
    fn only_root_or_the_reader_can_change_a_site_file() {
        let check = |owner, reader, mode| {
            check_trust(SITE_FILE, Uid::new(owner), mode, Uid::new(reader))
                .map_err(|error| error.problems()[0].fault())
        };

        assert_eq!(check(0, 1000, 0o100_644), Ok(()));
        assert_eq!(check(1000, 1000, 0o100_600), Ok(()));
        assert_eq!(check(0, 0, 0o100_644), Ok(()));
        assert_eq!(check(1001, 1000, 0o100_644), Err(Fault::WrongOwner));
        assert_eq!(check(1000, 0, 0o100_644), Err(Fault::WrongOwner));
        assert_eq!(check(0, 1000, 0o100_664), Err(Fault::OthersWrite));
        assert_eq!(check(0, 1000, 0o100_646), Err(Fault::OthersWrite));
    }

    #[test]
    fn an_error_joins_its_faults_in_one_line() {
        let error = load(&pair(), &Text::new(".env", ""), homes).unwrap_err();

        assert_eq!(error.to_string(), "example.env:1: required-unset USER");
        assert_eq!(Fault::OthersWrite.to_string(), "others-write");
    }
}
