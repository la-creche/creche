//! `component.yaml`, parsed as hostile input (contract 06 §8 and §10).
//!
//! A manifest can come from a branch that an agent wrote, so the reader
//! trusts nothing: a size cap before the parse, a closed key set, a check for
//! each scalar, and a refusal that names the field and does not show its
//! value.
//!
//! The reader does each check in the order of
//! `handover.manifest.parse_manifest`, so one text gives one refusal in both
//! languages.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::yaml::{self, Value, ValueId};
use super::{
    ContractId, ContractNumber, Kind, MANIFEST_CONTRACT_MAJOR, MANIFEST_CONTRACT_MINOR,
    RefusalCode, Releases, Repo, RestoreMode, RunsAs, SafeToken, VerifyUser,
};
use crate::ids::{ComponentName, ContractVersion, SecretName};

/// The largest `component.yaml`, in bytes. The reader applies the cap before
/// the parse.
pub const MAX_MANIFEST_BYTES: usize = 16 * 1024;

/// The largest count of items of a list field.
const MAX_LIST_ITEMS: usize = 64;

/// The largest count of words of one command.
const MAX_ARGV_ITEMS: usize = 32;

/// The largest count of characters of a string field and of a word.
const MAX_STRING_CHARS: usize = 512;

/// The largest count of characters of a path.
const MAX_PATH_CHARS: usize = 256;

/// The largest count of bytes of a path segment and of a unit name.
const SEGMENT_MAX: usize = 64;

/// What a path under the home of the operator starts with.
const HOME_PREFIX: &str = "~/";

/// The path of a component that is a whole repository.
const REPO_ROOT: &str = ".";

const TIMEOUT_MIN: u16 = 1;
const TIMEOUT_MAX: u16 = 300;
const KEEP_MIN: u8 = 1;
const KEEP_MAX: u8 = 10;

/// The one account that the operator can never be.
const ROOT_USER: &str = "root";

/// The largest count of bytes of the account name of the operator.
const OPERATOR_USER_MAX: usize = 32;

const REQUIRED_FIELDS: [&str; 11] = [
    "install",
    "kind",
    "manifest_version",
    "name",
    "path",
    "release",
    "repo",
    "restore",
    "runs_as",
    "unit",
    "verify",
];

const OPTIONAL_FIELDS: [&str; 5] = ["build", "depends_on", "provides", "requires", "secrets"];

/// What the reader gives for a field that the mapping does not hold.
static ABSENT: Value = Value::Null;

// --- the operator account of the site ---

/// The operator account of a site: its name and its home (`site.py` of the
/// Python release tool).
///
/// A manifest names no account of a host. It writes `operator` for the
/// account and `~/` for the home, and the reader fills both from this value.
///
/// The name is `[a-z_][a-z0-9_-]{0,31}` and is not `root`. The home is an
/// absolute path of plain segments: `(/[A-Za-z0-9_][A-Za-z0-9._-]*)+`.
///
/// ```
/// use creche_contracts::manifest::Operator;
///
/// let operator = Operator::new("keeper", "/home/keeper")?;
/// assert_eq!(operator.user(), "keeper");
/// assert_eq!(operator.home(), "/home/keeper");
/// # Ok::<(), creche_contracts::manifest::OperatorError>(())
/// ```
///
/// Code outside this module cannot build a value from raw strings:
///
/// ```compile_fail,E0451
/// use creche_contracts::manifest::Operator;
///
/// let operator = Operator {
///     user: String::from("root"),
///     home: String::from("/"),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Operator {
    user: String,
    home: String,
}

impl Operator {
    /// Checks the account name and then the home.
    ///
    /// # Errors
    ///
    /// Gives [`OperatorError`] for the first value that the site file must
    /// not hold.
    pub fn new(user: &str, home: &str) -> Result<Self, OperatorError> {
        if !is_operator_user(user) {
            return Err(OperatorError::User(SafeToken::of(user)));
        }

        if user == ROOT_USER {
            return Err(OperatorError::Root);
        }

        if !is_operator_home(home) {
            return Err(OperatorError::Home(SafeToken::of(home)));
        }

        Ok(Self {
            user: user.to_owned(),
            home: home.to_owned(),
        })
    }

    /// The account name of the operator.
    #[must_use]
    pub fn user(&self) -> &str {
        &self.user
    }

    /// The home of the operator.
    #[must_use]
    pub fn home(&self) -> &str {
        &self.home
    }
}

fn is_operator_user(text: &str) -> bool {
    let bytes = text.as_bytes();
    let first = bytes
        .first()
        .is_some_and(|byte| byte.is_ascii_lowercase() || *byte == b'_');
    let tail = bytes.iter().skip(1).all(|byte| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
    });

    first && tail && bytes.len() <= OPERATOR_USER_MAX
}

fn is_home_segment(segment: &str) -> bool {
    let bytes = segment.as_bytes();
    let first = bytes
        .first()
        .is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_');
    let tail = bytes
        .iter()
        .skip(1)
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'));

    first && tail
}

fn is_operator_home(text: &str) -> bool {
    text.strip_prefix('/')
        .is_some_and(|rest| rest.split('/').all(is_home_segment))
}

/// Why two values are not the operator account of a site.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OperatorError {
    /// The account name does not have the form of an account name.
    User(SafeToken),
    /// The account name is `root`.
    Root,
    /// The home is not an absolute path of plain segments.
    Home(SafeToken),
}

impl OperatorError {
    /// The check that the Python release tool names: `site`.
    #[must_use]
    pub const fn code(&self) -> RefusalCode {
        RefusalCode::Site
    }
}

impl fmt::Display for OperatorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::User(token) => {
                write!(
                    f,
                    "AGENT_OPERATOR_USER is not a value this accepts: {token}"
                )
            }
            Self::Root => f.write_str("AGENT_OPERATOR_USER names root"),
            Self::Home(token) => {
                write!(
                    f,
                    "AGENT_OPERATOR_HOME is not a value this accepts: {token}"
                )
            }
        }
    }
}

impl Error for OperatorError {}

// --- names and paths ---

/// The name of a systemd unit or of a compose project:
/// `[A-Za-z0-9@_.-]{1,64}` (contract 06 §8).
///
/// ```
/// use creche_contracts::manifest::UnitName;
///
/// let unit: UnitName = "creche-trigger@.service".parse()?;
/// assert_eq!(unit.as_str(), "creche-trigger@.service");
/// # Ok::<(), creche_contracts::manifest::UnitNameError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::UnitName;
///
/// let unit = UnitName(String::from("a b; reboot"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct UnitName(String);

impl UnitName {
    /// The name as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for UnitName {
    type Err = UnitNameError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.is_empty() {
            return Err(UnitNameError::Empty);
        }

        if text.len() > SEGMENT_MAX {
            return Err(UnitNameError::TooLong);
        }

        let bad = text.bytes().position(|byte| {
            !(byte.is_ascii_alphanumeric() || matches!(byte, b'@' | b'_' | b'.' | b'-'))
        });
        if let Some(at) = bad {
            return Err(UnitNameError::BadByte { at });
        }

        Ok(Self(text.to_owned()))
    }
}

