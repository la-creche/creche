//! The config of the noticeboard, the admin pages of the platform.
//!
//! `systemd/creche-noticeboard.service` starts the daemon with the variables
//! of `view.env` and of the site file. The Python reader is
//! `noticeboard.config.from_env`.

use super::values::{
    BindHost, DEFAULT_ATTENDANCE_SOCKET, DEFAULT_STATE_ROOT, DirPath, LanAddress, Port,
    TokenFilePath,
};
use super::{
    AtReload, AtStart, AttendanceTarget, Checked, ConfigError, ConfigErrors, Env, FailureAction,
    KeyError, LAN_ADDRESS, MIN_KEY_BYTES, Parsed, ProcessConfig, all2, all3, all4, key_of_file,
    number, pytext,
};
use crate::secret::Secret;

/// The variable that names the host of the bind. It wins over the LAN
/// address of the site file.
pub const BIND: &str = "VIEW_BIND";
/// The variable that names the port of the bind.
pub const PORT: &str = "VIEW_PORT";
/// The variable that names the state root.
pub const STATE_ROOT: &str = "VIEW_STATE_ROOT";
/// The variable that names the registry checkout.
pub const REGISTRY_DIR: &str = "VIEW_REGISTRY_DIR";
/// The variable that names the Unix socket of `attendance`.
pub const SESSIOND_SOCKET: &str = "VIEW_SESSIOND_SOCKET";
/// The variable that names the URL of `attendance`. It wins over the
/// socket.
pub const SESSIOND_URL: &str = "VIEW_SESSIOND_URL";
/// The variable that gives the count of rows on one page.
pub const PAGE_SIZE: &str = "VIEW_PAGE_SIZE";
/// The variable that turns `Secure` on the CSRF cookie off.
pub const COOKIE_SECURE: &str = "VIEW_COOKIE_SECURE";
/// The variable that holds the access key. The file wins over it.
pub const ACCESS_KEY: &str = "VIEW_ACCESS_KEY";
/// The variable that names the file of the access key.
pub const ACCESS_KEY_FILE: &str = "VIEW_ACCESS_KEY_FILE";

const DEFAULT_PORT: &str = "8370";
const DEFAULT_REGISTRY_DIR: &str = "/srv/agents/registry";

/// The count of rows on one page when the variable is not set.
const DEFAULT_PAGE_SIZE: u16 = 50;

/// The largest count of rows on one page.
const MAX_PAGE_SIZE: u16 = 500;

/// The words that turn the cookie switch off, in lower case. Each other
/// text leaves the switch on.
const OFF_WORDS: [&str; 3] = ["0", "false", "no"];

/// Whether the CSRF cookie has `Secure`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CookieSecure {
    /// The browser sends the cookie over HTTPS only. This is the default.
    On,
    /// For a development run over plain HTTP.
    Off,
}

/// The access key of the noticeboard, or where it is.
#[derive(Debug)]
pub enum AccessKey {
    /// The key itself, from `VIEW_ACCESS_KEY`. On a bind that is not
    /// loopback the key has 32 bytes or more.
    Key(Secret),
    /// The file that holds the key. The binary reads the file and gives the
    /// text to [`NoticeboardConfig::key_of_file`].
    File(TokenFilePath),
    /// No key. The type permits this only for a loopback bind.
    Open,
}

