//! The config of the secret intake of root.
//!
//! `systemd/creche-handover-intake.service` starts the daemon as root. The
//! unit gives it no `EnvironmentFile`. The daemon reads the site file
//! itself, and it takes two values from the environment that its wrapper
//! gives it. The Python reader is `handover.intake.run`.

use super::site::{OperatorUser, SiteError, SiteFile};
use super::values::{FilePath, HttpUrl, LanAddress, Port};
use super::{
    AtReload, AtStart, Checked, ConfigError, ConfigErrors, Env, FailureAction, Parsed,
    ProcessConfig, all2, all3,
};
use crate::secret::Secret;

/// The variable that names the hook of a push to the phone of the
/// operator.
pub const APPROVAL_URL: &str = "RELEASE_APPROVAL_URL";
/// The variable that holds the bearer of that hook.
pub const APPROVAL_TOKEN: &str = "RELEASE_APPROVAL_TOKEN";

/// The variables that the daemon keeps of the environment of root. It
/// removes each other variable before it accepts the first connection.
pub const KEPT_ENV: [&str; 5] = ["PATH", "HOME", "LANG", "LC_ALL", "TZ"];

/// The port of the intake on the LAN address. The port binds only while a
/// token is pending.
const INTAKE_PORT: &str = "8380";

/// The certificate and the key of root for the LAN address. Root makes the
/// two files by hand, and no release writes them.
const CERT_FILE: &str = "/etc/agent-intake/intake.crt";
const KEY_FILE: &str = "/etc/agent-intake/intake.key";

/// The hook that tells the operator about a pending secret.
///
/// Only the parse of an [`IntakeConfig`] makes a value.
///
/// ```
/// use creche_contracts::config::intake::{IntakeConfig, PushHook};
/// use creche_contracts::config::site::SiteFile;
/// use creche_contracts::config::{Env, HttpUrl};
///
/// let site = SiteFile::parse(b"AGENT_OPERATOR_USER=operator\nAGENT_LAN_ADDRESS=192.0.2.10\n")?;
/// let env = Env::from_pairs([
///     ("RELEASE_APPROVAL_URL", "http://192.0.2.10:1881/hook/release"),
///     ("RELEASE_APPROVAL_TOKEN", "a-test-token-of-the-hook"),
/// ]);
/// let config = IntakeConfig::from_parts(&site, &env)?;
/// let hook: &PushHook = config.push().ok_or("the two variables name a hook")?;
/// let url: &HttpUrl = hook.url();
/// assert_eq!(url.as_str(), "http://192.0.2.10:1881/hook/release");
/// assert!(hook.token().matches(b"a-test-token-of-the-hook"));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::intake::{IntakeConfig, PushHook};
/// use creche_contracts::config::site::SiteFile;
/// use creche_contracts::config::{Env, HttpUrl};
///
/// fn with_other_url(hook: PushHook, url: HttpUrl) -> PushHook {
///     PushHook { url, ..hook }
/// }
/// ```
#[derive(Debug)]
pub struct PushHook {
    url: HttpUrl,
    token: Secret,
}

impl PushHook {
    /// The URL of the hook.
    #[must_use]
    pub fn url(&self) -> &HttpUrl {
        &self.url
    }

    /// The bearer of the hook.
    #[must_use]
    pub fn token(&self) -> &Secret {
        &self.token
    }
}

