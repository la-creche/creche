//! What the host writes to the playpen and the playpen reads (contract 03
//! §3, §4).
//!
//! [`HostMessage`] is one message of that direction. The host makes a value
//! with a constructor, and [`HostMessage::encode`] writes the bytes that the
//! Python host writes. The playpen makes a value with [`HostMessage::parse`].
//!
//! The Python builders take each text and each object as it comes. The types
//! here hold only what the contract names, so a host cannot write a line
//! that the playpen refuses for its form.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::claim::TurnAddress;
use super::frame::{EncodeError, MAX_LINE_BYTES, framed};
use super::json::{self, Dialect, Json, JsonObject, ObjectWriter, array_text};
use super::vocabulary::{HostType, Word, WorkspaceKind};
use crate::ids::{AttachmentName, FamilyName, SandboxName, SessionId, Ulid};

/// What the Python host names itself in `hello`: `sessiond/<version>`.
pub const HOST_LABEL: &str = "sessiond/0.1.0";

/// The largest count of bytes of one prompt (contract 03 §8).
pub const MAX_PROMPT_BYTES: usize = 262_144;

/// The largest count of bytes of one persona (contract 03 §8).
pub const MAX_PERSONA_BYTES: usize = 16_384;

/// The largest count of bytes of one path inside the sandbox.
pub const MAX_PATH_BYTES: usize = 4096;

/// The largest count of bytes of one name that the playpen reads from the
/// host: a config revision, a pi entry id, a nonce and a model.
pub const MAX_NAME_BYTES: usize = 200;

/// The largest `deadline_s` that the playpen accepts: one day.
pub const MAX_DEADLINE_S: u64 = 86_400;

/// The largest `grace_ms` that the playpen accepts: ten minutes.
pub const MAX_GRACE_MS: u64 = 600_000;

/// The largest count of attachment names that the playpen accepts in one
/// turn.
pub const MAX_ATTACHMENTS: usize = 64;

/// Makes the type of one number of a message.
macro_rules! quantity {
    ($(#[$attribute:meta])* $name:ident) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(u64);

        impl $name {
            /// The value of this number.
            #[must_use]
            pub const fn new(value: u64) -> Self {
                Self(value)
            }

            /// The number.
            #[must_use]
            pub const fn get(self) -> u64 {
                self.0
            }
        }

        impl From<u64> for $name {
            fn from(value: u64) -> Self {
                Self(value)
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                self.0.fmt(f)
            }
        }
    };
}

quantity! {
    /// The credential generation that the host expects (contract 03 §12).
    ///
    /// ```
    /// use creche_contracts::channel::host::EnvEpoch;
    ///
    /// assert_eq!(EnvEpoch::new(7).get(), 7);
    /// ```
    ///
    /// Code outside this module cannot name the field:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::EnvEpoch;
    ///
    /// let epoch = EnvEpoch(7);
    /// ```
    EnvEpoch
}

quantity! {
    /// A count of seconds: a deadline or a time to live.
    ///
    /// ```
    /// use creche_contracts::channel::host::Seconds;
    ///
    /// assert_eq!(Seconds::new(600).get(), 600);
    /// ```
    ///
    /// Code outside this module cannot name the field:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::Seconds;
    ///
    /// let seconds = Seconds(600);
    /// ```
    Seconds
}

quantity! {
    /// A count of milliseconds: a grace time or the coalescing window.
    ///
    /// ```
    /// use creche_contracts::channel::host::Millis;
    ///
    /// assert_eq!(Millis::STOP_GRACE.get(), 5000);
    /// ```
    ///
    /// Code outside this module cannot name the field:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::Millis;
    ///
    /// let grace = Millis(5000);
    /// ```
    Millis
}

quantity! {
    /// A cap on the resident pi processes of one sandbox (contract 03 §6
    /// rule 8).
    ///
    /// ```
    /// use creche_contracts::channel::host::ProcessCap;
    ///
    /// assert_eq!(ProcessCap::DEFAULT.get(), 12);
    /// ```
    ///
    /// Code outside this module cannot name the field:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::ProcessCap;
    ///
    /// let cap = ProcessCap(12);
    /// ```
    ProcessCap
}

quantity! {
    /// A count of bytes: the line limit that `hello` states.
    ///
    /// ```
    /// use creche_contracts::channel::host::ByteCount;
    ///
    /// assert_eq!(ByteCount::LINE_LIMIT.get(), 1_048_576);
    /// ```
    ///
    /// Code outside this module cannot name the field:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::ByteCount;
    ///
    /// let limit = ByteCount(1);
    /// ```
    ByteCount
}

impl Seconds {
    /// How long the playpen waits for a `ping` before it exits (contract
    /// 03 §11.1): three missed pings.
    pub const HOST_DEADLINE: Self = Self(90);
}

impl Millis {
    /// The grace time of `stop_process` and of `shutdown` that the host
    /// sends (contract 03 §4.5).
    pub const STOP_GRACE: Self = Self(5000);

    /// The coalescing window that the host sends (contract 03 §9 rule 4).
    pub const COALESCE: Self = Self(50);
}

impl ProcessCap {
    /// The cap of a family that states none (contract 03 §6 rule 8).
    pub const DEFAULT: Self = Self(12);
}

impl ByteCount {
    /// The line limit of the contract (contract 03 §2 rule 5).
    pub const LINE_LIMIT: Self = Self(1_048_576);
}

/// Which bound a text breaks.
enum Bound {
    TooShort,
    TooLong,
}

/// Checks the count of bytes of `text`.
fn within(text: &str, min: usize, max: usize) -> Result<(), Bound> {
    if text.len() < min {
        return Err(Bound::TooShort);
    }

    if text.len() > max {
        return Err(Bound::TooLong);
    }

    Ok(())
}

/// Makes the type of one text of a message whose only rule is its size, and
/// the error type of that text.
macro_rules! sized_text {
    (
        $(#[$attribute:meta])*
        $name:ident,
        $(#[$error_attribute:meta])*
        $error:ident,
        $noun:literal,
        $min:expr,
        $max:expr
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);

        impl $name {
            /// The text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }

            fn check(text: &str) -> Result<(), $error> {
                within(text, $min, $max).map_err(|bound| match bound {
                    Bound::TooShort => $error::TooShort,
                    Bound::TooLong => $error::TooLong,
                })
            }
        }

        impl FromStr for $name {
            type Err = $error;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                Self::check(text)?;

                Ok(Self(text.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = $error;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                Self::check(&text)?;

                Ok(Self(text))
            }
        }

        $(#[$error_attribute])*
        ///
        /// No variant holds the text.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum $error {
            /// The text has fewer bytes than the type permits.
            TooShort,
            /// The text has more bytes than the type permits.
            TooLong,
        }

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                match self {
                    Self::TooShort => write!(f, "{} has {} bytes or more", $noun, $min),
                    Self::TooLong => write!(f, "{} has {} bytes at most", $noun, $max),
                }
            }
        }

        impl Error for $error {}
    };
}

sized_text! {
    /// The revision of the family config mount (contract 03 §7.1): 1 to 200
    /// bytes.
    ///
    /// ```
    /// use creche_contracts::channel::host::ConfigRev;
    ///
    /// let revision: ConfigRev = "reg-9f21c4".parse()?;
    /// assert_eq!(revision.as_str(), "reg-9f21c4");
    /// # Ok::<(), creche_contracts::channel::host::ConfigRevError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::ConfigRev;
    ///
    /// let revision = ConfigRev(String::new());
    /// ```
    // CONTRACT-QUESTION: contract 03 §4.1 calls `config_rev` a string and
    // gives no grammar. The playpen refuses an empty one and one of more
    // than 200 UTF-16 code units. This type takes that reading, in bytes. A
    // wider grammar costs a change to the playpen.
    ConfigRev,
    /// Why a text is not a config revision.
    ConfigRevError,
    "a config revision",
    1,
    MAX_NAME_BYTES
}

sized_text! {
    /// The id of one pi entry: a cursor into a session (contract 03 §4.1,
    /// §4.8): 1 to 200 bytes.
    ///
    /// ```
    /// use creche_contracts::channel::host::EntryId;
    ///
    /// let id: EntryId = "e4".parse()?;
    /// assert_eq!(id.as_str(), "e4");
    /// # Ok::<(), creche_contracts::channel::host::EntryIdError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::EntryId;
    ///
    /// let id = EntryId(String::new());
    /// ```
    // CONTRACT-QUESTION: contract 03 gives no grammar for a pi entry id. The
    // playpen refuses an empty one and one of more than 200 UTF-16 code
    // units. This type takes that reading, in bytes.
    EntryId,
    /// Why a text is not a pi entry id.
    EntryIdError,
    "a pi entry id",
    1,
    MAX_NAME_BYTES
}

sized_text! {
    /// The nonce of one `ping` (contract 03 §4.6): 200 bytes at most.
    ///
    /// ```
    /// use creche_contracts::channel::host::Nonce;
    ///
    /// let nonce: Nonce = "9f13".parse()?;
    /// assert_eq!(nonce.as_str(), "9f13");
    /// # Ok::<(), creche_contracts::channel::host::NonceError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::Nonce;
    ///
    /// let nonce = Nonce("9f13".repeat(99));
    /// ```
    Nonce,
    /// Why a text is not a nonce.
    NonceError,
    "a nonce",
    0,
    MAX_NAME_BYTES
}

sized_text! {
    /// The text of one prompt or of one steering message: at most
    /// [`MAX_PROMPT_BYTES`] bytes (contract 03 §8).
    ///
    /// ```
    /// use creche_contracts::channel::host::PromptText;
    ///
    /// let prompt: PromptText = "Is the door locked?".parse()?;
    /// assert_eq!(prompt.as_str(), "Is the door locked?");
    /// # Ok::<(), creche_contracts::channel::host::PromptTextError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::PromptText;
    ///
    /// let prompt = PromptText("a".repeat(300_000));
    /// ```
    // CONTRACT-QUESTION: contract 03 §8 caps `prompt` and gives no cap for the
    // `message` of `steer`. The playpen applies the cap of a prompt to it.
    // This type takes that reading.
    PromptText,
    /// Why a text is not a prompt.
    PromptTextError,
    "a prompt",
    0,
    MAX_PROMPT_BYTES
}

sized_text! {
    /// The persona text of a folder: at most [`MAX_PERSONA_BYTES`] bytes
    /// (contract 03 §8). The text carries no authority.
    ///
    /// ```
    /// use creche_contracts::channel::host::Persona;
    ///
    /// let persona: Persona = "Answer in one sentence.".parse()?;
    /// assert_eq!(persona.as_str(), "Answer in one sentence.");
    /// # Ok::<(), creche_contracts::channel::host::PersonaError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::Persona;
    ///
    /// let persona = Persona("a".repeat(20_000));
    /// ```
    Persona,
    /// Why a text is not a persona.
    PersonaError,
    "a persona",
    0,
    MAX_PERSONA_BYTES
}

sized_text! {
    /// A pi model pattern, or a bare model alias (contract 03 §4.1.1): 1 to
    /// 200 bytes.
    ///
    /// ```
    /// use creche_contracts::channel::host::Model;
    ///
    /// let model: Model = "code-router".parse()?;
    /// assert_eq!(model.as_str(), "code-router");
    /// # Ok::<(), creche_contracts::channel::host::ModelError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::channel::host::Model;
    ///
    /// let model = Model(String::new());
    /// ```
    // CONTRACT-QUESTION: contract 03 §4.1 gives no grammar for `model`. The
    // Python host leaves an empty one out, and the playpen has no cap. This
    // type refuses an empty one and takes the cap of each other name of the
    // channel. A longer pattern costs a change to this type only.
    Model,
    /// Why a text is not a model.
    ModelError,
    "a model",
    1,
    MAX_NAME_BYTES
}

/// What stands between two segments of a path.
const PATH_SEPARATOR: char = '/';

/// The segment that names the directory above.
const PARENT_SEGMENT: &str = "..";

/// An absolute path inside the sandbox: `cwd` or `session_dir` (contract 03
/// §4.1, §7.1).
///
/// The path starts with `/`. It has no `..` segment and no NUL, so it cannot
/// leave its mount. It has at most [`MAX_PATH_BYTES`] bytes.
///
/// ```
/// use creche_contracts::channel::host::SandboxPath;
///
/// let path: SandboxPath = "/srv/sessions/chat/tui-1/pi".parse()?;
/// assert_eq!(path.as_str(), "/srv/sessions/chat/tui-1/pi");
/// assert!("/srv/../etc".parse::<SandboxPath>().is_err());
/// # Ok::<(), creche_contracts::channel::host::SandboxPathError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::host::SandboxPath;
///
/// let path = SandboxPath(String::from("../etc"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SandboxPath(String);

impl SandboxPath {
    /// The path as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    fn check(text: &str) -> Result<(), SandboxPathError> {
        if !text.starts_with(PATH_SEPARATOR) {
            return Err(SandboxPathError::NotAbsolute);
        }

        if text.len() > MAX_PATH_BYTES {
            return Err(SandboxPathError::TooLong);
        }

        if text.contains('\0') {
            return Err(SandboxPathError::Nul);
        }

        if text
            .split(PATH_SEPARATOR)
            .any(|part| part == PARENT_SEGMENT)
        {
            return Err(SandboxPathError::ParentSegment);
        }

        Ok(())
    }
}

