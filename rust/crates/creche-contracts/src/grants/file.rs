//! The grant file (contract 04 §1): what one family can do.
//!
//! The caregiver writes one file for each family. The chaperone reads the file
//! again for each call. [`GrantFile`] is the one definition for the two sides:
//!
//! - The chaperone calls [`GrantFile::parse`] on the bytes of a file.
//! - The caregiver fills a [`RawGrantFile`], converts it, and writes
//!   [`GrantFile::to_bytes`].
//!
//! The two paths end in one check of each field. [`GrantFile::to_bytes`]
//! refuses to write more bytes than the chaperone reads. So the caregiver
//! cannot write a file that the chaperone refuses.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::ids::{FamilyName, ServerName, Sha256Hex, ToolName};

use super::issue::{Issue, IssueKind, NameError, Report, Step};
use super::json::{self, Charset, Integer, JsonError, KeyOrder, Layout, Map, Sign, Style, Value};

/// The version of the grant file that this module reads and writes (contract
/// 04 §1.2).
pub const GRANT_FILE_VERSION: u64 = 2;

/// The largest count of bytes in a grant file: 256 KiB (contract 04 §1.2).
pub const GRANT_FILE_MAX_BYTES: usize = 256 * 1024;

/// What `limits.pep_rpm` is when the file does not give it.
pub const DEFAULT_PEP_RPM: u64 = 60;

/// What `limits.max_inflight_delegations` is when the file does not give it
/// (contract 04 §1.2 rule 2).
pub const DEFAULT_MAX_INFLIGHT_DELEGATIONS: u64 = 2;

/// What `limits.max_open_gates` is when the file does not give it.
pub const DEFAULT_MAX_OPEN_GATES: u64 = 10;

/// The largest count of token digests: two (contract 04 §2.3).
const TOKEN_DIGESTS_MAX: usize = 2;

const SERVERS_MAX: usize = 64;
const VERBS_MAX: usize = 16;
const DELEGATES_MAX: usize = 32;
const APPROVAL_MAX: usize = 256;
const ALLOW_MAX: usize = 256;
const TARGETS_MAX: usize = 64;
const COMPONENTS_MAX: usize = 64;

/// The largest count of characters in the domain or the service of an
/// `ha_call` triple.
const HA_NAME_MAX: usize = 64;

const VERSION_KEY: &str = "version";
const FAMILY_KEY: &str = "family";
const REV_KEY: &str = "rev";
const TOKEN_KEY: &str = "token_sha256";
const ALIAS_KEY: &str = "model_alias";
const TOOLS_KEY: &str = "tools";
const VERBS_KEY: &str = "verbs";
const DELEGATES_KEY: &str = "delegates";
const APPROVAL_KEY: &str = "approval";
const LIMITS_KEY: &str = "limits";

/// Each key of a grant file.
const FILE_KEYS: &[&str] = &[
    VERSION_KEY,
    FAMILY_KEY,
    REV_KEY,
    TOKEN_KEY,
    ALIAS_KEY,
    TOOLS_KEY,
    VERBS_KEY,
    DELEGATES_KEY,
    APPROVAL_KEY,
    LIMITS_KEY,
];

const ALLOW_KEY: &str = "allow";
const TARGETS_KEY: &str = "targets";
const COMPONENTS_KEY: &str = "components";

/// Each key of the fence of a verb.
const FENCE_KEYS: &[&str] = &[ALLOW_KEY, TARGETS_KEY, COMPONENTS_KEY];

const DOMAIN_KEY: &str = "domain";
const SERVICE_KEY: &str = "service";
const ENTITY_KEY: &str = "entity_id";

/// Each key of one `ha_call` triple.
const TRIPLE_KEYS: &[&str] = &[DOMAIN_KEY, SERVICE_KEY, ENTITY_KEY];

const PEP_RPM_KEY: &str = "pep_rpm";
const INFLIGHT_KEY: &str = "max_inflight_delegations";
const GATES_KEY: &str = "max_open_gates";

/// Each key of the `limits` block.
const LIMIT_KEYS: &[&str] = &[PEP_RPM_KEY, INFLIGHT_KEY, GATES_KEY];

// --- texts with a cap ---

bounded_text! {
    /// The revision of one grant file: 1 to 128 characters of text (contract 04
    /// §1.2).
    ///
    /// The revision is opaque. It changes on each write, and each audit record
    /// names it.
    ///
    /// ```
    /// use creche_contracts::grants::GrantsRev;
    ///
    /// let rev: GrantsRev = "reg-9f21c4".parse()?;
    /// assert_eq!(rev.as_str(), "reg-9f21c4");
    /// # Ok::<(), creche_contracts::grants::GrantsRevError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::grants::GrantsRev;
    ///
    /// let rev = GrantsRev(String::new());
    /// ```
    GrantsRev,
    /// Why a text is not the revision of a grant file.
    GrantsRevError,
    "a revision",
    1,
    128
}

bounded_text! {
    /// The one model alias that the key of a family can name: 1 to 128
    /// characters of text (contract 04 §1.2).
    ///
    /// The chaperone holds the alias only to serve the manifest.
    ///
    /// ```
    /// use creche_contracts::grants::ModelAlias;
    ///
    /// let alias: ModelAlias = "agent-router".parse()?;
    /// assert_eq!(alias.as_str(), "agent-router");
    /// # Ok::<(), creche_contracts::grants::ModelAliasError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::grants::ModelAlias;
    ///
    /// let alias = ModelAlias(String::new());
    /// ```
    ModelAlias,
    /// Why a text is not a model alias.
    ModelAliasError,
    "a model alias",
    1,
    128
}

bounded_text! {
    /// The name of one action that needs an approval: 1 to 256 characters of
    /// text (contract 04 §1.2).
    ///
    /// An action is `<server>__<tool>`, the name of a verb or `invoke_agent`.
    /// The type takes each text, as the Python code does. An entry that names
    /// no granted action gates nothing.
    ///
    /// ```
    /// use creche_contracts::grants::ActionName;
    ///
    /// let action: ActionName = "kagi__kagi_extract".parse()?;
    /// assert_eq!(action.as_str(), "kagi__kagi_extract");
    /// # Ok::<(), creche_contracts::grants::ActionNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::grants::ActionName;
    ///
    /// let action = ActionName(String::new());
    /// ```
    ActionName,
    /// Why a text is not the name of an action.
    ActionNameError,
    "an action name",
    1,
    256
}

/// The domain or the service of one `ha_call` triple: 64 characters of text
/// or less (contract 04 §4.1).
///
/// The type takes each text, the empty text too, as the Python code does. A
/// call matches a triple only when its own domain and service are equal to
/// the text.
///
/// ```
/// use creche_contracts::grants::HaName;
///
/// let domain: HaName = "notify".parse()?;
/// assert_eq!(domain.as_str(), "notify");
/// # Ok::<(), creche_contracts::grants::HaNameError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::grants::HaName;
///
/// let domain = HaName(String::from("notify"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct HaName(String);

impl HaName {
    /// The text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for HaName {
    type Err = HaNameError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.chars().count() > HA_NAME_MAX {
            return Err(HaNameError::TooLong);
        }

        Ok(Self(text.to_owned()))
    }
}

/// Why a text is not the domain or the service of an `ha_call` triple.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HaNameError {
    /// The text has more than 64 characters.
    TooLong,
}

impl fmt::Display for HaNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "a domain or a service has {HA_NAME_MAX} characters or less"
        )
    }
}

impl Error for HaNameError {}

impl From<HaNameError> for IssueKind {
    fn from(error: HaNameError) -> Self {
        match error {
            HaNameError::TooLong => Self::TextTooLong { max: HA_NAME_MAX },
        }
    }
}

// --- the verbs ---

/// The largest count of bytes in the name of a verb.
const VERB_NAME_MAX: usize = 32;

/// The name of one verb in a grant file: `[a-z][a-z0-9_]{0,31}` (contract 04
/// §1.2, contract 01 §3.5).
///
/// The grammar takes a name that is not in the verb catalog. A grant file can
/// come from a newer caregiver. [`VerbName::verb`] tells if this chaperone
/// knows the verb.
///
/// The grammar is ASCII, so one byte is one character. The check reads bytes
/// and uses no pattern engine.
///
/// ```
/// use creche_contracts::grants::{Verb, VerbName};
///
/// let name: VerbName = "ha_call".parse()?;
/// assert_eq!(name.as_str(), "ha_call");
/// assert_eq!(name.verb(), Some(Verb::HaCall));
/// # Ok::<(), creche_contracts::grants::VerbNameError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::grants::VerbName;
///
/// let name = VerbName(String::from("ha_call"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct VerbName(String);

impl VerbName {
    /// The name as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The verb of the catalog that has this name. `None` for a name that this
    /// catalog does not hold.
    #[must_use]
    pub fn verb(&self) -> Option<Verb> {
        Verb::ALL.into_iter().find(|verb| verb.as_str() == self.0)
    }
}

impl FromStr for VerbName {
    type Err = VerbNameError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let bytes = text.as_bytes();
        let Some(first) = bytes.first() else {
            return Err(VerbNameError::TooShort);
        };
        if bytes.len() > VERB_NAME_MAX {
            return Err(VerbNameError::TooLong);
        }

        if !first.is_ascii_lowercase() {
            return Err(VerbNameError::BadFirstByte);
        }

        let in_tail =
            |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_';
        if let Some(at) = bytes.iter().position(|byte| !in_tail(byte)) {
            return Err(VerbNameError::BadByte { at });
        }

        Ok(Self(text.to_owned()))
    }
}

impl fmt::Display for VerbName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not the name of a verb.
///
/// No variant holds the text. The text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VerbNameError {
    /// The text is empty.
    TooShort,
    /// The text has more than 32 bytes.
    TooLong,
    /// The first byte is not `a` to `z`.
    BadFirstByte,
    /// The byte at this offset, from 0, is not `a` to `z`, `0` to `9` or `_`.
    BadByte {
        /// The offset of the byte, from 0.
        at: usize,
    },
}