/// The config of the noticeboard: each value that the service reads from
/// its environment.
///
/// Two rules of the service are in the type:
///
/// 1. The bind is never each interface: the host is a [`BindHost`].
/// 2. A bind that is not loopback needs an access key of 32 bytes or more
///    (contract 02 §3 rule 7). A LAN admin surface with no key is an open
///    one.
///
/// FAILURE ACTION. At start, a config that is not valid stops the daemon
/// with `EX_CONFIG`, and the unit file must hold
/// `RestartPreventExitStatus=78`. The Python service exits with 2 today,
/// and `ExecStartPre` runs the same check, so systemd starts the unit again
/// after each exit, with no limit. The daemon does not read this config
/// again.
///
/// systemd reads `RestartPreventExitStatus` only for the main process. After
/// a check in `ExecStartPre` that fails, systemd starts the unit again, also
/// when the unit file holds that line. The port of this service to Rust
/// removes the `ExecStartPre` line: the main process does the same parse.
///
/// The type is stricter than the Python reader in these places: the bind
/// host is an IP address or a host name, each path is absolute, and the URL
/// of `attendance` is an [`HttpUrl`].
///
/// ```
/// use creche_contracts::config::noticeboard::NoticeboardConfig;
/// use creche_contracts::config::{ConfigError, Env};
///
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// let errors = NoticeboardConfig::from_env(&env).unwrap_err();
/// assert_eq!(errors.as_slice(), [ConfigError::OpenOnLan { variable: "VIEW_BIND" }]);
/// ```
///
/// Code outside this module cannot build a config from raw parts, and cannot
/// change a field of a valid config:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::noticeboard::NoticeboardConfig;
/// use creche_contracts::config::{ConfigError, Env};
///
/// let env = Env::from_pairs([("VIEW_BIND", "127.0.0.1")]);
/// let config = NoticeboardConfig::from_env(&env).unwrap();
/// let other = NoticeboardConfig { page_size: 0, ..config };
/// ```
#[derive(Debug)]
pub struct NoticeboardConfig {
    bind: BindHost,
    port: Port,
    state_root: DirPath,
    registry_dir: DirPath,
    attendance: AttendanceTarget,
    page_size: u16,
    cookie_secure: CookieSecure,
    access_key: AccessKey,
}

impl Checked for NoticeboardConfig {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
}

impl ProcessConfig for NoticeboardConfig {
    const UNIT: &'static str = "creche-noticeboard.service";
}

/// The host of the bind: the variable of the service, else the LAN address
/// of the site file. The service has no default host.
fn bind(env: &Env) -> Parsed<BindHost> {
    if let Some(host) = env.parse::<BindHost>(BIND)? {
        return Ok(host);
    }

    env.require::<LanAddress>(LAN_ADDRESS).map(BindHost::from)
}

/// Where `attendance` answers. A URL wins over the socket.
fn attendance(env: &Env) -> Parsed<AttendanceTarget> {
    if let Some(url) = env.parse(SESSIOND_URL)? {
        return Ok(AttendanceTarget::Url(url));
    }

    env.parse_or(SESSIOND_SOCKET, || DEFAULT_ATTENDANCE_SOCKET.parse())
        .map(AttendanceTarget::Socket)
}

fn page_size(env: &Env) -> Parsed<u16> {
    let Some(size) = number(env, PAGE_SIZE, 1, i64::from(MAX_PAGE_SIZE))? else {
        return Ok(DEFAULT_PAGE_SIZE);
    };

    // The range check made sure that the number fits.
    Ok(u16::try_from(size).unwrap_or(DEFAULT_PAGE_SIZE))
}

/// The switch is on unless the variable turns it off.
fn cookie_secure(env: &Env) -> Parsed<CookieSecure> {
    let off = env
        .text(COOKIE_SECURE)?
        .is_some_and(|text| OFF_WORDS.contains(&text.to_ascii_lowercase().as_str()));

    Ok(if off {
        CookieSecure::Off
    } else {
        CookieSecure::On
    })
}

/// The access key of the environment. The file wins over the value.
fn access_key(env: &Env) -> Parsed<AccessKey> {
    if let Some(file) = env.parse(ACCESS_KEY_FILE)? {
        return Ok(AccessKey::File(file));
    }

    let key = super::secret(env, ACCESS_KEY)?;

    Ok(key.map_or(AccessKey::Open, AccessKey::Key))
}