impl FromStr for SandboxPath {
    type Err = SandboxPathError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::check(text)?;

        Ok(Self(text.to_owned()))
    }
}

impl TryFrom<String> for SandboxPath {
    type Error = SandboxPathError;

    fn try_from(text: String) -> Result<Self, Self::Error> {
        Self::check(&text)?;

        Ok(Self(text))
    }
}

/// Why a text is not a path inside the sandbox.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxPathError {
    /// The text does not start with `/`.
    NotAbsolute,
    /// The text has more than [`MAX_PATH_BYTES`] bytes.
    TooLong,
    /// The text holds a NUL.
    Nul,
    /// The text has a `..` segment.
    ParentSegment,
}

impl fmt::Display for SandboxPathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotAbsolute => f.write_str("a sandbox path starts with /"),
            Self::TooLong => write!(f, "a sandbox path has {MAX_PATH_BYTES} bytes at most"),
            Self::Nul => f.write_str("a sandbox path holds no NUL"),
            Self::ParentSegment => f.write_str("a sandbox path has no .. segment"),
        }
    }
}

impl Error for SandboxPathError {}

/// The largest count of digits in one number of a protocol version.
const VERSION_DIGITS_MAX: usize = 9;

/// The version of the protocol: `<major>.<minor>` (contract 03 §3).
///
/// The major numbers of the two sides must be equal. A minor number can
/// differ.
///
/// ```
/// use creche_contracts::channel::host::ProtocolVersion;
///
/// let version: ProtocolVersion = "1.0".parse()?;
/// assert_eq!(version, ProtocolVersion::CURRENT);
/// assert_eq!(version.to_string(), "1.0");
/// # Ok::<(), creche_contracts::channel::host::ProtocolVersionError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::ProtocolVersion;
///
/// let version = ProtocolVersion { major: 9, minor: 9 };
/// ```
// CONTRACT-QUESTION: contract 03 §3 writes the version as `<major>.<minor>`
// and gives no grammar for a number. The playpen accepts each text with a
// `.`. This type takes the conservative reading: two numbers of 1 to 9 ASCII
// digits, with no zero before another digit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ProtocolVersion {
    major: u32,
    minor: u32,
}

impl ProtocolVersion {
    /// The version that this code speaks.
    pub const CURRENT: Self = Self { major: 1, minor: 0 };

    /// The major number.
    #[must_use]
    pub fn major(self) -> u32 {
        self.major
    }

    /// The minor number.
    #[must_use]
    pub fn minor(self) -> u32 {
        self.minor
    }

    fn number(text: &str) -> Option<u32> {
        let digits = !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit());
        let zero_first = text.len() > 1 && text.starts_with('0');
        if !digits || zero_first || text.len() > VERSION_DIGITS_MAX {
            return None;
        }

        text.parse().ok()
    }
}

impl FromStr for ProtocolVersion {
    type Err = ProtocolVersionError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let (major, minor) = text.split_once('.').ok_or(ProtocolVersionError::NoDot)?;
        let major = Self::number(major).ok_or(ProtocolVersionError::BadNumber)?;
        let minor = Self::number(minor).ok_or(ProtocolVersionError::BadNumber)?;

        Ok(Self { major, minor })
    }
}

impl fmt::Display for ProtocolVersion {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}", self.major, self.minor)
    }
}

/// Why a text is not a protocol version.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProtocolVersionError {
    /// The text has no `.`.
    NoDot,
    /// A number is not 1 to 9 ASCII digits, or has a zero before another
    /// digit.
    BadNumber,
}

impl fmt::Display for ProtocolVersionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoDot => f.write_str("a protocol version is <major>.<minor>"),
            Self::BadNumber => f.write_str("a number of a protocol version is 1 to 9 digits"),
        }
    }
}

impl Error for ProtocolVersionError {}

/// The work directory that a turn runs in (contract 03 §7.2).
///
/// The host sends no path. The playpen derives the directory from the
/// session id of the owner.
///
/// ```
/// use creche_contracts::channel::host::Workspace;
/// use creche_contracts::channel::vocabulary::WorkspaceKind;
///
/// let workspace = Workspace::code_sandbox("owui-3f2a".parse().unwrap());
/// assert_eq!(workspace.kind(), WorkspaceKind::CodeSandbox);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::Workspace;
/// use creche_contracts::channel::vocabulary::WorkspaceKind;
///
/// let workspace = Workspace {
///     kind: WorkspaceKind::CodeSandbox,
///     owner_session: "owui-3f2a".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Workspace {
    kind: WorkspaceKind,
    owner_session: SessionId,
}

impl Workspace {
    /// The work directory of the chat session `owner_session`.
    #[must_use]
    pub fn code_sandbox(owner_session: SessionId) -> Self {
        Self {
            kind: WorkspaceKind::CodeSandbox,
            owner_session,
        }
    }

    /// The kind of the workspace.
    #[must_use]
    pub fn kind(&self) -> WorkspaceKind {
        self.kind
    }

    /// The session that owns the directory.
    #[must_use]
    pub fn owner_session(&self) -> &SessionId {
        &self.owner_session
    }

    fn encoded(&self) -> String {
        ObjectWriter::new()
            .text("kind", self.kind.as_str())
            .text("owner_session", self.owner_session.as_str())
            .finish()
    }
}

/// The pi entry that a turn forks from (contract 03 §4.1).
///
/// ```
/// use creche_contracts::channel::host::Branch;
///
/// let branch = Branch::new("e4".parse().unwrap());
/// assert_eq!(branch.fork_from().as_str(), "e4");
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::Branch;
///
/// let branch = Branch { fork_from: "e4".parse().unwrap() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Branch {
    fork_from: EntryId,
}

impl Branch {
    /// A fork from the entry `fork_from`.
    #[must_use]
    pub fn new(fork_from: EntryId) -> Self {
        Self { fork_from }
    }

    /// The entry that the turn forks from.
    #[must_use]
    pub fn fork_from(&self) -> &EntryId {
        &self.fork_from
    }

    fn encoded(&self) -> String {
        ObjectWriter::new()
            .text("fork_from", self.fork_from.as_str())
            .finish()
    }
}

/// The chain that a turn of a thin job belongs to (contract 03 §7.4 rule 4).
///
/// The chaperone mints the id. The session of the caller is advisory, and
/// the chaperone can send none.
///
/// ```
/// use creche_contracts::channel::host::Delegation;
///
/// let delegation = Delegation::new("01JBQ7WZ0X4T9V6K2H8M3N5PQS".parse().unwrap(), None);
/// assert_eq!(delegation.caller_session(), None);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::Delegation;
///
/// let delegation = Delegation {
///     id: "01JBQ7WZ0X4T9V6K2H8M3N5PQS".parse().unwrap(),
///     caller_session: None,
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delegation {
    id: Ulid,
    caller_session: Option<SessionId>,
}

impl Delegation {
    /// The chain `id`, and the session that asked for the job.
    #[must_use]
    pub fn new(id: Ulid, caller_session: Option<SessionId>) -> Self {
        Self { id, caller_session }
    }

    /// The id of the chain.
    #[must_use]
    pub fn id(&self) -> &Ulid {
        &self.id
    }

    /// The session that asked for the job.
    #[must_use]
    pub fn caller_session(&self) -> Option<&SessionId> {
        self.caller_session.as_ref()
    }

    /// Both keys are always there, as the Python host writes them.
    fn encoded(&self) -> String {
        let caller = self.caller_session.as_ref().map(SessionId::as_str);

        ObjectWriter::new()
            .text("id", self.id.as_str())
            .text_or_null("caller_session", caller)
            .finish()
    }
}

/// What the playpen needs to start the pi process of one session (contract
/// 03 §4.7). `open_session`, `start_turn` and `get_entries` carry it.
///
/// ```
/// use creche_contracts::channel::host::{Binding, EnvEpoch};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// assert_eq!(binding.session().as_str(), "tui-1");
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0616
/// use creche_contracts::channel::host::{Binding, EnvEpoch};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// let session = binding.session;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    session: SessionId,
    cwd: SandboxPath,
    session_dir: SandboxPath,
    env_epoch: EnvEpoch,
    config_rev: ConfigRev,
    model: Option<Model>,
    workspace: Option<Workspace>,
}

impl Binding {
    /// The binding of `session`, with no model and no workspace.
    #[must_use]
    pub fn new(
        session: SessionId,
        cwd: SandboxPath,
        session_dir: SandboxPath,
        env_epoch: EnvEpoch,
        config_rev: ConfigRev,
    ) -> Self {
        Self {
            session,
            cwd,
            session_dir,
            env_epoch,
            config_rev,
            model: None,
            workspace: None,
        }
    }

    /// The same binding with this model.
    #[must_use]
    pub fn with_model(mut self, model: Model) -> Self {
        self.model = Some(model);

        self
    }

    /// The same binding with this workspace.
    #[must_use]
    pub fn with_workspace(mut self, workspace: Workspace) -> Self {
        self.workspace = Some(workspace);

        self
    }

    /// The session.
    #[must_use]
    pub fn session(&self) -> &SessionId {
        &self.session
    }

    /// The work directory of pi.
    #[must_use]
    pub fn cwd(&self) -> &SandboxPath {
        &self.cwd
    }

    /// The directory of the pi store of the session.
    #[must_use]
    pub fn session_dir(&self) -> &SandboxPath {
        &self.session_dir
    }

    /// The credential generation that the host expects.
    #[must_use]
    pub fn env_epoch(&self) -> EnvEpoch {
        self.env_epoch
    }

    /// The revision of the family config mount.
    #[must_use]
    pub fn config_rev(&self) -> &ConfigRev {
        &self.config_rev
    }

    /// The model.
    #[must_use]
    pub fn model(&self) -> Option<&Model> {
        self.model.as_ref()
    }

    /// The workspace.
    #[must_use]
    pub fn workspace(&self) -> Option<&Workspace> {
        self.workspace.as_ref()
    }

    /// The fields that each message with a binding starts with.
    fn head(&self, writer: ObjectWriter) -> ObjectWriter {
        writer
            .text("session", self.session.as_str())
            .text("cwd", self.cwd.as_str())
            .text("session_dir", self.session_dir.as_str())
    }

    fn model_text(&self) -> Option<&str> {
        self.model.as_ref().map(Model::as_str)
    }

    fn workspace_text(&self) -> Option<String> {
        self.workspace.as_ref().map(Workspace::encoded)
    }
}

/// `hello`: the host accepts the channel (contract 03 §3).
///
/// A reader ignores an optional field that is absent or has a wrong type.
/// §3 version rule 2 forbids a fatal unknown field.
///
/// ```
/// use creche_contracts::channel::host::{EnvEpoch, Hello, ProcessCap, Seconds};
///
/// let hello = Hello::new(
///     "chat".parse().unwrap(),
///     "chat-s3".parse().unwrap(),
///     EnvEpoch::new(7),
///     Seconds::new(900),
///     ProcessCap::DEFAULT,
///     Seconds::new(0),
/// );
/// assert_eq!(hello.host_deadline_s(), Some(Seconds::HOST_DEADLINE));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{EnvEpoch, Hello, ProcessCap, Seconds};
///
/// let hello = Hello { env_epoch: EnvEpoch::new(7), pi_idle_ttl_s: Some(Seconds::new(900)) };
/// let cap = ProcessCap::DEFAULT;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hello {
    protocol: ProtocolVersion,
    host: Option<String>,
    family: FamilyName,
    sandbox: SandboxName,
    max_line_bytes: Option<ByteCount>,
    coalesce_ms: Option<Millis>,
    pi_idle_ttl_s: Option<Seconds>,
    max_resident_processes: Option<ProcessCap>,
    host_deadline_s: Option<Seconds>,
    channel_idle_ttl_s: Option<Seconds>,
    env_epoch: EnvEpoch,
}

