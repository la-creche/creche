//! The address of another service: the names of five variables, and the URL
//! type of a plane.
//!
//! A service takes each address of another service from its environment
//! (`rust/AGENTS.md`, "The rules for a service"). Each constant below is
//! the one home of the name of one such variable. The module of a daemon
//! reads the name from here and holds no copy of the text.
//!
//! Other variables hold an address too, for example the URL of `attendance`
//! that a door reads. Each of those names has its one home in the module of
//! the daemon that reads it.
//!
//! A plane is a service of the host that each sandbox calls: the chaperone
//! and LiteLLM (contract 01 §3.7 rule 4). [`PlaneUrl`] is the type of the two
//! variables that name a plane.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::values::{
    AUTHORITY_END, HTTP_SCHEME, LanAddress, LanAddressError, PORT_SEPARATOR, Port, USER_SEPARATOR,
};

/// The variable that holds the URL of the chaperone. The chaperone is one of
/// the two planes.
pub const AGENT_PEP_URL: &str = "AGENT_PEP_URL";
/// The variable that holds the base URL of LiteLLM. LiteLLM is one of the
/// two planes.
pub const AGENT_LITELLM_BASE_URL: &str = "AGENT_LITELLM_BASE_URL";
/// The variable that holds the URL of TEI, the embeddings service. The name
/// has no `AGENT_` at its start: the Python package `library` reads this
/// name, and one value has one name.
pub const TEI_URL: &str = "TEI_URL";
/// The variable of the site file that holds the URL of Home Assistant.
pub const AGENT_HA_URL: &str = "AGENT_HA_URL";
/// The variable that holds the URL of the hook of an approval gate. The
/// chaperone calls that hook.
pub const PEP_APPROVAL_URL: &str = "PEP_APPROVAL_URL";

/// The first digit of a port that the type refuses: one port has one text.
const LEADING_ZERO: char = '0';

// CONTRACT-QUESTION: contract 01 §3.7 rule 4 names the two plane endpoints
// as `host:port`. No contract gives the URL of a plane a grammar, and no
// Python reader holds one. The type takes the narrowest reading: `http://`,
// a host, a colon and a port, and no other byte. The port is decimal digits
// with no sign and no zero at its start, so one endpoint has one text. A
// laxer reading, for example a final slash or a port of `08300`, costs one
// function: `from_str`.
/// The URL of a plane: `http://<host>:<port>`, and no other byte.
///
/// The host has the form of a [`LanAddress`]: an IPv4 address or a host
/// name. The text must give the port, as decimal digits with no sign and no
/// zero at its start. The type refuses `https`, a user part, a path, a
/// query and a fragment. A final slash is a path.
///
/// One endpoint thus has one text. [`PlaneUrl::endpoint`] gives the
/// `host:port` of contract 01 §3.7 rule 4, and `Display` gives the text of
/// the URL again.
///
/// ```
/// use creche_contracts::config::endpoints::PlaneUrl;
///
/// let url: PlaneUrl = "http://192.0.2.10:8300".parse()?;
/// assert_eq!(url.endpoint(), "192.0.2.10:8300");
/// assert_eq!(url.to_string(), "http://192.0.2.10:8300");
/// assert!("http://192.0.2.10".parse::<PlaneUrl>().is_err());
/// assert!("http://192.0.2.10:8300/".parse::<PlaneUrl>().is_err());
/// # Ok::<(), creche_contracts::config::endpoints::PlaneUrlError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::endpoints::PlaneUrl;
///
/// let url = PlaneUrl {
///     host: "192.0.2.10".parse().unwrap(),
///     port: "8300".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PlaneUrl {
    host: LanAddress,
    port: Port,
}

impl PlaneUrl {
    /// The host: an IPv4 address or a host name.
    #[must_use]
    pub fn host(&self) -> &LanAddress {
        &self.host
    }

    /// The port.
    #[must_use]
    pub fn port(&self) -> Port {
        self.port
    }

