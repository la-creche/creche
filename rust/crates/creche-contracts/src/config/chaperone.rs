//! The config of the chaperone, the Policy Enforcement Point.
//!
//! `systemd/creche-chaperone.service` starts the daemon with the variables
//! of its `Environment=` lines and of the site file. The Python reader is
//! `chaperone.__main__.main`, with `chaperone.site` for the bind and the two
//! URLs. `main` reads the environment of the process and has no entry point
//! that takes a map, so only the `chaperone.site` part has vectors.

use super::values::{
    BindAddress, DirPath, FilePath, HttpUrl, LanAddress, Port, Seconds, SocketPath, TokenFilePath,
};
use super::{
    AtReload, AtStart, Checked, ConfigError, ConfigErrors, Env, FailureAction, LAN_ADDRESS, Parsed,
    ProcessConfig, all2, all3, all4,
};

/// The variable that names the state root: `grants/`, `audit/` and
/// `faults/pep/` are below it. The service does not start without it.
pub const REWORK_DIR: &str = "PEP_REWORK_DIR";
/// The variable that names the log of a request that resolves to no
/// family. The service does not start without it.
pub const AUDIT_DIR: &str = "PEP_AUDIT_DIR";
/// The variable that holds `host:port`. It wins over the LAN address of the
/// site file.
pub const BIND: &str = "PEP_BIND";
/// The variable that names the base roster. Without it the chaperone reads
/// no roster.
pub const UPSTREAMS: &str = "PEP_UPSTREAMS";
/// The variable that names the roster that root generates.
pub const UPSTREAMS_GENERATED: &str = "PEP_UPSTREAMS_GENERATED";
/// The variable that names the encrypted file of each upstream secret.
pub const SECRETS: &str = "PEP_SECRETS";
/// The variable that names the directory of one encrypted file for each
/// secret.
pub const SECRETS_DIR: &str = "PEP_SECRETS_DIR";
/// The variable that names the Unix socket of `attendance`.
pub const SESSIOND_SOCKET: &str = "PEP_SESSIOND_SOCKET";
/// The variable that names the token file of the delegate door.
pub const DELEGATE_TOKEN_FILE: &str = "PEP_DELEGATE_TOKEN_FILE";
/// The variable that names the token file of the dispatch door.
pub const DISPATCH_TOKEN_FILE: &str = "PEP_DISPATCH_TOKEN_FILE";
/// The variable that names the spool of release requests.
pub const RELEASE_REQUESTS_DIR: &str = "PEP_RELEASE_REQUESTS_DIR";
/// The variable that names the hook of an approval gate.
pub const APPROVAL_URL: &str = "PEP_APPROVAL_URL";
/// The variable that gives the time between two sweeps of the faulted
/// families.
pub const FAULT_SWEEP_INTERVAL_S: &str = "PEP_FAULT_SWEEP_INTERVAL_S";
/// The variable that names Home Assistant. It wins over the value of the
/// site file.
pub const HA_URL: &str = "HA_URL";
/// The variable of the site file that names Home Assistant.
pub const SITE_HA_URL: &str = "AGENT_HA_URL";

/// The port of the chaperone on the LAN address.
const PEP_PORT: &str = "8300";

/// The port of TEI, the embeddings service, on the LAN address.
const TEI_PORT: &str = "8085";

/// The time between two sweeps when the variable is not set (contract 04
/// §1.6 rule 7).
const DEFAULT_FAULT_SWEEP_S: &str = "5.0";

/// The two files of the roster that a reload reads again
/// (`stage7-releases.md` §4.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RosterFiles {
    base: FilePath,
    generated: Option<FilePath>,
}

impl RosterFiles {
    /// The base roster of the deployed tree.
    #[must_use]
    pub fn base(&self) -> &FilePath {
        &self.base
    }

    /// The roster that root writes at the end of an `mcp-servers` release.
    /// A name in the two files takes its row.
    #[must_use]
    pub fn generated(&self) -> Option<&FilePath> {
        self.generated.as_ref()
    }
}

