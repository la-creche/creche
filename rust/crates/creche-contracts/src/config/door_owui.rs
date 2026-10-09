//! The config of the Open WebUI door.
//!
//! `systemd/creche-door-owui.service` starts the daemon with the variables
//! of `door-owui.env`. The unit does not read the site file. The Python
//! reader is `agent_door_owui.config.from_env`. That function also reads the
//! two key files, so no vector covers it.

use super::values::{
    BindAddress, DEFAULT_ATTENDANCE_SOCKET, DEFAULT_FAMILIES_DIR, DirPath, TokenFilePath,
    default_tokens_dir,
};
use super::{
    AtReload, AtStart, AttendanceTarget, Checked, ConfigError, ConfigErrors, Env, FailureAction,
    Parsed, ProcessConfig, all2, all3, door_target,
};

/// The variable that holds `host:port` of the door.
pub const BIND: &str = "DOOR_OWUI_BIND";
/// The variable that names the file of the key that Open WebUI presents.
/// The door does not start without it.
pub const KEY_FILE: &str = "DOOR_OWUI_KEY_FILE";
/// The variable that names the file of the token that the door presents to
/// `attendance`.
pub const TOKEN_FILE: &str = "DOOR_OWUI_SESSIOND_TOKEN_FILE";
/// The variable that names the Unix socket of `attendance`.
pub const SESSIOND_SOCKET: &str = "DOOR_OWUI_SESSIOND_SOCKET";
/// The variable that names the URL of `attendance`.
pub const SESSIOND_URL: &str = "DOOR_OWUI_SESSIOND_URL";
/// The variable that names the directory of the status documents.
pub const FAMILIES_DIR: &str = "DOOR_OWUI_FAMILIES_DIR";

/// The bind of the door when the variable is not set (contract 02 §3 rule
/// 9 gives the port).
const DEFAULT_BIND: &str = "127.0.0.1:8340";
const DEFAULT_TOKEN_FILE: &str = concat!(default_tokens_dir!(), "/door-owui.token");

/// The config of the Open WebUI door: each value that the door reads from
/// its environment.
///
/// The config holds the path of each key file and no key (invariant 13).
/// The binary reads the two files and gives each text to
/// [`key_of_file`](super::key_of_file), which refuses a key of less than 32
/// bytes (contract 02 §3 rule 7). The door must not serve before the two
/// keys are valid.
///
/// FAILURE ACTION. At start, a config or a key file that is not valid stops
/// the daemon with `EX_CONFIG`, and the unit file must hold
/// `RestartPreventExitStatus=78`. The Python door exits with 1 today, and
/// `ExecStartPre` runs the same check, so systemd starts the unit again
/// after each exit, with no limit. The daemon does not read this config
/// again.
///
/// systemd reads `RestartPreventExitStatus` only for the main process. After
/// a check in `ExecStartPre` that fails, systemd starts the unit again, also
/// when the unit file holds that line. The port of this service to Rust
/// removes the `ExecStartPre` line: the main process does the same parse.
///
/// The type is stricter than the Python reader in these places: the host
/// of the bind is an IP address or a host name, each path is absolute and
/// not empty, and the URL of `attendance` has a host.
///
/// ```
/// use creche_contracts::config::door_owui::DoorOwuiConfig;
/// use creche_contracts::config::Env;
///
/// let key_file = "/srv/agents/state/rework/tokens/door-owui.key";
/// let env = Env::from_pairs([("DOOR_OWUI_KEY_FILE", key_file)]);
/// let config = DoorOwuiConfig::from_env(&env)?;
/// assert_eq!(config.bind().to_string(), "127.0.0.1:8340");
/// # Ok::<(), creche_contracts::config::ConfigErrors>(())
/// ```
///
/// Code outside this module cannot build a config from raw parts, and cannot
/// change a field of a valid config:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::door_owui::DoorOwuiConfig;
/// use creche_contracts::config::Env;
///
/// let key_file = "/srv/agents/state/rework/tokens/door-owui.key";
/// let env = Env::from_pairs([("DOOR_OWUI_KEY_FILE", key_file)]);
/// let config = DoorOwuiConfig::from_env(&env).unwrap();
/// let other = DoorOwuiConfig { bind: config.bind().clone(), ..config };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DoorOwuiConfig {
    bind: BindAddress,
    key_file: TokenFilePath,
    token_file: TokenFilePath,
    attendance: AttendanceTarget,
    families_dir: DirPath,
}

