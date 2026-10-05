//! The config of `attendance`, the session service.
//!
//! `systemd/creche-attendance.service` starts the daemon with the variables
//! of `sessiond.env` and of the site file. The Python reader is
//! `attendance.config.from_env`.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::values::TokenFilePath;
use super::values::{BindHost, DirPath, HttpUrl, LanAddress, Port, Seconds, SocketPath};
use super::values::{DEFAULT_ATTENDANCE_SOCKET, DEFAULT_STATE_ROOT};
use super::{
    AtReload, AtStart, Checked, ConfigError, ConfigErrors, Env, FailureAction, LAN_ADDRESS, Parsed,
    ProcessConfig, all2, all3, all4,
};
use crate::ids::SandboxName;

/// The variable that names the sessions root.
pub const SESSIONS_ROOT: &str = "SESSIOND_SESSIONS_ROOT";
/// The variable that names the state root.
pub const STATE_ROOT: &str = "SESSIOND_STATE_ROOT";
/// The variable that names the work root.
pub const WORK_ROOT: &str = "SESSIOND_WORK_ROOT";
/// The variable that names the Unix socket of the service.
pub const SOCKET: &str = "SESSIOND_SOCKET";
/// The variable that turns the TCP bind on.
pub const BIND_LAN: &str = "SESSIOND_BIND_LAN";
/// The variable that names the address of the TCP bind. It wins over the
/// LAN address of the site file.
pub const BIND_ADDRESS: &str = "SESSIOND_LAN_ADDRESS";
/// The variable that names the port of the TCP bind.
pub const LAN_PORT: &str = "SESSIOND_LAN_PORT";
/// The variable that holds the command of the channel.
pub const CHANNEL_COMMAND: &str = "SESSIOND_CHANNEL_COMMAND";
/// The variable that names the directory of the playpen logs.
pub const LOG_DIR: &str = "SESSIOND_LOG_DIR";
/// The variable that names Open WebUI. It turns the Open WebUI copy on.
pub const OWUI_URL: &str = "SESSIOND_OWUI_URL";
/// The variable that names the file of the Open WebUI key.
pub const OWUI_KEY_FILE: &str = "SESSIOND_OWUI_KEY_FILE";
/// The variable that names the Open WebUI folder of the copies.
pub const OWUI_FOLDER_ID: &str = "SESSIOND_OWUI_FOLDER_ID";
/// The variable that gives the age at which a playpen lock is stale.
pub const LOCK_STALE_S: &str = "SESSIOND_LOCK_STALE_S";
/// The variable that gives the time between two reads of a playpen lock.
pub const LOCK_POLL_S: &str = "SESSIOND_LOCK_POLL_S";

const DEFAULT_SESSIONS_ROOT: &str = "/srv/agents/sessions";
const DEFAULT_WORK_ROOT: &str = "/srv/agents/work";
const DEFAULT_LOG_DIR: &str = "/var/log/sessiond";
const DEFAULT_OWUI_KEY_FILE: &str = "/srv/agents/state/rework/tokens/owui-api.key";

/// The optional LAN port of `attendance` (contract 02 §3 rule 9).
const DEFAULT_LAN_PORT: &str = "8350";

/// The command that starts a playpen in a sandbox (contract 03 §1).
const DEFAULT_COMMAND: &str = "sbx exec --env-file {env_file} {sandbox} -- \
    node /opt/agent-supervisor/agent-supervisor.js --sandbox {sandbox}";

/// The two numbers of contract 03 §11.4 rule 4, in seconds.
const DEFAULT_LOCK_STALE_S: &str = "20.0";
const DEFAULT_LOCK_POLL_S: &str = "1.0";

/// The place of the sandbox name in a channel command.
const SANDBOX_FIELD: &str = "{sandbox}";

/// The place of the env file path in a channel command.
const ENV_FILE_FIELD: &str = "{env_file}";

/// The words that turn a switch on, in lower case.
const TRUE_WORDS: [&str; 4] = ["1", "true", "yes", "on"];