impl Hello {
    /// The `hello` that the host sends, with each field that the Python host
    /// writes.
    #[must_use]
    pub fn new(
        family: FamilyName,
        sandbox: SandboxName,
        env_epoch: EnvEpoch,
        pi_idle_ttl_s: Seconds,
        max_resident_processes: ProcessCap,
        channel_idle_ttl_s: Seconds,
    ) -> Self {
        Self {
            protocol: ProtocolVersion::CURRENT,
            host: Some(HOST_LABEL.to_owned()),
            family,
            sandbox,
            max_line_bytes: Some(ByteCount::LINE_LIMIT),
            coalesce_ms: Some(Millis::COALESCE),
            pi_idle_ttl_s: Some(pi_idle_ttl_s),
            max_resident_processes: Some(max_resident_processes),
            host_deadline_s: Some(Seconds::HOST_DEADLINE),
            channel_idle_ttl_s: Some(channel_idle_ttl_s),
            env_epoch,
        }
    }

    /// The version of the protocol.
    #[must_use]
    pub fn protocol(&self) -> ProtocolVersion {
        self.protocol
    }

    /// What the host names itself.
    #[must_use]
    pub fn host(&self) -> Option<&str> {
        self.host.as_deref()
    }

    /// The family that the sandbox serves.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The sandbox that the host dialled.
    #[must_use]
    pub fn sandbox(&self) -> &SandboxName {
        &self.sandbox
    }

    /// The line limit.
    #[must_use]
    pub fn max_line_bytes(&self) -> Option<ByteCount> {
        self.max_line_bytes
    }

    /// The coalescing window (§9).
    #[must_use]
    pub fn coalesce_ms(&self) -> Option<Millis> {
        self.coalesce_ms
    }

    /// How long a pi process stays resident (§6).
    #[must_use]
    pub fn pi_idle_ttl_s(&self) -> Option<Seconds> {
        self.pi_idle_ttl_s
    }

    /// The cap of the family on resident pi processes (§6 rule 8).
    #[must_use]
    pub fn max_resident_processes(&self) -> Option<ProcessCap> {
        self.max_resident_processes
    }

    /// How long the playpen waits for a `ping` before it exits (§11.1).
    #[must_use]
    pub fn host_deadline_s(&self) -> Option<Seconds> {
        self.host_deadline_s
    }

    /// How long the host keeps an idle channel open (§10).
    #[must_use]
    pub fn channel_idle_ttl_s(&self) -> Option<Seconds> {
        self.channel_idle_ttl_s
    }

    /// The current credential generation (§12).
    #[must_use]
    pub fn env_epoch(&self) -> EnvEpoch {
        self.env_epoch
    }

    fn body(&self) -> String {
        let number = |value: Option<u64>| value.map(|value| value.to_string());

        ObjectWriter::new()
            .text("type", HostType::Hello.as_str())
            .text("protocol", &self.protocol.to_string())
            .text_if("host", self.host.as_deref())
            .text("family", self.family.as_str())
            .text("sandbox", self.sandbox.as_str())
            .raw_if(
                "max_line_bytes",
                number(self.max_line_bytes.map(ByteCount::get)),
            )
            .raw_if("coalesce_ms", number(self.coalesce_ms.map(Millis::get)))
            .raw_if(
                "pi_idle_ttl_s",
                number(self.pi_idle_ttl_s.map(Seconds::get)),
            )
            .raw_if(
                "max_resident_processes",
                number(self.max_resident_processes.map(ProcessCap::get)),
            )
            .raw_if(
                "host_deadline_s",
                number(self.host_deadline_s.map(Seconds::get)),
            )
            .raw_if(
                "channel_idle_ttl_s",
                number(self.channel_idle_ttl_s.map(Seconds::get)),
            )
            .raw("env_epoch", &self.env_epoch.to_string())
            .finish()
    }
}

/// `open_session`: start the pi process of a session before its first
/// prompt (contract 03 §4.7).
///
/// ```
/// use creche_contracts::channel::host::{Binding, EnvEpoch, OpenSession};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// assert_eq!(OpenSession::new(binding.clone()).binding(), &binding);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{Binding, EnvEpoch, OpenSession};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// let open = OpenSession { binding };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenSession {
    binding: Binding,
}

impl OpenSession {
    /// The message for this binding.
    #[must_use]
    pub fn new(binding: Binding) -> Self {
        Self { binding }
    }

    /// What the playpen needs to start the process.
    #[must_use]
    pub fn binding(&self) -> &Binding {
        &self.binding
    }

    fn body(&self) -> String {
        let binding = &self.binding;

        binding
            .head(ObjectWriter::new().text("type", HostType::OpenSession.as_str()))
            .raw("env_epoch", &binding.env_epoch.to_string())
            .text("config_rev", binding.config_rev.as_str())
            .text_if("model", binding.model_text())
            .raw_if("workspace", binding.workspace_text())
            .finish()
    }
}

/// `get_entries`: read the pi entries of a session back (contract 03 §4.8).
///
/// ```
/// use creche_contracts::channel::host::{Binding, EnvEpoch, GetEntries};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// let read = GetEntries::new("01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(), binding);
/// assert_eq!(read.since(), None);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{Binding, EnvEpoch, GetEntries};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// let read = GetEntries { binding, since: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GetEntries {
    request: Ulid,
    binding: Binding,
    since: Option<EntryId>,
}

impl GetEntries {
    /// A read of each entry of the session of `binding`.
    #[must_use]
    pub fn new(request: Ulid, binding: Binding) -> Self {
        Self {
            request,
            binding,
            since: None,
        }
    }

    /// The same read, of the entries after `since` only.
    #[must_use]
    pub fn with_since(mut self, since: EntryId) -> Self {
        self.since = Some(since);

        self
    }

    /// The correlation id of the host.
    #[must_use]
    pub fn request(&self) -> &Ulid {
        &self.request
    }

    /// What the playpen needs to start the process.
    #[must_use]
    pub fn binding(&self) -> &Binding {
        &self.binding
    }

    /// The last entry id that the host knows.
    #[must_use]
    pub fn since(&self) -> Option<&EntryId> {
        self.since.as_ref()
    }

    fn body(&self) -> String {
        let binding = &self.binding;
        let writer = ObjectWriter::new()
            .text("type", HostType::GetEntries.as_str())
            .text("request", self.request.as_str());

        binding
            .head(writer)
            .raw("env_epoch", &binding.env_epoch.to_string())
            .text("config_rev", binding.config_rev.as_str())
            .text_if("since", self.since.as_ref().map(EntryId::as_str))
            .text_if("model", binding.model_text())
            .raw_if("workspace", binding.workspace_text())
            .finish()
    }
}

/// What `start_turn` and `prompt` share: the turn, its prompt and its
/// options (contract 03 §4.1, §4.2).
///
/// ```
/// use creche_contracts::channel::host::{Seconds, TurnRequest};
///
/// let request = TurnRequest::new(
///     "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(),
///     "Is the door locked?".parse().unwrap(),
///     Seconds::new(600),
/// );
/// assert_eq!(request.deadline_s(), Seconds::new(600));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{Seconds, TurnRequest};
///
/// let request = TurnRequest { deadline_s: Seconds::new(600), attachments: Vec::new() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TurnRequest {
    turn: Ulid,
    prompt: PromptText,
    deadline_s: Seconds,
    persona: Option<Persona>,
    attachments: Vec<AttachmentName>,
    branch: Option<Branch>,
    delegation: Option<Delegation>,
}

impl TurnRequest {
    /// The turn `turn` with this prompt and this limit in seconds.
    #[must_use]
    pub fn new(turn: Ulid, prompt: PromptText, deadline_s: Seconds) -> Self {
        Self {
            turn,
            prompt,
            deadline_s,
            persona: None,
            attachments: Vec::new(),
            branch: None,
            delegation: None,
        }
    }

    /// The same turn with this persona.
    #[must_use]
    pub fn with_persona(mut self, persona: Persona) -> Self {
        self.persona = Some(persona);

        self
    }

    /// The same turn with these file names of the session inbox.
    #[must_use]
    pub fn with_attachments(mut self, attachments: Vec<AttachmentName>) -> Self {
        self.attachments = attachments;

        self
    }

    /// The same turn as a fork.
    #[must_use]
    pub fn with_branch(mut self, branch: Branch) -> Self {
        self.branch = Some(branch);

        self
    }

    /// The same turn inside a chain.
    #[must_use]
    pub fn with_delegation(mut self, delegation: Delegation) -> Self {
        self.delegation = Some(delegation);

        self
    }

    /// The turn id.
    #[must_use]
    pub fn turn(&self) -> &Ulid {
        &self.turn
    }

    /// The text of the user.
    #[must_use]
    pub fn prompt(&self) -> &PromptText {
        &self.prompt
    }

    /// The turn limit in seconds.
    #[must_use]
    pub fn deadline_s(&self) -> Seconds {
        self.deadline_s
    }

    /// The persona text of the folder.
    #[must_use]
    pub fn persona(&self) -> Option<&Persona> {
        self.persona.as_ref()
    }

    /// The file names in the session inbox.
    #[must_use]
    pub fn attachments(&self) -> &[AttachmentName] {
        &self.attachments
    }

    /// The pi entry that the turn forks from.
    #[must_use]
    pub fn branch(&self) -> Option<&Branch> {
        self.branch.as_ref()
    }

    /// The chain that the turn belongs to.
    #[must_use]
    pub fn delegation(&self) -> Option<&Delegation> {
        self.delegation.as_ref()
    }

    /// The persona that a line holds. The Python host leaves an empty one
    /// out.
    fn persona_text(&self) -> Option<&str> {
        let persona = self.persona.as_ref().map(Persona::as_str);

        persona.filter(|text| !text.is_empty())
    }

    /// The attachments that a line holds. The Python host leaves an empty
    /// list out.
    fn attachments_text(&self) -> Option<String> {
        if self.attachments.is_empty() {
            return None;
        }

        Some(array_text(
            self.attachments.iter().map(AttachmentName::as_str),
        ))
    }
}

/// `start_turn`: run a turn, and start a pi process when none is resident
/// (contract 03 §4.1).
///
/// ```
/// use creche_contracts::channel::host::{Binding, EnvEpoch, Seconds, StartTurn, TurnRequest};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// let request = TurnRequest::new(
///     "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(),
///     "Is the door locked?".parse().unwrap(),
///     Seconds::new(600),
/// );
/// let start = StartTurn::new(request, binding);
/// assert_eq!(start.address().session().as_str(), "tui-1");
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{Binding, EnvEpoch, Seconds, StartTurn, TurnRequest};
///
/// let binding = Binding::new(
///     "tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1".parse().unwrap(),
///     "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
///     EnvEpoch::new(7),
///     "reg-9f21c4".parse().unwrap(),
/// );
/// let request = TurnRequest::new(
///     "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(),
///     "Is the door locked?".parse().unwrap(),
///     Seconds::new(600),
/// );
/// let start = StartTurn { request, binding };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StartTurn {
    request: TurnRequest,
    binding: Binding,
}

impl StartTurn {
    /// The turn `request` on the session of `binding`.
    #[must_use]
    pub fn new(request: TurnRequest, binding: Binding) -> Self {
        Self { request, binding }
    }

    /// The turn, its prompt and its options.
    #[must_use]
    pub fn request(&self) -> &TurnRequest {
        &self.request
    }

    /// What the playpen needs to start the process.
    #[must_use]
    pub fn binding(&self) -> &Binding {
        &self.binding
    }

    /// The session and the turn.
    #[must_use]
    pub fn address(&self) -> TurnAddress {
        TurnAddress::new(self.binding.session.clone(), self.request.turn.clone())
    }

    fn body(&self) -> String {
        let (request, binding) = (&self.request, &self.binding);
        let writer = ObjectWriter::new()
            .text("type", HostType::StartTurn.as_str())
            .text("turn", request.turn.as_str());

        binding
            .head(writer)
            .text("prompt", request.prompt.as_str())
            .raw("deadline_s", &request.deadline_s.to_string())
            .raw("env_epoch", &binding.env_epoch.to_string())
            .text("config_rev", binding.config_rev.as_str())
            .text_if("persona", request.persona_text())
            .text_if("model", binding.model_text())
            .raw_if("attachments", request.attachments_text())
            .raw_if("workspace", binding.workspace_text())
            .raw_if("branch", request.branch.as_ref().map(Branch::encoded))
            .raw_if(
                "delegation",
                request.delegation.as_ref().map(Delegation::encoded),
            )
            .finish()
    }
}

