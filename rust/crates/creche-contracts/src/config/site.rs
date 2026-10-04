//! The site file: `/etc/creche/site.env`.
//!
//! The site file holds what differs between two deployments: the owner of
//! the repositories, the account of the operator, the home of that account
//! and the LAN address of the host. It holds no secret.
//!
//! [`SiteFile`] is the raw form: each `KEY=VALUE` line. [`Site`] is the
//! valid form: the four values that root's executor needs, each one a type.
//! The Python reader is `handover.site`.
//!
//! systemd also reads the file, as the `EnvironmentFile` of each unit. A
//! daemon gets the values through its environment and parses them with its
//! own config type.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::pytext;
use super::values::{LanAddress, LanAddressError};
use super::{AtReload, AtStart, Checked, FailureAction};

/// Where a deployment writes its values.
pub const SITE_FILE: &str = "/etc/creche/site.env";

/// The largest count of bytes in a site file.
pub const MAX_SITE_BYTES: usize = 64 * 1024;

/// What starts a comment line.
const COMMENT: char = '#';

/// What separates a key from its value.
const ASSIGN: char = '=';

/// The two quote characters that systemd removes from a value.
const QUOTES: [char; 2] = ['"', '\''];

/// The write bits of the group and of each other account.
const OTHERS_WRITE: u32 = 0o022;

/// The uid of root.
const ROOT_UID: u32 = 0;

/// The one account that the operator cannot be.
const ROOT_USER: &str = "root";

/// The start of the key that gives one repository its name on GitHub. The
/// rest of the key is the name of the catalog in upper case, with `_` for
/// `-`.
const REPO_KEY_PREFIX: &str = "AGENT_GITHUB_REPO_";

/// A key of the site file that `handover.site` reads.
///
/// The set is closed for this reader. The reader ignores each other key of
/// the file, so a newer file with one more key is valid.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum SiteKey {
    /// `AGENT_GITHUB_OWNER`: the owner of the repositories of a release.
    GithubOwner,
    /// `AGENT_OPERATOR_USER`: the account that the user units run as.
    OperatorUser,
    /// `AGENT_OPERATOR_HOME`: the home of that account.
    OperatorHome,
    /// `AGENT_LAN_ADDRESS`: the LAN address of the host.
    LanAddress,
}

impl SiteKey {
    /// Each key, in the order of the file that [`Site::to_text`] writes.
    pub const ALL: [Self; 4] = [
        Self::GithubOwner,
        Self::OperatorUser,
        Self::OperatorHome,
        Self::LanAddress,
    ];

    /// The key as the file writes it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::GithubOwner => "AGENT_GITHUB_OWNER",
            Self::OperatorUser => "AGENT_OPERATOR_USER",
            Self::OperatorHome => "AGENT_OPERATOR_HOME",
            Self::LanAddress => super::LAN_ADDRESS,
        }
    }
}

impl fmt::Display for SiteKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// --- the raw form ---

/// Each `KEY=VALUE` line of a site file. The last line of a key wins.
///
/// The parse follows `handover.site`:
///
/// 1. The file is UTF-8 and has [`MAX_SITE_BYTES`] bytes or less.
/// 2. A line that is empty, and a line that starts with `#`, is ignored.
/// 3. Each other line holds `=`. The key is the text before the first `=`.
/// 4. The space at the two ends of the key and of the value is removed.
/// 5. One pair of quotes at the two ends of the value is removed.
///
/// A value is raw text. The typed readers, for example
/// [`SiteFile::lan_address`], check it.
///
/// ```
/// use creche_contracts::config::site::SiteFile;
///
/// let file = SiteFile::parse(b"# the host\nAGENT_LAN_ADDRESS=\"192.0.2.10\"\n")?;
/// assert_eq!(file.lan_address()?.as_str(), "192.0.2.10");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from a raw map:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::site::SiteFile;
///
/// let file = SiteFile(std::collections::BTreeMap::new());
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SiteFile(BTreeMap<String, String>);