impl fmt::Display for VerbNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooShort => f.write_str("a verb name has 1 byte or more"),
            Self::TooLong => write!(f, "a verb name has {VERB_NAME_MAX} bytes or less"),
            Self::BadFirstByte => f.write_str("a verb name starts with a to z"),
            Self::BadByte { at } => {
                write!(f, "byte {at} of a verb name is not a to z, 0 to 9 or _")
            }
        }
    }
}

impl Error for VerbNameError {}

/// One verb of the catalog: an action that the chaperone executes itself
/// (contract 04 §4.1, contract 01 §3.5).
///
/// The set is closed for one release of the chaperone. A grant file can name
/// a verb that this set does not hold, because the caregiver releases
/// separately. The reader of the grant file accepts such a name as a
/// [`VerbName`], and the decision denies each call of it. A reader of this
/// enum itself refuses an unknown value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verb {
    /// `embed`: embed one text.
    Embed,
    /// `ha_call`: call one Home Assistant service.
    HaCall,
    /// `enqueue`: start one session in another family.
    Enqueue,
    /// `job_status`: read the outcome of sessions that this family started.
    JobStatus,
    /// `release`: ask for one release of components.
    Release,
}

impl Verb {
    /// Each verb, in the order in which the caregiver writes the verbs of a
    /// grant file.
    pub const ALL: [Self; 5] = [
        Self::Embed,
        Self::HaCall,
        Self::Enqueue,
        Self::JobStatus,
        Self::Release,
    ];

    /// The name of the verb on the wire.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Embed => "embed",
            Self::HaCall => "ha_call",
            Self::Enqueue => "enqueue",
            Self::JobStatus => "job_status",
            Self::Release => "release",
        }
    }
}

impl fmt::Display for Verb {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One triple of `verbs.ha_call.allow` (contract 04 §4.1).
///
/// A call matches the triple when its domain and its service are equal to the
/// triple's, and the two have the same `entity_id` or the two have none.
///
/// ```
/// use creche_contracts::grants::HaAllow;
///
/// let triple = HaAllow::new("light".parse()?, "turn_on".parse()?, None);
/// assert_eq!(triple.domain().as_str(), "light");
/// assert_eq!(triple.entity_id(), None);
/// # Ok::<(), creche_contracts::grants::HaNameError>(())
/// ```
///
/// Code outside this module cannot build a triple from raw strings:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::HaAllow;
///
/// let triple = HaAllow { domain: "light".parse().unwrap(), service: "on".parse().unwrap(), entity_id: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct HaAllow {
    domain: HaName,
    service: HaName,
    entity_id: Option<String>,
}

impl HaAllow {
    /// One triple. `entity_id` is each text, as in the Python code.
    #[must_use]
    pub fn new(domain: HaName, service: HaName, entity_id: Option<String>) -> Self {
        Self {
            domain,
            service,
            entity_id,
        }
    }

    /// The domain of the service.
    #[must_use]
    pub fn domain(&self) -> &HaName {
        &self.domain
    }

    /// The name of the service.
    #[must_use]
    pub fn service(&self) -> &HaName {
        &self.service
    }

    /// The entity that the call must name. `None` means that the call must
    /// name no entity.
    #[must_use]
    pub fn entity_id(&self) -> Option<&str> {
        self.entity_id.as_deref()
    }
}

/// The fence of one granted verb (contract 01 §3.5, contract 04 §4.1).
///
/// One type holds the fence of each verb, as in the Python code. A verb reads
/// one key at most, and a key that it does not read has no effect. A fence
/// with no key is the fence of `embed` and of `job_status`.
///
/// A fence comes from a [`GrantFile`]:
///
/// ```
/// use creche_contracts::grants::{GrantFile, VerbFence, VerbName};
/// use creche_contracts::ids::FamilyName;
///
/// let family: FamilyName = "chat".parse()?;
/// let bytes = br#"{
///     "version": 2,
///     "family": "chat",
///     "rev": "reg-9f21c4",
///     "token_sha256": ["0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"],
///     "model_alias": "fast",
///     "verbs": {"enqueue": {"targets": ["scrum-lead"]}}
/// }"#;
/// let grants = GrantFile::parse(bytes, &family)?;
/// let name: VerbName = "enqueue".parse()?;
/// let fence: &VerbFence = grants.verbs().get(&name).ok_or("the file grants no enqueue")?;
/// let target: FamilyName = "scrum-lead".parse()?;
///
/// assert_eq!(fence.targets(), Some(&[target][..]));
/// assert_eq!(fence.allow(), None);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a fence:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::VerbFence;
///
/// let fence = VerbFence { allow: None, targets: None, components: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerbFence {
    allow: Option<Vec<HaAllow>>,
    targets: Option<Vec<FamilyName>>,
    components: Option<Vec<String>>,
}

impl VerbFence {
    /// The triples that an `ha_call` can match: 256 or less. `None` when the
    /// fence has no `allow` key.
    #[must_use]
    pub fn allow(&self) -> Option<&[HaAllow]> {
        self.allow.as_deref()
    }

    /// The families that `enqueue` can start: 64 or less. `None` when the
    /// fence has no `targets` key.
    #[must_use]
    pub fn targets(&self) -> Option<&[FamilyName]> {
        self.targets.as_deref()
    }

    /// The components that `release` can name: 64 or less. `None` when the
    /// fence has no `components` key. An entry is each text, as in the Python
    /// code.
    #[must_use]
    pub fn components(&self) -> Option<&[String]> {
        self.components.as_deref()
    }
}

// --- the digests and the limits ---

/// The digests of the tokens of one family: one, or two while a rotation runs
/// (contract 04 §2.3).
///
/// The type holds no token. A stolen grant file grants nothing.
///
/// ```
/// use creche_contracts::grants::TokenDigests;
/// use creche_contracts::ids::Sha256Hex;
///
/// let digest: Sha256Hex =
///     "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef".parse()?;
/// let digests = TokenDigests::one(digest.clone());
/// assert_eq!(digests.iter().collect::<Vec<_>>(), [&digest]);
/// # Ok::<(), creche_contracts::ids::Sha256HexError>(())
/// ```
///
/// Code outside this module cannot build a value with no digest:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::TokenDigests;
///
/// let digests = TokenDigests { first: "0".parse().unwrap(), second: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenDigests {
    first: Sha256Hex,
    second: Option<Sha256Hex>,
}

impl TokenDigests {
    /// The digest of the one token of a family.
    #[must_use]
    pub fn one(digest: Sha256Hex) -> Self {
        Self {
            first: digest,
            second: None,
        }
    }

    /// The two digests of a rotation, in the order of the file.
    #[must_use]
    pub fn two(first: Sha256Hex, second: Sha256Hex) -> Self {
        Self {
            first,
            second: Some(second),
        }
    }

    /// Each digest, in the order of the file.
    pub fn iter(&self) -> impl Iterator<Item = &Sha256Hex> {
        std::iter::once(&self.first).chain(self.second.as_ref())
    }
}

/// One limit of a family: an integer of 1 or more (contract 04 §1.2).
///
/// The Python code gives a limit no largest value, so a limit can be past 64
/// bits. Such a limit is never reached.
///
/// ```
/// use creche_contracts::grants::Limit;
///
/// let limit = Limit::new(2)?;
/// assert_eq!(limit.get(), Some(2));
/// assert!(!limit.is_reached_by(1));
/// assert!(limit.is_reached_by(2));
/// assert!(!limit.is_exceeded_by(2));
/// assert!(limit.is_exceeded_by(3));
/// # Ok::<(), creche_contracts::grants::LimitError>(())
/// ```
///
/// Code outside this module cannot build a limit of zero:
///
/// ```compile_fail,E0423
/// use creche_contracts::grants::{Integer, Limit};
///
/// let limit = Limit(Integer::from(0_u64));
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limit(Integer);

impl Limit {
    /// A limit of `value`, which is 1 or more.
    pub fn new(value: u64) -> Result<Self, LimitError> {
        if value == 0 {
            return Err(LimitError::BelowOne);
        }

        Ok(Self(Integer::from(value)))
    }

    /// The limit, when it is 2^64 - 1 or less.
    #[must_use]
    pub fn get(&self) -> Option<u64> {
        self.0.to_u64()
    }

    /// The limit as an integer of each size.
    #[must_use]
    pub fn as_integer(&self) -> &Integer {
        &self.0
    }

    /// Whether `count` is the limit or more.
    #[must_use]
    pub fn is_reached_by(&self, count: u64) -> bool {
        self.get().is_some_and(|limit| count >= limit)
    }

    /// Whether `count` is more than the limit.
    #[must_use]
    pub fn is_exceeded_by(&self, count: u64) -> bool {
        self.get().is_some_and(|limit| count > limit)
    }
}

/// Why a number is not a limit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LimitError {
    /// The number is zero.
    BelowOne,
}

impl fmt::Display for LimitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a limit is 1 or more")
    }
}

impl Error for LimitError {}

/// The three limits of one family (contract 04 §1.2).
///
/// The limits come from a [`GrantFile`]. A limit that the file does not give
/// has its default:
///
/// ```
/// use creche_contracts::grants::{GrantFile, Limit, Limits};
/// use creche_contracts::ids::FamilyName;
///
/// let family: FamilyName = "chat".parse()?;
/// let bytes = br#"{
///     "version": 2,
///     "family": "chat",
///     "rev": "reg-9f21c4",
///     "token_sha256": ["0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"],
///     "model_alias": "fast",
///     "limits": {"max_inflight_delegations": 8}
/// }"#;
/// let grants = GrantFile::parse(bytes, &family)?;
/// let limits: &Limits = grants.limits();
///
/// assert_eq!(limits.max_inflight_delegations(), &Limit::new(8)?);
/// assert_eq!(limits.pep_rpm().get(), Some(60));
/// assert_eq!(limits.max_open_gates().get(), Some(10));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build the limits:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{Limit, Limits};
///
/// let limit = Limit::new(1).unwrap();
/// let limits = Limits { pep_rpm: limit.clone(), max_inflight_delegations: limit.clone(), max_open_gates: limit };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limits {
    pep_rpm: Limit,
    max_inflight_delegations: Limit,
    max_open_gates: Limit,
}

