//! The value types of a config: an address, a port, a path, a duration, a URL.
//!
//! Each type has a private field and a parsing constructor. A config holds
//! these types and no raw text, so code after the parse cannot see a value
//! that the process cannot use.
//!
//! No error type here holds the text. A config value can be a secret, and a
//! caller writes the error to a log.

use std::error::Error;
use std::fmt;
use std::net::{Ipv4Addr, Ipv6Addr};
use std::num::NonZeroU16;
use std::str::FromStr;
use std::time::Duration;

use super::pytext::{self, Integer};

// --- the LAN address of the host ---

/// The largest count of bytes in one label of a host name.
const LABEL_MAX: usize = 63;

/// What separates two labels of a host name.
const LABEL_SEPARATOR: char = '.';

/// The start of a label that the C resolver reads as a hexadecimal number.
const HEX_PREFIXES: [&str; 2] = ["0x", "0X"];

/// Whether `label` is one DNS label: 1 to 63 ASCII letters, digits and
/// hyphens, with no hyphen at its two ends.
fn is_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    let ends_hold = bytes.first().is_some_and(u8::is_ascii_alphanumeric)
        && bytes.last().is_some_and(u8::is_ascii_alphanumeric);

    ends_hold
        && bytes.len() <= LABEL_MAX
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'-')
}

/// Whether the C resolver reads `label` as a number: decimal digits, or `0x`
/// and hexadecimal digits.
fn is_number_like(label: &str) -> bool {
    if label.bytes().all(|byte| byte.is_ascii_digit()) {
        return true;
    }

    HEX_PREFIXES.iter().any(|prefix| {
        label
            .strip_prefix(prefix)
            .is_some_and(|rest| rest.bytes().all(|byte| byte.is_ascii_hexdigit()))
    })
}

/// Checks the grammar that [`LanAddress`] and a host name of [`BindHost`]
/// share.
fn check_host_name(text: &str) -> Result<(), LanAddressError> {
    if text.is_empty() {
        return Err(LanAddressError::Empty);
    }

    let mut last = "";
    for (at, label) in text.split(LABEL_SEPARATOR).enumerate() {
        if !is_label(label) {
            return Err(LanAddressError::BadLabel { at });
        }

        last = label;
    }

    if !is_number_like(last) {
        return Ok(());
    }

    let address: Ipv4Addr = text.parse().map_err(|_| LanAddressError::NotIpv4)?;
    if address.is_unspecified() {
        return Err(LanAddressError::EachInterface);
    }

    Ok(())
}

// CONTRACT-QUESTION: no contract gives the LAN address of the site file a
// grammar. `handover.site` takes labels of letters, digits and hyphens with
// dots between them. The five other Python readers (`attendance.config`,
// `noticeboard.config`, `chaperone.site`, `caregiver.lan` and
// `agent_door_trigger.config`) take each text that is not empty. The type
// takes the strictest copy. It also refuses a text that ends in a number and
// is not one IPv4 address, and the address `0.0.0.0`: the C resolver reads `0`
// and `0.0` as `0.0.0.0`, and a service that binds that address answers on
// each interface. A laxer reading lets a typing error in the site file
// publish a service.
/// The LAN address of the host: `AGENT_LAN_ADDRESS` of the site file.
///
/// The value is an IPv4 address or a host name. It holds no port, no scheme
/// and no slash, because a service binds it and builds a URL from it.
///
/// A text whose last label is a number must be one IPv4 address of four
/// decimal numbers. The address `0.0.0.0` is refused: a service must not
/// bind each interface.
///
/// ```
/// use creche_contracts::config::LanAddress;
///
/// let address: LanAddress = "192.0.2.10".parse()?;
/// assert_eq!(address.as_str(), "192.0.2.10");
/// assert!("0.0.0.0".parse::<LanAddress>().is_err());
/// # Ok::<(), creche_contracts::config::LanAddressError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::LanAddress;
///
/// let address = LanAddress(String::from("0.0.0.0"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct LanAddress(String);

impl LanAddress {
    /// The address as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for LanAddress {
    type Err = LanAddressError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        check_host_name(text)?;

        Ok(Self(text.to_owned()))
    }
}

impl fmt::Display for LanAddress {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not a LAN address.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LanAddressError {
    /// The text has no byte.
    Empty,
    /// The label at this position, from 0, is not 1 to 63 letters, digits
    /// and hyphens, or it has a hyphen at one end.
    BadLabel {
        /// The position of the label, from 0.
        at: usize,
    },
    /// The text ends in a number, and it is not four decimal numbers from 0
    /// to 255.
    NotIpv4,
    /// The text is `0.0.0.0`, the address of each interface.
    EachInterface,
}

impl fmt::Display for LanAddressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("a LAN address has 1 byte or more"),
            Self::BadLabel { at } => write!(
                f,
                "label {at} of a LAN address is not 1 to {LABEL_MAX} letters, digits and hyphens"
            ),
            Self::NotIpv4 => {
                f.write_str("a LAN address that ends in a number is four numbers from 0 to 255")
            }
            Self::EachInterface => {
                f.write_str("a LAN address is not the address of each interface")
            }
        }
    }
}

impl Error for LanAddressError {}

// --- the host that a service binds ---