impl SiteFile {
    /// Parses the bytes of a site file.
    ///
    /// # Errors
    ///
    /// [`SiteFileError`] for a file that is too large, that is not UTF-8,
    /// or that holds a line with no `=`.
    pub fn parse(bytes: &[u8]) -> Result<Self, SiteFileError> {
        if bytes.len() > MAX_SITE_BYTES {
            return Err(SiteFileError::TooLarge);
        }

        let text = std::str::from_utf8(bytes).map_err(|_| SiteFileError::NotUtf8)?;
        let mut values = BTreeMap::new();
        for line in pytext::lines(text) {
            let bare = pytext::strip(line);
            if bare.is_empty() || bare.starts_with(COMMENT) {
                continue;
            }

            let (key, value) = bare.split_once(ASSIGN).ok_or(SiteFileError::NotKeyValue)?;
            let value = unquoted(pytext::strip(value));
            values.insert(pytext::strip(key).to_owned(), value.to_owned());
        }

        Ok(Self(values))
    }

    /// The raw value of one key. `None` for a key that the file does not
    /// set.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).map(String::as_str)
    }

    /// The value of one key as a `T`.
    fn read<T: FromStr>(
        &self,
        key: SiteKey,
        wrap: fn(T::Err) -> SiteFault,
    ) -> Result<T, SiteError> {
        let text = self.get(key.as_str()).ok_or(SiteError {
            key,
            fault: SiteFault::Unset,
        })?;

        text.parse().map_err(|error| SiteError {
            key,
            fault: wrap(error),
        })
    }

    /// The owner of the repositories that a release reads.
    ///
    /// # Errors
    ///
    /// [`SiteError`] when the file does not set the key, or when the value
    /// is not a GitHub login.
    pub fn github_owner(&self) -> Result<GithubOwner, SiteError> {
        self.read(SiteKey::GithubOwner, SiteFault::GithubOwner)
    }

    /// The account that the user units run as.
    ///
    /// # Errors
    ///
    /// [`SiteError`] when the file does not set the key, when the value is
    /// not an account name, or when the value is `root`.
    pub fn operator_user(&self) -> Result<OperatorUser, SiteError> {
        self.read(SiteKey::OperatorUser, SiteFault::OperatorUser)
    }

    /// The home of the account of the operator.
    ///
    /// # Errors
    ///
    /// [`SiteError`] when the file does not set the key, or when the value
    /// is not an absolute path of plain segments.
    pub fn operator_home(&self) -> Result<OperatorHome, SiteError> {
        self.read(SiteKey::OperatorHome, SiteFault::OperatorHome)
    }

    /// The LAN address of the host.
    ///
    /// # Errors
    ///
    /// [`SiteError`] when the file does not set the key, or when the value
    /// is not a LAN address.
    pub fn lan_address(&self) -> Result<LanAddress, SiteError> {
        self.read(SiteKey::LanAddress, SiteFault::LanAddress)
    }

    /// The name on GitHub of the repository that the catalog calls `name`.
    ///
    /// The file can give the repository another name, with the key
    /// `AGENT_GITHUB_REPO_<NAME>`. Without that key, the name of the catalog
    /// is the answer.
    ///
    /// # Errors
    ///
    /// [`RepoNameError`] when the value of the key is not a repository name.
    pub fn github_repo(&self, name: &RepoName) -> Result<RepoName, RepoNameError> {
        let suffix = name.as_str().to_ascii_uppercase().replace('-', "_");

        match self.get(&format!("{REPO_KEY_PREFIX}{suffix}")) {
            Some(value) => value.parse(),
            None => Ok(name.clone()),
        }
    }
}

/// The value without one pair of quotes at its two ends. The two quotes
/// must be the same character.
fn unquoted(value: &str) -> &str {
    QUOTES
        .iter()
        .find_map(|quote| value.strip_prefix(*quote)?.strip_suffix(*quote))
        .unwrap_or(value)
}

/// Why bytes are not a site file.
///
/// No variant holds a byte of the file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SiteFileError {
    /// The file has more than [`MAX_SITE_BYTES`] bytes.
    TooLarge,
    /// The file is not UTF-8.
    NotUtf8,
    /// A line is not empty, is not a comment and holds no `=`.
    NotKeyValue,
    /// The owner of the file is not root and not the account that reads it.
    WrongOwner,
    /// The group, or each other account, can write the file.
    OthersWrite,
}