/// The words that turn a switch off, in lower case.
const FALSE_WORDS: [&str; 4] = ["0", "false", "no", "off"];

/// Whether `attendance` also binds TCP (contract 02 §3 rule 2).
///
/// The set is closed. It does not cross a process boundary: the parse of
/// `SESSIOND_BIND_LAN` makes it, and a word outside the two word lists is an
/// error.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Bind {
    /// The Unix socket only. This is the default.
    SocketOnly,
    /// The Unix socket and the LAN address.
    SocketAndLan,
}

// CONTRACT-QUESTION: contract 03 §1 gives the command of the channel and no
// grammar for another one. The Python service accepts each text and splits
// it with `shlex` at each dial, so a quote with no end fails the first turn
// and not the start. The type splits the text at the parse and refuses a
// text that does not split, and a text with no word.
/// The command that starts a playpen in a sandbox, as a template.
///
/// The text splits into words as a POSIX shell splits it. Each word can
/// hold `{sandbox}` and `{env_file}`. [`ChannelCommand::argv`] puts the two
/// values in after the split, so no value can add a word.
///
/// ```
/// use creche_contracts::config::attendance::ChannelCommand;
///
/// let command: ChannelCommand = "sbx exec --env-file {env_file} {sandbox} -- node".parse()?;
/// let argv = command.argv(&"chat-s3".parse()?, "/state/supervisor-chat-s3.env");
/// assert_eq!(argv[3], "/state/supervisor-chat-s3.env");
/// assert_eq!(argv[4], "chat-s3");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::attendance::ChannelCommand;
///
/// let command = ChannelCommand {
///     template: String::from("'"),
///     words: Vec::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelCommand {
    template: String,
    words: Vec<String>,
}

impl ChannelCommand {
    /// The template as the config gives it.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.template
    }

    /// The argument list for one sandbox. `env_file` is the path of the env
    /// file of that sandbox, from the status document.
    #[must_use]
    pub fn argv(&self, sandbox: &SandboxName, env_file: &str) -> Vec<String> {
        self.words
            .iter()
            .map(|word| {
                word.replace(SANDBOX_FIELD, sandbox.as_str())
                    .replace(ENV_FILE_FIELD, env_file)
            })
            .collect()
    }
}

/// Where the split of a command is.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Quote {
    None,
    Single,
    Double,
}

/// The words of a command, as `shlex.split` of Python gives them.
fn split_words(text: &str) -> Result<Vec<String>, ChannelCommandError> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut in_word = false;
    let mut quote = Quote::None;
    let mut characters = text.chars();
    while let Some(character) = characters.next() {
        match (quote, character) {
            (Quote::None, ' ' | '\t' | '\r' | '\n') => {
                if in_word {
                    words.push(std::mem::take(&mut word));
                }
                in_word = false;
            }
            (Quote::None, '\\') => {
                word.push(characters.next().ok_or(ChannelCommandError::NoEscaped)?);
                in_word = true;
            }
            (Quote::None, '\'') => {
                quote = Quote::Single;
                in_word = true;
            }
            (Quote::None, '"') => {
                quote = Quote::Double;
                in_word = true;
            }
            (Quote::Single, '\'') | (Quote::Double, '"') => quote = Quote::None,
            (Quote::Double, '\\') => {
                let next = characters.next().ok_or(ChannelCommandError::NoEscaped)?;
                if !matches!(next, '\\' | '"') {
                    word.push('\\');
                }
                word.push(next);
            }
            (_, character) => {
                word.push(character);
                in_word = true;
            }
        }
    }

    if quote != Quote::None {
        return Err(ChannelCommandError::NoClosingQuote);
    }

    if in_word {
        words.push(word);
    }

    Ok(words)
}

impl FromStr for ChannelCommand {
    type Err = ChannelCommandError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let words = split_words(text)?;
        if words.first().is_none_or(String::is_empty) {
            return Err(ChannelCommandError::NoProgram);
        }

        Ok(Self {
            template: text.to_owned(),
            words,
        })
    }
}