impl fmt::Display for UnitName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not a unit name.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnitNameError {
    /// The text has no byte.
    Empty,
    /// The text has more than 64 bytes.
    TooLong,
    /// The byte at this offset, from 0, is not in the grammar.
    BadByte {
        /// The offset of the byte, from 0.
        at: usize,
    },
}

impl fmt::Display for UnitNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("a unit name has 1 byte or more"),
            Self::TooLong => write!(f, "a unit name has {SEGMENT_MAX} bytes or less"),
            Self::BadByte { at } => write!(
                f,
                "byte {at} of a unit name is not A to Z, a to z, 0 to 9, @, _, . or -"
            ),
        }
    }
}

impl Error for UnitNameError {}

fn is_path_segment(segment: &str) -> bool {
    (1..=SEGMENT_MAX).contains(&segment.len())
        && segment
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-'))
}

/// A path in a repository that cannot leave the repository (contract 06 §8,
/// the field `path`).
///
/// The path is `.` for a whole repository. Each other path has 256 characters
/// at most and is one or more segments of `[A-Za-z0-9_.-]{1,64}` with `/`
/// between them. No segment is `.` or `..`.
///
/// ```
/// use creche_contracts::manifest::RepoPath;
///
/// let path: RepoPath = "door-owui/src".parse()?;
/// assert_eq!(path.as_str(), "door-owui/src");
/// assert!("../etc".parse::<RepoPath>().is_err());
/// # Ok::<(), creche_contracts::manifest::RepoPathError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::RepoPath;
///
/// let path = RepoPath(String::from("../etc"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RepoPath(String);

impl RepoPath {
    /// The path as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Whether the path is the whole repository.
    #[must_use]
    pub fn is_whole_repo(&self) -> bool {
        self.0 == REPO_ROOT
    }
}

impl FromStr for RepoPath {
    type Err = RepoPathError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text == REPO_ROOT {
            return Ok(Self(text.to_owned()));
        }

        if text.chars().count() > MAX_PATH_CHARS {
            return Err(RepoPathError::TooLong);
        }

        if text.starts_with('/') || text.contains(['\\', '\0']) {
            return Err(RepoPathError::NotPlain);
        }

        let bad = text
            .split('/')
            .find(|segment| matches!(*segment, "." | "..") || !is_path_segment(segment));
        if let Some(segment) = bad {
            return Err(RepoPathError::Segment(SafeToken::of(segment)));
        }

        Ok(Self(text.to_owned()))
    }
}

impl fmt::Display for RepoPath {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not a path in a repository.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RepoPathError {
    /// The path has more than 256 characters.
    TooLong,
    /// The path starts with `/`, or holds `\` or NUL.
    NotPlain,
    /// This segment is `.`, `..`, empty, too long or not plain.
    Segment(SafeToken),
}

impl fmt::Display for RepoPathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLong => f.write_str("path is too long"),
            Self::NotPlain => f.write_str("path must be relative and plain"),
            Self::Segment(token) => write!(f, "path segment is not usable: {token}"),
        }
    }
}

impl Error for RepoPathError {}

/// An absolute path on the host: an install target or the program of a
/// command (contract 06 §8).
///
/// The path has 256 characters at most. It starts with `/`, does not end with
/// `/`, and holds no NUL, no empty segment, no `.` segment and no `..`
/// segment. Two `/` at the start are permitted, as POSIX keeps them.
///
/// ```
/// use creche_contracts::manifest::AbsolutePath;
///
/// let path: AbsolutePath = "/opt/components/chaperone".parse()?;
/// assert_eq!(path.as_str(), "/opt/components/chaperone");
/// assert!("/opt/components/../../etc".parse::<AbsolutePath>().is_err());
/// # Ok::<(), creche_contracts::manifest::AbsolutePathError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::AbsolutePath;
///
/// let path = AbsolutePath(String::from("/opt/components/../../etc"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct AbsolutePath(String);

impl AbsolutePath {
    /// The path as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for AbsolutePath {
    type Err = AbsolutePathError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.chars().count() > MAX_PATH_CHARS {
            return Err(AbsolutePathError::TooLong);
        }

        if !text.starts_with('/') || text.contains('\0') || text.ends_with('/') {
            return Err(AbsolutePathError::NotAbsolute);
        }

        // POSIX keeps exactly two `/` at the start of a path.
        let root = if text.starts_with("//") && !text.starts_with("///") {
            "//"
        } else {
            "/"
        };
        let segments = text.strip_prefix(root).unwrap_or(text);
        if segments
            .split('/')
            .any(|segment| segment.is_empty() || segment == ".")
        {
            return Err(AbsolutePathError::NotNormalized);
        }

        if segments.split('/').any(|segment| segment == "..") {
            return Err(AbsolutePathError::Parent);
        }

        Ok(Self(text.to_owned()))
    }
}

impl fmt::Display for AbsolutePath {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not an absolute path that the release tool uses.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AbsolutePathError {
    /// The path has more than 256 characters.
    TooLong,
    /// The path does not start with `/`, or ends with `/`, or holds NUL.
    NotAbsolute,
    /// The path holds an empty segment or a `.` segment.
    NotNormalized,
    /// The path holds a `..` segment.
    Parent,
}

impl fmt::Display for AbsolutePathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::TooLong => "path is too long",
            Self::NotAbsolute => "must be an absolute path",
            Self::NotNormalized => "must be a normalized path",
            Self::Parent => "must not hold '..'",
        })
    }
}

impl Error for AbsolutePathError {}

// --- bounded numbers ---

