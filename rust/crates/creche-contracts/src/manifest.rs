//! The component manifest, the resolved manifest, the live-state document and
//! the release request (contract 06).
//!
//! The release tool reads four kinds of untrusted text. Each kind has one
//! parsing constructor here, and each constructor gives a typed refusal:
//!
//! | Text | Type | Refusal |
//! |---|---|---|
//! | `component.yaml` | [`ComponentManifest`] | [`ManifestError`] |
//! | a request file | [`Request`] | [`RequestError`] |
//! | a live-state document | [`ReleaseState`] | [`StateError`] |
//! | the two operator values of the site file | [`Operator`] | [`OperatorError`] |
//!
//! [`ResolvedManifest`] is what root builds from those. Its canonical JSON
//! and its SHA-256 hash are the bytes that the approval of the operator binds
//! to.
//!
//! The requester and the executor share [`Request`]: one writer
//! ([`Request::to_bytes`]) and one parser ([`Request::parse`]).
//! [`Request::plan`] is the writer and then the parser, so a requester gets
//! the refusal that root gives.
//!
//! Each refusal has a code from the closed set [`RefusalCode`] and a detail.
//! The detail is the text that the Python release tool writes for the same
//! input. A detail holds no text of the input but a [`SafeToken`].
//!
//! Three private modules read and write the text forms. Each one gives what
//! the Python library of the release tool gives: `yaml` for `PyYAML`, `json`
//! for the `json` module, and `sha256` for `hashlib`.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use crate::ids::{Sha256Hex, Sha256HexError};

mod component;
mod json;
mod request;
mod resolved;
mod sha256;
mod state;
mod yaml;

pub use component::{
    AbsolutePath, AbsolutePathError, Argv, ComponentManifest, Install, Keep, KeepError,
    MAX_MANIFEST_BYTES, ManifestError, ManifestFault, Operator, OperatorError, PathField, Provided,
    RepoPath, RepoPathError, Required, Restore, Scope, Timeout, TimeoutError, UnitName,
    UnitNameError, Verify, YamlFault,
};
pub use request::{
    Draft, MAX_REQUEST_BYTES, MintError, Request, RequestError, RequestField, RequestKind,
    Requester, RequesterError, Timestamp, TimestampError, Wanted, mint_ulid,
};
pub use resolved::{
    Consumer, ContractRow, MANIFEST_VERSION, ResolvedAt, ResolvedAtError, ResolvedComponent,
    ResolvedManifest, Summary, SummaryFields, action_id, gate_id, release_action,
};
pub use state::{MAX_STATE_BYTES, ReleaseState, SourceFacts, StateError, StateLabel, StateMap};

// --- a text of the input that a refusal can show ---

/// The largest count of bytes of a text that a refusal shows.
const SAFE_TOKEN_MAX: usize = 64;

/// What a refusal shows in place of a text that is not safe to show.
const UNPRINTABLE: &str = "<unprintable>";

/// A text of the input that is safe in a log line, on a terminal and in the
/// ledger: `[A-Za-z0-9_.:/=@-]{1,64}`, or a placeholder for each other text.
///
/// A manifest can come from a branch that an agent wrote, so the name of an
/// unknown field is input too. A refusal shows such a text only through this
/// type.
///
/// ```
/// use creche_contracts::manifest::SafeToken;
///
/// assert_eq!(SafeToken::of("owner").as_str(), "owner");
/// assert_eq!(SafeToken::of("not a token").as_str(), "<unprintable>");
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::SafeToken;
///
/// let token = SafeToken(Some(String::from("\u{1b}[2J")));
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SafeToken(Option<String>);

impl SafeToken {
    /// The token of `text`: the text when it is safe, and the placeholder
    /// when it is not.
    #[must_use]
    pub fn of(text: &str) -> Self {
        let safe = (1..=SAFE_TOKEN_MAX).contains(&text.len())
            && text.bytes().all(|byte| {
                byte.is_ascii_alphanumeric()
                    || matches!(byte, b'_' | b'.' | b':' | b'/' | b'=' | b'@' | b'-')
            });

        Self(safe.then(|| text.to_owned()))
    }

    /// The text, or `<unprintable>`.
    #[must_use]
    pub fn as_str(&self) -> &str {
        self.0.as_deref().unwrap_or(UNPRINTABLE)
    }
}

impl fmt::Display for SafeToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// --- the closed sets ---