impl Checked for DoorOwuiConfig {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
}

impl ProcessConfig for DoorOwuiConfig {
    const UNIT: &'static str = "creche-door-owui.service";
}

/// The value of a variable that the Python door reads with no strip: a
/// variable that is set to the empty text is not a variable that is not
/// set.
fn exact<T: super::Value>(env: &Env, variable: &'static str, default: &'static str) -> Parsed<T> {
    env.exact(variable)?
        .unwrap_or(default)
        .parse()
        .map_err(|error| T::refuse(variable, error).into())
}

/// The token file of the door. A variable that is not set takes the
/// default. A variable that is set to the empty text is an error, as in the
/// Python door.
fn token_file(env: &Env) -> Parsed<TokenFilePath> {
    if env.exact(TOKEN_FILE)?.is_none() {
        return DEFAULT_TOKEN_FILE.parse().map_err(|error| {
            ConfigError::Path {
                variable: TOKEN_FILE,
                error,
            }
            .into()
        });
    }

    env.require(TOKEN_FILE)
}

impl DoorOwuiConfig {
    /// Parses the variables of the unit. The function collects each error.
    ///
    /// # Errors
    ///
    /// [`ConfigErrors`] holds one error for each variable that the door
    /// cannot use.
    pub fn from_env(env: &Env) -> Result<Self, ConfigErrors> {
        let files = all2(env.require(KEY_FILE), token_file(env));
        let (bind, (key_file, token_file), (attendance, families_dir)) = all3(
            exact(env, BIND, DEFAULT_BIND),
            files,
            all2(
                door_target(
                    env,
                    SESSIOND_URL,
                    SESSIOND_SOCKET,
                    DEFAULT_ATTENDANCE_SOCKET,
                ),
                exact(env, FAMILIES_DIR, DEFAULT_FAMILIES_DIR),
            ),
        )?;

        Ok(Self {
            bind,
            key_file,
            token_file,
            attendance,
            families_dir,
        })
    }

    /// The host and the port of the listener.
    #[must_use]
    pub fn bind(&self) -> &BindAddress {
        &self.bind
    }

    /// The file of the key that Open WebUI presents to the door.
    #[must_use]
    pub fn key_file(&self) -> &TokenFilePath {
        &self.key_file
    }

    /// The file of the token that the door presents to `attendance`.
    #[must_use]
    pub fn token_file(&self) -> &TokenFilePath {
        &self.token_file
    }

    /// Where `attendance` answers.
    #[must_use]
    pub fn attendance(&self) -> &AttendanceTarget {
        &self.attendance
    }

    /// The directory of the status documents.
    #[must_use]
    pub fn families_dir(&self) -> &DirPath {
        &self.families_dir
    }
}