/// Makes a number type with a smallest and a largest value, and its error
/// type.
macro_rules! bounded_number {
    (
        $(#[$attribute:meta])*
        $name:ident($inner:ty),
        $(#[$error_attribute:meta])*
        $error:ident,
        $min:ident,
        $max:ident,
        $said:literal
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name($inner);

        impl $name {
            /// Checks that `number` is in the range of the type.
            ///
            /// # Errors
            ///
            /// Gives the error of the type for a number outside the range.
            pub const fn new(number: $inner) -> Result<Self, $error> {
                if number < $min || number > $max {
                    return Err($error);
                }

                Ok(Self(number))
            }

            /// The number.
            #[must_use]
            pub const fn get(self) -> $inner {
                self.0
            }
        }

        $(#[$error_attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $error;

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                write!(f, "{} is {} to {}", $said, $min, $max)
            }
        }

        impl Error for $error {}
    };
}

bounded_number! {
    /// The hard time limit of a verify hook, in seconds: 1 to 300 (contract 06
    /// §4).
    ///
    /// ```
    /// use creche_contracts::manifest::Timeout;
    ///
    /// let timeout = Timeout::new(60)?;
    /// assert_eq!(timeout.get(), 60);
    /// assert!(Timeout::new(0).is_err());
    /// # Ok::<(), creche_contracts::manifest::TimeoutError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw number:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::manifest::Timeout;
    ///
    /// let timeout = Timeout(0);
    /// ```
    Timeout(u16),
    /// A time limit that is not 1 to 300 seconds.
    TimeoutError,
    TIMEOUT_MIN,
    TIMEOUT_MAX,
    "the time limit of a verify hook, in seconds,"
}

bounded_number! {
    /// How many previous artifacts of a component stay on disk: 1 to 10
    /// (contract 06 §5).
    ///
    /// ```
    /// use creche_contracts::manifest::Keep;
    ///
    /// let keep = Keep::new(1)?;
    /// assert_eq!(keep.get(), 1);
    /// assert!(Keep::new(11).is_err());
    /// # Ok::<(), creche_contracts::manifest::KeepError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw number:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::manifest::Keep;
    ///
    /// let keep = Keep(0);
    /// ```
    Keep(u8),
    /// A count of previous artifacts that is not 1 to 10.
    KeepError,
    KEEP_MIN,
    KEEP_MAX,
    "the count of previous artifacts"
}

// --- the parts of a manifest ---

/// One command as a list of words. No shell reads it (contract 06 §4 and §8).
///
/// A command has 1 to 32 words. A word has 512 characters at most and holds
/// no NUL and no line feed. The first word is an absolute path. Only
/// [`ComponentManifest::parse`] makes a value.
///
/// ```
/// use creche_contracts::manifest::ComponentManifest;
///
/// let text = r#"{manifest_version: "0.6", name: chaperone, repo: agent-control, path: chaperone, kind: venv, unit: null, runs_as: root, install: {to: /opt/x, prev: /opt/x.prev}, verify: {command: [/bin/true, --json], user: root, timeout_s: 5}, restore: {mode: automatic, keep: 1}, release: yes}"#;
/// let manifest = ComponentManifest::parse(text, None)?;
/// let command = manifest.verify().command();
/// assert_eq!(command.program().as_str(), "/bin/true");
/// assert_eq!(command.words(), ["/bin/true", "--json"]);
/// # Ok::<(), creche_contracts::manifest::ManifestError>(())
/// ```
///
/// Code outside this module cannot build a value from raw words:
///
/// ```compile_fail,E0451
/// use creche_contracts::manifest::Argv;
///
/// let command = Argv {
///     program: "/bin/sh".parse().unwrap(),
///     words: vec![String::from("sh"), String::from("-c")],
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Argv {
    program: AbsolutePath,
    words: Vec<String>,
}

impl Argv {
    /// The first word: the program.
    #[must_use]
    pub fn program(&self) -> &AbsolutePath {
        &self.program
    }

    /// Each word, the program first.
    #[must_use]
    pub fn words(&self) -> &[String] {
        &self.words
    }
}

/// Makes the two types of a contract reference. They differ in the name of
/// the second number.
macro_rules! contract_reference {
    ($(#[$attribute:meta])* $name:ident, $(#[$minor_attribute:meta])* $minor:ident) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub struct $name {
            contract: ContractId,
            major: ContractNumber,
            $minor: ContractNumber,
        }

        impl $name {
            /// The contract.
            #[must_use]
            pub const fn contract(&self) -> ContractId {
                self.contract
            }

            /// The first number of the contract version.
            #[must_use]
            pub const fn major(&self) -> ContractNumber {
                self.major
            }

            $(#[$minor_attribute])*
            #[must_use]
            pub const fn $minor(&self) -> ContractNumber {
                self.$minor
            }
        }
    };
}

contract_reference! {
    /// One `provides` entry: the component answers each request that is valid
    /// in `major.0` to `major.minor` (contract 06 §3.1).
    ///
    /// Only [`ComponentManifest::parse`] makes a value.
    ///
    /// ```
    /// use creche_contracts::manifest::{ComponentManifest, ContractId};
    ///
    /// let text = r#"{manifest_version: "0.6", name: chaperone, repo: agent-control, path: chaperone, kind: venv, unit: null, runs_as: root, install: {to: /opt/x, prev: /opt/x.prev}, provides: [{contract: pep-grant, major: 2, minor: 1}], verify: {command: [/bin/true], user: root, timeout_s: 5}, restore: {mode: automatic, keep: 1}, release: yes}"#;
    /// let manifest = ComponentManifest::parse(text, None)?;
    /// let provided = manifest.provides().first().ok_or("no entry")?;
    /// assert_eq!(provided.contract(), ContractId::PepGrant);
    /// assert_eq!(provided.minor().get(), 1);
    /// # Ok::<(), Box<dyn std::error::Error>>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from raw parts:
    ///
    /// ```compile_fail,E0451
    /// use creche_contracts::manifest::{ContractId, ContractNumber, Provided};
    ///
    /// let number = ContractNumber::new(1).unwrap();
    /// let provided = Provided { contract: ContractId::Channel, major: number, minor: number };
    /// ```
    Provided,
    /// The second number: the newest minor version that the component
    /// answers.
    minor
}

contract_reference! {
    /// One `requires` entry: the component calls only what is valid in
    /// `major.min_minor` (contract 06 §3.1).
    ///
    /// Only [`ComponentManifest::parse`] makes a value.
    ///
    /// ```
    /// use creche_contracts::manifest::{ComponentManifest, ContractId};
    ///
    /// let text = r#"{manifest_version: "0.6", name: chaperone, repo: agent-control, path: chaperone, kind: venv, unit: null, runs_as: root, install: {to: /opt/x, prev: /opt/x.prev}, requires: [{contract: session-api, major: 1, min_minor: 2}], verify: {command: [/bin/true], user: root, timeout_s: 5}, restore: {mode: automatic, keep: 1}, release: yes}"#;
    /// let manifest = ComponentManifest::parse(text, None)?;
    /// let required = manifest.requires().first().ok_or("no entry")?;
    /// assert_eq!(required.contract(), ContractId::SessionApi);
    /// assert_eq!(required.min_minor().get(), 2);
    /// # Ok::<(), Box<dyn std::error::Error>>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from raw parts:
    ///
    /// ```compile_fail,E0451
    /// use creche_contracts::manifest::{ContractId, ContractNumber, Required};
    ///
    /// let number = ContractNumber::new(1).unwrap();
    /// let required = Required { contract: ContractId::Channel, major: number, min_minor: number };
    /// ```
    Required,
    /// The second number: the oldest minor version that the component can
    /// call.
    min_minor
}

/// The verify hook of a component (contract 06 §4).
///
/// Only [`ComponentManifest::parse`] makes a value.
///
/// ```
/// use creche_contracts::manifest::{ComponentManifest, VerifyUser};
///
/// let text = r#"{manifest_version: "0.6", name: chaperone, repo: agent-control, path: chaperone, kind: venv, unit: null, runs_as: root, install: {to: /opt/x, prev: /opt/x.prev}, verify: {command: [/bin/true], user: root, timeout_s: 5}, restore: {mode: automatic, keep: 1}, release: yes}"#;
/// let manifest = ComponentManifest::parse(text, None)?;
/// assert_eq!(manifest.verify().user(), VerifyUser::Root);
/// assert_eq!(manifest.verify().timeout().get(), 5);
/// # Ok::<(), creche_contracts::manifest::ManifestError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::manifest::{ComponentManifest, Timeout, Verify, VerifyUser};
///
/// fn other(manifest: &ComponentManifest) -> Verify {
///     Verify {
///         command: manifest.verify().command().clone(),
///         user: VerifyUser::Root,
///         timeout: Timeout::new(1).unwrap(),
///     }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verify {
    command: Argv,
    user: VerifyUser,
    timeout: Timeout,
}

impl Verify {
    /// The command of the hook.
    #[must_use]
    pub fn command(&self) -> &Argv {
        &self.command
    }

    /// The account that runs the hook.
    #[must_use]
    pub const fn user(&self) -> VerifyUser {
        self.user
    }

    /// The hard time limit of the hook.
    #[must_use]
    pub const fn timeout(&self) -> Timeout {
        self.timeout
    }
}

/// What the executor does with a component after a failed verify hook
/// (contract 06 §5).
///
/// Only [`ComponentManifest::parse`] makes a value.
///
/// ```
/// use creche_contracts::manifest::{ComponentManifest, RestoreMode};
///
/// let text = r#"{manifest_version: "0.6", name: chaperone, repo: agent-control, path: chaperone, kind: venv, unit: null, runs_as: root, install: {to: /opt/x, prev: /opt/x.prev}, verify: {command: [/bin/true], user: root, timeout_s: 5}, restore: {mode: manual, keep: 3}, release: yes}"#;
/// let manifest = ComponentManifest::parse(text, None)?;
/// assert_eq!(manifest.restore().mode(), RestoreMode::Manual);
/// assert_eq!(manifest.restore().keep().get(), 3);
/// # Ok::<(), creche_contracts::manifest::ManifestError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::manifest::{Keep, Restore, RestoreMode};
///
/// let restore = Restore { mode: RestoreMode::Automatic, keep: Keep::new(1).unwrap() };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Restore {
    mode: RestoreMode,
    keep: Keep,
}

