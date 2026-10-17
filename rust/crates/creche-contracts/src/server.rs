//! The MCP server file: `server.toml` or `server.yaml`, and its install pin
//! (contract 01b).
//!
//! The two layers of [`crate::family`] hold a server file too:
//!
//! 1. [`RawServer`] holds each field as the file wrote it.
//!    `RawServer::from_toml` reads the bytes of a `server.toml` into it.
//! 2. [`Server`] holds a file that obeys each rule that one file can show. The
//!    one way to a `Server` is [`Server::from_raw`], with the markup of the
//!    file. `TryFrom<RawServer>` is that conversion for a YAML file.
//!
//! The conversion reports each violation, with the severity, the location and
//! the message of the Python validator `agent_family`. One rule needs more
//! than the file: the name is the name of the directory. `agent-family`
//! checks it.
//!
//! A TOML file gets the grammar of root, the second reader of a server file
//! (contract 01b §1). Each rule of root that the YAML path does not hold is a
//! rule of the TOML path: "The grammar of root" below lists them. A file that
//! the TOML path accepts thus passes root too. A YAML file keeps the rules of
//! the Python YAML path. A value that the TOML path accepts is a value that
//! the YAML path accepts, so one `Server` type holds both.
//!
//! Code outside this module cannot build a `Server` from raw fields:
//!
//! ```compile_fail,E0451
//! use creche_contracts::server::{RawServer, Server};
//!
//! fn take(raw: RawServer) -> Server {
//!     Server { name: raw.name }
//! }
//! ```
//!
//! The conversion is the way in:
//!
//! ```
//! use creche_contracts::server::{RawServer, Server};
//!
//! fn take(raw: RawServer) -> Option<Server> {
//!     Server::try_from(raw).ok()
//! }
//! ```

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::str::FromStr;

use creche_util::pytext;
use serde::{Deserialize, Serialize};

use crate::family::{
    ALL_TOOLS, DESCRIPTION_MAX, Description, DescriptionError, Issues, Markup, Placed,
    RawToolGrant, Refused, Section, Slot, collapse, tool_name_issue,
};
use crate::ids::{EnvName, PackageVersion, SecretName, ServerName, Sha256Hex, ToolName};

/// The largest count of characters in an identity (contract 01b §2). The
/// YAML path counts after the collapse of the spaces, and the TOML path
/// counts each character.
pub const IDENTITY_MAX: usize = 300;

/// The start of a value of `run.env` that names a secret (contract 01b
/// §4.1).
pub const SECRET_PREFIX: &str = "secret:";

/// A variable whose name holds one of these words takes its value from a
/// secret (contract 01b §4.1 rule 2).
const SECRET_NAME_MARKERS: [&str; 4] = ["TOKEN", "KEY", "PASSWORD", "SECRET"];

/// The largest count of bytes in a lock path (contract 01b §3.4 rule 3).
const LOCK_PATH_MAX: usize = 256;

/// The largest count of characters of a lock path that a message shows.
const LOCK_SHOWN_MAX: usize = 80;

/// The largest count of names in the `arg` of a fence (contract 01b §7.2).
const ARG_PARTS_MAX: usize = 2;

/// The largest count of bytes in one name of the `arg` of a fence (contract
/// 01b §7.2).
const ARG_NAME_MAX: usize = 64;

const DEFAULT_PYTHON: &str = "3.12";

fn default_python() -> String {
    DEFAULT_PYTHON.to_owned()
}

// --- the raw file ---

/// `install`, as the file wrote it. The source decides which fields the
/// block holds, so each pin field is optional here.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawInstall {
    pub source: String,
    #[serde(default)]
    pub package: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub lock: Option<String>,
    #[serde(default = "default_python")]
    pub python: String,
    #[serde(default)]
    pub repo: Option<String>,
    #[serde(default)]
    pub asset: Option<String>,
    #[serde(default)]
    pub sha256: Option<String>,
    #[serde(default, rename = "ref")]
    pub git_ref: Option<String>,
}

/// `run`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawRun {
    pub entrypoint: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub state_dir: bool,
    #[serde(default)]
    pub state_dir_env: Option<String>,
}

/// One entry of `tools`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawTool {
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub write: bool,
}

/// One entry of `arg_allows` or of `arg_denies`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawFence {
    pub tools: RawToolGrant,
    pub arg: String,
    pub values: Vec<String>,
}

/// One entry of `shared_secrets`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawSharedSecret {
    pub secret: String,
    pub server: String,
}

/// One `server.yaml`, as the file wrote it, with each default.
///
/// The shape is the shape of the Python model `McpServerFile`. No value rule
/// holds for a value of this type.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawServer {
    pub name: String,
    pub identity: String,
    pub install: RawInstall,
    pub run: RawRun,
    #[serde(default)]
    pub tools: Vec<RawTool>,
    #[serde(default)]
    pub arg_allows: Vec<RawFence>,
    #[serde(default)]
    pub arg_denies: Vec<RawFence>,
    #[serde(default)]
    pub shared_secrets: Vec<RawSharedSecret>,
}

impl RawServer {
    /// The names of the tools that the file declares, in the order of the
    /// file.
    pub fn tool_names(&self) -> impl Iterator<Item = &str> {
        self.tools.iter().map(|tool| tool.name.as_str())
    }
}

// --- value types ---

/// Makes a struct with private fields, and the two doc tests that show that
/// code outside this module can hold a value and cannot build one.
macro_rules! sealed {
    ($(#[$attribute:meta])* pub struct $name:ident { $($field:tt)* }) => {
        $(#[$attribute])*
        ///
        /// Code outside this module cannot build a value from raw fields:
        ///
        #[doc = "```compile_fail,E0451"]
        #[doc = concat!("use creche_contracts::server::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("fn build(other: ", stringify!($name), ") -> ", stringify!($name), " {")]
        #[doc = concat!("    ", stringify!($name), " { ..other }")]
        #[doc = "}"]
        #[doc = "```"]
        ///
        /// Code outside this module can hold a value:
        ///
        #[doc = "```"]
        #[doc = concat!("use creche_contracts::server::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("fn keep(other: ", stringify!($name), ") -> ", stringify!($name), " {")]
        #[doc = "    other"]
        #[doc = "}"]
        #[doc = "```"]
        pub struct $name { $($field)* }
    };
}

/// Makes the shell of a type that holds one checked text.
macro_rules! checked_text {
    ($(#[$attribute:meta])* $name:ident) => {
        $(#[$attribute])*
        ///
        /// Code outside this module cannot build a value from a raw value:
        ///
        #[doc = "```compile_fail,E0423"]
        #[doc = concat!("use creche_contracts::server::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("let value = ", stringify!($name), "(Default::default());")]
        #[doc = "```"]
        ///
        /// Code outside this module can hold a value:
        ///
        #[doc = "```"]
        #[doc = concat!("use creche_contracts::server::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("fn keep(other: ", stringify!($name), ") -> ", stringify!($name), " {")]
        #[doc = "    other"]
        #[doc = "}"]
        #[doc = "```"]
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);

        impl $name {
            /// The text, as the file wrote it.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

/// Makes the `Display` and the `Error` of an error type from one text.
macro_rules! error_text {
    ($name:ident => $text:literal) => {
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str($text)
            }
        }

        impl Error for $name {}
    };
}

checked_text! {
    /// Whose credential a server holds: 1 to 300 characters after the
    /// collapse of each run of spaces (contract 01b §2).
    ///
    /// ```
    /// use creche_contracts::server::Identity;
    ///
    /// assert!("The read-only account of the operator.".parse::<Identity>().is_ok());
    /// assert!("   ".parse::<Identity>().is_err());
    /// ```
    Identity
}

/// Why a text is not an identity. The field is the count of characters
/// after the collapse.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IdentityError {
    /// The count of characters after the collapse.
    pub length: usize,
}

impl fmt::Display for IdentityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "an identity has 1 to {IDENTITY_MAX} characters, not {}",
            self.length
        )
    }
}

impl Error for IdentityError {}

impl FromStr for Identity {
    type Err = IdentityError;

    fn from_str(text: &str) -> Result<Self, IdentityError> {
        let length = collapse(text).chars().count();
        if !(1..=IDENTITY_MAX).contains(&length) {
            return Err(IdentityError { length });
        }

        Ok(Self(text.to_owned()))
    }
}

/// Where a server comes from (contract 01b §3).
///
/// The set is closed. A reader refuses another value: the source decides
/// what root installs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstallSource {
    Pypi,
    GithubRelease,
    AgentMcp,
}

impl InstallSource {
    /// Each source, in the order of the contract.
    pub const ALL: [Self; 3] = [Self::Pypi, Self::GithubRelease, Self::AgentMcp];

    /// The text of the source in a file.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pypi => "pypi",
            Self::GithubRelease => "github-release",
            Self::AgentMcp => "agent-mcp",
        }
    }

    /// The source of a text. `None` for a text that is not in the set.
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|source| source.as_str() == text)
    }

    /// The pin fields that the source requires, in the order of the
    /// contract.
    fn required(self) -> &'static [&'static str] {
        match self {
            Self::Pypi => &["package", "version", "lock"],
            Self::GithubRelease => &["repo", "version", "asset", "sha256"],
            Self::AgentMcp => &[],
        }
    }
}

impl fmt::Display for InstallSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

checked_text! {
    /// A repository on GitHub: `<owner>/<name>`, each part
    /// `[A-Za-z0-9._-]+` (contract 01b §3.2).
    GithubRepo
}

/// Why a text is not a repository on GitHub.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GithubRepoError;

error_text!(GithubRepoError => "a GitHub repository is <owner>/<name>");

impl FromStr for GithubRepo {
    type Err = GithubRepoError;