/// Each text that a Python service refuses as the host of a bind, in lower
/// case: the wildcard set of `noticeboard.config`, `agent_door_owui.config`
/// and `agent_door_trigger.config`. The empty text is in each set, and
/// [`BindHostError::Empty`] holds that case.
const WILDCARDS: [&str; 4] = ["0.0.0.0", "::", "[::]", "*"];

/// The three texts that `noticeboard.config` reads as a loopback bind.
const LOOPBACK_TEXTS: [&str; 3] = ["127.0.0.1", "::1", "localhost"];

// CONTRACT-QUESTION: contract 02 §3 rule 2 says that a service binds the LAN
// address and never `0.0.0.0`. It gives a bind host no grammar. Three Python
// services refuse a small set of wildcard texts. The chaperone refuses each
// address of each interface in each spelling. `attendance` refuses no text.
// The type refuses each address of each interface in each spelling, and
// each text that is not an IP address or a host name. A laxer reading lets
// `0` or `::0` publish a service.
/// The host that a service binds: an IPv4 address, an IPv6 address or a host
/// name.
///
/// The type refuses each address that stands for each interface: `0.0.0.0`,
/// `::` in each spelling, and `*`. The root `AGENTS.md` holds the rule: bind
/// the LAN address or a Unix socket.
///
/// ```
/// use creche_contracts::config::BindHost;
///
/// let host: BindHost = "192.0.2.10".parse()?;
/// assert_eq!(host.as_str(), "192.0.2.10");
/// assert!("0.0.0.0".parse::<BindHost>().is_err());
/// assert!("::".parse::<BindHost>().is_err());
/// # Ok::<(), creche_contracts::config::BindHostError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::BindHost;
///
/// let host = BindHost(String::from("0.0.0.0"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct BindHost(String);

impl BindHost {
    /// The host as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Whether the host is one of the three texts that the noticeboard reads
    /// as loopback: `127.0.0.1`, `::1` and `localhost`.
    #[must_use]
    pub fn is_loopback_text(&self) -> bool {
        LOOPBACK_TEXTS.contains(&self.0.as_str())
    }
}

impl FromStr for BindHost {
    type Err = BindHostError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.is_empty() {
            return Err(BindHostError::Empty);
        }

        if WILDCARDS.contains(&text.to_ascii_lowercase().as_str()) {
            return Err(BindHostError::EachInterface);
        }

        if let Ok(address) = text.parse::<Ipv6Addr>() {
            let each_interface = address.is_unspecified()
                || address
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_unspecified());

            return if each_interface {
                Err(BindHostError::EachInterface)
            } else {
                Ok(Self(text.to_owned()))
            };
        }

        match check_host_name(text) {
            Ok(()) => Ok(Self(text.to_owned())),
            Err(LanAddressError::EachInterface) => Err(BindHostError::EachInterface),
            Err(LanAddressError::Empty) => Err(BindHostError::Empty),
            Err(LanAddressError::BadLabel { .. } | LanAddressError::NotIpv4) => {
                Err(BindHostError::NotAHost)
            }
        }
    }
}

impl From<LanAddress> for BindHost {
    fn from(address: LanAddress) -> Self {
        Self(address.0)
    }
}

impl fmt::Display for BindHost {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not the host of a bind.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindHostError {
    /// The text has no byte.
    Empty,
    /// The text stands for each interface of the host.
    EachInterface,
    /// The text is not an IP address and not a host name.
    NotAHost,
}

impl fmt::Display for BindHostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("a bind host has 1 byte or more"),
            Self::EachInterface => {
                f.write_str("a bind host is the LAN address or loopback, not each interface")
            }
            Self::NotAHost => f.write_str("a bind host is an IP address or a host name"),
        }
    }
}

impl Error for BindHostError {}

// --- a TCP port ---

/// A TCP port: 1 to 65535.
///
/// ```
/// use creche_contracts::config::Port;
///
/// let port: Port = "8350".parse()?;
/// assert_eq!(port.get(), 8350);
/// assert!("0".parse::<Port>().is_err());
/// # Ok::<(), creche_contracts::config::PortError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::Port;
///
/// let port = Port(std::num::NonZeroU16::MIN);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Port(NonZeroU16);

impl Port {
    /// The port as a number.
    #[must_use]
    pub fn get(self) -> u16 {
        self.0.get()
    }

    /// A port that the code of a service holds as a constant. `None` for 0.
    pub(super) const fn fixed(port: u16) -> Option<Self> {
        match NonZeroU16::new(port) {
            Some(port) => Some(Self(port)),
            None => None,
        }
    }
}

impl FromStr for Port {
    type Err = PortError;

    /// Reads the text as Python's `int` does: space at the two ends, a sign
    /// and single underscores between digits are permitted.
    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let number = match pytext::integer(text).ok_or(PortError::NotANumber)? {
            Integer::Fits(number) => number,
            Integer::TooLarge => return Err(PortError::OutOfRange),
        };
        let port = u16::try_from(number).map_err(|_| PortError::OutOfRange)?;

        NonZeroU16::new(port).map(Self).ok_or(PortError::OutOfRange)
    }
}

impl fmt::Display for Port {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Why a text is not a port.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortError {
    /// The text is not a decimal number.
    NotANumber,
    /// The number is not 1 to 65535.
    OutOfRange,
}

impl fmt::Display for PortError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotANumber => f.write_str("a port is a decimal number"),
            Self::OutOfRange => f.write_str("a port is 1 to 65535"),
        }
    }
}

impl Error for PortError {}