impl Restore {
    /// Whether the executor can install the previous artifact again.
    #[must_use]
    pub const fn mode(&self) -> RestoreMode {
        self.mode
    }

    /// How many previous artifacts stay on disk.
    #[must_use]
    pub const fn keep(&self) -> Keep {
        self.keep
    }
}

/// The two paths of an installed component: the live tree and the previous
/// tree (contract 06 §8). The two paths differ.
///
/// Only [`ComponentManifest::parse`] makes a value.
///
/// ```
/// use creche_contracts::manifest::{ComponentManifest, Operator};
///
/// let text = r#"{manifest_version: "0.6", name: attendance, repo: agent-control, path: attendance, kind: venv, unit: null, runs_as: operator, install: {to: ~/x, prev: ~/x.prev}, verify: {command: [/bin/true], user: root, timeout_s: 5}, restore: {mode: automatic, keep: 1}, release: yes}"#;
/// let operator = Operator::new("keeper", "/home/keeper")?;
/// let manifest = ComponentManifest::parse(text, Some(&operator))?;
/// assert_eq!(manifest.install().to().as_str(), "/home/keeper/x");
/// assert_eq!(manifest.install().prev().as_str(), "/home/keeper/x.prev");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::manifest::Install;
///
/// let install = Install { to: "/opt/x".parse().unwrap(), prev: "/opt/x".parse().unwrap() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Install {
    to: AbsolutePath,
    prev: AbsolutePath,
}

impl Install {
    /// The path of the live tree.
    #[must_use]
    pub fn to(&self) -> &AbsolutePath {
        &self.to
    }

    /// The path of the previous tree.
    #[must_use]
    pub fn prev(&self) -> &AbsolutePath {
        &self.prev
    }
}

/// One parsed `component.yaml` (contract 06 §8).
///
/// A value exists only through [`ComponentManifest::parse`], so code that
/// holds a value does not check a field again.
///
/// ```
/// use creche_contracts::manifest::{ComponentManifest, Kind, Releases, RunsAs};
///
/// let text = "\
/// manifest_version: \"0.6\"
/// name: chaperone
/// repo: agent-control
/// path: chaperone
/// kind: binary
/// unit: creche-chaperone.service
/// runs_as: root
/// build:
///   - [\"/usr/local/bin/cargo\", \"build\", \"--release\", \"--locked\"]
/// install:
///   to: /opt/components/chaperone
///   prev: /opt/components/chaperone.prev
/// verify:
///   command: [\"/opt/components/chaperone/bin/chaperone-verify\"]
///   user: root
///   timeout_s: 60
/// restore:
///   mode: automatic
///   keep: 1
/// release: yes
/// ";
/// let manifest = ComponentManifest::parse(text, None)?;
/// assert_eq!(manifest.name().as_str(), "chaperone");
/// assert_eq!(manifest.kind(), Kind::Binary);
/// assert_eq!(manifest.runs_as(), RunsAs::Root);
/// assert_eq!(manifest.release(), Releases::Yes);
/// assert_eq!(manifest.build().len(), 1);
/// # Ok::<(), creche_contracts::manifest::ManifestError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts, or change a
/// field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::{ComponentManifest, Kind};
///
/// fn change(mut manifest: ComponentManifest) {
///     manifest.kind = Kind::Data;
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComponentManifest {
    manifest_version: ContractVersion,
    name: ComponentName,
    repo: Repo,
    path: RepoPath,
    kind: Kind,
    unit: Option<UnitName>,
    runs_as: RunsAs,
    build: Vec<Argv>,
    install: Install,
    provides: Vec<Provided>,
    requires: Vec<Required>,
    depends_on: Vec<ComponentName>,
    verify: Verify,
    restore: Restore,
    secrets: Vec<SecretName>,
    release: Releases,
}

impl ComponentManifest {
    /// The `component-manifest` contract version that the file is written
    /// against, as the file writes it.
    #[must_use]
    pub fn manifest_version(&self) -> &ContractVersion {
        &self.manifest_version
    }

    /// The name of the component. The reader does not check the name against
    /// the catalog.
    #[must_use]
    pub fn name(&self) -> &ComponentName {
        &self.name
    }

    /// The repository of the source.
    #[must_use]
    pub const fn repo(&self) -> Repo {
        self.repo
    }

    /// The root of the component in its repository.
    #[must_use]
    pub fn path(&self) -> &RepoPath {
        &self.path
    }

    /// What the artifact is.
    #[must_use]
    pub const fn kind(&self) -> Kind {
        self.kind
    }

    /// The unit that the component owns. `None` for a component with no unit.
    #[must_use]
    pub fn unit(&self) -> Option<&UnitName> {
        self.unit.as_ref()
    }

    /// The account that the unit runs as.
    #[must_use]
    pub const fn runs_as(&self) -> RunsAs {
        self.runs_as
    }

    /// The build commands, in order. Empty for a component that root only
    /// fetches.
    #[must_use]
    pub fn build(&self) -> &[Argv] {
        &self.build
    }

    /// The two install paths.
    #[must_use]
    pub fn install(&self) -> &Install {
        &self.install
    }

    /// The contracts that the component provides.
    #[must_use]
    pub fn provides(&self) -> &[Provided] {
        &self.provides
    }

    /// The contracts that the component requires.
    #[must_use]
    pub fn requires(&self) -> &[Required] {
        &self.requires
    }

    /// The components that deploy before this one. No name is there twice.
    #[must_use]
    pub fn depends_on(&self) -> &[ComponentName] {
        &self.depends_on
    }