    fn from_str(text: &str) -> Result<Self, GithubRepoError> {
        let part = |part: &str| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        };
        if !text
            .split_once('/')
            .is_some_and(|(owner, name)| part(owner) && part(name))
        {
            return Err(GithubRepoError);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// The path of the lock file of a `pypi` server, from the root of the
    /// registry (contract 01b §3.4 rule 3): `[A-Za-z0-9][A-Za-z0-9._/-]{0,255}`,
    /// with no `..` segment, no empty segment and no `/` at the end.
    ///
    /// ```
    /// use creche_contracts::server::{LockPath, LockPathError};
    ///
    /// assert!("mcp/web-search/install.lock".parse::<LockPath>().is_ok());
    /// assert_eq!("../lock".parse::<LockPath>(), Err(LockPathError::Grammar));
    /// assert_eq!("mcp/../lock".parse::<LockPath>(), Err(LockPathError::NotPlain));
    /// ```
    LockPath
}

/// Why a text is not a lock path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockPathError {
    /// The text is not `[A-Za-z0-9][A-Za-z0-9._/-]{0,255}`.
    Grammar,
    /// The text holds a `..` segment, `//` or a `/` at the end.
    NotPlain,
}

impl fmt::Display for LockPathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Grammar => "a lock path is [A-Za-z0-9][A-Za-z0-9._/-]{0,255}",
            Self::NotPlain => "a lock path is a plain repository path",
        })
    }
}

impl Error for LockPathError {}

impl FromStr for LockPath {
    type Err = LockPathError;

    fn from_str(text: &str) -> Result<Self, LockPathError> {
        let bytes = text.as_bytes();
        let tail =
            |byte: &u8| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'/' | b'-');
        let grammar = bytes.first().is_some_and(u8::is_ascii_alphanumeric)
            && bytes.len() <= LOCK_PATH_MAX
            && bytes.iter().all(tail);
        if !grammar {
            return Err(LockPathError::Grammar);
        }

        if text.split('/').any(|part| part == "..") || text.ends_with('/') || text.contains("//") {
            return Err(LockPathError::NotPlain);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// The console script that starts a server: a name with no `/` (contract
    /// 01b §4).
    Entrypoint
}

/// Why a text is not an entrypoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EntrypointError;

error_text!(EntrypointError => "an entrypoint is a console script name with no path");

impl FromStr for Entrypoint {
    type Err = EntrypointError;

    fn from_str(text: &str) -> Result<Self, EntrypointError> {
        if text.is_empty() || text.contains('/') {
            return Err(EntrypointError);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// The argument that a fence reads: one name, or two names with `/`
    /// between them (contract 01b §7.2). A name is
    /// `[A-Za-z_][A-Za-z0-9_]{0,63}`.
    FenceArg
}

/// Why a text is not the argument of a fence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FenceArgError;

error_text!(FenceArgError => "a fence argument is one name, or two names joined by '/'");

impl FromStr for FenceArg {
    type Err = FenceArgError;

    fn from_str(text: &str) -> Result<Self, FenceArgError> {
        let name = |part: &str| {
            let bytes = part.as_bytes();
            let head = |byte: &u8| byte.is_ascii_alphabetic() || *byte == b'_';

            bytes.first().is_some_and(head)
                && bytes.len() <= ARG_NAME_MAX
                && bytes.iter().all(|byte| head(byte) || byte.is_ascii_digit())
        };
        if text.split('/').count() > ARG_PARTS_MAX || !text.split('/').all(name) {
            return Err(FenceArgError);
        }

        Ok(Self(text.to_owned()))
    }
}

/// The pin of a valid server file: the source, with the fields that only
/// that source holds (contract 01b §3). A `repo` on a `pypi` server is no
/// value of this type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pin {
    Pypi {
        package: String,
        version: PackageVersion,
        lock: LockPath,
    },
    GithubRelease {
        repo: GithubRepo,
        version: PackageVersion,
        asset: String,
        sha256: Sha256Hex,
    },
    /// The tag of the `mcp-servers` component is the version, so the file
    /// declares no pin.
    AgentMcp,
}

impl Pin {
    #[must_use]
    pub fn source(&self) -> InstallSource {
        match self {
            Self::Pypi { .. } => InstallSource::Pypi,
            Self::GithubRelease { .. } => InstallSource::GithubRelease,
            Self::AgentMcp => InstallSource::AgentMcp,
        }
    }
}

sealed! {
    /// `install` of a valid server file.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Install {
        pin: Pin,
        python: String,
    }
}

impl Install {
    #[must_use]
    pub fn pin(&self) -> &Pin {
        &self.pin
    }

    /// The Python version, as the file wrote it. The contract gives it no
    /// grammar.
    #[must_use]
    pub fn python(&self) -> &str {
        &self.python
    }
}

/// One value of `run.env` (contract 01b §4.1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EnvValue {
    /// The value itself. The name of its variable names no credential.
    Literal(String),
    /// `secret:<key>`: the key of a secret. The key is not empty.
    Secret(String),
}

/// The state directory of a server: none, or one whose path the server
/// reads from a variable (contract 01b §4.2). `state_dir: true` with no
/// variable is no value of this type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StateDir {
    None,
    /// The name of the variable, as the file wrote it.
    InVariable(String),
}

sealed! {
    /// `run` of a valid server file.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Run {
        entrypoint: Entrypoint,
        args: Vec<String>,
        env: BTreeMap<EnvName, EnvValue>,
        state_dir: StateDir,
    }
}

impl Run {
    #[must_use]
    pub fn entrypoint(&self) -> &Entrypoint {
        &self.entrypoint
    }

    #[must_use]
    pub fn args(&self) -> &[String] {
        &self.args
    }

    #[must_use]
    pub fn env(&self) -> &BTreeMap<EnvName, EnvValue> {
        &self.env
    }

    #[must_use]
    pub fn state_dir(&self) -> &StateDir {
        &self.state_dir
    }
}

sealed! {
    /// One tool that a valid server file declares (contract 01b §5).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Tool {
        name: ToolName,
        description: Description,
        write: bool,
    }
}

impl Tool {
    #[must_use]
    pub fn name(&self) -> &ToolName {
        &self.name
    }

    #[must_use]
    pub fn description(&self) -> &Description {
        &self.description
    }

    #[must_use]
    pub fn write(&self) -> bool {
        self.write
    }
}

/// The tools that one fence covers (contract 01b §7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FenceScope {
    /// Each tool that the file declares.
    All,
    /// These tools. The file declares each one.
    Named(Vec<ToolName>),
}

sealed! {
    /// One entry of `arg_allows` or of `arg_denies` of a valid server file.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Fence {
        tools: FenceScope,
        arg: FenceArg,
        values: Vec<String>,
    }
}

impl Fence {
    #[must_use]
    pub fn tools(&self) -> &FenceScope {
        &self.tools
    }

    #[must_use]
    pub fn arg(&self) -> &FenceArg {
        &self.arg
    }

    /// The values: one or more.
    #[must_use]
    pub fn values(&self) -> &[String] {
        &self.values
    }
}

sealed! {
    /// One secret that two servers name on purpose (contract 01b §4.3).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct SharedSecret {
        secret: String,
        server: ServerName,
    }
}

impl SharedSecret {
    /// The key of the secret. `run.env` of the file names it.
    #[must_use]
    pub fn secret(&self) -> &str {
        &self.secret
    }

    /// The other server. It is not the server of the file.
    #[must_use]
    pub fn server(&self) -> &ServerName {
        &self.server
    }
}

sealed! {
    /// One `server.yaml` that obeys each rule that one file can show.
    ///
    /// One rule is not in this type: the name is the name of the directory.
    /// `agent-family` checks it.
    #[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
    #[serde(try_from = "RawServer")]
    pub struct Server {
        name: ServerName,
        identity: Identity,
        install: Install,
        run: Run,
        tools: Vec<Tool>,
        arg_allows: Vec<Fence>,
        arg_denies: Vec<Fence>,
        shared_secrets: Vec<SharedSecret>,
    }
}

impl Server {
    #[must_use]
    pub fn name(&self) -> &ServerName {
        &self.name
    }

    #[must_use]
    pub fn identity(&self) -> &Identity {
        &self.identity
    }

    #[must_use]
    pub fn install(&self) -> &Install {
        &self.install
    }

    #[must_use]
    pub fn run(&self) -> &Run {
        &self.run
    }

    #[must_use]
    pub fn tools(&self) -> &[Tool] {
        &self.tools
    }

    #[must_use]
    pub fn arg_allows(&self) -> &[Fence] {
        &self.arg_allows
    }

    #[must_use]
    pub fn arg_denies(&self) -> &[Fence] {
        &self.arg_denies
    }

    #[must_use]
    pub fn shared_secrets(&self) -> &[SharedSecret] {
        &self.shared_secrets
    }
}

// --- the grammar of root ---

// Contract 01b §1: root reads each server file again at a release, and root
// accepts each TOML file that the validator accepts. The TOML path thus
// applies each rule of root below. The YAML path applies none of them. The
// contract gives no grammar for these texts and no cap for these counts. The
// numbers are those of the Python TOML path, which copies them from root.

/// The largest count of tools in a TOML file.
const TOOLS_MAX: usize = 200;

/// The largest count of words of `run.args` in a TOML file.
const ARGS_MAX: usize = 32;

/// The largest count of variables of `run.env` in a TOML file.
const ENV_MAX: usize = 32;

/// The largest count of entries of `arg_allows`, and of `arg_denies`, in a
/// TOML file.
const FENCES_MAX: usize = 64;

/// The largest count of values of one fence in a TOML file.
const FENCE_VALUES_MAX: usize = 256;

/// The largest count of entries of `shared_secrets` in a TOML file.
const SHARED_MAX: usize = 8;

/// The largest count of characters of one word of `run.args` in a TOML file.
const ARG_CHARS_MAX: usize = 256;

/// The largest count of characters of one value of `run.env` or of a fence
/// in a TOML file.
const VALUE_CHARS_MAX: usize = 256;

/// The Python versions that `install.python` of a TOML file names.
const PYTHONS: [&str; 2] = [DEFAULT_PYTHON, "3.13"];

/// One text of the grammar of root: a letter or a digit, then letters,
/// digits and the bytes of `more`, `max` bytes at most. Each byte is ASCII.
struct RootText {
    max: usize,
    more: &'static str,
}