/// Makes an enum whose values are words of a file, and its error type.
macro_rules! word_enum {
    (
        $(#[$attribute:meta])*
        $name:ident,
        $(#[$error_attribute:meta])*
        $error:ident,
        $said:literal,
        { $($(#[$variant_attribute:meta])* $variant:ident => $text:literal),+ $(,)? }
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name {
            $($(#[$variant_attribute])* $variant,)+
        }

        impl $name {
            /// Each value, in the order of the Python enum.
            pub const ALL: &'static [Self] = &[$(Self::$variant,)+];

            /// The value as the word of a file.
            #[must_use]
            pub const fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $text,)+
                }
            }
        }

        impl FromStr for $name {
            type Err = $error;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                match text {
                    $($text => Ok(Self::$variant),)+
                    _ => Err($error),
                }
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }

        impl PartialOrd for $name {
            fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
                Some(self.cmp(other))
            }
        }

        /// The order of the words, which is the order that Python sorts the
        /// values in.
        impl Ord for $name {
            fn cmp(&self, other: &Self) -> std::cmp::Ordering {
                self.as_str().cmp(other.as_str())
            }
        }

        $(#[$error_attribute])*
        ///
        /// The error holds no text. The text is untrusted, and a caller writes
        /// this error to a log.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $error;

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                write!(f, "the text is not {}", $said)
            }
        }

        impl Error for $error {}
    };
}

word_enum! {
    /// The check that refused a release (contract 06 §3.2 and §10,
    /// `stage7-releases.md` §2.4 and §3.2).
    ///
    /// The set is closed, so that no text of the input becomes the
    /// `refused_check` of a ledger entry. A reader refuses a word that is not
    /// in the set. A reader that only shows a ledger entry shows the word and
    /// does not use this type: a later executor can write a code that an
    /// earlier reader does not know.
    RefusalCode,
    /// Why a text is not a refusal code.
    UnknownRefusalCode,
    "a refusal code",
    {
        /// The request file is not a request.
        Request => "request",
        /// A `component.yaml` is not a manifest.
        Manifest => "manifest",
        /// The manifests and the catalog do not name the same components.
        Catalog => "catalog",
        /// `depends_on` has a cycle.
        Cycle => "cycle",
        /// The live-state document is not one.
        State => "state",
        /// A provider does not meet the floor of a consumer.
        C1 => "C1",
        /// Two components provide one contract.
        C2 => "C2",
        /// A component provides a contract that it does not own.
        C3 => "C3",
        /// A major change of a contract does not include each consumer.
        C4 => "C4",
        /// One requester has more than eight pending requests.
        Rate => "rate",
        /// The approval gate did not grant the release.
        Approval => "approval",
        /// The second resolution differs from the approved one.
        Drift => "drift",
        /// The first check of the provenance predicate.
        P1 => "P1",
        /// The second check of the provenance predicate.
        P2 => "P2",
        /// The third check of the provenance predicate.
        P3 => "P3",
        /// The fourth check of the provenance predicate.
        P4 => "P4",
        /// The fifth check of the provenance predicate.
        P5 => "P5",
        /// A component would go to an older version.
        Monotonic => "monotonic",
        /// An MCP server file is not one that root installs from.
        Server => "server",
        /// The staged tree reads code from outside the tree.
        Editable => "editable",
        /// The unit does not start a program inside the installed tree.
        Unit => "unit",
        /// Root does not read the source directory on these terms.
        Source => "source",
        /// The site file is absent or holds a value that is not usable.
        Site => "site",
    }
}

word_enum! {
    /// The repository that holds the source of a component (contract 06 §8).
    ///
    /// The set is closed. A reader refuses a manifest with another word:
    /// contract 06 §10 says that the schema is closed.
    Repo,
    /// Why a text is not the name of a repository.
    UnknownRepo,
    "the name of a repository",
    {
        /// The repository of the platform.
        AgentControl => "agent-control",
        /// The repository of the MCP servers.
        AgentMcp => "agent-mcp",
        /// The repository of the family files.
        AgentRegistry => "agent-registry",
    }
}

// CONTRACT-QUESTION: contract 06 §8 lists four kinds and not `binary`. The
// Python release tool has the fifth kind since the first stage of the port,
// with a question of its own in `handover/src/handover/catalog.py`. This type
// has the five kinds of the Python code. A reader that knows four kinds
// refuses a `binary` manifest, so `handover` releases before the first such
// manifest.
word_enum! {
    /// What the artifact of a component is (contract 06 §8).
    ///
    /// The set is closed. A reader refuses a manifest with another word. An
    /// older reader thus refuses a manifest with a kind that a later contract
    /// adds, and the release of the reader comes first.
    Kind,
    /// Why a text is not a kind of artifact.
    UnknownKind,
    "a kind of artifact",
    {
        /// A Python virtual environment.
        Venv => "venv",
        /// A tree of compiled programs, each at `<install.to>/bin/<name>`.
        Binary => "binary",
        /// An OCI image.
        OciImage => "oci-image",
        /// A compose project.
        Compose => "compose",
        /// Data that no release installs.
        Data => "data",
    }
}

word_enum! {
    /// The account that the unit of a component runs as (contract 06 §8).
    ///
    /// The set is closed. A reader refuses a manifest with another word, with
    /// one exception: [`ComponentManifest::parse`] reads the account name of
    /// the operator of the site as [`RunsAs::Operator`].
    RunsAs,
    /// Why a text is not a `runs_as` word.
    UnknownRunsAs,
    "a runs_as word",
    {
        /// Root.
        Root => "root",
        /// The operator account of the site. A manifest does not name the
        /// account itself.
        Operator => "operator",
        /// The sandbox driver.
        Sandbox => "sandbox",
        /// One account for each MCP server, `mcp-<name>`.
        Mcp => "mcp",
        /// No account: nothing runs the component.
        None => "none",
    }
}

word_enum! {
    /// The account that runs the verify hook of a component (contract 06 §4).
    ///
    /// The set is closed. A reader refuses a manifest with another word, with
    /// the same exception as [`RunsAs`].
    VerifyUser,
    /// Why a text is not a verify user.
    UnknownVerifyUser,
    "a verify user",
    {
        /// Root.
        Root => "root",
        /// The operator account of the site.
        Operator => "operator",
    }
}

word_enum! {
    /// Whether a failed verify hook can restore a component (contract 06 §5).
    ///
    /// The set is closed. A reader refuses a manifest with another word: an
    /// unknown mode must not read as `automatic`.
    RestoreMode,
    /// Why a text is not a restore mode.
    UnknownRestoreMode,
    "a restore mode",
    {
        /// The executor installs the previous artifact again.
        Automatic => "automatic",
        /// The executor stops. A person decides.
        Manual => "manual",
    }
}

word_enum! {
    /// What a release does with one component (contract 06 §9).
    ///
    /// The set is closed. Root writes the resolved manifest and reads it in
    /// the same run, so a reader refuses another word.
    Action,
    /// Why a text is not an action.
    UnknownAction,
    "an action",
    {
        /// The release installs a new version.
        Deploy => "deploy",
        /// The release does not touch the component.
        Unchanged => "unchanged",
        /// The release installs the previous version again.
        Restore => "restore",
    }
}

word_enum! {
    /// The id of one contract between components (contract 06 §3).
    ///
    /// The set is closed. A reader refuses a manifest or a live-state document
    /// with another word. A new contract thus needs a release of `handover`
    /// before the first manifest names it.
    ContractId,
    /// Why a text is not a contract id.
    UnknownContractId,
    "a contract id",
    {
        /// `family.yaml` and `server.yaml`.
        FamilyFile => "family-file",
        /// The session API.
        SessionApi => "session-api",
        /// The channel between the host and a playpen.
        Channel => "channel",
        /// The grant file, the family token and the audit record.
        PepGrant => "pep-grant",
        /// The status document and the switch-sandbox call.
        ManagerStatus => "manager-status",
        /// The two file formats of contract 06.
        ComponentManifest => "component-manifest",
    }
}

impl ContractId {
    /// The component that contract 06 §3 names the provider of the contract.
    /// Rule C3 refuses each other provider.
    #[must_use]
    pub const fn owner(self) -> &'static str {
        match self {
            Self::FamilyFile | Self::ManagerStatus => "caregiver",
            Self::SessionApi => "attendance",
            Self::Channel => "playpen",
            Self::PepGrant => "chaperone",
            Self::ComponentManifest => "handover",
        }
    }
}

word_enum! {
    /// Whether a component takes a release (contract 06 §8, the field
    /// `release`).
    ///
    /// The set is closed. A reader refuses a manifest with another word.
    /// [`ComponentManifest::parse`] also reads a YAML boolean: `true` is `yes`
    /// and `false` is `no`.
    Releases,
    /// Why a text is not a `release` word.
    UnknownReleases,
    "a release word",
    {
        /// The component takes a release.
        Yes => "yes",
        /// The component is data. No release installs it.
        No => "no",
    }
}

// --- the catalog ---

/// One row of the component list of contract 06 §1.
///
/// The list is in the code and not in a file, so no merge makes it longer.
///
/// ```
/// use creche_contracts::manifest::{CatalogRow, Kind, Releases, Repo, catalog_row};
///
/// let row: &CatalogRow = catalog_row("playpen").ok_or("no such component")?;
/// assert_eq!(row.repo(), Repo::AgentControl);
/// assert_eq!(row.kind(), Kind::OciImage);
/// assert_eq!(row.releases(), Releases::Yes);
/// assert_eq!(row.bundles(), ["toybox"]);
/// # Ok::<(), &str>(())
/// ```
///
/// Code outside this module cannot build a row:
///
/// ```compile_fail,E0451
/// use creche_contracts::manifest::{CatalogRow, Kind, Releases, Repo, catalog_row};
///
/// let row = CatalogRow {
///     name: "extra",
///     repo: Repo::AgentControl,
///     path: "extra",
///     kind: Kind::Venv,
///     releases: Releases::Yes,
///     bundles: &[],
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CatalogRow {
    name: &'static str,
    repo: Repo,
    path: &'static str,
    kind: Kind,
    releases: Releases,
    bundles: &'static [&'static str],
}

impl CatalogRow {
    /// The name of the component.
    #[must_use]
    pub const fn name(&self) -> &'static str {
        self.name
    }

    /// The repository of the component.
    #[must_use]
    pub const fn repo(&self) -> Repo {
        self.repo
    }

    /// The path of the component in its repository. `.` is the whole
    /// repository.
    #[must_use]
    pub const fn path(&self) -> &'static str {
        self.path
    }

    /// What the artifact of the component is.
    #[must_use]
    pub const fn kind(&self) -> Kind {
        self.kind
    }

    /// Whether the component takes a release.
    #[must_use]
    pub const fn releases(&self) -> Releases {
        self.releases
    }

    /// Each other workspace directory that the build of the component
    /// installs (contract 06 §1 rule 9). A change there moves the tag of the
    /// component.
    #[must_use]
    pub const fn bundles(&self) -> &'static [&'static str] {
        self.bundles
    }
}

const fn row(
    name: &'static str,
    repo: Repo,
    path: &'static str,
    kind: Kind,
    releases: Releases,
    bundles: &'static [&'static str],
) -> CatalogRow {
    CatalogRow {
        name,
        repo,
        path,
        kind,
        releases,
        bundles,
    }
}

/// The component list of contract 06 §1, in the order of the contract.
pub static CATALOG: [CatalogRow; 9] = [
    row(
        "chaperone",
        Repo::AgentControl,
        "chaperone",
        Kind::Venv,
        Releases::Yes,
        &["handover"],
    ),
    row(
        "attendance",
        Repo::AgentControl,
        "attendance",
        Kind::Venv,
        Releases::Yes,
        &["door-owui", "door-tui", "door-trigger", "family"],
    ),
    row(
        "caregiver",
        Repo::AgentControl,
        "caregiver",
        Kind::Venv,
        Releases::Yes,
        &["family"],
    ),
    row(
        "noticeboard",
        Repo::AgentControl,
        "noticeboard",
        Kind::Venv,
        Releases::Yes,
        &["family"],
    ),
    row(
        "playpen",
        Repo::AgentControl,
        "playpen",
        Kind::OciImage,
        Releases::Yes,
        &["toybox"],
    ),
    row(
        "mcp-servers",
        Repo::AgentMcp,
        ".",
        Kind::Venv,
        Releases::Yes,
        &[],
    ),
    row(
        "infra",
        Repo::AgentControl,
        "infra",
        Kind::Compose,
        Releases::Yes,
        &[],
    ),
    row(
        "handover",
        Repo::AgentControl,
        "handover",
        Kind::Venv,
        Releases::Yes,
        &[],
    ),
    row(
        "registry-data",
        Repo::AgentRegistry,
        ".",
        Kind::Data,
        Releases::No,
        &[],
    ),
];

/// The row of one component. `None` for a name that contract 06 §1 does not
/// list.
#[must_use]
pub fn catalog_row(name: &str) -> Option<&'static CatalogRow> {
    CATALOG.iter().find(|row| row.name == name)
}

/// Each component that a request can name, in the order of the catalog.
pub fn releasable_names() -> impl Iterator<Item = &'static str> {
    CATALOG
        .iter()
        .filter(|row| row.releases == Releases::Yes)
        .map(CatalogRow::name)
}

/// The components that leave the catalog. A checkout can hold the manifest of
/// such a component or not.
pub const RETIRING: &[&str] = &["infra"];

/// The components that come into the catalog. A checkout can hold the
/// manifest of such a component or not, and the allocator gives it no tag.
pub const ARRIVING: &[&str] = &[];