/// Why a text is not the command of a channel.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelCommandError {
    /// A quote has no end.
    NoClosingQuote,
    /// The text ends with a backslash.
    NoEscaped,
    /// The text has no word, or its first word is empty.
    NoProgram,
}

impl fmt::Display for ChannelCommandError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoClosingQuote => f.write_str("each quote of a command has an end"),
            Self::NoEscaped => f.write_str("a command does not end with a backslash"),
            Self::NoProgram => f.write_str("a command starts with the name of a program"),
        }
    }
}

impl Error for ChannelCommandError {}

/// The Open WebUI copy of a session (contract 02 §10.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OwuiCopy {
    url: Option<HttpUrl>,
    key_file: TokenFilePath,
    folder_id: Option<String>,
}

impl OwuiCopy {
    /// The base URL of Open WebUI. `None` turns the copy off.
    #[must_use]
    pub fn url(&self) -> Option<&HttpUrl> {
        self.url.as_ref()
    }

    /// The file that holds the Open WebUI key. The config never holds the
    /// key (invariant 13).
    #[must_use]
    pub fn key_file(&self) -> &TokenFilePath {
        &self.key_file
    }

    /// The Open WebUI folder of the copies. The id is opaque text.
    #[must_use]
    pub fn folder_id(&self) -> Option<&str> {
        self.folder_id.as_deref()
    }
}

/// The config of `attendance`: each value that the service reads from its
/// environment.
///
/// FAILURE ACTION. At start, a config that is not valid stops the daemon
/// with `EX_CONFIG`, and the unit file must hold
/// `RestartPreventExitStatus=78`. The Python service exits with 2 today,
/// and `ExecStartPre` runs the same check, so systemd starts the unit again
/// after each exit, with no limit. The daemon does not read this config
/// again. A `SIGHUP` reads the token files again, and a token file that is
/// not valid keeps the last good set.
///
/// systemd reads `RestartPreventExitStatus` only for the main process. After
/// a check in `ExecStartPre` that fails, systemd starts the unit again, also
/// when the unit file holds that line. The port of this service to Rust
/// removes the `ExecStartPre` line: the main process does the same parse.
///
/// The type is stricter than the Python reader in these places:
///
/// 1. The LAN address is a [`BindHost`]. The Python reader takes each text,
///    `0.0.0.0` too.
/// 2. Each path is absolute and holds no NUL byte, and the socket path has
///    107 bytes or less.
/// 3. The Open WebUI URL is an [`HttpUrl`].
/// 4. A count of seconds is finite.
/// 5. The channel command splits into words.
///
/// ```
/// use creche_contracts::config::attendance::{AttendanceConfig, Bind};
/// use creche_contracts::config::Env;
///
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// let config = AttendanceConfig::from_env(&env)?;
/// assert_eq!(config.bind(), Bind::SocketOnly);
/// assert_eq!(config.lan_port().get(), 8350);
/// # Ok::<(), creche_contracts::config::ConfigErrors>(())
/// ```
///
/// Code outside this module cannot build a config from raw parts, and cannot
/// change a field of a valid config:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::attendance::{AttendanceConfig, Bind};
/// use creche_contracts::config::Env;
///
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// let config = AttendanceConfig::from_env(&env).unwrap();
/// let other = AttendanceConfig { bind: Bind::SocketAndLan, ..config };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct AttendanceConfig {
    sessions_root: DirPath,
    state_root: DirPath,
    work_root: DirPath,
    log_dir: DirPath,
    socket_path: SocketPath,
    bind: Bind,
    lan_address: BindHost,
    lan_port: Port,
    channel_command: ChannelCommand,
    owui: OwuiCopy,
    lock_stale: Seconds,
    lock_poll: Seconds,
}

impl Checked for AttendanceConfig {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
}

impl ProcessConfig for AttendanceConfig {
    const UNIT: &'static str = "creche-attendance.service";
}