impl Limits {
    /// The calls for each minute, with the denied calls (contract 04 §5 row 2).
    #[must_use]
    pub fn pep_rpm(&self) -> &Limit {
        &self.pep_rpm
    }

    /// The delegate calls that run at one time for this family (contract 04
    /// §5 row 7).
    #[must_use]
    pub fn max_inflight_delegations(&self) -> &Limit {
        &self.max_inflight_delegations
    }

    /// The gates that this family holds open at one time (contract 04 §5 row
    /// 8).
    #[must_use]
    pub fn max_open_gates(&self) -> &Limit {
        &self.max_open_gates
    }
}

// --- the raw form ---

/// The fields of a grant file before the check: the raw form that the
/// caregiver fills.
///
/// Each field is public, and no field is checked. `GrantFile::try_from` makes
/// the valid type.
///
/// The type has no `Deserialize`. [`GrantFile::parse`] is the one reader of
/// the bytes of a grant file, so no code path reads a file with no check of
/// its family.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawGrantFile {
    /// The version of the file format. The conversion takes only
    /// [`GRANT_FILE_VERSION`].
    pub version: u64,
    /// The name of the family.
    pub family: String,
    /// The revision of this write.
    pub rev: String,
    /// One or two digests of tokens, as lower-case hex.
    pub token_sha256: Vec<String>,
    /// The one model alias of the family.
    pub model_alias: String,
    /// The granted tools of each MCP server, with `all` expanded.
    pub tools: BTreeMap<String, Vec<String>>,
    /// The fence of each granted verb.
    pub verbs: BTreeMap<String, RawVerbFence>,
    /// The thin families that this family can ask.
    pub delegates: Vec<String>,
    /// The actions that need an approval, with `<server>__*` expanded.
    pub approval: Vec<String>,
    /// The limits. A limit that is `None` takes its default.
    pub limits: RawLimits,
}

/// The raw fence of one verb. A key that is `None` is not in the file.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RawVerbFence {
    /// The triples of `ha_call`.
    pub allow: Option<Vec<RawHaAllow>>,
    /// The targets of `enqueue`.
    pub targets: Option<Vec<String>>,
    /// The components of `release`.
    pub components: Option<Vec<String>>,
}

/// One raw `ha_call` triple.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawHaAllow {
    /// The domain of the service.
    pub domain: String,
    /// The name of the service.
    pub service: String,
    /// The entity that a call must name, or `None` for no entity.
    pub entity_id: Option<String>,
}

/// The raw limits. A limit that is `None` takes its default.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RawLimits {
    /// The calls for each minute.
    pub pep_rpm: Option<u64>,
    /// The delegate calls at one time.
    pub max_inflight_delegations: Option<u64>,
    /// The open gates at one time.
    pub max_open_gates: Option<u64>,
}

fn texts(items: &[String]) -> Value {
    Value::List(items.iter().cloned().map(Value::Text).collect())
}

fn number(value: u64) -> Value {
    Value::Integer(Integer::from(value))
}

impl RawGrantFile {
    /// The raw fields as the document that the check reads.
    fn document(&self) -> Value {
        let mut tools = Map::new();
        for (server, names) in &self.tools {
            tools.insert(server, texts(names));
        }

        let mut verbs = Map::new();
        for (verb, fence) in &self.verbs {
            verbs.insert(verb, fence.document());
        }

        let mut limits = Map::new();
        let given = [
            (PEP_RPM_KEY, self.limits.pep_rpm),
            (INFLIGHT_KEY, self.limits.max_inflight_delegations),
            (GATES_KEY, self.limits.max_open_gates),
        ];
        for (key, limit) in given {
            if let Some(limit) = limit {
                limits.insert(key, number(limit));
            }
        }

        let mut document = Map::new();
        document.insert(VERSION_KEY, number(self.version));
        document.insert(FAMILY_KEY, Value::Text(self.family.clone()));
        document.insert(REV_KEY, Value::Text(self.rev.clone()));
        document.insert(TOKEN_KEY, texts(&self.token_sha256));
        document.insert(ALIAS_KEY, Value::Text(self.model_alias.clone()));
        document.insert(TOOLS_KEY, Value::Map(tools));
        document.insert(VERBS_KEY, Value::Map(verbs));
        document.insert(DELEGATES_KEY, texts(&self.delegates));
        document.insert(APPROVAL_KEY, texts(&self.approval));
        document.insert(LIMITS_KEY, Value::Map(limits));

        Value::Map(document)
    }
}

impl RawVerbFence {
    fn document(&self) -> Value {
        let mut fence = Map::new();
        if let Some(allow) = &self.allow {
            let triples = allow.iter().map(RawHaAllow::document).collect();
            fence.insert(ALLOW_KEY, Value::List(triples));
        }

        if let Some(targets) = &self.targets {
            fence.insert(TARGETS_KEY, texts(targets));
        }

        if let Some(components) = &self.components {
            fence.insert(COMPONENTS_KEY, texts(components));
        }

        Value::Map(fence)
    }
}

impl RawHaAllow {
    fn document(&self) -> Value {
        let entity = self.entity_id.clone().map_or(Value::Null, Value::Text);
        let mut triple = Map::new();
        triple.insert(DOMAIN_KEY, Value::Text(self.domain.clone()));
        triple.insert(SERVICE_KEY, Value::Text(self.service.clone()));
        triple.insert(ENTITY_KEY, entity);

        Value::Map(triple)
    }
}

// --- the valid form ---

/// One grant file, version 2: what one family can do (contract 04 §1.2).
///
/// A value is valid: each name has its grammar and each list has its cap. Code
/// that holds a value does not check it again.
///
/// The chaperone reads a file with [`GrantFile::parse`]:
///
/// ```
/// use creche_contracts::grants::GrantFile;
/// use creche_contracts::ids::FamilyName;
///
/// let family: FamilyName = "chat".parse()?;
/// let bytes = br#"{
///     "version": 2,
///     "family": "chat",
///     "rev": "reg-9f21c4",
///     "token_sha256": ["0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"],
///     "model_alias": "fast"
/// }"#;
/// let grants = GrantFile::parse(bytes, &family)?;
///
/// assert_eq!(grants.family(), &family);
/// assert_eq!(grants.limits().pep_rpm().get(), Some(60));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail
/// use creche_contracts::grants::GrantFile;
/// use creche_contracts::ids::FamilyName;
///
/// let family: FamilyName = "chat".parse().unwrap();
/// let grants = GrantFile { family };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrantFile {
    family: FamilyName,
    rev: GrantsRev,
    token_sha256: TokenDigests,
    model_alias: ModelAlias,
    tools: BTreeMap<ServerName, Vec<ToolName>>,
    verbs: BTreeMap<VerbName, VerbFence>,
    delegates: Vec<FamilyName>,
    approval: Vec<ActionName>,
    limits: Limits,
}

impl GrantFile {
    /// Reads the bytes of the grant file of `family`. `family` is the stem of
    /// the file name.
    ///
    /// The function does what `parse_grants` of the Python chaperone does. It
    /// reads JSON as the Python reader reads it, and it takes a limit in each
    /// form that the Python code takes.
    ///
    /// This is a process edge. When the parse fails, the chaperone denies each
    /// call of the family with `unknown_token` and raises the fault
    /// `grants_stale` with the text of the error (contract 04 §1.4, §1.6). It
    /// does not exit, and it keeps no last good value.
    pub fn parse(bytes: &[u8], family: &FamilyName) -> Result<Self, GrantFileError> {
        Self::read(bytes, family).map_err(|error| GrantFileError {
            family: family.clone(),
            error,
        })
    }

    fn read(bytes: &[u8], family: &FamilyName) -> Result<Self, GrantError> {
        if bytes.len() > GRANT_FILE_MAX_BYTES {
            return Err(GrantError::TooLarge { bytes: bytes.len() });
        }

        let document = json::read_utf8(bytes).map_err(GrantError::NotJson)?;
        let grants = Self::from_document(&document)?;

        // Contract 04 §1.2: `family` is equal to the stem of the file name.
        if grants.family != *family {
            return Err(GrantError::OtherFamily(grants.family));
        }

        Ok(grants)
    }

    /// The one check of a grant file. Each path to a value goes through it.
    fn from_document(document: &Value) -> Result<Self, GrantError> {
        let Value::Map(document) = document else {
            return Err(GrantError::NotObject);
        };

        // The version tells if this reader knows the other fields, so the
        // reader takes it first and reports it alone (contract 04 §1.4).
        let Some(Value::Integer(version)) = document.get(VERSION_KEY) else {
            return Err(GrantError::VersionNotInteger);
        };
        if version.to_u64() != Some(GRANT_FILE_VERSION) {
            return Err(GrantError::UnknownVersion(version.clone()));
        }

        let mut report = Report::new();
        let grants = check_file(document, &mut report);
        let issues = report.finish();
        match grants {
            Some(grants) if issues.is_empty() => Ok(grants),
            _ => Err(GrantError::Invalid(issues)),
        }
    }

    /// The family that the file is for.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The revision of the write.
    #[must_use]
    pub fn rev(&self) -> &GrantsRev {
        &self.rev
    }

    /// The digests of the tokens that name this family.
    #[must_use]
    pub fn token_sha256(&self) -> &TokenDigests {
        &self.token_sha256
    }

    /// The one model alias of the family.
    #[must_use]
    pub fn model_alias(&self) -> &ModelAlias {
        &self.model_alias
    }

    /// The granted tools of each MCP server: 64 servers or less. A list keeps
    /// the order of the file.
    #[must_use]
    pub fn tools(&self) -> &BTreeMap<ServerName, Vec<ToolName>> {
        &self.tools
    }

