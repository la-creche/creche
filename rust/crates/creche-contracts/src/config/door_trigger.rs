//! The config of the webhook listener of the trigger door.
//!
//! `systemd/creche-trigger-webhooks.service` starts `agent-trigger serve`
//! with the variables of the site file. The Python reader is
//! `agent_door_trigger.config.serve_config_from_env`. That function also
//! reads the token file, so no vector covers it.
//!
//! `agent-trigger fire` is not a daemon. A timer starts it one time for one
//! family, and it has no config type here.

use super::values::{
    BindAddress, DEFAULT_ATTENDANCE_SOCKET, DirPath, LanAddress, Port, Seconds, TokenFilePath,
};
use super::{
    AtReload, AtStart, AttendanceTarget, Checked, ConfigError, ConfigErrors, Env, FailureAction,
    LAN_ADDRESS, Parsed, ProcessConfig, all2, all3, all4, door_target,
};

/// The variable that holds `host:port` of the listener. It wins over the
/// LAN address of the site file.
pub const BIND: &str = "DOOR_TRIGGER_BIND";
/// The variable that names the Unix socket of `attendance`.
pub const SESSIOND_SOCKET: &str = "DOOR_TRIGGER_SESSIOND_SOCKET";
/// The variable that names the URL of `attendance`.
pub const SESSIOND_URL: &str = "DOOR_TRIGGER_SESSIOND_URL";
/// The variable that names the file of the token that the door presents to
/// `attendance`.
pub const TOKEN_FILE: &str = "DOOR_TRIGGER_SESSIOND_TOKEN_FILE";
/// The variable that names the directory of the status documents.
pub const FAMILIES_DIR: &str = "DOOR_TRIGGER_FAMILIES_DIR";
/// The variable that names the registry checkout.
pub const REGISTRY_ROOT: &str = "DOOR_TRIGGER_REGISTRY_ROOT";
/// The variable that names the directory of the webhook tokens.
pub const WEBHOOKS_DIR: &str = "DOOR_TRIGGER_WEBHOOKS_DIR";
/// The variable that gives the time between two reads of the routes.
pub const REFRESH_S: &str = "DOOR_TRIGGER_REFRESH_S";

/// The port of the webhook listener on the LAN address (`spec.md` §3.6).
const WEBHOOK_PORT: &str = "8360";
const DEFAULT_TOKEN_FILE: &str = "/srv/agents/state/rework/tokens/door-trigger.token";
const DEFAULT_FAMILIES_DIR: &str = "/srv/agents/state/rework/families";
const DEFAULT_REGISTRY_ROOT: &str = "/srv/agents/registry";
const DEFAULT_WEBHOOKS_DIR: &str = "/srv/agents/state/rework/triggers/webhooks";

/// The time between two reads of the routes: the caregiver writes a status
/// document again each 30 seconds or less (contract 05 §2 rule 4).
const DEFAULT_REFRESH_S: &str = "30.0";

/// The config of the webhook listener: each value that `agent-trigger
/// serve` reads from its environment.
///
/// The config holds the path of the token file and no token (invariant
/// 13). The binary reads the file and gives the text to
/// [`key_of_file`](super::key_of_file), which refuses a token of less than
/// 32 bytes (contract 02 §3 rule 7).
///
/// FAILURE ACTION. At start, a config or a token file that is not valid
/// stops the daemon with `EX_CONFIG`, and the unit file must hold
/// `RestartPreventExitStatus=78`. The Python door exits with 1 today, so
/// systemd starts the unit again after each exit, with no limit. The daemon
/// does not read this config again. A `SIGHUP` reads the routes again: the
/// registry and the webhook tokens. A route that is not valid keeps the
/// last good routes.
///
/// The type is stricter than the Python reader in these places: the host
/// of the bind is an IP address or a host name, each path is absolute and
/// not empty, the URL of `attendance` has a host, and the refresh time is
/// finite.
///
/// ```
/// use creche_contracts::config::door_trigger::DoorTriggerConfig;
/// use creche_contracts::config::Env;
///
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// let config = DoorTriggerConfig::from_env(&env)?;
/// assert_eq!(config.bind().to_string(), "192.0.2.10:8360");
/// # Ok::<(), creche_contracts::config::ConfigErrors>(())
/// ```
///
/// Code outside this module cannot build a config from raw parts, and cannot
/// change a field of a valid config:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::door_trigger::DoorTriggerConfig;
/// use creche_contracts::config::Env;
///
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// let config = DoorTriggerConfig::from_env(&env).unwrap();
/// let other = DoorTriggerConfig { bind: config.bind().clone(), ..config };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct DoorTriggerConfig {
    bind: BindAddress,
    attendance: AttendanceTarget,
    token_file: TokenFilePath,
    families_dir: DirPath,
    registry_root: DirPath,
    webhooks_dir: DirPath,
    refresh: Seconds,
}