// --- a host and a port ---

/// What separates the host from the port.
const PORT_SEPARATOR: char = ':';

/// The host and the port that a service binds: `host:port`.
///
/// The text splits at its last colon, so an IPv6 host can have brackets or
/// no brackets: `[::1]:8340` and `::1:8340` are the same bind.
///
/// ```
/// use creche_contracts::config::BindAddress;
///
/// let bind: BindAddress = "192.0.2.10:8300".parse()?;
/// assert_eq!(bind.host().as_str(), "192.0.2.10");
/// assert_eq!(bind.port().get(), 8300);
/// assert!("0.0.0.0:8300".parse::<BindAddress>().is_err());
/// # Ok::<(), creche_contracts::config::BindAddressError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::BindAddress;
///
/// let bind = BindAddress {
///     host: "192.0.2.10".parse().unwrap(),
///     port: "8300".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct BindAddress {
    host: BindHost,
    port: Port,
}

impl BindAddress {
    /// The bind of a service on the LAN address, at a port of its code.
    #[must_use]
    pub fn on(host: BindHost, port: Port) -> Self {
        Self { host, port }
    }

    /// The host.
    #[must_use]
    pub fn host(&self) -> &BindHost {
        &self.host
    }

    /// The port.
    #[must_use]
    pub fn port(&self) -> Port {
        self.port
    }
}

impl FromStr for BindAddress {
    type Err = BindAddressError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let (host, port) = text
            .rsplit_once(PORT_SEPARATOR)
            .ok_or(BindAddressError::NoPort)?;
        let host = match host.strip_prefix('[') {
            Some(inner) => inner
                .strip_suffix(']')
                .ok_or(BindAddressError::Host(BindHostError::NotAHost))?,
            None => host,
        };
        let host = host.parse().map_err(BindAddressError::Host)?;
        let port = port.parse().map_err(BindAddressError::Port)?;

        Ok(Self { host, port })
    }
}

impl fmt::Display for BindAddress {
    /// Writes `host:port`. An IPv6 host gets brackets, so the last colon is
    /// the one before the port.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.host.as_str().contains(PORT_SEPARATOR) {
            return write!(f, "[{}]{PORT_SEPARATOR}{}", self.host, self.port);
        }

        write!(f, "{}{PORT_SEPARATOR}{}", self.host, self.port)
    }
}

/// Why a text is not `host:port`.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindAddressError {
    /// The text has no colon.
    NoPort,
    /// The text before the last colon is not a bind host.
    Host(BindHostError),
    /// The text after the last colon is not a port.
    Port(PortError),
}

impl fmt::Display for BindAddressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoPort => f.write_str("a bind is host:port"),
            Self::Host(error) => write!(f, "before the last colon, {error}"),
            Self::Port(error) => write!(f, "after the last colon, {error}"),
        }
    }
}

impl Error for BindAddressError {}

// --- a path ---

/// The largest count of bytes in the path of a Unix socket. `sun_path` of
/// Linux holds 108 bytes, and the last one is the final NUL.
const SOCKET_PATH_MAX: usize = 107;

/// What separates two segments of a path.
const PATH_SEPARATOR: char = '/';

/// A segment that names the directory itself.
const SAME_DIRECTORY: &str = ".";

/// The start of a path that keeps two slashes. POSIX gives that start its own
/// meaning, and Python's `pathlib` keeps it.
const DOUBLE_ROOT: &str = "//";

/// Why a text is not a path that a config can hold.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathError {
    /// The text has no byte.
    Empty,
    /// The text does not start with `/`.
    NotAbsolute,
    /// The text holds a NUL byte.
    Nul,
    /// The path of a Unix socket has more than 107 bytes.
    TooLong,
}

impl fmt::Display for PathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("a path has 1 byte or more"),
            Self::NotAbsolute => f.write_str("a path starts with /"),
            Self::Nul => f.write_str("a path holds no NUL byte"),
            Self::TooLong => write!(
                f,
                "the path of a Unix socket has {SOCKET_PATH_MAX} bytes or less"
            ),
        }
    }
}

impl Error for PathError {}

/// The text of an absolute path as Python's `pathlib.PurePosixPath` writes
/// it: one slash between two segments, no `.` segment and no final slash. A
/// start of exactly two slashes stays.
fn normal_path(text: &str) -> Result<String, PathError> {
    if text.is_empty() {
        return Err(PathError::Empty);
    }

    if text.contains('\0') {
        return Err(PathError::Nul);
    }

    if !text.starts_with(PATH_SEPARATOR) {
        return Err(PathError::NotAbsolute);
    }

    let double = text.starts_with(DOUBLE_ROOT) && !text.starts_with("///");
    let segments: Vec<&str> = text
        .split(PATH_SEPARATOR)
        .filter(|segment| !segment.is_empty() && *segment != SAME_DIRECTORY)
        .collect();
    let root = if double { DOUBLE_ROOT } else { "/" };

    Ok(format!("{root}{}", segments.join("/")))
}