impl RootText {
    fn holds(&self, text: &str) -> bool {
        let bytes = text.as_bytes();
        let tail = |byte: &u8| byte.is_ascii_alphanumeric() || self.more.as_bytes().contains(byte);

        bytes.first().is_some_and(u8::is_ascii_alphanumeric)
            && bytes.len() <= self.max
            && bytes.iter().all(tail)
    }

    /// The grammar as the text of a pattern, for a message.
    fn form(&self) -> String {
        let most = self.max.saturating_sub(1);

        format!("[A-Za-z0-9][A-Za-z0-9{}]{{0,{most}}}", self.more)
    }
}

/// `install.package`: `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
const PACKAGE: RootText = RootText {
    max: 64,
    more: "._-",
};

/// `install.asset`: `[A-Za-z0-9][A-Za-z0-9._+-]{0,127}`.
const ASSET: RootText = RootText {
    max: 128,
    more: "._+-",
};

/// `run.entrypoint`: `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
const ENTRYPOINT: RootText = RootText {
    max: 64,
    more: "._-",
};

/// The owner of `install.repo`: `[A-Za-z0-9][A-Za-z0-9._-]{0,38}`.
const REPO_OWNER: RootText = RootText {
    max: 39,
    more: "._-",
};

/// The name of `install.repo`: `[A-Za-z0-9][A-Za-z0-9._-]{0,99}`.
const REPO_NAME: RootText = RootText {
    max: 100,
    more: "._-",
};

/// Whether `text` is a repository in the grammar of root: an owner, `/` and
/// a name.
fn root_repo(text: &str) -> bool {
    text.split_once('/')
        .is_some_and(|(owner, name)| REPO_OWNER.holds(owner) && REPO_NAME.holds(name))
}

/// What the TOML path finds in an identity or in a description.
enum Counted {
    /// 1 to the cap of characters, and not only white space.
    Fits,
    /// The count of characters is 0 or past the cap.
    Outside(usize),
    /// Only white space.
    Blank,
}

/// The rule of the TOML path for an identity and a description: each
/// character counts, and a text of white space only is no text.
fn counted(text: &str, max: usize) -> Counted {
    let length = text.chars().count();
    if !(1..=max).contains(&length) {
        return Counted::Outside(length);
    }

    if pytext::words(text).next().is_none() {
        return Counted::Blank;
    }

    Counted::Fits
}

/// A cap of root on the count of the entries of one list or one table.
/// `false` and one issue when `count` is past the cap.
fn vet_count(at: Slot, loc: &str, count: usize, cap: usize, out: &mut Issues) -> bool {
    if count <= cap {
        return true;
    }

    out.error(
        at,
        loc,
        format!("{loc} holds {count} entries; it may hold {cap} at most"),
    );

    false
}

/// A text that one rule of root holds: a word of `run.args`, or a value of
/// `run.env` or of a fence.
#[derive(Debug, Clone, Copy)]
enum Plain {
    Word,
    Value,
}

impl Plain {
    /// What a message calls the text.
    fn noun(self) -> &'static str {
        match self {
            Self::Word => "the word",
            Self::Value => "the value",
        }
    }

    fn cap(self) -> usize {
        match self {
            Self::Word => ARG_CHARS_MAX,
            Self::Value => VALUE_CHARS_MAX,
        }
    }
}

/// The rule of root for a plain text: a cap on its characters, and no NUL
/// character. `false` and one issue for each rule that the text breaks.
fn vet_plain(at: Slot, loc: &str, plain: Plain, text: &str, out: &mut Issues) -> bool {
    let (what, cap) = (plain.noun(), plain.cap());
    let length = text.chars().count();
    let short = length <= cap;
    if !short {
        out.error(
            at,
            loc,
            format!("{what} is {length} characters; it may have {cap} at most"),
        );
    }

    let clean = !text.contains('\0');
    if !clean {
        out.error(at, loc, format!("{what} holds a NUL character"));
    }

    short && clean
}

// --- the conversion ---

fn vet_name(raw: &RawServer, out: &mut Issues) -> Option<ServerName> {
    let name = raw.name.parse::<ServerName>().ok();
    if name.is_none() {
        out.error(
            Slot::of(Section::ServerName),
            "name",
            format!(
                "'{}' is not a server name; use [a-z][a-z0-9-]{{1,30}}, hyphens and never an \
                 underscore",
                raw.name
            ),
        );
    }

    name
}

fn vet_identity(raw: &RawServer, markup: Markup, out: &mut Issues) -> Option<Identity> {
    let text = &raw.identity;
    let count = |length: usize| {
        format!(
            "identity is {length} characters; it must be 1 to {IDENTITY_MAX} and say whose \
             credential this server holds"
        )
    };
    let problem = match markup {
        Markup::Yaml => match text.parse::<Identity>() {
            Ok(identity) => return Some(identity),
            Err(IdentityError { length }) => count(length),
        },
        // A text that fits has 1 to 300 characters after the collapse too, so
        // `Identity` takes it.
        Markup::Toml => match counted(text, IDENTITY_MAX) {
            Counted::Fits => return text.parse::<Identity>().ok(),
            Counted::Outside(length) => count(length),
            Counted::Blank => "identity holds only white space; it must say whose credential \
                               this server holds"
                .to_owned(),
        },
    };
    out.error(Slot::of(Section::ServerName).step(2), "identity", problem);

    None
}

