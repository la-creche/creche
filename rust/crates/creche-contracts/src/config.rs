//! The config of each process: the site file, the environment of each daemon,
//! the roster of the chaperone and the mount files of a sandbox.
//!
//! A process parses its config one time, at its edge, into a type of this
//! module. Code after the parse holds a [`BindAddress`], a [`SocketPath`] or a
//! [`Seconds`], and never a raw text. The parse returns `Result`, so a value
//! that the process cannot use is an error before the first socket opens.
//!
//! The types do not decide what the process does with that error.
//! [`FailureAction`] holds the decision, and [`Checked`] makes each config
//! state one. `rust/AGENTS.md` holds the rule: a daemon must not start again
//! in a loop on a value that is not valid.
//!
//! | Module | What it holds |
//! |---|---|
//! | [`site`] | The site file, `/etc/creche/site.env`. |
//! | [`attendance`], [`caregiver`], [`chaperone`], [`door_owui`], [`door_trigger`], [`noticeboard`], [`intake`] | The config of one daemon. |
//! | [`endpoints`] | The names of some variables that hold the address of another service, and the URL type of a plane. |
//! | [`roster`] | The roster of MCP servers that the chaperone reads. |
//! | [`mounts`] | `runtime.json`, `creds.json` and the env file of the playpen. |
//!
//! No type here reads the environment of the process or a file. A binary
//! reads them and gives the text to a constructor. [`Env`] holds the
//! variables.

use std::collections::BTreeMap;
use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::str::FromStr;

mod pytext;
#[cfg(test)]
mod python;
mod values;

pub mod attendance;
pub mod caregiver;
pub mod chaperone;
pub mod door_owui;
pub mod door_trigger;
pub mod endpoints;
pub mod intake;
pub mod mounts;
pub mod noticeboard;
pub mod roster;
pub mod site;

pub use values::{
    BindAddress, BindAddressError, BindHost, BindHostError, DirPath, FilePath, HttpUrl,
    HttpUrlError, LanAddress, LanAddressError, PathError, Port, PortError, Seconds, SecondsError,
    SocketPath, TokenFilePath, UrlScheme,
};

use crate::secret::{Secret, SecretError};
use endpoints::{PlaneUrl, PlaneUrlError};

// --- the failure action ---

/// The exit status `EX_CONFIG` of `sysexits.h`: the config of the process is
/// not valid.
pub const EX_CONFIG: u8 = 78;

/// The line that the unit file of a daemon must hold when the daemon exits
/// with [`EX_CONFIG`]. With the line, systemd does not start the daemon
/// again after that exit status of the main process.
///
/// systemd reads the line only for the main process. A process of
/// `ExecStartPre=` that exits with 78 still starts the unit again. A unit
/// that holds this line must thus hold no `ExecStartPre=` check of the
/// config: the main process does the parse.
pub const NO_RESTART_LINE: &str = "RestartPreventExitStatus=78";

/// What a process does when the parse of its config fails at start.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AtStart {
    /// The process writes each error to its log and exits with
    /// [`EX_CONFIG`].
    ///
    /// The unit file must then hold [`NO_RESTART_LINE`], and no
    /// `ExecStartPre=` check of the same config. Today each daemon unit
    /// holds `Restart=always` and `StartLimitIntervalSec=0`, and none holds
    /// that line. Without the line, systemd starts the process again after
    /// each exit status, with no limit.
    ExitConfig,
    /// The process starts, refuses each call and publishes the fault. Its
    /// unit file needs no change.
    RefuseEachCall,
}

/// What a process does when the parse of its config fails at a reload.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AtReload {
    /// The process reads the config only at start. It has no reload, and
    /// a call of [`reload`] with such a type does not build.
    NotRead,
    /// The process keeps the last good value and publishes a fault. It does
    /// not exit.
    KeepLastGood,
}

/// What a process does when the parse of one config fails: one decision for
/// the start and one for a reload.
///
/// [`FailureAction::new`] takes each pair of decisions.
///
/// ```
/// use creche_contracts::config::{AtReload, AtStart, FailureAction};
///
/// let action = FailureAction::new(AtStart::ExitConfig, AtReload::KeepLastGood);
/// assert_eq!(action.at_start(), AtStart::ExitConfig);
/// assert_eq!(action.at_reload(), AtReload::KeepLastGood);
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::{AtReload, AtStart, FailureAction};
///
/// let action = FailureAction {
///     at_start: AtStart::ExitConfig,
///     at_reload: AtReload::KeepLastGood,
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FailureAction {
    at_start: AtStart,
    at_reload: AtReload,
}

impl FailureAction {
    /// One decision for the start and one for a reload.
    #[must_use]
    pub const fn new(at_start: AtStart, at_reload: AtReload) -> Self {
        Self {
            at_start,
            at_reload,
        }
    }