// CONTRACT-QUESTION: no contract gives a config path a grammar. Each Python
// service takes each text, a relative path and a text with a NUL byte too.
// The path types refuse a relative path, because its meaning depends on the
// working directory of the unit. They refuse a NUL byte, because each open of
// such a path raises. `SocketPath` refuses a path of more than 107 bytes,
// because the kernel refuses the bind. A laxer reading makes the process stop
// at its first use of the path, after the config parse passed.
/// Makes one path type. Each type is an absolute path with no NUL byte, in
/// the normal form of Python's `pathlib`.
macro_rules! path_type {
    ($(#[$attribute:meta])* $name:ident, $max:expr) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);

        impl $name {
            /// The path as text, in its normal form.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }

            /// The path, for a call of the standard library.
            #[must_use]
            pub fn as_path(&self) -> &std::path::Path {
                std::path::Path::new(&self.0)
            }
        }

        impl FromStr for $name {
            type Err = PathError;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                let path = normal_path(text)?;
                let max: Option<usize> = $max;
                if max.is_some_and(|max| path.len() > max) {
                    return Err(PathError::TooLong);
                }

                Ok(Self(path))
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

path_type! {
    /// The path of a Unix socket: absolute, with no NUL byte, 107 bytes or
    /// less (contract 02 §3 rule 1).
    ///
    /// ```
    /// use creche_contracts::config::SocketPath;
    ///
    /// let path: SocketPath = "/srv/agents/state/rework/sock/sessiond.sock".parse()?;
    /// assert_eq!(path.as_str(), "/srv/agents/state/rework/sock/sessiond.sock");
    /// assert!("sessiond.sock".parse::<SocketPath>().is_err());
    /// # Ok::<(), creche_contracts::config::PathError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::SocketPath;
    ///
    /// let path = SocketPath(String::from("sessiond.sock"));
    /// ```
    SocketPath,
    Some(SOCKET_PATH_MAX)
}

path_type! {
    /// The path of a directory: absolute, with no NUL byte.
    ///
    /// ```
    /// use creche_contracts::config::DirPath;
    ///
    /// let path: DirPath = "/srv/agents/state/rework/".parse()?;
    /// assert_eq!(path.as_str(), "/srv/agents/state/rework");
    /// # Ok::<(), creche_contracts::config::PathError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::DirPath;
    ///
    /// let path = DirPath(String::from("state"));
    /// ```
    DirPath,
    None
}

impl DirPath {
    /// The directory of one entry of this directory. `None` when `name` is
    /// not one plain segment: it is empty, `.` or `..`, or it holds a slash
    /// or a NUL byte.
    #[must_use]
    pub fn join(&self, name: &str) -> Option<Self> {
        let plain = !name.is_empty()
            && name != SAME_DIRECTORY
            && name != ".."
            && !name.contains([PATH_SEPARATOR, '\0']);

        plain.then(|| self.child(name))
    }

    /// The directory of one entry of this directory, with no check of the
    /// name. The caller gives a constant of the code or the text of an id
    /// type, so the name is one plain segment.
    pub(super) fn child(&self, name: &str) -> Self {
        if self.0.ends_with(PATH_SEPARATOR) {
            return Self(format!("{}{name}", self.0));
        }

        Self(format!("{}/{name}", self.0))
    }
}

path_type! {
    /// The path of a file that holds one token or one key: absolute, with no
    /// NUL byte. The config holds the path and never the secret (invariant
    /// 13).
    ///
    /// ```
    /// use creche_contracts::config::TokenFilePath;
    ///
    /// let path: TokenFilePath = "/srv/agents/state/rework/tokens/door-owui.token".parse()?;
    /// assert!(path.as_str().ends_with("door-owui.token"));
    /// # Ok::<(), creche_contracts::config::PathError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::TokenFilePath;
    ///
    /// let path = TokenFilePath(String::from("token"));
    /// ```
    TokenFilePath,
    None
}

path_type! {
    /// The path of a file that holds no secret: absolute, with no NUL byte.
    ///
    /// ```
    /// use creche_contracts::config::FilePath;
    ///
    /// let path: FilePath = "/var/lib/creche-handover/upstreams.yaml".parse()?;
    /// assert!(path.as_str().ends_with("upstreams.yaml"));
    /// # Ok::<(), creche_contracts::config::PathError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::config::FilePath;
    ///
    /// let path = FilePath(String::from("upstreams.yaml"));
    /// ```
    FilePath,
    None
}

// --- a duration ---

// CONTRACT-QUESTION: contract 03 §11.4 rule 4 and the other sections that
// give a count of seconds do not say which numbers are permitted. Python's
// `float` reads `nan`, `inf` and `1e999`, and each Python service accepts
// them: `nan <= 0` is false. The type refuses a value that is not finite and
// a value that rounds to no time. `Duration::from_secs_f64` stops the process
// on the first, and a loop with the second never waits.
/// A count of seconds that a config gives: finite and more than zero.
///
/// The value always converts to a [`Duration`] of 1 nanosecond or more.
///
/// ```
/// use creche_contracts::config::Seconds;
///
/// let stale: Seconds = "20".parse()?;
/// assert_eq!(stale.as_secs_f64(), 20.0);
/// assert!("0".parse::<Seconds>().is_err());
/// assert!("nan".parse::<Seconds>().is_err());
/// # Ok::<(), creche_contracts::config::SecondsError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::Seconds;
///
/// let stale = Seconds(f64::NAN);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, PartialOrd)]
pub struct Seconds(f64);

impl Seconds {
    /// The count of seconds.
    #[must_use]
    pub fn as_secs_f64(self) -> f64 {
        self.0
    }

    /// The value as a duration of the standard library.
    #[must_use]
    pub fn as_duration(self) -> Duration {
        // The constructor made sure that the conversion passes.
        Duration::try_from_secs_f64(self.0).unwrap_or(Duration::MAX)
    }

    /// A count of seconds that a test holds as a constant. `None` for a
    /// value that the type refuses.
    #[cfg(test)]
    pub(super) fn fixed(seconds: f64) -> Option<Self> {
        Self::try_from(seconds).ok()
    }
}

impl TryFrom<f64> for Seconds {
    type Error = SecondsError;

    fn try_from(seconds: f64) -> Result<Self, Self::Error> {
        if !seconds.is_finite() {
            return Err(SecondsError::NotFinite);
        }

        if seconds <= 0.0 {
            return Err(SecondsError::NotPositive);
        }

        match Duration::try_from_secs_f64(seconds) {
            Ok(duration) if !duration.is_zero() => Ok(Self(seconds)),
            Ok(_) => Err(SecondsError::NotPositive),
            Err(_) => Err(SecondsError::TooLong),
        }
    }
}

impl FromStr for Seconds {
    type Err = SecondsError;

    /// Reads the text as Python's `float` does, then checks the number.
    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::try_from(pytext::float(text).ok_or(SecondsError::NotANumber)?)
    }
}

/// Why a text or a number is not a count of seconds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecondsError {
    /// The text is not a decimal number.
    NotANumber,
    /// The number is infinite, or it is not a number.
    NotFinite,
    /// The number is zero or less, or it is less than 1 nanosecond.
    NotPositive,
    /// The number is more than a duration can hold.
    TooLong,
}

impl fmt::Display for SecondsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotANumber => f.write_str("a count of seconds is a decimal number"),
            Self::NotFinite => f.write_str("a count of seconds is finite"),
            Self::NotPositive => f.write_str("a count of seconds is more than zero"),
            Self::TooLong => f.write_str("a count of seconds fits a duration"),
        }
    }
}