impl fmt::Display for SiteFileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge => write!(f, "the site file has {MAX_SITE_BYTES} bytes or less"),
            Self::NotUtf8 => f.write_str("the site file is UTF-8"),
            Self::NotKeyValue => f.write_str("each line of the site file is KEY=VALUE"),
            Self::WrongOwner => {
                f.write_str("the owner of the site file is root or the account that reads it")
            }
            Self::OthersWrite => {
                f.write_str("only the owner of the site file has the permission to write it")
            }
        }
    }
}

impl Error for SiteFileError {}

/// Makes sure that only root, or the account that reads the file, can
/// change a site file.
///
/// The file says which account root drops to and where root installs, so an
/// account that can write the file chooses the two. The binary takes the
/// owner and the mode from one `fstat` of the open file, and reads the file
/// through the same descriptor.
///
/// # Errors
///
/// [`SiteFileError::WrongOwner`] and [`SiteFileError::OthersWrite`].
pub fn check_trust(owner_uid: u32, reader_uid: u32, mode: u32) -> Result<(), SiteFileError> {
    if owner_uid != ROOT_UID && owner_uid != reader_uid {
        return Err(SiteFileError::WrongOwner);
    }

    if mode & OTHERS_WRITE != 0 {
        return Err(SiteFileError::OthersWrite);
    }

    Ok(())
}

// --- the typed values ---

/// The largest count of bytes in a GitHub login.
const GITHUB_OWNER_MAX: usize = 39;

/// The largest count of bytes in an account name.
const OPERATOR_USER_MAX: usize = 32;

/// The largest count of bytes in a repository name.
const REPO_NAME_MAX: usize = 100;

/// Makes a value type of the site file that holds its text and nothing
/// else, with a `check` function that the type gives.
macro_rules! site_text {
    ($(#[$attribute:meta])* $name:ident, $error:ident) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);

        impl $name {
            /// The value as text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = $error;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                Self::check(text)?;

                Ok(Self(text.to_owned()))
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

site_text! {
    /// A GitHub login: 1 to 39 ASCII letters, digits and single hyphens,
    /// with no hyphen at its two ends.
    ///
    /// ```
    /// use creche_contracts::config::site::GithubOwner;
    ///
    /// let owner: GithubOwner = "example-owner".parse()?;
    /// assert_eq!(owner.as_str(), "example-owner");
    /// # Ok::<(), creche_contracts::config::site::GithubOwnerError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::site::GithubOwner;
    ///
    /// let owner = GithubOwner(String::from("-"));
    /// ```
    GithubOwner,
    GithubOwnerError
}

impl GithubOwner {
    fn check(text: &str) -> Result<(), GithubOwnerError> {
        let bytes = text.as_bytes();
        if bytes.is_empty() || bytes.len() > GITHUB_OWNER_MAX {
            return Err(GithubOwnerError::BadLength);
        }

        let mut after_hyphen = true;
        for byte in bytes {
            match byte {
                b'-' if !after_hyphen => after_hyphen = true,
                byte if byte.is_ascii_alphanumeric() => after_hyphen = false,
                _ => return Err(GithubOwnerError::BadByte),
            }
        }

        if after_hyphen {
            return Err(GithubOwnerError::BadByte);
        }

        Ok(())
    }
}

/// Why a text is not a GitHub login.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GithubOwnerError {
    /// The text is empty, or it has more than 39 bytes.
    BadLength,
    /// A byte is not an ASCII letter, a digit or a single hyphen between two
    /// of them.
    BadByte,
}

impl fmt::Display for GithubOwnerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadLength => write!(f, "a GitHub login has 1 to {GITHUB_OWNER_MAX} bytes"),
            Self::BadByte => {
                f.write_str("a GitHub login holds letters, digits and single inner hyphens")
            }
        }
    }
}

impl Error for GithubOwnerError {}

site_text! {
    /// The Linux account of the operator: `[a-z_][a-z0-9_-]{0,31}`, and
    /// never `root`.
    ///
    /// A release that installs the trees of the operator as root gives each
    /// service the files of root.
    ///
    /// ```
    /// use creche_contracts::config::site::{OperatorUser, OperatorUserError};
    ///
    /// let user: OperatorUser = "operator".parse()?;
    /// assert_eq!(user.as_str(), "operator");
    /// assert_eq!("root".parse::<OperatorUser>(), Err(OperatorUserError::Root));
    /// # Ok::<(), OperatorUserError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::site::OperatorUser;
    ///
    /// let user = OperatorUser(String::from("root"));
    /// ```
    OperatorUser,
    OperatorUserError
}