    /// The verify hook.
    #[must_use]
    pub fn verify(&self) -> &Verify {
        &self.verify
    }

    /// The restore rule.
    #[must_use]
    pub const fn restore(&self) -> Restore {
        self.restore
    }

    /// The names of the secrets that the component reads. Never a value.
    #[must_use]
    pub fn secrets(&self) -> &[SecretName] {
        &self.secrets
    }

    /// Whether the component takes a release.
    #[must_use]
    pub const fn release(&self) -> Releases {
        self.release
    }

    /// Parses the text of one `component.yaml`.
    ///
    /// `operator` is the operator account of the site file. Give `None` on a
    /// host with no usable site file: the reader then refuses a manifest that
    /// needs the account.
    ///
    /// The failure action of a caller: refuse the release, write a ledger
    /// entry with the code and the detail, and change nothing on the host
    /// (contract 06 §10).
    ///
    /// # Errors
    ///
    /// Gives the first [`ManifestError`], in the order of the checks of the
    /// Python release tool.
    pub fn parse(text: &str, operator: Option<&Operator>) -> Result<Self, ManifestError> {
        if text.len() > MAX_MANIFEST_BYTES {
            return Err(top(ManifestFault::TooLarge));
        }

        let tree = yaml::load(text).map_err(|fault| top(ManifestFault::Yaml(fault.into())))?;
        let Value::Map(map) = tree.root() else {
            return Err(top(ManifestFault::NotMapping));
        };
        let known: Vec<&str> = REQUIRED_FIELDS
            .iter()
            .chain(&OPTIONAL_FIELDS)
            .copied()
            .collect();
        let fields = nested(&tree, map, "<top level>", &known, Scope::Top)?;
        let missing = REQUIRED_FIELDS
            .iter()
            .find(|field| map.get(field).is_none());
        if let Some(field) = missing {
            return Err(top(ManifestFault::Missing { field }));
        }

        let reader = ManifestReader { fields, operator };

        reader.read()
    }
}

// --- the refusals ---

/// Where in a manifest a refusal is. The Python release tool writes the scope
/// into the subject of the refusal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    /// The top level of the manifest.
    Top,
    /// The `install` mapping.
    Install,
    /// The `verify` mapping.
    Verify,
    /// The `restore` mapping.
    Restore,
    /// One entry of `provides`.
    Provides,
    /// One entry of `requires`.
    Requires,
    /// The site file, and not the manifest.
    Site,
}

impl Scope {
    /// The subject of a refusal in this scope. `label` is the name that the
    /// caller gives the manifest, for example `chaperone/component.yaml`.
    #[must_use]
    pub fn subject(self, label: &str) -> String {
        let field = match self {
            Self::Top => return label.to_owned(),
            Self::Site => return "site file".to_owned(),
            Self::Install => "install",
            Self::Verify => "verify",
            Self::Restore => "restore",
            Self::Provides => "provides",
            Self::Requires => "requires",
        };

        format!("{label}: {field}")
    }
}

/// Which path of a manifest a path refusal is about.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathField {
    /// The first word of a `build` command.
    Build,
    /// The first word of the `verify` command.
    Command,
    /// `install.to`.
    InstallTo,
    /// `install.prev`.
    InstallPrev,
}

impl PathField {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Build => "build[0]",
            Self::Command => "command[0]",
            Self::InstallTo => "install.to",
            Self::InstallPrev => "install.prev",
        }
    }
}

/// Why the YAML reader refuses a text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum YamlFault {
    /// The text holds a character that YAML does not permit.
    Unreadable,
    /// The text breaks a rule of YAML on this line, from 1.
    Line(usize),
    /// A collection nests deeper than 128 levels. The Python reader has no
    /// such refusal: its limit is the stack of its interpreter.
    Deep,
}

impl From<yaml::Fault> for YamlFault {
    fn from(fault: yaml::Fault) -> Self {
        match fault {
            yaml::Fault::Unreadable => Self::Unreadable,
            yaml::Fault::Line(line) => Self::Line(line.saturating_add(1)),
            yaml::Fault::Deep => Self::Deep,
        }
    }
}

/// The rule of contract 06 §8 that a manifest breaks.
///
/// A variant holds the name of a field, which the code gives, and never the
/// value of a field. A text of the input is there only as a [`SafeToken`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ManifestFault {
    /// The text is larger than [`MAX_MANIFEST_BYTES`].
    TooLarge,
    /// The text is not YAML that the reader takes.
    Yaml(YamlFault),
    /// The document is not a mapping.
    NotMapping,
    /// A mapping has a key that is not a string.
    NonStringKey {
        /// The field that holds the mapping.
        field: &'static str,
    },
    /// A mapping has a key that the schema does not name.
    UnknownField(SafeToken),
    /// The manifest does not have a field that the schema requires.
    Missing {
        /// The field.
        field: &'static str,
    },
    /// The value of a string field is not a string.
    NotString {
        /// The field.
        field: &'static str,
    },
    /// The value of a string field has more than 512 characters.
    TooLong {
        /// The field.
        field: &'static str,
    },
    /// The value of a field does not have the form of the field.
    Pattern {
        /// The field.
        field: &'static str,
        /// The value, when it is safe to show.
        token: SafeToken,
    },
    /// The value of a field is not a word of its closed set.
    NotOneOf {
        /// The field.
        field: &'static str,
        /// The value, when it is safe to show.
        token: SafeToken,
    },
    /// The value of a number field is not an integer.
    NotWhole {
        /// The field.
        field: &'static str,
    },
    /// The value of a number field is outside its range.
    OutOfRange {
        /// The field.
        field: &'static str,
        /// The smallest value.
        low: i64,
        /// The largest value.
        high: i64,
    },
    /// The value of a mapping field is not a mapping.
    NotMappingField {
        /// The field.
        field: &'static str,
    },
    /// The value of a list field is not a list.
    NotList {
        /// The field.
        field: &'static str,
    },
    /// A list field has more than 64 items.
    TooManyItems {
        /// The field.
        field: &'static str,
    },
    /// An item of `provides` or of `requires` is not a mapping.
    HoldsMappings {
        /// The field.
        field: &'static str,
    },
    /// A command is not a list. A string is the form that reaches a shell.
    ArgvNotList {
        /// The field.
        field: &'static str,
    },
    /// A command has no word or more than 32 words.
    ArgvCount {
        /// The field.
        field: &'static str,
    },
    /// A word of a command is not a string, or has more than 512 characters.
    ArgvWord {
        /// The field.
        field: &'static str,
    },
    /// A word of a command holds NUL or a line feed.
    ArgvControl {
        /// The field.
        field: &'static str,
    },
    /// An install path or the program of a command is not an absolute path
    /// that the release tool uses.
    Path {
        /// The path.
        field: PathField,
        /// The rule that the path breaks.
        fault: AbsolutePathError,
    },
    /// `path` is not a path in a repository.
    RepoPath(RepoPathError),
    /// `install.prev` is `install.to`.
    SamePaths,
    /// An item of `depends_on` is not a component name.
    DependsNames,
    /// `depends_on` holds this name two times.
    DependsRepeat(SafeToken),
    /// An item of `secrets` is not a secret name.
    SecretNames,
    /// `manifest_version` is not this contract's major version, at or below
    /// its minor version.
    VersionOutside(ContractVersion),
    /// A string that the release tool uses as a path or as a word holds a
    /// lone surrogate. The Python reader has no such refusal.
    NotUnicode {
        /// The field.
        field: &'static str,
    },
    /// The manifest needs the operator account, and the site file does not
    /// give one.
    Site,
}