/// Whether the service also binds TCP. The switch is off unless the
/// variable turns it on.
fn bind(env: &Env) -> Parsed<Bind> {
    let Some(text) = env.text(BIND_LAN)? else {
        return Ok(Bind::SocketOnly);
    };
    let word = text.to_ascii_lowercase();
    if FALSE_WORDS.contains(&word.as_str()) {
        return Ok(Bind::SocketOnly);
    }

    if TRUE_WORDS.contains(&word.as_str()) {
        return Ok(Bind::SocketAndLan);
    }

    Err(ConfigError::NotASwitch { variable: BIND_LAN }.into())
}

/// The address of the TCP bind: the variable of the service, else the LAN
/// address of the site file. The service has no default address.
fn lan_address(env: &Env) -> Parsed<BindHost> {
    if let Some(host) = env.parse::<BindHost>(BIND_ADDRESS)? {
        return Ok(host);
    }

    env.require::<LanAddress>(LAN_ADDRESS).map(BindHost::from)
}

fn channel_command(env: &Env) -> Parsed<ChannelCommand> {
    let text = env.text(CHANNEL_COMMAND)?.unwrap_or(DEFAULT_COMMAND);

    text.parse().map_err(|_| {
        ConfigError::BadForm {
            variable: CHANNEL_COMMAND,
            form: "a command that splits into words",
        }
        .into()
    })
}

fn owui(env: &Env) -> Parsed<OwuiCopy> {
    let (url, key_file, folder_id) = all3(
        env.parse(OWUI_URL),
        env.parse_or(OWUI_KEY_FILE, || DEFAULT_OWUI_KEY_FILE.parse()),
        env.text(OWUI_FOLDER_ID),
    )?;

    Ok(OwuiCopy {
        url,
        key_file,
        folder_id: folder_id.map(str::to_owned),
    })
}

impl AttendanceConfig {
    /// Parses the variables of the unit. The function collects each error.
    ///
    /// # Errors
    ///
    /// [`ConfigErrors`] holds one error for each variable that the service
    /// cannot use.
    pub fn from_env(env: &Env) -> Result<Self, ConfigErrors> {
        let roots = all4(
            env.parse_or(SESSIONS_ROOT, || DEFAULT_SESSIONS_ROOT.parse()),
            env.parse_or(STATE_ROOT, || DEFAULT_STATE_ROOT.parse()),
            env.parse_or(WORK_ROOT, || DEFAULT_WORK_ROOT.parse()),
            env.parse_or(LOG_DIR, || DEFAULT_LOG_DIR.parse()),
        );
        let listen = all4(
            env.parse_or(SOCKET, || DEFAULT_ATTENDANCE_SOCKET.parse()),
            bind(env),
            lan_address(env),
            env.parse_or(LAN_PORT, || DEFAULT_LAN_PORT.parse()),
        );
        let lock = all2(
            env.parse_or(LOCK_STALE_S, || DEFAULT_LOCK_STALE_S.parse()),
            env.parse_or(LOCK_POLL_S, || DEFAULT_LOCK_POLL_S.parse()),
        );
        let (roots, listen, (channel_command, owui, lock)) =
            all3(roots, listen, all3(channel_command(env), owui(env), lock))?;
        let (sessions_root, state_root, work_root, log_dir) = roots;
        let (socket_path, bind, lan_address, lan_port) = listen;
        let (lock_stale, lock_poll) = lock;

        Ok(Self {
            sessions_root,
            state_root,
            work_root,
            log_dir,
            socket_path,
            bind,
            lan_address,
            lan_port,
            channel_command,
            owui,
            lock_stale,
            lock_poll,
        })
    }

    /// The root of the session stores.
    #[must_use]
    pub fn sessions_root(&self) -> &DirPath {
        &self.sessions_root
    }

    /// The root of the state of the platform.
    #[must_use]
    pub fn state_root(&self) -> &DirPath {
        &self.state_root
    }

    /// The root of the work trees.
    #[must_use]
    pub fn work_root(&self) -> &DirPath {
        &self.work_root
    }

    /// The directory of the playpen logs.
    #[must_use]
    pub fn log_dir(&self) -> &DirPath {
        &self.log_dir
    }