impl OperatorUser {
    fn check(text: &str) -> Result<(), OperatorUserError> {
        let bytes = text.as_bytes();
        if bytes.is_empty() || bytes.len() > OPERATOR_USER_MAX {
            return Err(OperatorUserError::BadLength);
        }

        let first_holds = bytes
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || *byte == b'_');
        let tail_holds = bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        });
        if !first_holds || !tail_holds {
            return Err(OperatorUserError::BadByte);
        }

        if text == ROOT_USER {
            return Err(OperatorUserError::Root);
        }

        Ok(())
    }
}

/// Why a text is not the account of the operator.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperatorUserError {
    /// The text is empty, or it has more than 32 bytes.
    BadLength,
    /// The first byte is not `a` to `z` or `_`, or a later byte is not `a`
    /// to `z`, a digit, `_` or `-`.
    BadByte,
    /// The text is `root`.
    Root,
}

impl fmt::Display for OperatorUserError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadLength => write!(f, "an account name has 1 to {OPERATOR_USER_MAX} bytes"),
            Self::BadByte => f.write_str(
                "an account name starts with a to z or _ and holds a to z, 0 to 9, _ and -",
            ),
            Self::Root => f.write_str("the account of the operator is not root"),
        }
    }
}

impl Error for OperatorUserError {}

/// Whether a byte can be in a segment of a home path or in a repository
/// name, after the first byte.
fn is_plain_tail(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
}

/// Whether a byte can start a segment of a home path or a repository name.
fn is_plain_first(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

/// Whether `text` starts with a plain first byte and holds only plain
/// bytes.
fn is_plain_name(text: &str) -> bool {
    let bytes = text.as_bytes();

    bytes.first().copied().is_some_and(is_plain_first) && bytes.iter().copied().all(is_plain_tail)
}

site_text! {
    /// The home of the account of the operator: an absolute path of plain
    /// segments.
    ///
    /// Each segment starts with an ASCII letter, a digit or `_`. Each later
    /// byte is a letter, a digit, `.`, `_` or `-`. So the path holds no `..`
    /// segment and no final slash: an install path joins it.
    ///
    /// ```
    /// use creche_contracts::config::site::OperatorHome;
    ///
    /// let home: OperatorHome = "/home/operator".parse()?;
    /// assert_eq!(home.as_str(), "/home/operator");
    /// assert!("/home/../root".parse::<OperatorHome>().is_err());
    /// # Ok::<(), creche_contracts::config::site::OperatorHomeError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::site::OperatorHome;
    ///
    /// let home = OperatorHome(String::from("/home/../root"));
    /// ```
    OperatorHome,
    OperatorHomeError
}

impl OperatorHome {
    fn check(text: &str) -> Result<(), OperatorHomeError> {
        let rest = text
            .strip_prefix('/')
            .ok_or(OperatorHomeError::NotAbsolute)?;
        if !rest.split('/').all(is_plain_name) {
            return Err(OperatorHomeError::BadSegment);
        }

        Ok(())
    }
}

/// Why a text is not the home of the operator.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperatorHomeError {
    /// The text does not start with `/`.
    NotAbsolute,
    /// A segment is empty, starts with `.` or `-`, or holds a byte that is
    /// not a letter, a digit, `.`, `_` or `-`.
    BadSegment,
}

impl fmt::Display for OperatorHomeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotAbsolute => f.write_str("a home starts with /"),
            Self::BadSegment => f.write_str(
                "each segment of a home starts with a letter, a digit or _ and holds letters, \
                 digits, ., _ and -",
            ),
        }
    }
}

impl Error for OperatorHomeError {}

site_text! {
    /// The name of a repository on GitHub: `[A-Za-z0-9_][A-Za-z0-9._-]{0,99}`.
    ///
    /// The name goes into the path of an API call, so it does not start
    /// with `.` or `-`.
    ///
    /// ```
    /// use creche_contracts::config::site::RepoName;
    ///
    /// let name: RepoName = "agent-control".parse()?;
    /// assert_eq!(name.as_str(), "agent-control");
    /// assert!("..".parse::<RepoName>().is_err());
    /// # Ok::<(), creche_contracts::config::site::RepoNameError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::site::RepoName;
    ///
    /// let name = RepoName(String::from(".."));
    /// ```
    RepoName,
    RepoNameError
}