impl fmt::Display for ManifestFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge => write!(f, "larger than {MAX_MANIFEST_BYTES} bytes"),
            Self::Yaml(YamlFault::Unreadable) => f.write_str("does not parse: unreadable YAML"),
            Self::Yaml(YamlFault::Line(line)) => write!(f, "does not parse: line {line}"),
            Self::Yaml(YamlFault::Deep) => write!(
                f,
                "does not parse: nests deeper than {} levels",
                yaml::DEPTH_MAX
            ),
            Self::NotMapping => f.write_str("is not a mapping"),
            Self::NonStringKey { field } => write!(f, "field '{field}' has a non-string key"),
            Self::UnknownField(token) => write!(f, "unknown field: {token}"),
            Self::Missing { field } => write!(f, "missing required field: {field}"),
            Self::NotString { field } => write!(f, "field '{field}' must be a string"),
            Self::TooLong { field } => write!(
                f,
                "field '{field}' is longer than {MAX_STRING_CHARS} characters"
            ),
            Self::Pattern { field, token } => {
                write!(f, "field '{field}' does not match its pattern: {token}")
            }
            Self::NotOneOf { field, token } => {
                write!(f, "field '{field}' is not one of its values: {token}")
            }
            Self::NotWhole { field } => write!(f, "field '{field}' must be a whole number"),
            Self::OutOfRange { field, low, high } => {
                write!(f, "field '{field}' must be between {low} and {high}")
            }
            Self::NotMappingField { field } => write!(f, "field '{field}' must be a mapping"),
            Self::NotList { field } => write!(f, "field '{field}' must be a list"),
            Self::TooManyItems { field } => {
                write!(f, "field '{field}' holds more than {MAX_LIST_ITEMS} items")
            }
            Self::HoldsMappings { field } => write!(f, "field '{field}' holds mappings"),
            Self::ArgvNotList { field } => {
                write!(f, "field '{field}' must be a list of argv words")
            }
            Self::ArgvCount { field } => {
                write!(f, "field '{field}' argv holds 1 to {MAX_ARGV_ITEMS} words")
            }
            Self::ArgvWord { field } => write!(f, "field '{field}' argv words are short strings"),
            Self::ArgvControl { field } => {
                write!(f, "field '{field}' argv words hold no NUL and no newline")
            }
            Self::Path { field, fault } => write!(f, "field '{}' {fault}", field.as_str()),
            Self::RepoPath(fault) => write!(f, "{fault}"),
            Self::SamePaths => f.write_str("install.prev must differ from install.to"),
            Self::DependsNames => f.write_str("depends_on holds component names"),
            Self::DependsRepeat(token) => write!(f, "depends_on repeats {token}"),
            Self::SecretNames => f.write_str("secrets holds names, never values"),
            Self::VersionOutside(declared) => write!(
                f,
                "manifest_version {declared} is outside {MANIFEST_CONTRACT_MAJOR}.0 to \
                 {MANIFEST_CONTRACT_MAJOR}.{MANIFEST_CONTRACT_MINOR}"
            ),
            Self::NotUnicode { field } => {
                write!(f, "field '{field}' holds a character that is not Unicode")
            }
            Self::Site => f.write_str("the site file gives no operator account"),
        }
    }
}

/// Why a text is not a component manifest: where the fault is and which rule
/// it breaks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestError {
    /// Where in the manifest the fault is.
    pub scope: Scope,
    /// The rule that the manifest breaks.
    pub fault: ManifestFault,
}

impl ManifestError {
    /// The check that the ledger names: `manifest`, or `site` for a manifest
    /// that needs the site file.
    #[must_use]
    pub const fn code(&self) -> RefusalCode {
        match self.scope {
            Scope::Site => RefusalCode::Site,
            _ => RefusalCode::Manifest,
        }
    }

    /// The subject of the refusal. `label` is the name that the caller gives
    /// the manifest.
    #[must_use]
    pub fn subject(&self, label: &str) -> String {
        self.scope.subject(label)
    }

    /// The detail of the refusal, as the Python release tool writes it.
    #[must_use]
    pub fn detail(&self) -> String {
        self.fault.to_string()
    }
}

impl fmt::Display for ManifestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.fault)
    }
}

impl Error for ManifestError {}

fn top(fault: ManifestFault) -> ManifestError {
    ManifestError {
        scope: Scope::Top,
        fault,
    }
}

// --- the reader ---

/// The fields of one mapping, and the scope that a refusal names.
#[derive(Clone, Copy)]
struct Fields<'a> {
    tree: &'a yaml::Tree,
    map: &'a yaml::Map,
    scope: Scope,
}

/// Checks the keys of one mapping against the closed key set `known`.
fn nested<'a>(
    tree: &'a yaml::Tree,
    map: &'a yaml::Map,
    field: &'static str,
    known: &[&str],
    scope: Scope,
) -> Result<Fields<'a>, ManifestError> {
    if map.has_other_key() {
        return Err(ManifestError {
            scope,
            fault: ManifestFault::NonStringKey { field },
        });
    }

    // The keys come in the order of the code points, as Python sorts them.
    if let Some(unknown) = map.keys().find(|key| !known.contains(key)) {
        return Err(ManifestError {
            scope,
            fault: ManifestFault::UnknownField(SafeToken::of(unknown)),
        });
    }

    Ok(Fields { tree, map, scope })
}

fn count_of_chars(text: &str) -> usize {
    text.chars().count()
}

/// `.{1,512}`: a text of 1 to 512 characters.
fn is_any_text(text: &str) -> bool {
    (1..=MAX_STRING_CHARS).contains(&count_of_chars(text))
}

impl<'a> Fields<'a> {
    fn refuse(&self, fault: ManifestFault) -> ManifestError {
        ManifestError {
            scope: self.scope,
            fault,
        }
    }