impl NoticeboardConfig {
    /// Parses the variables of the unit. The function collects each error.
    ///
    /// # Errors
    ///
    /// [`ConfigErrors`] holds one error for each variable that the service
    /// cannot use.
    pub fn from_env(env: &Env) -> Result<Self, ConfigErrors> {
        let listen = all3(
            bind(env),
            env.parse_or(PORT, || DEFAULT_PORT.parse()),
            access_key(env),
        );
        let roots = all2(
            env.parse_or(STATE_ROOT, || DEFAULT_STATE_ROOT.parse()),
            env.parse_or(REGISTRY_DIR, || DEFAULT_REGISTRY_DIR.parse()),
        );
        let (listen, roots, attendance, (page_size, cookie_secure)) = all4(
            listen,
            roots,
            attendance(env),
            all2(page_size(env), cookie_secure(env)),
        )?;
        let (bind, port, access_key) = listen;
        let (state_root, registry_dir) = roots;
        let open = match &access_key {
            AccessKey::Key(key) => key.expose_secret().len() < MIN_KEY_BYTES,
            AccessKey::File(_) => false,
            AccessKey::Open => true,
        };
        if open && !bind.is_loopback_text() {
            return Err(ConfigError::OpenOnLan { variable: BIND }.into());
        }

        Ok(Self {
            bind,
            port,
            state_root,
            registry_dir,
            attendance,
            page_size,
            cookie_secure,
            access_key,
        })
    }

    /// The access key of the text of the key file, for a config whose
    /// access key is [`AccessKey::File`]. `None` stands for a loopback bind
    /// with an empty file.
    ///
    /// The function removes the space at the two ends of the text. On a
    /// bind that is not loopback it refuses a key of less than 32 bytes.
    ///
    /// # Errors
    ///
    /// [`ConfigError::OpenOnLan`] for a bind that is not loopback and a key
    /// that is missing or too short.
    pub fn key_of_file(&self, text: &str) -> Result<Option<Secret>, ConfigError> {
        match key_of_file(text) {
            Ok(key) => Ok(Some(key)),
            Err(KeyError::TooShort) if self.on_loopback() => {
                Ok(Secret::try_from(pytext::strip(text).to_owned()).ok())
            }
            Err(KeyError::TooShort) => Err(ConfigError::OpenOnLan { variable: BIND }),
        }
    }

    /// The host of the bind.
    #[must_use]
    pub fn bind(&self) -> &BindHost {
        &self.bind
    }

    /// The port of the bind.
    #[must_use]
    pub fn port(&self) -> Port {
        self.port
    }

    /// Whether the bind is one of the three loopback texts. A short key, or
    /// none, is permitted there for a development run.
    #[must_use]
    pub fn on_loopback(&self) -> bool {
        self.bind.is_loopback_text()
    }

    /// The root of the state of the platform.
    #[must_use]
    pub fn state_root(&self) -> &DirPath {
        &self.state_root
    }

    /// The registry checkout.
    #[must_use]
    pub fn registry_dir(&self) -> &DirPath {
        &self.registry_dir
    }

    /// Where `attendance` answers.
    #[must_use]
    pub fn attendance(&self) -> &AttendanceTarget {
        &self.attendance
    }

    /// The count of rows on one page: 1 to 500.
    #[must_use]
    pub fn page_size(&self) -> u16 {
        self.page_size
    }

    /// Whether the CSRF cookie has `Secure`.
    #[must_use]
    pub fn cookie_secure(&self) -> CookieSecure {
        self.cookie_secure
    }