/// The three files of the Cargo workspace that each `binary` build reads. A
/// change to one moves the tag of each binary component.
pub const BINARY_BUILD_FILES: [&str; 3] = [
    "rust/Cargo.lock",
    "rust/Cargo.toml",
    "rust/rust-toolchain.toml",
];

/// The first number of the `component-manifest` contract version that this
/// code speaks.
pub const MANIFEST_CONTRACT_MAJOR: u16 = 0;

/// The second number of that version. A manifest written against `0.MINOR`
/// with a `MINOR` at or below this one is accepted (contract 06 §3.1).
pub const MANIFEST_CONTRACT_MINOR: u16 = 6;

/// The component that deploys last, whatever `depends_on` says (contract 06
/// §1.1).
pub const LAST_IN_ORDER: &str = "handover";

/// The largest count of components that one request names: the catalog less
/// `registry-data`, which never releases (`stage7-releases.md` §2.3).
pub const MAX_REQUEST_COMPONENTS: usize = 8;

// --- numbers and digests ---

/// The largest number of a contract version in a manifest.
const CONTRACT_NUMBER_MAX: u16 = 999;

/// One number of a contract version in a manifest: 0 to 999 (contract 06
/// §3.1).
///
/// ```
/// use creche_contracts::manifest::ContractNumber;
///
/// let minor = ContractNumber::new(4)?;
/// assert_eq!(minor.get(), 4);
/// assert!(ContractNumber::new(1000).is_err());
/// # Ok::<(), creche_contracts::manifest::ContractNumberError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::ContractNumber;
///
/// let minor = ContractNumber(1000);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ContractNumber(u16);

impl ContractNumber {
    /// The largest number.
    pub const MAX: Self = Self(CONTRACT_NUMBER_MAX);

    /// Checks that `number` is 999 at most.
    ///
    /// # Errors
    ///
    /// Gives [`ContractNumberError`] for a larger number.
    pub const fn new(number: u16) -> Result<Self, ContractNumberError> {
        if number > CONTRACT_NUMBER_MAX {
            return Err(ContractNumberError);
        }

        Ok(Self(number))
    }

    /// The number.
    #[must_use]
    pub const fn get(self) -> u16 {
        self.0
    }
}

impl fmt::Display for ContractNumber {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// A number of a contract version that is larger than 999.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContractNumberError;

impl fmt::Display for ContractNumberError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "a number of a contract version is {CONTRACT_NUMBER_MAX} at most"
        )
    }
}

impl Error for ContractNumberError {}

/// What a digest starts with.
const DIGEST_PREFIX: &str = "sha256:";

/// A SHA-256 digest with its prefix: `sha256:[0-9a-f]{64}` (contract 06 §9).
///
/// The input digest and the artifact digest of a component and the hash of a
/// resolved manifest have this form.
///
/// ```
/// use creche_contracts::manifest::Digest;
///
/// let text = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
/// let digest: Digest = text.parse()?;
/// assert_eq!(digest.as_str(), text);
/// assert_eq!(digest.hex().len(), 64);
/// # Ok::<(), creche_contracts::manifest::DigestError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::Digest;
///
/// let digest = Digest(String::from("sha256:0"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Digest(String);

impl Digest {
    /// The digest as text, with its prefix.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The 64 hex bytes of the digest.
    #[must_use]
    pub fn hex(&self) -> &str {
        self.0.strip_prefix(DIGEST_PREFIX).unwrap_or(&self.0)
    }

    /// The digest of `bytes`. The hash function gives 64 lower-case hex
    /// bytes, which is the grammar of the type.
    fn of(bytes: &[u8]) -> Self {
        Self(format!("{DIGEST_PREFIX}{}", sha256::hex_digest(bytes)))
    }
}

impl FromStr for Digest {
    type Err = DigestError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        text.strip_prefix(DIGEST_PREFIX)
            .ok_or(DigestError::NoPrefix)?
            .parse::<Sha256Hex>()
            .map_err(DigestError::Hex)?;

        Ok(Self(text.to_owned()))
    }
}

impl fmt::Display for Digest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not a digest.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DigestError {
    /// The text does not start with `sha256:`.
    NoPrefix,
    /// The text after `sha256:` is not a SHA-256 digest in lower-case hex.
    Hex(Sha256HexError),
}

impl fmt::Display for DigestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoPrefix => write!(f, "a digest starts with {DIGEST_PREFIX}"),
            Self::Hex(error) => write!(f, "after {DIGEST_PREFIX}, {error}"),
        }
    }
}

impl Error for DigestError {}

#[cfg(test)]
mod tests {
    //! The differential test of the `manifest` module: each type against the
    //! vectors of the Python release tool (`vectors/data/manifest`).

    use std::collections::{BTreeMap, BTreeSet};

    use serde_json::{Map, Value, json};

    use super::*;
    use crate::ids::{ComponentName, ContractVersion, GateId, Ulid};
    use crate::vectors::{self, Input, Outcome, Vector};

    /// Each surface of this module, and the type or the function that the
    /// surface is the oracle of.
    const SURFACES: [(&str, &str); 11] = [
        ("manifest.component", "ComponentManifest::parse"),
        ("manifest.operator", "Operator::new"),
        ("manifest.catalog", "CATALOG and each closed set"),
        ("manifest.refusal_code", "RefusalCode"),
        ("manifest.request.parse", "Request::parse"),
        ("manifest.request.plan", "Request::plan"),
        ("manifest.request.ulid", "mint_ulid"),
        ("manifest.state", "ReleaseState::parse"),
        ("manifest.resolved", "ResolvedManifest"),
        ("manifest.gate", "gate_id, action_id and release_action"),
        ("manifest.summary", "Summary"),
    ];

    /// Each vector that the Python code accepts and the Rust code refuses, on
    /// purpose: the surface, the vector, the contract section and the reason.
    const DEVIATIONS: [(&str, &str, &str, &str); 11] = [
        (
            "manifest.component",
            "yaml-nested-200-replaced",
            "contract 06 §10",
            "the YAML reader refuses a text past 128 levels, also in a value that a later key \
             replaces",
        ),
        (
            "manifest.component",
            "yaml-tag-int-arabic-indic",
            "contract 06 §8",
            "a tagged number has ASCII digits (rust/AGENTS.md, rule 9)",
        ),
        (
            "manifest.component",
            "yaml-tag-int-no-break-space",
            "contract 06 §8",
            "a tagged number has ASCII spaces around it (rust/AGENTS.md, rule 9)",
        ),
        (
            "manifest.component",
            "build-word-lone-surrogate",
            "contract 06 §8",
            "a word of a command is a string, and a lone surrogate is in no string",
        ),
        (
            "manifest.component",
            "install-lone-surrogate",
            "contract 06 §8",
            "an install path is a string, and a lone surrogate is in no string",
        ),
        (
            "manifest.request.ulid",
            "negative-time",
            "contract 06 §9",
            "a ULID holds 48 bits of milliseconds (contract 02 §2), and a time below zero has none",
        ),
        (
            "manifest.request.ulid",
            "past-48-bits",
            "contract 06 §9",
            "a ULID holds 48 bits of milliseconds (contract 02 §2), and the mint refuses a later \
             time",
        ),
        (
            "manifest.resolved",
            "resolved-at-nan",
            "contract 06 §9",
            "resolved_at is a number, and JSON has no NaN",
        ),
        (
            "manifest.resolved",
            "resolved-at-infinity",
            "contract 06 §9",
            "resolved_at is a number, and JSON has no infinity",
        ),
        (
            "manifest.resolved",
            "resolved-at-negative-infinity",
            "contract 06 §9",
            "resolved_at is a number, and JSON has no infinity",
        ),
        (
            "manifest.resolved",
            "version-arabic-indic",
            "contract 06 §2",
            "a version has ASCII digits (rust/AGENTS.md, rule 9)",
        ),
    ];

    /// Each vector that both languages refuse with a different detail, on
    /// purpose: the surface, the vector, the contract section and the reason.
    const DETAILS: [(&str, &str, &str, &str); 1] = [(
        "manifest.component",
        "yaml-nested-200",
        "contract 06 §10",
        "the YAML reader refuses a text past 128 levels, and PyYAML has no such limit",
    )];

    /// The vectors of one surface on which the Rust code differs, by table.
    #[derive(Default)]
    struct Found {
        deviations: BTreeSet<String>,
        details: BTreeSet<String>,
    }

    fn rows_of(table: &[(&str, &str, &str, &str)], surface: &str) -> BTreeSet<String> {
        table
            .iter()
            .filter(|row| row.0 == surface)
            .map(|row| row.1.to_owned())
            .collect()
    }

    impl Found {
        /// Stops the test when the differences are not the rows of the two
        /// tables.
        fn check(&self, surface: &str) {
            assert_eq!(
                self.deviations,
                rows_of(&DEVIATIONS, surface),
                "{surface}: the vectors that Python accepts and Rust refuses are not the rows of \
                 DEVIATIONS"
            );
            assert_eq!(
                self.details,
                rows_of(&DETAILS, surface),
                "{surface}: the vectors with another detail are not the rows of DETAILS"
            );
        }
    }