    /// The fence of each granted verb: 16 verbs or less.
    #[must_use]
    pub fn verbs(&self) -> &BTreeMap<VerbName, VerbFence> {
        &self.verbs
    }

    /// The thin families that this family can ask: 32 or less.
    #[must_use]
    pub fn delegates(&self) -> &[FamilyName] {
        &self.delegates
    }

    /// The actions that need an approval: 256 or less.
    #[must_use]
    pub fn approval(&self) -> &[ActionName] {
        &self.approval
    }

    /// Whether `action` needs an approval. The function compares the whole
    /// name: the caregiver expanded each `<server>__*`.
    #[must_use]
    pub fn needs_approval(&self, action: &str) -> bool {
        self.approval.iter().any(|entry| entry.as_str() == action)
    }

    /// The limits of the family.
    #[must_use]
    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    /// The name of the file in the grants directory: `<family>.json`.
    #[must_use]
    pub fn file_name(&self) -> String {
        format!("{}.json", self.family)
    }

    /// The same grants with another revision and other digests (contract 04
    /// §2.3). A rotation moves the tokens that the chaperone accepts, and no
    /// grant.
    #[must_use]
    pub fn rotated(&self, rev: GrantsRev, token_sha256: TokenDigests) -> Self {
        Self {
            rev,
            token_sha256,
            ..self.clone()
        }
    }

    /// Whether `other` holds the same grants. The revision and the digests do
    /// not count: the first changes on each write, and a rotation owns the
    /// second.
    #[must_use]
    pub fn same_grants(&self, other: &Self) -> bool {
        self.family == other.family
            && self.model_alias == other.model_alias
            && self.tools == other.tools
            && self.verbs == other.verbs
            && self.delegates == other.delegates
            && self.approval == other.approval
            && self.limits == other.limits
    }

    /// The bytes of the file, as the caregiver writes them: JSON with an
    /// indent of 2, text outside ASCII as `\u` escapes, and one final newline.
    ///
    /// The servers are in the order of their names. The verbs of the catalog
    /// are in the order of [`Verb::ALL`], and each other verb follows in the
    /// order of its name.
    ///
    /// A valid value can be longer than [`GRANT_FILE_MAX_BYTES`] as a file,
    /// and the chaperone refuses such a file (contract 04 §1.2). The list of
    /// tools of a server has no cap on its count. The writer adds an indent,
    /// and it writes one character outside ASCII as 6 or 12 bytes. So a file
    /// that the reader took can be too long when the writer writes it again.
    ///
    /// This is a process edge. When the function refuses, the caregiver
    /// writes no file: it never writes a grant file that it knows to be wrong
    /// (contract 04 §1.3 rule 1).
    pub fn to_bytes(&self) -> Result<Vec<u8>, GrantWriteError> {
        let style = Style {
            layout: Layout::Indented,
            charset: Charset::Ascii,
            keys: KeyOrder::Kept,
        };
        let mut text = String::new();
        json::write(&self.document(), style, &mut text);
        text.push('\n');
        if text.len() > GRANT_FILE_MAX_BYTES {
            return Err(GrantWriteError::TooLarge { bytes: text.len() });
        }

        Ok(text.into_bytes())
    }

    fn document(&self) -> Value {
        let names = |items: &[FamilyName]| -> Value {
            let texts = items
                .iter()
                .map(|name| Value::Text(name.as_str().to_owned()));

            Value::List(texts.collect())
        };
        let digests = self
            .token_sha256
            .iter()
            .map(|digest| Value::Text(digest.as_str().to_owned()));
        let mut tools = Map::new();
        for (server, granted) in &self.tools {
            let granted = granted
                .iter()
                .map(|tool| Value::Text(tool.as_str().to_owned()));
            tools.insert(server.as_str(), Value::List(granted.collect()));
        }

        let in_catalog = Verb::ALL.iter().filter_map(|verb| {
            self.verbs
                .iter()
                .find(|(name, _)| name.verb() == Some(*verb))
        });
        let others = self.verbs.iter().filter(|(name, _)| name.verb().is_none());
        let mut verbs = Map::new();
        for (name, fence) in in_catalog.chain(others) {
            verbs.insert(name.as_str(), fence.document(&names));
        }

        let approval = self
            .approval
            .iter()
            .map(|action| Value::Text(action.as_str().to_owned()));
        let limit = |limit: &Limit| Value::Integer(limit.as_integer().clone());
        let mut limits = Map::new();
        limits.insert(PEP_RPM_KEY, limit(&self.limits.pep_rpm));
        limits.insert(INFLIGHT_KEY, limit(&self.limits.max_inflight_delegations));
        limits.insert(GATES_KEY, limit(&self.limits.max_open_gates));

        let mut document = Map::new();
        document.insert(VERSION_KEY, number(GRANT_FILE_VERSION));
        document.insert(FAMILY_KEY, Value::Text(self.family.as_str().to_owned()));
        document.insert(REV_KEY, Value::Text(self.rev.as_str().to_owned()));
        document.insert(TOKEN_KEY, Value::List(digests.collect()));
        document.insert(ALIAS_KEY, Value::Text(self.model_alias.as_str().to_owned()));
        document.insert(TOOLS_KEY, Value::Map(tools));
        document.insert(VERBS_KEY, Value::Map(verbs));
        document.insert(DELEGATES_KEY, names(&self.delegates));
        document.insert(APPROVAL_KEY, Value::List(approval.collect()));
        document.insert(LIMITS_KEY, Value::Map(limits));

        Value::Map(document)
    }
}

impl VerbFence {
    fn document(&self, names: &impl Fn(&[FamilyName]) -> Value) -> Value {
        let mut fence = Map::new();
        if let Some(allow) = &self.allow {
            let triples = allow.iter().map(|triple| {
                let entity = triple.entity_id.clone().map_or(Value::Null, Value::Text);
                let mut entry = Map::new();
                entry.insert(DOMAIN_KEY, Value::Text(triple.domain.as_str().to_owned()));
                entry.insert(SERVICE_KEY, Value::Text(triple.service.as_str().to_owned()));
                entry.insert(ENTITY_KEY, entity);

                Value::Map(entry)
            });
            fence.insert(ALLOW_KEY, Value::List(triples.collect()));
        }

        if let Some(targets) = &self.targets {
            fence.insert(TARGETS_KEY, names(targets));
        }

        if let Some(components) = &self.components {
            fence.insert(COMPONENTS_KEY, texts(components));
        }

        Value::Map(fence)
    }
}

impl TryFrom<RawGrantFile> for GrantFile {
    type Error = GrantError;

    /// Checks the raw fields. The conversion runs the check that
    /// [`GrantFile::parse`] runs, and it collects each issue.
    fn try_from(raw: RawGrantFile) -> Result<Self, Self::Error> {
        Self::from_document(&raw.document())
    }
}

// --- the errors ---

/// Why a document is not a grant file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GrantError {
    /// The file has more bytes than [`GRANT_FILE_MAX_BYTES`].
    TooLarge {
        /// The count of bytes.
        bytes: usize,
    },
    /// The bytes are not JSON as the Python reader takes it.
    NotJson(JsonError),
    /// The top level is not an object.
    NotObject,
    /// `version` is missing or is not an integer.
    VersionNotInteger,
    /// `version` is an integer that this reader does not know.
    UnknownVersion(Integer),
    /// One field or more is not valid. The list holds each issue.
    Invalid(Vec<Issue>),
    /// The file names a family that is not the stem of its file name.
    OtherFamily(FamilyName),
}

impl fmt::Display for GrantError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge { bytes } => write!(f, "{bytes} bytes exceeds the cap"),
            Self::NotJson(error) => write!(f, "not JSON ({error})"),
            Self::NotObject => f.write_str("the top level is not an object"),
            Self::VersionNotInteger => f.write_str("version is not an integer"),
            Self::UnknownVersion(version) => write!(f, "unknown version {version}"),
            // The count and no issue: an issue can hold text of the file.
            Self::Invalid(issues) => write!(f, "invalid ({} errors)", issues.len()),
            Self::OtherFamily(family) => write!(f, "names family '{family}'"),
        }
    }
}

impl Error for GrantError {}

/// Why the caregiver does not write a grant file.
///
/// The set is closed. The caregiver makes each value, and no process reads
/// one from a wire.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrantWriteError {
    /// The file has more bytes than [`GRANT_FILE_MAX_BYTES`]. The chaperone
    /// refuses such a file.
    TooLarge {
        /// The count of bytes.
        bytes: usize,
    },
}

impl fmt::Display for GrantWriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge { bytes } => write!(f, "{bytes} bytes exceeds the cap"),
        }
    }
}

impl Error for GrantWriteError {}

/// Why the chaperone does not serve the grant file of one family.
///
/// The text of the error is the message of the fault `grants_stale` (contract
/// 04 §1.6). It names the file and the rule. It holds no digest. It holds two
/// texts of the file at most: an integer that is not the version, and a family
/// name that is not the name of the file. Each one has a safe grammar.
///
/// ```
/// use creche_contracts::grants::{GrantError, GrantFile, GrantFileError};
/// use creche_contracts::ids::FamilyName;
///
/// let family: FamilyName = "chat".parse()?;
/// let error: GrantFileError = GrantFile::parse(br#"{"version": 3}"#, &family).unwrap_err();
///
/// assert_eq!(error.to_string(), "grants/chat.json: unknown version 3");
/// assert!(matches!(error.error(), GrantError::UnknownVersion(_)));
/// # Ok::<(), creche_contracts::ids::FamilyNameError>(())
/// ```
///
/// Code outside this module cannot build an error for a file that it did not
/// read:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{GrantError, GrantFileError};
///
/// let error = GrantFileError { family: "chat".parse().unwrap(), error: GrantError::NotObject };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrantFileError {
    family: FamilyName,
    error: GrantError,
}

impl GrantFileError {
    /// The family of the file: the stem of its name.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// Why the file is not a grant file.
    #[must_use]
    pub fn error(&self) -> &GrantError {
        &self.error
    }
}