/// The error of a key file of a door, with the variable that names the
/// file.
#[must_use]
pub fn key_error(variable: &'static str) -> ConfigError {
    ConfigError::BadForm {
        variable,
        form: "the path of a key file with a key of 32 bytes or more",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{BindAddressError, BindHostError, PathError};

    /// The variables that `systemd/creche-door-owui.service` gives the
    /// daemon: `door-owui.env`, which the cutover script writes.
    fn unit_env() -> Vec<(&'static str, &'static str)> {
        vec![
            ("HOME", "/home/operator"),
            ("XDG_CONFIG_HOME", "/home/operator/.config"),
            (BIND, "192.0.2.10:8340"),
            (KEY_FILE, "/srv/agents/state/rework/tokens/door-owui.key"),
        ]
    }

    fn with(pairs: &[(&'static str, &'static str)]) -> Parsed<DoorOwuiConfig> {
        let mut env = unit_env();
        env.extend_from_slice(pairs);

        DoorOwuiConfig::from_env(&Env::from_pairs(env))
    }

    fn one_error(pairs: &[(&'static str, &'static str)]) -> ConfigError {
        let errors = with(pairs).unwrap_err();

        assert_eq!(errors.as_slice().len(), 1, "{errors}");

        errors.as_slice()[0]
    }

    #[test]
    fn the_unit_gives_a_config_with_each_default() {
        let config = with(&[]).unwrap();

        assert_eq!(config.bind().to_string(), "192.0.2.10:8340");
        assert!(config.key_file().as_str().ends_with("door-owui.key"));
        assert_eq!(
            config.token_file().as_str(),
            "/srv/agents/state/rework/tokens/door-owui.token"
        );
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Socket(DEFAULT_ATTENDANCE_SOCKET.parse().unwrap())
        );
        assert_eq!(
            config.families_dir().as_str(),
            "/srv/agents/state/rework/families"
        );
    }

    #[test]
    fn each_variable_of_the_door_wins_over_its_default() {
        let config = with(&[
            (BIND, "[::1]:18340"),
            (TOKEN_FILE, " /tmp/root/door-owui.token "),
            (SESSIOND_URL, "http://192.0.2.10:8350"),
            (FAMILIES_DIR, "/tmp/root/families"),
        ])
        .unwrap();

        assert_eq!(config.bind().host().as_str(), "::1");
        assert_eq!(config.bind().port().get(), 18340);
        assert_eq!(config.token_file().as_str(), "/tmp/root/door-owui.token");
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Url("http://192.0.2.10:8350".parse().unwrap())
        );
        assert_eq!(config.families_dir().as_str(), "/tmp/root/families");
    }

    #[test]
    fn the_door_does_not_start_without_its_key_file() {
        let env = Env::from_pairs([(BIND, "192.0.2.10:8340")]);

        assert_eq!(
            DoorOwuiConfig::from_env(&env).unwrap_err().as_slice(),
            [ConfigError::Unset { variable: KEY_FILE }]
        );
        assert_eq!(
            one_error(&[(KEY_FILE, "  ")]),
            ConfigError::Unset { variable: KEY_FILE }
        );
    }

    #[test]
    fn an_empty_token_file_variable_is_an_error() {
        assert_eq!(
            one_error(&[(TOKEN_FILE, " ")]),
            ConfigError::Unset {
                variable: TOKEN_FILE
            }
        );
    }

    #[test]
    fn the_door_never_binds_each_interface() {
        let each = ConfigError::Bind {
            variable: BIND,
            error: BindAddressError::Host(BindHostError::EachInterface),
        };

        for bind in ["0.0.0.0:8340", ":::8340", "[::]:8340", "*:8340"] {
            assert_eq!(one_error(&[(BIND, bind)]), each, "{bind}");
        }

        assert_eq!(
            one_error(&[(BIND, "")]),
            ConfigError::Bind {
                variable: BIND,
                error: BindAddressError::NoPort
            }
        );
        assert_eq!(one_error(&[(BIND, ":8340")]).variable(), BIND);
        assert_eq!(one_error(&[(BIND, "192.0.2.10:http")]).variable(), BIND);
        assert_eq!(one_error(&[(BIND, "192.0.2.10:0")]).variable(), BIND);
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
        assert_eq!(
            with(&[(SESSIOND_SOCKET, "/tmp/root/sessiond.sock")])
                .unwrap()
                .attendance(),
            &AttendanceTarget::Socket("/tmp/root/sessiond.sock".parse().unwrap())
        );
        assert_eq!(
            one_error(&[(SESSIOND_URL, "192.0.2.10:8350")]).variable(),
            SESSIOND_URL
        );
    }

    #[test]
    fn an_empty_directory_variable_is_an_error() {
        assert_eq!(
            one_error(&[(FAMILIES_DIR, "")]),
            ConfigError::Path {
                variable: FAMILIES_DIR,
                error: PathError::Empty
            }
        );
    }

    #[test]
    fn the_error_of_a_key_file_names_its_variable() {
        assert_eq!(
            key_error(KEY_FILE).to_string(),
            "DOOR_OWUI_KEY_FILE: the value is not the path of a key file with a key of 32 bytes \
             or more"
        );
    }

    #[test]
    fn the_failure_action_is_an_exit_with_no_restart() {
        assert_eq!(DoorOwuiConfig::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(DoorOwuiConfig::FAILURE.at_reload(), AtReload::NotRead);
        assert_eq!(DoorOwuiConfig::UNIT, "creche-door-owui.service");
    }
}
