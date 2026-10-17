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
//! Two private modules read and write the text forms. Each one gives what
//! the Python library of the release tool gives: `yaml` for `PyYAML` and
//! `json` for the `json` module. The hash is `creche_util::sha256`, which
//! gives what `hashlib` gives.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use creche_util::{hex, sha256};

use crate::ids::{Sha256Hex, Sha256HexError};

mod component;
mod json;
mod request;
mod resolved;
mod state;
mod yaml;

/// The version of the `component-manifest` contract that the manifest of the
/// release tool provides: the two numbers, and their text.
///
/// The build script of the crate writes the file of this module. It reads
/// the `provides` row for the contract in `handover/component.yaml`, so that
/// row is the one source of the version (rust/AGENTS.md, rule 13).
mod provided {
    include!(concat!(env!("OUT_DIR"), "/manifest_contract.rs"));
}

// Each number of the row is a number of a contract version. The build script
// holds no bound of its own, so the build fails here for a number that
// `ContractNumber` refuses.
const _: () = assert!(
    ContractNumber::new(provided::MAJOR).is_ok() && ContractNumber::new(provided::MINOR).is_ok(),
    "a number of the `provides` row is no number of a contract version"
);

/// The build script of the crate, as a module of the test build. Its tests
/// hold the reader of the `provides` row.
#[cfg(test)]
#[path = "../build.rs"]
mod build_script;

