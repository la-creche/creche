//! The MCP server file: `server.yaml` and its install pin (contract 01b).
//!
//! The two layers of [`crate::family`] hold a server file too:
//!
//! 1. [`RawServer`] holds each field as the file wrote it.
//! 2. [`Server`] holds a file that obeys each rule that one file can show. The
//!    one way to a `Server` is `TryFrom<RawServer>`.
//!
//! The conversion reports each violation, with the severity, the location and
//! the message of the Python validator `agent_family`. One rule needs more
//! than the file: the name is the name of the directory. `agent-family`
//! checks it.
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

use serde::{Deserialize, Serialize};

use crate::family::{
    ALL_TOOLS, DESCRIPTION_MAX, Description, DescriptionError, Issues, Placed, RawToolGrant,
    Refused, Section, Slot, collapse, tool_name_issue,
};
use crate::ids::{EnvName, PackageVersion, ServerName, Sha256Hex, ToolName};

/// The largest count of characters in an identity, after the collapse of its
/// spaces (contract 01b §2).
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

fn vet_identity(raw: &RawServer, out: &mut Issues) -> Option<Identity> {
    match raw.identity.parse::<Identity>() {
        Ok(identity) => Some(identity),
        Err(IdentityError { length }) => {
            out.error(
                Slot::of(Section::ServerName).step(2),
                "identity",
                format!(
                    "identity is {length} characters; it must be 1 to {IDENTITY_MAX} and say \
                     whose credential this server holds"
                ),
            );

            None
        }
    }
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
        Err(error) => {
            // The Python pattern of the family package permits `+` and `-`
            // and has no size limit. `ids::PackageVersion` does not.
            let python_accepts = text.as_bytes().first().is_some_and(u8::is_ascii_digit)
                && text
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'-'));
            let msg = if python_accepts {
                format!("'{text}' is not an exact version; {error}")
            } else {
                format!("'{text}' is not an exact version; a range makes a hash meaningless")
            };
            out.error(at, "install.version", msg);

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

fn vet_install(raw: &RawServer, out: &mut Issues) -> Option<Install> {
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
    let repo = install.repo.as_ref().and_then(|text| {
        let repo = text.parse::<GithubRepo>().ok();
        if repo.is_none() {
            out.error(
                at,
                "install.repo",
                format!("'{text}' is not <owner>/<name> on GitHub"),
            );
        }

        repo
    });
    let lock = vet_lock(install, at, out);
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

    valid.then(|| Install {
        pin,
        python: install.python.clone(),
    })
}

/// Whether the Python pattern for the name of an environment variable takes
/// `text`. The pattern has no size limit, and `ids::EnvName` has one.
fn python_env_name(text: &str) -> bool {
    let bytes = text.as_bytes();
    let tail = |byte: &u8| byte.is_ascii_uppercase() || byte.is_ascii_digit() || *byte == b'_';

    bytes.first().is_some_and(u8::is_ascii_uppercase) && bytes.iter().all(tail)
}

fn vet_env_entry(
    name: &str,
    value: &str,
    at: Slot,
    out: &mut Issues,
) -> Option<(EnvName, EnvValue)> {
    let loc = format!("run.env.{name}");
    let env_name = match name.parse::<EnvName>() {
        Ok(env_name) => Some(env_name),
        Err(error) if python_env_name(name) => {
            // The Python pattern of the family package has no size limit.
            // `ids::EnvName` has one.
            out.error(
                at,
                &loc,
                format!("'{name}' is not an environment variable name; {error}"),
            );

            None
        }
        Err(_) => {
            out.error(
                at,
                &loc,
                format!("'{name}' is not an environment variable name; use [A-Z][A-Z0-9_]*"),
            );

            None
        }
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
        Some("") => {
            out.error(at, loc, "'secret:' names no key");

            None
        }
        Some(key) => Some(EnvValue::Secret(key.to_owned())),
    };

    Some((env_name?, env_value?))
}