/// `prompt`: run a turn on a process that the host believes is resident
/// (contract 03 §4.2).
///
/// The Python host has no builder for this message. It sends `start_turn`,
/// which is always correct.
///
/// ```
/// use creche_contracts::channel::host::{EnvEpoch, PromptTurn, Seconds, TurnRequest};
///
/// let request = TurnRequest::new(
///     "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(),
///     "Is the door locked?".parse().unwrap(),
///     Seconds::new(600),
/// );
/// let prompt = PromptTurn::new(request, "tui-1".parse().unwrap(), EnvEpoch::new(7));
/// assert_eq!(prompt.env_epoch(), EnvEpoch::new(7));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{EnvEpoch, PromptTurn, Seconds, TurnRequest};
///
/// let request = TurnRequest::new(
///     "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap(),
///     "Is the door locked?".parse().unwrap(),
///     Seconds::new(600),
/// );
/// let prompt = PromptTurn { request, env_epoch: EnvEpoch::new(7) };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptTurn {
    request: TurnRequest,
    session: SessionId,
    env_epoch: EnvEpoch,
}

impl PromptTurn {
    /// The turn `request` on the resident process of `session`.
    #[must_use]
    pub fn new(request: TurnRequest, session: SessionId, env_epoch: EnvEpoch) -> Self {
        Self {
            request,
            session,
            env_epoch,
        }
    }

    /// The turn, its prompt and its options.
    #[must_use]
    pub fn request(&self) -> &TurnRequest {
        &self.request
    }

    /// The session.
    #[must_use]
    pub fn session(&self) -> &SessionId {
        &self.session
    }

    /// The credential generation that the host expects.
    #[must_use]
    pub fn env_epoch(&self) -> EnvEpoch {
        self.env_epoch
    }

    /// The session and the turn.
    #[must_use]
    pub fn address(&self) -> TurnAddress {
        TurnAddress::new(self.session.clone(), self.request.turn.clone())
    }

    fn body(&self) -> String {
        let request = &self.request;

        ObjectWriter::new()
            .text("type", HostType::Prompt.as_str())
            .text("turn", request.turn.as_str())
            .text("session", self.session.as_str())
            .text("prompt", request.prompt.as_str())
            .raw("deadline_s", &request.deadline_s.to_string())
            .raw("env_epoch", &self.env_epoch.to_string())
            .text_if("persona", request.persona_text())
            .raw_if("attachments", request.attachments_text())
            .raw_if("branch", request.branch.as_ref().map(Branch::encoded))
            .raw_if(
                "delegation",
                request.delegation.as_ref().map(Delegation::encoded),
            )
            .finish()
    }
}

/// `steer`: queue a steering message into a turn that runs (contract 03
/// §4.3).
///
/// ```
/// use creche_contracts::channel::claim::TurnAddress;
/// use creche_contracts::channel::host::Steer;
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// let steer = Steer::new(address, "Check the garage too.".parse().unwrap());
/// assert_eq!(steer.message().as_str(), "Check the garage too.");
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::TurnAddress;
/// use creche_contracts::channel::host::Steer;
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// let steer = Steer { address };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Steer {
    address: TurnAddress,
    message: PromptText,
}

impl Steer {
    /// A steering message for the turn at `address`.
    #[must_use]
    pub fn new(address: TurnAddress, message: PromptText) -> Self {
        Self { address, message }
    }

    /// The session and the turn.
    #[must_use]
    pub fn address(&self) -> &TurnAddress {
        &self.address
    }

    /// The steering text.
    #[must_use]
    pub fn message(&self) -> &PromptText {
        &self.message
    }

    fn body(&self) -> String {
        ObjectWriter::new()
            .text("type", HostType::Steer.as_str())
            .text("session", self.address.session().as_str())
            .text("turn", self.address.turn().as_str())
            .text("message", self.message.as_str())
            .finish()
    }
}

/// `abort`: abort the turn of one session (contract 03 §4.4).
///
/// ```
/// use creche_contracts::channel::claim::TurnAddress;
/// use creche_contracts::channel::host::Abort;
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// assert_eq!(Abort::new(address.clone()).address(), &address);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::claim::TurnAddress;
/// use creche_contracts::channel::host::Abort;
///
/// let address = TurnAddress::new("tui-1".parse().unwrap(), "01JBQ7WZ0X4T9V6K2H8M3N5PQR".parse().unwrap());
/// let abort = Abort { address };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Abort {
    address: TurnAddress,
}

impl Abort {
    /// An abort of the turn at `address`.
    #[must_use]
    pub fn new(address: TurnAddress) -> Self {
        Self { address }
    }

    /// The session and the turn.
    #[must_use]
    pub fn address(&self) -> &TurnAddress {
        &self.address
    }

    fn body(&self) -> String {
        ObjectWriter::new()
            .text("type", HostType::Abort.as_str())
            .text("session", self.address.session().as_str())
            .text("turn", self.address.turn().as_str())
            .finish()
    }
}

/// `stop_process`: end the pi process of one session (contract 03 §4.5).
///
/// ```
/// use creche_contracts::channel::host::{Millis, StopProcess};
///
/// let stop = StopProcess::new("tui-1".parse().unwrap(), Millis::STOP_GRACE);
/// assert_eq!(stop.grace_ms(), Some(Millis::new(5000)));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{Millis, StopProcess};
///
/// let stop = StopProcess { grace_ms: Some(Millis::STOP_GRACE) };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopProcess {
    session: SessionId,
    grace_ms: Option<Millis>,
}

impl StopProcess {
    /// A stop of the process of `session`, after `grace_ms` at most. The
    /// Python host sends [`Millis::STOP_GRACE`].
    #[must_use]
    pub fn new(session: SessionId, grace_ms: Millis) -> Self {
        Self {
            session,
            grace_ms: Some(grace_ms),
        }
    }

    /// The session.
    #[must_use]
    pub fn session(&self) -> &SessionId {
        &self.session
    }

    /// How long the playpen waits for the turn to settle. A reader takes
    /// [`Millis::STOP_GRACE`] for no value.
    #[must_use]
    pub fn grace_ms(&self) -> Option<Millis> {
        self.grace_ms
    }

    fn body(&self) -> String {
        ObjectWriter::new()
            .text("type", HostType::StopProcess.as_str())
            .text("session", self.session.as_str())
            .raw_if("grace_ms", self.grace_ms.map(|grace| grace.to_string()))
            .finish()
    }
}

/// `ping`: the liveness probe (contract 03 §4.6).
///
/// ```
/// use creche_contracts::channel::host::Ping;
///
/// let ping = Ping::new("9f13".parse().unwrap());
/// assert_eq!(ping.nonce().map(|nonce| nonce.as_str()), Some("9f13"));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::Ping;
///
/// let ping = Ping { nonce: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ping {
    nonce: Option<Nonce>,
}

impl Ping {
    /// A probe with this nonce. The playpen sends the nonce back in `pong`.
    #[must_use]
    pub fn new(nonce: Nonce) -> Self {
        Self { nonce: Some(nonce) }
    }

    /// The nonce.
    #[must_use]
    pub fn nonce(&self) -> Option<&Nonce> {
        self.nonce.as_ref()
    }

    fn body(&self) -> String {
        ObjectWriter::new()
            .text("type", HostType::Ping.as_str())
            .text_if("nonce", self.nonce.as_ref().map(Nonce::as_str))
            .finish()
    }
}

/// `shutdown`: end each process and exit (contract 03 §4.6).
///
/// ```
/// use creche_contracts::channel::host::{Millis, Shutdown};
///
/// assert_eq!(Shutdown::new(Millis::STOP_GRACE).grace_ms(), Some(Millis::new(5000)));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{Millis, Shutdown};
///
/// let shutdown = Shutdown { grace_ms: Some(Millis::STOP_GRACE) };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Shutdown {
    grace_ms: Option<Millis>,
}

impl Shutdown {
    /// A shutdown after `grace_ms` at most for each session. The Python host
    /// sends [`Millis::STOP_GRACE`].
    #[must_use]
    pub fn new(grace_ms: Millis) -> Self {
        Self {
            grace_ms: Some(grace_ms),
        }
    }

    /// How long the playpen waits for each turn to settle.
    #[must_use]
    pub fn grace_ms(&self) -> Option<Millis> {
        self.grace_ms
    }

    fn body(&self) -> String {
        ObjectWriter::new()
            .text("type", HostType::Shutdown.as_str())
            .raw_if("grace_ms", self.grace_ms.map(|grace| grace.to_string()))
            .finish()
    }
}

/// One message from the host to the playpen (contract 03 §4).
///
/// The set of message types is closed. A reader refuses a line with an
/// unknown `type`. Inside a known message a reader ignores each field that it
/// does not know (§3 version rule 2).
///
/// ```
/// use creche_contracts::channel::host::{HostMessage, Ping};
///
/// let ping = HostMessage::Ping(Ping::new("9f13".parse().unwrap()));
/// let line = ping.encode().unwrap();
///
/// assert_eq!(line, "{\"type\":\"ping\",\"nonce\":\"9f13\"}\n");
/// assert_eq!(HostMessage::parse(line.trim_end()), Ok(ping));
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostMessage {
    /// `hello`.
    Hello(Hello),
    /// `open_session`.
    OpenSession(OpenSession),
    /// `start_turn`.
    StartTurn(StartTurn),
    /// `prompt`.
    Prompt(PromptTurn),
    /// `steer`.
    Steer(Steer),
    /// `abort`.
    Abort(Abort),
    /// `stop_process`.
    StopProcess(StopProcess),
    /// `get_entries`.
    GetEntries(GetEntries),
    /// `ping`.
    Ping(Ping),
    /// `shutdown`.
    Shutdown(Shutdown),
}

impl HostMessage {
    /// The type of the message.
    #[must_use]
    pub fn kind(&self) -> HostType {
        match self {
            Self::Hello(_) => HostType::Hello,
            Self::OpenSession(_) => HostType::OpenSession,
            Self::StartTurn(_) => HostType::StartTurn,
            Self::Prompt(_) => HostType::Prompt,
            Self::Steer(_) => HostType::Steer,
            Self::Abort(_) => HostType::Abort,
            Self::StopProcess(_) => HostType::StopProcess,
            Self::GetEntries(_) => HostType::GetEntries,
            Self::Ping(_) => HostType::Ping,
            Self::Shutdown(_) => HostType::Shutdown,
        }
    }

    /// The line of the message: compact JSON and one LF. The bytes are the
    /// bytes that the Python host writes for the same message.
    ///
    /// # Errors
    ///
    /// [`EncodeError::TooLarge`] when the line has more than
    /// [`MAX_LINE_BYTES`] bytes. A sender never writes such a line (§2 rule
    /// 6).
    pub fn encode(&self) -> Result<String, EncodeError> {
        self.encode_within(MAX_LINE_BYTES)
    }

    /// [`HostMessage::encode`] with another limit.
    ///
    /// # Errors
    ///
    /// [`EncodeError::TooLarge`] when the line has more than
    /// `max_line_bytes` bytes.
    pub fn encode_within(&self, max_line_bytes: usize) -> Result<String, EncodeError> {
        let body = match self {
            Self::Hello(message) => message.body(),
            Self::OpenSession(message) => message.body(),
            Self::StartTurn(message) => message.body(),
            Self::Prompt(message) => message.body(),
            Self::Steer(message) => message.body(),
            Self::Abort(message) => message.body(),
            Self::StopProcess(message) => message.body(),
            Self::GetEntries(message) => message.body(),
            Self::Ping(message) => message.body(),
            Self::Shutdown(message) => message.body(),
        };

        framed(body, max_line_bytes)
    }

    /// Reads one record from the host, in the playpen.
    ///
    /// This is a process edge. The playpen does not act on a refused line.
    /// For a refusal with an address it fails that turn with `internal`. For
    /// each other refusal it writes one `log` at level `error` (contract 03
    /// §5.3).
    ///
    /// # Errors
    ///
    /// Why the playpen does not act on the line.
    pub fn parse(text: &str) -> Result<Self, HostLineRefusal> {
        let value = json::read(text, Dialect::Strict).map_err(|_| HostLineFault::NotJson)?;
        let fields = value.into_object().ok_or(HostLineFault::NotObject)?;
        let record = Record {
            fields: &fields,
            address: None,
        };
        let kind = record.text("type").and_then(HostType::from_wire);

        match kind.ok_or(HostLineFault::UnknownType)? {
            HostType::Hello => record.hello().map(Self::Hello),
            HostType::OpenSession => record.open_session().map(Self::OpenSession),
            HostType::StartTurn => record.start_turn().map(Self::StartTurn),
            HostType::Prompt => record.prompt().map(Self::Prompt),
            HostType::Steer => record.steer().map(Self::Steer),
            HostType::Abort => record.abort().map(Self::Abort),
            HostType::StopProcess => record.stop_process().map(Self::StopProcess),
            HostType::GetEntries => record.get_entries().map(Self::GetEntries),
            HostType::Ping => record.ping().map(Self::Ping),
            HostType::Shutdown => record.shutdown().map(Self::Shutdown),
        }
    }
}