impl fmt::Display for GrantFileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "grants/{}.json: {}", self.family, self.error)
    }
}

impl Error for GrantFileError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.error)
    }
}

// --- the check ---

/// The value of a key that the object must have.
fn required<T>(
    object: &Map,
    report: &mut Report,
    key: &str,
    read: impl FnOnce(&mut Report, &Value) -> Option<T>,
) -> Option<T> {
    report.at_key(key, |report| match object.get(key) {
        Some(value) => read(report, value),
        None => {
            report.issue(IssueKind::Missing);

            None
        }
    })
}

/// The value of a key with a default.
fn defaulted<T>(
    object: &Map,
    report: &mut Report,
    key: &str,
    read: impl FnOnce(&mut Report, &Value) -> Option<T>,
    default: impl FnOnce() -> T,
) -> Option<T> {
    match object.get(key) {
        Some(value) => report.at_key(key, |report| read(report, value)),
        None => Some(default()),
    }
}

/// Records each key of `object` that is not in `known`, in the order of the
/// object. The Python code reports them after the known keys.
pub(super) fn unknown_keys(object: &Map, report: &mut Report, known: &[&str]) {
    for (key, _) in object.iter() {
        if !known.contains(&key) {
            report.at_key(key, |report| report.issue(IssueKind::UnknownKey));
        }
    }
}

pub(super) fn text<'a>(report: &mut Report, value: &'a Value) -> Option<&'a str> {
    let text = value.as_str();
    if text.is_none() {
        report.issue(IssueKind::NotText);
    }

    text
}

/// A text with a cap on its characters.
fn bounded<T>(report: &mut Report, value: &Value) -> Option<T>
where
    T: FromStr,
    T::Err: Into<IssueKind>,
{
    match text(report, value)?.parse() {
        Ok(made) => Some(made),
        Err(error) => {
            report.issue(error.into());

            None
        }
    }
}

/// A text with the grammar of a name.
fn name<T: FromStr>(
    report: &mut Report,
    value: &Value,
    wrap: fn(T::Err) -> NameError,
) -> Option<T> {
    match text(report, value)?.parse() {
        Ok(made) => Some(made),
        Err(error) => {
            report.issue(IssueKind::Name(wrap(error)));

            None
        }
    }
}

/// An array, with a cap on its valid items.
///
/// The function counts as the Python code counts. An item that is not valid
/// is an issue and does not count. When the valid items pass `max`, the one
/// issue is the count, and the function drops each issue of an item.
fn list_of<T>(
    report: &mut Report,
    value: &Value,
    min: usize,
    max: Option<usize>,
    mut item: impl FnMut(&mut Report, &Value) -> Option<T>,
) -> Option<Vec<T>> {
    let Value::List(items) = value else {
        report.issue(IssueKind::NotList);

        return None;
    };
    let mark = report.mark();
    let mut valid = Vec::new();
    for (index, raw) in items.iter().enumerate() {
        let Some(made) = report.at(Step::Index(index), |report| item(report, raw)) else {
            continue;
        };
        valid.push(made);
        if let Some(max) = max.filter(|max| valid.len() > *max) {
            report.forget_after(mark);
            report.issue(IssueKind::TooMany { max });

            return None;
        }
    }

    if valid.len() < min {
        report.issue(IssueKind::TooFew { min });
    }

    if report.has_after(mark) {
        return None;
    }

    Some(valid)
}

/// An object with names as its keys, with a cap on its entries.
///
/// The function counts as the Python code counts. It reports the count only
/// when no key and no value has an issue.
fn map_of<K: Ord + FromStr, V>(
    report: &mut Report,
    value: &Value,
    max: usize,
    wrap: fn(K::Err) -> NameError,
    mut read: impl FnMut(&mut Report, &Value) -> Option<V>,
) -> Option<BTreeMap<K, V>> {
    let Value::Map(entries) = value else {
        report.issue(IssueKind::NotMap);

        return None;
    };
    let mark = report.mark();
    let mut valid = BTreeMap::new();
    for (key, raw) in entries.iter() {
        let entry = report.at_key(key, |report| {
            let name = match key.parse::<K>() {
                Ok(name) => Some(name),
                Err(error) => {
                    let kind = IssueKind::Name(wrap(error));
                    report.at(Step::KeyItself, |report| report.issue(kind));

                    None
                }
            };
            let made = read(report, raw);

            name.zip(made)
        });
        if let Some((name, made)) = entry {
            valid.insert(name, made);
        }
    }

    if report.has_after(mark) {
        return None;
    }

    if valid.len() > max {
        report.issue(IssueKind::TooMany { max });

        return None;
    }

    Some(valid)
}

fn check_file(document: &Map, report: &mut Report) -> Option<GrantFile> {
    let family = required(document, report, FAMILY_KEY, |report, value| {
        name::<FamilyName>(report, value, NameError::Family)
    });
    let rev = required(document, report, REV_KEY, bounded::<GrantsRev>);
    let token_sha256 = required(document, report, TOKEN_KEY, check_digests);
    let model_alias = required(document, report, ALIAS_KEY, bounded::<ModelAlias>);
    let tools = defaulted(document, report, TOOLS_KEY, check_tools, BTreeMap::new);
    let verbs = defaulted(document, report, VERBS_KEY, check_verbs, BTreeMap::new);
    let delegates = defaulted(
        document,
        report,
        DELEGATES_KEY,
        |report, value| {
            list_of(report, value, 0, Some(DELEGATES_MAX), |report, value| {
                name::<FamilyName>(report, value, NameError::Family)
            })
        },
        Vec::new,
    );
    let approval = defaulted(
        document,
        report,
        APPROVAL_KEY,
        |report, value| list_of(report, value, 0, Some(APPROVAL_MAX), bounded::<ActionName>),
        Vec::new,
    );
    let limits = match document.get(LIMITS_KEY) {
        Some(value) => report.at_key(LIMITS_KEY, |report| check_limits(report, value)),
        None => check_limits(report, &Value::Map(Map::new())),
    };
    unknown_keys(document, report, FILE_KEYS);

    Some(GrantFile {
        family: family?,
        rev: rev?,
        token_sha256: token_sha256?,
        model_alias: model_alias?,
        tools: tools?,
        verbs: verbs?,
        delegates: delegates?,
        approval: approval?,
        limits: limits?,
    })
}

fn check_digests(report: &mut Report, value: &Value) -> Option<TokenDigests> {
    let digests = list_of(
        report,
        value,
        1,
        Some(TOKEN_DIGESTS_MAX),
        |report, value| name::<Sha256Hex>(report, value, NameError::Digest),
    )?;
    let mut digests = digests.into_iter();
    let first = digests.next()?;

    Some(TokenDigests {
        first,
        second: digests.next(),
    })
}

fn check_tools(report: &mut Report, value: &Value) -> Option<BTreeMap<ServerName, Vec<ToolName>>> {
    map_of(
        report,
        value,
        SERVERS_MAX,
        NameError::Server,
        |report, value| {
            list_of(report, value, 0, None, |report, value| {
                name::<ToolName>(report, value, NameError::Tool)
            })
        },
    )
}

fn check_verbs(report: &mut Report, value: &Value) -> Option<BTreeMap<VerbName, VerbFence>> {
    map_of(report, value, VERBS_MAX, NameError::Verb, check_fence)
}

/// An array that can be `null` or not there.
fn nullable_list<T>(
    object: &Map,
    report: &mut Report,
    key: &str,
    max: usize,
    item: impl FnMut(&mut Report, &Value) -> Option<T>,
) -> Option<Option<Vec<T>>> {
    match object.get(key) {
        None | Some(Value::Null) => Some(None),
        Some(value) => report
            .at_key(key, |report| list_of(report, value, 0, Some(max), item))
            .map(Some),
    }
}

fn check_fence(report: &mut Report, value: &Value) -> Option<VerbFence> {
    let Value::Map(fence) = value else {
        report.issue(IssueKind::NotObject);

        return None;
    };
    let allow = nullable_list(fence, report, ALLOW_KEY, ALLOW_MAX, check_triple);
    let targets = nullable_list(fence, report, TARGETS_KEY, TARGETS_MAX, |report, value| {
        name::<FamilyName>(report, value, NameError::Family)
    });
    let components = nullable_list(
        fence,
        report,
        COMPONENTS_KEY,
        COMPONENTS_MAX,
        |report, value| text(report, value).map(str::to_owned),
    );
    unknown_keys(fence, report, FENCE_KEYS);

    Some(VerbFence {
        allow: allow?,
        targets: targets?,
        components: components?,
    })
}

fn check_triple(report: &mut Report, value: &Value) -> Option<HaAllow> {
    let Value::Map(triple) = value else {
        report.issue(IssueKind::NotObject);

        return None;
    };
    let domain = required(triple, report, DOMAIN_KEY, bounded::<HaName>);
    let service = required(triple, report, SERVICE_KEY, bounded::<HaName>);
    let entity_id = match triple.get(ENTITY_KEY) {
        None | Some(Value::Null) => Some(None),
        Some(value) => report
            .at_key(ENTITY_KEY, |report| text(report, value))
            .map(|entity| Some(entity.to_owned())),
    };
    unknown_keys(triple, report, TRIPLE_KEYS);

    Some(HaAllow {
        domain: domain?,
        service: service?,
        entity_id: entity_id?,
    })
}

fn check_limits(report: &mut Report, value: &Value) -> Option<Limits> {
    let Value::Map(limits) = value else {
        report.issue(IssueKind::NotObject);

        return None;
    };
    let mut limit = |key: &str, default: u64| {
        defaulted(limits, report, key, check_limit, || {
            Limit(Integer::from(default))
        })
    };
    let pep_rpm = limit(PEP_RPM_KEY, DEFAULT_PEP_RPM);
    let max_inflight_delegations = limit(INFLIGHT_KEY, DEFAULT_MAX_INFLIGHT_DELEGATIONS);
    let max_open_gates = limit(GATES_KEY, DEFAULT_MAX_OPEN_GATES);
    unknown_keys(limits, report, LIMIT_KEYS);

    Some(Limits {
        pep_rpm: pep_rpm?,
        max_inflight_delegations: max_inflight_delegations?,
        max_open_gates: max_open_gates?,
    })
}