/// The door of `attendance` that the chaperone calls for a delegation and
/// for a dispatch (contract 04 §7, contract 02 §13.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Doors {
    socket: Option<SocketPath>,
    delegate_token_file: Option<TokenFilePath>,
    dispatch_token_file: Option<TokenFilePath>,
}

impl Doors {
    /// The Unix socket of `attendance`.
    #[must_use]
    pub fn socket(&self) -> Option<&SocketPath> {
        self.socket.as_ref()
    }

    /// The token file of the delegate door. The chaperone reads the file at
    /// each call, because `attendance` writes it at its own start.
    #[must_use]
    pub fn delegate_token_file(&self) -> Option<&TokenFilePath> {
        self.delegate_token_file.as_ref()
    }

    /// The token file of the dispatch door.
    #[must_use]
    pub fn dispatch_token_file(&self) -> Option<&TokenFilePath> {
        self.dispatch_token_file.as_ref()
    }
}

/// The config of the chaperone: each value that the service reads from its
/// environment.
///
/// No token is in the config. A token is in a file or in the encrypted
/// secrets (invariant 13).
///
/// FAILURE ACTION. At start, a config that is not valid stops the daemon
/// with `EX_CONFIG`, and the unit file must hold
/// `RestartPreventExitStatus=78`. The unit comment gives the reason for
/// `Restart=always` with no limit: the chaperone must return by itself when
/// a dependency returns. A config that is not valid is not such a case: no
/// restart corrects it. The Python service exits with 78 for no LAN
/// address, for a bind that it does not take and for secrets that do not
/// parse. It exits with 2 for a necessary variable that is not set. systemd
/// starts the unit again after each one, with no limit.
///
/// At a reload (`SIGHUP`), the chaperone reads the roster and the secrets
/// again, and not this config. A roster or a secrets file that does not
/// parse keeps the last good value (`roster::Roster`).
///
/// One variable has a softer rule in the Python service, and the type
/// keeps it: a `PEP_FAULT_SWEEP_INTERVAL_S` that is not a positive number
/// takes the default (contract 04 §1.6 rule 7).
/// [`ChaperoneConfig::sweep_fault`] gives the error, so the binary can
/// write it to its log.
///
/// The type is stricter than the Python reader in these places: the host
/// of a bind is an IP address or a host name, the port of a bind is not 0,
/// each path is absolute and not empty, and each URL is an [`HttpUrl`].
///
/// ```
/// use creche_contracts::config::chaperone::ChaperoneConfig;
/// use creche_contracts::config::Env;
///
/// let env = Env::from_pairs([
///     ("AGENT_LAN_ADDRESS", "192.0.2.10"),
///     ("PEP_REWORK_DIR", "/srv/agents/state/rework"),
///     ("PEP_AUDIT_DIR", "/srv/agents/state/pep/audit"),
/// ]);
/// let config = ChaperoneConfig::from_env(&env)?;
/// assert_eq!(config.bind().to_string(), "192.0.2.10:8300");
/// assert_eq!(config.tei_url().unwrap().as_str(), "http://192.0.2.10:8085");
/// # Ok::<(), creche_contracts::config::ConfigErrors>(())
/// ```
///
/// Code outside this module cannot build a config from raw parts, and cannot
/// change a field of a valid config:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::chaperone::ChaperoneConfig;
/// use creche_contracts::config::Env;
///
/// let env = Env::from_pairs([
///     ("AGENT_LAN_ADDRESS", "192.0.2.10"),
///     ("PEP_REWORK_DIR", "/srv/agents/state/rework"),
///     ("PEP_AUDIT_DIR", "/srv/agents/state/pep/audit"),
/// ]);
/// let config = ChaperoneConfig::from_env(&env).unwrap();
/// let other = ChaperoneConfig { bind: config.bind().clone(), ..config };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct ChaperoneConfig {
    rework_dir: DirPath,
    audit_dir: DirPath,
    bind: BindAddress,
    tei_url: Option<HttpUrl>,
    ha_url: Option<HttpUrl>,
    roster: Option<RosterFiles>,
    secrets_file: Option<FilePath>,
    secrets_dir: Option<DirPath>,
    doors: Doors,
    release_requests_dir: Option<DirPath>,
    approval_url: Option<HttpUrl>,
    sweep_interval: Seconds,
    sweep_fault: Option<ConfigError>,
}