    /// The endpoint of the plane, `host:port`: the form in which an egress
    /// list names a host (contract 01 §3.7).
    #[must_use]
    pub fn endpoint(&self) -> String {
        format!("{}{PORT_SEPARATOR}{}", self.host, self.port)
    }
}

/// The port of a plane URL, from the text after the last colon.
fn plane_port(text: &str) -> Result<Port, PlaneUrlError> {
    if text.is_empty() {
        return Err(PlaneUrlError::NoPort);
    }

    let digits_only = text.bytes().all(|byte| byte.is_ascii_digit());
    if !digits_only || text.starts_with(LEADING_ZERO) {
        return Err(PlaneUrlError::PortForm);
    }

    text.parse().map_err(|_| PlaneUrlError::PortRange)
}

impl FromStr for PlaneUrl {
    type Err = PlaneUrlError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let rest = text
            .strip_prefix(HTTP_SCHEME)
            .ok_or(PlaneUrlError::NotHttp)?;
        let authority = rest.split(AUTHORITY_END).next().unwrap_or(rest);
        if authority.contains(USER_SEPARATOR) {
            return Err(PlaneUrlError::UserPart);
        }

        if authority.len() != rest.len() {
            return Err(PlaneUrlError::AfterAuthority);
        }

        let (host, port) = rest.rsplit_once(PORT_SEPARATOR).unwrap_or((rest, ""));
        let host = host.parse().map_err(PlaneUrlError::Host)?;
        let port = plane_port(port)?;

        Ok(Self { host, port })
    }
}

impl fmt::Display for PlaneUrl {
    /// Writes the URL. The text is the text that the parse took.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{HTTP_SCHEME}{}{PORT_SEPARATOR}{}", self.host, self.port)
    }
}

/// Why a text is not the URL of a plane.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlaneUrlError {
    /// The text does not start with `http://`.
    NotHttp,
    /// The text has a user part before the host.
    UserPart,
    /// The text has a path, a query or a fragment after the host and the
    /// port.
    AfterAuthority,
    /// The host is not an IPv4 address and not a host name.
    Host(LanAddressError),
    /// The text gives no port after the host.
    NoPort,
    /// The port is not decimal digits only, or it starts with a zero.
    PortForm,
    /// The port is not 1 to 65535.
    PortRange,
}

impl fmt::Display for PlaneUrlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotHttp => write!(f, "a plane URL starts with {HTTP_SCHEME}"),
            Self::UserPart => f.write_str("a plane URL has no user part"),
            Self::AfterAuthority => {
                f.write_str("a plane URL has no path, no query and no fragment")
            }
            Self::Host(error) => write!(f, "in the host of a plane URL, {error}"),
            Self::NoPort => f.write_str("a plane URL gives its port"),
            Self::PortForm => {
                f.write_str("the port of a plane URL is decimal digits with no zero at its start")
            }
            Self::PortRange => f.write_str("the port of a plane URL is 1 to 65535"),
        }
    }
}