impl RepoName {
    fn check(text: &str) -> Result<(), RepoNameError> {
        if text.is_empty() || text.len() > REPO_NAME_MAX {
            return Err(RepoNameError::BadLength);
        }

        if !is_plain_name(text) {
            return Err(RepoNameError::BadByte);
        }

        Ok(())
    }
}

/// Why a text is not a repository name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RepoNameError {
    /// The text is empty, or it has more than 100 bytes.
    BadLength,
    /// The first byte is not a letter, a digit or `_`, or a later byte is
    /// not a letter, a digit, `.`, `_` or `-`.
    BadByte,
}

impl fmt::Display for RepoNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadLength => write!(f, "a repository name has 1 to {REPO_NAME_MAX} bytes"),
            Self::BadByte => f.write_str(
                "a repository name starts with a letter, a digit or _ and holds letters, digits, \
                 ., _ and -",
            ),
        }
    }
}

impl Error for RepoNameError {}

// --- the valid form ---

/// Why the site file gives no value for one key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SiteFault {
    /// The file does not set the key.
    Unset,
    /// The value is not a GitHub login.
    GithubOwner(GithubOwnerError),
    /// The value is not the account of the operator.
    OperatorUser(OperatorUserError),
    /// The value is not the home of the operator.
    OperatorHome(OperatorHomeError),
    /// The value is not a LAN address.
    LanAddress(LanAddressError),
}

/// Why the site file gives no value for one key: the key and the reason.
///
/// The error does not hold the value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SiteError {
    key: SiteKey,
    fault: SiteFault,
}

impl SiteError {
    /// The key that the error is about.
    #[must_use]
    pub fn key(&self) -> SiteKey {
        self.key
    }

    /// The reason.
    #[must_use]
    pub fn fault(&self) -> SiteFault {
        self.fault
    }
}

impl fmt::Display for SiteFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unset => f.write_str("the site file does not set the key"),
            Self::GithubOwner(error) => write!(f, "{error}"),
            Self::OperatorUser(error) => write!(f, "{error}"),
            Self::OperatorHome(error) => write!(f, "{error}"),
            Self::LanAddress(error) => write!(f, "{error}"),
        }
    }
}

impl fmt::Display for SiteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.key, self.fault)
    }
}

impl Error for SiteError {}

/// The four values of a site file that root's executor needs.
///
/// The conversion from a [`SiteFile`] collects the error of each key, so
/// the operator corrects the file in one step. `handover.site` stops at the
/// first key.
///
/// FAILURE ACTION. At start, a process that needs the site and cannot read
/// it exits with `EX_CONFIG`. A daemon that does so needs
/// `RestartPreventExitStatus=78` in its unit file. A process that reads the
/// file again while it runs keeps the last good value and publishes a
/// fault.
///
/// ```
/// use creche_contracts::config::site::{Site, SiteFile};
///
/// let text = "AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER=operator\n\
///             AGENT_OPERATOR_HOME=/home/operator\nAGENT_LAN_ADDRESS=192.0.2.10\n";
/// let site = Site::try_from(&SiteFile::parse(text.as_bytes())?)?;
/// assert_eq!(site.lan_address().as_str(), "192.0.2.10");
/// assert_eq!(site.to_text(), text);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::site::Site;
///
/// let site = Site {
///     github_owner: "example-owner".parse().unwrap(),
///     operator_user: "operator".parse().unwrap(),
///     operator_home: "/home/operator".parse().unwrap(),
///     lan_address: "192.0.2.10".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Site {
    github_owner: GithubOwner,
    operator_user: OperatorUser,
    operator_home: OperatorHome,
    lan_address: LanAddress,
}

impl Site {
    /// The site of the four values.
    #[must_use]
    pub fn new(
        github_owner: GithubOwner,
        operator_user: OperatorUser,
        operator_home: OperatorHome,
        lan_address: LanAddress,
    ) -> Self {
        Self {
            github_owner,
            operator_user,
            operator_home,
            lan_address,
        }
    }

    /// The owner of the repositories that a release reads.
    #[must_use]
    pub fn github_owner(&self) -> &GithubOwner {
        &self.github_owner
    }