// CONTRACT-QUESTION: no contract says what the chaperone does at start with
// a config that is not valid. `rust/AGENTS.md` rule 8 gives the choices. The
// comment in the unit file says that the chaperone must return by itself
// when a dependency returns. The type takes `AtStart::ExitConfig`: no
// restart corrects a config. The other reading, `AtStart::RefuseEachCall`,
// keeps `/healthz` in service. It costs a listener that binds before the
// config is valid, and the bind is a part of that config.
impl Checked for ChaperoneConfig {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
}

impl ProcessConfig for ChaperoneConfig {
    const UNIT: &'static str = "creche-chaperone.service";
}

/// The LAN address of the site file. `None` when the site gives none.
fn lan_address(env: &Env) -> Parsed<Option<LanAddress>> {
    env.parse(LAN_ADDRESS)
}

/// `host:port` of the listener: `PEP_BIND`, else the LAN address at 8300.
/// The service has no default address.
///
/// # Errors
///
/// [`ConfigErrors`] when `PEP_BIND` is not a bind, and when the two
/// variables are not set.
pub fn bind(env: &Env) -> Result<BindAddress, ConfigErrors> {
    if let Some(bind) = env.parse(BIND)? {
        return Ok(bind);
    }

    let (address, port) = all2(
        env.require::<LanAddress>(LAN_ADDRESS),
        fixed_port(BIND, PEP_PORT),
    )?;

    Ok(BindAddress::on(address.into(), port))
}

/// A port that this module holds as a constant.
fn fixed_port(variable: &'static str, port: &'static str) -> Parsed<Port> {
    port.parse()
        .map_err(|error| ConfigError::Port { variable, error }.into())
}

/// The base URL of TEI: `http://<LAN address>:8085`. `None` when the site
/// gives no LAN address. The verb `embed` then fails closed.
///
/// # Errors
///
/// [`ConfigErrors`] when the LAN address is not valid.
pub fn tei_url(env: &Env) -> Result<Option<HttpUrl>, ConfigErrors> {
    let Some(address) = lan_address(env)? else {
        return Ok(None);
    };

    Ok(Some(HttpUrl::on_lan(
        &address,
        fixed_port(LAN_ADDRESS, TEI_PORT)?,
    )))
}

/// The base URL of Home Assistant: `HA_URL`, else `AGENT_HA_URL` of the
/// site file. `None` for a site with no Home Assistant.
///
/// # Errors
///
/// [`ConfigErrors`] when the first variable that is set holds no URL.
pub fn ha_url(env: &Env) -> Result<Option<HttpUrl>, ConfigErrors> {
    env.first_set(&[HA_URL, SITE_HA_URL])
}

// CONTRACT-QUESTION: contract 04 §10 rule 7 makes the verify hook of the
// chaperone fail when the unit sets `PEP_UPSTREAMS_GENERATED` and the
// roster state is `off`, which is the state with no `PEP_UPSTREAMS`. The
// contract does not say what the service does at start in that state. The
// Python service starts and reads no roster. The type takes that reading,
// so the two accept the same environments. The stricter reading refuses
// to start. It costs a refusal that only the verify hook reports today.
/// The two roster files. `None` when `PEP_UPSTREAMS` is not set: the
/// chaperone then reads no roster, and the generated file is not read.
fn roster(env: &Env) -> Parsed<Option<RosterFiles>> {
    let base = env.parse::<FilePath>(UPSTREAMS);
    if matches!(base, Ok(None)) {
        return Ok(None);
    }

    let (base, generated) = all2(base, env.parse(UPSTREAMS_GENERATED))?;

    Ok(base.map(|base| RosterFiles { base, generated }))
}

fn doors(env: &Env) -> Parsed<Doors> {
    let (socket, delegate_token_file, dispatch_token_file) = all3(
        env.parse(SESSIOND_SOCKET),
        env.parse(DELEGATE_TOKEN_FILE),
        env.parse(DISPATCH_TOKEN_FILE),
    )?;

    Ok(Doors {
        socket,
        delegate_token_file,
        dispatch_token_file,
    })
}