impl Error for PlaneUrlError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Host(error) => Some(error),
            Self::NotHttp
            | Self::UserPart
            | Self::AfterAuthority
            | Self::NoPort
            | Self::PortForm
            | Self::PortRange => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::values::HttpUrl;
    use super::super::{ConfigError, ConfigErrors, Env};
    use super::*;

    #[test]
    fn each_name_is_the_text_that_the_services_read() {
        assert_eq!(AGENT_PEP_URL, "AGENT_PEP_URL");
        assert_eq!(AGENT_LITELLM_BASE_URL, "AGENT_LITELLM_BASE_URL");
        assert_eq!(TEI_URL, "TEI_URL");
        assert_eq!(AGENT_HA_URL, "AGENT_HA_URL");
        assert_eq!(PEP_APPROVAL_URL, "PEP_APPROVAL_URL");
    }

    #[test]
    fn a_plane_url_is_http_a_host_and_a_port() {
        for (text, host, port) in [
            ("http://192.0.2.10:8300", "192.0.2.10", 8300),
            ("http://192.0.2.10:4000", "192.0.2.10", 4000),
            ("http://127.0.0.1:1", "127.0.0.1", 1),
            ("http://localhost:65535", "localhost", 65535),
            ("http://host-1.example:80", "host-1.example", 80),
            ("http://A1:8300", "A1", 8300),
        ] {
            let url: PlaneUrl = text.parse().unwrap();

            assert_eq!(url.host().as_str(), host, "{text:?}");
            assert_eq!(url.port().get(), port, "{text:?}");
            assert_eq!(url.endpoint(), format!("{host}:{port}"), "{text:?}");
            assert_eq!(url.to_string(), text);
        }
    }

    #[test]
    fn a_text_that_is_no_plane_url_is_refused() {
        let long_port = format!("http://192.0.2.10:{}", "9".repeat(5000));
        for (text, error) in [
            ("", PlaneUrlError::NotHttp),
            ("192.0.2.10:8300", PlaneUrlError::NotHttp),
            ("https://192.0.2.10:8300", PlaneUrlError::NotHttp),
            ("HTTP://192.0.2.10:8300", PlaneUrlError::NotHttp),
            ("http:/192.0.2.10:8300", PlaneUrlError::NotHttp),
            (" http://192.0.2.10:8300", PlaneUrlError::NotHttp),
            ("http://user@192.0.2.10:8300", PlaneUrlError::UserPart),
            ("http://user:word@192.0.2.10:8300", PlaneUrlError::UserPart),
            ("http://user@192.0.2.10:8300/v1", PlaneUrlError::UserPart),
            ("http://192.0.2.10:8300/", PlaneUrlError::AfterAuthority),
            ("http://192.0.2.10:8300/v1", PlaneUrlError::AfterAuthority),
            ("http://192.0.2.10:8300?a=b", PlaneUrlError::AfterAuthority),
            ("http://192.0.2.10:8300#top", PlaneUrlError::AfterAuthority),
            ("http://192.0.2.10/:8300", PlaneUrlError::AfterAuthority),
            ("http://", PlaneUrlError::Host(LanAddressError::Empty)),
            ("http://:8300", PlaneUrlError::Host(LanAddressError::Empty)),
            (
                "http://0.0.0.0:8300",
                PlaneUrlError::Host(LanAddressError::EachInterface),
            ),
            (
                "http://192.0.2:8300",
                PlaneUrlError::Host(LanAddressError::NotIpv4),
            ),
            (
                "http://[::1]:8300",
                PlaneUrlError::Host(LanAddressError::BadLabel { at: 0 }),
            ),
            (
                "http://::1:8300",
                PlaneUrlError::Host(LanAddressError::BadLabel { at: 0 }),
            ),
            (
                "http://my_host:8300",
                PlaneUrlError::Host(LanAddressError::BadLabel { at: 0 }),
            ),
            (
                "http://192.0.2.10 :8300",
                PlaneUrlError::Host(LanAddressError::BadLabel { at: 3 }),
            ),
            (
                "http://192.0.2.10:8300:8300",
                PlaneUrlError::Host(LanAddressError::BadLabel { at: 3 }),
            ),
            (
                "http://１９２.0.2.10:8300",
                PlaneUrlError::Host(LanAddressError::BadLabel { at: 0 }),
            ),
            ("http://192.0.2.10", PlaneUrlError::NoPort),
            ("http://192.0.2.10:", PlaneUrlError::NoPort),
            ("http://localhost", PlaneUrlError::NoPort),
            ("http://192.0.2.10:8300\n", PlaneUrlError::PortForm),
            ("http://192.0.2.10:８３００", PlaneUrlError::PortForm),
            ("http://192.0.2.10:+8300", PlaneUrlError::PortForm),
            ("http://192.0.2.10:-1", PlaneUrlError::PortForm),
            ("http://192.0.2.10:8_300", PlaneUrlError::PortForm),
            ("http://192.0.2.10: 8300", PlaneUrlError::PortForm),
            ("http://192.0.2.10:8300 ", PlaneUrlError::PortForm),
            ("http://192.0.2.10:08300", PlaneUrlError::PortForm),
            ("http://192.0.2.10:0", PlaneUrlError::PortForm),
            ("http://192.0.2.10:0x50", PlaneUrlError::PortForm),
            ("http://192.0.2.10:http", PlaneUrlError::PortForm),
            ("http://192.0.2.10:65536", PlaneUrlError::PortRange),
            (
                "http://192.0.2.10:99999999999999999999",
                PlaneUrlError::PortRange,
            ),
            (long_port.as_str(), PlaneUrlError::PortRange),
        ] {
            assert_eq!(text.parse::<PlaneUrl>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn the_text_of_a_plane_url_is_the_url_that_a_client_calls() {
        let url: PlaneUrl = "http://192.0.2.10:8300".parse().unwrap();
        let dialed = HttpUrl::on_lan(url.host(), url.port());

        assert_eq!(dialed.as_str(), url.to_string());
        assert_eq!(dialed.as_str().parse::<HttpUrl>().unwrap(), dialed);
    }

    #[test]
    fn each_error_has_a_text_and_holds_no_value() {
        for (error, text) in [
            (PlaneUrlError::NotHttp, "a plane URL starts with http://"),
            (PlaneUrlError::UserPart, "a plane URL has no user part"),
            (
                PlaneUrlError::AfterAuthority,
                "a plane URL has no path, no query and no fragment",
            ),
            (
                PlaneUrlError::Host(LanAddressError::Empty),
                "in the host of a plane URL, a LAN address has 1 byte or more",
            ),
            (PlaneUrlError::NoPort, "a plane URL gives its port"),
            (
                PlaneUrlError::PortForm,
                "the port of a plane URL is decimal digits with no zero at its start",
            ),
            (
                PlaneUrlError::PortRange,
                "the port of a plane URL is 1 to 65535",
            ),
        ] {
            assert_eq!(error.to_string(), text);
            assert_eq!(
                error.source().is_some(),
                matches!(error, PlaneUrlError::Host(_))
            );
        }

        let error = "http://user:word@192.0.2.10:8300"
            .parse::<PlaneUrl>()
            .unwrap_err();

        assert!(!format!("{error} {error:?}").contains("word"));
    }

    #[test]
    fn a_variable_of_a_plane_reads_into_the_type() {
        let env = Env::from_pairs([
            (AGENT_PEP_URL, " http://192.0.2.10:8300\n"),
            (AGENT_LITELLM_BASE_URL, "http://192.0.2.10:4000/"),
        ]);
        let pep = env.require::<PlaneUrl>(AGENT_PEP_URL).unwrap();
        let refused = ConfigError::PlaneUrl {
            variable: AGENT_LITELLM_BASE_URL,
            error: PlaneUrlError::AfterAuthority,
        };

        assert_eq!(pep.endpoint(), "192.0.2.10:8300");
        assert_eq!(
            env.require::<PlaneUrl>(AGENT_LITELLM_BASE_URL),
            Err(ConfigErrors::from(refused))
        );
        assert_eq!(refused.variable(), AGENT_LITELLM_BASE_URL);
        assert_eq!(
            refused.to_string(),
            "AGENT_LITELLM_BASE_URL: a plane URL has no path, no query and no fragment"
        );
        assert!(!refused.to_string().contains("192.0.2.10"));
        assert!(refused.source().is_some());
        assert_eq!(
            Env::from_pairs([(TEI_URL, "")]).require::<PlaneUrl>(AGENT_PEP_URL),
            Err(ConfigErrors::from(ConfigError::Unset {
                variable: AGENT_PEP_URL
            }))
        );
    }
}