/// One limit. The Python code reads an integer from more than a JSON integer:
/// from `true`, from a float with no fraction and from a text.
fn check_limit(report: &mut Report, value: &Value) -> Option<Limit> {
    let read = match value {
        Value::Bool(flag) => Ok(Integer::from(u64::from(*flag))),
        Value::Integer(number) => Ok(number.clone()),
        Value::Float(number) => float_integer(*number),
        Value::Text(text) => text_integer(text),
        Value::Null | Value::List(_) | Value::Map(_) => Err(IssueKind::NotInteger),
    };
    let number = match read {
        Ok(number) => number,
        Err(kind) => {
            report.issue(kind);

            return None;
        }
    };
    if !number.is_positive() {
        report.issue(IssueKind::BelowMinimum { min: 1 });

        return None;
    }

    Some(Limit(number))
}

/// 2^63. The Python code reads a float as an integer only when the float is
/// between -2^63 and 2^63, and is not one of the two.
const FLOAT_INTEGER_BOUND: f64 = 9_223_372_036_854_775_808.0;

/// The integer of a float that has no fraction.
fn float_integer(number: f64) -> Result<Integer, IssueKind> {
    if !number.is_finite() {
        return Err(IssueKind::NotFinite);
    }

    if number.fract() != 0.0 {
        return Err(IssueKind::Fraction);
    }

    if number <= -FLOAT_INTEGER_BOUND || number >= FLOAT_INTEGER_BOUND {
        return Err(IssueKind::IntegerTooLarge);
    }

    // The float has no fraction, so its decimal text is its integer.
    let digits = format!("{:.0}", number.abs());

    let sign = if number < 0.0 {
        Sign::Minus
    } else {
        Sign::Plus
    };

    Ok(Integer::from_digits(sign, &digits))
}

/// The integer of a text, in each form that the Python code reads:
///
/// - white space at each end
/// - one `+` or one `-` at the start
/// - one `_` between two digits
/// - a zero at the start
/// - a `.` and one zero or more at the end
/// - a `-` after the zeros at the start, which is then the sign
fn text_integer(text: &str) -> Result<Integer, IssueKind> {
    let trimmed = text.trim();
    let (sign, unsigned) = match trimmed.strip_prefix('-') {
        Some(rest) => (Sign::Minus, rest),
        None => (Sign::Plus, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    let whole = match unsigned.split_once('.') {
        Some((whole, zeros)) if !zeros.is_empty() && zeros.bytes().all(|byte| byte == b'0') => {
            whole
        }
        Some(_) => return Err(IssueKind::NotIntegerText),
        None => unsigned,
    };
    if !whole.starts_with(|first: char| first.is_ascii_digit()) {
        return Err(IssueKind::NotIntegerText);
    }

    // The Python code skips each `0` and each `_` at the start. What stays
    // has one `_` between two digits at most.
    let significant = whole.trim_start_matches(['0', '_']);
    if let (Sign::Plus, Some(tail)) = (sign, significant.strip_prefix('-')) {
        return negative_tail(tail);
    }

    let grouped = !significant.ends_with('_')
        && !significant.contains("__")
        && significant
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'_');
    if !grouped || (significant.is_empty() && whole.ends_with('_')) {
        return Err(IssueKind::NotIntegerText);
    }

    let digits: String = significant.chars().filter(char::is_ascii_digit).collect();
    // The cap counts the `-` of a negative number as one digit.
    if digits.len() + usize::from(sign == Sign::Minus) > json::INT_MAX_DIGITS {
        // The Python code names the size only for digits with no other form
        // before them or between them: no white space, no `+`, no zero and
        // no `_`.
        let plain = text.trim_end().len() == trimmed.len()
            && !trimmed.starts_with('+')
            && significant.len() == whole.len()
            && !significant.contains('_');

        return Err(if plain {
            IssueKind::IntegerTooLarge
        } else {
            IssueKind::NotIntegerText
        });
    }

    Ok(Integer::from_digits(sign, &format!("0{digits}")))
}

/// The integer of a text that has a `-` after the zeros at its start, for
/// example `0-8`. `tail` is what follows the `-`.
///
/// The Python code reads the `-` as the sign of the tail. The tail has a
/// stricter form than a text with its `-` at the start:
///
/// - one `_` can come first
/// - a zero at the start is the whole number
/// - a tail with too many digits is not an integer text, and not an integer
///   that is too large
fn negative_tail(tail: &str) -> Result<Integer, IssueKind> {
    let number = tail.strip_prefix('_').unwrap_or(tail);
    let grouped = number.starts_with(|first: char| first.is_ascii_digit())
        && !number.ends_with('_')
        && !number.contains("__")
        && number
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'_');
    let digits: String = number.chars().filter(char::is_ascii_digit).collect();
    let zero_first = digits.len() > 1 && digits.starts_with('0');

    // The cap counts the `-` as one digit.
    if !grouped || zero_first || digits.len() >= json::INT_MAX_DIGITS {
        return Err(IssueKind::NotIntegerText);
    }

    Ok(Integer::from_digits(Sign::Minus, &digits))
}

#[cfg(test)]
mod tests {
    use super::*;

    const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    const OTHER_DIGEST: &str = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";

    fn family(name: &str) -> FamilyName {
        name.parse().unwrap()
    }

    fn raw() -> RawGrantFile {
        RawGrantFile {
            version: GRANT_FILE_VERSION,
            family: "chat".to_owned(),
            rev: "reg-9f21c4".to_owned(),
            token_sha256: vec![DIGEST.to_owned()],
            model_alias: "fast".to_owned(),
            tools: BTreeMap::new(),
            verbs: BTreeMap::new(),
            delegates: Vec::new(),
            approval: Vec::new(),
            limits: RawLimits::default(),
        }
    }

    fn issues_of(raw: RawGrantFile) -> Vec<String> {
        match GrantFile::try_from(raw) {
            Err(GrantError::Invalid(issues)) => issues.iter().map(Issue::to_string).collect(),
            other => panic!("not an invalid file: {other:?}"),
        }
    }

    #[test]
    fn a_text_with_a_cap_counts_characters() {
        // LATIN SMALL LETTER E WITH ACUTE has two bytes, and GRINNING FACE has
        // four bytes and two UTF-16 code units.
        for one in ["r", "\u{e9}", "\u{1f600}"] {
            assert!(one.repeat(128).parse::<GrantsRev>().is_ok(), "{one}");
            assert_eq!(
                one.repeat(129).parse::<GrantsRev>(),
                Err(GrantsRevError::TooLong),
                "{one}"
            );
            assert!(one.repeat(128).parse::<ModelAlias>().is_ok());
            assert_eq!(
                one.repeat(129).parse::<ModelAlias>(),
                Err(ModelAliasError::TooLong)
            );
            assert!(one.repeat(256).parse::<ActionName>().is_ok());
            assert_eq!(
                one.repeat(257).parse::<ActionName>(),
                Err(ActionNameError::TooLong)
            );
            assert!(one.repeat(64).parse::<HaName>().is_ok());
            assert_eq!(one.repeat(65).parse::<HaName>(), Err(HaNameError::TooLong));
        }

        assert_eq!("".parse::<GrantsRev>(), Err(GrantsRevError::TooShort));
        assert_eq!("".parse::<ModelAlias>(), Err(ModelAliasError::TooShort));
        assert_eq!("".parse::<ActionName>(), Err(ActionNameError::TooShort));
        assert_eq!("".parse::<HaName>().unwrap().as_str(), "");
    }

    #[test]
    fn a_text_with_a_cap_takes_each_character() {
        for text in ["a\nb", "a\0b", " ", "Not An Alias *", "kagi__*"] {
            assert_eq!(text.parse::<GrantsRev>().unwrap().as_str(), text);
            assert_eq!(text.parse::<ModelAlias>().unwrap().as_str(), text);
            assert_eq!(text.parse::<ActionName>().unwrap().as_str(), text);
            assert_eq!(GrantsRev::try_from(text.to_owned()).unwrap().as_str(), text);
        }
    }

    #[test]
    fn the_error_of_a_text_with_a_cap_says_which_rule_failed() {
        assert_eq!(
            GrantsRevError::TooShort.to_string(),
            "a revision has 1 character or more"
        );
        assert_eq!(
            GrantsRevError::TooLong.to_string(),
            "a revision has 128 characters or less"
        );
        assert_eq!(
            ActionNameError::TooLong.to_string(),
            "an action name has 256 characters or less"
        );
        assert_eq!(
            HaNameError::TooLong.to_string(),
            "a domain or a service has 64 characters or less"
        );
        assert_eq!(
            IssueKind::from(ModelAliasError::TooShort),
            IssueKind::TextTooShort { min: 1 }
        );
        assert_eq!(
            IssueKind::from(HaNameError::TooLong),
            IssueKind::TextTooLong { max: 64 }
        );

        let error: &dyn Error = &ModelAliasError::TooLong;

        assert!(error.source().is_none());
    }

    #[test]
    fn a_verb_name_is_lower_case_with_underscores() {
        let longest = "v".repeat(32);
        let too_long = "v".repeat(33);
        let accepted = ["a", "embed", "ha_call", "job_status", "v_", "a0", &longest];
        let refused = [
            ("", VerbNameError::TooShort),
            (too_long.as_str(), VerbNameError::TooLong),
            ("Embed", VerbNameError::BadFirstByte),
            ("_embed", VerbNameError::BadFirstByte),
            ("0embed", VerbNameError::BadFirstByte),
            ("ha-call", VerbNameError::BadByte { at: 2 }),
            ("haCall", VerbNameError::BadByte { at: 2 }),
            ("embed\n", VerbNameError::BadByte { at: 5 }),
            ("embed ", VerbNameError::BadByte { at: 5 }),
            // ARABIC-INDIC DIGIT ONE: a digit, and not ASCII.
            ("v\u{661}", VerbNameError::BadByte { at: 1 }),
        ];

        for text in accepted {
            assert_eq!(text.parse::<VerbName>().unwrap().as_str(), text);
        }

        for (text, error) in refused {
            assert_eq!(text.parse::<VerbName>(), Err(error), "{text:?}");
        }
    }