/// The time between two sweeps, and the error of a value that the service
/// does not use.
fn sweep(env: &Env) -> Parsed<(Seconds, Option<ConfigError>)> {
    let default = || {
        DEFAULT_FAULT_SWEEP_S.parse().map_err(|error| {
            ConfigErrors::from(ConfigError::Seconds {
                variable: FAULT_SWEEP_INTERVAL_S,
                error,
            })
        })
    };

    match env.parse::<Seconds>(FAULT_SWEEP_INTERVAL_S) {
        Ok(Some(seconds)) => Ok((seconds, None)),
        Ok(None) => Ok((default()?, None)),
        Err(errors) => Ok((default()?, errors.as_slice().first().copied())),
    }
}

impl ChaperoneConfig {
    /// Parses the variables of the unit. The function collects each error,
    /// and an error that two parts report is in the list one time.
    ///
    /// # Errors
    ///
    /// [`ConfigErrors`] holds one error for each variable that the service
    /// cannot use.
    pub fn from_env(env: &Env) -> Result<Self, ConfigErrors> {
        let listen = all4(
            env.require(REWORK_DIR),
            env.require(AUDIT_DIR),
            bind(env),
            all2(tei_url(env), ha_url(env)),
        );
        let stores = all3(roster(env), env.parse(SECRETS), env.parse(SECRETS_DIR));
        let verbs = all4(
            doors(env),
            env.parse(RELEASE_REQUESTS_DIR),
            env.parse(APPROVAL_URL),
            sweep(env),
        );
        // The bind and the URL of TEI read the same LAN address.
        let parsed = all3(listen, stores, verbs);
        let (listen, stores, verbs) = parsed.map_err(ConfigErrors::each_once)?;
        let (rework_dir, audit_dir, bind, (tei_url, ha_url)) = listen;
        let (roster, secrets_file, secrets_dir) = stores;
        let (doors, release_requests_dir, approval_url, (sweep_interval, sweep_fault)) = verbs;

        Ok(Self {
            rework_dir,
            audit_dir,
            bind,
            tei_url,
            ha_url,
            roster,
            secrets_file,
            secrets_dir,
            doors,
            release_requests_dir,
            approval_url,
            sweep_interval,
            sweep_fault,
        })
    }

    /// The state root: `grants/`, `audit/` and `faults/pep/` are below it.
    #[must_use]
    pub fn rework_dir(&self) -> &DirPath {
        &self.rework_dir
    }

    /// The log of a request that resolves to no family.
    #[must_use]
    pub fn audit_dir(&self) -> &DirPath {
        &self.audit_dir
    }

    /// The host and the port of the listener.
    #[must_use]
    pub fn bind(&self) -> &BindAddress {
        &self.bind
    }

    /// The base URL of TEI. `None` makes the verb `embed` fail closed.
    #[must_use]
    pub fn tei_url(&self) -> Option<&HttpUrl> {
        self.tei_url.as_ref()
    }

    /// The base URL of Home Assistant. `None` for a site with none.
    #[must_use]
    pub fn ha_url(&self) -> Option<&HttpUrl> {
        self.ha_url.as_ref()
    }

    /// The two roster files. `None` when the chaperone reads no roster:
    /// `/healthz` then says `roster.state: off`.
    #[must_use]
    pub fn roster(&self) -> Option<&RosterFiles> {
        self.roster.as_ref()
    }

    /// The encrypted file of each upstream secret.
    #[must_use]
    pub fn secrets_file(&self) -> Option<&FilePath> {
        self.secrets_file.as_ref()
    }

    /// The directory of one encrypted file for each secret.
    #[must_use]
    pub fn secrets_dir(&self) -> Option<&DirPath> {
        self.secrets_dir.as_ref()
    }

    /// The doors of `attendance`. Without a part of them, the verb that
    /// needs it stays a seam.
    #[must_use]
    pub fn doors(&self) -> &Doors {
        &self.doors
    }

    /// The spool of release requests. `None` leaves the verb `release` a
    /// seam.
    #[must_use]
    pub fn release_requests_dir(&self) -> Option<&DirPath> {
        self.release_requests_dir.as_ref()
    }