    /// The access key, or where it is.
    #[must_use]
    pub fn access_key(&self) -> &AccessKey {
        &self.access_key
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::BindHostError;

    /// A key of 32 bytes that no deployment uses.
    const KEY: &str = "0123456789abcdef0123456789abcdef";

    /// The variables that `systemd/creche-noticeboard.service` gives the
    /// daemon: the site file and a key file that `view.env` names.
    fn unit_env() -> Vec<(&'static str, &'static str)> {
        vec![
            ("HOME", "/home/operator"),
            ("XDG_CONFIG_HOME", "/home/operator/.config"),
            ("AGENT_LAN_ADDRESS", "192.0.2.10"),
            (
                ACCESS_KEY_FILE,
                "/srv/agents/state/rework/tokens/noticeboard.key",
            ),
        ]
    }

    fn with(pairs: &[(&'static str, &'static str)]) -> Parsed<NoticeboardConfig> {
        let mut env = unit_env();
        env.extend_from_slice(pairs);

        NoticeboardConfig::from_env(&Env::from_pairs(env))
    }

    fn one_error(pairs: &[(&'static str, &'static str)]) -> ConfigError {
        let errors = with(pairs).unwrap_err();

        assert_eq!(errors.as_slice().len(), 1, "{errors}");

        errors.as_slice()[0]
    }

    #[test]
    fn the_unit_gives_a_config_with_each_default() {
        let config = with(&[]).unwrap();

        assert_eq!(config.bind().as_str(), "192.0.2.10");
        assert_eq!(config.port().get(), 8370);
        assert!(!config.on_loopback());
        assert_eq!(config.state_root().as_str(), "/srv/agents/state/rework");
        assert_eq!(config.registry_dir().as_str(), "/srv/agents/registry");
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Socket(DEFAULT_ATTENDANCE_SOCKET.parse().unwrap())
        );
        assert_eq!(config.page_size(), 50);
        assert_eq!(config.cookie_secure(), CookieSecure::On);
        assert!(matches!(
            config.access_key(),
            AccessKey::File(path) if path.as_str().ends_with("noticeboard.key")
        ));
    }