    /// The value of a field. An absent field gives `None`, as in Python.
    fn raw(&self, field: &str) -> &'a Value {
        self.map.get(field).map_or(&ABSENT, |id| self.tree.get(id))
    }

    fn text(
        &self,
        field: &'static str,
        pattern: fn(&str) -> bool,
    ) -> Result<&'a yaml::Text, ManifestError> {
        let Value::Text(text) = self.raw(field) else {
            return Err(self.refuse(ManifestFault::NotString { field }));
        };
        if count_of_chars(text.as_str()) > MAX_STRING_CHARS {
            return Err(self.refuse(ManifestFault::TooLong { field }));
        }

        if !pattern(text.as_str()) {
            let token = SafeToken::of(text.as_str());
            return Err(self.refuse(ManifestFault::Pattern { field, token }));
        }

        Ok(text)
    }

    /// The value of a field as one word of a closed set.
    fn word<T: FromStr>(&self, field: &'static str) -> Result<T, ManifestError> {
        let Value::Text(text) = self.raw(field) else {
            return Err(self.refuse(ManifestFault::NotString { field }));
        };

        text.as_str().parse().map_err(|_| {
            let token = SafeToken::of(text.as_str());
            self.refuse(ManifestFault::NotOneOf { field, token })
        })
    }

    fn integer(&self, field: &'static str, low: i64, high: i64) -> Result<i64, ManifestError> {
        let Value::Int(value) = self.raw(field) else {
            return Err(self.refuse(ManifestFault::NotWhole { field }));
        };

        match value {
            Some(value) if (low..=high).contains(value) => Ok(*value),
            _ => Err(self.refuse(ManifestFault::OutOfRange { field, low, high })),
        }
    }

    fn mapping(
        &self,
        field: &'static str,
        known: &[&str],
        scope: Scope,
    ) -> Result<Self, ManifestError> {
        let Value::Map(map) = self.raw(field) else {
            return Err(self.refuse(ManifestFault::NotMappingField { field }));
        };

        nested(self.tree, map, field, known, scope)
    }

    /// The items of a list field. An absent field and `null` give no item.
    fn items(&self, field: &'static str) -> Result<&'a [ValueId], ManifestError> {
        match self.raw(field) {
            Value::Null => Ok(&[]),
            Value::List(items) if items.len() > MAX_LIST_ITEMS => {
                Err(self.refuse(ManifestFault::TooManyItems { field }))
            }
            Value::List(items) => Ok(items),
            _ => Err(self.refuse(ManifestFault::NotList { field })),
        }
    }
}

/// The value of a text that the reader checked with a parsing constructor.
/// The pattern of the field and the constructor are one grammar, so the
/// second parse cannot fail. If it does, the reader refuses the field.
fn checked<T: FromStr>(
    fields: &Fields<'_>,
    field: &'static str,
    text: &yaml::Text,
) -> Result<T, ManifestError> {
    text.as_str().parse().map_err(|_| {
        let token = SafeToken::of(text.as_str());
        fields.refuse(ManifestFault::Pattern { field, token })
    })
}

/// The number that `digits` writes, when it is small. Zeros at the start do
/// not change the number, as Python `int` reads it.
fn small_number(digits: &str) -> Option<u16> {
    match digits.trim_start_matches('0') {
        "" => Some(0),
        significant => significant.parse().ok(),
    }
}

/// One number of an integer field, in a type that holds its range.
fn narrow<T: TryFrom<i64>>(
    fields: &Fields<'_>,
    field: &'static str,
    low: i64,
    high: i64,
) -> Result<T, ManifestError> {
    let value = fields.integer(field, low, high)?;

    T::try_from(value).map_err(|_| fields.refuse(ManifestFault::OutOfRange { field, low, high }))
}

struct ManifestReader<'a> {
    fields: Fields<'a>,
    operator: Option<&'a Operator>,
}