    /// The account that the user units run as.
    #[must_use]
    pub fn operator_user(&self) -> &OperatorUser {
        &self.operator_user
    }

    /// The home of that account.
    #[must_use]
    pub fn operator_home(&self) -> &OperatorHome {
        &self.operator_home
    }

    /// The LAN address of the host.
    #[must_use]
    pub fn lan_address(&self) -> &LanAddress {
        &self.lan_address
    }

    /// The text of a site file that holds the four values: one `KEY=VALUE`
    /// line for each key, with no quotes.
    ///
    /// No value holds a space, a quote or a `#`, so `handover.site`, systemd
    /// and a shell read each line in the same way.
    #[must_use]
    pub fn to_text(&self) -> String {
        let values = [
            self.github_owner.as_str(),
            self.operator_user.as_str(),
            self.operator_home.as_str(),
            self.lan_address.as_str(),
        ];

        SiteKey::ALL
            .iter()
            .zip(values)
            .map(|(key, value)| format!("{key}{ASSIGN}{value}\n"))
            .collect()
    }
}

impl Checked for Site {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::KeepLastGood);
}

impl TryFrom<&SiteFile> for Site {
    type Error = SiteErrors;

    fn try_from(file: &SiteFile) -> Result<Self, Self::Error> {
        match (
            file.github_owner(),
            file.operator_user(),
            file.operator_home(),
            file.lan_address(),
        ) {
            (Ok(github_owner), Ok(operator_user), Ok(operator_home), Ok(lan_address)) => Ok(
                Self::new(github_owner, operator_user, operator_home, lan_address),
            ),
            (github_owner, operator_user, operator_home, lan_address) => {
                let errors = [
                    github_owner.err(),
                    operator_user.err(),
                    operator_home.err(),
                    lan_address.err(),
                ];

                Err(SiteErrors(errors.into_iter().flatten().collect()))
            }
        }
    }
}

/// Each error of one conversion of a site file: one error or more.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SiteErrors(Vec<SiteError>);

impl SiteErrors {
    /// The errors, in the order of [`SiteKey::ALL`]. The slice is never
    /// empty.
    #[must_use]
    pub fn as_slice(&self) -> &[SiteError] {
        &self.0
    }
}

impl fmt::Display for SiteErrors {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (at, error) in self.0.iter().enumerate() {
            if at > 0 {
                f.write_str("; ")?;
            }

            write!(f, "{error}")?;
        }

        Ok(())
    }
}

impl Error for SiteErrors {}

#[cfg(test)]
mod tests {
    use super::*;

    /// A site file that holds each key.
    const COMPLETE: &str = "AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER=operator\n\
        AGENT_OPERATOR_HOME=/home/operator\nAGENT_LAN_ADDRESS=192.0.2.10\n";

    fn file(text: &str) -> SiteFile {
        SiteFile::parse(text.as_bytes()).unwrap()
    }

    #[test]
    fn a_line_is_a_key_and_a_value() {
        let parsed = file(
            "# a comment\n\n  \t\nA=1\n B = 2 \nC=\"quoted\"\nD='single'\nE=\"mixed'\nF=a=b\n\
             G=\n=empty key\nH=\"\"\nI=\"\n  # indented comment\nA=3\r\nJ= \" spaced \" \n",
        );

        for (key, value) in [
            ("A", Some("3")),
            ("B", Some("2")),
            ("C", Some("quoted")),
            ("D", Some("single")),
            ("E", Some("\"mixed'")),
            ("F", Some("a=b")),
            ("G", Some("")),
            ("", Some("empty key")),
            ("H", Some("")),
            ("I", Some("\"")),
            ("J", Some(" spaced ")),
            ("K", None),
            ("# a comment", None),
        ] {
            assert_eq!(parsed.get(key), value, "{key:?}");
        }
    }

    #[test]
    fn a_line_ends_where_python_ends_it() {
        let parsed = file("A=1\u{0b}B=2\u{85}C=3\u{2028}D=4\rE=5\u{1c}F=6\u{1f}G=7");

        for (key, value) in [
            ("A", Some("1")),
            ("B", Some("2")),
            ("C", Some("3")),
            ("D", Some("4")),
            ("E", Some("5")),
            ("F", Some("6\u{1f}G=7")),
            ("G", None),
        ] {
            assert_eq!(parsed.get(key), value, "{key:?}");
        }
    }