pub use component::{
    AbsolutePath, AbsolutePathError, Argv, Build, CargoArgv, ComponentManifest, Install, Keep,
    KeepError, MAX_MANIFEST_BYTES, ManifestError, ManifestFault, Operator, OperatorError,
    PathField, Provided, RepoPath, RepoPathError, Required, Restore, Scope, Timeout, TimeoutError,
    UnitName, UnitNameError, Verify, YamlFault,
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
        /// The sixth check of the provenance predicate: the signature and
        /// the member list of the archive of a `binary` component.
        P6 => "P6",
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

/// The first word of each `build` command of a `binary` component: the name
/// of the program, with no path. A CI runner has no fixed path for the
/// program. The `build` command of each other kind starts with an absolute
/// path (contract 06 §8).
pub const BINARY_BUILD_PROGRAM: &str = "cargo";

/// The one target of a `binary` build in CI: a static program for Linux on
/// x86_64, with no C library of the host.
pub const BINARY_TARGET: &str = "x86_64-unknown-linux-musl";

/// The end of the name of the archive of a `binary` component:
/// `<component>-<BINARY_TARGET><ARCHIVE_SUFFIX>`, a gzip of a tar.
pub const ARCHIVE_SUFFIX: &str = ".tar.gz";

/// The end of the name of the signature bundle of an archive: the name of
/// the archive, then this.
pub const BUNDLE_SUFFIX: &str = ".sigstore.json";

/// The variable that tells `cargo install` where to write the programs of
/// a `binary` build.
pub const BINARY_INSTALL_ENV: &str = "CARGO_INSTALL_ROOT";

/// The workflow file that signs the archives of each repository. A
/// repository with no entry has no signer, so a `binary` component of that
/// repository gets no tag.
pub const RELEASE_WORKFLOW: [(Repo, &str); 1] = [(Repo::AgentControl, "release.yml")];

/// The one branch that a release comes from. A signature names the ref of
/// this branch.
pub const MAIN_BRANCH: &str = "main";

/// The first number of the newest `component-manifest` contract version
/// that this code takes. It is the number of the `provides` row in the
/// manifest of the release tool.
pub const MANIFEST_CONTRACT_MAJOR: u16 = provided::MAJOR;

/// The second number of that version, from the same row. A manifest that
/// declares `0.MINOR` with a `MINOR` at or below this one reads (contract
/// 06 §3.1).
pub const MANIFEST_CONTRACT_MINOR: u16 = provided::MINOR;

/// The component that deploys last, whatever `depends_on` says (contract 06
/// §1.1).
pub const LAST_IN_ORDER: &str = "handover";

/// The largest count of components that one request names: the catalog less
/// `registry-data`, which never releases (`stage7-releases.md` §2.3).
pub const MAX_REQUEST_COMPONENTS: usize = 8;

/// The largest archive of a `binary` component, in bytes.
pub const ARCHIVE_MAX_BYTES: u64 = 256 * 1024 * 1024;

/// The largest signature bundle of an archive, in bytes.
pub const BUNDLE_MAX_BYTES: u64 = 1024 * 1024;

/// The largest count of members of an archive.
pub const ARCHIVE_MAX_MEMBERS: usize = 64;

/// The largest count of bytes of the members of an archive after the
/// unpack.
pub const ARCHIVE_MAX_UNPACKED_BYTES: u64 = 512 * 1024 * 1024;

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
        Self(format!(
            "{DIGEST_PREFIX}{}",
            hex::lower(&sha256::digest(bytes))
        ))
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
    //! vectors of the Python release tool, the `manifest.` surfaces.

    use std::collections::{BTreeMap, BTreeSet};

    use creche_vectors::{self as vectors, Outcome, Vector};
    use serde_json::{Map, Value, json};

    use super::*;
    use crate::ids::{
        ComponentName, ContractVersion, FamilyNameError, GateId, Ulid, Version, VersionError,
    };

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
            .unwrap_or_else(|| panic!("{}: no refusal", vector.id()));

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

    /// The bytes of the `output` of a vector.
    fn output_of(vector: &Vector) -> Vec<u8> {
        vector.output().unwrap().bytes().unwrap()
    }

    #[test]
    fn each_surface_of_the_module_is_in_the_table() {
        let named: BTreeSet<&str> = SURFACES.iter().map(|(surface, _)| *surface).collect();
        let index = vectors::index().unwrap();
        let indexed: BTreeSet<&str> = index
            .iter()
            .map(|row| row.surface())
            .filter(|surface| surface.starts_with("manifest."))
            .collect();

        assert_eq!(indexed, named);
    }

    // --- component.yaml ---

    fn argv_json(argv: &Argv) -> Value {
        json!(argv.words())
    }

    fn build_json(build: &Build) -> Value {
        match build {
            Build::Root(commands) => json!(commands.iter().map(argv_json).collect::<Vec<_>>()),
            Build::Ci(commands) => json!(
                commands
                    .iter()
                    .map(|command| command.words().collect::<Vec<_>>())
                    .collect::<Vec<_>>()
            ),
        }
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
            "build": build_json(manifest.build()),
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
        let surface = vectors::surface("manifest.component").unwrap();
        let label = text_field(surface.context(), "subject");
        let account = surface
            .context()
            .get("operator")
            .unwrap()
            .as_object()
            .unwrap();
        let operator =
            Operator::new(text_field(account, "user"), text_field(account, "home")).unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let text = vector.input().text().unwrap();
            let site = text_field(vector.field("params").unwrap().as_object().unwrap(), "site");
            let operator = match site {
                "set" => Some(&operator),
                "missing" => None,
                other => panic!("{id}: the site {other}"),
            };
            let read = ComponentManifest::parse(text, operator);
            match (vector.result(), read) {
                (Outcome::Accepted, Ok(manifest)) => {
                    assert_eq!(&manifest_json(&manifest), vector.value().unwrap(), "{id}");
                }
                (Outcome::Accepted, Err(_)) => {
                    panic!("{id}: Python accepts the manifest and Rust refuses it")
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

                    assert_eq!(error.detail(), detail, "{id}");
                }
                (Outcome::Raised, Err(_)) => {}
                (Outcome::Refused | Outcome::Raised, Ok(_)) => {
                    panic!("{id}: Python refuses the manifest and Rust accepts it")
                }
            }
        }
    }

    /// The text of a list of fields, with the lines of one field replaced.
    fn with_field(fields: &[(&str, &str)], field: &str, lines: &str) -> String {
        assert!(fields.iter().any(|(name, _)| *name == field), "{field}");

        fields
            .iter()
            .map(|(name, own)| if *name == field { lines } else { own })
            .fold(String::new(), |text, lines| text + lines + "\n")
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

        with_field(&lines, field, line)
    }

    #[test]
    fn the_kind_gives_the_variant_of_the_build() {
        let read = |kind: &str, command: &str| {
            let text = manifest_with("kind", &format!("kind: {kind}"))
                + &format!("build: [[{command}, install, \"--locked\"]]\n");

            ComponentManifest::parse(&text, None)
        };

        let binary = read("binary", "cargo").unwrap();
        let Build::Ci(commands) = binary.build() else {
            panic!("a binary build is {:?}", binary.build());
        };
        let words: Vec<Vec<&str>> = commands
            .iter()
            .map(|command| command.words().collect())
            .collect();
        assert_eq!(words, [["cargo", "install", "--locked"]]);
        assert_eq!(commands[0].program(), BINARY_BUILD_PROGRAM);
        assert_eq!(commands[0].arguments(), ["install", "--locked"]);

        let venv = read("venv", "/usr/bin/cargo").unwrap();
        let Build::Root(commands) = venv.build() else {
            panic!("a venv build is {:?}", venv.build());
        };
        assert_eq!(commands[0].program().as_str(), "/usr/bin/cargo");

        let fetched = ComponentManifest::parse(&manifest_with("kind", "kind: binary"), None);
        assert_eq!(fetched.unwrap().build(), &Build::Ci(Vec::new()));
    }

    #[test]
    fn a_binary_build_starts_with_the_word_and_no_other() {
        let fault = |kind: &str, command: &str| {
            let text = manifest_with("kind", &format!("kind: {kind}"))
                + &format!("build: [[\"{command}\", build]]\n");

            ComponentManifest::parse(&text, None).unwrap_err()
        };

        // `~/cargo` gives no site refusal: the reader expands no `~/` in
        // that word, and the parse has no operator account.
        for word in [
            "/usr/bin/cargo",
            "~/cargo",
            "bin/cargo",
            "Cargo",
            "cargo-build",
            "rustc",
        ] {
            let error = fault("binary", word);

            assert_eq!(error.code(), RefusalCode::Manifest, "{word}");
            assert_eq!(error.fault(), &ManifestFault::BuildProgram, "{word}");
            assert_eq!(
                error.detail(),
                "field 'build[0]' must be the word cargo",
                "{word}"
            );
        }

        for kind in ["venv", "oci-image", "compose", "data"] {
            let error = fault(kind, BINARY_BUILD_PROGRAM);

            assert_eq!(
                error.detail(),
                "field 'build[0]' must be an absolute path",
                "{kind}"
            );
        }
    }

    #[test]
    fn a_binary_build_word_that_is_not_unicode_is_refused_first() {
        // The Python reader takes each of the two commands. It refuses the
        // first one as a first word that is not cargo, and accepts the second.
        for command in [r#"["\ud800", build]"#, r#"[cargo, "\ud800"]"#] {
            let text = manifest_with("kind", "kind: binary") + &format!("build: [{command}]\n");

            let error = ComponentManifest::parse(&text, None).unwrap_err();

            assert_eq!(error.code(), RefusalCode::Manifest, "{command}");
            assert_eq!(
                error.fault(),
                &ManifestFault::NotUnicode { field: "build" },
                "{command}"
            );
        }
    }

    #[test]
    fn a_manifest_reads_up_to_the_version_of_the_provides_row() {
        let read = |minor: u16| {
            let line = format!("manifest_version: \"{MANIFEST_CONTRACT_MAJOR}.{minor}\"");

            ComponentManifest::parse(&manifest_with("manifest_version", &line), None)
        };
        let next = MANIFEST_CONTRACT_MINOR + 1;

        let newest = read(MANIFEST_CONTRACT_MINOR).unwrap();
        let refused = read(next).unwrap_err();

        assert_eq!(newest.manifest_version().as_str(), MANIFEST_VERSION);
        assert_eq!(refused.code(), RefusalCode::Manifest);
        assert_eq!(
            refused.detail(),
            format!(
                "manifest_version {MANIFEST_CONTRACT_MAJOR}.{next} is outside \
                 {MANIFEST_CONTRACT_MAJOR}.0 to {MANIFEST_VERSION}"
            )
        );
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
            ComponentManifest::parse(&manifest_with(field, line), None)
                .map_err(|error| error.fault().clone())
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

    /// The valid manifest that the generator of the `manifest.component`
    /// vectors starts from: each field and its lines, but the version. The
    /// generator writes the newest version that the reader takes, so
    /// `generator_manifest_with` makes that line from `MANIFEST_VERSION`.
    const GENERATOR_FIELDS: [(&str, &str); 15] = [
        ("name", "name: attendance"),
        ("repo", "repo: agent-control"),
        ("path", "path: attendance"),
        ("kind", "kind: venv"),
        ("unit", "unit: creche-attendance.service"),
        ("runs_as", "runs_as: operator"),
        (
            "build",
            "build:\n  - [\"/usr/local/bin/uv\", \"sync\", \"--frozen\", \"--no-editable\", \
             \"--package\", \"attendance\"]",
        ),
        (
            "install",
            "install:\n  to: ~/.local/components/attendance\n  prev: \
             ~/.local/components/attendance.prev",
        ),
        (
            "provides",
            "provides:\n  - { contract: session-api, major: 1, minor: 4 }",
        ),
        (
            "requires",
            "requires:\n  - { contract: channel, major: 1, min_minor: 3 }\n  - { contract: \
             pep-grant, major: 2, min_minor: 0 }",
        ),
        ("depends_on", "depends_on: [chaperone]"),
        (
            "verify",
            "verify:\n  command: [\"~/.local/components/attendance/bin/attendance-verify\", \
             \"--json\"]\n  user: operator\n  timeout_s: 60",
        ),
        ("restore", "restore:\n  mode: automatic\n  keep: 1"),
        ("secrets", "secrets: [session_key]"),
        ("release", "release: yes"),
    ];

    /// The manifest of the generator with the lines of one field replaced.
    fn generator_manifest_with(field: &str, lines: &str) -> String {
        let version = format!("manifest_version: \"{MANIFEST_VERSION}\"");
        let mut fields = vec![("manifest_version", version.as_str())];
        fields.extend(GENERATOR_FIELDS);

        with_field(&fields, field, lines)
    }

    #[test]
    fn a_manifest_that_only_the_python_reader_takes_is_refused() {
        // `handover.manifest.parse_manifest` takes each of these manifests
        // but the first one. No vector holds one: no contract names the
        // input, and a person writes a manifest. The operator account is the
        // account of the vectors.
        let operator = Operator::new("keeper", "/home/keeper").unwrap();
        let nested = format!("unit: {}{}", "[".repeat(200), "]".repeat(200));
        let fault_of = |field: &str, lines: &str| {
            let text = generator_manifest_with(field, lines);

            ComponentManifest::parse(&text, Some(&operator))
                .map_err(|error| (error.code(), error.fault().clone()))
        };

        assert!(fault_of("unit", "unit: creche-attendance.service").is_ok());
        for (kind, field, lines, fault) in [
            // The Python reader refuses this one too: the unit is no text. It
            // gives another detail, because PyYAML has no nesting limit.
            (
                "a flow sequence of 200 levels",
                "unit",
                nested.clone(),
                ManifestFault::Yaml(YamlFault::Deep),
            ),
            (
                "a flow sequence of 200 levels in a key that a later key replaces",
                "unit",
                format!("{nested}\nunit: creche-attendance.service"),
                ManifestFault::Yaml(YamlFault::Deep),
            ),
            // U+0664, a decimal digit that is not ASCII. Python reads 4.
            (
                "an integer tag on a digit that is not ASCII",
                "restore",
                "restore:\n  mode: automatic\n  keep: !!int \"\u{664}\"".to_owned(),
                ManifestFault::Yaml(YamlFault::Line(25)),
            ),
            // U+00A0, a space that is not ASCII. Python reads 4.
            (
                "an integer tag on a digit and a space that is not ASCII",
                "restore",
                "restore:\n  mode: automatic\n  keep: !!int \"4\u{a0}\"".to_owned(),
                ManifestFault::Yaml(YamlFault::Line(25)),
            ),
            (
                "a word of a command with a lone surrogate",
                "build",
                "build:\n  - [\"/bin/a\", \"\\ud800\"]".to_owned(),
                ManifestFault::NotUnicode { field: "build" },
            ),
            (
                "an install path with a lone surrogate",
                "install",
                "install:\n  to: \"/opt/\\ud800\"\n  prev: /opt/x.prev".to_owned(),
                ManifestFault::NotUnicode {
                    field: "install.to",
                },
            ),
        ] {
            assert_eq!(
                fault_of(field, &lines),
                Err((RefusalCode::Manifest, fault)),
                "{kind}"
            );
        }
    }

    #[test]
    fn a_manifest_error_gives_the_scope_of_its_fault() {
        let scope_of = |field: &str, line: &str| {
            ComponentManifest::parse(&manifest_with(field, line), None)
                .unwrap_err()
                .scope()
        };
        let scopes = [
            ("kind", "kind: []", Scope::Top),
            (
                "install",
                "install: {to: /opt/x, prev: /opt/x.prev, bogus: 1}",
                Scope::Install,
            ),
            (
                "verify",
                "verify: {command: [/bin/true], user: root, timeout_s: 5, bogus: 1}",
                Scope::Verify,
            ),
            (
                "restore",
                "restore: {mode: automatic, keep: 1, bogus: 1}",
                Scope::Restore,
            ),
        ];

        for (field, line, scope) in scopes {
            assert_eq!(scope_of(field, line), scope, "{line}");
        }
    }

    #[test]
    fn the_operator_account_is_checked_as_the_site_file_reader_checks_it() {
        let surface = vectors::surface("manifest.operator").unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let args = vector.input().args().unwrap();
            let made = Operator::new(text_field(args, "user"), text_field(args, "home"));
            match (vector.result(), made) {
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
            "binary-build-program" => json!(BINARY_BUILD_PROGRAM),
            "binary-target" => json!(BINARY_TARGET),
            "archive-suffix" => json!(ARCHIVE_SUFFIX),
            "bundle-suffix" => json!(BUNDLE_SUFFIX),
            "binary-install-env" => json!(BINARY_INSTALL_ENV),
            "release-workflow" => RELEASE_WORKFLOW
                .iter()
                .map(|(repo, file)| (repo.as_str().to_owned(), json!(file)))
                .collect::<Map<String, Value>>()
                .into(),
            "main-branch" => json!(MAIN_BRANCH),
            "manifest-contract-version" => {
                json!({"major": MANIFEST_CONTRACT_MAJOR, "minor": MANIFEST_CONTRACT_MINOR})
            }
            "last-in-order" => json!(LAST_IN_ORDER),
            "max-request-components" => json!(MAX_REQUEST_COMPONENTS),
            "archive-max-bytes" => json!(ARCHIVE_MAX_BYTES),
            "bundle-max-bytes" => json!(BUNDLE_MAX_BYTES),
            "archive-max-members" => json!(ARCHIVE_MAX_MEMBERS),
            "archive-max-unpacked-bytes" => json!(ARCHIVE_MAX_UNPACKED_BYTES),
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
        let surface = vectors::surface("manifest.catalog").unwrap();
        for vector in surface.vectors() {
            assert_eq!(vector.result(), Outcome::Accepted, "{}", vector.id());
            assert_eq!(
                &catalog_value(vector.id()),
                vector.value().unwrap(),
                "{}",
                vector.id()
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

    /// The generator makes one vector `code-<name>` for each member of the
    /// Python enum, in its order. A written text can also be a code.
    #[test]
    fn a_refusal_code_is_one_of_the_python_codes() {
        let surface = vectors::surface("manifest.refusal_code").unwrap();
        let mut members = Vec::new();
        for vector in surface.vectors() {
            let text = vector.input().text().unwrap();
            match (vector.result(), text.parse::<RefusalCode>()) {
                (Outcome::Accepted, Ok(code)) => {
                    assert_eq!(
                        &json!(code.as_str()),
                        vector.value().unwrap(),
                        "{}",
                        vector.id()
                    );
                    if vector.id().starts_with("code-") {
                        members.push(code);
                    }
                }
                (Outcome::Refused | Outcome::Raised, Err(UnknownRefusalCode)) => {}
                (outcome, code) => panic!("{}: Python {outcome:?}, Rust {code:?}", vector.id()),
            }
        }

        assert_eq!(
            members,
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
        let id = vector.id();
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
        assert_eq!(written, output_of(vector), "{id}");
        assert_eq!(
            &Request::parse(&written, request.id()).unwrap(),
            request,
            "{id}"
        );
    }

    fn check_request_refusal(vector: &Vector, error: RequestError) {
        let (check, subject, detail) = refusal(vector);

        assert_eq!(error.code().as_str(), check, "{}", vector.id());
        assert_eq!(error.subject(), subject, "{}", vector.id());
        assert_eq!(error.detail(), detail, "{}", vector.id());
    }

    #[test]
    fn a_request_file_parses_as_the_executor_parses_it() {
        let surface = vectors::surface("manifest.request.parse").unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let raw = vector.input().bytes().unwrap();
            let params = vector.field("params").unwrap().as_object().unwrap();
            // The parser takes the id of the file name as a `Ulid`. A file name
            // that is no ULID has no request in Rust, and the Python parser
            // refuses such an id.
            let parsed = text_field(params, "request_id")
                .parse::<Ulid>()
                .ok()
                .map(|request_id| Request::parse(&raw, &request_id));
            match (vector.result(), parsed) {
                (Outcome::Accepted, Some(Ok(request))) => check_request(vector, &request),
                (Outcome::Refused, Some(Err(error))) => check_request_refusal(vector, error),
                (Outcome::Raised, Some(Err(_))) | (Outcome::Refused | Outcome::Raised, None) => {}
                (outcome, _) => panic!("{id}: Python {outcome:?}, and Rust differs"),
            }
        }
    }

    #[test]
    fn a_request_that_is_not_strict_keeps_the_refusal_for_the_notice() {
        let id: Ulid = "01K5J8M2Q7V3X9R4T6N0B8C2DE".parse().unwrap();
        let twice = format!(r#"{{"id": "{id}", "id": "{id}"}}"#);
        let refused = Request::parse(twice.as_bytes(), &id).unwrap_err();

        assert_eq!(refused.detail(), "is not a JSON object");
        assert_eq!(
            refused
                .not_strict()
                .map(|refusal| (refusal.rule(), refusal.at())),
            Some((crate::json::Rule::DuplicateKey, 37))
        );
        assert_eq!(RequestError::NotObject.not_strict(), None);
        assert_eq!(
            Request::parse(b"[]", &id).map_err(|error| error.not_strict().is_some()),
            Err(false)
        );
    }

    #[test]
    fn a_request_with_a_number_outside_the_ranges_of_strict_json_is_refused() {
        let id: Ulid = "01K5J8M2Q7V3X9R4T6N0B8C2DE".parse().unwrap();
        let file = |ts: &str| {
            format!(
                r#"{{"id": "{id}", "kind": "release", "components": {{"chaperone": "2.1.0"}}, "rollback_of": null, "requested_by": "human", "requester_session": null, "ts": {ts}}}"#
            )
        };
        let rule = |ts: &str| {
            Request::parse(file(ts).as_bytes(), &id)
                .unwrap_err()
                .not_strict()
                .map(crate::json::NotStrict::rule)
        };

        assert!(Request::parse(file("18446744073709551615").as_bytes(), &id).is_ok());
        assert!(Request::parse(file("1.7976931348623157e308").as_bytes(), &id).is_ok());
        assert_eq!(
            rule("18446744073709551616"),
            Some(crate::json::Rule::IntegerRange)
        );
        assert_eq!(
            rule(&format!("1{}", "0".repeat(308))),
            Some(crate::json::Rule::IntegerRange)
        );
        assert_eq!(rule("1e309"), Some(crate::json::Rule::FloatRange));
    }

    fn draft_of(args: &Map<String, Value>) -> Option<Draft> {
        let components = object_field(args, "components")
            .iter()
            .map(|(name, wanted)| (name.clone(), text_of(wanted).to_owned()))
            .collect::<BTreeMap<String, String>>();
        let draft = Draft::new(
            text_field(args, "request_id").parse().ok()?,
            text_field(args, "kind").to_owned(),
            components,
            text_field(args, "requested_by").to_owned(),
            float_of(text_field(args, "now_bits")),
        );

        Some(with_optional_parts(
            draft,
            optional_field(args, "rollback_of"),
            optional_field(args, "requester_session"),
        ))
    }

    /// The draft with each optional part that the caller holds.
    fn with_optional_parts(
        draft: Draft,
        rollback_of: Option<&str>,
        requester_session: Option<&str>,
    ) -> Draft {
        let draft = match rollback_of {
            Some(target) => draft.with_rollback_of(target.to_owned()),
            None => draft,
        };

        match requester_session {
            Some(session) => draft.with_requester_session(session.to_owned()),
            None => draft,
        }
    }

    #[test]
    fn a_requester_plans_the_request_that_the_python_requester_writes() {
        let surface = vectors::surface("manifest.request.plan").unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let planned =
                draft_of(vector.input().args().unwrap()).map(|draft| Request::plan(&draft));
            match (vector.result(), planned) {
                (Outcome::Accepted, Some(Ok(request))) => check_request(vector, &request),
                (Outcome::Refused, Some(Err(error))) => check_request_refusal(vector, error),
                // A draft holds its id as a `Ulid`. An id that is no ULID has
                // no draft in Rust, and the Python requester refuses such an id.
                (Outcome::Raised, Some(Err(_))) | (Outcome::Refused | Outcome::Raised, None) => {}
                (outcome, _) => panic!("{id}: Python {outcome:?}, and Rust differs"),
            }
        }
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
                    let draft = with_optional_parts(
                        Draft::new(
                            id.to_owned(),
                            (*kind).to_owned(),
                            releasable_names()
                                .map(|name| (name.to_owned(), "latest".to_owned()))
                                .chain([("chaperone".to_owned(), "1.2.3".to_owned())])
                                .collect(),
                            "agent-control".to_owned(),
                            now,
                        ),
                        rollback_of.as_deref(),
                        requester_session.as_deref(),
                    );
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
                &Request::plan(&Draft::new(
                    id.to_owned(),
                    "release".to_owned(),
                    BTreeMap::from([("chaperone".to_owned(), "1.2.3".to_owned())]),
                    "human".to_owned(),
                    1.0,
                ))
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
        let surface = vectors::surface("manifest.request.ulid").unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let args = vector.input().args().unwrap();
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

            assert_eq!(vector.result(), Outcome::Accepted, "{id}");
            let minted = minted.unwrap_or_else(|| panic!("{id}: Python mints an id and Rust none"));

            assert_eq!(
                &json!({"ulid": minted.as_str()}),
                vector.value().unwrap(),
                "{id}"
            );
        }

        let largest = Timestamp::new(281_474_976_710.655).unwrap();
        let past_48_bits = Timestamp::new(281_474_976_710.656).unwrap();

        assert_eq!(
            mint_ulid(largest, [0xff; 10]).unwrap().as_str(),
            "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
        );
        assert_eq!(mint_ulid(past_48_bits, [0; 10]), Err(MintError));
    }

    #[test]
    fn a_time_outside_the_48_bits_of_a_ulid_mints_no_request_id() {
        // `handover.requester.file.new_ulid` mints 26 characters for each of
        // these two times, with ten zero bytes of entropy. No vector holds
        // one: contract 02 §2 gives such a time no rule, and the requester
        // takes its time from the clock of the host.
        let before_1970 = float_of("bf50624dd2f1a9fc");
        let past_48_bits = float_of("4250624dd2f1a9fc");

        // One millisecond before 1970, and 2^48 milliseconds after it.
        assert_eq!(before_1970, -0.001);
        assert_eq!(past_48_bits, 281_474_976_710.656);
        assert_eq!(Timestamp::new(before_1970), Err(TimestampError));
        assert_eq!(
            mint_ulid(Timestamp::new(past_48_bits).unwrap(), [0; 10]),
            Err(MintError)
        );
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
        let surface = vectors::surface("manifest.state").unwrap();
        let label = text_field(surface.context(), "subject");
        for vector in surface.vectors() {
            let id = vector.id();
            let text = vector.input().text().unwrap();
            match (vector.result(), ReleaseState::parse(text)) {
                (Outcome::Accepted, Ok(state)) => {
                    assert_eq!(&state_json(&state), vector.value().unwrap(), "{id}");
                }
                (Outcome::Accepted, Err(_)) => {
                    panic!("{id}: Python accepts the document and Rust refuses it")
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
    }

    #[test]
    fn a_deep_document_and_a_long_version_are_refused() {
        let long = "1".repeat(4301);
        let deep = format!("{{\"live\": {}{}}}", "[".repeat(30_000), "]".repeat(30_000));
        let version = format!("{{\"provided\": {{\"pep-grant\": \"{long}.0\"}}}}");
        let refused = ReleaseState::parse(&deep).unwrap_err();

        assert_eq!(refused.detail(), "does not parse as JSON");
        assert_eq!(
            refused
                .not_strict()
                .map(|refusal| (refusal.rule(), refusal.at())),
            Some((crate::json::Rule::TooDeep, 72))
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
        let surface = vectors::surface("manifest.resolved").unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let built = resolved_of(vector.input().args().unwrap());
            match (vector.result(), built) {
                (Outcome::Accepted, Some(manifest)) => {
                    let mut value = vector.value().unwrap().clone();
                    let fields = value.as_object_mut().unwrap();
                    let hash = fields.remove("manifest_sha256").unwrap();
                    fields.remove("resolved_at").unwrap();
                    let output = String::from_utf8(output_of(vector)).unwrap();

                    assert_eq!(manifest.canonical_json(), output, "{id}");
                    assert_eq!(json!(manifest.sha256().as_str()), hash, "{id}");
                    assert_eq!(resolved_json(&manifest), value, "{id}");
                }
                (Outcome::Accepted, None) => {
                    panic!("{id}: Python builds a manifest and Rust has no value for it")
                }
                // The Python builder checks the id, `requested_by` and
                // `resolved_at`. In Rust the type of each argument holds its
                // check, so a refused vector has no value to build a manifest
                // from.
                (Outcome::Refused | Outcome::Raised, None) => {}
                (Outcome::Refused | Outcome::Raised, Some(_)) => {
                    panic!("{id}: Python refuses the arguments and Rust builds a manifest")
                }
            }
        }
    }

    #[test]
    fn a_resolved_row_takes_no_version_with_a_digit_that_is_not_ascii() {
        // `handover.resolve.build_document` checks no version and writes a
        // manifest for these arguments. No vector holds them: the resolver
        // makes each row, and a caller gives the builder no row of its own.
        // The version is U+0661, U+0662 and U+0663: the digits 1, 2 and 3.
        let arguments = |version: &str| {
            json!({
                "components": [{
                    "action": "deploy",
                    "from_version": null,
                    "name": "chaperone",
                    "to_version": version,
                }],
                "contracts": [],
                "facts": {},
                "id": "01K5J8M2Q7V3X9R4T6N0B8C2DE",
                "order": [],
                "requested_by": "agent-control",
                "resolved_at_bits": "41da32d2e0000000",
            })
        };
        let not_ascii = "\u{661}.\u{662}.\u{663}";

        assert_eq!(
            not_ascii.parse::<Version>(),
            Err(VersionError::BadByte { at: 0 })
        );
        assert_eq!(resolved_of(object_of(&arguments(not_ascii))), None);
        assert!(resolved_of(object_of(&arguments("1.2.3"))).is_some());
    }

    #[test]
    fn the_gate_and_the_action_are_the_python_ones() {
        let surface = vectors::surface("manifest.gate").unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let args = vector.input().args().unwrap();
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
        let surface = vectors::surface("manifest.summary").unwrap();
        for vector in surface.vectors() {
            let args = vector.input().args().unwrap();
            let field = |key: &str| text_field(args, key).to_owned();
            let summary = Summary::new(SummaryFields::new(
                field("review"),
                field("components"),
                field("contracts"),
                field("restarts"),
                field("restore"),
                field("requested_by"),
                field("manifest"),
            ));
            let value: Map<String, Value> = summary
                .fields()
                .into_iter()
                .map(|(name, text)| (name.to_owned(), json!(text)))
                .collect();

            assert_eq!(
                &Value::Object(value),
                vector.value().unwrap(),
                "{}",
                vector.id()
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

    #[test]
    fn a_requester_error_gives_the_rule_that_the_text_breaks() {
        let fault_of = |text: &str| text.parse::<Requester>().unwrap_err().fault();
        let faults = [
            ("", FamilyNameError::TooShort),
            ("Human", FamilyNameError::BadFirstByte),
            ("hu man", FamilyNameError::BadByte { at: 2 }),
        ];

        for (text, fault) in faults {
            assert_eq!(fault_of(text), fault, "{text:?}");
        }
    }
}