/// Which rule a line from the host breaks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostLineFault {
    /// The line is not JSON.
    NotJson,
    /// The line is JSON and not an object.
    NotObject,
    /// The line has no `type`, or a `type` that §4 does not name.
    UnknownType,
    /// This field is absent, has a wrong type or breaks its rule.
    Field(&'static str),
}

impl fmt::Display for HostLineFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotJson => f.write_str("the line is not JSON"),
            Self::NotObject => f.write_str("the line is not a JSON object"),
            Self::UnknownType => f.write_str("the type is absent or unknown"),
            Self::Field(name) => write!(f, "the field {name} is absent or not valid"),
        }
    }
}

/// Why the playpen does not act on a line from the host.
///
/// ```
/// use creche_contracts::channel::host::{HostLineFault, HostLineRefusal, HostMessage};
///
/// let refusal: HostLineRefusal = HostMessage::parse("[]").unwrap_err();
/// assert_eq!(refusal.fault(), HostLineFault::NotObject);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::host::{HostLineFault, HostLineRefusal, HostMessage};
///
/// let read = HostMessage::parse("[]");
/// let refusal = HostLineRefusal { fault: HostLineFault::NotJson, address: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostLineRefusal {
    fault: HostLineFault,
    address: Option<TurnAddress>,
}

impl HostLineRefusal {
    /// The rule that the line breaks.
    #[must_use]
    pub fn fault(&self) -> HostLineFault {
        self.fault
    }

    /// The turn that the line names, when the line names one. The playpen
    /// fails that turn, so the host does not wait for it.
    #[must_use]
    pub fn address(&self) -> Option<&TurnAddress> {
        self.address.as_ref()
    }
}

impl From<HostLineFault> for HostLineRefusal {
    fn from(fault: HostLineFault) -> Self {
        Self {
            fault,
            address: None,
        }
    }
}

impl fmt::Display for HostLineRefusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.fault.fmt(f)
    }
}

impl Error for HostLineRefusal {}

/// The fields of one line from the host, and the turn that the line names
/// when the reader knows it.
struct Record<'a> {
    fields: &'a JsonObject,
    address: Option<TurnAddress>,
}

type Read<T> = Result<T, HostLineRefusal>;

impl Record<'_> {
    fn refuse(&self, field: &'static str) -> HostLineRefusal {
        HostLineRefusal {
            fault: HostLineFault::Field(field),
            address: self.address.clone(),
        }
    }

    /// The field `key` when it is a text with no lone surrogate.
    fn text(&self, key: &str) -> Option<&str> {
        self.fields.get(key).and_then(Json::as_text)?.as_str()
    }

    /// The field `key` when it is an integer from 0 to 2^64 - 1. A number
    /// with a fraction or an exponent is no integer here, also `600.0`.
    fn number(&self, key: &str) -> Option<u64> {
        self.fields.get(key).and_then(Json::as_integer)?.to_u64()
    }

    /// Whether the field `key` is absent or `null`.
    fn absent(&self, key: &str) -> bool {
        matches!(self.fields.get(key), None | Some(Json::Null))
    }

    /// The field `key` as a value of `T`.
    fn required<T: FromStr>(&self, key: &'static str) -> Read<T> {
        let parsed = self.text(key).and_then(|text| text.parse().ok());

        parsed.ok_or_else(|| self.refuse(key))
    }

    fn epoch(&self) -> Read<EnvEpoch> {
        let epoch = self.number("env_epoch").map(EnvEpoch::new);

        epoch.ok_or_else(|| self.refuse("env_epoch"))
    }

    /// An optional grace time. A value that is no integer counts as absent.
    fn grace(&self) -> Read<Option<Millis>> {
        let Some(grace) = self.fields.get("grace_ms").and_then(Json::as_integer) else {
            return Ok(None);
        };

        match grace.to_u64() {
            Some(grace) if grace <= MAX_GRACE_MS => Ok(Some(Millis::new(grace))),
            _ => Err(self.refuse("grace_ms")),
        }
    }

    /// The object of the field `key`. `None` for an absent field.
    fn object(&self, key: &'static str) -> Read<Option<Record<'_>>> {
        if self.absent(key) {
            return Ok(None);
        }

        let fields = self.fields.get(key).and_then(Json::as_object);
        let fields = fields.ok_or_else(|| self.refuse(key))?;

        Ok(Some(Record {
            fields,
            address: self.address.clone(),
        }))
    }

    fn workspace(&self) -> Read<Option<Workspace>> {
        let Some(record) = self.object("workspace")? else {
            return Ok(None);
        };

        let kind = record.text("kind").and_then(WorkspaceKind::from_wire);
        let kind = kind.ok_or_else(|| self.refuse("workspace.kind"))?;
        let owner = record
            .text("owner_session")
            .and_then(|text| text.parse().ok());
        let owner_session = owner.ok_or_else(|| self.refuse("workspace.owner_session"))?;

        Ok(Some(Workspace {
            kind,
            owner_session,
        }))
    }

    fn branch(&self) -> Read<Option<Branch>> {
        let Some(record) = self.object("branch")? else {
            return Ok(None);
        };

        let from = record.text("fork_from").and_then(|text| text.parse().ok());

        Ok(Some(Branch::new(
            from.ok_or_else(|| self.refuse("branch.fork_from"))?,
        )))
    }

    fn delegation(&self) -> Read<Option<Delegation>> {
        let Some(record) = self.object("delegation")? else {
            return Ok(None);
        };

        let id = record.text("id").and_then(|text| text.parse().ok());
        let id = id.ok_or_else(|| self.refuse("delegation.id"))?;
        if record.absent("caller_session") {
            return Ok(Some(Delegation::new(id, None)));
        }

        let caller = record
            .text("caller_session")
            .and_then(|text| text.parse().ok());
        let caller: SessionId = caller.ok_or_else(|| self.refuse("delegation.caller_session"))?;

        Ok(Some(Delegation::new(id, Some(caller))))
    }

    fn persona(&self) -> Read<Option<Persona>> {
        if self.absent("persona") {
            return Ok(None);
        }

        self.required("persona").map(Some)
    }

    fn attachments(&self) -> Read<Vec<AttachmentName>> {
        if self.absent("attachments") {
            return Ok(Vec::new());
        }

        let items = self.fields.get("attachments").and_then(Json::as_array);
        let items = items.filter(|items| items.len() <= MAX_ATTACHMENTS);
        let items = items.ok_or_else(|| self.refuse("attachments"))?;
        let name = |item: &Json| item.as_text()?.as_str()?.parse().ok();
        let names: Option<Vec<AttachmentName>> = items.iter().map(name).collect();

        names.ok_or_else(|| self.refuse("attachments"))
    }

    /// The binding of `session`. A model that is no text, or an empty one,
    /// counts as absent.
    fn binding(&self, session: SessionId, env_epoch: EnvEpoch) -> Read<Binding> {
        Ok(Binding {
            session,
            cwd: self.required("cwd")?,
            session_dir: self.required("session_dir")?,
            env_epoch,
            config_rev: self.required("config_rev")?,
            workspace: self.workspace()?,
            model: self.text("model").and_then(|text| text.parse().ok()),
        })
    }

    /// The turn, the session, the prompt, the deadline and the epoch. The
    /// reader knows the address after the first two.
    fn turn_core(&mut self) -> Read<(Ulid, SessionId, PromptText, Seconds, EnvEpoch)> {
        let turn: Ulid = self.required("turn")?;
        let session: SessionId = self.required("session")?;
        self.address = Some(TurnAddress::new(session.clone(), turn.clone()));
        let prompt = self.required("prompt")?;
        let deadline = self.number("deadline_s");
        let deadline = deadline.filter(|seconds| (1..=MAX_DEADLINE_S).contains(seconds));
        let deadline = deadline.ok_or_else(|| self.refuse("deadline_s"))?;

        Ok((turn, session, prompt, Seconds::new(deadline), self.epoch()?))
    }

    fn turn_request(
        &self,
        turn: Ulid,
        prompt: PromptText,
        deadline_s: Seconds,
    ) -> Read<TurnRequest> {
        Ok(TurnRequest {
            turn,
            prompt,
            deadline_s,
            persona: self.persona()?,
            attachments: self.attachments()?,
            branch: self.branch()?,
            delegation: self.delegation()?,
        })
    }

    fn hello(&self) -> Read<Hello> {
        Ok(Hello {
            protocol: self.required("protocol")?,
            family: self.required("family")?,
            sandbox: self.required("sandbox")?,
            env_epoch: self.epoch()?,
            host: self.text("host").map(str::to_owned),
            max_line_bytes: self.number("max_line_bytes").map(ByteCount::new),
            coalesce_ms: self.number("coalesce_ms").map(Millis::new),
            pi_idle_ttl_s: self.number("pi_idle_ttl_s").map(Seconds::new),
            max_resident_processes: self.number("max_resident_processes").map(ProcessCap::new),
            host_deadline_s: self.number("host_deadline_s").map(Seconds::new),
            channel_idle_ttl_s: self.number("channel_idle_ttl_s").map(Seconds::new),
        })
    }

    fn open_session(&self) -> Read<OpenSession> {
        let session = self.required("session")?;
        let epoch = self.epoch()?;

        self.binding(session, epoch).map(OpenSession::new)
    }

    fn start_turn(mut self) -> Read<StartTurn> {
        let (turn, session, prompt, deadline, epoch) = self.turn_core()?;
        let binding = self.binding(session, epoch)?;
        let request = self.turn_request(turn, prompt, deadline)?;

        Ok(StartTurn { request, binding })
    }

    fn prompt(mut self) -> Read<PromptTurn> {
        let (turn, session, prompt, deadline, env_epoch) = self.turn_core()?;
        let request = self.turn_request(turn, prompt, deadline)?;

        Ok(PromptTurn {
            request,
            session,
            env_epoch,
        })
    }

    /// The session and the turn of `steer` and of `abort`.
    fn addressed(&mut self) -> Read<TurnAddress> {
        let session: SessionId = self.required("session")?;
        let turn: Ulid = self.required("turn")?;
        let address = TurnAddress::new(session, turn);
        self.address = Some(address.clone());

        Ok(address)
    }

    fn steer(mut self) -> Read<Steer> {
        let address = self.addressed()?;

        Ok(Steer {
            address,
            message: self.required("message")?,
        })
    }

    fn abort(mut self) -> Read<Abort> {
        self.addressed().map(Abort::new)
    }

    fn stop_process(&self) -> Read<StopProcess> {
        Ok(StopProcess {
            session: self.required("session")?,
            grace_ms: self.grace()?,
        })
    }

    fn get_entries(&self) -> Read<GetEntries> {
        let session = self.required("session")?;
        let request = self.required("request")?;
        let epoch = self.epoch()?;

        // A cursor that is no text counts as absent. An empty or a long one
        // is a fault.
        let since = match self.text("since") {
            Some(since) => Some(since.parse().map_err(|_| self.refuse("since"))?),
            None => None,
        };

        Ok(GetEntries {
            request,
            binding: self.binding(session, epoch)?,
            since,
        })
    }

    fn ping(&self) -> Read<Ping> {
        // A nonce that is no text counts as absent. A long one is a fault.
        let nonce = match self.text("nonce") {
            Some(nonce) => Some(nonce.parse().map_err(|_| self.refuse("nonce"))?),
            None => None,
        };

        Ok(Ping { nonce })
    }

    fn shutdown(&self) -> Read<Shutdown> {
        Ok(Shutdown {
            grace_ms: self.grace()?,
        })
    }
}

#[cfg(test)]
mod tests {
    use serde_json::{Map, Value};

    use super::*;
    use crate::vectors::{self, Marker, Outcome, Vector};

    const SURFACE: &str = "channel.build";

    const SESSION: &str = "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK";
    const TURN: &str = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";
    const OWNER: &str = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33";