    #[test]
    fn each_variable_of_the_service_wins_over_its_default() {
        let config = with(&[
            (BIND, "127.0.0.1"),
            (PORT, "18370"),
            (STATE_ROOT, "/tmp/root/state"),
            (REGISTRY_DIR, "/tmp/root/registry"),
            (SESSIOND_SOCKET, "/tmp/root/sessiond.sock"),
            (PAGE_SIZE, "500"),
            (COOKIE_SECURE, "No"),
        ])
        .unwrap();

        assert_eq!(config.bind().as_str(), "127.0.0.1");
        assert_eq!(config.port().get(), 18370);
        assert!(config.on_loopback());
        assert_eq!(config.state_root().as_str(), "/tmp/root/state");
        assert_eq!(config.registry_dir().as_str(), "/tmp/root/registry");
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Socket("/tmp/root/sessiond.sock".parse().unwrap())
        );
        assert_eq!(config.page_size(), 500);
        assert_eq!(config.cookie_secure(), CookieSecure::Off);
    }

    #[test]
    fn a_url_of_attendance_wins_over_the_socket() {
        let config = with(&[
            (SESSIOND_URL, "http://192.0.2.10:8350"),
            (SESSIOND_SOCKET, "not a path"),
        ])
        .unwrap();

        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Url("http://192.0.2.10:8350".parse().unwrap())
        );
    }

    #[test]
    fn the_cookie_switch_is_on_unless_a_word_turns_it_off() {
        for word in ["0", "false", "FALSE", "no"] {
            assert_eq!(
                with(&[(COOKIE_SECURE, word)]).unwrap().cookie_secure(),
                CookieSecure::Off
            );
        }

        for word in ["1", "true", "yes", "off", "maybe", ""] {
            assert_eq!(
                with(&[(COOKIE_SECURE, word)]).unwrap().cookie_secure(),
                CookieSecure::On
            );
        }
    }

    #[test]
    fn the_bind_is_never_each_interface() {
        for host in ["0.0.0.0", "::", "*"] {
            assert_eq!(
                one_error(&[(BIND, host)]),
                ConfigError::Host {
                    variable: BIND,
                    error: BindHostError::EachInterface
                },
                "{host}"
            );
        }
    }

    #[test]
    fn the_service_has_no_default_host() {
        let env = Env::from_pairs([(ACCESS_KEY, KEY)]);

        assert_eq!(
            NoticeboardConfig::from_env(&env).unwrap_err().as_slice(),
            [ConfigError::Unset {
                variable: LAN_ADDRESS
            }]
        );
    }

    #[test]
    fn a_bind_on_the_lan_needs_a_key_of_32_bytes() {
        let lan = |key: &'static str| {
            NoticeboardConfig::from_env(&Env::from_pairs([
                (LAN_ADDRESS, "192.0.2.10"),
                (ACCESS_KEY, key),
            ]))
        };
        let open = ConfigError::OpenOnLan { variable: BIND };

        assert!(matches!(
            lan(KEY).unwrap().access_key(),
            AccessKey::Key(key) if key.matches(KEY.as_bytes())
        ));
        assert_eq!(lan("").unwrap_err().as_slice(), [open]);
        assert_eq!(lan(KEY.get(1..).unwrap()).unwrap_err().as_slice(), [open]);
        assert_eq!(lan("  short  ").unwrap_err().as_slice(), [open]);
    }

    #[test]
    fn a_loopback_bind_takes_a_short_key_or_none() {
        let loopback = |host: &'static str, key: &'static str| {
            NoticeboardConfig::from_env(&Env::from_pairs([(BIND, host), (ACCESS_KEY, key)]))
        };

        for host in ["127.0.0.1", "::1", "localhost"] {
            assert!(matches!(
                loopback(host, "").unwrap().access_key(),
                AccessKey::Open
            ));
            assert!(matches!(
                loopback(host, "short").unwrap().access_key(),
                AccessKey::Key(key) if key.matches(b"short")
            ));
        }

        assert!(loopback("127.0.0.2", "short").is_err());
    }

    #[test]
    fn the_key_of_the_file_follows_the_same_rule() {
        let lan = with(&[]).unwrap();
        let loopback = with(&[(BIND, "127.0.0.1")]).unwrap();
        let open = ConfigError::OpenOnLan { variable: BIND };

        assert!(
            lan.key_of_file(&format!("{KEY}\n"))
                .unwrap()
                .unwrap()
                .matches(KEY.as_bytes())
        );
        assert_eq!(lan.key_of_file("").unwrap_err(), open);
        assert_eq!(lan.key_of_file("short\n").unwrap_err(), open);
        assert!(loopback.key_of_file("\n").unwrap().is_none());
        assert!(
            loopback
                .key_of_file("short\n")
                .unwrap()
                .unwrap()
                .matches(b"short")
        );
    }

    #[test]
    fn a_value_that_the_service_cannot_use_is_an_error_of_its_variable() {
        for (variable, value) in [
            (PORT, "0"),
            (PORT, "http"),
            (PAGE_SIZE, "0"),
            (PAGE_SIZE, "501"),
            (PAGE_SIZE, "many"),
            (STATE_ROOT, "state"),
            (REGISTRY_DIR, "registry"),
            (SESSIOND_SOCKET, "sessiond.sock"),
            (SESSIOND_URL, "192.0.2.10:8350"),
            (ACCESS_KEY_FILE, "noticeboard.key"),
            (BIND, "not a host"),
        ] {
            assert_eq!(
                one_error(&[(variable, value)]).variable(),
                variable,
                "{value}"
            );
        }

        assert_eq!(
            one_error(&[(PAGE_SIZE, "501")]),
            ConfigError::NotInRange {
                variable: PAGE_SIZE,
                min: 1,
                max: 500
            }
        );
    }

    #[test]
    fn the_parse_collects_the_error_of_each_variable() {
        let errors = with(&[(BIND, "0.0.0.0"), (PORT, "0"), (PAGE_SIZE, "0")]).unwrap_err();
        let variables: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(variables, [BIND, PORT, PAGE_SIZE]);
    }

    #[test]
    fn debug_prints_no_byte_of_the_key() {
        let env = Env::from_pairs([(BIND, "127.0.0.1"), (ACCESS_KEY, "sesame")]);
        let config = NoticeboardConfig::from_env(&env).unwrap();

        assert!(!format!("{config:?}").contains("sesame"));
    }

    #[test]
    fn the_failure_action_is_an_exit_with_no_restart() {
        assert_eq!(NoticeboardConfig::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(NoticeboardConfig::FAILURE.at_reload(), AtReload::NotRead);
        assert_eq!(NoticeboardConfig::UNIT, "creche-noticeboard.service");
    }
}