    #[test]
    fn a_verb_name_tells_if_the_catalog_holds_it() {
        for verb in Verb::ALL {
            let name: VerbName = verb.as_str().parse().unwrap();

            assert_eq!(name.verb(), Some(verb));
            assert_eq!(name.to_string(), verb.to_string());
        }

        assert_eq!("teleport".parse::<VerbName>().unwrap().verb(), None);
        assert_eq!("invoke_agent".parse::<VerbName>().unwrap().verb(), None);
    }

    #[test]
    fn a_verb_reads_and_writes_its_wire_name() {
        for verb in Verb::ALL {
            let wire = serde_json::to_string(&verb).unwrap();

            assert_eq!(wire, format!("\"{}\"", verb.as_str()));
            assert_eq!(serde_json::from_str::<Verb>(&wire).unwrap(), verb);
        }

        assert!(serde_json::from_str::<Verb>("\"teleport\"").is_err());
        assert!(serde_json::from_str::<Verb>("\"Embed\"").is_err());
    }

    #[test]
    fn the_error_of_a_verb_name_says_which_rule_failed() {
        assert_eq!(
            VerbNameError::TooShort.to_string(),
            "a verb name has 1 byte or more"
        );
        assert_eq!(
            VerbNameError::TooLong.to_string(),
            "a verb name has 32 bytes or less"
        );
        assert_eq!(
            VerbNameError::BadFirstByte.to_string(),
            "a verb name starts with a to z"
        );
        assert_eq!(
            VerbNameError::BadByte { at: 2 }.to_string(),
            "byte 2 of a verb name is not a to z, 0 to 9 or _"
        );
    }

    #[test]
    fn a_limit_is_one_or_more() {
        assert_eq!(Limit::new(0), Err(LimitError::BelowOne));
        assert_eq!(LimitError::BelowOne.to_string(), "a limit is 1 or more");
        assert_eq!(Limit::new(u64::MAX).unwrap().get(), Some(u64::MAX));
        assert_eq!(Limit::new(7).unwrap().as_integer(), &Integer::from(7_u64));
    }

    #[test]
    fn a_limit_past_64_bits_is_never_reached() {
        let huge = Limit(Integer::from_digits(Sign::Plus, "1180591620717411303424"));

        assert_eq!(huge.get(), None);
        assert!(!huge.is_reached_by(u64::MAX));
        assert!(!huge.is_exceeded_by(u64::MAX));
    }

    #[test]
    fn an_integer_text_reads_as_the_python_code_reads_it() {
        let accepted = [
            ("60", "60"),
            (" 60 ", "60"),
            ("\u{a0}60\u{3000}", "60"),
            ("+60", "60"),
            ("-5", "-5"),
            ("6_0", "60"),
            ("1_000.0", "1000"),
            ("060", "60"),
            ("0_60", "60"),
            ("0__60", "60"),
            ("0__0", "0"),
            ("00", "0"),
            ("-0", "0"),
            ("60.000", "60"),
            ("0.0", "0"),
            (" +0_6_0.0 ", "60"),
            // A `-` after the zeros at the start is the sign.
            ("0-8", "-8"),
            ("00__-2", "-2"),
            ("0_-9", "-9"),
            ("0-0", "0"),
            ("0-_8", "-8"),
            ("0-8_0", "-80"),
            ("0-8.0", "-8"),
            (" +0-8 ", "-8"),
        ];
        let refused = [
            "",
            "  ",
            "+",
            "-",
            "+-60",
            "+ 60",
            "_60",
            "60_",
            "6__0",
            "+_60",
            "0_",
            "60.",
            ".0",
            "60.5",
            "60.0_0",
            "5_.0",
            "5._",
            "60.0.0",
            "6e1",
            "0x3c",
            "1 000",
            "\u{1c}60",
            "\u{ff16}\u{ff10}",
            "6\u{660}",
            "inf",
            "true",
            "0-",
            "0-08",
            "0-0_8",
            "0-00",
            "0-__8",
            "0-8_",
            "0-.0",
            "0-8.5",
            "0--8",
            "0-+8",
            "0+8",
            "-0-8",
            "8-8",
            "_0-8",
        ];

        for (text, number) in accepted {
            assert_eq!(text_integer(text).unwrap().to_string(), number, "{text:?}");
        }

        for text in refused {
            assert_eq!(
                text_integer(text),
                Err(IssueKind::NotIntegerText),
                "{text:?}"
            );
        }
    }

    #[test]
    fn an_integer_text_has_4300_digits_or_less() {
        let most = "9".repeat(4300);
        let zeros = "0".repeat(5000);

        assert!(text_integer(&most).is_ok());
        assert!(text_integer(&format!("{zeros}{most}.{zeros}")).is_ok());
        assert_eq!(
            text_integer(&format!("{most}9")),
            Err(IssueKind::IntegerTooLarge)
        );
        assert_eq!(
            text_integer(&format!("-{most}")),
            Err(IssueKind::IntegerTooLarge)
        );
    }

    #[test]
    fn a_long_integer_text_names_its_size_only_in_the_plain_form() {
        let most = "9".repeat(4300);
        let plain = [
            format!("{most}9"),
            format!("{most}9 "),
            format!("{most}9.0"),
            format!("-{most}"),
            format!("-{most}.00\u{3000}"),
        ];
        let other = [
            format!("+{most}9"),
            format!(" {most}9"),
            format!("0{most}9"),
            format!("0_{most}9"),
            format!("9_{most}"),
            format!(" -{most}"),
            format!("-0{most}"),
            format!("-9_{}", "9".repeat(4299)),
        ];

        for text in plain {
            assert_eq!(text_integer(&text), Err(IssueKind::IntegerTooLarge));
        }

        for text in other {
            assert_eq!(text_integer(&text), Err(IssueKind::NotIntegerText));
        }
    }

    #[test]
    fn a_minus_after_zeros_counts_as_one_digit_of_4300() {
        let most = "9".repeat(4299);

        assert_eq!(
            text_integer(&format!("0-{most}")).unwrap().to_string(),
            format!("-{most}")
        );
        assert!(text_integer(&format!("{}-_{most}.0", "0".repeat(5000))).is_ok());
        assert_eq!(
            text_integer(&format!("0-{most}9")),
            Err(IssueKind::NotIntegerText)
        );
    }

    #[test]
    fn a_float_reads_as_an_integer_when_it_has_no_fraction() {
        assert_eq!(float_integer(60.0), Ok(Integer::from(60_u64)));
        assert_eq!(float_integer(-0.0), Ok(Integer::from(0_u64)));
        assert_eq!(float_integer(-3.0), Ok(Integer::from(-3_i64)));
        assert_eq!(
            float_integer(9.223_372_036_854_775e18).unwrap().to_string(),
            "9223372036854774784"
        );
        assert_eq!(float_integer(60.5), Err(IssueKind::Fraction));
        assert_eq!(float_integer(5e-324), Err(IssueKind::Fraction));
        assert_eq!(float_integer(f64::NAN), Err(IssueKind::NotFinite));
        assert_eq!(float_integer(f64::INFINITY), Err(IssueKind::NotFinite));
        assert_eq!(
            float_integer(FLOAT_INTEGER_BOUND),
            Err(IssueKind::IntegerTooLarge)
        );
        assert_eq!(
            float_integer(-FLOAT_INTEGER_BOUND),
            Err(IssueKind::IntegerTooLarge)
        );
        assert_eq!(float_integer(1e19), Err(IssueKind::IntegerTooLarge));
    }

    #[test]
    fn the_raw_form_converts_to_the_valid_form() {
        let grants = GrantFile::try_from(raw()).unwrap();

        assert_eq!(grants.family().as_str(), "chat");
        assert_eq!(grants.rev().as_str(), "reg-9f21c4");
        assert_eq!(grants.model_alias().as_str(), "fast");
        assert_eq!(grants.token_sha256().iter().count(), 1);
        assert!(grants.tools().is_empty());
        assert!(grants.verbs().is_empty());
        assert!(grants.delegates().is_empty());
        assert!(grants.approval().is_empty());
        assert_eq!(grants.limits().pep_rpm().get(), Some(60));
        assert_eq!(grants.limits().max_inflight_delegations().get(), Some(2));
        assert_eq!(grants.limits().max_open_gates().get(), Some(10));
        assert_eq!(grants.file_name(), "chat.json");
    }