    /// The decision for the start.
    #[must_use]
    pub const fn at_start(self) -> AtStart {
        self.at_start
    }

    /// The decision for a reload.
    #[must_use]
    pub const fn at_reload(self) -> AtReload {
        self.at_reload
    }
}

/// A config or a file that a process parses at its edge.
///
/// Each type states its failure action here. A type with no `FAILURE` does
/// not compile, so the port of a service must make the decision.
pub trait Checked {
    /// What the process does when the parse fails.
    const FAILURE: FailureAction;
}

/// The config of one daemon.
pub trait ProcessConfig: Checked {
    /// The unit that starts the daemon.
    const UNIT: &'static str;
}

/// What a daemon does after the parse of its config at start.
///
/// `E` is the error type of the parse. It is [`ConfigErrors`] for the
/// config of a daemon. The roster and the site file have an error type of
/// their own.
#[derive(Debug)]
pub enum Start<C, E = ConfigErrors> {
    /// The config is valid. The daemon runs with it.
    Run(C),
    /// The config is not valid. The daemon writes the errors to its log and
    /// returns this exit status from `main`.
    Exit {
        /// The exit status: [`EX_CONFIG`].
        status: u8,
        /// Each error of the parse.
        errors: E,
    },
    /// The config is not valid. The daemon starts, refuses each call and
    /// publishes the errors as a fault.
    RefuseEachCall {
        /// Each error of the parse.
        errors: E,
    },
}

/// Applies the failure action of `C` to the result of its parse at start.
///
/// ```
/// use creche_contracts::config::noticeboard::NoticeboardConfig;
/// use creche_contracts::config::{EX_CONFIG, Env, Start, start};
///
/// let env = Env::from_pairs([("VIEW_BIND", "0.0.0.0")]);
/// match start(NoticeboardConfig::from_env(&env)) {
///     Start::Exit { status, errors } => {
///         assert_eq!(status, EX_CONFIG);
///         assert_eq!(errors.as_slice().len(), 1);
///     }
///     Start::Run(_) | Start::RefuseEachCall { .. } => unreachable!(),
/// }
/// ```
pub fn start<C: Checked, E>(parsed: Result<C, E>) -> Start<C, E> {
    match (parsed, C::FAILURE.at_start()) {
        (Ok(config), _) => Start::Run(config),
        (Err(errors), AtStart::ExitConfig) => Start::Exit {
            status: EX_CONFIG,
            errors,
        },
        (Err(errors), AtStart::RefuseEachCall) => Start::RefuseEachCall { errors },
    }
}

/// What a daemon holds after a reload of its config.
///
/// `E` is the error type of the parse, as for [`Start`].
#[derive(Debug)]
pub enum Reload<C, E = ConfigErrors> {
    /// The new config is valid. The daemon uses it.
    Fresh(C),
    /// The new config is not valid. The daemon keeps the last good value
    /// and publishes the fault.
    Kept {
        /// The value that the daemon used before the reload.
        last_good: C,
        /// Each error of the parse.
        fault: E,
    },
}

/// Applies the reload rule: a parse that fails keeps the last good value.
///
/// The function never returns an exit status. A reload does not stop a
/// daemon.
///
/// The function takes only a type whose failure action says
/// [`AtReload::KeepLastGood`]:
///
/// ```
/// use creche_contracts::config::noticeboard::NoticeboardConfig;
/// use creche_contracts::config::roster::{RawRoster, Roster};
/// use creche_contracts::config::{Env, Reload, reload};
///
/// let good: RawRoster = serde_json::from_str(r#"{"web-search": {"command": "web-search"}}"#)?;
/// let bad: RawRoster = serde_json::from_str(
///     r#"{"web-search": {"command": "web-search",
///         "arg_denies": [{"tools": [], "arg": "site", "values": ["a"]}]}}"#,
/// )?;
/// let serving = Roster::try_from(good)?;
/// let Reload::Kept { last_good, .. } = reload(serving, Roster::try_from(bad)) else {
///     unreachable!()
/// };
/// assert_eq!(last_good.names().collect::<Vec<_>>(), ["web-search"]);
///
/// let env = Env::from_pairs([("VIEW_BIND", "127.0.0.1")]);
/// assert!(NoticeboardConfig::from_env(&env).is_ok());
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// A type that says [`AtReload::NotRead`] has no reload. A call with such a
/// type does not build. The check runs when the compiler builds the call,
/// so `cargo build` and `cargo test` report it, and `cargo check` does not:
///
/// ```compile_fail,E0080
/// use creche_contracts::config::noticeboard::NoticeboardConfig;
/// use creche_contracts::config::roster::{RawRoster, Roster};
/// use creche_contracts::config::{Env, Reload, reload};
///
/// let env = Env::from_pairs([("VIEW_BIND", "127.0.0.1")]);
/// let last_good = NoticeboardConfig::from_env(&env).unwrap();
/// let kept = reload(last_good, NoticeboardConfig::from_env(&env));
/// ```
pub fn reload<C: Checked, E>(last_good: C, parsed: Result<C, E>) -> Reload<C, E> {
    const {
        assert!(
            matches!(C::FAILURE.at_reload(), AtReload::KeepLastGood),
            "the failure action of this type says AtReload::NotRead: it has no reload"
        );
    }

    match parsed {
        Ok(config) => Reload::Fresh(config),
        Err(fault) => Reload::Kept { last_good, fault },
    }
}