impl Error for SecondsError {}

// --- a URL ---

/// The two schemes that a URL of a config can have.
const URL_SCHEMES: [&str; 2] = ["http://", "https://"];

/// The characters that end the authority of a URL.
const AUTHORITY_END: [char; 3] = ['/', '?', '#'];

/// What separates the user of a URL from its host.
const USER_SEPARATOR: char = '@';

// CONTRACT-QUESTION: no contract gives a config URL a grammar. The three
// doors check only the scheme, so `http://` alone passes there. `attendance`,
// the noticeboard and the chaperone check nothing. The type demands a scheme
// and a host. It refuses a user part, because a password in a URL breaks
// invariant 13. A laxer reading makes the first request fail after the config
// parse passed.
/// The base URL of an HTTP service: `http://` or `https://`, then a host.
///
/// The type keeps the text as the config gives it. [`HttpUrl::base`] gives
/// the text with no final slash, as the Python doors use it. The type
/// refuses a URL with a user part, a space or a control character: a URL
/// holds no secret (invariant 13).
///
/// ```
/// use creche_contracts::config::HttpUrl;
///
/// let url: HttpUrl = "http://192.0.2.10:8300/".parse()?;
/// assert_eq!(url.as_str(), "http://192.0.2.10:8300/");
/// assert_eq!(url.base(), "http://192.0.2.10:8300");
/// assert!("ftp://192.0.2.10".parse::<HttpUrl>().is_err());
/// assert!("http://user:password@192.0.2.10".parse::<HttpUrl>().is_err());
/// # Ok::<(), creche_contracts::config::HttpUrlError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::HttpUrl;
///
/// let url = HttpUrl(String::from("ftp://192.0.2.10"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct HttpUrl(String);

impl HttpUrl {
    /// The URL as the config gives it.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The URL with no final slash: the form that a client joins a path to.
    #[must_use]
    pub fn base(&self) -> &str {
        self.0.trim_end_matches('/')
    }

    /// The URL of a service on the LAN address: `http://<address>:<port>`.
    #[must_use]
    pub fn on_lan(address: &LanAddress, port: Port) -> Self {
        Self(format!("http://{address}:{port}"))
    }
}

impl FromStr for HttpUrl {
    type Err = HttpUrlError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let rest = URL_SCHEMES
            .iter()
            .find_map(|scheme| text.strip_prefix(scheme))
            .ok_or(HttpUrlError::NoScheme)?;
        if text
            .chars()
            .any(|character| character.is_control() || pytext::is_space(character))
        {
            return Err(HttpUrlError::BadCharacter);
        }

        let authority = rest.split(AUTHORITY_END).next().unwrap_or(rest);
        if authority.is_empty() {
            return Err(HttpUrlError::NoHost);
        }

        if authority.contains(USER_SEPARATOR) {
            return Err(HttpUrlError::UserPart);
        }

        Ok(Self(text.to_owned()))
    }
}

impl fmt::Display for HttpUrl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not the URL of an HTTP service.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HttpUrlError {
    /// The text does not start with `http://` or `https://`.
    NoScheme,
    /// The text has no host after the scheme.
    NoHost,
    /// The text has a user part before the host.
    UserPart,
    /// The text holds a space or a control character.
    BadCharacter,
}

impl fmt::Display for HttpUrlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoScheme => f.write_str("a URL starts with http:// or https://"),
            Self::NoHost => f.write_str("a URL has a host after its scheme"),
            Self::UserPart => f.write_str("a URL has no user part"),
            Self::BadCharacter => f.write_str("a URL holds no space and no control character"),
        }
    }
}