/// The pin fields that the file wrote, by name.
fn pin_fields(install: &RawInstall) -> [(&'static str, Option<&String>); 7] {
    [
        ("package", install.package.as_ref()),
        ("version", install.version.as_ref()),
        ("lock", install.lock.as_ref()),
        ("repo", install.repo.as_ref()),
        ("asset", install.asset.as_ref()),
        ("sha256", install.sha256.as_ref()),
        ("ref", install.git_ref.as_ref()),
    ]
}

fn vet_version(install: &RawInstall, at: Slot, out: &mut Issues) -> Option<PackageVersion> {
    let text = install.version.as_ref()?;
    match text.parse::<PackageVersion>() {
        Ok(version) => Some(version),
        Err(_) => {
            // The message shows the form that `ids::PackageVersion` holds, as
            // the message of the Python validator does.
            let form = PackageVersion::form();
            out.error(
                at,
                "install.version",
                format!("'{text}' is not an exact version; use {form}"),
            );

            None
        }
    }
}

fn vet_lock(install: &RawInstall, at: Slot, out: &mut Issues) -> Option<LockPath> {
    let text = install.lock.as_ref()?;
    match text.parse::<LockPath>() {
        Ok(lock) => Some(lock),
        Err(LockPathError::Grammar) => {
            let shown: String = collapse(text).chars().take(LOCK_SHOWN_MAX).collect();
            out.error(
                at,
                "install.lock",
                format!("'{shown}' is not a repository path"),
            );

            None
        }
        Err(LockPathError::NotPlain) => {
            out.error(
                at,
                "install.lock",
                "install.lock is not a plain repository path",
            );

            None
        }
    }
}

fn vet_repo(
    install: &RawInstall,
    markup: Markup,
    at: Slot,
    out: &mut Issues,
) -> Option<GithubRepo> {
    let text = install.repo.as_ref()?;
    // The grammar of root is narrower than `GithubRepo`, so a text that it
    // takes is a `GithubRepo` too.
    let root = match markup {
        Markup::Toml => root_repo(text),
        Markup::Yaml => true,
    };
    let repo = text.parse::<GithubRepo>().ok().filter(|_| root);
    if repo.is_none() {
        out.error(
            at,
            "install.repo",
            format!("'{text}' is not <owner>/<name> on GitHub"),
        );
    }

    repo
}

/// The TOML path only: the rules of root for `package`, `asset` and
/// `python`. `false` when one of them gives an issue.
fn vet_root_pins(install: &RawInstall, at: Slot, out: &mut Issues) -> bool {
    let mut valid = true;
    if let Some(package) = install.package.as_ref().filter(|text| !PACKAGE.holds(text)) {
        valid = false;
        let form = PACKAGE.form();
        out.error(
            at,
            "install.package",
            format!("'{package}' is not a package name; use {form}"),
        );
    }

    if let Some(asset) = install.asset.as_ref().filter(|text| !ASSET.holds(text)) {
        valid = false;
        let form = ASSET.form();
        out.error(
            at,
            "install.asset",
            format!("'{asset}' is not a release asset name; use {form}"),
        );
    }

    if !PYTHONS.contains(&install.python.as_str()) {
        valid = false;
        out.error(
            at,
            "install.python",
            format!(
                "'{}' is not a Python version in use; use one of {}",
                install.python,
                PYTHONS.join(", ")
            ),
        );
    }

    valid
}

fn vet_install(raw: &RawServer, markup: Markup, out: &mut Issues) -> Option<Install> {
    let at = Slot::of(Section::ServerInstall);
    let install = &raw.install;
    let Some(source) = InstallSource::parse(&install.source) else {
        let allowed = InstallSource::ALL.map(InstallSource::as_str).join(", ");
        out.error(
            at,
            "install.source",
            format!("'{}' is not a source; use one of {allowed}", install.source),
        );

        return None;
    };

    let mut valid = true;
    let fields = pin_fields(install);
    let present = |wanted: &str| {
        fields
            .iter()
            .any(|(name, value)| *name == wanted && value.is_some())
    };
    for name in source.required() {
        if !present(name) {
            valid = false;
            out.error(
                at,
                format!("install.{name}"),
                format!("source '{source}' requires 'install.{name}'"),
            );
        }
    }

    let mut extra: Vec<&str> = fields
        .iter()
        .filter(|(name, value)| value.is_some() && !source.required().contains(name))
        .map(|(name, _)| *name)
        .collect();
    extra.sort_unstable();
    for name in extra {
        valid = false;
        out.error(
            at,
            format!("install.{name}"),
            format!("source '{source}' does not take 'install.{name}'"),
        );
    }

    let version = vet_version(install, at, out);
    let sha256 = install.sha256.as_ref().and_then(|text| {
        let digest = text.parse::<Sha256Hex>().ok();
        if digest.is_none() {
            out.error(
                at,
                "install.sha256",
                "sha256 must be 64 lowercase hexadecimal characters",
            );
        }

        digest
    });
    let repo = vet_repo(install, markup, at, out);
    let lock = vet_lock(install, at, out);
    let root = match markup {
        Markup::Toml => vet_root_pins(install, at, out),
        Markup::Yaml => true,
    };
    let pin = match source {
        InstallSource::Pypi => Pin::Pypi {
            package: install.package.clone()?,
            version: version?,
            lock: lock?,
        },
        InstallSource::GithubRelease => Pin::GithubRelease {
            repo: repo?,
            version: version?,
            asset: install.asset.clone()?,
            sha256: sha256?,
        },
        InstallSource::AgentMcp => Pin::AgentMcp,
    };

    (valid && root).then(|| Install {
        pin,
        python: install.python.clone(),
    })
}

/// The key of a secret: the text after `secret:`. The TOML path takes the
/// grammar of `ids::SecretName`, and the YAML path takes each text that is
/// not empty.
fn vet_secret_key(key: &str, markup: Markup, at: Slot, loc: &str, out: &mut Issues) -> bool {
    match markup {
        Markup::Toml if key.parse::<SecretName>().is_err() => {
            let form = SecretName::form();
            out.error(
                at,
                loc,
                format!("the key after '{SECRET_PREFIX}' is not a secret key; use {form}"),
            );

            false
        }
        Markup::Yaml if key.is_empty() => {
            out.error(at, loc, "'secret:' names no key");

            false
        }
        Markup::Toml | Markup::Yaml => true,
    }
}

fn vet_env_entry(
    (name, value): (&str, &str),
    markup: Markup,
    at: Slot,
    out: &mut Issues,
) -> Option<(EnvName, EnvValue)> {
    let loc = format!("run.env.{name}");
    let env_name = match name.parse::<EnvName>() {
        Ok(env_name) => Some(env_name),
        Err(_) => {
            // The message shows the form that `ids::EnvName` holds, as the
            // message of the Python validator does.
            let form = EnvName::form();
            out.error(
                at,
                &loc,
                format!("'{name}' is not an environment variable name; use {form}"),
            );

            None
        }
    };
    let plain = match markup {
        Markup::Toml => vet_plain(at, &loc, Plain::Value, value, out),
        Markup::Yaml => true,
    };
    let key = value.strip_prefix(SECRET_PREFIX);
    let secret_shaped = SECRET_NAME_MARKERS
        .iter()
        .any(|marker| name.contains(marker));
    let env_value = match key {
        None if secret_shaped => {
            out.error(
                at,
                loc,
                format!(
                    "'{name}' names a credential, so its value must be 'secret:<key>'; a literal \
                     here puts a credential in git"
                ),
            );

            None
        }
        None => Some(EnvValue::Literal(value.to_owned())),
        Some(key) => {
            vet_secret_key(key, markup, at, &loc, out).then(|| EnvValue::Secret(key.to_owned()))
        }
    };

    Some((env_name?, env_value.filter(|_| plain)?))
}

/// The entrypoint of the YAML path: a name with no `/`.
fn vet_entrypoint(run: &RawRun, at: Slot, out: &mut Issues) -> Option<Entrypoint> {
    let entrypoint = run.entrypoint.parse::<Entrypoint>().ok();
    if entrypoint.is_none() {
        out.error(
            at,
            "run.entrypoint",
            format!(
                "'{}' must be a console script name with no path; the PEP builds the absolute \
                 path from install.source",
                run.entrypoint
            ),
        );
    }

    entrypoint
}

/// The TOML path only: the rules of root for the entrypoint, for `args` and
/// for the count of `env`. The second value is `false` when a rule of `args`
/// or of `env` gives an issue.
fn vet_root_run(run: &RawRun, at: Slot, out: &mut Issues) -> (Option<Entrypoint>, bool) {
    let text = &run.entrypoint;
    // The grammar of root is narrower than `Entrypoint`, so a text that it
    // takes is an `Entrypoint` too.
    let entrypoint = text
        .parse::<Entrypoint>()
        .ok()
        .filter(|_| ENTRYPOINT.holds(text));
    if entrypoint.is_none() {
        let form = ENTRYPOINT.form();
        out.error(
            at,
            "run.entrypoint",
            format!("'{text}' is not a console script name; use {form}"),
        );
    }

    let mut valid = vet_count(at, "run.args", run.args.len(), ARGS_MAX, out);
    for (index, word) in run.args.iter().enumerate() {
        let loc = format!("run.args[{index}]");
        valid = vet_plain(at, &loc, Plain::Word, word, out) && valid;
    }

    let env = vet_count(at, "run.env", run.env.len(), ENV_MAX, out);

    (entrypoint, valid && env)
}

fn vet_run(raw: &RawServer, markup: Markup, out: &mut Issues) -> Option<Run> {
    let at = Slot::of(Section::ServerRun);
    let run = &raw.run;
    let (entrypoint, mut valid) = match markup {
        Markup::Toml => vet_root_run(run, at, out),
        Markup::Yaml => (vet_entrypoint(run, at, out), true),
    };

    let mut env = BTreeMap::new();
    for (name, value) in &run.env {
        match vet_env_entry((name, value), markup, at, out) {
            Some((name, value)) => {
                env.insert(name, value);
            }
            None => valid = false,
        }
    }

    let state_dir = match (run.state_dir, &run.state_dir_env) {
        (false, None) => Some(StateDir::None),
        (true, Some(variable)) => Some(StateDir::InVariable(variable.clone())),
        (true, None) => {
            out.error(
                at,
                "run.state_dir_env",
                "state_dir: true requires 'run.state_dir_env'",
            );

            None
        }
        (false, Some(_)) => {
            out.error(
                at,
                "run.state_dir_env",
                "state_dir_env needs 'run.state_dir: true'",
            );

            None
        }
    };
    if let (Markup::Toml, Some(variable)) = (markup, &run.state_dir_env)
        && variable.parse::<EnvName>().is_err()
    {
        valid = false;
        let form = EnvName::form();
        out.error(
            at,
            "run.state_dir_env",
            format!("'{variable}' is not an environment variable name; use {form}"),
        );
    }

    valid.then_some(Run {
        entrypoint: entrypoint?,
        args: run.args.clone(),
        env,
        state_dir: state_dir?,
    })
}

fn vet_description(
    text: &str,
    markup: Markup,
    at: Slot,
    loc: &str,
    out: &mut Issues,
) -> Option<Description> {
    let count = |length: usize| {
        format!("description is {length} characters; it must be 1 to {DESCRIPTION_MAX}")
    };
    let problem = match markup {
        Markup::Yaml => match text.parse::<Description>() {
            Ok(description) => return Some(description),
            Err(DescriptionError { length }) => count(length),
        },
        // A text that fits has 1 to 200 characters after the collapse too, so
        // `Description` takes it.
        Markup::Toml => match counted(text, DESCRIPTION_MAX) {
            Counted::Fits => return text.parse::<Description>().ok(),
            Counted::Outside(length) => count(length),
            Counted::Blank => "description holds only white space".to_owned(),
        },
    };
    out.error(at, loc, problem);

    None
}

fn vet_tools(raw: &RawServer, markup: Markup, out: &mut Issues) -> Option<Vec<Tool>> {
    let at = Slot::of(Section::ServerTools);
    let mut valid = match markup {
        Markup::Toml => vet_count(at, "tools", raw.tools.len(), TOOLS_MAX, out),
        Markup::Yaml => true,
    };
    let mut tools = Vec::new();
    let mut seen = BTreeSet::new();
    for (index, entry) in raw.tools.iter().enumerate() {
        let loc = format!("tools[{index}]");
        let name = match entry.name.parse::<ToolName>() {
            Ok(name) => Some(name),
            Err(_) => {
                out.error(at, &loc, tool_name_issue(&entry.name));

                None
            }
        };
        let twice = !seen.insert(entry.name.as_str());
        if twice {
            out.error(
                at,
                &loc,
                format!("'{}' is declared twice in this file", entry.name),
            );
        }

        let description = vet_description(&entry.description, markup, at, &loc, out);
        match (name, description) {
            (Some(name), Some(description)) if !twice => tools.push(Tool {
                name,
                description,
                write: entry.write,
            }),
            _ => valid = false,
        }
    }

    valid.then_some(tools)
}

/// The TOML path only: the rules of root for the values of one fence.
/// `false` when one of them gives an issue.
fn vet_fence_values(entry: &RawFence, loc: &str, at: Slot, out: &mut Issues) -> bool {
    let at_values = format!("{loc}.values");
    let count = entry.values.len();
    let mut valid = vet_count(at, &at_values, count, FENCE_VALUES_MAX, out);
    for (index, value) in entry.values.iter().enumerate() {
        let loc = format!("{loc}.values[{index}]");
        valid = vet_plain(at, &loc, Plain::Value, value, out) && valid;
    }

    valid
}

fn vet_fence(
    raw: &RawServer,
    entry: &RawFence,
    loc: &str,
    markup: Markup,
    out: &mut Issues,
) -> Option<Fence> {
    let at = Slot::of(Section::ServerFences);
    let tools = match &entry.tools {
        // Contract 01b §7.1: `all` covers the tools that the file declares,
        // and a file with no tool gives it nothing to cover.
        RawToolGrant::Text(scope) if scope == ALL_TOOLS && raw.tool_names().next().is_none() => {
            out.error(
                at,
                format!("{loc}.tools"),
                format!("'{ALL_TOOLS}' needs at least one tool in this file's 'tools'"),
            );

            None
        }
        RawToolGrant::Text(scope) if scope == ALL_TOOLS => Some(FenceScope::All),
        RawToolGrant::Text(scope) => {
            out.error(
                at,
                format!("{loc}.tools"),
                format!("'{scope}' must be a list of tool names or '{ALL_TOOLS}'"),
            );

            None
        }
        // A rule of root: a list of tools holds one tool at least.
        RawToolGrant::Named(scope) if scope.is_empty() && markup == Markup::Toml => {
            out.error(
                at,
                format!("{loc}.tools"),
                format!("a fence entry needs at least one tool; use '{ALL_TOOLS}' or name a tool"),
            );

            None
        }
        RawToolGrant::Named(scope) => {
            let mut unknown: Vec<&str> = scope
                .iter()
                .map(String::as_str)
                .filter(|name| !raw.tool_names().any(|declared| declared == *name))
                .collect();
            if unknown.is_empty() {
                // A tool that the file declares with a name that is no tool
                // name has its own issue.
                scope
                    .iter()
                    .map(|name| name.parse::<ToolName>().ok())
                    .collect::<Option<Vec<_>>>()
                    .map(FenceScope::Named)
            } else {
                unknown.sort_unstable();
                out.error(
                    at,
                    format!("{loc}.tools"),
                    format!(
                        "{} are not declared in this file's 'tools'",
                        unknown.join(", ")
                    ),
                );

                None
            }
        }
    };
    let arg = entry.arg.parse::<FenceArg>().ok();
    if arg.is_none() {
        out.error(
            at,
            format!("{loc}.arg"),
            format!(
                "'{}' must be one argument name, or two joined by '/'",
                entry.arg
            ),
        );
    }

    if entry.values.is_empty() {
        out.error(
            at,
            format!("{loc}.values"),
            "a fence entry needs at least one value",
        );

        return None;
    }

    let values = match markup {
        Markup::Toml => vet_fence_values(entry, loc, at, out),
        Markup::Yaml => true,
    };

    values.then_some(Fence {
        tools: tools?,
        arg: arg?,
        values: entry.values.clone(),
    })
}

fn vet_fences(
    raw: &RawServer,
    field: &str,
    entries: &[RawFence],
    markup: Markup,
    out: &mut Issues,
) -> Option<Vec<Fence>> {
    let at = Slot::of(Section::ServerFences);
    let counted = match markup {
        Markup::Toml => vet_count(at, field, entries.len(), FENCES_MAX, out),
        Markup::Yaml => true,
    };
    let fences: Vec<Option<Fence>> = entries
        .iter()
        .enumerate()
        .map(|(index, entry)| vet_fence(raw, entry, &format!("{field}[{index}]"), markup, out))
        .collect();

    fences
        .into_iter()
        .collect::<Option<Vec<_>>>()
        .filter(|_| counted)
}

fn vet_shared(raw: &RawServer, markup: Markup, out: &mut Issues) -> Option<Vec<SharedSecret>> {
    let at = Slot::of(Section::ServerShared);
    let named: BTreeSet<&str> = raw
        .run
        .env
        .values()
        .filter_map(|value| value.strip_prefix(SECRET_PREFIX))
        .collect();
    let mut valid = match markup {
        Markup::Toml => vet_count(
            at,
            "shared_secrets",
            raw.shared_secrets.len(),
            SHARED_MAX,
            out,
        ),
        Markup::Yaml => true,
    };
    let mut shared = Vec::new();
    let mut seen = BTreeSet::new();
    for (index, entry) in raw.shared_secrets.iter().enumerate() {
        let loc = format!("shared_secrets[{index}]");
        let mut entry_valid = true;
        if !named.contains(entry.secret.as_str()) {
            entry_valid = false;
            out.error(
                at,
                &loc,
                format!(
                    "'{}' is not a secret this file's run.env names",
                    entry.secret
                ),
            );
        }

        if entry.server == raw.name {
            entry_valid = false;
            out.error(
                at,
                &loc,
                "a server cannot agree with itself; name the OTHER server",
            );
        }

        let server = entry.server.parse::<ServerName>().ok();
        if server.is_none() {
            out.error(at, &loc, format!("'{}' is not a server name", entry.server));
        }

        let twice = !seen.insert(entry.secret.as_str());
        if twice {
            entry_valid = false;
            out.error(
                at,
                loc,
                format!(
                    "'{}' is shared twice; one secret has one partner",
                    entry.secret
                ),
            );
        }

        match server {
            Some(server) if entry_valid => shared.push(SharedSecret {
                secret: entry.secret.clone(),
                server,
            }),
            _ => valid = false,
        }
    }

    valid.then_some(shared)
}

impl Server {
    /// Checks each rule that one file decides (contract 01b), with the
    /// grammar of `markup`. The conversion reads each field before it
    /// answers.
    ///
    /// A TOML file also gets each rule of root that the YAML path does not
    /// hold. A YAML file keeps the rules of the Python YAML path.
    ///
    /// # Errors
    ///
    /// [`Refused`] with each issue, when a rule gives an error.
    pub fn from_raw(raw: RawServer, markup: Markup) -> Result<Self, Refused> {
        let mut out = Issues::default();
        let name = vet_name(&raw, &mut out);
        let identity = vet_identity(&raw, markup, &mut out);
        let install = vet_install(&raw, markup, &mut out);
        let run = vet_run(&raw, markup, &mut out);
        let tools = vet_tools(&raw, markup, &mut out);
        let arg_allows = vet_fences(&raw, "arg_allows", &raw.arg_allows, markup, &mut out);
        let arg_denies = vet_fences(&raw, "arg_denies", &raw.arg_denies, markup, &mut out);
        let shared_secrets = vet_shared(&raw, markup, &mut out);
        let failed = out.has_error();
        let mut issues: Vec<Placed> = out.into_entries();
        issues.sort_by_key(|placed| placed.slot);
        let server = (|| {
            Some(Self {
                name: name?,
                identity: identity?,
                install: install?,
                run: run?,
                tools: tools?,
                arg_allows: arg_allows?,
                arg_denies: arg_denies?,
                shared_secrets: shared_secrets?,
            })
        })();
        match server {
            Some(server) if !failed => Ok(server),
            _ => Err(Refused::new(issues)),
        }
    }
}

impl TryFrom<RawServer> for Server {
    type Error = Refused;

    /// Checks each rule that one YAML file decides: [`Server::from_raw`]
    /// with [`Markup::Yaml`].
    fn try_from(raw: RawServer) -> Result<Self, Refused> {
        Self::from_raw(raw, Markup::Yaml)
    }
}

// --- back to the raw form ---

fn raw_fence(fence: &Fence) -> RawFence {
    RawFence {
        tools: match &fence.tools {
            FenceScope::All => RawToolGrant::Text(ALL_TOOLS.to_owned()),
            FenceScope::Named(tools) => {
                RawToolGrant::Named(tools.iter().map(|tool| tool.as_str().to_owned()).collect())
            }
        },
        arg: fence.arg.as_str().to_owned(),
        values: fence.values.clone(),
    }
}

fn raw_install(install: &Install) -> RawInstall {
    let empty = RawInstall {
        source: install.pin.source().as_str().to_owned(),
        package: None,
        version: None,
        lock: None,
        python: install.python.clone(),
        repo: None,
        asset: None,
        sha256: None,
        git_ref: None,
    };
    match &install.pin {
        Pin::Pypi {
            package,
            version,
            lock,
        } => RawInstall {
            package: Some(package.clone()),
            version: Some(version.as_str().to_owned()),
            lock: Some(lock.as_str().to_owned()),
            ..empty
        },
        Pin::GithubRelease {
            repo,
            version,
            asset,
            sha256,
        } => RawInstall {
            repo: Some(repo.as_str().to_owned()),
            version: Some(version.as_str().to_owned()),
            asset: Some(asset.clone()),
            sha256: Some(sha256.as_str().to_owned()),
            ..empty
        },
        Pin::AgentMcp => empty,
    }
}

impl From<&Server> for RawServer {
    /// The file that the conversion took, field for field.
    fn from(server: &Server) -> Self {
        let (state_dir, state_dir_env) = match &server.run.state_dir {
            StateDir::None => (false, None),
            StateDir::InVariable(variable) => (true, Some(variable.clone())),
        };

        Self {
            name: server.name.as_str().to_owned(),
            identity: server.identity.as_str().to_owned(),
            install: raw_install(&server.install),
            run: RawRun {
                entrypoint: server.run.entrypoint.as_str().to_owned(),
                args: server.run.args.clone(),
                env: server
                    .run
                    .env
                    .iter()
                    .map(|(name, value)| {
                        let value = match value {
                            EnvValue::Literal(text) => text.clone(),
                            EnvValue::Secret(key) => format!("{SECRET_PREFIX}{key}"),
                        };

                        (name.as_str().to_owned(), value)
                    })
                    .collect(),
                state_dir,
                state_dir_env,
            },
            tools: server
                .tools
                .iter()
                .map(|tool| RawTool {
                    name: tool.name.as_str().to_owned(),
                    description: tool.description.as_str().to_owned(),
                    write: tool.write,
                })
                .collect(),
            arg_allows: server.arg_allows.iter().map(raw_fence).collect(),
            arg_denies: server.arg_denies.iter().map(raw_fence).collect(),
            shared_secrets: server
                .shared_secrets
                .iter()
                .map(|shared| RawSharedSecret {
                    secret: shared.secret.clone(),
                    server: shared.server.as_str().to_owned(),
                })
                .collect(),
        }
    }
}

// --- the TOML file ---

/// `server.toml`: the bytes of one file to a [`RawServer`], or to each fault
/// of its shape.
///
/// The read and the check are those of `family.toml`
/// (`RawFamily::from_toml` of [`crate::family`]). [`crate::tomlfile`] reads
/// the bytes into one private raw type. The raw type is total: each field is
/// a lenient field, so the read fails for no value. The check then reads each
/// field as the Python model `McpServerFile` reads a TOML text, in the strict
/// mode of pydantic: a field takes the kind of its type and no other. It
/// reports each field in the order of the model, and then each key that the
/// model does not name, in sorted order. The reader gives the keys of
/// `run.env` in sorted order, so `run.env` reports its faults in that order
/// too.
///
/// A fault holds no text of a message. The validator in the crate
/// `agent-family` writes the message of each fault.
#[cfg(feature = "tomlfile")]
mod markup {
    use std::collections::BTreeMap;

    use serde::Deserialize;

    use super::{
        DEFAULT_PYTHON, RawFence, RawInstall, RawRun, RawServer, RawSharedSecret, RawTool,
    };
    use crate::family::{
        Block, Grant, LocPart, RawToolGrant, ShapeProblem, TomlRefusal, Walk, Wants, grant_of,
        owned, table, texts,
    };
    use crate::slot::{Nested, Slot as Field};
    use crate::tomlfile::{self, FileKind, Unknown};
    use Wants::{Boolean, List, Map, Text};

    impl RawServer {
        /// The raw server file of the bytes of one `server.toml`, with each
        /// default.
        ///
        /// # Errors
        ///
        /// [`TomlRefusal::Unread`] when the TOML reader refuses the bytes, and
        /// [`TomlRefusal::Shape`] with each fault of the shape.
        pub fn from_toml(bytes: &[u8]) -> Result<Self, TomlRefusal> {
            let raw: TomlServer =
                tomlfile::read(bytes, FileKind::Server).map_err(TomlRefusal::Unread)?;
            let mut walk = Walk::default();
            let server = server_of(&raw, &mut walk);

            walk.finish(server)
        }
    }

    // --- the raw type ---

    /// One `server.toml`, as the file wrote it. Each field of the Python
    /// model `McpServerFile` is here, and `unknown` takes each other key.
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct TomlServer {
        name: Field<String>,
        identity: Field<String>,
        install: Field<TomlInstall>,
        run: Field<TomlRun>,
        tools: Field<Vec<Field<TomlTool>>>,
        arg_allows: Field<Vec<Field<TomlFence>>>,
        arg_denies: Field<Vec<Field<TomlFence>>>,
        shared_secrets: Field<Vec<Field<TomlShared>>>,
        #[serde(flatten)]
        unknown: Unknown,
    }

    /// `install`.
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct TomlInstall {
        source: Field<String>,
        package: Field<String>,
        version: Field<String>,
        lock: Field<String>,
        python: Field<String>,
        repo: Field<String>,
        asset: Field<String>,
        sha256: Field<String>,
        #[serde(rename = "ref")]
        git_ref: Field<String>,
        #[serde(flatten)]
        unknown: Unknown,
    }

    /// `run`. `env` is a table with the keys of the author.
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct TomlRun {
        entrypoint: Field<String>,
        args: Field<Vec<Field<String>>>,
        env: Field<BTreeMap<String, Field<String>>>,
        state_dir: Field<bool>,
        state_dir_env: Field<String>,
        #[serde(flatten)]
        unknown: Unknown,
    }

    /// One item of `tools`.
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct TomlTool {
        name: Field<String>,
        description: Field<String>,
        write: Field<bool>,
        #[serde(flatten)]
        unknown: Unknown,
    }

    /// One item of `arg_allows` or of `arg_denies`.
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct TomlFence {
        tools: Field<Grant>,
        arg: Field<String>,
        values: Field<Vec<Field<String>>>,
        #[serde(flatten)]
        unknown: Unknown,
    }

    /// One item of `shared_secrets`.
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct TomlShared {
        secret: Field<String>,
        server: Field<String>,
        #[serde(flatten)]
        unknown: Unknown,
    }

    impl Nested for TomlInstall {}
    impl Nested for TomlRun {}
    impl Nested for TomlTool {}
    impl Nested for TomlFence {}
    impl Nested for TomlShared {}

    // --- the check of the shape ---

    fn install_of(raw: &TomlInstall, walk: &mut Walk) -> Option<RawInstall> {
        let source = walk.required("source", &raw.source, Text, owned);
        let package = walk.optional("package", &raw.package, Text, owned);
        let version = walk.optional("version", &raw.version, Text, owned);
        let lock = walk.optional("lock", &raw.lock, Text, owned);
        let python = walk.optional("python", &raw.python, Text, owned);
        let repo = walk.optional("repo", &raw.repo, Text, owned);
        let asset = walk.optional("asset", &raw.asset, Text, owned);
        let sha256 = walk.optional("sha256", &raw.sha256, Text, owned);
        let git_ref = walk.optional("ref", &raw.git_ref, Text, owned);
        walk.unknown(&raw.unknown)?;

        Some(RawInstall {
            source: source?,
            package: package?,
            version: version?,
            lock: lock?,
            python: python?.unwrap_or_else(|| DEFAULT_PYTHON.to_owned()),
            repo: repo?,
            asset: asset?,
            sha256: sha256?,
            git_ref: git_ref?,
        })
    }

    /// `run.env`: each value is a text. Each key gives its own fault, in the
    /// order of the keys.
    fn env_of(
        env: &BTreeMap<String, Field<String>>,
        walk: &mut Walk,
    ) -> Option<BTreeMap<String, String>> {
        let read: Vec<Option<(String, String)>> = env
            .iter()
            .map(|(name, value)| {
                let value = walk.at(LocPart::Key(name.clone()), |walk| {
                    walk.held(value, Text).cloned()
                });

                value.map(|value| (name.clone(), value))
            })
            .collect();

        read.into_iter().collect()
    }

    fn run_of(raw: &TomlRun, walk: &mut Walk) -> Option<RawRun> {
        let entrypoint = walk.required("entrypoint", &raw.entrypoint, Text, owned);
        let args = walk.optional("args", &raw.args, List, |l, w| texts(l, w));
        let env = walk.optional("env", &raw.env, Map, env_of);
        let state_dir = walk.optional("state_dir", &raw.state_dir, Boolean, owned);
        let state_dir_env = walk.optional("state_dir_env", &raw.state_dir_env, Text, owned);
        walk.unknown(&raw.unknown)?;

        Some(RawRun {
            entrypoint: entrypoint?,
            args: args?.unwrap_or_default(),
            env: env?.unwrap_or_default(),
            state_dir: state_dir?.unwrap_or(false),
            state_dir_env: state_dir_env?,
        })
    }

    fn tool_of(raw: &TomlTool, walk: &mut Walk) -> Option<RawTool> {
        let name = walk.required("name", &raw.name, Text, owned);
        let description = walk.required("description", &raw.description, Text, owned);
        let write = walk.optional("write", &raw.write, Boolean, owned);
        walk.unknown(&raw.unknown)?;

        Some(RawTool {
            name: name?,
            description: description?,
            write: write?.unwrap_or(false),
        })
    }

    /// `tools` of a fence: the union `list[str] | str` of the Python model,
    /// with no default.
    fn scope_of(tools: &Field<Grant>, walk: &mut Walk) -> Option<RawToolGrant> {
        walk.at(LocPart::Key("tools".to_owned()), |walk| match tools {
            Field::Missing => {
                walk.fault(ShapeProblem::Missing);

                None
            }
            Field::Null | Field::Other(_) | Field::Value(_) => grant_of(tools, walk),
        })
    }

    fn fence_of(raw: &TomlFence, walk: &mut Walk) -> Option<RawFence> {
        let tools = scope_of(&raw.tools, walk);
        let arg = walk.required("arg", &raw.arg, Text, owned);
        let values = walk.required("values", &raw.values, List, |l, w| texts(l, w));
        walk.unknown(&raw.unknown)?;

        Some(RawFence {
            tools: tools?,
            arg: arg?,
            values: values?,
        })
    }

    fn shared_of(raw: &TomlShared, walk: &mut Walk) -> Option<RawSharedSecret> {
        let secret = walk.required("secret", &raw.secret, Text, owned);
        let server = walk.required("server", &raw.server, Text, owned);
        walk.unknown(&raw.unknown)?;

        Some(RawSharedSecret {
            secret: secret?,
            server: server?,
        })
    }

    /// The Python model `McpServerFile`: each field in the order of the
    /// model.
    fn server_of(raw: &TomlServer, walk: &mut Walk) -> Option<RawServer> {
        let (tool, fence) = (table(Block::Tool), table(Block::Fence));
        let shared = table(Block::SharedSecret);
        let name = walk.required("name", &raw.name, Text, owned);
        let identity = walk.required("identity", &raw.identity, Text, owned);
        let install = walk.required("install", &raw.install, table(Block::Install), install_of);
        let run = walk.required("run", &raw.run, table(Block::Run), run_of);
        let tools = walk.optional("tools", &raw.tools, List, |l, w| w.each(l, tool, tool_of));
        let allows = walk.optional("arg_allows", &raw.arg_allows, List, |l, w| {
            w.each(l, fence, fence_of)
        });
        let denies = walk.optional("arg_denies", &raw.arg_denies, List, |l, w| {
            w.each(l, fence, fence_of)
        });
        let secrets = walk.optional("shared_secrets", &raw.shared_secrets, List, |l, w| {
            w.each(l, shared, shared_of)
        });
        walk.unknown(&raw.unknown)?;

        Some(RawServer {
            name: name?,
            identity: identity?,
            install: install?,
            run: run?,
            tools: tools?.unwrap_or_default(),
            arg_allows: allows?.unwrap_or_default(),
            arg_denies: denies?.unwrap_or_default(),
            shared_secrets: secrets?.unwrap_or_default(),
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use crate::family::GrantArm;
        use crate::slot::tests::reads_empty_table;
        use crate::tomlfile::TomlFault;

        /// An `agent-mcp` server that each rule of one file accepts.
        const HEAD: &str = "name = \"example\"\nidentity = \"One written server.\"\n\
                            install.source = \"agent-mcp\"\nrun.entrypoint = \"example-mcp\"\n";

        fn key(name: &str) -> LocPart {
            LocPart::Key(name.to_owned())
        }

        /// Each fault of a text that the reader takes, as a location and a
        /// problem.
        fn faults(text: &str) -> Vec<(Vec<LocPart>, ShapeProblem)> {
            match RawServer::from_toml(text.as_bytes()) {
                Err(TomlRefusal::Shape(faults)) => faults
                    .into_iter()
                    .map(|fault| (fault.loc().to_vec(), fault.problem()))
                    .collect(),
                other => panic!("no fault of the shape: {other:?}"),
            }
        }

        fn wrong(wants: Wants) -> ShapeProblem {
            ShapeProblem::WrongType(wants)
        }

        #[test]
        fn each_raw_struct_reads_an_empty_table() {
            assert!(reads_empty_table::<TomlServer>());
            assert!(reads_empty_table::<TomlInstall>());
            assert!(reads_empty_table::<TomlRun>());
            assert!(reads_empty_table::<TomlTool>());
            assert!(reads_empty_table::<TomlFence>());
            assert!(reads_empty_table::<TomlShared>());
        }

        #[test]
        fn a_file_with_only_the_required_fields_gets_each_default() {
            let raw = RawServer::from_toml(HEAD.as_bytes()).unwrap();

            assert_eq!(raw.install.python, DEFAULT_PYTHON);
            assert_eq!(raw.install.package, None);
            assert!(raw.run.args.is_empty() && raw.run.env.is_empty());
            assert!(!raw.run.state_dir);
            assert!(raw.tools.is_empty() && raw.shared_secrets.is_empty());
        }

        #[test]
        fn the_faults_follow_the_model_and_then_each_unknown_key_in_order() {
            let text = "zeta = 1\nalpha = 2\nname = 5\n\
                        run = { env = { ZZ = 1, BB = \"x\", AA = true }, zz = 1 }\n";

            assert_eq!(
                faults(text),
                [
                    (vec![key("name")], wrong(Wants::Text)),
                    (vec![key("identity")], ShapeProblem::Missing),
                    (vec![key("install")], ShapeProblem::Missing),
                    (vec![key("run"), key("entrypoint")], ShapeProblem::Missing),
                    (vec![key("run"), key("env"), key("AA")], wrong(Wants::Text)),
                    (vec![key("run"), key("env"), key("ZZ")], wrong(Wants::Text)),
                    (vec![key("run"), key("zz")], ShapeProblem::Unknown),
                    (vec![key("alpha")], ShapeProblem::Unknown),
                    (vec![key("zeta")], ShapeProblem::Unknown),
                ]
            );
        }

        #[test]
        fn a_fence_with_no_tools_and_a_fence_that_neither_arm_takes() {
            let text = format!(
                "{HEAD}arg_denies = [{{ arg = \"a\", values = [\"b\"] }}, \
                 {{ tools = 5, arg = \"a\", values = [\"b\"] }}]\n"
            );
            let index = |at: usize| vec![key("arg_denies"), LocPart::Index(at), key("tools")];
            let arm = |arm: GrantArm| {
                let mut loc = index(1);
                loc.push(LocPart::Arm(arm));

                loc
            };

            assert_eq!(
                faults(&text),
                [
                    (index(0), ShapeProblem::Missing),
                    (arm(GrantArm::Names), wrong(Wants::List)),
                    (arm(GrantArm::Text), wrong(Wants::Text)),
                ]
            );
        }

        #[test]
        fn a_list_where_a_table_is_wanted_is_a_wrong_type() {
            let text = "name = \"example\"\nidentity = \"x\"\ninstall = [\"agent-mcp\"]\n\
                        run = { entrypoint = \"x\", env = [\"A\"] }\ntools = [[\"a\", \"b\"]]\n";

            assert_eq!(
                faults(text),
                [
                    (vec![key("install")], wrong(Wants::Block(Block::Install))),
                    (vec![key("run"), key("env")], wrong(Wants::Map)),
                    (
                        vec![key("tools"), LocPart::Index(0)],
                        wrong(Wants::Block(Block::Tool)),
                    ),
                ]
            );
        }

        #[test]
        fn a_refusal_of_the_reader_is_no_fault_of_the_shape() {
            assert_eq!(
                RawServer::from_toml(b"name = \"\xff\"\n"),
                Err(TomlRefusal::Unread(TomlFault::NotUtf8))
            );
            let large = format!("{HEAD}#{}\n", "x".repeat(65536));
            assert_eq!(
                RawServer::from_toml(large.as_bytes()),
                Err(TomlRefusal::Unread(TomlFault::TooLarge { limit: 65536 }))
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use std::str::FromStr;

    use creche_vectors::{self as vectors, Outcome};

    use super::{
        ASSET, ENTRYPOINT, Entrypoint, EnvValue, FenceArg, GithubRepo, Identity, InstallSource,
        LockPath, LockPathError, Markup, PACKAGE, Pin, RawFence, RawInstall, RawRun, RawServer,
        RawSharedSecret, RawTool, RootText, Server, StateDir, root_repo,
    };
    use crate::family::RawToolGrant;

    /// Each text of `accepted` parses, and each text of `refused` does not.
    fn check_tables<T: FromStr>(accepted: &[&str], refused: &[&str]) {
        for text in accepted {
            assert!(text.parse::<T>().is_ok(), "{text:?} is refused");
        }

        for text in refused {
            assert!(text.parse::<T>().is_err(), "{text:?} is accepted");
        }
    }

    fn minimal(name: &str) -> RawServer {
        RawServer {
            name: name.to_owned(),
            identity: "One written server.".to_owned(),
            install: RawInstall {
                source: "agent-mcp".to_owned(),
                package: None,
                version: None,
                lock: None,
                python: "3.12".to_owned(),
                repo: None,
                asset: None,
                sha256: None,
                git_ref: None,
            },
            run: RawRun {
                entrypoint: "example-mcp".to_owned(),
                args: Vec::new(),
                env: std::collections::BTreeMap::new(),
                state_dir: false,
                state_dir_env: None,
            },
            tools: Vec::new(),
            arg_allows: Vec::new(),
            arg_denies: Vec::new(),
            shared_secrets: Vec::new(),
        }
    }

    fn locs_of(raw: RawServer) -> Vec<String> {
        Server::try_from(raw)
            .err()
            .map(|refused| refused.into_issues())
            .unwrap_or_default()
            .into_iter()
            .map(|placed| placed.issue.loc)
            .collect()
    }

    #[test]
    fn an_identity_has_1_to_300_characters_after_the_collapse() {
        let max = "x".repeat(300);
        check_tables::<Identity>(&["a", &max, " a \n b "], &["", "   "]);
        assert!("x".repeat(301).parse::<Identity>().is_err());
    }

    #[test]
    fn a_source_is_one_of_three() {
        assert_eq!(InstallSource::parse("pypi"), Some(InstallSource::Pypi));
        for text in ["PyPI", "docker", "", "pypi\n"] {
            assert_eq!(InstallSource::parse(text), None, "{text:?}");
        }
    }

    #[test]
    fn a_github_repository_is_an_owner_and_a_name() {
        check_tables::<GithubRepo>(
            &["a/b", "example-owner/example.mcp_1"],
            &["", "a", "a/", "/b", "a/b/c", "a b/c", "a/b\n", "a/\u{661}"],
        );
    }

    #[test]
    fn a_lock_path_is_a_plain_repository_path() {
        let max = "a".repeat(256);
        check_tables::<LockPath>(
            &["mcp/web-search/install.lock", "a", &max, "a/./b"],
            &["", "/etc/passwd", ".hidden", "a b", "a\n", "a/\u{661}"],
        );
        assert_eq!(
            "a".repeat(257).parse::<LockPath>(),
            Err(LockPathError::Grammar)
        );
        for text in ["mcp/../x.lock", "mcp/x/", "mcp//x.lock", "a/.."] {
            assert_eq!(
                text.parse::<LockPath>(),
                Err(LockPathError::NotPlain),
                "{text:?}"
            );
        }
    }

    #[test]
    fn an_entrypoint_is_a_name_with_no_path() {
        check_tables::<Entrypoint>(&["example-mcp", "a b"], &["", "/usr/bin/x", "a/b"]);
    }

    #[test]
    fn a_fence_argument_is_one_name_or_two() {
        check_tables::<FenceArg>(
            &["owner", "owner/repo", "_a/b_1"],
            &["", "a/", "/a", "a/b/c", "1a", "a-b", "a\n", "a\u{661}"],
        );
        let longest = "a".repeat(64);
        let longer = "a".repeat(65);
        for text in [longest.clone(), format!("{longest}/{longest}")] {
            assert!(text.parse::<FenceArg>().is_ok(), "{text:?} is refused");
        }

        for text in [longer.clone(), format!("owner/{longer}")] {
            assert!(text.parse::<FenceArg>().is_err(), "{text:?} is accepted");
        }
    }

    #[test]
    fn a_minimal_file_is_a_server() {
        let server = Server::try_from(minimal("web-search")).unwrap();
        assert_eq!(server.name().as_str(), "web-search");
        assert_eq!(server.install().pin(), &Pin::AgentMcp);
        assert_eq!(server.run().state_dir(), &StateDir::None);
        assert_eq!(RawServer::from(&server), minimal("web-search"));
    }

    #[test]
    fn a_secret_value_names_its_key() {
        let mut raw = minimal("web-search");
        raw.run
            .env
            .insert("A_TOKEN".to_owned(), "secret:a_key".to_owned());
        raw.run
            .env
            .insert("A_URL".to_owned(), "https://example.invalid".to_owned());
        let server = Server::try_from(raw).unwrap();
        let values: Vec<&EnvValue> = server.run().env().values().collect();
        assert_eq!(
            values,
            [
                &EnvValue::Secret("a_key".to_owned()),
                &EnvValue::Literal("https://example.invalid".to_owned()),
            ]
        );
    }

    #[test]
    fn one_conversion_reports_each_violation_in_the_order_of_the_contract() {
        let mut raw = minimal("Bad_Name");
        raw.identity = String::new();
        raw.install.version = Some("1.0".to_owned());
        raw.run.entrypoint = "a/b".to_owned();
        raw.run
            .env
            .insert("A_TOKEN".to_owned(), "literal".to_owned());
        raw.run.state_dir = true;
        assert_eq!(
            locs_of(raw),
            [
                "name",
                "identity",
                "install.version",
                "run.entrypoint",
                "run.env.A_TOKEN",
                "run.state_dir_env",
            ]
        );
    }

    /// The message of each issue of a file that the conversion refuses.
    fn msgs_of(raw: RawServer) -> Vec<String> {
        Server::try_from(raw)
            .err()
            .map(|refused| refused.into_issues())
            .unwrap_or_default()
            .into_iter()
            .map(|placed| placed.issue.msg)
            .collect()
    }

    #[test]
    fn a_version_of_65_bytes_or_with_a_sign_is_refused() {
        let long = "1".repeat(65);
        for version in ["1.0.0-rc1", "1.0.0+local", long.as_str()] {
            let mut raw = minimal("example");
            raw.install.version = Some(version.to_owned());
            let wanted =
                format!("'{version}' is not an exact version; use [0-9][0-9A-Za-z.]{{0,63}}");
            assert!(msgs_of(raw).contains(&wanted), "{version}");
        }
    }

    #[test]
    fn a_variable_name_of_65_bytes_is_refused() {
        let name = "A".repeat(65);
        let mut raw = minimal("example");
        raw.run.env.insert(name.clone(), "x".to_owned());
        assert_eq!(
            msgs_of(raw),
            [format!(
                "'{name}' is not an environment variable name; use [A-Z][A-Z0-9_]{{0,63}}"
            )]
        );

        let mut raw = minimal("example");
        raw.run.env.insert("A".repeat(64), "x".to_owned());
        assert!(Server::try_from(raw).is_ok());
    }

    #[test]
    fn a_tool_name_of_65_bytes_is_refused() {
        let tool = |name: String| RawTool {
            name,
            description: "One tool.".to_owned(),
            write: false,
        };
        let name = "a".repeat(65);
        let mut raw = minimal("example");
        raw.tools = vec![tool(name.clone())];
        assert_eq!(
            msgs_of(raw),
            [format!(
                "'{name}' is not a tool name; use [A-Za-z][A-Za-z0-9_-]{{0,63}}"
            )]
        );

        let mut raw = minimal("example");
        raw.tools = vec![tool("a".repeat(64))];
        assert!(Server::try_from(raw).is_ok());
    }

    /// Each text of `accepted` holds the grammar, and each text of `refused`
    /// does not.
    fn check_root(grammar: &RootText, accepted: &[&str], refused: &[&str]) {
        for text in accepted {
            assert!(grammar.holds(text), "{text:?} is refused");
        }

        for text in refused {
            assert!(!grammar.holds(text), "{text:?} is accepted");
        }
    }

    #[test]
    fn each_text_of_the_grammar_of_root_has_its_form_and_its_cap() {
        let (most, past) = ("p".repeat(64), "p".repeat(65));
        let common = [
            "", ".a", "-a", "_a", "a b", "a/b", "a\n", "\u{661}", "a\u{e9}",
        ];
        check_root(
            &PACKAGE,
            &["a", "A.b_c-1", &most],
            &[&common[..], &[&past]].concat(),
        );
        check_root(
            &ENTRYPOINT,
            &["a", "A.b_c-1", &most],
            &[&common[..], &[&past]].concat(),
        );
        let (most, past) = ("a".repeat(128), "a".repeat(129));
        check_root(
            &ASSET,
            &["a+b.tar.gz", &most],
            &[&common[..], &["+a", &past]].concat(),
        );

        assert_eq!(PACKAGE.form(), "[A-Za-z0-9][A-Za-z0-9._-]{0,63}");
        assert_eq!(ENTRYPOINT.form(), "[A-Za-z0-9][A-Za-z0-9._-]{0,63}");
        assert_eq!(ASSET.form(), "[A-Za-z0-9][A-Za-z0-9._+-]{0,127}");
    }

    #[test]
    fn a_repository_of_root_is_an_owner_of_39_bytes_and_a_name_of_100() {
        let owner = "o".repeat(39);
        let name = "n".repeat(100);
        let accepted = [
            format!("{owner}/{name}"),
            "a/b".to_owned(),
            "a.b/c-d_e".to_owned(),
        ];
        let refused = [
            format!("{owner}o/n"),
            format!("o/{name}n"),
            "-a/b".to_owned(),
            "a/.b".to_owned(),
            "a/b/c".to_owned(),
            "a".to_owned(),
            "a/b\n".to_owned(),
        ];
        for text in &accepted {
            assert!(root_repo(text), "{text:?} is refused");
            // The grammar of root is narrower than the type.
            assert!(text.parse::<GithubRepo>().is_ok(), "{text:?}");
        }

        for text in &refused {
            assert!(!root_repo(text), "{text:?} is accepted");
        }
    }

    #[test]
    fn an_entrypoint_of_root_is_an_entrypoint() {
        for text in ["a", "A.b_c-1", &"e".repeat(64)] {
            assert!(ENTRYPOINT.holds(text));
            assert!(text.parse::<Entrypoint>().is_ok(), "{text:?}");
        }
    }

    /// The location and the message of each issue of a file that the
    /// conversion of `markup` refuses.
    fn issues_of(raw: RawServer, markup: Markup) -> Vec<(String, String)> {
        Server::from_raw(raw, markup)
            .err()
            .map(|refused| refused.into_issues())
            .unwrap_or_default()
            .into_iter()
            .map(|placed| (placed.issue.loc, placed.issue.msg))
            .collect()
    }

    /// A file that the YAML path takes and that each rule of root refuses.
    fn past_root() -> RawServer {
        let fence = |tools: Vec<String>, values: Vec<String>| RawFence {
            tools: RawToolGrant::Named(tools),
            arg: "owner".to_owned(),
            values,
        };
        let mut raw = minimal("example");
        raw.identity = "x   ".repeat(100);
        raw.install.python = "3.11".to_owned();
        raw.run.entrypoint = "example mcp".to_owned();
        raw.run.args = vec!["a".to_owned(); 33];
        raw.run
            .env
            .insert("A_TOKEN".to_owned(), "secret:K\0".to_owned());
        raw.run.state_dir = true;
        raw.run.state_dir_env = Some("state".to_owned());
        raw.tools = vec![RawTool {
            name: "a".to_owned(),
            // 210 characters, and 139 after the collapse.
            description: "x  ".repeat(70),
            write: false,
        }];
        raw.arg_denies = vec![
            fence(Vec::new(), vec!["v".to_owned()]),
            fence(vec!["a".to_owned()], vec!["v".repeat(257)]),
        ];
        raw.shared_secrets = vec![
            RawSharedSecret {
                secret: "K\0".to_owned(),
                server: "other-server".to_owned(),
            };
            9
        ];

        raw
    }

    #[test]
    fn a_toml_file_gets_each_rule_of_root_and_a_yaml_file_does_not() {
        let refused = issues_of(past_root(), Markup::Toml);
        let locs: Vec<&str> = refused.iter().map(|(loc, _)| loc.as_str()).collect();

        assert_eq!(
            locs,
            [
                "identity",
                "install.python",
                "run.entrypoint",
                "run.args",
                "run.env.A_TOKEN",
                "run.env.A_TOKEN",
                "run.state_dir_env",
                "tools[0]",
                "arg_denies[0].tools",
                "arg_denies[1].values[0]",
                "shared_secrets",
                "shared_secrets[1]",
                "shared_secrets[2]",
                "shared_secrets[3]",
                "shared_secrets[4]",
                "shared_secrets[5]",
                "shared_secrets[6]",
                "shared_secrets[7]",
                "shared_secrets[8]",
            ]
        );
        // The YAML path refuses only the secret that is shared two times.
        let yaml = issues_of(past_root(), Markup::Yaml);
        assert!(
            yaml.iter()
                .all(|(loc, _)| loc.starts_with("shared_secrets[")),
            "{yaml:?}"
        );
        let mut raw = past_root();
        raw.shared_secrets.truncate(1);
        let server = Server::from_raw(raw.clone(), Markup::Yaml).unwrap();
        assert_eq!(Server::try_from(raw), Ok(server));
    }

    #[test]
    fn a_toml_text_of_white_space_only_has_its_own_message() {
        let mut raw = minimal("example");
        raw.identity = "\t \n".to_owned();
        raw.tools = vec![RawTool {
            name: "a".to_owned(),
            description: "   ".to_owned(),
            write: false,
        }];

        assert_eq!(
            issues_of(raw.clone(), Markup::Toml),
            [
                (
                    "identity".to_owned(),
                    "identity holds only white space; it must say whose credential this server \
                     holds"
                        .to_owned()
                ),
                (
                    "tools[0]".to_owned(),
                    "description holds only white space".to_owned()
                ),
            ]
        );
        assert_eq!(
            issues_of(raw, Markup::Yaml),
            [
                (
                    "identity".to_owned(),
                    "identity is 0 characters; it must be 1 to 300 and say whose credential \
                     this server holds"
                        .to_owned()
                ),
                (
                    "tools[0]".to_owned(),
                    "description is 0 characters; it must be 1 to 200".to_owned()
                ),
            ]
        );
    }

    /// The surfaces whose accepted vectors hold a parsed server file, with
    /// the markup of their inputs.
    const SURFACES: [(&str, Markup); 2] = [
        ("server_file", Markup::Yaml),
        ("server_file.toml", Markup::Toml),
    ];

    /// Each server file that the Python validator accepts is a `Server`, and
    /// the `Server` holds each field as the Python model holds it. The crate
    /// `agent-family` compares the report of each vector.
    #[test]
    fn each_accepted_vector_is_a_server_with_the_python_fields() {
        for (name, markup) in SURFACES {
            let surface = vectors::surface(name).unwrap();
            for vector in surface.vectors() {
                let Some(value) = vector
                    .value()
                    .filter(|_| vector.result() == Outcome::Accepted)
                else {
                    continue;
                };

                let id = vector.id();
                let raw: RawServer = serde_json::from_value(value.clone()).unwrap();
                assert_eq!(&serde_json::to_value(&raw).unwrap(), value, "{name} {id}");
                let server = Server::from_raw(raw, markup).unwrap_or_else(|refused| {
                    panic!("{name} {id}: {refused}");
                });
                let back = serde_json::to_value(RawServer::from(&server)).unwrap();
                assert_eq!(&back, value, "{name} {id}");
            }
        }
    }
}