    fn text_field<'a>(object: &'a Map<String, Value>, key: &str) -> &'a str {
        object
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or_else(|| panic!("no text {key}"))
    }

    fn optional_field<'a>(object: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
        match object.get(key) {
            Some(Value::Null) => None,
            Some(Value::String(text)) => Some(text),
            other => panic!("{key} is {other:?}"),
        }
    }

    /// The check, the subject and the detail of the refusal of a vector.
    fn refusal(vector: &Vector) -> (&str, &str, &str) {
        let refusal = vector
            .refusal()
            .and_then(Value::as_object)
            .unwrap_or_else(|| panic!("{}: no refusal", vector.id));

        (
            text_field(refusal, "check"),
            text_field(refusal, "subject"),
            text_field(refusal, "detail"),
        )
    }

    fn object_of(value: &Value) -> &Map<String, Value> {
        value
            .as_object()
            .unwrap_or_else(|| panic!("{value} is no object"))
    }

    fn object_field<'a>(object: &'a Map<String, Value>, key: &str) -> &'a Map<String, Value> {
        object_of(object.get(key).unwrap_or(&Value::Null))
    }

    fn text_of(value: &Value) -> &str {
        value
            .as_str()
            .unwrap_or_else(|| panic!("{value} is no text"))
    }

    fn float_of(bits: &str) -> f64 {
        f64::from_bits(u64::from_str_radix(bits, 16).unwrap())
    }

    fn bytes_of(vector: &Vector, key: &str) -> Vec<u8> {
        let input: Input = serde_json::from_value(vector.field(key).unwrap().clone()).unwrap();

        input.bytes().unwrap()
    }

    #[test]
    fn each_surface_of_the_module_is_in_the_table() {
        let named: BTreeSet<&str> = SURFACES.iter().map(|(surface, _)| *surface).collect();
        let indexed: BTreeSet<String> = vectors::index()
            .into_iter()
            .map(|row| row.surface)
            .filter(|surface| surface.starts_with("manifest."))
            .collect();

        assert_eq!(
            indexed,
            named.iter().map(|surface| (*surface).to_owned()).collect()
        );
        for (surface, id, section, reason) in DEVIATIONS.iter().chain(&DETAILS) {
            assert!(named.contains(surface), "{surface} {id}");
            assert!(section.starts_with("contract 06 §"), "{surface} {id}");
            assert!(!reason.is_empty(), "{surface} {id}");
        }
    }

    // --- component.yaml ---

    fn argv_json(argv: &Argv) -> Value {
        json!(argv.words())
    }

    fn manifest_json(manifest: &ComponentManifest) -> Value {
        let provides: Vec<Value> = manifest
            .provides()
            .iter()
            .map(|entry| {
                json!({
                    "contract": entry.contract().as_str(),
                    "major": entry.major().get(),
                    "minor": entry.minor().get(),
                })
            })
            .collect();
        let requires: Vec<Value> = manifest
            .requires()
            .iter()
            .map(|entry| {
                json!({
                    "contract": entry.contract().as_str(),
                    "major": entry.major().get(),
                    "min_minor": entry.min_minor().get(),
                })
            })
            .collect();
        let names = |names: &[ComponentName]| -> Vec<String> {
            names.iter().map(|name| name.as_str().to_owned()).collect()
        };

        json!({
            "manifest_version": manifest.manifest_version().as_str(),
            "name": manifest.name().as_str(),
            "repo": manifest.repo().as_str(),
            "path": manifest.path().as_str(),
            "kind": manifest.kind().as_str(),
            "unit": manifest.unit().map(UnitName::as_str),
            "runs_as": manifest.runs_as().as_str(),
            "build": manifest.build().iter().map(argv_json).collect::<Vec<_>>(),
            "install": {
                "to": manifest.install().to().as_str(),
                "prev": manifest.install().prev().as_str(),
            },
            "provides": provides,
            "requires": requires,
            "depends_on": names(manifest.depends_on()),
            "verify": {
                "command": argv_json(manifest.verify().command()),
                "user": manifest.verify().user().as_str(),
                "timeout_s": manifest.verify().timeout().get(),
            },
            "restore": {
                "mode": manifest.restore().mode().as_str(),
                "keep": manifest.restore().keep().get(),
            },
            "secrets": manifest.secrets().iter().map(|name| name.as_str()).collect::<Vec<_>>(),
            "release": manifest.release().as_str(),
        })
    }

    #[test]
    fn a_component_manifest_reads_as_the_python_reader_reads_it() {
        let surface = vectors::surface("manifest.component");
        let label = text_field(&surface.context, "subject");
        let account = surface
            .context
            .get("operator")
            .unwrap()
            .as_object()
            .unwrap();
        let operator =
            Operator::new(text_field(account, "user"), text_field(account, "home")).unwrap();
        let mut found = Found::default();
        for vector in &surface.vectors {
            let id = &vector.id;
            let text = vector.input.text().unwrap();
            let site = text_field(vector.field("params").unwrap().as_object().unwrap(), "site");
            let operator = match site {
                "set" => Some(&operator),
                "missing" => None,
                other => panic!("{id}: the site {other}"),
            };
            let read = ComponentManifest::parse(&text, operator);
            match (vector.result, read) {
                (Outcome::Accepted, Ok(manifest)) => {
                    assert_eq!(&manifest_json(&manifest), vector.value().unwrap(), "{id}");
                }
                (Outcome::Accepted, Err(_)) => {
                    found.deviations.insert(id.clone());
                }
                (Outcome::Refused, Err(error)) => {
                    let (check, subject, detail) = refusal(vector);

                    assert_eq!(error.code().as_str(), check, "{id}");
                    assert_eq!(error.subject(label), subject, "{id}");
                    // A refusal of the site file names the path of the file and
                    // why the file gives no account. This reader gets the account
                    // from its caller, so it knows only that there is none.
                    if error.code() == RefusalCode::Site {
                        continue;
                    }

                    if error.detail() != detail {
                        found.details.insert(id.clone());
                    }
                }
                (Outcome::Raised, Err(_)) => {}
                (Outcome::Refused | Outcome::Raised, Ok(_)) => {
                    panic!("{id}: Python refuses the manifest and Rust accepts it")
                }
            }
        }

        found.check("manifest.component");
    }

    /// A valid manifest with the line of one field replaced.
    fn manifest_with(field: &str, line: &str) -> String {
        let lines = [
            ("manifest_version", "manifest_version: \"0.6\""),
            ("name", "name: chaperone"),
            ("repo", "repo: agent-control"),
            ("path", "path: chaperone"),
            ("kind", "kind: venv"),
            ("unit", "unit: null"),
            ("runs_as", "runs_as: root"),
            ("install", "install: {to: /opt/x, prev: /opt/x.prev}"),
            (
                "verify",
                "verify: {command: [/bin/true], user: root, timeout_s: 5}",
            ),
            ("restore", "restore: {mode: automatic, keep: 1}"),
            ("release", "release: yes"),
        ];

        assert!(lines.iter().any(|(name, _)| *name == field), "{field}");

        lines
            .iter()
            .map(|(name, own)| if *name == field { line } else { own })
            .fold(String::new(), |text, line| text + line + "\n")
    }

    #[test]
    fn a_scalar_that_gives_no_value_is_refused() {
        let keep = |scalar: &str| format!("restore: {{mode: automatic, keep: {scalar}}}");
        let timeout = |scalar: &str| {
            format!("verify: {{command: [/bin/true], user: root, timeout_s: {scalar}}}")
        };
        // Each scalar is in a field that takes its value, so a reader that
        // gives a value accepts the manifest.
        let taken = [
            ("restore", keep("!!int \"1\"")),
            ("restore", keep("!!int \" 1\\t\"")),
            ("verify", timeout("!!int 0x5")),
            ("release", "release: !!bool yes".to_owned()),
        ];
        let no_value = [
            ("an integer tag on a word", "restore", keep("!!int abc")),
            ("an integer tag on no text", "restore", keep("!!int \"\"")),
            (
                "an integer with a control character after it",
                "restore",
                keep("!!int \"1\\x1c\""),
            ),
            (
                "an integer with a control character before it",
                "restore",
                keep("!!int \"\\x1f1\""),
            ),
            (
                "an integer of 4301 digits",
                "restore",
                keep(&"9".repeat(4301)),
            ),
            ("a float tag on a word", "verify", timeout("!!float abc")),
            (
                "a boolean tag on a word",
                "release",
                "release: !!bool maybe".to_owned(),
            ),
            (
                "an escape past the last code point",
                "unit",
                "unit: \"\\UFFFFFFFF\"".to_owned(),
            ),
        ];
        let fault_of = |field: &str, line: &str| {
            ComponentManifest::parse(&manifest_with(field, line), None).map_err(|error| error.fault)
        };

        for (field, line) in &taken {
            assert!(fault_of(field, line).is_ok(), "{line}");
        }

        for (kind, field, line) in &no_value {
            assert!(
                matches!(
                    fault_of(field, line),
                    Err(ManifestFault::Yaml(YamlFault::Line(_)))
                ),
                "{kind}"
            );
        }

        // The reader gives a value for an integer of 4300 digits. The range of
        // the field then refuses it.
        assert!(
            matches!(
                fault_of("restore", &keep(&"9".repeat(4300))),
                Err(ManifestFault::OutOfRange { field: "keep", .. })
            ),
            "an integer of 4300 digits"
        );
        assert_eq!(
            fault_of(
                "unit",
                &format!("unit: {}{}", "[".repeat(2000), "]".repeat(2000))
            ),
            Err(ManifestFault::Yaml(YamlFault::Deep)),
            "a nesting past the limit"
        );
    }

    #[test]
    fn the_operator_account_is_checked_as_the_site_file_reader_checks_it() {
        let surface = vectors::surface("manifest.operator");
        for vector in &surface.vectors {
            let id = &vector.id;
            let args = vector.input.args().unwrap();
            let made = Operator::new(text_field(args, "user"), text_field(args, "home"));
            match (vector.result, made) {
                (Outcome::Accepted, Ok(operator)) => {
                    let value = json!({"user": operator.user(), "home": operator.home()});

                    assert_eq!(&value, vector.value().unwrap(), "{id}");
                }
                (Outcome::Refused | Outcome::Raised, Err(error)) => {
                    let (check, subject, detail) = refusal(vector);

                    assert_eq!(error.code().as_str(), check, "{id}");
                    assert_eq!(Scope::Site.subject(""), subject, "{id}");
                    assert_eq!(error.to_string(), detail, "{id}");
                }
                (outcome, made) => panic!("{id}: Python {outcome:?}, Rust {made:?}"),
            }
        }
    }

    #[test]
    fn the_operator_account_has_its_grammar() {
        let accepted = [("keeper", "/home/keeper"), ("_k-9", "/a/b.c/_d-e")];
        let refused = [
            (
                "keeper\n",
                "/home/keeper",
                "AGENT_OPERATOR_USER is not a value this accepts",
            ),
            (
                "keeper\u{661}",
                "/home/keeper",
                "AGENT_OPERATOR_USER is not a value this accepts",
            ),
            (
                "Keeper",
                "/home/keeper",
                "AGENT_OPERATOR_USER is not a value this accepts",
            ),
            ("root", "/root", "AGENT_OPERATOR_USER names root"),
            (
                "keeper",
                "/home/keeper\n",
                "AGENT_OPERATOR_HOME is not a value this accepts",
            ),
            (
                "keeper",
                "/home/\u{661}",
                "AGENT_OPERATOR_HOME is not a value this accepts",
            ),
            (
                "keeper",
                "/home/../etc",
                "AGENT_OPERATOR_HOME is not a value this accepts",
            ),
        ];
        for (user, home) in accepted {
            let operator = Operator::new(user, home).unwrap();

            assert_eq!((operator.user(), operator.home()), (user, home));
        }

        for (user, home, said) in refused {
            let error = Operator::new(user, home).unwrap_err();

            assert!(error.to_string().starts_with(said), "{user:?} {home:?}");
            assert_eq!(error.code(), RefusalCode::Site);
        }
    }

    // --- the catalog and the closed sets ---

    fn words<T: Copy>(all: &[T], as_str: fn(T) -> &'static str) -> Value {
        json!(all.iter().map(|word| as_str(*word)).collect::<Vec<_>>())
    }

    fn catalog_value(id: &str) -> Value {
        match id {
            "components" => CATALOG
                .iter()
                .map(|row| {
                    json!({
                        "name": row.name(),
                        "repo": row.repo().as_str(),
                        "path": row.path(),
                        "kind": row.kind().as_str(),
                        "releases": row.releases().as_str(),
                        "bundles": row.bundles(),
                    })
                })
                .collect(),
            "releasable-names" => releasable_names().collect(),
            "contract-owners" => ContractId::ALL
                .iter()
                .map(|contract| (contract.as_str().to_owned(), json!(contract.owner())))
                .collect::<Map<String, Value>>()
                .into(),
            "retiring" => json!(RETIRING),
            "arriving" => json!(ARRIVING),
            "binary-build-files" => json!(BINARY_BUILD_FILES),
            "manifest-contract-version" => {
                json!({"major": MANIFEST_CONTRACT_MAJOR, "minor": MANIFEST_CONTRACT_MINOR})
            }
            "last-in-order" => json!(LAST_IN_ORDER),
            "max-request-components" => json!(MAX_REQUEST_COMPONENTS),
            "set-repo" => words(Repo::ALL, Repo::as_str),
            "set-kind" => words(Kind::ALL, Kind::as_str),
            "set-runs-as" => words(RunsAs::ALL, RunsAs::as_str),
            "set-verify-user" => words(VerifyUser::ALL, VerifyUser::as_str),
            "set-restore-mode" => words(RestoreMode::ALL, RestoreMode::as_str),
            "set-action" => words(Action::ALL, Action::as_str),
            "set-contract-id" => words(ContractId::ALL, ContractId::as_str),
            "set-releases" => words(Releases::ALL, Releases::as_str),
            other => panic!("the catalog of the Python code has {other}, and this crate does not"),
        }
    }

    #[test]
    fn the_catalog_and_each_closed_set_are_the_python_ones() {
        let surface = vectors::surface("manifest.catalog");
        for vector in &surface.vectors {
            assert_eq!(vector.result, Outcome::Accepted, "{}", vector.id);
            assert_eq!(
                &catalog_value(&vector.id),
                vector.value().unwrap(),
                "{}",
                vector.id
            );
        }

        assert_eq!(
            MANIFEST_VERSION,
            format!("{MANIFEST_CONTRACT_MAJOR}.{MANIFEST_CONTRACT_MINOR}")
        );
        for row in &CATALOG {
            assert!(
                row.name().parse::<ComponentName>().is_ok(),
                "{}",
                row.name()
            );
            assert!(row.path().parse::<RepoPath>().is_ok(), "{}", row.name());
            assert_eq!(catalog_row(row.name()), Some(row));
        }

        for contract in ContractId::ALL {
            assert!(catalog_row(contract.owner()).is_some(), "{contract}");
        }

        assert!(catalog_row(LAST_IN_ORDER).is_some());
        assert!(catalog_row("nobody").is_none());
        assert_eq!(releasable_names().count(), MAX_REQUEST_COMPONENTS);
    }

    #[test]
    fn a_refusal_code_is_one_of_the_python_codes() {
        let surface = vectors::surface("manifest.refusal_code");
        let mut accepted = Vec::new();
        for vector in &surface.vectors {
            let text = vector.input.text().unwrap();
            match (vector.result, text.parse::<RefusalCode>()) {
                (Outcome::Accepted, Ok(code)) => {
                    assert_eq!(
                        &json!(code.as_str()),
                        vector.value().unwrap(),
                        "{}",
                        vector.id
                    );
                    accepted.push(code);
                }
                (Outcome::Refused | Outcome::Raised, Err(UnknownRefusalCode)) => {}
                (outcome, code) => panic!("{}: Python {outcome:?}, Rust {code:?}", vector.id),
            }
        }

        assert_eq!(
            accepted,
            RefusalCode::ALL,
            "the codes, in the order of the Python enum"
        );
    }

    #[test]
    fn each_closed_set_refuses_a_word_that_is_not_in_the_set() {
        for word in ["", "Venv", "venv\n", " venv", "wheel"] {
            assert_eq!(word.parse::<Kind>(), Err(UnknownKind), "{word:?}");
        }

        assert_eq!("operator".parse(), Ok(RunsAs::Operator));
        assert_eq!("keeper".parse::<RunsAs>(), Err(UnknownRunsAs));
        assert_eq!("sandbox".parse::<VerifyUser>(), Err(UnknownVerifyUser));
        assert_eq!("true".parse::<Releases>(), Err(UnknownReleases));
        assert_eq!("Automatic".parse::<RestoreMode>(), Err(UnknownRestoreMode));
        assert_eq!("rollback".parse::<Action>(), Err(UnknownAction));
        assert_eq!("agent-other".parse::<Repo>(), Err(UnknownRepo));
        assert_eq!("chaperone".parse::<ContractId>(), Err(UnknownContractId));
        for contract in ContractId::ALL {
            assert_eq!(contract.to_string().parse(), Ok(*contract));
        }

        let mut sorted = ContractId::ALL.to_vec();
        sorted.sort();
        let texts: Vec<&str> = sorted.iter().map(|contract| contract.as_str()).collect();
        let mut by_text = texts.clone();
        by_text.sort_unstable();

        assert_eq!(texts, by_text, "a set sorts in the order of its words");
    }

    // --- the request ---

    fn request_json(request: &Request) -> Value {
        let components: Vec<Value> = request
            .components()
            .iter()
            .map(|(name, wanted)| json!([name.as_str(), wanted.as_str()]))
            .collect();

        json!({
            "id": request.id().as_str(),
            "kind": request.kind().as_str(),
            "components": components,
            "rollback_of": request.kind().rollback_of().map(Ulid::as_str),
            "requested_by": request.requested_by().as_str(),
            "requester_session": request.requester_session().map(|session| session.as_str()),
        })
    }

    /// Compares an accepted request with its vector: each field, the exact bits
    /// of `ts`, the bytes of the writer, and the parse of those bytes.
    fn check_request(vector: &Vector, request: &Request) {
        let id = &vector.id;
        let mut value = vector.value().unwrap().clone();
        value.as_object_mut().unwrap().remove("ts");
        let bits = vector.field("ts_bits").unwrap().as_str().unwrap();
        let written = request.to_bytes();

        assert_eq!(request_json(request), value, "{id}");
        assert_eq!(
            request.ts().get().to_bits(),
            float_of(bits).to_bits(),
            "{id}"
        );
        assert_eq!(written, bytes_of(vector, "output"), "{id}");
        assert_eq!(
            &Request::parse(&written, request.id()).unwrap(),
            request,
            "{id}"
        );
    }

    fn check_request_refusal(vector: &Vector, error: RequestError) {
        let (check, subject, detail) = refusal(vector);

        assert_eq!(error.code().as_str(), check, "{}", vector.id);
        assert_eq!(error.subject(), subject, "{}", vector.id);
        assert_eq!(error.detail(), detail, "{}", vector.id);
    }

    #[test]
    fn a_request_file_parses_as_the_executor_parses_it() {
        let surface = vectors::surface("manifest.request.parse");
        let mut found = Found::default();
        for vector in &surface.vectors {
            let id = &vector.id;
            let raw = vector.input.bytes().unwrap();
            let params = vector.field("params").unwrap().as_object().unwrap();
            // The parser takes the id of the file name as a `Ulid`. A file name
            // that is no ULID has no request in Rust, and the Python parser
            // refuses such an id.
            let parsed = text_field(params, "request_id")
                .parse::<Ulid>()
                .ok()
                .map(|request_id| Request::parse(&raw, &request_id));
            match (vector.result, parsed) {
                (Outcome::Accepted, Some(Ok(request))) => check_request(vector, &request),
                (Outcome::Accepted, Some(Err(_)) | None) => {
                    found.deviations.insert(id.clone());
                }
                (Outcome::Refused, Some(Err(error))) => check_request_refusal(vector, error),
                (Outcome::Raised, Some(Err(_))) | (Outcome::Refused | Outcome::Raised, None) => {}
                (outcome, _) => panic!("{id}: Python {outcome:?}, and Rust differs"),
            }
        }

        found.check("manifest.request.parse");
    }

    #[test]
    fn a_request_with_a_number_too_large_for_a_float_is_refused() {
        let id: Ulid = "01K5J8M2Q7V3X9R4T6N0B8C2DE".parse().unwrap();
        let file = |ts: &str| {
            format!(
                r#"{{"id": "{id}", "kind": "release", "components": {{"chaperone": "2.1.0"}}, "rollback_of": null, "requested_by": "human", "requester_session": null, "ts": {ts}}}"#
            )
        };
        let largest = format!("1{}", "0".repeat(308));
        let too_large = format!("1{}", "0".repeat(309));

        assert!(Request::parse(file(&largest).as_bytes(), &id).is_ok());
        assert_eq!(
            Request::parse(file(&too_large).as_bytes(), &id),
            Err(RequestError::Malformed(RequestField::Ts))
        );
    }

    fn draft_of(args: &Map<String, Value>) -> Option<Draft> {
        let components = object_field(args, "components")
            .iter()
            .map(|(name, wanted)| (name.clone(), text_of(wanted).to_owned()))
            .collect::<BTreeMap<String, String>>();

        Some(Draft {
            id: text_field(args, "request_id").parse().ok()?,
            kind: text_field(args, "kind").to_owned(),
            components,
            rollback_of: optional_field(args, "rollback_of").map(str::to_owned),
            requested_by: text_field(args, "requested_by").to_owned(),
            requester_session: optional_field(args, "requester_session").map(str::to_owned),
            now: float_of(text_field(args, "now_bits")),
        })
    }

    #[test]
    fn a_requester_plans_the_request_that_the_python_requester_writes() {
        let surface = vectors::surface("manifest.request.plan");
        let mut found = Found::default();
        for vector in &surface.vectors {
            let id = &vector.id;
            let planned = draft_of(vector.input.args().unwrap()).map(|draft| Request::plan(&draft));
            match (vector.result, planned) {
                (Outcome::Accepted, Some(Ok(request))) => check_request(vector, &request),
                (Outcome::Accepted, Some(Err(_)) | None) => {
                    found.deviations.insert(id.clone());
                }
                (Outcome::Refused, Some(Err(error))) => check_request_refusal(vector, error),
                // A draft holds its id as a `Ulid`. An id that is no ULID has
                // no draft in Rust, and the Python requester refuses such an id.
                (Outcome::Raised, Some(Err(_))) | (Outcome::Refused | Outcome::Raised, None) => {}
                (outcome, _) => panic!("{id}: Python {outcome:?}, and Rust differs"),
            }
        }

        found.check("manifest.request.plan");
    }

    #[test]
    fn the_writer_and_the_parser_of_a_request_are_one_round_trip() {
        let id: Ulid = "01K5J8M2Q7V3X9R4T6N0B8C2DE".parse().unwrap();
        let other: Ulid = "01K5J8M2Q7V3X9R4T6N0B8C2DF".parse().unwrap();
        let times = [
            0.0,
            -0.0,
            0.1,
            1_758_153_590.123_456_7,
            5e-324,
            1e22,
            f64::MAX,
        ];
        let kinds = [
            ("release", None),
            ("rollback", Some(other.as_str().to_owned())),
        ];
        let sessions = [None, Some("tui-01K5J7Z9R0P2M4C6H8K1N3V5W7".to_owned())];
        for now in times {
            for (kind, rollback_of) in &kinds {
                for requester_session in &sessions {
                    let draft = Draft {
                        id: id.clone(),
                        kind: (*kind).to_owned(),
                        components: releasable_names()
                            .map(|name| (name.to_owned(), "latest".to_owned()))
                            .chain([("chaperone".to_owned(), "1.2.3".to_owned())])
                            .collect(),
                        rollback_of: rollback_of.clone(),
                        requested_by: "agent-control".to_owned(),
                        requester_session: requester_session.clone(),
                        now,
                    };
                    let planned = Request::plan(&draft).unwrap();
                    let read = Request::parse(&planned.to_bytes(), &id).unwrap();

                    assert_eq!(read, planned);
                    assert_eq!(read.to_bytes(), planned.to_bytes());
                    assert_eq!(read.ts().get().to_bits(), now.to_bits());
                    assert_eq!(read.components().len(), MAX_REQUEST_COMPONENTS);
                }
            }
        }

        assert_eq!(
            Request::parse(
                &Request::plan(&Draft {
                    id: id.clone(),
                    kind: "release".to_owned(),
                    components: BTreeMap::from([("chaperone".to_owned(), "1.2.3".to_owned())]),
                    rollback_of: None,
                    requested_by: "human".to_owned(),
                    requester_session: None,
                    now: 1.0,
                })
                .unwrap()
                .to_bytes(),
                &other
            ),
            Err(RequestError::IdDiffers),
            "the bytes of one request are not the request of another file name"
        );
    }

    #[test]
    fn a_file_at_the_largest_size_can_give_bytes_past_it() {
        let id: Ulid = "01K5J8M2Q7V3X9R4T6N0B8C2DE".parse().unwrap();
        // A file with no space after `,` and `:`. The version fills the file.
        let file = |digits: usize| {
            format!(
                r#"{{"id":"{id}","kind":"release","components":{{"chaperone":"1.2.{}"}},"rollback_of":null,"requested_by":"human","requester_session":null,"ts":1.0}}"#,
                "3".repeat(digits)
            )
        };
        let largest = file(4096 - file(0).len());
        let request = Request::parse(largest.as_bytes(), &id).unwrap();
        let written = request.to_bytes();

        assert_eq!(largest.len(), MAX_REQUEST_BYTES);
        assert_eq!(written.len(), 4110, "the writer adds 14 spaces");
        assert_eq!(Request::parse(&written, &id), Err(RequestError::TooLarge));
    }

    #[test]
    fn a_request_id_is_minted_as_the_python_requester_mints_it() {
        let surface = vectors::surface("manifest.request.ulid");
        let mut found = Found::default();
        for vector in &surface.vectors {
            let id = &vector.id;
            let args = vector.input.args().unwrap();
            let hex = text_field(args, "entropy");
            let bytes: Vec<u8> = hex
                .as_bytes()
                .chunks(2)
                .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
                .collect();
            let entropy: [u8; 10] = bytes.try_into().unwrap();
            // A time below zero is no `Timestamp`, so it has no mint in Rust.
            let minted = Timestamp::new(float_of(text_field(args, "now_bits")))
                .ok()
                .and_then(|now| mint_ulid(now, entropy).ok());

            assert_eq!(vector.result, Outcome::Accepted, "{id}");
            match minted {
                Some(minted) => assert_eq!(
                    &json!({"ulid": minted.as_str()}),
                    vector.value().unwrap(),
                    "{id}"
                ),
                None => {
                    found.deviations.insert(id.clone());
                }
            }
        }

        found.check("manifest.request.ulid");

        let largest = Timestamp::new(281_474_976_710.655).unwrap();
        let past_48_bits = Timestamp::new(281_474_976_710.656).unwrap();

        assert_eq!(
            mint_ulid(largest, [0xff; 10]).unwrap().as_str(),
            "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
        );
        assert_eq!(mint_ulid(past_48_bits, [0; 10]), Err(MintError));
    }

    // --- the live-state document ---

    /// One number of a contract version as the Python code holds it: an integer.
    fn python_int(digits: &str) -> Value {
        let significant = match digits.trim_start_matches('0') {
            "" => "0",
            significant => significant,
        };

        significant
            .parse::<u64>()
            .map_or_else(|_| json!({"$int": significant}), |number| json!(number))
    }

    fn facts_json(facts: &SourceFacts) -> Value {
        json!({
            "sha": facts.sha().map(|sha| sha.as_str()),
            "input_digest": facts.input_digest().map(Digest::as_str),
            "artifact_digest": facts.artifact_digest().map(Digest::as_str),
        })
    }

    fn state_json(state: &ReleaseState) -> Value {
        let live: Map<String, Value> = state
            .live()
            .iter()
            .map(|(name, version)| {
                let version = version.as_ref().map(|version| version.as_str());
                (name.as_str().to_owned(), json!(version))
            })
            .collect();
        let provided: Map<String, Value> = state
            .provided()
            .iter()
            .map(|(contract, version)| {
                let numbers: Vec<Value> = version.as_str().split('.').map(python_int).collect();
                (contract.as_str().to_owned(), json!(numbers))
            })
            .collect();
        let latest: Map<String, Value> = state
            .latest()
            .iter()
            .map(|(name, version)| (name.as_str().to_owned(), json!(version.as_str())))
            .collect();
        let facts: Map<String, Value> = state
            .facts()
            .iter()
            .map(|(name, facts)| (name.as_str().to_owned(), facts_json(facts)))
            .collect();

        json!({"live": live, "provided": provided, "latest": latest, "facts": facts})
    }

    #[test]
    fn a_live_state_document_parses_as_the_python_parser_parses_it() {
        let surface = vectors::surface("manifest.state");
        let label = text_field(&surface.context, "subject");
        let mut found = Found::default();
        for vector in &surface.vectors {
            let id = &vector.id;
            let text = vector.input.text().unwrap();
            match (vector.result, ReleaseState::parse(&text)) {
                (Outcome::Accepted, Ok(state)) => {
                    assert_eq!(&state_json(&state), vector.value().unwrap(), "{id}");
                }
                (Outcome::Accepted, Err(_)) => {
                    found.deviations.insert(id.clone());
                }
                (Outcome::Refused, Err(error)) => {
                    let (check, subject, detail) = refusal(vector);

                    assert_eq!(error.code().as_str(), check, "{id}");
                    assert_eq!(label, subject, "{id}");
                    assert_eq!(error.detail(), detail, "{id}");
                }
                (Outcome::Raised, Err(_)) => {}
                (Outcome::Refused | Outcome::Raised, Ok(_)) => {
                    panic!("{id}: Python refuses the document and Rust accepts it")
                }
            }
        }

        found.check("manifest.state");
    }

    #[test]
    fn a_deep_document_and_a_long_version_are_refused() {
        let long = "1".repeat(4301);
        let deep = format!("{{\"live\": {}{}}}", "[".repeat(30_000), "]".repeat(30_000));
        let version = format!("{{\"provided\": {{\"pep-grant\": \"{long}.0\"}}}}");

        assert_eq!(
            ReleaseState::parse(&deep),
            Err(StateError::NotObject(StateLabel::Map(StateMap::Live)))
        );
        assert_eq!(
            ReleaseState::parse(&version),
            Err(StateError::ContractVersion(ContractId::PepGrant))
        );
        assert_eq!(long.parse::<ContractVersion>().ok(), None);
    }

    // --- the resolved manifest ---

    fn list_of<'a>(args: &'a Map<String, Value>, key: &str) -> &'a Vec<Value> {
        args.get(key).and_then(Value::as_array).unwrap()
    }

    fn number_of(object: &Map<String, Value>, key: &str) -> Option<ContractNumber> {
        let number = object.get(key).and_then(Value::as_u64)?;

        ContractNumber::new(u16::try_from(number).ok()?).ok()
    }

    fn fact_of(facts: &Map<String, Value>, name: &str) -> Option<SourceFacts> {
        let Some(fact) = facts.get(name).and_then(Value::as_object) else {
            return Some(SourceFacts::default());
        };
        let sha = optional_field(fact, "sha")
            .map(str::parse)
            .transpose()
            .ok()?;
        let input = optional_field(fact, "input_digest")
            .map(str::parse)
            .transpose()
            .ok()?;
        let artifact = optional_field(fact, "artifact_digest")
            .map(str::parse)
            .transpose()
            .ok()?;

        Some(SourceFacts::new(sha, input, artifact))
    }

    /// The resolved manifest of the arguments of a vector. `None` when an
    /// argument has no value in its Rust type.
    fn resolved_of(args: &Map<String, Value>) -> Option<ResolvedManifest> {
        let facts = object_field(args, "facts");
        let mut components = Vec::new();
        for row in list_of(args, "components") {
            let row = object_of(row);
            let name = text_field(row, "name");
            components.push(ResolvedComponent::new(
                name.parse().ok()?,
                text_field(row, "action").parse().ok()?,
                optional_field(row, "from_version")
                    .map(str::parse)
                    .transpose()
                    .ok()?,
                optional_field(row, "to_version")
                    .map(str::parse)
                    .transpose()
                    .ok()?,
                fact_of(facts, name)?,
            ));
        }

        let mut order = Vec::new();
        for name in list_of(args, "order") {
            order.push(text_of(name).parse().ok()?);
        }

        let mut contracts = Vec::new();
        for row in list_of(args, "contracts") {
            let row = object_of(row);
            let mut consumers = Vec::new();
            for consumer in list_of(row, "consumers") {
                let consumer = object_of(consumer);
                consumers.push(Consumer::new(
                    text_field(consumer, "name").parse().ok()?,
                    number_of(consumer, "major")?,
                    number_of(consumer, "min_minor")?,
                ));
            }

            contracts.push(ContractRow::new(
                text_field(row, "contract").parse().ok()?,
                text_field(row, "provider").parse().ok()?,
                number_of(row, "major")?,
                number_of(row, "minor")?,
                consumers,
            ));
        }

        Some(ResolvedManifest::new(
            text_field(args, "id").parse().ok()?,
            ResolvedAt::new(float_of(text_field(args, "resolved_at_bits"))).ok()?,
            text_field(args, "requested_by").parse().ok()?,
            components,
            order,
            contracts,
        ))
    }

    /// Each field of a resolved manifest but `resolved_at` and the hash.
    fn resolved_json(manifest: &ResolvedManifest) -> Value {
        let components: Vec<Value> = manifest
            .components()
            .iter()
            .map(|row| {
                json!({
                    "name": row.name().as_str(),
                    "action": row.action().as_str(),
                    "from_version": row.from_version().map(|version| version.as_str()),
                    "to_version": row.to_version().map(|version| version.as_str()),
                    "tag": row.tag(),
                    "sha": row.facts().sha().map(|sha| sha.as_str()),
                    "input_digest": row.facts().input_digest().map(Digest::as_str),
                    "artifact_digest": row.facts().artifact_digest().map(Digest::as_str),
                })
            })
            .collect();
        let contracts: Vec<Value> = manifest
            .contracts()
            .iter()
            .map(|row| {
                let consumers: Vec<Value> = row
                    .consumers()
                    .iter()
                    .map(|consumer| {
                        json!({
                            "name": consumer.name().as_str(),
                            "major": consumer.major().get(),
                            "min_minor": consumer.min_minor().get(),
                        })
                    })
                    .collect();

                json!({
                    "contract": row.contract().as_str(),
                    "provider": row.provider().as_str(),
                    "version": row.version(),
                    "consumers": consumers,
                })
            })
            .collect();
        let order: Vec<&str> = manifest.order().iter().map(|name| name.as_str()).collect();

        json!({
            "manifest_version": MANIFEST_VERSION,
            "id": manifest.id().as_str(),
            "requested_by": manifest.requested_by().as_str(),
            "components": components,
            "order": order,
            "contracts": contracts,
        })
    }

    #[test]
    fn a_resolved_manifest_has_the_bytes_and_the_hash_of_the_python_one() {
        let surface = vectors::surface("manifest.resolved");
        let mut found = Found::default();
        for vector in &surface.vectors {
            let id = &vector.id;
            let built = resolved_of(vector.input.args().unwrap());
            match (vector.result, built) {
                (Outcome::Accepted, Some(manifest)) => {
                    let mut value = vector.value().unwrap().clone();
                    let fields = value.as_object_mut().unwrap();
                    let hash = fields.remove("manifest_sha256").unwrap();
                    fields.remove("resolved_at").unwrap();
                    let output = String::from_utf8(bytes_of(vector, "output")).unwrap();

                    assert_eq!(manifest.canonical_json(), output, "{id}");
                    assert_eq!(json!(manifest.sha256().as_str()), hash, "{id}");
                    assert_eq!(resolved_json(&manifest), value, "{id}");
                }
                (Outcome::Accepted, None) => {
                    found.deviations.insert(id.clone());
                }
                // The Python builder checks the id and `requested_by`. In Rust
                // the type of each argument holds its check, so a refused vector
                // has no value to build a manifest from.
                (Outcome::Refused | Outcome::Raised, None) => {}
                (Outcome::Refused | Outcome::Raised, Some(_)) => {
                    panic!("{id}: Python refuses the arguments and Rust builds a manifest")
                }
            }
        }

        found.check("manifest.resolved");
    }

    #[test]
    fn the_gate_and_the_action_are_the_python_ones() {
        let surface = vectors::surface("manifest.gate");
        for vector in &surface.vectors {
            let id = &vector.id;
            let args = vector.input.args().unwrap();
            let hash: Digest = text_field(args, "manifest_sha256").parse().unwrap();
            let request: Ulid = text_field(args, "id").parse().unwrap();
            let gate: GateId = gate_id(&hash).unwrap();
            let action = release_action(&request, &gate);
            let value = json!({
                "gate": gate.as_str(),
                "action_id": action_id(&request),
                "action": action,
                "action_matches": true,
            });
            let parts: Vec<&str> = action.split('_').collect();

            assert_eq!(&value, vector.value().unwrap(), "{id}");
            assert_eq!(parts.len(), 4, "{id}: the action has four parts");
            assert_eq!(parts[..2], ["AGENT", "APPROVE"], "{id}");
            assert!(
                parts[2].len() == 26
                    && parts[2]
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || byte.is_ascii_lowercase()),
                "{id}"
            );
            assert_eq!(parts[3], gate.as_str(), "{id}");
        }
    }

    #[test]
    fn a_summary_is_cut_as_the_python_summary_is_cut() {
        let surface = vectors::surface("manifest.summary");
        for vector in &surface.vectors {
            let args = vector.input.args().unwrap();
            let field = |key: &str| text_field(args, key).to_owned();
            let summary = Summary::new(SummaryFields {
                review: field("review"),
                components: field("components"),
                contracts: field("contracts"),
                restarts: field("restarts"),
                restore: field("restore"),
                requested_by: field("requested_by"),
                manifest: field("manifest"),
            });
            let value: Map<String, Value> = summary
                .fields()
                .into_iter()
                .map(|(name, text)| (name.to_owned(), json!(text)))
                .collect();

            assert_eq!(
                &Value::Object(value),
                vector.value().unwrap(),
                "{}",
                vector.id
            );
        }
    }

    // --- the types with a grammar of their own ---

    #[test]
    fn a_unit_name_has_its_grammar() {
        let accepted = [
            "a",
            "creche-attendance.service",
            "creche-trigger@.service",
            "A_b.9",
        ];
        let refused = [
            ("", UnitNameError::Empty),
            ("bad unit", UnitNameError::BadByte { at: 3 }),
            ("a/b.service", UnitNameError::BadByte { at: 1 }),
            ("x.service\n", UnitNameError::BadByte { at: 9 }),
            ("\u{661}", UnitNameError::BadByte { at: 0 }),
        ];
        for text in accepted {
            assert_eq!(text.parse::<UnitName>().unwrap().as_str(), text);
        }

        for (text, error) in refused {
            assert_eq!(text.parse::<UnitName>(), Err(error), "{text:?}");
        }

        assert!("u".repeat(64).parse::<UnitName>().is_ok());
        assert_eq!(
            "u".repeat(65).parse::<UnitName>(),
            Err(UnitNameError::TooLong)
        );
    }

    #[test]
    fn a_path_has_its_grammar() {
        let absolute = [
            "/a",
            "/opt/components/x.prev",
            "//opt/x",
            "/opt/caf\u{e9}",
            "/opt/a b",
        ];
        let not_absolute = [
            ("", AbsolutePathError::NotAbsolute),
            ("opt/x", AbsolutePathError::NotAbsolute),
            ("~/x", AbsolutePathError::NotAbsolute),
            ("/", AbsolutePathError::NotAbsolute),
            ("/opt/x/", AbsolutePathError::NotAbsolute),
            ("/opt/\0", AbsolutePathError::NotAbsolute),
            ("/opt//x", AbsolutePathError::NotNormalized),
            ("///opt/x", AbsolutePathError::NotNormalized),
            ("/opt/./x", AbsolutePathError::NotNormalized),
            ("/opt/x/.", AbsolutePathError::NotNormalized),
            ("/opt/../etc", AbsolutePathError::Parent),
            ("/opt/x/..", AbsolutePathError::Parent),
        ];
        for text in absolute {
            assert_eq!(text.parse::<AbsolutePath>().unwrap().as_str(), text);
        }

        for (text, error) in not_absolute {
            assert_eq!(text.parse::<AbsolutePath>(), Err(error), "{text:?}");
        }

        assert!(
            format!("/{}", "p".repeat(255))
                .parse::<AbsolutePath>()
                .is_ok()
        );
        assert_eq!(
            format!("/{}", "p".repeat(256)).parse::<AbsolutePath>(),
            Err(AbsolutePathError::TooLong)
        );

        let in_repo = [".", "attendance", "a/b.c/d_e-f"];
        for text in in_repo {
            assert_eq!(text.parse::<RepoPath>().unwrap().as_str(), text);
        }

        assert!(".".parse::<RepoPath>().unwrap().is_whole_repo());
        assert_eq!("/a".parse::<RepoPath>(), Err(RepoPathError::NotPlain));
        assert_eq!("a\\b".parse::<RepoPath>(), Err(RepoPathError::NotPlain));
        for text in ["", "a//b", "a/../b", "./a", "a/", "a b", "caf\u{e9}", "a\n"] {
            assert!(
                matches!(text.parse::<RepoPath>(), Err(RepoPathError::Segment(_))),
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_digest_and_a_safe_token_have_their_grammar() {
        let hex = "0123456789abcdef".repeat(4);

        assert_eq!(
            format!("sha256:{hex}").parse::<Digest>().unwrap().hex(),
            hex
        );
        assert_eq!(hex.parse::<Digest>(), Err(DigestError::NoPrefix));
        assert!(matches!(
            format!("sha512:{hex}").parse::<Digest>(),
            Err(DigestError::NoPrefix)
        ));
        assert!(matches!(
            format!("sha256:{}", hex.to_uppercase()).parse::<Digest>(),
            Err(DigestError::Hex(_))
        ));
        assert!(matches!(
            format!("sha256:{hex}\n").parse::<Digest>(),
            Err(DigestError::Hex(_))
        ));
        for text in ["a", "A_b.9:/=@-", &"k".repeat(64)] {
            assert_eq!(SafeToken::of(text).as_str(), text);
        }

        for text in ["", "a b", "a\n", "\u{1b}[2J", "caf\u{e9}", &"k".repeat(65)] {
            assert_eq!(SafeToken::of(text).to_string(), "<unprintable>", "{text:?}");
        }
    }

    #[test]
    fn a_bounded_number_has_its_range() {
        assert_eq!(ContractNumber::new(0).unwrap().get(), 0);
        assert_eq!(ContractNumber::new(999).unwrap(), ContractNumber::MAX);
        assert_eq!(ContractNumber::new(1000), Err(ContractNumberError));
        assert_eq!(Timeout::new(0), Err(TimeoutError));
        assert_eq!(Timeout::new(1).unwrap().get(), 1);
        assert_eq!(Timeout::new(300).unwrap().get(), 300);
        assert_eq!(Timeout::new(301), Err(TimeoutError));
        assert_eq!(Keep::new(0), Err(KeepError));
        assert_eq!(Keep::new(10).unwrap().get(), 10);
        assert_eq!(Keep::new(11), Err(KeepError));
        assert_eq!(
            Timestamp::new(-0.0).unwrap().get().to_bits(),
            (-0.0_f64).to_bits()
        );
        assert_eq!(Timestamp::new(-1e-9), Err(TimestampError));
        assert_eq!(Timestamp::new(f64::INFINITY), Err(TimestampError));
        assert_eq!(ResolvedAt::new(-1.5).unwrap().get(), -1.5);
        assert_eq!(ResolvedAt::new(f64::NAN), Err(ResolvedAtError));
        assert!("human".parse::<Requester>().is_ok());
        assert!("Human".parse::<Requester>().is_err());
        assert!("human\n".parse::<Requester>().is_err());
    }
}