    /// The Unix socket that the service binds.
    #[must_use]
    pub fn socket_path(&self) -> &SocketPath {
        &self.socket_path
    }

    /// Whether the service also binds TCP.
    #[must_use]
    pub fn bind(&self) -> Bind {
        self.bind
    }

    /// The address of the TCP bind.
    #[must_use]
    pub fn lan_address(&self) -> &BindHost {
        &self.lan_address
    }

    /// The port of the TCP bind.
    #[must_use]
    pub fn lan_port(&self) -> Port {
        self.lan_port
    }

    /// The command that starts a playpen in a sandbox.
    #[must_use]
    pub fn channel_command(&self) -> &ChannelCommand {
        &self.channel_command
    }

    /// The Open WebUI copy of a session.
    #[must_use]
    pub fn owui(&self) -> &OwuiCopy {
        &self.owui
    }

    /// The age at which the lock of a playpen is stale.
    #[must_use]
    pub fn lock_stale(&self) -> Seconds {
        self.lock_stale
    }

    /// The time between two reads of the lock of a playpen.
    #[must_use]
    pub fn lock_poll(&self) -> Seconds {
        self.lock_poll
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{BindHostError, PathError, PortError, SecondsError};

    /// The variables that `systemd/creche-attendance.service` gives the
    /// daemon: the site file, and no line of `sessiond.env`.
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

    fn with(pairs: &[(&'static str, &'static str)]) -> Parsed<AttendanceConfig> {
        let mut env = unit_env();
        env.extend_from_slice(pairs);

        AttendanceConfig::from_env(&Env::from_pairs(env))
    }

    fn one_error(pairs: &[(&'static str, &'static str)]) -> ConfigError {
        let errors = with(pairs).unwrap_err();

        assert_eq!(errors.as_slice().len(), 1, "{errors}");

        errors.as_slice()[0]
    }

    #[test]
    fn the_unit_gives_a_config_with_each_default() {
        let config = with(&[]).unwrap();

        assert_eq!(config.sessions_root().as_str(), "/srv/agents/sessions");
        assert_eq!(config.state_root().as_str(), "/srv/agents/state/rework");
        assert_eq!(config.work_root().as_str(), "/srv/agents/work");
        assert_eq!(config.log_dir().as_str(), "/var/log/sessiond");
        assert_eq!(
            config.socket_path().as_str(),
            "/srv/agents/state/rework/sock/sessiond.sock"
        );
        assert_eq!(config.bind(), Bind::SocketOnly);
        assert_eq!(config.lan_address().as_str(), "192.0.2.10");
        assert_eq!(config.lan_port().get(), 8350);
        assert_eq!(config.channel_command().as_str(), DEFAULT_COMMAND);
        assert_eq!(config.owui().url(), None);
        assert_eq!(
            config.owui().key_file().as_str(),
            "/srv/agents/state/rework/tokens/owui-api.key"
        );
        assert_eq!(config.owui().folder_id(), None);
        assert_eq!(config.lock_stale().as_secs_f64(), 20.0);
        assert_eq!(config.lock_poll().as_secs_f64(), 1.0);
    }

    #[test]
    fn each_variable_of_the_service_wins_over_its_default() {
        let config = with(&[
            (SESSIONS_ROOT, "/tmp/root/sessions"),
            (STATE_ROOT, " /tmp/root/state "),
            (WORK_ROOT, "/tmp/root/work/"),
            (LOG_DIR, "/tmp/root/log"),
            (SOCKET, "/tmp/root/sessiond.sock"),
            (BIND_LAN, "Yes"),
            (BIND_ADDRESS, "127.0.0.1"),
            (LAN_PORT, "18350"),
            (CHANNEL_COMMAND, "node playpen.js --sandbox {sandbox}"),
            (OWUI_URL, "http://192.0.2.10:8181/"),
            (OWUI_KEY_FILE, "/tmp/root/owui.key"),
            (OWUI_FOLDER_ID, " folder-1 "),
            (LOCK_STALE_S, "0.8"),
            (LOCK_POLL_S, "0.05"),
        ])
        .unwrap();

        assert_eq!(config.sessions_root().as_str(), "/tmp/root/sessions");
        assert_eq!(config.state_root().as_str(), "/tmp/root/state");
        assert_eq!(config.work_root().as_str(), "/tmp/root/work");
        assert_eq!(config.log_dir().as_str(), "/tmp/root/log");
        assert_eq!(config.socket_path().as_str(), "/tmp/root/sessiond.sock");
        assert_eq!(config.bind(), Bind::SocketAndLan);
        assert_eq!(config.lan_address().as_str(), "127.0.0.1");
        assert_eq!(config.lan_port().get(), 18350);
        assert_eq!(
            config
                .channel_command()
                .argv(&"chat-s1".parse().unwrap(), "/x.env"),
            ["node", "playpen.js", "--sandbox", "chat-s1"]
        );
        assert_eq!(
            config.owui().url().unwrap().base(),
            "http://192.0.2.10:8181"
        );
        assert_eq!(config.owui().key_file().as_str(), "/tmp/root/owui.key");
        assert_eq!(config.owui().folder_id(), Some("folder-1"));
        assert_eq!(config.lock_stale().as_secs_f64(), 0.8);
        assert_eq!(config.lock_poll().as_secs_f64(), 0.05);
    }

    #[test]
    fn an_empty_variable_reads_as_a_variable_that_is_not_set() {
        let config = with(&[
            (STATE_ROOT, ""),
            (LAN_PORT, "  "),
            (BIND_LAN, ""),
            (OWUI_URL, " "),
        ]);

        assert_eq!(config, with(&[]));
    }

    #[test]
    fn the_switch_takes_the_words_of_the_python_service() {
        for word in ["1", "true", "TRUE", "yes", "on", " On "] {
            assert_eq!(
                with(&[(BIND_LAN, word)]).unwrap().bind(),
                Bind::SocketAndLan
            );
        }

        for word in ["0", "false", "no", "off", "OFF"] {
            assert_eq!(with(&[(BIND_LAN, word)]).unwrap().bind(), Bind::SocketOnly);
        }

        assert_eq!(
            one_error(&[(BIND_LAN, "maybe")]),
            ConfigError::NotASwitch { variable: BIND_LAN }
        );
    }

    #[test]
    fn the_service_has_no_default_address() {
        let env = Env::from_pairs([(STATE_ROOT, "/tmp/state")]);
        let errors = AttendanceConfig::from_env(&env).unwrap_err();

        assert_eq!(
            errors.as_slice(),
            [ConfigError::Unset {
                variable: LAN_ADDRESS
            }]
        );
    }

    #[test]
    fn the_service_never_binds_each_interface() {
        assert_eq!(
            one_error(&[(BIND_ADDRESS, "0.0.0.0")]),
            ConfigError::Host {
                variable: BIND_ADDRESS,
                error: BindHostError::EachInterface
            }
        );
        assert_eq!(one_error(&[(BIND_ADDRESS, "::")]).variable(), BIND_ADDRESS);

        let env = Env::from_pairs([(LAN_ADDRESS, "0.0.0.0")]);

        assert_eq!(
            AttendanceConfig::from_env(&env).unwrap_err().as_slice()[0].variable(),
            LAN_ADDRESS
        );
    }

    #[test]
    fn a_value_that_the_service_cannot_use_is_an_error_of_its_variable() {
        for (variable, value, error) in [
            (
                STATE_ROOT,
                "state",
                ConfigError::Path {
                    variable: STATE_ROOT,
                    error: PathError::NotAbsolute,
                },
            ),
            (
                SOCKET,
                "/a\0b",
                ConfigError::Path {
                    variable: SOCKET,
                    error: PathError::Nul,
                },
            ),
            (
                LAN_PORT,
                "0",
                ConfigError::Port {
                    variable: LAN_PORT,
                    error: PortError::OutOfRange,
                },
            ),
            (
                LAN_PORT,
                "http",
                ConfigError::Port {
                    variable: LAN_PORT,
                    error: PortError::NotANumber,
                },
            ),
            (
                LOCK_STALE_S,
                "nan",
                ConfigError::Seconds {
                    variable: LOCK_STALE_S,
                    error: SecondsError::NotFinite,
                },
            ),
            (
                LOCK_POLL_S,
                "0",
                ConfigError::Seconds {
                    variable: LOCK_POLL_S,
                    error: SecondsError::NotPositive,
                },
            ),
            (
                CHANNEL_COMMAND,
                "sbx 'exec",
                ConfigError::BadForm {
                    variable: CHANNEL_COMMAND,
                    form: "a command that splits into words",
                },
            ),
        ] {
            assert_eq!(one_error(&[(variable, value)]), error, "{variable}");
        }

        assert_eq!(
            one_error(&[(OWUI_URL, "192.0.2.10:8181")]).variable(),
            OWUI_URL
        );
    }

    #[test]
    fn the_parse_collects_the_error_of_each_variable() {
        let errors = with(&[
            (STATE_ROOT, "state"),
            (LAN_PORT, "0"),
            (LOCK_POLL_S, "-1"),
            (BIND_LAN, "maybe"),
        ])
        .unwrap_err();
        let variables: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(variables, [STATE_ROOT, BIND_LAN, LAN_PORT, LOCK_POLL_S]);
    }

    #[test]
    fn a_command_splits_as_a_posix_shell_splits_it() {
        for (text, words) in [
            ("a", vec!["a"]),
            ("  a \t b\n", vec!["a", "b"]),
            ("a 'b c' d", vec!["a", "b c", "d"]),
            ("a \"b c\" d", vec!["a", "b c", "d"]),
            ("a b\\ c", vec!["a", "b c"]),
            ("a '' b", vec!["a", "", "b"]),
            ("a \"\"", vec!["a", ""]),
            ("a'b'\"c\"d", vec!["abcd"]),
            ("a \"b\\\"c\\\\d\\e\"", vec!["a", "b\"c\\d\\e"]),
            ("a 'b\\c'", vec!["a", "b\\c"]),
            ("a \\'b", vec!["a", "'b"]),
            ("a #b", vec!["a", "#b"]),
            ("a\u{a0}b", vec!["a\u{a0}b"]),
        ] {
            let command: ChannelCommand = text.parse().unwrap();

            assert_eq!(command.words, words, "{text:?}");
            assert_eq!(command.as_str(), text);
        }
    }

    #[test]
    fn a_text_that_is_no_command_is_refused() {
        for (text, error) in [
            ("", ChannelCommandError::NoProgram),
            ("  \t", ChannelCommandError::NoProgram),
            ("'' a", ChannelCommandError::NoProgram),
            ("a 'b", ChannelCommandError::NoClosingQuote),
            ("a \"b", ChannelCommandError::NoClosingQuote),
            ("a \"b\\", ChannelCommandError::NoEscaped),
            ("a \\", ChannelCommandError::NoEscaped),
        ] {
            assert_eq!(
                text.parse::<ChannelCommand>().unwrap_err(),
                error,
                "{text:?}"
            );
        }

        assert_eq!(
            ChannelCommandError::NoClosingQuote.to_string(),
            "each quote of a command has an end"
        );
    }

    #[test]
    fn a_value_of_the_dial_cannot_add_a_word() {
        let command: ChannelCommand = "sbx exec --env-file {env_file} {sandbox} -- node"
            .parse()
            .unwrap();
        let argv = command.argv(&"chat-s3".parse().unwrap(), "/a b; rm -rf /.env");

        assert_eq!(
            argv,
            [
                "sbx",
                "exec",
                "--env-file",
                "/a b; rm -rf /.env",
                "chat-s3",
                "--",
                "node"
            ]
        );
    }

    #[test]
    fn the_failure_action_is_an_exit_with_no_restart() {
        assert_eq!(AttendanceConfig::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(AttendanceConfig::FAILURE.at_reload(), AtReload::NotRead);
        assert_eq!(AttendanceConfig::UNIT, "creche-attendance.service");
    }
}