/// The config of the secret intake: two values of the site file, two
/// values of the environment and the fixed paths of root.
///
/// FAILURE ACTION. At start, a config that is not valid stops the daemon
/// with `EX_CONFIG`, and the unit file must hold
/// `RestartPreventExitStatus=78`. The Python daemon exits with 1 today for
/// a site file that it cannot use, so systemd starts the unit again after
/// each exit, with no limit. The daemon does not read this config again.
///
/// A hook that is not set is not an error. The daemon then tells nobody
/// about a token, so it drops each token and serves no page. That is the
/// closed end.
///
/// The type is stricter than the Python reader in one place: a hook URL
/// that is set must be an [`HttpUrl`].
///
/// ```
/// use creche_contracts::config::intake::IntakeConfig;
/// use creche_contracts::config::site::SiteFile;
/// use creche_contracts::config::Env;
///
/// let site = SiteFile::parse(b"AGENT_OPERATOR_USER=operator\nAGENT_LAN_ADDRESS=192.0.2.10\n")
///     .unwrap();
/// let config = IntakeConfig::from_parts(&site, &Env::from_pairs([("PATH", "/usr/bin")]))?;
/// assert_eq!(config.link_base(), "https://192.0.2.10:8380");
/// assert!(config.push().is_none());
/// # Ok::<(), creche_contracts::config::ConfigErrors>(())
/// ```
///
/// Code outside this module cannot build a config from raw parts, and cannot
/// change a field of a valid config:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::intake::IntakeConfig;
/// use creche_contracts::config::site::SiteFile;
/// use creche_contracts::config::Env;
///
/// let site = SiteFile::parse(b"AGENT_OPERATOR_USER=operator\nAGENT_LAN_ADDRESS=192.0.2.10\n")
///     .unwrap();
/// let config = IntakeConfig::from_parts(&site, &Env::from_pairs([("PATH", "/usr/bin")]))
///     .unwrap();
/// let other = IntakeConfig { push: None, ..config };
/// ```
#[derive(Debug)]
pub struct IntakeConfig {
    lan_address: LanAddress,
    port: Port,
    operator_user: OperatorUser,
    push: Option<PushHook>,
    cert_file: FilePath,
    key_file: FilePath,
}

impl Checked for IntakeConfig {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
}

impl ProcessConfig for IntakeConfig {
    const UNIT: &'static str = "creche-handover-intake.service";
}

/// The error of one key of the site file, as an error of a config.
fn site_error(error: SiteError) -> ConfigErrors {
    ConfigError::Site {
        variable: error.key().as_str(),
        error: error.fault(),
    }
    .into()
}

/// The hook. `None` when the URL or the token is not set.
fn push(env: &Env) -> Parsed<Option<PushHook>> {
    let (url, token) = all2(env.parse(APPROVAL_URL), super::secret(env, APPROVAL_TOKEN))?;

    Ok(url.zip(token).map(|(url, token)| PushHook { url, token }))
}

/// A path that this module holds as a constant.
fn fixed_path(variable: &'static str, path: &'static str) -> Parsed<FilePath> {
    path.parse()
        .map_err(|error| ConfigError::Path { variable, error }.into())
}

impl IntakeConfig {
    /// Parses the two keys of the site file and the two variables. The
    /// function collects each error.
    ///
    /// # Errors
    ///
    /// [`ConfigErrors`] holds one error for each key and each variable that
    /// the daemon cannot use.
    pub fn from_parts(site: &SiteFile, env: &Env) -> Result<Self, ConfigErrors> {
        let port = INTAKE_PORT.parse().map_err(|error| {
            ConfigErrors::from(ConfigError::Port {
                variable: "the intake port",
                error,
            })
        });
        let from_site = all3(
            site.operator_user().map_err(site_error),
            site.lan_address().map_err(site_error),
            port,
        );
        let files = all2(
            fixed_path("the intake certificate", CERT_FILE),
            fixed_path("the intake key", KEY_FILE),
        );
        let ((operator_user, lan_address, port), push, (cert_file, key_file)) =
            all3(from_site, push(env), files)?;

        Ok(Self {
            lan_address,
            port,
            operator_user,
            push,
            cert_file,
            key_file,
        })
    }

    /// The LAN address that the intake binds while a token is pending.
    #[must_use]
    pub fn lan_address(&self) -> &LanAddress {
        &self.lan_address
    }

    /// The port of the intake.
    #[must_use]
    pub fn port(&self) -> Port {
        self.port
    }

    /// The account whose files the gap directory holds.
    #[must_use]
    pub fn operator_user(&self) -> &OperatorUser {
        &self.operator_user
    }

    /// The hook that tells the operator about a pending secret.
    #[must_use]
    pub fn push(&self) -> Option<&PushHook> {
        self.push.as_ref()
    }

    /// The certificate of root for the LAN address.
    #[must_use]
    pub fn cert_file(&self) -> &FilePath {
        &self.cert_file
    }

    /// The key of that certificate.
    #[must_use]
    pub fn key_file(&self) -> &FilePath {
        &self.key_file
    }