// --- the variables of a process ---

/// The value of one variable.
#[derive(Clone, PartialEq, Eq)]
enum EnvValue {
    Text(String),
    NotUtf8,
}

/// The variables that a process reads: an explicit map.
///
/// No type of this module reads the environment of the process. A binary
/// builds the map one time, with [`Env::from_os`], and gives it to the
/// constructor of its config. A test builds it with [`Env::from_pairs`].
///
/// `Debug` prints each name and no value: a value can be a secret.
///
/// ```
/// use creche_contracts::config::Env;
///
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// assert_eq!(format!("{env:?}"), r#"Env {"AGENT_LAN_ADDRESS"}"#);
/// ```
///
/// Code outside this module cannot build the map from a raw value, and
/// cannot read a value of it:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::Env;
///
/// let env = Env(std::collections::BTreeMap::new());
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Env(BTreeMap<String, EnvValue>);

impl Env {
    /// The map of the given names and values. A later pair wins.
    pub fn from_pairs<K, V, I>(pairs: I) -> Self
    where
        K: Into<String>,
        V: Into<String>,
        I: IntoIterator<Item = (K, V)>,
    {
        let values = pairs
            .into_iter()
            .map(|(name, value)| (name.into(), EnvValue::Text(value.into())));

        Self(values.collect())
    }

    /// The map of the environment of a process: `Env::from_os(std::env::vars_os())`.
    ///
    /// `std::env::vars` stops the process on a value that is not UTF-8.
    /// This function keeps such a variable, and a config that reads it gets
    /// [`ConfigError::NotUtf8`]. It drops a variable whose name is not
    /// UTF-8: no config reads such a name.
    pub fn from_os<I>(pairs: I) -> Self
    where
        I: IntoIterator<Item = (OsString, OsString)>,
    {
        let values = pairs.into_iter().filter_map(|(name, value)| {
            let value = value
                .into_string()
                .map_or(EnvValue::NotUtf8, EnvValue::Text);

            Some((name.into_string().ok()?, value))
        });

        Self(values.collect())
    }

    /// The value of `variable` as it is set. `None` for a variable that is
    /// not set.
    fn exact(&self, variable: &'static str) -> Result<Option<&str>, ConfigErrors> {
        match self.0.get(variable) {
            None => Ok(None),
            Some(EnvValue::Text(text)) => Ok(Some(text)),
            Some(EnvValue::NotUtf8) => Err(ConfigError::NotUtf8 { variable }.into()),
        }
    }

    /// The value of `variable` without the space at its two ends. `None` for
    /// a variable that is not set and for a value that is then empty: each
    /// Python service reads the two cases as one.
    fn text(&self, variable: &'static str) -> Result<Option<&str>, ConfigErrors> {
        let text = self.exact(variable)?.map(pytext::strip);

        Ok(text.filter(|text| !text.is_empty()))
    }

    /// The value of `variable` as a `T`. `None` for a variable that is not
    /// set or empty.
    fn parse<T: Value>(&self, variable: &'static str) -> Result<Option<T>, ConfigErrors> {
        let Some(text) = self.text(variable)? else {
            return Ok(None);
        };

        match text.parse() {
            Ok(value) => Ok(Some(value)),
            Err(error) => Err(T::refuse(variable, error).into()),
        }
    }

    /// The value of `variable` as a `T`, or the value that `default` makes.
    fn parse_or<T: Value>(
        &self,
        variable: &'static str,
        default: impl FnOnce() -> Result<T, T::Err>,
    ) -> Result<T, ConfigErrors> {
        if let Some(value) = self.parse(variable)? {
            return Ok(value);
        }

        default().map_err(|error| T::refuse(variable, error).into())
    }

    /// The value of `variable` as a `T`. A variable that is not set or empty
    /// is an error.
    fn require<T: Value>(&self, variable: &'static str) -> Result<T, ConfigErrors> {
        self.parse(variable)?
            .ok_or_else(|| ConfigError::Unset { variable }.into())
    }

    /// The value of the first of `variables` that is set, in order.
    fn first_set<T: Value>(&self, variables: &[&'static str]) -> Result<Option<T>, ConfigErrors> {
        for variable in variables {
            if let Some(value) = self.parse(variable)? {
                return Ok(Some(value));
            }
        }

        Ok(None)
    }
}

impl fmt::Debug for Env {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Env ")?;