    #[test]
    fn the_raw_form_with_each_field_converts() {
        let grants = GrantFile::try_from(RawGrantFile {
            token_sha256: vec![DIGEST.to_owned(), OTHER_DIGEST.to_owned()],
            tools: BTreeMap::from([("kagi".to_owned(), vec!["search".to_owned()])]),
            verbs: BTreeMap::from([
                ("embed".to_owned(), RawVerbFence::default()),
                (
                    "ha_call".to_owned(),
                    RawVerbFence {
                        allow: Some(vec![RawHaAllow {
                            domain: "light".to_owned(),
                            service: "turn_on".to_owned(),
                            entity_id: Some("light.example_lamp".to_owned()),
                        }]),
                        ..RawVerbFence::default()
                    },
                ),
                (
                    "enqueue".to_owned(),
                    RawVerbFence {
                        targets: Some(vec!["scrum-lead".to_owned()]),
                        ..RawVerbFence::default()
                    },
                ),
                (
                    "release".to_owned(),
                    RawVerbFence {
                        components: Some(vec!["chaperone".to_owned()]),
                        ..RawVerbFence::default()
                    },
                ),
            ]),
            delegates: vec!["vault-oracle".to_owned()],
            approval: vec!["ha_call".to_owned()],
            limits: RawLimits {
                max_inflight_delegations: Some(8),
                ..RawLimits::default()
            },
            ..raw()
        })
        .unwrap();
        let verb = |name: &str| {
            grants
                .verbs()
                .get(&name.parse::<VerbName>().unwrap())
                .unwrap()
        };
        let triple = &verb("ha_call").allow().unwrap()[0];

        assert_eq!(grants.token_sha256().iter().count(), 2);
        assert_eq!(
            grants.tools()[&"kagi".parse::<ServerName>().unwrap()][0].as_str(),
            "search"
        );
        assert_eq!(verb("embed").allow(), None);
        assert_eq!(verb("embed").targets(), None);
        assert_eq!(verb("embed").components(), None);
        assert_eq!(triple.domain().as_str(), "light");
        assert_eq!(triple.service().as_str(), "turn_on");
        assert_eq!(triple.entity_id(), Some("light.example_lamp"));
        assert_eq!(verb("enqueue").targets().unwrap(), [family("scrum-lead")]);
        assert_eq!(verb("release").components().unwrap(), ["chaperone"]);
        assert_eq!(grants.delegates(), [family("vault-oracle")]);
        assert!(grants.needs_approval("ha_call"));
        assert!(!grants.needs_approval("ha_cal"));
        assert!(!grants.needs_approval("ha_call "));
        assert_eq!(grants.limits().max_inflight_delegations().get(), Some(8));
    }

    #[test]
    fn the_raw_form_with_another_version_is_refused() {
        for version in [0, 1, 3] {
            let converted = GrantFile::try_from(RawGrantFile { version, ..raw() });

            assert_eq!(
                converted,
                Err(GrantError::UnknownVersion(Integer::from(version)))
            );
        }
    }

    #[test]
    fn the_conversion_collects_each_issue_of_the_raw_form() {
        let issues = issues_of(RawGrantFile {
            family: "Chat".to_owned(),
            rev: String::new(),
            token_sha256: vec![],
            tools: BTreeMap::from([("Kagi".to_owned(), vec!["1a".to_owned()])]),
            limits: RawLimits {
                pep_rpm: Some(0),
                ..RawLimits::default()
            },
            ..raw()
        });

        assert_eq!(
            issues,
            [
                "at \"family\": a family name starts with a to z",
                "at \"rev\": the text has 1 character or more",
                "at \"token_sha256\": the array has 1 valid item or more",
                "at \"tools\", \"Kagi\", the key: a server name starts with a to z",
                "at \"tools\", \"Kagi\", 0: a tool name starts with A to Z or a to z",
                "at \"limits\", \"pep_rpm\": the integer is 1 or more",
            ]
        );
    }

    #[test]
    fn what_the_writer_writes_the_reader_takes() {
        let grants = GrantFile::try_from(RawGrantFile {
            verbs: BTreeMap::from([
                ("release".to_owned(), RawVerbFence::default()),
                ("embed".to_owned(), RawVerbFence::default()),
                ("teleport".to_owned(), RawVerbFence::default()),
                ("a_new_verb".to_owned(), RawVerbFence::default()),
            ]),
            ..raw()
        })
        .unwrap();
        let bytes = grants.to_bytes().unwrap();
        let text = String::from_utf8(bytes.clone()).unwrap();
        let place = |verb: &str| text.find(&format!("\"{verb}\"")).unwrap();

        assert_eq!(GrantFile::parse(&bytes, &family("chat")), Ok(grants));
        assert!(text.ends_with("}\n") && !text.ends_with("\n\n"));
        assert!(place("embed") < place("release"));
        assert!(place("release") < place("a_new_verb"));
        assert!(place("a_new_verb") < place("teleport"));
    }

    /// A grant file with `tools` tools of 64 bytes for one server, and a
    /// model alias of `alias` characters.
    fn with_tools(tools: usize, alias: usize) -> GrantFile {
        let tools = (0..tools).map(|tool| format!("tool_{tool:059}")).collect();

        GrantFile::try_from(RawGrantFile {
            model_alias: "m".repeat(alias),
            tools: BTreeMap::from([("kagi".to_owned(), tools)]),
            ..raw()
        })
        .unwrap()
    }

    #[test]
    fn the_writer_refuses_a_file_over_the_cap() {
        // A list of tools has no cap on its count, so the raw form converts.
        let error = with_tools(4000, 1).to_bytes().unwrap_err();
        let GrantWriteError::TooLarge { bytes } = error;

        assert!(bytes > GRANT_FILE_MAX_BYTES);
        assert_eq!(error.to_string(), format!("{bytes} bytes exceeds the cap"));

        let error: &dyn Error = &error;

        assert!(error.source().is_none());
    }

    #[test]
    fn the_writer_writes_a_file_of_the_cap_and_no_byte_more() {
        // One tool is one line of 74 bytes. The alias then fills the file to
        // the cap: one character of it is one byte.
        let line =
            with_tools(2, 1).to_bytes().unwrap().len() - with_tools(1, 1).to_bytes().unwrap().len();
        let empty = with_tools(1, 1).to_bytes().unwrap().len() - line;
        let tools = (GRANT_FILE_MAX_BYTES - empty) / line;
        let alias = 1 + (GRANT_FILE_MAX_BYTES - empty) % line;
        let at_cap = with_tools(tools, alias);
        let bytes = at_cap.to_bytes().unwrap();

        assert_eq!(line, 74);
        assert_eq!(bytes.len(), GRANT_FILE_MAX_BYTES);
        assert_eq!(GrantFile::parse(&bytes, &family("chat")), Ok(at_cap));
        assert_eq!(
            with_tools(tools, alias + 1).to_bytes(),
            Err(GrantWriteError::TooLarge {
                bytes: GRANT_FILE_MAX_BYTES + 1
            })
        );
    }

    #[test]
    fn a_file_that_the_reader_took_can_be_too_long_to_write() {
        // GRINNING FACE is 4 bytes in the file that the reader takes. The
        // writer writes it as two `\u` escapes, which are 12 bytes.
        let entry = "\u{1f600}".repeat(250);
        let entries = vec![format!("\"{entry}\""); 250].join(",");
        let text = format!(
            "{{\"version\":2,\"family\":\"chat\",\"rev\":\"reg-9f21c4\",\
             \"token_sha256\":[\"{DIGEST}\"],\"model_alias\":\"fast\",\"approval\":[{entries}]}}"
        );
        let grants = GrantFile::parse(text.as_bytes(), &family("chat")).unwrap();
        let rotated = grants.rotated(
            "reg-000002".parse().unwrap(),
            TokenDigests::one(OTHER_DIGEST.parse().unwrap()),
        );

        assert!(text.len() <= GRANT_FILE_MAX_BYTES);
        assert!(matches!(
            grants.to_bytes(),
            Err(GrantWriteError::TooLarge { bytes }) if bytes > 2 * GRANT_FILE_MAX_BYTES
        ));
        assert!(matches!(
            rotated.to_bytes(),
            Err(GrantWriteError::TooLarge { .. })
        ));
    }

    #[test]
    fn a_rotation_moves_the_digests_and_no_grant() {
        let before = GrantFile::try_from(raw()).unwrap();
        let digests = TokenDigests::two(DIGEST.parse().unwrap(), OTHER_DIGEST.parse().unwrap());
        let after = before.rotated("reg-000002".parse().unwrap(), digests.clone());
        let other = GrantFile::try_from(RawGrantFile {
            delegates: vec!["vault-oracle".to_owned()],
            ..raw()
        })
        .unwrap();

        assert_ne!(after, before);
        assert!(after.same_grants(&before));
        assert_eq!(after.rev().as_str(), "reg-000002");
        assert_eq!(after.token_sha256(), &digests);
        assert!(!other.same_grants(&before));
    }

    #[test]
    fn a_file_over_the_cap_is_refused_before_the_reader_reads_it() {
        let bytes = vec![b' '; GRANT_FILE_MAX_BYTES + 1];
        let error = GrantFile::parse(&bytes, &family("chat")).unwrap_err();

        assert_eq!(
            error.error(),
            &GrantError::TooLarge {
                bytes: GRANT_FILE_MAX_BYTES + 1
            }
        );
        assert_eq!(
            error.to_string(),
            "grants/chat.json: 262145 bytes exceeds the cap"
        );
        assert!(matches!(
            GrantFile::parse(&bytes[1..], &family("chat"))
                .unwrap_err()
                .error(),
            GrantError::NotJson(_)
        ));
    }

    #[test]
    fn the_error_names_the_file_and_the_rule() {
        let file = family("chat");
        let said = |bytes: &[u8]| GrantFile::parse(bytes, &file).unwrap_err().to_string();
        let minimal = GrantFile::try_from(raw()).unwrap().to_bytes().unwrap();

        assert_eq!(
            said(b"not json"),
            "grants/chat.json: not JSON (the text stops being JSON after 0 characters)"
        );
        assert_eq!(
            said(b"[]"),
            "grants/chat.json: the top level is not an object"
        );
        assert_eq!(said(b"{}"), "grants/chat.json: version is not an integer");
        assert_eq!(
            said(br#"{"version": 2, "family": 5, "note": 1}"#),
            "grants/chat.json: invalid (5 errors)"
        );
        assert_eq!(
            GrantFile::parse(&minimal, &family("code"))
                .unwrap_err()
                .to_string(),
            "grants/code.json: names family 'chat'"
        );

        let error = GrantFile::parse(b"[]", &file).unwrap_err();

        assert_eq!(error.family(), &file);
        assert_eq!(
            error.source().unwrap().to_string(),
            "the top level is not an object"
        );
    }

    #[test]
    fn the_message_of_an_invalid_file_holds_no_text_of_the_file() {
        let secret = "s3cr3t-text-of-the-file";
        let bytes = format!(r#"{{"version": 2, "{secret}": "{secret}", "family": "{secret}"}}"#);
        let error = GrantFile::parse(bytes.as_bytes(), &family("chat")).unwrap_err();

        assert!(!error.to_string().contains(secret));
    }
}