    /// One vector on which the types differ from the Python host on purpose.
    struct Deviation {
        vector: &'static str,
        /// The section of contract 03 that the types hold.
        section: &'static str,
        difference: &'static str,
    }

    /// The Python builders write each object and each integer that a caller
    /// gives them. The types hold the form that the contract names, so they
    /// cannot hold the arguments of these vectors. A vector with `host` in its
    /// id holds the same message with the objects that the host makes.
    const DEVIATIONS: &[Deviation] = &[
        Deviation {
            vector: "open-session-every-argument",
            section: "§7.2",
            difference: "workspace has no kind and no owner_session",
        },
        Deviation {
            vector: "open-session-empty-model",
            section: "§7.2",
            difference: "workspace is the empty object",
        },
        Deviation {
            vector: "get-entries-every-argument",
            section: "§7.2",
            difference: "workspace has no kind and no owner_session",
        },
        Deviation {
            vector: "start-turn-every-argument",
            section: "§4.1, §7.2, §7.4",
            difference: "workspace, branch and delegation have other keys",
        },
        Deviation {
            vector: "start-turn-empty-optionals",
            section: "§4.1, §7.2",
            difference: "workspace and branch are the empty object",
        },
        Deviation {
            vector: "start-turn-numbers",
            section: "§4.1, §7.2",
            difference: "env_epoch is 2^64 and workspace holds floats",
        },
    ];