        f.debug_set().entries(self.0.keys()).finish()
    }
}

/// The variable of the site file that holds the LAN address of the host.
pub(crate) const LAN_ADDRESS: &str = "AGENT_LAN_ADDRESS";

/// A type that a variable can hold, and the error of a text that is not one.
trait Value: FromStr {
    /// The error of `variable` for a text that is not a value of the type.
    fn refuse(variable: &'static str, error: Self::Err) -> ConfigError;
}

/// Gives a value type its [`ConfigError`] variant.
macro_rules! config_value {
    ($($type:ty => $variant:ident),+ $(,)?) => {
        $(
            impl Value for $type {
                fn refuse(variable: &'static str, error: Self::Err) -> ConfigError {
                    ConfigError::$variant { variable, error }
                }
            }
        )+
    };
}

config_value! {
    LanAddress => Address,
    BindHost => Host,
    BindAddress => Bind,
    Port => Port,
    SocketPath => Path,
    DirPath => Path,
    TokenFilePath => Path,
    FilePath => Path,
    Seconds => Seconds,
    HttpUrl => Url,
    PlaneUrl => PlaneUrl,
}

// --- the errors ---

/// Why a process cannot use the value of one variable.
///
/// Each variant names the variable and the reason. No variant holds the
/// value: a value can be a secret, and a caller writes this error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfigError {
    /// The variable is not set, or its value is empty, and the process has
    /// no default for it.
    Unset {
        /// The name of the variable.
        variable: &'static str,
    },
    /// The value is not UTF-8.
    NotUtf8 {
        /// The name of the variable.
        variable: &'static str,
    },
    /// The value is not a LAN address.
    Address {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: LanAddressError,
    },
    /// The value is not the host of a bind.
    Host {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: BindHostError,
    },
    /// The value is not `host:port`.
    Bind {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: BindAddressError,
    },
    /// The value is not a port.
    Port {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: PortError,
    },
    /// The value is not a path that the process can use.
    Path {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: PathError,
    },
    /// The value is not a count of seconds.
    Seconds {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: SecondsError,
    },
    /// The value is not the URL of an HTTP service.
    Url {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: HttpUrlError,
    },
    /// The value is not the URL of a plane.
    PlaneUrl {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: PlaneUrlError,
    },
    /// The value is not a secret.
    Secret {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: SecretError,
    },
    /// The site file gives no valid value for the key.
    Site {
        /// The name of the key of the site file.
        variable: &'static str,
        /// The reason.
        error: site::SiteFault,
    },
    /// The value is not a yes and not a no.
    NotASwitch {
        /// The name of the variable.
        variable: &'static str,
    },
    /// The value is not a whole number in the range of the variable.
    NotInRange {
        /// The name of the variable.
        variable: &'static str,
        /// The smallest number that the variable takes.
        min: i64,
        /// The largest number that the variable takes.
        max: i64,
    },
    /// The value has a form that the process cannot use.
    BadForm {
        /// The name of the variable.
        variable: &'static str,
        /// The form that the variable takes.
        form: &'static str,
    },
    /// The variable and one other variable are set, and the process takes
    /// only one of the two.
    Both {
        /// The name of the variable.
        variable: &'static str,
        /// The name of the other variable.
        other: &'static str,
    },
    /// The bind is not loopback, and the key of the service is missing or
    /// has less than the smallest count of bytes.
    OpenOnLan {
        /// The name of the variable that holds the bind.
        variable: &'static str,
    },
}

impl ConfigError {
    /// The name of the variable that the error is about.
    #[must_use]
    pub fn variable(&self) -> &'static str {
        match self {
            Self::Unset { variable }
            | Self::NotUtf8 { variable }
            | Self::Address { variable, .. }
            | Self::Host { variable, .. }
            | Self::Bind { variable, .. }
            | Self::Port { variable, .. }
            | Self::Path { variable, .. }
            | Self::Seconds { variable, .. }
            | Self::Url { variable, .. }
            | Self::PlaneUrl { variable, .. }
            | Self::Secret { variable, .. }
            | Self::Site { variable, .. }
            | Self::NotASwitch { variable }
            | Self::NotInRange { variable, .. }
            | Self::BadForm { variable, .. }
            | Self::Both { variable, .. }
            | Self::OpenOnLan { variable } => variable,
        }
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: ", self.variable())?;
        match self {
            Self::Unset { .. } => f.write_str("the variable is not set"),
            Self::NotUtf8 { .. } => f.write_str("the value is not UTF-8"),
            Self::Address { error, .. } => write!(f, "{error}"),
            Self::Host { error, .. } => write!(f, "{error}"),
            Self::Bind { error, .. } => write!(f, "{error}"),
            Self::Port { error, .. } => write!(f, "{error}"),
            Self::Path { error, .. } => write!(f, "{error}"),
            Self::Seconds { error, .. } => write!(f, "{error}"),
            Self::Url { error, .. } => write!(f, "{error}"),
            Self::PlaneUrl { error, .. } => write!(f, "{error}"),
            Self::Secret { error, .. } => write!(f, "{error}"),
            Self::Site { error, .. } => write!(f, "{error}"),
            Self::NotASwitch { .. } => f.write_str("the value is not a yes or a no"),
            Self::NotInRange { min, max, .. } => {
                write!(f, "the value is not a whole number from {min} to {max}")
            }
            Self::BadForm { form, .. } => write!(f, "the value is not {form}"),
            Self::Both { other, .. } => write!(f, "set this variable or {other}, not the two"),
            Self::OpenOnLan { .. } => {
                f.write_str("the bind is not loopback, and the access key is missing or too short")
            }
        }
    }
}