fn vet_run(raw: &RawServer, out: &mut Issues) -> Option<Run> {
    let at = Slot::of(Section::ServerRun);
    let run = &raw.run;
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

    let mut env = BTreeMap::new();
    let mut env_valid = true;
    for (name, value) in &run.env {
        match vet_env_entry(name, value, at, out) {
            Some((name, value)) => {
                env.insert(name, value);
            }
            None => env_valid = false,
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

    env_valid.then_some(Run {
        entrypoint: entrypoint?,
        args: run.args.clone(),
        env,
        state_dir: state_dir?,
    })
}

fn vet_tools(raw: &RawServer, out: &mut Issues) -> Option<Vec<Tool>> {
    let at = Slot::of(Section::ServerTools);
    let mut tools = Vec::new();
    let mut valid = true;
    let mut seen = BTreeSet::new();
    for (index, entry) in raw.tools.iter().enumerate() {
        let loc = format!("tools[{index}]");
        let name = match entry.name.parse::<ToolName>() {
            Ok(name) => Some(name),
            Err(error) => {
                out.error(at, &loc, tool_name_issue(&entry.name, error));

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

        let description = match entry.description.parse::<Description>() {
            Ok(description) => Some(description),
            Err(DescriptionError { length }) => {
                out.error(
                    at,
                    loc,
                    format!(
                        "description is {length} characters; it must be 1 to {DESCRIPTION_MAX}"
                    ),
                );

                None
            }
        };
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

fn vet_fence(raw: &RawServer, entry: &RawFence, loc: &str, out: &mut Issues) -> Option<Fence> {
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

    Some(Fence {
        tools: tools?,
        arg: arg?,
        values: entry.values.clone(),
    })
}

fn vet_fences(
    raw: &RawServer,
    field: &str,
    entries: &[RawFence],
    out: &mut Issues,
) -> Option<Vec<Fence>> {
    let fences: Vec<Option<Fence>> = entries
        .iter()
        .enumerate()
        .map(|(index, entry)| vet_fence(raw, entry, &format!("{field}[{index}]"), out))
        .collect();

    fences.into_iter().collect()
}

fn vet_shared(raw: &RawServer, out: &mut Issues) -> Option<Vec<SharedSecret>> {
    let at = Slot::of(Section::ServerShared);
    let named: BTreeSet<&str> = raw
        .run
        .env
        .values()
        .filter_map(|value| value.strip_prefix(SECRET_PREFIX))
        .collect();
    let mut shared = Vec::new();
    let mut valid = true;
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

impl TryFrom<RawServer> for Server {
    type Error = Refused;

    /// Checks each rule that one file decides (contract 01b). The conversion
    /// reads each field before it answers.
    fn try_from(raw: RawServer) -> Result<Self, Refused> {
        let mut out = Issues::default();
        let name = vet_name(&raw, &mut out);
        let identity = vet_identity(&raw, &mut out);
        let install = vet_install(&raw, &mut out);
        let run = vet_run(&raw, &mut out);
        let tools = vet_tools(&raw, &mut out);
        let arg_allows = vet_fences(&raw, "arg_allows", &raw.arg_allows, &mut out);
        let arg_denies = vet_fences(&raw, "arg_denies", &raw.arg_denies, &mut out);
        let shared_secrets = vet_shared(&raw, &mut out);
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

#[cfg(test)]
mod tests {
    use std::str::FromStr;

    use super::{
        Entrypoint, EnvValue, FenceArg, GithubRepo, Identity, InstallSource, LockPath,
        LockPathError, Pin, RawInstall, RawRun, RawServer, Server, StateDir,
    };
    use crate::vectors::{self, Outcome};

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

    /// The vectors of `server_file` that the Python code accepts and that
    /// this type refuses on purpose, with the contract section. Each row has
    /// the stance `stricter` of `rust/AGENTS.md`: an id type of `ids`
    /// refuses one text that `agent_family` accepts.
    const DEVIATIONS: [(&str, &str); 5] = [
        (
            "long-version-hyphen",
            "contract 01b §3.1: no grammar for a version",
        ),
        (
            "long-version-plus",
            "contract 01b §3.1: no grammar for a version",
        ),
        (
            "long-version",
            "contract 01b §3.1: no grammar for a version",
        ),
        (
            "long-env-name",
            "contract 01b §4.1: no grammar for the name of a variable",
        ),
        (
            "long-tool-name",
            "contract 01b §5: no limit for the size of a tool name",
        ),
    ];

    /// Each server file that the Python validator accepts is a `Server`, and
    /// the `Server` holds each field as the Python model holds it. The crate
    /// `agent-family` compares the report of each vector.
    #[test]
    fn each_accepted_vector_is_a_server_with_the_python_fields() {
        let surface = vectors::surface("server_file");
        let mut deviations = 0;
        for vector in &surface.vectors {
            let Some(value) = vector
                .value()
                .filter(|_| vector.result == Outcome::Accepted)
            else {
                continue;
            };

            let id = &vector.id;
            let raw: RawServer = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(&serde_json::to_value(&raw).unwrap(), value, "{id}");
            let deviates = DEVIATIONS.iter().any(|(vector, _)| vector == id);
            deviations += usize::from(deviates);
            let server = Server::try_from(raw);
            assert_eq!(server.is_err(), deviates, "{id}: {server:?}");
            if let Ok(server) = server {
                let back = serde_json::to_value(RawServer::from(&server)).unwrap();
                assert_eq!(&back, value, "{id}");
            }
        }

        assert_eq!(deviations, DEVIATIONS.len(), "a row names no vector");
    }
}