impl Checked for DoorTriggerConfig {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
}

impl ProcessConfig for DoorTriggerConfig {
    const UNIT: &'static str = "creche-trigger-webhooks.service";
}

/// The bind of the listener. An explicit bind wins, the empty text too:
/// the type refuses that text. Without it the bind is the LAN address at
/// 8360. The listener has no default address.
fn bind(env: &Env) -> Parsed<BindAddress> {
    if let Some(text) = env.exact(BIND)? {
        return text.parse().map_err(|error| {
            ConfigError::Bind {
                variable: BIND,
                error,
            }
            .into()
        });
    }

    let port = WEBHOOK_PORT.parse::<Port>().map_err(|error| {
        ConfigErrors::from(ConfigError::Port {
            variable: BIND,
            error,
        })
    });
    let (address, port) = all2(env.require::<LanAddress>(LAN_ADDRESS), port)?;

    Ok(BindAddress::on(address.into(), port))
}

/// The directory of a variable that the Python door reads with no strip.
fn dir(env: &Env, variable: &'static str, default: &'static str) -> Parsed<DirPath> {
    env.exact(variable)?
        .unwrap_or(default)
        .parse()
        .map_err(|error| ConfigError::Path { variable, error }.into())
}

impl DoorTriggerConfig {
    /// Parses the variables of the unit. The function collects each error.
    ///
    /// # Errors
    ///
    /// [`ConfigErrors`] holds one error for each variable that the door
    /// cannot use.
    pub fn from_env(env: &Env) -> Result<Self, ConfigErrors> {
        let target = all3(
            bind(env),
            door_target(
                env,
                SESSIOND_URL,
                SESSIOND_SOCKET,
                DEFAULT_ATTENDANCE_SOCKET,
            ),
            env.parse_or(TOKEN_FILE, || DEFAULT_TOKEN_FILE.parse()),
        );
        let routes = all4(
            dir(env, FAMILIES_DIR, DEFAULT_FAMILIES_DIR),
            dir(env, REGISTRY_ROOT, DEFAULT_REGISTRY_ROOT),
            dir(env, WEBHOOKS_DIR, DEFAULT_WEBHOOKS_DIR),
            env.parse_or(REFRESH_S, || DEFAULT_REFRESH_S.parse()),
        );
        let ((bind, attendance, token_file), routes) = all2(target, routes)?;
        let (families_dir, registry_root, webhooks_dir, refresh) = routes;

        Ok(Self {
            bind,
            attendance,
            token_file,
            families_dir,
            registry_root,
            webhooks_dir,
            refresh,
        })
    }

    /// The host and the port of the listener.
    #[must_use]
    pub fn bind(&self) -> &BindAddress {
        &self.bind
    }

    /// Where `attendance` answers.
    #[must_use]
    pub fn attendance(&self) -> &AttendanceTarget {
        &self.attendance
    }

    /// The file of the token that the door presents to `attendance`.
    #[must_use]
    pub fn token_file(&self) -> &TokenFilePath {
        &self.token_file
    }

    /// The directory of the status documents.
    #[must_use]
    pub fn families_dir(&self) -> &DirPath {
        &self.families_dir
    }

    /// The registry checkout.
    #[must_use]
    pub fn registry_root(&self) -> &DirPath {
        &self.registry_root
    }

    /// The directory of the webhook tokens (contract 05 §6.4).
    #[must_use]
    pub fn webhooks_dir(&self) -> &DirPath {
        &self.webhooks_dir
    }

    /// The time between two reads of the routes.
    #[must_use]
    pub fn refresh(&self) -> Seconds {
        self.refresh
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{BindAddressError, BindHostError, SecondsError};

    /// The variables that `systemd/creche-trigger-webhooks.service` gives
    /// the daemon: the site file.
    fn unit_env() -> Vec<(&'static str, &'static str)> {
        vec![
            ("HOME", "/home/operator"),
            ("XDG_CONFIG_HOME", "/home/operator/.config"),
            ("AGENT_GITHUB_OWNER", "example-owner"),
            ("AGENT_OPERATOR_USER", "operator"),
            ("AGENT_OPERATOR_HOME", "/home/operator"),
            ("AGENT_LAN_ADDRESS", "192.0.2.10"),
        ]
    }

    fn with(pairs: &[(&'static str, &'static str)]) -> Parsed<DoorTriggerConfig> {
        let mut env = unit_env();
        env.extend_from_slice(pairs);

        DoorTriggerConfig::from_env(&Env::from_pairs(env))
    }

    fn one_error(pairs: &[(&'static str, &'static str)]) -> ConfigError {
        let errors = with(pairs).unwrap_err();

        assert_eq!(errors.as_slice().len(), 1, "{errors}");

        errors.as_slice()[0]
    }

    #[test]
    fn the_unit_gives_a_config_with_each_default() {
        let config = with(&[]).unwrap();

        assert_eq!(config.bind().to_string(), "192.0.2.10:8360");
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Socket(DEFAULT_ATTENDANCE_SOCKET.parse().unwrap())
        );
        assert_eq!(config.token_file().as_str(), DEFAULT_TOKEN_FILE);
        assert_eq!(config.families_dir().as_str(), DEFAULT_FAMILIES_DIR);
        assert_eq!(config.registry_root().as_str(), DEFAULT_REGISTRY_ROOT);
        assert_eq!(config.webhooks_dir().as_str(), DEFAULT_WEBHOOKS_DIR);
        assert_eq!(config.refresh().as_secs_f64(), 30.0);
    }