    /// The hook of an approval gate. `None` leaves a gated action a seam.
    #[must_use]
    pub fn approval_url(&self) -> Option<&HttpUrl> {
        self.approval_url.as_ref()
    }

    /// The time between two sweeps of the faulted families.
    #[must_use]
    pub fn sweep_interval(&self) -> Seconds {
        self.sweep_interval
    }

    /// The error of a `PEP_FAULT_SWEEP_INTERVAL_S` that the service did not
    /// use. The service then runs with the default interval.
    #[must_use]
    pub fn sweep_fault(&self) -> Option<ConfigError> {
        self.sweep_fault
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{BindAddressError, BindHostError, PortError, SecondsError};

    /// The variables that `systemd/creche-chaperone.service` gives the
    /// daemon: each `Environment=` line and the site file.
    fn unit_env() -> Vec<(&'static str, &'static str)> {
        vec![
            ("AGENT_GITHUB_OWNER", "example-owner"),
            ("AGENT_OPERATOR_USER", "operator"),
            ("AGENT_OPERATOR_HOME", "/home/operator"),
            ("AGENT_LAN_ADDRESS", "192.0.2.10"),
            (AUDIT_DIR, "/srv/agents/state/pep/audit"),
            (UPSTREAMS, "/opt/creche/chaperone/upstreams.yaml"),
            (
                RELEASE_REQUESTS_DIR,
                "/var/lib/creche-handover/releases/requests",
            ),
            (
                UPSTREAMS_GENERATED,
                "/var/lib/creche-handover/upstreams.yaml",
            ),
            ("SOPS_AGE_KEY_FILE", "/etc/agent-pep/age.key"),
            ("HOME", "/var/lib/creche-chaperone/home"),
            ("PATH", "/usr/local/bin:/usr/bin:/bin"),
            (REWORK_DIR, "/srv/agents/state/rework"),
            (
                SESSIOND_SOCKET,
                "/srv/agents/state/rework/sock/sessiond.sock",
            ),
            (
                DELEGATE_TOKEN_FILE,
                "/srv/agents/state/rework/tokens/door-delegate.token",
            ),
            (
                DISPATCH_TOKEN_FILE,
                "/srv/agents/state/rework/tokens/door-dispatch.token",
            ),
            (APPROVAL_URL, "http://127.0.0.1:1881/hook/approval"),
        ]
    }

    fn with(pairs: &[(&'static str, &'static str)]) -> Parsed<ChaperoneConfig> {
        let mut env = unit_env();
        env.extend_from_slice(pairs);

        ChaperoneConfig::from_env(&Env::from_pairs(env))
    }

    fn without(variable: &str) -> Parsed<ChaperoneConfig> {
        let env = unit_env().into_iter().filter(|(name, _)| *name != variable);

        ChaperoneConfig::from_env(&Env::from_pairs(env))
    }

    #[test]
    fn the_unit_gives_a_valid_config() {
        let config = with(&[]).unwrap();
        let roster = config.roster().unwrap();

        assert_eq!(config.rework_dir().as_str(), "/srv/agents/state/rework");
        assert_eq!(config.audit_dir().as_str(), "/srv/agents/state/pep/audit");
        assert_eq!(config.bind().to_string(), "192.0.2.10:8300");
        assert_eq!(config.tei_url().unwrap().as_str(), "http://192.0.2.10:8085");
        assert_eq!(config.ha_url(), None);
        assert_eq!(
            roster.base().as_str(),
            "/opt/creche/chaperone/upstreams.yaml"
        );
        assert_eq!(
            roster.generated().unwrap().as_str(),
            "/var/lib/creche-handover/upstreams.yaml"
        );
        assert_eq!(config.secrets_file(), None);
        assert_eq!(config.secrets_dir(), None);
        assert_eq!(
            config.doors().socket().unwrap().as_str(),
            "/srv/agents/state/rework/sock/sessiond.sock"
        );
        assert!(
            config
                .doors()
                .delegate_token_file()
                .unwrap()
                .as_str()
                .ends_with("door-delegate.token")
        );
        assert!(
            config
                .doors()
                .dispatch_token_file()
                .unwrap()
                .as_str()
                .ends_with("door-dispatch.token")
        );
        assert_eq!(
            config.release_requests_dir().unwrap().as_str(),
            "/var/lib/creche-handover/releases/requests"
        );
        assert_eq!(
            config.approval_url().unwrap().as_str(),
            "http://127.0.0.1:1881/hook/approval"
        );
        assert_eq!(config.sweep_interval().as_secs_f64(), 5.0);
        assert_eq!(config.sweep_fault(), None);
    }

    #[test]
    fn the_two_necessary_directories_have_no_default() {
        for variable in [REWORK_DIR, AUDIT_DIR] {
            assert_eq!(
                without(variable).unwrap_err().as_slice(),
                [ConfigError::Unset { variable }]
            );
        }
    }

    #[test]
    fn the_service_has_no_default_address() {
        assert_eq!(
            without(LAN_ADDRESS).unwrap_err().as_slice(),
            [ConfigError::Unset {
                variable: LAN_ADDRESS
            }]
        );
    }

    #[test]
    fn an_explicit_bind_wins_and_needs_no_site() {
        let env = Env::from_pairs([
            (REWORK_DIR, "/tmp/root/state"),
            (AUDIT_DIR, "/tmp/root/audit"),
            (BIND, "127.0.0.1:18300"),
        ]);
        let config = ChaperoneConfig::from_env(&env).unwrap();

        assert_eq!(config.bind().to_string(), "127.0.0.1:18300");
        assert_eq!(config.tei_url(), None);
        assert_eq!(config.roster(), None);
        assert_eq!(config.doors().socket(), None);
        assert_eq!(config.release_requests_dir(), None);
        assert_eq!(config.approval_url(), None);
    }

    #[test]
    fn the_bind_is_never_each_interface_and_its_port_is_a_number() {
        let bind = |value| with(&[(BIND, value)]).unwrap_err().as_slice().to_vec();

        assert_eq!(
            bind("0.0.0.0:8300"),
            [ConfigError::Bind {
                variable: BIND,
                error: BindAddressError::Host(BindHostError::EachInterface)
            }]
        );
        assert_eq!(
            bind("192.0.2.10"),
            [ConfigError::Bind {
                variable: BIND,
                error: BindAddressError::NoPort
            }]
        );
        assert_eq!(
            bind("192.0.2.10:http"),
            [ConfigError::Bind {
                variable: BIND,
                error: BindAddressError::Port(PortError::NotANumber)
            }]
        );
        assert_eq!(
            bind(":8300"),
            [ConfigError::Bind {
                variable: BIND,
                error: BindAddressError::Host(BindHostError::Empty)
            }]
        );
    }

    #[test]
    fn the_url_of_home_assistant_is_the_first_that_is_set() {
        let site = with(&[(SITE_HA_URL, "http://192.0.2.20:8123/")]).unwrap();
        let both = with(&[
            (SITE_HA_URL, "http://192.0.2.20:8123"),
            (HA_URL, "http://192.0.2.21:8123"),
        ])
        .unwrap();

        assert_eq!(site.ha_url().unwrap().base(), "http://192.0.2.20:8123");
        assert_eq!(both.ha_url().unwrap().as_str(), "http://192.0.2.21:8123");
        assert_eq!(
            with(&[(HA_URL, "192.0.2.21")]).unwrap_err().as_slice()[0].variable(),
            HA_URL
        );
    }

    #[test]
    fn a_generated_roster_with_no_base_roster_is_not_read() {
        let config = without(UPSTREAMS).unwrap();

        assert_eq!(config.roster(), None);
    }

    #[test]
    fn the_two_secret_stores_are_optional() {
        let config = with(&[
            (SECRETS, "/opt/creche/pep/secrets.enc.yaml"),
            (SECRETS_DIR, "/var/lib/creche-handover/secrets"),
        ])
        .unwrap();

        assert_eq!(
            config.secrets_file().unwrap().as_str(),
            "/opt/creche/pep/secrets.enc.yaml"
        );
        assert_eq!(
            config.secrets_dir().unwrap().as_str(),
            "/var/lib/creche-handover/secrets"
        );
    }

    #[test]
    fn a_sweep_interval_that_is_not_valid_takes_the_default() {
        assert_eq!(
            with(&[(FAULT_SWEEP_INTERVAL_S, "0.5")])
                .unwrap()
                .sweep_interval()
                .as_secs_f64(),
            0.5
        );
        for (value, error) in [
            ("soon", SecondsError::NotANumber),
            ("0", SecondsError::NotPositive),
            ("-1", SecondsError::NotPositive),
            ("nan", SecondsError::NotFinite),
            ("inf", SecondsError::NotFinite),
        ] {
            let config = with(&[(FAULT_SWEEP_INTERVAL_S, value)]).unwrap();

            assert_eq!(config.sweep_interval().as_secs_f64(), 5.0, "{value}");
            assert_eq!(
                config.sweep_fault(),
                Some(ConfigError::Seconds {
                    variable: FAULT_SWEEP_INTERVAL_S,
                    error
                }),
                "{value}"
            );
        }
    }

    #[test]
    fn a_value_that_the_service_cannot_use_is_an_error_of_its_variable() {
        for (variable, value) in [
            (REWORK_DIR, "state"),
            (AUDIT_DIR, "audit"),
            (UPSTREAMS, "upstreams.yaml"),
            (UPSTREAMS_GENERATED, "generated.yaml"),
            (SECRETS, "secrets.enc.yaml"),
            (SECRETS_DIR, "secrets"),
            (SESSIOND_SOCKET, "sessiond.sock"),
            (DELEGATE_TOKEN_FILE, "door-delegate.token"),
            (DISPATCH_TOKEN_FILE, "door-dispatch.token"),
            (RELEASE_REQUESTS_DIR, "requests"),
            (APPROVAL_URL, "127.0.0.1:1881"),
            (LAN_ADDRESS, "0.0.0.0"),
        ] {
            let errors = with(&[(variable, value)]).unwrap_err();

            assert!(
                errors
                    .as_slice()
                    .iter()
                    .all(|error| error.variable() == variable),
                "{variable}: {errors}"
            );
        }
    }

    #[test]
    fn the_parse_collects_the_error_of_each_variable() {
        let errors = with(&[
            (REWORK_DIR, "state"),
            (BIND, "0.0.0.0:8300"),
            (SECRETS, "x"),
            (APPROVAL_URL, "x"),
        ])
        .unwrap_err();
        let variables: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(variables, [REWORK_DIR, BIND, SECRETS, APPROVAL_URL]);
    }

    #[test]
    fn a_lan_address_that_is_not_valid_is_one_error() {
        // The bind and the URL of TEI read the same variable.
        let errors = with(&[(LAN_ADDRESS, "0.0.0.0")]).unwrap_err();

        assert_eq!(errors.as_slice().len(), 1, "{errors}");
        assert_eq!(errors.as_slice()[0].variable(), LAN_ADDRESS);
    }

    #[test]
    fn the_parse_collects_the_error_of_each_roster_file() {
        let errors = with(&[
            (UPSTREAMS, "upstreams.yaml"),
            (UPSTREAMS_GENERATED, "generated.yaml"),
        ])
        .unwrap_err();
        let variables: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(variables, [UPSTREAMS, UPSTREAMS_GENERATED]);
    }

    #[test]
    fn a_generated_roster_with_no_base_roster_is_not_parsed() {
        // The Python service does not read the variable then.
        let env = unit_env()
            .into_iter()
            .filter(|(name, _)| *name != UPSTREAMS);
        let env = env.chain([(UPSTREAMS_GENERATED, "generated.yaml")]);
        let config = ChaperoneConfig::from_env(&Env::from_pairs(env)).unwrap();

        assert_eq!(config.roster(), None);
    }

    #[test]
    fn the_failure_action_is_an_exit_with_no_restart() {
        assert_eq!(ChaperoneConfig::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(ChaperoneConfig::FAILURE.at_reload(), AtReload::NotRead);
        assert_eq!(ChaperoneConfig::UNIT, "creche-chaperone.service");
    }
}