impl Error for HttpUrlError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_lan_address_is_an_ipv4_address_or_a_host_name() {
        for text in [
            "192.0.2.10",
            "127.0.0.1",
            "255.255.255.255",
            "localhost",
            "host-1.example",
            "a",
            "A1",
            "1a",
            "0.a",
            "x.0y",
            &"a".repeat(63),
        ] {
            let address: LanAddress = text.parse().unwrap();

            assert_eq!(address.as_str(), text);
            assert_eq!(address.to_string(), text);
        }
    }

    #[test]
    fn a_text_that_is_no_lan_address_is_refused() {
        for (text, error) in [
            ("", LanAddressError::Empty),
            ("192.0.2.10\n", LanAddressError::BadLabel { at: 3 }),
            (" 192.0.2.10", LanAddressError::BadLabel { at: 0 }),
            ("192.0.2.10:8300", LanAddressError::BadLabel { at: 3 }),
            ("http://192.0.2.10", LanAddressError::BadLabel { at: 0 }),
            ("a/b", LanAddressError::BadLabel { at: 0 }),
            ("a..b", LanAddressError::BadLabel { at: 1 }),
            ("a.", LanAddressError::BadLabel { at: 1 }),
            (".a", LanAddressError::BadLabel { at: 0 }),
            ("-a", LanAddressError::BadLabel { at: 0 }),
            ("a-", LanAddressError::BadLabel { at: 0 }),
            ("my_host", LanAddressError::BadLabel { at: 0 }),
            ("::1", LanAddressError::BadLabel { at: 0 }),
            ("１９２.0.2.10", LanAddressError::BadLabel { at: 0 }),
            (&"a".repeat(64), LanAddressError::BadLabel { at: 0 }),
            ("0.0.0.0", LanAddressError::EachInterface),
            ("0", LanAddressError::NotIpv4),
            ("0.0", LanAddressError::NotIpv4),
            ("192.0.2", LanAddressError::NotIpv4),
            ("192.0.2.256", LanAddressError::NotIpv4),
            ("192.0.2.010", LanAddressError::NotIpv4),
            ("192.0.2.10.5", LanAddressError::NotIpv4),
            ("0x0", LanAddressError::NotIpv4),
            ("host.0x7f", LanAddressError::NotIpv4),
            ("host.123", LanAddressError::NotIpv4),
        ] {
            assert_eq!(text.parse::<LanAddress>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn a_bind_host_is_an_ip_address_or_a_host_name() {
        for text in [
            "192.0.2.10",
            "127.0.0.1",
            "::1",
            "2001:db8::10",
            "::ffff:192.0.2.10",
            "localhost",
            "host-1.example",
        ] {
            let host: BindHost = text.parse().unwrap();

            assert_eq!(host.as_str(), text);
            assert_eq!(host.to_string(), text);
        }
    }

    #[test]
    fn a_bind_host_is_never_each_interface() {
        for text in [
            "0.0.0.0",
            "::",
            "[::]",
            "*",
            "::0",
            "0::",
            "0:0:0:0:0:0:0:0",
            "::ffff:0.0.0.0",
            "::0.0.0.0",
        ] {
            assert_eq!(
                text.parse::<BindHost>().unwrap_err(),
                BindHostError::EachInterface,
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_text_that_is_no_bind_host_is_refused() {
        assert_eq!("".parse::<BindHost>().unwrap_err(), BindHostError::Empty);
        for text in [
            "0",
            "0.0",
            "0x0",
            "00.0.0.0",
            "my_host",
            "a b",
            "192.0.2.10 ",
            "192.0.2.10:80",
            "[::1]",
            "fe80::1%eth0",
            "http://192.0.2.10",
        ] {
            assert_eq!(
                text.parse::<BindHost>().unwrap_err(),
                BindHostError::NotAHost,
                "{text:?}"
            );
        }
    }

    #[test]
    fn the_noticeboard_reads_three_texts_as_loopback() {
        for text in ["127.0.0.1", "::1", "localhost"] {
            assert!(text.parse::<BindHost>().unwrap().is_loopback_text());
        }

        for text in ["127.0.0.2", "192.0.2.10", "LOCALHOST", "0:0:0:0:0:0:0:1"] {
            assert!(!text.parse::<BindHost>().unwrap().is_loopback_text());
        }
    }

    #[test]
    fn a_lan_address_is_a_bind_host() {
        let address: LanAddress = "192.0.2.10".parse().unwrap();

        assert_eq!(BindHost::from(address).as_str(), "192.0.2.10");
    }

    #[test]
    fn a_port_is_1_to_65535() {
        for (text, port) in [
            ("1", 1),
            ("8350", 8350),
            ("65535", 65535),
            (" 8350 ", 8350),
            ("+80", 80),
            ("0080", 80),
            ("8_350", 8350),
        ] {
            assert_eq!(text.parse::<Port>().unwrap().get(), port, "{text:?}");
        }

        assert_eq!(Port::fixed(8300).unwrap().to_string(), "8300");
        assert_eq!(Port::fixed(0), None);

        // Python's `int` reads 4300 digits at most. A zero at the start is
        // a digit.
        let zeros = "0".repeat(4298);

        assert_eq!(format!("{zeros}80").parse::<Port>().unwrap().get(), 80);
        assert_eq!(
            format!("{zeros}080").parse::<Port>().unwrap_err(),
            PortError::NotANumber
        );
    }

    #[test]
    fn a_text_that_is_no_port_is_refused() {
        // Python's `int` does not remove U+001C to U+001F.
        for text in [
            "",
            "http",
            "80.0",
            "0x50",
            "８３５０",
            "8350a",
            "\u{1f}8350",
            "8350\u{1c}",
        ] {
            assert_eq!(
                text.parse::<Port>().unwrap_err(),
                PortError::NotANumber,
                "{text:?}"
            );
        }

        for text in ["0", "-1", "65536", "99999999999999999999999"] {
            assert_eq!(
                text.parse::<Port>().unwrap_err(),
                PortError::OutOfRange,
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_bind_address_is_a_host_and_a_port() {
        for (text, host, port) in [
            ("192.0.2.10:8300", "192.0.2.10", 8300),
            ("127.0.0.1:8340", "127.0.0.1", 8340),
            ("localhost:1", "localhost", 1),
            ("[::1]:8340", "::1", 8340),
            ("::1:8340", "::1", 8340),
            ("[2001:db8::10]:65535", "2001:db8::10", 65535),
        ] {
            let bind: BindAddress = text.parse().unwrap();

            assert_eq!(bind.host().as_str(), host, "{text:?}");
            assert_eq!(bind.port().get(), port, "{text:?}");
        }

        let bind = BindAddress::on("192.0.2.10".parse().unwrap(), Port::fixed(8300).unwrap());

        assert_eq!(bind.to_string(), "192.0.2.10:8300");
        for text in ["[::1]:8340", "::1:8340"] {
            let bind: BindAddress = text.parse().unwrap();

            assert_eq!(bind.to_string(), "[::1]:8340");
            assert_eq!(bind.to_string().parse(), Ok(bind));
        }
    }

    #[test]
    fn a_text_that_is_no_bind_address_is_refused() {
        let each = BindAddressError::Host(BindHostError::EachInterface);
        for (text, error) in [
            ("", BindAddressError::NoPort),
            ("192.0.2.10", BindAddressError::NoPort),
            (":8300", BindAddressError::Host(BindHostError::Empty)),
            ("0.0.0.0:8300", each),
            ("*:8300", each),
            (":::8300", each),
            ("[::]:8300", each),
            ("[::0]:8300", each),
            ("[::1:8300", BindAddressError::Host(BindHostError::NotAHost)),
            ("192.0.2.10:", BindAddressError::Port(PortError::NotANumber)),
            (
                "192.0.2.10:http",
                BindAddressError::Port(PortError::NotANumber),
            ),
            (
                "192.0.2.10:0",
                BindAddressError::Port(PortError::OutOfRange),
            ),
            (
                "192.0.2.10:65536",
                BindAddressError::Port(PortError::OutOfRange),
            ),
            (
                "192.0.2.10:\u{1f}8300",
                BindAddressError::Port(PortError::NotANumber),
            ),
            (
                "192.0.2.10:8300\u{1c}",
                BindAddressError::Port(PortError::NotANumber),
            ),
        ] {
            assert_eq!(text.parse::<BindAddress>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn a_path_takes_the_normal_form_of_pathlib() {
        for (text, normal) in [
            ("/", "/"),
            ("/srv/agents", "/srv/agents"),
            ("/srv/agents/", "/srv/agents"),
            ("/srv//agents", "/srv/agents"),
            ("/srv/./agents/.", "/srv/agents"),
            ("/srv/../agents", "/srv/../agents"),
            ("//srv/agents", "//srv/agents"),
            ("///srv/agents", "/srv/agents"),
            ("//", "//"),
            ("/a b/ü", "/a b/ü"),
        ] {
            assert_eq!(
                text.parse::<DirPath>().unwrap().as_str(),
                normal,
                "{text:?}"
            );
            assert_eq!(text.parse::<FilePath>().unwrap().to_string(), normal);
            assert_eq!(text.parse::<TokenFilePath>().unwrap().as_str(), normal);
            assert_eq!(
                text.parse::<SocketPath>().unwrap().as_path(),
                std::path::Path::new(normal)
            );
        }
    }

    #[test]
    fn a_text_that_is_no_path_is_refused() {
        for (text, error) in [
            ("", PathError::Empty),
            ("sock/sessiond.sock", PathError::NotAbsolute),
            ("./sessiond.sock", PathError::NotAbsolute),
            (" /srv", PathError::NotAbsolute),
            ("/srv/a\0b", PathError::Nul),
        ] {
            assert_eq!(text.parse::<DirPath>().unwrap_err(), error, "{text:?}");
            assert_eq!(text.parse::<SocketPath>().unwrap_err(), error, "{text:?}");
            assert_eq!(text.parse::<FilePath>().unwrap_err(), error, "{text:?}");
            assert_eq!(
                text.parse::<TokenFilePath>().unwrap_err(),
                error,
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_socket_path_has_107_bytes_or_less() {
        let longest = format!("/{}", "a".repeat(106));
        let too_long = format!("/{}", "a".repeat(107));

        assert_eq!(longest.parse::<SocketPath>().unwrap().as_str(), longest);
        assert_eq!(
            too_long.parse::<SocketPath>().unwrap_err(),
            PathError::TooLong
        );
        assert_eq!(too_long.parse::<DirPath>().unwrap().as_str(), too_long);
    }

    #[test]
    fn a_directory_joins_one_plain_segment() {
        let root: DirPath = "/srv/agents/state/rework".parse().unwrap();

        assert_eq!(
            root.join("families").unwrap().as_str(),
            "/srv/agents/state/rework/families"
        );
        for (base, joined) in [("/", "/a"), ("//", "//a"), ("//net", "//net/a")] {
            let base: DirPath = base.parse().unwrap();

            assert_eq!(base.join("a").unwrap().as_str(), joined);
            assert_eq!(base.child("a").as_str(), joined);
        }

        for name in ["", ".", "..", "a/b", "/a", "a\0"] {
            assert_eq!(root.join(name), None, "{name:?}");
        }
    }

    #[test]
    fn a_count_of_seconds_is_finite_and_more_than_zero() {
        for (text, seconds) in [
            ("20", 20.0),
            ("1.0", 1.0),
            (" 0.25 ", 0.25),
            ("1e3", 1000.0),
            ("1_0", 10.0),
            ("1e-9", 1e-9),
        ] {
            let value: Seconds = text.parse().unwrap();

            assert_eq!(value.as_secs_f64(), seconds, "{text:?}");
            assert_eq!(value.as_duration(), Duration::from_secs_f64(seconds));
        }

        assert_eq!(Seconds::fixed(30.0).unwrap().as_secs_f64(), 30.0);
        assert_eq!(Seconds::fixed(0.0), None);
    }

    #[test]
    fn a_text_that_is_no_count_of_seconds_is_refused() {
        for (text, error) in [
            ("", SecondsError::NotANumber),
            ("soon", SecondsError::NotANumber),
            ("1s", SecondsError::NotANumber),
            ("١", SecondsError::NotANumber),
            ("nan", SecondsError::NotFinite),
            ("inf", SecondsError::NotFinite),
            ("-inf", SecondsError::NotFinite),
            ("1e999", SecondsError::NotFinite),
            ("0", SecondsError::NotPositive),
            ("-1", SecondsError::NotPositive),
            ("-0.0", SecondsError::NotPositive),
            ("1e-400", SecondsError::NotPositive),
            ("1e-12", SecondsError::NotPositive),
            ("1e30", SecondsError::TooLong),
        ] {
            assert_eq!(text.parse::<Seconds>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn a_url_has_a_scheme_and_a_host_and_a_base_with_no_final_slash() {
        for (text, url) in [
            ("http://192.0.2.10:8300", "http://192.0.2.10:8300"),
            ("http://192.0.2.10:8300/", "http://192.0.2.10:8300"),
            ("https://host.example//", "https://host.example"),
            ("http://sessiond", "http://sessiond"),
            ("http://host/hook/approval", "http://host/hook/approval"),
            ("http://[::1]:8350", "http://[::1]:8350"),
            ("http://host/a?b=c@d", "http://host/a?b=c@d"),
        ] {
            let parsed: HttpUrl = text.parse().unwrap();

            assert_eq!(parsed.base(), url, "{text:?}");
            assert_eq!(parsed.as_str(), text);
            assert_eq!(parsed.to_string(), text);
        }

        let address: LanAddress = "192.0.2.10".parse().unwrap();

        assert_eq!(
            HttpUrl::on_lan(&address, Port::fixed(8300).unwrap()).as_str(),
            "http://192.0.2.10:8300"
        );
    }

    #[test]
    fn a_text_that_is_no_url_is_refused() {
        for (text, error) in [
            ("", HttpUrlError::NoScheme),
            ("192.0.2.10:8300", HttpUrlError::NoScheme),
            ("ftp://192.0.2.10", HttpUrlError::NoScheme),
            ("HTTP://192.0.2.10", HttpUrlError::NoScheme),
            (" http://192.0.2.10", HttpUrlError::NoScheme),
            ("http://", HttpUrlError::NoHost),
            ("http:///path", HttpUrlError::NoHost),
            ("https://?query", HttpUrlError::NoHost),
            ("http://user@192.0.2.10", HttpUrlError::UserPart),
            ("http://user:password@192.0.2.10/", HttpUrlError::UserPart),
            ("http://192.0.2.10/a b", HttpUrlError::BadCharacter),
            ("http://192.0.2.10\n", HttpUrlError::BadCharacter),
            ("http://192.0.2.10/\u{7f}", HttpUrlError::BadCharacter),
        ] {
            assert_eq!(text.parse::<HttpUrl>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn each_error_says_which_rule_failed() {
        let errors: [Box<dyn Error>; 7] = [
            Box::new(LanAddressError::BadLabel { at: 2 }),
            Box::new(BindHostError::EachInterface),
            Box::new(PortError::OutOfRange),
            Box::new(BindAddressError::Port(PortError::NotANumber)),
            Box::new(PathError::TooLong),
            Box::new(SecondsError::NotFinite),
            Box::new(HttpUrlError::UserPart),
        ];
        let texts: Vec<String> = errors.iter().map(ToString::to_string).collect();

        assert_eq!(
            texts,
            [
                "label 2 of a LAN address is not 1 to 63 letters, digits and hyphens",
                "a bind host is the LAN address or loopback, not each interface",
                "a port is 1 to 65535",
                "after the last colon, a port is a decimal number",
                "the path of a Unix socket has 107 bytes or less",
                "a count of seconds is finite",
                "a URL has no user part",
            ]
        );
    }
}