impl Error for ConfigError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Address { error, .. } => Some(error),
            Self::Host { error, .. } => Some(error),
            Self::Bind { error, .. } => Some(error),
            Self::Port { error, .. } => Some(error),
            Self::Path { error, .. } => Some(error),
            Self::Seconds { error, .. } => Some(error),
            Self::Url { error, .. } => Some(error),
            Self::PlaneUrl { error, .. } => Some(error),
            Self::Secret { error, .. } => Some(error),
            Self::Unset { .. }
            | Self::NotUtf8 { .. }
            | Self::Site { .. }
            | Self::NotASwitch { .. }
            | Self::NotInRange { .. }
            | Self::BadForm { .. }
            | Self::Both { .. }
            | Self::OpenOnLan { .. } => None,
        }
    }
}

/// Each error of one parse of a config: one error or more.
///
/// The parse of a config does not stop at the first variable that it cannot
/// use. The operator then corrects each variable in one step.
///
/// ```
/// use creche_contracts::config::{ConfigError, ConfigErrors};
///
/// let errors = ConfigErrors::from(ConfigError::Unset { variable: "PEP_AUDIT_DIR" });
/// assert_eq!(errors.as_slice().len(), 1);
/// assert_eq!(errors.to_string(), "PEP_AUDIT_DIR: the variable is not set");
/// ```
///
/// Code outside this module cannot build a list with no error:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::{ConfigError, ConfigErrors};
///
/// let errors = ConfigErrors(Vec::new());
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigErrors(Vec<ConfigError>);

impl ConfigErrors {
    /// The errors, in the order of the parse. The slice is never empty.
    #[must_use]
    pub fn as_slice(&self) -> &[ConfigError] {
        &self.0
    }

    /// The errors of this parse, then the errors of `other`.
    fn and(mut self, other: Self) -> Self {
        self.0.extend(other.0);

        self
    }

    /// The errors with each one in the list one time, at its first place.
    /// Two parts of a config can read the same variable, and each part then
    /// reports the same error.
    fn each_once(self) -> Self {
        let mut once: Vec<ConfigError> = Vec::with_capacity(self.0.len());
        for error in self.0 {
            if !once.contains(&error) {
                once.push(error);
            }
        }

        Self(once)
    }
}

impl From<ConfigError> for ConfigErrors {
    fn from(error: ConfigError) -> Self {
        Self(vec![error])
    }
}

impl fmt::Display for ConfigErrors {
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

impl Error for ConfigErrors {}

/// The result of the parse of one part of a config.
type Parsed<T> = Result<T, ConfigErrors>;

/// The two values, or each error of the two parses.
fn all2<A, B>(first: Parsed<A>, second: Parsed<B>) -> Parsed<(A, B)> {
    match (first, second) {
        (Ok(first), Ok(second)) => Ok((first, second)),
        (Err(first), Err(second)) => Err(first.and(second)),
        (Err(errors), Ok(_)) | (Ok(_), Err(errors)) => Err(errors),
    }
}

/// The three values, or each error of the three parses.
fn all3<A, B, C>(first: Parsed<A>, second: Parsed<B>, third: Parsed<C>) -> Parsed<(A, B, C)> {
    let ((first, second), third) = all2(all2(first, second), third)?;

    Ok((first, second, third))
}

/// The four values, or each error of the four parses.
fn all4<A, B, C, D>(
    first: Parsed<A>,
    second: Parsed<B>,
    third: Parsed<C>,
    fourth: Parsed<D>,
) -> Parsed<(A, B, C, D)> {
    let ((first, second), (third, fourth)) = all2(all2(first, second), all2(third, fourth))?;

    Ok((first, second, third, fourth))
}

/// The value of a variable that holds a secret. `None` for a variable that
/// is not set or empty.
fn secret(env: &Env, variable: &'static str) -> Parsed<Option<Secret>> {
    let Some(text) = env.text(variable)? else {
        return Ok(None);
    };

    match Secret::try_from(text.to_owned()) {
        Ok(secret) => Ok(Some(secret)),
        Err(error) => Err(ConfigError::Secret { variable, error }.into()),
    }
}

/// A whole number of a variable, from `min` to `max`. `None` for a variable
/// that is not set or empty.
fn number(env: &Env, variable: &'static str, min: i64, max: i64) -> Parsed<Option<i64>> {
    let Some(text) = env.text(variable)? else {
        return Ok(None);
    };

    match pytext::integer(text) {
        Some(pytext::Integer::Fits(value)) if (min..=max).contains(&value) => Ok(Some(value)),
        _ => Err(ConfigError::NotInRange { variable, min, max }.into()),
    }
}

/// Where `attendance` answers a client on the same host.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AttendanceTarget {
    /// The Unix socket of `attendance`. This is the default (contract 02 §3
    /// rule 1).
    Socket(SocketPath),
    /// The URL of an `attendance` that binds the LAN address.
    Url(HttpUrl),
}