    #[test]
    fn a_file_that_is_no_site_file_is_refused() {
        let large = vec![b'#'; MAX_SITE_BYTES + 1];

        assert_eq!(SiteFile::parse(&large), Err(SiteFileError::TooLarge));
        assert!(SiteFile::parse(&large[1..]).is_ok());
        assert_eq!(SiteFile::parse(b"A=\xff"), Err(SiteFileError::NotUtf8));
        assert_eq!(
            SiteFile::parse(b"A=1\nno assignment\n"),
            Err(SiteFileError::NotKeyValue)
        );
        assert_eq!(SiteFile::parse(b"export"), Err(SiteFileError::NotKeyValue));
        assert_eq!(SiteFile::parse(b""), Ok(SiteFile(BTreeMap::new())));
    }

    #[test]
    fn only_root_or_the_reader_can_change_a_site_file() {
        assert_eq!(check_trust(0, 1000, 0o100644), Ok(()));
        assert_eq!(check_trust(1000, 1000, 0o100600), Ok(()));
        assert_eq!(check_trust(0, 0, 0o100644), Ok(()));
        assert_eq!(
            check_trust(1001, 1000, 0o100644),
            Err(SiteFileError::WrongOwner)
        );
        assert_eq!(
            check_trust(1000, 0, 0o100644),
            Err(SiteFileError::WrongOwner)
        );
        assert_eq!(
            check_trust(0, 1000, 0o100664),
            Err(SiteFileError::OthersWrite)
        );
        assert_eq!(
            check_trust(0, 1000, 0o100646),
            Err(SiteFileError::OthersWrite)
        );
    }