    #[test]
    fn each_variable_of_the_door_wins_over_its_default() {
        let config = with(&[
            (BIND, "127.0.0.1:18360"),
            (SESSIOND_URL, "https://192.0.2.10:8350"),
            (TOKEN_FILE, "/tmp/root/door-trigger.token"),
            (FAMILIES_DIR, "/tmp/root/families"),
            (REGISTRY_ROOT, "/tmp/root/registry"),
            (WEBHOOKS_DIR, "/tmp/root/webhooks"),
            (REFRESH_S, "0.5"),
        ])
        .unwrap();

        assert_eq!(config.bind().to_string(), "127.0.0.1:18360");
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Url("https://192.0.2.10:8350".parse().unwrap())
        );
        assert_eq!(config.token_file().as_str(), "/tmp/root/door-trigger.token");
        assert_eq!(config.families_dir().as_str(), "/tmp/root/families");
        assert_eq!(config.registry_root().as_str(), "/tmp/root/registry");
        assert_eq!(config.webhooks_dir().as_str(), "/tmp/root/webhooks");
        assert_eq!(config.refresh().as_secs_f64(), 0.5);
    }

    #[test]
    fn the_listener_has_no_default_address() {
        let env = Env::from_pairs([(REFRESH_S, "30")]);

        assert_eq!(
            DoorTriggerConfig::from_env(&env).unwrap_err().as_slice(),
            [ConfigError::Unset {
                variable: LAN_ADDRESS
            }]
        );
    }

    #[test]
    fn an_explicit_bind_wins_and_an_empty_one_is_refused() {
        let env = Env::from_pairs([(BIND, "127.0.0.1:18360")]);

        assert!(DoorTriggerConfig::from_env(&env).is_ok());
        assert_eq!(
            one_error(&[(BIND, "")]),
            ConfigError::Bind {
                variable: BIND,
                error: BindAddressError::NoPort
            }
        );
        assert_eq!(
            one_error(&[(BIND, "0.0.0.0:8360")]),
            ConfigError::Bind {
                variable: BIND,
                error: BindAddressError::Host(BindHostError::EachInterface)
            }
        );
    }

    #[test]
    fn the_door_takes_a_url_or_a_socket_and_not_the_two() {
        assert_eq!(
            one_error(&[
                (SESSIOND_URL, "http://192.0.2.10:8350"),
                (SESSIOND_SOCKET, "/tmp/root/sessiond.sock")
            ]),
            ConfigError::Both {
                variable: SESSIOND_URL,
                other: SESSIOND_SOCKET
            }
        );
    }

    #[test]
    fn a_value_that_the_door_cannot_use_is_an_error_of_its_variable() {
        for (variable, value) in [
            (TOKEN_FILE, "door-trigger.token"),
            (FAMILIES_DIR, "families"),
            (FAMILIES_DIR, ""),
            (REGISTRY_ROOT, "registry"),
            (WEBHOOKS_DIR, "webhooks"),
            (SESSIOND_SOCKET, "sessiond.sock"),
            (SESSIOND_URL, "192.0.2.10:8350"),
            (REFRESH_S, "0"),
            (REFRESH_S, "soon"),
        ] {
            assert_eq!(
                one_error(&[(variable, value)]).variable(),
                variable,
                "{value}"
            );
        }

        assert_eq!(
            one_error(&[(REFRESH_S, "inf")]),
            ConfigError::Seconds {
                variable: REFRESH_S,
                error: SecondsError::NotFinite
            }
        );
    }

    #[test]
    fn the_failure_action_is_an_exit_with_no_restart() {
        assert_eq!(DoorTriggerConfig::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(DoorTriggerConfig::FAILURE.at_reload(), AtReload::NotRead);
        assert_eq!(DoorTriggerConfig::UNIT, "creche-trigger-webhooks.service");
    }
}