    /// The start of the link that the operator opens:
    /// `https://<LAN address>:8380`.
    #[must_use]
    pub fn link_base(&self) -> String {
        format!("https://{}:{}", self.lan_address, self.port)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::site::{OperatorUserError, SiteFault};
    use crate::config::{HttpUrlError, LanAddressError};

    /// The site file of a host.
    const SITE: &str = "AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER=operator\n\
        AGENT_OPERATOR_HOME=/home/operator\nAGENT_LAN_ADDRESS=192.0.2.10\n";

    fn site(text: &str) -> SiteFile {
        SiteFile::parse(text.as_bytes()).unwrap()
    }

    /// The variables that the wrapper of `creche-handover-intake.service`
    /// gives the daemon.
    fn unit_env() -> Env {
        Env::from_pairs([
            ("HOME", "/root"),
            ("PATH", "/usr/local/bin:/usr/bin:/bin"),
            (APPROVAL_URL, "http://127.0.0.1:1881/hook/release"),
            (APPROVAL_TOKEN, "hook-bearer-test"),
        ])
    }

    #[test]
    fn the_unit_gives_a_valid_config() {
        let config = IntakeConfig::from_parts(&site(SITE), &unit_env()).unwrap();
        let push = config.push().unwrap();

        assert_eq!(config.lan_address().as_str(), "192.0.2.10");
        assert_eq!(config.port().get(), 8380);
        assert_eq!(config.operator_user().as_str(), "operator");
        assert_eq!(push.url().as_str(), "http://127.0.0.1:1881/hook/release");
        assert!(push.token().matches(b"hook-bearer-test"));
        assert_eq!(config.cert_file().as_str(), "/etc/agent-intake/intake.crt");
        assert_eq!(config.key_file().as_str(), "/etc/agent-intake/intake.key");
        assert_eq!(config.link_base(), "https://192.0.2.10:8380");
        assert!(!format!("{config:?}").contains("hook-bearer-test"));
    }

    #[test]
    fn a_hook_that_is_not_whole_is_no_hook() {
        for pairs in [
            vec![],
            vec![(APPROVAL_URL, "http://127.0.0.1:1881/hook/release")],
            vec![(APPROVAL_TOKEN, "hook-bearer-test")],
            vec![(APPROVAL_URL, ""), (APPROVAL_TOKEN, " ")],
        ] {
            let config = IntakeConfig::from_parts(&site(SITE), &Env::from_pairs(pairs)).unwrap();

            assert!(config.push().is_none());
        }
    }

    #[test]
    fn the_parse_collects_the_error_of_each_key_and_each_variable() {
        let env = Env::from_pairs([(APPROVAL_URL, "127.0.0.1:1881")]);
        let errors = IntakeConfig::from_parts(
            &site("AGENT_OPERATOR_USER=root\nAGENT_LAN_ADDRESS=0.0.0.0\n"),
            &env,
        )
        .unwrap_err();

        assert_eq!(
            errors.as_slice(),
            [
                ConfigError::Site {
                    variable: "AGENT_OPERATOR_USER",
                    error: SiteFault::OperatorUser(OperatorUserError::Root)
                },
                ConfigError::Site {
                    variable: "AGENT_LAN_ADDRESS",
                    error: SiteFault::LanAddress(LanAddressError::EachInterface)
                },
                ConfigError::Url {
                    variable: APPROVAL_URL,
                    error: HttpUrlError::NoScheme
                },
            ]
        );
        assert_eq!(
            errors.to_string(),
            "AGENT_OPERATOR_USER: the account of the operator is not root; AGENT_LAN_ADDRESS: a \
             LAN address is not the address of each interface; RELEASE_APPROVAL_URL: a URL starts \
             with http:// or https://"
        );
    }

    #[test]
    fn a_site_file_with_no_value_is_an_error_of_the_key() {
        let errors = IntakeConfig::from_parts(&site(""), &unit_env()).unwrap_err();
        let keys: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(keys, ["AGENT_OPERATOR_USER", "AGENT_LAN_ADDRESS"]);
        assert_eq!(
            errors.as_slice()[0].to_string(),
            "AGENT_OPERATOR_USER: the site file does not set the key"
        );
    }

    #[test]
    fn the_daemon_keeps_five_variables_of_the_environment_of_root() {
        assert_eq!(KEPT_ENV, ["PATH", "HOME", "LANG", "LC_ALL", "TZ"]);
    }

    #[test]
    fn the_failure_action_is_an_exit_with_no_restart() {
        assert_eq!(IntakeConfig::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(IntakeConfig::FAILURE.at_reload(), AtReload::NotRead);
        assert_eq!(IntakeConfig::UNIT, "creche-handover-intake.service");
    }
}