/// The target of a door: a URL or a socket, and never the two. With no
/// variable set, the target is the default socket.
fn door_target(
    env: &Env,
    url: &'static str,
    socket: &'static str,
    default_socket: &'static str,
) -> Parsed<AttendanceTarget> {
    let (url_text, socket_text) = all2(env.text(url), env.text(socket))?;
    if url_text.is_some() && socket_text.is_some() {
        return Err(ConfigError::Both {
            variable: url,
            other: socket,
        }
        .into());
    }

    if let Some(target) = env.parse(url)? {
        return Ok(AttendanceTarget::Url(target));
    }

    env.parse_or(socket, || default_socket.parse())
        .map(AttendanceTarget::Socket)
}

/// The smallest count of bytes in the key or the token of a door (contract 02
/// §3 rule 7).
pub const MIN_KEY_BYTES: usize = 32;

/// Why the text of a key file is not a key.
///
/// No variant holds the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyError {
    /// The key has less than [`MIN_KEY_BYTES`] bytes. An empty file is in
    /// this case.
    TooShort,
}

impl fmt::Display for KeyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooShort => write!(f, "a key has {MIN_KEY_BYTES} bytes or more"),
        }
    }
}

impl Error for KeyError {}

/// The key of a service or the token of a door, from the text of its file.
///
/// A config holds the path of the file and not the key (invariant 13). The
/// binary reads the file and gives the text to this function. The function
/// removes the space at the two ends, as each Python door does, and refuses
/// a key of less than [`MIN_KEY_BYTES`] bytes: a short key makes an admin
/// surface on the LAN an open one (contract 02 §3 rule 7).
///
/// ```
/// use creche_contracts::config::{KeyError, key_of_file};
///
/// let key = key_of_file("0123456789abcdef0123456789abcdef\n")?;
/// assert_eq!(key.expose_secret().len(), 32);
/// assert_eq!(key_of_file("short\n").unwrap_err(), KeyError::TooShort);
/// # Ok::<(), KeyError>(())
/// ```
///
/// # Errors
///
/// [`KeyError::TooShort`] for a key of less than 32 bytes.
pub fn key_of_file(text: &str) -> Result<Secret, KeyError> {
    let key = pytext::strip(text);
    if key.len() < MIN_KEY_BYTES {
        return Err(KeyError::TooShort);
    }

    Secret::try_from(key.to_owned()).map_err(|_| KeyError::TooShort)
}

/// The text without the space at its two ends, as `str.strip` of Python gives
/// it.
///
/// A reader of a token file or of a header calls this function. The one copy
/// of the rule is `creche_util::pytext::strip`. Python removes each
/// `White_Space` character of Unicode and the four separators U+001C to
/// U+001F. `str::trim` keeps the four separators.
///
/// ```
/// use creche_contracts::config::python_strip;
///
/// assert_eq!(python_strip(" \t token\r\n"), "token");
/// assert_eq!(python_strip("\u{1c}token\u{1f}"), "token");
/// assert_eq!(python_strip("\u{85}token\u{a0}"), "token");
/// assert_eq!(python_strip("to ken"), "to ken");
/// assert_eq!("\u{1c}token\u{1f}".trim(), "\u{1c}token\u{1f}");
/// ```
#[must_use]
pub fn python_strip(text: &str) -> &str {
    creche_util::pytext::strip(text)
}

#[cfg(test)]
mod tests {
    use std::os::unix::ffi::OsStringExt;

    use super::*;

    /// A config with one variable, for the tests of the failure action.
    #[derive(Debug, PartialEq)]
    struct Exits(Port);

    impl Checked for Exits {
        const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
    }

    #[derive(Debug, PartialEq)]
    struct Refuses(Port);

    impl Checked for Refuses {
        const FAILURE: FailureAction =
            FailureAction::new(AtStart::RefuseEachCall, AtReload::KeepLastGood);
    }