    /// Why the arguments of a vector are no value of the types.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Unbuildable {
        /// A text holds a lone surrogate. A `String` cannot hold it.
        NoUtf8,
        /// An argument is outside the type of its field.
        NotInType,
        /// No constructor stands for the builder of the vector.
        NoConstructor,
    }

    type Built<T> = Result<T, Unbuildable>;

    /// The named arguments of one builder.
    struct Args<'a>(&'a Map<String, Value>);

    impl Args<'_> {
        fn text(&self, key: &str) -> Built<Option<String>> {
            match self.0.get(key) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(text)) => Ok(Some(text.clone())),
                Some(other) => match Marker::of(other) {
                    Some(Marker::Utf16(units)) => String::from_utf16(&units)
                        .map(Some)
                        .map_err(|_| Unbuildable::NoUtf8),
                    _ => Err(Unbuildable::NotInType),
                },
            }
        }

        fn optional<T: FromStr>(&self, key: &str) -> Built<Option<T>> {
            match self.text(key)? {
                Some(text) => text.parse().map(Some).map_err(|_| Unbuildable::NotInType),
                None => Ok(None),
            }
        }

        /// An optional text that the Python builder leaves out when it is
        /// empty.
        fn truthy<T: FromStr>(&self, key: &str) -> Built<Option<T>> {
            match self.text(key)?.filter(|text| !text.is_empty()) {
                Some(text) => text.parse().map(Some).map_err(|_| Unbuildable::NotInType),
                None => Ok(None),
            }
        }

        fn required<T: FromStr>(&self, key: &str) -> Built<T> {
            self.optional(key)?.ok_or(Unbuildable::NotInType)
        }

        fn number(&self, key: &str) -> Built<Option<u64>> {
            match self.0.get(key) {
                None => Ok(None),
                Some(value) => value.as_u64().map(Some).ok_or(Unbuildable::NotInType),
            }
        }

        fn required_number(&self, key: &str) -> Built<u64> {
            self.number(key)?.ok_or(Unbuildable::NotInType)
        }

        /// The object of `key`, when it has exactly the keys `names`.
        fn object(&self, key: &str, names: &[&str]) -> Built<Option<Args<'_>>> {
            let fields = match self.0.get(key) {
                None | Some(Value::Null) => return Ok(None),
                Some(Value::Object(fields)) => fields,
                Some(_) => return Err(Unbuildable::NotInType),
            };

            let mut keys: Vec<&str> = fields.keys().map(String::as_str).collect();
            let mut names = names.to_vec();
            keys.sort_unstable();
            names.sort_unstable();
            if keys != names {
                return Err(Unbuildable::NotInType);
            }

            Ok(Some(Args(fields)))
        }

        fn workspace(&self) -> Built<Option<Workspace>> {
            let Some(fields) = self.object("workspace", &["kind", "owner_session"])? else {
                return Ok(None);
            };

            let kind: String = fields.required("kind")?;
            let kind = WorkspaceKind::from_wire(&kind).ok_or(Unbuildable::NotInType)?;

            Ok(Some(Workspace {
                kind,
                owner_session: fields.required("owner_session")?,
            }))
        }

        fn binding(&self) -> Built<Binding> {
            let mut binding = Binding::new(
                self.required("session")?,
                self.required("cwd")?,
                self.required("session_dir")?,
                EnvEpoch::new(self.required_number("epoch")?),
                self.required("config_rev")?,
            );
            if let Some(model) = self.truthy("model")? {
                binding = binding.with_model(model);
            }

            if let Some(workspace) = self.workspace()? {
                binding = binding.with_workspace(workspace);
            }

            Ok(binding)
        }

        fn address(&self) -> Built<TurnAddress> {
            Ok(TurnAddress::new(
                self.required("session")?,
                self.required("turn")?,
            ))
        }

        fn grace(&self) -> Built<Millis> {
            Ok(self
                .number("grace_ms")?
                .map_or(Millis::STOP_GRACE, Millis::new))
        }

        fn turn_request(&self) -> Built<TurnRequest> {
            let mut request = TurnRequest::new(
                self.required("turn")?,
                self.required("prompt")?,
                Seconds::new(self.required_number("deadline_s")?),
            );
            if let Some(persona) = self.optional("persona")? {
                request = request.with_persona(persona);
            }

            if let Some(Value::Array(names)) = self.0.get("attachments") {
                let name = |name: &Value| name.as_str()?.parse().ok();
                let names: Option<Vec<AttachmentName>> = names.iter().map(name).collect();

                request = request.with_attachments(names.ok_or(Unbuildable::NotInType)?);
            }

            if let Some(branch) = self.object("branch", &["fork_from"])? {
                request = request.with_branch(Branch::new(branch.required("fork_from")?));
            }

            if let Some(fields) = self.object("delegation", &["caller_session", "id"])? {
                let delegation =
                    Delegation::new(fields.required("id")?, fields.optional("caller_session")?);

                request = request.with_delegation(delegation);
            }

            Ok(request)
        }
    }

    /// Calls the constructor that stands for one Python builder.
    fn build(builder: &str, args: &Args<'_>) -> Built<HostMessage> {
        Ok(match builder {
            "hello" => HostMessage::Hello(Hello::new(
                args.required("family")?,
                args.required("sandbox")?,
                EnvEpoch::new(args.required_number("epoch")?),
                Seconds::new(args.required_number("pi_idle_ttl_s")?),
                args.number("max_resident_processes")?
                    .map_or(ProcessCap::DEFAULT, ProcessCap::new),
                Seconds::new(args.number("channel_idle_ttl_s")?.unwrap_or(0)),
            )),
            "open_session" => HostMessage::OpenSession(OpenSession::new(args.binding()?)),
            "get_entries" => {
                let read = GetEntries::new(args.required("request")?, args.binding()?);

                HostMessage::GetEntries(match args.truthy("since")? {
                    Some(since) => read.with_since(since),
                    None => read,
                })
            }
            "start_turn" => {
                HostMessage::StartTurn(StartTurn::new(args.turn_request()?, args.binding()?))
            }
            "steer" => HostMessage::Steer(Steer::new(args.address()?, args.required("message")?)),
            "abort" => HostMessage::Abort(Abort::new(args.address()?)),
            "stop_process" => {
                HostMessage::StopProcess(StopProcess::new(args.required("session")?, args.grace()?))
            }
            "ping" => HostMessage::Ping(Ping::new(args.required("nonce")?)),
            "shutdown" => HostMessage::Shutdown(Shutdown::new(args.grace()?)),
            _ => return Err(Unbuildable::NoConstructor),
        })
    }

    /// What one vector gives: the built message and the limit of its line.
    fn replay(vector: &Vector) -> (Built<HostMessage>, usize) {
        let id = &vector.id;
        let params = vector
            .field("params")
            .unwrap_or_else(|| panic!("{id}: no params"));
        let builder = params["builder"].as_str().unwrap();
        let limit = usize::try_from(params["max_line_bytes"].as_u64().unwrap()).unwrap();
        let args = vector
            .input
            .args()
            .unwrap_or_else(|| panic!("{id}: no args"));

        (build(builder, &Args(args)), limit)
    }

    #[test]
    fn each_host_line_has_the_bytes_of_the_python_host() {
        let surface = vectors::surface(SURFACE);
        let mut equal = 0;
        let mut differed = Vec::new();

        for vector in &surface.vectors {
            let id = vector.id.as_str();
            let (built, limit) = replay(vector);
            let line = built.map(|message| message.encode_within(limit));

            match (vector.result, line) {
                (Outcome::Accepted, Ok(Ok(line))) => {
                    let output = vector
                        .field("output")
                        .unwrap_or_else(|| panic!("{id}: no output"));

                    assert_eq!(output["text"].as_str(), Some(line.as_str()), "{id}");
                    equal += 1;
                }
                (Outcome::Accepted, Err(Unbuildable::NotInType)) => differed.push(id),
                (Outcome::Refused, Ok(Err(error))) => {
                    let refusal = vector.refusal().unwrap();

                    assert_eq!(
                        refusal["message"].as_str(),
                        Some(error.to_string().as_str()),
                        "{id}"
                    );
                }
                (Outcome::Refused, Err(Unbuildable::NoUtf8)) => {
                    let refusal = vector.refusal().unwrap();

                    assert_eq!(refusal["exception"], "UnicodeEncodeError", "{id}");
                }
                // The Python host raised. The types refuse.
                (Outcome::Raised, Ok(Err(_)) | Err(_)) => {}
                (result, line) => panic!("{id}: the Python host {result:?}, the types {line:?}"),
            }
        }

        let listed: Vec<&str> = DEVIATIONS.iter().map(|row| row.vector).collect();

        assert_eq!(differed, listed, "the vectors that differ and the rows");
        assert!(equal > 0, "{SURFACE} holds no accepted vector");
        for row in DEVIATIONS {
            assert!(row.section.starts_with('§'), "{}", row.vector);
            assert!(!row.difference.is_empty(), "{}", row.vector);
        }
    }

    fn binding() -> Binding {
        Binding::new(
            SESSION.parse().unwrap(),
            "/srv/sessions/chat/tui-1".parse().unwrap(),
            "/srv/sessions/chat/tui-1/pi".parse().unwrap(),
            EnvEpoch::new(7),
            "reg-9f21c4".parse().unwrap(),
        )
    }

    fn address() -> TurnAddress {
        TurnAddress::new(SESSION.parse().unwrap(), TURN.parse().unwrap())
    }

    fn request() -> TurnRequest {
        TurnRequest::new(
            TURN.parse().unwrap(),
            "Which sensor dropped out?".parse().unwrap(),
            Seconds::new(600),
        )
    }

    fn full_request() -> TurnRequest {
        request()
            .with_persona("You answer as the house assistant.".parse().unwrap())
            .with_attachments(vec!["notes.txt".parse().unwrap()])
            .with_branch(Branch::new("e4".parse().unwrap()))
            .with_delegation(Delegation::new(
                "01JBQ7WZ0X4T9V6K2H8M3N5PQS".parse().unwrap(),
                Some(OWNER.parse().unwrap()),
            ))
    }

    fn full_binding() -> Binding {
        binding()
            .with_model("code-router".parse().unwrap())
            .with_workspace(Workspace::code_sandbox(OWNER.parse().unwrap()))
    }

    /// One message of each type, with each optional field and with none.
    fn messages() -> Vec<HostMessage> {
        vec![
            HostMessage::Hello(Hello::new(
                "chat".parse().unwrap(),
                "chat-s3".parse().unwrap(),
                EnvEpoch::new(7),
                Seconds::new(900),
                ProcessCap::DEFAULT,
                Seconds::new(0),
            )),
            HostMessage::OpenSession(OpenSession::new(binding())),
            HostMessage::OpenSession(OpenSession::new(full_binding())),
            HostMessage::StartTurn(StartTurn::new(request(), binding())),
            HostMessage::StartTurn(StartTurn::new(full_request(), full_binding())),
            HostMessage::Prompt(PromptTurn::new(
                request(),
                SESSION.parse().unwrap(),
                EnvEpoch::new(7),
            )),
            HostMessage::Prompt(PromptTurn::new(
                full_request(),
                SESSION.parse().unwrap(),
                EnvEpoch::new(7),
            )),
            HostMessage::Steer(Steer::new(
                address(),
                "Check the garage too.".parse().unwrap(),
            )),
            HostMessage::Abort(Abort::new(address())),
            HostMessage::StopProcess(StopProcess::new(
                SESSION.parse().unwrap(),
                Millis::STOP_GRACE,
            )),
            HostMessage::GetEntries(GetEntries::new(TURN.parse().unwrap(), binding())),
            HostMessage::GetEntries(
                GetEntries::new(TURN.parse().unwrap(), full_binding())
                    .with_since("e4".parse().unwrap()),
            ),
            HostMessage::Ping(Ping::new("9f13".parse().unwrap())),
            HostMessage::Shutdown(Shutdown::new(Millis::STOP_GRACE)),
        ]
    }

    #[test]
    fn the_playpen_reads_each_line_that_the_host_writes() {
        let mut kinds = Vec::new();
        for message in messages() {
            let line = message.encode().unwrap();
            let record = line.strip_suffix('\n').unwrap();

            assert!(!record.contains('\n'), "{record}");
            assert_eq!(
                HostMessage::parse(record).as_ref(),
                Ok(&message),
                "{record}"
            );
            kinds.push(message.kind());
        }

        kinds.dedup();
        assert_eq!(kinds.len(), HostType::ALL.len());
        for kind in HostType::ALL {
            assert!(kinds.contains(kind), "{kind}");
        }
    }

    #[test]
    fn a_line_has_the_keys_of_the_contract_in_the_order_of_the_host() {
        let prompt = HostMessage::Prompt(PromptTurn::new(
            full_request(),
            SESSION.parse().unwrap(),
            EnvEpoch::new(7),
        ));
        let read = HostMessage::GetEntries(
            GetEntries::new(TURN.parse().unwrap(), full_binding())
                .with_since("e4".parse().unwrap()),
        );

        assert_eq!(
            prompt.encode().unwrap(),
            format!(
                "{{\"type\":\"prompt\",\"turn\":\"{TURN}\",\"session\":\"{SESSION}\",\
                 \"prompt\":\"Which sensor dropped out?\",\"deadline_s\":600,\"env_epoch\":7,\
                 \"persona\":\"You answer as the house assistant.\",\
                 \"attachments\":[\"notes.txt\"],\"branch\":{{\"fork_from\":\"e4\"}},\
                 \"delegation\":{{\"id\":\"01JBQ7WZ0X4T9V6K2H8M3N5PQS\",\
                 \"caller_session\":\"{OWNER}\"}}}}\n"
            )
        );
        assert_eq!(
            read.encode().unwrap(),
            format!(
                "{{\"type\":\"get_entries\",\"request\":\"{TURN}\",\"session\":\"{SESSION}\",\
                 \"cwd\":\"/srv/sessions/chat/tui-1\",\
                 \"session_dir\":\"/srv/sessions/chat/tui-1/pi\",\"env_epoch\":7,\
                 \"config_rev\":\"reg-9f21c4\",\"since\":\"e4\",\"model\":\"code-router\",\
                 \"workspace\":{{\"kind\":\"code-sandbox\",\"owner_session\":\"{OWNER}\"}}}}\n"
            )
        );
    }

    #[test]
    fn a_line_over_the_limit_is_not_written() {
        let prompt: PromptText = "a".repeat(MAX_PROMPT_BYTES).parse().unwrap();
        let request = || TurnRequest::new(TURN.parse().unwrap(), prompt.clone(), Seconds::new(60));
        let steer = |count: usize| {
            let message = "\u{0}".repeat(count).parse().unwrap();

            HostMessage::Steer(Steer::new(address(), message)).encode()
        };

        assert!(
            HostMessage::StartTurn(StartTurn::new(request(), binding()))
                .encode()
                .is_ok()
        );

        // A NUL is six bytes on the wire, so a text under its own cap can
        // make a line over the cap of a line.
        assert!(steer(174_000).is_ok());
        assert!(matches!(
            steer(175_000),
            Err(EncodeError::TooLarge {
                max_line_bytes: MAX_LINE_BYTES,
                ..
            })
        ));
    }

    /// A `start_turn` record with one more field.
    fn start_turn(extra: &str) -> String {
        format!(
            "{{\"type\":\"start_turn\",\"turn\":\"{TURN}\",\"session\":\"{SESSION}\",\
             \"cwd\":\"/w\",\"session_dir\":\"/w/pi\",\"prompt\":\"p\",\"deadline_s\":600,\
             \"env_epoch\":7,\"config_rev\":\"r1\"{extra}}}"
        )
    }

    fn fault(record: &str) -> (HostLineFault, bool) {
        let refusal = HostMessage::parse(record).expect_err(record);

        assert_eq!(refusal.to_string(), refusal.fault().to_string());
        if let Some(named) = refusal.address() {
            assert_eq!(named, &address(), "{record}");
        }

        (refusal.fault(), refusal.address().is_some())
    }

    #[test]
    fn the_playpen_refuses_a_line_that_is_no_message() {
        let refused = [
            ("", HostLineFault::NotJson),
            ("{", HostLineFault::NotJson),
            ("{\"type\":\"ping\",\"n\":NaN}", HostLineFault::NotJson),
            ("\u{feff}{\"type\":\"ping\"}", HostLineFault::NotJson),
            ("[]", HostLineFault::NotObject),
            ("\"ping\"", HostLineFault::NotObject),
            ("{}", HostLineFault::UnknownType),
            ("{\"type\":5}", HostLineFault::UnknownType),
            ("{\"type\":\"pong\"}", HostLineFault::UnknownType),
            ("{\"type\":\"PING\"}", HostLineFault::UnknownType),
            ("{\"type\":\"ping\\n\"}", HostLineFault::UnknownType),
        ];

        for (record, expected) in refused {
            assert_eq!(fault(record), (expected, false), "{record:?}");
        }

        let deep = format!("{{\"type\":\"ping\",\"x\":{}", "[".repeat(400_000));
        assert_eq!(fault(&deep), (HostLineFault::NotJson, false));
    }

    #[test]
    fn the_playpen_refuses_a_bad_field_and_names_the_turn_when_it_knows_it() {
        let field = HostLineFault::Field;
        let long = "a".repeat(MAX_NAME_BYTES + 1);
        let many = vec!["\"a\""; MAX_ATTACHMENTS + 1].join(",");
        let refused = [
            // Before the reader knows the turn.
            (start_turn(",\"turn\":\"not a ulid\""), field("turn"), false),
            (start_turn(",\"session\":\"../x\""), field("session"), false),
            (start_turn(",\"session\":7"), field("session"), false),
            // After.
            (start_turn(",\"prompt\":7"), field("prompt"), true),
            (start_turn(",\"deadline_s\":0"), field("deadline_s"), true),
            (
                start_turn(",\"deadline_s\":86401"),
                field("deadline_s"),
                true,
            ),
            (
                start_turn(",\"deadline_s\":600.0"),
                field("deadline_s"),
                true,
            ),
            (
                start_turn(",\"deadline_s\":\"600\""),
                field("deadline_s"),
                true,
            ),
            (start_turn(",\"env_epoch\":-1"), field("env_epoch"), true),
            (
                start_turn(",\"env_epoch\":18446744073709551616"),
                field("env_epoch"),
                true,
            ),
            (start_turn(",\"cwd\":\"w\""), field("cwd"), true),
            (start_turn(",\"cwd\":\"/w/../x\""), field("cwd"), true),
            (
                start_turn(",\"session_dir\":\"/w\\u0000\""),
                field("session_dir"),
                true,
            ),
            (
                start_turn(",\"config_rev\":\"\""),
                field("config_rev"),
                true,
            ),
            (
                start_turn(&format!(",\"config_rev\":\"{long}\"")),
                field("config_rev"),
                true,
            ),
            (start_turn(",\"workspace\":[]"), field("workspace"), true),
            (
                start_turn(",\"workspace\":{\"kind\":\"code-sandbox\"}"),
                field("workspace.owner_session"),
                true,
            ),
            (
                start_turn(",\"workspace\":{\"kind\":\"other\",\"owner_session\":\"s\"}"),
                field("workspace.kind"),
                true,
            ),
            (
                start_turn(",\"workspace\":{\"kind\":\"code-sandbox\",\"owner_session\":\"..\"}"),
                field("workspace.owner_session"),
                true,
            ),
            (start_turn(",\"persona\":7"), field("persona"), true),
            (
                start_turn(&format!(
                    ",\"persona\":\"{}\"",
                    "a".repeat(MAX_PERSONA_BYTES + 1)
                )),
                field("persona"),
                true,
            ),
            (
                start_turn(",\"attachments\":\"notes.txt\""),
                field("attachments"),
                true,
            ),
            (
                start_turn(",\"attachments\":[\"..\"]"),
                field("attachments"),
                true,
            ),
            (
                start_turn(",\"attachments\":[7]"),
                field("attachments"),
                true,
            ),
            (
                start_turn(&format!(",\"attachments\":[{many}]")),
                field("attachments"),
                true,
            ),
            (start_turn(",\"branch\":\"e4\""), field("branch"), true),
            (
                start_turn(",\"branch\":{}"),
                field("branch.fork_from"),
                true,
            ),
            (
                start_turn(",\"branch\":{\"fork_from\":\"\"}"),
                field("branch.fork_from"),
                true,
            ),
            (start_turn(",\"delegation\":7"), field("delegation"), true),
            (
                start_turn(",\"delegation\":{\"id\":\"x\"}"),
                field("delegation.id"),
                true,
            ),
            (
                start_turn(&format!(
                    ",\"delegation\":{{\"id\":\"{TURN}\",\"caller_session\":\"/\"}}"
                )),
                field("delegation.caller_session"),
                true,
            ),
        ];

        for (record, expected, addressed) in refused {
            assert_eq!(fault(&record), (expected, addressed), "{record}");
        }
    }

    #[test]
    fn the_playpen_refuses_a_bad_field_of_each_other_message() {
        let field = HostLineFault::Field;
        let long = "a".repeat(MAX_NAME_BYTES + 1);
        let steer = format!("{{\"type\":\"steer\",\"session\":\"{SESSION}\",\"turn\":\"{TURN}\"");
        let open = format!(
            "{{\"type\":\"open_session\",\"session\":\"{SESSION}\",\"cwd\":\"/w\",\
             \"session_dir\":\"/w/pi\",\"config_rev\":\"r1\""
        );
        let read = format!("{open},\"type\":\"get_entries\",\"env_epoch\":7");
        let refused = [
            ("{\"type\":\"hello\"}".to_owned(), field("protocol"), false),
            (
                "{\"type\":\"hello\",\"protocol\":\"1\",\"family\":\"chat\"}".to_owned(),
                field("protocol"),
                false,
            ),
            (
                "{\"type\":\"hello\",\"protocol\":\"1.0\",\"family\":\"Chat\"}".to_owned(),
                field("family"),
                false,
            ),
            (
                "{\"type\":\"hello\",\"protocol\":\"1.0\",\"family\":\"chat\",\"sandbox\":\"chat\"}"
                    .to_owned(),
                field("sandbox"),
                false,
            ),
            (
                "{\"type\":\"hello\",\"protocol\":\"1.0\",\"family\":\"chat\",\
                 \"sandbox\":\"chat-s3\"}"
                    .to_owned(),
                field("env_epoch"),
                false,
            ),
            (format!("{open}}}"), field("env_epoch"), false),
            (format!("{open},\"env_epoch\":7,\"cwd\":7}}"), field("cwd"), false),
            (format!("{read}}}"), field("request"), false),
            (format!("{read},\"request\":\"{TURN}\",\"since\":\"\"}}"), field("since"), false),
            (
                format!("{read},\"request\":\"{TURN}\",\"since\":\"{long}\"}}"),
                field("since"),
                false,
            ),
            (format!("{steer}}}"), field("message"), true),
            (format!("{steer},\"message\":7}}"), field("message"), true),
            (format!("{steer},\"turn\":\"x\",\"message\":\"m\"}}"), field("turn"), false),
            ("{\"type\":\"abort\",\"turn\":\"x\"}".to_owned(), field("session"), false),
            (
                format!("{{\"type\":\"abort\",\"session\":\"{SESSION}\"}}"),
                field("turn"),
                false,
            ),
            ("{\"type\":\"stop_process\"}".to_owned(), field("session"), false),
            (
                format!("{{\"type\":\"stop_process\",\"session\":\"{SESSION}\",\"grace_ms\":-1}}"),
                field("grace_ms"),
                false,
            ),
            ("{\"type\":\"shutdown\",\"grace_ms\":600001}".to_owned(), field("grace_ms"), false),
            (format!("{{\"type\":\"ping\",\"nonce\":\"{long}\"}}"), field("nonce"), false),
        ];

        for (record, expected, addressed) in refused {
            assert_eq!(fault(&record), (expected, addressed), "{record}");
        }
    }

    #[test]
    fn the_playpen_ignores_a_field_that_it_does_not_know() {
        let hello = HostMessage::parse(
            "{\"type\":\"hello\",\"protocol\":\"1.7\",\"family\":\"chat\",\"sandbox\":\"chat-s3\",\
             \"env_epoch\":7,\"max_line_bytes\":\"large\",\"coalesce_ms\":-5,\
             \"pi_idle_ttl_s\":1.5,\"host\":7,\"future\":{\"a\":[1,2]}}",
        );
        let Ok(HostMessage::Hello(hello)) = hello else {
            panic!("{hello:?}");
        };
        let lenient = [
            (
                "{\"type\":\"ping\"}",
                HostMessage::Ping(Ping { nonce: None }),
            ),
            (
                "{\"type\":\"ping\",\"nonce\":7}",
                HostMessage::Ping(Ping { nonce: None }),
            ),
            (
                "{\"type\":\"shutdown\"}",
                HostMessage::Shutdown(Shutdown { grace_ms: None }),
            ),
            (
                "{\"type\":\"shutdown\",\"grace_ms\":\"5000\",\"later\":true}",
                HostMessage::Shutdown(Shutdown { grace_ms: None }),
            ),
            (
                "{\"type\":\"shutdown\",\"grace_ms\":0}",
                HostMessage::Shutdown(Shutdown::new(Millis::new(0))),
            ),
        ];

        assert_eq!(hello.protocol().major(), 1);
        assert_eq!(hello.protocol().minor(), 7);
        assert_eq!(hello.family().as_str(), "chat");
        assert_eq!(hello.sandbox().as_str(), "chat-s3");
        assert_eq!(hello.env_epoch(), EnvEpoch::new(7));
        assert_eq!(hello.host(), None);
        assert_eq!(hello.max_line_bytes(), None);
        assert_eq!(hello.coalesce_ms(), None);
        assert_eq!(hello.pi_idle_ttl_s(), None);
        assert_eq!(hello.max_resident_processes(), None);
        assert_eq!(hello.host_deadline_s(), None);
        assert_eq!(hello.channel_idle_ttl_s(), None);
        for (record, message) in lenient {
            assert_eq!(HostMessage::parse(record), Ok(message), "{record}");
        }
    }

    #[test]
    fn the_playpen_reads_the_optional_fields_of_a_turn() {
        let null = start_turn(
            ",\"persona\":null,\"attachments\":null,\"workspace\":null,\"branch\":null,\
             \"delegation\":null,\"model\":null",
        );
        let no_caller = start_turn(&format!(
            ",\"delegation\":{{\"id\":\"{TURN}\"}},\"model\":\"\""
        ));
        let Ok(HostMessage::StartTurn(plain)) = HostMessage::parse(&null) else {
            panic!("{null}");
        };
        let Ok(HostMessage::StartTurn(chained)) = HostMessage::parse(&no_caller) else {
            panic!("{no_caller}");
        };

        assert_eq!(plain.address(), address());
        assert_eq!(plain.request().turn().as_str(), TURN);
        assert_eq!(plain.request().prompt().as_str(), "p");
        assert_eq!(plain.request().deadline_s(), Seconds::new(600));
        assert_eq!(plain.request().persona(), None);
        assert!(plain.request().attachments().is_empty());
        assert_eq!(plain.request().branch(), None);
        assert_eq!(plain.request().delegation(), None);
        assert_eq!(plain.binding().session().as_str(), SESSION);
        assert_eq!(plain.binding().cwd().as_str(), "/w");
        assert_eq!(plain.binding().session_dir().as_str(), "/w/pi");
        assert_eq!(plain.binding().env_epoch(), EnvEpoch::new(7));
        assert_eq!(plain.binding().config_rev().as_str(), "r1");
        assert_eq!(plain.binding().model(), None);
        assert_eq!(plain.binding().workspace(), None);
        assert_eq!(chained.binding().model(), None);
        assert_eq!(chained.request().delegation().unwrap().id().as_str(), TURN);
        assert_eq!(
            chained.request().delegation().unwrap().caller_session(),
            None
        );
    }

    #[test]
    fn each_message_gives_its_fields() {
        let full = StartTurn::new(full_request(), full_binding());
        let prompt = PromptTurn::new(request(), SESSION.parse().unwrap(), EnvEpoch::new(7));
        let read =
            GetEntries::new(TURN.parse().unwrap(), binding()).with_since("e4".parse().unwrap());
        let steer = Steer::new(address(), "m".parse().unwrap());
        let workspace = full.binding().workspace().unwrap();
        let delegation = full.request().delegation().unwrap();

        assert_eq!(workspace.kind(), WorkspaceKind::CodeSandbox);
        assert_eq!(workspace.owner_session().as_str(), OWNER);
        assert_eq!(full.binding().model().unwrap().as_str(), "code-router");
        assert_eq!(full.request().persona().unwrap().as_str().len(), 34);
        assert_eq!(full.request().attachments()[0].as_str(), "notes.txt");
        assert_eq!(full.request().branch().unwrap().fork_from().as_str(), "e4");
        assert_eq!(delegation.caller_session().unwrap().as_str(), OWNER);
        assert_eq!(prompt.address(), address());
        assert_eq!(prompt.session().as_str(), SESSION);
        assert_eq!(prompt.env_epoch().get(), 7);
        assert_eq!(prompt.request(), &request());
        assert_eq!(read.request().as_str(), TURN);
        assert_eq!(read.since().unwrap().as_str(), "e4");
        assert_eq!(read.binding(), &binding());
        assert_eq!(steer.address(), &address());
        assert_eq!(steer.message().as_str(), "m");
        assert_eq!(Abort::new(address()).address(), &address());
        assert_eq!(OpenSession::new(binding()).binding(), &binding());
        assert_eq!(
            StopProcess::new(SESSION.parse().unwrap(), Millis::new(1)).grace_ms(),
            Some(Millis::new(1))
        );
        assert_eq!(
            StopProcess::new(SESSION.parse().unwrap(), Millis::new(1))
                .session()
                .as_str(),
            SESSION
        );
        assert_eq!(
            Ping::new("n".parse().unwrap()).nonce().unwrap().as_str(),
            "n"
        );
        assert_eq!(
            Shutdown::new(Millis::STOP_GRACE).grace_ms(),
            Some(Millis::new(5000))
        );
    }

    #[test]
    fn a_sandbox_path_is_absolute_and_stays_in_its_mount() {
        let accepted = ["/", "/w", "/srv/a.b/..c/d..", "/w/", "//w", "/w/caf\u{e9}"];
        let refused = [
            ("", SandboxPathError::NotAbsolute),
            ("w", SandboxPathError::NotAbsolute),
            ("./w", SandboxPathError::NotAbsolute),
            ("\n/w", SandboxPathError::NotAbsolute),
            ("/w/..", SandboxPathError::ParentSegment),
            ("/../w", SandboxPathError::ParentSegment),
            ("/w/../x", SandboxPathError::ParentSegment),
            ("/w\0", SandboxPathError::Nul),
        ];
        let long = format!("/{}", "a".repeat(MAX_PATH_BYTES));

        for text in accepted {
            assert_eq!(text.parse::<SandboxPath>().unwrap().as_str(), text);
            assert!(SandboxPath::try_from(text.to_owned()).is_ok());
        }

        for (text, error) in refused {
            assert_eq!(text.parse::<SandboxPath>(), Err(error), "{text:?}");
            assert!(!error.to_string().is_empty());
        }

        assert_eq!(long.parse::<SandboxPath>(), Err(SandboxPathError::TooLong));
        assert!(SandboxPathError::TooLong.to_string().contains("4096"));
        assert!(
            long.strip_suffix('a')
                .unwrap()
                .parse::<SandboxPath>()
                .is_ok()
        );
    }

    #[test]
    fn a_protocol_version_is_two_numbers() {
        let accepted = [
            ("1.0", 1, 0),
            ("0.0", 0, 0),
            ("12.345", 12, 345),
            ("999999999.1", 999_999_999, 1),
        ];
        let refused = [
            ("", ProtocolVersionError::NoDot),
            ("1", ProtocolVersionError::NoDot),
            ("1.", ProtocolVersionError::BadNumber),
            (".0", ProtocolVersionError::BadNumber),
            ("1.0.0", ProtocolVersionError::BadNumber),
            ("01.0", ProtocolVersionError::BadNumber),
            ("1.00", ProtocolVersionError::BadNumber),
            ("1.0\n", ProtocolVersionError::BadNumber),
            (" 1.0", ProtocolVersionError::BadNumber),
            ("+1.0", ProtocolVersionError::BadNumber),
            ("1.-0", ProtocolVersionError::BadNumber),
            ("a.b", ProtocolVersionError::BadNumber),
            ("\u{0661}.0", ProtocolVersionError::BadNumber),
            ("1.\u{ff10}", ProtocolVersionError::BadNumber),
            ("1000000000.0", ProtocolVersionError::BadNumber),
        ];

        for (text, major, minor) in accepted {
            let version: ProtocolVersion = text.parse().unwrap();

            assert_eq!((version.major(), version.minor()), (major, minor));
            assert_eq!(version.to_string(), text);
        }

        for (text, error) in refused {
            assert_eq!(text.parse::<ProtocolVersion>(), Err(error), "{text:?}");
            assert!(!error.to_string().is_empty());
        }
    }

    /// The bounds of one sized text: the least and the most bytes.
    fn bounds<T>(min: usize, max: usize)
    where
        T: FromStr + TryFrom<String> + fmt::Debug,
        <T as FromStr>::Err: fmt::Display + fmt::Debug,
    {
        let least = "a".repeat(min);
        let most = "a".repeat(max);

        assert!(least.parse::<T>().is_ok());
        assert!(most.parse::<T>().is_ok());
        assert!(T::try_from(most.clone()).is_ok());
        assert!(T::try_from(format!("{most}a")).is_err());
        assert!(
            format!("{most}a")
                .parse::<T>()
                .unwrap_err()
                .to_string()
                .contains(&max.to_string())
        );
        if min > 0 {
            assert!(
                "".parse::<T>()
                    .unwrap_err()
                    .to_string()
                    .contains(&min.to_string())
            );
        }

        // The cap counts bytes, and a text can hold each character.
        assert!("\u{e9}".repeat(max / 2).parse::<T>().is_ok());
        assert!("\u{e9}".repeat(max / 2 + 1).parse::<T>().is_err());
        assert!("a\n\u{0661}".parse::<T>().is_ok());
    }

    #[test]
    fn each_sized_text_has_its_bounds() {
        bounds::<ConfigRev>(1, MAX_NAME_BYTES);
        bounds::<EntryId>(1, MAX_NAME_BYTES);
        bounds::<Model>(1, MAX_NAME_BYTES);
        bounds::<Nonce>(0, MAX_NAME_BYTES);
        bounds::<PromptText>(0, MAX_PROMPT_BYTES);
        bounds::<Persona>(0, MAX_PERSONA_BYTES);

        assert_eq!("".parse::<ConfigRev>(), Err(ConfigRevError::TooShort));
        assert_eq!("".parse::<EntryId>(), Err(EntryIdError::TooShort));
        assert_eq!("".parse::<Model>(), Err(ModelError::TooShort));
        assert_eq!("a".repeat(201).parse::<Nonce>(), Err(NonceError::TooLong));
        assert_eq!(
            "a".repeat(262_145).parse::<PromptText>(),
            Err(PromptTextError::TooLong)
        );
        assert_eq!(
            "a".repeat(16_385).parse::<Persona>(),
            Err(PersonaError::TooLong)
        );
    }

    #[test]
    fn each_number_keeps_its_value() {
        assert_eq!(EnvEpoch::from(7).get(), 7);
        assert_eq!(Seconds::from(90), Seconds::HOST_DEADLINE);
        assert_eq!(Millis::from(50), Millis::COALESCE);
        assert_eq!(ProcessCap::from(12).to_string(), "12");
        assert_eq!(ByteCount::from(1_048_576), ByteCount::LINE_LIMIT);
        assert_eq!(
            usize::try_from(ByteCount::LINE_LIMIT.get()),
            Ok(MAX_LINE_BYTES)
        );
        assert!(Seconds::new(1) < Seconds::new(2));
    }
}