impl ManifestReader<'_> {
    fn read(&self) -> Result<ComponentManifest, ManifestError> {
        let fields = &self.fields;
        let provides = self.read_references("provides", "minor", Scope::Provides)?;
        let requires = self.read_references("requires", "min_minor", Scope::Requires)?;
        let declared = fields.text("manifest_version", |text| {
            text.parse::<ContractVersion>().is_ok()
        })?;
        let manifest_version = Self::check_manifest_version(fields, declared)?;
        let name_text = fields.text("name", |text| text.parse::<ComponentName>().is_ok())?;
        let name = checked(fields, "name", name_text)?;
        let repo = fields.word("repo")?;
        let path = fields
            .text("path", is_any_text)?
            .as_str()
            .parse()
            .map_err(|fault| top(ManifestFault::RepoPath(fault)))?;
        let kind = fields.word("kind")?;
        let unit = match fields.raw("unit") {
            Value::Null => None,
            _ => {
                let text = fields.text("unit", |text| text.parse::<UnitName>().is_ok())?;
                Some(checked(fields, "unit", text)?)
            }
        };
        let runs_as = self.read_account(fields, "runs_as", RunsAs::Operator)?;
        let build = fields
            .items("build")?
            .iter()
            .map(|entry| self.read_argv(fields.tree.get(*entry), fields, "build", PathField::Build))
            .collect::<Result<_, _>>()?;
        let install = self.read_install()?;
        let depends_on = Self::read_depends_on(fields)?;
        let verify = self.read_verify()?;
        let restore = Self::read_restore(fields)?;
        let secrets = Self::read_secrets(fields)?;
        let release = Self::read_release(fields)?;

        Ok(ComponentManifest {
            manifest_version,
            name,
            repo,
            path,
            kind,
            unit,
            runs_as,
            build,
            install,
            provides: provides
                .into_iter()
                .map(|(contract, major, minor)| Provided {
                    contract,
                    major,
                    minor,
                })
                .collect(),
            requires: requires
                .into_iter()
                .map(|(contract, major, min_minor)| Required {
                    contract,
                    major,
                    min_minor,
                })
                .collect(),
            depends_on,
            verify,
            restore,
            secrets,
            release,
        })
    }

    /// The items of `provides` or of `requires`. The two fields differ in
    /// the key of the second number.
    fn read_references(
        &self,
        field: &'static str,
        minor_key: &'static str,
        scope: Scope,
    ) -> Result<Vec<(ContractId, ContractNumber, ContractNumber)>, ManifestError> {
        let fields = &self.fields;
        let known = ["contract", "major", minor_key];
        let high = i64::from(ContractNumber::MAX.get());
        let mut references = Vec::new();
        for entry in fields.items(field)? {
            let Value::Map(map) = fields.tree.get(*entry) else {
                return Err(fields.refuse(ManifestFault::HoldsMappings { field }));
            };
            let item = nested(fields.tree, map, field, &known, scope)?;
            let number = |key: &'static str| -> Result<ContractNumber, ManifestError> {
                let value: u16 = narrow(&item, key, 0, high)?;

                ContractNumber::new(value).map_err(|_| {
                    item.refuse(ManifestFault::OutOfRange {
                        field: key,
                        low: 0,
                        high,
                    })
                })
            };
            let contract = item.word("contract")?;
            let major = number("major")?;
            let minor = number(minor_key)?;
            references.push((contract, major, minor));
        }

        Ok(references)
    }

    /// Checks that the declared version has this contract's major version
    /// and a minor version that this code speaks.
    fn check_manifest_version(
        fields: &Fields<'_>,
        declared: &yaml::Text,
    ) -> Result<ContractVersion, ManifestError> {
        let version: ContractVersion = checked(fields, "manifest_version", declared)?;
        let mut numbers = version.as_str().split('.').map(small_number);
        let major = numbers.next().flatten();
        let minor = numbers.next().flatten();
        let inside = major == Some(MANIFEST_CONTRACT_MAJOR)
            && minor.is_some_and(|minor| minor <= MANIFEST_CONTRACT_MINOR);
        if !inside {
            return Err(top(ManifestFault::VersionOutside(version)));
        }

        Ok(version)
    }

    /// `runs_as` or `verify.user`: one word of the set.
    ///
    /// A manifest that root stamped into a live tree before the word
    /// `operator` existed names the account itself. It reads as `operator` on
    /// the host whose operator it is.
    fn read_account<T: FromStr>(
        &self,
        fields: &Fields<'_>,
        field: &'static str,
        operator_word: T,
    ) -> Result<T, ManifestError> {
        let refusal = match fields.word(field) {
            Ok(account) => return Ok(account),
            Err(refusal) => refusal,
        };

        match (fields.raw(field), self.operator) {
            (Value::Text(text), Some(operator))
                if !text.is_lossy() && text.as_str() == operator.user() =>
            {
                Ok(operator_word)
            }
            _ => Err(refusal),
        }
    }

    /// `~/x` as `<the home of the operator>/x`. Each other text stays.
    fn expand_home(&self, text: &str) -> Result<String, ManifestError> {
        let Some(rest) = text.strip_prefix(HOME_PREFIX) else {
            return Ok(text.to_owned());
        };
        let Some(operator) = self.operator else {
            return Err(ManifestError {
                scope: Scope::Site,
                fault: ManifestFault::Site,
            });
        };

        Ok(format!("{}/{rest}", operator.home()))
    }

    /// One command. A string is refused: a string is what reaches a shell.
    fn read_argv(
        &self,
        raw: &Value,
        fields: &Fields<'_>,
        field: &'static str,
        path_field: PathField,
    ) -> Result<Argv, ManifestError> {
        let Value::List(items) = raw else {
            return Err(fields.refuse(ManifestFault::ArgvNotList { field }));
        };
        if items.is_empty() || items.len() > MAX_ARGV_ITEMS {
            return Err(fields.refuse(ManifestFault::ArgvCount { field }));
        }

        let mut words = Vec::with_capacity(items.len());
        for item in items {
            let Value::Text(word) = fields.tree.get(*item) else {
                return Err(fields.refuse(ManifestFault::ArgvWord { field }));
            };
            if count_of_chars(word.as_str()) > MAX_STRING_CHARS {
                return Err(fields.refuse(ManifestFault::ArgvWord { field }));
            }

            if word.as_str().contains(['\0', '\n']) {
                return Err(fields.refuse(ManifestFault::ArgvControl { field }));
            }

            // CONTRACT-QUESTION: contract 06 §4 and §8 say that a word is a
            // string. The Python reader accepts a word with a lone surrogate
            // from the escape `"\ud800"`. No Rust string holds one, and no
            // program takes one as an argument, so this reader refuses the
            // word. To accept it, a word must be a list of code units.
            if word.is_lossy() {
                return Err(fields.refuse(ManifestFault::NotUnicode { field }));
            }

            words.push(word.as_str().to_owned());
        }

        let first = words.first().map_or("", String::as_str);
        let expanded = self.expand_home(first)?;
        let program: AbsolutePath = expanded.parse().map_err(|fault| {
            fields.refuse(ManifestFault::Path {
                field: path_field,
                fault,
            })
        })?;
        if let Some(word) = words.first_mut() {
            *word = expanded;
        }

        Ok(Argv { program, words })
    }

    /// One install path: a text, with the home of the operator for `~/`.
    fn read_install_path(
        &self,
        install: &Fields<'_>,
        key: &'static str,
        field: PathField,
    ) -> Result<AbsolutePath, ManifestError> {
        let text = install.text(key, is_any_text)?;
        let expanded = self.expand_home(text.as_str())?;
        // CONTRACT-QUESTION: contract 06 §8 says that an install path is an
        // absolute path. The Python reader accepts a path with a lone
        // surrogate. This reader refuses it, as it refuses such a word of a
        // command.
        if text.is_lossy() {
            return Err(top(ManifestFault::NotUnicode {
                field: field.as_str(),
            }));
        }

        expanded
            .parse()
            .map_err(|fault| top(ManifestFault::Path { field, fault }))
    }

    fn read_install(&self) -> Result<Install, ManifestError> {
        let install = self
            .fields
            .mapping("install", &["to", "prev"], Scope::Install)?;
        let to = self.read_install_path(&install, "to", PathField::InstallTo)?;
        let prev = self.read_install_path(&install, "prev", PathField::InstallPrev)?;
        if to == prev {
            return Err(top(ManifestFault::SamePaths));
        }

        Ok(Install { to, prev })
    }

    fn read_depends_on(fields: &Fields<'_>) -> Result<Vec<ComponentName>, ManifestError> {
        let mut names: Vec<ComponentName> = Vec::new();
        for entry in fields.items("depends_on")? {
            let name = match fields.tree.get(*entry) {
                Value::Text(text) => text.as_str().parse::<ComponentName>().ok(),
                _ => None,
            };
            let Some(name) = name else {
                return Err(fields.refuse(ManifestFault::DependsNames));
            };
            if names.contains(&name) {
                let token = SafeToken::of(name.as_str());
                return Err(fields.refuse(ManifestFault::DependsRepeat(token)));
            }

            names.push(name);
        }

        Ok(names)
    }

    fn read_secrets(fields: &Fields<'_>) -> Result<Vec<SecretName>, ManifestError> {
        fields
            .items("secrets")?
            .iter()
            .map(|entry| match fields.tree.get(*entry) {
                Value::Text(text) => text.as_str().parse().ok(),
                _ => None,
            })
            .map(|name| name.ok_or_else(|| fields.refuse(ManifestFault::SecretNames)))
            .collect()
    }

    fn read_verify(&self) -> Result<Verify, ManifestError> {
        let verify =
            self.fields
                .mapping("verify", &["command", "user", "timeout_s"], Scope::Verify)?;
        let command = self.read_argv(
            verify.raw("command"),
            &verify,
            "command",
            PathField::Command,
        )?;
        let user = self.read_account(&verify, "user", VerifyUser::Operator)?;
        let (low, high) = (i64::from(TIMEOUT_MIN), i64::from(TIMEOUT_MAX));
        let seconds: u16 = narrow(&verify, "timeout_s", low, high)?;
        let timeout = Timeout::new(seconds).map_err(|_| {
            verify.refuse(ManifestFault::OutOfRange {
                field: "timeout_s",
                low,
                high,
            })
        })?;

        Ok(Verify {
            command,
            user,
            timeout,
        })
    }

    fn read_restore(fields: &Fields<'_>) -> Result<Restore, ManifestError> {
        let restore = fields.mapping("restore", &["mode", "keep"], Scope::Restore)?;
        let mode = restore.word("mode")?;
        let (low, high) = (i64::from(KEEP_MIN), i64::from(KEEP_MAX));
        let count: u8 = narrow(&restore, "keep", low, high)?;
        let keep = Keep::new(count).map_err(|_| {
            restore.refuse(ManifestFault::OutOfRange {
                field: "keep",
                low,
                high,
            })
        })?;

        Ok(Restore { mode, keep })
    }

    /// `release: yes` is a YAML boolean. `release: "yes"` is a string. The
    /// reader takes both.
    fn read_release(fields: &Fields<'_>) -> Result<Releases, ManifestError> {
        match fields.raw("release") {
            Value::Bool(true) => Ok(Releases::Yes),
            Value::Bool(false) => Ok(Releases::No),
            _ => fields.word("release"),
        }
    }
}