    fn port(env: &Env) -> Parsed<Port> {
        env.require("PORT")
    }

    fn env(value: &str) -> Env {
        Env::from_pairs([("PORT", value)])
    }

    #[test]
    fn a_valid_config_runs() {
        let Start::Run(config) = start(port(&env("8300")).map(Exits)) else {
            panic!("the config is valid");
        };

        assert_eq!(config.0.get(), 8300);
    }

    #[test]
    fn a_config_that_is_not_valid_exits_with_78_when_the_type_says_so() {
        let Start::Exit { status, errors } = start(port(&env("0")).map(Exits)) else {
            panic!("the config is not valid");
        };

        assert_eq!(status, 78);
        assert_eq!(
            NO_RESTART_LINE,
            format!("RestartPreventExitStatus={status}")
        );
        assert_eq!(
            errors.as_slice(),
            [ConfigError::Port {
                variable: "PORT",
                error: PortError::OutOfRange
            }]
        );
        assert_eq!(Exits::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(Exits::FAILURE.at_reload(), AtReload::NotRead);
    }

    #[test]
    fn a_config_that_is_not_valid_refuses_each_call_when_the_type_says_so() {
        let Start::RefuseEachCall { errors } = start(port(&env("")).map(Refuses)) else {
            panic!("the config is not valid");
        };

        assert_eq!(errors.as_slice(), [ConfigError::Unset { variable: "PORT" }]);
    }

    #[test]
    fn a_reload_that_fails_keeps_the_last_good_value() {
        let last_good = Refuses(Port::fixed(8300).unwrap());
        let Reload::Kept { last_good, fault } = reload(last_good, port(&env("x")).map(Refuses))
        else {
            panic!("the reload fails");
        };

        assert_eq!(last_good.0.get(), 8300);
        assert_eq!(fault.as_slice().len(), 1);

        let Reload::Fresh(fresh) = reload(last_good, port(&env("8301")).map(Refuses)) else {
            panic!("the reload passes");
        };

        assert_eq!(fresh.0.get(), 8301);
    }

    #[test]
    fn start_and_reload_take_the_error_type_of_the_parse() {
        // The roster and the site file have an error type of their own.
        let parsed: Result<Refuses, &str> = Err("not valid");
        let Start::RefuseEachCall { errors } = start(parsed) else {
            panic!("the parse fails");
        };

        assert_eq!(errors, "not valid");

        let last_good = Refuses(Port::fixed(8300).unwrap());
        let Reload::Kept { last_good, fault } = reload(last_good, Err::<Refuses, _>(7_u8)) else {
            panic!("the reload fails");
        };

        assert_eq!(last_good.0.get(), 8300);
        assert_eq!(fault, 7);
    }

    #[test]
    fn a_variable_reads_as_python_reads_it() {
        let env = Env::from_pairs([("A", " 8300\n"), ("B", " \t"), ("C", "")]);

        assert_eq!(env.exact("A"), Ok(Some(" 8300\n")));
        assert_eq!(env.text("A"), Ok(Some("8300")));
        assert_eq!(env.text("B"), Ok(None));
        assert_eq!(env.exact("C"), Ok(Some("")));
        assert_eq!(env.text("C"), Ok(None));
        assert_eq!(env.text("D"), Ok(None));
        assert_eq!(env.parse::<Port>("A").unwrap().unwrap().get(), 8300);
        assert_eq!(env.parse::<Port>("B"), Ok(None));
    }

    #[test]
    fn a_later_pair_wins() {
        let env = Env::from_pairs([("A", "1"), ("A", "2")]);

        assert_eq!(env.text("A"), Ok(Some("2")));
    }

    #[test]
    fn a_value_that_is_not_utf8_is_an_error_of_its_variable() {
        let bad = OsString::from_vec(vec![0xff]);
        let env = Env::from_os([
            (OsString::from("PORT"), bad.clone()),
            (bad, OsString::from("8300")),
            (OsString::from("OTHER"), OsString::from("8300")),
        ]);
        let error = ConfigError::NotUtf8 { variable: "PORT" };

        assert_eq!(env.text("PORT"), Err(error.into()));
        assert_eq!(env.text("OTHER"), Ok(Some("8300")));
        assert_eq!(format!("{env:?}"), r#"Env {"OTHER", "PORT"}"#);
    }

    #[test]
    fn debug_prints_no_value() {
        let env = Env::from_pairs([("LITELLM_MASTER_KEY", "correct horse")]);

        assert_eq!(format!("{env:?}"), r#"Env {"LITELLM_MASTER_KEY"}"#);
        assert!(!format!("{env:#?}").contains("horse"));
    }

    #[test]
    fn the_first_variable_that_is_set_wins() {
        let env = Env::from_pairs([("B", "192.0.2.10"), ("C", "192.0.2.11")]);

        assert_eq!(
            env.first_set::<LanAddress>(&["A", "B", "C"])
                .unwrap()
                .unwrap()
                .as_str(),
            "192.0.2.10"
        );
        assert_eq!(env.first_set::<LanAddress>(&["A"]), Ok(None));
    }

    #[test]
    fn each_error_of_a_parse_is_kept() {
        let env = Env::from_pairs([("A", "0"), ("B", "x"), ("C", "1")]);
        let parsed = all4(
            env.require::<Port>("A"),
            env.require::<Port>("B"),
            env.require::<Port>("C"),
            env.require::<Port>("D"),
        );
        let errors = parsed.unwrap_err();
        let variables: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(variables, ["A", "B", "D"]);
        assert_eq!(
            errors.to_string(),
            "A: a port is 1 to 65535; B: a port is a decimal number; D: the variable is not set"
        );

        let ports = all3(
            env.require::<Port>("C"),
            env.require::<Port>("C"),
            env.require::<Port>("C"),
        );

        assert!(ports.is_ok());
    }

    #[test]
    fn an_error_that_two_parts_report_is_in_the_list_one_time() {
        let env = Env::from_pairs([("A", "0"), ("B", "x")]);
        let parsed = all4(
            env.require::<Port>("A"),
            env.require::<Port>("B"),
            env.require::<Port>("A"),
            env.require::<Port>("C"),
        );
        let errors = parsed.unwrap_err().each_once();
        let variables: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(variables, ["A", "B", "C"]);
    }

    #[test]
    fn a_number_of_a_variable_is_in_its_range() {
        let env = Env::from_pairs([("A", " 50 "), ("B", "0"), ("C", "5_0"), ("D", "x")]);
        let error = |variable| {
            Err(ConfigErrors::from(ConfigError::NotInRange {
                variable,
                min: 1,
                max: 500,
            }))
        };

        assert_eq!(number(&env, "A", 1, 500), Ok(Some(50)));
        assert_eq!(number(&env, "B", 1, 500), error("B"));
        assert_eq!(number(&env, "C", 1, 500), Ok(Some(50)));
        assert_eq!(number(&env, "D", 1, 500), error("D"));
        assert_eq!(number(&env, "E", 1, 500), Ok(None));
    }

    #[test]
    fn a_secret_of_a_variable_is_never_empty() {
        let env = Env::from_pairs([("A", " key \n"), ("B", "  ")]);

        assert!(secret(&env, "A").unwrap().unwrap().matches(b"key"));
        assert!(secret(&env, "B").unwrap().is_none());
        assert!(secret(&env, "C").unwrap().is_none());
    }

    #[test]
    fn a_key_of_a_file_has_32_bytes_or_more() {
        let key = "0123456789abcdef0123456789abcdef";

        assert!(
            key_of_file(&format!(" {key}\n"))
                .unwrap()
                .matches(key.as_bytes())
        );
        let short = key.get(1..).unwrap();
        for text in ["", "\n", "short", short, &format!(" {short} ")] {
            assert_eq!(
                key_of_file(text).unwrap_err(),
                KeyError::TooShort,
                "{text:?}"
            );
        }

        assert_eq!(KeyError::TooShort.to_string(), "a key has 32 bytes or more");
    }

    #[test]
    fn an_error_names_its_variable_and_never_the_value() {
        let env = Env::from_pairs([("VIEW_BIND", "0.0.0.0"), ("VIEW_ACCESS_KEY", "sesame")]);
        let error = env.parse::<BindHost>("VIEW_BIND").unwrap_err();
        let text = format!("{error} {error:?}");

        assert_eq!(
            error.to_string(),
            "VIEW_BIND: a bind host is the LAN address or loopback, not each interface"
        );
        assert!(!text.contains("0.0.0.0"));
        assert!(error.as_slice()[0].source().is_some());
        assert!(ConfigError::Unset { variable: "A" }.source().is_none());
    }

    #[test]
    fn each_error_has_a_text() {
        let both = ConfigError::Both {
            variable: "A",
            other: "B",
        };
        let range = ConfigError::NotInRange {
            variable: "A",
            min: 1,
            max: 500,
        };
        let form = ConfigError::BadForm {
            variable: "A",
            form: "an image reference",
        };

        assert_eq!(both.to_string(), "A: set this variable or B, not the two");
        assert_eq!(
            range.to_string(),
            "A: the value is not a whole number from 1 to 500"
        );
        assert_eq!(form.to_string(), "A: the value is not an image reference");
        assert_eq!(
            ConfigError::NotASwitch { variable: "A" }.to_string(),
            "A: the value is not a yes or a no"
        );
        assert_eq!(
            ConfigError::OpenOnLan { variable: "A" }.to_string(),
            "A: the bind is not loopback, and the access key is missing or too short"
        );
    }
}