    #[test]
    fn a_github_owner_is_a_github_login() {
        for text in ["a", "A", "0", "example-owner", "a-b-c", &"a".repeat(39)] {
            assert_eq!(text.parse::<GithubOwner>().unwrap().as_str(), text);
        }

        for (text, error) in [
            ("", GithubOwnerError::BadLength),
            (&*"a".repeat(40), GithubOwnerError::BadLength),
            ("-a", GithubOwnerError::BadByte),
            ("a-", GithubOwnerError::BadByte),
            ("a--b", GithubOwnerError::BadByte),
            ("a_b", GithubOwnerError::BadByte),
            ("a.b", GithubOwnerError::BadByte),
            ("a/b", GithubOwnerError::BadByte),
            ("owner\n", GithubOwnerError::BadByte),
            ("ownér", GithubOwnerError::BadByte),
        ] {
            assert_eq!(text.parse::<GithubOwner>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn an_operator_user_is_an_account_name_and_never_root() {
        for text in ["operator", "_", "a", "a-b_c9", "rooty", &"a".repeat(32)] {
            assert_eq!(text.parse::<OperatorUser>().unwrap().to_string(), text);
        }

        for (text, error) in [
            ("", OperatorUserError::BadLength),
            (&*"a".repeat(33), OperatorUserError::BadLength),
            ("root", OperatorUserError::Root),
            ("Operator", OperatorUserError::BadByte),
            ("9a", OperatorUserError::BadByte),
            ("-a", OperatorUserError::BadByte),
            ("a.b", OperatorUserError::BadByte),
            ("a b", OperatorUserError::BadByte),
            ("operator\n", OperatorUserError::BadByte),
            ("opérator", OperatorUserError::BadByte),
        ] {
            assert_eq!(text.parse::<OperatorUser>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn an_operator_home_is_an_absolute_path_of_plain_segments() {
        for text in ["/home/operator", "/a", "/_a/b.c-d_e", "/0/1"] {
            assert_eq!(text.parse::<OperatorHome>().unwrap().as_str(), text);
        }

        for (text, error) in [
            ("", OperatorHomeError::NotAbsolute),
            ("home/operator", OperatorHomeError::NotAbsolute),
            ("/", OperatorHomeError::BadSegment),
            ("/home/operator/", OperatorHomeError::BadSegment),
            ("/home//operator", OperatorHomeError::BadSegment),
            ("/home/../root", OperatorHomeError::BadSegment),
            ("/home/.hidden", OperatorHomeError::BadSegment),
            ("/home/-a", OperatorHomeError::BadSegment),
            ("/home/a b", OperatorHomeError::BadSegment),
            ("/home/operator\n", OperatorHomeError::BadSegment),
            ("/home/opérator", OperatorHomeError::BadSegment),
        ] {
            assert_eq!(text.parse::<OperatorHome>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn a_repo_name_is_a_repository_name_of_github() {
        for text in ["agent-control", "a", "_a", "a.b_c-d", &"a".repeat(100)] {
            assert_eq!(text.parse::<RepoName>().unwrap().as_str(), text);
        }

        for (text, error) in [
            ("", RepoNameError::BadLength),
            (&*"a".repeat(101), RepoNameError::BadLength),
            (".", RepoNameError::BadByte),
            ("..", RepoNameError::BadByte),
            ("-a", RepoNameError::BadByte),
            ("a/b", RepoNameError::BadByte),
            ("a\n", RepoNameError::BadByte),
            ("é", RepoNameError::BadByte),
        ] {
            assert_eq!(text.parse::<RepoName>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn the_file_can_give_a_repository_another_name() {
        let name: RepoName = "agent-control".parse().unwrap();
        let renamed = file("AGENT_GITHUB_REPO_AGENT_CONTROL=creche\n");
        let bad = file("AGENT_GITHUB_REPO_AGENT_CONTROL=../creche\n");

        assert_eq!(file("").github_repo(&name), Ok(name.clone()));
        assert_eq!(renamed.github_repo(&name).unwrap().as_str(), "creche");
        assert_eq!(bad.github_repo(&name), Err(RepoNameError::BadByte));
    }

    #[test]
    fn a_site_holds_the_four_values() {
        let site = Site::try_from(&file(COMPLETE)).unwrap();

        assert_eq!(site.github_owner().as_str(), "example-owner");
        assert_eq!(site.operator_user().as_str(), "operator");
        assert_eq!(site.operator_home().as_str(), "/home/operator");
        assert_eq!(site.lan_address().as_str(), "192.0.2.10");
    }

    #[test]
    fn the_text_of_a_site_reads_back_as_the_same_site() {
        let site = Site::try_from(&file(COMPLETE)).unwrap();

        assert_eq!(site.to_text(), COMPLETE);
        assert_eq!(Site::try_from(&file(&site.to_text())), Ok(site));
    }

    #[test]
    fn the_conversion_collects_the_error_of_each_key() {
        let errors = Site::try_from(&file(
            "AGENT_OPERATOR_USER=root\nAGENT_OPERATOR_HOME=home\nAGENT_LAN_ADDRESS=0.0.0.0\n",
        ))
        .unwrap_err();
        let found: Vec<(SiteKey, SiteFault)> = errors
            .as_slice()
            .iter()
            .map(|error| (error.key(), error.fault()))
            .collect();

        assert_eq!(
            found,
            [
                (SiteKey::GithubOwner, SiteFault::Unset),
                (
                    SiteKey::OperatorUser,
                    SiteFault::OperatorUser(OperatorUserError::Root)
                ),
                (
                    SiteKey::OperatorHome,
                    SiteFault::OperatorHome(OperatorHomeError::NotAbsolute)
                ),
                (
                    SiteKey::LanAddress,
                    SiteFault::LanAddress(LanAddressError::EachInterface)
                ),
            ]
        );
        assert_eq!(
            errors.to_string(),
            "AGENT_GITHUB_OWNER: the site file does not set the key; AGENT_OPERATOR_USER: the \
             account of the operator is not root; AGENT_OPERATOR_HOME: a home starts with /; \
             AGENT_LAN_ADDRESS: a LAN address is not the address of each interface"
        );
    }

    #[test]
    fn an_error_does_not_hold_the_value() {
        let error = file("AGENT_GITHUB_OWNER=not a login\n")
            .github_owner()
            .unwrap_err();

        assert!(!format!("{error} {error:?}").contains("not a login"));
        assert_eq!(
            SiteFileError::NotKeyValue.to_string(),
            "each line of the site file is KEY=VALUE"
        );
    }

    #[test]
    fn the_failure_action_of_a_site_is_stated() {
        assert_eq!(Site::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(Site::FAILURE.at_reload(), AtReload::KeepLastGood);
    }
}
